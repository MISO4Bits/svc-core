"""Avisos push (BITS-229): hermano de ``notificaciones.py`` para el canal del teléfono.

CoreTransaccional decide el aviso y arma el mensaje completo; lo publica como evento
``EnviarPush`` y la función ``fn-notificaciones`` solo lo entrega por Firebase Cloud Messaging.
El aviso complementa al correo con el PDF: no lo reemplaza.

Los textos no llevan montos ni datos de la póliza: se ven en la pantalla bloqueada. Todos salen por
``avisar``, con la tabla de textos de abajo; cada módulo nuevo agrega los suyos.
"""

from __future__ import annotations

import logging
from enum import StrEnum

from app.domain import DomainEvent, Poliza
from app.ports import DispositivoRepository, EventPublisher

logger = logging.getLogger("svc_core.avisos")

EVENTO_ENVIAR_PUSH = "EnviarPush"
DESTINO_SEGURO = "seguro"
PRIORIDAD_ALTA = "alta"

TITULO_POLIZA_EMITIDA = "Tu póliza ya está activa"
CUERPO_POLIZA_EMITIDA = "Ya puedes ver tu seguro y descargar el documento."


class Aviso(StrEnum):
    POLIZA_EMITIDA = "POLIZA_EMITIDA"
    SINIESTRO_FALTA_INFORMACION = "SINIESTRO_FALTA_INFORMACION"
    SINIESTRO_RECHAZADO = "SINIESTRO_RECHAZADO"
    SINIESTRO_PAGADO = "SINIESTRO_PAGADO"
    SINIESTRO_PAGO_AUTOMATICO = "SINIESTRO_PAGO_AUTOMATICO"


# (título, cuerpo) de cada aviso. Sin montos: se leen con el teléfono bloqueado.
TEXTOS: dict[Aviso, tuple[str, str]] = {
    Aviso.POLIZA_EMITIDA: (TITULO_POLIZA_EMITIDA, CUERPO_POLIZA_EMITIDA),
    Aviso.SINIESTRO_FALTA_INFORMACION: (
        "Necesitamos algo más para tu siniestro",
        "Abre tu seguro para ver qué falta y enviarlo.",
    ),
    Aviso.SINIESTRO_RECHAZADO: (
        "Tenemos una respuesta sobre tu siniestro",
        "Abre tu seguro para conocerla.",
    ),
    Aviso.SINIESTRO_PAGADO: (
        "Pagamos tu siniestro",
        "Abre tu seguro para ver el detalle del pago.",
    ),
    Aviso.SINIESTRO_PAGO_AUTOMATICO: (
        "Te pagamos solos",
        "Un sismo afectó tu municipio y ya pagamos la cuota de tu crédito.",
    ),
}


def ruta_seguro(poliza_id: str) -> dict:
    """Destino que abre el detalle del seguro en la app."""
    return {"destino": DESTINO_SEGURO, "id": poliza_id}


class AvisoService:
    def __init__(self, dispositivos: DispositivoRepository, events: EventPublisher) -> None:
        self._dispositivos = dispositivos
        self._events = events

    async def avisar(self, cliente_id: str, aviso: Aviso, ruta: dict | None = None) -> None:
        """Publica el aviso para todos los dispositivos del cliente. Si no tiene ninguno, no
        publica nada. Un fallo se propaga: quien llama decide que no afecta a su operación."""
        dispositivos = await self._dispositivos.de_cliente(cliente_id)
        if not dispositivos:
            logger.info("aviso %s omitido: el cliente no tiene dispositivos", aviso)
            return
        titulo, cuerpo = TEXTOS[aviso]
        payload: dict = {
            "clienteId": cliente_id,
            "tokens": [d.token for d in dispositivos],
            "titulo": titulo,
            "cuerpo": cuerpo,
            "prioridad": PRIORIDAD_ALTA,
        }
        if ruta is not None:
            payload["ruta"] = ruta
        await self._events.publish(DomainEvent(EVENTO_ENVIAR_PUSH, payload))
        logger.info("aviso %s publicado dispositivos=%d", aviso, len(dispositivos))

    async def avisar_poliza_emitida(self, poliza: Poliza) -> None:
        await self.avisar(poliza.cliente_id, Aviso.POLIZA_EMITIDA, ruta_seguro(poliza.id))
