from __future__ import annotations

import asyncio

import pytest

from app.adapters.verificador_local import VerificadorLocal
from app.adapters.verificador_mock import VerificadorMock
from app.calidad_imagen import UmbralesCalidad
from app.domain import EstadoEvidencia, MotivoRechazo, TipoEvidencia, Veredicto
from tests import imagenes

FRENTE, REVERSO, SELFIE = (
    TipoEvidencia.CEDULA_FRENTE,
    TipoEvidencia.CEDULA_REVERSO,
    TipoEvidencia.SELFIE,
)
NUMERO = "1032456789"
CODIGO = b"PubDSK_1\x00\x00001032456789RIOS     ANA\x00"
ACEPTADA = Veredicto(EstadoEvidencia.ACEPTADA)
MANUAL = Veredicto(EstadoEvidencia.REVISION_MANUAL)


# ---------- mock ----------


async def test_el_mock_acepta_por_defecto():
    assert await VerificadorMock().analizar(b"x", FRENTE, None) == ACEPTADA


async def test_el_mock_recorre_la_secuencia_en_rotacion():
    mock = VerificadorMock("BORROSA, ACEPTADA ,REVISION_MANUAL")

    resultados = [await mock.analizar(b"x", FRENTE, None) for _ in range(4)]

    assert resultados == [
        Veredicto(EstadoEvidencia.REINTENTAR, MotivoRechazo.BORROSA),
        ACEPTADA,
        MANUAL,
        Veredicto(EstadoEvidencia.REINTENTAR, MotivoRechazo.BORROSA),
    ]


def test_el_mock_falla_al_arrancar_si_un_nombre_no_existe():
    with pytest.raises(ValueError):
        VerificadorMock("ACEPTADA,INVENTADA")


@pytest.mark.parametrize("secuencia", ["", " , "])
def test_el_mock_exige_una_secuencia(secuencia):
    with pytest.raises(ValueError, match="vacía"):
        VerificadorMock(secuencia)


# ---------- local ----------


@pytest.fixture
def local() -> VerificadorLocal:
    return VerificadorLocal(UmbralesCalidad(), max_concurrentes=2)


def _foto(**kw) -> bytes:
    return imagenes.a_bytes(imagenes.foto_de_tarjeta(**kw))


async def test_el_frente_nitido_se_acepta(local):
    assert await local.analizar(_foto(), FRENTE, None) == ACEPTADA


async def test_una_foto_defectuosa_pide_repetir_con_su_motivo(local):
    borrosa = imagenes.a_bytes(imagenes.borrosa(imagenes.foto_de_tarjeta()))

    veredicto = await local.analizar(borrosa, FRENTE, None)

    assert veredicto == Veredicto(EstadoEvidencia.REINTENTAR, MotivoRechazo.BORROSA)


async def test_el_reverso_con_el_numero_registrado_se_acepta(local):
    assert await local.analizar(_foto(codigo=CODIGO), REVERSO, NUMERO) == ACEPTADA


async def test_el_reverso_con_otro_numero_va_a_revision_manual(local):
    assert await local.analizar(_foto(codigo=CODIGO), REVERSO, "1099999999") == MANUAL


async def test_el_reverso_sin_codigo_legible_va_a_revision_manual(local):
    assert await local.analizar(_foto(), REVERSO, NUMERO) == MANUAL


async def test_sin_numero_esperado_no_se_cruza(local):
    # Documentos que no son cédula de ciudadanía: solo calidad.
    assert await local.analizar(_foto(), REVERSO, None) == ACEPTADA


async def test_el_frente_no_lee_el_codigo(local):
    assert await local.analizar(_foto(), FRENTE, NUMERO) == ACEPTADA


async def test_la_selfie_solo_pasa_la_revision_de_calidad(local):
    nitida = imagenes.a_bytes(imagenes.sin_tarjeta())

    assert await local.analizar(nitida, SELFIE, None) == ACEPTADA


async def test_varios_analisis_a_la_vez_terminan_aunque_el_cupo_sea_uno():
    uno = VerificadorLocal(UmbralesCalidad(), max_concurrentes=1)

    resultados = await asyncio.gather(*(uno.analizar(_foto(), FRENTE, None) for _ in range(3)))

    assert resultados == [ACEPTADA] * 3
