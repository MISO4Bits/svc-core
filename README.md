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
| `CORE_PUSH_BACKEND` | `off` | avisos push (BITS-229): `off` \| `on` (al emitirse una póliza publica `EnviarPush` para los dispositivos registrados del cliente, con el publicador de `CORE_EVENT_BACKEND`) |
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

#### Selfie: prueba de vida y comparación con la cédula (BITS-235)

La selfie se compara con el frente de la cédula ya guardado, así que primero deben estar vigentes
**ambas caras** de la cédula (si no, `422`). Con `CORE_BIOMETRIA_BACKEND=local` el servidor revisa calidad,
que haya un solo rostro, que sea una persona real (prueba de vida pasiva) y que coincida con la cédula,
todo con modelos de OpenCV dentro del pod. Nivel básico, no certificado: no cubre cámaras virtuales ni
*deepfakes*. Los puntajes se guardan con la evidencia pero **no salen en la respuesta**.

| Variable | Default | Notas |
|---|---|---|
| `CORE_BIOMETRIA_BACKEND` | `mock` | `mock` \| `local` (modelos de OpenCV en el propio proceso) |
| `CORE_BIOMETRIA_MOCK_SECUENCIA` | `ACEPTADA` | como la del verificador de documentos, con los motivos `SIN_ROSTRO`, `VARIOS_ROSTROS`, `POSIBLE_SUPLANTACION`, `NO_COINCIDE_CON_CEDULA` |
| `CORE_BIOMETRIA_TIMEOUT_S` | `5.0` | si tarda más, la selfie pasa a revisión manual |
| `CORE_BIOMETRIA_MAX_CONCURRENTES` | `1` | análisis simultáneos por pod |
| `CORE_BIOMETRIA_MODELOS_RUTA` | `./models` | carpeta de los modelos; se cargan al arrancar y, si faltan, el servicio no arranca |
| `CORE_BIOMETRIA_VIDA_ACEPTAR` / `_RECHAZAR` | `0.80` / `0.40` | probabilidad de rostro real: sobre el primero se acepta, bajo el segundo se rechaza, en medio decide una persona |
| `CORE_BIOMETRIA_COINCIDENCIA_ACEPTAR` / `_RECHAZAR` | `0.40` / `0.25` | similitud coseno de SFace (la referencia de OpenCV para "misma persona" es ~0,36) |
| `CORE_BIOMETRIA_DETECCION_MIN` / `_ROSTRO_MIN` | `0.70` / `0.15` | confianza mínima del detector y tamaño mínimo del rostro (fracción del ancho) |

Los modelos (origen y licencias en [models/README.md](models/README.md)): tres van en el repositorio y
SFace (~37 MB) se baja con `python tools/descargar_modelos.py` (la imagen de Docker y el CI lo hacen solos).
Los umbrales son **provisionales**: se calibran con fotos reales usando
`python tools/calibrar_biometria.py --frente cedula.jpg --propias <carpeta> --ajenas <carpeta> --ataques <carpeta>`
(el servidor deja en el log los puntajes y la duración de cada etapa).

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

## Dispositivos y avisos push (BITS-229)

`PUT /v1/clientes/{clienteId}/dispositivos` registra el token de Firebase de un teléfono
(idempotente; si el token era de otro cliente pasa al nuevo; máximo 5 por cliente, se descarta el
menos visto) y `DELETE /v1/clientes/{clienteId}/dispositivos/{dispositivoId}` lo quita. Los llama
`bff-mobile` con el `clienteId` de la sesión.

Con `CORE_PUSH_BACKEND=on`, al emitirse una póliza se publica `EnviarPush` (junto a `PolizaEmitida`)
con los tokens del cliente, los textos aprobados y la ruta `seguro/<polizaId>`. `fn-notificaciones`
lo entrega por Firebase. Si el cliente no tiene dispositivos no se publica nada, y un fallo al
publicar solo se registra: nunca afecta a la emisión. Los logs nunca llevan el token completo.
