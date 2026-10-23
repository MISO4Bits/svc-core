"""La selfie (BITS-235): se compara con el frente de la cédula ya guardado."""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from app.adapters.almacen import InMemoryAlmacenEvidencias
from app.adapters.memory import InMemoryClienteRepository, InMemoryEvidenciaRepository
from app.domain import (
    CedulaIncompleta,
    Cliente,
    EstadoEvidencia,
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
FOTO_FRENTE = imagenes.a_bytes(imagenes.sin_tarjeta(semilla=1))
FOTO_REVERSO = imagenes.a_bytes(imagenes.sin_tarjeta(semilla=2))
FOTO_SELFIE = imagenes.a_bytes(imagenes.sin_tarjeta(semilla=3))
SUPLANTACION = Veredicto(
    EstadoEvidencia.REINTENTAR, MotivoRechazo.POSIBLE_SUPLANTACION, puntaje_vida=0.12
)
BUENA = Veredicto(EstadoEvidencia.ACEPTADA, puntaje_vida=0.93, puntaje_coincidencia=0.61)


class DocumentosFalsos:
    async def analizar(self, imagen, tipo, numero_esperado):
        return ACEPTADA


class BiometriaFalsa:
    def __init__(self, *respuestas, demora_s: float = 0.0) -> None:
        self._respuestas = list(respuestas)
        self._demora_s = demora_s
        self.llamadas: list[tuple[bytes, bytes]] = []

    async def verificar(self, selfie, frente_cedula):
        self.llamadas.append((selfie, frente_cedula))
        if self._demora_s:
            await asyncio.sleep(self._demora_s)
        respuesta = self._respuestas.pop(0) if len(self._respuestas) > 1 else self._respuestas[0]
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta


class AlmacenQueFalla(InMemoryAlmacenEvidencias):
    fallar = False

    async def obtener(self, referencia):
        if self.fallar:
            raise OSError("bucket no disponible")
        return await super().obtener(referencia)


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
def almacen() -> AlmacenQueFalla:
    return AlmacenQueFalla()


def _servicio(clientes, almacen, biometria, *, timeout_s=1.0) -> EvidenciaService:
    return EvidenciaService(
        clientes,
        InMemoryEvidenciaRepository(),
        almacen,
        DocumentosFalsos(),
        max_bytes=3_000_000,
        max_pixeles=40_000_000,
        intentos_max=3,
        timeout_s=2.0,
        biometria=biometria,
        biometria_timeout_s=timeout_s,
    )


async def _con_cedula(servicio, cliente) -> None:
    await servicio.subir(cliente.id, FRENTE, FOTO_FRENTE)
    await servicio.subir(cliente.id, REVERSO, FOTO_REVERSO)


async def test_sin_la_cedula_la_selfie_no_se_verifica_ni_gasta_un_intento(clientes, almacen):
    cliente = await _cliente(clientes)
    biometria = BiometriaFalsa(BUENA)
    servicio = _servicio(clientes, almacen, biometria)

    with pytest.raises(CedulaIncompleta):
        await servicio.subir(cliente.id, SELFIE, FOTO_SELFIE)

    assert biometria.llamadas == []
    estado = {e.tipo: e for e in await servicio.estado(cliente.id)}
    assert estado[SELFIE].intentos_restantes == 3


async def test_con_solo_el_frente_tampoco_se_sube_la_selfie(clientes, almacen):
    cliente = await _cliente(clientes)
    servicio = _servicio(clientes, almacen, BiometriaFalsa(BUENA))
    await servicio.subir(cliente.id, FRENTE, FOTO_FRENTE)

    with pytest.raises(CedulaIncompleta):
        await servicio.subir(cliente.id, SELFIE, FOTO_SELFIE)


async def test_la_selfie_se_compara_con_el_frente_guardado_y_guarda_los_puntajes(clientes, almacen):
    cliente = await _cliente(clientes)
    biometria = BiometriaFalsa(BUENA)
    servicio = _servicio(clientes, almacen, biometria)
    await _con_cedula(servicio, cliente)

    resultado = await servicio.subir(cliente.id, SELFIE, FOTO_SELFIE)

    assert biometria.llamadas == [(FOTO_SELFIE, FOTO_FRENTE)]
    assert resultado.estado is EstadoEvidencia.ACEPTADA
    assert resultado.evidencia is not None
    assert resultado.evidencia.puntaje_vida == 0.93
    assert resultado.evidencia.puntaje_coincidencia == 0.61
    assert await almacen.obtener(resultado.evidencia.almacen_ref) == FOTO_SELFIE


async def test_una_selfie_rechazada_no_se_guarda_y_descuenta_un_intento(clientes, almacen):
    cliente = await _cliente(clientes)
    servicio = _servicio(clientes, almacen, BiometriaFalsa(SUPLANTACION))
    await _con_cedula(servicio, cliente)

    resultado = await servicio.subir(cliente.id, SELFIE, FOTO_SELFIE)

    assert resultado.estado is EstadoEvidencia.REINTENTAR
    assert resultado.motivo is MotivoRechazo.POSIBLE_SUPLANTACION
    assert resultado.intentos_restantes == 2
    assert resultado.evidencia is None


async def test_el_tercer_rechazo_guarda_la_selfie_con_sus_puntajes_para_una_persona(
    clientes, almacen
):
    cliente = await _cliente(clientes)
    servicio = _servicio(clientes, almacen, BiometriaFalsa(SUPLANTACION))
    await _con_cedula(servicio, cliente)

    await servicio.subir(cliente.id, SELFIE, FOTO_SELFIE)
    await servicio.subir(cliente.id, SELFIE, FOTO_SELFIE)
    tercero = await servicio.subir(cliente.id, SELFIE, FOTO_SELFIE)

    assert tercero.estado is EstadoEvidencia.REVISION_MANUAL
    assert tercero.motivo is None
    assert tercero.evidencia is not None
    assert tercero.evidencia.puntaje_vida == 0.12


async def test_si_la_verificacion_tarda_demasiado_decide_una_persona(clientes, almacen):
    cliente = await _cliente(clientes)
    servicio = _servicio(clientes, almacen, BiometriaFalsa(BUENA, demora_s=0.5), timeout_s=0.05)
    await _con_cedula(servicio, cliente)

    resultado = await servicio.subir(cliente.id, SELFIE, FOTO_SELFIE)

    assert resultado.estado is EstadoEvidencia.REVISION_MANUAL
    assert resultado.evidencia is not None
    assert resultado.evidencia.puntaje_vida is None


async def test_si_la_verificacion_falla_decide_una_persona(clientes, almacen):
    cliente = await _cliente(clientes)
    servicio = _servicio(clientes, almacen, BiometriaFalsa(RuntimeError("modelo roto")))
    await _con_cedula(servicio, cliente)

    resultado = await servicio.subir(cliente.id, SELFIE, FOTO_SELFIE)

    assert resultado.estado is EstadoEvidencia.REVISION_MANUAL


async def test_si_no_se_puede_leer_el_frente_guardado_decide_una_persona(clientes, almacen):
    cliente = await _cliente(clientes)
    biometria = BiometriaFalsa(BUENA)
    servicio = _servicio(clientes, almacen, biometria)
    await _con_cedula(servicio, cliente)
    almacen.fallar = True

    resultado = await servicio.subir(cliente.id, SELFIE, FOTO_SELFIE)

    assert resultado.estado is EstadoEvidencia.REVISION_MANUAL
    assert biometria.llamadas == []


async def test_un_documento_que_no_es_cc_tambien_puede_subir_la_selfie(clientes, almacen):
    # Sus caras quedan en REVISION_MANUAL (no se cruza el número), que cuenta como vigente.
    cliente = await _cliente(clientes, TipoDocumento.CE)
    servicio = _servicio(clientes, almacen, BiometriaFalsa(BUENA))
    await _con_cedula(servicio, cliente)

    resultado = await servicio.subir(cliente.id, SELFIE, FOTO_SELFIE)

    assert resultado.estado is EstadoEvidencia.ACEPTADA


async def test_las_caras_de_la_cedula_no_pasan_por_el_verificador_biometrico(clientes, almacen):
    cliente = await _cliente(clientes)
    biometria = BiometriaFalsa(BUENA)
    servicio = _servicio(clientes, almacen, biometria)

    await _con_cedula(servicio, cliente)

    assert biometria.llamadas == []


async def test_sin_verificador_biometrico_la_selfie_solo_pasa_el_de_documentos(clientes, almacen):
    cliente = await _cliente(clientes)
    servicio = _servicio(clientes, almacen, None)

    resultado = await servicio.subir(cliente.id, SELFIE, FOTO_SELFIE)

    assert resultado.estado is EstadoEvidencia.ACEPTADA
