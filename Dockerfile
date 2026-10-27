# syntax=docker/dockerfile:1
# Multi-stage Dockerfile for wx-linux-exporter (Homelab-hardened)

# ==============================================================================
# Stage 1: Builder
# ==============================================================================
FROM python:3.12-slim AS builder

ARG INSTALL_OCR=0

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Build dependencies for C extensions / wheels
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc python3-dev && \
    rm -rf /var/lib/apt/lists/*

# Copy packaging metadata & source files needed for installation
COPY pyproject.toml requirements.txt requirements-ocr.txt README.md ./
COPY bin/ ./bin/
COPY clients/ ./clients/

# Install into /install prefix: runtime base + optional OCR
RUN pip install --prefix=/install --require-hashes -r requirements.txt && \
    pip install --prefix=/install --no-deps . && \
    if [ "$INSTALL_OCR" = "1" ]; then \
        pip install --prefix=/install -r requirements-ocr.txt; \
    fi

# ==============================================================================
# Stage 2: Runtime
# ==============================================================================
FROM python:3.12-slim AS runtime

ARG INSTALL_OCR=0

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/install/bin:${PATH}" \
    PYTHONPATH="/install/lib/python3.12/site-packages:${PYTHONPATH}" \
    PUID=1000 \
    PGID=1000 \
    WECHAT_VAULT_ROOT=/vault

# Runtime system utilities (curl for container HEALTHCHECK)
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    if [ "$INSTALL_OCR" = "1" ]; then \
        apt-get install -y --no-install-recommends libgomp1; \
    fi && \
    rm -rf /var/lib/apt/lists/*

# Copy installed prefix from builder
COPY --from=builder /install /install

# Setup dedicated non-root user and home directory
RUN groupadd -g 1000 wxuser 2>/dev/null || true && \
    useradd -u 1000 -g 1000 -d /home/wxuser -m wxuser 2>/dev/null || true && \
    mkdir -p /vault /home/wxuser /tmp && \
    chown -R 1000:1000 /home/wxuser /tmp

USER 1000:1000
WORKDIR /home/wxuser

# Expose default HTTP export port
EXPOSE 8765

# Healthcheck targeting the unauthenticated /health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://127.0.0.1:8765/health || exit 1

ENTRYPOINT ["wxexport"]
CMD ["--host", "0.0.0.0", "--port", "8765", "--vault-dir", "/vault/decrypted/current"]
