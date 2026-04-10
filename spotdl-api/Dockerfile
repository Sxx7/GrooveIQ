# spotdl-api — thin REST wrapper around spotDL
#
# Build:  docker build -t spotdl-api .
# Run:    docker run -p 8181:8181 -v /path/to/music:/music spotdl-api

FROM python:3.12-slim AS runtime

# FFmpeg is required by spotDL for transcoding
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Non-root user for safety
RUN useradd -m -s /bin/bash spotdl \
    && mkdir -p /music /cache \
    && chown -R spotdl:spotdl /app /music /cache

USER spotdl

EXPOSE 8181

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8181/health')"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8181", "--workers", "1"]
