"""Modelo de dominio del submódulo Identidad y Consentimiento.

Sin dependencias de framework: entidades, enums, eventos y errores de negocio.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
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


class TipoEvidencia(StrEnum):
    """Qué evidencia de identidad se sube (BITS-275). Los valores son los del contrato."""

    CEDULA_FRENTE = "cedula_frente"
    CEDULA_REVERSO = "cedula_reverso"
    SELFIE = "selfie"


class EstadoEvidencia(StrEnum):
    """Veredicto del verificador. Solo ``ACEPTADA`` y ``REVISION_MANUAL`` se persisten."""

    ACEPTADA = "ACEPTADA"
    REINTENTAR = "REINTENTAR"
    REVISION_MANUAL = "REVISION_MANUAL"


class MotivoRechazo(StrEnum):
    BORROSA = "BORROSA"
    OSCURA = "OSCURA"
    REFLEJO = "REFLEJO"
    RECORTADA = "RECORTADA"
    NO_ES_CEDULA = "NO_ES_CEDULA"
    # Selfie (BITS-235): rostro, prueba de vida y comparación con la cédula.
    SIN_ROSTRO = "SIN_ROSTRO"
    VARIOS_ROSTROS = "VARIOS_ROSTROS"
    POSIBLE_SUPLANTACION = "POSIBLE_SUPLANTACION"
    NO_COINCIDE_CON_CEDULA = "NO_COINCIDE_CON_CEDULA"


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


@dataclass
class Evidencia:
    """Foto de identidad aceptada de un cliente. Inmutable salvo ``reemplazada_en``/``poliza_ref``.

    *Vigente* = ni reemplazada por otra captura ni consumida por una póliza.
    """

    cliente_id: str
    tipo: TipoEvidencia
    estado: EstadoEvidencia
    almacen_ref: str
    id: str = field(default_factory=new_id)
    creada_en: datetime = field(default_factory=now_utc)
    reemplazada_en: datetime | None = None
    poliza_ref: str | None = None
    # Solo la selfie. Se guardan para la revisión manual y la calibración; no salen por la API.
    puntaje_vida: float | None = None
    puntaje_coincidencia: float | None = None


@dataclass(frozen=True)
class Veredicto:
    """Resultado del verificador sobre una imagen. Nunca lleva el número leído.

    Los puntajes solo los llena el verificador biométrico (selfie); ``None`` si no se
    llegó a calcularlos.
    """

    estado: EstadoEvidencia
    motivo: MotivoRechazo | None = None
    puntaje_vida: float | None = None
    puntaje_coincidencia: float | None = None


@dataclass(frozen=True)
class ResultadoSubida:
    """Lo que devuelve subir una evidencia. ``evidencia`` es ``None`` si hay que reintentar."""

    estado: EstadoEvidencia
    motivo: MotivoRechazo | None
    intentos_restantes: int
    evidencia: Evidencia | None = None


@dataclass(frozen=True)
class EstadoTipoEvidencia:
    """Estado vigente de un tipo de evidencia: ``evidencia`` es ``None`` si está pendiente."""

    tipo: TipoEvidencia
    intentos_restantes: int
    evidencia: Evidencia | None = None


@dataclass(frozen=True)
class DomainEvent:
    tipo: str
    datos: dict[str, Any]
    id: str = field(default_factory=new_id)
    ocurrido_en: datetime = field(default_factory=now_utc)


class TipoCorreo(StrEnum):
    BIENVENIDA = "bienvenida"
    VERIFICACION_CORREO = "verificacion-correo"
    POLIZA_EMITIDA = "poliza-emitida"


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


class CedulaIncompleta(DomainError):
    """La selfie se compara con la cédula, así que ambas caras deben estar ya en el sistema."""

    def __init__(self) -> None:
        super().__init__(
            "Para subir la selfie primero deben estar vigentes ambas caras de la cédula"
        )


class ImagenInvalida(DomainError):
    """El archivo no es una imagen JPEG o PNG decodificable."""


class ImagenMuyGrande(DomainError):
    def __init__(self, limite_bytes: int) -> None:
        super().__init__(f"La imagen supera el máximo de {limite_bytes} bytes")
        self.limite_bytes = limite_bytes


# --- Pólizas (BITS-101) ---


class EstadoPoliza(StrEnum):
    """``VENCIDA`` no se guarda: se deriva al leer una ``PENDIENTE`` cuya cotización venció."""

    PENDIENTE = "PENDIENTE"
    EMITIDA = "EMITIDA"
    RECHAZADA = "RECHAZADA"
    VENCIDA = "VENCIDA"


class CodigoCobertura(StrEnum):
    VIDA = "VIDA"
    HOGAR = "HOGAR"


class OrigenDatos(StrEnum):
    CONFIRMADO = "CONFIRMADO"
    DECLARADO = "DECLARADO"


class TipoPendiente(StrEnum):
    USUARIO = "USUARIO"
    AGENTE = "AGENTE"


class CodigoPendiente(StrEnum):
    CORREO_SIN_CONFIRMAR = "CORREO_SIN_CONFIRMAR"
    EVIDENCIA_FALTANTE = "EVIDENCIA_FALTANTE"
    EVIDENCIA_EN_REVISION = "EVIDENCIA_EN_REVISION"
    CREDITO_NO_VERIFICADO = "CREDITO_NO_VERIFICADO"


# Pendientes que solo una persona de Solventa puede levantar.
PENDIENTES_DE_AGENTE = frozenset(
    {CodigoPendiente.EVIDENCIA_EN_REVISION, CodigoPendiente.CREDITO_NO_VERIFICADO}
)

# Texto listo para mostrar al cliente, por pendiente.
MENSAJE_PENDIENTE = {
    CodigoPendiente.CORREO_SIN_CONFIRMAR: (
        "Confirma tu cuenta con el enlace que enviamos a tu correo."
    ),
    CodigoPendiente.EVIDENCIA_FALTANTE: "Sube la evidencia de identidad que falta para continuar.",
    CodigoPendiente.EVIDENCIA_EN_REVISION: "Un agente está revisando tus documentos.",
    CodigoPendiente.CREDITO_NO_VERIFICADO: (
        "Los datos de tu crédito deben ser verificados por un agente."
    ),
}

# Texto al cliente cuando un agente rechaza, según el escenario que lo motiva.
MENSAJE_RECHAZO = {
    CodigoPendiente.CREDITO_NO_VERIFICADO: (
        "No pudimos verificar los datos de tu crédito con la entidad acreedora."
    ),
    CodigoPendiente.EVIDENCIA_EN_REVISION: (
        "No pudimos validar tu identidad con los documentos que enviaste."
    ),
}


class TipoDecision(StrEnum):
    APROBAR = "APROBAR"
    RECHAZAR = "RECHAZAR"


class EstadoDocumentoPoliza(StrEnum):
    GENERANDO = "GENERANDO"
    LISTO = "LISTO"


@dataclass(frozen=True)
class Pendiente:
    codigo: CodigoPendiente
    tipo: TipoPendiente
    mensaje: str
    evidencias: tuple[TipoEvidencia, ...] = ()

    @classmethod
    def de(cls, codigo: CodigoPendiente, evidencias: tuple[TipoEvidencia, ...] = ()) -> Pendiente:
        tipo = TipoPendiente.AGENTE if codigo in PENDIENTES_DE_AGENTE else TipoPendiente.USUARIO
        return cls(codigo, tipo, MENSAJE_PENDIENTE[codigo], evidencias)


@dataclass(frozen=True)
class CoberturaContratada:
    codigo: CodigoCobertura
    obligatoria: bool
    suma_asegurada: Decimal
    prima_neta: Decimal
    iva: Decimal
    prima_total: Decimal


@dataclass(frozen=True)
class DatosCredito:
    valor_credito: Decimal
    plazo_restante_meses: int
    entidad_acreedora: str
    saldo_insoluto: Decimal


@dataclass(frozen=True)
class Vigencia:
    desde: datetime
    hasta: datetime


@dataclass(frozen=True)
class DocumentoAceptado:
    tipo: str
    version: str
    aceptado_en: datetime


@dataclass(frozen=True)
class EvidenciaDePoliza:
    """Foto de una evidencia que la póliza consumió al emitirse."""

    tipo: TipoEvidencia
    estado: EstadoEvidencia
    evidencia_id: str


@dataclass(frozen=True)
class Decision:
    decision: TipoDecision
    agente: str
    pendientes: tuple[CodigoPendiente, ...]
    decidida_en: datetime
    nota: str | None = None


@dataclass(frozen=True)
class MotivoRechazoPoliza:
    codigo: CodigoPendiente
    mensaje: str


@dataclass(frozen=True)
class CotizacionActiva:
    """Lo que Cotización devuelve de la cotización activa de un cliente."""

    estado: str  # VIGENTE | EXPIRADA
    edad: int
    datos_credito: DatosCredito
    origen_datos: OrigenDatos
    tasa: Decimal
    version_tarifa: int
    cobertura_meses: int
    coberturas: tuple[CoberturaContratada, ...]
    vigencia: Vigencia
    creada_en: datetime


@dataclass
class Poliza:
    """La solicitud de póliza y, cuando se emite, la póliza. Las cifras son la foto de la
    cotización: no se recalculan."""

    cliente_id: str
    cotizacion_creada_en: datetime
    solicitud_vigente_hasta: datetime
    coberturas: tuple[CoberturaContratada, ...]
    datos_credito: DatosCredito
    origen_datos: OrigenDatos
    tasa: Decimal
    version_tarifa: int
    cobertura_meses: int
    edad: int
    documentos_aceptados: tuple[DocumentoAceptado, ...]
    estado: EstadoPoliza = EstadoPoliza.PENDIENTE
    pendientes: tuple[Pendiente, ...] = ()
    id: str = field(default_factory=new_id)
    creada_en: datetime = field(default_factory=now_utc)
    ultima_evaluacion_en: datetime | None = None
    numero: str | None = None
    emitida_en: datetime | None = None
    vigencia: Vigencia | None = None
    evidencias: tuple[EvidenciaDePoliza, ...] = ()  # las que se consumieron al emitir
    decisiones: tuple[Decision, ...] = ()
    motivos_rechazo: tuple[MotivoRechazoPoliza, ...] = ()
    rechazada_en: datetime | None = None
    # Cuándo se encargó el correo con el PDF adjunto; evita mandarlo dos veces.
    documento_enviado_en: datetime | None = None

    @property
    def prima_total_mensual(self) -> Decimal:
        return sum((c.prima_total for c in self.coberturas), Decimal(0))

    def codigos_cobertura(self) -> frozenset[CodigoCobertura]:
        return frozenset(c.codigo for c in self.coberturas)

    def aprobados(self) -> frozenset[CodigoPendiente]:
        """Pendientes de agente que una persona ya aprobó."""
        return frozenset(
            codigo
            for d in self.decisiones
            if d.decision is TipoDecision.APROBAR
            for codigo in d.pendientes
        )

    def estado_en(self, ahora: datetime) -> EstadoPoliza:
        if self.estado is EstadoPoliza.PENDIENTE and ahora >= self.solicitud_vigente_hasta:
            return EstadoPoliza.VENCIDA
        return self.estado


@dataclass(frozen=True)
class FiltroPolizas:
    estado: EstadoPoliza | None = None
    tipo_pendiente: TipoPendiente | None = None
    codigo_pendiente: CodigoPendiente | None = None
    cliente_id: str | None = None
    creada_desde: datetime | None = None
    creada_hasta: datetime | None = None
    mas_recientes_primero: bool = False
    limite: int = 25
    cursor: str | None = None


@dataclass(frozen=True)
class PaginaPolizas:
    items: tuple[Poliza, ...]
    siguiente_cursor: str | None


@dataclass(frozen=True)
class ReferenciaDocumento:
    """Dónde quedó el PDF de la póliza (lo avisa fn-documentos con DocumentoPolizaGenerado)."""

    bucket: str
    objeto: str
    nombre_archivo: str
    content_type: str


@dataclass(frozen=True)
class DocumentoPoliza:
    estado: EstadoDocumentoPoliza
    url: str | None = None
    expira_en: datetime | None = None


class PolizaNoEncontrada(DomainError):
    def __init__(self, poliza_id: str) -> None:
        super().__init__(f"Póliza {poliza_id} no encontrada")
        self.poliza_id = poliza_id


class ClienteNoActivo(DomainError):
    def __init__(self, cliente_id: str) -> None:
        super().__init__(f"El cliente {cliente_id} no está activo")
        self.cliente_id = cliente_id


class SolicitudPolizaInvalida(DomainError):
    """Cuerpo bien formado pero que no se puede atender (cobertura que la cotización no tiene,
    sin la obligatoria, sin los documentos). Responde 400 con el campo."""

    def __init__(self, campo: str, mensaje: str) -> None:
        super().__init__(mensaje)
        self.campo = campo
        self.mensaje = mensaje


class FueraDeRango(DomainError):
    pass


class CotizacionNoDisponible(DomainError):
    def __init__(self) -> None:
        super().__init__("Cotización no está disponible, intenta de nuevo en un momento")


class DocumentoNoDisponible(DomainError):
    def __init__(self) -> None:
        super().__init__("El servicio de documentos no está disponible, intenta de nuevo")


class ConflictoPoliza(DomainError):
    """Base de los conflictos 409 con un ``codigo`` estable."""

    codigo = "CONFLICTO"


class SinCotizacion(ConflictoPoliza):
    codigo = "SIN_COTIZACION"

    def __init__(self) -> None:
        super().__init__("El cliente no tiene una cotización activa")


class CotizacionVencida(ConflictoPoliza):
    codigo = "COTIZACION_VENCIDA"

    def __init__(self) -> None:
        super().__init__("La cotización activa ya venció: vuelve a cotizar")


class CotizacionCambio(ConflictoPoliza):
    codigo = "COTIZACION_CAMBIO"

    def __init__(self) -> None:
        super().__init__("La cotización activa no es la que viste: revisa su precio de nuevo")


class SolicitudExistente(ConflictoPoliza):
    codigo = "SOLICITUD_EXISTENTE"

    def __init__(self) -> None:
        super().__init__(
            "Ya hay una solicitud de esa cotización con otras coberturas: vuelve a cotizar"
        )


class SolicitudRechazada(ConflictoPoliza):
    codigo = "SOLICITUD_RECHAZADA"

    def __init__(self) -> None:
        super().__init__("Esa solicitud fue rechazada: vuelve a cotizar para intentarlo de nuevo")


class PolizaNoEmitida(ConflictoPoliza):
    codigo = "POLIZA_NO_EMITIDA"

    def __init__(self) -> None:
        super().__init__("La póliza todavía no está emitida")


class PolizaNoPendiente(ConflictoPoliza):
    codigo = "POLIZA_NO_PENDIENTE"

    def __init__(self) -> None:
        super().__init__("La póliza no está pendiente")


class SolicitudVencida(ConflictoPoliza):
    codigo = "SOLICITUD_VENCIDA"

    def __init__(self) -> None:
        super().__init__("La solicitud venció: su cotización ya pasó la vigencia")


class PendienteNoAplica(DomainError):
    def __init__(self, codigo: str) -> None:
        super().__init__(f"La póliza no tiene el pendiente {codigo}")
        self.codigo = codigo


class PolizaYaExiste(DomainError):
    """Ya hay una solicitud de ese cliente para esa cotización (carrera entre dos solicitudes)."""


class NumeroPolizaDuplicado(DomainError):
    """El número generado ya lo tiene otra póliza; el servicio genera otro."""


# Reglas de edad (las mismas de Cotización, BITS-95).
EDAD_MINIMA = 18
EDAD_MAXIMA = 65
EDAD_MAXIMA_AL_TERMINAR = 75

# Sin I, L, O ni U: no se confunden al leerlo o dictarlo.
ALFABETO_NUMERO_POLIZA = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def edad_en(nacimiento: date, hoy: date) -> int:
    return hoy.year - nacimiento.year - ((hoy.month, hoy.day) < (nacimiento.month, nacimiento.day))


def validar_rango_de_edad(edad: int, plazo_restante_meses: int) -> None:
    """Edad entre 18 y 65 y crédito que termina a los 75 años o antes (en meses, sin redondear)."""
    if not EDAD_MINIMA <= edad <= EDAD_MAXIMA:
        raise FueraDeRango(f"La edad ({edad}) debe estar entre {EDAD_MINIMA} y {EDAD_MAXIMA} años")
    if edad * 12 + plazo_restante_meses > EDAD_MAXIMA_AL_TERMINAR * 12:
        raise FueraDeRango(
            f"El crédito debe terminar a los {EDAD_MAXIMA_AL_TERMINAR} años o antes "
            f"(edad {edad} y {plazo_restante_meses} meses restantes)"
        )


def sumar_meses(inicio: datetime, meses: int) -> datetime:
    """``inicio`` más ``meses`` calendario; el día se ajusta si el mes de llegada es más corto."""
    indice = inicio.month - 1 + meses
    anio, mes = inicio.year + indice // 12, indice % 12 + 1
    primero_del_siguiente = date(anio + (mes == 12), mes % 12 + 1, 1)
    ultimo_dia = (primero_del_siguiente - timedelta(days=1)).day
    return inicio.replace(year=anio, month=mes, day=min(inicio.day, ultimo_dia))


def generar_numero_poliza(emitida_en: datetime, aleatorio: Callable[[str], str]) -> str:
    """``SOL-VH-AAAA-XXXXXX``: producto Vida hipotecario, año de emisión y seis caracteres
    aleatorios (no consecutivo, para no crear un cuello de botella al escribir)."""
    sufijo = "".join(aleatorio(ALFABETO_NUMERO_POLIZA) for _ in range(6))
    return f"SOL-VH-{emitida_en.year}-{sufijo}"


# --- Dispositivos y avisos push (BITS-229) ---

MAX_DISPOSITIVOS_POR_CLIENTE = 5


class PlataformaDispositivo(StrEnum):
    ANDROID = "android"


@dataclass
class Dispositivo:
    """Teléfono de un cliente al que se le pueden mandar avisos push. El ``token`` identifica la
    instalación de la app en Firebase y es único: si otro cliente inicia sesión en el mismo
    teléfono, el dispositivo pasa a ser suyo."""

    cliente_id: str
    token: str
    plataforma: PlataformaDispositivo
    version_app: str
    id: str = field(default_factory=new_id)
    registrado_en: datetime = field(default_factory=now_utc)
    ultimo_visto_en: datetime = field(default_factory=now_utc)
