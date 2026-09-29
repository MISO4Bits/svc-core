"""Lista curada de dominios de correo desechable (BITS-93 AC-7).

No es un servicio externo — una lista inicial a mano es suficiente para el alcance
académico del proyecto. Se puede ampliar con el tiempo.
"""

from __future__ import annotations

DOMINIOS_DESECHABLES: frozenset[str] = frozenset(
    {
        "mailinator.com",
        "tempmail.com",
        "10minutemail.com",
        "guerrillamail.com",
        "yopmail.com",
        "trashmail.com",
        "getnada.com",
        "dispostable.com",
        "fakeinbox.com",
        "throwawaymail.com",
    }
)


def es_desechable(email: str) -> bool:
    _, _, dominio = email.rpartition("@")
    return dominio.lower() in DOMINIOS_DESECHABLES
