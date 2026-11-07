"""BITS-383 y BITS-384: estados, plazo, transiciones y repositorios de siniestros."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.adapters.almacen import InMemoryAlmacenEvidencias
from app.adapters.creditos_fake import CreditosFake
from app.adapters.memory import InMemoryIdempotencyStore, InMemorySiniestroRepository
from app.adapters.sqlite import SqliteDatabase, SqliteSiniestroRepository
from app.domain import (
    ArchivoSiniestro,
    BeneficiarioPago,
    ClaseArchivo,
    ClienteNoEncontrado,
    EstadoSiniestro,
    EvidenciaPedida,
    LineaPago,
    Pago,
    SiniestroNoEncontrado,
    SolicitudSiniestroInvalida,
    TipoSiniestro,
    TransicionInvalida,
)
from app.siniestros import ArchivoEntrada, SiniestroService
from tests import imagenes
from tests.unit.test_siniestros import (
    AHORA,
    HOY,
    _Clientes,
    _poliza,
    _Polizas,
    _reporte,
    _servicio,
    _siniestro,
)

FOTO = imagenes.a_bytes(imagenes.sin_tarjeta())
PAGO = Pago(
    AHORA,
    (
        LineaPago(BeneficiarioPago.BANCO, Decimal("140000000"), "Bancolombia, abono al saldo"),
        LineaPago(BeneficiarioPago.CLIENTE, Decimal("40000000.50"), "Cuenta de ahorros …4821"),
    ),
)


def _archivo(id_: str = "a-nuevo", entregado_en: datetime = AHORA) -> ArchivoSiniestro:
    return ArchivoSiniestro(
        id_, ClaseArchivo.DOCUMENTO, f"siniestros/{id_}.jpg", "image/jpeg", entregado_en
    )


# --- plazo ---


def test_la_fecha_limite_es_la_ultima_entrega_mas_un_mes_solo_en_estudio():
    en_estudio = _siniestro(ultima_entrega_en=datetime(2026, 11, 10, 15, tzinfo=UTC))

    assert en_estudio.fecha_limite == datetime(2026, 12, 10, 15, tzinfo=UTC)
    for estado in (
        EstadoSiniestro.FALTA_INFORMACION,
        EstadoSiniestro.PAGADO,
        EstadoSiniestro.RECHAZADO,
    ):
        assert _siniestro(estado=estado).fecha_limite is None


def test_la_fecha_limite_ajusta_el_dia_al_fin_de_un_mes_mas_corto():
    siniestro = _siniestro(ultima_entrega_en=datetime(2026, 1, 31, 9, tzinfo=UTC))

    assert siniestro.fecha_limite == datetime(2026, 2, 28, 9, tzinfo=UTC)


# --- transiciones del dominio ---


def test_pedir_evidencia_detiene_el_reloj_y_guarda_lo_pedido():
    siniestro = _siniestro()

    siniestro.pedir_evidencia("  Falta el recibo de la reparación  ", AHORA)

    assert siniestro.estado is EstadoSiniestro.FALTA_INFORMACION
    assert siniestro.evidencia_pedida == EvidenciaPedida("Falta el recibo de la reparación", AHORA)
    assert siniestro.fecha_limite is None
    assert siniestro.actualizado_en == AHORA


def test_entregar_evidencia_vuelve_a_estudio_y_reinicia_el_plazo():
    siniestro = _siniestro()
    siniestro.pedir_evidencia("Falta algo", AHORA)
    entrega = AHORA + timedelta(days=3)

    siniestro.entregar_evidencia((_archivo(entregado_en=entrega),), entrega)

    assert siniestro.estado is EstadoSiniestro.EN_ESTUDIO
    assert siniestro.ultima_entrega_en == entrega
    assert [a.id for a in siniestro.archivos] == ["a-1", "a-2", "a-nuevo"]
    assert siniestro.archivos[-1].entregado_en == entrega
    assert siniestro.evidencia_pedida is not None  # el ADMIN sigue viendo lo que pidió
    assert siniestro.fecha_limite is not None


def test_pagar_deja_la_orden_y_cierra_el_siniestro():
    siniestro = _siniestro()

    siniestro.pagar(PAGO, AHORA)

    assert siniestro.estado is EstadoSiniestro.PAGADO
    assert siniestro.pago == PAGO
    assert siniestro.fecha_limite is None


def test_rechazar_guarda_el_motivo_que_ve_el_cliente():
    siniestro = _siniestro()

    siniestro.rechazar("  El evento no está cubierto  ", AHORA)

    assert siniestro.estado is EstadoSiniestro.RECHAZADO
    assert siniestro.motivo_rechazo == "El evento no está cubierto"


@pytest.mark.parametrize(
    "estado",
    [EstadoSiniestro.FALTA_INFORMACION, EstadoSiniestro.PAGADO, EstadoSiniestro.RECHAZADO],
)
def test_solo_se_decide_un_siniestro_en_estudio(estado):
    for accion in (
        lambda s: s.pedir_evidencia("x", AHORA),
        lambda s: s.pagar(PAGO, AHORA),
        lambda s: s.rechazar("x", AHORA),
    ):
        siniestro = _siniestro(estado=estado)
        with pytest.raises(TransicionInvalida) as error:
            accion(siniestro)
        assert error.value.estado is estado
        assert error.value.codigo == "ESTADO_NO_PERMITE"
        assert siniestro.estado is estado  # no cambió nada


@pytest.mark.parametrize(
    "estado", [EstadoSiniestro.EN_ESTUDIO, EstadoSiniestro.PAGADO, EstadoSiniestro.RECHAZADO]
)
def test_solo_se_entrega_evidencia_cuando_falta_informacion(estado):
    siniestro = _siniestro(estado=estado)

    with pytest.raises(TransicionInvalida):
        siniestro.entregar_evidencia((_archivo(),), AHORA)

    assert len(siniestro.archivos) == 2


# --- repositorios (memoria y SQLite) ---


@pytest.fixture(params=["memoria", "sqlite"])
async def repositorio(request, tmp_path):
    if request.param == "memoria":
        return InMemorySiniestroRepository()
    db = SqliteDatabase(str(tmp_path / "estados.db"))
    await db.init()
    return SqliteSiniestroRepository(db)


async def test_actualizar_guarda_lo_que_cambio_la_transicion(repositorio):
    siniestro = _siniestro()
    await repositorio.crear(siniestro)
    siniestro.pedir_evidencia("Falta el recibo", AHORA)

    assert await repositorio.actualizar(siniestro, EstadoSiniestro.EN_ESTUDIO) is True

    assert await repositorio.obtener(siniestro.id) == siniestro


async def test_actualizar_guarda_el_pago_con_su_precision(repositorio):
    creado = _siniestro()
    await repositorio.crear(creado)
    siniestro = await repositorio.obtener(creado.id)
    siniestro.pagar(PAGO, AHORA)

    assert await repositorio.actualizar(siniestro, EstadoSiniestro.EN_ESTUDIO) is True

    guardado = await repositorio.obtener(siniestro.id)
    assert guardado.estado is EstadoSiniestro.PAGADO
    assert guardado.pago == PAGO
    assert guardado.pago.lineas[1].valor == Decimal("40000000.50")


async def test_actualizar_no_pisa_a_quien_llego_primero(repositorio):
    siniestro = _siniestro()
    await repositorio.crear(siniestro)
    uno = await repositorio.obtener(siniestro.id)
    otro = await repositorio.obtener(siniestro.id)
    uno.pedir_evidencia("lo pide uno", AHORA)
    otro.rechazar("lo rechaza otro", AHORA)

    primero = await repositorio.actualizar(uno, EstadoSiniestro.EN_ESTUDIO)
    segundo = await repositorio.actualizar(otro, EstadoSiniestro.EN_ESTUDIO)

    assert (primero, segundo) == (True, False)
    guardado = await repositorio.obtener(siniestro.id)
    assert guardado.estado is EstadoSiniestro.FALTA_INFORMACION
    assert guardado.motivo_rechazo is None


async def test_actualizar_un_siniestro_que_no_existe_devuelve_false(repositorio):
    assert await repositorio.actualizar(_siniestro(), EstadoSiniestro.EN_ESTUDIO) is False


async def test_listar_de_cliente_del_mas_reciente_al_mas_antiguo_y_solo_los_suyos(repositorio):
    viejo = _siniestro(numero="SIN-2026-AAAAAA", reportado_en=AHORA - timedelta(days=2))
    nuevo = _siniestro(
        numero="SIN-2026-BBBBBB",
        tipo=TipoSiniestro.INCAPACIDAD_TOTAL,
        reportado_en=AHORA,
    )
    ajeno = _siniestro(numero="SIN-2026-CCCCCC", cliente_id="otro", poliza_id="poliza-9")
    for siniestro in (viejo, nuevo, ajeno):
        await repositorio.crear(siniestro)

    propios = await repositorio.listar_de_cliente("cliente-1")

    assert [s.id for s in propios] == [nuevo.id, viejo.id]
    assert await repositorio.listar_de_cliente("nadie") == []


async def test_el_repositorio_guarda_el_paramétrico_con_su_evento(repositorio):
    evento = {"fecha": "2026-11-18", "municipio": "Bogotá", "magnitud": 6.1}
    siniestro = _siniestro(
        tipo=TipoSiniestro.SISMO_PARAMETRICO,
        automatico=True,
        evento=evento,
        destino_pago=None,
        narracion=None,
        archivos=(),
    )

    await repositorio.crear(siniestro)

    guardado = await repositorio.obtener(siniestro.id)
    assert (guardado.automatico, guardado.evento) == (True, evento)
    assert guardado.tipo.cobertura.value == "HOGAR"


async def test_la_memoria_devuelve_copias():
    repositorio = InMemorySiniestroRepository()
    siniestro = _siniestro()
    await repositorio.crear(siniestro)

    leido = await repositorio.obtener(siniestro.id)
    leido.rechazar("solo en mi copia", AHORA)

    assert (await repositorio.obtener(siniestro.id)).estado is EstadoSiniestro.EN_ESTUDIO
    assert (
        await repositorio.en_curso("poliza-1", siniestro.tipo)
    ).estado is EstadoSiniestro.EN_ESTUDIO


async def test_una_base_creada_antes_de_estas_columnas_se_migra(tmp_path):
    ruta = str(tmp_path / "vieja.db")
    saldo = (
        '{"valor": "1", "entidad": "B", "origen": "POLIZA", '
        '"consultadoEn": "2026-11-10T15:00:00+00:00"}'
    )
    conexion = sqlite3.connect(ruta)
    conexion.executescript(
        f"""
        CREATE TABLE siniestros (
            id TEXT PRIMARY KEY, numero TEXT NOT NULL, cliente_id TEXT NOT NULL,
            poliza_id TEXT NOT NULL, tipo TEXT NOT NULL, estado TEXT NOT NULL,
            fecha_ocurrencia TEXT NOT NULL, narracion TEXT, archivos TEXT NOT NULL DEFAULT '[]',
            saldo_credito TEXT NOT NULL, destino_pago TEXT, reportado_en TEXT NOT NULL,
            ultima_entrega_en TEXT NOT NULL, actualizado_en TEXT NOT NULL
        );
        INSERT INTO siniestros VALUES (
            'viejo', 'SIN-2026-VIEJO1', 'cliente-1', 'poliza-1', 'INCAPACIDAD_TOTAL', 'EN_ESTUDIO',
            '2026-11-10', NULL, '[]',
            '{saldo}',
            NULL, '2026-11-10T15:00:00+00:00', '2026-11-10T15:00:00+00:00',
            '2026-11-10T15:00:00+00:00'
        );
        """
    )
    conexion.commit()
    conexion.close()
    db = SqliteDatabase(ruta)

    await db.init()

    leido = await SqliteSiniestroRepository(db).obtener("viejo")
    assert leido is not None
    assert (leido.evidencia_pedida, leido.pago, leido.automatico, leido.evento) == (
        None,
        None,
        False,
        None,
    )


# --- servicio ---


def _servicio_con(siniestro, *, clientes=None):
    repositorio = InMemorySiniestroRepository()
    servicio = SiniestroService(
        clientes or _Clientes(),
        _Polizas(),
        repositorio,
        InMemoryAlmacenEvidencias(),
        CreditosFake(),
        InMemoryIdempotencyStore(),
        max_bytes=3 * 1024 * 1024,
        max_pixeles=40_000_000,
        ahora=lambda: AHORA + timedelta(days=1),
        hoy=lambda: HOY,
    )
    return servicio, repositorio


async def test_listar_exige_que_el_cliente_exista():
    servicio, _ = _servicio_con(None, clientes=_Clientes(existe=False))

    with pytest.raises(ClienteNoEncontrado):
        await servicio.listar("cliente-1")


async def test_obtener_un_siniestro_ajeno_o_inexistente_es_no_encontrado():
    siniestro = _siniestro()
    servicio, repositorio = _servicio_con(siniestro)
    await repositorio.crear(siniestro)

    with pytest.raises(SiniestroNoEncontrado):
        await servicio.obtener("otro-cliente", siniestro.id)
    with pytest.raises(SiniestroNoEncontrado):
        await servicio.obtener("cliente-1", "no-existe")
    assert (await servicio.obtener("cliente-1", siniestro.id)).id == siniestro.id


async def _en_falta_informacion():
    siniestro = _siniestro()
    servicio, repositorio = _servicio_con(siniestro)
    await repositorio.crear(siniestro)
    pedido = await repositorio.obtener(siniestro.id)
    pedido.pedir_evidencia("Falta el recibo", AHORA)
    await repositorio.actualizar(pedido, EstadoSiniestro.EN_ESTUDIO)
    return servicio, repositorio, siniestro.id


async def test_entregar_evidencias_vuelve_a_estudio_con_la_fecha_de_la_entrega():
    servicio, repositorio, siniestro_id = await _en_falta_informacion()

    siniestro = await servicio.entregar_evidencias(
        "cliente-1", siniestro_id, (ArchivoEntrada(FOTO, ClaseArchivo.DOCUMENTO),)
    )

    entrega = AHORA + timedelta(days=1)
    assert siniestro.estado is EstadoSiniestro.EN_ESTUDIO
    assert siniestro.ultima_entrega_en == entrega
    assert siniestro.archivos[-1].entregado_en == entrega
    assert siniestro.archivos[-1].referencia.startswith("siniestros/cliente-1/")
    assert (await repositorio.obtener(siniestro_id)).estado is EstadoSiniestro.EN_ESTUDIO


async def test_entregar_evidencias_exige_el_estado_la_evidencia_y_el_tope():
    siniestro = _siniestro()
    servicio, repositorio = _servicio_con(siniestro)
    await repositorio.crear(siniestro)

    with pytest.raises(TransicionInvalida):
        await servicio.entregar_evidencias(
            "cliente-1", siniestro.id, (ArchivoEntrada(FOTO, ClaseArchivo.FOTO),)
        )

    servicio, _, siniestro_id = await _en_falta_informacion()
    with pytest.raises(SolicitudSiniestroInvalida):
        await servicio.entregar_evidencias("cliente-1", siniestro_id, ())
    with pytest.raises(SolicitudSiniestroInvalida):
        await servicio.entregar_evidencias(
            "cliente-1", siniestro_id, (ArchivoEntrada(FOTO, ClaseArchivo.FOTO),) * 11
        )


async def test_una_entrega_que_pierde_la_carrera_falla_sin_pisar_a_la_otra():
    servicio, repositorio, siniestro_id = await _en_falta_informacion()
    original = repositorio.actualizar

    async def alguien_llega_primero(siniestro, estado_anterior):
        otra = await repositorio.obtener(siniestro_id)
        otra.entregar_evidencia((), AHORA)
        await original(otra, estado_anterior)
        return await original(siniestro, estado_anterior)

    repositorio.actualizar = alguien_llega_primero

    with pytest.raises(TransicionInvalida) as error:
        await servicio.entregar_evidencias(
            "cliente-1", siniestro_id, (ArchivoEntrada(FOTO, ClaseArchivo.DOCUMENTO),)
        )

    assert error.value.estado is EstadoSiniestro.EN_ESTUDIO


async def test_la_llave_de_entrega_es_por_siniestro_y_repetirla_devuelve_el_estado_actual():
    servicio, repositorio, siniestro_id = await _en_falta_informacion()
    archivos = (ArchivoEntrada(FOTO, ClaseArchivo.DOCUMENTO),)

    primera = await servicio.entregar_evidencias("cliente-1", siniestro_id, archivos, "llave")
    repetida = await servicio.entregar_evidencias("cliente-1", siniestro_id, archivos, "llave")

    assert repetida.id == primera.id
    assert len(repetida.archivos) == len(primera.archivos)  # no se guardó otra vez


async def test_el_cliente_no_puede_reportar_un_siniestro_parametrico():
    poliza = _poliza()
    servicio, *_ = _servicio(poliza)
    reporte = _reporte(
        poliza,
        tipo=TipoSiniestro.SISMO_PARAMETRICO,
        narracion="x",
        destino_pago={"tipo": "CHEQUE"},
        archivos=(ArchivoEntrada(FOTO, ClaseArchivo.FOTO),),
    )

    with pytest.raises(SolicitudSiniestroInvalida) as error:
        await servicio.reportar("cliente-1", reporte)

    assert error.value.campo == "tipo"
