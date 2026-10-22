"""Lectura del código bidimensional del reverso de la cédula y cruce con el número registrado.

El cruce no interpreta el contenido del código: solo comprueba si el número del cliente
aparece en él como cadena de dígitos. El diseño interno del código no está documentado
oficialmente, y así no se extrae, ni se guarda, ningún otro dato personal que contenga
(nombres, fecha de nacimiento, grupo sanguíneo).
"""

from __future__ import annotations

import re

import zxingcpp

from app.imagen import cargar_gris

# Un PDF417 de cédula necesita resolución: a 1000 px los módulos son demasiado finos.
LADO_LECTURA = 1600
_FORMATOS = [zxingcpp.BarcodeFormat.PDF417, zxingcpp.BarcodeFormat.QRCode]
_DIGITOS = re.compile(rb"\d+")
_MIN_DIGITOS = 4


def leer_codigos(datos: bytes) -> list[bytes]:
    """Contenido en bruto de los códigos PDF417 y QR hallados en la foto."""
    gris, _ = cargar_gris(datos, LADO_LECTURA)
    return [codigo.bytes for codigo in zxingcpp.read_barcodes(gris, formats=_FORMATOS)]


def _normalizar(numero: str) -> str:
    """Solo dígitos y sin ceros a la izquierda: '1.032.456.789' y '0001032456789' coinciden."""
    return re.sub(r"\D", "", numero).lstrip("0")


def contiene_numero(contenidos: list[bytes], esperado: str) -> bool:
    """``True`` si el número esperado aparece dentro de alguna cadena de dígitos del código."""
    objetivo = _normalizar(esperado).encode()
    if len(objetivo) < _MIN_DIGITOS:
        return False
    return any(
        objetivo in corrida.lstrip(b"0")
        for contenido in contenidos
        for corrida in _DIGITOS.findall(contenido)
    )
