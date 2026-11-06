"""Cuentas internas en memoria (``CORE_CUENTAS_INTERNAS_BACKEND=fake``): local y pruebas."""

from __future__ import annotations

from app.domain import CuentaInterna, CuentaInternaYaExiste, RolInterno, new_id


class CuentasInternasEnMemoria:
    def __init__(self) -> None:
        self._por_correo: dict[str, CuentaInterna] = {}
        self.contrasenas: dict[str, str] = {}

    async def crear_cuenta(
        self, correo: str, contrasena: str, rol: RolInterno, nombre: str | None
    ) -> CuentaInterna:
        if correo in self._por_correo:
            raise CuentaInternaYaExiste()
        cuenta = CuentaInterna(uid=new_id(), correo=correo, rol=rol, nombre=nombre)
        self._por_correo[correo] = cuenta
        self.contrasenas[correo] = contrasena
        return cuenta

    async def buscar_por_correo(self, correo: str) -> CuentaInterna | None:
        return self._por_correo.get(correo)
