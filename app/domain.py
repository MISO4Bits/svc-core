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
    BIENVENIDA_VENTA = "bienvenida-venta"
    CUENTA_ACTIVADA = "cuenta-activada"


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


class ClientesDistintos(DomainError):
    """El correo y el documento de la búsqueda no son de la misma cuenta (venta directa)."""

    def __init__(self, campo: str) -> None:
        super().__init__("El correo y el documento son de cuentas distintas")
        self.campo = campo


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
    # Rechazó la renovación y llegó el aniversario (BITS-387). Todavía acepta siniestros
    # ocurridos dentro de su vigencia.
    DESACTIVADA = "DESACTIVADA"


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


@dataclass(frozen=True)
class DatosVenta:
    """Lo que el socio capturó al vender (BITS-378): dónde está el inmueble y la cuota del
    crédito. Solo existe en las pólizas de venta directa."""

    direccion: str
    municipio: str
    cuota_mensual: Decimal


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
    # Solo la venta directa del socio (BITS-378): quién la vendió y lo que capturó.
    socio_id: str | None = None
    datos_venta: DatosVenta | None = None

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


class DocumentosLegalesNoDisponibles(DomainError):
    def __init__(self) -> None:
        super().__init__("No se pudo consultar la versión vigente de las condiciones")


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


# --- Cuentas internas del personal de Solventa (BITS-374) ---

DOMINIO_INTERNO = "solventa4bits.com"


class RolInterno(StrEnum):
    """Rol de una cuenta del personal. Vive en los ``customAttributes`` de Identity Platform."""

    ADMIN = "ADMIN"
    SOCIO = "SOCIO"


@dataclass(frozen=True)
class CuentaInterna:
    """``uid`` es el id de Identity Platform; el de un ``SOCIO`` es su ``socioId``. ``rol`` es
    ``None`` solo al buscar una cuenta que no es del personal (p. ej. de un cliente)."""

    uid: str
    correo: str
    rol: RolInterno | None
    nombre: str | None = None


def es_correo_interno(correo: str) -> bool:
    """El correo es del dominio de Solventa, sin distinguir mayúsculas."""
    usuario, _, dominio = correo.strip().lower().rpartition("@")
    return bool(usuario) and dominio == DOMINIO_INTERNO


class CorreoInternoInvalido(DomainError):
    def __init__(self) -> None:
        super().__init__(f"El correo debe ser del dominio @{DOMINIO_INTERNO}")


class NombreSocioObligatorio(DomainError):
    def __init__(self) -> None:
        super().__init__("El nombre es obligatorio para una cuenta SOCIO")


class CuentaInternaYaExiste(DomainError):
    def __init__(self) -> None:
        super().__init__("Ya existe una cuenta con ese correo")


class CuentaClienteYaExiste(DomainError):
    """El proveedor de identidad ya tiene una cuenta con ese correo."""


class IdentidadNoDisponible(DomainError):
    """Identity Platform no respondió o rechazó la operación. Se puede reintentar."""

    def __init__(self) -> None:
        super().__init__("El proveedor de identidad no está disponible, intenta de nuevo")


# --- Siniestros (BITS-381) ---

# El cliente aporta como mucho unos pocos archivos por reporte: un tope evita subidas sin límite.
MAX_ARCHIVOS_SINIESTRO = 10


class TipoSiniestro(StrEnum):
    INCAPACIDAD_TOTAL = "INCAPACIDAD_TOTAL"  # cobertura VIDA
    INCENDIO_TERREMOTO = "INCENDIO_TERREMOTO"  # cobertura HOGAR (Tu casa)
    # Lo crea el pago automático por sismo (BITS-386), nunca el cliente: no se puede reportar.
    SISMO_PARAMETRICO = "SISMO_PARAMETRICO"  # cobertura HOGAR

    @property
    def cobertura(self) -> CodigoCobertura:
        return (
            CodigoCobertura.VIDA
            if self is TipoSiniestro.INCAPACIDAD_TOTAL
            else CodigoCobertura.HOGAR
        )


class EstadoSiniestro(StrEnum):
    EN_ESTUDIO = "EN_ESTUDIO"
    FALTA_INFORMACION = "FALTA_INFORMACION"
    PAGADO = "PAGADO"
    RECHAZADO = "RECHAZADO"


# Mientras un siniestro está en alguno de estos estados, su cobertura no admite otro reporte.
ESTADOS_EN_CURSO = (EstadoSiniestro.EN_ESTUDIO, EstadoSiniestro.FALTA_INFORMACION)


class ClaseArchivo(StrEnum):
    FOTO = "FOTO"
    DOCUMENTO = "DOCUMENTO"


class OrigenSaldo(StrEnum):
    OPEN_FINANCE = "OPEN_FINANCE"
    POLIZA = "POLIZA"


@dataclass(frozen=True)
class ArchivoSiniestro:
    """``latitud`` y ``longitud`` son ``None`` si el cliente negó el permiso de ubicación.
    ``referencia`` es la clave en el bucket de evidencias."""

    id: str
    clase: ClaseArchivo
    referencia: str
    content_type: str
    entregado_en: datetime
    capturado_en: datetime | None = None
    latitud: float | None = None
    longitud: float | None = None


@dataclass(frozen=True)
class SaldoCredito:
    """Saldo del crédito guardado una sola vez, al reportar. Es lo que se paga al banco."""

    valor: Decimal
    entidad: str
    consultado_en: datetime
    origen: OrigenSaldo


class TipoDecisionSiniestro(StrEnum):
    PAGAR = "PAGAR"
    PEDIR_EVIDENCIA = "PEDIR_EVIDENCIA"
    RECHAZAR = "RECHAZAR"


class DanoHogar(StrEnum):
    """Cómo queda la casa. Define a quién se paga: el cliente repara, o hay pérdida total y el
    banco cobra primero hasta el saldo del crédito."""

    REPARABLE = "REPARABLE"
    PERDIDA_TOTAL = "PERDIDA_TOTAL"


@dataclass(frozen=True)
class DecisionSiniestro:
    """Lo que cada decisión deja: quién la tomó (el usuario de la sesión), cuándo, cuál fue y el
    valor o el motivo. No hay nota interna."""

    agente: str
    decision: TipoDecisionSiniestro
    fecha: datetime
    dano: DanoHogar | None = None
    valor: Decimal | None = None
    detalle: str | None = None  # lo que se pidió, en PEDIR_EVIDENCIA
    motivo: str | None = None  # lo que ve el cliente, en RECHAZAR


class OrigenAsistencia(StrEnum):
    """De dónde sale el punto al que va el prestador: las fotos del siniestro, el teléfono o una
    dirección que escribe el cliente (con su localidad)."""

    FOTOS = "FOTOS"
    TELEFONO = "TELEFONO"
    DIRECCION = "DIRECCION"


@dataclass(frozen=True)
class PrestadorAsistencia:
    """Los datos del prestador elegido, tal como los mandó la app (BITS-385): el catálogo vive en
    svc-productos y aquí solo se guarda lo que el cliente vio al pedir la ayuda."""

    id: str
    nombre: str
    direccion: str
    tiempo_llegada_min: int


@dataclass(frozen=True)
class Asistencia:
    """Una solicitud de ayuda en sitio. Cuelga del siniestro y no cambia su estado ni su plazo.
    ``numero`` es lo que ve el cliente en la confirmación (``ASI-AAAA-XXXXXX``)."""

    numero: str
    tipo: str
    prestador: PrestadorAsistencia
    origen: OrigenAsistencia
    solicitada_en: datetime
    latitud: float | None = None
    longitud: float | None = None
    direccion: str | None = None
    localidad: str | None = None
    id: str = field(default_factory=new_id)


@dataclass(frozen=True)
class EvidenciaPedida:
    """Lo que el ``ADMIN`` escribió al pedir más evidencia, tal como lo ve el cliente."""

    detalle: str
    pedida_en: datetime


class BeneficiarioPago(StrEnum):
    BANCO = "BANCO"
    CLIENTE = "CLIENTE"


@dataclass(frozen=True)
class LineaPago:
    """``destino`` es texto listo para mostrar, p. ej. «Cuenta de ahorros terminada en 4821»."""

    beneficiario: BeneficiarioPago
    valor: Decimal
    destino: str


@dataclass(frozen=True)
class Pago:
    """La orden de pago simulada: una o dos líneas, y una sola por siniestro."""

    pagado_en: datetime
    lineas: tuple[LineaPago, ...]


class ConflictoSiniestro(DomainError):
    """Base de los conflictos 409 de siniestros con un ``codigo`` estable."""

    codigo = "CONFLICTO"


class TransicionInvalida(ConflictoSiniestro):
    """El estado del siniestro no admite esa acción (p. ej. enviar evidencia sin que se haya
    pedido, o decidir uno que está en ``FALTA_INFORMACION``)."""

    codigo = "ESTADO_NO_PERMITE"

    def __init__(self, estado: EstadoSiniestro) -> None:
        super().__init__(f"El siniestro está {estado}: no admite esa acción")
        self.estado = estado


@dataclass
class Siniestro:
    """``numero`` es el radicado que ve el cliente. ``destino_pago`` se guarda tal como llega.

    Las transiciones son métodos del dominio (``pedir_evidencia``, ``entregar_evidencia``,
    ``pagar`` y ``rechazar``): las llama la bandeja del ``ADMIN`` y el pago por sismo por igual.
    Cada una exige el estado del que parte y deja el siniestro como quedaría si se guardara.
    """

    cliente_id: str
    poliza_id: str
    tipo: TipoSiniestro
    fecha_ocurrencia: date
    saldo_credito: SaldoCredito
    numero: str
    narracion: str | None = None
    archivos: tuple[ArchivoSiniestro, ...] = ()
    destino_pago: dict | None = None
    estado: EstadoSiniestro = EstadoSiniestro.EN_ESTUDIO
    id: str = field(default_factory=new_id)
    reportado_en: datetime = field(default_factory=now_utc)
    ultima_entrega_en: datetime = field(default_factory=now_utc)
    actualizado_en: datetime = field(default_factory=now_utc)
    evidencia_pedida: EvidenciaPedida | None = None
    motivo_rechazo: str | None = None
    pago: Pago | None = None
    decisiones: tuple[DecisionSiniestro, ...] = ()
    # Solo el paramétrico (BITS-386): lo creó el sismo y no una persona; ``evento`` es
    # ``{fecha, municipio, magnitud}``.
    automatico: bool = False
    evento: dict | None = None
    # Solicitudes de ayuda en sitio (BITS-385): solo Tu casa y solo con el siniestro en curso.
    asistencias: tuple[Asistencia, ...] = ()

    @property
    def fecha_limite(self) -> datetime | None:
        """La última entrega más un mes (art. 1080). Solo corre en ``EN_ESTUDIO``: en
        ``FALTA_INFORMACION`` el reloj se detiene y en los demás ya no aplica."""
        if self.estado is not EstadoSiniestro.EN_ESTUDIO:
            return None
        return sumar_meses(self.ultima_entrega_en, 1)

    def _exigir(self, estado: EstadoSiniestro) -> None:
        if self.estado is not estado:
            raise TransicionInvalida(self.estado)

    def pedir_evidencia(self, detalle: str, ahora: datetime, agente: str) -> None:
        self._exigir(EstadoSiniestro.EN_ESTUDIO)
        detalle = detalle.strip()
        self.estado = EstadoSiniestro.FALTA_INFORMACION
        self.evidencia_pedida = EvidenciaPedida(detalle, ahora)
        self.decisiones = (
            *self.decisiones,
            DecisionSiniestro(
                agente, TipoDecisionSiniestro.PEDIR_EVIDENCIA, ahora, detalle=detalle
            ),
        )
        self.actualizado_en = ahora

    def entregar_evidencia(self, archivos: tuple[ArchivoSiniestro, ...], ahora: datetime) -> None:
        self._exigir(EstadoSiniestro.FALTA_INFORMACION)
        self.archivos = (*self.archivos, *archivos)
        self.ultima_entrega_en = ahora
        self.estado = EstadoSiniestro.EN_ESTUDIO
        self.actualizado_en = ahora

    def pagar(
        self,
        pago: Pago,
        ahora: datetime,
        agente: str,
        *,
        dano: DanoHogar | None = None,
        valor: Decimal | None = None,
    ) -> None:
        self._exigir(EstadoSiniestro.EN_ESTUDIO)
        self.estado = EstadoSiniestro.PAGADO
        self.pago = pago
        self.decisiones = (
            *self.decisiones,
            DecisionSiniestro(agente, TipoDecisionSiniestro.PAGAR, ahora, dano=dano, valor=valor),
        )
        self.actualizado_en = ahora

    def rechazar(self, motivo: str, ahora: datetime, agente: str) -> None:
        self._exigir(EstadoSiniestro.EN_ESTUDIO)
        motivo = motivo.strip()
        self.estado = EstadoSiniestro.RECHAZADO
        self.motivo_rechazo = motivo
        self.decisiones = (
            *self.decisiones,
            DecisionSiniestro(agente, TipoDecisionSiniestro.RECHAZAR, ahora, motivo=motivo),
        )
        self.actualizado_en = ahora


class ValorSuperaSumaAsegurada(DomainError):
    """El valor que el ``ADMIN`` quiere pagar excede la suma asegurada de la cobertura. Responde
    422: la solicitud está bien formada pero la regla de negocio no se cumple."""

    def __init__(self, suma_asegurada: Decimal) -> None:
        super().__init__(f"El valor no puede superar la suma asegurada ({suma_asegurada})")
        self.suma_asegurada = suma_asegurada


class FueraDeVigencia(ConflictoSiniestro):
    codigo = "FUERA_DE_VIGENCIA"

    def __init__(self) -> None:
        super().__init__("Solo puedes reportar lo que ocurrió mientras la póliza estaba activa")


class SiniestroEnCurso(ConflictoSiniestro):
    codigo = "SINIESTRO_EN_CURSO"

    def __init__(self) -> None:
        super().__init__("La póliza ya tiene un siniestro en curso para esa cobertura")


class TipoNoCubierto(DomainError):
    def __init__(self, tipo: TipoSiniestro) -> None:
        super().__init__(f"La póliza no tiene la cobertura de {tipo}")
        self.tipo = tipo


class SolicitudSiniestroInvalida(SolicitudPolizaInvalida):
    """Reporte bien formado pero que no se puede atender (falta la narración, el destino del pago
    o la evidencia). Responde 400 con el campo, como las solicitudes de póliza."""


class NumeroSiniestroDuplicado(DomainError):
    """El radicado generado ya lo tiene otro siniestro; el servicio genera otro."""


class AyudaNoDisponible(ConflictoSiniestro):
    """La ayuda en sitio es de Tu casa: un siniestro de incapacidad total o el pago automático por
    sismo no la tienen."""

    codigo = "AYUDA_NO_DISPONIBLE"

    def __init__(self, tipo: TipoSiniestro) -> None:
        super().__init__(f"Un siniestro de {tipo} no tiene ayuda en sitio")
        self.tipo = tipo


class ArchivoNoEncontrado(DomainError):
    def __init__(self, archivo_id: str) -> None:
        super().__init__(f"Archivo {archivo_id} no encontrado")
        self.archivo_id = archivo_id


class SiniestroNoEncontrado(DomainError):
    def __init__(self, siniestro_id: str) -> None:
        super().__init__(f"Siniestro {siniestro_id} no encontrado")
        self.siniestro_id = siniestro_id


@dataclass(frozen=True)
class HipotecaReportada:
    """Hipoteca abierta como la reporta svc-perfilamiento (nombre de la entidad sin normalizar)."""

    entidad_acreedora: str
    saldo_insoluto: Decimal
    cuota_mensual: Decimal | None = None


@dataclass(frozen=True)
class CreditosReportados:
    """``estado`` es ``DISPONIBLE``, ``NO_DISPONIBLE`` o ``SIN_CONSENTIMIENTO``."""

    estado: str
    hipotecas: tuple[HipotecaReportada, ...] = ()


def generar_numero_asistencia(solicitada_en: datetime, aleatorio: Callable[[str], str]) -> str:
    """``ASI-AAAA-XXXXXX``: lo que ve el cliente al confirmar su solicitud de ayuda."""
    sufijo = "".join(aleatorio(ALFABETO_NUMERO_POLIZA) for _ in range(6))
    return f"ASI-{solicitada_en.year}-{sufijo}"


def generar_numero_siniestro(reportado_en: datetime, aleatorio: Callable[[str], str]) -> str:
    """``SIN-AAAA-XXXXXX``: el radicado que ve el cliente. Seis caracteres aleatorios, como el de
    la póliza, para no crear un cuello de botella al escribir."""
    sufijo = "".join(aleatorio(ALFABETO_NUMERO_POLIZA) for _ in range(6))
    return f"SIN-{reportado_en.year}-{sufijo}"
