"""Adaptador de producción: publica eventos de dominio en Google Cloud Pub/Sub.

Tópico único compartido (ver iac-gcp-dev/modules/pubsub) — el tipo de evento
va como atributo del mensaje (``tipo``), no como tópico aparte; cada
consumidor filtra por ese atributo en su propia suscripción.

Fuera del alcance de las pruebas locales (requiere credenciales e infraestructura
GCP). Se excluye de la medición de cobertura.
"""

from __future__ import annotations

import asyncio
import json
import logging

from app.domain import DomainEvent

logger = logging.getLogger("svc_core.adapters.pubsub")


class PubSubEventPublisher:  # pragma: no cover
    def __init__(self, project_id: str, topic: str) -> None:
        from google.cloud import pubsub_v1

        self._publisher = pubsub_v1.PublisherClient()
        self._topic_path = self._publisher.topic_path(project_id, topic)

    async def publish(self, event: DomainEvent) -> None:
        payload = json.dumps(
            {
                "tipo": event.tipo,
                "id": event.id,
                "ocurridoEn": event.ocurrido_en.isoformat(),
                "datos": event.datos,
            }
        ).encode("utf-8")
        future = self._publisher.publish(self._topic_path, payload, tipo=event.tipo)
        # future.result() es bloqueante (API síncrona del cliente de
        # Pub/Sub) — se ejecuta en un hilo aparte para no congelar el loop
        # de asyncio mientras espera el ack del servidor.
        try:
            message_id = await asyncio.to_thread(future.result, timeout=10)
        except Exception:
            logger.exception("no se pudo publicar el evento tipo=%s id=%s", event.tipo, event.id)
            raise
        logger.info(
            "evento_publicado: tipo=%s id=%s message_id=%s", event.tipo, event.id, message_id
        )
