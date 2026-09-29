"""Adaptador de persistencia sobre SQLite (aiosqlite).

Es el backend de desarrollo y del experimento. En producción se implementa el
mismo puerto con Cloud Spanner; el resto del servicio no cambia.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime

import aiosqlite

from app.domain import (
    Canal,
    Cliente,
    ClienteYaExiste,
    Consentimiento,
    ConsentimientoScope,
    EstadoCliente,
    EstadoConsentimiento,
    TipoDocumento,
    now_utc,
)
from app.logging_utils import sanear_para_log

logger = logging.getLogger("svc_core.adapters.sqlite")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clientes (
    id TEXT PRIMARY KEY,
    identity_ref TEXT NOT NULL,
    tipo_documento TEXT NOT NULL,
    numero_documento TEXT NOT NULL,
    primer_nombre TEXT NOT NULL,
    segundo_nombre TEXT,
    primer_apellido TEXT NOT NULL,
    segundo_apellido TEXT,
    fecha_nacimiento TEXT NOT NULL,
    email TEXT NOT NULL,
    telefono TEXT,
    estado TEXT NOT NULL,
    correo_confirmado INTEGER NOT NULL DEFAULT 0,
    confirmado_en TEXT,
    creado_en TEXT NOT NULL,
    actualizado_en TEXT,
    UNIQUE (tipo_documento, numero_documento),
    UNIQUE (email)
);
CREATE TABLE IF NOT EXISTS consentimientos (
    cliente_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    estado TEXT NOT NULL,
    version INTEGER NOT NULL,
    politica_version TEXT,
    canal TEXT,
    otorgado_en TEXT,
    revocado_en TEXT,
    actualizado_en TEXT NOT NULL,
    PRIMARY KEY (cliente_id, scope)
);
CREATE TABLE IF NOT EXISTS idempotencia (
    clave TEXT PRIMARY KEY,
    valor TEXT NOT NULL,
    creado_en TEXT NOT NULL
);
"""


def _iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class SqliteDatabase:
    def __init__(self, path: str) -> None:
        self.path = path

    async def init(self) -> None:
        async with self.connect() as conn:
            await conn.executescript(_SCHEMA)
            # Migración manual para bases de dev creadas antes de BITS-93: SQLite
            # no soporta "ADD COLUMN IF NOT EXISTS", así que se intenta y se
            # ignora el error si la columna ya existe. No agrega el UNIQUE(email)
            # a tablas viejas — eso solo aplica a bases nuevas (ver openapi.yaml,
            # Cliente, nota de limitación conocida del backend de dev).
            for ddl in (
                "ALTER TABLE clientes ADD COLUMN correo_confirmado INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE clientes ADD COLUMN confirmado_en TEXT",
            ):
                try:
                    await conn.execute(ddl)
                except aiosqlite.OperationalError:
                    pass
            await conn.commit()

    @asynccontextmanager
    async def connect(self):
        conn = await aiosqlite.connect(self.path)
        conn.row_factory = aiosqlite.Row
        try:
            yield conn
        finally:
            await conn.close()


def _row_to_cliente(row: aiosqlite.Row) -> Cliente:
    return Cliente(
        id=row["id"],
        identity_ref=row["identity_ref"],
        tipo_documento=TipoDocumento(row["tipo_documento"]),
        numero_documento=row["numero_documento"],
        primer_nombre=row["primer_nombre"],
        segundo_nombre=row["segundo_nombre"],
        primer_apellido=row["primer_apellido"],
        segundo_apellido=row["segundo_apellido"],
        fecha_nacimiento=date.fromisoformat(row["fecha_nacimiento"]),
        email=row["email"],
        telefono=row["telefono"],
        estado=EstadoCliente(row["estado"]),
        correo_confirmado=bool(row["correo_confirmado"]),
        confirmado_en=_dt(row["confirmado_en"]),
        creado_en=_dt(row["creado_en"]),
        actualizado_en=_dt(row["actualizado_en"]),
    )


def _row_to_consentimiento(row: aiosqlite.Row) -> Consentimiento:
    return Consentimiento(
        cliente_id=row["cliente_id"],
        scope=ConsentimientoScope(row["scope"]),
        estado=EstadoConsentimiento(row["estado"]),
        version=row["version"],
        politica_version=row["politica_version"],
        canal=Canal(row["canal"]) if row["canal"] else None,
        otorgado_en=_dt(row["otorgado_en"]),
        revocado_en=_dt(row["revocado_en"]),
        actualizado_en=_dt(row["actualizado_en"]),
    )


class SqliteClienteRepository:
    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    async def crear_con_consentimientos(
        self, cliente: Cliente, consentimientos: list[Consentimiento]
    ) -> Cliente:
        async with self._db.connect() as conn:
            try:
                await conn.execute(
                    """
                    INSERT INTO clientes (
                        id, identity_ref, tipo_documento, numero_documento,
                        primer_nombre, segundo_nombre, primer_apellido, segundo_apellido,
                        fecha_nacimiento, email, telefono, estado, correo_confirmado,
                        confirmado_en, creado_en, actualizado_en
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cliente.id,
                        cliente.identity_ref,
                        str(cliente.tipo_documento),
                        cliente.numero_documento,
                        cliente.primer_nombre,
                        cliente.segundo_nombre,
                        cliente.primer_apellido,
                        cliente.segundo_apellido,
                        _iso(cliente.fecha_nacimiento),
                        cliente.email,
                        cliente.telefono,
                        str(cliente.estado),
                        int(cliente.correo_confirmado),
                        _iso(cliente.confirmado_en),
                        _iso(cliente.creado_en),
                        _iso(cliente.actualizado_en),
                    ),
                )
                for consentimiento in consentimientos:
                    await conn.execute(
                        """
                        INSERT INTO consentimientos (
                            cliente_id, scope, estado, version, politica_version,
                            canal, otorgado_en, revocado_en, actualizado_en
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            consentimiento.cliente_id,
                            str(consentimiento.scope),
                            str(consentimiento.estado),
                            consentimiento.version,
                            consentimiento.politica_version,
                            str(consentimiento.canal) if consentimiento.canal else None,
                            _iso(consentimiento.otorgado_en),
                            _iso(consentimiento.revocado_en),
                            _iso(consentimiento.actualizado_en),
                        ),
                    )
                await conn.commit()
            except aiosqlite.IntegrityError as exc:
                # El chequeo de unicidad ya se hizo en la capa de servicio antes
                # de llegar aquí; esto solo cubre la condición de carrera. El
                # mensaje de SQLite indica qué columna violó el UNIQUE.
                campo = "correo" if "email" in str(exc) else "documento"
                logger.info("sqlite: conflicto de unicidad campo=%s", campo)
                raise ClienteYaExiste(
                    cliente.tipo_documento, cliente.numero_documento, campo=campo
                ) from exc
        logger.info("sqlite: cliente creado cliente_id=%s", cliente.id)
        return cliente

    async def obtener(self, cliente_id: str) -> Cliente | None:
        async with self._db.connect() as conn:
            cursor = await conn.execute("SELECT * FROM clientes WHERE id = ?", (cliente_id,))
            row = await cursor.fetchone()
        return _row_to_cliente(row) if row else None

    async def obtener_por_identity_ref(self, identity_ref: str) -> Cliente | None:
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                "SELECT * FROM clientes WHERE identity_ref = ? LIMIT 1", (identity_ref,)
            )
            row = await cursor.fetchone()
        return _row_to_cliente(row) if row else None

    async def existe_por_documento(self, tipo_documento: str, numero_documento: str) -> bool:
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                "SELECT 1 FROM clientes WHERE tipo_documento = ? AND numero_documento = ?",
                (str(tipo_documento), numero_documento),
            )
            return await cursor.fetchone() is not None

    async def existe_por_correo(self, email: str) -> bool:
        async with self._db.connect() as conn:
            cursor = await conn.execute("SELECT 1 FROM clientes WHERE email = ?", (email,))
            return await cursor.fetchone() is not None

    async def confirmar(self, cliente_id: str) -> Cliente | None:
        async with self._db.connect() as conn:
            await conn.execute(
                """
                UPDATE clientes SET correo_confirmado = 1, confirmado_en = ?
                WHERE id = ? AND correo_confirmado = 0
                """,
                (_iso(now_utc()), cliente_id),
            )
            await conn.commit()
            cursor = await conn.execute("SELECT * FROM clientes WHERE id = ?", (cliente_id,))
            row = await cursor.fetchone()
        return _row_to_cliente(row) if row else None


class SqliteConsentimientoRepository:
    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    async def listar(self, cliente_id: str) -> list[Consentimiento]:
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                "SELECT * FROM consentimientos WHERE cliente_id = ? ORDER BY scope",
                (cliente_id,),
            )
            rows = await cursor.fetchall()
        return [_row_to_consentimiento(r) for r in rows]

    async def obtener(self, cliente_id: str, scope: ConsentimientoScope) -> Consentimiento | None:
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                "SELECT * FROM consentimientos WHERE cliente_id = ? AND scope = ?",
                (cliente_id, str(scope)),
            )
            row = await cursor.fetchone()
        return _row_to_consentimiento(row) if row else None

    async def guardar(self, consentimiento: Consentimiento) -> Consentimiento:
        async with self._db.connect() as conn:
            await conn.execute(
                """
                INSERT INTO consentimientos (
                    cliente_id, scope, estado, version, politica_version,
                    canal, otorgado_en, revocado_en, actualizado_en
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (cliente_id, scope) DO UPDATE SET
                    estado = excluded.estado,
                    version = excluded.version,
                    politica_version = excluded.politica_version,
                    canal = excluded.canal,
                    otorgado_en = excluded.otorgado_en,
                    revocado_en = excluded.revocado_en,
                    actualizado_en = excluded.actualizado_en
                """,
                (
                    consentimiento.cliente_id,
                    str(consentimiento.scope),
                    str(consentimiento.estado),
                    consentimiento.version,
                    consentimiento.politica_version,
                    str(consentimiento.canal) if consentimiento.canal else None,
                    _iso(consentimiento.otorgado_en),
                    _iso(consentimiento.revocado_en),
                    _iso(consentimiento.actualizado_en),
                ),
            )
            await conn.commit()
        logger.info(
            "sqlite: consentimiento guardado cliente_id=%s scope=%s version=%s",
            sanear_para_log(consentimiento.cliente_id),
            consentimiento.scope,
            consentimiento.version,
        )
        return consentimiento


class SqliteIdempotencyStore:
    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    async def get(self, key: str) -> dict | None:
        async with self._db.connect() as conn:
            cursor = await conn.execute("SELECT valor FROM idempotencia WHERE clave = ?", (key,))
            row = await cursor.fetchone()
        return json.loads(row["valor"]) if row else None

    async def put(self, key: str, value: dict) -> None:
        async with self._db.connect() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO idempotencia (clave, valor, creado_en) VALUES (?, ?, ?)",
                (key, json.dumps(value), now_utc().isoformat()),
            )
            await conn.commit()
