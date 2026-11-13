"""BITS-386: aviso de sismo simulado y pago automático de una cuota, de punta a punta."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from decimal import Decimal

import aiosqlite
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from jsonschema import Draft202012Validator

from app.adapters.factory import maybe_init
from app.api.app import create_app
from app.consumidores import TIPOS_QUE_CONSUME, despachar_evento
from app.domain import (
    CreditosReportados,
    DomainEvent,
    EventoSismo,
    HipotecaReportada,
)
from app.siniestros import hoy_en_colombia
from tests.integration.test_siniestros_api import _poliza_emitida
from tests.integration.test_siniestros_estados_api import _ruta
from tests.integration.test_venta_directa_api import (
    ASISTIDO,
    _asistido,
    _settings,
    _vender,
)

HOY = hoy_en_colombia()


def _validar(spec: dict, ref: str, instancia) -> None:
    schema = {"$ref": f"#/components/schemas/{ref}", "components": spec["components"]}
    errores = sorted(Draft202012Validator(schema).iter_errors(instancia), key=str)
    assert not errores, [e.message for e in errores]


@pytest_asyncio.fixture
async def app(tmp_path):
    aplicacion = create_app(_settings(tmp_path))
    await maybe_init(aplicacion.state.clientes)
    return aplicacion


@pytest_asyncio.fixture
async def http(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as cliente:
        yield cliente


def _sismo(**cambios) -> EventoSismo:
    base = {"evento_id": "sismo-1", "municipio": "Bogotá", "magnitud": 6.1, "fecha": HOY}
    return EventoSismo(**{**base, **cambios})


async def _vendida(http, n: int = 1, *, municipio="Bogotá", cuota=1250000, coberturas=None):
    """(clienteId, polizaId) de una póliza que vendió un socio, con su municipio y su cuota."""
    cliente = {
        "numeroDocumento": f"5212{n:04d}",
        "email": f"cliente{n}@example.com",
        "primerNombre": ASISTIDO["primerNombre"],
    }
    cliente_id = (await _asistido(http, **cliente)).json()["id"]
    cambios = {"municipio": municipio, "cuotaMensual": cuota}
    if coberturas is not None:
        cambios["coberturas"] = coberturas
    respuesta = await _vender(http, cliente_id, **cambios)
    assert respuesta.status_code == 201, respuesta.text
    return cliente_id, respuesta.json()["polizaId"]


async def _siniestros(http, cliente_id: str) -> list[dict]:
    return (await http.get(_ruta(cliente_id))).json()


def _hipotecas(cuota: str | None = "900000", entidad="BANCOLOMBIA S.A.") -> CreditosReportados:
    return CreditosReportados(
        "DISPONIBLE",
        (
            HipotecaReportada(
                entidad, Decimal("120000000"), None if cuota is None else Decimal(cuota)
            ),
        ),
    )


def _creditos(app):
    return app.state.siniestros.creditos


# --- se paga ---


async def test_una_poliza_del_socio_se_paga_con_su_municipio_y_su_cuota_sin_consultar_perfil(
    app, http, openapi_spec
):
    cliente_id, poliza_id = await _vendida(http)

    resumen = await app.state.sismos.procesar(_sismo(municipio="BOGOTA"))  # sin tilde, mayúsculas

    assert resumen.pagadas == 1 and resumen.evaluadas == 1
    [fila] = await _siniestros(http, cliente_id)
    assert fila["estado"] == "PAGADO" and fila["automatico"] is True
    assert fila["tipo"] == "SISMO_PARAMETRICO"
    detalle = (await http.get(_ruta(cliente_id, fila["id"]))).json()
    _validar(openapi_spec, "Siniestro", detalle)
    assert detalle["pago"]["lineas"] == [
        {
            "beneficiario": "BANCO",
            "valor": 1250000,
            "destino": "Bancolombia, abono a la cuota del crédito",
        }
    ]
    assert detalle["evento"] == {"fecha": HOY.isoformat(), "municipio": "BOGOTA", "magnitud": 6.1}
    assert "eventoId" not in str(detalle)
    assert detalle["polizaId"] == poliza_id
    assert detalle["fechaLimite"] is None and detalle["archivos"] == []
    assert _creditos(app).consultas_de_ciudad == [] and _creditos(app).consultas == []


async def test_una_poliza_sin_socio_usa_la_ciudad_del_perfil_y_la_cuota_de_la_hipoteca(app, http):
    cliente_id, _ = await _poliza_emitida(app, http)
    _creditos(app).ciudades[cliente_id] = "Bogotá"
    _creditos(app).respuesta = _hipotecas("900000")

    resumen = await app.state.sismos.procesar(_sismo())

    assert resumen.pagadas == 1
    [fila] = await _siniestros(http, cliente_id)
    detalle = (await http.get(_ruta(cliente_id, fila["id"]))).json()
    assert detalle["pago"]["lineas"][0]["valor"] == 900000
    assert _creditos(app).consultas_de_ciudad == [cliente_id]


async def test_el_siniestro_aparece_en_la_bandeja_del_admin_como_automatico(
    app, http, openapi_spec
):
    await _vendida(http)
    await app.state.sismos.procesar(_sismo())

    bandeja = (await http.get("/v1/siniestros", params={"automatico": "true"})).json()
    manuales = (await http.get("/v1/siniestros", params={"automatico": "false"})).json()

    assert [s["tipo"] for s in bandeja] == ["SISMO_PARAMETRICO"] and manuales == []
    interno = (await http.get(f"/v1/siniestros/{bandeja[0]['id']}")).json()
    _validar(openapi_spec, "SiniestroInterno", interno)
    assert interno["decisiones"][0]["agente"] == "Pago automático por sismo"
    assert interno["decisiones"][0]["decision"] == "PAGAR"


async def test_el_cliente_recibe_el_aviso_te_pagamos_solos(tmp_path):
    aplicacion = create_app(_settings(tmp_path))
    await maybe_init(aplicacion.state.clientes)
    avisos = []

    class _Avisos:
        async def avisar(self, cliente_id, aviso, ruta=None):
            avisos.append((cliente_id, str(aviso), ruta))

    aplicacion.state.siniestros._avisos = _Avisos()
    async with AsyncClient(transport=ASGITransport(app=aplicacion), base_url="http://t") as http:
        cliente_id, poliza_id = await _vendida(http)

        await aplicacion.state.sismos.procesar(_sismo())

    assert avisos == [
        (cliente_id, "SINIESTRO_PAGO_AUTOMATICO", {"destino": "seguro", "id": poliza_id})
    ]


async def test_si_el_aviso_falla_el_pago_queda(app, http):
    class _Falla:
        async def avisar(self, *args, **kwargs):
            raise RuntimeError("push caído")

    app.state.siniestros._avisos = _Falla()
    cliente_id, _ = await _vendida(http)

    resumen = await app.state.sismos.procesar(_sismo())

    assert resumen.pagadas == 1
    assert (await _siniestros(http, cliente_id))[0]["estado"] == "PAGADO"


# --- quién no recibe nada ---


async def test_con_una_magnitud_bajo_el_umbral_no_se_crea_ningun_siniestro(app, http):
    cliente_id, _ = await _vendida(http)

    resumen = await app.state.sismos.procesar(_sismo(magnitud=5.4))

    assert resumen.bajo_umbral and resumen.evaluadas == 0
    assert (resumen.magnitud, resumen.umbral) == (5.4, 5.5)
    assert await _siniestros(http, cliente_id) == []


async def test_la_magnitud_igual_al_umbral_si_paga(app, http):
    await _vendida(http)

    assert (await app.state.sismos.procesar(_sismo(magnitud=5.5))).pagadas == 1


async def test_una_poliza_de_otro_municipio_no_recibe_nada(app, http):
    cliente_id, _ = await _vendida(http, municipio="Medellín")

    resumen = await app.state.sismos.procesar(_sismo())

    assert resumen.pagadas == 0 and resumen.resultados["OTRO_MUNICIPIO"] == 1
    assert await _siniestros(http, cliente_id) == []


async def test_una_poliza_sin_tu_casa_no_recibe_nada(app, http):
    cliente_id, _ = await _vendida(http, coberturas=["VIDA"])

    resumen = await app.state.sismos.procesar(_sismo())

    assert resumen.evaluadas == 0
    assert await _siniestros(http, cliente_id) == []


async def test_una_poliza_fuera_de_vigencia_en_la_fecha_del_sismo_no_recibe_nada(app, http):
    cliente_id, _ = await _vendida(http)

    ayer = await app.state.sismos.procesar(_sismo(fecha=HOY - timedelta(days=1)))
    dentro_de_30_anios = await app.state.sismos.procesar(_sismo(fecha=HOY + timedelta(days=11000)))

    assert ayer.evaluadas == 0 and dentro_de_30_anios.evaluadas == 0
    assert await _siniestros(http, cliente_id) == []


async def test_una_poliza_desactivada_no_recibe_nada(app, http, tmp_path):
    cliente_id, poliza_id = await _vendida(http)
    async with aiosqlite.connect(str(tmp_path / "venta.db")) as conn:
        await conn.execute("UPDATE polizas SET estado = 'DESACTIVADA' WHERE id = ?", (poliza_id,))
        await conn.commit()

    resumen = await app.state.sismos.procesar(_sismo())

    assert resumen.evaluadas == 0
    assert await _siniestros(http, cliente_id) == []


# --- lo que no se sabe omite esa póliza y las demás siguen ---


async def test_sin_municipio_conocido_se_omite_con_su_motivo_y_las_demas_siguen(app, http, caplog):
    sin_dato, _ = await _poliza_emitida(app, http)  # el perfil no sabe su ciudad
    con_dato, _ = await _vendida(http)

    with caplog.at_level(logging.INFO, logger="svc_core.sismos"):
        resumen = await app.state.sismos.procesar(_sismo())

    assert resumen.pagadas == 1 and resumen.motivos_de_omision == {"MUNICIPIO_DESCONOCIDO": 1}
    assert await _siniestros(http, sin_dato) == []
    assert len(await _siniestros(http, con_dato)) == 1
    assert "resultado=OMITIDA motivo=MUNICIPIO_DESCONOCIDO" in caplog.text


@pytest.mark.parametrize(
    "creditos",
    [
        CreditosReportados("NO_DISPONIBLE"),
        CreditosReportados("SIN_CONSENTIMIENTO"),
        CreditosReportados("DISPONIBLE", ()),
        CreditosReportados(
            "DISPONIBLE", (HipotecaReportada("Davivienda", Decimal("1"), Decimal("500000")),)
        ),
        CreditosReportados("DISPONIBLE", (HipotecaReportada("Bancolombia", Decimal("1"), None),)),
    ],
)
async def test_sin_cuota_conocida_se_omite_con_su_motivo(app, http, creditos):
    cliente_id, _ = await _poliza_emitida(app, http)
    _creditos(app).ciudades[cliente_id] = "Bogotá"
    _creditos(app).respuesta = creditos

    resumen = await app.state.sismos.procesar(_sismo())

    assert resumen.pagadas == 0 and resumen.motivos_de_omision == {"CUOTA_DESCONOCIDA": 1}
    assert await _siniestros(http, cliente_id) == []


async def test_una_poliza_que_falla_no_frena_a_las_demas(app, http, caplog):
    malo, _ = await _poliza_emitida(app, http)
    bueno, _ = await _vendida(http)

    async def _explota(cliente_id):
        raise RuntimeError("perfilamiento se cayó con datos personales de Ana")

    _creditos(app).ciudad = _explota
    with caplog.at_level(logging.INFO, logger="svc_core.sismos"):
        resumen = await app.state.sismos.procesar(_sismo())

    assert resumen.pagadas == 1
    assert resumen.motivos_de_omision == {"ERROR_RuntimeError": 1}
    assert await _siniestros(http, malo) == [] and len(await _siniestros(http, bueno)) == 1
    assert "datos personales de Ana" not in caplog.text


# --- no se paga dos veces ---


async def test_el_mismo_sismo_dos_veces_paga_una_sola_vez_cada_poliza(app, http):
    cliente_id, _ = await _vendida(http)

    primera = await app.state.sismos.procesar(_sismo())
    segunda = await app.state.sismos.procesar(_sismo())

    assert primera.pagadas == 1 and segunda.pagadas == 0
    assert segunda.resultados["DUPLICADA"] == 1
    assert len(await _siniestros(http, cliente_id)) == 1


async def test_el_mismo_sismo_entregado_a_la_vez_paga_una_sola_vez(app, http):
    cliente_id, _ = await _vendida(http)

    resumenes = await asyncio.gather(*(app.state.sismos.procesar(_sismo()) for _ in range(5)))

    assert sum(r.pagadas for r in resumenes) == 1
    assert len(await _siniestros(http, cliente_id)) == 1


async def test_otro_sismo_sobre_la_misma_poliza_paga_otra_cuota(app, http):
    cliente_id, _ = await _vendida(http)

    await app.state.sismos.procesar(_sismo(evento_id="sismo-1"))
    await app.state.sismos.procesar(_sismo(evento_id="sismo-2"))

    assert len(await _siniestros(http, cliente_id)) == 2


async def test_los_pagos_del_sismo_no_chocan_con_un_siniestro_en_curso(app, http):
    """Un siniestro de Tu casa en curso no impide el pago automático: es otro tipo."""
    cliente_id, poliza_id = await _poliza_emitida(app, http)
    from tests.integration.test_siniestros_api import _incendio, _reportar

    assert (await _reportar(http, cliente_id, _incendio(poliza_id))).status_code == 201
    _creditos(app).ciudades[cliente_id] = "Bogotá"
    _creditos(app).respuesta = _hipotecas()

    resumen = await app.state.sismos.procesar(_sismo())

    assert resumen.pagadas == 1
    assert len(await _siniestros(http, cliente_id)) == 2


# --- el aviso: POST /v1/eventos-sismicos ---

CUERPO = {"eventoId": "sismo-1", "municipio": "Bogotá", "magnitud": 6.1, "fecha": HOY.isoformat()}


async def test_el_aviso_responde_202_y_publica_sismo_reportado(app, http, openapi_spec):
    respuesta = await http.post("/v1/eventos-sismicos", json=CUERPO)

    assert respuesta.status_code == 202
    _validar(openapi_spec, "SismoAceptado", respuesta.json())
    assert respuesta.json() == {"eventoId": "sismo-1"}
    [evento] = [e for e in app.state.events.events if e.tipo == "SismoReportado"]
    assert evento.datos == {
        "eventoId": "sismo-1",
        "municipio": "Bogotá",
        "magnitud": 6.1,
        "fecha": HOY.isoformat(),
    }


async def test_el_aviso_no_paga_en_la_misma_peticion(app, http):
    cliente_id, _ = await _vendida(http)

    await http.post("/v1/eventos-sismicos", json=CUERPO)

    assert await _siniestros(http, cliente_id) == []  # lo hace el consumidor


@pytest.mark.parametrize(
    "cambios",
    [
        {"eventoId": ""},
        {"eventoId": "con espacios"},
        {"eventoId": "x" * 65},
        {"municipio": ""},
        {"magnitud": -0.1},
        {"magnitud": 10.1},
        {"magnitud": "fuerte"},
        {"fecha": "ayer"},
        {"fecha": (HOY + timedelta(days=1)).isoformat()},
        {"sobra": 1},
    ],
)
async def test_un_aviso_invalido_es_400_y_no_publica_nada(app, http, cambios):
    respuesta = await http.post("/v1/eventos-sismicos", json={**CUERPO, **cambios})

    assert respuesta.status_code == 400
    assert not [e for e in app.state.events.events if e.tipo == "SismoReportado"]


@pytest.mark.parametrize("campo", ["eventoId", "municipio", "magnitud", "fecha"])
async def test_un_aviso_sin_un_campo_es_400(http, campo):
    cuerpo = {k: v for k, v in CUERPO.items() if k != campo}

    assert (await http.post("/v1/eventos-sismicos", json=cuerpo)).status_code == 400


# --- el consumidor ---


async def test_el_consumidor_despacha_sismo_reportado_y_paga(app, http):
    cliente_id, _ = await _vendida(http)
    evento = DomainEvent("SismoReportado", {**CUERPO, "magnitud": 6.1})

    await despachar_evento(app.state.polizas, evento, app.state.sismos)

    assert len(await _siniestros(http, cliente_id)) == 1


def test_el_consumidor_declara_que_consume_sismo_reportado():
    assert "SismoReportado" in TIPOS_QUE_CONSUME


@pytest.mark.parametrize(
    "datos",
    [
        {"municipio": "Bogotá", "magnitud": 6, "fecha": HOY.isoformat()},
        {**CUERPO, "magnitud": "mucha"},
        {**CUERPO, "fecha": "no-es-fecha"},
        {**CUERPO, "magnitud": None},
    ],
)
async def test_un_sismo_mal_formado_se_descarta_sin_lanzar(app, http, datos, caplog):
    cliente_id, _ = await _vendida(http)

    with caplog.at_level(logging.ERROR, logger="svc_core.consumidores"):
        await despachar_evento(
            app.state.polizas, DomainEvent("SismoReportado", datos), app.state.sismos
        )

    assert "SismoReportado inválido" in caplog.text
    assert await _siniestros(http, cliente_id) == []


async def test_sin_servicio_de_sismos_el_evento_se_descarta_sin_lanzar(app, caplog):
    with caplog.at_level(logging.ERROR, logger="svc_core.consumidores"):
        await despachar_evento(app.state.polizas, DomainEvent("SismoReportado", CUERPO), None)

    assert "sin el servicio de sismos" in caplog.text


async def test_el_log_trae_una_linea_resumen_con_la_magnitud_y_el_umbral(app, http, caplog):
    await _vendida(http)

    with caplog.at_level(logging.INFO, logger="svc_core.sismos"):
        await app.state.sismos.procesar(_sismo(magnitud=5.0))
        await app.state.sismos.procesar(_sismo(evento_id="sismo-2"))

    assert "bajo_umbral=True" in caplog.text and "magnitud=5.0 umbral=5.5" in caplog.text
    assert "evaluadas=1 pagadas=1" in caplog.text


async def test_recorre_todas_las_paginas_de_polizas(app, http, monkeypatch):
    from app import sismos

    monkeypatch.setattr(sismos, "TAMANO_PAGINA", 2)
    clientes = [(await _vendida(http, n))[0] for n in (1, 2, 3, 4, 5)]

    resumen = await app.state.sismos.procesar(_sismo())

    assert resumen.pagadas == 5
    for cliente_id in clientes:
        assert len(await _siniestros(http, cliente_id)) == 1
