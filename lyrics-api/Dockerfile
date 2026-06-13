# lyrics-api — faster-whisper (CTranslate2, CUDA) ASR sidecar for GrooveIQ.
#
# Tier 3 of the lyrics cascade. Runs on a GPU VM (nvidia-container-toolkit):
#   docker build -t lyrics-api .
#   docker run --gpus all -p 8300:8300 \
#     -v /path/to/music:/music:ro -v lyrics_models:/data/models lyrics-api
#
# CUDA 12 + cuDNN runtime is what CTranslate2 needs; no PyTorch is installed.
FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04 AS runtime

ENV DEBIAN_FRONTEND=noninteractive
# ffmpeg decodes arbitrary audio; python3 runs the service.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir --upgrade pip \
    && pip3 install --no-cache-dir -r requirements.txt

COPY main.py .

# Non-root user; /data/models holds the auto-downloaded model (named volume).
RUN useradd -m -s /bin/bash lyrics \
    && mkdir -p /music /data/models \
    && chown -R lyrics:lyrics /app /music /data

USER lyrics

EXPOSE 8300

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8300/health')"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8300", "--workers", "1"]
