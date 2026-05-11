FROM python:3.12-slim@sha256:ec948fa5f90f4f8907e89f4800cfd2d2e91e391a4bce4a6afa77ba265bc3a2fe AS runtime

WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt
COPY *.py .

RUN useradd -m -s /bin/bash ablookup \
    && mkdir -p /data /data/tmp \
    && chown -R ablookup:ablookup /app /data

USER ablookup
EXPOSE 8200

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8200/health')"

CMD ["python", "-m", "main"]
