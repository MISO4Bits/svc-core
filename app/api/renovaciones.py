"""Trabajo diario de renovación (BITS-387). Se monta bajo ``/v1``.

Interno y sin BFF: lo llama el CronJob de Kubernetes de ``deploy`` una vez al día, y una
``NetworkPolicy`` solo deja entrar a ese pod. En ``dev`` acepta una fecha de corte simulada, para
demostrar las tres pasadas sin esperar un año.
"""

from __future__ import annotations

import logging
from datetime import datetime, time

from fastapi import APIRouter, Request

from app.api.polizas import RenovacionesDep
from app.api.schemas import EjecucionRenovacionesOut, EjecutarRenovacionesRequest
from app.domain import ZONA_COLOMBIA, SolicitudPolizaInvalida

logger = logging.getLogger("svc_core.api")
router = APIRouter(tags=["Renovaciones"])


@router.post("/renovaciones/ejecutar", response_model=EjecucionRenovacionesOut)
async def ejecutar_renovaciones(
    request: Request,
    renovaciones: RenovacionesDep,
    payload: EjecutarRenovacionesRequest | None = None,
) -> EjecucionRenovacionesOut:
    fecha = payload.fecha_corte if payload else None
    logger.info("POST /v1/renovaciones/ejecutar: solicitud recibida simulada=%s", fecha is not None)
    corte = None
    if fecha is not None:
        if request.app.state.settings.environment != "dev":
            raise SolicitudPolizaInvalida(
                "fechaCorte", "La fecha de corte simulada solo se acepta en dev"
            )
        # Mediodía de Colombia: lejos de los bordes del día en cualquier zona horaria.
        corte = datetime.combine(fecha, time(12), tzinfo=ZONA_COLOMBIA)
    resumen = await renovaciones.ejecutar(corte)
    return EjecucionRenovacionesOut(
        corte=resumen.corte,
        avisos_vencimiento=resumen.avisos_vencimiento,
        avisos_banco=resumen.avisos_banco,
        renovadas=resumen.renovadas,
        desactivadas=resumen.desactivadas,
    )
