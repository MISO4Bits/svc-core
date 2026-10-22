"""La medida se mueve en la dirección correcta y la decisión usa el motivo adecuado.

No prueban dónde poner los cortes (eso exige fotos reales): prueban que cada defecto
empeora justo su medida y se traduce en su motivo.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.calidad_imagen import (
    UmbralesCalidad,
    decidir,
    detectar_tarjeta,
    luminosidad,
    medir,
    nitidez,
    reflejo,
)
from app.config import Settings
from app.domain import MotivoRechazo
from app.imagen import cargar_gris
from tests import imagenes

U = UmbralesCalidad()


def _medir(foto, *, con_tarjeta=True):
    return medir(imagenes.a_bytes(foto), U, con_tarjeta=con_tarjeta)


def test_los_umbrales_salen_de_la_configuracion():
    umbrales = UmbralesCalidad.desde(
        Settings(calidad_nitidez_min=99.0, calidad_reflejo_max=0.1, calidad_lado_min_px=800)
    )

    assert umbrales.nitidez_min == 99.0
    assert umbrales.reflejo_max == 0.1
    assert umbrales.lado_min_px == 800
    assert umbrales.proporcion_tarjeta == 1.586


def test_desenfocar_baja_la_nitidez():
    nitida = _medir(imagenes.foto_de_tarjeta())
    borrosa = _medir(imagenes.borrosa(imagenes.foto_de_tarjeta()))

    assert borrosa.nitidez < nitida.nitidez / 10


def test_oscurecer_baja_la_luz_y_sube_los_pixeles_oscuros():
    normal = _medir(imagenes.foto_de_tarjeta())
    oscura = _medir(imagenes.oscura(imagenes.foto_de_tarjeta()))

    assert oscura.luz_media < normal.luz_media / 2
    assert oscura.oscuros > normal.oscuros


def test_la_media_sola_no_basta_para_detectar_una_foto_partida():
    mitades = np.zeros((100, 100), dtype=np.uint8)
    mitades[:, 50:] = 255

    media, oscuros, quemados = luminosidad(mitades)

    assert 120 < media < 135  # "bien" en promedio
    assert oscuros == pytest.approx(0.5)
    assert quemados == pytest.approx(0.5)


def test_una_mancha_blanca_sube_el_reflejo():
    sin = _medir(imagenes.foto_de_tarjeta())
    con = _medir(imagenes.con_reflejo(imagenes.foto_de_tarjeta()))

    assert sin.reflejo == 0
    assert con.reflejo > U.reflejo_max


def test_reflejo_es_cero_sin_pixeles_casi_blancos():
    assert reflejo(np.full((50, 50), 120, dtype=np.uint8)) == 0.0


def test_nitidez_de_una_imagen_lisa_es_cero():
    assert nitidez(np.full((50, 50), 120, dtype=np.uint8)) == 0.0


def test_halla_la_tarjeta_con_su_proporcion():
    gris, _ = cargar_gris(imagenes.a_bytes(imagenes.foto_de_tarjeta()), 1000)

    tarjeta = detectar_tarjeta(gris, U)

    assert tarjeta is not None
    assert tarjeta.proporcion == pytest.approx(1.586, abs=0.05)
    assert tarjeta.proporcion_valida
    assert not tarjeta.toca_borde


def test_no_halla_tarjeta_en_una_imagen_lisa():
    assert detectar_tarjeta(np.full((600, 800), 120, dtype=np.uint8), U) is None


def test_una_foto_buena_no_tiene_motivo():
    assert decidir(_medir(imagenes.foto_de_tarjeta()), U, con_tarjeta=True) is None


@pytest.mark.parametrize(
    ("foto", "motivo"),
    [
        (imagenes.borrosa(imagenes.foto_de_tarjeta()), MotivoRechazo.BORROSA),
        (imagenes.oscura(imagenes.foto_de_tarjeta()), MotivoRechazo.OSCURA),
        (imagenes.con_reflejo(imagenes.foto_de_tarjeta()), MotivoRechazo.REFLEJO),
        (imagenes.sobreexpuesta(imagenes.foto_de_tarjeta()), MotivoRechazo.REFLEJO),
        (imagenes.foto_de_tarjeta(desplazamiento=(560, 0)), MotivoRechazo.RECORTADA),
        (imagenes.sin_tarjeta(), MotivoRechazo.NO_ES_CEDULA),
        (imagenes.foto_de_tarjeta(ancho_tarjeta=520), MotivoRechazo.BORROSA),  # muy lejos
    ],
    ids=["borrosa", "oscura", "reflejo", "sobreexpuesta", "recortada", "sin-tarjeta", "lejos"],
)
def test_cada_defecto_da_su_motivo(foto, motivo):
    assert decidir(_medir(foto), U, con_tarjeta=True) is motivo


def test_una_foto_borrosa_no_se_confunde_con_no_es_cedula():
    # El desenfoque borra el contorno: no se halla la tarjeta, pero el motivo es la nitidez.
    medidas = _medir(imagenes.borrosa(imagenes.foto_de_tarjeta()))

    assert medidas.tarjeta is None
    assert decidir(medidas, U, con_tarjeta=True) is MotivoRechazo.BORROSA


def test_la_luz_se_revisa_antes_que_la_forma():
    # Una foto oscura rompe la detección de la tarjeta: no debe decir "no es cédula".
    medidas = _medir(imagenes.oscura(imagenes.foto_de_tarjeta()))

    assert decidir(medidas, U, con_tarjeta=True) is MotivoRechazo.OSCURA


def test_una_foto_con_muy_pocos_pixeles_se_pide_de_nuevo():
    chica = imagenes.foto_de_tarjeta().resize((500, 375))

    assert decidir(_medir(chica), U, con_tarjeta=True) is MotivoRechazo.BORROSA


def test_sin_tarjeta_solo_se_revisa_calidad():
    # La selfie no tiene tarjeta: una foto sin rectángulo y nítida es válida.
    medidas = _medir(imagenes.sin_tarjeta(), con_tarjeta=False)

    assert medidas.tarjeta is None
    assert decidir(medidas, U, con_tarjeta=False) is None


def test_los_umbrales_cambian_la_decision_sin_tocar_el_algoritmo():
    medidas = _medir(imagenes.foto_de_tarjeta())
    estricta = UmbralesCalidad(nitidez_min=1e9)

    assert decidir(medidas, estricta, con_tarjeta=True) is MotivoRechazo.BORROSA
