from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.telemetry import (
    _AtributosDeTraza,
    agregar_encabezado_trace_id,
    setup_telemetry,
    shutdown_telemetry,
)


def _settings(**overrides) -> Settings:
    return Settings(**overrides)


def test_setup_telemetry_deshabilitado_no_hace_nada():
    app = FastAPI()
    telemetry = setup_telemetry(app, _settings(otel_enabled=False))

    assert telemetry is None
    shutdown_telemetry(telemetry)  # no debe lanzar con None


def test_setup_telemetry_habilitado_instrumenta_la_app():
    app = FastAPI()
    telemetry = setup_telemetry(app, _settings(otel_enabled=True))

    try:
        assert telemetry is not None
        tracer_provider, meter_provider, logger_provider = telemetry
        assert tracer_provider is not None
        assert meter_provider is not None
        assert logger_provider is not None
    finally:
        shutdown_telemetry(telemetry)


def test_setup_telemetry_habilita_propagacion_de_logs_de_uvicorn():
    for logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logging.getLogger(logger_name).propagate = False

    app = FastAPI()
    telemetry = setup_telemetry(app, _settings(otel_enabled=True))

    try:
        for logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
            assert logging.getLogger(logger_name).propagate is True
    finally:
        shutdown_telemetry(telemetry)


def test_agrega_x_trace_id_cuando_hay_un_span_activo():
    app = FastAPI()
    telemetry = setup_telemetry(app, _settings(otel_enabled=True))
    agregar_encabezado_trace_id(app)

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    try:
        resp = TestClient(app).get("/ping")
        assert "X-Trace-Id" in resp.headers
        trace_id = resp.headers["X-Trace-Id"]
        assert len(trace_id) == 32
        int(trace_id, 16)  # es hexadecimal válido
    finally:
        shutdown_telemetry(telemetry)


def test_no_agrega_x_trace_id_sin_otel_habilitado():
    app = FastAPI()
    telemetry = setup_telemetry(app, _settings(otel_enabled=False))
    agregar_encabezado_trace_id(app)

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    resp = TestClient(app).get("/ping")

    assert "X-Trace-Id" not in resp.headers
    shutdown_telemetry(telemetry)


def _handler_de_prueba() -> logging.Handler:
    handler = logging.NullHandler()
    handler.addFilter(_AtributosDeTraza())
    handler.setFormatter(logging.Formatter("%(message)s trace_id=%(trace_id)s span_id=%(span_id)s"))
    return handler


def _registro(mensaje: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=mensaje,
        args=None,
        exc_info=None,
    )


def test_el_texto_del_log_trae_trace_id_y_span_id_sin_span_activo():
    handler = _handler_de_prueba()
    record = _registro("cliente creado")

    handler.filter(record)
    texto = handler.format(record)

    assert texto == "cliente creado trace_id=- span_id=-"


def test_el_texto_del_log_trae_trace_id_y_span_id_reales_con_span_activo():
    app = FastAPI()
    telemetry = setup_telemetry(app, _settings(otel_enabled=True))
    handler = _handler_de_prueba()
    record = _registro("cliente creado")

    try:
        tracer_provider, _, _ = telemetry
        with tracer_provider.get_tracer(__name__).start_as_current_span("span-de-prueba"):
            handler.filter(record)
            texto = handler.format(record)
    finally:
        shutdown_telemetry(telemetry)

    assert texto.startswith("cliente creado trace_id=")
    resto, span_parte = texto.rsplit(" span_id=", 1)
    trace_id = resto.removeprefix("cliente creado trace_id=")
    span_id = span_parte
    assert len(trace_id) == 32
    assert len(span_id) == 16
    int(trace_id, 16)
    int(span_id, 16)
