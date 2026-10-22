"""Fotos sintéticas de una tarjeta para probar el análisis sin cédulas reales."""

from __future__ import annotations

import io
import random

import numpy as np
import zxingcpp
from PIL import Image, ImageDraw, ImageFilter

ANCHO, ALTO = 1600, 1200
PROPORCION = 1.586


def _fondo(ancho: int, alto: int, semilla: int) -> Image.Image:
    """Fondo gris medio con ruido suave, como una mesa."""
    rng = np.random.default_rng(semilla)
    base = 95 + rng.normal(0, 4, (alto, ancho))
    return Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "L")


def _texto(dibujo: ImageDraw.ImageDraw, caja: tuple[int, int, int, int], semilla: int) -> None:
    """Renglones oscuros de largo irregular: bordes finos, como letras."""
    x0, y0, x1, y1 = caja
    rng = random.Random(semilla)
    y = y0 + 40
    while y < y1 - 40:
        x = x0 + 40
        while x < x1 - 60:
            largo = rng.randint(15, 70)
            dibujo.rectangle([x, y, min(x + largo, x1 - 40), y + 6], fill=rng.randint(10, 50))
            x += largo + rng.randint(8, 20)
        y += 26


def foto_de_tarjeta(
    *,
    ancho_tarjeta: int = 1100,
    desplazamiento: tuple[int, int] = (0, 0),
    semilla: int = 1,
    codigo: bytes | None = None,
) -> Image.Image:
    """Tarjeta clara con texto, centrada sobre un fondo más oscuro."""
    foto = _fondo(ANCHO, ALTO, semilla).convert("L")
    alto_tarjeta = round(ancho_tarjeta / PROPORCION)
    x0 = (ANCHO - ancho_tarjeta) // 2 + desplazamiento[0]
    y0 = (ALTO - alto_tarjeta) // 2 + desplazamiento[1]
    caja = (x0, y0, x0 + ancho_tarjeta, y0 + alto_tarjeta)
    dibujo = ImageDraw.Draw(foto)
    dibujo.rectangle(caja, fill=205)
    _texto(dibujo, caja, semilla)
    if codigo is not None:
        barras = zxingcpp.write_barcode_to_image(
            zxingcpp.create_barcode(codigo, zxingcpp.BarcodeFormat.PDF417)
        )
        parche = Image.fromarray(np.asarray(barras)).convert("L")
        parche = parche.resize((ancho_tarjeta - 160, max(120, parche.height * 3)), Image.NEAREST)
        foto.paste(parche, (x0 + 80, caja[3] - parche.height - 40))
    return foto


def sin_tarjeta(semilla: int = 3) -> Image.Image:
    """Solo fondo con ruido: no hay ningún rectángulo."""
    return _fondo(ANCHO, ALTO, semilla)


def borrosa(foto: Image.Image, radio: float = 10) -> Image.Image:
    return foto.filter(ImageFilter.GaussianBlur(radio))


def oscura(foto: Image.Image, factor: float = 0.2) -> Image.Image:
    return foto.point(lambda valor: int(valor * factor))


def con_reflejo(foto: Image.Image) -> Image.Image:
    """Mancha blanca grande sobre la tarjeta, como el brillo del plástico."""
    foto = foto.copy()
    ImageDraw.Draw(foto).ellipse([650, 450, 1050, 750], fill=255)
    return foto


def sobreexpuesta(foto: Image.Image) -> Image.Image:
    return foto.point(lambda valor: min(255, int(valor * 2.4)))


def a_bytes(foto: Image.Image, formato: str = "JPEG", calidad: int = 90) -> bytes:
    salida = io.BytesIO()
    foto.convert("RGB").save(salida, formato, quality=calidad)
    return salida.getvalue()
