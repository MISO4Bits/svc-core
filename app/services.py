"""Casos de uso del submódulo Identidad y Consentimiento."""

from __future__ import annotations

from datetime import date

from app.domain import (
    Canal,
    Cliente,
    ClienteNoEncontrado,
    ClienteYaExiste,
    Consentimiento,
    ConsentimientoNoEncontrado,
    ConsentimientoScope,
    DomainEvent,
    EstadoConsentimiento,
    TipoDocumento,
    now_utc,
)
from app.ports import ClienteRepository, ConsentimientoRepository, EventPublisher


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
        segundo_nombre: str | None = None,
        segundo_apellido: str | None = None,
        telefono: str | None = None,
    ) -> Cliente:
        if await self._clientes.existe_por_documento(tipo_documento, numero_documento):
            raise ClienteYaExiste(tipo_documento, numero_documento)

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
        creado = await self._clientes.crear(cliente)
        await self._events.publish(
            DomainEvent(
                "ClienteRegistrado",
                {
                    "clienteId": creado.id,
                    "identityRef": creado.identity_ref,
                    "email": creado.email,
                },
            )
        )
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
        await self.obtener_cliente(cliente_id)
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
                {"clienteId": cliente_id, "scope": str(scope), "version": guardado.version},
            )
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

    async def estado_consentimiento(
        self, cliente_id: str, scope: ConsentimientoScope
    ) -> Consentimiento:
        return await self.obtener_consentimiento(cliente_id, scope)
