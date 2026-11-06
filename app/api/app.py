from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.adapters.factory import (
    build_avisos,
    build_biometria,
    build_cuentas_internas,
    build_dispositivos,
    build_event_publisher,
    build_evidencias,
    build_notificaciones,
    build_polizas,
    build_repositories,
    maybe_init,
)
from app.api.dispositivos import router as dispositivos_router
from app.api.errors import install_error_handlers
from app.api.evidencias import router as evidencias_router
from app.api.polizas import router as polizas_router
from app.api.routes import router
from app.api.usuarios_internos import router as usuarios_internos_router
from app.config import Settings, get_settings
from app.cuentas_internas import CuentasInternasService
from app.dispositivos import DispositivoService
from app.evidencias import EvidenciaService
from app.logging_utils import SinRuidoDeHealthCheck
from app.polizas import PolizaService
from app.services import IdentityService
from app.telemetry import agregar_encabezado_trace_id, setup_telemetry, shutdown_telemetry

SPEC_PATH = Path(__file__).resolve().parents[2] / "openapi" / "openapi.yaml"


semilla_logger = logging.getLogger("svc_core.semilla")


async def _sembrar_admin(servicio: CuentasInternasService, correo: str, contrasena: str) -> None:
    """Crea el administrador de la semilla. Un fallo solo se registra: se reintenta al reiniciar."""
    try:
        await servicio.asegurar_admin(correo, contrasena)
    except Exception as exc:
        # Ni el correo ni la contraseña: solo el tipo del error.
        semilla_logger.error(
            "no se pudo asegurar el administrador de la semilla (%s)", type(exc).__name__
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("uvicorn.access").addFilter(SinRuidoDeHealthCheck())

    clientes, consentimientos, idempotency = build_repositories(settings)
    events = build_event_publisher(settings)
    notificaciones, cerrables = build_notificaciones(settings, events)
    cuentas_internas, cerrables_cuentas = build_cuentas_internas(settings)
    repo_evidencias, almacen, verificador = build_evidencias(settings)
    repo_polizas, cotizaciones, documentos, cerrables_polizas = build_polizas(
        settings, repo_evidencias
    )
    cerrables = [*cerrables, *cerrables_polizas, *cerrables_cuentas]
    repo_dispositivos = build_dispositivos(settings)
    polizas = PolizaService(
        clientes,
        repo_evidencias,
        repo_polizas,
        cotizaciones,
        documentos,
        events,
        correos=notificaciones,
        avisos=build_avisos(settings, repo_dispositivos, events),
    )
    dispositivos = DispositivoService(clientes, repo_dispositivos)
    # Si el cliente cumple algo que le faltaba a su póliza pendiente, esta se vuelve a evaluar.
    service = IdentityService(
        clientes, consentimientos, events, al_confirmar=polizas.reevaluar_cliente
    )
    biometria = build_biometria(settings)
    evidencias = EvidenciaService(
        clientes,
        repo_evidencias,
        almacen,
        verificador,
        max_bytes=settings.evidencia_max_bytes,
        max_pixeles=settings.evidencia_max_pixeles,
        intentos_max=settings.evidencia_intentos_max,
        timeout_s=settings.verificador_timeout_s,
        biometria=biometria,
        biometria_timeout_s=settings.biometria_timeout_s,
        al_guardar=polizas.reevaluar_cliente,
    )

    consumidor = None
    if settings.consumidor_backend == "pubsub":  # pragma: no cover
        from app.adapters.pubsub_consumer import ConsumidorPubSub

        consumidor = ConsumidorPubSub(
            settings.pubsub_project_id or "", settings.pubsub_subscription, polizas
        )

    tareas_de_arranque: list[asyncio.Task] = []

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        for adapter in (
            clientes,
            consentimientos,
            idempotency,
            repo_evidencias,
            repo_polizas,
            repo_dispositivos,
        ):
            await maybe_init(adapter)
        precargar = getattr(biometria, "precargar", None)
        if precargar is not None:
            # Cargar los modelos ahora, no en la primera selfie (que agotaría el tiempo límite).
            await asyncio.to_thread(precargar)
        if consumidor is not None:
            consumidor.iniciar()  # pragma: no cover — requiere Pub/Sub real
        if cuentas_internas and settings.admin_seed_email and settings.admin_seed_password:
            # En segundo plano: Identity Platform lento o caído no debe tumbar el arranque.
            tareas_de_arranque.append(
                asyncio.create_task(
                    _sembrar_admin(
                        cuentas_internas,
                        settings.admin_seed_email,
                        settings.admin_seed_password.get_secret_value(),
                    )
                )
            )
        yield
        for tarea in tareas_de_arranque:
            tarea.cancel()
        if consumidor is not None:
            consumidor.detener()  # pragma: no cover — idem
        for adaptador in cerrables:
            await adaptador.aclose()
        shutdown_telemetry(telemetry)

    app = FastAPI(
        title="ICustomerIdentity — CoreTransaccional",
        version="0.1.0",
        lifespan=lifespan,
    )
    telemetry = setup_telemetry(app, settings)
    agregar_encabezado_trace_id(app)
    app.state.settings = settings
    app.state.service = service
    app.state.clientes = clientes
    app.state.consentimientos = consentimientos
    app.state.idempotency = idempotency
    app.state.events = events
    app.state.notificaciones = notificaciones
    app.state.evidencias = evidencias
    app.state.polizas = polizas
    app.state.dispositivos = dispositivos
    app.state.cuentas_internas = cuentas_internas

    install_error_handlers(app)
    app.include_router(router, prefix="/v1")
    app.include_router(evidencias_router, prefix="/v1")
    app.include_router(polizas_router, prefix="/v1")
    app.include_router(dispositivos_router, prefix="/v1")
    app.include_router(usuarios_internos_router, prefix="/v1")

    @app.get("/health", include_in_schema=False)
    async def health() -> dict:
        return {"status": "ok", "service": settings.service_name}

    if SPEC_PATH.exists():

        @app.get("/openapi.yaml", include_in_schema=False)
        async def openapi_yaml() -> FileResponse:
            return FileResponse(SPEC_PATH, media_type="application/yaml")

    return app
