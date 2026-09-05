"""Manejo de errores en formato RFC 9457 (Problem Details)."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.domain import (
    ClienteNoEncontrado,
    ClienteYaExiste,
    ConsentimientoNoEncontrado,
    DomainError,
)

PROBLEM_MEDIA_TYPE = "application/problem+json"


def problema(
    status: int,
    title: str,
    *,
    detail: str | None = None,
    instance: str | None = None,
    errores: list[dict] | None = None,
) -> JSONResponse:
    body: dict = {"type": "about:blank", "title": title, "status": status}
    if detail:
        body["detail"] = detail
    if instance:
        body["instance"] = instance
    if errores:
        body["errores"] = errores
    return JSONResponse(status_code=status, content=body, media_type=PROBLEM_MEDIA_TYPE)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ClienteYaExiste)
    async def _ya_existe(request: Request, exc: ClienteYaExiste) -> JSONResponse:
        return problema(409, "El cliente ya existe", detail=str(exc), instance=str(request.url))

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
