"""BITS-229: registro de dispositivos y aviso push de póliza emitida."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.adapters.factory import build_avisos, build_dispositivos
from app.adapters.memory import InMemoryDispositivoRepository, InMemoryEventPublisher
from app.adapters.sqlite import SqliteDatabase, SqliteDispositivoRepository
from app.avisos import (
    CUERPO_POLIZA_EMITIDA,
    EVENTO_ENVIAR_PUSH,
    TITULO_POLIZA_EMITIDA,
    AvisoService,
)
from app.config import Settings
from app.dispositivos import DispositivoService, token_corto
from app.domain import (
    MAX_DISPOSITIVOS_POR_CLIENTE,
    ClienteNoEncontrado,
    Dispositivo,
    PlataformaDispositivo,
)
from app.polizas import PolizaService


def _dispositivo(cliente_id="c1", token="token-aaaaaaaa", minutos=0) -> Dispositivo:
    ahora = datetime(2026, 10, 30, 12, 0, tzinfo=UTC) + timedelta(minutes=minutos)
    return Dispositivo(
        cliente_id=cliente_id,
        token=token,
        plataforma=PlataformaDispositivo.ANDROID,
        version_app="1.0.0",
        registrado_en=ahora,
        ultimo_visto_en=ahora,
    )


@pytest.fixture(params=["memoria", "sqlite"])
def repo(request, tmp_path):
    if request.param == "memoria":
        return InMemoryDispositivoRepository()
    db = SqliteDatabase(str(tmp_path / "d.db"))
    return SqliteDispositivoRepository(db)


@pytest.fixture(autouse=True)
async def _esquema(repo):
    db = getattr(repo, "_db", None)
    if db is not None:
        await db.init()


# --- repositorio (memoria y SQLite se comportan igual) ---


async def test_registrar_guarda_y_lista_primero_el_mas_reciente(repo):
    await repo.registrar(_dispositivo(token="token-viejo-1", minutos=0), 5)
    await repo.registrar(_dispositivo(token="token-nuevo-1", minutos=5), 5)

    assert [d.token for d in await repo.de_cliente("c1")] == ["token-nuevo-1", "token-viejo-1"]
    assert await repo.de_cliente("otro") == []


async def test_repetir_el_token_conserva_el_id_y_renueva_la_marca(repo):
    primero = await repo.registrar(_dispositivo(minutos=0), 5)
    segundo = await repo.registrar(_con_version(9), 5)

    assert segundo.id == primero.id
    lista = await repo.de_cliente("c1")
    assert len(lista) == 1
    assert lista[0].version_app == "2.0.0"
    assert lista[0].ultimo_visto_en > lista[0].registrado_en


def _con_version(minutos: int) -> Dispositivo:
    d = _dispositivo(minutos=minutos)
    d.version_app = "2.0.0"
    return d


async def test_un_token_de_otro_cliente_se_reasigna_conservando_el_id(repo):
    de_ana = await repo.registrar(_dispositivo(cliente_id="ana", minutos=0), 5)

    de_luis = await repo.registrar(_dispositivo(cliente_id="luis", minutos=3), 5)

    assert de_luis.id == de_ana.id
    assert await repo.de_cliente("ana") == []
    assert [d.cliente_id for d in await repo.de_cliente("luis")] == ["luis"]
    # El anterior dueño ya no puede quitarlo.
    assert await repo.quitar("ana", de_ana.id) is False
    assert len(await repo.de_cliente("luis")) == 1


async def test_con_mas_del_maximo_se_descarta_el_menos_visto(repo):
    for i in range(MAX_DISPOSITIVOS_POR_CLIENTE + 2):
        await repo.registrar(_dispositivo(token=f"token-{i:08d}", minutos=i), 5)

    tokens = [d.token for d in await repo.de_cliente("c1")]

    assert len(tokens) == MAX_DISPOSITIVOS_POR_CLIENTE
    assert tokens[0] == "token-00000006"
    assert "token-00000000" not in tokens and "token-00000001" not in tokens


async def test_el_recien_guardado_se_queda_aunque_empate_en_la_marca(repo):
    for i in range(5):
        await repo.registrar(_dispositivo(token=f"token-{i:08d}", minutos=0), 5)

    guardado = await repo.registrar(_dispositivo(token="token-nuevo-000", minutos=0), 5)

    tokens = [d.token for d in await repo.de_cliente("c1")]
    assert len(tokens) == 5
    assert guardado.token in tokens


async def test_el_tope_es_por_cliente(repo):
    for i in range(3):
        await repo.registrar(_dispositivo(cliente_id="ana", token=f"ana-token-{i:03d}"), 3)
    await repo.registrar(_dispositivo(cliente_id="luis", token="luis-token-000"), 3)

    assert len(await repo.de_cliente("ana")) == 3
    assert len(await repo.de_cliente("luis")) == 1


async def test_quitar_solo_borra_el_del_cliente_y_es_idempotente(repo):
    guardado = await repo.registrar(_dispositivo(cliente_id="ana"), 5)

    assert await repo.quitar("luis", guardado.id) is False
    assert len(await repo.de_cliente("ana")) == 1
    assert await repo.quitar("ana", guardado.id) is True
    assert await repo.quitar("ana", guardado.id) is False
    assert await repo.de_cliente("ana") == []


# --- servicio ---


class _Clientes:
    def __init__(self, ids):
        self._ids = set(ids)

    async def obtener(self, cliente_id):
        return object() if cliente_id in self._ids else None


async def test_el_servicio_registra_y_rechaza_clientes_desconocidos():
    servicio = DispositivoService(_Clientes(["c1"]), InMemoryDispositivoRepository())

    d = await servicio.registrar("c1", "token-aaaaaaaa", PlataformaDispositivo.ANDROID, "1.0.0")
    mismo = await servicio.registrar("c1", "token-aaaaaaaa", PlataformaDispositivo.ANDROID, "1.0.1")

    assert d.id == mismo.id
    with pytest.raises(ClienteNoEncontrado):
        await servicio.registrar("x", "token-bbbbbbbb", PlataformaDispositivo.ANDROID, "1.0.0")


async def test_quitar_un_dispositivo_inexistente_no_falla():
    servicio = DispositivoService(_Clientes(["c1"]), InMemoryDispositivoRepository())

    await servicio.quitar("c1", "no-existe")


def test_el_log_solo_lleva_el_final_del_token():
    assert token_corto("abcdefghijklmnop") == "…klmnop"


# --- aviso de póliza emitida ---


class _PolizaFalsa:
    id = "poliza-1"
    cliente_id = "c1"


async def test_el_aviso_lleva_los_textos_aprobados_y_los_tokens_del_cliente():
    dispositivos = InMemoryDispositivoRepository()
    await dispositivos.registrar(_dispositivo(token="token-viejo-1", minutos=0), 5)
    await dispositivos.registrar(_dispositivo(token="token-nuevo-1", minutos=5), 5)
    eventos = InMemoryEventPublisher()

    await AvisoService(dispositivos, eventos).avisar_poliza_emitida(_PolizaFalsa())

    [evento] = eventos.events
    assert evento.tipo == EVENTO_ENVIAR_PUSH == "EnviarPush"
    assert evento.datos == {
        "clienteId": "c1",
        "tokens": ["token-nuevo-1", "token-viejo-1"],
        "titulo": TITULO_POLIZA_EMITIDA,
        "cuerpo": CUERPO_POLIZA_EMITIDA,
        "ruta": {"destino": "seguro", "id": "poliza-1"},
        "prioridad": "alta",
    }
    assert TITULO_POLIZA_EMITIDA == "Tu póliza ya está activa"
    assert CUERPO_POLIZA_EMITIDA == "Ya puedes ver tu seguro y descargar el documento."


async def test_sin_dispositivos_no_se_publica_nada():
    eventos = InMemoryEventPublisher()

    await AvisoService(InMemoryDispositivoRepository(), eventos).avisar_poliza_emitida(
        _PolizaFalsa()
    )

    assert eventos.events == []


async def test_un_fallo_del_aviso_no_rompe_la_emision(caplog):
    class _Roto:
        async def avisar_poliza_emitida(self, poliza):
            raise RuntimeError("pubsub caído")

    servicio = PolizaService.__new__(PolizaService)
    servicio._avisos = _Roto()

    await servicio._avisar_emitida(_PolizaFalsa())

    assert "no se pudo encargar el aviso push" in caplog.text


async def test_sin_servicio_de_avisos_no_hay_nada_que_hacer():
    servicio = PolizaService.__new__(PolizaService)
    servicio._avisos = None

    await servicio._avisar_emitida(_PolizaFalsa())


# --- fábrica ---


def test_la_fabrica_apaga_o_enciende_los_avisos():
    repo = InMemoryDispositivoRepository()
    eventos = InMemoryEventPublisher()

    assert build_avisos(Settings(), repo, eventos) is None
    assert isinstance(build_avisos(Settings(push_backend="on"), repo, eventos), AvisoService)
    with pytest.raises(ValueError):
        build_avisos(Settings(push_backend="otro"), repo, eventos)


def test_la_fabrica_elige_el_repositorio_de_dispositivos(tmp_path):
    assert isinstance(
        build_dispositivos(Settings(repository_backend="memory")), InMemoryDispositivoRepository
    )
    assert isinstance(
        build_dispositivos(
            Settings(repository_backend="sqlite", database_path=str(tmp_path / "x"))
        ),
        SqliteDispositivoRepository,
    )
    with pytest.raises(ValueError):
        build_dispositivos(Settings(repository_backend="otro"))
