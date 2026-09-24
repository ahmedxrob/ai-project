import asyncio
import binascii
import hashlib
import math
import json
import os
import random
import re
import shutil
import sys
import sqlite3
import subprocess
import time
import urllib.parse
import urllib.request
import difflib
import unicodedata
import uuid
import secrets
import xml.etree.ElementTree as ET
import zipfile
import tempfile
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Optional
from collections import defaultdict

from fastapi import (
    Body,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    UploadFile,
    File,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask


# ============================================================
# XROB MUSIC
# Downloader + Library + OpenSubsonic server
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
SERVER_VERSION = "3.7.2"

@asynccontextmanager
async def app_lifespan(_app):
    await startup_event()
    try:
        yield
    finally:
        tasks = [*DOWNLOAD_WORKER_TASKS, *BACKGROUND_TASKS]
        if SCHEDULED_SCANNER_TASK is not None:
            tasks.append(SCHEDULED_SCANNER_TASK)
        if LIBRARY_WARMUP_TASK is not None:
            tasks.append(LIBRARY_WARMUP_TASK)
        for task in tasks:
            if task and not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        DOWNLOAD_WORKER_TASKS.clear()
        BACKGROUND_TASKS.clear()


app = FastAPI(
    title="Xrob Music",
    version=SERVER_VERSION,
    lifespan=app_lifespan,
)

@app.middleware("http")
async def web_auth_middleware(request: Request, call_next):
    path = request.url.path
    # OpenSubsonic and static assets keep their existing authentication behavior.
    if path.startswith("/rest/") or path.startswith("/static/") or path in {"/api/health", "/api/auth/login", "/api/auth/status", "/api/auth/logout", "/favicon.ico"}:
        return await call_next(request)
    if path.startswith("/api/") and not _is_authenticated(request.cookies.get(AUTH_COOKIE)):
        return JSONResponse({"detail":"Authentication required"}, status_code=401)
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv("XROB_CORS_ORIGINS", "").split(",") if origin.strip()],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Requested-With"],
)

app.mount(
    "/static",
    StaticFiles(directory=STATIC_DIR),
    name="static",
)


# ============================================================
# CONFIGURATION
# ============================================================

DEFAULT_LIBRARY_PATH = os.getenv(
    "DOWNLOAD_DIR",
    "/media/xrob-music",
)

# The library path can be changed by the Home Assistant add-on through
# /data/options.json. The legacy DOWNLOAD_DIR environment variable remains
# supported for Docker users.
DOWNLOAD_DIR = Path(DEFAULT_LIBRARY_PATH)
COVER_CACHE_DIR = DOWNLOAD_DIR / ".covers"
DATA_DIR = Path(os.getenv("XROB_DATA_DIR", "/data"))
DB_FILE = DATA_DIR / "tasks.db"
SETTINGS_FILE = DATA_DIR / "settings.json"
AUTH_USER = os.getenv("XROB_USERNAME", "admin")
AUTH_PASSWORD = os.getenv("XROB_PASSWORD", "")
AUTH_COOKIE = "xrob_session"
AUTH_MIN_PASSWORD_LENGTH = 12
AUTH_SESSIONS = {}
AUTH_LOGIN_ATTEMPTS = defaultdict(list)
AUTH_LOGIN_WINDOW = 300
AUTH_LOGIN_MAX_ATTEMPTS = 5
AUTH_BOOTSTRAP_FILE = DATA_DIR / "web_bootstrap.txt"
PLAYER_STATE = None
PLAYER_STATE_UPDATED_AT = 0.0
PLAYER_STATE_MAX_AGE_SECONDS = 12.0
PLAYER_STATE_ACTIVE_HEARTBEAT_SECONDS = 6.0
PLAYER_STATE_HEARTBEAT_PERSIST_SECONDS = 5.0
PLAYER_STATE_MAX_QUEUE_ITEMS = 500
PLAYER_STATE_MAX_DAILY_MIX_ITEMS = 100
PLAYER_STATE_MAX_SYNC_DEVICES = 12
PLAYER_STATE_MAX_TEXT = 512
PLAYER_STATE_MAX_URL = 4096
PLAYER_STATE_PERSIST_INTERVAL_SECONDS = 5.0
PLAYER_STATE_DB_KEY = "default"
PLAYER_STATE_LAST_PERSISTED_AT = 0.0
PLAYER_STATE_LOCK = asyncio.Lock()


def _auth_token():
    return secrets.token_urlsafe(32)

def _hash_web_password(password):
    if not password:
        return ""
    iterations = 310000
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"

def _verify_web_password(password, stored):
    stored = str(stored or "")
    if not stored:
        return False, False
    if not stored.startswith("pbkdf2_sha256$"):
        # Legacy plaintext credentials are accepted once and migrated after success.
        return secrets.compare_digest(password, stored), True
    try:
        _, iterations, salt_hex, digest_hex = stored.split("$", 3)
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations))
        return secrets.compare_digest(actual, expected), False
    except Exception:
        return False, False

def _stored_web_password(settings):
    return str(settings.get("web_password_hash") or settings.get("web_password") or AUTH_PASSWORD or "")

def _write_settings_sync(settings):
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_name(f".{SETTINGS_FILE.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, SETTINGS_FILE)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass

def _ensure_secure_web_credentials_sync():
    settings = load_settings()
    explicit_env = os.getenv("XROB_PASSWORD")
    if explicit_env:
        return settings
    username = str(settings.get("web_username") or AUTH_USER or "admin")[:64]
    stored = str(settings.get("web_password_hash") or settings.get("web_password") or "")
    # Upgrade the insecure legacy default or an empty password before serving requests.
    if not stored or stored == "admin":
        password = secrets.token_urlsafe(18)
        settings["web_username"] = username
        settings["web_password_hash"] = _hash_web_password(password)
        settings.pop("web_password", None)
        _write_settings_sync(settings)
        try:
            AUTH_BOOTSTRAP_FILE.write_text(
                f"Xrob Music initial web login\nusername={username}\npassword={password}\n",
                encoding="utf-8",
            )
            os.chmod(AUTH_BOOTSTRAP_FILE, 0o600)
        except OSError:
            pass
        print(f"Xrob Music: generated a secure initial web password. Read {AUTH_BOOTSTRAP_FILE}")
    elif stored and not stored.startswith("pbkdf2_sha256$"):
        # Migrate an existing custom plaintext password to a one-way verifier.
        settings["web_password_hash"] = _hash_web_password(stored)
        settings.pop("web_password", None)
        _write_settings_sync(settings)
    return settings

def _current_web_credentials():
    settings = load_settings()
    return str(settings.get("web_username") or AUTH_USER or "admin"), _stored_web_password(settings)

def _auth_client_key(request: Request):
    return request.client.host if request.client else "unknown"

def _login_blocked(request: Request):
    now = time.time()
    key = _auth_client_key(request)
    attempts = [t for t in AUTH_LOGIN_ATTEMPTS.get(key, []) if now - t < AUTH_LOGIN_WINDOW]
    AUTH_LOGIN_ATTEMPTS[key] = attempts
    return len(attempts) >= AUTH_LOGIN_MAX_ATTEMPTS

def _record_login_failure(request: Request):
    key = _auth_client_key(request)
    AUTH_LOGIN_ATTEMPTS[key].append(time.time())

def _clear_login_failures(request: Request):
    AUTH_LOGIN_ATTEMPTS.pop(_auth_client_key(request), None)

def _is_authenticated(token):
    if not token:
        return False
    session = AUTH_SESSIONS.get(token)
    if not session:
        return False
    session["last_seen"] = time.time()
    return True


ADDON_OPTIONS_FILE = Path("/data/options.json")

SUBSONIC_VERSION = "1.16.1"

MAX_CONCURRENT_DOWNLOADS = 3
MAX_DEVICE_STALE_SECONDS = 25.0
DEVICE_RETENTION_SECONDS = 60 * 60 * 24 * 30
DOWNLOAD_HISTORY_MAX_ROWS = 5000
DB_SCHEMA_VERSION = 4
LIBRARY_METADATA_CONCURRENCY = max(4, min(12, int(os.getenv("XROB_LIBRARY_METADATA_CONCURRENCY", "8"))))

AUDIO_EXTENSIONS = {
    ".mp3",
    ".flac",
    ".m4a",
    ".ogg",
    ".wav",
    ".opus",
    ".aac",
    ".alac",
}

MEDIA_TYPES = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".alac": "audio/mp4",
}

DEFAULT_SETTINGS = {
    "audio_format": "mp3",
    "audio_quality": "320K",
    "embed_thumbnail": True,
    "embed_metadata": True,
    "organize_by_artist": False,
    "scan_enabled": True,
    "scan_interval_minutes": 60,
    "title_cleanup_rules": "(Visualizer)\n[Visualizer]\nOfficial Video\nOfficial Music Video\nVideo Clip",
    "metadata_mode": "auto",
    "daily_mix_track_count": 30,
    "replaygain_enabled": True,
    "replaygain_mode": "track",
    "replaygain_preamp_db": 0.0,
    "replaygain_prevent_clipping": True,
    "crossfade_seconds": 0.0,
    "gapless_playback": True,
    "download_location": "",
    "max_concurrent_downloads": 3,
    "auto_retry_downloads": True,
    "download_retry_limit": 2,
    "download_retry_backoff_seconds": 3,
    "artwork_behavior": "embed",
    "cache_size_mb": 256,
    "filename_mode": "title",
    "stats_retention_days": 365,
    "subsonic_user": "admin",
    "subsonic_password": "",
    "web_username": os.getenv("XROB_USERNAME", "admin"),
    "web_password_hash": "",
}

AUDIO_FORMATS = {"mp3", "flac", "m4a", "opus", "ogg", "wav", "aac", "alac"}
AUDIO_QUALITY_VALUES = {"0", "5", "64K", "96K", "128K", "160K", "192K", "256K", "320K"}


# Always invoke yt-dlp through the running Python environment.
# This works reliably inside the add-on even when the console script is not on PATH.
YT_DLP_COMMAND = [sys.executable, "-m", "yt_dlp"]
FFMPEG_COMMAND = [shutil.which("ffmpeg") or "ffmpeg"]

try:
    from mutagen import File as MutagenFile
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, TCON, APIC
    from mutagen.flac import Picture
except Exception:
    MutagenFile = None
    ID3 = TIT2 = TPE1 = TALB = TDRC = TCON = APIC = None
    Picture = None


# ============================================================
# RUNTIME
# ============================================================

TASKS = {}
TASK_QUEUE = asyncio.Queue()
ACTIVE_PROCESSES = {}
LAST_SAVED_TIME = {}
METADATA_CACHE = {}
METADATA_CACHE_MAX = 2000

def _cache_set_bounded(cache, key, value, maximum=2000):
    cache[key] = value
    if len(cache) > maximum:
        # Dicts preserve insertion order; evict the oldest quarter in one batch
        # instead of doing a pop on every insert past the limit.
        overflow = len(cache) - maximum
        remove_count = max(1, min(overflow, max(32, maximum // 4)))
        for old_key in list(cache)[:remove_count]:
            cache.pop(old_key, None)

DOWNLOAD_GUARD = asyncio.Lock()
LIBRARY_CACHE = None
LIBRARY_CACHE_TIME = 0.0
LIBRARY_CACHE_TTL = 10.0
LIBRARY_CACHE_LOCK = asyncio.Lock()
LIBRARY_INDEX_FILE = DATA_DIR / "library_index.json"
LIBRARY_WARMUP_TASK = None
DOWNLOAD_WORKER_TASKS = set()
BACKGROUND_TASKS = set()
SCHEDULED_SCANNER_TASK = None
LIBRARY_SCAN_LOCK = asyncio.Lock()
LIBRARY_REFRESH_LOCK = asyncio.Lock()
COVER_LOCKS = {}
COVER_LOCKS_GUARD = asyncio.Lock()
HISTORY_MAX_ROWS = 100000
APP_ERRORS_MAX_ROWS = 5000
MAX_PLAYLIST_SONGS = 10000


def track_background_task(coro):
    """Track short-lived tasks so shutdown can await them cleanly."""
    task = asyncio.create_task(coro)
    BACKGROUND_TASKS.add(task)
    task.add_done_callback(BACKGROUND_TASKS.discard)
    return task


# ============================================================
# ADD-ON OPTIONS
# ============================================================

def load_addon_options():
    if not ADDON_OPTIONS_FILE.exists():
        return {}

    try:
        with open(
            ADDON_OPTIONS_FILE,
            "r",
            encoding="utf-8",
        ) as f:
            data = json.load(f)

        return data if isinstance(data, dict) else {}

    except Exception as exc:
        print("Failed to read /data/options.json:", exc)
        return {}


def migrate_legacy_db(source: Path, destination: Path):
    """Best-effort migration from the old NAS-hosted SQLite database.

    A locked legacy DB must never prevent startup. If it cannot be backed up
    safely, the new local DB is initialized instead and the legacy file is
    left untouched.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_conn = None
    destination_conn = None
    try:
        source_conn = sqlite3.connect(source, timeout=5.0)
        source_conn.execute("PRAGMA busy_timeout = 5000")
        destination_conn = sqlite3.connect(destination, timeout=30.0)
        source_conn.backup(destination_conn)
        print(f"Migrated legacy database: {source} -> {destination}")
    except (sqlite3.Error, OSError) as exc:
        try:
            if destination.exists():
                destination.unlink()
        except OSError:
            pass
        print(f"Warning: could not migrate legacy database: {exc}. Starting with a new local database.")
    finally:
        for conn in (destination_conn, source_conn):
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


def configure_storage():
    """Apply the configured library path and prepare its local metadata cache."""
    global DOWNLOAD_DIR, COVER_CACHE_DIR, SETTINGS_FILE, DB_FILE, LIBRARY_INDEX_FILE

    addon = load_addon_options()
    configured = str(addon.get("music_path") or "").strip()
    persisted_location = ""
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as handle:
                raw_settings = json.load(handle)
            persisted_location = str(raw_settings.get("download_location") or "").strip() if isinstance(raw_settings, dict) else ""
        except Exception:
            persisted_location = ""
    requested_location = persisted_location or configured or os.getenv("DOWNLOAD_DIR", DEFAULT_LIBRARY_PATH)
    path = Path(requested_location).expanduser()

    if not path.is_absolute():
        raise RuntimeError("music_path must be an absolute path")

    DOWNLOAD_DIR = path
    COVER_CACHE_DIR = DOWNLOAD_DIR / ".covers"
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # SQLite databases should never live on the NAS/SMB mount. Network
    # filesystem locking is unreliable and can prevent the application from
    # starting with "database is locked". Keep the DB and settings on the
    # add-on's persistent local /data volume.
    legacy_db = DOWNLOAD_DIR / "tasks.db"
    legacy_settings = DOWNLOAD_DIR / ".settings.json"
    SETTINGS_FILE = DATA_DIR / "settings.json"
    LIBRARY_INDEX_FILE = DATA_DIR / "library_index.json"
    DB_FILE = DATA_DIR / "tasks.db"

    if not DB_FILE.exists() and legacy_db.exists():
        migrate_legacy_db(legacy_db, DB_FILE)

    if not SETTINGS_FILE.exists() and legacy_settings.exists():
        try:
            shutil.copy2(legacy_settings, SETTINGS_FILE)
        except OSError as exc:
            print(f"Warning: could not migrate legacy settings: {exc}")

    # Do not silently create a missing explicitly configured NAS mount. That
    # would make a disconnected NAS look like an empty local library.
    explicit_path = bool(configured)
    if explicit_path and not DOWNLOAD_DIR.exists():
        raise RuntimeError(
            f"Configured music_path does not exist or is not mounted: {DOWNLOAD_DIR}"
        )

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    COVER_CACHE_DIR.mkdir(parents=True, exist_ok=True)



def storage_info_sync():
    path = DOWNLOAD_DIR
    exists = path.exists() and path.is_dir()
    writable = False
    total = free = used = 0
    if exists:
        writable = os.access(path, os.W_OK)
        try:
            usage = shutil.disk_usage(path)
            total, free = usage.total, usage.free
            used = total - free
        except OSError:
            pass
    return {
        "path": str(path),
        "exists": exists,
        "writable": writable,
        "mounted": exists,
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "total": format_size(total),
        "used": format_size(used),
        "free": format_size(free),
    }


def invalidate_library_cache():
    global LIBRARY_CACHE, LIBRARY_CACHE_TIME
    LIBRARY_CACHE = None
    LIBRARY_CACHE_TIME = 0.0


def load_settings():
    settings = DEFAULT_SETTINGS.copy()

    if SETTINGS_FILE.exists():
        try:
            with open(
                SETTINGS_FILE,
                "r",
                encoding="utf-8",
            ) as f:
                data = json.load(f)

            if isinstance(data, dict):
                settings.update(data)

        except Exception:
            pass

    addon = load_addon_options()

    if addon.get("subsonic_user") is not None:
        settings["subsonic_user"] = str(
            addon.get("subsonic_user") or "admin"
        )

    if "subsonic_password" in addon:
        settings["subsonic_password"] = str(
            addon.get("subsonic_password") or ""
        )

    # Add-on auth options seed the first run only. Afterwards the in-app account
    # settings remain authoritative and are not overwritten on every request.
    if not SETTINGS_FILE.exists():
        if addon.get("web_username") is not None:
            settings["web_username"] = str(addon.get("web_username") or settings.get("web_username") or AUTH_USER or "admin")
        if "web_password" in addon and str(addon.get("web_password") or ""):
            settings["web_password"] = str(addon.get("web_password") or "")

    fmt = str(settings.get("audio_format") or "mp3").lower().lstrip(".")
    if fmt not in AUDIO_FORMATS:
        fmt = "mp3"
    settings["audio_format"] = fmt

    quality = str(settings.get("audio_quality") or "320K").upper()
    if quality not in AUDIO_QUALITY_VALUES:
        quality = "320K"
    settings["audio_quality"] = quality

    settings["embed_thumbnail"] = bool(settings.get("embed_thumbnail", True))
    settings["embed_metadata"] = bool(settings.get("embed_metadata", True))
    settings["organize_by_artist"] = bool(settings.get("organize_by_artist", False))
    settings["metadata_mode"] = str(settings.get("metadata_mode") or "auto").lower()
    if settings["metadata_mode"] not in {"auto", "musicbrainz", "off"}:
        settings["metadata_mode"] = "auto"
    settings["title_cleanup_rules"] = str(settings.get("title_cleanup_rules") or "")[:4096]
    settings["replaygain_enabled"] = bool(settings.get("replaygain_enabled", True))
    settings["replaygain_mode"] = str(settings.get("replaygain_mode") or "track").lower()
    if settings["replaygain_mode"] not in {"track", "album"}:
        settings["replaygain_mode"] = "track"
    try:
        settings["replaygain_preamp_db"] = max(-12.0, min(12.0, float(settings.get("replaygain_preamp_db", 0) or 0)))
    except (TypeError, ValueError):
        settings["replaygain_preamp_db"] = 0.0
    settings["replaygain_prevent_clipping"] = bool(settings.get("replaygain_prevent_clipping", True))
    try:
        settings["crossfade_seconds"] = max(0.0, min(12.0, float(settings.get("crossfade_seconds", 0) or 0)))
    except (TypeError, ValueError):
        settings["crossfade_seconds"] = 0.0
    settings["gapless_playback"] = bool(settings.get("gapless_playback", True))
    settings["download_location"] = str(settings.get("download_location") or "").strip()[:4096]
    try:
        settings["max_concurrent_downloads"] = max(1, min(8, int(settings.get("max_concurrent_downloads", 3) or 3)))
    except (TypeError, ValueError):
        settings["max_concurrent_downloads"] = 3
    settings["auto_retry_downloads"] = bool(settings.get("auto_retry_downloads", True))
    try:
        settings["download_retry_limit"] = max(0, min(5, int(settings.get("download_retry_limit", 2) or 2)))
    except (TypeError, ValueError):
        settings["download_retry_limit"] = 2
    try:
        settings["download_retry_backoff_seconds"] = max(1, min(60, int(settings.get("download_retry_backoff_seconds", 3) or 3)))
    except (TypeError, ValueError):
        settings["download_retry_backoff_seconds"] = 3
    settings["artwork_behavior"] = str(settings.get("artwork_behavior") or "embed").lower()
    if settings["artwork_behavior"] not in {"embed", "download", "none"}:
        settings["artwork_behavior"] = "embed"
    try:
        settings["cache_size_mb"] = max(32, min(2048, int(settings.get("cache_size_mb", 256) or 256)))
    except (TypeError, ValueError):
        settings["cache_size_mb"] = 256
    # Keep the user-facing cache setting meaningful without pretending artwork is
    # held entirely in RAM: metadata/search lookup caches scale with this value,
    # while artwork is primarily served from disk/browser cache.
    global METADATA_CACHE_MAX
    METADATA_CACHE_MAX = max(256, min(10000, settings["cache_size_mb"] * 8))
    settings["filename_mode"] = str(settings.get("filename_mode") or "title").lower()
    if settings["filename_mode"] not in {"title", "artist-title", "artist-album-title"}:
        settings["filename_mode"] = "title"
    try:
        settings["stats_retention_days"] = max(30, min(3650, int(settings.get("stats_retention_days", 365) or 365)))
    except (TypeError, ValueError):
        settings["stats_retention_days"] = 365
    settings.pop("max_results", None)

    return settings


def save_settings(data: dict):
    if not isinstance(data, dict):
        raise ValueError("Settings must be an object.")

    settings = load_settings()
    allowed = {
        "audio_format", "audio_quality", "embed_thumbnail",
        "embed_metadata", "organize_by_artist", "scan_enabled",
        "scan_interval_minutes", "title_cleanup_rules", "metadata_mode", "daily_mix_track_count",
        "replaygain_enabled", "replaygain_mode", "replaygain_preamp_db", "replaygain_prevent_clipping",
        "crossfade_seconds", "gapless_playback", "web_username", "web_password",
        "download_location", "max_concurrent_downloads", "auto_retry_downloads", "download_retry_limit",
        "download_retry_backoff_seconds", "artwork_behavior", "cache_size_mb", "filename_mode", "stats_retention_days",
    }

    old_user = str(settings.get("web_username") or "")
    for key in allowed & data.keys():
        if key not in {"web_password"}:
            settings[key] = data[key]

    if "web_password" in data and str(data.get("web_password") or ""):
        new_password = str(data.get("web_password") or "")
        if len(new_password) < AUTH_MIN_PASSWORD_LENGTH:
            raise ValueError(f"Web password must be at least {AUTH_MIN_PASSWORD_LENGTH} characters.")
        if new_password.casefold() in {"admin", "password", "password123", "changeme", "123456789012"}:
            raise ValueError("Choose a stronger web password.")
        settings["web_password_hash"] = _hash_web_password(new_password)
        settings.pop("web_password", None)

    fmt = str(settings.get("audio_format") or "mp3").lower().lstrip(".")
    if fmt not in AUDIO_FORMATS:
        raise ValueError(f"Unsupported audio format: {fmt}")
    settings["audio_format"] = fmt

    quality = str(settings.get("audio_quality") or "320K").upper()
    if quality not in AUDIO_QUALITY_VALUES:
        raise ValueError(f"Unsupported audio quality: {quality}")
    settings["audio_quality"] = quality

    settings["embed_thumbnail"] = bool(settings.get("embed_thumbnail"))
    settings["embed_metadata"] = bool(settings.get("embed_metadata"))
    settings["organize_by_artist"] = bool(settings.get("organize_by_artist"))
    settings["metadata_mode"] = str(settings.get("metadata_mode") or "auto").lower()
    if settings["metadata_mode"] not in {"auto", "musicbrainz", "off"}:
        settings["metadata_mode"] = "auto"
    settings["scan_enabled"] = bool(settings.get("scan_enabled", True))
    try:
        scan_interval = int(settings.get("scan_interval_minutes", 60) or 60)
    except (TypeError, ValueError):
        scan_interval = 60
    settings["scan_interval_minutes"] = max(5, min(10080, scan_interval))
    try:
        daily_mix_count = int(settings.get("daily_mix_track_count", 30) or 30)
    except (TypeError, ValueError):
        daily_mix_count = 30
    settings["daily_mix_track_count"] = max(5, min(50, daily_mix_count))
    settings["replaygain_enabled"] = bool(settings.get("replaygain_enabled", True))
    settings["replaygain_mode"] = str(settings.get("replaygain_mode") or "track").lower()
    if settings["replaygain_mode"] not in {"track", "album"}:
        settings["replaygain_mode"] = "track"
    try:
        settings["replaygain_preamp_db"] = max(-12.0, min(12.0, float(settings.get("replaygain_preamp_db", 0) or 0)))
    except (TypeError, ValueError):
        settings["replaygain_preamp_db"] = 0.0
    settings["replaygain_prevent_clipping"] = bool(settings.get("replaygain_prevent_clipping", True))
    try:
        settings["crossfade_seconds"] = max(0.0, min(12.0, float(settings.get("crossfade_seconds", 0) or 0)))
    except (TypeError, ValueError):
        settings["crossfade_seconds"] = 0.0
    settings["gapless_playback"] = bool(settings.get("gapless_playback", True))
    requested_location = str(settings.get("download_location") or "").strip()[:4096]
    if requested_location:
        candidate = Path(requested_location).expanduser()
        if not candidate.is_absolute():
            raise ValueError("Download location must be an absolute path.")
        if candidate.exists() and not candidate.is_dir():
            raise ValueError("Download location must be a directory.")
        if not candidate.exists():
            raise ValueError("Download location does not exist. Mount or create it first.")
        settings["download_location"] = str(candidate)
    else:
        settings["download_location"] = ""
    try:
        settings["max_concurrent_downloads"] = max(1, min(8, int(settings.get("max_concurrent_downloads", 3) or 3)))
    except (TypeError, ValueError):
        settings["max_concurrent_downloads"] = 3
    settings["auto_retry_downloads"] = bool(settings.get("auto_retry_downloads", True))
    try:
        settings["download_retry_limit"] = max(0, min(5, int(settings.get("download_retry_limit", 2) or 2)))
    except (TypeError, ValueError):
        settings["download_retry_limit"] = 2
    try:
        settings["download_retry_backoff_seconds"] = max(1, min(60, int(settings.get("download_retry_backoff_seconds", 3) or 3)))
    except (TypeError, ValueError):
        settings["download_retry_backoff_seconds"] = 3
    settings["artwork_behavior"] = str(settings.get("artwork_behavior") or "embed").lower()
    if settings["artwork_behavior"] not in {"embed", "download", "none"}:
        settings["artwork_behavior"] = "embed"
    try:
        settings["cache_size_mb"] = max(32, min(2048, int(settings.get("cache_size_mb", 256) or 256)))
    except (TypeError, ValueError):
        settings["cache_size_mb"] = 256
    global METADATA_CACHE_MAX
    METADATA_CACHE_MAX = max(256, min(10000, settings["cache_size_mb"] * 8))
    settings["filename_mode"] = str(settings.get("filename_mode") or "title").lower()
    if settings["filename_mode"] not in {"title", "artist-title", "artist-album-title"}:
        settings["filename_mode"] = "title"
    try:
        settings["stats_retention_days"] = max(30, min(3650, int(settings.get("stats_retention_days", 365) or 365)))
    except (TypeError, ValueError):
        settings["stats_retention_days"] = 365
    settings["web_username"] = str(settings.get("web_username") or os.getenv("XROB_USERNAME", "admin"))[:64]
    # Keep a verifier, never persist web passwords in plaintext.
    if settings.get("web_password") and not settings.get("web_password_hash"):
        legacy_password = str(settings.get("web_password"))
        if len(legacy_password) < AUTH_MIN_PASSWORD_LENGTH:
            raise ValueError(f"Web password must be at least {AUTH_MIN_PASSWORD_LENGTH} characters.")
        settings["web_password_hash"] = _hash_web_password(legacy_password)
        settings.pop("web_password", None)
    settings["web_password_hash"] = str(settings.get("web_password_hash") or "")

    _write_settings_sync(settings)
    if old_user != settings.get("web_username") or ("web_password" in data and str(data.get("web_password") or "")):
        AUTH_SESSIONS.clear()

    return settings


def public_settings():
    settings = dict(load_settings())
    # Subsonic credentials are add-on/server configuration, not web UI settings.
    settings.pop("subsonic_user", None)
    settings.pop("subsonic_password", None)
    settings["web_password_set"] = bool(settings.get("web_password_hash") or settings.get("web_password") or AUTH_PASSWORD)
    settings["web_username"] = str(settings.get("web_username") or "admin")
    settings.pop("web_password", None)
    settings["storage"] = storage_info_sync()
    return settings

async def load_settings_async():
    return await asyncio.to_thread(load_settings)


async def save_settings_async(data):
    return await asyncio.to_thread(save_settings, data)


async def public_settings_async():
    return await asyncio.to_thread(public_settings)


# ============================================================
# DATABASE
# ============================================================

@contextmanager
def db_connect():
    """Open and reliably close the local persistent SQLite database.

    A sqlite3 connection's native ``with`` statement manages transactions but
    does not close the connection. Xrob Music performs many short DB calls, so
    this wrapper owns the connection lifetime and closes it deterministically.
    """
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA cache_size = -4096")
        conn.execute("PRAGMA wal_autocheckpoint = 1000")
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def init_db():
    with db_connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                title TEXT,
                artist TEXT,
                album TEXT,
                url TEXT,
                elementId TEXT,
                status TEXT,
                percent REAL,
                speed TEXT,
                step TEXT,
                error TEXT,
                last_updated REAL,
                final_name TEXT,
                created_at REAL DEFAULT 0
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stars (
                item_id TEXT PRIMARY KEY,
                starred_at REAL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS playlists (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                comment TEXT DEFAULT '',
                owner TEXT DEFAULT 'admin',
                public INTEGER DEFAULT 0,
                song_ids TEXT DEFAULT '[]',
                created_at REAL,
                updated_at REAL
            )
            """
        )

        conn.execute("""CREATE TABLE IF NOT EXISTS playback_positions (song_id TEXT PRIMARY KEY, position REAL DEFAULT 0, duration REAL DEFAULT 0, updated_at REAL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS play_history (id INTEGER PRIMARY KEY AUTOINCREMENT, song_id TEXT NOT NULL, played_at REAL NOT NULL, duration REAL DEFAULT 0, position REAL DEFAULT 0)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_play_history_song_id ON play_history(song_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_play_history_played_at ON play_history(played_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_play_history_song_played_at ON play_history(song_id, played_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status_updated ON tasks(status, last_updated DESC)")
        conn.execute("""CREATE TABLE IF NOT EXISTS player_sessions (session_key TEXT PRIMARY KEY, owner_id TEXT, client_id TEXT, state_json TEXT NOT NULL, updated_at REAL NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS scan_state (id INTEGER PRIMARY KEY CHECK (id=1), started_at REAL, finished_at REAL, mode TEXT, status TEXT, message TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS app_errors (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL, source TEXT, message TEXT, task_id TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS song_review (song_id TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'pending', actioned_at REAL DEFAULT 0)""")
        # Permanent history of successfully edited tracks; independent from the current review queue.
        conn.execute("""CREATE TABLE IF NOT EXISTS song_editor_history (song_id TEXT PRIMARY KEY, edited_at REAL NOT NULL DEFAULT 0)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS artist_artwork (artist_id TEXT PRIMARY KEY, data BLOB NOT NULL, mime TEXT NOT NULL, updated_at REAL NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS download_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT UNIQUE NOT NULL, title TEXT, artist TEXT, album TEXT, url TEXT,
            status TEXT NOT NULL, percent REAL DEFAULT 0, speed TEXT DEFAULT '', step TEXT DEFAULT '',
            error TEXT DEFAULT '', final_name TEXT DEFAULT '', created_at REAL, completed_at REAL NOT NULL
        )""")
        conn.execute("""CREATE INDEX IF NOT EXISTS idx_download_history_completed_at ON download_history(completed_at DESC)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS devices (
            device_id TEXT PRIMARY KEY, client_id TEXT, tab_id TEXT, name TEXT NOT NULL, device_type TEXT DEFAULT 'browser',
            platform TEXT DEFAULT '', browser TEXT DEFAULT '', capabilities_json TEXT DEFAULT '{}', last_seen_at REAL NOT NULL, created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )""")
        conn.execute("""CREATE INDEX IF NOT EXISTS idx_devices_last_seen ON devices(last_seen_at DESC)""")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        if "identity_key" not in columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN identity_key TEXT DEFAULT ''")
        if "retry_count" not in columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN retry_count INTEGER DEFAULT 0")
        if "resume_available" not in columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN resume_available INTEGER DEFAULT 0")
        # Older releases did not have identity_key. Fill it deterministically and
        # neutralize duplicate legacy active rows before creating the partial unique index.
        for row in conn.execute("SELECT id,title,artist,url,status,identity_key FROM tasks WHERE status IN ('queued','downloading','processing')").fetchall():
            identity = str(row[5] or "")
            if not identity:
                title_key = normalize_duplicate_key(row[1] or "", row[2] or "")
                url_key = hashlib.sha256(str(row[3] or "").encode("utf-8")).hexdigest()
                identity = f"{title_key}|{url_key}"
                conn.execute("UPDATE tasks SET identity_key=? WHERE id=?", (identity,row[0]))
        try:
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_active_identity ON tasks(identity_key) WHERE identity_key IS NOT NULL AND identity_key <> '' AND status IN ('queued','downloading','processing')")
        except sqlite3.IntegrityError:
            duplicates = conn.execute("SELECT identity_key, COUNT(*) FROM tasks WHERE identity_key<>'' AND status IN ('queued','downloading','processing') GROUP BY identity_key HAVING COUNT(*)>1").fetchall()
            for identity,_count in duplicates:
                dup_rows = conn.execute("SELECT id FROM tasks WHERE identity_key=? AND status IN ('queued','downloading','processing') ORDER BY created_at ASC",(identity,)).fetchall()
                for dup in dup_rows[1:]:
                    conn.execute("UPDATE tasks SET identity_key='' WHERE id=?",(dup[0],))
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_active_identity ON tasks(identity_key) WHERE identity_key IS NOT NULL AND identity_key <> '' AND status IN ('queued','downloading','processing')")
        # Playlist extensions are additive and preserve the existing schema.
        playlist_cols = {row[1] for row in conn.execute("PRAGMA table_info(playlists)")}
        if "kind" not in playlist_cols:
            conn.execute("ALTER TABLE playlists ADD COLUMN kind TEXT DEFAULT 'manual'")
        if "rules" not in playlist_cols:
            conn.execute("ALTER TABLE playlists ADD COLUMN rules TEXT DEFAULT '{}'")

        columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        if "created_at" not in columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN created_at REAL DEFAULT 0")
            conn.execute("UPDATE tasks SET created_at = last_updated WHERE created_at = 0 OR created_at IS NULL")
        conn.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")
        conn.commit()


def db_save_task_sync(task):
    """Persist a task without allowing active-download conflicts to replace rows."""
    task_status = str(task.get("status") or "").lower()
    identity_key = str(task.get("identity_key") or "")
    with db_connect() as conn:
        try:
            conn.execute(
                """
                INSERT INTO tasks (
                    id, title, artist, album, url, elementId, status, percent, speed,
                    step, error, last_updated, final_name, created_at, identity_key,
                    retry_count, resume_available
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title, artist=excluded.artist, album=excluded.album,
                    url=excluded.url, elementId=excluded.elementId, status=excluded.status,
                    percent=excluded.percent, speed=excluded.speed, step=excluded.step,
                    error=excluded.error, last_updated=excluded.last_updated,
                    final_name=excluded.final_name, created_at=excluded.created_at,
                    identity_key=excluded.identity_key, retry_count=excluded.retry_count,
                    resume_available=excluded.resume_available
                """,
                (
                    task.get("id"), task.get("title"), task.get("artist"), task.get("album"),
                    task.get("url"), task.get("elementId"), task.get("status"), task.get("percent", 0),
                    task.get("speed", ""), task.get("step", ""), task.get("error", ""),
                    task.get("last_updated", 0), task.get("final_name", ""),
                    task.get("created_at", task.get("last_updated", 0)), identity_key,
                    safe_int(task.get("retry_count"), 0), 1 if task.get("resume_available") else 0,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # The partial unique index is the final race-condition guard. Never use
            # INSERT OR REPLACE here: REPLACE would delete the existing active task.
            if task_status in {"queued", "downloading", "processing"} and identity_key:
                conflict = conn.execute(
                    "SELECT id FROM tasks WHERE identity_key=? AND status IN ('queued','downloading','processing') AND id<>? LIMIT 1",
                    (identity_key, task.get("id")),
                ).fetchone()
                if conflict:
                    raise sqlite3.IntegrityError(
                        f"Active download already exists for this source (task {conflict[0]})"
                    ) from exc
            raise

        if task_status in {"completed", "error", "failed", "cancelled", "canceled"}:
            conn.execute(
                """INSERT INTO download_history(
                    task_id,title,artist,album,url,status,percent,speed,step,error,final_name,created_at,completed_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(task_id) DO UPDATE SET
                    title=excluded.title, artist=excluded.artist, album=excluded.album, url=excluded.url,
                    status=excluded.status, percent=excluded.percent, speed=excluded.speed, step=excluded.step,
                    error=excluded.error, final_name=excluded.final_name, created_at=excluded.created_at,
                    completed_at=excluded.completed_at
                """,
                (
                    task.get("id"), task.get("title"), task.get("artist"), task.get("album"), task.get("url"),
                    task.get("status"), task.get("percent", 0), task.get("speed", ""), task.get("step", ""),
                    task.get("error", ""), task.get("final_name", ""), task.get("created_at", 0), time.time(),
                ),
            )
            conn.execute(
                "DELETE FROM download_history WHERE id IN (SELECT id FROM download_history ORDER BY completed_at DESC LIMIT -1 OFFSET ?)",
                (DOWNLOAD_HISTORY_MAX_ROWS,),
            )
        conn.commit()


async def db_save_task(task, force=False):
    task_id = task.get("id")
    now = time.time()

    if (
        force
        or now - LAST_SAVED_TIME.get(task_id, 0) > 0.5
    ):
        LAST_SAVED_TIME[task_id] = now

        await asyncio.to_thread(
            db_save_task_sync,
            task,
        )


def db_load_tasks_sync():
    if not DB_FILE.exists():
        return {}

    tasks = {}

    with db_connect() as conn:
        conn.row_factory = sqlite3.Row

        for row in conn.execute(
            "SELECT * FROM tasks"
        ):
            item = dict(row)
            tasks[item["id"]] = item

    return tasks


def db_clear_finished_sync():
    with db_connect() as conn:
        conn.execute(
            """
            DELETE FROM tasks
            WHERE status IN (
                'completed',
                'cancelled',
                'canceled',
                'error',
                'failed'
            )
            """
        )
        conn.commit()


def db_delete_task_sync(task_id):
    with db_connect() as conn:
        conn.execute(
            "DELETE FROM tasks WHERE id = ?",
            (task_id,),
        )
        conn.commit()


def db_load_download_history_sync(limit=300):
    limit = max(1, min(2000, int(limit or 300)))
    with db_connect() as conn:
        rows = conn.execute("SELECT * FROM download_history ORDER BY completed_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]

def db_clear_download_history_sync():
    with db_connect() as conn:
        conn.execute("DELETE FROM download_history")
        conn.commit()

def db_register_device_sync(device):
    now=time.time()
    with db_connect() as conn:
        conn.execute("""INSERT INTO devices(device_id,client_id,tab_id,name,device_type,platform,browser,capabilities_json,last_seen_at,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(device_id) DO UPDATE SET client_id=excluded.client_id,tab_id=excluded.tab_id,name=excluded.name,device_type=excluded.device_type,platform=excluded.platform,browser=excluded.browser,capabilities_json=excluded.capabilities_json,last_seen_at=excluded.last_seen_at,updated_at=excluded.updated_at""",
            (device["deviceId"],device.get("clientId",""),device.get("tabId",""),device.get("name") or "This device",device.get("deviceType") or "browser",device.get("platform") or "",device.get("browser") or "",json.dumps(device.get("capabilities") or {},separators=(",",":")),now,now,now))
        cutoff=now-DEVICE_RETENTION_SECONDS
        conn.execute("DELETE FROM devices WHERE last_seen_at < ?", (cutoff,))
        conn.commit()
    return now

def db_get_devices_sync():
    with db_connect() as conn:
        rows=conn.execute("SELECT * FROM devices ORDER BY last_seen_at DESC,name COLLATE NOCASE ASC").fetchall()
    return [dict(row) for row in rows]


# ============================================================
# WEBSOCKET
# ============================================================

class ConnectionManager:
    def __init__(self):
        self.connections = []

    async def connect(self, websocket):
        await websocket.accept()

        if websocket not in self.connections:
            self.connections.append(websocket)

    def disconnect(self, websocket):
        if websocket in self.connections:
            self.connections.remove(websocket)

    async def broadcast(self, message):
        connections = list(self.connections)
        if not connections:
            return
        results = await asyncio.gather(
            *(websocket.send_json(message) for websocket in connections),
            return_exceptions=True,
        )
        for websocket, result in zip(connections, results):
            if isinstance(result, Exception):
                self.disconnect(websocket)


manager = ConnectionManager()


def _bounded_text(value, limit=PLAYER_STATE_MAX_TEXT):
    return str(value or "")[:limit]


def _sanitize_player_track(track):
    if not isinstance(track, dict):
        return None
    allowed = ("id", "name", "title", "artist", "album", "duration", "cover", "stream")
    cleaned = {}
    for key in allowed:
        if key not in track:
            continue
        value = track[key]
        if key == "duration":
            try:
                value = max(0.0, float(value or 0))
            except (TypeError, ValueError):
                value = 0.0
        elif key in {"id", "name", "title", "artist", "album"}:
            value = _bounded_text(value)
        elif key in {"cover", "stream"}:
            value = _bounded_text(value, PLAYER_STATE_MAX_URL)
        cleaned[key] = value
    return cleaned if cleaned.get("id") or cleaned.get("name") or cleaned.get("stream") else None


def _sanitize_player_state(state):
    if not isinstance(state, dict):
        return {}
    # Preserve the distinction between a full snapshot and a compact heartbeat.
    # Only sanitize keys that are actually present so compact updates cannot
    # overwrite a previously stored title, queue, volume, etc. with defaults.
    cleaned = dict(state)

    if "ownerId" in cleaned:
        cleaned["ownerId"] = _bounded_text(cleaned.get("ownerId"), 200)
    if "clientId" in cleaned:
        cleaned["clientId"] = _bounded_text(cleaned.get("clientId"), 200)
    for key in ("src", "art"):
        if key in cleaned:
            cleaned[key] = _bounded_text(cleaned.get(key), PLAYER_STATE_MAX_URL)
    for key in ("title", "artist", "songId", "source"):
        if key in cleaned:
            cleaned[key] = _bounded_text(cleaned.get(key))
    if "deviceName" in cleaned:
        cleaned["deviceName"] = _bounded_text(cleaned.get("deviceName"), 120)

    for key in ("currentTime", "duration", "volume"):
        if key not in cleaned:
            continue
        try:
            value = float(cleaned.get(key) or 0)
            cleaned[key] = max(0.0, value) if math.isfinite(value) else 0.0
        except (TypeError, ValueError):
            cleaned[key] = 0.0
    if "duration" in cleaned:
        cleaned["duration"] = min(86_400.0, cleaned["duration"])
    if "currentTime" in cleaned:
        cleaned["currentTime"] = min(86_400.0, cleaned["currentTime"])
        if cleaned.get("duration", 0) > 0:
            cleaned["currentTime"] = min(cleaned["currentTime"], cleaned["duration"])
    if "volume" in cleaned:
        cleaned["volume"] = min(1.0, cleaned["volume"])
    if "queueIndex" in cleaned:
        try:
            raw_index = cleaned.get("queueIndex")
            cleaned["queueIndex"] = int(raw_index) if raw_index is not None else -1
        except (TypeError, ValueError):
            cleaned["queueIndex"] = -1
        cleaned["queueIndex"] = max(-1, cleaned["queueIndex"])

    if "repeatMode" in cleaned:
        cleaned["repeatMode"] = cleaned.get("repeatMode") if cleaned.get("repeatMode") in {"off", "track", "queue"} else "off"
    if "shuffle" in cleaned:
        cleaned["shuffle"] = bool(cleaned.get("shuffle"))

    for key in ("paused", "muted", "force", "takeover"):
        if key in cleaned:
            cleaned[key] = bool(cleaned.get(key))

    if "queue" in cleaned:
        if isinstance(cleaned.get("queue"), list):
            cleaned["queue"] = [
                t for t in (_sanitize_player_track(item) for item in cleaned["queue"][:PLAYER_STATE_MAX_QUEUE_ITEMS])
                if t
            ]
        else:
            cleaned.pop("queue", None)

    if "syncMode" in cleaned:
        cleaned["syncMode"] = "linked" if str(cleaned.get("syncMode") or "").strip().lower() == "linked" else "off"
    if "syncDeviceIds" in cleaned:
        raw_ids = cleaned.get("syncDeviceIds") if isinstance(cleaned.get("syncDeviceIds"), list) else []
        seen_ids = set(); sync_ids = []
        for raw_id in raw_ids[:PLAYER_STATE_MAX_SYNC_DEVICES * 2]:
            value = _bounded_text(raw_id, 200).strip()
            if not value or value in seen_ids: continue
            seen_ids.add(value); sync_ids.append(value)
            if len(sync_ids) >= PLAYER_STATE_MAX_SYNC_DEVICES: break
        cleaned["syncDeviceIds"] = sync_ids
        if len(sync_ids) < 2 and cleaned.get("syncMode") == "linked": cleaned["syncMode"] = "off"
    if "syncGroupId" in cleaned:
        cleaned["syncGroupId"] = _bounded_text(cleaned.get("syncGroupId"), 160).strip()

    if "dailyMix" in cleaned:
        daily = cleaned.get("dailyMix")
        if isinstance(daily, dict):
            daily = dict(daily)
            if "tracks" in daily:
                tracks = daily.get("tracks") if isinstance(daily.get("tracks"), list) else []
                daily["tracks"] = [
                    t for t in (_sanitize_player_track(item) for item in tracks[:PLAYER_STATE_MAX_DAILY_MIX_ITEMS])
                    if t
                ]
            if "variant" in daily:
                try:
                    daily["variant"] = max(0, int(daily.get("variant", 0) or 0))
                except (TypeError, ValueError):
                    daily["variant"] = 0
            for key in ("title", "subtitle"):
                if key in daily:
                    daily[key] = _bounded_text(daily.get(key), PLAYER_STATE_MAX_TEXT)
            if "scrollLeft" in daily:
                try:
                    daily["scrollLeft"] = max(0.0, float(daily.get("scrollLeft", 0) or 0))
                except (TypeError, ValueError):
                    daily["scrollLeft"] = 0.0
            if "date" in daily:
                daily["date"] = _bounded_text(daily.get("date"), 32)
            cleaned["dailyMix"] = daily
        else:
            cleaned.pop("dailyMix", None)

    if "seq" in cleaned:
        try:
            cleaned["seq"] = max(0, int(cleaned.get("seq", 0) or 0))
        except (TypeError, ValueError):
            cleaned["seq"] = 0
    return cleaned


def _compact_player_state(state):
    keys = (
        "ownerId", "clientId", "src", "currentTime", "duration",
        "volume", "title", "artist", "art", "songId", "source", "deviceName",
        "queueIndex", "paused", "muted", "repeatMode", "shuffle",
        "syncMode", "syncDeviceIds", "syncGroupId", "at", "seq", "force",
    )
    return {key: state[key] for key in keys if key in state}


def _load_persisted_player_state_sync():
    try:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT state_json, updated_at FROM player_sessions WHERE session_key=?",
                (PLAYER_STATE_DB_KEY,),
            ).fetchone()
        if not row:
            return None
        state = _sanitize_player_state(json.loads(row[0] or "{}"))
        updated_at = float(row[1] or 0)
        if not state:
            return None
        return {"state": state, "updated_at": updated_at, "persistent": True}
    except Exception:
        return None


def _persist_player_state_sync(state, updated_at):
    payload = json.dumps(_sanitize_player_state(state), separators=(",", ":"), ensure_ascii=False)
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO player_sessions(session_key,owner_id,client_id,state_json,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(session_key) DO UPDATE SET owner_id=excluded.owner_id,client_id=excluded.client_id,state_json=excluded.state_json,updated_at=excluded.updated_at",
            (PLAYER_STATE_DB_KEY, str(state.get("ownerId") or ""), str(state.get("clientId") or ""), payload, float(updated_at)),
        )
        conn.commit()


def _finite_player_time(value, default=0.0):
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def _effective_player_position(state, now=None):
    """Return the server-authoritative playback position at *now*.

    The position is anchored whenever a state transition/heartbeat is received.
    While playing, elapsed wall-clock time is added to that anchor. This avoids
    using the age of the browser's last websocket message as the playback clock.
    """
    if not isinstance(state, dict):
        return 0.0
    now = float(now if now is not None else time.time())
    position = max(0.0, _finite_player_time(state.get("currentTime"), 0.0))
    if not bool(state.get("paused")):
        anchor = _finite_player_time(state.get("positionUpdatedAt") or state.get("at"), now)
        if anchor <= 0 or anchor > now + 2:
            anchor = now
        position += max(0.0, now - anchor)
    duration = max(0.0, _finite_player_time(state.get("duration"), 0.0))
    return min(position, duration) if duration > 0 else position


def _prepare_player_clock(state, now=None, preserve_position=False):
    now = float(now if now is not None else time.time())
    state = dict(state)
    if preserve_position:
        state["currentTime"] = _effective_player_position(state, now)
    else:
        state["currentTime"] = max(0.0, _finite_player_time(state.get("currentTime"), 0.0))
    duration = max(0.0, _finite_player_time(state.get("duration"), 0.0))
    if duration > 0:
        state["currentTime"] = min(state["currentTime"], duration)
    state["positionUpdatedAt"] = now
    state["lastSeenAt"] = now
    state["at"] = now
    if state.get("paused"):
        state.pop("playStartedAt", None)
    else:
        state["playStartedAt"] = now
    return _sanitize_player_state(state)


async def publish_player_state(state, full=True, preserve_position=False, broadcast=True):
    global PLAYER_STATE, PLAYER_STATE_UPDATED_AT, PLAYER_STATE_LAST_PERSISTED_AT
    if not isinstance(state, dict):
        return
    incoming = _sanitize_player_state(state)
    previous_owner = str(PLAYER_STATE.get("ownerId") or "") if isinstance(PLAYER_STATE, dict) else ""
    incoming_owner = str(incoming.get("ownerId") or "")
    if isinstance(PLAYER_STATE, dict) and not full and previous_owner == incoming_owner:
        merged = dict(PLAYER_STATE)
        merged.update(incoming)
    else:
        merged = incoming
    now = time.time()
    merged = _prepare_player_clock(merged, now, preserve_position=preserve_position)
    PLAYER_STATE = merged
    PLAYER_STATE_UPDATED_AT = now
    if full or now - PLAYER_STATE_LAST_PERSISTED_AT >= PLAYER_STATE_PERSIST_INTERVAL_SECONDS:
        try:
            await asyncio.to_thread(_persist_player_state_sync, merged, now)
            PLAYER_STATE_LAST_PERSISTED_AT = now
        except Exception as exc:
            print("Warning: could not persist player session:", exc)
    outbound = dict(merged) if full else _compact_player_state(merged)
    outbound["_serverUpdatedAt"] = PLAYER_STATE_UPDATED_AT
    outbound["_serverLastSeenAt"] = merged.get("lastSeenAt", PLAYER_STATE_UPDATED_AT)
    outbound["_serverCurrentTime"] = _effective_player_position(merged, now)
    if broadcast:
        await manager.broadcast({"type": "player_state", "state": outbound})


def get_player_state():
    global PLAYER_STATE, PLAYER_STATE_UPDATED_AT
    if not isinstance(PLAYER_STATE, dict):
        persisted = _load_persisted_player_state_sync()
        if persisted:
            PLAYER_STATE = persisted["state"]
            PLAYER_STATE_UPDATED_AT = persisted["updated_at"]
        else:
            return None
    now = time.time()
    last_seen = _finite_player_time(PLAYER_STATE.get("lastSeenAt"), PLAYER_STATE_UPDATED_AT)
    age = now - last_seen if last_seen else float("inf")
    state = dict(PLAYER_STATE)
    state["currentTime"] = _effective_player_position(state, now)
    # A read response is already anchored at `now`; callers can safely use the
    # returned currentTime without accidentally advancing the clock twice.
    state["positionUpdatedAt"] = now
    state["_serverCurrentTime"] = state["currentTime"]
    state["_serverLastSeenAt"] = last_seen
    return {
        "state": state,
        "updated_at": PLAYER_STATE_UPDATED_AT,
        "last_seen_at": last_seen,
        "stale": age > PLAYER_STATE_MAX_AGE_SECONDS,
        "active": age <= PLAYER_STATE_ACTIVE_HEARTBEAT_SECONDS,
        "persistent": True,
    }

async def get_player_state_async():
    return await asyncio.to_thread(get_player_state)


async def notify_task_update(task, force_save=False):
    await db_save_task(
        task,
        force=force_save,
    )

    await manager.broadcast(
        {
            "type": "task_update",
            "task": task,
        }
    )


# ============================================================
# HELPERS
# ============================================================

def safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def safe_float(value, default=0):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except Exception:
        return default


def finite_nonnegative_float(value, field_name):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{field_name} must be numeric") from None
    if not math.isfinite(number):
        raise HTTPException(400, f"{field_name} must be finite")
    return max(0.0, number)


def format_duration(seconds):
    seconds = safe_int(seconds, 0)
    return f"{seconds // 60}:{seconds % 60:02d}"


def format_size(size):
    try:
        if size >= 1024 ** 3:
            return f"{size / (1024 ** 3):.2f} GB"

        return f"{size / (1024 ** 2):.1f} MB"

    except Exception:
        return "0 MB"


def parse_tag_int(value, default=0):
    text = str(value or "").strip()
    if not text:
        return default
    match = re.match(r"^\s*(\d+)", text)
    return int(match.group(1)) if match else default



def clean_metadata_text(
    value,
    fallback="",
):
    value = str(value or "").strip()[:512]

    if not value:
        return fallback

    if re.match(
        r"^(https?|ftp)://",
        value,
        flags=re.I,
    ):
        return fallback

    if "/rest/" in value.lower():
        return fallback

    return value


def clean_title_with_rules(value, rules_text=""):
    value = clean_metadata_text(value, "Unknown Track")
    rules = [
        line.strip()
        for line in str(rules_text or "").splitlines()
        if line.strip()
    ]
    for rule in rules:
        try:
            value = re.sub(re.escape(rule), "", value, flags=re.IGNORECASE)
        except re.error:
            continue
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"[\(\[\{]\s*[\)\]\}]", "", value)
    value = re.sub(r"\s*[-–—:|•]+\s*$", "", value).strip()
    value = re.sub(r"^[\s–—:|•\-]+", "", value).strip()
    return value or "Unknown Track"


def normalize_catalog_title(value, rules_text=""):
    """Return a stable recording title for catalog matching and filenames.

    Download/search titles often contain track numbers, video labels, hashtags,
    producer credits, and other upload-only noise. Strip those decorations before
    MusicBrainz/Apple matching so the same song resolves to the same canonical name.
    """
    text = clean_title_with_rules(value, rules_text)

    # Leading track/disc numbers: ``06 - DELLALI`` / ``06. DELLALI`` / ``[06] DELLALI``.
    text = re.sub(r"^\s*\[?\d{1,3}\]?\s*(?:[-–—.)_:]+\s*|(?=\S))", "", text, count=1) if re.match(r"^\s*(?:\[\d{1,3}\]|\d{1,3}\s*[-–—.)_:])", text) else text

    # Upload-only album/collection hashtags such as ``#27album``.
    text = re.sub(r"\s+#\d{1,4}\s*album\b.*$", "", text, flags=re.IGNORECASE)

    # Producer credits commonly appear in download/search titles and are not
    # part of the canonical recording title.
    text = re.sub(r"\s*[\(\[]\s*prod(?:uced)?\.?\s*by\b[^\)\]]*[\)\]]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+prod(?:uced)?\.?\s*by\b.*$", "", text, flags=re.IGNORECASE)

    # Common video/upload decorations, including parenthesized forms.
    text = re.sub(r"\s*[\(\[]\s*(?:official\s+)?(?:lyric|lyrics|music\s+video|video|mv|visualizer|audio)\s*(?:video|clip)?\s*[\)\]]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+(?:official\s+)?(?:music\s+)?video(?:\s+clip)?\s*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+mv\s*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+(?:lyric|lyrics)\s*(?:video|clip)?\s*$", "", text, flags=re.IGNORECASE)

    text = re.sub(r"\s*[-–—|:]+\s*$", "", text)
    text = re.sub(r"\s+", " ", text).strip(" ._-–—|:")
    return text or "Unknown Track"


def clean_filename(value):
    value = clean_metadata_text(
        value,
        "Unknown",
    )

    value = re.sub(
        r'[\\/:*?"<>|]',
        "",
        value,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    ).strip(" .")

    return value[:180] if value else "Unknown"


def normalize_identity_text(value, title=False):
    """Normalize values used for stable library/download identity matching."""
    text = clean_metadata_text(value, "")
    if title:
        # Use the same catalog cleanup rules for both search results and files in
        # the library. This prevents e.g. ``06 - DELLALI (lyric video) #27album``
        # from becoming a different identity than the canonical ``DELLALI``.
        text = normalize_catalog_title(text)
    text = text.casefold()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"\b(official\s*(video|audio|music video)|lyrics?|hd|4k|remaster(?:ed)?|audio|visualizer|video clip)\b", " ", text, flags=re.I)
    text = re.sub(r"[\[\]\(\)\{\}]+", " ", text)
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def normalize_duplicate_key(value, artist=None):
    # Duplicate identity intentionally uses normalized artist + canonical title.
    if artist is not None:
        return f"{normalize_identity_text(artist)}\x00{normalize_identity_text(value, title=True)}"
    text = str(value or "")
    parts = text.split("|", 2)
    if len(parts) >= 2:
        title, artist = parts[0], parts[1]
        return f"{normalize_identity_text(artist)}\x00{normalize_identity_text(title, title=True)}"
    return normalize_identity_text(Path(text).stem, title=True)


# ============================================================
# LIBRARY FILES
# ============================================================

def get_audio_files_sync():
    base = DOWNLOAD_DIR.resolve()
    files = []
    try:
        iterator = DOWNLOAD_DIR.rglob("*")
        for path in iterator:
            if path.name.startswith(".") or path.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            try:
                resolved = path.resolve()
                if not resolved.is_file() or not resolved.is_relative_to(base):
                    continue
                files.append(resolved)
            except (OSError, RuntimeError):
                continue
    except OSError:
        return []
    return files


async def get_all_audio_files():
    return await asyncio.to_thread(
        get_audio_files_sync
    )


def resolve_file_sync(filename):
    base = DOWNLOAD_DIR.resolve()
    target = (DOWNLOAD_DIR / filename).resolve()

    try:
        safe = target.is_relative_to(base)
    except AttributeError:
        safe = target == base or base in target.parents

    if not safe:
        raise HTTPException(status_code=403, detail="Access denied")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    if target.suffix.lower() not in AUDIO_EXTENSIONS:
        raise HTTPException(status_code=404, detail="Audio file not found")
    return target


async def resolve_file(filename):
    return await asyncio.to_thread(
        resolve_file_sync,
        filename,
    )


# ============================================================
# METADATA
# ============================================================

def _path_metadata_fallback(path):
    """Build useful music metadata even when container tags are missing."""
    stem = re.sub(r"[._]+", " ", path.stem)
    stem = re.sub(r"\s+", " ", stem).strip()

    title = stem or "Unknown Track"
    artist = "Unknown Artist"
    album = stem or "Unknown Album"

    # Common layout: Artist/Album/01 - Title.ext
    relative_parent = path.parent.relative_to(DOWNLOAD_DIR) if path.parent != DOWNLOAD_DIR else Path(".")
    parts = list(relative_parent.parts)
    if len(parts) >= 2:
        artist = clean_metadata_text(parts[-2], artist)
        album = clean_metadata_text(parts[-1], album)
    elif len(parts) == 1:
        artist = clean_metadata_text(parts[0], artist)

    m = re.match(r"^\s*(\d{1,3})\s*[-_.]\s*(.+)$", title)
    track = int(m.group(1)) if m else 0
    if m:
        title = m.group(2).strip()

    return {
        "title": title,
        "artist": artist,
        "album": album,
        "genre": "",
        "year": "",
        "track": track,
        "disc": 0,
        "duration": 0,
        "bit_rate": 0,
        "sample_rate": 0,
        "channels": 0,
        "bit_depth": 0,
        "album_artist": artist,
        "replaygain_track_gain": None,
        "replaygain_album_gain": None,
        "replaygain_track_peak": None,
        "replaygain_album_peak": None,
        "has_artwork": False,
    }


def _parse_replaygain_db(value):
    if value is None:
        return None
    text = str(value).strip().lower().replace("db", "").strip()
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _parse_peak(value):
    if value is None:
        return None
    try:
        peak = float(str(value).strip())
        return peak if peak > 0 else None
    except (TypeError, ValueError):
        return None


def _mutagen_has_artwork(audio):
    if not audio:
        return False
    try:
        if hasattr(audio, "pictures") and audio.pictures:
            return True
        tags = getattr(audio, "tags", None)
        if not tags:
            return False
        keys = {str(k).lower() for k in tags.keys()}
        return bool({"apic:cover", "covr", "metadata_block_picture"} & keys) or any(k.startswith("apic") for k in keys)
    except Exception:
        return False


def read_metadata_sync(path):
    fallback = _path_metadata_fallback(path)
    try:
        stat = path.stat()
        cache_key = str(path)
        cache_stamp = (stat.st_mtime_ns, stat.st_size)
        cached = METADATA_CACHE.get(cache_key)
        if cached and cached[0] == cache_stamp:
            return cached[1]

        metadata = dict(fallback)
        command = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=10)
        if result.returncode == 0:
            raw = json.loads(result.stdout or "{}")
            fmt = raw.get("format") or {}
            streams = raw.get("streams") or []
            audio_stream = next((stream for stream in streams if stream.get("codec_type") == "audio"), {})
            tags = {}
            tags.update(fmt.get("tags") or {})
            tags.update(audio_stream.get("tags") or {})
            normalized = {str(k).strip().lower().replace("-", "_"): v for k, v in tags.items()}

            def first_tag(*keys):
                for key in keys:
                    value = clean_metadata_text(normalized.get(key), "")
                    if value:
                        return value
                return ""

            artist = first_tag("artist", "album_artist", "albumartist") or fallback["artist"]
            album_artist = first_tag("album_artist", "albumartist", "album artist") or artist
            album = first_tag("album") or fallback["album"]
            title = first_tag("title") or fallback["title"]
            metadata.update({
                "title": title,
                "artist": artist,
                "album": album,
                "album_artist": album_artist,
                "genre": first_tag("genre"),
                "year": first_tag("date", "year"),
                "track": parse_tag_int(first_tag("tracknumber", "track"), fallback["track"]),
                "disc": parse_tag_int(first_tag("discnumber", "disc"), 0),
                "duration": safe_float(fmt.get("duration"), 0),
                "bit_rate": safe_int(safe_float(audio_stream.get("bit_rate"), 0) / 1000, 0),
                "sample_rate": safe_int(audio_stream.get("sample_rate"), 0),
                "channels": safe_int(audio_stream.get("channels"), 0),
                "bit_depth": safe_int(audio_stream.get("bits_per_raw_sample") or audio_stream.get("bits_per_sample"), 0),
                "replaygain_track_gain": _parse_replaygain_db(first_tag("replaygain_track_gain")),
                "replaygain_album_gain": _parse_replaygain_db(first_tag("replaygain_album_gain")),
                "replaygain_track_peak": _parse_peak(first_tag("replaygain_track_peak")),
                "replaygain_album_peak": _parse_peak(first_tag("replaygain_album_peak")),
            })
            if metadata["bit_rate"] <= 0:
                metadata["bit_rate"] = safe_int(safe_float(fmt.get("bit_rate"), 0) / 1000, 0)

        # Mutagen is a second metadata source for files where ffprobe cannot
        # expose tags consistently (notably some M4A/MP3 variants).
        try:
            from mutagen import File as MutagenFile
            audio = MutagenFile(str(path), easy=True)
            if audio and audio.tags:
                def mt(*keys):
                    for key in keys:
                        value = audio.tags.get(key)
                        if isinstance(value, (list, tuple)) and value:
                            value = value[0]
                        value = clean_metadata_text(value, "")
                        if value:
                            return value
                    return ""
                metadata["title"] = mt("title") or metadata["title"]
                metadata["artist"] = mt("artist") or metadata["artist"]
                metadata["album"] = mt("album") or metadata["album"]
                metadata["album_artist"] = mt("albumartist", "album artist") or metadata["album_artist"] or metadata["artist"]
                metadata["genre"] = mt("genre") or metadata["genre"]
                metadata["year"] = mt("date", "year") or metadata["year"]
                metadata["track"] = parse_tag_int(mt("tracknumber"), metadata["track"])
                metadata["disc"] = parse_tag_int(mt("discnumber"), metadata["disc"])
                metadata["replaygain_track_gain"] = _parse_replaygain_db(mt("replaygain_track_gain")) if mt("replaygain_track_gain") else metadata.get("replaygain_track_gain")
                metadata["replaygain_album_gain"] = _parse_replaygain_db(mt("replaygain_album_gain")) if mt("replaygain_album_gain") else metadata.get("replaygain_album_gain")
                metadata["replaygain_track_peak"] = _parse_peak(mt("replaygain_track_peak")) if mt("replaygain_track_peak") else metadata.get("replaygain_track_peak")
                metadata["replaygain_album_peak"] = _parse_peak(mt("replaygain_album_peak")) if mt("replaygain_album_peak") else metadata.get("replaygain_album_peak")
                metadata["has_artwork"] = _mutagen_has_artwork(audio)
                if getattr(audio, "info", None):
                    metadata["duration"] = safe_float(getattr(audio.info, "length", 0), metadata["duration"])
                    metadata["bit_rate"] = safe_int(getattr(audio.info, "bitrate", 0) / 1000, metadata["bit_rate"])
        except Exception:
            pass

        metadata["artist"] = clean_metadata_text(metadata.get("artist"), fallback["artist"])
        metadata["album_artist"] = clean_metadata_text(metadata.get("album_artist"), metadata["artist"])
        metadata["album"] = clean_metadata_text(metadata.get("album"), fallback["album"])
        metadata["title"] = clean_metadata_text(metadata.get("title"), fallback["title"])
        _cache_set_bounded(METADATA_CACHE, cache_key, (cache_stamp, metadata), METADATA_CACHE_MAX)
        return metadata
    except Exception:
        return fallback


async def read_metadata(path):
    return await asyncio.to_thread(
        read_metadata_sync,
        path,
    )


# ============================================================
# STABLE IDS
# ============================================================

def make_song_id(path):
    relative = str(
        path.relative_to(
            DOWNLOAD_DIR
        )
    )

    digest = hashlib.sha1(
        relative.encode("utf-8")
    ).hexdigest()[:20]

    return f"song-{digest}"


def make_artist_id(name):
    name = clean_metadata_text(
        name,
        "Unknown Artist",
    )
    name = re.sub(r"\s+", " ", name).strip()

    # Artist identity is case-insensitive so differently cased metadata
    # such as "Cheb Akil" and "cheb akil" resolves to one artist.
    identity_name = name.casefold()

    digest = hashlib.sha1(
        identity_name.encode("utf-8")
    ).hexdigest()[:20]

    return f"artist-{digest}"


def make_album_id(
    artist,
    album,
):
    # Album identity is case-insensitive and whitespace-normalized, just like
    # artist identity. This prevents metadata such as "SHAW"/"Shaw" or
    # "cheb akil"/"Cheb Akil" from creating duplicate album cards.
    artist_identity = re.sub(r"\s+", " ", clean_metadata_text(artist, "Unknown Artist")).strip().casefold()
    album_identity = re.sub(r"\s+", " ", clean_metadata_text(album, "Unknown Album")).strip().casefold()
    raw = artist_identity + "\x00" + album_identity

    digest = hashlib.sha1(
        raw.encode("utf-8")
    ).hexdigest()[:20]

    return f"album-{digest}"


# ============================================================
# FAST LIBRARY INDEX
# ============================================================

def _load_library_index_sync():
    if not LIBRARY_INDEX_FILE.exists():
        return {}
    try:
        with open(LIBRARY_INDEX_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_library_index_sync(entries):
    tmp = LIBRARY_INDEX_FILE.with_suffix(".tmp")
    payload = {"version": 1, "entries": entries}
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(LIBRARY_INDEX_FILE)

def _fast_file_library_sync():
    files = get_audio_files_sync()
    rows = []
    for path in files:
        try:
            st = path.stat()
            rel = str(path.relative_to(DOWNLOAD_DIR))
            rows.append({"path": rel, "size": st.st_size, "mtime_ns": st.st_mtime_ns, "title": path.stem})
        except OSError:
            continue
    rows.sort(key=lambda x: x["path"].lower())
    return rows

async def fast_library_snapshot():
    rows = await asyncio.to_thread(_fast_file_library_sync)
    index = await asyncio.to_thread(_load_library_index_sync)
    cached = index.get("entries", {}) if isinstance(index, dict) else {}
    files=[]
    total=0
    for row in rows:
        total += int(row["size"] or 0)
        cached_row = cached.get(row["path"], {}) if isinstance(cached, dict) else {}
        title = cached_row.get("title") or row["title"]
        artist = cached_row.get("artist") or "Unknown Artist"
        album = cached_row.get("album") or "Unknown Album"
        enc = urllib.parse.quote(row["path"], safe="/")
        files.append({"name": row["path"], "title": title, "artist": artist, "album": album, "size": format_size(row["size"]), "bytes": row["size"], "duration": safe_float(cached_row.get("duration"), 0), "play_count": 0, "cover": "/api/library/cover/"+enc, "stream": "/api/library/stream/"+enc})
    # Cached metadata can provide artist/album counts before the full scan finishes.
    artists=set(); albums=set()
    for v in cached.values() if isinstance(cached, dict) else []:
        if v.get("artist"): artists.add(str(v["artist"]).strip().lower())
        if v.get("album"):
            albums.add((str(v.get("artist") or "Unknown Artist").strip().lower(), str(v["album"]).strip().lower()))
    return {"files": files, "total_size": format_size(total), "total_bytes": total, "artists_count": len(artists), "albums_count": len(albums), "ready": False, "storage": await asyncio.to_thread(storage_info_sync)}

def _build_library_index_entries_sync(library):
    entries = {}
    for song in library.get("songs", []):
        try:
            path = song["path"]
            st = path.stat()
            entries[str(path.relative_to(DOWNLOAD_DIR))] = {
                "mtime_ns": st.st_mtime_ns, "size": st.st_size,
                "title": song.get("title"), "artist": song.get("artist"),
                "album": song.get("album"), "album_artist": song.get("albumArtist"),
                "genre": song.get("genre"), "year": song.get("year"),
                "track": song.get("track"), "disc": song.get("disc"),
                "duration": song.get("duration"), "bit_rate": song.get("bit_rate"),
                "sample_rate": song.get("sample_rate"), "channels": song.get("channels"),
                "bit_depth": song.get("bit_depth"),
            }
        except Exception:
            pass
    return entries


async def persist_library_index(library):
    try:
        entries = await asyncio.to_thread(_build_library_index_entries_sync, library)
        await asyncio.to_thread(_save_library_index_sync, entries)
    except Exception as exc:
        print("Warning: could not save library index:", exc)

async def background_library_warmup():
    global LIBRARY_WARMUP_TASK
    try:
        library = await build_library(force=True)
        await persist_library_index(library)
    except Exception as exc:
        print("Library warmup failed:", exc)
    finally:
        LIBRARY_WARMUP_TASK = None

# ============================================================
# BUILD LIBRARY
# ============================================================

async def build_library(force=False):
    global LIBRARY_CACHE, LIBRARY_CACHE_TIME

    now = time.monotonic()
    if not force and LIBRARY_CACHE is not None and now - LIBRARY_CACHE_TIME < LIBRARY_CACHE_TTL:
        return LIBRARY_CACHE

    async with LIBRARY_CACHE_LOCK:
        now = time.monotonic()
        if not force and LIBRARY_CACHE is not None and now - LIBRARY_CACHE_TIME < LIBRARY_CACHE_TTL:
            return LIBRARY_CACHE

        files = await get_all_audio_files()
        files.sort(key=lambda path: str(path).lower())
        disk_index = await asyncio.to_thread(_load_library_index_sync)
        disk_entries = disk_index.get("entries", {}) if isinstance(disk_index, dict) else {}

        songs = []
        artists = {}
        albums = {}
        genres = {}

        metadata_semaphore = asyncio.Semaphore(LIBRARY_METADATA_CONCURRENCY)

        async def prepare_song_input(path):
            try:
                stat = await asyncio.to_thread(path.stat)
                rel = str(path.relative_to(DOWNLOAD_DIR))
                cached_disk = disk_entries.get(rel) if isinstance(disk_entries, dict) else None
                if cached_disk and int(cached_disk.get("mtime_ns", -1)) == int(stat.st_mtime_ns) and int(cached_disk.get("size", -1)) == int(stat.st_size):
                    metadata = dict(cached_disk)
                else:
                    async with metadata_semaphore:
                        metadata = await read_metadata(path)
                return path, stat, metadata
            except Exception:
                return path, None, None

        prepared = await asyncio.gather(
            *(prepare_song_input(path) for path in files),
            return_exceptions=False,
        )

        for path, stat, metadata in prepared:
            if stat is None or metadata is None:
                continue

            song_id = make_song_id(path)
            artist_name = clean_metadata_text(metadata.get("artist"), "Unknown Artist")
            album_artist = clean_metadata_text(metadata.get("album_artist"), artist_name)
            album_name = clean_metadata_text(metadata.get("album"), path.stem)
            artist_id = make_artist_id(artist_name)
            album_artist_id = make_artist_id(album_artist)
            album_id = make_album_id(album_artist, album_name)

            song = {
                "id": song_id,
                "title": clean_metadata_text(metadata.get("title"), path.stem),
                "artist": artist_name,
                "artistId": artist_id,
                "albumArtist": album_artist,
                "albumArtistId": album_artist_id,
                "album": album_name,
                "albumId": album_id,
                "genre": metadata.get("genre", ""),
                "year": metadata.get("year", ""),
                "track": metadata.get("track", 0),
                "disc": metadata.get("disc", 0),
                "duration": safe_int(metadata.get("duration"), 0),
                "bit_rate": safe_int(metadata.get("bit_rate"), 0),
                "bit_depth": safe_int(metadata.get("bit_depth"), 0),
                "sample_rate": safe_int(metadata.get("sample_rate"), 0),
                "channels": safe_int(metadata.get("channels"), 0),
                "replaygain_track_gain": metadata.get("replaygain_track_gain"),
                "replaygain_album_gain": metadata.get("replaygain_album_gain"),
                "replaygain_track_peak": metadata.get("replaygain_track_peak"),
                "replaygain_album_peak": metadata.get("replaygain_album_peak"),
                "has_artwork": bool(metadata.get("has_artwork")),
                "path": path,
                "suffix": path.suffix.lower(),
                "size": stat.st_size,
                "created": stat.st_ctime,
                "modified": stat.st_mtime,
            }
            songs.append(song)

            # Track artists own the tracks. Album artists also own the album
            # relationship so compilation/featured-artist metadata remains useful.
            artist_roles = {
                artist_id: artist_name,
                album_artist_id: album_artist,
            }
            for current_id, current_name in artist_roles.items():
                if current_id not in artists:
                    artists[current_id] = {
                        "id": current_id,
                        "name": current_name,
                        "albumIds": set(),
                        "songIds": [],
                    }
                artists[current_id]["albumIds"].add(album_id)
                if song_id not in artists[current_id]["songIds"]:
                    artists[current_id]["songIds"].append(song_id)

            if album_id not in albums:
                albums[album_id] = {
                    "id": album_id,
                    "name": album_name,
                    "artist": album_artist,
                    "artistId": album_artist_id,
                    "albumArtist": album_artist,
                    "year": metadata.get("year", ""),
                    "genre": metadata.get("genre", ""),
                    "songIds": [],
                    "path": path,
                }
            albums[album_id]["songIds"].append(song_id)

            if metadata.get("genre"):
                genres[metadata["genre"]] = genres.get(metadata["genre"], 0) + 1

        def song_sort_key(song):
            disc = safe_int(song.get("disc"), 0)
            track = safe_int(song.get("track"), 0)
            return (disc if disc > 0 else 9999, track if track > 0 else 9999, song["title"].lower(), str(song["path"]).lower())

        songs.sort(key=song_sort_key)
        song_by_id = {song["id"]: song for song in songs}
        for artist in artists.values():
            artist["albumIds"] = sorted(artist["albumIds"], key=lambda aid: albums[aid]["name"].lower())
            artist["songIds"] = sorted(artist["songIds"], key=lambda sid: song_sort_key(song_by_id[sid]))
        for album in albums.values():
            album["songIds"] = sorted(album["songIds"], key=lambda sid: song_sort_key(song_by_id[sid]))

        LIBRARY_CACHE = {
            "songs": songs,
            "artists": artists,
            "albums": albums,
            "genres": genres,
            "_songs_by_id": song_by_id,
            "_artists_by_id": dict(artists),
            "_albums_by_id": dict(albums),
        }
        LIBRARY_CACHE_TIME = time.monotonic()
        return LIBRARY_CACHE


async def find_song(song_id):
    library = await build_library()
    song_map = library.get("_songs_by_id")
    if not isinstance(song_map, dict):
        song_map = {song["id"]: song for song in library.get("songs", [])}
    return song_map.get(song_id)


async def find_artist(artist_id):
    library = await build_library()
    artist_map = library.get("_artists_by_id")
    if not isinstance(artist_map, dict):
        artist_map = library.get("artists", {})
    return artist_map.get(artist_id)


async def find_album(album_id):
    library = await build_library()
    album_map = library.get("_albums_by_id")
    if not isinstance(album_map, dict):
        album_map = library.get("albums", {})
    return album_map.get(album_id)


# ============================================================
# COVER ART
# ============================================================

def cover_cache_path(path):
    try:
        stat = path.stat()
        cache_key = f"{path}|{stat.st_mtime_ns}|{stat.st_size}"
    except OSError:
        cache_key = str(path)

    digest = hashlib.md5(
        cache_key.encode("utf-8")
    ).hexdigest()

    return (
        COVER_CACHE_DIR
        / f"{digest}.jpg"
    )


async def _cover_lock_for(cover):
    key = str(cover)
    async with COVER_LOCKS_GUARD:
        lock = COVER_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            COVER_LOCKS[key] = lock
        return lock


async def ensure_cover(path):
    cover = await asyncio.to_thread(cover_cache_path, path)
    lock = await _cover_lock_for(cover)
    async with lock:
        if await asyncio.to_thread(cover.exists):
            return cover
        command = ["ffmpeg", "-y", "-i", str(path), "-an", "-vcodec", "mjpeg", "-vframes", "1", str(cover)]
        try:
            await asyncio.to_thread(
                subprocess.run, command, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=10,
            )
        except Exception:
            return None
        return cover if await asyncio.to_thread(cover.exists) else None

async def resolve_cover_id(item_id):

    song = await find_song(item_id)

    if song:
        return song["path"]

    album = await find_album(item_id)

    if album:
        return album["path"]

    artist = await find_artist(item_id)

    if artist:

        library = await build_library()

        for album_id in artist["albumIds"]:

            album = library[
                "albums"
            ].get(
                album_id
            )

            if album:
                return album["path"]

    return None


# ============================================================
# DUPLICATES
# ============================================================

def cleanup_task_files(task_id):
    for path in DOWNLOAD_DIR.rglob(f"*{task_id}*"):
        try:
            if path.is_file():
                path.unlink()
        except Exception:
            pass



# ============================================================
# METADATA INTELLIGENCE
# ============================================================

_METADATA_CACHE = {}
_METADATA_LAST_MB_CALL = 0.0
_METADATA_RATE_LOCK = asyncio.Lock()


def _compact_identity(value):
    text = clean_metadata_text(value, "")
    text = re.sub(r"\b(feat\.?|ft\.?|featuring)\b.*$", "", text, flags=re.I)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip().casefold()


def _similarity(a, b):
    a1 = _compact_identity(a)
    b1 = _compact_identity(b)
    if not a1 or not b1:
        return 0.0
    if a1 == b1:
        return 1.0
    return difflib.SequenceMatcher(None, a1, b1).ratio()


def _http_json_sync(url, headers=None, timeout=12):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


async def _musicbrainz_lookup(artist, title, cleanup_rules=""):
    global _METADATA_LAST_MB_CALL
    artist = clean_metadata_text(artist, "")
    title = clean_title_with_rules(title, cleanup_rules)
    cache_key = ("mb", _compact_identity(artist), _compact_identity(title))
    if cache_key in _METADATA_CACHE:
        return _METADATA_CACHE[cache_key]
    if not artist or not title:
        return None
    async with _METADATA_RATE_LOCK:
        wait = 1.05 - (time.monotonic() - _METADATA_LAST_MB_CALL)
        if wait > 0:
            await asyncio.sleep(wait)
        _METADATA_LAST_MB_CALL = time.monotonic()
        query = f'artist:"{artist}" AND recording:"{title}"'
        url = "https://musicbrainz.org/ws/2/recording?" + urllib.parse.urlencode({
            "query": query,
            "fmt": "json",
            "limit": 8,
            "inc": "releases+artist-credits",
        })
        try:
            data = await asyncio.to_thread(_http_json_sync, url, {
                "User-Agent": "Xrob-Music/2.9.3 (metadata lookup)",
                "Accept": "application/json",
            })
        except Exception:
            return None
    recordings = data.get("recordings") or []
    best = None
    for rec in recordings:
        rec_title = rec.get("title") or ""
        credits = rec.get("artist-credit") or []
        rec_artist = " ".join(str(x.get("name") or x.get("artist", {}).get("name") or "").strip() for x in credits).strip()
        title_score = _similarity(title, rec_title)
        artist_score = _similarity(artist, rec_artist)
        score = title_score * 0.72 + artist_score * 0.28
        releases = rec.get("releases") or []
        release = next((r for r in releases if (r.get("title") or "").strip()), None)
        candidate = {
            "title": rec_title,
            "artist": rec_artist or artist,
            "album": (release or {}).get("title") or "",
            "score": score,
            "source": "MusicBrainz",
            "id": rec.get("id"),
        }
        if best is None or candidate["score"] > best["score"]:
            best = candidate
    if best:
        _cache_set_bounded(_METADATA_CACHE, cache_key, best, METADATA_CACHE_MAX)
    return best


async def _itunes_lookup(artist, title, cleanup_rules=""):
    artist = clean_metadata_text(artist, "")
    title = clean_title_with_rules(title, cleanup_rules)
    cache_key = ("itunes", _compact_identity(artist), _compact_identity(title))
    if cache_key in _METADATA_CACHE:
        return _METADATA_CACHE[cache_key]
    if not artist or not title:
        return None
    term = f"{artist} {title}"
    url = "https://itunes.apple.com/search?" + urllib.parse.urlencode({
        "term": term,
        "media": "music",
        "entity": "song",
        "limit": 10,
    })
    try:
        data = await asyncio.to_thread(_http_json_sync, url, {"User-Agent": "Xrob-Music/2.9.3"})
    except Exception:
        return None
    best = None
    for item in data.get("results") or []:
        cand_title = str(item.get("trackName") or "")
        cand_artist = str(item.get("artistName") or "")
        score = _similarity(title, cand_title) * 0.72 + _similarity(artist, cand_artist) * 0.28
        candidate = {
            "title": cand_title,
            "artist": cand_artist or artist,
            "album": str(item.get("collectionName") or ""),
            "score": score,
            "source": "Apple Music catalog",
            "id": item.get("trackId"),
        }
        if best is None or candidate["score"] > best["score"]:
            best = candidate
    if best:
        _cache_set_bounded(_METADATA_CACHE, cache_key, best, METADATA_CACHE_MAX)
    return best


async def resolve_source_metadata(url):
    """Ask yt-dlp for authoritative source metadata for URL-only/batch jobs."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *YT_DLP_COMMAND, "--no-playlist", "--skip-download", "--dump-single-json", "--no-warnings", url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(err.decode("utf-8", errors="ignore")[-600:] or "Source metadata lookup failed")
        payload = json.loads(out.decode("utf-8", errors="ignore"))
        if not isinstance(payload, dict):
            return {}
        title = clean_metadata_text(payload.get("track") or payload.get("title"), "")
        artist = clean_metadata_text(payload.get("artist") or payload.get("uploader") or payload.get("channel"), "")
        album = clean_metadata_text(payload.get("album") or payload.get("series"), "")
        return {"title": title, "artist": artist, "album": album}
    except Exception as exc:
        await write_app_error("source_metadata", str(exc))
        return {}


async def resolve_download_metadata(raw_title, artist, album, settings):
    rules_text = settings.get("title_cleanup_rules", "")
    title = clean_title_with_rules(raw_title or "Unknown Track", rules_text)
    catalog_title = normalize_catalog_title(title, rules_text)
    artist = clean_metadata_text(artist, "Unknown Artist")
    supplied_album = clean_metadata_text(album, "")
    result = {"title": catalog_title, "artist": artist, "album": supplied_album or artist, "confidence": 0.25, "source": "Supplied metadata", "reason": ""}

    mode = str(settings.get("metadata_mode") or "auto").lower()
    if mode == "off":
        return result

    candidates = []
    mb = await _musicbrainz_lookup(artist, catalog_title, rules_text) if mode in {"auto", "musicbrainz"} else {}
    it = await _itunes_lookup(artist, catalog_title, rules_text) if mode == "auto" else {}
    for cand in (mb, it):
        if cand and cand.get("score", 0) >= 0.55:
            candidates.append(cand)

    if candidates:
        candidates.sort(key=lambda x: x.get("score", 0), reverse=True)
        best = candidates[0]
        # Catalog data is evidence, never a reason to overwrite a clearly supplied album.
        chosen_album = supplied_album or clean_metadata_text(best.get("album"), "")
        result.update({
            "title": normalize_catalog_title(best.get("title") or catalog_title, rules_text),
            "artist": clean_metadata_text(best.get("artist"), artist),
            "album": chosen_album or artist,
            "confidence": float(best.get("score", 0.0)),
            "source": best.get("source") or "Catalog",
        })
    else:
        result["title"] = catalog_title

    result["album"] = clean_metadata_text(result.get("album"), "") or result["artist"] or "Unknown Artist"
    return result


async def refresh_after_download(final_path):
    try:
        async with LIBRARY_REFRESH_LOCK:
            library_now = await build_library(force=False)
            matched = next(
                (x for x in library_now.get("songs", []) if str(x.get("path")) == str(final_path)),
                None,
            )
            if matched:
                await asyncio.to_thread(_mark_song_review_sync, matched["id"], "pending", 0.0)
            else:
                # A download can finish just after a cached scan. Invalidate once
                # and rescan only when this completed path is genuinely missing.
                invalidate_library_cache()
                library_now = await build_library(force=True)
                await persist_library_index(library_now)
                matched = next(
                    (x for x in library_now.get("songs", []) if str(x.get("path")) == str(final_path)),
                    None,
                )
                if matched:
                    await asyncio.to_thread(_mark_song_review_sync, matched["id"], "pending", 0.0)
    except Exception as exc:
        await write_app_error("library_refresh_after_download", str(exc))


# ============================================================
# DOWNLOAD WORKER
# ============================================================

async def download_worker():

    while True:

        queue_item = await TASK_QUEUE.get()
        if isinstance(queue_item, (tuple, list)) and len(queue_item) == 2:
            task_id, queue_token = queue_item
        else:
            task_id, queue_token = queue_item, None

        process = None
        try:

            task = TASKS.get(task_id)

            if not task:
                continue

            if queue_token is not None and task.get("queue_token") != queue_token:
                # A cancelled/retried job may leave its old queue entry behind.
                # Tokens make that stale entry harmless.
                continue

            if task.get("cancel_requested"):

                task["status"] = "cancelled"
                task["step"] = "Cancelled"
                task["last_updated"] = (
                    time.time() * 1000
                )

                await notify_task_update(
                    task,
                    force_save=True,
                )

                continue

            settings = await load_settings_async()

            if str(task.get("title") or "").strip().casefold() in {"unknown track", "unknown", ""}:
                source_meta = await resolve_source_metadata(str(task.get("url") or ""))
                if source_meta:
                    task["title"] = normalize_catalog_title(source_meta.get("title") or task.get("title") or "Unknown Track", settings.get("title_cleanup_rules", ""))
                    task["artist"] = clean_metadata_text(source_meta.get("artist"), task.get("artist") or "Unknown Artist")
                    task["album"] = clean_metadata_text(source_meta.get("album"), task.get("album") or "")
                    task["identity_key"] = f"{normalize_duplicate_key(task.get('title',''), task.get('artist',''))}|{hashlib.sha256(str(task.get('url','')).encode('utf-8')).hexdigest()}"
                    await notify_task_update(task, force_save=True)

            # Re-check the lightweight library index at worker time as well. This
            # prevents a duplicate when the library changed after Save was clicked.
            existing = await find_existing_track(
                task.get("title", "Unknown Track"),
                task.get("artist", "Unknown Artist"),
            )
            if existing:
                task["status"] = "completed"
                task["percent"] = 100
                task["speed"] = ""
                task["step"] = "Already in library"
                task["final_name"] = existing
                task["last_updated"] = time.time() * 1000
                await notify_task_update(task, force_save=True)
                continue

            fmt = settings.get(
                "audio_format",
                "mp3",
            )

            quality = settings.get(
                "audio_quality",
                "320K",
            )

            task["status"] = "downloading"
            task["step"] = "Downloading stream..."
            task["last_updated"] = (
                time.time() * 1000
            )

            await notify_task_update(
                task,
                force_save=True,
            )

            output_template = str(
                DOWNLOAD_DIR
                / f"{task_id}.%(ext)s"
            )

            command = [
                *YT_DLP_COMMAND,
                "--no-playlist",
                "-x",
                "--audio-format",
                fmt,
                "--audio-quality",
                quality,
                "--newline",
                "--continue",
                "--no-overwrites",
                "--part",
                "-o",
                output_template,
            ]

            artwork_behavior = str(settings.get("artwork_behavior") or ("embed" if settings.get("embed_thumbnail", True) else "none")).lower()
            if artwork_behavior == "embed":
                command.append("--embed-thumbnail")
            elif artwork_behavior == "download":
                command.extend(["--write-thumbnail", "--convert-thumbnails", "jpg"])

            if settings.get(
                "embed_metadata",
                True,
            ):
                command.append(
                    "--add-metadata"
                )

            command.append(
                task["url"]
            )

            process = (
                await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            )

            ACTIVE_PROCESSES[task_id] = process
            process_output = []

            progress_regex = re.compile(
                r"\[download\]\s+~?\s*(\d+(?:\.\d+)?)%"
            )

            speed_regex = re.compile(
                r"at\s+([~0-9a-zA-Z./]+)"
            )

            while True:

                try:
                    line = await asyncio.wait_for(
                        process.stdout.readline(),
                        timeout=DOWNLOAD_OUTPUT_IDLE_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError as exc:
                    raise RuntimeError(
                        f"Download produced no progress output for {DOWNLOAD_OUTPUT_IDLE_TIMEOUT_SECONDS} seconds"
                    ) from exc

                if not line:
                    break

                text = line.decode(
                    "utf-8",
                    errors="ignore",
                ).strip()

                if text:
                    process_output.append(text)
                    if len(process_output) > 100:
                        process_output.pop(0)

                pct_match = progress_regex.search(
                    text
                )

                if pct_match:

                    task["percent"] = float(
                        pct_match.group(1)
                    )

                    speed_match = speed_regex.search(
                        text
                    )

                    if speed_match:
                        task["speed"] = (
                            speed_match.group(1)
                            .replace("~", "")
                        )

                    task["last_updated"] = (
                        time.time() * 1000
                    )

                    await notify_task_update(
                        task
                    )

                elif "[EmbedThumbnail]" in text:
                    task["status"] = "processing"; task["percent"] = 94; task["step"] = "Artwork..."
                    task["last_updated"] = time.time() * 1000
                    await notify_task_update(task, force_save=True)
                elif "[Metadata]" in text:
                    task["status"] = "processing"; task["percent"] = 92; task["step"] = "Metadata..."
                    task["last_updated"] = time.time() * 1000
                    await notify_task_update(task, force_save=True)
                elif any(marker in text for marker in ("[ExtractAudio]", "[Fixup]")):
                    task["status"] = "processing"
                    task["percent"] = 88
                    task["step"] = "Processing audio..."
                    task["last_updated"] = (
                        time.time() * 1000
                    )

                    await notify_task_update(
                        task,
                        force_save=True,
                    )

            await process.wait()

            ACTIVE_PROCESSES.pop(
                task_id,
                None,
            )

            if task.get("cancel_requested"):

                await asyncio.to_thread(
                    cleanup_task_files,
                    task_id,
                )

                task["status"] = "cancelled"
                task["step"] = "Cancelled"
                task["last_updated"] = (
                    time.time() * 1000
                )

                await notify_task_update(
                    task,
                    force_save=True,
                )

                continue

            if process.returncode != 0:

                error_text = "\n".join(process_output)

                def partial_exists_sync():
                    try:
                        return any(path.is_file() for path in DOWNLOAD_DIR.glob(f"{task_id}.*") if path.suffix.lower() in {".part", ".ytdl"})
                    except OSError:
                        return False
                task["resume_available"] = await asyncio.to_thread(partial_exists_sync)
                task["status"] = "error"
                task["step"] = "Download failed — retry available" if task.get("resume_available") else "Download failed"
                task["error"] = (error_text[-1200:] or "yt-dlp failed.")
                task["last_updated"] = time.time() * 1000

                settings_retry = await load_settings_async()
                retry_limit = safe_int(settings_retry.get("download_retry_limit"), 2)
                auto_retry = bool(settings_retry.get("auto_retry_downloads", True))
                retries = safe_int(task.get("retry_count"), 0)
                await notify_task_update(task, force_save=True)
                if auto_retry and retries < retry_limit and not task.get("cancel_requested"):
                    task["status"] = "queued"; task["step"] = f"Retrying automatically ({retries + 1}/{retry_limit})..."; task["percent"] = max(0, min(89, safe_float(task.get("percent"), 0))); task["retry_count"] = retries + 1; task["queue_token"] = uuid.uuid4().hex; task["last_updated"] = time.time() * 1000
                    await notify_task_update(task, force_save=True)
                    await asyncio.sleep(max(1, safe_int(settings_retry.get("download_retry_backoff_seconds"), 3)) * (2 ** max(0, retries)))
                    await TASK_QUEUE.put((task_id, task["queue_token"]))
                continue

            def find_downloaded_files_sync():
                try:
                    candidates = list(DOWNLOAD_DIR.glob(f"{task_id}.*"))
                except OSError:
                    return []
                return [path for path in candidates if path.is_file() and path.suffix.lower() not in {".part", ".ytdl", ".temp"}]

            possible_files = await asyncio.to_thread(find_downloaded_files_sync)

            if not possible_files:

                task["status"] = "error"
                task["step"] = "Download failed"
                task["error"] = (
                    "Downloaded file not found."
                )
                task["last_updated"] = (
                    time.time() * 1000
                )

                await notify_task_update(
                    task,
                    force_save=True,
                )

                continue

            audio_file = possible_files[0]

            extension = (
                audio_file.suffix
                or f".{fmt}"
            )
            clean_title = clean_filename(
                normalize_catalog_title(
                    task.get("title", "Unknown Track"),
                    settings.get("title_cleanup_rules", ""),
                )
            ) or "Unknown Track"

            resolved = await resolve_download_metadata(
                task.get("title", "Unknown Track"),
                task.get("artist", "Unknown Artist"),
                task.get("album", ""),
                settings,
            )
            task["title"] = resolved["title"]
            task["artist"] = resolved["artist"]
            task["album"] = resolved["album"] or task["artist"] or "Unknown Artist"
            task["metadata_confidence"] = round(float(resolved.get("confidence", 0.0)) * 100)
            task["metadata_source"] = resolved.get("source", "Fallback")
            task["metadata_reason"] = resolved.get("reason", "")
            clean_title = clean_filename(normalize_catalog_title(task["title"], settings.get("title_cleanup_rules", ""))) or "Unknown Track"
            task["status"] = "processing"
            task["percent"] = 93
            task["step"] = "Metadata complete"
            task["last_updated"] = time.time() * 1000
            await notify_task_update(task, force_save=True)
            artwork_behavior = str(settings.get("artwork_behavior") or ("embed" if settings.get("embed_thumbnail", True) else "none")).lower()
            if artwork_behavior != "none":
                task["percent"] = 97
                task["step"] = "Artwork..."
                task["last_updated"] = time.time() * 1000
                await notify_task_update(task, force_save=True)
            if settings.get("embed_metadata", True):
                task["status"] = "processing"
                task["percent"] = 96
                task["step"] = "Finalizing metadata..."
                task["last_updated"] = time.time() * 1000
                await notify_task_update(task, force_save=True)

                clean_file = DOWNLOAD_DIR / f"clean_{task_id}{extension}"
                clean_command = [
                    *FFMPEG_COMMAND, "-y", "-i", str(audio_file), "-map", "0", "-c", "copy",
                    "-metadata", f"title={clean_title}",
                    "-metadata", f"artist={task.get('artist', 'Unknown Artist')}",
                    "-metadata", f"album={task.get('album') or task.get('artist') or "Unknown Artist"}",
                    str(clean_file),
                ]
                clean_process = await asyncio.create_subprocess_exec(
                    *clean_command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                ACTIVE_PROCESSES[task_id] = clean_process
                _, clean_stderr = await communicate_with_timeout(
                    clean_process, METADATA_REWRITE_TIMEOUT_SECONDS, "Metadata rewrite"
                )
                ACTIVE_PROCESSES.pop(task_id, None)

                if task.get("cancel_requested"):
                    await asyncio.to_thread(cleanup_task_files, task_id)
                    task["status"] = "cancelled"
                    task["step"] = "Cancelled"
                    task["percent"] = 0
                    task["last_updated"] = time.time() * 1000
                    await notify_task_update(task, force_save=True)
                    continue

                clean_ready = clean_process.returncode == 0 and await asyncio.to_thread(clean_file.exists)
                if clean_ready:
                    try:
                        await asyncio.to_thread(audio_file.unlink)
                    except OSError:
                        pass
                    audio_file = clean_file
                else:
                    # Keep the downloaded file when metadata rewriting fails; the
                    # download itself is still usable.
                    print(
                        "Warning: metadata rewrite failed:",
                        clean_stderr.decode("utf-8", errors="ignore")[-1000:],
                    )

            artist = clean_filename(
                task.get(
                    "artist",
                    "Unknown Artist",
                )
            )

            task["status"] = "processing"
            task["percent"] = 99
            task["step"] = "Adding to library..."
            task["last_updated"] = time.time() * 1000
            await notify_task_update(task, force_save=True)

            async with DOWNLOAD_GUARD:
                if settings.get("organize_by_artist", False):
                    final_dir = DOWNLOAD_DIR / artist
                else:
                    final_dir = DOWNLOAD_DIR
                await asyncio.to_thread(final_dir.mkdir, parents=True, exist_ok=True)
                filename_mode = str(settings.get("filename_mode") or "title")
                if filename_mode == "artist-title":
                    base_name = f"{clean_filename(task.get('artist') or 'Unknown Artist')} - {clean_title}"
                elif filename_mode == "artist-album-title":
                    base_name = f"{clean_filename(task.get('artist') or 'Unknown Artist')} - {clean_filename(task.get('album') or '')} - {clean_title}"
                else:
                    base_name = clean_title
                base_name = clean_filename(base_name) or "Unknown Track"
                final_name = f"{base_name}{extension}"
                final_path = final_dir / final_name
                if await asyncio.to_thread(final_path.exists):
                    final_name = f"{clean_title}_{task_id[:4]}{extension}"
                    final_path = final_dir / final_name
                await asyncio.to_thread(shutil.move, str(audio_file), str(final_path))
                if artwork_behavior == "download":
                    try:
                        art_candidates=[p for p in DOWNLOAD_DIR.glob(f"{task_id}.*") if p.is_file() and p.suffix.lower() in {".jpg",".jpeg",".png",".webp"}]
                        if art_candidates:
                            art_target=final_path.with_suffix(".jpg")
                            await asyncio.to_thread(shutil.move, str(art_candidates[0]), str(art_target))
                    except Exception as art_exc:
                        await write_app_error("artwork", str(art_exc), task_id)

            task["final_name"] = str(
                final_path.relative_to(
                    DOWNLOAD_DIR
                )
            )

            task["status"] = "completed"
            task["percent"] = 100
            task["speed"] = ""
            task["step"] = "Ready"
            task["error"] = ""
            task["last_updated"] = (
                time.time() * 1000
            )

            METADATA_CACHE.pop(str(final_path), None)
            invalidate_library_cache()
            # Do not block completion on a full-library metadata rebuild. The file
            # is already safely in the library; queue a background refresh that also
            # persists the duplicate-detection index and adds the song to the editor.
            track_background_task(refresh_after_download(final_path))

            await notify_task_update(
                task,
                force_save=True,
            )

        except asyncio.CancelledError:
            raise

        except Exception as error:

            task = TASKS.get(task_id)

            ACTIVE_PROCESSES.pop(
                task_id,
                None,
            )

            if task and not task.get("resume_available"):
                try:
                    task["resume_available"] = any(path.is_file() for path in DOWNLOAD_DIR.glob(f"{task_id}.*") if path.suffix.lower() in {".part", ".ytdl"})
                except OSError:
                    task["resume_available"] = False
            if task and not task.get("resume_available"):
                await asyncio.to_thread(cleanup_task_files, task_id)

            if task:

                task["status"] = "error"
                task["step"] = (
                    "Unexpected error"
                )
                task["error"] = str(error)
                task["last_updated"] = (
                    time.time() * 1000
                )

                await notify_task_update(
                    task,
                    force_save=True,
                )
                await write_app_error("download_worker", str(error), task_id)

        finally:
            active = ACTIVE_PROCESSES.pop(task_id, None) or process
            if active is not None and getattr(active, "returncode", None) is None:
                try:
                    active.terminate()
                    await asyncio.wait_for(active.wait(), timeout=3)
                except Exception:
                    try:
                        active.kill()
                    except Exception:
                        pass
            TASK_QUEUE.task_done()


# ============================================================
# STARTUP
# ============================================================

async def startup_event():

    await asyncio.to_thread(configure_storage)
    await asyncio.to_thread(init_db)
    await asyncio.to_thread(_ensure_secure_web_credentials_sync)

    global TASKS

    TASKS = await asyncio.to_thread(
        db_load_tasks_sync
    )

    now = time.time() * 1000

    for task in TASKS.values():

        if task.get(
            "status"
        ) in {
            "downloading",
            "processing",
        }:

            task["status"] = "queued"
            task["step"] = (
                "Recovered after restart"
            )
            task["cancel_requested"] = False
            task["last_updated"] = now
            task["queue_token"] = uuid.uuid4().hex
            try:
                task["resume_available"] = any(path.is_file() for path in DOWNLOAD_DIR.glob(f"{task['id']}.*") if path.suffix.lower() in {".part", ".ytdl"})
            except OSError:
                task["resume_available"] = False
            task["identity_key"] = task.get("identity_key") or f"{normalize_duplicate_key(task.get('title',''), task.get('artist',''))}|{hashlib.sha256(str(task.get('url','')).encode('utf-8')).hexdigest()}"

            await db_save_task(
                task,
                force=True,
            )

    global LIBRARY_WARMUP_TASK, DOWNLOAD_WORKER_TASKS, SCHEDULED_SCANNER_TASK
    if not DOWNLOAD_WORKER_TASKS:
        settings = await load_settings_async()
        workers = max(1, min(8, safe_int(settings.get("max_concurrent_downloads"), MAX_CONCURRENT_DOWNLOADS)))
        for _ in range(workers):
            DOWNLOAD_WORKER_TASKS.add(asyncio.create_task(download_worker()))

    if SCHEDULED_SCANNER_TASK is None or SCHEDULED_SCANNER_TASK.done():
        SCHEDULED_SCANNER_TASK = asyncio.create_task(scheduled_library_scanner())

    if LIBRARY_WARMUP_TASK is None or LIBRARY_WARMUP_TASK.done():
        LIBRARY_WARMUP_TASK = asyncio.create_task(background_library_warmup())

    for task in TASKS.values():

        if task.get(
            "status"
        ) == "queued":

            await TASK_QUEUE.put((task["id"], task["queue_token"]))


# ============================================================
# WEBSOCKET ENDPOINT
# ============================================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # HTTP middleware does not authenticate WebSocket handshakes, so check the
    # same session cookie explicitly before accepting the connection.
    if not _is_authenticated(websocket.cookies.get(AUTH_COOKIE)):
        await websocket.close(code=1008)
        return

    await manager.connect(websocket)
    current_state = await get_player_state_async()
    if current_state:
        try:
            initial_state = dict(current_state["state"])
            initial_state["_serverUpdatedAt"] = current_state["updated_at"]
            initial_state["_serverLastSeenAt"] = current_state.get("last_seen_at", current_state["updated_at"])
            initial_state["_serverCurrentTime"] = current_state["state"].get("currentTime", 0)
            initial_state["_serverActive"] = bool(current_state.get("active"))
            initial_state["_serverStale"] = bool(current_state.get("stale"))
            initial_state["_serverPersistent"] = bool(current_state.get("persistent"))
            await websocket.send_json({"type": "player_state", "state": initial_state})
        except Exception:
            manager.disconnect(websocket)
            return

    try:
        while True:
            message = await websocket.receive_text()
            if message == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)


# WEB APP
# ============================================================

@app.get("/")
async def home():
    return FileResponse(
        STATIC_DIR / "index.html"
    )


@app.get("/api/health")
async def api_health():

    return {
        "status": "ok",
        "server": "Xrob Music",
        "version": SERVER_VERSION,
        "openSubsonic": True,
    }


@app.get("/api/settings")
async def api_get_settings():
    return await public_settings_async()


@app.post("/api/settings")
async def api_post_settings(
    data: dict = Body(...),
):
    try:
        await save_settings_async(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await public_settings_async()


# ============================================================
# YOUTUBE SEARCH
# ============================================================

SUBPROCESS_TIMEOUT_SECONDS = 45
PREVIEW_LOOKUP_TIMEOUT_SECONDS = 30
PREVIEW_STREAM_TIMEOUT_SECONDS = 180
DOWNLOAD_OUTPUT_IDLE_TIMEOUT_SECONDS = 120
METADATA_REWRITE_TIMEOUT_SECONDS = 60


async def communicate_with_timeout(process, timeout, label="process"):
    try:
        return await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        except Exception:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except Exception:
            pass
        raise RuntimeError(f"{label} timed out after {timeout} seconds")

async def youtube_search(
    query,
    max_results,
    page=1,
):

    start = (
        (page - 1)
        * max_results
        + 1
    )

    end = page * max_results

    command = [
        *YT_DLP_COMMAND,
        "--flat-playlist",
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
        "--playlist-start",
        str(start),
        "--playlist-end",
        str(end),
        f"ytsearch{end}:{query}",
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "yt-dlp is not installed in the Xrob Music container. Rebuild the add-on so requirements.txt is installed."
        ) from exc

    stdout, stderr = await communicate_with_timeout(process, SUBPROCESS_TIMEOUT_SECONDS, "YouTube search")

    if process.returncode != 0:

        raise RuntimeError(
            stderr.decode(
                "utf-8",
                errors="ignore",
            )[-2000:]
            or "yt-dlp search failed."
        )

    try:

        data = json.loads(
            stdout.decode(
                "utf-8",
                errors="ignore",
            )
        )

    except json.JSONDecodeError:

        raise RuntimeError(
            "Invalid YouTube search response."
        )

    results = []

    for item in data.get(
        "entries",
        [],
    ):

        if not item:
            continue

        video_id = item.get("id")

        if not video_id:
            continue

        duration = safe_int(
            item.get("duration", 0),
            0,
        )

        results.append(
            {
                "id": video_id,
                "title": item.get(
                    "title",
                    "Unknown Track",
                ),
                "channel": (
                    item.get("channel")
                    or item.get("uploader")
                    or "Unknown Artist"
                ),
                "duration": duration,
                "duration_text": format_duration(
                    duration
                ),
                "thumbnail": (
                    item.get("thumbnail")
                    or (
                        "https://i.ytimg.com/"
                        f"vi/{video_id}/"
                        "hqdefault.jpg"
                    )
                ),
                "url": (
                    "https://www.youtube.com/"
                    f"watch?v={video_id}"
                ),
            }
        )

    return results


def _library_duplicate_keys_sync():
    keys = set()
    try:
        index = _load_library_index_sync()
        entries = index.get("entries", {}) if isinstance(index, dict) else {}
        for rel, cached in entries.items():
            if not isinstance(cached, dict):
                cached = {}
            title = cached.get("title") or Path(str(rel)).stem
            artist = cached.get("artist") or "Unknown Artist"
            keys.add(normalize_duplicate_key(title, artist))
    except Exception:
        pass
    return keys


def _library_search_sync(query: str, page: int, limit: int = 20):
    query = normalize_identity_text(query)
    if not query:
        return []
    index = _load_library_index_sync()
    entries = index.get("entries", {}) if isinstance(index, dict) else {}
    rows = []
    for rel, cached in entries.items():
        if not isinstance(cached, dict):
            cached = {}
        rel = str(rel)
        path = DOWNLOAD_DIR / rel
        if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        title = str(cached.get("title") or path.stem)
        artist = str(cached.get("artist") or "Unknown Artist")
        album = str(cached.get("album") or "Unknown Album")
        haystack = " ".join((title, artist, album, rel))
        if query not in normalize_identity_text(haystack):
            continue
        enc = urllib.parse.quote(rel, safe="/")
        duration = safe_float(cached.get("duration"), 0)
        rows.append({
            "id": make_song_id(path),
            "title": title,
            "artist": artist,
            "album": album,
            "name": rel,
            "duration": duration,
            "duration_text": format_duration(duration),
            "cover": "/api/library/cover/" + enc,
            "stream": "/api/library/stream/" + enc,
            "source": "library",
        })
    # The index may be empty just after first install. Fall back to a light filesystem snapshot.
    if not rows:
        for row in _fast_file_library_sync():
            rel = str(row["path"])
            path = DOWNLOAD_DIR / rel
            cached = entries.get(rel, {}) if isinstance(entries, dict) else {}
            title = str(cached.get("title") or path.stem)
            artist = str(cached.get("artist") or "Unknown Artist")
            album = str(cached.get("album") or "Unknown Album")
            if query not in normalize_identity_text(" ".join((title, artist, album, rel))):
                continue
            enc = urllib.parse.quote(rel, safe="/")
            duration = safe_float(cached.get("duration"), 0)
            rows.append({"id":make_song_id(path),"title":title,"artist":artist,"album":album,"name":rel,"duration":duration,"duration_text":format_duration(duration),"cover":"/api/library/cover/"+enc,"stream":"/api/library/stream/"+enc,"source":"library"})
    rows.sort(key=lambda x: (str(x.get("artist") or "").casefold(), str(x.get("album") or "").casefold(), str(x.get("title") or "").casefold()))
    start = max(0, page - 1) * limit
    return rows[start:start + limit]


@app.get("/api/search")
async def api_search(
    q: str = Query(..., min_length=1, max_length=256),
    page: int = Query(1, ge=1, le=1000),
    source: str = Query("youtube"),
):

    if not q.strip():
        return []

    source = source.strip().lower()
    if source not in {"youtube", "library"}:
        raise HTTPException(status_code=400, detail="source must be 'youtube' or 'library'")

    try:
        if source == "library":
            return await asyncio.to_thread(_library_search_sync, q, page, 20)

        results = await youtube_search(q, 20, page)
        library_keys = await asyncio.to_thread(_library_duplicate_keys_sync)
        active_keys = {
            normalize_duplicate_key(task.get("title", ""), task.get("artist", ""))
            for task in TASKS.values()
            if task.get("status") in {"queued", "downloading", "processing"}
        }
        for item in results:
            key = normalize_duplicate_key(item.get("title", ""), item.get("channel", "Unknown Artist"))
            item["already_downloaded"] = bool(key and key in library_keys)
            item["already_queued"] = bool(key and key in active_keys)
            item["source"] = "youtube"
        return results

    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))


# ============================================================
def validate_media_url(raw_url):
    value = str(raw_url or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="URL missing")
    if len(value) > PLAYER_STATE_MAX_URL:
        raise HTTPException(status_code=400, detail="URL is too long")
    try:
        parsed = urllib.parse.urlparse(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid URL") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Only HTTP(S) media URLs are supported")
    if any(ch in value[:4096] for ch in ("\x00", "\r", "\n")):
        raise HTTPException(status_code=400, detail="Invalid URL")
    return value


# PREVIEW
# ============================================================

@app.get("/api/preview")
async def api_preview(
    url: str = Query(...),
):

    url = validate_media_url(url)

    try:
        process = await asyncio.create_subprocess_exec(
            *YT_DLP_COMMAND,
            "-g",
            "-f",
            "ba/bestaudio/b",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503,
            detail="yt-dlp is not installed in the Xrob Music container. Rebuild the add-on so requirements.txt is installed.",
        ) from exc

    stdout, stderr = await communicate_with_timeout(process, PREVIEW_LOOKUP_TIMEOUT_SECONDS, "Preview lookup")

    if (
        process.returncode != 0
        or not stdout
    ):

        raise HTTPException(
            status_code=500,
            detail=(
                stderr.decode(
                    "utf-8",
                    errors="ignore",
                )[-1000:]
                or "Preview unavailable."
            ),
        )

    direct_url = (
        stdout.decode(
            "utf-8",
            errors="ignore",
        )
        .strip()
        .splitlines()[0]
    )

    ffmpeg = (
        await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-i",
            direct_url,
            "-t",
            "120",
            "-f",
            "mp3",
            "-ab",
            "128k",
            "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    )

    async def generator():
        started = time.monotonic()
        try:
            while True:
                if time.monotonic() - started > PREVIEW_STREAM_TIMEOUT_SECONDS:
                    raise RuntimeError("Preview stream timed out")
                chunk = await asyncio.wait_for(ffmpeg.stdout.read(64 * 1024), timeout=15)
                if not chunk:
                    break
                yield chunk
        except (asyncio.TimeoutError, RuntimeError):
            try:
                if ffmpeg.returncode is None:
                    ffmpeg.kill()
            except Exception:
                pass
        finally:

            if ffmpeg.returncode is None:

                try:
                    ffmpeg.kill()
                except ProcessLookupError:
                    pass
                except Exception:
                    pass
            try:
                await ffmpeg.wait()
            except Exception:
                pass

    return StreamingResponse(
        generator(),
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "no-cache",
            "Access-Control-Allow-Origin": "*",
        },
    )


# ============================================================
# DOWNLOAD API
# ============================================================

def find_existing_track_fast_sync(title, artist):
    """Check the persisted library index without reading every audio tag."""
    target_key = normalize_duplicate_key(title, artist)
    if not target_key:
        return None

    try:
        index = _load_library_index_sync()
        entries = index.get("entries", {}) if isinstance(index, dict) else {}
        for rel, cached in entries.items():
            if not isinstance(cached, dict):
                cached = {}
            cached_title = cached.get("title") or Path(str(rel)).stem
            cached_artist = cached.get("artist") or "Unknown Artist"
            key = normalize_duplicate_key(cached_title, cached_artist)
            if key == target_key:
                path = DOWNLOAD_DIR / str(rel)
                if path.is_file():
                    return str(rel)
    except Exception:
        pass
    return None


async def find_existing_track(title, artist):
    return await asyncio.to_thread(find_existing_track_fast_sync, title, artist)


@app.post("/api/download")
async def api_download(
    payload: dict = Body(...),
):

    url = validate_media_url(payload.get("url"))

    settings = await load_settings_async()
    task_title = normalize_catalog_title(
        str(payload.get("title", "Unknown Track") or "Unknown Track"),
        settings.get("title_cleanup_rules", ""),
    )
    task_artist = clean_metadata_text(
        str(payload.get("artist", "Unknown Artist") or "Unknown Artist"),
        "Unknown Artist",
    )
    raw_album = payload.get("album")
    task_album = str(raw_album).strip() if raw_album else ""
    if task_album.casefold() in {"unknown album", "unknown"}:
        task_album = ""

    # Make the Save operation idempotent. The lock prevents two rapid/concurrent
    # clicks from both passing the duplicate check before either task is registered.
    async with DOWNLOAD_GUARD:
        target_key = normalize_duplicate_key(task_title, task_artist)
        identity_key = f"{target_key}|{hashlib.sha256(url.encode("utf-8")).hexdigest()}"
        for task in TASKS.values():
            task_key = task.get("identity_key") or f"{normalize_duplicate_key(task.get("title", ""), task.get("artist", ""))}|{hashlib.sha256(str(task.get("url", "")).encode("utf-8")).hexdigest()}"
            if task_key == identity_key and task.get("status") in {"queued", "downloading", "processing"}:
                return {"status": "already_queued", "task_id": task["id"]}
            if task.get("url") == url and task.get("status") in {"queued", "downloading", "processing"}:
                return {"status": "already_queued", "task_id": task["id"]}

        existing = await find_existing_track(task_title, task_artist)
        if existing:
            return {
                "status": "already_downloaded",
                "file": existing,
                "title": task_title,
                "artist": task_artist,
            }

        task_id = uuid.uuid4().hex[:12]
        task = {
            "id": task_id,
            "title": task_title,
            "artist": task_artist,
            "album": task_album,
            "url": url,
            "elementId": str(payload.get("elementId", "")),
            "status": "queued",
            "percent": 0,
            "speed": "",
            "step": "Queued...",
            "error": "",
            "last_updated": time.time() * 1000,
            "final_name": "",
            "cancel_requested": False,
            "created_at": time.time() * 1000,
            "queue_token": uuid.uuid4().hex,
            "identity_key": identity_key,
            "retry_count": 0,
            "resume_available": False,
        }
        TASKS[task_id] = task
        queue_token = task["queue_token"]

    await notify_task_update(task, force_save=True)
    await TASK_QUEUE.put((task_id, queue_token))

    return {
        "status": "ok",
        "task_id": task_id,
        "task": task,
    }


@app.post("/api/download/batch")
async def api_download_batch(payload: dict = Body(...)):
    urls=payload.get("urls") if isinstance(payload,dict) else []
    if not isinstance(urls,list): raise HTTPException(400,"urls must be an array")
    clean_urls=[]
    seen=set()
    for raw in urls[:200]:
        try: value=validate_media_url(raw)
        except HTTPException: continue
        if value not in seen:
            seen.add(value); clean_urls.append(value)
    if not clean_urls: raise HTTPException(400,"No valid URLs supplied")
    title=str(payload.get("title") or "").strip()[:256]
    artist=str(payload.get("artist") or "").strip()[:256]
    album=str(payload.get("album") or "").strip()[:256]
    results=[]
    for url in clean_urls:
        try:
            result=await api_download({"url":url,"title":title or "Unknown Track","artist":artist or "Unknown Artist","album":album})
            results.append({"url":url,**result})
        except HTTPException as exc:
            results.append({"url":url,"status":"error","error":str(exc.detail)})
    return {"status":"ok","count":len(results),"results":results}

@app.get("/api/downloads/history")
async def api_download_history(limit:int=Query(300,ge=1,le=2000)):
    return {"history":await asyncio.to_thread(db_load_download_history_sync,limit)}

@app.delete("/api/downloads/history")
async def api_download_history_clear():
    await asyncio.to_thread(db_clear_download_history_sync)
    return {"status":"cleared"}

@app.get("/api/tasks")
async def api_tasks():

    tasks = list(
        TASKS.values()
    )

    tasks.sort(
        key=lambda task: (
            0 if task.get("status") in {"queued", "downloading", "processing"} else 1,
            safe_float(task.get("created_at", task.get("last_updated", 0)), 0) if task.get("status") in {"queued", "downloading", "processing"} else -safe_float(task.get("last_updated", 0), 0),
        )
    )

    return tasks


@app.post(
    "/api/tasks/{task_id}/cancel"
)
async def api_cancel_task(
    task_id: str,
):

    task = TASKS.get(task_id)

    if not task:
        raise HTTPException(
            status_code=404,
            detail="Task not found",
        )

    task["cancel_requested"] = True

    process = ACTIVE_PROCESSES.get(
        task_id
    )

    if process:

        try:
            process.terminate()
        except Exception:
            pass

    task["status"] = "cancelled"
    task["step"] = "Cancelled"
    task["last_updated"] = (
        time.time() * 1000
    )

    await notify_task_update(
        task,
        force_save=True,
    )

    return {
        "status": "cancelled",
        "task_id": task_id,
    }


@app.post("/api/tasks/{task_id}/retry")
async def api_retry_task(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.get("status") not in {"error", "failed", "cancelled", "canceled"}:
        raise HTTPException(status_code=400, detail="Only failed or cancelled tasks can be retried.")
    if task_id in ACTIVE_PROCESSES:
        raise HTTPException(status_code=409, detail="Task is still stopping; retry in a moment.")

    task["status"] = "queued"
    task["percent"] = 0
    task["speed"] = ""
    task["step"] = "Queued..."
    task["error"] = ""
    task["cancel_requested"] = False
    task["created_at"] = time.time() * 1000
    task["last_updated"] = task["created_at"]
    task["queue_token"] = uuid.uuid4().hex
    task["retry_count"] = safe_int(task.get("retry_count"), 0) + 1

    await notify_task_update(task, force_save=True)
    await TASK_QUEUE.put((task_id, task["queue_token"]))
    return {"status": "queued", "task_id": task_id}


@app.delete(
    "/api/tasks/clear-completed"
)
async def api_clear_completed():

    removable = {
        "completed",
        "cancelled",
        "canceled",
        "error",
        "failed",
    }

    ids = [
        task_id
        for task_id, task in TASKS.items()
        if task.get("status") in removable
        and task_id not in ACTIVE_PROCESSES
    ]

    for task_id in ids:

        TASKS.pop(
            task_id,
            None,
        )

        LAST_SAVED_TIME.pop(
            task_id,
            None,
        )

    await asyncio.to_thread(
        db_clear_finished_sync
    )

    await manager.broadcast(
        {
            "type": "task_update",
            "action": "cleared",
            "count": len(ids),
        }
    )

    return {
        "status": "cleared",
        "count": len(ids),
    }


@app.delete(
    "/api/tasks/{task_id}"
)
async def api_delete_task(
    task_id: str,
):

    task = TASKS.get(task_id)

    if not task:
        raise HTTPException(
            status_code=404,
            detail="Task not found",
        )

    if task.get("status") in {
        "queued",
        "downloading",
        "processing",
    } or task_id in ACTIVE_PROCESSES:

        raise HTTPException(
            status_code=400,
            detail=(
                "Cancel the active task first."
            ),
        )

    TASKS.pop(
        task_id,
        None,
    )

    LAST_SAVED_TIME.pop(
        task_id,
        None,
    )

    await asyncio.to_thread(
        db_delete_task_sync,
        task_id,
    )

    return {
        "status": "deleted",
        "task_id": task_id,
    }


# ============================================================
# LIBRARY API
# ============================================================

@app.get("/api/library")
async def api_library():
    global LIBRARY_WARMUP_TASK
    if LIBRARY_CACHE is None:
        if LIBRARY_WARMUP_TASK is None or LIBRARY_WARMUP_TASK.done():
            LIBRARY_WARMUP_TASK = asyncio.create_task(background_library_warmup())
        return await fast_library_snapshot()

    files = await get_all_audio_files()

    def file_rows_sync(paths):
        rows = []
        for path in paths:
            try:
                rows.append((path, path.stat().st_size))
            except Exception:
                continue
        return rows

    result = []
    total = 0

    for path, size in await asyncio.to_thread(file_rows_sync, files):

        total += size

        result.append(
            {
                "name": str(
                    path.relative_to(
                        DOWNLOAD_DIR
                    )
                ),
                "size": format_size(size),
                "bytes": size,
            }
        )

    result.sort(
        key=lambda item:
            item["name"].lower()
    )

    library = await build_library()
    play_counts = await asyncio.to_thread(_play_count_map_sync)
    song_by_path = {str(song["path"]): song for song in library["songs"]}
    for item in result:
        path = str(DOWNLOAD_DIR / item["name"])
        song = song_by_path.get(path)
        if song:
            item["id"] = song["id"]
            item["title"] = song["title"]
            item["artist"] = song["artist"]
            item["album"] = song["album"]
            item["album_artist"] = song.get("albumArtist", song.get("artist", "Unknown Artist"))
            item["genre"] = song.get("genre", "")
            item["year"] = song.get("year", "")
            item["track"] = song.get("track", 0)
            item["duration"] = song.get("duration", 0)
            item["replaygain_track_gain"] = song.get("replaygain_track_gain")
            item["replaygain_album_gain"] = song.get("replaygain_album_gain")
            item["replaygain_track_peak"] = song.get("replaygain_track_peak")
            item["replaygain_album_peak"] = song.get("replaygain_album_peak")
            item["has_artwork"] = bool(song.get("has_artwork"))
            item["play_count"] = play_counts.get(song["id"], 0)
            item["cover"] = "/api/library/cover/" + urllib.parse.quote(item["name"], safe="/")
            item["stream"] = "/api/library/stream/" + urllib.parse.quote(item["name"], safe="/")
    artists = []
    for artist in library["artists"].values():
        song_ids = set(artist.get("songIds", []))
        album_ids = list(artist.get("albumIds", []))
        artists.append({
            "id": artist["id"],
            "name": artist["name"],
            "song_count": len(song_ids),
            "album_count": len(album_ids),
            "song_ids": sorted(song_ids),
            "album_ids": album_ids,
            "cover": f"/api/library/artist-artwork/{artist['id']}",
        })
    artists.sort(key=lambda item: item["name"].lower())

    albums = []
    song_map = {song["id"]: song for song in library["songs"]}
    for album in library["albums"].values():
        songs = [song_map[sid] for sid in album["songIds"] if sid in song_map]
        cover = "/api/library/cover/" + urllib.parse.quote(str(album["path"].relative_to(DOWNLOAD_DIR)), safe="/") if songs else ""
        albums.append({
            "id": album["id"],
            "name": album["name"],
            "artist": album["artist"],
            "artist_id": album["artistId"],
            "year": album.get("year", ""),
            "genre": album.get("genre", ""),
            "song_count": len(songs),
            "cover": cover,
            "song_ids": [song["id"] for song in songs],
        })
    albums.sort(key=lambda item: (item["artist"].lower(), item["name"].lower()))

    return {
        "files": result,
        "total_size": format_size(total),
        "total_bytes": total,
        "storage": await asyncio.to_thread(storage_info_sync),
        "artists": artists,
        "albums": albums,
        "ready": True,
    }


@app.post("/api/library/scan")
async def api_library_scan():
    # Keep the legacy endpoint on the same serialized scan path as quick/full scans
    # so a manual scan cannot race the scheduled scanner or another refresh.
    result = await api_library_scan_mode("quick")
    result["storage"] = await asyncio.to_thread(storage_info_sync)
    return result


@app.get("/api/library/statistics")
async def api_library_statistics():
    """Detailed, read-only library analytics. Kept separate from /api/stats so the
    existing fast stats endpoint remains lightweight."""
    library = await build_library()
    songs = library.get("songs", [])
    artists = library.get("artists", {})
    albums = library.get("albums", {})
    genres = library.get("genres", {})

    total_duration = sum(max(0, safe_int(song.get("duration"), 0)) for song in songs)
    total_bytes = sum(max(0, safe_int(song.get("size"), 0)) for song in songs)
    format_counts = {}
    bitrate_buckets = {"≤128 kbps": 0, "129–192 kbps": 0, "193–256 kbps": 0, "257–320 kbps": 0, ">320 kbps": 0}
    year_counts = {}
    sample_rates = {}
    channel_counts = {}

    for song in songs:
        ext = str(song.get("suffix") or "").lstrip(".").lower() or "unknown"
        format_counts[ext] = format_counts.get(ext, 0) + 1
        bitrate = safe_int(song.get("bit_rate"), 0)
        if bitrate <= 128:
            bitrate_buckets["≤128 kbps"] += 1
        elif bitrate <= 192:
            bitrate_buckets["129–192 kbps"] += 1
        elif bitrate <= 256:
            bitrate_buckets["193–256 kbps"] += 1
        elif bitrate <= 320:
            bitrate_buckets["257–320 kbps"] += 1
        else:
            bitrate_buckets[">320 kbps"] += 1
        year = str(song.get("year") or "").strip()
        if year and year[:4].isdigit():
            year_counts[year[:4]] = year_counts.get(year[:4], 0) + 1
        sr = safe_int(song.get("sample_rate"), 0)
        if sr:
            sample_rates[str(sr)] = sample_rates.get(str(sr), 0) + 1
        channels = safe_int(song.get("channels"), 0)
        if channels:
            channel_counts[str(channels)] = channel_counts.get(str(channels), 0) + 1

    (total_plays, unique_played, first_play, last_play, recent_plays,
     play_seconds, top_artists_rows, top_recent_rows) = await asyncio.to_thread(
        _library_statistics_db_sync, time.time() - 7 * 86400, time.time() - 30 * 86400
    )

    songs_by_id = {song["id"]: song for song in songs}
    top_artists = {}
    for row in top_artists_rows:
        song = songs_by_id.get(row[0])
        if not song:
            continue
        artist = song.get("artist") or "Unknown Artist"
        top_artists[artist] = top_artists.get(artist, 0) + int(row[1])
    top_artists_list = [{"name": name, "plays": plays} for name, plays in sorted(top_artists.items(), key=lambda x: (-x[1], x[0].lower()))[:10]]

    top_recent = []
    for row in top_recent_rows:
        song = songs_by_id.get(row[0])
        if not song:
            continue
        top_recent.append({
            "id": song["id"], "title": song.get("title", song.get("path", "")),
            "artist": song.get("artist", "Unknown Artist"), "album": song.get("album", "Unknown Album"),
            "plays": int(row[1]), "last_play": float(row[2] or 0)
        })

    avg_duration = (total_duration / len(songs)) if songs else 0
    avg_bitrate = (sum(safe_int(song.get("bit_rate"), 0) for song in songs) / len(songs)) if songs else 0
    unplayed_tracks = max(0, len(songs) - unique_played)
    return {
        "tracks": len(songs), "artists": len(artists), "albums": len(albums),
        "genres": len(genres), "total_bytes": total_bytes, "total_duration": total_duration,
        "average_duration": avg_duration, "average_bitrate": avg_bitrate, "unplayed_tracks": unplayed_tracks,
        "total_plays": total_plays, "unique_played": unique_played,
        "recent_7d_plays": recent_plays, "listened_seconds": play_seconds,
        "first_play": first_play, "last_play": last_play,
        "formats": [{"name": k, "count": v} for k, v in sorted(format_counts.items(), key=lambda x: (-x[1], x[0]))],
        "genres_breakdown": [{"name": k, "count": v} for k, v in sorted(genres.items(), key=lambda x: (-x[1], x[0].lower()))[:15]],
        "years": [{"name": k, "count": v} for k, v in sorted(year_counts.items(), key=lambda x: (-x[1], x[0]))[:15]],
        "bitrates": [{"name": k, "count": v} for k, v in bitrate_buckets.items() if v],
        "sample_rates": [{"name": k, "count": v} for k, v in sorted(sample_rates.items(), key=lambda x: (-x[1], x[0]))[:10]],
        "channels": [{"name": k, "count": v} for k, v in sorted(channel_counts.items(), key=lambda x: (-x[1], x[0]))[:10]],
        "top_artists": top_artists_list, "recent_favorites": top_recent,
    }


@app.get("/api/library/intelligence")
async def api_library_intelligence():
    """Actionable library quality checks: duplicates, missing tags/artwork, and ReplayGain coverage."""
    library = await build_library()
    songs = library.get("songs", [])
    duplicate_groups = defaultdict(list)
    missing_metadata = []
    missing_artwork = []
    replaygain_missing = []
    suspicious_names = []

    for song in songs:
        title = str(song.get("title") or "").strip()
        artist = str(song.get("artist") or "").strip()
        album = str(song.get("album") or "").strip()
        key = "|".join([re.sub(r"\s+", " ", artist).casefold(), re.sub(r"\s+", " ", title).casefold(), re.sub(r"\s+", " ", album).casefold()])
        duplicate_groups[key].append(song)
        issues = []
        if not title or title.casefold() in {"unknown track", "unknown"}: issues.append("title")
        if not artist or artist.casefold() in {"unknown artist", "unknown"}: issues.append("artist")
        if not album or album.casefold() in {"unknown album", "unknown"}: issues.append("album")
        if issues:
            missing_metadata.append({"id": song["id"], "title": title or song.get("path", Path("track")).stem, "artist": artist or "Unknown Artist", "album": album or "Unknown Album", "issues": issues})
        if not song.get("has_artwork"):
            missing_artwork.append({"id": song["id"], "title": title or song.get("path", Path("track")).stem, "artist": artist or "Unknown Artist", "album": album or "Unknown Album"})
        if song.get("replaygain_track_gain") is None and song.get("replaygain_album_gain") is None:
            replaygain_missing.append({"id": song["id"], "title": title or song.get("path", Path("track")).stem, "artist": artist or "Unknown Artist"})
        name = Path(str(song.get("path") or "")).name
        if re.search(r"(?:\[?\(?(?:official|lyric|lyrics|music video|video|visualizer)|\d{1,3}[-_. ])", name, re.I):
            suspicious_names.append({"id": song["id"], "name": name, "title": title, "artist": artist})

    duplicate_groups_out = []
    for key, group in duplicate_groups.items():
        if len(group) < 2:
            continue
        files = []
        for song in group[:20]:
            files.append({"id": song["id"], "name": str(song["path"].relative_to(DOWNLOAD_DIR)), "title": song["title"], "artist": song["artist"], "album": song["album"], "duration": song["duration"], "size": song["size"]})
        duplicate_groups_out.append({"key": key, "count": len(group), "files": files})
    duplicate_groups_out.sort(key=lambda x: (-x["count"], x["key"]))

    return {
        "track_count": len(songs),
        "duplicate_groups": duplicate_groups_out[:100],
        "duplicate_tracks": sum(max(0, x["count"] - 1) for x in duplicate_groups_out),
        "missing_metadata": missing_metadata[:200],
        "missing_metadata_count": len(missing_metadata),
        "missing_artwork": missing_artwork[:200],
        "missing_artwork_count": len(missing_artwork),
        "replaygain_missing": replaygain_missing[:200],
        "replaygain_missing_count": len(replaygain_missing),
        "suspicious_names": suspicious_names[:200],
        "suspicious_names_count": len(suspicious_names),
    }


@app.get("/api/daily-mix")
async def api_daily_mix(limit: int | None = None, variant: int = 0, refresh_token: str | None = None, exclude: str | None = None):
    """Local Spotify-style Daily Mix.

    It uses listening history, recency, repeat frequency, completion, stars, and
    artist/genre affinity. The daily seed keeps the mix coherent for a day while
    ``variant`` lets Refresh produce a different but still personalized mix.
    """
    settings = await load_settings_async()
    try:
        configured_limit = int(settings.get("daily_mix_track_count", 30) or 30)
    except (TypeError, ValueError):
        configured_limit = 30
    try:
        requested_limit = configured_limit if limit is None else int(limit)
    except (TypeError, ValueError):
        requested_limit = configured_limit
    # A limit supplied by the client is honored within safe bounds; the normal UI sends the configured value.
    limit = max(5, min(requested_limit, 50))
    try:
        variant_value = int(variant or 0)
    except (TypeError, ValueError):
        variant_value = 0
    variant = max(0, min(variant_value, 999999999))
    refresh_token = str(refresh_token or "").strip()[:160]
    excluded_ids = {item.strip() for item in str(exclude or "").split("|") if item.strip()}
    library = await build_library()
    songs = list(library.get("songs", []))
    if not songs:
        return {"date": time.strftime("%Y-%m-%d"), "title": "Daily Mix", "subtitle": "Your library is empty", "tracks": [], "reason": "empty"}

    now = time.time()
    day_key = time.strftime("%Y-%m-%d", time.localtime(now))
    seed_input = f"xrob-daily-mix:{day_key}:{variant}:{refresh_token or 'stable'}"
    rng = random.Random(int(hashlib.sha256(seed_input.encode()).hexdigest()[:16], 16))

    rows, recent_rows, star_rows = await asyncio.to_thread(_daily_mix_db_sync, now - 24 * 3600, now)

    history = {
        r[0]: {"plays": int(r[1]), "last": float(r[2] or 0), "heard": float(r[3] or 0), "duration_sum": float(r[4] or 0)}
        for r in rows
    }
    recent_ids = {r[0] for r in recent_rows}
    starred = {str(r[0]) for r in star_rows}

    artist_affinity, genre_affinity = {}, {}
    for song in songs:
        h = history.get(song["id"])
        if not h:
            continue
        days = max(0.0, (now - h["last"]) / 86400.0) if h["last"] else 3650.0
        recency = 1.0 / (1.0 + days / 14.0)
        completion = 0.0
        song_duration = max(1.0, safe_float(song.get("duration"), 0))
        if h["plays"] and h["heard"] > 0:
            completion = min(1.0, h["heard"] / max(song_duration * h["plays"], 1.0))
        weight = min(12.0, 1.0 + h["plays"] * 1.15) * (0.45 + 0.35 * recency + 0.20 * completion)
        if song["id"] in starred:
            weight += 4.0
        artist = str(song.get("artist") or "Unknown Artist").strip().casefold()
        genre = str(song.get("genre") or "").strip().casefold()
        artist_affinity[artist] = artist_affinity.get(artist, 0.0) + weight
        if genre:
            genre_affinity[genre] = genre_affinity.get(genre, 0.0) + weight

    any_history = bool(history)
    allow_excluded = len(songs) <= len(excluded_ids)
    candidates = []
    for song in songs:
        sid = song["id"]
        if sid in excluded_ids and not allow_excluded:
            continue
        h = history.get(sid, {"plays": 0, "last": 0, "heard": 0})
        plays = int(h.get("plays", 0))
        last = float(h.get("last", 0) or 0)
        days_since = (now - last) / 86400.0 if last else 3650.0
        recency = 1.0 / (1.0 + max(0.0, days_since) / 18.0)
        artist = str(song.get("artist") or "Unknown Artist").strip().casefold()
        genre = str(song.get("genre") or "").strip().casefold()
        artist_score = min(18.0, artist_affinity.get(artist, 0.0) * 0.72)
        genre_score = min(14.0, genre_affinity.get(genre, 0.0) * 0.30) if genre else 0.0
        familiarity = min(22.0, plays * 3.4) * (0.45 + 0.55 * recency)
        discovery = (4.0 if plays == 0 else 0.0) + min(5.0, max(0.0, days_since) / 20.0)
        freshness = 4.5 * recency
        star_bonus = 6.0 if sid in starred else 0.0
        recent_penalty = 18.0 if sid in recent_ids else 0.0
        # Unheard tracks matter, but known artists/genres still influence discovery.
        score = familiarity + artist_score + genre_score + discovery + freshness + star_bonus - recent_penalty
        score += rng.random() * (3.0 if not any_history else 1.35)
        candidates.append({"song": song, "score": score})

    candidates.sort(key=lambda x: x["score"], reverse=True)
    pool = candidates[:]
    selected, used = [], set()
    artist_counts, album_counts, genre_counts = {}, {}, {}

    while pool and len(selected) < min(limit, len(songs)):
        best_i, best_score = 0, float("-inf")
        for i, item in enumerate(pool[:160]):
            song = item["song"]
            artist = str(song.get("artist") or "Unknown Artist").strip().casefold()
            album = str(song.get("album") or "Unknown Album").strip().casefold()
            genre = str(song.get("genre") or "").strip().casefold()
            diversity = (
                -artist_counts.get(artist, 0) * 4.5
                -album_counts.get(album, 0) * 1.8
                -genre_counts.get(genre, 0) * 0.65
            )
            # Add controlled exploration so a strong artist does not dominate the mix.
            exploration = rng.random() * 1.6
            adjusted = item["score"] + diversity + exploration
            if adjusted > best_score:
                best_i, best_score = i, adjusted
        item = pool.pop(best_i)
        song = item["song"]
        sid = song["id"]
        if sid in used:
            continue
        used.add(sid)
        selected.append(song)
        artist = str(song.get("artist") or "Unknown Artist").strip().casefold()
        album = str(song.get("album") or "Unknown Album").strip().casefold()
        genre = str(song.get("genre") or "").strip().casefold()
        artist_counts[artist] = artist_counts.get(artist, 0) + 1
        album_counts[album] = album_counts.get(album, 0) + 1
        if genre:
            genre_counts[genre] = genre_counts.get(genre, 0) + 1

    def pack(song):
        enc = urllib.parse.quote(str(song["path"].relative_to(DOWNLOAD_DIR)), safe="/")
        return {
            "id": song["id"], "title": song.get("title", song["path"].stem),
            "artist": song.get("artist", "Unknown Artist"), "album": song.get("album", "Unknown Album"),
            "genre": song.get("genre", ""), "duration": song.get("duration", 0),
            "cover": f"/api/library/cover/{enc}", "stream": f"/api/library/stream/{enc}",
            "play_count": history.get(song["id"], {}).get("plays", 0),
        }

    listened = sum(1 for song in songs if song["id"] in history)
    if refresh_token:
        subtitle = f"Fresh Daily Mix · {len(selected)} new tracks"
        reason = "refreshed"
    elif listened == 0:
        subtitle = f"A starter mix from your library · {len(selected)} tracks"
        reason = "starter"
    else:
        subtitle = f"Based on what you play · {len(selected)} tracks"
        if recent_ids:
            subtitle += " · refreshed for today"
        reason = "personalized"
    response = {"date": day_key, "title": "Daily Mix", "subtitle": subtitle, "tracks": [pack(s) for s in selected], "reason": reason, "variant": variant}
    if refresh_token:
        response["generation"] = refresh_token
        response["cacheControl"] = "no-store"
        return JSONResponse(response, headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"})
    return response


@app.get("/api/stats")
async def api_stats():
    if LIBRARY_CACHE is None:
        snap = await fast_library_snapshot()
        all_play_count, _ = await asyncio.to_thread(_play_totals_sync)
        return {"tracks": len(snap["files"]), "artists": snap.get("artists_count", 0), "albums": snap.get("albums_count", 0), "total_bytes": snap["total_bytes"], "folder_size": snap["total_size"], "all_play_count": all_play_count, "played_tracks": 0, "ready": False}

    library = await build_library()

    songs = library["songs"]

    artists = library["artists"]
    albums = library["albums"]

    total = sum(song["size"] for song in songs)
    all_play_count, distinct_played = await asyncio.to_thread(_play_totals_sync)
    return {
        "tracks": len(songs),
        "artists": len(artists),
        "albums": len(albums),
        "total_bytes": total,
        "folder_size": format_size(total),
        "all_play_count": all_play_count,
        "played_tracks": distinct_played,
    }


@app.get("/api/home")
async def api_home():

    # Home must stay fast. Do not rebuild the entire metadata library
    # here because the frontend also requests /api/stats separately.
    files = await get_all_audio_files()
    files = await asyncio.to_thread(
        lambda paths: sorted(
            paths,
            key=lambda path: path.stat().st_mtime if path.exists() else 0,
            reverse=True,
        ),
        files,
    )

    recent_files = files[:12]

    async def make_recent(path):
        try:
            metadata = await read_metadata(path)
            relative_path = str(
                path.relative_to(DOWNLOAD_DIR)
            )

            encoded = urllib.parse.quote(
                relative_path,
                safe="/",
            )

            return {
                "id": make_song_id(path),
                "title": metadata.get(
                    "title",
                    path.stem,
                ),
                "artist": metadata.get(
                    "artist",
                    "Unknown Artist",
                ),
                "album": metadata.get(
                    "album",
                    path.stem,
                ),
                "duration": safe_int(
                    metadata.get(
                        "duration",
                        0,
                    ),
                    0,
                ),
                "cover": (
                    "/api/library/cover/"
                    + encoded
                ),
                "stream": (
                    "/api/library/stream/"
                    + encoded
                ),
            }

        except Exception as exc:
            print(
                "Home track metadata error:",
                exc,
            )
            return None

    recent_results = await asyncio.gather(
        *(make_recent(path) for path in recent_files),
        return_exceptions=False,
    )

    recent = [
        item
        for item in recent_results
        if item is not None
    ]

    active = sum(
        1
        for task in TASKS.values()
        if task.get("status") in {
            "queued",
            "downloading",
            "processing",
        }
    )

    library = await build_library()
    total_bytes = sum(song.get("size", 0) for song in library["songs"])
    all_play_count, _ = await asyncio.to_thread(_play_totals_sync)
    return {
        "stats": {
            "tracks": len(library["songs"]),
            "artists": len(library["artists"]),
            "albums": len(library["albums"]),
            "total_bytes": total_bytes,
            "folder_size": format_size(total_bytes),
            "all_play_count": all_play_count,
        },
        "active_downloads": active,
        "recently_added": recent,
    }


@app.get(
    "/api/library/cover/{filename:path}"
)
async def api_library_cover(
    filename: str,
):

    path = await resolve_file(filename)

    cover = await ensure_cover(path)

    if cover:
        return FileResponse(
            cover,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400, immutable"},
        )

    return Response(
        content=(
            "<svg xmlns='http://www.w3.org/2000/svg' "
            "width='300' height='300'>"
            "<rect width='100%' height='100%' "
            "fill='#202020'/>"
            "<text x='50%' y='50%' "
            "fill='#aaa' font-size='55' "
            "text-anchor='middle' "
            "dominant-baseline='central'>"
            "🎵"
            "</text></svg>"
        ),
        media_type="image/svg+xml",
    )


@app.get(
    "/api/library/stream/{filename:path}"
)
async def api_library_stream(
    filename: str,
):

    path = await resolve_file(filename)

    return FileResponse(
        path,
        media_type=MEDIA_TYPES.get(
            path.suffix.lower(),
            "audio/mpeg",
        ),
    )


@app.delete(
    "/api/library/{filename:path}"
)
async def api_delete_library(
    filename: str,
):

    path = await resolve_file(filename)

    try:
        def delete_file_sync(target):
            cover = cover_cache_path(target)
            target.unlink()
            try:
                if cover.exists():
                    cover.unlink()
            except OSError:
                pass

        await asyncio.to_thread(delete_file_sync, path)
        METADATA_CACHE.pop(str(path), None)
        invalidate_library_cache()

        return {
            "status": "deleted",
            "filename": filename,
        }

    except Exception as error:

        raise HTTPException(
            status_code=500,
            detail=str(error),
        )


# ============================================================
# SUBSONIC AUTHENTICATION
# ============================================================

def subsonic_credentials():

    settings = load_settings()

    return (
        str(
            settings.get(
                "subsonic_user",
                "admin",
            )
        ),
        str(
            settings.get(
                "subsonic_password",
                "",
            )
        ),
    )


def validate_subsonic_auth(
    request: Request,
):

    username, password = (
        subsonic_credentials()
    )

    supplied_user = request.query_params.get(
        "u",
        "",
    )

    supplied_password = request.query_params.get(
        "p",
        "",
    )

    token = request.query_params.get(
        "t",
        "",
    )

    salt = request.query_params.get(
        "s",
        "",
    )

    if supplied_user != username:
        return False

    # t = MD5(password + salt)
    if token and salt:

        expected = hashlib.md5(
            (
                password + salt
            ).encode("utf-8")
        ).hexdigest()

        if token.lower() == expected.lower():
            return True

    if supplied_password == password:
        return True

    if supplied_password.lower().startswith("enc:"):
        try:
            decoded = binascii.unhexlify(supplied_password[4:]).decode("utf-8")
            if decoded == password:
                return True
        except (binascii.Error, UnicodeDecodeError):
            pass

    # md5(password) compatibility
    if (
        supplied_password
        and len(supplied_password) == 32
    ):

        expected = hashlib.md5(
            password.encode("utf-8")
        ).hexdigest()

        if supplied_password.lower() == expected.lower():
            return True

    return False


def require_auth(request):

    if not validate_subsonic_auth(
        request
    ):

        return subsonic_error(
            request,
            40,
            "Wrong username or password.",
        )

    return None


# ============================================================
# SUBSONIC RESPONSE SERIALIZER
# ============================================================

def scalar(value):

    if isinstance(value, bool):
        return (
            "true"
            if value
            else "false"
        )

    if value is None:
        return ""

    return str(value)


def append_xml_dict(
    parent,
    data,
):

    for key, value in data.items():

        if isinstance(
            value,
            dict,
        ):

            child = ET.SubElement(
                parent,
                key,
            )

            append_xml_dict(
                child,
                value,
            )

        elif isinstance(
            value,
            list,
        ):

            for item in value:

                child = ET.SubElement(
                    parent,
                    key,
                )

                if isinstance(
                    item,
                    dict,
                ):

                    append_xml_dict(
                        child,
                        item,
                    )

                else:

                    child.text = scalar(item)

        else:

            parent.set(
                key,
                scalar(value),
            )


def append_xml_value(
    parent,
    key,
    value,
):

    if isinstance(
        value,
        dict,
    ):

        child = ET.SubElement(
            parent,
            key,
        )

        append_xml_dict(
            child,
            value,
        )

        return

    if isinstance(
        value,
        list,
    ):

        for item in value:

            child = ET.SubElement(
                parent,
                key,
            )

            if isinstance(
                item,
                dict,
            ):

                append_xml_dict(
                    child,
                    item,
                )

            else:

                child.text = scalar(item)

        return

    child = ET.SubElement(
        parent,
        key,
    )

    child.text = scalar(value)


def make_subsonic_response(
    payload,
    request,
):

    fmt = (
        request.query_params.get(
            "f",
            "xml",
        )
        or "xml"
    ).lower()

    if fmt == "json":

        return JSONResponse(
            content={
                "subsonic-response":
                    payload
            }
        )

    root_attributes = {}

    for key in (
        "status",
        "version",
        "serverVersion",
        "openSubsonic",
        "type",
    ):

        if key in payload:

            root_attributes[key] = scalar(
                payload[key]
            )

    if fmt == "xml":
        root_attributes.setdefault(
            "xmlns",
            "http://subsonic.org/restapi",
        )

    root = ET.Element(
        "subsonic-response",
        root_attributes,
    )

    extensions = payload.get(
        "openSubsonicExtensions"
    )

    if isinstance(
        extensions,
        list,
    ):

        wrapper = ET.SubElement(
            root,
            "openSubsonicExtensions",
        )

        for extension in extensions:

            if not isinstance(
                extension,
                dict,
            ):
                continue

            item = ET.SubElement(
                wrapper,
                "extension",
            )

            if "name" in extension:
                item.set(
                    "name",
                    scalar(
                        extension["name"]
                    ),
                )

            versions = extension.get(
                "versions",
                [],
            )

            if isinstance(
                versions,
                list,
            ):

                item.set(
                    "versions",
                    ",".join(
                        scalar(version)
                        for version in versions
                    ),
                )

            else:

                item.set(
                    "versions",
                    scalar(versions),
                )

    for key, value in payload.items():

        if key in {
            "status",
            "version",
            "serverVersion",
            "openSubsonic",
            "type",
            "openSubsonicExtensions",
        }:
            continue

        append_xml_value(
            root,
            key,
            value,
        )

    body = ET.tostring(
        root,
        encoding="utf-8",
        xml_declaration=True,
    )

    return Response(
        content=body,
        media_type="application/xml",
    )


def subsonic_error(
    request,
    code,
    message,
):

    return make_subsonic_response(
        {
            "status": "failed",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "type": "Xrob Music",
            "error": {
                "code": str(code),
                "message": message,
            },
        },
        request,
    )


# ============================================================
# STARRED
# ============================================================

def get_starred_at_sync(item_id):
    with db_connect() as conn:
        row = conn.execute(
            "SELECT starred_at FROM stars WHERE item_id = ?",
            (item_id,),
        ).fetchone()
    return float(row[0]) if row else None


def get_starred_map_sync():
    with db_connect() as conn:
        rows = conn.execute("SELECT item_id, starred_at FROM stars").fetchall()
    return {str(row[0]): float(row[1]) for row in rows if row[0]}


async def songs_to_subsonic_async(songs):
    songs = list(songs or [])
    if not songs:
        return []
    starred = await asyncio.to_thread(get_starred_map_sync)
    return [song_to_subsonic(song, starred.get(song.get("id"))) for song in songs]


def set_star_sync(
    item_id,
    enabled,
):

    with db_connect() as conn:

        if enabled:

            conn.execute(
                """
                INSERT OR REPLACE INTO stars
                (
                    item_id,
                    starred_at
                )
                VALUES (?, ?)
                """,
                (
                    item_id,
                    time.time(),
                ),
            )

        else:

            conn.execute(
                """
                DELETE FROM stars
                WHERE item_id = ?
                """,
                (item_id,),
            )

        conn.commit()


# ============================================================
# SUBSONIC OBJECTS
# ============================================================

_STARRED_UNSET = object()

def song_to_subsonic(song, starred_at=_STARRED_UNSET):

    track_value = safe_int(
        song.get("track"),
        0,
    )

    result = {
        "id": song["id"],
        "parent": song["albumId"],
        "isDir": False,
        "title": song["title"],
        "album": song["album"],
        "artist": song["artist"],
        "artistId": song["artistId"],
        "albumId": song["albumId"],
        "albumArtist": song["albumArtist"],
        "year": safe_int(
            song.get("year"),
            0,
        ),
        "genre": song.get(
            "genre",
            "",
        ),
        "coverArt": song["id"],
        "size": song["size"],
        "contentType": MEDIA_TYPES.get(
            song["suffix"],
            "audio/mpeg",
        ),
        "suffix": song["suffix"].lstrip(
            "."
        ),
        "duration": safe_int(
            song.get("duration"),
            0,
        ),
        "bitRate": safe_int(
            song.get("bit_rate"),
            0,
        ),
        "bitDepth": safe_int(
            song.get("bit_depth"),
            0,
        ),
        "samplingRate": safe_int(
            song.get("sample_rate"),
            0,
        ),
        "channelCount": safe_int(
            song.get("channels"),
            0,
        ),
        "path": str(
            song["path"].relative_to(
                DOWNLOAD_DIR
            )
        ),
        "type": "music",
        "mediaType": "song",
        "isVideo": False,
        "playCount": 0,
        "comment": "",
        "sortName": song["title"],
        "musicBrainzId": "",
        "isrc": [],
        "moods": [],
        "explicitStatus": "",
    }

    if song.get("genre"):
        result["genres"] = [
            {
                "name": song["genre"]
            }
        ]
    else:
        result["genres"] = []

    # OpenSubsonic defines starred/played as ISO-8601 date strings,
    # not booleans. Only query the database for single-song calls; list routes
    # pass a bulk-loaded starred map to avoid one SQLite query per song.
    if starred_at is _STARRED_UNSET:
        starred_at = get_starred_at_sync(song["id"])
    if starred_at is not None:
        result["starred"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(starred_at),
        )

    # Creation time is also an ISO-8601 string when supplied.
    created_at = safe_float(song.get("created"), 0)
    if created_at > 0:
        result["created"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(created_at),
        )

    result["artists"] = [
        {
            "id": song["artistId"],
            "name": song["artist"],
        }
    ]

    result["albumArtists"] = [
        {
            "id": song["albumArtistId"],
            "name": song["albumArtist"],
        }
    ]

    result["displayArtist"] = song["artist"]
    result["displayAlbumArtist"] = song["albumArtist"]

    if track_value > 0:
        result["track"] = track_value

    disc_value = safe_int(
        song.get("disc"),
        0,
    )
    if disc_value > 0:
        result["discNumber"] = disc_value

    return result


def album_to_subsonic(
    album,
    songs=None,
):

    songs = songs or []

    duration = sum(
        safe_int(
            song.get(
                "duration",
                0,
            )
        )
        for song in songs
    )

    genre = next(
        (
            song.get(
                "genre",
                "",
            )
            for song in songs
            if song.get(
                "genre",
                "",
            )
        ),
        album.get(
            "genre",
            "",
        ),
    )

    latest = max(
        (safe_float(song.get("modified", 0), 0) for song in songs),
        default=0,
    )
    created = min(
        (safe_float(song.get("created", 0), 0) for song in songs),
        default=latest,
    )

    return {
        "id": album["id"],
        "parent": album["artistId"],
        "isDir": True,
        "title": album["name"],
        "name": album["name"],
        "album": album["name"],
        "artist": album["artist"],
        "albumArtist": album["artist"],
        "artistId": album["artistId"],
        "year": safe_int(
            album.get(
                "year"
            ),
            0,
        ),
        "genre": genre,
        "coverArt": album["id"],
        "songCount": len(songs),
        "duration": duration,
        "playCount": 0,
        "isVideo": False,
        "created": (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created))
            if created > 0 else ""
        ),
        "changed": (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(latest))
            if latest > 0 else ""
        ),
    }


def artist_to_subsonic(
    artist,
):

    return {
        "id": artist["id"],
        "name": artist["name"],
        "albumCount": len(artist["albumIds"]),
        "songCount": len(artist.get("songIds", [])),
        "coverArt": artist["id"],
    }


# ============================================================
# PING
# ============================================================

@app.get("/rest/ping.view")
@app.get("/rest/ping")
async def rest_ping(
    request: Request,
):

    return make_subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "type": "Xrob Music",
        },
        request,
    )


# ============================================================
# OPENSUBSONIC EXTENSIONS
# ============================================================

@app.get(
    "/rest/getOpenSubsonicExtensions.view"
)
@app.get(
    "/rest/getOpenSubsonicExtensions"
)
async def rest_extensions(
    request: Request,
):

    return make_subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "type": "Xrob Music",
            "openSubsonicExtensions": [],
        },
        request,
    )


# ============================================================
# LICENSE
# ============================================================

@app.get("/rest/getLicense.view")
@app.get("/rest/getLicense")
async def rest_license(
    request: Request,
):

    error = require_auth(request)

    if error:
        return error

    return make_subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "type": "Xrob Music",
            "license": {
                "valid": True,
            },
        },
        request,
    )


# ============================================================
# MUSIC FOLDERS
# ============================================================

@app.get(
    "/rest/getMusicFolders.view"
)
@app.get(
    "/rest/getMusicFolders"
)
async def rest_music_folders(
    request: Request,
):

    error = require_auth(request)

    if error:
        return error

    return make_subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "type": "Xrob Music",
            "musicFolders": {
                "musicFolder": [
                    {
                        "id": 1,
                        "name": "Music",
                    }
                ]
            },
        },
        request,
    )


# ============================================================
# GET USER
# ============================================================

@app.get("/rest/getUser.view")
@app.get("/rest/getUser")
async def rest_get_user(
    request: Request,
    username: Optional[str] = Query(
        None
    ),
):

    error = require_auth(request)

    if error:
        return error

    configured_user, _ = (
        subsonic_credentials()
    )

    requested_user = (
        username
        or configured_user
    )

    if requested_user != configured_user:

        return subsonic_error(
            request,
            50,
            "User not found.",
        )

    return make_subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "type": "Xrob Music",
            "user": {
                "username": configured_user,
                "email": "",
                "scrobblingEnabled": True,
                "adminRole": True,
                "settingsRole": True,
                "downloadRole": True,
                "uploadRole": False,
                "playlistRole": True,
                "coverArtRole": True,
                "commentRole": True,
                "podcastRole": False,
                "shareRole": False,
                "jukeboxRole": False,
                "streamRole": True,
                "videoConversionRole": False,
                "musicFolderId": [1],
                "maxBitRate": 0,
            },
        },
        request,
    )


# ============================================================
# GET SCAN STATUS
# FIX FOR ARPEGGI
# ============================================================

@app.get(
    "/rest/getScanStatus.view"
)
@app.get(
    "/rest/getScanStatus"
)
async def rest_get_scan_status(
    request: Request,
):

    error = require_auth(request)

    if error:
        return error

    # Xrob Music is filesystem based.
    # There is no separate long-running scanner.
    #
    # We therefore report the library as not currently
    # scanning while providing a useful current count.
    try:
        library = await build_library(force=False)
        count = len(library.get("songs", []))
    except Exception:
        count = 0

    return make_subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "type": "Xrob Music",
            "scanStatus": {
                "scanning": False,
                "count": count,
            },
        },
        request,
    )


# ============================================================
# START SCAN
# ============================================================

@app.get(
    "/rest/startScan.view"
)
@app.get(
    "/rest/startScan"
)
async def rest_start_scan(
    request: Request,
):

    error = require_auth(request)

    if error:
        return error

    # Trigger a lightweight filesystem rebuild.
    try:
        library = await build_library(force=False)
        count = len(library.get("songs", []))
        if not library.get("songs") and await asyncio.to_thread(get_audio_files_sync):
            library = await build_library(force=True)
            count = len(library.get("songs", []))
    except Exception:
        count = 0

    return make_subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "type": "Xrob Music",
            "scanStatus": {
                "scanning": False,
                "count": count,
            },
        },
        request,
    )


# ============================================================
# BUILD ARTIST INDEXES
# ============================================================

async def build_artist_indexes():

    library = await build_library()

    grouped = {}

    for artist in library[
        "artists"
    ].values():

        name = (
            artist.get(
                "name",
                "",
            )
            .strip()
        )

        if not name:
            continue

        letter = (
            name[:1].upper()
            if name
            else "#"
        )

        grouped.setdefault(
            letter,
            [],
        ).append(
            artist
        )

    result = []

    for letter in sorted(
        grouped.keys()
    ):

        artists = grouped[
            letter
        ]

        artists.sort(
            key=lambda item:
                item["name"].lower()
        )

        result.append(
            {
                "name": letter,
                "artist": [
                    artist_to_subsonic(
                        artist
                    )
                    for artist in artists
                ],
            }
        )

    return result


# ============================================================
# GET ARTISTS
# ============================================================

@app.get("/rest/getArtists.view")
@app.get("/rest/getArtists")
async def rest_artists(
    request: Request,
    musicFolderId: Optional[str] = Query(None),
):

    error = require_auth(request)

    if error:
        return error

    if musicFolderId not in (None, "", "1", 1):
        return subsonic_error(request, 70, "Music folder not found.")

    indexes = await build_artist_indexes()

    return make_subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "type": "Xrob Music",
            "artists": {
                "ignoredArticles": "",
                "index": indexes,
                "lastModified": int(
                    max(
                        (
                            safe_float(song.get("modified", 0), 0)
                            for song in (await build_library())["songs"]
                        ),
                        default=time.time(),
                    )
                    * 1000
                ),
            },
        },
        request,
    )


# ============================================================
# GET INDEXES
# ============================================================

@app.get("/rest/getIndexes.view")
@app.get("/rest/getIndexes")
async def rest_indexes(
    request: Request,
    musicFolderId: Optional[str] = Query(
        None
    ),
    ifModifiedSince: Optional[int] = Query(
        None
    ),
):

    error = require_auth(request)

    if error:
        return error

    if musicFolderId not in (None, "", "1", 1):
        return subsonic_error(request, 70, "Music folder not found.")

    library = await build_library()

    grouped = {}

    for artist in library[
        "artists"
    ].values():

        name = (
            artist.get(
                "name",
                "",
            )
            .strip()
        )

        if not name:
            continue

        letter = (
            name[:1].upper()
            if name
            else "#"
        )

        grouped.setdefault(
            letter,
            [],
        ).append(
            {
                "id": artist["id"],
                "name": name,
                "albumCount": len(
                    artist.get(
                        "albumIds",
                        [],
                    )
                ),
            }
        )

    index_list = []

    for letter in sorted(
        grouped.keys()
    ):

        artist_list = grouped[
            letter
        ]

        artist_list.sort(
            key=lambda item:
                item["name"].lower()
        )

        index_list.append(
            {
                "name": letter,
                "artist": artist_list,
            }
        )

    latest_modified = max(
        (
            safe_float(
                song.get(
                    "modified",
                    0,
                ),
                0,
            )
            for song in library[
                "songs"
            ]
        ),
        default=time.time(),
    )

    # JSON number, not string.
    last_modified = int(
        latest_modified * 1000
    )

    return make_subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "type": "Xrob Music",
            "indexes": {
                "shortcut": [],
                "index": index_list,
                "child": [],
                "lastModified": last_modified,
                "ignoredArticles": "",
            },
        },
        request,
    )


# ============================================================
# GET ARTIST
# ============================================================

@app.get("/rest/getArtist.view")
@app.get("/rest/getArtist")
async def rest_artist(
    request: Request,
    id: str = Query(...),
):

    error = require_auth(request)

    if error:
        return error

    artist = await find_artist(id)

    if not artist:

        return subsonic_error(
            request,
            70,
            "Artist not found.",
        )

    library = await build_library()
    song_map = {song["id"]: song for song in library["songs"]}

    albums = []

    for album_id in artist[
        "albumIds"
    ]:

        album = library[
            "albums"
        ].get(
            album_id
        )

        if not album:
            continue

        songs = [
            song_map[sid]
            for sid in album["songIds"]
            if sid in song_map
        ]

        albums.append(
            album_to_subsonic(
                album,
                songs,
            )
        )

    albums.sort(
        key=lambda item:
            item["name"].lower()
    )

    return make_subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "type": "Xrob Music",
            "artist": {
                **artist_to_subsonic(
                    artist
                ),
                "album": albums,
            },
        },
        request,
    )


# ============================================================
# GET ALBUM
# ============================================================

@app.get("/rest/getAlbum.view")
@app.get("/rest/getAlbum")
async def rest_album(
    request: Request,
    id: str = Query(...),
):

    error = require_auth(request)

    if error:
        return error

    library = await build_library()
    song_map = {song["id"]: song for song in library["songs"]}

    album = library[
        "albums"
    ].get(
        id
    )

    if not album:

        return subsonic_error(
            request,
            70,
            "Album not found.",
        )

    songs = [
        song_map[sid]
        for sid in album["songIds"]
        if sid in song_map
    ]

    return make_subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "type": "Xrob Music",
            "album": {
                **album_to_subsonic(
                    album,
                    songs,
                ),
                "song": await songs_to_subsonic_async(songs),
            },
        },
        request,
    )


# ============================================================
# GET ALBUM LIST 2
# ============================================================

@app.get("/rest/getAlbumList2.view")
@app.get("/rest/getAlbumList2")
async def rest_album_list2(
    request: Request,
    type: str = Query(
        "alphabeticalByName"
    ),
    size: int = Query(50),
    offset: int = Query(0),
    fromYear: Optional[int] = Query(
        None
    ),
    toYear: Optional[int] = Query(
        None
    ),
    genre: Optional[str] = Query(
        None
    ),
    musicFolderId: Optional[str] = Query(
        None
    ),
):

    error = require_auth(request)

    if error:
        return error

    library = await build_library()
    song_map = {song["id"]: song for song in library["songs"]}

    album_data = []

    for album in library[
        "albums"
    ].values():

        songs = [
            song_map[sid]
            for sid in album["songIds"]
            if sid in song_map
        ]

        item = album_to_subsonic(
            album,
            songs,
        )

        item["_latest"] = max(
            (
                safe_float(
                    song.get(
                        "modified",
                        0,
                    ),
                    0,
                )
                for song in songs
            ),
            default=0,
        )

        item["_play_count"] = sum(
            safe_int(
                song.get(
                    "playCount",
                    0,
                ),
                0,
            )
            for song in songs
        )

        album_data.append(
            item
        )

    # Filters
    if fromYear is not None:

        album_data = [
            album
            for album in album_data
            if safe_int(
                album.get(
                    "year",
                    0,
                ),
                0,
            ) >= fromYear
        ]

    if toYear is not None:

        album_data = [
            album
            for album in album_data
            if safe_int(
                album.get(
                    "year",
                    0,
                ),
                0,
            ) <= toYear
        ]

    if genre:

        target_genre = genre.lower()

        album_data = [
            album
            for album in album_data
            if album.get(
                "genre",
                "",
            ).lower()
            == target_genre
        ]

    album_type = (
        type or "alphabeticalByName"
    )
    if album_type not in {
        "random", "newest", "frequent", "recent",
        "starred", "alphabeticalByName",
        "alphabeticalByArtist", "byYear", "byGenre",
    }:
        album_type = "alphabeticalByName"

    if musicFolderId not in (None, "", "1", 1):
        return subsonic_error(request, 70, "Music folder not found.")

    # Arpeggi asks for these.
    if album_type == "frequent":

        album_data.sort(
            key=lambda item: (
                item.get(
                    "_play_count",
                    0,
                ),
                item.get(
                    "_latest",
                    0,
                ),
            ),
            reverse=True,
        )

    elif album_type == "random":

        random.shuffle(
            album_data
        )

    elif album_type == "newest":

        album_data.sort(
            key=lambda item:
                item.get(
                    "_latest",
                    0,
                ),
            reverse=True,
        )

    elif album_type == "recent":

        album_data.sort(
            key=lambda item:
                item.get(
                    "_latest",
                    0,
                ),
            reverse=True,
        )

    elif album_type == "byYear":

        album_data.sort(
            key=lambda item: (
                safe_int(
                    item.get(
                        "year",
                        0,
                    ),
                    0,
                ),
                item.get(
                    "name",
                    "",
                ).lower(),
            ),
            reverse=True,
        )

    elif album_type == "alphabeticalByArtist":

        album_data.sort(
            key=lambda item: (
                item.get(
                    "artist",
                    "",
                ).lower(),

                item.get(
                    "name",
                    "",
                ).lower(),
            )
        )

    else:

        album_data.sort(
            key=lambda item:
                item.get(
                    "name",
                    "",
                ).lower()
        )

    start = _bounded_subsonic_int(offset, 0, 100000)
    end = start + max(1, _bounded_subsonic_int(size, 50, 500))

    result = []

    for album in album_data[
        start:end
    ]:

        album.pop(
            "_latest",
            None,
        )

        album.pop(
            "_play_count",
            None,
        )

        result.append(
            album
        )

    return make_subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "type": "Xrob Music",
            "albumList2": {
                "album": result,
            },
        },
        request,
    )


@app.get("/rest/getAlbumList.view")
@app.get("/rest/getAlbumList")
async def rest_album_list(
    request: Request,
    type: str = Query(
        "alphabeticalByName"
    ),
    size: int = Query(50),
    offset: int = Query(0),
    fromYear: Optional[int] = Query(
        None
    ),
    toYear: Optional[int] = Query(
        None
    ),
):

    return await rest_album_list2(
        request=request,
        type=type,
        size=size,
        offset=offset,
        fromYear=fromYear,
        toYear=toYear,
        genre=None,
        musicFolderId=None,
    )


# ============================================================
# GET MUSIC DIRECTORY
# ============================================================

@app.get(
    "/rest/getMusicDirectory.view"
)
@app.get(
    "/rest/getMusicDirectory"
)
async def rest_music_directory(
    request: Request,
    id: str = Query(...),
):

    error = require_auth(request)

    if error:
        return error

    library = await build_library()
    song_map = {song["id"]: song for song in library["songs"]}

    children = []

    if id == "1":

        for artist in sorted(
            library[
                "artists"
            ].values(),
            key=lambda item:
                item["name"].lower(),
        ):

            children.append(
                {
                    "id": artist["id"],
                    "parent": "1",
                    "isDir": True,
                    "title": artist["name"],
                    "name": artist["name"],
                    "type": "artist",
                    "coverArt": artist["id"],
                }
            )

    elif id.startswith("artist-"):

        artist = library[
            "artists"
        ].get(
            id
        )

        if artist:

            for album_id in sorted(
                artist["albumIds"]
            ):

                album = library[
                    "albums"
                ].get(
                    album_id
                )

                if not album:
                    continue

                songs = [
                    song_map[sid]
                    for sid in album["songIds"]
                    if sid in song_map
                ]

                item = album_to_subsonic(
                    album,
                    songs,
                )

                item["type"] = "album"

                children.append(
                    item
                )

    elif id.startswith("album-"):

        album = library[
            "albums"
        ].get(
            id
        )

        if album:

            songs = [
                song_map[sid]
                for sid in album["songIds"]
                if sid in song_map
            ]

            children.extend(await songs_to_subsonic_async(songs))

    if not (
        id == "1"
        or id.startswith("artist-")
        or id.startswith("album-")
    ):
        return subsonic_error(
            request,
            70,
            "Music directory not found.",
        )

    return make_subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "type": "Xrob Music",
            "directory": {
                "id": id,
                "name": "Music",
                "child": children,
            },
        },
        request,
    )


# ============================================================
# GET SONG
# ============================================================

@app.get("/rest/getSong.view")
@app.get("/rest/getSong")
async def rest_song(
    request: Request,
    id: str = Query(...),
):

    error = require_auth(request)

    if error:
        return error

    song = await find_song(id)

    if not song:

        return subsonic_error(
            request,
            70,
            "Song not found.",
        )

    starred_at = await asyncio.to_thread(get_starred_at_sync, song["id"])
    return make_subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "type": "Xrob Music",
            "song": song_to_subsonic(song, starred_at),
        },
        request,
    )


# ============================================================
# SEARCH
# ============================================================

def _bounded_subsonic_int(value, default=0, maximum=500):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(maximum, number))


async def search_impl(
    request,
    query,
    artist_count,
    artist_offset,
    album_count,
    album_offset,
    song_count,
    song_offset,
    response_key,
    music_folder_id=None,
):

    error = require_auth(request)

    if error:
        return error

    library = await build_library()
    song_map = {song["id"]: song for song in library["songs"]}

    if music_folder_id not in (None, "", "1", 1):
        return subsonic_error(request, 70, "Music folder not found.")

    q = str(query or "").strip()[:256].lower()
    artist_count = _bounded_subsonic_int(artist_count, 20)
    artist_offset = _bounded_subsonic_int(artist_offset, 0, 100000)
    album_count = _bounded_subsonic_int(album_count, 20)
    album_offset = _bounded_subsonic_int(album_offset, 0, 100000)
    song_count = _bounded_subsonic_int(song_count, 20)
    song_offset = _bounded_subsonic_int(song_offset, 0, 100000)

    matching_artists = [
        artist
        for artist in library[
            "artists"
        ].values()
        if q in artist[
            "name"
        ].lower()
    ]

    matching_albums = [
        album
        for album in library[
            "albums"
        ].values()
        if (
            q in album[
                "name"
            ].lower()
            or q in album[
                "artist"
            ].lower()
        )
    ]

    matching_songs = []

    for song in library[
        "songs"
    ]:

        haystack = (
            f"{song['title']} "
            f"{song['artist']} "
            f"{song['album']}"
        ).lower()

        if q in haystack:
            matching_songs.append(
                song
            )

    artist_objects = [
        artist_to_subsonic(artist)
        for artist in matching_artists
    ]

    album_objects = []

    for album in matching_albums:

        album_songs = [
            song_map[sid]
            for sid in album["songIds"]
            if sid in song_map
        ]

        album_objects.append(
            album_to_subsonic(
                album,
                album_songs,
            )
        )

    song_objects = await songs_to_subsonic_async(matching_songs)

    return make_subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "type": "Xrob Music",
            response_key: {
                "artist": artist_objects[
                    artist_offset:
                    artist_offset
                    + max(
                        0,
                        artist_count,
                    )
                ],
                "album": album_objects[
                    album_offset:
                    album_offset
                    + max(
                        0,
                        album_count,
                    )
                ],
                "song": song_objects[
                    song_offset:
                    song_offset
                    + max(
                        0,
                        song_count,
                    )
                ],
            },
        },
        request,
    )


@app.get("/rest/search2.view")
@app.get("/rest/search2")
async def rest_search2(
    request: Request,
    query: str = Query(""),
    artistCount: int = Query(20),
    artistOffset: int = Query(0),
    albumCount: int = Query(20),
    albumOffset: int = Query(0),
    songCount: int = Query(20),
    songOffset: int = Query(0),
    musicFolderId: Optional[str] = Query(None),
):

    return await search_impl(
        request,
        query,
        artistCount,
        artistOffset,
        albumCount,
        albumOffset,
        songCount,
        songOffset,
        "searchResult2",
        musicFolderId,
    )


@app.get("/rest/search3.view")
@app.get("/rest/search3")
async def rest_search3(
    request: Request,
    query: str = Query(""),
    artistCount: int = Query(20),
    artistOffset: int = Query(0),
    albumCount: int = Query(20),
    albumOffset: int = Query(0),
    songCount: int = Query(20),
    songOffset: int = Query(0),
    musicFolderId: Optional[str] = Query(None),
):

    return await search_impl(
        request,
        query,
        artistCount,
        artistOffset,
        albumCount,
        albumOffset,
        songCount,
        songOffset,
        "searchResult3",
        musicFolderId,
    )


# ============================================================
# RANDOM SONGS
# ============================================================

@app.get(
    "/rest/getRandomSongs.view"
)
@app.get(
    "/rest/getRandomSongs"
)
async def rest_random_songs(
    request: Request,
    size: int = Query(10),
    genre: Optional[str] = Query(None),
    fromYear: Optional[int] = Query(None),
    toYear: Optional[int] = Query(None),
    musicFolderId: Optional[str] = Query(None),
):

    error = require_auth(request)

    if error:
        return error

    library = await build_library()

    if musicFolderId not in (None, "", "1", 1):
        return subsonic_error(request, 70, "Music folder not found.")

    songs = list(library["songs"])

    if genre:
        songs = [song for song in songs if song.get("genre", "").lower() == genre.lower()]
    if fromYear is not None:
        songs = [song for song in songs if safe_int(song.get("year"), 0) >= fromYear]
    if toYear is not None:
        songs = [song for song in songs if safe_int(song.get("year"), 0) <= toYear]

    random.shuffle(songs)

    return make_subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "type": "Xrob Music",
            "randomSongs": {
                "song": await songs_to_subsonic_async(songs[:max(1, _bounded_subsonic_int(size, 10, 500))]),
            },
        },
        request,
    )


# ============================================================
# GENRES
# ============================================================

@app.get("/rest/getGenres.view")
@app.get("/rest/getGenres")
async def rest_genres(
    request: Request,
):

    error = require_auth(request)

    if error:
        return error

    library = await build_library()

    genres = []
    album_genres = {}
    for album in library["albums"].values():
        genre_name = str(album.get("genre") or "").strip()
        if genre_name:
            album_genres[genre_name] = album_genres.get(genre_name, 0) + 1

    for name, count in sorted(library["genres"].items()):
        genres.append(
            {
                "value": name,
                "songCount": count,
                "albumCount": album_genres.get(name, 0),
            }
        )

    return make_subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "type": "Xrob Music",
            "genres": {
                "genre": genres,
            },
        },
        request,
    )


@app.get(
    "/rest/getSongsByGenre.view"
)
@app.get(
    "/rest/getSongsByGenre"
)
async def rest_songs_by_genre(
    request: Request,
    genre: str = Query(""),
    count: int = Query(50),
    offset: int = Query(0),
    musicFolderId: Optional[str] = Query(None),
):

    error = require_auth(request)

    if error:
        return error

    library = await build_library()

    if musicFolderId not in (None, "", "1", 1):
        return subsonic_error(request, 70, "Music folder not found.")

    songs = [
        song
        for song in library[
            "songs"
        ]
        if (
            song["genre"].lower()
            == genre.lower()
        )
    ]

    return make_subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "type": "Xrob Music",
            "songsByGenre": {
                "song": await songs_to_subsonic_async(songs[_bounded_subsonic_int(offset, 0, 100000): _bounded_subsonic_int(offset, 0, 100000) + _bounded_subsonic_int(count, 50, 500)]),
            },
        },
        request,
    )


# ============================================================
# STREAM
# ============================================================

@app.get("/rest/stream.view")
@app.get("/rest/stream")
async def rest_stream(
    request: Request,
    id: str = Query(...),
):

    error = require_auth(request)

    if error:
        return error

    song = await find_song(id)

    if not song:

        return subsonic_error(
            request,
            70,
            "Song not found.",
        )

    return FileResponse(
        song["path"],
        media_type=MEDIA_TYPES.get(
            song["suffix"],
            "audio/mpeg",
        ),
        headers={
            "Accept-Ranges": "bytes",
            "Access-Control-Allow-Origin": "*",
        },
    )


# ============================================================
# DOWNLOAD
# ============================================================

@app.get("/rest/download.view")
@app.get("/rest/download")
async def rest_download(
    request: Request,
    id: str = Query(...),
):

    error = require_auth(request)

    if error:
        return error

    song = await find_song(id)

    if not song:

        return subsonic_error(
            request,
            70,
            "Song not found.",
        )

    return FileResponse(
        song["path"],
        media_type=MEDIA_TYPES.get(
            song["suffix"],
            "application/octet-stream",
        ),
        filename=song["path"].name,
    )


# ============================================================
# COVER ART
# ============================================================

@app.get("/rest/getCoverArt.view")
@app.get("/rest/getCoverArt")
async def rest_cover_art(
    request: Request,
    id: str = Query(...),
    size: int = Query(0),
):

    error = require_auth(request)

    if error:
        return error

    path = await resolve_cover_id(id)

    if not path:
        return Response(
            status_code=404
        )

    cover = await ensure_cover(path)

    if not cover:
        return Response(
            status_code=404
        )

    return FileResponse(
        cover,
        media_type="image/jpeg",
        headers={
            "Cache-Control":
                "public, max-age=86400",
        },
    )


# ============================================================
# STAR / UNSTAR
# ============================================================

@app.get("/rest/star.view")
@app.get("/rest/star")
async def rest_star(
    request: Request,
    id: str = Query(...),
):

    error = require_auth(request)

    if error:
        return error

    if not id:
        return subsonic_error(request, 70, "Missing media ID.")

    await asyncio.to_thread(
        set_star_sync,
        id,
        True,
    )

    return make_subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "type": "Xrob Music",
        },
        request,
    )


@app.get("/rest/unstar.view")
@app.get("/rest/unstar")
async def rest_unstar(
    request: Request,
    id: str = Query(...),
):

    error = require_auth(request)

    if error:
        return error

    if not id:
        return subsonic_error(request, 70, "Missing media ID.")

    await asyncio.to_thread(
        set_star_sync,
        id,
        False,
    )

    return make_subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "type": "Xrob Music",
        },
        request,
    )


@app.get(
    "/rest/getStarred2.view"
)
@app.get(
    "/rest/getStarred2"
)
async def rest_starred2(
    request: Request,
    musicFolderId: Optional[str] = Query(
        None
    ),
):

    error = require_auth(request)

    if error:
        return error

    rows = await asyncio.to_thread(lambda: db_connect_starred_rows())

    starred_ids = {
        row[0]
        for row in rows
    }

    library = await build_library()

    songs = await songs_to_subsonic_async([song for song in library["songs"] if song["id"] in starred_ids])

    return make_subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "type": "Xrob Music",
            "starred2": {
                "song": songs,
            },
        },
        request,
    )


# ============================================================
# PLAYLISTS
# ============================================================

def safe_song_ids(raw):
    try:
        value = json.loads(raw or "[]") if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item)[:512] for item in value[:MAX_PLAYLIST_SONGS] if item is not None]


def playlists_sync():

    with db_connect() as conn:

        conn.row_factory = sqlite3.Row

        rows = conn.execute(
            """
            SELECT *
            FROM playlists
            ORDER BY name
            """
        ).fetchall()

        return [
            dict(row)
            for row in rows
        ]


def playlist_sync(
    playlist_id,
):

    with db_connect() as conn:

        conn.row_factory = sqlite3.Row

        row = conn.execute(
            """
            SELECT *
            FROM playlists
            WHERE id = ?
            """,
            (playlist_id,),
        ).fetchone()

        return dict(row) if row else None


@app.get(
    "/rest/getPlaylists.view"
)
@app.get(
    "/rest/getPlaylists"
)
async def rest_playlists(
    request: Request,
):

    error = require_auth(request)

    if error:
        return error

    playlists = await asyncio.to_thread(
        playlists_sync
    )

    result = []

    for playlist in playlists:

        ids = safe_song_ids(
            playlist.get(
                "song_ids",
                "[]",
            )
        )

        result.append(
            {
                "id": playlist["id"],
                "name": playlist["name"],
                "comment": playlist["comment"],
                "owner": playlist["owner"],
                "public": bool(
                    playlist["public"]
                ),
                "songCount": len(ids),
            }
        )

    return make_subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "type": "Xrob Music",
            "playlists": {
                "playlist": result,
            },
        },
        request,
    )


@app.get(
    "/rest/getPlaylist.view"
)
@app.get(
    "/rest/getPlaylist"
)
async def rest_playlist(
    request: Request,
    id: str = Query(...),
):

    error = require_auth(request)

    if error:
        return error

    playlist = await asyncio.to_thread(
        playlist_sync,
        id,
    )

    if not playlist:

        return subsonic_error(
            request,
            70,
            "Playlist not found.",
        )

    ids = safe_song_ids(
        playlist.get(
            "song_ids",
            "[]",
        )
    )

    library = await build_library()
    song_map = {song["id"]: song for song in library["songs"]}

    playlist_songs = [song_map[song_id] for song_id in ids if song_id in song_map]

    songs = await songs_to_subsonic_async(playlist_songs)

    return make_subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "type": "Xrob Music",
            "playlist": {
                "id": playlist["id"],
                "name": playlist["name"],
                "comment": playlist["comment"],
                "owner": playlist["owner"],
                "public": bool(
                    playlist["public"]
                ),
                "songCount": len(songs),
                "entry": songs,
            },
        },
        request,
    )



# ============================================================
# PLAYER / PLAYLIST / HEALTH API
# ============================================================
def _mark_song_review_sync(song_id, state, actioned_at=0.0, history=False):
    with db_connect() as conn:
        conn.execute("INSERT INTO song_review(song_id,state,actioned_at) VALUES(?,?,?) ON CONFLICT(song_id) DO UPDATE SET state=excluded.state,actioned_at=excluded.actioned_at", (song_id, state, actioned_at))
        if history:
            conn.execute("INSERT INTO song_editor_history(song_id,edited_at) VALUES(?,?) ON CONFLICT(song_id) DO UPDATE SET edited_at=excluded.edited_at", (song_id, actioned_at))
        conn.commit()


def _song_editor_snapshot_sync(song_ids):
    with db_connect() as conn:
        conn.execute("INSERT OR IGNORE INTO song_editor_history(song_id,edited_at) SELECT song_id,actioned_at FROM song_review WHERE state='edited'")
        if song_ids:
            conn.executemany("INSERT OR IGNORE INTO song_review(song_id,state,actioned_at) VALUES(?,?,0)", [(song_id, "pending") for song_id in song_ids])
        conn.commit()
        conn.row_factory = sqlite3.Row
        review_rows = conn.execute("SELECT song_id,state,actioned_at FROM song_review").fetchall()
        history_rows = conn.execute("SELECT song_id,edited_at FROM song_editor_history ORDER BY edited_at DESC, song_id").fetchall()
    return review_rows, history_rows


def _reset_song_editor_sync(song_ids):
    with db_connect() as conn:
        conn.execute("DELETE FROM song_review")
        if song_ids:
            conn.executemany("INSERT INTO song_review(song_id,state,actioned_at) VALUES(?,?,0)", [(song_id, "pending") for song_id in song_ids])
        conn.commit()


def _artist_artwork_get_sync(artist_id):
    with db_connect() as conn:
        return conn.execute("SELECT data,mime FROM artist_artwork WHERE artist_id=?", (artist_id,)).fetchone()

def _artist_artwork_save_sync(artist_id, data, mime):
    with db_connect() as conn:
        conn.execute("INSERT INTO artist_artwork(artist_id,data,mime,updated_at) VALUES(?,?,?,?) ON CONFLICT(artist_id) DO UPDATE SET data=excluded.data,mime=excluded.mime,updated_at=excluded.updated_at", (artist_id,data,mime,time.time()))
        conn.commit()

def db_connect_starred_rows():
    with db_connect() as conn:
        return conn.execute("SELECT item_id FROM stars").fetchall()


def _library_statistics_db_sync(cutoff_7d, cutoff_30d):
    with db_connect() as conn:
        total_plays = int(conn.execute("SELECT COUNT(*) FROM play_history").fetchone()[0])
        unique_played = int(conn.execute("SELECT COUNT(DISTINCT song_id) FROM play_history").fetchone()[0])
        first_play = conn.execute("SELECT MIN(played_at) FROM play_history").fetchone()[0]
        last_play = conn.execute("SELECT MAX(played_at) FROM play_history").fetchone()[0]
        recent_plays = int(conn.execute("SELECT COUNT(*) FROM play_history WHERE played_at >= ?", (cutoff_7d,)).fetchone()[0])
        play_seconds = float(conn.execute("SELECT COALESCE(SUM(CASE WHEN duration > 0 THEN MIN(position, duration) ELSE position END),0) FROM play_history").fetchone()[0] or 0)
        top_artists_rows = conn.execute("SELECT ph.song_id, COUNT(*) AS plays FROM play_history ph GROUP BY ph.song_id ORDER BY plays DESC, MAX(ph.played_at) DESC LIMIT 20").fetchall()
        top_recent_rows = conn.execute("SELECT ph.song_id, COUNT(*) AS plays, MAX(ph.played_at) AS last_play FROM play_history ph WHERE ph.played_at >= ? GROUP BY ph.song_id ORDER BY plays DESC, last_play DESC LIMIT 12", (cutoff_30d,)).fetchall()
    return total_plays, unique_played, first_play, last_play, recent_plays, play_seconds, top_artists_rows, top_recent_rows



def _play_count_map_sync():
    with db_connect() as conn:
        return {r[0]: int(r[1]) for r in conn.execute("SELECT song_id, COUNT(*) FROM play_history GROUP BY song_id").fetchall()}


def _play_totals_sync():
    with db_connect() as conn:
        total = int(conn.execute("SELECT COUNT(*) FROM play_history").fetchone()[0])
        distinct = int(conn.execute("SELECT COUNT(DISTINCT song_id) FROM play_history").fetchone()[0])
        return total, distinct


def _recent_most_sync():
    with db_connect() as conn:
        recent = conn.execute("SELECT song_id, COUNT(*) c, MAX(played_at) t FROM play_history GROUP BY song_id ORDER BY t DESC LIMIT 24").fetchall()
        most = conn.execute("SELECT song_id, COUNT(*) c, MAX(played_at) t FROM play_history GROUP BY song_id ORDER BY c DESC, t DESC LIMIT 24").fetchall()
    return recent, most


def _playlist_rows_sync():
    with db_connect() as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM playlists ORDER BY name COLLATE NOCASE").fetchall()


def _playlist_create_sync(values):
    with db_connect() as conn:
        conn.execute("INSERT INTO playlists(id,name,comment,owner,public,song_ids,created_at,updated_at,kind,rules) VALUES(?,?,?,?,?,?,?,?,?,?)", values)
        conn.commit()


def _playlist_update_sync(values):
    with db_connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM playlists WHERE id=?", (values[-1],)).fetchone()
        if not row:
            return False
        conn.execute("UPDATE playlists SET name=?,comment=?,song_ids=?,updated_at=?,kind=?,rules=? WHERE id=?", values)
        conn.commit()
        return True


def _playlist_delete_sync(playlist_id):
    with db_connect() as conn:
        conn.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
        conn.commit()


def _daily_mix_db_sync(cutoff_24h, now=None):
    with db_connect() as conn:
        rows = conn.execute("""
            SELECT song_id, COUNT(*) AS plays, MAX(played_at) AS last_play,
                   COALESCE(SUM(CASE WHEN duration > 0 THEN MIN(position, duration) ELSE position END),0) AS heard,
                   COALESCE(SUM(duration),0) AS duration_sum
            FROM play_history GROUP BY song_id
        """).fetchall()
        recent_rows = conn.execute("SELECT song_id, MAX(played_at) FROM play_history WHERE played_at >= ? GROUP BY song_id", (cutoff_24h,)).fetchall()
        star_rows = conn.execute("SELECT item_id FROM stars").fetchall()
    return rows, recent_rows, star_rows


def _scan_state_sync(status, mode=None, message="", started_at=None):
    with db_connect() as conn:
        if status == "running":
            conn.execute("INSERT INTO scan_state(id,started_at,finished_at,mode,status,message) VALUES(1,?,NULL,?,?,?) ON CONFLICT(id) DO UPDATE SET started_at=excluded.started_at,finished_at=NULL,mode=excluded.mode,status=excluded.status,message=excluded.message", (started_at or time.time(), mode, status, message))
        else:
            conn.execute("UPDATE scan_state SET finished_at=?,status=?,message=? WHERE id=1", (time.time(), status, message))
        conn.commit()


def _scan_state_read_sync():
    with db_connect() as conn:
        row = conn.execute("SELECT started_at,finished_at,mode,status,message FROM scan_state WHERE id=1").fetchone()
    return dict(zip(["started_at","finished_at","mode","status","message"], row)) if row else {"status":"idle"}


def _errors_sync(limit):
    with db_connect() as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT id,created_at,source,message,task_id FROM app_errors ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()

def write_app_error_sync(source, message, task_id=None):
    try:
        with db_connect() as conn:
            conn.execute("INSERT INTO app_errors(created_at,source,message,task_id) VALUES(?,?,?,?)", (time.time(), str(source), str(message), task_id))
            conn.execute("DELETE FROM app_errors WHERE id IN (SELECT id FROM app_errors ORDER BY id DESC LIMIT -1 OFFSET ?)", (APP_ERRORS_MAX_ROWS,))
            conn.commit()
    except Exception:
        pass


async def write_app_error(source, message, task_id=None):
    await asyncio.to_thread(write_app_error_sync, source, message, task_id)


def _playlist_row_to_dict(row):
    d = dict(row)
    d["song_ids"] = safe_song_ids(d.get("song_ids", "[]"))
    raw_rules = d.get("rules")
    if isinstance(raw_rules, str):
        try:
            parsed_rules = json.loads(raw_rules or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed_rules = {}
    else:
        parsed_rules = raw_rules or {}
    d["rules"] = parsed_rules if isinstance(parsed_rules, dict) else {}
    d["public"] = bool(d.get("public", 0))
    d["kind"] = "smart" if str(d.get("kind") or "").lower() == "smart" else "manual"
    return d


def _device_type_from_payload(payload):
    raw=str(payload.get("deviceType") or "browser").lower()
    return raw if raw in {"desktop","phone","tablet","browser","tv"} else "browser"

def _public_devices(rows, player_data=None):
    player_state=(player_data or {}).get("state") if isinstance(player_data,dict) else None
    owner_id=str(player_state.get("ownerId") or "") if isinstance(player_state,dict) else ""
    owner_client_id=str(player_state.get("clientId") or "") if isinstance(player_state,dict) else ""
    sync_ids=set(str(x) for x in ((player_state or {}).get("syncDeviceIds") if isinstance(player_state,dict) else []) if str(x))
    sync_enabled=str((player_state or {}).get("syncMode") or "off") == "linked" and len(sync_ids) >= 2
    now=time.time()
    output=[]
    seen=set()
    for row in rows:
        device_id=str(row.get("device_id") or "")
        if not device_id or device_id in seen: continue
        seen.add(device_id)
        age=max(0,now-float(row.get("last_seen_at") or 0))
        online=age<=MAX_DEVICE_STALE_SECONDS
        is_owner=bool(owner_id and row.get("tab_id") == owner_id) or bool(owner_client_id and row.get("client_id") == owner_client_id)
        is_synced=sync_enabled and str(row.get("tab_id") or "") in sync_ids
        state="offline"
        if online and (is_owner or is_synced):
            state="paused" if bool(player_state and player_state.get("paused")) else "playing"
        elif online:
            state="available"
        track=None
        if (is_owner or is_synced) and isinstance(player_state,dict) and player_state.get("src"):
            track={"title":player_state.get("title") or "Unknown Track","artist":player_state.get("artist") or "Unknown Artist","album":player_state.get("album") or "","art":player_state.get("art") or "","currentTime":float(player_state.get("currentTime") or 0),"duration":float(player_state.get("duration") or 0),"paused":bool(player_state.get("paused"))}
        try:
            capabilities=json.loads(row.get("capabilities_json") or "{}") if row.get("capabilities_json") else {}
            if not isinstance(capabilities, dict): capabilities={}
        except Exception:
            capabilities={}
        output.append({"deviceId":device_id,"clientId":row.get("client_id") or "","tabId":row.get("tab_id") or "","name":row.get("name") or "This device","deviceType":row.get("device_type") or "browser","platform":row.get("platform") or "","browser":row.get("browser") or "","lastSeenAt":row.get("last_seen_at") or 0,"ageSeconds":age,"online":online,"state":state,"isOwner":is_owner,"isSynced":is_synced,"track":track,"capabilities":capabilities})
    if isinstance(player_state,dict) and owner_client_id and owner_client_id not in {str(x.get("clientId")) for x in output if x.get("clientId")}:
        output.append({"deviceId":owner_client_id,"clientId":owner_client_id,"tabId":owner_id,"name":player_state.get("deviceName") or "Current device","deviceType":"browser","platform":"","browser":"","lastSeenAt":player_data.get("last_seen_at") if isinstance(player_data,dict) else now,"ageSeconds":0,"online":True,"state":"paused" if player_state.get("paused") else "playing","isOwner":True,"isSynced":sync_enabled and owner_id in sync_ids,"track":{"title":player_state.get("title") or "Unknown Track","artist":player_state.get("artist") or "Unknown Artist","art":player_state.get("art") or "","currentTime":float(player_state.get("currentTime") or 0),"duration":float(player_state.get("duration") or 0),"paused":bool(player_state.get("paused"))},"capabilities":{}})
    return output

@app.post("/api/devices/register")
async def api_device_register(payload: dict = Body(...)):
    device_id=str(payload.get("deviceId") or "").strip()[:200]
    tab_id=str(payload.get("tabId") or "").strip()[:200]
    if not device_id or not tab_id: raise HTTPException(400,"deviceId and tabId are required")
    name=str(payload.get("name") or "This device").strip()[:120] or "This device"
    device={"deviceId":device_id,"clientId":str(payload.get("clientId") or device_id)[:200],"tabId":tab_id,"name":name,"deviceType":_device_type_from_payload(payload),"platform":str(payload.get("platform") or "")[:80],"browser":str(payload.get("browser") or "")[:80],"capabilities":payload.get("capabilities") if isinstance(payload.get("capabilities"),dict) else {}}
    await asyncio.to_thread(db_register_device_sync,device)
    data=await get_player_state_async()
    return {"status":"ok","device":device,"devices":_public_devices(await asyncio.to_thread(db_get_devices_sync),data)}

@app.post("/api/devices/heartbeat")
async def api_device_heartbeat(payload: dict = Body(...)):
    device_id=str(payload.get("deviceId") or "").strip()[:200]
    tab_id=str(payload.get("tabId") or "").strip()[:200]
    if not device_id or not tab_id: raise HTTPException(400,"deviceId and tabId are required")
    name=str(payload.get("name") or "This device").strip()[:120] or "This device"
    device={"deviceId":device_id,"clientId":str(payload.get("clientId") or device_id)[:200],"tabId":tab_id,"name":name,"deviceType":_device_type_from_payload(payload),"platform":str(payload.get("platform") or "")[:80],"browser":str(payload.get("browser") or "")[:80],"capabilities":payload.get("capabilities") if isinstance(payload.get("capabilities"),dict) else {}}
    await asyncio.to_thread(db_register_device_sync,device)
    return {"status":"ok","seenAt":time.time()}

@app.get("/api/devices")
async def api_devices():
    player_data=await get_player_state_async()
    devices=await asyncio.to_thread(db_get_devices_sync)
    return {"devices":_public_devices(devices,player_data),"ownerId":str((player_data or {}).get("state",{}).get("ownerId") or "") if isinstance(player_data,dict) else ""}

@app.post("/api/devices/rename")
async def api_device_rename(payload: dict = Body(...)):
    device_id=str(payload.get("deviceId") or "").strip()[:200]
    name=str(payload.get("name") or "").strip()[:120]
    if not device_id or not name: raise HTTPException(400,"deviceId and name are required")
    def rename():
        with db_connect() as conn:
            conn.execute("UPDATE devices SET name=?,updated_at=? WHERE device_id=?",(name,time.time(),device_id)); conn.commit()
    changed=await asyncio.to_thread(rename)
    return {"status":"ok"}

@app.delete("/api/devices/{device_id}")
async def api_device_remove(device_id:str):
    device_id = str(device_id or "").strip()[:200]
    row = await asyncio.to_thread(lambda: next((r for r in db_get_devices_sync() if str(r.get("device_id") or "") == device_id), None))
    tab_id = str((row or {}).get("tab_id") or "").strip()
    if tab_id:
        async with PLAYER_STATE_LOCK:
            current_data = await get_player_state_async()
            current = dict(current_data.get("state") or {}) if isinstance(current_data, dict) else {}
            sync_ids = [str(x) for x in (current.get("syncDeviceIds") or []) if str(x)]
            if tab_id in sync_ids:
                sync_ids = [x for x in sync_ids if x != tab_id]
                if len(sync_ids) < 2:
                    current.pop("syncDeviceIds", None); current.pop("syncGroupId", None); current["syncMode"] = "off"
                else:
                    current["syncDeviceIds"] = sync_ids
                    current["syncGroupId"] = hashlib.sha256("|".join(sorted(sync_ids)).encode("utf-8")).hexdigest()[:24]
                current["seq"] = safe_int(current.get("seq"), 0) + 1
                await publish_player_state(current, full=True)
    def remove():
        with db_connect() as conn:
            conn.execute("DELETE FROM devices WHERE device_id=?",(device_id,)); conn.commit()
    await asyncio.to_thread(remove)
    return {"status":"ok"}

@app.get("/api/player/state")
async def api_player_state():
    data = await get_player_state_async()
    if not data:
        return {"state": None, "updated_at": 0}
    return data


@app.post("/api/player/state")
async def api_player_state_update(payload: dict = Body(...)):
    state = payload.get("state") if isinstance(payload, dict) else None
    if not isinstance(state, dict):
        raise HTTPException(400, "state is required")
    owner_id = str(state.get("ownerId") or "").strip()
    if not owner_id:
        raise HTTPException(400, "state.ownerId is required")
    state = _sanitize_player_state(state)
    force = bool(payload.get("takeover") or state.get("takeover"))
    state.pop("takeover", None)

    # The server is the cross-device source of truth. A live owner cannot be
    # silently replaced by another browser; explicit Take Over sends force=true.
    async with PLAYER_STATE_LOCK:
        current = await get_player_state_async()
        current_state = current.get("state") if isinstance(current, dict) else None
        current_owner = str(current_state.get("ownerId") or "").strip() if isinstance(current_state, dict) else ""
        current_updated = float(current.get("updated_at") or 0) if isinstance(current, dict) else 0.0
        current_age = time.time() - current_updated if current_updated else float("inf")
        if current_owner and current_owner != owner_id and current_age <= PLAYER_STATE_MAX_AGE_SECONDS and not force:
            raise HTTPException(409, {
                "status": "owned",
                "ownerId": current_owner,
                "updated_at": current_updated,
            })
        incoming_seq = safe_int(state.get("seq"), 0)
        current_seq = safe_int(current_state.get("seq"), 0) if isinstance(current_state, dict) else 0
        if current_owner == owner_id and not force and current_seq > incoming_seq:
            # Every state snapshot is ordered by the owner's monotonically increasing
            # sequence number. Network retries, background timers and browser lifecycle
            # events can otherwise deliver an older snapshot after a newer one.
            return {
                "status": "ignored_stale",
                "updated_at": current.get("updated_at", 0) if isinstance(current, dict) else 0,
                "seq": current_seq,
                "ownerId": owner_id,
            }

        # The browser sends a snapshot; the server re-anchors its authoritative clock here.
        state.pop("positionUpdatedAt", None)
        state.pop("playStartedAt", None)
        await publish_player_state(state, full=force or bool(payload.get("full", False)))
        return {"status": "ok", "updated_at": PLAYER_STATE_UPDATED_AT, "seq": safe_int(PLAYER_STATE.get("seq"), 0)}


def _sanitize_player_command_payload(command, payload):
    data = payload if isinstance(payload, dict) else {}
    if command == "seek":
        value = data.get("time", 0)
        try:
            value = float(value)
        except (TypeError, ValueError):
            return {}
        return {"time": max(0.0, value) if math.isfinite(value) else 0.0}
    if command == "volume":
        value = data.get("volume", 0.8)
        try:
            value = float(value)
        except (TypeError, ValueError):
            return {}
        return {"volume": min(1.0, max(0.0, value)) if math.isfinite(value) else 0.8}
    if command in {"next", "previous"}:
        repeat = str(data.get("repeat") or "off")
        return {"repeat": repeat if repeat in {"off", "track", "queue"} else "off"}
    if command == "shuffle":
        return {"enabled": bool(data.get("enabled"))}
    if command == "repeat":
        mode = str(data.get("mode") or "off")
        return {"mode": mode if mode in {"off", "track", "queue"} else "off"}
    if command == "queue":
        state = _sanitize_player_state(data)
        allowed = {"queue", "queueIndex", "source"}
        return {key: state[key] for key in allowed if key in state}
    if command == "load-play":
        state = _sanitize_player_state(data)
        allowed = {"src", "source", "title", "artist", "art", "songId", "queueIndex", "queue", "dailyMix", "repeatMode", "shuffle"}
        return {key: state[key] for key in allowed if key in state}
    return {}


@app.post("/api/player/mirror")
async def api_player_mirror(payload: dict = Body(...)):
    target_id = str(payload.get("targetId") or "").strip()[:200]
    if not target_id:
        raise HTTPException(400, "targetId is required")
    rows = await asyncio.to_thread(db_get_devices_sync)
    target = next((row for row in rows if str(row.get("tab_id") or "") == target_id), None)
    if not target:
        raise HTTPException(404, "Device not found")
    last_seen = float(target.get("last_seen_at") or 0)
    if last_seen and time.time() - last_seen > MAX_DEVICE_STALE_SECONDS:
        raise HTTPException(409, "Device is offline")
    async with PLAYER_STATE_LOCK:
        current_data = await get_player_state_async()
        current = dict(current_data.get("state") or {}) if isinstance(current_data, dict) else {}
        owner_id = str(current.get("ownerId") or "").strip()
        if not owner_id:
            raise HTTPException(409, "No active player session")
        if target_id == owner_id:
            raise HTTPException(409, "This device already controls playback")
        now = time.time(); live_position = _effective_player_position(current, now)
        src = str(current.get("src") or payload.get("src") or "").strip()[:2000]
        if not src:
            raise HTTPException(409, "No active track to mirror")
        sync_ids = []
        for raw_id in [*(current.get("syncDeviceIds") or []), owner_id, target_id]:
            value = str(raw_id or "").strip()[:200]
            if value and value not in sync_ids: sync_ids.append(value)
        sync_ids = sync_ids[:PLAYER_STATE_MAX_SYNC_DEVICES]
        group_key = "|".join(sorted(sync_ids))
        next_state = dict(current)
        next_state["syncMode"] = "linked"
        next_state["syncDeviceIds"] = sync_ids
        next_state["syncGroupId"] = hashlib.sha256(group_key.encode("utf-8")).hexdigest()[:24]
        next_state["currentTime"] = live_position; next_state["positionUpdatedAt"] = now; next_state["lastSeenAt"] = now; next_state["at"] = now
        next_state["seq"] = safe_int(current.get("seq"), 0) + 1
        next_state = _sanitize_player_state(next_state)
        await publish_player_state(next_state, full=True)
        message_payload = {
            "src": src, "title": str(current.get("title") or payload.get("title") or "Unknown Track")[:300],
            "artist": str(current.get("artist") or payload.get("artist") or "Unknown Artist")[:300],
            "art": str(current.get("art") or payload.get("art") or "")[:2000],
            "songId": str(current.get("songId") or payload.get("songId") or "")[:512],
            "source": str(current.get("source") or payload.get("source") or "library")[:32], "currentTime": live_position,
        }
    await manager.broadcast({"type":"command","targetId":target_id,"command":"mirror-play","payload":message_payload,"id":str(payload.get("id") or uuid.uuid4().hex)[:300]})
    return {"status":"ok","targetId":target_id,"deviceId":target.get("device_id"),"syncMode":"linked","syncDeviceIds":sync_ids}


@app.post("/api/player/sync-group")
async def api_player_sync_group(payload: dict = Body(...)):
    action = str(payload.get("action") or "").strip().lower()
    target_id = str(payload.get("targetId") or "").strip()[:200]
    if action not in {"add", "remove", "clear"}: raise HTTPException(400, "Unsupported sync-group action")
    async with PLAYER_STATE_LOCK:
        current_data = await get_player_state_async(); current = dict(current_data.get("state") or {}) if isinstance(current_data, dict) else {}
        owner_id = str(current.get("ownerId") or "").strip()
        if not owner_id: raise HTTPException(409, "No active player session")
        sync_ids=[]
        for raw_id in current.get("syncDeviceIds") or []:
            value=str(raw_id or "").strip()[:200]
            if value and value not in sync_ids: sync_ids.append(value)
        if action == "clear": sync_ids=[]
        elif action == "add":
            if not target_id: raise HTTPException(400, "targetId is required")
            if target_id == owner_id: raise HTTPException(409, "This device already controls playback")
            devices = await asyncio.to_thread(db_get_devices_sync)
            target = next((row for row in devices if str(row.get("tab_id") or "") == target_id), None)
            if not target: raise HTTPException(404, "Device not found")
            if float(target.get("last_seen_at") or 0) and time.time() - float(target.get("last_seen_at") or 0) > MAX_DEVICE_STALE_SECONDS:
                raise HTTPException(409, "Device is offline")
            if target_id not in sync_ids: sync_ids.append(target_id)
            if owner_id not in sync_ids: sync_ids.insert(0, owner_id)
        else:
            if target_id: sync_ids=[x for x in sync_ids if x != target_id]
        sync_ids=sync_ids[:PLAYER_STATE_MAX_SYNC_DEVICES]
        if len(sync_ids)<2:
            current["syncMode"]="off"; current.pop("syncDeviceIds",None); current.pop("syncGroupId",None)
        else:
            current["syncMode"]="linked"; current["syncDeviceIds"]=sync_ids; current["syncGroupId"]=hashlib.sha256("|".join(sorted(sync_ids)).encode("utf-8")).hexdigest()[:24]
        current["seq"]=safe_int(current.get("seq"),0)+1
        await publish_player_state(current, full=True)
        return {"status":"ok","syncMode":current.get("syncMode"),"syncDeviceIds":current.get("syncDeviceIds",[]),"syncGroupId":current.get("syncGroupId","")}


@app.post("/api/player/command")
async def api_player_command(payload: dict = Body(...)):
    target_id = str(payload.get("targetId") or "").strip()
    command = str(payload.get("command") or "").strip()
    if not target_id or not command:
        raise HTTPException(400, "targetId and command are required")
    allowed_commands = {"play", "pause", "stop", "seek", "next", "previous", "volume", "shuffle", "repeat", "queue", "load-play"}
    if command not in allowed_commands:
        raise HTTPException(400, "Unsupported player command")

    message = {
        "type": "command",
        "targetId": target_id[:200],
        "command": command[:64],
        "payload": _sanitize_player_command_payload(command, payload.get("payload")),
        "id": str(payload.get("id") or "")[:300],
    }

    # Commands are state changes, not fire-and-forget events. Persist the resulting
    # player state so a device that is temporarily offline/reconnecting still
    # converges to the requested state when it reconnects. The target must remain
    # the current player owner, otherwise an old device could mutate a newer session.
    async with PLAYER_STATE_LOCK:
        current = await get_player_state_async()
        current_state = dict(current.get("state") or {}) if isinstance(current, dict) else {}
        current_owner = str(current_state.get("ownerId") or "").strip()
        if current_owner != target_id:
            if not current_owner:
                raise HTTPException(409, "No active player owner")
            raise HTTPException(409, {"status": "owned", "ownerId": current_owner})

        now = time.time()
        # Commands start from the server's live playback clock, not the last browser snapshot.
        current_state["currentTime"] = _effective_player_position(current_state, now)
        current_state["positionUpdatedAt"] = now
        current_state["lastSeenAt"] = now
        next_state = dict(current_state)
        command_payload = message["payload"]
        if command == "play":
            next_state["paused"] = False
        elif command in {"pause", "stop"}:
            next_state["paused"] = True
            if command == "stop": next_state["currentTime"] = 0.0
        elif command == "seek":
            next_state["currentTime"] = max(0.0, float(command_payload.get("time", 0)))
            duration = float(next_state.get("duration") or 0)
            if duration > 0:
                next_state["currentTime"] = min(next_state["currentTime"], duration)
        elif command == "volume":
            next_state["volume"] = min(1.0, max(0.0, float(command_payload.get("volume", 0.8))))
        elif command == "shuffle":
            next_state["shuffle"] = bool(command_payload.get("enabled"))
        elif command == "repeat":
            next_state["repeatMode"] = command_payload.get("mode", "off") if command_payload.get("mode") in {"off", "track", "queue"} else "off"
        elif command == "queue":
            if "queue" in command_payload:
                next_state["queue"] = command_payload["queue"] if isinstance(command_payload["queue"], list) else []
            if "queueIndex" in command_payload:
                next_state["queueIndex"] = max(-1, safe_int(command_payload.get("queueIndex"), -1))
            if command_payload.get("source"):
                next_state["source"] = str(command_payload.get("source"))[:32]
        elif command == "load-play":
            for key in ("src", "source", "title", "artist", "art", "songId", "queueIndex", "queue", "dailyMix", "repeatMode", "shuffle"):
                if key in command_payload:
                    next_state[key] = command_payload[key]
            next_state["currentTime"] = 0.0
            next_state["paused"] = False
        elif command in {"next", "previous"}:
            queue = next_state.get("queue") if isinstance(next_state.get("queue"), list) else []
            current_index = safe_int(next_state.get("queueIndex"), 0)
            repeat = command_payload.get("repeat") or next_state.get("repeatMode") or "off"
            if queue:
                direction = 1 if command == "next" else -1
                next_index = current_index + direction
                if next_index < 0 or next_index >= len(queue):
                    if repeat != "queue":
                        next_index = current_index
                    else:
                        next_index = 0 if command == "next" else len(queue) - 1
                track = queue[next_index] if 0 <= next_index < len(queue) else None
                if isinstance(track, dict) and next_index != current_index:
                    next_state["queueIndex"] = next_index
                    for key in ("id", "name", "title", "artist", "album", "duration", "cover", "stream"):
                        if key in track:
                            if key == "id":
                                next_state["songId"] = track[key]
                            elif key == "stream":
                                next_state["src"] = track[key]
                            elif key == "cover":
                                next_state["art"] = track[key]
                            else:
                                next_state[key] = track[key]
                    next_state["currentTime"] = 0.0
                    next_state["paused"] = False

        next_state["seq"] = max(safe_int(current_state.get("seq"), 0) + 1, safe_int(payload.get("seq"), 0))
        await publish_player_state(next_state, full=True)

    # The live target receives the command immediately as well. The client deduplicates
    # the same command ID when both WebSocket and BroadcastChannel paths deliver it.
    await manager.broadcast(message)
    return {"status": "ok", "state": next_state}


@app.post("/api/player/heartbeat")
async def api_player_heartbeat(payload: dict = Body(...)):
    owner_id = str(payload.get("ownerId") or "").strip()[:200]
    client_id = str(payload.get("clientId") or "").strip()[:200]
    if not owner_id:
        raise HTTPException(400, "ownerId is required")
    async with PLAYER_STATE_LOCK:
        current = await get_player_state_async()
        current_state = dict(current.get("state") or {}) if isinstance(current, dict) else {}
        if str(current_state.get("ownerId") or "") != owner_id:
            raise HTTPException(409, {"status": "owned", "ownerId": current_state.get("ownerId")})
        if client_id and current_state.get("clientId") and client_id != current_state.get("clientId"):
            raise HTTPException(409, {"status": "owned", "ownerId": current_state.get("ownerId")})
        # Heartbeats are not allowed to invent a position. Preserve the server clock.
        now = time.time()
        current_state["currentTime"] = _effective_player_position(current_state, now)
        current_state["positionUpdatedAt"] = now
        current_state["lastSeenAt"] = now
        current_state["seq"] = safe_int(current_state.get("seq"), 0) + 1
        await publish_player_state(current_state, full=False)
        state = dict(PLAYER_STATE)
        state["currentTime"] = _effective_player_position(state, time.time())
        state["positionUpdatedAt"] = PLAYER_STATE_UPDATED_AT
        return {"status": "ok", "state": state, "updated_at": PLAYER_STATE_UPDATED_AT}


@app.post("/api/player/handoff")
async def api_player_handoff(payload: dict = Body(...)):
    new_owner_id = str(payload.get("newOwnerId") or "").strip()[:200]
    new_client_id = str(payload.get("clientId") or "").strip()[:200]
    new_device_name = str(payload.get("deviceName") or "This device").strip()[:120]
    expected_owner_id = str(payload.get("expectedOwnerId") or "").strip()[:200]
    if not new_owner_id:
        raise HTTPException(400, "newOwnerId is required")

    async with PLAYER_STATE_LOCK:
        current = await get_player_state_async()
        current_state = dict(current.get("state") or {}) if isinstance(current, dict) else {}
        current_owner = str(current_state.get("ownerId") or "").strip()
        if not current_owner:
            raise HTTPException(409, "No active player session")
        if expected_owner_id and expected_owner_id != current_owner:
            raise HTTPException(409, {"status": "owned", "ownerId": current_owner})
        if current_owner == new_owner_id:
            return {"status": "already_owner", "state": current_state}

        now = time.time()
        live_position = _effective_player_position(current_state, now)
        previous_owner = current_owner
        next_state = dict(current_state)
        next_state["ownerId"] = new_owner_id
        if new_client_id:
            next_state["clientId"] = new_client_id
        next_state["deviceName"] = new_device_name
        next_state["currentTime"] = live_position
        next_state["positionUpdatedAt"] = now
        next_state["lastSeenAt"] = now
        next_state["at"] = now
        if next_state.get("paused"):
            next_state.pop("playStartedAt", None)
        else:
            next_state["playStartedAt"] = now
        # A handoff from a linked session must keep the remaining sync group
        # coherent: the new owner stays linked, while the previous owner is
        # removed so an offline/stale device cannot reclaim playback later.
        sync_ids = []
        for raw_id in current_state.get("syncDeviceIds") or []:
            value = str(raw_id or "").strip()[:200]
            if value and value not in sync_ids and value != previous_owner:
                sync_ids.append(value)
        if new_owner_id not in sync_ids:
            sync_ids.insert(0, new_owner_id)
        sync_ids = sync_ids[:PLAYER_STATE_MAX_SYNC_DEVICES]
        if len(sync_ids) >= 2:
            next_state["syncMode"] = "linked"
            next_state["syncDeviceIds"] = sync_ids
            next_state["syncGroupId"] = hashlib.sha256("|".join(sorted(sync_ids)).encode("utf-8")).hexdigest()[:24]
        else:
            next_state["syncMode"] = "off"
            next_state.pop("syncDeviceIds", None)
            next_state.pop("syncGroupId", None)
        next_state["seq"] = safe_int(current_state.get("seq"), 0) + 1
        next_state["handoffId"] = str(uuid.uuid4())
        await publish_player_state(next_state, full=True, broadcast=False)
        outbound_state = dict(PLAYER_STATE)
        outbound_state["currentTime"] = live_position
        outbound_state["_handoffFrom"] = previous_owner
        outbound_state["_handoffTo"] = new_owner_id
        await manager.broadcast({
            "type": "player_handoff",
            "fromOwnerId": previous_owner,
            "toOwnerId": new_owner_id,
            "state": outbound_state,
        })
        return {"status": "ok", "state": outbound_state, "updated_at": PLAYER_STATE_UPDATED_AT}


def get_player_positions_sync():
    with db_connect() as conn:
        rows = conn.execute("SELECT song_id,position,duration,updated_at FROM playback_positions").fetchall()
    return {r[0]: {"position": r[1], "duration": r[2], "updated_at": r[3]} for r in rows}


def save_player_position_sync(song_id, position, duration, now):
    duration = max(0.0, min(86_400.0, float(duration or 0)))
    position = max(0.0, min(86_400.0, float(position or 0)))
    if duration > 0:
        position = min(position, duration)
    with db_connect() as conn:
        conn.execute("INSERT INTO playback_positions(song_id,position,duration,updated_at) VALUES(?,?,?,?) ON CONFLICT(song_id) DO UPDATE SET position=excluded.position,duration=excluded.duration,updated_at=excluded.updated_at", (song_id, position, duration, now))
        conn.commit()


def save_player_history_sync(song_id, duration, position):
    with db_connect() as conn:
        conn.execute("INSERT INTO play_history(song_id,played_at,duration,position) VALUES(?,?,?,?)", (song_id, time.time(), duration, position))
        conn.execute("DELETE FROM play_history WHERE id IN (SELECT id FROM play_history ORDER BY id DESC LIMIT -1 OFFSET ?)", (HISTORY_MAX_ROWS,))
        try:
            retention = max(30, min(3650, int(load_settings().get("stats_retention_days", 365))))
            conn.execute("DELETE FROM play_history WHERE played_at < ?", (time.time() - retention * 86400,))
        except Exception:
            pass
        conn.commit()
        return int(conn.execute("SELECT COUNT(*) FROM play_history").fetchone()[0])


@app.get("/api/player/positions")
async def api_player_positions():
    return await asyncio.to_thread(get_player_positions_sync)


@app.post("/api/player/position")
async def api_player_position(payload: dict = Body(...)):
    song_id=str(payload.get("song_id") or "").strip()[:512]
    if not song_id: raise HTTPException(400, "song_id is required")
    position = finite_nonnegative_float(payload.get("position", 0), "position")
    duration = finite_nonnegative_float(payload.get("duration", 0), "duration")
    now=time.time()
    await asyncio.to_thread(save_player_position_sync, song_id, position, duration, now)
    return {"status":"ok"}


@app.post("/api/player/history")
async def api_player_history(payload: dict = Body(...)):
    song_id=str(payload.get("song_id") or "").strip()[:512]
    if not song_id: raise HTTPException(400, "song_id is required")
    duration = finite_nonnegative_float(payload.get("duration") or 0, "duration")
    position = finite_nonnegative_float(payload.get("position") or 0, "position")
    total_plays = await asyncio.to_thread(save_player_history_sync, song_id, duration, position)
    return {"status":"ok", "all_play_count": total_plays}


@app.get("/api/library/recent-most")
async def api_recent_most():
    library=await build_library()
    by_id={s["id"]:s for s in library["songs"]}
    recent, most = await asyncio.to_thread(_recent_most_sync)
    def pack(rows):
        out=[]
        for r in rows:
            song=by_id.get(r[0])
            if not song: continue
            rel=str(song["path"].relative_to(DOWNLOAD_DIR)); enc=urllib.parse.quote(rel,safe="/")
            out.append({"id":song["id"],"title":song["title"],"artist":song["artist"],"album":song["album"],"duration":song["duration"],"plays":int(r[1]),"cover":"/api/library/cover/"+enc,"stream":"/api/library/stream/"+enc})
        return out
    return {"recent":pack(recent),"most_played":pack(most)}


@app.get("/api/playlists")
async def api_playlists():
    rows = await asyncio.to_thread(_playlist_rows_sync)
    library=await build_library(); by_id={s["id"]:s for s in library["songs"]}
    out=[]
    for row in rows:
        p=_playlist_row_to_dict(row)
        ids=p["song_ids"]
        if p["kind"]=="smart":
            rules=p["rules"]
            filtered=list(library["songs"])
            if rules.get("genre"): filtered=[x for x in filtered if str(x.get("genre","" )).lower()==str(rules["genre"]).lower()]
            if rules.get("artist"): filtered=[x for x in filtered if str(x.get("artist","" )).lower()==str(rules["artist"]).lower()]
            if rules.get("year"): filtered=[x for x in filtered if str(x.get("year",""))==str(rules["year"])]
            ids=[x["id"] for x in filtered]
        out.append({"id":p["id"],"name":p["name"],"comment":p.get("comment","") ,"kind":p["kind"],"rules":p["rules"],"song_ids":[i for i in ids if i in by_id],"song_count":len([i for i in ids if i in by_id]),"created_at":p.get("created_at"),"updated_at":p.get("updated_at")})
    return out


@app.post("/api/playlists")
async def api_playlist_create(payload: dict = Body(...)):
    name = clean_metadata_text(payload.get("name"), "New Playlist")
    if not name:
        raise HTTPException(400, "Playlist name required")
    raw_ids = payload.get("song_ids") or []
    if not isinstance(raw_ids, list):
        raise HTTPException(400, "song_ids must be an array")
    ids = [str(x)[:512] for x in raw_ids[:MAX_PLAYLIST_SONGS] if x is not None]
    kind = "smart" if payload.get("kind") == "smart" else "manual"
    rules = payload.get("rules") or {}
    if not isinstance(rules, dict):
        raise HTTPException(400, "Playlist rules must be an object")
    comment = str(payload.get("comment") or "").strip()[:1000]
    try:
        rules_json = json.dumps(rules, ensure_ascii=False, separators=(",", ":"))
        ids_json = json.dumps(ids, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "Playlist contains invalid data") from exc
    if len(rules_json) > 16384:
        raise HTTPException(400, "Playlist rules are too large")
    now = time.time(); pid = "playlist-" + uuid.uuid4().hex[:16]
    await asyncio.to_thread(_playlist_create_sync, (pid, name, comment, "admin", 0, ids_json, now, now, kind, rules_json))
    return {"status":"ok","id":pid}


@app.put("/api/playlists/{playlist_id}")
async def api_playlist_update(playlist_id:str,payload:dict=Body(...)):
    rows = await asyncio.to_thread(_playlist_rows_sync)
    row = next((r for r in rows if str(r["id"]) == playlist_id), None)
    if not row:
        raise HTTPException(404,"Playlist not found")
    current = dict(row)
    name = clean_metadata_text(payload.get("name", current["name"]), "New Playlist")
    raw_ids = payload.get("song_ids", safe_song_ids(current.get("song_ids","[]")))
    if not isinstance(raw_ids, list):
        raise HTTPException(400, "song_ids must be an array")
    ids = [str(x)[:512] for x in raw_ids[:MAX_PLAYLIST_SONGS] if x is not None]
    kind = payload.get("kind", current.get("kind", "manual"))
    if "rules" in payload:
        rules = payload.get("rules") or {}
    else:
        try:
            rules = json.loads(current.get("rules") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            rules = {}
    if kind not in {"manual", "smart"}:
        raise HTTPException(400, "Invalid playlist kind")
    if not isinstance(rules, dict):
        raise HTTPException(400, "Playlist rules must be an object")
    comment = str(payload.get("comment", current.get("comment") or "")).strip()[:1000]
    try:
        ids_json = json.dumps(ids, ensure_ascii=False, separators=(",", ":"))
        rules_json = json.dumps(rules, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "Playlist contains invalid data") from exc
    if len(rules_json) > 16384:
        raise HTTPException(400, "Playlist rules are too large")
    updated = await asyncio.to_thread(_playlist_update_sync, (name, comment, ids_json, time.time(), kind, rules_json, playlist_id))
    if not updated:
        raise HTTPException(404,"Playlist not found")
    return {"status":"ok"}


@app.delete("/api/playlists/{playlist_id}")
async def api_playlist_delete(playlist_id:str):
    await asyncio.to_thread(_playlist_delete_sync, playlist_id)
    return {"status":"ok"}


@app.get("/api/playlists/{playlist_id}")
async def api_playlist_get(playlist_id:str):
    allp=await api_playlists()
    for p in allp:
        if p["id"]==playlist_id:
            library=await build_library(); by_id={s["id"]:s for s in library["songs"]}
            tracks=[]
            for sid in p["song_ids"]:
                s=by_id.get(sid)
                if s:
                    rel=str(s["path"].relative_to(DOWNLOAD_DIR)); enc=urllib.parse.quote(rel,safe="/")
                    tracks.append({"id":s["id"],"title":s["title"],"artist":s["artist"],"album":s["album"],"duration":s["duration"],"cover":"/api/library/cover/"+enc,"stream":"/api/library/stream/"+enc})
            p["tracks"]=tracks
            return p
    raise HTTPException(404,"Playlist not found")


@app.get("/api/library/health")
async def api_library_health():
    # Reuse the normal library index. Health checks must not trigger ffmpeg for
    # every track or block the event loop with repeated filesystem calls.
    library = await build_library()
    songs = library.get("songs", [])
    bad_tags, missing_art, groups = [], [], {}
    for song in songs:
        rel = str(song["path"].relative_to(DOWNLOAD_DIR))
        title = str(song.get("title") or "").strip()
        artist = str(song.get("artist") or "").strip()
        album = str(song.get("album") or "").strip()
        title_missing = not title or title.casefold() in {"unknown", "unknown track"}
        artist_missing = not artist or artist.casefold() in {"unknown", "unknown artist"}
        album_missing = not album or album.casefold() in {"unknown", "unknown album"}
        if title_missing or artist_missing or album_missing:
            bad_tags.append({"path": rel, "title": title, "artist": artist, "album": album})
        if not song.get("has_artwork"):
            missing_art.append(rel)
        key = normalize_duplicate_key(title or Path(rel).stem, artist or "Unknown Artist")
        if key:
            groups.setdefault(key, []).append({
                "path": rel, "id": song["id"], "title": title or Path(rel).stem,
                "artist": artist or "Unknown Artist", "album": album or "Unknown Album",
                "size": safe_int(song.get("size"), 0), "duration": safe_float(song.get("duration"), 0),
            })
    duplicates = [
        {"key": key, "title": files[0].get("title", "") if files else "",
         "artist": files[0].get("artist", "") if files else "", "files": files}
        for key, files in groups.items() if len(files) > 1
    ]
    return {
        "unreadable": [],
        "bad_tags": bad_tags,
        "missing_artwork": missing_art,
        "duplicates": duplicates,
        "counts": {
            "unreadable": 0, "bad_tags": len(bad_tags),
            "missing_artwork": len(missing_art), "duplicates": len(duplicates),
            "duplicate_files": sum(len(item["files"]) for item in duplicates),
        },
    }


@app.post("/api/library/scan/{mode}")
async def api_library_scan_mode(mode: str):
    if mode not in {"quick", "full"}:
        raise HTTPException(400, "mode must be quick or full")
    if LIBRARY_SCAN_LOCK.locked():
        raise HTTPException(409, "A library scan is already running")

    async with LIBRARY_SCAN_LOCK:
        await asyncio.to_thread(_scan_state_sync, "running", mode, "Scanning", time.time())
        try:
            invalidate_library_cache()
            # build_library already uses the on-disk index and only reads tags for
            # new/changed files. Metadata reads are performed concurrently.
            library = await build_library(force=True)
            if mode == "full":
                cover_tasks = [ensure_cover(song["path"]) for song in library["songs"]]
                if cover_tasks:
                    semaphore = asyncio.Semaphore(8)

                    async def cover_one(coro):
                        async with semaphore:
                            try:
                                return await coro
                            except Exception:
                                return None

                    await asyncio.gather(*(cover_one(c) for c in cover_tasks), return_exceptions=True)
            await persist_library_index(library)
            await asyncio.to_thread(_scan_state_sync, "ok", None, f"{len(library['songs'])} tracks scanned")
            return {"status": "ok", "mode": mode, "tracks": len(library["songs"])}
        except Exception as exc:
            await write_app_error("library_scan", str(exc))
            await asyncio.to_thread(_scan_state_sync, "error", None, str(exc))
            raise


@app.get("/api/library/scan/status")
async def api_library_scan_status():
    return await asyncio.to_thread(_scan_state_read_sync)


@app.post("/api/auth/login")
async def api_auth_login(request: Request, payload: dict = Body(...)):
    if _login_blocked(request):
        raise HTTPException(429, "Too many failed sign-in attempts. Try again in a few minutes.")
    user = str(payload.get("username") or "")[:64]
    password = str(payload.get("password") or "")[:256]
    def verify_login_sync():
        expected, credential = _current_web_credentials()
        ok, legacy = _verify_web_password(password, credential)
        return expected, ok, legacy

    expected_user, password_ok, was_legacy_plaintext = await asyncio.to_thread(verify_login_sync)
    if not secrets.compare_digest(user, expected_user) or not password_ok:
        _record_login_failure(request)
        raise HTTPException(401, "Invalid username or password")
    _clear_login_failures(request)
    if was_legacy_plaintext and not os.getenv("XROB_PASSWORD"):
        settings = await load_settings_async()
        settings["web_password_hash"] = _hash_web_password(password)
        settings.pop("web_password", None)
        await asyncio.to_thread(_write_settings_sync, settings)
    token = _auth_token()
    now = time.time()
    AUTH_SESSIONS[token] = {"created": now, "last_seen": now, "username": expected_user}
    response = JSONResponse({"status":"ok", "username":expected_user})
    response.set_cookie(
        AUTH_COOKIE, token, httponly=True, samesite="lax",
        secure=request.url.scheme == "https", path="/"
    )
    return response

@app.get("/api/auth/status")
async def api_auth_status(request: Request):
    authenticated = _is_authenticated(request.cookies.get(AUTH_COOKIE))
    expected_user, _ = await asyncio.to_thread(_current_web_credentials)
    return {"authenticated": authenticated, "username": expected_user if authenticated else None}

@app.post("/api/auth/logout")
async def api_auth_logout(request: Request):
    token=request.cookies.get(AUTH_COOKIE)
    if token: AUTH_SESSIONS.pop(token, None)
    response=JSONResponse({"status":"ok"}); response.delete_cookie(AUTH_COOKIE, path="/"); return response

@app.get("/api/diagnostics")
async def api_diagnostics():
    def check_sync():
        checks = {}
        try:
            with db_connect() as conn:
                integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0] or "")
                foreign = int(conn.execute("PRAGMA foreign_keys").fetchone()[0] or 0)
                checks["database"] = {"ok": integrity.lower() == "ok", "integrity": integrity, "foreign_keys": bool(foreign)}
        except Exception as exc:
            checks["database"] = {"ok": False, "integrity": str(exc), "foreign_keys": False}
        try:
            usage = shutil.disk_usage(DOWNLOAD_DIR)
            checks["storage"] = {"ok": DOWNLOAD_DIR.exists() and os.access(DOWNLOAD_DIR, os.W_OK), "path": str(DOWNLOAD_DIR), "free_bytes": usage.free, "total_bytes": usage.total}
        except Exception as exc:
            checks["storage"] = {"ok": False, "path": str(DOWNLOAD_DIR), "error": str(exc)}
        return checks
    checks = await asyncio.to_thread(check_sync)
    tools = {}
    for name, command in (("python", [sys.executable, "--version"]), ("ffmpeg", [FFMPEG_COMMAND[0], "-version"]), ("yt_dlp", [*YT_DLP_COMMAND, "--version"])):
        try:
            proc = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
            first = out.decode("utf-8", errors="ignore").splitlines()[0] if out else ""
            tools[name] = {"ok": proc.returncode == 0, "version": first[:200]}
        except Exception as exc:
            tools[name] = {"ok": False, "error": str(exc)}
    checks["tools"] = tools
    checks["runtime"] = {"server_version": SERVER_VERSION, "tasks": len(TASKS), "active_processes": len(ACTIVE_PROCESSES), "websocket_connections": len(manager.connections) if hasattr(manager, "connections") else 0}
    return checks

@app.get("/api/backup")
async def api_backup():
    await asyncio.to_thread(init_db)
    settings = await asyncio.to_thread(load_settings)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    tmp = Path(tempfile.gettempdir()) / f"xrob-music-backup-{stamp}-{uuid.uuid4().hex[:6]}.zip"
    def build_backup():
        with db_connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(FULL)")
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(DB_FILE, "tasks.db")
            if SETTINGS_FILE.exists():
                archive.write(SETTINGS_FILE, "settings.json")
            if LIBRARY_INDEX_FILE.exists():
                archive.write(LIBRARY_INDEX_FILE, "library_index.json")
            archive.writestr("manifest.json", json.dumps({"server_version": SERVER_VERSION, "created_at": time.time(), "database_schema": DB_SCHEMA_VERSION, "settings_fields": sorted(settings.keys())}, indent=2))
        return tmp
    try:
        path = await asyncio.to_thread(build_backup)
        def cleanup():
            try: path.unlink(missing_ok=True)
            except Exception: pass
        return FileResponse(path, media_type="application/zip", filename=f"xrob-music-backup-{stamp}.zip", background=BackgroundTask(cleanup))
    except Exception as exc:
        await write_app_error("backup", str(exc))
        raise HTTPException(500, f"Backup failed: {exc}")

@app.post("/api/restore")
async def api_restore(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(400, "A .zip Xrob Music backup is required")
    raw = await file.read()
    if len(raw) > 64 * 1024 * 1024:
        raise HTTPException(413, "Backup is too large")
    temp = Path(tempfile.gettempdir()) / f"xrob-restore-{uuid.uuid4().hex}.zip"
    db_tmp = None
    try:
        await asyncio.to_thread(temp.write_bytes, raw)
        def validate_zip():
            with zipfile.ZipFile(temp, "r") as z:
                names=set(z.namelist())
                if "tasks.db" not in names:
                    raise ValueError("Backup does not contain tasks.db")
                if any(name.startswith("/") or ".." in Path(name).parts for name in names):
                    raise ValueError("Backup contains unsafe paths")
                target = Path(tempfile.gettempdir()) / f"xrob-restore-db-{uuid.uuid4().hex}.db"
                with z.open("tasks.db") as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                test = sqlite3.connect(target, timeout=10.0)
                try:
                    test.execute("PRAGMA foreign_keys = ON")
                    ok=str(test.execute("PRAGMA integrity_check").fetchone()[0] or "")
                    if ok.lower() != "ok": raise ValueError(f"Database integrity check failed: {ok}")
                finally: test.close()
                settings_payload = z.read("settings.json") if "settings.json" in names else None
                if settings_payload is not None:
                    try:
                        decoded = json.loads(settings_payload.decode("utf-8"))
                        if not isinstance(decoded, dict):
                            raise ValueError("settings.json must contain an object")
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise ValueError(f"Invalid settings.json: {exc}") from exc
                index_payload = z.read("library_index.json") if "library_index.json" in names else None
                if index_payload is not None:
                    try:
                        decoded = json.loads(index_payload.decode("utf-8"))
                        if not isinstance(decoded, (dict, list)):
                            raise ValueError("library_index.json must contain an object or array")
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise ValueError(f"Invalid library_index.json: {exc}") from exc
                return target, settings_payload, index_payload
        db_tmp, settings_bytes, index_bytes = await asyncio.to_thread(validate_zip)
        stamp=time.strftime("%Y%m%d-%H%M%S", time.localtime())
        safety=DB_FILE.with_name(f"tasks.db.before-restore-{stamp}-{uuid.uuid4().hex[:6]}")
        if DB_FILE.exists():
            await asyncio.to_thread(shutil.copy2, DB_FILE, safety)
        await asyncio.to_thread(shutil.copy2, db_tmp, DB_FILE)
        # Run the current migration set against the restored database before exposing
        # it to the application. This keeps older valid backups forward-compatible.
        await asyncio.to_thread(init_db)
        if settings_bytes is not None:
            await asyncio.to_thread(SETTINGS_FILE.write_bytes, settings_bytes)
        if index_bytes is not None:
            await asyncio.to_thread(LIBRARY_INDEX_FILE.write_bytes, index_bytes)
        global TASKS, LIBRARY_CACHE, LIBRARY_CACHE_TIME
        TASKS = await asyncio.to_thread(db_load_tasks_sync)
        LIBRARY_CACHE=None; LIBRARY_CACHE_TIME=0.0
        return {"status":"restored", "tasks":len(TASKS), "safety_backup":str(safety) if safety.exists() else ""}
    except (zipfile.BadZipFile, ValueError) as exc:
        await write_app_error("restore", str(exc))
        raise HTTPException(400, f"Restore rejected: {exc}")
    except Exception as exc:
        await write_app_error("restore", str(exc))
        raise HTTPException(500, f"Restore failed: {exc}")
    finally:
        try: temp.unlink(missing_ok=True)
        except Exception: pass
        if db_tmp is not None:
            try: db_tmp.unlink(missing_ok=True)
            except Exception: pass

@app.get("/api/errors")
async def api_errors(limit:int=Query(200,ge=1,le=1000)):
    if not isinstance(limit, int):
        limit = 200
    limit = max(1, min(1000, limit))
    rows = await asyncio.to_thread(_errors_sync, limit)
    failures=[]
    for task in sorted(TASKS.values(), key=lambda x:x.get("last_updated",0), reverse=True):
        if task.get("status")=="failed" or task.get("error"):
            failures.append({"created_at":task.get("last_updated",0)/1000 if task.get("last_updated",0)>10000000000 else task.get("last_updated",0),"source":"download","message":task.get("error") or "Download failed","task_id":task.get("id"),"title":task.get("title")})
    return {"errors":[dict(r) for r in rows]+failures[:limit]}


@app.post("/api/library/metadata")
async def api_library_metadata(payload: dict = Body(...)):
    song_id=str(payload.get("id") or "")
    song=await find_song(song_id)
    if not song: raise HTTPException(404,"Track not found")
    path = song["path"]
    fields = {}
    for key in ("title", "artist", "album"):
        if key in payload:
            value = clean_metadata_text(payload.get(key), "")
            fields[key] = value
    if not fields:
        return {"status":"ok"}
    if MutagenFile is None: raise HTTPException(500,"Metadata library unavailable")
    def write_tags():
        audio=MutagenFile(path, easy=False)
        if audio is None: raise RuntimeError("Unsupported audio file")
        ext=path.suffix.lower()
        title=str(fields.get("title",song["title"]))
        artist=str(fields.get("artist",song["artist"]))
        album=str(fields.get("album",song["album"]))
        if ext==".mp3":
            try: audio.add_tags()
            except Exception: pass
            if audio.tags is None: audio.tags=ID3()
            audio.tags.delall("TIT2"); audio.tags.delall("TPE1"); audio.tags.delall("TALB")
            audio.tags.add(TIT2(encoding=3,text=title)); audio.tags.add(TPE1(encoding=3,text=artist)); audio.tags.add(TALB(encoding=3,text=album))
        else:
            tags=audio.tags or {}
            for key,val in (("title",title),("artist",artist),("album",album)):
                if val: tags[key]=[val]
                else: tags.pop(key,None)
            audio.tags=tags
        audio.save()
    try: await asyncio.to_thread(write_tags)
    except Exception as exc: await write_app_error("metadata",str(exc)); raise HTTPException(500,f"Metadata update failed: {exc}")
    edited_at = time.time()
    await asyncio.to_thread(_mark_song_review_sync, song_id, "edited", edited_at, True)
    invalidate_library_cache(); return {"status":"ok"}


@app.get("/api/song-editor")
async def api_song_editor():
    library = await build_library()
    songs = library["songs"]
    songs_by_id = {s["id"]: s for s in songs}

    # song_review is only the CURRENT review queue. song_editor_history is the
    # permanent list of tracks that have been edited and can therefore be reopened.
    review_rows, history_rows = await asyncio.to_thread(_song_editor_snapshot_sync, [s["id"] for s in songs])

    def make_item(s):
        rel = str(s["path"].relative_to(DOWNLOAD_DIR))
        enc = urllib.parse.quote(rel, safe="/")
        return {
            "id": s["id"],
            "title": s["title"],
            "artist": s["artist"],
            "album": s["album"],
            "name": rel,
            "duration": s.get("duration", 0),
            "cover": "/api/library/cover/" + enc,
            "stream": "/api/library/stream/" + enc,
        }

    out = []
    for row in review_rows:
        song = songs_by_id.get(row["song_id"])
        if song and row["state"] == "pending":
            out.append(make_item(song))

    edited = []
    for row in history_rows:
        song = songs_by_id.get(row["song_id"])
        if song:
            item = make_item(song)
            item["edited_at"] = float(row["edited_at"] or 0)
            edited.append(item)

    return {
        "tracks": out,
        "count": len(out),
        "edited_tracks": edited,
        "recently_edited_tracks": edited,
        "edited_count": len(edited),
    }

@app.post("/api/song-editor/reset")
async def api_song_editor_reset():
    library = await build_library()
    ids = [s["id"] for s in library["songs"]]
    # Reset ONLY the current review queue. Never erase song_editor_history.
    await asyncio.to_thread(_reset_song_editor_sync, ids)
    return {"status": "ok", "count": len(ids)}


@app.post("/api/song-editor/{song_id}/import")
async def api_song_editor_import(song_id: str):
    song = await find_song(song_id)
    if not song:
        raise HTTPException(404, "Track not found")
    await asyncio.to_thread(_mark_song_review_sync, song_id, "pending", 0.0)
    return {"status": "ok", "count": 1}


@app.post("/api/song-editor/{song_id}/skip")
async def api_song_editor_skip(song_id:str):
    song=await find_song(song_id)
    if not song: raise HTTPException(404,"Track not found")
    await asyncio.to_thread(_mark_song_review_sync, song_id, "skipped", time.time())
    return {"status":"ok"}


@app.get("/api/library/artist-artwork/{artist_id}")
async def api_artist_artwork(artist_id: str):
    row = await asyncio.to_thread(_artist_artwork_get_sync, artist_id)
    if not row: raise HTTPException(404, "Artist artwork not found")
    return Response(content=row[0], media_type=row[1])

@app.post("/api/library/artist-artwork/{artist_id}")
async def api_artist_artwork_upload(artist_id: str, upload: UploadFile = File(...)):
    data = await upload.read()
    if not data or len(data) > 15 * 1024 * 1024: raise HTTPException(400, "Invalid artwork")
    mime = upload.content_type or "image/jpeg"
    if mime not in {"image/jpeg","image/png","image/webp"}: raise HTTPException(400, "Use JPEG, PNG or WebP artwork")
    await asyncio.to_thread(_artist_artwork_save_sync, artist_id, data, mime)
    invalidate_library_cache()
    return {"status":"ok","artist_id":artist_id}

@app.post("/api/library/artwork/{song_id}")
async def api_library_artwork(song_id:str, upload:UploadFile=File(...)):
    song=await find_song(song_id)
    if not song: raise HTTPException(404,"Track not found")
    data=await upload.read()
    if not data or len(data)>15*1024*1024: raise HTTPException(400,"Invalid artwork")
    mime=upload.content_type or "image/jpeg"
    if mime not in {"image/jpeg", "image/png", "image/webp"}: raise HTTPException(400,"Use JPEG, PNG or WebP artwork")
    if MutagenFile is None: raise HTTPException(500,"Metadata library unavailable")
    def write_art():
        audio=MutagenFile(song["path"], easy=False)
        ext=song["path"].suffix.lower()
        if ext==".mp3":
            if audio.tags is None: audio.add_tags()
            audio.tags.delall("APIC")
            audio.tags.add(APIC(mime=mime,type=3,desc="Cover",data=data))
        elif ext==".flac" and Picture:
            pic=Picture(); pic.type=3; pic.mime=mime; pic.data=data; audio.clear_pictures(); audio.add_picture(pic); audio.save()
            return
        elif ext in {".m4a",".mp4"}:
            from mutagen.mp4 import MP4Cover
            audio["covr"]=[MP4Cover(data,imageformat=MP4Cover.FORMAT_PNG if "png" in mime else MP4Cover.FORMAT_JPEG)]
        else:
            raise RuntimeError("Embedded artwork is not supported for this format")
        audio.save()
    try: await asyncio.to_thread(write_art)
    except Exception as exc: await write_app_error("artwork",str(exc)); raise HTTPException(500,f"Artwork update failed: {exc}")
    invalidate_library_cache(); return {"status":"ok"}


async def scheduled_library_scanner():
    while True:
        try:
            settings=await load_settings_async()
            enabled = bool(settings.get("scan_enabled", True))
            try:
                minutes = int(settings.get("scan_interval_minutes", 60) or 60)
            except (TypeError, ValueError):
                minutes = 60
            minutes = max(5, min(10080, minutes))
            if enabled:
                await asyncio.sleep(minutes*60)
                try:
                    await api_library_scan_mode("quick")
                except HTTPException as exc:
                    if exc.status_code != 409:
                        raise
            else:
                await asyncio.sleep(300)
        except asyncio.CancelledError: return
        except Exception as exc:
            await write_app_error("scheduled_scan",str(exc)); await asyncio.sleep(300)


# ============================================================
# SCROBBLE
# ============================================================

@app.get("/rest/scrobble.view")
@app.get("/rest/scrobble")
async def rest_scrobble(
    request: Request,
    id: str = Query(""),
    submission: bool = Query(True),
):

    error = require_auth(request)

    if error:
        return error

    return make_subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "type": "Xrob Music",
        },
        request,
    )


# ============================================================
# NOW PLAYING
# ============================================================

@app.get(
    "/rest/getNowPlaying.view"
)
@app.get(
    "/rest/getNowPlaying"
)
async def rest_now_playing(
    request: Request,
):

    error = require_auth(request)

    if error:
        return error

    return make_subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "type": "Xrob Music",
            "nowPlaying": {
                "entry": [],
            },
        },
        request,
    )


# ============================================================
# SIMILAR SONGS
# ============================================================

@app.get(
    "/rest/getSimilarSongs2.view"
)
@app.get(
    "/rest/getSimilarSongs2"
)
async def rest_similar_songs(
    request: Request,
    id: str = Query(...),
    count: int = Query(20),
):

    error = require_auth(request)

    if error:
        return error

    target = await find_song(id)

    if not target:

        return subsonic_error(
            request,
            70,
            "Song not found.",
        )

    library = await build_library()

    similar = [
        song
        for song in library[
            "songs"
        ]
        if (
            song["id"] != id
            and song["artist"]
            == target["artist"]
        )
    ]

    return make_subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "type": "Xrob Music",
            "similarSongs2": {
                "song": await songs_to_subsonic_async(similar[:_bounded_subsonic_int(count, 20, 500)]),
            },
        },
        request,
    )


# ============================================================
# LYRICS
# ============================================================

@app.get(
    "/rest/getLyricsBySongId.view"
)
@app.get(
    "/rest/getLyricsBySongId"
)
async def rest_lyrics(
    request: Request,
    id: str = Query(""),
):

    error = require_auth(request)

    if error:
        return error

    return make_subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "type": "Xrob Music",
            "lyricsList": {
                "structuredLyrics": [],
            },
        },
        request,
    )


# ============================================================
# END
# ============================================================
