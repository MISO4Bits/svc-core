"""Manejo de errores en formato RFC 9457 (Problem Details)."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.domain import (
    ClienteNoActivo,
    ClienteNoEncontrado,
    ClienteYaExiste,
    ConflictoPoliza,
    ConsentimientoNoEncontrado,
    CotizacionNoDisponible,
    DocumentoNoDisponible,
    DomainError,
    FueraDeRango,
    ImagenInvalida,
    ImagenMuyGrande,
    PendienteNoAplica,
    PolizaNoEncontrada,
    SolicitudPolizaInvalida,
)
from app.logging_utils import sanear_para_log

logger = logging.getLogger("svc_core.api")
PROBLEM_MEDIA_TYPE = "application/problem+json"


def problema(
    status: int,
    title: str,
    *,
    detail: str | None = None,
    instance: str | None = None,
    errores: list[dict] | None = None,
    codigo: str | None = None,
) -> JSONResponse:
    # Punto único de log para todos los handlers de abajo — todos pasan
    # por aquí con su instance=str(request.url), que puede traer \r\n
    # inyectados por el cliente (CWE-117) y hay que sanear antes de
    # escribirlo en un log.
    logger.warning("%s (%s): %s", title, status, sanear_para_log(instance or ""))
    body: dict = {"type": "about:blank", "title": title, "status": status}
    if detail:
        body["detail"] = detail
    if instance:
        body["instance"] = instance
    if errores:
        body["errores"] = errores
    if codigo:
        body["codigo"] = codigo
    return JSONResponse(status_code=status, content=body, media_type=PROBLEM_MEDIA_TYPE)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ClienteYaExiste)
    async def _ya_existe(request: Request, exc: ClienteYaExiste) -> JSONResponse:
        return problema(
            409,
            "El cliente ya existe",
            detail=str(exc),
            instance=str(request.url),
            errores=[{"campo": exc.campo, "mensaje": str(exc)}],
        )

    @app.exception_handler(ClienteNoEncontrado)
    async def _cliente_no_encontrado(request: Request, exc: ClienteNoEncontrado) -> JSONResponse:
        return problema(404, "Cliente no encontrado", detail=str(exc), instance=str(request.url))

    @app.exception_handler(ConsentimientoNoEncontrado)
    async def _consentimiento_no_encontrado(
        request: Request, exc: ConsentimientoNoEncontrado
    ) -> JSONResponse:
        return problema(
            404,
            "Consentimiento no encontrado",
            detail=str(exc),
            instance=str(request.url),
        )

    @app.exception_handler(ImagenInvalida)
    async def _imagen_invalida(request: Request, exc: ImagenInvalida) -> JSONResponse:
        return problema(400, "Imagen inválida", detail=str(exc), instance=str(request.url))

    @app.exception_handler(ImagenMuyGrande)
    async def _imagen_muy_grande(request: Request, exc: ImagenMuyGrande) -> JSONResponse:
        return problema(413, "Imagen demasiado grande", detail=str(exc), instance=str(request.url))

    @app.exception_handler(PolizaNoEncontrada)
    async def _poliza_no_encontrada(request: Request, exc: PolizaNoEncontrada) -> JSONResponse:
        return problema(404, "Póliza no encontrada", detail=str(exc), instance=str(request.url))

    @app.exception_handler(ClienteNoActivo)
    async def _cliente_no_activo(request: Request, exc: ClienteNoActivo) -> JSONResponse:
        return problema(
            403,
            "El cliente no está activo",
            detail=str(exc),
            instance=str(request.url),
            codigo="CLIENTE_NO_ACTIVO",
        )

    @app.exception_handler(SolicitudPolizaInvalida)
    async def _solicitud_poliza_invalida(
        request: Request, exc: SolicitudPolizaInvalida
    ) -> JSONResponse:
        return problema(
            400,
            "Solicitud inválida",
            detail=exc.mensaje,
            instance=str(request.url),
            errores=[{"campo": exc.campo, "mensaje": exc.mensaje}],
        )

    @app.exception_handler(FueraDeRango)
    async def _fuera_de_rango(request: Request, exc: FueraDeRango) -> JSONResponse:
        return problema(
            422,
            "Edad o plazo fuera de rango",
            detail=str(exc),
            instance=str(request.url),
            codigo="FUERA_DE_RANGO",
        )

    @app.exception_handler(PendienteNoAplica)
    async def _pendiente_no_aplica(request: Request, exc: PendienteNoAplica) -> JSONResponse:
        return problema(
            422,
            "El pendiente no aplica",
            detail=str(exc),
            instance=str(request.url),
            codigo="PENDIENTE_NO_APLICA",
        )

    @app.exception_handler(ConflictoPoliza)
    async def _conflicto_poliza(request: Request, exc: ConflictoPoliza) -> JSONResponse:
        return problema(
            409,
            "Conflicto con el estado de la póliza",
            detail=str(exc),
            instance=str(request.url),
            codigo=exc.codigo,
        )

    @app.exception_handler(CotizacionNoDisponible)
    async def _cotizacion_no_disponible(
        request: Request, exc: CotizacionNoDisponible
    ) -> JSONResponse:
        return problema(
            503,
            "Cotización no disponible",
            detail=str(exc),
            instance=str(request.url),
            codigo="COTIZACION_NO_DISPONIBLE",
        )

    @app.exception_handler(DocumentoNoDisponible)
    async def _documento_no_disponible(
        request: Request, exc: DocumentoNoDisponible
    ) -> JSONResponse:
        return problema(
            503,
            "Documento no disponible",
            detail=str(exc),
            instance=str(request.url),
            codigo="DOCUMENTO_NO_DISPONIBLE",
        )

    @app.exception_handler(DomainError)
    async def _regla_negocio(request: Request, exc: DomainError) -> JSONResponse:
        return problema(
            422,
            "Regla de negocio no satisfecha",
            detail=str(exc),
            instance=str(request.url),
        )

    @app.exception_handler(RequestValidationError)
    async def _validacion(request: Request, exc: RequestValidationError) -> JSONResponse:
        errores = [
            {"campo": ".".join(str(p) for p in err["loc"]), "mensaje": err["msg"]}
            for err in exc.errors()
        ]
        return problema(
            400,
            "Solicitud inválida",
            detail="La solicitud no cumple el esquema",
            instance=str(request.url),
            errores=errores,
        )
