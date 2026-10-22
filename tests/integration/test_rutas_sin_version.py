"""Alias temporal sin versión (BITS-275): se borra este archivo al retirar el alias."""

from __future__ import annotations

from tests.conftest import CLIENTE_VALIDO


async def test_ruta_sin_version_sigue_respondiendo_y_apunta_a_v1(client):
    creado = await client.post("/clientes", json=CLIENTE_VALIDO)

    assert creado.status_code == 201
    cliente_id = creado.json()["id"]
    assert creado.headers["location"] == f"/v1/clientes/{cliente_id}"

    por_alias = await client.get(f"/clientes/{cliente_id}")
    por_v1 = await client.get(f"/v1/clientes/{cliente_id}")
    assert por_alias.status_code == por_v1.status_code == 200
    assert por_alias.json() == por_v1.json()


def test_el_alias_no_aparece_en_el_esquema(app):
    rutas = app.openapi()["paths"]

    assert not [ruta for ruta in rutas if ruta.startswith("/clientes")]
    assert [ruta for ruta in rutas if ruta.startswith("/v1/clientes")]


async def test_salud_y_contrato_no_llevan_version(client):
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/v1/health")).status_code == 404
