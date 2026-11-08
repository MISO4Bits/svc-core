"""BITS-376: el Backoffice abre la imagen de una evidencia para revisar la solicitud."""

from __future__ import annotations

from app.adapters.memory import InMemoryEvidenciaRepository
from app.domain import EstadoEvidencia, Evidencia, TipoEvidencia
from tests import imagenes
from tests.conftest import CLIENTE_VALIDO


async def _subir(client, formato: str, nombre: str, tipo_mime: str) -> tuple[bytes, str]:
    registro = await client.post("/v1/clientes", json=CLIENTE_VALIDO)
    cliente_id = registro.json()["id"]
    datos = imagenes.a_bytes(imagenes.sin_tarjeta(), formato)
    subida = await client.post(
        f"/v1/clientes/{cliente_id}/evidencias-identidad",
        data={"tipo": "cedula_frente"},
        files={"imagen": (nombre, datos, tipo_mime)},
    )
    assert subida.status_code == 201
    return datos, subida.json()["evidenciaId"]


async def test_la_imagen_sale_tal_como_se_guardo_y_sin_cache(client):
    datos, evidencia_id = await _subir(client, "JPEG", "frente.jpg", "image/jpeg")

    respuesta = await client.get(f"/v1/evidencias-identidad/{evidencia_id}/imagen")

    assert respuesta.status_code == 200
    assert respuesta.headers["content-type"] == "image/jpeg"
    assert respuesta.headers["cache-control"] == "no-store"
    assert respuesta.headers["content-disposition"] == "inline"
    assert respuesta.content == datos


async def test_una_imagen_png_sale_con_su_tipo(client):
    datos, evidencia_id = await _subir(client, "PNG", "frente.png", "image/png")

    respuesta = await client.get(f"/v1/evidencias-identidad/{evidencia_id}/imagen")

    assert respuesta.status_code == 200
    assert respuesta.headers["content-type"] == "image/png"
    assert respuesta.content == datos


async def test_una_evidencia_que_no_existe_es_404(client):
    respuesta = await client.get(
        "/v1/evidencias-identidad/00000000-0000-0000-0000-000000000000/imagen"
    )

    assert respuesta.status_code == 404


async def test_el_repositorio_encuentra_una_evidencia_ya_consumida_por_una_poliza():
    repo = InMemoryEvidenciaRepository()
    evidencia = Evidencia(
        cliente_id="c1",
        tipo=TipoEvidencia.CEDULA_FRENTE,
        estado=EstadoEvidencia.ACEPTADA,
        almacen_ref="evidencias/c1/cedula_frente/x.jpg",
    )
    await repo.guardar_reemplazando(evidencia)
    await repo.consumir("c1", "poliza-1")

    assert await repo.vigente("c1", TipoEvidencia.CEDULA_FRENTE) is None
    assert await repo.obtener(evidencia.id) is evidencia
    assert await repo.obtener("otra") is None
