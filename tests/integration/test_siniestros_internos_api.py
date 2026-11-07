"""BITS-377: bandeja, archivos y decisiones del ADMIN, de punta a punta."""

from __future__ import annotations

import asyncio

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from jsonschema import Draft202012Validator

from app.adapters.factory import maybe_init
from app.api.app import create_app
from app.config import Settings
from tests.integration.test_dispositivos_api import _registrar_dispositivo
from tests.integration.test_siniestros_api import (
    FOTO,
    TRANSFERENCIA,
    _datos,
    _incendio,
    _poliza_emitida,
    _reportar,
)
from tests.integration.test_siniestros_estados_api import _entregar, _ruta

AGENTE = "admin@solventa4bits.com"


def _validar(spec: dict, ref: str, instancia) -> None:
    schema = {"$ref": f"#/components/schemas/{ref}", "components": spec["components"]}
    errores = sorted(Draft202012Validator(schema).iter_errors(instancia), key=str)
    assert not errores, [e.message for e in errores]


@pytest_asyncio.fixture
async def con_push(tmp_path):
    """Como `client`, pero con los avisos push encendidos para ver qué se publica."""
    settings = Settings(
        repository_backend="sqlite",
        database_path=str(tmp_path / "bandeja.db"),
        event_backend="memory",
        almacen_backend="memory",
        push_backend="on",
    )
    app = create_app(settings)
    await maybe_init(app.state.clientes)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield app, http


async def _radicar(app, http, *, incendio: bool = True):
    """(clienteId, polizaId, siniestroId) de un siniestro recién radicado."""
    cliente_id, poliza_id = await _poliza_emitida(app, http)
    datos = _incendio(poliza_id) if incendio else _datos(poliza_id)
    respuesta = await _reportar(http, cliente_id, datos)
    assert respuesta.status_code == 201
    return cliente_id, poliza_id, respuesta.json()["id"]


async def _decidir(http, siniestro_id: str, **cuerpo):
    return await http.post(
        f"/v1/siniestros/{siniestro_id}/decisiones", json={"agente": AGENTE, **cuerpo}
    )


def _pushes(app) -> list:
    return [e for e in app.state.events.events if e.tipo == "EnviarPush"]


# --- bandeja ---


async def test_la_bandeja_lista_los_siniestros_de_todos_los_clientes_por_plazo(
    app, client, openapi_spec
):
    ana, _, de_ana = await _radicar(app, client)
    _, poliza_de_luis = await _poliza_emitida(
        app,
        client,
        cliente={
            "identityRef": "idp-sub-0002",
            "numeroDocumento": "1032456790",
            "email": "luis@example.com",
        },
    )
    luis = (await client.get("/v1/clientes", params={"identityRef": "idp-sub-0002"})).json()["id"]
    de_luis = (await _reportar(client, luis, _datos(poliza_de_luis))).json()["id"]

    respuesta = await client.get("/v1/siniestros", params={"estado": "EN_ESTUDIO"})

    assert respuesta.status_code == 200
    bandeja = respuesta.json()
    assert [s["id"] for s in bandeja] == [de_ana, de_luis]  # vence antes el que se reportó antes
    for fila in bandeja:
        _validar(openapi_spec, "SiniestroInternoResumen", fila)
        assert fila["fechaLimite"] is not None
    assert {s["clienteId"] for s in bandeja} == {ana, luis}


async def test_la_bandeja_filtra_por_estado_y_automatico_y_rechaza_valores_invalidos(app, client):
    _, _, siniestro_id = await _radicar(app, client)
    await _decidir(client, siniestro_id, decision="RECHAZAR", motivo="No está cubierto")

    en_estudio = await client.get("/v1/siniestros", params={"estado": "EN_ESTUDIO"})
    rechazados = await client.get("/v1/siniestros", params={"estado": "RECHAZADO"})
    automaticos = await client.get("/v1/siniestros", params={"automatico": "true"})
    todos = await client.get("/v1/siniestros")
    invalido = await client.get("/v1/siniestros", params={"estado": "INVENTADO"})

    assert en_estudio.json() == []
    assert [s["id"] for s in rechazados.json()] == [siniestro_id]
    assert rechazados.json()[0]["fechaLimite"] is None
    assert automaticos.json() == []
    assert len(todos.json()) == 1
    assert invalido.status_code == 400


# --- detalle y archivos ---


async def test_el_detalle_del_admin_trae_cliente_poliza_saldo_y_como_recibe_el_pago(
    app, client, openapi_spec
):
    cliente_id, poliza_id, siniestro_id = await _radicar(app, client)

    respuesta = await client.get(f"/v1/siniestros/{siniestro_id}")

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    _validar(openapi_spec, "SiniestroInterno", cuerpo)
    assert cuerpo["clienteId"] == cliente_id
    assert cuerpo["poliza"]["id"] == poliza_id
    assert cuerpo["poliza"]["cobertura"] == "HOGAR"
    assert cuerpo["poliza"]["sumaAsegurada"] == 150000000
    assert cuerpo["saldoCredito"]["valor"] == 140000000
    assert cuerpo["saldoCredito"]["origen"] == "POLIZA"
    assert cuerpo["saldoCredito"]["entidad"] == "Bancolombia"
    assert cuerpo["destinoPago"] == TRANSFERENCIA
    assert cuerpo["decisiones"] == []
    assert cuerpo["fechaLimite"] is not None
    assert "referencia" not in respuesta.text


async def test_el_detalle_de_un_siniestro_que_no_existe_es_404(client):
    assert (await client.get("/v1/siniestros/no-existe")).status_code == 404


async def test_el_archivo_se_abre_en_la_bandeja_sin_descargarlo(app, client):
    _, _, siniestro_id = await _radicar(app, client)
    archivo_id = (await client.get(f"/v1/siniestros/{siniestro_id}")).json()["archivos"][0]["id"]

    respuesta = await client.get(f"/v1/siniestros/{siniestro_id}/evidencias/{archivo_id}")

    assert respuesta.status_code == 200
    assert respuesta.content == FOTO
    assert respuesta.headers["content-type"] == "image/jpeg"
    assert respuesta.headers["cache-control"] == "no-store"
    assert respuesta.headers["content-disposition"] == "inline"


async def test_un_archivo_o_un_siniestro_inexistente_es_404(app, client):
    _, _, siniestro_id = await _radicar(app, client)

    assert (
        await client.get(f"/v1/siniestros/{siniestro_id}/evidencias/no-existe")
    ).status_code == 404
    assert (await client.get("/v1/siniestros/no-existe/evidencias/a-1")).status_code == 404


# --- decisiones ---


async def test_pagar_incapacidad_total_deja_una_linea_al_banco_y_avisa(con_push, openapi_spec):
    app, http = con_push
    cliente_id, _, siniestro_id = await _radicar(app, http, incendio=False)
    await _registrar_dispositivo(http, cliente_id, "fcm-token-telefono-1")

    respuesta = await _decidir(http, siniestro_id, decision="PAGAR")

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    _validar(openapi_spec, "SiniestroInterno", cuerpo)
    assert cuerpo["estado"] == "PAGADO"
    assert cuerpo["fechaLimite"] is None
    [linea] = cuerpo["pago"]["lineas"]
    assert linea == {
        "beneficiario": "BANCO",
        "valor": 140000000,
        "destino": "Bancolombia, abono al saldo del crédito",
    }
    [aviso] = _pushes(app)
    assert aviso.datos["titulo"] == "Pagamos tu siniestro"
    assert aviso.datos["ruta"]["destino"] == "seguro"
    assert "140" not in aviso.datos["cuerpo"]  # sin montos en la pantalla bloqueada


async def test_perdida_total_paga_al_banco_y_al_cliente_y_deja_la_constancia(app, client):
    _, _, siniestro_id = await _radicar(app, client)

    respuesta = await _decidir(
        client, siniestro_id, decision="PAGAR", dano="PERDIDA_TOTAL", valor=150000000
    )

    cuerpo = respuesta.json()
    assert [(x["beneficiario"], x["valor"]) for x in cuerpo["pago"]["lineas"]] == [
        ("BANCO", 140000000),
        ("CLIENTE", 10000000),
    ]
    assert cuerpo["pago"]["lineas"][1]["destino"] == "Cuenta de ahorros terminada en 2345"
    [decision] = cuerpo["decisiones"]
    assert (decision["agente"], decision["decision"], decision["dano"]) == (
        AGENTE,
        "PAGAR",
        "PERDIDA_TOTAL",
    )
    assert decision["valor"] == 150000000
    assert TRANSFERENCIA["numeroCuenta"] not in str(cuerpo["pago"])


async def test_pedir_evidencia_saca_de_la_bandeja_y_el_cliente_ve_lo_pedido(con_push):
    app, http = con_push
    cliente_id, _, siniestro_id = await _radicar(app, http)
    await _registrar_dispositivo(http, cliente_id, "fcm-token-telefono-1")

    respuesta = await _decidir(
        http, siniestro_id, decision="PEDIR_EVIDENCIA", detalle="Falta el recibo de la reparación"
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["estado"] == "FALTA_INFORMACION"
    assert respuesta.json()["fechaLimite"] is None
    assert (await http.get("/v1/siniestros", params={"estado": "EN_ESTUDIO"})).json() == []
    visto_por_el_cliente = (await http.get(_ruta(cliente_id, siniestro_id))).json()
    assert visto_por_el_cliente["estado"] == "FALTA_INFORMACION"
    assert visto_por_el_cliente["evidenciaPedida"]["detalle"] == "Falta el recibo de la reparación"
    assert _pushes(app)[0].datos["titulo"] == "Necesitamos algo más para tu siniestro"


async def test_el_ciclo_completo_pedir_entregar_y_volver_a_la_bandeja(app, client):
    cliente_id, _, siniestro_id = await _radicar(app, client)
    await _decidir(client, siniestro_id, decision="PEDIR_EVIDENCIA", detalle="Falta el recibo")

    entrega = await _entregar(client, cliente_id, siniestro_id)

    assert entrega.status_code == 201
    bandeja = (await client.get("/v1/siniestros", params={"estado": "EN_ESTUDIO"})).json()
    assert [s["id"] for s in bandeja] == [siniestro_id]
    detalle = (await client.get(f"/v1/siniestros/{siniestro_id}")).json()
    assert detalle["evidenciaPedida"]["detalle"] == "Falta el recibo"  # el ADMIN sigue viéndola
    primera, segunda = detalle["archivos"]
    assert segunda["entregadoEn"] > primera["entregadoEn"]
    # Y ahora sí se puede pagar.
    pagado = await _decidir(
        client, siniestro_id, decision="PAGAR", dano="REPARABLE", valor=40000000
    )
    assert pagado.json()["estado"] == "PAGADO"
    assert [d["decision"] for d in pagado.json()["decisiones"]] == ["PEDIR_EVIDENCIA", "PAGAR"]


async def test_rechazar_guarda_el_motivo_que_ve_el_cliente_y_avisa(con_push):
    app, http = con_push
    cliente_id, _, siniestro_id = await _radicar(app, http)
    await _registrar_dispositivo(http, cliente_id, "fcm-token-telefono-1")

    respuesta = await _decidir(
        http, siniestro_id, decision="RECHAZAR", motivo="El evento no está cubierto"
    )

    assert respuesta.json()["estado"] == "RECHAZADO"
    assert respuesta.json()["motivoRechazo"] == "El evento no está cubierto"
    visto = (await http.get(_ruta(cliente_id, siniestro_id))).json()
    assert visto["motivoRechazo"] == "El evento no está cubierto"
    assert _pushes(app)[0].datos["titulo"] == "Tenemos una respuesta sobre tu siniestro"


async def test_pagar_dos_veces_deja_una_sola_orden(con_push):
    app, http = con_push
    cliente_id, _, siniestro_id = await _radicar(app, http)
    await _registrar_dispositivo(http, cliente_id, "fcm-token-telefono-1")
    primero = await _decidir(http, siniestro_id, decision="PAGAR", dano="REPARABLE", valor=40000000)

    segundo = await _decidir(
        http, siniestro_id, decision="PAGAR", dano="PERDIDA_TOTAL", valor=150000000
    )

    assert (primero.status_code, segundo.status_code) == (200, 200)
    assert segundo.json()["pago"] == primero.json()["pago"]
    assert len(segundo.json()["decisiones"]) == 1
    assert len(_pushes(app)) == 1


async def test_dos_pagos_simultaneos_dejan_una_sola_orden(con_push):
    app, http = con_push
    cliente_id, _, siniestro_id = await _radicar(app, http)
    await _registrar_dispositivo(http, cliente_id, "fcm-token-telefono-1")

    respuestas = await asyncio.gather(
        _decidir(http, siniestro_id, decision="PAGAR", dano="REPARABLE", valor=40000000),
        _decidir(http, siniestro_id, decision="PAGAR", dano="PERDIDA_TOTAL", valor=150000000),
    )

    assert [r.status_code for r in respuestas] == [200, 200]
    assert respuestas[0].json()["pago"] == respuestas[1].json()["pago"]
    final = (await http.get(f"/v1/siniestros/{siniestro_id}")).json()
    assert final["estado"] == "PAGADO"
    assert len(final["decisiones"]) == 1
    assert len(final["pago"]["lineas"]) in (1, 2)
    assert len(_pushes(app)) == 1


async def test_pagar_y_rechazar_a_la_vez_dejan_una_sola_decision(app, client):
    _, _, siniestro_id = await _radicar(app, client)

    respuestas = await asyncio.gather(
        _decidir(client, siniestro_id, decision="PAGAR", dano="REPARABLE", valor=1000),
        _decidir(client, siniestro_id, decision="RECHAZAR", motivo="No cubierto"),
    )

    assert [r.status_code for r in respuestas] == [200, 200]
    assert respuestas[0].json()["estado"] == respuestas[1].json()["estado"]
    final = (await client.get(f"/v1/siniestros/{siniestro_id}")).json()
    assert len(final["decisiones"]) == 1


async def test_el_valor_que_supera_la_suma_asegurada_es_422(app, client, openapi_spec):
    _, _, siniestro_id = await _radicar(app, client)

    respuesta = await _decidir(
        client, siniestro_id, decision="PAGAR", dano="REPARABLE", valor=150000001
    )

    assert respuesta.status_code == 422
    assert respuesta.json()["codigo"] == "VALOR_SUPERA_SUMA_ASEGURADA"
    assert respuesta.headers["content-type"].startswith("application/problem+json")
    _validar(openapi_spec, "ProblemaSiniestro", respuesta.json())
    assert (await client.get(f"/v1/siniestros/{siniestro_id}")).json()["estado"] == "EN_ESTUDIO"


async def test_decidir_un_siniestro_en_falta_informacion_es_409(app, client, openapi_spec):
    _, _, siniestro_id = await _radicar(app, client)
    await _decidir(client, siniestro_id, decision="PEDIR_EVIDENCIA", detalle="falta algo")

    respuesta = await _decidir(client, siniestro_id, decision="RECHAZAR", motivo="No cubierto")

    assert respuesta.status_code == 409
    assert respuesta.json()["codigo"] == "ESTADO_NO_PERMITE"
    _validar(openapi_spec, "ProblemaSiniestro", respuesta.json())


async def test_decidir_un_siniestro_que_no_existe_es_404(client):
    assert (await _decidir(client, "no-existe", decision="PAGAR")).status_code == 404


async def test_cuerpos_invalidos_son_400(app, client):
    _, _, siniestro_id = await _radicar(app, client)
    ruta = f"/v1/siniestros/{siniestro_id}/decisiones"

    for cuerpo in (
        {"decision": "PAGAR"},  # sin agente
        {"agente": AGENTE},  # sin decisión
        {"agente": AGENTE, "decision": "REGALAR"},
        {"agente": "ab", "decision": "PAGAR", "dano": "REPARABLE", "valor": 1},
        {"agente": AGENTE, "decision": "PAGAR", "dano": "ENORME", "valor": 1},
        {"agente": AGENTE, "decision": "PAGAR", "dano": "REPARABLE", "valor": 0},
        {"agente": AGENTE, "decision": "PAGAR", "dano": "REPARABLE", "valor": -3},
        {"agente": AGENTE, "decision": "PAGAR", "dano": "REPARABLE"},  # sin valor
        {"agente": AGENTE, "decision": "PAGAR", "valor": 1000},  # sin daño
        {"agente": AGENTE, "decision": "PEDIR_EVIDENCIA"},  # sin detalle
        {"agente": AGENTE, "decision": "RECHAZAR"},  # sin motivo
        {"agente": AGENTE, "decision": "RECHAZAR", "motivo": "x", "valor": 5},  # valor sobrante
        {"agente": AGENTE, "decision": "RECHAZAR", "motivo": "x", "nota": "interna"},  # sin notas
    ):
        respuesta = await client.post(ruta, json=cuerpo)
        assert respuesta.status_code == 400, cuerpo

    assert (await client.get(f"/v1/siniestros/{siniestro_id}")).json()["estado"] == "EN_ESTUDIO"


async def test_incapacidad_total_con_valor_es_400(app, client):
    _, _, siniestro_id = await _radicar(app, client, incendio=False)

    respuesta = await _decidir(client, siniestro_id, decision="PAGAR", valor=1000)

    assert respuesta.status_code == 400
    assert respuesta.json()["errores"][0]["campo"] == "valor"


async def test_el_log_no_incluye_al_agente_ni_el_numero_de_cuenta(app, client, caplog):
    import logging

    _, _, siniestro_id = await _radicar(app, client)

    with caplog.at_level(logging.INFO):
        await _decidir(client, siniestro_id, decision="PAGAR", dano="REPARABLE", valor=1000)

    assert AGENTE not in caplog.text
    assert TRANSFERENCIA["numeroCuenta"] not in caplog.text
