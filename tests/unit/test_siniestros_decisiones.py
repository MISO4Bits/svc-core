"""BITS-377: bandeja, archivos y decisiones del ADMIN sobre los siniestros."""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

import pytest

from app.adapters.almacen import InMemoryAlmacenEvidencias
from app.adapters.creditos_fake import CreditosFake
from app.adapters.memory import (
    InMemoryDispositivoRepository,
    InMemoryEventPublisher,
    InMemoryIdempotencyStore,
    InMemorySiniestroRepository,
)
from app.adapters.sqlite import SqliteDatabase, SqliteSiniestroRepository
from app.avisos import Aviso, AvisoService
from app.domain import (
    ArchivoNoEncontrado,
    BeneficiarioPago,
    DanoHogar,
    Dispositivo,
    EstadoSiniestro,
    PlataformaDispositivo,
    PolizaNoEncontrada,
    SiniestroNoEncontrado,
    SolicitudSiniestroInvalida,
    TipoDecisionSiniestro,
    TipoSiniestro,
    TransicionInvalida,
    ValorSuperaSumaAsegurada,
)
from app.siniestros import DecisionEntrada, SiniestroService
from tests.unit.test_siniestros import AHORA, HOY, _Clientes, _poliza, _Polizas, _siniestro

AGENTE = "admin@solventa4bits.com"
PAGAR = TipoDecisionSiniestro.PAGAR
PEDIR = TipoDecisionSiniestro.PEDIR_EVIDENCIA
RECHAZAR = TipoDecisionSiniestro.RECHAZAR
TRANSFERENCIA = {
    "tipo": "TRANSFERENCIA",
    "banco": "Bancolombia",
    "tipoCuenta": "AHORROS",
    "numeroCuenta": "48210012345",
}
CHEQUE = {
    "tipo": "CHEQUE",
    "oficina": {"id": "of-1", "banco": "BBVA", "nombre": "Chapinero", "direccion": "Cra 13 # 60"},
}


class _Avisos(AvisoService):
    """Un AvisoService con un teléfono registrado, para ver qué se publica."""

    def __init__(self) -> None:
        self.eventos = InMemoryEventPublisher()
        super().__init__(InMemoryDispositivoRepository(), self.eventos)

    async def registrar_telefono(self, cliente_id: str = "cliente-1") -> None:
        await self._dispositivos.registrar(
            Dispositivo(cliente_id, "fcm-token-0001", PlataformaDispositivo.ANDROID, "1.0"), 5
        )


async def _entorno(*, avisos: AvisoService | None = None, repositorio=None, **cambios):
    """(servicio, repositorio, siniestro EN_ESTUDIO de Tu casa ya guardado)."""
    poliza = _poliza()
    repositorio = repositorio or InMemorySiniestroRepository()
    siniestro = _siniestro(poliza_id=poliza.id, **{"destino_pago": TRANSFERENCIA, **cambios})
    await repositorio.crear(siniestro)
    servicio = SiniestroService(
        _Clientes(),
        _Polizas(poliza),
        repositorio,
        InMemoryAlmacenEvidencias(),
        CreditosFake(),
        InMemoryIdempotencyStore(),
        max_bytes=3 * 1024 * 1024,
        max_pixeles=40_000_000,
        ahora=lambda: AHORA + timedelta(hours=1),
        hoy=lambda: HOY,
        avisos=avisos,
    )
    return servicio, repositorio, siniestro


def _pagar(**campos) -> DecisionEntrada:
    return DecisionEntrada(PAGAR, AGENTE, **campos)


# --- pagar: incapacidad total ---


async def test_pagar_incapacidad_total_es_una_linea_al_banco_por_el_saldo_guardado():
    servicio, _, siniestro = await _entorno(tipo=TipoSiniestro.INCAPACIDAD_TOTAL, destino_pago=None)

    pagado = await servicio.decidir(siniestro.id, _pagar())

    assert pagado.estado is EstadoSiniestro.PAGADO
    [linea] = pagado.pago.lineas
    assert linea.beneficiario is BeneficiarioPago.BANCO
    assert linea.valor == Decimal("140000000")
    assert linea.destino == "Bancolombia, abono al saldo del crédito"
    assert pagado.pago.pagado_en == AHORA + timedelta(hours=1)


async def test_pagar_incapacidad_total_no_lleva_dano_ni_valor():
    servicio, repositorio, siniestro = await _entorno(
        tipo=TipoSiniestro.INCAPACIDAD_TOTAL, destino_pago=None
    )

    for campos in ({"valor": Decimal(1)}, {"dano": DanoHogar.REPARABLE}):
        with pytest.raises(SolicitudSiniestroInvalida) as error:
            await servicio.decidir(siniestro.id, _pagar(**campos))
        assert error.value.campo in ("valor", "dano")

    assert (await repositorio.obtener(siniestro.id)).estado is EstadoSiniestro.EN_ESTUDIO


# --- pagar: Tu casa ---


async def test_pagar_tu_casa_reparable_va_al_cliente_a_la_cuenta_que_eligio():
    servicio, _, siniestro = await _entorno()

    pagado = await servicio.decidir(
        siniestro.id, _pagar(dano=DanoHogar.REPARABLE, valor=Decimal("40000000"))
    )

    [linea] = pagado.pago.lineas
    assert linea.beneficiario is BeneficiarioPago.CLIENTE
    assert linea.valor == Decimal("40000000")
    assert linea.destino == "Cuenta de ahorros terminada en 2345"


async def test_el_texto_del_destino_nunca_trae_el_numero_completo():
    servicio, _, siniestro = await _entorno()

    pagado = await servicio.decidir(
        siniestro.id, _pagar(dano=DanoHogar.REPARABLE, valor=Decimal("1000"))
    )

    assert "48210012345" not in pagado.pago.lineas[0].destino


async def test_el_destino_por_cheque_y_por_cuenta_corriente_se_muestran_listos():
    servicio, _, siniestro = await _entorno(destino_pago=CHEQUE)
    cheque = await servicio.decidir(
        siniestro.id, _pagar(dano=DanoHogar.REPARABLE, valor=Decimal("1000"))
    )
    servicio2, _, otro = await _entorno(destino_pago={**TRANSFERENCIA, "tipoCuenta": "CORRIENTE"})
    corriente = await servicio2.decidir(
        otro.id, _pagar(dano=DanoHogar.REPARABLE, valor=Decimal("1000"))
    )

    assert cheque.pago.lineas[0].destino == "Cheque en BBVA, Chapinero (Cra 13 # 60)"
    assert corriente.pago.lineas[0].destino == "Cuenta de corriente terminada en 2345"


async def test_perdida_total_paga_primero_al_banco_hasta_el_saldo_y_el_resto_al_cliente():
    servicio, _, siniestro = await _entorno()

    pagado = await servicio.decidir(
        siniestro.id, _pagar(dano=DanoHogar.PERDIDA_TOTAL, valor=Decimal("150000000"))
    )

    banco, cliente = pagado.pago.lineas
    assert (banco.beneficiario, banco.valor) == (BeneficiarioPago.BANCO, Decimal("140000000"))
    assert (cliente.beneficiario, cliente.valor) == (BeneficiarioPago.CLIENTE, Decimal("10000000"))
    assert banco.valor + cliente.valor == Decimal("150000000")


@pytest.mark.parametrize("valor", [Decimal("100000000"), Decimal("140000000")])
async def test_perdida_total_que_no_supera_el_saldo_va_toda_al_banco_sin_linea_al_cliente(valor):
    servicio, _, siniestro = await _entorno()

    pagado = await servicio.decidir(siniestro.id, _pagar(dano=DanoHogar.PERDIDA_TOTAL, valor=valor))

    [linea] = pagado.pago.lineas
    assert (linea.beneficiario, linea.valor) == (BeneficiarioPago.BANCO, valor)


async def test_el_valor_no_puede_superar_la_suma_asegurada_de_hogar():
    servicio, repositorio, siniestro = await _entorno()

    with pytest.raises(ValorSuperaSumaAsegurada) as error:
        await servicio.decidir(
            siniestro.id, _pagar(dano=DanoHogar.REPARABLE, valor=Decimal("150000001"))
        )

    assert error.value.suma_asegurada == Decimal("150000000")
    guardado = await repositorio.obtener(siniestro.id)
    assert guardado.estado is EstadoSiniestro.EN_ESTUDIO
    assert guardado.pago is None
    # El tope mismo sí se puede pagar.
    exacto = await servicio.decidir(
        siniestro.id, _pagar(dano=DanoHogar.REPARABLE, valor=Decimal("150000000"))
    )
    assert exacto.estado is EstadoSiniestro.PAGADO


@pytest.mark.parametrize(
    ("campos", "campo"),
    [
        ({"valor": Decimal("1000")}, "dano"),
        ({"dano": DanoHogar.REPARABLE}, "valor"),
        ({"dano": DanoHogar.REPARABLE, "valor": Decimal("0")}, "valor"),
        ({"dano": DanoHogar.REPARABLE, "valor": Decimal("-5")}, "valor"),
        ({}, "dano"),
    ],
)
async def test_pagar_tu_casa_exige_dano_y_valor(campos, campo):
    servicio, _, siniestro = await _entorno()

    with pytest.raises(SolicitudSiniestroInvalida) as error:
        await servicio.decidir(siniestro.id, _pagar(**campos))

    assert error.value.campo == campo


async def test_pagar_deja_constancia_de_quien_decidio_el_valor_y_el_dano():
    servicio, repositorio, siniestro = await _entorno()

    await servicio.decidir(
        siniestro.id, _pagar(dano=DanoHogar.PERDIDA_TOTAL, valor=Decimal("150000000"))
    )

    [decision] = (await repositorio.obtener(siniestro.id)).decisiones
    assert decision.agente == AGENTE
    assert decision.decision is PAGAR
    assert (decision.dano, decision.valor) == (DanoHogar.PERDIDA_TOTAL, Decimal("150000000"))
    assert decision.fecha == AHORA + timedelta(hours=1)


async def test_tu_casa_sin_cobertura_hogar_en_la_poliza_no_se_paga():
    from app.domain import CodigoCobertura, TipoNoCubierto

    poliza = _poliza(coberturas=(CodigoCobertura.VIDA,))
    repositorio = InMemorySiniestroRepository()
    siniestro = _siniestro(poliza_id=poliza.id, destino_pago=TRANSFERENCIA)
    await repositorio.crear(siniestro)
    servicio = SiniestroService(
        _Clientes(),
        _Polizas(poliza),
        repositorio,
        InMemoryAlmacenEvidencias(),
        CreditosFake(),
        InMemoryIdempotencyStore(),
        max_bytes=1,
        max_pixeles=1,
    )

    with pytest.raises(TipoNoCubierto):
        await servicio.decidir(
            siniestro.id, _pagar(dano=DanoHogar.REPARABLE, valor=Decimal("1000"))
        )


# --- pedir evidencia y rechazar ---


async def test_pedir_evidencia_guarda_lo_pedido_y_saca_el_siniestro_de_la_bandeja():
    servicio, repositorio, siniestro = await _entorno()

    pedido = await servicio.decidir(
        siniestro.id,
        DecisionEntrada(PEDIR, AGENTE, detalle="  Falta el recibo de la reparación  "),
    )

    assert pedido.estado is EstadoSiniestro.FALTA_INFORMACION
    assert pedido.evidencia_pedida.detalle == "Falta el recibo de la reparación"
    assert pedido.fecha_limite is None
    [decision] = (await repositorio.obtener(siniestro.id)).decisiones
    assert (decision.decision, decision.detalle) == (PEDIR, "Falta el recibo de la reparación")
    assert await servicio.bandeja(EstadoSiniestro.EN_ESTUDIO, None) == []


async def test_rechazar_guarda_el_motivo_que_ve_el_cliente():
    servicio, repositorio, siniestro = await _entorno()

    rechazado = await servicio.decidir(
        siniestro.id, DecisionEntrada(RECHAZAR, AGENTE, motivo="El evento no está cubierto")
    )

    assert rechazado.estado is EstadoSiniestro.RECHAZADO
    assert rechazado.motivo_rechazo == "El evento no está cubierto"
    [decision] = (await repositorio.obtener(siniestro.id)).decisiones
    assert (decision.decision, decision.motivo) == (RECHAZAR, "El evento no está cubierto")


@pytest.mark.parametrize(
    ("entrada", "campo"),
    [
        (DecisionEntrada(PEDIR, AGENTE), "detalle"),
        (DecisionEntrada(PEDIR, AGENTE, detalle="   "), "detalle"),
        (DecisionEntrada(PEDIR, AGENTE, detalle="x" * 2001), "detalle"),
        (DecisionEntrada(RECHAZAR, AGENTE), "motivo"),
        (DecisionEntrada(RECHAZAR, AGENTE, motivo="  "), "motivo"),
        (DecisionEntrada(PEDIR, AGENTE, detalle="falta", motivo="no"), "motivo"),
        (DecisionEntrada(RECHAZAR, AGENTE, motivo="no", detalle="falta"), "detalle"),
        (DecisionEntrada(PEDIR, AGENTE, detalle="falta", valor=Decimal(1)), "valor"),
        (DecisionEntrada(RECHAZAR, AGENTE, motivo="no", dano=DanoHogar.REPARABLE), "dano"),
        (
            DecisionEntrada(PAGAR, AGENTE, dano=DanoHogar.REPARABLE, valor=Decimal(1), motivo="x"),
            "motivo",
        ),
    ],
)
async def test_una_decision_sin_lo_que_necesita_o_con_lo_que_no_le_toca_es_400(entrada, campo):
    servicio, repositorio, siniestro = await _entorno()

    with pytest.raises(SolicitudSiniestroInvalida) as error:
        await servicio.decidir(siniestro.id, entrada)

    assert error.value.campo == campo
    assert (await repositorio.obtener(siniestro.id)).estado is EstadoSiniestro.EN_ESTUDIO


# --- siniestros que ya no se deciden ---


async def test_pagar_otra_vez_devuelve_la_orden_que_existe_sin_crear_otra():
    servicio, repositorio, siniestro = await _entorno()
    primero = await servicio.decidir(
        siniestro.id, _pagar(dano=DanoHogar.REPARABLE, valor=Decimal("40000000"))
    )

    segundo = await servicio.decidir(
        siniestro.id, _pagar(dano=DanoHogar.PERDIDA_TOTAL, valor=Decimal("150000000"))
    )

    assert segundo.pago == primero.pago
    assert len((await repositorio.obtener(siniestro.id)).decisiones) == 1


@pytest.mark.parametrize(
    "otra",
    [
        DecisionEntrada(PAGAR, AGENTE, dano=DanoHogar.REPARABLE, valor=Decimal(1)),
        DecisionEntrada(PEDIR, AGENTE, detalle="falta"),
        DecisionEntrada(RECHAZAR, AGENTE, motivo="no"),
    ],
)
async def test_un_siniestro_pagado_o_rechazado_no_cambia_con_ninguna_decision(otra):
    avisos = _Avisos()
    await avisos.registrar_telefono()
    for terminal in (
        DecisionEntrada(RECHAZAR, AGENTE, motivo="No está cubierto"),
        _pagar(dano=DanoHogar.REPARABLE, valor=Decimal("1000")),
    ):
        servicio, repositorio, siniestro = await _entorno(avisos=avisos)
        cerrado = await servicio.decidir(siniestro.id, terminal)
        avisos_antes = len(avisos.eventos.events)

        de_nuevo = await servicio.decidir(siniestro.id, otra)

        assert de_nuevo.estado is cerrado.estado
        assert len(de_nuevo.decisiones) == 1
        assert len(avisos.eventos.events) == avisos_antes  # no se avisa otra vez


async def test_un_siniestro_en_falta_informacion_ya_salio_de_la_bandeja():
    servicio, _, siniestro = await _entorno()
    await servicio.decidir(siniestro.id, DecisionEntrada(PEDIR, AGENTE, detalle="falta algo"))

    with pytest.raises(TransicionInvalida) as error:
        await servicio.decidir(siniestro.id, _pagar(dano=DanoHogar.REPARABLE, valor=Decimal(1)))

    assert error.value.estado is EstadoSiniestro.FALTA_INFORMACION


async def test_decidir_un_siniestro_que_no_existe_es_no_encontrado():
    servicio, _, _ = await _entorno()

    with pytest.raises(SiniestroNoEncontrado):
        await servicio.decidir("no-existe", _pagar())


# --- carreras ---


async def _con_carrera(estado_que_gana: str):
    servicio, repositorio, siniestro = await _entorno()
    original = repositorio.actualizar

    async def otra_decision_llega_primero(nuevo, estado_anterior):
        ganador = await repositorio.obtener(siniestro.id)
        if estado_que_gana == "rechazado":
            ganador.rechazar("gana la otra", AHORA, "otro@solventa4bits.com")
        else:
            ganador.pedir_evidencia("falta algo", AHORA, "otro@solventa4bits.com")
        await original(ganador, estado_anterior)
        return await original(nuevo, estado_anterior)

    repositorio.actualizar = otra_decision_llega_primero
    return servicio, siniestro


async def test_si_otra_decision_cerro_el_siniestro_primero_se_devuelve_como_quedo():
    servicio, siniestro = await _con_carrera("rechazado")

    resultado = await servicio.decidir(
        siniestro.id, _pagar(dano=DanoHogar.REPARABLE, valor=Decimal("1000"))
    )

    assert resultado.estado is EstadoSiniestro.RECHAZADO
    assert resultado.pago is None
    assert [d.agente for d in resultado.decisiones] == ["otro@solventa4bits.com"]


async def test_si_otra_decision_lo_dejo_en_falta_informacion_responde_409():
    servicio, siniestro = await _con_carrera("pedido")

    with pytest.raises(TransicionInvalida):
        await servicio.decidir(siniestro.id, _pagar(dano=DanoHogar.REPARABLE, valor=Decimal(1)))


# --- avisos ---


@pytest.mark.parametrize(
    ("entrada", "aviso"),
    [
        (_pagar(dano=DanoHogar.REPARABLE, valor=Decimal("1000")), Aviso.SINIESTRO_PAGADO),
        (DecisionEntrada(PEDIR, AGENTE, detalle="falta"), Aviso.SINIESTRO_FALTA_INFORMACION),
        (DecisionEntrada(RECHAZAR, AGENTE, motivo="no"), Aviso.SINIESTRO_RECHAZADO),
    ],
)
async def test_cada_decision_avisa_al_cliente_con_su_texto_y_la_ruta_del_seguro(entrada, aviso):
    avisos = _Avisos()
    await avisos.registrar_telefono()
    servicio, _, siniestro = await _entorno(avisos=avisos)

    await servicio.decidir(siniestro.id, entrada)

    [evento] = avisos.eventos.events
    assert evento.datos["clienteId"] == "cliente-1"
    assert evento.datos["ruta"] == {"destino": "seguro", "id": siniestro.poliza_id}
    from app.avisos import TEXTOS

    assert (evento.datos["titulo"], evento.datos["cuerpo"]) == TEXTOS[aviso]


async def test_si_el_aviso_falla_la_decision_queda_guardada_y_se_registra(caplog):
    class _Caido(_Avisos):
        async def avisar(self, *args, **kwargs):
            raise RuntimeError("fcm caído para admin@solventa4bits.com")

    servicio, repositorio, siniestro = await _entorno(avisos=_Caido())

    with caplog.at_level(logging.WARNING, logger="svc_core.siniestros"):
        resultado = await servicio.decidir(
            siniestro.id, DecisionEntrada(RECHAZAR, AGENTE, motivo="no")
        )

    assert resultado.estado is EstadoSiniestro.RECHAZADO
    assert (await repositorio.obtener(siniestro.id)).estado is EstadoSiniestro.RECHAZADO
    assert "no se pudo avisar" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "admin@solventa4bits.com" not in caplog.text


async def test_el_log_de_la_decision_no_incluye_al_agente_ni_el_destino(caplog):
    servicio, _, siniestro = await _entorno()

    with caplog.at_level(logging.INFO, logger="svc_core.siniestros"):
        await servicio.decidir(
            siniestro.id, _pagar(dano=DanoHogar.REPARABLE, valor=Decimal("1000"))
        )

    assert "decisión aplicada" in caplog.text
    assert AGENTE not in caplog.text
    assert "48210012345" not in caplog.text


# --- bandeja ---


async def _varios(repositorio: InMemorySiniestroRepository):
    """Tres en estudio con distinto plazo, uno pedido, uno pagado y uno automático."""
    base = {"cliente_id": "cliente-1", "tipo": TipoSiniestro.INCENDIO_TERREMOTO}
    ordenes = [
        ("SIN-2026-AAAAAA", "p-1", AHORA - timedelta(days=1), EstadoSiniestro.EN_ESTUDIO, False),
        ("SIN-2026-BBBBBB", "p-2", AHORA - timedelta(days=9), EstadoSiniestro.EN_ESTUDIO, False),
        ("SIN-2026-CCCCCC", "p-3", AHORA - timedelta(days=5), EstadoSiniestro.EN_ESTUDIO, False),
        (
            "SIN-2026-DDDDDD",
            "p-4",
            AHORA - timedelta(days=2),
            EstadoSiniestro.FALTA_INFORMACION,
            False,
        ),
        ("SIN-2026-EEEEEE", "p-5", AHORA - timedelta(days=3), EstadoSiniestro.PAGADO, False),
        ("SIN-2026-FFFFFF", "p-6", AHORA, EstadoSiniestro.PAGADO, True),
    ]
    for numero, poliza_id, fecha, estado, automatico in ordenes:
        await repositorio.crear(
            _siniestro(
                **base,
                numero=numero,
                poliza_id=poliza_id,
                estado=estado,
                automatico=automatico,
                reportado_en=fecha,
                ultima_entrega_en=fecha,
            )
        )


async def test_la_bandeja_va_por_plazo_y_luego_los_demas_del_mas_reciente_al_mas_antiguo():
    servicio, repositorio, _ = await _entorno()
    repositorio._items.clear()
    await _varios(repositorio)

    todos = await servicio.bandeja(None, None)

    assert [s.numero for s in todos] == [
        "SIN-2026-BBBBBB",  # vence antes
        "SIN-2026-CCCCCC",
        "SIN-2026-AAAAAA",  # vence después
        "SIN-2026-FFFFFF",  # sin plazo: del más reciente
        "SIN-2026-DDDDDD",
        "SIN-2026-EEEEEE",
    ]


async def test_la_bandeja_filtra_por_estado_y_por_automatico():
    servicio, repositorio, _ = await _entorno()
    repositorio._items.clear()
    await _varios(repositorio)

    en_estudio = await servicio.bandeja(EstadoSiniestro.EN_ESTUDIO, None)
    automaticos = await servicio.bandeja(None, True)
    manuales_pagados = await servicio.bandeja(EstadoSiniestro.PAGADO, False)

    assert [s.numero for s in en_estudio] == [
        "SIN-2026-BBBBBB",
        "SIN-2026-CCCCCC",
        "SIN-2026-AAAAAA",
    ]
    assert [s.numero for s in automaticos] == ["SIN-2026-FFFFFF"]
    assert [s.numero for s in manuales_pagados] == ["SIN-2026-EEEEEE"]


# --- archivo y detalle ---


async def test_el_archivo_se_devuelve_con_su_tipo_de_contenido():
    servicio, repositorio, siniestro = await _entorno()
    referencia = siniestro.archivos[0].referencia
    servicio._almacen._objetos[referencia] = b"los bytes de la foto"

    contenido, tipo = await servicio.archivo(siniestro.id, siniestro.archivos[0].id)

    assert (contenido, tipo) == (b"los bytes de la foto", "image/jpeg")


async def test_un_archivo_o_un_siniestro_que_no_existe_es_no_encontrado():
    servicio, _, siniestro = await _entorno()

    with pytest.raises(ArchivoNoEncontrado):
        await servicio.archivo(siniestro.id, "no-existe")
    with pytest.raises(SiniestroNoEncontrado):
        await servicio.archivo("no-existe", "a-1")


async def test_el_detalle_interno_trae_la_poliza_y_falla_si_ya_no_existe():
    servicio, repositorio, siniestro = await _entorno()

    detalle, poliza = await servicio.detalle_interno(siniestro.id)
    assert detalle.id == siniestro.id and poliza.id == siniestro.poliza_id

    huerfano = _siniestro(poliza_id="no-existe", numero="SIN-2026-HUERFA")
    await repositorio.crear(huerfano)
    with pytest.raises(PolizaNoEncontrada):
        await servicio.detalle_interno(huerfano.id)


# --- repositorios: decisiones y filtros (memoria y SQLite) ---


@pytest.fixture(params=["memoria", "sqlite"])
async def repo(request, tmp_path):
    if request.param == "memoria":
        return InMemorySiniestroRepository()
    db = SqliteDatabase(str(tmp_path / "decisiones.db"))
    await db.init()
    return SqliteSiniestroRepository(db)


async def test_las_decisiones_se_guardan_y_se_leen_igual(repo):
    siniestro = _siniestro()
    await repo.crear(siniestro)
    leido = await repo.obtener(siniestro.id)
    leido.pagar(_pago(), AHORA, AGENTE, dano=DanoHogar.PERDIDA_TOTAL, valor=Decimal("150000000.50"))

    assert await repo.actualizar(leido, EstadoSiniestro.EN_ESTUDIO) is True

    assert await repo.obtener(siniestro.id) == leido


async def test_todas_las_decisiones_se_guardan(repo):
    pedido = _siniestro(numero="SIN-2026-PEDIDO")
    pedido.pedir_evidencia("falta algo", AHORA, AGENTE)
    rechazado = _siniestro(numero="SIN-2026-RECHAZ", poliza_id="p-2")
    rechazado.rechazar("no cubierto", AHORA, AGENTE)
    for s in (pedido, rechazado):
        await repo.crear(s)

    assert (await repo.obtener(pedido.id)).decisiones == pedido.decisiones
    assert (await repo.obtener(rechazado.id)).decisiones == rechazado.decisiones


async def test_listar_filtra_por_estado_y_automatico(repo):
    await repo.crear(_siniestro(numero="SIN-2026-ENEST1"))
    await repo.crear(
        _siniestro(numero="SIN-2026-PAGAD1", poliza_id="p-2", estado=EstadoSiniestro.PAGADO)
    )
    await repo.crear(
        _siniestro(
            numero="SIN-2026-AUTOM1",
            poliza_id="p-3",
            estado=EstadoSiniestro.PAGADO,
            automatico=True,
        )
    )

    assert len(await repo.listar(None, None)) == 3
    assert [s.numero for s in await repo.listar(EstadoSiniestro.EN_ESTUDIO, None)] == [
        "SIN-2026-ENEST1"
    ]
    assert [s.numero for s in await repo.listar(None, True)] == ["SIN-2026-AUTOM1"]
    assert [s.numero for s in await repo.listar(EstadoSiniestro.PAGADO, False)] == [
        "SIN-2026-PAGAD1"
    ]
    assert await repo.listar(EstadoSiniestro.RECHAZADO, None) == []


def _pago():
    from app.domain import LineaPago, Pago

    return Pago(
        AHORA,
        (
            LineaPago(BeneficiarioPago.BANCO, Decimal("140000000"), "Bancolombia, abono"),
            LineaPago(BeneficiarioPago.CLIENTE, Decimal("10000000.50"), "Cuenta …4345"),
        ),
    )


async def test_un_pago_de_tu_casa_sin_destino_registrado_lo_dice_en_vez_de_inventarlo():
    servicio, _, siniestro = await _entorno(destino_pago=None)

    pagado = await servicio.decidir(
        siniestro.id, _pagar(dano=DanoHogar.REPARABLE, valor=Decimal("1000"))
    )

    assert pagado.pago.lineas[0].destino == "Sin destino registrado"


async def test_si_la_poliza_ya_no_existe_no_se_puede_pagar_tu_casa():
    repositorio = InMemorySiniestroRepository()
    huerfano = _siniestro(poliza_id="no-existe", destino_pago=TRANSFERENCIA)
    await repositorio.crear(huerfano)
    servicio = SiniestroService(
        _Clientes(),
        _Polizas(),
        repositorio,
        InMemoryAlmacenEvidencias(),
        CreditosFake(),
        InMemoryIdempotencyStore(),
        max_bytes=1,
        max_pixeles=1,
    )

    with pytest.raises(PolizaNoEncontrada):
        await servicio.decidir(huerfano.id, _pagar(dano=DanoHogar.REPARABLE, valor=Decimal("1000")))
