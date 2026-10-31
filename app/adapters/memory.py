"""Adaptadores en memoria. Usados en pruebas unitarias y para arranque rápido local."""

from __future__ import annotations

import base64
from datetime import UTC, datetime

from app.domain import (
    Cliente,
    Consentimiento,
    ConsentimientoScope,
    Decision,
    Dispositivo,
    DomainEvent,
    EstadoPoliza,
    Evidencia,
    EvidenciaDePoliza,
    FiltroPolizas,
    MotivoRechazoPoliza,
    NumeroPolizaDuplicado,
    PaginaPolizas,
    Pendiente,
    Poliza,
    PolizaYaExiste,
    TipoEvidencia,
    Vigencia,
    now_utc,
)


class InMemoryClienteRepository:
    def __init__(self, consentimientos: InMemoryConsentimientoRepository | None = None) -> None:
        self._by_id: dict[str, Cliente] = {}
        self._docs: set[tuple[str, str]] = set()
        self._emails: set[str] = set()
        self._consentimientos = consentimientos

    async def crear_con_consentimientos(
        self, cliente: Cliente, consentimientos: list[Consentimiento]
    ) -> Cliente:
        self._by_id[cliente.id] = cliente
        self._docs.add((str(cliente.tipo_documento), cliente.numero_documento))
        self._emails.add(cliente.email)
        if self._consentimientos is not None:
            for consentimiento in consentimientos:
                await self._consentimientos.guardar(consentimiento)
        return cliente

    async def obtener(self, cliente_id: str) -> Cliente | None:
        return self._by_id.get(cliente_id)

    async def obtener_por_identity_ref(self, identity_ref: str) -> Cliente | None:
        return next((c for c in self._by_id.values() if c.identity_ref == identity_ref), None)

    async def existe_por_documento(self, tipo_documento: str, numero_documento: str) -> bool:
        return (str(tipo_documento), numero_documento) in self._docs

    async def existe_por_correo(self, email: str) -> bool:
        return email in self._emails

    async def confirmar(self, cliente_id: str) -> Cliente | None:
        cliente = self._by_id.get(cliente_id)
        if cliente is None:
            return None
        if not cliente.correo_confirmado:
            cliente.correo_confirmado = True
            cliente.confirmado_en = now_utc()
        return cliente


class InMemoryConsentimientoRepository:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], Consentimiento] = {}

    async def listar(self, cliente_id: str) -> list[Consentimiento]:
        return sorted(
            (c for (cid, _s), c in self._items.items() if cid == cliente_id),
            key=lambda c: str(c.scope),
        )

    async def obtener(self, cliente_id: str, scope: ConsentimientoScope) -> Consentimiento | None:
        return self._items.get((cliente_id, str(scope)))

    async def guardar(self, consentimiento: Consentimiento) -> Consentimiento:
        self._items[(consentimiento.cliente_id, str(consentimiento.scope))] = consentimiento
        return consentimiento


class InMemoryIdempotencyStore:
    def __init__(self) -> None:
        self._data: dict[str, dict] = {}

    async def get(self, key: str) -> dict | None:
        return self._data.get(key)

    async def put(self, key: str, value: dict) -> None:
        self._data[key] = value


class InMemoryEventPublisher:
    def __init__(self) -> None:
        self.events: list[DomainEvent] = []

    async def publish(self, event: DomainEvent) -> None:
        self.events.append(event)


class InMemoryEvidenciaRepository:
    def __init__(self) -> None:
        self._items: list[Evidencia] = []
        self._rechazos: dict[tuple[str, str], int] = {}

    async def vigente(self, cliente_id: str, tipo: TipoEvidencia) -> Evidencia | None:
        candidatas = [
            e
            for e in self._items
            if e.cliente_id == cliente_id
            and e.tipo == tipo
            and e.reemplazada_en is None
            and e.poliza_ref is None
        ]
        return max(candidatas, key=lambda e: e.creada_en, default=None)

    async def guardar_reemplazando(self, evidencia: Evidencia) -> Evidencia:
        anterior = await self.vigente(evidencia.cliente_id, evidencia.tipo)
        if anterior is not None:
            anterior.reemplazada_en = now_utc()
        self._items.append(evidencia)
        return evidencia

    async def consumir(self, cliente_id: str, poliza_ref: str) -> int:
        consumidas = 0
        for tipo in TipoEvidencia:
            vigente = await self.vigente(cliente_id, tipo)
            if vigente is not None:
                vigente.poliza_ref = poliza_ref
                consumidas += 1
        return consumidas

    async def rechazos(self, cliente_id: str, tipo: TipoEvidencia) -> int:
        return self._rechazos.get((cliente_id, str(tipo)), 0)

    async def registrar_rechazo(self, cliente_id: str, tipo: TipoEvidencia) -> int:
        clave = (cliente_id, str(tipo))
        self._rechazos[clave] = self._rechazos.get(clave, 0) + 1
        return self._rechazos[clave]

    async def reiniciar_rechazos(self, cliente_id: str, tipo: TipoEvidencia) -> None:
        self._rechazos.pop((cliente_id, str(tipo)), None)


def _en_utc(valor: datetime) -> datetime:
    return valor.astimezone(UTC)


def _cursor(poliza: Poliza) -> str:
    crudo = f"{_en_utc(poliza.creada_en).isoformat()}|{poliza.id}"
    return base64.urlsafe_b64encode(crudo.encode()).decode()


def _decodificar_cursor(cursor: str) -> tuple[str, str]:
    creada_en, _, poliza_id = base64.urlsafe_b64decode(cursor.encode()).decode().partition("|")
    return creada_en, poliza_id


class InMemoryPolizaRepository:
    def __init__(self, evidencias: InMemoryEvidenciaRepository | None = None) -> None:
        self._items: dict[str, Poliza] = {}
        self._evidencias = evidencias

    async def crear(self, poliza: Poliza) -> None:
        for existente in self._items.values():
            if existente.cliente_id == poliza.cliente_id and _en_utc(
                existente.cotizacion_creada_en
            ) == _en_utc(poliza.cotizacion_creada_en):
                raise PolizaYaExiste(poliza.cliente_id)
        self._items[poliza.id] = poliza

    async def obtener(self, poliza_id: str) -> Poliza | None:
        return self._items.get(poliza_id)

    async def obtener_de_cotizacion(
        self, cliente_id: str, cotizacion_creada_en: datetime
    ) -> Poliza | None:
        return next(
            (
                p
                for p in self._items.values()
                if p.cliente_id == cliente_id
                and _en_utc(p.cotizacion_creada_en) == _en_utc(cotizacion_creada_en)
            ),
            None,
        )

    async def listar_de_cliente(self, cliente_id: str) -> list[Poliza]:
        propias = (p for p in self._items.values() if p.cliente_id == cliente_id)
        return sorted(propias, key=lambda p: p.creada_en, reverse=True)

    async def pendientes_de_cliente(self, cliente_id: str) -> list[Poliza]:
        return [
            p
            for p in self._items.values()
            if p.cliente_id == cliente_id and p.estado is EstadoPoliza.PENDIENTE
        ]

    async def listar(self, filtro: FiltroPolizas, ahora: datetime) -> PaginaPolizas:
        candidatas = [p for p in self._items.values() if _cumple(p, filtro, ahora)]
        candidatas.sort(
            key=lambda p: (_en_utc(p.creada_en), p.id), reverse=filtro.mas_recientes_primero
        )
        if filtro.cursor:
            creada_en, poliza_id = _decodificar_cursor(filtro.cursor)
            ref = (datetime.fromisoformat(creada_en), poliza_id)
            if filtro.mas_recientes_primero:
                candidatas = [p for p in candidatas if (_en_utc(p.creada_en), p.id) < ref]
            else:
                candidatas = [p for p in candidatas if (_en_utc(p.creada_en), p.id) > ref]
        pagina = candidatas[: filtro.limite]
        hay_mas = len(candidatas) > filtro.limite
        return PaginaPolizas(tuple(pagina), _cursor(pagina[-1]) if hay_mas else None)

    async def actualizar_pendientes(
        self, poliza_id: str, pendientes: tuple[Pendiente, ...], evaluada_en: datetime
    ) -> None:
        poliza = self._items.get(poliza_id)
        if poliza is not None and poliza.estado is EstadoPoliza.PENDIENTE:
            poliza.pendientes = pendientes
            poliza.ultima_evaluacion_en = evaluada_en

    async def emitir(
        self, poliza_id: str, numero: str, emitida_en: datetime, vigencia: Vigencia
    ) -> Poliza | None:
        poliza = self._items.get(poliza_id)
        if poliza is None or poliza.estado is not EstadoPoliza.PENDIENTE:
            return None
        if any(p.numero == numero for p in self._items.values()):
            raise NumeroPolizaDuplicado(numero)
        consumidas: list[EvidenciaDePoliza] = []
        if self._evidencias is not None:
            for tipo in TipoEvidencia:
                vigente = await self._evidencias.vigente(poliza.cliente_id, tipo)
                if vigente is not None:
                    consumidas.append(EvidenciaDePoliza(tipo, vigente.estado, vigente.id))
            await self._evidencias.consumir(poliza.cliente_id, poliza.id)
        poliza.estado = EstadoPoliza.EMITIDA
        poliza.numero = numero
        poliza.emitida_en = emitida_en
        poliza.vigencia = vigencia
        poliza.pendientes = ()
        poliza.evidencias = tuple(consumidas)
        return poliza

    async def aprobar(self, poliza_id: str, decision: Decision) -> bool:
        poliza = self._items.get(poliza_id)
        if poliza is None or poliza.estado is not EstadoPoliza.PENDIENTE:
            return False
        poliza.decisiones = (*poliza.decisiones, decision)
        return True

    async def rechazar(
        self,
        poliza_id: str,
        decision: Decision,
        motivos: tuple[MotivoRechazoPoliza, ...],
        rechazada_en: datetime,
    ) -> Poliza | None:
        poliza = self._items.get(poliza_id)
        if poliza is None or poliza.estado is not EstadoPoliza.PENDIENTE:
            return None
        poliza.decisiones = (*poliza.decisiones, decision)
        poliza.estado = EstadoPoliza.RECHAZADA
        poliza.motivos_rechazo = motivos
        poliza.rechazada_en = rechazada_en
        return poliza

    async def reclamar_envio_documento(self, poliza_id: str, cuando: datetime) -> bool:
        poliza = self._items.get(poliza_id)
        if poliza is None or poliza.documento_enviado_en is not None:
            return False
        poliza.documento_enviado_en = cuando
        return True

    async def liberar_envio_documento(self, poliza_id: str) -> None:
        poliza = self._items.get(poliza_id)
        if poliza is not None:
            poliza.documento_enviado_en = None

    async def borrar_pendientes_de_cliente(
        self, cliente_id: str, salvo_cotizacion: datetime | None
    ) -> int:
        a_borrar = [
            p.id
            for p in self._items.values()
            if p.cliente_id == cliente_id
            and p.estado is EstadoPoliza.PENDIENTE
            and (
                salvo_cotizacion is None
                or _en_utc(p.cotizacion_creada_en) != _en_utc(salvo_cotizacion)
            )
        ]
        for poliza_id in a_borrar:
            del self._items[poliza_id]
        return len(a_borrar)


def _cumple(poliza: Poliza, filtro: FiltroPolizas, ahora: datetime) -> bool:
    if filtro.cliente_id and poliza.cliente_id != filtro.cliente_id:
        return False
    if filtro.estado and poliza.estado_en(ahora) is not filtro.estado:
        return False
    if filtro.tipo_pendiente and not any(
        p.tipo is filtro.tipo_pendiente for p in poliza.pendientes
    ):
        return False
    if filtro.codigo_pendiente and not any(
        p.codigo is filtro.codigo_pendiente for p in poliza.pendientes
    ):
        return False
    if filtro.creada_desde and _en_utc(poliza.creada_en) < _en_utc(filtro.creada_desde):
        return False
    return not (filtro.creada_hasta and _en_utc(poliza.creada_en) > _en_utc(filtro.creada_hasta))


class InMemoryDispositivoRepository:
    def __init__(self) -> None:
        self._por_token: dict[str, Dispositivo] = {}

    async def registrar(self, dispositivo: Dispositivo, maximo: int) -> Dispositivo:
        guardado = self._por_token.get(dispositivo.token)
        if guardado is None:
            guardado = self._por_token[dispositivo.token] = dispositivo
        else:
            if guardado.cliente_id != dispositivo.cliente_id:
                guardado.cliente_id = dispositivo.cliente_id
                guardado.registrado_en = dispositivo.registrado_en
            guardado.plataforma = dispositivo.plataforma
            guardado.version_app = dispositivo.version_app
            guardado.ultimo_visto_en = dispositivo.ultimo_visto_en
        # El recién guardado siempre se queda, aunque otro tenga la misma marca de tiempo.
        propios = sorted(
            (d for d in self._por_token.values() if d.cliente_id == guardado.cliente_id),
            key=lambda d: (d is guardado, d.ultimo_visto_en),
        )
        for sobrante in propios[: max(0, len(propios) - maximo)]:
            del self._por_token[sobrante.token]
        return guardado

    async def quitar(self, cliente_id: str, dispositivo_id: str) -> bool:
        for token, d in self._por_token.items():
            if d.id == dispositivo_id and d.cliente_id == cliente_id:
                del self._por_token[token]
                return True
        return False

    async def de_cliente(self, cliente_id: str) -> list[Dispositivo]:
        return sorted(
            (d for d in self._por_token.values() if d.cliente_id == cliente_id),
            key=lambda d: d.ultimo_visto_en,
            reverse=True,
        )
