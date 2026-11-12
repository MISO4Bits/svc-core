"""BITS-381: ``POST /v1/clientes/{clienteId}/siniestros`` de punta a punta."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from jsonschema import Draft202012Validator

from app.adapters.creditos_fake import CreditosFake
from app.adapters.polizas_fake import cotizacion_de_ejemplo
from app.domain import CreditosReportados, HipotecaReportada, OrigenSaldo
from app.siniestros import hoy_en_colombia
from tests import imagenes
from tests.conftest import CLIENTE_VALIDO

DOCS = [{"tipo": "poliza", "version": "V1"}]
FOTO = imagenes.a_bytes(imagenes.sin_tarjeta())
TRANSFERENCIA = {
    "tipo": "TRANSFERENCIA",
    "banco": "Bancolombia",
    "tipoCuenta": "AHORROS",
    "numeroCuenta": "48210012345",
}
CHEQUE = {
    "tipo": "CHEQUE",
    "oficina": {"id": "of-1", "banco": "BBVA", "nombre": "Chapinero", "direccion": "Cra 13 # 60"},
}


def _validar(spec: dict, ref: str, instancia) -> None:
    schema = {"$ref": f"#/components/schemas/{ref}", "components": spec["components"]}
    errores = sorted(Draft202012Validator(schema).iter_errors(instancia), key=str)
    assert not errores, [e.message for e in errores]


async def _poliza_emitida(
    app, http, *, coberturas=("VIDA", "HOGAR"), cliente=None
) -> tuple[str, str]:
    """(clienteId, polizaId) de una póliza ya emitida."""
    datos = {**CLIENTE_VALIDO, **(cliente or {})}
    cliente_id = (await http.post("/v1/clientes", json=datos)).json()["id"]
    await http.post(f"/v1/clientes/{cliente_id}/confirmacion")
    for tipo in ("cedula_frente", "cedula_reverso", "selfie"):
        subida = await http.post(
            f"/v1/clientes/{cliente_id}/evidencias-identidad",
            data={"tipo": tipo},
            files={"imagen": ("foto.jpg", FOTO, "image/jpeg")},
        )
        assert subida.status_code == 201
    cotizacion = cotizacion_de_ejemplo()
    app.state.polizas._cotizaciones.poner(cliente_id, cotizacion)
    respuesta = await http.post(
        f"/v1/clientes/{cliente_id}/polizas",
        json={
            "coberturas": list(coberturas),
            "cotizacionCreadaEn": cotizacion.creada_en.isoformat(),
            "documentosAceptados": DOCS,
        },
    )
    assert respuesta.json()["estado"] == "EMITIDA"
    return cliente_id, respuesta.json()["polizaId"]


def _hoy() -> str:
    # La fecha de Colombia, que es la que usa el servicio. Con la de la máquina, un equipo en UTC
    # después de las 19:00 de Colombia mandaría un día en el futuro (FUERA_DE_VIGENCIA).
    return hoy_en_colombia().isoformat()


def _datos(poliza_id, **cambios) -> dict:
    base = {
        "polizaId": poliza_id,
        "tipo": "INCAPACIDAD_TOTAL",
        "fechaOcurrencia": _hoy(),
        "archivos": [{"clase": "DOCUMENTO", "capturadoEn": datetime.now(UTC).isoformat()}],
    }
    return {**base, **cambios}


def _incendio(poliza_id, **cambios) -> dict:
    base = {
        "tipo": "INCENDIO_TERREMOTO",
        "narracion": "Se incendió la cocina",
        "destinoPago": TRANSFERENCIA,
        "archivos": [
            {
                "clase": "FOTO",
                "capturadoEn": datetime.now(UTC).isoformat(),
                "latitud": 4.65,
                "longitud": -74.06,
            }
        ],
    }
    return _datos(poliza_id, **{**base, **cambios})


async def _reportar(http, cliente_id, datos, *, archivos=1, llave=None, contenido=FOTO):
    cabeceras = {"Idempotency-Key": llave} if llave else {}
    return await http.post(
        f"/v1/clientes/{cliente_id}/siniestros",
        data={"datos": json.dumps(datos)},
        files=[("archivos", (f"a{i}.jpg", contenido, "image/jpeg")) for i in range(archivos)],
        headers=cabeceras,
    )


async def _guardado(app, respuesta):
    return await app.state.siniestros._siniestros.obtener(respuesta.json()["id"])


# --- reporte exitoso ---


async def test_incapacidad_total_se_radica_y_cumple_el_contrato(app, client, openapi_spec):
    cliente_id, poliza_id = await _poliza_emitida(app, client)

    respuesta = await _reportar(client, cliente_id, _datos(poliza_id))

    assert respuesta.status_code == 201
    _validar(openapi_spec, "SiniestroReportado", respuesta.json())
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == "EN_ESTUDIO"
    assert re.fullmatch(r"SIN-\d{4}-[0-9A-Z]{6}", cuerpo["numero"])
    guardado = await _guardado(app, respuesta)
    assert guardado.destino_pago is None
    [archivo] = guardado.archivos
    assert archivo.referencia.startswith(f"siniestros/{cliente_id}/{guardado.id}/")
    assert archivo.entregado_en == guardado.ultima_entrega_en


async def test_incendio_guarda_el_destino_tal_como_llega_y_la_ubicacion(app, client):
    cliente_id, poliza_id = await _poliza_emitida(app, client)

    respuesta = await _reportar(client, cliente_id, _incendio(poliza_id))

    assert respuesta.status_code == 201
    guardado = await _guardado(app, respuesta)
    assert guardado.destino_pago == TRANSFERENCIA
    assert guardado.narracion == "Se incendió la cocina"
    assert (guardado.archivos[0].latitud, guardado.archivos[0].longitud) == (4.65, -74.06)


async def test_el_destino_por_cheque_se_guarda_con_la_oficina(app, client):
    cliente_id, poliza_id = await _poliza_emitida(app, client)

    respuesta = await _reportar(client, cliente_id, _incendio(poliza_id, destinoPago=CHEQUE))

    assert respuesta.status_code == 201
    assert (await _guardado(app, respuesta)).destino_pago == CHEQUE


async def test_una_foto_sin_ubicacion_se_acepta(app, client):
    cliente_id, poliza_id = await _poliza_emitida(app, client)
    datos = _incendio(poliza_id, archivos=[{"clase": "FOTO", "latitud": None, "longitud": None}])

    respuesta = await _reportar(client, cliente_id, datos)

    assert respuesta.status_code == 201
    assert (await _guardado(app, respuesta)).archivos[0].latitud is None


async def test_vida_y_tu_casa_son_independientes(app, client):
    cliente_id, poliza_id = await _poliza_emitida(app, client)

    vida = await _reportar(client, cliente_id, _datos(poliza_id))
    casa = await _reportar(client, cliente_id, _incendio(poliza_id))

    assert (vida.status_code, casa.status_code) == (201, 201)


async def test_un_reporte_no_envia_aviso_push(app, client):
    cliente_id, poliza_id = await _poliza_emitida(app, client)

    await _reportar(client, cliente_id, _datos(poliza_id))

    assert not [e for e in app.state.events.events if e.tipo == "EnviarPush"]


# --- idempotencia y siniestro en curso ---


async def test_reintentar_con_la_misma_llave_devuelve_el_mismo_siniestro(app, client):
    cliente_id, poliza_id = await _poliza_emitida(app, client)

    primero = await _reportar(client, cliente_id, _datos(poliza_id), llave="llave-1")
    segundo = await _reportar(client, cliente_id, _datos(poliza_id), llave="llave-1")

    assert (primero.status_code, segundo.status_code) == (201, 201)
    assert primero.json() == segundo.json()


async def test_un_segundo_reporte_de_la_misma_cobertura_es_409(app, client, openapi_spec):
    cliente_id, poliza_id = await _poliza_emitida(app, client)
    await _reportar(client, cliente_id, _datos(poliza_id), llave="llave-1")

    respuesta = await _reportar(client, cliente_id, _datos(poliza_id), llave="llave-2")

    assert respuesta.status_code == 409
    assert respuesta.json()["codigo"] == "SINIESTRO_EN_CURSO"
    assert respuesta.headers["content-type"].startswith("application/problem+json")
    _validar(openapi_spec, "ProblemaSiniestro", respuesta.json())


async def test_dos_reportes_simultaneos_de_la_misma_cobertura_dejan_uno(app, client):
    cliente_id, poliza_id = await _poliza_emitida(app, client)

    respuestas = await asyncio.gather(
        _reportar(client, cliente_id, _datos(poliza_id), llave="a"),
        _reportar(client, cliente_id, _datos(poliza_id), llave="b"),
    )

    assert sorted(r.status_code for r in respuestas) == [201, 409]


# --- vigencia, cobertura y pertenencia ---


async def test_fecha_futura_o_anterior_a_la_emision_es_fuera_de_vigencia(app, client, openapi_spec):
    cliente_id, poliza_id = await _poliza_emitida(app, client)
    manana = (datetime.now(UTC) + timedelta(days=2)).date().isoformat()
    ayer = (datetime.now(UTC) - timedelta(days=2)).date().isoformat()

    for fecha in (manana, ayer):
        respuesta = await _reportar(client, cliente_id, _datos(poliza_id, fechaOcurrencia=fecha))
        assert respuesta.status_code == 409
        assert respuesta.json()["codigo"] == "FUERA_DE_VIGENCIA"
        _validar(openapi_spec, "ProblemaSiniestro", respuesta.json())


async def test_incendio_sin_la_cobertura_hogar_es_400_tipo_no_cubierto(app, client, openapi_spec):
    cliente_id, poliza_id = await _poliza_emitida(app, client, coberturas=("VIDA",))

    respuesta = await _reportar(client, cliente_id, _incendio(poliza_id))

    assert respuesta.status_code == 400
    assert respuesta.json()["codigo"] == "TIPO_NO_CUBIERTO"
    _validar(openapi_spec, "ProblemaSiniestro", respuesta.json())


async def test_poliza_ajena_o_inexistente_o_cliente_inexistente_es_404(app, client):
    ana, poliza_de_ana = await _poliza_emitida(app, client)
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

    assert (await _reportar(client, luis, _datos(poliza_de_ana))).status_code == 404
    assert (await _reportar(client, ana, _datos("no-existe"))).status_code == 404
    assert (await _reportar(client, "no-existe", _datos(poliza_de_ana))).status_code == 404


# --- reporte incompleto o inválido ---


async def test_incendio_sin_destino_del_pago_es_400(app, client):
    cliente_id, poliza_id = await _poliza_emitida(app, client)
    datos = _incendio(poliza_id)
    del datos["destinoPago"]

    respuesta = await _reportar(client, cliente_id, datos)

    assert respuesta.status_code == 400
    assert respuesta.json()["errores"][0]["campo"] == "destinoPago"


async def test_incapacidad_con_destino_del_pago_es_400(app, client):
    cliente_id, poliza_id = await _poliza_emitida(app, client)

    respuesta = await _reportar(client, cliente_id, _datos(poliza_id, destinoPago=TRANSFERENCIA))

    assert respuesta.status_code == 400
    assert respuesta.json()["errores"][0]["campo"] == "destinoPago"


async def test_incendio_sin_narracion_es_400(app, client):
    cliente_id, poliza_id = await _poliza_emitida(app, client)

    respuesta = await _reportar(client, cliente_id, _incendio(poliza_id, narracion="  "))

    assert respuesta.status_code == 400
    assert respuesta.json()["errores"][0]["campo"] == "narracion"


async def test_la_evidencia_debe_ser_la_de_su_tipo(app, client):
    cliente_id, poliza_id = await _poliza_emitida(app, client)
    solo_documento = _incendio(poliza_id, archivos=[{"clase": "DOCUMENTO"}])
    solo_foto = _datos(poliza_id, archivos=[{"clase": "FOTO"}])

    assert (await _reportar(client, cliente_id, solo_documento)).status_code == 400
    assert (await _reportar(client, cliente_id, solo_foto)).status_code == 400


async def test_cada_archivo_necesita_su_dato_y_viceversa(app, client):
    cliente_id, poliza_id = await _poliza_emitida(app, client)

    respuesta = await _reportar(client, cliente_id, _datos(poliza_id), archivos=2)

    assert respuesta.status_code == 400
    assert respuesta.json()["errores"][0]["campo"] == "archivos"


async def test_un_archivo_que_no_es_imagen_es_400_y_no_guarda_nada(app, client):
    cliente_id, poliza_id = await _poliza_emitida(app, client)

    respuesta = await _reportar(client, cliente_id, _datos(poliza_id), contenido=b"no soy una foto")

    assert respuesta.status_code == 400
    assert await app.state.siniestros._siniestros.en_curso(poliza_id, _vida()) is None


def _vida():
    from app.domain import TipoSiniestro

    return TipoSiniestro.INCAPACIDAD_TOTAL


async def test_una_imagen_demasiado_grande_es_413(app, client, settings):
    cliente_id, poliza_id = await _poliza_emitida(app, client)
    grande = FOTO + b"\0" * (settings.evidencia_max_bytes + 1)

    respuesta = await _reportar(client, cliente_id, _datos(poliza_id), contenido=grande)

    assert respuesta.status_code == 413


async def test_datos_ilegibles_o_con_valores_fuera_de_rango_son_400(app, client):
    cliente_id, poliza_id = await _poliza_emitida(app, client)
    ruta = f"/v1/clientes/{cliente_id}/siniestros"
    archivo = [("archivos", ("a.jpg", FOTO, "image/jpeg"))]

    for crudo in (
        "esto no es json",
        json.dumps(_datos(poliza_id, tipo="SISMO_PARAMETRICO")),
        json.dumps(_datos(poliza_id, fechaOcurrencia="mañana")),
        json.dumps(_datos(poliza_id, archivos=[{"clase": "FOTO", "latitud": 120.0}])),
        json.dumps(_datos(poliza_id, esAdmin=True)),
    ):
        respuesta = await client.post(ruta, data={"datos": crudo}, files=archivo)
        assert respuesta.status_code == 400, crudo


async def test_el_destino_invalido_es_400_y_el_error_no_repite_el_numero_de_cuenta(app, client):
    cliente_id, poliza_id = await _poliza_emitida(app, client)
    cuenta = {**TRANSFERENCIA, "numeroCuenta": "12ab"}

    respuesta = await _reportar(client, cliente_id, _incendio(poliza_id, destinoPago=cuenta))

    assert respuesta.status_code == 400
    assert "12ab" not in respuesta.text
    assert TRANSFERENCIA["numeroCuenta"] not in respuesta.text


async def test_sin_la_parte_de_archivos_es_400(app, client):
    cliente_id, poliza_id = await _poliza_emitida(app, client)

    respuesta = await client.post(
        f"/v1/clientes/{cliente_id}/siniestros", data={"datos": json.dumps(_datos(poliza_id))}
    )

    assert respuesta.status_code == 400


# --- saldo del crédito ---


def _creditos(app) -> CreditosFake:
    creditos = app.state.siniestros._creditos
    assert isinstance(creditos, CreditosFake)
    return creditos


async def test_sin_datos_de_perfilamiento_el_saldo_es_el_de_la_poliza(app, client):
    cliente_id, poliza_id = await _poliza_emitida(app, client)

    respuesta = await _reportar(client, cliente_id, _datos(poliza_id))

    saldo = (await _guardado(app, respuesta)).saldo_credito
    assert (saldo.valor, saldo.entidad, saldo.origen) == (
        Decimal("140000000"),
        "Bancolombia",
        OrigenSaldo.POLIZA,
    )
    assert _creditos(app).consultas == [cliente_id]


async def test_con_open_finance_el_saldo_es_el_de_la_hipoteca_de_la_entidad_de_la_poliza(
    app, client
):
    cliente_id, poliza_id = await _poliza_emitida(app, client)
    _creditos(app).respuesta = CreditosReportados(
        estado="DISPONIBLE",
        hipotecas=(
            HipotecaReportada("BBVA", Decimal("90000000")),
            HipotecaReportada("BANCOLÓMBIA", Decimal("118500000")),
        ),
    )

    respuesta = await _reportar(client, cliente_id, _datos(poliza_id))

    saldo = (await _guardado(app, respuesta)).saldo_credito
    assert (saldo.valor, saldo.origen) == (Decimal("118500000"), OrigenSaldo.OPEN_FINANCE)
    assert saldo.entidad == "Bancolombia"


async def test_sin_consentimiento_o_sin_la_entidad_se_usa_el_saldo_de_la_poliza(app, client):
    cliente_id, poliza_id = await _poliza_emitida(app, client)
    _creditos(app).respuesta = CreditosReportados(estado="SIN_CONSENTIMIENTO")
    primero = await _reportar(client, cliente_id, _datos(poliza_id))
    _creditos(app).respuesta = CreditosReportados(
        estado="DISPONIBLE", hipotecas=(HipotecaReportada("Davivienda", Decimal("1")),)
    )
    segundo = await _reportar(client, cliente_id, _incendio(poliza_id))

    for respuesta in (primero, segundo):
        saldo = (await _guardado(app, respuesta)).saldo_credito
        assert (saldo.valor, saldo.origen) == (Decimal("140000000"), OrigenSaldo.POLIZA)
