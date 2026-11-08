"""Venta directa del socio (BITS-378/379). Se monta bajo ``/v1``.

Interno y sin gateway: solo lo llama bff-web, desde las rutas que exigen el rol ``SOCIO``. El
``socioId`` lo pone el BFF con el ``uid`` del token; Core no autentica, confía en quien lo llama.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Header, Request, Response, status
from opentelemetry import context as otel_context

from app.api.polizas import PolizasDep, _a_poliza_interna_out
from app.api.routes import IdempotencyDep
from app.api.schemas import (
    ClienteOut,
    CoberturaContratadaOut,
    CrearClienteAsistidoRequest,
    DatosCreditoOut,
    PolizaInternaOut,
    PrecioPolizaOut,
    PrecioPolizaRequest,
    VenderPolizaRequest,
    VigenciaOut,
)
from app.domain import DatosCredito
from app.logging_utils import sanear_para_log
from app.notificaciones import ejecutar_con_contexto

logger = logging.getLogger("svc_core.api")
router = APIRouter()


def _credito(payload: PrecioPolizaRequest | VenderPolizaRequest) -> DatosCredito:
    """El crédito es nuevo: el saldo es el valor. ``str`` evita los decimales binarios del float."""
    valor = Decimal(str(payload.valor_credito))
    return DatosCredito(
        valor_credito=valor,
        plazo_restante_meses=payload.plazo_restante_meses,
        entidad_acreedora=payload.entidad_acreedora.strip(),
        saldo_insoluto=valor,
    )


@router.post(
    "/clientes/asistidos",
    response_model=ClienteOut,
    status_code=status.HTTP_201_CREATED,
    tags=["Venta directa"],
)
async def crear_cliente_asistido(
    payload: CrearClienteAsistidoRequest,
    request: Request,
    background: BackgroundTasks,
    idempotency: IdempotencyDep,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ClienteOut:
    # Ni el correo ni el documento se escriben en el log.
    logger.info("POST /v1/clientes/asistidos: solicitud recibida")
    clave = f"cliente-asistido:{idempotency_key}" if idempotency_key else None
    if clave:
        guardada = await idempotency.get(clave)
        if guardada is not None:
            response.status_code = guardada["status"]
            response.headers["Location"] = f"/v1/clientes/{guardada['body']['id']}"
            return ClienteOut.model_validate(guardada["body"])

    cliente, contrasena = await request.app.state.clientes_asistidos.crear(
        tipo_documento=payload.tipo_documento,
        numero_documento=payload.numero_documento,
        primer_nombre=payload.primer_nombre,
        segundo_nombre=payload.segundo_nombre,
        primer_apellido=payload.primer_apellido,
        segundo_apellido=payload.segundo_apellido,
        fecha_nacimiento=payload.fecha_nacimiento,
        email=payload.email,
        telefono=payload.telefono,
    )
    out = ClienteOut.model_validate(cliente)
    response.headers["Location"] = f"/v1/clientes/{cliente.id}"

    # La bienvenida y los datos de acceso salen después de responder y sin afectar el alta. La
    # contraseña solo viaja a la tarea: no se devuelve, no se guarda con la clave de idempotencia
    # ni se registra.
    notificaciones = request.app.state.notificaciones
    if notificaciones is not None:
        background.add_task(
            ejecutar_con_contexto,
            otel_context.get_current(),
            notificaciones.enviar_correos_de_alta,
            cliente,
            contrasena,
        )
    if clave:
        await idempotency.put(
            clave,
            {
                "status": status.HTTP_201_CREATED,
                "body": out.model_dump(mode="json", by_alias=True),
            },
        )
    return out


@router.post("/polizas/precio", response_model=PrecioPolizaOut, tags=["Venta directa"])
async def calcular_precio(payload: PrecioPolizaRequest, service: PolizasDep) -> PrecioPolizaOut:
    logger.info(
        "POST /v1/polizas/precio: solicitud recibida cliente_id=%s",
        sanear_para_log(payload.cliente_id),
    )
    cotizacion = await service.precio(payload.cliente_id, _credito(payload))
    d = cotizacion.datos_credito
    return PrecioPolizaOut(
        edad=cotizacion.edad,
        datos_credito=DatosCreditoOut(
            valor_credito=float(d.valor_credito),
            plazo_restante_meses=d.plazo_restante_meses,
            entidad_acreedora=d.entidad_acreedora,
            saldo_insoluto=float(d.saldo_insoluto),
        ),
        origen_datos=cotizacion.origen_datos,
        tasa=float(cotizacion.tasa),
        version_tarifa=cotizacion.version_tarifa,
        cobertura_meses=cotizacion.cobertura_meses,
        coberturas=[
            CoberturaContratadaOut(
                codigo=c.codigo,
                obligatoria=c.obligatoria,
                suma_asegurada=float(c.suma_asegurada),
                prima_neta=float(c.prima_neta),
                iva=float(c.iva),
                prima_total=float(c.prima_total),
            )
            for c in cotizacion.coberturas
        ],
        vigencia_cotizacion=VigenciaOut(
            desde=cotizacion.vigencia.desde, hasta=cotizacion.vigencia.hasta
        ),
        creada_en=cotizacion.creada_en,
    )


@router.post(
    "/polizas",
    response_model=PolizaInternaOut,
    status_code=status.HTTP_201_CREATED,
    tags=["Venta directa"],
)
async def vender_poliza(
    payload: VenderPolizaRequest,
    service: PolizasDep,
    idempotency: IdempotencyDep,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> PolizaInternaOut:
    logger.info(
        "POST /v1/polizas: solicitud recibida cliente_id=%s socio_id=%s",
        sanear_para_log(payload.cliente_id),
        sanear_para_log(payload.socio_id),
    )
    # La clave lleva al socio: la misma clave de dos socios no puede devolver la venta del otro.
    clave = f"venta:{payload.socio_id}:{idempotency_key}" if idempotency_key else None
    if clave:
        guardada = await idempotency.get(clave)
        if guardada is not None:
            response.status_code = guardada["status"]
            return PolizaInternaOut.model_validate(guardada["body"])

    poliza = await service.vender(
        socio_id=payload.socio_id,
        cliente_id=payload.cliente_id,
        coberturas=payload.coberturas,
        datos=_credito(payload),
        cuota_mensual=Decimal(str(payload.cuota_mensual)),
        direccion=payload.direccion,
        municipio=payload.municipio,
    )
    out = await _a_poliza_interna_out(service, poliza)
    response.headers["Location"] = f"/v1/polizas/{poliza.id}"

    # Sin correo propio: la póliza le llega al cliente por el flujo de siempre (PolizaEmitida →
    # documento → correo con el PDF) y por el aviso push. La bienvenida es del alta.
    if clave:
        await idempotency.put(
            clave,
            {
                "status": status.HTTP_201_CREATED,
                "body": out.model_dump(mode="json", by_alias=True),
            },
        )
    return out
