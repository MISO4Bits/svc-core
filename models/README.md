# Modelos de biometría (BITS-235)

Prueba de vida pasiva y comparación de la selfie con la cédula. Se ejecutan con `cv2` (OpenCV), sin dependencias adicionales. Ver `app/biometria.py`.

| Archivo | Para qué | Origen | Licencia | En el repo |
|---|---|---|---|---|
| `face_detection_yunet_2023mar.onnx` | Detectar rostros (YuNet) | [opencv_zoo](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet) | MIT | Sí |
| `face_recognition_sface_2021dec.onnx` | Comparar dos rostros (SFace) | [opencv_zoo](https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface) | Apache 2.0 | **No** (37 MB); `python tools/descargar_modelos.py` |
| `MiniFASNetV2.onnx` | Prueba de vida, recorte ×2,7 | [yakhyo/face-anti-spoofing](https://github.com/yakhyo/face-anti-spoofing/releases/tag/weights), conversión a ONNX de [Silent-Face-Anti-Spoofing](https://github.com/minivision-ai/Silent-Face-Anti-Spoofing) | Apache 2.0 (repositorio) | Sí |
| `MiniFASNetV1SE.onnx` | Prueba de vida, recorte ×4,0 | ídem | Apache 2.0 (repositorio) | Sí |

Las sumas de control SHA-256 están en `tools/descargar_modelos.py`, que también las verifica.

La licencia de los pesos de MiniFASNet no se declara aparte de la del repositorio; si el proyecto saliera del uso académico habría que revisarla de nuevo.

Las dos redes de vida dan tres clases (`[impreso, real, pantalla]`); se usa la probabilidad de la clase "real" promediada entre las dos. Son un nivel básico, no certificado.
