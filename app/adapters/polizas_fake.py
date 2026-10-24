"""Dobles de Cotización y del documento de póliza, para correr Core solo y para las pruebas."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.domain import (
    CoberturaContratada,
    CodigoCobertura,
    CotizacionActiva,
    CotizacionNoDisponible,
    DatosCredito,
    DocumentoPoliza,
    EstadoDocumentoPoliza,
    OrigenDatos,
    Poliza,
    Vigencia,
    now_utc,
)


def cotizacion_de_ejemplo(
    *,
    origen: OrigenDatos = OrigenDatos.CONFIRMADO,
    edad: int = 38,
    plazo_meses: int = 240,
) -> CotizacionActiva:
    """La que arma Cotización para un crédito de $140 millones (cifras redondeadas)."""
    ahora = now_utc()
    return CotizacionActiva(
        estado="VIGENTE",
        edad=edad,
        datos_credito=DatosCredito(
            valor_credito=Decimal("150000000"),
            plazo_restante_meses=plazo_meses,
            entidad_acreedora="Bancolombia",
            saldo_insoluto=Decimal("140000000"),
        ),
        origen_datos=origen,
        tasa=Decimal("0.196"),
        version_tarifa=2,
        cobertura_meses=plazo_meses,
        coberturas=(
            CoberturaContratada(
                CodigoCobertura.VIDA,
                True,
                Decimal("140000000"),
                Decimal("24696"),
                Decimal("0"),
                Decimal("24696"),
            ),
            CoberturaContratada(
                CodigoCobertura.HOGAR,
                False,
                Decimal("150000000"),
                Decimal("14850"),
                Decimal("2822"),
                Decimal("17672"),
            ),
        ),
        vigencia=Vigencia(ahora, ahora + timedelta(days=30)),
        creada_en=ahora,
    )


class CotizacionFake:
    """Guarda una cotización activa por cliente. ``caida=True`` imita a Cotización caído."""

    def __init__(self, *, caida: bool = False) -> None:
        self._por_cliente: dict[str, CotizacionActiva] = {}
        self.caida = caida
        self.llamadas = 0

    def poner(self, cliente_id: str, cotizacion: CotizacionActiva) -> CotizacionActiva:
        self._por_cliente[cliente_id] = cotizacion
        return cotizacion

    def quitar(self, cliente_id: str) -> None:
        self._por_cliente.pop(cliente_id, None)

    async def activa(self, cliente_id: str) -> CotizacionActiva | None:
        self.llamadas += 1
        if self.caida:
            raise CotizacionNoDisponible()
        return self._por_cliente.get(cliente_id)


class DocumentoPolizaFake:
    """El documento siempre está listo, con un enlace de mentira."""

    async def consultar(self, poliza: Poliza) -> DocumentoPoliza:
        return DocumentoPoliza(
            EstadoDocumentoPoliza.LISTO,
            url=f"http://documentos.local/{poliza.numero}.pdf",
            expira_en=now_utc() + timedelta(minutes=15),
        )
