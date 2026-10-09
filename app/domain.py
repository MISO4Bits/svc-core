"""Modelo de dominio del submódulo Identidad y Consentimiento.

Sin dependencias de framework: entidades, enums, eventos y errores de negocio.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, Literal


def now_utc() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


class TipoDocumento(StrEnum):
    CC = "CC"
    CE = "CE"
    PA = "PA"
    NIT = "NIT"


class EstadoCliente(StrEnum):
    ACTIVO = "ACTIVO"
    BLOQUEADO = "BLOQUEADO"
    INACTIVO = "INACTIVO"


class ConsentimientoScope(StrEnum):
    OPEN_FINANCE = "OPEN_FINANCE"
    OPEN_DATA = "OPEN_DATA"


class EstadoConsentimiento(StrEnum):
    OTORGADO = "OTORGADO"
    REVOCADO = "REVOCADO"
    NO_OTORGADO = "NO_OTORGADO"


class Canal(StrEnum):
    WEB = "WEB"
    MOVIL = "MOVIL"
    BACKOFFICE = "BACKOFFICE"


@dataclass
class Cliente:
    identity_ref: str
    tipo_documento: TipoDocumento
    numero_documento: str
    primer_nombre: str
    primer_apellido: str
    fecha_nacimiento: date
    email: str
    telefono: str
    segundo_nombre: str | None = None
    segundo_apellido: str | None = None
    estado: EstadoCliente = EstadoCliente.ACTIVO
    correo_confirmado: bool = False
    confirmado_en: datetime | None = None
    id: str = field(default_factory=new_id)
    creado_en: datetime = field(default_factory=now_utc)
    actualizado_en: datetime | None = None


@dataclass
class Consentimiento:
    cliente_id: str
    scope: ConsentimientoScope
    estado: EstadoConsentimiento
    version: int = 1
    politica_version: str | None = None
    canal: Canal | None = None
    otorgado_en: datetime | None = None
    revocado_en: datetime | None = None
    actualizado_en: datetime = field(default_factory=now_utc)

    @property
    def vigente(self) -> bool:
        """El estado efectivo habilita el acceso a datos del scope."""
        return self.estado is EstadoConsentimiento.OTORGADO


@dataclass(frozen=True)
class DomainEvent:
    tipo: str
    datos: dict[str, Any]
    id: str = field(default_factory=new_id)
    ocurrido_en: datetime = field(default_factory=now_utc)


class TipoCorreo(StrEnum):
    BIENVENIDA = "bienvenida"
    VERIFICACION_CORREO = "verificacion-correo"


@dataclass(frozen=True)
class PlantillaCorreo:
    """Plantilla tal cual la publica Productos y Configuración de Mercado (sin rellenar)."""

    tipo: str
    version: str
    asunto: str
    cuerpo_html: str
    cuerpo_texto: str


class NotificacionError(Exception):
    """No se pudo preparar o publicar un correo. Nunca llega al cliente del registro."""


class DomainError(Exception):
    """Base de los errores de negocio."""


class ClienteYaExiste(DomainError):
    def __init__(
        self,
        tipo_documento: str,
        numero_documento: str,
        *,
        campo: Literal["documento", "correo"] = "documento",
    ) -> None:
        super().__init__(f"Ya existe un cliente para {tipo_documento} {numero_documento}")
        self.tipo_documento = str(tipo_documento)
        self.numero_documento = numero_documento
        self.campo = campo


class CorreoDesechable(DomainError):
    def __init__(self, email: str) -> None:
        super().__init__("El dominio del correo no admite registro (correo desechable)")
        self.email = email


class ClienteNoEncontrado(DomainError):
    def __init__(self, cliente_id: str) -> None:
        super().__init__(f"Cliente {cliente_id} no encontrado")
        self.cliente_id = cliente_id


class ConsentimientoNoEncontrado(DomainError):
    def __init__(self, cliente_id: str, scope: str) -> None:
        super().__init__(f"Consentimiento {scope} del cliente {cliente_id} no encontrado")
        self.cliente_id = cliente_id
        self.scope = str(scope)
