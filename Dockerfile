FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=5000 \
    CAMERA_HOST=0.0.0.0 \
    CAMERA_DATA_DIR=/data \
    CAMERA_SCAN_DIR=/app \
    CAMERA_NO_BROWSER=1 \
    CAMERA_MAX_LIVE_STREAMS=16

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY camera_dashboard.py .
COPY app.py .

RUN mkdir -p /data /app/logs /app/snapshots \
    && chmod 700 /data

EXPOSE 5000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "camera_dashboard.py"]
