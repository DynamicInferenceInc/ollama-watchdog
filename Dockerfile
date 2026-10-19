FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WATCHDOG_DATA_DIR=/data \
    WATCHDOG_PORT=8080

WORKDIR /app

COPY pyproject.toml ./
COPY ollama_watchdog ./ollama_watchdog

RUN pip install --no-cache-dir . tzdata \
    && useradd --system --uid 10001 --no-create-home watchdog \
    && mkdir -p /data \
    && chown watchdog /data

USER watchdog
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3)"

CMD ["ollama-watchdog-web"]
