"""Lectura y validación de imágenes de evidencia. Funciones puras, sin FastAPI ni base de datos."""

from __future__ import annotations

import io

import numpy as np
from PIL import Image, ImageOps

from app.domain import ImagenInvalida, ImagenMuyGrande

_JPEG = b"\xff\xd8\xff"
_PNG = b"\x89PNG\r\n\x1a\n"


def tipo_de_contenido(datos: bytes) -> str | None:
    """Tipo real según los primeros bytes; no se confía en la extensión ni en lo declarado."""
    if datos.startswith(_JPEG):
        return "image/jpeg"
    if datos.startswith(_PNG):
        return "image/png"
    return None


def validar_imagen(datos: bytes, *, max_bytes: int, max_pixeles: int) -> str:
    """Comprueba tamaño, formato real y dimensiones. Devuelve el tipo de contenido.

    Las dimensiones se leen del encabezado, antes de decodificar, para que una imagen
    diminuta en bytes pero enorme en píxeles no agote la memoria.
    """
    if len(datos) > max_bytes:
        raise ImagenMuyGrande(max_bytes)
    tipo = tipo_de_contenido(datos)
    if tipo is None:
        raise ImagenInvalida("El archivo no es una imagen JPEG o PNG")
    try:
        with Image.open(io.BytesIO(datos)) as imagen:
            ancho, alto = imagen.size
            if ancho * alto > max_pixeles:
                raise ImagenInvalida("La imagen tiene demasiados píxeles")
            imagen.verify()
    except ImagenInvalida:
        raise
    except Exception as exc:
        raise ImagenInvalida("La imagen está dañada o no se puede leer") from exc
    return tipo


def cargar_gris(datos: bytes, lado_max: int) -> tuple[np.ndarray, tuple[int, int]]:
    """Imagen en escala de grises, orientada y reducida a ``lado_max`` de lado largo.

    Reducir a un tamaño fijo hace comparables las medidas entre fotos de distinta
    resolución y deja el análisis en decenas de milisegundos. Devuelve también el
    tamaño original ``(ancho, alto)`` ya orientado.
    """
    try:
        with Image.open(io.BytesIO(datos)) as imagen:
            imagen = ImageOps.exif_transpose(imagen)
            tamano = imagen.size
            imagen.draft("L", (lado_max, lado_max))  # JPEG: decodifica ya reducido
            gris = imagen.convert("L")
            gris.thumbnail((lado_max, lado_max))
            return np.asarray(gris, dtype=np.uint8), tamano
    except Exception as exc:
        raise ImagenInvalida("La imagen está dañada o no se puede leer") from exc
