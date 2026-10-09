"""Casos de uso del submódulo Identidad y Consentimiento."""

from __future__ import annotations

import logging
from datetime import date

from app.domain import (
    Canal,
    Cliente,
    ClienteNoEncontrado,
    ClienteYaExiste,
    Consentimiento,
    ConsentimientoNoEncontrado,
    ConsentimientoScope,
    CorreoDesechable,
    DomainEvent,
    EstadoConsentimiento,
    TipoDocumento,
    now_utc,
)
from app.dominios_desechables import es_desechable
from app.logging_utils import sanear_para_log
from app.ports import ClienteRepository, ConsentimientoRepository, EventPublisher

logger = logging.getLogger("svc_core.identity")


class IdentityService:
    def __init__(
        self,
        clientes: ClienteRepository,
        consentimientos: ConsentimientoRepository,
        events: EventPublisher,
    ) -> None:
        self._clientes = clientes
        self._consentimientos = consentimientos
        self._events = events

    async def registrar_cliente(
        self,
        *,
        identity_ref: str,
        tipo_documento: TipoDocumento,
        numero_documento: str,
        primer_nombre: str,
        primer_apellido: str,
        fecha_nacimiento: date,
        email: str,
        canal: Canal,
        autoriza_tratamiento_datos: bool,
        autoriza_datos_financieros: bool,
        telefono: str,
        segundo_nombre: str | None = None,
        segundo_apellido: str | None = None,
        politica_version_tratamiento_datos: str | None = None,
        politica_version_datos_financieros: str | None = None,
    ) -> Cliente:
        if es_desechable(email):
            logger.info("registrar_cliente: dominio de correo desechable")
            raise CorreoDesechable(email)

        logger.info("registrar_cliente: verificando correo existente")
        if await self._clientes.existe_por_correo(email):
            logger.info("registrar_cliente: correo ya registrado")
            raise ClienteYaExiste(tipo_documento, numero_documento, campo="correo")

        logger.info("registrar_cliente: verificando documento existente")
        if await self._clientes.existe_por_documento(tipo_documento, numero_documento):
            logger.info("registrar_cliente: documento ya registrado")
            raise ClienteYaExiste(tipo_documento, numero_documento, campo="documento")

        cliente = Cliente(
            identity_ref=identity_ref,
            tipo_documento=TipoDocumento(tipo_documento),
            numero_documento=numero_documento,
            primer_nombre=primer_nombre,
            primer_apellido=primer_apellido,
            fecha_nacimiento=fecha_nacimiento,
            email=email,
            segundo_nombre=segundo_nombre,
            segundo_apellido=segundo_apellido,
            telefono=telefono,
        )
        consentimientos = [
            Consentimiento(
                cliente_id=cliente.id,
                scope=ConsentimientoScope.OPEN_DATA,
                estado=(
                    EstadoConsentimiento.OTORGADO
                    if autoriza_tratamiento_datos
                    else EstadoConsentimiento.NO_OTORGADO
                ),
                politica_version=(
                    politica_version_tratamiento_datos if autoriza_tratamiento_datos else None
                ),
                canal=canal,
                otorgado_en=now_utc() if autoriza_tratamiento_datos else None,
            ),
            Consentimiento(
                cliente_id=cliente.id,
                scope=ConsentimientoScope.OPEN_FINANCE,
                estado=(
                    EstadoConsentimiento.OTORGADO
                    if autoriza_datos_financieros
                    else EstadoConsentimiento.NO_OTORGADO
                ),
                politica_version=(
                    politica_version_datos_financieros if autoriza_datos_financieros else None
                ),
                canal=canal,
                otorgado_en=now_utc() if autoriza_datos_financieros else None,
            ),
        ]
        creado = await self._clientes.crear_con_consentimientos(cliente, consentimientos)
        await self._events.publish(
            DomainEvent(
                "ClienteRegistrado",
                {
                    "clienteId": creado.id,
                    "identityRef": creado.identity_ref,
                    "email": creado.email,
                    "tipoDocumento": str(creado.tipo_documento),
                    "numeroDocumento": creado.numero_documento,
                    # Perfilamiento decide con estos flags si consulta Open Data /
                    # Open Finance (BITS-93 AC-8/AC-9) — no encadenado con la
                    # cotización, se dispara aquí de forma asíncrona.
                    "autorizaTratamientoDatos": autoriza_tratamiento_datos,
                    "autorizaDatosFinancieros": autoriza_datos_financieros,
                },
            )
        )
        logger.info("registrar_cliente: cliente creado cliente_id=%s", creado.id)
        return creado

    async def obtener_cliente(self, cliente_id: str) -> Cliente:
        cliente = await self._clientes.obtener(cliente_id)
        if cliente is None:
            raise ClienteNoEncontrado(cliente_id)
        return cliente

    async def buscar_por_identity_ref(self, identity_ref: str) -> Cliente:
        cliente = await self._clientes.obtener_por_identity_ref(identity_ref)
        if cliente is None:
            raise ClienteNoEncontrado(identity_ref)
        return cliente

    async def existe_cliente(
        self,
        *,
        email: str | None = None,
        tipo_documento: TipoDocumento | None = None,
        numero_documento: str | None = None,
    ) -> dict[str, bool | None]:
        correo_disponible = None
        if email is not None:
            correo_disponible = not await self._clientes.existe_por_correo(email)
        documento_disponible = None
        if tipo_documento is not None and numero_documento is not None:
            documento_disponible = not await self._clientes.existe_por_documento(
                tipo_documento, numero_documento
            )
        return {"correoDisponible": correo_disponible, "documentoDisponible": documento_disponible}

    async def confirmar_cliente(self, cliente_id: str) -> Cliente:
        await self.obtener_cliente(cliente_id)
        confirmado = await self._clientes.confirmar(cliente_id)
        assert confirmado is not None  # ya validamos que existe arriba
        logger.info("confirmar_cliente: cliente_id=%s", sanear_para_log(cliente_id))
        return confirmado

    async def listar_consentimientos(self, cliente_id: str) -> list[Consentimiento]:
        await self.obtener_cliente(cliente_id)
        return await self._consentimientos.listar(cliente_id)

    async def obtener_consentimiento(
        self, cliente_id: str, scope: ConsentimientoScope
    ) -> Consentimiento:
        await self.obtener_cliente(cliente_id)
        consentimiento = await self._consentimientos.obtener(cliente_id, scope)
        if consentimiento is None:
            raise ConsentimientoNoEncontrado(cliente_id, scope)
        return consentimiento

    async def otorgar_consentimiento(
        self,
        cliente_id: str,
        *,
        scope: ConsentimientoScope,
        politica_version: str,
        canal: Canal,
    ) -> Consentimiento:
        cliente = await self.obtener_cliente(cliente_id)
        logger.info(
            "otorgar_consentimiento: cliente_id=%s scope=%s", sanear_para_log(cliente_id), scope
        )
        actual = await self._consentimientos.obtener(cliente_id, scope)
        version = actual.version + 1 if actual is not None else 1
        consentimiento = Consentimiento(
            cliente_id=cliente_id,
            scope=ConsentimientoScope(scope),
            estado=EstadoConsentimiento.OTORGADO,
            version=version,
            politica_version=politica_version,
            canal=Canal(canal),
            otorgado_en=now_utc(),
            actualizado_en=now_utc(),
        )
        guardado = await self._consentimientos.guardar(consentimiento)
        await self._events.publish(
            DomainEvent(
                "ConsentimientoOtorgado",
                {
                    "clienteId": cliente_id,
                    "scope": str(scope),
                    "version": guardado.version,
                    # Perfilamiento consume este evento de forma asíncrona y
                    # necesita el documento para consultar Open Finance/Open
                    # Data — viaja en el propio evento para que el consumidor
                    # no tenga que llamar de vuelta a CoreTransaccional
                    # (Confluence, página Perfilamiento, Sección 8, decisión
                    # 2026-09-18: sin puerto síncrono nuevo hacia Core).
                    "tipoDocumento": str(cliente.tipo_documento),
                    "numeroDocumento": cliente.numero_documento,
                },
            )
        )
        logger.info(
            "otorgar_consentimiento: consentimiento otorgado cliente_id=%s scope=%s version=%s",
            sanear_para_log(cliente_id),
            scope,
            guardado.version,
        )
        return guardado

    async def revocar_consentimiento(self, cliente_id: str, scope: ConsentimientoScope) -> None:
        consentimiento = await self.obtener_consentimiento(cliente_id, scope)
        consentimiento.estado = EstadoConsentimiento.REVOCADO
        consentimiento.revocado_en = now_utc()
        consentimiento.actualizado_en = now_utc()
        consentimiento.version += 1
        await self._consentimientos.guardar(consentimiento)
        await self._events.publish(
            DomainEvent(
                "ConsentimientoRevocado",
                {
                    "clienteId": cliente_id,
                    "scope": str(scope),
                    "version": consentimiento.version,
                },
            )
        )
        logger.info(
            "revocar_consentimiento: consentimiento revocado cliente_id=%s scope=%s version=%s",
            sanear_para_log(cliente_id),
            scope,
            consentimiento.version,
        )

    async def estado_consentimiento(
        self, cliente_id: str, scope: ConsentimientoScope
    ) -> Consentimiento:
        return await self.obtener_consentimiento(cliente_id, scope)
