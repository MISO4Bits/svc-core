"""Adaptador HTTP hacia Productos y Configuración de Mercado: versión vigente de un documento legal.

``GET /documentos-legales?mercado=&idioma=&tipo=``: la venta directa del socio acepta las
condiciones de la póliza en nombre del cliente, y lo hace en la versión que Productos tiene
publicada en ese momento.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.domain import DocumentosLegalesNoDisponibles

logger = logging.getLogger("svc_core.adapters.documentos_legales")

_REINTENTOS = 1


class DocumentosLegalesHttp:
    def __init__(
        self,
        base_url: str,
        mercado: str,
        idioma: str,
        timeout_s: float = 2.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=timeout_s)
        self._mercado = mercado
        self._idioma = idioma

    async def aclose(self) -> None:
        await self._client.aclose()

    async def version_vigente(self, tipo: str) -> str:
        parametros = {"mercado": self._mercado, "idioma": self._idioma, "tipo": tipo}
        for intento in range(_REINTENTOS + 1):
            try:
                respuesta = await self._client.get("/documentos-legales", params=parametros)
            except httpx.TransportError:
                logger.warning("documentos legales: Productos no respondió")
            else:
                if respuesta.status_code == 200:
                    return self._version_de(respuesta.json(), tipo)
                if respuesta.status_code < 500:
                    logger.error(
                        "documentos legales: Productos respondió %s", respuesta.status_code
                    )
                    raise DocumentosLegalesNoDisponibles()
                logger.warning("documentos legales: Productos respondió %s", respuesta.status_code)
            if intento < _REINTENTOS:
                await asyncio.sleep(0.1)
        raise DocumentosLegalesNoDisponibles()

    @staticmethod
    def _version_de(documentos: object, tipo: str) -> str:
        if isinstance(documentos, list):
            for documento in documentos:
                if isinstance(documento, dict) and documento.get("tipo") == tipo:
                    version = documento.get("version")
                    if isinstance(version, str) and version:
                        return version
        logger.error("documentos legales: no hay un documento publicado de tipo %s", tipo)
        raise DocumentosLegalesNoDisponibles()
