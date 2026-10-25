"""BITS-101: cliente HTTP de Cotización, despacho de eventos, fábrica y reglas puras del dominio."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest

from app.adapters.cotizacion_http import CotizacionHttp, cotizacion_desde_dict
from app.adapters.factory import build_polizas
from app.adapters.memory import InMemoryEvidenciaRepository, InMemoryPolizaRepository
from app.adapters.sqlite import SqliteEvidenciaRepository, SqlitePolizaRepository
from app.config import Settings
from app.consumidores import despachar_evento
from app.domain import (
    CodigoCobertura,
    CotizacionNoDisponible,
    DomainEvent,
    FueraDeRango,
    OrigenDatos,
    edad_en,
    generar_numero_poliza,
    sumar_meses,
    validar_rango_de_edad,
)

RESPUESTA = {
    "estado": "VIGENTE",
    "edad": 38,
    "datosCredito": {
        "valorCredito": 150000000,
        "plazoRestanteMeses": 240,
        "entidadAcreedora": "Bancolombia",
        "saldoInsoluto": 140000000.0,
    },
    "origenDatos": "CONFIRMADO",
    "tasa": 0.196,
    "versionTarifa": 2,
    "coberturaMeses": 240,
    "coberturas": [
        {
            "codigo": "VIDA",
            "obligatoria": True,
            "sumaAsegurada": 140000000,
            "primaNeta": 24696,
            "iva": 0,
            "primaTotal": 24696,
        },
        {
            "codigo": "HOGAR",
            "obligatoria": False,
            "sumaAsegurada": 150000000,
            "primaNeta": 14850,
            "iva": 2822,
            "primaTotal": 17672,
        },
    ],
    "vigenciaCotizacion": {
        "desde": "2026-10-24T12:00:00.123456Z",
        "hasta": "2026-11-23T12:00:00.123456Z",
    },
    "creadaEn": "2026-10-24T12:00:00.123456Z",
}


def _cliente(manejador, **kwargs) -> CotizacionHttp:
    http = httpx.AsyncClient(
        base_url="http://cotizacion", transport=httpx.MockTransport(manejador), timeout=1
    )
    return CotizacionHttp("http://cotizacion", client=http, **kwargs)


async def test_lee_la_cotizacion_activa_del_cliente():
    pedidos = []

    def manejador(pedido: httpx.Request) -> httpx.Response:
        pedidos.append(str(pedido.url))
        return httpx.Response(200, json=RESPUESTA)

    cot = await _cliente(manejador).activa("cli-1")

    assert pedidos == ["http://cotizacion/v1/clientes/cli-1/cotizacion-activa"]
    assert cot.origen_datos is OrigenDatos.CONFIRMADO
    assert cot.datos_credito.saldo_insoluto == Decimal("140000000.0")
    assert cot.tasa == Decimal("0.196")
    assert [c.codigo for c in cot.coberturas] == [CodigoCobertura.VIDA, CodigoCobertura.HOGAR]
    assert cot.coberturas[1].prima_total == Decimal("17672")
    assert cot.creada_en == datetime(2026, 10, 24, 12, 0, 0, 123456, tzinfo=UTC)
    assert cot.vigencia.hasta > cot.vigencia.desde


async def test_sin_cotizacion_es_none():
    cliente = _cliente(lambda _p: httpx.Response(404, json={"title": "no hay"}))

    assert await cliente.activa("cli-1") is None


async def test_un_5xx_se_reintenta_una_vez_y_luego_es_503():
    llamadas = []

    def manejador(_p):
        llamadas.append(1)
        return httpx.Response(503)

    with pytest.raises(CotizacionNoDisponible):
        await _cliente(manejador).activa("cli-1")

    assert len(llamadas) == 2


async def test_un_5xx_seguido_de_un_200_recupera():
    respuestas = iter([httpx.Response(500), httpx.Response(200, json=RESPUESTA)])

    cot = await _cliente(lambda _p: next(respuestas)).activa("cli-1")

    assert cot.edad == 38


async def test_un_corte_de_red_es_503():
    def manejador(_p):
        raise httpx.ConnectError("sin red")

    with pytest.raises(CotizacionNoDisponible):
        await _cliente(manejador).activa("cli-1")


async def test_un_4xx_inesperado_no_se_reintenta():
    llamadas = []

    def manejador(_p):
        llamadas.append(1)
        return httpx.Response(400)

    with pytest.raises(CotizacionNoDisponible):
        await _cliente(manejador).activa("cli-1")

    assert len(llamadas) == 1


async def test_cierra_el_cliente_http():
    cliente = _cliente(lambda _p: httpx.Response(200, json=RESPUESTA))

    await cliente.aclose()


def test_parsea_montos_decimales_sin_perder_exactitud():
    cot = cotizacion_desde_dict(RESPUESTA)

    assert cot.datos_credito.valor_credito == Decimal("150000000")
    assert cot.coberturas[0].prima_neta == Decimal("24696")


# --- eventos ---


class _PolizasEspia:
    def __init__(self) -> None:
        self.llamadas = []

    async def descartar_pendientes(self, cliente_id, creada_en):
        self.llamadas.append((cliente_id, creada_en))


async def test_cotizacion_creada_descarta_las_pendientes_de_otras_cotizaciones():
    polizas = _PolizasEspia()
    evento = DomainEvent(
        "CotizacionCreada", {"clienteId": "cli-1", "creadaEn": "2026-10-24T12:00:00+00:00"}
    )

    await despachar_evento(polizas, evento)

    assert polizas.llamadas == [("cli-1", datetime(2026, 10, 24, 12, 0, tzinfo=UTC))]


async def test_cotizacion_eliminada_descarta_todas_las_pendientes():
    polizas = _PolizasEspia()

    await despachar_evento(polizas, DomainEvent("CotizacionEliminada", {"clienteId": "cli-1"}))

    assert polizas.llamadas == [("cli-1", None)]


async def test_un_tipo_desconocido_se_ignora():
    polizas = _PolizasEspia()

    await despachar_evento(polizas, DomainEvent("Otro", {"clienteId": "cli-1"}))

    assert polizas.llamadas == []


# --- fábrica ---


def test_la_fabrica_arma_los_adaptadores_en_memoria():
    evidencias = InMemoryEvidenciaRepository()

    repo, cotizaciones, documentos, cerrables = build_polizas(
        Settings(repository_backend="memory"), evidencias
    )

    assert isinstance(repo, InMemoryPolizaRepository)
    assert cerrables == []
    assert cotizaciones is not None and documentos is not None


def test_la_fabrica_arma_sqlite_y_el_cliente_http(tmp_path):
    settings = Settings(
        repository_backend="sqlite",
        database_path=str(tmp_path / "x.db"),
        cotizacion_backend="http",
    )

    repo, cotizaciones, _, cerrables = build_polizas(
        settings, SqliteEvidenciaRepository(SqlitePolizaRepository(None)._db)
    )

    assert isinstance(repo, SqlitePolizaRepository)
    assert isinstance(cotizaciones, CotizacionHttp)
    assert cerrables == [cotizaciones]


@pytest.mark.parametrize(
    "cambio",
    [
        {"repository_backend": "otro"},
        {"cotizacion_backend": "otro"},
        {"documento_backend": "otro"},
    ],
)
def test_la_fabrica_rechaza_backends_desconocidos(cambio):
    settings = Settings(**{"repository_backend": "memory", **cambio})
    evidencias = InMemoryEvidenciaRepository()

    with pytest.raises((ValueError, AssertionError)):
        build_polizas(settings, evidencias)


# --- reglas puras ---


@pytest.mark.parametrize(
    ("nacimiento", "hoy", "edad"),
    [
        (date(1990, 10, 24), date(2026, 10, 24), 36),  # cumple hoy
        (date(1990, 10, 25), date(2026, 10, 24), 35),  # cumple mañana
        (date(1990, 2, 28), date(2026, 3, 1), 36),
    ],
)
def test_edad_en(nacimiento, hoy, edad):
    assert edad_en(nacimiento, hoy) == edad


@pytest.mark.parametrize(
    ("edad", "plazo"),
    [(18, 12), (65, 120), (40, 420), (30, 540)],  # 65+10 = 75 justo; 40+35 = 75 justo
)
def test_edades_y_plazos_dentro_de_rango(edad, plazo):
    validar_rango_de_edad(edad, plazo)


@pytest.mark.parametrize(("edad", "plazo"), [(17, 12), (66, 12), (65, 121), (40, 421), (30, 541)])
def test_edades_y_plazos_fuera_de_rango(edad, plazo):
    with pytest.raises(FueraDeRango):
        validar_rango_de_edad(edad, plazo)


@pytest.mark.parametrize(
    ("inicio", "meses", "esperado"),
    [
        (datetime(2026, 10, 24, tzinfo=UTC), 240, datetime(2046, 10, 24, tzinfo=UTC)),
        (datetime(2026, 1, 31, tzinfo=UTC), 1, datetime(2026, 2, 28, tzinfo=UTC)),
        (datetime(2024, 1, 31, tzinfo=UTC), 1, datetime(2024, 2, 29, tzinfo=UTC)),
        (datetime(2026, 11, 30, tzinfo=UTC), 1, datetime(2026, 12, 30, tzinfo=UTC)),
        (datetime(2026, 12, 15, tzinfo=UTC), 1, datetime(2027, 1, 15, tzinfo=UTC)),
        (datetime(2026, 3, 31, tzinfo=UTC), 12, datetime(2027, 3, 31, tzinfo=UTC)),
    ],
)
def test_sumar_meses(inicio, meses, esperado):
    assert sumar_meses(inicio, meses) == esperado


def test_el_numero_de_poliza_usa_el_anio_y_el_alfabeto_sin_ambiguos():
    letras = iter("0123456789")

    numero = generar_numero_poliza(datetime(2026, 10, 24, tzinfo=UTC), lambda _a: next(letras))

    assert numero == "SOL-VH-2026-012345"
