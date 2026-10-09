"""Puertos (interfaces) del hexágono. Los adaptadores viven en ``app/adapters``."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.domain import (
    Cliente,
    Consentimiento,
    ConsentimientoScope,
    DomainEvent,
    PlantillaCorreo,
    TipoCorreo,
)


@runtime_checkable
class ClienteRepository(Protocol):
    async def crear_con_consentimientos(
        self, cliente: Cliente, consentimientos: list[Consentimiento]
    ) -> Cliente:
        """Crea el cliente y sus consentimientos (OPEN_DATA, OPEN_FINANCE) atómicamente."""
        ...

    async def obtener(self, cliente_id: str) -> Cliente | None: ...

    async def obtener_por_identity_ref(self, identity_ref: str) -> Cliente | None: ...

    async def existe_por_documento(self, tipo_documento: str, numero_documento: str) -> bool: ...

    async def existe_por_correo(self, email: str) -> bool: ...

    async def confirmar(self, cliente_id: str) -> Cliente | None:
        """Idempotente: si ya estaba confirmado, no toca ``confirmado_en`` de nuevo."""
        ...


@runtime_checkable
class ConsentimientoRepository(Protocol):
    async def listar(self, cliente_id: str) -> list[Consentimiento]: ...

    async def obtener(
        self, cliente_id: str, scope: ConsentimientoScope
    ) -> Consentimiento | None: ...

    async def guardar(self, consentimiento: Consentimiento) -> Consentimiento: ...


@runtime_checkable
class IdempotencyStore(Protocol):
    async def get(self, key: str) -> dict | None: ...

    async def put(self, key: str, value: dict) -> None: ...


@runtime_checkable
class EventPublisher(Protocol):
    async def publish(self, event: DomainEvent) -> None: ...


@runtime_checkable
class PlantillasCorreoPort(Protocol):
    async def obtener_vigente(self, tipo: TipoCorreo, mercado: str, idioma: str) -> PlantillaCorreo:
        """Plantilla vigente (sin rellenar). Lanza ``NotificacionError`` si no la obtiene."""
        ...


@runtime_checkable
class IdentidadAdminPort(Protocol):
    async def generar_codigo_verificacion(self, email: str) -> str:
        """Pide a Identity Platform un código de un solo uso para verificar ese correo.
        Lanza ``NotificacionError`` si no lo obtiene."""
        ...
