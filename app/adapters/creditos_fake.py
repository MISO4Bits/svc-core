"""Créditos del cliente en memoria (``CORE_CREDITOS_BACKEND=fake``): local y pruebas."""

from __future__ import annotations

from app.domain import CreditosReportados


class CreditosFake:
    """Sin hipotecas por defecto: el saldo sale de la póliza, igual que cuando no hay
    consentimiento. Las pruebas fijan ``respuesta`` para simular otro caso."""

    def __init__(self, respuesta: CreditosReportados | None = None) -> None:
        self.respuesta = respuesta or CreditosReportados(estado="NO_DISPONIBLE")
        self.consultas: list[str] = []

    async def hipotecas(self, cliente_id: str) -> CreditosReportados:
        self.consultas.append(cliente_id)
        return self.respuesta
