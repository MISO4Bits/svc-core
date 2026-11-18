"""Renovación anual de la póliza (BITS-387).

Un trabajo programado, que corre una vez al día, hace tres pasadas sobre las pólizas emitidas:

1. 45 días antes del aniversario: publica el aviso de vencimiento. No guarda la propuesta (se
   calcula al leer, con el IPC de la configuración): solo deja la marca ``aviso_vencimiento_en``,
   y desde ahí el cliente puede aceptar o rechazar.
2. 30 días antes: cierra la decisión del cliente y, si rechazó, registra el aviso a la entidad
   acreedora (simulado: va dentro de la columna ``renovacion``, sin tabla aparte).
3. Aniversario: renueva (vigencia nueva de 12 meses y prima nueva) si el cliente aceptó o no
   respondió; si rechazó, deja la póliza ``DESACTIVADA``.

Cada pasada deja su marca en ``renovacion`` y selecciona solo lo que no la tiene, así que correr
el trabajo dos veces el mismo día no repite avisos ni renueva dos veces. El aniversario además se
protege en el repositorio: solo renueva si ``vigencia.hasta`` sigue siendo la que se leyó.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal

from app.avisos import Aviso, AvisoService, ruta_seguro
from app.domain import (
    DIAS_AVISO_BANCO,
    DIAS_AVISO_VENCIMIENTO,
    MESES_VIGENCIA,
    TIPO_AVISO_NO_RENOVACION,
    ZONA_COLOMBIA,
    AvisoBanco,
    DecisionRenovacion,
    EstadoPoliza,
    PlazoDeRenovacionVencido,
    Poliza,
    PolizaNoEncontrada,
    PropuestaRenovacion,
    Renovacion,
    RenovacionRealizada,
    SinPropuestaDeRenovacion,
    Vigencia,
    escalar_coberturas,
    now_utc,
    prima_total,
    propuesta_de_renovacion,
    sumar_meses,
)
from app.logging_utils import sanear_para_log
from app.ports import PolizaRepository

logger = logging.getLogger("svc_core.renovaciones")


@dataclass(frozen=True)
class ResumenEjecucion:
    corte: datetime
    avisos_vencimiento: int = 0
    avisos_banco: int = 0
    renovadas: int = 0
    desactivadas: int = 0


def _dia(valor: datetime) -> date:
    """El día de Colombia: los aniversarios cuentan en días, no en instantes."""
    return valor.astimezone(ZONA_COLOMBIA).date()


class RenovacionService:
    def __init__(
        self,
        polizas: PolizaRepository,
        avisos: AvisoService | None = None,
        *,
        ipc: Decimal,
        ahora: Callable[[], datetime] = now_utc,
    ) -> None:
        self._polizas = polizas
        self._avisos = avisos
        self._ipc = ipc
        self._ahora = ahora

    @property
    def ipc(self) -> Decimal:
        return self._ipc

    def propuesta(self, poliza: Poliza) -> PropuestaRenovacion | None:
        return propuesta_de_renovacion(poliza, self._ipc)

    # --- decisión del cliente ---

    async def decidir(
        self, cliente_id: str, poliza_id: str, decision: DecisionRenovacion
    ) -> Poliza:
        """Guarda la decisión del cliente. Se puede cambiar hasta que se cierre (30 días antes)."""
        poliza = await self._polizas.obtener(poliza_id)
        if poliza is None or poliza.cliente_id != cliente_id:
            raise PolizaNoEncontrada(poliza_id)
        r = poliza.renovacion
        if (
            poliza.estado is not EstadoPoliza.EMITIDA
            or poliza.vigencia is None
            or r.aviso_vencimiento_en is None
        ):
            raise SinPropuestaDeRenovacion()
        propuesta = propuesta_de_renovacion(poliza, self._ipc)
        assert propuesta is not None
        if r.decision_cerrada_en is not None or _dia(self._ahora()) >= _dia(
            propuesta.decidir_hasta
        ):
            raise PlazoDeRenovacionVencido()
        nueva = replace(r, decision=decision, decidida_en=self._ahora())
        await self._polizas.guardar_renovacion(poliza.id, nueva)
        logger.info(
            "renovación decidida poliza_id=%s decision=%s",
            sanear_para_log(poliza.id),
            decision,
        )
        return replace(poliza, renovacion=nueva)

    # --- trabajo diario ---

    async def ejecutar(self, corte: datetime | None = None) -> ResumenEjecucion:
        """Las tres pasadas, en orden, con ``corte`` como "hoy" (por defecto, ahora)."""
        ahora = corte or self._ahora()
        hoy = _dia(ahora)
        vencimiento = await self._pasada_vencimiento(ahora, hoy)
        banco = await self._pasada_banco(ahora, hoy)
        renovadas, desactivadas = await self._pasada_aniversario(ahora, hoy)
        resumen = ResumenEjecucion(ahora, vencimiento, banco, renovadas, desactivadas)
        logger.info(
            "renovaciones ejecutadas corte=%s avisos_vencimiento=%d avisos_banco=%d "
            "renovadas=%d desactivadas=%d",
            hoy.isoformat(),
            vencimiento,
            banco,
            renovadas,
            desactivadas,
        )
        return resumen

    async def _pasada_vencimiento(self, ahora: datetime, hoy: date) -> int:
        hechas = 0
        for p in await self._polizas.emitidas():
            if p.vigencia is None or p.renovacion.aviso_vencimiento_en is not None:
                continue
            aniversario = _dia(p.vigencia.hasta)
            if not aniversario - timedelta(days=DIAS_AVISO_VENCIMIENTO) <= hoy < aniversario:
                continue
            # La marca va primero: si el aviso falla se deshace y la próxima corrida reintenta.
            marcada = replace(p.renovacion, aviso_vencimiento_en=ahora)
            await self._polizas.guardar_renovacion(p.id, marcada)
            try:
                await self._avisar(p, Aviso.RENOVACION_VENCIMIENTO, propagar=True)
            except Exception:
                logger.exception(
                    "no se pudo publicar el aviso de vencimiento poliza_id=%s",
                    sanear_para_log(p.id),
                )
                await self._polizas.guardar_renovacion(p.id, p.renovacion)
                continue
            hechas += 1
        return hechas

    async def _pasada_banco(self, ahora: datetime, hoy: date) -> int:
        hechos = 0
        for p in await self._polizas.emitidas():
            r = p.renovacion
            if p.vigencia is None or r.decision_cerrada_en is not None:
                continue
            if hoy < _dia(p.vigencia.hasta) - timedelta(days=DIAS_AVISO_BANCO):
                continue
            aviso = None
            if r.decision is DecisionRenovacion.RECHAZAR:
                aviso = AvisoBanco(
                    entidad_acreedora=p.datos_credito.entidad_acreedora,
                    tipo=TIPO_AVISO_NO_RENOVACION,
                    fecha_efectiva=p.vigencia.hasta,
                    registrado_en=ahora,
                )
            elif r.aviso_vencimiento_en is None:
                continue  # sin propuesta no hay decisión que cerrar
            await self._polizas.guardar_renovacion(
                p.id, replace(r, decision_cerrada_en=ahora, aviso_banco=aviso)
            )
            if aviso is not None:
                hechos += 1
                logger.info(
                    "aviso al banco poliza_id=%s entidad=%s tipo=%s fecha_efectiva=%s "
                    "registrado_en=%s canal=%s",
                    sanear_para_log(p.id),
                    sanear_para_log(aviso.entidad_acreedora),
                    aviso.tipo,
                    _dia(aviso.fecha_efectiva).isoformat(),
                    aviso.registrado_en.isoformat(),
                    aviso.canal,
                )
        return hechos

    async def _pasada_aniversario(self, ahora: datetime, hoy: date) -> tuple[int, int]:
        renovadas = desactivadas = 0
        for p in await self._polizas.emitidas():
            if p.vigencia is None or hoy < _dia(p.vigencia.hasta):
                continue
            if p.renovacion.decision is DecisionRenovacion.RECHAZAR:
                if await self._desactivar(p, ahora):
                    desactivadas += 1
            elif await self._renovar(p, ahora):
                renovadas += 1
        return renovadas, desactivadas

    async def _renovar(self, p: Poliza, ahora: datetime) -> bool:
        assert p.vigencia is not None
        vigencia = Vigencia(p.vigencia.hasta, sumar_meses(p.vigencia.hasta, MESES_VIGENCIA))
        coberturas = escalar_coberturas(p.coberturas, self._ipc)
        realizada = RenovacionRealizada(
            renovada_en=ahora,
            vigencia_anterior=p.vigencia,
            vigencia_nueva=vigencia,
            prima_anterior=p.prima_total_mensual,
            prima_nueva=prima_total(coberturas),
            ipc=self._ipc,
        )
        # Las marcas del ciclo terminan: el siguiente aniversario empieza de cero.
        nueva = Renovacion(historial=(*p.renovacion.historial, realizada))
        renovada = await self._polizas.renovar(p.id, p.vigencia.hasta, vigencia, coberturas, nueva)
        if renovada is None:
            return False
        logger.info(
            "póliza renovada poliza_id=%s vigencia_hasta=%s prima_anterior=%s prima_nueva=%s "
            "ipc=%s",
            sanear_para_log(p.id),
            vigencia.hasta.isoformat(),
            realizada.prima_anterior,
            realizada.prima_nueva,
            realizada.ipc,
        )
        await self._avisar(renovada, Aviso.RENOVACION_RENOVADA)
        return True

    async def _desactivar(self, p: Poliza, ahora: datetime) -> bool:
        assert p.vigencia is not None
        nueva = replace(p.renovacion, desactivada_en=ahora)
        desactivada = await self._polizas.desactivar(p.id, p.vigencia.hasta, nueva)
        if desactivada is None:
            return False
        logger.info("póliza desactivada poliza_id=%s", sanear_para_log(p.id))
        await self._avisar(desactivada, Aviso.RENOVACION_DESACTIVADA)
        return True

    async def _avisar(self, poliza: Poliza, aviso: Aviso, *, propagar: bool = False) -> None:
        """El aviso push complementa al estado de la póliza: si ya cambió, un fallo solo se
        registra. Con ``propagar`` el fallo sube, para que quien llama deshaga su marca."""
        if self._avisos is None:
            return
        try:
            await self._avisos.avisar(poliza.cliente_id, aviso, ruta_seguro(poliza.id))
        except Exception:
            if propagar:
                raise
            logger.exception(
                "no se pudo publicar el aviso %s poliza_id=%s", aviso, sanear_para_log(poliza.id)
            )
