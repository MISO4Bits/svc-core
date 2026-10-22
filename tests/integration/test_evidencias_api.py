from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.factory import maybe_init
from app.api.app import create_app
from tests import imagenes
from tests.conftest import CLIENTE_VALIDO

CODIGO = b"PubDSK_1\x00\x00001032456789RIOS     ANA\x00"  # el número de CLIENTE_VALIDO


@asynccontextmanager
async def _http(settings, **cambios):
    aplicacion = create_app(settings.model_copy(update=cambios))
    await maybe_init(aplicacion.state.clientes)
    async with AsyncClient(transport=ASGITransport(app=aplicacion), base_url="http://t") as http:
        yield http


async def _registrar(http, **cambios) -> str:
    respuesta = await http.post("/v1/clientes", json={**CLIENTE_VALIDO, **cambios})
    assert respuesta.status_code == 201
    return respuesta.json()["id"]


def _subir(http, cliente_id, tipo, datos=None, nombre="foto.jpg"):
    datos = datos if datos is not None else imagenes.a_bytes(imagenes.sin_tarjeta())
    return http.post(
        f"/v1/clientes/{cliente_id}/evidencias-identidad",
        data={"tipo": tipo},
        files={"imagen": (nombre, datos, "image/jpeg")},
    )


async def test_una_foto_aceptada_responde_201_con_la_evidencia(client):
    cliente_id = await _registrar(client)

    respuesta = await _subir(client, cliente_id, "cedula_frente")

    assert respuesta.status_code == 201
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == "ACEPTADA"
    assert cuerpo["tipo"] == "cedula_frente"
    assert cuerpo["evidenciaId"]
    assert cuerpo["motivo"] is None
    assert cuerpo["intentosRestantes"] == 3
    assert "numeroDetectado" not in cuerpo


async def test_un_rechazo_responde_200_con_el_motivo_y_no_guarda_nada(settings):
    async with _http(settings, verificador_mock_secuencia="BORROSA") as http:
        cliente_id = await _registrar(http)

        respuesta = await _subir(http, cliente_id, "cedula_frente")
        estado = await http.get(f"/v1/clientes/{cliente_id}/evidencias-identidad")

    assert respuesta.status_code == 200
    assert respuesta.json() == {
        "evidenciaId": None,
        "tipo": "cedula_frente",
        "estado": "REINTENTAR",
        "motivo": "BORROSA",
        "intentosRestantes": 2,
    }
    frente = next(e for e in estado.json() if e["tipo"] == "cedula_frente")
    assert frente["estado"] == "PENDIENTE"
    assert frente["intentosRestantes"] == 2


async def test_la_tercera_foto_rechazada_queda_en_revision_manual(settings):
    async with _http(settings, verificador_mock_secuencia="REFLEJO") as http:
        cliente_id = await _registrar(http)

        primeras = [await _subir(http, cliente_id, "cedula_reverso") for _ in range(2)]
        tercera = await _subir(http, cliente_id, "cedula_reverso")
        estado = await http.get(f"/v1/clientes/{cliente_id}/evidencias-identidad")

    assert [r.status_code for r in primeras] == [200, 200]  # rechazos: nada se creó
    assert tercera.status_code == 201
    assert tercera.json()["estado"] == "REVISION_MANUAL"
    reverso = next(e for e in estado.json() if e["tipo"] == "cedula_reverso")
    assert reverso["estado"] == "REVISION_MANUAL"
    assert reverso["evidenciaId"] == tercera.json()["evidenciaId"]


async def test_el_estado_lista_los_tres_tipos_y_refleja_lo_subido(client):
    cliente_id = await _registrar(client)
    ruta = f"/v1/clientes/{cliente_id}/evidencias-identidad"

    antes = (await client.get(ruta)).json()
    await _subir(client, cliente_id, "cedula_frente")
    despues = {e["tipo"]: e for e in (await client.get(ruta)).json()}

    assert [e["tipo"] for e in antes] == ["cedula_frente", "cedula_reverso", "selfie"]
    assert {e["estado"] for e in antes} == {"PENDIENTE"}
    assert despues["cedula_frente"]["estado"] == "ACEPTADA"
    assert despues["cedula_frente"]["creadaEn"]
    assert despues["cedula_reverso"]["estado"] == "PENDIENTE"


async def test_un_cliente_inexistente_responde_404(client):
    subida = await _subir(client, "00000000-0000-0000-0000-000000000000", "cedula_frente")
    estado = await client.get(
        "/v1/clientes/00000000-0000-0000-0000-000000000000/evidencias-identidad"
    )

    assert subida.status_code == estado.status_code == 404
    assert subida.headers["content-type"].startswith("application/problem+json")


async def test_un_tipo_fuera_de_la_lista_responde_400(client):
    cliente_id = await _registrar(client)

    respuesta = await _subir(client, cliente_id, "pasaporte")

    assert respuesta.status_code == 400
    assert any("tipo" in e["campo"] for e in respuesta.json()["errores"])


async def test_sin_tipo_o_sin_imagen_responde_400(client):
    cliente_id = await _registrar(client)
    ruta = f"/v1/clientes/{cliente_id}/evidencias-identidad"
    foto = ("foto.jpg", imagenes.a_bytes(imagenes.sin_tarjeta()), "image/jpeg")

    sin_tipo = await client.post(ruta, files={"imagen": foto})
    sin_imagen = await client.post(ruta, data={"tipo": "selfie"})

    assert sin_tipo.status_code == sin_imagen.status_code == 400


async def test_un_archivo_que_no_es_imagen_responde_400(client):
    cliente_id = await _registrar(client)

    respuesta = await _subir(client, cliente_id, "cedula_frente", b"%PDF-1.7 no es imagen", "x.jpg")

    assert respuesta.status_code == 400
    assert respuesta.json()["title"] == "Imagen inválida"


async def test_una_imagen_mas_grande_que_el_limite_responde_413(settings):
    async with _http(settings, evidencia_max_bytes=1000) as http:
        cliente_id = await _registrar(http)

        respuesta = await _subir(http, cliente_id, "cedula_frente")

    assert respuesta.status_code == 413
    assert respuesta.json()["title"] == "Imagen demasiado grande"


async def test_las_rutas_de_evidencias_no_existen_sin_version(client):
    cliente_id = await _registrar(client)

    respuesta = await client.get(f"/clientes/{cliente_id}/evidencias-identidad")

    assert respuesta.status_code == 404


async def test_la_imagen_aceptada_queda_en_el_almacen(settings, tmp_path):
    async with _http(settings, almacen_backend="filesystem", almacen_ruta=str(tmp_path)) as http:
        cliente_id = await _registrar(http)

        respuesta = await _subir(http, cliente_id, "cedula_frente")

    guardada = tmp_path / "evidencias" / cliente_id / "cedula_frente"
    assert [f.name for f in guardada.iterdir()] == [f"{respuesta.json()['evidenciaId']}.jpg"]


async def test_un_documento_que_no_es_cc_queda_en_revision_manual(client):
    cliente_id = await _registrar(
        client, tipoDocumento="CE", numeroDocumento="E123456", email="ce@example.com"
    )

    respuesta = await _subir(client, cliente_id, "cedula_frente")

    assert respuesta.status_code == 201
    assert respuesta.json()["estado"] == "REVISION_MANUAL"


# ---------- con el verificador real ----------


@pytest.fixture
def con_verificador_local(settings):
    return settings.model_copy(update={"verificador_backend": "local"})


async def test_el_verificador_real_acepta_el_reverso_cuyo_codigo_trae_el_numero(
    con_verificador_local,
):
    async with _http(con_verificador_local) as http:
        cliente_id = await _registrar(http)
        reverso = imagenes.a_bytes(imagenes.foto_de_tarjeta(codigo=CODIGO))

        respuesta = await _subir(http, cliente_id, "cedula_reverso", reverso)

    assert respuesta.status_code == 201
    assert respuesta.json()["estado"] == "ACEPTADA"


async def test_el_verificador_real_manda_a_revision_el_reverso_con_otro_numero(
    con_verificador_local,
):
    async with _http(con_verificador_local) as http:
        cliente_id = await _registrar(http, numeroDocumento="1099999999")
        reverso = imagenes.a_bytes(imagenes.foto_de_tarjeta(codigo=CODIGO))

        respuesta = await _subir(http, cliente_id, "cedula_reverso", reverso)

    assert respuesta.json()["estado"] == "REVISION_MANUAL"


async def test_el_verificador_real_pide_repetir_una_foto_borrosa(con_verificador_local):
    async with _http(con_verificador_local) as http:
        cliente_id = await _registrar(http)
        borrosa = imagenes.a_bytes(imagenes.borrosa(imagenes.foto_de_tarjeta()))

        respuesta = await _subir(http, cliente_id, "cedula_frente", borrosa)

    assert respuesta.status_code == 200
    assert respuesta.json()["motivo"] == "BORROSA"


async def test_el_verificador_real_con_ambas_caras_deja_la_compuerta_lista(con_verificador_local):
    async with _http(con_verificador_local) as http:
        cliente_id = await _registrar(http)
        frente = imagenes.a_bytes(imagenes.foto_de_tarjeta())
        reverso = imagenes.a_bytes(imagenes.foto_de_tarjeta(codigo=CODIGO))

        r1 = await _subir(http, cliente_id, "cedula_frente", frente)
        r2 = await _subir(http, cliente_id, "cedula_reverso", reverso)
        estado = (await http.get(f"/v1/clientes/{cliente_id}/evidencias-identidad")).json()

    assert (r1.status_code, r2.status_code) == (201, 201)
    assert {e["tipo"]: e["estado"] for e in estado}["cedula_frente"] == "ACEPTADA"
    assert {e["tipo"]: e["estado"] for e in estado}["cedula_reverso"] == "ACEPTADA"
