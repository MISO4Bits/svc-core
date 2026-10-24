"""BITS-101: solicitud, evaluación, emisión y decisión de pólizas (servicio con dobles)."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from app.adapters.memory import (
    InMemoryClienteRepository,
    InMemoryEventPublisher,
    InMemoryEvidenciaRepository,
    InMemoryPolizaRepository,
)
from app.adapters.polizas_fake import CotizacionFake, DocumentoPolizaFake, cotizacion_de_ejemplo
from app.domain import (
    Cliente,
    ClienteNoActivo,
    ClienteNoEncontrado,
    CodigoCobertura,
    CodigoPendiente,
    CotizacionCambio,
    CotizacionNoDisponible,
    CotizacionVencida,
    EstadoCliente,
    EstadoEvidencia,
    EstadoPoliza,
    Evidencia,
    FiltroPolizas,
    FueraDeRango,
    OrigenDatos,
    PendienteNoAplica,
    PolizaNoEmitida,
    PolizaNoEncontrada,
    PolizaNoPendiente,
    PolizaYaExiste,
    SinCotizacion,
    SolicitudExistente,
    SolicitudPolizaInvalida,
    SolicitudRechazada,
    SolicitudVencida,
    TipoDecision,
    TipoDocumento,
    TipoEvidencia,
    TipoPendiente,
)
from app.polizas import PolizaService

VIDA, HOGAR = CodigoCobertura.VIDA, CodigoCobertura.HOGAR
DOCS = [("poliza", "V1")]
FORMATO_NUMERO = re.compile(r"^SOL-VH-\d{4}-[0-9A-HJKMNP-TV-Z]{6}$")


class Reloj:
    def __init__(self) -> None:
        self.ahora = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.ahora


class Escenario:
    """Un cliente, sus repositorios y el servicio. Por defecto el cliente cumple todo."""

    def __init__(self, *, aleatorio=None) -> None:
        self.reloj = Reloj()
        self.evidencias = InMemoryEvidenciaRepository()
        self.clientes = InMemoryClienteRepository()
        self.polizas = InMemoryPolizaRepository(self.evidencias)
        self.cotizaciones = CotizacionFake()
        self.eventos = InMemoryEventPublisher()
        kwargs = {"aleatorio": aleatorio} if aleatorio else {}
        self.servicio = PolizaService(
            self.clientes,
            self.evidencias,
            self.polizas,
            self.cotizaciones,
            DocumentoPolizaFake(),
            self.eventos,
            ahora=self.reloj,
            **kwargs,
        )

    async def cliente(self, *, confirmado=True, estado=EstadoCliente.ACTIVO, nacimiento=None):
        cliente = Cliente(
            identity_ref=f"idp-{len(self.clientes._by_id)}",
            tipo_documento=TipoDocumento.CC,
            numero_documento=f"10{len(self.clientes._by_id):08d}",
            primer_nombre="Ana",
            segundo_nombre="María",
            primer_apellido="Ríos",
            fecha_nacimiento=nacimiento or date(1988, 5, 20),
            email=f"ana{len(self.clientes._by_id)}@correo.co",
            telefono="+573001234567",
            correo_confirmado=confirmado,
            estado=estado,
        )
        await self.clientes.crear_con_consentimientos(cliente, [])
        return cliente

    async def evidencia(self, cliente_id, tipo, estado=EstadoEvidencia.ACEPTADA):
        return await self.evidencias.guardar_reemplazando(
            Evidencia(cliente_id=cliente_id, tipo=tipo, estado=estado, almacen_ref=f"ref/{tipo}")
        )

    async def todas_las_evidencias(self, cliente_id):
        for tipo in TipoEvidencia:
            await self.evidencia(cliente_id, tipo)

    def cotizar(self, cliente_id, **kwargs):
        cot = cotizacion_de_ejemplo(**kwargs)
        cot = replace(
            cot,
            creada_en=self.reloj.ahora,
            vigencia=replace(
                cot.vigencia,
                desde=self.reloj.ahora,
                hasta=self.reloj.ahora + timedelta(days=30),
            ),
        )
        return self.cotizaciones.poner(cliente_id, cot)

    async def solicitar(self, cliente, coberturas=(VIDA,), cot=None):
        cot = cot or self.cotizaciones._por_cliente[cliente.id]
        return await self.servicio.solicitar(cliente.id, coberturas, cot.creada_en, DOCS)


@pytest.fixture
def e() -> Escenario:
    return Escenario()


async def _listo(e: Escenario, **kwargs):
    """Cliente que lo cumple todo, con su cotización CONFIRMADO."""
    cliente = await e.cliente(**kwargs)
    await e.todas_las_evidencias(cliente.id)
    e.cotizar(cliente.id)
    return cliente


# --- emisión ---


async def test_con_todo_cumplido_la_poliza_se_emite_en_la_misma_solicitud(e):
    cliente = await _listo(e)

    poliza, creada = await e.solicitar(cliente, (VIDA, HOGAR))

    assert creada
    assert poliza.estado is EstadoPoliza.EMITIDA
    assert FORMATO_NUMERO.match(poliza.numero)
    assert poliza.pendientes == ()
    assert poliza.emitida_en == e.reloj.ahora
    assert poliza.vigencia.desde == e.reloj.ahora
    assert poliza.vigencia.hasta == datetime(2046, 10, 24, 12, 0, tzinfo=UTC)  # 240 meses
    assert [c.codigo for c in poliza.coberturas] == [VIDA, HOGAR]
    assert str(poliza.prima_total_mensual) == "42368"  # 24696 + 17672
    assert {x.tipo for x in poliza.evidencias} == set(TipoEvidencia)


async def test_al_emitir_se_consumen_las_evidencias(e):
    cliente = await _listo(e)

    poliza, _ = await e.solicitar(cliente)

    for tipo in TipoEvidencia:
        assert await e.evidencias.vigente(cliente.id, tipo) is None
    assert poliza.estado is EstadoPoliza.EMITIDA


async def test_al_emitir_se_publica_poliza_emitida_con_lo_que_necesita_el_documento(e):
    cliente = await _listo(e)

    poliza, _ = await e.solicitar(cliente, (VIDA, HOGAR))

    [evento] = e.eventos.events
    assert evento.tipo == "PolizaEmitida"
    datos = evento.datos
    assert datos["numero"] == poliza.numero
    assert datos["asegurado"]["nombre"] == "Ana María Ríos"
    assert datos["asegurado"]["email"] == cliente.email
    assert datos["primaTotalMensual"] == 42368
    assert [c["codigo"] for c in datos["coberturas"]] == ["VIDA", "HOGAR"]
    assert datos["documentosAceptados"][0]["version"] == "V1"


async def test_solo_vida_cuesta_solo_vida(e):
    cliente = await _listo(e)

    poliza, _ = await e.solicitar(cliente, (VIDA,))

    assert [c.codigo for c in poliza.coberturas] == [VIDA]
    assert str(poliza.prima_total_mensual) == "24696"


async def test_un_fallo_al_publicar_no_deshace_la_emision(e):
    cliente = await _listo(e)

    async def falla(_evento):
        raise RuntimeError("Pub/Sub caído")

    e.eventos.publish = falla

    poliza, _ = await e.solicitar(cliente)

    assert poliza.estado is EstadoPoliza.EMITIDA


# --- pendientes ---


async def test_correo_sin_confirmar_deja_la_solicitud_pendiente(e):
    cliente = await _listo(e, confirmado=False)

    poliza, creada = await e.solicitar(cliente)

    assert creada
    assert poliza.estado is EstadoPoliza.PENDIENTE
    assert [p.codigo for p in poliza.pendientes] == [CodigoPendiente.CORREO_SIN_CONFIRMAR]
    assert poliza.pendientes[0].tipo is TipoPendiente.USUARIO
    assert poliza.numero is None
    assert e.eventos.events == []


async def test_evidencias_faltantes_dicen_cuales(e):
    cliente = await e.cliente()
    await e.evidencia(cliente.id, TipoEvidencia.CEDULA_FRENTE)
    e.cotizar(cliente.id)

    poliza, _ = await e.solicitar(cliente)

    [pendiente] = poliza.pendientes
    assert pendiente.codigo is CodigoPendiente.EVIDENCIA_FALTANTE
    assert pendiente.evidencias == (TipoEvidencia.CEDULA_REVERSO, TipoEvidencia.SELFIE)


async def test_evidencia_en_revision_manual_pende_de_un_agente(e):
    cliente = await e.cliente()
    await e.evidencia(cliente.id, TipoEvidencia.CEDULA_FRENTE)
    await e.evidencia(cliente.id, TipoEvidencia.CEDULA_REVERSO, EstadoEvidencia.REVISION_MANUAL)
    await e.evidencia(cliente.id, TipoEvidencia.SELFIE)
    e.cotizar(cliente.id)

    poliza, _ = await e.solicitar(cliente)

    [pendiente] = poliza.pendientes
    assert pendiente.codigo is CodigoPendiente.EVIDENCIA_EN_REVISION
    assert pendiente.tipo is TipoPendiente.AGENTE
    assert pendiente.evidencias == (TipoEvidencia.CEDULA_REVERSO,)


async def test_credito_declarado_pende_de_un_agente(e):
    cliente = await e.cliente()
    await e.todas_las_evidencias(cliente.id)
    e.cotizar(cliente.id, origen=OrigenDatos.DECLARADO)

    poliza, _ = await e.solicitar(cliente)

    [pendiente] = poliza.pendientes
    assert pendiente.codigo is CodigoPendiente.CREDITO_NO_VERIFICADO
    assert pendiente.tipo is TipoPendiente.AGENTE
    assert pendiente.mensaje == "Los datos de tu crédito deben ser verificados por un agente."


async def test_varios_pendientes_a_la_vez(e):
    cliente = await e.cliente(confirmado=False)
    e.cotizar(cliente.id, origen=OrigenDatos.DECLARADO)

    poliza, _ = await e.solicitar(cliente)

    assert [p.codigo for p in poliza.pendientes] == [
        CodigoPendiente.CORREO_SIN_CONFIRMAR,
        CodigoPendiente.EVIDENCIA_FALTANTE,
        CodigoPendiente.CREDITO_NO_VERIFICADO,
    ]


async def test_una_evidencia_ya_consumida_cuenta_como_faltante(e):
    cliente = await _listo(e)
    await e.solicitar(cliente)  # la primera póliza consume las tres
    nueva = e.cotizar(cliente.id)
    nueva = e.cotizaciones.poner(
        cliente.id, replace(nueva, creada_en=e.reloj.ahora + timedelta(minutes=1))
    )

    poliza, _ = await e.solicitar(cliente, cot=nueva)

    [pendiente] = poliza.pendientes
    assert pendiente.codigo is CodigoPendiente.EVIDENCIA_FALTANTE
    assert len(pendiente.evidencias) == 3


async def test_cumplir_lo_ultimo_que_faltaba_emite_sola(e):
    cliente = await e.cliente(confirmado=False)
    await e.todas_las_evidencias(cliente.id)
    e.cotizar(cliente.id)
    poliza, _ = await e.solicitar(cliente)
    assert poliza.estado is EstadoPoliza.PENDIENTE

    await e.clientes.confirmar(cliente.id)
    await e.servicio.reevaluar_cliente(cliente.id)  # el gancho que dispara la confirmación

    emitida = await e.servicio.obtener(cliente.id, poliza.id)
    assert emitida.estado is EstadoPoliza.EMITIDA
    assert [ev.tipo for ev in e.eventos.events] == ["PolizaEmitida"]


async def test_obtener_una_pendiente_la_reevalua(e):
    cliente = await e.cliente()
    e.cotizar(cliente.id)
    poliza, _ = await e.solicitar(cliente)
    assert poliza.pendientes[0].codigo is CodigoPendiente.EVIDENCIA_FALTANTE

    await e.todas_las_evidencias(cliente.id)

    assert (await e.servicio.obtener(cliente.id, poliza.id)).estado is EstadoPoliza.EMITIDA


async def test_un_fallo_en_el_gancho_no_se_propaga(e):
    async def roto(_cliente_id):
        raise RuntimeError("boom")

    e.polizas.pendientes_de_cliente = roto

    await e.servicio.reevaluar_cliente("cli-1")  # no lanza


# --- idempotencia de la solicitud ---


async def test_repetir_la_solicitud_devuelve_la_misma_sin_duplicar(e):
    cliente = await e.cliente(confirmado=False)
    e.cotizar(cliente.id)
    primera, creada1 = await e.solicitar(cliente)
    segunda, creada2 = await e.solicitar(cliente)

    assert (creada1, creada2) == (True, False)
    assert primera.id == segunda.id
    assert len(await e.servicio.listar_de_cliente(cliente.id)) == 1


async def test_repetir_con_otras_coberturas_es_conflicto(e):
    cliente = await e.cliente(confirmado=False)
    e.cotizar(cliente.id)
    await e.solicitar(cliente, (VIDA,))

    with pytest.raises(SolicitudExistente):
        await e.solicitar(cliente, (VIDA, HOGAR))


async def test_repetir_sobre_una_emitida_devuelve_la_emitida(e):
    cliente = await _listo(e)
    primera, _ = await e.solicitar(cliente)

    segunda, creada = await e.solicitar(cliente)

    assert not creada
    assert segunda.numero == primera.numero
    assert len(e.eventos.events) == 1  # no se emite dos veces


async def test_repetir_sobre_una_rechazada_es_conflicto(e):
    cliente = await e.cliente()
    await e.todas_las_evidencias(cliente.id)
    e.cotizar(cliente.id, origen=OrigenDatos.DECLARADO)
    poliza, _ = await e.solicitar(cliente)
    await e.servicio.decidir(
        poliza.id, TipoDecision.RECHAZAR, "maria", [CodigoPendiente.CREDITO_NO_VERIFICADO], None
    )

    with pytest.raises(SolicitudRechazada):
        await e.solicitar(cliente)


# --- errores duros ---


async def test_cliente_inexistente(e):
    with pytest.raises(ClienteNoEncontrado):
        await e.servicio.solicitar("nadie", [VIDA], e.reloj.ahora, DOCS)


async def test_cliente_bloqueado_no_puede_solicitar(e):
    cliente = await _listo(e, estado=EstadoCliente.BLOQUEADO)

    with pytest.raises(ClienteNoActivo):
        await e.solicitar(cliente)


async def test_la_cobertura_obligatoria_es_vida(e):
    cliente = await _listo(e)

    with pytest.raises(SolicitudPolizaInvalida) as error:
        await e.solicitar(cliente, (HOGAR,))

    assert error.value.campo == "coberturas"


async def test_hay_que_aceptar_las_condiciones_de_la_poliza(e):
    cliente = await _listo(e)
    cot = e.cotizaciones._por_cliente[cliente.id]

    with pytest.raises(SolicitudPolizaInvalida) as error:
        await e.servicio.solicitar(cliente.id, [VIDA], cot.creada_en, [("otro", "V1")])

    assert error.value.campo == "documentosAceptados"


async def test_una_cobertura_que_la_cotizacion_no_tiene_es_invalida(e):
    cliente = await _listo(e)
    cot = e.cotizaciones._por_cliente[cliente.id]
    e.cotizaciones.poner(cliente.id, replace(cot, coberturas=cot.coberturas[:1]))

    with pytest.raises(SolicitudPolizaInvalida) as error:
        await e.solicitar(cliente, (VIDA, HOGAR))

    assert "HOGAR" in error.value.mensaje


async def test_sin_cotizacion_activa(e):
    cliente = await e.cliente()

    with pytest.raises(SinCotizacion):
        await e.servicio.solicitar(cliente.id, [VIDA], e.reloj.ahora, DOCS)


async def test_si_la_cotizacion_cambio_no_se_emite_con_otro_precio(e):
    cliente = await _listo(e)
    vista = e.cotizaciones._por_cliente[cliente.id]
    e.cotizaciones.poner(
        cliente.id, replace(vista, creada_en=vista.creada_en + timedelta(seconds=5))
    )

    with pytest.raises(CotizacionCambio):
        await e.solicitar(cliente, cot=vista)


async def test_cotizacion_vencida(e):
    cliente = await _listo(e)
    e.reloj.ahora += timedelta(days=31)

    with pytest.raises(CotizacionVencida):
        await e.solicitar(cliente)


async def test_cotizacion_expirada_segun_cotizacion(e):
    cliente = await _listo(e)
    cot = e.cotizaciones._por_cliente[cliente.id]
    e.cotizaciones.poner(cliente.id, replace(cot, estado="EXPIRADA"))

    with pytest.raises(CotizacionVencida):
        await e.solicitar(cliente)


@pytest.mark.parametrize(
    ("nacimiento", "plazo"),
    [
        (date(2010, 1, 1), 240),  # menor de 18
        (date(1950, 1, 1), 240),  # mayor de 65
        (date(1970, 1, 1), 300),  # 56 años + 25 años > 75
    ],
)
async def test_edad_o_plazo_fuera_de_rango(e, nacimiento, plazo):
    cliente = await e.cliente(nacimiento=nacimiento)
    await e.todas_las_evidencias(cliente.id)
    e.cotizar(cliente.id, plazo_meses=plazo)

    with pytest.raises(FueraDeRango):
        await e.solicitar(cliente)
    assert await e.servicio.listar_de_cliente(cliente.id) == []


async def test_la_edad_se_calcula_con_la_fecha_de_hoy_y_no_la_de_la_cotizacion(e):
    # En la cotización tenía 65 años; hoy cumplió 66.
    cliente = await e.cliente(nacimiento=date(1960, 10, 23))
    await e.todas_las_evidencias(cliente.id)
    e.cotizar(cliente.id, edad=65, plazo_meses=60)

    with pytest.raises(FueraDeRango):
        await e.solicitar(cliente)


async def test_cotizacion_no_disponible_no_emite_nada(e):
    cliente = await _listo(e)
    e.cotizaciones.caida = True

    with pytest.raises(CotizacionNoDisponible):
        await e.solicitar(cliente)
    assert await e.servicio.listar_de_cliente(cliente.id) == []


async def test_un_fallo_inesperado_de_cotizacion_tambien_es_503(e):
    cliente = await _listo(e)

    async def rompe(_cliente_id):
        raise ValueError("respuesta rara")

    e.cotizaciones.activa = rompe

    with pytest.raises(CotizacionNoDisponible):
        await e.solicitar(cliente)


# --- una pendiente por cliente ---


async def test_una_solicitud_nueva_borra_la_pendiente_de_la_cotizacion_anterior(e):
    cliente = await e.cliente(confirmado=False)
    e.cotizar(cliente.id)
    vieja, _ = await e.solicitar(cliente)
    e.reloj.ahora += timedelta(minutes=5)
    e.cotizar(cliente.id)  # el cliente volvió a cotizar

    nueva, _ = await e.solicitar(cliente)

    assert nueva.id != vieja.id
    assert await e.polizas.obtener(vieja.id) is None
    assert [p.id for p in await e.servicio.listar_de_cliente(cliente.id)] == [nueva.id]


async def test_no_se_borra_una_emitida(e):
    cliente = await _listo(e)
    emitida, _ = await e.solicitar(cliente)
    e.reloj.ahora += timedelta(minutes=5)
    e.cotizar(cliente.id)

    await e.servicio.solicitar(
        cliente.id, [VIDA], e.cotizaciones._por_cliente[cliente.id].creada_en, DOCS
    )

    assert await e.polizas.obtener(emitida.id) is not None


async def test_el_evento_de_nueva_cotizacion_borra_la_pendiente_anterior(e):
    cliente = await e.cliente(confirmado=False)
    e.cotizar(cliente.id)
    vieja, _ = await e.solicitar(cliente)
    nueva_cotizacion = e.reloj.ahora + timedelta(minutes=5)

    await e.servicio.descartar_pendientes(cliente.id, nueva_cotizacion)

    assert await e.polizas.obtener(vieja.id) is None


async def test_el_evento_no_borra_la_pendiente_de_la_cotizacion_nueva(e):
    cliente = await e.cliente(confirmado=False)
    cot = e.cotizar(cliente.id)
    poliza, _ = await e.solicitar(cliente)

    await e.servicio.descartar_pendientes(cliente.id, cot.creada_en)

    assert await e.polizas.obtener(poliza.id) is not None


async def test_eliminar_la_cotizacion_borra_todas_las_pendientes(e):
    cliente = await e.cliente(confirmado=False)
    e.cotizar(cliente.id)
    poliza, _ = await e.solicitar(cliente)

    await e.servicio.descartar_pendientes(cliente.id, None)

    assert await e.polizas.obtener(poliza.id) is None


# --- vigencia de la solicitud ---


async def test_una_pendiente_con_cotizacion_vencida_se_lee_como_vencida(e):
    cliente = await e.cliente(confirmado=False)
    e.cotizar(cliente.id)
    poliza, _ = await e.solicitar(cliente)
    e.reloj.ahora += timedelta(days=31)

    [leida] = await e.servicio.listar_de_cliente(cliente.id)

    assert leida.estado is EstadoPoliza.PENDIENTE  # lo guardado
    assert leida.estado_en(e.reloj.ahora) is EstadoPoliza.VENCIDA
    assert await e.servicio.listar_de_cliente(cliente.id, EstadoPoliza.VENCIDA) == [leida]
    assert poliza.id == leida.id


async def test_una_vencida_no_se_reevalua_ni_se_emite(e):
    cliente = await e.cliente()
    e.cotizar(cliente.id)
    poliza, _ = await e.solicitar(cliente)
    await e.todas_las_evidencias(cliente.id)
    e.reloj.ahora += timedelta(days=31)

    leida = await e.servicio.obtener(cliente.id, poliza.id)

    assert leida.estado is EstadoPoliza.PENDIENTE
    assert e.eventos.events == []


# --- lectura ---


async def test_una_poliza_ajena_no_se_encuentra(e):
    cliente = await _listo(e)
    otro = await e.cliente()
    poliza, _ = await e.solicitar(cliente)

    with pytest.raises(PolizaNoEncontrada):
        await e.servicio.obtener(otro.id, poliza.id)


async def test_listar_de_un_cliente_inexistente(e):
    with pytest.raises(ClienteNoEncontrado):
        await e.servicio.listar_de_cliente("nadie")


async def test_el_documento_solo_existe_si_esta_emitida(e):
    cliente = await e.cliente(confirmado=False)
    e.cotizar(cliente.id)
    pendiente, _ = await e.solicitar(cliente)

    with pytest.raises(PolizaNoEmitida):
        await e.servicio.documento(cliente.id, pendiente.id)


async def test_el_documento_de_una_emitida(e):
    cliente = await _listo(e)
    poliza, _ = await e.solicitar(cliente)

    documento = await e.servicio.documento(cliente.id, poliza.id)

    assert documento.url.endswith(f"{poliza.numero}.pdf")


# --- decisiones del agente ---


async def _declarada(e, **kwargs):
    cliente = await e.cliente(**kwargs)
    await e.todas_las_evidencias(cliente.id)
    e.cotizar(cliente.id, origen=OrigenDatos.DECLARADO)
    poliza, _ = await e.solicitar(cliente)
    return cliente, poliza


async def test_aprobar_el_ultimo_pendiente_emite_la_poliza(e):
    _, poliza = await _declarada(e)

    emitida = await e.servicio.decidir(
        poliza.id,
        TipoDecision.APROBAR,
        "maria@solventa.co",
        [CodigoPendiente.CREDITO_NO_VERIFICADO],
        "ok",
    )

    assert emitida.estado is EstadoPoliza.EMITIDA
    assert emitida.decisiones[0].agente == "maria@solventa.co"
    assert emitida.decisiones[0].nota == "ok"
    assert len(e.eventos.events) == 1


async def test_aprobar_no_emite_si_quedan_pendientes_del_usuario(e):
    _, poliza = await _declarada(e, confirmado=False)

    despues = await e.servicio.decidir(
        poliza.id, TipoDecision.APROBAR, "maria", [CodigoPendiente.CREDITO_NO_VERIFICADO], None
    )

    assert despues.estado is EstadoPoliza.PENDIENTE
    assert [p.codigo for p in despues.pendientes] == [CodigoPendiente.CORREO_SIN_CONFIRMAR]
    assert e.eventos.events == []


async def test_repetir_una_aprobacion_no_cambia_nada(e):
    _, poliza = await _declarada(e, confirmado=False)
    codigos = [CodigoPendiente.CREDITO_NO_VERIFICADO]
    await e.servicio.decidir(poliza.id, TipoDecision.APROBAR, "maria", codigos, None)

    otra_vez = await e.servicio.decidir(poliza.id, TipoDecision.APROBAR, "juan", codigos, None)

    assert len(otra_vez.decisiones) == 1
    assert otra_vez.decisiones[0].agente == "maria"


async def test_aprobar_la_revision_de_evidencias_la_levanta(e):
    cliente = await e.cliente()
    await e.evidencia(cliente.id, TipoEvidencia.CEDULA_FRENTE)
    await e.evidencia(cliente.id, TipoEvidencia.CEDULA_REVERSO, EstadoEvidencia.REVISION_MANUAL)
    await e.evidencia(cliente.id, TipoEvidencia.SELFIE)
    e.cotizar(cliente.id)
    poliza, _ = await e.solicitar(cliente)

    emitida = await e.servicio.decidir(
        poliza.id, TipoDecision.APROBAR, "maria", [CodigoPendiente.EVIDENCIA_EN_REVISION], None
    )

    assert emitida.estado is EstadoPoliza.EMITIDA


async def test_rechazar_deja_el_motivo_del_escenario(e):
    _, poliza = await _declarada(e)

    rechazada = await e.servicio.decidir(
        poliza.id, TipoDecision.RECHAZAR, "maria", [CodigoPendiente.CREDITO_NO_VERIFICADO], "nota"
    )

    assert rechazada.estado is EstadoPoliza.RECHAZADA
    assert [(m.codigo, m.mensaje) for m in rechazada.motivos_rechazo] == [
        (
            CodigoPendiente.CREDITO_NO_VERIFICADO,
            "No pudimos verificar los datos de tu crédito con la entidad acreedora.",
        )
    ]
    assert rechazada.rechazada_en == e.reloj.ahora
    assert e.eventos.events == []


async def test_rechazar_no_consume_las_evidencias(e):
    cliente, poliza = await _declarada(e)

    await e.servicio.decidir(
        poliza.id, TipoDecision.RECHAZAR, "maria", [CodigoPendiente.CREDITO_NO_VERIFICADO], None
    )

    for tipo in TipoEvidencia:
        assert await e.evidencias.vigente(cliente.id, tipo) is not None


async def test_rechazar_con_pendientes_de_usuario_sigue_siendo_posible(e):
    _, poliza = await _declarada(e, confirmado=False)

    rechazada = await e.servicio.decidir(
        poliza.id, TipoDecision.RECHAZAR, "maria", [CodigoPendiente.CREDITO_NO_VERIFICADO], None
    )

    assert rechazada.estado is EstadoPoliza.RECHAZADA


async def test_decidir_un_pendiente_que_la_poliza_no_tiene(e):
    _, poliza = await _declarada(e)

    with pytest.raises(PendienteNoAplica):
        await e.servicio.decidir(
            poliza.id, TipoDecision.APROBAR, "maria", [CodigoPendiente.EVIDENCIA_EN_REVISION], None
        )


async def test_un_pendiente_de_usuario_no_se_decide(e):
    _, poliza = await _declarada(e, confirmado=False)

    with pytest.raises(PendienteNoAplica):
        await e.servicio.decidir(
            poliza.id, TipoDecision.APROBAR, "maria", [CodigoPendiente.CORREO_SIN_CONFIRMAR], None
        )


async def test_no_se_decide_sobre_una_emitida_ni_una_rechazada(e):
    cliente = await _listo(e)
    emitida, _ = await e.solicitar(cliente)

    with pytest.raises(PolizaNoPendiente):
        await e.servicio.decidir(
            emitida.id, TipoDecision.APROBAR, "maria", [CodigoPendiente.CREDITO_NO_VERIFICADO], None
        )


async def test_no_se_decide_sobre_una_vencida(e):
    _, poliza = await _declarada(e)
    e.reloj.ahora += timedelta(days=31)

    with pytest.raises(SolicitudVencida):
        await e.servicio.decidir(
            poliza.id, TipoDecision.APROBAR, "maria", [CodigoPendiente.CREDITO_NO_VERIFICADO], None
        )


async def test_decidir_sobre_una_poliza_inexistente(e):
    with pytest.raises(PolizaNoEncontrada):
        await e.servicio.decidir(
            "x", TipoDecision.APROBAR, "maria", [CodigoPendiente.CREDITO_NO_VERIFICADO], None
        )


# --- cola del backoffice ---


async def test_la_cola_se_filtra_por_estado_y_tipo_de_pendiente(e):
    _, de_agente = await _declarada(e)
    cliente = await e.cliente(confirmado=False)
    e.cotizar(cliente.id)
    e.reloj.ahora += timedelta(seconds=1)
    de_usuario, _ = await e.solicitar(cliente)

    todas = await e.servicio.listar(FiltroPolizas())
    solo_agente = await e.servicio.listar(
        FiltroPolizas(estado=EstadoPoliza.PENDIENTE, tipo_pendiente=TipoPendiente.AGENTE)
    )
    por_codigo = await e.servicio.listar(
        FiltroPolizas(codigo_pendiente=CodigoPendiente.CORREO_SIN_CONFIRMAR)
    )

    assert {p.id for p in todas.items} == {de_agente.id, de_usuario.id}
    assert [p.id for p in solo_agente.items] == [de_agente.id]
    assert [p.id for p in por_codigo.items] == [de_usuario.id]


async def test_la_cola_pagina_de_la_mas_antigua_a_la_mas_reciente(e):
    ids = []
    for _ in range(3):
        e.reloj.ahora += timedelta(seconds=1)
        _, poliza = await _declarada(e)
        ids.append(poliza.id)

    primera = await e.servicio.listar(FiltroPolizas(limite=2))
    segunda = await e.servicio.listar(FiltroPolizas(limite=2, cursor=primera.siguiente_cursor))
    al_reves = await e.servicio.listar(FiltroPolizas(limite=2, mas_recientes_primero=True))

    assert [p.id for p in primera.items] == ids[:2]
    assert primera.siguiente_cursor is not None
    assert [p.id for p in segunda.items] == ids[2:]
    assert segunda.siguiente_cursor is None
    assert [p.id for p in al_reves.items] == [ids[2], ids[1]]


async def test_la_cola_filtra_por_cliente_y_fechas(e):
    cliente, poliza = await _declarada(e)

    por_cliente = await e.servicio.listar(FiltroPolizas(cliente_id=cliente.id))
    despues = await e.servicio.listar(
        FiltroPolizas(creada_desde=e.reloj.ahora + timedelta(hours=1))
    )
    antes = await e.servicio.listar(FiltroPolizas(creada_hasta=e.reloj.ahora - timedelta(hours=1)))

    assert [p.id for p in por_cliente.items] == [poliza.id]
    assert despues.items == ()
    assert antes.items == ()


async def test_la_cola_distingue_pendientes_vigentes_de_vencidas(e):
    _, poliza = await _declarada(e)
    e.reloj.ahora += timedelta(days=31)

    vencidas = await e.servicio.listar(FiltroPolizas(estado=EstadoPoliza.VENCIDA))
    pendientes = await e.servicio.listar(FiltroPolizas(estado=EstadoPoliza.PENDIENTE))

    assert [p.id for p in vencidas.items] == [poliza.id]
    assert pendientes.items == ()


# --- número de póliza ---


async def test_si_el_numero_ya_existe_se_genera_otro():
    secuencia = iter("AAAAAA" + "AAAAAA" + "BBBBBB")
    e = Escenario(aleatorio=lambda _alfabeto: next(secuencia))
    primero = await _listo(e)
    una, _ = await e.solicitar(primero)
    otro = await _listo(e)

    dos, _ = await e.solicitar(otro)

    assert una.numero.endswith("-AAAAAA")
    assert dos.numero.endswith("-BBBBBB")
    assert una.numero != dos.numero


# --- carreras y fallos poco comunes ---


async def test_si_otra_solicitud_gana_la_carrera_se_devuelve_esa(e):
    cliente = await e.cliente(confirmado=False)
    cot = e.cotizar(cliente.id)
    ganadora = None
    crear = e.polizas.crear

    async def pierde_la_carrera(poliza):
        nonlocal ganadora
        e.polizas.crear = crear
        ganadora, _ = await e.servicio.solicitar(cliente.id, [VIDA], cot.creada_en, DOCS)
        raise PolizaYaExiste(cliente.id)

    e.polizas.crear = pierde_la_carrera

    poliza, creada = await e.solicitar(cliente)

    assert not creada
    assert poliza.id == ganadora.id


async def test_si_no_hay_numero_libre_falla_en_vez_de_emitir_sin_numero():
    e = Escenario(aleatorio=lambda _alfabeto: "A")  # siempre el mismo número
    primero = await _listo(e)
    await e.solicitar(primero)
    otro = await _listo(e)

    with pytest.raises(RuntimeError):
        await e.solicitar(otro)


async def test_aprobar_una_poliza_que_dejo_de_estar_pendiente_es_conflicto(e):
    _, poliza = await _declarada(e)

    async def ya_no(_poliza_id, _decision):
        return False

    e.polizas.aprobar = ya_no

    with pytest.raises(PolizaNoPendiente):
        await e.servicio.decidir(
            poliza.id, TipoDecision.APROBAR, "m", [CodigoPendiente.CREDITO_NO_VERIFICADO], None
        )


async def test_rechazar_una_poliza_que_dejo_de_estar_pendiente_es_conflicto(e):
    _, poliza = await _declarada(e)

    async def ya_no(*_args):
        return None

    e.polizas.rechazar = ya_no

    with pytest.raises(PolizaNoPendiente):
        await e.servicio.decidir(
            poliza.id, TipoDecision.RECHAZAR, "m", [CodigoPendiente.CREDITO_NO_VERIFICADO], None
        )


async def test_la_cola_pagina_tambien_de_la_mas_reciente_a_la_mas_antigua(e):
    ids = []
    for _ in range(3):
        e.reloj.ahora += timedelta(seconds=1)
        _, poliza = await _declarada(e)
        ids.append(poliza.id)

    primera = await e.servicio.listar(FiltroPolizas(limite=2, mas_recientes_primero=True))
    segunda = await e.servicio.listar(
        FiltroPolizas(limite=2, mas_recientes_primero=True, cursor=primera.siguiente_cursor)
    )

    assert [p.id for p in segunda.items] == [ids[0]]


async def test_quitar_la_cotizacion_la_deja_sin_cotizacion(e):
    cliente = await e.cliente()
    e.cotizar(cliente.id)
    e.cotizaciones.quitar(cliente.id)

    with pytest.raises(SinCotizacion):
        await e.servicio.solicitar(cliente.id, [VIDA], e.reloj.ahora, DOCS)
