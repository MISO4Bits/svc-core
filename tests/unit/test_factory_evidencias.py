from __future__ import annotations

import pytest

from app.adapters.almacen import FilesystemAlmacenEvidencias, InMemoryAlmacenEvidencias
from app.adapters.factory import build_evidencias
from app.adapters.memory import InMemoryEvidenciaRepository
from app.adapters.sqlite import SqliteEvidenciaRepository
from app.adapters.verificador_local import VerificadorLocal
from app.adapters.verificador_mock import VerificadorMock
from app.config import Settings


def test_por_defecto_usa_sqlite_disco_y_mock():
    repo, almacen, verificador = build_evidencias(Settings())

    assert isinstance(repo, SqliteEvidenciaRepository)
    assert isinstance(almacen, FilesystemAlmacenEvidencias)
    assert isinstance(verificador, VerificadorMock)


def test_en_memoria_y_con_el_verificador_real():
    repo, almacen, verificador = build_evidencias(
        Settings(repository_backend="memory", almacen_backend="memory", verificador_backend="local")
    )

    assert isinstance(repo, InMemoryEvidenciaRepository)
    assert isinstance(almacen, InMemoryAlmacenEvidencias)
    assert isinstance(verificador, VerificadorLocal)


def test_el_mock_toma_su_secuencia_de_la_configuracion():
    *_, verificador = build_evidencias(Settings(verificador_mock_secuencia="BORROSA"))

    assert isinstance(verificador, VerificadorMock)


@pytest.mark.parametrize(
    ("cambios", "texto"),
    [
        ({"repository_backend": "mongo"}, "repository_backend"),
        ({"almacen_backend": "ftp"}, "almacen_backend"),
        ({"verificador_backend": "magia"}, "verificador_backend"),
        ({"almacen_backend": "gcs", "almacen_bucket": None}, "CORE_ALMACEN_BUCKET"),
    ],
)
def test_una_configuracion_invalida_falla_al_arrancar(cambios, texto):
    with pytest.raises(ValueError, match=texto):
        build_evidencias(Settings(**cambios))
