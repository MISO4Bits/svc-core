"""Rutas de los siniestros (BITS-381). Se montan bajo ``/v1``.

Las llama el BFF móvil con el ``clienteId`` de la sesión. El reporte es ``multipart``: la parte
``datos`` (JSON) lleva la póliza, el tipo, la fecha, la narración, el destino del pago y, por cada
archivo, su clase y su ubicación; las partes ``archivos`` son las imágenes, en el mismo orden.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Header, Request, Response, UploadFile, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from app.api.schemas import ReportarSiniestroDatos, SiniestroReportadoOut
from app.domain import SolicitudSiniestroInvalida
from app.logging_utils import sanear_para_log
from app.siniestros import ArchivoEntrada, ReporteSiniestro, SiniestroService

logger = logging.getLogger("svc_core.api")
router = APIRouter()


def get_siniestros(request: Request) -> SiniestroService:
    return request.app.state.siniestros


SiniestrosDep = Annotated[SiniestroService, Depends(get_siniestros)]


def _leer_datos(crudo: str) -> ReportarSiniestroDatos:
    """Valida la parte ``datos``. Los errores salen con el campo (``datos.tipo``, ...) y sin el
    valor recibido: ahí puede venir un número de cuenta."""
    try:
        return ReportarSiniestroDatos.model_validate_json(crudo)
    except ValidationError as exc:
        raise RequestValidationError(
            [
                {"loc": ("datos", *e["loc"]), "msg": e["msg"], "type": e["type"]}
                for e in exc.errors()
            ]
        ) from None


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
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=64)] = None,
) -> SiniestroReportadoOut:
    reporte = _leer_datos(datos)
    # Ni el destino del pago ni las imágenes se escriben en el log.
    logger.info(
        "POST /v1/clientes/%s/siniestros: solicitud recibida tipo=%s archivos=%d",
        sanear_para_log(cliente_id),
        reporte.tipo,
        len(archivos),
    )
    if len(archivos) != len(reporte.archivos):
        raise SolicitudSiniestroInvalida(
            "archivos", "Debe haber un dato por cada archivo, en el mismo orden"
        )
    entradas = []
    for parte, dato in zip(archivos, reporte.archivos, strict=True):
        # Se lee un byte de más para detectar el exceso sin cargar un archivo sin límite.
        contenido = await parte.read(servicio.max_bytes + 1)
        entradas.append(
            ArchivoEntrada(
                contenido=contenido,
                clase=dato.clase,
                capturado_en=dato.capturado_en,
                latitud=dato.latitud,
                longitud=dato.longitud,
            )
        )
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
            archivos=tuple(entradas),
        ),
        idempotency_key,
    )
    response.headers["Cache-Control"] = "no-store"
    return SiniestroReportadoOut.model_validate(siniestro)
