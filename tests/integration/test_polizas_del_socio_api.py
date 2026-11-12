"""BITS-380: ``GET /v1/polizas?socioId=``, las pólizas que vendió cada socio."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.adapters.factory import maybe_init
from app.adapters.memory import InMemoryClienteRepository, InMemoryPolizaRepository
from app.api.app import create_app
from app.domain import Cliente, EstadoPoliza, FiltroPolizas, TipoDocumento
from tests.conftest import CLIENTE_VALIDO
from tests.integration.test_venta_directa_api import (
    ASISTIDO,
    _asistido,
    _settings,
    _validar,
    _vender,
)


@pytest_asyncio.fixture
async def app(tmp_path):
    aplicacion = create_app(_settings(tmp_path))
    await maybe_init(aplicacion.state.clientes)
    return aplicacion


@pytest_asyncio.fixture
async def http(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as cliente:
        yield cliente


SOCIO_A = "socio-uid-a"
SOCIO_B = "socio-uid-b"


async def _vendida(http, socio: str, **cliente) -> dict:
    """Da de alta un cliente y le vende una póliza como ``socio``; devuelve la póliza."""
    cliente_id = (await _asistido(http, **cliente)).json()["id"]
    respuesta = await _vender(http, cliente_id, socioId=socio)
    assert respuesta.status_code == 201
    return respuesta.json()


def _otro(numero: str, correo: str, **extra) -> dict:
    return {"numeroDocumento": numero, "email": correo, **extra}


async def _listar(http, **params):
    return await http.get("/v1/polizas", params=params)


async def test_cada_socio_recibe_solo_lo_que_vendio(app, http, openapi_spec):
    de_a = await _vendida(http, SOCIO_A)
    de_b = await _vendida(http, SOCIO_B, **_otro("80999888", "otro@example.com"))

    lista_a = await _listar(http, socioId=SOCIO_A)
    lista_b = await _listar(http, socioId=SOCIO_B)

    assert lista_a.status_code == lista_b.status_code == 200
    assert [p["polizaId"] for p in lista_a.json()["items"]] == [de_a["polizaId"]]
    assert [p["polizaId"] for p in lista_b.json()["items"]] == [de_b["polizaId"]]
    _validar(openapi_spec, "PaginaPolizas", lista_a.json())


async def test_un_socio_sin_ventas_recibe_la_lista_vacia(app, http):
    await _vendida(http, SOCIO_A)

    lista = await _listar(http, socioId="socio-sin-ventas")

    assert lista.status_code == 200
    assert lista.json() == {"items": [], "siguienteCursor": None}


async def test_las_polizas_que_no_vendio_un_socio_no_salen_con_su_filtro(app, http):
    """El autoservicio (sin socio) y las de otro socio no aparecen."""
    await _vendida(http, SOCIO_B)
    cliente_propio = (await http.post("/v1/clientes", json=_cliente_de_autoservicio())).json()["id"]
    assert cliente_propio

    lista = await _listar(http, socioId=SOCIO_A)

    assert lista.json()["items"] == []


def _cliente_de_autoservicio() -> dict:
    return CLIENTE_VALIDO


async def test_cada_fila_trae_el_cliente_con_los_ultimos_cuatro_digitos(app, http, openapi_spec):
    poliza = await _vendida(http, SOCIO_A)

    [fila] = (await _listar(http, socioId=SOCIO_A)).json()["items"]

    assert fila["polizaId"] == poliza["polizaId"]
    assert fila["socioId"] == SOCIO_A
    assert fila["estado"] == "EMITIDA"
    assert fila["emitidaEn"] and fila["creadaEn"]
    assert fila["primaTotalMensual"] == poliza["primaTotalMensual"]
    assert fila["cliente"] == {
        "id": poliza["clienteId"],
        "nombre": "Laura María Gómez",
        "tipoDocumento": "CC",
        "ultimosDigitosDocumento": ASISTIDO["numeroDocumento"][-4:],
        "estado": "ACTIVO",
    }
    cuerpo = str(fila)
    for dato in (ASISTIDO["numeroDocumento"], "laura.gomez@example.com", "+573101112233"):
        assert dato not in cuerpo
    _validar(openapi_spec, "PolizaResumen", fila)


async def test_el_nombre_incluye_el_segundo_apellido_y_omite_lo_que_falta(app, http):
    await _vendida(
        http, SOCIO_A, segundoNombre=None, segundoApellido="Pérez", primerApellido="Restrepo"
    )

    [fila] = (await _listar(http, socioId=SOCIO_A)).json()["items"]

    assert fila["cliente"]["nombre"] == "Laura Restrepo Pérez"


async def test_sin_filtro_de_socio_las_filas_no_traen_al_cliente(app, http):
    await _vendida(http, SOCIO_A)

    [fila] = (await _listar(http)).json()["items"]

    assert fila["cliente"] is None
    assert fila["socioId"] == SOCIO_A


async def test_se_combina_con_estado_orden_y_paginacion(app, http):
    primera = await _vendida(http, SOCIO_A)
    segunda = await _vendida(http, SOCIO_A, **_otro("80111222", "dos@example.com"))
    tercera = await _vendida(http, SOCIO_A, **_otro("80333444", "tres@example.com"))

    pagina = (await _listar(http, socioId=SOCIO_A, orden="-creadaEn", limite=2)).json()
    resto = (
        await _listar(
            http, socioId=SOCIO_A, orden="-creadaEn", limite=2, cursor=pagina["siguienteCursor"]
        )
    ).json()
    emitidas = (await _listar(http, socioId=SOCIO_A, estado="EMITIDA")).json()
    pendientes = (await _listar(http, socioId=SOCIO_A, estado="PENDIENTE")).json()

    assert [p["polizaId"] for p in pagina["items"]] == [tercera["polizaId"], segunda["polizaId"]]
    assert [p["polizaId"] for p in resto["items"]] == [primera["polizaId"]]
    assert resto["siguienteCursor"] is None
    assert len(emitidas["items"]) == 3
    assert pendientes["items"] == []
    assert all(p["cliente"] for p in pagina["items"] + resto["items"])


@pytest.mark.parametrize("socio", ["", "x" * 129])
async def test_un_socio_vacio_o_demasiado_largo_es_400(app, http, socio):
    respuesta = await _listar(http, socioId=socio)

    assert respuesta.status_code == 400


# --- repositorios: la misma regla en memoria y en SQLite ---


async def test_el_filtro_en_memoria_tambien_separa_por_socio(app, http):
    """El repositorio en memoria aplica el mismo filtro que SQLite (el de las pruebas rápidas)."""
    poliza = await _vendida(http, SOCIO_A)
    guardada = await app.state.polizas._polizas.obtener(poliza["polizaId"])
    memoria = InMemoryPolizaRepository()
    await memoria.crear(guardada)
    ahora = datetime.now(UTC)

    de_a = await memoria.listar(FiltroPolizas(socio_id=SOCIO_A), ahora)
    de_b = await memoria.listar(FiltroPolizas(socio_id=SOCIO_B), ahora)
    todas = await memoria.listar(FiltroPolizas(), ahora)

    assert [p.id for p in de_a.items] == [guardada.id]
    assert de_b.items == ()
    assert [p.id for p in todas.items] == [guardada.id]
    assert guardada.estado is EstadoPoliza.EMITIDA


async def test_obtener_varios_devuelve_solo_los_que_existen_y_sin_repetir(app, http):
    poliza = await _vendida(http, SOCIO_A)
    repo_sqlite = app.state.clientes
    memoria = InMemoryClienteRepository()
    cliente = Cliente(
        identity_ref="ref-1",
        tipo_documento=TipoDocumento.CC,
        numero_documento="1234567",
        primer_nombre="Ana",
        primer_apellido="Ríos",
        fecha_nacimiento=date(1990, 1, 1),
        email="ana@example.com",
        telefono="+573001112233",
    )
    memoria._by_id[cliente.id] = cliente

    de_sqlite = await repo_sqlite.obtener_varios([poliza["clienteId"], poliza["clienteId"], "no"])
    de_memoria = await memoria.obtener_varios([cliente.id, cliente.id, "no"])

    assert list(de_sqlite) == [poliza["clienteId"]]
    assert list(de_memoria) == [cliente.id]
    assert await repo_sqlite.obtener_varios([]) == {}
