"""Modelos Pydantic de la API. Reflejan el contrato ``openapi/openapi.yaml``."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from app.domain import (
    PENDIENTES_DE_AGENTE,
    BeneficiarioPago,
    Canal,
    ClaseArchivo,
    CodigoCobertura,
    CodigoPendiente,
    ConsentimientoScope,
    DanoHogar,
    EstadoCliente,
    EstadoConsentimiento,
    EstadoDocumentoPoliza,
    EstadoEvidencia,
    EstadoPoliza,
    EstadoSiniestro,
    MotivoRechazo,
    OrigenDatos,
    OrigenSaldo,
    PlataformaDispositivo,
    RolInterno,
    TipoDecision,
    TipoDecisionSiniestro,
    TipoDocumento,
    TipoEvidencia,
    TipoPendiente,
    TipoSiniestro,
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


# --- Dispositivos para avisos push (BITS-229) ---


class RegistrarDispositivoRequest(_Model):
    token: str = Field(min_length=8, max_length=4096, pattern=r"^\S+$")
    plataforma: PlataformaDispositivo
    version_app: str = Field(min_length=1, max_length=32)


class DispositivoOut(_Model):
    dispositivo_id: str


class CrearUsuarioInternoRequest(_Model):
    correo: str = Field(max_length=254, pattern=_EMAIL)
    contrasena: str = Field(min_length=8, max_length=128)
    rol: RolInterno
    nombre: str | None = Field(default=None, max_length=100)


class UsuarioInternoOut(_Model):
    uid: str
    correo: str
    rol: RolInterno


# --- Siniestros (BITS-381) ---


class ArchivoReporteIn(_Model):
    """Datos de un archivo del reporte, en el mismo orden que las partes ``archivos``. La
    ubicación viaja aquí y no en la foto, porque la app borra los metadatos al recomprimirla."""

    clase: ClaseArchivo
    capturado_en: AwareDatetime | None = None
    latitud: float | None = Field(default=None, ge=-90, le=90)
    longitud: float | None = Field(default=None, ge=-180, le=180)


class OficinaIn(_Model):
    id: str = Field(min_length=1, max_length=64)
    banco: str = Field(min_length=1, max_length=100)
    nombre: str = Field(min_length=1, max_length=100)
    direccion: str = Field(min_length=1, max_length=200)


class DestinoTransferenciaIn(_Model):
    tipo: Literal["TRANSFERENCIA"]
    banco: str = Field(min_length=1, max_length=100)
    tipo_cuenta: Literal["AHORROS", "CORRIENTE"]
    numero_cuenta: str = Field(pattern=r"^[0-9]{4,20}$")


class DestinoChequeIn(_Model):
    tipo: Literal["CHEQUE"]
    oficina: OficinaIn


DestinoPagoIn = Annotated[DestinoTransferenciaIn | DestinoChequeIn, Field(discriminator="tipo")]


class ReportarSiniestroDatos(_Model):
    """La parte ``datos`` (JSON) del reporte multipart."""

    poliza_id: str = Field(min_length=1, max_length=64)
    # El paramétrico lo crea el pago automático por sismo: el cliente no lo reporta.
    tipo: Literal[TipoSiniestro.INCAPACIDAD_TOTAL, TipoSiniestro.INCENDIO_TERREMOTO]
    fecha_ocurrencia: date
    narracion: str | None = None
    archivos: list[ArchivoReporteIn]
    destino_pago: DestinoPagoIn | None = None


class SiniestroReportadoOut(_Model):
    id: str
    numero: str
    estado: EstadoSiniestro


class EntregarEvidenciasDatos(_Model):
    """La parte ``datos`` (JSON) del envío de lo que falta, con un dato por archivo."""

    archivos: list[ArchivoReporteIn] = Field(min_length=1, max_length=10)


class ArchivoSiniestroOut(_Model):
    """Sin la referencia del bucket: el archivo se pide por su ``id``."""

    id: str
    clase: ClaseArchivo
    capturado_en: AwareDatetime | None = None
    latitud: float | None = None
    longitud: float | None = None
    entregado_en: AwareDatetime


class EvidenciaPedidaOut(_Model):
    detalle: str
    pedida_en: AwareDatetime


class LineaPagoOut(_Model):
    beneficiario: BeneficiarioPago
    valor: float
    destino: str


class PagoOut(_Model):
    pagado_en: AwareDatetime
    lineas: list[LineaPagoOut]


class EventoSismoOut(_Model):
    fecha: date
    municipio: str
    magnitud: float


class SiniestroResumenOut(_Model):
    id: str
    numero: str
    poliza_id: str
    tipo: TipoSiniestro
    automatico: bool
    estado: EstadoSiniestro
    reportado_en: AwareDatetime
    actualizado_en: AwareDatetime


class SiniestroOut(SiniestroResumenOut):
    """El detalle que ve el cliente. No lleva el saldo del crédito ni el destino del pago: ahí
    está el número de cuenta y no hace falta devolverlo."""

    fecha_ocurrencia: date
    narracion: str | None = None
    ultima_entrega_en: AwareDatetime
    fecha_limite: AwareDatetime | None = None
    evidencia_pedida: EvidenciaPedidaOut | None = None
    motivo_rechazo: str | None = None
    pago: PagoOut | None = None
    archivos: list[ArchivoSiniestroOut]
    evento: EventoSismoOut | None = None


# --- Bandeja del ADMIN (BITS-377) ---


class DecidirSiniestroRequest(_Model):
    """La decisión del ``ADMIN``. ``agente`` lo llena el BFF con el usuario de la sesión."""

    decision: TipoDecisionSiniestro
    agente: str = Field(min_length=3, max_length=254)
    dano: DanoHogar | None = None
    valor: float | None = Field(default=None, gt=0, le=1_000_000_000_000)
    detalle: str | None = Field(default=None, max_length=2000)
    motivo: str | None = Field(default=None, max_length=2000)


class PolizaDelSiniestroOut(_Model):
    id: str
    numero: str | None = None
    cobertura: CodigoCobertura
    suma_asegurada: float


class SaldoCreditoOut(_Model):
    valor: float
    entidad: str
    consultado_en: AwareDatetime
    origen: OrigenSaldo


class DecisionSiniestroOut(_Model):
    agente: str
    decision: TipoDecisionSiniestro
    fecha: AwareDatetime
    dano: DanoHogar | None = None
    valor: float | None = None
    detalle: str | None = None
    motivo: str | None = None


class SiniestroInternoResumenOut(SiniestroResumenOut):
    """Una fila de la bandeja: lo que el ``ADMIN`` necesita para ordenar y abrir."""

    cliente_id: str
    fecha_limite: AwareDatetime | None = None


class SiniestroInternoOut(SiniestroOut):
    """El detalle del ``ADMIN``: el del cliente más el saldo guardado, cómo recibe el pago, la
    póliza afectada y las decisiones. Interno: no sale por el portal del cliente."""

    cliente_id: str
    poliza: PolizaDelSiniestroOut
    saldo_credito: SaldoCreditoOut
    destino_pago: dict | None = None
    decisiones: list[DecisionSiniestroOut]
