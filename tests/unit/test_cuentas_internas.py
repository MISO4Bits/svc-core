"""BITS-374: cuentas internas del personal (servicio, adaptador de Identity Platform y fábrica)."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from app.adapters import factory
from app.adapters.cuentas_fake import CuentasInternasEnMemoria
from app.adapters.identity_admin import IdentityPlatformAdmin
from app.api.app import _sembrar_admin, create_app
from app.config import Settings
from app.cuentas_internas import CuentasInternasService
from app.domain import (
    CorreoInternoInvalido,
    CuentaInterna,
    CuentaInternaYaExiste,
    IdentidadNoDisponible,
    NombreSocioObligatorio,
    RolInterno,
    es_correo_interno,
)

ADMIN = "admin@solventa4bits.com"
CLAVE = "clave-maestra-1"


def _servicio() -> tuple[CuentasInternasService, CuentasInternasEnMemoria]:
    cuentas = CuentasInternasEnMemoria()
    return CuentasInternasService(cuentas), cuentas


# --- dominio ---


@pytest.mark.parametrize(
    ("correo", "esperado"),
    [
        ("ana@solventa4bits.com", True),
        ("ANA@Solventa4Bits.COM", True),
        ("  ana@solventa4bits.com ", True),
        ("ana@gmail.com", False),
        ("ana@solventa4bits.com.co", False),
        ("ana@sub.solventa4bits.com", False),
        ("@solventa4bits.com", False),
        ("solventa4bits.com", False),
        ("", False),
    ],
)
def test_es_correo_interno(correo, esperado):
    assert es_correo_interno(correo) is esperado


# --- servicio ---


async def test_crear_socio_guarda_el_correo_en_minusculas_y_el_nombre():
    servicio, cuentas = _servicio()

    cuenta = await servicio.crear("  Socio@Solventa4Bits.com ", CLAVE, RolInterno.SOCIO, " Aliado ")

    assert cuenta.correo == "socio@solventa4bits.com"
    assert cuenta.rol is RolInterno.SOCIO
    assert cuenta.nombre == "Aliado"
    assert await cuentas.buscar_por_correo("socio@solventa4bits.com") == cuenta


async def test_crear_admin_no_exige_nombre():
    servicio, _ = _servicio()

    cuenta = await servicio.crear(ADMIN, CLAVE, RolInterno.ADMIN, None)

    assert cuenta.rol is RolInterno.ADMIN
    assert cuenta.nombre is None


async def test_crear_socio_sin_nombre_falla():
    servicio, _ = _servicio()

    for nombre in (None, "", "   "):
        with pytest.raises(NombreSocioObligatorio):
            await servicio.crear("socio@solventa4bits.com", CLAVE, RolInterno.SOCIO, nombre)


async def test_crear_con_correo_de_otro_dominio_falla_sin_llamar_al_proveedor():
    servicio, cuentas = _servicio()

    with pytest.raises(CorreoInternoInvalido):
        await servicio.crear("ana@gmail.com", CLAVE, RolInterno.ADMIN, None)

    assert cuentas.contrasenas == {}


async def test_crear_con_correo_repetido_falla_aunque_cambien_las_mayusculas():
    servicio, _ = _servicio()
    await servicio.crear(ADMIN, CLAVE, RolInterno.ADMIN, None)

    with pytest.raises(CuentaInternaYaExiste):
        await servicio.crear(
            ADMIN.upper().replace("SOLVENTA4BITS.COM", "solventa4bits.com"),
            CLAVE,
            RolInterno.ADMIN,
            None,
        )


async def test_el_nombre_del_socio_puede_repetirse():
    servicio, _ = _servicio()

    primero = await servicio.crear("uno@solventa4bits.com", CLAVE, RolInterno.SOCIO, "Aliado")
    segundo = await servicio.crear("dos@solventa4bits.com", CLAVE, RolInterno.SOCIO, "Aliado")

    assert primero.uid != segundo.uid


# --- semilla ---


async def test_la_semilla_crea_el_admin_si_no_existe():
    servicio, cuentas = _servicio()

    cuenta = await servicio.asegurar_admin(ADMIN, CLAVE)

    assert cuenta is not None
    assert cuenta.rol is RolInterno.ADMIN
    assert cuentas.contrasenas[ADMIN] == CLAVE


async def test_la_semilla_es_idempotente_y_no_pisa_la_contrasena():
    servicio, cuentas = _servicio()
    primera = await servicio.asegurar_admin(ADMIN, CLAVE)
    cuentas.contrasenas[ADMIN] = "la-que-cambio-david"

    segunda = await servicio.asegurar_admin(ADMIN, "otra-clave-distinta")

    assert segunda == primera
    assert cuentas.contrasenas[ADMIN] == "la-que-cambio-david"


async def test_la_semilla_no_modifica_una_cuenta_que_no_es_admin():
    servicio, cuentas = _servicio()
    await cuentas.crear_cuenta(ADMIN, CLAVE, RolInterno.SOCIO, "Aliado")

    assert await servicio.asegurar_admin(ADMIN, CLAVE) is None
    assert (await cuentas.buscar_por_correo(ADMIN)).rol is RolInterno.SOCIO


async def test_la_semilla_con_correo_de_otro_dominio_falla():
    servicio, _ = _servicio()

    with pytest.raises(CorreoInternoInvalido):
        await servicio.asegurar_admin("admin@gmail.com", CLAVE)


async def test_la_semilla_tolera_la_carrera_con_otro_pod():
    class _CarreraConOtroPod(CuentasInternasEnMemoria):
        """La búsqueda no la ve, pero otro pod la creó justo antes."""

        def __init__(self) -> None:
            super().__init__()
            self.busquedas = 0

        async def buscar_por_correo(self, correo):
            self.busquedas += 1
            if self.busquedas == 1:
                return None
            return await super().buscar_por_correo(correo)

    cuentas = _CarreraConOtroPod()
    await cuentas.crear_cuenta(ADMIN, CLAVE, RolInterno.ADMIN, "Administrador")

    cuenta = await CuentasInternasService(cuentas).asegurar_admin(ADMIN, CLAVE)

    assert cuenta is not None
    assert cuenta.correo == ADMIN


async def test_sembrar_admin_registra_el_fallo_sin_secretos_y_no_lo_propaga(caplog):
    class _Caido(CuentasInternasEnMemoria):
        async def buscar_por_correo(self, correo):
            raise IdentidadNoDisponible()

    with caplog.at_level(logging.ERROR, logger="svc_core.semilla"):
        await _sembrar_admin(CuentasInternasService(_Caido()), ADMIN, CLAVE)

    assert "IdentidadNoDisponible" in caplog.text
    assert CLAVE not in caplog.text
    assert ADMIN not in caplog.text


async def test_el_arranque_siembra_el_admin_en_segundo_plano():
    settings = Settings(
        repository_backend="memory",
        event_backend="memory",
        almacen_backend="memory",
        cuentas_internas_backend="fake",
        admin_seed_email=ADMIN,
        admin_seed_password=CLAVE,
    )
    aplicacion = create_app(settings)

    async with aplicacion.router.lifespan_context(aplicacion):
        for _ in range(5):
            await asyncio.sleep(0)
        servicio = aplicacion.state.cuentas_internas
        cuenta = await servicio._cuentas.buscar_por_correo(ADMIN)

    assert cuenta is not None
    assert cuenta.rol is RolInterno.ADMIN


async def test_el_arranque_no_siembra_si_falta_la_contrasena():
    settings = Settings(
        repository_backend="memory",
        event_backend="memory",
        almacen_backend="memory",
        cuentas_internas_backend="fake",
        admin_seed_email=ADMIN,
    )
    aplicacion = create_app(settings)

    async with aplicacion.router.lifespan_context(aplicacion):
        for _ in range(5):
            await asyncio.sleep(0)
        assert await aplicacion.state.cuentas_internas._cuentas.buscar_por_correo(ADMIN) is None


# --- adaptador de Identity Platform ---


async def _token() -> str:
    return "token-de-servicio"


def _admin(handler) -> IdentityPlatformAdmin:
    cliente = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return IdentityPlatformAdmin("solventa-dev", _token, client=cliente)


async def test_crear_cuenta_pide_correo_verificado_rol_y_nombre():
    pedidos: list[httpx.Request] = []

    def handler(request):
        pedidos.append(request)
        return httpx.Response(200, json={"localId": "uid-1", "email": "socio@solventa4bits.com"})

    admin = _admin(handler)
    cuenta = await admin.crear_cuenta("socio@solventa4bits.com", CLAVE, RolInterno.SOCIO, "Aliado")

    assert cuenta == CuentaInterna("uid-1", "socio@solventa4bits.com", RolInterno.SOCIO, "Aliado")
    pedido = pedidos[0]
    assert str(pedido.url) == (
        "https://identitytoolkit.googleapis.com/v1/projects/solventa-dev/accounts"
    )
    assert pedido.headers["Authorization"] == "Bearer token-de-servicio"
    cuerpo = json.loads(pedido.content)
    assert cuerpo["emailVerified"] is True
    assert cuerpo["displayName"] == "Aliado"
    assert json.loads(cuerpo["customAttributes"]) == {"rol": "SOCIO"}
    await admin.aclose()


async def test_crear_cuenta_sin_nombre_no_envia_displayname():
    cuerpos: list[dict] = []

    def handler(request):
        cuerpos.append(json.loads(request.content))
        return httpx.Response(200, json={"localId": "uid-2"})

    await _admin(handler).crear_cuenta(ADMIN, CLAVE, RolInterno.ADMIN, None)

    assert "displayName" not in cuerpos[0]


async def test_crear_cuenta_con_correo_existente_es_conflicto():
    admin = _admin(lambda _r: httpx.Response(400, json={"error": {"message": "EMAIL_EXISTS"}}))

    with pytest.raises(CuentaInternaYaExiste):
        await admin.crear_cuenta(ADMIN, CLAVE, RolInterno.ADMIN, None)


@pytest.mark.parametrize(
    "respuesta",
    [
        httpx.Response(400, json={"error": {"message": "WEAK_PASSWORD : x"}}),
        httpx.Response(403, json={"error": {"message": "PERMISSION_DENIED"}}),
        httpx.Response(500, text="boom"),
        httpx.Response(400, text="no es json"),
    ],
)
async def test_crear_cuenta_con_otro_error_no_expone_el_cuerpo(respuesta, caplog):
    admin = _admin(lambda _r: respuesta)

    with caplog.at_level(logging.WARNING), pytest.raises(IdentidadNoDisponible):
        await admin.crear_cuenta(ADMIN, CLAVE, RolInterno.ADMIN, None)

    assert CLAVE not in caplog.text
    assert ADMIN not in caplog.text


async def test_crear_cuenta_con_la_red_caida():
    def handler(_request):
        raise httpx.ConnectError("sin red")

    with pytest.raises(IdentidadNoDisponible):
        await _admin(handler).crear_cuenta(ADMIN, CLAVE, RolInterno.ADMIN, None)


async def test_buscar_por_correo_lee_el_rol_y_el_nombre():
    pedidos: list[httpx.Request] = []

    def handler(request):
        pedidos.append(request)
        usuario = {
            "localId": "uid-3",
            "email": ADMIN,
            "displayName": "Administrador",
            "customAttributes": '{"rol": "ADMIN"}',
        }
        return httpx.Response(200, json={"users": [usuario]})

    cuenta = await _admin(handler).buscar_por_correo(ADMIN)

    assert cuenta == CuentaInterna("uid-3", ADMIN, RolInterno.ADMIN, "Administrador")
    assert str(pedidos[0].url).endswith("/projects/solventa-dev/accounts:lookup")
    assert json.loads(pedidos[0].content) == {"email": [ADMIN]}


async def test_buscar_por_correo_sin_resultado():
    assert (
        await _admin(lambda _r: httpx.Response(200, json={"kind": "x"})).buscar_por_correo(ADMIN)
        is None
    )


@pytest.mark.parametrize("atributos", [None, "", "no-es-json", '{"rol": "HACKER"}', "[]", "{}"])
async def test_buscar_por_correo_de_una_cuenta_sin_rol_valido(atributos):
    usuario = {"localId": "uid-4", "email": "cliente@example.com", "customAttributes": atributos}
    admin = _admin(lambda _r: httpx.Response(200, json={"users": [usuario]}))

    cuenta = await admin.buscar_por_correo("cliente@example.com")

    assert cuenta is not None
    assert cuenta.rol is None


async def test_buscar_por_correo_con_error_del_proveedor():
    with pytest.raises(IdentidadNoDisponible):
        await _admin(lambda _r: httpx.Response(500)).buscar_por_correo(ADMIN)


# --- fábrica ---


def test_fabrica_off_no_construye_nada():
    assert factory.build_cuentas_internas(Settings()) == (None, [])


def test_fabrica_fake_usa_el_doble_en_memoria():
    servicio, cerrables = factory.build_cuentas_internas(Settings(cuentas_internas_backend="fake"))

    assert isinstance(servicio, CuentasInternasService)
    assert cerrables == []


def test_fabrica_real_exige_el_proyecto_de_identity_platform():
    with pytest.raises(ValueError, match="CORE_IDENTITY_PROJECT_ID"):
        factory.build_cuentas_internas(Settings(cuentas_internas_backend="real"))


async def test_fabrica_real_construye_el_adaptador_y_lo_deja_cerrable(monkeypatch):
    monkeypatch.setattr("app.adapters.identity_admin.token_de_servicio", lambda: _token)

    servicio, cerrables = factory.build_cuentas_internas(
        Settings(cuentas_internas_backend="real", identity_project_id="solventa-dev")
    )

    assert isinstance(servicio, CuentasInternasService)
    assert [type(c).__name__ for c in cerrables] == ["IdentityPlatformAdmin"]
    for cerrable in cerrables:
        await cerrable.aclose()


def test_fabrica_rechaza_un_backend_desconocido():
    with pytest.raises(ValueError, match="no soportado"):
        factory.build_cuentas_internas(Settings(cuentas_internas_backend="paloma"))


# --- configuración ---


def test_la_contrasena_de_la_semilla_se_lee_del_archivo_montado(tmp_path):
    (tmp_path / "CORE_ADMIN_SEED_PASSWORD").write_text(f"{CLAVE}\n", encoding="utf-8")

    settings = Settings(_secrets_dir=tmp_path)

    assert settings.admin_seed_password is not None
    assert settings.admin_seed_password.get_secret_value() == CLAVE
    assert CLAVE not in repr(settings)
