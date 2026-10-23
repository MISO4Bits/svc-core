"""Verificador biométrico que analiza la selfie en el propio proceso, sin servicios externos."""

from __future__ import annotations

import asyncio
import threading

from app.biometria import Modelos, ModelosOpenCV, UmbralesBiometria, verificar_selfie
from app.calidad_imagen import UmbralesCalidad
from app.domain import Veredicto


class VerificadorBiometricoLocal:
    """Prueba de vida pasiva y comparación con la cédula (ver ``app.biometria``).

    Los modelos consumen procesador y svc-core también atiende transacciones de dinero:
    ``max_concurrentes`` limita cuántos análisis corren a la vez, y corren en hilos aparte
    para no bloquear el bucle de eventos. Los modelos se cargan al arrancar (``precargar``)
    para que la primera selfie no agote el tiempo límite y caiga en revisión manual.
    """

    def __init__(
        self,
        calidad: UmbralesCalidad,
        umbrales: UmbralesBiometria,
        *,
        ruta_modelos: str,
        max_concurrentes: int = 1,
        modelos: Modelos | None = None,
    ) -> None:
        self._calidad = calidad
        self._umbrales = umbrales
        self._ruta_modelos = ruta_modelos
        self._modelos = modelos
        self._carga = threading.Lock()
        self._cupo = asyncio.Semaphore(max_concurrentes)

    def precargar(self) -> None:
        """Carga los modelos ahora: si faltan, falla al arrancar y no en la primera selfie."""
        self._obtener_modelos()

    def _obtener_modelos(self) -> Modelos:
        with self._carga:
            if self._modelos is None:
                self._modelos = ModelosOpenCV(self._ruta_modelos, self._umbrales.deteccion_min)
            return self._modelos

    async def verificar(self, selfie: bytes, frente_cedula: bytes) -> Veredicto:
        async with self._cupo:
            return await asyncio.to_thread(self._verificar, selfie, frente_cedula)

    def _verificar(self, selfie: bytes, frente_cedula: bytes) -> Veredicto:
        return verificar_selfie(
            selfie,
            frente_cedula,
            modelos=self._obtener_modelos(),
            calidad=self._calidad,
            umbrales=self._umbrales,
        )
