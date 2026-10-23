"""Prueba de vida pasiva y comparación de la selfie con la cédula (BITS-235).

Todo corre en el propio proceso con los modelos de OpenCV: YuNet detecta rostros, MiniFASNet
(dos redes, promediadas) estima si el rostro es real y SFace compara dos rostros. Es un nivel
básico, no certificado: no cubre cámaras virtuales ni ataques de inyección.

Dos capas separadas, como en ``calidad_imagen``: ``ModelosOpenCV`` mide y ``decidir`` compara
con los umbrales, que se ajustan por configuración sin tocar el algoritmo.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np

from app.calidad_imagen import UmbralesCalidad, medir
from app.calidad_imagen import decidir as decidir_calidad
from app.config import Settings
from app.domain import EstadoEvidencia, MotivoRechazo, Veredicto
from app.imagen import cargar_bgr

logger = logging.getLogger("svc_core.biometria")

LADO_ANALISIS = 1280
DETECTOR = "face_detection_yunet_2023mar.onnx"
RECONOCEDOR = "face_recognition_sface_2021dec.onnx"
# Cada red de vida recorta el rostro con su propia ampliación (la de su entrenamiento).
REDES_DE_VIDA = (("MiniFASNetV2.onnx", 2.7), ("MiniFASNetV1SE.onnx", 4.0))
ARCHIVOS_DE_MODELOS = (DETECTOR, RECONOCEDOR, *(nombre for nombre, _ in REDES_DE_VIDA))
_LADO_RED_DE_VIDA = 80
_CLASE_REAL = 1  # las redes dan tres clases: [impreso, real, pantalla]
# Un segundo rostro solo cuenta si mide al menos esta fracción del principal; los más
# pequeños son fondo (un cuadro, alguien lejos).
_FRACCION_OTRO_ROSTRO = 0.5


@dataclass(frozen=True)
class UmbralesBiometria:
    vida_aceptar: float = 0.80
    vida_rechazar: float = 0.40
    coincidencia_aceptar: float = 0.40
    coincidencia_rechazar: float = 0.25
    deteccion_min: float = 0.70
    rostro_min: float = 0.15

    @classmethod
    def desde(cls, settings: Settings) -> UmbralesBiometria:
        return cls(
            vida_aceptar=settings.biometria_vida_aceptar,
            vida_rechazar=settings.biometria_vida_rechazar,
            coincidencia_aceptar=settings.biometria_coincidencia_aceptar,
            coincidencia_rechazar=settings.biometria_coincidencia_rechazar,
            deteccion_min=settings.biometria_deteccion_min,
            rostro_min=settings.biometria_rostro_min,
        )


@dataclass(frozen=True)
class Rostro:
    """Una detección de YuNet: caja, cinco puntos de referencia y confianza (15 valores)."""

    fila: np.ndarray

    @property
    def caja(self) -> tuple[float, float, float, float]:
        x, y, ancho, alto = (float(v) for v in self.fila[:4])
        return x, y, ancho, alto

    @property
    def ancho(self) -> float:
        return float(self.fila[2])


class Modelos(Protocol):
    def detectar(self, imagen: np.ndarray) -> list[Rostro]:
        """Rostros hallados, del más grande al más pequeño."""
        ...

    def vida(self, imagen: np.ndarray, rostro: Rostro) -> float:
        """Probabilidad, de 0 a 1, de que el rostro sea real y no una foto o una pantalla."""
        ...

    def similitud(self, a: np.ndarray, rostro_a: Rostro, b: np.ndarray, rostro_b: Rostro) -> float:
        """Similitud coseno entre dos rostros: 1 es idéntico."""
        ...


def recorte_de_vida(
    imagen: np.ndarray, caja: tuple[float, float, float, float], escala: float
) -> np.ndarray:
    """Recorta el rostro ampliado ``escala`` veces y lo deja de 80x80.

    Si la ampliación se sale de la foto se desplaza el recorte, no se achica: así no se
    deforma el rostro, que es lo que la red espera.
    """
    alto, ancho = imagen.shape[:2]
    x, y, w, h = caja
    escala = min((alto - 1) / h, (ancho - 1) / w, escala)
    mitad_w, mitad_h = w * escala / 2, h * escala / 2
    cx, cy = x + w / 2, y + h / 2
    x0, y0, x1, y1 = cx - mitad_w, cy - mitad_h, cx + mitad_w, cy + mitad_h
    if x0 < 0:
        x0, x1 = 0, x1 - x0
    if y0 < 0:
        y0, y1 = 0, y1 - y0
    if x1 > ancho - 1:
        x0, x1 = x0 - (x1 - (ancho - 1)), ancho - 1
    if y1 > alto - 1:
        y0, y1 = y0 - (y1 - (alto - 1)), alto - 1
    recorte = imagen[max(0, int(y0)) : int(y1) + 1, max(0, int(x0)) : int(x1) + 1]
    return cv2.resize(recorte, (_LADO_RED_DE_VIDA, _LADO_RED_DE_VIDA))


class ModelosOpenCV:
    """Carga los cuatro modelos de ``ruta`` y los ejecuta con ``cv2``.

    Las redes y el detector guardan estado entre llamadas, así que un cerrojo impide que dos
    hilos las usen a la vez.
    """

    def __init__(self, ruta: str | Path, deteccion_min: float) -> None:
        base = Path(ruta)
        faltan = [n for n in ARCHIVOS_DE_MODELOS if not (base / n).is_file()]
        if faltan:
            raise FileNotFoundError(
                f"Faltan modelos de biometría en {base}: {', '.join(faltan)} "
                "(se bajan con tools/descargar_modelos.py)"
            )
        self._detector = cv2.FaceDetectorYN.create(
            str(base / DETECTOR), "", (320, 320), deteccion_min, 0.3, 5000
        )
        self._reconocedor = cv2.FaceRecognizerSF.create(str(base / RECONOCEDOR), "")
        self._redes = [
            (cv2.dnn.readNetFromONNX(str(base / nombre)), escala)
            for nombre, escala in REDES_DE_VIDA
        ]
        self._cerrojo = threading.Lock()

    def detectar(self, imagen: np.ndarray) -> list[Rostro]:
        alto, ancho = imagen.shape[:2]
        with self._cerrojo:
            self._detector.setInputSize((ancho, alto))
            _, filas = self._detector.detect(imagen)
        if filas is None:
            return []
        rostros = [Rostro(np.array(fila, dtype=np.float32)) for fila in filas]
        return sorted(rostros, key=lambda r: r.fila[2] * r.fila[3], reverse=True)

    def vida(self, imagen: np.ndarray, rostro: Rostro) -> float:
        probabilidades = []
        with self._cerrojo:
            for red, escala in self._redes:
                recorte = recorte_de_vida(imagen, rostro.caja, escala)
                red.setInput(cv2.dnn.blobFromImage(recorte, 1.0, (_LADO_RED_DE_VIDA,) * 2))
                logits = red.forward().astype(np.float64)[0]
                exponenciales = np.exp(logits - logits.max())
                probabilidades.append(exponenciales[_CLASE_REAL] / exponenciales.sum())
        return float(np.mean(probabilidades))

    def similitud(self, a: np.ndarray, rostro_a: Rostro, b: np.ndarray, rostro_b: Rostro) -> float:
        with self._cerrojo:
            vector_a = self._reconocedor.feature(self._reconocedor.alignCrop(a, rostro_a.fila))
            vector_b = self._reconocedor.feature(self._reconocedor.alignCrop(b, rostro_b.fila))
            return float(
                self._reconocedor.match(vector_a, vector_b, cv2.FaceRecognizerSF_FR_COSINE)
            )


def decidir(vida: float, similitud: float, umbrales: UmbralesBiometria) -> Veredicto:
    """Compara los puntajes con sus umbrales. Entre "rechazar" y "aceptar" decide una persona."""
    puntajes = {"puntaje_vida": vida, "puntaje_coincidencia": similitud}
    if vida < umbrales.vida_rechazar:
        return Veredicto(EstadoEvidencia.REINTENTAR, MotivoRechazo.POSIBLE_SUPLANTACION, **puntajes)
    if similitud < umbrales.coincidencia_rechazar:
        return Veredicto(
            EstadoEvidencia.REINTENTAR, MotivoRechazo.NO_COINCIDE_CON_CEDULA, **puntajes
        )
    if vida >= umbrales.vida_aceptar and similitud >= umbrales.coincidencia_aceptar:
        return Veredicto(EstadoEvidencia.ACEPTADA, **puntajes)
    return Veredicto(EstadoEvidencia.REVISION_MANUAL, **puntajes)


def verificar_selfie(
    selfie: bytes,
    frente_cedula: bytes,
    *,
    modelos: Modelos,
    calidad: UmbralesCalidad,
    umbrales: UmbralesBiometria,
) -> Veredicto:
    """Calidad, rostros, vida y coincidencia, en ese orden; corta en el primero que falla."""
    inicio = time.perf_counter()
    medidas = medir(selfie, calidad, con_tarjeta=False)
    motivo = decidir_calidad(medidas, calidad, con_tarjeta=False)
    if motivo is not None:
        return Veredicto(EstadoEvidencia.REINTENTAR, motivo)
    t_calidad = time.perf_counter()

    foto = cargar_bgr(selfie, LADO_ANALISIS)
    rostros = modelos.detectar(foto)
    t_rostros = time.perf_counter()
    if not rostros or rostros[0].ancho < umbrales.rostro_min * foto.shape[1]:
        # Un rostro diminuto se trata igual que ninguno: el mensaje es "acércate y encuádrate".
        return Veredicto(EstadoEvidencia.REINTENTAR, MotivoRechazo.SIN_ROSTRO)
    principal = rostros[0]
    if any(r.ancho >= _FRACCION_OTRO_ROSTRO * principal.ancho for r in rostros[1:]):
        return Veredicto(EstadoEvidencia.REINTENTAR, MotivoRechazo.VARIOS_ROSTROS)

    vida = modelos.vida(foto, principal)
    t_vida = time.perf_counter()
    if vida < umbrales.vida_rechazar:
        logger.info("verificar_selfie: posible suplantación vida=%.3f", vida)
        return Veredicto(
            EstadoEvidencia.REINTENTAR, MotivoRechazo.POSIBLE_SUPLANTACION, puntaje_vida=vida
        )

    cedula = cargar_bgr(frente_cedula, LADO_ANALISIS)
    rostros_cedula = modelos.detectar(cedula)
    if not rostros_cedula:
        # El problema es el documento, no la selfie: repetirla no lo arregla.
        logger.info("verificar_selfie: sin rostro en el frente de la cédula, revisión manual")
        return Veredicto(EstadoEvidencia.REVISION_MANUAL, puntaje_vida=vida)
    similitud = modelos.similitud(foto, principal, cedula, rostros_cedula[0])
    veredicto = decidir(vida, similitud, umbrales)
    fin = time.perf_counter()
    logger.info(
        "verificar_selfie: estado=%s motivo=%s vida=%.3f coincidencia=%.3f "
        "calidad_ms=%d rostros_ms=%d vida_ms=%d coincidencia_ms=%d",
        veredicto.estado,
        veredicto.motivo,
        vida,
        similitud,
        (t_calidad - inicio) * 1000,
        (t_rostros - t_calidad) * 1000,
        (t_vida - t_rostros) * 1000,
        (fin - t_vida) * 1000,
    )
    return veredicto
