"""BITS-101: la API de pólizas completa contra SQLite, validada contra el contrato OpenAPI."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest
from jsonschema import Draft202012Validator

from app.adapters.polizas_fake import cotizacion_de_ejemplo
from app.domain import OrigenDatos
from tests import imagenes
from tests.conftest import CLIENTE_VALIDO

DOCS = [{"tipo": "poliza", "version": "V1"}]


def _validar(spec: dict, ref: str, instancia) -> None:
    schema = {"$ref": f"#/components/schemas/{ref}", "components": spec["components"]}
    errores = sorted(Draft202012Validator(schema).iter_errors(instancia), key=str)
    assert not errores, [e.message for e in errores]


async def _registrar(http, **cambios) -> str:
    respuesta = await http.post("/v1/clientes", json={**CLIENTE_VALIDO, **cambios})
    assert respuesta.status_code == 201
    return respuesta.json()["id"]


async def _subir(http, cliente_id, tipo):
    datos = imagenes.a_bytes(imagenes.sin_tarjeta())
    respuesta = await http.post(
        f"/v1/clientes/{cliente_id}/evidencias-identidad",
        data={"tipo": tipo},
        files={"imagen": ("foto.jpg", datos, "image/jpeg")},
    )
    assert respuesta.status_code == 201


async def _evidencias(http, cliente_id):
    for tipo in ("cedula_frente", "cedula_reverso", "selfie"):
        await _subir(http, cliente_id, tipo)


def _cotizar(app, cliente_id, **kwargs):
    return app.state.polizas._cotizaciones.poner(cliente_id, cotizacion_de_ejemplo(**kwargs))


def _cuerpo(cotizacion, coberturas=("VIDA",)):
    return {
        "coberturas": list(coberturas),
        "cotizacionCreadaEn": cotizacion.creada_en.isoformat(),
        "documentosAceptados": DOCS,
    }


async def _solicitar(http, cliente_id, cotizacion, coberturas=("VIDA",)):
    return await http.post(
        f"/v1/clientes/{cliente_id}/polizas", json=_cuerpo(cotizacion, coberturas)
    )


# --- flujo ---


async def test_la_solicitud_pendiente_se_emite_sola_cuando_el_cliente_cumple(
    app, client, openapi_spec
):
    cliente_id = await _registrar(client)
    cot = _cotizar(app, cliente_id)

    creada = await _solicitar(client, cliente_id, cot, ("VIDA", "HOGAR"))

    assert creada.status_code == 201
    cuerpo = creada.json()
    _validar(openapi_spec, "Poliza", cuerpo)
    assert cuerpo["estado"] == "PENDIENTE"
    assert cuerpo["numero"] is None
    assert cuerpo["documento"] == {"estado": "NO_DISPONIBLE"}
    assert [p["codigo"] for p in cuerpo["pendientes"]] == [
        "CORREO_SIN_CONFIRMAR",
        "EVIDENCIA_FALTANTE",
    ]
    assert cuerpo["pendientes"][1]["detalle"] == {
        "evidencias": ["cedula_frente", "cedula_reverso", "selfie"]
    }
    assert [c["codigo"] for c in cuerpo["coberturas"]] == ["VIDA", "HOGAR"]
    assert cuerpo["primaTotalMensual"] == 42368
    assert cuerpo["origenDatos"] == "CONFIRMADO"
    poliza_id = cuerpo["polizaId"]

    await _evidencias(client, cliente_id)  # el gancho de evidencias re-evalúa
    despues = (await client.get(f"/v1/clientes/{cliente_id}/polizas/{poliza_id}")).json()
    assert [p["codigo"] for p in despues["pendientes"]] == ["CORREO_SIN_CONFIRMAR"]

    await client.post(f"/v1/clientes/{cliente_id}/confirmacion")  # el gancho del correo

    emitida = (await client.get(f"/v1/clientes/{cliente_id}/polizas/{poliza_id}")).json()
    _validar(openapi_spec, "Poliza", emitida)
    assert emitida["estado"] == "EMITIDA"
    assert emitida["numero"].startswith("SOL-VH-")
    assert emitida["pendientes"] == []
    assert emitida["documento"] == {"estado": "DISPONIBLE"}
    assert emitida["vigencia"]["desde"] < emitida["vigencia"]["hasta"]
    assert emitida["solicitudVigenteHasta"] is None
    eventos = [e for e in app.state.events.events if e.tipo == "PolizaEmitida"]
    assert len(eventos) == 1


async def test_con_todo_cumplido_se_emite_en_la_misma_respuesta(app, client, openapi_spec):
    cliente_id = await _registrar(client)
    await client.post(f"/v1/clientes/{cliente_id}/confirmacion")
    await _evidencias(client, cliente_id)
    cot = _cotizar(app, cliente_id)

    respuesta = await _solicitar(client, cliente_id, cot)

    assert respuesta.status_code == 201
    assert respuesta.json()["estado"] == "EMITIDA"
    _validar(openapi_spec, "Poliza", respuesta.json())


async def test_repetir_la_solicitud_responde_200_con_la_misma_poliza(app, client):
    cliente_id = await _registrar(client)
    cot = _cotizar(app, cliente_id)
    primera = await _solicitar(client, cliente_id, cot)

    segunda = await _solicitar(client, cliente_id, cot)

    assert (primera.status_code, segunda.status_code) == (201, 200)
    assert primera.json()["polizaId"] == segunda.json()["polizaId"]


async def test_lista_las_polizas_del_cliente_y_filtra_por_estado(app, client, openapi_spec):
    cliente_id = await _registrar(client)
    await _solicitar(client, cliente_id, _cotizar(app, cliente_id))

    todas = await client.get(f"/v1/clientes/{cliente_id}/polizas")
    emitidas = await client.get(f"/v1/clientes/{cliente_id}/polizas", params={"estado": "EMITIDA"})

    assert todas.status_code == 200
    for item in todas.json():
        _validar(openapi_spec, "Poliza", item)
    assert len(todas.json()) == 1
    assert emitidas.json() == []


async def test_el_documento_de_una_emitida(app, client):
    cliente_id = await _registrar(client)
    await client.post(f"/v1/clientes/{cliente_id}/confirmacion")
    await _evidencias(client, cliente_id)
    poliza = (await _solicitar(client, cliente_id, _cotizar(app, cliente_id))).json()

    respuesta = await client.get(
        f"/v1/clientes/{cliente_id}/polizas/{poliza['polizaId']}/documento"
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["estado"] == "LISTO"
    assert respuesta.json()["url"].endswith(f"{poliza['numero']}.pdf")


async def test_el_documento_de_una_pendiente_es_409(app, client, openapi_spec):
    cliente_id = await _registrar(client)
    poliza = (await _solicitar(client, cliente_id, _cotizar(app, cliente_id))).json()

    respuesta = await client.get(
        f"/v1/clientes/{cliente_id}/polizas/{poliza['polizaId']}/documento"
    )

    assert respuesta.status_code == 409
    assert respuesta.json()["codigo"] == "POLIZA_NO_EMITIDA"
    _validar(openapi_spec, "ProblemaPoliza", respuesta.json())


async def test_el_documento_generandose_responde_202(app, client):
    from app.domain import DocumentoPoliza, EstadoDocumentoPoliza

    cliente_id = await _registrar(client)
    await client.post(f"/v1/clientes/{cliente_id}/confirmacion")
    await _evidencias(client, cliente_id)
    poliza = (await _solicitar(client, cliente_id, _cotizar(app, cliente_id))).json()

    async def generando(_poliza):
        return DocumentoPoliza(EstadoDocumentoPoliza.GENERANDO)

    app.state.polizas._documentos.consultar = generando

    respuesta = await client.get(
        f"/v1/clientes/{cliente_id}/polizas/{poliza['polizaId']}/documento"
    )

    assert respuesta.status_code == 202
    assert respuesta.headers["retry-after"] == "2"
    assert respuesta.json() == {"estado": "GENERANDO", "url": None, "expiraEn": None}


# --- errores ---


async def test_errores_de_la_solicitud_llevan_su_codigo(app, client, openapi_spec):
    cliente_id = await _registrar(client)
    cot = _cotizar(app, cliente_id)
    otro = await _registrar(
        client, identityRef="idp-2", numeroDocumento="1032456790", email="otro@correo.co"
    )
    sin = await client.post(f"/v1/clientes/{otro}/polizas", json=_cuerpo(cot))  # sin cotización

    # La activa ya no es la que el cliente vio.
    app.state.polizas._cotizaciones.poner(
        cliente_id, replace(cot, creada_en=cot.creada_en + timedelta(seconds=3))
    )
    cambio = await _solicitar(client, cliente_id, cot)

    # La activa expiró.
    expirada = replace(cot, estado="EXPIRADA")
    app.state.polizas._cotizaciones.poner(cliente_id, expirada)
    vencida = await _solicitar(client, cliente_id, expirada)

    assert (sin.status_code, sin.json()["codigo"]) == (409, "SIN_COTIZACION")
    assert (cambio.status_code, cambio.json()["codigo"]) == (409, "COTIZACION_CAMBIO")
    assert (vencida.status_code, vencida.json()["codigo"]) == (409, "COTIZACION_VENCIDA")
    for respuesta in (sin, cambio, vencida):
        _validar(openapi_spec, "ProblemaPoliza", respuesta.json())


async def test_edad_fuera_de_rango_es_422(app, client, openapi_spec):
    cliente_id = await _registrar(client, fechaNacimiento="1940-01-01")
    cot = _cotizar(app, cliente_id)

    respuesta = await _solicitar(client, cliente_id, cot)

    assert respuesta.status_code == 422
    assert respuesta.json()["codigo"] == "FUERA_DE_RANGO"
    _validar(openapi_spec, "ProblemaPoliza", respuesta.json())


async def test_cotizacion_caida_es_503(app, client, openapi_spec):
    cliente_id = await _registrar(client)
    cot = _cotizar(app, cliente_id)
    app.state.polizas._cotizaciones.caida = True

    respuesta = await _solicitar(client, cliente_id, cot)

    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "COTIZACION_NO_DISPONIBLE"
    _validar(openapi_spec, "ProblemaPoliza", respuesta.json())


async def test_otras_coberturas_sobre_la_misma_solicitud_es_409(app, client):
    cliente_id = await _registrar(client)
    cot = _cotizar(app, cliente_id)
    await _solicitar(client, cliente_id, cot, ("VIDA",))

    respuesta = await _solicitar(client, cliente_id, cot, ("VIDA", "HOGAR"))

    assert respuesta.status_code == 409
    assert respuesta.json()["codigo"] == "SOLICITUD_EXISTENTE"


@pytest.mark.parametrize(
    ("cambio", "campo"),
    [
        ({"coberturas": ["HOGAR"]}, "coberturas"),
        ({"coberturas": ["OTRA"]}, "coberturas"),
        ({"coberturas": []}, "coberturas"),
        ({"documentosAceptados": []}, "documentosAceptados"),
        ({"documentosAceptados": [{"tipo": "poliza", "version": "uno"}]}, "documentosAceptados"),
        ({"cotizacionCreadaEn": "2026-10-24T12:00:00"}, "cotizacionCreadaEn"),
    ],
)
async def test_cuerpo_invalido_es_400_con_el_campo(app, client, cambio, campo):
    cliente_id = await _registrar(client)
    cot = _cotizar(app, cliente_id)

    respuesta = await client.post(
        f"/v1/clientes/{cliente_id}/polizas", json={**_cuerpo(cot), **cambio}
    )

    assert respuesta.status_code == 400
    assert any(campo in e["campo"] for e in respuesta.json()["errores"])


async def test_cliente_inexistente_es_404(client):
    respuesta = await client.get("/v1/clientes/nadie/polizas")

    assert respuesta.status_code == 404


async def test_poliza_ajena_o_inexistente_es_404(app, client):
    cliente_id = await _registrar(client)

    respuesta = await client.get(f"/v1/clientes/{cliente_id}/polizas/no-existe")

    assert respuesta.status_code == 404


# --- el cliente vuelve a cotizar ---


async def test_la_pendiente_de_la_cotizacion_anterior_se_borra_con_el_evento(app, client):
    from app.consumidores import despachar_evento
    from app.domain import DomainEvent

    cliente_id = await _registrar(client)
    poliza = (await _solicitar(client, cliente_id, _cotizar(app, cliente_id))).json()
    nueva = _cotizar(app, cliente_id)
    nueva = app.state.polizas._cotizaciones.poner(
        cliente_id, replace(nueva, creada_en=nueva.creada_en + timedelta(minutes=1))
    )

    await despachar_evento(
        app.state.polizas,
        DomainEvent(
            "CotizacionCreada", {"clienteId": cliente_id, "creadaEn": nueva.creada_en.isoformat()}
        ),
    )

    respuesta = await client.get(f"/v1/clientes/{cliente_id}/polizas/{poliza['polizaId']}")
    assert respuesta.status_code == 404


# --- backoffice ---


async def test_el_backoffice_ve_la_cola_y_el_detalle(app, client, openapi_spec):
    cliente_id = await _registrar(client)
    await client.post(f"/v1/clientes/{cliente_id}/confirmacion")
    await _evidencias(client, cliente_id)
    poliza = (
        await _solicitar(
            client, cliente_id, _cotizar(app, cliente_id, origen=OrigenDatos.DECLARADO)
        )
    ).json()

    cola = await client.get(
        "/v1/polizas", params={"estado": "PENDIENTE", "tipoPendiente": "AGENTE"}
    )
    detalle = await client.get(f"/v1/polizas/{poliza['polizaId']}")

    assert cola.status_code == 200
    _validar(openapi_spec, "PaginaPolizas", cola.json())
    assert [i["polizaId"] for i in cola.json()["items"]] == [poliza["polizaId"]]
    assert cola.json()["items"][0]["pendientes"] == [
        {"codigo": "CREDITO_NO_VERIFICADO", "tipo": "AGENTE"}
    ]
    assert cola.json()["siguienteCursor"] is None
    _validar(openapi_spec, "PolizaInterna", detalle.json())
    assert detalle.json()["clienteId"] == cliente_id
    assert {e["tipo"]: e["estado"] for e in detalle.json()["evidencias"]} == {
        "cedula_frente": "ACEPTADA",
        "cedula_reverso": "ACEPTADA",
        "selfie": "ACEPTADA",
    }
    assert detalle.json()["decisiones"] == []


async def test_el_agente_aprueba_y_la_poliza_se_emite(app, client, openapi_spec):
    cliente_id = await _registrar(client)
    await client.post(f"/v1/clientes/{cliente_id}/confirmacion")
    await _evidencias(client, cliente_id)
    poliza = (
        await _solicitar(
            client, cliente_id, _cotizar(app, cliente_id, origen=OrigenDatos.DECLARADO)
        )
    ).json()

    respuesta = await client.post(
        f"/v1/polizas/{poliza['polizaId']}/decisiones",
        json={
            "decision": "APROBAR",
            "agente": "maria@solventa.co",
            "pendientes": ["CREDITO_NO_VERIFICADO"],
            "nota": "Certificado del banco",
        },
    )

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    _validar(openapi_spec, "PolizaInterna", cuerpo)
    assert cuerpo["estado"] == "EMITIDA"
    assert cuerpo["decisiones"][0]["agente"] == "maria@solventa.co"
    assert {e["tipo"] for e in cuerpo["evidencias"]} == {
        "cedula_frente",
        "cedula_reverso",
        "selfie",
    }


async def test_el_agente_rechaza_y_el_cliente_ve_el_motivo_del_escenario(app, client, openapi_spec):
    cliente_id = await _registrar(client)
    cot = _cotizar(app, cliente_id, origen=OrigenDatos.DECLARADO)
    poliza = (await _solicitar(client, cliente_id, cot)).json()

    respuesta = await client.post(
        f"/v1/polizas/{poliza['polizaId']}/decisiones",
        json={
            "decision": "RECHAZAR",
            "agente": "maria@solventa.co",
            "pendientes": ["CREDITO_NO_VERIFICADO"],
        },
    )
    vista_cliente = (
        await client.get(f"/v1/clientes/{cliente_id}/polizas/{poliza['polizaId']}")
    ).json()

    assert respuesta.status_code == 200
    _validar(openapi_spec, "PolizaInterna", respuesta.json())
    _validar(openapi_spec, "Poliza", vista_cliente)
    assert vista_cliente["estado"] == "RECHAZADA"
    assert vista_cliente["rechazo"]["motivos"] == [
        {
            "codigo": "CREDITO_NO_VERIFICADO",
            "mensaje": "No pudimos verificar los datos de tu crédito con la entidad acreedora.",
        }
    ]
    repetir = await _solicitar(client, cliente_id, cot)
    assert repetir.status_code == 409
    assert repetir.json()["codigo"] == "SOLICITUD_RECHAZADA"


@pytest.mark.parametrize(
    ("cuerpo", "estado", "codigo"),
    [
        (
            {"decision": "APROBAR", "agente": "m", "pendientes": ["EVIDENCIA_EN_REVISION"]},
            422,
            "PENDIENTE_NO_APLICA",
        ),
        (
            {"decision": "APROBAR", "agente": "m", "pendientes": ["CORREO_SIN_CONFIRMAR"]},
            400,
            None,
        ),
        ({"decision": "APROBAR", "agente": "m", "pendientes": []}, 400, None),
        ({"decision": "BORRAR", "agente": "m", "pendientes": ["CREDITO_NO_VERIFICADO"]}, 400, None),
        ({"decision": "APROBAR", "pendientes": ["CREDITO_NO_VERIFICADO"]}, 400, None),
    ],
)
async def test_decisiones_invalidas(app, client, openapi_spec, cuerpo, estado, codigo):
    cliente_id = await _registrar(client)
    poliza = (
        await _solicitar(
            client, cliente_id, _cotizar(app, cliente_id, origen=OrigenDatos.DECLARADO)
        )
    ).json()

    respuesta = await client.post(f"/v1/polizas/{poliza['polizaId']}/decisiones", json=cuerpo)

    assert respuesta.status_code == estado
    if codigo:
        assert respuesta.json()["codigo"] == codigo
        _validar(openapi_spec, "ProblemaPoliza", respuesta.json())


async def test_decidir_sobre_una_emitida_es_409(app, client):
    cliente_id = await _registrar(client)
    await client.post(f"/v1/clientes/{cliente_id}/confirmacion")
    await _evidencias(client, cliente_id)
    poliza = (await _solicitar(client, cliente_id, _cotizar(app, cliente_id))).json()

    respuesta = await client.post(
        f"/v1/polizas/{poliza['polizaId']}/decisiones",
        json={"decision": "APROBAR", "agente": "m", "pendientes": ["CREDITO_NO_VERIFICADO"]},
    )

    assert respuesta.status_code == 409
    assert respuesta.json()["codigo"] == "POLIZA_NO_PENDIENTE"


async def test_poliza_inexistente_en_el_backoffice_es_404(client):
    assert (await client.get("/v1/polizas/no-existe")).status_code == 404
    decidir = await client.post(
        "/v1/polizas/no-existe/decisiones",
        json={"decision": "APROBAR", "agente": "m", "pendientes": ["CREDITO_NO_VERIFICADO"]},
    )
    assert decidir.status_code == 404


async def test_un_cursor_invalido_es_400(client):
    for cursor in ("???", "aGVsbG8="):  # no es base64 / no tiene separador
        respuesta = await client.get("/v1/polizas", params={"cursor": cursor})
        assert respuesta.status_code == 400


async def test_la_cola_pagina_con_cursor(app, client):
    for numero in range(3):
        cliente_id = await _registrar(
            client,
            identityRef=f"idp-{numero}",
            numeroDocumento=f"10324567{numero}0",
            email=f"c{numero}@correo.co",
        )
        await _solicitar(client, cliente_id, _cotizar(app, cliente_id))

    primera = (await client.get("/v1/polizas", params={"limite": 2})).json()
    resto = (
        await client.get("/v1/polizas", params={"limite": 2, "cursor": primera["siguienteCursor"]})
    ).json()
    reves = (await client.get("/v1/polizas", params={"orden": "-creadaEn", "limite": 1})).json()

    assert len(primera["items"]) == 2 and primera["siguienteCursor"]
    assert len(resto["items"]) == 1 and resto["siguienteCursor"] is None
    assert reves["items"][0]["polizaId"] == resto["items"][0]["polizaId"]


# --- concurrencia (SQLite real: dos conexiones) ---


async def test_dos_evaluaciones_simultaneas_emiten_una_sola_vez(app, client):
    cliente_id = await _registrar(client)
    await client.post(f"/v1/clientes/{cliente_id}/confirmacion")
    cot = _cotizar(app, cliente_id)
    poliza = (await _solicitar(client, cliente_id, cot)).json()
    assert poliza["estado"] == "PENDIENTE"
    servicio = app.state.polizas
    for tipo in ("cedula_frente", "cedula_reverso", "selfie"):
        await _subir_sin_gancho(app, client, cliente_id, tipo)

    pendiente = await servicio._polizas.obtener(poliza["polizaId"])
    resultados = await asyncio.gather(
        servicio.reevaluar(replace(pendiente)), servicio.reevaluar(replace(pendiente))
    )

    emitidas = [r for r in resultados if r.estado.value == "EMITIDA"]
    assert len(emitidas) == 2  # las dos ven la póliza emitida...
    assert len({r.numero for r in emitidas}) == 1  # ...con el mismo número
    assert len([e for e in app.state.events.events if e.tipo == "PolizaEmitida"]) == 1


async def _subir_sin_gancho(app, http, cliente_id, tipo):
    """Sube la evidencia sin que el gancho re-evalúe: la evaluación la dispara la prueba."""
    evidencias = app.state.evidencias
    original = evidencias._al_guardar

    async def nada(_cliente_id):
        return None

    evidencias._al_guardar = nada
    try:
        await _subir(http, cliente_id, tipo)
    finally:
        evidencias._al_guardar = original


async def test_un_cursor_sin_separador_es_400(client):
    import base64

    cursor = base64.urlsafe_b64encode(b"2026-10-24T12:00:00+00:00").decode()

    respuesta = await client.get("/v1/polizas", params={"cursor": cursor})

    assert respuesta.status_code == 400


async def test_si_el_servicio_de_documentos_cae_es_503(app, client, openapi_spec):
    from app.domain import DocumentoNoDisponible

    cliente_id = await _registrar(client)
    await client.post(f"/v1/clientes/{cliente_id}/confirmacion")
    await _evidencias(client, cliente_id)
    poliza = (await _solicitar(client, cliente_id, _cotizar(app, cliente_id))).json()

    async def cae(_poliza):
        raise DocumentoNoDisponible()

    app.state.polizas._documentos.consultar = cae

    respuesta = await client.get(
        f"/v1/clientes/{cliente_id}/polizas/{poliza['polizaId']}/documento"
    )

    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "DOCUMENTO_NO_DISPONIBLE"
    _validar(openapi_spec, "ProblemaPoliza", respuesta.json())
