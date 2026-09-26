"""Unified FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from knowledge.api.import_file_router import register_import_router
from knowledge.api.lifecycle_router import register_lifecycle_router
from knowledge.api.query_router import register_query_router
from knowledge.api.system_router import register_system_router
from knowledge.core.app_config import AppConfig, get_app_config
from knowledge.core.paths import get_front_page_dir


def create_app(config: AppConfig | None = None) -> FastAPI:
    settings = config or get_app_config()
    app = FastAPI(
        title=settings.name,
        description=settings.description,
        version=settings.version,
    )
    if config is not None:
        app.dependency_overrides[get_app_config] = lambda: settings

    allow_credentials = settings.cors_allow_credentials and "*" not in settings.cors_origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_system_router(app)
    register_lifecycle_router(app)
    register_import_router(app)
    register_query_router(app)

    @app.get("/", include_in_schema=False)
    async def home() -> RedirectResponse:
        return RedirectResponse(url="/chat.html", status_code=307)

    front_page_dir = get_front_page_dir()
    if front_page_dir.exists():
        app.mount(
            "/",
            StaticFiles(directory=front_page_dir, html=True),
            name="front",
        )

    return app
