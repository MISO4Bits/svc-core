"""Adaptador de eventos para local/desarrollo: escribe al log en vez de a Pub/Sub."""

from __future__ import annotations

import logging

from app.domain import DomainEvent

logger = logging.getLogger("svc_core.eventos")


class LoggingEventPublisher:
    async def publish(self, event: DomainEvent) -> None:
        # event.datos puede traer PII (email) — no se loguea tal cual.
        logger.info("evento_dominio: tipo=%s id=%s", event.tipo, event.id)
