from __future__ import annotations

from datetime import date

import pytest

from app.adapters.memory import (
    InMemoryClienteRepository,
    InMemoryConsentimientoRepository,
    InMemoryEventPublisher,
)
from app.domain import (
    Canal,
    ClienteNoEncontrado,
    ClienteYaExiste,
    ConsentimientoNoEncontrado,
    ConsentimientoScope,
    EstadoConsentimiento,
    TipoDocumento,
)
from app.services import IdentityService

DATOS = dict(
    identity_ref="sub-1",
    tipo_documento=TipoDocumento.CC,
    numero_documento="123456",
    primer_nombre="Ana",
    primer_apellido="Ríos",
    fecha_nacimiento=date(1990, 1, 1),
    email="ana@example.com",
)


@pytest.fixture
def events() -> InMemoryEventPublisher:
    return InMemoryEventPublisher()


@pytest.fixture
def service(events: InMemoryEventPublisher) -> IdentityService:
    return IdentityService(
        InMemoryClienteRepository(), InMemoryConsentimientoRepository(), events
    )


async def test_registrar_cliente_publica_evento(service, events):
    cliente = await service.registrar_cliente(**DATOS)

    assert cliente.id
    assert cliente.estado.value == "ACTIVO"
    assert [e.tipo for e in events.events] == ["ClienteRegistrado"]
    assert events.events[0].datos["clienteId"] == cliente.id


async def test_registrar_cliente_duplicado(service):
    await service.registrar_cliente(**DATOS)
    with pytest.raises(ClienteYaExiste):
        await service.registrar_cliente(**DATOS)


async def test_obtener_cliente_inexistente(service):
    with pytest.raises(ClienteNoEncontrado):
        await service.obtener_cliente("no-existe")


async def test_buscar_por_identity_ref(service):
    cliente = await service.registrar_cliente(**DATOS)
    encontrado = await service.buscar_por_identity_ref("sub-1")
    assert encontrado.id == cliente.id
    with pytest.raises(ClienteNoEncontrado):
        await service.buscar_por_identity_ref("sub-desconocido")


async def test_listar_consentimientos_cliente_inexistente(service):
    with pytest.raises(ClienteNoEncontrado):
        await service.listar_consentimientos("no-existe")


async def test_otorgar_consentimiento_incrementa_version(service, events):
    cliente = await service.registrar_cliente(**DATOS)

    c1 = await service.otorgar_consentimiento(
        cliente.id,
        scope=ConsentimientoScope.OPEN_FINANCE,
        politica_version="v1",
        canal=Canal.WEB,
    )
    c2 = await service.otorgar_consentimiento(
        cliente.id,
        scope=ConsentimientoScope.OPEN_FINANCE,
        politica_version="v2",
        canal=Canal.WEB,
    )

    assert c1.version == 1
    assert c2.version == 2
    assert c2.vigente is True
    assert [e.tipo for e in events.events[-2:]] == [
        "ConsentimientoOtorgado",
        "ConsentimientoOtorgado",
    ]


async def test_otorgar_consentimiento_cliente_inexistente(service):
    with pytest.raises(ClienteNoEncontrado):
        await service.otorgar_consentimiento(
            "no-existe",
            scope=ConsentimientoScope.OPEN_DATA,
            politica_version="v1",
            canal=Canal.WEB,
        )


async def test_obtener_consentimiento_inexistente(service):
    cliente = await service.registrar_cliente(**DATOS)
    with pytest.raises(ConsentimientoNoEncontrado):
        await service.obtener_consentimiento(cliente.id, ConsentimientoScope.OPEN_DATA)


async def test_revocar_consentimiento(service, events):
    cliente = await service.registrar_cliente(**DATOS)
    await service.otorgar_consentimiento(
        cliente.id,
        scope=ConsentimientoScope.OPEN_DATA,
        politica_version="v1",
        canal=Canal.MOVIL,
    )

    await service.revocar_consentimiento(cliente.id, ConsentimientoScope.OPEN_DATA)

    consentimiento = await service.obtener_consentimiento(
        cliente.id, ConsentimientoScope.OPEN_DATA
    )
    assert consentimiento.estado is EstadoConsentimiento.REVOCADO
    assert consentimiento.vigente is False
    assert consentimiento.revocado_en is not None
    assert events.events[-1].tipo == "ConsentimientoRevocado"


async def test_revocar_consentimiento_inexistente(service):
    cliente = await service.registrar_cliente(**DATOS)
    with pytest.raises(ConsentimientoNoEncontrado):
        await service.revocar_consentimiento(cliente.id, ConsentimientoScope.OPEN_DATA)


async def test_estado_consentimiento_delega_en_obtener(service):
    cliente = await service.registrar_cliente(**DATOS)
    await service.otorgar_consentimiento(
        cliente.id,
        scope=ConsentimientoScope.OPEN_FINANCE,
        politica_version="v1",
        canal=Canal.WEB,
    )
    estado = await service.estado_consentimiento(
        cliente.id, ConsentimientoScope.OPEN_FINANCE
    )
    assert estado.vigente is True
