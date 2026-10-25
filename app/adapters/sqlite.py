"""Adaptador de persistencia sobre SQLite (aiosqlite).

Es el backend de desarrollo y del experimento. En producción se implementa el
mismo puerto con Cloud Spanner; el resto del servicio no cambia.
"""

from __future__ import annotations

import base64
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal

import aiosqlite

from app.domain import (
    Canal,
    Cliente,
    ClienteYaExiste,
    CoberturaContratada,
    CodigoCobertura,
    CodigoPendiente,
    Consentimiento,
    ConsentimientoScope,
    DatosCredito,
    Decision,
    DocumentoAceptado,
    EstadoCliente,
    EstadoConsentimiento,
    EstadoEvidencia,
    EstadoPoliza,
    Evidencia,
    EvidenciaDePoliza,
    FiltroPolizas,
    MotivoRechazoPoliza,
    NumeroPolizaDuplicado,
    OrigenDatos,
    PaginaPolizas,
    Pendiente,
    Poliza,
    PolizaYaExiste,
    TipoDecision,
    TipoDocumento,
    TipoEvidencia,
    TipoPendiente,
    Vigencia,
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
    telefono TEXT NOT NULL,
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
CREATE TABLE IF NOT EXISTS evidencias (
    id TEXT PRIMARY KEY,
    cliente_id TEXT NOT NULL,
    tipo TEXT NOT NULL,
    estado TEXT NOT NULL,
    almacen_ref TEXT NOT NULL,
    creada_en TEXT NOT NULL,
    reemplazada_en TEXT,
    poliza_ref TEXT,
    puntaje_vida REAL,
    puntaje_coincidencia REAL
);
CREATE INDEX IF NOT EXISTS idx_evidencias_vigentes ON evidencias (cliente_id, tipo);
CREATE TABLE IF NOT EXISTS evidencia_rechazos (
    cliente_id TEXT NOT NULL,
    tipo TEXT NOT NULL,
    total INTEGER NOT NULL,
    PRIMARY KEY (cliente_id, tipo)
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
            await conn.executescript(_SCHEMA + _ESQUEMA_POLIZAS)
            # Migración manual para bases de dev creadas antes de BITS-93: SQLite
            # no soporta "ADD COLUMN IF NOT EXISTS", así que se intenta y se
            # ignora el error si la columna ya existe. No agrega el UNIQUE(email)
            # a tablas viejas — eso solo aplica a bases nuevas (ver openapi.yaml,
            # Cliente, nota de limitación conocida del backend de dev).
            for ddl in (
                "ALTER TABLE clientes ADD COLUMN correo_confirmado INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE clientes ADD COLUMN confirmado_en TEXT",
                # BITS-235: puntajes de la selfie, en bases de dev anteriores.
                "ALTER TABLE evidencias ADD COLUMN puntaje_vida REAL",
                "ALTER TABLE evidencias ADD COLUMN puntaje_coincidencia REAL",
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


def _row_to_evidencia(row: aiosqlite.Row) -> Evidencia:
    return Evidencia(
        id=row["id"],
        cliente_id=row["cliente_id"],
        tipo=TipoEvidencia(row["tipo"]),
        estado=EstadoEvidencia(row["estado"]),
        almacen_ref=row["almacen_ref"],
        creada_en=_dt(row["creada_en"]),
        reemplazada_en=_dt(row["reemplazada_en"]),
        poliza_ref=row["poliza_ref"],
        puntaje_vida=row["puntaje_vida"],
        puntaje_coincidencia=row["puntaje_coincidencia"],
    )


_VIGENTE = "reemplazada_en IS NULL AND poliza_ref IS NULL"


class SqliteEvidenciaRepository:
    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    async def vigente(self, cliente_id: str, tipo: TipoEvidencia) -> Evidencia | None:
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                f"SELECT * FROM evidencias WHERE cliente_id = ? AND tipo = ? AND {_VIGENTE} "
                "ORDER BY creada_en DESC LIMIT 1",
                (cliente_id, str(tipo)),
            )
            row = await cursor.fetchone()
        return _row_to_evidencia(row) if row else None

    async def guardar_reemplazando(self, evidencia: Evidencia) -> Evidencia:
        async with self._db.connect() as conn:
            await conn.execute(
                f"UPDATE evidencias SET reemplazada_en = ? "
                f"WHERE cliente_id = ? AND tipo = ? AND {_VIGENTE}",
                (_iso(now_utc()), evidencia.cliente_id, str(evidencia.tipo)),
            )
            await conn.execute(
                """
                INSERT INTO evidencias (
                    id, cliente_id, tipo, estado, almacen_ref, creada_en, reemplazada_en,
                    poliza_ref, puntaje_vida, puntaje_coincidencia
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidencia.id,
                    evidencia.cliente_id,
                    str(evidencia.tipo),
                    str(evidencia.estado),
                    evidencia.almacen_ref,
                    _iso(evidencia.creada_en),
                    _iso(evidencia.reemplazada_en),
                    evidencia.poliza_ref,
                    evidencia.puntaje_vida,
                    evidencia.puntaje_coincidencia,
                ),
            )
            await conn.commit()
        logger.info(
            "sqlite: evidencia guardada cliente_id=%s tipo=%s estado=%s",
            sanear_para_log(evidencia.cliente_id),
            evidencia.tipo,
            evidencia.estado,
        )
        return evidencia

    async def consumir(self, cliente_id: str, poliza_ref: str) -> int:
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                f"UPDATE evidencias SET poliza_ref = ? WHERE cliente_id = ? AND {_VIGENTE}",
                (poliza_ref, cliente_id),
            )
            await conn.commit()
            return cursor.rowcount

    async def rechazos(self, cliente_id: str, tipo: TipoEvidencia) -> int:
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                "SELECT total FROM evidencia_rechazos WHERE cliente_id = ? AND tipo = ?",
                (cliente_id, str(tipo)),
            )
            row = await cursor.fetchone()
        return row["total"] if row else 0

    async def registrar_rechazo(self, cliente_id: str, tipo: TipoEvidencia) -> int:
        async with self._db.connect() as conn:
            await conn.execute(
                """
                INSERT INTO evidencia_rechazos (cliente_id, tipo, total) VALUES (?, ?, 1)
                ON CONFLICT (cliente_id, tipo) DO UPDATE SET total = total + 1
                """,
                (cliente_id, str(tipo)),
            )
            await conn.commit()
            cursor = await conn.execute(
                "SELECT total FROM evidencia_rechazos WHERE cliente_id = ? AND tipo = ?",
                (cliente_id, str(tipo)),
            )
            row = await cursor.fetchone()
        return row["total"]

    async def reiniciar_rechazos(self, cliente_id: str, tipo: TipoEvidencia) -> None:
        async with self._db.connect() as conn:
            await conn.execute(
                "DELETE FROM evidencia_rechazos WHERE cliente_id = ? AND tipo = ?",
                (cliente_id, str(tipo)),
            )
            await conn.commit()


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


# --- Pólizas (BITS-101) ---
# Los datos anidados (coberturas, pendientes, decisiones...) van como JSON en la misma fila: la
# póliza se lee y se escribe entera. Las fechas se guardan en UTC para poder compararlas como
# texto.


def _utc(valor: datetime) -> str:
    return valor.astimezone(UTC).isoformat()


def _cobertura_a_dict(c: CoberturaContratada) -> dict:
    return {
        "codigo": str(c.codigo),
        "obligatoria": c.obligatoria,
        "sumaAsegurada": str(c.suma_asegurada),
        "primaNeta": str(c.prima_neta),
        "iva": str(c.iva),
        "primaTotal": str(c.prima_total),
    }


def _cobertura_de_dict(d: dict) -> CoberturaContratada:
    return CoberturaContratada(
        codigo=CodigoCobertura(d["codigo"]),
        obligatoria=d["obligatoria"],
        suma_asegurada=Decimal(d["sumaAsegurada"]),
        prima_neta=Decimal(d["primaNeta"]),
        iva=Decimal(d["iva"]),
        prima_total=Decimal(d["primaTotal"]),
    )


def _pendiente_a_dict(p: Pendiente) -> dict:
    return {
        "codigo": str(p.codigo),
        "tipo": str(p.tipo),
        "mensaje": p.mensaje,
        "evidencias": [str(e) for e in p.evidencias],
    }


def _pendiente_de_dict(d: dict) -> Pendiente:
    return Pendiente(
        codigo=CodigoPendiente(d["codigo"]),
        tipo=TipoPendiente(d["tipo"]),
        mensaje=d["mensaje"],
        evidencias=tuple(TipoEvidencia(e) for e in d["evidencias"]),
    )


def _decision_a_dict(d: Decision) -> dict:
    return {
        "decision": str(d.decision),
        "agente": d.agente,
        "pendientes": [str(p) for p in d.pendientes],
        "decididaEn": _utc(d.decidida_en),
        "nota": d.nota,
    }


def _decision_de_dict(d: dict) -> Decision:
    return Decision(
        decision=TipoDecision(d["decision"]),
        agente=d["agente"],
        pendientes=tuple(CodigoPendiente(p) for p in d["pendientes"]),
        decidida_en=datetime.fromisoformat(d["decididaEn"]),
        nota=d["nota"],
    )


def _poliza_a_fila(p: Poliza) -> tuple:
    c = p.datos_credito
    return (
        p.id,
        p.cliente_id,
        _utc(p.cotizacion_creada_en),
        _utc(p.solicitud_vigente_hasta),
        str(p.estado),
        json.dumps([_cobertura_a_dict(x) for x in p.coberturas]),
        json.dumps(
            {
                "valorCredito": str(c.valor_credito),
                "plazoRestanteMeses": c.plazo_restante_meses,
                "entidadAcreedora": c.entidad_acreedora,
                "saldoInsoluto": str(c.saldo_insoluto),
            }
        ),
        str(p.origen_datos),
        str(p.tasa),
        p.version_tarifa,
        p.cobertura_meses,
        p.edad,
        json.dumps(
            [
                {"tipo": d.tipo, "version": d.version, "aceptadoEn": _utc(d.aceptado_en)}
                for d in p.documentos_aceptados
            ]
        ),
        json.dumps([_pendiente_a_dict(x) for x in p.pendientes]),
        json.dumps([_decision_a_dict(x) for x in p.decisiones]),
        json.dumps([{"codigo": str(m.codigo), "mensaje": m.mensaje} for m in p.motivos_rechazo]),
        json.dumps(
            [
                {"id": e.evidencia_id, "tipo": str(e.tipo), "estado": str(e.estado)}
                for e in p.evidencias
            ]
        ),
        p.numero,
        _iso(p.emitida_en and p.emitida_en.astimezone(UTC)),
        _iso(p.vigencia.desde.astimezone(UTC)) if p.vigencia else None,
        _iso(p.vigencia.hasta.astimezone(UTC)) if p.vigencia else None,
        _iso(p.rechazada_en and p.rechazada_en.astimezone(UTC)),
        _utc(p.creada_en),
        _iso(p.ultima_evaluacion_en and p.ultima_evaluacion_en.astimezone(UTC)),
        _iso(p.documento_enviado_en and p.documento_enviado_en.astimezone(UTC)),
    )


_COLUMNAS_POLIZA = (
    "id, cliente_id, cotizacion_creada_en, solicitud_vigente_hasta, estado, coberturas, "
    "datos_credito, origen_datos, tasa, version_tarifa, cobertura_meses, edad, "
    "documentos_aceptados, pendientes, decisiones, motivos_rechazo, evidencias, numero, "
    "emitida_en, vigencia_desde, vigencia_hasta, rechazada_en, creada_en, ultima_evaluacion_en, "
    "documento_enviado_en"
)


def _fila_a_poliza(row: aiosqlite.Row) -> Poliza:
    credito = json.loads(row["datos_credito"])
    vigencia = (
        Vigencia(_dt(row["vigencia_desde"]), _dt(row["vigencia_hasta"]))
        if row["vigencia_desde"]
        else None
    )
    return Poliza(
        id=row["id"],
        cliente_id=row["cliente_id"],
        cotizacion_creada_en=_dt(row["cotizacion_creada_en"]),
        solicitud_vigente_hasta=_dt(row["solicitud_vigente_hasta"]),
        estado=EstadoPoliza(row["estado"]),
        coberturas=tuple(_cobertura_de_dict(x) for x in json.loads(row["coberturas"])),
        datos_credito=DatosCredito(
            valor_credito=Decimal(credito["valorCredito"]),
            plazo_restante_meses=credito["plazoRestanteMeses"],
            entidad_acreedora=credito["entidadAcreedora"],
            saldo_insoluto=Decimal(credito["saldoInsoluto"]),
        ),
        origen_datos=OrigenDatos(row["origen_datos"]),
        tasa=Decimal(row["tasa"]),
        version_tarifa=row["version_tarifa"],
        cobertura_meses=row["cobertura_meses"],
        edad=row["edad"],
        documentos_aceptados=tuple(
            DocumentoAceptado(d["tipo"], d["version"], datetime.fromisoformat(d["aceptadoEn"]))
            for d in json.loads(row["documentos_aceptados"])
        ),
        pendientes=tuple(_pendiente_de_dict(x) for x in json.loads(row["pendientes"])),
        decisiones=tuple(_decision_de_dict(x) for x in json.loads(row["decisiones"])),
        motivos_rechazo=tuple(
            MotivoRechazoPoliza(CodigoPendiente(m["codigo"]), m["mensaje"])
            for m in json.loads(row["motivos_rechazo"])
        ),
        evidencias=tuple(
            EvidenciaDePoliza(TipoEvidencia(e["tipo"]), EstadoEvidencia(e["estado"]), e["id"])
            for e in json.loads(row["evidencias"])
        ),
        numero=row["numero"],
        emitida_en=_dt(row["emitida_en"]),
        vigencia=vigencia,
        rechazada_en=_dt(row["rechazada_en"]),
        creada_en=_dt(row["creada_en"]),
        ultima_evaluacion_en=_dt(row["ultima_evaluacion_en"]),
        documento_enviado_en=_dt(row["documento_enviado_en"]),
    )


def _cursor_de(poliza: Poliza) -> str:
    crudo = f"{_utc(poliza.creada_en)}|{poliza.id}"
    return base64.urlsafe_b64encode(crudo.encode()).decode()


class SqlitePolizaRepository:
    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    async def _leer(self, conn: aiosqlite.Connection, poliza_id: str) -> Poliza | None:
        cursor = await conn.execute(
            f"SELECT {_COLUMNAS_POLIZA} FROM polizas WHERE id = ?", (poliza_id,)
        )
        row = await cursor.fetchone()
        return _fila_a_poliza(row) if row else None

    async def crear(self, poliza: Poliza) -> None:
        async with self._db.connect() as conn:
            try:
                await conn.execute(
                    f"INSERT INTO polizas ({_COLUMNAS_POLIZA}) VALUES ({', '.join('?' * 25)})",
                    _poliza_a_fila(poliza),
                )
                await conn.commit()
            except aiosqlite.IntegrityError as exc:
                raise PolizaYaExiste(poliza.cliente_id) from exc
        logger.info(
            "sqlite: póliza creada cliente_id=%s estado=%s",
            sanear_para_log(poliza.cliente_id),
            poliza.estado,
        )

    async def obtener(self, poliza_id: str) -> Poliza | None:
        async with self._db.connect() as conn:
            return await self._leer(conn, poliza_id)

    async def obtener_de_cotizacion(
        self, cliente_id: str, cotizacion_creada_en: datetime
    ) -> Poliza | None:
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                f"SELECT {_COLUMNAS_POLIZA} FROM polizas "
                "WHERE cliente_id = ? AND cotizacion_creada_en = ?",
                (cliente_id, _utc(cotizacion_creada_en)),
            )
            row = await cursor.fetchone()
        return _fila_a_poliza(row) if row else None

    async def listar_de_cliente(self, cliente_id: str) -> list[Poliza]:
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                f"SELECT {_COLUMNAS_POLIZA} FROM polizas WHERE cliente_id = ? "
                "ORDER BY creada_en DESC",
                (cliente_id,),
            )
            return [_fila_a_poliza(r) for r in await cursor.fetchall()]

    async def pendientes_de_cliente(self, cliente_id: str) -> list[Poliza]:
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                f"SELECT {_COLUMNAS_POLIZA} FROM polizas "
                "WHERE cliente_id = ? AND estado = 'PENDIENTE'",
                (cliente_id,),
            )
            return [_fila_a_poliza(r) for r in await cursor.fetchall()]

    async def listar(self, filtro: FiltroPolizas, ahora: datetime) -> PaginaPolizas:
        condiciones: list[str] = []
        parametros: list = []
        if filtro.estado is EstadoPoliza.VENCIDA:
            condiciones.append("estado = 'PENDIENTE' AND solicitud_vigente_hasta <= ?")
            parametros.append(_utc(ahora))
        elif filtro.estado is EstadoPoliza.PENDIENTE:
            condiciones.append("estado = 'PENDIENTE' AND solicitud_vigente_hasta > ?")
            parametros.append(_utc(ahora))
        elif filtro.estado is not None:
            condiciones.append("estado = ?")
            parametros.append(str(filtro.estado))
        for campo, valor in (
            ("$.tipo", filtro.tipo_pendiente),
            ("$.codigo", filtro.codigo_pendiente),
        ):
            if valor is not None:
                condiciones.append(
                    "EXISTS (SELECT 1 FROM json_each(polizas.pendientes) j "
                    f"WHERE json_extract(j.value, '{campo}') = ?)"
                )
                parametros.append(str(valor))
        if filtro.cliente_id:
            condiciones.append("cliente_id = ?")
            parametros.append(filtro.cliente_id)
        if filtro.creada_desde:
            condiciones.append("creada_en >= ?")
            parametros.append(_utc(filtro.creada_desde))
        if filtro.creada_hasta:
            condiciones.append("creada_en <= ?")
            parametros.append(_utc(filtro.creada_hasta))
        comparador, orden = ("<", "DESC") if filtro.mas_recientes_primero else (">", "ASC")
        if filtro.cursor:
            creada_en, _, poliza_id = (
                base64.urlsafe_b64decode(filtro.cursor.encode()).decode().partition("|")
            )
            condiciones.append(f"(creada_en, id) {comparador} (?, ?)")
            parametros.extend([creada_en, poliza_id])
        donde = f"WHERE {' AND '.join(condiciones)}" if condiciones else ""
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                f"SELECT {_COLUMNAS_POLIZA} FROM polizas {donde} "
                f"ORDER BY creada_en {orden}, id {orden} LIMIT ?",
                (*parametros, filtro.limite + 1),
            )
            filas = await cursor.fetchall()
        items = [_fila_a_poliza(r) for r in filas]
        hay_mas = len(items) > filtro.limite
        items = items[: filtro.limite]
        return PaginaPolizas(tuple(items), _cursor_de(items[-1]) if hay_mas else None)

    async def actualizar_pendientes(
        self, poliza_id: str, pendientes: tuple[Pendiente, ...], evaluada_en: datetime
    ) -> None:
        async with self._db.connect() as conn:
            await conn.execute(
                "UPDATE polizas SET pendientes = ?, ultima_evaluacion_en = ? "
                "WHERE id = ? AND estado = 'PENDIENTE'",
                (
                    json.dumps([_pendiente_a_dict(p) for p in pendientes]),
                    _utc(evaluada_en),
                    poliza_id,
                ),
            )
            await conn.commit()

    async def emitir(
        self, poliza_id: str, numero: str, emitida_en: datetime, vigencia: Vigencia
    ) -> Poliza | None:
        async with self._db.connect() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = await conn.execute(
                    "UPDATE polizas SET estado = 'EMITIDA', numero = ?, emitida_en = ?, "
                    "vigencia_desde = ?, vigencia_hasta = ?, pendientes = '[]' "
                    "WHERE id = ? AND estado = 'PENDIENTE'",
                    (
                        numero,
                        _utc(emitida_en),
                        _utc(vigencia.desde),
                        _utc(vigencia.hasta),
                        poliza_id,
                    ),
                )
                if cursor.rowcount == 0:
                    await conn.rollback()
                    return None
                cursor = await conn.execute(
                    "SELECT cliente_id FROM polizas WHERE id = ?", (poliza_id,)
                )
                cliente_id = (await cursor.fetchone())["cliente_id"]
                cursor = await conn.execute(
                    f"SELECT id, tipo, estado FROM evidencias WHERE cliente_id = ? AND {_VIGENTE}",
                    (cliente_id,),
                )
                consumidas = [
                    {"id": r["id"], "tipo": r["tipo"], "estado": r["estado"]}
                    for r in await cursor.fetchall()
                ]
                await conn.execute(
                    f"UPDATE evidencias SET poliza_ref = ? WHERE cliente_id = ? AND {_VIGENTE}",
                    (poliza_id, cliente_id),
                )
                await conn.execute(
                    "UPDATE polizas SET evidencias = ? WHERE id = ?",
                    (json.dumps(consumidas), poliza_id),
                )
                await conn.commit()
            except aiosqlite.IntegrityError as exc:
                await conn.rollback()
                raise NumeroPolizaDuplicado(numero) from exc
            return await self._leer(conn, poliza_id)

    async def _con_decision(
        self, conn: aiosqlite.Connection, poliza_id: str, decision: Decision
    ) -> list[dict] | None:
        """Lee las decisiones de una póliza ``PENDIENTE`` (con el candado de escritura tomado)
        y devuelve la lista con la nueva; ``None`` si no existe o ya no está pendiente."""
        cursor = await conn.execute(
            "SELECT decisiones FROM polizas WHERE id = ? AND estado = 'PENDIENTE'", (poliza_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return [*json.loads(row["decisiones"]), _decision_a_dict(decision)]

    async def aprobar(self, poliza_id: str, decision: Decision) -> bool:
        async with self._db.connect() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            decisiones = await self._con_decision(conn, poliza_id, decision)
            if decisiones is None:
                await conn.rollback()
                return False
            await conn.execute(
                "UPDATE polizas SET decisiones = ? WHERE id = ?",
                (json.dumps(decisiones), poliza_id),
            )
            await conn.commit()
            return True

    async def rechazar(
        self,
        poliza_id: str,
        decision: Decision,
        motivos: tuple[MotivoRechazoPoliza, ...],
        rechazada_en: datetime,
    ) -> Poliza | None:
        async with self._db.connect() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            decisiones = await self._con_decision(conn, poliza_id, decision)
            if decisiones is None:
                await conn.rollback()
                return None
            await conn.execute(
                "UPDATE polizas SET estado = 'RECHAZADA', decisiones = ?, motivos_rechazo = ?, "
                "rechazada_en = ? WHERE id = ?",
                (
                    json.dumps(decisiones),
                    json.dumps([{"codigo": str(m.codigo), "mensaje": m.mensaje} for m in motivos]),
                    _utc(rechazada_en),
                    poliza_id,
                ),
            )
            await conn.commit()
            return await self._leer(conn, poliza_id)

    async def reclamar_envio_documento(self, poliza_id: str, cuando: datetime) -> bool:
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                "UPDATE polizas SET documento_enviado_en = ? "
                "WHERE id = ? AND documento_enviado_en IS NULL",
                (_utc(cuando), poliza_id),
            )
            await conn.commit()
            return cursor.rowcount == 1

    async def liberar_envio_documento(self, poliza_id: str) -> None:
        async with self._db.connect() as conn:
            await conn.execute(
                "UPDATE polizas SET documento_enviado_en = NULL WHERE id = ?", (poliza_id,)
            )
            await conn.commit()

    async def borrar_pendientes_de_cliente(
        self, cliente_id: str, salvo_cotizacion: datetime | None
    ) -> int:
        async with self._db.connect() as conn:
            if salvo_cotizacion is None:
                cursor = await conn.execute(
                    "DELETE FROM polizas WHERE cliente_id = ? AND estado = 'PENDIENTE'",
                    (cliente_id,),
                )
            else:
                cursor = await conn.execute(
                    "DELETE FROM polizas WHERE cliente_id = ? AND estado = 'PENDIENTE' "
                    "AND cotizacion_creada_en <> ?",
                    (cliente_id, _utc(salvo_cotizacion)),
                )
            await conn.commit()
            return cursor.rowcount


_ESQUEMA_POLIZAS = """
CREATE TABLE IF NOT EXISTS polizas (
    id TEXT PRIMARY KEY,
    cliente_id TEXT NOT NULL,
    cotizacion_creada_en TEXT NOT NULL,
    solicitud_vigente_hasta TEXT NOT NULL,
    estado TEXT NOT NULL,
    coberturas TEXT NOT NULL,
    datos_credito TEXT NOT NULL,
    origen_datos TEXT NOT NULL,
    tasa TEXT NOT NULL,
    version_tarifa INTEGER NOT NULL,
    cobertura_meses INTEGER NOT NULL,
    edad INTEGER NOT NULL,
    documentos_aceptados TEXT NOT NULL,
    pendientes TEXT NOT NULL,
    decisiones TEXT NOT NULL DEFAULT '[]',
    motivos_rechazo TEXT NOT NULL DEFAULT '[]',
    evidencias TEXT NOT NULL DEFAULT '[]',
    numero TEXT,
    emitida_en TEXT,
    vigencia_desde TEXT,
    vigencia_hasta TEXT,
    rechazada_en TEXT,
    creada_en TEXT NOT NULL,
    ultima_evaluacion_en TEXT,
    documento_enviado_en TEXT,
    UNIQUE (cliente_id, cotizacion_creada_en)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_polizas_numero ON polizas (numero) WHERE numero IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_polizas_cliente ON polizas (cliente_id, estado);
CREATE INDEX IF NOT EXISTS idx_polizas_cola ON polizas (estado, creada_en);
"""
