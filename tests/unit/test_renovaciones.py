"""BITS-387: renovación anual. El mismo servicio contra el repositorio en memoria y SQLite."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from app.adapters.memory import (
    InMemoryDispositivoRepository,
    InMemoryEventPublisher,
    InMemoryPolizaRepository,
)
from app.adapters.sqlite import SqliteDatabase, SqlitePolizaRepository
from app.avisos import TEXTOS, Aviso, AvisoService
from app.domain import (
    CoberturaContratada,
    CodigoCobertura,
    DatosCredito,
    DecisionRenovacion,
    Dispositivo,
    EstadoPoliza,
    EstadoRenovacion,
    OrigenDatos,
    PlataformaDispositivo,
    PlazoDeRenovacionVencido,
    Poliza,
    PolizaNoEncontrada,
    SinPropuestaDeRenovacion,
    Vigencia,
    escalar_coberturas,
    prima_total,
    propuesta_de_renovacion,
)
from app.renovaciones import RenovacionService

IPC = Decimal("0.05")
EMITIDA_EN = datetime(2026, 11, 13, 17, 0, tzinfo=UTC)
ANIVERSARIO = datetime(2027, 11, 13, 17, 0, tzinfo=UTC)
CLIENTE = "cli-1"


def _hoy(dias_antes_del_aniversario: int) -> datetime:
    return ANIVERSARIO - timedelta(days=dias_antes_del_aniversario)


def _poliza(**cambios) -> Poliza:
    base = {
        "cliente_id": CLIENTE,
        "cotizacion_creada_en": EMITIDA_EN,
        "solicitud_vigente_hasta": EMITIDA_EN + timedelta(days=1),
        "coberturas": (
            CoberturaContratada(
                CodigoCobertura.VIDA,
                True,
                Decimal("140000000"),
                Decimal("24696"),
                Decimal("0"),
                Decimal("24696"),
            ),
            CoberturaContratada(
                CodigoCobertura.HOGAR,
                False,
                Decimal("200000000"),
                Decimal("14850"),
                Decimal("2822"),
                Decimal("17672"),
            ),
        ),
        "datos_credito": DatosCredito(
            Decimal("150000000"), 240, "Bancolombia", Decimal("140000000")
        ),
        "origen_datos": OrigenDatos.CONFIRMADO,
        "tasa": Decimal("0.196"),
        "version_tarifa": 2,
        "cobertura_meses": 240,
        "edad": 38,
        "documentos_aceptados": (),
        "estado": EstadoPoliza.EMITIDA,
        "numero": "SOL-VH-2026-AAAAAA",
        "emitida_en": EMITIDA_EN,
        "vigencia": Vigencia(EMITIDA_EN, ANIVERSARIO),
        "creada_en": EMITIDA_EN,
    }
    return Poliza(**{**base, **cambios})


class _Entorno:
    def __init__(self, repo, eventos, servicio, avisos) -> None:
        self.repo = repo
        self.eventos = eventos
        self.servicio = servicio
        self.avisos = avisos

    async def guardar(self, **cambios) -> Poliza:
        poliza = _poliza(**cambios)
        await self.repo.crear(poliza)
        return poliza

    async def leer(self, poliza: Poliza) -> Poliza:
        leida = await self.repo.obtener(poliza.id)
        assert leida is not None
        return leida

    def avisos_enviados(self) -> list[str]:
        return [e.datos["titulo"] for e in self.eventos.events]


@pytest_asyncio.fixture(params=["memoria", "sqlite"])
async def e(request, tmp_path) -> _Entorno:
    if request.param == "memoria":
        repo = InMemoryPolizaRepository()
    else:
        db = SqliteDatabase(str(tmp_path / "renovaciones.db"))
        await db.init()
        repo = SqlitePolizaRepository(db)
    dispositivos = InMemoryDispositivoRepository()
    ahora = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    await dispositivos.registrar(
        Dispositivo(
            cliente_id=CLIENTE,
            token="token-aaaaaaaa",
            plataforma=PlataformaDispositivo.ANDROID,
            version_app="1.0.0",
            registrado_en=ahora,
            ultimo_visto_en=ahora,
        ),
        5,
    )
    eventos = InMemoryEventPublisher()
    avisos = AvisoService(dispositivos, eventos)
    return _Entorno(repo, eventos, RenovacionService(repo, avisos, ipc=IPC), avisos)


# --- la prima nueva ---


def test_la_prima_nueva_es_la_actual_por_uno_mas_ipc_en_pesos_enteros():
    nuevas = escalar_coberturas(_poliza().coberturas, IPC)

    vida, hogar = nuevas
    assert vida.prima_total == Decimal("25931")  # 24.696 × 1,05 = 25.930,8
    assert (hogar.prima_neta, hogar.iva, hogar.prima_total) == (
        Decimal("15593"),
        Decimal("2963"),
        Decimal("18556"),
    )
    assert hogar.suma_asegurada == Decimal("200000000")  # la suma asegurada no cambia
    assert prima_total(nuevas) == Decimal("44487")


def test_la_propuesta_no_existe_hasta_que_se_avisa_el_vencimiento():
    poliza = _poliza()
    assert propuesta_de_renovacion(poliza, IPC) is None


# --- pasada 1: 45 días antes ---


async def test_a_45_dias_avisa_el_vencimiento_y_la_propuesta_aparece(e):
    poliza = await e.guardar()

    resumen = await e.servicio.ejecutar(_hoy(45))

    assert resumen.avisos_vencimiento == 1
    assert e.avisos_enviados() == [TEXTOS[Aviso.RENOVACION_VENCIMIENTO][0]]
    leida = await e.leer(poliza)
    assert leida.renovacion.aviso_vencimiento_en is not None
    propuesta = propuesta_de_renovacion(leida, IPC)
    assert propuesta.estado is EstadoRenovacion.SIN_RESPUESTA
    assert propuesta.prima_actual == Decimal("42368")  # no se guarda: se calcula al leer
    assert propuesta.prima_nueva == Decimal("44487")
    assert propuesta.fecha_renovacion == ANIVERSARIO
    assert propuesta.decidir_hasta == ANIVERSARIO - timedelta(days=30)
    assert leida.prima_total_mensual == Decimal("42368")  # la prima guardada no cambió


async def test_antes_de_los_45_dias_no_avisa(e):
    await e.guardar()

    resumen = await e.servicio.ejecutar(_hoy(46))

    assert resumen.avisos_vencimiento == 0
    assert e.eventos.events == []


async def test_correr_dos_veces_el_mismo_dia_no_repite_el_aviso_de_vencimiento(e):
    await e.guardar()

    await e.servicio.ejecutar(_hoy(45))
    segunda = await e.servicio.ejecutar(_hoy(45))

    assert segunda.avisos_vencimiento == 0
    assert len(e.eventos.events) == 1


async def test_si_el_aviso_falla_se_deshace_la_marca_y_se_reintenta(e, monkeypatch):
    poliza = await e.guardar()

    async def falla(*_a, **_k):
        raise RuntimeError("pub/sub caído")

    with monkeypatch.context() as m:
        m.setattr(e.avisos, "avisar", falla)
        resumen = await e.servicio.ejecutar(_hoy(45))
    assert resumen.avisos_vencimiento == 0
    assert (await e.leer(poliza)).renovacion.aviso_vencimiento_en is None

    reintento = await e.servicio.ejecutar(_hoy(44))
    assert reintento.avisos_vencimiento == 1


# --- la decisión del cliente ---


async def test_sin_propuesta_no_se_puede_decidir(e):
    poliza = await e.guardar()

    with pytest.raises(SinPropuestaDeRenovacion):
        await e.servicio.decidir(CLIENTE, poliza.id, DecisionRenovacion.RECHAZAR)


async def test_una_poliza_ajena_o_inexistente_no_se_encuentra(e):
    poliza = await e.guardar()

    with pytest.raises(PolizaNoEncontrada):
        await e.servicio.decidir("otro-cliente", poliza.id, DecisionRenovacion.ACEPTAR)
    with pytest.raises(PolizaNoEncontrada):
        await e.servicio.decidir(CLIENTE, "no-existe", DecisionRenovacion.ACEPTAR)


async def test_el_cliente_puede_cambiar_de_decision_antes_de_que_se_cierre(e):
    poliza = await e.guardar()
    await e.servicio.ejecutar(_hoy(45))

    await e.servicio.decidir(CLIENTE, poliza.id, DecisionRenovacion.RECHAZAR)
    decidida = await e.servicio.decidir(CLIENTE, poliza.id, DecisionRenovacion.ACEPTAR)

    assert decidida.renovacion.estado is EstadoRenovacion.ACEPTADA
    assert (await e.leer(poliza)).renovacion.decision is DecisionRenovacion.ACEPTAR


async def test_a_los_30_dias_la_decision_se_cierra(e):
    poliza = await e.guardar()
    await e.servicio.ejecutar(_hoy(45))
    await e.servicio.decidir(CLIENTE, poliza.id, DecisionRenovacion.ACEPTAR)

    await e.servicio.ejecutar(_hoy(30))

    with pytest.raises(PlazoDeRenovacionVencido):
        await e.servicio.decidir(CLIENTE, poliza.id, DecisionRenovacion.RECHAZAR)
    assert (await e.leer(poliza)).renovacion.decision_cerrada_en is not None


async def test_el_plazo_tambien_se_vence_por_fecha_si_el_trabajo_no_corrio(e):
    poliza = await e.guardar()
    await e.servicio.ejecutar(_hoy(45))
    reloj = RenovacionService(e.repo, e.avisos, ipc=IPC, ahora=lambda: _hoy(30))

    with pytest.raises(PlazoDeRenovacionVencido):
        await reloj.decidir(CLIENTE, poliza.id, DecisionRenovacion.RECHAZAR)


# --- pasada 2: 30 días antes ---


async def test_a_30_dias_registra_el_aviso_al_banco_de_la_poliza_rechazada(e):
    poliza = await e.guardar()
    await e.servicio.ejecutar(_hoy(45))
    await e.servicio.decidir(CLIENTE, poliza.id, DecisionRenovacion.RECHAZAR)

    resumen = await e.servicio.ejecutar(_hoy(30))

    assert resumen.avisos_banco == 1
    banco = (await e.leer(poliza)).renovacion.aviso_banco
    assert banco.entidad_acreedora == "Bancolombia"
    assert banco.tipo == "NO_RENOVACION"
    assert banco.fecha_efectiva == ANIVERSARIO
    assert banco.registrado_en == _hoy(30)
    assert banco.canal == "SIMULADO"


async def test_el_aviso_al_banco_no_se_registra_dos_veces(e):
    poliza = await e.guardar()
    await e.servicio.ejecutar(_hoy(45))
    await e.servicio.decidir(CLIENTE, poliza.id, DecisionRenovacion.RECHAZAR)

    await e.servicio.ejecutar(_hoy(30))
    primero = (await e.leer(poliza)).renovacion.aviso_banco
    segunda = await e.servicio.ejecutar(_hoy(30))
    despues = await e.servicio.ejecutar(_hoy(20))

    assert segunda.avisos_banco == despues.avisos_banco == 0
    assert (await e.leer(poliza)).renovacion.aviso_banco == primero


async def test_a_30_dias_la_poliza_aceptada_no_genera_aviso_al_banco(e):
    poliza = await e.guardar()
    await e.servicio.ejecutar(_hoy(45))
    await e.servicio.decidir(CLIENTE, poliza.id, DecisionRenovacion.ACEPTAR)

    resumen = await e.servicio.ejecutar(_hoy(30))

    assert resumen.avisos_banco == 0
    leida = await e.leer(poliza)
    assert leida.renovacion.aviso_banco is None
    assert leida.renovacion.decision_cerrada_en is not None


# --- pasada 3: el aniversario ---


async def test_en_el_aniversario_renueva_con_la_prima_nueva_y_un_ano_mas(e):
    poliza = await e.guardar()
    await e.servicio.ejecutar(_hoy(45))
    await e.servicio.decidir(CLIENTE, poliza.id, DecisionRenovacion.ACEPTAR)
    await e.servicio.ejecutar(_hoy(30))

    resumen = await e.servicio.ejecutar(_hoy(0))

    assert resumen.renovadas == 1
    leida = await e.leer(poliza)
    assert leida.estado is EstadoPoliza.EMITIDA
    assert leida.vigencia == Vigencia(ANIVERSARIO, datetime(2028, 11, 13, 17, 0, tzinfo=UTC))
    assert leida.emitida_en == EMITIDA_EN  # la emisión original no se mueve
    assert leida.prima_total_mensual == Decimal("44487")
    [hecha] = leida.renovacion.historial
    assert hecha.vigencia_anterior == Vigencia(EMITIDA_EN, ANIVERSARIO)
    assert hecha.vigencia_nueva == leida.vigencia
    assert (hecha.prima_anterior, hecha.prima_nueva, hecha.ipc) == (
        Decimal("42368"),
        Decimal("44487"),
        IPC,
    )
    # El ciclo siguiente empieza de cero.
    assert leida.renovacion.decision is None
    assert leida.renovacion.aviso_vencimiento_en is None
    assert e.avisos_enviados()[-1] == TEXTOS[Aviso.RENOVACION_RENOVADA][0]


async def test_sin_respuesta_tambien_se_renueva(e):
    poliza = await e.guardar()
    await e.servicio.ejecutar(_hoy(45))

    resumen = await e.servicio.ejecutar(_hoy(0))

    assert resumen.renovadas == 1
    assert (await e.leer(poliza)).vigencia.hasta == datetime(2028, 11, 13, 17, 0, tzinfo=UTC)


async def test_correr_dos_veces_el_aniversario_no_renueva_dos_veces(e):
    poliza = await e.guardar()
    await e.servicio.ejecutar(_hoy(45))

    await e.servicio.ejecutar(_hoy(0))
    segunda = await e.servicio.ejecutar(_hoy(0))

    assert segunda.renovadas == 0
    leida = await e.leer(poliza)
    assert len(leida.renovacion.historial) == 1
    assert leida.prima_total_mensual == Decimal("44487")


async def test_en_el_aniversario_la_poliza_rechazada_queda_desactivada(e):
    poliza = await e.guardar()
    await e.servicio.ejecutar(_hoy(45))
    await e.servicio.decidir(CLIENTE, poliza.id, DecisionRenovacion.RECHAZAR)
    await e.servicio.ejecutar(_hoy(30))

    resumen = await e.servicio.ejecutar(_hoy(0))

    assert resumen.desactivadas == 1
    leida = await e.leer(poliza)
    assert leida.estado is EstadoPoliza.DESACTIVADA
    assert leida.vigencia == Vigencia(EMITIDA_EN, ANIVERSARIO)  # conserva su vigencia
    assert leida.renovacion.desactivada_en == _hoy(0)
    assert leida.renovacion.aviso_banco is not None
    assert e.avisos_enviados()[-1] == TEXTOS[Aviso.RENOVACION_DESACTIVADA][0]
    # Ya no es una póliza emitida: el trabajo no la vuelve a tocar.
    assert (await e.servicio.ejecutar(_hoy(0))).desactivadas == 0
    assert await e.repo.emitidas() == []


async def test_si_el_trabajo_no_corrio_a_los_30_dias_el_aniversario_igual_avisa_al_banco(e):
    poliza = await e.guardar()
    await e.servicio.ejecutar(_hoy(45))
    await e.servicio.decidir(CLIENTE, poliza.id, DecisionRenovacion.RECHAZAR)

    await e.servicio.ejecutar(_hoy(0))

    leida = await e.leer(poliza)
    assert leida.estado is EstadoPoliza.DESACTIVADA
    assert leida.renovacion.aviso_banco is not None


async def test_el_trabajo_renueva_varios_anos_si_se_le_adelanta_la_fecha(e):
    poliza = await e.guardar()

    await e.servicio.ejecutar(_hoy(0))
    await e.servicio.ejecutar(ANIVERSARIO + timedelta(days=366))

    leida = await e.leer(poliza)
    assert len(leida.renovacion.historial) == 2
    assert leida.vigencia.hasta == datetime(2029, 11, 13, 17, 0, tzinfo=UTC)
    assert leida.prima_total_mensual == prima_total(
        escalar_coberturas(escalar_coberturas(_poliza().coberturas, IPC), IPC)
    )


async def test_sin_avisos_configurados_el_trabajo_igual_renueva(e):
    poliza = await e.guardar()
    sin_avisos = RenovacionService(e.repo, None, ipc=IPC)

    resumen = await sin_avisos.ejecutar(_hoy(0))

    assert resumen.renovadas == 1
    assert (await e.leer(poliza)).vigencia.desde == ANIVERSARIO


async def test_las_polizas_que_no_estan_emitidas_no_se_tocan(e):
    pendiente = await e.guardar(estado=EstadoPoliza.PENDIENTE, vigencia=None, numero=None)

    resumen = await e.servicio.ejecutar(_hoy(0))

    assert (resumen.renovadas, resumen.desactivadas, resumen.avisos_vencimiento) == (0, 0, 0)
    assert (await e.leer(pendiente)).estado is EstadoPoliza.PENDIENTE


async def test_el_repositorio_no_renueva_si_la_vigencia_ya_cambio(e):
    poliza = await e.guardar()
    nueva = Vigencia(ANIVERSARIO, datetime(2028, 11, 13, 17, 0, tzinfo=UTC))

    assert (
        await e.repo.renovar(
            poliza.id,
            ANIVERSARIO + timedelta(days=1),
            nueva,
            poliza.coberturas,
            replace(poliza.renovacion),
        )
        is None
    )
    assert (
        await e.repo.desactivar(poliza.id, ANIVERSARIO + timedelta(days=1), poliza.renovacion)
        is None
    )
    assert (await e.leer(poliza)).vigencia == poliza.vigencia
