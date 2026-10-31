"""BITS-229: registro de dispositivos por la API y aviso push al emitirse la póliza."""

from __future__ import annotations

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from jsonschema import Draft202012Validator

from app.adapters.factory import maybe_init
from app.adapters.polizas_fake import cotizacion_de_ejemplo
from app.api.app import create_app
from app.config import Settings
from tests import imagenes
from tests.conftest import CLIENTE_VALIDO

TOKEN = "fcm-token-de-prueba-0001"
DOCS = [{"tipo": "poliza", "version": "V1"}]


def _validar(spec: dict, ref: str, instancia) -> None:
    schema = {"$ref": f"#/components/schemas/{ref}", "components": spec["components"]}
    errores = sorted(Draft202012Validator(schema).iter_errors(instancia), key=str)
    assert not errores, [e.message for e in errores]


def _cuerpo(token=TOKEN, **cambios) -> dict:
    return {"token": token, "plataforma": "android", "versionApp": "1.0.0", **cambios}


async def _registrar_cliente(http, **cambios) -> str:
    respuesta = await http.post("/v1/clientes", json={**CLIENTE_VALIDO, **cambios})
    assert respuesta.status_code == 201
    return respuesta.json()["id"]


async def _registrar_dispositivo(http, cliente_id, token=TOKEN):
    return await http.put(f"/v1/clientes/{cliente_id}/dispositivos", json=_cuerpo(token))


# --- API ---


async def test_registrar_devuelve_el_id_y_cumple_el_contrato(client, openapi_spec):
    cliente_id = await _registrar_cliente(client)

    respuesta = await _registrar_dispositivo(client, cliente_id)

    assert respuesta.status_code == 200
    _validar(openapi_spec, "Dispositivo", respuesta.json())


async def test_registrar_dos_veces_el_mismo_token_devuelve_el_mismo_id(client):
    cliente_id = await _registrar_cliente(client)

    primero = await _registrar_dispositivo(client, cliente_id)
    segundo = await _registrar_dispositivo(client, cliente_id)

    assert primero.json() == segundo.json()


async def test_cliente_inexistente_es_404(client):
    respuesta = await _registrar_dispositivo(client, "no-existe")

    assert respuesta.status_code == 404


async def test_cuerpo_invalido_es_400(client):
    cliente_id = await _registrar_cliente(client)
    url = f"/v1/clientes/{cliente_id}/dispositivos"

    assert (await client.put(url, json=_cuerpo(plataforma="ios"))).status_code == 400
    assert (await client.put(url, json=_cuerpo(token="con espacios dentro"))).status_code == 400
    assert (await client.put(url, json=_cuerpo(token="corto"))).status_code == 400
    assert (await client.put(url, json={"token": TOKEN})).status_code == 400
    assert (await client.put(url, json=_cuerpo(otro="x"))).status_code == 400


async def test_quitar_es_204_y_repetirlo_tambien(client, app):
    cliente_id = await _registrar_cliente(client)
    dispositivo_id = (await _registrar_dispositivo(client, cliente_id)).json()["dispositivoId"]
    url = f"/v1/clientes/{cliente_id}/dispositivos/{dispositivo_id}"

    primero = await client.delete(url)
    segundo = await client.delete(url)

    assert (primero.status_code, segundo.status_code) == (204, 204)
    assert await app.state.dispositivos._dispositivos.de_cliente(cliente_id) == []


async def test_un_cliente_no_puede_quitar_el_dispositivo_de_otro(client, app):
    ana = await _registrar_cliente(client)
    luis = await _registrar_cliente(
        client,
        identityRef="idp-sub-0002",
        numeroDocumento="1032456790",
        email="luis@example.com",
    )
    de_ana = (await _registrar_dispositivo(client, ana)).json()["dispositivoId"]

    respuesta = await client.delete(f"/v1/clientes/{luis}/dispositivos/{de_ana}")

    assert respuesta.status_code == 204
    assert len(await app.state.dispositivos._dispositivos.de_cliente(ana)) == 1


# --- aviso de póliza emitida ---


@pytest_asyncio.fixture
async def con_push(tmp_path):
    settings = Settings(
        repository_backend="sqlite",
        database_path=str(tmp_path / "push.db"),
        event_backend="memory",
        almacen_backend="memory",
        push_backend="on",
    )
    app = create_app(settings)
    await maybe_init(app.state.clientes)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield app, http


async def _solicitar_emitida(app, http, cliente_id):
    await http.post(f"/v1/clientes/{cliente_id}/confirmacion")
    for tipo in ("cedula_frente", "cedula_reverso", "selfie"):
        respuesta = await http.post(
            f"/v1/clientes/{cliente_id}/evidencias-identidad",
            data={"tipo": tipo},
            files={"imagen": ("foto.jpg", imagenes.a_bytes(imagenes.sin_tarjeta()), "image/jpeg")},
        )
        assert respuesta.status_code == 201
    cotizacion = cotizacion_de_ejemplo()
    app.state.polizas._cotizaciones.poner(cliente_id, cotizacion)
    return await http.post(
        f"/v1/clientes/{cliente_id}/polizas",
        json={
            "coberturas": ["VIDA"],
            "cotizacionCreadaEn": cotizacion.creada_en.isoformat(),
            "documentosAceptados": DOCS,
        },
    )


def _pushes(app) -> list:
    return [e for e in app.state.events.events if e.tipo == "EnviarPush"]


async def test_al_emitirse_la_poliza_se_avisa_a_los_dispositivos_del_cliente(con_push):
    app, http = con_push
    cliente_id = await _registrar_cliente(http)
    await _registrar_dispositivo(http, cliente_id, "fcm-token-telefono-1")
    await _registrar_dispositivo(http, cliente_id, "fcm-token-telefono-2")

    respuesta = await _solicitar_emitida(app, http, cliente_id)

    assert respuesta.json()["estado"] == "EMITIDA"
    [aviso] = _pushes(app)
    assert aviso.datos["clienteId"] == cliente_id
    assert sorted(aviso.datos["tokens"]) == ["fcm-token-telefono-1", "fcm-token-telefono-2"]
    assert aviso.datos["ruta"] == {"destino": "seguro", "id": respuesta.json()["polizaId"]}
    assert aviso.datos["titulo"] == "Tu póliza ya está activa"
    assert aviso.datos["prioridad"] == "alta"
    # El aviso no sustituye al documento: PolizaEmitida sigue saliendo.
    assert [e.tipo for e in app.state.events.events].count("PolizaEmitida") == 1


async def test_sin_dispositivos_la_emision_no_publica_aviso(con_push):
    app, http = con_push
    cliente_id = await _registrar_cliente(http)

    respuesta = await _solicitar_emitida(app, http, cliente_id)

    assert respuesta.json()["estado"] == "EMITIDA"
    assert _pushes(app) == []


async def test_un_dispositivo_quitado_ya_no_recibe_el_aviso(con_push):
    app, http = con_push
    cliente_id = await _registrar_cliente(http)
    dispositivo_id = (await _registrar_dispositivo(http, cliente_id)).json()["dispositivoId"]
    await http.delete(f"/v1/clientes/{cliente_id}/dispositivos/{dispositivo_id}")

    await _solicitar_emitida(app, http, cliente_id)

    assert _pushes(app) == []


async def test_un_fallo_al_publicar_el_aviso_no_impide_la_emision(con_push):
    app, http = con_push
    cliente_id = await _registrar_cliente(http)
    await _registrar_dispositivo(http, cliente_id)
    publicar = app.state.events.publish

    async def publicar_o_fallar(evento):
        if evento.tipo == "EnviarPush":
            raise RuntimeError("Pub/Sub no responde")
        await publicar(evento)

    app.state.events.publish = publicar_o_fallar

    respuesta = await _solicitar_emitida(app, http, cliente_id)

    assert respuesta.status_code == 201
    assert respuesta.json()["estado"] == "EMITIDA"
    assert _pushes(app) == []


async def test_con_el_aviso_apagado_no_se_publica_nada(app, client):
    cliente_id = await _registrar_cliente(client)
    await _registrar_dispositivo(client, cliente_id)

    await _solicitar_emitida(app, client, cliente_id)

    assert _pushes(app) == []
