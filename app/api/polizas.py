"""Rutas de las pólizas (BITS-101). Se montan bajo ``/v1``.

Dos conjuntos: las del cliente (``/clientes/{id}/polizas``, las que llaman los BFF) y las del
backoffice (``/polizas``, internas, no pasan por el gateway).
"""

from __future__ import annotations

import base64
import binascii
import logging
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.api.schemas import (
    CoberturaContratadaOut,
    DatosCreditoOut,
    DecisionOut,
    DecisionRequest,
    DetallePendienteOut,
    DocumentoAceptadoOut,
    DocumentoPolizaOut,
    EstadoDocumentoOut,
    EvidenciaDePolizaOut,
    MotivoRechazoOut,
    PaginaPolizasOut,
    PendienteOut,
    PendienteResumenOut,
    PolizaInternaOut,
    PolizaOut,
    PolizaResumenOut,
    RechazoOut,
    SolicitarPolizaRequest,
    VigenciaOut,
)
from app.domain import (
    CodigoPendiente,
    EstadoDocumentoPoliza,
    EstadoPoliza,
    FiltroPolizas,
    Poliza,
    SolicitudPolizaInvalida,
    TipoPendiente,
    now_utc,
)
from app.logging_utils import sanear_para_log
from app.polizas import PolizaService

logger = logging.getLogger("svc_core.api")
router = APIRouter()

REINTENTO_DOCUMENTO_S = 2


def get_polizas(request: Request) -> PolizaService:
    return request.app.state.polizas


PolizasDep = Annotated[PolizaService, Depends(get_polizas)]


def a_poliza_out(poliza: Poliza) -> PolizaOut:
    return PolizaOut(**_campos_poliza(poliza))


def _campos_poliza(p: Poliza) -> dict:
    ahora = now_utc()
    estado = p.estado_en(ahora)
    d = p.datos_credito
    return {
        "poliza_id": p.id,
        "numero": p.numero,
        "estado": estado,
        "creada_en": p.creada_en,
        "emitida_en": p.emitida_en,
        "solicitud_vigente_hasta": (
            None if estado is EstadoPoliza.EMITIDA else p.solicitud_vigente_hasta
        ),
        "vigencia": VigenciaOut(desde=p.vigencia.desde, hasta=p.vigencia.hasta)
        if p.vigencia
        else None,
        "coberturas": [
            CoberturaContratadaOut(
                codigo=c.codigo,
                obligatoria=c.obligatoria,
                suma_asegurada=float(c.suma_asegurada),
                prima_neta=float(c.prima_neta),
                iva=float(c.iva),
                prima_total=float(c.prima_total),
            )
            for c in p.coberturas
        ],
        "prima_total_mensual": float(p.prima_total_mensual),
        "datos_credito": DatosCreditoOut(
            valor_credito=float(d.valor_credito),
            plazo_restante_meses=d.plazo_restante_meses,
            entidad_acreedora=d.entidad_acreedora,
            saldo_insoluto=float(d.saldo_insoluto),
        ),
        "origen_datos": p.origen_datos,
        "cotizacion_creada_en": p.cotizacion_creada_en,
        "pendientes": [
            PendienteOut(
                codigo=x.codigo,
                tipo=x.tipo,
                mensaje=x.mensaje,
                detalle=DetallePendienteOut(evidencias=list(x.evidencias))
                if x.evidencias
                else None,
            )
            for x in p.pendientes
        ],
        "documento": EstadoDocumentoOut(
            estado="DISPONIBLE" if p.estado is EstadoPoliza.EMITIDA else "NO_DISPONIBLE"
        ),
        "documentos_aceptados": [
            DocumentoAceptadoOut(tipo=x.tipo, version=x.version, aceptado_en=x.aceptado_en)
            for x in p.documentos_aceptados
        ],
        "rechazo": RechazoOut(
            motivos=[
                MotivoRechazoOut(codigo=m.codigo, mensaje=m.mensaje) for m in p.motivos_rechazo
            ],
            rechazada_en=p.rechazada_en,
        )
        if p.estado is EstadoPoliza.RECHAZADA and p.rechazada_en
        else None,
    }


async def _a_poliza_interna_out(service: PolizaService, p: Poliza) -> PolizaInternaOut:
    if p.estado is EstadoPoliza.EMITIDA:
        evidencias = [
            EvidenciaDePolizaOut(tipo=e.tipo, estado=e.estado.value, evidencia_id=e.evidencia_id)
            for e in p.evidencias
        ]
    else:
        evidencias = [
            EvidenciaDePolizaOut(
                tipo=tipo,
                estado=vigente.estado.value if vigente else "PENDIENTE",
                evidencia_id=vigente.id if vigente else None,
            )
            for tipo, vigente in await service.evidencias_vigentes(p.cliente_id)
        ]
    return PolizaInternaOut(
        **_campos_poliza(p),
        cliente_id=p.cliente_id,
        ultima_evaluacion_en=p.ultima_evaluacion_en,
        tasa=float(p.tasa),
        version_tarifa=p.version_tarifa,
        evidencias=evidencias,
        decisiones=[
            DecisionOut(
                decision=d.decision,
                agente=d.agente,
                pendientes=list(d.pendientes),
                nota=d.nota,
                decidida_en=d.decidida_en,
            )
            for d in p.decisiones
        ],
    )


# --- cliente ---


@router.post(
    "/clientes/{cliente_id}/polizas",
    response_model=PolizaOut,
    status_code=status.HTTP_201_CREATED,
    tags=["Pólizas"],
)
async def solicitar_poliza(
    cliente_id: str,
    payload: SolicitarPolizaRequest,
    service: PolizasDep,
    response: Response,
) -> PolizaOut:
    logger.info("POST /v1/clientes/%s/polizas: solicitud recibida", sanear_para_log(cliente_id))
    poliza, creada = await service.solicitar(
        cliente_id,
        payload.coberturas,
        payload.cotizacion_creada_en,
        [(d.tipo, d.version) for d in payload.documentos_aceptados],
    )
    if not creada:
        response.status_code = status.HTTP_200_OK
    return a_poliza_out(poliza)


@router.get("/clientes/{cliente_id}/polizas", response_model=list[PolizaOut], tags=["Pólizas"])
async def listar_polizas_de_cliente(
    cliente_id: str,
    service: PolizasDep,
    estado: Annotated[EstadoPoliza | None, Query()] = None,
) -> list[PolizaOut]:
    logger.info("GET /v1/clientes/%s/polizas: solicitud recibida", sanear_para_log(cliente_id))
    return [a_poliza_out(p) for p in await service.listar_de_cliente(cliente_id, estado)]


@router.get(
    "/clientes/{cliente_id}/polizas/{poliza_id}", response_model=PolizaOut, tags=["Pólizas"]
)
async def obtener_poliza_de_cliente(
    cliente_id: str, poliza_id: str, service: PolizasDep
) -> PolizaOut:
    logger.info(
        "GET /v1/clientes/%s/polizas/%s: solicitud recibida",
        sanear_para_log(cliente_id),
        sanear_para_log(poliza_id),
    )
    return a_poliza_out(await service.obtener(cliente_id, poliza_id))


@router.get(
    "/clientes/{cliente_id}/polizas/{poliza_id}/documento",
    response_model=DocumentoPolizaOut,
    tags=["Pólizas"],
)
async def documento_de_poliza(
    cliente_id: str, poliza_id: str, service: PolizasDep, response: Response
) -> DocumentoPolizaOut:
    logger.info(
        "GET /v1/clientes/%s/polizas/%s/documento: solicitud recibida",
        sanear_para_log(cliente_id),
        sanear_para_log(poliza_id),
    )
    documento = await service.documento(cliente_id, poliza_id)
    if documento.estado is EstadoDocumentoPoliza.GENERANDO:
        response.status_code = status.HTTP_202_ACCEPTED
        response.headers["Retry-After"] = str(REINTENTO_DOCUMENTO_S)
    return DocumentoPolizaOut(
        estado=documento.estado, url=documento.url, expira_en=documento.expira_en
    )


# --- backoffice ---


def _validar_cursor(cursor: str | None) -> None:
    if cursor is None:
        return
    try:
        creada_en, separador, _ = base64.urlsafe_b64decode(cursor.encode()).decode().partition("|")
        datetime.fromisoformat(creada_en)
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise SolicitudPolizaInvalida("cursor", "El cursor no es válido") from exc
    if not separador:
        raise SolicitudPolizaInvalida("cursor", "El cursor no es válido")


@router.get("/polizas", response_model=PaginaPolizasOut, tags=["Pólizas (backoffice)"])
async def listar_polizas(
    service: PolizasDep,
    estado: Annotated[EstadoPoliza | None, Query()] = None,
    tipo_pendiente: Annotated[TipoPendiente | None, Query(alias="tipoPendiente")] = None,
    codigo_pendiente: Annotated[CodigoPendiente | None, Query(alias="codigoPendiente")] = None,
    cliente_id: Annotated[str | None, Query(alias="clienteId", max_length=64)] = None,
    creada_desde: Annotated[datetime | None, Query(alias="creadaDesde")] = None,
    creada_hasta: Annotated[datetime | None, Query(alias="creadaHasta")] = None,
    orden: Annotated[Literal["creadaEn", "-creadaEn"], Query()] = "creadaEn",
    limite: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
) -> PaginaPolizasOut:
    logger.info("GET /v1/polizas: solicitud recibida estado=%s", estado)
    _validar_cursor(cursor)
    pagina = await service.listar(
        FiltroPolizas(
            estado=estado,
            tipo_pendiente=tipo_pendiente,
            codigo_pendiente=codigo_pendiente,
            cliente_id=cliente_id,
            creada_desde=creada_desde,
            creada_hasta=creada_hasta,
            mas_recientes_primero=orden == "-creadaEn",
            limite=limite,
            cursor=cursor,
        )
    )
    ahora = now_utc()
    return PaginaPolizasOut(
        items=[
            PolizaResumenOut(
                poliza_id=p.id,
                numero=p.numero,
                cliente_id=p.cliente_id,
                estado=p.estado_en(ahora),
                creada_en=p.creada_en,
                solicitud_vigente_hasta=(
                    None if p.estado is EstadoPoliza.EMITIDA else p.solicitud_vigente_hasta
                ),
                prima_total_mensual=float(p.prima_total_mensual),
                origen_datos=p.origen_datos,
                pendientes=[
                    PendienteResumenOut(codigo=x.codigo, tipo=x.tipo) for x in p.pendientes
                ],
            )
            for p in pagina.items
        ],
        siguiente_cursor=pagina.siguiente_cursor,
    )


@router.get("/polizas/{poliza_id}", response_model=PolizaInternaOut, tags=["Pólizas (backoffice)"])
async def obtener_poliza_interna(poliza_id: str, service: PolizasDep) -> PolizaInternaOut:
    logger.info("GET /v1/polizas/%s: solicitud recibida", sanear_para_log(poliza_id))
    return await _a_poliza_interna_out(service, await service.obtener_interna(poliza_id))


@router.post(
    "/polizas/{poliza_id}/decisiones",
    response_model=PolizaInternaOut,
    tags=["Pólizas (backoffice)"],
)
async def decidir_poliza(
    poliza_id: str, payload: DecisionRequest, service: PolizasDep
) -> PolizaInternaOut:
    logger.info(
        "POST /v1/polizas/%s/decisiones: solicitud recibida decision=%s",
        sanear_para_log(poliza_id),
        payload.decision,
    )
    poliza = await service.decidir(
        poliza_id, payload.decision, payload.agente, payload.pendientes, payload.nota
    )
    return await _a_poliza_interna_out(service, poliza)
