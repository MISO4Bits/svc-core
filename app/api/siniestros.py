"""Rutas de los siniestros del cliente (BITS-381, BITS-383, BITS-384). Se montan bajo ``/v1``.

Las llaman el BFF móvil y el BFF web con el ``clienteId`` de la sesión. El reporte y el envío de
lo que falta son ``multipart``: la parte ``datos`` (JSON) lleva, por cada archivo, su clase y su
ubicación; las partes ``archivos`` son las imágenes, en el mismo orden.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Header, Request, Response, UploadFile, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError

from app.api.schemas import (
    AsistenciaOut,
    EntregarEvidenciasDatos,
    PedirAyudaRequest,
    ReportarSiniestroDatos,
    SiniestroOut,
    SiniestroReportadoOut,
    SiniestroResumenOut,
)
from app.domain import PrestadorAsistencia, SolicitudSiniestroInvalida
from app.logging_utils import sanear_para_log
from app.siniestros import ArchivoEntrada, AsistenciaEntrada, ReporteSiniestro, SiniestroService

logger = logging.getLogger("svc_core.api")
router = APIRouter()


def get_siniestros(request: Request) -> SiniestroService:
    return request.app.state.siniestros


SiniestrosDep = Annotated[SiniestroService, Depends(get_siniestros)]
LlaveDeIdempotencia = Annotated[str | None, Header(alias="Idempotency-Key", max_length=64)]


def _leer_datos[T: BaseModel](modelo: type[T], crudo: str) -> T:
    """Valida la parte ``datos``. Los errores salen con el campo (``datos.tipo``, ...) y sin el
    valor recibido: ahí puede venir un número de cuenta."""
    try:
        return modelo.model_validate_json(crudo)
    except ValidationError as exc:
        raise RequestValidationError(
            [
                {"loc": ("datos", *e["loc"]), "msg": e["msg"], "type": e["type"]}
                for e in exc.errors()
            ]
        ) from None


async def _leer_archivos(
    partes: list[UploadFile],
    datos: EntregarEvidenciasDatos | ReportarSiniestroDatos,
    max_bytes: int,
) -> tuple[ArchivoEntrada, ...]:
    if len(partes) != len(datos.archivos):
        raise SolicitudSiniestroInvalida(
            "archivos", "Debe haber un dato por cada archivo, en el mismo orden"
        )
    entradas = []
    for parte, dato in zip(partes, datos.archivos, strict=True):
        # Se lee un byte de más para detectar el exceso sin cargar un archivo sin límite.
        contenido = await parte.read(max_bytes + 1)
        entradas.append(
            ArchivoEntrada(
                contenido=contenido,
                clase=dato.clase,
                capturado_en=dato.capturado_en,
                latitud=dato.latitud,
                longitud=dato.longitud,
            )
        )
    return tuple(entradas)


@router.post(
    "/clientes/{cliente_id}/siniestros",
    response_model=SiniestroReportadoOut,
    status_code=status.HTTP_201_CREATED,
    tags=["Siniestros"],
)
async def reportar_siniestro(
    cliente_id: str,
    servicio: SiniestrosDep,
    response: Response,
    datos: Annotated[str, Form()],
    archivos: Annotated[list[UploadFile], File()],
    idempotency_key: LlaveDeIdempotencia = None,
) -> SiniestroReportadoOut:
    reporte = _leer_datos(ReportarSiniestroDatos, datos)
    # Ni el destino del pago ni las imágenes se escriben en el log.
    logger.info(
        "POST /v1/clientes/%s/siniestros: solicitud recibida tipo=%s archivos=%d",
        sanear_para_log(cliente_id),
        reporte.tipo,
        len(archivos),
    )
    entradas = await _leer_archivos(archivos, reporte, servicio.max_bytes)
    siniestro = await servicio.reportar(
        cliente_id,
        ReporteSiniestro(
            poliza_id=reporte.poliza_id,
            tipo=reporte.tipo,
            fecha_ocurrencia=reporte.fecha_ocurrencia,
            narracion=reporte.narracion,
            # Se guarda tal como llega, ya validado por el esquema.
            destino_pago=(
                reporte.destino_pago.model_dump(by_alias=True) if reporte.destino_pago else None
            ),
            archivos=entradas,
        ),
        idempotency_key,
    )
    response.headers["Cache-Control"] = "no-store"
    return SiniestroReportadoOut.model_validate(siniestro)


@router.get(
    "/clientes/{cliente_id}/siniestros",
    response_model=list[SiniestroResumenOut],
    tags=["Siniestros"],
)
async def listar_siniestros(cliente_id: str, servicio: SiniestrosDep) -> list[SiniestroResumenOut]:
    logger.info("GET /v1/clientes/%s/siniestros: solicitud recibida", sanear_para_log(cliente_id))
    siniestros = await servicio.listar(cliente_id)
    return [SiniestroResumenOut.model_validate(s) for s in siniestros]


@router.get(
    "/clientes/{cliente_id}/siniestros/{siniestro_id}",
    response_model=SiniestroOut,
    tags=["Siniestros"],
)
async def obtener_siniestro(
    cliente_id: str, siniestro_id: str, servicio: SiniestrosDep
) -> SiniestroOut:
    logger.info(
        "GET /v1/clientes/%s/siniestros/%s: solicitud recibida",
        sanear_para_log(cliente_id),
        sanear_para_log(siniestro_id),
    )
    return SiniestroOut.model_validate(await servicio.obtener(cliente_id, siniestro_id))


@router.post(
    "/clientes/{cliente_id}/siniestros/{siniestro_id}/evidencias",
    response_model=SiniestroOut,
    status_code=status.HTTP_201_CREATED,
    tags=["Siniestros"],
)
async def entregar_evidencias(
    cliente_id: str,
    siniestro_id: str,
    servicio: SiniestrosDep,
    response: Response,
    datos: Annotated[str, Form()],
    archivos: Annotated[list[UploadFile], File()],
    idempotency_key: LlaveDeIdempotencia = None,
) -> SiniestroOut:
    entrega = _leer_datos(EntregarEvidenciasDatos, datos)
    logger.info(
        "POST /v1/clientes/%s/siniestros/%s/evidencias: solicitud recibida archivos=%d",
        sanear_para_log(cliente_id),
        sanear_para_log(siniestro_id),
        len(archivos),
    )
    entradas = await _leer_archivos(archivos, entrega, servicio.max_bytes)
    siniestro = await servicio.entregar_evidencias(
        cliente_id, siniestro_id, entradas, idempotency_key
    )
    response.headers["Cache-Control"] = "no-store"
    return SiniestroOut.model_validate(siniestro)


@router.post(
    "/clientes/{cliente_id}/siniestros/{siniestro_id}/asistencias",
    response_model=AsistenciaOut,
    status_code=status.HTTP_201_CREATED,
    tags=["Siniestros"],
)
async def pedir_ayuda(
    cliente_id: str,
    siniestro_id: str,
    payload: PedirAyudaRequest,
    servicio: SiniestrosDep,
    response: Response,
    idempotency_key: LlaveDeIdempotencia = None,
) -> AsistenciaOut:
    # La ubicación y la dirección del cliente no se escriben en el log.
    logger.info(
        "POST /v1/clientes/%s/siniestros/%s/asistencias: solicitud recibida tipo=%s origen=%s",
        sanear_para_log(cliente_id),
        sanear_para_log(siniestro_id),
        payload.tipo,
        payload.origen,
    )
    asistencia = await servicio.pedir_ayuda(
        cliente_id,
        siniestro_id,
        AsistenciaEntrada(
            tipo=payload.tipo,
            prestador=PrestadorAsistencia(
                id=payload.prestador.id,
                nombre=payload.prestador.nombre,
                direccion=payload.prestador.direccion,
                tiempo_llegada_min=payload.prestador.tiempo_llegada_min,
            ),
            origen=payload.origen,
            latitud=payload.latitud,
            longitud=payload.longitud,
            direccion=payload.direccion,
            localidad=payload.localidad,
        ),
        idempotency_key,
    )
    response.headers["Cache-Control"] = "no-store"
    return AsistenciaOut.desde_dominio(asistencia)
