"""
Authentication helper for Databricks API calls.
Handles both Service Principal and User Auth tokens using Databricks SDK.

In Databricks Apps, DATABRICKS_TOKEN is always set alongside DATABRICKS_CLIENT_ID /
DATABRICKS_CLIENT_SECRET. Unified-auth tools may prefer PAT and authenticate as the
wrong identity — Genie Space bindings are on the *app service principal*.
We therefore fetch an SP access token via the workspace OAuth endpoint when
client-credentials env vars exist.
"""

import logging
import os
import time
from typing import Optional

import httpx
from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)

_sp_workspace_client: Optional[WorkspaceClient] = None
_m2m_token: Optional[str] = None
_m2m_not_after: float = 0.0  # monotonic deadline


def get_service_principal_client() -> Optional[WorkspaceClient]:
    """Get WorkspaceClient authenticated with Service Principal."""
    global _sp_workspace_client

    if _sp_workspace_client:
        return _sp_workspace_client

    client_id = os.getenv("DATABRICKS_CLIENT_ID")
    client_secret = os.getenv("DATABRICKS_CLIENT_SECRET")

    if not client_id or not client_secret:
        logger.warning("DATABRICKS_CLIENT_ID or DATABRICKS_CLIENT_SECRET not set")
        return None

    try:
        _sp_workspace_client = WorkspaceClient()
        logger.info("Service Principal WorkspaceClient initialized")
        return _sp_workspace_client
    except Exception as e:
        logger.error("Failed to create Service Principal client: %s", e)
        return None


def _workspace_m2m_access_token() -> Optional[str]:
    """Workspace OAuth client_credentials token (scopes the SP for REST APIs it may call)."""
    global _m2m_token, _m2m_not_after

    client_id = os.getenv("DATABRICKS_CLIENT_ID")
    client_secret = os.getenv("DATABRICKS_CLIENT_SECRET")
    host = os.getenv("DATABRICKS_HOST", "").strip().rstrip("/")
    if not client_id or not client_secret or not host:
        return None

    now = time.monotonic()
    if _m2m_token and now < _m2m_not_after:
        return _m2m_token

    base = ensure_https(host)
    url = f"{base.rstrip('/')}/oidc/v1/token"
    try:
        with httpx.Client() as cx:
            r = cx.post(
                url,
                auth=(client_id, client_secret),
                data={
                    "grant_type": "client_credentials",
                    "scope": "all-apis",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30.0,
            )
        if not r.is_success:
            logger.warning(
                "Workspace M2M token request failed (%s): %s",
                r.status_code,
                (r.text or "")[:256],
            )
            return None
        body = r.json()
        tok = body.get("access_token")
        if not tok:
            logger.warning("Workspace M2M response missing access_token")
            return None
        expires_in = float(body.get("expires_in") or 3600)
        # Refresh a bit early
        skew = max(120.0, expires_in * 0.05)
        _m2m_token = tok
        _m2m_not_after = now + max(300.0, expires_in - skew)
        logger.info("Fetched app service principal OAuth token via client_credentials")
        return tok
    except Exception:
        logger.exception("Workspace M2M token request threw")
        return None


def get_service_principal_token() -> Optional[str]:
    """OAuth2 access token for the app service principal (Databricks Apps SP when ID/secret are set).

    Prefer explicit client_credentials exchange so PAT (DATABRICKS_TOKEN) does not override identity.
    """
    tok = _workspace_m2m_access_token()
    if tok:
        return tok

    client = get_service_principal_client()
    if not client:
        token = os.getenv("DATABRICKS_TOKEN", "")
        if token:
            logger.debug("Using DATABRICKS_TOKEN from environment (local dev)")
        return token or None

    try:
        if hasattr(client.config, '_header_factory') and callable(client.config._header_factory):
            auth_headers = client.config._header_factory()
            if isinstance(auth_headers, dict) and 'Authorization' in auth_headers:
                auth_value = auth_headers['Authorization']
                if auth_value.startswith('Bearer '):
                    return auth_value[7:]
                return auth_value

        if hasattr(client.config, '_credentials_strategy'):
            creds = client.config._credentials_strategy
            if hasattr(creds, 'token') and callable(creds.token):
                sdk_tok = creds.token(client.config)
                if sdk_tok:
                    return sdk_tok

        logger.warning("Could not extract token from SDK, falling back to env var")
        return os.getenv("DATABRICKS_TOKEN") or None

    except Exception:
        logger.exception("Failed to get token from SDK")
        return os.getenv("DATABRICKS_TOKEN") or None


def ensure_https(host: str) -> str:
    """Ensure host has https:// protocol."""
    if not host:
        return ""
    if not host.startswith(('http://', 'https://')):
        return f"https://{host}"
    return host
