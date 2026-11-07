"""Dobles de Cotización y del documento de póliza, para correr Core solo y para las pruebas."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from app.domain import (
    CoberturaContratada,
    CodigoCobertura,
    CotizacionActiva,
    CotizacionNoDisponible,
    DatosCredito,
    DocumentoPoliza,
    DocumentosLegalesNoDisponibles,
    EstadoDocumentoPoliza,
    OrigenDatos,
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
        self.precios: list[tuple[int, DatosCredito]] = []

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

    async def precio(self, edad: int, datos: DatosCredito) -> CotizacionActiva:
        """El precio de ejemplo con el crédito y la edad pedidos (sin guardar nada)."""
        self.llamadas += 1
        self.precios.append((edad, datos))
        if self.caida:
            raise CotizacionNoDisponible()
        base = cotizacion_de_ejemplo(edad=edad, plazo_meses=datos.plazo_restante_meses)
        coberturas = tuple(
            replace(
                c,
                suma_asegurada=datos.saldo_insoluto
                if c.codigo is CodigoCobertura.VIDA
                else datos.valor_credito,
            )
            for c in base.coberturas
        )
        return replace(base, datos_credito=datos, coberturas=coberturas)


class DocumentoPolizaFake:
    """El documento siempre está listo, con un enlace de mentira."""

    async def consultar(self, datos: dict) -> DocumentoPoliza:
        return DocumentoPoliza(
            EstadoDocumentoPoliza.LISTO,
            url=f"https://documentos.local/{datos['numero']}.pdf",
            expira_en=now_utc() + timedelta(minutes=15),
        )


class DocumentosLegalesFake:
    """La versión vigente de las condiciones es siempre la misma, salvo que se cambie."""

    def __init__(self, version: str = "V1", *, caido: bool = False) -> None:
        self.version = version
        self.caido = caido

    async def version_vigente(self, tipo: str) -> str:
        if self.caido:
            raise DocumentosLegalesNoDisponibles()
        return self.version
