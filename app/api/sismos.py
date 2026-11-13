"""Aviso de sismo simulado (BITS-386). Se monta bajo ``/v1``.

Interno y sin BFF: lo llama quien simula al Servicio Geológico Colombiano (Postman). Hace de
ingesta de un evento externo: valida el cuerpo, publica ``SismoReportado`` en ``solventa-dominio``
y responde ``202``. El pago lo hace el consumidor de Core (``app.sismos``), no esta petición.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, status

from app.api.schemas import SismoAceptadoOut, SismoRequest
from app.domain import DomainEvent, SolicitudSiniestroInvalida
from app.logging_utils import sanear_para_log
from app.siniestros import hoy_en_colombia

logger = logging.getLogger("svc_core.api")
router = APIRouter(tags=["Sismos"])


@router.post(
    "/eventos-sismicos",
    response_model=SismoAceptadoOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reportar_sismo(payload: SismoRequest, request: Request) -> SismoAceptadoOut:
    logger.info(
        "POST /v1/eventos-sismicos: solicitud recibida evento_id=%s magnitud=%s",
        sanear_para_log(payload.evento_id),
        payload.magnitud,
    )
    if payload.fecha > hoy_en_colombia():
        raise SolicitudSiniestroInvalida("fecha", "La fecha del sismo no puede ser futura")
    await request.app.state.events.publish(
        DomainEvent(
            "SismoReportado",
            {
                "eventoId": payload.evento_id,
                "municipio": payload.municipio,
                "magnitud": payload.magnitud,
                "fecha": payload.fecha.isoformat(),
            },
        )
    )
    return SismoAceptadoOut(evento_id=payload.evento_id)
