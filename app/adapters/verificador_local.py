"""Verificador que analiza la imagen en el propio proceso, sin llamar a servicios externos."""

from __future__ import annotations

import asyncio

from app.calidad_imagen import UmbralesCalidad, decidir, medir
from app.codigo_reverso import contiene_numero, leer_codigos
from app.domain import EstadoEvidencia, MotivoRechazo, TipoEvidencia, Veredicto

_CEDULA = (TipoEvidencia.CEDULA_FRENTE, TipoEvidencia.CEDULA_REVERSO)


class VerificadorLocal:
    """Calidad (nitidez, luz, reflejo), forma de la tarjeta y, en el reverso, cruce del número.

    El análisis consume procesador y svc-core también atiende transacciones de dinero:
    ``max_concurrentes`` limita cuántos corren a la vez, y corren en hilos aparte para no
    bloquear el bucle de eventos. La selfie solo pasa la revisión de calidad; la prueba de
    vida es de su propia historia.
    """

    def __init__(self, umbrales: UmbralesCalidad, *, max_concurrentes: int = 2) -> None:
        self._umbrales = umbrales
        self._cupo = asyncio.Semaphore(max_concurrentes)

    async def analizar(
        self, imagen: bytes, tipo: TipoEvidencia, numero_esperado: str | None
    ) -> Veredicto:
        async with self._cupo:
            return await asyncio.to_thread(self._analizar, imagen, tipo, numero_esperado)

    def _analizar(
        self, imagen: bytes, tipo: TipoEvidencia, numero_esperado: str | None
    ) -> Veredicto:
        es_cedula = tipo in _CEDULA
        medidas = medir(imagen, self._umbrales, con_tarjeta=es_cedula)
        motivo: MotivoRechazo | None = decidir(medidas, self._umbrales, con_tarjeta=es_cedula)
        if motivo is not None:
            return Veredicto(EstadoEvidencia.REINTENTAR, motivo)

        if tipo is TipoEvidencia.CEDULA_REVERSO and numero_esperado is not None:
            if not contiene_numero(leer_codigos(imagen), numero_esperado):
                # No se pudo leer el código o no trae ese número: repetir la foto no lo
                # arregla, lo decide una persona.
                return Veredicto(EstadoEvidencia.REVISION_MANUAL)
        return Veredicto(EstadoEvidencia.ACEPTADA)
