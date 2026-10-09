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
