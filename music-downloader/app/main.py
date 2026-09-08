import asyncio
import binascii
import hashlib
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
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

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


# ============================================================
# XROB MUSIC
# Downloader + Library + OpenSubsonic server
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="Xrob Music",
    version="2.6.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
    "/share/mymusic/music",
)

# The library path can be changed by the Home Assistant add-on through
# /data/options.json. The legacy DOWNLOAD_DIR environment variable remains
# supported for Docker users.
DOWNLOAD_DIR = Path(DEFAULT_LIBRARY_PATH)
COVER_CACHE_DIR = DOWNLOAD_DIR / ".covers"
DATA_DIR = Path(os.getenv("XROB_DATA_DIR", "/data"))
DB_FILE = DATA_DIR / "tasks.db"
SETTINGS_FILE = DATA_DIR / "settings.json"

ADDON_OPTIONS_FILE = Path("/data/options.json")

SUBSONIC_VERSION = "1.16.1"
SERVER_VERSION = "2.7.0"

MAX_CONCURRENT_DOWNLOADS = 3

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
    "subsonic_user": "admin",
    "subsonic_password": "",
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
LIBRARY_CACHE = None
LIBRARY_CACHE_TIME = 0.0
LIBRARY_CACHE_TTL = 10.0
LIBRARY_CACHE_LOCK = asyncio.Lock()


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
    try:
        source_conn = sqlite3.connect(source, timeout=5.0)
        source_conn.execute("PRAGMA busy_timeout = 5000")
        destination_conn = sqlite3.connect(destination, timeout=30.0)
        try:
            source_conn.backup(destination_conn)
        finally:
            destination_conn.close()
            source_conn.close()
        print(f"Migrated legacy database: {source} -> {destination}")
    except (sqlite3.Error, OSError) as exc:
        try:
            if destination.exists():
                destination.unlink()
        except OSError:
            pass
        print(f"Warning: could not migrate legacy database: {exc}. Starting with a new local database.")


def configure_storage():
    """Apply the configured library path and prepare its local metadata cache."""
    global DOWNLOAD_DIR, COVER_CACHE_DIR, SETTINGS_FILE, DB_FILE

    addon = load_addon_options()
    configured = str(addon.get("music_path") or "").strip()
    path = Path(configured or os.getenv("DOWNLOAD_DIR", DEFAULT_LIBRARY_PATH)).expanduser()

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
    settings.pop("max_results", None)

    return settings


def save_settings(data: dict):
    if not isinstance(data, dict):
        raise ValueError("Settings must be an object.")

    settings = load_settings()
    allowed = {
        "audio_format", "audio_quality", "embed_thumbnail",
        "embed_metadata", "organize_by_artist",
    }

    for key in allowed & data.keys():
        settings[key] = data[key]

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

    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)

    return settings


def public_settings():
    settings = dict(load_settings())
    # Subsonic credentials are add-on/server configuration, not web UI settings.
    settings.pop("subsonic_user", None)
    settings.pop("subsonic_password", None)
    settings["storage"] = storage_info_sync()
    return settings


# ============================================================
# DATABASE
# ============================================================

def db_connect():
    """Open the local persistent SQLite database with safe lock handling.

    The music library may live on SMB/NFS, but SQLite must stay on the
    add-on's local /data volume. This avoids unreliable file locking on
    network filesystems.
    """
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with db_connect() as conn:
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
        conn.execute("""CREATE TABLE IF NOT EXISTS scan_state (id INTEGER PRIMARY KEY CHECK (id=1), started_at REAL, finished_at REAL, mode TEXT, status TEXT, message TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS app_errors (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL, source TEXT, message TEXT, task_id TEXT)""")
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
        conn.commit()


def db_save_task_sync(task):
    with db_connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO tasks (
                id,
                title,
                artist,
                album,
                url,
                elementId,
                status,
                percent,
                speed,
                step,
                error,
                last_updated,
                final_name,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task.get("id"),
                task.get("title"),
                task.get("artist"),
                task.get("album"),
                task.get("url"),
                task.get("elementId"),
                task.get("status"),
                task.get("percent", 0),
                task.get("speed", ""),
                task.get("step", ""),
                task.get("error", ""),
                task.get("last_updated", 0),
                task.get("final_name", ""),
                task.get("created_at", task.get("last_updated", 0)),
            ),
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
        for websocket in list(self.connections):
            try:
                await websocket.send_json(message)
            except Exception:
                self.disconnect(websocket)


manager = ConnectionManager()


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
        return float(value)
    except Exception:
        return default


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


def iso_utc(timestamp):
    timestamp = safe_float(timestamp, 0)
    if timestamp <= 0:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


def clean_metadata_text(
    value,
    fallback="",
):
    value = str(value or "").strip()

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


def normalize_duplicate_key(value):
    value = Path(
        value or ""
    ).stem.lower()

    value = re.sub(
        r"\b(official\s*(video|audio|music video)|lyrics?|hd|4k|remaster(ed)?|audio)\b",
        " ",
        value,
        flags=re.I,
    )

    return re.sub(
        r"[^a-z0-9]+",
        "",
        value,
    )


# ============================================================
# LIBRARY FILES
# ============================================================

def get_audio_files_sync():
    return [
        path
        for path in DOWNLOAD_DIR.rglob("*")
        if (
            path.is_file()
            and not path.name.startswith(".")
            and path.suffix.lower() in AUDIO_EXTENSIONS
        )
    ]


async def get_all_audio_files():
    return await asyncio.to_thread(
        get_audio_files_sync
    )


def resolve_file_sync(filename):
    base = DOWNLOAD_DIR.resolve()
    target = (
        DOWNLOAD_DIR / filename
    ).resolve()

    try:
        safe = target.is_relative_to(base)
    except AttributeError:
        safe = (
            target == base
            or base in target.parents
        )

    if not safe:
        raise HTTPException(
            status_code=403,
            detail="Access denied",
        )

    if (
        target.exists()
        and target.is_file()
    ):
        return target

    target_name = Path(filename).name
    matches = [
        match.resolve()
        for match in DOWNLOAD_DIR.rglob("*")
        if match.is_file() and match.name == target_name
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise HTTPException(
            status_code=409,
            detail="Ambiguous filename; use the full library-relative path.",
        )

    raise HTTPException(
        status_code=404,
        detail="File not found",
    )


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
    }


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
                if getattr(audio, "info", None):
                    metadata["duration"] = safe_float(getattr(audio.info, "length", 0), metadata["duration"])
                    metadata["bit_rate"] = safe_int(getattr(audio.info, "bitrate", 0) / 1000, metadata["bit_rate"])
        except Exception:
            pass

        metadata["artist"] = clean_metadata_text(metadata.get("artist"), fallback["artist"])
        metadata["album_artist"] = clean_metadata_text(metadata.get("album_artist"), metadata["artist"])
        metadata["album"] = clean_metadata_text(metadata.get("album"), fallback["album"])
        metadata["title"] = clean_metadata_text(metadata.get("title"), fallback["title"])
        METADATA_CACHE[cache_key] = (cache_stamp, metadata)
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

    digest = hashlib.sha1(
        name.encode("utf-8")
    ).hexdigest()[:20]

    return f"artist-{digest}"


def make_album_id(
    artist,
    album,
):
    raw = (
        str(artist)
        + "\x00"
        + str(album)
    )

    digest = hashlib.sha1(
        raw.encode("utf-8")
    ).hexdigest()[:20]

    return f"album-{digest}"


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

        songs = []
        artists = {}
        albums = {}
        genres = {}

        for path in files:
            try:
                stat = path.stat()
                metadata = await read_metadata(path)
            except Exception:
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

        LIBRARY_CACHE = {"songs": songs, "artists": artists, "albums": albums, "genres": genres}
        LIBRARY_CACHE_TIME = time.monotonic()
        return LIBRARY_CACHE


async def find_song(song_id):
    library = await build_library()

    for song in library["songs"]:
        if song["id"] == song_id:
            return song

    return None


async def find_artist(artist_id):
    library = await build_library()

    return library["artists"].get(
        artist_id
    )


async def find_album(album_id):
    library = await build_library()

    return library["albums"].get(
        album_id
    )


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


async def ensure_cover(path):

    cover = cover_cache_path(path)

    if cover.exists():
        return cover

    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(path),
        "-an",
        "-vcodec",
        "mjpeg",
        "-vframes",
        "1",
        str(cover),
    ]

    try:
        await asyncio.to_thread(
            subprocess.run,
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except Exception:
        pass

    return cover if cover.exists() else None


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
# DOWNLOAD WORKER
# ============================================================

async def download_worker():

    while True:

        queue_item = await TASK_QUEUE.get()
        if isinstance(queue_item, (tuple, list)) and len(queue_item) == 2:
            task_id, queue_token = queue_item
        else:
            task_id, queue_token = queue_item, None

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

            settings = load_settings()

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
                "-o",
                output_template,
            ]

            if settings.get(
                "embed_thumbnail",
                True,
            ):
                command.append(
                    "--embed-thumbnail"
                )

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

                line = (
                    await process.stdout.readline()
                )

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

                elif any(
                    marker in text
                    for marker in (
                        "[ExtractAudio]",
                        "[EmbedThumbnail]",
                        "[Metadata]",
                        "[Fixup]",
                    )
                ):

                    task["status"] = "processing"
                    task["percent"] = 92
                    task["step"] = (
                        "Processing metadata..."
                    )
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

                await asyncio.to_thread(
                    cleanup_task_files,
                    task_id,
                )

                task["status"] = "error"
                task["step"] = "Download failed"
                task["error"] = (
                    error_text[-1200:]
                    or "yt-dlp failed."
                )
                task["last_updated"] = (
                    time.time() * 1000
                )

                await notify_task_update(
                    task,
                    force_save=True,
                )

                continue

            possible_files = [
                path
                for path in DOWNLOAD_DIR.glob(
                    f"{task_id}.*"
                )
                if (
                    path.is_file()
                    and path.suffix.lower()
                    not in {
                        ".part",
                        ".ytdl",
                        ".temp",
                    }
                )
            ]

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

            if settings.get("embed_metadata", True):
                task["status"] = "processing"
                task["percent"] = 96
                task["step"] = "Finalizing metadata..."
                task["last_updated"] = time.time() * 1000
                await notify_task_update(task, force_save=True)

                clean_title = clean_filename(task.get("title", "Unknown Track"))
                clean_file = DOWNLOAD_DIR / f"clean_{task_id}{extension}"
                clean_command = [
                    *FFMPEG_COMMAND, "-y", "-i", str(audio_file), "-map", "0", "-c", "copy",
                    "-metadata", f"title={clean_title}",
                    "-metadata", f"artist={task.get('artist', 'Unknown Artist')}",
                    "-metadata", f"album={task.get('album') or clean_title}",
                    str(clean_file),
                ]
                clean_process = await asyncio.create_subprocess_exec(
                    *clean_command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                ACTIVE_PROCESSES[task_id] = clean_process
                _, clean_stderr = await clean_process.communicate()
                ACTIVE_PROCESSES.pop(task_id, None)

                if task.get("cancel_requested"):
                    await asyncio.to_thread(cleanup_task_files, task_id)
                    task["status"] = "cancelled"
                    task["step"] = "Cancelled"
                    task["percent"] = 0
                    task["last_updated"] = time.time() * 1000
                    await notify_task_update(task, force_save=True)
                    continue

                if clean_process.returncode == 0 and clean_file.exists():
                    try:
                        audio_file.unlink()
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

            if settings.get(
                "organize_by_artist",
                False,
            ):

                final_dir = (
                    DOWNLOAD_DIR / artist
                )

                final_dir.mkdir(
                    parents=True,
                    exist_ok=True,
                )

            else:
                final_dir = DOWNLOAD_DIR

            final_name = (
                f"{clean_title}{extension}"
            )

            final_path = (
                final_dir / final_name
            )

            if final_path.exists():
                final_name = f"{clean_title}_{task_id[:4]}{extension}"
                final_path = final_dir / final_name

            shutil.move(
                str(audio_file),
                str(final_path),
            )

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

            await asyncio.to_thread(
                cleanup_task_files,
                task_id,
            )

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

        finally:

            ACTIVE_PROCESSES.pop(
                task_id,
                None,
            )

            TASK_QUEUE.task_done()


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
async def startup_event():

    await asyncio.to_thread(configure_storage)
    await asyncio.to_thread(init_db)

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

            await db_save_task(
                task,
                force=True,
            )

    for _ in range(
        MAX_CONCURRENT_DOWNLOADS
    ):
        asyncio.create_task(
            download_worker()
        )

    asyncio.create_task(scheduled_library_scanner())

    for task in TASKS.values():

        if task.get(
            "status"
        ) == "queued":

            await TASK_QUEUE.put((task["id"], task["queue_token"]))


# ============================================================
# WEBSOCKET ENDPOINT
# ============================================================

@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
):

    await manager.connect(
        websocket
    )

    try:

        while True:
            await websocket.receive_text()

    except WebSocketDisconnect:

        manager.disconnect(
            websocket
        )

    except Exception:

        manager.disconnect(
            websocket
        )


# ============================================================
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
    return public_settings()


@app.post("/api/settings")
async def api_post_settings(
    data: dict = Body(...),
):
    try:
        save_settings(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return public_settings()


# ============================================================
# YOUTUBE SEARCH
# ============================================================

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

    stdout, stderr = await process.communicate()

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


@app.get("/api/search")
async def api_search(
    q: str = Query(...),
    page: int = Query(1),
):

    if not q.strip():
        return []

    max_results = 20

    try:

        return await youtube_search(
            q,
            max_results,
            max(1, page),
        )

    except Exception as error:

        raise HTTPException(
            status_code=500,
            detail=str(error),
        )


# ============================================================
# PREVIEW
# ============================================================

@app.get("/api/preview")
async def api_preview(
    url: str = Query(...),
):

    if not url:
        raise HTTPException(
            status_code=400,
            detail="URL missing",
        )

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

    stdout, stderr = await process.communicate()

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

        try:

            while True:

                chunk = await ffmpeg.stdout.read(
                    64 * 1024
                )

                if not chunk:
                    break

                yield chunk

        finally:

            if ffmpeg.returncode is None:

                try:
                    ffmpeg.kill()
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

@app.post("/api/download")
async def api_download(
    payload: dict = Body(...),
):

    url = payload.get("url")

    if not url:
        raise HTTPException(
            status_code=400,
            detail="Missing URL",
        )

    for task in TASKS.values():

        if (
            task.get("url") == url
            and task.get("status") in {
                "queued",
                "downloading",
                "processing",
            }
        ):

            return {
                "status": "already_queued",
                "task_id": task["id"],
            }

    task_id = uuid.uuid4().hex[:12]

    task = {
        "id": task_id,
        "title": str(
            payload.get(
                "title",
                "Unknown Track",
            )
        ),
        "artist": str(
            payload.get(
                "artist",
                "Unknown Artist",
            )
        ),
        "album": str(
            payload.get(
                "album",
                payload.get("title", "Unknown Track"),
            )
        ),
        "url": url,
        "elementId": str(
            payload.get(
                "elementId",
                "",
            )
        ),
        "status": "queued",
        "percent": 0,
        "speed": "",
        "step": "Queued...",
        "error": "",
        "last_updated": (
            time.time() * 1000
        ),
        "final_name": "",
        "cancel_requested": False,
        "created_at": time.time() * 1000,
        "queue_token": uuid.uuid4().hex,
    }

    TASKS[task_id] = task

    await notify_task_update(
        task,
        force_save=True,
    )

    await TASK_QUEUE.put((task_id, task["queue_token"]))

    return {
        "status": "ok",
        "task_id": task_id,
    }


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
    }:

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

    files = await get_all_audio_files()

    result = []
    total = 0

    for path in files:

        try:
            size = path.stat().st_size
        except Exception:
            continue

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
    song_by_path = {str(song["path"]): song for song in library["songs"]}
    for item in result:
        path = str((DOWNLOAD_DIR / item["name"]).resolve())
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
        "storage": storage_info_sync(),
        "artists": artists,
        "albums": albums,
    }


@app.post("/api/library/scan")
async def api_library_scan():
    invalidate_library_cache()
    library = await build_library(force=True)
    storage = await asyncio.to_thread(storage_info_sync)
    return {"status": "ok", "tracks": len(library["songs"]), "artists": len(library["artists"]), "albums": len(library["albums"]), "storage": storage}


@app.get("/api/stats")
async def api_stats():

    library = await build_library()

    songs = library["songs"]

    artists = library["artists"]
    albums = library["albums"]

    total = sum(
        song["size"]
        for song in songs
    )

    return {
        "tracks": len(songs),
        "artists": len(artists),
        "albums": len(albums),
        "total_bytes": total,
        "folder_size": format_size(total),
    }


@app.get("/api/home")
async def api_home():

    # Home must stay fast. Do not rebuild the entire metadata library
    # here because the frontend also requests /api/stats separately.
    files = await get_all_audio_files()

    files.sort(
        key=lambda path: path.stat().st_mtime
        if path.exists()
        else 0,
        reverse=True,
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
    return {
        "stats": {
            "tracks": len(library["songs"]),
            "artists": len(library["artists"]),
            "albums": len(library["albums"]),
            "total_bytes": total_bytes,
            "folder_size": format_size(total_bytes),
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

        cover = cover_cache_path(path)

        path.unlink()

        if cover.exists():
            cover.unlink()

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
            """
            SELECT starred_at
            FROM stars
            WHERE item_id = ?
            """,
            (item_id,),
        ).fetchone()

    return float(row[0]) if row else None


def is_starred_sync(item_id):
    return get_starred_at_sync(item_id) is not None


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

def song_to_subsonic(song):

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
    # not booleans. Only include starred when the song is actually starred.
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
            song
            for song in library[
                "songs"
            ]
            if song["id"]
            in album["songIds"]
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
        song
        for song in library[
            "songs"
        ]
        if song["id"]
        in album["songIds"]
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
                "song": [
                    song_to_subsonic(
                        song
                    )
                    for song in songs
                ],
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

    album_data = []

    for album in library[
        "albums"
    ].values():

        songs = [
            song
            for song in library[
                "songs"
            ]
            if song["id"]
            in album["songIds"]
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

    start = max(
        0,
        offset,
    )

    end = (
        start
        + max(
            1,
            min(500, max(1, size)),
        )
    )

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
                    song
                    for song in library[
                        "songs"
                    ]
                    if song["id"]
                    in album["songIds"]
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
                song
                for song in library[
                    "songs"
                ]
                if song["id"]
                in album["songIds"]
            ]

            children.extend(
                song_to_subsonic(song)
                for song in songs
            )

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

    return make_subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "type": "Xrob Music",
            "song": song_to_subsonic(
                song
            ),
        },
        request,
    )


# ============================================================
# SEARCH
# ============================================================

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

    if music_folder_id not in (None, "", "1", 1):
        return subsonic_error(request, 70, "Music folder not found.")

    q = query.lower().strip()

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
            song
            for song in library[
                "songs"
            ]
            if song["id"]
            in album["songIds"]
        ]

        album_objects.append(
            album_to_subsonic(
                album,
                album_songs,
            )
        )

    song_objects = [
        song_to_subsonic(song)
        for song in matching_songs
    ]

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
                "song": [
                    song_to_subsonic(
                        song
                    )
                    for song
                    in songs[
                        :max(
                            1,
                            size,
                        )
                    ]
                ],
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
                "song": [
                    song_to_subsonic(song)
                    for song
                    in songs[
                        offset:
                        offset + count
                    ]
                ],
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

    with db_connect() as conn:

        rows = conn.execute(
            "SELECT item_id FROM stars"
        ).fetchall()

    starred_ids = {
        row[0]
        for row in rows
    }

    library = await build_library()

    songs = [
        song_to_subsonic(song)
        for song in library[
            "songs"
        ]
        if song["id"] in starred_ids
    ]

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
    return [str(item) for item in value if item is not None]


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

    songs = []

    for song_id in ids:

        song = next(
            (
                item
                for item in library[
                    "songs"
                ]
                if item["id"] == song_id
            ),
            None,
        )

        if song:
            songs.append(
                song_to_subsonic(song)
            )

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

async def write_app_error(source, message, task_id=None):
    try:
        with db_connect() as conn:
            conn.execute("INSERT INTO app_errors(created_at,source,message,task_id) VALUES(?,?,?,?)", (time.time(), str(source), str(message), task_id))
            conn.commit()
    except Exception:
        pass


def _playlist_row_to_dict(row):
    d=dict(row)
    d["song_ids"]=safe_song_ids(d.get("song_ids", "[]"))
    d["rules"]=json.loads(d.get("rules") or "{}") if isinstance(d.get("rules"), str) else (d.get("rules") or {})
    d["public"]=bool(d.get("public", 0))
    d["kind"]=d.get("kind") or "manual"
    return d


@app.get("/api/player/positions")
async def api_player_positions():
    with db_connect() as conn:
        rows=conn.execute("SELECT song_id,position,duration,updated_at FROM playback_positions").fetchall()
    return {r[0]: {"position":r[1],"duration":r[2],"updated_at":r[3]} for r in rows}


@app.post("/api/player/position")
async def api_player_position(payload: dict = Body(...)):
    song_id=str(payload.get("song_id") or "").strip()
    if not song_id: raise HTTPException(400, "song_id is required")
    position=max(0.0, float(payload.get("position") or 0))
    duration=max(0.0, float(payload.get("duration") or 0))
    now=time.time()
    with db_connect() as conn:
        conn.execute("INSERT INTO playback_positions(song_id,position,duration,updated_at) VALUES(?,?,?,?) ON CONFLICT(song_id) DO UPDATE SET position=excluded.position,duration=excluded.duration,updated_at=excluded.updated_at", (song_id,position,duration,now))
        conn.commit()
    return {"status":"ok"}


@app.post("/api/player/history")
async def api_player_history(payload: dict = Body(...)):
    song_id=str(payload.get("song_id") or "").strip()
    if not song_id: raise HTTPException(400, "song_id is required")
    with db_connect() as conn:
        conn.execute("INSERT INTO play_history(song_id,played_at,duration,position) VALUES(?,?,?,?)", (song_id,time.time(),float(payload.get("duration") or 0),float(payload.get("position") or 0)))
        conn.commit()
    return {"status":"ok"}


@app.get("/api/library/recent-most")
async def api_recent_most():
    library=await build_library()
    by_id={s["id"]:s for s in library["songs"]}
    with db_connect() as conn:
        recent=conn.execute("SELECT song_id, MAX(played_at) t FROM play_history GROUP BY song_id ORDER BY t DESC LIMIT 24").fetchall()
        most=conn.execute("SELECT song_id, COUNT(*) c, MAX(played_at) t FROM play_history GROUP BY song_id ORDER BY c DESC, t DESC LIMIT 24").fetchall()
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
    with db_connect() as conn:
        conn.row_factory=sqlite3.Row
        rows=conn.execute("SELECT * FROM playlists ORDER BY name COLLATE NOCASE").fetchall()
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
    name=str(payload.get("name") or "New Playlist").strip()
    if not name: raise HTTPException(400,"Playlist name required")
    ids=[str(x) for x in (payload.get("song_ids") or [])]
    kind="smart" if payload.get("kind")=="smart" else "manual"
    rules=payload.get("rules") or {}
    now=time.time(); pid="playlist-"+uuid.uuid4().hex[:16]
    with db_connect() as conn:
        conn.execute("INSERT INTO playlists(id,name,comment,owner,public,song_ids,created_at,updated_at,kind,rules) VALUES(?,?,?,?,?,?,?,?,?,?)", (pid,name,str(payload.get("comment") or ""),"admin",0,json.dumps(ids),now,now,kind,json.dumps(rules)))
        conn.commit()
    return {"status":"ok","id":pid}


@app.put("/api/playlists/{playlist_id}")
async def api_playlist_update(playlist_id:str,payload:dict=Body(...)):
    with db_connect() as conn:
        row=conn.execute("SELECT * FROM playlists WHERE id=?",(playlist_id,)).fetchone()
        if not row: raise HTTPException(404,"Playlist not found")
        current=dict(row)
        name=str(payload.get("name",current["name"])).strip()
        ids=[str(x) for x in payload.get("song_ids",safe_song_ids(current.get("song_ids","[]")))]
        kind=payload.get("kind", current.get("kind","manual")); rules=payload.get("rules", json.loads(current.get("rules") or "{}"))
        conn.execute("UPDATE playlists SET name=?,comment=?,song_ids=?,updated_at=?,kind=?,rules=? WHERE id=?",(name,str(payload.get("comment",current.get("comment") or "")),json.dumps(ids),time.time(),kind,json.dumps(rules),playlist_id)); conn.commit()
    return {"status":"ok"}


@app.delete("/api/playlists/{playlist_id}")
async def api_playlist_delete(playlist_id:str):
    with db_connect() as conn:
        conn.execute("DELETE FROM playlists WHERE id=?",(playlist_id,)); conn.commit()
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
    files=await get_all_audio_files(); unreadable=[]; bad_tags=[]; missing_art=[]; groups={}
    for path in files:
        try: md=await read_metadata(path)
        except Exception as exc: unreadable.append({"path":str(path.relative_to(DOWNLOAD_DIR)),"error":str(exc)}); continue
        title=str(md.get("title") or "").strip(); artist=str(md.get("artist") or "").strip(); album=str(md.get("album") or "").strip()
        if not title or not artist or not album: bad_tags.append({"path":str(path.relative_to(DOWNLOAD_DIR)),"title":title,"artist":artist,"album":album})
        if not await ensure_cover(path): missing_art.append(str(path.relative_to(DOWNLOAD_DIR)))
        key=normalize_duplicate_key(f"{title}|{artist}|{album}")
        groups.setdefault(key,[]).append(str(path.relative_to(DOWNLOAD_DIR)))
    duplicates=[{"key":k,"files":v} for k,v in groups.items() if k and len(v)>1]
    return {"unreadable":unreadable,"bad_tags":bad_tags,"missing_artwork":missing_art,"duplicates":duplicates,"counts":{"unreadable":len(unreadable),"bad_tags":len(bad_tags),"missing_artwork":len(missing_art),"duplicates":len(duplicates)}}


@app.post("/api/library/scan/{mode}")
async def api_library_scan_mode(mode:str):
    if mode not in {"quick","full"}: raise HTTPException(400,"mode must be quick or full")
    with db_connect() as conn:
        conn.execute("INSERT INTO scan_state(id,started_at,finished_at,mode,status,message) VALUES(1,?,NULL,?, ?, ?) ON CONFLICT(id) DO UPDATE SET started_at=excluded.started_at,finished_at=NULL,mode=excluded.mode,status=excluded.status,message=excluded.message",(time.time(),mode,"running","Scanning")); conn.commit()
    try:
        invalidate_library_cache()
        library=await build_library(force=True)
        if mode=="full":
            for song in library["songs"]:
                try: await ensure_cover(song["path"])
                except Exception: pass
        with db_connect() as conn:
            conn.execute("UPDATE scan_state SET finished_at=?,status=?,message=? WHERE id=1",(time.time(),"ok",f"{len(library['songs'])} tracks scanned")); conn.commit()
        return {"status":"ok","mode":mode,"tracks":len(library["songs"])}
    except Exception as exc:
        await write_app_error("library_scan",str(exc))
        with db_connect() as conn:
            conn.execute("UPDATE scan_state SET finished_at=?,status=?,message=? WHERE id=1",(time.time(),"error",str(exc))); conn.commit()
        raise


@app.get("/api/library/scan/status")
async def api_library_scan_status():
    with db_connect() as conn:
        row=conn.execute("SELECT started_at,finished_at,mode,status,message FROM scan_state WHERE id=1").fetchone()
    return dict(zip(["started_at","finished_at","mode","status","message"],row)) if row else {"status":"idle"}


@app.get("/api/errors")
async def api_errors(limit:int=Query(200,ge=1,le=1000)):
    if not isinstance(limit, int):
        limit = 200
    limit = max(1, min(1000, limit))
    with db_connect() as conn:
        conn.row_factory=sqlite3.Row
        rows=conn.execute("SELECT id,created_at,source,message,task_id FROM app_errors ORDER BY created_at DESC LIMIT ?",(limit,)).fetchall()
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
    path=song["path"]; fields={k:payload.get(k) for k in ("title","artist","album","year","genre") if k in payload}
    if not fields: return {"status":"ok"}
    if MutagenFile is None: raise HTTPException(500,"Metadata library unavailable")
    def write_tags():
        audio=MutagenFile(path, easy=False)
        if audio is None: raise RuntimeError("Unsupported audio file")
        ext=path.suffix.lower()
        title=str(fields.get("title",song["title"]))
        artist=str(fields.get("artist",song["artist"]))
        album=str(fields.get("album",song["album"]))
        year=str(fields.get("year",song.get("year", "")))
        genre=str(fields.get("genre",song.get("genre", "")))
        if ext==".mp3":
            try: audio.add_tags()
            except Exception: pass
            if audio.tags is None: audio.tags=ID3()
            audio.tags.delall("TIT2"); audio.tags.delall("TPE1"); audio.tags.delall("TALB"); audio.tags.delall("TDRC"); audio.tags.delall("TCON")
            audio.tags.add(TIT2(encoding=3,text=title)); audio.tags.add(TPE1(encoding=3,text=artist)); audio.tags.add(TALB(encoding=3,text=album));
            if year: audio.tags.add(TDRC(encoding=3,text=year))
            if genre: audio.tags.add(TCON(encoding=3,text=genre))
        else:
            tags=audio.tags or {}
            for key,val in (("title",title),("artist",artist),("album",album),("date",year),("genre",genre)):
                if val: tags[key]=[val]
                else: tags.pop(key,None)
            audio.tags=tags
        audio.save()
    try: await asyncio.to_thread(write_tags)
    except Exception as exc: await write_app_error("metadata",str(exc)); raise HTTPException(500,f"Metadata update failed: {exc}")
    invalidate_library_cache(); return {"status":"ok"}


@app.post("/api/library/artwork/{song_id}")
async def api_library_artwork(song_id:str, upload:UploadFile=File(...)):
    song=await find_song(song_id)
    if not song: raise HTTPException(404,"Track not found")
    data=await upload.read()
    if not data or len(data)>15*1024*1024: raise HTTPException(400,"Invalid artwork")
    if MutagenFile is None: raise HTTPException(500,"Metadata library unavailable")
    def write_art():
        audio=MutagenFile(song["path"], easy=False)
        ext=song["path"].suffix.lower()
        mime=upload.content_type or "image/jpeg"
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
            settings=load_settings()
            enabled=bool(settings.get("scan_enabled",True)); minutes=max(5,int(settings.get("scan_interval_minutes",60) or 60))
            if enabled: await asyncio.sleep(minutes*60); await api_library_scan_mode("quick")
            else: await asyncio.sleep(300)
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
                "song": [
                    song_to_subsonic(song)
                    for song
                    in similar[
                        :max(0, count)
                    ]
                ],
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
