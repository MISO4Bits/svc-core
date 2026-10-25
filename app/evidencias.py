"""Casos de uso de las evidencias de identidad (cédula por ambas caras y selfie)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace

from app.domain import (
    CedulaIncompleta,
    Cliente,
    ClienteNoEncontrado,
    EstadoEvidencia,
    EstadoTipoEvidencia,
    Evidencia,
    ImagenInvalida,
    ResultadoSubida,
    TipoDocumento,
    TipoEvidencia,
    Veredicto,
    new_id,
)
from app.imagen import validar_imagen
from app.logging_utils import sanear_para_log
from app.ports import (
    AlmacenEvidencias,
    ClienteRepository,
    EvidenciaRepository,
    VerificadorBiometrico,
    VerificadorDocumento,
)

logger = logging.getLogger("svc_core.evidencias")

_EXTENSION = {"image/jpeg": "jpg", "image/png": "png"}
_ACEPTADAS = (EstadoEvidencia.ACEPTADA, EstadoEvidencia.REVISION_MANUAL)


class EvidenciaService:
    def __init__(
        self,
        clientes: ClienteRepository,
        evidencias: EvidenciaRepository,
        almacen: AlmacenEvidencias,
        verificador: VerificadorDocumento,
        *,
        max_bytes: int,
        max_pixeles: int,
        intentos_max: int,
        timeout_s: float,
        biometria: VerificadorBiometrico | None = None,
        biometria_timeout_s: float = 5.0,
        al_guardar: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._clientes = clientes
        self._evidencias = evidencias
        self._almacen = almacen
        self._verificador = verificador
        self.max_bytes = max_bytes
        self._max_pixeles = max_pixeles
        self._intentos_max = intentos_max
        self._timeout_s = timeout_s
        # Sin verificador biométrico la selfie solo pasa la revisión de calidad del de documentos.
        self._biometria = biometria
        self._biometria_timeout_s = biometria_timeout_s
        # Gancho: la póliza pendiente del cliente se vuelve a evaluar (BITS-101).
        self._al_guardar = al_guardar

    async def subir(self, cliente_id: str, tipo: TipoEvidencia, imagen: bytes) -> ResultadoSubida:
        cliente = await self._clientes.obtener(cliente_id)
        if cliente is None:
            raise ClienteNoEncontrado(cliente_id)
        content_type = validar_imagen(
            imagen, max_bytes=self.max_bytes, max_pixeles=self._max_pixeles
        )

        if tipo is TipoEvidencia.SELFIE and self._biometria is not None:
            veredicto = await self._verificar_selfie(cliente_id, imagen)
        else:
            veredicto = await self._verificar_documento(cliente, tipo, imagen)

        cliente_log = sanear_para_log(cliente_id)
        if veredicto.estado is EstadoEvidencia.REINTENTAR:
            rechazos = await self._evidencias.registrar_rechazo(cliente_id, tipo)
            logger.info(
                "subir: rechazada cliente_id=%s tipo=%s motivo=%s rechazos=%s",
                cliente_log,
                tipo,
                veredicto.motivo,
                rechazos,
            )
            if rechazos < self._intentos_max:
                return ResultadoSubida(
                    estado=EstadoEvidencia.REINTENTAR,
                    motivo=veredicto.motivo,
                    intentos_restantes=self._intentos_max - rechazos,
                )
            # Tercer rechazo: no se atrapa al usuario; esta foto sí se guarda para que una
            # persona la revise.
            veredicto = replace(veredicto, estado=EstadoEvidencia.REVISION_MANUAL, motivo=None)

        evidencia_id = new_id()
        referencia = await self._almacen.guardar(
            f"evidencias/{cliente_id}/{tipo}/{evidencia_id}.{_EXTENSION[content_type]}",
            imagen,
            content_type,
        )
        evidencia = await self._evidencias.guardar_reemplazando(
            Evidencia(
                id=evidencia_id,
                cliente_id=cliente_id,
                tipo=tipo,
                estado=veredicto.estado,
                almacen_ref=referencia,
                puntaje_vida=veredicto.puntaje_vida,
                puntaje_coincidencia=veredicto.puntaje_coincidencia,
            )
        )
        await self._evidencias.reiniciar_rechazos(cliente_id, tipo)
        logger.info(
            "subir: evidencia guardada cliente_id=%s tipo=%s estado=%s",
            cliente_log,
            tipo,
            veredicto.estado,
        )
        if self._al_guardar is not None:
            await self._al_guardar(cliente_id)
        return ResultadoSubida(
            estado=veredicto.estado,
            motivo=None,
            intentos_restantes=self._intentos_max,  # el contador se reinicia al guardar
            evidencia=evidencia,
        )

    async def _verificar_documento(
        self, cliente: Cliente, tipo: TipoEvidencia, imagen: bytes
    ) -> Veredicto:
        # El cruce con el número registrado solo existe para la cédula de ciudadanía: es el
        # único documento cuyo reverso sabemos leer.
        es_cc = cliente.tipo_documento is TipoDocumento.CC
        cruzar = tipo is TipoEvidencia.CEDULA_REVERSO and es_cc
        veredicto = await self._verificar(
            imagen, tipo, cliente.numero_documento if cruzar else None
        )
        if (
            tipo is not TipoEvidencia.SELFIE
            and not es_cc
            and veredicto.estado is EstadoEvidencia.ACEPTADA
        ):
            veredicto = Veredicto(EstadoEvidencia.REVISION_MANUAL)
        return veredicto

    async def _verificar_selfie(self, cliente_id: str, selfie: bytes) -> Veredicto:
        """Compara la selfie con el frente de la cédula ya guardado. Falla hacia "seguir"."""
        frente = await self._evidencias.vigente(cliente_id, TipoEvidencia.CEDULA_FRENTE)
        reverso = await self._evidencias.vigente(cliente_id, TipoEvidencia.CEDULA_REVERSO)
        if frente is None or reverso is None:
            raise CedulaIncompleta()
        if not {frente.estado, reverso.estado} <= set(_ACEPTADAS):
            raise CedulaIncompleta()
        try:
            foto_frente = await self._almacen.obtener(frente.almacen_ref)
            return await asyncio.wait_for(
                self._biometria.verificar(selfie, foto_frente), self._biometria_timeout_s
            )
        except TimeoutError:
            logger.warning("verificar_selfie: tiempo agotado, pasa a revisión manual")
        except Exception:
            logger.exception("verificar_selfie: falló la verificación, pasa a revisión manual")
        return Veredicto(EstadoEvidencia.REVISION_MANUAL)

    async def _verificar(
        self, imagen: bytes, tipo: TipoEvidencia, numero_esperado: str | None
    ) -> Veredicto:
        """Falla hacia "seguir": si el verificador tarda o se rompe, decide una persona."""
        try:
            return await asyncio.wait_for(
                self._verificador.analizar(imagen, tipo, numero_esperado), self._timeout_s
            )
        except ImagenInvalida:
            raise
        except TimeoutError:
            logger.warning("verificar: tiempo agotado tipo=%s, pasa a revisión manual", tipo)
        except Exception:
            logger.exception(
                "verificar: falló el verificador tipo=%s, pasa a revisión manual", tipo
            )
        return Veredicto(EstadoEvidencia.REVISION_MANUAL)

    async def estado(self, cliente_id: str) -> list[EstadoTipoEvidencia]:
        if await self._clientes.obtener(cliente_id) is None:
            raise ClienteNoEncontrado(cliente_id)
        return [
            EstadoTipoEvidencia(
                tipo=tipo,
                evidencia=await self._evidencias.vigente(cliente_id, tipo),
                intentos_restantes=self._intentos_max
                - await self._evidencias.rechazos(cliente_id, tipo),
            )
            for tipo in TipoEvidencia
        ]

    async def evidencias_listas(self, cliente_id: str) -> bool:
        """Compuerta a la firma: ambas caras de la cédula vigentes, sin consumir y aceptadas."""
        for tipo in (TipoEvidencia.CEDULA_FRENTE, TipoEvidencia.CEDULA_REVERSO):
            vigente = await self._evidencias.vigente(cliente_id, tipo)
            if vigente is None or vigente.estado not in _ACEPTADAS:
                return False
        return True

    async def consumir(self, cliente_id: str, poliza_ref: str) -> int:
        """Las evidencias vigentes pasan a la póliza; la emisión las referencia, no las copia."""
        return await self._evidencias.consumir(cliente_id, poliza_ref)
