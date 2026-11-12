"""BITS-385: ``POST /v1/clientes/{clienteId}/siniestros/{id}/asistencias`` de punta a punta."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime

import pytest
from jsonschema import Draft202012Validator

from app.adapters.memory import InMemorySiniestroRepository
from app.domain import (
    Asistencia,
    EstadoSiniestro,
    OrigenAsistencia,
    PrestadorAsistencia,
)
from tests.integration.test_siniestros_estados_api import (
    _pedir_evidencia,
    _radicar,
    _ruta,
)

PRESTADOR = {
    "id": "pr-001",
    "nombre": "Hogar Reparaciones Cedritos",
    "direccion": "Calle 123 # 1-87",
    "tiempoLlegadaMin": 20,
}
DESDE_FOTOS = {
    "tipo": "plomeria",
    "prestador": PRESTADOR,
    "origen": "FOTOS",
    "latitud": 4.65,
    "longitud": -74.06,
}
DESDE_DIRECCION = {
    "tipo": "electricidad",
    "prestador": PRESTADOR,
    "origen": "DIRECCION",
    "direccion": "Carrera 7 # 71-21",
    "localidad": "chapinero",
}
AGENTE = "admin@solventa4bits.com"


def _validar(spec: dict, ref: str, instancia) -> None:
    schema = {"$ref": f"#/components/schemas/{ref}", "components": spec["components"]}
    errores = sorted(Draft202012Validator(schema).iter_errors(instancia), key=str)
    assert not errores, [e.message for e in errores]


def _pedir(client, cliente_id, siniestro_id, cuerpo=None, llave=None):
    return client.post(
        _ruta(cliente_id, siniestro_id, "asistencias"),
        json=cuerpo if cuerpo is not None else DESDE_FOTOS,
        headers={"Idempotency-Key": llave} if llave else {},
    )


async def _decidir(client, siniestro_id: str, cuerpo: dict):
    return await client.post(
        f"/v1/siniestros/{siniestro_id}/decisiones", json={"agente": AGENTE, **cuerpo}
    )


# --- solicitud exitosa ---


async def test_pedir_ayuda_responde_201_con_numero_prestador_y_cumple_el_contrato(
    app, client, openapi_spec
):
    cliente_id, _, siniestro_id = await _radicar(app, client, incendio=True)

    respuesta = await _pedir(client, cliente_id, siniestro_id)

    assert respuesta.status_code == 201
    cuerpo = respuesta.json()
    _validar(openapi_spec, "Asistencia", cuerpo)
    assert re.fullmatch(r"ASI-\d{4}-[A-Z0-9]{6}", cuerpo["numero"])
    assert cuerpo["prestador"] == PRESTADOR
    assert cuerpo["tiempoLlegadaMin"] == 20
    assert cuerpo["tipo"] == "plomeria"
    assert (cuerpo["origen"], cuerpo["latitud"], cuerpo["longitud"]) == ("FOTOS", 4.65, -74.06)
    assert cuerpo["direccion"] is None
    assert respuesta.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("origen", ["FOTOS", "TELEFONO"])
async def test_pedir_ayuda_desde_las_fotos_o_el_telefono_lleva_coordenadas(app, client, origen):
    cliente_id, _, siniestro_id = await _radicar(app, client, incendio=True)

    respuesta = await _pedir(client, cliente_id, siniestro_id, {**DESDE_FOTOS, "origen": origen})

    assert respuesta.status_code == 201
    assert respuesta.json()["origen"] == origen


async def test_pedir_ayuda_con_una_direccion_lleva_la_direccion_y_la_localidad(
    app, client, openapi_spec
):
    cliente_id, _, siniestro_id = await _radicar(app, client, incendio=True)

    respuesta = await _pedir(client, cliente_id, siniestro_id, DESDE_DIRECCION)

    assert respuesta.status_code == 201
    cuerpo = respuesta.json()
    _validar(openapi_spec, "Asistencia", cuerpo)
    assert (cuerpo["direccion"], cuerpo["localidad"]) == ("Carrera 7 # 71-21", "chapinero")
    assert cuerpo["latitud"] is None and cuerpo["longitud"] is None


async def test_el_detalle_trae_las_solicitudes_de_la_mas_antigua_a_la_mas_reciente(
    app, client, openapi_spec
):
    cliente_id, _, siniestro_id = await _radicar(app, client, incendio=True)
    primera = (await _pedir(client, cliente_id, siniestro_id)).json()
    segunda = (await _pedir(client, cliente_id, siniestro_id, DESDE_DIRECCION)).json()

    detalle = (await client.get(_ruta(cliente_id, siniestro_id))).json()

    _validar(openapi_spec, "Siniestro", detalle)
    assert [a["id"] for a in detalle["asistencias"]] == [primera["id"], segunda["id"]]
    assert detalle["asistencias"][0] == primera


async def test_un_siniestro_sin_solicitudes_trae_la_lista_vacia(app, client, openapi_spec):
    cliente_id, _, siniestro_id = await _radicar(app, client, incendio=True)

    detalle = (await client.get(_ruta(cliente_id, siniestro_id))).json()

    _validar(openapi_spec, "Siniestro", detalle)
    assert detalle["asistencias"] == []


async def test_el_detalle_del_admin_tambien_las_trae(app, client, openapi_spec):
    cliente_id, _, siniestro_id = await _radicar(app, client, incendio=True)
    await _pedir(client, cliente_id, siniestro_id)

    detalle = (await client.get(f"/v1/siniestros/{siniestro_id}")).json()

    _validar(openapi_spec, "SiniestroInterno", detalle)
    assert len(detalle["asistencias"]) == 1


async def test_pedir_ayuda_no_cambia_el_estado_ni_el_plazo(app, client):
    cliente_id, _, siniestro_id = await _radicar(app, client, incendio=True)
    antes = (await client.get(_ruta(cliente_id, siniestro_id))).json()

    await _pedir(client, cliente_id, siniestro_id)

    despues = (await client.get(_ruta(cliente_id, siniestro_id))).json()
    for campo in ("estado", "fechaLimite", "ultimaEntregaEn", "actualizadoEn"):
        assert despues[campo] == antes[campo]


async def test_tambien_se_puede_pedir_con_el_siniestro_en_falta_informacion(app, client):
    cliente_id, _, siniestro_id = await _radicar(app, client, incendio=True)
    await _pedir_evidencia(app, siniestro_id)

    respuesta = await _pedir(client, cliente_id, siniestro_id)

    assert respuesta.status_code == 201
    detalle = (await client.get(_ruta(cliente_id, siniestro_id))).json()
    assert detalle["estado"] == "FALTA_INFORMACION"
    assert len(detalle["asistencias"]) == 1


# --- idempotencia ---


async def test_repetir_la_solicitud_con_la_misma_llave_devuelve_la_misma(app, client):
    cliente_id, _, siniestro_id = await _radicar(app, client, incendio=True)

    primera = await _pedir(client, cliente_id, siniestro_id, llave="ayuda-1")
    repetida = await _pedir(client, cliente_id, siniestro_id, llave="ayuda-1")

    assert repetida.status_code == 201
    assert repetida.json() == primera.json()
    detalle = (await client.get(_ruta(cliente_id, siniestro_id))).json()
    assert len(detalle["asistencias"]) == 1


async def test_con_otra_llave_es_otra_solicitud(app, client):
    cliente_id, _, siniestro_id = await _radicar(app, client, incendio=True)

    primera = await _pedir(client, cliente_id, siniestro_id, llave="ayuda-1")
    otra = await _pedir(client, cliente_id, siniestro_id, llave="ayuda-2")

    assert otra.json()["id"] != primera.json()["id"]
    detalle = (await client.get(_ruta(cliente_id, siniestro_id))).json()
    assert len(detalle["asistencias"]) == 2


async def test_dos_solicitudes_al_mismo_tiempo_quedan_las_dos(app, client):
    cliente_id, _, siniestro_id = await _radicar(app, client, incendio=True)

    a, b = await asyncio.gather(
        _pedir(client, cliente_id, siniestro_id, llave="a"),
        _pedir(client, cliente_id, siniestro_id, DESDE_DIRECCION, llave="b"),
    )

    assert (a.status_code, b.status_code) == (201, 201)
    detalle = (await client.get(_ruta(cliente_id, siniestro_id))).json()
    assert {x["id"] for x in detalle["asistencias"]} == {a.json()["id"], b.json()["id"]}


# --- reglas ---


async def test_un_siniestro_de_incapacidad_total_no_tiene_ayuda(app, client):
    cliente_id, _, siniestro_id = await _radicar(app, client)

    respuesta = await _pedir(client, cliente_id, siniestro_id)

    assert respuesta.status_code == 409
    assert respuesta.json()["codigo"] == "AYUDA_NO_DISPONIBLE"


@pytest.mark.parametrize(
    "decision",
    [{"decision": "RECHAZAR", "motivo": "No está cubierto"}, {"decision": "PAGAR"}],
)
async def test_un_siniestro_pagado_o_rechazado_ya_no_admite_ayuda(app, client, decision):
    cliente_id, _, siniestro_id = await _radicar(app, client, incendio=True)
    if decision["decision"] == "PAGAR":
        decision = {**decision, "dano": "REPARABLE", "valor": 1000000}
    assert (await _decidir(client, siniestro_id, decision)).status_code == 200

    respuesta = await _pedir(client, cliente_id, siniestro_id)

    assert respuesta.status_code == 409
    assert respuesta.json()["codigo"] == "ESTADO_NO_PERMITE"


async def test_el_siniestro_de_otro_cliente_responde_404(app, client):
    _, _, siniestro_id = await _radicar(app, client, incendio=True)

    respuesta = await _pedir(client, "otro-cliente", siniestro_id)

    assert respuesta.status_code == 404


async def test_un_siniestro_que_no_existe_responde_404(app, client):
    cliente_id, _, _ = await _radicar(app, client, incendio=True)

    respuesta = await _pedir(client, cliente_id, "no-existe")

    assert respuesta.status_code == 404


@pytest.mark.parametrize(
    "cambios",
    [
        {"latitud": None, "longitud": None},
        {"longitud": None},
        {"direccion": "Calle 1"},
        {"localidad": "chapinero"},
        {"origen": "TELEFONO", "latitud": None},
        {"origen": "DIRECCION"},
        {"latitud": 91},
        {"longitud": -181},
        {"tipo": "Plomería"},
        {"tipo": ""},
        {"origen": "OTRO"},
        {"prestador": {**PRESTADOR, "id": ""}},
        {"prestador": {**PRESTADOR, "tiempoLlegadaMin": 0}},
        {"prestador": {"id": "pr-001"}},
        {"prestador": {**PRESTADOR, "sobra": 1}},
        {"agente": "alguien"},
    ],
)
async def test_un_cuerpo_incoherente_es_400_y_no_guarda_nada(app, client, cambios):
    cliente_id, _, siniestro_id = await _radicar(app, client, incendio=True)

    respuesta = await _pedir(client, cliente_id, siniestro_id, {**DESDE_FOTOS, **cambios})

    assert respuesta.status_code == 400
    detalle = (await client.get(_ruta(cliente_id, siniestro_id))).json()
    assert detalle["asistencias"] == []


async def test_con_origen_direccion_no_van_coordenadas(app, client):
    cliente_id, _, siniestro_id = await _radicar(app, client, incendio=True)

    respuesta = await _pedir(
        client, cliente_id, siniestro_id, {**DESDE_DIRECCION, "latitud": 4.6, "longitud": -74.0}
    )

    assert respuesta.status_code == 400


async def test_el_log_no_escribe_la_ubicacion_ni_la_direccion(app, client, caplog):
    cliente_id, _, siniestro_id = await _radicar(app, client, incendio=True)

    with caplog.at_level("INFO"):
        await _pedir(client, cliente_id, siniestro_id, DESDE_DIRECCION)
        await _pedir(client, cliente_id, siniestro_id, DESDE_FOTOS)

    assert "Carrera 7 # 71-21" not in caplog.text
    assert "4.65" not in caplog.text and "-74.06" not in caplog.text


# --- repositorio: la condición sobre el estado va en la misma operación ---


def _asistencia() -> Asistencia:
    return Asistencia(
        numero="ASI-2026-ABC234",
        tipo="plomeria",
        prestador=PrestadorAsistencia("pr-001", "Hogar", "Calle 1", 20),
        origen=OrigenAsistencia.FOTOS,
        solicitada_en=datetime.now(UTC),
        latitud=4.6,
        longitud=-74.0,
    )


async def test_el_repositorio_sqlite_no_agrega_si_el_siniestro_ya_se_cerro(app, client):
    cliente_id, _, siniestro_id = await _radicar(app, client, incendio=True)
    repositorio = app.state.siniestros._siniestros
    await _decidir(client, siniestro_id, {"decision": "RECHAZAR", "motivo": "No cubierto"})

    agregado = await repositorio.agregar_asistencia(siniestro_id, _asistencia())

    assert agregado is False
    guardado = await repositorio.obtener(siniestro_id)
    assert guardado.estado is EstadoSiniestro.RECHAZADO
    assert guardado.asistencias == ()


async def test_el_repositorio_sqlite_guarda_y_devuelve_la_asistencia_completa(app, client):
    _, _, siniestro_id = await _radicar(app, client, incendio=True)
    repositorio = app.state.siniestros._siniestros
    esperada = _asistencia()

    assert await repositorio.agregar_asistencia(siniestro_id, esperada) is True

    guardado = await repositorio.obtener(siniestro_id)
    assert guardado.asistencias == (esperada,)


async def test_el_repositorio_en_memoria_agrega_solo_a_un_siniestro_en_curso(app, client):
    _, _, siniestro_id = await _radicar(app, client, incendio=True)
    origen = await app.state.siniestros._siniestros.obtener(siniestro_id)
    memoria = InMemorySiniestroRepository()
    await memoria.crear(origen)

    assert await memoria.agregar_asistencia("no-existe", _asistencia()) is False
    assert await memoria.agregar_asistencia(origen.id, _asistencia()) is True
    assert len((await memoria.obtener(origen.id)).asistencias) == 1

    origen.rechazar("No cubierto", datetime.now(UTC), AGENTE)
    await memoria.actualizar(origen, EstadoSiniestro.EN_ESTUDIO)
    assert await memoria.agregar_asistencia(origen.id, _asistencia()) is False


async def test_si_el_siniestro_se_cierra_justo_antes_de_guardar_responde_409(
    app, client, monkeypatch
):
    cliente_id, _, siniestro_id = await _radicar(app, client, incendio=True)
    repositorio = app.state.siniestros._siniestros
    original = repositorio.agregar_asistencia

    async def cerrado_antes(id_siniestro, asistencia):
        await _decidir(client, id_siniestro, {"decision": "RECHAZAR", "motivo": "No cubierto"})
        return await original(id_siniestro, asistencia)

    monkeypatch.setattr(repositorio, "agregar_asistencia", cerrado_antes)

    respuesta = await _pedir(client, cliente_id, siniestro_id, llave="carrera")

    assert respuesta.status_code == 409
    assert respuesta.json()["codigo"] == "ESTADO_NO_PERMITE"
    detalle = (await client.get(_ruta(cliente_id, siniestro_id))).json()
    assert detalle["asistencias"] == []
