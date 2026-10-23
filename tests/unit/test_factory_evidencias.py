from __future__ import annotations

import pytest

from app.adapters.almacen import FilesystemAlmacenEvidencias, InMemoryAlmacenEvidencias
from app.adapters.factory import build_biometria, build_evidencias
from app.adapters.memory import InMemoryEvidenciaRepository
from app.adapters.sqlite import SqliteEvidenciaRepository
from app.adapters.verificador_biometrico import VerificadorBiometricoLocal
from app.adapters.verificador_local import VerificadorLocal
from app.adapters.verificador_mock import VerificadorBiometricoMock, VerificadorMock
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


def test_la_biometria_por_defecto_es_el_mock():
    assert isinstance(build_biometria(Settings()), VerificadorBiometricoMock)


def test_la_biometria_local_se_crea_sin_cargar_los_modelos():
    # Los modelos se cargan al arrancar la aplicación (precargar), no al construir el adaptador.
    verificador = build_biometria(
        Settings(biometria_backend="local", biometria_modelos_ruta="/no/existe")
    )

    assert isinstance(verificador, VerificadorBiometricoLocal)


def test_una_biometria_desconocida_falla_al_arrancar():
    with pytest.raises(ValueError, match="biometria_backend"):
        build_biometria(Settings(biometria_backend="magia"))
