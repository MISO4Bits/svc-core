"""Clientes que un socio da de alta en el backoffice para venderles un seguro (BITS-378/379).

El socio captura los datos con el cliente delante. Core crea la cuenta del cliente en Identity
Platform con una contraseña temporal que genera él mismo (el socio nunca la ve: le llega al cliente
por correo), registra al cliente por el canal ``BACKOFFICE`` y le confirma el correo. Ninguna
autorización de datos queda otorgada: el cliente no las dio, así que Perfilamiento no consulta
nada hasta que él mismo las otorgue desde la web o la app.
"""

from __future__ import annotations

import logging
import secrets
from datetime import date

from app.domain import (
    Canal,
    Cliente,
    ClienteNoEncontrado,
    ClientesDistintos,
    ClienteYaExiste,
    CuentaClienteYaExiste,
    IdentidadNoDisponible,
    SolicitudPolizaInvalida,
    TipoDocumento,
)
from app.ports import ClienteRepository, CuentasClientePort
from app.services import IdentityService

logger = logging.getLogger("svc_core.clientes_asistidos")

_MINUSCULAS = "abcdefghijkmnopqrstuvwxyz"  # sin l
_MAYUSCULAS = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # sin I ni O
_DIGITOS = "23456789"  # sin 0 ni 1
_SIMBOLOS = "#$%*+-=?@"
_LARGO_CONTRASENA = 14


def generar_contrasena(aleatorio: secrets.SystemRandom | None = None) -> str:
    """Contraseña temporal con las cuatro clases de caracteres y sin los que se confunden al leer
    el correo (0/O, 1/l/I). Sale de ``secrets``: nunca de un generador predecible."""
    azar = aleatorio or secrets.SystemRandom()
    todos = _MINUSCULAS + _MAYUSCULAS + _DIGITOS + _SIMBOLOS
    caracteres = [azar.choice(clase) for clase in (_MINUSCULAS, _MAYUSCULAS, _DIGITOS, _SIMBOLOS)]
    caracteres += [azar.choice(todos) for _ in range(_LARGO_CONTRASENA - len(caracteres))]
    azar.shuffle(caracteres)
    return "".join(caracteres)


class ClientesAsistidosService:
    def __init__(
        self,
        clientes: ClienteRepository,
        identidad: IdentityService,
        cuentas: CuentasClientePort | None,
    ) -> None:
        self._clientes = clientes
        self._identidad = identidad
        self._cuentas = cuentas

    async def buscar(
        self,
        *,
        correo: str | None,
        tipo_documento: TipoDocumento | None,
        numero_documento: str | None,
    ) -> Cliente:
        """Busca por correo, por tipo y número de documento, o por los dos a la vez.

        Con los dos, deben ser de la misma cuenta: si son de cuentas distintas, o uno es de una
        cuenta y el otro no existe como cliente, ``ClientesDistintos`` (409) con el campo que
        choca. Así el socio no le vende a otra persona por un correo mal escrito.
        """
        por_documento = tipo_documento is not None and numero_documento is not None
        if (tipo_documento is None) != (numero_documento is None):
            raise SolicitudPolizaInvalida(
                "numeroDocumento", "El tipo y el número de documento van juntos"
            )
        if correo is None and not por_documento:
            raise SolicitudPolizaInvalida(
                "correo", "Indica el correo, o el tipo y el número de documento"
            )
        del_correo = await self._clientes.buscar_por_correo(correo.strip()) if correo else None
        del_documento = (
            await self._clientes.buscar_por_documento(tipo_documento, numero_documento)
            if por_documento
            else None
        )
        if correo is not None and por_documento:
            if del_documento is not None and del_correo is not None:
                if del_documento.id != del_correo.id:
                    raise ClientesDistintos("correo")
                return del_documento
            if del_documento is not None:
                return del_documento
            if del_correo is not None:
                # El correo es de otra persona: no se le vende por un documento que no es suyo.
                raise ClientesDistintos("documento")
            raise ClienteNoEncontrado("los datos indicados")
        cliente = del_correo if correo is not None else del_documento
        if cliente is None:
            raise ClienteNoEncontrado("el correo indicado" if correo else "el documento indicado")
        return cliente

    async def crear(
        self,
        *,
        tipo_documento: TipoDocumento,
        numero_documento: str,
        primer_nombre: str,
        primer_apellido: str,
        fecha_nacimiento: date,
        email: str,
        telefono: str,
        segundo_nombre: str | None = None,
        segundo_apellido: str | None = None,
    ) -> tuple[Cliente, str]:
        """Crea la cuenta y el cliente. Devuelve el cliente y la contraseña temporal, que quien
        llama solo debe usar para el correo de acceso: no se guarda ni se registra."""
        if self._cuentas is None:
            raise IdentidadNoDisponible()
        correo = email.strip().lower()
        # Antes de tocar Identity Platform: así un cliente repetido no crea ni borra una cuenta.
        if await self._clientes.existe_por_correo(correo):
            raise ClienteYaExiste(tipo_documento, numero_documento, campo="correo")
        if await self._clientes.existe_por_documento(tipo_documento, numero_documento):
            raise ClienteYaExiste(tipo_documento, numero_documento, campo="documento")

        contrasena = generar_contrasena()
        try:
            uid = await self._cuentas.crear_cuenta_cliente(
                correo, contrasena, f"{primer_nombre} {primer_apellido}"
            )
        except CuentaClienteYaExiste:
            # Tiene cuenta en Identity Platform pero no es cliente: no hay a quién enlazarla.
            raise ClienteYaExiste(tipo_documento, numero_documento, campo="correo") from None
        try:
            cliente = await self._identidad.registrar_cliente(
                identity_ref=uid,
                tipo_documento=tipo_documento,
                numero_documento=numero_documento,
                primer_nombre=primer_nombre,
                segundo_nombre=segundo_nombre,
                primer_apellido=primer_apellido,
                segundo_apellido=segundo_apellido,
                fecha_nacimiento=fecha_nacimiento,
                email=correo,
                telefono=telefono,
                canal=Canal.BACKOFFICE,
                autoriza_tratamiento_datos=False,
                autoriza_datos_financieros=False,
            )
        except BaseException:
            # No se deja una cuenta sin cliente: el correo quedaría tomado para siempre.
            await self._cuentas.borrar_cuenta(uid)
            raise
        confirmado = await self._identidad.confirmar_cliente(cliente.id)
        logger.info("cliente asistido creado cliente_id=%s", cliente.id)
        return confirmado, contrasena
