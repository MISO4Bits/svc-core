"""Almacenes de imágenes de evidencia: en memoria (pruebas) y en disco (desarrollo local)."""

from __future__ import annotations

import asyncio
from pathlib import Path


class InMemoryAlmacenEvidencias:
    def __init__(self) -> None:
        self._objetos: dict[str, bytes] = {}

    async def guardar(self, clave: str, datos: bytes, content_type: str) -> str:
        self._objetos[clave] = datos
        return clave

    async def obtener(self, referencia: str) -> bytes:
        return self._objetos[referencia]


class FilesystemAlmacenEvidencias:
    def __init__(self, raiz: str) -> None:
        self._raiz = Path(raiz).resolve()

    def _ruta(self, clave: str) -> Path:
        ruta = (self._raiz / clave).resolve()
        if not ruta.is_relative_to(self._raiz):
            raise ValueError("La clave sale de la carpeta del almacén")
        return ruta

    async def guardar(self, clave: str, datos: bytes, content_type: str) -> str:
        ruta = self._ruta(clave)

        def escribir() -> None:
            ruta.parent.mkdir(parents=True, exist_ok=True)
            ruta.write_bytes(datos)

        await asyncio.to_thread(escribir)
        return clave

    async def obtener(self, referencia: str) -> bytes:
        return await asyncio.to_thread(self._ruta(referencia).read_bytes)
