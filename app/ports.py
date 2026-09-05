"""Puertos (interfaces) del hexágono. Los adaptadores viven en ``app/adapters``."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.domain import Cliente, Consentimiento, ConsentimientoScope, DomainEvent


@runtime_checkable
class ClienteRepository(Protocol):
    async def crear(self, cliente: Cliente) -> Cliente: ...

    async def obtener(self, cliente_id: str) -> Cliente | None: ...

    async def obtener_por_identity_ref(self, identity_ref: str) -> Cliente | None: ...

    async def existe_por_documento(self, tipo_documento: str, numero_documento: str) -> bool: ...


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
