from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.errors import install_error_handlers, problema
from app.api.schemas import ClienteOut, ConsentimientoOut, RegistrarClienteRequest
from app.domain import (
    Cliente,
    ClienteNoEncontrado,
    ClienteYaExiste,
    Consentimiento,
    ConsentimientoNoEncontrado,
    DomainError,
    EstadoConsentimiento,
    TipoDocumento,
)

VALIDO = {
    "identityRef": "sub-1",
    "tipoDocumento": "CC",
    "numeroDocumento": "123456",
    "primerNombre": "Ana",
    "primerApellido": "Ríos",
    "fechaNacimiento": "1990-01-01",
    "email": "ana@example.com",
}


def test_registrar_request_acepta_camel_case():
    req = RegistrarClienteRequest.model_validate(VALIDO)
    assert req.identity_ref == "sub-1"
    assert req.tipo_documento is TipoDocumento.CC


@pytest.mark.parametrize(
    "override",
    [
        {"email": "no-es-email"},
        {"numeroDocumento": "ab"},
        {"telefono": "123"},
        {"extra": "campo"},
    ],
)
def test_registrar_request_rechaza_invalidos(override):
    with pytest.raises(ValidationError):
        RegistrarClienteRequest.model_validate({**VALIDO, **override})


def test_cliente_out_serializa_en_camel_case():
    dominio = Cliente(
        identity_ref="sub-1",
        tipo_documento=TipoDocumento.CC,
        numero_documento="123456",
        primer_nombre="Ana",
        primer_apellido="Ríos",
        fecha_nacimiento=date(1990, 1, 1),
        email="ana@example.com",
        creado_en=datetime(2026, 1, 1, tzinfo=UTC),
    )
    data = ClienteOut.model_validate(dominio).model_dump(mode="json", by_alias=True)
    assert data["identityRef"] == "sub-1"
    assert data["numeroDocumento"] == "123456"
    assert data["creadoEn"].startswith("2026-01-01")


def test_consentimiento_out_desde_dominio():
    consentimiento = Consentimiento(
        cliente_id="c1",
        scope="OPEN_FINANCE",
        estado=EstadoConsentimiento.OTORGADO,
        version=3,
    )
    out = ConsentimientoOut.model_validate(consentimiento).model_dump(by_alias=True)
    assert out["version"] == 3
    assert "clienteId" not in out  # el id de cliente no viaja en la respuesta


def test_problema_construye_media_type_correcto():
    resp = problema(422, "X", detail="d", errores=[{"campo": "a", "mensaje": "m"}])
    assert resp.status_code == 422
    assert resp.media_type == "application/problem+json"


def _app_con_errores() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/ya-existe")
    async def _ya_existe():
        raise ClienteYaExiste("CC", "1")

    @app.get("/cliente-404")
    async def _cliente_404():
        raise ClienteNoEncontrado("c1")

    @app.get("/cons-404")
    async def _cons_404():
        raise ConsentimientoNoEncontrado("c1", "OPEN_DATA")

    @app.get("/dominio")
    async def _dominio():
        raise DomainError("regla")

    return app


@pytest.mark.parametrize(
    "ruta,esperado",
    [
        ("/ya-existe", 409),
        ("/cliente-404", 404),
        ("/cons-404", 404),
        ("/dominio", 422),
    ],
)
def test_handlers_mapean_status(ruta, esperado):
    client = TestClient(_app_con_errores())
    resp = client.get(ruta)
    assert resp.status_code == esperado
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["status"] == esperado
