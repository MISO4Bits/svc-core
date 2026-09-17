"""Instrumentación OpenTelemetry (DI-008): trazas, métricas y logs vía
OTLP/gRPC hacia el receptor de Grafana Alloy dentro del cluster, que a su vez
reenvía a Grafana Cloud. Ver ``iac-gcp-dev/modules/observability``.

A diferencia de bff-web/svc-cotizacion, este servicio no depende de httpx en
producción (no hace llamadas salientes síncronas a otros servicios), así que
no se instrumenta httpx aquí.
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Request
from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.semconv._incubating.attributes import code_attributes

from app.config import Settings

Telemetry = tuple[TracerProvider, MeterProvider, LoggerProvider]


class _OtelLoggingHandler(LoggingHandler):
    """``LoggingHandler`` de ``opentelemetry-sdk`` recortando el ruido que
    agrega ``_get_attributes()`` en cada log (``code.line.number``,
    ``code.file.path`` con la ruta absoluta dentro del contenedor) —
    ``_get_attributes`` es un ``staticmethod`` sin parámetro para
    desactivar esto, así que se sobreescribe.

    Nota: esta clase del SDK está deprecada (advierte usar
    ``opentelemetry-instrumentation-logging`` en su lugar) — no se migró
    todavía, es un cambio de paquete aparte, no algo para mezclar con
    este ajuste puntual de qué atributos exportar.
    """

    @staticmethod
    def _get_attributes(record: logging.LogRecord) -> dict:
        attributes = LoggingHandler._get_attributes(record)
        attributes.pop(code_attributes.CODE_LINE_NUMBER, None)
        ruta = attributes.get(code_attributes.CODE_FILE_PATH)
        if ruta:
            attributes[code_attributes.CODE_FILE_PATH] = os.path.basename(ruta)
        return attributes


class _AtributosDeTraza(logging.Filter):
    """Agrega ``trace_id``/``span_id`` como atributos del ``LogRecord``
    (no toca ``record.msg``) para que el ``Formatter`` del handler de
    OTel los incluya, en texto plano, en el cuerpo final del log."""

    def filter(self, record: logging.LogRecord) -> bool:
        contexto = trace.get_current_span().get_span_context()
        if contexto.is_valid:
            record.trace_id = format(contexto.trace_id, "032x")
            record.span_id = format(contexto.span_id, "016x")
        else:
            record.trace_id = "-"
            record.span_id = "-"
        return True


def setup_telemetry(app: FastAPI, settings: Settings) -> Telemetry | None:
    """Configura los proveedores del SDK e instrumenta FastAPI.

    Sin efecto si ``settings.otel_enabled`` es falso (default local/tests):
    el exportador por lotes no debe intentar conectarse a un receptor que no
    existe en ese contexto.
    """
    if not settings.otel_enabled:
        return None

    endpoint = settings.otel_exporter_endpoint
    resource = Resource.create({"service.name": settings.service_name})

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
    )
    trace.set_tracer_provider(tracer_provider)

    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[
            PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=endpoint, insecure=True))
        ],
    )
    metrics.set_meter_provider(meter_provider)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=endpoint, insecure=True))
    )
    set_logger_provider(logger_provider)
    otel_log_handler = _OtelLoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
    # Grafana Cloud ya recibe trace_id/span_id como campos propios del log
    # (LogRecord.trace_id/span_id, tomados del span activo) — pero eso solo
    # se ve al expandir el detalle de la línea en Loki, no permite un
    # "contiene" de texto plano ni aparece en el listado. Se agregan
    # también al texto del mensaje para poder buscarlos así.
    otel_log_handler.addFilter(_AtributosDeTraza())
    otel_log_handler.setFormatter(
        logging.Formatter("%(message)s trace_id=%(trace_id)s span_id=%(span_id)s")
    )
    logging.getLogger().addHandler(otel_log_handler)

    # uvicorn configura sus propios loggers ("uvicorn", "uvicorn.access",
    # "uvicorn.error") con propagate=False por defecto — sin esto, el
    # access log (incluidos los health checks) nunca llega al handler de
    # arriba, colgado del root logger. No se les agrega el handler
    # directamente para no duplicar sus propios logs de consola.
    for logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logging.getLogger(logger_name).propagate = True

    # /health lo golpean los probes cada 10-20s — es puro ruido para trazas
    # de negocio (no aporta nada a "cómo se comportó un endpoint real").
    FastAPIInstrumentor.instrument_app(
        app, tracer_provider=tracer_provider, excluded_urls="/health"
    )

    return tracer_provider, meter_provider, logger_provider


def shutdown_telemetry(telemetry: Telemetry | None) -> None:
    """Vacía los buffers pendientes al apagar la app (SIGTERM no espera al
    hilo del ``BatchSpanProcessor``, así que hay que forzar el flush)."""
    if telemetry is None:
        return
    for provider in telemetry:
        provider.shutdown()


def agregar_encabezado_trace_id(app: FastAPI) -> None:
    """Expone el ``trace_id`` de la petición como ``X-Trace-Id`` en la
    respuesta (trazabilidad distribuida, DI-008 — issue 4Bits BITS-92).

    Un mismo ``trace_id`` identifica la transacción completa a través de
    todos los servicios que la atienden (bff-web → svc-core, cada uno con
    sus propios spans); el ``span_id`` es propio de cada salto. bff-web ya
    propaga el ``traceparent`` (W3C Trace Context) en cada llamada
    saliente por httpx — con ``FastAPIInstrumentor`` activo aquí, este
    servicio extrae ese header automáticamente y continúa el mismo trace.

    Sin efecto si OTel está deshabilitado: no hay span activo, por lo que
    ``get_current_span()`` devuelve uno inválido y no se agrega el header.
    """

    @app.middleware("http")
    async def _trace_id_en_respuesta(request: Request, call_next):
        response = await call_next(request)
        contexto = trace.get_current_span().get_span_context()
        if contexto.is_valid:
            response.headers["X-Trace-Id"] = format(contexto.trace_id, "032x")
        return response
