"""Calibra los umbrales de la prueba de vida y de la comparación con la cédula.

Toma la foto del frente de la cédula de una persona y tres carpetas de selfies: las de esa
misma persona, las de otras personas (impostores) y los ataques (foto impresa, foto en la
pantalla de otro celular). Corre los mismos modelos que el servidor e imprime los dos puntajes
de cada foto y cómo se reparten, para elegir los umbrales de ``CORE_BIOMETRIA_*``.

    python tools/calibrar_biometria.py --frente cedula.jpg \\
        --propias fotos/propias --ajenas fotos/ajenas --ataques fotos/ataques

Se busca que casi ninguna selfie propia se rechace y que ninguna ajena ni ataque se acepte. Con
pocas fotos es un control de humo, no una medición formal. Para varias personas, repetir con el
frente de cada una. Las fotos son datos biométricos: no se suben al repositorio.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

from app.biometria import (
    LADO_ANALISIS,
    Modelos,
    ModelosOpenCV,
    UmbralesBiometria,
    verificar_selfie,
)
from app.calidad_imagen import UmbralesCalidad
from app.config import Settings
from app.domain import EstadoEvidencia
from app.imagen import cargar_bgr

_EXTENSIONES = {".jpg", ".jpeg", ".png"}
_GRUPOS = ("propias", "ajenas", "ataques")


def _fotos(carpeta: Path) -> list[Path]:
    return sorted(p for p in carpeta.iterdir() if p.suffix.lower() in _EXTENSIONES)


def puntajes(
    modelos: Modelos, selfie: bytes, frente: bytes
) -> tuple[int, float | None, float | None]:
    """Cantidad de rostros, vida y similitud con el frente, sin cortar en ningún umbral."""
    foto = cargar_bgr(selfie, LADO_ANALISIS)
    rostros = modelos.detectar(foto)
    if not rostros:
        return 0, None, None
    cedula = cargar_bgr(frente, LADO_ANALISIS)
    rostros_cedula = modelos.detectar(cedula)
    vida = modelos.vida(foto, rostros[0])
    if not rostros_cedula:
        return len(rostros), vida, None
    return len(rostros), vida, modelos.similitud(foto, rostros[0], cedula, rostros_cedula[0])


def _resumen(nombre: str, valores: list[float]) -> str:
    valores = sorted(valores)
    if not valores:
        return f"  {nombre:9s} (sin valores)"
    return (
        f"  {nombre:9s} n={len(valores):3d}  mín={valores[0]:6.3f}  "
        f"mediana={statistics.median(valores):6.3f}  máx={valores[-1]:6.3f}"
    )


def main(argv: list[str] | None = None, *, modelos: Modelos | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--frente", type=Path, required=True, help="foto del frente de la cédula")
    for grupo in _GRUPOS:
        parser.add_argument(f"--{grupo}", type=Path, required=True)
    parser.add_argument("--modelos", type=Path, default=None, help="carpeta de los modelos")
    args = parser.parse_args(argv)

    settings = Settings()
    umbrales = UmbralesBiometria.desde(settings)
    calidad = UmbralesCalidad.desde(settings)
    if modelos is None:
        modelos = ModelosOpenCV(
            args.modelos or settings.biometria_modelos_ruta, umbrales.deteccion_min
        )
    frente = args.frente.read_bytes()

    vidas: dict[str, list[float]] = {g: [] for g in _GRUPOS}
    similitudes: dict[str, list[float]] = {g: [] for g in _GRUPOS}
    aceptadas: dict[str, list[str]] = {g: [] for g in _GRUPOS}
    for grupo in _GRUPOS:
        print(f"\n== {grupo} ==")
        for foto in _fotos(getattr(args, grupo)):
            selfie = foto.read_bytes()
            rostros, vida, similitud = puntajes(modelos, selfie, frente)
            veredicto = verificar_selfie(
                selfie, frente, modelos=modelos, calidad=calidad, umbrales=umbrales
            )
            if vida is not None:
                vidas[grupo].append(vida)
            if similitud is not None:
                similitudes[grupo].append(similitud)
            if veredicto.estado is EstadoEvidencia.ACEPTADA:
                aceptadas[grupo].append(foto.name)
            vida_txt = "  -  " if vida is None else f"{vida:5.3f}"
            sim_txt = "  -  " if similitud is None else f"{similitud:5.3f}"
            print(
                f"  {foto.name:32s} rostros={rostros} vida={vida_txt} "
                f"coincidencia={sim_txt} -> {veredicto.motivo or veredicto.estado}"
            )

    print("\n== Distribución de los puntajes ==")
    print(f"\nvida  [aceptar >= {umbrales.vida_aceptar}, rechazar < {umbrales.vida_rechazar}]")
    for grupo in _GRUPOS:
        print(_resumen(grupo, vidas[grupo]))
    print(
        f"\ncoincidencia  [aceptar >= {umbrales.coincidencia_aceptar}, "
        f"rechazar < {umbrales.coincidencia_rechazar}]"
    )
    for grupo in _GRUPOS:
        print(_resumen(grupo, similitudes[grupo]))

    print(f"\nPropias aceptadas: {len(aceptadas['propias'])} de {len(_fotos(args.propias))}")
    for nombre in ("ajenas", "ataques"):
        print(
            f"{nombre.capitalize()} que se aceptarían (no debería haber): {len(aceptadas[nombre])}"
        )
        for archivo in aceptadas[nombre]:
            print(f"  - {archivo}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
