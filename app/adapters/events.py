"""Adaptador de eventos para local/desarrollo: escribe al log en vez de a Pub/Sub."""

from __future__ import annotations

import logging

from app.domain import DomainEvent

logger = logging.getLogger("svc_core.eventos")


class LoggingEventPublisher:
    async def publish(self, event: DomainEvent) -> None:
        logger.info(
            "evento_dominio tipo=%s id=%s datos=%s",
            event.tipo,
            event.id,
            event.datos,
        )
