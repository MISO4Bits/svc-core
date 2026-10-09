"""Correos del registro: bienvenida y verificación de correo.

CoreTransaccional arma el mensaje completo (plantilla de Productos rellenada con
los datos del cliente) y lo publica como evento ``EnviarCorreo``. La función
``fn-notificaciones`` no decide nada: recibe el evento y lo envía.

Todo esto corre fuera de la petición de registro: un fallo aquí se registra pero
nunca hace fallar ni demora el alta del cliente.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from opentelemetry import context as otel_context
from opentelemetry import trace

from app.domain import Cliente, DomainEvent, NotificacionError, PlantillaCorreo, TipoCorreo
from app.ports import EventPublisher, IdentidadAdminPort, PlantillasCorreoPort

logger = logging.getLogger("svc_core.notificaciones")
tracer = trace.get_tracer("svc_core.notificaciones")

EVENTO_ENVIAR_CORREO = "EnviarCorreo"
_MARCADOR = re.compile(r"\{\{(\w+)\}\}")


def _una_linea(valor: str) -> str:
    """Sin saltos de línea ni espacios repetidos (el asunto es un encabezado de correo)."""
    return " ".join(valor.split())


def rellenar(plantilla: str, variables: dict[str, str], *, como_html: bool) -> str:
    """Sustituye ``{{variable}}``. En HTML escapa los valores; una variable sin valor
    es un error (no se envía un correo con un marcador a la vista)."""

    def _valor(coincidencia: re.Match[str]) -> str:
        nombre = coincidencia.group(1)
        if nombre not in variables:
            raise NotificacionError(f"La plantilla usa la variable {nombre}, que no se conoce")
        valor = variables[nombre]
        return html.escape(valor, quote=True) if como_html else valor

    return _MARCADOR.sub(_valor, plantilla)


def con_parametro(url: str, nombre: str, valor: str) -> str:
    partes = urlsplit(url)
    consulta = [*parse_qsl(partes.query, keep_blank_values=True), (nombre, valor)]
    return urlunsplit(partes._replace(query=urlencode(consulta)))


class NotificacionService:
    def __init__(
        self,
        plantillas: PlantillasCorreoPort,
        identidad: IdentidadAdminPort,
        events: EventPublisher,
        *,
        url_web: str,
        url_verificacion: str,
        mercado: str,
        idioma: str,
        ahora: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._plantillas = plantillas
        self._identidad = identidad
        self._events = events
        self._url_web = url_web
        self._url_verificacion = url_verificacion
        self._mercado = mercado
        self._idioma = idioma
        self._ahora = ahora

    async def enviar_correos_del_registro(self, cliente: Cliente) -> None:
        """Bienvenida y verificación, cada una por su lado: si una falla, la otra sale."""
        for tipo, variables_extra in (
            (TipoCorreo.BIENVENIDA, self._variables_bienvenida),
            (TipoCorreo.VERIFICACION_CORREO, self._variables_verificacion),
        ):
            with tracer.start_as_current_span(
                "core.preparar_correo", attributes={"correo.plantilla": str(tipo)}
            ):
                try:
                    variables = await variables_extra(cliente)
                    await self._publicar(cliente, tipo, variables)
                except NotificacionError as exc:
                    logger.warning("correo no enviado plantilla=%s motivo=%s", tipo, exc)
                except Exception:  # noqa: BLE001 - nada de esto puede afectar al registro
                    logger.warning("correo no enviado plantilla=%s", tipo, exc_info=True)

    def _variables_base(self, cliente: Cliente) -> dict[str, str]:
        return {"nombre": cliente.primer_nombre, "anio": str(self._ahora().year)}

    async def _variables_bienvenida(self, cliente: Cliente) -> dict[str, str]:
        return {**self._variables_base(cliente), "urlWeb": self._url_web}

    async def _variables_verificacion(self, cliente: Cliente) -> dict[str, str]:
        codigo = await self._identidad.generar_codigo_verificacion(cliente.email)
        enlace = con_parametro(self._url_verificacion, "oobCode", codigo)
        return {**self._variables_base(cliente), "enlaceVerificacion": enlace}

    async def _publicar(
        self, cliente: Cliente, tipo: TipoCorreo, variables: dict[str, str]
    ) -> None:
        plantilla = await self._plantillas.obtener_vigente(tipo, self._mercado, self._idioma)
        mensaje = self._armar(plantilla, variables)
        await self._events.publish(
            DomainEvent(
                EVENTO_ENVIAR_CORREO,
                {
                    "destinatario": cliente.email,
                    "plantilla": str(tipo),
                    "plantillaVersion": plantilla.version,
                    "clienteId": cliente.id,
                    **mensaje,
                },
            )
        )
        logger.info("correo publicado plantilla=%s version=%s", tipo, plantilla.version)

    @staticmethod
    def _armar(plantilla: PlantillaCorreo, variables: dict[str, str]) -> dict[str, str]:
        en_una_linea = {nombre: _una_linea(valor) for nombre, valor in variables.items()}
        return {
            "asunto": rellenar(plantilla.asunto, en_una_linea, como_html=False),
            "cuerpoHtml": rellenar(plantilla.cuerpo_html, variables, como_html=True),
            "cuerpoTexto": rellenar(plantilla.cuerpo_texto, variables, como_html=False),
        }


async def ejecutar_con_contexto(
    contexto: otel_context.Context,
    funcion: Callable[..., Awaitable[None]],
    *args: object,
) -> None:
    """Corre ``funcion`` dentro del contexto de traza de la petición que la originó,
    para que el trabajo posterior a la respuesta conserve el mismo ``trace_id``."""
    token = otel_context.attach(contexto)
    try:
        await funcion(*args)
    finally:
        otel_context.detach(token)
