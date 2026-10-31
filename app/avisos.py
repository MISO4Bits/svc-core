"""Avisos push (BITS-229): hermano de ``notificaciones.py`` para el canal del teléfono.

CoreTransaccional decide el aviso y arma el mensaje completo; lo publica como evento
``EnviarPush`` y la función ``fn-notificaciones`` solo lo entrega por Firebase Cloud Messaging.
El aviso complementa al correo con el PDF: no lo reemplaza.

Los textos no llevan montos ni datos de la póliza: se ven en la pantalla bloqueada.
"""

from __future__ import annotations

import logging

from app.domain import DomainEvent, Poliza
from app.ports import DispositivoRepository, EventPublisher

logger = logging.getLogger("svc_core.avisos")

EVENTO_ENVIAR_PUSH = "EnviarPush"
DESTINO_SEGURO = "seguro"
PRIORIDAD_ALTA = "alta"

TITULO_POLIZA_EMITIDA = "Tu póliza ya está activa"
CUERPO_POLIZA_EMITIDA = "Ya puedes ver tu seguro y descargar el documento."


class AvisoService:
    def __init__(self, dispositivos: DispositivoRepository, events: EventPublisher) -> None:
        self._dispositivos = dispositivos
        self._events = events

    async def avisar_poliza_emitida(self, poliza: Poliza) -> None:
        """Publica el aviso para todos los dispositivos del cliente. Si no tiene ninguno, no
        publica nada. Un fallo se propaga: quien llama decide que no afecta a la póliza."""
        dispositivos = await self._dispositivos.de_cliente(poliza.cliente_id)
        if not dispositivos:
            logger.info("aviso de póliza emitida omitido: el cliente no tiene dispositivos")
            return
        await self._events.publish(
            DomainEvent(
                EVENTO_ENVIAR_PUSH,
                {
                    "clienteId": poliza.cliente_id,
                    "tokens": [d.token for d in dispositivos],
                    "titulo": TITULO_POLIZA_EMITIDA,
                    "cuerpo": CUERPO_POLIZA_EMITIDA,
                    "ruta": {"destino": DESTINO_SEGURO, "id": poliza.id},
                    "prioridad": PRIORIDAD_ALTA,
                },
            )
        )
        logger.info(
            "aviso de póliza emitida publicado poliza_id=%s dispositivos=%d",
            poliza.id,
            len(dispositivos),
        )
