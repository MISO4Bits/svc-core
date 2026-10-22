"""Casos de uso de las evidencias de identidad (cédula por ambas caras y selfie)."""

from __future__ import annotations

import asyncio
import logging

from app.domain import (
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
    ) -> None:
        self._clientes = clientes
        self._evidencias = evidencias
        self._almacen = almacen
        self._verificador = verificador
        self.max_bytes = max_bytes
        self._max_pixeles = max_pixeles
        self._intentos_max = intentos_max
        self._timeout_s = timeout_s

    async def subir(self, cliente_id: str, tipo: TipoEvidencia, imagen: bytes) -> ResultadoSubida:
        cliente = await self._clientes.obtener(cliente_id)
        if cliente is None:
            raise ClienteNoEncontrado(cliente_id)
        content_type = validar_imagen(
            imagen, max_bytes=self.max_bytes, max_pixeles=self._max_pixeles
        )

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
            veredicto = Veredicto(EstadoEvidencia.REVISION_MANUAL)

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
            )
        )
        await self._evidencias.reiniciar_rechazos(cliente_id, tipo)
        logger.info(
            "subir: evidencia guardada cliente_id=%s tipo=%s estado=%s",
            cliente_log,
            tipo,
            veredicto.estado,
        )
        return ResultadoSubida(
            estado=veredicto.estado,
            motivo=None,
            intentos_restantes=self._intentos_max,  # el contador se reinicia al guardar
            evidencia=evidencia,
        )

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
