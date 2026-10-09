"""El registro dispara los correos en segundo plano, sin afectar su respuesta."""

from __future__ import annotations

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.adapters.factory import maybe_init
from app.api.app import create_app
from app.config import Settings
from tests.conftest import CLIENTE_VALIDO


def _settings(tmp_path, **cambios) -> Settings:
    datos = {
        "repository_backend": "sqlite",
        "database_path": str(tmp_path / "correos.db"),
        "event_backend": "memory",
        "notificaciones_backend": "fake",
    }
    return Settings(**{**datos, **cambios})


async def _cliente(settings):
    app = create_app(settings)
    await maybe_init(app.state.clientes)
    return app, AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _tipos(app) -> list[str]:
    return [e.tipo for e in app.state.events.events]


@pytest_asyncio.fixture
async def con_correos(tmp_path):
    app, http = await _cliente(_settings(tmp_path))
    async with http:
        yield app, http


async def test_registrar_publica_los_dos_correos_con_el_enlace_a_la_web(con_correos):
    app, http = con_correos

    resp = await http.post("/clientes", json=CLIENTE_VALIDO)

    assert resp.status_code == 201
    assert _tipos(app).count("EnviarCorreo") == 2
    correos = {
        e.datos["plantilla"]: e.datos for e in app.state.events.events if e.tipo == "EnviarCorreo"
    }
    assert correos["bienvenida"]["destinatario"] == CLIENTE_VALIDO["email"]
    assert correos["bienvenida"]["clienteId"] == resp.json()["id"]
    assert (
        "https://solventa4bits.com/verificar-correo?oobCode="
        in (correos["verificacion-correo"]["cuerpoHtml"])
    )


async def test_el_reintento_idempotente_no_vuelve_a_enviar_correos(con_correos):
    app, http = con_correos
    cabeceras = {"Idempotency-Key": "reintento-1"}

    await http.post("/clientes", json=CLIENTE_VALIDO, headers=cabeceras)
    repetido = await http.post("/clientes", json=CLIENTE_VALIDO, headers=cabeceras)

    assert repetido.status_code == 201
    assert _tipos(app).count("EnviarCorreo") == 2


async def test_un_registro_rechazado_no_envia_correos(con_correos):
    app, http = con_correos
    await http.post("/clientes", json=CLIENTE_VALIDO)

    repetido = await http.post("/clientes", json=CLIENTE_VALIDO)

    assert repetido.status_code == 409
    assert _tipos(app).count("EnviarCorreo") == 2


async def test_con_las_notificaciones_apagadas_solo_se_publica_el_registro(tmp_path):
    app, http = await _cliente(_settings(tmp_path, notificaciones_backend="off"))
    async with http:
        resp = await http.post("/clientes", json=CLIENTE_VALIDO)

    assert resp.status_code == 201
    assert _tipos(app) == ["ClienteRegistrado"]


async def test_un_fallo_de_los_correos_no_cambia_la_respuesta_del_registro(tmp_path):
    app, http = await _cliente(_settings(tmp_path))

    async def _roto(*_a, **_k):
        raise RuntimeError("boom")

    app.state.notificaciones._plantillas.obtener_vigente = _roto
    async with http:
        resp = await http.post("/clientes", json=CLIENTE_VALIDO)

    assert resp.status_code == 201
    assert _tipos(app) == ["ClienteRegistrado"]


async def test_al_apagar_el_servicio_se_cierran_los_adaptadores_de_correo(tmp_path, monkeypatch):
    async def _token() -> str:
        return "token"

    monkeypatch.setattr("app.adapters.identity_admin.token_de_servicio", lambda: _token)
    settings = _settings(
        tmp_path, notificaciones_backend="real", identity_project_id="solventa-dev"
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        assert app.state.notificaciones is not None
