"""Correos del registro: armado del mensaje, aislamiento de fallos y trazabilidad."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from app.adapters.memory import InMemoryEventPublisher
from app.adapters.notificaciones_fake import FakeIdentidadAdmin, FakePlantillasCorreo
from app.domain import (
    Cliente,
    DomainEvent,
    NotificacionError,
    PlantillaCorreo,
    TipoCorreo,
    TipoDocumento,
)
from app.notificaciones import (
    EVENTO_ENVIAR_CORREO,
    NotificacionService,
    con_parametro,
    ejecutar_con_contexto,
    rellenar,
)

PLANTILLA_BIENVENIDA = PlantillaCorreo(
    tipo="bienvenida",
    version="V2",
    asunto="Hola {{nombre}}",
    cuerpo_html='<p>Hola {{nombre}}</p><a href="{{urlWeb}}">Ir</a> {{anio}}',
    cuerpo_texto="Hola {{nombre}}: {{urlWeb}} ({{anio}})",
)
PLANTILLA_VERIFICACION = PlantillaCorreo(
    tipo="verificacion-correo",
    version="V1",
    asunto="Confirma tu correo, {{nombre}}",
    cuerpo_html='<a href="{{enlaceVerificacion}}">Confirmar</a>',
    cuerpo_texto="Confirma en {{enlaceVerificacion}}",
)


class PlantillasEnMemoria:
    def __init__(self, **plantillas: PlantillaCorreo) -> None:
        self._plantillas = {
            TipoCorreo.BIENVENIDA: plantillas.get("bienvenida", PLANTILLA_BIENVENIDA),
            TipoCorreo.VERIFICACION_CORREO: plantillas.get("verificacion", PLANTILLA_VERIFICACION),
        }

    async def obtener_vigente(self, tipo, mercado, idioma):
        self.ultima_consulta = (tipo, mercado, idioma)
        return self._plantillas[tipo]


def _cliente(**cambios) -> Cliente:
    datos = {
        "identity_ref": "sub-1",
        "tipo_documento": TipoDocumento.CC,
        "numero_documento": "123456",
        "primer_nombre": "Ana",
        "primer_apellido": "Ríos",
        "fecha_nacimiento": date(1990, 1, 1),
        "email": "ana@example.com",
        "telefono": "+573001234567",
    }
    return Cliente(**{**datos, **cambios})


def _servicio(events, plantillas=None, identidad=None) -> NotificacionService:
    return NotificacionService(
        plantillas or PlantillasEnMemoria(),
        identidad or FakeIdentidadAdmin(),
        events,
        url_web="https://solventa4bits.com",
        url_verificacion="https://solventa4bits.com/verificar-correo",
        mercado="CO",
        idioma="es-CO",
        ahora=lambda: datetime(2026, 10, 8, tzinfo=UTC),
    )


@pytest.fixture
def events() -> InMemoryEventPublisher:
    return InMemoryEventPublisher()


def _por_plantilla(events: InMemoryEventPublisher) -> dict[str, dict]:
    return {e.datos["plantilla"]: e.datos for e in events.events if e.tipo == EVENTO_ENVIAR_CORREO}


# --- rellenar y con_parametro ---


def test_rellenar_html_escapa_los_valores():
    html = rellenar("<p>{{nombre}}</p>", {"nombre": '<b>"Ana" & Co</b>'}, como_html=True)
    assert html == "<p>&lt;b&gt;&quot;Ana&quot; &amp; Co&lt;/b&gt;</p>"


def test_rellenar_texto_no_escapa():
    assert rellenar("Hola {{nombre}}", {"nombre": "A&B"}, como_html=False) == "Hola A&B"


def test_rellenar_con_variable_desconocida_falla():
    with pytest.raises(NotificacionError, match="otra"):
        rellenar("{{nombre}} {{otra}}", {"nombre": "Ana"}, como_html=True)


def test_con_parametro_agrega_y_codifica():
    assert (
        con_parametro("https://x.co/verificar-correo", "oobCode", "a b&c")
        == "https://x.co/verificar-correo?oobCode=a+b%26c"
    )


def test_con_parametro_conserva_la_consulta_existente():
    url = con_parametro("https://x.co/v?lang=es", "oobCode", "abc")
    assert url == "https://x.co/v?lang=es&oobCode=abc"


# --- servicio ---


async def test_publica_un_evento_por_correo_con_el_mensaje_ya_armado(events):
    identidad = FakeIdentidadAdmin()
    plantillas = PlantillasEnMemoria()
    cliente = _cliente()

    await _servicio(events, plantillas, identidad).enviar_correos_del_registro(cliente)

    correos = _por_plantilla(events)
    assert set(correos) == {"bienvenida", "verificacion-correo"}
    bienvenida = correos["bienvenida"]
    assert bienvenida["destinatario"] == "ana@example.com"
    assert bienvenida["clienteId"] == cliente.id
    assert bienvenida["plantillaVersion"] == "V2"
    assert bienvenida["asunto"] == "Hola Ana"
    assert (
        bienvenida["cuerpoHtml"] == '<p>Hola Ana</p><a href="https://solventa4bits.com">Ir</a> 2026'
    )
    assert bienvenida["cuerpoTexto"] == "Hola Ana: https://solventa4bits.com (2026)"
    enlace = "https://solventa4bits.com/verificar-correo?oobCode=codigo-local-1"
    verificacion = correos["verificacion-correo"]
    assert verificacion["asunto"] == "Confirma tu correo, Ana"
    assert enlace in verificacion["cuerpoHtml"]
    assert enlace in verificacion["cuerpoTexto"]
    assert identidad.codigos == [("ana@example.com", "codigo-local-1")]
    assert plantillas.ultima_consulta[1:] == ("CO", "es-CO")


async def test_el_nombre_se_escapa_en_el_html_y_no_rompe_el_asunto(events):
    cliente = _cliente(primer_nombre="<script>x</script>\nBcc: otro@malo.co")

    await _servicio(events).enviar_correos_del_registro(cliente)

    bienvenida = _por_plantilla(events)["bienvenida"]
    assert "<script>" not in bienvenida["cuerpoHtml"]
    assert "&lt;script&gt;" in bienvenida["cuerpoHtml"]
    assert "\n" not in bienvenida["asunto"]
    assert "\r" not in bienvenida["asunto"]


async def test_si_falla_la_verificacion_igual_sale_la_bienvenida(events):
    class IdentidadCaida:
        async def generar_codigo_verificacion(self, email):
            raise NotificacionError("Identity Platform no está disponible")

    await _servicio(events, identidad=IdentidadCaida()).enviar_correos_del_registro(_cliente())

    assert set(_por_plantilla(events)) == {"bienvenida"}


async def test_si_falla_productos_no_sale_ningun_correo_y_no_se_propaga(events):
    class ProductosCaido:
        async def obtener_vigente(self, tipo, mercado, idioma):
            raise NotificacionError("Productos no está disponible")

    await _servicio(events, plantillas=ProductosCaido()).enviar_correos_del_registro(_cliente())

    assert events.events == []


async def test_un_fallo_inesperado_no_se_propaga(events):
    class Roto:
        async def obtener_vigente(self, tipo, mercado, idioma):
            raise RuntimeError("boom")

    await _servicio(events, plantillas=Roto()).enviar_correos_del_registro(_cliente())

    assert events.events == []


async def test_una_variable_desconocida_en_la_plantilla_no_envia_ese_correo(events):
    mala = PlantillaCorreo("bienvenida", "V3", "Hola {{nombre}}", "<p>{{otra}}</p>", "Hola")
    servicio = _servicio(events, plantillas=PlantillasEnMemoria(bienvenida=mala))

    await servicio.enviar_correos_del_registro(_cliente())

    assert set(_por_plantilla(events)) == {"verificacion-correo"}


async def test_las_plantillas_falsas_de_desarrollo_se_pueden_rellenar(events):
    servicio = NotificacionService(
        FakePlantillasCorreo(),
        FakeIdentidadAdmin(),
        events,
        url_web="https://w.co",
        url_verificacion="https://w.co/v",
        mercado="CO",
        idioma="es-CO",
    )

    await servicio.enviar_correos_del_registro(_cliente())

    assert set(_por_plantilla(events)) == {"bienvenida", "verificacion-correo"}


# --- trazabilidad ---


async def test_ejecutar_con_contexto_conserva_el_trace_id():
    tracer = TracerProvider().get_tracer("prueba")
    vistos: list[int] = []

    async def _tarea() -> None:
        vistos.append(trace.get_current_span().get_span_context().trace_id)

    with tracer.start_as_current_span("peticion") as span:
        contexto = otel_context.get_current()
        esperado = span.get_span_context().trace_id

    # La petición ya terminó: sin el contexto capturado no habría span activo.
    await _tarea()
    assert vistos[-1] != esperado
    await ejecutar_con_contexto(contexto, _tarea)
    assert vistos[-1] == esperado
    # y el contexto anterior se restablece
    assert trace.get_current_span().get_span_context().trace_id != esperado


async def test_el_evento_se_publica_dentro_del_trace_de_la_peticion():
    tracer = TracerProvider().get_tracer("prueba")
    trazas: list[int] = []

    class Publicador:
        async def publish(self, event: DomainEvent) -> None:
            trazas.append(trace.get_current_span().get_span_context().trace_id)

    servicio = _servicio(Publicador())
    with tracer.start_as_current_span("peticion") as span:
        contexto = otel_context.get_current()
        esperado = span.get_span_context().trace_id

    await ejecutar_con_contexto(contexto, servicio.enviar_correos_del_registro, _cliente())

    assert trazas == [esperado, esperado]
