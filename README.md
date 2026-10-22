# svc-core

CoreTransaccional — submódulo **Identidad y Consentimiento** (`ICustomerIdentity`).
Contrato: [`openapi/openapi.yaml`](openapi/openapi.yaml).

## Correr el servicio en local para pruebas

Requiere Python 3.12+.

```bash
# 1. entorno e instalación (incluye dependencias de prueba)
python -m venv .venv
source .venv/Scripts/activate      # Windows (Git Bash);  en Linux/Mac: source .venv/bin/activate
pip install -e ".[dev]"

# 2. levantar la API (backend SQLite local, eventos al log)
uvicorn app.main:app --reload --port 8080
```

- Docs interactivas: <http://localhost:8080/docs> · contrato: <http://localhost:8080/openapi.yaml> · salud: <http://localhost:8080/health>
- Crea un archivo `svc_core.db` (SQLite) en el directorio. Bórralo para empezar de cero (obligatorio si es anterior a BITS-93: `telefono` ahora es `NOT NULL` y SQLite no lo migra).

### Variables de entorno (opcionales, prefijo `CORE_`)

| Variable | Default | Notas |
|---|---|---|
| `CORE_REPOSITORY_BACKEND` | `sqlite` | `sqlite` \| `memory` (sin persistencia) |
| `CORE_DATABASE_PATH` | `./svc_core.db` | ruta del archivo SQLite |
| `CORE_EVENT_BACKEND` | `logging` | `logging` \| `memory` \| `pubsub` |
| `CORE_NOTIFICACIONES_BACKEND` | `off` | correos del registro: `off` \| `fake` (dobles locales) \| `real` (Productos + Identity Platform) |
| `CORE_PRODUCTOS_BASE_URL` | `http://svc-productos.svc-productos.svc.cluster.local` | de dónde sale la plantilla del correo |
| `CORE_IDENTITY_PROJECT_ID` | — | proyecto de Identity Platform (obligatoria con `real`) |
| `CORE_WEB_URL_BASE` / `CORE_WEB_URL_VERIFICACION` | `https://solventa4bits.com` / `.../verificar-correo` | enlaces que van dentro del correo |
| `CORE_CORREO_MERCADO` / `CORE_CORREO_IDIOMA` | `CO` / `es-CO` | plantilla que se pide a Productos |

#### Evidencias de identidad (cédula y selfie, BITS-275)

| Variable | Default | Notas |
|---|---|---|
| `CORE_VERIFICADOR_BACKEND` | `mock` | `mock` (veredictos configurables) \| `local` (analiza la imagen en el propio proceso) |
| `CORE_VERIFICADOR_MOCK_SECUENCIA` | `ACEPTADA` | veredictos del mock en rotación: `ACEPTADA`, `REVISION_MANUAL` o un motivo (`BORROSA`, `OSCURA`, `REFLEJO`, `RECORTADA`, `NO_ES_CEDULA`). P. ej. `BORROSA,ACEPTADA` rechaza la primera foto y acepta la siguiente |
| `CORE_VERIFICADOR_TIMEOUT_S` | `2.5` | si el verificador tarda más, la evidencia pasa a revisión manual |
| `CORE_VERIFICADOR_MAX_CONCURRENTES` | `2` | análisis simultáneos por pod (comparten procesador con las transacciones) |
| `CORE_ALMACEN_BACKEND` | `filesystem` | `memory` \| `filesystem` \| `gcs` |
| `CORE_ALMACEN_RUTA` | `./evidencias` | carpeta del almacén en disco (ignorada por git) |
| `CORE_ALMACEN_BUCKET` | — | bucket de Cloud Storage (obligatoria con `gcs`) |
| `CORE_EVIDENCIA_MAX_BYTES` | `3145728` | peso máximo de una imagen (3 MB) |
| `CORE_EVIDENCIA_INTENTOS_MAX` | `3` | al llegar a este número de rechazos de un tipo, pasa a revisión manual |
| `CORE_CALIDAD_*` | ver `app/config.py` | umbrales de calidad (nitidez, luz, reflejo, forma de la tarjeta) |

Subir una foto (una por request; `tipo` ∈ `cedula_frente`, `cedula_reverso`, `selfie`):

```bash
curl -X POST http://localhost:8080/v1/clientes/<clienteId>/evidencias-identidad \
  -F tipo=cedula_frente -F imagen=@frente.jpg
curl http://localhost:8080/v1/clientes/<clienteId>/evidencias-identidad   # estado por tipo
```

Con el `mock` los clientes pueden probar cada veredicto sin imágenes reales. Con `local`, los umbrales
son **provisionales**: se calibran con fotos reales usando
`python tools/calibrar_calidad.py --buenas <carpeta> --malas <carpeta>`. Las fotos de cédulas son datos
personales: no se suben al repositorio.

Con `CORE_NOTIFICACIONES_BACKEND=fake` y `CORE_EVENT_BACKEND=logging`, cada registro deja en el log
la publicación de los dos eventos `EnviarCorreo`, sin necesitar Productos ni Identity Platform.

### Prueba rápida con curl

```bash
curl -X POST http://localhost:8080/v1/clientes -H 'content-type: application/json' -d '{
  "identityRef":"idp-sub-001","tipoDocumento":"CC","numeroDocumento":"1032456789",
  "primerNombre":"Ana","primerApellido":"Rios","fechaNacimiento":"1991-05-20",
  "email":"ana.rios@example.com","telefono":"+573001234567","canal":"WEB",
  "autorizaTratamientoDatos":true,"autorizaDatosFinancieros":true}'
```

## Pruebas

```bash
pytest                                   # todas (unit + contrato + integración)
pytest --cov=app --cov-report=term-missing   # con cobertura (gate 80%)
pytest tests/unit          tests/contract          tests/integration   # por tipo
```

La suite usa SQLite en archivos temporales; no necesita Docker ni servicios externos.
