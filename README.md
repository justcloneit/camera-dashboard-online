The application file is intentionally named **`camera_dashboard.py`**.
You can run the same file locally, on Replit, Railway, Render, Fly.io,
Docker, Docker Compose, or a Linux VPS.

## Files
- `camera_dashboard.py` — main Camera Dashboard v161 application
- `app.py` — optional compatibility launcher; it simply execs `camera_dashboard.py`
- `requirements.txt` — Python dependencies
- `Dockerfile` — Python 3.12 + FFmpeg/ffprobe + tini
- `docker-compose.yml` — local/VPS Docker deployment
- `Procfile` — platforms using Procfile
- `render.yaml` — Render deployment
- `railway.toml` — Railway deployment
- `fly.toml` — Fly.io deployment
- `.replit` — Replit run/deployment helper
- `runtime.txt` — Python runtime
- `.dockerignore` — excludes runtime database/log files from the image

## Run directly
```bash
python camera_dashboard.py --port 5000
```

For hosted environments:
```bash
python camera_dashboard.py
```
The application reads `PORT` automatically.

## Persistent storage
The dashboard uses SQLite, backups, logs, snapshots and configuration.
For production hosting, mount persistent storage at `/data` and set:
```text
CAMERA_DATA_DIR=/data
```

Do not put camera passwords or Telegram secrets into deployment files.

## FFmpeg
The Docker image installs both `ffmpeg` and `ffprobe` for RTSP validation
and stream handling.

## Health endpoints
- `/healthz`
- `/readyz`

## Replit
Run:
```bash
python camera_dashboard.py
```
Replit supplies `PORT`.

## Railway
The included Railway configuration starts:
```bash
python camera_dashboard.py
```
Use a persistent Railway Volume mounted at `/data` if database/snapshots
must survive redeploys.

## Render
The included configuration uses the Dockerfile. Add a persistent disk
mounted at `/data` when SQLite/snapshots must survive redeploys.

## Fly.io
Change the `app` name in `fly.toml`, create the `/data` volume, then deploy:
```bash
fly deploy
```

## Docker
```bash
docker compose up -d --build
```

## Network scope
The dashboard does not enable public-range scanning. It works with your
authorized inventory and configured camera/NVR targets.
