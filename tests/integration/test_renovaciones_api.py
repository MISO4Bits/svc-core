"""BITS-387: la renovación de punta a punta por la API, validada contra el contrato OpenAPI."""

from __future__ import annotations

from datetime import timedelta

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from jsonschema import Draft202012Validator

from app.adapters.factory import maybe_init
from app.api.app import create_app
from app.domain import ZONA_COLOMBIA
from tests.integration.test_siniestros_api import _poliza_emitida
from tests.integration.test_venta_directa_api import _settings


def _validar(spec: dict, ref: str, instancia) -> None:
    schema = {"$ref": f"#/components/schemas/{ref}", "components": spec["components"]}
    errores = sorted(Draft202012Validator(schema).iter_errors(instancia), key=str)
    assert not errores, [e.message for e in errores]


@pytest_asyncio.fixture
async def app(tmp_path):
    aplicacion = create_app(_settings(tmp_path, environment="dev"))
    await maybe_init(aplicacion.state.clientes)
    return aplicacion


@pytest_asyncio.fixture
async def http(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as cliente:
        yield cliente


async def _aniversario(app, http, cliente_id, poliza_id):
    """El día de Colombia del aniversario de la póliza."""
    poliza = (await http.get(f"/v1/clientes/{cliente_id}/polizas/{poliza_id}")).json()
    from datetime import datetime

    return datetime.fromisoformat(poliza["vigencia"]["hasta"]).astimezone(ZONA_COLOMBIA).date()


async def _ejecutar(http, fecha):
    respuesta = await http.post("/v1/renovaciones/ejecutar", json={"fechaCorte": fecha.isoformat()})
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


async def test_la_poliza_recien_emitida_aun_no_tiene_renovacion(app, http, openapi_spec):
    cliente_id, poliza_id = await _poliza_emitida(app, http)

    poliza = (await http.get(f"/v1/clientes/{cliente_id}/polizas/{poliza_id}")).json()

    _validar(openapi_spec, "Poliza", poliza)
    assert poliza["renovacion"] is None


async def test_el_cliente_rechaza_y_la_poliza_pasa_por_las_tres_pasadas(app, http, openapi_spec):
    cliente_id, poliza_id = await _poliza_emitida(app, http)
    aniversario = await _aniversario(app, http, cliente_id, poliza_id)
    ruta = f"/v1/clientes/{cliente_id}/polizas/{poliza_id}"

    assert (await _ejecutar(http, aniversario - timedelta(days=45)))["avisosVencimiento"] == 1
    propuesta = (await http.get(ruta)).json()
    _validar(openapi_spec, "Poliza", propuesta)
    renovacion = propuesta["renovacion"]
    assert renovacion["estado"] == "SIN_RESPUESTA"
    assert renovacion["ipc"] == 0.05
    assert renovacion["primaActual"] == 42368
    assert renovacion["primaNueva"] == 44487
    assert renovacion["avisoBanco"] is None
    assert propuesta["primaTotalMensual"] == 42368  # la propuesta no cambia la prima vigente

    decidida = await http.post(f"{ruta}/renovacion", json={"decision": "RECHAZAR"})
    assert decidida.status_code == 200
    _validar(openapi_spec, "Poliza", decidida.json())
    assert decidida.json()["renovacion"]["estado"] == "RECHAZADA"

    assert (await _ejecutar(http, aniversario - timedelta(days=30)))["avisosBanco"] == 1
    cerrada = (await http.get(ruta)).json()["renovacion"]
    assert cerrada["avisoBanco"]["tipo"] == "NO_RENOVACION"
    assert cerrada["avisoBanco"]["canal"] == "SIMULADO"
    assert cerrada["avisoBanco"]["entidadAcreedora"] == "Bancolombia"
    tarde = await http.post(f"{ruta}/renovacion", json={"decision": "ACEPTAR"})
    assert tarde.status_code == 409
    assert tarde.json()["codigo"] == "PLAZO_VENCIDO"

    assert (await _ejecutar(http, aniversario))["desactivadas"] == 1
    final = (await http.get(ruta)).json()
    _validar(openapi_spec, "Poliza", final)
    assert final["estado"] == "DESACTIVADA"
    assert final["renovacion"]["avisoBanco"] is not None


async def test_sin_respuesta_la_poliza_se_renueva_con_la_prima_nueva(app, http, openapi_spec):
    cliente_id, poliza_id = await _poliza_emitida(app, http)
    aniversario = await _aniversario(app, http, cliente_id, poliza_id)
    ruta = f"/v1/clientes/{cliente_id}/polizas/{poliza_id}"
    antes = (await http.get(ruta)).json()

    await _ejecutar(http, aniversario - timedelta(days=45))
    resumen = await _ejecutar(http, aniversario)

    assert resumen["renovadas"] == 1
    despues = (await http.get(ruta)).json()
    _validar(openapi_spec, "Poliza", despues)
    assert despues["estado"] == "EMITIDA"
    assert despues["vigencia"]["desde"] == antes["vigencia"]["hasta"]
    assert despues["primaTotalMensual"] == 44487
    assert despues["emitidaEn"] == antes["emitidaEn"]
    assert despues["renovacion"] is None  # el ciclo nuevo empieza de cero
    assert (await _ejecutar(http, aniversario))["renovadas"] == 0  # dos veces, una renovación


async def test_decidir_sin_propuesta_o_sobre_algo_ajeno(app, http, openapi_spec):
    cliente_id, poliza_id = await _poliza_emitida(app, http)
    ruta = f"/v1/clientes/{cliente_id}/polizas/{poliza_id}/renovacion"

    sin_propuesta = await http.post(ruta, json={"decision": "ACEPTAR"})
    assert sin_propuesta.status_code == 409
    assert sin_propuesta.json()["codigo"] == "SIN_PROPUESTA"
    _validar(openapi_spec, "ProblemaRenovacion", sin_propuesta.json())

    ajena = await http.post(
        f"/v1/clientes/otro-cliente/polizas/{poliza_id}/renovacion", json={"decision": "ACEPTAR"}
    )
    assert ajena.status_code == 404
    invalida = await http.post(ruta, json={"decision": "TAL_VEZ"})
    assert invalida.status_code == 400


async def test_la_lista_del_cliente_tambien_trae_la_renovacion(app, http, openapi_spec):
    cliente_id, poliza_id = await _poliza_emitida(app, http)
    aniversario = await _aniversario(app, http, cliente_id, poliza_id)
    await _ejecutar(http, aniversario - timedelta(days=45))

    [poliza] = (await http.get(f"/v1/clientes/{cliente_id}/polizas")).json()

    assert poliza["renovacion"]["estado"] == "SIN_RESPUESTA"


async def test_el_trabajo_sin_cuerpo_corre_con_la_fecha_de_hoy(app, http, openapi_spec):
    await _poliza_emitida(app, http)

    respuesta = await http.post("/v1/renovaciones/ejecutar")

    assert respuesta.status_code == 200
    _validar(openapi_spec, "EjecucionRenovaciones", respuesta.json())
    assert respuesta.json()["renovadas"] == 0


async def test_la_fecha_de_corte_simulada_solo_se_acepta_en_dev(tmp_path, openapi_spec):
    aplicacion = create_app(_settings(tmp_path, environment="prod"))
    await maybe_init(aplicacion.state.clientes)
    async with AsyncClient(transport=ASGITransport(app=aplicacion), base_url="http://t") as http:
        simulada = await http.post("/v1/renovaciones/ejecutar", json={"fechaCorte": "2027-11-13"})
        real = await http.post("/v1/renovaciones/ejecutar")

    assert simulada.status_code == 400
    assert real.status_code == 200
