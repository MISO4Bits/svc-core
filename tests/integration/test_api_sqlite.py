"""Pruebas de integración: la API completa contra el adaptador SQLite real."""

from __future__ import annotations

from tests.conftest import CLIENTE_VALIDO


async def test_registro_sin_telefono_devuelve_400(client):
    sin_telefono = {k: v for k, v in CLIENTE_VALIDO.items() if k != "telefono"}
    respuesta = await client.post("/clientes", json=sin_telefono)
    assert respuesta.status_code == 400
    assert any(e["campo"].endswith("telefono") for e in respuesta.json()["errores"])


async def test_registro_y_consulta(client, app):
    creado = await client.post("/clientes", json=CLIENTE_VALIDO)
    assert creado.status_code == 201
    body = creado.json()
    assert body["estado"] == "ACTIVO"
    assert creado.headers["location"] == f"/clientes/{body['id']}"

    obtenido = await client.get(f"/clientes/{body['id']}")
    assert obtenido.status_code == 200
    assert obtenido.json()["email"] == CLIENTE_VALIDO["email"]

    por_identidad = await client.get(
        "/clientes", params={"identityRef": CLIENTE_VALIDO["identityRef"]}
    )
    assert por_identidad.status_code == 200
    assert por_identidad.json()["id"] == body["id"]
    assert (await client.get("/clientes", params={"identityRef": "nope"})).status_code == 404

    eventos = [e.tipo for e in app.state.events.events]
    assert eventos == ["ClienteRegistrado"]


async def test_registro_duplicado_devuelve_409(client):
    await client.post("/clientes", json=CLIENTE_VALIDO)
    repetido = await client.post("/clientes", json=CLIENTE_VALIDO)
    assert repetido.status_code == 409
    assert repetido.json()["status"] == 409
    assert repetido.json()["errores"][0]["campo"] == "correo"


async def test_registro_documento_duplicado_indica_campo_documento(client):
    await client.post("/clientes", json=CLIENTE_VALIDO)
    otro_correo = {**CLIENTE_VALIDO, "email": "otra.persona@example.com"}
    repetido = await client.post("/clientes", json=otro_correo)
    assert repetido.status_code == 409
    assert repetido.json()["errores"][0]["campo"] == "documento"


async def test_registro_correo_desechable_devuelve_422(client):
    payload = {**CLIENTE_VALIDO, "email": "ana@mailinator.com"}
    resp = await client.post("/clientes", json=payload)
    assert resp.status_code == 422


async def test_registro_declina_financiero_no_bloquea(client):
    payload = {**CLIENTE_VALIDO, "autorizaDatosFinancieros": False}
    resp = await client.post("/clientes", json=payload)
    assert resp.status_code == 201
    assert resp.json()["estado"] == "ACTIVO"


async def test_disponibilidad(client):
    await client.post("/clientes", json=CLIENTE_VALIDO)

    ocupado = await client.get(
        "/clientes/disponibilidad", params={"correo": CLIENTE_VALIDO["email"]}
    )
    assert ocupado.status_code == 200
    assert ocupado.json()["correoDisponible"] is False

    libre = await client.get("/clientes/disponibilidad", params={"correo": "libre@example.com"})
    assert libre.json()["correoDisponible"] is True
    assert libre.json()["documentoDisponible"] is None


async def test_confirmacion_es_idempotente(client):
    creado = (await client.post("/clientes", json=CLIENTE_VALIDO)).json()
    assert creado["correoConfirmado"] is False

    confirmado = await client.post(f"/clientes/{creado['id']}/confirmacion")
    assert confirmado.status_code == 200
    assert confirmado.json()["correoConfirmado"] is True
    primera_vez = confirmado.json()["confirmadoEn"]

    otra_vez = await client.post(f"/clientes/{creado['id']}/confirmacion")
    assert otra_vez.json()["confirmadoEn"] == primera_vez


async def test_confirmacion_cliente_inexistente_devuelve_404(client):
    resp = await client.post("/clientes/00000000-0000-0000-0000-000000000000/confirmacion")
    assert resp.status_code == 404


async def test_cliente_inexistente_devuelve_404(client):
    resp = await client.get("/clientes/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


async def test_body_invalido_devuelve_400(client):
    resp = await client.post("/clientes", json={"email": "malo"})
    assert resp.status_code == 400
    assert resp.json()["errores"]


async def test_idempotencia_no_duplica(client, app):
    headers = {"Idempotency-Key": "clave-abc"}
    primero = await client.post("/clientes", json=CLIENTE_VALIDO, headers=headers)
    segundo = await client.post("/clientes", json=CLIENTE_VALIDO, headers=headers)

    assert primero.status_code == segundo.status_code == 201
    assert primero.json()["id"] == segundo.json()["id"]
    assert [e.tipo for e in app.state.events.events] == ["ClienteRegistrado"]


async def test_ciclo_de_consentimiento(client):
    # El registro unificado (BITS-93) ya crea OPEN_DATA y OPEN_FINANCE en
    # version 1 (CLIENTE_VALIDO autoriza ambos), así que otorgar de nuevo
    # parte de ahí, no de version 1.
    cliente_id = (await client.post("/clientes", json=CLIENTE_VALIDO)).json()["id"]
    ruta = f"/clientes/{cliente_id}/consentimientos"

    v1 = await client.post(
        ruta, json={"scope": "OPEN_DATA", "politicaVersion": "v1", "canal": "WEB"}
    )
    assert v1.status_code == 201
    assert v1.json()["version"] == 2

    v2 = await client.post(
        ruta, json={"scope": "OPEN_DATA", "politicaVersion": "v2", "canal": "WEB"}
    )
    assert v2.json()["version"] == 3

    listado = await client.get(ruta)
    assert listado.status_code == 200
    # OPEN_DATA (recién otorgado) + OPEN_FINANCE (del registro) = 2
    assert len(listado.json()) == 2

    detalle = await client.get(f"{ruta}/OPEN_DATA")
    assert detalle.status_code == 200
    assert detalle.json()["politicaVersion"] == "v2"

    estado = await client.get(f"{ruta}/OPEN_DATA/estado")
    assert estado.json()["vigente"] is True

    revocado = await client.delete(f"{ruta}/OPEN_DATA")
    assert revocado.status_code == 204

    estado_final = await client.get(f"{ruta}/OPEN_DATA/estado")
    assert estado_final.json()["estado"] == "REVOCADO"
    assert estado_final.json()["vigente"] is False


async def test_consentimiento_para_cliente_inexistente_devuelve_404(client):
    resp = await client.post(
        "/clientes/no-existe/consentimientos",
        json={"scope": "OPEN_DATA", "politicaVersion": "v1", "canal": "WEB"},
    )
    assert resp.status_code == 404


async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
