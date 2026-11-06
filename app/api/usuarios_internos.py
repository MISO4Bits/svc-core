"""Alta de cuentas del personal de Solventa (BITS-374). Se monta bajo ``/v1``.

Interno y sin gateway: solo lo llama bff-web, desde la ruta que exige el rol ``ADMIN``.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from app.api.schemas import CrearUsuarioInternoRequest, UsuarioInternoOut
from app.cuentas_internas import CuentasInternasService
from app.domain import IdentidadNoDisponible

logger = logging.getLogger("svc_core.api")
router = APIRouter()


def get_cuentas_internas(request: Request) -> CuentasInternasService:
    servicio = request.app.state.cuentas_internas
    if servicio is None:
        # CORE_CUENTAS_INTERNAS_BACKEND=off: no hay proveedor de identidad configurado.
        raise IdentidadNoDisponible()
    return servicio


CuentasDep = Annotated[CuentasInternasService, Depends(get_cuentas_internas)]


@router.post(
    "/usuarios-internos",
    response_model=UsuarioInternoOut,
    status_code=status.HTTP_201_CREATED,
    tags=["Usuarios internos"],
)
async def crear_usuario_interno(
    payload: CrearUsuarioInternoRequest, servicio: CuentasDep
) -> UsuarioInternoOut:
    # Ni la contraseña ni el correo se escriben en el log: el servicio registra solo el uid.
    logger.info("POST /v1/usuarios-internos: solicitud recibida rol=%s", payload.rol)
    cuenta = await servicio.crear(payload.correo, payload.contrasena, payload.rol, payload.nombre)
    return UsuarioInternoOut(uid=cuenta.uid, correo=cuenta.correo, rol=cuenta.rol)
