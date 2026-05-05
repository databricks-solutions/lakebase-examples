"""
Databricks Genie API client.
Handles conversation lifecycle, message polling, and SQL execution.
"""

import logging
import httpx
import asyncio
from typing import Any, Dict, Optional

from app.auth import ensure_https
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


def _default_workspace_bearer_token() -> str:
    """Bearer for Genie/SQL when RuntimeSettings is absent (non-request paths)."""
    from app.auth import get_service_principal_token

    if getattr(settings, "auth_use_app_service_principal", False):
        return (get_service_principal_token() or settings.databricks_token or "").strip()
    if settings.genie_force_app_sp_token:
        return (get_service_principal_token() or settings.databricks_token or "").strip()
    return (settings.databricks_token or "").strip()


def _alternate_genie_runtime_settings(runtime_settings: Optional["RuntimeSettings"]):
    """On Genie 403, retry once with alternate identity vs the primary Genie bearer.

    With AUTH_USE_APP_SERVICE_PRINCIPAL (default): primary is app SP; alternate is user JWT.
    Otherwise: primary is usually user JWT; alternate is app SP (legacy).
    GENIE_FORCE_APP_SERVICE_PRINCIPAL without unified auth behaves like the unified-SP case.
    """
    from app.auth import get_service_principal_token
    from app.runtime_config import RuntimeSettings as RS

    sp = get_service_principal_token()
    if not sp or not runtime_settings:
        return None

    user_jwt = (runtime_settings.user_token or "").strip()
    sp_tok = sp.strip()
    if not user_jwt or user_jwt == sp_tok:
        return None

    sp_primary = getattr(settings, "auth_use_app_service_principal", False) or settings.genie_force_app_sp_token

    if sp_primary:
        if runtime_settings._genie_bearer_override is not None:
            return None
        return RS(
            runtime_settings.runtime,
            runtime_settings.user_token,
            runtime_settings.user_email,
            genie_bearer_override=user_jwt,
        )

    curr = (runtime_settings.genie_rest_token or "").strip()
    if curr == sp_tok:
        return None
    return RS(
        runtime_settings.runtime,
        sp_tok,
        runtime_settings.user_email,
        genie_bearer_override=None,
    )


def _alternate_sql_runtime_settings(runtime_settings: Optional["RuntimeSettings"]):
    """On SQL statements 403, retry once with alternate identity for the warehouse API."""
    from app.auth import get_service_principal_token
    from app.runtime_config import RuntimeSettings as RS

    sp = get_service_principal_token()
    if not sp or not runtime_settings:
        return None
    sp_tok = sp.strip()
    curr = (runtime_settings.databricks_token or "").strip()
    ut = (runtime_settings.user_token or "").strip()
    if ut == sp_tok:
        ut = ""

    auth_sp_all = getattr(settings, "auth_use_app_service_principal", False)

    if auth_sp_all:
        if curr != sp_tok or not ut:
            return None
        return RS(
            runtime_settings.runtime,
            ut,
            runtime_settings.user_email,
            sql_bearer_override=ut,
        )

    if not ut:
        return None
    if curr == sp_tok:
        return None
    return RS(runtime_settings.runtime, sp_tok, runtime_settings.user_email)


class GenieRateLimitError(Exception):
    """Raised when Genie API returns 429 Too Many Requests."""
    def __init__(self, retry_after: float = 60.0):
        self.retry_after = retry_after
        super().__init__(f"Genie API rate limited. Retry after {retry_after}s")


class GenieConfigError(Exception):
    """Raised for non-retryable errors (404 space not found, 401 unauthorized, 403 forbidden)."""
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Genie API {status_code}: {detail}")


def _genie_status_canonical(status) -> Optional[str]:
    """Genie REST status strings may vary in casing; normalize for comparisons."""
    if isinstance(status, str):
        return status.upper()
    return None


def _extract_sql_from_genie_attachments(attachments: list) -> Optional[str]:
    """Best-effort SQL extraction from Genie message attachments (schema varies)."""
    if not attachments:
        return None

    def from_obj(obj):
        if not isinstance(obj, dict):
            return None
        q = obj.get("query")
        if isinstance(q, str) and q.strip():
            return q.strip()
        if isinstance(q, dict):
            sql = q.get("query") or q.get("sql") or q.get("statement")
            if isinstance(sql, str) and sql.strip():
                return sql.strip()
        for key in ("sql", "sql_query", "statement"):
            v = obj.get(key)
            if isinstance(v, str) and len(v.strip()) > 8:
                return v.strip()
        nested = obj.get("suggested_queries") or obj.get("queries")
        if isinstance(nested, list):
            for item in nested:
                s = from_obj(item) if isinstance(item, dict) else None
                if s:
                    return s
        return None

    for att in attachments:
        if not isinstance(att, dict):
            continue
        sql = from_obj(att)
        if sql:
            return sql
        inner = att.get("attachment") or att.get("payload")
        if isinstance(inner, dict):
            sql = from_obj(inner)
            if sql:
                return sql
        if isinstance(inner, list):
            for sub in inner:
                if isinstance(sub, dict):
                    sql = from_obj(sub)
                    if sql:
                        return sql
    return None


class GenieService:
    def __init__(self):
        host = ensure_https(settings.databricks_host)
        self.default_base_url = f"{host}/api/2.0/genie" if host else ""
        tok = _default_workspace_bearer_token()
        self.default_headers = {
            "Authorization": f"Bearer {tok}",
            "Content-Type": "application/json",
        } if tok else {}

    def _get_config(self, runtime_settings=None):
        """Get configuration (runtime or default)."""
        if runtime_settings:
            host = ensure_https(runtime_settings.databricks_host)
            token = runtime_settings.genie_rest_token

            if not token or (isinstance(token, str) and token.strip() == ""):
                logger.warning("Empty authentication token — Genie call will fail")

            return (
                f"{host}/api/2.0/genie",
                {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
        host = ensure_https(settings.databricks_host)
        base = f"{host}/api/2.0/genie" if host else ""
        tok = _default_workspace_bearer_token()
        headers = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"} if tok else {}
        return base, headers

    async def start_conversation(self, space_id: str, query: str, runtime_settings=None) -> Dict:
        """
        Start a new conversation with Genie (creates conversation and sends first message).
        Reference: https://docs.databricks.com/api/workspace/genie/startconversation
        """
        base_url, headers = self._get_config(runtime_settings)
        url = f"{base_url}/spaces/{space_id}/start-conversation"
        payload = {"content": query}

        logger.info("Genie start-conversation space=%s query=%r url=%s token_len=%d", space_id, query[:80], url, len(headers.get("Authorization", "")) if headers else 0)

        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, json=payload, timeout=30.0)

            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", 60))
                logger.warning("Genie API rate limited (429). Retry-After: %ss", retry_after)
                raise GenieRateLimitError(retry_after)

            if response.status_code in (401, 403, 404):
                detail = {
                    401: f"Unauthorized. Check your token/credentials for {space_id}",
                    403: f"Forbidden. The service principal or user lacks access to Genie Space {space_id}",
                    404: f"Genie Space '{space_id}' not found. Verify the Space ID exists on this workspace",
                }[response.status_code]
                logger.error(
                    "Genie config error %d: %s — host=%s body=%s",
                    response.status_code,
                    detail,
                    base_url.partition("/api")[0],
                    response.text[:500],
                )
                err = GenieConfigError(response.status_code, detail)
                if response.status_code == 403:
                    rs_sp = _alternate_genie_runtime_settings(runtime_settings)
                    if rs_sp is None and runtime_settings is not None:
                        from app.auth import get_service_principal_token
                        if not get_service_principal_token():
                            logger.error(
                                "Genie returned 403 and app SP OAuth token unavailable "
                                "(check DATABRICKS_CLIENT_ID/SECRET, DATABRICKS_HOST, and /oidc/v1/token)."
                            )
                    if rs_sp is not None:
                        logger.warning(
                            "Genie start-conversation 403 with primary token; retrying with alternate identity"
                        )
                        return await self.start_conversation(space_id, query, rs_sp)
                raise err

            if not response.is_success:
                body = response.text[:500]
                logger.error("Genie API error %d: %s", response.status_code, body)
                raise Exception(f"Genie API {response.status_code}: {body}")
            data = response.json()

            conversation_id = data.get("conversation_id")
            message_id = data.get("message_id")

            return await self._poll_message(space_id, conversation_id, message_id, runtime_settings)

    async def send_message(
        self,
        space_id: str,
        conversation_id: str,
        query: str,
        runtime_settings=None
    ) -> Dict:
        """
        Send a message to an existing Genie conversation.
        Reference: https://docs.databricks.com/api/workspace/genie/createmessage
        """
        base_url, headers = self._get_config(runtime_settings)

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{base_url}/spaces/{space_id}/conversations/{conversation_id}/messages",
                headers=headers,
                json={"content": query},
                timeout=30.0
            )

            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", 60))
                logger.warning("Genie API rate limited (429) on send_message. Retry-After: %ss", retry_after)
                raise GenieRateLimitError(retry_after)

            if response.status_code in (401, 403, 404):
                detail = {
                    401: f"Unauthorized. Check your token/credentials",
                    403: f"Forbidden. Lacks access to Genie Space {space_id}",
                    404: f"Conversation {conversation_id} or Space {space_id} not found",
                }[response.status_code]
                logger.error("Genie config error %d on send_message: %s", response.status_code, detail)
                err = GenieConfigError(response.status_code, detail)
                if response.status_code == 403:
                    rs_sp = _alternate_genie_runtime_settings(runtime_settings)
                    if rs_sp is not None:
                        logger.warning(
                            "Genie send_message 403 with primary Genie bearer; retrying alternate identity"
                        )
                        return await self.send_message(
                            space_id, conversation_id, query, rs_sp
                        )
                raise err

            response.raise_for_status()
            message_data = response.json()
            message_id = message_data.get("message_id")

            return await self._poll_message(space_id, conversation_id, message_id, runtime_settings)

    async def _poll_message(
        self,
        space_id: str,
        conversation_id: str,
        message_id: str,
        runtime_settings=None
    ) -> Dict:
        """
        Poll for message completion.
        Reference: https://docs.databricks.com/api/workspace/genie/getmessage
        """
        base_url, headers = self._get_config(runtime_settings)

        async with httpx.AsyncClient() as client:
            max_attempts = 60
            for attempt in range(max_attempts):
                await asyncio.sleep(2)

                response = await client.get(
                    f"{base_url}/spaces/{space_id}/conversations/{conversation_id}/messages/{message_id}",
                    headers=headers,
                    timeout=30.0
                )
                response.raise_for_status()
                data = response.json()

                status_key = _genie_status_canonical(data.get("status"))

                if status_key == "COMPLETED":
                    attachments = data.get("attachments", [])
                    sql_query = _extract_sql_from_genie_attachments(attachments)
                    if not sql_query and attachments:
                        keys_preview = []
                        for a in attachments[:3]:
                            if isinstance(a, dict):
                                keys_preview.append(list(a.keys())[:14])
                            else:
                                keys_preview.append(type(a).__name__)
                        logger.warning(
                            "Genie COMPLETED but no SQL extracted (semantic cache skip). "
                            "attachment_count=%s first_attachment_keys=%s",
                            len(attachments),
                            keys_preview,
                        )
                    return {
                        "conversation_id": conversation_id,
                        "message_id": message_id,
                        "status": "COMPLETED",
                        "attachments": attachments,
                        "sql_query": sql_query,
                        "result": attachments
                    }

                elif status_key in ("FAILED", "CANCELLED"):
                    error_obj = data.get("error", {})
                    return {
                        "conversation_id": conversation_id,
                        "message_id": message_id,
                        "status": status_key,
                        "error": error_obj.get("error", "Unknown error"),
                        "error_type": error_obj.get("type")
                    }

                elif status_key == "QUERY_RESULT_EXPIRED":
                    return {
                        "conversation_id": conversation_id,
                        "message_id": message_id,
                        "status": "QUERY_RESULT_EXPIRED",
                        "error": "Query result expired. Please rerun the query."
                    }

            return {
                "conversation_id": conversation_id,
                "message_id": message_id,
                "status": "TIMEOUT",
                "error": "Query timed out after 120 seconds"
            }

    async def execute_sql(self, sql_query: str, runtime_settings=None) -> Dict:
        """Execute SQL query against warehouse."""
        rs = runtime_settings if runtime_settings else settings

        host = ensure_https(rs.databricks_host)
        sql_url = f"{host}/api/2.0/sql/statements"
        headers = {
            "Authorization": f"Bearer {rs.databricks_token}",
            "Content-Type": "application/json"
        } if runtime_settings else self.default_headers

        logger.info("Executing SQL via warehouse=%s query=%s", rs.sql_warehouse_id, sql_query[:100])

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    sql_url,
                    headers=headers,
                    json={
                        "statement": sql_query,
                        "warehouse_id": rs.sql_warehouse_id,
                        "wait_timeout": "30s"
                    },
                    timeout=60.0
                )
                response.raise_for_status()
                data = response.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 403 and runtime_settings is not None:
                    rs_alt = _alternate_sql_runtime_settings(runtime_settings)
                    if rs_alt is not None:
                        logger.warning(
                            "SQL statements API returned 403; retrying with alternate identity"
                        )
                        return await self.execute_sql(sql_query, rs_alt)
                logger.error(
                    "SQL warehouse error %d: %s",
                    e.response.status_code,
                    e.response.text[:200],
                )
                raise

            statement_id = data.get("statement_id")
            status = data.get("status", {}).get("state")

            if status not in ["SUCCEEDED", "FAILED", "CANCELED"]:
                max_attempts = 30
                for attempt in range(max_attempts):
                    await asyncio.sleep(1)

                    status_response = await client.get(
                        f"{host}/api/2.0/sql/statements/{statement_id}",
                        headers=headers,
                        timeout=30.0
                    )
                    status_response.raise_for_status()
                    data = status_response.json()
                    status = data.get("status", {}).get("state")

                    if status in ["SUCCEEDED", "FAILED", "CANCELED"]:
                        break

            # Extract columns from manifest and combine with data_array
            manifest = data.get("manifest", {})
            schema_cols = manifest.get("schema", {}).get("columns", [])
            columns = [c.get("name", "") for c in schema_cols]
            raw_result = data.get("result") or {}
            data_array = raw_result.get("data_array", []) if isinstance(raw_result, dict) else []
            row_count = raw_result.get("row_count", len(data_array)) if isinstance(raw_result, dict) else 0

            structured_result = {
                "columns": columns,
                "data_array": data_array,
                "row_count": row_count,
            } if status == "SUCCEEDED" else None

            return {
                "statement_id": statement_id,
                "status": status,
                "result": structured_result,
                "error": data.get("status", {}).get("error")
            }


genie_service = GenieService()
