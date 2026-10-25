"""BITS-101: documento real de la póliza (fn-documentos), correo con adjunto y su idempotencia."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from app.adapters.documento_http import RUTA, DocumentoPolizaHttp
from app.adapters.factory import build_polizas
from app.adapters.memory import InMemoryEvidenciaRepository, InMemoryPolizaRepository
from app.adapters.notificaciones_fake import FakePlantillasCorreo
from app.config import Settings
from app.consumidores import despachar_evento
from app.domain import (
    DocumentoNoDisponible,
    DomainEvent,
    EstadoDocumentoPoliza,
    NotificacionError,
    ReferenciaDocumento,
)
from app.notificaciones import NotificacionService
from app.polizas import datos_para_documento
from tests.unit.test_polizas_service import Escenario, _listo

AVISO = {
    "polizaId": "",
    "bucket": "solventa-dev-polizas",
    "objeto": "polizas/x.pdf",
    "nombreArchivo": "Poliza-SOL-VH-2026-K7M2Q9.pdf",
    "contentType": "application/pdf",
}


# --- cliente HTTP de fn-documentos ---


def _http(manejador, tokens=None) -> DocumentoPolizaHttp:
    pedidos_de_token: list[str] = []

    async def token(audiencia: str) -> str | None:
        pedidos_de_token.append(audiencia)
        return next(tokens) if tokens else "token-1"

    cliente = DocumentoPolizaHttp(
        "https://fn-documentos.run.app/",
        client=httpx.AsyncClient(
            base_url="https://fn-documentos.run.app", transport=httpx.MockTransport(manejador)
        ),
        proveedor_de_token=token,
    )
    cliente.pedidos_de_token = pedidos_de_token
    return cliente


RESPUESTA = {
    "estado": "LISTO",
    "url": "https://storage.googleapis.com/b/polizas/x.pdf?firma=1",
    "expiraEn": "2026-10-24T12:15:00+00:00",
}


async def test_pide_el_enlace_con_los_datos_de_la_poliza_y_un_token_de_identidad():
    pedidos = []

    def manejador(pedido: httpx.Request) -> httpx.Response:
        pedidos.append(pedido)
        return httpx.Response(200, json=RESPUESTA)

    cliente = _http(manejador)

    documento = await cliente.consultar({"polizaId": "p1", "numero": "SOL-1"})

    assert documento.estado is EstadoDocumentoPoliza.LISTO
    assert documento.url == RESPUESTA["url"]
    assert documento.expira_en == datetime(2026, 10, 24, 12, 15, tzinfo=UTC)
    assert str(pedidos[0].url) == f"https://fn-documentos.run.app{RUTA}"
    assert pedidos[0].headers["Authorization"] == "Bearer token-1"
    assert cliente.pedidos_de_token == ["https://fn-documentos.run.app"]  # audiencia sin "/"


async def test_el_token_se_reutiliza_entre_llamadas():
    cliente = _http(lambda _p: httpx.Response(200, json=RESPUESTA))

    await cliente.consultar({})
    await cliente.consultar({})

    assert len(cliente.pedidos_de_token) == 1


async def test_un_401_descarta_el_token_y_la_siguiente_llamada_pide_otro():
    respuestas = iter([httpx.Response(401), httpx.Response(200, json=RESPUESTA)])
    cliente = _http(lambda _p: next(respuestas), tokens=iter(["viejo", "nuevo"]))

    with pytest.raises(DocumentoNoDisponible):
        await cliente.consultar({})
    await cliente.consultar({})

    assert len(cliente.pedidos_de_token) == 2


async def test_sin_credenciales_la_llamada_sale_sin_encabezado():
    pedidos = []

    def manejador(pedido):
        pedidos.append(pedido)
        return httpx.Response(200, json=RESPUESTA)

    async def sin_token(_audiencia):
        return None

    cliente = DocumentoPolizaHttp(
        "https://fn-documentos.run.app",
        client=httpx.AsyncClient(
            base_url="https://fn-documentos.run.app", transport=httpx.MockTransport(manejador)
        ),
        proveedor_de_token=sin_token,
    )

    await cliente.consultar({})

    assert "authorization" not in pedidos[0].headers


@pytest.mark.parametrize("estado", [400, 500, 503])
async def test_un_error_de_la_funcion_es_documento_no_disponible(estado):
    cliente = _http(lambda _p: httpx.Response(estado))

    with pytest.raises(DocumentoNoDisponible):
        await cliente.consultar({})


async def test_un_corte_de_red_es_documento_no_disponible():
    def manejador(_p):
        raise httpx.ConnectError("sin red")

    with pytest.raises(DocumentoNoDisponible):
        await _http(manejador).consultar({})


async def test_cierra_el_cliente():
    await _http(lambda _p: httpx.Response(200, json=RESPUESTA)).aclose()


def test_la_fabrica_arma_el_cliente_http_y_exige_la_url():
    memoria = Settings(repository_backend="memory", documento_backend="http")
    with pytest.raises(ValueError, match="DOCUMENTO_BASE_URL"):
        build_polizas(memoria, InMemoryEvidenciaRepository())

    repo, _, documentos, cerrables = build_polizas(
        Settings(
            repository_backend="memory",
            documento_backend="http",
            documento_base_url="https://fn-documentos.run.app",
        ),
        InMemoryEvidenciaRepository(),
    )

    assert isinstance(repo, InMemoryPolizaRepository)
    assert isinstance(documentos, DocumentoPolizaHttp)
    assert cerrables == [documentos]


# --- qué se le manda a fn-documentos ---


async def test_los_datos_para_el_documento_traen_al_asegurado():
    e = Escenario()
    cliente = await _listo(e)
    poliza, _ = await e.solicitar(cliente, cot=None)

    datos = datos_para_documento(poliza, cliente)

    assert datos["polizaId"] == poliza.id
    assert datos["numero"] == poliza.numero
    assert datos["asegurado"]["nombre"] == "Ana María Ríos"
    assert datos["asegurado"]["tipoDocumento"] == "CC"
    assert datos["datosCredito"]["entidadAcreedora"] == "Bancolombia"
    assert datos_para_documento(poliza, None)["asegurado"] is None


async def test_el_documento_de_una_emitida_le_pasa_los_datos_al_servicio_de_documentos():
    e = Escenario()
    cliente = await _listo(e)
    poliza, _ = await e.solicitar(cliente)
    recibidos = []

    async def consultar(datos):
        recibidos.append(datos)
        return await DocumentoFalso.consultar(datos)

    e.servicio._documentos.consultar = consultar

    await e.servicio.documento(cliente.id, poliza.id)

    assert recibidos[0]["numero"] == poliza.numero
    assert recibidos[0]["asegurado"]["email"] == cliente.email


class DocumentoFalso:
    @staticmethod
    async def consultar(_datos):
        from app.domain import DocumentoPoliza

        return DocumentoPoliza(EstadoDocumentoPoliza.LISTO, url="https://x")


# --- correo con el PDF adjunto ---


class CorreosEspia:
    def __init__(self, error: Exception | None = None) -> None:
        self.enviados: list[tuple] = []
        self.error = error

    async def enviar_poliza_emitida(self, cliente, numero, documento):
        if self.error:
            raise self.error
        self.enviados.append((cliente.id, numero, documento))


async def _emitida(correos):
    e = Escenario()
    e.servicio._correos = correos
    cliente = await _listo(e)
    poliza, _ = await e.solicitar(cliente)
    return e, cliente, poliza, {**AVISO, "polizaId": poliza.id}


async def test_el_aviso_del_documento_encarga_el_correo_con_el_archivo():
    correos = CorreosEspia()
    e, cliente, poliza, aviso = await _emitida(correos)

    await e.servicio.notificar_documento(aviso)

    [(cliente_id, numero, documento)] = correos.enviados
    assert (cliente_id, numero) == (cliente.id, poliza.numero)
    assert documento == ReferenciaDocumento(
        "solventa-dev-polizas",
        "polizas/x.pdf",
        "Poliza-SOL-VH-2026-K7M2Q9.pdf",
        "application/pdf",
    )


async def test_un_aviso_repetido_no_manda_dos_correos():
    correos = CorreosEspia()
    e, _, _, aviso = await _emitida(correos)

    await e.servicio.notificar_documento(aviso)
    await e.servicio.notificar_documento(aviso)

    assert len(correos.enviados) == 1


async def test_si_el_correo_falla_se_deshace_el_reclamo_y_se_puede_reintentar():
    correos = CorreosEspia(error=NotificacionError("Productos no responde"))
    e, _, _, aviso = await _emitida(correos)

    with pytest.raises(NotificacionError):
        await e.servicio.notificar_documento(aviso)
    correos.error = None
    await e.servicio.notificar_documento(aviso)

    assert len(correos.enviados) == 1


async def test_sin_servicio_de_correos_el_aviso_no_hace_nada():
    e, _, poliza, aviso = await _emitida(None)

    await e.servicio.notificar_documento(aviso)

    assert (await e.polizas.obtener(poliza.id)).documento_enviado_en is None


async def test_el_aviso_de_una_poliza_inexistente_o_pendiente_se_ignora():
    correos = CorreosEspia()
    e, _, _, aviso = await _emitida(correos)
    pendiente = await e.cliente(confirmado=False)
    e.cotizar(pendiente.id)
    sin_emitir, _ = await e.solicitar(pendiente)

    await e.servicio.notificar_documento({**aviso, "polizaId": "no-existe"})
    await e.servicio.notificar_documento({**aviso, "polizaId": sin_emitir.id})

    assert correos.enviados == []


async def test_el_aviso_de_una_poliza_cuyo_cliente_no_existe_se_ignora():
    correos = CorreosEspia()
    e, cliente, _, aviso = await _emitida(correos)
    e.clientes._by_id.pop(cliente.id)

    await e.servicio.notificar_documento(aviso)

    assert correos.enviados == []


async def test_el_consumidor_despacha_el_aviso_del_documento():
    correos = CorreosEspia()
    e, _, _, aviso = await _emitida(correos)

    await despachar_evento(e.servicio, DomainEvent("DocumentoPolizaGenerado", aviso))

    assert len(correos.enviados) == 1


async def test_un_aviso_sin_los_campos_necesarios_se_descarta_sin_reintentar():
    correos = CorreosEspia()
    e, _, _, aviso = await _emitida(correos)
    incompleto = {k: v for k, v in aviso.items() if k != "bucket"}

    await despachar_evento(e.servicio, DomainEvent("DocumentoPolizaGenerado", incompleto))

    assert correos.enviados == []
    # el reclamo no quedó puesto: un aviso bueno posterior sí sale
    await despachar_evento(e.servicio, DomainEvent("DocumentoPolizaGenerado", aviso))
    assert len(correos.enviados) == 1


# --- el evento que sale hacia fn-notificaciones ---


class PlantillasConNumero(FakePlantillasCorreo):
    pass


async def test_el_correo_de_la_poliza_lleva_el_numero_y_el_adjunto_como_referencia():
    from app.adapters.memory import InMemoryEventPublisher
    from app.adapters.notificaciones_fake import FakeIdentidadAdmin
    from tests.unit.test_notificaciones import _cliente

    eventos = InMemoryEventPublisher()
    servicio = NotificacionService(
        PlantillasConNumero(),
        FakeIdentidadAdmin(),
        eventos,
        url_web="https://solventa4bits.com",
        url_verificacion="https://solventa4bits.com/verificar",
        mercado="CO",
        idioma="es-CO",
    )

    await servicio.enviar_poliza_emitida(
        _cliente(),
        "SOL-VH-2026-K7M2Q9",
        ReferenciaDocumento(
            "solventa-dev-polizas", "polizas/x.pdf", "Poliza-SOL.pdf", "application/pdf"
        ),
    )

    [evento] = eventos.events
    assert evento.tipo == "EnviarCorreo"
    assert evento.datos["plantilla"] == "poliza-emitida"
    assert evento.datos["destinatario"] == "ana@example.com"
    assert "SOL-VH-2026-K7M2Q9" in evento.datos["cuerpoTexto"]
    assert evento.datos["adjuntos"] == [
        {
            "nombre": "Poliza-SOL.pdf",
            "bucket": "solventa-dev-polizas",
            "objeto": "polizas/x.pdf",
            "contentType": "application/pdf",
        }
    ]
