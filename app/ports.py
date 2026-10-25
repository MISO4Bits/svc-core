"""Puertos (interfaces) del hexágono. Los adaptadores viven en ``app/adapters``."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from app.domain import (
    Cliente,
    Consentimiento,
    ConsentimientoScope,
    CotizacionActiva,
    Decision,
    DocumentoPoliza,
    DomainEvent,
    Evidencia,
    FiltroPolizas,
    MotivoRechazoPoliza,
    PaginaPolizas,
    Pendiente,
    PlantillaCorreo,
    Poliza,
    ReferenciaDocumento,
    TipoCorreo,
    TipoEvidencia,
    Veredicto,
    Vigencia,
)


@runtime_checkable
class ClienteRepository(Protocol):
    async def crear_con_consentimientos(
        self, cliente: Cliente, consentimientos: list[Consentimiento]
    ) -> Cliente:
        """Crea el cliente y sus consentimientos (OPEN_DATA, OPEN_FINANCE) atómicamente."""
        ...

    async def obtener(self, cliente_id: str) -> Cliente | None: ...

    async def obtener_por_identity_ref(self, identity_ref: str) -> Cliente | None: ...

    async def existe_por_documento(self, tipo_documento: str, numero_documento: str) -> bool: ...

    async def existe_por_correo(self, email: str) -> bool: ...

    async def confirmar(self, cliente_id: str) -> Cliente | None:
        """Idempotente: si ya estaba confirmado, no toca ``confirmado_en`` de nuevo."""
        ...


@runtime_checkable
class ConsentimientoRepository(Protocol):
    async def listar(self, cliente_id: str) -> list[Consentimiento]: ...

    async def obtener(
        self, cliente_id: str, scope: ConsentimientoScope
    ) -> Consentimiento | None: ...

    async def guardar(self, consentimiento: Consentimiento) -> Consentimiento: ...


@runtime_checkable
class IdempotencyStore(Protocol):
    async def get(self, key: str) -> dict | None: ...

    async def put(self, key: str, value: dict) -> None: ...


@runtime_checkable
class EventPublisher(Protocol):
    async def publish(self, event: DomainEvent) -> None: ...


@runtime_checkable
class PlantillasCorreoPort(Protocol):
    async def obtener_vigente(self, tipo: TipoCorreo, mercado: str, idioma: str) -> PlantillaCorreo:
        """Plantilla vigente (sin rellenar). Lanza ``NotificacionError`` si no la obtiene."""
        ...


@runtime_checkable
class IdentidadAdminPort(Protocol):
    async def generar_codigo_verificacion(self, email: str) -> str:
        """Pide a Identity Platform un código de un solo uso para verificar ese correo.
        Lanza ``NotificacionError`` si no lo obtiene."""
        ...


@runtime_checkable
class EvidenciaRepository(Protocol):
    async def vigente(self, cliente_id: str, tipo: TipoEvidencia) -> Evidencia | None:
        """Última evidencia del tipo que no fue reemplazada ni consumida por una póliza."""
        ...

    async def guardar_reemplazando(self, evidencia: Evidencia) -> Evidencia:
        """Guarda la evidencia y marca como reemplazada la vigente anterior del mismo tipo."""
        ...

    async def consumir(self, cliente_id: str, poliza_ref: str) -> int:
        """Asocia las vigentes del cliente a la póliza (ya no cuentan como vigentes).
        Devuelve cuántas consumió."""
        ...

    async def rechazos(self, cliente_id: str, tipo: TipoEvidencia) -> int:
        """Rechazos acumulados del tipo desde la última evidencia guardada."""
        ...

    async def registrar_rechazo(self, cliente_id: str, tipo: TipoEvidencia) -> int:
        """Suma un rechazo y devuelve el total."""
        ...

    async def reiniciar_rechazos(self, cliente_id: str, tipo: TipoEvidencia) -> None: ...


@runtime_checkable
class AlmacenEvidencias(Protocol):
    async def guardar(self, clave: str, datos: bytes, content_type: str) -> str:
        """Guarda la imagen y devuelve la referencia con la que se recupera."""
        ...

    async def obtener(self, referencia: str) -> bytes: ...


@runtime_checkable
class VerificadorDocumento(Protocol):
    async def analizar(
        self, imagen: bytes, tipo: TipoEvidencia, numero_esperado: str | None
    ) -> Veredicto:
        """Decide si la imagen sirve. Con ``numero_esperado`` y un reverso de cédula, la
        acepta solo si el código del reverso contiene ese número; si no puede leerlo o no
        coincide, devuelve ``REVISION_MANUAL``. El número leído no sale del verificador."""
        ...


@runtime_checkable
class VerificadorBiometrico(Protocol):
    async def verificar(self, selfie: bytes, frente_cedula: bytes) -> Veredicto:
        """Decide si la selfie sirve: calidad, un solo rostro, persona real y coincidencia con
        el rostro de la cédula. Si no hay rostro en la cédula devuelve ``REVISION_MANUAL``
        (el problema es del documento, no del usuario). Los puntajes del veredicto no salen
        del servicio."""
        ...


@runtime_checkable
class PolizaRepository(Protocol):
    async def crear(self, poliza: Poliza) -> None:
        """Guarda la solicitud. Lanza ``PolizaYaExiste`` si el cliente ya tiene una de esa
        cotización (carrera entre dos solicitudes)."""
        ...

    async def obtener(self, poliza_id: str) -> Poliza | None: ...

    async def obtener_de_cotizacion(
        self, cliente_id: str, cotizacion_creada_en: datetime
    ) -> Poliza | None: ...

    async def listar_de_cliente(self, cliente_id: str) -> list[Poliza]:
        """Todas las del cliente, de la más reciente a la más antigua."""
        ...

    async def pendientes_de_cliente(self, cliente_id: str) -> list[Poliza]:
        """Las que están en ``PENDIENTE`` (guardado), aunque su cotización ya haya vencido."""
        ...

    async def listar(self, filtro: FiltroPolizas, ahora: datetime) -> PaginaPolizas:
        """Cola del backoffice: pólizas de todos los clientes. ``VENCIDA`` se calcula con
        ``ahora`` porque no se guarda."""
        ...

    async def actualizar_pendientes(
        self, poliza_id: str, pendientes: tuple[Pendiente, ...], evaluada_en: datetime
    ) -> None:
        """Guarda la última evaluación. No toca una póliza que ya no esté ``PENDIENTE``."""
        ...

    async def emitir(
        self, poliza_id: str, numero: str, emitida_en: datetime, vigencia: Vigencia
    ) -> Poliza | None:
        """En una sola operación: pasa a ``EMITIDA`` (solo si sigue ``PENDIENTE``), asigna el
        número y consume las evidencias vigentes del cliente. Devuelve ``None`` si ya no estaba
        ``PENDIENTE``; lanza ``NumeroPolizaDuplicado`` si el número ya existe."""
        ...

    async def aprobar(self, poliza_id: str, decision: Decision) -> bool:
        """Agrega la decisión del agente. ``False`` si la póliza ya no está ``PENDIENTE``."""
        ...

    async def rechazar(
        self,
        poliza_id: str,
        decision: Decision,
        motivos: tuple[MotivoRechazoPoliza, ...],
        rechazada_en: datetime,
    ) -> Poliza | None:
        """Pasa a ``RECHAZADA`` (solo si sigue ``PENDIENTE``). ``None`` si ya no lo estaba."""
        ...

    async def reclamar_envio_documento(self, poliza_id: str, cuando: datetime) -> bool:
        """Marca que se va a enviar el correo con el PDF. ``True`` solo la primera vez: así una
        reentrega del aviso no manda el correo dos veces."""
        ...

    async def liberar_envio_documento(self, poliza_id: str) -> None:
        """Deshace el reclamo si el correo no se pudo encargar, para que se reintente."""
        ...

    async def borrar_pendientes_de_cliente(
        self, cliente_id: str, salvo_cotizacion: datetime | None
    ) -> int:
        """Borra las ``PENDIENTE`` del cliente cuya cotización no sea ``salvo_cotizacion``
        (todas si es ``None``). No toca emitidas ni rechazadas. Devuelve cuántas borró."""
        ...


@runtime_checkable
class CotizacionPort(Protocol):
    async def activa(self, cliente_id: str) -> CotizacionActiva | None:
        """La cotización activa del cliente, o ``None`` si no tiene. Lanza
        ``CotizacionNoDisponible`` si Cotización no responde."""
        ...


@runtime_checkable
class DocumentoPolizaPort(Protocol):
    async def consultar(self, datos: dict) -> DocumentoPoliza:
        """Enlace temporal para ver el PDF de una póliza emitida. ``datos`` es lo mismo que viaja
        en ``PolizaEmitida``: si el documento no existe, el servicio de documentos lo genera.
        Lanza ``DocumentoNoDisponible`` si no responde."""
        ...


@runtime_checkable
class CorreosDePolizaPort(Protocol):
    async def enviar_poliza_emitida(
        self, cliente: Cliente, numero: str, documento: ReferenciaDocumento
    ) -> None:
        """Encarga el correo con el PDF adjunto. A diferencia de los del registro, un fallo se
        propaga: quien llama reintenta."""
        ...
