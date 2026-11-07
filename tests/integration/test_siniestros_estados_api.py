"""BITS-383 y BITS-384: lista, detalle y envío de lo que falta, de punta a punta."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from jsonschema import Draft202012Validator

from app.domain import EstadoSiniestro
from tests.conftest import CLIENTE_VALIDO
from tests.integration.test_siniestros_api import (
    FOTO,
    TRANSFERENCIA,
    _datos,
    _incendio,
    _poliza_emitida,
    _reportar,
)


def _validar(spec: dict, ref: str, instancia) -> None:
    schema = {"$ref": f"#/components/schemas/{ref}", "components": spec["components"]}
    errores = sorted(Draft202012Validator(schema).iter_errors(instancia), key=str)
    assert not errores, [e.message for e in errores]


async def _radicar(app, client, *, incendio: bool = False) -> tuple[str, str, str]:
    """(clienteId, polizaId, siniestroId) de un siniestro recién radicado."""
    cliente_id, poliza_id = await _poliza_emitida(app, client)
    datos = _incendio(poliza_id) if incendio else _datos(poliza_id)
    respuesta = await _reportar(client, cliente_id, datos)
    assert respuesta.status_code == 201
    return cliente_id, poliza_id, respuesta.json()["id"]


async def _pedir_evidencia(app, siniestro_id: str, detalle: str = "Falta el recibo") -> None:
    """Lo que hará la bandeja del ADMIN (BITS-377), llamado directo sobre el dominio."""
    repositorio = app.state.siniestros._siniestros
    siniestro = await repositorio.obtener(siniestro_id)
    siniestro.pedir_evidencia(detalle, datetime.now(UTC), "admin@solventa4bits.com")
    assert await repositorio.actualizar(siniestro, EstadoSiniestro.EN_ESTUDIO)


def _ruta(cliente_id: str, siniestro_id: str | None = None, *resto: str) -> str:
    base = f"/v1/clientes/{cliente_id}/siniestros"
    return "/".join([base, siniestro_id, *resto]) if siniestro_id else base


async def _entregar(client, cliente_id, siniestro_id, *, archivos=1, llave=None, contenido=FOTO):
    datos = {"archivos": [{"clase": "DOCUMENTO"}] * archivos}
    return await client.post(
        _ruta(cliente_id, siniestro_id, "evidencias"),
        data={"datos": json.dumps(datos)},
        files=[("archivos", (f"a{i}.jpg", contenido, "image/jpeg")) for i in range(archivos)],
        headers={"Idempotency-Key": llave} if llave else {},
    )


# --- lista ---


async def test_la_lista_esta_vacia_si_no_hay_siniestros(app, client, openapi_spec):
    cliente_id, _ = await _poliza_emitida(app, client)

    respuesta = await client.get(_ruta(cliente_id))

    assert respuesta.status_code == 200
    assert respuesta.json() == []


async def test_la_lista_trae_los_del_cliente_del_mas_reciente_al_mas_antiguo(
    app, client, openapi_spec
):
    cliente_id, poliza_id = await _poliza_emitida(app, client)
    primero = (await _reportar(client, cliente_id, _datos(poliza_id))).json()
    segundo = (await _reportar(client, cliente_id, _incendio(poliza_id))).json()

    respuesta = await client.get(_ruta(cliente_id))

    assert respuesta.status_code == 200
    lista = respuesta.json()
    assert [s["id"] for s in lista] == [segundo["id"], primero["id"]]
    for item in lista:
        _validar(openapi_spec, "SiniestroResumen", item)
    assert lista[0]["tipo"] == "INCENDIO_TERREMOTO"
    assert lista[0]["automatico"] is False
    assert lista[0]["polizaId"] == poliza_id


async def test_la_lista_no_trae_los_de_otro_cliente(app, client):
    ana, _, _ = await _radicar(app, client)
    luis, poliza_de_luis = await _poliza_emitida(
        app,
        client,
        cliente={
            "identityRef": "idp-sub-0002",
            "numeroDocumento": "1032456790",
            "email": "luis@example.com",
        },
    )

    assert await client.get(_ruta(luis)) is not None
    assert (await client.get(_ruta(luis))).json() == []
    assert len((await client.get(_ruta(ana))).json()) == 1


async def test_la_lista_de_un_cliente_inexistente_es_404(client):
    assert (await client.get(_ruta("no-existe"))).status_code == 404


# --- detalle ---


async def test_el_detalle_cumple_el_contrato_y_trae_el_plazo(app, client, openapi_spec):
    cliente_id, poliza_id, siniestro_id = await _radicar(app, client, incendio=True)

    respuesta = await client.get(_ruta(cliente_id, siniestro_id))

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    _validar(openapi_spec, "Siniestro", cuerpo)
    assert cuerpo["estado"] == "EN_ESTUDIO"
    assert cuerpo["fechaLimite"] is not None
    assert cuerpo["evidenciaPedida"] is None
    assert cuerpo["motivoRechazo"] is None
    assert cuerpo["pago"] is None
    assert cuerpo["evento"] is None
    assert cuerpo["narracion"] == "Se incendió la cocina"
    [archivo] = cuerpo["archivos"]
    assert archivo["entregadoEn"] == cuerpo["ultimaEntregaEn"]
    assert (archivo["latitud"], archivo["longitud"]) == (4.65, -74.06)


async def test_el_detalle_no_devuelve_el_saldo_ni_el_destino_del_pago(app, client):
    cliente_id, _, siniestro_id = await _radicar(app, client, incendio=True)

    texto = (await client.get(_ruta(cliente_id, siniestro_id))).text

    assert TRANSFERENCIA["numeroCuenta"] not in texto
    for prohibido in ("saldoCredito", "destinoPago", "numeroCuenta", "referencia", "siniestros/"):
        assert prohibido not in texto


async def test_un_siniestro_de_otro_cliente_o_inexistente_es_404(app, client):
    ana, _, de_ana = await _radicar(app, client)
    luis = (
        await client.post(
            "/v1/clientes",
            json={
                **CLIENTE_VALIDO,
                "identityRef": "idp-sub-0002",
                "numeroDocumento": "1032456790",
                "email": "luis@example.com",
            },
        )
    ).json()["id"]

    assert (await client.get(_ruta(luis, de_ana))).status_code == 404
    assert (await client.get(_ruta(ana, "no-existe"))).status_code == 404


async def test_en_falta_informacion_el_detalle_muestra_lo_pedido_y_el_reloj_se_detiene(
    app, client, openapi_spec
):
    cliente_id, _, siniestro_id = await _radicar(app, client)
    await _pedir_evidencia(app, siniestro_id, "Falta el recibo de la reparación")

    cuerpo = (await client.get(_ruta(cliente_id, siniestro_id))).json()

    _validar(openapi_spec, "Siniestro", cuerpo)
    assert cuerpo["estado"] == "FALTA_INFORMACION"
    assert cuerpo["fechaLimite"] is None
    assert cuerpo["evidenciaPedida"]["detalle"] == "Falta el recibo de la reparación"


# --- lo que falta ---


async def test_entregar_lo_que_falta_vuelve_a_estudio_y_deja_la_fecha_de_cada_entrega(
    app, client, openapi_spec
):
    cliente_id, _, siniestro_id = await _radicar(app, client)
    await _pedir_evidencia(app, siniestro_id)

    respuesta = await _entregar(client, cliente_id, siniestro_id)

    assert respuesta.status_code == 201
    cuerpo = respuesta.json()
    _validar(openapi_spec, "Siniestro", cuerpo)
    assert cuerpo["estado"] == "EN_ESTUDIO"
    assert cuerpo["fechaLimite"] is not None
    assert cuerpo["evidenciaPedida"]["detalle"] == "Falta el recibo"
    primera, segunda = cuerpo["archivos"]
    assert segunda["entregadoEn"] > primera["entregadoEn"]
    assert segunda["entregadoEn"] == cuerpo["ultimaEntregaEn"]
    assert respuesta.headers["Cache-Control"] == "no-store"


async def test_entregar_lo_que_falta_no_envia_push(app, client):
    cliente_id, _, siniestro_id = await _radicar(app, client)
    await _pedir_evidencia(app, siniestro_id)

    await _entregar(client, cliente_id, siniestro_id)

    assert not [e for e in app.state.events.events if e.tipo == "EnviarPush"]


async def test_repetir_la_entrega_con_la_misma_llave_no_guarda_otra_vez(app, client):
    cliente_id, _, siniestro_id = await _radicar(app, client)
    await _pedir_evidencia(app, siniestro_id)

    primera = await _entregar(client, cliente_id, siniestro_id, llave="llave-1")
    repetida = await _entregar(client, cliente_id, siniestro_id, llave="llave-1")

    assert (primera.status_code, repetida.status_code) == (201, 201)
    assert len(repetida.json()["archivos"]) == len(primera.json()["archivos"]) == 2


async def test_sin_que_se_haya_pedido_nada_la_entrega_es_409(app, client, openapi_spec):
    cliente_id, _, siniestro_id = await _radicar(app, client)

    respuesta = await _entregar(client, cliente_id, siniestro_id)

    assert respuesta.status_code == 409
    assert respuesta.json()["codigo"] == "ESTADO_NO_PERMITE"
    assert respuesta.headers["content-type"].startswith("application/problem+json")
    _validar(openapi_spec, "ProblemaSiniestro", respuesta.json())


async def test_una_segunda_entrega_con_otra_llave_es_409(app, client):
    cliente_id, _, siniestro_id = await _radicar(app, client)
    await _pedir_evidencia(app, siniestro_id)
    await _entregar(client, cliente_id, siniestro_id, llave="a")

    respuesta = await _entregar(client, cliente_id, siniestro_id, llave="b")

    assert respuesta.status_code == 409


async def test_dos_entregas_simultaneas_dejan_una(app, client):
    cliente_id, _, siniestro_id = await _radicar(app, client)
    await _pedir_evidencia(app, siniestro_id)

    respuestas = await asyncio.gather(
        _entregar(client, cliente_id, siniestro_id, llave="a"),
        _entregar(client, cliente_id, siniestro_id, llave="b"),
    )

    assert sorted(r.status_code for r in respuestas) == [201, 409]
    detalle = (await client.get(_ruta(cliente_id, siniestro_id))).json()
    assert len(detalle["archivos"]) == 2


async def test_la_entrega_de_un_siniestro_ajeno_o_inexistente_es_404(app, client):
    ana, _, de_ana = await _radicar(app, client)
    await _pedir_evidencia(app, de_ana)

    assert (await _entregar(client, "otro-cliente", de_ana)).status_code == 404
    assert (await _entregar(client, ana, "no-existe")).status_code == 404


async def test_la_entrega_necesita_un_dato_por_archivo_y_al_menos_un_archivo(app, client):
    cliente_id, _, siniestro_id = await _radicar(app, client)
    await _pedir_evidencia(app, siniestro_id)
    ruta = _ruta(cliente_id, siniestro_id, "evidencias")
    foto = ("a.jpg", FOTO, "image/jpeg")

    desparejada = await client.post(
        ruta,
        data={"datos": json.dumps({"archivos": [{"clase": "DOCUMENTO"}]})},
        files=[("archivos", foto), ("archivos", foto)],
    )
    sin_datos = await client.post(
        ruta, data={"datos": json.dumps({"archivos": []})}, files=[("archivos", foto)]
    )
    ilegible = await client.post(ruta, data={"datos": "no es json"}, files=[("archivos", foto)])
    sin_archivos = await client.post(ruta, data={"datos": json.dumps({"archivos": []})})

    assert desparejada.status_code == 400
    assert sin_datos.status_code == 400
    assert ilegible.status_code == 400
    assert sin_archivos.status_code == 400
    # Nada de lo anterior movió el siniestro.
    assert (await client.get(_ruta(cliente_id, siniestro_id))).json()[
        "estado"
    ] == "FALTA_INFORMACION"


async def test_una_imagen_invalida_o_enorme_en_la_entrega_no_cambia_el_siniestro(
    app, client, settings
):
    cliente_id, _, siniestro_id = await _radicar(app, client)
    await _pedir_evidencia(app, siniestro_id)
    enorme = FOTO + b"\0" * (settings.evidencia_max_bytes + 1)

    mala = await _entregar(client, cliente_id, siniestro_id, contenido=b"no soy una foto")
    grande = await _entregar(client, cliente_id, siniestro_id, contenido=enorme)

    assert (mala.status_code, grande.status_code) == (400, 413)
    detalle = (await client.get(_ruta(cliente_id, siniestro_id))).json()
    assert detalle["estado"] == "FALTA_INFORMACION"
    assert len(detalle["archivos"]) == 1
