# syntax=docker/dockerfile:1.7
# Imagen de svc-core (CoreTransaccional — ICustomerIdentity).
# Multi-stage: build de dependencias aislado + runtime slim, no-root, con healthcheck.

ARG PYTHON_VERSION=3.12

# ---------- build ----------
FROM python:${PYTHON_VERSION}-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Instala solo dependencias primero para aprovechar la caché de capas.
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install "."

# ---------- runtime ----------
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CORE_ENVIRONMENT=production

RUN groupadd --system app && useradd --system --gid app --home /app app
WORKDIR /app

COPY --from=build /opt/venv /opt/venv
COPY app ./app
COPY openapi ./openapi

USER app
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
