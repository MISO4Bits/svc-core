"""Adaptador HTTP hacia svc-perfilamiento (hipotecas del cliente).

``GET /clientes/{cliente_id}/creditos-hipotecarios``: perfilamiento ya trajo los datos de Open
Finance al otorgarse el consentimiento y los guarda 24 horas; esta llamada lee lo guardado.

Nunca lanza: ante un corte, un error o una respuesta ilegible devuelve ``NO_DISPONIBLE``. El
reporte de un siniestro se radica igual, con el saldo que tiene la póliza.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

import httpx

from app.domain import CreditosReportados, HipotecaReportada
from app.logging_utils import sanear_para_log

logger = logging.getLogger("svc_core.adapters.perfilamiento")

NO_DISPONIBLE = CreditosReportados(estado="NO_DISPONIBLE")


class PerfilamientoHttp:
    def __init__(
        self, base_url: str, timeout_s: float = 1.0, client: httpx.AsyncClient | None = None
    ) -> None:
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=timeout_s)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def ciudad(self, cliente_id: str) -> str | None:
        """La ciudad del perfil (la que reportó Open Data), para saber si un sismo afectó al
        cliente. ``None`` si no se sabe; nunca lanza: esa póliza se omite y las demás siguen."""
        ruta = f"/v1/perfiles/{quote(cliente_id, safe='')}"
        try:
            respuesta = await self._client.get(ruta)
        except httpx.TransportError:
            logger.warning(
                "perfilamiento: sin respuesta cliente_id=%s", sanear_para_log(cliente_id)
            )
            return None
        if respuesta.status_code != 200:
            # 404: el cliente no tiene perfil (sin consentimiento). Cualquier otro: no se sabe.
            logger.info("perfilamiento respondió %s al pedir la ciudad", respuesta.status_code)
            return None
        try:
            ciudad = respuesta.json().get("ciudad")
        except (ValueError, AttributeError):
            logger.warning("perfilamiento devolvió una respuesta ilegible")
            return None
        return ciudad.strip() or None if isinstance(ciudad, str) else None

    async def hipotecas(self, cliente_id: str) -> CreditosReportados:
        ruta = f"/clientes/{quote(cliente_id, safe='')}/creditos-hipotecarios"
        try:
            respuesta = await self._client.get(ruta)
        except httpx.TransportError:
            logger.warning(
                "perfilamiento: sin respuesta cliente_id=%s", sanear_para_log(cliente_id)
            )
            return NO_DISPONIBLE
        if respuesta.status_code != 200:
            logger.warning("perfilamiento respondió %s", respuesta.status_code)
            return NO_DISPONIBLE
        try:
            datos = respuesta.json()
            return CreditosReportados(
                estado=datos["estado"],
                hipotecas=tuple(
                    HipotecaReportada(
                        entidad_acreedora=h["entidadAcreedora"],
                        saldo_insoluto=Decimal(str(h["saldoInsoluto"])),
                        cuota_mensual=(
                            Decimal(str(h["cuotaMensual"]))
                            if h.get("cuotaMensual") is not None
                            else None
                        ),
                    )
                    for h in datos.get("hipotecas", [])
                ),
            )
        except (ValueError, KeyError, TypeError, InvalidOperation):
            logger.warning("perfilamiento devolvió una respuesta ilegible")
            return NO_DISPONIBLE
