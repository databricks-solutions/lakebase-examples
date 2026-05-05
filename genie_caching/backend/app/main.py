import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from starlette.middleware.wsgi import WSGIMiddleware
import asyncio
from contextlib import asynccontextmanager
from app.api.routes import router
from app.api.genie_clone_routes import genie_clone_router
from app.api.gateway_routes import gateway_router
from app.api.mcp_routes import mcp_router
from app.api.rbac_routes import rbac_router
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize storage backend (creates connection pool if using Lakebase/PGVector)
    from app.services.database import initialize_storage
    storage = await initialize_storage()

    # Hydrate global settings from Lakebase so they survive redeploys
    try:
        from app.api.config_store import load_global_settings_from_db
        await load_global_settings_from_db()
    except Exception as e:
        logger.warning("Global settings hydrate failed (continuing with env defaults): %s", e)

    # Start periodic JWT refresh for all Lakebase backends
    refresh_task = None
    if settings.storage_backend in ("lakebase", "pgvector") and settings.lakebase_instance:
        async def _token_refresh_loop():
            while True:
                await asyncio.sleep(30 * 60)  # Every 30 minutes
                try:
                    logger.info("Background JWT refresh: checking all backends")
                    await storage.refresh_all_backends()
                except Exception as e:
                    logger.error("Background JWT refresh failed: %s", e)

        refresh_task = asyncio.create_task(_token_refresh_loop())
        logger.info("Started background JWT refresh task (every 30 min)")

    yield

    if refresh_task:
        refresh_task.cancel()
    try:
        tasks = ([refresh_task] if refresh_task else [])
        await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        pass
    finally:
        from app.services.rbac import close_http_client
        from app.api.gateway_routes import close_discovery_client
        await close_http_client()
        await close_discovery_client()


from app.version import __version__ as APP_VERSION

app = FastAPI(
    title="Genie API with Cache & Queue",
    description="Full-stack application for Databricks Genie API with intelligent caching and queueing",
    version=APP_VERSION,
    lifespan=lifespan
)

if settings.is_production:
    allow_origins = [settings.databricks_host] if settings.databricks_host else ["*"]
else:
    allow_origins = [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")
app.include_router(gateway_router, prefix="/api")
app.include_router(rbac_router, prefix="/api")
app.include_router(genie_clone_router, prefix="/api/2.0/genie")
app.include_router(mcp_router, prefix="/api/2.0/mcp")

try:
    from app.ui import build_wsgi_dashboard

    _dash_flask_app = build_wsgi_dashboard()
    app.mount("/dash", WSGIMiddleware(_dash_flask_app))
    logger.info("Dash UI mounted at /dash/")
    _favicon_path = Path(__file__).resolve().parent / "ui" / "assets" / "genie-icon-alt.svg"

    @app.get("/favicon.ico")
    async def favicon():
        if _favicon_path.is_file():
            return FileResponse(_favicon_path, media_type="image/svg+xml")
        return RedirectResponse("/dash/_favicon.ico", status_code=302)

    @app.get("/")
    async def root_redirect_to_dash():
        return RedirectResponse("/dash/", status_code=302)

    @app.get("/playground")
    async def legacy_playground_root():
        return RedirectResponse("/dash/playground", status_code=302)

    @app.get("/playground/{rest:path}")
    async def legacy_playground(rest: str):  # noqa: ARG001
        return RedirectResponse("/dash/playground", status_code=302)

except Exception as e:
    logger.exception("Dash UI failed to initialize: %s", e)

    @app.get("/")
    async def root_fallback():
        return {
            "message": "Genie API Backend — Dash UI unavailable",
            "docs": "/docs",
            "error": str(e),
        }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=not settings.is_production
    )
