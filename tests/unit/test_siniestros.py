"""BITS-381: servicio de siniestros, repositorios, adaptador hacia svc-perfilamiento y fábrica."""

from __future__ import annotations

import json
import logging
import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from app.adapters import factory
from app.adapters.almacen import InMemoryAlmacenEvidencias
from app.adapters.creditos_fake import CreditosFake
from app.adapters.memory import (
    InMemoryDispositivoRepository,
    InMemoryEventPublisher,
    InMemoryIdempotencyStore,
    InMemorySiniestroRepository,
)
from app.adapters.perfilamiento_http import PerfilamientoHttp
from app.adapters.polizas_fake import cotizacion_de_ejemplo
from app.adapters.sqlite import SqliteDatabase, SqliteSiniestroRepository
from app.avisos import TEXTOS, Aviso, AvisoService, ruta_seguro
from app.config import Settings
from app.domain import (
    ArchivoSiniestro,
    ClaseArchivo,
    ClienteNoEncontrado,
    CodigoCobertura,
    CreditosReportados,
    Dispositivo,
    EstadoPoliza,
    EstadoSiniestro,
    FueraDeVigencia,
    HipotecaReportada,
    NumeroSiniestroDuplicado,
    OrigenSaldo,
    PlataformaDispositivo,
    Poliza,
    PolizaNoEncontrada,
    SaldoCredito,
    Siniestro,
    SiniestroEnCurso,
    SolicitudSiniestroInvalida,
    TipoNoCubierto,
    TipoSiniestro,
    Vigencia,
    generar_numero_siniestro,
)
from app.siniestros import (
    ArchivoEntrada,
    ReporteSiniestro,
    SiniestroService,
    hoy_en_colombia,
)
from tests import imagenes

FOTO = imagenes.a_bytes(imagenes.sin_tarjeta())
AHORA = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
HOY = date(2026, 11, 10)


# --- armado ---


class _Clientes:
    """Solo importa que el cliente exista."""

    def __init__(self, existe: bool = True) -> None:
        self._existe = existe

    async def obtener(self, cliente_id: str):
        return object() if self._existe else None


class _Polizas:
    def __init__(self, *polizas: Poliza) -> None:
        self._por_id = {p.id: p for p in polizas}

    async def obtener(self, poliza_id: str) -> Poliza | None:
        return self._por_id.get(poliza_id)


def _poliza(
    *,
    estado: EstadoPoliza = EstadoPoliza.EMITIDA,
    cliente_id: str = "cliente-1",
    emitida_en: datetime = AHORA - timedelta(days=30),
    hasta: datetime = AHORA + timedelta(days=300),
    coberturas: tuple[CodigoCobertura, ...] = (CodigoCobertura.VIDA, CodigoCobertura.HOGAR),
) -> Poliza:
    cotizacion = cotizacion_de_ejemplo()
    return Poliza(
        cliente_id=cliente_id,
        cotizacion_creada_en=cotizacion.creada_en,
        solicitud_vigente_hasta=cotizacion.creada_en + timedelta(days=1),
        coberturas=tuple(c for c in cotizacion.coberturas if c.codigo in coberturas),
        datos_credito=cotizacion.datos_credito,
        origen_datos=cotizacion.origen_datos,
        tasa=cotizacion.tasa,
        version_tarifa=cotizacion.version_tarifa,
        cobertura_meses=cotizacion.cobertura_meses,
        edad=cotizacion.edad,
        documentos_aceptados=(),
        estado=estado,
        emitida_en=emitida_en,
        vigencia=Vigencia(emitida_en, hasta),
    )


def _servicio(
    poliza: Poliza | None = None,
    *,
    clientes: _Clientes | None = None,
    creditos: CreditosFake | None = None,
    aleatorio=None,
) -> tuple[SiniestroService, InMemorySiniestroRepository, InMemoryAlmacenEvidencias, CreditosFake]:
    repositorio = InMemorySiniestroRepository()
    almacen = InMemoryAlmacenEvidencias()
    creditos = creditos or CreditosFake()
    polizas = _Polizas(poliza) if poliza else _Polizas()
    servicio = SiniestroService(
        clientes or _Clientes(),
        polizas,
        repositorio,
        almacen,
        creditos,
        InMemoryIdempotencyStore(),
        max_bytes=3 * 1024 * 1024,
        max_pixeles=40_000_000,
        ahora=lambda: AHORA,
        hoy=lambda: HOY,
        **({"aleatorio": aleatorio} if aleatorio else {}),
    )
    return servicio, repositorio, almacen, creditos


def _reporte(poliza: Poliza, **cambios) -> ReporteSiniestro:
    base = {
        "poliza_id": poliza.id,
        "tipo": TipoSiniestro.INCAPACIDAD_TOTAL,
        "fecha_ocurrencia": HOY,
        "archivos": (ArchivoEntrada(FOTO, ClaseArchivo.DOCUMENTO),),
    }
    return ReporteSiniestro(**{**base, **cambios})


# --- dominio ---


def test_cada_tipo_apunta_a_su_cobertura():
    assert TipoSiniestro.INCAPACIDAD_TOTAL.cobertura is CodigoCobertura.VIDA
    assert TipoSiniestro.INCENDIO_TERREMOTO.cobertura is CodigoCobertura.HOGAR


def test_el_radicado_lleva_el_anio_y_seis_caracteres_sin_ambiguos():
    numero = generar_numero_siniestro(AHORA, random.Random(7).choice)

    assert numero.startswith("SIN-2026-")
    assert len(numero) == len("SIN-2026-") + 6
    assert not set(numero[9:]) & set("ILOU")


def test_hoy_en_colombia_es_una_fecha():
    assert isinstance(hoy_en_colombia(), date)


# --- servicio: vigencia ---


@pytest.mark.parametrize(
    ("fecha", "esperado"),
    [
        (HOY, True),
        (HOY - timedelta(days=30), True),  # el día de la emisión
        (HOY - timedelta(days=31), False),  # antes de emitirse
        (HOY + timedelta(days=1), False),  # futura
    ],
)
async def test_la_fecha_se_compara_con_la_emision_y_con_hoy(fecha, esperado):
    poliza = _poliza()
    servicio, *_ = _servicio(poliza)

    if esperado:
        assert await servicio.reportar("cliente-1", _reporte(poliza, fecha_ocurrencia=fecha))
    else:
        with pytest.raises(FueraDeVigencia):
            await servicio.reportar("cliente-1", _reporte(poliza, fecha_ocurrencia=fecha))


async def test_una_fecha_posterior_al_fin_de_la_vigencia_es_fuera_de_vigencia():
    # Una póliza desactivada ya terminó: lo que ocurrió después no está cubierto.
    poliza = _poliza(estado=EstadoPoliza.DESACTIVADA, hasta=AHORA - timedelta(days=5))
    servicio, *_ = _servicio(poliza)

    with pytest.raises(FueraDeVigencia):
        await servicio.reportar("cliente-1", _reporte(poliza, fecha_ocurrencia=HOY))
    assert await servicio.reportar(
        "cliente-1", _reporte(poliza, fecha_ocurrencia=HOY - timedelta(days=10))
    )


@pytest.mark.parametrize("estado", [EstadoPoliza.PENDIENTE, EstadoPoliza.RECHAZADA])
async def test_una_poliza_sin_emitir_no_acepta_siniestros(estado):
    poliza = _poliza(estado=estado)
    servicio, *_ = _servicio(poliza)

    with pytest.raises(FueraDeVigencia):
        await servicio.reportar("cliente-1", _reporte(poliza))


async def test_una_poliza_emitida_sin_fechas_no_acepta_siniestros():
    poliza = _poliza()
    poliza.emitida_en = None
    servicio, *_ = _servicio(poliza)

    with pytest.raises(FueraDeVigencia):
        await servicio.reportar("cliente-1", _reporte(poliza))


async def test_la_poliza_desactivada_acepta_siniestros_de_su_vigencia():
    poliza = _poliza(estado=EstadoPoliza.DESACTIVADA)
    servicio, *_ = _servicio(poliza)

    siniestro = await servicio.reportar("cliente-1", _reporte(poliza))

    assert siniestro.estado is EstadoSiniestro.EN_ESTUDIO


# --- servicio: pertenencia y cobertura ---


async def test_cliente_o_poliza_inexistente_o_ajena():
    poliza = _poliza()

    servicio, *_ = _servicio(poliza, clientes=_Clientes(existe=False))
    with pytest.raises(ClienteNoEncontrado):
        await servicio.reportar("cliente-1", _reporte(poliza))

    servicio, *_ = _servicio(poliza)
    with pytest.raises(PolizaNoEncontrada):
        await servicio.reportar("otro-cliente", _reporte(poliza))
    with pytest.raises(PolizaNoEncontrada):
        await servicio.reportar("cliente-1", _reporte(poliza, poliza_id="no-existe"))


async def test_incendio_sin_hogar_es_tipo_no_cubierto():
    poliza = _poliza(coberturas=(CodigoCobertura.VIDA,))
    servicio, *_ = _servicio(poliza)
    reporte = _reporte(
        poliza,
        tipo=TipoSiniestro.INCENDIO_TERREMOTO,
        narracion="x",
        destino_pago={"tipo": "CHEQUE"},
        archivos=(ArchivoEntrada(FOTO, ClaseArchivo.FOTO),),
    )

    with pytest.raises(TipoNoCubierto) as error:
        await servicio.reportar("cliente-1", reporte)

    assert error.value.tipo is TipoSiniestro.INCENDIO_TERREMOTO


# --- servicio: contenido ---


async def _rechaza(poliza: Poliza, campo: str, **cambios) -> None:
    servicio, _, almacen, _ = _servicio(poliza)
    with pytest.raises(SolicitudSiniestroInvalida) as error:
        await servicio.reportar("cliente-1", _reporte(poliza, **cambios))
    assert error.value.campo == campo
    assert almacen._objetos == {}  # nada llegó al bucket


async def test_reglas_de_contenido_de_incapacidad_total():
    poliza = _poliza()
    foto = (ArchivoEntrada(FOTO, ClaseArchivo.FOTO),)

    await _rechaza(poliza, "destinoPago", destino_pago={"tipo": "CHEQUE"})
    await _rechaza(poliza, "archivos", archivos=())
    await _rechaza(poliza, "archivos", archivos=foto)  # falta el dictamen
    await _rechaza(poliza, "narracion", narracion="x" * 4001)


async def test_reglas_de_contenido_de_incendio():
    poliza = _poliza()
    incendio = {
        "tipo": TipoSiniestro.INCENDIO_TERREMOTO,
        "narracion": "Se incendió",
        "destino_pago": {"tipo": "CHEQUE"},
        "archivos": (ArchivoEntrada(FOTO, ClaseArchivo.FOTO),),
    }

    await _rechaza(poliza, "narracion", **{**incendio, "narracion": None})
    await _rechaza(poliza, "destinoPago", **{**incendio, "destino_pago": None})
    await _rechaza(
        poliza,
        "archivos",
        **{**incendio, "archivos": (ArchivoEntrada(FOTO, ClaseArchivo.DOCUMENTO),)},
    )
    await _rechaza(
        poliza,
        "archivos",
        **{**incendio, "archivos": (ArchivoEntrada(FOTO, ClaseArchivo.FOTO),) * 11},
    )


async def test_un_archivo_malo_no_deja_los_buenos_en_el_bucket():
    from app.domain import ImagenInvalida

    poliza = _poliza()
    servicio, repositorio, almacen, _ = _servicio(poliza)
    archivos = (
        ArchivoEntrada(FOTO, ClaseArchivo.DOCUMENTO),
        ArchivoEntrada(b"no soy una imagen", ClaseArchivo.DOCUMENTO),
    )

    with pytest.raises(ImagenInvalida):
        await servicio.reportar("cliente-1", _reporte(poliza, archivos=archivos))

    assert almacen._objetos == {}
    assert await repositorio.en_curso(poliza.id, TipoSiniestro.INCAPACIDAD_TOTAL) is None


# --- servicio: saldo, radicado e idempotencia ---


async def test_el_saldo_compara_la_entidad_sin_tildes_ni_mayusculas():
    poliza = _poliza()
    creditos = CreditosFake(
        CreditosReportados("DISPONIBLE", (HipotecaReportada("  BANCOLÓMBIA  S.A.", Decimal("1")),))
    )
    servicio, *_ = _servicio(poliza, creditos=creditos)

    # "bancolombia s.a." no es igual a "bancolombia": no se adivina, se usa el saldo de la póliza.
    siniestro = await servicio.reportar("cliente-1", _reporte(poliza))

    assert siniestro.saldo_credito.origen is OrigenSaldo.POLIZA


async def test_el_saldo_de_open_finance_se_guarda_con_su_fecha_y_la_entidad_de_la_poliza():
    poliza = _poliza()
    creditos = CreditosFake(
        CreditosReportados("DISPONIBLE", (HipotecaReportada("bancolómbia", Decimal("99000000")),))
    )
    servicio, *_ = _servicio(poliza, creditos=creditos)

    siniestro = await servicio.reportar("cliente-1", _reporte(poliza))

    assert siniestro.saldo_credito == SaldoCredito(
        Decimal("99000000"), "Bancolombia", AHORA, OrigenSaldo.OPEN_FINANCE
    )


async def test_un_radicado_repetido_se_reintenta_con_otro():
    poliza = _poliza()
    servicio, repositorio, *_ = _servicio(poliza, aleatorio=random.Random(1).choice)
    otra = _poliza(cliente_id="cliente-2")
    primero = await servicio.reportar("cliente-1", _reporte(poliza))

    class _Misma:
        """Devuelve primero los caracteres del radicado ya usado y luego otros."""

        def __init__(self) -> None:
            self._secuencia = iter(primero.numero[-6:] + "AAAAAA")

        def __call__(self, _alfabeto: str) -> str:
            return next(self._secuencia)

    repetido = SiniestroService(
        _Clientes(),
        _Polizas(otra),
        repositorio,
        InMemoryAlmacenEvidencias(),
        CreditosFake(),
        InMemoryIdempotencyStore(),
        max_bytes=3 * 1024 * 1024,
        max_pixeles=40_000_000,
        ahora=lambda: AHORA,
        hoy=lambda: HOY,
        aleatorio=_Misma(),
    )

    segundo = await repetido.reportar("cliente-2", _reporte(otra))

    assert segundo.numero == "SIN-2026-AAAAAA"


async def test_si_ningun_radicado_sirve_falla_en_vez_de_repetir():
    poliza = _poliza()
    servicio, repositorio, *_ = _servicio(poliza, aleatorio=lambda _a: "A")
    otra = _poliza(cliente_id="cliente-2")
    await servicio.reportar("cliente-1", _reporte(poliza))
    repetidor = SiniestroService(
        _Clientes(),
        _Polizas(otra),
        repositorio,
        InMemoryAlmacenEvidencias(),
        CreditosFake(),
        InMemoryIdempotencyStore(),
        max_bytes=3 * 1024 * 1024,
        max_pixeles=40_000_000,
        ahora=lambda: AHORA,
        hoy=lambda: HOY,
        aleatorio=lambda _a: "A",
    )

    with pytest.raises(RuntimeError, match="radicado"):
        await repetidor.reportar("cliente-2", _reporte(otra))


async def test_la_llave_de_idempotencia_es_por_cliente():
    uno = _poliza(cliente_id="cliente-1")
    dos = _poliza(cliente_id="cliente-2")
    repositorio = InMemorySiniestroRepository()
    servicio = SiniestroService(
        _Clientes(),
        _Polizas(uno, dos),
        repositorio,
        InMemoryAlmacenEvidencias(),
        CreditosFake(),
        InMemoryIdempotencyStore(),
        max_bytes=3 * 1024 * 1024,
        max_pixeles=40_000_000,
        ahora=lambda: AHORA,
        hoy=lambda: HOY,
    )

    a = await servicio.reportar("cliente-1", _reporte(uno), "misma-llave")
    b = await servicio.reportar("cliente-2", _reporte(dos), "misma-llave")

    assert a.id != b.id


async def test_si_el_siniestro_de_la_llave_ya_no_existe_se_reporta_de_nuevo():
    poliza = _poliza()
    servicio, repositorio, *_ = _servicio(poliza)
    primero = await servicio.reportar("cliente-1", _reporte(poliza), "llave")
    repositorio._items.clear()

    segundo = await servicio.reportar("cliente-1", _reporte(poliza), "llave")

    assert segundo.id != primero.id


async def test_el_log_del_reporte_no_incluye_el_destino_del_pago(caplog):
    poliza = _poliza()
    servicio, *_ = _servicio(poliza)
    reporte = _reporte(
        poliza,
        tipo=TipoSiniestro.INCENDIO_TERREMOTO,
        narracion="Se incendió",
        destino_pago={"tipo": "TRANSFERENCIA", "numeroCuenta": "48210012345"},
        archivos=(ArchivoEntrada(FOTO, ClaseArchivo.FOTO),),
    )

    with caplog.at_level(logging.INFO):
        await servicio.reportar("cliente-1", reporte)

    assert "48210012345" not in caplog.text


# --- repositorios (memoria y SQLite) ---


def _siniestro(**cambios) -> Siniestro:
    base = {
        "cliente_id": "cliente-1",
        "poliza_id": "poliza-1",
        "tipo": TipoSiniestro.INCENDIO_TERREMOTO,
        "fecha_ocurrencia": HOY,
        "saldo_credito": SaldoCredito(
            Decimal("140000000"), "Bancolombia", AHORA, OrigenSaldo.POLIZA
        ),
        "numero": "SIN-2026-AAAAAA",
        "narracion": "Se incendió",
        "archivos": (
            ArchivoSiniestro(
                "a-1",
                ClaseArchivo.FOTO,
                "siniestros/c/s/a-1.jpg",
                "image/jpeg",
                AHORA,
                capturado_en=AHORA - timedelta(minutes=5),
                latitud=4.65,
                longitud=-74.06,
            ),
            ArchivoSiniestro(
                "a-2", ClaseArchivo.DOCUMENTO, "siniestros/c/s/a-2.png", "image/png", AHORA
            ),
        ),
        "destino_pago": {"tipo": "CHEQUE", "oficina": {"id": "of-1"}},
        "reportado_en": AHORA,
        "ultima_entrega_en": AHORA,
        "actualizado_en": AHORA,
    }
    return Siniestro(**{**base, **cambios})


@pytest.fixture(params=["memoria", "sqlite"])
async def repositorio(request, tmp_path):
    if request.param == "memoria":
        return InMemorySiniestroRepository()
    db = SqliteDatabase(str(tmp_path / "siniestros.db"))
    await db.init()
    return SqliteSiniestroRepository(db)


async def test_el_repositorio_guarda_y_devuelve_lo_mismo(repositorio):
    siniestro = _siniestro()

    await repositorio.crear(siniestro)

    assert await repositorio.obtener(siniestro.id) == siniestro
    assert await repositorio.obtener("no-existe") is None


async def test_el_repositorio_guarda_un_siniestro_sin_destino_ni_narracion(repositorio):
    siniestro = _siniestro(
        tipo=TipoSiniestro.INCAPACIDAD_TOTAL, destino_pago=None, narracion=None, archivos=()
    )

    await repositorio.crear(siniestro)

    assert await repositorio.obtener(siniestro.id) == siniestro


async def test_el_repositorio_rechaza_un_radicado_repetido(repositorio):
    await repositorio.crear(_siniestro())

    with pytest.raises(NumeroSiniestroDuplicado):
        await repositorio.crear(_siniestro(poliza_id="otra", id="otro-id"))


async def test_el_repositorio_no_admite_dos_en_curso_de_la_misma_cobertura(repositorio):
    await repositorio.crear(_siniestro())

    with pytest.raises(SiniestroEnCurso):
        await repositorio.crear(_siniestro(numero="SIN-2026-BBBBBB"))
    # Otra cobertura de la misma póliza, y otra póliza con la misma cobertura, sí.
    await repositorio.crear(
        _siniestro(numero="SIN-2026-CCCCCC", tipo=TipoSiniestro.INCAPACIDAD_TOTAL)
    )
    await repositorio.crear(_siniestro(numero="SIN-2026-DDDDDD", poliza_id="poliza-2"))


async def test_en_curso_encuentra_solo_los_que_siguen_abiertos(repositorio):
    siniestro = _siniestro()
    await repositorio.crear(siniestro)

    assert (await repositorio.en_curso("poliza-1", siniestro.tipo)).id == siniestro.id
    assert await repositorio.en_curso("poliza-1", TipoSiniestro.INCAPACIDAD_TOTAL) is None
    assert await repositorio.en_curso("otra", siniestro.tipo) is None


async def test_un_siniestro_pagado_deja_libre_la_cobertura_en_memoria():
    repositorio = InMemorySiniestroRepository()
    pagado = _siniestro(estado=EstadoSiniestro.PAGADO)
    await repositorio.crear(pagado)

    assert await repositorio.en_curso("poliza-1", pagado.tipo) is None
    await repositorio.crear(_siniestro(numero="SIN-2026-EEEEEE"))


async def test_un_siniestro_pagado_o_rechazado_deja_libre_la_cobertura_en_sqlite(tmp_path):
    db = SqliteDatabase(str(tmp_path / "libre.db"))
    await db.init()
    repositorio = SqliteSiniestroRepository(db)
    await repositorio.crear(_siniestro(estado=EstadoSiniestro.PAGADO))
    await repositorio.crear(_siniestro(numero="SIN-2026-FFFFFF", estado=EstadoSiniestro.RECHAZADO))

    assert await repositorio.en_curso("poliza-1", TipoSiniestro.INCENDIO_TERREMOTO) is None
    await repositorio.crear(_siniestro(numero="SIN-2026-GGGGGG"))


async def test_la_base_se_puede_inicializar_dos_veces(tmp_path):
    db = SqliteDatabase(str(tmp_path / "doble.db"))
    await db.init()
    await db.init()


# --- adaptador hacia svc-perfilamiento ---


def _perfilamiento(handler) -> PerfilamientoHttp:
    cliente = httpx.AsyncClient(
        base_url="http://perfilamiento", transport=httpx.MockTransport(handler)
    )
    return PerfilamientoHttp("http://perfilamiento", client=cliente)


async def test_perfilamiento_lee_las_hipotecas_y_la_cuota():
    pedidos: list[httpx.Request] = []

    def handler(request):
        pedidos.append(request)
        return httpx.Response(
            200,
            json={
                "estado": "DISPONIBLE",
                "hipotecas": [
                    {
                        "entidadAcreedora": "Bancolombia",
                        "saldoInsoluto": 118500000.5,
                        "cuotaMensual": 950000,
                    },
                    {"entidadAcreedora": "BBVA", "saldoInsoluto": 1},
                ],
            },
        )

    adaptador = _perfilamiento(handler)
    creditos = await adaptador.hipotecas("c/1")

    assert creditos.estado == "DISPONIBLE"
    assert creditos.hipotecas[0] == HipotecaReportada(
        "Bancolombia", Decimal("118500000.5"), Decimal("950000")
    )
    assert creditos.hipotecas[1].cuota_mensual is None
    assert pedidos[0].url.raw_path == b"/clientes/c%2F1/creditos-hipotecarios"
    await adaptador.aclose()


async def test_perfilamiento_sin_consentimiento_devuelve_su_estado():
    adaptador = _perfilamiento(
        lambda _r: httpx.Response(200, json={"estado": "SIN_CONSENTIMIENTO", "hipotecas": []})
    )

    assert (await adaptador.hipotecas("c")).estado == "SIN_CONSENTIMIENTO"


@pytest.mark.parametrize(
    "respuesta",
    [
        httpx.Response(500),
        httpx.Response(404),
        httpx.Response(200, text="no es json"),
        httpx.Response(200, json={"sin": "estado"}),
        httpx.Response(
            200,
            json={
                "estado": "DISPONIBLE",
                "hipotecas": [{"entidadAcreedora": "X", "saldoInsoluto": "abc"}],
            },
        ),
    ],
)
async def test_perfilamiento_con_fallas_devuelve_no_disponible_sin_lanzar(respuesta):
    adaptador = _perfilamiento(lambda _r: respuesta)

    creditos = await adaptador.hipotecas("c")

    assert creditos == CreditosReportados(estado="NO_DISPONIBLE")


async def test_perfilamiento_con_la_red_caida_devuelve_no_disponible():
    def handler(_request):
        raise httpx.ConnectError("sin red")

    assert (await _perfilamiento(handler).hipotecas("c")).estado == "NO_DISPONIBLE"


async def test_perfilamiento_crea_su_propio_cliente_si_no_se_le_pasa_uno():
    adaptador = PerfilamientoHttp("http://perfilamiento")
    await adaptador.aclose()


async def test_el_doble_de_creditos_registra_las_consultas():
    creditos = CreditosFake()

    assert (await creditos.hipotecas("c-1")).estado == "NO_DISPONIBLE"
    assert creditos.consultas == ["c-1"]


# --- fábrica ---


def _construir(**cambios):
    settings = Settings(repository_backend="memory", **cambios)
    return factory.build_siniestros(
        settings,
        _Clientes(),
        _Polizas(),
        InMemoryAlmacenEvidencias(),
        InMemoryIdempotencyStore(),
    )


def test_fabrica_memoria_y_fake_no_dejan_nada_por_cerrar():
    servicio, cerrables = _construir()

    assert isinstance(servicio, SiniestroService)
    assert cerrables == []


def test_fabrica_sqlite(tmp_path):
    servicio, _ = factory.build_siniestros(
        Settings(repository_backend="sqlite", database_path=str(tmp_path / "f.db")),
        _Clientes(),
        _Polizas(),
        InMemoryAlmacenEvidencias(),
        InMemoryIdempotencyStore(),
    )

    assert isinstance(servicio._siniestros, SqliteSiniestroRepository)


async def test_fabrica_http_deja_cerrable_el_adaptador():
    servicio, cerrables = _construir(creditos_backend="http")

    assert [type(c).__name__ for c in cerrables] == ["PerfilamientoHttp"]
    for cerrable in cerrables:
        await cerrable.aclose()
    assert isinstance(servicio, SiniestroService)


def test_fabrica_rechaza_backends_desconocidos():
    with pytest.raises(ValueError, match="creditos_backend"):
        _construir(creditos_backend="paloma")
    with pytest.raises(ValueError, match="repository_backend"):
        factory.build_siniestros(
            Settings(repository_backend="otro"),
            _Clientes(),
            _Polizas(),
            InMemoryAlmacenEvidencias(),
            InMemoryIdempotencyStore(),
        )


# --- avisos ---


async def _avisos_con_dispositivo() -> tuple[AvisoService, InMemoryEventPublisher]:
    dispositivos = InMemoryDispositivoRepository()
    await dispositivos.registrar(
        Dispositivo("c-1", "fcm-token-0001", PlataformaDispositivo.ANDROID, "1.0"), 5
    )
    eventos = InMemoryEventPublisher()
    return AvisoService(dispositivos, eventos), eventos


def test_todos_los_avisos_tienen_texto_y_ninguno_lleva_montos():
    assert set(TEXTOS) == set(Aviso)
    for titulo, cuerpo in TEXTOS.values():
        assert titulo and cuerpo
        assert not any(c.isdigit() or c == "$" for c in titulo + cuerpo)


@pytest.mark.parametrize("aviso", list(Aviso))
async def test_cada_aviso_publica_su_texto_y_su_ruta(aviso):
    servicio, eventos = await _avisos_con_dispositivo()

    await servicio.avisar("c-1", aviso, ruta_seguro("poliza-1"))

    [evento] = eventos.events
    assert evento.datos["titulo"], evento.datos["cuerpo"]
    assert (evento.datos["titulo"], evento.datos["cuerpo"]) == TEXTOS[aviso]
    assert evento.datos["ruta"] == {"destino": "seguro", "id": "poliza-1"}
    assert evento.datos["tokens"] == ["fcm-token-0001"]


async def test_un_aviso_sin_ruta_no_la_incluye():
    servicio, eventos = await _avisos_con_dispositivo()

    await servicio.avisar("c-1", Aviso.SINIESTRO_PAGADO)

    assert "ruta" not in eventos.events[0].datos


async def test_sin_dispositivos_no_se_publica_nada():
    eventos = InMemoryEventPublisher()
    servicio = AvisoService(InMemoryDispositivoRepository(), eventos)

    await servicio.avisar("c-1", Aviso.SINIESTRO_PAGADO)

    assert eventos.events == []


def test_el_destino_de_la_ruta_es_el_seguro():
    assert json.dumps(ruta_seguro("p")) == '{"destino": "seguro", "id": "p"}'


# --- ciudad del cliente (BITS-386) ---


@pytest.mark.parametrize(
    ("respuesta", "esperada"),
    [
        (httpx.Response(200, json={"clienteId": "c", "ciudad": "Bogotá"}), "Bogotá"),
        (httpx.Response(200, json={"clienteId": "c", "ciudad": "  Soacha "}), "Soacha"),
        (httpx.Response(200, json={"clienteId": "c", "ciudad": None}), None),
        (httpx.Response(200, json={"clienteId": "c", "ciudad": "   "}), None),
        (httpx.Response(200, json={"clienteId": "c"}), None),
        (httpx.Response(200, json={"clienteId": "c", "ciudad": 5}), None),
        (httpx.Response(200, content=b"no es json"), None),
        (httpx.Response(200, json=["lista"]), None),
        (httpx.Response(404, json={"detail": "sin perfil"}), None),
        (httpx.Response(503), None),
    ],
)
async def test_perfilamiento_lee_la_ciudad_y_nunca_lanza(respuesta, esperada):
    pedidos: list[httpx.Request] = []

    def handler(request):
        pedidos.append(request)
        return respuesta

    adaptador = _perfilamiento(handler)

    assert await adaptador.ciudad("cli/1") == esperada
    assert pedidos[0].url.raw_path.decode() == "/v1/perfiles/cli%2F1"


async def test_perfilamiento_sin_respuesta_no_conoce_la_ciudad():
    def handler(request):
        raise httpx.ConnectError("sin red")

    assert await _perfilamiento(handler).ciudad("cli-1") is None
