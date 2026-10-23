"""La selfie por la API (BITS-235), con el verificador biométrico de pruebas."""

from __future__ import annotations

from contextlib import asynccontextmanager

from httpx import ASGITransport, AsyncClient

from app.adapters.factory import maybe_init
from app.api.app import create_app
from tests import imagenes
from tests.conftest import CLIENTE_VALIDO


@asynccontextmanager
async def _http(settings, **cambios):
    aplicacion = create_app(settings.model_copy(update=cambios))
    await maybe_init(aplicacion.state.clientes)
    async with AsyncClient(transport=ASGITransport(app=aplicacion), base_url="http://t") as http:
        yield http


async def _registrar(http) -> str:
    respuesta = await http.post("/v1/clientes", json=CLIENTE_VALIDO)
    assert respuesta.status_code == 201
    return respuesta.json()["id"]


def _subir(http, cliente_id, tipo):
    return http.post(
        f"/v1/clientes/{cliente_id}/evidencias-identidad",
        data={"tipo": tipo},
        files={"imagen": ("foto.jpg", imagenes.a_bytes(imagenes.sin_tarjeta()), "image/jpeg")},
    )


async def _subir_cedula(http, cliente_id) -> None:
    for tipo in ("cedula_frente", "cedula_reverso"):
        assert (await _subir(http, cliente_id, tipo)).status_code == 201


async def test_la_selfie_sin_la_cedula_responde_422(client):
    cliente_id = await _registrar(client)

    respuesta = await _subir(client, cliente_id, "selfie")

    assert respuesta.status_code == 422
    assert respuesta.headers["content-type"].startswith("application/problem+json")
    assert "cédula" in respuesta.json()["detail"]
    estado = await client.get(f"/v1/clientes/{cliente_id}/evidencias-identidad")
    selfie = next(e for e in estado.json() if e["tipo"] == "selfie")
    assert selfie["intentosRestantes"] == 3  # el 422 no cuenta como intento


async def test_la_selfie_aceptada_responde_201_y_no_expone_puntajes(client):
    cliente_id = await _registrar(client)
    await _subir_cedula(client, cliente_id)

    respuesta = await _subir(client, cliente_id, "selfie")
    estado = await client.get(f"/v1/clientes/{cliente_id}/evidencias-identidad")

    assert respuesta.status_code == 201
    assert set(respuesta.json()) == {"evidenciaId", "tipo", "estado", "motivo", "intentosRestantes"}
    assert respuesta.json()["estado"] == "ACEPTADA"
    selfie = next(e for e in estado.json() if e["tipo"] == "selfie")
    assert selfie["estado"] == "ACEPTADA"
    assert not {"puntajeVida", "puntajeCoincidencia"} & set(selfie)


async def test_cada_motivo_nuevo_llega_al_cliente(settings):
    motivos = ["SIN_ROSTRO", "VARIOS_ROSTROS", "POSIBLE_SUPLANTACION", "NO_COINCIDE_CON_CEDULA"]
    async with _http(settings, biometria_mock_secuencia=",".join(motivos)) as http:
        cliente_id = await _registrar(http)
        await _subir_cedula(http, cliente_id)

        recibidos = []
        for _ in range(2):  # el tercer rechazo ya pasaría a revisión manual
            respuesta = await _subir(http, cliente_id, "selfie")
            assert respuesta.status_code == 200
            recibidos.append(respuesta.json()["motivo"])

    assert recibidos == motivos[:2]


async def test_la_tercera_selfie_rechazada_queda_en_revision_manual(settings):
    async with _http(settings, biometria_mock_secuencia="NO_COINCIDE_CON_CEDULA") as http:
        cliente_id = await _registrar(http)
        await _subir_cedula(http, cliente_id)

        await _subir(http, cliente_id, "selfie")
        await _subir(http, cliente_id, "selfie")
        tercera = await _subir(http, cliente_id, "selfie")

    assert tercera.status_code == 201
    assert tercera.json()["estado"] == "REVISION_MANUAL"
