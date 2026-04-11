FROM python:3.12-slim AS runtime

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py .

RUN useradd -m -s /bin/bash ablookup \
    && mkdir -p /data /data/tmp \
    && chown -R ablookup:ablookup /app /data

USER ablookup
EXPOSE 8200

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8200/health')"

CMD ["python", "-m", "main"]
