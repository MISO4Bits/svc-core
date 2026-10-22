from __future__ import annotations

import pytest

from app.codigo_reverso import contiene_numero, leer_codigos
from tests import imagenes

CONTENIDO = b"PubDSK_1\x00\x00001032456789RIOS     ANA\x00"


def test_lee_un_pdf417_dentro_de_la_foto_de_la_tarjeta():
    foto = imagenes.foto_de_tarjeta(codigo=CONTENIDO)

    assert leer_codigos(imagenes.a_bytes(foto)) == [CONTENIDO]


def test_sin_codigo_no_devuelve_nada():
    assert leer_codigos(imagenes.a_bytes(imagenes.foto_de_tarjeta())) == []


@pytest.mark.parametrize(
    "esperado",
    ["1032456789", "1.032.456.789", "0001032456789", " 1032456789 "],
    ids=["exacto", "con-puntos", "con-ceros", "con-espacios"],
)
def test_el_numero_coincide_sin_importar_el_formato(esperado):
    assert contiene_numero([CONTENIDO], esperado)


def test_otro_numero_no_coincide():
    assert not contiene_numero([CONTENIDO], "1099999999")


def test_sin_contenidos_no_coincide():
    assert not contiene_numero([], "1032456789")


def test_un_numero_demasiado_corto_no_se_compara():
    # Cuatro dígitos coinciden por azar con cualquier campo numérico del código.
    assert not contiene_numero([b"abc 12345678 xyz"], "12")


def test_busca_en_todos_los_codigos():
    assert contiene_numero([b"nada 99", CONTENIDO], "1032456789")
