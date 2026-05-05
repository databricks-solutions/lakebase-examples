"""
Databricks Foundation Model API for embeddings.
Uses Databricks SDK for clean, authenticated API calls.
"""

from __future__ import annotations

import logging
from typing import Any, List

from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config

from app.auth import ensure_https, get_service_principal_token
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


def _embedding_scope_denied(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "model-serving" in msg and "scope" in msg


class DatabricksEmbeddingService:
    """
    Embedding service using Databricks SDK.
    Supports Foundation Model endpoints like databricks-gte-large-en.
    """

    def __init__(self):
        self.default_endpoint = settings.databricks_embedding_endpoint

    def _resolve_host(self, runtime_settings=None) -> str:
        h = ""
        if runtime_settings:
            h = (runtime_settings.databricks_host or "").strip()
        if not h:
            h = (settings.databricks_host or "").strip()
        return ensure_https(h).rstrip("/")

    def _resolve_endpoint(self, runtime_settings=None) -> str:
        if runtime_settings:
            ep = getattr(runtime_settings, "databricks_embedding_endpoint", None) or ""
            if ep.strip():
                return ep.strip()
        return (self.default_endpoint or "").strip() or settings.databricks_embedding_endpoint

    def _workspace_client_pat(self, host: str, token: str) -> WorkspaceClient:
        config = Config(host=host, token=token.strip(), auth_type="pat")
        return WorkspaceClient(config=config)

    def _get_workspace_client(self, runtime_settings=None):
        """Return (WorkspaceClient, endpoint_name).

        Uses the same bearer as SQL/Genie: app SP when AUTH_USE_APP_SERVICE_PRINCIPAL (default),
        or EMBEDDING_FORCE_APP_* when unified auth is off.
        """
        host = self._resolve_host(runtime_settings)
        if not host:
            raise RuntimeError("DATABRICKS_HOST is not configured for embeddings")

        endpoint = self._resolve_endpoint(runtime_settings)

        prefer_sp = getattr(
            settings, "auth_use_app_service_principal", False
        ) or settings.embedding_force_app_sp_token
        if prefer_sp:
            sp = get_service_principal_token()
            if sp and sp.strip():
                return self._workspace_client_pat(host, sp.strip()), endpoint
            if getattr(settings, "auth_use_app_service_principal", False):
                logger.warning(
                    "AUTH_USE_APP_SERVICE_PRINCIPAL but SP token missing; "
                    "falling back to user/PAT for embeddings"
                )

        if runtime_settings:
            token = (runtime_settings.databricks_token or "").strip()
            if not token:
                raise RuntimeError(
                    "No token available for embeddings (configure app OAuth or user token)."
                )
            return self._workspace_client_pat(host, token), endpoint

        token = (settings.databricks_token or "").strip()
        if not token:
            raise RuntimeError("DATABRICKS_TOKEN is not set — cannot call embedding endpoint")
        return self._workspace_client_pat(host, token), endpoint

    @staticmethod
    def _extract_embeddings(response: Any) -> List[List[float]]:
        embeddings = None
        response_dict = response.as_dict() if hasattr(response, "as_dict") else None

        if hasattr(response, "predictions") and response.predictions is not None:
            embeddings = response.predictions
        elif hasattr(response, "data") and response.data is not None:
            embeddings = [
                item.embedding if hasattr(item, "embedding") else item.get("embedding")
                for item in response.data
            ]
        elif response_dict:
            if response_dict.get("predictions"):
                embeddings = response_dict["predictions"]
            elif response_dict.get("data"):
                data = response_dict["data"]
                if isinstance(data, list) and len(data) > 0:
                    if isinstance(data[0], dict) and "embedding" in data[0]:
                        embeddings = [item["embedding"] for item in data]
                    else:
                        embeddings = data

        if embeddings is None:
            raise ValueError(
                f"Could not extract embeddings from response type={type(response)}"
            )

        return embeddings

    def get_embedding(self, text: str, runtime_settings=None) -> List[float]:
        """Generate embedding for a single text using Databricks SDK."""
        return self.get_embeddings([text], runtime_settings)[0]

    def get_embeddings(self, texts: List[str], runtime_settings=None) -> List[List[float]]:
        """Generate embeddings for multiple texts using Databricks SDK."""

        def _run(client: WorkspaceClient, endpoint: str) -> List[List[float]]:
            logger.info("Embedding API call: endpoint=%s texts=%d", endpoint, len(texts))
            response = client.serving_endpoints.query(name=endpoint, input=texts)
            embeddings = self._extract_embeddings(response)
            logger.info("Got %d embeddings", len(embeddings))
            return embeddings

        prefer_sp = getattr(
            settings, "auth_use_app_service_principal", False
        ) or settings.embedding_force_app_sp_token

        try:
            client, endpoint = self._get_workspace_client(runtime_settings)
            return _run(client, endpoint)
        except Exception as first:
            if prefer_sp:
                logger.exception("Error calling Databricks embedding API")
                raise
            if not _embedding_scope_denied(first):
                logger.exception("Error calling Databricks embedding API")
                raise
            sp = get_service_principal_token()
            if not sp or not sp.strip():
                logger.exception("Error calling Databricks embedding API")
                raise
            logger.warning(
                "Embedding PermissionDenied on user token (model-serving scope); "
                "retrying with app service principal"
            )
            try:
                host = self._resolve_host(runtime_settings)
                endpoint = self._resolve_endpoint(runtime_settings)
                client = self._workspace_client_pat(host, sp.strip())
                return _run(client, endpoint)
            except Exception:
                logger.exception("Databricks embedding API failed after SP retry")
                raise


class LocalEmbeddingService:
    """
    Local embedding service using sentence-transformers.
    Used as fallback when Databricks API is not available.
    """

    def __init__(self):
        try:
            from sentence_transformers import SentenceTransformer

            self.model = SentenceTransformer(settings.local_embedding_model)
        except ImportError as e:
            raise ImportError(
                "sentence-transformers not installed. "
                "Install with: pip install sentence-transformers"
            ) from e

    def get_embedding(self, text: str) -> List[float]:
        """Generate embedding for a single text."""
        embedding = self.model.encode(text)
        return embedding.tolist()

    def get_embeddings(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings for multiple texts."""
        embeddings = self.model.encode(texts)
        return embeddings.tolist()


def get_embedding_service():
    """Get embedding service based on configuration."""
    if settings.embedding_provider == "databricks":
        return DatabricksEmbeddingService()
    return LocalEmbeddingService()


embedding_service = get_embedding_service()
