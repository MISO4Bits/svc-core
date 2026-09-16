from __future__ import annotations

import logging

from fastapi import FastAPI

from app.config import Settings
from app.telemetry import setup_telemetry, shutdown_telemetry


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
