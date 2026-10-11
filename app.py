#!/usr/bin/env python3
"""Compatibility launcher for Camera Dashboard.

The real application file is camera_dashboard.py. Platforms may run either
`python camera_dashboard.py` directly or `python app.py`.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "camera_dashboard.py"
if not SCRIPT.exists():
    raise SystemExit(f"[Camera Dashboard] Missing {SCRIPT.name}")

# Keep platform environment variables intact; camera_dashboard.py reads PORT,
# CAMERA_DATA_DIR, CAMERA_WORKERS and related settings itself.
os.execv(sys.executable, [sys.executable, str(SCRIPT), *sys.argv[1:]])
