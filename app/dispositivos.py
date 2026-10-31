"""Dispositivos de los clientes para los avisos push (BITS-229).

La app obtiene su token de Firebase y lo registra aquí a través del BFF móvil. Core es dueño del
cliente, así que guarda los tokens; la función que entrega el aviso no guarda estado.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from app.domain import (
    MAX_DISPOSITIVOS_POR_CLIENTE,
    ClienteNoEncontrado,
    Dispositivo,
    PlataformaDispositivo,
    now_utc,
)
from app.ports import ClienteRepository, DispositivoRepository

logger = logging.getLogger("svc_core.dispositivos")


def token_corto(token: str) -> str:
    """Lo único del token que se escribe en los logs: identifica al dispositivo sin exponerlo."""
    return f"…{token[-6:]}"


class DispositivoService:
    def __init__(
        self,
        clientes: ClienteRepository,
        dispositivos: DispositivoRepository,
        *,
        maximo: int = MAX_DISPOSITIVOS_POR_CLIENTE,
        ahora: Callable[[], datetime] = now_utc,
    ) -> None:
        self._clientes = clientes
        self._dispositivos = dispositivos
        self._maximo = maximo
        self._ahora = ahora

    async def registrar(
        self,
        cliente_id: str,
        token: str,
        plataforma: PlataformaDispositivo,
        version_app: str,
    ) -> Dispositivo:
        """Idempotente: repetir el registro del mismo token solo renueva ``ultimo_visto_en``."""
        if await self._clientes.obtener(cliente_id) is None:
            raise ClienteNoEncontrado(cliente_id)
        ahora = self._ahora()
        dispositivo = await self._dispositivos.registrar(
            Dispositivo(
                cliente_id=cliente_id,
                token=token,
                plataforma=plataforma,
                version_app=version_app,
                registrado_en=ahora,
                ultimo_visto_en=ahora,
            ),
            self._maximo,
        )
        logger.info(
            "dispositivo registrado dispositivo_id=%s token=%s", dispositivo.id, token_corto(token)
        )
        return dispositivo

    async def quitar(self, cliente_id: str, dispositivo_id: str) -> None:
        """Idempotente: quitar un dispositivo que ya no está (o que es de otro cliente) no falla,
        porque la app lo pide al cerrar sesión y no debe depender de lo que haya pasado antes."""
        if await self._dispositivos.quitar(cliente_id, dispositivo_id):
            logger.info("dispositivo quitado dispositivo_id=%s", dispositivo_id)
