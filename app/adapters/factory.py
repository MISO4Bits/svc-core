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
from app.notificaciones import NotificacionService
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
        consentimientos = InMemoryConsentimientoRepository()
        return (
            InMemoryClienteRepository(consentimientos),
            consentimientos,
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

        return PubSubEventPublisher(settings.pubsub_project_id or "", settings.pubsub_topic)
    return LoggingEventPublisher()


def build_notificaciones(
    settings: Settings, events: EventPublisher
) -> tuple[NotificacionService | None, list[object]]:
    """Servicio de correos del registro y los adaptadores que hay que cerrar al apagar.

    ``off`` (por defecto) no envía nada: local y pruebas no necesitan Productos ni
    Identity Platform.
    """
    if settings.notificaciones_backend == "off":
        return None, []
    if settings.notificaciones_backend == "fake":
        from app.adapters.notificaciones_fake import FakeIdentidadAdmin, FakePlantillasCorreo

        plantillas, identidad = FakePlantillasCorreo(), FakeIdentidadAdmin()
        cerrables: list[object] = []
    elif settings.notificaciones_backend == "real":
        from app.adapters.identity_admin import IdentityPlatformAdmin, token_de_servicio
        from app.adapters.productos_http import ProductosPlantillasHttp

        if not settings.identity_project_id:
            raise ValueError("CORE_IDENTITY_PROJECT_ID es obligatorio con notificaciones reales")
        plantillas = ProductosPlantillasHttp(
            settings.productos_base_url, settings.productos_timeout_s
        )
        identidad = IdentityPlatformAdmin(settings.identity_project_id, token_de_servicio())
        cerrables = [plantillas, identidad]
    else:
        raise ValueError(f"notificaciones_backend no soportado: {settings.notificaciones_backend}")
    servicio = NotificacionService(
        plantillas,
        identidad,
        events,
        url_web=settings.web_url_base,
        url_verificacion=settings.web_url_verificacion,
        mercado=settings.correo_mercado,
        idioma=settings.correo_idioma,
    )
    return servicio, cerrables


async def maybe_init(obj: object) -> None:
    """Inicializa el esquema si el adaptador está respaldado por SQLite."""
    db = getattr(obj, "_db", None)
    if isinstance(db, SqliteDatabase):
        await db.init()
