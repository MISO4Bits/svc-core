"""Pago automático por sismo (BITS-386).

Un seguro paramétrico paga por un hecho medible y no por un reclamo: si llega el aviso de un sismo
con magnitud sobre el umbral, cada póliza con Tu casa que estaba vigente ese día y cuyo municipio
es el del sismo recibe, sin que el cliente haga nada, el pago de una cuota de su crédito. «Pagar»
es la orden de pago simulada del siniestro (``Pago``): no hay pasarela y no se mueve dinero.

El municipio y la cuota salen de lo que capturó el socio al vender (``datos_venta``) o, si la póliza
no la vendió un socio, de svc-perfilamiento: la ciudad del perfil y la cuota de la hipoteca de la
entidad de la póliza. Lo que no se pueda saber omite esa póliza con su motivo y las demás siguen.

Un mismo sismo no paga dos veces la misma póliza: las que ya lo cobraron se saltan al empezar y un
índice único de la base lo garantiza si dos entregas corren a la vez.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.domain import (
    CodigoCobertura,
    EstadoPoliza,
    EventoSismo,
    FiltroPolizas,
    Poliza,
    SiniestroDeSismoRepetido,
    now_utc,
)
from app.logging_utils import sanear_para_log
from app.ports import CiudadClientePort, PolizaRepository
from app.siniestros import ESTADO_DISPONIBLE, SiniestroService, normalizado

logger = logging.getLogger("svc_core.sismos")

TAMANO_PAGINA = 100

PAGADA = "PAGADA"
DUPLICADA = "DUPLICADA"
OMITIDA = "OMITIDA"
OTRO_MUNICIPIO = "OTRO_MUNICIPIO"


_SUFIJOS_SOCIETARIOS = frozenset({"sa", "sas", "ltda"})


def misma_entidad(a: str, b: str) -> bool:
    """Si dos nombres son del mismo banco. Open Finance reporta «BANCOLOMBIA S.A.» y la póliza
    guarda «Bancolombia»: se ignoran mayúsculas, tildes, puntuación y el sufijo societario, y basta
    con que las palabras de uno estén todas en el otro («Banco AV Villas» y «BANCO COMERCIAL AV
    VILLAS S.A.»)."""

    def palabras(nombre: str) -> frozenset[str]:
        limpio = normalizado(nombre).replace(".", "").replace(",", "")
        return frozenset(p for p in limpio.split() if p not in _SUFIJOS_SOCIETARIOS)

    pa, pb = palabras(a), palabras(b)
    return bool(pa) and bool(pb) and (pa <= pb or pb <= pa)


@dataclass
class ResumenSismo:
    """Lo que pasó con el sismo, también como una línea de log estructurada."""

    evento_id: str
    magnitud: float
    umbral: float
    bajo_umbral: bool = False
    resultados: Counter = field(default_factory=Counter)
    motivos_de_omision: Counter = field(default_factory=Counter)

    @property
    def evaluadas(self) -> int:
        return sum(self.resultados.values())

    @property
    def pagadas(self) -> int:
        return self.resultados[PAGADA]


class SismoService:
    def __init__(
        self,
        polizas: PolizaRepository,
        siniestros: SiniestroService,
        ciudades: CiudadClientePort,
        *,
        magnitud_minima: float,
        concurrencia: int = 10,
        ahora: Callable[[], datetime] = now_utc,
    ) -> None:
        self._polizas = polizas
        self._siniestros = siniestros
        self._ciudades = ciudades
        self._magnitud_minima = magnitud_minima
        self._concurrencia = concurrencia
        self._ahora = ahora

    async def procesar(self, evento: EventoSismo) -> ResumenSismo:
        resumen = ResumenSismo(evento.evento_id, evento.magnitud, self._magnitud_minima)
        if evento.magnitud < self._magnitud_minima:
            resumen.bajo_umbral = True
            self._registrar_resumen(resumen)
            return resumen

        ya_cobradas = await self._siniestros.polizas_con_siniestro_de_evento(evento.evento_id)
        cupo = asyncio.Semaphore(self._concurrencia)
        cursor: str | None = None
        while True:
            pagina = await self._polizas.listar(
                FiltroPolizas(
                    estado=EstadoPoliza.EMITIDA,
                    con_cobertura=CodigoCobertura.HOGAR,
                    vigente_el=evento.fecha,
                    limite=TAMANO_PAGINA,
                    cursor=cursor,
                ),
                self._ahora(),
            )
            await asyncio.gather(
                *(self._una(p, evento, ya_cobradas, resumen, cupo) for p in pagina.items)
            )
            cursor = pagina.siguiente_cursor
            if cursor is None:
                break
        self._registrar_resumen(resumen)
        return resumen

    async def _una(
        self,
        poliza: Poliza,
        evento: EventoSismo,
        ya_cobradas: frozenset[str],
        resumen: ResumenSismo,
        cupo: asyncio.Semaphore,
    ) -> None:
        async with cupo:
            try:
                resultado, motivo = await self._evaluar(poliza, evento, ya_cobradas)
            except Exception as exc:
                # Una póliza que falla no frena a las demás ni devuelve el mensaje a Pub/Sub: una
                # póliza con un problema permanente lo reintentaría para siempre.
                resultado, motivo = OMITIDA, f"ERROR_{type(exc).__name__}"
        resumen.resultados[resultado] += 1
        if resultado == OMITIDA:
            resumen.motivos_de_omision[motivo] += 1
        if resultado != OTRO_MUNICIPIO:
            # Las de otro municipio son casi todas: solo cuentan en el resumen.
            logger.info(
                "sismo: evento_id=%s poliza_id=%s resultado=%s motivo=%s",
                sanear_para_log(evento.evento_id),
                poliza.id,
                resultado,
                motivo or "-",
            )

    async def _evaluar(
        self, poliza: Poliza, evento: EventoSismo, ya_cobradas: frozenset[str]
    ) -> tuple[str, str | None]:
        if poliza.id in ya_cobradas:
            return DUPLICADA, None
        venta = poliza.datos_venta
        if venta is not None:
            municipio: str | None = venta.municipio
            cuota: Decimal | None = venta.cuota_mensual
        else:
            municipio = await self._ciudades.ciudad(poliza.cliente_id)
            cuota = None
        if municipio is None:
            return OMITIDA, "MUNICIPIO_DESCONOCIDO"
        if normalizado(municipio) != normalizado(evento.municipio):
            return OTRO_MUNICIPIO, None
        if cuota is None:
            cuota = await self._cuota_de_la_hipoteca(poliza)
        if cuota is None:
            return OMITIDA, "CUOTA_DESCONOCIDA"
        try:
            await self._siniestros.pagar_por_sismo(poliza, evento, cuota)
        except SiniestroDeSismoRepetido:
            return DUPLICADA, None
        return PAGADA, None

    async def _cuota_de_la_hipoteca(self, poliza: Poliza) -> Decimal | None:
        """La cuota de la hipoteca de la entidad de la póliza, según Open Finance."""
        reportados = await self._siniestros.creditos.hipotecas(poliza.cliente_id)
        if reportados.estado != ESTADO_DISPONIBLE:
            return None
        hipoteca = next(
            (
                h
                for h in reportados.hipotecas
                if misma_entidad(h.entidad_acreedora, poliza.datos_credito.entidad_acreedora)
            ),
            None,
        )
        return hipoteca.cuota_mensual if hipoteca is not None else None

    @staticmethod
    def _registrar_resumen(resumen: ResumenSismo) -> None:
        logger.info(
            "sismo resumen: evento_id=%s magnitud=%s umbral=%s bajo_umbral=%s evaluadas=%d "
            "pagadas=%d duplicadas=%d otro_municipio=%d omitidas=%d motivos=%s",
            sanear_para_log(resumen.evento_id),
            resumen.magnitud,
            resumen.umbral,
            resumen.bajo_umbral,
            resumen.evaluadas,
            resumen.pagadas,
            resumen.resultados[DUPLICADA],
            resumen.resultados[OTRO_MUNICIPIO],
            resumen.resultados[OMITIDA],
            dict(resumen.motivos_de_omision),
        )
