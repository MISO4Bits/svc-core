"""BITS-378/379: venta directa del socio — clientes asistidos, precio y emisión inmediata."""

from __future__ import annotations

import aiosqlite
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from jsonschema import Draft202012Validator

from app.adapters.factory import maybe_init
from app.api.app import create_app
from app.config import Settings
from app.domain import TipoEvidencia
from tests import imagenes
from tests.conftest import CLIENTE_VALIDO

CREDITO = {
    "valorCredito": 150000000,
    "plazoRestanteMeses": 240,
    "entidadAcreedora": "Bancolombia",
}
ASISTIDO = {
    "tipoDocumento": "CC",
    "numeroDocumento": "52123456",
    "primerNombre": "Laura",
    "segundoNombre": "María",
    "primerApellido": "Gómez",
    "fechaNacimiento": "1988-03-14",
    "email": "Laura.Gomez@Example.com",
    "telefono": "+573101112233",
}
VENTA = {
    "socioId": "socio-uid-1",
    "coberturas": ["VIDA", "HOGAR"],
    "cuotaMensual": 1250000,
    "direccion": "Calle 10 # 5-20 Apto 301",
    "municipio": "Bogotá",
    **CREDITO,
}


def _validar(spec: dict, ref: str, instancia) -> None:
    schema = {"$ref": f"#/components/schemas/{ref}", "components": spec["components"]}
    errores = sorted(Draft202012Validator(schema).iter_errors(instancia), key=str)
    assert not errores, [e.message for e in errores]


def _settings(tmp_path, **cambios) -> Settings:
    datos = {
        "repository_backend": "sqlite",
        "database_path": str(tmp_path / "venta.db"),
        "event_backend": "memory",
        "almacen_backend": "memory",
        "notificaciones_backend": "fake",
        "cuentas_internas_backend": "fake",
    }
    return Settings(**{**datos, **cambios})


@pytest_asyncio.fixture
async def app(tmp_path):
    aplicacion = create_app(_settings(tmp_path))
    await maybe_init(aplicacion.state.clientes)
    return aplicacion


@pytest_asyncio.fixture
async def http(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as cliente:
        yield cliente


def _correos(app) -> dict[str, dict]:
    return {
        e.datos["plantilla"]: e.datos for e in app.state.events.events if e.tipo == "EnviarCorreo"
    }


def _tipos(app) -> list[str]:
    return [e.tipo for e in app.state.events.events]


async def _registrar(http, **cambios) -> str:
    respuesta = await http.post("/v1/clientes", json={**CLIENTE_VALIDO, **cambios})
    assert respuesta.status_code == 201
    return respuesta.json()["id"]


async def _asistido(http, **cambios):
    return await http.post("/v1/clientes/asistidos", json={**ASISTIDO, **cambios})


# --- clientes asistidos ---


async def test_crea_el_cliente_con_cuenta_correo_confirmado_y_sin_autorizaciones(
    app, http, openapi_spec
):
    respuesta = await _asistido(http)

    assert respuesta.status_code == 201
    cuerpo = respuesta.json()
    _validar(openapi_spec, "Cliente", cuerpo)
    assert respuesta.headers["Location"] == f"/v1/clientes/{cuerpo['id']}"
    assert cuerpo["correoConfirmado"] is True
    assert cuerpo["email"] == "laura.gomez@example.com"
    assert cuerpo["estado"] == "ACTIVO"
    cuentas = app.state.clientes_asistidos._cuentas.cuentas
    assert list(cuentas) == [cuerpo["identityRef"]]
    assert cuentas[cuerpo["identityRef"]][0] == "laura.gomez@example.com"
    consentimientos = (await http.get(f"/v1/clientes/{cuerpo['id']}/consentimientos")).json()
    assert {c["scope"]: c["estado"] for c in consentimientos} == {
        "OPEN_DATA": "NO_OTORGADO",
        "OPEN_FINANCE": "NO_OTORGADO",
    }
    assert {c["canal"] for c in consentimientos} == {"BACKOFFICE"}
    registrado = next(e for e in app.state.events.events if e.tipo == "ClienteRegistrado")
    assert registrado.datos["autorizaTratamientoDatos"] is False
    assert registrado.datos["autorizaDatosFinancieros"] is False


async def test_la_contrasena_solo_viaja_en_el_correo_de_acceso(app, http):
    respuesta = await _asistido(http)

    cuerpo = respuesta.json()
    contrasena = app.state.clientes_asistidos._cuentas.cuentas[cuerpo["identityRef"]][1]
    assert len(contrasena) >= 12
    correos = _correos(app)
    assert list(correos) == ["cuenta-activada"]  # el aviso de venta sale al vender, no al alta
    assert correos["cuenta-activada"]["destinatario"] == "laura.gomez@example.com"
    assert contrasena in correos["cuenta-activada"]["cuerpoHtml"]
    assert contrasena in correos["cuenta-activada"]["cuerpoTexto"]
    assert contrasena not in respuesta.text
    otros = [e for e in app.state.events.events if e.datos.get("plantilla") != "cuenta-activada"]
    assert all(contrasena not in str(e.datos) for e in otros)


async def test_un_correo_o_un_documento_repetido_es_409_y_no_crea_cuenta(app, http):
    await _asistido(http)
    cuentas = app.state.clientes_asistidos._cuentas.cuentas

    por_correo = await _asistido(http, numeroDocumento="99999999")
    por_documento = await _asistido(http, email="otra@example.com")

    assert por_correo.status_code == 409
    assert por_correo.json()["errores"][0]["campo"] == "correo"
    assert por_documento.status_code == 409
    assert por_documento.json()["errores"][0]["campo"] == "documento"
    assert len(cuentas) == 1
    assert len(_correos(app)) == 1


async def test_un_correo_ya_tomado_en_identity_platform_es_409_de_correo(app, http):
    await app.state.clientes_asistidos._cuentas.crear_cuenta_cliente(
        "laura.gomez@example.com", "x", "Laura"
    )

    respuesta = await _asistido(http)

    assert respuesta.status_code == 409
    assert respuesta.json()["errores"][0]["campo"] == "correo"
    assert "ClienteRegistrado" not in _tipos(app)


async def test_si_el_registro_falla_se_borra_la_cuenta_creada(app, http):
    respuesta = await _asistido(http, email="alguien@mailinator.com")

    assert respuesta.status_code in (400, 422)
    cuentas = app.state.clientes_asistidos._cuentas
    assert cuentas.cuentas == {}
    assert len(cuentas.borradas) == 1
    assert _correos(app) == {}


async def test_el_reintento_con_la_misma_clave_no_crea_otra_cuenta_ni_otro_correo(app, http):
    cabeceras = {"Idempotency-Key": "alta-1"}

    primera = await http.post("/v1/clientes/asistidos", json=ASISTIDO, headers=cabeceras)
    repetida = await http.post("/v1/clientes/asistidos", json=ASISTIDO, headers=cabeceras)

    assert repetida.status_code == 201
    assert repetida.json() == primera.json()
    assert len(app.state.clientes_asistidos._cuentas.cuentas) == 1
    assert len(_correos(app)) == 1


async def test_un_cuerpo_incompleto_o_con_campos_de_mas_es_400(http):
    sin_telefono = {k: v for k, v in ASISTIDO.items() if k != "telefono"}

    assert (await http.post("/v1/clientes/asistidos", json=sin_telefono)).status_code == 400
    assert (await _asistido(http, canal="WEB")).status_code == 400
    assert (await _asistido(http, email="no-es-un-correo")).status_code == 400


async def test_sin_proveedor_de_identidad_responde_503(tmp_path):
    aplicacion = create_app(_settings(tmp_path, cuentas_internas_backend="off"))
    await maybe_init(aplicacion.state.clientes)
    async with AsyncClient(transport=ASGITransport(app=aplicacion), base_url="http://t") as h:
        respuesta = await h.post("/v1/clientes/asistidos", json=ASISTIDO)

    assert respuesta.status_code == 503
    assert "ClienteRegistrado" not in _tipos(aplicacion)


async def test_con_identity_platform_caido_no_se_crea_el_cliente(app, http):
    app.state.clientes_asistidos._cuentas.caida = True

    respuesta = await _asistido(http)

    assert respuesta.status_code == 503
    assert "ClienteRegistrado" not in _tipos(app)


# --- buscar clientes ---


async def test_busca_por_correo_sin_importar_las_mayusculas(http, openapi_spec):
    creado = (await _asistido(http)).json()

    respuesta = await http.get("/v1/clientes", params={"correo": "LAURA.GOMEZ@example.com"})

    assert respuesta.status_code == 200
    _validar(openapi_spec, "Cliente", respuesta.json())
    assert respuesta.json()["id"] == creado["id"]


async def test_busca_por_tipo_y_numero_de_documento(http):
    creado = (await _asistido(http)).json()

    respuesta = await http.get(
        "/v1/clientes", params={"tipoDocumento": "CC", "numeroDocumento": "52123456"}
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["id"] == creado["id"]


async def test_un_cliente_que_no_existe_es_404(http):
    por_correo = await http.get("/v1/clientes", params={"correo": "nadie@example.com"})
    por_documento = await http.get(
        "/v1/clientes", params={"tipoDocumento": "CC", "numeroDocumento": "00000000"}
    )

    assert por_correo.status_code == 404
    assert por_documento.status_code == 404


async def test_buscar_sin_dato_o_mezclando_formas_es_400(http):
    assert (await http.get("/v1/clientes")).status_code == 400
    assert (await http.get("/v1/clientes", params={"tipoDocumento": "CC"})).status_code == 400
    mezcla = await http.get("/v1/clientes", params={"correo": "a@b.co", "identityRef": "x"})
    assert mezcla.status_code == 400
    doble = await http.get(
        "/v1/clientes",
        params={"correo": "a@b.co", "tipoDocumento": "CC", "numeroDocumento": "1234"},
    )
    assert doble.status_code == 400


async def test_buscar_por_identity_ref_sigue_igual(http):
    creado = (await _asistido(http)).json()

    respuesta = await http.get("/v1/clientes", params={"identityRef": creado["identityRef"]})

    assert respuesta.status_code == 200
    assert respuesta.json()["id"] == creado["id"]


# --- precio ---


async def test_el_precio_usa_la_edad_del_cliente_y_el_credito_nuevo(app, http, openapi_spec):
    cliente_id = (await _asistido(http)).json()["id"]

    respuesta = await http.post("/v1/polizas/precio", json={"clienteId": cliente_id, **CREDITO})

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    _validar(openapi_spec, "PrecioPoliza", cuerpo)
    assert [c["codigo"] for c in cuerpo["coberturas"]] == ["VIDA", "HOGAR"]
    assert cuerpo["datosCredito"]["saldoInsoluto"] == cuerpo["datosCredito"]["valorCredito"]
    edad, datos = app.state.polizas._cotizaciones.precios[-1]
    assert 36 <= edad <= 38  # nació en 1988
    assert datos.saldo_insoluto == datos.valor_credito == 150000000


async def test_calcular_el_precio_no_crea_ni_toca_ninguna_solicitud(app, http):
    cliente_id = await _registrar(http)
    cotizaciones = app.state.polizas._cotizaciones
    activa = cotizaciones.poner(cliente_id, _activa())

    await http.post("/v1/polizas/precio", json={"clienteId": cliente_id, **CREDITO})

    assert (await http.get(f"/v1/clientes/{cliente_id}/polizas")).json() == []
    assert "CotizacionCreada" not in _tipos(app)
    assert await cotizaciones.activa(cliente_id) == activa


def _activa():
    from app.adapters.polizas_fake import cotizacion_de_ejemplo

    return cotizacion_de_ejemplo()


async def test_el_precio_de_un_cliente_inexistente_es_404(http):
    respuesta = await http.post("/v1/polizas/precio", json={"clienteId": "no-existe", **CREDITO})

    assert respuesta.status_code == 404


async def test_el_precio_fuera_de_la_edad_permitida_es_422(http):
    cliente_id = (await _asistido(http, fechaNacimiento="1950-01-01")).json()["id"]

    respuesta = await http.post("/v1/polizas/precio", json={"clienteId": cliente_id, **CREDITO})

    assert respuesta.status_code == 422
    assert respuesta.json()["codigo"] == "FUERA_DE_RANGO"


async def test_el_precio_de_un_credito_que_termina_despues_de_los_75_es_422(http):
    cliente_id = (await _asistido(http, fechaNacimiento="1962-01-01")).json()["id"]

    respuesta = await http.post(
        "/v1/polizas/precio",
        json={"clienteId": cliente_id, **{**CREDITO, "plazoRestanteMeses": 240}},
    )

    assert respuesta.status_code == 422


async def test_los_limites_del_credito_son_400_con_el_campo(http):
    cliente_id = (await _asistido(http)).json()["id"]

    corto = await http.post(
        "/v1/polizas/precio",
        json={"clienteId": cliente_id, **{**CREDITO, "valorCredito": 9_999_999}},
    )
    largo = await http.post(
        "/v1/polizas/precio",
        json={"clienteId": cliente_id, **{**CREDITO, "plazoRestanteMeses": 481}},
    )
    sin_entidad = await http.post(
        "/v1/polizas/precio",
        json={"clienteId": cliente_id, **{**CREDITO, "entidadAcreedora": " x "}},
    )

    assert corto.status_code == 400
    assert corto.json()["errores"][0]["campo"] == "valorCredito"
    assert largo.status_code == 400
    assert sin_entidad.status_code == 400
    assert sin_entidad.json()["errores"][0]["campo"] == "entidadAcreedora"


async def test_con_cotizacion_caida_el_precio_es_503(app, http):
    cliente_id = (await _asistido(http)).json()["id"]
    app.state.polizas._cotizaciones.caida = True

    respuesta = await http.post("/v1/polizas/precio", json={"clienteId": cliente_id, **CREDITO})

    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "COTIZACION_NO_DISPONIBLE"


# --- venta ---


async def _vender(http, cliente_id, **cambios):
    return await http.post("/v1/polizas", json={"clienteId": cliente_id, **VENTA, **cambios})


async def test_la_venta_emite_al_instante_a_nombre_del_cliente(app, http, openapi_spec):
    cliente_id = (await _asistido(http)).json()["id"]

    respuesta = await _vender(http, cliente_id)

    assert respuesta.status_code == 201
    cuerpo = respuesta.json()
    _validar(openapi_spec, "PolizaInterna", cuerpo)
    assert respuesta.headers["Location"] == f"/v1/polizas/{cuerpo['polizaId']}"
    assert cuerpo["estado"] == "EMITIDA"
    assert cuerpo["numero"].startswith("SOL-VH-")
    assert cuerpo["clienteId"] == cliente_id
    assert cuerpo["socioId"] == "socio-uid-1"
    assert cuerpo["datosVenta"] == {
        "direccion": "Calle 10 # 5-20 Apto 301",
        "municipio": "Bogotá",
        "cuotaMensual": 1250000,
    }
    assert cuerpo["pendientes"] == []
    assert [c["codigo"] for c in cuerpo["coberturas"]] == ["VIDA", "HOGAR"]
    assert cuerpo["datosCredito"]["saldoInsoluto"] == cuerpo["datosCredito"]["valorCredito"]
    assert [d["tipo"] for d in cuerpo["documentosAceptados"]] == ["poliza"]
    assert cuerpo["documentosAceptados"][0]["version"] == "V1"


async def test_la_venta_publica_la_poliza_emitida_y_avisa_al_cliente(app, http):
    cliente_id = (await _asistido(http)).json()["id"]

    cuerpo = (await _vender(http, cliente_id)).json()

    emitida = [e for e in app.state.events.events if e.tipo == "PolizaEmitida"]
    assert len(emitida) == 1
    assert emitida[0].datos["polizaId"] == cuerpo["polizaId"]
    assert emitida[0].datos["asegurado"]["email"] == "laura.gomez@example.com"
    assert _correos(app)["bienvenida-venta"]["destinatario"] == "laura.gomez@example.com"


async def test_la_venta_acepta_las_condiciones_en_la_version_vigente(app, http):
    cliente_id = (await _asistido(http)).json()["id"]
    app.state.polizas._documentos_legales.version = "V3"

    cuerpo = (await _vender(http, cliente_id)).json()

    assert cuerpo["documentosAceptados"][0]["version"] == "V3"


async def test_vender_solo_vida_deja_solo_vida(http):
    cliente_id = (await _asistido(http)).json()["id"]

    cuerpo = (await _vender(http, cliente_id, coberturas=["VIDA"])).json()

    assert [c["codigo"] for c in cuerpo["coberturas"]] == ["VIDA"]


async def test_la_venta_no_consume_las_evidencias_del_cliente(app, http):
    cliente_id = await _registrar(http)
    for tipo in ("cedula_frente", "cedula_reverso", "selfie"):
        subida = await http.post(
            f"/v1/clientes/{cliente_id}/evidencias-identidad",
            data={"tipo": tipo},
            files={"imagen": ("f.jpg", imagenes.a_bytes(imagenes.sin_tarjeta()), "image/jpeg")},
        )
        assert subida.status_code == 201

    cuerpo = (await _vender(http, cliente_id)).json()

    assert cuerpo["estado"] == "EMITIDA"
    assert cuerpo["evidencias"] == []
    repositorio = app.state.polizas._evidencias
    for tipo in TipoEvidencia:
        vigente = await repositorio.vigente(cliente_id, tipo)
        assert vigente is not None and vigente.poliza_ref is None


async def test_la_venta_emite_aunque_el_cliente_no_haya_confirmado_el_correo(http):
    cliente_id = await _registrar(http)  # registro normal: correo sin confirmar, sin evidencias

    respuesta = await _vender(http, cliente_id)

    assert respuesta.status_code == 201
    assert respuesta.json()["estado"] == "EMITIDA"


async def test_la_venta_no_borra_la_solicitud_pendiente_del_cliente(app, http):
    cliente_id = await _registrar(http)
    cotizacion = app.state.polizas._cotizaciones.poner(cliente_id, _activa())
    solicitud = await http.post(
        f"/v1/clientes/{cliente_id}/polizas",
        json={
            "coberturas": ["VIDA"],
            "cotizacionCreadaEn": cotizacion.creada_en.isoformat(),
            "documentosAceptados": [{"tipo": "poliza", "version": "V1"}],
        },
    )
    assert solicitud.json()["estado"] == "PENDIENTE"

    await _vender(http, cliente_id)

    listadas = (await http.get(f"/v1/clientes/{cliente_id}/polizas")).json()
    assert sorted(p["estado"] for p in listadas) == ["EMITIDA", "PENDIENTE"]


async def test_el_cliente_ve_la_poliza_vendida_sin_los_datos_internos(http, openapi_spec):
    cliente_id = (await _asistido(http)).json()["id"]
    poliza_id = (await _vender(http, cliente_id)).json()["polizaId"]

    suya = await http.get(f"/v1/clientes/{cliente_id}/polizas/{poliza_id}")
    interna = await http.get(f"/v1/polizas/{poliza_id}")

    assert suya.status_code == 200
    _validar(openapi_spec, "Poliza", suya.json())
    assert "socioId" not in suya.json() and "datosVenta" not in suya.json()
    assert interna.json()["socioId"] == "socio-uid-1"
    _validar(openapi_spec, "PolizaInterna", interna.json())
    # Las pólizas que no son de venta directa llevan los dos campos en null.
    otra = await _registrar(
        http, identityRef="otra", numeroDocumento="777777", email="o@example.com"
    )
    assert (await http.get("/v1/polizas", params={"clienteId": otra})).json()["items"] == []


async def test_el_reintento_con_la_misma_clave_devuelve_la_misma_venta(app, http):
    cliente_id = (await _asistido(http)).json()["id"]
    cabeceras = {"Idempotency-Key": "venta-1"}
    cuerpo = {"clienteId": cliente_id, **VENTA}

    primera = await http.post("/v1/polizas", json=cuerpo, headers=cabeceras)
    repetida = await http.post("/v1/polizas", json=cuerpo, headers=cabeceras)

    assert repetida.status_code == 201
    assert repetida.json() == primera.json()
    assert len((await http.get(f"/v1/clientes/{cliente_id}/polizas")).json()) == 1
    assert _tipos(app).count("PolizaEmitida") == 1
    assert [
        e.datos["plantilla"] for e in app.state.events.events if e.tipo == "EnviarCorreo"
    ].count("bienvenida-venta") == 1


async def test_la_misma_clave_de_otro_socio_no_devuelve_la_venta_ajena(http):
    cliente_id = (await _asistido(http)).json()["id"]
    cabeceras = {"Idempotency-Key": "compartida"}

    a = await http.post("/v1/polizas", json={"clienteId": cliente_id, **VENTA}, headers=cabeceras)
    b = await http.post(
        "/v1/polizas",
        json={"clienteId": cliente_id, **VENTA, "socioId": "socio-uid-2"},
        headers=cabeceras,
    )

    assert a.json()["polizaId"] != b.json()["polizaId"]
    assert b.json()["socioId"] == "socio-uid-2"


async def test_una_venta_sin_vida_es_400(http):
    cliente_id = (await _asistido(http)).json()["id"]

    respuesta = await _vender(http, cliente_id, coberturas=["HOGAR"])

    assert respuesta.status_code == 400
    assert respuesta.json()["errores"][0]["campo"] == "coberturas"


async def test_los_datos_de_la_venta_se_validan(http):
    cliente_id = (await _asistido(http)).json()["id"]

    en_blanco = await _vender(http, cliente_id, direccion="   ")
    sin_municipio = await _vender(http, cliente_id, municipio="")
    cuota_cero = await _vender(http, cliente_id, cuotaMensual=0)
    repetidas = await _vender(http, cliente_id, coberturas=["VIDA", "VIDA"])
    extra = await _vender(http, cliente_id, saldoInsoluto=1)

    assert en_blanco.status_code == 400
    assert en_blanco.json()["errores"][0]["campo"] == "direccion"
    assert sin_municipio.status_code == 400
    assert cuota_cero.status_code == 400
    assert repetidas.status_code == 400
    assert extra.status_code == 400


async def test_vender_a_un_cliente_inexistente_es_404(http):
    assert (await _vender(http, "no-existe")).status_code == 404


async def test_vender_a_un_cliente_bloqueado_es_403(app, http, tmp_path):
    cliente_id = (await _asistido(http)).json()["id"]
    async with aiosqlite.connect(app.state.settings.database_path) as conn:
        await conn.execute("UPDATE clientes SET estado = 'BLOQUEADO'")
        await conn.commit()

    respuesta = await _vender(http, cliente_id)

    assert respuesta.status_code == 403
    assert respuesta.json()["codigo"] == "CLIENTE_NO_ACTIVO"


async def test_vender_fuera_del_rango_de_edad_es_422_y_no_emite(app, http):
    cliente_id = (await _asistido(http, fechaNacimiento="1950-01-01")).json()["id"]

    respuesta = await _vender(http, cliente_id)

    assert respuesta.status_code == 422
    assert "PolizaEmitida" not in _tipos(app)
    assert (await http.get(f"/v1/clientes/{cliente_id}/polizas")).json() == []


async def test_sin_cotizacion_o_sin_condiciones_no_se_emite_nada(app, http):
    cliente_id = (await _asistido(http)).json()["id"]

    app.state.polizas._cotizaciones.caida = True
    sin_cotizacion = await _vender(http, cliente_id)
    app.state.polizas._cotizaciones.caida = False
    app.state.polizas._documentos_legales.caido = True
    sin_condiciones = await _vender(http, cliente_id)

    assert sin_cotizacion.status_code == 503
    assert sin_condiciones.status_code == 503
    assert sin_condiciones.json()["codigo"] == "DOCUMENTOS_LEGALES_NO_DISPONIBLES"
    assert (await http.get(f"/v1/clientes/{cliente_id}/polizas")).json() == []
    assert "PolizaEmitida" not in _tipos(app)
