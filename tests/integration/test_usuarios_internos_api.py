"""BITS-374: ``POST /v1/usuarios-internos``."""

from __future__ import annotations

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from jsonschema import Draft202012Validator

from app.adapters.factory import maybe_init
from app.api.app import create_app
from app.config import Settings

RUTA = "/v1/usuarios-internos"
CLAVE = "clave-del-socio-1"


def _validar(spec: dict, ref: str, instancia) -> None:
    schema = {"$ref": f"#/components/schemas/{ref}", "components": spec["components"]}
    errores = sorted(Draft202012Validator(schema).iter_errors(instancia), key=str)
    assert not errores, [e.message for e in errores]


def _cuerpo(**cambios) -> dict:
    return {
        "correo": "socio@solventa4bits.com",
        "contrasena": CLAVE,
        "rol": "SOCIO",
        "nombre": "Aliado",
        **cambios,
    }


@pytest_asyncio.fixture
async def http(tmp_path):
    settings = Settings(
        repository_backend="sqlite",
        database_path=str(tmp_path / "test.db"),
        event_backend="memory",
        almacen_backend="memory",
        cuentas_internas_backend="fake",
    )
    aplicacion = create_app(settings)
    await maybe_init(aplicacion.state.clientes)
    async with AsyncClient(transport=ASGITransport(app=aplicacion), base_url="http://t") as cliente:
        yield cliente


async def test_crea_un_socio_y_cumple_el_contrato(http, openapi_spec):
    respuesta = await http.post(RUTA, json=_cuerpo())

    assert respuesta.status_code == 201
    _validar(openapi_spec, "UsuarioInterno", respuesta.json())
    cuerpo = respuesta.json()
    assert cuerpo["rol"] == "SOCIO"
    assert cuerpo["correo"] == "socio@solventa4bits.com"
    assert cuerpo["uid"]


async def test_crea_un_admin_sin_nombre(http):
    respuesta = await http.post(
        RUTA, json=_cuerpo(rol="ADMIN", nombre=None, correo="a@solventa4bits.com")
    )

    assert respuesta.status_code == 201
    assert respuesta.json()["rol"] == "ADMIN"


async def test_la_respuesta_no_trae_la_contrasena(http):
    respuesta = await http.post(RUTA, json=_cuerpo())

    assert CLAVE not in respuesta.text
    assert "contrasena" not in respuesta.json()


async def test_correo_repetido_es_409(http, openapi_spec):
    await http.post(RUTA, json=_cuerpo())

    respuesta = await http.post(RUTA, json=_cuerpo(correo="SOCIO@solventa4bits.com"))

    assert respuesta.status_code == 409
    assert respuesta.headers["content-type"].startswith("application/problem+json")
    _validar(openapi_spec, "Problema", respuesta.json())
    assert respuesta.json()["errores"][0]["campo"] == "correo"


async def test_correo_de_otro_dominio_es_400(http, openapi_spec):
    respuesta = await http.post(RUTA, json=_cuerpo(correo="ana@gmail.com"))

    assert respuesta.status_code == 400
    _validar(openapi_spec, "Problema", respuesta.json())
    assert respuesta.json()["errores"][0]["campo"] == "correo"


async def test_socio_sin_nombre_es_400(http):
    respuesta = await http.post(RUTA, json=_cuerpo(nombre=None))

    assert respuesta.status_code == 400
    assert respuesta.json()["errores"][0]["campo"] == "nombre"


async def test_rol_desconocido_es_400(http):
    respuesta = await http.post(RUTA, json=_cuerpo(rol="BACKOFFICE"))

    assert respuesta.status_code == 400


async def test_contrasena_corta_es_400_y_no_se_devuelve_en_el_error(http):
    respuesta = await http.post(RUTA, json=_cuerpo(contrasena="corta"))

    assert respuesta.status_code == 400
    assert "corta" not in respuesta.text


async def test_campos_extra_son_400(http):
    respuesta = await http.post(RUTA, json=_cuerpo(esAdmin=True))

    assert respuesta.status_code == 400


async def test_sin_proveedor_de_identidad_es_503(client, openapi_spec):
    # El fixture `client` usa el backend por defecto, "off".
    respuesta = await client.post(RUTA, json=_cuerpo())

    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "IDENTIDAD_NO_DISPONIBLE"
    _validar(openapi_spec, "ProblemaPoliza", respuesta.json())
