"""Bandeja de siniestros del ``ADMIN`` (BITS-377). Se montan bajo ``/v1``.

Interno y sin gateway: solo lo llama bff-web, desde las rutas que exigen el rol ``ADMIN``. El
``agente`` de una decisión es el usuario de la sesión, que llena el BFF.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.api.schemas import (
    DecidirSiniestroRequest,
    DecisionSiniestroOut,
    PolizaDelSiniestroOut,
    SaldoCreditoOut,
    SiniestroInternoOut,
    SiniestroInternoResumenOut,
    SiniestroOut,
)
from app.api.siniestros import SiniestrosDep
from app.domain import CodigoCobertura, EstadoSiniestro, Poliza, Siniestro
from app.logging_utils import sanear_para_log
from app.siniestros import DecisionEntrada

logger = logging.getLogger("svc_core.api")
router = APIRouter()


def _interno(siniestro: Siniestro, poliza: Poliza) -> SiniestroInternoOut:
    """El detalle del ``ADMIN``: el del cliente más lo que solo él ve."""
    cobertura = siniestro.tipo.cobertura
    suma = next(
        (c.suma_asegurada for c in poliza.coberturas if c.codigo is cobertura),
        Decimal(0),
    )
    base = SiniestroOut.model_validate(siniestro)
    return SiniestroInternoOut(
        **base.model_dump(),
        cliente_id=siniestro.cliente_id,
        poliza=PolizaDelSiniestroOut(
            id=poliza.id,
            numero=poliza.numero,
            cobertura=CodigoCobertura(cobertura),
            suma_asegurada=suma,
        ),
        saldo_credito=SaldoCreditoOut.model_validate(siniestro.saldo_credito),
        destino_pago=siniestro.destino_pago,
        decisiones=[DecisionSiniestroOut.model_validate(d) for d in siniestro.decisiones],
    )


@router.get(
    "/siniestros",
    response_model=list[SiniestroInternoResumenOut],
    tags=["Siniestros (backoffice)"],
)
async def listar_bandeja(
    servicio: SiniestrosDep,
    estado: Annotated[EstadoSiniestro | None, Query()] = None,
    automatico: Annotated[bool | None, Query()] = None,
) -> list[SiniestroInternoResumenOut]:
    logger.info(
        "GET /v1/siniestros: solicitud recibida estado=%s automatico=%s", estado, automatico
    )
    siniestros = await servicio.bandeja(estado, automatico)
    return [SiniestroInternoResumenOut.model_validate(s) for s in siniestros]


@router.get(
    "/siniestros/{siniestro_id}",
    response_model=SiniestroInternoOut,
    tags=["Siniestros (backoffice)"],
)
async def obtener_siniestro_interno(
    siniestro_id: str, servicio: SiniestrosDep
) -> SiniestroInternoOut:
    logger.info("GET /v1/siniestros/%s: solicitud recibida", sanear_para_log(siniestro_id))
    siniestro, poliza = await servicio.detalle_interno(siniestro_id)
    return _interno(siniestro, poliza)


@router.get(
    "/siniestros/{siniestro_id}/evidencias/{evidencia_id}",
    tags=["Siniestros (backoffice)"],
    response_class=Response,
    responses={200: {"content": {"image/jpeg": {}, "image/png": {}}}},
)
async def obtener_archivo_del_siniestro(
    siniestro_id: str, evidencia_id: str, servicio: SiniestrosDep
) -> Response:
    # La imagen no se escribe en el log, solo a qué siniestro pertenece.
    logger.info(
        "GET /v1/siniestros/%s/evidencias: solicitud recibida", sanear_para_log(siniestro_id)
    )
    contenido, tipo = await servicio.archivo(siniestro_id, evidencia_id)
    return Response(
        content=contenido,
        media_type=tipo,
        headers={"Cache-Control": "no-store", "Content-Disposition": "inline"},
    )


@router.post(
    "/siniestros/{siniestro_id}/decisiones",
    response_model=SiniestroInternoOut,
    tags=["Siniestros (backoffice)"],
)
async def decidir_siniestro(
    siniestro_id: str, payload: DecidirSiniestroRequest, servicio: SiniestrosDep
) -> SiniestroInternoOut:
    # Ni el agente (su correo) ni el destino del pago se escriben en el log.
    logger.info(
        "POST /v1/siniestros/%s/decisiones: solicitud recibida decision=%s",
        sanear_para_log(siniestro_id),
        payload.decision,
    )
    siniestro = await servicio.decidir(
        siniestro_id,
        DecisionEntrada(
            decision=payload.decision,
            agente=payload.agente,
            dano=payload.dano,
            valor=Decimal(str(payload.valor)) if payload.valor is not None else None,
            detalle=payload.detalle,
            motivo=payload.motivo,
        ),
    )
    _, poliza = await servicio.detalle_interno(siniestro.id)
    return _interno(siniestro, poliza)
