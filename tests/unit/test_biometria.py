from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest

from app.adapters import verificador_biometrico as modulo_verificador
from app.adapters.verificador_biometrico import VerificadorBiometricoLocal
from app.adapters.verificador_mock import VerificadorBiometricoMock
from app.biometria import (
    ARCHIVOS_DE_MODELOS,
    ModelosOpenCV,
    Rostro,
    UmbralesBiometria,
    decidir,
    recorte_de_vida,
    verificar_selfie,
)
from app.calidad_imagen import UmbralesCalidad
from app.domain import EstadoEvidencia, MotivoRechazo, Veredicto
from tests import imagenes

CALIDAD = UmbralesCalidad()
UMBRALES = UmbralesBiometria()
SELFIE = imagenes.a_bytes(imagenes.sin_tarjeta(semilla=5))
FRENTE = imagenes.a_bytes(imagenes.sin_tarjeta(semilla=9))
MODELOS_REALES = Path(__file__).resolve().parents[2] / "models"
requiere_modelos = pytest.mark.skipif(
    not all((MODELOS_REALES / nombre).is_file() for nombre in ARCHIVOS_DE_MODELOS),
    reason="faltan los modelos (python tools/descargar_modelos.py)",
)


def _rostro(ancho: float = 300.0, x: float = 200.0, y: float = 150.0) -> Rostro:
    fila = np.zeros(15, dtype=np.float32)
    fila[:4] = (x, y, ancho, ancho)
    # Cinco puntos de referencia razonables dentro de la caja y la confianza.
    fila[4:14] = [
        x + ancho * 0.3, y + ancho * 0.35, x + ancho * 0.7, y + ancho * 0.35,
        x + ancho * 0.5, y + ancho * 0.55, x + ancho * 0.35, y + ancho * 0.8,
        x + ancho * 0.65, y + ancho * 0.8,
    ]  # fmt: skip
    fila[14] = 0.95
    return Rostro(fila)


class ModelosFalsos:
    """Devuelve lo programado: primero los rostros de la selfie y luego los de la cédula."""

    def __init__(self, selfie=(), cedula=(), *, vida=0.95, similitud=0.70) -> None:
        self._detecciones = [list(selfie), list(cedula)]
        self._vida = vida
        self._similitud = similitud
        self.llamadas: list[str] = []

    def detectar(self, imagen):
        self.llamadas.append("detectar")
        return self._detecciones.pop(0)

    def vida(self, imagen, rostro):
        self.llamadas.append("vida")
        return self._vida

    def similitud(self, a, rostro_a, b, rostro_b):
        self.llamadas.append("similitud")
        return self._similitud


def _verificar(modelos, selfie=SELFIE, frente=FRENTE) -> Veredicto:
    return verificar_selfie(selfie, frente, modelos=modelos, calidad=CALIDAD, umbrales=UMBRALES)


# ---------- decisión por umbrales ----------


@pytest.mark.parametrize(
    ("vida", "similitud", "estado", "motivo"),
    [
        (0.95, 0.70, EstadoEvidencia.ACEPTADA, None),
        (0.80, 0.40, EstadoEvidencia.ACEPTADA, None),  # justo en los umbrales superiores
        (0.39, 0.90, EstadoEvidencia.REINTENTAR, MotivoRechazo.POSIBLE_SUPLANTACION),
        (0.39, 0.10, EstadoEvidencia.REINTENTAR, MotivoRechazo.POSIBLE_SUPLANTACION),  # 1.º vida
        (0.95, 0.24, EstadoEvidencia.REINTENTAR, MotivoRechazo.NO_COINCIDE_CON_CEDULA),
        (0.60, 0.70, EstadoEvidencia.REVISION_MANUAL, None),  # vida en la zona gris
        (0.95, 0.30, EstadoEvidencia.REVISION_MANUAL, None),  # coincidencia en la zona gris
        (0.40, 0.25, EstadoEvidencia.REVISION_MANUAL, None),  # justo en los umbrales inferiores
    ],
)
def test_decidir_compara_los_puntajes_con_sus_dos_umbrales(vida, similitud, estado, motivo):
    veredicto = decidir(vida, similitud, UMBRALES)

    assert (veredicto.estado, veredicto.motivo) == (estado, motivo)
    assert veredicto.puntaje_vida == vida
    assert veredicto.puntaje_coincidencia == similitud


def test_los_umbrales_salen_de_la_configuracion():
    from app.config import Settings

    umbrales = UmbralesBiometria.desde(
        Settings(biometria_vida_aceptar=0.9, biometria_coincidencia_rechazar=0.1)
    )

    assert umbrales.vida_aceptar == 0.9
    assert umbrales.coincidencia_rechazar == 0.1
    assert umbrales.rostro_min == 0.15


# ---------- recorte para la prueba de vida ----------


def test_el_recorte_de_vida_siempre_es_de_80x80():
    imagen = np.zeros((600, 800, 3), dtype=np.uint8)

    assert recorte_de_vida(imagen, (300, 200, 100, 120), 2.7).shape == (80, 80, 3)


def test_el_recorte_se_desplaza_en_vez_de_deformarse_en_el_borde():
    # Un rostro pegado a la esquina: la ampliación se sale de la foto por arriba y a la izquierda.
    imagen = np.zeros((600, 800, 3), dtype=np.uint8)
    imagen[:300, :400] = 200  # cuadrante superior izquierdo claro

    recorte = recorte_de_vida(imagen, (0, 0, 100, 100), 4.0)

    assert recorte.shape == (80, 80, 3)
    assert recorte.mean() > 100  # el recorte arranca en la esquina, no se salió hacia el vacío


def test_la_ampliacion_se_limita_al_tamano_de_la_foto():
    imagen = np.zeros((100, 100, 3), dtype=np.uint8)

    assert recorte_de_vida(imagen, (10, 10, 80, 80), 4.0).shape == (80, 80, 3)


# ---------- flujo con modelos falsos ----------


def test_una_selfie_buena_que_coincide_se_acepta_con_sus_puntajes():
    modelos = ModelosFalsos([_rostro()], [_rostro(80)], vida=0.93, similitud=0.62)

    veredicto = _verificar(modelos)

    assert veredicto == Veredicto(
        EstadoEvidencia.ACEPTADA, puntaje_vida=0.93, puntaje_coincidencia=0.62
    )
    assert modelos.llamadas == ["detectar", "vida", "detectar", "similitud"]


def test_la_calidad_se_revisa_antes_de_gastar_los_modelos():
    modelos = ModelosFalsos()
    oscura = imagenes.a_bytes(imagenes.oscura(imagenes.sin_tarjeta()))

    veredicto = _verificar(modelos, selfie=oscura)

    assert veredicto == Veredicto(EstadoEvidencia.REINTENTAR, MotivoRechazo.OSCURA)
    assert modelos.llamadas == []


def test_sin_rostro_se_pide_repetir():
    modelos = ModelosFalsos([])

    assert _verificar(modelos) == Veredicto(EstadoEvidencia.REINTENTAR, MotivoRechazo.SIN_ROSTRO)
    assert modelos.llamadas == ["detectar"]


def test_un_rostro_diminuto_cuenta_como_sin_rostro():
    modelos = ModelosFalsos([_rostro(ancho=60)])

    assert _verificar(modelos).motivo is MotivoRechazo.SIN_ROSTRO
    assert modelos.llamadas == ["detectar"]


def test_dos_rostros_grandes_se_rechazan():
    modelos = ModelosFalsos([_rostro(300), _rostro(250, x=700)])

    assert _verificar(modelos).motivo is MotivoRechazo.VARIOS_ROSTROS
    assert modelos.llamadas == ["detectar"]


def test_un_rostro_pequeno_al_fondo_no_cuenta_como_otra_persona():
    modelos = ModelosFalsos([_rostro(300), _rostro(120, x=900)], [_rostro(80)])

    assert _verificar(modelos).estado is EstadoEvidencia.ACEPTADA


def test_una_vida_baja_se_rechaza_sin_comparar_con_la_cedula():
    modelos = ModelosFalsos([_rostro()], vida=0.10)

    veredicto = _verificar(modelos)

    assert veredicto == Veredicto(
        EstadoEvidencia.REINTENTAR, MotivoRechazo.POSIBLE_SUPLANTACION, puntaje_vida=0.10
    )
    assert modelos.llamadas == ["detectar", "vida"]


def test_sin_rostro_en_la_cedula_decide_una_persona_y_no_se_culpa_al_usuario():
    modelos = ModelosFalsos([_rostro()], [], vida=0.93)

    veredicto = _verificar(modelos)

    assert veredicto == Veredicto(EstadoEvidencia.REVISION_MANUAL, puntaje_vida=0.93)
    assert "similitud" not in modelos.llamadas


def test_una_coincidencia_baja_se_rechaza():
    modelos = ModelosFalsos([_rostro()], [_rostro(80)], similitud=0.05)

    assert _verificar(modelos).motivo is MotivoRechazo.NO_COINCIDE_CON_CEDULA


def test_una_coincidencia_en_la_zona_gris_va_a_revision_manual():
    modelos = ModelosFalsos([_rostro()], [_rostro(80)], similitud=0.32)

    assert _verificar(modelos).estado is EstadoEvidencia.REVISION_MANUAL


# ---------- modelos reales de OpenCV ----------


def test_faltan_modelos_y_el_error_dice_cuales(tmp_path):
    with pytest.raises(FileNotFoundError, match="descargar_modelos"):
        ModelosOpenCV(tmp_path, 0.7)


@requiere_modelos
def test_los_modelos_reales_cargan_y_no_ven_rostros_en_una_foto_sin_ellos():
    modelos = ModelosOpenCV(MODELOS_REALES, 0.7)
    foto = np.full((480, 640, 3), 128, dtype=np.uint8)

    assert modelos.detectar(foto) == []


@requiere_modelos
def test_la_vida_real_es_una_probabilidad():
    modelos = ModelosOpenCV(MODELOS_REALES, 0.7)
    rng = np.random.default_rng(1)
    foto = rng.integers(0, 255, (480, 640, 3), dtype=np.uint8)

    assert 0.0 <= modelos.vida(foto, _rostro()) <= 1.0


@requiere_modelos
def test_un_rostro_se_parece_totalmente_a_si_mismo():
    modelos = ModelosOpenCV(MODELOS_REALES, 0.7)
    rng = np.random.default_rng(2)
    foto = rng.integers(0, 255, (480, 640, 3), dtype=np.uint8)
    rostro = _rostro()

    assert modelos.similitud(foto, rostro, foto, rostro) == pytest.approx(1.0, abs=1e-4)


@requiere_modelos
def test_el_flujo_completo_con_modelos_reales_corta_en_sin_rostro():
    foto = imagenes.a_bytes(imagenes.sin_tarjeta())
    modelos = ModelosOpenCV(MODELOS_REALES, 0.7)

    assert _verificar(modelos, selfie=foto).motivo is MotivoRechazo.SIN_ROSTRO


# ---------- verificador local (asíncrono) ----------


def _local(modelos, *, max_concurrentes=1, ruta="no-importa") -> VerificadorBiometricoLocal:
    return VerificadorBiometricoLocal(
        CALIDAD, UMBRALES, ruta_modelos=ruta, max_concurrentes=max_concurrentes, modelos=modelos
    )


async def test_el_verificador_local_decide_en_un_hilo_aparte():
    verificador = _local(ModelosFalsos([_rostro()], [_rostro(80)]))

    veredicto = await verificador.verificar(SELFIE, FRENTE)

    assert veredicto.estado is EstadoEvidencia.ACEPTADA


async def test_varios_analisis_a_la_vez_terminan_aunque_el_cupo_sea_uno():
    class Constante(ModelosFalsos):
        def detectar(self, imagen):
            return [_rostro()]

    verificador = _local(Constante(), max_concurrentes=1)

    resultados = await asyncio.gather(*(verificador.verificar(SELFIE, FRENTE) for _ in range(3)))

    assert [r.estado for r in resultados] == [EstadoEvidencia.ACEPTADA] * 3


def test_precargar_con_modelos_ya_dados_no_toca_el_disco():
    _local(ModelosFalsos()).precargar()


def test_precargar_falla_si_faltan_modelos(tmp_path):
    with pytest.raises(FileNotFoundError):
        VerificadorBiometricoLocal(
            CALIDAD, UMBRALES, ruta_modelos=str(tmp_path), max_concurrentes=1
        ).precargar()


def test_los_modelos_se_cargan_una_sola_vez(monkeypatch):
    creados = []

    class Falso(ModelosFalsos):
        def __init__(self, ruta, deteccion_min):
            super().__init__()
            creados.append((ruta, deteccion_min))

    monkeypatch.setattr(modulo_verificador, "ModelosOpenCV", Falso)
    verificador = VerificadorBiometricoLocal(
        CALIDAD, UMBRALES, ruta_modelos="/modelos", max_concurrentes=1
    )

    verificador.precargar()
    verificador.precargar()

    assert creados == [("/modelos", UMBRALES.deteccion_min)]


# ---------- mock ----------


async def test_el_mock_biometrico_recorre_su_secuencia_sin_mirar_las_imagenes():
    mock = VerificadorBiometricoMock("POSIBLE_SUPLANTACION, ACEPTADA")

    resultados = [await mock.verificar(b"x", b"y") for _ in range(3)]

    assert [r.estado for r in resultados] == [
        EstadoEvidencia.REINTENTAR,
        EstadoEvidencia.ACEPTADA,
        EstadoEvidencia.REINTENTAR,
    ]
    assert resultados[0].motivo is MotivoRechazo.POSIBLE_SUPLANTACION


def test_el_mock_biometrico_falla_al_arrancar_con_un_motivo_que_no_existe():
    with pytest.raises(ValueError):
        VerificadorBiometricoMock("NO_EXISTE")
