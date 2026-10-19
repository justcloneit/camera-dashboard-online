#!/usr/bin/env python3
"""
Camera Dashboard v204.0 — Recheck Error Message Fix
==========================================================
Database · Discovery · Validation · Streaming · Telegram · Browser UI

Usage:
    python3 camera_dashboard.py [--port $PORT] [--host 0.0.0.0] [--dir .]
    camera_dashboard.exe
"""

# ── Standard library ──────────────────────────────────────────────────────────
import argparse
import atexit
import csv
import errno
import copy
import hashlib
import hmac
import io
import json
import logging
import logging.handlers
import os
import re
import shutil
import socket
import select
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
import signal
import gzip
# Cross-platform advisory process locking: POSIX flock, Windows msvcrt byte lock.
if os.name == 'nt':
    import msvcrt
else:
    import fcntl
import tempfile
import uuid
import zipfile
import platform
import sysconfig
import base64
import statistics
import secrets
import random

# ── Self-healing dependency bootstrap ─────────────────────────────────────────
# The script remains deployable when a platform omits requirements.txt. It only
# invokes pip when one or more required imports are missing.
_REQUIRED_PYTHON_PACKAGES = (
    ('flask', 'Flask'),
    ('flask_sock', 'flask-sock'),
    ('requests', 'requests'),
    ('cryptography', 'cryptography'),
)

def _missing_required_packages():
    import importlib.util
    missing = []
    for module_name, package_name in _REQUIRED_PYTHON_PACKAGES:
        try:
            available = importlib.util.find_spec(module_name) is not None
        except (ImportError, ValueError, ModuleNotFoundError):
            available = False
        if not available:
            missing.append((module_name, package_name))
    return missing

def _pip_install(arguments):
    command = [sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check'] + list(arguments)
    print('[Dependencies] Installing missing Python packages: ' + ' '.join(arguments), flush=True)
    try:
        result = subprocess.run(command, check=False)
        return result.returncode == 0
    except Exception as exc:
        print(f'[Dependencies] pip invocation failed: {type(exc).__name__}: {exc}', flush=True)
        return False

def _ensure_required_packages():
    missing = _missing_required_packages()
    if not missing:
        return

    print('[Dependencies] Missing: ' + ', '.join(package for _, package in missing), flush=True)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    requirements_path = os.path.join(script_dir, 'requirements.txt')

    # Prefer the deployment's supplied requirements file when available. If it
    # is absent, unreadable, or incomplete, fall back to the embedded manifest.
    installed_from_file = False
    if os.path.isfile(requirements_path):
        print(f'[Dependencies] Found {requirements_path}; trying it first.', flush=True)
        installed_from_file = _pip_install(['-r', requirements_path])

    missing = _missing_required_packages()
    if missing:
        fallback_packages = [package for _, package in missing]
        if not _pip_install(fallback_packages):
            # Some minimal Python images ship without pip. Try enabling it once.
            try:
                bootstrap = subprocess.run(
                    [sys.executable, '-m', 'ensurepip', '--upgrade'],
                    check=False,
                )
            except Exception:
                bootstrap = None
            if bootstrap is not None and bootstrap.returncode == 0:
                _pip_install(fallback_packages)

    missing = _missing_required_packages()
    if missing:
        names = ', '.join(package for _, package in missing)
        reason = 'The platform may block package installation or have no internet access.'
        raise SystemExit(
            f'\n[Dependencies] ERROR: required packages still missing: {names}.\n'
            f'Automatic installation failed. {reason}\n'
            f'If your platform supports it, provide requirements.txt with: '
            f'Flask, flask-sock, requests, cryptography. Then restart the app.'
        )
    print('[Dependencies] Required Python packages are ready.', flush=True)

try:
    _ensure_required_packages()
except SystemExit:
    raise
except Exception as _dependency_bootstrap_error:
    raise SystemExit(
        f'[Dependencies] Automatic dependency setup failed: '
        f'{type(_dependency_bootstrap_error).__name__}: {_dependency_bootstrap_error}'
    )

try:
    from cryptography.fernet import Fernet, InvalidToken
except Exception:  # optional until Telegram secure storage is used
    Fernet = None
    InvalidToken = Exception
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, quote, unquote, urljoin
from xml.etree import ElementTree


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime.

    Use this instead of the deprecated datetime.utcnow(). Keeping UTC
    timezone-aware internally avoids ambiguous/naive datetime values while
    remaining compatible with Python versions that provide timezone.utc.
    """
    return datetime.now(timezone.utc)


def utc_iso(timespec: str | None = None) -> str:
    """Return an ISO-8601 UTC timestamp with a trailing Z.

    Examples:
        2026-09-30T07:54:12Z
        2026-09-30T07:54:12.123456Z
    """
    value = utc_now()
    if timespec is not None:
        value = value.replace(microsecond=0) if timespec == 'seconds' else value
        text = value.isoformat(timespec=timespec)
    else:
        text = value.isoformat()
    return text.replace('+00:00', 'Z')

# ── Third-party ───────────────────────────────────────────────────────────────
try:
    from werkzeug.middleware.proxy_fix import ProxyFix
except ImportError:
    ProxyFix = None
try:
    from flask import (Flask, Response, jsonify, render_template_string,
                       request, send_from_directory, session, redirect,
                       url_for, has_request_context, g)
except ImportError:
    sys.exit('[!] Flask not found.  Run: pip install flask flask-sock requests')
try:
    from flask_sock import Sock
except ImportError:
    sys.exit('[!] flask-sock not found. Install dependencies with: pip install -r requirements.txt (requires flask-sock)')
try:
    import requests as _requests
except ImportError:
    _requests = None


# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

VERSION = '204.0'
SCHEMA_VERSION = 140
SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
# Default runtime/data location is the project workspace itself.
# Set CAMERA_DATA_DIR only when a different location is intentionally desired.
def _default_data_dir() -> str:
    configured = os.getenv('CAMERA_DATA_DIR', '').strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    if os.name == 'nt':
        base = os.getenv('LOCALAPPDATA') or os.path.expanduser('~')
        return os.path.join(base, 'CameraDashboard', 'data')
    base = os.getenv('XDG_STATE_HOME', '').strip() or os.path.join(os.path.expanduser('~'), '.local', 'state')
    # Hosted workspaces often have a persistent mounted workspace and rely on
    # legacy DB adoption. Their explicit data dir remains supported.
    if any(os.getenv(k) for k in ('REPL_ID','REPLIT_DEPLOYMENT','RAILWAY_PROJECT_ID','RAILWAY_ENVIRONMENT')):
        return SCRIPT_DIR
    return os.path.join(base, 'camera-dashboard')

DATA_DIR     = _default_data_dir()
# Keep runtime files (DB, snapshots, backups, Telegram config) private by default.
try:
    os.umask(0o077)
except Exception:
    pass
SNAPSHOT_DIR  = os.path.join(DATA_DIR, 'snapshots')
LOG_DIR       = os.path.join(DATA_DIR, 'logs')
DB_PATH       = os.path.join(DATA_DIR, 'camera_db.sqlite')
BACKGROUND_SERVICES_CONFIG_PATH = os.path.join(DATA_DIR, 'background_services.json')

# Single-process guard: only one Camera Dashboard process may use this DB.
# SQLite/WAL is safe for multiple readers, but this application intentionally
# owns the DB for validation, backups, discovery, and background maintenance.
_PID_LOCK_PATH = DB_PATH + '.pid'
_pid_lock_handle = None

def _release_pid_lock() -> None:
    global _pid_lock_handle
    handle = _pid_lock_handle
    _pid_lock_handle = None
    if handle is None:
        return
    try:
        if os.name == 'nt':
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        handle.close()
    except Exception:
        pass

def _acquire_pid_lock() -> None:
    """Refuse startup if another dashboard process already owns this DB."""
    global _PID_LOCK_PATH, _pid_lock_handle
    _PID_LOCK_PATH = DB_PATH + '.pid'
    # Use a binary handle on Windows because msvcrt.locking locks byte ranges.
    handle = open(_PID_LOCK_PATH, 'a+b' if os.name == 'nt' else 'a+', encoding=None if os.name == 'nt' else 'utf-8')
    try:
        if os.name == 'nt':
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b' '); handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        try:
            handle.seek(0)
            other = handle.read().strip() or 'unknown'
        except Exception:
            other = 'unknown'
        try:
            handle.close()
        except Exception:
            pass
        log.critical('[DB Lock] Another camera_dashboard.py (PID %s) is already using %s',
                     other, DB_PATH)
        raise SystemExit(
            f'Refusing to start: PID {other} already holds the database lock at {_PID_LOCK_PATH}'
        )
    except Exception:
        try:
            handle.close()
        except Exception:
            pass
        raise

    # The OS advisory lock is authoritative; PID text is only diagnostic.
    # A hosted workspace may have a per-project quota exhausted while `df -h`
    # still shows free filesystem blocks. Do not abort startup merely because
    # this optional metadata write fails after the lock was successfully taken.
    try:
        handle.seek(0)
        handle.truncate()
        payload = str(os.getpid()).encode('ascii') if os.name == 'nt' else str(os.getpid())
        handle.write(payload)
        handle.flush()
    except OSError as exc:
        try:
            print(f'[DB Lock] Advisory lock acquired; PID metadata could not be written: {exc}',
                  file=sys.stderr, flush=True)
        except Exception:
            pass
    _pid_lock_handle = handle
    atexit.register(_release_pid_lock)
    log.info('[DB Lock] Acquired exclusive PID lock: %s (PID %d)',
             _PID_LOCK_PATH, os.getpid())

# Keep exactly one verified persistent backup. Older .bak.* files are cleaned
# after a successful backup/maintenance operation.
DB_BACKUPS    = [DB_PATH + '.bak']
DB_BACKUP_GLOB_PREFIX = DB_PATH + '.bak'
# Canonical default: /home/runner/workspace when the script is run there.
# The application no longer auto-adopts /data/camera_db.sqlite.
TG_CONFIG     = os.path.join(DATA_DIR, 'telegram_config.json')
TG_KEY_FILE  = os.path.join(DATA_DIR, '.camera_dashboard_master.key')
TG_DB_KEY    = 'telegram.secure.v1'
RTSP_SAVE     = os.path.join(DATA_DIR, 'generated_rtsp_links.txt')
MIN_FREE_BYTES = max(64*1024*1024, int(os.getenv('CAMERA_MIN_FREE_BYTES', str(512*1024*1024))))
CRITICAL_FREE_BYTES = max(32*1024*1024, int(os.getenv('CAMERA_CRITICAL_FREE_BYTES', str(256*1024*1024))))
SCAN_BATCH_SIZE = max(100, min(5000, int(os.getenv('CAMERA_SCAN_BATCH_SIZE', '500'))))
PAGE_SIZE     = 200
MAX_STREAMS   = max(1, min(256, int(os.getenv('CAMERA_MAX_LIVE_STREAMS', '36'))))
# User-tunable capacity. These are deliberately independent: validation can
# process a large inventory while live viewing remains bounded by stream/CPU
# resources. CLI values are applied in main() before the server starts.
VALIDATION_BATCH_OVERRIDE = max(0, min(10000, int(os.getenv('CAMERA_VALIDATION_BATCH', '0') or 0)))
MAX_MULTI_VIEW = max(1, min(256, int(os.getenv('CAMERA_MAX_MULTI_VIEW', '64'))))
ADVANCED_HEALTH_INTERVAL = max(15, int(os.getenv('CAMERA_HEALTH_INTERVAL_SECONDS', '60')))

STREAM_RECOVERY_ATTEMPTS = max(0, min(2, int(os.getenv('CAMERA_STREAM_RECOVERY_ATTEMPTS', '1'))))
STREAM_MAX_ATTEMPTS = max(4, min(32, int(os.getenv('CAMERA_STREAM_MAX_ATTEMPTS', '24'))))
VALIDATION_PROBE_CONCURRENCY = max(1, min(64, int(os.getenv('CAMERA_VALIDATION_PROBE_CONCURRENCY', '8'))))
STREAM_MAX_WIDTH = max(0, min(3840, int(os.getenv('CAMERA_STREAM_MAX_WIDTH', '1280'))))
STREAM_MAX_BUFFER = max(4 * 1024 * 1024, int(os.getenv('CAMERA_STREAM_MAX_BUFFER', str(16 * 1024 * 1024))))
RTSP_API_TOKEN = os.getenv('CAMERA_RTSP_API_TOKEN', '').strip()
TELEGRAM_SNAPSHOT_ALERTS = os.getenv('CAMERA_TELEGRAM_SNAPSHOT_ALERTS', '1').strip().lower() in {'1','true','yes','on'}
DELETED_RETENTION_DAYS = max(
    1, int(os.getenv('CAMERA_DELETED_RETENTION_DAYS', '30')))
SNAPSHOT_RETENTION_DAYS = max(
    1, int(os.getenv('CAMERA_SNAPSHOT_RETENTION_DAYS', '14')))
HISTORY_RETENTION_DAYS = max(
    7, int(os.getenv('CAMERA_HISTORY_RETENTION_DAYS', '30')))
MAX_HISTORY_ROWS = max(
    10000, int(os.getenv('CAMERA_MAX_HISTORY_ROWS', '2000000')))
AUTH_USER     = os.getenv('CAMERA_ADMIN_USER', '').strip()
AUTH_PASSWORD = os.getenv('CAMERA_ADMIN_PASSWORD', '')
READONLY_USER = os.getenv('CAMERA_READONLY_USER', '').strip()
READONLY_PASSWORD = os.getenv('CAMERA_READONLY_PASSWORD', '')
AUTH_ENABLED_CONFIG = os.getenv('CAMERA_AUTH_ENABLED', '').strip().lower() in {'1','true','yes','on'}
SESSION_TIMEOUT_SECONDS = max(300, int(os.getenv('CAMERA_SESSION_TIMEOUT_SECONDS', '28800')))
SECURE_COOKIE = os.getenv('CAMERA_SECURE_COOKIE', '').strip().lower() in {'1','true','yes','on'}
CAMERA_VERIFY_TLS = os.getenv('CAMERA_VERIFY_TLS', '1').strip().lower() in {'1','true','yes','on'}
_V43_HTTP_VERIFY_TLS = CAMERA_VERIFY_TLS
LOGIN_MAX_FAILURES = max(3, int(os.getenv('CAMERA_LOGIN_MAX_FAILURES', '5')))
LOGIN_WINDOW_SECONDS = max(60, int(os.getenv('CAMERA_LOGIN_WINDOW_SECONDS', '300')))
LOGIN_LOCKOUT_SECONDS = max(60, int(os.getenv('CAMERA_LOGIN_LOCKOUT_SECONDS', '900')))

# Granular dashboard permissions. Admin always has every permission. Database
# users may be assigned a custom subset from the Security panel.
AUTH_PERMISSION_DEFS = {
    'dashboard.view': 'View dashboard',
    'stream.view': 'View/play live streams',
    'camera.control': 'Run camera tests, discovery and stream controls',
    'camera.manage': 'Add, edit, delete, transfer and organize cameras',
    'diagnostics.run': 'Run diagnostics and device checks',
    'reports.view': 'View device and health reports',
    'health.view': 'View health and performance data',
    'alerts.manage': 'View/test/manage alerts and Telegram actions',
    'database.manage': 'Backup, restore, integrity and maintenance',
    'settings.manage': 'Change application settings and saved layouts',
    'security.view': 'View security status',
    'security.manage': 'Manage users, sessions and login lockouts',
}
AUTH_ROLE_DEFAULTS = {
    'admin': list(AUTH_PERMISSION_DEFS),
    'readonly': ['dashboard.view', 'stream.view'],
    'operator': ['dashboard.view', 'stream.view', 'alerts.manage', 'camera.control', 'camera.manage', 'reports.view', 'diagnostics.run'],
    'custom': [],
}
AUTH_DB_ITERATIONS = max(600000, int(os.getenv('CAMERA_AUTH_ITERATIONS', '600000')))
SESSION_ACTIVE_WINDOW_SECONDS = max(60, min(SESSION_TIMEOUT_SECONDS, 1800))

_login_attempts = {}
_login_attempts_lock = threading.RLock()
# A separate per-client guard is deliberately NOT cleared by the user-lockout
# controls. This prevents an administrator from accidentally removing the
# protection that throttles repeated attacks from one source IP.
_login_ip_guard = {}
# Authentication epoch is persisted in SQLite so a normal application restart
# does not invalidate every browser session. It is incremented only by an
# explicit "logout all sessions" action (or another deliberate security reset).
_auth_epoch = 1
_auth_epoch_lock = threading.Lock()
# One-time compatibility window for v83 sessions created before the epoch was
# persisted. It is disabled immediately after an explicit logout-all.
_AUTH_EPOCH_MIGRATION_GRACE = False
# Explicit, bounded TCP candidates for one user-supplied device host. The
# user-facing scan prioritizes the common web ports, then checks the complete
# requested list. This never expands to neighboring hosts or CIDR ranges.
DEVICE_HTTP_PORTS = (
    80, 8080, 23, 81, 82, 83, 443, 554, 1024, 1025, 1050,
    2000, 2001, 3000, 4000, 4321, 4567, 5060, 5554, 6789, 7080,
    7554, 8000, 8081, 8082, 8083, 8084, 8085, 8090, 8091, 8200,
    8443, 8554, 8888, 8899, 9000, 9001, 9527, 9999, 10554, 18080,
    18081, 34567, 37777, 37778, 37779, 50000, 50001,
)
DEVICE_HTTPS_PORTS = {443, 8443}
DEVICE_RTSP_PORTS = {554, 5554, 7554, 8554, 10554}
DEVICE_PORT_SCAN_TIMEOUT = 0.35
DEVICE_HTTP_PROBE_TIMEOUT = 1.25
DEVICE_DISCOVERY_TIMEOUT = 2.0
DEVICE_MAX_PROBE_WORKERS = 8

# ── Adaptive resource controller ─────────────────────────────────────────────
# Validation is network/FFmpeg heavy. A fixed worker count is suboptimal across
# Replit/free/shared VMs, small VPSs, and powerful servers, so the dashboard
# derives a safe worker budget from CPU + available RAM and re-evaluates it for
# every validation batch. CAMERA_WORKERS may be an integer to pin the value,
# or "auto" (default) to enable adaptive mode.
_WORKER_LOCK = threading.RLock()
_WORKER_STATE = {
    'mode': os.getenv('CAMERA_WORKERS', 'auto').strip().lower() or 'auto',
    'workers': 0,
    'batch_limit': 0,
    'cpu_count': 0,
    'ram_total_gb': 0.0,
    'ram_available_gb': 0.0,
    'load1': 0.0,
    'updated': 0.0,
}


def _resource_snapshot() -> dict:
    cpu = max(1, int(os.cpu_count() or 1))
    total = avail = 0
    # Prefer psutil if already installed; never require another dependency.
    try:
        import psutil  # type: ignore
        vm = psutil.virtual_memory()
        total, avail = int(vm.total), int(vm.available)
        load1 = float(psutil.getloadavg()[0]) if hasattr(psutil, 'getloadavg') else 0.0
    except Exception:
        load1 = 0.0
        try:
            if sys.platform.startswith('linux'):
                mem = Path('/proc/meminfo').read_text(errors='ignore')
                vals = {}
                for line in mem.splitlines():
                    m = re.match(r'^(MemTotal|MemAvailable):\s+(\d+)\s+kB', line)
                    if m:
                        vals[m.group(1)] = int(m.group(2)) * 1024
                total, avail = vals.get('MemTotal', 0), vals.get('MemAvailable', 0)
                if hasattr(os, 'getloadavg'):
                    load1 = float(os.getloadavg()[0])
            else:
                total = int(getattr(os, 'sysconf', lambda *_: 0)('SC_PAGE_SIZE') *
                            getattr(os, 'sysconf', lambda *_: 0)('SC_PHYS_PAGES'))
                avail = total
        except Exception:
            pass
    if not total:
        total = max(avail, 512 * 1024 * 1024)
    if not avail:
        avail = total
    return {
        'cpu_count': cpu,
        'ram_total_gb': round(total / (1024 ** 3), 2),
        'ram_available_gb': round(avail / (1024 ** 3), 2),
        'load1': round(load1, 2),
    }


def _auto_validation_workers() -> int:
    snap = _resource_snapshot()
    mode = _WORKER_STATE.get('mode', 'auto')
    # Explicit --workers/CAMERA_WORKERS is now respected up to 64.  Only
    # automatic mode applies the conservative CPU/RAM budget.  This lets a
    # user deliberately run 16/32/60 workers on a stronger machine while the
    # safe default remains conservative on a 4-vCPU Replit VM.
    MAX_VALIDATION_WORKERS = 64
    if mode.isdigit():
        requested = max(1, min(MAX_VALIDATION_WORKERS, int(mode)))
        workers = requested
    else:
        cpu = snap['cpu_count']
        avail_gb = max(0.5, snap['ram_available_gb'])
        auto_cap = min(12, max(4, cpu * 2))
        by_ram = max(4, min(auto_cap, int((avail_gb * 1024) / 512)))
        workers = min(auto_cap, by_ram)
        if snap['load1'] > cpu * 0.85:
            workers = max(2, int(workers * 0.60))
        elif snap['load1'] > cpu * 0.65:
            workers = max(2, int(workers * 0.75))
    workers = max(1, min(MAX_VALIDATION_WORKERS, int(workers)))
    # An explicit --validation-batch/CAMERA_VALIDATION_BATCH is a HARD QUEUE
    # CLAIM LIMIT and is deliberately independent of the worker count.  In
    # particular, changing auto workers (for example 8 -> 3 under load) must
    # never silently turn a requested batch of 100 into 30.
    #
    # Note: a claim can still legitimately return fewer rows when fewer than
    # the requested number are currently eligible (next_attempt reached and
    # lease unlocked).  The queue worker logs requested vs claimed so this is
    # distinguishable from a configuration change.
    if VALIDATION_BATCH_OVERRIDE:
        batch = max(1, min(10000, int(VALIDATION_BATCH_OVERRIDE)))
    else:
        batch = min(2000, max(120, workers * 10))
    with _WORKER_LOCK:
        _WORKER_STATE.update({**snap, 'workers': workers, 'batch_limit': batch,
                              'updated': time.time()})
    return workers


def _validation_worker_config() -> tuple[int, int]:
    workers = _auto_validation_workers()
    with _WORKER_LOCK:
        return workers, int(_WORKER_STATE.get('batch_limit') or min(2000, max(100, workers * 10)))


# Directories / files to skip during discovery
_SKIP_DIRS  = {'.git', '.local', '.agents', '__pycache__', 'node_modules',
               '.cache', 'snapshots', 'logs', '.upm', 'replit', '.pythonlibs',
               'dist', 'build', '.venv', 'venv', 'env'}
_SKIP_FILES = {
    'camera_db.sqlite', 'requirements.txt', 'generated_rtsp_links.txt',
    'telegram_config.json', 'package.json', 'package-lock.json',
    'credentials.txt', os.path.basename(__file__),
}
# Device types that are NOT cameras
_SKIP_TYPES = {'router', 'switch', 'modem', 'access point', 'access_point',
               'firewall', 'printer', 'nas', 'server'}

# Global mutable: search root (overridable via --dir)
_SEARCH_DIR: str = SCRIPT_DIR
_RUNTIME_SKIP_FILES = {'Live.txt'}

os.makedirs(SNAPSHOT_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)


# ══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════════════

class QuotaSafeStreamHandler(logging.StreamHandler):
    """Console handler that safely degrades if the log sink rejects writes."""
    def __init__(self, stream=None):
        super().__init__(stream)
        self._disabled_after_oserror = False
        self._warning_emitted = False

    def emit(self, record):
        if self._disabled_after_oserror:
            return
        try:
            super().emit(record)
        except OSError as exc:
            self._disabled_after_oserror = True
            if not self._warning_emitted:
                self._warning_emitted = True
                try:
                    sys.__stderr__.write(
                        f'[Logging] Console handler disabled after {type(exc).__name__}: {exc}\\n')
                    sys.__stderr__.flush()
                except Exception:
                    pass

    def handleError(self, record):
        exc_info = sys.exc_info()
        exc = exc_info[1] if exc_info else None
        if isinstance(exc, OSError):
            self._disabled_after_oserror = True
            return
        self._disabled_after_oserror = True


class QuotaSafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Disable file logging after a filesystem/quota error without breaking the app.

    Python's default logging error handler prints a large traceback for every log
    event when rotation fails with EDQUOT/ENOSPC. On hosted platforms this can
    flood the console while the actual application remains healthy. Keep console
    logging active, emit one concise warning, and stop retrying the failing file.
    """
    def __init__(self, *args, **kwargs):
        self._file_logging_disabled = False
        self._quota_warning_emitted = False
        super().__init__(*args, **kwargs)

    def emit(self, record):
        if self._file_logging_disabled:
            return
        try:
            super().emit(record)
        except OSError as exc:
            self._disable_file_logging(exc)

    def handleError(self, record):
        exc_info = sys.exc_info()
        exc = exc_info[1] if exc_info else None
        if isinstance(exc, OSError):
            self._disable_file_logging(exc)
            return
        # Do not allow a logging backend problem to cascade into app requests.
        self._disable_file_logging(exc or RuntimeError('unknown logging handler error'))

    def _disable_file_logging(self, exc):
        self._file_logging_disabled = True
        try:
            if self.stream:
                self.stream.close()
        except Exception:
            pass
        self.stream = None
        if not self._quota_warning_emitted:
            self._quota_warning_emitted = True
            try:
                print(f'[Logging] File logging disabled for {self.baseFilename}: '
                      f'{type(exc).__name__}: {exc}. Console logging remains active.',
                      file=sys.stderr, flush=True)
            except Exception:
                pass


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-8s %(name)s %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        QuotaSafeStreamHandler(),
        QuotaSafeRotatingFileHandler(
            os.path.join(LOG_DIR, 'dashboard.log'),
            maxBytes=5_000_000, backupCount=3, encoding='utf-8'),
    ],
)
log = logging.getLogger('camera_dashboard')

_LIFECYCLE_PATH = os.path.join(LOG_DIR, 'lifecycle.jsonl')
def _lifecycle_event(event: str, reason: str = '') -> None:
    """Append a small durable lifecycle record and mirror critical events to console."""
    record = {'time': utc_iso('seconds'), 'event': event, 'reason': str(reason)[:500],
              'pid': os.getpid(), 'version': VERSION, 'platform': platform.platform()}
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(_LIFECYCLE_PATH, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + '\n')
            fh.flush(); os.fsync(fh.fileno())
    except Exception as exc:
        print(f'[Lifecycle] Could not persist {event}: {exc}', file=sys.stderr, flush=True)
    if event in {'startup','unclean_exit','shutdown'}:
        print(f'[Lifecycle] {event}: {record["reason"] or "no reason supplied"}', flush=True)

stream_error_log = logging.getLogger('stream_errors')
stream_error_log.setLevel(logging.INFO)
stream_error_log.propagate = False
if not stream_error_log.handlers:
    stream_error_log.addHandler(QuotaSafeRotatingFileHandler(
        os.path.join(LOG_DIR, 'stream_errors.log'),
        maxBytes=5_000_000, backupCount=3, encoding='utf-8'))


# ══════════════════════════════════════════════════════════════════════════════
# FLASK APP
# ══════════════════════════════════════════════════════════════════════════════

app  = Flask(__name__)
if ProxyFix is not None:
    # Hosted deployments such as Replit terminate HTTPS at a reverse proxy.
    # Trust one proxy hop so request.is_secure reflects the public scheme.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.getenv('SESSION_SECRET') or os.urandom(24)
if not os.getenv('SESSION_SECRET'):
    log.warning('SESSION_SECRET is not set; sessions will be invalidated on restart. Set a persistent CAMERA/SESSION secret for production.')
app.config.update(
    # Configurable request upload cap: defaults to 200 MiB and is capped at 200 MiB.
    # Set CAMERA_MAX_UPLOAD_BYTES to a smaller positive byte count to lower the limit.
    MAX_CONTENT_LENGTH=max(
        1 * 1024 * 1024,
        min(
            200 * 1024 * 1024,
            int(os.getenv('CAMERA_MAX_UPLOAD_BYTES', str(200 * 1024 * 1024))),
        ),
    ),
    MAX_FORM_MEMORY_SIZE=2 * 1024 * 1024,
    SESSION_COOKIE_NAME='camera_dashboard_session',
    SESSION_COOKIE_PATH='/',
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE=('None' if SECURE_COOKIE else 'Lax'),
    SESSION_COOKIE_SECURE=SECURE_COOKIE,
    PERMANENT_SESSION_LIFETIME=timedelta(seconds=SESSION_TIMEOUT_SECONDS),
)
# Replit HTTPS previews are handled by the signed compatibility cookie above;
# keep the primary Flask cookie settings unchanged for backward compatibility.
sock = Sock(app)

# Active ffmpeg stream subprocesses: cam_id → subprocess.Popen
_streams: dict = {}  # cam_id -> set(Popen)
_stream_proc_owners: dict = {}  # Popen -> authenticated username/session owner
_stream_subscribers: dict = {}
_streams_lock: threading.Lock = threading.Lock()
_worker_heartbeat: dict[str, dict] = {}
_worker_heartbeat_lock: threading.Lock = threading.Lock()

def _touch_worker(name: str, state: str = 'running', detail: str = '') -> None:
    key=str(name); new_state=str(state or 'unknown')
    with _worker_heartbeat_lock:
        previous=_worker_heartbeat.get(key,{}).get('state')
        _worker_heartbeat[key] = {'last_seen':time.time(),'state':new_state,'detail':str(detail or '')[:240]}
    if previous != new_state and key != 'worker-supervisor':
        _record_worker_event(key,new_state,detail)

def _worker_health_snapshot() -> dict:
    now = time.time()
    with _worker_heartbeat_lock:
        return {
            name: {
                'state': value.get('state', 'unknown'),
                'detail': value.get('detail', ''),
                'age_seconds': round(max(0.0, now - float(value.get('last_seen') or now)), 1),
                'stale': max(0.0, now - float(value.get('last_seen') or now)) > 180,
            }
            for name, value in _worker_heartbeat.items()
        }

# Backward-compatible alias for older route names.
_worker_health = _worker_health_snapshot


# v154 Persistent Background Service Controls.
# These switches are stored in a tiny JSON preferences file, not SQLite.
# Turning a service OFF therefore survives application restarts until the
# operator explicitly turns it back ON. Camera data and DB schema are untouched.
_BACKGROUND_SERVICES = {
    'advanced-metadata': {'label': 'Advanced Metadata Sync', 'enabled': True, 'kind': 'worker'},
    'audit-log': {'label': 'Audit Log Writes', 'enabled': True, 'kind': 'write'},
    'health-supervisor': {'label': 'Health Supervisor', 'enabled': True, 'kind': 'worker'},
    'schedule-supervisor': {'label': 'Schedule Supervisor', 'enabled': True, 'kind': 'worker'},
    'validation-queue': {'label': 'Validation Queue', 'enabled': True, 'kind': 'worker'},
    'file-scanner': {'label': 'Background File Scanner', 'enabled': True, 'kind': 'worker'},
    'database-backup': {'label': 'Verified Database Backup', 'enabled': True, 'kind': 'worker'},
    'feature-supervisor': {'label': 'Feature/Health Maintenance', 'enabled': True, 'kind': 'worker'},
    'worker-supervisor': {'label': 'Worker Auto-Restart Supervisor', 'enabled': True, 'kind': 'worker'},
    'full-integrity': {'label': 'Background Full Integrity Check', 'enabled': True, 'kind': 'worker'},
    'inventory-resume': {'label': 'Inventory Job Resume', 'enabled': True, 'kind': 'worker'},
}
_BACKGROUND_SERVICES_LOCK = threading.RLock()
_BACKGROUND_SERVICE_THREADS = {}

# v155: debounce JSON preference writes so rapid UI toggles are coalesced into
# one small atomic file update.  The in-memory state changes immediately; the
# JSON file is flushed about 5 seconds after the last change.  If no service
# setting changes, this writer does nothing and never touches the file.
_BACKGROUND_SERVICE_PERSIST_DELAY = max(5.0, float(os.getenv('CAMERA_BACKGROUND_SERVICE_PERSIST_DELAY_SECONDS', '5')))
_BACKGROUND_SERVICE_PERSIST_LOCK = threading.RLock()
_BACKGROUND_SERVICE_PERSIST_EVENT = threading.Event()
_BACKGROUND_SERVICE_PERSIST_DIRTY = False
_BACKGROUND_SERVICE_PERSIST_THREAD = None

def _load_persistent_background_service_states() -> None:
    """Load operator ON/OFF preferences without ever failing startup."""
    try:
        if not os.path.exists(BACKGROUND_SERVICES_CONFIG_PATH):
            return
        with open(BACKGROUND_SERVICES_CONFIG_PATH, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        states = data.get('services', {}) if isinstance(data, dict) else {}
        if not isinstance(states, dict):
            return
        with _BACKGROUND_SERVICES_LOCK:
            for name, item in _BACKGROUND_SERVICES.items():
                if name in states and isinstance(states[name], dict) and 'enabled' in states[name]:
                    item['enabled'] = bool(states[name].get('enabled'))
        disabled = [name for name, item in _BACKGROUND_SERVICES.items() if not item.get('enabled', True)]
        if disabled:
            log.info('[Services] Persistent OFF preferences loaded: %s', ', '.join(disabled))
    except Exception as exc:
        log.warning('[Services] Could not load persistent preferences; using defaults: %s', exc)

def _save_persistent_background_service_states() -> bool:
    """Atomically persist only background-service ON/OFF preferences."""
    tmp_path = None
    try:
        with _BACKGROUND_SERVICES_LOCK:
            payload = {
                'version': 1,
                'updated_at': utc_iso(),
                'services': {
                    name: {'enabled': bool(item.get('enabled', True))}
                    for name, item in _BACKGROUND_SERVICES.items()
                },
            }
        target_dir = os.path.dirname(BACKGROUND_SERVICES_CONFIG_PATH) or '.'
        os.makedirs(target_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix='.background_services.', suffix='.tmp',
                                        dir=target_dir)
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write('\n')
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, BACKGROUND_SERVICES_CONFIG_PATH)
        tmp_path = None
        log.info('[Services] Persisted background service preferences: %s', BACKGROUND_SERVICES_CONFIG_PATH)
        return True
    except Exception as exc:
        # A preferences failure must never affect camera/DB operation.
        log.warning('[Services] Could not persist background service preferences: %s', exc)
        return False
    finally:
        if tmp_path:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass

def _background_service_persist_worker() -> None:
    """Flush dirty service preferences after a quiet debounce interval."""
    global _BACKGROUND_SERVICE_PERSIST_DIRTY
    while not _shutdown_requested.is_set():
        _BACKGROUND_SERVICE_PERSIST_EVENT.wait(1.0)
        if _shutdown_requested.is_set():
            break
        if not _BACKGROUND_SERVICE_PERSIST_EVENT.is_set():
            continue
        # Coalesce rapid ON/OFF changes.
        _BACKGROUND_SERVICE_PERSIST_EVENT.clear()
        if _shutdown_requested.wait(_BACKGROUND_SERVICE_PERSIST_DELAY):
            break
        with _BACKGROUND_SERVICE_PERSIST_LOCK:
            dirty = _BACKGROUND_SERVICE_PERSIST_DIRTY
        if not dirty:
            continue
        if _save_persistent_background_service_states():
            with _BACKGROUND_SERVICE_PERSIST_LOCK:
                _BACKGROUND_SERVICE_PERSIST_DIRTY = False
        else:
            # Retry later without losing the operator's requested state.
            _BACKGROUND_SERVICE_PERSIST_EVENT.set()

def _request_persistent_background_service_save() -> None:
    """Mark preferences dirty; the debounced writer performs the file update."""
    global _BACKGROUND_SERVICE_PERSIST_DIRTY, _BACKGROUND_SERVICE_PERSIST_THREAD
    with _BACKGROUND_SERVICE_PERSIST_LOCK:
        _BACKGROUND_SERVICE_PERSIST_DIRTY = True
        if _BACKGROUND_SERVICE_PERSIST_THREAD is None or not _BACKGROUND_SERVICE_PERSIST_THREAD.is_alive():
            _BACKGROUND_SERVICE_PERSIST_THREAD = threading.Thread(
                target=_background_service_persist_worker, daemon=True,
                name='background-service-prefs')
            _BACKGROUND_SERVICE_PERSIST_THREAD.start()
    _BACKGROUND_SERVICE_PERSIST_EVENT.set()
    log.info('[Services] Background service preference changed; JSON flush scheduled in %.1fs',
             _BACKGROUND_SERVICE_PERSIST_DELAY)

def _flush_persistent_background_service_states(force: bool = False) -> bool:
    """Flush pending preferences during shutdown or explicit forced flush."""
    global _BACKGROUND_SERVICE_PERSIST_DIRTY
    with _BACKGROUND_SERVICE_PERSIST_LOCK:
        dirty = _BACKGROUND_SERVICE_PERSIST_DIRTY
    if not dirty and not force:
        return True
    ok = _save_persistent_background_service_states()
    if ok:
        with _BACKGROUND_SERVICE_PERSIST_LOCK:
            _BACKGROUND_SERVICE_PERSIST_DIRTY = False
    return ok

_load_persistent_background_service_states()

def _service_enabled(name: str) -> bool:
    with _BACKGROUND_SERVICES_LOCK:
        item = _BACKGROUND_SERVICES.get(str(name))
        return bool(item is None or item.get('enabled', True))

def _service_set_enabled(name: str, enabled: bool) -> bool:
    with _BACKGROUND_SERVICES_LOCK:
        if str(name) not in _BACKGROUND_SERVICES:
            return False
        _BACKGROUND_SERVICES[str(name)]['enabled'] = bool(enabled)
    _request_persistent_background_service_save()
    return True

def _service_thread_alive(name: str) -> bool:
    t = _BACKGROUND_SERVICE_THREADS.get(str(name))
    return bool(t and t.is_alive())

def _service_register_thread(name: str, thread: threading.Thread) -> None:
    with _BACKGROUND_SERVICES_LOCK:
        _BACKGROUND_SERVICE_THREADS[str(name)] = thread

def _background_services_snapshot() -> dict:
    now=time.time()
    with _BACKGROUND_SERVICES_LOCK:
        out={}
        health=_worker_health_snapshot()
        for name,item in _BACKGROUND_SERVICES.items():
            enabled=bool(item.get('enabled', True))
            worker_map={
                'advanced-metadata':'startup-metadata',
                'health-supervisor':'health-supervisor',
                'schedule-supervisor':'schedule-supervisor',
                'validation-queue':'validation-queue',
                'file-scanner':'file-scanner',
                'database-backup':'db-backup',
                'feature-supervisor':'v43-feature-supervisor',
                'worker-supervisor':'worker-supervisor',
                'full-integrity':'db-integrity',
            }
            hb=health.get(worker_map.get(name,''),{})
            alive=_service_thread_alive(name)
            if not enabled:
                status='OFF'
            elif hb.get('state') in {'running','starting','waiting'} or alive:
                status='RUNNING'
            elif hb.get('state') == 'stopped':
                status='STOPPED'
            elif name == 'audit-log':
                status='ON'
            elif name == 'inventory-resume':
                status='ON'
            else:
                status='STARTING/IDLE'
            out[name]={
                'label':item['label'],'enabled':enabled,'status':status,
                'kind':item.get('kind','worker'),
                'heartbeat':hb,
                'thread_alive':alive,
            }
        return out



# ══════════════════════════════════════════════════════════════════════════════
# v61 RELIABILITY / RECOVERY SUPERVISOR
# ══════════════════════════════════════════════════════════════════════════════
_WORKER_RESTARTERS: dict[str, callable] = {}
_WORKER_RESTART_LOCK = threading.RLock()
_WORKER_SUPERVISOR_STARTED = False
_WORKER_SUPERVISOR_RESTARTS: dict[str, int] = {}
_WORKER_SUPERVISOR_COOLDOWN: dict[str, float] = {}
# Defensive fallback: keep supervisor restart history available even if
# an older generated/edited copy omitted the module-level definition.
globals().setdefault('_V62_WORKER_RESTART_HISTORY', {})
# Restart timestamps used by the supervisor's bounded restart budget.
# Keep this in-memory only; it is intentionally reset on process startup.
_V62_WORKER_RESTART_HISTORY: dict[str, list[float]] = {}


def _register_worker_restarter(name: str, fn) -> None:
    with _WORKER_RESTART_LOCK:
        _WORKER_RESTARTERS[str(name)] = fn


def _supervisor_snapshot() -> dict:
    with _WORKER_RESTART_LOCK:
        return {
            'restarts': dict(_WORKER_SUPERVISOR_RESTARTS),
            'cooldown': {k: round(max(0.0, v-time.time()), 1)
                         for k, v in _WORKER_SUPERVISOR_COOLDOWN.items()
                         if v > time.time()},
        }


def _worker_supervisor_loop(interval: float = 30.0) -> None:
    restart_history = globals().setdefault('_V62_WORKER_RESTART_HISTORY', {})
    _touch_worker('worker-supervisor', 'starting')
    while not _shutdown_requested.is_set():
        try:
            _touch_worker('worker-supervisor', 'running')
            now = time.time()
            health = _worker_health_snapshot()
            for name, info in health.items():
                if name == 'worker-supervisor' or name not in _WORKER_RESTARTERS:
                    continue
                # Do not restart intentionally idle/waiting workers. A worker is
                # considered stale only when it stopped heartbeating while active.
                if info.get('state') not in {'running','starting','error'}:
                    continue
                if not info.get('stale'):
                    continue
                with _WORKER_RESTART_LOCK:
                    if now < _WORKER_SUPERVISOR_COOLDOWN.get(name, 0):
                        continue
                    history = [ts for ts in list(restart_history.get(name, []))
                               if now - ts < V62_RESTART_WINDOW]
                    if len(history) >= V62_RESTART_LIMIT:
                        restart_history[name] = history
                        _advanced_audit('worker_restart_suppressed','worker',name,
                                        'restart budget exceeded')
                        _record_recovery_event('worker_restart','worker','suppressed',
                                               f'{name}: restart budget exceeded')
                        _WORKER_SUPERVISOR_COOLDOWN[name] = now + min(600, V62_RESTART_WINDOW)
                        continue
                    history.append(now)
                    restart_history[name] = history
                    _WORKER_SUPERVISOR_COOLDOWN[name] = now + 120
                try:
                    _WORKER_RESTARTERS[name]()
                    with _WORKER_RESTART_LOCK:
                        _WORKER_SUPERVISOR_RESTARTS[name] = _WORKER_SUPERVISOR_RESTARTS.get(name, 0) + 1
                    _advanced_audit('worker_restart','worker',name,'stale heartbeat auto-restart')
                    _record_recovery_event('worker_restart', name, 'ok', 'stale heartbeat')
                except Exception as exc:
                    log.exception('Worker supervisor restart failed for %s', name)
                    _record_recovery_event('worker_restart', name, 'failed', f'{type(exc).__name__}: {exc}')
        except Exception:
            log.exception('Worker supervisor loop failed')
        _shutdown_requested.wait(max(10.0, interval))
    _touch_worker('worker-supervisor', 'stopped')


def start_worker_supervisor(interval: float = 30.0) -> threading.Thread:
    global _WORKER_SUPERVISOR_STARTED
    with _WORKER_RESTART_LOCK:
        if _WORKER_SUPERVISOR_STARTED:
            return next((t for t in threading.enumerate() if t.name == 'worker-supervisor'), threading.current_thread())
        _WORKER_SUPERVISOR_STARTED = True
    t = threading.Thread(target=_worker_supervisor_loop, args=(interval,), daemon=True, name='worker-supervisor')
    t.start()
    return t

# Inventory jobs run outside the HTTP request so large imports cannot hold a
# Replit/proxy request open long enough to produce a 502 gateway response.
INVENTORY_WORKERS = max(1, min(8, int(os.getenv('CAMERA_INVENTORY_WORKERS', '4'))))
INVENTORY_MAX_RESULTS = max(1000, int(os.getenv('CAMERA_INVENTORY_MAX_RESULTS', '10000')))
_inventory_jobs_lock = threading.RLock()
_inventory_job_threads: dict[str, threading.Thread] = {}


def _auth_enabled() -> bool:
    # Explicit CAMERA_AUTH_ENABLED=1 is required when credentials are supplied.
    return bool(AUTH_ENABLED_CONFIG and AUTH_USER and AUTH_PASSWORD)

def _load_persistent_auth_epoch() -> int:
    """Load the authentication epoch without making startup dependent on it."""
    global _auth_epoch, _AUTH_EPOCH_MIGRATION_GRACE
    if not _auth_enabled():
        return _auth_epoch
    try:
        c = _db_conn()
        # app_settings already exists in the core schema and is safe to reuse
        # for one small application-wide security state value.
        row = c.execute(
            "SELECT value FROM app_settings WHERE key=?",
            ('security.auth_epoch',)
        ).fetchone()
        if row and str(row['value'] or '').strip().isdigit():
            value = max(1, int(row['value']))
        else:
            marker = c.execute("SELECT value FROM app_settings WHERE key='security.auth_epoch_migrated'").fetchone()
            _AUTH_EPOCH_MIGRATION_GRACE = not (marker and str(marker['value'] or '') == '1')
            # v83 did not persist the epoch. Preserve the first generation of
            # existing sessions during the one-time migration only.
            active = c.execute(
                "SELECT COUNT(*) AS n FROM v43_auth_sessions WHERE revoked=0"
            ).fetchone()
            value = 1 if int(active['n'] or 0) else 1
            c.execute("INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,datetime('now')) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=datetime('now')", ('security.auth_epoch_migrated','1'))
            c.execute(
                "INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,datetime('now')) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=datetime('now')",
                ('security.auth_epoch', str(value))
            )
            c.commit()
        with _auth_epoch_lock:
            _auth_epoch = value
        log.info('[Auth] persistent auth epoch loaded: %d', value)
        return value
    except Exception as exc:
        # Authentication must remain usable even if the optional persistence
        # record cannot be read. Keep the in-process fallback value.
        log.warning('[Auth] persistent auth epoch load failed: %s', exc)
        return _auth_epoch

def _persist_auth_epoch(value: int) -> None:
    """Persist the current authentication epoch with SQLite retry handling."""
    value = max(1, int(value))
    try:
        def _write():
            c = _db_conn()
            c.execute(
                "INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,datetime('now')) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=datetime('now')",
                ('security.auth_epoch', str(value))
            )
            c.commit()
        _db_retry_write(_write, 'persist auth epoch')
    except Exception as exc:
        log.warning('[Auth] persistent auth epoch write failed: %s', exc)

def _hash_user_password(password: str) -> str:
    if not password or len(password) < 8:
        raise ValueError('Password must contain at least 8 characters')
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, AUTH_DB_ITERATIONS)
    return 'pbkdf2_sha256${}${}${}'.format(
        AUTH_DB_ITERATIONS, base64.urlsafe_b64encode(salt).decode('ascii'),
        base64.urlsafe_b64encode(digest).decode('ascii'))

def _verify_user_password(password: str, encoded: str) -> bool:
    try:
        scheme, iterations, salt_b64, digest_b64 = str(encoded or '').split('$', 3)
        if scheme != 'pbkdf2_sha256':
            return False
        salt = base64.urlsafe_b64decode(salt_b64.encode('ascii'))
        expected = base64.urlsafe_b64decode(digest_b64.encode('ascii'))
        actual = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False

def _default_permissions(role: str) -> list[str]:
    return list(AUTH_ROLE_DEFAULTS.get(role, AUTH_ROLE_DEFAULTS['custom']))

def _user_row(username: str):
    try:
        return _db_conn().execute('SELECT * FROM v43_auth_users WHERE username=?', (username,)).fetchone()
    except Exception:
        return None

_USER_PERMISSION_CACHE: dict[tuple[str, int, str], set[str]] = {}
_USER_PERMISSION_CACHE_LOCK = threading.RLock()


def _user_permissions(username: str, role: str = '') -> set[str]:
    if role == 'admin':
        return set(AUTH_PERMISSION_DEFS)
    row = _user_row(username)
    auth_version = int(row['auth_version'] or 1) if row and 'auth_version' in row.keys() else 0
    key = (str(username or ''), auth_version, str(role or ''))
    with _USER_PERMISSION_CACHE_LOCK:
        cached = _USER_PERMISSION_CACHE.get(key)
        if cached is not None:
            return set(cached)
    if role == 'readonly':
        result = {'dashboard.view', 'stream.view'}
    elif row:
        try:
            perms = json.loads(row['permissions_json'] or '[]')
            if isinstance(perms, list):
                result = {str(x) for x in perms if str(x) in AUTH_PERMISSION_DEFS}
                if role == 'operator':
                    result.add('stream.view')
            else:
                result = set(_default_permissions(role))
        except Exception:
            result = set(_default_permissions(role))
    else:
        result = set(_default_permissions(role))
    with _USER_PERMISSION_CACHE_LOCK:
        _USER_PERMISSION_CACHE[key] = set(result)
        if len(_USER_PERMISSION_CACHE) > 1024:
            for old_key in list(_USER_PERMISSION_CACHE)[:256]:
                _USER_PERMISSION_CACHE.pop(old_key, None)
    return set(result)

def _has_permission(permission: str) -> bool:
    if not _auth_enabled():
        return True
    if hasattr(g, 'api_key_permissions'):
        return permission in getattr(g, 'api_key_permissions', set())
    role = _current_role()
    if role == 'admin':
        return True
    username = session.get('user', '')
    return permission in _user_permissions(username, role)

# ──────────────────────────────────────────────────────────────────────────────
# Per-user camera/device access scope (v85)
# Roles/permissions answer WHAT a user may do; access scope answers WHICH
# cameras/devices they may touch. Admins remain unrestricted.
# ──────────────────────────────────────────────────────────────────────────────
def _access_scope_tables(c):
    c.executescript("""
        CREATE TABLE IF NOT EXISTS v43_user_access_scope (
            username TEXT NOT NULL,
            scope_type TEXT NOT NULL CHECK(scope_type IN ('country','device','camera')),
            scope_value TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY(username, scope_type, scope_value),
            FOREIGN KEY(username) REFERENCES v43_auth_users(username) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_v43_user_scope_user ON v43_user_access_scope(username,scope_type);
        CREATE INDEX IF NOT EXISTS idx_v43_user_scope_value ON v43_user_access_scope(scope_type,scope_value);
    """)
    c.commit()

def _ensure_access_scope_tables():
    try:
        _access_scope_tables(_db_conn())
    except Exception:
        log.exception('[Access] could not initialize access-scope tables')

def _normalize_scope_value(value: str) -> str:
    return str(value or '').strip()

def _load_user_scope(username: str) -> dict:
    out={'countries':[], 'devices':[], 'cameras':[]}
    if not username or not _auth_enabled(): return out
    try:
        rows=_db_conn().execute('SELECT scope_type,scope_value FROM v43_user_access_scope WHERE username=? ORDER BY scope_type,scope_value',(username,)).fetchall()
        for r in rows:
            k={'country':'countries','device':'devices','camera':'cameras'}.get(r['scope_type'])
            if k: out[k].append(str(r['scope_value']))
    except Exception:
        log.exception('[Access] failed loading scope for %s',username)
    return out

def _camera_access_allowed(camera: dict|sqlite3.Row|None, username: str|None=None) -> bool:
    if not camera or not _auth_enabled(): return bool(camera)
    user=str(username or _current_user() or '')
    if not user or (_current_role()=='admin' and not hasattr(g, 'api_key_permissions')): return True
    scope=_load_user_scope(user)
    # Empty scope means unrestricted for backward compatibility. Once an admin
    # assigns at least one rule, the user is restricted to the union of rules.
    if not any(scope.values()): return True
    cid=str(camera.get('id','') if hasattr(camera,'get') else camera['id'])
    country=str(camera.get('country','') if hasattr(camera,'get') else camera['country']).strip().lower()
    device=str(camera.get('device_id','') if hasattr(camera,'get') else camera['device_id']).strip().lower()
    ip=str(camera.get('ip','') if hasattr(camera,'get') else camera['ip']).strip().lower()
    countries={str(x).strip().lower() for x in scope['countries']}
    devices={str(x).strip().lower() for x in scope['devices']}
    cameras={str(x).strip() for x in scope['cameras']}
    return cid in cameras or (device and device in devices) or (ip and ip in devices) or (country and country in countries)

def _filter_camera_access(cameras):
    if not _auth_enabled() or (_current_role()=='admin' and not hasattr(g, 'api_key_permissions')): return cameras
    return [c for c in cameras if _camera_access_allowed(c)]

def _camera_access_denied(camera_id: str):
    cam=db_get_camera(camera_id)
    if not cam: return jsonify({'ok':False,'detail':'Camera not found'}),404
    if not _camera_access_allowed(cam): return jsonify({'ok':False,'detail':'Camera access is not assigned to this user.'}),403
    return None

def _scope_payload(username: str) -> dict:
    scope=_load_user_scope(username)
    return {
        'username': username,
        'countries': scope['countries'], 'devices': scope['devices'], 'cameras': scope['cameras'],
        'restricted': bool(any(scope.values())),
    }

def _client_key() -> str:
    return request.remote_addr or 'unknown'

def _login_key(username: str, client_key: str) -> str:
    return f'{client_key}|{username.lower()}'

def _record_login_failure(username: str, client_key: str) -> None:
    now = time.time()
    key = _login_key(username, client_key)
    with _login_attempts_lock:
        # Remove expired entries periodically; otherwise a credential-stuffing source
        # can leave unbounded per-user/IP state in this process.
        if len(_login_attempts) > 2048:
            cutoff = now - max(LOGIN_WINDOW_SECONDS, LOGIN_LOCKOUT_SECONDS)
            for old_key, old_item in list(_login_attempts.items()):
                if float(old_item.get('locked_until', 0) or 0) < now and float(old_item.get('first', 0) or 0) < cutoff:
                    _login_attempts.pop(old_key, None)
        item = _login_attempts.get(key, {'failures': 0, 'first': now, 'locked_until': 0})
        if now - item.get('first', now) > LOGIN_WINDOW_SECONDS:
            item = {'failures': 0, 'first': now, 'locked_until': 0}
        item['failures'] += 1
        if item['failures'] >= LOGIN_MAX_FAILURES:
            item['locked_until'] = now + LOGIN_LOCKOUT_SECONDS
        _login_attempts[key] = item
        ip_item = _login_ip_guard.get(client_key, {'failures': 0, 'first': now, 'locked_until': 0})
        if now - ip_item.get('first', now) > LOGIN_WINDOW_SECONDS:
            ip_item = {'failures': 0, 'first': now, 'locked_until': 0}
        ip_item['failures'] += 1
        if ip_item['failures'] >= LOGIN_MAX_FAILURES * 3:
            ip_item['locked_until'] = now + LOGIN_LOCKOUT_SECONDS
        _login_ip_guard[client_key] = ip_item
    try:
        first_dt = utc_now() - timedelta(seconds=max(0, now - item['first']))
        locked = utc_now() + timedelta(seconds=LOGIN_LOCKOUT_SECONDS) if item['locked_until'] > now else None
        ip_locked = utc_now() + timedelta(seconds=LOGIN_LOCKOUT_SECONDS) if ip_item['locked_until'] > now else None
        def _write():
            c=_db_conn()
            c.execute("""INSERT INTO v43_login_lockouts(username,client_ip,failures,first_attempt,locked_until,updated_at)
                         VALUES(?,?,?,?,?,?)
                         ON CONFLICT(username,client_ip) DO UPDATE SET failures=excluded.failures,first_attempt=excluded.first_attempt,locked_until=excluded.locked_until,updated_at=excluded.updated_at""",
                      (username,client_key,item['failures'],first_dt.isoformat().replace('+00:00','Z'),locked.isoformat().replace('+00:00','Z') if locked else '',utc_iso()))
            c.execute("""INSERT INTO v43_login_lockouts(username,client_ip,failures,first_attempt,locked_until,updated_at)
                         VALUES('__ip__',?,?,?,?,?)
                         ON CONFLICT(username,client_ip) DO UPDATE SET failures=excluded.failures,first_attempt=excluded.first_attempt,locked_until=excluded.locked_until,updated_at=excluded.updated_at""",
                      (client_key,ip_item['failures'],(utc_now()-timedelta(seconds=max(0,now-ip_item['first']))).isoformat().replace('+00:00','Z'),ip_locked.isoformat().replace('+00:00','Z') if ip_locked else '',utc_iso()))
            c.commit()
        _db_retry_write(_write,'login lockout write')
    except Exception:
        log.exception('login lockout persistence failed')

def _load_login_lockout(username: str, client_key: str):
    try:
        c = _db_conn()
        iprow=c.execute("SELECT failures,first_attempt,locked_until FROM v43_login_lockouts WHERE username='__ip__' AND client_ip=?",(client_key,)).fetchone()
        if iprow:
            first = time.time()
            try:
                if iprow['first_attempt']:
                    first = datetime.fromisoformat(str(iprow['first_attempt']).replace('Z','+00:00')).timestamp()
            except Exception:
                pass
            locked_ts = 0.0
            try:
                if iprow['locked_until']:
                    locked_ts = datetime.fromisoformat(str(iprow['locked_until']).replace('Z','+00:00')).timestamp()
            except Exception:
                pass
            with _login_attempts_lock:
                _login_ip_guard[client_key]={'failures':int(iprow['failures'] or 0),'first':first,'locked_until':locked_ts}
        row=c.execute('SELECT failures,locked_until FROM v43_login_lockouts WHERE username=? AND client_ip=?',(username,client_key)).fetchone()
        if row and row['locked_until']:
            dt=datetime.fromisoformat(str(row['locked_until']).replace('Z','+00:00'))
            if dt.timestamp()>time.time():
                with _login_attempts_lock:
                    _login_attempts[_login_key(username,client_key)]={'failures':int(row['failures'] or 0),'first':time.time(),'locked_until':dt.timestamp()}
                return True
    except Exception:
        pass
    return False

def _ip_guard_locked(client_key: str) -> bool:
    now = time.time()
    with _login_attempts_lock:
        item = _login_ip_guard.get(client_key, {'failures': 0, 'first': now, 'locked_until': 0})
        if item.get('locked_until', 0) > now:
            return True
    try:
        row=_db_conn().execute("SELECT failures,locked_until FROM v43_login_lockouts WHERE username='__ip__' AND client_ip=?",(client_key,)).fetchone()
        if row and row['locked_until']:
            dt=datetime.fromisoformat(str(row['locked_until']).replace('Z','+00:00'))
            if dt.timestamp()>now:
                with _login_attempts_lock:
                    _login_ip_guard[client_key]={'failures':int(row['failures'] or 0),'first':now,'locked_until':dt.timestamp()}
                return True
    except Exception:
        pass
    return False

def _clear_login_failures(username: str, client_key: str | None = None) -> None:
    with _login_attempts_lock:
        if client_key is None:
            suffix = '|' + username.lower()
            for key in list(_login_attempts):
                if key.endswith(suffix):
                    _login_attempts.pop(key, None)
        else:
            _login_attempts.pop(_login_key(username, client_key), None)
    try:
        def _write():
            c=_db_conn()
            if client_key is None: c.execute('DELETE FROM v43_login_lockouts WHERE username=?',(username,))
            else: c.execute('DELETE FROM v43_login_lockouts WHERE username=? AND client_ip=?',(username,client_key))
            c.commit()
        _db_retry_write(_write,'clear login lockout')
    except Exception: pass

def _clear_all_user_lockouts() -> int:
    with _login_attempts_lock:
        n = len(_login_attempts)
        _login_attempts.clear()
    try:
        def _write():
            c=_db_conn(); c.execute("DELETE FROM v43_login_lockouts WHERE username!='__ip__'"); c.commit()
        _db_retry_write(_write,'clear user lockouts')
    except Exception: pass
    return n

def _session_valid() -> bool:
    if not _auth_enabled():
        return True
    username = str(session.get('user') or '')
    if not username:
        # Anonymous requests are normal for /login, health/static endpoints and
        # the first request before authentication. Do not flood the log or spend
        # CPU formatting warnings for an expected unauthenticated state.
        return False
    if session.get('auth_epoch') != _auth_epoch:
        # v84 migration compatibility: v83 did not persist the epoch, so an
        # already-authenticated browser may legitimately carry epoch 0 or 1.
        # Only allow this narrow compatibility once, and only when the current
        # security epoch has not been explicitly advanced.
        legacy_epoch = session.get('auth_epoch')
        if legacy_epoch in {0, 1} and _auth_epoch == 1:
            session['auth_epoch'] = _auth_epoch
            session.modified = True
            log.info('[Auth] migrated legacy session epoch user=%s legacy_epoch=%r -> %d',
                     username, legacy_epoch, _auth_epoch)
        else:
            log.info('[Auth] stale session rejected user=%s session_epoch=%r current_epoch=%r',
                     username, session.get('auth_epoch'), _auth_epoch)
            session.clear()
            return False
    last = float(session.get('last_activity') or 0)
    if last <= 0 or (time.time() - last) > SESSION_TIMEOUT_SECONDS:
        log.warning('[Auth] session invalid: timeout user=%s last_activity=%r',
                    username, last)
        session.clear()
        return False
    sid = str(session.get('session_id') or '')
    row = _user_row(username)
    if row:
        if not int(row['enabled'] or 0):
            session.clear(); return False
        if int(session.get('auth_version', 0)) != int(row['auth_version'] or 1):
            session.clear(); return False
    elif username not in {AUTH_USER, READONLY_USER}:
        session.clear(); return False
    if sid:
        srow = None
        session_check_error = None
        # The dashboard has many background SQLite writers. Do not turn a
        # transient SQLite read/busy error into an immediate browser logout.
        for _attempt in range(3):
            try:
                srow = _db_conn().execute(
                    'SELECT revoked, last_seen FROM v43_auth_sessions WHERE session_id=? AND username=?',
                    (sid, username)).fetchone()
                session_check_error = None
                break
            except Exception as exc:
                session_check_error = exc
                if _attempt < 2:
                    time.sleep(0.12 * (_attempt + 1))
        if session_check_error is not None:
            # A busy/temporarily unavailable SQLite read must not destroy a
            # perfectly valid browser session. The signed Flask session already
            # passed epoch/user/auth-version checks; keep it valid and retry the
            # persistent session-store check on the next request.
            log.debug('[Auth] session-store verification temporarily unavailable for %s: %s',
                      username, session_check_error)
            session['last_activity'] = time.time()
            return True
        if not srow or int(srow['revoked'] or 0):
            log.info('[Auth] session rejected for %s: missing or revoked session_id=%s',
                     username, sid[:12])
            session.clear()
            return False
        if time.time() - float(session.get('_session_touch') or 0) > 30:
            try:
                _db_retry_write(lambda: _db_conn().execute(
                    "UPDATE v43_auth_sessions SET last_seen=? WHERE session_id=?",
                    (utc_iso(), sid)).connection.commit(), 'session heartbeat')
                session['_session_touch'] = time.time()
            except Exception:
                pass
    session['last_activity'] = time.time()
    return True

def _current_role() -> str:
    if not has_request_context():
        return 'admin'
    if hasattr(g, 'api_key_permissions'):
        return 'api_key'
    return session.get('role', '') if _auth_enabled() and _session_valid() else 'admin'

def _current_user() -> str:
    if not has_request_context():
        return 'system'
    if hasattr(g, 'api_key_permissions'):
        return str(getattr(g, 'api_key_owner', '') or ('api-key:' + str(getattr(g, 'api_key_name', 'unknown'))))
    if not _auth_enabled():
        return AUTH_USER or 'admin'
    return str(session.get('user') or '')

def _required_permission_for_request() -> str | None:
    path = request.path
    method = request.method.upper()
    # Self-service password changes are authorized by the endpoint itself: any
    # authenticated user may change their own password; only admin may reset others.
    if path.startswith('/api/v43/users/') and path.endswith('/password') and method == 'POST':
        return None
    if path.startswith('/api/v43/security') or path.startswith('/api/v43/users'):
        return 'security.manage' if method != 'GET' else 'security.view'
    if path.startswith('/api/admin/') or path in {'/api/advanced/backup', '/api/advanced/integrity', '/api/v43/backup/restore', '/api/v43/maintenance', '/api/v40/maintenance', '/api/v40/backup/restore'}:
        return 'database.manage'
    if path.startswith('/api/v110/api-keys'):
        return 'security.manage' if method != 'GET' else 'security.view'
    if path.startswith('/api/v43/settings') or path.startswith('/api/v40/settings'):
        return 'settings.manage' if method != 'GET' else 'security.view'
    if path.startswith('/api/v43/alerts') or path.startswith('/api/v40/alerts') or path.startswith('/api/telegram/'):
        return 'alerts.manage'
    if path in {'/api/v43/features'}:
        return 'diagnostics.run'
    if path.startswith('/api/v43/health'):
        return 'health.view'
    if path.startswith('/api/v43/performance'):
        return 'diagnostics.run'
    if path.startswith('/api/v43/device/report/'):
        return 'reports.view'
    if path.startswith('/api/v43/recovery'):
        return 'settings.manage'
    if path.startswith('/api/v62/sessions/') and path.endswith('/revoke'):
        return 'security.manage'
    if path.startswith('/api/v62/sessions'):
        return 'security.view'
    if path.startswith('/api/v62/self-test') or path.startswith('/api/v62/operations') or path.startswith('/api/v62/queue-health'):
        return 'diagnostics.run'
    if path.startswith('/api/v62/queue-cleanup'):
        return 'diagnostics.run'
    if path.startswith('/api/v62/config-health'):
        return 'security.view'
    if path.startswith('/api/v64/validation/control'):
        return 'diagnostics.run'
    if path.startswith('/api/v64/dashboard-metrics'):
        return 'dashboard.view'
    if path.startswith('/api/v110/backup') or path.startswith('/api/v110/db') or path.startswith('/api/v110/maintenance') or path.startswith('/api/v110/retention') or path.startswith('/api/v110/api-keys'):
        return 'database.manage'
    if path.startswith('/api/v110/scan') or path.startswith('/api/v110/setup') or path.startswith('/api/v110/diagnostics'):
        return 'diagnostics.run'
    if path.startswith('/api/v110/cameras'):
        return 'camera.manage' if method != 'GET' else 'dashboard.view'
    if path.startswith('/api/v61/db/'):
        return 'database.manage'
    if path.startswith('/api/v61/audit') or path.startswith('/api/v61/login-history') or path.startswith('/api/v61/sessions'):
        return 'security.view'
    if path.startswith('/api/v61/reports'):
        return 'reports.view'
    if path.startswith('/api/v61/worker') or path.startswith('/api/v61/quality'):
        return 'diagnostics.run'
    if path.startswith('/api/v61/recovery'):
        return 'settings.manage'
    if path.startswith('/api/v61/geo'):
        return 'health.view'
    if path.startswith('/api/v43/layouts'):
        return 'settings.manage'
    # Live playback and credential-bearing RTSP retrieval require the explicit
    # stream.view permission. dashboard.view alone must never grant playback.
    if path.startswith('/stream/') or path.startswith('/ws/stream/') or path.endswith('/rtsp'):
        return 'stream.view'
    if path.startswith('/stop/'):
        return 'stream.view'
    if 'diagnostic' in path or path.endswith('/diagnose'):
        return 'diagnostics.run'
    if path.startswith('/api/category/') or path.startswith('/api/bulk/') or path.startswith('/api/cameras/') and method in {'POST','PUT','PATCH','DELETE'}:
        return 'camera.manage'
    if path.startswith('/api/advanced/') and method in {'POST','PUT','PATCH','DELETE'}:
        return 'camera.manage'
    if path.startswith('/api/v43/') and method in {'POST','PUT','PATCH','DELETE'}:
        return 'camera.control'
    if path.startswith('/api/') and method in {'POST','PUT','PATCH','DELETE'}:
        return 'camera.control'
    # Default-deny for unclassified API endpoints: unknown APIs are treated as
    # security-sensitive until an explicit least-privilege rule is added.
    if path.startswith('/api/'):
        return 'security.manage'
    return None

def _auth_error(message: str = 'Authentication required') -> Response:
    if request.path.startswith('/api/') or request.path.startswith('/stream'):
        return jsonify({'ok': False, 'detail': message}), 401
    return redirect(url_for('login', next=request.full_path))


@app.after_request
def _v111_security_headers(response):
    response.headers.setdefault('X-Content-Type-Options','nosniff')
    response.headers.setdefault('X-Frame-Options','SAMEORIGIN')
    response.headers.setdefault('Referrer-Policy','strict-origin-when-cross-origin')
    response.headers.setdefault('Content-Security-Policy',"default-src 'self' 'unsafe-inline' data: blob:; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self' ws: wss:; frame-ancestors 'self'")
    return response


@app.after_request
def _v75_cookie_scheme_compat(response: Response):
    """Make auth cookies work behind Replit/proxy previews and normal browsers.

    Replit previews can be embedded in a cross-site iframe. In that case a
    SameSite=Lax cookie may be stored but is not returned to the application.
    On HTTPS we therefore use SameSite=None + Secure. On plain HTTP we fall
    back to Lax + no Secure because browsers reject Secure cookies over HTTP.
    """
    try:
        cookies = response.headers.getlist('Set-Cookie')
        if not cookies:
            return response
        response.headers.remove('Set-Cookie')
        secure = bool(request.is_secure)
        for cookie in cookies:
            cookie = re.sub(r';\s*SameSite=[^;]+', '', cookie, flags=re.I)
            cookie = re.sub(r';\s*Secure(?=;|$)', '', cookie, flags=re.I)
            cookie = re.sub(r';\s*Domain=[^;]+', '', cookie, flags=re.I)
            if secure:
                cookie += '; SameSite=None; Secure; Partitioned'
            else:
                cookie += '; SameSite=Lax'
            response.headers.add('Set-Cookie', cookie)
    except Exception as exc:
        log.warning('[Auth] v75 cookie compatibility rewrite failed: %s', exc)
    return response


# v74 authentication compatibility: some hosted reverse-proxy/browser paths
# can drop Flask's normal signed session cookie after the login redirect.
# Keep a second signed, HttpOnly compatibility cookie carrying the same small
# session payload. It is signed with Flask's existing secret, so it cannot be
# forged without the application secret. The cookie is intentionally not marked
# Secure so it also works when a Replit preview/proxy presents the browser over
# plain HTTP. HTTPS deployments should still use the primary Secure cookie when
# CAMERA_SECURE_COOKIE=1.
_V74_AUTH_COOKIE = 'camera_dashboard_auth'

def _v74_session_serializer():
    try:
        return app.session_interface.get_signing_serializer(app)
    except Exception:
        return None

def _v74_auth_cookie_value():
    try:
        serializer = _v74_session_serializer()
        if serializer is None or not session.get('user'):
            return ''
        return serializer.dumps(dict(session))
    except Exception:
        log.exception('[Auth] v74 fallback cookie creation failed')
        return ''

def _v74_restore_fallback_session():
    if session.get('user') or not _auth_enabled():
        return
    primary_name = app.config.get('SESSION_COOKIE_NAME', 'camera_dashboard_session')
    # Never attempt fallback restoration when the browser already supplied the
    # primary Flask session cookie; this avoids duplicate auth work on every
    # request.
    if request.cookies.get(primary_name):
        return
    token = request.cookies.get(_V74_AUTH_COOKIE, '')
    if not token:
        return
    try:
        serializer = _v74_session_serializer()
        if serializer is None:
            return
        restored = serializer.loads(token, max_age=SESSION_TIMEOUT_SECONDS)
        if not isinstance(restored, dict) or not restored.get('user'):
            raise ValueError('invalid fallback session payload')

        # v101: never restore a compatibility cookie whose security epoch is
        # already stale.  The old flow restored it first and only then
        # _session_valid() rejected it, producing a misleading
        # "restored ... / auth_epoch mismatch" pair after logout-all.
        # Rejecting before session mutation preserves the logout-all security
        # boundary and avoids unnecessary session work/log noise.
        restored_epoch = restored.get('auth_epoch')
        try:
            restored_epoch_int = int(restored_epoch)
        except (TypeError, ValueError):
            restored_epoch_int = -1
        if restored_epoch_int != int(_auth_epoch):
            log.info('[Auth] stale compatibility cookie ignored user=%s cookie_epoch=%r current_epoch=%r',
                     str(restored.get('user') or ''), restored_epoch, _auth_epoch)
            return

        session.clear()
        session.update(restored)
        session.permanent = True
        session.modified = True
        log.info('[Auth] v74 restored session from compatibility cookie user=%s',
                 str(restored.get('user') or ''))
    except Exception as exc:
        log.warning('[Auth] v74 fallback cookie rejected: %s', exc)
        session.clear()

@app.before_request
def _v74_restore_auth_session():
    _v74_restore_fallback_session()

def _v74_issue_auth_cookie(response: Response):
    """Issue the signed compatibility session cookie immediately.

    Replit/proxy deployments can occasionally drop Flask's primary session
    cookie during a redirect.  The compatibility cookie is signed with the
    same Flask serializer and is therefore safe to use as the fallback.
    """
    try:
        if not (_auth_enabled() and session.get('user')):
            return response
        token = _v74_auth_cookie_value()
        if not token:
            return response
        existing = response.headers.getlist('Set-Cookie')
        if any((_V74_AUTH_COOKIE + '=') in c for c in existing):
            return response
        response.set_cookie(
            _V74_AUTH_COOKIE, token, max_age=SESSION_TIMEOUT_SECONDS,
            httponly=True, samesite=('None' if request.is_secure else 'Lax'),
            secure=bool(request.is_secure), path='/'
        )
        if request.is_secure:
            cookies = response.headers.getlist('Set-Cookie')
            response.headers.remove('Set-Cookie')
            for cookie in cookies:
                if _V74_AUTH_COOKIE + '=' in cookie and 'Partitioned' not in cookie:
                    cookie += '; Partitioned'
                response.headers.add('Set-Cookie', cookie)
    except Exception as exc:
        log.warning('[Auth] immediate compatibility cookie issue failed: %s', exc)
    return response


@app.after_request
def _v74_sync_auth_cookie(response: Response):
    """Maintain the v74 fallback cookie only when the primary cookie is absent.

    The old implementation re-signed and emitted a compatibility cookie on
    virtually every authenticated API response. On a busy dashboard this adds
    avoidable serialization/header work and produces noisy logs. The fallback
    is only needed when the browser/proxy did not send the primary session
    cookie (or during the login redirect itself).
    """
    try:
        if not _auth_enabled() or not session.get('user'):
            return response
        primary_name = app.config.get('SESSION_COOKIE_NAME', 'camera_dashboard_session')
        primary_present = bool(request.cookies.get(primary_name))
        if primary_present:
            return response
        token = _v74_auth_cookie_value()
        if not token:
            return response
        response.set_cookie(
            _V74_AUTH_COOKIE, token, max_age=SESSION_TIMEOUT_SECONDS,
            httponly=True, samesite=('None' if request.is_secure else 'Lax'),
            secure=bool(request.is_secure), path='/'
        )
        if request.is_secure:
            _cookies = response.headers.getlist('Set-Cookie')
            response.headers.remove('Set-Cookie')
            for _cookie in _cookies:
                if _V74_AUTH_COOKIE + '=' in _cookie and 'Partitioned' not in _cookie:
                    _cookie += '; Partitioned'
                response.headers.add('Set-Cookie', _cookie)
        log.debug('[Auth] compatibility cookie refreshed path=%s secure=%s',
                  request.path, request.is_secure)
    except Exception as exc:
        log.warning('[Auth] v74 compatibility cookie sync failed: %s', exc)
    return response

_API_RATE_STATE = {}
_API_RATE_LOCK = threading.RLock()
API_RATE_LIMIT = max(60, int(os.getenv('CAMERA_API_RATE_LIMIT','300')))

def _api_key_context():
    supplied = request.headers.get('X-Camera-API-Key','').strip()
    if not supplied:
        auth = request.headers.get('Authorization','')
        if auth.lower().startswith('bearer '): supplied = auth[7:].strip()
    if not supplied: return None
    # API keys are 256-bit random high-entropy bearer secrets; unsalted SHA-256 is
    # intentional here because the token itself is never derived from a password.
    digest=hashlib.sha256(supplied.encode()).hexdigest()
    try:
        row=_db_conn().execute('SELECT * FROM api_keys WHERE key_hash=? AND enabled=1',(digest,)).fetchone()
        if not row: return None
        perms=set(json.loads(row['permissions_json'] or '[]'))
        g.api_key_id=int(row['id']); g.api_key_name=str(row['name']); g.api_key_permissions=perms
        g.api_key_owner=str(row['owner_username'] or '') if 'owner_username' in row.keys() else ''
        _db_retry_write(lambda: _db_conn().execute("UPDATE api_keys SET last_used=datetime('now') WHERE id=?",(int(row['id']),)).connection.commit(),'api key last-used')
        return row
    except Exception:
        return None

def _csrf_token() -> str:
    token = str(session.get('_csrf_token') or '')
    if not token:
        token = secrets.token_urlsafe(32)
        session['_csrf_token'] = token
    return token


def _csrf_and_rate_guard():
    if request.method=='OPTIONS' or request.path in {'/login','/healthz','/readyz','/_health','/favicon.ico'}: return None
    if request.path.startswith('/api/'):
        key=_client_key()
        now=time.time()
        with _API_RATE_LOCK:
            bucket=_API_RATE_STATE.setdefault(key,[])
            cutoff=now-60; bucket[:]=[t for t in bucket if t>=cutoff]
            if len(bucket)>=API_RATE_LIMIT:
                return jsonify({'ok':False,'detail':'API rate limit exceeded'}),429
            bucket.append(now)
            # Periodically prune idle client buckets so a long-lived process does not
            # accumulate one list entry per source IP forever.
            if len(_API_RATE_STATE) > 2048:
                stale=[k for k,v in _API_RATE_STATE.items() if not v or v[-1] < cutoff]
                for k in stale[:1024]: _API_RATE_STATE.pop(k,None)
    if request.method in {'POST','PUT','PATCH','DELETE'} and _auth_enabled():
        # Bearer/API-key requests authenticate with the key itself and do not use
        # browser CSRF cookies. Browser/session requests always require the token.
        if hasattr(g, 'api_key_permissions'):
            return None
        expected=_csrf_token()
        token=request.headers.get('X-CSRF-Token','') or request.form.get('_csrf','')
        if expected and token and hmac.compare_digest(str(token),str(expected)):
            return None
        return jsonify({'ok':False,'detail':'CSRF protection rejected this request'}),403
    return None

@app.before_request
def _v114_api_security_guard():
    _api_key_context()
    return _csrf_and_rate_guard()


@app.before_request
def _enforce_camera_access_scope():
    if not _auth_enabled() or _current_role()=='admin' or request.method=='OPTIONS':
        return None
    # Protect every route whose path variable identifies a camera. This sits
    # above individual handlers so legacy/new endpoints cannot accidentally
    # bypass the user's camera/device allow-list.
    view_args=request.view_args or {}
    for key in ('cam_id','camera_id'):
        cid=view_args.get(key)
        if cid:
            denied=_camera_access_denied(str(cid))
            if denied: return denied
    # Bulk APIs commonly accept an ids[] JSON list instead of a path variable.
    if request.method in ('POST','PUT','PATCH') and request.is_json:
        try:
            payload=request.get_json(silent=True) or {}
            ids=payload.get('ids') or payload.get('camera_ids') or []
            if isinstance(ids,list):
                for cid in ids[:5000]:
                    denied=_camera_access_denied(str(cid))
                    if denied: return denied
        except Exception:
            pass
    return None

@app.before_request
def _require_dashboard_auth():
    if request.endpoint in {'login', 'logout', 'favicon', 'healthz', 'readyz', '_health_endpoint'} or request.method == 'OPTIONS':
        return None
    if hasattr(g, 'api_key_permissions'):
        required = _required_permission_for_request()
        if required and not _has_permission(required):
            return jsonify({'ok': False, 'detail': f'Permission required: {required}'}), 403
        return None
    if not _auth_enabled():
        return None
    if not _session_valid():
        return _auth_error('Session expired or logged out')
    required = _required_permission_for_request()
    if required and not _has_permission(required):
        return jsonify({'ok': False, 'detail': f'Permission required: {required}'}), 403
    return None


# ══════════════════════════════════════════════════════════════════════════════
@app.before_request
def _v135_mark_ui_activity():
    # Any browser/API request is evidence that the operator is actively using
    # the dashboard. Background DB writers yield briefly in that window.
    _db_mark_ui_activity()
    return None

# DATABASE  (SQLite3 WAL, per-thread connections)
# ══════════════════════════════════════════════════════════════════════════════

_db_local = threading.local()
_DB_PATH_ADOPTION_LOCK = threading.Lock()

# Graceful application shutdown state.  The Stop App button and OS signals
# use the same path so the database is flushed/backed up before the process
# is allowed to terminate.
_shutdown_requested = threading.Event()
_shutdown_lock = threading.Lock()
_shutdown_complete = threading.Event()
_shutdown_started_at = None
_SHUTDOWN_HARD_DEADLINE_SECONDS = max(10.0, float(os.getenv('CAMERA_SHUTDOWN_DEADLINE_SECONDS', '20')))
_shutdown_watchdog = None
_SHUTDOWN_FLUSH_LOCK = threading.Lock()
# These primitives must exist before the Stop App worker or signal handler
# invokes _tg_notify_shutdown_once(). They were missing in V170, causing a
# NameError after the database flush had already completed.
_TG_SHUTDOWN_SENT = threading.Event()
_TG_SHUTDOWN_LOCK = threading.Lock()
CLEAN_SHUTDOWN_MARKER = ''

def _clean_shutdown_marker_path() -> str:
    return CLEAN_SHUTDOWN_MARKER or (DB_PATH + '.clean_shutdown.json')

def _remove_clean_shutdown_marker() -> None:
    try:
        os.remove(_clean_shutdown_marker_path())
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.debug('[Shutdown] Could not remove clean-shutdown marker: %s', exc)

def _write_clean_shutdown_marker() -> bool:
    """Record that the DB reached a clean, bounded shutdown barrier.

    The marker is consumed on the next startup. It is deliberately written
    only after the final commit/checkpoint succeeds, so a crash leaves no
    false proof of a clean shutdown.
    """
    path = _clean_shutdown_marker_path()
    try:
        db_stat = os.stat(DB_PATH)
        wal_path = DB_PATH + '-wal'
        wal_stat = os.stat(wal_path) if os.path.exists(wal_path) else None
        payload = {
            'version': VERSION,
            'timestamp': utc_iso(),
            'db_size': int(db_stat.st_size),
            'db_mtime_ns': int(db_stat.st_mtime_ns),
            'wal_size': int(wal_stat.st_size) if wal_stat else 0,
            'wal_mtime_ns': int(wal_stat.st_mtime_ns) if wal_stat else 0,
        }
        tmp = path + f'.tmp.{os.getpid()}.{threading.get_ident()}'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, separators=(',', ':'))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return True
    except Exception as exc:
        try:
            os.remove(tmp)
        except Exception:
            pass
        log.warning('[Shutdown] Could not write clean-shutdown marker: %s', exc)
        return False

def _consume_valid_clean_shutdown_marker() -> bool:
    """Return True when the previous process completed a clean DB barrier.

    The marker is consumed immediately. If anything changed the DB after the
    marker was written, normal quick_check is used instead. This prevents a
    stale marker from masking a later crash.
    """
    path = _clean_shutdown_marker_path()
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            marker = json.load(fh)
        os.remove(path)
        if not os.path.exists(DB_PATH):
            return False
        db_stat = os.stat(DB_PATH)
        wal_path = DB_PATH + '-wal'
        wal_size = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0
        same_db = (int(marker.get('db_size', -1)) == int(db_stat.st_size) and
                   int(marker.get('db_mtime_ns', -1)) == int(db_stat.st_mtime_ns) and
                   int(marker.get('wal_size', -1)) == int(wal_size))
        if same_db:
            # Lightweight open/read check only; the expensive page-by-page
            # quick_check is unnecessary after our own clean shutdown barrier.
            conn = sqlite3.connect(DB_PATH, timeout=2)
            try:
                conn.execute('SELECT name FROM sqlite_master LIMIT 1').fetchone()
            finally:
                conn.close()
            log.info('[DB Protection] Clean-shutdown marker valid; skipped full startup quick_check')
            return True
        log.warning('[DB Protection] Clean-shutdown marker did not match current DB; running normal quick_check')
    except FileNotFoundError:
        return False
    except Exception as exc:
        log.warning('[DB Protection] Clean-shutdown marker validation failed; running normal quick_check: %s', exc)
        try:
            os.remove(path)
        except Exception:
            pass
    return False

def _checkpoint_database() -> bool:
    """Attempt a bounded SQLite WAL checkpoint during shutdown."""
    acquired = False
    conn = None
    try:
        acquired = _DB_WRITE_LOCK.acquire(timeout=3.0)
        if not acquired:
            log.warning('[Shutdown] SQLite writer lock still busy; skipping checkpoint safely')
            return False
        conn = getattr(_db_local, 'conn', None)
        if conn is None:
            conn = sqlite3.connect(DB_PATH, timeout=2.0, check_same_thread=False)
            conn.execute('PRAGMA busy_timeout=2000')
        row = conn.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()
        conn.commit()
        if row and len(row) >= 1 and int(row[0]) not in (0,):
            log.warning('[Shutdown] SQLite WAL checkpoint reported busy: %s', tuple(row))
            return False
        return True
    except Exception as e:
        log.error('[Shutdown] SQLite checkpoint failed: %s', e)
        return False
    finally:
        if conn is not getattr(_db_local, 'conn', None):
            try: conn.close()
            except Exception: pass
        if acquired:
            _DB_WRITE_LOCK.release()

def _flush_database_for_shutdown() -> bool:
    """Perform a bounded final DB commit/checkpoint; never create a backup."""
    if not _SHUTDOWN_FLUSH_LOCK.acquire(blocking=False):
        log.info('[Shutdown] DB flush already running; waiting for existing shutdown barrier')
        return _shutdown_complete.wait(max(1.0, _SHUTDOWN_HARD_DEADLINE_SECONDS - 1.0))
    ok = True
    try:
        log.info('[Shutdown] DB flush phase started')
        acquired = _DB_WRITE_LOCK.acquire(timeout=3.0)
        if not acquired:
            log.error('[Shutdown] Could not acquire SQLite writer lock within 3s; continuing without unsafe force-unlock')
            return False
        try:
            c = getattr(_db_local, 'conn', None)
            if c is not None:
                try:
                    c.commit()
                except Exception as e:
                    log.error('[Shutdown] SQLite final commit failed: %s', e)
                    ok = False
        finally:
            _DB_WRITE_LOCK.release()

        # A backup may still be cancelling. Never wait for it here. The backup
        # path observes _shutdown_requested and aborts without replacing .bak.
        if _DB_BACKUP_LOCK.locked():
            log.info('[Shutdown] Database backup cancellation requested; not waiting for active backup')
        else:
            log.info('[Shutdown] No active database backup')

        if not _checkpoint_database():
            ok = False
        if ok:
            log.info('[Shutdown] SQLite commit/checkpoint complete')
        else:
            log.error('[Shutdown] SQLite flush completed with errors')
        return ok
    finally:
        _SHUTDOWN_FLUSH_LOCK.release()

def _graceful_shutdown(reason='user') -> bool:
    """Idempotent shutdown barrier: stop workers/feeds, flush DB, mark clean."""
    global _shutdown_started_at, _shutdown_watchdog
    with _shutdown_lock:
        if _shutdown_complete.is_set():
            return True
        if _shutdown_requested.is_set():
            log.info('[Shutdown] Shutdown already in progress; joining existing barrier')
            return _shutdown_complete.wait(max(1.0, _SHUTDOWN_HARD_DEADLINE_SECONDS - 1.0))
        _shutdown_requested.set()
        _shutdown_started_at = datetime.now(timezone.utc).isoformat()
        def _shutdown_watchdog_fn():
            if not _shutdown_complete.wait(_SHUTDOWN_HARD_DEADLINE_SECONDS):
                log.critical('[Shutdown] Hard deadline %.1fs exceeded; forcing process exit 75', _SHUTDOWN_HARD_DEADLINE_SECONDS)
                logging.shutdown()
                os._exit(75)
        _shutdown_watchdog = threading.Thread(target=_shutdown_watchdog_fn, daemon=True, name='shutdown-watchdog')
        _shutdown_watchdog.start()

    log.warning('[Shutdown] Graceful shutdown requested: %s', reason)
    try:
        _SCAN_CANCEL.set()
        _SCAN_PAUSE.clear()
        _scan_state_set(cancel_requested=True, phase='cancelling')
    except Exception:
        pass
    try:
        _validation_queue_paused.set()
        _validation_queue_restart_requested.set()
        log.info('[Shutdown] Background validation queue paused; active probes will finish/cancel cooperatively')
    except Exception:
        pass
    try:
        _TG_CONTROL_STOP.set()
    except Exception:
        pass
    try:
        with _streams_lock:
            active = [p for bucket in _streams.values() for p in bucket]
            _streams.clear(); _stream_subscribers.clear()
        log.info('[Shutdown] Stopping %d active stream process(es)', len(active))
        for proc in active:
            try: _stop_ffmpeg(proc)
            except Exception: pass
    except Exception:
        log.exception('[Shutdown] Failed stopping active streams during shutdown')

    # Give worker loops a tiny cooperative window, but never sleep for a long
    # period while the UI request is waiting.
    _shutdown_requested.wait(0.15)
    ok = _flush_database_for_shutdown()
    if ok:
        marker_ok = _write_clean_shutdown_marker()
        if marker_ok:
            log.info('[Shutdown] Graceful shutdown DB flush complete; clean marker written; periodic verified backup remains the recovery copy.')
        else:
            # The DB barrier succeeded, but a marker write failure must not be
            # reported as if the marker existed. Startup can run normal checks.
            log.warning('[Shutdown] Database flush completed, but clean marker could not be written; startup integrity checks will run normally.')
    else:
        _remove_clean_shutdown_marker()
        log.error('[Shutdown] Graceful shutdown DB flush completed with errors; clean marker NOT written.')
    _shutdown_complete.set()
    return ok


def _tg_notify_shutdown_once() -> None:
    with _TG_SHUTDOWN_LOCK:
        if _TG_SHUTDOWN_SENT.is_set():
            return
        _TG_SHUTDOWN_SENT.set()
    try:
        tg_notify_shutdown()
    except Exception:
        log.debug('Shutdown Telegram notification failed', exc_info=True)

_shutdown_exit_lock = threading.Lock()
_shutdown_exit_started = False

def _install_shutdown_signal_handlers() -> None:
    """Install non-blocking SIGINT/SIGTERM handlers.

    Python signal handlers must not raise SystemExit while ThreadPoolExecutor or
    atexit is joining worker threads.  The first signal starts one shutdown
    thread; that thread performs the DB barrier and then exits the process after
    an explicit best-effort Telegram notification.
    """
    def _handler(signum, frame):
        global _shutdown_exit_started
        name = signal.Signals(signum).name
        with _shutdown_exit_lock:
            if _shutdown_complete.is_set():
                log.info('Shutdown signal %s received after graceful shutdown; exiting cleanly', name)
                try: _tg_notify_shutdown_once()
                except Exception: pass
                logging.shutdown()
                os._exit(0)
            if _shutdown_exit_started:
                log.warning('Shutdown signal %s received again; forcing process exit', name)
                logging.shutdown()
                os._exit(130)
            _shutdown_exit_started = True
        def _finish():
            ok = False
            try:
                ok = _graceful_shutdown(name)
                log.info('Shutdown signal %s handled; database flush=%s', name, ok)
                try:
                    _tg_notify_shutdown_once()
                except Exception:
                    log.debug('Shutdown Telegram notification failed', exc_info=True)
            finally:
                logging.shutdown()
                os._exit(0 if ok else 1)
        threading.Thread(target=_finish, daemon=True, name='graceful-shutdown').start()
    try:
        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)
    except Exception as e:
        log.warning('Could not install shutdown signal handlers: %s', e)


def _sqlite_environmental_io_error(exc: BaseException) -> bool:
    """Distinguish quota/filesystem I/O failures from evidence of DB corruption."""
    msg = str(exc).lower()
    return any(token in msg for token in (
        'disk i/o error', 'disk is full', 'database or disk is full',
        'disk quota exceeded', 'no space left on device',
        'input/output error', 'unable to open database file'
    ))


def _sqlite_fast_probe_ok(path: str) -> bool:
    """Read-only structural probe; environmental I/O failures abort recovery decisions."""
    if not os.path.exists(path):
        return False
    try:
        if os.path.getsize(path) <= 0:
            return False
    except OSError as exc:
        raise RuntimeError(
            f'SQLITE_STORAGE_IO_ERROR: cannot stat database {path}: {type(exc).__name__}: {exc}. '
            'Do not restore or recreate the database until the storage quota/filesystem is repaired.'
        ) from exc
    conn = None
    try:
        uri = 'file:' + os.path.abspath(path).replace('\\', '/') + '?mode=ro'
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        conn.execute('PRAGMA schema_version').fetchone()
        row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cameras' LIMIT 1").fetchone()
        if row is not None:
            conn.execute('SELECT 1 FROM cameras LIMIT 1').fetchone()
        return True
    except sqlite3.Error as exc:
        log.error('SQLite fast startup probe failed for %s: %s', path, exc)
        if _sqlite_environmental_io_error(exc):
            raise RuntimeError(
                f'SQLITE_STORAGE_IO_ERROR: SQLite could not read {path} because of a filesystem/storage I/O error '
                f'({exc}). This is not proof of database corruption. Free/fix storage first; do not create a new DB.'
            ) from exc
        return False
    finally:
        if conn:
            conn.close()


def _sqlite_quick_check_ok(path: str) -> bool:
    """Run SQLite quick_check for an explicit/on-demand diagnostic.

    This is deliberately not used as the normal startup gate because SQLite
    quick_check can scan a large database and take minutes.
    """
    if not os.path.exists(path):
        return False
    conn = None
    try:
        uri = 'file:' + os.path.abspath(path).replace('\\', '/') + '?mode=ro'
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        row = conn.execute('PRAGMA quick_check(1)').fetchone()
        return bool(row and str(row[0]).lower() == 'ok')
    except sqlite3.Error as e:
        if _sqlite_environmental_io_error(e):
            log.error('SQLite quick check blocked by filesystem/storage I/O for %s: %s', path, e)
        else:
            log.error('SQLite quick check failed for %s: %s', path, e)
        return False
    finally:
        if conn:
            conn.close()

def _sqlite_integrity_ok(path: str) -> bool:
    """Check a database or backup without modifying it."""
    if not os.path.exists(path):
        return False
    conn = None
    try:
        uri = 'file:' + os.path.abspath(path).replace('\\', '/') + '?mode=ro'
        conn = sqlite3.connect(uri, uri=True, timeout=10)
        result = conn.execute('PRAGMA integrity_check').fetchall()
        return len(result) == 1 and result[0][0] == 'ok'
    except sqlite3.Error as e:
        if _sqlite_environmental_io_error(e):
            log.error('SQLite integrity check blocked by filesystem/storage I/O for %s: %s', path, e)
        else:
            log.error('SQLite integrity check failed for %s: %s', path, e)
        return False
    finally:
        if conn:
            conn.close()

def _background_live_integrity_check() -> None:
    """Run a non-destructive integrity check when the service is enabled."""
    _touch_worker('db-integrity','starting')
    if not _service_enabled('full-integrity'):
        _touch_worker('db-integrity','stopped','disabled by operator'); return
    try:
        _touch_worker('db-integrity','running')
        deep = os.getenv('CAMERA_BACKGROUND_FULL_INTEGRITY', '').strip().lower() in {'1','true','yes','on'}
        started = time.monotonic()
        ok = _sqlite_integrity_ok(DB_PATH) if deep else _sqlite_quick_check_ok(DB_PATH)
        mode = 'integrity_check' if deep else 'quick_check'
        _touch_worker('db-integrity','idle' if ok else 'error', f'{mode}=ok' if ok else f'{mode}=failed')
        log.info('[Services] Background %s %s in %.2fs', mode, 'PASS' if ok else 'FAILED', time.monotonic() - started)
    except Exception as exc:
        _touch_worker('db-integrity','error',type(exc).__name__)
        log.warning('[Services] Background full integrity check failed: %s', exc)


def _adopt_legacy_database_path_if_needed() -> None:
    with _DB_PATH_ADOPTION_LOCK:
        _adopt_legacy_database_path_if_needed_locked()

def _adopt_legacy_database_path_if_needed_locked() -> None:
    """Compatibility bridge for the v110+ runtime data directory.

    Older releases may have kept camera_db.sqlite beside the application script.
    The current default is the workspace itself, so the DB has one canonical
    location. Do not silently adopt the old /data runtime DB instead of ever creating
    an empty database.  We deliberately adopt the existing path first rather
    than copying a live SQLite database, because an active WAL may contain
    committed data that would be lost by a naive file copy.

    An explicitly supplied CAMERA_DATA_DIR / --data-dir remains authoritative:
    if the operator intentionally selected another data directory, protection
    still refuses to invent a new DB unless that directory has a verified
    backup.
    """
    global DB_PATH, DB_BACKUPS, DB_BACKUP_GLOB_PREFIX, DATA_DIR
    if os.path.exists(DB_PATH):
        return

    # If the operator explicitly configured a data directory, never silently
    # redirect it to the old project directory.
    explicit = bool(os.getenv('CAMERA_DATA_DIR'))
    if explicit:
        return

    legacy_candidates = []
    # Search the historical locations used by older releases, including the
    # current runtime data directory's parent.  Never create a new DB here;
    # only adopt an existing database after a full integrity check.
    workspace = os.path.abspath(os.path.dirname(SCRIPT_DIR))
    for candidate in (
        os.path.join(SCRIPT_DIR, 'camera_db.sqlite'),
        os.path.join(os.getcwd(), 'camera_db.sqlite'),
        os.path.join(DATA_DIR, 'camera_db.sqlite'),
        os.path.join(workspace, 'camera_db.sqlite'),
        '/home/runner/workspace/camera_db.sqlite',
    ):
        candidate = os.path.abspath(candidate)
        if candidate == os.path.abspath(DB_PATH) or candidate in legacy_candidates:
            continue
        legacy_candidates.append(candidate)

    for candidate in legacy_candidates:
        if not os.path.exists(candidate):
            continue
        if _sqlite_quick_check_ok(candidate):
            old_dir = os.path.dirname(candidate)
            DB_PATH = candidate
            DB_BACKUPS = [DB_PATH + '.bak']
            DB_BACKUP_GLOB_PREFIX = DB_PATH + '.bak'
            log.warning(
                '[DB Compatibility] New runtime DB not found; adopting existing verified legacy DB: %s',
                candidate,
            )
            log.warning(
                '[DB Compatibility] Runtime data directory remains %s for new files; existing DB is kept in place to preserve WAL/data safely.',
                DATA_DIR,
            )
            return

    # Last-resort compatibility recovery: adopt a verified backup from a
    # historical DB location.  This still refuses to invent an empty DB.
    backup_candidates = []
    for base in legacy_candidates:
        backup_candidates.extend((
            base + '.bak',
            base + '.backup',
        ))
    for base_dir in (SCRIPT_DIR, os.getcwd(), DATA_DIR, workspace, '/home/runner/workspace'):
        try:
            for pat in ('camera_db.sqlite.bak', 'camera_db.sqlite.backup.*', 'camera_db.sqlite.bak.*'):
                backup_candidates.extend(str(x) for x in Path(base_dir).glob(pat))
        except OSError:
            pass
    seen=set()
    verified=[]
    for candidate in backup_candidates:
        candidate=os.path.abspath(candidate)
        if candidate in seen or not os.path.isfile(candidate):
            continue
        seen.add(candidate)
        try:
            size=os.path.getsize(candidate)
            if size > 0 and _sqlite_quick_check_ok(candidate):
                verified.append((candidate, size, os.path.getmtime(candidate)))
        except OSError:
            continue
    if verified:
        verified.sort(key=lambda x:x[2], reverse=True)
        candidate=verified[0][0]
        log.warning('[DB Compatibility] No live legacy DB found; adopting newest verified backup: %s', candidate)
        # Keep the backup itself as the live source until normal startup has
        # completed; SQLite can safely open it read/write.  Point backups at
        # its sibling only after a successful adoption.
        DB_PATH = candidate
        DB_BACKUPS = [candidate]
        DB_BACKUP_GLOB_PREFIX = candidate
        return

def _recover_database_if_needed(allow_create_if_missing: bool = False) -> None:
    """Protect the live DB from silent empty-database recreation.

    If the live DB is missing or corrupt, recover only from a verified
    rotating backup. If no verified backup exists, abort startup rather than
    allowing CREATE TABLE to produce a brand-new empty database.
    """
    live_exists = os.path.exists(DB_PATH)
    if live_exists:
        # A clean shutdown marker is the fastest safe path after our own
        # bounded commit/checkpoint barrier. It is consumed immediately so a
        # later crash cannot inherit the marker. Unclean starts still perform
        # the normal quick_check protection path.
        if _consume_valid_clean_shutdown_marker():
            log.info('[DB Protection] Live database accepted from clean shutdown barrier: %s', DB_PATH)
            return
        # Full integrity_check is intentionally NOT part of normal startup.
        # A large but healthy SQLite DB can take several minutes to scan, which
        # used to prevent Flask from binding its HTTP port. Operators can force
        # the old behavior with CAMERA_STARTUP_FULL_INTEGRITY=1.
        force_full = os.getenv('CAMERA_STARTUP_FULL_INTEGRITY', '').strip().lower() in {'1','true','yes','on'}
        if force_full:
            log.info('[DB Protection] Forced full startup integrity check enabled')
            verified = _sqlite_integrity_ok(DB_PATH)
        else:
            started = time.monotonic()
            verified = _sqlite_fast_probe_ok(DB_PATH)
            log.info('[DB Protection] Fast database startup probe %s in %.2fs: %s',
                     'PASS' if verified else 'FAILED', time.monotonic() - started, DB_PATH)
            if verified and os.path.getsize(DB_PATH) >= 64 * 1024 * 1024:
                log.info('[DB Protection] Deep SQLite quick_check deferred to background maintenance for large DB')
        if verified:
            log.info('[DB Protection] Live database verified: %s', DB_PATH)
            return
        log.critical('[DB Protection] Live database failed startup verification: %s', DB_PATH)

    if not live_exists:
        log.critical('[DB Protection] LIVE DATABASE IS MISSING: %s', DB_PATH)
        # Explicit operator intent: when --create-db-if-missing is supplied,
        # a missing live DB means START FRESH. Do not restore/adopt any old
        # backup in this mode. This is intentionally different from the
        # normal protection path, which prefers verified recovery.
        if allow_create_if_missing:
            log.warning('[DB Protection] --create-db-if-missing supplied; starting a brand-new database at %s (existing backups will NOT be adopted).', DB_PATH)
            return
    else:
        log.critical('[DB Protection] LIVE DATABASE FAILED INTEGRITY CHECK: %s', DB_PATH)

    valid_backups = []
    for backup in DB_BACKUPS:
        if not os.path.exists(backup):
            continue
        try:
            size = os.path.getsize(backup)
            mtime = os.path.getmtime(backup)
        except OSError:
            continue
        if size > 0:
            try:
                backup_ok = _sqlite_quick_check_ok(backup)
            except Exception:
                raise
            if not backup_ok:
                # Disk quota errors can make a healthy backup temporarily unreadable.
                # Do not label it corrupt based only on an I/O failure.
                if not _storage_status().get('writable_probe', False):
                    raise RuntimeError(
                        'DB PROTECTION STOP: runtime storage is not writable (quota/full filesystem). '
                        'A backup could not be verified safely. Free storage first, then restart; '
                        'do not use --create-db-if-missing.'
                    )
                continue
            # A quick check is enough to select a candidate without blocking
            # startup. The copied recovery DB is still subjected to the full
            # integrity check below before it replaces the live DB.
            valid_backups.append((backup, size, mtime))
            log.info('[DB Protection] Verified backup available: %s (%d bytes)',
                     backup, size)

    if not valid_backups:
        if not live_exists and allow_create_if_missing:
            log.warning('[DB Protection] Live DB is missing and --create-db-if-missing was supplied; allowing fresh DB initialization: %s', DB_PATH)
            return
        raise RuntimeError(
            'DB PROTECTION STOP: live SQLite database is missing/corrupt and no '
            'verified backup is available. Refusing to create a new empty database. '
            'Use --create-db-if-missing only when you intentionally want a fresh database.')

    backup, size, _mtime = max(valid_backups, key=lambda x: x[2])
    restore_tmp = DB_PATH + f'.restore.{os.getpid()}.tmp'
    try:
        try:
            os.remove(restore_tmp)
        except FileNotFoundError:
            pass
        shutil.copy2(backup, restore_tmp)
        if not _sqlite_integrity_ok(restore_tmp):
            raise RuntimeError(f'restored temporary database failed integrity check: {backup}')
        os.replace(restore_tmp, DB_PATH)
        # Do not carry WAL/SHM sidecars from another DB generation into the
        # restored snapshot. The backup itself is a verified SQLite database.
        for sidecar in (DB_PATH + '-wal', DB_PATH + '-shm'):
            try:
                os.remove(sidecar)
            except FileNotFoundError:
                pass
        if not _sqlite_integrity_ok(DB_PATH):
            raise RuntimeError(f'restored database failed final integrity check: {backup}')
        log.critical('[DB Protection] RECOVERED LIVE DATABASE from %s (%d bytes)',
                     backup, size)
    except Exception:
        try:
            os.remove(restore_tmp)
        except OSError:
            pass
        raise


_DB_WRITE_LOCK = threading.RLock()
_DB_BACKUP_LOCK = threading.Lock()
# Pause background writers during online backup so SQLite's backup API can finish
# instead of repeatedly restarting its page copy while the live DB keeps changing.
_DB_BACKUP_ACTIVE = threading.Event()
_DB_MAINTENANCE_LOCK = threading.Lock()
_DB_MAINTENANCE_ACTIVE = threading.Event()
_DB_MAINTENANCE_STATE_LOCK = threading.Lock()
_DB_MAINTENANCE_STATE = {
    'active': False,
    'state': 'free',
    'phase': 'idle',
    'actor': '',
    'started_at': '',
    'updated_at': '',
    'completed_at': '',
    'last_result': '',
    'last_detail': '',
    'last_saved_bytes': 0,
}

def _db_maintenance_set(**updates):
    with _DB_MAINTENANCE_STATE_LOCK:
        _DB_MAINTENANCE_STATE.update(updates)
        _DB_MAINTENANCE_STATE['updated_at'] = utc_iso()
        return dict(_DB_MAINTENANCE_STATE)

def _db_maintenance_snapshot():
    with _DB_MAINTENANCE_STATE_LOCK:
        return dict(_DB_MAINTENANCE_STATE)

_SCAN_STATE_LOCK = threading.RLock()
_SCAN_STATE = {'running':False,'paused':False,'cancel_requested':False,'force':False,'phase':'idle','started_at':'','updated_at':'','finished_at':'','files_total':0,'files_processed':0,'discovered':0,'processed':0,'added':0,'updated':0,'skipped':0,'failed':0,'current_file':'','last_error':'','last_scan_at':''}
_SCAN_PAUSE = threading.Event()
_SCAN_CANCEL = threading.Event()
def _scan_state_set(**updates):
    with _SCAN_STATE_LOCK:
        _SCAN_STATE.update(updates); _SCAN_STATE['updated_at']=utc_iso(); return dict(_SCAN_STATE)
def _scan_state_snapshot():
    with _SCAN_STATE_LOCK: return dict(_SCAN_STATE)
def _storage_status(path=None):
    """Check host free space and perform a real write probe for quota exhaustion."""
    target = os.path.abspath(path or DATA_DIR)
    total = used = free = 0
    details = []
    state = 'unknown'
    try:
        total, used, free = shutil.disk_usage(target)
        state = 'critical' if free < CRITICAL_FREE_BYTES else ('warning' if free < MIN_FREE_BYTES else 'ok')
    except OSError as exc:
        details.append(f'disk_usage: {type(exc).__name__}: {exc}')
        state = 'critical'

    writable = False
    probe_path = None
    try:
        os.makedirs(target, exist_ok=True)
        fd, probe_path = tempfile.mkstemp(prefix='.camera-write-probe-', dir=target)
        try:
            os.write(fd, b'camera-dashboard-write-probe\\n')
            os.fsync(fd)
            writable = True
        finally:
            os.close(fd)
    except OSError as exc:
        details.append(f'write_probe: {type(exc).__name__}: {exc}')
        if exc.errno in (errno.ENOSPC, getattr(errno, 'EDQUOT', -1), errno.EROFS, errno.EACCES, errno.EPERM):
            state = 'critical'
    finally:
        if probe_path:
            try:
                os.unlink(probe_path)
            except OSError as exc:
                details.append(f'probe_cleanup: {type(exc).__name__}: {exc}')

    if not writable and state == 'ok':
        state = 'critical'
    return {
        'ok': state not in {'critical', 'unknown'} and writable,
        'state': state, 'total_bytes': total, 'used_bytes': used,
        'free_bytes': free, 'free_gb': round(free / 1073741824, 2),
        'min_free_bytes': MIN_FREE_BYTES, 'critical_free_bytes': CRITICAL_FREE_BYTES,
        'writable_probe': writable, 'detail': '; '.join(details)[:1000]
    }


def _storage_allows_write(extra_bytes=0, operation='database write'):
    st = _storage_status()
    if not st['ok']:
        log.critical('[Storage] Refusing %s: state=%s free=%.2f GB writable_probe=%s detail=%s',
                     operation, st.get('state'), st.get('free_gb', 0),
                     st.get('writable_probe'), st.get('detail', ''))
        return False, st
    if st['free_bytes'] < max(MIN_FREE_BYTES, int(extra_bytes or 0)):
        log.warning('[Storage] Low disk before %s: %.2f GB free', operation, st.get('free_gb', 0))
        return False, st
    if st['state'] == 'warning':
        log.warning('[Storage] Warning before %s: %.2f GB free', operation, st.get('free_gb', 0))
    return True, st


_VALIDATION_ACTIVE_LOCK = threading.Lock()
_VALIDATION_ACTIVE_COUNT = 0
# v181: scans and validation share SQLite; longer bounded waits prevent a transient
# validation claim from aborting a multi-thousand-record discovery pass.
_DB_BUSY_RETRIES = max(12, min(30, int(os.getenv('CAMERA_DB_BUSY_RETRIES', '20'))))
_DB_BUSY_SLEEP = max(0.10, min(0.50, float(os.getenv('CAMERA_DB_BUSY_SLEEP', '0.15'))))
# v135: background writers are deliberately rate-limited so validation,
# retention, health and audit work cannot continuously occupy SQLite while a
# human is using the dashboard. Foreground/API writes always get priority.
_DB_BACKGROUND_WRITE_INTERVAL = max(0.25, float(os.getenv('CAMERA_DB_BACKGROUND_WRITE_INTERVAL', '0.75')))
_DB_UI_PRIORITY_WINDOW = max(0.50, float(os.getenv('CAMERA_DB_UI_PRIORITY_WINDOW', '2.00')))
_DB_BACKGROUND_LABELS = (
    'validation', 'health', 'retention', 'audit', 'metadata', 'worker',
    'recovery', 'scan history', 'inventory', 'backup', 'feature', 'discovery',
    'bulk camera upsert'
)
_DB_BG_RATE_LOCK = threading.Lock()
_DB_LAST_BACKGROUND_WRITE = 0.0
_UI_ACTIVITY_LOCK = threading.Lock()
_UI_LAST_ACTIVITY = 0.0

def _db_mark_ui_activity():
    global _UI_LAST_ACTIVITY
    with _UI_ACTIVITY_LOCK:
        _UI_LAST_ACTIVITY = time.monotonic()

def _db_ui_recent():
    with _UI_ACTIVITY_LOCK:
        return (time.monotonic() - _UI_LAST_ACTIVITY) < _DB_UI_PRIORITY_WINDOW

def _db_is_background_label(label):
    low = str(label or '').strip().lower()
    return any(token in low for token in _DB_BACKGROUND_LABELS)

def _db_wait_background_slot(label):
    """Yield background DB writers so foreground dashboard actions stay responsive."""
    global _DB_LAST_BACKGROUND_WRITE
    while not _shutdown_requested.is_set():
        if _DB_BACKUP_ACTIVE.is_set():
            _shutdown_requested.wait(0.10)
            continue
        now = time.monotonic()
        with _DB_BG_RATE_LOCK:
            wait_for = _DB_BACKGROUND_WRITE_INTERVAL - (now - _DB_LAST_BACKGROUND_WRITE)
            if wait_for <= 0:
                if _db_ui_recent():
                    wait_for = min(0.35, _DB_BACKGROUND_WRITE_INTERVAL)
                else:
                    _DB_LAST_BACKGROUND_WRITE = now
                    return
        if _shutdown_requested.wait(max(0.03, wait_for)):
            return


# v102: short-lived read cache prevents many dashboard readers from repeatedly
# scanning large SQLite tables while validation is writing. It never caches writes.
_METRICS_CACHE_LOCK = threading.Lock()
_METRICS_CACHE = {}
def _cached_read(key, ttl, loader):
    now=time.monotonic()
    with _METRICS_CACHE_LOCK:
        item=_METRICS_CACHE.get(key)
        if item and now-item[0] < ttl:
            return item[1]
    value=loader()
    with _METRICS_CACHE_LOCK:
        _METRICS_CACHE[key]=(time.monotonic(), value)
    return value

def _invalidate_read_cache(prefix=None):
    with _METRICS_CACHE_LOCK:
        if prefix is None:
            _METRICS_CACHE.clear()
        else:
            for k in list(_METRICS_CACHE):
                if str(k).startswith(prefix): _METRICS_CACHE.pop(k,None)

_DB_WRITE_STATE_LOCK = threading.RLock()
_DB_WRITE_STATE = {'active': False, 'label': '', 'started': 0.0, 'thread': 0}
_DB_SLOW_WRITE_SECONDS = max(1.0, float(os.getenv('CAMERA_DB_SLOW_WRITE_SECONDS', '2.0')))

def _db_write_state() -> dict:
    with _DB_WRITE_STATE_LOCK:
        return dict(_DB_WRITE_STATE)

def _v136_sql_is_write(sql: str) -> bool:
    text = str(sql or '').lstrip()
    if not text: return False
    while text.startswith('--'):
        nl = text.find('\n')
        if nl < 0: return False
        text = text[nl+1:].lstrip()
    m = re.match(r'([A-Za-z]+)', text)
    return bool(m and m.group(1).upper() in {'INSERT','UPDATE','DELETE','REPLACE','CREATE','ALTER','DROP','VACUUM','REINDEX'})

class _V136SQLiteConnection(sqlite3.Connection):
    """Serialize legacy direct execute()+commit() writes with the app writer lock."""
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs); self._v136_auto_writer_lock=False
    def _begin_write(self):
        if not self._v136_auto_writer_lock:
            _DB_WRITE_LOCK.acquire(); self._v136_auto_writer_lock=True
    def _abort_auto_writer_lock(self):
        # A failed direct write must not leave this thread holding the process-wide
        # RLock indefinitely. Roll back the partial transaction before releasing it.
        if self._v136_auto_writer_lock:
            try:
                super().rollback()
            except Exception:
                pass
            self._v136_auto_writer_lock = False
            try:
                _DB_WRITE_LOCK.release()
            except RuntimeError:
                pass
    def execute(self,sql,parameters=()):
        if _v136_sql_is_write(sql): self._begin_write()
        try:
            return super().execute(sql,parameters)
        except Exception:
            self._abort_auto_writer_lock()
            raise
    def executemany(self,sql,seq_of_parameters):
        if _v136_sql_is_write(sql): self._begin_write()
        try:
            return super().executemany(sql,seq_of_parameters)
        except Exception:
            self._abort_auto_writer_lock()
            raise
    def executescript(self,sql_script):
        self._begin_write()
        try:
            return super().executescript(sql_script)
        except Exception:
            self._abort_auto_writer_lock()
            raise
        finally:
            if self._v136_auto_writer_lock:
                self._v136_auto_writer_lock=False; _DB_WRITE_LOCK.release()
    def commit(self):
        auto=self._v136_auto_writer_lock
        try:
            return super().commit()
        except Exception:
            try: super().rollback()
            except Exception: pass
            raise
        finally:
            if auto:
                self._v136_auto_writer_lock=False; _DB_WRITE_LOCK.release()
    def rollback(self):
        auto=self._v136_auto_writer_lock
        try: return super().rollback()
        finally:
            if auto:
                self._v136_auto_writer_lock=False; _DB_WRITE_LOCK.release()
    def close(self):
        auto=self._v136_auto_writer_lock
        try: return super().close()
        finally:
            if auto:
                self._v136_auto_writer_lock=False; _DB_WRITE_LOCK.release()

def _db_conn() -> sqlite3.Connection:
    if not getattr(_db_local, 'conn', None):
        c = sqlite3.connect(DB_PATH, timeout=2.0, check_same_thread=False, factory=_V136SQLiteConnection)
        # Keep the connection timeout short. _db_retry_write() owns the retry
        # policy; a 30-second SQLite busy timeout made one small heartbeat wait
        # half a minute before our retry logic even got a chance to run.
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA synchronous=NORMAL')
        c.execute('PRAGMA busy_timeout=2000')
        c.execute('PRAGMA wal_autocheckpoint=1000')
        c.execute('PRAGMA cache_size=-16384')
        c.execute('PRAGMA temp_store=MEMORY')
        c.row_factory = sqlite3.Row
        _db_local.conn = c
    return _db_local.conn

def _db_close_thread_connection():
    conn=getattr(_db_local,'conn',None)
    if conn is not None:
        try: conn.close()
        except Exception: pass
        try: delattr(_db_local,'conn')
        except Exception: pass

def _db_retry_write(fn, label='database write'):
    """Serialize short writes and tolerate transient/external SQLite locks.

    v125 also records write duration so a future long transaction can be
    identified instead of treating every 'database is locked' message as a
    generic retry problem.
    """
    if _shutdown_requested.is_set() and _db_is_background_label(label):
        log.debug('[SQLite] %s skipped: shutdown requested', label)
        return None
    last = None
    for attempt in range(_DB_BUSY_RETRIES):
        if _shutdown_requested.is_set() and _db_is_background_label(label):
            log.debug('[SQLite] %s cancelled cleanly during shutdown', label)
            return None
        try:
            is_background = _db_is_background_label(label)
            if is_background:
                _db_wait_background_slot(label)
                # Re-check after throttling: a backup may have started between the
                # slot wait and lock acquisition.
                while _DB_BACKUP_ACTIVE.is_set() and not _shutdown_requested.is_set():
                    _shutdown_requested.wait(0.10)
                if _shutdown_requested.is_set():
                    raise sqlite3.OperationalError(f'{label} cancelled during shutdown')
            started = time.monotonic()
            lock_acquired = False
            if is_background:
                lock_acquired = _DB_WRITE_LOCK.acquire(timeout=max(2.0, min(5.0, float(os.getenv('CAMERA_DB_WRITE_LOCK_TIMEOUT', '3.0')))))
                if not lock_acquired:
                    # Record the actual contention cause. Previously this branch
                    # retried without setting `last`, producing the misleading
                    # final message `failed after 8 retries: None`.
                    last = sqlite3.OperationalError(
                        f'{label}: timed out waiting for the application SQLite write lock '
                        f'(attempt {attempt + 1}/{_DB_BUSY_RETRIES})'
                    )
                    if attempt in (0, 3, 6):
                        with _DB_WRITE_STATE_LOCK:
                            writer_state = dict(_DB_WRITE_STATE)
                        holder = str(writer_state.get('label') or 'untracked/direct lock holder')
                        age = (max(0.0, time.monotonic() - float(writer_state.get('started') or 0.0))
                               if writer_state.get('active') else 0.0)
                        log.warning(
                            '[SQLite] %s waiting for application write lock; retry %d/%d; '
                            'tracked_holder=%s holder_age=%.2fs',
                            label, attempt + 1, _DB_BUSY_RETRIES, holder, age)
                    if _shutdown_requested.wait(min(0.5, 0.15 * (attempt + 1))):
                        break
                    continue
            else:
                _DB_WRITE_LOCK.acquire(); lock_acquired=True
            try:
                with _DB_WRITE_STATE_LOCK:
                    _DB_WRITE_STATE.update(active=True,label=str(label),started=started,thread=threading.get_ident())
                try:
                    conn=getattr(_db_local,'conn',None)
                    if conn is not None and conn.in_transaction: conn.rollback()
                    result=fn(); _invalidate_read_cache('stats')
                    elapsed=time.monotonic()-started
                    if elapsed >= _DB_SLOW_WRITE_SECONDS:
                        log.warning('[SQLite] slow write: %s took %.2fs',label,elapsed)
                    return result
                finally:
                    with _DB_WRITE_STATE_LOCK:
                        _DB_WRITE_STATE.update(active=False,label='',started=0.0,thread=0)
            finally:
                if lock_acquired: _DB_WRITE_LOCK.release()
        except sqlite3.OperationalError as exc:
            last = exc
            msg = str(exc).lower()
            # Background writes are intentionally cancellable at shutdown. Treat
            # cancellation as a normal lifecycle outcome, not a failed DB write.
            if (_shutdown_requested.is_set() and _db_is_background_label(label)
                    and ('cancelled during shutdown' in msg or 'shutdown requested' in msg)):
                log.debug('[SQLite] %s stopped cooperatively during shutdown', label)
                return None
            if any(token in msg for token in ('disk is full', 'database or disk is full', 'disk quota exceeded', 'no space left')):
                log.critical('[Storage] SQLite write failed because storage is full/quota-exhausted; operation=%s error=%s',
                             label, type(exc).__name__)
                raise
            if 'locked' not in msg and 'busy' not in msg:
                raise
            try:
                conn = getattr(_db_local, 'conn', None)
                if conn is not None:
                    conn.rollback()
            except Exception:
                pass
            if attempt + 1 < _DB_BUSY_RETRIES:
                delay = min(2.0, _DB_BUSY_SLEEP * (attempt + 1))
                if attempt in (0, 4, 9, 14):
                    log.warning('[SQLite] %s busy/locked; retry %d/%d in %.2fs',
                                label, attempt + 1, _DB_BUSY_RETRIES, delay)
                if _shutdown_requested.wait(delay):
                    break
    log.error('[SQLite] %s failed after %d retries: %s', label, _DB_BUSY_RETRIES, last)
    raise last if last else sqlite3.OperationalError(f'{label} failed')


def _cleanup_old_database_backups(keep: str = DB_BACKUPS[0]) -> list[str]:
    """Remove legacy rotating backup files, keeping only the verified latest backup."""
    removed = []
    base = os.path.basename(DB_BACKUP_GLOB_PREFIX)
    directory = os.path.dirname(DB_BACKUP_GLOB_PREFIX) or '.'
    try:
        for name in os.listdir(directory):
            if not name.startswith(base):
                continue
            path = os.path.join(directory, name)
            if os.path.abspath(path) == os.path.abspath(keep):
                continue
            # Only remove known backup forms: .bak.N and temporary backup files.
            suffix = name[len(base):]
            if (suffix == '' or suffix.startswith('.') or suffix in ('-wal','-shm')
                    or '.backup.' in suffix):
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                        removed.append(path)
                except OSError as exc:
                    log.warning('Could not remove old database backup %s: %s', path, exc)
    except OSError as exc:
        log.warning('Could not enumerate old database backups: %s', exc)
    return removed


def _cleanup_stale_backup_temp_files(min_age_seconds: float = 3600.0) -> list[str]:
    """Remove orphaned SQLite backup temp files/sidecars older than one hour.

    Current backup work is protected by _DB_BACKUP_LOCK and this age threshold;
    live DB/WAL/SHM files and the persistent verified .bak are never touched.
    """
    removed = []
    directory = os.path.dirname(DB_PATH) or '.'
    prefix = os.path.basename(DB_PATH) + '.backup.'
    cutoff = time.time() - max(60.0, float(min_age_seconds))
    try:
        for name in os.listdir(directory):
            if not name.startswith(prefix) or '.tmp' not in name:
                continue
            path = os.path.join(directory, name)
            try:
                if not os.path.isfile(path) or os.path.getmtime(path) >= cutoff:
                    continue
                os.remove(path)
                removed.append(path)
            except OSError as exc:
                log.warning('[SQLite] Could not remove stale backup temp %s: %s', name, exc)
    except OSError as exc:
        log.warning('[SQLite] Could not inspect backup temp files: %s', exc)
    if removed:
        log.info('[SQLite] Removed %d stale backup temp/sidecar file(s)', len(removed))
    return removed


def _remove_sqlite_temp_family(path: str) -> None:
    """Remove a temporary database and all SQLite sidecars created beside it."""
    for candidate in (path, path + '-wal', path + '-shm', path + '-journal'):
        try:
            os.remove(candidate)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning('[SQLite] Could not remove temporary file %s: %s', candidate, exc)


def _publish_independent_backup_history(primary_backup: str) -> bool:
    """Copy a verified backup to a separately configured history directory.

    Set CAMERA_BACKUP_HISTORY_DIR to persistent storage independent from the
    project/source directory (ideally a different volume). Keep a bounded number
    of copies. A secondary-history failure never invalidates the primary backup.
    """
    target_dir = os.getenv('CAMERA_BACKUP_HISTORY_DIR', '').strip()
    if not target_dir:
        return False
    target_dir = os.path.abspath(os.path.expanduser(target_dir))
    try:
        if os.path.commonpath([target_dir, os.path.abspath(DATA_DIR)]) == os.path.abspath(DATA_DIR):
            log.warning('[Backup history] configured directory is inside DATA_DIR; it is not independent storage: %s', target_dir)
        os.makedirs(target_dir, mode=0o700, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        final_path = os.path.join(target_dir, f'camera_db-{stamp}-{os.getpid()}.sqlite.bak')
        temp_path = final_path + '.tmp'
        shutil.copy2(primary_backup, temp_path)
        if not _sqlite_quick_check_ok(temp_path):
            raise sqlite3.DatabaseError('secondary backup failed quick_check')
        with open(temp_path, 'rb') as handle:
            os.fsync(handle.fileno())
        os.replace(temp_path, final_path)
        keep = max(2, min(30, int(os.getenv('CAMERA_BACKUP_HISTORY_KEEP', '5'))))
        files = sorted(
            (os.path.join(target_dir, name) for name in os.listdir(target_dir)
             if name.startswith('camera_db-') and name.endswith('.sqlite.bak')),
            key=lambda item: os.path.getmtime(item), reverse=True)
        for stale in files[keep:]:
            try: os.remove(stale)
            except OSError as exc:
                log.warning('[Backup history] could not remove old copy %s: %s', stale, exc)
        log.info('[Backup history] independent verified copy created: %s (retained=%d)', final_path, min(len(files) + 1, keep))
        return True
    except Exception as exc:
        log.error('[Backup history] secondary copy failed; primary backup remains valid: %s: %s',
                  type(exc).__name__, exc)
        try:
            if 'temp_path' in locals() and os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        return False


def db_create_backup(clean_old: bool = True, _shutdown: bool = False) -> bool:
    """Create one verified SQLite backup and optionally remove legacy backups.

    The old backup is never removed until the new backup has been fully copied,
    committed, and passed integrity_check. This guarantees that a failed backup
    does not destroy the last known-good recovery point.
    """
    tmp=f"{DB_PATH}.backup.{os.getpid()}.{threading.get_ident()}.tmp"
    dest=None
    source=None
    if _shutdown_requested.is_set() and not _shutdown:
        log.info('SQLite backup skipped: shutdown requested')
        return False
    # Never wait two minutes merely to acquire the backup mutex.  A competing
    # backup will finish on its own; this caller can retry on the next cycle.
    acquire_timeout = 3.0 if (_shutdown or _shutdown_requested.is_set()) else 5.0
    if not _DB_BACKUP_LOCK.acquire(timeout=acquire_timeout):
        log.info('SQLite backup skipped: another backup is already running')
        return False
    try:
        _DB_BACKUP_ACTIVE.set()
        # Let any in-flight app writer finish before starting the snapshot, then
        # pause background writes until the backup copy is published or aborted.
        with _DB_WRITE_LOCK:
            current = getattr(_db_local, 'conn', None)
            if current is not None:
                try: current.commit()
                except Exception:
                    try: current.rollback()
                    except Exception: pass
        _cleanup_stale_backup_temp_files()
        allowed, storage = _storage_allows_write(max(64*1024*1024, os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0), 'database backup')
        if not allowed: return False
        if not os.path.exists(DB_PATH) or os.path.getsize(DB_PATH) <= 0:
            log.critical('[DB Protection] Refusing backup: live DB is missing/empty')
            return False
        # Flush any transaction owned by the caller thread before opening the
        # backup snapshot. The actual copy deliberately does NOT hold the global
        # writer lock: a large DB must not freeze validation for minutes. Use a
        # dedicated read connection so a request thread's transaction cannot be
        # accidentally carried into the backup operation.
        with _DB_WRITE_LOCK:
            current = getattr(_db_local, 'conn', None)
            if current is not None:
                try: current.execute('PRAGMA wal_checkpoint(PASSIVE)')
                except Exception: pass
        source = sqlite3.connect(DB_PATH, timeout=5.0, check_same_thread=False)
        source.row_factory = sqlite3.Row
        source.execute('PRAGMA busy_timeout=5000')
        try: os.remove(tmp)
        except FileNotFoundError: pass
        dest=sqlite3.connect(tmp, timeout=10)
        dest.execute('PRAGMA journal_mode=DELETE')
        dest.execute('PRAGMA synchronous=FULL')
        # sqlite3.Connection.backup() can otherwise spend an unbounded amount
        # of time copying a large DB.  Abort cleanly at the configured deadline
        # and leave the last verified .bak untouched.
        backup_deadline = time.monotonic() + max(
            10.0,
            float(os.getenv('CAMERA_BACKUP_TIMEOUT_SECONDS', '1800'))
        )
        last_progress_log = [time.monotonic()]
        def _backup_progress(status, remaining, total):
            now = time.monotonic()
            if _shutdown_requested.is_set():
                raise RuntimeError('backup cancelled: shutdown requested')
            if now >= backup_deadline:
                raise TimeoutError('SQLite backup deadline exceeded')
            if now - last_progress_log[0] >= 60.0:
                copied = max(0, int(total) - int(remaining))
                log.info('[SQLite] Backup progress: %d/%d pages copied; %d remaining',
                         copied, int(total), int(remaining))
                last_progress_log[0] = now
        source.backup(dest, pages=1000, progress=_backup_progress, sleep=0.05)
        dest.commit(); dest.close(); dest=None
        # Do not run a second full integrity scan here. That doubled backup
        # latency on large databases and could make the backup appear hung.
        # The default publication check is structural/fast; operators can opt
        # into the heavier quick/full verification when needed.
        verify_mode = os.getenv('CAMERA_BACKUP_VERIFY_MODE', 'fast').strip().lower()
        if verify_mode == 'full':
            verified = _sqlite_integrity_ok(tmp)
        elif verify_mode == 'quick':
            verified = _sqlite_quick_check_ok(tmp)
        else:
            verified = _sqlite_fast_probe_ok(tmp)
        if not verified:
            raise sqlite3.DatabaseError(f'backup {verify_mode} verification failed')
        with _DB_WRITE_LOCK:
            os.replace(tmp, DB_BACKUPS[0])
            if clean_old:
                _cleanup_old_database_backups(DB_BACKUPS[0])
        log.info('SQLite backup created: %s (primary verified backup)', DB_BACKUPS[0])
        _publish_independent_backup_history(DB_BACKUPS[0])
        return True
    except TimeoutError as e:
        log.warning('SQLite backup deferred/aborted: %s', e)
        return False
    except (OSError,sqlite3.Error,RuntimeError) as e:
        log.error('SQLite backup failed: %s',e)
        return False
    finally:
        if dest:
            try: dest.close()
            except Exception: pass
        try: source.close()
        except Exception: pass
        _remove_sqlite_temp_family(tmp)
        _DB_BACKUP_ACTIVE.clear()
        _DB_BACKUP_LOCK.release()


def db_compact_and_backup(actor: str = 'system') -> dict:
    """Safely shrink the live SQLite DB, checkpoint WAL, and leave one backup.

    The validator queue is paused while the maintenance operation waits for
    already-running validation cycles to finish. VACUUM is only attempted after
    a verified safety backup exists. If compaction fails, that safety backup is
    retained rather than deleting recovery data.
    """
    if not _DB_MAINTENANCE_LOCK.acquire(blocking=False):
        # A previous maintenance request is already controlling the database.
        # Every dashboard client can see this shared state through metrics.
        snap = _db_maintenance_snapshot()
        return {'ok': True, 'running': True, 'detail': 'Database maintenance is already running; the existing operation will continue safely.',
                'maintenance': snap}

    global _VALIDATION_ACTIVE_COUNT
    started = time.monotonic()
    safety_backup = False
    _db_maintenance_set(active=True, state='busy', phase='waiting for active validation to finish',
                         actor=str(actor or 'system')[:80], started_at=utc_iso(),
                         completed_at='', last_result='', last_detail='', last_saved_bytes=0)
    old_size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    old_wal = os.path.getsize(DB_PATH + '-wal') if os.path.exists(DB_PATH + '-wal') else 0
    _DB_MAINTENANCE_ACTIVE.set()
    try:
        # Prevent the queue from claiming new validation work.
        try:
            _validation_queue_paused.set()
        except Exception:
            pass

        # Wait for active validation cycles to drain so VACUUM is not competing
        # with dozens of camera-result writers.
        deadline = time.monotonic() + 180.0
        while True:
            with _VALIDATION_ACTIVE_LOCK:
                active = _VALIDATION_ACTIVE_COUNT
            if active <= 0 or time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        if active > 0:
            return {'ok': False, 'detail': 'Validation is still active; maintenance was cancelled safely.',
                    'active_validation_cycles': active}

        # First create a verified recovery point. Do not delete any old backup
        # until the final compacted backup has also succeeded.
        _db_maintenance_set(phase='creating safety backup')
        safety_backup = db_create_backup(clean_old=False)
        if not safety_backup:
            return {'ok': False, 'detail': 'Safety backup failed; database was not compacted.'}

        _db_maintenance_set(phase='checkpointing WAL and shrinking database')
        with _DB_WRITE_LOCK:
            conn = sqlite3.connect(DB_PATH, timeout=180, check_same_thread=False)
            try:
                conn.execute('PRAGMA busy_timeout=180000')
                before_pages = int(conn.execute('PRAGMA page_count').fetchone()[0] or 0)
                before_free = int(conn.execute('PRAGMA freelist_count').fetchone()[0] or 0)
                checkpoint = conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                if checkpoint and int(checkpoint[0] or 0) != 0:
                    raise sqlite3.OperationalError(
                        f'WAL checkpoint is busy (busy={checkpoint[0]}, log_pages={checkpoint[1]}, checkpointed_pages={checkpoint[2]}); close other active requests and retry'
                    )
                # VACUUM in WAL mode can produce a WAL as large as (or larger
                # than) the database. Skip it when there is little reclaimable
                # space, and checkpoint immediately after a real compaction.
                page_size = int(conn.execute('PRAGMA page_size').fetchone()[0] or 4096)
                reclaimable_bytes = before_free * page_size
                vacuum_threshold = max(64 * 1024 * 1024, int(before_pages * page_size * 0.03))
                if reclaimable_bytes >= vacuum_threshold:
                    conn.execute('VACUUM')
                    conn.commit()
                    post_vacuum_checkpoint = conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                    if post_vacuum_checkpoint and int(post_vacuum_checkpoint[0] or 0) != 0:
                        log.warning('[DB Maintenance] Post-VACUUM WAL checkpoint busy; WAL will be left intact for SQLite recovery')
                else:
                    log.info('[DB Maintenance] Skipping VACUUM: only %.1f MB reclaimable; threshold %.1f MB',
                             reclaimable_bytes / 1048576, vacuum_threshold / 1048576)
                after_pages = int(conn.execute('PRAGMA page_count').fetchone()[0] or 0)
                after_free = int(conn.execute('PRAGMA freelist_count').fetchone()[0] or 0)
                log.info('[DB Maintenance] Page audit: page_size=%s pages=%d->%d free_pages=%d->%d',
                         page_size, before_pages, after_pages, before_free, after_free)
            finally:
                conn.close()

        # Re-checkpoint with a fresh connection after closing the maintenance
        # connection, so committed VACUUM pages do not remain in a giant WAL.
        try:
            checkpoint_conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
            try:
                checkpoint_conn.execute('PRAGMA busy_timeout=30000')
                final_checkpoint = checkpoint_conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                if final_checkpoint and int(final_checkpoint[0] or 0) != 0:
                    log.warning('[DB Maintenance] Final WAL checkpoint busy: %s', tuple(final_checkpoint))
            finally:
                checkpoint_conn.close()
        except Exception as checkpoint_error:
            log.warning('[DB Maintenance] Final WAL checkpoint failed safely: %s', checkpoint_error)

        new_size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
        new_wal = os.path.getsize(DB_PATH + '-wal') if os.path.exists(DB_PATH + '-wal') else 0
        if not _sqlite_integrity_ok(DB_PATH):
            raise sqlite3.DatabaseError('live database failed integrity check after VACUUM')

        # Final backup is the only persistent backup left after a successful
        # compact operation. If this fails, keep the safety backup.
        _db_maintenance_set(phase='creating final verified backup')
        final_backup = db_create_backup(clean_old=True)
        if not final_backup:
            return {'ok': False,
                    'detail': 'Database was compacted, but final backup failed. Existing safety backup was retained.',
                    'old_size': old_size, 'new_size': new_size}

        removed = _cleanup_old_database_backups(DB_BACKUPS[0])
        removed_temp = _cleanup_stale_backup_temp_files(min_age_seconds=60.0)
        saved = max(0, old_size - new_size)
        pct = round((saved / old_size) * 100, 2) if old_size else 0.0
        log.info('[DB Maintenance] VACUUM complete: %d -> %d bytes (saved %d / %s%%); WAL %d -> %d bytes',
                 old_size, new_size, saved, pct, old_wal, new_wal)
        _db_maintenance_set(active=False, state='free', phase='idle', completed_at=utc_iso(),
                             last_result='success',
                             last_detail=f'Database shrink complete; saved {saved / 1048576:.1f} MB.',
                             last_saved_bytes=saved)
        result = {
            'ok': True,
            'old_size': old_size,
            'new_size': new_size,
            'saved_bytes': saved,
            'saved_percent': pct,
            'old_wal_bytes': old_wal,
            'new_wal_bytes': new_wal,
            'backup': DB_BACKUPS[0],
            'removed_old_backups': len(removed),
            'removed_stale_temp_files': len(removed_temp),
            'elapsed_seconds': round(time.monotonic() - started, 1),
            'maintenance': _db_maintenance_snapshot(),
        }
        return result
    except Exception as exc:
        log.exception('[DB Maintenance] compact failed')
        _db_maintenance_set(active=False, state='free', phase='idle', completed_at=utc_iso(),
                             last_result='failed', last_detail=str(exc)[:500])
        # Never remove the last verified backup on failure.
        return {'ok': False, 'detail': str(exc), 'safety_backup': safety_backup,
                'maintenance': _db_maintenance_snapshot()}
    finally:
        # If an early safe cancellation/return happened before success/failure
        # state was written, publish a clean FREE state for other clients.
        snap = _db_maintenance_snapshot()
        if snap.get('active'):
            _db_maintenance_set(active=False, state='free', phase='idle',
                                 completed_at=utc_iso(), last_result='cancelled',
                                 last_detail=snap.get('last_detail') or 'Database maintenance ended safely.')
        _DB_MAINTENANCE_ACTIVE.clear()
        try:
            _validation_queue_paused.clear()
        except Exception:
            pass
        _DB_MAINTENANCE_LOCK.release()


def start_database_backup_worker(delay: float = 60.0,
                                 interval: float = 1800.0) -> threading.Thread:
    """Run verified rotating backups without blocking app startup or requests."""
    def _run():
        _touch_worker('db-backup', 'starting')
        if _shutdown_requested.wait(max(0.0, delay)):
            _touch_worker('db-backup', 'stopped', 'shutdown requested')
            return
        while not _shutdown_requested.is_set():
            try:
                _touch_worker('db-backup', 'running')
                if _shutdown_requested.is_set():
                    break
                ok = db_create_backup()
                _touch_worker('db-backup', 'idle' if ok else 'error',
                              '' if ok else 'backup failed')
            except Exception as e:
                log.error('SQLite backup worker error: %s', e)
                _touch_worker('db-backup', 'error', type(e).__name__)
            if _shutdown_requested.wait(max(0.0, interval)):
                break
        _touch_worker('db-backup', 'stopped', 'shutdown requested')
    t = threading.Thread(target=_run, daemon=True, name='db-backup')
    t.start()
    return t




def _advanced_schema(c):
    """Create and safely migrate the advanced schema.

    Older database files can already contain these tables with fewer columns.
    SQLite's CREATE TABLE IF NOT EXISTS does not migrate an existing table, so
    indexes on newly introduced columns (notably created_at) can otherwise
    crash startup. This migration is idempotent and runs on every startup.
    """
    c.executescript("""
    CREATE TABLE IF NOT EXISTS device_fingerprints(device_key TEXT PRIMARY KEY,camera_id TEXT NOT NULL DEFAULT '',fingerprint TEXT NOT NULL DEFAULT '',vendor TEXT NOT NULL DEFAULT '',model TEXT NOT NULL DEFAULT '',serial TEXT NOT NULL DEFAULT '',first_seen TEXT NOT NULL DEFAULT (datetime('now')),last_seen TEXT NOT NULL DEFAULT (datetime('now')),confidence REAL NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS device_evidence(id INTEGER PRIMARY KEY AUTOINCREMENT,camera_id TEXT NOT NULL,evidence_type TEXT NOT NULL,result TEXT NOT NULL DEFAULT '',value TEXT NOT NULL DEFAULT '',confidence REAL NOT NULL DEFAULT 0,created_at TEXT NOT NULL DEFAULT (datetime('now')));
    CREATE TABLE IF NOT EXISTS device_events(id INTEGER PRIMARY KEY AUTOINCREMENT,camera_id TEXT NOT NULL,event_type TEXT NOT NULL,old_value TEXT NOT NULL DEFAULT '',new_value TEXT NOT NULL DEFAULT '',detail TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL DEFAULT (datetime('now')));
    CREATE TABLE IF NOT EXISTS stream_observations(id INTEGER PRIMARY KEY AUTOINCREMENT,camera_id TEXT NOT NULL,rtsp_url TEXT NOT NULL DEFAULT '',codec TEXT NOT NULL DEFAULT '',resolution TEXT NOT NULL DEFAULT '',fps REAL NOT NULL DEFAULT 0,bitrate INTEGER NOT NULL DEFAULT 0,audio INTEGER NOT NULL DEFAULT 0,video INTEGER NOT NULL DEFAULT 0,latency_ms INTEGER NOT NULL DEFAULT 0,first_frame INTEGER NOT NULL DEFAULT 0,transport TEXT NOT NULL DEFAULT '',failure_reason TEXT NOT NULL DEFAULT '',quality_score REAL NOT NULL DEFAULT 0,observed_at TEXT NOT NULL DEFAULT (datetime('now')));
    CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT,actor TEXT NOT NULL DEFAULT 'system',action TEXT NOT NULL,object_type TEXT NOT NULL DEFAULT '',object_id TEXT NOT NULL DEFAULT '',detail TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL DEFAULT (datetime('now')));
    CREATE TABLE IF NOT EXISTS camera_trash(camera_id TEXT PRIMARY KEY,deleted_at TEXT NOT NULL DEFAULT (datetime('now')),reason TEXT NOT NULL DEFAULT '',previous_group TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS saved_filters(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT UNIQUE NOT NULL,query_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL DEFAULT (datetime('now')));
    CREATE TABLE IF NOT EXISTS device_tags(camera_id TEXT NOT NULL,tag TEXT NOT NULL,PRIMARY KEY(camera_id,tag));
    CREATE TABLE IF NOT EXISTS category_meta(category TEXT PRIMARY KEY,note TEXT NOT NULL DEFAULT '',locked INTEGER NOT NULL DEFAULT 0,color TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL DEFAULT (datetime('now')));
    CREATE TABLE IF NOT EXISTS scheduled_jobs(id TEXT PRIMARY KEY,name TEXT UNIQUE NOT NULL,job_type TEXT NOT NULL,interval_seconds INTEGER NOT NULL DEFAULT 21600,enabled INTEGER NOT NULL DEFAULT 1,last_run TEXT,next_run TEXT);
    """)

    # Legacy compatibility: v14/v15 used device_evidence.camera_id as the
    # PRIMARY KEY and stored one summary row per camera. v18+ stores multiple
    # evidence rows per camera, so CREATE TABLE IF NOT EXISTS cannot upgrade
    # that old schema. Detect the legacy uniqueness and rebuild the table
    # transactionally while preserving its existing evidence as individual rows.
    try:
        ev_info = c.execute('PRAGMA table_info(device_evidence)').fetchall()
        ev_cols = {row[1]: row for row in ev_info}
        legacy_camera_pk = bool(ev_cols.get('camera_id') and ev_cols['camera_id'][5] == 1)
        unique_camera_index = False
        for idx in c.execute('PRAGMA index_list(device_evidence)').fetchall():
            if len(idx) >= 3 and int(idx[2] or 0) == 1:
                idx_name = idx[1]
                idx_cols = [r[2] for r in c.execute(f'PRAGMA index_info({idx_name})').fetchall()]
                if idx_cols == ['camera_id']:
                    unique_camera_index = True
                    break
        if legacy_camera_pk or unique_camera_index or 'id' not in ev_cols or 'evidence_type' not in ev_cols:
            legacy_table = 'device_evidence_legacy_v18'
            c.execute(f'DROP TABLE IF EXISTS {legacy_table}')
            c.execute(f'ALTER TABLE device_evidence RENAME TO {legacy_table}')
            c.execute('''CREATE TABLE device_evidence(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                camera_id TEXT NOT NULL,
                evidence_type TEXT NOT NULL,
                result TEXT NOT NULL DEFAULT '',
                value TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )''')
            legacy_cols = {row[1] for row in c.execute(f'PRAGMA table_info({legacy_table})').fetchall()}
            if {'camera_id','auth_ok','http_ok','onvif_ok','vendor_identified','channels_found','rtsp_validated','confidence','state'} <= legacy_cols:
                mappings = [
                    ('auth_ok','authentication','ok'), ('http_ok','http','ok'),
                    ('onvif_ok','onvif','ok'), ('vendor_identified','vendor','identified'),
                    ('channels_found','channels','found'), ('rtsp_validated','rtsp','validated'),
                    ('state','state','value'),
                ]
                for field, etype, result in mappings:
                    c.execute(
                        f'''INSERT INTO device_evidence(camera_id,evidence_type,result,value,confidence,created_at)
                            SELECT camera_id, ?, ?, CAST({field} AS TEXT),
                                   COALESCE(confidence,0),
                                   COALESCE(updated_at,datetime('now'))
                            FROM {legacy_table} WHERE camera_id IS NOT NULL AND camera_id<>'' ''',
                        (etype, result)
                    )
            elif 'camera_id' in legacy_cols:
                # Unknown legacy shape: preserve one opaque row per old camera
                # rather than deleting data.
                c.execute(
                    f'''INSERT INTO device_evidence(camera_id,evidence_type,result,value,confidence)
                        SELECT camera_id, 'legacy', 'preserved', '', 0
                        FROM {legacy_table} WHERE camera_id IS NOT NULL AND camera_id<>'' '''
                )
            c.execute(f'DROP TABLE {legacy_table}')
            log.info('DB migration: rebuilt legacy device_evidence to multi-row schema')
    except Exception:
        log.exception('DB migration: legacy device_evidence rebuild failed')
        raise

    migrations = {
        'device_fingerprints': [
            ('camera_id', "TEXT NOT NULL DEFAULT ''"), ('fingerprint', "TEXT NOT NULL DEFAULT ''"),
            ('vendor', "TEXT NOT NULL DEFAULT ''"), ('model', "TEXT NOT NULL DEFAULT ''"),
            ('serial', "TEXT NOT NULL DEFAULT ''"), ('first_seen', "TEXT NOT NULL DEFAULT (datetime('now'))"),
            ('last_seen', "TEXT NOT NULL DEFAULT (datetime('now'))"), ('confidence', "REAL NOT NULL DEFAULT 0"),
        ],
        'device_evidence': [
            ('camera_id', "TEXT NOT NULL DEFAULT ''"), ('evidence_type', "TEXT NOT NULL DEFAULT ''"),
            ('result', "TEXT NOT NULL DEFAULT ''"), ('value', "TEXT NOT NULL DEFAULT ''"),
            ('confidence', "REAL NOT NULL DEFAULT 0"), ('created_at', "TEXT NOT NULL DEFAULT (datetime('now'))"),
        ],
        'device_events': [
            ('camera_id', "TEXT NOT NULL DEFAULT ''"), ('event_type', "TEXT NOT NULL DEFAULT ''"),
            ('old_value', "TEXT NOT NULL DEFAULT ''"), ('new_value', "TEXT NOT NULL DEFAULT ''"),
            ('detail', "TEXT NOT NULL DEFAULT ''"), ('created_at', "TEXT NOT NULL DEFAULT (datetime('now'))"),
        ],
        'stream_observations': [
            ('camera_id', "TEXT NOT NULL DEFAULT ''"), ('rtsp_url', "TEXT NOT NULL DEFAULT ''"),
            ('codec', "TEXT NOT NULL DEFAULT ''"), ('resolution', "TEXT NOT NULL DEFAULT ''"),
            ('fps', "REAL NOT NULL DEFAULT 0"), ('bitrate', "INTEGER NOT NULL DEFAULT 0"),
            ('audio', "INTEGER NOT NULL DEFAULT 0"), ('video', "INTEGER NOT NULL DEFAULT 0"),
            ('latency_ms', "INTEGER NOT NULL DEFAULT 0"), ('first_frame', "INTEGER NOT NULL DEFAULT 0"),
            ('transport', "TEXT NOT NULL DEFAULT ''"), ('failure_reason', "TEXT NOT NULL DEFAULT ''"),
            ('quality_score', "REAL NOT NULL DEFAULT 0"), ('observed_at', "TEXT NOT NULL DEFAULT (datetime('now'))"),
        ],
        'audit_log': [
            ('actor', "TEXT NOT NULL DEFAULT 'system'"), ('action', "TEXT NOT NULL DEFAULT ''"),
            ('object_type', "TEXT NOT NULL DEFAULT ''"), ('object_id', "TEXT NOT NULL DEFAULT ''"),
            ('detail', "TEXT NOT NULL DEFAULT ''"), ('created_at', "TEXT NOT NULL DEFAULT (datetime('now'))"),
        ],
        'camera_trash': [
            ('deleted_at', "TEXT NOT NULL DEFAULT (datetime('now'))"), ('reason', "TEXT NOT NULL DEFAULT ''"),
            ('previous_group', "TEXT NOT NULL DEFAULT ''"),
        ],
        'saved_filters': [
            ('name', "TEXT NOT NULL DEFAULT ''"), ('query_json', "TEXT NOT NULL DEFAULT '{}'"),
            ('created_at', "TEXT NOT NULL DEFAULT (datetime('now'))"),
        ],
        'category_meta': [
            ('note', "TEXT NOT NULL DEFAULT ''"), ('locked', "INTEGER NOT NULL DEFAULT 0"),
            ('color', "TEXT NOT NULL DEFAULT ''"), ('updated_at', "TEXT NOT NULL DEFAULT (datetime('now'))"),
        ],
        'scheduled_jobs': [
            ('name', "TEXT NOT NULL DEFAULT ''"), ('job_type', "TEXT NOT NULL DEFAULT ''"),
            ('interval_seconds', "INTEGER NOT NULL DEFAULT 21600"), ('enabled', "INTEGER NOT NULL DEFAULT 1"),
            ('last_run', "TEXT"), ('next_run', "TEXT"),
        ],
        'camera_health_history': [
            ('rtsp_score', "REAL NOT NULL DEFAULT 0.0"),
        ],
    }
    for table, cols in migrations.items():
        existing = {row[1] for row in c.execute(f'PRAGMA table_info({table})').fetchall()}
        for name, definition in cols:
            if name in existing:
                continue
            try:
                # SQLite only permits constant DEFAULT expressions when ALTER TABLE
                # adds a column. Older databases may therefore fail on defaults such
                # as DEFAULT (datetime('now')). Add those timestamp columns as nullable,
                # then backfill them explicitly. New tables above still use the richer
                # DEFAULT expression normally.
                alter_definition = definition
                needs_timestamp_backfill = "DEFAULT (datetime('now'))" in definition
                if needs_timestamp_backfill:
                    alter_definition = re.sub(r"\s+NOT NULL\s+DEFAULT\s*\(datetime\('now'\)\)", "", definition, flags=re.I)
                c.execute(f'ALTER TABLE {table} ADD COLUMN {name} {alter_definition}')
                if needs_timestamp_backfill:
                    c.execute(f"UPDATE {table} SET {name}=datetime('now') WHERE {name} IS NULL")
                existing.add(name)
                log.info('DB migration: added %s.%s', table, name)
            except sqlite3.OperationalError as exc:
                existing_now = {row[1] for row in c.execute(f'PRAGMA table_info({table})').fetchall()}
                if name not in existing_now:
                    raise RuntimeError(f'Database migration failed for {table}.{name}: {exc}') from exc
    # Normalize legacy health scoring after the migration has added rtsp_score.
    try:
        health_cols={r[1] for r in c.execute('PRAGMA table_info(camera_health_history)').fetchall()}
        if 'rtsp_score' in health_cols and 'candidate_score' in health_cols:
            c.execute('UPDATE camera_health_history SET rtsp_score=COALESCE(candidate_score,0) WHERE COALESCE(rtsp_score,0)=0')
    except sqlite3.Error:
        log.exception('health score compatibility migration failed')

    c.executescript("""
    CREATE TABLE IF NOT EXISTS camera_tags (camera_id TEXT NOT NULL, tag TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')), PRIMARY KEY(camera_id,tag));
    CREATE TABLE IF NOT EXISTS live_layouts (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, layout_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT (datetime('now')), updated_at TEXT NOT NULL DEFAULT (datetime('now')));
    CREATE TABLE IF NOT EXISTS alert_rules (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, event_type TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, cooldown_seconds INTEGER NOT NULL DEFAULT 300, config_json TEXT NOT NULL DEFAULT '{}', last_sent TEXT);
    CREATE TABLE IF NOT EXISTS camera_alert_history (id INTEGER PRIMARY KEY AUTOINCREMENT, camera_id TEXT NOT NULL, event_type TEXT NOT NULL, old_status TEXT NOT NULL DEFAULT '', new_status TEXT NOT NULL DEFAULT '', message TEXT NOT NULL DEFAULT '', sent INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT (datetime('now')));
    CREATE TABLE IF NOT EXISTS diagnostic_history (id INTEGER PRIMARY KEY AUTOINCREMENT, camera_id TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT (datetime('now')));
    CREATE TABLE IF NOT EXISTS camera_change_history (id INTEGER PRIMARY KEY AUTOINCREMENT, camera_id TEXT NOT NULL, field_name TEXT NOT NULL, old_value TEXT NOT NULL DEFAULT '', new_value TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT (datetime('now')));
    CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT (datetime('now')));
    CREATE INDEX IF NOT EXISTS idx_tags_camera ON camera_tags(camera_id);
    CREATE INDEX IF NOT EXISTS idx_alert_history_cam ON camera_alert_history(camera_id,created_at);
    CREATE INDEX IF NOT EXISTS idx_diag_history_cam ON diagnostic_history(camera_id,created_at);
    CREATE INDEX IF NOT EXISTS idx_change_history_cam ON camera_change_history(camera_id,created_at);
    CREATE INDEX IF NOT EXISTS idx_alert_rules_type ON alert_rules(event_type,enabled);
    CREATE INDEX IF NOT EXISTS idx_layout_updated ON live_layouts(updated_at);
    CREATE INDEX IF NOT EXISTS idx_adv_evt_cam ON device_events(camera_id,created_at);
    CREATE INDEX IF NOT EXISTS idx_adv_stream_cam ON stream_observations(camera_id,observed_at);
    CREATE INDEX IF NOT EXISTS idx_adv_audit_time ON audit_log(created_at);
    """)
    c.commit()

def _advanced_audit(action, object_type='', object_id='', detail='', actor='system'):
    """Best-effort audit write that participates in the global SQLite retry path."""
    def _write():
        c = _db_conn()
        c.execute(
            'INSERT INTO audit_log(actor,action,object_type,object_id,detail) VALUES (?,?,?,?,?)',
            (str(actor or 'system')[:80], str(action)[:120],
             str(object_type)[:80], str(object_id)[:160], str(detail)[:2000])
        )
        c.commit()
    try:
        _db_retry_write(_write, 'audit log write')
    except Exception:
        log.exception('audit log failed')

def _advanced_confidence(cam):
    checks=[('reachability',bool(cam.get('ip')),10),('credentials',bool(cam.get('username')),10),('vendor',bool(cam.get('camera_type')),15),('http',bool(cam.get('web_url')),10),('device_id',bool(cam.get('device_id')),10),('channel',bool(cam.get('channel_index')),10),('rtsp_candidate',bool(cam.get('rtsp_urls')),15),('rtsp_validation',cam.get('status')=='online',20)]
    score=sum(points for _,ok,points in checks if ok); return min(100,score),[(n,'present',p) for n,ok,p in checks if ok]

def _sync_advanced_metadata(cam):
    # Background Services OFF must be authoritative for every entry point.
    # This guard prevents startup recovery, a worker, or a future caller from
    # touching device_fingerprints/device_evidence while the operator has
    # disabled Advanced Metadata Sync.
    if _shutdown_requested.is_set() or not _service_enabled('advanced-metadata'):
        return False
    try:
        cid=str(cam.get('id') or '')
        if not cid: return
        score,evidence=_advanced_confidence(cam); ip=_safe_str(cam.get('ip')).strip().lower(); port=str(cam.get('port') or '')
        key=hashlib.sha256(f'{ip}|{port}'.encode()).hexdigest()[:32]
        fp=hashlib.sha256('|'.join([ip,port,_safe_str(cam.get('camera_type')).lower(),_safe_str(cam.get('label')).lower(),_safe_str(cam.get('device_id')).lower(),str(cam.get('channel_index') or '')]).encode()).hexdigest()
        def _write():
            c=_db_conn()
            c.execute('''INSERT INTO device_fingerprints(device_key,camera_id,fingerprint,vendor,model,serial,last_seen,confidence) VALUES(?,?,?,?,?,?,datetime('now'),?) ON CONFLICT(device_key) DO UPDATE SET camera_id=excluded.camera_id,fingerprint=excluded.fingerprint,vendor=excluded.vendor,model=excluded.model,serial=excluded.serial,last_seen=datetime('now'),confidence=excluded.confidence''',(key,cid,fp,_safe_str(cam.get('camera_type')),_safe_str(cam.get('label')),_safe_str(cam.get('device_id')),score))
            c.execute('DELETE FROM device_evidence WHERE camera_id=?',(cid,))
            for item in evidence:
                if not isinstance(item,(list,tuple)) or len(item)<3: continue
                et,res,val=item[0],item[1],item[2]; conf=item[3] if len(item)>=4 else (float(val) if isinstance(val,(int,float)) else 0.0)
                c.execute('INSERT INTO device_evidence(camera_id,evidence_type,result,value,confidence) VALUES(?,?,?,?,?)',(cid,str(et),str(res),str(val)[:500],conf))
            c.commit()
        if _shutdown_requested.is_set():
            return False
        _db_retry_write(_write,'advanced metadata sync')
        return not _shutdown_requested.is_set()
    except Exception as exc:
        if _shutdown_requested.is_set() and 'cancelled during shutdown' in str(exc).lower():
            log.debug('Advanced metadata sync cancelled during shutdown')
            return False
        message = str(exc).lower()
        if isinstance(exc, sqlite3.OperationalError) and (
            'application sqlite write lock' in message or
            'application write lock' in message or
            'database is locked' in message or
            'database is busy' in message
        ):
            # Background metadata is non-critical. Keep the item eligible for
            # a later reconciliation cycle and avoid flooding Railway logs.
            log.warning('Advanced metadata sync deferred due to SQLite contention (camera_id=%s): %s',
                        _safe_str(cam.get('id')), exc)
            return False
        log.exception('advanced metadata sync failed')
        return False


def _run_advanced_metadata_service() -> None:
    """Bounded background metadata reconciliation controlled by Services."""
    _touch_worker('startup-metadata','starting')
    while not _shutdown_requested.is_set() and _service_enabled('advanced-metadata'):
        try:
            _touch_worker('startup-metadata','running')
            processed = _advanced_reconcile_all(300)
            _touch_worker('startup-metadata','idle',f'processed={processed}')
        except Exception as exc:
            log.warning('[Services] Advanced metadata service error: %s', exc)
            _touch_worker('startup-metadata','error',type(exc).__name__)
        if _shutdown_requested.wait(300.0): break
    _touch_worker('startup-metadata','stopped','disabled by operator' if not _service_enabled('advanced-metadata') else 'shutdown requested')


def _advanced_reconcile_all(limit=3000):
    # Respect the persistent service switch at the reconciliation boundary.
    # In particular, startup must never perform an advanced-metadata pass when
    # the operator has turned Advanced Metadata Sync OFF.
    if _shutdown_requested.is_set() or not _service_enabled('advanced-metadata'):
        return 0
    rows=_db_conn().execute('SELECT * FROM cameras WHERE deleted=0 ORDER BY updated_at DESC LIMIT ?',(min(10000,limit),)).fetchall()
    processed=0
    for row in rows:
        if _shutdown_requested.is_set():
            break
        _sync_advanced_metadata(_db_row(row))
        processed += 1
    return processed

def _stream_quality_score(data):
    return min(100,(35 if data.get('video') else 0)+(35 if data.get('first_frame') else 0)+(10 if data.get('codec') else 0)+(10 if data.get('resolution') else 0)+(5 if data.get('fps') else 0)+(5 if data.get('latency_ms') and int(data.get('latency_ms'))<1000 else 0))

def _v134_ensure_runtime_compat(c):
    """Ensure tables/columns used by runtime APIs exist after fresh or partial schema init."""
    # CREATE IF NOT EXISTS cannot add columns to tables created by an earlier
    # prerequisite schema pass, so explicitly repair the small compatibility set.
    table_sql = {
        'login_history': """CREATE TABLE IF NOT EXISTS login_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL DEFAULT '',
            ok INTEGER NOT NULL DEFAULT 0,
            ip TEXT NOT NULL DEFAULT '',
            user_agent TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
        'v43_auth_users': """CREATE TABLE IF NOT EXISTS v43_auth_users(
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT 'custom',
            permissions_json TEXT NOT NULL DEFAULT '[]',
            enabled INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL DEFAULT 'database',
            auth_version INTEGER NOT NULL DEFAULT 1,
            force_password_change INTEGER NOT NULL DEFAULT 0,
            password_changed_at TEXT,
            last_login_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
    }
    for name, sql in table_sql.items():
        try:
            c.execute(sql)
        except sqlite3.Error:
            log.exception('[v134 schema] could not ensure table %s', name)
    required_columns = {
        'login_history': {
            'username': "TEXT NOT NULL DEFAULT ''",
            'ok': 'INTEGER NOT NULL DEFAULT 0',
            'ip': "TEXT NOT NULL DEFAULT ''",
            'user_agent': "TEXT NOT NULL DEFAULT ''",
            'detail': "TEXT NOT NULL DEFAULT ''",
            'created_at': "TEXT NOT NULL DEFAULT (datetime('now'))",
        },
        'v43_auth_users': {
            'password_hash': "TEXT NOT NULL DEFAULT ''",
            'role': "TEXT NOT NULL DEFAULT 'custom'",
            'permissions_json': "TEXT NOT NULL DEFAULT '[]'",
            'enabled': 'INTEGER NOT NULL DEFAULT 1',
            'source': "TEXT NOT NULL DEFAULT 'database'",
            'auth_version': 'INTEGER NOT NULL DEFAULT 1',
            'force_password_change': 'INTEGER NOT NULL DEFAULT 0',
            'password_changed_at': 'TEXT',
            'last_login_at': 'TEXT',
            'created_at': "TEXT NOT NULL DEFAULT (datetime('now'))",
            'updated_at': "TEXT NOT NULL DEFAULT (datetime('now'))",
        },
    }
    for table, cols in required_columns.items():
        try:
            existing = {r[1] for r in c.execute(f'PRAGMA table_info({table})').fetchall()}
            for col, definition in cols.items():
                if col not in existing:
                    c.execute(f'ALTER TABLE {table} ADD COLUMN {col} {definition}')
                    log.info('[v134 schema] added %s.%s', table, col)
        except sqlite3.Error:
            log.exception('[v134 schema] column compatibility failed for %s', table)
    try:
        c.execute('CREATE INDEX IF NOT EXISTS idx_login_history_time ON login_history(created_at)')
    except sqlite3.Error:
        pass
    try:
        c.commit()
    except sqlite3.Error:
        pass


def db_init(allow_create_if_missing: bool = False) -> None:
    # Missing/corrupt DB protection is handled by _recover_database_if_needed().
    # If --create-db-if-missing was explicitly supplied and the live DB is
    # missing, that function deliberately returns without adopting any backup;
    # db_init must then continue and build the fresh schema.
    _recover_database_if_needed(allow_create_if_missing=allow_create_if_missing)
    db_missing = (not os.path.exists(DB_PATH) or os.path.getsize(DB_PATH) <= 0)
    if db_missing and not allow_create_if_missing:
        raise RuntimeError('DB PROTECTION STOP: refusing to initialize a missing/empty camera_db.sqlite')
    if db_missing and allow_create_if_missing:
        log.warning('[DB Protection] Initializing a fresh database because --create-db-if-missing was supplied: %s', DB_PATH)
    c = _db_conn()
    # Fresh databases need one more prerequisite before _advanced_schema():
    # that migration layer may ALTER camera_health_history before the main
    # schema creates it.  Create the base table first so fresh DB creation
    # cannot fail with 'no such table: camera_health_history'.  Existing DBs
    # are unchanged because this is IF NOT EXISTS/idempotent.
    c.execute('''
        CREATE TABLE IF NOT EXISTS camera_health_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            camera_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'unknown',
            codec TEXT NOT NULL DEFAULT '',
            resolution TEXT NOT NULL DEFAULT '',
            fps REAL NOT NULL DEFAULT 0.0,
            latency_ms INTEGER NOT NULL DEFAULT 0,
            failure_reason TEXT NOT NULL DEFAULT '',
            candidate_url TEXT NOT NULL DEFAULT '',
            transport TEXT NOT NULL DEFAULT '',
            candidate_score REAL NOT NULL DEFAULT 0.0,
            checked_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    ''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_health_history_time ON camera_health_history(checked_at)')
    c.commit()
    # Fresh databases must create advanced/retention tables before the main
    # schema creates indexes that reference them.  Existing databases are
    # unchanged because all statements are IF NOT EXISTS/idempotent.
    _advanced_schema(c)
    c.commit()
    c.executescript('''
        CREATE TABLE IF NOT EXISTS cameras (
            id            TEXT    PRIMARY KEY,
            label         TEXT    NOT NULL DEFAULT '',
            ip            TEXT    NOT NULL DEFAULT '',
            port          INTEGER NOT NULL DEFAULT 80,
            camera_type   TEXT    NOT NULL DEFAULT '',
            username      TEXT    NOT NULL DEFAULT '',
            password      TEXT    NOT NULL DEFAULT '',
            country       TEXT    NOT NULL DEFAULT '',
            region        TEXT    NOT NULL DEFAULT '',
            city          TEXT    NOT NULL DEFAULT '',
            rtsp_urls     TEXT    NOT NULL DEFAULT '[]',
            rtsp_port     INTEGER NOT NULL DEFAULT 554,
            web_url       TEXT    NOT NULL DEFAULT '',
            source_file   TEXT    NOT NULL DEFAULT '',
            group_name    TEXT    NOT NULL DEFAULT '',
            status        TEXT    NOT NULL DEFAULT 'unknown',
            codec         TEXT    NOT NULL DEFAULT '',
            resolution    TEXT    NOT NULL DEFAULT '',
            fps           REAL    NOT NULL DEFAULT 0.0,
            latency_ms    INTEGER NOT NULL DEFAULT 0,
            failure_reason TEXT   NOT NULL DEFAULT '',
            rtsp_score    REAL    NOT NULL DEFAULT 0.0,
            last_checked  TEXT,
            last_seen     TEXT,
            favorite      INTEGER NOT NULL DEFAULT 0,
            deleted       INTEGER NOT NULL DEFAULT 0,
            snapshot_path TEXT    NOT NULL DEFAULT '',
            device_id     TEXT    NOT NULL DEFAULT '',
            channel_index INTEGER NOT NULL DEFAULT 0,
            channel_name  TEXT    NOT NULL DEFAULT '',
            geo_source    TEXT    NOT NULL DEFAULT '',
            geo_confidence TEXT   NOT NULL DEFAULT '',
            first_seen    TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at    TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_cam_status    ON cameras(status)     WHERE deleted=0;
        CREATE INDEX IF NOT EXISTS idx_cam_country   ON cameras(country)    WHERE deleted=0;
        CREATE INDEX IF NOT EXISTS idx_cam_favorite  ON cameras(favorite)   WHERE deleted=0;
        CREATE INDEX IF NOT EXISTS idx_cam_group     ON cameras(group_name) WHERE deleted=0;
        CREATE INDEX IF NOT EXISTS idx_cam_source    ON cameras(source_file) WHERE deleted=0;
        CREATE INDEX IF NOT EXISTS idx_cam_ip        ON cameras(ip)         WHERE deleted=0;
        CREATE TABLE IF NOT EXISTS category_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT '',
            target_category TEXT NOT NULL DEFAULT '',
            camera_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_category_actions_time ON category_actions(created_at);
        CREATE TABLE IF NOT EXISTS discovery_sources (
            path          TEXT PRIMARY KEY,
            size_bytes    INTEGER NOT NULL DEFAULT 0,
            mtime_ns      INTEGER NOT NULL DEFAULT 0,
            last_scanned  TEXT,
            scan_status   TEXT NOT NULL DEFAULT 'ok',
            camera_count  INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS discovery_members (
            source_path   TEXT NOT NULL,
            camera_id     TEXT NOT NULL,
            last_seen     TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY(source_path, camera_id),
            FOREIGN KEY(camera_id) REFERENCES cameras(id)
        );
        CREATE INDEX IF NOT EXISTS idx_discovery_members_camera
            ON discovery_members(camera_id);
        CREATE TABLE IF NOT EXISTS validation_queue (
            camera_id     TEXT PRIMARY KEY,
            attempts      INTEGER NOT NULL DEFAULT 0,
            last_attempt  TEXT,
            next_attempt  TEXT NOT NULL DEFAULT (datetime('now')),
            priority      INTEGER NOT NULL DEFAULT 50,
            FOREIGN KEY(camera_id) REFERENCES cameras(id)
        );
        CREATE INDEX IF NOT EXISTS idx_validation_due
            ON validation_queue(next_attempt, priority);
        CREATE TABLE IF NOT EXISTS camera_health_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            camera_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'unknown',
            codec TEXT NOT NULL DEFAULT '',
            resolution TEXT NOT NULL DEFAULT '',
            fps REAL NOT NULL DEFAULT 0.0,
            latency_ms INTEGER NOT NULL DEFAULT 0,
            failure_reason TEXT NOT NULL DEFAULT '',
            candidate_url TEXT NOT NULL DEFAULT '',
            transport TEXT NOT NULL DEFAULT '',
            candidate_score REAL NOT NULL DEFAULT 0.0,
            checked_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_health_history_time
            ON camera_health_history(checked_at);
        CREATE TABLE IF NOT EXISTS telegram_delivery_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL DEFAULT 'message',
            destination TEXT NOT NULL DEFAULT '',
            ok INTEGER NOT NULL DEFAULT 0,
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_tg_delivery_time
            ON telegram_delivery_log(created_at);
        CREATE TABLE IF NOT EXISTS worker_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            worker TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_worker_events_time ON worker_events(created_at);
        CREATE TABLE IF NOT EXISTS validation_recovery (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            camera_id TEXT NOT NULL,
            action TEXT NOT NULL DEFAULT '',
            result TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_validation_recovery_camera ON validation_recovery(camera_id,created_at);
        CREATE TABLE IF NOT EXISTS login_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL DEFAULT '',
            ok INTEGER NOT NULL DEFAULT 0,
            ip TEXT NOT NULL DEFAULT '',
            user_agent TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_login_history_time ON login_history(created_at);
        CREATE TABLE IF NOT EXISTS telegram_cooldowns (
            event_key TEXT PRIMARY KEY,
            last_sent REAL NOT NULL DEFAULT 0,
            suppressed INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS recovery_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            target TEXT NOT NULL DEFAULT '',
            result TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_recovery_events_time ON recovery_events(created_at);
        CREATE TABLE IF NOT EXISTS inventory_jobs (
            id TEXT PRIMARY KEY,
            filename TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'queued',
            total INTEGER NOT NULL DEFAULT 0,
            processed INTEGER NOT NULL DEFAULT 0,
            imported INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            started_at TEXT,
            finished_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            error TEXT NOT NULL DEFAULT '',
            results_json TEXT NOT NULL DEFAULT '{\"devices\":[],\"errors\":[]}'
        );
        CREATE INDEX IF NOT EXISTS idx_inventory_jobs_created ON inventory_jobs(created_at);
        CREATE INDEX IF NOT EXISTS idx_inventory_jobs_status_created ON inventory_jobs(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_camera_trash_deleted_at ON camera_trash(deleted_at);
        CREATE INDEX IF NOT EXISTS idx_camera_health_history_checked_at ON camera_health_history(checked_at);
        CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log(created_at);
        CREATE INDEX IF NOT EXISTS idx_camera_alert_history_created_at ON camera_alert_history(created_at);
        CREATE INDEX IF NOT EXISTS idx_diagnostic_history_created_at ON diagnostic_history(created_at);
        CREATE INDEX IF NOT EXISTS idx_stream_observations_observed_at ON stream_observations(observed_at);
        CREATE INDEX IF NOT EXISTS idx_worker_events_created_at ON worker_events(created_at);
        CREATE INDEX IF NOT EXISTS idx_recovery_events_created_at ON recovery_events(created_at);
        CREATE INDEX IF NOT EXISTS idx_login_history_created_at ON login_history(created_at);
        CREATE INDEX IF NOT EXISTS idx_telegram_delivery_log_created_at ON telegram_delivery_log(created_at);
        CREATE TABLE IF NOT EXISTS scan_history (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT, force_scan INTEGER NOT NULL DEFAULT 0, discovered INTEGER NOT NULL DEFAULT 0, processed INTEGER NOT NULL DEFAULT 0, added INTEGER NOT NULL DEFAULT 0, updated INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT '');
        CREATE INDEX IF NOT EXISTS idx_scan_history_finished_at ON scan_history(finished_at);
        CREATE INDEX IF NOT EXISTS idx_device_evidence_created_at ON device_evidence(created_at);
        CREATE INDEX IF NOT EXISTS idx_device_events_created_at ON device_events(created_at);
        CREATE TABLE IF NOT EXISTS camera_notes (camera_id TEXT PRIMARY KEY, owner TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', install_details TEXT NOT NULL DEFAULT '', maintenance_notes TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE IF NOT EXISTS maintenance_windows (id INTEGER PRIMARY KEY AUTOINCREMENT, group_name TEXT NOT NULL DEFAULT '', starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS api_keys (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, key_hash TEXT NOT NULL, permissions_json TEXT NOT NULL DEFAULT '[\"dashboard.view\"]', enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT (datetime('now')), last_used TEXT, owner_username TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS app_retention (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS schema_version (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL DEFAULT (datetime('now')));
    ''')
    try:
        cols = {r[1] for r in c.execute('PRAGMA table_info(inventory_jobs)').fetchall()}
        if 'targets_json' not in cols:
            c.execute("ALTER TABLE inventory_jobs ADD COLUMN targets_json TEXT NOT NULL DEFAULT '[]'")
            c.commit()
    except sqlite3.Error:
        log.exception('Could not migrate inventory_jobs.targets_json')
    try:
        api_cols = {r[1] for r in c.execute('PRAGMA table_info(api_keys)').fetchall()}
        if api_cols and 'owner_username' not in api_cols:
            # Schema-critical: use the common writer lock/retry path.
            def _add_api_owner_column():
                c2 = _db_conn()
                c2.execute("ALTER TABLE api_keys ADD COLUMN owner_username TEXT NOT NULL DEFAULT ''")
                c2.commit()
            _db_retry_write(_add_api_owner_column, 'add api_keys.owner_username')
            api_cols = {r[1] for r in _db_conn().execute('PRAGMA table_info(api_keys)').fetchall()}

        if api_cols and 'owner_username' in api_cols and AUTH_USER:
            # Backfill is data maintenance, not a reason to abort startup.
            def _backfill_api_owner():
                c2 = _db_conn()
                row = c2.execute("SELECT COUNT(*) AS n FROM api_keys WHERE owner_username=''").fetchone()
                if not row or int(row['n'] or 0) <= 0:
                    return 0
                c2.execute("UPDATE api_keys SET owner_username=? WHERE owner_username=''", (AUTH_USER,))
                c2.commit()
                return int(row['n'] or 0)
            try:
                # Owner backfill is deliberately non-fatal during startup. A
                # separate dashboard process may briefly hold the SQLite writer
                # lock. Use a short local busy timeout rather than inheriting the
                # normal 30-second connection timeout and multiplying it across
                # retries.
                with _DB_WRITE_LOCK:
                    c2 = _db_conn()
                    c2.execute('PRAGMA busy_timeout=3000')
                    try:
                        _backfill_api_owner()
                    finally:
                        c2.execute('PRAGMA busy_timeout=30000')
            except sqlite3.OperationalError as exc:
                if 'locked' in str(exc).lower() or 'busy' in str(exc).lower():
                    try:
                        c.rollback()
                    except Exception:
                        pass
                    log.warning('Could not backfill api_keys.owner_username during startup because SQLite is busy; startup will continue and the backfill can retry later.')
                else:
                    raise
    except sqlite3.Error:
        try:
            c.rollback()
        except Exception:
            pass
        log.exception('Could not migrate api_keys.owner_username')

    # Ensure the extended v43 schema exists before later db_init migrations
    # reference its auth/health/snapshot/feature tables. This is idempotent.
    _v43_ensure_schema()
    # A fresh DB can pass through multiple idempotent schema layers. Repair the
    # small set of runtime columns after all layers have run so old prerequisite
    # table definitions cannot shadow the full definitions below.
    _v134_ensure_runtime_compat(c)

    # v66 auth/security migrations — preserve existing users and sessions.
    try:
        _v134_ensure_runtime_compat(c)
        auth_cols = {r[1] for r in c.execute('PRAGMA table_info(v43_auth_users)').fetchall()}
        for col, definition in (
            ('force_password_change', 'INTEGER NOT NULL DEFAULT 0'),
            ('password_changed_at', 'TEXT'),
            ('last_login_at', 'TEXT'),
        ):
            if col not in auth_cols:
                c.execute(f'ALTER TABLE v43_auth_users ADD COLUMN {col} {definition}')
        c.commit()
    except sqlite3.Error:
        log.exception('Could not migrate v66 auth fields')

    # Additions are deliberately idempotent so imported databases keep their
    # camera and queue data across upgrades.
    for column, definition in (
        ('deleted_at', 'TEXT'),
        ('last_alert_status', "TEXT NOT NULL DEFAULT ''"),
        ('last_alert_at', 'TEXT'),
        ('device_id', "TEXT NOT NULL DEFAULT ''"),
        ('channel_index', 'INTEGER NOT NULL DEFAULT 0'),
        ('channel_name', "TEXT NOT NULL DEFAULT ''"),
        ('geo_source', "TEXT NOT NULL DEFAULT ''"),
        ('geo_confidence', "TEXT NOT NULL DEFAULT ''"),
        ('failure_reason', "TEXT NOT NULL DEFAULT ''"),
        ('rtsp_score', 'REAL NOT NULL DEFAULT 0.0'),
        ('rtsp_port', 'INTEGER NOT NULL DEFAULT 554'),
    ):
        try:
            c.execute(f'ALTER TABLE cameras ADD COLUMN {column} {definition}')
        except sqlite3.OperationalError as e:
            if 'duplicate column name' not in str(e).lower():
                raise

    for column, definition in (
        ('locked_until', "TEXT"),
        ('locked_by', "TEXT NOT NULL DEFAULT ''"),
        ('last_error', "TEXT NOT NULL DEFAULT ''"),
        ('max_attempts', "INTEGER NOT NULL DEFAULT 12"),
    ):
        try:
            c.execute(f'ALTER TABLE validation_queue ADD COLUMN {column} {definition}')
        except sqlite3.OperationalError as e:
            if 'duplicate column name' not in str(e).lower():
                raise
    for column, definition in (
        ('failure_reason', "TEXT NOT NULL DEFAULT ''"),
        ('candidate_url', "TEXT NOT NULL DEFAULT ''"),
        ('transport', "TEXT NOT NULL DEFAULT ''"),
        ('candidate_score', 'REAL NOT NULL DEFAULT 0.0'),
    ):
        try:
            c.execute(f'ALTER TABLE camera_health_history ADD COLUMN {column} {definition}')
        except sqlite3.OperationalError as e:
            if 'duplicate column name' not in str(e).lower():
                raise
    c.execute("""
        UPDATE cameras
        SET deleted_at=COALESCE(deleted_at, updated_at, datetime('now'))
        WHERE deleted=1 AND deleted_at IS NULL
    """)
    c.commit()
    # Existing imports predate discovery_sources. Bootstrap their signatures from
    # the database so the first restart after an import does not parse hundreds of
    # megabytes of unchanged playlists again.
    try:
        rows = c.execute('''
            SELECT source_file, COUNT(*) FROM cameras
            WHERE deleted=0 AND source_file!='' GROUP BY source_file
        ''').fetchall()
        for source_file, count in rows:
            try:
                st = os.stat(source_file)
            except OSError:
                continue
            c.execute('''
                INSERT OR IGNORE INTO discovery_sources
                    (path,size_bytes,mtime_ns,last_scanned,scan_status,camera_count)
                VALUES (?,?,?,?,?,?)
            ''', (source_file, st.st_size, st.st_mtime_ns,
                  datetime.now().strftime('%Y-%m-%dT%H:%M:%S'), 'ok', count))
        c.execute('''
            INSERT OR IGNORE INTO validation_queue(camera_id, priority, next_attempt)
            SELECT id,
                   CASE WHEN favorite=1 THEN 1
                        WHEN status='online' THEN 10
                        WHEN status='checking' THEN 20
                        WHEN status='offline' THEN 80 ELSE 90 END,
                   CASE WHEN status='online' THEN datetime('now','+15 minutes')
                        WHEN status='offline' THEN datetime('now','+6 hours')
                        ELSE datetime('now') END
            FROM cameras WHERE deleted=0
        ''')
        c.commit()
    except sqlite3.Error as e:
        log.warning('Discovery state bootstrap failed: %s', e)
    _advanced_schema(c)
    # Load persisted retention values so /api/v110/retention survives restarts.
    global HISTORY_RETENTION_DAYS, SNAPSHOT_RETENTION_DAYS, DELETED_RETENTION_DAYS
    try:
        for rr in c.execute("SELECT key,value FROM app_retention WHERE key IN ('history_days','snapshot_days','deleted_days')").fetchall():
            iv=int(rr['value'])
            if rr['key']=='history_days': HISTORY_RETENTION_DAYS=max(7,iv)
            elif rr['key']=='snapshot_days': SNAPSHOT_RETENTION_DAYS=max(1,iv)
            elif rr['key']=='deleted_days': DELETED_RETENTION_DAYS=max(1,iv)
    except Exception:
        pass
    c.execute("INSERT INTO schema_version(id,version) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,updated_at=datetime('now')", (SCHEMA_VERSION,))
    c.commit()
    required={r[1] for r in c.execute('PRAGMA table_info(cameras)').fetchall()}
    missing={'rtsp_port','rtsp_score','failure_reason','device_id','channel_index'}-required
    if missing: raise RuntimeError('Database schema incomplete; missing cameras columns: '+', '.join(sorted(missing)))
    c.commit()
    log.info('Database schema verified — cameras columns OK')
    log.info('Database ready at %s', DB_PATH)


def _safe_str(v) -> str:
    if v is None:
        return ''
    s = str(v) if not isinstance(v, str) else v
    return s.encode('utf-8', 'replace').decode('utf-8')


def _mask_rtsp(rtsp_url: str) -> str:
    """Mask credentials in either a raw RTSP URL or text containing RTSP URLs."""
    if not rtsp_url:
        return ''
    value = str(rtsp_url)
    # Stream logs pass formatted messages (not just a URL). The previous code
    # returned those messages unchanged because their overall scheme is empty.
    # Sanitize every embedded RTSP URL before handling a raw URL below.
    if not value.lower().startswith(('rtsp://', 'rtsps://')):
        pattern = re.compile(r'''(?i)rtsps?://[^\s'"<>]+''')
        def _replace_embedded(match):
            token = match.group(0)
            trailing = ''
            while token and token[-1] in '),;]':
                trailing = token[-1] + trailing
                token = token[:-1]
            return _mask_rtsp(token) + trailing
        return pattern.sub(_replace_embedded, value)
    try:
        parsed=urlsplit(value)
        if parsed.scheme.lower() not in {'rtsp','rtsps'}:
            return value
        host=parsed.hostname or ''
        if ':' in host and not host.startswith('['): host=f'[{host}]'
        port=f':{parsed.port}' if parsed.port else ''
        user=unquote(parsed.username or '')
        password=unquote(parsed.password or '')
        auth=(quote(user,safe='') + (':******' if password else '') + '@') if user else ''
        safe=f'{parsed.scheme}://{auth}{host}{port}{parsed.path or ""}'
        if parsed.query: safe+='?'+parsed.query
        if parsed.fragment: safe+='#'+parsed.fragment
        return safe
    except Exception:
        return re.sub(r'(?i)(rtsps?://)([^/@:]+)(?::[^@]*)?@', r'\1\2:******@', str(rtsp_url))


def _stream_log(message: str, *args) -> None:
    """Write stream diagnostics without ever exposing RTSP credentials."""
    try:
        rendered = message % args if args else message
        stream_error_log.info(_mask_rtsp(rendered))
    except Exception:
        stream_error_log.info('Unable to format stream diagnostic')


def _db_row(row: sqlite3.Row) -> dict:
    d = {k: _safe_str(v) if isinstance(v, str) else (v or '') for k, v in dict(row).items()}
    try:
        d['rtsp_urls'] = [_safe_str(u) for u in json.loads(d.get('rtsp_urls') or '[]')
                          if isinstance(u, str)]
    except Exception:
        d['rtsp_urls'] = []
    d['rtsp_display'] = [_mask_rtsp(u) for u in d['rtsp_urls']]
    return d


_STATUS_SORT = "CASE status WHEN 'online' THEN 0 WHEN 'checking' THEN 1 WHEN 'unknown' THEN 2 ELSE 3 END"


def db_bulk_upsert(cameras: list[dict]) -> int:
    # Bounded discovery upsert. Never build a giant SQLite IN (...) parameter list.
    if not cameras:
        return 0
    c = _db_conn()
    chunk_size = max(10, min(50, int(os.getenv('CAMERA_DB_UPSERT_CHUNK', '50'))))
    ids = [str(cam.get('id') or '') for cam in cameras if cam.get('id')]
    existing_urls = {}
    for pos in range(0, len(ids), chunk_size):
        chunk = ids[pos:pos + chunk_size]
        ph = ','.join('?' * len(chunk))
        for row in c.execute(f'SELECT id,rtsp_urls FROM cameras WHERE id IN ({ph})', chunk).fetchall():
            try:
                value = json.loads(row['rtsp_urls'] or '[]')
                existing_urls[row['id']] = value if isinstance(value, list) else []
            except Exception:
                existing_urls[row['id']] = []
    rows=[]; memberships=[]; queued=[]
    for cam in cameras:
        cid=str(cam.get('id') or '')
        if not cid: continue
        incoming=[u for u in cam.get('rtsp_urls',[]) if isinstance(u,str) and u]
        previous=[u for u in existing_urls.get(cid,[]) if isinstance(u,str) and u]
        merged=previous+[u for u in incoming if u not in previous]
        rows.append((cid,cam.get('label',''),cam.get('ip',''),cam.get('port',80),cam.get('camera_type',''),cam.get('username',''),cam.get('password',''),cam.get('country',''),cam.get('region',''),cam.get('city',''),json.dumps(merged),cam.get('web_url',''),cam.get('source_file',''),cam.get('group_name',cam.get('country','')),cam.get('device_id',''),int(cam.get('channel_index') or 0),cam.get('channel_name',''),cam.get('geo_source',''),cam.get('geo_confidence','')))
        memberships.append((cam.get('source_file',''),cid) if cam.get('source_file') else None)
        has_new_candidate = any(url not in previous for url in incoming)
        queue_priority = 0 if has_new_candidate else _validation_priority(cam)
        queued.append((cid,queue_priority,VALIDATION_MAX_ATTEMPTS,int(has_new_candidate)))
    upsert_sql = '''
        INSERT INTO cameras (id,label,ip,port,camera_type,username,password,country,region,city,rtsp_urls,web_url,source_file,group_name,device_id,channel_index,channel_name,geo_source,geo_confidence)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            label=CASE WHEN trim(COALESCE(excluded.label,''))='' OR lower(trim(excluded.label)) IN ('camera','unknown','generic','generic_rtsp','generic_rtsp_pending') THEN cameras.label ELSE excluded.label END, ip=excluded.ip, port=excluded.port,
            camera_type=CASE WHEN excluded.camera_type!='' THEN excluded.camera_type ELSE cameras.camera_type END,
            username=CASE WHEN excluded.username!='' THEN excluded.username ELSE cameras.username END,
            password=CASE WHEN excluded.password!='' THEN excluded.password ELSE cameras.password END,
            country=CASE WHEN excluded.country!='' THEN excluded.country ELSE cameras.country END,
            region=CASE WHEN excluded.region!='' THEN excluded.region ELSE cameras.region END,
            city=CASE WHEN excluded.city!='' THEN excluded.city ELSE cameras.city END,
            rtsp_urls=CASE WHEN excluded.rtsp_urls!='[]' THEN excluded.rtsp_urls ELSE cameras.rtsp_urls END,
            web_url=CASE WHEN excluded.web_url!='' THEN excluded.web_url ELSE cameras.web_url END,
            source_file=excluded.source_file, group_name=excluded.group_name,
            device_id=CASE WHEN excluded.device_id!='' THEN excluded.device_id ELSE cameras.device_id END,
            channel_index=CASE WHEN excluded.channel_index>0 THEN excluded.channel_index ELSE cameras.channel_index END,
            channel_name=CASE WHEN excluded.channel_name!='' THEN excluded.channel_name ELSE cameras.channel_name END,
            geo_source=CASE WHEN excluded.geo_source!='' THEN excluded.geo_source ELSE cameras.geo_source END,
            geo_confidence=CASE WHEN excluded.geo_confidence!='' THEN excluded.geo_confidence ELSE cameras.geo_confidence END,
            deleted=0, deleted_at=NULL, updated_at=datetime('now')
    '''
    # Refresh membership at most daily. Re-scanning unchanged files should
    # not dirty thousands of SQLite rows on every startup and inflate the WAL.
    member_sql = '''INSERT INTO discovery_members(source_path,camera_id,last_seen) VALUES (?, ?, datetime('now')) ON CONFLICT(source_path,camera_id) DO UPDATE SET last_seen=datetime('now') WHERE discovery_members.last_seen < datetime('now','-1 day')'''
    queue_sql = '''INSERT INTO validation_queue(camera_id,priority,next_attempt,max_attempts) VALUES (?, ?, datetime('now'), ?) ON CONFLICT(camera_id) DO UPDATE SET priority=MIN(validation_queue.priority,excluded.priority),next_attempt=CASE WHEN ?=1 OR excluded.priority < validation_queue.priority THEN datetime('now') ELSE validation_queue.next_attempt END,max_attempts=MIN(validation_queue.max_attempts,excluded.max_attempts)'''
    # v136: commit each small discovery chunk independently.
    for pos in range(0, len(rows), chunk_size):
        end=min(pos+chunk_size,len(rows))
        def _write_chunk(pos=pos,end=end):
            c.executemany(upsert_sql,rows[pos:end])
            member_chunk=[x for x in memberships[pos:end] if x]
            if member_chunk: c.executemany(member_sql,member_chunk)
            if queued: c.executemany(queue_sql,queued[pos:end])
            c.commit(); return end-pos
        _db_retry_write(_write_chunk,'bulk camera upsert')
        if end < len(rows): _shutdown_requested.wait(0.35 if _db_ui_recent() else 0.12)
        if pos and pos % (chunk_size*20)==0: log.info('[DB] Discovery upsert progress: %d/%d',end,len(rows))
    return len(rows)


def _record_recovery_event(event_type: str, target: str = '', result: str = '', detail: str = '') -> None:
    try:
        c = _db_conn()
        c.execute('INSERT INTO recovery_events(event_type,target,result,detail) VALUES(?,?,?,?)',
                  (str(event_type)[:80], str(target)[:160], str(result)[:40], str(detail)[:1000]))
        c.commit()
    except Exception:
        log.debug('recovery event write failed', exc_info=True)


def _record_worker_event(name: str, state: str, detail: str = '') -> None:
    try:
        c = _db_conn()
        c.execute('INSERT INTO worker_events(worker,state,detail) VALUES(?,?,?)',
                  (str(name)[:80], str(state)[:40], str(detail)[:1000]))
        c.commit()
    except Exception:
        pass


def _record_login_history(username: str, ok: bool, detail: str = '') -> None:
    try:
        c = _db_conn()
        c.execute('INSERT INTO login_history(username,ok,ip,user_agent,detail) VALUES(?,?,?,?,?)',
                  (str(username)[:100], int(bool(ok)), request.remote_addr or '',
                   request.headers.get('User-Agent','')[:300], str(detail)[:500]))
        c.commit()
    except Exception:
        pass


def _claim_validation_batch(limit: int, worker_id: str) -> list[dict]:
    """Atomically claim validation work with the same bounded DB retry path as other writers."""
    limit = max(1, min(int(limit), 2000))

    def _write():
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        lease = (datetime.now(timezone.utc) + timedelta(minutes=15)).strftime('%Y-%m-%dT%H:%M:%SZ')
        c = _db_conn()
        rows = c.execute("""
            SELECT c.* FROM cameras c JOIN validation_queue q ON q.camera_id=c.id
            WHERE c.deleted=0 AND q.next_attempt <= datetime('now')
              AND (q.locked_until IS NULL OR q.locked_until < ?)
            ORDER BY CASE WHEN COALESCE(q.attempts,0)=0 THEN 0 ELSE 1 END,q.priority,q.next_attempt,c.id LIMIT ?
        """, (now, limit)).fetchall()
        ids = [r['id'] for r in rows]
        if ids:
            ph = ','.join('?' * len(ids))
            c.execute(f"""UPDATE validation_queue SET locked_until=?,locked_by=?
                          WHERE camera_id IN ({ph})""", [lease, worker_id, *ids])
            c.commit()
        return rows

    rows = _db_retry_write(_write, 'claim validation batch')
    # Validation expects rtsp_urls to be a list, not the JSON text stored in
    # SQLite. _db_row performs that conversion and keeps the rest of the row
    # as a normal dict. Returning dict(row) here leaves rtsp_urls as a string;
    # _validate_one then iterates individual characters, produces no ranked
    # candidates, and incorrectly labels every camera "no-decoded-video".
    return [_db_row(row) for row in rows]


def _release_validation_claims(cam_ids: list[str], worker_id: str = '') -> None:
    """Release validation leases with bounded SQLite busy retries."""
    if not cam_ids:
        return
    ph = ','.join('?' * len(cam_ids))
    args = list(cam_ids)
    where = f'camera_id IN ({ph})'
    if worker_id:
        where += ' AND locked_by=?'
        args.append(worker_id)

    def _write():
        c = _db_conn()
        c.execute(
            f"UPDATE validation_queue SET locked_until=NULL,locked_by='' WHERE {where}",
            args)
        c.commit()

    _db_retry_write(_write, 'release validation claims')


def _db_update_validation_once(cam_id: str, status: str, codec: str = '',
                         resolution: str = '', fps: float = 0.0,
                         latency_ms: int = 0, failure_reason: str = '',
                         candidate_url: str = '', transport: str = '',
                         candidate_score: float = 0.0) -> None:
    now = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    c = _db_conn()
    previous = c.execute('SELECT status FROM cameras WHERE id=?',
                         (cam_id,)).fetchone()
    old_status = previous['status'] if previous else ''
    fields={'status':status,'latency_ms':int(latency_ms or 0),'updated_at':now,'last_checked':now,'rtsp_score':float(candidate_score or 0)}
    if status=='online':
        if codec: fields['codec']=codec
        if resolution: fields['resolution']=resolution
        if float(fps or 0)>0: fields['fps']=float(fps)
        fields['failure_reason']=''
        fields['last_seen']=now
    elif status=='offline':
        fields.update(codec='',resolution='',fps=0,failure_reason=str(failure_reason or '')[:240])
    assignments=', '.join(f'{k}=?' for k in fields)
    c.execute(f'UPDATE cameras SET {assignments} WHERE id=?',list(fields.values())+[cam_id])
    c.execute('''
        INSERT INTO camera_health_history
            (camera_id,status,codec,resolution,fps,latency_ms,
             failure_reason,candidate_url,transport,candidate_score,checked_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    ''', (cam_id, status, codec or '', resolution or '', float(fps or 0),
          int(latency_ms or 0), failure_reason[:240],
          _mask_rtsp(candidate_url)[:500], transport[:16],
          float(candidate_score or 0), now))
    c.execute("UPDATE validation_queue SET locked_until=NULL,locked_by='',last_error=? WHERE camera_id=?", (failure_reason[:500], cam_id))
    if status == 'online':
        priority = 1 if c.execute(
            'SELECT favorite FROM cameras WHERE id=?', (cam_id,)
        ).fetchone()[0] else 10
        c.execute('''
            INSERT INTO validation_queue(camera_id,attempts,last_attempt,next_attempt,priority)
            VALUES (?,0,datetime('now'),datetime('now','+15 minutes'),?)
            ON CONFLICT(camera_id) DO UPDATE SET
                last_attempt=datetime('now'),next_attempt=datetime('now','+15 minutes'),
                priority=?
        ''', (cam_id, priority, priority))
    else:
        row = c.execute('SELECT attempts,max_attempts FROM validation_queue WHERE camera_id=?',
                        (cam_id,)).fetchone()
        current_attempts=int(row[0] or 0) if row else 0
        max_attempts=int(row[1]) if row and row[1] else VALIDATION_MAX_ATTEMPTS
        max_attempts=max(1,min(100,max_attempts))
        attempts=min(current_attempts+1,max_attempts)
        delay=_validation_retry_delay_minutes(attempts)
        priority=80
        c.execute(f"""INSERT INTO validation_queue(camera_id,attempts,last_attempt,next_attempt,priority,max_attempts)
            VALUES (?, ?, datetime('now'), datetime('now','+{delay} minutes'), ?, ?)
            ON CONFLICT(camera_id) DO UPDATE SET attempts=?,last_attempt=datetime('now'),
            next_attempt=datetime('now','+{delay} minutes'),priority=?""", (cam_id,attempts,priority,max_attempts,attempts,priority))
    c.commit()
    return old_status


def db_update_validation(cam_id: str, status: str, codec: str = '',
                         resolution: str = '', fps: float = 0.0,
                         latency_ms: int = 0, failure_reason: str = '',
                         candidate_url: str = '', transport: str = '',
                         candidate_score: float = 0.0) -> None:
    """Persist one validation result with bounded SQLite busy retries."""
    def _write():
        return _db_update_validation_once(
            cam_id, status, codec, resolution, fps, latency_ms,
            failure_reason, candidate_url, transport, candidate_score)
    old_status = _db_retry_write(_write, 'camera validation update')
    # Never perform network I/O while the SQLite writer lock is held.
    if status in {'online', 'offline'} and old_status != status:
        try:
            tg_notify_status_transition(cam_id, old_status, status)
        except Exception:
            log.debug('[Telegram] status transition notification failed', exc_info=True)


def db_set_status_bulk(cam_ids: list[str], status: str) -> None:
    """Bulk status update protected against transient SQLite writer contention."""
    if not cam_ids:
        return
    now = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')

    def _write():
        c = _db_conn()
        # SQLite has a finite bound-parameter limit; refresh scopes can contain
        # hundreds of thousands of cameras.
        for start in range(0, len(cam_ids), 500):
            chunk = cam_ids[start:start + 500]
            ph = ','.join('?' * len(chunk))
            c.execute(
                f"UPDATE cameras SET status=?,updated_at=? WHERE id IN ({ph})",
                [status, now] + chunk)
            c.execute(
                f"UPDATE validation_queue SET next_attempt=datetime('now') "
                f"WHERE camera_id IN ({ph})", chunk)
        c.commit()

    _db_retry_write(_write, 'bulk camera status update')


def db_set_favorite(cam_id: str, state: bool) -> None:
    _db_conn().execute(
        "UPDATE cameras SET favorite=?,updated_at=datetime('now') WHERE id=?",
        (1 if state else 0, cam_id))
    _db_conn().commit()


def db_soft_delete(cam_id: str) -> None:
    _db_conn().execute(
        "UPDATE cameras SET deleted=1,deleted_at=datetime('now'),updated_at=datetime('now') WHERE id=?",
        (cam_id,))
    _db_conn().commit()


def db_reconcile_source(source_file: str, found_ids: set[str]) -> int:
    """Remove stale source membership and soft-delete orphaned cameras.

    Membership tracking avoids deleting a camera still present in another
    playlist. Older imported records without membership rows use a
    conservative source_file fallback.
    """
    if not source_file:
        return 0
    c = _db_conn()
    known = {r[0] for r in c.execute(
        'SELECT camera_id FROM discovery_members WHERE source_path=?',
        (source_file,)).fetchall()}
    if not known:
        known = {r[0] for r in c.execute(
            'SELECT id FROM cameras WHERE deleted=0 AND source_file=?',
            (source_file,)).fetchall()}
    stale = known - set(found_ids)
    if not stale:
        return 0
    ph = ','.join('?' * len(stale))
    c.execute(f'DELETE FROM discovery_members WHERE source_path=? AND camera_id IN ({ph})',
              [source_file, *stale])
    orphaned = [r[0] for r in c.execute(f'''
        SELECT id FROM cameras
        WHERE id IN ({ph}) AND NOT EXISTS (
            SELECT 1 FROM discovery_members m WHERE m.camera_id=cameras.id
        )
    ''', list(stale)).fetchall()]
    if orphaned:
        oph = ','.join('?' * len(orphaned))
        c.execute(f"""
            UPDATE cameras SET deleted=1,deleted_at=datetime('now'),
                   updated_at=datetime('now')
            WHERE id IN ({oph})
        """, orphaned)
        c.execute(f'DELETE FROM validation_queue WHERE camera_id IN ({oph})', orphaned)
    c.commit()
    if orphaned:
        log.info('Discovery cleanup: soft-deleted %d stale cameras from %s',
                 len(orphaned), source_file)
    return len(orphaned)


def db_prune_deleted(limit: int = 1000, retention_days: int = 30) -> int:
    """Bound old soft-deleted rows so rescans cannot grow the DB forever."""
    c = _db_conn()
    rows = c.execute('''
        SELECT id,snapshot_path FROM cameras
        WHERE deleted=1 AND favorite=0
          AND deleted_at IS NOT NULL
          AND deleted_at < datetime('now', ?)
        ORDER BY deleted_at
        LIMIT ?
    ''', (f'-{max(1, retention_days)} days', max(1, min(limit, 5000)))).fetchall()
    if not rows:
        return 0
    ids = [r['id'] for r in rows]
    ph = ','.join('?' * len(ids))
    c.execute(f'DELETE FROM discovery_members WHERE camera_id IN ({ph})', ids)
    c.execute(f'DELETE FROM validation_queue WHERE camera_id IN ({ph})', ids)
    c.execute(f'DELETE FROM cameras WHERE id IN ({ph})', ids)
    c.commit()
    for row in rows:
        if row['snapshot_path']:
            try:
                os.remove(os.path.join(SNAPSHOT_DIR, row['snapshot_path']))
            except OSError:
                pass
    log.info('Database cleanup: permanently removed %d old deleted cameras', len(ids))
    return len(ids)


def db_set_snapshot(cam_id: str, filename: str) -> None:
    _db_conn().execute(
        "UPDATE cameras SET snapshot_path=?,updated_at=datetime('now') WHERE id=?",
        (filename, cam_id))
    _db_conn().commit()


def db_get_camera(cam_id: str) -> Optional[dict]:
    row = _db_conn().execute(
        'SELECT * FROM cameras WHERE id=? AND deleted=0', (cam_id,)).fetchone()
    cam = _db_row(row) if row else None
    if cam and not _camera_access_allowed(cam):
        return None
    return cam


def db_promote_rtsp_url(cam_id: str, rtsp_url: str) -> None:
    """Put a URL that just produced video first without discarding alternatives."""
    if not rtsp_url:
        return
    c = _db_conn()
    row = c.execute('SELECT rtsp_urls FROM cameras WHERE id=?', (cam_id,)).fetchone()
    if not row:
        return
    try:
        urls = json.loads(row['rtsp_urls'] or '[]')
    except Exception:
        urls = []
    urls = [rtsp_url] + [u for u in urls if u != rtsp_url]
    c.execute("UPDATE cameras SET rtsp_urls=?,updated_at=datetime('now') WHERE id=?",
              (json.dumps(urls), cam_id))
    c.commit()


def db_get_due_cameras(limit: int = 2000) -> list[dict]:
    rows = _db_conn().execute("""
        SELECT c.* FROM cameras c JOIN validation_queue q ON q.camera_id=c.id
        WHERE c.deleted=0 AND q.next_attempt <= datetime('now')
          AND (q.locked_until IS NULL OR q.locked_until < datetime('now'))
        ORDER BY CASE WHEN COALESCE(q.attempts,0)=0 THEN 0 ELSE 1 END,q.priority,q.next_attempt,c.id LIMIT ?
    """, (max(1, min(limit, 10000)),)).fetchall()
    return [_db_row(r) for r in rows]


def db_get_resume_batch(limit: int = 2000) -> list[dict]:
    worker_id = f'validator-{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex[:8]}'
    return _claim_validation_batch(limit, worker_id)


def _normalize_group_name(value: str) -> str:
    value = _safe_str(value).replace('\x00', ' ').strip()
    return re.sub(r'\s+', ' ', value)[:120]


def db_transfer_cameras(cam_ids: list[str], target_group: str) -> int:
    target_group = _normalize_group_name(target_group)
    if not target_group:
        raise ValueError('Target category is required')
    ids = list(dict.fromkeys(str(x) for x in cam_ids if str(x).strip()))
    if not ids:
        return 0
    c = _db_conn(); total = 0
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]; ph = ','.join('?' * len(chunk))
        cur = c.execute("UPDATE cameras SET group_name=?, updated_at=datetime('now') WHERE deleted=0 AND id IN (" + ph + ")", [target_group] + chunk)
        total += cur.rowcount
    c.execute("INSERT INTO category_actions(action,target_category,camera_count) VALUES ('transfer',?,?)", (target_group, total))
    c.commit(); return total


def db_delete_group(group_name: str) -> int:
    group_name = _normalize_group_name(group_name)
    if not group_name:
        raise ValueError('Category is required')
    c = _db_conn()
    rows = c.execute("SELECT id FROM cameras WHERE deleted=0 AND COALESCE(NULLIF(group_name,''),NULLIF(country,''),'Unknown')=?", (group_name,)).fetchall()
    ids = [r['id'] for r in rows]
    if not ids: return 0
    now = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]; ph = ','.join('?' * len(chunk))
        c.execute("UPDATE cameras SET deleted=1, deleted_at=?, updated_at=? WHERE deleted=0 AND id IN (" + ph + ")", [now, now] + chunk)
        c.execute("DELETE FROM validation_queue WHERE camera_id IN (" + ph + ")", chunk)
    c.execute("INSERT INTO category_actions(action,category,camera_count) VALUES ('delete',?,?)", (group_name, len(ids)))
    c.commit(); return len(ids)


def _db_get_group_stats_uncached() -> list[dict]:
    c = _db_conn()
    where = ['c.deleted=0']
    params = []
    if _auth_enabled() and _current_role() != 'admin':
        user = _current_user()
        where.append('''(
            NOT EXISTS (SELECT 1 FROM v43_user_access_scope sx WHERE sx.username=?)
            OR EXISTS (
                SELECT 1 FROM v43_user_access_scope s WHERE s.username=? AND
                ((s.scope_type='camera' AND s.scope_value=c.id) OR
                 (s.scope_type='country' AND lower(s.scope_value)=lower(c.country)) OR
                 (s.scope_type='device' AND (lower(s.scope_value)=lower(c.device_id) OR lower(s.scope_value)=lower(c.ip))))
            )
        )''')
        params.extend([user, user])
    rows = c.execute(f'''
        SELECT COALESCE(NULLIF(c.group_name,''), NULLIF(c.country,''), 'Unknown') AS display_group,
               MAX(NULLIF(c.area,'')) AS area, MAX(NULLIF(c.city,'')) AS city,
               MAX(NULLIF(c.region,'')) AS region, MAX(NULLIF(c.country,'')) AS country,
               MAX(NULLIF(c.camera_type,'')) AS camera_type, MAX(NULLIF(c.label,'')) AS sample_label, MAX(NULLIF(c.ip,'')) AS sample_ip,
               MAX(c.port) AS sample_port,
               COUNT(*) AS total, SUM(c.status='online') AS online, SUM(c.status='offline') AS offline,
               SUM(c.status='checking') AS checking, SUM(c.status='unknown') AS unknown,
               SUM(c.favorite=1) AS favorites, MIN(c.source_file) AS sample_source, MAX(c.last_seen) AS last_active
        FROM cameras c WHERE {' AND '.join(where)}
        GROUP BY display_group ORDER BY SUM(c.status='online') DESC, display_group
    ''', params).fetchall()
    out=[]
    for r in rows:
        d=dict(r)
        for k,v in d.items():
            if isinstance(v,str): d[k]=_safe_str(v)
        # Category headings are display-only: preserve display_group/group_key for
        # filtering, while showing country, area, city, vendor, and IP:port.
        raw_group = _safe_str(d.get('display_group') or '').strip()
        camera_type = _safe_str(d.get('camera_type') or '').strip()
        sample_label = _safe_str(d.get('sample_label') or '').strip()
        known_vendors = (
            ('Hikvision', ('hikvision', 'hik')),
            ('Dahua', ('dahua', 'anjhua')),
            ('Uniview', ('uniview', 'unv')),
            ('Axis', ('axis',)), ('Hanwha', ('hanwha', 'wisenet')),
            ('Tiandy', ('tiandy',)), ('Vivotek', ('vivotek',)),
            ('Bosch', ('bosch',)), ('Reolink', ('reolink',)),
            ('TP-Link', ('tp-link', 'tplink')), ('Foscam', ('foscam',)),
            ('Amcrest', ('amcrest',)),
        )
        vendor = ''
        for candidate, needles in known_vendors:
            if any(re.search(r'(?<![A-Za-z0-9])' + re.escape(n) + r'(?![A-Za-z0-9])', value, re.I)
                   for value in (camera_type, sample_label, raw_group) for n in needles):
                vendor = candidate
                break
        if not vendor and camera_type and camera_type.lower() not in {
            'generic', 'generic_rtsp', 'generic_rtsp_pending', 'unknown',
            'vendor_unsupported', 'camera', 'rtsp'
        }:
            vendor = camera_type
        if not vendor:
            vendor = 'RTSP Device'

        # Add location parts in the requested order. Drop blanks and duplicate
        # values (e.g. when area and city are both recorded as Sholapur).
        location_parts = []
        for key in ('country', 'area', 'city'):
            value = _safe_str(d.get(key) or '').strip()
            if not value or value.lower() in {'unknown', 'n/a', 'none', 'null'}:
                continue
            if value.casefold() not in {part.casefold() for part in location_parts}:
                location_parts.append(value)
        endpoint_ip = _safe_str(d.get('sample_ip') or '').strip()
        endpoint_port = d.get('sample_port')
        endpoint = f'{endpoint_ip}:{endpoint_port}' if endpoint_ip and endpoint_port else endpoint_ip

        # Include region/state only when no area/city has been saved, so the
        # requested country · area · city order remains easy to scan.
        if not any(key in d and _safe_str(d.get(key) or '').strip() for key in ('area', 'city')):
            region = _safe_str(d.get('region') or '').strip()
            if region and region.casefold() not in {part.casefold() for part in location_parts}:
                location_parts.insert(1 if location_parts else 0, region)
        title_parts = location_parts + [vendor]
        if endpoint:
            title_parts.append(endpoint)
        d['display_title'] = ' · '.join(title_parts)
        out.append(d)
    return out


def db_get_group_stats() -> list[dict]:
    return _cached_read('group_stats', 5.0, _db_get_group_stats_uncached)


def _camera_display_name(cam: dict) -> str:
    """Return a useful camera title without showing generic placeholders as names."""
    label = _safe_str(cam.get('label') or '').strip()
    channel_name = _safe_str(cam.get('channel_name') or '').strip()
    camera_type = _safe_str(cam.get('camera_type') or '').strip()
    if camera_type.lower().replace('-', '_').replace(' ', '_') in {'generic', 'generic_rtsp', 'generic_rtsp_pending', 'unknown', 'vendor_unsupported'}:
        camera_type = 'Camera'
    ip = _safe_str(cam.get('ip') or '').strip()
    channel_index = cam.get('channel_index') or 0
    generic_patterns = (
        r'^(?:generic|camera|unknown|rtsp(?:[_ -].*)?|generic_rtsp_pending|generic_rtsp)'
        r'(?:\s*[—-].*)?$',
    )
    pending_stream_label = bool(re.match(r'^supplied rtsp stream(?: \(pending\))?$', label, re.I)
                                or re.match(r'^imported rtsp stream$', label, re.I))
    label_is_generic = (not label or label.lower() in {'camera', 'unknown', 'generic', 'generic_rtsp', 'generic_rtsp_pending'}
                        or any(re.match(pattern, label, re.I) for pattern in generic_patterns)
                        or pending_stream_label)
    if channel_name and channel_name.lower() not in {'channel', 'unknown', 'rtsp pending'} and not re.search(r'rtsp pending', channel_name, re.I):
        name = channel_name
    elif pending_stream_label:
        name = f'Channel {int(channel_index or 1)} — RTSP pending' if 'pending' in label.lower() else f'Channel {int(channel_index or 1)} — RTSP stream'
    elif not label_is_generic:
        name = label
    elif channel_index:
        name = f'{camera_type or "Camera"} — Channel {channel_index}'
    else:
        name = f'{camera_type or "Camera"} — {ip}' if ip else 'Unnamed camera'
    # Avoid titles that are just an IP address or a generic vendor label.
    if name.lower() in {'generic', 'camera', 'unknown', 'generic_rtsp_pending', 'generic_rtsp'}:
        name = f'{camera_type or "Camera"} — {ip}' if ip else 'Unnamed camera'
    return name[:180]


def db_get_cameras_by_group(display_group: str, limit: int = PAGE_SIZE,
                             offset: int = 0) -> list[dict]:
    if display_group == '__favorites__':
        where = 'c.favorite=1'; params=[]
    else:
        where = "COALESCE(NULLIF(c.group_name,''),NULLIF(c.country,''),'Unknown')=?"; params=[display_group]
    if _auth_enabled() and _current_role() != 'admin':
        user=_current_user()
        where += ''' AND (
            NOT EXISTS (SELECT 1 FROM v43_user_access_scope sx WHERE sx.username=?)
            OR EXISTS (
                SELECT 1 FROM v43_user_access_scope s WHERE s.username=? AND
                ((s.scope_type='camera' AND s.scope_value=c.id) OR
                 (s.scope_type='country' AND lower(s.scope_value)=lower(c.country)) OR
                 (s.scope_type='device' AND (lower(s.scope_value)=lower(c.device_id) OR lower(s.scope_value)=lower(c.ip))))
            )
        )'''
        params.extend([user,user])
    rows=_db_conn().execute(
        f'SELECT c.* FROM cameras c WHERE c.deleted=0 AND {where} '
        f'ORDER BY {_STATUS_SORT.replace("status","c.status")}, COALESCE(c.last_seen,"") DESC, c.ip LIMIT ? OFFSET ?',
        params+[max(1,int(limit)),max(0,int(offset))]).fetchall()
    result = [_db_row(r) for r in rows]
    for cam in result:
        cam['display_name'] = _camera_display_name(cam)
    return result


def db_get_group_count(display_group: str) -> int:
    c=_db_conn()
    if display_group == '__favorites__':
        where='c.favorite=1'; params=[]
    else:
        where="COALESCE(NULLIF(c.group_name,''),NULLIF(c.country,''),'Unknown')=?"; params=[display_group]
    if _auth_enabled() and _current_role() != 'admin':
        user=_current_user()
        where += ''' AND (
            NOT EXISTS (SELECT 1 FROM v43_user_access_scope sx WHERE sx.username=?)
            OR EXISTS (
                SELECT 1 FROM v43_user_access_scope s WHERE s.username=? AND
                ((s.scope_type='camera' AND s.scope_value=c.id) OR
                 (s.scope_type='country' AND lower(s.scope_value)=lower(c.country)) OR
                 (s.scope_type='device' AND (lower(s.scope_value)=lower(c.device_id) OR lower(s.scope_value)=lower(c.ip))))
            )
        )'''
        params.extend([user,user])
    row=c.execute(f'SELECT COUNT(*) FROM cameras c WHERE c.deleted=0 AND {where}',params).fetchone()
    return int(row[0] or 0) if row else 0


def db_get_cameras_by_source(source_file: str) -> list[dict]:
    rows = _db_conn().execute(
        f'SELECT * FROM cameras WHERE deleted=0 AND source_file=? '
        f'ORDER BY {_STATUS_SORT}, ip', (source_file,)).fetchall()
    return _filter_camera_access([_db_row(r) for r in rows])


def db_get_source_stats() -> list[dict]:
    rows = _db_conn().execute(f'''
        SELECT source_file,
               COUNT(*)           AS total,
               SUM(status='online') AS online,
               SUM(status='offline') AS offline
        FROM cameras WHERE deleted=0 AND source_file!=''
        GROUP BY source_file ORDER BY total DESC
    ''').fetchall()
    return [dict(r) for r in rows]


def _db_get_stats_uncached() -> dict:
    c = _db_conn()
    clauses = ['c.deleted=0']
    params = []
    if _auth_enabled() and _current_role() != 'admin':
        user = _current_user()
        clauses.append('''(
            NOT EXISTS (SELECT 1 FROM v43_user_access_scope sx WHERE sx.username=?)
            OR EXISTS (
                SELECT 1 FROM v43_user_access_scope s WHERE s.username=? AND
                ((s.scope_type='camera' AND s.scope_value=c.id) OR
                 (s.scope_type='country' AND lower(s.scope_value)=lower(c.country)) OR
                 (s.scope_type='device' AND (lower(s.scope_value)=lower(c.device_id) OR lower(s.scope_value)=lower(c.ip))))
            )
        )''')
        params.extend([user, user])
    where = ' AND '.join(clauses)
    row = c.execute(f'''
        SELECT COUNT(*) AS total, SUM(c.status='online') AS online, SUM(c.status='offline') AS offline,
               SUM(c.status='checking') AS checking, SUM(c.status='unknown') AS unknown, SUM(c.favorite=1) AS favorites,
               COUNT(DISTINCT COALESCE(NULLIF(c.group_name,''),NULLIF(c.country,''),'Unknown')) AS groups
        FROM cameras c WHERE {where}
    ''', params).fetchone()
    return {k:(v or 0) for k,v in dict(row).items()} if row else {'total':0,'online':0,'offline':0,'checking':0,'unknown':0,'favorites':0,'groups':0}


def db_get_stats() -> dict:
    # Stats are informational; a 3-second cache prevents repeated COUNT/GROUP scans
    # from competing with validation writes.
    return _cached_read('stats', 3.0, _db_get_stats_uncached)


def db_search_cameras(query: str = '', status: str = 'all',
                      group: str = 'all', favorites: bool = False,
                      online_only: bool = False, limit: int = PAGE_SIZE,
                      offset: int = 0) -> tuple[list[dict], int]:
    """Search cameras in SQLite instead of filtering only loaded cards."""
    clauses = ['deleted=0']
    params: list = []
    q = (query or '').strip().lower()
    if q:
        like = f'%{q}%'
        clauses.append(
            "LOWER(COALESCE(label,'') || ' ' || COALESCE(ip,'') || ' ' || "
            "COALESCE(country,'') || ' ' || COALESCE(region,'') || ' ' || "
            "COALESCE(city,'') || ' ' || COALESCE(camera_type,'') || ' ' || "
            "COALESCE(group_name,'')) LIKE ?")
        params.append(like)
    if status and status != 'all':
        clauses.append('status=?')
        params.append(status)
    if group and group != 'all':
        if group == '__favorites__':
            clauses.append('favorite=1')
        else:
            clauses.append("COALESCE(NULLIF(group_name,''),"
                           "NULLIF(country,''),'Unknown')=?")
            params.append(group)
    if favorites:
        clauses.append('favorite=1')
    if online_only:
        clauses.append("status='online'")
    where = ' AND '.join(clauses)
    c = _db_conn()
    total = c.execute(f'SELECT COUNT(*) FROM cameras WHERE {where}',
                      params).fetchone()[0]
    rows = c.execute(
        f'SELECT * FROM cameras WHERE {where} '
        f'ORDER BY {_STATUS_SORT}, COALESCE(last_seen,"") DESC, ip '
        'LIMIT ? OFFSET ?', params + [
            max(1, min(int(limit), 500)), max(0, int(offset))
        ]).fetchall()
    filtered = _filter_camera_access([_db_row(row) for row in rows])
    if _auth_enabled() and _current_role() != 'admin':
        # Scope filtering happens after SQL criteria but before pagination.
        all_rows = _filter_camera_access([_db_row(r) for r in c.execute(f'SELECT * FROM cameras WHERE {where} ORDER BY {_STATUS_SORT}, COALESCE(last_seen,"") DESC, ip', params).fetchall()])
        total = len(all_rows)
        filtered = all_rows[max(0,int(offset)):max(0,int(offset))+max(1,min(int(limit),500))]
    return filtered, total


def db_cleanup_snapshots(retention_days: int = SNAPSHOT_RETENTION_DAYS) -> int:
    """Remove old snapshot files and clear their camera references."""
    cutoff = time.time() - max(1, retention_days) * 86400
    removed = 0
    c = _db_conn()
    rows = c.execute(
        "SELECT id,snapshot_path FROM cameras WHERE snapshot_path!=''"
    ).fetchall()
    for row in rows:
        path = os.path.join(SNAPSHOT_DIR, os.path.basename(row['snapshot_path']))
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                c.execute(
                    "UPDATE cameras SET snapshot_path='',updated_at=datetime('now') "
                    "WHERE id=?", (row['id'],))
                removed += 1
        except OSError:
            continue
    c.commit()
    return removed


# ══════════════════════════════════════════════════════════════════════════════
# DISCOVERY  (recursive file scanner)
# ══════════════════════════════════════════════════════════════════════════════

_DAHUA_TYPES  = {'dahua', 'anjhua', 'loocam', 'imou'}
_HIKV_TYPES   = {'hikvision', 'hik vision', 'ezviz', 'hiwatch'}
_FOSCAM_TYPES = {'foscam'}
_NVR_TYPES    = {'nvr', 'network video recorder', 'dvr'}
_AXIS_TYPES   = {'axis', 'axis communications'}
_UNIVIEW_TYPES = {'uniview', 'unv'}
_ATTR_RE      = re.compile(r'(\w[\w-]*)=["\']([^"\']*)["\']')


def _detect_brand(ctype: str) -> str:
    ct = ctype.lower()
    if any(x in ct for x in _DAHUA_TYPES):  return 'dahua'
    if any(x in ct for x in _HIKV_TYPES):   return 'hikvision'
    if any(x in ct for x in _FOSCAM_TYPES): return 'foscam'
    if any(x in ct for x in _NVR_TYPES):    return 'nvr'
    if any(x in ct for x in _AXIS_TYPES):   return 'axis'
    if any(x in ct for x in _UNIVIEW_TYPES): return 'uniview'
    if 'reolink' in ct: return 'reolink'
    if 'tiandy' in ct: return 'tiandy'
    if 'amcrest' in ct: return 'amcrest'
    if 'hanwha' in ct or 'wisenet' in ct or 'samsung' in ct:
        return 'hanwha'
    return 'generic'


def _is_camera(ctype: str) -> bool:
    return not any(x in ctype.lower() for x in _SKIP_TYPES)


def _build_rtsp_urls(cam: dict) -> list[str]:
    ip    = cam.get('ip', '')
    user  = cam.get('username', 'admin')
    pwd   = cam.get('password', '')
    brand = _detect_brand(cam.get('camera_type', ''))
    creds = f'{user}:{pwd}@' if user else ''
    base  = f'rtsp://{creds}{ip}:{int(cam.get("rtsp_port") or 554)}'
    existing = list(cam.get('rtsp_urls', []))
    if brand == 'dahua':
        built = [f'{base}/cam/realmonitor?channel=1&subtype=0',
                 f'{base}/cam/realmonitor?channel=1&subtype=1']
    elif brand == 'hikvision':
        built = [f'{base}/Streaming/Channels/101',
                 f'{base}/Streaming/Channels/102',
                 f'{base}/h264/ch1/main/av_stream']
    elif brand == 'foscam':
        built = [f'{base}/videoMain', f'{base}/videoSub', f'{base}/live',
                 f'{base}/11', f'{base}/video1']
    elif brand == 'axis':
        built = [f'{base}/axis-media/media.amp',
                 f'{base}/axis-media/media.amp?videocodec=h264',
                 f'{base}/mpeg4/media.amp', f'{base}/live', f'{base}/1']
    elif brand == 'uniview':
        built = [f'{base}/media/video1', f'{base}/media/video2',
                 f'{base}/Streaming/Channels/101']
    elif brand == 'reolink':
        built = [f'{base}/h264Preview_01_main',
                 f'{base}/h264Preview_01_sub',
                 f'{base}/h264Preview_01_fluent',
                 f'{base}/Streaming/Channels/101']
    elif brand == 'tiandy':
        built = [f'{base}/Streaming/Channels/101', f'{base}/live/ch00_0',
                 f'{base}/cam/realmonitor?channel=1&subtype=0',
                 f'{base}/stream1']
    elif brand == 'amcrest':
        built = [f'{base}/cam/realmonitor?channel=1&subtype=0&unicast=true',
                 f'{base}/Streaming/Channels/101',
                 f'{base}/h264/ch1/main/av_stream', f'{base}/live']
    elif brand == 'hanwha':
        built = [f'{base}/profile1/media.smp', f'{base}/profile2/media.smp',
                 f'{base}/Streaming/Channels/101', f'{base}/live']
    elif brand == 'nvr':
        built = [f'{base}/cam/realmonitor?channel=1&subtype=0',
                 f'{base}/Streaming/Channels/101',
                 f'{base}/Streaming/Channels/1',
                 f'{base}/live/ch00_0',
                 f'{base}/h264/ch1/main/av_stream',
                 f'{base}/live']
    else:
        # Generic/door-camera candidates. Keep this bounded and read-only;
        # they are tested only against the explicitly supplied camera host
        # with the credentials already stored for that camera.
        built = [
            f'{base}/stream1', f'{base}/stream2', f'{base}/stream',
            f'{base}/live', f'{base}/live/ch0', f'{base}/live/ch00_0',
            f'{base}/live/ch01_0', f'{base}/h264Preview_01_main',
            f'{base}/h264Preview_01_sub', f'{base}/cam/realmonitor?channel=1&subtype=0',
            f'{base}/Streaming/Channels/101', f'{base}/Streaming/Channels/102',
            f'{base}/media/video1', f'{base}/media/video2',
            f'{base}/videoMain', f'{base}/videoSub', f'{base}/video1',
            f'{base}/video2', f'{base}/1', f'{base}/2', f'{base}/11',
        ]
    return existing + [u for u in built if u not in existing]


def _detect_http_video_feed_for_camera(cam: dict, max_services: int = 20, max_candidates: int = 36) -> dict:
    """Detect an authenticated video feed exposed through HTTP/HTTPS services."""
    host = str(cam.get('ip') or '').strip()
    if not host or _requests is None:
        return {'ok': False, 'detail': 'HTTP video detection unavailable'}
    username = str(cam.get('username') or '')
    password = str(cam.get('password') or '')
    try:
        probe = _probe_device_http_ports(host, DEVICE_HTTP_PORTS, username, password)
    except Exception as exc:
        return {'ok': False, 'detail': f'HTTP service probe failed: {type(exc).__name__}'}
    services = probe.get('http_services') or []
    if not services:
        return {'ok': False, 'detail': 'No HTTP/HTTPS service found', 'http_probe': probe}
    sess, auths = _device_session(username, password)
    media_patterns = [
        r"(?i)(?:https?://[^\s\"'<>]+\.(?:m3u8|flv|mjpeg|mjpg|mp4)(?:\?[^\s\"'<>]*)?)",
        r"(?i)(?:[\"'])([^\"']+\.(?:m3u8|flv|mjpeg|mjpg|mp4)(?:\?[^\"']*)?)(?:[\"'])",
        r"(?i)(?:streamUrl|streamURL|playUrl|playURL|videoUrl|videoURL|liveUrl|liveURL|hlsUrl|hlsURL)\s*[:=]\s*[\"']([^\"']+)[\"']",
    ]
    common_paths = ['/', '/index.html', '/index.htm', '/live', '/liveview', '/preview', '/video', '/video/live', '/stream', '/stream1', '/stream2', '/live.m3u8', '/stream.m3u8', '/index.m3u8', '/hls.m3u8', '/live.flv', '/live/mjpeg', '/video.mjpg', '/mjpeg']
    candidates=[]; seen=set(); pages=[]
    def add(u, base=''):
        if not u: return
        u=u.strip().replace('\\/','/')
        if u.startswith('//'): u='http:'+u
        u=urljoin(base or f'http://{host}/', u)
        if not (u.startswith('http://') or u.startswith('https://')): return
        if urlsplit(u).hostname not in (host, host.strip('[]')): return
        parsed=urlsplit(u)
        clean=f'{parsed.scheme}://{host}:{parsed.port or (443 if parsed.scheme=="https" else 80)}{parsed.path or "/"}'
        if parsed.query: clean += '?' + parsed.query
        if clean not in seen and len(candidates)<max_candidates:
            seen.add(clean); candidates.append(clean)
    for svc in services[:max_services]:
        port=int(svc.get('port') or 80)
        scheme=svc.get('scheme') or ('https' if svc.get('https') else 'http')
        root=f'{scheme}://{host}:{port}/'
        add(root)
        for path in common_paths: add(path, root)
        try:
            response=_device_get(sess, auths, root, timeout=4)
            if hasattr(response,'status_code') and response.status_code < 500:
                text_body=response.text or ''
                pages.append({'port':port,'url':root,'status':int(response.status_code)})
                for pat in media_patterns:
                    for m in re.findall(pat, text_body):
                        val=m if isinstance(m,str) else (m[0] if m else '')
                        add(val, root)
                for asset in re.findall(r"(?i)(?:src|href)\s*=\s*[\"']([^\"']+)[\"']", text_body):
                    if any(x in asset.lower() for x in ('.js','.m3u8','.flv','.mjpeg','.mjpg','.mp4','stream','video','live')):
                        add(asset, root)
                        if asset.lower().endswith('.js') or '.js?' in asset.lower():
                            try:
                                js=_device_get(sess, auths, urljoin(root,asset), timeout=4)
                                if hasattr(js,'status_code') and js.status_code < 500:
                                    jb=js.text or ''
                                    for pat in media_patterns:
                                        for m in re.findall(pat,jb):
                                            val=m if isinstance(m,str) else (m[0] if m else '')
                                            add(val, root)
                            except Exception: pass
        except Exception: pass
    for u in candidates:
        low=u.lower()
        if not any(ext in low for ext in ('.m3u8','.flv','.mjpeg','.mjpg','.mp4')): continue
        cmd=['ffprobe','-v','error','-rw_timeout','3500000','-analyzeduration','2500000','-probesize','2500000','-select_streams','v:0','-show_entries','stream=codec_name,width,height,r_frame_rate','-of','json',u]
        try:
            proc=subprocess.run(cmd,capture_output=True,timeout=4.0)
            if proc.returncode!=0: continue
            data=json.loads(proc.stdout.decode(errors='ignore') or '{}'); streams=data.get('streams') or []
            if not streams: continue
            st=streams[0]; parsed=urlsplit(u)
            safe=f'{parsed.scheme}://{host}:{parsed.port or (443 if parsed.scheme=="https" else 80)}{parsed.path or "/"}'
            if parsed.query: safe += '?' + parsed.query
            return {'ok':True,'protocol':parsed.scheme,'http_port':parsed.port or (443 if parsed.scheme=='https' else 80),'media_type':('hls' if '.m3u8' in low else 'http-flv' if '.flv' in low else 'mjpeg' if ('.mjpeg' in low or '.mjpg' in low) else 'http-video'),'safe_url':safe,'url':u,'codec':st.get('codec_name') or '','width':st.get('width'),'height':st.get('height'),'fps':st.get('r_frame_rate') or '','http_services':services,'pages_checked':pages,'candidates':len(candidates)}
        except Exception: continue
    return {'ok':False,'detail':'HTTP web service found, but no decodable video endpoint was discovered','http_services':services,'pages_checked':pages,'candidates':len(candidates)}

def _detect_rtsp_feed_for_camera(cam: dict, max_candidates: int = 18) -> dict:
    """Find a working authenticated RTSP video feed on one supplied camera.

    This is intentionally limited to the camera host already present in the
    database and uses the stored username/password. It does not scan external
    hosts, brute-force credentials, or modify the device.
    """
    host = str(cam.get('ip') or '').strip()
    if not host:
        return {'ok': False, 'detail': 'Camera has no device host'}
    username = str(cam.get('username') or '')
    password = str(cam.get('password') or '')
    try:
        default_port = int(cam.get('rtsp_port') or 554)
    except (TypeError, ValueError):
        default_port = 554

    # Prefer the stored port, then a small bounded set of standard RTSP ports.
    port_candidates = []
    for port in [default_port, 554, 8554, 10554, 5554, 7554]:
        if port not in DEVICE_RTSP_PORTS:
            continue
        if port not in port_candidates:
            port_candidates.append(port)
    reachable_ports = []
    for port in port_candidates:
        try:
            with socket.create_connection((host, port), timeout=DEVICE_PORT_SCAN_TIMEOUT):
                reachable_ports.append(port)
        except OSError:
            pass
    if not reachable_ports:
        return {'ok': False, 'detail': 'No RTSP port reachable', 'ports_checked': port_candidates}

    candidates = _build_rtsp_urls(cam)
    # Existing URLs are valuable, but avoid testing an unbounded inventory.
    candidates = candidates[:max(1, min(int(max_candidates), 12))]
    deadline = time.monotonic() + float(os.getenv('CAMERA_RTSP_DETECT_MAX_SECONDS','45'))
    tried = []

    def _url_for(port: int, candidate: str) -> str:
        parsed = urlsplit(candidate)
        path = parsed.path or '/'
        if parsed.query:
            path += '?' + parsed.query
        from urllib.parse import quote
        auth = ''
        if username:
            auth = quote(username, safe='')
            if password:
                auth += ':' + quote(password, safe='')
            auth += '@'
        return f'rtsp://{auth}{host}:{port}{path}'

    for port in reachable_ports:
        if time.monotonic() >= deadline: break
        for candidate in candidates:
            if time.monotonic() >= deadline: break
            url = _url_for(port, candidate)
            # Do not include credential-bearing URLs in returned diagnostics.
            safe_url = f'rtsp://{host}:{port}{urlsplit(url).path}'
            if urlsplit(url).query:
                safe_url += '?' + urlsplit(url).query
            tried.append(safe_url)
            cmd = [
                'ffprobe', '-v', 'error', '-rtsp_transport', 'tcp',
                '-rw_timeout', '3500000', '-analyzeduration', '2500000',
                '-probesize', '2500000', '-select_streams', 'v:0',
                '-show_entries', 'stream=codec_name,width,height,r_frame_rate',
                '-of', 'json', url,
            ]
            try:
                proc = None
                for transport in ('tcp','udp'):
                    cmd[3] = transport
                    proc = subprocess.run(cmd, capture_output=True, timeout=3.0)
                    if proc.returncode == 0: break
                if not proc or proc.returncode != 0:
                    continue
                data = json.loads(proc.stdout.decode(errors='ignore') or '{}')
                streams = data.get('streams') or []
                if not streams:
                    continue
                stream = streams[0]
                detected_vendor = _infer_vendor_from_rtsp_url(url) or _detect_brand(cam.get('camera_type') or '')
                return {
                    'ok': True, 'rtsp_url': url, 'safe_url': safe_url,
                    'port': port, 'vendor': detected_vendor or 'generic',
                    'codec': stream.get('codec_name') or '',
                    'width': stream.get('width'), 'height': stream.get('height'),
                    'fps': stream.get('r_frame_rate') or '',
                    'ports_checked': port_candidates,
                    'reachable_ports': reachable_ports,
                    'tried': len(tried),
                }
            except (subprocess.TimeoutExpired, ValueError, json.JSONDecodeError, OSError):
                continue
    return {
        'ok': False, 'detail': 'No candidate produced a decodable video stream',
        'ports_checked': port_candidates, 'reachable_ports': reachable_ports,
        'tried': tried[-24:], 'tried_count': len(tried),
    }


def _rtsp_credentials(rtsp_url: str) -> tuple[str, str, str, int]:
    """Extract non-secret connection fields from one stored RTSP URL."""
    try:
        parsed = urlsplit(rtsp_url)
        host = parsed.hostname or ''
        user = unquote(parsed.username or '')
        password = unquote(parsed.password or '')
        port = int(parsed.port or 554)
        return host, user, password, port
    except (TypeError, ValueError):
        return '', '', '', 554


def _cam_id(ip: str, port: int, username: str, rtsp: str = '') -> str:
    key = rtsp if rtsp else f'{ip}:{port}:{username}'
    return hashlib.md5(key.encode()).hexdigest()[:12]


def _parse_valid_camera_block(block: str, source_file: str) -> Optional[dict]:
    lines = [l.strip() for l in block.strip().splitlines() if l.strip()]
    cam: dict = {'source_file': source_file, 'rtsp_urls': []}
    for line in lines:
        # Keep a complete RTSP URL before splitting key/value fields on ':'.
        # The old ordering silently discarded URL candidates because "rtsp://"
        # looked like a key with a colon.
        if line.lower().startswith(('rtsp://','rtsps://')):
            cam['rtsp_urls'].append(line.split('#', 1)[0].strip())
            continue
        kv = line.split(':', 1)
        if len(kv) < 2:
            continue
        key, val = kv[0].strip().lower(), kv[1].strip()
        if   'camera type' in key:   cam['camera_type'] = val
        elif 'ip address'  in key:   cam['ip']          = val
        elif 'rtsp' in key and 'port' in key:
            try: cam['rtsp_port'] = int(val)
            except: pass
        elif 'port' in key:
            try: cam['port'] = int(val)
            except: pass
        elif 'username' in key:      cam['username'] = val
        elif 'password' in key:      cam['password'] = val
        elif 'country'  in key:      cam['country']  = val
        elif 'city'     in key:      cam['city']     = val
        elif 'region'   in key:      cam['region']   = val
        elif 'rtsp' in key and val.lower().startswith('//'):
            cam['rtsp_urls'].append('rtsp:' + val)
    if not cam.get('ip') or not _is_camera(cam.get('camera_type', 'generic')):
        return None
    if cam.get('rtsp_urls') and not cam.get('username'):
        _, user, password, rtsp_port = _rtsp_credentials(cam['rtsp_urls'][0])
        cam['username'] = user
        cam['password'] = password
        cam.setdefault('rtsp_port', rtsp_port)
    cam['rtsp_urls']  = _build_rtsp_urls(cam)
    cam['web_url']    = f"http://{cam['ip']}:{cam.get('port', 80)}"
    cam['id']         = _cam_id(cam['ip'], cam.get('port', 80), cam.get('username', ''))
    cam['label']      = f"{cam.get('camera_type','Camera')} — {cam['ip']}"
    cam['group_name'] = cam.get('country', 'Unknown')
    return cam


def _parse_rtsp_file(path: str) -> list[dict]:
    cams: list[dict] = []
    current_label = 'Unknown'
    try:
        with open(path, encoding='utf-8', errors='ignore') as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                if line.lower().startswith(('rtsp://','rtsps://')):
                    rtsp_url = line.split('#')[0].strip()
                    if not rtsp_url:
                        continue
                    cid = _cam_id('', 554, '', rtsp_url)
                    m   = re.search(r'@?([\d.]+):\d+', rtsp_url)
                    parsed_host, rtsp_user, rtsp_password, rtsp_port = (
                        _rtsp_credentials(rtsp_url))
                    ip  = parsed_host or (m.group(1) if m else '')
                    cams.append({
                        'id': cid, 'ip': ip, 'port': 554,
                        'camera_type': 'RTSP Stream',
                        'label': f'RTSP — {ip or rtsp_url[:40]}',
                        'rtsp_urls': [rtsp_url], 'web_url': '',
                        'source_file': path, 'country': current_label,
                        'city': '', 'region': '', 'username': rtsp_user,
                        'password': rtsp_password, 'rtsp_port': rtsp_port,
                        'group_name': current_label,
                    })
                else:
                    current_label = line.rstrip(':').strip() or 'Unknown'
    except Exception as e:
        log.debug('Error parsing RTSP file %s: %s', path, e)
    return cams


def _parse_m3u_file(path: str) -> list[dict]:
    cams: list[dict] = []
    current_name  = ''
    current_group = os.path.splitext(os.path.basename(path))[0]
    seen_ids: set  = set()
    try:
        source = open(path, encoding='utf-8-sig', errors='ignore')
    except Exception:
        return cams
    try:
      for raw_line in source:
          line = raw_line.strip()
          if not line or line.startswith('#EXTM3U'):
              continue
          if line.startswith('#EXTINF'):
              attrs        = dict(_ATTR_RE.findall(line))
              current_name = attrs.get('tvg-name') or attrs.get('tvg-id') or ''
              if not current_name and ',' in line:
                  current_name = line.split(',', 1)[-1].strip()
              current_group = attrs.get('group-title') or current_group
              continue
          if line.startswith('#'):
              continue
          if not line.lower().startswith(('rtsp://','rtsps://')):
              current_name = ''
              continue
          cid = _cam_id('', 554, '', line)
          if cid in seen_ids:
              current_name = ''
              continue
          seen_ids.add(cid)
          m  = re.search(r'rtsp://[^@]*@?([\d.a-zA-Z.\-]+):(\d+)', line)
          parsed_host, rtsp_user, rtsp_password, rtsp_port = _rtsp_credentials(line)
          ip = parsed_host or (m.group(1) if m else '')
          cams.append({
              'id': cid, 'ip': ip, 'port': 554,
              'camera_type': 'M3U Stream',
              'device_class': 'standalone_stream',
              'label': current_name.strip() or f'RTSP — {ip or line[:40]}',
              'rtsp_urls': [line], 'web_url': '',
              'source_file': path, 'country': current_group.strip(),
              'city': '', 'region': '', 'username': rtsp_user,
              'password': rtsp_password, 'rtsp_port': rtsp_port,
              'group_name': current_group.strip(),
          })
          current_name = ''
      return cams
    finally:
        source.close()


def _parse_json_file(path: str) -> list[dict]:
    cams: list[dict] = []
    try:
        data = json.load(open(path, encoding='utf-8', errors='ignore'))
        if not isinstance(data, list):
            data = [data] if isinstance(data, dict) else []
        for item in data:
            if isinstance(item, str) and item.lower().startswith(('rtsp://','rtsps://')):
                cid = _cam_id('', 554, '', item)
                m   = re.search(r'@?([\d.]+):\d+', item)
                cams.append({
                    'id': cid, 'ip': m.group(1) if m else '', 'port': 554,
                    'camera_type': 'JSON Stream',
                    'label': f"RTSP — {m.group(1) if m else item[:40]}",
                    'rtsp_urls': [item], 'web_url': '', 'source_file': path,
                    'country': '', 'city': '', 'region': '',
                    'username': '', 'password': '', 'group_name': 'JSON Import',
                })
            elif isinstance(item, dict):
                rtsp = (item.get('rtsp') or item.get('url') or
                        item.get('stream') or item.get('rtsp_url') or '')
                if not rtsp or not rtsp.lower().startswith(('rtsp://','rtsps://')):
                    continue
                cid = _cam_id('', 554, '', rtsp)
                m   = re.search(r'@?([\d.]+):\d+', rtsp)
                ip  = m.group(1) if m else item.get('ip', '')
                cams.append({
                    'id': cid, 'ip': ip, 'port': item.get('port', 554),
                    'camera_type': item.get('type', item.get('brand', 'JSON Camera')),
                    'label': item.get('label', item.get('name', f'Camera — {ip}')),
                    'rtsp_urls': [rtsp],
                    'web_url': item.get('web_url', item.get('url_web', '')),
                    'source_file': path,
                    'country':  item.get('country', ''),
                    'city':     item.get('city', ''),
                    'region':   item.get('region', ''),
                    'username': item.get('username', item.get('user', '')),
                    'password': item.get('password', item.get('pass', '')),
                    'group_name': item.get('group', item.get('country', 'JSON Import')),
                })
    except Exception as e:
        log.debug('Error parsing JSON file %s: %s', path, e)
    return cams


def _parse_csv_file(path: str) -> list[dict]:
    cams: list[dict] = []
    try:
        with open(path, newline='', encoding='utf-8-sig', errors='ignore') as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Look for RTSP URL in any column
                rtsp = ''
                for key in ('rtsp', 'rtsp_url', 'url', 'stream', 'link'):
                    rtsp = (row.get(key) or row.get(key.upper()) or '').strip()
                    if rtsp.lower().startswith(('rtsp://','rtsps://')):
                        break
                if not rtsp:
                    for v in row.values():
                        if v and v.strip().lower().startswith(('rtsp://','rtsps://')):
                            rtsp = v.strip()
                            break
                if not rtsp:
                    continue
                cid   = _cam_id('', 554, '', rtsp)
                m     = re.search(r'@?([\d.]+):\d+', rtsp)
                ip    = m.group(1) if m else row.get('ip', row.get('IP', ''))
                group = row.get('group', row.get('country', row.get('GROUP', 'CSV Import')))
                cams.append({
                    'id': cid, 'ip': ip, 'port': 554,
                    'camera_type': row.get('type', row.get('brand', 'CSV Camera')),
                    'label': row.get('label', row.get('name', f'Camera — {ip}')),
                    'rtsp_urls': [rtsp], 'web_url': '',
                    'source_file': path,
                    'country':  row.get('country', ''),
                    'city':     row.get('city', ''),
                    'region':   row.get('region', ''),
                    'username': row.get('username', row.get('user', '')),
                    'password': row.get('password', row.get('pass', '')),
                    'group_name': group,
                })
    except Exception as e:
        log.debug('Error parsing CSV file %s: %s', path, e)
    return cams


def _parse_xml_file(path: str) -> list[dict]:
    cams: list[dict] = []
    try:
        tree = ElementTree.parse(path)
        root = tree.getroot()
        # Walk all elements looking for RTSP URLs in text or attributes
        for elem in root.iter():
            # Check attributes
            for attr_val in elem.attrib.values():
                if attr_val and attr_val.lower().startswith(('rtsp://','rtsps://')):
                    rtsp = attr_val.strip()
                    cid  = _cam_id('', 554, '', rtsp)
                    m    = re.search(r'@?([\d.]+):\d+', rtsp)
                    cams.append({
                        'id': cid, 'ip': m.group(1) if m else '', 'port': 554,
                        'camera_type': 'XML Camera',
                        'label': elem.get('name', elem.get('label', f'XML Camera')),
                        'rtsp_urls': [rtsp], 'web_url': '', 'source_file': path,
                        'country': '', 'city': '', 'region': '',
                        'username': '', 'password': '', 'group_name': 'XML Import',
                    })
            # Check text content
            txt = (elem.text or '').strip()
            if txt.lower().startswith(('rtsp://','rtsps://')):
                cid = _cam_id('', 554, '', txt)
                m   = re.search(r'@?([\d.]+):\d+', txt)
                cams.append({
                    'id': cid, 'ip': m.group(1) if m else '', 'port': 554,
                    'camera_type': 'XML Camera',
                    'label': f'XML — {m.group(1) if m else txt[:40]}',
                    'rtsp_urls': [txt], 'web_url': '', 'source_file': path,
                    'country': '', 'city': '', 'region': '',
                    'username': '', 'password': '', 'group_name': 'XML Import',
                })
    except Exception as e:
        log.debug('Error parsing XML file %s: %s', path, e)
    # Deduplicate by id
    seen: set = set()
    return [c for c in cams if not (c['id'] in seen or seen.add(c['id']))]


def _should_skip(path: Path, root: Path) -> bool:
    """Return True if this path should be skipped during discovery."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    for part in relative.parts[:-1]:
        if part.startswith('.') or part in _SKIP_DIRS:
            return True
    return path.name in _SKIP_FILES or path.name in _RUNTIME_SKIP_FILES or path.name.startswith('.')


_last_discovery_members: dict[str, set[str]] = {}


def find_all_cameras(search_dir: str, force: bool = False, on_batch=None, collect: bool = True) -> list[dict]:
    cameras: list[dict] = []
    seen_ids: set        = set()
    root = Path(search_dir).resolve()
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f'Camera discovery directory is missing or not a directory: {root}')
    source_members: dict[str, set[str]] = {}
    current_sources: set[str] = set()
    pending_records=[]

    pending_batch = []
    last_batch_flush = time.monotonic()
    def flush_batch(force: bool = False):
        nonlocal last_batch_flush
        if not on_batch or not pending_batch:
            return
        now = time.monotonic()
        # Flush by size or at least every two seconds; do not wait for the
        # entire workspace traversal to finish before adding new cameras.
        if not force and len(pending_batch) < SCAN_BATCH_SIZE and now - last_batch_flush < 2.0:
            return
        batch = pending_batch[:]
        on_batch(batch)
        del pending_batch[:len(batch)]
        last_batch_flush = time.monotonic()

    def add(cams: list[dict]) -> None:
        for cam in cams:
            cid = cam.get('id', '')
            if cid and cid not in seen_ids:
                seen_ids.add(cid)
                pending_batch.append(cam)
                if collect:
                    cameras.append(cam)
                if on_batch and len(pending_batch) >= SCAN_BATCH_SIZE:
                    flush_batch(force=True)

    # Index supported files in one pruned filesystem walk. Repeated root.rglob()
    # calls for each extension made large workspace scans unnecessarily slow.
    source_files = {ext: [] for ext in ('.txt', '.m3u', '.m3u8', '.json', '.csv', '.xml')}
    walked_dirs = 0
    last_index_log = time.monotonic()
    for current_dir, dirnames, filenames in os.walk(str(root), topdown=True, followlinks=False):
        walked_dirs += 1
        now = time.monotonic()
        if now - last_index_log >= 5.0:
            indexed_count = sum(len(paths) for paths in source_files.values())
            log.info('[Scanner] Indexing still active: directories=%d supported_files=%d current_dir=%s',
                     walked_dirs, indexed_count, current_dir)
            last_index_log = now
        dirnames[:] = sorted(d for d in dirnames if not d.startswith('.') and d not in _SKIP_DIRS)
        for filename in filenames:
            path = Path(current_dir) / filename
            ext = path.suffix.lower()
            if ext in source_files and not _should_skip(path, root):
                source_files[ext].append(path)
    for paths in source_files.values():
        paths.sort()
    log.info('[Scanner] Indexed supported files once: %s',
             ', '.join(f'{ext}={len(paths)}' for ext, paths in source_files.items()))

    def changed(path: Path) -> bool:
        try:
            st = path.stat()
        except OSError:
            return False
        row = _db_conn().execute(
            'SELECT size_bytes,mtime_ns FROM discovery_sources WHERE path=?',
            (str(path),)).fetchone()
        return force or not row or row['size_bytes'] != st.st_size or row['mtime_ns'] != st.st_mtime_ns

    def record(path: Path, count: int, status: str = 'ok') -> None:
        try:
            st = path.stat()
            _db_conn().execute('''
                INSERT INTO discovery_sources
                    (path,size_bytes,mtime_ns,last_scanned,scan_status,camera_count)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET
                    size_bytes=excluded.size_bytes,mtime_ns=excluded.mtime_ns,
                    last_scanned=excluded.last_scanned,scan_status=excluded.scan_status,
                    camera_count=excluded.camera_count
            ''', (str(path), st.st_size, st.st_mtime_ns,
                  datetime.now().strftime('%Y-%m-%dT%H:%M:%S'), status, count))
            pending_records.append((str(path),st.st_size,st.st_mtime_ns,datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),status,count))
        except (OSError, sqlite3.Error):
            pass

    for txt in source_files['.txt']:
        if _should_skip(txt, root):
            continue
        current_sources.add(str(txt))
        if not changed(txt):
            flush_batch()
            continue
        name_low = txt.name.lower()
        parsed = []
        try:
            content = txt.read_text(encoding='utf-8', errors='ignore')
            if 'validcamera' in name_low or 'valid_camera' in name_low:
                for block in re.split(r'={10,}', content):
                    cam = _parse_valid_camera_block(block, str(txt))
                    if cam:
                        parsed.append(cam)
            else:
                # A TXT file may contain BOTH RTSP links and inventory rows.
                # Previously, the presence of one rtsp:// line sent the whole
                # file through _parse_rtsp_file(), silently dropping rows such as
                # "192.0.2.10,admin,password". Parse both formats and merge by ID.
                if 'rtsp://' in content.lower() or 'rtsps://' in content.lower():
                    parsed.extend(_parse_rtsp_file(str(txt)))

                # Also accept plain inventory-style TXT rows in mixed files:
                #   192.0.2.10,admin,password
                #   192.0.2.11:8080 admin password hikvision
                # RTSP URLs and playlist directives are deliberately excluded.
                for raw in content.splitlines():
                    line = raw.strip()
                    if not line or line.startswith('#') or line.lower().startswith(('rtsp://', 'rtsps://')):
                        continue
                    parts = [p.strip() for p in re.split(r'[,;\\t ]+', line) if p.strip()]
                    if len(parts) < 3:
                        continue
                    target = parts[0]
                    if not re.match(r'^(?:[A-Za-z0-9._-]+)(?::\\d{1,5})?$', target):
                        continue
                    host, port = (target.rsplit(':', 1) if
                                  ':' in target and target.rsplit(':', 1)[1].isdigit()
                                  else (target, '80'))
                    if host.lower() in {'ip', 'host', 'address', 'ip_address', 'ip-address'}:
                        continue
                    # A bare word is more likely a group/title line than a host.
                    # Accept a dotted host/IP, or an explicit host:port token.
                    if '.' not in host and ':' not in target:
                        continue
                    # Avoid treating arbitrary labels as credentials; inventory
                    # rows must have a plausible host and non-empty username.
                    if not host or not parts[1] or parts[1].lower() in {'username', 'user'}:
                        continue
                    user, password = parts[1], parts[2]
                    vendor = parts[3] if len(parts) > 3 else 'auto'
                    try:
                        pnum = int(port)
                    except ValueError:
                        continue
                    if not (1 <= pnum <= 65535):
                        continue
                    cid = _cam_id(host, pnum, user)
                    parsed.append({
                        'id': cid, 'ip': host, 'port': pnum,
                        'camera_type': vendor or 'Generic Camera',
                        'label': f'{vendor or "Camera"} — {host}',
                        'rtsp_urls': [], 'web_url': f'http://{host}:{pnum}',
                        'source_file': str(txt), 'country': '', 'region': '', 'city': '',
                        'username': user, 'password': password,
                        'group_name': 'TXT Import',
                    })
                # Keep the first record per ID, merging distinct stream URLs so
                # a mixed file cannot create duplicate camera IDs in one batch.
                merged_by_id = {}
                for cam in parsed:
                    cid = str(cam.get('id') or '')
                    if not cid:
                        continue
                    if cid not in merged_by_id:
                        merged_by_id[cid] = cam
                    else:
                        existing = merged_by_id[cid]
                        urls = list(existing.get('rtsp_urls') or [])
                        for url in cam.get('rtsp_urls') or []:
                            if url not in urls:
                                urls.append(url)
                        existing['rtsp_urls'] = urls
                parsed = list(merged_by_id.values())
        except Exception as e:
            log.debug('Error reading %s: %s', txt, e)
            record(txt, 0, 'error')
            flush_batch()
            continue
        add(parsed)
        source_members[str(txt)] = {c['id'] for c in parsed if c.get('id')}
        if parsed:
            log.info('[Scanner] TXT parsed source=%s records=%d unique_ids=%d rtsp_urls=%d',
                     txt.name, len(parsed), len(source_members[str(txt)]),
                     sum(len(c.get('rtsp_urls') or []) for c in parsed))
        record(txt, len(parsed))
        flush_batch()

    for m3u in sorted(source_files['.m3u'] + source_files['.m3u8']):
        if _should_skip(m3u, root):
            continue
        current_sources.add(str(m3u))
        if changed(m3u):
            parsed = _parse_m3u_file(str(m3u))
            add(parsed)
            source_members[str(m3u)] = {c['id'] for c in parsed if c.get('id')}
            record(m3u, len(parsed))
        flush_batch()

    for jf in source_files['.json']:
        if _should_skip(jf, root) or jf.name in {'package.json', 'package-lock.json'}:
            continue
        current_sources.add(str(jf))
        if changed(jf):
            parsed = []
            try:
                if 'rtsp://' in jf.read_text(encoding='utf-8', errors='ignore').lower():
                    parsed = _parse_json_file(str(jf))
            except Exception:
                record(jf, 0, 'error')
                flush_batch()
                continue
            add(parsed)
            source_members[str(jf)] = {c['id'] for c in parsed if c.get('id')}
            record(jf, len(parsed))
        flush_batch()

    for cf in source_files['.csv']:
        if _should_skip(cf, root):
            continue
        current_sources.add(str(cf))
        if changed(cf):
            parsed = _parse_csv_file(str(cf))
            add(parsed)
            source_members[str(cf)] = {c['id'] for c in parsed if c.get('id')}
            record(cf, len(parsed))
        flush_batch()

    for xf in source_files['.xml']:
        if _should_skip(xf, root):
            continue
        current_sources.add(str(xf))
        if changed(xf):
            parsed = _parse_xml_file(str(xf))
            add(parsed)
            source_members[str(xf)] = {c['id'] for c in parsed if c.get('id')}
            record(xf, len(parsed))
        flush_batch()

    flush_batch(force=True)
    if pending_records:
        def _flush_records():
            c=_db_conn(); c.executemany("INSERT INTO discovery_sources(path,size_bytes,mtime_ns,last_scanned,scan_status,camera_count) VALUES(?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET size_bytes=excluded.size_bytes,mtime_ns=excluded.mtime_ns,last_scanned=excluded.last_scanned,scan_status=excluded.scan_status,camera_count=excluded.camera_count",pending_records); c.commit()
        _db_retry_write(_flush_records,'discovery source metadata')
    # Keep source membership in memory for the upsert/cleanup phase. Missing
    # source files are reconciled there too, after all current records exist.
    global _last_discovery_members
    _last_discovery_members = source_members
    missing = _db_conn().execute(
        "SELECT path FROM discovery_sources WHERE path LIKE ?",
        (str(root) + '%',)).fetchall()
    for row in missing:
        if row['path'] not in current_sources and os.path.commonpath(
                [os.path.abspath(row['path']), str(root)]) == str(root):
            source_members.setdefault(row['path'], set())
    _db_conn().commit()
    # Rehydrate unchanged source files from SQLite. The previous implementation
    # correctly skipped unchanged files for speed, but then returned only records
    # parsed during this invocation. After a restart that produced the misleading
    # "Discovery: found 0 cameras" even though the DB already contained them.
    # Keep the fast stat/mtime skip while restoring the current inventory from DB.
    if current_sources:
        source_list = sorted(current_sources)
        for start in range(0, len(source_list), 400):
            chunk = source_list[start:start + 400]
            ph = ','.join('?' * len(chunk))
            try:
                rows = _db_conn().execute(
                    f'SELECT * FROM cameras WHERE deleted=0 AND source_file IN ({ph})',
                    chunk).fetchall()
            except sqlite3.Error:
                rows = []
            for row in rows:
                cam = _db_row(row)
                cid = str(cam.get('id') or '')
                source = str(cam.get('source_file') or '')
                if not cid or cid in seen_ids:
                    if cid and source:
                        source_members.setdefault(source, set()).add(cid)
                    continue
                seen_ids.add(cid)
                source_members.setdefault(source, set()).add(cid)
                if on_batch:
                    pending_batch.append(cam)
                    if len(pending_batch) >= SCAN_BATCH_SIZE:
                        flush_batch(force=True)
                    else:
                        flush_batch()
                if collect:
                    cameras.append(cam)
    flush_batch(force=True)
    _last_discovery_members = source_members
    log.info('Discovery: found %d cameras in %s%s', len(cameras), search_dir,
             ' (forced)' if force else '')
    return cameras


# ══════════════════════════════════════════════════════════════════════════════
# VALIDATOR
# ══════════════════════════════════════════════════════════════════════════════

_val_lock     = threading.Lock()
_val_progress = {
    'total': 0, 'done': 0, 'online': 0, 'offline': 0,
    'running': False, 'started': 0.0, 'elapsed': 0.0, 'scope': '',
    'speed_per_sec': 0.0, 'eta_seconds': None,
}
# Prevent stacking concurrent full validations
_full_val_active = threading.Event()
# v157: only one validation cycle may execute at a time.
# This is separate from the persisted queue lease: it prevents two in-process
# validation runners from consuming CPU/DB resources concurrently.
_VALIDATION_RUN_LOCK = threading.Lock()
# v64 validation controls: pause affects the persisted validation queue worker
# only; an already-running camera probe is allowed to finish safely.
_validation_queue_paused = threading.Event()
_validation_queue_restart_requested = threading.Event()

# v65 Validation Engine 2.0 policy. These settings are environment-overridable
# so large installations can tune retry pressure without editing the source.
VALIDATION_MAX_ATTEMPTS = max(1, min(100, int(os.getenv('CAMERA_VALIDATION_MAX_ATTEMPTS', '12'))))
VALIDATION_RETRY_BASE_MINUTES = max(1, min(1440, int(os.getenv('CAMERA_VALIDATION_RETRY_BASE_MINUTES', '6'))))

# v66 security policy
MAX_SESSIONS_PER_USER = max(1, min(50, int(os.getenv('CAMERA_MAX_SESSIONS_PER_USER', '5'))))
PASSWORD_MAX_AGE_DAYS = max(0, min(3650, int(os.getenv('CAMERA_PASSWORD_MAX_AGE_DAYS', '0'))))
VALIDATION_RETRY_MAX_MINUTES = max(VALIDATION_RETRY_BASE_MINUTES, min(10080, int(os.getenv('CAMERA_VALIDATION_RETRY_MAX_MINUTES', '1440'))))
VALIDATION_ONLINE='online'; VALIDATION_OFFLINE='offline'; VALIDATION_CHECKING='checking'
_VALIDATION_PROBE_SEMAPHORE=threading.BoundedSemaphore(VALIDATION_PROBE_CONCURRENCY)
VALIDATION_MAX_CANDIDATES=max(1,min(24,int(os.getenv('CAMERA_VALIDATION_MAX_CANDIDATES','6'))))
VALIDATION_FFPROBE_TIMEOUT=max(2.0,min(12.0,float(os.getenv('CAMERA_VALIDATION_FFPROBE_TIMEOUT','5'))))

def _validation_priority(cam: dict) -> int:
    """Lower number means earlier validation. Favor new/favorite/unknown cameras."""
    if cam.get('favorite'):
        return 1
    status = str(cam.get('status') or '').lower()
    if status in {'', 'unknown', 'new', 'checking'}:
        return 5
    if status == 'online':
        return 20
    if status == 'offline':
        return 80
    return 40

def _validation_retry_delay_minutes(attempts: int) -> int:
    attempts = max(1, int(attempts))
    return min(VALIDATION_RETRY_MAX_MINUTES,
               VALIDATION_RETRY_BASE_MINUTES * (2 ** min(attempts - 1, 10)))


def val_get_progress() -> dict:
    with _val_lock:
        return dict(_val_progress)


def _tcp_probe(host: str, port: int, timeout: float = 3.0) -> tuple[bool, int]:
    try:
        t0 = time.monotonic()
        s  = socket.create_connection((host, port), timeout=timeout)
        ms = int((time.monotonic() - t0) * 1000)
        s.close()
        return True, ms
    except Exception:
        return False, 0


def _rtsp_host_port(rtsp_url: str) -> tuple[str, int] | tuple[None, None]:
    try:
        parts = urlsplit(rtsp_url)
        if parts.scheme.lower() not in {'rtsp','rtsps'} or not parts.hostname:
            return None, None
        return parts.hostname, parts.port or 554
    except (ValueError, TypeError):
        return None, None


def _classify_ffprobe_error(stderr: str, transport: str) -> str:
    """Convert FFprobe output into an actionable, credential-free reason."""
    clean = re.sub(r'rtsp://[^\s]+', '[RTSP URL]', ' '.join(
        (stderr or '').split()), flags=re.IGNORECASE)
    low = clean.lower()
    if any(x in low for x in ('401 unauthorized', '403 forbidden',
                              'unauthorized', 'authentication failed',
                              'invalid data found')):
        category = 'authentication-or-invalid-stream'
    elif any(x in low for x in ('timed out', 'timeout', 'operation timed out')):
        category = 'timeout'
    elif any(x in low for x in ('connection refused', 'connection reset',
                                'no route to host', 'network is unreachable')):
        category = 'transport-unreachable'
    elif any(x in low for x in ('could not find codec', 'decoder',
                                'unsupported codec')):
        category = 'unsupported-codec'
    else:
        category = 'ffprobe-failed'
    detail = clean[-120:] if clean else ''
    return f'{transport}:{category}' + (f' ({detail})' if detail else '')


def _ffmpeg_fallback_meta(rtsp_url: str, timeout: float = 8.0,
                          transport: str = 'tcp',
                          failure_reasons: Optional[list[str]] = None) -> Optional[dict]:
    """Use the legacy FFmpeg frame-probe path when ffprobe cannot verify a stream.

    Older dashboard builds used ffmpeg directly. Some hosted environments have
    ffmpeg but no usable ffprobe binary/build, so keep that working path as a
    compatibility fallback instead of marking every stream offline.
    """
    cmd = ['ffmpeg', '-hide_banner', '-nostdin', '-loglevel', 'info',
           '-rtsp_transport', transport, '-timeout', '8000000',
           '-i', rtsp_url, '-t', '2', '-frames:v', '1', '-f', 'null', '-']
    try:
        with _VALIDATION_PROBE_SEMAPHORE:
            r = subprocess.run(cmd, capture_output=True, timeout=max(2.0, timeout))
        output = (r.stderr or b'').decode(errors='ignore')
        if r.returncode != 0:
            if failure_reasons is not None:
                failure_reasons.append(_classify_ffprobe_error(output, transport)
                                       .replace('ffprobe', 'ffmpeg'))
            return None
        # A successful one-frame decode is enough to call the stream online.
        codec, resolution, fps = '', '', 0.0
        m = re.search(r'Video:\s*([^,\s]+).*?(\d{2,5}x\d{2,5})', output, re.I)
        if m:
            codec, resolution = m.group(1).upper(), m.group(2)
        fm = re.search(r'(\d+(?:\.\d+)?)\s*fps', output, re.I)
        if fm:
            try:
                fps = float(fm.group(1))
            except ValueError:
                fps = 0.0
        return {'codec': codec, 'resolution': resolution, 'fps': fps}
    except subprocess.TimeoutExpired:
        if failure_reasons is not None:
            failure_reasons.append(f'{transport}:ffmpeg-timeout')
        return None
    except FileNotFoundError:
        if failure_reasons is not None:
            failure_reasons.append(f'{transport}:ffmpeg-not-installed')
        return None
    except Exception as exc:
        if failure_reasons is not None:
            failure_reasons.append(f'{transport}:ffmpeg-probe-error ({type(exc).__name__})')
        return None


def _ffprobe_meta(rtsp_url: str, timeout: float = 12.0,
                  transport: str = 'tcp',
                  failure_reasons: Optional[list[str]] = None) -> Optional[dict]:
    """Probe video metadata with ffprobe, falling back to the legacy ffmpeg path."""
    cmd = ['ffprobe', '-v', 'error', '-rtsp_transport', transport,
           '-timeout', '8000000',
           '-print_format', 'json', '-show_streams', rtsp_url]
    ffprobe_reason = ''
    try:
        with _VALIDATION_PROBE_SEMAPHORE:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if r.returncode == 0:
            data = json.loads(r.stdout.decode(errors='ignore'))
            video = next((s for s in data.get('streams', [])
                          if s.get('codec_type') == 'video'), None)
            if video:
                w, h = video.get('width', 0), video.get('height', 0)
                fps = 0.0
                for key in ('avg_frame_rate', 'r_frame_rate'):
                    afr = video.get(key, '')
                    if '/' in afr:
                        try:
                            n, d = afr.split('/')
                            if float(d):
                                fps = round(float(n) / float(d), 1)
                                if fps > 0:
                                    break
                        except Exception:
                            pass
                return {'codec': video.get('codec_name', '').upper(),
                        'resolution': f'{w}x{h}' if w and h else '',
                        'fps': fps}
            ffprobe_reason = f'{transport}:no-video-stream'
        else:
            ffprobe_reason = _classify_ffprobe_error(
                r.stderr.decode(errors='ignore'), transport)
    except subprocess.TimeoutExpired:
        ffprobe_reason = f'{transport}:ffprobe-timeout'
    except FileNotFoundError:
        ffprobe_reason = f'{transport}:ffprobe-not-installed'
    except (ValueError, json.JSONDecodeError):
        ffprobe_reason = f'{transport}:invalid-ffprobe-output'
    except Exception as exc:
        ffprobe_reason = f'{transport}:ffprobe-probe-error ({type(exc).__name__})'

    # Compatibility: the previous working version verified a decoded frame
    # with ffmpeg directly. Try that path before concluding the camera is offline.
    fallback = _ffmpeg_fallback_meta(rtsp_url, timeout=min(timeout, 8.0),
                                      transport=transport,
                                      failure_reasons=failure_reasons)
    if fallback:
        return fallback
    if failure_reasons is not None and ffprobe_reason:
        failure_reasons.append(ffprobe_reason)
    return None


def _score_rtsp_candidate(cam: dict, rtsp_url: str, position: int) -> float:
    """Rank known RTSP candidates without probing additional network targets."""
    low = (rtsp_url or '').lower()
    brand = _detect_brand(cam.get('camera_type', ''))
    score = max(0.0, 12.0 - float(position))
    if brand == 'hikvision':
        if '/streaming/channels/' in low:
            score += 35
        elif '/h264/ch' in low:
            score += 25
    elif brand == 'dahua':
        if 'cam/realmonitor' in low:
            score += 35
        if 'subtype=0' in low:
            score += 10
    elif brand == 'foscam' and '/videomain' in low:
        score += 30
    if any(path in low for path in ('/stream1', '/live/ch0', 'main',
                                    'channels/101', 'subtype=0')):
        score += 8
    if any(path in low for path in ('subtype=1', 'channels/102')):
        score += 2
    return round(score, 2)


def _validate_one(cam: dict) -> str:
    cam_id    = cam['id']
    rtsp_urls = cam.get('rtsp_urls') or []
    if not rtsp_urls:
        db_update_validation(cam_id, 'offline',
                             failure_reason='no-stored-candidate')
        return 'offline'
    ranked = sorted(
        ((-_score_rtsp_candidate(cam, url, pos), pos, url)
         for pos, url in enumerate(rtsp_urls)
         if isinstance(url, str) and url.lower().startswith(('rtsp://','rtsps://'))),
        key=lambda item: (item[0], item[1]))[:VALIDATION_MAX_CANDIDATES]
    reasons: list[str] = []
    last_url, last_transport = '', ''
    for candidate_index, (neg_score, _, rtsp_url) in enumerate(ranked):
        score = -neg_score
        host, port = _rtsp_host_port(rtsp_url)
        if not host:
            reasons.append(f'candidate:{score}:invalid-rtsp-url')
            continue
        reachable, ms = _tcp_probe(host, port, timeout=min(4.0, 1.5 + 0.5 * candidate_index))
        if not reachable:
            reasons.append(f'candidate:{score}:tcp-unreachable')
            continue
        for transport in ('tcp', 'udp'):
            last_url, last_transport = rtsp_url, transport
            probe_reasons: list[str] = []
            meta = _ffprobe_meta(rtsp_url, timeout=min(VALIDATION_FFPROBE_TIMEOUT, 3.0 + candidate_index * 0.5), transport=transport,
                                  failure_reasons=probe_reasons)
            if meta:
                db_update_validation(cam_id, 'online', codec=meta['codec'],
                                      resolution=meta['resolution'], fps=meta['fps'],
                                      latency_ms=ms, candidate_url=rtsp_url,
                                      transport=transport, candidate_score=score)
                db_promote_rtsp_url(cam_id, rtsp_url)
                _stream_log('[Validation] ONLINE camera=%s transport=%s latency_ms=%s codec=%s resolution=%s fps=%s url=%s',
                            cam_id, transport, ms, meta.get('codec', ''),
                            meta.get('resolution', ''), meta.get('fps', 0), rtsp_url)
                return 'online'
            reasons.extend(f'candidate:{score}:{reason}'
                           for reason in probe_reasons)
    reason = '; '.join(reasons)[-240:] or 'no-decoded-video'
    db_update_validation(cam_id, 'offline', failure_reason=reason,
                         candidate_url=last_url, transport=last_transport)
    _stream_log('[Validation] OFFLINE camera=%s candidates=%s reason=%s last_transport=%s url=%s',
                cam_id, len(ranked), reason, last_transport or 'none', last_url or 'none')
    return 'offline'


def _validate_one_and_close(cam: dict) -> str:
    try: return _validate_one(cam)
    finally: _db_close_thread_connection()


def run_validation_cycle(cameras: list[dict], max_workers: int | None = None,
                          scope: str = 'all', _lock_held: bool = False) -> None:
    global _VALIDATION_ACTIVE_COUNT
    if max_workers is None or max_workers <= 0: max_workers=_validation_worker_config()[0]
    # Maintenance pauses new cycles; an already-running cycle is allowed to
    # finish before VACUUM starts.
    while _DB_MAINTENANCE_ACTIVE.is_set():
        time.sleep(0.25)
    acquired = False
    if not _lock_held:
        # Never allow two in-process validation runners to overlap.  Waiting is
        # intentional here because callers such as scheduled/category checks
        # should finish rather than silently discard their requested cycle.
        _VALIDATION_RUN_LOCK.acquire()
        acquired = True
    with _VALIDATION_ACTIVE_LOCK:
        _VALIDATION_ACTIVE_COUNT += 1
    try:
        _run_validation_cycle_impl(cameras, max_workers=max_workers, scope=scope)
    finally:
        with _VALIDATION_ACTIVE_LOCK:
            _VALIDATION_ACTIVE_COUNT = max(0, _VALIDATION_ACTIVE_COUNT - 1)
        if acquired:
            _VALIDATION_RUN_LOCK.release()


def _run_validation_cycle_impl(cameras: list[dict], max_workers: int | None = None,
                               scope: str = 'all') -> None:
    _touch_worker('validator', 'running', scope)
    if max_workers is None or max_workers <= 0: max_workers=_validation_worker_config()[0]
    total = len(cameras)
    with _val_lock:
        _val_progress.update({
            'total': total, 'done': 0, 'online': 0, 'offline': 0, 'errors': 0,
            'running': True, 'started': time.time(), 'elapsed': 0.0, 'scope': scope,
            'speed_per_sec': 0.0, 'eta_seconds': None,
        })
    log.info('[Validator] Starting — %d cameras, scope=%s, workers=%d',
             total, scope, max_workers)
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=max_workers,
                             thread_name_prefix='cam-val') as pool:
        futures = {pool.submit(_validate_one_and_close, cam): cam for cam in cameras}
        for fut in as_completed(futures):
            status = 'error'
            try:
                status = fut.result()
            except Exception as e:
                cam = futures.get(fut, {})
                cam_id = cam.get('id', '?') if hasattr(cam, 'get') else '?'
                log.warning('[Validator] Camera validation raised an exception (camera_id=%s): %s', cam_id, e, exc_info=True)
            with _val_lock:
                _val_progress['done']    += 1
                if status == 'error':
                    _val_progress['errors'] = int(_val_progress.get('errors', 0) or 0) + 1
                _val_progress['elapsed']  = round(time.monotonic() - t0, 1)
                elapsed_now = max(0.001, time.monotonic() - t0)
                _val_progress['speed_per_sec'] = round(_val_progress['done'] / elapsed_now, 2)
                remaining = max(0, total - _val_progress['done'])
                _val_progress['eta_seconds'] = round(remaining / _val_progress['speed_per_sec'], 1) if _val_progress['speed_per_sec'] > 0 else None
                if status == 'online':
                    _val_progress['online'] += 1
                elif status == 'offline':
                    _val_progress['offline'] += 1
    elapsed = time.monotonic() - t0
    with _val_lock:
        _val_progress['running'] = False
        _val_progress['elapsed'] = round(elapsed, 1)
        _val_progress['speed_per_sec'] = round(total / elapsed, 2) if elapsed > 0 else 0.0
        _val_progress['eta_seconds'] = 0.0
    _touch_worker('validator', 'idle', scope)
    # Report counts from this validation cycle. Database-wide totals can be
    # much larger than a claimed batch (e.g. 400 queued cameras), which made
    # the previous completion log look internally inconsistent.
    with _val_lock:
        cycle_online = int(_val_progress.get('online', 0) or 0)
        cycle_offline = int(_val_progress.get('offline', 0) or 0)
        cycle_done = int(_val_progress.get('done', 0) or 0)
    with _val_lock:
        cycle_errors = int(_val_progress.get('errors', 0) or 0)
    log.info('[Validator] Done in %.1fs — batch_done=%d/%d online=%d offline=%d errors=%d',
             elapsed, cycle_done, total, cycle_online, cycle_offline, cycle_errors)


def start_background_validation(cameras: list[dict], delay: float = 2.0,
                                  max_workers: int = 30,
                                  scope: str = 'all') -> threading.Thread:
    def _run():
        time.sleep(delay)
        run_validation_cycle(cameras, max_workers=max_workers, scope=scope)
        _full_val_active.clear()
    t = threading.Thread(target=_run, daemon=True, name=f'cam-val-{scope[:20]}')
    t.start()
    return t


def start_validation_queue_worker(max_workers: int | None = None,
                                  delay: float = 5.0) -> threading.Thread:
    """Continuously drain persisted validation work with leases, retries and adaptive resources.

    v64 adds an explicit pause/restart control. Pause never kills an active
    FFmpeg/validation operation; it prevents the queue worker from claiming
    another batch until resumed. Restart is graceful and waits for the current
    batch to finish before replacing the queue worker.
    """
    if max_workers is None or max_workers <= 0:
        initial_workers, initial_batch_limit = _validation_worker_config()
    else:
        initial_workers = max_workers
        initial_workers = min(64, max(1, int(max_workers)))
        initial_batch_limit = (VALIDATION_BATCH_OVERRIDE or min(2000, max(120, initial_workers * 10)))
    worker_id = f'validation-queue-{os.getpid()}-{uuid.uuid4().hex[:8]}'

    def _run():
        current_workers, current_batch_limit = initial_workers, initial_batch_limit
        restart_after_exit = False
        _touch_worker('validation-queue', 'starting', worker_id)
        if _validation_queue_restart_requested.is_set():
            # A restart request made before this replacement worker started is
            # consumed here so the replacement does not immediately stop again.
            _validation_queue_restart_requested.clear()
        if delay > 0:
            _shutdown_requested.wait(delay)
        while not _shutdown_requested.is_set():
            claimed = []
            slot_held = False
            try:
                if _validation_queue_restart_requested.is_set():
                    restart_after_exit = True
                    break
                if _validation_queue_paused.is_set():
                    _touch_worker('validation-queue', 'waiting', 'validation paused by operator')
                    _validation_queue_restart_requested.wait(5.0)
                    continue
                _touch_worker('validation-queue', 'running', worker_id)
                if _full_val_active.is_set() or val_get_progress()['running']:
                    _touch_worker('validation-queue', 'waiting', 'another validation scope is active')
                    _validation_queue_restart_requested.wait(15.0)
                    continue
                if _WORKER_STATE.get('mode', 'auto') == 'auto':
                    current_workers, current_batch_limit = _validation_worker_config()
                # v157: reserve the single in-process validation slot before
                # claiming queue rows. Otherwise a second runner could lease
                # another batch while the first batch was still validating.
                if not _VALIDATION_RUN_LOCK.acquire(blocking=False):
                    _touch_worker('validation-queue', 'waiting', 'another validation cycle is running')
                    _validation_queue_restart_requested.wait(5.0)
                    continue
                slot_held = True
                try:
                    # Keep batches short enough that freshly discovered/high-priority
                    # cameras are not stuck behind hundreds of old queued records.
                    fair_claim_limit = min(current_batch_limit, max(25, current_workers * 4))
                    claimed = _claim_validation_batch(fair_claim_limit, worker_id)
                    if not claimed:
                        _touch_worker('validation-queue', 'idle', 'queue empty; polling for newly discovered cameras')
                        _validation_queue_restart_requested.wait(3.0)
                        continue
                    ids = [cam['id'] for cam in claimed]
                    db_set_status_bulk(ids, 'checking')
                    if VALIDATION_BATCH_OVERRIDE:
                        log.info('[Validator] Queue lease batch — %d cameras claimed, workers=%d (configured ceiling=%d, responsive claim=%d)',
                                 len(claimed), current_workers, current_batch_limit, fair_claim_limit)
                    else:
                        log.info('[Validator] Queue lease batch — %d cameras, workers=%d (auto ceiling=%d, responsive claim=%d)',
                                 len(claimed), current_workers, current_batch_limit, fair_claim_limit)
                    _record_recovery_event('validation_batch', worker_id, 'claimed', str(len(claimed)))
                    run_validation_cycle(claimed, max_workers=current_workers, scope='queue-resume', _lock_held=True)
                    _release_validation_claims(ids, worker_id)
                    if _validation_queue_restart_requested.is_set():
                        restart_after_exit = True
                        break
                finally:
                    if slot_held:
                        _VALIDATION_RUN_LOCK.release()
                        slot_held = False
            except Exception as e:
                log.warning('[Validator] Queue worker error: %s', e)
                if claimed:
                    _release_validation_claims([c['id'] for c in claimed], worker_id)
                _record_recovery_event('validation_batch', worker_id, 'failed',
                                       f'{type(e).__name__}: {e}')
                _touch_worker('validation-queue', 'error', type(e).__name__)
                _validation_queue_restart_requested.wait(30.0)
        _touch_worker('validation-queue', 'stopped', 'restart requested' if restart_after_exit else '')
        if restart_after_exit and not _shutdown_requested.is_set():
            # Clear before spawning so the replacement worker starts normally.
            _validation_queue_restart_requested.clear()
            time.sleep(0.25)
            start_validation_queue_worker(max_workers=None, delay=0.5)

    t = threading.Thread(target=_run, daemon=True, name='cam-val-queue')
    t.start()
    return t


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════

_TG_DEST_CACHE={'key':None,'items':[]}
_TG_DEST_CACHE_LOCK=threading.Lock()


def _telegram_cipher() -> Optional['Fernet']:
    """Return the encryption cipher used for Telegram credentials.

    Priority:
      1. CAMERA_ENCRYPTION_KEY (recommended production secret)
      2. SESSION_SECRET (already required for persistent Flask sessions)
      3. A local 0600 key file generated once.

    The encryption key is deliberately NOT stored in SQLite. Storing both the
    ciphertext and its decryption key in the same database would provide little
    protection if the database were copied.
    """
    if Fernet is None:
        return None
    raw = os.getenv('CAMERA_ENCRYPTION_KEY', '').strip()
    if not raw:
        raw = os.getenv('SESSION_SECRET', '').strip()
    if raw:
        key = base64.urlsafe_b64encode(hashlib.sha256(raw.encode('utf-8')).digest())
        return Fernet(key)
    try:
        key_path = TG_KEY_FILE
        if not os.path.exists(key_path):
            key = Fernet.generate_key()
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            fd = os.open(key_path, flags, 0o600)
            try:
                os.write(fd, key)
            finally:
                os.close(fd)
        with open(key_path, 'rb') as fh:
            key = fh.read().strip()
        if len(key) != 44:
            raise ValueError('invalid local Telegram encryption key')
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
        return Fernet(key)
    except Exception as exc:
        log.error('[Telegram Security] Could not initialize encryption key: %s', exc)
        return None


def _telegram_db_ciphertext() -> str:
    try:
        row = _db_conn().execute(
            'SELECT value FROM app_settings WHERE key=?', (TG_DB_KEY,)).fetchone()
        return str(row['value'] if row else '')
    except Exception:
        return ''


def _telegram_decrypt_config(include_disabled: bool = False) -> list[dict]:
    """Decrypt all Telegram destinations without exposing credentials to the UI.

    Normal delivery returns enabled destinations only.  Administrative metadata
    views may request disabled entries as well so the dashboard can show and
    manage the complete destination list without returning tokens/chat IDs.
    """
    ciphertext = _telegram_db_ciphertext()
    if not ciphertext:
        return []
    cipher = _telegram_cipher()
    if cipher is None:
        log.error('[Telegram Security] cryptography package is unavailable')
        return []
    try:
        plain = cipher.decrypt(ciphertext.encode('ascii'), ttl=None)
        cfg = json.loads(plain.decode('utf-8'))
        items = cfg.get('destinations') if isinstance(cfg, dict) else None
        if not isinstance(items, list):
            return []
        result = []
        for d in items:
            if not isinstance(d, dict) or not d.get('bot_token') or not d.get('chat_id'):
                continue
            if not include_disabled and not d.get('enabled', True):
                continue
            result.append({str(k): v for k, v in d.items()})
        return result
    except InvalidToken:
        log.error('[Telegram Security] Telegram credentials could not be decrypted. Check CAMERA_ENCRYPTION_KEY/SESSION_SECRET.')
    except Exception as exc:
        log.error('[Telegram Security] Invalid encrypted Telegram configuration: %s', exc)
    return []


def _save_telegram_config(destinations: list[dict]) -> tuple[bool, str]:
    cipher = _telegram_cipher()
    if cipher is None:
        return False, "Secure encryption is unavailable because the Python package 'cryptography' is not installed. Add cryptography to requirements.txt and redeploy; setting CAMERA_ENCRYPTION_KEY alone cannot replace the package."
    clean=[]
    for d in destinations:
        token=str(d.get('bot_token') or '').strip()
        chat_id=str(d.get('chat_id') or '').strip()
        if not token or not chat_id:
            continue
        clean.append({
            'name': str(d.get('name') or 'Telegram').strip()[:100],
            'bot_token': token,
            'chat_id': chat_id,
            'enabled': bool(d.get('enabled', True)),
        })
    payload=json.dumps({'version':1,'destinations':clean}, ensure_ascii=False, separators=(',',':')).encode('utf-8')
    encrypted=cipher.encrypt(payload).decode('ascii')
    try:
        _db_retry_write(lambda: _db_conn().execute(
            "INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=datetime('now')",
            (TG_DB_KEY, encrypted)).connection.commit(), 'telegram secure config')
        with _TG_DEST_CACHE_LOCK:
            _TG_DEST_CACHE.update(key=('db', encrypted), items=list(clean))
        _advanced_audit('telegram_config_saved', 'settings', TG_DB_KEY,
                         f'{len(clean)} encrypted destination(s)', _current_user() if has_request_context() else 'system')
        return True, 'Telegram credentials saved encrypted in the database.'
    except Exception as exc:
        log.exception('[Telegram Security] Failed to save encrypted configuration')
        return False, f'Could not save encrypted Telegram configuration: {type(exc).__name__}'


def _delete_telegram_config() -> tuple[bool, str]:
    try:
        _db_retry_write(lambda: _db_conn().execute(
            'DELETE FROM app_settings WHERE key=?', (TG_DB_KEY,)).connection.commit(), 'telegram secure config delete')
        with _TG_DEST_CACHE_LOCK:
            _TG_DEST_CACHE.update(key=None, items=[])
        _advanced_audit('telegram_config_deleted', 'settings', TG_DB_KEY, 'Encrypted Telegram credentials removed', _current_user() if has_request_context() else 'system')
        return True, 'Encrypted Telegram configuration removed.'
    except Exception as exc:
        return False, f'Could not remove Telegram configuration: {type(exc).__name__}'


def _legacy_tg_file_destinations(include_disabled: bool = False) -> list[dict]:
    """Load the existing plaintext Telegram config for backward compatibility.

    Multiple destinations are preserved. The plaintext values remain server-side
    only; the dashboard receives names/status metadata, never bot tokens or chat IDs.
    """
    for path in [TG_CONFIG, os.path.join(SCRIPT_DIR, 'a.json')]:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding='utf-8') as fh:
                cfg = json.load(fh)
            if isinstance(cfg, dict) and isinstance(cfg.get('destinations'), list):
                result = []
                for d in cfg['destinations']:
                    if not isinstance(d, dict) or not d.get('bot_token') or not d.get('chat_id'):
                        continue
                    if not include_disabled and not d.get('enabled', True):
                        continue
                    result.append(d)
                return result
            if isinstance(cfg, dict) and cfg.get('bot_token') and cfg.get('chat_id'):
                return [cfg] if include_disabled or cfg.get('enabled', True) else []
        except Exception:
            continue
    return []


def _load_tg_destinations() -> list[dict]:
    """Load encrypted DB credentials first, then legacy plaintext file.

    Priority:
      1. Encrypted SQLite configuration
      2. Existing telegram_config.json / a.json compatibility configuration

    This means users do not have to migrate immediately, while new users can
    configure Telegram entirely from the application UI.
    """
    ciphertext = _telegram_db_ciphertext()
    cache_key = ('db', ciphertext)
    with _TG_DEST_CACHE_LOCK:
        if _TG_DEST_CACHE['key'] == cache_key:
            return list(_TG_DEST_CACHE['items'])
    items = _telegram_decrypt_config() if ciphertext else []
    if not items:
        items = _legacy_tg_file_destinations()
    effective_key = ('db', ciphertext, tuple(
        (d.get('name','Telegram'), d.get('bot_token',''), d.get('chat_id',''), bool(d.get('enabled', True)))
        for d in items
    ))
    with _TG_DEST_CACHE_LOCK:
        _TG_DEST_CACHE.update(key=effective_key, items=list(items))
    return list(items)


def _send_telegram(message: str, photo_path: str = '') -> tuple[bool, str]:
    if _requests is None:
        return False, 'requests library not installed'
    dests = _load_tg_destinations()
    if not dests:
        return False, 'Telegram not configured. Open Telegram Settings and save an encrypted configuration.'
    sent, errors = 0, []
    for dest in dests:
        token = dest['bot_token']
        cid   = str(dest['chat_id'])
        for attempt in range(2):
            try:
                if photo_path and os.path.exists(photo_path):
                    with open(photo_path, 'rb') as ph:
                        r = _requests.post(
                            f'https://api.telegram.org/bot{token}/sendPhoto',
                            data={'chat_id': cid, 'caption': message[:1024],
                                  'parse_mode': 'Markdown'},
                            files={'photo': ph}, timeout=5)
                else:
                    r = _requests.post(
                        f'https://api.telegram.org/bot{token}/sendMessage',
                        json={'chat_id': cid, 'text': message, 'parse_mode': 'Markdown'},
                        timeout=10)
                if r.ok:
                    sent += 1
                    break
                if attempt:
                    detail = ''
                    try:
                        body = r.json()
                        detail = str(body.get('description') or body.get('error_code') or '').strip()
                    except Exception:
                        detail = r.text[:180].strip()
                    errors.append(f"{dest.get('name','?')}: Telegram HTTP {r.status_code}" + (f" — {detail}" if detail else ''))
            except Exception as e:
                if attempt:
                    errors.append(f"{dest.get('name','?')}: {type(e).__name__}")
                else:
                    time.sleep(0.5)
    return (sent > 0), (f'Sent to {sent}' if sent else '; '.join(errors))


def _send_telegram_document(content, filename: str,
                            caption: str = '') -> tuple[bool, str]:
    """Send bytes or a file path without loading a large export into RAM."""
    if _requests is None:
        return False, 'requests library not installed'
    dests = _load_tg_destinations()
    if not dests:
        return False, 'Telegram not configured. Open Telegram Settings and save an encrypted configuration.'
    is_path = isinstance(content, (str, os.PathLike))
    size = os.path.getsize(content) if is_path else len(content)
    if size > 49 * 1024 * 1024:
        return False, 'Export is larger than Telegram document limit'
    sent, errors = 0, []
    for dest in dests:
        fh = None
        try:
            if is_path:
                fh = open(content, 'rb')
                payload = (filename, fh, 'application/gzip')
            else:
                payload = (filename, io.BytesIO(content), 'application/gzip')
            r = _requests.post(
                f"https://api.telegram.org/bot{dest['bot_token']}/sendDocument",
                data={'chat_id': str(dest['chat_id']), 'caption': caption[:1024]},
                files={'document': payload}, timeout=90)
            if r.ok:
                sent += 1
            else:
                errors.append(f"{dest.get('name','?')}: Telegram HTTP {r.status_code}")
        except Exception:
            errors.append(f"{dest.get('name','?')}: request failed")
        finally:
            if fh:
                try: fh.close()
                except Exception: pass
    return (sent > 0), (f'Sent to {sent}' if sent else '; '.join(errors))


def _build_camera_export_file(include_credentials: bool = False) -> str:
    """Write a gzipped JSONL export to disk using an explicit metadata allowlist."""
    if include_credentials and os.getenv('CAMERA_ALLOW_CREDENTIAL_EXPORT', '').strip().lower() not in {'1','true','yes'}:
        raise PermissionError('Credential-inclusive export is disabled; set CAMERA_ALLOW_CREDENTIAL_EXPORT=1 only for a trusted backup workflow.')
    allowed_fields = (
        'id', 'label', 'ip', 'port', 'camera_type', 'country', 'region', 'city',
        'web_url', 'source_file', 'group_name', 'device_id', 'channel_index',
        'channel_name', 'geo_source', 'geo_confidence', 'status', 'last_seen',
        'last_checked', 'created_at', 'updated_at', 'favorite', 'notes'
    )
    if include_credentials:
        allowed_fields += ('username', 'password', 'rtsp_urls')
    fd, path = tempfile.mkstemp(prefix='camera-export-', suffix='.jsonl.gz', dir=DATA_DIR)
    os.close(fd)
    try:
        with open(path, 'wb') as raw:
            with gzip.GzipFile(fileobj=raw, mode='wb', compresslevel=6) as gz:
                cursor = _db_conn().execute('SELECT * FROM cameras WHERE deleted=0 ORDER BY id')
                columns = set(cursor.description[i][0] for i in range(len(cursor.description)))
                selected = [field for field in allowed_fields if field in columns]
                for row in cursor:
                    item = {field: row[field] for field in selected}
                    if not include_credentials:
                        # No RTSP URL, username or password is included in default exports.
                        pass
                    gz.write((json.dumps(item, ensure_ascii=False, default=str) + '\n').encode('utf-8'))
        return path
    except Exception:
        try: os.remove(path)
        except OSError: pass
        raise


_tg_transition_lock = threading.Lock()
_tg_last_transition_sent = 0.0

_TG_COOLDOWN_SECONDS = max(10, int(os.getenv('CAMERA_TELEGRAM_COOLDOWN_SECONDS','300')))
_TG_EVENT_COOLDOWNS = {
    'status_transition': _TG_COOLDOWN_SECONDS,
    'worker_error': max(30, int(os.getenv('CAMERA_TELEGRAM_WORKER_COOLDOWN_SECONDS','600'))),
    'db_error': max(30, int(os.getenv('CAMERA_TELEGRAM_DB_COOLDOWN_SECONDS','900'))),
    'security': max(30, int(os.getenv('CAMERA_TELEGRAM_SECURITY_COOLDOWN_SECONDS','600'))),
}

def _telegram_cooldown_allows(event_key: str, seconds: int) -> bool:
    now=time.time()
    try:
        c=_db_conn()
        row=c.execute('SELECT last_sent FROM telegram_cooldowns WHERE event_key=?',(event_key,)).fetchone()
        if row and now-float(row['last_sent'] or 0) < seconds:
            c.execute('UPDATE telegram_cooldowns SET suppressed=suppressed+1 WHERE event_key=?',(event_key,)); c.commit(); return False
        c.execute('INSERT INTO telegram_cooldowns(event_key,last_sent,suppressed) VALUES(?,?,0) ON CONFLICT(event_key) DO UPDATE SET last_sent=excluded.last_sent', (event_key,now)); c.commit(); return True
    except Exception:
        return True


def tg_notify_status_transition(cam_id: str, old_status: str,
                                new_status: str) -> None:
    """Send useful state changes without flooding Telegram during validation."""
    global _tg_last_transition_sent
    if new_status not in {'online', 'offline'} or old_status == new_status:
        return
    if not _telegram_cooldown_allows(f'status:{cam_id}:{new_status}', _TG_EVENT_COOLDOWNS['status_transition']):
        log.debug('Telegram status alert cooldown for camera=%s', cam_id)
        return
    cam = db_get_camera(cam_id) or {'label': cam_id, 'ip': ''}
    msg = (f"📡 *Camera status changed*\n"
           f"Camera: `{cam.get('label') or cam.get('ip') or cam_id}`\n"
           f"Status: `{old_status or 'unknown'} → {new_status}`\n"
           f"Time: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`")
    threading.Thread(target=_send_telegram, args=(msg,),
                     daemon=True, name='tg-status').start()


def tg_notify_startup(port: int) -> None:
    def _send():
        try:
            stats = db_get_stats()
            msg   = (f"📷 *Camera Dashboard v{VERSION} Started*\n"
                     f"🕐 `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`\n"
                     f"📊 Cameras: {stats.get('total',0)} total · "
                     f"{stats.get('online',0)} online\n"
                     f"🌐 http://localhost:{port}")
            _send_telegram(msg)
        except Exception:
            pass
    threading.Thread(target=_send, daemon=True, name='tg-startup').start()


def tg_notify_shutdown() -> None:
    try:
        msg = (f"🔴 *Camera Dashboard Stopped*\n"
               f"🕐 `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`")
        threading.Thread(target=lambda: _send_telegram(msg), daemon=True, name='tg-shutdown').start()
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# FFMPEG STREAMING  (WebSocket MJPEG)
# ══════════════════════════════════════════════════════════════════════════════

def _ffmpeg_stream_cmd(rtsp_url: str, transport: str = 'tcp') -> list[str]:
    return [
        'ffmpeg', '-hide_banner', '-loglevel', 'error',
        '-rtsp_transport', transport,
        '-timeout', '8000000',  # 8 seconds in microseconds for FFmpeg RTSP timeout builds,
        '-i', rtsp_url,
        '-f', 'mjpeg', '-q:v', '5', '-r', '12',
        '-vf', (f'scale={STREAM_MAX_WIDTH}:-2' if STREAM_MAX_WIDTH else 'null'), 'pipe:1',
    ]


def _ffmpeg_snap_cmd(rtsp_url: str, transport: str = 'tcp') -> list[str]:
    return [
        'ffmpeg', '-hide_banner', '-loglevel', 'error',
        '-rtsp_transport', transport,
        '-i', rtsp_url,
        '-frames:v', '1', '-f', 'image2', '-vcodec', 'mjpeg', '-q:v', '2', 'pipe:1',
    ]


def _stream_jpeg_frames(proc: subprocess.Popen, first_frame_timeout: float = 12.0,
                        idle_timeout: float = 20.0):
    """Yield JPEG frames and fail explicitly when FFmpeg produces no video."""
    buf = b''
    started = time.monotonic()
    last_data = started
    got_frame = False
    if not proc.stdout:
        raise RuntimeError('FFmpeg stdout pipe unavailable')
    fd = proc.stdout.fileno()
    while True:
        deadline = (started + max(1.0, first_frame_timeout) if not got_frame
                    else last_data + max(1.0, idle_timeout))
        wait = max(0.0, deadline - time.monotonic())
        readable, _, _ = select.select([fd], [], [], min(wait, 1.0))
        if not readable:
            if time.monotonic() >= deadline:
                if not got_frame:
                    raise TimeoutError(f'no-video-frame-within-{first_frame_timeout:.0f}s')
                raise TimeoutError(f'video-frame-stalled-for-{idle_timeout:.0f}s')
            if proc.poll() is not None:
                break
            continue
        chunk = os.read(fd, 8192)
        if not chunk:
            break
        last_data = time.monotonic()
        buf += chunk
        if len(buf) > STREAM_MAX_BUFFER:
            soi = buf.rfind(b'\xff\xd8')
            buf = buf[soi:] if soi >= 0 else buf[-65536:]
        while True:
            frame_start = buf.find(b'\xff\xd8')
            frame_end = buf.find(b'\xff\xd9', frame_start + 2) if frame_start != -1 else -1
            if frame_start == -1 or frame_end == -1:
                break
            frame = buf[frame_start:frame_end + 2]
            buf = buf[frame_end + 2:]
            got_frame = True
            yield frame


def _ffmpeg_diagnostic(proc: Optional[subprocess.Popen],
                       stderr_lines: Optional[list[str]] = None) -> str:
    """Return a short actionable FFmpeg error without logging credentials."""
    if not proc:
        return 'FFmpeg did not start'
    detail = '\n'.join(stderr_lines or []).strip()
    detail = re.sub(r'rtsp://[^\s]+', '[RTSP URL]', detail, flags=re.IGNORECASE)
    detail = ' '.join(detail.split())
    if detail:
        return detail[-500:]
    code = proc.returncode
    return f'FFmpeg exited (code {code})' if code is not None else 'Browser stream unavailable'


def _capture_ffmpeg_stderr(proc: subprocess.Popen) -> list[str]:
    """Drain FFmpeg stderr concurrently so diagnostics cannot deadlock a stream."""
    lines: list[str] = []

    def _reader() -> None:
        try:
            if not proc.stderr:
                return
            for raw in iter(proc.stderr.readline, b''):
                text = raw.decode(errors='replace').strip()
                if text:
                    lines.append(text[-1000:])
                    del lines[:-100:]
        except (OSError, ValueError):
            pass

    threading.Thread(target=_reader, daemon=True,
                     name=f'ffmpeg-stderr-{proc.pid}').start()
    return lines


def _stop_ffmpeg(proc: Optional[subprocess.Popen]) -> None:
    if not proc or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=1)
        except Exception:
            pass


def _reserve_stream_slot(cam_id: str) -> bool:
    with _streams_lock:
        total=sum(_stream_subscribers.values())
        if total>=MAX_STREAMS: return False
        _stream_subscribers[cam_id]=int(_stream_subscribers.get(cam_id,0))+1
        _streams.setdefault(cam_id,set())
        return True

def _register_stream_proc(cam_id,proc):
    owner = str(session.get('user') or 'public') if has_request_context() else 'system'
    with _streams_lock:
        _streams.setdefault(cam_id,set()).add(proc)
        _stream_proc_owners[proc] = owner

def _unregister_stream_proc(cam_id,proc):
    with _streams_lock:
        bucket=_streams.get(cam_id)
        if bucket is not None: bucket.discard(proc)
        _stream_proc_owners.pop(proc, None)
        if bucket is not None and not bucket: _streams.pop(cam_id,None)

def _release_stream_slot(cam_id: str) -> None:
    with _streams_lock:
        n=int(_stream_subscribers.get(cam_id,0))-1
        if n>0:_stream_subscribers[cam_id]=n
        else:_stream_subscribers.pop(cam_id,None)

def _stream_urls_for_camera(cam_id: str) -> list[str]:
    cam = db_get_camera(cam_id)
    urls = [u for u in (cam or {}).get('rtsp_urls', [])
            if isinstance(u, str) and u.lower().startswith(('rtsp://','rtsps://'))]

    # Recover credentials from DB columns for imported URLs that omitted them.
    # Never guess a password and never modify the stored URL/database record.
    username = str((cam or {}).get('username') or '').strip()
    password = str((cam or {}).get('password') or '')
    if password:
        repaired = []
        for url in urls:
            try:
                parts = urlsplit(url)
                if parts.scheme.lower() not in {'rtsp', 'rtsps'} or parts.password:
                    repaired.append(url)
                    continue
                user = unquote(parts.username or '') or username
                if not user:
                    repaired.append(url)
                    continue
                host = parts.hostname or ''
                if ':' in host and not host.startswith('['):
                    host = f'[{host}]'
                port = f':{parts.port}' if parts.port else ''
                auth = f'{quote(user, safe="")}:{quote(password, safe="")}@'
                rebuilt = f'{parts.scheme}://{auth}{host}{port}{parts.path or ""}'
                if parts.query:
                    rebuilt += '?' + parts.query
                if parts.fragment:
                    rebuilt += '#' + parts.fragment
                repaired.append(rebuilt)
            except (TypeError, ValueError):
                repaired.append(url)
        urls = repaired

    return [item[2] for item in sorted(
        ((-_score_rtsp_candidate(cam or {}, url, pos), pos, url)
         for pos, url in enumerate(urls)),
        key=lambda item: (item[0], item[1]))]


def _resolve_camera_rtsp(cam_id: str, requested: str = '') -> tuple[Optional[str], str]:
    """Resolve a stored candidate URL and return a safe diagnostic on failure."""
    stored = _stream_urls_for_camera(cam_id)
    if requested and requested in stored:
        return requested, ''
    if stored:
        return stored[0], ''
    # Never execute a browser-supplied RTSP URL when SQLite has no stored
    # candidate. This keeps credentials and arbitrary network targets server-side.
    return None, 'No stored RTSP URL'


# ══════════════════════════════════════════════════════════════════════════════
# HTML TEMPLATES  (embedded — no files on disk)
# ══════════════════════════════════════════════════════════════════════════════

# ─── Camera card grid fragment (Jinja2) ───────────────────────────────────────
_HTML_CAM_GRID = r"""
{% if not cameras %}
<div style="grid-column:1/-1;padding:24px;text-align:center;color:var(--muted);font-size:13px">
  No cameras in this group.
</div>
{% else %}
{% for cam in cameras %}
{% set rtsp0 = cam.rtsp_urls[0] if cam.rtsp_urls else '' %}
{% set rtsp_display0 = cam.rtsp_display[0] if cam.rtsp_display else '' %}
<div class="cam-card"
     data-id="{{ cam.id }}"
      data-device-id="{{ cam.device_id or '' }}"
     data-has-rtsp="{{ '1' if rtsp0 else '0' }}"
     data-status="{{ cam.status }}"
     data-favorite="{{ cam.favorite }}"
     data-group="{{ group_name | e }}"
     data-search='{{ (cam.ip~' '~cam.camera_type~' '~cam.country~' '~(cam.city or '')~' '~(cam.label or '')~' '~(cam.username or ''))|lower|tojson|forceescape }}'>

  <div class="cam-thumb" id="thumb-{{ cam.id }}"
       {% if rtsp0 and can_stream %}data-action-thumb="watch" data-id="{{ cam.id }}" title="Click to watch"{% endif %}>
    {% if cam.snapshot_path %}
    <img class="snap-preview" src="/snapshots/{{ cam.snapshot_path | e }}" alt="snap"
         onerror="this.style.display='none'">
    {% if rtsp0 and can_stream %}<div class="snap-overlay"><span>&#9654;</span></div>{% endif %}
    {% else %}
    <div class="placeholder">
      <span class="play-icon">{% if rtsp0 and can_stream %}&#9654;{% else %}&#128683;{% endif %}</span>
      <span class="ph-label">{% if rtsp0 and can_stream %}Click to watch{% elif rtsp0 %}Live playback permission required{% else %}No RTSP{% endif %}</span>
    </div>
    {% endif %}
  </div>

  <div class="cam-info">
    <div class="card-top-row">
      <input class="select-cam" type="checkbox" data-select-id="{{ cam.id }}"
             aria-label="Select {{ cam.label or cam.ip or cam.id | e }}">
      <span class="status-badge status-{{ cam.status }}">
        {% if cam.status == 'online' %}&#128994; LIVE
        {% elif cam.status == 'checking' %}&#128992; CHECKING
        {% elif cam.status == 'offline' %}&#128308; OFFLINE
        {% else %}&#9898; UNKNOWN{% endif %}
      </span>
      <span class="cam-type" title="{{ cam.camera_type | e }}">{{ cam.camera_type }}</span>
      <div class="top-actions">
        <button class="fav-btn {% if cam.favorite %}active{% endif %}"
                data-action="fav" data-id="{{ cam.id }}"
                title="Favorite">&#9733;</button>
        {% if can_camera_manage %}<button class="del-btn"
                data-action="del" data-id="{{ cam.id }}"
                title="Remove">&#10005;</button>{% endif %}
        {% if cam.ip or rtsp0 %}
        <button class="fav-btn" data-action="device" data-id="{{ cam.id }}"
                title="{% if cam.device_id %}Device details{% else %}Login to device{% endif %}">
          {% if cam.device_id %}ⓘ{% else %}🔐{% endif %}
        </button>
        {% endif %}
      </div>
    </div>

    <div class="cam-name" title="{{ cam.display_name | e }}">{{ cam.display_name | e }}</div>

    {% if cam.ip %}
    <div class="cam-ip">
      <span class="sdot {{ cam.status }}">&#9679;</span>
      {{ cam.ip }}{% if cam.port and cam.port not in (80, 554) %}:{{ cam.port }}{% endif %}
    </div>
    {% endif %}

    {% if cam.city or cam.country or cam.area %}
    <div class="cam-location">&#128205; {{ cam.area or '' }}{% if cam.area and cam.city %}, {% endif %}{{ cam.city or '' }}{% if (cam.area or cam.city) and cam.region %}, {% endif %}{{ cam.region or '' }}{% if (cam.area or cam.city or cam.region) and cam.country %}, {% endif %}{{ cam.country or '' }}</div>
    {% endif %}
    {% if cam.description %}<div class="cam-ts">{{ cam.description | e }}</div>{% endif %}

    {% if cam.codec or cam.resolution or cam.fps or cam.latency_ms %}
    <div class="meta-row">
      {% if cam.codec %}<span class="meta-badge codec">{{ cam.codec }}</span>{% endif %}
      {% if cam.resolution %}<span class="meta-badge res">{{ cam.resolution }}</span>{% endif %}
      {% if cam.fps and cam.fps > 0 %}<span class="meta-badge fps">{{ "%.1f"|format(cam.fps) }}fps</span>{% endif %}
      {% if cam.latency_ms and cam.latency_ms > 0 %}<span class="meta-badge lat">{{ cam.latency_ms }}ms</span>{% endif %}
    </div>
    {% endif %}

    {% if cam.last_checked %}
    <div class="cam-ts">Checked: {{ cam.last_checked[:16] | replace('T',' ') }}</div>
    {% endif %}

    {% if rtsp0 and can_stream %}
    <div class="rtsp-box" data-action="copy" data-id="{{ cam.id }}"
         title="Click to copy">{{ rtsp_display0 }}</div>
    {% endif %}

    <div class="actions">
      {% if rtsp0 and can_stream %}
      <button class="btn btn-green watch-btn" data-action="watch" data-id="{{ cam.id }}">&#9654; Watch</button>
      <button class="btn btn-yellow" data-action="device-login" data-id="{{ cam.id }}" title="Login/test this CCTV device using its stored credentials">🔐 Login/Test</button>
      <button class="btn"            data-action="stopall" data-id="{{ cam.id }}" title="Stop all currently playing camera feeds">&#9632; Stop All</button>
      <button class="btn"            data-action="fullscreen" data-id="{{ cam.id }}">&#9974; Full</button>
      <button class="btn"            data-action="diagnose" data-id="{{ cam.id }}">&#9881; Diag</button>
      <button class="btn"            data-action="copy"  data-id="{{ cam.id }}">&#128203; Copy</button>
      <button class="btn btn-yellow" data-action="vlc"   data-id="{{ cam.id }}">&#127902; VLC</button>
      <button class="btn"            data-action="snap"  data-id="{{ cam.id }}">&#128247; Snap</button>
      <button class="btn"            data-action="tg"    data-id="{{ cam.id }}">&#10148; TG</button>
      {% endif %}
      <button class="btn" data-action="geo-lookup" data-id="{{ cam.id }}" title="Look up approximate public-IP location and save city/region/country">🌍 Lookup Location</button>
      <button class="btn btn-primary" data-action="intel" data-id="{{ cam.id }}" title="On-demand NVR/DVR status, storage, users, playback and location">🖥 Device Intel</button>
      <button class="btn btn-primary" data-action="detect-feed" data-id="{{ cam.id }}" title="Detect an authenticated RTSP feed for this camera/door camera">📡 Detect Feed</button>
      {% if cam.web_url %}<a class="btn" href="{{ cam.web_url | e }}" target="_blank" rel="noopener">&#127760;</a>{% endif %}
    </div>
  </div>
</div>
{% endfor %}
{% endif %}

{% if offset + (cameras|length) < total %}
<div class="load-more" style="grid-column:1/-1;text-align:center;padding:14px"
     data-group="{{ group_name | e }}"
     data-offset="{{ offset + (cameras|length) }}"
     data-remaining="{{ total - offset - (cameras|length) }}"
     data-pagesize="{{ page_size }}">
  <button class="btn btn-primary" data-action="loadmore">
    &#11015; Load {{ [page_size, total - offset - (cameras|length)]|min }} more
    &nbsp;({{ total - offset - (cameras|length) }} remaining)
  </button>
</div>
{% endif %}
"""

# ─── Full dashboard page ──────────────────────────────────────────────────────
_HTML_DASHBOARD = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta name="csrf-token" content="{{ csrf_token|e }}">
<script>
(function(){const t=document.querySelector('meta[name="csrf-token"]')?.content;if(!t)return;const orig=window.fetch;window.fetch=function(input,init){init=init||{};const u=typeof input==='string'?input:input.url;const method=String(init.method||(typeof input!=='string'&&input.method)||'GET').toUpperCase();if(/^(POST|PUT|PATCH|DELETE)$/.test(method)){init.headers=new Headers(init.headers||{});if(!init.headers.has('X-CSRF-Token'))init.headers.set('X-CSRF-Token',t);}return orig.call(this,input,init);};})();
</script>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>📷 Camera Dashboard v{{ app_version }}</title>
<style>
:root{
  --bg:#0d1117;--surf:#161b22;--border:#30363d;--accent:#58a6ff;
  --green:#3fb950;--yellow:#d29922;--orange:#e36209;--red:#f85149;
  --text:#c9d1d9;--muted:#8b949e;--card:#1c2128;--hover:#21262d;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;font-size:14px}
a{color:inherit;text-decoration:none}

/* ── Header ── */
header{background:var(--surf);border-bottom:1px solid var(--border);
  padding:8px 14px;display:flex;align-items:center;gap:8px;
  position:sticky;top:0;z-index:300;flex-wrap:wrap}
header h1{font-size:15px;color:var(--accent);white-space:nowrap;flex-shrink:0}
.search-bar{flex:1;min-width:140px;max-width:300px;padding:5px 10px;
  background:var(--bg);border:1px solid var(--border);border-radius:6px;
  color:var(--text);font-size:13px}
.search-bar:focus{outline:none;border-color:var(--accent)}
.hdr-stats{color:var(--muted);font-size:12px;white-space:nowrap;
  display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.stat-pill{padding:1px 7px;border-radius:10px;font-size:11px;font-weight:700}
.sp-on {background:rgba(63,185,80,.15);color:#3fb950}
.sp-chk{background:rgba(210,153,34,.15);color:#d29922}
.sp-off{background:rgba(248,81,73,.15);color:#f85149}
.sp-unk{background:rgba(139,148,158,.15);color:#8b949e}
.no-tg{color:var(--red);font-size:11px}

/* ── Filter bar ── */
.filter-bar{background:var(--surf);border-bottom:1px solid var(--border);
  padding:5px 14px;display:flex;align-items:center;gap:6px;flex-wrap:wrap;
  position:sticky;top:44px;z-index:299}
.filter-label{font-size:11px;color:var(--muted);text-transform:uppercase;
  letter-spacing:.05em;flex-shrink:0}
.filter-select{padding:3px 6px;background:var(--bg);border:1px solid var(--border);
  border-radius:5px;color:var(--text);font-size:12px;cursor:pointer}
.filter-btn{padding:3px 9px;border-radius:5px;border:1px solid var(--border);
  cursor:pointer;font-size:12px;background:var(--bg);color:var(--muted);transition:all .15s}
.filter-btn.active{background:var(--accent);color:#000;border-color:var(--accent);font-weight:700}
.filter-sep{width:1px;height:14px;background:var(--border);flex-shrink:0}

/* ── Validation progress ── */
#val-bar{background:var(--surf);border-bottom:1px solid var(--border);
  padding:4px 14px;align-items:center;gap:9px;display:none}
#val-bar.show{display:flex}
.prog-track{flex:1;height:5px;background:var(--border);border-radius:3px;overflow:hidden}
.prog-fill{height:100%;background:var(--accent);border-radius:3px;
  transition:width .4s;width:0}
.val-txt{font-size:11px;color:var(--muted);white-space:nowrap;min-width:180px}

/* ── Main ── */
main{padding:10px 14px;max-width:1800px;margin:0 auto}

/* ── Group sections ── */
.group-section{margin:6px 0 0}
.section-header{display:flex;align-items:center;gap:7px;padding:7px 11px;
  background:var(--surf);border:1px solid var(--border);border-radius:8px;
  cursor:pointer;user-select:none;transition:border-color .15s}
.section-header:hover{border-color:var(--accent)}
.section-header h2{font-size:13px;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chevron{font-size:10px;color:var(--muted);transition:transform .2s;flex-shrink:0}
.group-section.expanded .chevron{transform:rotate(90deg)}
.badge{background:var(--accent);color:#000;border-radius:10px;padding:1px 7px;
  font-size:11px;font-weight:700;flex-shrink:0}
.category-progress{font-size:10px;color:#d29922;white-space:nowrap;margin-left:4px}
.sec-mini{font-size:11px;display:flex;gap:5px;flex-shrink:0}
.cam-grid{display:none;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));
  gap:10px;padding:8px 0 2px}
.group-section.expanded .cam-grid{display:grid}
.server-search-results{display:none;margin-top:8px}
.server-search-results.active{display:block}
.section-hidden{display:none!important}
.loading-msg,.err-msg{grid-column:1/-1;padding:18px;text-align:center;
  font-size:13px;color:var(--muted)}
.err-msg{color:var(--red)}
.sec-refresh-btn{flex-shrink:0;background:transparent;border:1px solid var(--border);
  border-radius:4px;color:var(--muted);cursor:pointer;font-size:11px;
  padding:2px 6px;transition:all .15s}
.sec-refresh-btn:hover{border-color:var(--accent);color:var(--accent)}
.sec-refresh-btn.spinning{animation:spin 1s linear infinite;border-color:var(--yellow);color:var(--yellow)}
@keyframes spin{to{transform:rotate(360deg)}}

/* ── Camera card ── */
.cam-card{background:var(--card);border:1px solid var(--border);border-radius:10px;
  overflow:hidden;transition:border-color .2s;display:flex;flex-direction:column}
.cam-card:hover{border-color:var(--accent)}
.cam-card.hidden{display:none}

.cam-thumb{position:relative;width:100%;aspect-ratio:16/9;background:#000;
  display:flex;align-items:center;justify-content:center;cursor:pointer;overflow:hidden}
canvas.live,img.live{width:100%;height:100%;object-fit:contain;display:block}
.placeholder{display:flex;flex-direction:column;align-items:center;gap:5px;
  color:var(--muted);font-size:12px;user-select:none;text-align:center;padding:8px;width:100%}
.play-icon{font-size:36px;opacity:.45}
.live-badge{position:absolute;top:6px;left:6px;background:#e53e3e;color:#fff;
  font-size:10px;font-weight:700;padding:2px 6px;border-radius:4px}
.stop-btn{position:absolute;top:6px;right:6px;background:rgba(0,0,0,.65);color:#fff;
  border:none;border-radius:4px;padding:2px 7px;cursor:pointer;font-size:11px}
.snap-preview{width:100%;height:100%;object-fit:cover;display:block;opacity:.85}
.snap-overlay{position:absolute;inset:0;background:rgba(0,0,0,.4);
  display:flex;align-items:center;justify-content:center;opacity:0;transition:opacity .2s}
.cam-thumb:hover .snap-overlay{opacity:1}
.snap-overlay span{font-size:30px}

.cam-info{padding:7px 9px;display:flex;flex-direction:column;gap:3px;flex:1}
.card-top-row{display:flex;align-items:center;gap:4px;margin-bottom:2px}
.status-badge{padding:1px 7px;border-radius:10px;font-size:10px;font-weight:700;flex-shrink:0}
.status-online  {background:rgba(63,185,80,.15);color:#3fb950}
.status-checking{background:rgba(210,153,34,.15);color:#d29922}
.status-offline {background:rgba(248,81,73,.15);color:#f85149}
.status-unknown {background:rgba(139,148,158,.15);color:#8b949e}
.cam-type{font-size:10px;color:var(--muted);text-transform:uppercase;
  letter-spacing:.04em;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.top-actions{display:flex;gap:2px;flex-shrink:0}
.fav-btn,.del-btn{border:none;background:transparent;cursor:pointer;
  font-size:14px;padding:2px 3px;border-radius:3px;color:var(--muted)}
.fav-btn:hover,.fav-btn.active{color:var(--yellow)}
.del-btn:hover{color:var(--red)}
.select-cam{accent-color:var(--accent);width:15px;height:15px;cursor:pointer}
.cam-ip{font-family:monospace;color:var(--accent);font-size:13px;font-weight:600;
  display:flex;align-items:center;gap:4px}
.sdot{font-size:9px;flex-shrink:0;color:#8b949e}
.sdot.online  {color:#3fb950}
.sdot.checking{color:#d29922}
.sdot.offline {color:#f85149}
.cam-cred{font-family:monospace;font-size:11px;color:var(--yellow)}
.cam-name{font-size:12px;font-weight:700;color:var(--text);line-height:1.35;overflow-wrap:anywhere;margin:3px 0 2px}.cam-location{font-size:11px;color:var(--muted)}
.cam-ts{font-size:10px;color:var(--muted);font-family:monospace}
.meta-row{display:flex;gap:3px;flex-wrap:wrap;margin:1px 0}
.meta-badge{padding:1px 5px;border-radius:4px;font-size:10px;font-weight:600;
  background:var(--hover);color:var(--text);font-family:monospace}
.meta-badge.lat{color:#3fb950}
.meta-badge.codec{color:var(--accent)}
.rtsp-box{background:var(--bg);border:1px solid var(--border);border-radius:5px;
  padding:3px 6px;font-family:monospace;font-size:10px;color:#3fb950;
  word-break:break-all;max-height:38px;overflow:hidden;cursor:pointer;margin:2px 0}
.rtsp-box:hover{border-color:#3fb950}
.actions{display:flex;gap:3px;flex-wrap:wrap;margin-top:3px}
.btn{padding:3px 8px;border-radius:5px;border:1px solid var(--border);cursor:pointer;
  font-size:11px;text-decoration:none;display:inline-flex;align-items:center;gap:3px;
  background:var(--surf);color:var(--text);transition:opacity .15s;white-space:nowrap}
.btn:hover{opacity:.75}
.btn-green  {background:#3fb950;color:#000;border-color:#3fb950;font-weight:700}
.btn-yellow {background:var(--yellow);color:#000;border-color:var(--yellow);font-weight:700}
.btn-primary{background:var(--accent);color:#000;border-color:var(--accent);font-weight:700}
.btn-danger {background:var(--red);color:#fff;border-color:var(--red);font-weight:700}
.btn-stop-app{background:#5b1f2a;color:#fff;border-color:var(--red)}

/* ── Toast notifications ── */
.toast{position:fixed;bottom:18px;right:18px;padding:9px 14px;border-radius:8px;
  font-weight:600;opacity:0;transition:opacity .3s;pointer-events:none;z-index:999;
  font-size:13px;max-width:320px;word-break:break-word;background:#3fb950;color:#000}
.toast.error{background:var(--red);color:#fff}
.toast.show{opacity:1}
.tg-toast{position:fixed;bottom:18px;left:18px;background:var(--accent);color:#000;
  padding:9px 14px;border-radius:8px;font-weight:600;opacity:0;
  transition:opacity .3s;pointer-events:none;z-index:999;font-size:13px}
.tg-toast.show{opacity:1}

/* ── Modals ── */
dialog{background:var(--surf);border:1px solid var(--border);border-radius:12px;
  padding:22px;color:var(--text);max-width:460px;width:93%;
  box-shadow:0 8px 32px rgba(0,0,0,.65)}
dialog::backdrop{background:rgba(0,0,0,.72)}
dialog h2{margin-bottom:14px;font-size:16px;color:var(--accent)}
.field{margin-bottom:11px}
.field label{display:block;font-size:11px;color:var(--muted);margin-bottom:4px;
  text-transform:uppercase;letter-spacing:.04em}
.field input,.field select{width:100%;padding:7px 9px;background:var(--bg);
  border:1px solid var(--border);border-radius:6px;color:var(--text);
  font-size:13px;font-family:monospace}
.field input:focus,.field select:focus{outline:none;border-color:var(--accent)}
.modal-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:14px}
#m3u-progress{font-size:12px;color:var(--accent);margin-bottom:6px;display:none}
#search-empty{text-align:center;padding:40px;color:var(--muted);display:none;font-size:13px}

/* ── Multi-camera live wall ── */
.multi-view-modal{position:fixed;inset:0;background:rgba(0,0,0,.82);z-index:1000;
  display:none;align-items:stretch;justify-content:center;padding:14px}
.multi-view-modal.open{display:flex}
.multi-view-panel{width:100%;height:100%;background:var(--bg);border:1px solid var(--border);
  border-radius:10px;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 18px 60px rgba(0,0,0,.55)}
.multi-view-head{min-height:48px;padding:8px 12px;background:var(--surf);border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}
.multi-view-info{color:var(--muted);font-size:12px;margin-left:6px}
.multi-view-controls{display:flex;gap:5px;flex-wrap:wrap}
.multi-view-grid{flex:1;min-height:0;padding:8px;display:grid;grid-template-columns:repeat(var(--mv-cols,2),minmax(0,1fr));
  grid-auto-rows:minmax(0,1fr);gap:7px;overflow:auto}
.multi-tile{min-width:0;min-height:0;background:#000;border:1px solid var(--border);border-radius:7px;overflow:hidden;
  display:flex;flex-direction:column}
.multi-tile-head{height:28px;flex:0 0 28px;background:rgba(22,27,34,.96);padding:5px 8px;display:flex;justify-content:space-between;
  gap:8px;font-size:11px;color:var(--text);overflow:hidden}
.multi-tile-head span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.multi-tile-head small{color:var(--muted);white-space:nowrap}
.multi-screen{position:relative;flex:1;min-height:0;background:#000;display:flex;align-items:center;justify-content:center}
.multi-canvas{display:block;width:100%;height:100%;object-fit:contain;background:#000}
.multi-status{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:12px;background:rgba(0,0,0,.35);pointer-events:none}
@media(max-width:900px){.multi-view-grid{grid-template-columns:repeat(2,minmax(0,1fr))!important}.multi-view-head{align-items:flex-start}}

/* ── v63 compact live operational metrics ── */
/* Keep the dashboard work-first: status information is compact and never dominates the camera area. */
/* v63/v107: work-first single-line operational metrics.
   Keep every operation visible in one horizontal strip so the camera/work area gets the space. */
.v63-ops{margin:3px 8px 4px;padding:4px;background:var(--surf);border:1px solid var(--border);border-radius:6px}
.v63-ops-head{display:flex;align-items:center;gap:5px;flex-wrap:nowrap;margin-bottom:3px;min-height:16px}
.v63-ops-title{font-size:9px;font-weight:700;color:var(--accent);white-space:nowrap}
.v63-ops-live{font-size:7px;color:var(--muted);margin-left:auto;white-space:nowrap}
.db-maintenance-state{font-size:8px;font-weight:700;padding:2px 6px;border-radius:9px;white-space:nowrap;border:1px solid var(--border);background:var(--card)}
.db-maintenance-state.busy{color:#ffcc66;border-color:#8a6d1f;background:rgba(138,109,31,.12)}
.db-maintenance-state.free{color:#3fb950;border-color:#238636;background:rgba(35,134,54,.12)}
.db-maintenance-state.failed{color:#f85149;border-color:#da3633;background:rgba(218,54,51,.10)}
.v63-ops-grid{display:grid;grid-template-columns:repeat(8,minmax(0,1fr));gap:2px;align-items:stretch}
.v63-metric{background:var(--card);border:1px solid var(--border);border-radius:4px;padding:3px 4px;min-width:0;height:31px;display:grid;grid-template-columns:auto minmax(0,1fr);grid-template-rows:12px 11px;column-gap:4px;align-items:center}
.v63-metric-label{grid-column:1 / -1;font-size:6px;color:var(--muted);text-transform:uppercase;letter-spacing:.02em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1}
.v63-metric-value{grid-column:1;font-size:10px;font-weight:700;line-height:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin:0;min-width:0}
.v63-metric-detail{grid-column:2;font-size:6px;color:var(--muted);margin:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;line-height:1}
.v63-progress{grid-column:1 / -1;height:1px;background:var(--bg);border-radius:1px;overflow:hidden;margin:1px 0 0;display:none}
.v63-progress span{display:block;height:100%;width:0;background:var(--accent);transition:width .25s ease}
.v63-ops-actions{display:flex;gap:2px;flex-wrap:nowrap;overflow:hidden;margin-top:3px}
.v63-ops-actions .btn{font-size:7px;padding:2px 4px;gap:1px;white-space:nowrap;min-height:18px}
#v66Ops .v63-ops-grid{grid-template-columns:repeat(3,minmax(0,1fr));gap:2px}
#v66Ops .v63-metric{height:28px;padding:2px 4px}
#v66Ops .v63-metric-value{font-size:9px}
#v66Ops .v63-metric-detail{font-size:6px}
@media(max-width:1200px){.v63-ops-grid{grid-template-columns:repeat(8,minmax(70px,1fr));overflow-x:auto}.v63-metric{min-width:70px}.v63-ops-actions{overflow-x:auto;scrollbar-width:thin}}
@media(max-width:600px){.v63-ops{margin-left:4px;margin-right:4px}.v63-ops-grid{grid-template-columns:repeat(8,minmax(78px,1fr));overflow-x:auto}.v63-ops-actions{overflow-x:auto}header{gap:2px!important}}
</style>
</head>
<body>
<script>window.__securityViewAllowed={{ 'true' if can_security else 'false' }};</script>

<!-- Header -->
<header>
  <h1>📷 Camera Dashboard</h1>
  <input class="search-bar" type="text" id="searchBox"
         placeholder="Search IP, country, type…" oninput="onSearch()">
  <div class="hdr-stats" id="hdrStats">
    <span id="statTotal">{{ total }} total</span>
    <span class="stat-pill sp-on"  id="statOn" >🟢 {{ online }}</span>
    <span class="stat-pill sp-chk" id="statChk">🟡 {{ checking }}</span>
    <span class="stat-pill sp-off" id="statOff">🔴 {{ offline }}</span>
    <span class="stat-pill sp-unk" id="statUnk">⚪ {{ unknown_c }}</span>
    <span id="statGroups">{{ num_groups }} groups</span>
    <span class="stat-pill sp-feed" id="statFeeds" title="Currently open live feeds">🎥 0 / {{ max_streams }} feeds</span>
    <span id="statTime" style="color:var(--muted);font-size:10px">{{ refresh }}</span>
  </div>
  {% if not tg_ok %}<span class="no-tg" title="Edit telegram_config.json">⚠ TG off</span>{% endif %}
  <span class="stat-pill" title="Currently logged-in dashboard user">👤 {{ current_user }} · {{ current_role }}</span>
  {% if current_user %}<button class="btn" onclick="openAccountPasswordModal()" title="Change your own dashboard password">🔑 Password</button>{% endif %}
  <button class="btn btn-primary" id="globalRefreshBtn" onclick="refreshAll()" title="Revalidate all cameras">🔄 Refresh</button>
  <button class="btn btn-primary" onclick="recheckFiles()" title="Re-read every camera playlist and text file">🔎 Recheck</button>
  {% if can_stream %}<button class="btn" id="headerStopFeedsBtn" onclick="stopAllCams()" title="Stop every active camera feed">■ Stop feeds</button>{% endif %}
  {% if can_db %}<button class="btn btn-stop-app" onclick="stopApplication()" title="Stop the Flask application">⏹ Stop app</button>{% endif %}
  {% if can_db %}<button class="btn" onclick="openTelegramSettings()" title="Configure Telegram securely; credentials are encrypted in SQLite">🔐 Telegram</button>{% endif %}
  {% if can_alerts %}<button class="btn" onclick="testTelegram()" title="Send a Telegram connection test">🧪 TG test</button>
  <button class="btn" onclick="sendCameraList()" title="Send a compressed camera export to Telegram">📤 TG list</button>{% endif %}
  {% if can_db %}<button class="btn" onclick="openAdminPopup()" title="Open administration tools in a popup">🛠 Admin</button>{% endif %}
  {% if can_camera_control %}<button class="btn" onclick="openDeviceModal()" title="Connect to one authorized CCTV device">🔌 Device</button>{% endif %}
  {% if can_camera_manage %}<button class="btn" onclick="openAddModal()">➕ Add</button>
  <button class="btn" onclick="openM3uModal()">📂 M3U</button>{% endif %}
  {% if can_db or can_diagnostics or can_stream or can_settings or can_alerts or can_health or can_security or can_reports %}<span class="dashboard-tools-label">WORK:</span>{% endif %}
  {% if can_db %}<button class="btn" onclick="dashboardTool('integrity')" title="SQLite integrity check">🧪 DB Integrity</button>
  <button class="btn" onclick="dashboardTool('backup')" title="Verified database backup">💾 DB Backup</button>{% endif %}
  {% if can_diagnostics %}<button class="btn" onclick="dashboardTool('diagnostics')" title="Run diagnostics for selected cameras">🩺 Diagnostics</button>{% endif %}
  {% if can_db %}<button class="btn btn-primary" id="bgSvcHeaderBtn" onclick="openBackgroundServices()" title="View and turn background services on/off">⚙ Services</button>{% endif %}
  {% if can_stream %}<button class="btn" onclick="dashboardTool('snapshots')" title="Take snapshots of selected cameras">📸 Snapshots</button>{% endif %}
  {% if can_diagnostics %}<button class="btn" onclick="dashboardTool('feature-matrix')" title="Priority 1–16 feature matrix">🧩 Feature Matrix</button>{% endif %}
  {% if can_settings %}<button class="btn" onclick="dashboardTool('layouts')" title="Manage saved live layouts">▦ Layouts</button>{% endif %}
  {% if can_alerts %}<button class="btn" onclick="dashboardTool('alerts')" title="View and test alerts">🔔 Alerts</button>{% endif %}
  {% if can_health %}<button class="btn" onclick="dashboardTool('health')" title="Health overview and history">❤️ Health</button>{% endif %}
  {% if can_diagnostics %}<button class="btn" onclick="dashboardTool('performance')" title="CPU/RAM/workers/performance">⚡ Performance</button>{% endif %}
  {% if can_security %}<button class="btn" onclick="dashboardTool('security')" title="Security status">🔐 Security</button>{% endif %}
  {% if can_settings %}<button class="btn" onclick="dashboardTool('recovery')" title="Stream recovery settings">♻ Recovery</button>{% endif %}
  {% if can_reports %}<button class="btn" onclick="dashboardTool('reports')" title="Selected device reports">📋 Reports</button>{% endif %}
  {% if can_db %}<button class="btn" onclick="openSystemCenter()" title="Reliability, storage, scan and setup tools">🛠 System</button>
  <button class="btn" onclick="dashboardTool('maintenance')" title="Maintenance and retention cleanup">🧹 Maintenance</button>{% endif %}
  {% if can_db %}<button class="btn" onclick="shrinkDatabase()" title="Checkpoint WAL, VACUUM database and keep only the latest verified backup">🗜️ Shrink DB</button>{% endif %}
  {% if can_db %}<button class="btn" onclick="exportUserConfig()" title="Export users and application settings only; no camera records">🔐 Export User/Config</button>
  <button class="btn" onclick="document.getElementById('userConfigImportFile').click()" title="Import users and application settings only">📥 Import User/Config</button>
  <input id="userConfigImportFile" type="file" accept="application/vnd.sqlite3,.sqlite,.db" style="display:none" onchange="importUserConfigFile(this)">{% endif %}
  {% if can_stream %}<button class="btn btn-primary" onclick="openMultiView(1)" title="1-camera live view">▦ 1</button>
  <button class="btn btn-primary" onclick="openMultiView(4)" title="4-camera live view">▦ 4</button>
  <button class="btn btn-primary" onclick="openMultiView(8)" title="8-camera live view">▦ 8</button>
  <button class="btn btn-primary" onclick="openMultiView(16)" title="16-camera live view">▦ 16</button>
  <button class="btn btn-primary" onclick="openMultiView(25)" title="25-camera live view">▦ 25</button>
  <button class="btn btn-primary" onclick="openMultiView(36)" title="36-camera live view">▦ 36</button>
  <button class="btn" onclick="openMultiView(49)" title="49-camera live view">▦ 49</button>
  <button class="btn" onclick="openMultiView(64)" title="64-camera live view">▦ 64</button>
  <button class="btn" onclick="openCustomMultiView()" title="Custom camera grid">▦ Custom</button>{% endif %}
  {% if can_diagnostics or can_reports %}<button class="btn btn-primary" onclick="openV43FeatureCenter()" title="Priority 1–16 feature status and coverage">🧰 All Features</button>{% endif %}
  <button class="btn" onclick="location.reload()" title="Reload page">↺</button>
</header>

<!-- v63/v107: compact live operational/worker/validation metrics — work-first layout -->
<section id="v63Ops" class="v63-ops v63-ops-compact" aria-label="Live operational metrics">
  <div class="v63-ops-head">
    <span class="v63-ops-title">⚙ Live Operations</span>
    <span id="v63OpsState" class="stat-pill sp-chk">Connecting…</span>
    <span id="dbMaintenanceState" class="db-maintenance-state" hidden>—</span>
    <span id="v63OpsLive" class="v63-ops-live">—</span>
  </div>
  <div class="v63-ops-grid">
    <div class="v63-metric"><div class="v63-metric-label">Worker</div><div id="v63Worker" class="v63-metric-value">—</div><div id="v63WorkerDetail" class="v63-metric-detail">—</div></div>
    <div class="v63-metric"><div class="v63-metric-label">Workers</div><div id="v63Workers" class="v63-metric-value">—</div><div id="v63WorkersDetail" class="v63-metric-detail">CPU/RAM adaptive</div></div>
    <div class="v63-metric"><div class="v63-metric-label">Validation</div><div id="v63Validation" class="v63-metric-value">—</div><div id="v63ValidationDetail" class="v63-metric-detail">—</div><div class="v63-progress"><span id="v63ValidationBar"></span></div></div>
    <div class="v63-metric"><div class="v63-metric-label">Speed</div><div id="v63Speed" class="v63-metric-value">—</div><div id="v63Eta" class="v63-metric-detail">ETA —</div></div>
    <div class="v63-metric"><div class="v63-metric-label">Queue</div><div id="v63Queue" class="v63-metric-value">—</div><div id="v63QueueDetail" class="v63-metric-detail">due / locked / stale</div></div>
    <div class="v63-metric"><div class="v63-metric-label">Live Feeds</div><div id="v63Feeds" class="v63-metric-value">—</div><div id="v63FeedsDetail" class="v63-metric-detail">limit —</div></div>
    {% if can_diagnostics %}<div class="v63-metric"><div class="v63-metric-label">CPU / RAM</div><div id="v63Resource" class="v63-metric-value">—</div><div id="v63ResourceDetail" class="v63-metric-detail">—</div></div>{% endif %}
    {% if can_db %}<div class="v63-metric"><div class="v63-metric-label">Database</div><div id="v63Db" class="v63-metric-value">—</div><div id="v63DbDetail" class="v63-metric-detail">—</div></div>{% endif %}
  </div>
  <div class="v63-ops-actions">
    <button class="btn" onclick="v63RefreshOps()">↻ Refresh</button>
    {% if can_diagnostics %}<button id="v64PauseBtn" class="btn" onclick="v64ValidationAction()">⏸ Pause Validation</button>
    <button class="btn" onclick="v64ValidationAction('retry-failed')">↻ Retry Failed</button>
    <button class="btn" onclick="v64ValidationAction('queue-cleanup')">🧹 Queue Cleanup</button>
    <button class="btn" onclick="v64ValidationAction('restart-worker')">🔄 Restart Worker</button>
    <button class="btn" onclick="v65ValidationAction('retry-exhausted')">↻ Retry Exhausted</button>
    <button class="btn" onclick="v65ValidationAction('reprioritize')">⇅ Reprioritize Queue</button>{% endif %}
    {% if can_diagnostics %}<button class="btn" onclick="dashboardTool('performance')">⚡ Performance</button>{% endif %}
    {% if can_db %}<button class="btn" onclick="dashboardTool('integrity')">🧪 DB Integrity</button>{% endif %}
  </div>
  <div id="v64ControlStatus" class="v63-ops-live" style="margin-top:6px">Validation controls ready.</div>
</section>

{% if can_diagnostics or can_reports or can_security %}
<section id="v66Ops" class="v63-ops" aria-label="Advanced observability">
  <div class="v63-ops-head"><span class="v63-ops-title">📊 Advanced Observability</span><span id="v66State" class="stat-pill sp-chk">Connecting…</span></div>
  <div class="v63-ops-grid">
    {% if can_diagnostics %}<div class="v63-metric"><div class="v63-metric-label">RTSP Quality</div><div id="v66Quality" class="v63-metric-value">—</div><div id="v66QualityDetail" class="v63-metric-detail">—</div></div>{% endif %}
    {% if can_reports %}<div class="v63-metric"><div class="v63-metric-label">Geo Coverage</div><div id="v66Geo" class="v63-metric-value">—</div><div id="v66GeoDetail" class="v63-metric-detail">Country / region / city</div></div>{% endif %}
    {% if can_security %}<div class="v63-metric"><div class="v63-metric-label">Security</div><div id="v66Security" class="v63-metric-value">—</div><div id="v66SecurityDetail" class="v63-metric-detail">—</div></div>{% endif %}
  </div>
</section>
{% endif %}

<!-- Filter bar -->
<div class="filter-bar">
  <span class="filter-label">Status</span>
  <select class="filter-select" id="statusFilter" onchange="applyFilters()">
    <option value="all">All</option>
    <option value="online">🟢 Live</option>
    <option value="checking">🟡 Checking</option>
    <option value="offline">🔴 Offline</option>
    <option value="unknown">⚪ Unknown</option>
  </select>
  <div class="filter-sep"></div>
  <button class="filter-btn" id="favsToggle" onclick="toggleFavs()">★ Favorites{% if favorites > 0 %} ({{ favorites }}){% endif %}</button>
  <div class="filter-sep"></div>
  <span class="filter-label">Group</span>
  <select class="filter-select" id="groupFilter" onchange="applyFilters()">
    <option value="all">All groups</option>
    {% for g in groups %}
    <option value="{{ g.group_key | e }}">{{ g.display_group | e }}</option>
    {% endfor %}
  </select>
  <div class="filter-sep"></div>
  <button class="filter-btn" onclick="bulkAction('favorite')">★ Selected favorites</button>
  <button class="filter-btn" onclick="bulkAction('recheck')">🔄 Selected recheck</button>
  <button class="filter-btn" onclick="bulkAction('export')">📤 Selected export</button>
  <button class="filter-btn" onclick="transferSelectedToPrompt()">📥 Transfer selected</button>
  <button class="filter-btn" onclick="bulkAction('delete')">🗑 Selected delete</button>
  <span class="selection-counter" id="selection-counter">0 selected</span>
  <button class="filter-btn" onclick="clearSelection()">✕ Clear selection</button>
  <button class="filter-btn" onclick="invertVisibleSelection()">⇄ Invert visible</button>
</div>

<!-- Validation progress bar -->
<div id="val-bar" {% if val_running %}class="show"{% endif %}>
  <div class="prog-track">
    <div class="prog-fill" id="val-fill"
         style="width:{% if val_total > 0 %}{{ (val_done/val_total*100)|round|int }}{% else %}0{% endif %}%"></div>
  </div>
  <span class="val-txt" id="val-txt">Validating… {{ val_done }}/{{ val_total }}</span>
</div>

<main>
<div id="server-search-results" class="server-search-results group-section">
  <div class="section-header">
    <span>🔎</span><h2>Search results</h2>
    <span class="badge" id="search-result-count">0</span>
  </div>
  <div class="cam-grid" id="search-result-grid"></div>
</div>
{% for g in groups %}
<div class="group-section {% if loop.first %}expanded{% endif %}"
     id="sec-{{ loop.index }}"
     data-group="{{ g.group_key | e }}"
     data-loaded="{% if loop.first %}loading{% else %}false{% endif %}">
  <div class="section-header" onclick="toggleSection(this)">
    <span class="chevron">▶</span>
    <h2>{% if loop.first %}⭐ {% endif %}{{ (g.display_title or g.display_group) | e }}</h2>
    <div class="sec-mini">
      {% if g.online %}<span style="color:#3fb950">🟢{{ g.online }}</span>{% endif %}
      {% if g.checking %}<span style="color:#d29922">🟡{{ g.checking }}</span>{% endif %}
      {% if g.offline %}<span style="color:#f85149">🔴{{ g.offline }}</span>{% endif %}
      <span class="category-progress" data-category-progress="{{ g.group_key | e }}" title="Category checking status"></span>
    </div>
    <span class="badge">{{ g.total }}</span>
    <button class="sec-refresh-btn category-select-all-btn" onclick="selectAllCategory(event, this)"
            data-group="{{ g.group_key | e }}" data-selected="0" title="Select all cameras in this category">☑</button>
    <button class="sec-refresh-btn" onclick="categoryRecheck(event, this)"
            data-group="{{ g.group_key | e }}" title="Recheck every camera in this category">🔄</button>
    <button class="sec-refresh-btn" onclick="categoryFavorite(event, this)"
            data-group="{{ g.group_key | e }}" title="Favorite every camera in this category">★</button>
    <button class="sec-refresh-btn" onclick="transferSelectedToGroup(event, this)"
            data-group="{{ g.group_key | e }}" title="Transfer selected cameras here">📥</button>
    <button class="sec-refresh-btn" onclick="categoryOfflineRecheck(event, this)"
            data-group="{{ g.group_key | e }}" title="Recheck only offline/unknown cameras in this category">⚡</button>
    <button class="sec-refresh-btn group-delete-btn" onclick="deleteGroup(event, this)"
            data-group="{{ g.group_key | e }}" title="Delete entire category">🗑</button>
  </div>
  <div class="cam-grid" id="grid-{{ loop.index }}">
    {% if loop.first %}<div class="loading-msg">⏳ Loading cameras…</div>{% endif %}
  </div>
</div>
{% endfor %}
<p id="search-empty">No groups match your filter.</p>
</main>

<!-- Transfer Cameras Modal -->
<dialog id="transfer-modal">
  <h2>📥 Transfer Cameras</h2>
  <div id="transfer-count" style="font-size:12px;color:var(--muted);margin-bottom:12px"></div>
  <div class="field"><label>Destination category</label>
    <select id="transfer-target" required></select>
  </div>
  <div class="modal-actions">
    <button type="button" class="btn" onclick="document.getElementById('transfer-modal').close()">Cancel</button>
    <button type="button" class="btn btn-green" onclick="confirmTransferSelection()">Transfer</button>
  </div>
</dialog>

<!-- Add Camera Modal -->
<dialog id="add-modal">
  <h2>➕ Add Camera</h2>
  <form onsubmit="submitAddCamera(event)">
    <div class="field"><label>RTSP URL *</label>
      <input id="add-rtsp" type="text" placeholder="rtsp://user:pass@ip:554/stream" required></div>
    <div class="field"><label>Label</label>
      <input id="add-label" type="text" placeholder="Front Door Camera"></div>
    <div class="field"><label>Group</label>
      <input id="add-group" type="text" placeholder="My Cameras" value="My Cameras"></div>
    <div class="modal-actions">
      <button type="button" class="btn" onclick="document.getElementById('add-modal').close()">Cancel</button>
      <button type="submit" class="btn btn-green">Add Camera</button>
    </div>
  </form>
</dialog>

<!-- M3U Import Modal -->
<dialog id="m3u-modal">
  <h2>📂 Import Playlist</h2>
  <form onsubmit="submitM3u(event)">
    <div class="field"><label>File (.m3u, .m3u8, .txt, .csv) *</label>
      <input id="m3u-file" type="file" accept=".m3u,.m3u8,.txt,.csv,.xml" required
             style="padding:5px 0;background:transparent;border:none;color:var(--text)"></div>
    <p style="font-size:11px;color:var(--muted);margin-bottom:8px">Only rtsp:// entries are imported.</p>
    <div id="m3u-progress"></div>
    <div class="modal-actions">
      <button type="button" class="btn" onclick="document.getElementById('m3u-modal').close()">Cancel</button>
      <button type="button" class="btn" onclick="previewM3u()">Preview</button>
      <button type="submit" class="btn btn-green" id="m3u-submit">Import</button>
    </div>
  </form>
</dialog>

<!-- Authorized CCTV device modal -->
<dialog id="device-modal">
  <h2>🔌 Connect CCTV Device</h2>
  <form onsubmit="discoverDevice(event)">
    <div class="field"><label>Device IP / hostname *</label>
      <input id="device-host" required placeholder="192.168.1.50"></div>
    <div class="field"><label>HTTP port</label>
      <select id="device-port-mode" onchange="toggleDevicePortMode()">
        <option value="auto">Automatic — check all TCP + HTTP/HTTPS ports</option>
        <option value="manual">Manual port</option>
      </select>
      <input id="device-port" type="number" value="80" min="1" max="65535"
             style="margin-top:6px" disabled>
      <small style="display:block;color:var(--muted);line-height:1.35">
        Automatic mode checks every supported port for TCP reachability and HTTP/HTTPS (not just port 80). Port order starts with 80 and 8080, then: 23, 81, 82, 83,
        443, 554, 1024, 1025, 1050, 2000, 2001, 3000, 4000, 4321, 4567,
        5060, 5554, 6789, 7080, 7554, 8000, 8081–8085, 8090, 8091, 8200,
        8443, 8554, 8888, 8899, 9000, 9001, 9527, 9999, 10554, 18080,
        18081, 34567, 37777–37779, 50000, 50001.
      </small>
    </div>
    <div class="field"><label>RTSP port</label>
      <input id="device-rtsp-port" type="number" value="554" min="1" max="65535"></div>
    <div class="field"><label>Vendor</label>
      <select id="device-vendor"><option value="auto">Auto detect</option>
      <option value="hikvision">Hikvision</option><option value="dahua">Dahua</option>
      <option value="nvr">NVR / DVR / XMEye</option>
      <option value="onvif">ONVIF-compatible</option>
      <option value="axis">Axis</option><option value="uniview">Uniview / UNV</option>
      <option value="reolink">Reolink</option><option value="tiandy">Tiandy</option>
      <option value="amcrest">Amcrest</option><option value="foscam">Foscam</option>
      <option value="hanwha">Hanwha / Wisenet</option>
      <option value="generic">Generic authenticated RTSP</option></select></div>
    <div class="field"><label>Username *</label>
      <input id="device-user" autocomplete="username" required></div>
    <div class="field"><label>Password *</label>
      <input id="device-pass" type="password" autocomplete="current-password" required></div>
    <label style="font-size:12px;color:var(--muted)">
      <input id="device-geo" type="checkbox"> Enrich approximate public-IP geolocation
    </label>
    <p id="device-progress" style="font-size:11px;color:var(--muted);white-space:pre-wrap"></p>
    <div class="modal-actions">
      <button type="button" class="btn" onclick="document.getElementById('device-modal').close()">Cancel</button>
      <button type="submit" class="btn btn-green" id="device-submit">Discover channels</button>
    </div>
  </form>
  <hr style="border:0;border-top:1px solid var(--border);margin:14px 0">
  <div class="field">
    <label>Authorized device inventory (.txt or .csv)</label>
    <input id="device-inventory-file" type="file" accept=".txt,.csv"
           style="padding:5px 0;background:transparent;border:none;color:var(--text)">
    <small style="display:block;color:var(--muted);line-height:1.35">
      One explicit device per row, or ValidCamera blocks. Credentials are used
      only for authenticated discovery; public-IP scanning is not performed.
    </small>
  </div>
  <p id="device-inventory-progress" style="font-size:11px;color:var(--muted);white-space:pre-wrap"></p>
  <div class="modal-actions">
    <button type="button" class="btn" onclick="importDeviceInventory()">Import device file</button>
  </div>
</dialog>

<div class="toast"    id="toast"></div>
<dialog id="device-intel-modal" style="width:min(900px,96vw);max-width:900px">
  <h2>🖥 Device Intelligence — On-demand</h2>
  <p style="color:var(--muted);margin:8px 0 12px">Nothing is queried automatically. Choose a section to request it from this device. Unsupported fields are reported as unavailable rather than guessed.</p>
  <div class="actions" style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px">
    <button class="btn" onclick="runDeviceIntel('storage')">💽 Disk / Storage</button>
    <button class="btn" onclick="runDeviceIntel('users')">👥 Device Users</button>
    <button class="btn" onclick="runDeviceIntel('channels')">📹 Channels / Live</button>
    <button class="btn" onclick="runDeviceIntel('playback')">⏪ Playback Retention</button>
    <button class="btn" onclick="runDeviceIntel('performance')">⚙ Performance</button>
    <button class="btn" onclick="runDeviceIntel('health')">❤️ Health</button>
    <button class="btn" onclick="runDeviceIntel('alerts')">🚨 Alerts</button>
    <button class="btn" onclick="lookupDeviceLocation()">🌍 Lookup Location</button>
    <button class="btn" onclick="editDeviceLocation()">📍 Edit / Save Location</button>
  </div>
  <div id="device-intel-status" style="color:var(--muted);margin-bottom:8px">Select a diagnostic button.</div>
  <pre id="device-intel-result" style="max-height:42vh;overflow:auto;white-space:pre-wrap;color:var(--text);background:var(--bg);padding:10px;border-radius:6px"></pre>
  <div class="modal-actions"><button type="button" class="btn" onclick="closeDeviceDialog('device-intel-modal')">Close</button></div>
</dialog>

<dialog id="telegram-settings-modal" style="width:min(700px,94vw);max-width:700px">
  <h2>🔐 Telegram Secure Settings</h2>
  <p style="color:var(--muted);line-height:1.45">
    Multiple Telegram destinations are supported. Every enabled destination receives the same status, alert, test message, snapshot or export. Bot Tokens and Chat IDs are encrypted before being written to SQLite and are never returned to the browser.
  </p>
  <div id="telegram-secure-status" style="padding:8px 10px;border-radius:7px;background:var(--bg);margin:10px 0;color:var(--muted)">Loading…</div>
  <div style="font-weight:700;margin:12px 0 6px">Configured destinations</div>
  <div id="telegram-destination-list" style="display:grid;gap:7px;margin-bottom:14px"></div>

  <form onsubmit="addTelegramDestination(event)" style="border:1px solid var(--border);border-radius:8px;padding:10px">
    <div style="font-weight:700;margin-bottom:8px">➕ Add destination</div>
    <div class="field"><label>Destination name</label><input id="tg-sec-name" value="Telegram Destination" maxlength="100"></div>
    <div class="field"><label>Bot Token</label><input id="tg-sec-token" type="password" autocomplete="new-password" placeholder="123456789:AA..." required></div>
    <div class="field"><label>Chat ID</label><input id="tg-sec-chat" type="password" autocomplete="off" placeholder="123456789 or -100..." required></div>
    <label style="display:flex;align-items:center;gap:7px;color:var(--muted);font-size:12px"><input id="tg-sec-enabled" type="checkbox" checked style="width:auto"> Enabled — this destination receives messages</label>
    <div class="modal-actions">
      <button type="submit" class="btn btn-green">🔒 Add & Encrypt</button>
      <button type="button" class="btn" onclick="clearTelegramAddFields()">Clear</button>
    </div>
  </form>

  <div class="modal-actions" style="margin-top:10px">
    <button type="button" class="btn" onclick="document.getElementById('telegram-settings-modal').close()">Close</button>
    <input id="tg-user-config-file" type="file" accept=".json,application/json" style="display:none" onchange="importTelegramUpload(this)">
    <button type="button" class="btn btn-green" onclick="document.getElementById('tg-user-config-file').click()">📂 Choose telegram_config.json from my device</button>
    <button type="button" class="btn" onclick="importTelegramFile()" title="Use a config file already present on the server">🗄 Import server file</button>
    <button type="button" class="btn" onclick="testTelegram()">🧪 Test all destinations</button>
    <button type="button" class="btn" style="color:#ff7b72" onclick="deleteTelegramSettings()">🗑 Remove all</button>
  </div>
  <p style="font-size:11px;color:var(--muted);margin-top:8px">Choose the JSON file from your computer. It is parsed in memory and valid Bot Tokens / Chat IDs are encrypted into this deployment's SQLite database; the uploaded file is not saved as a plaintext file.</p>
  <p style="font-size:11px;color:var(--muted);margin-top:10px">
    Security note: the encryption key is not stored in the database. Production deployments should set a persistent
    <code>CAMERA_ENCRYPTION_KEY</code> or <code>SESSION_SECRET</code>. If neither exists, the app creates a local 0600 master-key file.
  </p>
</dialog>

<dialog id="device-details-modal">
  <h2>📡 CCTV Device Details</h2>
  <pre id="device-details" style="max-height:55vh;overflow:auto;white-space:pre-wrap;color:var(--text)"></pre>
  <div class="modal-actions">
    <button type="button" class="btn" id="device-config-btn">⬇ Download config</button>
    <button type="button" class="btn" onclick="closeDeviceDialog('device-details-modal')">Back / close</button>
  </div>
</dialog>

<div class="tg-toast" id="tg-toast"></div>

<script>
'use strict';

// ── State ─────────────────────────────────────────────────────────────────────
const liveStreams = {};
window.__MAX_STREAMS = {{ max_streams|default(36) }};
window.__MAX_MULTI_VIEW = {{ max_multi_view|default(64) }};
let   _statsTimer = null;

// ── Event delegation for all camera card actions ───────────────────────────
document.addEventListener('click', e => {
  // Thumb click → watch
  const thumb = e.target.closest('[data-action-thumb="watch"]');
  if (thumb) {
    e.stopPropagation();
    // Clicking a live frame must not stop it accidentally. Use the explicit
    // Stop button for that; clicking a paused frame always starts playback.
    if (!liveStreams[thumb.dataset.id]) doAction('watch', thumb.dataset.id, thumb);
    return;
  }

  const el = e.target.closest('[data-action]');
  if (!el) return;
  const act  = el.dataset.action;
  const id   = el.dataset.id || el.closest('.cam-card')?.dataset.id;
  const card = id ? document.querySelector(`.cam-card[data-id="${id}"]`) : null;

  if (act === 'loadmore') { e.stopPropagation(); doLoadMore(el); return; }
  if (act === 'fav')  { e.stopPropagation(); doFavorite(id, el); return; }
  if (act === 'del')  { e.stopPropagation(); doDelete(id, card); return; }
  if (card) { e.stopPropagation(); doAction(act, id, card); }
});

function getUrls(card) {
  try { return JSON.parse(card?.dataset.rtsp || '[]'); } catch(_) { return []; }
}

async function fetchRtsp(id) {
  const r = await fetch('/api/cameras/' + encodeURIComponent(id) + '/rtsp');
  const d = await r.json();
  if (!r.ok || !d.ok || !d.rtsp) throw new Error(d.detail || 'No stored RTSP URL');
  return d.rtsp;
}

async function doAction(act, id, card) {
  const urls = getUrls(card);
  switch(act) {
    case 'watch': toggleWatch(id, card); break;
    case 'stop':  stopCam(id); break;
    case 'stopall': stopAllCams(); break;
    case 'device-login': loginToCamera(id); break;
    case 'detect-feed': detectCameraFeed(id); break;
    case 'fullscreen': fullscreenCam(id); break;
    case 'diagnose': diagnoseCamera(id); break;
    case 'copy':  fetchRtsp(id).then(copyRTSP).catch(e => showToast('❌ ' + e.message, true)); break;
    case 'vlc':   fetchRtsp(id).then(openVLC).catch(e => showToast('❌ ' + e.message, true)); break;
    case 'snap':  takeSnapshot(id); break;
    case 'tg':    sendTelegram(id); break;
    case 'device': openDeviceDetails(id); break;
    case 'intel': openDeviceIntel(id); break;
    case 'geo-lookup': lookupCameraLocation(id); break;
  }
}

// ── Lazy section loading ──────────────────────────────────────────────────────
async function loadGroupCams(sec) {
  const group = sec.dataset.group;
  const grid  = sec.querySelector('.cam-grid');
  sec.dataset.loaded = 'loading';
  grid.innerHTML = '<div class="loading-msg">⏳ Loading cameras…</div>';
  try {
    const r = await fetch('/group_html/' + encodeURIComponent(group));
    if (!r.ok) throw new Error('HTTP ' + r.status);
    grid.innerHTML = await r.text();
    sec.dataset.loaded = 'true';
    syncSelectionInSection(sec);
    probeSection(sec);
    applyFiltersToSection(sec);
    applyFilters();
  } catch(e) {
    grid.innerHTML = '<div class="err-msg">❌ Failed to load: ' + e.message + '</div>';
    sec.dataset.loaded = 'false';
  }
}

function toggleSection(hdr) {
  const sec=hdr.closest('.group-section');
  const key='camera-dashboard-section:'+(sec.dataset.group||sec.id||'unknown');
  const expanded=sec.classList.toggle('expanded');
  try{localStorage.setItem(key,expanded?'1':'0');}catch(_){}
  if(expanded && sec.dataset.loaded==='false') loadGroupCams(sec);
}

async function pollCategoryProgress() {
  try {
    const d = await fetch('/api/category/progress').then(r => r.json());
    document.querySelectorAll('[data-category-progress]').forEach(el => {
      const g = el.dataset.categoryProgress || '';
      const x = (d.categories || {})[g];
      if (!x) { el.textContent = ''; return; }
      if (x.active) {
        el.textContent = '⏳ ' + x.checking + '/' + x.total + ' checking';
        el.title = 'Currently scanning/rechecking this category';
      } else if (x.checking > 0) {
        el.textContent = '🟡 ' + x.checking + ' checking';
        el.title = 'Waiting/resume queue';
      } else { el.textContent = ''; }
    });
  } catch (_) {}
  setTimeout(pollCategoryProgress, 5000);
}

window.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.group-section[data-group]').forEach(sec=>{try{const x=localStorage.getItem('camera-dashboard-section:'+(sec.dataset.group||sec.id||'unknown'));if(x==='0')sec.classList.remove('expanded');if(x==='1')sec.classList.add('expanded');}catch(_) {}});
  document.querySelectorAll('.group-section[data-loaded="loading"]').forEach(loadGroupCams);
  pollValidation();
  pollCategoryProgress();
  scheduleStatsRefresh();
});

async function doLoadMore(btn) {
  const wrap  = btn.closest('.load-more');
  const group = wrap.dataset.group;
  const offset= parseInt(wrap.dataset.offset, 10);
  btn.disabled = true; btn.textContent = 'Loading…';
  try {
    const r   = await fetch('/group_html/' + encodeURIComponent(group) + '?offset=' + offset);
    const html = await r.text();
    const tmp  = document.createElement('div');
    tmp.innerHTML = html;
    const grid = wrap.closest('.cam-grid');
    wrap.remove();
    [...tmp.children].forEach(child => grid.appendChild(child));
    const sec = grid.closest('.group-section');
    syncSelectionInSection(sec);
    probeSection(sec);
    applyFiltersToSection(sec);
  } catch(e) {
    btn.disabled = false; btn.textContent = 'Retry';
    showToast('❌ Load failed: ' + e.message, true);
  }
}

// ── FFmpeg HTTP MJPEG streaming ───────────────────────────────────────────
function updateLiveFeedCounter() {
  const el=document.getElementById('statFeeds');
  const n=Object.keys(liveStreams).length + (typeof multiViewStreams !== 'undefined' ? Object.keys(multiViewStreams).length : 0);
  if(el) el.textContent='🎥 '+n+' / '+(window.__MAX_STREAMS||36)+' feeds';
  const stop=document.getElementById('headerStopFeedsBtn');
  if(stop) stop.textContent=n ? '■ Stop feeds ('+n+')' : '■ Stop feeds';
}

function toggleWatch(id, card) {
  if (liveStreams[id]) { stopCam(id); return; }
  liveStreams[id] = { ws:null, canvas:null, tid:null, connecting:true };
  updateLiveFeedCounter();
  fetchRtsp(id).then(() => tryStream(id)).catch(e => {
    delete liveStreams[id];
    updateLiveFeedCounter();
    showToast('❌ ' + e.message, true);
  });
}

function tryStream(id) {
  const st = liveStreams[id]; if (!st) return;
  const thumb = document.getElementById('thumb-' + id); if (!thumb) return;

  let img = thumb.querySelector('img.live-ffmpeg');
  if (!img) {
    img = document.createElement('img');
    img.className = 'live live-ffmpeg';
    img.alt = 'Live camera stream';
    img.style.cssText = 'display:none;width:100%;height:100%;object-fit:contain';
    thumb.appendChild(img);
  }
  st.img = img;

  let badge = thumb.querySelector('.live-badge');
  if (!badge) {
    badge = document.createElement('span');
    badge.className = 'live-badge';
    badge.textContent = '● LIVE';
    badge.style.display = 'none';
    thumb.appendChild(badge);
  }
  let stopBtn = thumb.querySelector('.stop-btn');
  if (!stopBtn) {
    stopBtn = document.createElement('button');
    stopBtn.className = 'stop-btn';
    stopBtn.textContent = '■ Stop';
    stopBtn.onclick = e => { e.stopPropagation(); stopCam(id); };
    thumb.appendChild(stopBtn);
  }
  const ph = thumb.querySelector('.placeholder');
  const sn = thumb.querySelector('.snap-preview');
  const so = thumb.querySelector('.snap-overlay');
  if (sn) sn.style.opacity = '0.25';
  if (so) so.style.display = 'none';
  if (ph) {
    ph.style.display = '';
    const t = ph.querySelector('.ph-label') || ph.querySelector('span:last-child');
    if (t) t.textContent = 'Connecting with FFmpeg…';
  }
  img.style.display = 'none';
  badge.style.display = 'none';
  stopBtn.style.display = 'block';

  const watchBtn = document.querySelector(`.cam-card[data-id="${id}"] .watch-btn`);
  if (watchBtn) { watchBtn.textContent = '■ Stop'; watchBtn.classList.remove('btn-green'); }

  // The server's /stream endpoint runs FFmpeg and returns multipart MJPEG.
  // A timestamp avoids stale browser/proxy caches. The startup timeout above is intentionally longer than one RTSP retry.
  let connected = false;
  // The server tries multiple RTSP URLs and both TCP/UDP transports.
  // A 20-second browser deadline can cancel the HTTP request before those
  // retries finish (each no-frame probe may wait up to 12 seconds).
  // Allow a bounded 60 seconds for the first decodable JPEG frame.
  st.tid = setTimeout(() => {
    if (liveStreams[id] === st && !connected) {
      showToast('❌ No video frame after 60s. Check stream_errors.log for RTSP/network/codec details.', true);
      stopCam(id);
    }
  }, 60000);

  img.onload = () => {
    if (liveStreams[id] !== st) return;
    connected = true;
    clearTimeout(st.tid);
    if (ph) ph.style.display = 'none';
    if (sn) sn.style.display = 'none';
    img.style.display = 'block';
    badge.style.display = 'block';
    updateLiveFeedCounter();
  };
  img.onerror = () => {
    if (liveStreams[id] !== st) return;
    clearTimeout(st.tid);
    showToast('❌ FFmpeg HTTP stream failed; check stream_errors.log', true);
    stopCam(id);
  };
  img.src = '/stream/' + encodeURIComponent(id) + '?t=' + Date.now();
}

function onStreamFail(id) {
  if (!liveStreams[id]) return;
  showToast('❌ Stream failed — see the diagnostic message', true);
  stopCam(id);
}

function stopCam(id) {
  const st = liveStreams[id];
  if (st) { clearTimeout(st.tid); if (st.ws) { try { st.ws.close(); } catch(_) {} } if (st.img) { st.img.onload=null; st.img.onerror=null; st.img.src=''; } }
  delete liveStreams[id];
  updateLiveFeedCounter();
  const thumb = document.getElementById('thumb-' + id); if (!thumb) return;
  thumb.querySelector('canvas.live')?.remove();
  thumb.querySelector('img.live-ffmpeg')?.remove();
  const badge  = thumb.querySelector('.live-badge');
  const stopBtn= thumb.querySelector('.stop-btn');
  const ph = thumb.querySelector('.placeholder');
  const sn = thumb.querySelector('.snap-preview');
  const so = thumb.querySelector('.snap-overlay');
  if (badge)   badge.style.display   = 'none';
  if (stopBtn) stopBtn.style.display = 'none';
  if (sn) { sn.style.display = 'block'; sn.style.opacity = '0.85'; if(so) so.style.display = ''; }
  else if (ph) { ph.style.display = ''; }
  const watchBtn = document.querySelector(`.cam-card[data-id="${id}"] .watch-btn`);
  if (watchBtn) { watchBtn.textContent = '▶ Watch'; watchBtn.classList.add('btn-green'); }
  try {
    const csrf=document.querySelector('meta[name="csrf-token"]')?.content||'';
    const fd=new FormData(); fd.append('_csrf',csrf);
    navigator.sendBeacon('/stop/' + id, fd);
  } catch(_) { fetch('/stop/' + id,{method:'POST'}).catch(()=>{}); }
}

function stopAllCams() {
  Object.keys(liveStreams).forEach(stopCam);
  if (typeof stopMultiView === 'function') stopMultiView();
  fetch('/api/stop_all', {method:'POST'}).catch(() => {});
  showToast('■ All camera feeds stopped');
}

function fullscreenCam(id) {
  const thumb = document.getElementById('thumb-' + id);
  if (!thumb) return;
  if (document.fullscreenElement) document.exitFullscreen?.();
  else thumb.requestFullscreen?.().catch(() => showToast('Fullscreen unavailable', true));
}

async function stopApplication() {
  if (!confirm('Stop the Camera Dashboard safely? Active feeds and background jobs will be stopped, then SQLite will commit/checkpoint before the application exits. The existing verified backup will be preserved.')) return;
  const btns=document.querySelectorAll('.btn-stop-app'); btns.forEach(b=>{b.disabled=true;b.textContent='⏳ Stopping…';});
  stopAllCams();
  showToast('⏳ Stopping background work and safely flushing SQLite…');
  try {
    const r = await fetch('/api/shutdown', {method:'POST', headers:{'Content-Type':'application/json'}});
    const d = await r.json().catch(() => ({ok:false, message:'Invalid shutdown response'}));
    if (d.ok) {
      showToast('✅ Shutdown started. SQLite commit/checkpoint is running; the app will exit automatically.');
    } else {
      btns.forEach(b=>{b.disabled=false;b.textContent='⏹ Stop app';});
      showToast('❌ '+(d.message || 'Safe shutdown failed'), true);
    }
  } catch (e) {
    // The process may exit before the browser receives the response. That is
    // expected for a successful Stop App operation, so do not report it as a
    // database failure.
    showToast('⏳ Shutdown signal sent. Wait for the terminal process to exit before restarting.', false);
  }
}

// v154 Background Services diagnostic panel.
(function(){
  const style=document.createElement('style');style.textContent=`#bgSvcBtn{position:fixed;right:18px;bottom:18px;z-index:9998}.bgsvc-modal{position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:9999;display:none;align-items:center;justify-content:center}.bgsvc-card{width:min(860px,94vw);max-height:82vh;overflow:auto;background:var(--panel,#171e27);border:1px solid var(--border,#39434e);border-radius:12px;padding:18px;box-shadow:0 12px 40px rgba(0,0,0,.45)}.bgsvc-row{display:grid;grid-template-columns:1fr auto auto;gap:10px;align-items:center;padding:10px 0;border-bottom:1px solid var(--border,#39434e)}.bgsvc-status{font-size:12px;font-weight:700}.bgsvc-on{color:#54d17a}.bgsvc-off{color:#ff7777}.bgsvc-btn{padding:6px 10px;border-radius:6px;border:1px solid var(--border,#506070);background:var(--button,#243447);color:inherit;cursor:pointer}.bgsvc-muted{opacity:.72;font-size:12px}`;document.head.appendChild(style);
  const b=document.getElementById('bgSvcHeaderBtn');
  const m=document.createElement('div');m.className='bgsvc-modal';m.innerHTML=`<div class="bgsvc-card"><div style="display:flex;justify-content:space-between;gap:12px;align-items:center"><h3 style="margin:0">⚙ Background Services</h3><button class="bgsvc-btn" id="bgSvcClose">Close</button></div><p class="bgsvc-muted">Diagnostic controls. OFF pauses the selected background activity cooperatively; it does not delete data. OFF preferences are saved and remain OFF after application restart until you turn them ON.</p><div id="bgSvcRows">Loading…</div><div class="bgsvc-muted" style="margin-top:12px">OFF services are automatically shown at the top so paused services are easy to review.</div></div>`;document.body.appendChild(m);
  const rows=m.querySelector('#bgSvcRows');
  async function load(){try{const r=await fetch('/api/admin/background-services');const d=await r.json();if(!r.ok)throw Error(d.detail||'Permission denied');rows.innerHTML='';Object.entries(d.services||{}).sort((a,b)=>Number(a[1].enabled)-Number(b[1].enabled)||String(a[1].label||a[0]).localeCompare(String(b[1].label||b[0]))).forEach(([name,x])=>{const row=document.createElement('div');row.className='bgsvc-row';const st=x.status||'UNKNOWN';row.innerHTML=`<div><b>${x.label}</b><div class="bgsvc-muted">${name}</div></div><span class="bgsvc-status ${x.enabled?'bgsvc-on':'bgsvc-off'}">${st}</span><button class="bgsvc-btn" data-name="${name}" data-enabled="${x.enabled?'0':'1'}">${x.enabled?'OFF':'ON'}</button>`;rows.appendChild(row);});rows.querySelectorAll('button[data-name]').forEach(btn=>btn.onclick=async()=>{btn.disabled=true;try{const name=btn.dataset.name;const enabled=btn.dataset.enabled==='1';const r=await fetch('/api/admin/background-services/'+encodeURIComponent(name),{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name="csrf-token"]')?.content||''},body:JSON.stringify({enabled})});const d=await r.json();if(!r.ok)throw Error(d.detail||'Service change failed');load();}catch(e){alert(e.message)}finally{btn.disabled=false}})}catch(e){rows.innerHTML='<div style="padding:12px">'+e.message+'</div>';}}
  window.openBackgroundServices=()=>{m.style.display='flex';load()};
  if (b) b.onclick=window.openBackgroundServices;
  m.querySelector('#bgSvcClose').onclick=()=>m.style.display='none';
  m.addEventListener('click',e=>{if(e.target===m)m.style.display='none'});
  setInterval(()=>{if(m.style.display==='flex')load()},3000);
})();


async function diagnoseCamera(id) {
  showToast('⚙ Diagnosing TCP/UDP and HTTP management ports…');
  try {
    const d = await fetch('/api/cameras/' + encodeURIComponent(id) + '/diagnose')
      .then(r => r.json());
    const parts = [];
    if (d.tcp) parts.push('TCP: ' + d.tcp);
    if (d.udp) parts.push('UDP: ' + d.udp);
    if (d.http) {
      const reachable = (d.http.reachable || []).map(x => x.port).join(', ');
      parts.push('HTTP: ' + (reachable || d.http.detail || 'none reachable'));
    }
    if (d.credentials) parts.push('credentials present');
    showToast('⚙ ' + (parts.join(' · ') || d.detail || 'No result'), !d.ok);
  } catch(e) { showToast('❌ Diagnosis failed: ' + e.message, true); }
}

// ── Live TCP probe (UI dot update) ────────────────────────────────────────────
function probeSection(sec) {
  const items = [...sec.querySelectorAll('.cam-card[data-id]')]
    .map(c => ({id: c.dataset.id}))
    .filter(x => document.querySelector(`.cam-card[data-id="${x.id}"]`)?.dataset.hasRtsp === '1');
  const BATCH = 8; let i = 0;
  function next() {
    if (i >= items.length) return;
    const batch = items.slice(i, i + BATCH); i += BATCH;
    fetch('/probe_batch', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(batch)
    }).then(r => r.json()).then(res => {
      res.forEach(({id, ok}) => {
        const dot = document.querySelector(`.cam-card[data-id="${id}"] .sdot`);
        if (dot) { dot.className = 'sdot ' + (ok ? 'online' : 'offline'); }
      });
      setTimeout(next, 400);
    }).catch(() => setTimeout(next, 2000));
  }
  setTimeout(next, 800);
}

// ── Validation progress ───────────────────────────────────────────────────────
function pollValidation() {
  fetch('/api/validate_progress').then(r => r.json()).then(d => {
    const bar  = document.getElementById('val-bar');
    const fill = document.getElementById('val-fill');
    const txt  = document.getElementById('val-txt');
    if (d.running || (d.total > 0 && d.done < d.total)) {
      bar.classList.add('show');
      const pct = d.total > 0 ? Math.round(d.done / d.total * 100) : 0;
      fill.style.width = pct + '%';
      const scope = d.scope && d.scope !== 'all' ? ' [' + d.scope + ']' : '';
      txt.textContent = 'Validating' + scope + '… ' + d.done.toLocaleString() + '/' + d.total.toLocaleString() + ' (' + pct + '%)';
      setTimeout(pollValidation, 3000);
    } else {
      if (d.total > 0) {
        fill.style.width = '100%';
        txt.textContent  = '✓ Done — ' + (d.online || 0).toLocaleString() + ' online';
        setTimeout(() => bar.classList.remove('show'), 4000);
      } else {
        bar.classList.remove('show');
      }
      // Re-enable global refresh button
      const btn = document.getElementById('globalRefreshBtn');
      if (btn) { btn.disabled = false; btn.textContent = '🔄 Refresh'; }
      // Stop any spinning refresh buttons
      document.querySelectorAll('.sec-refresh-btn.spinning').forEach(b => {
        b.classList.remove('spinning'); b.textContent = '🔄';
      });
    }
  }).catch(() => setTimeout(pollValidation, 7000));
}

// v102: lightweight client scheduler. Avoid synchronized polling bursts after page load.
window.__dashRequest = window.__dashRequest || {inflight:new Map(), last:new Map()};
window.dashboardFetchJSON = async function(url, opts, minIntervalMs){
  const now=Date.now(), key=url; const st=window.__dashRequest;
  if(st.inflight.has(key)) return st.inflight.get(key);
  const last=st.last.get(key)||0;
  if(minIntervalMs && now-last<minIntervalMs) return null;
  st.last.set(key,now);
  const p=fetch(url, opts||{}).then(async r=>{
    const d=await r.json(); if(!r.ok) throw new Error('HTTP '+r.status); return d;
  }).finally(()=>st.inflight.delete(key));
  st.inflight.set(key,p); return p;
};

// ── Stats auto-refresh ────────────────────────────────────────────────────────
function scheduleStatsRefresh() {
  _statsTimer = setInterval(refreshStats, 45000);
}
async function refreshStats() {
  try {
    const d = await window.dashboardFetchJSON('/api/stats', {cache:'no-store'}, 15000); if(!d) return;
    document.getElementById('statTotal').textContent   = d.total.toLocaleString() + ' total';
    document.getElementById('statOn').textContent      = '🟢 ' + (d.online||0).toLocaleString();
    document.getElementById('statChk').textContent     = '🟡 ' + (d.checking||0).toLocaleString();
    document.getElementById('statOff').textContent     = '🔴 ' + (d.offline||0).toLocaleString();
    document.getElementById('statUnk').textContent     = '⚪ ' + (d.unknown||0).toLocaleString();
    document.getElementById('statGroups').textContent  = (d.groups||0) + ' groups';
    const feedEl = document.getElementById('statFeeds');
    if (feedEl) feedEl.textContent = '🎥 ' + (d.active_streams||0) + ' / ' + (d.max_streams||24) + ' feeds';
    const stopEl = document.getElementById('headerStopFeedsBtn');
    if (stopEl) stopEl.textContent = (d.active_streams||0) ? '■ Stop feeds (' + d.active_streams + ')' : '■ Stop feeds';
    document.getElementById('statTime').textContent    = new Date().toLocaleTimeString();
  } catch(_) {}
}

// ── Refresh controls ──────────────────────────────────────────────────────────
async function exportUserConfig() {
  if (!confirm('Export user accounts, password hashes, application settings, API-key hashes and configured secrets (including Telegram bot tokens) to a sensitive SQLite file? Camera/device records are excluded. Store the file securely.')) return;
  try {
    const r = await fetch('/api/admin/user-config/export', {cache:'no-store'});
    if (!r.ok) { const d = await r.json().catch(()=>({})); throw Error(d.detail || 'Export failed'); }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href = url;
    a.download = 'camera_dashboard_user_config.sqlite'; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(()=>URL.revokeObjectURL(url), 30000);
    showToast('✅ User/application configuration exported. Keep the file private.');
  } catch (e) { showToast('❌ Export failed: ' + e.message, true); }
}
async function importUserConfigFile(input) {
  const file = input && input.files && input.files[0]; if (!file) return;
  if (!confirm('Import users and application settings from ' + file.name + '? Existing matching user/config entries may be updated. Camera/device data will not be imported. Only import a trusted SQLite file.')) { input.value=''; return; }
  try {
    const body = new FormData(); body.append('file', file);
    const r = await fetch('/api/admin/user-config/import', {method:'POST', body});
    const d = await r.json(); if (!r.ok || !d.ok) throw Error(d.detail || 'Import failed');
    showToast('✅ Imported users/config only — users: ' + (d.imported_users||0) + ', settings: ' + (d.imported_settings||0) + ', other config rows: ' + (d.imported_other_rows||0) + '.');
    setTimeout(()=>location.reload(), 1200);
  } catch (e) { showToast('❌ Import failed: ' + e.message, true); }
  finally { input.value=''; }
}

async function shrinkDatabase() {
  if (!confirm('Shrink the SQLite database now? Validation will pause, WAL will be checkpointed, VACUUM will reclaim unused space, and only the latest verified backup will be kept.')) return;
  const btn = [...document.querySelectorAll('header .btn')].find(b => b.textContent.includes('Shrink DB'));
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Shrinking…'; }
  try {
    const r = await fetch('/api/admin/shrink', {method:'POST'});
    let d = await r.json();
    if (!r.ok || !d.ok) throw Error(d.detail || 'Could not start database shrink');
    // /api/admin/shrink starts a background job; it does not return the final
    // byte counts. Poll the status endpoint instead of falsely reporting 0 MB.
    const started = Date.now();
    while (Date.now() - started < 60 * 60 * 1000) {
      await new Promise(resolve => setTimeout(resolve, 2500));
      const sr = await fetch('/api/admin/shrink/status', {cache:'no-store'});
      d = await sr.json();
      if (!sr.ok || !d.ok) throw Error(d.detail || 'Could not read database shrink status');
      if (d.status === 'completed' || d.status === 'failed') break;
    }
    if (d.status === 'running') throw Error('Database shrink is still running. Open System → Storage to check its status; do not start another shrink.');
    const result = d.result || {};
    if (d.status !== 'completed' || !result.ok) throw Error(result.detail || d.error || 'Database shrink failed');
    const saved = ((result.saved_bytes || 0) / 1048576).toFixed(1);
    const oldMb = ((result.old_size || 0) / 1048576).toFixed(1);
    const newMb = ((result.new_size || 0) / 1048576).toFixed(1);
    showToast(saved === '0.0'
      ? 'ℹ️ Shrink finished, but SQLite had no reclaimable pages: DB ' + oldMb + ' → ' + newMb + ' MB.'
      : '✅ DB shrink complete — saved ' + saved + ' MB (' + oldMb + ' → ' + newMb + ' MB).');
  } catch (e) {
    showToast('❌ DB shrink failed: ' + e.message, true);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '🗜️ Shrink DB'; }
  }
}

async function refreshAll() {
  const btn = document.getElementById('globalRefreshBtn');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Refreshing…'; }
  try {
    await fetch('/api/refresh/all', {method:'POST'});
    showToast('🔄 Full refresh started — checking all cameras…');
    pollValidation();
  } catch(e) {
    showToast('❌ Refresh failed: ' + e.message, true);
    if (btn) { btn.disabled = false; btn.textContent = '🔄 Refresh'; }
  }
}

async function recheckFiles() {
  const btn = [...document.querySelectorAll('header .btn')].find(b => b.textContent.includes('Recheck'));
  const originalLabel = '🔎 Recheck';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Rechecking…'; }
  try {
    const response = await fetch('/api/recheck', {method:'POST', headers:{'Accept':'application/json'}});
    let d = {};
    try { d = await response.json(); } catch (_) { d = {}; }
    if (!response.ok || d.ok === false) {
      const detail = d.detail || d.message || d.error || `Request failed (HTTP ${response.status})`;
      throw new Error(String(detail || 'Recheck could not be started'));
    }
    showToast('🔎 ' + (d.message || d.detail || 'All supported camera files queued for recheck'));
    setTimeout(() => location.reload(), 1800);
  } catch(e) {
    const message = (e && e.message) ? e.message : 'Unknown error while starting recheck';
    showToast('❌ Recheck failed: ' + message, true);
    if (btn) { btn.disabled = false; btn.textContent = originalLabel; }
  }
}

async function sendCameraList() {
  showToast('📤 Building compressed camera export…');
  try {
    const d = await fetch('/api/telegram/camera-list', {method:'POST'}).then(r => r.json());
    showTgToast(d.ok ? '✅ Camera list sent to Telegram' : '❌ ' + (d.detail || 'Telegram failed'));
  } catch(e) { showTgToast('❌ ' + e.message); }
}

async function openTelegramSettings() {
  const dlg=document.getElementById('telegram-settings-modal');
  if (!dlg) return;
  dlg.showModal();
  await refreshTelegramDestinations();
}

function clearTelegramAddFields() {
  document.getElementById('tg-sec-name').value='Telegram Destination';
  document.getElementById('tg-sec-token').value='';
  document.getElementById('tg-sec-chat').value='';
  document.getElementById('tg-sec-enabled').checked=true;
}

async function refreshTelegramDestinations() {
  const st=document.getElementById('telegram-secure-status');
  const list=document.getElementById('telegram-destination-list');
  st.textContent='⏳ Checking Telegram destinations…';
  list.innerHTML='';
  try {
    const d=await fetch('/api/telegram/secure-status').then(async r=>{const x=await r.json(); if(!r.ok) throw Error(x.detail||('HTTP '+r.status)); return x;});
    if (!d.configured) {
      st.textContent='⚪ No Telegram destinations are configured yet.';
      list.innerHTML='<div style="color:var(--muted);padding:8px">No destinations. Add one below or import your existing telegram_config.json.</div>';
      return;
    }
    const sourceText=d.encrypted ? '🔒 Encrypted SQLite' : '⚠️ Existing plaintext file';
    st.textContent=sourceText+' · '+d.count+' destination(s). Enabled destinations receive the same outgoing messages.';
    (d.destinations||[]).forEach(x=>{
      const row=document.createElement('div');
      row.style.cssText='display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 10px;border:1px solid var(--border);border-radius:7px;background:var(--bg)';
      const state=x.enabled ? '🟢 Enabled' : '⚪ Disabled';
      row.innerHTML='<div><strong>'+escapeHtml(x.name||('Telegram '+(x.index+1)))+'</strong><div style="font-size:11px;color:var(--muted)">'+state+' · credentials hidden</div></div>' +
        '<button type="button" class="btn" style="color:#ff7b72" onclick="removeTelegramDestination('+Number(x.index)+')">Remove</button>';
      list.appendChild(row);
    });
  } catch(e) {
    st.textContent='❌ '+e.message;
    list.innerHTML='<div style="color:#ff7b72">Could not load Telegram destinations.</div>';
  }
}

async function addTelegramDestination(e) {
  e.preventDefault();
  const st=document.getElementById('telegram-secure-status');
  st.textContent='⏳ Encrypting and adding destination…';
  const body={action:'add',name:document.getElementById('tg-sec-name').value.trim(),bot_token:document.getElementById('tg-sec-token').value,chat_id:document.getElementById('tg-sec-chat').value,enabled:document.getElementById('tg-sec-enabled').checked};
  try {
    const r=await fetch('/api/telegram/secure-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d=await r.json(); if(!r.ok||!d.ok) throw Error(d.detail||('HTTP '+r.status));
    showTgToast('🔒 Destination added and encrypted');
    clearTelegramAddFields();
    await refreshTelegramDestinations();
  } catch(e) { st.textContent='❌ '+e.message; }
}

async function removeTelegramDestination(index) {
  if (!confirm('Remove this Telegram destination? Its encrypted Bot Token and Chat ID will be removed from the database.')) return;
  const st=document.getElementById('telegram-secure-status'); st.textContent='⏳ Removing destination…';
  try {
    const r=await fetch('/api/telegram/secure-config',{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({index:index})});
    const d=await r.json(); if(!r.ok||!d.ok) throw Error(d.detail||('HTTP '+r.status));
    showTgToast('🗑 Destination removed');
    await refreshTelegramDestinations();
  } catch(e) { st.textContent='❌ '+e.message; }
}

async function importTelegramUpload(input) {
  const file=input && input.files && input.files[0];
  if (!file) return;
  const st=document.getElementById('telegram-secure-status');
  if (!/\.json$/i.test(file.name) && file.type!=='application/json') {
    st.textContent='❌ Please select a .json Telegram configuration file.';
    input.value=''; return;
  }
  if (file.size > 1024*1024) {
    st.textContent='❌ File is too large. Maximum size is 1 MB.';
    input.value=''; return;
  }
  if (!confirm('Import valid Telegram destinations from '+file.name+' and encrypt them in this deployment? Existing encrypted destinations will be replaced.')) {
    input.value=''; return;
  }
  st.textContent='⏳ Uploading JSON and encrypting Telegram credentials…';
  try {
    const fd=new FormData(); fd.append('file',file,file.name);
    const r=await fetch('/api/telegram/import-upload',{method:'POST',body:fd});
    const d=await r.json();
    if(!r.ok||!d.ok) throw Error(d.detail||('HTTP '+r.status));
    showTgToast('🔒 '+d.imported+' destination(s) imported and encrypted');
    input.value='';
    await refreshTelegramDestinations();
  } catch(e) {
    st.textContent='❌ '+e.message;
    input.value='';
  }
}

async function importTelegramFile() {
  const st=document.getElementById('telegram-secure-status');
  if (!confirm('Import ALL valid destinations from telegram_config.json into encrypted SQLite storage? The plaintext file will NOT be deleted.')) return;
  st.textContent='⏳ Importing and encrypting all destinations…';
  try {
    const r=await fetch('/api/telegram/import-file',{method:'POST'});
    const d=await r.json();
    if(!r.ok||!d.ok) throw Error(d.detail||('HTTP '+r.status));
    showTgToast('🔒 '+d.imported+' destination(s) imported and encrypted');
    await refreshTelegramDestinations();
  } catch(e) { st.textContent='❌ '+e.message; }
}

async function deleteTelegramSettings() {
  if (!confirm('Remove ALL encrypted Telegram destinations from the database?')) return;
  const st=document.getElementById('telegram-secure-status'); st.textContent='⏳ Removing all…';
  try {
    const r=await fetch('/api/telegram/secure-config',{method:'DELETE'});
    const d=await r.json(); if(!r.ok||!d.ok) throw Error(d.detail||('HTTP '+r.status));
    showTgToast('🗑 All Telegram destinations removed');
    await refreshTelegramDestinations();
  } catch(e) { st.textContent='❌ '+e.message; }
}

async function testTelegram() {
  showTgToast('🧪 Checking Telegram configuration…');
  try {
    const s = await fetch('/api/telegram/status').then(async r => { const d=await r.json(); if(!r.ok) throw Error(d.detail||('HTTP '+r.status)); return d; });
    if(!s.ok){ const detail=s.detail || (s.destinations||[]).map(x => x.name+': '+(x.detail||'failed')).join(' | ') || 'Telegram configuration/API check failed'; showTgToast('❌ '+detail); return; }
    const d = await fetch('/api/telegram/test', {method:'POST'}).then(async r => { const x=await r.json(); if(!r.ok) throw Error(x.detail||('HTTP '+r.status)); return x; });
    showTgToast(d.ok ? '✅ Telegram test message sent' : '❌ ' + (d.detail || 'Telegram failed'));
  } catch(e) { showTgToast('❌ ' + e.message); }
}

async function refreshGroup(e, btn) {
  e.stopPropagation();
  const group = btn.dataset.group;
  btn.classList.add('spinning');
  try {
    await fetch('/api/refresh/group/' + encodeURIComponent(group), {method:'POST'});
    showToast('🔄 Refreshing "' + group + '"…');
    pollValidation();
    // After a moment, reload the section to show 'checking' badges
    const sec = btn.closest('.group-section');
    if (sec && sec.dataset.loaded === 'true') {
      setTimeout(() => loadGroupCams(sec), 1500);
    }
  } catch(ex) {
    btn.classList.remove('spinning');
    showToast('❌ Refresh failed: ' + ex.message, true);
  }
}

// ── Filters ───────────────────────────────────────────────────────────────────
let _searchTimer = null;
let _searchSerial = 0;

function onSearch() {
  clearTimeout(_searchTimer);
  const q = (document.getElementById('searchBox').value || '').trim();
  if (!q) {
    document.getElementById('server-search-results').classList.remove('active');
    applyFiltersLocal();
    return;
  }
  _searchTimer = setTimeout(serverSearch, 250);
}

async function serverSearch() {
  const serial = ++_searchSerial;
  const q = (document.getElementById('searchBox').value || '').trim();
  if (!q) return;
  const status = document.getElementById('statusFilter').value;
  const favsOnly = document.getElementById('favsToggle').classList.contains('active');
  const group = document.getElementById('groupFilter').value;
  const box = document.getElementById('server-search-results');
  const grid = document.getElementById('search-result-grid');
  try {
    const params = new URLSearchParams({
      q, status, group, favorites: favsOnly ? '1' : '0', limit: '200'
    });
    const r = await fetch('/search_html?' + params.toString());
    if (!r.ok) throw new Error('HTTP ' + r.status);
    if (serial !== _searchSerial) return;
    grid.innerHTML = await r.text();
    const count = grid.querySelectorAll('.cam-card').length;
    document.getElementById('search-result-count').textContent = count;
    box.classList.add('active');
    document.querySelectorAll('main > .group-section:not(#server-search-results)')
      .forEach(sec => sec.classList.add('section-hidden'));
    document.getElementById('search-empty').style.display = count ? 'none' : '';
  } catch (e) {
    showToast('❌ Search failed: ' + e.message, true);
  }
}

function applyFilters() {
  if ((document.getElementById('searchBox').value || '').trim()) {
    clearTimeout(_searchTimer);
    _searchTimer = setTimeout(serverSearch, 0);
    return;
  }
  applyFiltersLocal();
}

function applyFiltersLocal() {
  const q        = (document.getElementById('searchBox').value || '').toLowerCase().trim();
  const status   = document.getElementById('statusFilter').value;
  const favsOnly = document.getElementById('favsToggle').classList.contains('active');
  const selGroup = document.getElementById('groupFilter').value;
  document.getElementById('server-search-results').classList.remove('active');
  let   anyVis   = false;
  document.querySelectorAll('.group-section').forEach(sec => {
    const grp = sec.dataset.group || '';
    if (selGroup !== 'all' && grp !== selGroup) { sec.classList.add('section-hidden'); return; }
    sec.classList.remove('section-hidden');
    if (sec.dataset.loaded === 'true') {
      let secVis = false;
      sec.querySelectorAll('.cam-card').forEach(card => {
        const show = (!q      || card.dataset.search.includes(q))
                  && (status === 'all' || card.dataset.status === status)
                  && (!favsOnly || card.dataset.favorite === '1');
        card.classList.toggle('hidden', !show);
        if (show) secVis = true;
      });
      sec.classList.toggle('section-hidden', !secVis);
      if (secVis && (q || status !== 'all' || favsOnly)) sec.classList.add('expanded');
      if (secVis) anyVis = true;
    } else {
      anyVis = true;
      if (q || status !== 'all' || favsOnly) sec.classList.add('expanded');
    }
  });
  document.getElementById('search-empty').style.display = anyVis ? 'none' : '';
}

function applyFiltersToSection(sec) {
  const q       = (document.getElementById('searchBox').value || '').toLowerCase().trim();
  const status  = document.getElementById('statusFilter').value;
  const favsOnly= document.getElementById('favsToggle').classList.contains('active');
  sec.querySelectorAll('.cam-card').forEach(card => {
    const show = (!q      || card.dataset.search.includes(q))
              && (status === 'all' || card.dataset.status === status)
              && (!favsOnly || card.dataset.favorite === '1');
    card.classList.toggle('hidden', !show);
  });
}

function toggleFavs() { document.getElementById('favsToggle').classList.toggle('active'); applyFilters(); }

// ── Camera actions ────────────────────────────────────────────────────────────
const selectedCameraSet = new Set();

function updateSelectionUI() {
  const el = document.getElementById('selection-counter');
  if (el) el.textContent = selectedCameraSet.size + ' selected';
}
function clearSelection() {
  selectedCameraSet.clear();
  document.querySelectorAll('[data-select-id]').forEach(cb => cb.checked = false);
  document.querySelectorAll('.category-select-all-btn').forEach(btn => { btn.dataset.selected='0'; btn.textContent='☑'; btn.title='Select all cameras in this category'; });
  updateSelectionUI();
}
function invertVisibleSelection() {
  document.querySelectorAll('[data-select-id]').forEach(cb => {
    const id=String(cb.dataset.selectId||''); if(!id)return;
    if(selectedCameraSet.has(id))selectedCameraSet.delete(id);else selectedCameraSet.add(id);
    cb.checked=selectedCameraSet.has(id);
  });
  updateSelectionUI();
}

function syncSelectionInSection(sec) {
  if (!sec) return;
  sec.querySelectorAll('[data-select-id]').forEach(cb => {
    const id = String(cb.dataset.selectId || '');
    cb.checked = !!id && selectedCameraSet.has(id);
  });
}

document.addEventListener('change', e => {
  const cb = e.target.closest('[data-select-id]');
  if (!cb) return;
  const id = String(cb.dataset.selectId || '');
  if (!id) return;
  if (cb.checked) selectedCameraSet.add(id);
  else selectedCameraSet.delete(id);
  updateSelectionUI();
});

function selectedCameraIds() {
  document.querySelectorAll('[data-select-id]:checked').forEach(el => {
    const id = String(el.dataset.selectId || '');
    if (id) selectedCameraSet.add(id);
  });
  updateSelectionUI();
  return [...selectedCameraSet];
}

async function selectAllCategory(e, btn) {
  e.stopPropagation();
  const group = btn?.dataset.group || '';
  if (!group) return;
  const sec = btn.closest('.group-section');
  try {
    btn.disabled = true;
    const selecting = btn.dataset.selected !== '1';
    const r = await fetch('/api/category/camera-ids?group=' + encodeURIComponent(group));
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.detail || 'Could not load category cameras');
    const ids = (d.ids || []).map(String).filter(Boolean);
    if (selecting) {
      ids.forEach(id => selectedCameraSet.add(id));
      btn.dataset.selected = '1';
      btn.textContent = '☑✓';
      btn.title = 'Unselect all cameras in this category';
      showToast('✅ Selected all ' + ids.length + ' camera(s) in "' + group + '"');
    } else {
      ids.forEach(id => selectedCameraSet.delete(id));
      btn.dataset.selected = '0';
      btn.textContent = '☑';
      btn.title = 'Select all cameras in this category';
      showToast('Cleared selection for "' + group + '"');
    }
    syncSelectionInSection(sec);
    updateSelectionUI();
  } catch (err) {
    showToast('❌ ' + err.message, true);
  } finally {
    btn.disabled = false;
  }
}
async function categoryAction(action, btn) {
  const group=btn?.dataset.group||''; if(!group)return;
  const label=action==='recheck'?'Recheck':'Favorite';
  if(!confirm(label+' every camera in "'+group+'"?'))return;
  try {
    btn.disabled=true;
    const d=await fetch('/api/category/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({group})}).then(r=>r.json());
    if(!d.ok)throw new Error(d.detail||'Category action failed');
    showToast('✅ '+(d.message||label+' completed'));
  } catch(e){showToast('❌ '+e.message,true)} finally{btn.disabled=false}
}
function categoryRecheck(e,btn){e.stopPropagation();return categoryAction('recheck',btn)}
function categoryFavorite(e,btn){e.stopPropagation();return categoryAction('favorite',btn)}
async function categoryOfflineRecheck(e,btn){
  e.stopPropagation();
  const group=btn?.dataset.group||''; if(!group)return;
  if(!confirm('Recheck only offline/unknown cameras in +group+?')) return;
  try {
    btn.disabled=true; btn.classList.add('spinning');
    const d=await fetch('/api/category/offline-recheck',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({group})}).then(r=>r.json());
    if(!d.ok) throw new Error(d.detail||'Offline recheck failed');
    showToast('⚡ '+(d.message||'Offline recheck queued'));
    pollValidation();
  } catch(e){showToast('❌ '+e.message,true)} finally{btn.disabled=false;btn.classList.remove('spinning')}
}

async function bulkAction(action) {
  const ids = selectedCameraIds();
  if (!ids.length) {
    showToast('Select at least one camera first', true);
    return;
  }
  if (action === 'delete'
      && !confirm('Remove ' + ids.length + ' selected cameras from the dashboard?')) return;
  try {
    const endpoint = '/api/bulk/' + action;
    const d = await fetch(endpoint, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ids})
    }).then(r => r.json());
    if (!d.ok) throw new Error(d.detail || 'Bulk action failed');
    if (action === 'export') {
      const blob = new Blob([JSON.stringify(d.cameras || [], null, 2)],
                            {type: 'application/json'});
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'camera-selection.json';
      a.click();
      URL.revokeObjectURL(a.href);
    } else {
      showToast('✅ ' + (d.message || (action + ' completed')));
      setTimeout(() => location.reload(), 700);
    }
  } catch (e) {
    showToast('❌ ' + e.message, true);
  }
}

function getAvailableCategories() {
  const select = document.getElementById('groupFilter');
  if (!select) return [];
  return [...select.options]
    .filter(o => o.value && o.value !== 'all')
    .map(o => ({value: o.value, label: o.textContent.trim()}));
}

function openTransferModal(ids, preferredTarget = '') {
  if (!ids?.length) { showToast('Select at least one camera first', true); return; }
  const modal = document.getElementById('transfer-modal');
  const target = document.getElementById('transfer-target');
  const count = document.getElementById('transfer-count');
  if (!modal || !target) return;

  const categories = getAvailableCategories();
  target.innerHTML = '';
  categories.forEach(c => {
    const opt = document.createElement('option');
    opt.value = c.value;
    opt.textContent = c.label;
    target.appendChild(opt);
  });

  if (!categories.length) {
    showToast('No destination categories available', true);
    return;
  }

  count.textContent = 'Transfer ' + ids.length + ' selected camera(s) to an existing category.';
  const wanted = (preferredTarget || '').trim();
  if (wanted && categories.some(c => c.value === wanted)) target.value = wanted;
  else if (target.options.length) target.selectedIndex = 0;

  modal.dataset.ids = JSON.stringify(ids);
  modal.showModal();
}

function transferSelectedToPrompt() {
  const ids = selectedCameraIds();
  if (!ids.length) { showToast('Select at least one camera first', true); return; }
  openTransferModal(ids);
}

function transferSelectedToGroup(e, btn) {
  e.stopPropagation();
  const ids = selectedCameraIds();
  if (!ids.length) { showToast('Select cameras first, then choose 📥 on the target category', true); return; }
  const target = btn.dataset.group || '';
  if (!target) return;
  openTransferModal(ids, target);
}

async function confirmTransferSelection() {
  const modal = document.getElementById('transfer-modal');
  const target = document.getElementById('transfer-target')?.value || '';
  let ids = [];
  try { ids = JSON.parse(modal?.dataset.ids || '[]'); } catch (_) {}
  if (!ids.length) { showToast('No cameras selected', true); return; }
  if (!target) { showToast('Select a destination category', true); return; }
  if (!confirm('Transfer ' + ids.length + ' selected camera(s) to "' + target + '"?')) return;
  modal?.close();
  await transferCameras(ids, target);
}

async function transferCameras(ids, target) {
  target = (target || '').trim();
  if (!target) { showToast('❌ Target category is required', true); return; }
  try {
    const d = await fetch('/api/category/transfer', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ids, target_group: target})
    }).then(r => r.json());
    if (!d.ok) throw new Error(d.detail || 'Transfer failed');
    showToast('✅ Transferred ' + d.count + ' camera(s) to "' + target + '"');
    setTimeout(() => location.reload(), 500);
  } catch (e) { showToast('❌ ' + e.message, true); }
}

async function deleteGroup(e, btn) {
  e.stopPropagation();
  const group = btn.dataset.group || '';
  if (!group) return;
  const sec = btn.closest('.group-section');
  const count = sec?.querySelector('.badge')?.textContent?.trim() || '';
  const msg = 'Delete the entire category "' + group + '"' + (count ? ' (' + count + ' cameras)' : '') + '? This removes all cameras in this category from the dashboard.';
  if (!confirm(msg)) return;
  try {
    const d = await fetch('/api/category/delete', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({group: group})
    }).then(r => r.json());
    if (!d.ok) throw new Error(d.detail || 'Category delete failed');
    showToast('🗑 Deleted category "' + group + '" (' + d.count + ' cameras)');
    setTimeout(() => location.reload(), 600);
  } catch (e) { showToast('❌ ' + e.message, true); }
}

async function doFavorite(id, btn) {
  try {
    const d = await fetch('/favorite/' + id, {method:'POST'}).then(r => r.json());
    if (!d.ok) return;
    btn.classList.toggle('active', d.favorite);
    const card = btn.closest('.cam-card');
    if (card) card.dataset.favorite = d.favorite ? '1' : '0';
    showToast(d.favorite ? '★ Favorited' : '☆ Removed from favorites');
  } catch(e) { showToast('❌ Failed', true); }
}

async function doDelete(id, card) {
  if (!confirm('Remove this camera from the dashboard?')) return;
  try {
    const d = await fetch('/delete/' + id, {method:'POST'}).then(r => r.json());
    if (d.ok) { card?.remove(); showToast('🗑 Camera removed'); }
  } catch(e) { showToast('❌ Failed', true); }
}

function copyRTSP(rtsp) {
  if (!rtsp) { showToast('No RTSP URL', true); return; }
  navigator.clipboard.writeText(rtsp)
    .then(() => showToast('📋 RTSP copied!'))
    .catch(() => {
      const t = document.createElement('textarea');
      t.value = rtsp; document.body.appendChild(t);
      t.select(); document.execCommand('copy'); document.body.removeChild(t);
      showToast('📋 RTSP copied!');
    });
}

function openVLC(rtsp) {
  if (!rtsp) { showToast('No RTSP URL', true); return; }
  // Try to open directly in VLC via vlc:// protocol, fall back to copy
  const link = document.createElement('a');
  link.href = 'vlc://' + rtsp.replace(/^rtsp:\/\//i, '');
  link.click();
  copyRTSP(rtsp);
  showToast('📺 Opening in VLC (URL also copied)');
}

function takeSnapshot(id) {
  showToast('📷 Capturing snapshot…');
  const a = document.createElement('a'); a.href = '/snapshot/' + encodeURIComponent(id);
  a.download = ''; document.body.appendChild(a); a.click(); document.body.removeChild(a);
  pollSnapshotPath(id);
}

async function pollSnapshotPath(id){const started=Date.now();while(Date.now()-started<15000){try{const cam=await fetch('/api/cameras/'+encodeURIComponent(id),{cache:'no-store'}).then(r=>r.json());if(cam.snapshot_path){await refreshCardSnap(id);return;}}catch(_){}await new Promise(r=>setTimeout(r,700));}}

async function refreshCardSnap(id) {
  try {
    const cam = await fetch('/api/cameras/' + id).then(r => r.json());
    if (!cam.snapshot_path) return;
    const thumb = document.getElementById('thumb-' + id); if (!thumb) return;
    let img = thumb.querySelector('.snap-preview');
    if (!img) {
      img = document.createElement('img'); img.className = 'snap-preview'; img.alt = 'snap';
      img.onerror = () => img.style.display = 'none';
      thumb.insertBefore(img, thumb.firstChild);
    }
    img.src = '/snapshots/' + cam.snapshot_path + '?t=' + Date.now();
    img.style.display = 'block'; img.style.opacity = '0.85';
    showToast('📷 Snapshot saved!');
  } catch(_) {}
}

async function sendTelegram(id) {
  try {
    const d = await fetch('/send_rtsp/' + id).then(r => r.json());
    showTgToast(d.ok ? '✅ Sent to Telegram!' : '❌ ' + (d.detail || 'Failed'));
  } catch(e) { showTgToast('❌ ' + e.message); }
}

// ── Modals ────────────────────────────────────────────────────────────────────
function openAddModal()  { document.getElementById('add-modal').showModal(); }
function openM3uModal()  { document.getElementById('m3u-modal').showModal(); }
function openDeviceModal() { document.getElementById('device-modal').showModal(); }
function closeDeviceDialog(id) {
  const dialog = document.getElementById(id);
  if (dialog?.open) dialog.close();
}
function toggleDevicePortMode() {
  const mode = document.getElementById('device-port-mode')?.value;
  const input = document.getElementById('device-port');
  if (input) input.disabled = mode !== 'manual';
}
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  const open = [...document.querySelectorAll('dialog[open]')].pop();
  if (open) open.close();
});
document.addEventListener('click', e => {
  if (e.target?.tagName === 'DIALOG' && e.target.open) e.target.close();
});

async function discoverDevice(e) {
  e.preventDefault();
  const progress = document.getElementById('device-progress');
  const button = document.getElementById('device-submit');
  button.disabled = true; progress.textContent = '⏳ Checking all supported ports for TCP + HTTP/HTTPS, then reading device channels…';
  const payload = {
    host: document.getElementById('device-host').value.trim(),
    port: document.getElementById('device-port-mode').value === 'auto'
      ? 'auto' : document.getElementById('device-port').value,
    rtsp_port: document.getElementById('device-rtsp-port').value,
    vendor: document.getElementById('device-vendor').value,
    username: document.getElementById('device-user').value,
    password: document.getElementById('device-pass').value,
    enrich_geo: document.getElementById('device-geo').checked
  };
  try {
    const r = await fetch('/api/device/discover', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(payload)
    });
    const ct = (r.headers.get('content-type') || '').toLowerCase();
    let d;
    if (ct.includes('application/json')) d = await r.json();
    else {
      const body = await r.text();
      throw Error('HTTP ' + r.status + ': ' + (body.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 300) || 'non-JSON server response'));
    }
    if (!r.ok || !d.ok) throw Error((d.category ? '[' + d.category + '] ' : '') + (d.detail || 'Device discovery failed'));
    document.getElementById('device-modal').close();
    showToast('✅ ' + d.vendor + ' device imported: ' + d.count + ' channels');
    setTimeout(() => location.reload(), 1200);
  } catch (err) {
    progress.textContent = '❌ ' + err.message;
  } finally {
    button.disabled = false;
  }
}

async function importDeviceInventory() {
  const input = document.getElementById('device-inventory-file');
  const progress = document.getElementById('device-inventory-progress');
  const button = [...document.querySelectorAll('#device-modal .btn')]
    .find(b => b.textContent.includes('Import device file'));
  const file = input.files[0];
  if (!file) { progress.textContent = 'Choose a .txt or .csv file first'; return; }
  const fd = new FormData(); fd.append('file', file);
  if (button) button.disabled = true;
  progress.textContent = '⏳ Uploading inventory and starting background import…';

  const readResponse = async (r) => {
    const ct = (r.headers.get('content-type') || '').toLowerCase();
    if (ct.includes('application/json')) return await r.json();
    const body = await r.text();
    const clean = body.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 350);
    throw Error(`HTTP ${r.status}: ${clean || 'empty response from server'}`);
  };

  try {
    const r = await fetch('/api/device/inventory-import', {method:'POST', body: fd});
    const d = await readResponse(r);
    if (!r.ok || !d.ok) throw Error(d?.detail || `Inventory queue failed (HTTP ${r.status})`);
    const jobId = d.job_id;
    if (!jobId) throw Error('Server accepted the upload but returned no job ID');

    progress.textContent = `✅ Queued ${d.target_count} device(s).\nWorkers: ${d.workers}\nStarting…`;

    let finished = false;
    while (!finished) {
      await new Promise(resolve => setTimeout(resolve, 1200));
      const sr = await fetch('/api/device/inventory-import/status/' + encodeURIComponent(jobId),
                             {cache:'no-store'});
      const status = await readResponse(sr);
      if (!sr.ok || !status.ok) throw Error(status?.detail || `Status request failed (HTTP ${sr.status})`);
      const total = Number(status.total || 0);
      const processed = Number(status.processed || 0);
      const imported = Number(status.imported || 0);
      const failed = Number(status.failed || 0);
      const pct = total ? Math.round((processed / total) * 100) : 0;
      progress.textContent =
        `⏳ Importing… ${processed}/${total} (${pct}%)\n` +
        `Imported: ${imported} | Failed: ${failed}`;

      finished = ['completed','failed','cancelled'].includes(status.status);
      if (finished) {
        const errors = status.errors || [];
        const lines = [
          `Processed: ${total} listed device(s).`,
          `Imported: ${imported}.`,
          `Failed: ${failed}.`
        ];
        if (errors.length) {
          errors.slice(0, 12).forEach(item => lines.push(
            '• ' + (item.host || 'unknown host') + ' [' +
            (item.category || 'device_request_failed') + ']: ' +
            (item.detail || 'request failed')));
          if (errors.length > 12) lines.push(`…and ${errors.length - 12} more.`);
        }
        if (status.error) lines.push('Job error: ' + status.error);
        progress.textContent = lines.join('\n');
        showToast(imported
          ? `✅ Inventory complete — ${imported} imported, ${failed} failed`
          : `❌ Inventory complete — ${failed} failed`, !imported);
        if (imported) setTimeout(() => location.reload(), 1800);
      }
    }
  } catch (err) {
    progress.textContent = '❌ ' + (err?.message || err);
  } finally {
    if (button) button.disabled = false;
  }
}

let activeIntelCameraId = '';
async function openDeviceIntel(id) {
  activeIntelCameraId = String(id || '');
  const modal = document.getElementById('device-intel-modal');
  document.getElementById('device-intel-status').textContent = 'Ready. Choose a section; device requests run only after you press a button.';
  document.getElementById('device-intel-result').textContent = '';
  modal.showModal();
}
async function runDeviceIntel(section) {
  const status = document.getElementById('device-intel-status');
  const out = document.getElementById('device-intel-result');
  if (!activeIntelCameraId) { status.textContent = 'No camera selected.'; return; }
  status.textContent = 'Requesting ' + section + ' from the selected device…';
  out.textContent = '';
  try {
    const r = await fetch('/api/cameras/' + encodeURIComponent(activeIntelCameraId) + '/intelligence/' + encodeURIComponent(section), {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const d = await r.json();
    if (!r.ok || !d.ok) throw Error(d.detail || ('HTTP ' + r.status));
    status.textContent = (d.available ? 'Completed' : 'Partially available') + ' · ' + (d.vendor || 'unknown vendor') + ' · ' + (d.host || 'unknown host') + (d.note ? '\n' + d.note : '');
    out.textContent = JSON.stringify(d.data || d, null, 2);
  } catch (e) { status.textContent = 'Request failed: ' + (e?.message || e); }
}
async function lookupCameraLocation(cameraId) {
  const id = cameraId || activeIntelCameraId;
  if (!id) return;
  const card = document.querySelector('.cam-card[data-id="' + CSS.escape(id) + '"]');
  const button = card?.querySelector('[data-action="geo-lookup"]');
  if (button) { button.disabled = true; button.dataset.originalText = button.textContent; button.textContent = '🌍 Looking…'; }
  try {
    const r = await fetch('/api/cameras/' + encodeURIComponent(id) + '/geo-update', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({mode:'lookup'})
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw Error(d.detail || ('HTTP ' + r.status));
    if (d.updated) {
      showToast('📍 Location saved: ' + [d.geo?.city, d.geo?.region, d.geo?.country].filter(Boolean).join(', '));
      setTimeout(() => location.reload(), 900);
    } else {
      showToast('Location not updated: ' + (d.detail || 'No usable public-IP location returned.'), true);
    }
    if (activeIntelCameraId === id) {
      const status = document.getElementById('device-intel-status');
      const out = document.getElementById('device-intel-result');
      if (status) status.textContent = d.updated ? 'Location saved. IP geolocation is approximate, not GPS.' : (d.detail || 'No reliable location returned; use Edit / Save Location.');
      if (out) out.textContent = JSON.stringify(d, null, 2);
    }
  } catch (e) { showToast('Location lookup failed: ' + (e?.message || e), true); }
  finally { if (button) { button.disabled = false; button.textContent = button.dataset.originalText || '🌍 Lookup Location'; } }
}
async function lookupDeviceLocation() { return lookupCameraLocation(activeIntelCameraId); }
async function editDeviceLocation() {
  if (!activeIntelCameraId) return;
  const card = document.querySelector('.cam-card[data-id="' + CSS.escape(activeIntelCameraId) + '"]');
  const label = card?.querySelector('.cam-ip')?.textContent?.trim() || activeIntelCameraId;
  const country = prompt('Country (leave blank to keep current):', ''); if (country === null) return;
  const region = prompt('Region / State / Province (blank keeps current):', ''); if (region === null) return;
  const city = prompt('City (blank keeps current):', ''); if (city === null) return;
  const area = prompt('Area / district / neighborhood (blank keeps current):', ''); if (area === null) return;
  const description = prompt('Camera/device description (blank keeps current):', label); if (description === null) return;
  const latText = prompt('Latitude (optional; blank to keep current):', ''); if (latText === null) return;
  const lonText = prompt('Longitude (optional; blank to keep current):', ''); if (lonText === null) return;
  const payload = {mode:'manual',country,region,city,area,description,latitude:latText,longitude:lonText};
  const status = document.getElementById('device-intel-status');
  const out = document.getElementById('device-intel-result');
  status.textContent = 'Saving location and description…';
  try {
    const r = await fetch('/api/cameras/' + encodeURIComponent(activeIntelCameraId) + '/geo-update', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const d = await r.json();
    if (!r.ok || !d.ok) throw Error(d.detail || ('HTTP ' + r.status));
    status.textContent = 'Location/description saved to the selected camera/device records.';
    out.textContent = JSON.stringify(d, null, 2);
    setTimeout(() => location.reload(), 900);
  } catch (e) { status.textContent = 'Save failed: ' + (e?.message || e); }
}

async function openDeviceDetails(id) {
  const card = document.querySelector(`.cam-card[data-id="${id}"]`);
  const deviceId = card?.dataset.deviceId;
  if (!deviceId) return loginToCamera(id);
  const modal = document.getElementById('device-details-modal');
  const pre = document.getElementById('device-details');
  const cfg = document.getElementById('device-config-btn');
  pre.textContent = 'Loading…'; modal.showModal();
  try {
    const d = await fetch('/api/device/' + encodeURIComponent(deviceId)).then(r => r.json());
    if (!d.ok) throw Error(d.detail || 'Device not found');
    pre.textContent = JSON.stringify(d, null, 2);
    cfg.onclick = async () => {
      cfg.disabled = true;
      try {
        const r = await fetch('/api/device/' + encodeURIComponent(deviceId) + '/config',
                              {method:'POST'});
        if (!r.ok) throw Error((await r.json()).detail || 'Config download failed');
        const blob = await r.blob();
        const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
        a.download = 'device-' + deviceId + '-config.bin'; a.click();
        URL.revokeObjectURL(a.href);
      } catch (err) { showToast('❌ ' + err.message, true); }
      finally { cfg.disabled = false; }
    };
  } catch (err) {
    pre.textContent = '❌ ' + err.message;
  }
}

async function detectCameraFeed(id) {
  const modal = document.getElementById('device-details-modal');
  const pre = document.getElementById('device-details');
  const cfg = document.getElementById('device-config-btn');
  cfg.disabled = true; cfg.textContent = 'Detecting…';
  pre.textContent = '📡 Detecting video feed…\nChecking every bounded RTSP + HTTP/HTTPS service with the stored credentials.\nThis may take a few seconds.';
  modal.showModal();
  try {
    const r = await fetch('/api/cameras/' + encodeURIComponent(id) + '/detect-feed', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'
    });
    const ct = (r.headers.get('content-type') || '').toLowerCase();
    const d = ct.includes('application/json') ? await r.json() : {ok:false, detail:'HTTP '+r.status+' '+(await r.text()).slice(0,240)};
    if (!r.ok || !d.ok) throw Error((d.category ? '['+d.category+'] ' : '') + (d.detail || 'No working video feed found'));
    pre.textContent = [
      '📡 VIDEO FEED DETECTED',
      'Vendor: ' + (d.vendor || 'generic'),
      'Feed type: ' + (d.feed_type || 'RTSP') + (d.http_port ? '\nHTTP port: ' + d.http_port : '') + (d.port ? '\nRTSP port: ' + d.port : ''),
      'Codec: ' + (d.codec || 'unknown'),
      'Resolution: ' + ((d.width && d.height) ? d.width + 'x' + d.height : 'unknown'),
      'FPS: ' + (d.fps || 'unknown'),
      'Feed: ' + (d.safe_url || 'detected'),
      'Reachable RTSP ports: ' + ((d.reachable_ports || []).join(', ') || 'none'),
      'Candidates tested: ' + (d.tried || 0),
      '',
      '✅ Feed saved to this camera. Press Watch to play it.'
    ].join('\n');
    cfg.disabled = false; cfg.textContent = '▶ Watch detected feed';
    cfg.onclick = () => { modal.close(); setTimeout(() => toggleWatch(id, document.querySelector('.cam-card[data-id="' + id + '"]')), 100); };
    showToast('✅ Video feed detected and saved');
    const card = document.querySelector('.cam-card[data-id="' + id + '"]');
    if (card) {
      const box = card.querySelector('.rtsp-box');
      if (box) box.textContent = d.safe_url || box.textContent;
    }
  } catch(err) {
    pre.textContent = '❌ ' + (err?.message || err) + '\n\nMake sure the stored username/password belong to the door camera and that its RTSP service is enabled.';
    showToast('❌ Video feed not detected', true);
  } finally {
    if (cfg.textContent === 'Detecting…') { cfg.disabled = true; cfg.textContent = 'No config action'; }
  }
}

async function loginToCamera(id) {
  const category = prompt('Optional: enter a category to save this CCTV device (blank = test only):', '');
  const modal = document.getElementById('device-details-modal');
  const pre = document.getElementById('device-details');
  const cfg = document.getElementById('device-config-btn');
  cfg.disabled = true; cfg.textContent = 'Checking…';
  pre.textContent = '🔐 Login + HTTP port test + RTSP port test + device information + approximate geolocation…';
  modal.showModal();
  try {
    const r = await fetch('/api/cameras/' + encodeURIComponent(id) + '/device-check', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({group:category})});
    const ct = (r.headers.get('content-type') || '').toLowerCase();
    const d = ct.includes('application/json') ? await r.json() : {ok:false, detail:'HTTP '+r.status+' '+(await r.text()).slice(0,240)};
    if (!r.ok || !d.ok) throw Error((d.category ? '['+d.category+'] ' : '') + (d.detail || 'Device check failed'));
    const rt=d.rtsp||{};
    pre.textContent = [
      'DEVICE: '+(d.device_name||'Unknown'),
      'Type: '+(d.device_class === 'standalone_stream' ? 'Standalone video stream' : 'CCTV/NVR device'),
      'Vendor: '+d.vendor,
      'Web: '+(d.web_url||'not detected'),
      'RTSP port '+rt.port+': '+(rt.reachable?'✅ reachable ('+rt.latency_ms+' ms)':'❌ unreachable'),
      'HTTP port: '+(d.http_probe?.selected_port||'none'),
      (d.device_class === 'standalone_stream'
        ? 'Stream: '+(d.stream_count || 1)+(d.stream_detected ? ' ✅ detected' : '')
        : 'Channels: '+(d.count||0)),
      (d.device_class === 'standalone_stream'
        ? 'Channel API: N/A — standalone stream'
        : 'Channel API: '+((d.count||0) > 0 ? 'detected' : 'none')),
      'Location: '+[d.geo?.city,d.geo?.region,d.geo?.country].filter(Boolean).join(', '),
      d.saved_category?'Saved to: '+d.saved_category:'Category unchanged'
    ].join('\n')+'\n\nDevice details:\n'+JSON.stringify(d.info||{},null,2);
    if (d.web_url) { cfg.disabled=false; cfg.textContent='🌐 Open CCTV Web'; cfg.onclick=()=>window.open(d.web_url,'_blank','noopener'); }
    showToast('✅ CCTV check complete — '+(rt.reachable?'RTSP reachable':'RTSP port not responding'), !rt.reachable);
    if (d.saved_category) setTimeout(()=>location.reload(),1800);
  } catch(err) { pre.textContent='❌ '+err.message; }
  finally { if (!cfg.onclick) { cfg.disabled=false; cfg.textContent='Close / back'; } }
}

function submitAddCamera(e) {
  e.preventDefault();
  const rtsp  = document.getElementById('add-rtsp').value.trim();
  const label = document.getElementById('add-label').value.trim();
  const group = document.getElementById('add-group').value.trim() || 'My Cameras';
  if (!rtsp.toLowerCase().startsWith('rtsp://')) {
    showToast('❌ URL must start with rtsp://', true); return;
  }
  fetch('/add_camera', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({rtsp_url: rtsp, label, group})})
    .then(r => r.json()).then(d => {
      if (d.ok) {
        document.getElementById('add-modal').close();
        showToast('✅ Camera added — reloading…');
        setTimeout(() => location.reload(), 1200);
      } else { showToast('❌ ' + d.detail, true); }
    });
}

function submitM3u(e) {
  e.preventDefault();
  const file = document.getElementById('m3u-file').files[0]; if (!file) return;
  const prog   = document.getElementById('m3u-progress');
  const submit = document.getElementById('m3u-submit');
  prog.textContent = '⏳ Uploading…'; prog.style.display = ''; submit.disabled = true;
  const fd = new FormData(); fd.append('file', file);
  fetch('/upload_m3u', {method:'POST', body: fd})
    .then(async r => {
      const d = await r.json().catch(() => ({}));
      if (!r.ok || !d.ok) throw Error(d.detail || ('Upload failed (HTTP ' + r.status + ')'));
      if (!d.job_id) throw Error(d.detail || 'Server did not create an import job');
      prog.textContent = '✅ Upload received. Importing…'; submit.disabled = true;
      const poll = async () => {
        try {
          const r2 = await fetch('/api/import/status/' + encodeURIComponent(d.job_id), {cache:'no-store'});
          const s = await r2.json();
          if (!r2.ok || !s.ok) throw Error(s.detail || 'Import status unavailable');
          prog.textContent = (s.status === 'completed' ? '✅ ' : s.status === 'failed' ? '❌ ' : '⏳ ')
            + (s.message || s.status) + (s.parsed ? ' · parsed ' + s.parsed : '')
            + (s.upserted ? ' · saved ' + s.upserted : '');
          if (s.status === 'completed') {
            document.getElementById('m3u-modal').close();
            showToast('✅ ' + s.filename + ' — ' + s.upserted + ' records imported — reloading…');
            setTimeout(() => location.reload(), 1400); return;
          }
          if (s.status === 'failed') { submit.disabled = false; return; }
          setTimeout(poll, 1200);
        } catch (err) {
          prog.textContent = '❌ Import status error: ' + err.message;
          submit.disabled = false;
        }
      };
      setTimeout(poll, 500);
    }).catch(err => { prog.textContent = '❌ ' + err.message; submit.disabled = false; });
}

async function previewM3u() {
  const file = document.getElementById('m3u-file').files[0];
  const prog = document.getElementById('m3u-progress');
  if (!file) { prog.textContent = 'Choose a file first'; prog.style.display = ''; return; }
  prog.textContent = '⏳ Reading preview — nothing will be saved…'; prog.style.display = '';
  const fd = new FormData(); fd.append('file', file);
  try {
    const r = await fetch('/api/import/preview', {method:'POST', body: fd});
    const d = await r.json();
    if (!r.ok || !d.ok) throw Error(d.detail || 'Preview failed');
    const sample = (d.sample || []).slice(0, 5)
      .map(x => (x.label || x.ip || x.id) + ' · ' + (x.rtsp_urls || []).join(', '))
      .join('\n');
    prog.textContent = '✅ ' + d.count + ' records detected. Sample:\n' + (sample || '(none)');
  } catch (e) {
    prog.textContent = '❌ ' + e.message;
  }
}

// ── Toast helpers ─────────────────────────────────────────────────────────────
function showToast(msg, error) {
  const t = document.getElementById('toast'); if (!t) return;
  t.innerHTML = msg;
  t.className = 'toast' + (error ? ' error' : '') + ' show';
  clearTimeout(t._tid); t._tid = setTimeout(() => t.classList.remove('show'), 3500);
}
function showTgToast(msg) {
  const t = document.getElementById('tg-toast');
  t.innerHTML = msg; t.classList.add('show');
  clearTimeout(t._tid); t._tid = setTimeout(() => t.classList.remove('show'), 3500);
}
</script>
<!-- Multi-camera live view -->
<style>
.dashboard-tools-label{font-size:10px;opacity:.65;margin-left:2px;align-self:center;letter-spacing:.4px}
.dashboard-tool-modal{position:fixed;inset:0;background:rgba(0,0,0,.72);z-index:10060;display:none;align-items:center;justify-content:center;padding:18px}
.dashboard-tool-modal.open{display:flex}.dashboard-tool-panel{width:min(1250px,96vw);height:min(82vh,760px);background:#0f172a;color:#e5e7eb;border:1px solid #334155;border-radius:12px;box-shadow:0 20px 80px #000;display:flex;flex-direction:column;overflow:hidden}
.dashboard-tool-head{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;border-bottom:1px solid #334155;background:#111827}
.dashboard-tool-body{padding:12px;overflow:auto;flex:1}.dashboard-tool-status{padding:8px 10px;border-radius:7px;background:#020617;border:1px solid #334155;margin-bottom:10px;white-space:pre-wrap}.dashboard-tool-pre{white-space:pre-wrap;background:#020617;border:1px solid #334155;border-radius:8px;padding:10px;max-height:55vh;overflow:auto;font:12px ui-monospace,SFMono-Regular,monospace}
.admin-popup-body{padding:12px;overflow:auto}
</style>
<div id="dashboardToolModal" class="dashboard-tool-modal" aria-hidden="true">
 <div class="dashboard-tool-panel">
  <div class="dashboard-tool-head"><strong id="dashboardToolTitle">Dashboard Work Tool</strong><button class="btn" onclick="closeDashboardTool()">✕ Close</button></div>
  <div class="dashboard-tool-body"><div id="dashboardToolStatus" class="dashboard-tool-status">Ready.</div><div id="dashboardToolActions" class="v43-feature-actions"></div><pre id="dashboardToolOutput" class="dashboard-tool-pre">Select a tool.</pre></div>
 </div>
</div>
<div id="adminPopupModal" class="dashboard-tool-modal" aria-hidden="true">
 <div class="dashboard-tool-panel" style="width:min(1100px,96vw);height:min(82vh,820px)">
  <div class="dashboard-tool-head"><strong>🛠 Administration</strong><div style="display:flex;gap:6px;align-items:center"><span id="adminPopupRole" class="multi-view-info">Administrator tools</span><button class="btn" onclick="closeAdminPopup()">✕ Close</button></div></div>
  <div class="dashboard-tool-body admin-popup-body">
   <div id="adminPopupActions" class="v43-feature-actions" style="margin-bottom:10px">
    <button class="btn" onclick="adminPopupRefresh()">🔄 Refresh</button>
    <button class="btn" onclick="adminPopupBackup()">💾 Backup DB</button>
    <button class="btn" onclick="adminPopupCleanup()">🧹 Cleanup & Backup</button>
    <button class="btn" onclick="adminPopupIntegrity()">🧪 DB Integrity</button>
   </div>
   <div id="adminPopupStatus" class="dashboard-tool-status">Ready.</div>
   <pre id="adminPopupOutput" class="dashboard-tool-pre">Loading administration status…</pre>
  </div>
 </div>
</div>
<div id="multiViewModal" class="multi-view-modal" aria-hidden="true">
  <div class="multi-view-panel">
    <div class="multi-view-head">
      <div>
        <strong>📺 Multi-Camera Live View</strong>
        <span id="multiViewInfo" class="multi-view-info"></span>
      </div>
      <div class="multi-view-controls">
        <button class="btn" onclick="setMultiViewLayout(1)">1 View</button>
        <button class="btn" onclick="setMultiViewLayout(4)">4 View</button>
        <button class="btn" onclick="setMultiViewLayout(8)">8 View</button>
        <button class="btn" onclick="setMultiViewLayout(16)">16 View</button>
        <button class="btn" onclick="setMultiViewLayout(25)">25 View</button>
        <button class="btn" onclick="setMultiViewLayout(36)">36 View</button>
        <button class="btn" onclick="setMultiViewLayout(49)">49 View</button>
        <button class="btn" onclick="setMultiViewLayout(64)">64 View</button>
        <button class="btn" onclick="openCustomMultiView(true)">Custom Grid</button>
        <button class="btn" onclick="saveMultiLayoutPrompt()">💾 Save Layout</button>
        <button class="btn" onclick="stopMultiView()">■ Stop</button>
        <button class="btn btn-primary" onclick="closeMultiView()">✕ Close</button>
      </div>
    </div>
    <div id="multiViewGrid" class="multi-view-grid"></div>
  </div>
</div>

<script>
/* ── Multi-camera live wall ─────────────────────────────────────────────── */
let multiViewStreams = {};
let multiViewIds = [];
let multiViewCount = Number(localStorage.getItem('camera.mv.count') || 4);
let multiViewCustomColumns = Number(localStorage.getItem('camera.mv.cols') || 0);
const multiViewReconnectTimers = {};
const multiViewStats = {};

function selectedOrVisibleCameraIds(limit) {
  const wanted = Math.max(1, Number(limit) || 1);
  const selected = typeof selectedCameraIds === 'function' ? selectedCameraIds() : [...(selectedCameraSet || [])];
  const selectedUnique = [...new Set((selected || []).map(String).filter(Boolean))];
  const visible = [...document.querySelectorAll('.cam-card[data-id]')]
    .filter(c => !c.classList.contains('hidden'))
    .map(c => String(c.dataset.id || ''))
    .filter(Boolean);
  // Selected cameras always have priority. If fewer than the requested layout
  // are selected, fill the remaining tiles from currently visible cameras so
  // 8/16/25/36 view never silently collapses to 7/15/etc.
  const ids = selectedUnique.length ? selectedUnique.slice() : visible.slice();
  if (ids.length < wanted) {
    for (const id of visible) {
      if (!ids.includes(id)) ids.push(id);
      if (ids.length >= wanted) break;
    }
  }
  return [...new Set(ids)].slice(0, wanted);
}

function openMultiView(count) {
  const ids = selectedOrVisibleCameraIds(count);
  if (!ids.length) { showToast('❌ No cameras available for multi-view', true); return; }
  multiViewCount = count;
  multiViewIds = ids;
  renderMultiView();
  document.getElementById('multiViewModal').classList.add('open');
  document.getElementById('multiViewModal').setAttribute('aria-hidden','false');
  startMultiViewStreams();
}

function openCustomMultiView(fromModal) {
  const current = fromModal ? multiViewCount : 4;
  const rawCount = prompt('Camera count (1–' + (window.__MAX_MULTI_VIEW || 64) + '):', String(current));
  if (rawCount === null) return;
  const n = Math.max(1, Math.min(Number(window.__MAX_MULTI_VIEW || 64), parseInt(rawCount, 10) || current));
  const rawCols = prompt('Grid columns (1–8):', String(Math.min(8, Math.ceil(Math.sqrt(n)))));
  if (rawCols === null) return;
  const cols = Math.max(1, Math.min(8, parseInt(rawCols, 10) || 2));
  const ids = selectedOrVisibleCameraIds(n);
  if (!ids.length) { showToast('❌ No cameras available for custom view', true); return; }
  multiViewCount=n; multiViewCustomColumns=cols; multiViewIds=ids;
  localStorage.setItem('camera.mv.count',String(n)); localStorage.setItem('camera.mv.cols',String(cols));
  renderMultiView();
  document.getElementById('multiViewModal').classList.add('open');
  document.getElementById('multiViewModal').setAttribute('aria-hidden','false');
  startMultiViewStreams();
}
function setMultiViewLayout(count) {
  if (!multiViewIds.length) { openMultiView(count); return; }
  multiViewCount=count; multiViewCustomColumns=0; localStorage.setItem('camera.mv.count',String(count));
  multiViewIds=selectedOrVisibleCameraIds(count); stopMultiView(); renderMultiView(); startMultiViewStreams();
}
function saveMultiLayoutPrompt(){const name=prompt('Layout name:','My Live Wall');if(!name)return;saveCurrentMultiLayout(name).then(r=>r.json()).then(d=>showToast(d.ok?'✅ Layout saved':'❌ '+(d.detail||'Save failed'),!d.ok)).catch(e=>showToast('❌ '+e.message,true));}

function multiViewColumns(n) {
  if (multiViewCustomColumns > 0) return Math.min(8, Math.max(1, multiViewCustomColumns));
  if (n <= 1) return 1; if (n <= 4) return 2; if (n <= 8) return 4; if (n <= 16) return 4; if (n <= 25) return 5; if (n <= 36) return 6; return 8;
}

function renderMultiView() {
  const grid = document.getElementById('multiViewGrid');
  if (!grid) return;
  grid.style.setProperty('--mv-cols', multiViewColumns(multiViewIds.length));
  grid.innerHTML = multiViewIds.map((id, i) => {
    const card = document.querySelector('.cam-card[data-id="' + CSS.escape(id) + '"]');
    const label = card?.querySelector('.cam-ip')?.textContent?.trim() || ('Camera ' + (i + 1));
    const type = card?.querySelector('.cam-type')?.textContent?.trim() || '';
    return '<div class="multi-tile" draggable="true" data-mv-id="' + escapeHtml(id) + '" ondragstart="multiDragStart(event,\'' + escapeHtml(id) + '\')" ondragover="event.preventDefault()" ondrop="multiDrop(event,\'' + escapeHtml(id) + '\')">' +
      '<div class="multi-tile-head"><span>' + escapeHtml(label) + '</span><small>' + escapeHtml(type) + '</small><span class="mv-fps"></span></div>' +
      '<div class="multi-screen" ondblclick="fullscreenMultiTile(\'' + escapeHtml(id) + '\')"><canvas class="multi-canvas"></canvas><div class="multi-status">⏳ Connecting…</div></div>' +
      '<div class="multi-tile-actions"><button class="btn" onclick="snapshotMultiTile(\'' + escapeHtml(id) + '\')">📸</button><button class="btn" onclick="fullscreenMultiTile(\'' + escapeHtml(id) + '\')">⛶</button><button class="btn" onclick="stopOneMultiStream(\'' + escapeHtml(id) + '\')">■</button></div>' +
      '</div>';
  }).join('');
  const info = document.getElementById('multiViewInfo');
  if (info) info.textContent = ' ' + multiViewIds.length + ' camera' + (multiViewIds.length === 1 ? '' : 's');
}

let multiDragId=null; function multiDragStart(e,id){multiDragId=id;try{e.dataTransfer.setData('text/plain',id);}catch(_){}} function multiDrop(e,target){e.preventDefault();const src=multiDragId||e.dataTransfer?.getData('text/plain');if(!src||src===target)return;const a=multiViewIds.indexOf(src),b=multiViewIds.indexOf(target);if(a<0||b<0)return;[multiViewIds[a],multiViewIds[b]]=[multiViewIds[b],multiViewIds[a]];renderMultiView();startMultiViewStreams();}
function escapeHtml(v) {
  return String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

async function startMultiViewStreams() {
  const max = Number(window.__MAX_STREAMS || 36);
  const mainActive = Object.keys(liveStreams || {}).length;
  const multiActive = Object.keys(multiViewStreams || {}).length;
  const available = Math.max(0, max - mainActive - multiActive);
  if (multiViewIds.length > available && multiViewIds.length > multiActive) {
    const keep = Math.max(0, available);
    showToast('⚠ Multi-view limited to ' + keep + ' available feed slot(s). Stop other feeds or increase CAMERA_MAX_LIVE_STREAMS.', true);
    multiViewIds = multiViewIds.slice(0, keep);
    renderMultiView();
  }
  for (const id of multiViewIds) {
    if (!document.querySelector('.multi-tile[data-mv-id="' + CSS.escape(id) + '"]')) continue;
    startOneMultiStream(id);
    await new Promise(r => setTimeout(r, 80));
  }
  updateLiveFeedCounter();
}

async function startOneMultiStream(id) {
  if (multiViewStreams[id]) return;
  const tile = document.querySelector('.multi-tile[data-mv-id="' + CSS.escape(id) + '"]');
  if (!tile) return;
  const canvas = tile.querySelector('.multi-canvas');
  const status = tile.querySelector('.multi-status');
  const ctx = canvas.getContext('2d');
  const st = {ws:null, tid:null, canvas};
  multiViewStreams[id] = st;
  try {
    await fetchRtsp(id);
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(proto + '://' + location.host + '/ws/stream/' + encodeURIComponent(id));
    ws.binaryType = 'arraybuffer'; st.ws = ws;
    st.tid = setTimeout(() => { try { ws.close(); } catch(_) {} }, 15000);
    ws.onmessage = async ev => {
      if (multiViewStreams[id]?.ws !== ws) return;
      if (typeof ev.data === 'string') { status.textContent = '❌ ' + ev.data.replace(/^ERROR:\s*/i,''); stopOneMultiStream(id); return; }
      clearTimeout(st.tid);
      st.tid = setTimeout(() => { try { ws.close(); } catch(_) {} }, 10000);
      try {
        const bm = await createImageBitmap(new Blob([ev.data], {type:'image/jpeg'}));
        canvas.width = bm.width; canvas.height = bm.height;
        ctx.drawImage(bm,0,0); const now=performance.now(); const st2=multiViewStats[id]||(multiViewStats[id]={frames:0,last:now,fps:0}); st2.frames++; if(now-st2.last>=1000){st2.fps=Math.round(st2.frames*1000/(now-st2.last));st2.frames=0;st2.last=now;const badge=tile.querySelector('.mv-fps');if(badge)badge.textContent=st2.fps+' FPS · '+bm.width+'×'+bm.height;} bm.close(); status.style.display = 'none';
      } catch(_) {}
    };
    ws.onerror = () => { status.textContent = '⚠ Stream error'; };
    ws.onclose = () => {
      if (multiViewStreams[id]?.ws !== ws) return;
      delete multiViewStreams[id]; updateLiveFeedCounter();
      status.textContent = '🔄 Reconnecting…';
      clearTimeout(multiViewReconnectTimers[id]);
      multiViewReconnectTimers[id] = setTimeout(() => { if (multiViewIds.includes(id)) startOneMultiStream(id); }, 2500);
    };
  } catch (e) {
    status.textContent = '❌ ' + e.message;
    delete multiViewStreams[id];
  }
}

async function snapshotMultiTile(id) {
  try { const r=await fetch('/snapshot/'+encodeURIComponent(id)); if(!r.ok) throw Error('Snapshot failed'); const b=await r.blob(); const u=URL.createObjectURL(b); const a=document.createElement('a'); a.href=u; a.download='camera_'+id+'_'+Date.now()+'.jpg'; a.click(); setTimeout(()=>URL.revokeObjectURL(u),1000); } catch(e){ showToast('❌ '+e.message,true); }
}
function fullscreenMultiTile(id) {
  const el=document.querySelector('.multi-tile[data-mv-id="'+CSS.escape(id)+'"] .multi-screen'); if(!el) return;
  if (el.requestFullscreen) el.requestFullscreen();
}
function saveCurrentMultiLayout(name) {
  const layout={count:multiViewCount,ids:multiViewIds,columns:multiViewColumns(multiViewIds.length)};
  return fetch('/api/v40/layouts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,layout})});
}
function stopOneMultiStream(id) {
  const st = multiViewStreams[id];
  if (st) { clearTimeout(st.tid); try { st.ws?.close(); } catch(_) {} }
  delete multiViewStreams[id];
  updateLiveFeedCounter();
}

function stopMultiView() {
  Object.keys(multiViewStreams).forEach(stopOneMultiStream);
  updateLiveFeedCounter();
}

function closeMultiView() {
  stopMultiView();
  const modal = document.getElementById('multiViewModal');
  if (modal) { modal.classList.remove('open'); modal.setAttribute('aria-hidden','true'); }
}
</script>
<!-- Unified dashboard work controls: all operational buttons live on the main dashboard. -->
<script>
(function(){
  const $=id=>document.getElementById(id);
  window.openDashboardTool=function(title,actions,output){
    const m=$('dashboardToolModal'); if(!m)return;
    $('dashboardToolTitle').textContent=title||'Dashboard Work Tool';
    $('dashboardToolActions').innerHTML=actions||'';
    $('dashboardToolOutput').textContent=typeof output==='string'?output:JSON.stringify(output??{},null,2);
    $('dashboardToolStatus').textContent='Ready.';
    m.classList.add('open'); m.setAttribute('aria-hidden','false');
  };
  window.closeDashboardTool=function(){const m=$('dashboardToolModal');if(m){m.classList.remove('open');m.setAttribute('aria-hidden','true');}};
  window.dashboardToolSet=function(msg,ok=true){const x=$('dashboardToolStatus');if(x){x.textContent=msg;x.style.borderColor=ok?'#166534':'#991b1b';x.style.color=ok?'#86efac':'#fca5a5';}};
  window.dashboardToolOut=function(v){const x=$('dashboardToolOutput');if(x)x.textContent=typeof v==='string'?v:JSON.stringify(v,null,2);};
  window.dashboardToolGet=async function(url,opt){
    const r=await fetch(url,opt||{}); const ct=(r.headers.get('content-type')||'').toLowerCase(); let d;
    try{d=ct.includes('json')?await r.json():{ok:r.ok,detail:await r.text()};}catch(e){throw Error('Invalid response from '+url);}
    if(!r.ok||d.ok===false)throw Error(d.detail||('HTTP '+r.status)); return d;
  };
  window.dashboardAuthMonitor=async function(){
    try{
      const r=await fetch('/api/session-status',{method:'GET',cache:'no-store',credentials:'same-origin'});
      if(r.status===401){
        window.location.replace('/login?logged_out=1');
        return false;
      }
      if(r.status===403){window.location.replace('/login?logged_out=1');return false;}
      if(!r.ok)return true;
      const d=await r.json();
      if(d.authenticated!==true){window.location.replace('/login?logged_out=1');return false;}
      return true;
    }catch(e){
      // Network failures are not treated as logout; the next heartbeat retries.
      return true;
    }
  };
  if(!window.__dashboardAuthMonitorStarted){
    window.__dashboardAuthMonitorStarted=true;
    window.dashboardAuthMonitor();
    window.__dashboardAuthMonitorTimer=setInterval(window.dashboardAuthMonitor,15000);
    document.addEventListener('visibilitychange',()=>{if(!document.hidden)window.dashboardAuthMonitor();});
  }
  window.dashboardToolSelected=function(){try{return typeof selectedCameraIds==='function'?selectedCameraIds():[]}catch(e){return[]}};
  window.__permissionMap={db:{{ 'true' if can_db else 'false' }},alerts:{{ 'true' if can_alerts else 'false' }},cameraControl:{{ 'true' if can_camera_control else 'false' }},cameraManage:{{ 'true' if can_camera_manage else 'false' }},diagnostics:{{ 'true' if can_diagnostics else 'false' }},reports:{{ 'true' if can_reports else 'false' }},health:{{ 'true' if can_health else 'false' }},settings:{{ 'true' if can_settings else 'false' }},stream:{{ 'true' if can_stream else 'false' }},security:{{ 'true' if can_security else 'false' }}};
  window.dashboardTool=async function(kind){
    const req={integrity:'db',backup:'db',diagnostics:'diagnostics','feature-matrix':'diagnostics',layouts:'settings',alerts:'alerts',health:'health',performance:'diagnostics',security:'security',recovery:'settings',reports:'reports',maintenance:'db',snapshots:'stream'};
    const ids=dashboardToolSelected(), actions='<button class="btn" onclick="closeDashboardTool()">Close</button>';
    if(req[kind] && !window.__permissionMap[req[kind]]){openDashboardTool('Dashboard Work Tool',actions,'Permission required: '+req[kind]);dashboardToolSet('Permission required: '+req[kind],false);return;}
    try{
      if(kind==='integrity'){openDashboardTool('🧪 Database Integrity',actions,'Running…');const d=await dashboardToolGet('/api/advanced/integrity');dashboardToolSet(d.integrity||d.detail||'Integrity check completed.',!!d.ok);dashboardToolOut(d);return;}
      if(kind==='backup'){openDashboardTool('💾 Database Backup',actions,'Creating verified backup…');const d=await dashboardToolGet('/api/advanced/backup',{method:'POST'});dashboardToolSet(d.detail||'Backup completed.',!!d.ok);dashboardToolOut(d);return;}
      if(kind==='diagnostics'){
        openDashboardTool('🩺 Selected Diagnostics',actions,'Preparing…');
        if(!ids.length){dashboardToolSet('Select one or more cameras on the main dashboard first.',false);dashboardToolOut('0 cameras selected.');return;}
        const d=await dashboardToolGet('/api/advanced/diagnostics',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids})});
        dashboardToolSet('Diagnostics completed for '+(d.count??ids.length)+' camera(s).',!!d.ok);dashboardToolOut(d);return;
      }
      if(kind==='snapshots'){
        openDashboardTool('📸 Selected Snapshots',actions,'Preparing…');
        if(!ids.length){dashboardToolSet('Select one or more cameras first.',false);dashboardToolOut('0 cameras selected.');return;}
        const results=[];
        for(const id of ids.slice(0,100)){
          try{const r=await fetch('/snapshot/'+encodeURIComponent(id));if(!r.ok)throw Error('HTTP '+r.status);const b=await r.blob();const u=URL.createObjectURL(b);const a=document.createElement('a');a.href=u;a.download='camera_'+id+'_'+Date.now()+'.jpg';a.click();setTimeout(()=>URL.revokeObjectURL(u),1500);results.push({id,ok:true});}
          catch(e){results.push({id,ok:false,error:e.message});}
        }
        dashboardToolSet('Snapshot operation completed for '+results.length+' camera(s).',true);dashboardToolOut(results);return;
      }
      if(kind==='feature-matrix'){const d=await dashboardToolGet('/api/v43/features');openDashboardTool('🧩 Priority 1–16 Feature Matrix — v73',actions,d.suite||d);return;}
      if(kind==='layouts'){const d=await dashboardToolGet('/api/v43/layouts');openDashboardTool('▦ Saved Live Layouts',actions,d);return;}
      if(kind==='alerts'){const d=await dashboardToolGet('/api/v43/alerts');openDashboardTool('🔔 Alerts',actions,d);return;}
      if(kind==='health'){const d=await dashboardToolGet('/api/v43/health?limit=200');openDashboardTool('❤️ Health Monitoring',actions,d);return;}
      if(kind==='performance'){const d=await dashboardToolGet('/api/v43/performance');openDashboardTool('⚡ Performance',actions,d);return;}
      if(kind==='security'){if(!window.__securityViewAllowed){dashboardToolSet('Security access is not available for this account.',false);return;}const d=await dashboardToolGet('/api/v43/security');let a=actions;if(d.current_permissions&&d.current_permissions.includes('security.manage')){a+='<button class="btn" onclick="dashboardSecurityLogoutAll()">🚪 Logout all sessions</button><button class="btn" onclick="dashboardSecurityClearLockouts()">🔓 Clear user lockouts</button>'; }openDashboardTool('🔐 Security',a,'');dashboardSecurityRender(d);return;}
      if(kind==='recovery'){const d=await dashboardToolGet('/api/v43/recovery');openDashboardTool('♻ Stream Recovery',actions,d);return;}
      if(kind==='reports'){
        openDashboardTool('📋 Selected Device Reports',actions,'Loading…');
        if(!ids.length){dashboardToolSet('Select camera(s) first.',false);return;}
        const out=[];for(const id of ids.slice(0,100))out.push(await dashboardToolGet('/api/v43/device/report/'+encodeURIComponent(id)+'/full'));
        dashboardToolSet('Loaded '+out.length+' report(s).',true);dashboardToolOut(out);return;
      }
      if(kind==='maintenance'){
        if(!confirm('Run maintenance, retention cleanup and create a safety backup?'))return;
        openDashboardTool('🧹 Maintenance',actions,'Running…');const d=await dashboardToolGet('/api/v43/maintenance',{method:'POST'});dashboardToolSet('Maintenance completed.',!!d.ok);dashboardToolOut(d);return;
      }
    }catch(e){openDashboardTool('Dashboard Work Tool',actions,e.message);dashboardToolSet(e.message,false);}
  };
  window.openAdminPopup=async function(){const m=$('adminPopupModal');if(!m){alert('Administration popup is unavailable.');return;}m.classList.add('open');m.setAttribute('aria-hidden','false');await adminPopupRefresh();};
  window.closeAdminPopup=function(){const m=$('adminPopupModal');if(m){m.classList.remove('open');m.setAttribute('aria-hidden','true');}};
  window.adminPopupRefresh=async function(){try{const d=await dashboardToolGet('/api/admin/health');$('adminPopupStatus').textContent=d.ok?'Administration status loaded.':'Administration request failed.';$('adminPopupStatus').style.color=d.ok?'var(--green)':'var(--red)';$('adminPopupOutput').textContent=JSON.stringify(d,null,2);}catch(e){$('adminPopupStatus').textContent='❌ '+e.message;$('adminPopupStatus').style.color='var(--red)';$('adminPopupOutput').textContent=e.message;}};
  window.adminPopupBackup=async function(){try{$('adminPopupStatus').textContent='Creating verified database backup…';const d=await dashboardToolGet('/api/advanced/backup',{method:'POST'});$('adminPopupStatus').textContent=d.ok?'✅ Database backup completed.':'❌ '+(d.detail||'Backup failed');$('adminPopupOutput').textContent=JSON.stringify(d,null,2);}catch(e){$('adminPopupStatus').textContent='❌ '+e.message;}};
  window.adminPopupCleanup=async function(){if(!confirm('Run administrator cleanup, WAL checkpoint and verified backup?'))return;try{$('adminPopupStatus').textContent='Running cleanup…';const d=await dashboardToolGet('/api/admin/cleanup',{method:'POST'});$('adminPopupStatus').textContent=d.ok?'✅ Cleanup and backup completed.':'❌ '+(d.detail||'Cleanup failed');$('adminPopupOutput').textContent=JSON.stringify(d,null,2);}catch(e){$('adminPopupStatus').textContent='❌ '+e.message;}};
  window.adminPopupIntegrity=async function(){try{$('adminPopupStatus').textContent='Checking SQLite integrity…';const d=await dashboardToolGet('/api/advanced/integrity');$('adminPopupStatus').textContent=d.ok?'✅ '+(d.integrity||'Database integrity OK'):'❌ '+(d.detail||'Integrity check failed');$('adminPopupOutput').textContent=JSON.stringify(d,null,2);}catch(e){$('adminPopupStatus').textContent='❌ '+e.message;}};
  window.dashboardToolTestAlerts=async function(){try{const d=await dashboardToolGet('/api/v43/alerts/test',{method:'POST'});dashboardToolSet(d.detail||'Alert test completed.',!!d.ok);dashboardToolOut(d);}catch(e){dashboardToolSet(e.message,false);}};
  window.__securityData=null;
  window.__securityEditingUser='';
  window.dashboardSecurityPerms=function(){return Array.from(document.querySelectorAll('#secPermEditor input[type=checkbox]:checked')).map(x=>x.value);};
  window.dashboardSecurityApplyRole=function(role, target='secPermEditor'){
    const box=document.getElementById(target); if(!box)return;
    const defaults=(window.__securityData?.role_defaults||{})[role]||[];
    const isAdmin=role==='admin', isReadonly=role==='readonly', isOperator=role==='operator';
    box.querySelectorAll('input[type=checkbox]').forEach(cb=>{
      const requiredOperatorStream=isOperator && cb.value==='stream.view';
      cb.checked=isAdmin?true:(defaults.includes(cb.value)||requiredOperatorStream);
      cb.disabled=isAdmin||isReadonly||requiredOperatorStream;
    });
  };
  window.dashboardSecurityBuildPerms=function(perms=[], role='', disabled=false){
    const defs=window.__securityData?.permission_definitions||{};
    let h=''; Object.entries(defs).forEach(([k,v])=>{const requiredOperatorStream=role==='operator'&&k==='stream.view';const checked=role==='admin'||perms.includes(k)||requiredOperatorStream;const lock=disabled||role==='admin'||role==='readonly'||requiredOperatorStream;h+='<label style="display:flex;gap:6px;align-items:flex-start;padding:3px 0"><input type="checkbox" value="'+String(k).replace(/"/g,'&quot;')+'" '+(checked?'checked ':'')+(lock?'disabled ':'')+'> <span><b>'+k+'</b> <small style="color:var(--muted)">'+v+'</small></span></label>';});return h;
  };
  window.dashboardSecurityRenderEditor=function(user=null){
    const out=document.getElementById('secUserEditor');if(!out)return;
    const d=window.__securityData||{}; const role=user?.role||'custom'; const perms=Array.isArray(user?.permissions)?user.permissions:((d.role_defaults||{})[role]||[]);
    window.__securityEditingUser=user?.username||'';
    const isEdit=!!user; const canReset=d.current_role==='admin';
    let h='<div style="border:1px solid var(--border);border-radius:8px;padding:10px">';
    h+='<div style="display:flex;justify-content:space-between;align-items:center"><b>'+ (isEdit?'✏️ Edit user: '+String(user.username):'➕ Add user') +'</b>'+(isEdit?'<button class="btn" type="button" data-sec-action="cancel-edit">✕ Cancel</button>':'')+'</div>';
    h+='<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:7px;margin-top:8px">';
    h+='<label>Username<input id="secEditUsername" value="'+String(user?.username||'').replace(/"/g,'&quot;')+'" '+(isEdit?'readonly':'')+' placeholder="Username" style="width:100%;box-sizing:border-box"></label>';
    h+='<label>Role<select id="secEditRole" style="width:100%;box-sizing:border-box"><option value="custom">custom</option><option value="operator">operator</option><option value="readonly">readonly</option><option value="admin">admin</option></select></label>';
    h+='<label>New password<input id="secEditPassword" type="password" placeholder="'+(isEdit?'Leave blank to keep current':'Password (8+ chars)')+'" style="width:100%;box-sizing:border-box"></label>';
    h+='<label style="display:flex;align-items:center;gap:6px;margin-top:24px"><input id="secEditEnabled" type="checkbox" '+(user?((user.enabled!==false)?'checked':''):'checked')+' '+((user&&user.username===d.primary_admin_user)?'disabled':'')+'> Enabled</label>';
    h+='</div><div id="secPermEditor" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:4px;margin-top:8px">'+window.dashboardSecurityBuildPerms(perms,role)+'</div>';
     const scope=user?.access_scope||{}; const countries=Array.isArray(scope.countries)?scope.countries:[]; const devices=Array.isArray(scope.devices)?scope.devices:[]; const cameras=Array.isArray(scope.cameras)?scope.cameras:[];
     const available=Array.isArray(d.available_countries)?d.available_countries:[];
     h+='<div style="border:1px solid var(--border);border-radius:8px;padding:10px;margin-top:10px"><b>🎯 Camera / Device Access Scope</b><div style="color:var(--muted);margin:5px 0 8px">Empty scope = unrestricted. Adding any rule limits this user to the selected countries, devices/IPs, or camera IDs.</div>';
     h+='<div id="secAccessCountries" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:4px">';
     available.forEach(c=>{const ck=countries.some(x=>String(x).toLowerCase()===String(c).toLowerCase());h+='<label><input type="checkbox" value="'+String(c).replace(/&/g,'&amp;').replace(/"/g,'&quot;')+'" '+(ck?'checked':'')+'> '+String(c).replace(/[&<>]/g,x=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[x]))+'</label>';});
     const availDevices=Array.isArray(d.available_devices)?d.available_devices:[];
     h+='<div style="margin-top:8px;max-height:220px;overflow:auto;border:1px solid var(--border);padding:7px;border-radius:6px"><b>Devices</b><div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:4px;margin-top:5px">';
     availDevices.slice(0,1000).forEach(v=>{const dv=String(v.device_id||v.ip||'').trim();if(!dv)return;const ck=devices.some(x=>String(x).toLowerCase()===dv.toLowerCase());const label=(v.label||v.device_id||v.ip||dv)+' — '+(v.ip||'')+(v.country?' ['+v.country+']':'');h+='<label title="'+label.replace(/"/g,'&quot;')+'"><input class="secAccessDeviceCheck" type="checkbox" value="'+dv.replace(/"/g,'&quot;')+'" '+(ck?'checked':'')+'> '+label.replace(/[&<>]/g,x=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[x]))+'</label>';});
     h+='</div></div><label style="display:block;margin-top:8px">Additional Device IDs or IPs <small style="color:var(--muted)">(one per line)</small><textarea id="secAccessDevices" rows="3" style="width:100%;box-sizing:border-box"></textarea></label>';
     h+='<label style="display:block;margin-top:8px">Specific Camera IDs <small style="color:var(--muted)">(one per line)</small><textarea id="secAccessCameras" rows="3" style="width:100%;box-sizing:border-box">'+cameras.join('\n').replace(/&/g,'&amp;').replace(/</g,'&lt;')+'</textarea></label></div>';
    h+='<div style="margin-top:8px;color:var(--muted)">'+(role==='admin'?'Admin always receives every permission.':role==='readonly'?'Readonly is restricted to dashboard.view + stream.view.':role==='operator'?'Operator always includes stream.view for live playback; other permissions can be adjusted as needed.':'Select the default permissions and add/remove any extra permissions as needed.')+'</div>';
    h+='<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:9px"><button class="btn btn-primary" type="button" data-sec-action="save-editor">💾 '+(isEdit?'Save changes':'Create user')+'</button>';
    if(isEdit&&canReset)h+='<button class="btn" type="button" data-sec-action="reset-password">🔑 Admin reset password</button>';
    h+='</div></div>';
    out.innerHTML=h;
    const rs=document.getElementById('secEditRole'); if(rs){rs.value=role;rs.onchange=()=>window.dashboardSecurityApplyRole(rs.value);}
  };
  window.dashboardSecurityRender=function(d){
    window.__securityData=d;
    const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const out=document.getElementById('dashboardToolOutput'); if(!out)return;
    const can=Array.isArray(d.current_permissions)&&d.current_permissions.includes('security.manage');
    let h='<div style="font-family:system-ui;color:var(--text);font-size:13px">';
    h+='<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px"><span class="stat-pill">👤 <b>Logged in:</b> '+esc(d.current_user)+'</span><span class="stat-pill">🔐 <b>Role:</b> '+esc(d.current_role)+'</span><span class="stat-pill">🛡 <b>Active sessions:</b> '+esc((d.active_sessions||[]).length)+'</span><button class="btn" type="button" onclick="dashboardSecurityChangeOwnPassword()">🔑 Change my password</button></div>';
    if(can){
      h+='<div id="secUserEditor"></div>';
      h+='<div style="border:1px solid var(--border);border-radius:8px;padding:10px;margin-top:10px"><b>👥 Users</b><div style="overflow:auto"><table style="width:100%;border-collapse:collapse;margin-top:8px"><thead><tr><th>User</th><th>Role</th><th>Source</th><th>Access scope</th><th>Enabled</th><th>Sessions</th><th>Actions</th></tr></thead><tbody>';
      (d.users||[]).forEach(u=>{
        const uname=esc(u.username);
        const sc=u.access_scope||{}; const scopeText=sc.restricted?('🎯 '+((sc.countries||[]).length)+' country, '+((sc.devices||[]).length)+' device, '+((sc.cameras||[]).length)+' camera rule(s)'):'🌐 All devices';
         h+='<tr><td>'+uname+(u.current?' ⭐':'')+'</td><td>'+esc(u.role)+'</td><td>'+esc(u.source)+'</td><td>'+esc(scopeText)+'</td><td>'+ (u.enabled?'✅':'❌')+'</td><td>'+esc(u.active_sessions)+'</td><td style="white-space:nowrap">';
        h+='<button class="btn" type="button" data-sec-action="edit-user" data-username="'+uname+'">⚙ Edit</button>';
        h+='<button class="btn" type="button" data-sec-action="logout-user" data-username="'+uname+'">🚪 Logout</button>';
        if(!u.current && u.username!==(d.primary_admin_user||'')) {
          if(u.enabled) h+='<button class="btn" type="button" data-sec-action="disable-user" data-username="'+uname+'">⛔ Disable</button>';
          else h+='<button class="btn" type="button" data-sec-action="enable-user" data-username="'+uname+'">✅ Enable</button>';
        }
        h+='<button class="btn" type="button" data-sec-action="clear-lockout" data-username="'+uname+'">🔓 Clear lockout</button>';
        if(d.current_role==='admin')h+='<button class="btn" type="button" data-sec-action="reset-user-password" data-username="'+uname+'">🔑 Reset password</button>';
        h+='</td></tr>';
      });
      h+='</tbody></table></div></div>';
      h+='<div style="border:1px solid var(--border);border-radius:8px;padding:10px;margin-top:10px"><b>🔑 Permission definitions</b><div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:5px;margin-top:8px">';
      Object.entries(d.permission_definitions||{}).forEach(([k,v])=>{h+='<label><code>'+esc(k)+'</code> — '+esc(v)+'</label>';});
      h+='</div></div>';
      h+='<div style="border:1px solid var(--border);border-radius:8px;padding:10px;margin-top:10px"><b>🖥 Active sessions</b><div style="overflow:auto"><table style="width:100%;border-collapse:collapse;margin-top:8px"><thead><tr><th>User</th><th>Role</th><th>IP</th><th>Created</th><th>Last seen</th><th>Action</th></tr></thead><tbody>';
      (d.active_sessions||[]).forEach(x=>{const uname=esc(x.username);h+='<tr><td>'+uname+'</td><td>'+esc(x.role)+'</td><td>'+esc(x.client_ip)+'</td><td>'+esc(x.created_at)+'</td><td>'+esc(x.last_seen)+'</td><td><button class="btn" type="button" data-sec-action="logout-user" data-username="'+uname+'">🚪 Logout user</button></td></tr>';});
      h+='</tbody></table></div></div>';
    }
    h+='<pre style="margin-top:10px">'+esc(JSON.stringify({auth_enabled:d.auth_enabled,auth_configured:d.auth_configured,secure_cookie_configured:d.secure_cookie_configured,session_hardening:d.session_hardening,locked_clients:d.locked_clients,ip_guard_locked_clients:d.ip_guard_locked_clients,session_timeout_seconds:d.session_timeout_seconds,current_permissions:d.current_permissions},null,2))+'</pre></div>';
    out.innerHTML=h;
    if(can)window.dashboardSecurityRenderEditor();
  };
  window.dashboardSecurityRefresh=async function(){try{const d=await dashboardToolGet('/api/v43/security');window.__securityData=d;dashboardSecurityRender(d);}catch(e){dashboardToolSet(e.message,false);}};
  window.dashboardSecuritySaveEditor=async function(){
    const u=document.getElementById('secEditUsername')?.value.trim(), p=document.getElementById('secEditPassword')?.value||'', r=document.getElementById('secEditRole')?.value||'custom', editing=window.__securityEditingUser;
    if(!u){dashboardToolSet('Username is required.',false);return;}
    if(!editing && !p){dashboardToolSet('Password is required for a new user.',false);return;}
    const perms=window.dashboardSecurityPerms();
    const enabled=document.getElementById('secEditEnabled')?.checked!==false;
    const body={role:r,permissions:perms,enabled};if(p)body.password=p;
    try{
      const url=editing?'/api/v43/users/'+encodeURIComponent(editing):'/api/v43/users';
      const method=editing?'PATCH':'POST';
      const payload=editing?{role:r,permissions:perms,enabled}:{username:u,password:p,role:r,permissions:perms,enabled};
      if(editing&&p)payload.password=p;
      const d=await dashboardToolGet(url,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
       const countryValues=Array.from(document.querySelectorAll('#secAccessCountries input[type=checkbox]:checked')).map(x=>x.value);
       const checkedDeviceValues=Array.from(document.querySelectorAll('#secUserEditor .secAccessDeviceCheck:checked')).map(x=>x.value);
       const extraDeviceValues=(document.getElementById('secAccessDevices')?.value||'').split(/\r?\n/).map(x=>x.trim()).filter(Boolean);
       const deviceValues=Array.from(new Set(checkedDeviceValues.concat(extraDeviceValues)));
       const cameraValues=(document.getElementById('secAccessCameras')?.value||'').split(/\r?\n/).map(x=>x.trim()).filter(Boolean);
       await dashboardToolGet('/api/v43/users/'+encodeURIComponent(u)+'/access',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({countries:countryValues,devices:deviceValues,cameras:cameraValues})});
      dashboardToolSet(d.detail||'User saved.',true);
      if(editing&&editing===(window.__securityData||{}).current_user&&(!enabled||p||r!==(window.__securityData||{}).current_role)){
        setTimeout(()=>location.href='/login?user_updated=1',500);return;
      }
      await dashboardSecurityRefresh();
    }catch(e){dashboardToolSet(e.message,false);}
  };
  window.dashboardSecurityEditUser=async function(username){const d=window.__securityData||await dashboardToolGet('/api/v43/security');const u=(d.users||[]).find(x=>x.username===username);if(!u){dashboardToolSet('User not found.',false);return;}window.dashboardSecurityRenderEditor(u);document.getElementById('secUserEditor')?.scrollIntoView({behavior:'smooth',block:'start'});};
  window.dashboardSecurityChangeOwnPassword=async function(){
    const current=prompt('Current password:'); if(current===null)return; const np=prompt('New password (8+ chars):'); if(np===null)return; const cp=prompt('Confirm new password:'); if(cp!==np){alert('New passwords do not match.');return;}
    try{const d=await dashboardToolGet('/api/v43/users/'+encodeURIComponent((window.__securityData||{}).current_user)+'/password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({current_password:current,new_password:np})});dashboardToolSet(d.detail||'Password changed. You must sign in again.',true);setTimeout(()=>location.href='/login?password_changed=1',700);}catch(e){dashboardToolSet(e.message,false);}
  };
  window.dashboardSecurityResetPassword=async function(username){
    const np=prompt('Admin reset: enter new password for '+username+' (8+ chars):');if(np===null)return;const cp=prompt('Confirm new password:');if(cp!==np){alert('New passwords do not match.');return;}
    try{const d=await dashboardToolGet('/api/v43/users/'+encodeURIComponent(username)+'/password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({new_password:np})});dashboardToolSet(d.detail||'Password reset. Existing sessions were revoked.',true);if(username===(window.__securityData||{}).current_user){setTimeout(()=>location.href=d.redirect||'/login?password_reset=1',500);return;}await dashboardSecurityRefresh();}catch(e){dashboardToolSet(e.message,false);}
  };
  document.addEventListener('click',async function(e){
    const b=e.target.closest?.('[data-sec-action]'); if(!b)return; const action=b.dataset.secAction, username=b.dataset.username;
    try{
      if(action==='edit-user'){await dashboardSecurityEditUser(username);return;}
      if(action==='cancel-edit'){window.dashboardSecurityRenderEditor();return;}
      if(action==='save-editor'){await dashboardSecuritySaveEditor();return;}
      if(action==='reset-password'||action==='reset-user-password'){if(action==='reset-user-password')await dashboardSecurityResetPassword(username);else await dashboardSecurityResetPassword(window.__securityEditingUser);return;}
      if(action==='logout-user'){if(!confirm('Invalidate ALL active sessions for '+username+'?'))return;dashboardToolSet('Revoking '+username+' sessions…',true);const d=await dashboardToolGet('/api/v43/users/'+encodeURIComponent(username)+'/logout',{method:'POST'});dashboardToolSet(d.detail||'User sessions revoked.',true);await dashboardSecurityRefresh();return;}
      if(action==='disable-user'){
        if(!confirm('Disable '+username+' and revoke all sessions?'))return;
        const r=await dashboardToolGet('/api/v43/users/'+encodeURIComponent(username),{method:'DELETE'});
        dashboardToolSet(r.detail||('User '+username+' disabled and sessions revoked.'),true);
        await dashboardSecurityRefresh();
        return;
      }
      if(action==='enable-user'){
        dashboardToolSet('Enabling '+username+'…',true);
        const d=window.__securityData?.users?.find(x=>x.username===username);
        if(!d){dashboardToolSet('User '+username+' was not found in the current security list.',false);return;}
        const role=d.role||'custom';
        const perms=role==='admin'?Object.keys(window.__securityData?.permission_definitions||{}):(role==='readonly'?['dashboard.view','stream.view']:(Array.isArray(d.permissions)?d.permissions:((window.__securityData?.role_defaults||{})[role]||[])));
        const r=await dashboardToolGet('/api/v43/users/'+encodeURIComponent(username),{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:true,role,permissions:perms})});
        dashboardToolSet(r.detail||('User '+username+' enabled.'),true);
        await dashboardSecurityRefresh();
        return;
      }
      if(action==='clear-lockout'){const d=await dashboardToolGet('/api/v43/security/clear-lockout/'+encodeURIComponent(username),{method:'POST'});dashboardToolSet(d.detail||'Lockout cleared.',true);await dashboardSecurityRefresh();return;}
    }catch(err){dashboardToolSet(err.message,false);}
  });

  window.dashboardSecurityLogoutAll=async function(){if(!confirm('Invalidate all dashboard sessions? Every logged-in user will need to sign in again.'))return;try{dashboardToolSet('Invalidating all dashboard sessions…',true);const d=await dashboardToolGet('/api/v43/security/logout-all',{method:'POST'});dashboardToolSet(d.detail||'All sessions invalidated.',true);dashboardToolOut(d);setTimeout(()=>{location.href='/login';},700);}catch(e){dashboardToolSet(e.message,false);dashboardToolOut(e.message);}};
window.dashboardSecurityClearLockouts=async function(){if(!confirm('Clear user-specific login lockouts? The IP anti-bruteforce guard will NOT be cleared.'))return;try{const d=await dashboardToolGet('/api/v43/security/clear-lockouts',{method:'POST'});dashboardToolSet(d.detail||'Lockouts cleared.',true);dashboardToolOut(d);await dashboardSecurityRefresh();}catch(e){dashboardToolSet(e.message,false);}};
window.saveCurrentDashboardLayout=async function(){
    const name=prompt('Layout name:','Dashboard Layout');if(!name)return;
    try{const ids=typeof multiViewIds!=='undefined'?multiViewIds:[];const d=await dashboardToolGet('/api/v43/layouts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,layout:{count:ids.length||4,ids,columns:typeof multiViewColumns==='function'?multiViewColumns(ids.length||4):2}})});dashboardToolSet('Layout saved.',true);dashboardToolOut(d);}catch(e){dashboardToolSet(e.message,false);}
  };
  document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeDashboardTool();closeAdminPopup();closeV43FeatureCenter();}});
  document.addEventListener('click',e=>{const m=$('dashboardToolModal');if(m&&e.target===m)closeDashboardTool();const a=$('adminPopupModal');if(a&&e.target===a)closeAdminPopup();const f=$('v43FeatureModal');if(f&&e.target===f)closeV43FeatureCenter();});
})();
</script>

<script>
/* v63 live operational dashboard polling */
(function(){
  const $=id=>document.getElementById(id);
  let timer=null, busy=false;
  const esc=v=>String(v??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
  const set=(id,v,cls)=>{const e=$(id);if(!e)return;e.textContent=v==null?'—':v;if(cls)e.className='v63-metric-value '+cls;};
  const fmtBytes=n=>{n=Number(n)||0;if(n<1024)return n+' B';if(n<1048576)return (n/1024).toFixed(1)+' KB';if(n<1073741824)return (n/1048576).toFixed(1)+' MB';return (n/1073741824).toFixed(2)+' GB';};
  const fmtTime=s=>{s=Number(s)||0;if(!s)return '—';if(s<60)return Math.round(s)+'s';if(s<3600)return Math.floor(s/60)+'m '+Math.floor(s%60)+'s';return Math.floor(s/3600)+'h '+Math.floor((s%3600)/60)+'m';};
  function status(ok){const e=$('v63OpsState');if(!e)return;e.textContent=ok?'LIVE':'ERROR';e.className='stat-pill '+(ok?'sp-on':'sp-off');}
  async function v64ControlState(){
    try{
      const r=await fetch('/api/v64/validation/control',{cache:'no-store'});
      const d=await r.json(); if(!r.ok||d.ok===false)throw new Error(d.detail||('HTTP '+r.status));
      const paused=!!d.control?.paused;
      const b=$('v64PauseBtn'); if(b)b.textContent=paused?'▶ Resume Validation':'⏸ Pause Validation';
      const s=$('v64ControlStatus'); if(s)s.textContent=paused?'Validation queue is PAUSED. Active probes are allowed to finish.':'Validation queue is running/ready.';
    }catch(e){const s=$('v64ControlStatus');if(s)s.textContent='Control status unavailable: '+e.message;}
  }
  window.v64ValidationAction=async function(action){
    action=action||'pause';
    if(action==='pause'||action==='resume'){}
    try{
      if(action==='pause'){
        const state=await fetch('/api/v64/validation/control',{cache:'no-store'}).then(r=>r.json());
        action=state.control?.paused?'resume':'pause';
      }
      const labels={'pause':'Pausing validation…','resume':'Resuming validation…','retry-failed':'Requeuing failed cameras…','queue-cleanup':'Cleaning stale queue claims…','restart-worker':'Requesting worker restart…'};
      const s=$('v64ControlStatus');if(s)s.textContent=labels[action]||'Working…';
      const r=await fetch('/api/v64/validation/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action})});
      const d=await r.json();if(!r.ok||d.ok===false)throw new Error(d.detail||('HTTP '+r.status));
      if(s)s.textContent=d.detail||'Done.';
      await v64ControlState();
      await v63RefreshOps();
    }catch(e){const s=$('v64ControlStatus');if(s)s.textContent='Control failed: '+e.message;}
  };

  window.v65ValidationAction=async function(action){
    const labels={'retry-exhausted':'Requeuing exhausted cameras…','reprioritize':'Reprioritizing validation queue…'};
    const s=$('v64ControlStatus');if(s)s.textContent=labels[action]||'Working…';
    try{
      const r=await fetch('/api/v65/validation/'+encodeURIComponent(action),{method:'POST',headers:{'Content-Type':'application/json'}});
      const d=await r.json();if(!r.ok||d.ok===false)throw new Error(d.detail||('HTTP '+r.status));
      if(s)s.textContent=d.detail||'Done.'; await v63RefreshOps();
    }catch(e){if(s)s.textContent='Validation Engine 2.0 failed: '+e.message;}
  };

  let lastMaintenanceActive=false;
  let lastMaintenanceCompleted='';
  function updateMaintenanceStatus(m){
    const e=$('dbMaintenanceState'); if(!e)return;
    const active=!!m?.active; const state=m?.state||'free';
    if(active){
      e.hidden=false; e.className='db-maintenance-state busy';
      const phase=m.phase||'working'; const who=m.actor?(' · '+m.actor):'';
      e.textContent='🔒 DB BUSY · '+phase+who;
      if(!lastMaintenanceActive) showToast('🔒 Database maintenance started — database writes are temporarily restricted.');
    }else if(m?.last_result==='success' && m.completed_at && m.completed_at!==lastMaintenanceCompleted){
      lastMaintenanceCompleted=m.completed_at; e.hidden=false; e.className='db-maintenance-state free';
      e.textContent='✓ DB FREE · shrink complete';
      showToast('✅ Database is free again — shrink completed successfully.');
      setTimeout(()=>{if(!lastMaintenanceActive)e.hidden=true;},12000);
    }else if(m?.last_result==='failed' && m.completed_at && m.completed_at!==lastMaintenanceCompleted){
      lastMaintenanceCompleted=m.completed_at; e.hidden=false; e.className='db-maintenance-state failed';
      e.textContent='⚠ DB MAINTENANCE FAILED';
      showToast('⚠ Database maintenance failed; existing verified backup was preserved.', true);
      setTimeout(()=>{if(!lastMaintenanceActive)e.hidden=true;},15000);
    }
    lastMaintenanceActive=active;
  }

  window.v63RefreshOps=async function(){
    if(busy)return; busy=true;
    try{
      const r=await fetch('/api/v64/dashboard-metrics',{cache:'no-store'});
      const d=await r.json();
      if(!r.ok||d.ok===false)throw new Error(d.detail||('HTTP '+r.status));
      const w=d.worker||{}, res=d.resource||{}, v=d.validation||{}, q=d.queue||{}, db=d.db||{}, f=d.feeds||{};
      const maintenance=d.database_maintenance||{};
      updateMaintenanceStatus(maintenance);
      const vc=d.validation_control||{}; const paused=!!vc.paused; const pb=$('v64PauseBtn'); if(pb)pb.textContent=paused?'▶ Resume Validation':'⏸ Pause Validation'; const cs=$('v64ControlStatus'); if(cs&&!cs.textContent.includes('failed'))cs.textContent=paused?'Validation queue is PAUSED. Active probes are allowed to finish.':'Validation queue is running/ready.';
      const workerName=Object.keys(w.workers||{}).find(k=>/validation-queue|validator/i.test(k))||Object.keys(w.workers||{})[0]||'—';
      const worker=(w.workers||{})[workerName]||{};
      set('v63Worker',worker.state||'—',worker.state==='running'||worker.state==='idle'?'v63-ok':worker.state==='error'?'v63-bad':'v63-warn');
      set('v63WorkerDetail',workerName+(worker.detail?' · '+worker.detail:''));
      set('v63Workers',res.workers??w.adaptive_workers??'—');
      set('v63WorkersDetail',res.mode==='auto'?'AUTO · adaptive':'Mode: '+(res.mode||'—'));
      const total=Number(v.total)||0, done=Number(v.done)||0, pct=total?Math.min(100,done*100/total):0;
      set('v63Validation',total?(done+'/'+total+' · '+pct.toFixed(0)+'%'):(v.running?'Running':'Idle'),v.running?'v63-warn':'v63-ok');
      set('v63ValidationDetail',(Number(v.online)||0)+' online · '+(Number(v.offline)||0)+' offline');
      const bar=$('v63ValidationBar');if(bar)bar.style.width=pct.toFixed(1)+'%';
      const speed=Number(v.speed_per_sec||v.speed)||0;set('v63Speed',speed?speed.toFixed(2)+' cam/s':'—');set('v63Eta','ETA '+fmtTime(v.eta_seconds));
      const qe=d.validation_engine||{};
      set('v63Queue',(Number(qe.due??q.due)||0)+' due');
      set('v63QueueDetail',(Number(qe.locked??q.locked)||0)+' locked · '+(Number(qe.stale_claims??q.stale_claims)||0)+' stale · '+(Number(qe.exhausted)||0)+' exhausted');
      if(qe.eta_seconds!=null)set('v63Eta','Queue ETA '+fmtTime(qe.eta_seconds));
      set('v63Feeds',(Number(f.active)||0)+' / '+(Number(f.limit)||'—'));set('v63FeedsDetail','Recovery '+(Number(f.recovery_attempts)||0));
      set('v63Resource',(res.cpu_percent!=null?Number(res.cpu_percent).toFixed(0)+'% CPU':(res.load1!=null?'load '+Number(res.load1).toFixed(2):'—')));
      set('v63ResourceDetail',(res.ram_available_gb!=null?Number(res.ram_available_gb).toFixed(1)+' GB free RAM':'')+(res.cpu_count?' · '+res.cpu_count+' CPU':'') );
      const dbBusy=!!maintenance.active;
      set('v63Db',dbBusy?'BUSY':(db.integrity===true?'OK':db.integrity===false?'FAIL':'—'),dbBusy?'v63-warn':(db.integrity===true?'v63-ok':db.integrity===false?'v63-bad':'v63-warn'));
      set('v63DbDetail',dbBusy?((maintenance.phase||'maintenance running')+(maintenance.actor?' · '+maintenance.actor:'')):(fmtBytes(db.size||0)+(db.free_bytes?' · '+fmtBytes(db.free_bytes)+' free':'')));
      const live=$('v63OpsLive');if(live)live.textContent='Updated '+new Date().toLocaleTimeString();status(true);
    }catch(e){status(false);const live=$('v63OpsLive');if(live)live.textContent='Metrics unavailable: '+e.message;}
    finally{busy=false;}
  };
  v63RefreshOps();v64ControlState();timer=setInterval(v63RefreshOps,3000);
  document.addEventListener('visibilitychange',()=>{if(document.hidden){if(timer)clearInterval(timer);timer=null;}else{v63RefreshOps();if(!timer)timer=setInterval(v63RefreshOps,3000);}});
})();
</script>

<script>
/* v66 advanced observability */
(function(){
  const $=id=>document.getElementById(id);
  async function run(){
    try{
      const jobs=[];
      {% if can_diagnostics %}jobs.push(fetch('/api/v66/quality/summary?limit=1',{cache:'no-store'}).then(r=>r.json()));{% endif %}
      {% if can_reports %}jobs.push(fetch('/api/v66/geo/map',{cache:'no-store'}).then(r=>r.json()));{% endif %}
      {% if can_security %}jobs.push(fetch('/api/v66/security/summary',{cache:'no-store'}).then(r=>r.json()));{% endif %}
      const d=await Promise.all(jobs); let i=0;
      {% if can_diagnostics %}{let q=d[i++];if(q.ok){let a=q.aggregate||{};if($('v66Quality'))$('v66Quality').textContent=(a.avg_quality||0).toFixed(1)+' / 100';if($('v66QualityDetail'))$('v66QualityDetail').textContent=(a.avg_latency_ms||0).toFixed(0)+'ms · '+(a.avg_fps||0).toFixed(1)+' FPS';}}{% endif %}
      {% if can_reports %}{let g=d[i++];if(g.ok){let n=(g.points||[]).length;if($('v66Geo'))$('v66Geo').textContent=n+' locations';}}{% endif %}
      {% if can_security %}{let s=d[i++];if(s.ok){if($('v66Security'))$('v66Security').textContent=s.active_sessions+' active';if($('v66SecurityDetail'))$('v66SecurityDetail').textContent=s.failed_logins_24h+' failed / 24h · max '+s.max_sessions_per_user;}}{% endif %}
      if($('v66State')){$('v66State').textContent='LIVE';$('v66State').className='stat-pill sp-on';}
    }catch(e){if($('v66State')){$('v66State').textContent='ERROR';$('v66State').className='stat-pill sp-off';}}
  }
  run();setInterval(()=>{if(!document.hidden)run()},30000);
})();
</script>

<!-- Single Feature Center: status/coverage only. Operational controls remain on the main dashboard. -->
<style>
.v43-modal{position:fixed;inset:0;background:rgba(0,0,0,.74);z-index:10070;display:none;overflow:auto}.v43-modal.open{display:block}.v43-panel{width:min(1400px,97vw);margin:3vh auto;background:#0f172a;color:#e5e7eb;border:1px solid #334155;border-radius:14px;box-shadow:0 20px 80px #000;padding:16px}.v43-head{display:flex;justify-content:space-between;gap:12px;align-items:center;position:sticky;top:0;background:#0f172a;padding-bottom:10px;z-index:2}.v43-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:10px}.v43-card{border:1px solid #334155;border-radius:10px;padding:12px;background:#111827}.v43-card h4{margin:0 0 8px;color:#93c5fd}.v43-card ul{margin:6px 0 0 18px;padding:0;max-height:240px;overflow:auto;font-size:12px;line-height:1.55}.v43-ok{color:#22c55e}.v43-warn{color:#f59e0b}.v43-small{font-size:11px;opacity:.72}.v43-status{padding:8px 10px;border-radius:7px;background:#020617;border:1px solid #334155;margin:8px 0;white-space:pre-wrap}.v43-out{background:#020617;border:1px solid #334155;border-radius:8px;padding:10px;white-space:pre-wrap;max-height:260px;overflow:auto;font:12px ui-monospace,SFMono-Regular,monospace}.v43-search{min-width:220px;flex:1;background:#020617;color:#e5e7eb;border:1px solid #334155;border-radius:7px;padding:7px}
</style>
<div id="v43FeatureModal" class="v43-modal" aria-hidden="true">
 <div class="v43-panel">
  <div class="v43-head"><div><strong>🧰 All Features — Priority 1–16 — v73</strong><div class="v43-small" id="v43FeatureSummary">Loading…</div></div><button class="btn" onclick="closeV43FeatureCenter()">✕ Close</button></div>
  <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:10px 0"><input id="v43FeatureSearch" class="v43-search" placeholder="Search Priority 1–16 features…" oninput="v43FilterFeatures()"><button class="btn btn-primary" onclick="v43RefreshCenter()">🔄 Refresh status</button></div>
  <div id="v43FeatureStatus" class="v43-status">Connecting…</div>
  <div id="v43Stats" class="v43-grid"></div>
  <div id="v43PriorityGrid" class="v43-grid"></div>
  <pre id="v43Output" class="v43-out">Ready.</pre>
 </div>
</div>
<script>
(function(){
  const q=id=>document.getElementById(id);
  window.openV43FeatureCenter=async function(){const m=q('v43FeatureModal');if(!m)return; m.classList.add('open');m.setAttribute('aria-hidden','false');await v43RefreshCenter();};
  window.closeV43FeatureCenter=function(){const m=q('v43FeatureModal');if(m){m.classList.remove('open');m.setAttribute('aria-hidden','true');}};
  window.v43Esc=function(x){return String(x??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));};
  window.v43Get=async function(url,opt){const r=await fetch(url,opt||{});const ct=(r.headers.get('content-type')||'').toLowerCase();let d;try{d=ct.includes('json')?await r.json():{ok:r.ok,detail:await r.text()};}catch(e){throw Error('Invalid response from '+url);}if(!r.ok||d.ok===false)throw Error(d.detail||('HTTP '+r.status));return d;};
  window.v43Status=function(msg,ok=true){const x=q('v43FeatureStatus');if(x){x.textContent=msg;x.style.borderColor=ok?'#166534':'#991b1b';x.style.color=ok?'#86efac':'#fca5a5';}};
  window.v43Output=function(v){const x=q('v43Output');if(x)x.textContent=typeof v==='string'?v:JSON.stringify(v,null,2);};
  window.v43FilterFeatures=function(){const t=(q('v43FeatureSearch')?.value||'').toLowerCase();document.querySelectorAll('#v43PriorityGrid .v43-card').forEach(c=>c.style.display=(!t||c.textContent.toLowerCase().includes(t))?'':'none');};
  window.v43RefreshCenter=async function(){
    try{
      v43Status('Loading live feature status…');
      const [d,a]=await Promise.all([v43Get('/api/v43/features'),v43Get('/api/v43/analytics')]);
      const suite=d.suite||{}, c=suite.counts||{}, n=a.analytics||{};
      const summary=q('v43FeatureSummary');if(summary)summary.textContent='v'+(suite.version||'?')+' • '+(c.cameras||0)+' cameras • '+(c.health_samples||0)+' health samples • '+(c.stream_candidates||0)+' stream candidates';
      const stats=q('v43Stats');if(stats)stats.innerHTML=Object.entries({Cameras:c.cameras||0,'Health Samples':c.health_samples||0,Snapshots:c.snapshots||0,'Stream Candidates':c.stream_candidates||0,Ports:c.ports||0,Online:n.online||0,Offline:n.offline||0,'Avg Latency':(n.avg_latency_ms??0)+' ms','Quality Avg':n.quality_avg??0}).map(([k,v])=>'<div class="v43-card"><div class="v43-small">'+v43Esc(k)+'</div><div style="font-size:20px;font-weight:700">'+v43Esc(v)+'</div></div>').join('');
      const grid=q('v43PriorityGrid');if(grid)grid.innerHTML=Object.entries(suite.priorities||{}).map(([k,items])=>'<div class="v43-card"><h4>'+v43Esc(k.replace(/^P\\d+_/,'').replaceAll('_',' ').toUpperCase())+'</h4><ul>'+items.map(x=>'<li class="v43-ok">✓ '+v43Esc(x)+'</li>').join('')+'</ul></div>').join('');
      v43FilterFeatures();v43Status('Feature Center connected. Operational controls are available directly on the main dashboard.',true);v43Output({feature_version:suite.version,counts:c,analytics:n});
    }catch(e){v43Status('Feature Center API error: '+e.message,false);v43Output(e.message);}
  };
})();
</script>

</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
# BACKGROUND TASKS
# ══════════════════════════════════════════════════════════════════════════════

def _scan_and_upsert(force: bool = False) -> list[dict]:
    allowed,storage=_storage_allows_write(16*1024*1024,'camera scan')
    if not allowed: raise RuntimeError(f"Low disk space: {storage.get('free_gb',0)} GB free; scan blocked")
    _SCAN_PAUSE.clear()
    started=utc_iso()
    # Claim the scan atomically. The old code checked running under the lock,
    # released it, and only then set running=True, allowing two callers to pass
    # the check at the same time.
    with _SCAN_STATE_LOCK:
        if _SCAN_STATE['running']:
            raise RuntimeError('Camera scan is already running')
        _SCAN_CANCEL.clear()
        _SCAN_STATE.update({
            'running': True, 'paused': False, 'cancel_requested': False,
            'force': bool(force), 'phase': 'discovering',
            'started_at': started, 'finished_at': '', 'discovered': 0,
            'processed': 0, 'added': 0, 'updated': 0, 'skipped': 0,
            'failed': 0, 'current_file': '', 'last_error': '',
            'updated_at': utc_iso(),
        })
    totals={'discovered':0,'processed':0,'added':0,'updated':0,'skipped':0,'failed':0}
    last_progress_log = [0.0]
    def batch_upsert(batch):
        if _SCAN_CANCEL.is_set(): return
        while _SCAN_PAUSE.is_set() and not _SCAN_CANCEL.is_set(): _scan_state_set(phase='paused',paused=True); time.sleep(.25)
        if _SCAN_CANCEL.is_set(): return
        _scan_state_set(phase='writing batch',paused=False,discovered=totals['discovered']+len(batch))
        ids=[str(x.get('id') or '') for x in batch if x.get('id')]; before=set()
        try:
            c=_db_conn()
            for pos in range(0,len(ids),400):
                chunk=ids[pos:pos+400]; ph=','.join('?'*len(chunk)); before.update(r['id'] for r in c.execute(f'SELECT id FROM cameras WHERE id IN ({ph})',chunk).fetchall())
            db_bulk_upsert(batch); totals['processed']+=len(batch); totals['discovered']+=len(batch); totals['added']+=sum(1 for x in ids if x not in before); totals['updated']+=sum(1 for x in ids if x in before)
            _scan_state_set(discovered=totals['discovered'],processed=totals['processed'],added=totals['added'],updated=totals['updated'])
            now = time.monotonic()
            if last_progress_log[0] == 0.0 or now - last_progress_log[0] >= 5.0:
                log.info('[Scanner] Live progress: records_seen=%d processed=%d new_camera_ids=%d existing_camera_ids_updated=%d failed=%d',
                         totals['discovered'], totals['processed'], totals['added'], totals['updated'], totals['failed'])
                last_progress_log[0] = now
        except Exception as exc:
            totals['failed']+=len(batch); _scan_state_set(failed=totals['failed'],last_error=str(exc)[:500]); raise
    try:
        find_all_cameras(_SEARCH_DIR,force=force,on_batch=batch_upsert,collect=False)
        for source_file,found_ids in _last_discovery_members.items():
            if _SCAN_CANCEL.is_set(): break
            while _SCAN_PAUSE.is_set() and not _SCAN_CANCEL.is_set(): time.sleep(.25)
            db_reconcile_source(source_file,found_ids)
        if not _SCAN_CANCEL.is_set(): db_prune_deleted(limit=1000,retention_days=DELETED_RETENTION_DAYS)
        finished=utc_iso(); _scan_state_set(running=False,phase='cancelled' if _SCAN_CANCEL.is_set() else 'complete',cancel_requested=_SCAN_CANCEL.is_set(),finished_at=finished,last_scan_at=finished,**totals)
        log.info('[Scanner] %s: records_seen=%d processed=%d new_camera_ids=%d existing_camera_ids_updated=%d skipped=%d failed=%d',
                 'Cancelled' if _SCAN_CANCEL.is_set() else 'Complete',
                 totals['discovered'], totals['processed'], totals['added'],
                 totals['updated'], totals['skipped'], totals['failed'])
        try: _db_retry_write(lambda: _db_conn().execute('INSERT INTO scan_history(started_at,finished_at,force_scan,discovered,processed,added,updated,skipped,failed,detail) VALUES(?,?,?,?,?,?,?,?,?,?)',(started,finished,int(force),totals['discovered'],totals['processed'],totals['added'],totals['updated'],totals['skipped'],totals['failed'],'cancelled' if _SCAN_CANCEL.is_set() else 'ok')).connection.commit(),'scan history')
        except Exception: pass
        return []
    except Exception as exc:
        _scan_state_set(running=False,phase='error',finished_at=utc_iso(),last_error=str(exc)[:500],**totals); raise


def _background_scanner() -> None:
    """Re-scan camera files every 5 minutes to pick up new/changed playlists."""
    _touch_worker('file-scanner', 'starting')
    if not _service_enabled('file-scanner'):
        _touch_worker('file-scanner','stopped','disabled by operator'); return
    time.sleep(300 + random.uniform(0,30))
    while not _shutdown_requested.is_set():
        if not _service_enabled('file-scanner'):
            _touch_worker('file-scanner','stopped','disabled by operator'); return
        try:
            _touch_worker('file-scanner', 'running')
            _scan_and_upsert()
            _touch_worker('file-scanner', 'idle')
        except RuntimeError as e:
            if str(e) == 'Camera scan is already running':
                _touch_worker('file-scanner', 'idle', 'scan already running; skipped duplicate cycle')
                log.debug('[Scanner] Duplicate scheduled scan skipped; another scan is already running')
            else:
                log.warning('Background scanner error: %s', _mask_rtsp(str(e)))
                _touch_worker('file-scanner', 'error', type(e).__name__)
        except Exception as e:
            log.warning('Background scanner error: %s', _mask_rtsp(str(e)))
            _touch_worker('file-scanner', 'error', type(e).__name__)
        _shutdown_requested.wait(300 + random.uniform(0,30))


# ══════════════════════════════════════════════════════════════════════════════
# AUTHENTICATED LOCAL DEVICE ADAPTERS
# ══════════════════════════════════════════════════════════════════════════════

def _validate_device_host(host: str) -> str:
    host = (host or '').strip()
    if host.startswith('[') and host.endswith(']'):
        host = host[1:-1].strip()
    if not re.fullmatch(r'[A-Za-z0-9._:%-]{1,253}', host):
        raise ValueError('Device host must be a hostname, IPv4, or IPv6 address')
    if any(ch in host for ch in '/?#@'):
        raise ValueError('Device host must not contain a URL path or credentials')
    return host


def _device_target(host: str, port, username: str, password: str) -> tuple[str, int | str]:
    """Validate one explicitly supplied device target.

    This deliberately accepts one host only. It never expands a CIDR, downloads
    target ranges, guesses credentials, or probes neighboring addresses.
    """
    host = _validate_device_host(host)
    # Username/password are required for authenticated device login, but a
    # supplied RTSP/M3U stream may legitimately be anonymous. The caller can
    # validate the exact stream without credentials.
    if not username:
        return host, ('auto' if str(port).strip().lower() in ('auto', 'scan', 'automatic') else int(port or 80))
    if str(port).strip().lower() in ('auto', 'scan', 'automatic'):
        return host, 'auto'
    try:
        port = int(port or 80)
    except (TypeError, ValueError):
        raise ValueError('Device port must be numeric')
    if not 1 <= port <= 65535:
        raise ValueError('Device port must be between 1 and 65535')
    return host, port


def _probe_http_service(host: str, port: int, username: str = '', password: str = '') -> dict:
    """Fast read-only HTTP/HTTPS probe for one explicitly supplied host/port."""
    result = {
        'port': int(port), 'http': False, 'https': False,
        'status': None, 'scheme': '', 'auth': 'not-tested',
        'server': '', 'content_type': '', 'title': '', 'camera_hint': '',
    }
    if _requests is None:
        return result
    try:
        sess, auths = _device_session(username, password)
    except Exception:
        return result
    for scheme in (('https', 'http') if int(port) in DEVICE_HTTPS_PORTS else ('http', 'https')):
        url_host = f'[{host}]' if ':' in host and not host.startswith('[') else host
        url = f'{scheme}://{url_host}:{int(port)}/'
        try:
            response = _device_get(sess, auths, url, timeout=DEVICE_HTTP_PROBE_TIMEOUT)
            if not hasattr(response, 'status_code'):
                continue
            code = int(response.status_code)
            if code < 600:
                result[scheme] = True
            if result['status'] is None or code == 200:
                result['status'] = code
                result['scheme'] = scheme
            result['server'] = str(response.headers.get('Server') or '')[:160]
            result['content_type'] = str(response.headers.get('Content-Type') or '')[:160]
            body = (response.text or '')[:8000]
            low = body.lower()
            title_match = re.search(r'<title[^>]*>(.*?)</title>', body, re.I | re.S)
            if title_match:
                result['title'] = re.sub(r'\s+', ' ', title_match.group(1)).strip()[:160]
            markers = (
                ('hikvision', ('hikvision', 'isapi', '<deviceinfo')),
                ('dahua', ('dahua', 'anjhua', 'magicbox', 'global.login')),
                ('uniview', ('uniview', 'unv', 'sysinfoservice')),
                ('axis', ('axis communications', 'axis-cgi', 'axis camera')),
                ('reolink', ('reolink',)), ('amcrest', ('amcrest',)),
                ('foscam', ('foscam',)), ('hanwha', ('hanwha', 'wisenet',)),
            )
            for name, hints in markers:
                if any(h in low for h in hints) or any(h in result['server'].lower() for h in hints):
                    result['camera_hint'] = name
                    break
            if code in (200, 204):
                result['auth'] = 'authenticated-or-public'
            elif code in (401, 403):
                result['auth'] = 'authentication-required'
            else:
                result['auth'] = f'http-{code}'
            # A successful response is enough; don't wait for the second scheme.
            if code in (200, 204, 401, 403):
                break
        except Exception:
            continue
    return result

_DEVICE_HTTP_SCAN_CACHE: dict[tuple, tuple[float, dict]] = {}
_DEVICE_HTTP_SCAN_CACHE_LOCK = threading.RLock()
_DEVICE_HTTP_SCAN_CACHE_TTL = 30.0


def _probe_device_http_ports(host: str, ports=DEVICE_HTTP_PORTS,
                             username: str = '', password: str = '') -> dict:
    """Probe all bounded ports concurrently so the Replit proxy is not held open for 30+ seconds."""
    host = _validate_device_host(host)
    requested = tuple(dict.fromkeys(int(p) for p in ports))
    cache_key = (host, requested, str(username or ''), str(password or ''))
    now = time.monotonic()
    with _DEVICE_HTTP_SCAN_CACHE_LOCK:
        cached = _DEVICE_HTTP_SCAN_CACHE.get(cache_key)
        if cached and now - cached[0] < _DEVICE_HTTP_SCAN_CACHE_TTL:
            return copy.deepcopy(cached[1])
    allowed = set(DEVICE_HTTP_PORTS)
    if any(p not in allowed for p in requested):
        raise ValueError('HTTP port scan contains an unsupported port')

    def tcp_probe(port):
        started = time.monotonic()
        item = {'port': port,
                'scheme': ('https' if port in DEVICE_HTTPS_PORTS else
                           'rtsp' if port in DEVICE_RTSP_PORTS else 'tcp'),
                'reachable': False, 'latency_ms': 0}
        try:
            with socket.create_connection((host, port), timeout=DEVICE_PORT_SCAN_TIMEOUT):
                item['reachable'] = True
        except (OSError, ValueError):
            pass
        item['latency_ms'] = round((time.monotonic() - started) * 1000)
        return item

    with ThreadPoolExecutor(max_workers=min(DEVICE_MAX_PROBE_WORKERS, max(1, len(requested)))) as ex:
        results = list(ex.map(tcp_probe, requested))
    reachable = [x for x in results if x['reachable']]

    def http_probe(item):
        return _probe_http_service(host, item['port'], username, password)

    http_services = []
    if reachable and _requests is not None:
        with ThreadPoolExecutor(max_workers=min(DEVICE_MAX_PROBE_WORKERS, len(reachable))) as ex:
            for service in ex.map(http_probe, reachable):
                if service.get('status') is not None and (service.get('http') or service.get('https')):
                    http_services.append(service)

    payload = {
        'host': host,
        'ports': [x['port'] for x in results],
        'reachable': reachable,
        'selected_port': reachable[0]['port'] if reachable else None,
        'results': results,
        'http_services': http_services,
        'http_ports': [int(x['port']) for x in http_services],
        'probe_duration_ms': round(max((x.get('latency_ms', 0) for x in results), default=0)),
    }
    with _DEVICE_HTTP_SCAN_CACHE_LOCK:
        _DEVICE_HTTP_SCAN_CACHE[cache_key] = (time.monotonic(), copy.deepcopy(payload))
        if len(_DEVICE_HTTP_SCAN_CACHE) > 512:
            cutoff = time.monotonic() - _DEVICE_HTTP_SCAN_CACHE_TTL
            for key, (stamp, _) in list(_DEVICE_HTTP_SCAN_CACHE.items()):
                if stamp < cutoff:
                    _DEVICE_HTTP_SCAN_CACHE.pop(key, None)
    return payload

def _resolve_device_port(host: str, port, username: str = '', password: str = '',
                        vendor: str = 'auto') -> tuple[int, dict]:
    """Resolve the management/web port without treating any open TCP port as the NVR.

    An IP can have multiple forwarded services (for example :80 -> a door
    controller/camera and :554 -> the NVR).  The old implementation selected
    the first reachable port, which could incorrectly turn an unrelated web
    service into the CCTV device.  In auto mode, prefer ports that answer a
    known CCTV/NVR management API with the supplied credentials.
    """
    if str(port).strip().lower() not in ('auto', 'scan', 'automatic'):
        return int(port or 80), {}

    scan = _probe_device_http_ports(host, username=username, password=password)
    reachable = [int(item['port']) for item in scan.get('reachable', [])]
    if not reachable:
        raise RuntimeError(
            'HTTP port unreachable; checked ' +
            ', '.join(str(p) for p in scan['ports']))

    # If a reachable HTTP service already identified itself as a camera/web service,
    # prefer that port before falling back to raw TCP order. This prevents a door
    # camera on :80 from being displaced by an unrelated open TCP service.
    services = scan.get('http_services') or []
    hinted = [x for x in services if x.get('camera_hint')]
    if hinted:
        preferred = hinted[0]
        scan['selected_port'] = int(preferred['port'])
        scan['selection_reason'] = 'camera_http_service_hint'
        return int(preferred['port']), scan
    successful_http = [x for x in services if x.get('status') in (200, 204)]
    if successful_http:
        preferred = successful_http[0]
        scan['selected_port'] = int(preferred['port'])
        scan['selection_reason'] = 'authenticated_or_public_http_service'
        return int(preferred['port']), scan

    if username and password and _requests is not None:
        deadline = time.monotonic() + float(os.getenv('CAMERA_DEVICE_RESOLVE_MAX_SECONDS','30'))
        try:
            sess, auths = _device_session(username, password)
            hint = _normalize_vendor_hint(vendor)
            # Test only the explicitly supplied host and the bounded ports already
            # discovered above.  No neighboring hosts/ranges are touched.
            for candidate in reachable:
                if time.monotonic() >= deadline: break
                url_host = f'[{host}]' if ':' in host and not host.startswith('[') else host
                bases = [f'https://{url_host}:{candidate}', f'http://{url_host}:{candidate}']
                for base in bases:
                    if time.monotonic() >= deadline: break
                    if hint in ('auto', 'hikvision', 'hik'):
                        r = _device_get(sess, auths, base + '/ISAPI/System/deviceInfo', timeout=min(3.0, max(0.1, deadline-time.monotonic())))
                        if hasattr(r, 'status_code') and r.status_code == 200:
                            scan['selected_port'] = candidate
                            scan['selection_reason'] = 'hikvision_api'
                            return candidate, scan
                    if hint in ('auto', 'dahua', 'anjhua', 'nvr'):
                        r = _device_get(sess, auths, base + '/cgi-bin/magicBox.cgi?action=getSystemInfo', timeout=min(3.0, max(0.1, deadline-time.monotonic())))
                        if hasattr(r, 'status_code') and r.status_code == 200:
                            scan['selected_port'] = candidate
                            scan['selection_reason'] = 'dahua_api'
                            return candidate, scan
                    if hint in ('auto', 'uniview', 'nvr'):
                        body = ('<?xml version=\"1.0\" encoding=\"utf-8\"?>'
                                '<soap:Envelope xmlns:soap=\"http://schemas.xmlsoap.org/soap/envelope/\">'
                                '<soap:Body><c:GetDeviceInfo xmlns:c=\"http://www.uniview.com/2014/07\"></c:GetDeviceInfo>'
                                '</soap:Body></soap:Envelope>')
                        r = _device_soap_request(sess, auths, base, '/webapi/system/SysInfoService?wsdl', body, timeout=min(3.0,max(0.1,deadline-time.monotonic())))
                        if hasattr(r, 'status_code') and r.status_code == 200:
                            scan['selected_port'] = candidate
                            scan['selection_reason'] = 'uniview_api'
                            return candidate, scan
                    if hint in ('auto', 'onvif', 'nvr'):
                        r = _device_onvif_request(sess, auths, base, '<tds:GetDeviceInformation xmlns:tds=\"http://www.onvif.org/ver10/device/wsdl\"/>')
                        if hasattr(r, 'status_code') and r.status_code == 200:
                            scan['selected_port'] = candidate
                            scan['selection_reason'] = 'onvif_api'
                            return candidate, scan
        except Exception:
            # API fingerprinting is an enhancement; the bounded TCP results are
            # still returned below.
            pass

    # No vendor API was positively identified. Do NOT choose port 80 merely
    # because it is open: on many installations port 80 belongs to a door
    # station/door camera while the NVR is exposed on 8000/8080/8443/etc.
    # Prefer conventional CCTV/NVR management ports and put port 80 last.
    # RTSP ports are never selected as HTTP management ports.
    preferred = (8000, 8080, 8081, 8082, 8083, 8084, 8085, 8200,
                 8443, 443, 8899, 8888, 9000, 9001, 8090, 8091,
                 81, 82, 83, 7080, 6789, 3000, 4000, 4321, 4567,
                 1024, 1025, 1050, 2000, 2001, 34567, 37777, 37778,
                 37779, 18080, 18081, 50000, 50001, 80)
    ordered = [p for p in preferred if p in reachable and p not in DEVICE_RTSP_PORTS]
    if ordered:
        selected = ordered[0]
        scan['selected_port'] = selected
        scan['selection_reason'] = 'cctv_management_port_priority'
        scan['warning'] = (
            'No vendor API fingerprint was confirmed; selected a conventional '
            'CCTV management port. Port 80 was deprioritized because it may '
            'belong to a door station/camera on the same public IP.')
        return selected, scan

    # If the only reachable service is port 80, keep it available as a last
    # resort, but explicitly mark it as unverified instead of presenting it as
    # the NVR management port.
    non_rtsp = [p for p in reachable if p not in DEVICE_RTSP_PORTS]
    if non_rtsp:
        selected = non_rtsp[0]
        scan['selected_port'] = selected
        scan['selection_reason'] = 'unverified_management_port'
        scan['warning'] = (
            'No CCTV/NVR management API was positively identified on the '
            'reachable ports.')
        return selected, scan

    raise RuntimeError(
        'RTSP is reachable, but no separate CCTV/NVR management port was identified')


def _infer_vendor_from_rtsp_url(rtsp_url: str) -> str:
    """Infer a CCTV vendor hint from an explicitly supplied RTSP path.

    This is only a hint for the same user-supplied device; it does not scan or
    contact any additional host.
    """
    text = (rtsp_url or '').lower()
    if '/streaming/channels/' in text or '/psia/streaming/' in text or '/h264/ch' in text:
        return 'hikvision'
    if '/cam/realmonitor' in text or '/h264preview_' in text or 'magicbox' in text:
        return 'dahua'
    if '/media/video' in text or '/media/video' in text:
        return 'uniview'
    return ''


def _normalize_vendor_hint(vendor: str) -> str:
    hint = (vendor or 'auto').strip().lower()
    if hint in ('auto', ''):
        return 'auto'
    if any(value in hint for value in ('hikvision', 'hik', 'hivision')):
        return 'hikvision'
    if 'uniview' in hint or re.search(r'\bunv\b', hint):
        return 'uniview'
    if 'nvr' in hint or 'dvr' in hint or 'xmeye' in hint:
        return 'nvr'
    if any(value in hint for value in ('dahua', 'dahau', 'anjhua',
                                       'amcrest')):
        return 'dahua'
    if 'axis' in hint:
        return 'axis'
    if any(value in hint for value in ('onvif', 'bosch', 'hanwha',
                                       'samsung', 'wisenet', 'vivotek')):
        return 'onvif'
    if any(value in hint for value in ('reolink', 'tiandy', 'foscam',
                                       'generic', 'camera', 'stream')):
        return 'generic'
    return hint


def _device_session(username: str, password: str):
    if _requests is None:
        raise RuntimeError('requests library is not installed')
    from requests.auth import HTTPBasicAuth, HTTPDigestAuth
    sess = _requests.Session()
    sess.verify = CAMERA_VERIFY_TLS
    sess.headers.update({'User-Agent': 'CameraDashboard-LocalDevice/1.0', 'Accept-Encoding': 'identity'})
    return sess, [HTTPDigestAuth(username, password),
                  HTTPBasicAuth(username, password)]


def _device_get(sess, auths, url: str, timeout: float = DEVICE_DISCOVERY_TIMEOUT):
    """Try digest then basic auth for one explicit read-only request."""
    last = None
    for auth in auths:
        try:
            response = sess.get(url, auth=auth, timeout=timeout,
                                allow_redirects=False)
            last = response
            if response.status_code not in (401, 403):
                return response
        except Exception as exc:
            last = exc
    return last


def _xml_local_text(root, name: str) -> str:
    for elem in root.iter():
        if elem.tag.rsplit('}', 1)[-1].lower() == name.lower():
            return (elem.text or '').strip()
    return ''


def _device_info_from_hik(root) -> dict:
    return {
        'model': _xml_local_text(root, 'model'),
        'serial': _xml_local_text(root, 'serialNumber'),
        'firmware': _xml_local_text(root, 'firmwareVersion'),
        'device_name': _xml_local_text(root, 'deviceName'),
    }


def _device_info_from_dahua(text: str) -> dict:
    result = {}
    for line in text.splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            key = key.strip().lower()
            value = value.strip()
            if key.endswith(('devicetype', 'device.type')):
                result['model'] = value
            elif key.endswith(('serialnumber', 'serialno')):
                result['serial'] = value
            elif key.endswith(('version', 'softwareversion')):
                result['firmware'] = value
            elif key.endswith(('deviceName', 'name')):
                result['device_name'] = value
    if not result and text.strip():
        result['raw_summary'] = ' '.join(text.split())[:300]
    return result


def _onvif_soap(body: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
        '<s:Body>' + body + '</s:Body></s:Envelope>')


def _device_onvif_request(sess, auths, base_url: str, body: str, timeout: float = 8.0):
    if _requests is None:
        return None
    endpoint = base_url.rstrip('/') + '/onvif/device_service'
    headers = {'Content-Type': 'application/soap+xml;charset=UTF-8'}
    last = None
    for auth in auths:
        try:
            response = sess.post(endpoint, data=_onvif_soap(body),
                                 headers=headers, auth=auth, timeout=timeout)
            last = response
            if response.status_code not in (401, 403):
                return response
        except Exception as exc:
            last = exc
    return last


def _device_soap_request(sess, auths, base_url: str, endpoint: str,
                         body: str, timeout: float = 8.0):
    """Send one authenticated, read-only SOAP request to an explicit device."""
    if _requests is None:
        return None
    target = base_url.rstrip('/') + endpoint
    headers = {'Content-Type': 'application/soap+xml;charset=UTF-8'}
    last = None
    for auth in auths:
        try:
            response = sess.post(target, data=body, headers=headers, auth=auth,
                                 timeout=timeout)
            last = response
            if response.status_code not in (401, 403):
                return response
        except Exception as exc:
            last = exc
    return last


def _rtsp_describe_verified(host: str, port: int, path: str,
                            username: str, password: str,
                            timeout: float = 1.5) -> bool:
    """Confirm one RTSP path with supplied credentials.

    Only a real RTSP 200 response is accepted. A 401 response is deliberately
    not treated as channel discovery because many recorders return it for
    nonexistent channel numbers too.
    """
    if not path.startswith('/'):
        path = '/' + path
    uri = f'rtsp://{host}:{port}{path}'
    basic = ''
    if username:
        import base64
        basic = 'Authorization: Basic ' + base64.b64encode(
            f'{username}:{password}'.encode()).decode() + '\r\n'

    def request_once(extra: str, cseq: int = 1):
        try:
            with socket.create_connection((host, int(port)), timeout=timeout) as sock:
                sock.sendall((
                    f'DESCRIBE {uri} RTSP/1.0\r\nCSeq: {cseq}\r\n'
                    f'{extra}User-Agent: CameraDashboard/1.0\r\n\r\n'
                ).encode())
                return sock.recv(1024).decode(errors='ignore')
        except (OSError, ValueError):
            return ''

    response = request_once(basic)
    if 'RTSP/1.0 200' in response:
        return True
    challenge = re.search(
        r'WWW-Authenticate:\s*Digest\s+([^\r\n]+)', response, re.I)
    if not challenge:
        return False
    values = dict(re.findall(r'(\w+)=["]?([^",\s]+)["]?',
                             challenge.group(1)))
    realm, nonce = values.get('realm'), values.get('nonce')
    if not realm or not nonce:
        return False
    ha1 = hashlib.md5(f'{username}:{realm}:{password}'.encode()).hexdigest()
    ha2 = hashlib.md5(f'DESCRIBE:{uri}'.encode()).hexdigest()
    digest = hashlib.md5(f'{ha1}:{nonce}:{ha2}'.encode()).hexdigest()
    auth = (
        f'Authorization: Digest username="{username}", realm="{realm}", '
        f'nonce="{nonce}", uri="{uri}", response="{digest}"\r\n')
    return 'RTSP/1.0 200' in request_once(auth, 2)


def _device_geo(host: str, enabled: bool = False) -> dict:
    """Look up approximate location for a public device IP using HTTPS fallbacks.

    A reachable/live camera is not necessarily geolocatable: private RFC1918,
    loopback, link-local and other non-global addresses do not encode physical
    location. Never substitute the dashboard server's own public IP for the
    camera's IP, because that would silently report the hosting provider's site.
    """
    if not enabled:
        return {'geo_source': 'disabled', 'geo_confidence': 'none'}
    if _requests is None:
        return {'geo_source': 'dependency-missing', 'geo_confidence': 'none',
                'detail': 'The requests package is unavailable; install project requirements.'}

    import ipaddress
    target = str(host or '').strip().strip('[]')
    try:
        addr = ipaddress.ip_address(target)
    except ValueError:
        # Hostnames are allowed only when they resolve to a globally routable IP.
        try:
            resolved = []
            for item in socket.getaddrinfo(target, None, type=socket.SOCK_STREAM):
                try:
                    candidate = ipaddress.ip_address(item[4][0].split('%', 1)[0])
                    if candidate.is_global and candidate not in resolved:
                        resolved.append(candidate)
                except ValueError:
                    continue
            addr = resolved[0] if resolved else None
        except (OSError, TypeError):
            addr = None
        if addr is None:
            return {'geo_source': 'not-publicly-geolocatable', 'geo_confidence': 'none',
                    'detail': 'This device address is a private/local IP or did not resolve to a public IP. A live connection does not provide GPS coordinates. Enter the device site manually, or use a public IP assigned to the camera site.'}
    if not addr.is_global:
        return {'geo_source': 'private-network', 'geo_confidence': 'none',
                'detail': 'This is not a globally routable public IP. Public-IP geolocation cannot determine the camera site from a private/local address; use the saved site location or enter it manually.'}

    ip = str(addr)
    # Provider schemas differ. Try several HTTPS services, stopping at the first
    # valid result containing at least one usable geographic field.
    providers = (
        ('ipapi.co', f'https://ipapi.co/{quote(ip, safe="")}/json/'),
        ('ipwho.is', f'https://ipwho.is/{quote(ip, safe="")}'),
        ('ipinfo.io', f'https://ipinfo.io/{quote(ip, safe="")}/json'),
    )
    failures = []
    for provider, url in providers:
        try:
            response = _requests.get(url, timeout=(2.5, 3.5),
                                     headers={'User-Agent': 'CameraDashboard/178.0'})
            if not response.ok:
                failures.append(f'{provider}: HTTP {response.status_code}')
                continue
            data = response.json()
            if not isinstance(data, dict):
                failures.append(f'{provider}: invalid response')
                continue
            if data.get('error') or data.get('success') is False:
                failures.append(f'{provider}: lookup rejected')
                continue
            connection = data.get('connection') if isinstance(data.get('connection'), dict) else {}
            country = str(data.get('country_name') or data.get('country') or '').strip()
            region = str(data.get('region') or data.get('region_name') or '').strip()
            city = str(data.get('city') or '').strip()
            isp = str(data.get('org') or data.get('isp') or connection.get('isp') or '').strip()
            asn = str(data.get('asn') or connection.get('asn') or '').strip()
            # ipinfo returns an ISO country code, not a country name.
            if len(country) == 2 and country.isalpha():
                try:
                    import pycountry
                    country_obj = pycountry.countries.get(alpha_2=country.upper())
                    if country_obj:
                        country = country_obj.name
                except ImportError:
                    country = country.upper()
            if any((city, region, country)):
                return {'country': country, 'region': region, 'city': city,
                        'isp': isp, 'asn': asn, 'geo_source': provider,
                        'geo_confidence': 'approximate', 'ip': ip,
                        'detail': 'Approximate public-IP geolocation; not device GPS.'}
            failures.append(f'{provider}: no geographic fields')
        except Exception as exc:
            failures.append(f'{provider}: {type(exc).__name__}')
            log.debug('Geolocation provider %s failed: %s', provider, type(exc).__name__)

    return {'geo_source': 'lookup-failed', 'geo_confidence': 'none', 'ip': ip,
            'detail': 'No geolocation provider returned a usable city, region, or country. ' + '; '.join(failures)}


def discover_local_device(host: str, port, username: str, password: str,
                          vendor: str = 'auto', rtsp_port: int = 554,
                          enrich_geo: bool = False, country: str = '',
                          region: str = '', city: str = '',
                          source_file: str = 'device://local-authenticated',
                          rtsp_url: str = '') -> dict:
    """Read-only discovery for one explicitly supplied CCTV device."""
    host, port = _device_target(host, port, username, password)
    supplied_rtsp = (rtsp_url or '').strip()
    effective_vendor = vendor or 'auto'
    inferred_vendor = _infer_vendor_from_rtsp_url(supplied_rtsp)
    if _normalize_vendor_hint(effective_vendor) in ('auto', 'generic') and inferred_vendor:
        effective_vendor = inferred_vendor
    port, http_probe = _resolve_device_port(
        host, port, username, password, effective_vendor)
    # In auto mode _resolve_device_port already performed the complete bounded
    # HTTP/HTTPS matrix. In manual mode run the same matrix as well so Login/Test
    # never treats port 80 as the only HTTP service.
    if not http_probe.get('http_services'):
        http_probe = _probe_device_http_ports(
            host, DEVICE_HTTP_PORTS, username=username, password=password)
        http_probe['selected_port'] = port
        http_probe['selection_reason'] = 'manual_port_with_full_http_probe'
    try:
        rtsp_port = int(rtsp_port or 554)
    except (TypeError, ValueError):
        raise ValueError('RTSP port must be numeric')
    if not 1 <= rtsp_port <= 65535:
        raise ValueError('RTSP port must be between 1 and 65535')
    sess, auths = _device_session(username, password)
    base_urls = [f'https://{host}:{port}', f'http://{host}:{port}']
    vendor_hint = _normalize_vendor_hint(vendor)
    info, detected = {}, ''
    for base in base_urls:
        if vendor_hint in ('auto', 'hikvision', 'hik'):
            response = _device_get(sess, auths, base + '/ISAPI/System/deviceInfo')
            if hasattr(response, 'status_code') and response.status_code == 200:
                try:
                    info = _device_info_from_hik(ElementTree.fromstring(response.content))
                except ElementTree.ParseError:
                    info = {'raw_summary': response.text[:300]}
                detected = 'hikvision'
                break
        if vendor_hint in ('auto', 'dahua', 'anjhua', 'nvr'):
            response = _device_get(
                sess, auths,
                base + '/cgi-bin/magicBox.cgi?action=getSystemInfo')
            if hasattr(response, 'status_code') and response.status_code == 200:
                info = _device_info_from_dahua(response.text)
                detected = 'dahua'
                break
    if not detected and vendor_hint in ('auto', 'uniview', 'nvr'):
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
            '<soap:Body><c:GetDeviceInfo xmlns:c="http://www.uniview.com/2014/07">'
            '</c:GetDeviceInfo></soap:Body></soap:Envelope>')
        for base in base_urls:
            response = _device_soap_request(
                sess, auths, base, '/webapi/system/SysInfoService?wsdl', body)
            if hasattr(response, 'status_code') and response.status_code == 200:
                try:
                    root = ElementTree.fromstring(response.content)
                    info = {
                        'model': _xml_local_text(root, 'ModelName') or
                                 _xml_local_text(root, 'Model'),
                        'firmware': _xml_local_text(root, 'SoftwareVersion'),
                        'serial': _xml_local_text(root, 'SerialNumber'),
                    }
                except ElementTree.ParseError:
                    info = {'raw_summary': response.text[:300]}
                detected = 'uniview'
                break
    if not detected and vendor_hint in ('auto', 'axis', 'nvr'):
        for base in base_urls:
            response = _device_get(
                sess, auths, base + '/axis-cgi/streams/list.json')
            if hasattr(response, 'status_code') and response.status_code == 200:
                try:
                    payload = response.json()
                except (ValueError, AttributeError):
                    payload = {}
                if isinstance(payload, dict) and 'apiVersion' in payload:
                    brand = _device_get(
                        sess, auths,
                        base + '/axis-cgi/param.cgi?action=list&group=root.Brand')
                    info = {}
                    if hasattr(brand, 'status_code') and brand.status_code == 200:
                        for line in brand.text.splitlines():
                            if '=' in line:
                                key, value = line.split('=', 1)
                                key = key.rsplit('.', 1)[-1].lower()
                                if key in ('prodfullname', 'prodshortname'):
                                    info['model'] = value.strip()
                                elif key in ('prodnumber', 'serialnumber'):
                                    info['serial'] = value.strip()
                    detected = 'axis'
                    break
    if not detected and vendor_hint in ('auto', 'onvif', 'nvr'):
        body = '<tds:GetDeviceInformation xmlns:tds="http://www.onvif.org/ver10/device/wsdl"/>'
        for base in base_urls:
            response = _device_onvif_request(sess, auths, base, body)
            if hasattr(response, 'status_code') and response.status_code == 200:
                try:
                    root = ElementTree.fromstring(response.content)
                    info = {
                        'manufacturer': _xml_local_text(root, 'Manufacturer'),
                        'model': _xml_local_text(root, 'Model'),
                        'firmware': _xml_local_text(root, 'FirmwareVersion'),
                        'serial': _xml_local_text(root, 'SerialNumber'),
                        'device_name': _xml_local_text(root, 'HardwareId'),
                    }
                except ElementTree.ParseError:
                    info = {'raw_summary': response.text[:300]}
                detected = 'onvif'
                break
    if not detected and vendor_hint in ('auto', 'generic'):
        for base in base_urls:
            response = _device_get(sess, auths, base + '/')
            if hasattr(response, 'status_code') and response.status_code == 200:
                info = {'raw_summary': 'Authenticated web endpoint'}
                detected = 'generic'
                break
    # A device can legitimately expose RTSP while its HTTP API is unknown,
    # vendor-customized, disabled, or on a different management port.  If the
    # user supplied an RTSP URL, verify that exact path with the supplied
    # credentials and import it instead of rejecting an authenticated device
    # merely because vendor fingerprinting failed.
    if not detected and supplied_rtsp.lower().startswith(('rtsp://','rtsps://')):
        parsed = urlsplit(supplied_rtsp)
        rtsp_host = parsed.hostname or host
        rtsp_user = unquote(parsed.username or username)
        rtsp_pass = unquote(parsed.password or password)
        rtsp_p = int(parsed.port or rtsp_port)
        rtsp_path = parsed.path or '/'
        if parsed.query:
            rtsp_path += '?' + parsed.query
        if rtsp_host != host:
            raise RuntimeError('Supplied RTSP URL host does not match the device host')
        rtsp_verified = _rtsp_describe_verified(rtsp_host, rtsp_p, rtsp_path,
                                                  rtsp_user, rtsp_pass)
        detected = 'generic_rtsp' if rtsp_verified else ''
        info = {'raw_summary': ('Authenticated RTSP endpoint' if rtsp_verified
                                else 'Supplied RTSP endpoint pending validation')}
        username, password, rtsp_port = rtsp_user, rtsp_pass, rtsp_p
        channels = [{
            'index': 1,
            'name': ('Supplied RTSP Stream' if rtsp_verified else 'Supplied RTSP Stream (pending)'),
            'rtsp_urls': [supplied_rtsp],
        }]
    else:
        channels = []

    if not detected:
        for base in base_urls:
            try:
                response = _device_get(sess, auths, base + '/')
                if hasattr(response, 'status_code') and response.status_code == 200:
                    detected = 'generic'
                    info = info or {'raw_summary': 'Authenticated generic web endpoint'}
                    break
            except Exception:
                continue
    if not detected and channels:
        detected = 'generic_rtsp_pending'
        info = info or {'raw_summary': 'RTSP endpoint pending validation'}
    if not detected:
        raise RuntimeError('Authenticated device identification failed')
    http_base = f'http://{host}:{port}'
    if detected in ('generic_rtsp', 'generic_rtsp_pending'):
        pass
    elif detected == 'hikvision':
        response = _device_get(sess, auths, http_base + '/ISAPI/Streaming/channels')
        if hasattr(response, 'status_code') and response.status_code == 200:
            try:
                root = ElementTree.fromstring(response.content)
                ids = []
                for elem in root.iter():
                    if elem.tag.rsplit('}', 1)[-1].lower() == 'id':
                        try:
                            ids.append(int((elem.text or '').strip()))
                        except ValueError:
                            pass
                grouped = {}
                for channel_id in sorted(set(ids)):
                    base_channel = (channel_id // 100) if channel_id >= 100 else channel_id
                    grouped.setdefault(base_channel, []).append(
                        f'rtsp://{quote(username, safe="")}:{quote(password, safe="")}@'
                        f'{host}:{rtsp_port}/Streaming/Channels/{channel_id}')
                for base_channel, rtsp_urls in sorted(grouped.items()):
                    channels.append({
                        'index': base_channel,
                        'name': f'Channel {base_channel}',
                        'rtsp_urls': rtsp_urls,
                    })
            except ElementTree.ParseError:
                pass
    elif detected == 'dahua':
        response = _device_get(
            sess, auths,
            http_base + '/cgi-bin/configManager.cgi?action=getConfig&name=Encode')
        max_channel = 0
        if hasattr(response, 'status_code') and response.status_code == 200:
            values = re.findall(r'(?:Encode|Channel)[^\n\[]*\[?(\d+)\]?', response.text,
                                flags=re.IGNORECASE)
            if values:
                max_channel = max(int(v) for v in values)
        if max_channel:
            max_channel = min(max_channel + 1, 128)
            for index in range(1, max_channel + 1):
                prefix = f'rtsp://{quote(username, safe="")}:{quote(password, safe="")}@'
                channels.append({
                    'index': index,
                    'name': f'Channel {index}',
                    'rtsp_urls': [
                        f'{prefix}{host}:{rtsp_port}/cam/realmonitor?channel={index}&subtype=0',
                        f'{prefix}{host}:{rtsp_port}/cam/realmonitor?channel={index}&subtype=1',
                    ],
                })
    elif detected == 'uniview':
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
            '<soap:Body><c:GetDeviceInfo xmlns:c="http://www.uniview.com/2014/07">'
            '</c:GetDeviceInfo></soap:Body></soap:Envelope>')
        response = _device_soap_request(
            sess, auths, http_base, '/webapi/system/SysInfoService?wsdl', body)
        text = response.text if hasattr(response, 'status_code') and \
            response.status_code == 200 else ''
        match = re.search(r'<ChannelNum>(\d+)</ChannelNum>', text)
        if match:
            count = min(int(match.group(1)), 128)
            for index in range(1, count + 1):
                channels.append({
                    'index': index,
                    'name': f'Channel {index}',
                    'rtsp_urls': [
                        f'rtsp://{quote(username, safe="")}:{quote(password, safe="")}@'
                        f'{host}:{rtsp_port}/media/video{index}',
                    ],
                })
    elif detected == 'axis':
        response = _device_get(
            sess, auths, http_base + '/axis-cgi/streams/list.json')
        try:
            payloads = response.json() if response.status_code == 200 else []
        except (ValueError, AttributeError):
            payloads = []
        if isinstance(payloads, dict):
            payloads = payloads.get('payloads', [])
        for index, payload in enumerate(payloads[:128], 1):
            if not isinstance(payload, dict):
                continue
            uri = str(payload.get('uri') or '').strip()
            if not uri:
                continue
            if uri.startswith(('rtsp://','rtsps://')):
                parsed = urlsplit(uri)
                if parsed.hostname and not parsed.username:
                    rtsp_urls = [
                        f'rtsp://{quote(username, safe="")}:{quote(password, safe="")}@'
                        f'{parsed.hostname}:{parsed.port or rtsp_port}'
                        f'{parsed.path or "/"}'
                        f'?{parsed.query}' if parsed.query else
                        f'rtsp://{quote(username, safe="")}:{quote(password, safe="")}@'
                        f'{parsed.hostname}:{parsed.port or rtsp_port}'
                        f'{parsed.path or "/"}'
                    ]
                else:
                    rtsp_urls = [uri]
            else:
                rtsp_urls = [
                    f'rtsp://{quote(username, safe="")}:{quote(password, safe="")}@'
                    f'{host}:{rtsp_port}/{uri.lstrip("/")}'
                ]
            channels.append({
                'index': index,
                'name': uri.rsplit('/', 1)[-1] or f'Channel {index}',
                'rtsp_urls': rtsp_urls,
            })
    elif detected == 'generic':
        templates = [
            '/Streaming/Channels/{n}01',
            '/h264/ch{n}/main/av_stream',
            '/cam/realmonitor?channel={n}&subtype=0',
            '/media/video{n}',
            '/stream{n}',
        ]
        misses = 0
        for index in range(1, 33):
            found = ''
            for template in templates:
                path = template.format(n=index)
                if _rtsp_describe_verified(
                        host, rtsp_port, path, username, password):
                    found = path
                    break
            if found:
                misses = 0
                channels.append({
                    'index': index,
                    'name': f'Channel {index}',
                    'rtsp_urls': [
                        f'rtsp://{quote(username, safe="")}:{quote(password, safe="")}@'
                        f'{host}:{rtsp_port}{found}',
                    ],
                })
            else:
                misses += 1
                if misses >= 3 and channels:
                    break
    else:
        body = '<trt:GetProfiles xmlns:trt="http://www.onvif.org/ver10/media/wsdl"/>'
        response = _device_onvif_request(sess, auths, http_base, body)
        if hasattr(response, 'status_code') and response.status_code == 200:
            try:
                root = ElementTree.fromstring(response.content)
                profiles = []
                for elem in root.iter():
                    if elem.tag.rsplit('}', 1)[-1].lower() == 'profiles':
                        token = elem.attrib.get('token', '')
                        name = ''
                        for child in elem.iter():
                            if child.tag.rsplit('}', 1)[-1].lower() == 'name':
                                name = (child.text or '').strip()
                                break
                        if token:
                            profiles.append((token, name or token))
                for index, (token, name) in enumerate(profiles, 1):
                    uri_body = (
                        '<trt:GetStreamUri xmlns:trt="http://www.onvif.org/ver10/media/wsdl">'
                        '<trt:StreamSetup><tt:Stream xmlns:tt="http://www.onvif.org/ver10/schema">'
                        'RTP-Unicast</tt:Stream><tt:Transport '
                        'xmlns:tt="http://www.onvif.org/ver10/schema"><tt:Protocol>RTSP'
                        '</tt:Protocol></tt:Transport></trt:StreamSetup>'
                        f'<trt:ProfileToken>{token}</trt:ProfileToken></trt:GetStreamUri>')
                    uri_response = _device_onvif_request(sess, auths, http_base, uri_body)
                    urls = []
                    if hasattr(uri_response, 'status_code') and uri_response.status_code == 200:
                        try:
                            uri_root = ElementTree.fromstring(uri_response.content)
                            uri = _xml_local_text(uri_root, 'Uri')
                            if uri.startswith(('rtsp://','rtsps://')):
                                urls = [uri]
                        except ElementTree.ParseError:
                            pass
                    if urls:
                        channels.append({'index': index, 'name': name,
                                         'rtsp_urls': urls})
            except ElementTree.ParseError:
                pass
    if not channels:
        prefix = f'rtsp://{quote(username, safe="")}:{quote(password, safe="")}@{host}:{rtsp_port}'
        if detected == 'hikvision':
            paths = [f'/Streaming/Channels/{n}{suffix}' for n in range(1, 9) for suffix in ('01','02')]
        elif detected == 'dahua':
            paths = [f'/cam/realmonitor?channel={n}&subtype={sub}' for n in range(1, 9) for sub in (0,1)]
        elif detected == 'uniview':
            paths = [f'/media/video{n}' for n in range(1, 9)]
        elif detected == 'axis':
            paths = ['/axis-media/media.amp?videocodec=h264', '/axis-media/media.amp?videocodec=h265']
        elif detected == 'onvif':
            paths = [f'/live/ch{n:02d}_0' for n in range(0, 8)]
        else:
            paths = ['/live','/stream1','/stream2','/stream0','/stream','/live/main','/live/sub',
                     '/videoMain','/videoSub','/media/video1','/media/video2',
                     '/h264Preview_01_main','/h264Preview_01_sub','/h264/ch1/main/av_stream',
                     '/h264/ch1/sub/av_stream']
        urls = [supplied_rtsp] if supplied_rtsp.lower().startswith(('rtsp://','rtsps://')) else [prefix + x for x in paths]
        for idx, url in enumerate(urls[:32], 1):
            channels.append({'index': idx, 'name': f'Channel {idx} (RTSP pending)', 'rtsp_urls': [url]})

    geo = _device_geo(host, enrich_geo)
    country = (country or '').strip()
    region = (region or '').strip()
    city = (city or '').strip()
    if not country and not region and not city:
        location_source = geo.get('geo_source', '')
        location_confidence = geo.get('geo_confidence', '')
    else:
        location_source = 'inventory'
        location_confidence = 'provided'
    country = country or geo.get('country', '')
    region = region or geo.get('region', '')
    city = city or geo.get('city', '')
    device_id = 'device-' + _cam_id(host, port, username)
    display_vendor = 'RTSP-only / Unknown vendor' if detected == 'generic_rtsp' else detected.title()
    group = f'{country} · {display_vendor} · {host}:{port}' if country \
        else f'{display_vendor} · {host}:{port}'
    cameras = []
    for channel in sorted(channels, key=lambda item: int(item.get('index', 0))):
        index = int(channel.get('index') or 0)
        cameras.append({
            'id': _cam_id(host, rtsp_port, username,
                          channel.get('rtsp_urls', [''])[0]),
            'ip': host, 'port': port, 'camera_type': display_vendor,
            'label': f'{index:02d} — {channel.get("name") or "Channel"}',
            'rtsp_urls': channel.get('rtsp_urls', []), 'web_url': base_urls[0],
            'source_file': source_file,
            'country': country, 'region': region, 'city': city,
            'username': username,
            'password': password, 'group_name': group,
            'device_id': device_id, 'channel_index': index,
            'channel_name': channel.get('name', ''),
            'geo_source': location_source,
            'geo_confidence': location_confidence,
        })
    return {
        'device_id': device_id, 'vendor': detected, 'host': host, 'port': port,
        'rtsp_port': rtsp_port, 'info': info, 'channels': cameras,
        'geo': {'country': country, 'region': region, 'city': city,
                'geo_source': location_source,
                'geo_confidence': location_confidence},
        'http_probe': http_probe, 'session': None,
    }


@app.route('/api/device/discover-channels', methods=['POST', 'OPTIONS'])
@app.route('/api/device/discover', methods=['POST', 'OPTIONS'])
def api_device_discover() -> Response:
    """Discover one explicitly supplied CCTV device.

    The Device dialog sends POST /api/device/discover. Keep this route explicit
    in the standalone build so the UI never falls through to a 405 handler.
    OPTIONS is allowed for browser/preflight compatibility.
    """
    if request.method == 'OPTIONS':
        return ('', 204)
    denied = _admin_only()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    host = data.get('host') or data.get('ip') or ''
    username = data.get('username') or data.get('user') or ''
    password = data.get('password') or data.get('pass') or ''
    vendor = data.get('vendor') or 'auto'
    port = data.get('port', 'auto')
    rtsp_port = data.get('rtsp_port', 554)
    enrich_geo = bool(data.get('enrich_geo', False))
    try:
        result = discover_local_device(
            host=host,
            port=port,
            username=username,
            password=password,
            vendor=vendor,
            rtsp_port=rtsp_port,
            enrich_geo=enrich_geo,
        )
        channels = result.get('channels') or []
        if channels:
            db_bulk_upsert(channels)
        safe = [_safe_device_camera(cam) for cam in channels]
        return jsonify({
            'ok': True,
            'device_id': result.get('device_id'),
            'vendor': result.get('vendor') or 'generic',
            'host': result.get('host'),
            'port': result.get('port'),
            'rtsp_port': result.get('rtsp_port'),
            'info': result.get('info') or {},
            'geo': result.get('geo') or {},
            'http_probe': result.get('http_probe') or {},
            'count': len(safe),
            'channels': safe,
        })
    except Exception as exc:
        log.exception('Device discovery failed for %s:%s', host, port)
        return jsonify({
            'ok': False,
            'category': 'device_discovery_error',
            'detail': f'{type(exc).__name__}: {str(exc)[:500]}',
        }), 400


def device_config_bytes(host: str, port, username: str, password: str,
                        vendor: str = 'auto') -> tuple[bytes, str, str]:
    """Download an authenticated read-only vendor configuration response."""
    host, port = _device_target(host, port, username, password)
    sess, auths = _device_session(username, password)
    vendor = _normalize_vendor_hint(vendor)
    if vendor in ('auto', 'hikvision', 'hik'):
        response = _device_get(
            sess, auths, f'http://{host}:{port}/ISAPI/System/configurationData',
            timeout=20)
        if hasattr(response, 'status_code') and response.status_code == 200:
            return response.content, 'hikvision', 'configuration.xml'
    if vendor in ('auto', 'dahua', 'anjhua'):
        response = _device_get(
            sess, auths,
            f'http://{host}:{port}/cgi-bin/configManager.cgi?action=getConfig&name=General',
            timeout=20)
        if hasattr(response, 'status_code') and response.status_code == 200:
            return response.content, 'dahua', 'configuration.txt'
    if vendor in ('auto', 'axis'):
        response = _device_get(
            sess, auths,
            f'http://{host}:{port}/axis-cgi/param.cgi?action=list',
            timeout=20)
        if hasattr(response, 'status_code') and response.status_code == 200:
            return response.content, 'axis', 'parameters.txt'
    raise RuntimeError('Authenticated read-only configuration download failed')


# ══════════════════════════════════════════════════════════════════════════════
# FLASK ROUTES
# ══════════════════════════════════════════════════════════════════════════════

def _render_index() -> Response:
    groups  = db_get_group_stats()
    stats   = db_get_stats()
    vp      = val_get_progress()
    groups = [dict(g, group_key=g['display_group']) for g in groups]
    current_user = _current_user()
    current_role = _current_role()
    permissions = set(_user_permissions(current_user, current_role)) if _auth_enabled() else set(AUTH_PERMISSION_DEFS)
    # v135: Favorites is a virtual stream category. Every user with
    # stream.view sees it; actual cameras remain protected by access scope.
    if 'stream.view' in permissions:
        groups.insert(0, {
            'display_group': '★ Favorites', 'group_key': '__favorites__',
            'total': stats.get('favorites', 0), 'online': 0, 'checking': 0,
            'offline': 0, 'unknown': 0, 'favorites': stats.get('favorites', 0),
        })
    favorites_group = [g for g in groups if g.get('group_key') == '__favorites__']
    normal_groups = [g for g in groups if g.get('group_key') != '__favorites__']
    groups_sorted = favorites_group + sorted(
        normal_groups, key=lambda g: (-g.get('online', 0), g['display_group']))
    current_user = _current_user()
    current_role = _current_role()
    permissions = set(_user_permissions(current_user, current_role)) if _auth_enabled() else set(AUTH_PERMISSION_DEFS)
    html = render_template_string(
        _HTML_DASHBOARD,
        groups      = groups_sorted,
        total       = stats.get('total', 0),
        online      = stats.get('online', 0),
        checking    = stats.get('checking', 0),
        offline     = stats.get('offline', 0),
        unknown_c   = stats.get('unknown', 0),
        favorites   = stats.get('favorites', 0),
        num_groups  = len(groups_sorted),
        refresh     = datetime.now().strftime('%H:%M:%S'),
        tg_ok       = bool(_load_tg_destinations()),
        val_running = vp['running'],
        val_done    = vp['done'],
        val_total   = vp['total'],
        max_streams = MAX_STREAMS,
        max_multi_view = MAX_MULTI_VIEW,
        app_version = VERSION,
        current_user = current_user,
        current_role = current_role,
        can_stream = 'stream.view' in permissions,
        can_security = 'security.view' in permissions,
        can_db = 'database.manage' in permissions,
        can_alerts = 'alerts.manage' in permissions,
        can_camera_control = 'camera.control' in permissions,
        can_camera_manage = 'camera.manage' in permissions,
        can_diagnostics = 'diagnostics.run' in permissions,
        can_reports = 'reports.view' in permissions,
        can_health = 'health.view' in permissions,
        can_settings = 'settings.manage' in permissions,
        csrf_token = _csrf_token(),
    )
    safe = html.encode('utf-8', 'replace').decode('utf-8')
    return Response(safe.encode('utf-8'), content_type='text/html; charset=utf-8')


@app.route('/healthz', methods=['GET'])
def healthz():
    return jsonify({
        'ok': True,
        'service': 'camera-dashboard',
        'version': VERSION,
        'pid': os.getpid(),
        'time': utc_iso(),
    }), 200


@app.route('/_health',methods=['GET'])
def _health_endpoint(): return jsonify({'ok':True,'service':'camera-dashboard','version':VERSION,'time':utc_iso(),'validation':val_get_progress()})


@app.route('/readyz', methods=['GET'])
def readyz():
    """Lightweight unauthenticated readiness probe for hosted platforms."""
    return jsonify({
        'ok': True,
        'ready': True,
        'service': 'camera-dashboard',
        'version': VERSION,
        'pid': os.getpid(),
        'auth_enabled': _auth_enabled(),
        'time': utc_iso(),
    }), 200


@app.route('/')
def index() -> Response:
    return _render_index()


@app.route('/login', methods=['GET', 'POST'])
def login() -> Response:
    if not _auth_enabled():
        return redirect(url_for('index'))
    # Avoid showing the login form to a browser that already has a valid
    # authenticated session.
    if request.method == 'GET' and _session_valid():
        return redirect(url_for('index'))
    if request.method == 'POST':
        data = request.form
        username = (data.get('username') or '').strip()
        password = data.get('password') or ''
        client = _client_key()
        if _ip_guard_locked(client):
            error = 'Too many failed attempts from this network. Try again later.'
        elif _load_login_lockout(username or '<unknown>', client):
            error = 'Too many failed attempts for this user. Try again later.'
        else:
            role = ''
            auth_version = 1
            env_match = False
            row = _user_row(username)
            if not row or not int(row['enabled'] or 0) or not row['password_hash']:
                _verify_user_password(password, 'pbkdf2_sha256$600000$Q2FtZXJhRGFzaGJvYXJkRHVtbXk=$Ul120y2K6QHL7KHMlGp-P5F-CbAj4Cv2Pdc2_iMKF-w=')
            # A password explicitly set through Security is authoritative.
            # The environment secret remains the fallback only when the DB user
            # has no stored password yet.
            if row and int(row['enabled'] or 0) and row['password_hash'] and _verify_user_password(password, row['password_hash']):
                role = str(row['role'] or 'custom')
                auth_version = int(row['auth_version'] or 1)
            elif row and not int(row['enabled'] or 0):
                role = ''
            elif username == AUTH_USER and hmac.compare_digest(password, AUTH_PASSWORD):
                role = 'admin'; env_match = True
            elif (READONLY_USER and READONLY_PASSWORD and username == READONLY_USER
                  and hmac.compare_digest(password, READONLY_PASSWORD)):
                role = 'readonly'; env_match = True
            if role:
                # v66: cap concurrent sessions per user; revoke oldest sessions first.
                try:
                    c = _db_conn()
                    active = c.execute("SELECT session_id FROM v43_auth_sessions WHERE username=? AND revoked=0 ORDER BY last_seen DESC", (username,)).fetchall()
                    if len(active) >= MAX_SESSIONS_PER_USER:
                        for r in active[MAX_SESSIONS_PER_USER-1:]:
                            c.execute('UPDATE v43_auth_sessions SET revoked=1 WHERE session_id=?', (r['session_id'],))
                        c.commit()
                except Exception:
                    log.exception('v66 session limit enforcement failed')
                _clear_login_failures(username, client)
                try:
                    if row and role and not env_match:
                        _db_conn().execute("UPDATE v43_auth_users SET last_login_at=datetime('now'),updated_at=datetime('now') WHERE username=?", (username,))
                        _db_conn().commit()
                except Exception:
                    pass
                sid = uuid.uuid4().hex
                session.clear()
                # Rotate CSRF after authentication to prevent token fixation.
                session['_csrf_token'] = secrets.token_urlsafe(32)
                session.permanent = True
                session['user'] = username
                session['role'] = role
                session['auth_epoch'] = _auth_epoch
                session['auth_version'] = auth_version
                session['session_id'] = sid
                session['last_activity'] = time.time()
                session['_session_touch'] = time.time()
                try:
                    def _create_session():
                        c = _db_conn()
                        c.execute("INSERT INTO v43_auth_sessions(session_id,username,role,client_ip,user_agent,created_at,last_seen,revoked) VALUES(?,?,?,?,?,?,?,0)",
                                  (sid, username, role, client, request.headers.get('User-Agent','')[:500], utc_iso(), utc_iso()))
                        c.commit()
                    _db_retry_write(_create_session, 'session create')
                except Exception:
                    log.exception('failed to persist login session for %s', username)
                force_change = False
                try:
                    if row:
                        force_change = bool(int(row['force_password_change'] or 0))
                        if PASSWORD_MAX_AGE_DAYS and row['password_changed_at']:
                            age = (datetime.now(timezone.utc) - datetime.fromisoformat(str(row['password_changed_at']).replace('Z','+00:00'))).days
                            force_change = force_change or age >= PASSWORD_MAX_AGE_DAYS
                except Exception:
                    pass
                log.info('[Auth] login success user=%s role=%s session=%s next=%s',
                         username, role, sid[:12], data.get('next') or '/')
                session.modified = True
                if force_change:
                    return redirect('/?force_password_change=1')
                return redirect(data.get('next') or url_for('index'))
            _record_login_failure(username or '<unknown>', client)
            error = 'Invalid username or password'
    else:
        error = ''
    return Response(render_template_string("""<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Camera Dashboard Login</title>
<style>
body{background:#0d1117;color:#c9d1d9;font-family:system-ui;display:grid;
place-items:center;min-height:100vh}.box{background:#161b22;border:1px solid #30363d;
border-radius:12px;padding:28px;width:min(360px,90vw)}h1{font-size:20px;color:#58a6ff}
label{display:block;font-size:12px;color:#8b949e;margin:14px 0 5px}
input{width:100%;box-sizing:border-box;padding:9px;background:#0d1117;
border:1px solid #30363d;border-radius:6px;color:#c9d1d9}
button{margin-top:18px;width:100%;padding:9px;background:#3fb950;border:0;
border-radius:6px;font-weight:700;cursor:pointer}.error{color:#f85149;font-size:13px}
</style></head><body><form class="box" method="post">
<h1>📷 Camera Dashboard</h1><p>Sign in to continue.</p>
{% if error %}<div class="error">{{ error }}</div>{% endif %}
<input type="hidden" name="next" value="{{ next|e }}">
<label>Username</label><input name="username" autocomplete="username" required>
<label>Password</label><input name="password" type="password"
autocomplete="current-password" required><button>Sign in</button>
</form></body></html>""", error=error,
        next=request.args.get('next') or request.form.get('next') or ''),
        content_type='text/html; charset=utf-8')

@app.route('/logout')
def logout() -> Response:
    session.clear()
    response = redirect(url_for('login') if _auth_enabled() else url_for('index'))
    response.delete_cookie(_V74_AUTH_COOKIE, path='/')
    return response


@app.route('/favicon.ico')
def favicon() -> Response:
    # The embedded dashboard has no icon asset; return an explicit empty
    # response so browsers do not report a noisy 404 on every page load.
    return Response('<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 64 64\"><rect width=\"64\" height=\"64\" rx=\"12\" fill=\"#111827\"/><circle cx=\"32\" cy=\"32\" r=\"15\" fill=\"#58a6ff\"/></svg>',mimetype='image/svg+xml')


@app.route('/group_html/<path:group_name>')
def group_html(group_name: str) -> Response:
    offset  = int(request.args.get('offset', 0))
    cameras = db_get_cameras_by_group(group_name, limit=PAGE_SIZE, offset=offset)
    total   = db_get_group_count(group_name)
    gn_safe = group_name.encode('utf-8', 'replace').decode('utf-8')
    html    = render_template_string(
        _HTML_CAM_GRID,
        cameras    = cameras,
        group_name = gn_safe,
        offset     = offset,
        total      = total,
        page_size  = PAGE_SIZE,
        can_stream = _has_permission('stream.view'),
    )
    safe = html.encode('utf-8', 'replace').decode('utf-8')
    return Response(safe.encode('utf-8'), content_type='text/html; charset=utf-8')


@app.route('/search_html')
def search_html() -> Response:
    cameras, total = db_search_cameras(
        query=request.args.get('q', ''),
        status=request.args.get('status', 'all'),
        group=request.args.get('group', 'all'),
        favorites=request.args.get('favorites', '0') == '1',
        online_only=request.args.get('online_only', '0') == '1',
        limit=request.args.get('limit', PAGE_SIZE),
        offset=request.args.get('offset', 0),
    )
    html = render_template_string(
        _HTML_CAM_GRID,
        cameras=cameras,
        group_name='Search results',
        offset=int(request.args.get('offset', 0)),
        total=total,
        page_size=PAGE_SIZE,
        can_stream=_has_permission('stream.view'),
    )
    return Response(html.encode('utf-8', 'replace'),
                    content_type='text/html; charset=utf-8')


@app.route('/api/search')
def api_search() -> Response:
    cameras, total = db_search_cameras(
        query=request.args.get('q', ''),
        status=request.args.get('status', 'all'),
        group=request.args.get('group', 'all'),
        favorites=request.args.get('favorites', '0') == '1',
        online_only=request.args.get('online_only', '0') == '1',
        limit=request.args.get('limit', PAGE_SIZE),
        offset=request.args.get('offset', 0),
    )
    return jsonify({
        'ok': True,
        'total': total,
        'offset': max(0, int(request.args.get('offset', 0))),
        'limit': min(500, max(1, int(request.args.get('limit', PAGE_SIZE)))),
        'cameras': cameras,
    })


def _admin_only() -> Optional[Response]:
    if _auth_enabled() and not _has_permission('database.manage'):
        return jsonify({'ok': False, 'detail': 'Permission required: database.manage'}), 403
    return None


@app.route('/api/admin/health')
def api_admin_health() -> Response:
    denied = _admin_only()
    if denied:
        return denied
    db_size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    wal_path = DB_PATH + '-wal'
    backup_info = []
    for path in DB_BACKUPS:
        backup_info.append({
            'path': os.path.basename(path),
            'exists': os.path.exists(path),
            'bytes': os.path.getsize(path) if os.path.exists(path) else 0,
            'mtime': datetime.fromtimestamp(os.path.getmtime(path)).isoformat()
            if os.path.exists(path) else None,
        })
    queue = _db_conn().execute(
        "SELECT COUNT(*) FROM validation_queue WHERE next_attempt<=datetime('now')"
    ).fetchone()[0]
    deleted = _db_conn().execute(
        'SELECT COUNT(*) FROM cameras WHERE deleted=1'
    ).fetchone()[0]
    with _streams_lock:
        active_streams = sum(_stream_subscribers.values())
    return jsonify({
        'ok': True,
        'database': {
            'path': DB_PATH, 'bytes': db_size,
            'wal_bytes': os.path.getsize(wal_path) if os.path.exists(wal_path) else 0,
            'integrity': _cached_read('live_integrity', 60.0, lambda: _sqlite_integrity_ok(DB_PATH)),
            'cameras': db_get_stats().get('total', 0),
            'deleted_rows': deleted,
            'due_validation': queue,
        },
        'streams': {'active': active_streams, 'limit': MAX_STREAMS,
                    'recovery_attempts': STREAM_RECOVERY_ATTEMPTS},
        'workers': _worker_health_snapshot(),
        'settings': {
            'deleted_retention_days': DELETED_RETENTION_DAYS,
            'snapshot_retention_days': SNAPSHOT_RETENTION_DAYS,
        },
        'backups': backup_info,
        'validation': val_get_progress(),
    })


@app.route('/api/admin/cleanup', methods=['POST'])
def api_admin_cleanup() -> Response:
    denied = _admin_only()
    if denied:
        return denied
    removed_deleted = db_prune_deleted(
        limit=5000, retention_days=DELETED_RETENTION_DAYS)
    removed_snapshots = db_cleanup_snapshots(SNAPSHOT_RETENTION_DAYS)
    try:
        _db_conn().execute('PRAGMA wal_checkpoint(TRUNCATE)')
        _db_conn().commit()
    except sqlite3.Error as e:
        log.warning('WAL checkpoint failed during admin cleanup: %s', e)
    backup_ok = db_create_backup()
    return jsonify({
        'ok': backup_ok,
        'removed_deleted': removed_deleted,
        'removed_snapshots': removed_snapshots,
        'backup_created': backup_ok,
    })


# A deliberately narrow export allowlist: no camera inventory, RTSP URLs, stream
# observations, discovery queues, camera history, snapshots, or login sessions.
_USER_CONFIG_TABLES = ('v43_auth_users', 'v43_user_roles', 'app_settings', 'api_keys',
                       'alert_rules', 'scheduled_jobs', 'app_retention')
_USER_CONFIG_FORMAT = 'camera-dashboard-user-config-v1'


def _user_config_table_rows(conn, table):
    allowed = set(_USER_CONFIG_TABLES)
    if table not in allowed:
        raise ValueError('Table is not allowed in user/config transfer')
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if not exists:
        return []
    columns = [str(r[1]) for r in conn.execute('PRAGMA table_info("' + table + '")').fetchall()]
    # Never export session material or login/audit history, even if a future
    # schema adds such columns to an allowlisted table.
    rows = conn.execute('SELECT * FROM "' + table + '"').fetchall()
    return [{k: row[k] for k in columns if k in row.keys()} for row in rows]


@app.route('/api/admin/user-config/export', methods=['GET'])
def api_export_user_config():
    denied = _admin_only()
    if denied: return denied
    fd, out_path = tempfile.mkstemp(prefix='camera_dashboard_user_config_', suffix='.sqlite', dir=DATA_DIR)
    os.close(fd)
    try:
        src = sqlite3.connect(DB_PATH, timeout=20)
        src.row_factory = sqlite3.Row
        dst = sqlite3.connect(out_path, timeout=20)
        try:
            dst.execute('PRAGMA journal_mode=DELETE')
            dst.execute('PRAGMA synchronous=FULL')
            dst.execute('PRAGMA foreign_keys=OFF')
            dst.execute('''CREATE TABLE transfer_manifest(
                format TEXT PRIMARY KEY, app_version TEXT NOT NULL,
                exported_at TEXT NOT NULL, notice TEXT NOT NULL)''')
            dst.execute('''CREATE TABLE transfer_telegram_destinations(
                id INTEGER PRIMARY KEY CHECK(id=1), payload_json TEXT NOT NULL)''')
            dst.execute('''CREATE TABLE transfer_background_services(
                id INTEGER PRIMARY KEY CHECK(id=1), payload_json TEXT NOT NULL)''')
            dst.execute('INSERT INTO transfer_manifest VALUES(?,?,?,?)', (
                _USER_CONFIG_FORMAT, VERSION, utc_iso(),
                'Sensitive user/application configuration only; camera/device records are excluded.'))

            for table in _USER_CONFIG_TABLES:
                schema = src.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                if not schema or not schema[0]:
                    continue
                # Reuse only the schema for explicitly allowlisted tables. No
                # camera tables, triggers, views, indexes, or arbitrary SQL are copied.
                dst.execute(schema[0])
                cols = [str(r[1]) for r in src.execute('PRAGMA table_info("' + table + '")').fetchall()]
                if not cols:
                    continue
                rows = src.execute('SELECT * FROM "' + table + '"').fetchall()
                qcols = ','.join('"' + c + '"' for c in cols)
                marks = ','.join('?' for _ in cols)
                for row in rows:
                    values = [row[c] for c in cols]
                    # Telegram ciphertext uses the source host's local key and
                    # cannot be decrypted on another host, so move it separately.
                    if table == 'app_settings' and str(row['key']) == TG_DB_KEY:
                        continue
                    dst.execute('INSERT INTO "' + table + '" (' + qcols + ') VALUES (' + marks + ')', values)

            telegram = _telegram_decrypt_config(include_disabled=True)
            if not telegram:
                telegram = _legacy_tg_file_destinations(include_disabled=True)
            dst.execute('INSERT INTO transfer_telegram_destinations(id,payload_json) VALUES(1,?)',
                        (json.dumps(telegram, ensure_ascii=False),))
            services = {}
            try:
                if os.path.isfile(BACKGROUND_SERVICES_CONFIG_PATH):
                    with open(BACKGROUND_SERVICES_CONFIG_PATH, 'r', encoding='utf-8') as fh:
                        candidate = json.load(fh)
                    if isinstance(candidate, dict): services = candidate
            except Exception as exc:
                log.warning('[UserConfig] Could not export background service settings: %s', exc)
            dst.execute('INSERT INTO transfer_background_services(id,payload_json) VALUES(1,?)',
                        (json.dumps(services, ensure_ascii=False),))
            dst.execute('PRAGMA user_version=1')
            dst.commit()
        finally:
            src.close()
            dst.close()
        with open(out_path, 'rb') as fh:
            data = fh.read()
        response = Response(data, mimetype='application/vnd.sqlite3')
        response.headers['Content-Disposition'] = 'attachment; filename="camera_dashboard_user_config.sqlite"'
        response.headers['Cache-Control'] = 'no-store, private'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Length'] = str(len(data))
        return response
    except Exception as exc:
        log.exception('[UserConfig] SQLite export failed')
        return jsonify({'ok': False, 'detail': 'Export failed: ' + type(exc).__name__}), 500
    finally:
        _remove_sqlite_temp_family(out_path)


def _import_user_config_database_file(source_path: str) -> dict:
    """Safely merge an allowlisted user/config transfer file into the live DB.

    This is shared by startup CLI imports and is intentionally independent of
    Flask request authentication. Callers must explicitly select a trusted file.
    Camera/device tables and all unapproved tables are rejected.
    """
    source_path = os.path.abspath(source_path)
    live_path = os.path.abspath(DB_PATH)
    if source_path == live_path:
        raise ValueError('The transfer file cannot be the live camera database.')
    if not os.path.isfile(source_path):
        raise FileNotFoundError(source_path)
    size = os.path.getsize(source_path)
    if size <= 0 or size > 10 * 1024 * 1024:
        raise ValueError('Transfer file must be between 1 byte and 10 MB.')
    with open(source_path, 'rb') as fh:
        if fh.read(16) != b'SQLite format 3\x00':
            raise ValueError('File is not a valid SQLite database.')
    uri = 'file:' + source_path.replace('\\', '/') + '?mode=ro'
    source = sqlite3.connect(uri, uri=True, timeout=15)
    source.row_factory = sqlite3.Row
    try:
        check = source.execute('PRAGMA quick_check').fetchone()
        if not check or str(check[0]).lower() != 'ok':
            raise ValueError('Transfer database failed SQLite integrity checking.')
        names = {str(r[0]) for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if 'transfer_manifest' not in names:
            raise ValueError('Transfer manifest is missing.')
        manifest = source.execute('SELECT format FROM transfer_manifest LIMIT 1').fetchone()
        if not manifest or manifest['format'] != _USER_CONFIG_FORMAT:
            raise ValueError('Unsupported user/config transfer format.')
        allowed_names = set(_USER_CONFIG_TABLES) | {
            'transfer_manifest', 'transfer_telegram_destinations',
            'transfer_background_services', 'sqlite_sequence'}
        if names - allowed_names or 'cameras' in names:
            raise ValueError('Transfer database contains unapproved tables; import refused.')

        # Read optional configuration payloads before touching the live DB. If
        # Telegram secrets exist but secure encryption is unavailable, preserve
        # the transfer file and report a partial import rather than silently
        # archiving the only copy of those secrets.
        telegram = []
        if 'transfer_telegram_destinations' in names:
            row = source.execute('SELECT payload_json FROM transfer_telegram_destinations WHERE id=1').fetchone()
            if row:
                telegram = json.loads(row['payload_json'] or '[]')
        services = {}
        if 'transfer_background_services' in names:
            row = source.execute('SELECT payload_json FROM transfer_background_services WHERE id=1').fetchone()
            if row:
                services = json.loads(row['payload_json'] or '{}')

        imported_users = imported_settings = imported_other = 0
        conn = _db_conn()
        conn.execute('BEGIN IMMEDIATE')
        try:
            for table in _USER_CONFIG_TABLES:
                if table not in names:
                    continue
                destination_exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                source_columns = [str(r[1]) for r in source.execute('PRAGMA table_info("' + table + '")').fetchall()]
                dest_info = conn.execute('PRAGMA table_info("' + table + '")').fetchall() if destination_exists else []
                if not dest_info or not source_columns:
                    continue
                dest_columns = [str(r[1]) for r in dest_info]
                pk_cols = [str(r[1]) for r in sorted(dest_info, key=lambda r: int(r[5] or 0)) if int(r[5] or 0) > 0]
                if table == 'api_keys' and 'name' in dest_columns:
                    pk_cols = ['name']
                if not pk_cols:
                    continue
                rows = source.execute('SELECT * FROM "' + table + '"').fetchall()
                for row in rows:
                    keys = [k for k in source_columns if k in dest_columns]
                    if table == 'api_keys':
                        keys = [k for k in keys if k != 'id']
                    if not keys or not all(k in keys for k in pk_cols):
                        continue
                    vals = [row[k] for k in keys]
                    if table == 'scheduled_jobs' and 'id' in keys and 'name' in keys:
                        existing_job = conn.execute('SELECT id FROM scheduled_jobs WHERE name=?', (row['name'],)).fetchone()
                        id_index = keys.index('id')
                        if existing_job:
                            vals[id_index] = existing_job['id']
                        else:
                            collision = conn.execute('SELECT name FROM scheduled_jobs WHERE id=?', (vals[id_index],)).fetchone()
                            if collision and str(collision['name']) != str(row['name']):
                                vals[id_index] = uuid.uuid4().hex
                    marks = ','.join('?' for _ in keys)
                    updates = [k for k in keys if k not in pk_cols]
                    sql = 'INSERT INTO "' + table + '" (' + ','.join('"'+k+'"' for k in keys) + ') VALUES (' + marks + ')'
                    if updates:
                        sql += ' ON CONFLICT(' + ','.join('"'+k+'"' for k in pk_cols) + ') DO UPDATE SET ' + ','.join('"'+k+'"=excluded."'+k+'"' for k in updates)
                    else:
                        sql += ' ON CONFLICT(' + ','.join('"'+k+'"' for k in pk_cols) + ') DO NOTHING'
                    conn.execute(sql, vals)
                    if table == 'v43_auth_users': imported_users += 1
                    elif table == 'app_settings': imported_settings += 1
                    else: imported_other += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        telegram_imported = True
        if isinstance(telegram, list) and telegram:
            ok, detail = _save_telegram_config(telegram)
            if not ok:
                telegram_imported = False
                log.error('[UserConfig] Telegram destinations were not imported: %s', detail)

        services_imported = True
        if isinstance(services, dict) and services:
            tmp = BACKGROUND_SERVICES_CONFIG_PATH + '.import.tmp'
            try:
                with open(tmp, 'w', encoding='utf-8') as fh:
                    json.dump(services, fh, ensure_ascii=False, indent=2)
                    fh.flush(); os.fsync(fh.fileno())
                os.replace(tmp, BACKGROUND_SERVICES_CONFIG_PATH)
                # The preferences were loaded at module import time. Reload them
                # now so imported ON/OFF choices take effect during this startup.
                _load_persistent_background_service_states()
            except Exception as exc:
                try:
                    if os.path.exists(tmp): os.unlink(tmp)
                except OSError:
                    pass
                services_imported = False
                log.warning('[UserConfig] Background service settings were not imported: %s', exc)
        complete = bool(telegram_imported and services_imported)
        if not complete:
            log.warning('[UserConfig] Import is PARTIAL; source file must be retained and retried after fixing the missing configuration dependency/error.')
        return {'ok': complete, 'complete': complete,
                'telegram_imported': telegram_imported, 'services_imported': services_imported,
                'imported_users': imported_users,
                'imported_settings': imported_settings, 'imported_other_rows': imported_other}
    finally:
        source.close()


@app.route('/api/admin/user-config/import', methods=['POST'])
def api_import_user_config():
    denied = _admin_only()
    if denied: return denied
    upload = request.files.get('file')
    if not upload:
        return jsonify({'ok': False, 'detail': 'Choose a user/config SQLite file first.'}), 400
    fd, in_path = tempfile.mkstemp(prefix='camera_dashboard_user_config_import_', suffix='.sqlite', dir=DATA_DIR)
    try:
        total = 0
        with os.fdopen(fd, 'wb') as fh:
            while True:
                chunk = upload.stream.read(1024 * 1024)
                if not chunk: break
                total += len(chunk)
                if total > 10 * 1024 * 1024:
                    return jsonify({'ok': False, 'detail': 'Import file exceeds the 10 MB limit.'}), 413
                fh.write(chunk)
            fh.flush(); os.fsync(fh.fileno())
        with open(in_path, 'rb') as fh:
            if fh.read(16) != b'SQLite format 3\x00':
                return jsonify({'ok': False, 'detail': 'The selected file is not a valid SQLite database.'}), 400
        uri = 'file:' + in_path.replace('\\', '/') + '?mode=ro'
        source = sqlite3.connect(uri, uri=True, timeout=15)
        source.row_factory = sqlite3.Row
        try:
            check = source.execute('PRAGMA quick_check').fetchone()
            if not check or str(check[0]).lower() != 'ok':
                return jsonify({'ok': False, 'detail': 'The transfer database failed SQLite integrity checking.'}), 400
            names = {str(r[0]) for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if 'transfer_manifest' not in names:
                return jsonify({'ok': False, 'detail': 'Transfer manifest is missing; choose an exported user/config database.'}), 400
            manifest = source.execute('SELECT format FROM transfer_manifest LIMIT 1').fetchone()
            if not manifest or manifest['format'] != _USER_CONFIG_FORMAT:
                return jsonify({'ok': False, 'detail': 'Unsupported user/config transfer format.'}), 400
            unexpected = names - set(_USER_CONFIG_TABLES) - {
                'transfer_manifest', 'transfer_telegram_destinations', 'transfer_background_services', 'sqlite_sequence'}
            if unexpected or 'cameras' in names:
                return jsonify({'ok': False, 'detail': 'Transfer database contains unapproved tables; import refused.'}), 400

            imported_users = imported_settings = imported_other = 0
            conn = _db_conn()
            conn.execute('BEGIN IMMEDIATE')
            try:
                for table in _USER_CONFIG_TABLES:
                    if table not in names:
                        continue
                    destination_exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                    source_columns = [str(r[1]) for r in source.execute('PRAGMA table_info("' + table + '")').fetchall()]
                    dest_info = conn.execute('PRAGMA table_info("' + table + '")').fetchall() if destination_exists else []
                    if not dest_info or not source_columns:
                        continue
                    dest_columns = [str(r[1]) for r in dest_info]
                    pk_cols = [str(r[1]) for r in sorted(dest_info, key=lambda r: int(r[5] or 0)) if int(r[5] or 0) > 0]
                    if table == 'api_keys' and 'name' in dest_columns:
                        pk_cols = ['name']
                    if not pk_cols:
                        continue
                    rows = source.execute('SELECT * FROM "' + table + '"').fetchall()
                    for row in rows:
                        keys = [k for k in source_columns if k in dest_columns]
                        if table == 'api_keys': keys = [k for k in keys if k != 'id']
                        if not keys or not all(k in keys for k in pk_cols): continue
                        vals = [row[k] for k in keys]
                        # Scheduled jobs have both an ID primary key and a unique
                        # name. Reuse the local ID for an existing name; if a new
                        # imported ID collides with a different local job, assign
                        # a fresh ID instead of aborting the whole transfer.
                        if table == 'scheduled_jobs' and 'id' in keys and 'name' in keys:
                            name_value = row['name']
                            existing_job = conn.execute('SELECT id FROM scheduled_jobs WHERE name=?', (name_value,)).fetchone()
                            id_index = keys.index('id')
                            if existing_job:
                                vals[id_index] = existing_job['id']
                            else:
                                id_collision = conn.execute('SELECT name FROM scheduled_jobs WHERE id=?', (vals[id_index],)).fetchone()
                                if id_collision and str(id_collision['name']) != str(name_value):
                                    vals[id_index] = uuid.uuid4().hex
                        marks = ','.join('?' for _ in keys)
                        updates = [k for k in keys if k not in pk_cols]
                        sql = 'INSERT INTO "' + table + '" (' + ','.join('"'+k+'"' for k in keys) + ') VALUES (' + marks + ')'
                        if updates:
                            sql += ' ON CONFLICT(' + ','.join('"'+k+'"' for k in pk_cols) + ') DO UPDATE SET ' + ','.join('"'+k+'"=excluded."'+k+'"' for k in updates)
                        else:
                            sql += ' ON CONFLICT(' + ','.join('"'+k+'"' for k in pk_cols) + ') DO NOTHING'
                        conn.execute(sql, vals)
                        if table == 'v43_auth_users': imported_users += 1
                        elif table == 'app_settings': imported_settings += 1
                        else: imported_other += 1
                conn.commit()
            except Exception:
                conn.rollback()
                raise

            telegram = []
            if 'transfer_telegram_destinations' in names:
                row = source.execute('SELECT payload_json FROM transfer_telegram_destinations WHERE id=1').fetchone()
                if row:
                    telegram = json.loads(row['payload_json'] or '[]')
            if isinstance(telegram, list) and telegram:
                ok, detail = _save_telegram_config(telegram)
                if not ok:
                    log.error('[UserConfig] Telegram destinations were not imported: %s', detail)

            services = {}
            if 'transfer_background_services' in names:
                row = source.execute('SELECT payload_json FROM transfer_background_services WHERE id=1').fetchone()
                if row: services = json.loads(row['payload_json'] or '{}')
            if isinstance(services, dict) and services:
                try:
                    tmp = BACKGROUND_SERVICES_CONFIG_PATH + '.import.tmp'
                    with open(tmp, 'w', encoding='utf-8') as fh:
                        json.dump(services, fh, ensure_ascii=False, indent=2)
                        fh.flush(); os.fsync(fh.fileno())
                    os.replace(tmp, BACKGROUND_SERVICES_CONFIG_PATH)
                except Exception as exc:
                    log.warning('[UserConfig] Background service settings were not imported: %s', exc)
            return jsonify({'ok': True, 'imported_users': imported_users,
                            'imported_settings': imported_settings, 'imported_other_rows': imported_other,
                            'detail': 'Imported allowlisted user/application settings only. Camera/device records, sessions, and validation history were excluded.'})
        finally:
            source.close()
    except (sqlite3.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        log.exception('[UserConfig] SQLite import rejected')
        return jsonify({'ok': False, 'detail': 'Invalid user/config database: ' + str(exc)[:250]}), 400
    except Exception as exc:
        log.exception('[UserConfig] SQLite import failed')
        return jsonify({'ok': False, 'detail': 'Import failed: ' + type(exc).__name__}), 500
    finally:
        _remove_sqlite_temp_family(in_path)


_DB_SHRINK_JOB_LOCK=threading.RLock()
_DB_SHRINK_JOB={'status':'idle','result':None,'job_id':''}

def _run_db_shrink_job(job_id, actor):
    result=db_compact_and_backup(actor)
    with _DB_SHRINK_JOB_LOCK: _DB_SHRINK_JOB.update(status='completed' if result.get('ok') else 'failed',result=result,job_id=job_id,finished_at=utc_iso())

@app.route('/api/admin/shrink', methods=['POST'])
def api_admin_shrink() -> Response:
    denied = _admin_only()
    if denied: return denied
    with _DB_SHRINK_JOB_LOCK:
        if _DB_SHRINK_JOB.get('status')=='running': return jsonify({'ok':True,'running':True,'job_id':_DB_SHRINK_JOB.get('job_id')}),200
        job_id=uuid.uuid4().hex; _DB_SHRINK_JOB.update(status='running',result=None,job_id=job_id,started_at=utc_iso())
    threading.Thread(target=_run_db_shrink_job,args=(job_id,_current_user()),daemon=True,name='db-shrink').start()
    return jsonify({'ok':True,'queued':True,'running':True,'job_id':job_id}),202

@app.route('/api/admin/shrink/status')
def api_admin_shrink_status():
    denied=_admin_only()
    if denied: return denied
    with _DB_SHRINK_JOB_LOCK: return jsonify({'ok':True,**dict(_DB_SHRINK_JOB)})


@app.route('/admin')
def admin_page() -> Response:
    """Embedded maintenance page for the header Admin link."""
    return Response(render_template_string(r'''<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Camera Dashboard — Admin</title>
<style>
:root{color-scheme:dark;--bg:#0d1117;--panel:#161b22;--line:#30363d;
--text:#c9d1d9;--muted:#8b949e;--blue:#58a6ff;--green:#3fb950;--red:#f85149}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:14px system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px}
header{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:18px}
h1{font-size:21px;color:var(--blue);margin:0 auto 0 0}
.btn{border:1px solid var(--line);border-radius:7px;background:#21262d;
color:var(--text);padding:8px 12px;cursor:pointer;text-decoration:none}
.btn:hover{border-color:var(--blue)}.btn-green{border-color:#238636;color:#aff5b5}
.btn-danger{border-color:#da3633;color:#ffb4ab}
#message{min-height:20px;color:var(--muted);margin:8px 0 14px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:14px}
.card h2{font-size:14px;margin:0 0 10px;color:var(--blue)}
dl{display:grid;grid-template-columns:max-content 1fr;gap:6px 12px;margin:0}
dt{color:var(--muted)}dd{margin:0;overflow-wrap:anywhere}
pre{white-space:pre-wrap;max-height:330px;overflow:auto;color:var(--text);
font:12px ui-monospace,SFMono-Regular,monospace}
.ok{color:var(--green)}.bad{color:var(--red)}
</style></head><body><main>
<header><h1>🛠 Camera Dashboard Admin</h1>
<a class="btn" href="/">← Dashboard</a>
<button class="btn" onclick="loadHealth()">↻ Refresh health</button>
<button class="btn btn-green" onclick="runCleanup()">Run cleanup &amp; backup</button><button class="btn" onclick="runShrink()">🗜️ Shrink DB</button><button class="btn" onclick="loadAnalytics()">📊 Analytics</button><button class="btn" onclick="testAlerts()">🔔 Test alerts</button></header>
<div id="message">Loading health…</div>
<section class="grid">
<article class="card"><h2>Database</h2><dl id="database"></dl></article>
<article class="card"><h2>Streams &amp; settings</h2><dl id="streams"></dl></article>
<article class="card"><h2>Backups</h2><pre id="backups"></pre></article>
<article class="card"><h2>Workers</h2><pre id="workers"></pre></article>
<article class="card"><h2>Validation</h2><pre id="validation"></pre></article>
</section>
<script>
'use strict';
const esc = value => String(value ?? '').replace(/[&<>"']/g,
  ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
function setDl(id, values) {
  document.getElementById(id).innerHTML = Object.entries(values).map(([k,v]) =>
    '<dt>'+esc(k)+'</dt><dd>'+esc(v)+'</dd>').join('');
}
async function loadHealth() {
  const msg = document.getElementById('message');
  msg.textContent = 'Loading health…';
  try {
    const r = await fetch('/api/admin/health');
    const d = await r.json();
    if (!r.ok || !d.ok) throw Error(d.detail || 'Health request failed');
    setDl('database', {
      'Integrity': d.database.integrity ? 'ok' : 'FAILED',
      'Cameras': d.database.cameras,
      'Deleted rows': d.database.deleted_rows,
      'Due validation': d.database.due_validation,
      'Database bytes': d.database.bytes,
      'WAL bytes': d.database.wal_bytes
    });
    setDl('streams', {
      'Active': d.streams.active + ' / ' + d.streams.limit,
      'Recovery attempts': d.streams.recovery_attempts,
      'Deleted retention': d.settings.deleted_retention_days + ' days',
      'Snapshot retention': d.settings.snapshot_retention_days + ' days'
    });
    document.getElementById('backups').textContent =
      JSON.stringify(d.backups, null, 2);
    document.getElementById('workers').textContent =
      JSON.stringify(d.workers, null, 2);
    document.getElementById('validation').textContent =
      JSON.stringify(d.validation, null, 2);
    msg.textContent = 'Health refreshed at ' + new Date().toLocaleTimeString();
    msg.className = 'ok';
  } catch (e) {
    msg.textContent = '❌ ' + e.message;
    msg.className = 'bad';
  }
}
async function runCleanup() {
  if (!confirm('Run cleanup, WAL checkpoint, and verified backup?')) return;
  const msg = document.getElementById('message');
  msg.textContent = 'Running cleanup…';
  try {
    const r = await fetch('/api/admin/cleanup', {method:'POST'});
    const d = await r.json();
    if (!r.ok || !d.ok) throw Error(d.detail || 'Cleanup or backup failed');
    msg.textContent = '✅ Cleanup complete; verified backup created.';
    msg.className = 'ok';
    await loadHealth();
  } catch (e) {
    msg.textContent = '❌ ' + e.message;
    msg.className = 'bad';
  }
}
async function runShrink(){
  if(!confirm('Pause validation and shrink the SQLite database? Only the latest verified backup will remain.')) return;
  const msg=document.getElementById('message'); msg.textContent='Shrinking database…';
  try{const r=await fetch('/api/admin/shrink',{method:'POST'});const d=await r.json();
    if(d.running){msg.textContent='⏳ Database shrink is already running; the existing operation will continue safely.'; msg.className=''; return;}
    if(!r.ok||!d.ok) throw Error(d.detail||'Shrink failed');
    msg.textContent='✅ Shrink complete; saved '+((d.saved_bytes||0)/1048576).toFixed(1)+' MB. One verified backup retained.'; msg.className='ok';
    await loadHealth();
  }catch(e){msg.textContent='❌ '+e.message;msg.className='bad';}
}
async function loadAnalytics(){try{const r=await fetch('/api/v40/analytics');const d=await r.json();alert(JSON.stringify(d.analytics,null,2));}catch(e){alert(e.message)}}
async function testAlerts(){try{const r=await fetch('/api/v40/alerts/test',{method:'POST'});const d=await r.json();alert(d.detail||JSON.stringify(d));}catch(e){alert(e.message)}}
loadHealth();
</script></main>
<script>
window.openDashboardTool=function(title,actions,output){
  const m=document.getElementById('dashboardToolModal'); if(!m)return;
  document.getElementById('dashboardToolTitle').textContent=title||'Dashboard Work Tool';
  document.getElementById('dashboardToolActions').innerHTML=actions||'';
  document.getElementById('dashboardToolOutput').textContent=output||'Ready.';
  document.getElementById('dashboardToolStatus').textContent='Ready.';
  m.classList.add('open'); m.setAttribute('aria-hidden','false');
};
window.closeDashboardTool=function(){const m=document.getElementById('dashboardToolModal');if(m){m.classList.remove('open');m.setAttribute('aria-hidden','true');}};
window.dashboardToolSet=function(msg,ok=true){const x=document.getElementById('dashboardToolStatus');if(x){x.textContent=msg;x.style.borderColor=ok?'#166534':'#991b1b';x.style.color=ok?'#86efac':'#fca5a5';}};
window.dashboardToolOut=function(v){const x=document.getElementById('dashboardToolOutput');if(x)x.textContent=typeof v==='string'?v:JSON.stringify(v,null,2);};
window.dashboardToolGet=async function(url,opt){const r=await fetch(url,opt);const ct=(r.headers.get('content-type')||'').toLowerCase();let d;try{d=ct.includes('json')?await r.json():{ok:r.ok,text:await r.text()};}catch(e){throw Error('Invalid response from '+url);}if(!r.ok||d.ok===false)throw Error(d.detail||('HTTP '+r.status));return d;};
window.dashboardToolSelected=function(){try{return typeof selectedCameraIds==='function'?selectedCameraIds():[]}catch(e){return[]}};
window.dashboardTool=async function(kind){
  const ids=dashboardToolSelected();
  const esc=x=>String(x??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
  const baseActions='<button class="btn" onclick="closeDashboardTool()">Close</button>';
  try{
    if(kind==='integrity'){openDashboardTool('🧪 Database Integrity',baseActions,'Running…');const d=await dashboardToolGet('/api/advanced/integrity');dashboardToolSet(d.integrity||d.detail||'Integrity check completed',!!d.ok);dashboardToolOut(d);return;}
    if(kind==='backup'){openDashboardTool('💾 Database Backup',baseActions,'Creating verified backup…');const d=await dashboardToolGet('/api/advanced/backup',{method:'POST'});dashboardToolSet(d.detail||'Backup completed',!!d.ok);dashboardToolOut(d);return;}
    if(kind==='diagnostics'){openDashboardTool('🩺 Selected Diagnostics',baseActions,'Preparing…');if(!ids.length){dashboardToolSet('Select one or more cameras on the main dashboard first.',false);dashboardToolOut('0 cameras selected.');return;}const d=await dashboardToolGet('/api/advanced/diagnostics',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids})});dashboardToolSet('Diagnostics completed for '+d.count+' camera(s).',true);dashboardToolOut(d);return;}
    if(kind==='snapshots'){openDashboardTool('📸 Selected Snapshots',baseActions,'Preparing…');if(!ids.length){dashboardToolSet('Select one or more cameras on the main dashboard first.',false);dashboardToolOut('0 cameras selected.');return;}const results=[];for(const id of ids.slice(0,100)){try{const r=await fetch('/snapshot/'+encodeURIComponent(id));if(!r.ok)throw Error('HTTP '+r.status);const b=await r.blob();const u=URL.createObjectURL(b);const a=document.createElement('a');a.href=u;a.download='camera_'+id+'_'+Date.now()+'.jpg';a.click();setTimeout(()=>URL.revokeObjectURL(u),1500);results.push({id,ok:true});}catch(e){results.push({id,ok:false,error:e.message});}}dashboardToolSet('Snapshot operation completed for '+results.length+' camera(s).',true);dashboardToolOut(results);return;}
    if(kind==='feature-matrix'){const d=await dashboardToolGet('/api/v43/features');const suite=d.suite||d;openDashboardTool('🧩 Priority 1–16 Feature Matrix — v73',baseActions,JSON.stringify(suite,null,2));return;}
    if(kind==='layouts'){const d=await dashboardToolGet('/api/v43/layouts');openDashboardTool('▦ Saved Live Layouts',baseActions+'<button class="btn" onclick="saveCurrentDashboardLayout()">💾 Save current</button>',JSON.stringify(d,null,2));return;}
    if(kind==='alerts'){const d=await dashboardToolGet('/api/v43/alerts');openDashboardTool('🔔 Alerts',baseActions+'<button class="btn" onclick="dashboardToolTestAlerts()">🧪 Test alert</button>',JSON.stringify(d,null,2));return;}
    if(kind==='health'){const d=await dashboardToolGet('/api/v43/health?limit=200');openDashboardTool('❤️ Health Monitoring',baseActions+'<button class="btn" onclick="dashboardTool(\'health\')">🔄 Refresh</button>',JSON.stringify(d,null,2));return;}
    if(kind==='performance'){const d=await dashboardToolGet('/api/v43/performance');openDashboardTool('⚡ Performance',baseActions+'<button class="btn" onclick="dashboardTool(\'performance\')">🔄 Refresh</button>',JSON.stringify(d,null,2));return;}
    if(kind==='security'){const d=await dashboardToolGet('/api/v43/security');let a=baseActions+'<button class="btn" onclick="dashboardSecurityChangeOwnPassword()">🔑 Change my password</button>';if(d.current_permissions&&d.current_permissions.includes('security.manage')){a+='<button class="btn" onclick="dashboardSecurityLogoutAll()">🚪 Logout all sessions</button><button class="btn" onclick="dashboardSecurityClearLockouts()">🔓 Clear user lockouts</button>'; }openDashboardTool('🔐 Security',a,'');dashboardSecurityRender(d);return;}
    if(kind==='recovery'){const d=await dashboardToolGet('/api/v43/recovery');openDashboardTool('♻ Stream Recovery',baseActions,JSON.stringify(d,null,2));return;}
    if(kind==='reports'){openDashboardTool('📋 Selected Device Reports',baseActions,'Loading…');if(!ids.length){dashboardToolSet('Select camera(s) first.',false);return;}const out=[];for(const id of ids.slice(0,100))out.push(await dashboardToolGet('/api/v43/device/report/'+encodeURIComponent(id)+'/full'));dashboardToolSet('Loaded '+out.length+' report(s).',true);dashboardToolOut(out);return;}
    if(kind==='maintenance'){if(!confirm('Run maintenance, retention cleanup and create a safety backup?'))return;openDashboardTool('🧹 Maintenance',baseActions,'Running…');const d=await dashboardToolGet('/api/v43/maintenance',{method:'POST'});dashboardToolSet('Maintenance completed.',!!d.ok);dashboardToolOut(d);return;}
  }catch(e){openDashboardTool('Dashboard Work Tool',baseActions);dashboardToolSet(e.message,false);dashboardToolOut(e.message);}
};
window.dashboardToolTestAlerts=async function(){try{const d=await dashboardToolGet('/api/v43/alerts/test',{method:'POST'});dashboardToolSet(d.detail||'Alert test completed.',!!d.ok);dashboardToolOut(d);}catch(e){dashboardToolSet(e.message,false);}};
window.dashboardSecurityLogoutAll=async function(){if(!confirm('Invalidate all dashboard sessions? Every logged-in user will need to sign in again.'))return;try{dashboardToolSet('Invalidating all dashboard sessions…',true);const d=await dashboardToolGet('/api/v43/security/logout-all',{method:'POST'});dashboardToolSet(d.detail||'All sessions invalidated.',true);dashboardToolOut(d);setTimeout(()=>{location.href='/login';},700);}catch(e){dashboardToolSet(e.message,false);dashboardToolOut(e.message);}};
window.dashboardSecurityClearLockouts=async function(){if(!confirm('Clear user-specific login lockouts? The IP anti-bruteforce guard will NOT be cleared.'))return;try{const d=await dashboardToolGet('/api/v43/security/clear-lockouts',{method:'POST'});dashboardToolSet(d.detail||'Lockouts cleared.',true);dashboardToolOut(d);}catch(e){dashboardToolSet(e.message,false);}};
window.saveCurrentDashboardLayout=async function(){const name=prompt('Layout name:','Dashboard Layout');if(!name)return;try{const ids=typeof multiViewIds!=='undefined'?multiViewIds:[];const d=await dashboardToolGet('/api/v43/layouts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,layout:{count:ids.length||4,ids,columns:typeof multiViewColumns==='function'?multiViewColumns(ids.length||4):2}})});dashboardToolSet('Layout saved.',true);dashboardToolOut(d);}catch(e){dashboardToolSet(e.message,false);}};
window.openAdminPopup=function(){const m=document.getElementById('adminPopupModal'),f=document.getElementById('adminPopupFrame');if(!m||!f)return;f.src='/admin?popup=1';m.classList.add('open');m.setAttribute('aria-hidden','false');};
window.closeAdminPopup=function(){const m=document.getElementById('adminPopupModal'),f=document.getElementById('adminPopupFrame');if(m){m.classList.remove('open');m.setAttribute('aria-hidden','true');}if(f)f.src='about:blank';};
window.openAccountPasswordModal=async function(){
  const current=(window.__securityData||{}).current_user||'{{ current_user }}';
  const actions='<button class="btn" onclick="closeDashboardTool()">Close</button>';
  openDashboardTool('🔑 Change my password',actions,'');
  const out=document.getElementById('dashboardToolOutput'); if(!out)return;
  const safe=String(current).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  out.innerHTML='<div style="max-width:520px;border:1px solid var(--border);border-radius:8px;padding:12px"><b>Change password for '+safe+'</b><div style="display:grid;gap:8px;margin-top:10px"><label>Current password<input id="acctCurrentPassword" type="password" style="width:100%;box-sizing:border-box"></label><label>New password<input id="acctNewPassword" type="password" placeholder="8+ characters" style="width:100%;box-sizing:border-box"></label><label>Confirm new password<input id="acctConfirmPassword" type="password" style="width:100%;box-sizing:border-box"></label><button class="btn btn-primary" type="button" onclick="submitAccountPassword()">🔐 Change password</button></div></div>';
};
window.submitAccountPassword=async function(){
  const current=document.getElementById('acctCurrentPassword')?.value||'', np=document.getElementById('acctNewPassword')?.value||'', cp=document.getElementById('acctConfirmPassword')?.value||'';
  if(!current||!np||!cp){dashboardToolSet('All password fields are required.',false);return;}
  if(np!==cp){dashboardToolSet('New passwords do not match.',false);return;}
  try{const d=await dashboardToolGet('/api/account/password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({current_password:current,new_password:np,confirm_password:cp})});dashboardToolSet(d.detail||'Password changed. Please sign in again.',true);setTimeout(()=>location.href=d.redirect||'/login',700);}catch(e){dashboardToolSet(e.message,false);}
};
document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeDashboardTool();closeAdminPopup();}});
document.addEventListener('click',e=>{const m=document.getElementById('dashboardToolModal');if(m&&e.target===m)closeDashboardTool();const a=document.getElementById('adminPopupModal');if(a&&e.target===a)closeAdminPopup();});
</script>

</body></html>'''), content_type='text/html; charset=utf-8')


def _safe_device_camera(cam: dict) -> dict:
    out = dict(cam)
    out.pop('password', None)
    out.pop('username', None)
    raw = out.pop('rtsp_urls', [])
    out['rtsp_display'] = [_mask_rtsp(u) for u in raw]
    return out


def _camera_device_credentials(cam: dict) -> tuple[str, str, str, int]:
    """Resolve device-login fields from a stored row and all RTSP candidates."""
    host = (cam.get('ip') or '').strip()
    username = cam.get('username') or ''
    password = cam.get('password') or ''
    try:
        rtsp_port = int(cam.get('rtsp_port') or 554)
    except (TypeError, ValueError):
        rtsp_port = 554
    for candidate in cam.get('rtsp_urls') or []:
        parsed_host, parsed_user, parsed_password, parsed_port = (
            _rtsp_credentials(candidate))
        host = host or parsed_host
        username = username or parsed_user
        password = password or parsed_password
        if rtsp_port == 554 and parsed_port != 554:
            rtsp_port = parsed_port
        if host and username and password:
            break
    return host, username, password, rtsp_port


def _device_error_category(exc: Exception) -> str:
    if isinstance(exc, (ValueError, csv.Error)):
        return 'invalid_inventory'
    message = str(exc).lower()
    if 'username and password' in message or 'authentication' in message:
        return 'authentication_failed'
    if 'rtsp-only' in message or 'rtsp endpoint' in message:
        return 'rtsp_only'
    if 'rtsp' in message and ('failed' in message or 'pending' in message or 'path' in message):
        return 'rtsp_pending'
    if 'unsupported' in message or 'identification failed' in message:
        return 'vendor_unsupported'
    if 'http port' in message or 'unreachable' in message:
        return 'http_port_unreachable'
    if 'channel enumeration' in message or 'no channels' in message:
        return 'no_channels'
    return 'device_request_failed'


def _parse_device_inventory(path: str) -> list[dict]:
    """Parse explicit user-supplied device inventory without public scanning.

    Accepted inputs:
      * CSV with ip/host, username, password and optional rtsp_url/rtsp_port.
      * ValidCamera-style blocks, including RTSP URL lines.
      * Simple: host:port username password [vendor] (whitespace format cannot safely represent passwords containing spaces; use CSV for those).
      * A bare RTSP URL line, including anonymous RTSP URLs.
      * .m3u/.m3u8 playlists containing RTSP URLs, including anonymous streams.
    """
    devices = []
    ext = os.path.splitext(path)[1].lower()

    def add_target(host='', port=80, username='', password='', vendor='auto',
                   rtsp_port=554, rtsp_url='', country='', region='', city=''):
        host = (host or '').strip()
        rtsp_url = (rtsp_url or '').strip()
        if rtsp_url.lower().startswith(('rtsp://','rtsps://')):
            rh, ru, rpw, rport = _rtsp_credentials(rtsp_url)
            if not host:
                host = rh
            if not username:
                username = ru
            if not password:
                password = rpw
            if not rtsp_port or str(rtsp_port) == '554':
                rtsp_port = rport
        if not host:
            return
        # Standalone RTSP/M3U streams may have no embedded credentials.
        # Keep them as valid inventory targets; authenticated device actions
        # will still require credentials when applicable.
        if not username and not rtsp_url:
            return
        # Empty passwords are valid on some user-managed devices.
        devices.append({
            'host': host, 'port': port or 80, 'username': username,
            'password': password, 'vendor': vendor or 'auto',
            'rtsp_port': rtsp_port or 554, 'country': country or '',
            'region': region or '', 'city': city or '',
            'source_file': path, 'rtsp_url': rtsp_url,
        })

    if ext in {'.m3u', '.m3u8'}:
        content = Path(path).read_text(encoding='utf-8', errors='ignore')
        for raw in content.splitlines():
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            if line.lower().startswith(('rtsp://','rtsps://')):
                add_target(rtsp_url=line.split('#', 1)[0].strip())
        return devices

    if ext == '.csv':
        with open(path, newline='', encoding='utf-8-sig', errors='ignore') as fp:
            for row in csv.DictReader(fp):
                r = {str(k).strip().lower(): str(v or '').strip()
                     for k, v in row.items()}
                add_target(
                    r.get('ip') or r.get('ip_address') or r.get('ip address') or
                    r.get('host') or r.get('hostname') or r.get('address') or r.get('device'),
                    r.get('port') or r.get('http_port') or r.get('web_port') or 80,
                    r.get('username') or r.get('user') or r.get('login') or r.get('userid'),
                    r.get('password') or r.get('pass') or r.get('passwd') or r.get('pwd'),
                    r.get('vendor') or r.get('brand') or r.get('type') or r.get('camera_type') or 'auto',
                    r.get('rtsp_port') or r.get('stream_port') or r.get('rtspport') or 554,
                    r.get('rtsp_url') or r.get('stream_url') or r.get('stream') or
                    r.get('url') or r.get('link') or '',
                    r.get('country', ''), r.get('region') or r.get('region/state') or '',
                    r.get('city', ''))
        return devices

    content = Path(path).read_text(encoding='utf-8', errors='ignore')
    if re.search(r'camera\s+type\s*:|ip\s+address\s*:|valid\s*camera|rtsp://', content, re.I):
        normalized = re.sub(r'(?m)^\s*={10,}\s*$', '\n\n', content)
        blocks = re.split(r'\n\s*\n+', normalized.strip())
        for block in blocks:
            cam = _parse_valid_camera_block(block, path)
            # If the block contains only an RTSP URL, parse it directly.
            rtsp_urls = cam.get('rtsp_urls', []) if cam else []
            if cam:
                rtsp_url = rtsp_urls[0] if rtsp_urls else ''
                add_target(cam.get('ip', ''), cam.get('port') or 80,
                           cam.get('username', ''), cam.get('password', ''),
                           cam.get('camera_type') or 'auto',
                           cam.get('rtsp_port') or 554, rtsp_url,
                           cam.get('country', ''), cam.get('region', ''), cam.get('city', ''))
            elif rtsp_urls:
                add_target(rtsp_url=rtsp_urls[0])
        return devices

    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.lower().startswith(('rtsp://','rtsps://')):
            add_target(rtsp_url=line.split('#', 1)[0].strip())
            continue
        parts = [p.strip() for p in re.split(r'[,;\s]+', line) if p.strip()]
        if len(parts) < 1:
            continue
        # A bare stream URL is valid even without credentials.
        if parts[0].lower().startswith(('rtsp://', 'rtsps://')):
            add_target(rtsp_url=parts[0])
            continue
        if len(parts) < 3:
            continue
        target = parts[0]
        host, port = (target.rsplit(':', 1) if
                      ':' in target and target.rsplit(':', 1)[1].isdigit()
                      else (target, '80'))
        add_target(host, port, parts[1], parts[2],
                   parts[3] if len(parts) > 3 else 'auto', 554)
    return devices


def _inventory_job_row(job_id: str) -> dict:
    row = _db_conn().execute(
        'SELECT * FROM inventory_jobs WHERE id=?', (job_id,)
    ).fetchone()
    if not row:
        return {'ok': False, 'detail': 'Inventory job not found'}
    data = dict(row)
    try:
        payload = json.loads(data.pop('results_json') or '{}')
    except Exception:
        payload = {'devices': [], 'errors': []}
    data.update(payload)
    data['ok'] = True
    data['complete'] = data.get('status') == 'completed'
    data['progress_percent'] = round(
        (float(data.get('processed') or 0) / max(1, int(data.get('total') or 0))) * 100, 1
    ) if data.get('total') else 0.0
    return data


def _inventory_job_update(job_id: str, **fields) -> None:
    allowed = {
        'status', 'total', 'processed', 'imported', 'failed',
        'started_at', 'finished_at', 'error', 'results_json', 'targets_json'
    }
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    assignments = ', '.join(f'{k}=?' for k in fields)
    values = list(fields.values()) + [job_id]
    conn = _db_conn()
    conn.execute(f'UPDATE inventory_jobs SET {assignments} WHERE id=?', values)
    conn.commit()


def _protect_inventory_targets(targets):
    raw=json.dumps(targets, ensure_ascii=False, separators=(',',':')).encode('utf-8')
    secret=(os.getenv('SESSION_SECRET') or '').encode('utf-8')
    if not secret: raise RuntimeError('SESSION_SECRET must be configured to persist inventory credentials safely')
    key=hashlib.sha256(secret+b'|inventory-targets|').digest(); stream=bytearray(); counter=0
    while len(stream)<len(raw): stream.extend(hashlib.sha256(key+counter.to_bytes(8,'big')).digest()); counter+=1
    enc=bytes(a^b for a,b in zip(raw,stream)); mac=hmac.new(key,enc,hashlib.sha256).hexdigest()
    return json.dumps({'v':1,'mac':mac,'data':base64.b64encode(enc).decode('ascii')},separators=(',',':'))

def _unprotect_inventory_targets(blob):
    try:
        obj=json.loads(blob or '{}'); secret=(os.getenv('SESSION_SECRET') or '').encode('utf-8')
        if not secret or obj.get('v')!=1: return []
        key=hashlib.sha256(secret+b'|inventory-targets|').digest(); enc=base64.b64decode(obj.get('data',''))
        if not hmac.compare_digest(hmac.new(key,enc,hashlib.sha256).hexdigest(),str(obj.get('mac',''))): return []
        stream=bytearray(); counter=0
        while len(stream)<len(enc): stream.extend(hashlib.sha256(key+counter.to_bytes(8,'big')).digest()); counter+=1
        return json.loads(bytes(a^b for a,b in zip(enc,stream)).decode('utf-8'))
    except Exception: return []


def _run_inventory_job(job_id: str, targets: list[dict], filename: str) -> None:
    started = utc_iso('seconds')
    imported, errors = [], []
    processed = 0
    _touch_worker('inventory-import', 'running', f'job={job_id} total={len(targets)}')
    _inventory_job_update(job_id, status='running', started_at=started,
                          total=len(targets), processed=0, imported=0, failed=0)
    try:
        def one(target):
            try:
                supplied_rtsp = str(target.get('rtsp_url') or '').strip()
                if supplied_rtsp.lower().startswith(('rtsp://', 'rtsps://')):
                    # Playlist/RTSP inventory must not require a management login.
                    # Preserve the exact feed; validation can test it independently.
                    rh, ru, rpw, rport = _rtsp_credentials(supplied_rtsp)
                    host = rh or str(target.get('host') or '')
                    user = ru or str(target.get('username') or '')
                    password = rpw or str(target.get('password') or '')
                    cid = _cam_id('', rport, user, supplied_rtsp)
                    channel = {
                        'id': cid,
                        'ip': host,
                        'port': rport,
                        'camera_type': 'RTSP Stream',
                        'label': f'RTSP — {host or supplied_rtsp[:50]}',
                        'rtsp_urls': [supplied_rtsp],
                        'web_url': '',
                        'source_file': target.get('source_file', filename),
                        'country': target.get('country', ''),
                        'region': target.get('region', ''),
                        'city': target.get('city', ''),
                        'username': user,
                        'password': password,
                        'rtsp_port': rport,
                        'group_name': target.get('country') or 'RTSP Import',
                        'device_id': 'device-' + _cam_id(host, rport, user),
                        'channel_index': 1,
                        'channel_name': 'Imported RTSP Stream',
                    }
                    db_bulk_upsert([channel])
                    return ('ok', {
                        'device_id': channel['device_id'], 'host': host,
                        'vendor': 'generic_rtsp', 'status': 'rtsp_pending',
                        'rtsp_pending': True, 'channels': 1,
                    })
                result = discover_local_device(**target, enrich_geo=False)
                channels = result.get('channels') or []
                if channels:
                    db_bulk_upsert(channels)
                return ('ok', {
                    'device_id': result.get('device_id', ''),
                    'host': target.get('host', ''),
                    'vendor': result.get('vendor', target.get('vendor', 'auto')),
                    'status': ('rtsp_only' if result.get('vendor') in ('generic_rtsp', 'generic_rtsp_pending') else ('rtsp_pending' if any('pending' in str(c.get('name','')).lower() for c in channels) else 'authenticated')),
                    'rtsp_pending': any('pending' in str(c.get('name','')).lower() for c in channels),
                    'channels': len(channels),
                })
            except Exception as exc:
                return ('error', {
                    'host': target.get('host', ''),
                    'category': _device_error_category(exc),
                    'detail': str(exc)[:240],
                })

        with ThreadPoolExecutor(max_workers=INVENTORY_WORKERS,
                                thread_name_prefix='inventory') as pool:
            futures = [pool.submit(one, target) for target in targets]
            for future in as_completed(futures):
                kind, item = future.result()
                if kind == 'ok':
                    imported.append(item)
                else:
                    errors.append(item)
                processed += 1
                _inventory_job_update(
                    job_id, processed=processed, imported=len(imported),
                    failed=len(errors),
                    results_json=json.dumps({'devices': imported, 'errors': errors}, ensure_ascii=False)
                )
                _touch_worker('inventory-import', 'running',
                              f'job={job_id} {processed}/{len(targets)}')
        finished = utc_iso('seconds')
        _inventory_job_update(
            job_id, status='completed', finished_at=finished,
            processed=processed, imported=len(imported), failed=len(errors),
            results_json=json.dumps({'devices': imported, 'errors': errors}, ensure_ascii=False),
            targets_json='[]'
        )
        _touch_worker('inventory-import', 'idle', f'job={job_id} completed')
    except Exception as exc:
        log.exception('Inventory job %s failed', job_id)
        finished = utc_iso('seconds')
        _inventory_job_update(
            job_id, status='failed', finished_at=finished, error=type(exc).__name__,
            processed=processed, imported=len(imported), failed=max(1, len(errors)),
            results_json=json.dumps({'devices': imported, 'errors': errors}, ensure_ascii=False),
            targets_json='[]'
        )
        _touch_worker('inventory-import', 'error', f'job={job_id}: {type(exc).__name__}')
    finally:
        with _inventory_jobs_lock:
            _inventory_job_threads.pop(job_id, None)


@app.route('/api/device/inventory-import', methods=['POST'])
def api_device_inventory_import() -> Response:
    """Queue an inventory import and return immediately.

    Large inventories must never be processed inside the HTTP request. Replit
    and other reverse proxies can return 502 when a request stays open while
    dozens/hundreds of device logins are running.
    """
    denied = _admin_only()
    if denied:
        return denied
    upload = request.files.get('file')
    if not upload:
        return jsonify({'ok': False, 'detail': 'No device inventory file uploaded'}), 400
    ext = os.path.splitext(upload.filename or '')[1].lower()
    if ext not in {'.csv', '.txt', '.m3u', '.m3u8'}:
        return jsonify({'ok': False, 'detail': 'Use a .csv, .txt, .m3u, or .m3u8 device inventory'}), 400
    tmp_path = ''
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            upload.save(tmp)
            tmp_path = tmp.name
        targets = _parse_device_inventory(tmp_path)
        if not targets:
            return jsonify({
                'ok': False, 'category': 'no_rows_parsed',
                'detail': 'No rows with host, username, and password were found',
            }), 400
        if len(targets) > INVENTORY_MAX_RESULTS:
            return jsonify({
                'ok': False, 'category': 'inventory_too_large',
                'detail': f'Inventory contains {len(targets)} devices; maximum is {INVENTORY_MAX_RESULTS}',
            }), 400
        inventory_source = 'inventory://' + os.path.basename(upload.filename or 'device-inventory')
        for target in targets:
            target['source_file'] = inventory_source

        job_id = uuid.uuid4().hex
        conn = _db_conn()
        conn.execute(
            """INSERT INTO inventory_jobs
               (id, filename, status, total, results_json, targets_json)
               VALUES (?, ?, 'queued', ?, ?, ?)""",
            (job_id, os.path.basename(upload.filename or 'device-inventory'),
             len(targets), json.dumps({'devices': [], 'errors': []}),
             _protect_inventory_targets(targets))
        )
        conn.commit()

        thread = threading.Thread(
            target=_run_inventory_job,
            args=(job_id, targets, os.path.basename(upload.filename or 'device-inventory')),
            daemon=True,
            name=f'inventory-job-{job_id[:8]}',
        )
        with _inventory_jobs_lock:
            _inventory_job_threads[job_id] = thread
        thread.start()
        return jsonify({
            'ok': True,
            'queued': True,
            'job_id': job_id,
            'target_count': len(targets),
            'workers': INVENTORY_WORKERS,
            'detail': f'Inventory queued: {len(targets)} device(s)',
        }), 202
    except Exception as exc:
        log.exception('Inventory upload/queue failed')
        return jsonify({'ok': False,
                        'detail': f'Inventory import failed: {type(exc).__name__}: {str(exc)[:180]}'}), 500
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


@app.route('/api/device/inventory-import/status/<job_id>', methods=['GET'])
def api_device_inventory_import_status(job_id: str) -> Response:
    denied = _admin_only()
    if denied:
        return denied
    return jsonify(_inventory_job_row(job_id))


@app.route('/api/device/inventory-import/cancel/<job_id>', methods=['POST'])
def api_device_inventory_import_cancel(job_id: str) -> Response:
    denied = _admin_only()
    if denied:
        return denied
    row = _db_conn().execute(
        'SELECT status FROM inventory_jobs WHERE id=?', (job_id,)
    ).fetchone()
    if not row:
        return jsonify({'ok': False, 'detail': 'Inventory job not found'}), 404
    if row['status'] in ('completed', 'failed', 'cancelled'):
        return jsonify({'ok': True, 'detail': 'Job already finished'})
    _inventory_job_update(job_id, status='cancelled', finished_at=utc_iso('seconds'))
    return jsonify({'ok': True, 'detail': 'Cancellation requested'})


@app.route('/api/cameras/<cam_id>/detect-feed', methods=['POST'])
def api_camera_detect_feed(cam_id: str) -> Response:
    denied = _admin_only()
    if denied:
        return denied
    row = _db_conn().execute(
        'SELECT * FROM cameras WHERE deleted=0 AND id=?', (cam_id,)).fetchone()
    if not row:
        return jsonify({'ok': False, 'detail': 'Camera not found'}), 404
    cam = _db_row(row)
    try:
        result = _detect_rtsp_feed_for_camera(cam)
        feed_kind = 'rtsp'
        http_result = {}
        if not result.get('ok'):
            http_result = _detect_http_video_feed_for_camera(cam)
            if http_result.get('ok'):
                result = http_result
                feed_kind = 'http'
            else:
                return jsonify({
                    'ok': False,
                    'detail': 'No working video feed found',
                    'rtsp': result,
                    'http': http_result,
                }), 422
        if feed_kind == 'rtsp':
            found_url = result['rtsp_url']
            db_promote_rtsp_url(cam_id, found_url)
        c = _db_conn()
        if feed_kind == 'rtsp':
            # rtsp_port exists on new DBs and is migrated above for legacy DBs.
            c.execute(
                "UPDATE cameras SET rtsp_port=?, "
                "camera_type=CASE WHEN COALESCE(camera_type,'') IN ('','generic','Generic') "
                "THEN ? ELSE camera_type END, updated_at=datetime('now') WHERE id=?",
                (int(result['port']), result.get('vendor') or 'generic', cam_id),
            )
        else:
            c.execute(
                "UPDATE cameras SET web_url=?, updated_at=datetime('now') WHERE id=?",
                (result.get('safe_url') or cam.get('web_url') or '', cam_id),
            )
        c.commit()
        return jsonify({
            'ok': True,
            'camera_id': cam_id,
            'feed_type': feed_kind,
            'safe_url': result.get('safe_url', ''),
            'port': result.get('port'),
            'http_port': result.get('http_port'),
            'vendor': result.get('vendor', 'generic'),
            'codec': result.get('codec', ''),
            'width': result.get('width'),
            'height': result.get('height'),
            'fps': result.get('fps', ''),
            'reachable_ports': result.get('reachable_ports', []),
            'http_services': result.get('http_services', []),
            'tried': result.get('tried', 0),
            'detail': ('Authenticated RTSP video feed detected and saved.'
                       if feed_kind == 'rtsp'
                       else 'Authenticated HTTP video service detected and saved.'),
        })
    except Exception as exc:
        log.exception('Detect feed failed for camera %s', cam_id)
        return jsonify({
            'ok': False,
            'category': 'feed_detection_error',
            'detail': f'{type(exc).__name__}: {str(exc)[:240]}',
        }), 400


@app.route('/api/cameras/<cam_id>/device-check', methods=['POST'])
def api_camera_device_check(cam_id: str) -> Response:
    """Authenticate and inspect the explicitly supplied camera/device.

    Important: this route must not require NVR vendor identification to succeed.
    A public IP can expose a door camera web service on :80 and an NVR on another
    port. We therefore probe the complete bounded HTTP/HTTPS port matrix first,
    then attempt vendor discovery only when a verified management API exists.
    """
    denied = _admin_only()
    if denied:
        return denied
    payload = request.get_json(silent=True) or {}
    row = _db_conn().execute(
        'SELECT * FROM cameras WHERE deleted=0 AND id=?', (cam_id,)).fetchone()
    if not row:
        return jsonify({'ok': False, 'detail': 'Camera not found'}), 404
    cam = _db_row(row)
    host, username, password, rtsp_port = _camera_device_credentials(cam)
    if not host:
        return jsonify({'ok': False, 'detail': 'Camera has no device host'}), 400

    target_group = _normalize_group_name(payload.get('group') or '')
    rtsp_url = (cam.get('rtsp_urls') or [''])[0] if cam.get('rtsp_urls') else ''

    try:
        # 1) Always inspect every bounded HTTP/HTTPS port. Port 80 is not special.
        http_probe = _probe_device_http_ports(
            host, DEVICE_HTTP_PORTS, username=username, password=password)

        # 2) RTSP reachability is reported separately.
        try:
            rtsp_p = int(rtsp_port or 554)
        except (TypeError, ValueError):
            rtsp_p = 554
        rtsp_result = {
            'reachable': False,
            'port': rtsp_p,
            'latency_ms': None,
            'detail': 'not tested',
        }
        started = time.monotonic()
        try:
            with socket.create_connection(
                    (host, rtsp_p), timeout=DEVICE_PORT_SCAN_TIMEOUT):
                rtsp_result.update({
                    'reachable': True,
                    'latency_ms': round((time.monotonic() - started) * 1000),
                    'detail': 'TCP reachable',
                })
        except Exception as exc:
            rtsp_result['detail'] = type(exc).__name__

        # 3) If a known vendor API is actually verified, use full read-only
        # discovery. Otherwise do NOT fail merely because this is a generic
        # door-camera web service.
        verified_reason = http_probe.get('selection_reason', '')
        result = None
        if verified_reason in {
            'hikvision_api', 'dahua_api', 'uniview_api', 'onvif_api'
        }:
            result = discover_local_device(
                host, 'auto', username, password,
                cam.get('camera_type') or 'auto', rtsp_p, True,
                cam.get('country', ''), cam.get('region', ''),
                cam.get('city', ''),
                f'device://camera-check/{cam_id}', rtsp_url=rtsp_url)

        services = http_probe.get('http_services') or []
        authenticated_services = [
            x for x in services
            if x.get('auth') == 'authenticated-or-public'
        ]

        # Prefer an actually identified vendor service; otherwise use the first
        # authenticated web service as the device's web endpoint.
        web_service = None
        if result:
            web_service = {'port': result.get('port'),
                           'scheme': 'https' if int(result.get('port', 0)) in DEVICE_HTTPS_PORTS else 'http'}
        elif authenticated_services:
            web_service = authenticated_services[0]
        elif services:
            web_service = services[0]

        if result:
            device_id = result['device_id']
            info = dict(result.get('info') or {})
            vendor = result.get('vendor') or 'generic'
            selected_port = result.get('port')
            channels_count = len(result.get('channels') or [])
            geo = result.get('geo', {})
            web_url = ((result.get('channels') or [{}])[0].get('web_url', '')
                       or (f"http://{host}:{selected_port}" if selected_port else ''))
        else:
            selected_port = (web_service or {}).get('port')
            scheme = (web_service or {}).get('scheme') or 'http'
            web_url = (f'{scheme}://{host}:{int(selected_port)}'
                       if selected_port else '')
            hint = str((web_service or {}).get('camera_hint') or '').strip()
            vendor = hint or cam.get('camera_type') or 'generic'
            title = str((web_service or {}).get('title') or '').strip()
            server = str((web_service or {}).get('server') or '').strip()
            info = {
                'raw_summary': 'Authenticated web endpoint',
                'web_title': title,
                'server': server,
                'camera_hint': hint,
            }
            # A standalone RTSP/M3U/JSON stream has no NVR channel API.
            # If the supplied/stored stream is present, represent it as one
            # standalone stream instead of incorrectly reporting Channels: 0.
            stream_urls = cam.get('rtsp_urls') or []
            standalone_stream = bool(rtsp_url or stream_urls) or str(cam.get('camera_type') or '').lower() in {
                'm3u stream', 'json stream', 'rtsp stream', 'standalone stream'
            }
            channels_count = 1 if standalone_stream else 0
            device_class = 'standalone_stream' if standalone_stream else 'generic_device'
            geo = {
                'country': cam.get('country', ''),
                'region': cam.get('region', ''),
                'city': cam.get('city', ''),
            }
            device_id = _cam_id(host, int(selected_port or 0), username)

        # Save only the selected web URL/category when explicitly requested;
        # never overwrite the whole device inventory from an unverified generic
        # web page.
        c = _db_conn()
        if web_url:
            c.execute(
                "UPDATE cameras SET web_url=?, updated_at=datetime('now') WHERE id=?",
                (web_url, cam_id))
        if target_group:
            c.execute(
                "UPDATE cameras SET group_name=?, updated_at=datetime('now') "
                "WHERE deleted=0 AND id=?", (target_group, cam_id))
        c.commit()

        if not authenticated_services and not result and not rtsp_result['reachable']:
            return jsonify({
                'ok': False,
                'category': 'no_authenticated_service',
                'detail': 'No authenticated HTTP/HTTPS service or reachable RTSP port was found.',
                'host': host,
                'http_probe': http_probe,
                'rtsp': rtsp_result,
            }), 422

        device_name = (info.get('device_name') or info.get('model') or
                       (web_service or {}).get('title') or vendor or
                       ('CCTV ' + host))
        return jsonify({
            'ok': True,
            'device_id': device_id,
            'device_name': device_name,
            'vendor': vendor,
            'host': host,
            'port': selected_port,
            'rtsp_port': rtsp_p,
            'rtsp': rtsp_result,
            'info': info,
            'geo': geo,
            'http_probe': http_probe,
            'count': channels_count,
            'stream_count': 1 if device_class == 'standalone_stream' and rtsp_result.get('reachable') else (1 if device_class == 'standalone_stream' and (rtsp_url or cam.get('rtsp_urls')) else 0),
            'device_class': device_class,
            'stream_detected': bool(device_class == 'standalone_stream' and (rtsp_result.get('reachable') or rtsp_url or cam.get('rtsp_urls'))),
            'saved_category': target_group,
            'web_url': web_url,
            'management_port_verified': verified_reason in {
                'hikvision_api', 'dahua_api', 'uniview_api', 'onvif_api'
            },
            'service_count': len(services),
            'authenticated_http_count': len(authenticated_services),
        })
    except Exception as exc:
        log.exception('Camera device check failed for %s', cam_id)
        return jsonify({
            'ok': False,
            'category': 'device_check_error',
            'detail': f'{type(exc).__name__}: {str(exc)[:300]}',
            'host': host,
        }), 400

@app.route('/api/cameras/<cam_id>/device-login', methods=['POST'])
def api_camera_device_login(cam_id: str) -> Response:
    denied = _admin_only()
    if denied:
        return denied
    row = _db_conn().execute(
        'SELECT * FROM cameras WHERE deleted=0 AND id=?', (cam_id,)).fetchone()
    if not row:
        return jsonify({'ok': False, 'detail': 'Camera not found'}), 404
    cam = _db_row(row)
    host, username, password, rtsp_port = _camera_device_credentials(cam)
    if not host:
        return jsonify({'ok': False, 'category': 'invalid_inventory',
                        'detail': 'Camera has no device host'}), 400
    try:
        result = discover_local_device(
            host, 'auto', username, password, cam.get('camera_type') or 'auto',
            rtsp_port, False, cam.get('country', ''), cam.get('region', ''),
            cam.get('city', ''), f'device://camera-login/{cam_id}')
        db_bulk_upsert(result['channels'])
        safe = [_safe_device_camera(item) for item in result['channels']]
        return jsonify({
            'ok': True, 'device_id': result['device_id'],
            'vendor': result['vendor'], 'host': result['host'],
            'port': result['port'], 'info': result['info'],
            'geo': result['geo'], 'http_probe': result.get('http_probe', {}),
            'count': len(safe), 'channels': safe,
        })
    except Exception as exc:
        log.info('Camera device login failed for %s: %s', cam_id,
                 type(exc).__name__)
        return jsonify({'ok': False, 'category': _device_error_category(exc),
                        'detail': str(exc)[:240]}), 400


def _intel_xml_payload(root):
    """Convert vendor XML into bounded nested data while preserving record grouping."""
    def convert(elem, depth=0):
        if depth > 12:
            return (elem.text or '').strip()[:500]
        children = list(elem)
        if not children:
            return (elem.text or '').strip()[:500]
        out = {}
        for child in children[:1000]:
            key = child.tag.rsplit('}', 1)[-1]
            if any(x in key.lower() for x in ('password','passwd','secret','token','credential','privatekey')):
                continue
            value = convert(child, depth + 1)
            if key in out:
                if not isinstance(out[key], list): out[key] = [out[key]]
                if len(out[key]) < 500: out[key].append(value)
            else:
                out[key] = value
        # Include useful attributes without exposing possible secrets.
        for key, value in list(elem.attrib.items())[:30]:
            if not any(x in key.lower() for x in ('password','passwd','secret','token','credential')):
                out.setdefault('@'+key, str(value)[:200])
        return out
    return {root.tag.rsplit('}', 1)[-1]: convert(root)}


def _intel_parse_text(text):
    out = {}
    for line in str(text or '').splitlines()[:500]:
        if '=' not in line: continue
        k, v = line.split('=', 1)
        k, v = k.strip(), v.strip()
        if not k or any(x in k.lower() for x in ('password','passwd','secret','token')): continue
        out[k[:100]] = v[:500]
    return out


@app.route('/api/cameras/<cam_id>/intelligence/<section>', methods=['POST'])
def api_camera_device_intelligence(cam_id: str, section: str) -> Response:
    """On-demand, bounded, read-only diagnostics for one known inventory device."""
    denied = _admin_only()
    if denied: return denied
    allowed = {'storage','users','channels','playback','performance','health','alerts'}
    if section not in allowed:
        return jsonify({'ok':False,'detail':'Unknown diagnostic section'}), 400
    row = _db_conn().execute('SELECT * FROM cameras WHERE deleted=0 AND id=?', (cam_id,)).fetchone()
    if not row: return jsonify({'ok':False,'detail':'Camera not found'}), 404
    cam = _db_row(row)
    host, username, password, rtsp_port = _camera_device_credentials(cam)
    if not host: return jsonify({'ok':False,'detail':'Camera has no device host'}), 400
    try: port = int(cam.get('port') or 80)
    except Exception: port = 80
    vendor_hint = str(cam.get('camera_type') or '').lower()
    vendor = ('hikvision' if any(x in vendor_hint for x in ('hikvision','hik')) else
              'dahua' if any(x in vendor_hint for x in ('dahua','amcrest','xmeye')) else
              'uniview' if any(x in vendor_hint for x in ('uniview','unv')) else 'unknown')
    if cam.get('device_id'):
        device_rows = _db_conn().execute(
            'SELECT id,channel_index,channel_name,status FROM cameras WHERE deleted=0 AND device_id=? ORDER BY channel_index,id',
            (cam.get('device_id'),)).fetchall()
    else:
        device_rows = _db_conn().execute(
            'SELECT id,channel_index,channel_name,status FROM cameras WHERE deleted=0 AND ip=? AND port=? ORDER BY channel_index,id',
            (host, port)).fetchall()
    if not device_rows: device_rows = [row]
    with _streams_lock:
        local_live = sum(int(_stream_subscribers.get(str(x['id']), 0) or 0) for x in device_rows)
        live_camera_ids = [str(x['id']) for x in device_rows if int(_stream_subscribers.get(str(x['id']),0) or 0)>0]
    local_status = {
        'inventory_channels': len(device_rows),
        'channels_marked_online': sum(str(x['status']).lower() == 'online' for x in device_rows),
        'dashboard_live_view_subscribers': local_live,
        'dashboard_live_camera_ids': live_camera_ids,
        'note': 'Dashboard subscriber count is not the same as NVR physical live-channel status.'
    }
    if _requests is None:
        return jsonify({'ok':True,'available':False,'section':section,'host':host,'vendor':vendor,'data':local_status,
                        'note':'requests package is unavailable; remote device diagnostics could not run.'})
    sess, auths = _device_session(username, password)
    schemes = ['https','http'] if port in DEVICE_HTTPS_PORTS else ['http','https']
    bases = [f'{scheme}://{host}:{port}' for scheme in schemes]
    # For known vendor-specific read-only API endpoints. Each click performs
    # only the requested section; no periodic or automatic polling is added.
    candidates = {
      'channels': {
        'hikvision':['/ISAPI/ContentMgmt/InputProxy/channels','/ISAPI/Streaming/channels'],
        'dahua':['/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle','/cgi-bin/devVideoInput.cgi?action=getCollect'],
        'uniview':['/LAPI/V1.0/Channels','/LAPI/V1.0/Channels/Status']},
      'storage': {
        'hikvision':['/ISAPI/System/Storage/volumes','/ISAPI/ContentMgmt/Storage'],
        'dahua':['/cgi-bin/storageDevice.cgi?action=getDeviceAllInfo','/cgi-bin/storageDevice.cgi?action=getDeviceInfo'],
        'uniview':['/LAPI/V1.0/Storage/Disks','/LAPI/V1.0/Storage/Disks/Status']},
      'users': {
        'hikvision':['/ISAPI/Security/users','/ISAPI/System/Users'],
        'dahua':['/cgi-bin/userManager.cgi?action=getUserInfoAll'],
        'uniview':['/LAPI/V1.0/Users','/LAPI/V1.0/UserMgmt/Users']},
      'performance': {
        'hikvision':['/ISAPI/System/status','/ISAPI/System/health'],
        'dahua':['/cgi-bin/magicBox.cgi?action=getSystemInfo','/cgi-bin/magicBox.cgi?action=getSystemStatus'],
        'uniview':['/LAPI/V1.0/System/DeviceInfo','/LAPI/V1.0/System/Status']},
      'health': {
        'hikvision':['/ISAPI/System/status','/ISAPI/System/health','/ISAPI/System/Storage/volumes'],
        'dahua':['/cgi-bin/magicBox.cgi?action=getSystemInfo','/cgi-bin/storageDevice.cgi?action=getDeviceAllInfo'],
        'uniview':['/LAPI/V1.0/System/Status','/LAPI/V1.0/Storage/Disks']},
      'alerts': {
        'hikvision':['/ISAPI/System/Log/entries','/ISAPI/ContentMgmt/logSearch'],
        'dahua':['/cgi-bin/eventManager.cgi?action=getEventIndexes','/cgi-bin/log.cgi?action=getLog'],
        'uniview':['/LAPI/V1.0/Alarm/Events','/LAPI/V1.0/System/Logs']},
      'playback': {
        'hikvision':['/ISAPI/ContentMgmt/record/tracks','/ISAPI/ContentMgmt/record/tracks/1'],
        'dahua':['/cgi-bin/recordManager.cgi?action=getRecordInfo','/cgi-bin/storageDevice.cgi?action=getDeviceAllInfo'],
        'uniview':['/LAPI/V1.0/Record/Status','/LAPI/V1.0/Record/Config']}
    }
    parsed = None; endpoint_used = ''; status_code = None; last_error = ''
    vendor_keys = [vendor] if vendor in ('hikvision','dahua','uniview') else ['hikvision','dahua','uniview']
    paths = []
    for vk in vendor_keys: paths.extend(candidates[section].get(vk, []))
    for base in bases:
        for path in paths[:6]:
            try:
                response = _device_get(sess, auths, base + path, timeout=3.5)
                if not hasattr(response, 'status_code'):
                    last_error = type(response).__name__; continue
                status_code = int(response.status_code)
                if status_code != 200: continue
                content_type = (response.headers.get('Content-Type') or '').lower()
                body = response.text[:200000]
                if '<' in body[:100] or 'xml' in content_type:
                    try: parsed = _intel_xml_payload(ElementTree.fromstring(response.content))
                    except Exception: parsed = {'raw_summary': re.sub(r'(?i)<(password|passwd|secret|token)[^>]*>.*?</\1>', '<redacted>', body, flags=re.S)[:3000]}
                else:
                    try:
                        payload = response.json()
                        parsed = payload if isinstance(payload, (dict,list)) else {'result':str(payload)[:3000]}
                    except Exception: parsed = _intel_parse_text(body) or {'raw_summary':body[:3000]}
                endpoint_used = base + path
                break
            except Exception as exc:
                last_error = type(exc).__name__
        if parsed is not None: break
    if section == 'users' and parsed is not None:
        # Recursively redact secrets; only usernames, roles and enable state are useful.
        def safe_users(obj):
            if isinstance(obj, dict):
                return {str(k):safe_users(v) for k,v in obj.items() if not any(t in str(k).lower() for t in ('password','passwd','secret','token','credential','salt','hash'))}
            if isinstance(obj, list): return [safe_users(v) for v in obj[:500]]
            if isinstance(obj, str): return obj[:500]
            return obj
        parsed = safe_users(parsed)
    if section == 'channels':
        data = {'dashboard':local_status, 'remote_channel_response':parsed, 'endpoint':endpoint_used,
                'remote_live_count':None,
                'note':'The remote endpoint may return configured channels rather than actual live decoding state; a count is only reported when the device response explicitly exposes status fields.'}
        def collect_statuses(obj):
            found=[]
            if isinstance(obj,dict):
                for k,v in obj.items():
                    if str(k).lower() in {'status','online','signalstatus','videostatus','connectionstatus'}:
                        if isinstance(v,(str,int,float,bool)): found.append(str(v).lower())
                    found.extend(collect_statuses(v))
            elif isinstance(obj,list):
                for v in obj[:1000]: found.extend(collect_statuses(v))
            return found
        statuses=collect_statuses(parsed)
        if statuses:
            data['remote_status_values']=statuses[:1000]
            data['remote_live_count']=sum(1 for x in statuses if x in {'online','connected','true','1','normal','working','ok'})
        available = parsed is not None
    elif section == 'playback':
        data = {'device_api':parsed, 'retention_days':None,
                'retention_status':'unknown',
                'note':'Storage/record-track endpoints do not reliably reveal oldest recorded date on every model. Exact retention requires a supported recording-search API or a user-supplied device response.'}
        available = parsed is not None
    elif section == 'performance':
        if isinstance(parsed,dict):
            def collect_metrics(obj):
                found={}
                if isinstance(obj,dict):
                    for k,v in obj.items():
                        if any(x in str(k).lower() for x in ('cpu','memory','ram','temperature','uptime','load','fan','voltage')) and not isinstance(v,(dict,list)):
                            found[str(k)]=v
                        else: found.update(collect_metrics(v))
                elif isinstance(obj,list):
                    for item in obj[:500]: found.update(collect_metrics(item))
                return found
            matches=collect_metrics(parsed)
            data={'metrics':matches or parsed,'endpoint':endpoint_used,'local_dashboard_resources':_resource_snapshot()}
        else: data={'remote_metrics':None,'local_dashboard_resources':_resource_snapshot(),'note':'Remote CPU/RAM metrics are not exposed by a supported endpoint on this model.'}
        available = parsed is not None
    elif section == 'health':
        data={'remote_status':parsed,'endpoint':endpoint_used,'local_inventory':local_status}
        available = parsed is not None
    elif section == 'alerts':
        try:
            rows = _db_conn().execute("SELECT * FROM device_events WHERE camera_id=? ORDER BY id DESC LIMIT 100",(cam_id,)).fetchall()
            local_events=[dict(x) for x in rows]
        except Exception: local_events=[]
        data={'remote_alerts':parsed,'recent_dashboard_device_events':local_events,'endpoint':endpoint_used,
              'note':'Remote alert/log APIs differ by firmware; dashboard events are shown separately.'}
        available = parsed is not None or bool(local_events)
    else:
        data = {'result':parsed,'endpoint':endpoint_used}
        available = parsed is not None
    if not available:
        data['unavailable_reason'] = ('Device API returned no supported response.' if not last_error else 'Device request failed: '+last_error)
        data['hint'] = 'Verify the saved management port, device username/password, vendor, and firmware API permissions. No device configuration was changed.'
    return jsonify({'ok':True,'available':available,'section':section,'host':host,'vendor':vendor,
                    'http_status':status_code,'data':data,'note':'Read-only, on-demand request; no background polling.'})


@app.route('/api/cameras/<cam_id>/geo-update', methods=['POST'])
def api_camera_geo_update(cam_id: str) -> Response:
    """Explicitly look up approximate public-IP geolocation or save operator-entered location."""
    denied = _admin_only()
    if denied: return denied
    row = _db_conn().execute('SELECT * FROM cameras WHERE deleted=0 AND id=?',(cam_id,)).fetchone()
    if not row: return jsonify({'ok':False,'detail':'Camera not found'}),404
    cam = _db_row(row); payload = request.get_json(silent=True) or {}
    mode = str(payload.get('mode') or 'manual').lower()
    if mode == 'lookup':
        geo = _device_geo(str(cam.get('ip') or ''), True)
        if not geo or not any(str(geo.get(k) or '').strip() for k in ('city','region','country')):
            # Preserve any already saved site/location fields instead of implying
            # that a live camera's private IP can be geolocated.
            return jsonify({'ok':True,'updated':False,'geo':geo,
                'detail':str((geo or {}).get('detail') or 'No geolocation provider returned a usable city, region, or country. Existing saved location was not changed.')})
        fields = {k:(str(geo.get(src) or '').strip() or str(cam.get(k) or '').strip()) for k,src in [('country','country'),('region','region'),('city','city')]}
        fields['geo_source']=str(geo.get('geo_source') or 'ip-geolocation')
        fields['geo_confidence']=str(geo.get('geo_confidence') or 'approximate')
        fields['location_updated_at']=utc_iso()
        _location_text = ', '.join(x for x in (fields['city'],fields['region'],fields['country']) if x)
        _old_description = str(cam.get('description') or '').strip()
        fields['description'] = _old_description or _location_text
        if _location_text and _location_text.lower() not in fields['description'].lower():
            fields['description'] = (fields['description'] + ' | Location: ' + _location_text).strip(' |')
        c = _db_conn()
        device_id = str(cam.get('device_id') or '')
        if device_id:
            c.execute('UPDATE cameras SET country=?,region=?,city=?,geo_source=?,geo_confidence=?,location_updated_at=?,updated_at=datetime(\'now\') WHERE deleted=0 AND device_id=?',
                      (fields['country'],fields['region'],fields['city'],fields['geo_source'],fields['geo_confidence'],fields['location_updated_at'],device_id))
        else:
            c.execute('UPDATE cameras SET country=?,region=?,city=?,geo_source=?,geo_confidence=?,location_updated_at=?,updated_at=datetime(\'now\') WHERE deleted=0 AND id=?',
                      (fields['country'],fields['region'],fields['city'],fields['geo_source'],fields['geo_confidence'],fields['location_updated_at'],cam_id))
        c.execute('UPDATE cameras SET description=? WHERE id=?', (fields['description'],cam_id))
        c.commit()
        return jsonify({'ok':True,'updated':True,'approximate':True,'geo':geo,'description':fields['description'],
                        'detail':'Approximate public-IP location saved. It is not GPS and may be inaccurate.'})
    # Manual mode: blank values preserve current values; explicit operator input is authoritative.
    def keep_or(value, old): return str(value).strip() if str(value or '').strip() else str(old or '')
    country=keep_or(payload.get('country'),cam.get('country')); region=keep_or(payload.get('region'),cam.get('region'))
    city=keep_or(payload.get('city'),cam.get('city')); area=keep_or(payload.get('area'),cam.get('area'))
    description=keep_or(payload.get('description'),cam.get('description'))
    def coord(name, old):
        raw=str(payload.get(name) or '').strip()
        if not raw: return old
        val=float(raw)
        if name=='latitude' and not -90 <= val <= 90: raise ValueError('Latitude must be between -90 and 90')
        if name=='longitude' and not -180 <= val <= 180: raise ValueError('Longitude must be between -180 and 180')
        return val
    try: latitude=coord('latitude',cam.get('latitude')); longitude=coord('longitude',cam.get('longitude'))
    except (ValueError,TypeError) as exc: return jsonify({'ok':False,'detail':str(exc)}),400
    device_id=str(cam.get('device_id') or '')
    c=_db_conn()
    # Blank coordinates mean "leave existing coordinates untouched". Location
    # is device-wide; camera descriptions remain per-channel to avoid overwriting
    # distinct labels/descriptions for other channels on the same NVR.
    coord_updates=[]; coord_params=[]
    if str(payload.get('latitude') or '').strip(): coord_updates.append('latitude=?'); coord_params.append(latitude)
    if str(payload.get('longitude') or '').strip(): coord_updates.append('longitude=?'); coord_params.append(longitude)
    where = ('device_id=?' if device_id else 'id=?')
    identity = device_id or cam_id
    sql = 'UPDATE cameras SET country=?,region=?,city=?,area=?,geo_source=\'operator-entered\',geo_confidence=\'operator-verified\',location_updated_at=?,updated_at=datetime(\'now\')'
    sql += (',' + ','.join(coord_updates)) if coord_updates else ''
    sql += ' WHERE deleted=0 AND ' + where
    c.execute(sql, (country,region,city,area,utc_iso(),*coord_params,identity))
    c.execute('UPDATE cameras SET description=? WHERE id=?', (description,cam_id))
    c.commit()
    return jsonify({'ok':True,'updated':True,'location':{'country':country,'region':region,'city':city,'area':area,'latitude':latitude,'longitude':longitude,'description':description},'updated_device_id':device_id or cam_id})


@app.route('/api/device/<device_id>')
def api_device_detail(device_id: str) -> Response:
    rows = _db_conn().execute(
        'SELECT * FROM cameras WHERE deleted=0 AND device_id=? '
        'ORDER BY channel_index,id', (device_id,)).fetchall()
    if not rows:
        return jsonify({'ok': False, 'detail': 'Device not found'}), 404
    cameras = [_safe_device_camera(_db_row(row)) for row in rows]
    first = dict(rows[0])
    return jsonify({'ok': True, 'device_id': device_id,
                    'host': first.get('ip', ''), 'port': first.get('port', 0),
                    'vendor': first.get('camera_type', ''),
                    'country': first.get('country', ''),
                    'region': first.get('region', ''), 'city': first.get('city', ''),
                    'channels': cameras})


@app.route('/api/device/<device_id>/config', methods=['POST'])
def api_device_config(device_id: str) -> Response:
    denied = _admin_only()
    if denied:
        return denied
    row = _db_conn().execute(
        'SELECT * FROM cameras WHERE deleted=0 AND device_id=? '
        'ORDER BY channel_index LIMIT 1', (device_id,)).fetchone()
    if not row:
        return jsonify({'ok': False, 'detail': 'Device not found'}), 404
    try:
        content, vendor, filename = device_config_bytes(
            row['ip'], row['port'], row['username'], row['password'],
            row['camera_type'])
        ext = filename.rsplit('.', 1)[-1]
        return Response(content, mimetype='application/octet-stream',
                        headers={'Content-Disposition':
                                 f'attachment; filename={vendor}-{device_id}.{ext}'})
    except Exception as exc:
        log.info('Device config download failed for %s: %s',
                 device_id, type(exc).__name__)
        return jsonify({'ok': False, 'detail': str(exc)[:200]}), 400


@app.route('/api/import/preview', methods=['POST'])
def import_preview() -> Response:
    upload = request.files.get('file')
    if not upload:
        return jsonify({'ok': False, 'detail': 'No file uploaded'}), 400
    name = os.path.basename(upload.filename or 'import.txt')
    ext = os.path.splitext(name)[1].lower()
    if ext not in {'.m3u', '.m3u8', '.txt', '.csv', '.xml', '.json'}:
        return jsonify({'ok': False, 'detail': 'Unsupported import format'}), 400
    tmp_path = ''
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            upload.save(tmp)
            tmp_path = tmp.name
        if ext in ('.m3u', '.m3u8'):
            cameras = _parse_m3u_file(tmp_path)
        elif ext == '.csv':
            cameras = _parse_csv_file(tmp_path)
        elif ext == '.xml':
            cameras = _parse_xml_file(tmp_path)
        elif ext == '.json':
            cameras = _parse_json_file(tmp_path)
        else:
            cameras = _parse_rtsp_file(tmp_path)
        sample = []
        for cam in cameras[:20]:
            item = dict(cam)
            item['password'] = ''
            item['rtsp_urls'] = [_mask_rtsp(u) for u in item.get('rtsp_urls', [])]
            sample.append(item)
        return jsonify({'ok': True, 'filename': name, 'count': len(cameras),
                        'sample': sample, 'saved': False})
    except Exception as exc:
        return jsonify({'ok': False,
                        'detail': f'Preview failed: {type(exc).__name}'}), 400
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


@app.route('/api/refresh/all', methods=['POST'])
def api_refresh_all() -> Response:
    def _run():
        _full_val_active.set()
        _scan_and_upsert()
        batch=db_get_resume_batch(_validation_worker_config()[1])
        if batch: db_set_status_bulk([c['id'] for c in batch], 'checking')
        run_validation_cycle(batch, max_workers=_validation_worker_config()[0], scope='all')
        _full_val_active.clear()
    with _SCAN_STATE_LOCK:
        if _SCAN_STATE.get('running'):
            return jsonify({'ok': False, 'running': True, 'message': 'A camera scan is already running; the new refresh was not started.'}), 409
    if not _full_val_active.is_set():
        t = threading.Thread(target=_run, daemon=True, name='refresh-all')
        t.start()
        log.info('[Refresh] Full refresh triggered by user')
    return jsonify({'ok': True, 'message': 'Full refresh started'})


@app.route('/api/recheck', methods=['POST'])
def api_recheck() -> Response:
    """Force-read every supported playlist/text file without a 364K burst."""
    def _run():
        try:
            _scan_and_upsert(force=True)
            cameras = []
            _, batch_limit = _validation_worker_config()
            batch = db_get_resume_batch(batch_limit)
            if batch:
                ids = [cam['id'] for cam in batch]
                db_set_status_bulk(ids, 'checking')
                start_background_validation(batch, delay=1.0,
                                           max_workers=_validation_worker_config()[0], scope='recheck')
            log.info('[Recheck] Forced file scan found %d cameras; validation batch=%d',
                     len(batch))
        except Exception:
            log.exception('[Recheck] Forced file scan failed')
    with _SCAN_STATE_LOCK:
        if _SCAN_STATE.get('running'):
            return jsonify({'ok': False, 'running': True, 'message': 'A camera scan is already running; the recheck was not started.'}), 409
    threading.Thread(target=_run, daemon=True, name='recheck-files').start()
    return jsonify({'ok': True, 'message': 'All supported camera files are being rechecked'})


@app.route('/api/refresh/group/<path:group_name>', methods=['POST'])
def api_refresh_group(group_name: str) -> Response:
    cameras = db_get_cameras_by_group(group_name, limit=99999, offset=0)
    if not cameras:
        return jsonify({'ok': False, 'detail': 'Group not found or empty'})
    ids = [c['id'] for c in cameras]
    db_set_status_bulk(ids, 'checking')

    def _run():
        run_validation_cycle(cameras, max_workers=_validation_worker_config()[0], scope=group_name)
    t = threading.Thread(target=_run, daemon=True, name=f'refresh-grp-{group_name[:20]}')
    t.start()
    log.info('[Refresh] Group refresh: %s (%d cameras)', group_name, len(cameras))
    return jsonify({'ok': True, 'count': len(cameras)})


@app.route('/api/refresh/source', methods=['POST'])
def api_refresh_source() -> Response:
    data        = request.get_json(silent=True) or {}
    source_file = data.get('source_file', '')
    if not source_file:
        return jsonify({'ok': False, 'detail': 'source_file required'})
    cameras = db_get_cameras_by_source(source_file)
    if not cameras:
        return jsonify({'ok': False, 'detail': 'No cameras for that source'})
    ids = [c['id'] for c in cameras]
    db_set_status_bulk(ids, 'checking')

    def _run():
        run_validation_cycle(cameras, max_workers=_validation_worker_config()[0],
                              scope=os.path.basename(source_file))
    t = threading.Thread(target=_run, daemon=True,
                         name=f'refresh-src-{os.path.basename(source_file)[:20]}')
    t.start()
    return jsonify({'ok': True, 'count': len(cameras)})


@app.route('/probe_batch', methods=['POST'])
def probe_batch() -> Response:
    items=request.get_json(silent=True) or []
    if not isinstance(items,list): return jsonify({'ok':False,'detail':'Expected JSON array'}),400
    results=[]; lock=threading.Lock()
    def check(item):
        cid=str(item.get('id') or ''); ok=False
        try:
            if _camera_access_denied(cid):
                with lock: results.append({'id':cid,'ok':False,'detail':'Access denied'})
                return
            cam=db_get_camera(cid)
            for url in ((cam or {}).get('rtsp_urls') or [])[:3]:
                host,port=_rtsp_host_port(url)
                if host and _tcp_probe(host,port,2.0)[0]: ok=True; break
        except Exception: pass
        with lock: results.append({'id':cid,'ok':ok})
    threads=[threading.Thread(target=check,args=(i,),daemon=True) for i in items[:20]]
    for t in threads: t.start()
    for t in threads: t.join(timeout=3)
    return jsonify(results)


@app.route('/add_camera', methods=['POST'])
def add_camera() -> Response:
    data     = request.get_json(silent=True) or {}
    rtsp_url = (data.get('rtsp_url') or '').strip()
    label    = (data.get('label')    or '').strip()
    group    = (data.get('group')    or 'My Cameras').strip()

    if not rtsp_url.lower().startswith(('rtsp://','rtsps://')):
        return jsonify({'ok': False, 'detail': 'URL must start with rtsp://'})

    live_file = os.path.join(_SEARCH_DIR, 'Live.txt')
    try:
        existing  = (Path(live_file).read_text(encoding='utf-8', errors='ignore')
                     if os.path.exists(live_file) else '')
        last_hdr  = ''
        for ln in existing.splitlines():
            ln = ln.strip()
            if ln and not ln.lower().startswith(('rtsp://','rtsps://')):
                last_hdr = ln.rstrip(':')
        with open(live_file, 'a', encoding='utf-8') as f:
            if last_hdr.lower() != group.lower():
                f.write(f'\n{group}:\n')
            f.write(f'{rtsp_url}{("  # " + label) if label else ""}\n')
        # Live.txt is runtime-managed; do not mutate the global skip policy.
        db_bulk_upsert(_parse_rtsp_file(live_file))
        return jsonify({'ok': True, 'rtsp': rtsp_url})
    except Exception as e:
        log.error('add_camera failed: %s', _mask_rtsp(str(e)))
        return jsonify({'ok': False, 'detail': _mask_rtsp(str(e))})


_M3U_IMPORT_LOCK = threading.RLock()
_M3U_IMPORT_JOBS: dict[str, dict] = {}
_M3U_IMPORT_JOBS_MAX = 100


def _m3u_import_job(job_id: str, temp_path: str, dest: str,
                    safe_name: str, ext: str) -> None:
    state = _M3U_IMPORT_JOBS[job_id]
    try:
        state.update(status='parsing', message='Parsing uploaded file')
        if ext in ('.m3u', '.m3u8'):
            cams = _parse_m3u_file(temp_path)
        elif ext == '.csv':
            cams = _parse_csv_file(temp_path)
        elif ext == '.xml':
            cams = _parse_xml_file(temp_path)
        else:
            # Stream TXT lines for RTSP playlists; only use whole-file parsing
            # for structured camera inventory blocks.
            with open(temp_path, 'r', encoding='utf-8-sig', errors='ignore') as fh:
                prefix = fh.read(256 * 1024)
            if re.search(r'camera\s+type\s*:|ip\s+address\s*:', prefix, re.I):
                content = Path(temp_path).read_text(encoding='utf-8-sig', errors='ignore')
                cams = []
                for block in re.split(r'={10,}', content):
                    cam = _parse_valid_camera_block(block, temp_path)
                    if cam:
                        cams.append(cam)
                if not cams and re.search(r'(?i)rtsps?://', prefix):
                    cams = _parse_rtsp_file(temp_path)
            else:
                cams = _parse_rtsp_file(temp_path)

        if not cams:
            state.update(status='failed', message='No supported camera/RTSP records found',
                         error_category='no_supported_records', parsed=0, upserted=0)
            log.warning('[M3U upload] job=%s rejected filename=%s reason=no_supported_records',
                        job_id, safe_name)
            return

        state.update(status='importing', parsed=len(cams), message=f'Importing {len(cams)} records')
        for cam in cams:
            cam['source_file'] = dest

        # Chunk DB writes so UI and validation requests can continue to make progress.
        chunk_size = max(25, min(250, int(os.getenv('CAMERA_IMPORT_BATCH_SIZE', '100'))))
        total = 0
        for pos in range(0, len(cams), chunk_size):
            chunk = cams[pos:pos + chunk_size]
            total += int(db_bulk_upsert(chunk) or 0)
            state.update(upserted=total, progress=min(99, int(100 * min(pos + len(chunk), len(cams)) / len(cams))))
            if pos + chunk_size < len(cams):
                _shutdown_requested.wait(0.03)

        # Never report success if shutdown cancelled a background SQLite write
        # or any parsed rows were not acknowledged by the database writer.
        if total != len(cams):
            raise RuntimeError(
                f'partial database import: parsed={len(cams)} acknowledged_upserts={total}; '
                'source file was not published as a successful import')
        # Publish source file only after every database chunk is acknowledged.
        os.replace(temp_path, dest)
        state.update(status='completed', progress=100, upserted=total,
                     message=f'Imported {len(cams)} camera records',
                     completed_at=utc_iso())
        log.info('[M3U upload] success job=%s filename=%s parsed=%d upserted=%d',
                 job_id, safe_name, len(cams), total)
    except OSError as exc:
        category = 'storage_quota' if exc.errno in (getattr(errno, 'ENOSPC', 28),
                                                     getattr(errno, 'EDQUOT', 122)) else 'filesystem_error'
        message = ('Storage quota is full; free project storage and retry.'
                   if category == 'storage_quota'
                   else f'Filesystem failure ({type(exc).__name__})')
        state.update(status='failed', error_category=category, message=message,
                     completed_at=utc_iso())
        log.exception('[M3U upload] background filesystem failure job=%s filename=%s',
                      job_id, safe_name)
    except Exception as exc:
        state.update(status='failed', error_category='import_failed',
                     message=f'Import failed ({type(exc).__name__}); see server log',
                     completed_at=utc_iso())
        log.exception('[M3U upload] background import failure job=%s filename=%s',
                      job_id, safe_name)
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        with _M3U_IMPORT_LOCK:
            # Keep recent job status for polling, but bound memory use.
            if len(_M3U_IMPORT_JOBS) > _M3U_IMPORT_JOBS_MAX:
                finished = [k for k, v in _M3U_IMPORT_JOBS.items()
                            if v.get('status') in {'completed', 'failed'} and k != job_id]
                for old_id in finished[:max(0, len(_M3U_IMPORT_JOBS) - _M3U_IMPORT_JOBS_MAX)]:
                    _M3U_IMPORT_JOBS.pop(old_id, None)


@app.route('/upload_m3u', methods=['POST'])
def upload_m3u() -> Response:
    """Stage an upload to disk and return quickly; import/upsert runs in a worker."""
    upload_name = ''
    temp_path = ''
    try:
        f = request.files.get('file')
        if not f or not (f.filename or '').strip():
            return jsonify({'ok': False, 'detail': 'No file selected. Choose an M3U file and try again.'}), 400

        upload_name = os.path.basename(f.filename or 'playlist.m3u')
        ext = os.path.splitext(upload_name)[1].lower()
        if ext not in {'.m3u', '.m3u8', '.txt', '.csv', '.xml'}:
            return jsonify({'ok': False, 'detail': 'Unsupported file type. Upload .m3u, .m3u8, .txt, .csv, or .xml.'}), 400

        safe_name = re.sub(r'[^\w.\-]', '_', upload_name) or ('playlist' + ext)
        os.makedirs(_SEARCH_DIR, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix='.upload-', suffix=ext, dir=_SEARCH_DIR)
        os.close(fd)
        f.save(temp_path)
        if os.path.getsize(temp_path) <= 0:
            return jsonify({'ok': False, 'detail': 'Uploaded file is empty.'}), 400

        dest = os.path.join(_SEARCH_DIR, safe_name)
        job_id = uuid.uuid4().hex[:16]
        with _M3U_IMPORT_LOCK:
            _M3U_IMPORT_JOBS[job_id] = {
                'job_id': job_id, 'filename': safe_name, 'status': 'queued',
                'progress': 0, 'parsed': 0, 'upserted': 0,
                'message': 'Upload received; import queued', 'created_at': utc_iso()
            }
        thread = threading.Thread(target=_m3u_import_job,
                                  args=(job_id, temp_path, dest, safe_name, ext),
                                  daemon=True, name=f'm3u-import-{job_id}')
        thread.start()
        temp_path = ''
        log.info('[M3U upload] accepted job=%s filename=%s bytes=%d',
                 job_id, safe_name, int(request.content_length or 0))
        return jsonify({'ok': True, 'accepted': True, 'job_id': job_id,
                        'filename': safe_name, 'message': 'Upload received; import is running.'}), 202

    except Exception as exc:
        # Werkzeug raises ClientDisconnected while reading multipart request bodies
        # when the browser/proxy closes the connection. Do not misreport it as an
        # internal parser/DB failure or emit a full traceback for this normal case.
        try:
            from werkzeug.exceptions import ClientDisconnected, RequestEntityTooLarge
        except Exception:
            ClientDisconnected = type('_NoClientDisconnected', (Exception,), {})
            RequestEntityTooLarge = type('_NoRequestEntityTooLarge', (Exception,), {})
        if isinstance(exc, ClientDisconnected):
            log.warning('[M3U upload] client disconnected while receiving filename=%s; no import job created',
                        upload_name or '(not received)')
            return jsonify({'ok': False, 'category': 'client_disconnected',
                            'detail': 'The browser or hosting proxy disconnected before the upload completed. Retry the upload; no import job was started.'}), 400
        if isinstance(exc, RequestEntityTooLarge):
            log.warning('[M3U upload] rejected oversized upload filename=%s limit=%d',
                        upload_name, int(app.config.get('MAX_CONTENT_LENGTH') or 0))
            return jsonify({'ok': False, 'category': 'upload_too_large',
                            'detail': 'Upload exceeds the configured file-size limit. Increase CAMERA_MAX_UPLOAD_BYTES if appropriate.'}), 413
        if isinstance(exc, OSError):
            category = 'storage_quota' if exc.errno in (getattr(errno, 'ENOSPC', 28),
                                                         getattr(errno, 'EDQUOT', 122)) else 'filesystem_error'
            status = 507 if category == 'storage_quota' else 500
            detail = ('Project storage quota is full. Free storage and retry.'
                      if category == 'storage_quota'
                      else f'Could not save the upload ({type(exc).__name__}). Check data-directory permissions.')
            log.error('[M3U upload] file failure filename=%s type=%s errno=%s',
                      upload_name, type(exc).__name__, exc.errno)
            return jsonify({'ok': False, 'category': category, 'detail': detail}), status
        log.exception('[M3U upload] failed filename=%s error_type=%s',
                      upload_name, type(exc).__name__)
        return jsonify({'ok': False, 'category': 'upload_failed',
                        'detail': f'Upload failed ({type(exc).__name__}); check server logs.'}), 500
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass


@app.route('/api/import/status/<job_id>', methods=['GET'])
def m3u_import_status(job_id: str) -> Response:
    with _M3U_IMPORT_LOCK:
        state = _M3U_IMPORT_JOBS.get(job_id)
        if not state:
            return jsonify({'ok': False, 'detail': 'Import job not found or expired'}), 404
        return jsonify({'ok': True, **dict(state)})


@app.route('/snapshot/<cam_id>')
def snapshot(cam_id: str) -> Response:
    cam = db_get_camera(cam_id)
    rtsp_url, detail = _resolve_camera_rtsp(cam_id)
    if not rtsp_url:
        return Response(detail, status=404)
    try:
        result = subprocess.run(_ffmpeg_snap_cmd(rtsp_url),
                                 capture_output=True, timeout=18)
        if result.returncode == 0 and result.stdout:
            ts    = datetime.now().strftime('%Y%m%d_%H%M%S')
            fname = f'snap_{cam_id}_{ts}.jpg'
            fpath = os.path.join(SNAPSHOT_DIR, fname)
            Path(fpath).write_bytes(result.stdout)
            db_set_snapshot(cam_id, fname)
            label = (cam or {}).get('label', cam_id)
            safe  = re.sub(r'[^\w.\-]', '_', label)[:40]
            if TELEGRAM_SNAPSHOT_ALERTS:
                threading.Thread(target=_send_telegram,
                                 args=(f'📷 *Snapshot*\n`{_mask_rtsp(rtsp_url)[:60]}`',),
                                 kwargs={'photo_path': fpath},daemon=True).start()
            return Response(
                result.stdout, mimetype='image/jpeg',
                headers={'Content-Disposition': f'attachment; filename="{safe}_{ts}.jpg"',
                         'Cache-Control': 'no-store'})
        return Response(
            f'Capture failed: {result.stderr.decode(errors="ignore")[-200:]}',
            status=500)
    except subprocess.TimeoutExpired:
        return Response('Snapshot timed out', status=504)
    except Exception as e:
        return Response(str(e), status=500)


@app.route('/snapshots/<path:filename>')
def serve_snapshot(filename: str) -> Response:
    return send_from_directory(SNAPSHOT_DIR, filename)


@app.route('/favorite/<cam_id>', methods=['POST'])
def toggle_favorite(cam_id: str) -> Response:
    cam = db_get_camera(cam_id)
    if not cam:
        return jsonify({'ok': False, 'detail': 'Camera not found'})
    new_state = not bool(cam.get('favorite'))
    db_set_favorite(cam_id, new_state)
    return jsonify({'ok': True, 'favorite': new_state})


@app.route('/delete/<cam_id>', methods=['POST'])
def delete_camera(cam_id: str) -> Response:
    db_soft_delete(cam_id)
    return jsonify({'ok': True})


def _bulk_ids() -> list[str]:
    data = request.get_json(silent=True) or {}
    ids = data.get('ids') or []
    return list(dict.fromkeys(str(x) for x in ids if str(x).strip()))[:10000]


@app.route('/api/category/transfer', methods=['POST'])
def api_category_transfer() -> Response:
    payload = request.get_json(silent=True) or {}
    ids = payload.get('ids') or []
    target = _normalize_group_name(payload.get('target_group') or '')
    if not isinstance(ids, list) or not target:
        return jsonify({'ok': False, 'detail': 'Camera IDs and target category are required'}), 400
    if len(ids) > 5000:
        return jsonify({'ok': False, 'detail': 'Maximum 5000 cameras per transfer'}), 400
    try:
        count = db_transfer_cameras(ids, target)
        return jsonify({'ok': True, 'count': count, 'target_group': target, 'message': f'Transferred {count} cameras to {target}'})
    except Exception as exc:
        log.exception('Category transfer failed')
        return jsonify({'ok': False, 'detail': str(exc)[:240]}), 500


@app.route('/api/category/progress')
def api_category_progress() -> Response:
    rows = _db_conn().execute(
        "SELECT COALESCE(NULLIF(group_name,''),NULLIF(country,''),'Unknown') AS grp, "
        "COUNT(*) AS total, SUM(CASE WHEN status='checking' THEN 1 ELSE 0 END) AS checking, "
        "SUM(CASE WHEN status='online' THEN 1 ELSE 0 END) AS online, "
        "SUM(CASE WHEN status='offline' THEN 1 ELSE 0 END) AS offline "
        "FROM cameras WHERE deleted=0 GROUP BY grp").fetchall()
    vp = val_get_progress(); scope = str(vp.get('scope') or '') if vp.get('running') else ''
    out = {}
    for r in rows:
        g = str(r['grp'] or 'Unknown')
        out[g] = {'total': int(r['total'] or 0), 'checking': int(r['checking'] or 0),
                  'online': int(r['online'] or 0), 'offline': int(r['offline'] or 0),
                  'active': scope in ('category:'+g, 'group:'+g)}
    return jsonify({'ok': True, 'categories': out, 'active_scope': scope, 'running': bool(vp.get('running'))})

@app.route('/api/category/recheck', methods=['POST'])
def api_category_recheck() -> Response:
    payload=request.get_json(silent=True) or {}; group=_normalize_group_name(payload.get('group') or '')
    if not group:return jsonify({'ok':False,'detail':'Category is required'}),400
    if val_get_progress().get('running'):
        return jsonify({'ok':False,'detail':'Another validation is already running; wait until it finishes'}),409
    rows=_db_conn().execute("SELECT * FROM cameras WHERE deleted=0 AND COALESCE(NULLIF(group_name,''),NULLIF(country,''),'Unknown')=?",(group,)).fetchall()
    cams=[_db_row(r) for r in rows]
    if cams:
        db_set_status_bulk([c['id'] for c in cams],'checking')
        start_background_validation(cams, delay=0.3, max_workers=_validation_worker_config()[0], scope='category:'+group)
    return jsonify({'ok':True,'count':len(cams),'message':f'Scanning/rechecking {len(cams)} cameras in {group}'})

@app.route('/api/category/offline-recheck', methods=['POST'])
def api_category_offline_recheck() -> Response:
    payload = request.get_json(silent=True) or {}
    group = _normalize_group_name(payload.get('group') or '')
    if not group:
        return jsonify({'ok': False, 'detail': 'Category is required'}), 400
    rows = _db_conn().execute(
        "SELECT id FROM cameras WHERE deleted=0 AND status IN ('offline','unknown') "
        "AND COALESCE(NULLIF(group_name,''),NULLIF(country,''),'Unknown')=?",
        (group,)).fetchall()
    ids = [str(r[0]) for r in rows if r[0]]
    if val_get_progress().get('running'):
        return jsonify({'ok': False, 'detail': 'Another validation is already running; wait until it finishes'}), 409
    cams = [db_get_camera(i) for i in ids]
    cams = [c for c in cams if c]
    if cams:
        db_set_status_bulk([c['id'] for c in cams], 'checking')
        start_background_validation(cams, delay=0.3, max_workers=_validation_worker_config()[0], scope='category:'+group+' [offline/unknown]')
    return jsonify({'ok': True, 'count': len(cams),
                    'message': f'Rechecking {len(cams)} offline/unknown cameras in {group}'})


@app.route('/api/category/favorite', methods=['POST'])
def api_category_favorite() -> Response:
    payload=request.get_json(silent=True) or {}; group=_normalize_group_name(payload.get('group') or '')
    if not group:return jsonify({'ok':False,'detail':'Category is required'}),400
    c=_db_conn(); cur=c.execute("UPDATE cameras SET favorite=1,updated_at=datetime('now') WHERE deleted=0 AND COALESCE(NULLIF(group_name,''),NULLIF(country,''),'Unknown')=?",(group,)); c.commit()
    return jsonify({'ok':True,'count':cur.rowcount,'message':f'Favorited {cur.rowcount} cameras in {group}'})

@app.route('/api/category/delete', methods=['POST'])
def api_category_delete() -> Response:
    payload = request.get_json(silent=True) or {}
    group = _normalize_group_name(payload.get('group') or '')
    if not group:
        return jsonify({'ok': False, 'detail': 'Category is required'}), 400
    try:
        count = db_delete_group(group)
        return jsonify({'ok': True, 'count': count, 'group': group, 'message': f'Deleted category {group}'})
    except Exception as exc:
        log.exception('Category delete failed')
        return jsonify({'ok': False, 'detail': str(exc)[:240]}), 500


@app.route('/api/bulk/favorite', methods=['POST'])
def bulk_favorite() -> Response:
    ids = _bulk_ids()
    if not ids:
        return jsonify({'ok': False, 'detail': 'No camera IDs supplied'}), 400
    c = _db_conn()
    ph = ','.join('?' * len(ids))
    c.execute(
        f"UPDATE cameras SET favorite=1,updated_at=datetime('now') "
        f"WHERE deleted=0 AND id IN ({ph})", ids)
    c.commit()
    return jsonify({'ok': True, 'message': f'Favorited {c.rowcount} cameras'})


@app.route('/api/bulk/delete', methods=['POST'])
def bulk_delete() -> Response:
    ids = _bulk_ids()
    if not ids:
        return jsonify({'ok': False, 'detail': 'No camera IDs supplied'}), 400
    now = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    c = _db_conn()
    ph = ','.join('?' * len(ids))
    c.execute(
        f"UPDATE cameras SET deleted=1,deleted_at=?,updated_at=? "
        f"WHERE deleted=0 AND id IN ({ph})", [now, now] + ids)
    c.execute(f'DELETE FROM validation_queue WHERE camera_id IN ({ph})', ids)
    c.commit()
    return jsonify({'ok': True, 'message': f'Removed {c.rowcount} cameras'})


@app.route('/api/bulk/recheck', methods=['POST'])
def bulk_recheck() -> Response:
    ids = _bulk_ids()
    if not ids:
        return jsonify({'ok': False, 'detail': 'No camera IDs supplied'}), 400
    cams = []
    for cam_id in ids:
        cam = db_get_camera(cam_id)
        if cam:
            cams.append(cam)
    db_set_status_bulk([cam['id'] for cam in cams], 'checking')
    if cams:
        start_background_validation(cams, delay=0.5, max_workers=_validation_worker_config()[0],
                                    scope='selected')
    return jsonify({'ok': True, 'message': f'Queued {len(cams)} cameras'})


@app.route('/api/bulk/export', methods=['POST'])
def bulk_export() -> Response:
    ids = _bulk_ids()
    if not ids:
        return jsonify({'ok': False, 'detail': 'No camera IDs supplied'}), 400
    ph = ','.join('?' * len(ids))
    rows = _db_conn().execute(
        f'SELECT * FROM cameras WHERE deleted=0 AND id IN ({ph})', ids).fetchall()
    cameras = []
    for row in rows:
        item = dict(row)
        item['password'] = ''
        try:
            item['rtsp_urls'] = [_mask_rtsp(u) for u in json.loads(item.get('rtsp_urls') or '[]')]
        except Exception:
            item['rtsp_urls'] = []
        cameras.append(item)
    return jsonify({'ok': True, 'cameras': cameras, 'count': len(cameras)})


@app.route('/send_rtsp/<cam_id>')
def send_rtsp_tg(cam_id: str) -> Response:
    cam      = db_get_camera(cam_id) or {'id': cam_id, 'label': 'Camera',
                                          'ip': '', 'city': '', 'country': '',
                                          'rtsp_urls': [], 'snapshot_path': ''}
    rtsp_url = (cam.get('rtsp_urls') or [''])[0]
    if not rtsp_url:
        return jsonify({'ok': False, 'detail': 'No RTSP URL'})
    ts  = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    msg = (f"📷 *Camera RTSP Link*\n🕐 `{ts}`\n"
           f"📡 *Camera:* `{cam.get('label', cam.get('ip','?'))}`\n"
           f"🌍 *Location:* {cam.get('city','')} {cam.get('country','')}\n"
           f"🔗 *RTSP:*\n`{_mask_rtsp(rtsp_url)}`")
    # Append to RTSP log
    try:
        with open(RTSP_SAVE, 'a', encoding='utf-8') as fp:
            fp.write(f'[{ts}] {cam.get("label","Camera")} | {rtsp_url}\n')
    except Exception:
        pass
    snap_path = ''
    if cam.get('snapshot_path'):
        snap_path = os.path.join(SNAPSHOT_DIR, cam['snapshot_path'])
    ok, detail = _send_telegram(msg, photo_path=snap_path)
    return jsonify({'ok': ok, 'detail': detail, 'rtsp': _mask_rtsp(rtsp_url)})


@app.route('/api/telegram/secure-status', methods=['GET'])
def telegram_secure_status() -> Response:
    denied = _admin_only()
    if denied:
        return denied
    encrypted_all = _telegram_decrypt_config(include_disabled=True)
    ciphertext = _telegram_db_ciphertext()
    encrypted = bool(ciphertext and encrypted_all)
    file_items = _legacy_tg_file_destinations(include_disabled=True) if not encrypted else []
    items = encrypted_all or file_items
    source = 'encrypted-db' if encrypted else ('plaintext-file' if file_items else 'none')
    metadata = [
        {
            'index': i,
            'name': str(d.get('name') or f'Telegram {i + 1}')[:100],
            'enabled': bool(d.get('enabled', True)),
        }
        for i, d in enumerate(items)
    ]
    return jsonify({
        'ok': True,
        'configured': bool(items),
        'count': len(items),
        'name': metadata[0]['name'] if metadata else '',
        'destinations': metadata,
        'encrypted': encrypted,
        'source': source,
        'plaintext_file_present': bool(file_items),
        'detail': (
            f'{len(items)} Telegram destination(s) are stored encrypted in SQLite.' if encrypted else
            f'{len(file_items)} destination(s) from the existing telegram_config.json are active. Import them to encrypt.' if file_items else
            'No Telegram configuration is saved.'
        )
    })


@app.route('/api/telegram/secure-config', methods=['POST','DELETE'])
def telegram_secure_config() -> Response:
    denied = _admin_only()
    if denied:
        return denied
    if request.method == 'DELETE':
        d = request.get_json(silent=True) or {}
        index = d.get('index')
        if index is None:
            ok, detail = _delete_telegram_config()
            return jsonify({'ok': ok, 'detail': detail}), (200 if ok else 500)
        try:
            index = int(index)
        except (TypeError, ValueError):
            return jsonify({'ok': False, 'detail': 'Invalid destination index.'}), 400
        items = _telegram_decrypt_config(include_disabled=True)
        if not items:
            items = _legacy_tg_file_destinations(include_disabled=True)
        if index < 0 or index >= len(items):
            return jsonify({'ok': False, 'detail': 'Destination not found.'}), 404
        removed = items.pop(index)
        if items:
            ok, detail = _save_telegram_config(items)
        else:
            ok, detail = _delete_telegram_config()
        if ok:
            return jsonify({'ok': True, 'detail': f"Removed Telegram destination '{str(removed.get('name') or 'Telegram')[:100]}'.", 'remaining': len(items)})
        return jsonify({'ok': False, 'detail': detail}), 500

    d = request.get_json(silent=True) or {}
    action = str(d.get('action') or 'replace').strip().lower()
    token = str(d.get('bot_token') or '').strip()
    chat_id = str(d.get('chat_id') or '').strip()
    name = str(d.get('name') or 'My Telegram').strip()[:100]
    enabled = bool(d.get('enabled', True))
    if not token or not chat_id:
        return jsonify({'ok': False, 'detail': 'Bot Token and Chat ID are required.'}), 400
    if len(token) > 512 or len(chat_id) > 256:
        return jsonify({'ok': False, 'detail': 'Telegram credential field is too long.'}), 400

    new_item = {'name': name, 'bot_token': token, 'chat_id': chat_id, 'enabled': enabled}
    if action == 'add':
        items = _telegram_decrypt_config(include_disabled=True)
        if not items:
            items = _legacy_tg_file_destinations(include_disabled=True)
        items.append(new_item)
        ok, detail = _save_telegram_config(items)
        return jsonify({'ok': ok, 'detail': detail, 'added': name, 'count': len(items)}), (200 if ok else 500)

    # Backward-compatible replace operation: replace the complete encrypted list
    # with the single supplied destination. The dashboard uses action=add for new
    # destinations so existing destinations are preserved.
    ok, detail = _save_telegram_config([new_item])
    return jsonify({'ok': ok, 'detail': detail, 'count': 1}), (200 if ok else 500)


@app.route('/api/telegram/import-upload', methods=['POST'])
def telegram_import_upload() -> Response:
    """Import a user-selected JSON file without persisting plaintext credentials."""
    denied = _admin_only()
    if denied:
        return denied
    upload = request.files.get('file')
    if upload is None or not str(upload.filename or '').strip():
        return jsonify({'ok': False, 'detail': 'Choose a telegram_config.json file first.'}), 400
    filename = os.path.basename(upload.filename or 'telegram_config.json')
    if not filename.lower().endswith('.json'):
        return jsonify({'ok': False, 'detail': 'Only .json Telegram configuration files are supported.'}), 400
    try:
        raw = upload.stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            return jsonify({'ok': False, 'detail': 'JSON file exceeds the 1 MB upload limit.'}), 413
        cfg = json.loads(raw.decode('utf-8-sig'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return jsonify({'ok': False, 'detail': 'The selected file is not valid UTF-8 JSON.'}), 400
    except Exception as exc:
        log.warning('[Telegram Import] Upload read failed: %s', type(exc).__name__)
        return jsonify({'ok': False, 'detail': 'Could not read the selected JSON file.'}), 400

    if not isinstance(cfg, dict):
        return jsonify({'ok': False, 'detail': 'Expected a JSON object with a destinations list.'}), 400
    raw_items = cfg.get('destinations')
    if not isinstance(raw_items, list):
        raw_items = [cfg] if cfg.get('bot_token') or cfg.get('chat_id') else []
    if cfg.get('enabled') is False:
        raw_items = []

    clean = []
    skipped = 0
    for item in raw_items:
        if not isinstance(item, dict):
            skipped += 1
            continue
        token = str(item.get('bot_token') or item.get('token') or '').strip()
        chat_id = str(item.get('chat_id') or item.get('chatId') or '').strip()
        if not token or not chat_id:
            skipped += 1
            continue
        if len(token) > 512 or len(chat_id) > 256:
            skipped += 1
            continue
        clean.append({
            'name': str(item.get('name') or f'Telegram {len(clean) + 1}').strip()[:100],
            'bot_token': token,
            'chat_id': chat_id,
            'enabled': bool(item.get('enabled', True)),
        })
    if not clean:
        return jsonify({'ok': False, 'detail': 'No valid destinations found. Each destination needs bot_token and chat_id, and the top-level enabled flag must not be false.'}), 400

    ok, detail = _save_telegram_config(clean)
    if not ok:
        return jsonify({'ok': False, 'detail': detail}), 500
    return jsonify({'ok': True, 'detail': detail, 'imported': len(clean),
                    'skipped': skipped, 'source_file': filename,
                    'plaintext_file_saved': False})


@app.route('/api/telegram/import-file', methods=['POST'])
def telegram_import_file() -> Response:
    """Import the existing plaintext Telegram config into encrypted SQLite."""
    denied = _admin_only()
    if denied:
        return denied
    items = _legacy_tg_file_destinations()
    if not items:
        return jsonify({'ok': False, 'detail': 'No valid telegram_config.json (or a.json) was found.'}), 404
    clean = []
    for d in items:
        clean.append({
            'name': str(d.get('name') or 'Telegram').strip()[:100],
            'bot_token': str(d.get('bot_token') or '').strip(),
            'chat_id': str(d.get('chat_id') or '').strip(),
            'enabled': bool(d.get('enabled', True)),
        })
    ok, detail = _save_telegram_config(clean)
    if ok:
        return jsonify({'ok': True, 'detail': detail, 'imported': len(clean), 'source_file': os.path.basename(TG_CONFIG)})
    return jsonify({'ok': False, 'detail': detail}), 500


@app.route('/api/telegram/test', methods=['POST'])
def telegram_test() -> Response:
    ok, detail = _send_telegram(
        f"✅ *Camera Dashboard Telegram test*\n"
        f"`{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`")
    return jsonify({'ok': ok, 'detail': detail})


@app.route('/api/telegram/status', methods=['GET'])
def telegram_status() -> Response:
    """Return actionable Telegram configuration and Bot API status."""
    dests = _load_tg_destinations()
    if not dests:
        return jsonify({'ok': False, 'configured': False, 'detail': 'Telegram is not configured. Open Telegram Settings and save the Bot Token + Chat ID.'})
    if _requests is None:
        return jsonify({'ok': False, 'configured': True, 'detail': 'requests library not installed'}), 500
    results = []
    overall = True
    for dest in dests:
        try:
            r = _requests.get(f"https://api.telegram.org/bot{dest['bot_token']}/getMe", timeout=10)
            body = r.json() if 'application/json' in r.headers.get('Content-Type','') else {}
            ok = bool(r.ok and body.get('ok'))
            overall = overall and ok
            results.append({'name': dest.get('name','Telegram'), 'ok': ok, 'http_status': r.status_code, 'bot': body.get('result',{}).get('username','') if ok else '', 'detail': body.get('description','') if not ok else 'Bot token valid'})
        except Exception as e:
            overall = False
            results.append({'name': dest.get('name','Telegram'), 'ok': False, 'detail': f'{type(e).__name__}: {e}'})
    return jsonify({'ok': overall, 'configured': True, 'destinations': results})


@app.route('/api/telegram/camera-list', methods=['POST'])
def telegram_camera_list() -> Response:
    try:
        export_path = _build_camera_export_file()
        try:
            stats = db_get_stats()
            size = os.path.getsize(export_path)
            ok, detail = _send_telegram_document(
                export_path, 'camera-dashboard-export.jsonl.gz',
                f"📦 Camera database export · {stats.get('total', 0)} active cameras")
            return jsonify({'ok': ok, 'detail': detail, 'bytes': size})
        finally:
            try: os.remove(export_path)
            except OSError: pass
    except Exception:
        log.exception('Telegram camera export failed')
        return jsonify({'ok': False, 'detail': 'Camera export failed'}), 500


@app.route('/api/cameras/<cam_id>/rtsp')
def api_camera_rtsp(cam_id: str) -> Response:
    """Return the stored candidate used by copy/VLC actions.

    Playback itself never receives a URL from the browser; it uses the camera
    id and resolves the credential-containing URL on the server.
    """
    urls = _stream_urls_for_camera(cam_id)
    if not urls:
        return jsonify({'ok': False, 'detail': 'No stored RTSP URL'}), 404
    return jsonify({'ok': True, 'rtsp': urls[0], 'count': len(urls)})


@app.route('/api/cameras/<cam_id>/diagnose')
def api_camera_diagnose(cam_id: str) -> Response:
    """Compare RTSP transports and bounded HTTP management ports."""
    urls = _stream_urls_for_camera(cam_id)
    if not urls:
        return jsonify({'ok': False, 'detail': 'No stored RTSP URL'}), 404
    url = urls[0]
    parts = urlsplit(url)
    result = {'ok': False, 'tcp': 'failed', 'udp': 'failed',
              'credentials': bool(parts.username or parts.password),
              'http': {'ok': False, 'detail': 'No device host'}}
    row = _db_conn().execute(
        'SELECT ip,port FROM cameras WHERE deleted=0 AND id=?', (cam_id,)
    ).fetchone()
    host = (row['ip'] if row else '') or parts.hostname or ''
    if host:
        try:
            result['http'] = _probe_device_http_ports(host)
            result['http']['ok'] = bool(result['http'].get('reachable'))
        except Exception as exc:
            result['http'] = {'ok': False, 'category': _device_error_category(exc),
                              'detail': str(exc)[:180]}
    for transport in ('tcp', 'udp'):
        cmd = ['ffprobe', '-v', 'error', '-rtsp_transport', transport,
               '-timeout', '5000000', '-select_streams', 'v:0',
               '-show_entries', 'stream=codec_name,width,height',
               '-of', 'default=noprint_wrappers=1', url]
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=8)
            if p.returncode == 0 and p.stdout.strip():
                result[transport] = 'works'
                result['ok'] = True
            else:
                err = p.stderr.decode(errors='ignore').strip()
                result[transport] = 'failed: ' + re.sub(
                    r'rtsp://[^\s]+', '[RTSP URL]', ' '.join(err.split()))[-180:]
        except subprocess.TimeoutExpired:
            result[transport] = 'timed out'
        except Exception:
            result[transport] = 'unavailable'
    result['ok'] = result['ok'] or bool(result['http'].get('ok'))
    if not result['ok']:
        result['detail'] = (
            'Check VLC credentials, then try the transport that succeeds. '
            'If VLC works but both probes fail, compare the exact RTSP path, '
            'HTTP management port, and camera network access from this runtime.')
    return jsonify(result)


@sock.route('/ws/stream/<cam_id>')
def ws_stream(ws, cam_id: str) -> None:
    if _auth_enabled():
        if not _session_valid():
            try:
                ws.send('ERROR: Session expired or logged out')
            except Exception:
                pass
            return
        if not _has_permission('stream.view'):
            try:
                ws.send('ERROR: Permission required: stream.view')
            except Exception:
                pass
            return
    # Resolve only from SQLite. Do not accept a credential-bearing URL from
    # browser query parameters for the live playback path.
    urls = _stream_urls_for_camera(cam_id)
    if not urls:
        try:
            ws.send('ERROR: No stored RTSP URL')
        except Exception:
            pass
        return
    if not _reserve_stream_slot(cam_id):
        try:
            ws.send(f'ERROR: Maximum live feed limit reached ({MAX_STREAMS})')
        except Exception:
            pass
        return
    sent_frames = 0
    diagnostics: list[str] = []
    attempt_count = 0
    try:
        attempt_cap_reached = False
        for recovery_round in range(STREAM_RECOVERY_ATTEMPTS + 1):
            round_sent = 0
            for rtsp_url in urls:
                for transport in ('tcp', 'udp'):
                    attempt_count += 1
                    if attempt_count > STREAM_MAX_ATTEMPTS:
                        diagnostics.append(f'stream attempt cap reached ({STREAM_MAX_ATTEMPTS})')
                        attempt_cap_reached = True
                        break
                    proc = None
                    stderr_lines: list[str] = []
                    try:
                        _stream_log('Starting stream camera=%s transport=%s url=%s',
                                    cam_id, transport, rtsp_url)
                        proc = subprocess.Popen(
                            _ffmpeg_stream_cmd(rtsp_url, transport),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
                        stderr_lines = _capture_ffmpeg_stderr(proc)
                        _register_stream_proc(cam_id,proc)
                        candidate_frames = 0
                        for frame in _stream_jpeg_frames(proc):
                            ws.send(frame)
                            candidate_frames += 1
                            sent_frames += 1
                        if candidate_frames:
                            db_promote_rtsp_url(cam_id, rtsp_url)
                            _record_recovery_event('rtsp_reconnect', cam_id, 'recovered', f'{transport};frames={candidate_frames}')
                            round_sent += candidate_frames
                            if proc.poll() is None:
                                return
                            if recovery_round < STREAM_RECOVERY_ATTEMPTS:
                                diagnostics.append(
                                    f'watchdog retry {recovery_round + 1} after '
                                    f'{transport} process exit')
                                break
                            return
                        diagnostics.append(
                            f'{transport} {_ffmpeg_diagnostic(proc, stderr_lines)}')
                    except Exception as exc:
                        diagnostics.append(
                            f'{transport} {type(exc).__name__}: {exc}')
                    finally:
                        _stop_ffmpeg(proc)
                        _unregister_stream_proc(cam_id,proc)
                if round_sent:
                    break
                if attempt_cap_reached:
                    break
            if attempt_cap_reached:
                break
            if round_sent and recovery_round < STREAM_RECOVERY_ATTEMPTS:
                time.sleep(0.2)
                continue
            break
        diagnostic = '; '.join(diagnostics)[-1000:] or 'No decoded video frame'
        _stream_log('Stream failed camera=%s frames=%d detail=%s',
                    cam_id, sent_frames, diagnostic)
        _record_recovery_event('rtsp_reconnect', cam_id, 'failed', diagnostic)
        try:
            ws.send(f'ERROR: {diagnostic}')
        except Exception:
            pass
    except Exception as exc:
        diagnostic = str(exc)
        _stream_log('Stream exception camera=%s frames=%d detail=%s',
                    cam_id, sent_frames, diagnostic)
        try:
            ws.send(f'ERROR: {diagnostic}')
        except Exception:
            pass
    finally:
        _release_stream_slot(cam_id)


@app.route('/stream/<cam_id>')
def stream_mjpeg(cam_id: str) -> Response:
    """HTTP MJPEG fallback (for browsers that don't support WebSocket)."""
    urls = _stream_urls_for_camera(cam_id)
    if not urls:
        return Response('No stored RTSP URL', status=404)
    if not _reserve_stream_slot(cam_id):
        return Response(
            f'Maximum live feed limit reached ({MAX_STREAMS})',
            status=429)

    def gen():
        sent_frames = 0
        attempt_count = 0
        attempt_cap_reached = False
        try:
            diagnostics: list[str] = []
            for rtsp_url in urls:
                if attempt_cap_reached:
                    break
                for transport in ('tcp', 'udp'):
                    attempt_count += 1
                    if attempt_count > STREAM_MAX_ATTEMPTS:
                        diagnostics.append(f'stream attempt cap reached ({STREAM_MAX_ATTEMPTS})')
                        attempt_cap_reached = True
                        break
                    proc = None
                    stderr_lines: list[str] = []
                    try:
                        _stream_log(
                            'Starting HTTP stream camera=%s transport=%s url=%s',
                            cam_id, transport, rtsp_url)
                        proc = subprocess.Popen(
                            _ffmpeg_stream_cmd(rtsp_url, transport),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
                        stderr_lines = _capture_ffmpeg_stderr(proc)
                        _register_stream_proc(cam_id,proc)
                        candidate_frames = 0
                        for frame in _stream_jpeg_frames(proc, first_frame_timeout=12.0,
                                                         idle_timeout=20.0):
                            candidate_frames += 1
                            sent_frames += 1
                            if candidate_frames == 1:
                                _stream_log('[HTTP stream] FIRST_FRAME camera=%s transport=%s frame_bytes=%s url=%s',
                                            cam_id, transport, len(frame), rtsp_url)
                            yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n'
                        if candidate_frames:
                            _stream_log('[HTTP stream] ENDED camera=%s transport=%s frames=%s url=%s',
                                        cam_id, transport, candidate_frames, rtsp_url)
                            db_promote_rtsp_url(cam_id, rtsp_url)
                            return
                        detail = _ffmpeg_diagnostic(proc, stderr_lines)
                        diagnostics.append(f'{transport} {detail}')
                        if '401 unauthorized' in detail.lower() or 'authorization failed' in detail.lower():
                            category = 'AUTH_REJECTED: check stored username/password'
                        elif 'connection refused' in detail.lower():
                            category = 'PORT_REFUSED: check RTSP service/port/firewall'
                        elif 'timed out' in detail.lower() or 'timeout' in detail.lower():
                            category = 'NETWORK_TIMEOUT: check reachability/routing/firewall'
                        else:
                            category = 'STREAM_OPEN_FAILED'
                        _stream_log('[HTTP stream] NO_FRAMES camera=%s transport=%s category=%s detail=%s url=%s',
                                    cam_id, transport, category, detail, rtsp_url)
                    except GeneratorExit:
                        raise
                    except Exception as exc:
                        detail = f'{type(exc).__name__}: {exc}'
                        diagnostics.append(f'{transport} {detail}')
                        _stream_log('[HTTP stream] ATTEMPT_FAILED camera=%s transport=%s detail=%s url=%s',
                                    cam_id, transport, detail, rtsp_url)
                    finally:
                        _stop_ffmpeg(proc)
                        _unregister_stream_proc(cam_id,proc)
            if not sent_frames:
                _stream_log('[HTTP stream] FINAL_FAILURE camera=%s attempts=%s urls=%s frames=0 detail=%s',
                            cam_id, attempt_count, len(urls), '; '.join(diagnostics)[-1000:])
        except GeneratorExit:
            pass
        finally:
            _release_stream_slot(cam_id)

    return Response(gen(), mimetype='multipart/x-mixed-replace; boundary=frame',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/stop/<cam_id>',methods=['GET','POST'])
def stop_stream(cam_id):
    with _streams_lock: subscribers=int(_stream_subscribers.get(cam_id,0)); procs=list(_streams.get(cam_id,set()))
    if subscribers>1: return jsonify({'ok':True,'stopped':False,'subscribers':subscribers-1,'detail':'Other viewers are still using this stream'})
    for proc in procs:_stop_ffmpeg(proc)
    return jsonify({'ok':True,'stopped':True,'subscribers':0})

@app.route('/api/stop_all',methods=['POST'])
def stop_all_streams():
    owner = str(session.get('user') or 'public') if _auth_enabled() else 'public'
    is_admin = (not _auth_enabled()) or _current_role() == 'admin'
    with _streams_lock:
        if is_admin:
            active=[p for bucket in _streams.values() for p in bucket]
        else:
            active=[p for p in _stream_proc_owners if _stream_proc_owners.get(p)==owner]
    # Do not touch subscriber counters here. Each stream generator owns exactly
    # one reserve/release lifecycle; mutating the counter from this endpoint can
    # underflow when two viewers share one camera and both generators later exit.
    for proc in active:
        _stop_ffmpeg(proc)
    return jsonify({'ok':True,'stopped':len(active),'scope':'all' if is_admin else 'own'})


@app.route('/api/shutdown', methods=['POST'])
def shutdown_application() -> Response:
    """Start a non-blocking, observable graceful application shutdown."""
    log.warning('[Shutdown] Stop App HTTP request received from %s', request.remote_addr or 'unknown')
    if _shutdown_complete.is_set():
        return jsonify({'ok': True, 'state': 'stopped', 'message': 'Shutdown already completed.'}), 200
    if _shutdown_requested.is_set():
        return jsonify({'ok': True, 'state': 'stopping', 'message': 'Shutdown is already in progress.'}), 202

    def _shutdown_worker():
        ok = False
        try:
            log.info('[Shutdown] Stop App worker started')
            ok = _graceful_shutdown('dashboard Stop app button')
            log.info('[Shutdown] Stop App worker finished; database flush=%s', ok)
            _tg_notify_shutdown_once()
        except Exception:
            log.exception('[Shutdown] Stop App worker failed')
        finally:
            # There is no reason to depend on the optional Werkzeug shutdown
            # callback in hosted/Replit mode. Once the persistence barrier is
            # complete, terminate the process explicitly and predictably.
            time.sleep(0.15)
            logging.shutdown()
            os._exit(0 if ok else 1)

    threading.Thread(target=_shutdown_worker, daemon=True, name='dashboard-stop-app').start()
    return jsonify({
        'ok': True,
        'state': 'stopping',
        'message': 'Shutdown started. Background work is being cancelled; SQLite commit/checkpoint will complete before the process exits.'
    }), 202


# ── REST API ──────────────────────────────────────────────────────────────────

@app.route('/api/stats')
def api_stats() -> Response:
    stats = db_get_stats()
    with _streams_lock:
        stats['active_streams'] = sum(_stream_subscribers.values())
    stats['max_streams'] = MAX_STREAMS
    return jsonify(stats)


@app.route('/api/groups')
def api_groups() -> Response:
    return jsonify(db_get_group_stats())


@app.route('/api/sources')
def api_sources() -> Response:
    return jsonify(db_get_source_stats())


@app.route('/api/cameras')
def api_cameras() -> Response:
    limit=max(1,min(500,int(request.args.get('limit',100)))); offset=max(0,int(request.args.get('offset',0)))
    status=str(request.args.get('status') or '').strip().lower(); group=str(request.args.get('group') or '').strip()
    where=['deleted=0']; params=[]
    if status and status!='all': where.append('status=?'); params.append(status)
    if group and group!='all': where.append("COALESCE(NULLIF(group_name,''),NULLIF(country,''),'Unknown')=?"); params.append(group)
    rows=_db_conn().execute(f"SELECT * FROM cameras WHERE {' AND '.join(where)} ORDER BY {_STATUS_SORT},id LIMIT ? OFFSET ?",params+[limit,offset]).fetchall()
    items=_filter_camera_access([_db_row(r) for r in rows]); total=(len(items) if _current_role()!='admin' else int(_db_conn().execute(f"SELECT COUNT(*) FROM cameras WHERE {' AND '.join(where)}",params).fetchone()[0]))
    return jsonify({'ok':True,'items':items,'cameras':items,'total':total,'limit':limit,'offset':offset})


@app.route('/api/category/camera-ids')
def api_category_camera_ids() -> Response:
    group = _normalize_group_name(request.args.get('group') or '')
    if not group:
        return jsonify({'ok': False, 'detail': 'Category is required'}), 400
    # __favorites__ is a virtual category, not a real group_name value.
    # Handle it explicitly so category-level Select All works for Favorites.
    if group == '__favorites__':
        rows = _db_conn().execute(
            "SELECT id FROM cameras WHERE deleted=0 AND favorite=1 ORDER BY id"
        ).fetchall()
    else:
        rows = _db_conn().execute(
            "SELECT id FROM cameras WHERE deleted=0 AND "
            "COALESCE(NULLIF(group_name,''),NULLIF(country,''),'Unknown')=? "
            "ORDER BY id", (group,)
        ).fetchall()
    return jsonify({'ok': True, 'group': group, 'ids': [str(r[0]) for r in rows], 'count': len(rows)})


@app.route('/api/cameras/<cam_id>')
def api_camera(cam_id: str) -> Response:
    cam = db_get_camera(cam_id)
    return jsonify(cam) if cam else Response('Not found', status=404)


@app.route('/api/validate_progress')
def api_validate_progress() -> Response:
    return jsonify(val_get_progress())


@app.route('/api/rtsp')
def api_rtsp() -> Response:
    if RTSP_API_TOKEN:
        supplied=request.headers.get('X-Camera-RTSP-Token','') or request.args.get('token','')
        if not hmac.compare_digest(str(supplied),RTSP_API_TOKEN): return jsonify({'ok':False,'detail':'RTSP export token required'}),403
    if os.path.exists(RTSP_SAVE): return Response(open(RTSP_SAVE,encoding='utf-8').read(),mimetype='text/plain')
    return Response('# No links saved yet.\n',mimetype='text/plain')



# ADVANCED MANAGEMENT API (optional; main dashboard UI remains unchanged)
@app.route('/api/advanced/health')
def api_adv_health():
    c=_db_conn(); total=c.execute('SELECT COUNT(*) FROM cameras WHERE deleted=0').fetchone()[0]; online=c.execute("SELECT COUNT(*) FROM cameras WHERE deleted=0 AND status='online'").fetchone()[0]; offline=c.execute("SELECT COUNT(*) FROM cameras WHERE deleted=0 AND status='offline'").fetchone()[0]; pending=c.execute("SELECT COUNT(*) FROM cameras WHERE deleted=0 AND status IN ('unknown','checking','rtsp_pending','no_channels')").fetchone()[0]; avg=c.execute('SELECT COALESCE(AVG(confidence),0) FROM device_fingerprints').fetchone()[0]; return jsonify({'ok':True,'total':total,'online':online,'offline':offline,'pending':pending,'avg_confidence':round(float(avg or 0),1),'workers':_worker_health_snapshot()})

@app.route('/api/advanced/device/<cam_id>/evidence')
def api_adv_evidence(cam_id):
    c=_db_conn(); fp=c.execute('SELECT * FROM device_fingerprints WHERE camera_id=? ORDER BY last_seen DESC LIMIT 1',(cam_id,)).fetchone(); ev=c.execute('SELECT evidence_type,result,value,confidence,created_at FROM device_evidence WHERE camera_id=? ORDER BY id DESC LIMIT 300',(cam_id,)).fetchall(); events=c.execute('SELECT event_type,old_value,new_value,detail,created_at FROM device_events WHERE camera_id=? ORDER BY id DESC LIMIT 300',(cam_id,)).fetchall(); return jsonify({'ok':True,'fingerprint':dict(fp) if fp else None,'evidence':[dict(x) for x in ev],'events':[dict(x) for x in events]})

@app.route('/api/advanced/device/<cam_id>/timeline')
def api_adv_timeline(cam_id):
    c=_db_conn(); a=c.execute('SELECT event_type,old_value,new_value,detail,created_at FROM device_events WHERE camera_id=? ORDER BY id DESC LIMIT 500',(cam_id,)).fetchall(); h=c.execute('SELECT status,codec,resolution,fps,latency_ms,failure_reason,candidate_url,transport,candidate_score,checked_at FROM camera_health_history WHERE camera_id=? ORDER BY id DESC LIMIT 300',(cam_id,)).fetchall(); s=c.execute('SELECT codec,resolution,fps,bitrate,audio,video,latency_ms,first_frame,transport,failure_reason,quality_score,observed_at FROM stream_observations WHERE camera_id=? ORDER BY id DESC LIMIT 300',(cam_id,)).fetchall(); return jsonify({'ok':True,'events':[dict(x) for x in a],'health':[dict(x) for x in h],'streams':[dict(x) for x in s]})

@app.route('/api/advanced/device/<cam_id>/observe',methods=['POST'])
def api_adv_observe(cam_id):
    d=request.get_json(silent=True) or {}; cam=db_get_camera(cam_id)
    if not cam:return jsonify({'ok':False,'detail':'Camera not found'}),404
    score=_stream_quality_score(d); c=_db_conn(); c.execute('INSERT INTO stream_observations(camera_id,rtsp_url,codec,resolution,fps,bitrate,audio,video,latency_ms,first_frame,transport,failure_reason,quality_score) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(cam_id,_mask_rtsp(_safe_str(d.get('rtsp_url'))),_safe_str(d.get('codec')),_safe_str(d.get('resolution')),float(d.get('fps') or 0),int(d.get('bitrate') or 0),int(bool(d.get('audio'))),int(bool(d.get('video'))),int(d.get('latency_ms') or 0),int(bool(d.get('first_frame'))),_safe_str(d.get('transport')),_safe_str(d.get('failure_reason')),score)); c.execute('INSERT INTO device_events(camera_id,event_type,new_value,detail) VALUES(?,?,?,?)',(cam_id,'stream_observation',str(score),_safe_str(d.get('failure_reason')))); c.commit(); _advanced_audit('stream_observation','camera',cam_id,str(score)); return jsonify({'ok':True,'quality_score':score})

@app.route('/api/advanced/duplicates')
def api_adv_duplicates():
    rows=_db_conn().execute("SELECT ip,port,COUNT(*) n,GROUP_CONCAT(id) ids FROM cameras WHERE deleted=0 AND ip!='' GROUP BY ip,port HAVING COUNT(*)>1 ORDER BY n DESC LIMIT 1000").fetchall(); return jsonify({'ok':True,'duplicates':[dict(x) for x in rows]})

@app.route('/api/advanced/merge',methods=['POST'])
def api_adv_merge():
    d=request.get_json(silent=True) or {}; ids=list(dict.fromkeys(str(x) for x in d.get('ids',[]) if x)); canonical=str(d.get('canonical') or '')
    if len(ids)<2 or canonical not in ids:return jsonify({'ok':False,'detail':'Provide ids and canonical'}),400
    c=_db_conn(); ph=','.join('?'*len(ids)); rows=c.execute(f'SELECT * FROM cameras WHERE id IN ({ph}) AND deleted=0',ids).fetchall(); keep=next((r for r in rows if r['id']==canonical),None)
    if len(rows)<2 or not keep:return jsonify({'ok':False,'detail':'Duplicate records not found'}),404
    try:urls=json.loads(keep['rtsp_urls'] or '[]')
    except Exception:urls=[]
    for r in rows:
        if r['id']==canonical:continue
        try:more=json.loads(r['rtsp_urls'] or '[]')
        except Exception:more=[]
        urls += [u for u in more if u not in urls]; c.execute("UPDATE cameras SET deleted=1,deleted_at=datetime('now'),updated_at=datetime('now') WHERE id=?",(r['id'],)); c.execute('DELETE FROM validation_queue WHERE camera_id=?',(r['id'],))
    c.execute("UPDATE cameras SET rtsp_urls=?,updated_at=datetime('now') WHERE id=?",(json.dumps(urls),canonical)); c.commit(); _advanced_audit('merge_duplicates','camera',canonical,','.join(ids)); return jsonify({'ok':True,'canonical':canonical,'merged':len(rows)-1})

@app.route('/api/advanced/trash')
def api_adv_trash():
    rows=_db_conn().execute('SELECT id,label,ip,group_name,deleted_at FROM cameras WHERE deleted=1 ORDER BY deleted_at DESC LIMIT 2000').fetchall(); return jsonify({'ok':True,'items':[dict(x) for x in rows]})

@app.route('/api/advanced/restore',methods=['POST'])
def api_adv_restore():
    ids=list(dict.fromkeys(str(x) for x in (request.get_json(silent=True) or {}).get('ids',[]) if x));
    if not ids:return jsonify({'ok':False,'detail':'No ids'}),400
    c=_db_conn(); ph=','.join('?'*len(ids)); cur=c.execute(f"UPDATE cameras SET deleted=0,updated_at=datetime('now') WHERE id IN ({ph})",ids); c.commit(); _advanced_audit('restore','camera',','.join(ids),str(cur.rowcount)); return jsonify({'ok':True,'restored':cur.rowcount})

@app.route('/api/advanced/category/meta',methods=['GET','POST'])
def api_adv_category_meta():
    c=_db_conn()
    if request.method=='POST':
        d=request.get_json(silent=True) or {}; cat=_normalize_group_name(d.get('category',''))
        if not cat:return jsonify({'ok':False,'detail':'category required'}),400
        c.execute("INSERT INTO category_meta(category,note,locked,color) VALUES(?,?,?,?) ON CONFLICT(category) DO UPDATE SET note=excluded.note,locked=excluded.locked,color=excluded.color,updated_at=datetime('now')",(cat,_safe_str(d.get('note')),int(bool(d.get('locked'))),_safe_str(d.get('color')))); c.commit(); _advanced_audit('category_meta','category',cat,'updated'); return jsonify({'ok':True})
    rows=c.execute('SELECT * FROM category_meta ORDER BY category').fetchall(); return jsonify({'ok':True,'categories':[dict(x) for x in rows]})

@app.route('/api/advanced/category/rename',methods=['POST'])
def api_adv_category_rename():
    d=request.get_json(silent=True) or {}; old=_normalize_group_name(d.get('old','')); new=_normalize_group_name(d.get('new',''))
    if not old or not new:return jsonify({'ok':False,'detail':'old/new required'}),400
    c=_db_conn(); conflict=c.execute('SELECT 1 FROM cameras WHERE deleted=0 AND group_name=? LIMIT 1',(new,)).fetchone()
    if conflict and old.lower()!=new.lower():return jsonify({'ok':False,'detail':'Destination already exists; use merge'}),409
    cur=c.execute("UPDATE cameras SET group_name=?,updated_at=datetime('now') WHERE deleted=0 AND group_name=?",(new,old)); c.execute('UPDATE category_meta SET category=? WHERE category=?',(new,old)); c.commit(); _advanced_audit('category_rename','category',old,new); return jsonify({'ok':True,'renamed':cur.rowcount})

@app.route('/api/advanced/category/merge',methods=['POST'])
def api_adv_category_merge():
    d=request.get_json(silent=True) or {}; sources=[_normalize_group_name(x) for x in d.get('sources',[]) if _normalize_group_name(x)]; target=_normalize_group_name(d.get('target',''))
    if not sources or not target or target in sources:return jsonify({'ok':False,'detail':'Valid sources and target required'}),400
    c=_db_conn(); total=0
    for g in sources:
        cur=c.execute("UPDATE cameras SET group_name=?,updated_at=datetime('now') WHERE deleted=0 AND group_name=?",(target,g)); total+=cur.rowcount
    c.commit(); _advanced_audit('category_merge','category',target,','.join(sources)); return jsonify({'ok':True,'moved':total})

@app.route('/api/advanced/search')
def api_adv_search():
    q=_safe_str(request.args.get('q','')); status=_safe_str(request.args.get('status','all')); country=_safe_str(request.args.get('country','')); vendor=_safe_str(request.args.get('vendor','')); limit=min(1000,max(1,int(request.args.get('limit',200)))); clauses=['deleted=0']; args=[]
    if q:clauses.append('(ip LIKE ? OR label LIKE ? OR channel_name LIKE ? OR rtsp_urls LIKE ? OR device_id LIKE ?)');args += [f'%{q}%']*5
    if status!='all':clauses.append('status=?');args.append(status)
    if country:clauses.append('country=?');args.append(country)
    if vendor:clauses.append('camera_type LIKE ?');args.append(f'%{vendor}%')
    rows=_db_conn().execute('SELECT * FROM cameras WHERE '+' AND '.join(clauses)+' ORDER BY updated_at DESC LIMIT ?',args+[limit]).fetchall();return jsonify({'ok':True,'count':len(rows),'items':[_db_row(r) for r in rows]})

@app.route('/api/advanced/filter/save',methods=['POST'])
def api_adv_filter_save():
    d=request.get_json(silent=True) or {}; name=_safe_str(d.get('name')).strip()
    if not name:return jsonify({'ok':False,'detail':'name required'}),400
    c=_db_conn();c.execute('INSERT INTO saved_filters(name,query_json) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET query_json=excluded.query_json',(name,json.dumps(d.get('query') or {})));c.commit();return jsonify({'ok':True})

@app.route('/api/advanced/filter/list')
def api_adv_filter_list():
    rows=_db_conn().execute('SELECT * FROM saved_filters ORDER BY name').fetchall();return jsonify({'ok':True,'filters':[dict(x) for x in rows]})

@app.route('/api/advanced/audit')
def api_adv_audit():
    rows=_db_conn().execute('SELECT actor,action,object_type,object_id,detail,created_at FROM audit_log ORDER BY id DESC LIMIT 2000').fetchall();return jsonify({'ok':True,'items':[dict(x) for x in rows]})

@app.route('/api/advanced/reconcile',methods=['POST'])
def api_adv_reconcile():
    if not _service_enabled('advanced-metadata'):
        return jsonify({'ok':False,'processed':0,'detail':'Advanced Metadata Sync is OFF in Background Services'}),409
    d=request.get_json(silent=True) or {}
    n=_advanced_reconcile_all(min(10000,int(d.get('limit',3000))))
    return jsonify({'ok':True,'processed':n})

@app.route('/api/advanced/report')
def api_adv_report():
    c=_db_conn();total=c.execute('SELECT COUNT(*) FROM cameras WHERE deleted=0').fetchone()[0];statuses=c.execute('SELECT status,COUNT(*) n FROM cameras WHERE deleted=0 GROUP BY status').fetchall();vendors=c.execute('SELECT camera_type,COUNT(*) n FROM cameras WHERE deleted=0 GROUP BY camera_type ORDER BY n DESC LIMIT 100').fetchall();countries=c.execute('SELECT country,COUNT(*) n FROM cameras WHERE deleted=0 GROUP BY country ORDER BY n DESC LIMIT 200').fetchall();return jsonify({'ok':True,'generated_at':datetime.now().isoformat(),'total':total,'statuses':[dict(x) for x in statuses],'vendors':[dict(x) for x in vendors],'countries':[dict(x) for x in countries]})

# ══════════════════════════════════════════════════════════════════════════════
# v39 ADVANCED OPERATIONS API — safe, authenticated inventory/health tooling
# ══════════════════════════════════════════════════════════════════════════════
def _adv_mem_available_mb():
    try:
        pages = os.sysconf('SC_AVPHYS_PAGES'); size = os.sysconf('SC_PAGE_SIZE')
        return round((pages * size) / 1024 / 1024, 1)
    except Exception:
        return None

def _adv_cpu_load():
    try:
        if hasattr(os, 'getloadavg'):
            one, five, fifteen = os.getloadavg()
            return {'one_min': round(one,2), 'five_min': round(five,2), 'fifteen_min': round(fifteen,2)}
    except Exception:
        pass
    return {}

def _adv_db_summary():
    c=_db_conn()
    total=int(c.execute('SELECT COUNT(*) FROM cameras WHERE deleted=0').fetchone()[0])
    online=int(c.execute("SELECT COUNT(*) FROM cameras WHERE deleted=0 AND lower(status)='online'").fetchone()[0])
    offline=int(c.execute("SELECT COUNT(*) FROM cameras WHERE deleted=0 AND lower(status)='offline'").fetchone()[0])
    checking=int(c.execute("SELECT COUNT(*) FROM cameras WHERE deleted=0 AND lower(status) IN ('checking','validating','pending')").fetchone()[0])
    unknown=max(0,total-online-offline-checking)
    rtsp_working=int(c.execute("SELECT COUNT(*) FROM cameras WHERE deleted=0 AND (rtsp_score>0 OR lower(status)='online')").fetchone()[0])
    favorites=int(c.execute('SELECT COUNT(*) FROM cameras WHERE deleted=0 AND favorite=1').fetchone()[0])
    groups=int(c.execute("SELECT COUNT(DISTINCT group_name) FROM cameras WHERE deleted=0 AND trim(group_name)<>''").fetchone()[0])
    rows=c.execute("SELECT COALESCE(NULLIF(camera_type,''),'Unknown') type, COALESCE(NULLIF(country,''),'Unknown') country, COALESCE(NULLIF(status,''),'unknown') status, COUNT(*) count FROM cameras WHERE deleted=0 GROUP BY type,country,status ORDER BY count DESC LIMIT 200").fetchall()
    # Vendor is not guaranteed as a cameras-table column in old DBs; infer from camera_type/label safely.
    breakdown=[]
    for r in rows:
        typ=str(r[0] or 'Unknown'); vendor=typ
        low=typ.lower()
        for name in ('hikvision','dahua','uniview','axis','foscam','reolink','amcrest','onvif','m3u','rtsp'):
            if name in low: vendor=name.upper() if name=='rtsp' else name.title(); break
        breakdown.append({'vendor':vendor,'status':r[2],'type':typ,'country':r[1],'count':int(r[3])})
    return {'total':total,'online':online,'offline':offline,'checking':checking,'unknown':unknown,'rtsp_working':rtsp_working,'favorites':favorites,'groups':groups,'breakdown':breakdown}

@app.route('/api/advanced/overview')
def api_advanced_overview():
    try:
        usage=shutil.disk_usage(SCRIPT_DIR)
        backups=[]
        for b in DB_BACKUPS:
            if os.path.exists(b): backups.append({'path':os.path.basename(b),'size':os.path.getsize(b),'mtime':datetime.fromtimestamp(os.path.getmtime(b),timezone.utc).isoformat()})
        workers={}
        try: workers=_worker_health_snapshot()
        except Exception: pass
        return jsonify({'ok':True,'version':VERSION,'cameras':_adv_db_summary(),'runtime':{'cpu_cores':os.cpu_count() or 1,'load':_adv_cpu_load(),'ram_available_mb':_adv_mem_available_mb(),'ram_available':((_adv_mem_available_mb() or 0)>0 and f'{_adv_mem_available_mb():.0f} MB' or 'unknown'),'disk_free':f'{usage.free/1024/1024/1024:.1f} GB','disk_total':f'{usage.total/1024/1024/1024:.1f} GB','max_streams':MAX_STREAMS},'backup':{'latest':backups[0]['mtime'] if backups else None,'files':backups},'workers':workers})
    except Exception as e:
        log.exception('advanced overview failed')
        return jsonify({'ok':False,'detail':str(e)}),500

@app.route('/api/advanced/backup', methods=['POST'])
def api_advanced_backup():
    if _auth_enabled() and not _has_permission('database.manage'):
        return jsonify({'ok':False,'detail':'Permission required: database.manage'}),403
    ok=db_create_backup()
    return jsonify({'ok':ok,'detail':'Verified SQLite backup created.' if ok else 'SQLite backup failed.'}), (200 if ok else 500)

@app.route('/api/advanced/integrity')
def api_advanced_integrity():
    if _auth_enabled() and not _has_permission('database.manage'):
        return jsonify({'ok':False,'detail':'Permission required: database.manage'}),403
    try:
        c=_db_conn(); rows=c.execute('PRAGMA integrity_check').fetchall(); ok=(len(rows)==1 and rows[0][0]=='ok')
        return jsonify({'ok':ok,'integrity':rows[0][0] if rows else 'no result'})
    except Exception as e:
        return jsonify({'ok':False,'detail':str(e)}),500

@app.route('/api/advanced/export')
def api_advanced_export():
    try:
        d=_adv_db_summary(); d['generated_at']=utc_iso(); d['version']=VERSION; d['runtime']={'cpu_cores':os.cpu_count() or 1,'load':_adv_cpu_load(),'ram_available_mb':_adv_mem_available_mb()}; return jsonify(d)
    except Exception as e: return jsonify({'ok':False,'detail':str(e)}),500

@app.route('/api/advanced/diagnostics', methods=['POST'])
def api_advanced_diagnostics():
    if _auth_enabled() and not _has_permission('diagnostics.run'):
        return jsonify({'ok':False,'detail':'Permission required: diagnostics.run'}),403
    try:
        payload=request.get_json(silent=True) or {}; ids=[str(x) for x in (payload.get('ids') or [])][:50]
        results=[]
        for cid in ids:
            cam=db_get_camera(cid)
            if not cam: results.append({'id':cid,'ok':False,'detail':'Camera not found'}); continue
            # Reuse the existing read-only diagnostic endpoint logic where possible.
            host=str(cam.get('ip') or '').strip(); port=int(cam.get('port') or 0); rtsp_port=int(cam.get('rtsp_port') or 554)
            row={'id':cid,'label':cam.get('label',''),'ip':host,'port':port,'rtsp_port':rtsp_port,'status':cam.get('status','unknown'),'type':cam.get('camera_type',''),'country':cam.get('country',''),'last_checked':cam.get('last_checked',''),'latency_ms':cam.get('latency_ms',0),'rtsp_score':cam.get('rtsp_score',0),'failure_reason':cam.get('failure_reason',''),'ok':True}
            # Single-host TCP checks only; never scan neighboring addresses.
            checks=[]
            for pnum in [port,rtsp_port]:
                if not host or not pnum or pnum in [x.get('port') for x in checks]: continue
                t0=time.monotonic()
                try:
                    with socket.create_connection((host,pnum),timeout=min(1.0,DEVICE_PORT_SCAN_TIMEOUT)):
                        checks.append({'port':pnum,'reachable':True,'latency_ms':round((time.monotonic()-t0)*1000,1)})
                except Exception as ex: checks.append({'port':pnum,'reachable':False,'error':type(ex).__name__})
            row['tcp']=checks; results.append(row)
        return jsonify({'ok':True,'count':len(results),'results':results})
    except Exception as e:
        log.exception('advanced diagnostics failed'); return jsonify({'ok':False,'detail':str(e)}),500


# ══════════════════════════════════════════════════════════════════════════════
# v40 COMPLETE OPERATIONS LAYER — analytics, alerts, layouts, exports, history
# ══════════════════════════════════════════════════════════════════════════════

def _v40_actor():
    return session.get('user', 'system') if _auth_enabled() else 'system'

def _v40_json(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':'))

def _v40_camera_vendor(cam):
    text=' '.join(str(cam.get(k) or '') for k in ('camera_type','label','device_name','web_title')).lower()
    for v in ('hikvision','dahua','uniview','axis','foscam','reolink','amcrest','hanwha','samsung','bosch','tapo','xmeye','xiongmai','onvif'):
        if v in text: return v
    if 'm3u' in text: return 'm3u'
    if 'rtsp' in text: return 'rtsp'
    return 'unknown'

def _v40_device_class(cam):
    t=str(cam.get('camera_type') or '').lower()
    if 'm3u' in t or 'rtsp stream' in t or 'standalone' in t: return 'standalone_stream'
    if 'door' in t: return 'door_camera'
    if any(x in t for x in ('nvr','network video recorder')): return 'nvr'
    if 'dvr' in t: return 'dvr'
    if 'camera' in t or 'ip cam' in t: return 'ip_camera'
    return 'unknown'

def _v40_quality_score(cam):
    score=0.0
    if str(cam.get('status')).lower()=='online': score+=35
    if cam.get('codec'): score+=10
    if cam.get('resolution'): score+=15
    if float(cam.get('fps') or 0)>0: score+=10
    if float(cam.get('latency_ms') or 0)>0:
        score += max(0, min(15, 15-float(cam.get('latency_ms') or 0)/100))
    if float(cam.get('rtsp_score') or 0)>0: score+=15
    return round(min(100,score),1)

def _v40_write_history(cam_id, result):
    try:
        c=_db_conn(); c.execute('INSERT INTO diagnostic_history(camera_id,result_json) VALUES(?,?)',(cam_id,_v40_json(result))); c.commit()
    except Exception: log.exception('diagnostic history failed')

def _v40_alert(event_type, cam_id, old_status='', new_status='', message=''):
    """Persist an alert without holding a SQLite transaction during Telegram I/O."""
    try:
        c = _db_conn()
        rule = c.execute(
            'SELECT * FROM alert_rules WHERE event_type=? AND enabled=1 ORDER BY id LIMIT 1',
            (event_type,)
        ).fetchone()
        fingerprint = hashlib.sha256(f'{event_type}|{cam_id}'.encode()).hexdigest()
        dedup = c.execute(
            'SELECT last_sent FROM v43_alert_dedup WHERE fingerprint=?',
            (fingerprint,)
        ).fetchone()
        # End any read transaction before doing network I/O. A slow Telegram
        # request must never keep SQLite locked.
        try:
            c.rollback()
        except Exception:
            pass

        sent = 0
        telegram_ok = False
        telegram_detail = ''
        if rule:
            last = (dedup['last_sent'] if dedup else '') or rule['last_sent'] or ''
            allow = True
            if last:
                try:
                    allow = (utc_now() - datetime.fromisoformat(last.replace('Z', '+00:00'))).total_seconds() >= int(rule['cooldown_seconds'] or 300)
                except Exception:
                    pass
            if allow:
                telegram_ok, telegram_detail = _send_telegram(message)
                sent = int(telegram_ok)
            else:
                telegram_detail = ''

        alert_message = message if telegram_ok or not rule or not telegram_detail else f'{message} | telegram_error: {str(telegram_detail)[:300]}'

        def _write_alert():
            now = utc_iso()
            c2 = _db_conn()
            if telegram_ok and rule:
                c2.execute("UPDATE alert_rules SET last_sent=? WHERE id=?", (now, rule['id']))
                c2.execute("INSERT INTO v43_alert_dedup(fingerprint,last_sent,count) VALUES(?,?,1) ON CONFLICT(fingerprint) DO UPDATE SET last_sent=excluded.last_sent,count=v43_alert_dedup.count+1", (fingerprint, now))
            c2.execute(
                'INSERT INTO camera_alert_history(camera_id,event_type,old_status,new_status,message,sent) VALUES(?,?,?,?,?,?)',
                (cam_id, event_type, old_status, new_status, alert_message, sent)
            )
            c2.commit()

        _db_retry_write(_write_alert, 'alert history write')
    except Exception:
        log.exception('alert processing failed')

def _v40_analytics():
    """Compute analytics incrementally so large inventories never become a huge dict list."""
    c=_db_conn()
    clauses=['deleted=0']; params=[]
    if _auth_enabled() and not (_current_role()=='admin' and not hasattr(g,'api_key_permissions')):
        user=_current_user()
        if user:
            clauses.append("EXISTS (SELECT 1 FROM v43_user_access_scope s WHERE s.username=? AND ((s.scope_type='camera' AND s.scope_value=cameras.id) OR (s.scope_type='country' AND lower(s.scope_value)=lower(cameras.country)) OR (s.scope_type='device' AND (lower(s.scope_value)=lower(cameras.device_id) OR lower(s.scope_value)=lower(cameras.ip)))) )")
            params.append(user)
    where=' AND '.join(clauses)
    total=online=offline=checking=favorites=0
    vendors={}; device_classes={}; countries={}; categories={}; codecs={}; resolutions={}; lat=[]; quality_sum=0.0
    for row in c.execute('SELECT status,favorite,camera_type,device_id,country,group_name,codec,resolution,latency_ms,rtsp_score FROM cameras WHERE '+where,params):
        cam=dict(row)
        total+=1; st=str(cam.get('status') or '').lower()
        online += st=='online'; offline += st=='offline'; checking += st=='checking'; favorites += int(cam.get('favorite') or 0)
        def inc(d,k):
            k=k or 'Unknown'; d[k]=d.get(k,0)+1
        inc(vendors,_v40_camera_vendor(cam)); inc(device_classes,_v40_device_class(cam)); inc(countries,cam.get('country') or 'Unknown'); inc(categories,cam.get('group_name') or 'Uncategorized'); inc(codecs,cam.get('codec') or 'Unknown'); inc(resolutions,cam.get('resolution') or 'Unknown')
        lm=float(cam.get('latency_ms') or 0)
        if lm>0: lat.append(lm)
        quality_sum += _v40_quality_score(cam)
    vendors=dict(sorted(vendors.items(),key=lambda x:(-x[1],x[0]))); device_classes=dict(sorted(device_classes.items(),key=lambda x:(-x[1],x[0])))
    countries=dict(sorted(countries.items(),key=lambda x:(-x[1],x[0]))); categories=dict(sorted(categories.items(),key=lambda x:(-x[1],x[0])))
    codecs=dict(sorted(codecs.items(),key=lambda x:(-x[1],x[0]))); resolutions=dict(sorted(resolutions.items(),key=lambda x:(-x[1],x[0])))
    p95=round(statistics.quantiles(lat,n=20)[18],1) if len(lat)>=20 else (max(lat) if lat else 0)
    return {'total':total,'online':online,'offline':offline,'checking':checking,'favorites':favorites,'vendors':vendors,'device_classes':device_classes,'countries':countries,'categories':categories,'codecs':codecs,'resolutions':resolutions,'avg_latency_ms':round(statistics.mean(lat),1) if lat else 0,'p95_latency_ms':p95,'quality_avg':round(quality_sum/total,1) if total else 0}

def _v40_export_rows(ids=None):
    c=_db_conn()
    if ids:
        ids=[str(x) for x in ids if str(x)][:5000]; ph=','.join('?'*len(ids)); return _filter_camera_access([dict(x) for x in c.execute(f'SELECT * FROM cameras WHERE id IN ({ph}) ORDER BY id',ids).fetchall()])
    return _filter_camera_access([dict(x) for x in c.execute('SELECT * FROM cameras WHERE deleted=0 ORDER BY group_name,id').fetchall()])

def _v40_csv(rows):
    fields=['id','label','ip','port','username','group_name','camera_type','status','country','region','city','codec','resolution','fps','latency_ms','rtsp_port','rtsp_score','last_checked','failure_reason']
    out=io.StringIO(); w=csv.DictWriter(out,fieldnames=fields,extrasaction='ignore'); w.writeheader()
    for r in rows:
        rr=dict(r); rr['username']=rr.get('username') or ''; rr.pop('password',None); w.writerow(rr)
    return out.getvalue().encode('utf-8-sig')

def _v40_m3u(rows):
    lines=['#EXTM3U']
    for r in rows:
        urls=[]
        try: urls=json.loads(r.get('rtsp_urls') or '[]')
        except Exception: urls=[]
        for u in urls:
            if isinstance(u,str) and u.lower().startswith(('rtsp://','http://','https://')):
                lines.append(f"#EXTINF:-1,{r.get('label') or r.get('ip') or r.get('id')}")
                lines.append(u)
    return ('\n'.join(lines)+'\n').encode('utf-8')

def _v40_health_snapshot():
    """Export health history safely across old/new SQLite schemas."""
    c=_db_conn()
    cols={r[1] for r in c.execute('PRAGMA table_info(camera_health_history)').fetchall()}
    wanted=['id','camera_id','status','latency_ms','codec','resolution','fps','failure_reason','candidate_url','transport','checked_at']
    if 'rtsp_score' in cols:
        wanted.append('rtsp_score')
    elif 'candidate_score' in cols:
        wanted.append('candidate_score AS rtsp_score')
    sql='SELECT '+','.join(wanted)+' FROM camera_health_history ORDER BY id DESC LIMIT 5000'
    return [dict(x) for x in c.execute(sql).fetchall()]

def _v40_add_tag(cam_id, tag):
    tag=str(tag or '').strip()[:80]
    if not tag: return False
    c=_db_conn(); c.execute('INSERT OR IGNORE INTO camera_tags(camera_id,tag) VALUES(?,?)',(str(cam_id),tag)); c.commit(); return True

def _v40_remove_tag(cam_id, tag):
    c=_db_conn(); c.execute('DELETE FROM camera_tags WHERE camera_id=? AND tag=?',(str(cam_id),str(tag))); c.commit()

def _v40_tags(cam_id=None):
    c=_db_conn()
    if cam_id is not None: return [r[0] for r in c.execute('SELECT tag FROM camera_tags WHERE camera_id=? ORDER BY tag',(str(cam_id),)).fetchall()]
    return [dict(r) for r in c.execute('SELECT camera_id,tag,created_at FROM camera_tags ORDER BY tag,camera_id').fetchall()]

_V126_LAST_RETENTION = 0.0
_V135_RETENTION_INDEX = 0

def _v126_retention_delete(table, column, cutoff_sql, batch_size=250, max_batches=1):
    """Delete old retention rows in tiny committed transactions.

    Retention must never hold the SQLite writer for the whole history table.
    Each batch is independently committed so validation/status/audit writers can
    make progress between cleanup batches.
    """
    if _shutdown_requested.is_set():
        return 0
    total = 0
    for _ in range(max(1, int(max_batches))):
        if _shutdown_requested.is_set():
            break
        def _delete_batch():
            c = _db_conn()
            rows = c.execute(
                f"SELECT rowid FROM {table} WHERE {column} IS NOT NULL AND {column} < datetime('now', ?) LIMIT ?",
                (cutoff_sql, int(batch_size)),
            ).fetchall()
            if not rows:
                return 0
            ids = [r[0] for r in rows]
            placeholders = ','.join('?' for _ in ids)
            cur = c.execute(f"DELETE FROM {table} WHERE rowid IN ({placeholders})", ids)
            c.commit()
            return max(0, int(cur.rowcount or 0))
        try:
            n = _db_retry_write(_delete_batch, f'retention {table}')
            if not n:
                break
            total += n
            if n < batch_size:
                break
        except Exception:
            break
    return total


def _v40_maintenance():
    """Low-contention retention maintenance.

    The old implementation deleted entire history tables in one transaction and
    then ran WAL TRUNCATE. With a large DB that could monopolize SQLite for
    100+ seconds. v126 deliberately uses bounded, committed batches and never
    runs WAL TRUNCATE while validation is active.
    """
    global _V126_LAST_RETENTION
    now_mono = time.monotonic()
    # Health sampling can remain frequent; database retention cannot.  Five
    # minutes is enough for housekeeping and prevents repeated large scans.
    if now_mono - _V126_LAST_RETENTION < 600.0:
        return 0
    _V126_LAST_RETENTION = now_mono
    if _shutdown_requested.is_set():
        return 0

    deleted_cutoff = (utc_now() - timedelta(days=DELETED_RETENTION_DAYS)).strftime('%Y-%m-%dT%H:%M:%S')
    snap_cutoff = (utc_now() - timedelta(days=SNAPSHOT_RETENTION_DAYS)).timestamp()
    cutoff_sql = f'-{HISTORY_RETENTION_DAYS} days'
    removed = 0

    # v135: one retention target per maintenance pass. This avoids a burst of
    # many writer transactions that competes with validation/UI operations.
    global _V135_RETENTION_INDEX
    retention_targets=[
        ('camera_trash','deleted_at'),('camera_health_history','checked_at'),
        ('audit_log','created_at'),('camera_alert_history','created_at'),
        ('diagnostic_history','created_at'),('v43_health_samples','checked_at'),
        ('v43_snapshot_history','created_at'),('stream_observations','observed_at'),
        ('worker_events','created_at'),('recovery_events','created_at'),
        ('login_history','created_at'),('telegram_delivery_log','created_at'),
        ('scan_history','finished_at'),('device_evidence','created_at'),
        ('device_events','created_at'),('v43_feature_runs','started_at')]
    table,col=retention_targets[_V135_RETENTION_INDEX % len(retention_targets)]
    _V135_RETENTION_INDEX=(_V135_RETENTION_INDEX+1)%len(retention_targets)
    try:
        cutoff=deleted_cutoff if table=='camera_trash' else cutoff_sql
        removed += _v126_retention_delete(table,col,cutoff,batch_size=100,max_batches=1)
    except Exception:
        pass
    # Inventory jobs are larger; give them an isolated small pass only every
    # fourth maintenance cycle.
    if _V135_RETENTION_INDEX % 4 == 0:
        try:
            inv_cutoff=(utc_now()-timedelta(days=HISTORY_RETENTION_DAYS)).isoformat().replace('+00:00','Z')
            def _delete_inventory_batch():
                c=_db_conn(); rows=c.execute("SELECT rowid FROM inventory_jobs WHERE status IN ('completed','failed','cancelled') AND created_at < ? ORDER BY created_at LIMIT 100",(inv_cutoff,)).fetchall()
                if not rows:return 0
                ids=[r[0] for r in rows]; ph=','.join('?' for _ in ids)
                cur=c.execute(f'DELETE FROM inventory_jobs WHERE rowid IN ({ph})',ids); c.commit()
                return max(0,int(cur.rowcount or 0))
            removed += _db_retry_write(_delete_inventory_batch,'inventory job retention')
        except Exception:
            pass
    if os.path.isdir(SNAPSHOT_DIR):
        checked=0
        for f in Path(SNAPSHOT_DIR).iterdir():
            if _shutdown_requested.is_set() or checked>=100: break
            try:
                if f.is_file() and f.stat().st_mtime < snap_cutoff: f.unlink()
                checked+=1
            except Exception: pass

    # Do not TRUNCATE the WAL from inside maintenance. TRUNCATE needs an
    # exclusive lock and was a major source of contention with validators.
    # Normal WAL autocheckpointing remains responsible for routine cleanup.
    if os.path.isdir(SNAPSHOT_DIR):
        for f in Path(SNAPSHOT_DIR).iterdir():
            if _shutdown_requested.is_set():
                break
            try:
                if f.is_file() and f.stat().st_mtime < snap_cutoff:
                    f.unlink()
            except Exception:
                pass
    return removed

@app.route('/api/v40/analytics')
def v40_analytics():
    return jsonify({'ok':True,'generated_at':utc_iso(),'analytics':_v40_analytics()})

@app.route('/api/v40/health/history')
def v40_health_history():
    limit=max(1,min(5000,int(request.args.get('limit',500)))); cid=request.args.get('camera_id','').strip()
    c=_db_conn();
    if cid: rows=c.execute('SELECT * FROM camera_health_history WHERE camera_id=? ORDER BY id DESC LIMIT ?',(cid,limit)).fetchall()
    else: rows=c.execute('SELECT * FROM camera_health_history ORDER BY id DESC LIMIT ?',(limit,)).fetchall()
    return jsonify({'ok':True,'items':[dict(x) for x in rows]})

@app.route('/api/v40/cameras/<cam_id>/history')
def v40_camera_history(cam_id):
    c=_db_conn(); health=c.execute('SELECT * FROM camera_health_history WHERE camera_id=? ORDER BY id DESC LIMIT 500',(cam_id,)).fetchall(); diag=c.execute('SELECT result_json,created_at FROM diagnostic_history WHERE camera_id=? ORDER BY id DESC LIMIT 100',(cam_id,)).fetchall(); changes=c.execute('SELECT field_name,old_value,new_value,created_at FROM camera_change_history WHERE camera_id=? ORDER BY id DESC LIMIT 200',(cam_id,)).fetchall(); alerts=c.execute('SELECT event_type,old_status,new_status,message,sent,created_at FROM camera_alert_history WHERE camera_id=? ORDER BY id DESC LIMIT 200',(cam_id,)).fetchall(); return jsonify({'ok':True,'health':[dict(x) for x in health],'diagnostics':[dict(x) for x in diag],'changes':[dict(x) for x in changes],'alerts':[dict(x) for x in alerts],'tags':_v40_tags(cam_id)})

@app.route('/api/v40/tags',methods=['GET','POST','DELETE'])
def v40_tags_api():
    d=request.get_json(silent=True) or {}
    if request.method=='POST':
        for cid in d.get('camera_ids') or [d.get('camera_id')]: _v40_add_tag(cid,d.get('tag'))
        _advanced_audit('tag_add','camera',','.join(map(str,d.get('camera_ids') or [d.get('camera_id','')])),str(d.get('tag','')),'v40')
    elif request.method=='DELETE': _v40_remove_tag(d.get('camera_id'),d.get('tag'))
    if d.get('camera_id'): return jsonify({'ok':True,'tags':_v40_tags(d.get('camera_id'))})
    return jsonify({'ok':True,'items':_v40_tags()})

@app.route('/api/v40/layouts',methods=['GET','POST','DELETE'])
def v40_layouts():
    c=_db_conn(); d=request.get_json(silent=True) or {}
    if request.method=='POST':
        name=str(d.get('name') or '').strip()[:100]; layout=d.get('layout') or {}
        if not name: return jsonify({'ok':False,'detail':'Layout name required'}),400
        c.execute("INSERT INTO live_layouts(name,layout_json,updated_at) VALUES(?,?,datetime('now')) ON CONFLICT(name) DO UPDATE SET layout_json=excluded.layout_json,updated_at=datetime('now')",(name,_v40_json(layout))); c.commit()
    elif request.method=='DELETE':
        c.execute('DELETE FROM live_layouts WHERE name=?',(str(d.get('name') or ''),)); c.commit()
    rows=c.execute('SELECT * FROM live_layouts ORDER BY name').fetchall(); return jsonify({'ok':True,'layouts':[dict(x) for x in rows]})

@app.route('/api/v40/alerts/rules',methods=['GET','POST','DELETE'])
def v40_alert_rules():
    c=_db_conn(); d=request.get_json(silent=True) or {}
    if request.method=='POST':
        name=str(d.get('name') or '').strip()[:100]; event=str(d.get('event_type') or 'status_change');
        c.execute("INSERT INTO alert_rules(name,event_type,enabled,cooldown_seconds,config_json) VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET event_type=excluded.event_type,enabled=excluded.enabled,cooldown_seconds=excluded.cooldown_seconds,config_json=excluded.config_json",(name,event,int(bool(d.get('enabled',True))),max(60,int(d.get('cooldown_seconds',300))),_v40_json(d.get('config') or {}))); c.commit()
    elif request.method=='DELETE': c.execute('DELETE FROM alert_rules WHERE id=?',(int(d.get('id',0)),)); c.commit()
    return jsonify({'ok':True,'rules':[dict(x) for x in c.execute('SELECT * FROM alert_rules ORDER BY id').fetchall()]})

@app.route('/api/v40/alerts/test',methods=['POST'])
def v40_alert_test():
    ok,msg=_send_telegram('🧪 *Camera Dashboard v40 alert test*\nTime: '+utc_iso())
    return jsonify({'ok':ok,'detail':msg}), (200 if ok else 503)

@app.route('/api/v40/alerts/history')
def v40_alert_history():
    limit=max(1,min(2000,int(request.args.get('limit',200)))); rows=_db_conn().execute('SELECT * FROM camera_alert_history ORDER BY id DESC LIMIT ?',(limit,)).fetchall(); return jsonify({'ok':True,'items':[dict(x) for x in rows]})

@app.route('/api/v40/export/<fmt>')
def v40_export(fmt):
    ids=[x for x in request.args.get('ids','').split(',') if x]
    rows=_v40_export_rows(ids or None)
    if fmt=='csv': return Response(_v40_csv(rows),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=camera_inventory.csv'})
    if fmt=='json': return Response(json.dumps([{k:v for k,v in r.items() if k!='password'} for r in rows],ensure_ascii=False,indent=2),mimetype='application/json',headers={'Content-Disposition':'attachment; filename=camera_inventory.json'})
    if fmt=='m3u': return Response(_v40_m3u(rows),mimetype='audio/x-mpegurl',headers={'Content-Disposition':'attachment; filename=cameras.m3u'})
    if fmt=='health': return Response(json.dumps(_v40_health_snapshot(),ensure_ascii=False,indent=2),mimetype='application/json',headers={'Content-Disposition':'attachment; filename=health_history.json'})
    return jsonify({'ok':False,'detail':'Unsupported export format'}),400

@app.route('/api/v40/backup/list')
def v40_backup_list():
    items=[]
    for b in DB_BACKUPS:
        if os.path.exists(b): items.append({'name':os.path.basename(b),'size':os.path.getsize(b),'mtime':datetime.fromtimestamp(os.path.getmtime(b),timezone.utc).isoformat()})
    return jsonify({'ok':True,'items':items})

@app.route('/api/v40/maintenance',methods=['POST'])
def v40_maintenance_api():
    removed=_v40_maintenance(); ok=db_create_backup(); return jsonify({'ok':ok,'removed':removed,'backup':ok})

@app.route('/api/v40/backup/restore',methods=['POST'])
def v40_backup_restore():
    d=request.get_json(silent=True) or {}; name=os.path.basename(str(d.get('name') or ''))
    allowed={os.path.basename(x):x for x in DB_BACKUPS}
    src=allowed.get(name)
    if not src or not os.path.exists(src): return jsonify({'ok':False,'detail':'Backup not found'}),404
    if not _sqlite_integrity_ok(src): return jsonify({'ok':False,'detail':'Selected backup failed integrity check'}),422
    # Preserve current DB before restore.
    if not db_create_backup(): return jsonify({'ok':False,'detail':'Could not create safety backup before restore'}),500
    try:
        for sidecar in (DB_PATH+'-wal',DB_PATH+'-shm'):
            try: os.remove(sidecar)
            except FileNotFoundError: pass
        shutil.copy2(src,DB_PATH)
        ok=_sqlite_integrity_ok(DB_PATH); _advanced_audit('restore_backup','database',name,'integrity='+str(ok),_v40_actor())
        return jsonify({'ok':ok,'detail':'Database restored. Restart the application to refresh all connections.' if ok else 'Restore failed integrity check'})
    except Exception as e: return jsonify({'ok':False,'detail':str(e)}),500

@app.route('/api/v40/settings',methods=['GET','POST','DELETE'])
def v40_settings():
    c=_db_conn(); d=request.get_json(silent=True) or {}
    if request.method=='POST':
        for k,v in (d.get('values') or {}).items():
            k=str(k)[:100]; c.execute("INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,datetime('now')) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=datetime('now')",(k,str(v)[:4000]))
        c.commit()
    elif request.method=='DELETE':
        c.execute('DELETE FROM app_settings WHERE key=?',(str(d.get('key') or ''),)); c.commit()
    return jsonify({'ok':True,'settings':{r['key']:r['value'] for r in c.execute('SELECT key,value FROM app_settings ORDER BY key').fetchall()}})

@app.route('/api/v40/worker')
def v40_worker(): return jsonify({'ok':True,'worker':_worker_health_snapshot(),'resource':_resource_snapshot()})

@app.route('/api/v40/search')
def v40_search():
    q=str(request.args.get('q') or '').strip(); status=str(request.args.get('status') or '').strip(); vendor=str(request.args.get('vendor') or '').strip().lower(); country=str(request.args.get('country') or '').strip(); typ=str(request.args.get('type') or '').strip().lower(); tag=str(request.args.get('tag') or '').strip()
    c=_db_conn(); clauses=['deleted=0']; args=[]
    if q: clauses.append('(label LIKE ? OR ip LIKE ? OR group_name LIKE ? OR camera_type LIKE ? OR country LIKE ? OR city LIKE ?)'); args += [f'%{q}%']*6
    if status: clauses.append('lower(status)=?'); args.append(status.lower())
    if country: clauses.append('country=?'); args.append(country)
    if typ: clauses.append('lower(camera_type) LIKE ?'); args.append('%'+typ+'%')
    rows=c.execute('SELECT * FROM cameras WHERE '+' AND '.join(clauses)+' ORDER BY group_name,id LIMIT 1000',args).fetchall(); out=[]
    for r in rows:
        d=dict(r)
        if vendor and _v40_camera_vendor(d)!=vendor: continue
        if tag and tag not in _v40_tags(d['id']): continue
        d['vendor']=_v40_camera_vendor(d); d['device_class']=_v40_device_class(d); d['quality_score']=_v40_quality_score(d); d.pop('password',None); out.append(d)
    return jsonify({'ok':True,'count':len(out),'items':out})

@app.route('/api/v40/snapshot/bulk',methods=['POST'])
def v40_bulk_snapshot():
    d=request.get_json(silent=True) or {}; ids=[str(x) for x in (d.get('ids') or [])][:50]; results=[]
    Path(SNAPSHOT_DIR).mkdir(parents=True,exist_ok=True)
    def one(cid):
        cam=db_get_camera(cid)
        if not cam: return {'id':cid,'ok':False,'detail':'Camera not found'}
        url,detail=_resolve_camera_rtsp(cid)
        if not url: return {'id':cid,'ok':False,'detail':detail or 'No RTSP candidate'}
        try:
            r=subprocess.run(_ffmpeg_snap_cmd(url),capture_output=True,timeout=18)
            if r.returncode!=0 or not r.stdout: return {'id':cid,'ok':False,'detail':r.stderr.decode(errors='ignore')[-240:]}
            ts=datetime.now().strftime('%Y%m%d_%H%M%S'); fname=f'snap_{cid}_{ts}.jpg'; Path(SNAPSHOT_DIR,fname).write_bytes(r.stdout); db_set_snapshot(cid,fname)
            return {'id':cid,'ok':True,'filename':fname,'url':'/snapshots/'+quote(fname)}
        except Exception as e: return {'id':cid,'ok':False,'detail':type(e).__name__+': '+str(e)[:180]}
    with ThreadPoolExecutor(max_workers=min(8,max(1,len(ids))),thread_name_prefix='bulk-snap') as pool:
        results=list(pool.map(one,ids))
    _advanced_audit('bulk_snapshot','camera',','.join(ids),f'{sum(1 for x in results if x.get("ok"))}/{len(results)}')
    return jsonify({'ok':True,'results':results})

@app.route('/api/v40/schedule',methods=['GET','POST','DELETE'])
def v40_schedule():
    c=_db_conn(); d=request.get_json(silent=True) or {}
    if request.method=='POST':
        name=str(d.get('name') or '').strip()[:100]; job=str(d.get('job_type') or 'health_check'); interval=max(3600,int(d.get('interval_seconds',21600)))
        c.execute("INSERT INTO scheduled_jobs(id,name,job_type,interval_seconds,enabled,last_run,next_run) VALUES(?,?,?,?,?,NULL,datetime('now', '+' || ? || ' seconds')) ON CONFLICT(name) DO UPDATE SET job_type=excluded.job_type,interval_seconds=excluded.interval_seconds,enabled=excluded.enabled,next_run=excluded.next_run",(str(uuid.uuid4()),name,job,interval,int(bool(d.get('enabled',True))),interval)); c.commit()
    elif request.method=='DELETE': c.execute('DELETE FROM scheduled_jobs WHERE id=?',(str(d.get('id')),)); c.commit()
    return jsonify({'ok':True,'items':[dict(x) for x in c.execute('SELECT * FROM scheduled_jobs ORDER BY name').fetchall()]})

@app.route('/api/v40/audit/export')
def v40_audit_export():
    rows=[dict(x) for x in _db_conn().execute('SELECT actor,action,object_type,object_id,detail,created_at FROM audit_log ORDER BY id DESC').fetchall()]; return Response(json.dumps(rows,ensure_ascii=False,indent=2),mimetype='application/json',headers={'Content-Disposition':'attachment; filename=audit_log.json'})

@app.route('/api/v40/device/report/<cam_id>')
def v40_device_report(cam_id):
    cam=db_get_camera(cam_id)
    if not cam: return jsonify({'ok':False,'detail':'Camera not found'}),404
    c=_db_conn(); fp=c.execute('SELECT * FROM device_fingerprints WHERE camera_id=? ORDER BY last_seen DESC LIMIT 1',(cam_id,)).fetchone(); ev=c.execute('SELECT * FROM device_evidence WHERE camera_id=? ORDER BY id DESC LIMIT 300',(cam_id,)).fetchall(); hist=c.execute('SELECT * FROM camera_health_history WHERE camera_id=? ORDER BY id DESC LIMIT 100',(cam_id,)).fetchall()
    report={'generated_at':utc_iso(),'camera':{k:v for k,v in cam.items() if k not in ('password','rtsp_urls')},'vendor':_v40_camera_vendor(cam),'device_class':_v40_device_class(cam),'quality_score':_v40_quality_score(cam),'tags':_v40_tags(cam_id),'fingerprint':dict(fp) if fp else None,'evidence':[dict(x) for x in ev],'health':[dict(x) for x in hist]}
    return jsonify({'ok':True,'report':report})

# Background health supervisor: relies only on the existing persisted validation queue.
def _v40_health_supervisor():
    _touch_worker('health-supervisor','starting')
    while not _shutdown_requested.is_set():
        if not _service_enabled('health-supervisor'):
            _touch_worker('health-supervisor','stopped','disabled by operator'); return
        try:
            _touch_worker('health-supervisor','running')
            c=_db_conn(); rows=c.execute("SELECT id,status,last_alert_status,label,ip FROM cameras WHERE deleted=0 AND status IN ('online','offline') ORDER BY updated_at DESC LIMIT 5000").fetchall()
            for r in rows:
                old=str(r['last_alert_status'] or '')
                new=str(r['status'] or '')
                if new in ('online','offline') and old != new:
                    msg=f"📡 *Camera status changed*\\nCamera: `{r['label'] or r['ip'] or r['id']}`\\nStatus: `{old or 'unknown'} → {new}`\\nTime: `{utc_iso('seconds')}`"
                    _v40_alert('status_change', r['id'], old, new, msg)
                    def _write_status():
                        c2 = _db_conn()
                        c2.execute(
                            'UPDATE cameras SET last_alert_status=?,last_alert_at=? WHERE id=?',
                            (new, utc_iso(), r['id'])
                        )
                        c2.commit()
                    _db_retry_write(_write_status, 'health supervisor status write')
            try:
                c.rollback()
            except Exception:
                pass
        except sqlite3.OperationalError as exc:
            if 'locked' in str(exc).lower() or 'busy' in str(exc).lower():
                log.warning('[SQLite] health supervisor deferred a locked write; it will retry on the next health cycle')
            else:
                log.exception('v40 health supervisor error')
        except Exception as exc:
            if _shutdown_requested.is_set():
                log.debug('v40 health supervisor exiting during shutdown: %s', exc)
                return
            log.exception('v40 health supervisor error')
        _shutdown_requested.wait(max(15,ADVANCED_HEALTH_INTERVAL))

def _v40_schedule_supervisor():
    _touch_worker('schedule-supervisor','starting')
    while not _shutdown_requested.is_set():
        if not _service_enabled('schedule-supervisor'):
            _touch_worker('schedule-supervisor','stopped','disabled by operator'); return
        try:
            c=_db_conn(); now=utc_now()
            rows=c.execute("SELECT * FROM scheduled_jobs WHERE enabled=1 AND (next_run IS NULL OR next_run<=datetime('now'))").fetchall()
            for r in rows:
                job=str(r['job_type'] or '')
                if job in ('maintenance','cleanup'): _v40_maintenance(); db_create_backup()
                elif job in ('health_check','recheck'): 
                    cams=db_get_resume_batch(min(200,_validation_worker_config()[1]))
                    if cams and not val_get_progress()['running']: threading.Thread(target=run_validation_cycle,args=(cams,),kwargs={'max_workers':_validation_worker_config()[0],'scope':'scheduled'},daemon=True).start()
                elif job in ('daily_report','health_report'):
                    a=_v40_analytics(); _send_telegram('📊 *Camera Dashboard Health Report*\n'+_v40_json(a))
                interval=max(3600,int(r['interval_seconds'] or 21600)); c.execute("UPDATE scheduled_jobs SET last_run=?,next_run=datetime('now', '+' || ? || ' seconds') WHERE id=?",(utc_iso(),interval,r['id']))
            c.commit(); _touch_worker('schedule-supervisor','idle',f'{len(rows)} jobs processed')
        except Exception: log.exception('v40 schedule supervisor error')
        _shutdown_requested.wait(60)

def start_v40_health_supervisor():
    if not _service_enabled('health-supervisor'):
        _touch_worker('health-supervisor','stopped','disabled by operator'); return threading.current_thread()
    t=threading.Thread(target=_v40_health_supervisor,daemon=True,name='v40-health-supervisor'); t.start(); return t

def start_v40_schedule_supervisor():
    if not _service_enabled('schedule-supervisor'):
        _touch_worker('schedule-supervisor','stopped','disabled by operator'); return threading.current_thread()
    t=threading.Thread(target=_v40_schedule_supervisor,daemon=True,name='v40-schedule-supervisor'); t.start(); return t

@app.route('/api/v42/features')
def v42_features():
    groups={
      'live_view':['1/4/8/16/25/36 layouts','custom rows/columns','drag/drop tile ordering','fullscreen tile','per-tile snapshot','per-tile stop','auto reconnect','live status','FPS/resolution','max-feed protection','saved layouts'],
      'device_intelligence':['Hikvision/Dahua/Uniview/generic detection','NVR/DVR/IP/door/standalone classification','channel enumeration','device fingerprint','evidence history','confidence score','duplicate detection/merge','device timeline'],
      'stream':['RTSP validation','candidate paths','main/sub stream candidates','HTTP/HTTPS media detection','M3U/M3U8 support','stream observations','quality score','latency tracking','reconnect'],
      'health':['online/offline/checking states','health history','diagnostic history','device change history','response latency','quality analytics','status-change alerts','health export'],
      'database':['SQLite WAL','migrations','integrity check','rotating verified backups','restore safety backup','trash/restore','audit log','retention maintenance','shutdown flush'],
      'organization':['categories','category metadata','favorites','bulk select','bulk transfer','bulk delete','tags','saved filters','saved layouts'],
      'export':['CSV','JSON','M3U','health JSON','device report','diagnostic results'],
      'automation':['adaptive workers','validation resume','scheduled jobs','maintenance','health checks','Telegram alert framework'],
      'security':['password masking in exports','read-only diagnostics','bounded user-supplied host probing','audit trail']
    }
    return jsonify({'ok':True,'version':VERSION,'features':groups})




# ══════════════════════════════════════════════════════════════════════════════
# v43 COMPLETE FEATURE SUITE
# Implements the full Priority 1–16 feature specification as one integrated
# standalone layer. Existing v37-v42 APIs remain available.
# ══════════════════════════════════════════════════════════════════════════════

# Current Priority 1–16 feature matrix.
# The /api/v43/features route is retained for backward compatibility, but the
# matrix itself is always tied to the running application VERSION.
V43_FEATURE_VERSION = VERSION

V43_PRIORITIES = {
    'P1_live_view': [
        '1/4/8/16/25/36 camera layouts','custom rows/columns','drag/drop tiles',
        'fullscreen tile','per-tile snapshot','per-tile stop','auto reconnect',
        'LIVE/OFFLINE/RECONNECTING state','FPS/resolution/latency display',
        'maximum-feed protection','saved layouts','camera selection for multi-view',
        'Stop All feeds','live-feed counter','WebSocket live streams',
    ],
    'P2_device_intelligence': [
        'Hikvision/Dahua/Uniview detection','generic ONVIF detection',
        'NVR/DVR/IP-camera/door-camera classification','channel/profile enumeration',
        'codec/resolution/model/firmware evidence','HTTP/HTTPS/RTSP service detection',
        'device confidence/evidence','device fingerprint/history','device timeline',
        'same-device multi-port correlation','duplicate detection/merge',
    ],
    'P3_stream_detection': [
        'RTSP path discovery','vendor RTSP candidates','main/sub-stream candidates',
        'credential validation with supplied credentials','anonymous RTSP support',
        'HTTP MJPEG/HLS/M3U/M3U8 detection','working candidate promotion',
        'candidate scoring/ranking','stream quality observations','RTSP reconnect/backoff',
        'standalone RTSP/M3U stream support','embedded RTSP credential extraction',
    ],
    'P4_health_monitoring': [
        'online/offline/checking/unknown states','online/offline history',
        'last successful/failed check','consecutive failures','response latency',
        'RTSP latency','packet-loss observation','uptime statistics','category health',
        'dashboard health','health history and scoring','RTSP quality analytics',
        'FPS/bitrate/latency observations',
    ],
    'P5_automatic_recovery': [
        'RTSP auto-reconnect','HTTP retry','device recheck','exponential backoff',
        'duplicate-check suppression','validation resume','restart-safe persistence',
        'SQLite progress','validation queue lease/claim handling','stale-claim cleanup',
        'retry state/backoff','retry exhausted handling','queue reprioritization',
        'failed-camera retry','worker restart/recovery controls',
    ],
    'P6_database_safety': [
        'SQLite WAL','schema migrations','schema-drift detection','integrity checks',
        'rotating verified backups','backup/restore safety copy',
        'transactional bulk operations','duplicate protection',
        'credential-change/device-change history','audit log','retention maintenance',
        'shutdown persistence barrier','database lock/retry protection',
        'recovery history and operational metrics',
    ],
    'P7_organization': [
        'category rename/merge/delete','category lock/metadata','bulk transfer/delete',
        'bulk favorite/recheck/export','favorites','tags','saved filters',
        'advanced search','IP/vendor/channel/status/country search',
        'category Select All','destination-category transfer','delete entire category',
    ],
    'P8_geoip': [
        'country/region/city','ISP/ASN when provider supplies it','geo confidence',
        'geo cache','country grouping','geo-aware search/export','Geo Coverage API',
        'country/region/city aggregation','camera map/location data',
    ],
    'P9_snapshots': [
        'single snapshot','bulk snapshots','timestamp naming',
        'camera/IP/channel metadata','snapshot history','quality score',
        'retention cleanup','latest/previous comparison metadata',
        'Telegram snapshot alerts','snapshot failure reporting',
    ],
    'P10_dashboard': [
        'total/online/offline/checking/unknown','RTSP working/failing',
        'NVR/DVR/IP/door/M3U counts','country/vendor/channel analytics',
        'health charts/data','live-feed counter','validation progress',
        'real-time worker status','real-time queue status','validation speed',
        'validation ETA','adaptive worker count','CPU/RAM status',
        'database integrity/size status','Advanced Observability section',
    ],
    'P11_diagnostics': [
        'full diagnostic','TCP/HTTP/HTTPS/RTSP tests','authentication result',
        'device info','channel test','snapshot test','stream test',
        'latency and failure classification','diagnostic report',
        'startup self-test','operational diagnostics','route inventory',
        'configuration health report','queue health metrics',
    ],
    'P12_alerts': [
        'offline alert','back-online alert','RTSP failure alert',
        'authentication alert','device-change alert','channel-change alert',
        'cooldown','deduplication','alert history','daily report',
        'Telegram startup notification','Telegram snapshot alert',
        'worker/recovery/security alert framework',
    ],
    'P13_security': [
        'admin/read-only/custom roles','permission-based dashboard controls',
        'password masking','secret-safe exports','session hardening',
        'audit trail','bounded user-supplied-host diagnostics',
        'no public-range scanning','active session management',
        'individual session revocation','Logout All','lockout management',
        'concurrent-session limit','force password change','password-age policy',
        'security/configuration health report',
    ],
    'P14_performance': [
        'adaptive worker count','adaptive queue','CPU/RAM monitoring',
        'per-operation timeouts','connection reuse','result caching',
        'background processing','non-blocking UI','large inventory handling',
        'worker supervisor','automatic worker restart','restart cooldown',
        'restart budget/anti-restart-storm protection','queue backpressure',
        'resource-aware validation batching','graceful shutdown',
    ],
    'P15_import_export': [
        'TXT/CSV/M3U/M3U8/RTSP import','combined TXT generation',
        'combined M3U generation','CSV/JSON/M3U export','health export',
        'device report','diagnostic export','category export','selected-camera export',
        'safe credential redaction','camera list export','recheck imported files',
    ],
    'P16_smart_features': [
        'automatic device classification','duplicate feed detection',
        'same-device multi-port correlation','channel numbering patterns',
        'best RTSP candidate selection','main/sub selection','change detection',
        'fingerprint/evidence timeline','confidence scoring',
        'validation priority scheduling','favorite/new/unknown priority',
        'stream quality scoring','Geo Coverage analytics','security observability',
        'recovery history','operational metrics',
    ],
}


def _v43_ensure_schema():
    """Create/upgrade every v43 persistence structure without resetting data.

    The schema marker lets already-migrated databases skip the large migration
    pass on every boot. Missing/old markers still run the full compatibility
    migration path.
    """
    c = _db_conn()
    # V160 additive location metadata migration. Keep existing camera rows and
    # all current schema/data intact; these columns are nullable/defaulted.
    _camera_cols = {r[1] for r in c.execute('PRAGMA table_info(cameras)').fetchall()}
    for _name, _definition in (
        ('description', "TEXT NOT NULL DEFAULT ''"),
        ('area', "TEXT NOT NULL DEFAULT ''"),
        ('latitude', 'REAL'),
        ('longitude', 'REAL'),
        ('location_updated_at', 'TEXT'),
    ):
        if _name not in _camera_cols:
            c.execute(f'ALTER TABLE cameras ADD COLUMN {_name} {_definition}')
    c.commit()
    try:
        marker = c.execute('SELECT version FROM schema_version WHERE id=1').fetchone()
        if marker and int(marker['version'] if hasattr(marker, 'keys') else marker[0]) >= SCHEMA_VERSION:
            required = {r[1] for r in c.execute('PRAGMA table_info(cameras)').fetchall()}
            required_cols = {'rtsp_port','rtsp_score','failure_reason','device_id','channel_index','description','area','latitude','longitude','location_updated_at'}
            required_tables = {'v43_health_samples','v43_camera_ports','v43_stream_candidates','v43_snapshot_history','v43_geo_cache','v43_feature_runs','v43_user_roles','v43_auth_users','v43_auth_sessions','v43_login_lockouts','v43_auth_audit','v43_user_access_scope','api_keys','app_retention','inventory_jobs','device_evidence','device_events'}
            existing_tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if required_cols.issubset(required) and required_tables.issubset(existing_tables):
                log.info('Database schema marker %s — migration pass skipped (required tables verified)', marker['version'] if hasattr(marker, 'keys') else marker[0])
                return
    except sqlite3.Error:
        pass
    # Health compatibility for old v40/v41 databases.
    for table, name, definition in [
        ('camera_health_history','rtsp_score','REAL NOT NULL DEFAULT 0.0'),
        ('camera_health_history','candidate_score','REAL NOT NULL DEFAULT 0.0'),
        ('camera_health_history','failure_reason','TEXT NOT NULL DEFAULT ""'),
        ('camera_health_history','transport','TEXT NOT NULL DEFAULT ""'),
        ('camera_health_history','first_frame','INTEGER NOT NULL DEFAULT 0'),
        ('camera_health_history','quality_score','REAL NOT NULL DEFAULT 0.0'),
        ('camera_health_history','checked_at','TEXT'),
    ]:
        try:
            cols={r[1] for r in c.execute(f'PRAGMA table_info({table})').fetchall()}
            if name not in cols:
                c.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
        except sqlite3.Error:
            pass
    c.executescript('''
        CREATE TABLE IF NOT EXISTS v43_health_samples(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            camera_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'unknown',
            latency_ms REAL NOT NULL DEFAULT 0,
            packet_loss REAL NOT NULL DEFAULT 0,
            rtsp_ok INTEGER NOT NULL DEFAULT 0,
            http_ok INTEGER NOT NULL DEFAULT 0,
            snapshot_ok INTEGER NOT NULL DEFAULT 0,
            quality_score REAL NOT NULL DEFAULT 0,
            failure_reason TEXT NOT NULL DEFAULT '',
            checked_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_v43_health_cam_time ON v43_health_samples(camera_id,checked_at);
        CREATE TABLE IF NOT EXISTS v43_camera_ports(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            camera_id TEXT NOT NULL,
            port INTEGER NOT NULL,
            protocol TEXT NOT NULL DEFAULT 'tcp',
            reachable INTEGER NOT NULL DEFAULT 0,
            latency_ms REAL NOT NULL DEFAULT 0,
            service TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            server TEXT NOT NULL DEFAULT '',
            checked_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(camera_id,port,protocol)
        );
        CREATE TABLE IF NOT EXISTS v43_stream_candidates(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            camera_id TEXT NOT NULL,
            url TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'rtsp',
            stream_role TEXT NOT NULL DEFAULT 'unknown',
            score REAL NOT NULL DEFAULT 0,
            working INTEGER NOT NULL DEFAULT 0,
            codec TEXT NOT NULL DEFAULT '',
            resolution TEXT NOT NULL DEFAULT '',
            fps REAL NOT NULL DEFAULT 0,
            latency_ms REAL NOT NULL DEFAULT 0,
            failure_reason TEXT NOT NULL DEFAULT '',
            checked_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(camera_id,url)
        );
        CREATE INDEX IF NOT EXISTS idx_v43_candidates_cam ON v43_stream_candidates(camera_id,score DESC);
        CREATE TABLE IF NOT EXISTS v43_snapshot_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            camera_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            width INTEGER NOT NULL DEFAULT 0,
            height INTEGER NOT NULL DEFAULT 0,
            bytes INTEGER NOT NULL DEFAULT 0,
            quality_score REAL NOT NULL DEFAULT 0,
            captured_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_v43_snapshot_cam ON v43_snapshot_history(camera_id,captured_at);
        CREATE TABLE IF NOT EXISTS v43_geo_cache(
            ip TEXT PRIMARY KEY,
            country TEXT NOT NULL DEFAULT '',
            region TEXT NOT NULL DEFAULT '',
            city TEXT NOT NULL DEFAULT '',
            isp TEXT NOT NULL DEFAULT '',
            asn TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            confidence TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS v43_feature_runs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feature TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            started_at TEXT NOT NULL DEFAULT (datetime('now')),
            finished_at TEXT,
            detail TEXT NOT NULL DEFAULT '',
            result_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS v43_user_roles(
            username TEXT PRIMARY KEY,
            role TEXT NOT NULL DEFAULT 'readonly',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS v43_auth_users(
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT 'custom',
            permissions_json TEXT NOT NULL DEFAULT '[]',
            enabled INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL DEFAULT 'database',
            auth_version INTEGER NOT NULL DEFAULT 1,
            force_password_change INTEGER NOT NULL DEFAULT 0,
            password_changed_at TEXT,
            last_login_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS v43_auth_sessions(
            session_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'custom',
            client_ip TEXT NOT NULL DEFAULT '',
            user_agent TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_seen TEXT NOT NULL DEFAULT (datetime('now')),
            revoked INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_v43_auth_sessions_user ON v43_auth_sessions(username,last_seen);
        CREATE TABLE IF NOT EXISTS v43_login_lockouts(
            username TEXT NOT NULL,
            client_ip TEXT NOT NULL,
            failures INTEGER NOT NULL DEFAULT 0,
            first_attempt TEXT NOT NULL DEFAULT (datetime('now')),
            locked_until TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY(username,client_ip)
        );
        CREATE TABLE IF NOT EXISTS v43_alert_dedup(
            fingerprint TEXT PRIMARY KEY,
            last_sent TEXT NOT NULL DEFAULT (datetime('now')),
            count INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS v43_layout_camera_state(
            layout_id INTEGER NOT NULL,
            camera_id TEXT NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(layout_id,camera_id)
        );
        CREATE TABLE IF NOT EXISTS v43_import_export_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation TEXT NOT NULL,
            filename TEXT NOT NULL DEFAULT '',
            camera_count INTEGER NOT NULL DEFAULT 0,
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    ''')
    # Seed environment accounts only when they do not already exist. Once an
    # administrator changes an environment user's password or disables that user,
    # the DB state must survive restarts instead of being overwritten on every boot.
    try:
        for _u,_r,_p in ((AUTH_USER,'admin',AUTH_PERMISSION_DEFS.keys()), (READONLY_USER,'readonly',AUTH_ROLE_DEFAULTS['readonly'])):
            if _u:
                _perms=list(_p)
                c.execute("INSERT OR IGNORE INTO v43_auth_users(username,password_hash,role,permissions_json,enabled,source) VALUES(?,?,?,?,1,'environment')", (_u,'',_r,json.dumps(_perms)))
                if _r == 'readonly':
                    c.execute("UPDATE v43_auth_users SET role='readonly',permissions_json=? WHERE username=? AND source='environment'", (json.dumps(_perms), _u))
                c.execute("INSERT INTO v43_user_roles(username,role,enabled) VALUES(?,?,1) ON CONFLICT(username) DO UPDATE SET role=excluded.role", (_u,_r))
    except sqlite3.Error:
        log.exception('auth user seed failed')
    # Seed standard alert rules once. A marker prevents an administrator's
    # renamed/de-tuned rules from being normalized on every restart.
    try:
        seeded = c.execute("SELECT value FROM app_settings WHERE key='v43.alert_seeded'").fetchone()
        if not seeded or str(seeded['value'] or '') != '1':
            for _name,_event in [('Offline alert','offline'),('Back online alert','back_online'),('RTSP failure alert','rtsp_failure'),('Authentication failure alert','auth_failure'),('Device change alert','device_change'),('Channel change alert','channel_change')]:
                c.execute("INSERT OR IGNORE INTO alert_rules(name,event_type,enabled,cooldown_seconds,config_json) VALUES(?,?,1,300,'{}')",(_name,_event))
            c.execute("INSERT INTO app_settings(key,value,updated_at) VALUES('v43.alert_seeded','1',datetime('now')) ON CONFLICT(key) DO UPDATE SET value='1',updated_at=datetime('now')")
            c.commit()
    except sqlite3.Error:
        pass
    # Normalize legacy health score naming.
    try:
        c.execute("UPDATE camera_health_history SET rtsp_score=COALESCE(NULLIF(rtsp_score,0),candidate_score,0)")
    except sqlite3.Error:
        pass
    c.commit()


def _v43_safe_health_rows(camera_id='', limit=5000):
    """Schema-safe health retrieval for databases created by v31-v42."""
    c=_db_conn(); limit=max(1,min(10000,int(limit or 5000)))
    cols={r[1] for r in c.execute('PRAGMA table_info(camera_health_history)').fetchall()}
    wanted=['status','latency_ms','codec','resolution','fps','checked_at','failure_reason','transport','first_frame','quality_score']
    score='rtsp_score' if 'rtsp_score' in cols else ('candidate_score' if 'candidate_score' in cols else '0 AS rtsp_score')
    select=', '.join([x for x in wanted if x in cols] + [score + (' AS rtsp_score' if score in {'rtsp_score','candidate_score'} else '')])
    if 'id' in cols: select='id,'+select
    if 'camera_id' in cols: select='camera_id,'+select
    if 'id' not in cols or 'camera_id' not in cols:
        return []
    sql=f'SELECT {select} FROM camera_health_history'
    args=[]
    if camera_id:
        sql+=' WHERE camera_id=?'; args.append(camera_id)
    sql+=' ORDER BY id DESC LIMIT ?'; args.append(limit)
    return [dict(r) for r in c.execute(sql,args).fetchall()]


def _v43_ping_host(host, timeout=1.5):
    """Ping one already-known/user-supplied camera host and measure loss."""
    if not host: return {'ok':False,'detail':'empty host','packet_loss':100.0}
    if re.search(r'[^0-9a-fA-F:.]', host): return {'ok':False,'detail':'unsupported host format','packet_loss':100.0}
    count=3
    ping_bin=shutil.which('ping')
    if not ping_bin:
        return {'ok':False,'packet_loss':100.0,'detail':'ping executable unavailable'}
    cmd=[ping_bin,'-c',str(count),'-W',str(max(1,int(timeout)))] if os.name!='nt' else [ping_bin,'-n',str(count),'-w',str(int(timeout*1000)),host]
    if os.name!='nt': cmd.append(host)
    try:
        t=time.monotonic(); r=subprocess.run(cmd,capture_output=True,timeout=timeout*count+2,text=True); ms=(time.monotonic()-t)*1000
        output=(r.stdout or r.stderr)
        loss=100.0
        m=re.search(r'(\d+(?:\.\d+)?)%\s*(?:packet )?loss',output,re.I)
        if m: loss=float(m.group(1))
        else:
            m=re.search(r'Lost\s*=\s*(\d+)',output,re.I)
            if m: loss=100.0*int(m.group(1))/count
            elif r.returncode==0: loss=0.0
        return {'ok':r.returncode==0,'latency_ms':round(ms/count,1),'packet_loss':round(loss,1),'packets':count,'output':output[-500:]}
    except Exception as e: return {'ok':False,'packet_loss':100.0,'detail':type(e).__name__}


def _v43_tcp_check(host, port, timeout=1.0):
    t=time.monotonic()
    try:
        with socket.create_connection((host,int(port)),timeout=timeout):
            return {'reachable':True,'latency_ms':round((time.monotonic()-t)*1000,1)}
    except Exception as e:
        return {'reachable':False,'latency_ms':round((time.monotonic()-t)*1000,1),'error':type(e).__name__}


def _v43_http_check(url, username='', password='', timeout=2.0):
    if _requests is None: return {'ok':False,'detail':'requests unavailable'}
    try:
        auth=(username,password) if username else None
        t=time.monotonic(); r=_requests.get(url,auth=auth,timeout=timeout,verify=_V43_HTTP_VERIFY_TLS,allow_redirects=True,headers={'User-Agent':'CameraDashboard/43'})
        body=(r.text or '')[:20000]
        low=body.lower()
        media=[x for x in ('m3u8','mjpeg','flv','mp4','streamurl','playurl','videourl','liveurl','hlsurl') if x in low]
        return {'ok':r.status_code<400,'status_code':r.status_code,'latency_ms':round((time.monotonic()-t)*1000,1),'title':re.search(r'<title[^>]*>(.*?)</title>',body,re.I|re.S).group(1).strip()[:200] if re.search(r'<title[^>]*>(.*?)</title>',body,re.I|re.S) else '','server':r.headers.get('Server',''),'media_hints':media,'url':r.url}
    except Exception as e: return {'ok':False,'detail':type(e).__name__+': '+str(e)[:180]}


def _v43_camera_diagnostic(cam):
    deadline=time.monotonic()+float(os.getenv('CAMERA_DIAGNOSTIC_MAX_SECONDS','45'))
    host=str(cam.get('ip') or '').strip(); port=int(cam.get('port') or 0); rtsp_port=int(cam.get('rtsp_port') or 554)
    result={'id':cam.get('id'),'label':cam.get('label'),'ip':host,'started_at':utc_iso(),'checks':{},'vendor':_v40_camera_vendor(cam),'device_class':_v40_device_class(cam)}
    result['checks']['ping']={}
    ports=[]
    for pnum in dict.fromkeys([port,80,443,8080,rtsp_port,554]):
        if time.monotonic() >= deadline: break
        if not host or not pnum: continue
        ports.append({'port':pnum,**_v43_tcp_check(host,pnum,min(1.0,max(0.1,deadline-time.monotonic())))})
    result['checks']['ports']=ports
    web=[]
    for pnum in dict.fromkeys([port,80,443,8080,8443]):
        if time.monotonic() >= deadline: break
        if not host or not pnum: continue
        scheme='https' if pnum in DEVICE_HTTPS_PORTS else 'http'
        web.append({'port':pnum,**_v43_http_check(f'{scheme}://{f"[{host}]" if ":" in host and not host.startswith("[") else host}:{pnum}/',cam.get('username',''),cam.get('password',''),timeout=min(2.0,max(0.1,deadline-time.monotonic())))})
    result['checks']['web']=web
    urls=[]
    try: urls=json.loads(cam.get('rtsp_urls') or '[]') if isinstance(cam.get('rtsp_urls'),str) else list(cam.get('rtsp_urls') or [])
    except Exception: urls=[]
    result['checks']['rtsp_candidates']=[]
    for u in urls[:12]:
        if time.monotonic() >= deadline: break
        # Safe reachability only; actual authentication/FFmpeg validation stays in existing validator.
        parsed=urlsplit(str(u)); hp=parsed.port or rtsp_port
        result['checks']['rtsp_candidates'].append({'url':re.sub(r'(?<=//)[^/@]+@','***@',str(u)),'port':hp,**_v43_tcp_check(parsed.hostname or host,hp,min(1.0,max(0.1,deadline-time.monotonic())))})
    if time.monotonic() < deadline:
        result['checks']['ping']=_v43_ping_host(host, timeout=min(1.0,max(0.1,deadline-time.monotonic())))
    else:
        result['checks']['ping']={'ok':False,'packet_loss':100.0,'detail':'diagnostic deadline reached'}
    result['checks']['health']={'status':cam.get('status'),'latency_ms':cam.get('latency_ms'),'rtsp_score':cam.get('rtsp_score'),'failure_reason':cam.get('failure_reason')}
    result['finished_at']=utc_iso()
    return result


def _v43_record_health(cam_id, diagnostic=None):
    cam=db_get_camera(cam_id)
    if not cam: return None
    d=diagnostic or _v43_camera_diagnostic(cam)
    ping=d.get('checks',{}).get('ping',{})
    ports=d.get('checks',{}).get('ports',[])
    tcp_ok=any(x.get('reachable') for x in ports)
    rtsp_ok=float(cam.get('rtsp_score') or 0)>0 or str(cam.get('status')).lower()=='online'
    latency=float(cam.get('latency_ms') or 0)
    q=_v40_quality_score(cam)
    failure=str(cam.get('failure_reason') or '')
    packet_loss=float(ping.get('packet_loss') or 0)
    last_sample = None
    try:
        last_sample = _db_conn().execute('SELECT status,latency_ms,packet_loss,rtsp_ok,http_ok,quality_score,failure_reason,checked_at FROM v43_health_samples WHERE camera_id=? ORDER BY id DESC LIMIT 1',(cam_id,)).fetchone()
    except Exception:
        pass
    state_changed = (not last_sample or str(last_sample['status']) != str(cam.get('status','unknown')) or
                     int(last_sample['rtsp_ok'] or 0) != int(rtsp_ok) or
                     int(last_sample['http_ok'] or 0) != int(tcp_ok) or
                     str(last_sample['failure_reason'] or '') != failure or
                     abs(float(last_sample['quality_score'] or 0) - q) >= 5.0)
    if state_changed:
        def _write_health_sample():
            c2=_db_conn(); c2.execute('INSERT INTO v43_health_samples(camera_id,status,latency_ms,packet_loss,rtsp_ok,http_ok,snapshot_ok,quality_score,failure_reason) VALUES(?,?,?,?,?,?,?,?,?)',(cam_id,cam.get('status','unknown'),latency,packet_loss,int(rtsp_ok),int(tcp_ok),0,q,failure)); c2.commit()
        _db_retry_write(_write_health_sample, 'v43 health sample')
    status=str(cam.get('status') or 'unknown').lower()
    if status=='offline': _v40_alert('offline',cam_id,'online','offline',f'📡 Camera OFFLINE: {cam.get("label") or cam.get("ip") or cam_id}')
    elif status=='online': _v40_alert('back_online',cam_id,'offline','online',f'✅ Camera BACK ONLINE: {cam.get("label") or cam.get("ip") or cam_id}')
    if not rtsp_ok and status=='offline': _v40_alert('rtsp_failure',cam_id,status,status,f'📺 RTSP failure: {cam.get("label") or cam.get("ip") or cam_id}')
    if any(x in failure.lower() for x in ('credential','authentication','unauthorized','401')): _v40_alert('auth_failure',cam_id,status,status,f'🔐 Authentication failure: {cam.get("label") or cam.get("ip") or cam_id}')
    try:
        recent=_db_conn().execute('SELECT field_name,old_value,new_value,created_at FROM camera_change_history WHERE camera_id=? ORDER BY id DESC LIMIT 6',(cam_id,)).fetchall()
        for ch in recent:
            field=str(ch['field_name'] or '').lower()
            if field in ('device_id','camera_type','web_url','port','rtsp_port'):
                _v40_alert('device_change',cam_id,str(ch['old_value']),str(ch['new_value']),f'🔄 Device change: {cam.get("label") or cam.get("ip") or cam_id} — {field}: {ch["old_value"]} → {ch["new_value"]}')
            elif field in ('channel_index','channel_name','channel_count'):
                _v40_alert('channel_change',cam_id,str(ch['old_value']),str(ch['new_value']),f'📺 Channel change: {cam.get("label") or cam.get("ip") or cam_id} — {field}: {ch["old_value"]} → {ch["new_value"]}')
    except Exception:
        pass
    return {'camera_id':cam_id,'status':cam.get('status'),'latency_ms':latency,'packet_loss':packet_loss,'rtsp_ok':rtsp_ok,'tcp_ok':tcp_ok,'quality_score':q,'ping_ok':bool(ping.get('ok'))}


def _v43_snapshot_record(cam_id, filename, quality=0, width=0, height=0, bytes_count=0):
    try:
        def _write():
            c=_db_conn(); c.execute('INSERT INTO v43_snapshot_history(camera_id,filename,width,height,bytes,quality_score) VALUES(?,?,?,?,?,?)',(cam_id,filename,int(width or 0),int(height or 0),int(bytes_count or 0),float(quality or 0))); c.commit()
        _db_retry_write(_write, 'feature snapshot history')
    except Exception: log.exception('snapshot history record failed')


def _v43_redact_camera(row):
    d=dict(row)
    d.pop('password',None); d.pop('rtsp_urls',None)
    for key in ('web_url','source_file'):
        if d.get(key): d[key]=re.sub(r'(?i)((?:rtsp|https?)://)[^\s/@]+:[^\s/@]+@', r'\1***:***@', str(d[key]))
    if d.get('username'): d['username']='***'
    return d


def _v43_m3u(rows):
    out=['#EXTM3U']
    for r in rows:
        urls=[]
        try: urls=json.loads(r.get('rtsp_urls') or '[]') if isinstance(r.get('rtsp_urls'),str) else list(r.get('rtsp_urls') or [])
        except Exception: urls=[]
        if not urls: continue
        for u in urls[:1]:
            out.append(f'#EXTINF:-1,{r.get("label") or r.get("ip") or r.get("id")}')
            out.append(str(u))
    return '\n'.join(out)+'\n'


def _v43_table_count(c, table):
    try:
        return int(c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0])
    except Exception:
        return 0

def _v43_feature_status():
    c=_db_conn()
    try: total=int(c.execute('SELECT COUNT(*) FROM cameras WHERE deleted=0').fetchone()[0])
    except Exception: total=0
    return {'version':V43_FEATURE_VERSION,'priorities':V43_PRIORITIES,'counts':{
        'cameras':total,'health_samples':_v43_table_count(c,'v43_health_samples'),
        'snapshots':_v43_table_count(c,'v43_snapshot_history'),
        'stream_candidates':_v43_table_count(c,'v43_stream_candidates'),
        'ports':_v43_table_count(c,'v43_camera_ports')}}


@app.route('/api/v43/features')
def v43_features():
    return jsonify({'ok':True,'suite':_v43_feature_status()})


@app.route('/api/v43/health')
def v43_health():
    cid=str(request.args.get('camera_id') or '')
    return jsonify({'ok':True,'analytics':_v40_analytics(),'history':_v43_safe_health_rows(cid,min(5000,int(request.args.get('limit',500))))})


@app.route('/api/v43/health/sample',methods=['POST'])
def v43_health_sample():
    d=request.get_json(silent=True) or {}; ids=[str(x) for x in (d.get('ids') or [])][:100]
    if not ids: ids=[str(x['id']) for x in _db_conn().execute("SELECT id FROM cameras WHERE deleted=0 ORDER BY updated_at DESC LIMIT 50").fetchall()]
    results=[]
    for cid in ids:
        try: results.append(_v43_record_health(cid))
        except Exception as e: results.append({'camera_id':cid,'ok':False,'detail':str(e)})
    return jsonify({'ok':True,'results':results})


_V43_DIAG_JOBS_LOCK=threading.RLock()
_V43_DIAG_JOBS={}

def _run_v43_diagnostic_job(job_id, ids):
    with _V43_DIAG_JOBS_LOCK: _V43_DIAG_JOBS[job_id]={'status':'running','results':[],'started_at':utc_iso()}
    results=[]
    for cid in ids:
        cam=db_get_camera(cid)
        if not cam: results.append({'id':cid,'ok':False,'detail':'Camera not found'}); continue
        try:
            r=_v43_camera_diagnostic(cam); _v40_write_history(cid,r); _v43_record_health(cid,r); results.append({'ok':True,**r})
        except Exception as e: results.append({'id':cid,'ok':False,'detail':type(e).__name__+': '+str(e)})
        with _V43_DIAG_JOBS_LOCK: _V43_DIAG_JOBS[job_id]['results']=list(results)
    _advanced_audit('full_diagnostic','camera',','.join(ids),f'{sum(bool(x.get("ok")) for x in results)}/{len(results)}')
    with _V43_DIAG_JOBS_LOCK: _V43_DIAG_JOBS[job_id].update(status='completed',finished_at=utc_iso())

@app.route('/api/v43/diagnostic/full',methods=['POST'])
def v43_full_diagnostic():
    d=request.get_json(silent=True) or {}; ids=[str(x) for x in (d.get('ids') or [])][:50]
    if not ids: return jsonify({'ok':False,'detail':'No camera IDs supplied'}),400
    job_id=uuid.uuid4().hex
    with _V43_DIAG_JOBS_LOCK: _V43_DIAG_JOBS[job_id]={'status':'queued','results':[],'started_at':utc_iso()}
    threading.Thread(target=_run_v43_diagnostic_job,args=(job_id,ids),daemon=True,name=f'v43-diag-{job_id[:8]}').start()
    return jsonify({'ok':True,'queued':True,'job_id':job_id,'status_url':f'/api/v43/diagnostic/full/{job_id}'}),202

@app.route('/api/v43/diagnostic/full/<job_id>')
def v43_full_diagnostic_status(job_id):
    with _V43_DIAG_JOBS_LOCK: item=_V43_DIAG_JOBS.get(str(job_id))
    if not item: return jsonify({'ok':False,'detail':'Diagnostic job not found'}),404
    return jsonify({'ok':True,'job_id':job_id,**item})


@app.route('/api/v43/ports/<cam_id>')
def v43_ports(cam_id):
    rows=_db_conn().execute('SELECT * FROM v43_camera_ports WHERE camera_id=? ORDER BY port',(cam_id,)).fetchall()
    return jsonify({'ok':True,'items':[dict(x) for x in rows]})


@app.route('/api/v43/candidates/<cam_id>')
def v43_candidates(cam_id):
    rows=_db_conn().execute('SELECT id,camera_id,kind,stream_role,score,working,codec,resolution,fps,latency_ms,failure_reason,checked_at FROM v43_stream_candidates WHERE camera_id=? ORDER BY score DESC,id DESC',(cam_id,)).fetchall()
    return jsonify({'ok':True,'items':[dict(x) for x in rows]})


@app.route('/api/v43/history/<cam_id>')
def v43_history(cam_id):
    c=_db_conn(); return jsonify({'ok':True,'health':_v43_safe_health_rows(cam_id,2000),'snapshots':[dict(x) for x in c.execute('SELECT * FROM v43_snapshot_history WHERE camera_id=? ORDER BY id DESC LIMIT 500',(cam_id,)).fetchall()],'changes':[dict(x) for x in c.execute('SELECT * FROM camera_change_history WHERE camera_id=? ORDER BY id DESC LIMIT 500',(cam_id,)).fetchall()],'alerts':[dict(x) for x in c.execute('SELECT * FROM camera_alert_history WHERE camera_id=? ORDER BY id DESC LIMIT 500',(cam_id,)).fetchall()]})


@app.route('/api/v43/analytics')
def v43_analytics():
    a=_v40_analytics(); c=_db_conn()
    classes={}
    for r in c.execute('SELECT camera_type,COUNT(*) n FROM cameras WHERE deleted=0 GROUP BY camera_type ORDER BY n DESC').fetchall(): classes[r['camera_type'] or 'Unknown']=r['n']
    return jsonify({'ok':True,'analytics':a,'device_classes':classes,'health_samples':int(c.execute('SELECT COUNT(*) FROM v43_health_samples').fetchone()[0])})


@app.route('/api/v43/diagnostic/export',methods=['POST'])
def v43_diagnostic_export():
    d=request.get_json(silent=True) or {}; ids=[str(x) for x in (d.get('ids') or [])][:100]; results=[]
    for cid in ids:
        cam=db_get_camera(cid)
        if cam:
            try: results.append(_v43_camera_diagnostic(cam))
            except Exception as e: results.append({'id':cid,'ok':False,'detail':str(e)})
    payload=json.dumps({'generated_at':utc_iso(),'results':results},ensure_ascii=False,indent=2)
    return Response(payload,mimetype='application/json',headers={'Content-Disposition':'attachment; filename=diagnostics_v43.json'})


@app.route('/api/v43/export',methods=['GET','POST'])
def v43_export():
    d=request.get_json(silent=True) or {}; fmt=str(request.args.get('format') or d.get('format') or 'json').lower(); ids=[str(x) for x in (d.get('ids') or [])][:5000]
    c=_db_conn()
    if ids:
        ph=','.join('?'*len(ids)); rows=_filter_camera_access([dict(x) for x in c.execute(f'SELECT * FROM cameras WHERE id IN ({ph}) ORDER BY group_name,id',ids).fetchall()])
    else: rows=_filter_camera_access([dict(x) for x in c.execute('SELECT * FROM cameras WHERE deleted=0 ORDER BY group_name,id').fetchall()])
    safe=[_v43_redact_camera(r) for r in rows]
    c.execute('INSERT INTO v43_import_export_history(operation,filename,camera_count,detail) VALUES(?,?,?,?)',('export',fmt,len(rows),'redacted')); c.commit()
    if fmt=='csv':
        fields=['id','label','ip','port','username','group_name','camera_type','status','country','region','city','codec','resolution','fps','latency_ms','rtsp_port','rtsp_score','last_checked','failure_reason']
        out=io.StringIO(); w=csv.DictWriter(out,fieldnames=fields,extrasaction='ignore'); w.writeheader(); [w.writerow(r) for r in safe]
        return Response(out.getvalue(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=cameras_v43.csv'})
    if fmt=='m3u':
        return Response(_v43_m3u(rows),mimetype='audio/x-mpegurl',headers={'Content-Disposition':'attachment; filename=cameras_v43.m3u'})
    if fmt=='health':
        return Response(json.dumps({'generated_at':utc_iso(),'analytics':_v40_analytics(),'history':_v43_safe_health_rows('',5000)},ensure_ascii=False,indent=2),mimetype='application/json',headers={'Content-Disposition':'attachment; filename=health_v43.json'})
    if fmt=='audit':
        rr=[dict(x) for x in c.execute('SELECT actor,action,object_type,object_id,detail,created_at FROM audit_log ORDER BY id DESC LIMIT 10000').fetchall()]
        return Response(json.dumps(rr,ensure_ascii=False,indent=2),mimetype='application/json',headers={'Content-Disposition':'attachment; filename=audit_v43.json'})
    return Response(json.dumps({'generated_at':utc_iso(),'cameras':safe},ensure_ascii=False,indent=2),mimetype='application/json',headers={'Content-Disposition':'attachment; filename=cameras_v43.json'})


@app.route('/api/v43/snapshots/<cam_id>')
def v43_snapshot_history(cam_id):
    rows=_db_conn().execute('SELECT * FROM v43_snapshot_history WHERE camera_id=? ORDER BY id DESC LIMIT 500',(cam_id,)).fetchall(); return jsonify({'ok':True,'items':[dict(x) for x in rows]})


@app.route('/api/v43/alerts',methods=['GET','POST','DELETE'])
def v43_alerts():
    c=_db_conn(); d=request.get_json(silent=True) or {}
    if request.method=='POST':
        name=str(d.get('name') or '').strip()[:100]; event=str(d.get('event_type') or 'status_change'); cooldown=max(30,int(d.get('cooldown_seconds',300))); enabled=int(bool(d.get('enabled',True)))
        if not name:return jsonify({'ok':False,'detail':'name required'}),400
        c.execute("INSERT INTO alert_rules(name,event_type,enabled,cooldown_seconds,config_json) VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET event_type=excluded.event_type,enabled=excluded.enabled,cooldown_seconds=excluded.cooldown_seconds,config_json=excluded.config_json",(name,event,enabled,cooldown,json.dumps(d.get('config') or {}))); c.commit(); _advanced_audit('alert_rule','alert',name,event)
    elif request.method=='DELETE': c.execute('DELETE FROM alert_rules WHERE id=?',(int(d.get('id') or 0),)); c.commit()
    return jsonify({'ok':True,'rules':[dict(x) for x in c.execute('SELECT * FROM alert_rules ORDER BY name').fetchall()]})


@app.route('/api/v43/alerts/test',methods=['POST'])
def v43_alert_test():
    d=request.get_json(silent=True) or {}; msg=str(d.get('message') or '📡 Camera Dashboard test alert'); ok,detail=_send_telegram(msg); return jsonify({'ok':bool(ok),'detail':detail})


@app.route('/api/v43/alerts/history')
def v43_alert_history():
    rows=_db_conn().execute('SELECT * FROM camera_alert_history ORDER BY id DESC LIMIT 2000').fetchall(); return jsonify({'ok':True,'items':[dict(x) for x in rows]})


@app.route('/api/v43/layouts',methods=['GET','POST','DELETE'])
def v43_layouts():
    c=_db_conn(); d=request.get_json(silent=True) or {}
    if request.method=='POST':
        name=str(d.get('name') or '').strip()[:100]; layout=d.get('layout') or {}
        if not name:return jsonify({'ok':False,'detail':'name required'}),400
        c.execute("INSERT INTO live_layouts(name,layout_json) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET layout_json=excluded.layout_json,updated_at=datetime('now')",(name,json.dumps(layout))); c.commit()
    elif request.method=='DELETE': c.execute('DELETE FROM live_layouts WHERE id=?',(int(d.get('id') or 0),)); c.commit()
    return jsonify({'ok':True,'items':[dict(x) for x in c.execute('SELECT * FROM live_layouts ORDER BY updated_at DESC').fetchall()]})


@app.route('/api/v43/settings',methods=['GET','POST','DELETE'])
def v43_settings():
    c=_db_conn(); d=request.get_json(silent=True) or {}
    if request.method=='POST':
        for k,v in (d.get('settings') or {}).items(): c.execute('INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=datetime(\'now\')',(str(k)[:100],json.dumps(v) if not isinstance(v,str) else v))
        c.commit()
    elif request.method=='DELETE': c.execute('DELETE FROM app_settings WHERE key=?',(str(d.get('key') or ''),)); c.commit()
    return jsonify({'ok':True,'settings':{r['key']:r['value'] for r in c.execute('SELECT key,value FROM app_settings ORDER BY key').fetchall()}})


@app.route('/api/v43/backup/status')
def v43_backup_status():
    items=[]
    for p in DB_BACKUPS:
        if os.path.exists(p): items.append({'name':os.path.basename(p),'size':os.path.getsize(p),'mtime':datetime.fromtimestamp(os.path.getmtime(p),timezone.utc).isoformat(),'integrity':_sqlite_integrity_ok(p)})
    return jsonify({'ok':True,'items':items,'database':{'path':DB_PATH,'integrity':_cached_read('live_integrity',60.0,lambda:_sqlite_integrity_ok(DB_PATH)),'size':os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0}})


@app.route('/api/v43/backup/restore',methods=['POST'])
def v43_backup_restore():
    d=request.get_json(silent=True) or {}; name=os.path.basename(str(d.get('name') or '')); allowed={os.path.basename(x):x for x in DB_BACKUPS}; src=allowed.get(name)
    if not src or not _sqlite_integrity_ok(src): return jsonify({'ok':False,'detail':'Backup not found or integrity check failed'}),400
    if not db_create_backup(): return jsonify({'ok':False,'detail':'Safety backup failed; restore aborted'}),500
    try:
        shutil.copy2(src,DB_PATH)
        for side in (DB_PATH+'-wal',DB_PATH+'-shm'):
            try: os.remove(side)
            except FileNotFoundError: pass
        ok=_sqlite_integrity_ok(DB_PATH)
        _advanced_audit('database_restore','database',name,'integrity='+str(ok))
        return jsonify({'ok':ok,'detail':'Database restored. Restart application to reopen thread-local connections safely.' if ok else 'Restored file failed integrity check'})
    except Exception as e: return jsonify({'ok':False,'detail':str(e)}),500


@app.route('/api/v43/maintenance',methods=['POST'])
def v43_maintenance():
    removed=_v40_maintenance(); backup=db_create_backup(); return jsonify({'ok':True,'removed':removed,'backup':backup,'integrity':_cached_read('live_integrity',60.0,lambda:_sqlite_integrity_ok(DB_PATH))})


@app.route('/api/v43/security')
def v43_security():
    now = time.time()
    with _login_attempts_lock:
        locked = sum(1 for x in _login_attempts.values() if x.get('locked_until', 0) > now)
        ip_locked = sum(1 for x in _login_ip_guard.values() if x.get('locked_until', 0) > now)
    try:
        c = _db_conn()
        locked = max(locked, int(c.execute("SELECT COUNT(*) FROM v43_login_lockouts WHERE username!='__ip__' AND locked_until!='' AND locked_until>?", (utc_iso(),)).fetchone()[0]))
        ip_locked = max(ip_locked, int(c.execute("SELECT COUNT(*) FROM v43_login_lockouts WHERE username='__ip__' AND locked_until!='' AND locked_until>?", (utc_iso(),)).fetchone()[0]))
        cutoff = (utc_now() - timedelta(seconds=SESSION_ACTIVE_WINDOW_SECONDS)).isoformat().replace('+00:00','Z')
        c.execute("DELETE FROM v43_auth_sessions WHERE revoked=1 OR last_seen < ?", (cutoff,))
        users = []
        for row in c.execute('SELECT username,role,permissions_json,enabled,source,auth_version,created_at,updated_at FROM v43_auth_users ORDER BY username').fetchall():
            d = dict(row)
            try: d['permissions'] = json.loads(d.pop('permissions_json') or '[]')
            except Exception: d['permissions'] = []
            # Role invariants are enforced for the security UI as well as runtime checks.
            # This prevents legacy/previously saved permissions from leaking into the
            # readonly/admin views after an upgrade.
            if d['role'] == 'admin':
                d['permissions'] = list(AUTH_PERMISSION_DEFS)
            elif d['role'] == 'readonly':
                d['permissions'] = ['dashboard.view', 'stream.view']
            d['access_scope'] = _scope_payload(d['username'])
            d['current'] = d['username'] == _current_user()
            d['active_sessions'] = c.execute('SELECT COUNT(*) FROM v43_auth_sessions WHERE username=? AND revoked=0 AND last_seen>=?', (d['username'], cutoff)).fetchone()[0]
            users.append(d)
        sessions = [dict(x) for x in c.execute('SELECT session_id,username,role,client_ip,created_at,last_seen,revoked FROM v43_auth_sessions WHERE revoked=0 AND last_seen>=? ORDER BY last_seen DESC LIMIT 200', (cutoff,)).fetchall()]
    except Exception:
        users=[]; sessions=[]
    return jsonify({
        'ok': True,
        'auth_enabled': _auth_enabled(),
        'auth_configured': bool(AUTH_USER and AUTH_PASSWORD),
        'current_user': _current_user(),
        'current_role': _current_role(),
        'current_permissions': sorted(_user_permissions(_current_user(), _current_role())),
        'primary_admin_user': AUTH_USER,
        'session_timeout_seconds': SESSION_TIMEOUT_SECONDS,
        'secure_cookie_configured': SECURE_COOKIE,
        'session_hardening': {'httponly': True, 'samesite': 'Lax', 'secure': SECURE_COOKIE},
        'readonly_configured': bool(READONLY_USER and READONLY_PASSWORD),
        'locked_clients': locked,
        'ip_guard_locked_clients': ip_locked,
        'users': users,
        'active_sessions': sessions,
        'permission_definitions': AUTH_PERMISSION_DEFS,
        'role_defaults': AUTH_ROLE_DEFAULTS,
        'access_scope_types': ['country','device','camera'],
        'access_scope_note': 'Empty scope is unrestricted for backward compatibility; non-empty scope is an allow-list union.',
        'available_countries': [str(x[0]) for x in _db_conn().execute("SELECT DISTINCT country FROM cameras WHERE deleted=0 AND TRIM(country)!='' ORDER BY country COLLATE NOCASE").fetchall()],
        'available_devices': [dict(x) for x in _db_conn().execute("SELECT DISTINCT device_id, ip, country, MAX(label) AS label FROM cameras WHERE deleted=0 AND (TRIM(device_id)!='' OR TRIM(ip)!='') GROUP BY device_id,ip,country ORDER BY country COLLATE NOCASE, device_id, ip LIMIT 2000").fetchall()],
        'bounded_diagnostics': True,
        'public_range_scanning': False,
        'credential_bruteforce': False,
    })

@app.route('/api/v43/security/logout-all', methods=['POST'])
def v43_security_logout_all():
    if not _has_permission('security.manage'):
        return jsonify({'ok': False, 'detail': 'Permission required: security.manage'}), 403
    global _auth_epoch
    with _auth_epoch_lock:
        _auth_epoch = max(1, int(_auth_epoch) + 1)
        epoch = _auth_epoch
    # Persist BEFORE returning success. If persistence fails, the action is
    # still enforced for the current process through session revocation, but
    # we log the failure so it can be diagnosed instead of silently claiming
    # restart-safe invalidation.
    _persist_auth_epoch(epoch)
    global _AUTH_EPOCH_MIGRATION_GRACE
    _AUTH_EPOCH_MIGRATION_GRACE = False
    try:
        _db_retry_write(lambda: _db_conn().execute('UPDATE v43_auth_sessions SET revoked=1 WHERE revoked=0').connection.commit(), 'logout all sessions')
    except Exception:
        log.exception('[Auth] failed to revoke persisted sessions during logout-all')
    actor = _current_user()
    session.clear()
    _advanced_audit('security_logout_all', 'session', 'all', 'epoch='+str(epoch), actor=actor)
    return jsonify({'ok': True, 'detail': 'All dashboard sessions were invalidated. You will need to sign in again.'})

@app.route('/api/v43/security/clear-lockouts', methods=['POST'])
def v43_security_clear_lockouts():
    if not _has_permission('security.manage'):
        return jsonify({'ok': False, 'detail': 'Permission required: security.manage'}), 403
    count = _clear_all_user_lockouts()
    return jsonify({'ok': True, 'detail': f'Cleared {count} user lockout records. IP anti-bruteforce guards were preserved.'})

@app.route('/api/v43/security/clear-lockout/<path:username>', methods=['POST'])
def v43_security_clear_lockout_user(username):
    if not _has_permission('security.manage'):
        return jsonify({'ok': False, 'detail': 'Permission required: security.manage'}), 403
    username = username.strip()
    _clear_login_failures(username)
    return jsonify({'ok': True, 'detail': f'Lockout cleared for user {username}. IP anti-bruteforce guards were preserved.'})

@app.route('/api/v43/users',methods=['GET','POST'])
def v43_users():
    if request.method == 'GET' and not _has_permission('security.view'):
        return jsonify({'ok':False,'detail':'Permission required: security.view'}),403
    if request.method == 'POST' and not _has_permission('security.manage'):
        return jsonify({'ok':False,'detail':'Permission required: security.manage'}),403
    c=_db_conn(); d=request.get_json(silent=True) or {}
    if request.method=='POST':
        u=str(d.get('username') or '').strip()[:100]
        role=str(d.get('role') or 'custom').strip().lower()
        password=str(d.get('password') or '')
        enabled=1 if d.get('enabled', True) else 0
        if not re.fullmatch(r'[A-Za-z0-9_.@-]{2,100}', u):
            return jsonify({'ok':False,'detail':'Invalid username'}),400
        if role not in AUTH_ROLE_DEFAULTS:
            return jsonify({'ok':False,'detail':'Invalid role'}),400
        if u == AUTH_USER and role != 'admin':
            return jsonify({'ok':False,'detail':'The primary environment admin must remain admin.'}),409
        if u == READONLY_USER and role != 'readonly':
            return jsonify({'ok':False,'detail':'The environment readonly account must remain readonly.'}),409
        if not password:
            return jsonify({'ok':False,'detail':'Password is required when creating a user'}),400
        try:
            ph=_hash_user_password(password)
        except ValueError as e:
            return jsonify({'ok':False,'detail':str(e)}),400
        perms=d.get('permissions')
        if role == 'admin':
            perms=list(AUTH_PERMISSION_DEFS)
        elif role == 'readonly':
            perms=['dashboard.view', 'stream.view']
        elif perms is None:
            perms=_default_permissions(role)
        perms=sorted({str(x) for x in (perms or []) if str(x) in AUTH_PERMISSION_DEFS})
        if role == 'operator':
            if 'stream.view' not in perms:
                perms.append('stream.view')
                perms.sort()
        existing=c.execute('SELECT auth_version FROM v43_auth_users WHERE username=?',(u,)).fetchone()
        version=int(existing['auth_version'] or 1)+1 if existing else 1
        c.execute("""INSERT INTO v43_auth_users(username,password_hash,role,permissions_json,enabled,source,auth_version,created_at,updated_at)
                     VALUES(?,?,?,?,?,'database',?,datetime('now'),datetime('now'))
                     ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash,role=excluded.role,permissions_json=excluded.permissions_json,enabled=excluded.enabled,source='database',auth_version=?,updated_at=datetime('now')""",
                  (u,ph,role,json.dumps(perms),enabled,version,version))
        c.execute('UPDATE v43_auth_sessions SET revoked=1 WHERE username=?',(u,))
        c.execute("INSERT INTO v43_user_roles(username,role,enabled) VALUES(?,?,?) ON CONFLICT(username) DO UPDATE SET role=excluded.role,enabled=excluded.enabled",(u,role,enabled))
        c.commit()
        _advanced_audit('user_upsert','user',u,f'role={role};enabled={enabled};sessions_revoked=1',actor=_current_user())
    rows=[]
    cutoff=(datetime.now(timezone.utc)-timedelta(seconds=SESSION_ACTIVE_WINDOW_SECONDS)).isoformat().replace('+00:00','Z')
    for r in c.execute('SELECT username,role,permissions_json,enabled,source,auth_version,created_at,updated_at FROM v43_auth_users ORDER BY username').fetchall():
        x=dict(r)
        try:x['permissions']=json.loads(x.pop('permissions_json') or '[]')
        except Exception:x['permissions']=[]
        if x['role']=='admin': x['permissions']=list(AUTH_PERMISSION_DEFS)
        elif x['role']=='readonly': x['permissions']=['dashboard.view', 'stream.view']
        x['current']=x['username']==_current_user()
        x['active_sessions']=c.execute('SELECT COUNT(*) FROM v43_auth_sessions WHERE username=? AND revoked=0 AND last_seen>=?',(x['username'],cutoff)).fetchone()[0]
        rows.append(x)
    return jsonify({'ok':True,'items':rows,'permissions':AUTH_PERMISSION_DEFS,'roles':AUTH_ROLE_DEFAULTS})

@app.route('/api/v43/users/<path:username>',methods=['PATCH','DELETE'])
def v43_user_manage(username):
    if not _has_permission('security.manage'):
        return jsonify({'ok':False,'detail':'Permission required: security.manage'}),403
    username=username.strip()
    row=_user_row(username)
    if not row:
        return jsonify({'ok':False,'detail':'User not found'}),404
    if request.method=='DELETE':
        if username == _current_user():
            return jsonify({'ok':False,'detail':'You cannot disable the account you are currently using.'}),409
        if username == AUTH_USER:
            return jsonify({'ok':False,'detail':'The primary admin account cannot be disabled.'}),409
        try:
            def _disable():
                c=_db_conn()
                c.execute("UPDATE v43_auth_users SET enabled=0,auth_version=auth_version+1,updated_at=datetime('now') WHERE username=?",(username,))
                c.execute('UPDATE v43_auth_sessions SET revoked=1 WHERE username=?',(username,))
                c.execute('UPDATE v43_user_roles SET enabled=0 WHERE username=?',(username,))
                c.commit()
            _db_retry_write(_disable,'disable user')
        except Exception as e:
            log.exception('disable user failed for %s',username)
            return jsonify({'ok':False,'detail':f'Could not disable {username}: {e}'}),500
        _advanced_audit('user_disable','user',username,'disabled and sessions revoked',actor=_current_user())
        return jsonify({'ok':True,'detail':f'User {username} disabled and all sessions revoked.','username':username})
    d=request.get_json(silent=True) or {}
    role=str(d.get('role',row['role'])).strip().lower()
    enabled=1 if d.get('enabled',bool(row['enabled'])) else 0
    if username == _current_user() and not enabled:
        return jsonify({'ok':False,'detail':'You cannot disable the account you are currently using.'}),409
    if username == AUTH_USER and not enabled:
        return jsonify({'ok':False,'detail':'The primary admin account cannot be disabled.'}),409
    if role not in AUTH_ROLE_DEFAULTS:
        return jsonify({'ok':False,'detail':'Invalid role'}),400
    if username == AUTH_USER and role != 'admin':
        return jsonify({'ok':False,'detail':'The primary environment admin must remain admin.'}),409
    if username == READONLY_USER and role != 'readonly':
        return jsonify({'ok':False,'detail':'The environment readonly account must remain readonly.'}),409
    perms=d.get('permissions')
    if role=='admin':
        perms=list(AUTH_PERMISSION_DEFS)
    elif role=='readonly':
        perms=['dashboard.view', 'stream.view']
    elif perms is None:
        try: perms=json.loads(row['permissions_json'] or '[]')
        except Exception: perms=_default_permissions(role)
    perms=sorted({str(x) for x in (perms or []) if str(x) in AUTH_PERMISSION_DEFS})
    if role == 'operator':
        if 'stream.view' not in perms:
            perms.append('stream.view')
            perms.sort()
    sets=['role=?','permissions_json=?','enabled=?','auth_version=auth_version+1','updated_at=datetime(\'now\')']
    vals=[role,json.dumps(perms),enabled]
    if 'password' in d and d.get('password'):
        try:ph=_hash_user_password(str(d['password']))
        except ValueError as e:return jsonify({'ok':False,'detail':str(e)}),400
        sets.insert(0,'password_hash=?'); vals.insert(0,ph)
        sets.append("source='database'")
    vals.append(username)
    try:
        def _update():
            c=_db_conn()
            c.execute(f"UPDATE v43_auth_users SET {','.join(sets)} WHERE username=?",vals)
            c.execute('UPDATE v43_auth_sessions SET revoked=1 WHERE username=?',(username,))
            c.commit()
        _db_retry_write(_update,'update user')
    except Exception as e:
        log.exception('user update failed for %s',username)
        return jsonify({'ok':False,'detail':f'Could not update {username}: {e}'}),500
    _advanced_audit('user_update','user',username,f'role={role};enabled={enabled};sessions_revoked=1',actor=_current_user())
    return jsonify({'ok':True,'detail':f'User {username} updated. Existing sessions were revoked.','username':username})

@app.route('/api/v43/users/<path:username>/logout',methods=['POST'])
def v43_user_logout(username):
    if not _has_permission('security.manage'):
        return jsonify({'ok':False,'detail':'Permission required: security.manage'}),403
    username=username.strip()
    row=_user_row(username)
    if not row:
        return jsonify({'ok':False,'detail':'User not found'}),404
    actor=_current_user()
    was_current=(username==actor)
    try:
        def _revoke():
            c=_db_conn()
            c.execute('UPDATE v43_auth_sessions SET revoked=1 WHERE username=?',(username,))
            c.execute("UPDATE v43_auth_users SET auth_version=auth_version+1,updated_at=datetime('now') WHERE username=?",(username,))
            c.commit()
        _db_retry_write(_revoke,'logout user sessions')
    except Exception as e:
        log.exception('failed to revoke sessions for %s',username)
        return jsonify({'ok':False,'detail':f'Could not revoke sessions for {username}: {e}'}),500
    if was_current: session.clear()
    _advanced_audit('security_logout_user','user',username,'all sessions revoked',actor=actor)
    return jsonify({'ok':True,'detail':f'All sessions for {username} were invalidated.','current_user':actor,'logged_out_user':username})

@app.route('/api/v43/users/<path:username>/access', methods=['GET','PUT'])
def v43_user_access(username):
    required = 'security.view' if request.method == 'GET' else 'security.manage'
    if not _has_permission(required):
        return jsonify({'ok':False,'detail':f'Permission required: {required}'}),403
    username=username.strip(); row=_user_row(username)
    if not row: return jsonify({'ok':False,'detail':'User not found'}),404
    _ensure_access_scope_tables()
    if request.method=='GET': return jsonify({'ok':True, **_scope_payload(username)})
    d=request.get_json(silent=True) or {}
    allowed_types={'country':'countries','device':'devices','camera':'cameras'}
    values=[]
    for typ,key in allowed_types.items():
        raw=d.get(key,[]) or []
        if isinstance(raw,str): raw=[x.strip() for x in raw.replace('\r','').split('\n') if x.strip()]
        if not isinstance(raw,list): return jsonify({'ok':False,'detail':f'{key} must be a list or newline-separated string'}),400
        for v in raw[:10000]:
            v=_normalize_scope_value(v)
            if v: values.append((username,typ,v))
    try:
        def _save():
            c=_db_conn(); c.execute('DELETE FROM v43_user_access_scope WHERE username=?',(username,))
            if values: c.executemany('INSERT OR IGNORE INTO v43_user_access_scope(username,scope_type,scope_value) VALUES(?,?,?)',values)
            c.execute('UPDATE v43_auth_users SET auth_version=auth_version+1,updated_at=datetime(\'now\') WHERE username=?',(username,)); c.execute('UPDATE v43_auth_sessions SET revoked=1 WHERE username=?',(username,)); c.commit()
        _db_retry_write(_save,'save user camera access scope')
    except Exception as e:
        log.exception('[Access] failed saving scope for %s',username); return jsonify({'ok':False,'detail':f'Could not save access scope: {e}'}),500
    _advanced_audit('user_access_scope_update','user',username,json.dumps(_scope_payload(username),ensure_ascii=False),actor=_current_user())
    return jsonify({'ok':True,'detail':f'Access scope for {username} saved. Existing sessions were revoked.','scope':_scope_payload(username)})

@app.route('/api/v43/users/<path:username>/password',methods=['POST'])
def v43_user_password(username):
    """Password management: users change their own password; admins may reset any user."""
    username=username.strip()
    actor=_current_user()
    role=_current_role()
    if not _auth_enabled():
        return jsonify({'ok':False,'detail':'Authentication is disabled.'}),403
    if role!='admin' and username!=actor:
        return jsonify({'ok':False,'detail':'Only an administrator can reset another user password.'}),403
    row=_user_row(username)
    if not row:
        return jsonify({'ok':False,'detail':'User not found'}),404
    d=request.get_json(silent=True) or {}
    new_password=str(d.get('new_password') or '')
    if len(new_password)<8:
        return jsonify({'ok':False,'detail':'New password must contain at least 8 characters.'}),400
    if role!='admin':
        current=str(d.get('current_password') or '')
        valid=False
        if row['password_hash']:
            valid=_verify_user_password(current,row['password_hash'])
        elif username==AUTH_USER:
            valid=hmac.compare_digest(current,AUTH_PASSWORD)
        elif username==READONLY_USER and READONLY_PASSWORD:
            valid=hmac.compare_digest(current,READONLY_PASSWORD)
        if not valid:
            return jsonify({'ok':False,'detail':'Current password is incorrect.'}),400
    try:
        ph=_hash_user_password(new_password)
        def _change():
            c=_db_conn()
            c.execute("UPDATE v43_auth_users SET password_hash=?,source='database',auth_version=auth_version+1,force_password_change=0,password_changed_at=datetime('now'),updated_at=datetime('now') WHERE username=?",(ph,username))
            c.execute('UPDATE v43_auth_sessions SET revoked=1 WHERE username=?',(username,))
            c.commit()
        _db_retry_write(_change,'change user password')
    except Exception as e:
        log.exception('password change failed for %s',username)
        return jsonify({'ok':False,'detail':f'Could not change password for {username}: {e}'}),500
    _advanced_audit('password_change','user',username,'password changed; all sessions revoked',actor=actor)
    if username==actor:
        session.clear()
    return jsonify({'ok':True,'detail':f'Password changed for {username}. All existing sessions were revoked.','username':username,'current_user':actor})

@app.route('/api/account/password',methods=['POST'])
def api_account_password():
    """Authenticated self-service password change for every role."""
    if not _auth_enabled() or not _session_valid():
        return jsonify({'ok':False,'detail':'Authentication required.'}),401
    username=_current_user()
    row=_user_row(username)
    if not row:
        return jsonify({'ok':False,'detail':'User record not found.'}),404
    d=request.get_json(silent=True) or {}
    current=str(d.get('current_password') or '')
    new_password=str(d.get('new_password') or '')
    confirm=str(d.get('confirm_password') or '')
    if len(new_password)<8:
        return jsonify({'ok':False,'detail':'New password must contain at least 8 characters.'}),400
    if new_password!=confirm:
        return jsonify({'ok':False,'detail':'New passwords do not match.'}),400
    valid=False
    if row['password_hash']:
        valid=_verify_user_password(current,row['password_hash'])
    elif username==AUTH_USER:
        valid=hmac.compare_digest(current,AUTH_PASSWORD)
    elif username==READONLY_USER and READONLY_PASSWORD:
        valid=hmac.compare_digest(current,READONLY_PASSWORD)
    if not valid:
        return jsonify({'ok':False,'detail':'Current password is incorrect.'}),400
    try:
        ph=_hash_user_password(new_password)
        def _change():
            c=_db_conn()
            c.execute("UPDATE v43_auth_users SET password_hash=?,source='database',auth_version=auth_version+1,updated_at=datetime('now') WHERE username=?",(ph,username))
            c.execute('UPDATE v43_auth_sessions SET revoked=1 WHERE username=?',(username,))
            c.commit()
        _db_retry_write(_change,'self service password change')
    except Exception as e:
        log.exception('self-service password change failed for %s',username)
        return jsonify({'ok':False,'detail':f'Could not change password: {e}'}),500
    _advanced_audit('password_change','user',username,'self-service password change; all sessions revoked',actor=username)
    session.clear()
    return jsonify({'ok':True,'detail':'Password changed successfully. Please sign in again.','redirect':'/login?password_changed=1'})

@app.route('/api/session-status',methods=['GET'])
def api_session_status():
    if _auth_enabled() and not _session_valid():
        return jsonify({'ok':False,'authenticated':False,'detail':'Session expired or logged out'}), 401, {'Cache-Control':'no-store'}
    return jsonify({'ok':True,'authenticated':True,'username':_current_user(),'role':_current_role(),'permissions':sorted(_user_permissions(_current_user(),_current_role()))}), 200, {'Cache-Control':'no-store'}

@app.route('/api/v43/performance')
def v43_performance():
    return jsonify({'ok':True,'resource':_resource_snapshot(),'workers':_worker_health_snapshot(),'max_live_streams':MAX_STREAMS,'validation':val_get_progress()})


@app.route('/api/v43/import/history')
def v43_import_history():
    rows=_db_conn().execute('SELECT * FROM v43_import_export_history ORDER BY id DESC LIMIT 1000').fetchall(); return jsonify({'ok':True,'items':[dict(x) for x in rows]})


@app.route('/api/v43/device/report/<cam_id>/full')
def v43_full_device_report(cam_id):
    cam=db_get_camera(cam_id)
    if not cam:return jsonify({'ok':False,'detail':'Camera not found'}),404
    c=_db_conn(); return jsonify({'ok':True,'report':{'camera':_v43_redact_camera(cam),'vendor':_v40_camera_vendor(cam),'device_class':_v40_device_class(cam),'confidence':_advanced_confidence(cam),'health':_v43_safe_health_rows(cam_id,500),'ports':[dict(x) for x in c.execute('SELECT * FROM v43_camera_ports WHERE camera_id=?',(cam_id,)).fetchall()],'streams':[dict(x) for x in c.execute('SELECT * FROM v43_stream_candidates WHERE camera_id=?',(cam_id,)).fetchall()],'snapshots':[dict(x) for x in c.execute('SELECT * FROM v43_snapshot_history WHERE camera_id=? LIMIT 100',(cam_id,)).fetchall()]}})



@app.route('/api/v43/uptime/<cam_id>')
def v43_uptime(cam_id):
    rows=_db_conn().execute("SELECT status,checked_at FROM v43_health_samples WHERE camera_id=? ORDER BY id DESC LIMIT 10000",(cam_id,)).fetchall()
    total=len(rows); online=sum(str(r['status']).lower()=='online' for r in rows)
    return jsonify({'ok':True,'camera_id':cam_id,'samples':total,'online_samples':online,'uptime_percent':round(online*100/total,2) if total else 0,'latest':dict(rows[0]) if rows else None})

@app.route('/api/v43/correlate/<cam_id>')
def v43_correlate(cam_id):
    cam=db_get_camera(cam_id)
    if not cam:return jsonify({'ok':False,'detail':'Camera not found'}),404
    ip=str(cam.get('ip') or ''); rows=_db_conn().execute("SELECT id,label,ip,port,rtsp_port,camera_type,status,device_id,channel_index,channel_name,group_name FROM cameras WHERE deleted=0 AND ip=? ORDER BY port,id",(ip,)).fetchall()
    return jsonify({'ok':True,'camera_id':cam_id,'same_host':[dict(x) for x in rows]})

@app.route('/api/v43/channel-patterns')
def v43_channel_patterns():
    rows=_db_conn().execute("SELECT COALESCE(device_id,'') device_id,COUNT(*) n,MIN(channel_index) min_channel,MAX(channel_index) max_channel FROM cameras WHERE deleted=0 GROUP BY device_id HAVING device_id<>'' ORDER BY n DESC LIMIT 1000").fetchall()
    return jsonify({'ok':True,'patterns':[dict(x) for x in rows]})

@app.route('/api/v43/geo/<cam_id>')
def v43_geo(cam_id):
    cam=db_get_camera(cam_id)
    if not cam:return jsonify({'ok':False,'detail':'Camera not found'}),404
    ip=str(cam.get('ip') or '')
    row=_db_conn().execute('SELECT * FROM v43_geo_cache WHERE ip=?',(ip,)).fetchone()
    if row:return jsonify({'ok':True,'geo':dict(row)})
    geo=_device_geo(ip,True)
    if geo:
        _db_conn().execute('INSERT INTO v43_geo_cache(ip,country,region,city,isp,asn,source,confidence) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(ip) DO UPDATE SET country=excluded.country,region=excluded.region,city=excluded.city,isp=excluded.isp,asn=excluded.asn,source=excluded.source,confidence=excluded.confidence,updated_at=datetime("now")',(ip,geo.get('country',''),geo.get('region',''),geo.get('city',''),geo.get('isp',''),geo.get('asn',''),geo.get('geo_source',''),geo.get('geo_confidence',''))); _db_conn().commit()
    return jsonify({'ok':True,'geo':geo})

@app.route('/api/v43/snapshot/compare/<cam_id>')
def v43_snapshot_compare(cam_id):
    rows=_db_conn().execute('SELECT * FROM v43_snapshot_history WHERE camera_id=? ORDER BY id DESC LIMIT 2',(cam_id,)).fetchall()
    return jsonify({'ok':True,'latest':dict(rows[0]) if rows else None,'previous':dict(rows[1]) if len(rows)>1 else None})

@app.route('/api/v43/recovery',methods=['GET','POST'])
def v43_recovery():
    c=_db_conn()
    if request.method=='POST':
        d=request.get_json(silent=True) or {}
        allowed={'attempts':max(0,min(10,int(d.get('attempts',STREAM_RECOVERY_ATTEMPTS)))),'base_delay':max(0.2,min(30,float(d.get('base_delay',1)))),'max_delay':max(1,min(120,float(d.get('max_delay',30))))}
        c.execute("INSERT INTO app_settings(key,value) VALUES('v43_recovery',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=datetime('now')",(json.dumps(allowed),)); c.commit()
    row=c.execute("SELECT value FROM app_settings WHERE key='v43_recovery'").fetchone(); cfg={'attempts':STREAM_RECOVERY_ATTEMPTS,'base_delay':1,'max_delay':30}
    if row:
        try: cfg.update(json.loads(row['value']))
        except Exception: pass
    return jsonify({'ok':True,'recovery':cfg})

@app.route('/api/v43/search')
def v43_search():
    q=str(request.args.get('q') or '').strip(); status=str(request.args.get('status') or '').strip().lower(); vendor=str(request.args.get('vendor') or '').strip().lower(); country=str(request.args.get('country') or '').strip(); typ=str(request.args.get('type') or '').strip().lower(); group=str(request.args.get('group') or '').strip(); channel=str(request.args.get('channel') or '').strip()
    c=_db_conn(); clauses=['deleted=0']; args=[]
    if q: clauses.append('(id LIKE ? OR label LIKE ? OR ip LIKE ? OR group_name LIKE ? OR camera_type LIKE ? OR country LIKE ? OR city LIKE ? OR channel_name LIKE ?)'); args += [f'%{q}%']*8
    if status: clauses.append('lower(status)=?'); args.append(status)
    if country: clauses.append('country=?'); args.append(country)
    if group: clauses.append('group_name=?'); args.append(group)
    if channel: clauses.append('(CAST(channel_index AS TEXT)=? OR channel_name LIKE ?)'); args += [channel,f'%{channel}%']
    rows=[dict(x) for x in c.execute('SELECT * FROM cameras WHERE '+' AND '.join(clauses)+' ORDER BY group_name,id LIMIT 2000',args).fetchall()]
    out=[]
    for d in rows:
        if vendor and _v40_camera_vendor(d)!=vendor: continue
        if typ and typ not in str(d.get('camera_type','')).lower(): continue
        d['vendor']=_v40_camera_vendor(d); d['device_class']=_v40_device_class(d); d['quality_score']=_v40_quality_score(d); out.append(_v43_redact_camera(d))
    return jsonify({'ok':True,'count':len(out),'items':out})

def _v43_feature_supervisor():
    _touch_worker('v43-feature-supervisor','starting')
    while not _shutdown_requested.is_set():
        if not _service_enabled('feature-supervisor'):
            _touch_worker('v43-feature-supervisor','stopped','disabled by operator'); return
        try:
            _touch_worker('v43-feature-supervisor','running')
            # Sample a bounded batch of recently active cameras. The actual validator remains responsible for RTSP/auth validation.
            ids=[r['id'] for r in _db_conn().execute("SELECT id FROM cameras WHERE deleted=0 AND status IN ('online','offline','checking','unknown') ORDER BY updated_at DESC LIMIT 25").fetchall()]
            for cid in ids:
                if _shutdown_requested.is_set(): break
                try: _v43_record_health(cid)
                except Exception: pass
            # Retention is internally rate-limited to once per 5 minutes.
            _v40_maintenance()
        except Exception: log.exception('v43 feature supervisor failed')
        _shutdown_requested.wait(max(60,ADVANCED_HEALTH_INTERVAL))
    _touch_worker('v43-feature-supervisor','stopped')


def start_v43_feature_supervisor():
    if not _service_enabled('feature-supervisor'):
        _touch_worker('v43-feature-supervisor','stopped','disabled by operator'); return threading.current_thread()
    t=threading.Thread(target=_v43_feature_supervisor,daemon=True,name='v43-feature-supervisor'); t.start(); return t

# ══════════════════════════════════════════════════════════════════════════════
# v61 ROADMAP API — priorities 1..16
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/api/admin/background-services', methods=['GET'])
def api_background_services():
    denied = _admin_only()
    if denied:
        return denied
    return jsonify({'ok': True, 'services': _background_services_snapshot()})

@app.route('/api/admin/background-services/<name>', methods=['POST'])
def api_background_service_toggle(name):
    denied = _admin_only()
    if denied:
        return denied
    name=str(name).strip().lower()
    if name not in _BACKGROUND_SERVICES:
        return jsonify({'ok':False,'detail':'Unknown background service'}),404
    data=request.get_json(silent=True) or {}
    enabled=bool(data.get('enabled'))
    _service_set_enabled(name, enabled)
    log.warning('[Services] %s -> %s by %s (persistent)', name, 'ON' if enabled else 'OFF', _current_user())
    if name == 'audit-log':
        return jsonify({'ok':True,'service':name,'enabled':enabled,'services':_background_services_snapshot()})
    # Start selected service immediately when possible. OFF is cooperative: the
    # worker exits at its next safe loop boundary; no process is killed.
    if enabled:
        try:
            if name == 'health-supervisor': _service_register_thread(name,start_v40_health_supervisor())
            elif name == 'schedule-supervisor': _service_register_thread(name,start_v40_schedule_supervisor())
            elif name == 'feature-supervisor': _service_register_thread(name,start_v43_feature_supervisor())
            elif name == 'validation-queue': _service_register_thread(name,start_validation_queue_worker(max_workers=None,delay=0.2))
            elif name == 'file-scanner':
                t=threading.Thread(target=_background_scanner,daemon=True,name='cam-scanner'); _service_register_thread(name,t); t.start()
            elif name == 'database-backup': _service_register_thread(name,start_database_backup_worker(delay=0.2,interval=max(300.0,float(os.getenv('CAMERA_BACKUP_INTERVAL_SECONDS','1800')))))
            elif name == 'worker-supervisor': _service_register_thread(name,start_worker_supervisor(interval=max(15,int(os.getenv('CAMERA_WORKER_SUPERVISOR_INTERVAL','30')))))
            elif name == 'full-integrity':
                t=threading.Thread(target=_background_live_integrity_check,daemon=True,name='db-integrity'); _service_register_thread(name,t); t.start()
            elif name == 'advanced-metadata':
                t=threading.Thread(target=_run_advanced_metadata_service,daemon=True,name='startup-metadata'); _service_register_thread(name,t); t.start()
            elif name == 'inventory-resume':
                log.info('[Services] Inventory Job Resume enabled; queued jobs will be resumed by the next startup/recovery cycle')
        except Exception as exc:
            log.exception('[Services] Failed starting %s',name)
            return jsonify({'ok':False,'detail':f'{type(exc).__name__}: {exc}','services':_background_services_snapshot()}),500
    return jsonify({'ok':True,'service':name,'enabled':enabled,'services':_background_services_snapshot()})



@app.route('/api/v61/worker')
def v61_worker():
    return jsonify({'ok':True,'workers':_worker_health_snapshot(),'supervisor':_supervisor_snapshot(),'resource':_resource_snapshot(),'validation':val_get_progress()})

@app.route('/api/v61/db/status')
def v61_db_status():
    usage=shutil.disk_usage(SCRIPT_DIR)
    backups=[]
    for path in DB_BACKUPS:
        if os.path.exists(path):
            backups.append({'path':os.path.basename(path),'size':os.path.getsize(path),'mtime':datetime.fromtimestamp(os.path.getmtime(path),timezone.utc).isoformat(),'integrity':_sqlite_integrity_ok(path)})
    return jsonify({'ok':True,'db':os.path.basename(DB_PATH),'size':os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0,'integrity':_cached_read('live_integrity',60.0,lambda:_sqlite_integrity_ok(DB_PATH)),'wal':os.path.exists(DB_PATH+'-wal'),'free_bytes':usage.free,'backups':backups})

@app.route('/api/v61/db/backup',methods=['POST'])
def v61_db_backup():
    ok=db_create_backup(); _record_recovery_event('db_backup','manual','ok' if ok else 'failed','verified rotating backup'); return jsonify({'ok':ok,'backups':DB_BACKUPS})

@app.route('/api/v61/audit')
def v61_audit():
    limit=max(1,min(2000,int(request.args.get('limit',200)))); c=_db_conn()
    rows=c.execute('SELECT * FROM audit_log ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    return jsonify({'ok':True,'items':[dict(x) for x in rows]})

@app.route('/api/v61/login-history')
def v61_login_history():
    limit=max(1,min(2000,int(request.args.get('limit',200)))); rows=_db_conn().execute('SELECT * FROM login_history ORDER BY id DESC LIMIT ?', (limit,)).fetchall(); return jsonify({'ok':True,'items':[dict(x) for x in rows]})

@app.route('/api/v61/sessions')
def v61_sessions():
    rows=_db_conn().execute('SELECT username,session_id,created_at,last_seen,revoked,user_agent,ip FROM v43_auth_sessions ORDER BY last_seen DESC LIMIT 1000').fetchall(); return jsonify({'ok':True,'items':[dict(x) for x in rows]})

@app.route('/api/v61/quality/<cam_id>')
def v61_quality(cam_id):
    rows=_db_conn().execute('SELECT * FROM stream_observations WHERE camera_id=? ORDER BY id DESC LIMIT 100',(cam_id,)).fetchall()
    items=[dict(r) for r in rows]
    for d in items:
        score=100.0
        score-=20 if not d.get('video') else 0
        score-=15 if not d.get('first_frame') else 0
        score-=10 if not d.get('codec') else 0
        score-=10 if not d.get('resolution') else 0
        score-=10 if not d.get('fps') else 0
        lat=float(d.get('latency_ms') or 0)
        if lat>2000: score-=20
        elif lat>1000: score-=10
        elif lat>500: score-=5
        bitrate=float(d.get('bitrate') or 0)
        if bitrate<=0: score-=5
        score=max(0,min(100,round(score,1))); d['advanced_quality_score']=score
    return jsonify({'ok':True,'camera_id':cam_id,'observations':items})

@app.route('/api/v61/reports/summary')
def v61_report_summary():
    c=_db_conn(); total=c.execute('SELECT COUNT(*) FROM cameras WHERE deleted=0').fetchone()[0]
    by_status=[dict(r) for r in c.execute("SELECT status,COUNT(*) n FROM cameras WHERE deleted=0 GROUP BY status ORDER BY n DESC").fetchall()]
    by_vendor=[dict(r) for r in c.execute("SELECT camera_type vendor,COUNT(*) n FROM cameras WHERE deleted=0 GROUP BY camera_type ORDER BY n DESC LIMIT 100").fetchall()]
    by_country=[dict(r) for r in c.execute("SELECT country,COUNT(*) n FROM cameras WHERE deleted=0 GROUP BY country ORDER BY n DESC LIMIT 100").fetchall()]
    return jsonify({'ok':True,'total':total,'status':by_status,'vendor':by_vendor,'country':by_country,'generated_at':utc_iso()})

@app.route('/api/v61/reports/export')
def v61_report_export():
    fmt=str(request.args.get('format','json')).lower(); rows=[dict(r) for r in _db_conn().execute('SELECT id,label,ip,port,camera_type,country,region,city,status,codec,resolution,fps,latency_ms,rtsp_score,last_checked,last_seen,group_name FROM cameras WHERE deleted=0 ORDER BY group_name,id').fetchall()]
    if fmt=='csv':
        out=io.StringIO(); w=csv.DictWriter(out,fieldnames=list(rows[0].keys()) if rows else ['id']); w.writeheader(); w.writerows(rows); return Response(out.getvalue(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=camera_report.csv'})
    return jsonify({'ok':True,'generated_at':utc_iso(),'items':rows})

@app.route('/api/v61/geo/summary')
def v61_geo_summary():
    rows=_db_conn().execute("SELECT country,COUNT(*) n FROM cameras WHERE deleted=0 GROUP BY country ORDER BY n DESC LIMIT 200").fetchall(); return jsonify({'ok':True,'countries':[dict(r) for r in rows]})

@app.route('/api/v61/recovery')
def v61_recovery():
    rows=_db_conn().execute('SELECT * FROM recovery_events ORDER BY id DESC LIMIT 500').fetchall(); return jsonify({'ok':True,'events':[dict(r) for r in rows],'supervisor':_supervisor_snapshot()})


# ══════════════════════════════════════════════════════════════════════════════
# v66 OBSERVABILITY + SECURITY HARDENING
# Continuous quality analytics, geo map data, alert statistics and session policy.
# ══════════════════════════════════════════════════════════════════════════════
def _v66_quality_summary(limit=500):
    c=_db_conn()
    rows=c.execute("""
        SELECT camera_id,
               COUNT(*) samples,
               AVG(NULLIF(fps,0)) avg_fps,
               AVG(NULLIF(bitrate,0)) avg_bitrate,
               AVG(NULLIF(latency_ms,0)) avg_latency,
               AVG(quality_score) avg_quality,
               MAX(observed_at) last_observed
        FROM stream_observations
        GROUP BY camera_id
        ORDER BY last_observed DESC
        LIMIT ?
    """,(max(1,min(5000,int(limit))),)).fetchall()
    return [dict(r) for r in rows]

@app.route('/api/v66/quality/summary')
def v66_quality_summary():
    if not _has_permission('diagnostics.run'):
        return jsonify({'ok':False,'detail':'Diagnostics permission required'}),403
    rows=_v66_quality_summary(request.args.get('limit',500))
    if not rows:
        return jsonify({'ok':True,'items':[],'aggregate':{'samples':0}})
    nums=lambda k:[float(r[k] or 0) for r in rows if r.get(k) is not None]
    q=nums('avg_quality'); lat=nums('avg_latency'); fps=nums('avg_fps'); br=nums('avg_bitrate')
    return jsonify({'ok':True,'items':rows,'aggregate':{
        'cameras':len(rows),'avg_quality':round(sum(q)/len(q),1) if q else 0,
        'avg_latency_ms':round(sum(lat)/len(lat),1) if lat else 0,
        'avg_fps':round(sum(fps)/len(fps),1) if fps else 0,
        'avg_bitrate':round(sum(br)/len(br),1) if br else 0,
    },'generated_at':utc_iso()})

@app.route('/api/v66/geo/map')
def v66_geo_map():
    if not _has_permission('reports.view'):
        return jsonify({'ok':False,'detail':'Reports permission required'}),403
    rows=_db_conn().execute("""
        SELECT country,region,city,COUNT(*) count
        FROM cameras WHERE deleted=0 GROUP BY country,region,city
        ORDER BY count DESC LIMIT 5000
    """).fetchall()
    return jsonify({'ok':True,'points':[dict(r) for r in rows],'generated_at':utc_iso()})

@app.route('/api/v66/security/summary')
def v66_security_summary():
    if not _has_permission('security.view'):
        return jsonify({'ok':False,'detail':'Security permission required'}),403
    c=_db_conn()
    active=int(c.execute('SELECT COUNT(*) FROM v43_auth_sessions WHERE revoked=0').fetchone()[0])
    users=int(c.execute('SELECT COUNT(*) FROM v43_auth_users WHERE enabled=1').fetchone()[0])
    # This endpoint is polled frequently; never perform schema-repair writes here.
    login_cols={r[1] for r in c.execute('PRAGMA table_info(login_history)').fetchall()}
    if {'ok','created_at'}.issubset(login_cols):
        failures=int(c.execute("SELECT COUNT(*) FROM login_history WHERE ok=0 AND created_at >= datetime('now','-24 hours')").fetchone()[0])
    else:
        failures=0
    return jsonify({'ok':True,'active_sessions':active,'enabled_users':users,'failed_logins_24h':failures,
                    'max_sessions_per_user':MAX_SESSIONS_PER_USER,'password_max_age_days':PASSWORD_MAX_AGE_DAYS})

@app.route('/api/v66/users/<path:username>/force-password',methods=['POST'])
def v66_force_password(username):
    if not _has_permission('security.manage'):
        return jsonify({'ok':False,'detail':'Permission required: security.manage'}),403
    username=unquote(username).strip()
    if not _user_row(username):
        return jsonify({'ok':False,'detail':'User not found'}),404
    d=request.get_json(silent=True) or {}
    state=bool(d.get('required',True))
    c=_db_conn()
    with _DB_WRITE_LOCK:
        c.execute('UPDATE v43_auth_users SET force_password_change=?,updated_at=datetime(\'now\') WHERE username=?',(1 if state else 0,username)); c.commit()
    _advanced_audit('force_password_change','user',username,'required='+str(state),actor=_current_user())
    return jsonify({'ok':True,'username':username,'force_password_change':state})

# ══════════════════════════════════════════════════════════════════════════════
# v63 MAIN DASHBOARD OPERATIONAL METRICS
# High-level metrics are intentionally exposed through dashboard.view so the
# existing main dashboard can update live without opening a separate page.
# Detailed diagnostics remain permission-gated by the existing dashboard tools.
# ══════════════════════════════════════════════════════════════════════════════
def v63_dashboard_metrics():
    if not _has_permission('dashboard.view'):
        return jsonify({'ok': False, 'detail': 'Dashboard permission required'}), 403
    try:
        workers = _worker_health_snapshot()
        resource = _resource_snapshot()
        vp = val_get_progress()
        queue = _v62_queue_health() if '_v62_queue_health' in globals() else {'ok': False, 'total': 0, 'due': 0, 'locked': 0, 'stale_claims': 0}
        with _streams_lock:
            active = sum(_stream_subscribers.values())
        payload = {
            'ok': True,
            'timestamp': utc_iso(),
            'worker': {'workers': workers, 'supervisor': _supervisor_snapshot()},
            'validation': vp,
            'queue': queue,
            'feeds': {'active': active, 'limit': MAX_STREAMS, 'recovery_attempts': STREAM_RECOVERY_ATTEMPTS},
            'db': {'integrity': _cached_read('live_integrity', 60.0, lambda: _sqlite_integrity_ok(DB_PATH)), 'size': os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0},
            'database_maintenance': _db_maintenance_snapshot(),
        }
        # Resource details are useful but are only exposed to diagnostics users.
        if _has_permission('diagnostics.run'):
            payload['resource'] = resource
            payload['worker']['adaptive_workers'] = _WORKER_STATE.get('workers', 0)
            payload['worker']['mode'] = _WORKER_STATE.get('mode', 'auto')
        # DB free-space/integrity detail is only exposed to database managers.
        if _has_permission('database.manage'):
            usage = shutil.disk_usage(SCRIPT_DIR)
            payload['db']['free_bytes'] = usage.free
        return jsonify(payload)
    except Exception as exc:
        log.exception('v63 dashboard metrics failed')
        return jsonify({'ok': False, 'detail': f'{type(exc).__name__}: {exc}'}), 500


# ══════════════════════════════════════════════════════════════════════════════
# v62 PRODUCTION HARDENING
# 17. Startup self-test / dependency + configuration validation
# 18. Database schema-drift detection
# 19. Validation queue lease/claim health metrics + stale-claim cleanup
# ══════════════════════════════════════════════════════════════════════════════
# v64 DIRECT VALIDATION CONTROLS
# Pause/resume, retry failed, queue cleanup and graceful worker restart.
# ══════════════════════════════════════════════════════════════════════════════
def _v64_validation_control_snapshot() -> dict:
    return {
        'paused': _validation_queue_paused.is_set(),
        'restart_pending': _validation_queue_restart_requested.is_set(),
        'queue': _v62_queue_health() if '_v62_queue_health' in globals() else {},
        'progress': val_get_progress(),
    }


def _v64_require_control_permission():
    if not _has_permission('diagnostics.run'):
        return jsonify({'ok': False, 'detail': 'Diagnostics permission required'}), 403
    return None


@app.route('/api/v64/validation/control', methods=['GET', 'POST'])
def v64_validation_control():
    denied = _v64_require_control_permission()
    if denied:
        return denied
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        action = str(data.get('action', '')).strip().lower()
        if action == 'pause':
            _validation_queue_paused.set()
            _record_recovery_event('validation_control', 'queue', 'paused', 'operator requested pause')
            _advanced_audit('validation_pause', 'validation_queue', 'queue', 'operator paused validation queue')
            return jsonify({'ok': True, 'detail': 'Validation queue paused. Active probes will finish safely.',
                            'control': _v64_validation_control_snapshot()})
        if action == 'resume':
            _validation_queue_paused.clear()
            _record_recovery_event('validation_control', 'queue', 'resumed', 'operator resumed validation')
            _advanced_audit('validation_resume', 'validation_queue', 'queue', 'operator resumed validation queue')
            return jsonify({'ok': True, 'detail': 'Validation queue resumed.',
                            'control': _v64_validation_control_snapshot()})
        if action == 'retry-failed':
            c = _db_conn()
            with _DB_WRITE_LOCK:
                cur = c.execute("""
                    UPDATE validation_queue
                    SET attempts=0, last_attempt=NULL, next_attempt=datetime('now'),
                        locked_until=NULL, locked_by='', last_error=''
                    WHERE camera_id IN (
                        SELECT id FROM cameras WHERE deleted=0 AND status='offline'
                    )
                """)
                c.commit()
                count = int(cur.rowcount or 0)
            _record_recovery_event('validation_retry_failed', 'validation_queue', 'ok', str(count))
            _advanced_audit('validation_retry_failed', 'validation_queue', 'queue', f'requeued {count} failed cameras')
            return jsonify({'ok': True, 'detail': f'Requeued {count} failed camera(s).',
                            'requeued': count, 'control': _v64_validation_control_snapshot()})
        if action == 'queue-cleanup':
            count = _v62_release_stale_claims()
            return jsonify({'ok': True, 'detail': f'Released {count} stale claim(s).',
                            'released': count, 'control': _v64_validation_control_snapshot()})
        if action == 'restart-worker':
            if _validation_queue_restart_requested.is_set():
                return jsonify({'ok': True, 'detail': 'Validation worker restart is already pending.',
                                'control': _v64_validation_control_snapshot()})
            _validation_queue_restart_requested.set()
            _record_recovery_event('worker_restart', 'validation-queue', 'requested', 'operator requested graceful restart')
            _advanced_audit('worker_restart', 'worker', 'validation-queue', 'operator requested graceful restart')
            return jsonify({'ok': True, 'detail': 'Validation worker restart requested. Current batch will finish first.',
                            'control': _v64_validation_control_snapshot()})
        return jsonify({'ok': False, 'detail': 'Unknown validation control action'}), 400
    return jsonify({'ok': True, 'control': _v64_validation_control_snapshot()})


@app.route('/api/v64/dashboard-metrics')
def v64_dashboard_metrics():
    if not _has_permission('dashboard.view'):
        return jsonify({'ok': False, 'detail': 'Dashboard permission required'}), 403
    try:
        base = v63_dashboard_metrics().get_json()
        base['version'] = VERSION
        base['validation_control'] = _v64_validation_control_snapshot()
        base['validation_engine'] = _v65_queue_snapshot()
        return jsonify(base)
    except Exception as exc:
        log.exception('v64 dashboard metrics failed')
        return jsonify({'ok': False, 'detail': f'{type(exc).__name__}: {exc}'}), 500


# ══════════════════════════════════════════════════════════════════════════════
# v65 VALIDATION ENGINE 2.0
# Priority-aware queue, configurable exponential retry policy, queue ETA,
# per-camera retry state and duplicate/in-flight protection metrics.
# ══════════════════════════════════════════════════════════════════════════════
def _v65_queue_snapshot() -> dict:
    c = _db_conn()
    now_clause = "datetime('now')"
    total = int(c.execute('SELECT COUNT(*) FROM validation_queue').fetchone()[0])
    due = int(c.execute(f'SELECT COUNT(*) FROM validation_queue WHERE next_attempt <= {now_clause}').fetchone()[0])
    locked = int(c.execute("SELECT COUNT(*) FROM validation_queue WHERE locked_until IS NOT NULL AND locked_until >= datetime('now')").fetchone()[0])
    stale = int(c.execute("SELECT COUNT(*) FROM validation_queue WHERE locked_until IS NOT NULL AND locked_until < datetime('now')").fetchone()[0])
    exhausted = int(c.execute("SELECT COUNT(*) FROM validation_queue WHERE attempts >= max_attempts AND next_attempt > datetime('now')").fetchone()[0])
    retrying = int(c.execute("SELECT COUNT(*) FROM validation_queue WHERE attempts > 0 AND next_attempt > datetime('now')").fetchone()[0])
    priority = [dict(r) for r in c.execute("SELECT priority,COUNT(*) count FROM validation_queue GROUP BY priority ORDER BY priority").fetchall()]
    with _val_lock:
        vp = dict(_val_progress)
    speed = float(vp.get('speed_per_sec') or 0.0)
    eta = round(due / speed, 1) if speed > 0 and due > 0 else None
    return {
        'total': total, 'due': due, 'locked': locked, 'stale_claims': stale,
        'exhausted': exhausted, 'retrying': retrying, 'priority': priority,
        'speed_per_sec': speed, 'eta_seconds': eta,
        'max_attempts': VALIDATION_MAX_ATTEMPTS,
        'retry_base_minutes': VALIDATION_RETRY_BASE_MINUTES,
        'retry_max_minutes': VALIDATION_RETRY_MAX_MINUTES,
    }

@app.route('/api/v65/validation-engine')
def v65_validation_engine():
    if not _has_permission('dashboard.view'):
        return jsonify({'ok': False, 'detail': 'Dashboard permission required'}), 403
    try:
        snap = _v65_queue_snapshot()
        snap['control'] = _v64_validation_control_snapshot()
        return jsonify({'ok': True, 'version': VERSION, 'engine': snap})
    except Exception as exc:
        log.exception('v65 validation engine metrics failed')
        return jsonify({'ok': False, 'detail': f'{type(exc).__name__}: {exc}'}), 500

@app.route('/api/v65/validation/retry-exhausted', methods=['POST'])
def v65_retry_exhausted():
    denied = _v64_require_control_permission()
    if denied:
        return denied
    c = _db_conn()
    with _DB_WRITE_LOCK:
        cur = c.execute("""
            UPDATE validation_queue
               SET attempts=0,last_attempt=NULL,next_attempt=datetime('now'),
                   locked_until=NULL,locked_by='',last_error=''
             WHERE attempts >= max_attempts
        """)
        c.commit()
        count = int(cur.rowcount or 0)
    _record_recovery_event('validation_retry_exhausted', 'validation_queue', 'ok', str(count))
    _advanced_audit('validation_retry_exhausted', 'validation_queue', 'queue', f'requeued {count} exhausted cameras')
    return jsonify({'ok': True, 'requeued': count, 'detail': f'Requeued {count} exhausted camera(s).', 'engine': _v65_queue_snapshot()})

@app.route('/api/v65/validation/reprioritize', methods=['POST'])
def v65_reprioritize():
    denied = _v64_require_control_permission()
    if denied:
        return denied
    c = _db_conn()
    with _DB_WRITE_LOCK:
        rows = c.execute("SELECT id,status,favorite FROM cameras WHERE deleted=0").fetchall()
        updates = [(_validation_priority(dict(r)), r['id']) for r in rows]
        c.executemany('UPDATE validation_queue SET priority=? WHERE camera_id=?', updates)
        c.commit()
    _advanced_audit('validation_reprioritize', 'validation_queue', 'queue', f'reprioritized {len(updates)} cameras')
    return jsonify({'ok': True, 'updated': len(updates), 'engine': _v65_queue_snapshot()})


# 20. Worker restart budget / anti-restart storm protection
# 21. Session/device management detail + forced-session controls
# 22. Security/configuration health report
# 23. Operational diagnostics + route inventory
# 24. Recovery history + operational metrics
# ══════════════════════════════════════════════════════════════════════════════

V62_REQUIRED_TABLES = {
    'cameras', 'validation_queue', 'audit_log', 'login_history',
    'v43_auth_users', 'v43_auth_sessions', 'recovery_events',
    'worker_events', 'stream_observations'
}
V62_REQUIRED_FUNCTIONS = {
    'db_init', '_v43_ensure_schema', '_validation_worker_config',
    'start_validation_queue_worker', 'start_worker_supervisor',
    'db_create_backup', '_record_recovery_event', '_session_valid'
}
V62_STARTUP_SELFTEST = {'ran': False, 'ok': False, 'at': '', 'errors': [], 'warnings': [], 'success_count': 0}
V62_RESTART_LIMIT = max(1, min(20, int(os.getenv('CAMERA_WORKER_MAX_RESTARTS', '5'))))
V62_RESTART_WINDOW = max(60, int(os.getenv('CAMERA_WORKER_RESTART_WINDOW_SECONDS', '900')))
V62_QUEUE_LEASE_MINUTES = max(2, min(60, int(os.getenv('CAMERA_QUEUE_LEASE_MINUTES', '15'))))


def _v62_table_columns(table: str) -> set[str]:
    try:
        rows = _db_conn().execute(f'PRAGMA table_info("{table}")').fetchall()
        return {str(r['name']) for r in rows}
    except Exception:
        return set()


def _v62_schema_check() -> dict:
    errors, warnings, tables = [], [], {}
    try:
        c = _db_conn()
        existing = {str(r[0]) for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for table in sorted(V62_REQUIRED_TABLES):
            if table not in existing:
                errors.append(f'missing table: {table}')
            else:
                tables[table] = sorted(_v62_table_columns(table))
        if 'validation_queue' in existing:
            cols = _v62_table_columns('validation_queue')
            for col in ('camera_id', 'next_attempt', 'locked_until', 'locked_by'):
                if col not in cols:
                    errors.append(f'validation_queue missing column: {col}')
        if 'v43_auth_sessions' in existing:
            cols = _v62_table_columns('v43_auth_sessions')
            for col in ('username', 'session_id', 'created_at', 'last_seen', 'revoked'):
                if col not in cols:
                    errors.append(f'v43_auth_sessions missing column: {col}')
    except Exception as exc:
        errors.append(f'schema check failed: {type(exc).__name__}: {exc}')
    return {'ok': not errors, 'errors': errors, 'warnings': warnings, 'tables': tables}


_PACKAGE_VERSION_CACHE = {}
def _package_version(mod, name='') -> str:
    """Return a dependency version safely, with a small in-process cache."""
    key = name or getattr(mod, '__name__', '') or repr(mod)
    cached = _PACKAGE_VERSION_CACHE.get(key)
    if cached is not None:
        return cached

    result = ''
    try:
        from importlib.metadata import version as _dist_version
        package_name = name or getattr(mod, '__name__', '')
        if package_name:
            result = str(_dist_version(package_name))
    except Exception:
        result = ''

    if not result:
        try:
            result = str(getattr(mod, '__version__', '') or '')
        except Exception:
            result = ''

    if not result:
        result = 'unknown'
    _PACKAGE_VERSION_CACHE[key] = result
    return result


def _v62_dependency_check() -> dict:
    deps = {}
    for name in ('flask', 'requests', 'flask_sock'):
        try:
            mod = __import__(name)
            deps[name] = {'available': True, 'version': str(_package_version(mod, name))}
        except Exception as exc:
            deps[name] = {'available': False, 'error': type(exc).__name__}
    ffmpeg = shutil.which('ffmpeg')
    ffprobe = shutil.which('ffprobe')
    deps['ffmpeg'] = {'available': bool(ffmpeg), 'path': ffmpeg or ''}
    deps['ffprobe'] = {'available': bool(ffprobe), 'path': ffprobe or ''}
    return deps


def _v62_config_check() -> dict:
    warnings, errors = [], []
    if _auth_enabled() and not SECURE_COOKIE:
        warnings.append('Authentication is enabled but CAMERA_SECURE_COOKIE is false.')
    if AUTH_ENABLED_CONFIG and (not AUTH_USER or not AUTH_PASSWORD):
        errors.append('CAMERA_AUTH_ENABLED is set but admin credentials are incomplete.')
    if MAX_STREAMS < 1:
        errors.append('CAMERA_MAX_LIVE_STREAMS must be >= 1.')
    return {'ok': not errors, 'errors': errors, 'warnings': warnings}


def v62_startup_selftest() -> dict:
    errors, warnings = [], []
    missing = sorted(name for name in V62_REQUIRED_FUNCTIONS if name not in globals() or not callable(globals().get(name)))
    errors.extend('missing function: ' + name for name in missing)
    schema = _v62_schema_check()
    errors.extend(schema['errors']); warnings.extend(schema['warnings'])
    config = _v62_config_check()
    errors.extend(config['errors']); warnings.extend(config['warnings'])
    deps = _v62_dependency_check()
    if not deps['ffmpeg']['available']:
        warnings.append('ffmpeg executable not found; RTSP validation/live processing may be unavailable.')
    if not deps['ffprobe']['available']:
        warnings.append('ffprobe executable not found; advanced stream diagnostics may be unavailable.')
    result = {'ok': not errors, 'version': VERSION, 'timestamp': utc_iso(),
              'errors': errors, 'warnings': warnings, 'schema': schema,
              'dependencies': deps, 'config': config}
    V62_STARTUP_SELFTEST.update({'ran': True, 'ok': result['ok'], 'at': result['timestamp'],
                                 'errors': errors, 'warnings': warnings})
    if result['ok']:
        V62_STARTUP_SELFTEST['success_count'] = int(V62_STARTUP_SELFTEST.get('success_count',0)) + 1
    if not result['ok']:
        _record_recovery_event('startup_selftest', 'application', 'failed',
                               f"{len(errors)} errors, {len(warnings)} warnings")
    return result


def _v62_queue_health() -> dict:
    def _load():
        try:
            c = _db_conn()
            total = int(c.execute('SELECT COUNT(*) FROM validation_queue').fetchone()[0])
            due = int(c.execute("SELECT COUNT(*) FROM validation_queue WHERE next_attempt <= datetime('now')").fetchone()[0])
            locked = int(c.execute("SELECT COUNT(*) FROM validation_queue WHERE locked_until IS NOT NULL AND locked_until >= datetime('now')").fetchone()[0])
            stale = int(c.execute("SELECT COUNT(*) FROM validation_queue WHERE locked_until IS NOT NULL AND locked_until < datetime('now')").fetchone()[0])
            return {'ok': True, 'total': total, 'due': due, 'locked': locked, 'stale_claims': stale}
        except Exception as exc:
            return {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}
    return _cached_read('v62_queue_health', 2.0, _load)


def _v62_release_stale_claims() -> int:
    """Release expired validation leases without violating legacy NOT NULL schemas."""
    c = None
    try:
        c = _db_conn()
        with _DB_WRITE_LOCK:
            # validation_queue.locked_by is NOT NULL in migrated databases.
            # Use the empty-string sentinel rather than NULL.
            cur = c.execute(
                "UPDATE validation_queue SET locked_until=NULL, locked_by='' "
                "WHERE locked_until IS NOT NULL AND locked_until < datetime('now')"
            )
            c.commit()
            count = int(cur.rowcount or 0)
        if count:
            _record_recovery_event('validation_claim_cleanup', 'validation_queue', 'ok', f'released {count} stale claims')
        return count
    except Exception as exc:
        log.warning('Validation claim cleanup failed: %s', exc)
        return 0


def _v62_operational_snapshot() -> dict:
    try:
        disk = shutil.disk_usage(SCRIPT_DIR)
        return {'timestamp': utc_iso(), 'worker': _worker_health_snapshot(),
                'supervisor': _supervisor_snapshot(), 'resource': _resource_snapshot(),
                'validation': val_get_progress(), 'queue': _v62_queue_health(),
                'db': {'size': os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0,
                       'free_bytes': disk.free, 'integrity': _cached_read('live_integrity', 60.0, lambda: _sqlite_integrity_ok(DB_PATH))},
                'selftest': V62_STARTUP_SELFTEST.copy(),
                'write_state': _db_write_state()}
    except Exception as exc:
        return {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}


@app.route('/api/v62/self-test')
def v62_self_test():
    return jsonify(v62_startup_selftest())


@app.route('/api/v62/operations')
def v62_operations():
    return jsonify({'ok': True, 'operations': _v62_operational_snapshot()})


@app.route('/api/v62/queue-health')
def v62_queue_health():
    return jsonify({'ok': True, 'queue': _v62_queue_health()})


@app.route('/api/v62/queue-cleanup', methods=['POST'])
def v62_queue_cleanup():
    count = _v62_release_stale_claims()
    return jsonify({'ok': True, 'released': count, 'queue': _v62_queue_health()})


@app.route('/api/v62/sessions/devices')
def v62_session_devices():
    rows = _db_conn().execute("SELECT username,session_id,created_at,last_seen,revoked,ip,user_agent FROM v43_auth_sessions WHERE revoked=0 ORDER BY last_seen DESC LIMIT 1000").fetchall()
    items = []
    for r in rows:
        d = dict(r); d['active'] = not bool(d.get('revoked')); items.append(d)
    return jsonify({'ok': True, 'items': items})


@app.route('/api/v62/sessions/<session_id>/revoke', methods=['POST'])
def v62_revoke_session(session_id):
    if not _auth_enabled() or _current_role() != 'admin':
        return jsonify({'ok': False, 'detail': 'Admin required'}), 403
    c = _db_conn()
    cur = c.execute('UPDATE v43_auth_sessions SET revoked=1 WHERE session_id=?', (session_id,))
    c.commit()
    _advanced_audit('session_revoke', 'session', session_id, 'admin revoked session')
    return jsonify({'ok': True, 'revoked': int(cur.rowcount or 0)})


@app.route('/api/v62/config-health')
def v62_config_health():
    return jsonify({'ok': True, 'config': _v62_config_check(), 'dependencies': _v62_dependency_check(),
                    'limits': {'max_streams': MAX_STREAMS, 'queue_lease_minutes': V62_QUEUE_LEASE_MINUTES,
                               'worker_restart_limit': V62_RESTART_LIMIT,
                               'worker_restart_window_seconds': V62_RESTART_WINDOW}})


# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def _ensure_tg_template() -> None:
    # V162: plaintext Telegram configuration files are no longer generated.
    # Credentials are entered through the dashboard and stored encrypted in SQLite.
    return


def v111_regression_selftest():
    errors=[]
    try:
        if 'secret' in _mask_rtsp('rtsp://user:secret@example.com:554/live'): errors.append('RTSP mask failed')
        if _rtsp_host_port('rtsps://example.com:322/live')!=('example.com',322): errors.append('rtsps parse failed')
        if not _parse_valid_camera_block('Camera Type: Hikvision\nIP Address: 127.0.0.1\nRTSP: rtsp://127.0.0.1:554/live','selftest.txt'): errors.append('camera block parser failed')
    except Exception as exc: errors.append(f'{type(exc).__name__}: {exc}')
    if errors: log.error('[v111 self-test] FAILED: %s',errors)
    else: log.info('[v124 self-test] PASS')
    return {'ok':not errors,'errors':errors}


def main() -> None:
    global _SEARCH_DIR, MAX_STREAMS, VALIDATION_BATCH_OVERRIDE, DATA_DIR, SNAPSHOT_DIR, LOG_DIR, DB_PATH, DB_BACKUPS, DB_BACKUP_GLOB_PREFIX, TG_CONFIG, RTSP_SAVE

    parser = argparse.ArgumentParser(
        description=f'Camera Dashboard v{VERSION} — Single-File RTSP Viewer')
    # Hosted platforms (Railway, Render, Fly.io, Heroku-like platforms, Replit,
    # and container hosts) route traffic to the container's network interface.
    # Binding to 127.0.0.1 makes the app unreachable from outside the container.
    hosted_env = bool(
        os.getenv('PORT') or os.getenv('REPLIT_PORT') or os.getenv('RAILWAY_ENVIRONMENT')
        or os.getenv('RAILWAY_PROJECT_ID') or os.getenv('RENDER') or os.getenv('FLY_APP_NAME')
        or os.getenv('DYNO') or os.getenv('KOYEB_APP_NAME') or os.getenv('SPACE_ID')
        or os.getenv('REPL_ID') or os.getenv('REPLIT_DEV_DOMAIN') or os.getenv('REPLIT_DEPLOYMENT')
    )
    platform_port = os.getenv('PORT') or os.getenv('REPLIT_PORT')
    parser.add_argument('--port', type=int, default=int(platform_port or '5000'),
                        help='HTTP port (uses PORT/REPLIT_PORT on hosted platforms; otherwise 5000)')
    parser.add_argument('--host', default=('0.0.0.0' if hosted_env else '127.0.0.1'),
                        help='Bind host (hosted platforms require 0.0.0.0)')
    parser.add_argument('--dir',  default=SCRIPT_DIR,     help='Directory to scan for cameras')
    parser.add_argument('--data-dir', default=os.getenv('CAMERA_DATA_DIR', DATA_DIR), help='Runtime data directory for DB/logs/backups/snapshots/config')
    parser.add_argument('--no-browser', action='store_true', help='Do not auto-open browser')
    parser.add_argument('--workers', default=os.getenv('CAMERA_WORKERS', 'auto'),
                        help='Validation workers: auto or 1-64 (default: auto; explicit value is respected)')
    parser.add_argument('--validation-batch', type=int, default=VALIDATION_BATCH_OVERRIDE,
                        help='Validation queue batch size: 0=automatic, 1-10000')
    parser.add_argument('--max-streams', type=int, default=MAX_STREAMS,
                        help='Maximum simultaneous live feeds: 1-256')
    parser.add_argument('--import-user-config', nargs='?', const='__DEFAULT__', default=None, metavar='PATH',
                        help='Import a trusted user/config SQLite file during startup (no camera data). Without PATH, uses camera_dashboard_user_config.sqlite in --data-dir.')
    parser.add_argument('--no-auto-import-user-config', action='store_true',
                        help='Disable automatic import of camera_dashboard_user_config.sqlite when present in --data-dir.')
    parser.add_argument('--create-db-if-missing', action='store_true',
                        help='Create a fresh SQLite database only when the live DB is missing and no verified backup exists; never replace a corrupt existing DB.')
    args = parser.parse_args()
    # A deployment Start Command may explicitly contain --host 127.0.0.1 from
    # an older configuration. On hosted platforms, correct that unsafe binding
    # automatically so the deployment remains reachable without editing each
    # platform's start command. Local-only binding remains available locally.
    if hosted_env and args.host.strip().lower() in {'127.0.0.1', 'localhost', '::1'}:
        if os.getenv('CAMERA_ALLOW_LOOPBACK_HOST', '').strip().lower() not in {'1', 'true', 'yes'}:
            log.warning('[HTTP] Hosted environment detected; overriding loopback bind %s with 0.0.0.0', args.host)
            args.host = '0.0.0.0'
    # Fail closed for any non-loopback bind. A reachable dashboard must not
    # silently run as an unauthenticated admin when environment variables are missing.
    network_exposed = args.host.strip().lower() not in {'127.0.0.1', 'localhost', '::1'}
    if network_exposed and not (AUTH_ENABLED_CONFIG and AUTH_USER and AUTH_PASSWORD):
        message = ('SECURITY ERROR: network-exposed binding requires CAMERA_AUTH_ENABLED=1, '
                   'CAMERA_ADMIN_USER and a strong CAMERA_ADMIN_PASSWORD. '
                   'For local-only use, bind to 127.0.0.1.')
        print(message, file=sys.stderr, flush=True)
        raise SystemExit(2)
    if network_exposed:
        log.warning('[Security] Network bind requires authenticated sessions; secure_cookie=%s', SECURE_COOKIE)

    worker_arg = str(args.workers).strip().lower()
    if worker_arg != 'auto':
        if not worker_arg.isdigit() or not 1 <= int(worker_arg) <= 64:
            parser.error('--workers must be auto or an integer from 1 to 64')
    if not 0 <= int(args.validation_batch) <= 10000:
        parser.error('--validation-batch must be between 0 and 10000')
    if not 1 <= int(args.max_streams) <= 256:
        parser.error('--max-streams must be between 1 and 256')
    MAX_STREAMS = int(args.max_streams)
    VALIDATION_BATCH_OVERRIDE = int(args.validation_batch)
    _WORKER_STATE['mode'] = worker_arg
    startup_workers, startup_batch = _validation_worker_config()
    snap = _resource_snapshot()
    log.info('[Resources] mode=%s cpu=%d RAM=%.2fGB available=%.2fGB load1=%.2f -> effective_workers=%d queue_batch=%d',
             worker_arg, snap['cpu_count'], snap['ram_total_gb'], snap['ram_available_gb'],
             snap['load1'], startup_workers, startup_batch)

    os.umask(0o077)
    DATA_DIR=os.path.abspath(args.data_dir); os.makedirs(DATA_DIR,exist_ok=True)
    try:
        if os.path.commonpath([DATA_DIR, SCRIPT_DIR]) == os.path.abspath(SCRIPT_DIR):
            log.warning('[Storage/Git] Runtime data directory is inside the source tree. Configure --data-dir or CAMERA_DATA_DIR to a persistent data location and exclude DB/WAL/SHM/backups from Git/LFS to avoid large runtime files in source history.')
    except (ValueError, OSError):
        pass
    try: os.chmod(DATA_DIR,0o700)
    except OSError: pass
    SNAPSHOT_DIR=os.path.join(DATA_DIR,'snapshots'); LOG_DIR=os.path.join(DATA_DIR,'logs'); DB_PATH=os.path.join(DATA_DIR,'camera_db.sqlite'); DB_BACKUPS=[DB_PATH+'.bak']; DB_BACKUP_GLOB_PREFIX=DB_PATH+'.bak'; TG_CONFIG=os.path.join(DATA_DIR,'telegram_config.json'); RTSP_SAVE=os.path.join(DATA_DIR,'generated_rtsp_links.txt')
    global CLEAN_SHUTDOWN_MARKER
    CLEAN_SHUTDOWN_MARKER = DB_PATH + '.clean_shutdown.json'
    _SEARCH_DIR = os.path.abspath(args.dir)
    # v112 compatibility: never lose an existing legacy database merely because
    # the runtime data directory changed between releases. When the operator
    # explicitly requests --create-db-if-missing, however, keep the requested
    # data directory authoritative so a missing DB is created fresh instead of
    # silently adopting an old legacy DB or backup.
    if not args.create_db_if_missing:
        _adopt_legacy_database_path_if_needed()
    else:
        log.info('[DB Compatibility] --create-db-if-missing active; legacy DB/backup adoption is disabled for this startup.')
    # Rebind the clean-shutdown marker after any legacy DB adoption so the marker
    # always follows the actual live database path.
    CLEAN_SHUTDOWN_MARKER = DB_PATH + '.clean_shutdown.json'
    # Acquire the lock only after --data-dir / legacy DB adoption has resolved
    # the final DB_PATH. This prevents two dashboard processes from sharing the
    # same SQLite database while still allowing separate data directories.
    _acquire_pid_lock()
    _lifecycle_event('startup', f'host={args.host} port={args.port}')
    atexit.register(lambda: _lifecycle_event('shutdown', 'process exiting; graceful reason may be unavailable'))
    _ensure_tg_template()
    db_init(allow_create_if_missing=bool(args.create_db_if_missing))
    _v43_ensure_schema()
    # Optional one-time import: explicit --import-user-config PATH always wins;
    # otherwise import the standard file when present unless auto-import is disabled.
    default_transfer = os.path.join(DATA_DIR, 'camera_dashboard_user_config.sqlite')
    explicit_transfer = args.import_user_config is not None
    if explicit_transfer:
        transfer_path = default_transfer if args.import_user_config == '__DEFAULT__' else os.path.abspath(args.import_user_config)
    elif not args.no_auto_import_user_config and os.path.isfile(default_transfer):
        transfer_path = default_transfer
    else:
        transfer_path = None
    if transfer_path:
        try:
            log.info('[UserConfig] Startup import requested from %s', transfer_path)
            import_result = _import_user_config_database_file(transfer_path)
            log.info('[UserConfig] Startup import %s: users=%d settings=%d other_config_rows=%d telegram=%s services=%s',
                     'completed' if import_result.get('complete', True) else 'PARTIAL',
                     import_result['imported_users'], import_result['imported_settings'],
                     import_result['imported_other_rows'],
                     'ok' if import_result.get('telegram_imported', True) else 'not-imported',
                     'ok' if import_result.get('services_imported', True) else 'not-imported')
            # Never archive a source transfer while any included configuration
            # failed to import. It remains available for a dependency/key fix.
            if import_result.get('complete', True) and not explicit_transfer and os.path.abspath(transfer_path) == os.path.abspath(default_transfer):
                archived = default_transfer + '.imported.' + datetime.now().strftime('%Y%m%d_%H%M%S')
                try:
                    os.replace(default_transfer, archived)
                    log.info('[UserConfig] Imported file archived as %s to prevent repeated startup imports', archived)
                except OSError as archive_exc:
                    log.warning('[UserConfig] Import succeeded but source file could not be archived: %s', archive_exc)
        except FileNotFoundError:
            if explicit_transfer:
                raise RuntimeError('User/config transfer file not found: ' + transfer_path)
            log.info('[UserConfig] No default transfer file present; startup import skipped')
        except Exception as import_exc:
            log.exception('[UserConfig] Startup import failed; existing database was not replaced')
            if explicit_transfer:
                raise RuntimeError('User/config startup import failed: ' + str(import_exc)[:300]) from import_exc
            # Automatic import is best-effort: keep the server available and leave
            # the original file in place for operator inspection/retry.
    # Install signal handling before background workers start so SIGTERM/SIGINT
    # cannot arrive during startup before the graceful-shutdown barrier exists.
    _install_shutdown_signal_handlers()
    # Inventory credentials are encrypted at rest and require a persistent secret.
    # Fail during startup instead of waiting for a job to fail later.
    if _auth_enabled() and not os.getenv('SESSION_SECRET'):
        raise RuntimeError('SESSION_SECRET must be configured when authentication is enabled.')
    try:
        inv_pending = int(_db_conn().execute("SELECT COUNT(*) FROM inventory_jobs WHERE status IN ('queued','running')").fetchone()[0])
    except Exception:
        inv_pending = 0
    if inv_pending and not os.getenv('SESSION_SECRET'):
        raise RuntimeError('SESSION_SECRET must be configured to resume protected inventory jobs.')
    # Keep only authentication/schema prerequisites on the critical startup
    # path. Expensive maintenance, stale-claim recovery, self-tests, metadata
    # reconciliation and inventory-job resume are moved behind HTTP readiness.
    # This is the key rule: a healthy database must not make the dashboard
    # inaccessible merely because maintenance is slow.
    try:
        _ensure_access_scope_tables()
    except Exception:
        log.exception('Access-scope table initialization failed')
        raise
    # v84: restore the security epoch before HTTP starts so existing sessions
    # remain usable immediately after a normal restart.
    _load_persistent_auth_epoch()

    # Register Telegram shutdown notification
    atexit.register(_tg_notify_shutdown_once)

    def _startup_worker():
        # HTTP is already available at this point. Everything below is allowed
        # to take seconds or minutes without blocking the browser/login page.
        # Give the server a short head-start so Replit/local health probes can
        # connect before maintenance begins.
        try:
            time.sleep(max(0.0, float(os.getenv('CAMERA_POST_HTTP_START_DELAY', '2'))))
        except Exception:
            pass

        if _shutdown_requested.is_set():
            log.info('[Startup] Deferred startup cancelled: shutdown requested')
            return
        log.info('[Startup] HTTP is ready; beginning deferred startup maintenance')
        try:
            if _shutdown_requested.is_set(): return
            v111_regression_selftest()
        except Exception:
            log.exception('Deferred regression self-test failed')
        try:
            if _shutdown_requested.is_set(): return
            _v62_release_stale_claims()
        except Exception:
            if not _shutdown_requested.is_set():
                log.exception('Deferred stale-claim recovery failed')
        try:
            if _shutdown_requested.is_set(): return
            selftest = v62_startup_selftest()
            if not selftest.get('ok'):
                log.error('[v62 self-test] FAILED: %s', selftest.get('errors'))
            elif selftest.get('warnings'):
                log.warning('[v62 self-test] warnings: %s', selftest.get('warnings'))
            else:
                log.info('[v62 self-test] PASS')
        except Exception:
            log.exception('[v62 self-test] deferred execution failed')

        # Advanced Metadata Sync is an operator-controlled background service.
        # Do not run startup reconciliation behind the operator's OFF switch.
        if _service_enabled('advanced-metadata') and not _shutdown_requested.is_set():
            try:
                started = time.monotonic()
                processed = _advanced_reconcile_all(3000)
                log.info('[Startup] Advanced metadata reconciliation complete in %.2fs (processed=%d)', time.monotonic() - started, processed)
            except Exception:
                log.exception('Advanced metadata bootstrap failed')
        else:
            _touch_worker('startup-metadata','stopped','disabled by operator' if not _service_enabled('advanced-metadata') else 'shutdown requested')

        # Resume protected inventory jobs after the HTTP endpoint is available.
        try:
            if _shutdown_requested.is_set(): return
            c = _db_conn()
            c.execute("UPDATE inventory_jobs SET status='queued', finished_at=NULL, error='server_restart_resume' WHERE status IN ('queued','running')")
            c.commit()
            for row in c.execute("SELECT id,filename,targets_json FROM inventory_jobs WHERE status='queued' AND targets_json!='[]' ORDER BY created_at").fetchall():
                try:
                    targets = _unprotect_inventory_targets(row['targets_json'])
                    if not isinstance(targets, list) or not targets:
                        continue
                    thread = threading.Thread(target=_run_inventory_job, args=(row['id'], targets, row['filename']), daemon=True, name=f'inventory-resume-{row["id"][:8]}')
                    with _inventory_jobs_lock:
                        _inventory_job_threads[row['id']] = thread
                    thread.start()
                    log.info('Resumed inventory job %s with %d targets', row['id'], len(targets))
                except Exception:
                    log.exception('Could not resume inventory job %s', row['id'])
        except Exception:
            log.exception('Could not reconcile inventory jobs after restart')

        if _shutdown_requested.is_set():
            return
        try:
            start_validation_queue_worker(max_workers=None, delay=1.0)
            log.info('[Startup] Validation queue worker started before discovery; new cameras can be checked during the scan')
        except Exception:
            log.exception('[Startup] Could not start early validation queue worker')
        log.info('Background startup: scanning %s …', _SEARCH_DIR)
        try:
            _scan_and_upsert()
            cameras = []
            log.info('Startup scan complete — streamed discovery batches into database')
        except Exception as e:
            if _shutdown_requested.is_set() and 'cancelled during shutdown' in str(e).lower():
                log.info('[Startup] Scan stopped cleanly because shutdown was requested')
            else:
                log.error('Startup scan error: %s', e, exc_info=True)
            cameras = []

        if not _shutdown_requested.is_set():
            # The persistent queue worker is the sole validation scheduler. Avoid
            # claiming a second giant startup batch that would block newly found
            # cameras behind a long one-shot validation cycle.
            log.info('[Startup] Persistent validation queue remains the single validation runner for pending and newly discovered cameras')

        # Notifications must not be allowed to make database/startup recovery fail.
        try:
            if _shutdown_requested.is_set(): return
            tg_notify_startup(args.port)
        except Exception as e:
            log.warning('Telegram startup notification failed: %s', e)

    threading.Thread(target=_startup_worker,   daemon=True, name='startup').start()
    if _service_enabled('file-scanner'):
        t_scan=threading.Thread(target=_background_scanner, daemon=True, name='cam-scanner'); _service_register_thread('file-scanner', t_scan); t_scan.start()
    else:
        _touch_worker('file-scanner','stopped','disabled by operator')
    backup_interval = max(300.0, float(os.getenv(
        'CAMERA_BACKUP_INTERVAL_SECONDS', '1800')))
    # Do not start the first SQLite backup at the exact moment the HTTP server
    # is coming online. On Replit a cold-start backup can briefly compete with
    # login/session writes and make the preview appear hung. The worker itself
    # still runs at the configured interval after this short startup grace.
    def _delayed_backup_start():
        try:
            time.sleep(45.0)
            if not _shutdown_requested.is_set() and _service_enabled('database-backup'):
                _service_register_thread('database-backup', start_database_backup_worker(interval=backup_interval))
        except Exception:
            log.exception('Delayed database backup worker failed to start')
    threading.Thread(target=_delayed_backup_start, daemon=True, name='db-backup-delay').start()
    if _service_enabled('health-supervisor'): _service_register_thread('health-supervisor', start_v40_health_supervisor())
    else: _touch_worker('health-supervisor','stopped','disabled by operator')
    if _service_enabled('schedule-supervisor'): _service_register_thread('schedule-supervisor', start_v40_schedule_supervisor())
    else: _touch_worker('schedule-supervisor','stopped','disabled by operator')
    if _service_enabled('feature-supervisor'): _service_register_thread('feature-supervisor', start_v43_feature_supervisor())
    else: _touch_worker('v43-feature-supervisor','stopped','disabled by operator')
    # v61: supervisor is started last so it can observe every long-running worker.
    _register_worker_restarter('validation-queue', lambda: start_validation_queue_worker(max_workers=None, delay=1.0))
    _register_worker_restarter('db-backup', lambda: start_database_backup_worker(interval=backup_interval))
    _register_worker_restarter('v43-feature-supervisor', start_v43_feature_supervisor)
    _register_worker_restarter('health-supervisor', start_v40_health_supervisor)
    _register_worker_restarter('schedule-supervisor', start_v40_schedule_supervisor)
    _register_worker_restarter('file-scanner', lambda: threading.Thread(target=_background_scanner, daemon=True, name='cam-scanner-restart').start())
    _register_worker_restarter('worker-supervisor', lambda: None)
    if _service_enabled('worker-supervisor'):
        _service_register_thread('worker-supervisor', start_worker_supervisor(interval=max(15, int(os.getenv('CAMERA_WORKER_SUPERVISOR_INTERVAL','30')))))
    else:
        _touch_worker('worker-supervisor','stopped','disabled by operator')

    # Never try to open a local browser in hosted/Replit environments.
    hosted = hosted_env
    if not args.no_browser and not hosted:
        def _open_when_ready():
            deadline=time.monotonic()+20
            while time.monotonic()<deadline and not _shutdown_requested.is_set():
                try:
                    with socket.create_connection(('127.0.0.1',args.port),timeout=0.4): webbrowser.open(f'http://127.0.0.1:{args.port}'); return
                except OSError: time.sleep(0.25)
        threading.Thread(target=_open_when_ready,daemon=True,name='browser-open').start()

    border = '═' * 64
    print(f'\n{border}')
    print(f'  📷  Camera Dashboard  v{VERSION}  — Single File Edition')
    print(f'{border}')
    print(f'  Scan dir  : {_SEARCH_DIR}')
    print(f'  Database  : {DB_PATH}')
    print(f'  Snapshots : {SNAPSHOT_DIR}')
    if Fernet is None:
        telegram_banner = '✗ cryptography missing — secure Telegram import disabled'
    else:
        telegram_banner = '✓ encrypted config' if _load_tg_destinations() else '✗ not configured — use Telegram button'
    print(f'  Telegram  : {telegram_banner}')
    print(f'{"─" * 64}')
    print(f'  🌐  Open  : http://{args.host}:{args.port}')
    print(f'  ❤️  Health : http://{args.host}:{args.port}/healthz')
    if hosted:
        print('  ☁️  Hosted platform detected — using platform PORT and 0.0.0.0 binding')
    print(f'  Logs      : {os.path.join(LOG_DIR, "dashboard.log")}')
    print(f'  Services  : {BACKGROUND_SERVICES_CONFIG_PATH} (persistent ON/OFF)')
    print(f'  Scanning camera files in background…\n')

    log.info('[HTTP] Startup critical path complete; binding Flask server now on %s:%d (platform PORT=%s)',
             args.host, args.port, os.getenv('PORT') or os.getenv('REPLIT_PORT') or 'unset')
    try:
        app.run(host=args.host, port=args.port, debug=False, threaded=True, use_reloader=False)
    except OSError as exc:
        log.exception('[HTTP] Could not bind port %d on %s: %s', args.port, args.host, exc)
        raise



# v110 reliability/operations center
def _system_db_health():
    st=_cached_read('storage_status',10.0,_storage_status); db_size=os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    wal_size=os.path.getsize(DB_PATH+'-wal') if os.path.exists(DB_PATH+'-wal') else 0
    backup=None
    if DB_BACKUPS and os.path.exists(DB_BACKUPS[0]):
        backup={'name':os.path.basename(DB_BACKUPS[0]),'size':os.path.getsize(DB_BACKUPS[0]),'mtime':datetime.fromtimestamp(os.path.getmtime(DB_BACKUPS[0]),timezone.utc).isoformat(),'integrity':_cached_read('backup_integrity:'+DB_BACKUPS[0],60.0,lambda:_sqlite_integrity_ok(DB_BACKUPS[0]))}
    return {'storage':st,'database':{'path':DB_PATH,'size':db_size,'wal_bytes':wal_size,'integrity':_cached_read('live_integrity',60.0,lambda:_sqlite_integrity_ok(DB_PATH))},'backup':backup,'maintenance':_db_maintenance_snapshot(),'scan':_scan_state_snapshot(),'validation':val_get_progress()}

@app.route('/api/v110/health')
def v110_health(): return jsonify({'ok':True,**_system_db_health()})

@app.route('/api/v110/scan/status')
def v110_scan_status(): return jsonify({'ok':True,'scan':_scan_state_snapshot(),'storage':_storage_status()})

@app.route('/api/v110/scan/control',methods=['POST'])
def v110_scan_control():
    action=str((request.get_json(silent=True) or {}).get('action') or '').lower()
    if action=='pause': _SCAN_PAUSE.set(); _scan_state_set(paused=True,phase='paused'); return jsonify({'ok':True,'scan':_scan_state_snapshot()})
    if action=='resume': _SCAN_PAUSE.clear(); _scan_state_set(paused=False,phase='discovering'); return jsonify({'ok':True,'scan':_scan_state_snapshot()})
    if action=='cancel': _SCAN_CANCEL.set(); _SCAN_PAUSE.clear(); _scan_state_set(cancel_requested=True,phase='cancelling'); return jsonify({'ok':True,'scan':_scan_state_snapshot()})
    if action in {'start','full-rescan'}:
        force=action=='full-rescan'
        if _scan_state_snapshot().get('running'): return jsonify({'ok':False,'detail':'Scan already running','scan':_scan_state_snapshot()}),409
        threading.Thread(target=lambda: _scan_and_upsert(force=force),daemon=True,name='v110-scan').start(); return jsonify({'ok':True,'message':'scan started','force':force})
    return jsonify({'ok':False,'detail':'Unknown scan action'}),400

@app.route('/api/v110/backup/list')
def v110_backup_list():
    items=[]
    for p in DB_BACKUPS:
        if os.path.exists(p): items.append({'name':os.path.basename(p),'size':os.path.getsize(p),'mtime':datetime.fromtimestamp(os.path.getmtime(p),timezone.utc).isoformat(),'integrity':_sqlite_integrity_ok(p)})
    return jsonify({'ok':True,'items':items,'health':_system_db_health()})

@app.route('/api/v110/backup/create',methods=['POST'])
def v110_backup_create():
    ok=db_create_backup(clean_old=True); return jsonify({'ok':ok,'health':_system_db_health()}),(200 if ok else 507)

@app.route('/api/v110/backup/restore',methods=['POST'])
def v110_backup_restore():
    src=DB_BACKUPS[0] if DB_BACKUPS else ''
    if not src or not os.path.exists(src) or not _sqlite_integrity_ok(src): return jsonify({'ok':False,'detail':'Verified backup unavailable'}),400
    tmp=DB_PATH+'.restore-v110.tmp'
    try:
        shutil.copy2(src,tmp)
        if not _sqlite_integrity_ok(tmp): raise RuntimeError('backup integrity failed')
        os.replace(tmp,DB_PATH)
        for side in (DB_PATH+'-wal',DB_PATH+'-shm'):
            try: os.remove(side)
            except FileNotFoundError: pass
        return jsonify({'ok':_sqlite_integrity_ok(DB_PATH),'detail':'Restore complete; restart application to reopen DB connections safely'})
    except Exception as exc:
        try: os.remove(tmp)
        except OSError: pass
        return jsonify({'ok':False,'detail':str(exc)}),500

@app.route('/api/v110/maintenance/shrink',methods=['POST'])
def v110_shrink():
    result=db_compact_and_backup(_current_user()); return jsonify(result),(200 if result.get('ok') else 409)

def _redact_diagnostic_text(text: str) -> str:
    text=_mask_rtsp(text or '')
    text=re.sub(r'(?im)^(\s*Authorization\s*:\s*).+$', r'\1[REDACTED]', text)
    text=re.sub(r'(?im)^(\s*Cookie\s*:\s*).+$', r'\1[REDACTED]', text)
    text=re.sub(r'(?i)(session(?:id|_id)?|token|api[_-]?key|password)=[^&\s]+', r'\1=[REDACTED]', text)
    text=re.sub(r'(?i)(\"(?:password|passwd|token|access_token|refresh_token|api[_-]?key|authorization|cookie|bot_token|secret)\"\s*:\s*)\"[^\"]*\"', r'\1\"[REDACTED]\"', text)
    text=re.sub(r'(?i)(\"(?:password|passwd|token|access_token|refresh_token|api[_-]?key|authorization|cookie|bot_token|secret)\"\s*:\s*)[^,}\n]+', r'\1\"[REDACTED]\"', text)
    return text

@app.route('/api/v110/diagnostics/bundle')
def v110_diag_bundle():
    out=os.path.join(DATA_DIR,f'diagnostic_bundle_{datetime.now().strftime("%Y%m%d_%H%M%S")}.zip')
    try:
        with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
            logp=os.path.join(LOG_DIR,'dashboard.log')
            if os.path.exists(logp): z.writestr('dashboard_sanitized.log',_redact_diagnostic_text(open(logp,'r',encoding='utf-8',errors='ignore').read()))
            z.writestr('system_health.json',json.dumps(_system_db_health(),indent=2,default=str))
            z.writestr('runtime.json',json.dumps({'version':VERSION,'python':sys.version,'platform':platform.platform(),'executable':sys.executable,'data_dir':DATA_DIR,'scan_dir':_SEARCH_DIR,'ffmpeg':shutil.which('ffmpeg'),'ffprobe':shutil.which('ffprobe')},indent=2,default=str))
        def _cleanup_diag_file(path=out):
            try: time.sleep(120); os.remove(path)
            except OSError: pass
        threading.Thread(target=_cleanup_diag_file,daemon=True,name='diag-bundle-cleanup').start()
        return jsonify({'ok':True,'path':out,'name':os.path.basename(out),'cleanup_seconds':120})
    except Exception as exc: return jsonify({'ok':False,'detail':str(exc)}),500

@app.route('/api/v110/setup/check')
def v110_setup_check():
    checks=[]
    vals=[('python',True,sys.version.split()[0]),('ffmpeg',bool(shutil.which('ffmpeg')),shutil.which('ffmpeg') or 'missing'),('ffprobe',bool(shutil.which('ffprobe')),shutil.which('ffprobe') or 'missing'),('data_dir',os.path.isdir(DATA_DIR),DATA_DIR),('data_writable',os.access(DATA_DIR,os.W_OK),DATA_DIR),('database',os.path.exists(DB_PATH),DB_PATH)]
    for name,ok,detail in vals: checks.append({'name':name,'ok':ok,'detail':detail})
    return jsonify({'ok':all(x['ok'] for x in checks),'checks':checks,'storage':_storage_status()})

@app.route('/api/v110/retention',methods=['GET','POST'])
def v110_retention():
    global HISTORY_RETENTION_DAYS, SNAPSHOT_RETENTION_DAYS, DELETED_RETENTION_DAYS
    c=_db_conn()
    if request.method=='POST':
        data=request.get_json(silent=True) or {}
        for k,v in data.items():
            if k not in {'history_days','snapshot_days','deleted_days'}: continue
            iv=max(1,int(v))
            if k=='history_days': HISTORY_RETENTION_DAYS=max(7,iv)
            elif k=='snapshot_days': SNAPSHOT_RETENTION_DAYS=max(1,iv)
            elif k=='deleted_days': DELETED_RETENTION_DAYS=max(1,iv)
            _db_retry_write(lambda k=k,v=iv: _db_conn().execute('INSERT INTO app_retention(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(k,str(v))).connection.commit(),'retention')
    rows=c.execute('SELECT key,value FROM app_retention ORDER BY key').fetchall(); return jsonify({'ok':True,'items':{r['key']:r['value'] for r in rows}})

@app.route('/api/v110/api-keys',methods=['GET','POST','DELETE'])
def v110_api_keys():
    c=_db_conn()
    if request.method=='GET':
        rows=c.execute('SELECT id,name,permissions_json,enabled,created_at,last_used FROM api_keys ORDER BY id DESC').fetchall(); return jsonify({'ok':True,'items':[dict(r) for r in rows]})
    data=request.get_json(silent=True) or {}
    if request.method=='DELETE': _db_retry_write(lambda: _db_conn().execute('DELETE FROM api_keys WHERE id=?',(int(data.get('id') or 0),)).connection.commit(),'delete api key'); return jsonify({'ok':True})
    name=str(data.get('name') or '').strip()[:100]
    if not name:return jsonify({'ok':False,'detail':'name required'}),400
    raw=secrets.token_urlsafe(32); h=hashlib.sha256(raw.encode()).hexdigest(); perms=data.get('permissions') or ['dashboard.view']
    try: _db_retry_write(lambda: _db_conn().execute('INSERT INTO api_keys(name,key_hash,permissions_json,owner_username) VALUES(?,?,?,?)',(name,h,json.dumps(perms),_current_user())).connection.commit(),'create api key')
    except sqlite3.IntegrityError:return jsonify({'ok':False,'detail':'key name already exists'}),409
    return jsonify({'ok':True,'name':name,'api_key':raw,'warning':'Save this key now; it will not be shown again.'})

@app.route('/api/v110/cameras/<cam_id>/notes',methods=['GET','POST'])
def v110_camera_notes(cam_id):
    if request.method=='GET':
        r=_db_conn().execute('SELECT * FROM camera_notes WHERE camera_id=?',(cam_id,)).fetchone(); return jsonify({'ok':True,'item':dict(r) if r else {}})
    d=request.get_json(silent=True) or {}
    _db_retry_write(lambda: _db_conn().execute('INSERT INTO camera_notes(camera_id,owner,model,install_details,maintenance_notes,updated_at) VALUES(?,?,?,?,?,datetime(\'now\')) ON CONFLICT(camera_id) DO UPDATE SET owner=excluded.owner,model=excluded.model,install_details=excluded.install_details,maintenance_notes=excluded.maintenance_notes,updated_at=datetime(\'now\')',(cam_id,str(d.get('owner') or '')[:200],str(d.get('model') or '')[:200],str(d.get('install_details') or '')[:2000],str(d.get('maintenance_notes') or '')[:4000])).connection.commit(),'camera notes')
    return jsonify({'ok':True})

@app.route('/system-center')
def v110_system_center():
    if _auth_enabled() and not _session_valid(): return redirect(url_for('login',next='/system-center'))
    html = """<!doctype html><html><head><meta charset='utf-8'><meta name='csrf-token' content='__CSRF_TOKEN__'><title>Camera Dashboard System Center</title><style>body{font:14px Arial;background:#111820;color:#ddd;margin:20px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}.card{border:1px solid #39434e;background:#171e27;border-radius:8px;padding:14px}button{background:#243447;color:#fff;border:1px solid #506070;border-radius:5px;padding:7px 10px;margin:3px;cursor:pointer}pre{white-space:pre-wrap;max-height:360px;overflow:auto}</style></head><body><h2>🛠 Camera Dashboard — System Center</h2><div class='grid'><div class='card'><h3>Storage / Database</h3><pre id='health'>Loading…</pre><button onclick='load()'>Refresh</button><button onclick='backup()'>Create Backup</button><button onclick='shrink()'>Shrink DB</button><button onclick='exportCfg()'>Export User/Config</button><button onclick='document.querySelector("#cfgfile").click()'>Import User/Config</button><input id='cfgfile' type='file' accept='application/vnd.sqlite3,.sqlite,.db' style='display:none' onchange='importCfg(this)'></div><div class='card'><h3>Scan controls</h3><pre id='scan'>Loading…</pre><button onclick='scan("pause")'>Pause</button><button onclick='scan("resume")'>Resume</button><button onclick='scan("cancel")'>Cancel</button><button onclick='scan("full-rescan")'>Full Rescan</button></div><div class='card'><h3>Setup / Support</h3><pre id='setup'>Loading…</pre><button onclick='diag()'>Diagnostic Bundle</button></div></div><script>const __csrf=document.querySelector('meta[name=csrf-token]')?.content||'';async function j(u,o){o=o||{};o.headers=new Headers(o.headers||{});if(/^(POST|PUT|PATCH|DELETE)$/i.test(o.method||'GET'))o.headers.set('X-CSRF-Token',__csrf);let r=await fetch(u,o);return await r.json()}async function load(){let x=await j('/api/v110/health');document.querySelector('#health').textContent=JSON.stringify(x,null,2);let s=await j('/api/v110/scan/status');document.querySelector('#scan').textContent=JSON.stringify(s,null,2);let q=await j('/api/v110/setup/check');document.querySelector('#setup').textContent=JSON.stringify(q,null,2)}async function scan(a){await j('/api/v110/scan/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:a})});load()}async function backup(){await j('/api/v110/backup/create',{method:'POST'});load()}async function shrink(){if(!confirm('Shrink SQLite now? This may take time and pauses validation.'))return;let x=await j('/api/admin/shrink',{method:'POST'});if(!x.ok)throw Error(x.detail||'Could not start shrink');for(let i=0;i<1440;i++){await new Promise(r=>setTimeout(r,2500));x=await j('/api/admin/shrink/status');if(x.status==='completed'||x.status==='failed')break;}if(x.status==='completed'&&x.result?.ok)alert('Shrink finished. Saved '+((x.result.saved_bytes||0)/1048576).toFixed(1)+' MB; DB '+((x.result.old_size||0)/1048576).toFixed(1)+' → '+((x.result.new_size||0)/1048576).toFixed(1)+' MB.');else alert('Shrink: '+(x.result?.detail||x.status||'unknown status'));load()}async function exportCfg(){if(!confirm('Export sensitive user/config data and secrets? No camera data is included. Keep the file private.'))return;location.href='/api/admin/user-config/export'}async function importCfg(el){if(!el.files?.[0])return;if(!confirm('Import a trusted user/config SQLite database? Matching settings/users may be updated. No cameras are imported.')){el.value='';return}let f=new FormData();f.append('file',el.files[0]);let x=await j('/api/admin/user-config/import',{method:'POST',body:f});alert(x.detail||JSON.stringify(x));el.value='';load()}async function diag(){let x=await j('/api/v110/diagnostics/bundle');alert(x.ok?'Created: '+x.name:x.detail)}load();setInterval(load,3000)</script></body></html>""".replace('__CSRF_TOKEN__', _csrf_token())
    return Response(html,mimetype='text/html')

@app.errorhandler(404)
def _json_404(error):
    if request.path.startswith('/api/'):
        return jsonify({'ok': False, 'detail': 'API endpoint not found', 'path': request.path}), 404
    return error


@app.errorhandler(405)
def _json_405(error):
    if request.path.startswith('/api/'):
        return jsonify({'ok': False, 'detail': 'HTTP method not allowed', 'path': request.path}), 405
    return error


@app.errorhandler(500)
def _json_500(error):
    if request.path.startswith('/api/'):
        log.exception('Unhandled API exception: %s', error)
        return jsonify({'ok': False, 'detail': 'Internal server error'}), 500
    return error


# ══════════════════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    main()
