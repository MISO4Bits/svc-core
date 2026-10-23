from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest

from app.biometria import Rostro
from tests import imagenes

RUTA = Path(__file__).resolve().parents[2] / "tools" / "calibrar_biometria.py"


@pytest.fixture(scope="module")
def calibrar():
    spec = importlib.util.spec_from_file_location("calibrar_biometria", RUTA)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _rostro() -> Rostro:
    fila = np.zeros(15, dtype=np.float32)
    fila[:4] = (200, 150, 300, 300)
    fila[14] = 0.95
    return Rostro(fila)


class ModelosPorNombre:
    """Reparte los puntajes por la semilla de la foto: la selfie "ajena" parece otra persona."""

    def detectar(self, imagen):
        return [_rostro()]

    def vida(self, imagen, rostro):
        return 0.95

    def similitud(self, a, rostro_a, b, rostro_b):
        # La foto "ajena" se hizo más clara a propósito: cambia su media y no se parece.
        return 0.10 if float(a.mean()) > 150 else 0.70


def _guardar(carpeta: Path, nombre: str, foto) -> None:
    carpeta.mkdir(exist_ok=True)
    (carpeta / nombre).write_bytes(imagenes.a_bytes(foto))


def test_imprime_los_puntajes_y_lo_que_se_aceptaria(calibrar, tmp_path, capsys):
    frente = tmp_path / "cedula.jpg"
    frente.write_bytes(imagenes.a_bytes(imagenes.sin_tarjeta(semilla=1)))
    _guardar(tmp_path / "propias", "yo.jpg", imagenes.sin_tarjeta(semilla=2))
    _guardar(tmp_path / "ajenas", "otra.jpg", imagenes.sobreexpuesta(imagenes.sin_tarjeta()))
    _guardar(tmp_path / "ataques", "pantalla.jpg", imagenes.sin_tarjeta(semilla=4))
    (tmp_path / "ataques" / "notas.txt").write_text("se ignora")

    codigo = calibrar.main(
        [
            "--frente",
            str(frente),
            "--propias",
            str(tmp_path / "propias"),
            "--ajenas",
            str(tmp_path / "ajenas"),
            "--ataques",
            str(tmp_path / "ataques"),
        ],
        modelos=ModelosPorNombre(),
    )

    salida = capsys.readouterr().out
    assert codigo == 0
    assert "yo.jpg" in salida and "otra.jpg" in salida and "pantalla.jpg" in salida
    assert "notas.txt" not in salida
    assert "Propias aceptadas: 1 de 1" in salida
    assert "Ajenas que se aceptarían (no debería haber): 0" in salida
    # El "ataque" del modelo falso se parece y tiene vida alta: el control de humo lo delata.
    assert "Ataques que se aceptarían (no debería haber): 1" in salida
    assert "pantalla.jpg" in salida.split("Ataques que se aceptarían")[1]


def test_los_puntajes_sin_rostro_quedan_vacios(calibrar):
    class SinRostros:
        def detectar(self, imagen):
            return []

    foto = imagenes.a_bytes(imagenes.sin_tarjeta())

    assert calibrar.puntajes(SinRostros(), foto, foto) == (0, None, None)


def test_sin_rostro_en_la_cedula_no_hay_similitud(calibrar):
    class SoloLaSelfie:
        def __init__(self) -> None:
            self.llamadas = 0

        def detectar(self, imagen):
            self.llamadas += 1
            return [_rostro()] if self.llamadas == 1 else []

        def vida(self, imagen, rostro):
            return 0.9

    foto = imagenes.a_bytes(imagenes.sin_tarjeta())

    assert calibrar.puntajes(SoloLaSelfie(), foto, foto) == (1, 0.9, None)
