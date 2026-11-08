"""Cuentas internas en memoria (``CORE_CUENTAS_INTERNAS_BACKEND=fake``): local y pruebas."""

from __future__ import annotations

from app.domain import (
    CuentaClienteYaExiste,
    CuentaInterna,
    CuentaInternaYaExiste,
    IdentidadNoDisponible,
    RolInterno,
    new_id,
)


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


class CuentasClienteEnMemoria:
    """Cuentas de clientes en memoria: local y pruebas. Guarda la contraseña para poder
    comprobar qué se le mandó al cliente."""

    def __init__(self, *, caida: bool = False) -> None:
        self.cuentas: dict[str, tuple[str, str]] = {}  # uid -> (correo, contraseña)
        self.borradas: list[str] = []
        self.caida = caida

    async def crear_cuenta_cliente(self, correo: str, contrasena: str, nombre: str) -> str:
        if self.caida:
            raise IdentidadNoDisponible()
        if any(c == correo for c, _ in self.cuentas.values()):
            raise CuentaClienteYaExiste()
        uid = new_id()
        self.cuentas[uid] = (correo, contrasena)
        return uid

    async def borrar_cuenta(self, uid: str) -> None:
        self.cuentas.pop(uid, None)
        self.borradas.append(uid)
