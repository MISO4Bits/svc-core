"""Punto de entrada ASGI: ``uvicorn app.main:app``."""

from app.api.app import create_app

app = create_app()
