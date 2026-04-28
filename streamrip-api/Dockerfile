# streamrip-api — thin REST wrapper around streamrip
#
# Build:  docker build -t streamrip-api .
# Run:    docker run -p 8282:8282 -v /path/to/music:/music streamrip-api

FROM python:3.14-slim AS runtime

# FFmpeg is required by streamrip for transcoding
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Non-root user for safety
RUN useradd -m -s /bin/bash streamrip \
    && mkdir -p /music /config \
    && chown -R streamrip:streamrip /app /music /config

USER streamrip

# streamrip stores config in ~/.config/streamrip/config.toml
ENV XDG_CONFIG_HOME=/config

EXPOSE 8282

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8282/health')"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8282", "--workers", "1"]
