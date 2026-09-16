"""Instrumentación OpenTelemetry (DI-008): trazas, métricas y logs vía
OTLP/gRPC hacia el receptor de Grafana Alloy dentro del cluster, que a su vez
reenvía a Grafana Cloud. Ver ``iac-gcp-dev/modules/observability``.

A diferencia de bff-web/svc-cotizacion, este servicio no depende de httpx en
producción (no hace llamadas salientes síncronas a otros servicios), así que
no se instrumenta httpx aquí.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
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

from app.config import Settings

Telemetry = tuple[TracerProvider, MeterProvider, LoggerProvider]


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
    logging.getLogger().addHandler(
        LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
    )

    # uvicorn configura sus propios loggers ("uvicorn", "uvicorn.access",
    # "uvicorn.error") con propagate=False por defecto — sin esto, el
    # access log (incluidos los health checks) nunca llega al handler de
    # arriba, colgado del root logger. No se les agrega el handler
    # directamente para no duplicar sus propios logs de consola.
    for logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logging.getLogger(logger_name).propagate = True

    FastAPIInstrumentor.instrument_app(app, tracer_provider=tracer_provider)

    return tracer_provider, meter_provider, logger_provider


def shutdown_telemetry(telemetry: Telemetry | None) -> None:
    """Vacía los buffers pendientes al apagar la app (SIGTERM no espera al
    hilo del ``BatchSpanProcessor``, así que hay que forzar el flush)."""
    if telemetry is None:
        return
    for provider in telemetry:
        provider.shutdown()
