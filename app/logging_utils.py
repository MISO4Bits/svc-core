"""Utilidades de logging compartidas por el bootstrap de la app."""

from __future__ import annotations

import logging


class SinRuidoDeHealthCheck(logging.Filter):
    """Descarta el access log de uvicorn para ``/health``.

    Los probes de Kubernetes lo golpean cada 10-20s — no aporta nada para
    entender el comportamiento de un endpoint de negocio y ahoga, tanto en
    consola como en Grafana Cloud, los logs que sí importan (mismo criterio
    aplicado a las trazas en ``telemetry.py`` vía ``excluded_urls``).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access llama a logger.info(fmt, client_addr, method,
        # full_path, http_version, status_code) — full_path es args[2].
        return not (record.args and len(record.args) >= 3 and record.args[2] == "/health")


def sanear_para_log(valor: str) -> str:
    """Quita saltos de línea de un valor que viene del cliente (path o
    query param, sin validar contra un formato cerrado) antes de
    escribirlo en un log.

    Sin esto, un valor como ``"a%0d%0aINFO: cuenta admin creada"`` en la
    URL permite falsificar líneas de log completas (CWE-117 / log
    injection, pythonsecurity:S5145).
    """
    return valor.replace("\r", "").replace("\n", "")
