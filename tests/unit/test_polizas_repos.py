"""El mismo comportamiento del repositorio de pólizas contra memoria y contra SQLite."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from app.adapters.memory import InMemoryEvidenciaRepository, InMemoryPolizaRepository
from app.adapters.sqlite import SqliteDatabase, SqliteEvidenciaRepository, SqlitePolizaRepository
from app.domain import (
    CoberturaContratada,
    CodigoCobertura,
    CodigoPendiente,
    DatosCredito,
    Decision,
    DocumentoAceptado,
    EstadoEvidencia,
    EstadoPoliza,
    Evidencia,
    FiltroPolizas,
    MotivoRechazoPoliza,
    NumeroPolizaDuplicado,
    OrigenDatos,
    Pendiente,
    Poliza,
    PolizaYaExiste,
    TipoDecision,
    TipoEvidencia,
    TipoPendiente,
    Vigencia,
)

AHORA = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
COBERTURA = CoberturaContratada(
    CodigoCobertura.VIDA,
    True,
    Decimal("140000000"),
    Decimal("24696"),
    Decimal("0"),
    Decimal("24696"),
)


def poliza(cliente_id="cli-1", cotizacion=AHORA, creada_en=AHORA, **cambios) -> Poliza:
    base = {
        "cliente_id": cliente_id,
        "cotizacion_creada_en": cotizacion,
        "solicitud_vigente_hasta": cotizacion + timedelta(days=30),
        "coberturas": (COBERTURA,),
        "datos_credito": DatosCredito(
            Decimal("150000000"), 240, "Bancolombia", Decimal("140000000")
        ),
        "origen_datos": OrigenDatos.DECLARADO,
        "tasa": Decimal("0.196"),
        "version_tarifa": 2,
        "cobertura_meses": 240,
        "edad": 38,
        "documentos_aceptados": (DocumentoAceptado("poliza", "V1", creada_en),),
        "pendientes": (Pendiente.de(CodigoPendiente.CREDITO_NO_VERIFICADO),),
        "creada_en": creada_en,
    }
    return Poliza(**{**base, **cambios})


@pytest_asyncio.fixture(params=["memoria", "sqlite"])
async def repos(request, tmp_path):
    if request.param == "memoria":
        evidencias = InMemoryEvidenciaRepository()
        return InMemoryPolizaRepository(evidencias), evidencias
    db = SqliteDatabase(str(tmp_path / "polizas.db"))
    await db.init()
    return SqlitePolizaRepository(db), SqliteEvidenciaRepository(db)


async def test_guarda_y_recupera_la_poliza_entera(repos):
    polizas, _ = repos
    original = poliza(
        pendientes=(
            Pendiente.de(CodigoPendiente.EVIDENCIA_FALTANTE, (TipoEvidencia.SELFIE,)),
            Pendiente.de(CodigoPendiente.CREDITO_NO_VERIFICADO),
        ),
        ultima_evaluacion_en=AHORA,
    )

    await polizas.crear(original)

    assert await polizas.obtener(original.id) == original
    assert await polizas.obtener("otra") is None


async def test_una_segunda_solicitud_de_la_misma_cotizacion_choca(repos):
    polizas, _ = repos
    await polizas.crear(poliza())

    with pytest.raises(PolizaYaExiste):
        await polizas.crear(poliza())


async def test_se_busca_por_cliente_y_cotizacion(repos):
    polizas, _ = repos
    guardada = poliza()
    await polizas.crear(guardada)

    assert await polizas.obtener_de_cotizacion("cli-1", AHORA) == guardada
    assert await polizas.obtener_de_cotizacion("cli-1", AHORA + timedelta(seconds=1)) is None
    assert await polizas.obtener_de_cotizacion("cli-2", AHORA) is None


async def test_la_cotizacion_se_compara_en_utc_sin_importar_el_huso(repos):
    polizas, _ = repos
    await polizas.crear(poliza())
    bogota = AHORA.astimezone(timezone_bogota())

    assert await polizas.obtener_de_cotizacion("cli-1", bogota) is not None


def timezone_bogota():
    from datetime import timezone

    return timezone(timedelta(hours=-5))


async def test_lista_las_del_cliente_de_la_mas_reciente_a_la_mas_antigua(repos):
    polizas, _ = repos
    vieja = poliza(cotizacion=AHORA, creada_en=AHORA)
    nueva = poliza(cotizacion=AHORA + timedelta(days=1), creada_en=AHORA + timedelta(days=1))
    ajena = poliza(cliente_id="cli-2")
    for p in (vieja, nueva, ajena):
        await polizas.crear(p)

    assert [p.id for p in await polizas.listar_de_cliente("cli-1")] == [nueva.id, vieja.id]


async def test_actualizar_pendientes_solo_toca_las_pendientes(repos):
    polizas, _ = repos
    p = poliza()
    await polizas.crear(p)
    nuevos = (Pendiente.de(CodigoPendiente.CORREO_SIN_CONFIRMAR),)

    await polizas.actualizar_pendientes(p.id, nuevos, AHORA)

    guardada = await polizas.obtener(p.id)
    assert guardada.pendientes == nuevos
    assert guardada.ultima_evaluacion_en == AHORA


async def test_emitir_asigna_numero_y_consume_las_evidencias(repos):
    polizas, evidencias = repos
    p = poliza(pendientes=())
    await polizas.crear(p)
    for tipo in TipoEvidencia:
        await evidencias.guardar_reemplazando(
            Evidencia(
                cliente_id="cli-1", tipo=tipo, estado=EstadoEvidencia.ACEPTADA, almacen_ref="r"
            )
        )
    vigencia = Vigencia(AHORA, AHORA + timedelta(days=365))

    emitida = await polizas.emitir(p.id, "SOL-VH-2026-AAAAAA", AHORA, vigencia)

    assert emitida.estado is EstadoPoliza.EMITIDA
    assert emitida.numero == "SOL-VH-2026-AAAAAA"
    assert emitida.vigencia == vigencia
    assert emitida.pendientes == ()
    assert {e.tipo for e in emitida.evidencias} == set(TipoEvidencia)
    assert all(e.estado is EstadoEvidencia.ACEPTADA for e in emitida.evidencias)
    for tipo in TipoEvidencia:
        assert await evidencias.vigente("cli-1", tipo) is None
    assert await polizas.obtener(p.id) == emitida


async def test_emitir_dos_veces_solo_emite_una(repos):
    polizas, _ = repos
    p = poliza(pendientes=())
    await polizas.crear(p)
    vigencia = Vigencia(AHORA, AHORA + timedelta(days=365))

    primera = await polizas.emitir(p.id, "SOL-VH-2026-AAAAAA", AHORA, vigencia)
    segunda = await polizas.emitir(p.id, "SOL-VH-2026-BBBBBB", AHORA, vigencia)

    assert primera is not None
    assert segunda is None
    assert (await polizas.obtener(p.id)).numero == "SOL-VH-2026-AAAAAA"


async def test_un_numero_repetido_no_emite_ni_consume(repos):
    polizas, evidencias = repos
    primera = poliza(pendientes=())
    segunda = poliza(cotizacion=AHORA + timedelta(days=1), pendientes=())
    await polizas.crear(primera)
    await polizas.crear(segunda)
    vigencia = Vigencia(AHORA, AHORA + timedelta(days=365))
    await polizas.emitir(primera.id, "SOL-VH-2026-AAAAAA", AHORA, vigencia)
    await evidencias.guardar_reemplazando(
        Evidencia(
            cliente_id="cli-1",
            tipo=TipoEvidencia.SELFIE,
            estado=EstadoEvidencia.ACEPTADA,
            almacen_ref="r",
        )
    )

    with pytest.raises(NumeroPolizaDuplicado):
        await polizas.emitir(segunda.id, "SOL-VH-2026-AAAAAA", AHORA, vigencia)

    assert (await polizas.obtener(segunda.id)).estado is EstadoPoliza.PENDIENTE
    assert await evidencias.vigente("cli-1", TipoEvidencia.SELFIE) is not None


async def test_aprobar_agrega_la_decision_mientras_siga_pendiente(repos):
    polizas, _ = repos
    p = poliza()
    await polizas.crear(p)
    decision = Decision(
        TipoDecision.APROBAR, "maria", (CodigoPendiente.CREDITO_NO_VERIFICADO,), AHORA, "nota"
    )

    assert await polizas.aprobar(p.id, decision)

    assert (await polizas.obtener(p.id)).decisiones == (decision,)
    assert not await polizas.aprobar("inexistente", decision)


async def test_rechazar_guarda_decision_motivos_y_estado(repos):
    polizas, _ = repos
    p = poliza()
    await polizas.crear(p)
    decision = Decision(
        TipoDecision.RECHAZAR, "maria", (CodigoPendiente.CREDITO_NO_VERIFICADO,), AHORA
    )
    motivos = (MotivoRechazoPoliza(CodigoPendiente.CREDITO_NO_VERIFICADO, "No verificamos"),)

    rechazada = await polizas.rechazar(p.id, decision, motivos, AHORA)

    assert rechazada.estado is EstadoPoliza.RECHAZADA
    assert rechazada.motivos_rechazo == motivos
    assert rechazada.rechazada_en == AHORA
    assert rechazada.decisiones == (decision,)
    assert await polizas.rechazar(p.id, decision, motivos, AHORA) is None
    assert not await polizas.aprobar(p.id, decision)


async def test_borrar_pendientes_respeta_la_cotizacion_vigente_y_las_ya_resueltas(repos):
    polizas, _ = repos
    vieja = poliza(cotizacion=AHORA)
    emitida = poliza(cotizacion=AHORA + timedelta(days=1), pendientes=())
    nueva = poliza(cotizacion=AHORA + timedelta(days=2))
    ajena = poliza(cliente_id="cli-2")
    for p in (vieja, emitida, nueva, ajena):
        await polizas.crear(p)
    await polizas.emitir(
        emitida.id, "SOL-VH-2026-AAAAAA", AHORA, Vigencia(AHORA, AHORA + timedelta(days=1))
    )

    borradas = await polizas.borrar_pendientes_de_cliente("cli-1", nueva.cotizacion_creada_en)

    assert borradas == 1
    assert await polizas.obtener(vieja.id) is None
    assert await polizas.obtener(emitida.id) is not None
    assert await polizas.obtener(nueva.id) is not None
    assert await polizas.obtener(ajena.id) is not None
    assert await polizas.borrar_pendientes_de_cliente("cli-1", None) == 1
    assert await polizas.obtener(nueva.id) is None


async def test_la_cola_filtra_ordena_y_pagina(repos):
    polizas, _ = repos
    ids = []
    for dia in range(3):
        p = poliza(
            cliente_id=f"cli-{dia}",
            cotizacion=AHORA,
            creada_en=AHORA + timedelta(hours=dia),
            pendientes=(
                (Pendiente.de(CodigoPendiente.CORREO_SIN_CONFIRMAR),)
                if dia == 1
                else (Pendiente.de(CodigoPendiente.CREDITO_NO_VERIFICADO),)
            ),
        )
        await polizas.crear(p)
        ids.append(p.id)
    despues = AHORA + timedelta(days=1)

    todas = await polizas.listar(FiltroPolizas(limite=2), despues)
    resto = await polizas.listar(FiltroPolizas(limite=2, cursor=todas.siguiente_cursor), despues)
    reves = await polizas.listar(FiltroPolizas(limite=2, mas_recientes_primero=True), despues)
    usuario = await polizas.listar(FiltroPolizas(tipo_pendiente=TipoPendiente.USUARIO), despues)
    codigo = await polizas.listar(
        FiltroPolizas(codigo_pendiente=CodigoPendiente.CREDITO_NO_VERIFICADO), despues
    )
    cliente = await polizas.listar(FiltroPolizas(cliente_id="cli-2"), despues)
    rango = await polizas.listar(
        FiltroPolizas(
            creada_desde=AHORA + timedelta(minutes=30),
            creada_hasta=AHORA + timedelta(hours=1, minutes=30),
        ),
        despues,
    )

    assert [p.id for p in todas.items] == ids[:2]
    assert [p.id for p in resto.items] == ids[2:]
    assert resto.siguiente_cursor is None
    assert [p.id for p in reves.items] == [ids[2], ids[1]]
    assert [p.id for p in usuario.items] == [ids[1]]
    assert [p.id for p in codigo.items] == [ids[0], ids[2]]
    assert [p.id for p in cliente.items] == [ids[2]]
    assert [p.id for p in rango.items] == [ids[1]]


async def test_la_cola_calcula_vencida_con_la_hora_que_se_le_pasa(repos):
    polizas, _ = repos
    p = poliza()
    await polizas.crear(p)
    dentro = AHORA + timedelta(days=29)
    fuera = AHORA + timedelta(days=31)

    assert (await polizas.listar(FiltroPolizas(estado=EstadoPoliza.PENDIENTE), dentro)).items
    assert not (await polizas.listar(FiltroPolizas(estado=EstadoPoliza.VENCIDA), dentro)).items
    assert not (await polizas.listar(FiltroPolizas(estado=EstadoPoliza.PENDIENTE), fuera)).items
    assert (await polizas.listar(FiltroPolizas(estado=EstadoPoliza.VENCIDA), fuera)).items
    assert not (await polizas.listar(FiltroPolizas(estado=EstadoPoliza.EMITIDA), fuera)).items


async def test_lo_guardado_sobrevive_a_un_reinicio_con_el_mismo_archivo(tmp_path):
    ruta = str(tmp_path / "pod.db")
    db = SqliteDatabase(ruta)
    await db.init()
    guardada = poliza()
    await SqlitePolizaRepository(db).crear(guardada)

    reinicio = SqliteDatabase(ruta)
    await reinicio.init()  # el esquema es idempotente

    assert await SqlitePolizaRepository(reinicio).obtener(guardada.id) == guardada
