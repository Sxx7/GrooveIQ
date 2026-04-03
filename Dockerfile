# GrooveIQ – Production Dockerfile
# Multi-stage build: keeps final image lean.
#
# NOTE: essentia-tensorflow only ships amd64 wheels.
# Build with: docker build --platform linux/amd64 -t grooveiq .
#
# Stage 1 (builder): download and prepare Python wheels
# Stage 2 (runtime): copy only what's needed to run

# ── Stage 1: builder ──────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Build tools needed to compile implicit (C/C++ via scikit-build/cmake)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
    && rm -rf /var/lib/apt/lists/*

# Copy and build/download wheels
COPY requirements.txt .
RUN pip install --upgrade pip wheel && \
    pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt


# ── Stage 2: runtime ──────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="GrooveIQ"
LABEL org.opencontainers.image.description="Behavioral music recommendation engine"
LABEL org.opencontainers.image.source="https://gitlab.local.devii.ch/simon/grooveiq"

# Runtime audio codec libs (needed by Essentia at import time)
# Package versions match Debian Trixie (python:3.12-slim base)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libavcodec61 \
        libavformat61 \
        libavutil59 \
        libswresample5 \
        libfftw3-double3 \
        libyaml-0-2 \
        libsamplerate0 \
        libtag2 \
        libchromaprint1 \
        libgomp1 \
        tini \
    && rm -rf /var/lib/apt/lists/*

# Non-root user for security
RUN groupadd --gid 1001 grooveiq && \
    useradd  --uid 1001 --gid grooveiq --no-create-home --shell /sbin/nologin grooveiq

WORKDIR /app

# Install pre-built wheels from builder stage
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels /wheels/* && \
    rm -rf /wheels

# Copy application source
COPY --chown=grooveiq:grooveiq app/ ./app/

# Data directories (override with volume mounts)
RUN mkdir -p /data /data/models /music /cache/essentia && \
    chown -R grooveiq:grooveiq /data /music /cache

# Essentia model cache (pre-trained mood/danceability models)
ENV ESSENTIA_MODELS_PATH=/cache/essentia

# Runtime environment defaults (all overridable via docker-compose / env)
ENV APP_ENV=production \
    DATABASE_URL=sqlite+aiosqlite:////data/grooveiq.db \
    MUSIC_LIBRARY_PATH=/music \
    LOG_JSON=true \
    LOG_LEVEL=INFO \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER grooveiq

EXPOSE 8000

# tini as PID 1 ensures clean shutdown and SIGTERM forwarding to uvicorn
ENTRYPOINT ["/usr/bin/tini", "--"]

CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--loop", "uvloop", \
     "--http", "httptools", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]
