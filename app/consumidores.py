"""Despacho de los eventos de dominio que Core consume desde Pub/Sub.

Aislado de la mecánica de transporte (``adapters/pubsub_consumer.py``) a propósito: esta función
es pura respecto del evento ya decodificado, así que se prueba sin credenciales de GCP.

Cotización publica ``CotizacionCreada`` al guardar una cotización y ``CotizacionEliminada`` al
borrarla. En ambos casos las pólizas ``PENDIENTE`` del cliente de una cotización anterior no
llegaron a feliz término y se borran (BITS-101).

fn-documentos publica ``DocumentoPolizaGenerado`` cuando el PDF de una póliza está en el bucket:
Core encarga entonces el correo con el archivo adjunto.
"""

from __future__ import annotations

import logging
from datetime import datetime

from app.domain import DomainEvent
from app.logging_utils import sanear_para_log
from app.polizas import PolizaService

logger = logging.getLogger("svc_core.consumidores")

TIPOS_QUE_CONSUME = frozenset(
    {"CotizacionCreada", "CotizacionEliminada", "DocumentoPolizaGenerado"}
)


async def despachar_evento(polizas: PolizaService, evento: DomainEvent) -> None:
    if evento.tipo == "CotizacionCreada":
        cliente_id = evento.datos["clienteId"]
        logger.info("despachar_evento: CotizacionCreada cliente_id=%s", sanear_para_log(cliente_id))
        await polizas.descartar_pendientes(
            cliente_id, datetime.fromisoformat(evento.datos["creadaEn"])
        )
        return
    if evento.tipo == "CotizacionEliminada":
        cliente_id = evento.datos["clienteId"]
        logger.info(
            "despachar_evento: CotizacionEliminada cliente_id=%s", sanear_para_log(cliente_id)
        )
        await polizas.descartar_pendientes(cliente_id, None)
        return

    if evento.tipo == "DocumentoPolizaGenerado":
        try:
            await polizas.notificar_documento(evento.datos)
        except KeyError as exc:
            # Un aviso sin los campos que necesita no se arregla reintentando: se descarta.
            logger.error("despachar_evento: DocumentoPolizaGenerado sin el campo %s", exc)
        return

    # El filtro de la suscripción deja pasar solo estos tipos: llegar aquí es señal de que el
    # filtro y este despacho se desalinearon.
    logger.warning("despachar_evento: tipo de evento no reconocido tipo=%s", evento.tipo)
