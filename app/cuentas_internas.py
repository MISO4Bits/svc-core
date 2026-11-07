"""Cuentas del personal de Solventa: administradores y socios (BITS-374).

Las cuentas viven en Identity Platform, que sobrevive al ``terraform destroy``: aquí no hay tabla
propia. Core solo valida la regla del dominio y del nombre antes de pedir la creación. El ``uid`` de
una cuenta ``SOCIO`` es su ``socioId``.
"""

from __future__ import annotations

import logging

from app.domain import (
    CorreoInternoInvalido,
    CuentaInterna,
    CuentaInternaYaExiste,
    NombreSocioObligatorio,
    RolInterno,
    es_correo_interno,
)
from app.ports import CuentasInternasPort

logger = logging.getLogger("svc_core.cuentas_internas")

NOMBRE_ADMIN_SEMILLA = "Administrador"


class CuentasInternasService:
    def __init__(self, cuentas: CuentasInternasPort) -> None:
        self._cuentas = cuentas

    async def crear(
        self, correo: str, contrasena: str, rol: RolInterno, nombre: str | None
    ) -> CuentaInterna:
        correo = correo.strip().lower()
        if not es_correo_interno(correo):
            raise CorreoInternoInvalido()
        nombre = (nombre or "").strip() or None
        if rol is RolInterno.SOCIO and nombre is None:
            raise NombreSocioObligatorio()
        cuenta = await self._cuentas.crear_cuenta(correo, contrasena, rol, nombre)
        logger.info("cuenta interna creada uid=%s rol=%s", cuenta.uid, cuenta.rol)
        return cuenta

    async def asegurar_admin(self, correo: str, contrasena: str) -> CuentaInterna | None:
        """Crea el administrador de la semilla si no existe. Idempotente: una cuenta que ya existe
        no se toca, así que no pisa una contraseña que alguien cambió. Devuelve la cuenta que
        quedó, o ``None`` si el correo es de otra cuenta que no es ``ADMIN``."""
        correo = correo.strip().lower()
        if not es_correo_interno(correo):
            raise CorreoInternoInvalido()
        existente = await self._cuentas.buscar_por_correo(correo)
        if existente is None:
            try:
                cuenta = await self._cuentas.crear_cuenta(
                    correo, contrasena, RolInterno.ADMIN, NOMBRE_ADMIN_SEMILLA
                )
            except CuentaInternaYaExiste:
                # Otro pod la creó entre la búsqueda y la creación.
                logger.info("administrador de la semilla ya existe (creado por otro pod)")
                return await self._cuentas.buscar_por_correo(correo)
            logger.info("administrador de la semilla creado uid=%s", cuenta.uid)
            return cuenta
        if existente.rol is not RolInterno.ADMIN:
            logger.warning(
                "el correo de la semilla es de una cuenta con rol %s: no se modifica", existente.rol
            )
            return None
        logger.info("administrador de la semilla ya existe uid=%s", existente.uid)
        return existente
