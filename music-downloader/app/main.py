import asyncio
import hashlib
import json
import os
import random
import re
import shutil
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
    version="2.3.0",
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

DOWNLOAD_DIR = Path(
    os.getenv(
        "DOWNLOAD_DIR",
        "/share/mymusic/music",
    )
)

DOWNLOAD_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

COVER_CACHE_DIR = DOWNLOAD_DIR / ".covers"
COVER_CACHE_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

SETTINGS_FILE = DOWNLOAD_DIR / ".settings.json"
DB_FILE = DOWNLOAD_DIR / "tasks.db"

ADDON_OPTIONS_FILE = Path("/data/options.json")

SUBSONIC_VERSION = "1.16.1"
SERVER_VERSION = "2.3.0"

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
    "max_results": 20,
    "organize_by_artist": False,
    "poll_interval": 1500,
    "subsonic_user": "admin",
    "subsonic_password": "",
}


# ============================================================
# RUNTIME
# ============================================================

TASKS = {}
TASK_QUEUE = asyncio.Queue()
ACTIVE_PROCESSES = {}
LAST_SAVED_TIME = {}
METADATA_CACHE = {}


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

    # Remove old Navidrome configuration.
    for key in (
        "navidrome_url",
        "navidrome_user",
        "navidrome_token",
        "navidrome_salt",
    ):
        settings.pop(key, None)

    return settings


def save_settings(data: dict):
    settings = load_settings()

    protected = {
        "subsonic_user",
        "subsonic_password",
        "navidrome_url",
        "navidrome_user",
        "navidrome_token",
        "navidrome_salt",
    }

    for key, value in data.items():
        if key not in protected:
            settings[key] = value

    for key in (
        "navidrome_url",
        "navidrome_user",
        "navidrome_token",
        "navidrome_salt",
    ):
        settings.pop(key, None)

    with open(
        SETTINGS_FILE,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            settings,
            f,
            indent=2,
        )

    return settings


def public_settings():
    settings = dict(load_settings())
    settings.pop("subsonic_password", None)
    return settings


# ============================================================
# DATABASE
# ============================================================

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
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
                final_name TEXT
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

        conn.commit()


def db_save_task_sync(task):
    with sqlite3.connect(DB_FILE) as conn:
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
                final_name
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row

        for row in conn.execute(
            "SELECT * FROM tasks"
        ):
            item = dict(row)
            tasks[item["id"]] = item

    return tasks


def db_clear_finished_sync():
    with sqlite3.connect(DB_FILE) as conn:
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
    with sqlite3.connect(DB_FILE) as conn:
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

    for match in DOWNLOAD_DIR.rglob("*"):
        if (
            match.is_file()
            and match.name == target_name
        ):
            return match.resolve()

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

def read_metadata_sync(path):
    try:
        stat = path.stat()
        cache_key = str(path)

        cached = METADATA_CACHE.get(cache_key)

        if cached and cached[0] == stat.st_mtime:
            return cached[1]

        # Read the complete container + stream metadata. In particular,
        # -show_format / -show_streams includes format-level and stream-level
        # tags written by yt-dlp/FFmpeg. Without the tags, artists/albums
        # incorrectly fall back to "Unknown Artist" / filename.
        command = [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )

        metadata = {}

        if result.returncode == 0:
            raw = json.loads(
                result.stdout or "{}"
            )

            fmt = raw.get(
                "format",
                {},
            )
            # Container-level music tags live here.
            fmt_tags = dict(fmt.get("tags") or {})

            streams = raw.get("streams") or []
            audio_stream = next(
                (
                    stream
                    for stream in streams
                    if stream.get("codec_type") == "audio"
                ),
                {},
            )

            tags = dict(fmt_tags)
            stream_tags = dict(audio_stream.get("tags") or {})

            # Merge container-level and audio-stream-level tags. Stream tags
            # win when a container stores the authoritative music metadata there.
            tags.update(stream_tags)

            # Normalize common tag casing used by different containers.
            normalized_tags = {
                str(key).lower(): value
                for key, value in tags.items()
            }

            metadata = {
                "title": clean_metadata_text(
                    normalized_tags.get("title"),
                    path.stem,
                ),
                "artist": clean_metadata_text(
                    normalized_tags.get("artist")
                    or normalized_tags.get("album_artist")
                    or normalized_tags.get("albumartist"),
                    (
                        path.parent.name
                        if path.parent != DOWNLOAD_DIR
                        else "Unknown Artist"
                    ),
                ),
                "album": clean_metadata_text(
                    normalized_tags.get("album"),
                    path.stem,
                ),
                "genre": clean_metadata_text(
                    normalized_tags.get("genre"),
                    "",
                ),
                "year": clean_metadata_text(
                    normalized_tags.get("date")
                    or normalized_tags.get("year"),
                    "",
                ),
                "track": clean_metadata_text(
                    normalized_tags.get("tracknumber")
                    or normalized_tags.get("track"),
                    "",
                ),
                "disc": clean_metadata_text(
                    normalized_tags.get("discnumber")
                    or normalized_tags.get("disc"),
                    "",
                ),
                "duration": safe_float(
                    fmt.get("duration"),
                    0,
                ),
                "bit_rate": safe_int(
                    safe_float(audio_stream.get("bit_rate"), 0) / 1000,
                    0,
                ),
                "sample_rate": safe_int(
                    audio_stream.get("sample_rate"),
                    0,
                ),
                "channels": safe_int(
                    audio_stream.get("channels"),
                    0,
                ),
                "bit_depth": safe_int(
                    audio_stream.get("bits_per_raw_sample")
                    or audio_stream.get("bits_per_sample"),
                    0,
                ),
            }

            if metadata["bit_rate"] <= 0:
                metadata["bit_rate"] = safe_int(
                    safe_float(fmt.get("bit_rate"), 0) / 1000,
                    0,
                )

        if not metadata:
            metadata = {
                "title": path.stem,
                "artist": (
                    path.parent.name
                    if path.parent != DOWNLOAD_DIR
                    else "Unknown Artist"
                ),
                "album": path.stem,
                "genre": "",
                "year": "",
                "track": "",
                "disc": "",
                "duration": 0,
                "bit_rate": 0,
                "sample_rate": 0,
                "channels": 0,
                "bit_depth": 0,
            }

        METADATA_CACHE[cache_key] = (
            stat.st_mtime,
            metadata,
        )

        return metadata

    except Exception:
        return {
            "title": path.stem,
            "artist": (
                path.parent.name
                if path.parent != DOWNLOAD_DIR
                else "Unknown Artist"
            ),
            "album": path.stem,
            "genre": "",
            "year": "",
            "track": "",
            "disc": "",
            "duration": 0,
            "bit_rate": 0,
            "sample_rate": 0,
            "channels": 0,
            "bit_depth": 0,
        }


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

async def build_library():

    files = await get_all_audio_files()

    songs = []
    artists = {}
    albums = {}
    genres = {}

    for path in files:

        try:
            stat = path.stat()
        except Exception:
            continue

        metadata = await read_metadata(path)

        song_id = make_song_id(path)
        artist_id = make_artist_id(
            metadata["artist"]
        )
        album_id = make_album_id(
            metadata["artist"],
            metadata["album"],
        )

        song = {
            "id": song_id,
            "title": metadata["title"],
            "artist": metadata["artist"],
            "artistId": artist_id,
            "album": metadata["album"],
            "albumId": album_id,
            "genre": metadata["genre"],
            "year": metadata["year"],
            "track": metadata["track"],
            "disc": metadata["disc"],
            "duration": safe_int(
                metadata["duration"],
                0,
            ),
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

        if artist_id not in artists:
            artists[artist_id] = {
                "id": artist_id,
                "name": metadata["artist"],
                "albumIds": set(),
                "songIds": [],
            }

        artists[artist_id]["albumIds"].add(
            album_id
        )

        artists[artist_id]["songIds"].append(
            song_id
        )

        if album_id not in albums:
            albums[album_id] = {
                "id": album_id,
                "name": metadata["album"],
                "artist": metadata["artist"],
                "artistId": artist_id,
                "year": metadata["year"],
                "genre": metadata["genre"],
                "songIds": [],
                "path": path,
            }

        albums[album_id]["songIds"].append(
            song_id
        )

        if metadata["genre"]:
            genres[metadata["genre"]] = (
                genres.get(
                    metadata["genre"],
                    0,
                )
                + 1
            )

    for artist in artists.values():
        artist["albumIds"] = list(
            artist["albumIds"]
        )

    return {
        "songs": songs,
        "artists": artists,
        "albums": albums,
        "genres": genres,
    }


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
    digest = hashlib.md5(
        str(path).encode("utf-8")
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

async def is_duplicate(title):

    files = await get_all_audio_files()

    wanted = normalize_duplicate_key(
        title
    )

    return any(
        normalize_duplicate_key(
            file.name
        ) == wanted
        for file in files
    )


def cleanup_task_files(task_id):

    for path in DOWNLOAD_DIR.glob(
        f"*{task_id}*"
    ):

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

        task_id = await TASK_QUEUE.get()

        try:

            task = TASKS.get(task_id)

            if not task:
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
                "yt-dlp",
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
                    stderr=asyncio.subprocess.PIPE,
                )
            )

            ACTIVE_PROCESSES[task_id] = process

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

                stderr = (
                    await process.stderr.read()
                )

                error_text = stderr.decode(
                    "utf-8",
                    errors="ignore",
                )

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

            task["status"] = "processing"
            task["percent"] = 96
            task["step"] = (
                "Cleaning metadata..."
            )
            task["last_updated"] = (
                time.time() * 1000
            )

            await notify_task_update(
                task,
                force_save=True,
            )

            clean_title = clean_filename(
                task.get(
                    "title",
                    "Unknown Track",
                )
            )

            clean_file = (
                DOWNLOAD_DIR
                / f"clean_{task_id}{extension}"
            )

            clean_command = [
                "ffmpeg",
                "-y",
                "-i",
                str(audio_file),
                "-map",
                "0",
                "-c",
                "copy",
                "-metadata",
                f"title={clean_title}",
                "-metadata",
                (
                    "artist="
                    f"{task.get('artist', 'Unknown Artist')}"
                ),
                "-metadata",
                f"album={clean_title}",
                str(clean_file),
            ]

            clean_process = (
                await asyncio.create_subprocess_exec(
                    *clean_command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            )

            _, clean_stderr = (
                await clean_process.communicate()
            )

            if (
                clean_process.returncode == 0
                and clean_file.exists()
            ):

                try:
                    audio_file.unlink()
                except Exception:
                    pass

                audio_file = clean_file

            elif not audio_file.exists():

                task["status"] = "error"
                task["step"] = "Processing failed"
                task["error"] = (
                    clean_stderr.decode(
                        "utf-8",
                        errors="ignore",
                    )[-1200:]
                    or "FFmpeg failed."
                )
                task["last_updated"] = (
                    time.time() * 1000
                )

                await notify_task_update(
                    task,
                    force_save=True,
                )

                continue

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

            if (
                final_path.exists()
                or await is_duplicate(
                    clean_title
                )
            ):

                final_name = (
                    f"{clean_title}_"
                    f"{task_id[:4]}"
                    f"{extension}"
                )

                final_path = (
                    final_dir / final_name
                )

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

            METADATA_CACHE.pop(
                str(final_path),
                None,
            )

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

    await asyncio.to_thread(
        init_db
    )

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

    for task in TASKS.values():

        if task.get(
            "status"
        ) == "queued":

            await TASK_QUEUE.put(
                task["id"]
            )


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
    save_settings(data)
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
        "yt-dlp",
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

    process = (
        await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    )

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

    settings = load_settings()

    max_results = max(
        5,
        min(
            safe_int(
                settings.get(
                    "max_results",
                    20,
                ),
                20,
            ),
            50,
        ),
    )

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

    process = (
        await asyncio.create_subprocess_exec(
            "yt-dlp",
            "-g",
            "-f",
            "ba/bestaudio/b",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    )

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
                "title",
                "Unknown Track",
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
    }

    TASKS[task_id] = task

    await notify_task_update(
        task,
        force_save=True,
    )

    await TASK_QUEUE.put(task_id)

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
            0
            if task.get("status") in {
                "queued",
                "downloading",
                "processing",
            }
            else 1,
            -safe_float(
                task.get(
                    "last_updated",
                    0,
                )
            ),
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

    return {
        "files": result,
        "total_size": format_size(total),
        "total_bytes": total,
    }


@app.get("/api/stats")
async def api_stats():

    library = await build_library()

    songs = library["songs"]

    artists = {
        song["artist"]
        for song in songs
    }

    albums = {
        (
            song["artist"],
            song["album"],
        )
        for song in songs
    }

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

    stats = await api_stats()

    library = await build_library()

    songs = sorted(
        library["songs"],
        key=lambda song:
            song["modified"],
        reverse=True,
    )

    settings = load_settings()

    username = urllib.parse.quote(
        settings.get(
            "subsonic_user",
            "admin",
        )
    )

    password = urllib.parse.quote(
        settings.get(
            "subsonic_password",
            "",
        )
    )

    recent = []

    for song in songs[:12]:

        recent.append(
            {
                "id": song["id"],
                "title": song["title"],
                "artist": song["artist"],
                "album": song["album"],
                "duration": song["duration"],
                "cover": (
                    "/rest/getCoverArt.view"
                    "?id="
                    + urllib.parse.quote(song["id"])
                    + "&u="
                    + username
                    + "&p="
                    + password
                    + "&v=1.16.1"
                    + "&c=XrobMusic"
                    + "&f=json"
                ),
            }
        )

    active = sum(
        1
        for task in TASKS.values()
        if task.get("status") in {
            "queued",
            "downloading",
            "processing",
        }
    )

    return {
        "stats": stats,
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

        METADATA_CACHE.pop(
            str(path),
            None,
        )

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

    with sqlite3.connect(DB_FILE) as conn:

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

    with sqlite3.connect(DB_FILE) as conn:

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
        "albumArtist": song["artist"],
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
            "id": song["artistId"],
            "name": song["artist"],
        }
    ]

    result["displayArtist"] = song["artist"]
    result["displayAlbumArtist"] = song["artist"]

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

        library = await build_library()

        count = (
            len(
                library.get(
                    "songs",
                    [],
                )
            )
            + len(
                library.get(
                    "albums",
                    {},
                )
            )
            + len(
                library.get(
                    "artists",
                    {},
                )
            )
        )

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

        library = await build_library()

        count = (
            len(
                library.get(
                    "songs",
                    [],
                )
            )
            + len(
                library.get(
                    "albums",
                    {},
                )
            )
            + len(
                library.get(
                    "artists",
                    {},
                )
            )
        )

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
):

    error = require_auth(request)

    if error:
        return error

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
            size,
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
):

    error = require_auth(request)

    if error:
        return error

    library = await build_library()

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
):

    error = require_auth(request)

    if error:
        return error

    library = await build_library()

    songs = list(
        library["songs"]
    )

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

    for name, count in sorted(
        library[
            "genres"
        ].items()
    ):

        genres.append(
            {
                "value": name,
                "songCount": count,
                "albumCount": 0,
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
):

    error = require_auth(request)

    if error:
        return error

    library = await build_library()

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

    with sqlite3.connect(DB_FILE) as conn:

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

def playlists_sync():

    with sqlite3.connect(
        DB_FILE
    ) as conn:

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

    with sqlite3.connect(
        DB_FILE
    ) as conn:

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

        ids = json.loads(
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

    ids = json.loads(
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
