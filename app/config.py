from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración por variables de entorno (prefijo ``CORE_``)."""

    model_config = SettingsConfigDict(env_prefix="CORE_", env_file=".env", extra="ignore")

    service_name: str = "svc-core"
    environment: str = "local"

    # Persistencia: "sqlite" (local / experimento) | "memory" (pruebas)
    # En producción se añade el adaptador Cloud Spanner con el mismo puerto.
    repository_backend: str = "sqlite"
    database_path: str = "./svc_core.db"

    # Publicación de eventos de dominio: "logging" | "memory" | "pubsub"
    event_backend: str = "logging"
    pubsub_project_id: str | None = None
    pubsub_topic: str = "solventa-dominio"

    # Correos del registro (bienvenida y verificación de correo): Core arma el
    # mensaje (plantilla de Productos + datos del cliente) y lo publica como
    # evento "EnviarCorreo"; la función fn-notificaciones solo lo envía.
    # "off" (no envía nada) | "fake" (dobles locales) | "real" (Productos + Identity Platform)
    notificaciones_backend: str = "off"
    productos_base_url: str = "http://svc-productos.svc-productos.svc.cluster.local"
    productos_timeout_s: float = 2.0
    identity_project_id: str | None = None
    # URLs de la web que van dentro del correo (sin barra final en la base).
    web_url_base: str = "https://solventa4bits.com"
    web_url_verificacion: str = "https://solventa4bits.com/verificar-correo"
    correo_mercado: str = "CO"
    correo_idioma: str = "es-CO"

    # Evidencias de identidad (BITS-275): cédula por ambas caras y selfie.
    # Verificador: "mock" (veredictos configurables) | "local" (análisis de imagen en proceso).
    verificador_backend: str = "mock"
    # Veredictos del mock, en rotación: "ACEPTADA", "REVISION_MANUAL" o un motivo de rechazo
    # (BORROSA, OSCURA, REFLEJO, RECORTADA, NO_ES_CEDULA). P. ej. "BORROSA,ACEPTADA".
    verificador_mock_secuencia: str = "ACEPTADA"
    # Si el verificador tarda más, la evidencia pasa a revisión manual (no se bloquea al usuario).
    verificador_timeout_s: float = 2.5
    # Análisis simultáneos por pod: usan procesador y comparten pod con las transacciones.
    verificador_max_concurrentes: int = 2
    # Almacén de imágenes: "memory" | "filesystem" | "gcs"
    almacen_backend: str = "filesystem"
    almacen_ruta: str = "./evidencias"
    almacen_bucket: str | None = None
    evidencia_max_bytes: int = 3 * 1024 * 1024
    evidencia_max_pixeles: int = 40_000_000
    evidencia_intentos_max: int = 3
    # Umbrales de calidad. Provisionales hasta calibrarlos con fotos reales
    # (tools/calibrar_calidad.py); se ajustan por entorno sin tocar el algoritmo.
    calidad_lado_min_px: int = 600
    calidad_nitidez_min: float = 40.0
    calidad_luz_media_min: float = 55.0
    calidad_luz_media_max: float = 225.0
    calidad_oscuros_max: float = 0.60
    calidad_quemados_max: float = 0.45
    calidad_reflejo_max: float = 0.04
    calidad_proporcion_tarjeta: float = 1.586
    calidad_proporcion_tolerancia: float = 0.25
    calidad_tarjeta_area_min: float = 0.25
    calidad_margen_borde: float = 0.01

    # Prueba de vida y comparación de la selfie con la cédula (BITS-235).
    # "mock" (veredictos configurables) | "local" (modelos de OpenCV dentro del pod).
    biometria_backend: str = "mock"
    # Igual que verificador_mock_secuencia, con los motivos de la selfie (SIN_ROSTRO,
    # VARIOS_ROSTROS, POSIBLE_SUPLANTACION, NO_COINCIDE_CON_CEDULA).
    biometria_mock_secuencia: str = "ACEPTADA"
    biometria_timeout_s: float = 5.0
    # Los modelos de visión usan mucho procesador y comparten pod con las transacciones.
    biometria_max_concurrentes: int = 1
    biometria_modelos_ruta: str = "./models"
    # Dos umbrales por puntaje: sobre "aceptar" pasa, bajo "rechazar" se pide repetir y en
    # medio decide una persona. Provisionales hasta calibrarlos con fotos reales
    # (tools/calibrar_biometria.py). Vida: probabilidad de rostro real, 0 a 1. Coincidencia:
    # similitud coseno de SFace (la referencia de OpenCV para "misma persona" es ~0,36).
    biometria_vida_aceptar: float = 0.80
    biometria_vida_rechazar: float = 0.40
    biometria_coincidencia_aceptar: float = 0.40
    biometria_coincidencia_rechazar: float = 0.25
    # Confianza mínima del detector de rostros y tamaño mínimo del rostro (fracción del ancho).
    biometria_deteccion_min: float = 0.70
    biometria_rostro_min: float = 0.15

    # Pólizas (BITS-101). Cotización activa del cliente: "fake" (dobles locales) | "http"
    # (svc-cotizacion, ruta interna). Documento de la póliza: "fake" | "http".
    cotizacion_backend: str = "fake"
    # Tráfico interno del clúster: sin TLS entre pods (igual que productos_base_url).
    cotizacion_base_url: str = "http://svc-cotizacion.svc-cotizacion.svc.cluster.local"  # NOSONAR
    cotizacion_timeout_s: float = 1.0
    documento_backend: str = "fake"
    # Consumo de CotizacionCreada / CotizacionEliminada (borra la póliza pendiente de la
    # cotización anterior): "off" | "pubsub" (suscripción en el tópico compartido).
    consumidor_backend: str = "off"
    pubsub_subscription: str = "core-cotizacion"

    # Observabilidad (DI-008): OTLP/gRPC hacia Grafana Alloy dentro del
    # cluster. Deshabilitado por defecto — en local/tests no hay receptor
    # escuchando; se habilita vía CORE_OTEL_ENABLED=true en el manifiesto de
    # despliegue.
    otel_enabled: bool = False
    otel_exporter_endpoint: str = (
        "k8s-monitoring-alloy-receiver.observability.svc.cluster.local:4317"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
