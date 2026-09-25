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
import urllib.error
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
    Form,
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
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from .catalog import LibraryCatalog, StorageUnavailable, strong_file_hash


# ============================================================
# XROB MUSIC
# Downloader + Library + OpenSubsonic server
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
SERVER_VERSION = "4.3.7"

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
        if LIBRARY_HEALTH_TASK is not None:
            tasks.append(LIBRARY_HEALTH_TASK)
        if LIBRARY_REFRESH_TASK is not None:
            tasks.append(LIBRARY_REFRESH_TASK)
        if RUNTIME_MAINTENANCE_TASK is not None:
            tasks.append(RUNTIME_MAINTENANCE_TASK)
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


@app.exception_handler(StorageUnavailable)
async def storage_unavailable_handler(_request: Request, exc: StorageUnavailable):
    return JSONResponse(
        status_code=503,
        content={"detail": _sanitize_external_error(exc, "Music storage is unavailable."), "storage_state": "offline"},
    )

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    safe = _sanitize_external_error(exc, "Internal server error.")
    try:
        await write_app_error("unhandled_exception", safe)
    except Exception:
        pass
    return JSONResponse(status_code=500, content={"detail": safe})

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
AUTH_SESSION_IDLE_SECONDS = 12 * 60 * 60
AUTH_SESSION_ABSOLUTE_SECONDS = 30 * 24 * 60 * 60
AUTH_SESSION_MAX = 32
AUTH_SESSION_CLEANUP_INTERVAL = 60.0
AUTH_SESSION_LAST_CLEANUP = 0.0
AUTH_LOGIN_ATTEMPTS = defaultdict(list)
AUTH_LOGIN_WINDOW = 300
AUTH_LOGIN_MAX_ATTEMPTS = 5
SUBSONIC_RATE_LIMIT = (240, 60.0)
SUBSONIC_FAILURE_LIMIT = (10, 300.0)
SUBSONIC_FAILURE_STATE = defaultdict(list)
SUBSONIC_FAILURE_LAST_CLEANUP = 0.0
# Lightweight per-client API throttling for expensive external-provider operations.
RATE_LIMIT_STATE = defaultdict(list)
RATE_LIMIT_LOCK = asyncio.Lock()
SEARCH_RATE_LIMIT = (30, 60.0)   # requests / rolling window / client
PREVIEW_RATE_LIMIT = (12, 60.0)  # requests / rolling window / client
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


def _sanitize_external_error(message, fallback="Operation failed.", max_length=600):
    """Return user-safe diagnostics without exposing URLs, credentials, or local paths."""
    text = str(message or "").replace("\x00", " ")
    text = re.sub(r"https?://\S+", "[external-url]", text, flags=re.I)
    text = re.sub(r"(?i)(password|passwd|token|secret|api[_ -]?key)\s*[:=]\s*[^\s,;]+", r"\1=[redacted]", text)
    text = re.sub(r"(?i)(?:/data|/media|/share|/config|/root|/app)(?:[/\\][^\s,;]*)*", "[local-path]", text)
    text = re.sub(r"(?i)cookie=[^\s;]+", "cookie=[redacted]", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text[:max_length]
    return text or fallback


def _cleanup_auth_sessions(now=None, force=False):
    global AUTH_SESSION_LAST_CLEANUP
    now = float(now or time.time())
    if not force and now - AUTH_SESSION_LAST_CLEANUP < AUTH_SESSION_CLEANUP_INTERVAL and len(AUTH_SESSIONS) <= AUTH_SESSION_MAX:
        return
    expired = []
    for token, session in list(AUTH_SESSIONS.items()):
        created = float(session.get("created") or 0)
        last_seen = float(session.get("last_seen") or created)
        if now - created >= AUTH_SESSION_ABSOLUTE_SECONDS or now - last_seen >= AUTH_SESSION_IDLE_SECONDS:
            expired.append(token)
    for token in expired:
        AUTH_SESSIONS.pop(token, None)
    if len(AUTH_SESSIONS) > AUTH_SESSION_MAX:
        ordered = sorted(AUTH_SESSIONS.items(), key=lambda item: float(item[1].get("last_seen") or 0))
        for token, _session in ordered[: len(AUTH_SESSIONS) - AUTH_SESSION_MAX]:
            AUTH_SESSIONS.pop(token, None)
    AUTH_SESSION_LAST_CLEANUP = now


def _approved_download_roots():
    """Return canonical approved download roots, all constrained to DOWNLOAD_DIR."""
    base = DOWNLOAD_DIR.resolve()
    roots = [base]
    raw_roots = os.getenv("XROB_APPROVED_DOWNLOAD_ROOTS", "")
    for raw in raw_roots.split(","):
        raw = str(raw).strip()
        if not raw:
            continue
        try:
            candidate = Path(raw).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if candidate == base or base in candidate.parents:
            roots.append(candidate)
    unique=[]
    seen=set()
    for path in roots:
        key=str(path)
        if key not in seen:
            seen.add(key); unique.append(path)
    return unique

def _resolve_approved_download_root(raw_location, allow_empty=True):
    raw = str(raw_location or "").strip()
    if not raw:
        if allow_empty:
            return DOWNLOAD_DIR.resolve()
        raise ValueError("Download location is required.")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ValueError("Download location must be an absolute path.")
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("Download location could not be resolved.") from exc
    base = DOWNLOAD_DIR.resolve()
    if not (resolved == base or base in resolved.parents):
        raise ValueError("Download location must be inside the configured music library root.")
    if not any(resolved == root or root in resolved.parents for root in _approved_download_roots()):
        raise ValueError("Download location must be inside an approved music storage root.")
    if candidate.exists() and not candidate.is_dir():
        raise ValueError("Download location must be a directory.")
    if not candidate.exists():
        # The canonical base may be temporarily absent while the NAS is offline or
        # while Home Assistant is preparing the mount. Keep the configured base as
        # a valid setting; custom roots still fail closed until they exist.
        if resolved != base:
            raise ValueError("Download location does not exist. Mount or create it first.")
    return resolved

def _validate_download_location(raw_location):
    return str(_resolve_approved_download_root(raw_location, allow_empty=False))

def _download_root_for_settings(settings):
    return _resolve_approved_download_root(str((settings or {}).get("download_location") or ""), allow_empty=True)

def task_download_root(task):
    """Resolve a persisted task root through the same canonical policy as settings."""
    return _resolve_approved_download_root(str((task or {}).get("download_root") or ""), allow_empty=True)

def cleanup_task_files(task_id, root=None):
    try:
        resolved_root = _resolve_approved_download_root(str(root or ""), allow_empty=True)
    except (ValueError, RuntimeError):
        return
    try:
        for path in resolved_root.rglob(f"*{task_id}*"):
            try:
                if path.is_file():
                    path.unlink()
            except OSError:
                pass
    except OSError:
        pass

def _validate_image_payload(data, declared_mime="", max_bytes=15 * 1024 * 1024):
    if not data or len(data) > max_bytes:
        raise ValueError("Invalid artwork.")
    declared = str(declared_mime or "").split(";", 1)[0].strip().lower()
    width = height = None
    actual = None
    if data.startswith(b"\xff\xd8\xff"):
        actual = "image/jpeg"
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            while i < len(data) and data[i] == 0xFF:
                i += 1
            if i >= len(data): break
            marker = data[i]; i += 1
            if marker in {0xD8, 0xD9}: continue
            if i + 2 > len(data): break
            seg_len = int.from_bytes(data[i:i+2], "big")
            if seg_len < 2 or i + seg_len > len(data): break
            if marker in {0xC0,0xC1,0xC2,0xC3,0xC5,0xC6,0xC7,0xC9,0xCA,0xCB,0xCD,0xCE,0xCF} and seg_len >= 7:
                height = int.from_bytes(data[i+3:i+5], "big")
                width = int.from_bytes(data[i+5:i+7], "big")
                break
            i += seg_len
    elif data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24 and data[12:16] == b"IHDR":
        actual = "image/png"
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
    elif data.startswith(b"RIFF") and len(data) >= 16 and data[8:12] == b"WEBP":
        actual = "image/webp"
        chunk = data[12:16]
        if chunk == b"VP8X" and len(data) >= 30:
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
        elif chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
            width = int.from_bytes(data[26:28], "little") & 0x3fff
            height = int.from_bytes(data[28:30], "little") & 0x3fff
        elif chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2f:
            bits = int.from_bytes(data[21:25], "little")
            width = (bits & 0x3fff) + 1
            height = ((bits >> 14) & 0x3fff) + 1
    if actual is None:
        raise ValueError("Artwork is not a valid JPEG, PNG, or WebP image.")
    if declared and declared not in {"application/octet-stream", actual}:
        raise ValueError("Artwork MIME type does not match its image data.")
    if not width or not height or width < 1 or height < 1:
        raise ValueError("Artwork dimensions could not be verified.")
    if width > 12000 or height > 12000 or width * height > 64_000_000:
        raise ValueError("Artwork dimensions are too large.")
    return actual, width, height


def _sanitized_backup_settings(settings, include_secrets=False):
    payload = dict(settings or {})
    if not include_secrets:
        for key in ("subsonic_user", "subsonic_password", "web_password", "web_password_hash"):
            payload.pop(key, None)
    return payload


def _backup_key(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, BACKUP_ENCRYPTION_ITERATIONS, dklen=32)


def _encrypt_backup_payload(payload, password):
    password = str(password or "")
    if len(password) < BACKUP_PASSWORD_MIN_LENGTH:
        raise ValueError(f"Backup password must be at least {BACKUP_PASSWORD_MIN_LENGTH} characters.")
    salt = secrets.token_bytes(BACKUP_ENCRYPTION_SALT_BYTES)
    nonce = secrets.token_bytes(BACKUP_ENCRYPTION_NONCE_BYTES)
    key = _backup_key(password, salt)
    ciphertext = AESGCM(key).encrypt(nonce, payload, BACKUP_ENCRYPTION_MAGIC)
    return BACKUP_ENCRYPTION_MAGIC + salt + nonce + ciphertext


def _decrypt_backup_payload(data, password):
    if not data.startswith(BACKUP_ENCRYPTION_MAGIC):
        return data, False
    if len(data) < len(BACKUP_ENCRYPTION_MAGIC) + BACKUP_ENCRYPTION_SALT_BYTES + BACKUP_ENCRYPTION_NONCE_BYTES + 16:
        raise ValueError("Encrypted backup is incomplete.")
    password = str(password or "")
    if len(password) < BACKUP_PASSWORD_MIN_LENGTH:
        raise ValueError(f"Encrypted backup password must be at least {BACKUP_PASSWORD_MIN_LENGTH} characters.")
    offset = len(BACKUP_ENCRYPTION_MAGIC)
    salt = data[offset:offset + BACKUP_ENCRYPTION_SALT_BYTES]
    offset += BACKUP_ENCRYPTION_SALT_BYTES
    nonce = data[offset:offset + BACKUP_ENCRYPTION_NONCE_BYTES]
    offset += BACKUP_ENCRYPTION_NONCE_BYTES
    ciphertext = data[offset:]
    try:
        payload = AESGCM(_backup_key(password, salt)).decrypt(nonce, ciphertext, BACKUP_ENCRYPTION_MAGIC)
    except Exception as exc:
        raise ValueError("Encrypted backup password is incorrect or the backup is corrupted.") from exc
    if len(payload) > MAX_RESTORE_UNCOMPRESSED_BYTES:
        raise ValueError("Decrypted backup is too large.")
    return payload, True


def _validate_backup_zip_bytes(raw_bytes):
    if len(raw_bytes) > MAX_RESTORE_UPLOAD_BYTES:
        raise ValueError("Backup is too large.")
    with zipfile.ZipFile(__import__('io').BytesIO(raw_bytes), "r") as z:
        infos = z.infolist()
        if len(infos) > MAX_RESTORE_ENTRIES:
            raise ValueError("Backup contains too many files.")
        total = 0
        for info in infos:
            name = info.filename
            if name.startswith("/") or ".." in Path(name).parts or "\\" in name:
                raise ValueError("Backup contains unsafe paths.")
            if info.is_dir():
                continue
            if info.file_size < 0 or info.file_size > MAX_RESTORE_UNCOMPRESSED_BYTES:
                raise ValueError("Backup contains an oversized file.")
            total += info.file_size
            if total > MAX_RESTORE_UNCOMPRESSED_BYTES:
                raise ValueError("Backup expands beyond the restore size limit.")
            if name == "tasks.db" and info.file_size > MAX_RESTORE_DB_BYTES:
                raise ValueError("Database exceeds the restore size limit.")
        if "tasks.db" not in z.namelist():
            raise ValueError("Backup does not contain tasks.db")
        return True

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

def _consume_bootstrap_after_successful_login_sync():
    try:
        AUTH_BOOTSTRAP_FILE.unlink(missing_ok=True)
    except OSError:
        pass

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


async def _enforce_rate_limit(request: Request, bucket: str, limit: int, window_seconds: float):
    now = time.monotonic()
    key = f"{bucket}:{_auth_client_key(request)}"
    async with RATE_LIMIT_LOCK:
        entries = [ts for ts in RATE_LIMIT_STATE.get(key, []) if now - ts < window_seconds]
        allowed = len(entries) < limit
        if allowed:
            entries.append(now)
        RATE_LIMIT_STATE[key] = entries
    if not allowed:
        retry_after = max(1, int(window_seconds - (now - entries[0]))) if entries else max(1, int(window_seconds))
        raise HTTPException(
            status_code=429,
            detail=f"Too many {bucket} requests. Please try again shortly.",
            headers={"Retry-After": str(retry_after)},
        )


def _cleanup_rate_limit_state_sync():
    mono_now = time.monotonic()
    wall_now = time.time()
    stale = [key for key, entries in RATE_LIMIT_STATE.items() if not entries or mono_now - entries[-1] >= 300]
    for key in stale:
        RATE_LIMIT_STATE.pop(key, None)
    stale_login = [key for key, entries in AUTH_LOGIN_ATTEMPTS.items() if not entries or wall_now - entries[-1] >= AUTH_LOGIN_WINDOW]
    for key in stale_login:
        AUTH_LOGIN_ATTEMPTS.pop(key, None)
    stale_subsonic = [key for key, entries in SUBSONIC_FAILURE_STATE.items() if not entries or mono_now - entries[-1] >= SUBSONIC_FAILURE_LIMIT[1]]
    for key in stale_subsonic:
        SUBSONIC_FAILURE_STATE.pop(key, None)


async def _retry_catalog_pending_tasks():
    """Retry durable file->catalog commits without re-downloading media."""
    pending = [
        task for task in list(TASKS.values())
        if task.get("status") == "catalog_pending" and task.get("catalog_pending_path")
    ]
    for task in pending[:8]:
        task_id = str(task.get("id") or "")
        path = Path(str(task.get("catalog_pending_path") or ""))
        try:
            root = task_download_root(task)
        except (OSError, RuntimeError, ValueError) as exc:
            safe = _sanitize_external_error(exc, "Pending download location is invalid.", 500)
            task["status"] = "error"
            task["step"] = "Library commit failed"
            task["error"] = safe
            task["catalog_pending_path"] = ""
            task["last_updated"] = time.time() * 1000
            await notify_task_update(task, force_save=True)
            identity = str(task.get("content_identity") or "")
            if identity:
                await _release_content_identity_reservations(identity)
            continue
        try:
            resolved = path.resolve()
            base = DOWNLOAD_DIR.resolve()
            if not (resolved == base or base in resolved.parents):
                raise ValueError("Pending catalog path is outside the music library root.")
        except (OSError, RuntimeError, ValueError) as exc:
            safe = _sanitize_external_error(exc, "Pending library file is invalid.", 500)
            task["status"] = "error"
            task["step"] = "Library commit failed"
            task["error"] = safe
            task["catalog_pending_path"] = ""
            task["last_updated"] = time.time() * 1000
            if task_id:
                await asyncio.to_thread(cleanup_task_files, task_id, root)
                await notify_task_update(task, force_save=True)
            continue
        if not await asyncio.to_thread(resolved.is_file):
            task["status"] = "error"
            task["step"] = "Library file missing"
            task["error"] = "The finalized download file is missing."
            task["catalog_pending_path"] = ""
            task["last_updated"] = time.time() * 1000
            await notify_task_update(task, force_save=True)
            identity = str(task.get("content_identity") or "")
            if identity:
                async with DOWNLOAD_IDENTITY_LOCK:
                    ACTIVE_CONTENT_DOWNLOADS.discard(identity)
            continue
        try:
            async with DOWNLOAD_GUARD:
                await refresh_after_download(resolved)
                task["catalog_pending_path"] = ""
                task["status"] = "completed"
                task["percent"] = 100
                task["speed"] = ""
                task["step"] = "Ready"
                task["error"] = ""
                task["last_updated"] = time.time() * 1000
                await notify_task_update(task, force_save=True)
            identity = str(task.get("content_identity") or "")
            if identity:
                async with DOWNLOAD_IDENTITY_LOCK:
                    ACTIVE_CONTENT_DOWNLOADS.discard(identity)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            safe = _sanitize_external_error(exc, "Library commit failed; retrying automatically.", 700)
            task["status"] = "catalog_pending"
            task["step"] = "Library commit pending"
            task["error"] = safe
            task["last_updated"] = time.time() * 1000
            await notify_task_update(task, force_save=True)


async def runtime_maintenance_loop():
    while True:
        try:
            await _retry_catalog_pending_tasks()
            await _retry_library_delete_intents()
            _cleanup_rate_limit_state_sync()
            _cleanup_auth_sessions(force=False)
            async with COVER_LOCKS_GUARD:
                stale_cover_keys = []
                for key, entry in COVER_LOCKS.items():
                    if isinstance(entry, dict) and int(entry.get("users") or 0) <= 0 and not entry.get("lock").locked():
                        stale_cover_keys.append(key)
                for key in stale_cover_keys:
                    COVER_LOCKS.pop(key, None)
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            await write_app_error("runtime_maintenance", _sanitize_external_error(exc, "Runtime maintenance failed."))
            await asyncio.sleep(60)


def _is_authenticated(token):
    if not token:
        _cleanup_auth_sessions()
        return False
    now = time.time()
    _cleanup_auth_sessions(now)
    session = AUTH_SESSIONS.get(token)
    if not session:
        return False
    created = float(session.get("created") or 0)
    last_seen = float(session.get("last_seen") or created)
    if now - created >= AUTH_SESSION_ABSOLUTE_SECONDS or now - last_seen >= AUTH_SESSION_IDLE_SECONDS:
        AUTH_SESSIONS.pop(token, None)
        return False
    session["last_seen"] = now
    return True


ADDON_OPTIONS_FILE = Path("/data/options.json")

SUBSONIC_VERSION = "1.16.1"

MAX_CONCURRENT_DOWNLOADS = 3
MAX_PENDING_DOWNLOADS = 500
MAX_DEVICE_STALE_SECONDS = 25.0
DEVICE_RETENTION_SECONDS = 60 * 60 * 24 * 30
DOWNLOAD_HISTORY_MAX_ROWS = 5000
TASK_TERMINAL_MAX_ROWS = 1000
TASK_TERMINAL_RETENTION_DAYS = 30
TASK_PRUNE_INTERVAL_SECONDS = 300.0
MAX_RESTORE_UPLOAD_BYTES = 64 * 1024 * 1024
MAX_RESTORE_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_RESTORE_DB_BYTES = 256 * 1024 * 1024
MAX_RESTORE_ENTRIES = 32
BACKUP_ENCRYPTION_MAGIC = b"XROBENC1"
BACKUP_ENCRYPTION_SALT_BYTES = 16
BACKUP_ENCRYPTION_NONCE_BYTES = 12
BACKUP_ENCRYPTION_ITERATIONS = 390000
BACKUP_PASSWORD_MIN_LENGTH = 12
LIBRARY_REFRESH_DEBOUNCE_SECONDS = 1.5
DB_SCHEMA_VERSION = 11
LIBRARY_HEALTH_INTERVAL_SECONDS = 6 * 60 * 60
LIBRARY_HEALTH_MAX_FILES_PER_RUN = 50000
DUPLICATE_DURATION_TOLERANCE_SECONDS = 2.5
DUPLICATE_TITLE_THRESHOLD = 0.93
DUPLICATE_ARTIST_THRESHOLD = 0.93
DUPLICATE_ALBUM_THRESHOLD = 0.80
LYRICS_CACHE_MAX = 2000
LYRICS_LOOKUP_TIMEOUT_SECONDS = 10
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
    "health_scan_interval_minutes": 360,
    "title_cleanup_rules": "(Visualizer)\n[Visualizer]\nOfficial Video\nOfficial Music Video\nVideo Clip",
    "metadata_mode": "auto",
    "daily_mix_track_count": 30,
    "replaygain_enabled": True,
    "replaygain_mode": "track",
    "replaygain_preamp_db": 0.0,
    "replaygain_prevent_clipping": True,
    "crossfade_seconds": 0.0,
    "gapless_playback": True,
    "keep_playing": True,
    "download_location": "",
    "max_concurrent_downloads": 3,
    "max_pending_downloads": 500,
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
YT_DLP_JS_RUNTIME_ARGS = ["--js-runtimes", "deno"] if shutil.which("deno") else []
YT_DLP_COMMAND = [sys.executable, "-m", "yt_dlp", *YT_DLP_JS_RUNTIME_ARGS]
FFMPEG_COMMAND = [shutil.which("ffmpeg") or "ffmpeg"]
FFPROBE_COMMAND = [shutil.which("ffprobe") or "ffprobe"]

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
DOWNLOAD_IDENTITY_LOCK = asyncio.Lock()
ACTIVE_CONTENT_DOWNLOADS = set()
DOWNLOAD_WORKER_RESIZE_LOCK = asyncio.Lock()
LIBRARY_CACHE = None
LIBRARY_CACHE_LOCK = asyncio.Lock()
LIBRARY_INDEX_FILE = DATA_DIR / "library_index.json"
LIBRARY_WARMUP_TASK = None
DOWNLOAD_WORKER_TASKS = set()
DOWNLOAD_WORKER_CONTROLS = {}
QUEUED_TASK_IDS = set()
BACKGROUND_TASKS = set()
SCHEDULED_SCANNER_TASK = None
LIBRARY_HEALTH_TASK = None
RUNTIME_MAINTENANCE_TASK = None
LIBRARY_SCAN_LOCK = asyncio.Lock()
LIBRARY_CATALOG_LOCK = asyncio.Lock()
LIBRARY_REFRESH_TASK = None
LIBRARY_REFRESH_REQUESTED_AT = 0.0
LIBRARY_REFRESH_GENERATION = 0
LIBRARY_REFRESH_DIRTY = False
LIBRARY_REFRESH_MODE = "quick"
LIBRARY_REFRESH_REASONS = set()
LIBRARY_REVISION = 0

LIBRARY_REFRESH_WAITERS = []
SCHEDULED_SCANNER_WAKE = None
LIBRARY_HEALTH_WAKE = None
COVER_LOCKS = {}
COVER_LOCKS_GUARD = asyncio.Lock()
SIMILARITY_RESULTS_CACHE = {}
SIMILARITY_CACHE_MAX = 512
SIMILARITY_CACHE_TTL = 60.0
HISTORY_MAX_ROWS = 100000
APP_ERRORS_MAX_ROWS = 5000
MAX_PLAYLIST_SONGS = 10000
STORAGE_STATE = "unknown"
STORAGE_ERROR = ""
STORAGE_LAST_CHECKED_AT = 0.0
STORAGE_STATE_DB_READY = False
TASK_PRUNE_LAST_AT = 0.0

LIBRARY_CATALOG = None


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
    requested_location = configured or os.getenv("DOWNLOAD_DIR", DEFAULT_LIBRARY_PATH)
    path = Path(requested_location).expanduser()

    if not path.is_absolute():
        raise RuntimeError("music_path must be an absolute path")

    DOWNLOAD_DIR = path.resolve()
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

    # Do not silently create a missing explicitly configured NAS mount. Keep
    # the configured path as-is so runtime storage health can report OFFLINE
    # instead of masking a disconnected NAS with a new local directory.
    explicit_path = bool(persisted_location or configured)
    if explicit_path and not DOWNLOAD_DIR.exists():
        print(f"Warning: configured music_path is not mounted yet: {DOWNLOAD_DIR}")
    else:
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        COVER_CACHE_DIR.mkdir(parents=True, exist_ok=True)



def _persist_storage_state_sync(state, error=""):
    global STORAGE_STATE, STORAGE_ERROR, STORAGE_LAST_CHECKED_AT
    STORAGE_STATE = str(state or "unknown")
    STORAGE_ERROR = str(error or "")[:500]
    STORAGE_LAST_CHECKED_AT = time.time()
    if not STORAGE_STATE_DB_READY or not DB_FILE.exists():
        return
    try:
        with db_connect() as conn:
            conn.execute(
                "INSERT INTO storage_state(id,state,error,checked_at,online_since,offline_since) VALUES(1,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET state=excluded.state,error=excluded.error,checked_at=excluded.checked_at,"
                "online_since=CASE WHEN excluded.state='online' AND storage_state.state<>'online' THEN excluded.checked_at ELSE storage_state.online_since END,"
                "offline_since=CASE WHEN excluded.state='offline' AND storage_state.state<>'offline' THEN excluded.checked_at ELSE storage_state.offline_since END",
                (STORAGE_STATE, STORAGE_ERROR, STORAGE_LAST_CHECKED_AT,
                 STORAGE_LAST_CHECKED_AT if STORAGE_STATE == "online" else None,
                 STORAGE_LAST_CHECKED_AT if STORAGE_STATE == "offline" else None),
            )
    except Exception:
        pass


def _probe_storage_sync():
    try:
        DOWNLOAD_DIR.stat()
        if not DOWNLOAD_DIR.is_dir():
            raise OSError("Configured music path is not a directory")
        if not os.access(DOWNLOAD_DIR, os.R_OK):
            raise PermissionError("Configured music path is not readable")
        with os.scandir(DOWNLOAD_DIR) as iterator:
            next(iterator, None)
        return True, ""
    except (OSError, PermissionError, RuntimeError) as exc:
        return False, _sanitize_external_error(exc, "Storage is unavailable.", 400)


def storage_info_sync():
    ok, error = _probe_storage_sync()
    now = time.time()
    new_state = "online" if ok else "offline"
    if new_state != STORAGE_STATE or error != STORAGE_ERROR or now - STORAGE_LAST_CHECKED_AT > 30:
        _persist_storage_state_sync(new_state, error)
    path = DOWNLOAD_DIR
    exists = False
    writable = False
    total = free = used = 0
    try:
        exists = path.is_dir()
        writable = exists and os.access(path, os.W_OK)
        if exists:
            usage = shutil.disk_usage(path)
            total, free = usage.total, usage.free
            used = total - free
    except (OSError, RuntimeError):
        exists = False
    return {
        "path": str(path),
        "exists": exists,
        "writable": writable,
        "mounted": exists,
        "state": STORAGE_STATE,
        "online": STORAGE_STATE == "online",
        "error": STORAGE_ERROR,
        "checked_at": STORAGE_LAST_CHECKED_AT,
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "total": format_size(total),
        "used": format_size(used),
        "free": format_size(free),
    }


def _load_library_revision_sync():
    try:
        with db_connect() as conn:
            row = conn.execute("SELECT revision FROM library_meta WHERE id=1").fetchone()
            return int(row[0] or 0) if row else 0
    except Exception:
        return 0

def _bump_library_revision_sync():
    now = time.time()
    with db_connect() as conn:
        conn.execute("INSERT INTO library_meta(id,revision,updated_at) VALUES(1,1,?) ON CONFLICT(id) DO UPDATE SET revision=library_meta.revision+1, updated_at=excluded.updated_at", (now,))
        row = conn.execute("SELECT revision FROM library_meta WHERE id=1").fetchone()
        return int(row[0] or 0) if row else 0

def invalidate_library_cache(reason="library_changed"):
    global LIBRARY_CACHE, LIBRARY_REVISION
    LIBRARY_CACHE = None
    SIMILARITY_RESULTS_CACHE.clear()
    try:
        if DB_FILE.exists():
            LIBRARY_REVISION = _bump_library_revision_sync()
    except Exception as exc:
        try:
            write_app_error_sync("library_revision", exc)
        except Exception:
            pass
    return LIBRARY_REVISION


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

        except Exception as exc:
            corrupt = None
            try:
                corrupt = SETTINGS_FILE.with_name(f"{SETTINGS_FILE.name}.corrupt-{int(time.time())}")
                os.replace(SETTINGS_FILE, corrupt)
            except OSError:
                try:
                    corrupt = SETTINGS_FILE.with_name(f"{SETTINGS_FILE.name}.corrupt-backup-{int(time.time())}")
                    shutil.copy2(SETTINGS_FILE, corrupt)
                except OSError:
                    corrupt = None
            settings["settings_recovery_warning"] = "Settings were reset to safe defaults because the settings file was invalid. The original file was preserved for recovery."
            try:
                write_app_error_sync("settings_load", exc)
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
    settings["keep_playing"] = bool(settings.get("keep_playing", True))
    settings["download_location"] = str(settings.get("download_location") or "").strip()[:4096]
    if not settings["download_location"]:
        settings["download_location"] = str(DOWNLOAD_DIR.resolve())
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
        "crossfade_seconds", "gapless_playback", "keep_playing", "web_username", "web_password",
        "download_location", "max_concurrent_downloads", "max_pending_downloads", "auto_retry_downloads", "download_retry_limit",
        "download_retry_backoff_seconds", "artwork_behavior", "cache_size_mb", "filename_mode", "stats_retention_days", "health_scan_interval_minutes",
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
        health_interval = int(settings.get("health_scan_interval_minutes", 360) or 360)
    except (TypeError, ValueError):
        health_interval = 360
    settings["health_scan_interval_minutes"] = max(30, min(10080, health_interval))
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
    settings["keep_playing"] = bool(settings.get("keep_playing", True))
    requested_location = str(settings.get("download_location") or "").strip()[:4096]
    settings["download_location"] = str(_resolve_approved_download_root(requested_location, allow_empty=True))
    try:
        settings["max_concurrent_downloads"] = max(1, min(8, int(settings.get("max_concurrent_downloads", 3) or 3)))
    except (TypeError, ValueError):
        settings["max_concurrent_downloads"] = 3
    try:
        settings["max_pending_downloads"] = max(50, min(5000, int(settings.get("max_pending_downloads", 500) or 500)))
    except (TypeError, ValueError):
        settings["max_pending_downloads"] = 500
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
    # Password verifiers are server credentials and must never be returned to the browser.
    settings.pop("web_password_hash", None)
    settings["storage"] = storage_info_sync()
    return settings

async def load_settings_async():
    return await asyncio.to_thread(load_settings)


async def save_settings_async(data):
    settings = await asyncio.to_thread(save_settings, data)
    try:
        await resize_download_workers(settings.get("max_concurrent_downloads", MAX_CONCURRENT_DOWNLOADS))
    except Exception as exc:
        await write_app_error("worker_resize", exc)
    global SCHEDULED_SCANNER_WAKE, LIBRARY_HEALTH_WAKE
    for wake in (SCHEDULED_SCANNER_WAKE, LIBRARY_HEALTH_WAKE):
        if wake is not None:
            try:
                wake.set()
            except Exception:
                pass
    return settings


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
        conn.execute("""CREATE TABLE IF NOT EXISTS library_meta (
            id INTEGER PRIMARY KEY CHECK (id=1),
            revision INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT 0
        )""")
        conn.execute("INSERT OR IGNORE INTO library_meta(id,revision,updated_at) VALUES(1,0,?)", (time.time(),))
        # Legacy installations can have a reduced tasks schema. Add missing columns
        # before creating indexes that depend on them; otherwise an upgrade can fail
        # during startup with "no such column: tasks.last_updated".
        task_columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        task_column_defs = {
            "last_updated": "REAL DEFAULT 0",
            "final_name": "TEXT DEFAULT ''",
            "created_at": "REAL DEFAULT 0",
            "content_identity": "TEXT DEFAULT ''",
            "identity_version": "INTEGER DEFAULT 1",
            "album_version": "TEXT DEFAULT ''",
            "download_root": "TEXT DEFAULT ''",
            "catalog_pending_path": "TEXT DEFAULT ''",
        }
        for column, definition in task_column_defs.items():
            if column not in task_columns:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {definition}")
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
        conn.execute("""CREATE TABLE IF NOT EXISTS storage_state (
            id INTEGER PRIMARY KEY CHECK (id=1),
            state TEXT NOT NULL DEFAULT 'unknown',
            error TEXT DEFAULT '',
            checked_at REAL NOT NULL DEFAULT 0,
            online_since REAL,
            offline_since REAL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS library_health (
            id INTEGER PRIMARY KEY CHECK (id=1),
            status TEXT NOT NULL DEFAULT 'idle',
            started_at REAL DEFAULT 0,
            finished_at REAL DEFAULT 0,
            message TEXT DEFAULT '',
            report_json TEXT NOT NULL DEFAULT '{}',
            updated_at REAL NOT NULL DEFAULT 0
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS library_delete_intents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            song_id TEXT NOT NULL DEFAULT '',
            relative_path TEXT UNIQUE NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_library_delete_intents_updated ON library_delete_intents(updated_at ASC)")
        conn.execute("""CREATE TABLE IF NOT EXISTS subsonic_scrobbles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT UNIQUE NOT NULL,
            username TEXT NOT NULL DEFAULT '',
            song_id TEXT NOT NULL,
            submission INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL,
            position REAL DEFAULT 0,
            duration REAL DEFAULT 0
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_subsonic_scrobbles_song_created ON subsonic_scrobbles(song_id, created_at DESC)")
        # The media catalog is authoritative in SQLite. library_index.json is only
        # consumed by the one-time compatibility migration in startup_event().
        global LIBRARY_CATALOG
        if LIBRARY_CATALOG is None:
            LIBRARY_CATALOG = LibraryCatalog(DB_FILE, DOWNLOAD_DIR, LIBRARY_INDEX_FILE)
        LIBRARY_CATALOG.init_schema(conn)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        if "identity_key" not in columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN identity_key TEXT DEFAULT ''")
        if "retry_count" not in columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN retry_count INTEGER DEFAULT 0")
        if "resume_available" not in columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN resume_available INTEGER DEFAULT 0")
        if "content_identity" not in columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN content_identity TEXT DEFAULT ''")
        if "identity_version" not in columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN identity_version INTEGER DEFAULT 1")
        # Older releases did not have identity_key. Fill it deterministically and
        # neutralize duplicate legacy active rows before creating the partial unique index.
        for row in conn.execute("SELECT id,title,artist,url,status,identity_key FROM tasks WHERE status IN ('queued','downloading','processing','catalog_pending')").fetchall():
            identity = str(row[5] or "")
            if not identity:
                title_key = normalize_duplicate_key(row[1] or "", row[2] or "")
                url_key = hashlib.sha256(str(row[3] or "").encode("utf-8")).hexdigest()
                identity = f"{title_key}|{url_key}"
                conn.execute("UPDATE tasks SET identity_key=? WHERE id=?", (identity,row[0]))
        try:
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_active_identity ON tasks(identity_key) WHERE identity_key IS NOT NULL AND identity_key <> '' AND status IN ('queued','downloading','processing','catalog_pending')")
        except sqlite3.IntegrityError:
            duplicates = conn.execute("SELECT identity_key, COUNT(*) FROM tasks WHERE identity_key<>'' AND status IN ('queued','downloading','processing','catalog_pending') GROUP BY identity_key HAVING COUNT(*)>1").fetchall()
            for identity,_count in duplicates:
                dup_rows = conn.execute("SELECT id FROM tasks WHERE identity_key=? AND status IN ('queued','downloading','processing','catalog_pending') ORDER BY created_at ASC",(identity,)).fetchall()
                for dup in dup_rows[1:]:
                    conn.execute("UPDATE tasks SET identity_key='' WHERE id=?",(dup[0],))
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_active_identity ON tasks(identity_key) WHERE identity_key IS NOT NULL AND identity_key <> '' AND status IN ('queued','downloading','processing','catalog_pending')")
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
                    retry_count, resume_available, content_identity, identity_version, album_version,
                    download_root, catalog_pending_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title, artist=excluded.artist, album=excluded.album,
                    url=excluded.url, elementId=excluded.elementId, status=excluded.status,
                    percent=excluded.percent, speed=excluded.speed, step=excluded.step,
                    error=excluded.error, last_updated=excluded.last_updated,
                    final_name=excluded.final_name, created_at=excluded.created_at,
                    identity_key=excluded.identity_key, retry_count=excluded.retry_count,
                    resume_available=excluded.resume_available, content_identity=excluded.content_identity,
                    identity_version=excluded.identity_version, album_version=excluded.album_version,
                    download_root=excluded.download_root, catalog_pending_path=excluded.catalog_pending_path
                """,
                (
                    task.get("id"), task.get("title"), task.get("artist"), task.get("album"),
                    task.get("url"), task.get("elementId"), task.get("status"), task.get("percent", 0),
                    task.get("speed", ""), task.get("step", ""), task.get("error", ""),
                    task.get("last_updated", 0), task.get("final_name", ""),
                    task.get("created_at", task.get("last_updated", 0)), identity_key,
                    safe_int(task.get("retry_count"), 0), 1 if task.get("resume_available") else 0,
                    str(task.get("content_identity") or ""), safe_int(task.get("identity_version"), 1),
                    str(task.get("album_version") or ""), str(task.get("download_root") or ""),
                    str(task.get("catalog_pending_path") or ""),
                ),
            )
        except sqlite3.IntegrityError as exc:
            # The partial unique index is the final race-condition guard. Never use
            # INSERT OR REPLACE here: REPLACE would delete the existing active task.
            if task_status in ACTIVE_TASK_STATES and identity_key:
                conflict = conn.execute(
                    "SELECT id FROM tasks WHERE identity_key=? AND status IN ('queued','downloading','processing','catalog_pending') AND id<>? LIMIT 1",
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


def db_prune_tasks_sync():
    """Bound terminal task history without touching active download state."""
    cutoff = time.time() - (TASK_TERMINAL_RETENTION_DAYS * 86400)
    terminal = ("completed", "cancelled", "canceled", "error", "failed")
    with db_connect() as conn:
        conn.execute(
            "DELETE FROM tasks WHERE status IN (?,?,?,?,?) AND last_updated < ?",
            (*terminal, cutoff * 1000),
        )
        conn.execute(
            """DELETE FROM tasks
               WHERE id IN (
                   SELECT id FROM tasks
                   WHERE status IN (?,?,?,?,?)
                   ORDER BY last_updated DESC
                   LIMIT -1 OFFSET ?
               )""",
            (*terminal, TASK_TERMINAL_MAX_ROWS),
        )
        conn.commit()


async def _maybe_prune_tasks(force=False):
    global TASK_PRUNE_LAST_AT
    now = time.monotonic()
    if not force and now - TASK_PRUNE_LAST_AT < TASK_PRUNE_INTERVAL_SECONDS:
        return
    TASK_PRUNE_LAST_AT = now
    await asyncio.to_thread(db_prune_tasks_sync)


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

async def broadcast_stats_invalidated(reason="state_changed"):
    try:
        await manager.broadcast({"type":"stats_invalidated","reason":str(reason)[:120]})
    except Exception as exc:
        await write_app_error("stats_broadcast", _sanitize_external_error(exc, "Statistics event failed.", 300))



def _bounded_text(value, limit=PLAYER_STATE_MAX_TEXT):
    return str(value or "")[:limit]


def _sanitize_player_track(track):
    # Accept both the rich queue-object form emitted by the web client and
    # a compact canonical/legacy song-id form used by older clients.
    if isinstance(track, str):
        track = {"id": track}
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
            raw_queue=[_sanitize_player_track(item) for item in cleaned["queue"][:PLAYER_STATE_MAX_QUEUE_ITEMS]]
            raw_queue=[item for item in raw_queue if item]
            if LIBRARY_CATALOG is not None:
                try:
                    id_map=LIBRARY_CATALOG.resolve_song_ids([item.get("id") for item in raw_queue if item.get("id")])
                    for item in raw_queue:
                        sid=str(item.get("id") or "")
                        if sid in id_map:
                            item["id"]=id_map[sid]
                except Exception:
                    pass
            cleaned["queue"] = raw_queue
        else:
            cleaned.pop("queue", None)
        if cleaned.get("queue"):
            cleaned["queueIndex"] = min(max(safe_int(cleaned.get("queueIndex"), -1), -1), len(cleaned["queue"]) - 1)
        elif "queueIndex" in cleaned:
            cleaned["queueIndex"] = -1

    if "songId" in cleaned and cleaned.get("songId") and LIBRARY_CATALOG is not None:
        try:
            cleaned["songId"] = LIBRARY_CATALOG.resolve_song_id(cleaned["songId"])
        except Exception:
            pass

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
                cleaned_tracks=[t for t in (_sanitize_player_track(item) for item in tracks[:PLAYER_STATE_MAX_DAILY_MIX_ITEMS]) if t]
                if LIBRARY_CATALOG is not None:
                    try:
                        id_map=LIBRARY_CATALOG.resolve_song_ids([item.get("id") for item in cleaned_tracks if item.get("id")])
                        for item in cleaned_tracks:
                            sid=str(item.get("id") or "")
                            if sid in id_map:
                                item["id"]=id_map[sid]
                    except Exception:
                        pass
                daily["tracks"] = cleaned_tracks
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


async def _refill_download_queue():
    """Reconcile persisted queued tasks with the in-memory work queue.

    The database/TASKS map is authoritative; the asyncio queue is disposable.
    Queue tokens make stale entries harmless after cancellation/retry.
    """
    queued_tasks = [
        (task_id, task) for task_id, task in sorted(
        TASKS.items(),
        key=lambda item: safe_float(item[1].get("created_at", item[1].get("last_updated", 0)), 0),
        ) if task.get("status") == "queued" and not task.get("cancel_requested") and task_id not in QUEUED_TASK_IDS
    ]
    if queued_tasks:
        ok, error = await asyncio.to_thread(_probe_storage_sync)
        if not ok:
            await asyncio.to_thread(_persist_storage_state_sync, "offline", error)
            return

    for task_id, task in queued_tasks:
        if task.get("status") != "queued":
            continue
        if task.get("cancel_requested"):
            continue
        if task_id in QUEUED_TASK_IDS:
            continue
        token = str(task.get("queue_token") or "")
        if not token:
            token = uuid.uuid4().hex
            task["queue_token"] = token
            await db_save_task(task, force=True)
        QUEUED_TASK_IDS.add(task_id)
        await TASK_QUEUE.put((task_id, token))


def _worker_done(worker_task):
    DOWNLOAD_WORKER_TASKS.discard(worker_task)
    DOWNLOAD_WORKER_CONTROLS.pop(worker_task, None)


async def resize_download_workers(target):
    """Resize the download worker pool without cancelling active downloads."""
    target = max(1, min(8, safe_int(target, MAX_CONCURRENT_DOWNLOADS)))
    async with DOWNLOAD_WORKER_RESIZE_LOCK:
        active_workers = [task for task in DOWNLOAD_WORKER_TASKS if not task.done()]
        if len(active_workers) < target:
            for _ in range(target - len(active_workers)):
                stop_event = asyncio.Event()
                worker = asyncio.create_task(download_worker(stop_event))
                DOWNLOAD_WORKER_TASKS.add(worker)
                DOWNLOAD_WORKER_CONTROLS[worker] = stop_event
                worker.add_done_callback(_worker_done)
        elif len(active_workers) > target:
            # Do not cancel workers: stop_event is checked between jobs so an active
            # yt-dlp/ffmpeg process is allowed to finish normally.
            extras = active_workers[target:]
            for worker in extras:
                control = DOWNLOAD_WORKER_CONTROLS.get(worker)
                if control is not None:
                    control.set()


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
    """Return a stable artist+title key; empty when identity is too weak to trust."""
    if artist is not None:
        artist_key = normalize_identity_text(artist)
        title_key = normalize_identity_text(value, title=True)
        generic_artists = {"unknown artist", "unknown", "various artists"}
        generic_titles = {"unknown track", "unknown", ""}
        if not artist_key or artist_key in generic_artists or not title_key or title_key in generic_titles:
            return ""
        return f"{artist_key}\x00{title_key}"
    text = str(value or "")
    parts = text.split("|", 2)
    if len(parts) >= 2:
        title, artist = parts[0], parts[1]
        return normalize_duplicate_key(title, artist)
    title_key = normalize_identity_text(Path(text).stem, title=True)
    return "" if not title_key or title_key in {"unknown", "unknown track"} else title_key


# ============================================================
# LIBRARY FILES
# ============================================================

def get_audio_files_sync():
    ok, error = _probe_storage_sync()
    if not ok:
        _persist_storage_state_sync("offline", error)
        raise StorageUnavailable(f"Music storage is offline: {error}")
    _persist_storage_state_sync("online", "")
    base = DOWNLOAD_DIR.resolve()
    files = []
    try:
        iterator = DOWNLOAD_DIR.rglob("*")
        for path in iterator:
            if path.name.startswith(".") or path.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            resolved = path.resolve()
            if not resolved.is_file() or not resolved.is_relative_to(base):
                continue
            files.append(resolved)
    except (OSError, RuntimeError) as exc:
        _persist_storage_state_sync("offline", _sanitize_external_error(exc, "Storage is unavailable.", 400))
        raise StorageUnavailable(f"Music storage became unavailable during scan: {exc}") from exc
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
        "album_version": "",
        "is_compilation": False,
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
                "album_version": first_tag("album_version", "albumversion", "version", "edition", "release_version"),
                "is_compilation": str(first_tag("compilation", "itunescompilation", "album_type", "release_type")).strip().lower() in {"1", "true", "yes", "compilation", "various artists"},
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
                metadata["album_version"] = mt("albumversion", "version", "edition", "release_version") or metadata.get("album_version", "")
                compilation_raw = mt("compilation", "itunescompilation", "albumtype", "release_type")
                if compilation_raw:
                    metadata["is_compilation"] = compilation_raw.strip().lower() in {"1", "true", "yes"}
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


# ============================================================
# PERSISTENT LIBRARY CATALOG
# ============================================================

def make_artist_id(name):
    name = clean_metadata_text(name, "Unknown Artist")
    name = re.sub(r"\s+", " ", name).strip()
    digest = hashlib.sha1(name.casefold().encode("utf-8")).hexdigest()[:20]
    return f"artist-{digest}"


def make_album_id(artist, album, version="", compilation=False):
    artist_identity = re.sub(r"\s+", " ", clean_metadata_text(artist, "Unknown Artist")).strip().casefold()
    album_identity = re.sub(r"\s+", " ", clean_metadata_text(album, "Unknown Album")).strip().casefold()
    version_identity = re.sub(r"\s+", " ", clean_metadata_text(version, "")).strip().casefold()
    if compilation:
        artist_identity = "various artists"
    digest = hashlib.sha1((artist_identity + "\x00" + album_identity + "\x00" + version_identity).encode("utf-8")).hexdigest()[:20]
    return f"album-{digest}"


def _catalog_rows_sync():
    if LIBRARY_CATALOG is None:
        return []
    return LIBRARY_CATALOG.rows()


def _catalog_row_to_metadata(row):
    if LIBRARY_CATALOG is None:
        return {}
    return LIBRARY_CATALOG._metadata_from_row(row)


async def _reconcile_library_catalog(files):
    if LIBRARY_CATALOG is None:
        raise RuntimeError("Library catalog is not initialized")
    async with LIBRARY_CATALOG_LOCK:
        await asyncio.to_thread(LIBRARY_CATALOG.reconcile, files, read_metadata_sync, LIBRARY_METADATA_CONCURRENCY)


def _catalog_song_for_path_sync(path):
    if LIBRARY_CATALOG is None:
        return None
    try:
        return LIBRARY_CATALOG.song_for_path(path)
    except (OSError, RuntimeError, ValueError):
        return None



def _resolve_song_id_sync(song_id):
    if LIBRARY_CATALOG is None:
        return str(song_id or "")[:512]
    return LIBRARY_CATALOG.resolve_song_id(song_id)

def _resolve_song_ids_sync(song_ids):
    if LIBRARY_CATALOG is None:
        return {str(v):str(v) for v in song_ids or [] if str(v)}
    aliases=LIBRARY_CATALOG.resolve_song_ids(song_ids)
    return {str(v): aliases.get(str(v), str(v)) for v in song_ids or [] if str(v)}

def _canonical_song_id_for_write(song_id):
    sid = str(song_id or "").strip()[:512]
    if not sid:
        return ""
    try:
        return _resolve_song_id_sync(sid) or sid
    except Exception:
        return sid



@app.post("/api/player/resolve-queue")
async def api_player_resolve_queue(payload: dict = Body(...)):
    raw=payload.get("ids") if isinstance(payload,dict) else []
    if not isinstance(raw,list):
        raise HTTPException(400,"ids must be an array")
    ids=[str(v)[:512] for v in raw[:PLAYER_STATE_MAX_QUEUE_ITEMS] if str(v)]
    return {"mapping": await asyncio.to_thread(_resolve_song_ids_sync, ids)}


async def fast_library_snapshot():
    rows = await asyncio.to_thread(lambda: [row for row in _catalog_rows_sync() if not int(row.get("missing") or 0)])
    files=[]; total=0; artists=set(); albums=set()
    for row in rows:
        metadata=_catalog_row_to_metadata(row)
        rel=str(row["relative_path"])
        size=int(row.get("size") or 0)
        total += size
        title=clean_metadata_text(metadata.get("title"), Path(rel).stem)
        artist=clean_metadata_text(metadata.get("artist"), "Unknown Artist")
        album=clean_metadata_text(metadata.get("album"), "Unknown Album")
        artists.add(artist.casefold())
        albums.add((artist.casefold(), album.casefold()))
        enc=urllib.parse.quote(rel,safe="/")
        version = f"{int(row.get('mtime_ns') or 0)}-{int(row.get('size') or 0)}"
        files.append({"id":str(row["id"]),"name":rel,"title":title,"artist":artist,"album":album,"size":format_size(size),"bytes":size,"duration":safe_float(metadata.get("duration"),0),"play_count":0,"cover":f"/api/library/cover/{enc}?v={urllib.parse.quote(version)}","stream":"/api/library/stream/"+enc})
    storage=await asyncio.to_thread(storage_info_sync)
    scan_state=await asyncio.to_thread(_scan_state_read_sync)
    scan_status=str(scan_state.get("status") or "idle")
    if storage.get("state") == "offline":
        library_state="offline"
    elif scan_status == "running":
        library_state="scanning"
    elif scan_status == "error":
        library_state="error"
    elif not rows:
        library_state="empty"
    else:
        library_state="ready"
    return {"files":files,"total_size":format_size(total),"total_bytes":total,"artists_count":len(artists),"albums_count":len(albums),"ready":library_state in {"ready","empty"},"library_state":library_state,"storage":storage,"storage_state":storage.get("state",STORAGE_STATE),"scan_state":scan_state,"revision":LIBRARY_REVISION}


async def build_library(force=False):
    global LIBRARY_CACHE
    if not force and LIBRARY_CACHE is not None:
        return LIBRARY_CACHE
    async with LIBRARY_CACHE_LOCK:
        if not force and LIBRARY_CACHE is not None:
            return LIBRARY_CACHE
        if force:
            files = await get_all_audio_files()
            files.sort(key=lambda path: str(path).lower())
            await _reconcile_library_catalog(files)
        rows = await asyncio.to_thread(lambda: [row for row in _catalog_rows_sync() if not int(row.get("missing") or 0)])
        songs=[]; artists={}; albums={}; genres={}
        # First pass: infer compilation albums from folder/album grouping where metadata
        # does not explicitly provide a consistent album artist.
        album_artist_votes = defaultdict(set)
        row_context = []
        for row in rows:
            metadata = _catalog_row_to_metadata(row)
            rel = str(row["relative_path"])
            folder_key = str(Path(rel).parent).casefold()
            album_key = (folder_key, _compact_identity(metadata.get("album") or Path(rel).stem))
            artist_key = _compact_identity(metadata.get("artist") or "Unknown Artist")
            album_artist_votes[album_key].add(artist_key)
            row_context.append((row, metadata, album_key))
        for row,metadata,album_group_key in row_context:

            path=(DOWNLOAD_DIR / str(row["relative_path"])).resolve()
            artist_name=clean_metadata_text(metadata.get("artist"),"Unknown Artist")
            explicit_album_artist=clean_metadata_text(metadata.get("album_artist"), "")
            explicit_compilation=bool(metadata.get("is_compilation")) or _compact_identity(explicit_album_artist) == "various artists"
            vote_count=len(album_artist_votes.get(album_group_key, set()))
            infer_from_folder=(not explicit_album_artist or _compact_identity(explicit_album_artist) in {"unknown", "unknown artist", _compact_identity(artist_name)}) and vote_count >= 2
            inferred_compilation=explicit_compilation or infer_from_folder
            album_artist=explicit_album_artist or artist_name
            if inferred_compilation:
                album_artist = "Various Artists"
            album_name=clean_metadata_text(metadata.get("album"),path.stem)
            album_version=clean_metadata_text(metadata.get("album_version"),"")
            artist_id=make_artist_id(artist_name); album_artist_id=make_artist_id(album_artist); album_id=make_album_id(album_artist,album_name,album_version,inferred_compilation)
            try:
                created=path.stat().st_ctime; modified=path.stat().st_mtime
            except OSError:
                created=modified=0
            song={
                "id":str(row["id"]),"title":clean_metadata_text(metadata.get("title"),path.stem),"artist":artist_name,"artistId":artist_id,
                "albumArtist":album_artist,"albumArtistId":album_artist_id,"album":album_name,"albumId":album_id,"albumVersion":album_version,"isCompilation":inferred_compilation,
                "genre":metadata.get("genre", ""),"year":metadata.get("year", ""),"track":metadata.get("track", 0),"disc":metadata.get("disc", 0),
                "duration":safe_int(metadata.get("duration"),0),"bit_rate":safe_int(metadata.get("bit_rate"),0),"bit_depth":safe_int(metadata.get("bit_depth"),0),
                "sample_rate":safe_int(metadata.get("sample_rate"),0),"channels":safe_int(metadata.get("channels"),0),
                "replaygain_track_gain":metadata.get("replaygain_track_gain"),"replaygain_album_gain":metadata.get("replaygain_album_gain"),
                "replaygain_track_peak":metadata.get("replaygain_track_peak"),"replaygain_album_peak":metadata.get("replaygain_album_peak"),
                "has_artwork":bool(metadata.get("has_artwork")),"path":path,"suffix":path.suffix.lower(),"size":int(row.get("size") or 0),"created":created,"modified":modified,
            }
            songs.append(song)
            for current_id,current_name in {artist_id:artist_name,album_artist_id:album_artist}.items():
                if current_id not in artists:
                    artists[current_id]={"id":current_id,"name":current_name,"albumIds":set(),"songIds":[]}
                artists[current_id]["albumIds"].add(album_id)
                artists[current_id]["songIds"].append(song["id"]) if song["id"] not in artists[current_id]["songIds"] else None
            if album_id not in albums:
                albums[album_id]={"id":album_id,"name":album_name,"artist":album_artist,"artistId":album_artist_id,"albumArtist":album_artist,"albumVersion":album_version,"isCompilation":inferred_compilation,"year":metadata.get("year",""),"genre":metadata.get("genre",""),"songIds":[],"path":path}
            albums[album_id]["songIds"].append(song["id"])
            if metadata.get("genre"):
                genres[metadata["genre"]]=genres.get(metadata["genre"],0)+1
        def song_sort_key(song):
            disc=safe_int(song.get("disc"),0); track=safe_int(song.get("track"),0)
            return (disc if disc>0 else 9999,track if track>0 else 9999,song["title"].lower(),str(song["path"]).lower())
        songs.sort(key=song_sort_key); song_by_id={song["id"]:song for song in songs}
        for artist in artists.values():
            artist["albumIds"]=sorted(artist["albumIds"],key=lambda aid:albums[aid]["name"].lower()); artist["songIds"]=sorted(artist["songIds"],key=lambda sid:song_sort_key(song_by_id[sid]))
        for album in albums.values():
            album["songIds"]=sorted(album["songIds"],key=lambda sid:song_sort_key(song_by_id[sid]))
        LIBRARY_CACHE={"songs":songs,"artists":artists,"albums":albums,"genres":genres,"_songs_by_id":song_by_id,"_artists_by_id":dict(artists),"_albums_by_id":dict(albums),"_revision":LIBRARY_REVISION}
        return LIBRARY_CACHE


async def find_song(song_id):
    resolved = await asyncio.to_thread(_resolve_song_id_sync, song_id)
    library=await build_library()
    return library.get("_songs_by_id",{}).get(resolved)


async def find_artist(artist_id):
    library=await build_library(); return library.get("_artists_by_id",{}).get(artist_id)


async def find_album(album_id):
    library=await build_library(); return library.get("_albums_by_id",{}).get(album_id)


async def persist_library_index(*_args, **_kwargs):
    """Compatibility no-op. SQLite library_songs is the only runtime source of truth."""
    return None


async def background_library_warmup():
    global LIBRARY_WARMUP_TASK
    try:
        await request_library_refresh(reason="startup", mode="quick", wait=True)
    except StorageUnavailable as exc:
        print("Library warmup skipped: storage offline:", exc)
    except Exception as exc:
        print("Library warmup failed:", exc)
    finally:
        LIBRARY_WARMUP_TASK=None


# ============================================================
# LIBRARY REFRESH ENGINE
# ============================================================

async def _perform_library_refresh(mode="quick", reason="manual"):
    async with LIBRARY_SCAN_LOCK:
        started=time.time()
        await asyncio.to_thread(_scan_state_sync,"running",mode,"Refreshing library",started)
        try:
            invalidate_library_cache()
            library=await build_library(force=True)
            if mode=="full":
                cover_tasks=[ensure_cover(song["path"]) for song in library["songs"]]
                if cover_tasks:
                    semaphore=asyncio.Semaphore(8)
                    async def cover_one(coro):
                        async with semaphore:
                            try: return await coro
                            except Exception: return None
                    await asyncio.gather(*(cover_one(c) for c in cover_tasks),return_exceptions=True)
            await asyncio.to_thread(_scan_state_sync,"ok",mode,f"{len(library['songs'])} tracks scanned")
            await manager.broadcast({"type":"library_updated","reason":reason,"count":len(library["songs"]),"revision":LIBRARY_REVISION})
            await broadcast_stats_invalidated("library_updated")
            return library
        except StorageUnavailable as exc:
            safe = _sanitize_external_error(exc, "Music storage is offline.")
            await asyncio.to_thread(_scan_state_sync,"error",mode,safe,started)
            await manager.broadcast({"type":"storage_state","state":"offline","error":safe})
            raise
        except Exception as exc:
            safe = _sanitize_external_error(exc, "Library refresh failed.")
            await write_app_error("library_scan",safe)
            await asyncio.to_thread(_scan_state_sync,"error",mode,safe,started)
            raise


async def _library_refresh_worker():
    global LIBRARY_REFRESH_TASK,LIBRARY_REFRESH_DIRTY,LIBRARY_REFRESH_MODE,LIBRARY_REFRESH_REQUESTED_AT,LIBRARY_REFRESH_REASONS,LIBRARY_REFRESH_WAITERS
    try:
        while True:
            delay=max(0.0,LIBRARY_REFRESH_DEBOUNCE_SECONDS-(time.monotonic()-LIBRARY_REFRESH_REQUESTED_AT))
            if delay: await asyncio.sleep(delay)
            target_generation=LIBRARY_REFRESH_GENERATION
            mode=LIBRARY_REFRESH_MODE
            reason=", ".join(sorted(LIBRARY_REFRESH_REASONS)) or "manual"
            LIBRARY_REFRESH_DIRTY=False; LIBRARY_REFRESH_MODE="quick"; LIBRARY_REFRESH_REASONS=set()
            try:
                result=await _perform_library_refresh(mode,reason); error=None
            except Exception as exc:
                result=None; error=exc
            ready=[]; remain=[]
            for generation,fut in LIBRARY_REFRESH_WAITERS:
                if generation<=target_generation: ready.append((fut,error,result))
                else: remain.append((generation,fut))
            LIBRARY_REFRESH_WAITERS=remain
            for fut,exc,result_value in ready:
                if fut.done(): continue
                if exc is not None: fut.set_exception(exc)
                else: fut.set_result(result_value)
            if not LIBRARY_REFRESH_DIRTY: break
    finally:
        LIBRARY_REFRESH_TASK=None


async def request_library_refresh(reason="manual",mode="quick",wait=False):
    global LIBRARY_REFRESH_TASK,LIBRARY_REFRESH_REQUESTED_AT,LIBRARY_REFRESH_GENERATION,LIBRARY_REFRESH_DIRTY,LIBRARY_REFRESH_MODE,LIBRARY_REFRESH_REASONS,LIBRARY_REFRESH_WAITERS
    requested_mode="full" if str(mode).lower()=="full" else "quick"
    LIBRARY_REFRESH_GENERATION += 1
    generation=LIBRARY_REFRESH_GENERATION
    LIBRARY_REFRESH_REQUESTED_AT=time.monotonic()
    LIBRARY_REFRESH_DIRTY=True
    LIBRARY_REFRESH_MODE="full" if requested_mode=="full" or LIBRARY_REFRESH_MODE=="full" else "quick"
    if reason: LIBRARY_REFRESH_REASONS.add(str(reason)[:80])
    waiter=None
    if wait:
        waiter=asyncio.get_running_loop().create_future()
        LIBRARY_REFRESH_WAITERS.append((generation,waiter))
    if LIBRARY_REFRESH_TASK is None or LIBRARY_REFRESH_TASK.done():
        LIBRARY_REFRESH_TASK=asyncio.create_task(_library_refresh_worker())
    return await waiter if waiter is not None else None


# ============================================================
# COVER ART
# ============================================================

def versioned_cover_url(path, version=""):
    try:
        relative = str(Path(path).resolve().relative_to(DOWNLOAD_DIR.resolve()))
    except (OSError, RuntimeError, ValueError):
        relative = str(Path(path).name)
    try:
        stat = Path(path).stat()
        stamp = version or f"{int(stat.st_mtime_ns)}-{int(stat.st_size)}"
    except OSError:
        stamp = version or "0"
    return f"/api/library/cover/{urllib.parse.quote(relative, safe='/')}?v={urllib.parse.quote(str(stamp), safe='')}"


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
        entry = COVER_LOCKS.get(key)
        if not isinstance(entry, dict):
            entry = {"lock": asyncio.Lock(), "users": 0}
            COVER_LOCKS[key] = entry
        entry["users"] = int(entry.get("users") or 0) + 1
        return key, entry["lock"]


async def _cover_lock_release(key, lock):
    async with COVER_LOCKS_GUARD:
        entry = COVER_LOCKS.get(key)
        if isinstance(entry, dict) and entry.get("lock") is lock:
            entry["users"] = max(0, int(entry.get("users") or 0) - 1)
            if entry["users"] == 0 and not lock.locked():
                COVER_LOCKS.pop(key, None)


async def ensure_cover(path):
    cover = await asyncio.to_thread(cover_cache_path, path)
    lock_key, lock = await _cover_lock_for(cover)
    try:
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
    finally:
        await _cover_lock_release(lock_key, lock)

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

async def _switch_content_identity_reservation(old_identity, new_identity):
    """Atomically move an in-flight download reservation to final normalized identity."""
    old_identity = str(old_identity or "")
    new_identity = str(new_identity or "")
    async with DOWNLOAD_IDENTITY_LOCK:
        if new_identity and new_identity != old_identity and new_identity in ACTIVE_CONTENT_DOWNLOADS:
            return False
        if old_identity and old_identity != new_identity:
            ACTIVE_CONTENT_DOWNLOADS.discard(old_identity)
        if new_identity:
            ACTIVE_CONTENT_DOWNLOADS.add(new_identity)
        return True


async def _release_content_identity_reservations(*identities):
    """Release one or more reservation keys as one synchronized operation."""
    keys = {str(identity or "") for identity in identities if str(identity or "")}
    if not keys:
        return
    async with DOWNLOAD_IDENTITY_LOCK:
        for key in keys:
            ACTIVE_CONTENT_DOWNLOADS.discard(key)



# ============================================================
# METADATA INTELLIGENCE
# ============================================================

_METADATA_CACHE = {}
_METADATA_LAST_MB_CALL = 0.0
_METADATA_RATE_LOCK = asyncio.Lock()
_METADATA_LOOKUP_SEMAPHORE = asyncio.Semaphore(2)
_YOUTUBE_SEARCH_SEMAPHORE = asyncio.Semaphore(2)
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}
YOUTUBE_GENERIC_ARTISTS = {"youtube", "youtube music", "music", "unknown", "unknown artist", "various artists", "vevo", "topic"}


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


def _usable_artist_hint(value):
    text = clean_metadata_text(value, "")
    compact = _compact_identity(text)
    generic = {_compact_identity(x) for x in YOUTUBE_GENERIC_ARTISTS}
    if not text or compact in generic:
        return ""
    text = re.sub(r"\s*[-|•]\s*(official\s+)?(?:topic|vevo)\s*$", "", text, flags=re.I).strip()
    return text


def _source_metadata_from_payload(payload):
    raw_title = clean_metadata_text(payload.get("track") or payload.get("title"), "")
    artist = _usable_artist_hint(payload.get("artist") or payload.get("creator") or payload.get("album_artist"))
    if not artist:
        artist = _usable_artist_hint(payload.get("uploader") or payload.get("channel"))
    title = raw_title
    if title and not artist and " - " in title:
        hinted_artist, hinted_title = [part.strip() for part in title.split(" - ", 1)]
        if _usable_artist_hint(hinted_artist):
            artist, title = hinted_artist, hinted_title
    album = clean_metadata_text(payload.get("album") or payload.get("release_title"), "")
    return {"title": title, "artist": artist, "album": album, "duration": safe_float(payload.get("duration"), 0)}


async def _metadata_http_json(url, headers=None, timeout=12, attempts=3):
    last_error = None
    for attempt in range(max(1, attempts)):
        try:
            return await asyncio.to_thread(_http_json_sync, url, headers or {}, timeout)
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in {429, 500, 502, 503, 504} or attempt >= attempts - 1:
                break
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt >= attempts - 1:
                break
        await asyncio.sleep(min(4.0, 0.75 * (2 ** attempt)))
    if last_error:
        raise last_error
    return {}


async def _musicbrainz_lookup(artist, title, cleanup_rules="", album_hint=""):
    global _METADATA_LAST_MB_CALL
    artist = _usable_artist_hint(artist)
    title = clean_title_with_rules(title, cleanup_rules)
    cache_key = ("mb", _compact_identity(artist), _compact_identity(title), _compact_identity(album_hint))
    if cache_key in _METADATA_CACHE:
        return _METADATA_CACHE[cache_key]
    if not title:
        return None
    queries = [f'artist:"{artist}" AND recording:"{title}"'] if artist else []
    if album_hint:
        queries.insert(0, f'artist:"{artist}" AND recording:"{title}" AND release:"{album_hint}"' if artist else f'recording:"{title}" AND release:"{album_hint}"')
    queries.append(f'recording:"{title}"')
    best = None
    async with _METADATA_LOOKUP_SEMAPHORE:
        for query in queries:
            async with _METADATA_RATE_LOCK:
                wait = 1.05 - (time.monotonic() - _METADATA_LAST_MB_CALL)
                if wait > 0:
                    await asyncio.sleep(wait)
                _METADATA_LAST_MB_CALL = time.monotonic()
                url = "https://musicbrainz.org/ws/2/recording?" + urllib.parse.urlencode({
                    "query": query, "fmt": "json", "limit": 10, "inc": "releases+artist-credits+release-groups"
                })
                try:
                    data = await _metadata_http_json(url, {
                        "User-Agent": f"Xrob-Music/{SERVER_VERSION} (metadata lookup)",
                        "Accept": "application/json",
                    }, timeout=12, attempts=3)
                except Exception as exc:
                    await write_app_error("musicbrainz", _sanitize_external_error(exc, "MusicBrainz lookup failed.", 900))
                    continue
            for rec in data.get("recordings") or []:
                rec_title = clean_metadata_text(rec.get("title"), "")
                credits = rec.get("artist-credit") or []
                rec_artist = "".join(
                    str(item.get("name") or item.get("artist", {}).get("name") or "").strip() + str(item.get("joinphrase") or "")
                    for item in credits
                ).strip()
                title_score = _similarity(title, rec_title)
                artist_score = _similarity(artist, rec_artist) if artist else 0.0
                score = title_score if not artist else title_score * 0.72 + artist_score * 0.28
                releases = rec.get("releases") or []
                def release_score(rel):
                    rel_title=clean_metadata_text(rel.get("title"), "")
                    album_score=_similarity(album_hint, rel_title) if album_hint else 0.0
                    tokens_target=_duplicate_variant_tokens(album_hint)
                    tokens_rel=_duplicate_variant_tokens(rel_title)
                    variant_bonus=0.20 if tokens_target and tokens_target == tokens_rel else 0.0
                    official_bonus=0.10 if str(rel.get("status") or "").casefold() == "official" else 0.0
                    group=rel.get("release-group") or {}
                    primary_bonus=0.05 if str(group.get("primary-type") or "").casefold() == "album" else 0.0
                    secondary=group.get("secondary-types") or []
                    secondary_penalty=0.05 if secondary and not tokens_target else 0.0
                    return album_score*0.70 + variant_bonus + official_bonus + primary_bonus - secondary_penalty
                release=max(releases, key=release_score) if releases else None
                candidate_album=clean_metadata_text((release or {}).get("title"), "")
                candidate_version=""
                if candidate_album:
                    raw_tokens=_duplicate_variant_tokens(candidate_album)
                    candidate_version=" ".join(sorted(raw_tokens))
                candidate = {
                    "title": rec_title, "artist": rec_artist or artist,
                    "album": candidate_album, "album_version": candidate_version,
                    "score": float(score), "source": "MusicBrainz", "id": rec.get("id"),
                }
                artist_ok = not artist or artist_score >= 0.55
                title_ok = title_score >= (0.78 if artist else 0.90)
                if artist_ok and title_ok and (best is None or candidate["score"] > best["score"]):
                    best = candidate
            if best and best["score"] >= 0.90:
                break
    _cache_set_bounded(_METADATA_CACHE, cache_key, best, METADATA_CACHE_MAX)
    return best


async def _itunes_lookup(artist, title, cleanup_rules=""):
    artist = _usable_artist_hint(artist)
    title = clean_title_with_rules(title, cleanup_rules)
    cache_key = ("itunes", _compact_identity(artist), _compact_identity(title))
    if cache_key in _METADATA_CACHE:
        return _METADATA_CACHE[cache_key]
    if not title:
        return None
    term = f"{artist} {title}".strip()
    url = "https://itunes.apple.com/search?" + urllib.parse.urlencode({"term": term, "media": "music", "entity": "song", "limit": 10})
    async with _METADATA_LOOKUP_SEMAPHORE:
        try:
            data = await _metadata_http_json(url, {"User-Agent": f"Xrob Music/{SERVER_VERSION}"}, timeout=10, attempts=3)
        except Exception as exc:
            await write_app_error("itunes", _sanitize_external_error(exc, "Apple catalog lookup failed.", 900))
            _cache_set_bounded(_METADATA_CACHE, cache_key, None, METADATA_CACHE_MAX)
            return None
    best = None
    for item in data.get("results") or []:
        cand_title = clean_metadata_text(item.get("trackName"), "")
        cand_artist = _usable_artist_hint(item.get("artistName"))
        title_score = _similarity(title, cand_title)
        artist_score = _similarity(artist, cand_artist) if artist else 0.0
        score = title_score if not artist else title_score * 0.72 + artist_score * 0.28
        if (artist and artist_score < 0.55) or title_score < 0.78:
            continue
        candidate = {"title": cand_title, "artist": cand_artist or artist, "album": clean_metadata_text(item.get("collectionName"), ""), "score": float(score), "source": "Apple Music catalog", "id": item.get("trackId")}
        if best is None or candidate["score"] > best["score"]:
            best = candidate
    _cache_set_bounded(_METADATA_CACHE, cache_key, best, METADATA_CACHE_MAX)
    return best


async def resolve_source_metadata(url):
    """Ask yt-dlp for source metadata without blindly treating channel/uploader as artist."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *YT_DLP_COMMAND, "--no-playlist", "--skip-download", "--dump-single-json", "--no-warnings", url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(err.decode("utf-8", errors="ignore")[-600:] or "Source metadata lookup failed")
        payload = json.loads(out.decode("utf-8", errors="ignore"))
        result = _source_metadata_from_payload(payload) if isinstance(payload, dict) else {}
        result["_lookup_ok"] = True
        return result
    except Exception as exc:
        safe = _sanitize_external_error(exc, "Source metadata lookup failed.", 900)
        await write_app_error("source_metadata", safe)
        return {"_lookup_ok": False, "_lookup_error": safe}


async def resolve_download_metadata(raw_title, artist, album, settings):
    rules_text = settings.get("title_cleanup_rules", "")
    source = _source_metadata_from_payload({"title": raw_title, "artist": artist, "album": album})
    title = clean_title_with_rules(source.get("title") or "Unknown Track", rules_text)
    artist = _usable_artist_hint(source.get("artist")) or "Unknown Artist"
    supplied_album = clean_metadata_text(source.get("album"), "")
    if artist != "Unknown Artist" and " - " in title:
        left, right = [part.strip() for part in title.split(" - ", 1)]
        if _similarity(left, artist) >= 0.90 and right:
            title = right
    title = normalize_catalog_title(title, rules_text)
    result = {"title": title, "artist": artist, "album": supplied_album or "", "confidence": 0.25, "source": "Supplied metadata", "reason": ""}
    mode = str(settings.get("metadata_mode") or "auto").lower()
    if mode == "off":
        return result
    lookup_tasks = []
    if mode in {"auto", "musicbrainz"}:
        lookup_tasks.append(_musicbrainz_lookup(artist, title, rules_text, supplied_album))
    if mode == "auto":
        lookup_tasks.append(_itunes_lookup(artist, title, rules_text))
    candidates = [c for c in await asyncio.gather(*lookup_tasks, return_exceptions=True) if isinstance(c, dict) and c.get("score", 0.0) >= 0.72]
    if candidates:
        candidates.sort(key=lambda c: float(c.get("score", 0.0)), reverse=True)
        best = candidates[0]
        second_score = float(candidates[1].get("score", 0.0)) if len(candidates) > 1 else 0.0
        best_score = float(best.get("score", 0.0))
        gap = max(0.0, best_score - second_score)
        chosen_album = supplied_album or clean_metadata_text(best.get("album"), "")
        level = _normalize_confidence(best_score, gap)
        result.update({"title": normalize_catalog_title(best.get("title") or title, rules_text), "artist": _usable_artist_hint(best.get("artist")) or artist, "album": chosen_album, "album_version": best.get("album_version", ""), "confidence": best_score, "confidence_level": level, "confidence_gap": gap, "source": best.get("source") or "Catalog", "reason": f"Catalog confidence {best_score:.2f}; {level} confidence; candidate gap {gap:.2f}"})
    return result


# ============================================================
# DOWNLOAD WORKER
# ============================================================

TERMINAL_TASK_STATES = {"completed", "error", "failed", "cancelled", "canceled"}
ACTIVE_TASK_STATES = {"queued", "downloading", "processing", "catalog_pending"}


def _task_cancelled(task):
    return bool(task and task.get("cancel_requested")) or bool(task and str(task.get("status") or "").lower() in {"cancelled", "canceled"})


def _set_task_cancelled(task):
    task["cancel_requested"] = True
    task["status"] = "cancelled"
    task["step"] = "Cancelled"
    task["percent"] = 0
    task["speed"] = ""
    task["last_updated"] = time.time() * 1000


async def _commit_download_to_catalog_locked(final_path: Path):
    """Commit one finalized file while the catalog lock is already held."""
    final_path = Path(final_path)
    if LIBRARY_CATALOG is None:
        raise RuntimeError("Library catalog is not initialized")
    if not await asyncio.to_thread(final_path.is_file):
        raise FileNotFoundError("Finalized download file is missing")
    record = await asyncio.to_thread(LIBRARY_CATALOG.upsert_file, final_path, read_metadata_sync)
    invalidate_library_cache()
    song_id = str(record.get("id") or "")
    # Catalog commit is authoritative. Notification failures must never turn a
    # successfully committed media file back into a failed download.
    try:
        if song_id:
            await asyncio.to_thread(_ensure_song_review_pending_sync, song_id)
    except Exception as exc:
        await write_app_error("song_review", exc)
    try:
        await manager.broadcast({"type": "library_updated", "songId": song_id, "path": str(final_path), "revision": LIBRARY_REVISION})
    except Exception as exc:
        await write_app_error("library_broadcast", exc)
    return song_id


async def refresh_after_download(final_path: Path):
    """Commit one finalized file while blocking concurrent filesystem reconciliation."""
    async with LIBRARY_SCAN_LOCK:
        async with LIBRARY_CATALOG_LOCK:
            return await _commit_download_to_catalog_locked(final_path)


def _ensure_song_review_pending_sync(song_id):
    with db_connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO song_review(song_id,state,actioned_at) VALUES(?,?,0)",
            (str(song_id), "pending"),
        )
        conn.commit()


async def download_worker(stop_event=None):
    """Process download jobs until the worker is asked to drain and exit."""
    stop_event = stop_event or asyncio.Event()

    while True:
        if stop_event.is_set():
            break
        try:
            queue_item = await asyncio.wait_for(TASK_QUEUE.get(), timeout=1.0)
        except asyncio.TimeoutError:
            await _refill_download_queue()
            continue
        if isinstance(queue_item, (tuple, list)) and len(queue_item) == 2:
            task_id, queue_token = queue_item
        else:
            task_id, queue_token = queue_item, None
        QUEUED_TASK_IDS.discard(task_id)

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

            if task.get("status") == "catalog_pending":
                # Catalog-pending tasks are recovered by runtime maintenance, not the download workers.
                continue

            settings = await load_settings_async()
            download_root = task_download_root(task)
            try:
                await asyncio.to_thread(download_root.mkdir, parents=True, exist_ok=True)
                # Persist a validated root for restart-safe cleanup/resume handling.
                task["download_root"] = str(download_root)
            except OSError as exc:
                raise RuntimeError(_sanitize_external_error(exc, "Download storage is unavailable.", 400)) from exc

            storage_ok, storage_error = await asyncio.to_thread(_probe_storage_sync)
            if not storage_ok:
                _persist_storage_state_sync("offline", storage_error)
                task["status"] = "queued"
                task["step"] = "Waiting for music storage..."
                task["error"] = storage_error
                task["queue_token"] = uuid.uuid4().hex
                task["last_updated"] = time.time() * 1000
                await notify_task_update(task, force_save=True)
                await asyncio.sleep(2)
                continue
            _persist_storage_state_sync("online", "")

            if str(task.get("title") or "").strip().casefold() in {"unknown track", "unknown", ""}:
                source_meta = await resolve_source_metadata(str(task.get("url") or ""))
                if source_meta.get("_lookup_ok"):
                    task["title"] = normalize_catalog_title(source_meta.get("title") or task.get("title") or "Unknown Track", settings.get("title_cleanup_rules", ""))
                    task["artist"] = clean_metadata_text(source_meta.get("artist"), task.get("artist") or "Unknown Artist")
                    task["album"] = clean_metadata_text(source_meta.get("album"), task.get("album") or "")
                    task["duration"] = safe_float(source_meta.get("duration"), task.get("duration") or 0)
                    task["album_version"] = clean_metadata_text(source_meta.get("album_version", ""), task.get("album_version") or "")
                    task["content_identity"] = _download_content_identity(task.get("title", ""), task.get("artist", ""), task.get("album", ""), task.get("duration", 0), task.get("album_version", ""))
                    task["identity_version"] = 2
                    url_hash = hashlib.sha256(str(task.get("url", "")).encode("utf-8")).hexdigest()
                    task["identity_key"] = f"{task.get('content_identity')}|{url_hash}" if task.get("content_identity") else f"url:{url_hash}"
                    await notify_task_update(task, force_save=True)

            # Re-check immediately before provider work. A content-identity reservation
            # prevents two different source URLs for the same song from downloading in parallel.
            content_identity = str(task.get("content_identity") or "")
            reserved_identity = False
            if content_identity:
                async with DOWNLOAD_IDENTITY_LOCK:
                    if content_identity in ACTIVE_CONTENT_DOWNLOADS:
                        task["status"] = "queued"
                        task["step"] = "Waiting for duplicate check..."
                        task["queue_token"] = uuid.uuid4().hex
                        task["last_updated"] = time.time() * 1000
                        await notify_task_update(task, force_save=False)
                        await asyncio.sleep(1.0)
                        QUEUED_TASK_IDS.add(task_id)
                        TASK_QUEUE.put_nowait((task_id, task["queue_token"]))
                        continue
                    ACTIVE_CONTENT_DOWNLOADS.add(content_identity)
                    reserved_identity = True

            existing = await find_existing_track(
                task.get("title", "Unknown Track"),
                task.get("artist", "Unknown Artist"),
                task.get("album", ""),
                task.get("duration") or 0,
            )
            if existing:
                task["status"] = "completed"
                task["percent"] = 100
                task["speed"] = ""
                task["step"] = "Already in library"
                task["final_name"] = existing
                task["last_updated"] = time.time() * 1000
                if reserved_identity:
                    async with DOWNLOAD_IDENTITY_LOCK:
                        ACTIVE_CONTENT_DOWNLOADS.discard(content_identity)
                    reserved_identity = False
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

            output_template = str(download_root / f"{task_id}.%(ext)s")

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
                "--retries", "3",
                "--fragment-retries", "3",
                "--socket-timeout", "15",
                "--retry-sleep", "exp=1:3",
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
                        for pattern in (f"{task_id}.*", f"clean_{task_id}.*"):
                            if any(path.is_file() for path in download_root.glob(pattern) if path.suffix.lower() in {".part", ".ytdl", ".temp"}):
                                return True
                        return False
                    except OSError:
                        return False
                task["resume_available"] = await asyncio.to_thread(partial_exists_sync)
                task["status"] = "error"
                task["step"] = "Download failed — retry available" if task.get("resume_available") else "Download failed"
                task["error"] = _sanitize_external_error(error_text[-1200:], "yt-dlp failed.", 900)
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
                    await _refill_download_queue()
                continue

            def find_downloaded_files_sync():
                try:
                    candidates = list(download_root.glob(f"{task_id}.*"))
                except OSError:
                    return []
                audio = [path for path in candidates if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS]
                preferred = [path for path in audio if path.suffix.lower() == f".{fmt.lower()}"]
                return preferred + [path for path in audio if path not in preferred]

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
            valid_media, validation = await asyncio.to_thread(_audio_validation_sync, audio_file)
            if not valid_media:
                task["status"] = "error"
                task["step"] = "Media validation failed"
                task["error"] = _sanitize_external_error(validation, "Downloaded media failed validation.", 500)
                task["last_updated"] = time.time() * 1000
                await notify_task_update(task, force_save=True)
                await asyncio.to_thread(cleanup_task_files, task_id, download_root if 'download_root' in locals() else task_download_root(task))
                continue
            if isinstance(validation, dict) and validation.get("duration"):
                task["duration"] = float(validation["duration"])

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
            if _task_cancelled(task):
                await asyncio.to_thread(cleanup_task_files, task_id, download_root if 'download_root' in locals() else task_download_root(task))
                _set_task_cancelled(task)
                await notify_task_update(task, force_save=True)
                continue
            task["metadata_confidence"] = round(float(resolved.get("confidence", 0.0)) * 100)
            task["metadata_confidence_level"] = resolved.get("confidence_level", "low")
            task["metadata_confidence_gap"] = round(float(resolved.get("confidence_gap", 0.0)) * 100)
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

                clean_file = download_root / f"clean_{task_id}{extension}"
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
                    await asyncio.to_thread(cleanup_task_files, task_id, download_root if 'download_root' in locals() else task_download_root(task))
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
                    valid_media, validation = await asyncio.to_thread(_audio_validation_sync, audio_file)
                    if not valid_media:
                        await asyncio.to_thread(cleanup_task_files, task_id, download_root if 'download_root' in locals() else task_download_root(task))
                        raise RuntimeError(_sanitize_external_error(validation, "Final media validation failed.", 500))
                else:
                    # Keep the downloaded file when metadata rewriting fails; the
                    # download itself is still usable.
                    print(
                        "Warning: metadata rewrite failed:",
                        clean_stderr.decode("utf-8", errors="ignore")[-1000:],
                    )

            # Re-read the actual finalized tags after metadata normalization so the
            # final duplicate check uses the same identity the catalog will receive.
            try:
                final_meta = await asyncio.to_thread(read_metadata_sync, audio_file)
                if isinstance(final_meta, dict):
                    task["title"] = normalize_catalog_title(final_meta.get("title") or task.get("title") or "Unknown Track", settings.get("title_cleanup_rules", ""))
                    task["artist"] = clean_metadata_text(final_meta.get("artist") or task.get("artist") or "Unknown Artist", "Unknown Artist")
                    task["album"] = clean_metadata_text(final_meta.get("album") or task.get("album") or "", "")
                    task["album_version"] = clean_metadata_text(final_meta.get("album_version") or task.get("album_version") or "", "")
                    actual_duration = safe_float(final_meta.get("duration"), 0)
                    if actual_duration:
                        task["duration"] = actual_duration
            except Exception as meta_exc:
                await write_app_error("download_final_metadata", _sanitize_external_error(meta_exc, "Final metadata read failed."), task_id)

            artist = clean_filename(task.get("artist", "Unknown Artist"))

            task["status"] = "processing"
            task["percent"] = 99
            task["step"] = "Adding to library..."
            task["last_updated"] = time.time() * 1000
            await notify_task_update(task, force_save=True)

            async with DOWNLOAD_GUARD:
                # The final normalized identity becomes the reservation identity.
                # This is done before the final duplicate check so a second worker
                # cannot enter the same normalized track while we finalize.
                final_identity = _download_content_identity(
                    task.get("title", ""),
                    task.get("artist", ""),
                    task.get("album", ""),
                    task.get("duration", 0),
                    task.get("album_version", ""),
                )
                old_identity = content_identity
                async with LIBRARY_SCAN_LOCK:
                    async with LIBRARY_CATALOG_LOCK:
                        # Switch to the final normalized identity before consulting the
                            # catalog. DOWNLOAD_GUARD prevents another local worker from
                            # interleaving this finalization, while the identity reservation
                            # prevents a separately queued source from finalizing the same
                            # normalized track at the same time. Holding the scan lock also
                            # prevents a concurrent filesystem reconciliation from seeing a
                            # pre-commit snapshot and marking the newly finalized file missing.
                        reservation_ok = await _switch_content_identity_reservation(old_identity, final_identity)
                        if not reservation_ok:
                            conflict_id = next((str(other.get("id")) for other in TASKS.values() if str(other.get("content_identity") or "") == final_identity and str(other.get("id")) != task_id and other.get("status") in ACTIVE_TASK_STATES), "")
                            await asyncio.to_thread(cleanup_task_files, task_id, download_root)
                            task["status"] = "completed"
                            task["percent"] = 100
                            task["speed"] = ""
                            task["step"] = "Duplicate download removed"
                            task["error"] = ""
                            task["duplicate_of"] = {"task_id": conflict_id, "identity": final_identity}
                            task["final_name"] = ""
                            task["catalog_pending_path"] = ""
                            task["last_updated"] = time.time() * 1000
                            await notify_task_update(task, force_save=True)
                            await _release_content_identity_reservations(old_identity, final_identity)
                            reserved_identity = False
                            content_identity = final_identity
                            continue

                        content_identity = final_identity
                        reserved_identity = bool(final_identity)
                        task["content_identity"] = final_identity
                        task["identity_version"] = 2
                        task["identity_key"] = f"{final_identity}|{hashlib.sha256(str(task.get('url', '')).encode('utf-8')).hexdigest()}" if final_identity else f"url:{hashlib.sha256(str(task.get('url', '')).encode('utf-8')).hexdigest()}"
                        await notify_task_update(task, force_save=True)

                        final_duplicate = await find_existing_track(
                            task.get("title", ""),
                            task.get("artist", ""),
                            task.get("album", ""),
                            task.get("duration", 0),
                            task.get("album_version", ""),
                        )
                        if final_duplicate:
                            await asyncio.to_thread(cleanup_task_files, task_id, download_root)
                            task["status"] = "completed"
                            task["percent"] = 100
                            task["speed"] = ""
                            task["step"] = "Already in library — duplicate removed"
                            task["error"] = ""
                            task["duplicate_of"] = {
                                "id": final_duplicate.get("id", ""),
                                "path": final_duplicate.get("path", ""),
                                "title": final_duplicate.get("title", ""),
                                "artist": final_duplicate.get("artist", ""),
                            }
                            task["final_name"] = ""
                            task["catalog_pending_path"] = ""
                            task["last_updated"] = time.time() * 1000
                            await notify_task_update(task, force_save=True)
                            await _release_content_identity_reservations(old_identity, final_identity)
                            reserved_identity = False
                            content_identity = final_identity
                            continue

                        task["catalog_pending_path"] = ""

                        if _task_cancelled(task):
                            await asyncio.to_thread(cleanup_task_files, task_id, download_root)
                            _set_task_cancelled(task)
                            await notify_task_update(task, force_save=True)
                            continue

                        if settings.get("organize_by_artist", False):
                            final_dir = download_root / artist
                        else:
                            final_dir = download_root
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
                                art_candidates=[p for p in download_root.glob(f"{task_id}.*") if p.is_file() and p.suffix.lower() in {".jpg",".jpeg",".png",".webp"}]
                                if art_candidates:
                                    art_target=final_path.with_suffix(".jpg")
                                    await asyncio.to_thread(shutil.move, str(art_candidates[0]), str(art_target))
                            except Exception as art_exc:
                                await write_app_error("artwork", art_exc, task_id)

                        # The file is now physically finalized but not yet considered a
                        # completed download. Keep a durable pending path until SQLite
                        # accepts the catalog row. This is the durable commit boundary: a
                        # restart can retry the catalog commit without re-downloading.
                        task["final_name"] = str(final_path.relative_to(DOWNLOAD_DIR))
                        task["catalog_pending_path"] = str(final_path)
                        task["status"] = "catalog_pending"
                        task["percent"] = 100
                        task["speed"] = ""
                        task["step"] = "Committing to library..."
                        task["error"] = ""
                        task["last_updated"] = time.time() * 1000
                        await notify_task_update(task, force_save=True)

                        try:
                            await _commit_download_to_catalog_locked(final_path)
                        except Exception as commit_exc:
                            safe = _sanitize_external_error(commit_exc, "Library commit failed; retrying automatically.", 700)
                            task["status"] = "catalog_pending"
                            task["step"] = "Library commit pending"
                            task["error"] = safe
                            task["last_updated"] = time.time() * 1000
                            await notify_task_update(task, force_save=True)
                            await write_app_error("library_commit_pending", safe, task_id)
                            # Keep the reservation and final file; runtime maintenance will retry.
                            reserved_identity = bool(final_identity)
                            continue

                        # Only after the catalog commit succeeds is the download terminal.
                        task["catalog_pending_path"] = ""
                        task["status"] = "completed"
                        task["percent"] = 100
                        task["speed"] = ""
                        task["step"] = "Ready"
                        task["error"] = ""
                        task["last_updated"] = time.time() * 1000
                        await notify_task_update(task, force_save=True)

            METADATA_CACHE.pop(str(final_path), None)
            if reserved_identity and content_identity:
                async with DOWNLOAD_IDENTITY_LOCK:
                    ACTIVE_CONTENT_DOWNLOADS.discard(content_identity)
                reserved_identity = False

        except asyncio.CancelledError:
            if reserved_identity and content_identity:
                async with DOWNLOAD_IDENTITY_LOCK:
                    ACTIVE_CONTENT_DOWNLOADS.discard(content_identity)
            raise

        except Exception as error:

            if reserved_identity and content_identity:
                async with DOWNLOAD_IDENTITY_LOCK:
                    ACTIVE_CONTENT_DOWNLOADS.discard(content_identity)
                reserved_identity = False

            task = TASKS.get(task_id)

            ACTIVE_PROCESSES.pop(
                task_id,
                None,
            )

            if task and not task.get("resume_available"):
                try:
                    task_root_for_error = download_root if 'download_root' in locals() else task_download_root(task)
                    task["resume_available"] = any(path.is_file() for path in task_root_for_error.glob(f"{task_id}.*") if path.suffix.lower() in {".part", ".ytdl"})
                except (OSError, RuntimeError, ValueError):
                    task_root_for_error = download_root if 'download_root' in locals() else DOWNLOAD_DIR
                    task["resume_available"] = False
            else:
                task_root_for_error = download_root if 'download_root' in locals() else DOWNLOAD_DIR
            if task and not task.get("resume_available"):
                await asyncio.to_thread(cleanup_task_files, task_id, task_root_for_error)

            if task:

                task["status"] = "error"
                task["step"] = (
                    "Unexpected error"
                )
                task["error"] = _sanitize_external_error(error, "Download failed.", 900)
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
            try:
                await _maybe_prune_tasks()
            except Exception as housekeeping_exc:
                await write_app_error("task_prune", str(housekeeping_exc))
            try:
                await _refill_download_queue()
            except Exception as queue_exc:
                await write_app_error("queue_refill", str(queue_exc))


# ============================================================
# STARTUP
# ============================================================

async def startup_event():

    await asyncio.to_thread(configure_storage)
    await asyncio.to_thread(init_db)
    global STORAGE_STATE_DB_READY, LIBRARY_CATALOG, SCHEDULED_SCANNER_TASK, LIBRARY_WARMUP_TASK, LIBRARY_HEALTH_TASK, QUEUED_TASK_IDS, LIBRARY_REVISION
    STORAGE_STATE_DB_READY = True
    LIBRARY_REVISION = await asyncio.to_thread(_load_library_revision_sync)
    if LIBRARY_CATALOG is not None:
        try:
            await asyncio.to_thread(LIBRARY_CATALOG.migrate_legacy_index)
        except Exception as exc:
            await write_app_error("library_migration", exc)
    await asyncio.to_thread(storage_info_sync)
    await asyncio.to_thread(_ensure_secure_web_credentials_sync)
    await _maybe_prune_tasks(force=True)

    global TASKS

    TASKS = await asyncio.to_thread(
        db_load_tasks_sync
    )

    # A catalog-pending task already owns the durable finalized file. Rebuild its
    # in-memory identity reservation before any new download request can pass preflight.
    async with DOWNLOAD_IDENTITY_LOCK:
        for pending_task in TASKS.values():
            if pending_task.get("status") == "catalog_pending":
                pending_identity = str(pending_task.get("content_identity") or "")
                if pending_identity:
                    ACTIVE_CONTENT_DOWNLOADS.add(pending_identity)

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
                task["resume_available"] = any(path.is_file() for path in task_download_root(task).glob(f"{task['id']}.*") if path.suffix.lower() in {".part", ".ytdl"})
            except (OSError, RuntimeError, ValueError):
                task["resume_available"] = False
            if int(task.get("identity_version") or 0) < 2:
                task["content_identity"] = _download_content_identity(task.get("title", ""), task.get("artist", ""), task.get("album", ""), task.get("duration", 0))
                task["identity_version"] = 2
            if not task.get("content_identity"):
                task["content_identity"] = _download_content_identity(task.get("title", ""), task.get("artist", ""), task.get("album", ""), task.get("duration", 0))
            url_hash = hashlib.sha256(str(task.get("url", "")).encode("utf-8")).hexdigest()
            task["identity_key"] = f"{task.get('content_identity')}|{url_hash}" if task.get("content_identity") else f"url:{url_hash}"

            await db_save_task(
                task,
                force=True,
            )

    startup_seen_content_identities = set()

    # Rebuild v2 identities for active pre-download tasks and collapse same-track races.
    # Durable catalog-pending tasks already own finalized files and identities; never
    # rewrite/collapse them during startup or their post-restart commit reservation can drift.
    for task in sorted(TASKS.values(), key=lambda item: safe_float(item.get("created_at", 0), 0)):
        status = str(task.get("status") or "").lower()
        if status not in ACTIVE_TASK_STATES:
            continue
        if status == "catalog_pending":
            if task.get("content_identity"):
                startup_seen_content_identities.add(str(task.get("content_identity")))
            continue
        identity = _download_content_identity(
            task.get("title", ""), task.get("artist", ""), task.get("album", ""),
            task.get("duration", 0), task.get("album_version", "")
        )
        if identity and identity in startup_seen_content_identities:
            task["status"] = "cancelled"
            task["step"] = "Duplicate active download suppressed"
            task["error"] = "An active download for this track already exists."
            task["cancel_requested"] = False
            task["last_updated"] = time.time() * 1000
        else:
            if identity:
                startup_seen_content_identities.add(identity)
            task["content_identity"] = identity
            task["identity_version"] = 2
            url_hash = hashlib.sha256(str(task.get("url", "")).encode("utf-8")).hexdigest()
            task["identity_key"] = f"{identity}|{url_hash}" if identity else f"url:{url_hash}"
        await db_save_task(task, force=True)

    global LIBRARY_WARMUP_TASK, LIBRARY_HEALTH_TASK, DOWNLOAD_WORKER_TASKS, SCHEDULED_SCANNER_TASK, QUEUED_TASK_IDS, RUNTIME_MAINTENANCE_TASK
    QUEUED_TASK_IDS.clear()
    settings = await load_settings_async()
    await resize_download_workers(settings.get("max_concurrent_downloads", MAX_CONCURRENT_DOWNLOADS))

    if SCHEDULED_SCANNER_TASK is None or SCHEDULED_SCANNER_TASK.done():
        SCHEDULED_SCANNER_TASK = asyncio.create_task(scheduled_library_scanner())

    if LIBRARY_WARMUP_TASK is None or LIBRARY_WARMUP_TASK.done():
        LIBRARY_WARMUP_TASK = asyncio.create_task(background_library_warmup())

    if LIBRARY_HEALTH_TASK is None or LIBRARY_HEALTH_TASK.done():
        LIBRARY_HEALTH_TASK = asyncio.create_task(background_library_health_scanner())

    if RUNTIME_MAINTENANCE_TASK is None or RUNTIME_MAINTENANCE_TASK.done():
        RUNTIME_MAINTENANCE_TASK = asyncio.create_task(runtime_maintenance_loop())

    await _refill_download_queue()


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
        "openSubsonic": bool(subsonic_credentials()[1]),
        "storage": {
            "state": STORAGE_STATE,
            "online": STORAGE_STATE == "online",
            "error": STORAGE_ERROR,
            "checked_at": STORAGE_LAST_CHECKED_AT,
        },
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
        raise HTTPException(status_code=400, detail=_sanitize_external_error(exc, "Invalid settings.")) from exc
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

YOUTUBE_SEARCH_CACHE = {}
YOUTUBE_SEARCH_CACHE_LOCK = asyncio.Lock()
YOUTUBE_SEARCH_CACHE_TTL = 60.0
YOUTUBE_SEARCH_CACHE_MAX = 128


async def youtube_search(
    query,
    max_results,
    page=1,
):
    query = re.sub(r"\s+", " ", str(query or "")).strip()[:160]
    if not query:
        return []
    max_results = max(1, min(50, safe_int(max_results, 20)))
    page = max(1, min(20, safe_int(page, 1)))
    start = (page - 1) * max_results + 1
    end = min(page * max_results, 400)
    if start > end:
        return []
    cache_key = (query.casefold(), int(max_results), int(page))
    now_mono = time.monotonic()
    async with YOUTUBE_SEARCH_CACHE_LOCK:
        cached = YOUTUBE_SEARCH_CACHE.get(cache_key)
        if cached and now_mono - cached[0] < YOUTUBE_SEARCH_CACHE_TTL:
            return [dict(item) for item in cached[1]]
        stale_keys = [key for key, value in YOUTUBE_SEARCH_CACHE.items() if now_mono - value[0] >= YOUTUBE_SEARCH_CACHE_TTL]
        for key in stale_keys:
            YOUTUBE_SEARCH_CACHE.pop(key, None)
    command = [
        *YT_DLP_COMMAND, "--flat-playlist", "--dump-single-json", "--skip-download", "--no-warnings",
        "--retries", "3", "--socket-timeout", "15",
        "--playlist-start", str(start), "--playlist-end", str(end), f"ytsearch{end}:{query}",
    ]
    async with _YOUTUBE_SEARCH_SEMAPHORE:
        try:
            process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except FileNotFoundError as exc:
            raise RuntimeError("yt-dlp is not installed in the Xrob Music container. Rebuild the add-on so requirements.txt is installed.") from exc
        stdout, stderr = await communicate_with_timeout(process, SUBPROCESS_TIMEOUT_SECONDS, "YouTube search")
    if process.returncode != 0:
        raise RuntimeError(_sanitize_external_error(stderr.decode("utf-8", errors="ignore")[-2000:], "yt-dlp search failed.", 900))
    try:
        data = json.loads(stdout.decode("utf-8", errors="ignore"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("Invalid YouTube search response.") from exc
    results = []
    for item in data.get("entries", []) or []:
        if not item:
            continue
        video_id = str(item.get("id") or "").strip()
        if not video_id:
            continue
        raw_title = clean_metadata_text(item.get("title"), "Unknown Track")
        artist = _usable_artist_hint(item.get("artist") or item.get("creator") or item.get("uploader") or item.get("channel"))
        title = normalize_catalog_title(raw_title)
        if not artist and " - " in raw_title:
            left, right = [part.strip() for part in raw_title.split(" - ", 1)]
            hinted = _usable_artist_hint(left)
            if hinted:
                artist, title = hinted, normalize_catalog_title(right)
        duration = safe_int(item.get("duration"), 0)
        results.append({
            "id": video_id, "title": title or "Unknown Track", "raw_title": raw_title,
            "artist": artist or "Unknown Artist",
            "channel": clean_metadata_text(item.get("channel") or item.get("uploader"), ""),
            "duration": duration, "duration_text": format_duration(duration),
            "thumbnail": item.get("thumbnail") or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            "url": f"https://www.youtube.com/watch?v={video_id}", "source": "youtube",
        })
    async with YOUTUBE_SEARCH_CACHE_LOCK:
        YOUTUBE_SEARCH_CACHE[cache_key] = (time.monotonic(), [dict(item) for item in results])
        while len(YOUTUBE_SEARCH_CACHE) > YOUTUBE_SEARCH_CACHE_MAX:
            oldest = min(YOUTUBE_SEARCH_CACHE.items(), key=lambda pair: pair[1][0])[0]
            YOUTUBE_SEARCH_CACHE.pop(oldest, None)
    return results


def _search_duplicate_state_sync(items, tasks):
    active_by_url = set()
    active_by_identity = set()
    for task in tasks.values() if isinstance(tasks, dict) else []:
        if str(task.get("status") or "").lower() not in ACTIVE_TASK_STATES:
            continue
        url = str(task.get("url") or "").strip()
        if url: active_by_url.add(url)
        key = str(task.get("content_identity") or "")
        if int(task.get("identity_version") or 0) < 2 or not key:
            key = _download_content_identity(task.get("title", ""), task.get("artist", ""), task.get("album", ""), task.get("duration", 0), task.get("album_version", ""))
        if key: active_by_identity.add(key)

    def alternatives(item):
        pairs = [(item.get("title", ""), item.get("artist", ""))]
        raw = str(item.get("raw_title") or "")
        if " - " in raw:
            left, right = [part.strip() for part in raw.split(" - ", 1)]
            pairs.append((right, left))
        channel = _usable_artist_hint(item.get("channel"))
        if channel:
            pairs.append((item.get("title", ""), channel))
        return pairs

    decorated = []
    for item in items or []:
        row = dict(item)
        best = None
        match_score = 0.0
        for title, artist in alternatives(row):
            candidates = LIBRARY_CATALOG.duplicate_candidates(title, artist, row.get("album", ""), row.get("duration", 0), 160) if LIBRARY_CATALOG is not None else []
            for candidate in candidates:
                md = LibraryCatalog._metadata_from_row(candidate)
                existing = {
                    "id": str(candidate.get("id") or ""),
                    "path": str(candidate.get("relative_path") or ""),
                    "title": md.get("title") or Path(str(candidate.get("relative_path") or "")).stem,
                    "artist": md.get("artist") or "Unknown Artist",
                    "album": md.get("album") or "",
                    "duration": safe_float(md.get("duration"), 0),
                    "album_version": md.get("album_version") or candidate.get("variant_key") or "",
                }
                score, breakdown = _candidate_duplicate_score({"title": title, "artist": artist, "album": row.get("album", ""), "duration": row.get("duration", 0), "album_version": row.get("album_version", "")}, existing)
                if best is None or score > match_score:
                    best, match_score = {**existing, "breakdown": breakdown}, score
        duration_close = True
        if best is not None and safe_float(row.get("duration"), 0) and safe_float(best.get("duration"), 0):
            duration_close = abs(safe_float(row.get("duration"), 0) - safe_float(best.get("duration"), 0)) <= DUPLICATE_DURATION_TOLERANCE_SECONDS
        target_variants = _duplicate_variant_tokens(row.get("title", "")) | _duplicate_variant_tokens(row.get("album", ""))
        match_variants = (_duplicate_variant_tokens(best.get("title", "")) | _duplicate_variant_tokens(best.get("album", ""))) if best else set()
        variant_compatible = target_variants == match_variants
        exact_enough = bool(best and duration_close and variant_compatible and match_score >= 0.92)
        possible = bool(best and not exact_enough and match_score >= 0.84 and duration_close)
        row["already_downloaded"] = exact_enough
        row["possible_match"] = possible
        row["match_confidence"] = round(float(match_score), 3) if best else 0.0
        row["library_match"] = best or None
        row["library_match_reason"] = "strong metadata match" if exact_enough else ("possible title/artist/edition match" if possible else "")
        active_identity = _download_content_identity(row.get("title", ""), row.get("artist", ""), row.get("album", ""), row.get("duration", 0), row.get("album_version", ""))
        row["already_queued"] = bool((str(row.get("url") or "") in active_by_url) or (active_identity and active_identity in active_by_identity))
        row["download_state"] = "library" if exact_enough else ("possible" if possible else ("queued" if row["already_queued"] else "available"))
        decorated.append(row)
    return decorated


@app.get("/api/search")
async def api_search(
    request: Request,
    q: str = Query(""),
    source: str = Query("youtube"),
    page: int = Query(1, ge=1, le=20),
    limit: int = Query(20, ge=1, le=50),
):
    """Search external music sources used by the web UI.

    The browser expects a plain JSON array for infinite scrolling. Keep that
    contract stable and translate provider/runtime failures into actionable HTTP
    errors instead of leaking a generic 500/404 response.
    """
    query = re.sub(r"\s+", " ", str(q or "")).strip()[:160]
    provider = str(source or "youtube").strip().lower()

    if not query:
        return []

    await _enforce_rate_limit(request, "search", *SEARCH_RATE_LIMIT)

    if provider not in {"youtube", "yt", "yt-dlp"}:
        raise HTTPException(status_code=400, detail=f"Unsupported search source: {provider}")

    try:
        results = await youtube_search(query, limit, page)
    except RuntimeError as exc:
        message = _sanitize_external_error(exc, "YouTube search is unavailable.", 900)
        raise HTTPException(status_code=503, detail=message) from exc
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        safe = _sanitize_external_error(exc, "YouTube search failed.", 900)
        await write_app_error("youtube_search", safe)
        raise HTTPException(status_code=502, detail=safe) from exc

    # Preserve stable ordering while preventing a duplicated video from ever
    # appearing twice when yt-dlp/provider pagination changes between requests.
    seen = set()
    cleaned = []
    for item in results or []:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "").strip()
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        cleaned.append(item)

    decorated = await asyncio.to_thread(_search_duplicate_state_sync, cleaned, TASKS)
    response = JSONResponse(decorated, headers={"X-Search-Has-More": "1" if page < 20 and len(decorated) >= limit else "0"})
    return response


# ============================================================
def _canonicalize_media_url(value):
    parsed = urllib.parse.urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in YOUTUBE_HOSTS:
        raise HTTPException(status_code=400, detail="Only YouTube media URLs are supported")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="Credential-bearing media URLs are not allowed")
    if parsed.port not in (None, 80, 443):
        raise HTTPException(status_code=400, detail="Custom URL ports are not allowed")
    if parsed.fragment:
        raise HTTPException(status_code=400, detail="URL fragments are not supported")

    video_id = ""
    path = parsed.path or ""
    if host in {"youtu.be", "www.youtu.be"}:
        video_id = path.strip("/").split("/", 1)[0]
    elif path.startswith("/shorts/") or path.startswith("/live/"):
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 2:
            video_id = parts[1]
    elif path == "/watch" or path in {"/", ""}:
        video_id = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
    else:
        raise HTTPException(status_code=400, detail="Unsupported YouTube URL format")

    video_id = re.sub(r"[^A-Za-z0-9_-].*$", "", str(video_id or ""))[:64]
    if not video_id or not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", video_id):
        raise HTTPException(status_code=400, detail="A valid YouTube video URL is required")
    return f"https://www.youtube.com/watch?v={video_id}"


def validate_media_url(raw_url):
    value = str(raw_url or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="URL missing")
    if len(value) > PLAYER_STATE_MAX_URL:
        raise HTTPException(status_code=400, detail="URL is too long")
    if any(ord(ch) < 32 for ch in value[:4096]):
        raise HTTPException(status_code=400, detail="Invalid URL")
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid URL") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Only HTTP(S) media URLs are supported")
    return _canonicalize_media_url(value)


# PREVIEW
# ============================================================

@app.get("/api/preview")
async def api_preview(
    request: Request,
    url: str = Query(...),
):

    await _enforce_rate_limit(request, "preview", *PREVIEW_RATE_LIMIT)
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
                "Preview unavailable."
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
# MEDIA VALIDATION / DUPLICATE ENGINE
# ============================================================

def _audio_validation_sync(path: Path):
    path = Path(path)
    if not path.is_file():
        return False, "Downloaded media file was not created."
    try:
        if path.stat().st_size <= 0:
            return False, "Downloaded media file is empty."
    except OSError as exc:
        return False, f"Downloaded media file cannot be read: {exc}"
    try:
        command = [*FFPROBE_COMMAND, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_type,duration", "-show_entries", "format=duration", "-of", "json", str(path)]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
        if result.returncode != 0:
            return False, _sanitize_external_error(result.stderr, "Downloaded audio failed media validation.", 500)
        payload = json.loads(result.stdout or "{}")
        streams = payload.get("streams") or []
        if not streams or streams[0].get("codec_type") != "audio":
            return False, "Downloaded file does not contain a valid audio stream."
        duration = safe_float(streams[0].get("duration") or (payload.get("format") or {}).get("duration"), 0)
        if duration < 0:
            return False, "Downloaded audio has invalid duration."
        return True, {"duration": duration}
    except subprocess.TimeoutExpired:
        return False, "Downloaded media validation timed out."
    except Exception as exc:
        return False, _sanitize_external_error(exc, "Downloaded media validation failed.", 500)


def _duplicate_variant_tokens(value):
    text = _compact_identity(value)
    return set(re.findall(r"\b(?:deluxe|expanded|anniversary|edition|remaster|remastered|live|acoustic|instrumental|karaoke|radio|single|album|bonus|explicit|clean|demo|mix|version|edit)\b", text))


def _candidate_duplicate_score(target, existing):
    title_score = _similarity(target.get("title", ""), existing.get("title", ""))
    artist_score = _similarity(target.get("artist", ""), existing.get("artist", ""))
    album_score = _similarity(target.get("album", ""), existing.get("album", "")) if target.get("album") and existing.get("album") else 0.0
    duration_a = safe_float(target.get("duration"), 0)
    duration_b = safe_float(existing.get("duration"), 0)
    duration_score = 0.0
    if duration_a > 0 and duration_b > 0:
        delta = abs(duration_a - duration_b)
        duration_score = max(0.0, 1.0 - min(delta, 10.0) / 10.0)
    variant_penalty = 0.0
    if _duplicate_variant_tokens(target.get("title")) != _duplicate_variant_tokens(existing.get("title")):
        variant_penalty += 0.22
    if _duplicate_variant_tokens(target.get("album")) != _duplicate_variant_tokens(existing.get("album")):
        variant_penalty += 0.10
    if _duplicate_variant_tokens(target.get("album_version")) != _duplicate_variant_tokens(existing.get("album_version")):
        variant_penalty += 0.12
    score = title_score * 0.45 + artist_score * 0.30 + album_score * 0.10 + duration_score * 0.15 - variant_penalty
    return max(0.0, min(1.0, score)), {
        "title": title_score, "artist": artist_score, "album": album_score,
        "duration": duration_score, "variant_penalty": variant_penalty,
    }


def find_strong_duplicate_sync(title, artist, album="", duration=0, album_version=""):
    target = {"title": title, "artist": artist, "album": album, "duration": duration, "album_version": album_version}
    candidates = LIBRARY_CATALOG.duplicate_candidates(title, artist, album, duration, 160) if LIBRARY_CATALOG is not None else []
    best = None
    for row in candidates:
        md = LibraryCatalog._metadata_from_row(row)
        rel = str(row.get("relative_path") or "")
        candidate = {
            "id": str(row.get("id") or ""),
            "relative_path": rel,
            "title": md.get("title") or Path(rel).stem,
            "artist": md.get("artist") or "Unknown Artist",
            "album": md.get("album") or "",
            "album_version": md.get("album_version") or row.get("variant_key") or "",
            "duration": safe_float(md.get("duration"), 0),
        }
        score, breakdown = _candidate_duplicate_score(target, candidate)
        if breakdown["title"] < DUPLICATE_TITLE_THRESHOLD or breakdown["artist"] < DUPLICATE_ARTIST_THRESHOLD:
            continue
        target_variants = _duplicate_variant_tokens(target.get("title")) | _duplicate_variant_tokens(target.get("album")) | _duplicate_variant_tokens(target.get("album_version"))
        candidate_variants = _duplicate_variant_tokens(candidate.get("title")) | _duplicate_variant_tokens(candidate.get("album")) | _duplicate_variant_tokens(candidate.get("album_version"))
        if target_variants != candidate_variants:
            continue
        if target.get("album") and candidate.get("album") and breakdown["album"] < DUPLICATE_ALBUM_THRESHOLD:
            continue
        duration_close = not duration or not candidate["duration"] or abs(float(duration) - candidate["duration"]) <= DUPLICATE_DURATION_TOLERANCE_SECONDS
        if not duration_close:
            continue
        if best is None or score > best["score"]:
            best = {"file": rel, "id": candidate["id"], "score": score, "breakdown": breakdown}
    return best


def strong_duplicate_report_sync(limit=100):
    rows = [r for r in (LIBRARY_CATALOG.rows() if LIBRARY_CATALOG is not None else []) if not int(r.get("missing") or 0)]
    groups = defaultdict(list)
    hashes = {}
    for row in rows:
        fp = str(row.get("fingerprint") or "")
        if fp:
            groups[(int(row.get("size") or 0), fp)].append(row)
    duplicate_groups = []
    for key, group in groups.items():
        if len(group) < 2:
            continue
        verified = []
        for row in group:
            rel = str(row.get("relative_path") or "")
            path = DOWNLOAD_DIR / rel
            if not path.is_file():
                continue
            try:
                sh = str(row.get("strong_hash") or "")
                if not sh:
                    sh = strong_file_hash(path)
                    hashes[str(row["id"])] = sh
                verified.append((row, sh))
            except OSError:
                continue
        by_hash = defaultdict(list)
        for row, sh in verified:
            by_hash[sh].append(row)
        for sh, exact in by_hash.items():
            if len(exact) > 1:
                duplicate_groups.append({
                    "strong_hash": sh,
                    "files": [str(r.get("relative_path") or "") for r in exact],
                    "ids": [str(r.get("id") or "") for r in exact],
                    "count": len(exact),
                    "verified": True,
                })
    if hashes and LIBRARY_CATALOG is not None:
        with LIBRARY_CATALOG._connect() as conn:
            for sid, sh in hashes.items():
                conn.execute("UPDATE library_songs SET strong_hash=?,updated_at=? WHERE id=?", (sh, time.time(), sid))
            conn.commit()
    return duplicate_groups[:max(1, int(limit))]


def _normalize_confidence(score, gap=0.0):
    score = max(0.0, min(1.0, float(score or 0)))
    if score >= 0.95 and gap >= 0.05:
        return "high"
    if score >= 0.95 and gap <= 0.001:
        return "high"
    if score >= 0.84 and (gap >= 0.03 or gap <= 0.001):
        return "medium"
    return "low"


# ============================================================
# DOWNLOAD API
# ============================================================

def find_existing_track_fast_sync(title, artist, album="", duration=0, album_version=""):
    """Use multi-field identity and fail closed when the catalog cannot be checked."""
    match = find_strong_duplicate_sync(title, artist, album, duration, album_version)
    return match.get("file") if match else None


async def find_existing_track(title, artist, album="", duration=0, album_version=""):
    return await asyncio.to_thread(find_existing_track_fast_sync, title, artist, album, duration, album_version)


def _download_content_identity(title, artist, album="", duration=0, album_version=""):
    title_key = normalize_identity_text(title, title=True)
    artist_key = normalize_identity_text(artist)
    album_key = normalize_identity_text(album)
    variants = sorted((_duplicate_variant_tokens(title) | _duplicate_variant_tokens(album) | _duplicate_variant_tokens(album_version)))
    duration_value = safe_float(duration, 0)
    duration_bucket = int(round(duration_value / 5.0) * 5) if duration_value > 0 else 0
    parts = ["v2", artist_key, title_key, album_key, str(duration_bucket)]
    parts.append(",".join(variants))
    return "|".join(parts) if title_key and artist_key else ""


async def _prepare_download_identity(url, title, artist, album, duration, settings):
    """Resolve provider metadata and retain explicit confidence when the provider lookup fails."""
    source = await resolve_source_metadata(url)
    cleanup_rules = settings.get("title_cleanup_rules", "")
    source_ok = bool(source.get("_lookup_ok", False)) if isinstance(source, dict) else False
    resolved_title = normalize_catalog_title(source.get("title") or title or "Unknown Track", cleanup_rules)
    resolved_artist = _usable_artist_hint(source.get("artist")) or clean_metadata_text(artist, "Unknown Artist")
    resolved_album = clean_metadata_text(source.get("album"), "") or clean_metadata_text(album, "")
    resolved_duration = safe_float(source.get("duration"), 0) or safe_float(duration, 0)
    if resolved_title.casefold() in {"unknown track", "unknown"} and title:
        resolved_title = normalize_catalog_title(title, cleanup_rules)
    confidence = 0.82 if source_ok and (source.get("title") or source.get("artist")) else 0.25
    source_label = "provider" if source_ok else "supplied_fallback"
    album_version = clean_metadata_text(source.get("album_version", "") if source_ok else "", "")
    content_identity = _download_content_identity(resolved_title, resolved_artist, resolved_album, resolved_duration, album_version)
    return resolved_title, resolved_artist or "Unknown Artist", resolved_album, resolved_duration, confidence, source_label, content_identity, album_version


async def _check_download_availability(url, title, artist, album, duration, settings):
    resolved_title, resolved_artist, resolved_album, resolved_duration, confidence, source_label, content_identity, album_version = await _prepare_download_identity(url, title, artist, album, duration, settings)
    existing = await find_existing_track(resolved_title, resolved_artist, resolved_album, resolved_duration, album_version)
    if existing:
        return {"status": "already_downloaded", "file": existing, "title": resolved_title, "artist": resolved_artist, "album": resolved_album, "duration": resolved_duration, "metadata_confidence": confidence, "metadata_source": source_label, "album_version": album_version}
    for task in TASKS.values():
        if task.get("status") not in ACTIVE_TASK_STATES:
            continue
        task_identity = str(task.get("content_identity") or "")
        if content_identity and task_identity == content_identity:
            return {"status": "already_queued", "task_id": task.get("id"), "title": resolved_title, "artist": resolved_artist, "album": resolved_album, "duration": resolved_duration, "metadata_confidence": confidence, "metadata_source": source_label, "album_version": album_version}
        if str(task.get("url") or "") == url:
            return {"status": "already_queued", "task_id": task.get("id"), "title": resolved_title, "artist": resolved_artist, "album": resolved_album, "duration": resolved_duration, "metadata_confidence": confidence, "metadata_source": source_label}
    return {"status": "available", "title": resolved_title, "artist": resolved_artist, "album": resolved_album, "duration": resolved_duration, "content_identity": content_identity, "metadata_confidence": confidence, "metadata_source": source_label}


@app.post("/api/download/check")
async def api_download_check(payload: dict = Body(...)):
    url = validate_media_url(payload.get("url"))
    settings = await load_settings_async()
    try:
        result = await _check_download_availability(
            url,
            str(payload.get("title") or ""),
            str(payload.get("artist") or ""),
            str(payload.get("album") or ""),
            payload.get("duration") or 0,
            settings,
        )
    except StorageUnavailable as exc:
        raise HTTPException(503, _sanitize_external_error(exc, "Music library is unavailable; download check blocked.")) from exc
    except Exception as exc:
        safe = _sanitize_external_error(exc, "Library duplicate check is unavailable; download blocked.")
        await write_app_error("download_preflight", safe)
        raise HTTPException(503, safe) from exc
    return result


@app.post("/api/download")
async def api_download(
    payload: dict = Body(...),
):

    url = validate_media_url(payload.get("url"))

    settings = await load_settings_async()
    task_title, task_artist, task_album, task_duration, metadata_confidence, metadata_source, prepared_content_identity, prepared_album_version = await _prepare_download_identity(
        url,
        str(payload.get("title", "Unknown Track") or "Unknown Track"),
        str(payload.get("artist", "Unknown Artist") or "Unknown Artist"),
        str(payload.get("album") or ""),
        payload.get("duration") or 0,
        settings,
    )
    if task_album.casefold() in {"unknown album", "unknown"}:
        task_album = ""

    # Make the Save operation idempotent. The lock prevents two rapid/concurrent
    # clicks from both passing the duplicate check before either task is registered.
    async with DOWNLOAD_GUARD:
        content_identity = prepared_content_identity or _download_content_identity(task_title, task_artist, task_album, task_duration, prepared_album_version)
        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
        identity_key = f"{content_identity}|{url_hash}" if content_identity else f"url:{url_hash}"
        for task in TASKS.values():
            if task.get("identity_key"):
                task_key = task["identity_key"]
            else:
                normalized = normalize_duplicate_key(task.get("title", ""), task.get("artist", ""))
                album_identity = _compact_identity(task.get("album", ""))
                normalized = f"{normalized}|{album_identity}" if normalized and album_identity else normalized
                task_url_hash = hashlib.sha256(str(task.get("url", "")).encode("utf-8")).hexdigest()
                task_key = f"{normalized}|{task_url_hash}" if normalized else f"url:{task_url_hash}"
            if task_key == identity_key and task.get("status") in ACTIVE_TASK_STATES:
                return {"status": "already_queued", "task_id": task["id"]}
            if content_identity and str(task.get("content_identity") or "") == content_identity and task.get("status") in ACTIVE_TASK_STATES:
                return {"status": "already_queued", "task_id": task["id"]}
            if task.get("url") == url and task.get("status") in ACTIVE_TASK_STATES:
                return {"status": "already_queued", "task_id": task["id"]}

        try:
            existing = await find_existing_track(task_title, task_artist, task_album, task_duration, prepared_album_version)
        except StorageUnavailable as exc:
            raise HTTPException(503, _sanitize_external_error(exc, "Music library is unavailable; download blocked.")) from exc
        except Exception as exc:
            safe = _sanitize_external_error(exc, "Library duplicate check is unavailable; download blocked.")
            await write_app_error("download_preflight", safe)
            raise HTTPException(503, safe) from exc
        if existing:
            return {
                "status": "already_downloaded",
                "file": existing,
                "title": task_title,
                "artist": task_artist,
            }

        max_pending = max(50, min(5000, safe_int(settings.get("max_pending_downloads"), MAX_PENDING_DOWNLOADS)))
        pending_count = sum(
            1 for item in TASKS.values()
            if item.get("status") in ACTIVE_TASK_STATES
        )
        if pending_count >= max_pending:
            raise HTTPException(429, f"Download queue is full ({max_pending} pending tasks).")

        task_id = uuid.uuid4().hex[:12]
        task = {
            "id": task_id,
            "title": task_title,
            "artist": task_artist,
            "album": task_album,
            "duration": task_duration,
            "content_identity": content_identity,
            "identity_version": 2,
            "album_version": prepared_album_version,
            "download_root": str(_download_root_for_settings(settings)),
            "catalog_pending_path": "",
            "metadata_confidence": metadata_confidence,
            "metadata_source": metadata_source,
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
    await _refill_download_queue()

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
            0 if task.get("status") in ACTIVE_TASK_STATES else 1,
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
    async with DOWNLOAD_GUARD:
        task = TASKS.get(task_id)

        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        status = str(task.get("status") or "").lower()
        if status in TERMINAL_TASK_STATES and status != "cancelled":
            return {"status": status, "task_id": task_id}
        if status == "cancelled":
            return {"status": "cancelled", "task_id": task_id}
        if status == "catalog_pending":
            raise HTTPException(409, "Library commit is pending; this task cannot be cancelled.")

        _set_task_cancelled(task)
        QUEUED_TASK_IDS.discard(task_id)
        process = ACTIVE_PROCESSES.get(task_id)
        if process:
            try:
                process.terminate()
            except Exception:
                pass

        await notify_task_update(task, force_save=True)
        return {"status": "cancelled", "task_id": task_id}


@app.post("/api/tasks/{task_id}/retry")
async def api_retry_task(task_id: str):
    async with DOWNLOAD_GUARD:
        task = TASKS.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.get("status") not in {"error", "failed", "cancelled", "canceled"}:
            raise HTTPException(status_code=400, detail="Only failed or cancelled tasks can be retried.")
        if task_id in ACTIVE_PROCESSES:
            raise HTTPException(status_code=409, detail="Task is still stopping; retry in a moment.")
        settings = await load_settings_async()
        max_pending = max(50, min(5000, safe_int(settings.get("max_pending_downloads"), MAX_PENDING_DOWNLOADS)))
        pending_count = sum(
            1 for item in TASKS.values()
            if item.get("status") in ACTIVE_TASK_STATES and item.get("id") != task_id
        )
        if pending_count >= max_pending:
            raise HTTPException(429, f"Download queue is full ({max_pending} pending tasks).")

        task["status"] = "queued"
        task["percent"] = 0
        task["speed"] = ""
        task["step"] = "Queued..."
        task["error"] = ""
        task["cancel_requested"] = False
        task["created_at"] = time.time() * 1000
        task["last_updated"] = task["created_at"]
        task["queue_token"] = uuid.uuid4().hex
        task["retry_count"] = 0

    await notify_task_update(task, force_save=True)
    await _refill_download_queue()
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

    if task.get("status") in ACTIVE_TASK_STATES or task_id in ACTIVE_PROCESSES:

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
        snapshot=await fast_library_snapshot()
        snapshot["storage_state"]=snapshot.get("storage",{}).get("state",STORAGE_STATE)
        return snapshot

    library=LIBRARY_CACHE
    play_counts=await asyncio.to_thread(_play_count_map_sync)
    result=[]; total=0
    for song in library.get("songs",[]):
        rel=str(Path(song["path"]).resolve().relative_to(DOWNLOAD_DIR.resolve())); size=int(song.get("size") or 0); total += size
        result.append({"id":song["id"],"name":rel,"size":format_size(size),"bytes":size,"title":song.get("title",Path(rel).stem),"artist":song.get("artist","Unknown Artist"),"album":song.get("album","Unknown Album"),"album_artist":song.get("albumArtist",song.get("artist","Unknown Artist")),"genre":song.get("genre",""),"year":song.get("year",""),"track":song.get("track",0),"duration":song.get("duration",0),"replaygain_track_gain":song.get("replaygain_track_gain"),"replaygain_album_gain":song.get("replaygain_album_gain"),"replaygain_track_peak":song.get("replaygain_track_peak"),"replaygain_album_peak":song.get("replaygain_album_peak"),"has_artwork":bool(song.get("has_artwork")),"play_count":play_counts.get(song["id"],0),"cover":versioned_cover_url(song["path"]),"stream":"/api/library/stream/"+urllib.parse.quote(rel,safe="/")})
    result.sort(key=lambda item:item["name"].lower())
    artists=[]
    for artist in library["artists"].values():
        song_ids=set(artist.get("songIds",[])); album_ids=list(artist.get("albumIds",[]))
        artists.append({"id":artist["id"],"name":artist["name"],"song_count":len(song_ids),"album_count":len(album_ids),"song_ids":sorted(song_ids),"album_ids":album_ids,"cover":f"/api/library/artist-artwork/{artist['id']}"})
    artists.sort(key=lambda item:item["name"].lower())
    albums=[]; song_map={song["id"]:song for song in library["songs"]}
    for album in library["albums"].values():
        songs=[song_map[sid] for sid in album["songIds"] if sid in song_map]
        cover=(versioned_cover_url(album["path"])) if songs else ""
        albums.append({"id":album["id"],"name":album["name"],"artist":album["artist"],"artist_id":album["artistId"],"year":album.get("year",""),"genre":album.get("genre",""),"song_count":len(songs),"cover":cover,"song_ids":[song["id"] for song in songs]})
    albums.sort(key=lambda item:(item["artist"].lower(),item["name"].lower()))
    storage=await asyncio.to_thread(storage_info_sync)
    scan_state=await asyncio.to_thread(_scan_state_read_sync)
    scan_status=str(scan_state.get("status") or "idle")
    if storage.get("state") == "offline":
        library_state="offline"
    elif scan_status == "running":
        library_state="scanning"
    elif scan_status == "error" and not result:
        library_state="error"
    elif not result:
        library_state="empty"
    else:
        library_state="ready"
    return {"files":result,"total_size":format_size(total),"total_bytes":total,"storage":storage,"storage_state":storage.get("state",STORAGE_STATE),"artists":artists,"albums":albums,"ready":library_state in {"ready","empty"},"library_state":library_state,"scan_state":scan_state,"revision":LIBRARY_REVISION}


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
    """Actionable library quality checks using the same duplicate engine as the duplicate API."""
    library = await build_library()
    songs = library.get("songs", [])
    by_id = {str(song.get("id")): song for song in songs}
    health_state = await asyncio.to_thread(_health_read_sync)
    health_report = health_state.get("report") if isinstance(health_state, dict) else {}
    unreadable = list((health_report or {}).get("unreadable") or []) if isinstance(health_report, dict) else []
    raw_duplicates = await asyncio.to_thread(strong_duplicate_report_sync, 100)
    duplicate_groups_out = []
    for group in raw_duplicates:
        files = []
        for sid in group.get("ids") or []:
            song = by_id.get(str(sid))
            if not song:
                continue
            files.append({
                "id": song["id"], "name": str(song["path"].relative_to(DOWNLOAD_DIR)),
                "title": song.get("title", ""), "artist": song.get("artist", "Unknown Artist"),
                "album": song.get("album", "Unknown Album"), "duration": song.get("duration", 0), "size": song.get("size", 0),
            })
        if len(files) >= 2:
            first = files[0]
            duplicate_groups_out.append({"key": group.get("strong_hash", ""), "count": len(files), "files": files, "title": first.get("title"), "artist": first.get("artist"), "album": first.get("album"), "verified": True})

    missing_metadata=[]; missing_artwork=[]; replaygain_missing=[]; suspicious_names=[]
    for song in songs:
        title = str(song.get("title") or "").strip(); artist = str(song.get("artist") or "").strip(); album = str(song.get("album") or "").strip()
        issues=[]
        if not title or title.casefold() in {"unknown track", "unknown"}: issues.append("title")
        if not artist or artist.casefold() in {"unknown artist", "unknown"}: issues.append("artist")
        if not album or album.casefold() in {"unknown album", "unknown"}: issues.append("album")
        if issues: missing_metadata.append({"id":song["id"],"title":title or Path(str(song.get("path") or "track")).stem,"artist":artist or "Unknown Artist","album":album or "Unknown Album","issues":issues})
        if not song.get("has_artwork"): missing_artwork.append({"id":song["id"],"title":title or Path(str(song.get("path") or "track")).stem,"artist":artist or "Unknown Artist","album":album or "Unknown Album"})
        if song.get("replaygain_track_gain") is None and song.get("replaygain_album_gain") is None: replaygain_missing.append({"id":song["id"],"title":title or Path(str(song.get("path") or "track")).stem,"artist":artist or "Unknown Artist"})
        name=Path(str(song.get("path") or "")).name
        if re.search(r"(?:\[?\(?(?:official|lyric|lyrics|music video|video|visualizer)|\d{1,3}[-_. ])", name, re.I): suspicious_names.append({"id":song["id"],"name":name,"title":title,"artist":artist})
    return {
        "track_count":len(songs), "unreadable":unreadable[:200], "unreadable_count":len(unreadable),
        "duplicate_groups":duplicate_groups_out[:100], "duplicate_tracks":sum(max(0,x["count"]-1) for x in duplicate_groups_out),
        "missing_metadata":missing_metadata[:200], "missing_metadata_count":len(missing_metadata),
        "missing_artwork":missing_artwork[:200], "missing_artwork_count":len(missing_artwork),
        "replaygain_missing":replaygain_missing[:200], "replaygain_missing_count":len(replaygain_missing),
        "suspicious_names":suspicious_names[:200], "suspicious_names_count":len(suspicious_names),
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
            "cover": versioned_cover_url(song["path"]), "stream": f"/api/library/stream/{enc}",
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
        all_play_count, distinct_played = await asyncio.to_thread(_play_totals_sync)
        return {"tracks": len(snap["files"]), "artists": snap.get("artists_count", 0), "albums": snap.get("albums_count", 0), "total_bytes": snap["total_bytes"], "folder_size": snap["total_size"], "all_play_count": all_play_count, "played_tracks": distinct_played, "ready": bool(snap.get("ready", False)), "library_state": snap.get("library_state", "unknown"), "revision": snap.get("revision", LIBRARY_REVISION)}

    library = await build_library()

    songs = library["songs"]
    artists = library["artists"]
    albums = library["albums"]
    total = sum(song["size"] for song in songs)
    all_play_count, distinct_played = await asyncio.to_thread(_play_totals_sync)
    storage = await asyncio.to_thread(storage_info_sync)
    storage_state = str(storage.get("state") or STORAGE_STATE)
    library_state = "offline" if storage_state == "offline" else ("ready" if songs else "empty")
    return {
        "tracks": len(songs),
        "artists": len(artists),
        "albums": len(albums),
        "total_bytes": total,
        "folder_size": format_size(total),
        "all_play_count": all_play_count,
        "played_tracks": distinct_played,
        "ready": library_state in {"ready", "empty"},
        "library_state": library_state,
        "storage_state": storage_state,
        "revision": LIBRARY_REVISION,
    }


@app.get("/api/home")
async def api_home():
    # Home reads the authoritative catalog. build_library is cached and only
    # reconciles the filesystem when the catalog cache expires.
    library = await build_library()
    songs = sorted(library.get("songs", []), key=lambda item: safe_float(item.get("modified"), 0), reverse=True)
    recent = []
    for song in songs[:12]:
        relative_path = str(Path(song["path"]).relative_to(DOWNLOAD_DIR))
        encoded = urllib.parse.quote(relative_path, safe="/")
        recent.append({
            "id": str(song.get("id") or ""),
            "title": song.get("title") or Path(relative_path).stem,
            "artist": song.get("artist") or "Unknown Artist",
            "album": song.get("album") or Path(relative_path).stem,
            "duration": safe_int(song.get("duration"), 0),
            "cover": versioned_cover_url(song["path"]),
            "stream": "/api/library/stream/" + encoded,
        })

    active = sum(
        1
        for task in TASKS.values()
        if task.get("status") in {
            "queued",
            "downloading",
            "processing",
        }
    )

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
            headers={"Cache-Control": "public, max-age=86400", "ETag": hashlib.sha1(f"{cover}|{cover.stat().st_mtime_ns}|{cover.stat().st_size}".encode("utf-8")).hexdigest()},
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


def _cleanup_deleted_song_sync(song_id):
    with db_connect() as conn:
        conn.execute("DELETE FROM stars WHERE item_id=?", (song_id,))
        conn.execute("DELETE FROM playback_positions WHERE song_id=?", (song_id,))
        conn.execute("DELETE FROM song_review WHERE song_id=?", (song_id,))
        conn.execute("DELETE FROM song_editor_history WHERE song_id=?", (song_id,))
        conn.execute("DELETE FROM play_history WHERE song_id=?", (song_id,))
        conn.execute("DELETE FROM subsonic_scrobbles WHERE song_id=?", (song_id,))
        rows = conn.execute("SELECT id,song_ids FROM playlists").fetchall()
        for row in rows:
            try:
                ids = json.loads(row[1] or "[]")
                if not isinstance(ids, list):
                    ids = []
                filtered = [value for value in ids if str(value) != str(song_id)]
                if filtered != ids:
                    conn.execute("UPDATE playlists SET song_ids=?,updated_at=? WHERE id=?", (json.dumps(filtered, separators=(",", ":")), time.time(), row[0]))
            except Exception:
                continue
        conn.commit()


def _create_delete_intent_sync(song_id, path):
    resolved = Path(path).resolve()
    rel = str(resolved.relative_to(DOWNLOAD_DIR.resolve()))
    now = time.time()
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO library_delete_intents(song_id,relative_path,attempts,last_error,created_at,updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(relative_path) DO UPDATE SET song_id=excluded.song_id,updated_at=excluded.updated_at",
            (str(song_id or ""), rel, 0, "", now, now),
        )
        conn.commit()


def _clear_delete_intent_sync(relative_path):
    with db_connect() as conn:
        conn.execute("DELETE FROM library_delete_intents WHERE relative_path=?", (str(relative_path),))
        conn.commit()


def _record_delete_intent_error_sync(relative_path, message):
    with db_connect() as conn:
        conn.execute("UPDATE library_delete_intents SET attempts=attempts+1,last_error=?,updated_at=? WHERE relative_path=?", (str(message or "")[:500], time.time(), str(relative_path)))
        conn.commit()


def _delete_intents_sync(limit=20):
    with db_connect() as conn:
        rows = conn.execute("SELECT id,song_id,relative_path,attempts,last_error FROM library_delete_intents ORDER BY updated_at ASC,id ASC LIMIT ?", (max(1,int(limit)),)).fetchall()
    return [dict(row) for row in rows]


async def _retry_library_delete_intents():
    for item in await asyncio.to_thread(_delete_intents_sync, 12):
        rel = str(item.get("relative_path") or "")
        song_id = str(item.get("song_id") or "")
        try:
            candidate = (DOWNLOAD_DIR / rel).resolve()
            candidate.relative_to(DOWNLOAD_DIR.resolve())
        except (OSError, RuntimeError, ValueError) as exc:
            safe = _sanitize_external_error(exc, "Pending library deletion path is invalid.", 400)
            await asyncio.to_thread(_record_delete_intent_error_sync, rel, safe)
            continue
        try:
            async with LIBRARY_SCAN_LOCK:
                async with LIBRARY_CATALOG_LOCK:
                    if candidate.is_file():
                        await asyncio.to_thread(candidate.unlink)
                        cover = await asyncio.to_thread(cover_cache_path, candidate)
                        try:
                            if cover.exists():
                                await asyncio.to_thread(cover.unlink)
                        except OSError:
                            pass
                    if song_id and LIBRARY_CATALOG is not None:
                        await asyncio.to_thread(LIBRARY_CATALOG.mark_missing, song_id)
                        await asyncio.to_thread(_cleanup_deleted_song_sync, song_id)
                    METADATA_CACHE.pop(str(candidate), None)
                    invalidate_library_cache("delete_retry")
                    await asyncio.to_thread(_clear_delete_intent_sync, rel)
            await manager.broadcast({"type":"library_updated","reason":"delete_retry","songId":song_id,"revision":LIBRARY_REVISION})
        except Exception as exc:
            await asyncio.to_thread(_record_delete_intent_error_sync, rel, _sanitize_external_error(exc, "Pending library deletion failed.", 500))


@app.delete(
    "/api/library/{filename:path}"
)
async def api_delete_library(
    filename: str,
):

    path = await resolve_file(filename)
    catalog_row = await asyncio.to_thread(_catalog_song_for_path_sync, path)
    deleted_song_id = str(catalog_row.get("id") or "") if isinstance(catalog_row, dict) else str(catalog_row or "")
    rel = str(path.resolve().relative_to(DOWNLOAD_DIR.resolve()))

    try:
        async with LIBRARY_SCAN_LOCK:
            async with LIBRARY_CATALOG_LOCK:
                if deleted_song_id:
                    await asyncio.to_thread(_create_delete_intent_sync, deleted_song_id, path)
                def delete_file_sync(target):
                    cover = cover_cache_path(target)
                    target.unlink()
                    try:
                        if cover.exists():
                            cover.unlink()
                    except OSError:
                        pass
                await asyncio.to_thread(delete_file_sync, path)
                if deleted_song_id and LIBRARY_CATALOG is not None:
                    await asyncio.to_thread(LIBRARY_CATALOG.mark_missing, deleted_song_id)
                    await asyncio.to_thread(_cleanup_deleted_song_sync, deleted_song_id)
                METADATA_CACHE.pop(str(path), None)
                invalidate_library_cache("delete")
                if deleted_song_id:
                    await asyncio.to_thread(_clear_delete_intent_sync, rel)
        await manager.broadcast({"type":"library_updated","reason":"delete","songId":deleted_song_id,"revision":LIBRARY_REVISION})
        await broadcast_stats_invalidated("delete")
        return {"status":"deleted","filename":filename}
    except Exception as error:
        if deleted_song_id:
            try:
                await asyncio.to_thread(_record_delete_intent_error_sync, rel, _sanitize_external_error(error, "File deletion failed.", 500))
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=_sanitize_external_error(error, "File deletion failed.")) from error


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


def _subsonic_failure_key(request, supplied_user=""):
    return f"{_auth_client_key(request)}:{str(supplied_user or '')[:128]}"


def _subsonic_auth_blocked(request, supplied_user=""):
    global SUBSONIC_FAILURE_LAST_CLEANUP
    now = time.monotonic()
    if now - SUBSONIC_FAILURE_LAST_CLEANUP > 300:
        stale = [key for key, entries in SUBSONIC_FAILURE_STATE.items() if not entries or now - entries[-1] >= 600]
        for key in stale:
            SUBSONIC_FAILURE_STATE.pop(key, None)
        SUBSONIC_FAILURE_LAST_CLEANUP = now
    key = _subsonic_failure_key(request, supplied_user)
    entries = [ts for ts in SUBSONIC_FAILURE_STATE.get(key, []) if now - ts < SUBSONIC_FAILURE_LIMIT[1]]
    SUBSONIC_FAILURE_STATE[key] = entries
    return len(entries) >= SUBSONIC_FAILURE_LIMIT[0]


def _record_subsonic_failure(request, supplied_user=""):
    key = _subsonic_failure_key(request, supplied_user)
    now = time.monotonic()
    entries = [ts for ts in SUBSONIC_FAILURE_STATE.get(key, []) if now - ts < SUBSONIC_FAILURE_LIMIT[1]]
    entries.append(now)
    SUBSONIC_FAILURE_STATE[key] = entries


def _clear_subsonic_failures(request, supplied_user=""):
    SUBSONIC_FAILURE_STATE.pop(_subsonic_failure_key(request, supplied_user), None)


def validate_subsonic_auth(
    request: Request,
):

    username, password = subsonic_credentials()
    supplied_user = request.query_params.get("u", "")[:128]
    if _subsonic_auth_blocked(request, supplied_user):
        return False

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

    # OpenSubsonic is disabled until a non-empty password is configured.
    if not username or not password:
        return False

    if supplied_user != username:
        _record_subsonic_failure(request, supplied_user)
        return False

    # t = MD5(password + salt)
    if token and salt:

        expected = hashlib.md5(
            (
                password + salt
            ).encode("utf-8")
        ).hexdigest()

        if token.lower() == expected.lower():
            _clear_subsonic_failures(request, supplied_user)
            return True

    if supplied_password == password:
        _clear_subsonic_failures(request, supplied_user)
        return True

    if supplied_password.lower().startswith("enc:"):
        try:
            decoded = binascii.unhexlify(supplied_password[4:]).decode("utf-8")
            if decoded == password:
                _clear_subsonic_failures(request, supplied_user)
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
            _clear_subsonic_failures(request, supplied_user)
            return True

    _record_subsonic_failure(request, supplied_user)
    return False


async def require_auth(request):
    await _enforce_rate_limit(request, "subsonic", *SUBSONIC_RATE_LIMIT)
    if not validate_subsonic_auth(request):

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
    error = await require_auth(request)
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
    error = await require_auth(request)
    if error:
        return error

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

    error = await require_auth(request)

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

    error = await require_auth(request)

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

    error = await require_auth(request)

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

@app.get("/rest/getScanStatus.view")
@app.get("/rest/getScanStatus")
async def rest_get_scan_status(request: Request):
    error = await require_auth(request)
    if error:
        return error
    try:
        scan = await asyncio.to_thread(_scan_state_read_sync)
        snap = await fast_library_snapshot()
        status = str(scan.get("status") or "idle")
        payload = {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "type": "Xrob Music",
            "scanStatus": {
                "scanning": status == "running",
                "count": len(snap.get("files", [])),
                "status": status,
                "mode": str(scan.get("mode") or ""),
                "message": str(scan.get("message") or "")[:300],
                "lastScanStarted": int(float(scan.get("started_at") or 0) * 1000) if scan.get("started_at") else 0,
                "lastScanFinished": int(float(scan.get("finished_at") or 0) * 1000) if scan.get("finished_at") else 0,
            },
        }
    except Exception as exc:
        await write_app_error("subsonic_scan_status", exc)
        payload = {
            "status": "ok", "version": SUBSONIC_VERSION, "serverVersion": SERVER_VERSION,
            "openSubsonic": True, "type": "Xrob Music",
            "scanStatus": {"scanning": False, "count": 0, "status": "error"},
        }
    return make_subsonic_response(payload, request)


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

    error = await require_auth(request)

    if error:
        return error

    try:
        snap = await fast_library_snapshot()
        await request_library_refresh(reason="subsonic", mode="quick", wait=False)
        count = len(snap.get("files") or [])
        scanning = True
    except Exception:
        count = 0
        scanning = False

    return make_subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "type": "Xrob Music",
            "scanStatus": {
                "scanning": scanning,
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

    error = await require_auth(request)

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

    error = await require_auth(request)

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

    error = await require_auth(request)

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

    error = await require_auth(request)

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

    error = await require_auth(request)

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

    error = await require_auth(request)

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

    error = await require_auth(request)

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

    error = await require_auth(request)

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

    error = await require_auth(request)

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

    error = await require_auth(request)

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

    error = await require_auth(request)

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

    error = await require_auth(request)

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

    error = await require_auth(request)

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

    error = await require_auth(request)

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

    error = await require_auth(request)

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

    error = await require_auth(request)

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

    error = await require_auth(request)

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

    error = await require_auth(request)

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

    error = await require_auth(request)

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


def _canonicalize_playlist_song_ids_sync(raw_ids):
    values = raw_ids if isinstance(raw_ids, list) else []
    canonical=[]; seen=set(); missing=[]
    with db_connect() as conn:
        for value in values[:MAX_PLAYLIST_SONGS]:
            sid = str(value or "").strip()[:512]
            if not sid: continue
            try:
                alias = conn.execute("SELECT song_id FROM library_song_aliases WHERE legacy_id=?", (sid,)).fetchone()
                resolved = str(alias[0]) if alias else sid
                row = conn.execute("SELECT id FROM library_songs WHERE id=? AND missing=0", (resolved,)).fetchone()
            except sqlite3.Error:
                row = None
                resolved = sid
            if row and resolved not in seen:
                canonical.append(resolved); seen.add(resolved)
            elif not row:
                missing.append(sid)
    return canonical, missing

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
        cursor = conn.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
        conn.commit()
        return int(cursor.rowcount or 0)


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
        safe_message = _sanitize_external_error(message, "Operation failed.", 900)
        with db_connect() as conn:
            conn.execute("INSERT INTO app_errors(created_at,source,message,task_id) VALUES(?,?,?,?)", (time.time(), str(source)[:120], safe_message, task_id))
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
    song_id = _canonical_song_id_for_write(song_id)
    duration = max(0.0, min(86_400.0, float(duration or 0)))
    position = max(0.0, min(86_400.0, float(position or 0)))
    if duration > 0:
        position = min(position, duration)
    with db_connect() as conn:
        conn.execute("INSERT INTO playback_positions(song_id,position,duration,updated_at) VALUES(?,?,?,?) ON CONFLICT(song_id) DO UPDATE SET position=excluded.position,duration=excluded.duration,updated_at=excluded.updated_at", (song_id, position, duration, now))
        conn.commit()


def _persist_subsonic_scrobble_sync(fingerprint, username, song_id, submission, position, duration):
    song_id = _canonical_song_id_for_write(song_id)
    with db_connect() as conn:
        conn.execute("INSERT OR IGNORE INTO subsonic_scrobbles(fingerprint,username,song_id,submission,created_at,position,duration) VALUES(?,?,?,?,?,?,?)", (fingerprint, username, song_id, int(bool(submission)), time.time(), position, duration))
        conn.commit()


def _persist_scrobble_history_sync(song_id, duration, position):
    song_id = _canonical_song_id_for_write(song_id)
    now=time.time()
    with db_connect() as conn:
        recent=conn.execute("SELECT id FROM play_history WHERE song_id=? AND played_at>=? ORDER BY played_at DESC LIMIT 1", (song_id, now-120)).fetchone()
        if recent:
            return
        conn.execute("INSERT INTO play_history(song_id,played_at,duration,position) VALUES(?,?,?,?)", (song_id,now,duration,position))
        conn.execute("DELETE FROM play_history WHERE id IN (SELECT id FROM play_history ORDER BY id DESC LIMIT -1 OFFSET ?)", (HISTORY_MAX_ROWS,))
        conn.commit()


def save_player_history_sync(song_id, duration, position):
    song_id = _canonical_song_id_for_write(song_id)
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
    await broadcast_stats_invalidated("play_history")
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
            out.append({"id":song["id"],"title":song["title"],"artist":song["artist"],"album":song["album"],"duration":song["duration"],"plays":int(r[1]),"cover":versioned_cover_url(song["path"]),"stream":"/api/library/stream/"+enc})
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
    ids, missing_ids = await asyncio.to_thread(_canonicalize_playlist_song_ids_sync, raw_ids)
    if missing_ids:
        raise HTTPException(400, {"message": "Playlist contains unknown or unavailable song IDs", "song_ids": missing_ids[:50]})
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
    ids, missing_ids = await asyncio.to_thread(_canonicalize_playlist_song_ids_sync, raw_ids)
    if missing_ids:
        raise HTTPException(400, {"message": "Playlist contains unknown or unavailable song IDs", "song_ids": missing_ids[:50]})
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
    removed = await asyncio.to_thread(_playlist_delete_sync, playlist_id)
    if not removed:
        raise HTTPException(404,"Playlist not found")
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
                    tracks.append({"id":s["id"],"title":s["title"],"artist":s["artist"],"album":s["album"],"duration":s["duration"],"cover":versioned_cover_url(s["path"]),"stream":"/api/library/stream/"+enc})
            p["tracks"]=tracks
            return p
    raise HTTPException(404,"Playlist not found")


def _audio_file_readable_sync(path):
    if not path.exists() or not path.is_file() or path.stat().st_size <= 0:
        return False
    if MutagenFile is not None:
        try:
            audio = MutagenFile(path, easy=False)
            if audio is not None:
                info = getattr(audio, "info", None)
                if info is not None:
                    return True
        except Exception:
            pass
    # Mutagen may not understand every format build; use ffprobe as the authoritative decoder check.
    try:
        completed = subprocess.run(
            [*FFPROBE_COMMAND, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if completed.returncode != 0:
            return False
        value = (completed.stdout or "").strip()
        return bool(value) and math.isfinite(float(value)) and float(value) > 0
    except Exception:
        return False



def _health_store_sync(status, report=None, message="", started_at=None, finished_at=None):
    payload = json.dumps(report or {}, ensure_ascii=False, separators=(",", ":"))
    with db_connect() as conn:
        conn.execute("INSERT INTO library_health(id,status,started_at,finished_at,message,report_json,updated_at) VALUES(1,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,started_at=excluded.started_at,finished_at=excluded.finished_at,message=excluded.message,report_json=excluded.report_json,updated_at=excluded.updated_at", (status, started_at or 0, finished_at or 0, message[:500], payload, time.time()))
        conn.commit()


def _health_read_sync():
    with db_connect() as conn:
        row = conn.execute("SELECT status,started_at,finished_at,message,report_json,updated_at FROM library_health WHERE id=1").fetchone()
    if not row:
        return {"status": "idle", "report": {}, "updated_at": 0}
    try:
        report = json.loads(row[4] or "{}")
    except Exception:
        report = {}
    return {"status": row[0], "started_at": row[1], "finished_at": row[2], "message": row[3], "report": report, "updated_at": row[5]}


def _run_library_health_sync():
    ok, error = _probe_storage_sync()
    if not ok:
        raise StorageUnavailable(error)
    all_rows = [r for r in (LIBRARY_CATALOG.rows() if LIBRARY_CATALOG is not None else []) if not int(r.get("missing") or 0)]
    rows = LIBRARY_CATALOG.health_batch(LIBRARY_HEALTH_MAX_FILES_PER_RUN) if LIBRARY_CATALOG is not None else []
    unreadable=[]; bad_tags=[]; missing_art=[]; checked_ids=[]
    for row in rows:
        checked_ids.append(str(row.get("id") or ""))
        rel=str(row.get("relative_path") or "")
        try:
            path=(DOWNLOAD_DIR / rel).resolve()
            path.relative_to(DOWNLOAD_DIR.resolve())
        except (OSError, RuntimeError, ValueError):
            unreadable.append({"path":rel,"reason":"unsafe catalog path"}); continue
        if not path.is_file():
            unreadable.append({"path":rel,"reason":"file missing"}); continue
        valid, details = _audio_validation_sync(path)
        if not valid:
            unreadable.append({"path":rel,"reason":str(details)[:400]})
        md=LibraryCatalog._metadata_from_row(row)
        title=clean_metadata_text(md.get("title"),""); artist=clean_metadata_text(md.get("artist"),""); album=clean_metadata_text(md.get("album"),"")
        if not title or not artist or not album or artist.casefold() in {"unknown","unknown artist"} or album.casefold() in {"unknown","unknown album"}:
            bad_tags.append({"path":rel,"title":title,"artist":artist,"album":album})
        if not md.get("has_artwork"):
            missing_art.append(rel)
    if LIBRARY_CATALOG is not None and checked_ids:
        LIBRARY_CATALOG.mark_health_checked(checked_ids, time.time())
    report={"checked":len(checked_ids),"total":len(all_rows),"unreadable":unreadable[:200],"bad_tags":bad_tags[:500],"missing_artwork":missing_art[:500],"duplicate_groups":[],"counts":{"unreadable":len(unreadable),"bad_tags":len(bad_tags),"missing_artwork":len(missing_art),"duplicates":0,"duplicate_files":0}}
    return report


async def background_library_health_scanner():
    global LIBRARY_HEALTH_TASK, LIBRARY_HEALTH_WAKE
    if LIBRARY_HEALTH_WAKE is None:
        LIBRARY_HEALTH_WAKE = asyncio.Event()
    first_run = True
    while True:
        if first_run:
            first_run = False
            try:
                await asyncio.wait_for(LIBRARY_HEALTH_WAKE.wait(), timeout=120)
                LIBRARY_HEALTH_WAKE.clear()
                continue
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                return
        started=time.time()
        try:
            await asyncio.to_thread(_health_store_sync,"running",{},"Library health scan running",started,0)
            report=await asyncio.to_thread(_run_library_health_sync)
            await asyncio.to_thread(_health_store_sync,"ok",report,f"Checked {report.get('checked',0)} tracks",started,time.time())
        except asyncio.CancelledError:
            return
        except StorageUnavailable as exc:
            await asyncio.to_thread(_health_store_sync,"offline",{},_sanitize_external_error(exc,"Music storage is offline.",400),started,time.time())
        except Exception as exc:
            safe=_sanitize_external_error(exc,"Library health scan failed.",500)
            await asyncio.to_thread(_health_store_sync,"error",{},safe,started,time.time())
            await write_app_error("library_health",safe)
        try:
            settings=await load_settings_async()
            interval=max(30,min(10080,int(settings.get("health_scan_interval_minutes",LIBRARY_HEALTH_INTERVAL_SECONDS/60) or LIBRARY_HEALTH_INTERVAL_SECONDS/60)))
        except Exception:
            interval=int(LIBRARY_HEALTH_INTERVAL_SECONDS/60)
        LIBRARY_HEALTH_WAKE.clear()
        try:
            await asyncio.wait_for(LIBRARY_HEALTH_WAKE.wait(), timeout=interval * 60)
            LIBRARY_HEALTH_WAKE.clear()
            continue
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            return


@app.get("/api/library/health/status")
async def api_library_health_status():
    return await asyncio.to_thread(_health_read_sync)


@app.get("/api/library/duplicates")
async def api_library_duplicates(limit:int=Query(100,ge=1,le=500)):
    return {"duplicates": await asyncio.to_thread(strong_duplicate_report_sync, limit)}


@app.get("/api/library/health")
async def api_library_health():
    global LIBRARY_HEALTH_TASK
    state = await asyncio.to_thread(_health_read_sync)
    active = bool(LIBRARY_HEALTH_TASK and not LIBRARY_HEALTH_TASK.done())
    current_status = str(state.get("status") or "")
    if current_status in {"idle", "error", "offline"}:
        queued_at = time.time()
        if active:
            # The scanner is created during startup and may still be waiting for
            # its initial delay. Wake that existing task immediately on demand.
            if LIBRARY_HEALTH_WAKE is not None:
                LIBRARY_HEALTH_WAKE.set()
        else:
            LIBRARY_HEALTH_TASK = asyncio.create_task(background_library_health_scanner())
            if LIBRARY_HEALTH_WAKE is not None:
                LIBRARY_HEALTH_WAKE.set()
        await asyncio.to_thread(_health_store_sync, "running", state.get("report") or {}, "Health scan queued", queued_at, 0)
        state = dict(state or {})
        state.update({"status": "running", "message": "Health scan queued", "started_at": queued_at, "updated_at": queued_at})
    return state


@app.post("/api/library/scan/{mode}")
async def api_library_scan_mode(mode: str):
    if mode not in {"quick", "full"}:
        raise HTTPException(400, "mode must be quick or full")
    try:
        library = await request_library_refresh(reason="manual", mode=mode, wait=True)
        return {"status": "ok", "mode": mode, "tracks": len(library.get("songs", [])) if library else 0}
    except StorageUnavailable as exc:
        raise HTTPException(503, _sanitize_external_error(exc, "Music storage is offline.")) from exc
    except Exception as exc:
        safe = _sanitize_external_error(exc, "Library refresh failed.")
        await write_app_error("library_refresh", safe)
        raise HTTPException(500, safe) from exc


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
    await asyncio.to_thread(_consume_bootstrap_after_successful_login_sync)
    if was_legacy_plaintext and not os.getenv("XROB_PASSWORD"):
        settings = await load_settings_async()
        settings["web_password_hash"] = _hash_web_password(password)
        settings.pop("web_password", None)
        await asyncio.to_thread(_write_settings_sync, settings)
    token = _auth_token()
    now = time.time()
    _cleanup_auth_sessions(now, force=True)
    AUTH_SESSIONS[token] = {"created": now, "last_seen": now, "username": expected_user}
    _cleanup_auth_sessions(now, force=True)
    response = JSONResponse({"status":"ok", "username":expected_user})
    response.set_cookie(
        AUTH_COOKIE, token, httponly=True, samesite="lax",
        secure=request.url.scheme == "https", path="/", max_age=AUTH_SESSION_ABSOLUTE_SECONDS
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
            checks["storage"] = {"ok": False, "path": str(DOWNLOAD_DIR), "error": _sanitize_external_error(exc, "Storage check failed.")}
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
            tools[name] = {"ok": False, "error": _sanitize_external_error(exc, "Tool check failed.")}
    checks["tools"] = tools
    checks["runtime"] = {"server_version": SERVER_VERSION, "tasks": len(TASKS), "active_processes": len(ACTIVE_PROCESSES), "websocket_connections": len(manager.connections) if hasattr(manager, "connections") else 0}
    return checks

async def _build_backup_zip(include_secrets=False):
    await asyncio.to_thread(init_db)
    settings = await asyncio.to_thread(load_settings)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    tmp = Path(tempfile.gettempdir()) / f"xrob-music-backup-{stamp}-{uuid.uuid4().hex[:8]}.zip"
    db_snapshot = Path(tempfile.gettempdir()) / f"xrob-db-snapshot-{uuid.uuid4().hex}.db"

    def build_backup():
        source = sqlite3.connect(DB_FILE, timeout=30.0)
        target = sqlite3.connect(db_snapshot, timeout=30.0)
        try:
            source.backup(target)
            target.commit()
        finally:
            target.close(); source.close()
        backup_settings = _sanitized_backup_settings(settings, include_secrets=include_secrets)
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(db_snapshot, "tasks.db")
            archive.writestr("settings.json", json.dumps(backup_settings, ensure_ascii=False, indent=2))
            archive.writestr("manifest.json", json.dumps({
                "format": 2,
                "server_version": SERVER_VERSION,
                "created_at": time.time(),
                "database_schema": DB_SCHEMA_VERSION,
                "encrypted_settings": bool(include_secrets),
                "library_index": "not included; SQLite catalog is authoritative",
            }, indent=2))
        return tmp

    try:
        return await asyncio.to_thread(build_backup), db_snapshot, stamp
    except Exception:
        for candidate in (tmp, db_snapshot):
            try: candidate.unlink(missing_ok=True)
            except OSError: pass
        raise


@app.get("/api/backup")
async def api_backup():
    try:
        path, db_snapshot, stamp = await _build_backup_zip(include_secrets=False)
        def cleanup():
            for candidate in (path, db_snapshot):
                try: candidate.unlink(missing_ok=True)
                except OSError: pass
        return FileResponse(path, media_type="application/zip", filename=f"xrob-music-backup-{stamp}.zip", background=BackgroundTask(cleanup))
    except Exception as exc:
        await write_app_error("backup", _sanitize_external_error(exc, "Backup failed."))
        raise HTTPException(500, "Backup failed. Please try again.")


@app.post("/api/backup/encrypted")
async def api_backup_encrypted(payload: dict = Body(...)):
    password = str(payload.get("password") or "")[:256]
    if len(password) < BACKUP_PASSWORD_MIN_LENGTH:
        raise HTTPException(400, f"Backup password must be at least {BACKUP_PASSWORD_MIN_LENGTH} characters.")
    path = db_snapshot = None
    encrypted_path = None
    try:
        path, db_snapshot, stamp = await _build_backup_zip(include_secrets=True)
        plain = await asyncio.to_thread(path.read_bytes)
        encrypted_path = Path(tempfile.gettempdir()) / f"xrob-music-backup-{stamp}-{uuid.uuid4().hex[:8]}.xrbk"
        encrypted = await asyncio.to_thread(_encrypt_backup_payload, plain, password)
        await asyncio.to_thread(encrypted_path.write_bytes, encrypted)
        def cleanup():
            for candidate in (path, db_snapshot, encrypted_path):
                try: candidate.unlink(missing_ok=True)
                except OSError: pass
        return FileResponse(encrypted_path, media_type="application/octet-stream", filename=f"xrob-music-backup-{stamp}.xrbk", background=BackgroundTask(cleanup))
    except ValueError as exc:
        raise HTTPException(400, _sanitize_external_error(exc, "Invalid encrypted backup request."))
    except Exception as exc:
        await write_app_error("backup_encrypted", exc)
        for candidate in (path, db_snapshot, encrypted_path):
            if candidate:
                try: candidate.unlink(missing_ok=True)
                except OSError: pass
        raise HTTPException(500, "Encrypted backup failed. Please try again.")


def _prune_restore_safety_backups_sync(keep=3):
    files = sorted(DB_FILE.parent.glob(f"{DB_FILE.name}.before-restore-*.bak"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    for old in files[max(1, int(keep)):]:
        try:
            old.unlink(missing_ok=True)
        except OSError:
            pass


async def _stop_runtime_for_restore():
    global SCHEDULED_SCANNER_TASK, LIBRARY_WARMUP_TASK, LIBRARY_HEALTH_TASK, LIBRARY_REFRESH_TASK, DOWNLOAD_WORKER_TASKS, RUNTIME_MAINTENANCE_TASK
    tasks = list(DOWNLOAD_WORKER_TASKS)
    for candidate in (SCHEDULED_SCANNER_TASK, LIBRARY_WARMUP_TASK, LIBRARY_HEALTH_TASK, LIBRARY_REFRESH_TASK, RUNTIME_MAINTENANCE_TASK):
        if candidate is not None:
            tasks.append(candidate)
    for task in tasks:
        if task and not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    DOWNLOAD_WORKER_TASKS.clear()
    SCHEDULED_SCANNER_TASK = None
    LIBRARY_WARMUP_TASK = None
    LIBRARY_HEALTH_TASK = None
    LIBRARY_REFRESH_TASK = None
    RUNTIME_MAINTENANCE_TASK = None
    QUEUED_TASK_IDS.clear()
    # Remove queued jobs; the restored DB becomes the sole source of truth.
    while True:
        try:
            TASK_QUEUE.get_nowait()
            TASK_QUEUE.task_done()
        except asyncio.QueueEmpty:
            break


async def _restart_runtime_after_restore():
    global SCHEDULED_SCANNER_TASK, LIBRARY_WARMUP_TASK, LIBRARY_HEALTH_TASK, DOWNLOAD_WORKER_TASKS, LIBRARY_CATALOG, RUNTIME_MAINTENANCE_TASK
    if LIBRARY_CATALOG is None:
        LIBRARY_CATALOG = LibraryCatalog(DB_FILE, DOWNLOAD_DIR, LIBRARY_INDEX_FILE)
    await asyncio.to_thread(init_db)
    await asyncio.to_thread(LIBRARY_CATALOG.migrate_legacy_index)
    settings = await load_settings_async()
    await resize_download_workers(settings.get("max_concurrent_downloads", MAX_CONCURRENT_DOWNLOADS))
    SCHEDULED_SCANNER_TASK = asyncio.create_task(scheduled_library_scanner())
    LIBRARY_WARMUP_TASK = asyncio.create_task(background_library_warmup())
    LIBRARY_HEALTH_TASK = asyncio.create_task(background_library_health_scanner())
    RUNTIME_MAINTENANCE_TASK = asyncio.create_task(runtime_maintenance_loop())
    QUEUED_TASK_IDS.clear()
    await _refill_download_queue()


@app.post("/api/restore")
async def api_restore(file: UploadFile = File(...), password: str = Form("")):
    global TASKS, LIBRARY_CACHE, PLAYER_STATE, PLAYER_STATE_UPDATED_AT
    if not file.filename or not file.filename.lower().endswith((".zip", ".xrbk")):
        raise HTTPException(400, "A Xrob Music .zip or encrypted .xrbk backup is required")
    raw = await file.read()
    if len(raw) > MAX_RESTORE_UPLOAD_BYTES:
        raise HTTPException(413, "Backup is too large")
    temp = Path(tempfile.gettempdir()) / f"xrob-restore-{uuid.uuid4().hex}.zip"
    db_tmp = None
    safety = None
    runtime_stopped = False
    try:
        decrypted, encrypted = await asyncio.to_thread(_decrypt_backup_payload, raw, password)
        await asyncio.to_thread(_validate_backup_zip_bytes, decrypted)
        await asyncio.to_thread(temp.write_bytes, decrypted)

        current_settings = await load_settings_async()
        def validate_zip():
            with zipfile.ZipFile(temp, "r") as z:
                names = set(z.namelist())
                target = Path(tempfile.gettempdir()) / f"xrob-restore-db-{uuid.uuid4().hex}.db"
                with z.open("tasks.db") as src, open(target, "wb") as dst:
                    remaining = MAX_RESTORE_DB_BYTES
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk: break
                        remaining -= len(chunk)
                        if remaining < 0:
                            raise ValueError("Database exceeds the restore size limit.")
                        dst.write(chunk)
                    dst.flush(); os.fsync(dst.fileno())
                test = sqlite3.connect(target, timeout=10.0)
                try:
                    test.execute("PRAGMA foreign_keys = ON")
                    ok = str(test.execute("PRAGMA integrity_check").fetchone()[0] or "")
                    if ok.lower() != "ok":
                        raise ValueError(f"Database integrity check failed: {_sanitize_external_error(ok, 'database check failed', 300)}")
                finally:
                    test.close()
                settings_payload = z.read("settings.json") if "settings.json" in names else json.dumps({}).encode("utf-8")
                decoded = json.loads(settings_payload.decode("utf-8"))
                if not isinstance(decoded, dict):
                    raise ValueError("settings.json must contain an object")
                # Ordinary restore never imports credentials. Encrypted backups may intentionally contain them.
                decoded = dict(decoded)
                if not encrypted:
                    for key in ("subsonic_user", "subsonic_password", "web_password", "web_password_hash"):
                        decoded.pop(key, None)
                return target, decoded, encrypted

        db_tmp, restored_settings, encrypted_backup = await asyncio.to_thread(validate_zip)
        merged_settings = dict(current_settings)
        merged_settings.update(restored_settings)
        if not encrypted_backup:
            for key in ("subsonic_user", "subsonic_password", "web_password", "web_password_hash"):
                if key in current_settings:
                    merged_settings[key] = current_settings[key]

        await _stop_runtime_for_restore()
        runtime_stopped = True
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        safety = DB_FILE.with_name(f"tasks.db.before-restore-{stamp}-{uuid.uuid4().hex[:6]}")
        if DB_FILE.exists():
            await asyncio.to_thread(_sqlite_backup_file, DB_FILE, safety)
            try:
                safety_files = sorted(DB_FILE.parent.glob("tasks.db.before-restore-*"), key=lambda p: p.stat().st_mtime, reverse=True)
                for old in safety_files[3:]:
                    old.unlink(missing_ok=True)
            except OSError:
                pass
        restore_db_tmp = DB_FILE.with_name(f".{DB_FILE.name}.restore-{uuid.uuid4().hex}.tmp")
        await asyncio.to_thread(shutil.copy2, db_tmp, restore_db_tmp)
        await asyncio.to_thread(os.replace, restore_db_tmp, DB_FILE)
        await asyncio.to_thread(init_db)

        def write_restore_state():
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            settings_tmp = SETTINGS_FILE.with_suffix(".restore.tmp")
            settings_tmp.write_text(json.dumps(merged_settings, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(settings_tmp, SETTINGS_FILE)
            # library_index.json is no longer a source of truth and is deliberately not restored.
            try: LIBRARY_INDEX_FILE.unlink(missing_ok=True)
            except OSError: pass
        await asyncio.to_thread(write_restore_state)
        await asyncio.to_thread(_ensure_secure_web_credentials_sync)

        TASKS = await asyncio.to_thread(db_load_tasks_sync)
        PLAYER_STATE = None
        PLAYER_STATE_UPDATED_AT = 0.0
        LIBRARY_CACHE = None
        await _restart_runtime_after_restore()
        runtime_stopped = False
        return {"status":"restored", "encrypted": encrypted_backup, "tasks":len(TASKS), "safety_backup":str(safety) if safety and safety.exists() else ""}
    except (zipfile.BadZipFile, ValueError, json.JSONDecodeError) as exc:
        safe = _sanitize_external_error(exc, "Restore rejected.")
        await write_app_error("restore", safe)
        raise HTTPException(400, safe)
    except Exception as exc:
        safe = _sanitize_external_error(exc, "Restore failed.")
        await write_app_error("restore", safe)
        raise HTTPException(500, safe)
    finally:
        if runtime_stopped:
            try:
                TASKS = await asyncio.to_thread(db_load_tasks_sync)
                LIBRARY_CACHE = None
                await _restart_runtime_after_restore()
            except Exception as restart_exc:
                await write_app_error("restore_restart", _sanitize_external_error(restart_exc, "Restore recovery failed."))
        try: temp.unlink(missing_ok=True)
        except Exception: pass
        if db_tmp is not None:
            try: db_tmp.unlink(missing_ok=True)
            except Exception: pass


def _sqlite_backup_file(source_path, target_path):
    source = sqlite3.connect(source_path, timeout=30.0)
    target = sqlite3.connect(target_path, timeout=30.0)
    try:
        source.backup(target)
        target.commit()
    finally:
        target.close(); source.close()


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


async def _sync_catalog_after_media_edit(path, reason="media_edit"):
    async with LIBRARY_SCAN_LOCK:
        async with LIBRARY_CATALOG_LOCK:
            if LIBRARY_CATALOG is None:
                raise RuntimeError("Library catalog is not initialized")
            record = await asyncio.to_thread(LIBRARY_CATALOG.update_metadata_for_path, path, read_metadata_sync)
            invalidate_library_cache(reason)
            try:
                await manager.broadcast({"type":"library_updated","reason":reason,"songId":str(record.get("id") or ""),"revision":LIBRARY_REVISION})
            except Exception as exc:
                await write_app_error("library_broadcast", exc)
            return record


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
    except Exception as exc:
        safe = _sanitize_external_error(exc, "Metadata update failed.")
        await write_app_error("metadata", safe)
        raise HTTPException(500, safe)
    edited_at = time.time()
    await asyncio.to_thread(_mark_song_review_sync, song_id, "edited", edited_at, True)
    await _sync_catalog_after_media_edit(path, "metadata_edit")
    return {"status":"ok"}


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
            "cover": versioned_cover_url(s["path"]),
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
    try:
        mime, _width, _height = _validate_image_payload(data, upload.content_type)
    except ValueError as exc:
        raise HTTPException(400, _sanitize_external_error(exc, "Invalid request."))
    await asyncio.to_thread(_artist_artwork_save_sync, artist_id, data, mime)
    invalidate_library_cache("artist_artwork_edit")
    await manager.broadcast({"type":"library_updated","reason":"artist_artwork_edit","artistId":artist_id,"revision":LIBRARY_REVISION})
    return {"status":"ok","artist_id":artist_id}

@app.post("/api/library/artwork/{song_id}")
async def api_library_artwork(song_id:str, upload:UploadFile=File(...)):
    song=await find_song(song_id)
    if not song: raise HTTPException(404,"Track not found")
    data=await upload.read()
    try:
        mime, _width, _height = _validate_image_payload(data, upload.content_type)
    except ValueError as exc:
        raise HTTPException(400, _sanitize_external_error(exc, "Invalid request."))
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
    except Exception as exc:
        safe = _sanitize_external_error(exc, "Artwork update failed.")
        await write_app_error("artwork", safe)
        raise HTTPException(500, safe)
    await _sync_catalog_after_media_edit(song["path"], "artwork_edit")
    return {"status":"ok"}


async def scheduled_library_scanner():
    global SCHEDULED_SCANNER_WAKE
    if SCHEDULED_SCANNER_WAKE is None:
        SCHEDULED_SCANNER_WAKE = asyncio.Event()
    while True:
        try:
            settings=await load_settings_async()
            enabled = bool(settings.get("scan_enabled", True))
            try: minutes = max(5, min(10080, int(settings.get("scan_interval_minutes", 60) or 60)))
            except (TypeError, ValueError): minutes = 60
            wait_seconds = minutes * 60 if enabled else 300
            SCHEDULED_SCANNER_WAKE.clear()
            try:
                await asyncio.wait_for(SCHEDULED_SCANNER_WAKE.wait(), timeout=wait_seconds)
                continue
            except asyncio.TimeoutError:
                pass
            if enabled:
                try:
                    await request_library_refresh(reason="scheduled", mode="quick", wait=False)
                except Exception as exc:
                    await write_app_error("scheduled_scan", exc)
        except asyncio.CancelledError: return
        except Exception as exc:
            await write_app_error("scheduled_scan",exc)
            await asyncio.sleep(300)


# ============================================================
async def _get_song_lyrics(song):
    embedded = await asyncio.to_thread(_extract_lyrics_tags_sync, song["path"])
    lyrics = embedded if embedded.get("plain") or embedded.get("synced") else await _fetch_lrclib_lyrics(song)
    structured = []
    synced = str(lyrics.get("syncedLyrics") or "")
    if synced:
        lines = []
        for line in synced.splitlines():
            times = re.findall(r"\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\]", line)
            text_line = re.sub(r"\[[^\]]+\]", "", line).strip()
            for mm, ss, frac in times:
                ms = int(mm) * 60000 + int(ss) * 1000 + (int((frac + "00")[:3]) if frac else 0)
                lines.append({"start": ms, "value": text_line})
        if lines:
            structured = [{"line": lines, "displayArtist": song.get("artist") or "", "displayTitle": song.get("title") or "", "language": ""}]
    return {"plainLyrics": lyrics.get("plainLyrics") or lyrics.get("plain") or "", "syncedLyrics": synced, "structuredLyrics": structured, "source": lyrics.get("source") or ("embedded" if embedded else "")}


@app.get("/api/lyrics/{song_id}")
async def api_lyrics(song_id: str, request: Request):
    song = await find_song(song_id)
    if not song:
        raise HTTPException(404, "Song not found")
    data = await _get_song_lyrics(song)
    return {"song_id": song_id, "title": song.get("title") or "Unknown Track", "artist": song.get("artist") or "Unknown Artist", "album": song.get("album") or "", **data}


# SCROBBLE
# ============================================================

@app.get("/rest/scrobble.view")
@app.get("/rest/scrobble")
async def rest_scrobble(request: Request, id: str = Query(""), submission: bool = Query(True), timeMs: Optional[int] = Query(None), time: Optional[int] = Query(None), duration: Optional[int] = Query(None)):
    error = await require_auth(request)
    if error: return error
    resolved_id = await asyncio.to_thread(_resolve_song_id_sync, id)
    song = await find_song(resolved_id) if resolved_id else None
    if not song:
        return subsonic_error(request, 70, "Song not found.")
    username, _ = subsonic_credentials()
    supplied_time = timeMs if timeMs is not None else time
    position = max(0.0, float(supplied_time or 0) / 1000.0) if supplied_time is not None else 0.0
    song_duration = float(duration or song.get("duration") or 0)
    fingerprint = hashlib.sha256(f"{username}|{resolved_id}|{bool(submission)}|{int(position//30)}".encode()).hexdigest()
    await asyncio.to_thread(_persist_subsonic_scrobble_sync, fingerprint, username, resolved_id, bool(submission), position, song_duration)
    if submission:
        await asyncio.to_thread(_persist_scrobble_history_sync, resolved_id, song_duration, position)
        await broadcast_stats_invalidated("subsonic_scrobble")
    return make_subsonic_response({"status":"ok","version":SUBSONIC_VERSION,"serverVersion":SERVER_VERSION,"openSubsonic":True,"type":"Xrob Music"},request)


@app.get("/rest/getNowPlaying.view")
@app.get("/rest/getNowPlaying")
async def rest_now_playing(request: Request):
    error = await require_auth(request)
    if error: return error
    pdata = await get_player_state_async()
    state = pdata.get("state") if isinstance(pdata,dict) else None
    entries=[]
    if isinstance(state,dict) and state.get("src") and not pdata.get("stale"):
        song = await find_song(str(state.get("songId") or "")) if state.get("songId") else None
        if song:
            elapsed=float(state.get("currentTime") or 0)
            if not state.get("paused"):
                elapsed=_effective_player_position(state,time.time())
            item=await songs_to_subsonic_async([song])
            if item:
                item[0].update({"username":subsonic_credentials()[0],"playerName":state.get("deviceName") or state.get("clientId") or "Xrob Music","minutesAgo":max(0,int((time.time()-float(state.get("lastSeenAt") or time.time()))/60)),"playerId":state.get("clientId") or "xrob","isVideo":False,"position":int(elapsed),"remaining":int(max(0,float(song.get("duration") or 0)-elapsed))})
                entries=item
        elif state.get("title"):
            entries=[{"id":str(state.get("songId") or state.get("src")),"title":state.get("title") or "Unknown Track","artist":state.get("artist") or "Unknown Artist","album":state.get("album") or "","duration":int(state.get("duration") or 0),"position":int(state.get("currentTime") or 0),"username":subsonic_credentials()[0],"playerName":state.get("deviceName") or "Xrob Music"}]
    return make_subsonic_response({"status":"ok","version":SUBSONIC_VERSION,"serverVersion":SERVER_VERSION,"openSubsonic":True,"type":"Xrob Music","nowPlaying":{"entry":entries}},request)


@app.get("/rest/getSimilarSongs2.view")
@app.get("/rest/getSimilarSongs2")
async def rest_similar_songs(request: Request,id: str = Query(...),count: int = Query(20)):
    error = await require_auth(request)
    if error: return error
    target=await find_song(id)
    if not target: return subsonic_error(request,70,"Song not found.")
    wanted=_bounded_subsonic_int(count,20,500)
    cache_key=(str(target.get("id") or id),wanted)
    cached=SIMILARITY_RESULTS_CACHE.get(cache_key)
    now=time.monotonic()
    if cached and now-cached[0] < SIMILARITY_CACHE_TTL:
        chosen_ids=cached[1]
        library=await build_library()
        by_id=library.get("_songs_by_id",{})
        chosen=[by_id[sid] for sid in chosen_ids if sid in by_id]
        return make_subsonic_response({"status":"ok","version":SUBSONIC_VERSION,"serverVersion":SERVER_VERSION,"openSubsonic":True,"type":"Xrob Music","similarSongs2":{"song":await songs_to_subsonic_async(chosen)}},request)
    library=await build_library()
    songs=library["songs"]
    target_artist=_compact_identity(target.get("artist")); target_genre=_compact_identity(target.get("genre")); target_album_artist=_compact_identity(target.get("albumArtist")); target_album=_compact_identity(target.get("album")); target_year=str(target.get("year") or "")
    dur_a=safe_float(target.get("duration"),0)
    candidates=[]; seen=set()
    for song in songs:
        if song["id"]==target["id"] or song["id"] in seen: continue
        artist_key=_compact_identity(song.get("artist")); genre_key=_compact_identity(song.get("genre")); album_artist_key=_compact_identity(song.get("albumArtist")); album_key=_compact_identity(song.get("album")); year=str(song.get("year") or "")
        dur_b=safe_float(song.get("duration"),0)
        duration_band=bool(dur_a and dur_b and abs(dur_a-dur_b)<=45)
        if not (artist_key==target_artist or genre_key==target_genre or album_artist_key==target_album_artist or album_key==target_album or (target_year and year==target_year) or duration_band):
            continue
        seen.add(song["id"]); candidates.append(song)
    if len(candidates) < wanted * 4:
        candidates=[song for song in songs if song["id"]!=target["id"]][:min(len(songs), max(wanted*8, 160))]
    scored=[]
    for song in candidates:
        title=_similarity(target.get("title",""),song.get("title",""))
        artist=_similarity(target.get("artist",""),song.get("artist",""))
        album_artist=_similarity(target.get("albumArtist",""),song.get("albumArtist",""))
        album=_similarity(target.get("album",""),song.get("album",""))
        genre=1.0 if target.get("genre") and target.get("genre").casefold()==song.get("genre","").casefold() else 0.0
        year=1.0 if target.get("year") and target.get("year")==song.get("year") else 0.0
        dur_b=safe_float(song.get("duration"),0)
        duration=max(0.0,1.0-min(abs(dur_a-dur_b),30.0)/30.0) if dur_a and dur_b else 0.0
        same_album_artist = 1.0 if target_album_artist and target_album_artist == _compact_identity(song.get("albumArtist")) else 0.0
        score=artist*0.32+album_artist*0.12+album*0.14+genre*0.14+year*0.05+duration*0.08+same_album_artist*0.10+title*0.05
        scored.append((score,song))
    scored.sort(key=lambda x:(x[0],safe_int(x[1].get("play_count"),0)),reverse=True)
    chosen=[song for _score,song in scored[:wanted]]
    _cache_set_bounded(SIMILARITY_RESULTS_CACHE,cache_key,([time.monotonic(),[s["id"] for s in chosen]]),SIMILARITY_CACHE_MAX)
    return make_subsonic_response({"status":"ok","version":SUBSONIC_VERSION,"serverVersion":SERVER_VERSION,"openSubsonic":True,"type":"Xrob Music","similarSongs2":{"song":await songs_to_subsonic_async(chosen)}},request)


def _extract_lyrics_tags_sync(path: Path):
    result={"plain":"","synced":""}
    if MutagenFile is None:
        return result
    try:
        audio=MutagenFile(str(path),easy=True)
        tags=getattr(audio,"tags",None)
        if tags:
            for key in ("lyrics","unsyncedlyrics"):
                value=tags.get(key)
                if isinstance(value,(list,tuple)):
                    value=value[0] if value else ""
                if value and str(value).strip():
                    result["plain"]=str(value).strip(); break
    except Exception:
        pass
    try:
        audio=MutagenFile(str(path),easy=False)
        tags=getattr(audio,"tags",None)
        if tags:
            if hasattr(tags,"getall"):
                for frame in tags.getall("USLT"):
                    value=getattr(frame,"text","")
                    if isinstance(value,(list,tuple)):
                        value=value[0] if value else ""
                    if value and str(value).strip():
                        result["plain"]=str(value).strip(); break
                for frame in tags.getall("SYLT"):
                    items=getattr(frame,"text",None) or getattr(frame,"synced_text",None)
                    if isinstance(items,list):
                        lines=[]
                        for pair in items:
                            if isinstance(pair,(tuple,list)) and len(pair)>=2:
                                lines.append((int(pair[1]),str(pair[0])))
                        if lines:
                            result["synced"]="\\n".join(f"[{ms//60000:02d}:{(ms%60000)//1000:02d}.{ms%1000:03d}]{text}" for ms,text in sorted(lines))
                            break
            # Vorbis/MP4 raw tags may still expose a textual LYRICS field.
            if not result["plain"]:
                for key in ("LYRICS","lyrics","UNSYNCEDLYRICS"):
                    try:
                        value=tags.get(key)
                        if isinstance(value,(list,tuple)):
                            value=value[0] if value else ""
                        if value and str(value).strip():
                            result["plain"]=str(value).strip(); break
                    except Exception:
                        pass
    except Exception:
        pass
    return result


async def _fetch_lrclib_lyrics(song):
    artist=clean_metadata_text(song.get("artist"),"")
    title=clean_metadata_text(song.get("title"),"")
    album=clean_metadata_text(song.get("album"),"")
    duration=safe_float(song.get("duration"),0)
    if not artist or not title: return {}
    cache_key=("lrclib",_compact_identity(artist),_compact_identity(title),_compact_identity(album),int(duration))
    # METADATA_CACHE is already bounded and safe to reuse as a short-lived process cache.
    if cache_key in METADATA_CACHE:
        return METADATA_CACHE[cache_key] or {}
    try:
        params={"artist_name":artist,"track_name":title}
        if album: params["album_name"]=album
        if duration: params["duration"]=int(duration)
        data=await _metadata_http_json("https://lrclib.net/api/get?"+urllib.parse.urlencode(params),{"User-Agent":f"Xrob Music/{SERVER_VERSION}"},timeout=LYRICS_LOOKUP_TIMEOUT_SECONDS,attempts=2)
        parsed={"plainLyrics":clean_metadata_text(data.get("plainLyrics"),""),"syncedLyrics":str(data.get("syncedLyrics") or ""),"source":"LRCLIB"}
        _cache_set_bounded(METADATA_CACHE,cache_key,parsed,LYRICS_CACHE_MAX)
        return parsed
    except Exception as exc:
        await write_app_error("lyrics",_sanitize_external_error(exc,"Lyrics lookup failed.",400))
        _cache_set_bounded(METADATA_CACHE,cache_key,{},LYRICS_CACHE_MAX)
        return {}


@app.get("/rest/getLyricsBySongId.view")
@app.get("/rest/getLyricsBySongId")
async def rest_lyrics(request: Request,id: str = Query("")):
    error=await require_auth(request)
    if error: return error
    song=await find_song(id)
    if not song: return subsonic_error(request,70,"Song not found.")
    lyrics = await _get_song_lyrics(song)
    payload={"status":"ok","version":SUBSONIC_VERSION,"serverVersion":SERVER_VERSION,"openSubsonic":True,"type":"Xrob Music","lyricsList":{"structuredLyrics":lyrics.get("structuredLyrics",[])}}
    if lyrics.get("plainLyrics"):
        payload["lyricsList"]["plainLyrics"]=lyrics["plainLyrics"]
    return make_subsonic_response(payload,request)




# ============================================================
# END
# ============================================================
