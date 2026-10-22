"""Almacén de imágenes de evidencia sobre Cloud Storage (bucket privado, sin URL públicas)."""

from __future__ import annotations

import asyncio
from typing import Any


class GcsAlmacenEvidencias:
    """``client`` se inyecta en las pruebas; en el pod se crea con las credenciales de la
    cuenta de servicio (Workload Identity), sin llaves en el código ni en el entorno."""

    def __init__(self, bucket: str, client: Any | None = None) -> None:
        if client is None:  # pragma: no cover - requiere credenciales reales
            from google.cloud import storage

            client = storage.Client()
        self._bucket = client.bucket(bucket)

    async def guardar(self, clave: str, datos: bytes, content_type: str) -> str:
        blob = self._bucket.blob(clave)
        await asyncio.to_thread(blob.upload_from_string, datos, content_type=content_type)
        return clave

    async def obtener(self, referencia: str) -> bytes:
        blob = self._bucket.blob(referencia)
        return await asyncio.to_thread(blob.download_as_bytes)
