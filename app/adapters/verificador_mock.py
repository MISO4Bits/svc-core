"""Verificador de pruebas: devuelve veredictos configurables, sin mirar la imagen.

Sirve para desarrollar y probar los clientes sin depender del análisis real. La
secuencia (``CORE_VERIFICADOR_MOCK_SECUENCIA``) se recorre en rotación, una entrada por
llamada: ``BORROSA,ACEPTADA`` rechaza la primera foto y acepta la siguiente.
"""

from __future__ import annotations

from itertools import cycle

from app.domain import EstadoEvidencia, MotivoRechazo, TipoEvidencia, Veredicto


def _veredicto(nombre: str) -> Veredicto:
    nombre = nombre.strip().upper()
    if nombre in (EstadoEvidencia.ACEPTADA, EstadoEvidencia.REVISION_MANUAL):
        return Veredicto(EstadoEvidencia(nombre))
    return Veredicto(EstadoEvidencia.REINTENTAR, MotivoRechazo(nombre))


class VerificadorMock:
    def __init__(self, secuencia: str = "ACEPTADA") -> None:
        nombres = [n for n in secuencia.split(",") if n.strip()]
        if not nombres:
            raise ValueError("verificador_mock_secuencia no puede estar vacía")
        # Falla al arrancar, no en la primera foto, si un nombre no existe.
        self._veredictos = cycle([_veredicto(n) for n in nombres])

    async def analizar(
        self, imagen: bytes, tipo: TipoEvidencia, numero_esperado: str | None
    ) -> Veredicto:
        return next(self._veredictos)
