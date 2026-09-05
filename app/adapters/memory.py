"""Adaptadores en memoria. Usados en pruebas unitarias y para arranque rápido local."""

from __future__ import annotations

from app.domain import Cliente, Consentimiento, ConsentimientoScope, DomainEvent


class InMemoryClienteRepository:
    def __init__(self) -> None:
        self._by_id: dict[str, Cliente] = {}
        self._docs: set[tuple[str, str]] = set()

    async def crear(self, cliente: Cliente) -> Cliente:
        self._by_id[cliente.id] = cliente
        self._docs.add((str(cliente.tipo_documento), cliente.numero_documento))
        return cliente

    async def obtener(self, cliente_id: str) -> Cliente | None:
        return self._by_id.get(cliente_id)

    async def obtener_por_identity_ref(self, identity_ref: str) -> Cliente | None:
        return next(
            (c for c in self._by_id.values() if c.identity_ref == identity_ref), None
        )

    async def existe_por_documento(
        self, tipo_documento: str, numero_documento: str
    ) -> bool:
        return (str(tipo_documento), numero_documento) in self._docs


class InMemoryConsentimientoRepository:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], Consentimiento] = {}

    async def listar(self, cliente_id: str) -> list[Consentimiento]:
        return sorted(
            (c for (cid, _s), c in self._items.items() if cid == cliente_id),
            key=lambda c: str(c.scope),
        )

    async def obtener(
        self, cliente_id: str, scope: ConsentimientoScope
    ) -> Consentimiento | None:
        return self._items.get((cliente_id, str(scope)))

    async def guardar(self, consentimiento: Consentimiento) -> Consentimiento:
        self._items[(consentimiento.cliente_id, str(consentimiento.scope))] = consentimiento
        return consentimiento


class InMemoryIdempotencyStore:
    def __init__(self) -> None:
        self._data: dict[str, dict] = {}

    async def get(self, key: str) -> dict | None:
        return self._data.get(key)

    async def put(self, key: str, value: dict) -> None:
        self._data[key] = value


class InMemoryEventPublisher:
    def __init__(self) -> None:
        self.events: list[DomainEvent] = []

    async def publish(self, event: DomainEvent) -> None:
        self.events.append(event)
