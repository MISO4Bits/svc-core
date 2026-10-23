from __future__ import annotations

import io

import pytest
from PIL import Image

from app.domain import ImagenInvalida, ImagenMuyGrande
from app.imagen import cargar_bgr, cargar_gris, tipo_de_contenido, validar_imagen
from tests import imagenes

LIMITES = {"max_bytes": 3_000_000, "max_pixeles": 40_000_000}


def test_reconoce_el_tipo_por_los_primeros_bytes():
    assert tipo_de_contenido(imagenes.a_bytes(imagenes.sin_tarjeta())) == "image/jpeg"
    assert tipo_de_contenido(imagenes.a_bytes(imagenes.sin_tarjeta(), "PNG")) == "image/png"
    assert tipo_de_contenido(b"%PDF-1.7 ...") is None


@pytest.mark.parametrize("formato", ["JPEG", "PNG"])
def test_acepta_jpeg_y_png_validos(formato):
    datos = imagenes.a_bytes(imagenes.sin_tarjeta(), formato)

    assert validar_imagen(datos, **LIMITES) == tipo_de_contenido(datos)


def test_rechaza_lo_que_no_es_una_imagen_aunque_traiga_otra_extension():
    with pytest.raises(ImagenInvalida):
        validar_imagen(b"%PDF-1.7 contenido", **LIMITES)


def test_rechaza_una_imagen_truncada():
    datos = imagenes.a_bytes(imagenes.sin_tarjeta())

    with pytest.raises(ImagenInvalida):
        validar_imagen(datos[:200], **LIMITES)


def test_rechaza_cabecera_de_imagen_sin_contenido():
    with pytest.raises(ImagenInvalida):
        validar_imagen(b"\xff\xd8\xff\x00basura", **LIMITES)


def test_rechaza_lo_que_pesa_mas_del_limite():
    datos = imagenes.a_bytes(imagenes.sin_tarjeta())

    with pytest.raises(ImagenMuyGrande) as error:
        validar_imagen(datos, max_bytes=1000, max_pixeles=40_000_000)

    assert error.value.limite_bytes == 1000


def test_rechaza_la_imagen_con_demasiados_pixeles_sin_decodificarla():
    datos = imagenes.a_bytes(imagenes.sin_tarjeta())  # 1600 x 1200 = 1,92 millones

    with pytest.raises(ImagenInvalida, match="píxeles"):
        validar_imagen(datos, max_bytes=3_000_000, max_pixeles=1_000_000)


def test_carga_en_gris_y_reduce_al_lado_pedido():
    gris, original = cargar_gris(imagenes.a_bytes(imagenes.sin_tarjeta()), 800)

    assert original == (1600, 1200)
    assert gris.ndim == 2
    assert max(gris.shape) == 800


def test_cargar_gris_aplica_la_orientacion_de_la_camara():
    foto = Image.new("RGB", (400, 200), "white")
    exif = Image.Exif()
    exif[0x0112] = 6  # girada 90 grados
    salida = io.BytesIO()
    foto.save(salida, "JPEG", exif=exif)

    gris, original = cargar_gris(salida.getvalue(), 1000)

    assert original == (200, 400)
    assert gris.shape == (400, 200)


def test_cargar_gris_falla_con_bytes_dañados():
    with pytest.raises(ImagenInvalida):
        cargar_gris(b"no es una imagen", 1000)


def test_cargar_bgr_devuelve_los_canales_en_el_orden_de_opencv():
    roja = Image.new("RGB", (100, 50), (255, 0, 0))
    salida = io.BytesIO()
    roja.save(salida, "PNG")

    bgr = cargar_bgr(salida.getvalue(), 1000)

    assert bgr.shape == (50, 100, 3)
    assert tuple(bgr[0, 0]) == (0, 0, 255)  # azul, verde, rojo
    assert bgr.flags["C_CONTIGUOUS"]


def test_cargar_bgr_reduce_al_lado_pedido_y_aplica_la_orientacion():
    foto = Image.new("RGB", (1600, 800), "white")
    exif = Image.Exif()
    exif[0x0112] = 6
    salida = io.BytesIO()
    foto.save(salida, "JPEG", exif=exif)

    bgr = cargar_bgr(salida.getvalue(), 400)

    assert bgr.shape[:2] == (400, 200)


def test_cargar_bgr_falla_con_bytes_dañados():
    with pytest.raises(ImagenInvalida):
        cargar_bgr(b"no es una imagen", 1000)
