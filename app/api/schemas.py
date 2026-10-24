"""Modelos Pydantic de la API. Reflejan el contrato ``openapi/openapi.yaml``."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from app.domain import (
    PENDIENTES_DE_AGENTE,
    Canal,
    CodigoCobertura,
    CodigoPendiente,
    ConsentimientoScope,
    EstadoCliente,
    EstadoConsentimiento,
    EstadoDocumentoPoliza,
    EstadoEvidencia,
    EstadoPoliza,
    MotivoRechazo,
    OrigenDatos,
    TipoDecision,
    TipoDocumento,
    TipoEvidencia,
    TipoPendiente,
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
    telefono: str = Field(pattern=_TELEFONO)
    canal: Canal
    autoriza_tratamiento_datos: bool
    politica_version_tratamiento_datos: str | None = Field(default=None, max_length=20)
    autoriza_datos_financieros: bool
    politica_version_datos_financieros: str | None = Field(default=None, max_length=20)


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
    telefono: str
    estado: EstadoCliente
    correo_confirmado: bool
    confirmado_en: datetime | None = None
    creado_en: datetime
    actualizado_en: datetime | None = None


class DisponibilidadOut(_Model):
    correo_disponible: bool | None = None
    documento_disponible: bool | None = None


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


class EvidenciaSubidaOut(_Model):
    evidencia_id: str | None = None
    tipo: TipoEvidencia
    estado: EstadoEvidencia
    motivo: MotivoRechazo | None = None
    intentos_restantes: int


class EstadoTipoEvidenciaOut(_Model):
    tipo: TipoEvidencia
    estado: Literal["ACEPTADA", "REVISION_MANUAL", "PENDIENTE"]
    evidencia_id: str | None = None
    creada_en: datetime | None = None
    intentos_restantes: int


# --- Pólizas (BITS-101) ---


class DocumentoAceptadoIn(_Model):
    tipo: Literal["poliza"]
    version: str = Field(pattern=r"^V[0-9]+$", max_length=20)


class SolicitarPolizaRequest(_Model):
    coberturas: list[CodigoCobertura] = Field(min_length=1, max_length=len(CodigoCobertura))
    cotizacion_creada_en: AwareDatetime
    documentos_aceptados: list[DocumentoAceptadoIn] = Field(min_length=1, max_length=5)


class DecisionRequest(_Model):
    decision: TipoDecision
    agente: str = Field(min_length=1, max_length=128)
    pendientes: list[CodigoPendiente] = Field(min_length=1, max_length=2)
    nota: str | None = Field(default=None, max_length=500)

    @field_validator("pendientes")
    @classmethod
    def _solo_de_agente(cls, valor: list[CodigoPendiente]) -> list[CodigoPendiente]:
        if len(set(valor)) != len(valor):
            raise ValueError("los pendientes no pueden repetirse")
        no_agente = [str(c) for c in valor if c not in PENDIENTES_DE_AGENTE]
        if no_agente:
            raise ValueError(f"solo se deciden pendientes de agente, no: {', '.join(no_agente)}")
        return valor


class CoberturaContratadaOut(_Model):
    codigo: CodigoCobertura
    obligatoria: bool
    suma_asegurada: float
    prima_neta: float
    iva: float
    prima_total: float


class DatosCreditoOut(_Model):
    valor_credito: float
    plazo_restante_meses: int
    entidad_acreedora: str
    saldo_insoluto: float


class VigenciaOut(_Model):
    desde: datetime
    hasta: datetime


class DetallePendienteOut(_Model):
    evidencias: list[TipoEvidencia]


class PendienteOut(_Model):
    codigo: CodigoPendiente
    tipo: TipoPendiente
    mensaje: str
    detalle: DetallePendienteOut | None = None


class EstadoDocumentoOut(_Model):
    # DISPONIBLE: ya se puede pedir el documento (si aún se está generando, GET /documento
    # responde 202). NO_DISPONIBLE: la póliza todavía no está emitida.
    estado: Literal["NO_DISPONIBLE", "DISPONIBLE"]


class DocumentoAceptadoOut(_Model):
    tipo: str
    version: str
    aceptado_en: datetime


class MotivoRechazoOut(_Model):
    codigo: CodigoPendiente
    mensaje: str


class RechazoOut(_Model):
    motivos: list[MotivoRechazoOut]
    rechazada_en: datetime


class PolizaOut(_Model):
    poliza_id: str
    numero: str | None
    estado: EstadoPoliza
    producto: Literal["VIDA_HIPOTECARIO"] = "VIDA_HIPOTECARIO"
    creada_en: datetime
    emitida_en: datetime | None
    solicitud_vigente_hasta: datetime | None
    vigencia: VigenciaOut | None
    coberturas: list[CoberturaContratadaOut]
    prima_total_mensual: float
    moneda: Literal["COP"] = "COP"
    datos_credito: DatosCreditoOut
    origen_datos: OrigenDatos
    cotizacion_creada_en: datetime
    pendientes: list[PendienteOut]
    documento: EstadoDocumentoOut
    documentos_aceptados: list[DocumentoAceptadoOut]
    rechazo: RechazoOut | None = None


class EvidenciaDePolizaOut(_Model):
    tipo: TipoEvidencia
    estado: Literal["ACEPTADA", "REVISION_MANUAL", "PENDIENTE"]
    evidencia_id: str | None


class DecisionOut(_Model):
    decision: TipoDecision
    agente: str
    pendientes: list[CodigoPendiente]
    nota: str | None
    decidida_en: datetime


class PolizaInternaOut(PolizaOut):
    cliente_id: str
    ultima_evaluacion_en: datetime | None
    tasa: float
    version_tarifa: int
    evidencias: list[EvidenciaDePolizaOut]
    decisiones: list[DecisionOut]


class PendienteResumenOut(_Model):
    codigo: CodigoPendiente
    tipo: TipoPendiente


class PolizaResumenOut(_Model):
    poliza_id: str
    numero: str | None
    cliente_id: str
    estado: EstadoPoliza
    creada_en: datetime
    solicitud_vigente_hasta: datetime | None
    prima_total_mensual: float
    origen_datos: OrigenDatos
    pendientes: list[PendienteResumenOut]


class PaginaPolizasOut(_Model):
    items: list[PolizaResumenOut]
    siguiente_cursor: str | None


class DocumentoPolizaOut(_Model):
    estado: EstadoDocumentoPoliza
    url: str | None = None
    expira_en: datetime | None = None
