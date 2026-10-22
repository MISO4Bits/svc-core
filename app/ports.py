"""Puertos (interfaces) del hexágono. Los adaptadores viven en ``app/adapters``."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.domain import (
    Cliente,
    Consentimiento,
    ConsentimientoScope,
    DomainEvent,
    Evidencia,
    PlantillaCorreo,
    TipoCorreo,
    TipoEvidencia,
    Veredicto,
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
