"""Siniestros (BITS-381): el cliente reporta un incendio, un terremoto o una incapacidad total.

El siniestro nace ``EN_ESTUDIO`` con su radicado. Al reportar se guarda, una sola vez, el saldo del
crédito (es lo que se paga al banco) y los archivos van al bucket de evidencias con el prefijo
``siniestros/``. Las decisiones del ``ADMIN`` y los demás estados llegan en las fases siguientes.
"""

from __future__ import annotations

import logging
import secrets
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.domain import (
    MAX_ARCHIVOS_SINIESTRO,
    ArchivoSiniestro,
    ClaseArchivo,
    ClienteNoEncontrado,
    EstadoPoliza,
    EstadoSiniestro,
    FueraDeVigencia,
    NumeroSiniestroDuplicado,
    OrigenSaldo,
    Poliza,
    PolizaNoEncontrada,
    SaldoCredito,
    Siniestro,
    SiniestroEnCurso,
    SiniestroNoEncontrado,
    SolicitudSiniestroInvalida,
    TipoNoCubierto,
    TipoSiniestro,
    TransicionInvalida,
    generar_numero_siniestro,
    new_id,
    now_utc,
)
from app.imagen import validar_imagen
from app.logging_utils import sanear_para_log
from app.ports import (
    AlmacenEvidencias,
    ClienteRepository,
    CreditosPort,
    IdempotencyStore,
    PolizaRepository,
    SiniestroRepository,
)

logger = logging.getLogger("svc_core.siniestros")

# Colombia no tiene horario de verano: UTC-5 fijo, sin depender de la base de zonas horarias.
ZONA_COLOMBIA = timezone(timedelta(hours=-5))
ESTADO_DISPONIBLE = "DISPONIBLE"
MAX_NARRACION = 4000
_EXTENSION = {"image/jpeg": "jpg", "image/png": "png"}
_INTENTOS_NUMERO = 5
_ESTADOS_CON_COBERTURA = (EstadoPoliza.EMITIDA, EstadoPoliza.DESACTIVADA)


def hoy_en_colombia() -> date:
    return now_utc().astimezone(ZONA_COLOMBIA).date()


@dataclass(frozen=True)
class ArchivoEntrada:
    """Un archivo del reporte con sus datos. La ubicación viaja aparte de la foto porque la app
    borra los metadatos al recomprimirla."""

    contenido: bytes
    clase: ClaseArchivo
    capturado_en: datetime | None = None
    latitud: float | None = None
    longitud: float | None = None


@dataclass(frozen=True)
class ReporteSiniestro:
    poliza_id: str
    tipo: TipoSiniestro
    fecha_ocurrencia: date
    archivos: tuple[ArchivoEntrada, ...]
    narracion: str | None = None
    destino_pago: dict | None = None


def _normalizado(texto: str) -> str:
    """Sin tildes ni mayúsculas: el banco se escribe distinto según la fuente."""
    sin_tildes = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return " ".join(sin_tildes.casefold().split())


class SiniestroService:
    def __init__(
        self,
        clientes: ClienteRepository,
        polizas: PolizaRepository,
        siniestros: SiniestroRepository,
        almacen: AlmacenEvidencias,
        creditos: CreditosPort,
        idempotency: IdempotencyStore,
        *,
        max_bytes: int,
        max_pixeles: int,
        ahora: Callable[[], datetime] = now_utc,
        hoy: Callable[[], date] = hoy_en_colombia,
        aleatorio: Callable[[str], str] = secrets.choice,
    ) -> None:
        self._clientes = clientes
        self._polizas = polizas
        self._siniestros = siniestros
        self._almacen = almacen
        self._creditos = creditos
        self._idempotency = idempotency
        self._max_bytes = max_bytes
        self._max_pixeles = max_pixeles
        self._ahora = ahora
        self._hoy = hoy
        self._aleatorio = aleatorio

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    async def reportar(
        self, cliente_id: str, reporte: ReporteSiniestro, idempotency_key: str | None = None
    ) -> Siniestro:
        """Idempotente con ``idempotency_key``: repetir el reporte devuelve el mismo siniestro."""
        clave = f"siniestro:{cliente_id}:{idempotency_key}" if idempotency_key else None
        if clave is not None:
            previo = await self._idempotency.get(clave)
            if previo is not None:
                existente = await self._siniestros.obtener(previo["siniestroId"])
                if existente is not None:
                    logger.info("reporte repetido siniestro_id=%s", existente.id)
                    return existente

        if await self._clientes.obtener(cliente_id) is None:
            raise ClienteNoEncontrado(cliente_id)
        poliza = await self._polizas.obtener(reporte.poliza_id)
        if poliza is None or poliza.cliente_id != cliente_id:
            raise PolizaNoEncontrada(reporte.poliza_id)

        self._validar_vigencia(poliza, reporte.fecha_ocurrencia)
        self._validar_cobertura(poliza, reporte.tipo)
        self._validar_contenido(reporte)
        if await self._siniestros.en_curso(poliza.id, reporte.tipo) is not None:
            raise SiniestroEnCurso()

        siniestro_id = new_id()
        ahora = self._ahora()
        archivos = await self._guardar_archivos(cliente_id, siniestro_id, reporte.archivos, ahora)
        saldo = await self._saldo(cliente_id, poliza, ahora)

        siniestro = await self._crear(
            Siniestro(
                id=siniestro_id,
                cliente_id=cliente_id,
                poliza_id=poliza.id,
                tipo=reporte.tipo,
                fecha_ocurrencia=reporte.fecha_ocurrencia,
                narracion=(reporte.narracion or "").strip() or None,
                archivos=archivos,
                saldo_credito=saldo,
                destino_pago=reporte.destino_pago,
                numero="",
                reportado_en=ahora,
                ultima_entrega_en=ahora,
                actualizado_en=ahora,
            )
        )
        if clave is not None:
            await self._idempotency.put(clave, {"siniestroId": siniestro.id})
        logger.info(
            "siniestro reportado siniestro_id=%s cliente_id=%s tipo=%s saldo_origen=%s",
            siniestro.id,
            sanear_para_log(cliente_id),
            siniestro.tipo,
            siniestro.saldo_credito.origen,
        )
        return siniestro

    # --- consulta ---

    async def obtener(self, cliente_id: str, siniestro_id: str) -> Siniestro:
        """Un siniestro ajeno responde igual que uno que no existe."""
        siniestro = await self._siniestros.obtener(siniestro_id)
        if siniestro is None or siniestro.cliente_id != cliente_id:
            raise SiniestroNoEncontrado(siniestro_id)
        return siniestro

    async def listar(self, cliente_id: str) -> list[Siniestro]:
        if await self._clientes.obtener(cliente_id) is None:
            raise ClienteNoEncontrado(cliente_id)
        return await self._siniestros.listar_de_cliente(cliente_id)

    # --- lo que falta ---

    async def entregar_evidencias(
        self,
        cliente_id: str,
        siniestro_id: str,
        archivos: tuple[ArchivoEntrada, ...],
        idempotency_key: str | None = None,
    ) -> Siniestro:
        """El cliente envía lo que Solventa le pidió: el siniestro vuelve a ``EN_ESTUDIO``, el
        reloj del plazo arranca de nuevo y no se avisa a nadie (la bandeja es el aviso del
        ``ADMIN``). Idempotente con ``idempotency_key``."""
        clave = (
            f"siniestro-evidencias:{cliente_id}:{siniestro_id}:{idempotency_key}"
            if idempotency_key
            else None
        )
        if clave is not None and await self._idempotency.get(clave) is not None:
            logger.info("entrega repetida siniestro_id=%s", sanear_para_log(siniestro_id))
            return await self.obtener(cliente_id, siniestro_id)

        siniestro = await self.obtener(cliente_id, siniestro_id)
        if siniestro.estado is not EstadoSiniestro.FALTA_INFORMACION:
            raise TransicionInvalida(siniestro.estado)
        if not archivos:
            raise SolicitudSiniestroInvalida("archivos", "Falta la evidencia")
        if len(archivos) > MAX_ARCHIVOS_SINIESTRO:
            raise SolicitudSiniestroInvalida("archivos", "Son demasiados archivos")

        ahora = self._ahora()
        guardados = await self._guardar_archivos(cliente_id, siniestro.id, archivos, ahora)
        anterior = siniestro.estado
        siniestro.entregar_evidencia(guardados, ahora)
        if not await self._siniestros.actualizar(siniestro, anterior):
            # Otra entrega llegó primero.
            raise TransicionInvalida(EstadoSiniestro.EN_ESTUDIO)
        if clave is not None:
            await self._idempotency.put(clave, {"siniestroId": siniestro.id})
        logger.info(
            "evidencia entregada siniestro_id=%s cliente_id=%s archivos=%d",
            siniestro.id,
            sanear_para_log(cliente_id),
            len(guardados),
        )
        return siniestro

    # --- validaciones ---

    def _validar_vigencia(self, poliza: Poliza, fecha: date) -> None:
        """La cobertura es continua: se compara con ``emitida_en`` y ``vigencia.hasta``, no con
        ``vigencia.desde``, que la renovación corre."""
        if (
            poliza.estado not in _ESTADOS_CON_COBERTURA
            or poliza.emitida_en is None
            or poliza.vigencia is None
        ):
            raise FueraDeVigencia()
        desde = poliza.emitida_en.astimezone(ZONA_COLOMBIA).date()
        hasta = poliza.vigencia.hasta.astimezone(ZONA_COLOMBIA).date()
        if fecha > self._hoy() or fecha < desde or fecha > hasta:
            raise FueraDeVigencia()

    @staticmethod
    def _validar_cobertura(poliza: Poliza, tipo: TipoSiniestro) -> None:
        if tipo.cobertura not in poliza.codigos_cobertura():
            raise TipoNoCubierto(tipo)

    @staticmethod
    def _validar_contenido(reporte: ReporteSiniestro) -> None:
        if reporte.tipo is TipoSiniestro.SISMO_PARAMETRICO:
            raise SolicitudSiniestroInvalida(
                "tipo", "Este siniestro lo crea Solventa, no el cliente"
            )
        incendio = reporte.tipo is TipoSiniestro.INCENDIO_TERREMOTO
        narracion = (reporte.narracion or "").strip()
        if incendio and not narracion:
            raise SolicitudSiniestroInvalida("narracion", "Cuéntanos qué pasó")
        if len(narracion) > MAX_NARRACION:
            raise SolicitudSiniestroInvalida("narracion", "El texto es demasiado largo")
        if incendio and reporte.destino_pago is None:
            raise SolicitudSiniestroInvalida("destinoPago", "Indica cómo quieres recibir el pago")
        if not incendio and reporte.destino_pago is not None:
            raise SolicitudSiniestroInvalida(
                "destinoPago", "La incapacidad total se paga al banco: no lleva destino"
            )
        if not reporte.archivos:
            raise SolicitudSiniestroInvalida("archivos", "Falta la evidencia")
        if len(reporte.archivos) > MAX_ARCHIVOS_SINIESTRO:
            raise SolicitudSiniestroInvalida("archivos", "Son demasiados archivos")
        requerida = ClaseArchivo.FOTO if incendio else ClaseArchivo.DOCUMENTO
        if not any(a.clase is requerida for a in reporte.archivos):
            motivo = "Falta al menos una foto del daño" if incendio else "Falta el dictamen"
            raise SolicitudSiniestroInvalida("archivos", motivo)

    # --- archivos y saldo ---

    async def _guardar_archivos(
        self,
        cliente_id: str,
        siniestro_id: str,
        entradas: tuple[ArchivoEntrada, ...],
        ahora: datetime,
    ) -> tuple[ArchivoSiniestro, ...]:
        # Se validan todos antes de guardar alguno: un archivo malo no deja los otros en el bucket.
        tipos = [
            validar_imagen(a.contenido, max_bytes=self._max_bytes, max_pixeles=self._max_pixeles)
            for a in entradas
        ]
        guardados: list[ArchivoSiniestro] = []
        for entrada, content_type in zip(entradas, tipos, strict=True):
            archivo_id = new_id()
            referencia = await self._almacen.guardar(
                f"siniestros/{cliente_id}/{siniestro_id}/{archivo_id}.{_EXTENSION[content_type]}",
                entrada.contenido,
                content_type,
            )
            guardados.append(
                ArchivoSiniestro(
                    id=archivo_id,
                    clase=entrada.clase,
                    referencia=referencia,
                    content_type=content_type,
                    entregado_en=ahora,
                    capturado_en=entrada.capturado_en,
                    latitud=entrada.latitud,
                    longitud=entrada.longitud,
                )
            )
        return tuple(guardados)

    async def _saldo(self, cliente_id: str, poliza: Poliza, ahora: datetime) -> SaldoCredito:
        """Una sola consulta, al radicar. Toma la hipoteca de la entidad de la póliza (Core no
        guarda el número de la obligación). Si perfilamiento falla, no responde o no hay
        consentimiento, se queda con el saldo de la póliza: el reporte se radica igual."""
        credito = poliza.datos_credito
        reportados = await self._creditos.hipotecas(cliente_id)
        if reportados.estado == ESTADO_DISPONIBLE:
            objetivo = _normalizado(credito.entidad_acreedora)
            hipoteca = next(
                (h for h in reportados.hipotecas if _normalizado(h.entidad_acreedora) == objetivo),
                None,
            )
            if hipoteca is not None:
                return SaldoCredito(
                    valor=Decimal(hipoteca.saldo_insoluto),
                    entidad=credito.entidad_acreedora,
                    consultado_en=ahora,
                    origen=OrigenSaldo.OPEN_FINANCE,
                )
        return SaldoCredito(
            valor=credito.saldo_insoluto,
            entidad=credito.entidad_acreedora,
            consultado_en=ahora,
            origen=OrigenSaldo.POLIZA,
        )

    async def _crear(self, siniestro: Siniestro) -> Siniestro:
        for _ in range(_INTENTOS_NUMERO):
            siniestro.numero = generar_numero_siniestro(siniestro.reportado_en, self._aleatorio)
            try:
                await self._siniestros.crear(siniestro)
            except NumeroSiniestroDuplicado:
                continue
            return siniestro
        raise RuntimeError("no se pudo generar un radicado de siniestro único")
