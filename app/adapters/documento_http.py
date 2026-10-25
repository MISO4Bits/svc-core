"""Adaptador HTTP hacia fn-documentos (documento de la póliza).

``POST /v1/documentos/polizas/enlace`` con los datos de la póliza: la función genera el PDF si
falta y devuelve un enlace firmado de pocos minutos. Es una función de Cloud Run sin acceso
público: se la llama con un token de identidad de la cuenta de servicio de Core (el mismo
mecanismo con el que Pub/Sub invoca a ``fn-notificaciones``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime

import httpx

from app.domain import DocumentoNoDisponible, DocumentoPoliza, EstadoDocumentoPoliza

logger = logging.getLogger("svc_core.adapters.documento")

RUTA = "/v1/documentos/polizas/enlace"
# Los tokens de identidad duran una hora: se renuevan un poco antes.
_VIGENCIA_TOKEN_S = 50 * 60

ProveedorDeToken = Callable[[str], Awaitable[str | None]]


async def token_de_identidad(audiencia: str) -> str | None:  # pragma: no cover
    """Token de identidad de la cuenta de servicio del pod para esa audiencia (Cloud Run).
    ``None`` fuera de GCP: la llamada sale sin credenciales y la función la rechazará."""

    def _pedir() -> str | None:
        import google.auth.exceptions
        import google.auth.transport.requests
        from google.oauth2 import id_token

        try:
            return id_token.fetch_id_token(google.auth.transport.requests.Request(), audiencia)
        except google.auth.exceptions.GoogleAuthError:
            logger.warning("no hay credenciales de GCP para firmar la llamada a fn-documentos")
            return None

    return await asyncio.to_thread(_pedir)


class DocumentoPolizaHttp:
    def __init__(
        self,
        base_url: str,
        timeout_s: float = 4.0,
        client: httpx.AsyncClient | None = None,
        proveedor_de_token: ProveedorDeToken = token_de_identidad,
    ) -> None:
        self._audiencia = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(base_url=self._audiencia, timeout=timeout_s)
        self._proveedor = proveedor_de_token
        self._token: str | None = None
        self._token_vence = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _encabezados(self) -> dict[str, str]:
        ahora = time.monotonic()
        if self._token is None or ahora >= self._token_vence:
            self._token = await self._proveedor(self._audiencia)
            self._token_vence = ahora + _VIGENCIA_TOKEN_S
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    async def consultar(self, datos: dict) -> DocumentoPoliza:
        try:
            respuesta = await self._client.post(RUTA, json=datos, headers=await self._encabezados())
        except httpx.TransportError as exc:
            logger.warning("fn-documentos sin respuesta: %s", type(exc).__name__)
            raise DocumentoNoDisponible() from exc
        if respuesta.status_code != 200:
            logger.warning("fn-documentos respondió %s", respuesta.status_code)
            if respuesta.status_code in (401, 403):
                self._token = None  # el siguiente intento pide un token nuevo
            raise DocumentoNoDisponible()
        cuerpo = respuesta.json()
        return DocumentoPoliza(
            EstadoDocumentoPoliza.LISTO,
            url=cuerpo["url"],
            expira_en=datetime.fromisoformat(cuerpo["expiraEn"]),
        )
