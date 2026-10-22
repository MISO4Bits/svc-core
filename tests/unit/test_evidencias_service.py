from __future__ import annotations

import asyncio
from datetime import date

import pytest

from app.adapters.almacen import InMemoryAlmacenEvidencias
from app.adapters.memory import InMemoryClienteRepository, InMemoryEvidenciaRepository
from app.domain import (
    Cliente,
    ClienteNoEncontrado,
    EstadoEvidencia,
    ImagenInvalida,
    ImagenMuyGrande,
    MotivoRechazo,
    TipoDocumento,
    TipoEvidencia,
    Veredicto,
)
from app.evidencias import EvidenciaService
from tests import imagenes

FRENTE, REVERSO, SELFIE = (
    TipoEvidencia.CEDULA_FRENTE,
    TipoEvidencia.CEDULA_REVERSO,
    TipoEvidencia.SELFIE,
)
ACEPTADA = Veredicto(EstadoEvidencia.ACEPTADA)
MANUAL = Veredicto(EstadoEvidencia.REVISION_MANUAL)
BORROSA = Veredicto(EstadoEvidencia.REINTENTAR, MotivoRechazo.BORROSA)
IMAGEN = imagenes.a_bytes(imagenes.sin_tarjeta())


class VerificadorFalso:
    """Responde lo que se le programe y recuerda con qué número esperado lo llamaron."""

    def __init__(self, *respuestas, demora_s: float = 0.0) -> None:
        self._respuestas = list(respuestas)
        self._demora_s = demora_s
        self.numeros_esperados: list[str | None] = []

    async def analizar(self, imagen, tipo, numero_esperado):
        self.numeros_esperados.append(numero_esperado)
        if self._demora_s:
            await asyncio.sleep(self._demora_s)
        respuesta = self._respuestas.pop(0) if len(self._respuestas) > 1 else self._respuestas[0]
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta


async def _cliente(clientes, tipo_documento=TipoDocumento.CC) -> Cliente:
    cliente = Cliente(
        identity_ref="idp-1",
        tipo_documento=tipo_documento,
        numero_documento="1032456789",
        primer_nombre="Ana",
        primer_apellido="Ríos",
        fecha_nacimiento=date(1991, 5, 20),
        email="ana@example.com",
        telefono="+573001234567",
    )
    return await clientes.crear_con_consentimientos(cliente, [])


@pytest.fixture
def clientes() -> InMemoryClienteRepository:
    return InMemoryClienteRepository()


@pytest.fixture
def almacen() -> InMemoryAlmacenEvidencias:
    return InMemoryAlmacenEvidencias()


def _servicio(clientes, almacen, verificador, *, timeout_s=2.0, max_bytes=3_000_000):
    return EvidenciaService(
        clientes,
        InMemoryEvidenciaRepository(),
        almacen,
        verificador,
        max_bytes=max_bytes,
        max_pixeles=40_000_000,
        intentos_max=3,
        timeout_s=timeout_s,
    )


async def test_una_foto_aceptada_se_guarda_como_evidencia_vigente(clientes, almacen):
    cliente = await _cliente(clientes)
    servicio = _servicio(clientes, almacen, VerificadorFalso(ACEPTADA))

    resultado = await servicio.subir(cliente.id, FRENTE, IMAGEN)

    assert resultado.estado is EstadoEvidencia.ACEPTADA
    assert resultado.motivo is None
    assert resultado.intentos_restantes == 3
    assert resultado.evidencia is not None
    assert resultado.evidencia.almacen_ref == (
        f"evidencias/{cliente.id}/cedula_frente/{resultado.evidencia.id}.jpg"
    )
    assert await almacen.obtener(resultado.evidencia.almacen_ref) == IMAGEN


async def test_el_cliente_inexistente_no_llega_al_verificador(clientes, almacen):
    verificador = VerificadorFalso(ACEPTADA)
    servicio = _servicio(clientes, almacen, verificador)

    with pytest.raises(ClienteNoEncontrado):
        await servicio.subir("no-existe", FRENTE, IMAGEN)

    assert verificador.numeros_esperados == []


async def test_un_archivo_que_no_es_imagen_no_llega_al_verificador(clientes, almacen):
    cliente = await _cliente(clientes)
    verificador = VerificadorFalso(ACEPTADA)
    servicio = _servicio(clientes, almacen, verificador)

    with pytest.raises(ImagenInvalida):
        await servicio.subir(cliente.id, FRENTE, b"%PDF-1.7")

    assert verificador.numeros_esperados == []


async def test_una_imagen_mas_grande_que_el_limite_se_rechaza(clientes, almacen):
    cliente = await _cliente(clientes)
    servicio = _servicio(clientes, almacen, VerificadorFalso(ACEPTADA), max_bytes=1000)

    with pytest.raises(ImagenMuyGrande):
        await servicio.subir(cliente.id, FRENTE, IMAGEN)


async def test_un_rechazo_no_guarda_nada_y_descuenta_un_intento(clientes, almacen):
    cliente = await _cliente(clientes)
    servicio = _servicio(clientes, almacen, VerificadorFalso(BORROSA))

    resultado = await servicio.subir(cliente.id, FRENTE, IMAGEN)

    assert resultado.estado is EstadoEvidencia.REINTENTAR
    assert resultado.motivo is MotivoRechazo.BORROSA
    assert resultado.intentos_restantes == 2
    assert resultado.evidencia is None
    assert not await servicio.evidencias_listas(cliente.id)
    assert almacen._objetos == {}


async def test_el_tercer_rechazo_pasa_a_revision_manual_y_guarda_esa_foto(clientes, almacen):
    cliente = await _cliente(clientes)
    servicio = _servicio(clientes, almacen, VerificadorFalso(BORROSA))

    primero = await servicio.subir(cliente.id, FRENTE, IMAGEN)
    segundo = await servicio.subir(cliente.id, FRENTE, IMAGEN)
    tercero = await servicio.subir(cliente.id, FRENTE, IMAGEN)

    assert [primero.intentos_restantes, segundo.intentos_restantes] == [2, 1]
    assert tercero.estado is EstadoEvidencia.REVISION_MANUAL
    assert tercero.motivo is None
    assert tercero.evidencia is not None
    assert await almacen.obtener(tercero.evidencia.almacen_ref) == IMAGEN  # una persona la verá


async def test_aceptar_reinicia_el_conteo_de_rechazos(clientes, almacen):
    cliente = await _cliente(clientes)
    servicio = _servicio(clientes, almacen, VerificadorFalso(BORROSA, BORROSA, ACEPTADA))

    await servicio.subir(cliente.id, FRENTE, IMAGEN)
    await servicio.subir(cliente.id, FRENTE, IMAGEN)
    aceptada = await servicio.subir(cliente.id, FRENTE, IMAGEN)

    assert aceptada.estado is EstadoEvidencia.ACEPTADA
    estado = {e.tipo: e for e in await servicio.estado(cliente.id)}
    assert estado[FRENTE].intentos_restantes == 3


async def test_los_rechazos_se_cuentan_por_tipo(clientes, almacen):
    cliente = await _cliente(clientes)
    servicio = _servicio(clientes, almacen, VerificadorFalso(BORROSA))

    await servicio.subir(cliente.id, FRENTE, IMAGEN)
    await servicio.subir(cliente.id, FRENTE, IMAGEN)
    reverso = await servicio.subir(cliente.id, REVERSO, IMAGEN)

    assert reverso.intentos_restantes == 2


async def test_repetir_una_foto_aceptada_reemplaza_a_la_anterior(clientes, almacen):
    cliente = await _cliente(clientes)
    servicio = _servicio(clientes, almacen, VerificadorFalso(ACEPTADA))

    primera = await servicio.subir(cliente.id, FRENTE, IMAGEN)
    segunda = await servicio.subir(cliente.id, FRENTE, IMAGEN)

    estado = {e.tipo: e for e in await servicio.estado(cliente.id)}
    assert estado[FRENTE].evidencia.id == segunda.evidencia.id != primera.evidencia.id


async def test_solo_el_reverso_de_una_cc_se_cruza_con_el_numero_registrado(clientes, almacen):
    cliente = await _cliente(clientes)
    verificador = VerificadorFalso(ACEPTADA)
    servicio = _servicio(clientes, almacen, verificador)

    await servicio.subir(cliente.id, FRENTE, IMAGEN)
    await servicio.subir(cliente.id, REVERSO, IMAGEN)
    await servicio.subir(cliente.id, SELFIE, IMAGEN)

    assert verificador.numeros_esperados == [None, "1032456789", None]


@pytest.mark.parametrize("tipo_documento", [TipoDocumento.CE, TipoDocumento.PA, TipoDocumento.NIT])
async def test_un_documento_que_no_es_cc_va_a_revision_manual_sin_cruce(
    clientes, almacen, tipo_documento
):
    cliente = await _cliente(clientes, tipo_documento)
    verificador = VerificadorFalso(ACEPTADA)
    servicio = _servicio(clientes, almacen, verificador)

    frente = await servicio.subir(cliente.id, FRENTE, IMAGEN)
    reverso = await servicio.subir(cliente.id, REVERSO, IMAGEN)

    assert frente.estado is reverso.estado is EstadoEvidencia.REVISION_MANUAL
    assert verificador.numeros_esperados == [None, None]  # no hay código que sepamos leer


async def test_la_calidad_se_sigue_aplicando_a_los_documentos_que_no_son_cc(clientes, almacen):
    cliente = await _cliente(clientes, TipoDocumento.CE)
    servicio = _servicio(clientes, almacen, VerificadorFalso(BORROSA))

    resultado = await servicio.subir(cliente.id, FRENTE, IMAGEN)

    assert resultado.estado is EstadoEvidencia.REINTENTAR


async def test_la_selfie_de_un_documento_que_no_es_cc_se_acepta(clientes, almacen):
    cliente = await _cliente(clientes, TipoDocumento.PA)
    servicio = _servicio(clientes, almacen, VerificadorFalso(ACEPTADA))

    resultado = await servicio.subir(cliente.id, SELFIE, IMAGEN)

    assert resultado.estado is EstadoEvidencia.ACEPTADA


async def test_si_el_verificador_tarda_demasiado_decide_una_persona(clientes, almacen):
    cliente = await _cliente(clientes)
    servicio = _servicio(
        clientes, almacen, VerificadorFalso(ACEPTADA, demora_s=1.0), timeout_s=0.05
    )

    resultado = await servicio.subir(cliente.id, FRENTE, IMAGEN)

    assert resultado.estado is EstadoEvidencia.REVISION_MANUAL
    assert resultado.evidencia is not None


async def test_si_el_verificador_falla_decide_una_persona(clientes, almacen):
    cliente = await _cliente(clientes)
    servicio = _servicio(clientes, almacen, VerificadorFalso(RuntimeError("se rompió")))

    resultado = await servicio.subir(cliente.id, FRENTE, IMAGEN)

    assert resultado.estado is EstadoEvidencia.REVISION_MANUAL


async def test_una_imagen_ilegible_para_el_verificador_no_se_esconde_tras_revision_manual(
    clientes, almacen
):
    cliente = await _cliente(clientes)
    servicio = _servicio(clientes, almacen, VerificadorFalso(ImagenInvalida("dañada")))

    with pytest.raises(ImagenInvalida):
        await servicio.subir(cliente.id, FRENTE, IMAGEN)


async def test_sin_subir_nada_los_tres_tipos_estan_pendientes(clientes, almacen):
    cliente = await _cliente(clientes)
    servicio = _servicio(clientes, almacen, VerificadorFalso(ACEPTADA))

    estado = await servicio.estado(cliente.id)

    assert [e.tipo for e in estado] == [FRENTE, REVERSO, SELFIE]
    assert all(e.evidencia is None and e.intentos_restantes == 3 for e in estado)


async def test_el_estado_de_un_cliente_inexistente_falla(clientes, almacen):
    servicio = _servicio(clientes, almacen, VerificadorFalso(ACEPTADA))

    with pytest.raises(ClienteNoEncontrado):
        await servicio.estado("no-existe")


async def test_la_compuerta_pide_ambas_caras(clientes, almacen):
    cliente = await _cliente(clientes)
    servicio = _servicio(clientes, almacen, VerificadorFalso(ACEPTADA))
    assert not await servicio.evidencias_listas(cliente.id)

    await servicio.subir(cliente.id, FRENTE, IMAGEN)
    assert not await servicio.evidencias_listas(cliente.id)

    await servicio.subir(cliente.id, REVERSO, IMAGEN)
    assert await servicio.evidencias_listas(cliente.id)


async def test_la_selfie_no_cuenta_para_la_compuerta(clientes, almacen):
    cliente = await _cliente(clientes)
    servicio = _servicio(clientes, almacen, VerificadorFalso(ACEPTADA))

    await servicio.subir(cliente.id, SELFIE, IMAGEN)

    assert not await servicio.evidencias_listas(cliente.id)


async def test_la_revision_manual_tambien_abre_la_compuerta(clientes, almacen):
    cliente = await _cliente(clientes)
    servicio = _servicio(clientes, almacen, VerificadorFalso(MANUAL))

    await servicio.subir(cliente.id, FRENTE, IMAGEN)
    await servicio.subir(cliente.id, REVERSO, IMAGEN)

    assert await servicio.evidencias_listas(cliente.id)


async def test_consumir_cierra_la_compuerta_y_no_se_reutiliza(clientes, almacen):
    cliente = await _cliente(clientes)
    servicio = _servicio(clientes, almacen, VerificadorFalso(ACEPTADA))
    await servicio.subir(cliente.id, FRENTE, IMAGEN)
    await servicio.subir(cliente.id, REVERSO, IMAGEN)

    assert await servicio.consumir(cliente.id, "poliza-1") == 2

    assert not await servicio.evidencias_listas(cliente.id)
    assert all(e.evidencia is None for e in await servicio.estado(cliente.id))
