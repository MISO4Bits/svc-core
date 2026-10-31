"""Rutas de los dispositivos para avisos push (BITS-229). Se montan bajo ``/v1``.

Las llama el BFF móvil con el ``clienteId`` de la sesión.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from app.api.schemas import DispositivoOut, RegistrarDispositivoRequest
from app.dispositivos import DispositivoService, token_corto
from app.logging_utils import sanear_para_log

logger = logging.getLogger("svc_core.api")
router = APIRouter()


def get_dispositivos(request: Request) -> DispositivoService:
    return request.app.state.dispositivos


DispositivosDep = Annotated[DispositivoService, Depends(get_dispositivos)]


@router.put(
    "/clientes/{cliente_id}/dispositivos", response_model=DispositivoOut, tags=["Dispositivos"]
)
async def registrar_dispositivo(
    cliente_id: str, payload: RegistrarDispositivoRequest, servicio: DispositivosDep
) -> DispositivoOut:
    logger.info(
        "PUT /v1/clientes/%s/dispositivos: solicitud recibida token=%s",
        sanear_para_log(cliente_id),
        token_corto(payload.token),
    )
    dispositivo = await servicio.registrar(
        cliente_id, payload.token, payload.plataforma, payload.version_app
    )
    return DispositivoOut(dispositivo_id=dispositivo.id)


@router.delete(
    "/clientes/{cliente_id}/dispositivos/{dispositivo_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["Dispositivos"],
)
async def quitar_dispositivo(
    cliente_id: str, dispositivo_id: str, servicio: DispositivosDep
) -> Response:
    logger.info(
        "DELETE /v1/clientes/%s/dispositivos/%s: solicitud recibida",
        sanear_para_log(cliente_id),
        sanear_para_log(dispositivo_id),
    )
    await servicio.quitar(cliente_id, dispositivo_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
