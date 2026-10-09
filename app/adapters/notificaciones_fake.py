"""Dobles locales de Productos e Identity Platform (``CORE_NOTIFICACIONES_BACKEND=fake``)."""

from __future__ import annotations

from app.domain import PlantillaCorreo, TipoCorreo


class FakePlantillasCorreo:
    async def obtener_vigente(self, tipo: TipoCorreo, mercado: str, idioma: str) -> PlantillaCorreo:
        enlace = (
            "{{enlaceVerificacion}}" if tipo is TipoCorreo.VERIFICACION_CORREO else "{{urlWeb}}"
        )
        return PlantillaCorreo(
            tipo=str(tipo),
            version="V1",
            asunto="Hola {{nombre}}",
            cuerpo_html=f'<p>Hola {{{{nombre}}}}</p><a href="{enlace}">Ir</a>',
            cuerpo_texto=f"Hola {{{{nombre}}}}: {enlace}",
        )


class FakeIdentidadAdmin:
    def __init__(self) -> None:
        self.codigos: list[tuple[str, str]] = []

    async def generar_codigo_verificacion(self, email: str) -> str:
        codigo = f"codigo-local-{len(self.codigos) + 1}"
        self.codigos.append((email, codigo))
        return codigo
