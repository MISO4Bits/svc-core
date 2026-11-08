"""BITS-378/379: adaptadores y piezas internas de la venta directa del socio."""

from __future__ import annotations

import json
import string
from datetime import UTC, date, datetime
from decimal import Decimal

import aiosqlite
import httpx
import pytest

from app.adapters import factory
from app.adapters.cotizacion_http import CotizacionHttp
from app.adapters.cuentas_fake import CuentasClienteEnMemoria
from app.adapters.documentos_legales_http import DocumentosLegalesHttp
from app.adapters.identity_admin import IdentityPlatformAdmin
from app.adapters.memory import (
    InMemoryClienteRepository,
    InMemoryEvidenciaRepository,
    InMemoryPolizaRepository,
)
from app.adapters.polizas_fake import CotizacionFake, DocumentosLegalesFake
from app.adapters.sqlite import SqliteClienteRepository, SqliteDatabase, SqlitePolizaRepository
from app.clientes_asistidos import generar_contrasena
from app.config import Settings
from app.domain import (
    Cliente,
    CoberturaContratada,
    CodigoCobertura,
    CotizacionNoDisponible,
    CuentaClienteYaExiste,
    DatosCredito,
    DatosVenta,
    DocumentosLegalesNoDisponibles,
    IdentidadNoDisponible,
    OrigenDatos,
    Poliza,
    TipoDocumento,
    Vigencia,
)
from tests.unit.test_polizas_adapters import RESPUESTA

CREDITO = DatosCredito(Decimal("150000000"), 240, "Bancolombia", Decimal("150000000"))


# --- contraseña temporal ---


def test_la_contrasena_tiene_las_cuatro_clases_y_ningun_caracter_ambiguo():
    for _ in range(200):
        c = generar_contrasena()
        assert len(c) == 14
        assert any(ch in string.ascii_lowercase for ch in c)
        assert any(ch in string.ascii_uppercase for ch in c)
        assert any(ch in string.digits for ch in c)
        assert any(ch in "#$%*+-=?@" for ch in c)
        assert not set(c) & set("01lIO")


def test_dos_contrasenas_no_se_repiten():
    assert len({generar_contrasena() for _ in range(100)}) == 100


# --- Cotización HTTP: precio ---


def _cotizacion(manejador) -> CotizacionHttp:
    http = httpx.AsyncClient(
        base_url="http://cotizacion", transport=httpx.MockTransport(manejador), timeout=1
    )
    return CotizacionHttp("http://cotizacion", client=http)


async def test_precio_llama_a_la_ruta_sin_estado_con_la_edad_y_el_credito():
    pedidos = []

    def manejador(pedido: httpx.Request) -> httpx.Response:
        pedidos.append(pedido)
        return httpx.Response(200, json=RESPUESTA)

    cotizacion = await _cotizacion(manejador).precio(38, CREDITO)

    assert pedidos[0].method == "POST"
    assert pedidos[0].url.path == "/v1/cotizaciones/precio"
    assert json.loads(pedidos[0].content) == {
        "edad": 38,
        "datosCredito": {
            "valorCredito": 150000000.0,
            "plazoRestanteMeses": 240,
            "entidadAcreedora": "Bancolombia",
            "saldoInsoluto": 150000000.0,
        },
    }
    assert cotizacion.origen_datos is OrigenDatos.CONFIRMADO
    assert [c.codigo for c in cotizacion.coberturas] == [
        CodigoCobertura.VIDA,
        CodigoCobertura.HOGAR,
    ]


async def test_precio_reintenta_un_5xx_y_luego_se_rinde():
    llamadas = []

    def manejador(pedido: httpx.Request) -> httpx.Response:
        llamadas.append(1)
        return httpx.Response(503)

    with pytest.raises(CotizacionNoDisponible):
        await _cotizacion(manejador).precio(38, CREDITO)
    assert len(llamadas) == 2


async def test_precio_se_recupera_si_el_reintento_responde():
    respuestas = iter([httpx.Response(502), httpx.Response(200, json=RESPUESTA)])

    cotizacion = await _cotizacion(lambda _: next(respuestas)).precio(38, CREDITO)

    assert cotizacion.edad == 38


async def test_precio_con_un_4xx_no_reintenta():
    llamadas = []

    def manejador(pedido: httpx.Request) -> httpx.Response:
        llamadas.append(1)
        return httpx.Response(400, json={"title": "Solicitud inválida"})

    with pytest.raises(CotizacionNoDisponible):
        await _cotizacion(manejador).precio(38, CREDITO)
    assert len(llamadas) == 1


async def test_precio_sin_red_es_cotizacion_no_disponible():
    def manejador(pedido: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sin red")

    with pytest.raises(CotizacionNoDisponible):
        await _cotizacion(manejador).precio(38, CREDITO)


async def test_el_fake_de_cotizacion_arma_el_precio_con_el_credito_pedido():
    fake = CotizacionFake()

    cotizacion = await fake.precio(40, CREDITO)

    assert cotizacion.edad == 40
    assert cotizacion.datos_credito == CREDITO
    assert fake.precios == [(40, CREDITO)]
    fake.caida = True
    with pytest.raises(CotizacionNoDisponible):
        await fake.precio(40, CREDITO)


# --- documentos legales HTTP ---


def _legales(manejador) -> DocumentosLegalesHttp:
    http = httpx.AsyncClient(
        base_url="http://productos", transport=httpx.MockTransport(manejador), timeout=1
    )
    return DocumentosLegalesHttp("http://productos", "CO", "es-CO", client=http)


async def test_la_version_vigente_sale_del_documento_del_tipo_pedido():
    pedidos = []

    def manejador(pedido: httpx.Request) -> httpx.Response:
        pedidos.append(pedido)
        return httpx.Response(
            200, json=[{"tipo": "terminos", "version": "V9"}, {"tipo": "poliza", "version": "V4"}]
        )

    version = await _legales(manejador).version_vigente("poliza")

    assert version == "V4"
    assert dict(pedidos[0].url.params) == {"mercado": "CO", "idioma": "es-CO", "tipo": "poliza"}


@pytest.mark.parametrize(
    "respuesta",
    [
        httpx.Response(200, json=[]),
        httpx.Response(200, json=[{"tipo": "terminos", "version": "V1"}]),
        httpx.Response(200, json=[{"tipo": "poliza"}]),
        httpx.Response(200, json={"tipo": "poliza", "version": "V1"}),
        httpx.Response(400, json={}),
        httpx.Response(500),
    ],
)
async def test_sin_un_documento_publicado_no_hay_version(respuesta):
    with pytest.raises(DocumentosLegalesNoDisponibles):
        await _legales(lambda _: respuesta).version_vigente("poliza")


async def test_productos_sin_red_no_hay_version():
    def manejador(pedido: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sin red")

    with pytest.raises(DocumentosLegalesNoDisponibles):
        await _legales(manejador).version_vigente("poliza")


async def test_productos_se_recupera_tras_un_5xx():
    respuestas = iter(
        [httpx.Response(503), httpx.Response(200, json=[{"tipo": "poliza", "version": "V2"}])]
    )

    assert await _legales(lambda _: next(respuestas)).version_vigente("poliza") == "V2"


async def test_el_fake_de_documentos_legales():
    fake = DocumentosLegalesFake("V7")
    assert await fake.version_vigente("poliza") == "V7"
    fake.caido = True
    with pytest.raises(DocumentosLegalesNoDisponibles):
        await fake.version_vigente("poliza")


# --- Identity Platform: cuentas de cliente ---


async def _token() -> str:
    return "token"


def _admin(manejador) -> IdentityPlatformAdmin:
    return IdentityPlatformAdmin(
        "solventa-dev", _token, client=httpx.AsyncClient(transport=httpx.MockTransport(manejador))
    )


async def test_crea_la_cuenta_verificada_y_sin_rol():
    pedidos: list[httpx.Request] = []

    def manejador(pedido):
        pedidos.append(pedido)
        return httpx.Response(200, json={"localId": "uid-9"})

    uid = await _admin(manejador).crear_cuenta_cliente("a@b.co", "Clave-123", "Ana Ríos")

    assert uid == "uid-9"
    assert len(pedidos) == 1  # sin segundo paso: no hay rol que fijar
    assert pedidos[0].url.path.endswith("/accounts")
    cuerpo = json.loads(pedidos[0].content)
    assert cuerpo == {
        "email": "a@b.co",
        "password": "Clave-123",
        "emailVerified": True,
        "displayName": "Ana Ríos",
    }
    assert "customAttributes" not in cuerpo


async def test_un_correo_tomado_es_cuenta_cliente_ya_existe():
    def manejador(pedido):
        return httpx.Response(400, json={"error": {"message": "EMAIL_EXISTS"}})

    with pytest.raises(CuentaClienteYaExiste):
        await _admin(manejador).crear_cuenta_cliente("a@b.co", "x", "Ana")


async def test_otro_fallo_o_sin_red_es_identidad_no_disponible():
    def rechazo(pedido):
        return httpx.Response(500, json={"error": {"message": "INTERNAL"}})

    def sin_red(pedido):
        raise httpx.ConnectError("sin red")

    with pytest.raises(IdentidadNoDisponible):
        await _admin(rechazo).crear_cuenta_cliente("a@b.co", "x", "Ana")
    with pytest.raises(IdentidadNoDisponible):
        await _admin(sin_red).crear_cuenta_cliente("a@b.co", "x", "Ana")


async def test_borrar_la_cuenta_nunca_lanza():
    pedidos: list[httpx.Request] = []

    def manejador(pedido):
        pedidos.append(pedido)
        return httpx.Response(500)

    await _admin(manejador).borrar_cuenta("uid-9")

    assert pedidos[0].url.path.endswith("/accounts:delete")
    assert json.loads(pedidos[0].content) == {"localId": "uid-9"}


async def test_la_cuenta_en_memoria():
    cuentas = CuentasClienteEnMemoria()
    uid = await cuentas.crear_cuenta_cliente("a@b.co", "x", "Ana")
    with pytest.raises(CuentaClienteYaExiste):
        await cuentas.crear_cuenta_cliente("a@b.co", "y", "Ana")
    await cuentas.borrar_cuenta(uid)
    assert cuentas.cuentas == {} and cuentas.borradas == [uid]


# --- búsqueda de clientes ---


def _cliente(**cambios) -> Cliente:
    datos = {
        "identity_ref": "ref-1",
        "tipo_documento": TipoDocumento.CC,
        "numero_documento": "1032456789",
        "primer_nombre": "Ana",
        "primer_apellido": "Ríos",
        "fecha_nacimiento": date(1991, 5, 20),
        "email": "Ana.Rios@Example.com",
        "telefono": "+573001234567",
    }
    return Cliente(**{**datos, **cambios})


@pytest.fixture(params=["memoria", "sqlite"])
async def clientes(request, tmp_path):
    if request.param == "memoria":
        return InMemoryClienteRepository()
    db = SqliteDatabase(str(tmp_path / "c.db"))
    await db.init()
    return SqliteClienteRepository(db)


async def test_busca_al_cliente_por_correo_y_por_documento(clientes):
    cliente = _cliente()
    await clientes.crear_con_consentimientos(cliente, [])

    assert (await clientes.buscar_por_correo("ana.rios@example.com")).id == cliente.id
    assert (await clientes.buscar_por_documento("CC", "1032456789")).id == cliente.id
    assert await clientes.buscar_por_correo("otra@example.com") is None
    assert await clientes.buscar_por_documento("CE", "1032456789") is None
    assert await clientes.buscar_por_documento("CC", "1") is None


# --- pólizas: columnas de la venta y emisión sin consumir evidencias ---


def _poliza(**cambios) -> Poliza:
    ahora = datetime(2026, 11, 1, 12, tzinfo=UTC)
    datos = {
        "cliente_id": "cli-1",
        "cotizacion_creada_en": ahora,
        "solicitud_vigente_hasta": ahora.replace(month=12),
        "coberturas": (
            CoberturaContratada(
                CodigoCobertura.VIDA,
                True,
                Decimal("1"),
                Decimal("1"),
                Decimal("0"),
                Decimal("1"),
            ),
        ),
        "datos_credito": CREDITO,
        "origen_datos": OrigenDatos.CONFIRMADO,
        "tasa": Decimal("0.2"),
        "version_tarifa": 1,
        "cobertura_meses": 240,
        "edad": 38,
        "documentos_aceptados": (),
        "socio_id": "socio-1",
        "datos_venta": DatosVenta("Calle 1", "Cali", Decimal("1250000.50")),
    }
    return Poliza(**{**datos, **cambios})


@pytest.fixture(params=["memoria", "sqlite"])
async def polizas(request, tmp_path):
    if request.param == "memoria":
        return InMemoryPolizaRepository(InMemoryEvidenciaRepository())
    db = SqliteDatabase(str(tmp_path / "p.db"))
    await db.init()
    return SqlitePolizaRepository(db)


async def test_la_poliza_guarda_el_socio_y_los_datos_de_la_venta(polizas):
    poliza = _poliza()
    await polizas.crear(poliza)

    leida = await polizas.obtener(poliza.id)

    assert leida.socio_id == "socio-1"
    assert leida.datos_venta == DatosVenta("Calle 1", "Cali", Decimal("1250000.50"))


async def test_una_poliza_normal_no_tiene_socio_ni_datos_de_venta(polizas):
    poliza = _poliza(socio_id=None, datos_venta=None)
    await polizas.crear(poliza)

    leida = await polizas.obtener(poliza.id)

    assert leida.socio_id is None and leida.datos_venta is None


async def test_emitir_sin_consumir_deja_las_evidencias_del_cliente(tmp_path):
    from app.domain import EstadoEvidencia, Evidencia, TipoEvidencia

    db = SqliteDatabase(str(tmp_path / "e.db"))
    await db.init()
    from app.adapters.sqlite import SqliteEvidenciaRepository

    evidencias = SqliteEvidenciaRepository(db)
    repo = SqlitePolizaRepository(db)
    await evidencias.guardar_reemplazando(
        Evidencia("cli-1", TipoEvidencia.SELFIE, EstadoEvidencia.ACEPTADA, "ref")
    )
    sin_consumir, consumiendo = (
        _poliza(),
        _poliza(cotizacion_creada_en=datetime(2026, 11, 2, tzinfo=UTC)),
    )
    await repo.crear(sin_consumir)
    await repo.crear(consumiendo)
    ahora = datetime(2026, 11, 1, tzinfo=UTC)
    vigencia = Vigencia(ahora, ahora.replace(year=2046))

    emitida = await repo.emitir(
        sin_consumir.id, "SOL-VH-A", ahora, vigencia, consumir_evidencias=False
    )

    assert emitida.estado.value == "EMITIDA" and emitida.evidencias == ()
    assert (await evidencias.vigente("cli-1", TipoEvidencia.SELFIE)).poliza_ref is None
    await repo.emitir(consumiendo.id, "SOL-VH-B", ahora, vigencia)
    assert await evidencias.vigente("cli-1", TipoEvidencia.SELFIE) is None


async def test_una_base_anterior_a_la_venta_directa_gana_las_columnas(tmp_path):
    ruta = str(tmp_path / "vieja.db")
    db = SqliteDatabase(ruta)
    await db.init()
    async with aiosqlite.connect(ruta) as conn:
        await conn.execute("ALTER TABLE polizas DROP COLUMN socio_id")
        await conn.execute("ALTER TABLE polizas DROP COLUMN datos_venta")
        await conn.commit()

    await db.init()  # la migración vuelve a agregarlas

    repo = SqlitePolizaRepository(db)
    poliza = _poliza()
    await repo.crear(poliza)
    assert (await repo.obtener(poliza.id)).socio_id == "socio-1"


# --- fábrica ---


async def test_las_condiciones_de_la_poliza_siguen_al_backend_configurado():
    fake, cerrables = factory.build_documentos_legales(Settings())
    assert isinstance(fake, DocumentosLegalesFake) and cerrables == []

    real, cerrables = factory.build_documentos_legales(
        Settings(documentos_legales_backend="http", productos_base_url="http://productos")
    )
    assert isinstance(real, DocumentosLegalesHttp) and cerrables == [real]
    await real.aclose()

    with pytest.raises(ValueError, match="documentos_legales_backend"):
        factory.build_documentos_legales(Settings(documentos_legales_backend="paloma"))


async def test_las_cuentas_de_cliente_siguen_al_backend_de_cuentas_internas(monkeypatch):
    assert factory.build_cuentas_cliente(Settings()) == (None, [])
    cuentas, cerrables = factory.build_cuentas_cliente(Settings(cuentas_internas_backend="fake"))
    assert isinstance(cuentas, CuentasClienteEnMemoria) and cerrables == []

    with pytest.raises(ValueError, match="CORE_IDENTITY_PROJECT_ID"):
        factory.build_cuentas_cliente(Settings(cuentas_internas_backend="real"))

    monkeypatch.setattr("app.adapters.identity_admin.token_de_servicio", lambda: _token)
    real, cerrables = factory.build_cuentas_cliente(
        Settings(cuentas_internas_backend="real", identity_project_id="solventa-dev")
    )
    assert isinstance(real, IdentityPlatformAdmin) and cerrables == [real]
    await real.aclose()

    with pytest.raises(ValueError, match="cuentas_internas_backend"):
        factory.build_cuentas_cliente(Settings(cuentas_internas_backend="paloma"))


# --- reglas del servicio (las que la API ya filtra con su esquema) ---


def _servicio(escenario, *, legales="V1"):
    from app.adapters.polizas_fake import DocumentoPolizaFake
    from app.polizas import PolizaService

    return PolizaService(
        escenario.clientes,
        escenario.evidencias,
        escenario.polizas,
        escenario.cotizaciones,
        DocumentoPolizaFake(),
        escenario.eventos,
        documentos_legales=DocumentosLegalesFake(legales) if legales else None,
        ahora=escenario.reloj,
    )


def _venta(cliente_id, **cambios):
    datos = {
        "socio_id": "socio-1",
        "cliente_id": cliente_id,
        "coberturas": [CodigoCobertura.VIDA],
        "datos": CREDITO,
        "cuota_mensual": Decimal("1000000"),
        "direccion": "Calle 1",
        "municipio": "Cali",
    }
    return {**datos, **cambios}


@pytest.mark.parametrize(
    ("cambios", "campo"),
    [
        ({"socio_id": "  "}, "socioId"),
        ({"direccion": " "}, "direccion"),
        ({"direccion": "x" * 201}, "direccion"),
        ({"municipio": ""}, "municipio"),
        ({"municipio": "x" * 201}, "municipio"),
        ({"cuota_mensual": Decimal(0)}, "cuotaMensual"),
        ({"coberturas": [CodigoCobertura.HOGAR]}, "coberturas"),
    ],
)
async def test_vender_valida_sus_datos_antes_de_llamar_a_nadie(cambios, campo):
    from app.domain import SolicitudPolizaInvalida
    from tests.unit.test_polizas_service import Escenario

    escenario = Escenario()
    cliente = await escenario.cliente()

    with pytest.raises(SolicitudPolizaInvalida) as error:
        await _servicio(escenario).vender(**_venta(cliente.id, **cambios))

    assert error.value.campo == campo
    assert escenario.cotizaciones.precios == []


@pytest.mark.parametrize(
    ("cambios", "campo"),
    [
        (
            {"valor_credito": Decimal(9_999_999), "saldo_insoluto": Decimal(9_999_999)},
            "valorCredito",
        ),
        ({"plazo_restante_meses": 0}, "plazoRestanteMeses"),
        ({"plazo_restante_meses": 481}, "plazoRestanteMeses"),
        ({"entidad_acreedora": " a "}, "entidadAcreedora"),
        ({"entidad_acreedora": "x" * 81}, "entidadAcreedora"),
    ],
)
def test_validar_datos_credito(cambios, campo):
    from dataclasses import replace

    from app.domain import SolicitudPolizaInvalida
    from app.polizas import validar_datos_credito

    with pytest.raises(SolicitudPolizaInvalida) as error:
        validar_datos_credito(replace(CREDITO, **cambios))
    assert error.value.campo == campo


async def test_vender_sin_servicio_de_condiciones_no_emite():
    from tests.unit.test_polizas_service import Escenario

    escenario = Escenario()
    cliente = await escenario.cliente()

    with pytest.raises(DocumentosLegalesNoDisponibles):
        await _servicio(escenario, legales=None).vender(**_venta(cliente.id))

    assert await escenario.polizas.listar_de_cliente(cliente.id) == []


async def test_vender_a_un_cliente_bloqueado_o_inexistente():
    from app.domain import ClienteNoActivo, ClienteNoEncontrado, EstadoCliente
    from tests.unit.test_polizas_service import Escenario

    escenario = Escenario()
    bloqueado = await escenario.cliente(estado=EstadoCliente.BLOQUEADO)

    with pytest.raises(ClienteNoActivo):
        await _servicio(escenario).vender(**_venta(bloqueado.id))
    with pytest.raises(ClienteNoEncontrado):
        await _servicio(escenario).vender(**_venta("no-existe"))


async def test_un_fallo_inesperado_de_cotizacion_es_cotizacion_no_disponible():
    from tests.unit.test_polizas_service import Escenario

    escenario = Escenario()
    cliente = await escenario.cliente()

    async def roto(edad, datos):
        raise RuntimeError("explotó")

    escenario.cotizaciones.precio = roto

    with pytest.raises(CotizacionNoDisponible):
        await _servicio(escenario).precio(cliente.id, CREDITO)


async def test_si_la_cotizacion_no_trae_hogar_pedirlo_es_400():
    from dataclasses import replace

    from app.domain import SolicitudPolizaInvalida
    from tests.unit.test_polizas_service import Escenario

    escenario = Escenario()
    cliente = await escenario.cliente()
    original = escenario.cotizaciones.precio

    async def solo_vida(edad, datos):
        cot = await original(edad, datos)
        return replace(cot, coberturas=cot.coberturas[:1])

    escenario.cotizaciones.precio = solo_vida

    with pytest.raises(SolicitudPolizaInvalida) as error:
        await _servicio(escenario).vender(
            **_venta(cliente.id, coberturas=[CodigoCobertura.VIDA, CodigoCobertura.HOGAR])
        )
    assert error.value.campo == "coberturas"


async def test_dos_ventas_del_mismo_precio_a_la_vez_dejan_una_sola():
    from app.domain import PolizaYaExiste, SolicitudExistente
    from tests.unit.test_polizas_service import Escenario

    escenario = Escenario()
    cliente = await escenario.cliente()

    async def ocupada(poliza):
        raise PolizaYaExiste(poliza.cliente_id)

    escenario.polizas.crear = ocupada

    with pytest.raises(SolicitudExistente):
        await _servicio(escenario).vender(**_venta(cliente.id))


# --- correos de la venta ---


async def test_un_error_inesperado_al_preparar_el_correo_no_rompe_el_alta(caplog):
    from app.adapters.memory import InMemoryEventPublisher
    from tests.unit.test_notificaciones import _cliente as cliente_de_prueba
    from tests.unit.test_notificaciones import _servicio as notificaciones

    class Rota:
        async def obtener_vigente(self, tipo, mercado, idioma):
            raise RuntimeError("clave-secreta-en-el-detalle")

    servicio = notificaciones(InMemoryEventPublisher(), plantillas=Rota())

    await servicio.enviar_correos_de_alta(cliente_de_prueba(), "clave-secreta-en-el-detalle")

    assert "clave-secreta-en-el-detalle" not in caplog.text
    assert "RuntimeError" in caplog.text
