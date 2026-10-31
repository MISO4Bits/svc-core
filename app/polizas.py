"""Casos de uso del submódulo Pólizas (BITS-101): solicitud, evaluación, emisión y decisión de un
agente.

La solicitud siempre se acepta. Si falta algo, la póliza queda ``PENDIENTE`` con la lista de
pendientes y se vuelve a evaluar cuando el cliente cumple algo (o un agente decide). Cuando no
queda ninguno, pasa a ``EMITIDA``: número, vigencia, evidencias consumidas y el evento
``PolizaEmitida`` para que Comunicaciones y Documentos genere el documento.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from app.domain import (
    MENSAJE_RECHAZO,
    PENDIENTES_DE_AGENTE,
    Cliente,
    ClienteNoActivo,
    ClienteNoEncontrado,
    CodigoCobertura,
    CodigoPendiente,
    CotizacionActiva,
    CotizacionCambio,
    CotizacionNoDisponible,
    CotizacionVencida,
    Decision,
    DocumentoAceptado,
    DocumentoPoliza,
    DomainEvent,
    EstadoCliente,
    EstadoEvidencia,
    EstadoPoliza,
    Evidencia,
    FiltroPolizas,
    MotivoRechazoPoliza,
    NumeroPolizaDuplicado,
    OrigenDatos,
    PaginaPolizas,
    Pendiente,
    PendienteNoAplica,
    Poliza,
    PolizaNoEmitida,
    PolizaNoEncontrada,
    PolizaNoPendiente,
    PolizaYaExiste,
    ReferenciaDocumento,
    SinCotizacion,
    SolicitudExistente,
    SolicitudPolizaInvalida,
    SolicitudRechazada,
    SolicitudVencida,
    TipoDecision,
    TipoEvidencia,
    Vigencia,
    edad_en,
    generar_numero_poliza,
    now_utc,
    sumar_meses,
    validar_rango_de_edad,
)
from app.logging_utils import sanear_para_log
from app.ports import (
    AvisosDePolizaPort,
    ClienteRepository,
    CorreosDePolizaPort,
    CotizacionPort,
    DocumentoPolizaPort,
    EventPublisher,
    EvidenciaRepository,
    PolizaRepository,
)

logger = logging.getLogger("svc_core.polizas")

DOCUMENTO_DE_POLIZA = "poliza"
_INTENTOS_NUMERO = 5


def datos_para_documento(poliza: Poliza, cliente: Cliente | None) -> dict:
    """Lo que necesita el documento de la póliza. Es el cuerpo del evento ``PolizaEmitida`` y lo
    que se le manda a fn-documentos para pedir el enlace."""
    assert poliza.vigencia is not None and poliza.emitida_en is not None
    return {
        "polizaId": poliza.id,
        "clienteId": poliza.cliente_id,
        "numero": poliza.numero,
        "emitidaEn": poliza.emitida_en.isoformat(),
        "vigencia": {
            "desde": poliza.vigencia.desde.isoformat(),
            "hasta": poliza.vigencia.hasta.isoformat(),
        },
        "coberturas": [
            {
                "codigo": str(c.codigo),
                "obligatoria": c.obligatoria,
                "sumaAsegurada": float(c.suma_asegurada),
                "primaNeta": float(c.prima_neta),
                "iva": float(c.iva),
                "primaTotal": float(c.prima_total),
            }
            for c in poliza.coberturas
        ],
        "primaTotalMensual": float(poliza.prima_total_mensual),
        "moneda": "COP",
        "datosCredito": {
            "valorCredito": float(poliza.datos_credito.valor_credito),
            "plazoRestanteMeses": poliza.datos_credito.plazo_restante_meses,
            "entidadAcreedora": poliza.datos_credito.entidad_acreedora,
            "saldoInsoluto": float(poliza.datos_credito.saldo_insoluto),
        },
        "origenDatos": str(poliza.origen_datos),
        "asegurado": None
        if cliente is None
        else {
            "nombre": " ".join(
                p
                for p in (
                    cliente.primer_nombre,
                    cliente.segundo_nombre,
                    cliente.primer_apellido,
                    cliente.segundo_apellido,
                )
                if p
            ),
            "tipoDocumento": str(cliente.tipo_documento),
            "numeroDocumento": cliente.numero_documento,
            "email": cliente.email,
        },
        "documentosAceptados": [
            {"tipo": d.tipo, "version": d.version, "aceptadoEn": d.aceptado_en.isoformat()}
            for d in poliza.documentos_aceptados
        ],
    }


class PolizaService:
    def __init__(
        self,
        clientes: ClienteRepository,
        evidencias: EvidenciaRepository,
        polizas: PolizaRepository,
        cotizaciones: CotizacionPort,
        documentos: DocumentoPolizaPort,
        events: EventPublisher,
        *,
        correos: CorreosDePolizaPort | None = None,
        avisos: AvisosDePolizaPort | None = None,
        ahora: Callable[[], datetime] = now_utc,
        aleatorio: Callable[[str], str] = secrets.choice,
    ) -> None:
        self._correos = correos
        self._avisos = avisos
        self._clientes = clientes
        self._evidencias = evidencias
        self._polizas = polizas
        self._cotizaciones = cotizaciones
        self._documentos = documentos
        self._events = events
        self._ahora = ahora
        self._aleatorio = aleatorio

    # --- cliente final ---

    async def solicitar(
        self,
        cliente_id: str,
        coberturas: Sequence[CodigoCobertura],
        cotizacion_creada_en: datetime,
        documentos: Sequence[tuple[str, str]],
    ) -> tuple[Poliza, bool]:
        """Crea la solicitud (o devuelve la que ya existe de esa cotización). El booleano es
        ``True`` si se creó ahora."""
        cliente = await self._clientes.obtener(cliente_id)
        if cliente is None:
            raise ClienteNoEncontrado(cliente_id)
        if cliente.estado is not EstadoCliente.ACTIVO:
            raise ClienteNoActivo(cliente_id)
        elegidas = frozenset(coberturas)
        if CodigoCobertura.VIDA not in elegidas:
            raise SolicitudPolizaInvalida("coberturas", "La cobertura VIDA es obligatoria")
        if not any(tipo == DOCUMENTO_DE_POLIZA for tipo, _ in documentos):
            raise SolicitudPolizaInvalida(
                "documentosAceptados", "Debes aceptar las condiciones de la póliza"
            )

        existente = await self._polizas.obtener_de_cotizacion(cliente_id, cotizacion_creada_en)
        if existente is not None:
            return await self._de_solicitud_existente(existente, elegidas), False

        cotizacion = await self._cotizacion_activa(cliente_id)
        if cotizacion.creada_en.astimezone(UTC) != cotizacion_creada_en.astimezone(UTC):
            raise CotizacionCambio()
        ahora = self._ahora()
        if cotizacion.estado != "VIGENTE" or ahora >= cotizacion.vigencia.hasta:
            raise CotizacionVencida()
        validar_rango_de_edad(
            edad_en(cliente.fecha_nacimiento, ahora.date()),
            cotizacion.datos_credito.plazo_restante_meses,
        )
        disponibles = {c.codigo: c for c in cotizacion.coberturas}
        faltan = sorted(str(c) for c in elegidas if c not in disponibles)
        if faltan:
            raise SolicitudPolizaInvalida(
                "coberturas", f"La cotización no incluye: {', '.join(faltan)}"
            )

        poliza = Poliza(
            cliente_id=cliente_id,
            cotizacion_creada_en=cotizacion.creada_en,
            solicitud_vigente_hasta=cotizacion.vigencia.hasta,
            coberturas=tuple(disponibles[c] for c in CodigoCobertura if c in elegidas),
            datos_credito=cotizacion.datos_credito,
            origen_datos=cotizacion.origen_datos,
            tasa=cotizacion.tasa,
            version_tarifa=cotizacion.version_tarifa,
            cobertura_meses=cotizacion.cobertura_meses,
            edad=cotizacion.edad,
            documentos_aceptados=tuple(
                DocumentoAceptado(tipo, version, ahora) for tipo, version in documentos
            ),
            creada_en=ahora,
        )
        poliza.pendientes = await self._evaluar(cliente_id, poliza)
        poliza.ultima_evaluacion_en = ahora
        # Como máximo una pendiente por cliente: la de otra cotización no llegó a feliz término.
        await self._polizas.borrar_pendientes_de_cliente(cliente_id, cotizacion.creada_en)
        try:
            await self._polizas.crear(poliza)
        except PolizaYaExiste:
            ganadora = await self._polizas.obtener_de_cotizacion(cliente_id, cotizacion.creada_en)
            assert ganadora is not None
            return await self._de_solicitud_existente(ganadora, elegidas), False
        logger.info(
            "solicitar: póliza creada cliente_id=%s poliza_id=%s pendientes=%s",
            sanear_para_log(cliente_id),
            poliza.id,
            [str(p.codigo) for p in poliza.pendientes],
        )
        if not poliza.pendientes:
            poliza = await self._emitir(poliza)
        return poliza, True

    async def obtener(self, cliente_id: str, poliza_id: str) -> Poliza:
        """La póliza de un cliente; si está pendiente, se re-evalúa antes de devolverla."""
        poliza = await self._polizas.obtener(poliza_id)
        if poliza is None or poliza.cliente_id != cliente_id:
            raise PolizaNoEncontrada(poliza_id)
        return await self.reevaluar(poliza)

    async def listar_de_cliente(
        self, cliente_id: str, estado: EstadoPoliza | None = None
    ) -> list[Poliza]:
        if await self._clientes.obtener(cliente_id) is None:
            raise ClienteNoEncontrado(cliente_id)
        ahora = self._ahora()
        polizas = await self._polizas.listar_de_cliente(cliente_id)
        return [p for p in polizas if estado is None or p.estado_en(ahora) is estado]

    async def documento(self, cliente_id: str, poliza_id: str) -> DocumentoPoliza:
        poliza = await self.obtener(cliente_id, poliza_id)
        if poliza.estado is not EstadoPoliza.EMITIDA:
            raise PolizaNoEmitida()
        cliente = await self._clientes.obtener(cliente_id)
        return await self._documentos.consultar(datos_para_documento(poliza, cliente))

    # --- evaluación y emisión ---

    async def reevaluar(self, poliza: Poliza) -> Poliza:
        """Vuelve a mirar qué falta; si no falta nada, emite. No llama a Cotización."""
        if poliza.estado is not EstadoPoliza.PENDIENTE:
            return poliza
        ahora = self._ahora()
        if poliza.estado_en(ahora) is EstadoPoliza.VENCIDA:
            return poliza
        pendientes = await self._evaluar(poliza.cliente_id, poliza)
        if pendientes != poliza.pendientes:
            await self._polizas.actualizar_pendientes(poliza.id, pendientes, ahora)
            poliza.pendientes = pendientes
            poliza.ultima_evaluacion_en = ahora
        if not pendientes:
            return await self._emitir(poliza)
        return poliza

    async def reevaluar_cliente(self, cliente_id: str) -> None:
        """Gancho: el cliente cumplió algo (confirmó el correo, quedó una evidencia). Un fallo
        aquí no debe romper la operación que lo disparó; el siguiente ``GET`` lo corrige."""
        try:
            for poliza in await self._polizas.pendientes_de_cliente(cliente_id):
                await self.reevaluar(poliza)
        except Exception:
            logger.exception(
                "reevaluar_cliente: falló la re-evaluación cliente_id=%s",
                sanear_para_log(cliente_id),
            )

    async def descartar_pendientes(
        self, cliente_id: str, cotizacion_creada_en: datetime | None
    ) -> None:
        """El cliente volvió a cotizar (o eliminó su cotización): sus pólizas pendientes de la
        cotización anterior no llegaron a feliz término y se borran."""
        borradas = await self._polizas.borrar_pendientes_de_cliente(
            cliente_id, cotizacion_creada_en
        )
        if borradas:
            logger.info(
                "descartar_pendientes: borradas=%s cliente_id=%s",
                borradas,
                sanear_para_log(cliente_id),
            )

    async def _evaluar(self, cliente_id: str, poliza: Poliza) -> tuple[Pendiente, ...]:
        cliente = await self._clientes.obtener(cliente_id)
        if cliente is None:
            raise ClienteNoEncontrado(cliente_id)
        aprobados = poliza.aprobados()
        pendientes: list[Pendiente] = []
        if not cliente.correo_confirmado:
            pendientes.append(Pendiente.de(CodigoPendiente.CORREO_SIN_CONFIRMAR))

        faltan: list[TipoEvidencia] = []
        en_revision: list[TipoEvidencia] = []
        for tipo in TipoEvidencia:
            vigente = await self._evidencias.vigente(cliente_id, tipo)
            if vigente is None:
                faltan.append(tipo)
            elif vigente.estado is EstadoEvidencia.REVISION_MANUAL:
                en_revision.append(tipo)
        if faltan:
            pendientes.append(Pendiente.de(CodigoPendiente.EVIDENCIA_FALTANTE, tuple(faltan)))
        if en_revision and CodigoPendiente.EVIDENCIA_EN_REVISION not in aprobados:
            pendientes.append(
                Pendiente.de(CodigoPendiente.EVIDENCIA_EN_REVISION, tuple(en_revision))
            )

        if (
            poliza.origen_datos is OrigenDatos.DECLARADO
            and CodigoPendiente.CREDITO_NO_VERIFICADO not in aprobados
        ):
            pendientes.append(Pendiente.de(CodigoPendiente.CREDITO_NO_VERIFICADO))
        return tuple(pendientes)

    async def _emitir(self, poliza: Poliza) -> Poliza:
        for _ in range(_INTENTOS_NUMERO):
            emitida_en = self._ahora()
            numero = generar_numero_poliza(emitida_en, self._aleatorio)
            vigencia = Vigencia(emitida_en, sumar_meses(emitida_en, poliza.cobertura_meses))
            try:
                emitida = await self._polizas.emitir(poliza.id, numero, emitida_en, vigencia)
            except NumeroPolizaDuplicado:
                continue
            if emitida is None:
                # Otra evaluación la emitió, un agente la rechazó o se borró: se muestra lo vigente.
                return await self._polizas.obtener(poliza.id) or poliza
            await self._publicar_emitida(emitida)
            return emitida
        raise RuntimeError("no se pudo generar un número de póliza único")

    async def _publicar_emitida(self, poliza: Poliza) -> None:
        """Lleva todo lo que el documento necesita, para que quien lo genera no llame a Core."""
        cliente = await self._clientes.obtener(poliza.cliente_id)
        try:
            await self._events.publish(
                DomainEvent("PolizaEmitida", datos_para_documento(poliza, cliente))
            )
        except Exception:
            # La póliza ya está emitida; el documento se genera bajo demanda si falta.
            logger.exception("no se pudo publicar PolizaEmitida poliza_id=%s", poliza.id)
        await self._avisar_emitida(poliza)

    async def _avisar_emitida(self, poliza: Poliza) -> None:
        """Aviso push de póliza emitida (BITS-229). Un fallo se registra y nada más: la póliza
        ya está emitida y el cliente sigue teniendo el correo."""
        if self._avisos is None:
            return
        try:
            await self._avisos.avisar_poliza_emitida(poliza)
        except Exception:
            logger.exception("no se pudo encargar el aviso push poliza_id=%s", poliza.id)

    async def notificar_documento(self, datos: dict) -> None:
        """``DocumentoPolizaGenerado``: el PDF ya está en el bucket, se encarga el correo con el
        archivo adjunto. Idempotente: el aviso puede llegar más de una vez y el correo sale una.

        Un fallo se propaga (y el reclamo se deshace) para que Pub/Sub reentregue el aviso.
        """
        if self._correos is None:
            logger.info("notificar_documento: correos desactivados")
            return
        poliza = await self._polizas.obtener(datos["polizaId"])
        if poliza is None or poliza.estado is not EstadoPoliza.EMITIDA:
            logger.warning("notificar_documento: la póliza no está emitida o no existe")
            return
        cliente = await self._clientes.obtener(poliza.cliente_id)
        if cliente is None:
            logger.warning("notificar_documento: el cliente de la póliza no existe")
            return
        if not await self._polizas.reclamar_envio_documento(poliza.id, self._ahora()):
            logger.info("notificar_documento: el correo ya se encargó poliza_id=%s", poliza.id)
            return
        try:
            await self._correos.enviar_poliza_emitida(
                cliente,
                poliza.numero or "",
                ReferenciaDocumento(
                    bucket=datos["bucket"],
                    objeto=datos["objeto"],
                    nombre_archivo=datos["nombreArchivo"],
                    content_type=datos.get("contentType", "application/pdf"),
                ),
            )
        except Exception:
            await self._polizas.liberar_envio_documento(poliza.id)
            raise
        logger.info("notificar_documento: correo encargado poliza_id=%s", poliza.id)

    async def _de_solicitud_existente(
        self, existente: Poliza, elegidas: frozenset[CodigoCobertura]
    ) -> Poliza:
        if existente.estado is EstadoPoliza.RECHAZADA:
            raise SolicitudRechazada()
        if existente.codigos_cobertura() != elegidas:
            raise SolicitudExistente()
        return await self.reevaluar(existente)

    async def _cotizacion_activa(self, cliente_id: str) -> CotizacionActiva:
        try:
            cotizacion = await self._cotizaciones.activa(cliente_id)
        except CotizacionNoDisponible:
            raise
        except Exception as exc:
            logger.exception("cotización activa: fallo inesperado")
            raise CotizacionNoDisponible() from exc
        if cotizacion is None:
            raise SinCotizacion()
        return cotizacion

    # --- backoffice ---

    async def listar(self, filtro: FiltroPolizas) -> PaginaPolizas:
        return await self._polizas.listar(filtro, self._ahora())

    async def obtener_interna(self, poliza_id: str) -> Poliza:
        poliza = await self._polizas.obtener(poliza_id)
        if poliza is None:
            raise PolizaNoEncontrada(poliza_id)
        return poliza

    async def evidencias_vigentes(
        self, cliente_id: str
    ) -> list[tuple[TipoEvidencia, Evidencia | None]]:
        """Estado vigente de cada tipo de evidencia del cliente, para quien revisa."""
        return [(tipo, await self._evidencias.vigente(cliente_id, tipo)) for tipo in TipoEvidencia]

    async def decidir(
        self,
        poliza_id: str,
        decision: TipoDecision,
        agente: str,
        pendientes: Sequence[CodigoPendiente],
        nota: str | None,
    ) -> Poliza:
        """El personal de Solventa aprueba pendientes de agente o rechaza la póliza."""
        poliza = await self.obtener_interna(poliza_id)
        ahora = self._ahora()
        if poliza.estado_en(ahora) is EstadoPoliza.VENCIDA:
            raise SolicitudVencida()
        if poliza.estado is not EstadoPoliza.PENDIENTE:
            raise PolizaNoPendiente()

        indicados = tuple(dict.fromkeys(pendientes))
        vigentes = {p.codigo for p in poliza.pendientes}
        # Repetir una aprobación ya registrada no es un error.
        permitidos = vigentes | (poliza.aprobados() if decision is TipoDecision.APROBAR else set())
        for codigo in indicados:
            if codigo not in PENDIENTES_DE_AGENTE or codigo not in permitidos:
                raise PendienteNoAplica(str(codigo))

        registro = Decision(decision, agente, indicados, ahora, nota)
        if decision is TipoDecision.RECHAZAR:
            motivos = tuple(MotivoRechazoPoliza(c, MENSAJE_RECHAZO[c]) for c in indicados)
            rechazada = await self._polizas.rechazar(poliza.id, registro, motivos, ahora)
            if rechazada is None:
                raise PolizaNoPendiente()
            logger.info("decidir: rechazada poliza_id=%s agente=%s", poliza.id, agente)
            return rechazada

        nuevos = tuple(c for c in indicados if c not in poliza.aprobados())
        if nuevos:
            if not await self._polizas.aprobar(
                poliza.id, Decision(decision, agente, nuevos, ahora, nota)
            ):
                raise PolizaNoPendiente()
            logger.info(
                "decidir: aprobada poliza_id=%s agente=%s pendientes=%s",
                poliza.id,
                agente,
                [str(c) for c in nuevos],
            )
        actual = await self.obtener_interna(poliza.id)
        return await self.reevaluar(actual)
