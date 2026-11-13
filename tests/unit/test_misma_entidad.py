"""BITS-386: cruce de la entidad de la póliza con la que reporta Open Finance."""

from __future__ import annotations

import pytest

from app.sismos import misma_entidad


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("BANCOLOMBIA S.A.", "Bancolombia"),
        ("Bancolombia", "bancolombia"),
        ("BANCO DE BOGOTÁ S.A.", "Banco de Bogota"),
        ("BBVA COLOMBIA S.A.", "BBVA Colombia"),
        ("ITAÚ COLOMBIA S.A.", "Itaú"),
        ("BANCO COMERCIAL AV VILLAS S.A.", "Banco AV Villas"),
        ("FONDO NACIONAL DEL AHORRO", "Fondo Nacional del Ahorro"),
        ("Scotiabank Colpatria S.A.S.", "SCOTIABANK COLPATRIA"),
    ],
)
def test_son_el_mismo_banco(a, b):
    assert misma_entidad(a, b) and misma_entidad(b, a)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Bancolombia", "Davivienda S.A."),
        ("BANCO DE BOGOTÁ S.A.", "BANCO DE OCCIDENTE S.A."),
        ("Banco Caja Social", "Banco AV Villas"),
        ("", "Bancolombia"),
        ("S.A.", "Bancolombia"),
    ],
)
def test_no_son_el_mismo_banco(a, b):
    assert not misma_entidad(a, b)
