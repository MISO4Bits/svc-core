"""Rutas de las evidencias de identidad. Se montan solo bajo ``/v1`` (sin el alias sin versión)."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile, status

from app.api.schemas import EstadoTipoEvidenciaOut, EvidenciaSubidaOut
from app.domain import EstadoEvidencia, TipoEvidencia
from app.evidencias import EvidenciaService
from app.logging_utils import sanear_para_log

logger = logging.getLogger("svc_core.api")
router = APIRouter(tags=["Evidencias de identidad"])


def get_evidencias(request: Request) -> EvidenciaService:
    return request.app.state.evidencias


EvidenciasDep = Annotated[EvidenciaService, Depends(get_evidencias)]


@router.post(
    "/clientes/{cliente_id}/evidencias-identidad",
    response_model=EvidenciaSubidaOut,
    status_code=status.HTTP_201_CREATED,
)
async def subir_evidencia(
    cliente_id: str,
    tipo: Annotated[TipoEvidencia, Form()],
    imagen: Annotated[UploadFile, File()],
    service: EvidenciasDep,
    response: Response,
) -> EvidenciaSubidaOut:
    logger.info(
        "POST /v1/clientes/%s/evidencias-identidad: solicitud recibida tipo=%s",
        sanear_para_log(cliente_id),
        tipo,
    )
    # Se lee un byte de más para detectar el exceso sin cargar un archivo sin límite.
    datos = await imagen.read(service.max_bytes + 1)
    resultado = await service.subir(cliente_id, tipo, datos)
    if resultado.estado is EstadoEvidencia.REINTENTAR:
        response.status_code = status.HTTP_200_OK  # veredicto normal: no se creó nada
    return EvidenciaSubidaOut(
        evidencia_id=resultado.evidencia.id if resultado.evidencia else None,
        tipo=tipo,
        estado=resultado.estado,
        motivo=resultado.motivo,
        intentos_restantes=resultado.intentos_restantes,
    )


@router.get(
    "/clientes/{cliente_id}/evidencias-identidad",
    response_model=list[EstadoTipoEvidenciaOut],
)
async def estado_evidencias(
    cliente_id: str, service: EvidenciasDep
) -> list[EstadoTipoEvidenciaOut]:
    logger.info(
        "GET /v1/clientes/%s/evidencias-identidad: solicitud recibida", sanear_para_log(cliente_id)
    )
    return [
        EstadoTipoEvidenciaOut(
            tipo=item.tipo,
            estado=item.evidencia.estado.value if item.evidencia else "PENDIENTE",
            evidencia_id=item.evidencia.id if item.evidencia else None,
            creada_en=item.evidencia.creada_en if item.evidencia else None,
            intentos_restantes=item.intentos_restantes,
        )
        for item in await service.estado(cliente_id)
    ]
