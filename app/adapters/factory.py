"""Fábrica simple de adaptadores a partir de la configuración (sin framework de DI)."""

from __future__ import annotations

from app.adapters.almacen import FilesystemAlmacenEvidencias, InMemoryAlmacenEvidencias
from app.adapters.events import LoggingEventPublisher
from app.adapters.memory import (
    InMemoryClienteRepository,
    InMemoryConsentimientoRepository,
    InMemoryDispositivoRepository,
    InMemoryEventPublisher,
    InMemoryEvidenciaRepository,
    InMemoryIdempotencyStore,
    InMemoryPolizaRepository,
    InMemorySiniestroRepository,
)
from app.adapters.sqlite import (
    SqliteClienteRepository,
    SqliteConsentimientoRepository,
    SqliteDatabase,
    SqliteDispositivoRepository,
    SqliteEvidenciaRepository,
    SqliteIdempotencyStore,
    SqlitePolizaRepository,
    SqliteSiniestroRepository,
)
from app.adapters.verificador_mock import VerificadorBiometricoMock, VerificadorMock
from app.avisos import AvisoService
from app.calidad_imagen import UmbralesCalidad
from app.config import Settings
from app.cuentas_internas import CuentasInternasService
from app.notificaciones import NotificacionService
from app.ports import (
    AlmacenEvidencias,
    ClienteRepository,
    ConsentimientoRepository,
    CotizacionPort,
    CreditosPort,
    CuentasClientePort,
    DispositivoRepository,
    DocumentoPolizaPort,
    DocumentosLegalesPort,
    EventPublisher,
    EvidenciaRepository,
    IdempotencyStore,
    PolizaRepository,
    SiniestroRepository,
    VerificadorBiometrico,
    VerificadorDocumento,
)
from app.siniestros import SiniestroService


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


def build_biometria(settings: Settings) -> VerificadorBiometrico:
    """Verificador de la selfie (prueba de vida y comparación con la cédula)."""
    if settings.biometria_backend == "mock":
        return VerificadorBiometricoMock(settings.biometria_mock_secuencia)
    if settings.biometria_backend == "local":
        # Import diferido, como el verificador de documentos: cargar OpenCV solo si se usa.
        from app.adapters.verificador_biometrico import VerificadorBiometricoLocal
        from app.biometria import UmbralesBiometria

        return VerificadorBiometricoLocal(
            UmbralesCalidad.desde(settings),
            UmbralesBiometria.desde(settings),
            ruta_modelos=settings.biometria_modelos_ruta,
            max_concurrentes=settings.biometria_max_concurrentes,
        )
    raise ValueError(f"biometria_backend no soportado: {settings.biometria_backend}")


def build_polizas(
    settings: Settings, repo_evidencias: EvidenciaRepository
) -> tuple[PolizaRepository, CotizacionPort, DocumentoPolizaPort, list[object]]:
    """Repositorio de pólizas, cliente de Cotización y del documento, y lo que hay que cerrar."""
    if settings.repository_backend == "memory":
        # Emitir consume las evidencias: el repositorio en memoria necesita el mismo de evidencias.
        assert isinstance(repo_evidencias, InMemoryEvidenciaRepository)
        repositorio: PolizaRepository = InMemoryPolizaRepository(repo_evidencias)
    elif settings.repository_backend == "sqlite":
        repositorio = SqlitePolizaRepository(SqliteDatabase(settings.database_path))
    else:
        raise ValueError(f"repository_backend no soportado: {settings.repository_backend}")

    cerrables: list[object] = []
    if settings.cotizacion_backend == "fake":
        from app.adapters.polizas_fake import CotizacionFake

        cotizaciones: CotizacionPort = CotizacionFake()
    elif settings.cotizacion_backend == "http":
        from app.adapters.cotizacion_http import CotizacionHttp

        http = CotizacionHttp(settings.cotizacion_base_url, settings.cotizacion_timeout_s)
        cotizaciones = http
        cerrables.append(http)
    else:
        raise ValueError(f"cotizacion_backend no soportado: {settings.cotizacion_backend}")

    if settings.documento_backend == "fake":
        from app.adapters.polizas_fake import DocumentoPolizaFake

        documentos: DocumentoPolizaPort = DocumentoPolizaFake()
    elif settings.documento_backend == "http":
        from app.adapters.documento_http import DocumentoPolizaHttp

        if not settings.documento_base_url:
            raise ValueError("CORE_DOCUMENTO_BASE_URL es obligatorio con documento_backend=http")
        servicio = DocumentoPolizaHttp(settings.documento_base_url, settings.documento_timeout_s)
        documentos = servicio
        cerrables.append(servicio)
    else:
        raise ValueError(f"documento_backend no soportado: {settings.documento_backend}")
    return repositorio, cotizaciones, documentos, cerrables


def build_documentos_legales(settings: Settings) -> tuple[DocumentosLegalesPort, list[object]]:
    """Versión vigente de las condiciones de la póliza (venta directa del socio, BITS-378)."""
    if settings.documentos_legales_backend == "fake":
        from app.adapters.polizas_fake import DocumentosLegalesFake

        return DocumentosLegalesFake(), []
    if settings.documentos_legales_backend == "http":
        from app.adapters.documentos_legales_http import DocumentosLegalesHttp

        http = DocumentosLegalesHttp(
            settings.productos_base_url,
            settings.correo_mercado,
            settings.correo_idioma,
            settings.productos_timeout_s,
        )
        return http, [http]
    raise ValueError(
        f"documentos_legales_backend no soportado: {settings.documentos_legales_backend}"
    )


def build_siniestros(
    settings: Settings,
    clientes: ClienteRepository,
    polizas: PolizaRepository,
    almacen: AlmacenEvidencias,
    idempotency: IdempotencyStore,
    avisos: AvisoService | None = None,
) -> tuple[SiniestroService, list[object]]:
    """Servicio de siniestros y los adaptadores que hay que cerrar al apagar (BITS-381)."""
    if settings.repository_backend == "memory":
        repositorio: SiniestroRepository = InMemorySiniestroRepository()
    elif settings.repository_backend == "sqlite":
        repositorio = SqliteSiniestroRepository(SqliteDatabase(settings.database_path))
    else:
        raise ValueError(f"repository_backend no soportado: {settings.repository_backend}")

    cerrables: list[object] = []
    if settings.creditos_backend == "fake":
        from app.adapters.creditos_fake import CreditosFake

        creditos: CreditosPort = CreditosFake()
    elif settings.creditos_backend == "http":
        from app.adapters.perfilamiento_http import PerfilamientoHttp

        http = PerfilamientoHttp(settings.perfilamiento_base_url, settings.perfilamiento_timeout_s)
        creditos = http
        cerrables.append(http)
    else:
        raise ValueError(f"creditos_backend no soportado: {settings.creditos_backend}")

    servicio = SiniestroService(
        clientes,
        polizas,
        repositorio,
        almacen,
        creditos,
        idempotency,
        max_bytes=settings.evidencia_max_bytes,
        max_pixeles=settings.evidencia_max_pixeles,
        avisos=avisos,
    )
    return servicio, cerrables


def build_dispositivos(settings: Settings) -> DispositivoRepository:
    """Dispositivos registrados para avisos push (BITS-229)."""
    if settings.repository_backend == "memory":
        return InMemoryDispositivoRepository()
    if settings.repository_backend == "sqlite":
        return SqliteDispositivoRepository(SqliteDatabase(settings.database_path))
    raise ValueError(f"repository_backend no soportado: {settings.repository_backend}")


def build_avisos(
    settings: Settings, dispositivos: DispositivoRepository, events: EventPublisher
) -> AvisoService | None:
    """Servicio de avisos push. ``off`` (por defecto) no avisa: local y pruebas no lo necesitan."""
    if settings.push_backend == "off":
        return None
    if settings.push_backend == "on":
        return AvisoService(dispositivos, events)
    raise ValueError(f"push_backend no soportado: {settings.push_backend}")


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


def build_cuentas_internas(
    settings: Settings,
) -> tuple[CuentasInternasService | None, list[object]]:
    """Servicio de cuentas del personal y los adaptadores que hay que cerrar al apagar.

    ``off`` (por defecto) no construye nada: el endpoint responde 503."""
    if settings.cuentas_internas_backend == "off":
        return None, []
    if settings.cuentas_internas_backend == "fake":
        from app.adapters.cuentas_fake import CuentasInternasEnMemoria

        return CuentasInternasService(CuentasInternasEnMemoria()), []
    if settings.cuentas_internas_backend == "real":
        from app.adapters.identity_admin import IdentityPlatformAdmin, token_de_servicio

        if not settings.identity_project_id:
            raise ValueError("CORE_IDENTITY_PROJECT_ID es obligatorio con cuentas internas reales")
        identidad = IdentityPlatformAdmin(settings.identity_project_id, token_de_servicio())
        return CuentasInternasService(identidad), [identidad]
    raise ValueError(f"cuentas_internas_backend no soportado: {settings.cuentas_internas_backend}")


def build_cuentas_cliente(settings: Settings) -> tuple[CuentasClientePort | None, list[object]]:
    """Cuentas de los clientes que da de alta un socio (BITS-378). Sigue a
    ``cuentas_internas_backend``: ``off`` no construye nada y el alta responde 503."""
    if settings.cuentas_internas_backend == "off":
        return None, []
    if settings.cuentas_internas_backend == "fake":
        from app.adapters.cuentas_fake import CuentasClienteEnMemoria

        return CuentasClienteEnMemoria(), []
    if settings.cuentas_internas_backend == "real":
        from app.adapters.identity_admin import IdentityPlatformAdmin, token_de_servicio

        if not settings.identity_project_id:
            raise ValueError("CORE_IDENTITY_PROJECT_ID es obligatorio con cuentas reales")
        identidad = IdentityPlatformAdmin(settings.identity_project_id, token_de_servicio())
        return identidad, [identidad]
    raise ValueError(f"cuentas_internas_backend no soportado: {settings.cuentas_internas_backend}")


async def maybe_init(obj: object) -> None:
    """Inicializa el esquema si el adaptador está respaldado por SQLite."""
    db = getattr(obj, "_db", None)
    if isinstance(db, SqliteDatabase):
        await db.init()
