"""Adaptador hacia la API de administración de Identity Platform.

Pide el enlace de verificación de correo con credenciales de servicio (las del pod,
por Workload Identity) y devuelve solo el ``oobCode``: el enlace que llega al cliente
lo arma Core apuntando a la web, no a la página de Google.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qs, urlsplit

import httpx

from app.domain import (
    CuentaInterna,
    CuentaInternaYaExiste,
    IdentidadNoDisponible,
    NotificacionError,
    RolInterno,
)

logger = logging.getLogger("svc_core.adapters.identity_admin")

_BASE = "https://identitytoolkit.googleapis.com/v1/projects/{proyecto}"
_URL = _BASE + "/accounts:sendOobCode"
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
        self._base = _BASE.format(proyecto=project_id)
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

    # --- Cuentas internas del personal (BITS-374) ---

    async def crear_cuenta(
        self, correo: str, contrasena: str, rol: RolInterno, nombre: str | None
    ) -> CuentaInterna:
        cuerpo: dict = {"email": correo, "password": contrasena, "emailVerified": True}
        if nombre:
            cuerpo["displayName"] = nombre
        respuesta = await self._llamar("accounts", cuerpo)
        if respuesta.status_code != 200:
            if _codigo_de_error(respuesta) == "EMAIL_EXISTS":
                raise CuentaInternaYaExiste()
            raise _fallo(respuesta)
        uid = respuesta.json()["localId"]
        # Crear ignora `customAttributes` sin avisar (probado contra Identity Platform real): el
        # rol solo se guarda al actualizar. Sin él la cuenta no podría ingresar (403).
        try:
            fijado = await self._llamar(
                "accounts:update",
                {"localId": uid, "customAttributes": json.dumps({"rol": str(rol)})},
            )
            if fijado.status_code != 200:
                raise _fallo(fijado)
        except IdentidadNoDisponible:
            # No se deja una cuenta sin rol: reintentar daría 409 y nadie podría usarla.
            await self._borrar_sin_fallar(uid)
            raise
        return CuentaInterna(uid=uid, correo=correo, rol=rol, nombre=nombre)

    async def _borrar_sin_fallar(self, uid: str) -> None:
        try:
            respuesta = await self._llamar("accounts:delete", {"localId": uid})
        except IdentidadNoDisponible:
            respuesta = None
        if respuesta is None or respuesta.status_code != 200:
            logger.error("no se pudo borrar la cuenta sin rol uid=%s", uid)

    async def buscar_por_correo(self, correo: str) -> CuentaInterna | None:
        respuesta = await self._llamar("accounts:lookup", {"email": [correo]})
        if respuesta.status_code != 200:
            raise _fallo(respuesta)
        usuarios = respuesta.json().get("users") or []
        if not usuarios:
            return None
        usuario = usuarios[0]
        return CuentaInterna(
            uid=usuario["localId"],
            correo=usuario.get("email", correo),
            rol=_rol_de(usuario.get("customAttributes")),
            nombre=usuario.get("displayName"),
        )

    async def _llamar(self, ruta: str, cuerpo: dict) -> httpx.Response:
        try:
            return await self._client.post(
                f"{self._base}/{ruta}",
                headers={"Authorization": f"Bearer {await self._token()}"},
                json=cuerpo,
            )
        except httpx.TransportError as exc:
            logger.warning("Identity Platform no está disponible (%s)", type(exc).__name__)
            raise IdentidadNoDisponible() from exc


_CODIGO = re.compile(r"^[A-Z_]+")


def _codigo_de_error(respuesta: httpx.Response) -> str:
    """Solo el código del error (``EMAIL_EXISTS``): el texto completo puede nombrar el correo."""
    try:
        mensaje = respuesta.json().get("error", {}).get("message", "")
    except ValueError:
        return ""
    coincidencia = _CODIGO.match(mensaje if isinstance(mensaje, str) else "")
    return coincidencia.group(0) if coincidencia else ""


def _fallo(respuesta: httpx.Response) -> IdentidadNoDisponible:
    logger.warning(
        "Identity Platform rechazó la operación status=%s codigo=%s",
        respuesta.status_code,
        _codigo_de_error(respuesta),
    )
    return IdentidadNoDisponible()


def _rol_de(custom_attributes: str | None) -> RolInterno | None:
    if not custom_attributes:
        return None
    try:
        rol = json.loads(custom_attributes).get("rol")
    except (ValueError, AttributeError):
        return None
    return RolInterno(rol) if rol in {r.value for r in RolInterno} else None
