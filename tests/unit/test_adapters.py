from __future__ import annotations

import logging
from datetime import date

import aiosqlite
import pytest

from app.adapters.events import LoggingEventPublisher
from app.adapters.factory import (
    build_event_publisher,
    build_repositories,
    maybe_init,
)
from app.adapters.memory import (
    InMemoryClienteRepository,
    InMemoryConsentimientoRepository,
    InMemoryEventPublisher,
    InMemoryIdempotencyStore,
)
from app.adapters.sqlite import SqliteClienteRepository, SqliteDatabase
from app.config import Settings
from app.domain import (
    Canal,
    Cliente,
    ClienteYaExiste,
    Consentimiento,
    ConsentimientoScope,
    DomainEvent,
    EstadoConsentimiento,
    TipoDocumento,
)


def _cliente(**over) -> Cliente:
    base = dict(
        identity_ref="sub-1",
        tipo_documento=TipoDocumento.CC,
        numero_documento="123456",
        primer_nombre="Ana",
        primer_apellido="Ríos",
        fecha_nacimiento=date(1990, 1, 1),
        email="ana@example.com",
        telefono="+573001234567",
    )
    base.update(over)
    return Cliente(**base)


async def test_memory_cliente_repository_ciclo():
    consentimientos = InMemoryConsentimientoRepository()
    repo = InMemoryClienteRepository(consentimientos)
    cliente = _cliente()
    consentimiento = Consentimiento(
        cliente_id=cliente.id,
        scope=ConsentimientoScope.OPEN_FINANCE,
        estado=EstadoConsentimiento.OTORGADO,
    )

    await repo.crear_con_consentimientos(cliente, [consentimiento])

    assert await repo.obtener(cliente.id) == cliente
    assert await repo.obtener("otro") is None
    assert await repo.obtener_por_identity_ref("sub-1") == cliente
    assert await repo.obtener_por_identity_ref("sub-x") is None
    assert await repo.existe_por_documento("CC", "123456") is True
    assert await repo.existe_por_documento("CC", "999") is False
    assert await repo.existe_por_correo("ana@example.com") is True
    assert await repo.existe_por_correo("otro@example.com") is False
    assert await consentimientos.obtener(cliente.id, ConsentimientoScope.OPEN_FINANCE) is not None


async def test_memory_cliente_repository_confirmar_es_idempotente():
    repo = InMemoryClienteRepository()
    cliente = _cliente()
    await repo.crear_con_consentimientos(cliente, [])

    confirmado = await repo.confirmar(cliente.id)
    assert confirmado.correo_confirmado is True
    primera_vez = confirmado.confirmado_en

    confirmado_otra_vez = await repo.confirmar(cliente.id)
    assert confirmado_otra_vez.confirmado_en == primera_vez
    assert await repo.confirmar("no-existe") is None


async def test_memory_consentimiento_repository_listar_ordenado():
    repo = InMemoryConsentimientoRepository()
    for scope in (ConsentimientoScope.OPEN_FINANCE, ConsentimientoScope.OPEN_DATA):
        await repo.guardar(
            Consentimiento(cliente_id="c1", scope=scope, estado=EstadoConsentimiento.OTORGADO)
        )
    await repo.guardar(
        Consentimiento(
            cliente_id="c2",
            scope=ConsentimientoScope.OPEN_DATA,
            estado=EstadoConsentimiento.OTORGADO,
        )
    )

    listado = await repo.listar("c1")
    assert [str(c.scope) for c in listado] == ["OPEN_DATA", "OPEN_FINANCE"]
    assert await repo.obtener("c1", ConsentimientoScope.OPEN_DATA) is not None
    assert await repo.obtener("c1", "OTRO") is None


async def test_memory_idempotency_store():
    store = InMemoryIdempotencyStore()
    assert await store.get("k") is None
    await store.put("k", {"status": 201})
    assert await store.get("k") == {"status": 201}


async def test_memory_event_publisher_records():
    publisher = InMemoryEventPublisher()
    await publisher.publish(DomainEvent("X", {"a": 1}))
    assert publisher.events[0].tipo == "X"


async def test_logging_event_publisher(caplog):
    publisher = LoggingEventPublisher()
    with caplog.at_level(logging.INFO, logger="svc_core.eventos"):
        await publisher.publish(DomainEvent("ClienteRegistrado", {"clienteId": "c1"}))
    assert "ClienteRegistrado" in caplog.text


async def test_sqlite_cliente_repository_conflicto(tmp_path):
    db = SqliteDatabase(str(tmp_path / "c.db"))
    await db.init()
    repo = SqliteClienteRepository(db)

    creado = await repo.crear_con_consentimientos(_cliente(), [])
    with pytest.raises(ClienteYaExiste):
        await repo.crear_con_consentimientos(_cliente(identity_ref="sub-2"), [])
    assert (await repo.obtener_por_identity_ref("sub-1")).id == creado.id
    assert await repo.obtener_por_identity_ref("ausente") is None
    assert await repo.existe_por_correo("ana@example.com") is True
    assert await repo.existe_por_correo("otro@example.com") is False


async def test_sqlite_exige_el_celular_a_nivel_de_base_de_datos(tmp_path):
    db = SqliteDatabase(str(tmp_path / "c.db"))
    await db.init()
    async with db.connect() as conn:
        with pytest.raises(aiosqlite.IntegrityError, match="telefono"):
            await conn.execute(
                """
                INSERT INTO clientes (
                    id, identity_ref, tipo_documento, numero_documento, primer_nombre,
                    primer_apellido, fecha_nacimiento, email, telefono, estado, creado_en
                ) VALUES ('1', 's', 'CC', '123456', 'Ana', 'Ríos', '1990-01-01',
                          'ana@example.com', NULL, 'ACTIVO', '2026-01-01')
                """
            )


async def test_sqlite_cliente_repository_confirmar_es_idempotente(tmp_path):
    db = SqliteDatabase(str(tmp_path / "c.db"))
    await db.init()
    repo = SqliteClienteRepository(db)
    creado = await repo.crear_con_consentimientos(_cliente(), [])
    assert creado.correo_confirmado is False

    confirmado = await repo.confirmar(creado.id)
    assert confirmado.correo_confirmado is True
    primera_vez = confirmado.confirmado_en

    confirmado_otra_vez = await repo.confirmar(creado.id)
    assert confirmado_otra_vez.confirmado_en == primera_vez
    assert await repo.confirmar("no-existe") is None


async def test_sqlite_crear_con_consentimiento_persiste_consentimiento(tmp_path):
    from app.adapters.sqlite import SqliteConsentimientoRepository

    db = SqliteDatabase(str(tmp_path / "c.db"))
    await db.init()
    repo = SqliteClienteRepository(db)
    consentimiento = Consentimiento(
        cliente_id="",
        scope=ConsentimientoScope.OPEN_FINANCE,
        estado=EstadoConsentimiento.OTORGADO,
    )
    cliente = _cliente()
    consentimiento.cliente_id = cliente.id

    await repo.crear_con_consentimientos(cliente, [consentimiento])

    guardado = await SqliteConsentimientoRepository(db).obtener(
        cliente.id, ConsentimientoScope.OPEN_FINANCE
    )
    assert guardado is not None
    assert guardado.estado is EstadoConsentimiento.OTORGADO


async def test_sqlite_consentimiento_upsert(tmp_path):
    from app.adapters.sqlite import SqliteConsentimientoRepository

    db = SqliteDatabase(str(tmp_path / "c.db"))
    await db.init()
    repo = SqliteConsentimientoRepository(db)

    await repo.guardar(
        Consentimiento(
            cliente_id="c1",
            scope=ConsentimientoScope.OPEN_FINANCE,
            estado=EstadoConsentimiento.OTORGADO,
            version=1,
            canal=Canal.WEB,
        )
    )
    await repo.guardar(
        Consentimiento(
            cliente_id="c1",
            scope=ConsentimientoScope.OPEN_FINANCE,
            estado=EstadoConsentimiento.REVOCADO,
            version=2,
        )
    )

    actual = await repo.obtener("c1", ConsentimientoScope.OPEN_FINANCE)
    assert actual.version == 2
    assert actual.estado is EstadoConsentimiento.REVOCADO
    assert await repo.listar("c1") != []


async def test_sqlite_idempotency_store_roundtrip(tmp_path):
    from app.adapters.sqlite import SqliteIdempotencyStore

    db = SqliteDatabase(str(tmp_path / "c.db"))
    await db.init()
    store = SqliteIdempotencyStore(db)

    assert await store.get("k") is None
    await store.put("k", {"status": 201, "body": {"id": "x"}})
    assert (await store.get("k"))["body"]["id"] == "x"


def test_build_repositories_memory():
    repos = build_repositories(Settings(repository_backend="memory"))
    assert isinstance(repos[0], InMemoryClienteRepository)


def test_build_repositories_sqlite(tmp_path):
    repos = build_repositories(
        Settings(repository_backend="sqlite", database_path=str(tmp_path / "x.db"))
    )
    assert isinstance(repos[0], SqliteClienteRepository)


def test_build_repositories_invalido():
    with pytest.raises(ValueError):
        build_repositories(Settings(repository_backend="cassandra"))


def test_build_event_publisher_variantes():
    assert isinstance(
        build_event_publisher(Settings(event_backend="memory")), InMemoryEventPublisher
    )
    assert isinstance(
        build_event_publisher(Settings(event_backend="logging")), LoggingEventPublisher
    )
    assert isinstance(build_event_publisher(Settings(event_backend="otro")), LoggingEventPublisher)


async def test_maybe_init_ignora_no_sqlite():
    await maybe_init(InMemoryClienteRepository())  # no lanza
