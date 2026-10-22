"""Calibra los umbrales de calidad con fotos reales.

Corre las mismas medidas que el verificador sobre dos carpetas, una de fotos buenas y
otra de malas (movidas, oscuras, con reflejo, cortadas), e imprime cómo se reparten para
elegir los umbrales. Apunta a que casi ninguna foto buena se rechace: un falso rechazo
cuesta más que dejar pasar una dudosa, que igual termina en revisión manual.

    python tools/calibrar_calidad.py --buenas fotos/buenas --malas fotos/malas

Las fotos de cédulas son datos personales: no se suben al repositorio.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

from app.calidad_imagen import Medidas, UmbralesCalidad, decidir, medir
from app.config import Settings

_EXTENSIONES = {".jpg", ".jpeg", ".png"}
_METRICAS = {
    "nitidez": ("nitidez", "más alta = más nítida; umbral CORE_CALIDAD_NITIDEZ_MIN"),
    "luz_media": ("luz_media", "umbrales CORE_CALIDAD_LUZ_MEDIA_MIN / _MAX"),
    "quemados": ("quemados", "umbral CORE_CALIDAD_QUEMADOS_MAX"),
    "oscuros": ("oscuros", "umbral CORE_CALIDAD_OSCUROS_MAX"),
    "reflejo": ("reflejo", "umbral CORE_CALIDAD_REFLEJO_MAX"),
}


def _fotos(carpeta: Path) -> list[Path]:
    return sorted(p for p in carpeta.iterdir() if p.suffix.lower() in _EXTENSIONES)


def _medir_carpeta(
    carpeta: Path, umbrales: UmbralesCalidad, con_tarjeta: bool
) -> list[tuple[Path, Medidas]]:
    return [(f, medir(f.read_bytes(), umbrales, con_tarjeta=con_tarjeta)) for f in _fotos(carpeta)]


def _resumen(nombre: str, medidas: list[Medidas], atributo: str) -> str:
    valores = sorted(getattr(m, atributo) for m in medidas)
    if not valores:
        return f"  {nombre:10s} (sin fotos)"
    p10 = valores[max(0, int(len(valores) * 0.1) - 1)]
    return (
        f"  {nombre:10s} n={len(valores):3d}  mín={valores[0]:9.3f}  p10={p10:9.3f}  "
        f"mediana={statistics.median(valores):9.3f}  máx={valores[-1]:9.3f}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--buenas", type=Path, required=True)
    parser.add_argument("--malas", type=Path, required=True)
    parser.add_argument(
        "--selfie", action="store_true", help="no busca la tarjeta (fotos de rostro)"
    )
    args = parser.parse_args(argv)

    umbrales = UmbralesCalidad.desde(Settings())
    con_tarjeta = not args.selfie
    resultado = {
        "buenas": _medir_carpeta(args.buenas, umbrales, con_tarjeta),
        "malas": _medir_carpeta(args.malas, umbrales, con_tarjeta),
    }

    for nombre, items in resultado.items():
        print(f"\n== {nombre} ==")
        for foto, medidas in items:
            motivo = decidir(medidas, umbrales, con_tarjeta=con_tarjeta)
            print(
                f"  {foto.name:32s} nitidez={medidas.nitidez:8.1f} luz={medidas.luz_media:6.1f} "
                f"reflejo={medidas.reflejo:.3f} -> {motivo or 'ACEPTADA'}"
            )

    print("\n== Distribución por medida (umbrales actuales entre paréntesis en la ayuda) ==")
    for atributo, (_, ayuda) in _METRICAS.items():
        print(f"\n{atributo}  [{ayuda}]")
        for nombre, items in resultado.items():
            print(_resumen(nombre, [m for _, m in items], atributo))

    buenas_rechazadas = [
        f.name
        for f, m in resultado["buenas"]
        if decidir(m, umbrales, con_tarjeta=con_tarjeta) is not None
    ]
    malas_aceptadas = [
        f.name
        for f, m in resultado["malas"]
        if decidir(m, umbrales, con_tarjeta=con_tarjeta) is None
    ]
    print(f"\nBuenas que se rechazarían (falsos rechazos): {len(buenas_rechazadas)}")
    for nombre in buenas_rechazadas:
        print(f"  - {nombre}")
    print(f"Malas que se aceptarían: {len(malas_aceptadas)}")
    for nombre in malas_aceptadas:
        print(f"  - {nombre}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
