from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, AliasChoices
from functools import lru_cache
import os
from pathlib import Path

# Resolve .env paths relative to the project root (parent of backend/)
_BACKEND_DIR = Path(__file__).resolve().parent.parent  # backend/
_PROJECT_DIR = _BACKEND_DIR.parent  # project root
_ENV_FILES = [
    str(_PROJECT_DIR / ".env"),
    str(_BACKEND_DIR / ".env"),
]


class Settings(BaseSettings):
    # Databricks - automatically provided in Databricks Apps environment or from frontend
    # Strip trailing slashes to avoid double slash issues in URLs
    # All optional (can be provided via frontend config)
    databricks_host: str = Field(default_factory=lambda: os.getenv("DATABRICKS_HOST", "").rstrip('/'))
    databricks_token: str = Field(default_factory=lambda: os.getenv("DATABRICKS_TOKEN", ""))
    # When true (default in Apps), Genie, SQL Warehouse, Model Serving (embeddings), and other
    # workspace REST calls use the app's OAuth service principal (DATABRICKS_CLIENT_ID/SECRET M2M),
    # not the end-user JWT. RBAC still uses X-Forwarded-Email + user token from headers only.
    # Set to false for local dev with user OAuth only.
    auth_use_app_service_principal: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "AUTH_USE_APP_SERVICE_PRINCIPAL",
            "USE_APP_SP_FOR_DATABRICKS_API",
        ),
    )
    # When true, Genie REST calls (spaces, conversations) use app OAuth M2M (DATABRICKS_CLIENT_*)
    # instead of the user's X-Forwarded-Access-Token. Use when Genie denies the delegated user even
    # though Catalog > Genie space sharing shows the app's service principal allowed.
    genie_force_app_sp_token: bool = Field(
        default=False,
        validation_alias=AliasChoices("GENIE_FORCE_APP_SERVICE_PRINCIPAL", "GENIE_FORCE_APP_SP_TOKEN"),
    )
    # When True, embedding calls use app SP (DATABRICKS_CLIENT_*) instead of X-Forwarded-Access-Token.
    # Delegated user tokens often lack OAuth scope "model-serving", which Model Serving rejects.
    embedding_force_app_sp_token: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "EMBEDDING_FORCE_APP_SERVICE_PRINCIPAL",
            "EMBEDDING_FORCE_APP_SP_TOKEN",
        ),
    )
    genie_space_id: str = Field(default="")  # Can be provided from frontend (backward compat)
    genie_spaces: list = Field(default_factory=list)  # List of {"id": "...", "name": "..."}
    sql_warehouse_id: str = Field(default="")  # Can be provided from frontend
    
    # Application environment
    app_env: str = os.getenv("APP_ENV", "development")  # development, production
    
    # Application settings
    max_queries_per_minute: int = int(os.getenv("MAX_QUERIES_PER_MINUTE", "5"))
    similarity_threshold: float = float(os.getenv("SIMILARITY_THRESHOLD", "0.92"))
    cache_ttl_hours: float = float(os.getenv("CACHE_TTL_HOURS", "24"))  # 0 = no freshness limit
    # When true, semantic cache matches ignore per-user identity filtering.
    shared_cache: bool = True  # False = per-identity cache slices; also set per-gateway / global_settings

    # Embedding configuration
    embedding_provider: str = os.getenv("EMBEDDING_PROVIDER", "databricks")  # databricks or local
    databricks_embedding_endpoint: str = os.getenv("DATABRICKS_EMBEDDING_ENDPOINT", "databricks-gte-large-en")
    local_embedding_model: str = os.getenv("LOCAL_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    
    # Storage backend — Lakebase (pgvector) only
    storage_backend: str = os.getenv("STORAGE_BACKEND", "pgvector")
    
    # Databricks Unity Catalog configuration (for production)
    catalog_name: str = os.getenv("CATALOG_NAME", "main")
    schema_name: str = os.getenv("SCHEMA_NAME", "genie_cache")
    cache_table_name: str = os.getenv("CACHE_TABLE_NAME", "cached_queries")
    
    # PostgreSQL + PGVector configuration
    postgres_host: str = os.getenv("POSTGRES_HOST", "localhost")
    postgres_port: int = int(os.getenv("POSTGRES_PORT", "5432"))
    postgres_user: str = os.getenv("POSTGRES_USER", "postgres")
    postgres_password: str = os.getenv("POSTGRES_PASSWORD", "")
    postgres_database: str = os.getenv("POSTGRES_DATABASE", "genie_cache")
    postgres_sslmode: str = os.getenv("POSTGRES_SSLMODE", "prefer")  # prefer, require, disable
    pgvector_table_name: str = os.getenv("PGVECTOR_TABLE_NAME", "cached_queries")
    
    # Databricks Lakebase configuration (overrides standard PostgreSQL settings)
    # Example: instance-xxx.database.azuredatabricks.net
    lakebase_instance: str = os.getenv("LAKEBASE_INSTANCE", "")
    lakebase_catalog: str = os.getenv("LAKEBASE_CATALOG", "")  # e.g., sean_lakebase_genie
    lakebase_schema: str = os.getenv("LAKEBASE_SCHEMA", "public")
    
    @property
    def postgres_connection_string(self) -> str:
        """Build PostgreSQL connection string"""
        from urllib.parse import quote_plus
        
        # Use Lakebase settings if configured
        if self.lakebase_instance:
            host = self.lakebase_instance
            database = "databricks_postgres"  # Standard for Lakebase
            # For Lakebase, we need the actual Databricks user email, not 'postgres'
            # This should come from runtime config or environment
            user = quote_plus(self.postgres_user) if self.postgres_user else quote_plus("postgres")
            password = quote_plus(self.postgres_password)
            sslmode = self.postgres_sslmode
            
            return f"postgresql://{user}:{password}@{host}:{self.postgres_port}/{database}?sslmode={sslmode}"
        
        # Standard PostgreSQL connection
        return f"postgresql://{self.postgres_user}:{self.postgres_password}@{self.postgres_host}:{self.postgres_port}/{self.postgres_database}"
    
    @property
    def full_table_name(self) -> str:
        """Get full table name with catalog/schema prefix if using Lakebase"""
        if self.lakebase_catalog:
            # Lakebase uses three-level namespace: catalog.schema.table
            return f"{self.lakebase_catalog}.{self.lakebase_schema}.{self.pgvector_table_name}"

        if self.lakebase_schema and self.lakebase_schema != "public":
            return f"{self.lakebase_schema}.{self.pgvector_table_name}"

        # Plain PostgreSQL: bare table name, schema resolved via search_path
        return self.pgvector_table_name
    
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"
    
    @property
    def is_databricks(self) -> bool:
        """Check if running in Databricks Apps environment"""
        return bool(os.getenv("DATABRICKS_RUNTIME_VERSION")) or self.storage_backend == "databricks"
    
    model_config = SettingsConfigDict(
        env_file=_ENV_FILES,
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache()
def get_settings() -> Settings:
    return Settings()
