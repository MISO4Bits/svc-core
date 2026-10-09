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
    Cliente,
    ClienteNoEncontrado,
    ClienteYaExiste,
    ConsentimientoNoEncontrado,
    ConsentimientoScope,
    CorreoDesechable,
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
    telefono="+573001234567",
    canal=Canal.WEB,
    autoriza_tratamiento_datos=True,
    autoriza_datos_financieros=True,
)


@pytest.fixture
def events() -> InMemoryEventPublisher:
    return InMemoryEventPublisher()


@pytest.fixture
def repos():
    consentimientos = InMemoryConsentimientoRepository()
    clientes = InMemoryClienteRepository(consentimientos)
    return clientes, consentimientos


@pytest.fixture
def service(repos, events: InMemoryEventPublisher) -> IdentityService:
    clientes, consentimientos = repos
    return IdentityService(clientes, consentimientos, events)


async def test_registrar_cliente_publica_evento(service, events):
    cliente = await service.registrar_cliente(**DATOS)

    assert cliente.id
    assert cliente.estado.value == "ACTIVO"
    assert [e.tipo for e in events.events] == ["ClienteRegistrado"]
    assert events.events[0].datos["clienteId"] == cliente.id


async def test_registrar_cliente_duplicado(service):
    await service.registrar_cliente(**DATOS)
    with pytest.raises(ClienteYaExiste) as exc_info:
        await service.registrar_cliente(**{**DATOS, "email": "otro@example.com"})
    assert exc_info.value.campo == "documento"


async def test_registrar_cliente_correo_duplicado(service):
    await service.registrar_cliente(**DATOS)
    with pytest.raises(ClienteYaExiste) as exc_info:
        await service.registrar_cliente(**{**DATOS, "numero_documento": "999999"})
    assert exc_info.value.campo == "correo"


async def test_registrar_cliente_correo_desechable(service):
    with pytest.raises(CorreoDesechable):
        await service.registrar_cliente(**{**DATOS, "email": "ana@mailinator.com"})


async def test_registrar_cliente_declina_financiero_no_bloquea(service, events):
    cliente = await service.registrar_cliente(**{**DATOS, "autoriza_datos_financieros": False})

    assert cliente.id
    consentimiento = await service.obtener_consentimiento(
        cliente.id, ConsentimientoScope.OPEN_FINANCE
    )
    assert consentimiento.estado is EstadoConsentimiento.NO_OTORGADO
    assert events.events[-1].datos["autorizaDatosFinancieros"] is False


async def test_registrar_cliente_declina_tratamiento_datos_no_bloquea(service, events):
    cliente = await service.registrar_cliente(**{**DATOS, "autoriza_tratamiento_datos": False})

    assert cliente.id
    consentimiento = await service.obtener_consentimiento(cliente.id, ConsentimientoScope.OPEN_DATA)
    assert consentimiento.estado is EstadoConsentimiento.NO_OTORGADO
    assert consentimiento.politica_version is None
    assert events.events[-1].datos["autorizaTratamientoDatos"] is False


async def test_existe_cliente(service):
    await service.registrar_cliente(**DATOS)

    disponible = await service.existe_cliente(email="libre@example.com")
    assert disponible == {"correoDisponible": True, "documentoDisponible": None}

    ocupado = await service.existe_cliente(
        email=DATOS["email"],
        tipo_documento=DATOS["tipo_documento"],
        numero_documento=DATOS["numero_documento"],
    )
    assert ocupado == {"correoDisponible": False, "documentoDisponible": False}

    sin_consulta = await service.existe_cliente()
    assert sin_consulta == {"correoDisponible": None, "documentoDisponible": None}


async def test_confirmar_cliente_es_idempotente(service):
    cliente = await service.registrar_cliente(**DATOS)
    assert cliente.correo_confirmado is False

    confirmado = await service.confirmar_cliente(cliente.id)
    assert confirmado.correo_confirmado is True
    primera_confirmacion = confirmado.confirmado_en

    confirmado_otra_vez = await service.confirmar_cliente(cliente.id)
    assert confirmado_otra_vez.confirmado_en == primera_confirmacion


async def test_confirmar_cliente_inexistente(service):
    with pytest.raises(ClienteNoEncontrado):
        await service.confirmar_cliente("no-existe")


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
    # El registro unificado (BITS-93) ya crea OPEN_DATA en version 1 cuando
    # autoriza_tratamiento_datos=True, así que un otorgamiento posterior parte
    # de ahí, no de version 1.
    cliente = await service.registrar_cliente(**DATOS)

    c1 = await service.otorgar_consentimiento(
        cliente.id,
        scope=ConsentimientoScope.OPEN_DATA,
        politica_version="v1",
        canal=Canal.WEB,
    )
    c2 = await service.otorgar_consentimiento(
        cliente.id,
        scope=ConsentimientoScope.OPEN_DATA,
        politica_version="v2",
        canal=Canal.WEB,
    )

    assert c1.version == 2
    assert c2.version == 3
    assert c2.vigente is True
    assert [e.tipo for e in events.events[-2:]] == [
        "ConsentimientoOtorgado",
        "ConsentimientoOtorgado",
    ]
    # Perfilamiento consume este evento de forma asíncrona y necesita el
    # documento del cliente para consultar Open Finance/Open Data sin
    # llamar de vuelta a CoreTransaccional.
    assert events.events[-1].datos["tipoDocumento"] == "CC"
    assert events.events[-1].datos["numeroDocumento"] == "123456"


async def test_otorgar_consentimiento_cliente_inexistente(service):
    with pytest.raises(ClienteNoEncontrado):
        await service.otorgar_consentimiento(
            "no-existe",
            scope=ConsentimientoScope.OPEN_DATA,
            politica_version="v1",
            canal=Canal.WEB,
        )


async def test_obtener_consentimiento_inexistente(repos):
    # El registro unificado (BITS-93) ya crea un registro por scope (OTORGADO o
    # NO_OTORGADO), así que para probar "no encontrado" hay que insertar un
    # cliente sin pasar por el registro (sin consentimientos asociados).
    clientes, _ = repos
    cliente = await clientes.crear_con_consentimientos(
        Cliente(
            identity_ref="sub-x",
            tipo_documento=TipoDocumento.CC,
            numero_documento="000000",
            primer_nombre="Sin",
            primer_apellido="Consentimientos",
            fecha_nacimiento=date(1990, 1, 1),
            email="sin-consentimientos@example.com",
        ),
        [],
    )
    consentimientos = InMemoryConsentimientoRepository()
    service = IdentityService(clientes, consentimientos, InMemoryEventPublisher())
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

    consentimiento = await service.obtener_consentimiento(cliente.id, ConsentimientoScope.OPEN_DATA)
    assert consentimiento.estado is EstadoConsentimiento.REVOCADO
    assert consentimiento.vigente is False
    assert consentimiento.revocado_en is not None
    assert events.events[-1].tipo == "ConsentimientoRevocado"


async def test_revocar_consentimiento_inexistente(repos):
    clientes, _ = repos
    cliente = await clientes.crear_con_consentimientos(
        Cliente(
            identity_ref="sub-y",
            tipo_documento=TipoDocumento.CC,
            numero_documento="000001",
            primer_nombre="Sin",
            primer_apellido="Consentimientos",
            fecha_nacimiento=date(1990, 1, 1),
            email="sin-consentimientos-2@example.com",
        ),
        [],
    )
    service = IdentityService(
        clientes, InMemoryConsentimientoRepository(), InMemoryEventPublisher()
    )
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
    estado = await service.estado_consentimiento(cliente.id, ConsentimientoScope.OPEN_FINANCE)
    assert estado.vigente is True
