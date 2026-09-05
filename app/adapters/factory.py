"""Fábrica simple de adaptadores a partir de la configuración (sin framework de DI)."""

from __future__ import annotations

from app.adapters.events import LoggingEventPublisher
from app.adapters.memory import (
    InMemoryClienteRepository,
    InMemoryConsentimientoRepository,
    InMemoryEventPublisher,
    InMemoryIdempotencyStore,
)
from app.adapters.sqlite import (
    SqliteClienteRepository,
    SqliteConsentimientoRepository,
    SqliteDatabase,
    SqliteIdempotencyStore,
)
from app.config import Settings
from app.ports import (
    ClienteRepository,
    ConsentimientoRepository,
    EventPublisher,
    IdempotencyStore,
)


def build_repositories(
    settings: Settings,
) -> tuple[ClienteRepository, ConsentimientoRepository, IdempotencyStore]:
    if settings.repository_backend == "memory":
        return (
            InMemoryClienteRepository(),
            InMemoryConsentimientoRepository(),
            InMemoryIdempotencyStore(),
        )
    if settings.repository_backend == "sqlite":
        db = SqliteDatabase(settings.database_path)
        return (
            SqliteClienteRepository(db),
            SqliteConsentimientoRepository(db),
            SqliteIdempotencyStore(db),
        )
    raise ValueError(f"repository_backend no soportado: {settings.repository_backend}")


def build_event_publisher(settings: Settings) -> EventPublisher:
    if settings.event_backend == "memory":
        return InMemoryEventPublisher()
    if settings.event_backend == "pubsub":  # pragma: no cover
        from app.adapters.pubsub import PubSubEventPublisher

        return PubSubEventPublisher(
            settings.pubsub_project_id or "", settings.pubsub_topic
        )
    return LoggingEventPublisher()


async def maybe_init(obj: object) -> None:
    """Inicializa el esquema si el adaptador está respaldado por SQLite."""
    db = getattr(obj, "_db", None)
    if isinstance(db, SqliteDatabase):
        await db.init()
