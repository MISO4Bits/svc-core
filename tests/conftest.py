from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
import yaml
from httpx import ASGITransport, AsyncClient

from app.adapters.factory import maybe_init
from app.api.app import create_app
from app.config import Settings

SPEC_PATH = Path(__file__).resolve().parents[1] / "openapi" / "openapi.yaml"

CLIENTE_VALIDO = {
    "identityRef": "idp-sub-0001",
    "tipoDocumento": "CC",
    "numeroDocumento": "1032456789",
    "primerNombre": "Ana",
    "primerApellido": "Ríos",
    "fechaNacimiento": "1991-05-20",
    "email": "ana.rios@example.com",
}


@pytest.fixture(scope="session")
def openapi_spec() -> dict:
    return yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        repository_backend="sqlite",
        database_path=str(tmp_path / "test.db"),
        event_backend="memory",
    )


@pytest_asyncio.fixture
async def app(settings):
    application = create_app(settings)
    await maybe_init(application.state.clientes)
    return application


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
