"""Medición de calidad de una foto de documento y decisión contra umbrales.

Dos pasos separados: ``medir`` devuelve números y ``decidir`` los compara con los
umbrales. Así los umbrales se ajustan por configuración sin tocar el algoritmo.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from app.config import Settings
from app.domain import MotivoRechazo
from app.imagen import cargar_gris

LADO_ANALISIS = 1000
_NIVEL_OSCURO = 30
_NIVEL_QUEMADO = 240
_NIVEL_REFLEJO = 245
# Sin la tarjeta, la nitidez se mide en el centro: el marco guía la deja ahí.
_MARGEN_CENTRO = 0.10
# Dentro de la tarjeta se ignora el borde, donde la transición con el fondo falsea la nitidez.
_MARGEN_TARJETA = 0.05
_AREA_MIN_CANDIDATA = 0.05


@dataclass(frozen=True)
class UmbralesCalidad:
    lado_min_px: int = 600
    nitidez_min: float = 40.0
    luz_media_min: float = 55.0
    luz_media_max: float = 225.0
    oscuros_max: float = 0.60
    quemados_max: float = 0.45
    reflejo_max: float = 0.04
    proporcion_tarjeta: float = 1.586
    proporcion_tolerancia: float = 0.25
    tarjeta_area_min: float = 0.25
    margen_borde: float = 0.01

    @classmethod
    def desde(cls, settings: Settings) -> UmbralesCalidad:
        return cls(
            lado_min_px=settings.calidad_lado_min_px,
            nitidez_min=settings.calidad_nitidez_min,
            luz_media_min=settings.calidad_luz_media_min,
            luz_media_max=settings.calidad_luz_media_max,
            oscuros_max=settings.calidad_oscuros_max,
            quemados_max=settings.calidad_quemados_max,
            reflejo_max=settings.calidad_reflejo_max,
            proporcion_tarjeta=settings.calidad_proporcion_tarjeta,
            proporcion_tolerancia=settings.calidad_proporcion_tolerancia,
            tarjeta_area_min=settings.calidad_tarjeta_area_min,
            margen_borde=settings.calidad_margen_borde,
        )


@dataclass(frozen=True)
class Tarjeta:
    """Rectángulo más grande hallado en la foto, en coordenadas de la imagen de análisis."""

    caja: tuple[int, int, int, int]  # x0, y0, x1, y1
    area: float  # fracción de la foto
    proporcion: float  # lado largo / lado corto
    proporcion_valida: bool
    toca_borde: bool


@dataclass(frozen=True)
class Medidas:
    lado_mayor_px: int
    luz_media: float
    oscuros: float
    quemados: float
    reflejo: float
    nitidez: float
    tarjeta: Tarjeta | None


def nitidez(gris: np.ndarray) -> float:
    """Varianza del Laplaciano: bordes marcados dan varianza alta, una foto borrosa baja."""
    g = gris.astype(np.float32)
    laplaciano = g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:] - 4 * g[1:-1, 1:-1]
    return float(laplaciano.var())


def luminosidad(gris: np.ndarray) -> tuple[float, float, float]:
    """Media y fracción de píxeles casi negros y casi blancos.

    La media sola engaña (mitad negra y mitad blanca promedia "bien"), por eso se
    miran también los extremos.
    """
    return (
        float(gris.mean()),
        float((gris < _NIVEL_OSCURO).mean()),
        float((gris > _NIVEL_QUEMADO).mean()),
    )


def reflejo(gris: np.ndarray) -> float:
    """Fracción de la foto que ocupa la mancha casi blanca conectada más grande."""
    mascara = (gris >= _NIVEL_REFLEJO).astype(np.uint8)
    cantidad, _, estadisticas, _ = cv2.connectedComponentsWithStats(mascara, connectivity=8)
    if cantidad <= 1:
        return 0.0
    mayor = int(estadisticas[1:, cv2.CC_STAT_AREA].max())
    return mayor / gris.size


def detectar_tarjeta(gris: np.ndarray, umbrales: UmbralesCalidad) -> Tarjeta | None:
    alto, ancho = gris.shape
    suave = cv2.GaussianBlur(gris, (5, 5), 0)
    bordes = cv2.Canny(suave, 40, 120)
    bordes = cv2.dilate(bordes, np.ones((3, 3), np.uint8), iterations=2)
    contornos, _ = cv2.findContours(bordes, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contornos:
        return None

    mayor = max(contornos, key=cv2.contourArea)
    (_, _), (w, h), _ = cv2.minAreaRect(mayor)
    if min(w, h) <= 0:
        return None
    area = (w * h) / (ancho * alto)
    if area < _AREA_MIN_CANDIDATA:
        return None

    x, y, bw, bh = cv2.boundingRect(mayor)
    margen = max(4, int(umbrales.margen_borde * min(ancho, alto)))
    proporcion = max(w, h) / min(w, h)
    return Tarjeta(
        caja=(x, y, x + bw, y + bh),
        area=float(area),
        proporcion=float(proporcion),
        proporcion_valida=abs(proporcion - umbrales.proporcion_tarjeta)
        <= umbrales.proporcion_tolerancia,
        toca_borde=x <= margen
        or y <= margen
        or x + bw >= ancho - margen
        or y + bh >= alto - margen,
    )


def _region_de_nitidez(gris: np.ndarray, tarjeta: Tarjeta | None) -> np.ndarray:
    alto, ancho = gris.shape
    if tarjeta is not None:
        x0, y0, x1, y1 = tarjeta.caja
        mx, my = int((x1 - x0) * _MARGEN_TARJETA), int((y1 - y0) * _MARGEN_TARJETA)
        region = gris[y0 + my : y1 - my, x0 + mx : x1 - mx]
        if min(region.shape) > 8:
            return region
    mx, my = int(ancho * _MARGEN_CENTRO), int(alto * _MARGEN_CENTRO)
    return gris[my : alto - my, mx : ancho - mx]


def medir(datos: bytes, umbrales: UmbralesCalidad, *, con_tarjeta: bool) -> Medidas:
    """Mide la foto. Con ``con_tarjeta`` busca además el rectángulo del documento."""
    gris, (ancho, alto) = cargar_gris(datos, LADO_ANALISIS)
    tarjeta = detectar_tarjeta(gris, umbrales) if con_tarjeta else None
    media, oscuros, quemados = luminosidad(gris)
    return Medidas(
        lado_mayor_px=max(ancho, alto),
        luz_media=media,
        oscuros=oscuros,
        quemados=quemados,
        reflejo=reflejo(gris),
        nitidez=nitidez(_region_de_nitidez(gris, tarjeta)),
        tarjeta=tarjeta,
    )


def decidir(
    medidas: Medidas, umbrales: UmbralesCalidad, *, con_tarjeta: bool
) -> MotivoRechazo | None:
    """``None`` si la foto sirve; si no, el motivo que se le muestra al usuario.

    El orden importa: una foto oscura o quemada rompe la detección de la tarjeta y
    daría un ``NO_ES_CEDULA`` engañoso, así que la luz se revisa primero.
    """
    if medidas.lado_mayor_px < umbrales.lado_min_px:
        return MotivoRechazo.BORROSA  # pocos píxeles útiles: que se acerque o use mejor cámara
    if medidas.luz_media < umbrales.luz_media_min or medidas.oscuros > umbrales.oscuros_max:
        return MotivoRechazo.OSCURA
    if medidas.luz_media > umbrales.luz_media_max or medidas.quemados > umbrales.quemados_max:
        return MotivoRechazo.REFLEJO  # sobreexposición global: demasiada luz sobre el plástico

    if con_tarjeta:
        tarjeta = medidas.tarjeta
        if tarjeta is None:
            # Una foto muy borrosa borra el contorno: antes que "no es cédula", es borrosa.
            if medidas.nitidez < umbrales.nitidez_min:
                return MotivoRechazo.BORROSA
            return MotivoRechazo.NO_ES_CEDULA
        # Una figura grande que toca el borde es, casi seguro, una tarjeta cortada aunque su
        # proporción ya no cuadre.
        recorte_evidente = tarjeta.area >= umbrales.tarjeta_area_min
        if tarjeta.toca_borde and (tarjeta.proporcion_valida or recorte_evidente):
            return MotivoRechazo.RECORTADA
        if not tarjeta.proporcion_valida:
            return MotivoRechazo.NO_ES_CEDULA
        if tarjeta.area < umbrales.tarjeta_area_min:
            return MotivoRechazo.BORROSA  # demasiado lejos: la tarjeta ocupa pocos píxeles

    if medidas.reflejo > umbrales.reflejo_max:
        return MotivoRechazo.REFLEJO
    if medidas.nitidez < umbrales.nitidez_min:
        return MotivoRechazo.BORROSA
    return None
