"""Adaptador HTTP hacia Productos y Configuración de Mercado (plantillas de correo)."""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.domain import NotificacionError, PlantillaCorreo, TipoCorreo

logger = logging.getLogger("svc_core.adapters.productos")

_REINTENTOS = 2


class ProductosPlantillasHttp:
    def __init__(
        self, base_url: str, timeout_s: float = 2.0, client: httpx.AsyncClient | None = None
    ) -> None:
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=timeout_s)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def obtener_vigente(self, tipo: TipoCorreo, mercado: str, idioma: str) -> PlantillaCorreo:
        respuesta = await self._get(
            f"/plantillas-correo/{tipo}", {"mercado": mercado, "idioma": idioma}
        )
        if respuesta.status_code != 200:
            raise NotificacionError(f"Productos respondió {respuesta.status_code} para {tipo}")
        datos = respuesta.json()
        return PlantillaCorreo(
            tipo=datos["tipo"],
            version=datos["version"],
            asunto=datos["asunto"],
            cuerpo_html=datos["cuerpoHtml"],
            cuerpo_texto=datos["cuerpoTexto"],
        )

    async def _get(self, ruta: str, params: dict[str, str]) -> httpx.Response:
        ultimo_error: Exception | None = None
        for intento in range(_REINTENTOS + 1):
            try:
                respuesta = await self._client.get(ruta, params=params)
            except httpx.TransportError as exc:
                ultimo_error = exc
            else:
                if respuesta.status_code < 500:
                    return respuesta
                ultimo_error = NotificacionError(f"Productos respondió {respuesta.status_code}")
            if intento < _REINTENTOS:
                await asyncio.sleep(0.1 * (intento + 1))
        raise NotificacionError("Productos no está disponible") from ultimo_error
