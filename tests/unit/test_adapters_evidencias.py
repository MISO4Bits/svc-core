"""Repositorios y almacenes de evidencias: el mismo comportamiento en memoria y en SQLite."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.adapters.almacen import FilesystemAlmacenEvidencias, InMemoryAlmacenEvidencias
from app.adapters.gcs import GcsAlmacenEvidencias
from app.adapters.memory import InMemoryEvidenciaRepository
from app.adapters.sqlite import SqliteDatabase, SqliteEvidenciaRepository
from app.domain import EstadoEvidencia, Evidencia, TipoEvidencia

FRENTE, REVERSO = TipoEvidencia.CEDULA_FRENTE, TipoEvidencia.CEDULA_REVERSO
SELFIE = TipoEvidencia.SELFIE
CLIENTE, OTRO = "cliente-1", "cliente-2"


@pytest.fixture(params=["memory", "sqlite"])
async def repo(request, tmp_path):
    if request.param == "memory":
        return InMemoryEvidenciaRepository()
    db = SqliteDatabase(str(tmp_path / "evidencias.db"))
    await db.init()
    return SqliteEvidenciaRepository(db)


def _evidencia(tipo=FRENTE, cliente=CLIENTE, minutos=0, **kw) -> Evidencia:
    base = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    return Evidencia(
        cliente_id=cliente,
        tipo=tipo,
        estado=kw.pop("estado", EstadoEvidencia.ACEPTADA),
        almacen_ref=f"ref-{tipo}-{minutos}",
        creada_en=base + timedelta(minutes=minutos),
        **kw,
    )


async def test_sin_evidencias_no_hay_vigente(repo):
    assert await repo.vigente(CLIENTE, FRENTE) is None


async def test_guardar_deja_la_evidencia_como_vigente(repo):
    guardada = await repo.guardar_reemplazando(_evidencia())

    vigente = await repo.vigente(CLIENTE, FRENTE)

    assert vigente is not None
    assert vigente.id == guardada.id
    assert vigente.estado is EstadoEvidencia.ACEPTADA
    assert vigente.poliza_ref is None


async def test_la_vigente_es_por_cliente_y_por_tipo(repo):
    await repo.guardar_reemplazando(_evidencia(FRENTE))

    assert await repo.vigente(CLIENTE, REVERSO) is None
    assert await repo.vigente(OTRO, FRENTE) is None


async def test_repetir_reemplaza_a_la_vigente_anterior(repo):
    primera = await repo.guardar_reemplazando(_evidencia(minutos=0))
    segunda = await repo.guardar_reemplazando(_evidencia(minutos=5))

    vigente = await repo.vigente(CLIENTE, FRENTE)

    assert vigente is not None
    assert vigente.id == segunda.id != primera.id


async def test_consumir_asocia_las_vigentes_a_la_poliza_y_dejan_de_ser_vigentes(repo):
    await repo.guardar_reemplazando(_evidencia(FRENTE))
    await repo.guardar_reemplazando(_evidencia(REVERSO))
    await repo.guardar_reemplazando(_evidencia(FRENTE, cliente=OTRO))

    consumidas = await repo.consumir(CLIENTE, "poliza-9")

    assert consumidas == 2
    assert await repo.vigente(CLIENTE, FRENTE) is None
    assert await repo.vigente(CLIENTE, REVERSO) is None
    assert await repo.vigente(OTRO, FRENTE) is not None  # no toca a otros clientes


async def test_una_captura_nueva_no_modifica_la_evidencia_ya_consumida(repo):
    await repo.guardar_reemplazando(_evidencia(minutos=0))
    await repo.consumir(CLIENTE, "poliza-9")

    nueva = await repo.guardar_reemplazando(_evidencia(minutos=10))

    vigente = await repo.vigente(CLIENTE, FRENTE)
    assert vigente is not None
    assert vigente.id == nueva.id
    assert await repo.consumir(CLIENTE, "poliza-10") == 1  # solo la nueva


async def test_consumir_sin_vigentes_devuelve_cero(repo):
    assert await repo.consumir(CLIENTE, "poliza-9") == 0


async def test_cuenta_los_rechazos_por_cliente_y_tipo(repo):
    assert await repo.rechazos(CLIENTE, FRENTE) == 0
    assert await repo.registrar_rechazo(CLIENTE, FRENTE) == 1
    assert await repo.registrar_rechazo(CLIENTE, FRENTE) == 2
    assert await repo.registrar_rechazo(CLIENTE, REVERSO) == 1

    assert await repo.rechazos(CLIENTE, FRENTE) == 2
    assert await repo.rechazos(OTRO, FRENTE) == 0


async def test_reiniciar_los_rechazos_vuelve_a_cero(repo):
    await repo.registrar_rechazo(CLIENTE, FRENTE)

    await repo.reiniciar_rechazos(CLIENTE, FRENTE)
    await repo.reiniciar_rechazos(CLIENTE, REVERSO)  # sin contador: no falla

    assert await repo.rechazos(CLIENTE, FRENTE) == 0


# ---------- almacenes ----------


async def test_el_almacen_en_memoria_devuelve_lo_guardado():
    almacen = InMemoryAlmacenEvidencias()

    ref = await almacen.guardar("a/b.jpg", b"datos", "image/jpeg")

    assert await almacen.obtener(ref) == b"datos"


async def test_el_almacen_en_disco_guarda_y_recupera(tmp_path):
    almacen = FilesystemAlmacenEvidencias(str(tmp_path))

    ref = await almacen.guardar("evidencias/c1/cedula_frente/e1.jpg", b"datos", "image/jpeg")

    assert await almacen.obtener(ref) == b"datos"
    assert (tmp_path / "evidencias" / "c1" / "cedula_frente" / "e1.jpg").exists()


async def test_el_almacen_en_disco_no_deja_salir_de_su_carpeta(tmp_path):
    almacen = FilesystemAlmacenEvidencias(str(tmp_path / "raiz"))

    with pytest.raises(ValueError, match="carpeta"):
        await almacen.guardar("../fuera.jpg", b"x", "image/jpeg")


class _BlobFalso:
    def __init__(self, almacenados: dict, clave: str) -> None:
        self._almacenados, self._clave = almacenados, clave

    def upload_from_string(self, datos: bytes, content_type: str) -> None:
        self._almacenados[self._clave] = (datos, content_type)

    def download_as_bytes(self) -> bytes:
        return self._almacenados[self._clave][0]


class _ClienteGcsFalso:
    def __init__(self) -> None:
        self.almacenados: dict = {}
        self.bucket_pedido: str | None = None

    def bucket(self, nombre: str):
        self.bucket_pedido = nombre
        return self

    def blob(self, clave: str) -> _BlobFalso:
        return _BlobFalso(self.almacenados, clave)


async def test_el_almacen_gcs_guarda_con_su_tipo_y_recupera():
    cliente = _ClienteGcsFalso()
    almacen = GcsAlmacenEvidencias("solventa-evidencias", client=cliente)

    ref = await almacen.guardar("evidencias/c1/e1.png", b"datos", "image/png")

    assert cliente.bucket_pedido == "solventa-evidencias"
    assert cliente.almacenados["evidencias/c1/e1.png"] == (b"datos", "image/png")
    assert await almacen.obtener(ref) == b"datos"


async def test_los_puntajes_de_la_selfie_se_guardan_y_se_leen(repo):
    await repo.guardar_reemplazando(
        _evidencia(SELFIE, puntaje_vida=0.91, puntaje_coincidencia=0.55)
    )

    vigente = await repo.vigente(CLIENTE, SELFIE)

    assert vigente is not None
    assert (vigente.puntaje_vida, vigente.puntaje_coincidencia) == (0.91, 0.55)


async def test_una_evidencia_sin_puntajes_los_devuelve_vacios(repo):
    await repo.guardar_reemplazando(_evidencia(FRENTE))

    vigente = await repo.vigente(CLIENTE, FRENTE)

    assert vigente is not None
    assert (vigente.puntaje_vida, vigente.puntaje_coincidencia) == (None, None)


async def test_una_base_anterior_sin_las_columnas_de_puntajes_se_migra(tmp_path):
    import aiosqlite

    ruta = str(tmp_path / "vieja.db")
    async with aiosqlite.connect(ruta) as conn:
        await conn.execute(
            "CREATE TABLE evidencias (id TEXT PRIMARY KEY, cliente_id TEXT NOT NULL, "
            "tipo TEXT NOT NULL, estado TEXT NOT NULL, almacen_ref TEXT NOT NULL, "
            "creada_en TEXT NOT NULL, reemplazada_en TEXT, poliza_ref TEXT)"
        )
        await conn.commit()
    db = SqliteDatabase(ruta)
    await db.init()
    repositorio = SqliteEvidenciaRepository(db)

    await repositorio.guardar_reemplazando(_evidencia(SELFIE, puntaje_vida=0.7))

    vigente = await repositorio.vigente(CLIENTE, SELFIE)
    assert vigente is not None and vigente.puntaje_vida == 0.7
