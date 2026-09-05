"""Pruebas de contrato: la implementación cumple ``openapi/openapi.yaml``.

Se valida (a) que toda operación del contrato está enrutada y (b) que las
respuestas reales validan contra el JSON Schema declarado para ese status.
"""

from __future__ import annotations

import re

import pytest
from jsonschema import Draft202012Validator

from tests.conftest import CLIENTE_VALIDO

_PATH_PARAM = re.compile(r"\{[^}]+\}")


def _spec_operations(spec: dict) -> set[tuple[str, str]]:
    ops: set[tuple[str, str]] = set()
    for path, item in spec["paths"].items():
        for method in item:
            if method.lower() in {"get", "post", "put", "patch", "delete"}:
                ops.add((method.upper(), path))
    return ops


def _app_operations(app) -> set[tuple[str, str]]:
    """Operaciones que FastAPI genera desde el código (su propio OpenAPI)."""
    ops: set[tuple[str, str]] = set()
    for path, item in app.openapi()["paths"].items():
        if not path.startswith("/clientes"):
            continue
        for method in item:
            if method.lower() in {"get", "post", "put", "patch", "delete"}:
                ops.add((method.upper(), path))
    return ops


def _normalize(op: tuple[str, str]) -> tuple[str, str]:
    method, path = op
    return method, _PATH_PARAM.sub("{}", path)


def test_todas_las_operaciones_del_contrato_estan_enrutadas(app, openapi_spec):
    spec_ops = {_normalize(o) for o in _spec_operations(openapi_spec)}
    app_ops = {_normalize(o) for o in _app_operations(app)}
    faltantes = spec_ops - app_ops
    assert not faltantes, f"Operaciones del contrato sin implementar: {faltantes}"


def test_no_hay_rutas_de_negocio_fuera_del_contrato(app, openapi_spec):
    spec_ops = {_normalize(o) for o in _spec_operations(openapi_spec)}
    app_ops = {_normalize(o) for o in _app_operations(app)}
    extra = app_ops - spec_ops
    assert not extra, f"Rutas no declaradas en el contrato: {extra}"


def _validator(spec: dict, ref: str) -> Draft202012Validator:
    schema = {"$ref": f"#/components/schemas/{ref}", "components": spec["components"]}
    return Draft202012Validator(schema)


def _assert_valid(spec: dict, ref: str, instance) -> None:
    errores = sorted(_validator(spec, ref).iter_errors(instance), key=str)
    assert not errores, f"{ref}: {[e.message for e in errores]}"


async def test_respuestas_cumplen_el_esquema(client, openapi_spec):
    creado = await client.post("/clientes", json=CLIENTE_VALIDO)
    assert creado.status_code == 201
    _assert_valid(openapi_spec, "Cliente", creado.json())
    cliente_id = creado.json()["id"]

    obtenido = await client.get(f"/clientes/{cliente_id}")
    assert obtenido.status_code == 200
    _assert_valid(openapi_spec, "Cliente", obtenido.json())

    otorgado = await client.post(
        f"/clientes/{cliente_id}/consentimientos",
        json={"scope": "OPEN_FINANCE", "politicaVersion": "v1", "canal": "WEB"},
    )
    assert otorgado.status_code == 201
    _assert_valid(openapi_spec, "Consentimiento", otorgado.json())

    estado = await client.get(f"/clientes/{cliente_id}/consentimientos/OPEN_FINANCE/estado")
    assert estado.status_code == 200
    _assert_valid(openapi_spec, "EstadoConsentimiento", estado.json())

    lista = await client.get(f"/clientes/{cliente_id}/consentimientos")
    assert lista.status_code == 200
    for item in lista.json():
        _assert_valid(openapi_spec, "Consentimiento", item)


async def test_errores_cumplen_problem_details(client, openapi_spec):
    no_existe = await client.get("/clientes/desconocido")
    assert no_existe.status_code == 404
    assert no_existe.headers["content-type"].startswith("application/problem+json")
    _assert_valid(openapi_spec, "Problema", no_existe.json())

    malo = await client.post("/clientes", json={"email": "x"})
    assert malo.status_code == 400
    _assert_valid(openapi_spec, "Problema", malo.json())


@pytest.mark.parametrize("ruta", ["/openapi.yaml"])
async def test_expone_el_contrato(client, ruta):
    resp = await client.get(ruta)
    assert resp.status_code == 200
    assert "openapi" in resp.text
