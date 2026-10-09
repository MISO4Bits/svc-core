"""Adaptador hacia la API de administración de Identity Platform.

Pide el enlace de verificación de correo con credenciales de servicio (las del pod,
por Workload Identity) y devuelve solo el ``oobCode``: el enlace que llega al cliente
lo arma Core apuntando a la web, no a la página de Google.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qs, urlsplit

import httpx

from app.domain import NotificacionError

logger = logging.getLogger("svc_core.adapters.identity_admin")

_URL = "https://identitytoolkit.googleapis.com/v1/projects/{proyecto}/accounts:sendOobCode"
_ALCANCE = "https://www.googleapis.com/auth/cloud-platform"

TokenProvider = Callable[[], Awaitable[str]]


def token_de_servicio() -> TokenProvider:  # pragma: no cover - requiere credenciales de GCP
    """Token de acceso de las credenciales por defecto del entorno (se renueva solo)."""
    import google.auth
    from google.auth.transport.requests import Request

    credenciales, _ = google.auth.default(scopes=[_ALCANCE])

    async def _token() -> str:
        if not credenciales.valid:
            await asyncio.to_thread(credenciales.refresh, Request())
        return credenciales.token

    return _token


class IdentityPlatformAdmin:
    def __init__(
        self,
        project_id: str,
        token_provider: TokenProvider,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 3.0,
    ) -> None:
        self._url = _URL.format(proyecto=project_id)
        self._token = token_provider
        self._client = client or httpx.AsyncClient(timeout=timeout_s)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def generar_codigo_verificacion(self, email: str) -> str:
        try:
            respuesta = await self._client.post(
                self._url,
                headers={"Authorization": f"Bearer {await self._token()}"},
                json={"requestType": "VERIFY_EMAIL", "email": email, "returnOobLink": True},
            )
        except httpx.TransportError as exc:
            raise NotificacionError("Identity Platform no está disponible") from exc
        if respuesta.status_code != 200:
            # El cuerpo del error puede nombrar el correo: no se registra.
            raise NotificacionError(f"Identity Platform respondió {respuesta.status_code}")
        enlace = respuesta.json().get("oobLink", "")
        codigos = parse_qs(urlsplit(enlace).query).get("oobCode")
        if not codigos:
            raise NotificacionError("Identity Platform no devolvió un código de verificación")
        return codigos[0]
