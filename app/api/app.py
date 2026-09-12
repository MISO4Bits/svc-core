from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.adapters.factory import build_event_publisher, build_repositories, maybe_init
from app.api.errors import install_error_handlers
from app.api.routes import router
from app.config import Settings, get_settings
from app.services import IdentityService

SPEC_PATH = Path(__file__).resolve().parents[2] / "openapi" / "openapi.yaml"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=logging.INFO)

    clientes, consentimientos, idempotency = build_repositories(settings)
    events = build_event_publisher(settings)
    service = IdentityService(clientes, consentimientos, events)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        for adapter in (clientes, consentimientos, idempotency):
            await maybe_init(adapter)
        yield

    app = FastAPI(
        title="ICustomerIdentity — CoreTransaccional",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.service = service
    app.state.clientes = clientes
    app.state.consentimientos = consentimientos
    app.state.idempotency = idempotency
    app.state.events = events

    install_error_handlers(app)
    app.include_router(router)

    @app.get("/health", include_in_schema=False)
    async def health() -> dict:
        return {"status": "ok", "service": settings.service_name}

    if SPEC_PATH.exists():

        @app.get("/openapi.yaml", include_in_schema=False)
        async def openapi_yaml() -> FileResponse:
            return FileResponse(SPEC_PATH, media_type="application/yaml")

    return app
