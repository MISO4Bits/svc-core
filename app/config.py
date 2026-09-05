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


@lru_cache
def get_settings() -> Settings:
    return Settings()
