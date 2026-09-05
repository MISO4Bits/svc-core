from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status

from app.api.schemas import (
    ClienteOut,
    ConsentimientoOut,
    EstadoConsentimientoOut,
    OtorgarConsentimientoRequest,
    RegistrarClienteRequest,
)
from app.domain import ConsentimientoScope
from app.ports import IdempotencyStore
from app.services import IdentityService

router = APIRouter()


def get_service(request: Request) -> IdentityService:
    return request.app.state.service


def get_idempotency(request: Request) -> IdempotencyStore:
    return request.app.state.idempotency


ServiceDep = Annotated[IdentityService, Depends(get_service)]
IdempotencyDep = Annotated[IdempotencyStore, Depends(get_idempotency)]


@router.post(
    "/clientes",
    response_model=ClienteOut,
    status_code=status.HTTP_201_CREATED,
    tags=["Clientes"],
)
async def registrar_cliente(
    payload: RegistrarClienteRequest,
    service: ServiceDep,
    idempotency: IdempotencyDep,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ClienteOut:
    if idempotency_key:
        cached = await idempotency.get(idempotency_key)
        if cached is not None:
            response.status_code = cached["status"]
            response.headers["Location"] = f"/clientes/{cached['body']['id']}"
            return ClienteOut.model_validate(cached["body"])

    cliente = await service.registrar_cliente(
        identity_ref=payload.identity_ref,
        tipo_documento=payload.tipo_documento,
        numero_documento=payload.numero_documento,
        primer_nombre=payload.primer_nombre,
        primer_apellido=payload.primer_apellido,
        fecha_nacimiento=payload.fecha_nacimiento,
        email=payload.email,
        segundo_nombre=payload.segundo_nombre,
        segundo_apellido=payload.segundo_apellido,
        telefono=payload.telefono,
    )
    out = ClienteOut.model_validate(cliente)
    response.headers["Location"] = f"/clientes/{cliente.id}"

    if idempotency_key:
        await idempotency.put(
            idempotency_key,
            {
                "status": status.HTTP_201_CREATED,
                "body": out.model_dump(mode="json", by_alias=True),
            },
        )
    return out


@router.get("/clientes", response_model=ClienteOut, tags=["Clientes"])
async def buscar_cliente_por_identidad(
    service: ServiceDep,
    identity_ref: Annotated[str, Query(alias="identityRef", max_length=128)],
) -> ClienteOut:
    cliente = await service.buscar_por_identity_ref(identity_ref)
    return ClienteOut.model_validate(cliente)


@router.get("/clientes/{cliente_id}", response_model=ClienteOut, tags=["Clientes"])
async def obtener_cliente(cliente_id: str, service: ServiceDep) -> ClienteOut:
    cliente = await service.obtener_cliente(cliente_id)
    return ClienteOut.model_validate(cliente)


@router.get(
    "/clientes/{cliente_id}/consentimientos",
    response_model=list[ConsentimientoOut],
    tags=["Consentimientos"],
)
async def listar_consentimientos(cliente_id: str, service: ServiceDep) -> list[ConsentimientoOut]:
    items = await service.listar_consentimientos(cliente_id)
    return [ConsentimientoOut.model_validate(c) for c in items]


@router.post(
    "/clientes/{cliente_id}/consentimientos",
    response_model=ConsentimientoOut,
    status_code=status.HTTP_201_CREATED,
    tags=["Consentimientos"],
)
async def otorgar_consentimiento(
    cliente_id: str,
    payload: OtorgarConsentimientoRequest,
    service: ServiceDep,
) -> ConsentimientoOut:
    consentimiento = await service.otorgar_consentimiento(
        cliente_id,
        scope=payload.scope,
        politica_version=payload.politica_version,
        canal=payload.canal,
    )
    return ConsentimientoOut.model_validate(consentimiento)


@router.get(
    "/clientes/{cliente_id}/consentimientos/{scope}",
    response_model=ConsentimientoOut,
    tags=["Consentimientos"],
)
async def obtener_consentimiento(
    cliente_id: str, scope: ConsentimientoScope, service: ServiceDep
) -> ConsentimientoOut:
    consentimiento = await service.obtener_consentimiento(cliente_id, scope)
    return ConsentimientoOut.model_validate(consentimiento)


@router.delete(
    "/clientes/{cliente_id}/consentimientos/{scope}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["Consentimientos"],
)
async def revocar_consentimiento(
    cliente_id: str, scope: ConsentimientoScope, service: ServiceDep
) -> Response:
    await service.revocar_consentimiento(cliente_id, scope)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/clientes/{cliente_id}/consentimientos/{scope}/estado",
    response_model=EstadoConsentimientoOut,
    tags=["Consentimientos"],
)
async def estado_consentimiento(
    cliente_id: str, scope: ConsentimientoScope, service: ServiceDep
) -> EstadoConsentimientoOut:
    consentimiento = await service.estado_consentimiento(cliente_id, scope)
    return EstadoConsentimientoOut.model_validate(consentimiento)
