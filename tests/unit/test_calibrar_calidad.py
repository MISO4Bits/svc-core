from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from tests import imagenes

RUTA = Path(__file__).resolve().parents[2] / "tools" / "calibrar_calidad.py"


@pytest.fixture(scope="module")
def calibrar():
    spec = importlib.util.spec_from_file_location("calibrar_calidad", RUTA)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def test_imprime_la_distribucion_y_los_falsos_rechazos(calibrar, tmp_path, capsys):
    buenas, malas = tmp_path / "buenas", tmp_path / "malas"
    buenas.mkdir()
    malas.mkdir()
    (buenas / "ok.jpg").write_bytes(imagenes.a_bytes(imagenes.foto_de_tarjeta()))
    (buenas / "dudosa.jpg").write_bytes(
        imagenes.a_bytes(imagenes.borrosa(imagenes.foto_de_tarjeta()))
    )
    (malas / "movida.jpg").write_bytes(
        imagenes.a_bytes(imagenes.borrosa(imagenes.foto_de_tarjeta()))
    )
    (malas / "notas.txt").write_text("se ignora")

    codigo = calibrar.main(["--buenas", str(buenas), "--malas", str(malas)])

    salida = capsys.readouterr().out
    assert codigo == 0
    assert "ok.jpg" in salida and "movida.jpg" in salida
    assert "notas.txt" not in salida
    assert "falsos rechazos): 1" in salida  # la "buena" borrosa
    assert "Malas que se aceptarían: 0" in salida


def test_con_carpetas_vacias_no_falla(calibrar, tmp_path, capsys):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()

    assert calibrar.main(["--buenas", str(tmp_path / "a"), "--malas", str(tmp_path / "b")]) == 0

    assert "(sin fotos)" in capsys.readouterr().out
