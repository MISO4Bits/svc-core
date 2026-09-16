from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.telemetry import agregar_encabezado_trace_id, setup_telemetry, shutdown_telemetry


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
