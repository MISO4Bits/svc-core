"""Fábrica simple de adaptadores a partir de la configuración (sin framework de DI)."""

from __future__ import annotations

from app.adapters.almacen import FilesystemAlmacenEvidencias, InMemoryAlmacenEvidencias
from app.adapters.events import LoggingEventPublisher
from app.adapters.memory import (
    InMemoryClienteRepository,
    InMemoryConsentimientoRepository,
    InMemoryEventPublisher,
    InMemoryEvidenciaRepository,
    InMemoryIdempotencyStore,
)
from app.adapters.sqlite import (
    SqliteClienteRepository,
    SqliteConsentimientoRepository,
    SqliteDatabase,
    SqliteEvidenciaRepository,
    SqliteIdempotencyStore,
)
from app.adapters.verificador_mock import VerificadorMock
from app.calidad_imagen import UmbralesCalidad
from app.config import Settings
from app.notificaciones import NotificacionService
from app.ports import (
    AlmacenEvidencias,
    ClienteRepository,
    ConsentimientoRepository,
    EventPublisher,
    EvidenciaRepository,
    IdempotencyStore,
    VerificadorDocumento,
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


def build_evidencias(
    settings: Settings,
) -> tuple[EvidenciaRepository, AlmacenEvidencias, VerificadorDocumento]:
    """Repositorio, almacén de imágenes y verificador de las evidencias de identidad."""
    if settings.repository_backend == "memory":
        repositorio: EvidenciaRepository = InMemoryEvidenciaRepository()
    elif settings.repository_backend == "sqlite":
        repositorio = SqliteEvidenciaRepository(SqliteDatabase(settings.database_path))
    else:
        raise ValueError(f"repository_backend no soportado: {settings.repository_backend}")

    if settings.almacen_backend == "memory":
        almacen: AlmacenEvidencias = InMemoryAlmacenEvidencias()
    elif settings.almacen_backend == "filesystem":
        almacen = FilesystemAlmacenEvidencias(settings.almacen_ruta)
    elif settings.almacen_backend == "gcs":
        from app.adapters.gcs import GcsAlmacenEvidencias

        if not settings.almacen_bucket:
            raise ValueError("CORE_ALMACEN_BUCKET es obligatorio con almacen_backend=gcs")
        almacen = GcsAlmacenEvidencias(settings.almacen_bucket)
    else:
        raise ValueError(f"almacen_backend no soportado: {settings.almacen_backend}")

    if settings.verificador_backend == "mock":
        verificador: VerificadorDocumento = VerificadorMock(settings.verificador_mock_secuencia)
    elif settings.verificador_backend == "local":
        # Import diferido: cargar OpenCV y zxing solo cuando se usa el verificador real.
        from app.adapters.verificador_local import VerificadorLocal

        verificador = VerificadorLocal(
            UmbralesCalidad.desde(settings),
            max_concurrentes=settings.verificador_max_concurrentes,
        )
    else:
        raise ValueError(f"verificador_backend no soportado: {settings.verificador_backend}")
    return repositorio, almacen, verificador


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
