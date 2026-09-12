"""Modelos Pydantic de la API. Reflejan el contrato ``openapi/openapi.yaml``."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.domain import (
    Canal,
    ConsentimientoScope,
    EstadoCliente,
    EstadoConsentimiento,
    TipoDocumento,
)

_EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
_TELEFONO = r"^\+?[0-9]{7,15}$"
_DOCUMENTO = r"^[0-9A-Za-z-]+$"


class _Model(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
        extra="forbid",
    )


class RegistrarClienteRequest(_Model):
    identity_ref: str = Field(max_length=128)
    tipo_documento: TipoDocumento
    numero_documento: str = Field(min_length=4, max_length=20, pattern=_DOCUMENTO)
    primer_nombre: str = Field(min_length=1, max_length=60)
    segundo_nombre: str | None = Field(default=None, max_length=60)
    primer_apellido: str = Field(min_length=1, max_length=60)
    segundo_apellido: str | None = Field(default=None, max_length=60)
    fecha_nacimiento: date
    email: str = Field(max_length=254, pattern=_EMAIL)
    telefono: str | None = Field(default=None, pattern=_TELEFONO)


class ClienteOut(_Model):
    id: str
    identity_ref: str
    tipo_documento: TipoDocumento
    numero_documento: str
    primer_nombre: str
    segundo_nombre: str | None = None
    primer_apellido: str
    segundo_apellido: str | None = None
    fecha_nacimiento: date
    email: str
    telefono: str | None = None
    estado: EstadoCliente
    creado_en: datetime
    actualizado_en: datetime | None = None


class OtorgarConsentimientoRequest(_Model):
    scope: ConsentimientoScope
    politica_version: str = Field(max_length=20)
    canal: Canal


class ConsentimientoOut(_Model):
    scope: ConsentimientoScope
    estado: EstadoConsentimiento
    version: int
    politica_version: str | None = None
    canal: Canal | None = None
    otorgado_en: datetime | None = None
    revocado_en: datetime | None = None
    actualizado_en: datetime


class EstadoConsentimientoOut(_Model):
    scope: ConsentimientoScope
    estado: EstadoConsentimiento
    version: int
    vigente: bool
