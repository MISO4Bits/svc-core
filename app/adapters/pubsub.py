"""Adaptador de producción: publica eventos de dominio en Google Cloud Pub/Sub.

Fuera del alcance de las pruebas locales (requiere credenciales e infraestructura
GCP). Se excluye de la medición de cobertura.
"""

from __future__ import annotations

import json

from app.domain import DomainEvent


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
        self._publisher.publish(self._topic_path, payload, tipo=event.tipo)
