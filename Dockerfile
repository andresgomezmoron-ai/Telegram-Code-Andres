FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    CLAUDEGRAM_STATE_DIR=/data

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY claudegram/ ./claudegram/

RUN useradd --uid 10001 --create-home bot && mkdir -p /data && chown bot /data
USER bot
VOLUME ["/data"]

ENTRYPOINT ["python", "-m", "claudegram"]
