"""Descarga los modelos de biometría que no van en el repositorio y verifica los demás.

Los tres modelos pequeños (YuNet y las dos redes de MiniFASNet, ~3,7 MB) están en ``models/``.
SFace pesa ~37 MB y se baja con este script, tanto en local como al construir la imagen. Todos
se comparan con su suma de control SHA-256: si un archivo cambia, el script falla.

    python tools/descargar_modelos.py [directorio]    # por defecto, ./models

Orígenes y licencias: ver ``models/README.md``.
"""

from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path

_ZOO = "https://github.com/opencv/opencv_zoo/raw/main/models"
_PESOS = "https://github.com/yakhyo/face-anti-spoofing/releases/download/weights"

# nombre -> (URL, SHA-256, ¿va en el repositorio?)
MODELOS = {
    "face_detection_yunet_2023mar.onnx": (
        f"{_ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
        True,
    ),
    "face_recognition_sface_2021dec.onnx": (
        f"{_ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
        "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
        False,
    ),
    "MiniFASNetV2.onnx": (
        f"{_PESOS}/MiniFASNetV2.onnx",
        "b32929adc2d9c34b9486f8c4c7bc97c1b69bc0ea9befefc380e4faae4e463907",
        True,
    ),
    "MiniFASNetV1SE.onnx": (
        f"{_PESOS}/MiniFASNetV1SE.onnx",
        "ebab7f90c7833fbccd46d3a555410e78d969db5438e169b6524be444862b3676",
        True,
    ),
}


def sha256(ruta: Path) -> str:
    resumen = hashlib.sha256()
    with ruta.open("rb") as archivo:
        for bloque in iter(lambda: archivo.read(1 << 20), b""):
            resumen.update(bloque)
    return resumen.hexdigest()


def asegurar(destino: Path) -> list[str]:
    """Deja todos los modelos en ``destino`` con la suma correcta. Devuelve los problemas."""
    destino.mkdir(parents=True, exist_ok=True)
    problemas = []
    for nombre, (url, esperada, en_repositorio) in MODELOS.items():
        ruta = destino / nombre
        if not ruta.is_file():
            if en_repositorio:
                problemas.append(f"{nombre}: falta, debería estar en el repositorio")
                continue
            print(f"Descargando {nombre} ...")
            temporal = ruta.with_suffix(".descarga")
            urllib.request.urlretrieve(url, temporal)  # noqa: S310 - URL fija de este archivo
            temporal.replace(ruta)
        if sha256(ruta) != esperada:
            problemas.append(f"{nombre}: la suma SHA-256 no coincide con la esperada")
    return problemas


def main() -> int:
    destino = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("models")
    problemas = asegurar(destino)
    for problema in problemas:
        print(f"ERROR {problema}", file=sys.stderr)
    if not problemas:
        print(f"Modelos listos en {destino}")
    return 1 if problemas else 0


if __name__ == "__main__":
    raise SystemExit(main())
