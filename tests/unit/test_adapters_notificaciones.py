"""Adaptadores de los correos del registro (Productos e Identity Platform) y fábrica."""

from __future__ import annotations

import json

import httpx
import pytest

from app.adapters import factory
from app.adapters.identity_admin import IdentityPlatformAdmin
from app.adapters.memory import InMemoryEventPublisher
from app.adapters.productos_http import ProductosPlantillasHttp
from app.config import Settings
from app.domain import NotificacionError, TipoCorreo
from app.notificaciones import NotificacionService

PLANTILLA_JSON = {
    "tipo": "bienvenida",
    "version": "V1",
    "asunto": "Hola {{nombre}}",
    "cuerpoHtml": "<p>{{nombre}}</p>",
    "cuerpoTexto": "{{nombre}}",
}


def _cliente_http(handler, base_url="http://productos") -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _sin_esperas(monkeypatch):
    async def _no_dormir(_segundos):
        return None

    monkeypatch.setattr("app.adapters.productos_http.asyncio.sleep", _no_dormir)


# --- Productos ---


async def test_productos_devuelve_la_plantilla_y_envia_mercado_e_idioma():
    pedidos: list[httpx.Request] = []

    def handler(request):
        pedidos.append(request)
        return httpx.Response(200, json=PLANTILLA_JSON)

    adaptador = ProductosPlantillasHttp("http://productos", client=_cliente_http(handler))
    plantilla = await adaptador.obtener_vigente(TipoCorreo.BIENVENIDA, "CO", "es-CO")

    assert (plantilla.tipo, plantilla.version) == ("bienvenida", "V1")
    assert plantilla.cuerpo_html == "<p>{{nombre}}</p>"
    assert pedidos[0].url.path == "/plantillas-correo/bienvenida"
    assert dict(pedidos[0].url.params) == {"mercado": "CO", "idioma": "es-CO"}
    await adaptador.aclose()


async def test_productos_404_es_un_error_de_notificacion_sin_reintentos():
    llamadas = []

    def handler(request):
        llamadas.append(1)
        return httpx.Response(404, json={"title": "no existe"})

    adaptador = ProductosPlantillasHttp("http://productos", client=_cliente_http(handler))
    with pytest.raises(NotificacionError, match="404"):
        await adaptador.obtener_vigente(TipoCorreo.BIENVENIDA, "CO", "es-CO")
    assert len(llamadas) == 1


async def test_productos_reintenta_ante_5xx_y_se_recupera():
    respuestas = iter([httpx.Response(503), httpx.Response(200, json=PLANTILLA_JSON)])
    adaptador = ProductosPlantillasHttp(
        "http://productos", client=_cliente_http(lambda _r: next(respuestas))
    )

    plantilla = await adaptador.obtener_vigente(TipoCorreo.BIENVENIDA, "CO", "es-CO")

    assert plantilla.version == "V1"


async def test_productos_reintenta_ante_fallas_de_red_y_se_rinde():
    llamadas = []

    def handler(request):
        llamadas.append(1)
        raise httpx.ConnectError("sin red")

    adaptador = ProductosPlantillasHttp("http://productos", client=_cliente_http(handler))
    with pytest.raises(NotificacionError, match="no está disponible"):
        await adaptador.obtener_vigente(TipoCorreo.BIENVENIDA, "CO", "es-CO")
    assert len(llamadas) == 3


async def test_productos_crea_su_propio_cliente_si_no_se_le_pasa_uno():
    adaptador = ProductosPlantillasHttp("http://productos", 1.5)
    await adaptador.aclose()


# --- Identity Platform ---


async def _token() -> str:
    return "token-de-servicio"


async def test_identity_devuelve_el_codigo_del_enlace():
    pedidos: list[httpx.Request] = []

    def handler(request):
        pedidos.append(request)
        enlace = "https://p.firebaseapp.com/__/auth/action?mode=verifyEmail&oobCode=ABC123&lang=es"
        return httpx.Response(200, json={"email": "ana@example.com", "oobLink": enlace})

    admin = IdentityPlatformAdmin("solventa-dev", _token, client=_cliente_http(handler))
    codigo = await admin.generar_codigo_verificacion("ana@example.com")

    assert codigo == "ABC123"
    pedido = pedidos[0]
    assert str(pedido.url) == (
        "https://identitytoolkit.googleapis.com/v1/projects/solventa-dev/accounts:sendOobCode"
    )
    assert pedido.headers["Authorization"] == "Bearer token-de-servicio"
    assert json.loads(pedido.content) == {
        "requestType": "VERIFY_EMAIL",
        "email": "ana@example.com",
        "returnOobLink": True,
    }
    await admin.aclose()


async def test_identity_error_no_expone_el_cuerpo():
    def handler(request):
        return httpx.Response(400, json={"error": {"message": "EMAIL_NOT_FOUND ana@example.com"}})

    admin = IdentityPlatformAdmin("p", _token, client=_cliente_http(handler))
    with pytest.raises(NotificacionError) as error:
        await admin.generar_codigo_verificacion("ana@example.com")
    assert "400" in str(error.value)
    assert "ana@example.com" not in str(error.value)


@pytest.mark.parametrize(
    "cuerpo", [{}, {"oobLink": ""}, {"oobLink": "https://x.co/?mode=verifyEmail"}]
)
async def test_identity_sin_codigo_en_la_respuesta(cuerpo):
    admin = IdentityPlatformAdmin(
        "p", _token, client=_cliente_http(lambda _r: httpx.Response(200, json=cuerpo))
    )
    with pytest.raises(NotificacionError, match="código"):
        await admin.generar_codigo_verificacion("ana@example.com")


async def test_identity_falla_de_red():
    def handler(request):
        raise httpx.ConnectError("sin red")

    admin = IdentityPlatformAdmin("p", _token, client=_cliente_http(handler))
    with pytest.raises(NotificacionError, match="no está disponible"):
        await admin.generar_codigo_verificacion("ana@example.com")


async def test_identity_crea_su_propio_cliente_si_no_se_le_pasa_uno():
    admin = IdentityPlatformAdmin("p", _token)
    await admin.aclose()


# --- fábrica ---


def test_fabrica_off_no_construye_nada():
    assert factory.build_notificaciones(Settings(), InMemoryEventPublisher()) == (None, [])


def test_fabrica_fake_construye_el_servicio_con_dobles():
    servicio, cerrables = factory.build_notificaciones(
        Settings(notificaciones_backend="fake"), InMemoryEventPublisher()
    )
    assert isinstance(servicio, NotificacionService)
    assert cerrables == []


def test_fabrica_real_exige_el_proyecto_de_identity_platform():
    with pytest.raises(ValueError, match="CORE_IDENTITY_PROJECT_ID"):
        factory.build_notificaciones(
            Settings(notificaciones_backend="real"), InMemoryEventPublisher()
        )


async def test_fabrica_real_construye_los_adaptadores_y_los_deja_cerrables(monkeypatch):
    monkeypatch.setattr("app.adapters.identity_admin.token_de_servicio", lambda: _token)
    servicio, cerrables = factory.build_notificaciones(
        Settings(notificaciones_backend="real", identity_project_id="solventa-dev"),
        InMemoryEventPublisher(),
    )
    assert isinstance(servicio, NotificacionService)
    assert [type(c).__name__ for c in cerrables] == [
        "ProductosPlantillasHttp",
        "IdentityPlatformAdmin",
    ]
    for cerrable in cerrables:
        await cerrable.aclose()


def test_fabrica_rechaza_un_backend_desconocido():
    with pytest.raises(ValueError, match="no soportado"):
        factory.build_notificaciones(
            Settings(notificaciones_backend="correo-por-paloma"), InMemoryEventPublisher()
        )
