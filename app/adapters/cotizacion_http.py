"""Adaptador HTTP hacia Cotización y Rating (cotización activa de un cliente).

``GET /v1/clientes/{clienteId}/cotizacion-activa``: ruta interna que no pasa por el gateway.
``404`` significa que el cliente no tiene cotización (no es un fallo); un ``5xx`` o un corte de red
lanzan ``CotizacionNoDisponible``: sin cotización no se emite nada.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from decimal import Decimal

import httpx

from app.domain import (
    CoberturaContratada,
    CodigoCobertura,
    CotizacionActiva,
    CotizacionNoDisponible,
    DatosCredito,
    OrigenDatos,
    Vigencia,
)
from app.logging_utils import sanear_para_log

logger = logging.getLogger("svc_core.adapters.cotizacion")

_REINTENTOS = 1


def _decimal(valor: float | int | str) -> Decimal:
    return Decimal(str(valor))


def cotizacion_desde_dict(d: dict) -> CotizacionActiva:
    credito = d["datosCredito"]
    return CotizacionActiva(
        estado=d["estado"],
        edad=d["edad"],
        datos_credito=DatosCredito(
            valor_credito=_decimal(credito["valorCredito"]),
            plazo_restante_meses=credito["plazoRestanteMeses"],
            entidad_acreedora=credito["entidadAcreedora"],
            saldo_insoluto=_decimal(credito["saldoInsoluto"]),
        ),
        origen_datos=OrigenDatos(d["origenDatos"]),
        tasa=_decimal(d["tasa"]),
        version_tarifa=d["versionTarifa"],
        cobertura_meses=d["coberturaMeses"],
        coberturas=tuple(
            CoberturaContratada(
                codigo=CodigoCobertura(c["codigo"]),
                obligatoria=c["obligatoria"],
                suma_asegurada=_decimal(c["sumaAsegurada"]),
                prima_neta=_decimal(c["primaNeta"]),
                iva=_decimal(c["iva"]),
                prima_total=_decimal(c["primaTotal"]),
            )
            for c in d["coberturas"]
        ),
        vigencia=Vigencia(
            desde=datetime.fromisoformat(d["vigenciaCotizacion"]["desde"]),
            hasta=datetime.fromisoformat(d["vigenciaCotizacion"]["hasta"]),
        ),
        creada_en=datetime.fromisoformat(d["creadaEn"]),
    )


class CotizacionHttp:
    def __init__(
        self, base_url: str, timeout_s: float = 1.0, client: httpx.AsyncClient | None = None
    ) -> None:
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=timeout_s)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def activa(self, cliente_id: str) -> CotizacionActiva | None:
        ruta = f"/v1/clientes/{cliente_id}/cotizacion-activa"
        for intento in range(_REINTENTOS + 1):
            try:
                respuesta = await self._client.get(ruta)
            except httpx.TransportError:
                logger.warning(
                    "cotización activa: sin respuesta cliente_id=%s", sanear_para_log(cliente_id)
                )
            else:
                if respuesta.status_code == 404:
                    return None
                if respuesta.status_code == 200:
                    return cotizacion_desde_dict(respuesta.json())
                if respuesta.status_code < 500:
                    logger.error("cotización respondió %s", respuesta.status_code)
                    raise CotizacionNoDisponible()
                logger.warning("cotización respondió %s", respuesta.status_code)
            if intento < _REINTENTOS:
                await asyncio.sleep(0.1)
        raise CotizacionNoDisponible()
