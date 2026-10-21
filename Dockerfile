FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    DATA_DIR=/data \
    PORT=8000

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid appuser --create-home appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data

USER appuser

EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]

CMD ["python", "-m", "app.cli", "serve"]
