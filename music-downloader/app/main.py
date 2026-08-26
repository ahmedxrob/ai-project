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
# APPLICATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="Xrob Music",
    version="1.9.7",
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
# PATHS
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

ADDON_OPTIONS_FILE = Path(
    "/data/options.json"
)

SUBSONIC_VERSION = "1.16.1"
SERVER_VERSION = "1.9.7"

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
# SETTINGS / ADD-ON OPTIONS
# ============================================================

def load_addon_options():
    if not ADDON_OPTIONS_FILE.exists():
        return {}

    try:
        with open(
            ADDON_OPTIONS_FILE,
            "r",
            encoding="utf-8",
        ) as file:
            data = json.load(file)

        return data if isinstance(data, dict) else {}

    except Exception as error:
        print(
            "Failed to read add-on options:",
            error,
        )
        return {}


def load_settings():
    settings = DEFAULT_SETTINGS.copy()

    if SETTINGS_FILE.exists():
        try:
            with open(
                SETTINGS_FILE,
                "r",
                encoding="utf-8",
            ) as file:
                saved = json.load(file)

            if isinstance(saved, dict):
                settings.update(saved)

        except Exception:
            pass

    addon = load_addon_options()

    if addon.get("subsonic_user"):
        settings["subsonic_user"] = str(
            addon["subsonic_user"]
        )

    if "subsonic_password" in addon:
        settings["subsonic_password"] = str(
            addon.get("subsonic_password") or ""
        )

    # Remove obsolete Navidrome settings from memory.
    for key in (
        "navidrome_url",
        "navidrome_user",
        "navidrome_token",
        "navidrome_salt",
    ):
        settings.pop(key, None)

    return settings


def save_settings(data):
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
    ) as file:
        json.dump(
            settings,
            file,
            indent=2,
        )

    return settings


def public_settings():
    settings = load_settings()

    safe = dict(settings)

    safe.pop(
        "subsonic_password",
        None,
    )

    return safe


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
            INSERT OR REPLACE INTO tasks
            (
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


async def db_save_task(
    task,
    force=False,
):
    task_id = task.get("id")
    now = time.time()

    if (
        force
        or now - LAST_SAVED_TIME.get(
            task_id,
            0,
        ) > 0.5
    ):
        LAST_SAVED_TIME[task_id] = now

        await asyncio.to_thread(
            db_save_task_sync,
            task,
        )


def db_load_tasks_sync():
    if not DB_FILE.exists():
        return {}

    result = {}

    with sqlite3.connect(DB_FILE) as conn:

        conn.row_factory = sqlite3.Row

        for row in conn.execute(
            "SELECT * FROM tasks"
        ):
            item = dict(row)
            result[item["id"]] = item

    return result


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
            self.connections.append(
                websocket
            )

    def disconnect(self, websocket):
        if websocket in self.connections:
            self.connections.remove(
                websocket
            )

    async def broadcast(self, message):
        for websocket in list(
            self.connections
        ):
            try:
                await websocket.send_json(
                    message
                )
            except Exception:
                self.disconnect(
                    websocket
                )


manager = ConnectionManager()


async def notify_task_update(
    task,
    force_save=False,
):
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
# GENERIC HELPERS
# ============================================================

def safe_int(
    value,
    default=0,
):
    try:
        return int(value)
    except Exception:
        return default


def safe_float(
    value,
    default=0,
):
    try:
        return float(value)
    except Exception:
        return default


def format_duration(seconds):
    seconds = safe_int(
        seconds,
        0,
    )

    return (
        f"{seconds // 60}:"
        f"{seconds % 60:02d}"
    )


def format_size(size):
    try:

        if size >= 1024 ** 3:
            return (
                f"{size / (1024 ** 3):.2f} GB"
            )

        return (
            f"{size / (1024 ** 2):.1f} MB"
        )

    except Exception:
        return "0 MB"


def clean_metadata_text(
    value,
    fallback="",
):
    value = str(
        value or ""
    ).strip()

    if not value:
        return fallback

    # Protect against accidental URLs being written
    # into artist/title/album metadata.
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

    return (
        value[:180]
        if value
        else "Unknown"
    )


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
# LIBRARY FILE SYSTEM
# ============================================================

def get_audio_files_sync():
    return [
        path
        for path in DOWNLOAD_DIR.rglob("*")
        if (
            path.is_file()
            and not path.name.startswith(".")
            and path.suffix.lower()
            in AUDIO_EXTENSIONS
        )
    ]


async def get_all_audio_files():
    return await asyncio.to_thread(
        get_audio_files_sync
    )


def resolve_file_sync(
    filename,
):
    base = DOWNLOAD_DIR.resolve()
    target = (
        DOWNLOAD_DIR / filename
    ).resolve()

    try:
        safe = target.is_relative_to(
            base
        )
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

    wanted = Path(
        filename
    ).name

    for path in DOWNLOAD_DIR.rglob("*"):

        if (
            path.is_file()
            and path.name == wanted
        ):
            return path.resolve()

    raise HTTPException(
        status_code=404,
        detail="File not found",
    )


async def resolve_file(
    filename,
):
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

        cached = METADATA_CACHE.get(
            cache_key
        )

        if (
            cached
            and cached[0] == stat.st_mtime
        ):
            return cached[1]

        command = [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_entries",
            "format=duration:format_tags",
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

            tags = fmt.get(
                "tags",
                {},
            )

            metadata = {
                "title":
                    clean_metadata_text(
                        tags.get("title"),
                        path.stem,
                    ),

                "artist":
                    clean_metadata_text(
                        tags.get("artist")
                        or tags.get(
                            "album_artist"
                        ),
                        (
                            path.parent.name
                            if path.parent != DOWNLOAD_DIR
                            else "Unknown Artist"
                        ),
                    ),

                "album":
                    clean_metadata_text(
                        tags.get("album"),
                        path.stem,
                    ),

                "genre":
                    clean_metadata_text(
                        tags.get("genre"),
                        "",
                    ),

                "year":
                    clean_metadata_text(
                        tags.get("date")
                        or tags.get("year"),
                        "",
                    ),

                "track":
                    clean_metadata_text(
                        tags.get("track"),
                        "",
                    ),

                "disc":
                    clean_metadata_text(
                        tags.get("disc"),
                        "",
                    ),

                "duration":
                    safe_float(
                        fmt.get("duration"),
                        0,
                    ),
            }

        if not metadata:

            metadata = {
                "title":
                    path.stem,

                "artist":
                    (
                        path.parent.name
                        if path.parent != DOWNLOAD_DIR
                        else "Unknown Artist"
                    ),

                "album":
                    path.stem,

                "genre":
                    "",

                "year":
                    "",

                "track":
                    "",

                "disc":
                    "",

                "duration":
                    0,
            }

        METADATA_CACHE[cache_key] = (
            stat.st_mtime,
            metadata,
        )

        return metadata

    except Exception:

        return {
            "title":
                path.stem,

            "artist":
                (
                    path.parent.name
                    if path.parent != DOWNLOAD_DIR
                    else "Unknown Artist"
                ),

            "album":
                path.stem,

            "genre":
                "",

            "year":
                "",

            "track":
                "",

            "disc":
                "",

            "duration":
                0,
        }


async def read_metadata(
    path,
):
    return await asyncio.to_thread(
        read_metadata_sync,
        path,
    )


# ============================================================
# LIBRARY IDS
# ============================================================

def make_song_id(path):

    relative = str(
        path.relative_to(
            DOWNLOAD_DIR
        )
    )

    digest = hashlib.sha1(
        relative.encode(
            "utf-8"
        )
    ).hexdigest()[:20]

    return f"song-{digest}"


def make_artist_id(name):

    clean_name = clean_metadata_text(
        name,
        "Unknown Artist",
    )

    digest = hashlib.sha1(
        clean_name.encode(
            "utf-8"
        )
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
        raw.encode(
            "utf-8"
        )
    ).hexdigest()[:20]

    return f"album-{digest}"


# ============================================================
# LIBRARY INDEX
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

        metadata = await read_metadata(
            path
        )

        song_id = make_song_id(
            path
        )

        artist_id = make_artist_id(
            metadata["artist"]
        )

        album_id = make_album_id(
            metadata["artist"],
            metadata["album"],
        )

        song = {
            "id":
                song_id,

            "title":
                metadata["title"],

            "artist":
                metadata["artist"],

            "artistId":
                artist_id,

            "album":
                metadata["album"],

            "albumId":
                album_id,

            "genre":
                metadata["genre"],

            "year":
                metadata["year"],

            "track":
                metadata["track"],

            "disc":
                metadata["disc"],

            "duration":
                safe_int(
                    metadata["duration"],
                    0,
                ),

            "path":
                path,

            "suffix":
                path.suffix.lower(),

            "size":
                stat.st_size,

            "created":
                stat.st_ctime,

            "modified":
                stat.st_mtime,
        }

        songs.append(
            song
        )

        if artist_id not in artists:

            artists[artist_id] = {
                "id":
                    artist_id,

                "name":
                    metadata["artist"],

                "albumIds":
                    set(),

                "songIds":
                    [],
            }

        artists[
            artist_id
        ][
            "albumIds"
        ].add(
            album_id
        )

        artists[
            artist_id
        ][
            "songIds"
        ].append(
            song_id
        )

        if album_id not in albums:

            albums[album_id] = {
                "id":
                    album_id,

                "name":
                    metadata["album"],

                "artist":
                    metadata["artist"],

                "artistId":
                    artist_id,

                "year":
                    metadata["year"],

                "genre":
                    metadata["genre"],

                "songIds":
                    [],

                "path":
                    path,
            }

        albums[
            album_id
        ][
            "songIds"
        ].append(
            song_id
        )

        if metadata["genre"]:

            genres[
                metadata["genre"]
            ] = (
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
        "songs":
            songs,
        "artists":
            artists,
        "albums":
            albums,
        "genres":
            genres,
    }


async def find_song(
    song_id,
):
    library = await build_library()

    for song in library[
        "songs"
    ]:

        if song["id"] == song_id:
            return song

    return None


async def find_artist(
    artist_id,
):
    library = await build_library()

    return library[
        "artists"
    ].get(
        artist_id
    )


async def find_album(
    album_id,
):
    library = await build_library()

    return library[
        "albums"
    ].get(
        album_id
    )


# ============================================================
# COVER CACHE
# ============================================================

def cover_cache_path(
    path,
):
    digest = hashlib.md5(
        str(path).encode(
            "utf-8"
        )
    ).hexdigest()

    return (
        COVER_CACHE_DIR
        / f"{digest}.jpg"
    )


async def ensure_cover(
    path,
):

    cover = cover_cache_path(
        path
    )

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

    return (
        cover
        if cover.exists()
        else None
    )


# ============================================================
# DUPLICATE CHECK
# ============================================================

async def is_duplicate(
    title,
):

    files = await get_all_audio_files()

    target = normalize_duplicate_key(
        title
    )

    return any(
        normalize_duplicate_key(
            file.name
        ) == target
        for file in files
    )


def cleanup_task_files(
    task_id,
):

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

            task = TASKS.get(
                task_id
            )

            if not task:
                continue

            if task.get(
                "cancel_requested"
            ):

                task["status"] = (
                    "cancelled"
                )

                task["step"] = (
                    "Cancelled"
                )

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

            task["status"] = (
                "downloading"
            )

            task["step"] = (
                "Downloading stream..."
            )

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

            ACTIVE_PROCESSES[
                task_id
            ] = process

            progress_re = re.compile(
                r"\[download\]\s+~?\s*(\d+(?:\.\d+)?)%"
            )

            speed_re = re.compile(
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

                progress_match = (
                    progress_re.search(
                        text
                    )
                )

                if progress_match:

                    task["percent"] = float(
                        progress_match.group(
                            1
                        )
                    )

                    speed_match = (
                        speed_re.search(
                            text
                        )
                    )

                    if speed_match:

                        task["speed"] = (
                            speed_match.group(
                                1
                            ).replace(
                                "~",
                                "",
                            )
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

                    task["status"] = (
                        "processing"
                    )

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

            if task.get(
                "cancel_requested"
            ):

                await asyncio.to_thread(
                    cleanup_task_files,
                    task_id,
                )

                task["status"] = (
                    "cancelled"
                )

                task["step"] = (
                    "Cancelled"
                )

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

                task["status"] = (
                    "error"
                )

                task["step"] = (
                    "Download failed"
                )

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

                task["status"] = (
                    "error"
                )

                task["step"] = (
                    "Download failed"
                )

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

            audio_file = (
                possible_files[0]
            )

            extension = (
                audio_file.suffix
                or f".{fmt}"
            )

            task["status"] = (
                "processing"
            )

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
                / f"clean_{task_id}"
                f"{extension}"
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

                task["status"] = (
                    "error"
                )

                task["step"] = (
                    "Processing failed"
                )

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
                    DOWNLOAD_DIR
                    / artist
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
                final_dir
                / final_name
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
                    final_dir
                    / final_name
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

            task["status"] = (
                "completed"
            )

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

            task = TASKS.get(
                task_id
            )

            ACTIVE_PROCESSES.pop(
                task_id,
                None,
            )

            await asyncio.to_thread(
                cleanup_task_files,
                task_id,
            )

            if task:

                task["status"] = (
                    "error"
                )

                task["step"] = (
                    "Unexpected error"
                )

                task["error"] = str(
                    error
                )

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

            task["status"] = (
                "queued"
            )

            task["step"] = (
                "Recovered after restart"
            )

            task["cancel_requested"] = (
                False
            )

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
# WEBSOCKET
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
# WEB
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

    end = (
        page
        * max_results
    )

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

    stdout, stderr = (
        await process.communicate()
    )

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

        video_id = item.get(
            "id"
        )

        if not video_id:
            continue

        duration = safe_int(
            item.get(
                "duration",
                0,
            ),
            0,
        )

        results.append(
            {
                "id":
                    video_id,

                "title":
                    item.get(
                        "title",
                        "Unknown Track",
                    ),

                "channel":
                    (
                        item.get("channel")
                        or item.get("uploader")
                        or "Unknown Artist"
                    ),

                "duration":
                    duration,

                "duration_text":
                    format_duration(
                        duration
                    ),

                "thumbnail":
                    (
                        item.get(
                            "thumbnail"
                        )
                        or
                        (
                            "https://i.ytimg.com/"
                            f"vi/{video_id}/"
                            "hqdefault.jpg"
                        )
                    ),

                "url":
                    (
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
            max(
                1,
                page,
            ),
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

    stdout, stderr = (
        await process.communicate()
    )

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
        stdout
        .decode(
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
            "Cache-Control":
                "no-cache",
            "Access-Control-Allow-Origin":
                "*",
        },
    )


# ============================================================
# DOWNLOAD API
# ============================================================

@app.post("/api/download")
async def api_download(
    payload: dict = Body(...),
):

    url = payload.get(
        "url"
    )

    if not url:
        raise HTTPException(
            status_code=400,
            detail="Missing URL",
        )

    for task in TASKS.values():

        if (
            task.get(
                "url"
            ) == url
            and task.get(
                "status"
            ) in {
                "queued",
                "downloading",
                "processing",
            }
        ):

            return {
                "status":
                    "already_queued",
                "task_id":
                    task["id"],
            }

    task_id = uuid.uuid4().hex[:12]

    task = {
        "id":
            task_id,

        "title":
            str(
                payload.get(
                    "title",
                    "Unknown Track",
                )
            ),

        "artist":
            str(
                payload.get(
                    "artist",
                    "Unknown Artist",
                )
            ),

        "album":
            str(
                payload.get(
                    "title",
                    "Unknown Track",
                )
            ),

        "url":
            url,

        "elementId":
            str(
                payload.get(
                    "elementId",
                    "",
                )
            ),

        "status":
            "queued",

        "percent":
            0,

        "speed":
            "",

        "step":
            "Queued...",

        "error":
            "",

        "last_updated":
            time.time() * 1000,

        "final_name":
            "",

        "cancel_requested":
            False,
    }

    TASKS[task_id] = task

    await notify_task_update(
        task,
        force_save=True,
    )

    await TASK_QUEUE.put(
        task_id
    )

    return {
        "status":
            "ok",
        "task_id":
            task_id,
    }


@app.get("/api/tasks")
async def api_tasks():

    tasks = list(
        TASKS.values()
    )

    tasks.sort(
        key=lambda task: (
            0
            if task.get(
                "status"
            ) in {
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

    task = TASKS.get(
        task_id
    )

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

    task["status"] = (
        "cancelled"
    )

    task["step"] = (
        "Cancelled"
    )

    task["last_updated"] = (
        time.time() * 1000
    )

    await notify_task_update(
        task,
        force_save=True,
    )

    return {
        "status":
            "cancelled",
        "task_id":
            task_id,
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
        if task.get(
            "status"
        ) in removable
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

        completed = (
            task_id
        )

    await asyncio.to_thread(
        db_clear_finished_sync
    )

    await manager.broadcast(
        {
            "type":
                "task_update",
            "action":
                "cleared",
            "count":
                len(ids),
        }
    )

    return {
        "status":
            "cleared",
        "count":
            len(ids),
    }


@app.delete(
    "/api/tasks/{task_id}"
)
async def api_delete_task(
    task_id: str,
):

    task = TASKS.get(
        task_id
    )

    if not task:
        raise HTTPException(
            status_code=404,
            detail="Task not found",
        )

    if task.get(
        "status"
    ) in {
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
        "status":
            "deleted",
        "task_id":
            task_id,
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
                "name":
                    str(
                        path.relative_to(
                            DOWNLOAD_DIR
                        )
                    ),

                "size":
                    format_size(
                        size
                    ),

                "bytes":
                    size,
            }
        )

    result.sort(
        key=lambda item:
            item["name"].lower()
    )

    return {
        "files":
            result,

        "total_size":
            format_size(
                total
            ),

        "total_bytes":
            total,
    }


@app.get("/api/stats")
async def api_stats():

    library = await build_library()

    songs = library[
        "songs"
    ]

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
        "tracks":
            len(songs),

        "artists":
            len(artists),

        "albums":
            len(albums),

        "total_bytes":
            total,

        "folder_size":
            format_size(
                total
            ),
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

    recent = []

    for song in songs[:12]:

        recent.append(
            {
                "id":
                    song["id"],

                "title":
                    song["title"],

                "artist":
                    song["artist"],

                "album":
                    song["album"],

                "duration":
                    song["duration"],

                "cover":
                    (
                        "/rest/getCoverArt.view"
                        "?id="
                        + urllib.parse.quote(
                            song["id"]
                        )
                    ),
            }
        )

    active = sum(
        1
        for task in TASKS.values()
        if task.get(
            "status"
        ) in {
            "queued",
            "downloading",
            "processing",
        }
    )

    return {
        "stats":
            stats,

        "active_downloads":
            active,

        "recently_added":
            recent,
    }


@app.get(
    "/api/library/cover/{filename:path}"
)
async def api_library_cover(
    filename: str,
):

    path = await resolve_file(
        filename
    )

    cover = await ensure_cover(
        path
    )

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

    path = await resolve_file(
        filename
    )

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

    path = await resolve_file(
        filename
    )

    try:

        cover = cover_cache_path(
            path
        )

        path.unlink()

        if cover.exists():
            cover.unlink()

        METADATA_CACHE.pop(
            str(path),
            None,
        )

        return {
            "status":
                "deleted",
            "filename":
                filename,
        }

    except Exception as error:

        raise HTTPException(
            status_code=500,
            detail=str(error),
        )


# ============================================================
# SUBSONIC AUTH
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

    supplied_user = (
        request.query_params.get(
            "u",
            "",
        )
    )

    supplied_password = (
        request.query_params.get(
            "p",
            "",
        )
    )

    token = (
        request.query_params.get(
            "t",
            "",
        )
    )

    salt = (
        request.query_params.get(
            "s",
            "",
        )
    )

    if not username or not password:
        return False

    if supplied_user != username:
        return False

    # Token authentication.
    if token and salt:

        expected = hashlib.md5(
            (
                password + salt
            ).encode(
                "utf-8"
            )
        ).hexdigest()

        if (
            token.lower()
            == expected.lower()
        ):
            return True

    # Plain password authentication.
    if supplied_password == password:
        return True

    # Some older clients send md5(password).
    if (
        supplied_password
        and len(supplied_password) == 32
    ):

        expected = hashlib.md5(
            password.encode(
                "utf-8"
            )
        ).hexdigest()

        if (
            supplied_password.lower()
            == expected.lower()
        ):
            return True

    return False


def require_auth(
    request: Request,
):

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
# SUBSONIC RESPONSE SERIALIZATION
# ============================================================

def scalar(value):

    if isinstance(
        value,
        bool,
    ):
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

                    child.text = scalar(
                        item
                    )

        else:

            parent.set(
                key,
                scalar(
                    value
                ),
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

                child.text = scalar(
                    item
                )

    else:

        child = ET.SubElement(
            parent,
            key,
        )

        child.text = scalar(
            value
        )


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
            {
                "subsonic-response":
                    payload
            }
        )

    attrs = {}

    for key in (
        "status",
        "version",
        "serverVersion",
        "openSubsonic",
        "type",
    ):

        if key in payload:

            attrs[key] = scalar(
                payload[key]
            )

    root = ET.Element(
        "subsonic-response",
        attrs,
    )

    for key, value in payload.items():

        if key in {
            "status",
            "version",
            "serverVersion",
            "openSubsonic",
            "type",
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
            "status":
                "failed",

            "version":
                SUBSONIC_VERSION,

            "serverVersion":
                SERVER_VERSION,

            "openSubsonic":
                True,

            "error": {
                "code":
                    str(code),

                "message":
                    message,
            },
        },
        request,
    )


# ============================================================
# STAR / FAVORITE HELPERS
# ============================================================

def is_starred_sync(
    item_id,
):

    with sqlite3.connect(DB_FILE) as conn:

        row = conn.execute(
            """
            SELECT 1
            FROM stars
            WHERE item_id = ?
            """,
            (item_id,),
        ).fetchone()

    return bool(row)


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
# SUBSONIC OBJECT CONVERSION
# ============================================================

def song_to_subsonic(
    song,
):

    return {
        "id":
            song["id"],

        "parent":
            song["albumId"],

        "isDir":
            False,

        "title":
            song["title"],

        "album":
            song["album"],

        "artist":
            song["artist"],

        "artistId":
            song["artistId"],

        "track":
            safe_int(
                song["track"],
                0,
            ),

        "year":
            safe_int(
                song["year"],
                0,
            ),

        "genre":
            song["genre"],

        "coverArt":
            song["id"],

        "size":
            song["size"],

        "contentType":
            MEDIA_TYPES.get(
                song["suffix"],
                "audio/mpeg",
            ),

        "suffix":
            song["suffix"].lstrip(
                "."
            ),

        "duration":
            song["duration"],

        "path":
            str(
                song["path"].relative_to(
                    DOWNLOAD_DIR
                )
            ),

        "type":
            "music",

        "isVideo":
            False,

        "starred":
            is_starred_sync(
                song["id"]
            ),
    }


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

    return {
        "id":
            album["id"],

        "parent":
            album["artistId"],

        "isDir":
            True,

        "title":
            album["name"],

        "name":
            album["name"],

        "album":
            album["name"],

        "artist":
            album["artist"],

        "artistId":
            album["artistId"],

        "year":
            safe_int(
                album.get(
                    "year"
                ),
                0,
            ),

        "genre":
            genre,

        "coverArt":
            album["id"],

        "songCount":
            len(songs),

        "duration":
            duration,

        "playCount":
            0,

        "isVideo":
            False,
    }


def artist_to_subsonic(
    artist,
):

    return {
        "id":
            artist["id"],

        "name":
            artist["name"],

        "albumCount":
            len(
                artist["albumIds"]
            ),

        "coverArt":
            artist["id"],
    }


# ============================================================
# COVER RESOLUTION FOR SUBSONIC
# ============================================================

async def resolve_cover_id(
    item_id,
):

    song = await find_song(
        item_id
    )

    if song:
        return song["path"]

    album = await find_album(
        item_id
    )

    if album:
        return album["path"]

    artist = await find_artist(
        item_id
    )

    if artist:

        library = await build_library()

        for album_id in artist[
            "albumIds"
        ]:

            album = library[
                "albums"
            ].get(
                album_id
            )

            if album:
                return album["path"]

    return None


# ============================================================
# SUBSONIC CORE
# ============================================================

@app.get("/rest/ping.view")
@app.get("/rest/ping")
async def rest_ping(
    request: Request,
):

    return make_subsonic_response(
        {
            "status":
                "ok",
            "version":
                SUBSONIC_VERSION,
            "serverVersion":
                SERVER_VERSION,
            "openSubsonic":
                True,
            "type":
                "Xrob Music",
        },
        request,
    )


@app.get(
    "/rest/getOpenSubsonicExtensions.view"
)
async def rest_extensions(
    request: Request,
):

    return make_subsonic_response(
        {
            "status":
                "ok",
            "version":
                SUBSONIC_VERSION,
            "serverVersion":
                SERVER_VERSION,
            "openSubsonic":
                True,

            "openSubsonicExtensions": {
                "extension": [
                    {
                        "name":
                            "transcodeOffset",
                        "versions":
                            "1",
                    },
                    {
                        "name":
                            "songLyrics",
                        "versions":
                            "1",
                    },
                ],
            },
        },
        request,
    )


@app.get(
    "/rest/getLicense.view"
)
async def rest_license(
    request: Request,
):

    error = require_auth(
        request
    )

    if error:
        return error

    return make_subsonic_response(
        {
            "status":
                "ok",
            "version":
                SUBSONIC_VERSION,
            "serverVersion":
                SERVER_VERSION,
            "openSubsonic":
                True,

            "license": {
                "valid":
                    True,
            },
        },
        request,
    )


@app.get(
    "/rest/getMusicFolders.view"
)
async def rest_music_folders(
    request: Request,
):

    error = require_auth(
        request
    )

    if error:
        return error

    return make_subsonic_response(
        {
            "status":
                "ok",
            "version":
                SUBSONIC_VERSION,
            "serverVersion":
                SERVER_VERSION,
            "openSubsonic":
                True,

            "musicFolders": {
                "musicFolder": [
                    {
                        "id":
                            "1",
                        "name":
                            "Music",
                    }
                ],
            },
        },
        request,
    )


# ============================================================
# ARTISTS
# ============================================================

@app.get("/rest/getArtists.view")
async def rest_artists(
    request: Request,
):

    error = require_auth(
        request
    )

    if error:
        return error

    library = await build_library()

    grouped = {}

    for artist in library[
        "artists"
    ].values():

        name = artist[
            "name"
        ]

        letter = (
            name[:1].upper()
            if name
            else "#"
        )

        grouped.setdefault(
            letter,
            [],
        ).append(
            artist_to_subsonic(
                artist
            )
        )

    indexes = []

    for letter in sorted(
        grouped
    ):

        grouped[letter].sort(
            key=lambda item:
                item["name"].lower()
        )

        indexes.append(
            {
                "name":
                    letter,

                "artist":
                    grouped[letter],
            }
        )

    return make_subsonic_response(
        {
            "status":
                "ok",

            "version":
                SUBSONIC_VERSION,

            "serverVersion":
                SERVER_VERSION,

            "openSubsonic":
                True,

            "artists": {
                "ignoredArticles":
                    "",

                "index":
                    indexes,
            },
        },
        request,
    )


@app.get(
    "/rest/getIndexes.view"
)
async def rest_indexes(
    request: Request,
):

    return await rest_artists(
        request
    )


@app.get(
    "/rest/getArtist.view"
)
async def rest_artist(
    request: Request,
    id: str = Query(...),
):

    error = require_auth(
        request
    )

    if error:
        return error

    artist = await find_artist(
        id
    )

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
            for song
            in library["songs"]
            if song["id"]
            in album["songIds"]
        ]

        albums.append(
            album_to_subsonic(
                album,
                songs,
            )
        )

    return make_subsonic_response(
        {
            "status":
                "ok",

            "version":
                SUBSONIC_VERSION,

            "serverVersion":
                SERVER_VERSION,

            "openSubsonic":
                True,

            "artist": {
                **artist_to_subsonic(
                    artist
                ),

                "album":
                    albums,
            },
        },
        request,
    )


# ============================================================
# ALBUMS
# ============================================================

@app.get(
    "/rest/getAlbum.view"
)
async def rest_album(
    request: Request,
    id: str = Query(...),
):

    error = require_auth(
        request
    )

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
        for song
        in library["songs"]
        if song["id"]
        in album["songIds"]
    ]

    return make_subsonic_response(
        {
            "status":
                "ok",

            "version":
                SUBSONIC_VERSION,

            "serverVersion":
                SERVER_VERSION,

            "openSubsonic":
                True,

            "album": {
                **album_to_subsonic(
                    album,
                    songs,
                ),

                "song": [
                    song_to_subsonic(
                        song
                    )
                    for song
                    in songs
                ],
            },
        },
        request,
    )


@app.get(
    "/rest/getAlbumList2.view"
)
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
):

    error = require_auth(
        request
    )

    if error:
        return error

    library = await build_library()

    albums = list(
        library[
            "albums"
        ].values()
    )

    album_data = []

    for album in albums:

        songs = [
            song
            for song
            in library["songs"]
            if song["id"]
            in album["songIds"]
        ]

        obj = {
            **album_to_subsonic(
                album,
                songs,
            ),

            "_songs":
                songs,

            "_latest":
                max(
                    (
                        song["modified"]
                        for song
                        in songs
                    ),
                    default=0,
                ),

            "_playCount":
                0,
        }

        album_data.append(
            obj
        )

    if fromYear is not None:

        album_data = [
            album
            for album
            in album_data
            if (
                safe_int(
                    album.get(
                        "year"
                    ),
                    0,
                )
                >= fromYear
            )
        ]

    if toYear is not None:

        album_data = [
            album
            for album
            in album_data
            if (
                safe_int(
                    album.get(
                        "year"
                    ),
                    0,
                )
                <= toYear
            )
        ]

    if genre:

        genre_lower = genre.lower()

        album_data = [
            album
            for album
            in album_data
            if album.get(
                "genre",
                "",
            ).lower()
            == genre_lower
        ]

    normalized_type = (
        type or
        "alphabeticalByName"
    )

    if normalized_type == "random":

        random.shuffle(
            album_data
        )

    elif normalized_type in {
        "newest",
        "recent",
    }:

        album_data.sort(
            key=lambda item:
                item["_latest"],
            reverse=True,
        )

    elif normalized_type == "frequent":

        album_data.sort(
            key=lambda item:
                item["_playCount"],
            reverse=True,
        )

    elif normalized_type == "alphabeticalByArtist":

        album_data.sort(
            key=lambda item: (
                item["artist"].lower(),
                item["name"].lower(),
            )
        )

    else:

        album_data.sort(
            key=lambda item:
                item["name"].lower()
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

    output = []

    for album in album_data[
        start:end
    ]:

        album.pop(
            "_songs",
            None,
        )

        album.pop(
            "_latest",
            None,
        )

        album.pop(
            "_playCount",
            None,
        )

        output.append(
            album
        )

    return make_subsonic_response(
        {
            "status":
                "ok",

            "version":
                SUBSONIC_VERSION,

            "serverVersion":
                SERVER_VERSION,

            "openSubsonic":
                True,

            "albumList2": {
                "album":
                    output,
            },
        },
        request,
    )


@app.get(
    "/rest/getAlbumList.view"
)
async def rest_album_list(
    request: Request,
    type: str = Query(
        "alphabeticalByName"
    ),
    size: int = Query(50),
    offset: int = Query(0),
):

    return await rest_album_list2(
        request=request,
        type=type,
        size=size,
        offset=offset,
    )


# ============================================================
# DIRECTORY
# ============================================================

@app.get(
    "/rest/getMusicDirectory.view"
)
async def rest_music_directory(
    request: Request,
    id: str = Query(...),
):

    error = require_auth(
        request
    )

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
                    "id":
                        artist["id"],

                    "parent":
                        "1",

                    "isDir":
                        True,

                    "title":
                        artist["name"],

                    "name":
                        artist["name"],

                    "type":
                        "artist",

                    "coverArt":
                        artist["id"],
                }
            )

    elif id.startswith(
        "artist-"
    ):

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
                    for song
                    in library["songs"]
                    if song["id"]
                    in album["songIds"]
                ]

                item = album_to_subsonic(
                    album,
                    songs,
                )

                item["type"] = (
                    "album"
                )

                children.append(
                    item
                )

    elif id.startswith(
        "album-"
    ):

        album = library[
            "albums"
        ].get(
            id
        )

        if album:

            songs = [
                song
                for song
                in library["songs"]
                if song["id"]
                in album["songIds"]
            ]

            for song in songs:

                children.append(
                    song_to_subsonic(
                        song
                    )
                )

    return make_subsonic_response(
        {
            "status":
                "ok",

            "version":
                SUBSONIC_VERSION,

            "serverVersion":
                SERVER_VERSION,

            "openSubsonic":
                True,

            "directory": {
                "id":
                    id,

                "name":
                    "Music",

                "child":
                    children,
            },
        },
        request,
    )


# ============================================================
# SONG
# ============================================================

@app.get(
    "/rest/getSong.view"
)
async def rest_song(
    request: Request,
    id: str = Query(...),
):

    error = require_auth(
        request
    )

    if error:
        return error

    song = await find_song(
        id
    )

    if not song:

        return subsonic_error(
            request,
            70,
            "Song not found.",
        )

    return make_subsonic_response(
        {
            "status":
                "ok",

            "version":
                SUBSONIC_VERSION,

            "serverVersion":
                SERVER_VERSION,

            "openSubsonic":
                True,

            "song":
                song_to_subsonic(
                    song
                ),
        },
        request,
    )


# ============================================================
# SEARCH2
# ============================================================

@app.get(
    "/rest/search2.view"
)
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

    return await _rest_search_impl(
        request=request,
        query=query,
        artist_count=artistCount,
        artist_offset=artistOffset,
        album_count=albumCount,
        album_offset=albumOffset,
        song_count=songCount,
        song_offset=songOffset,
        version="searchResult2",
    )


# ============================================================
# SEARCH3
# ============================================================

@app.get(
    "/rest/search3.view"
)
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

    return await _rest_search_impl(
        request=request,
        query=query,
        artist_count=artistCount,
        artist_offset=artistOffset,
        album_count=albumCount,
        album_offset=albumOffset,
        song_count=songCount,
        song_offset=songOffset,
        version="searchResult3",
    )


async def _rest_search_impl(
    request,
    query,
    artist_count,
    artist_offset,
    album_count,
    album_offset,
    song_count,
    song_offset,
    version,
):

    error = require_auth(
        request
    )

    if error:
        return error

    library = await build_library()

    q = query.lower().strip()

    matching_artists = [
        artist
        for artist
        in library["artists"].values()
        if q in artist[
            "name"
        ].lower()
    ]

    matching_albums = []

    for album in library[
        "albums"
    ].values():

        if (
            q in album[
                "name"
            ].lower()
            or q in album[
                "artist"
            ].lower()
        ):

            matching_albums.append(
                album
            )

    matching_songs = []

    for song in library[
        "songs"
    ]:

        haystack = (
            song["title"]
            + " "
            + song["artist"]
            + " "
            + song["album"]
        ).lower()

        if q in haystack:
            matching_songs.append(
                song
            )

    artist_objects = [
        artist_to_subsonic(
            artist
        )
        for artist
        in matching_artists
    ]

    album_objects = []

    for album in matching_albums:

        songs = [
            song
            for song
            in library["songs"]
            if song["id"]
            in album["songIds"]
        ]

        album_objects.append(
            album_to_subsonic(
                album,
                songs,
            )
        )

    song_objects = [
        song_to_subsonic(
            song
        )
        for song
        in matching_songs
    ]

    response_data = {
        "status":
            "ok",

        "version":
            SUBSONIC_VERSION,

        "serverVersion":
            SERVER_VERSION,

        "openSubsonic":
            True,
    }

    if version == "searchResult2":

        response_data[
            "searchResult2"
        ] = {
            "artist":
                artist_objects[
                    artist_offset:
                    artist_offset
                    + max(
                        0,
                        artist_count,
                    )
                ],

            "album":
                album_objects[
                    album_offset:
                    album_offset
                    + max(
                        0,
                        album_count,
                    )
                ],

            "song":
                song_objects[
                    song_offset:
                    song_offset
                    + max(
                        0,
                        song_count,
                    )
                ],
        }

    else:

        response_data[
            "searchResult3"
        ] = {
            "artist":
                artist_objects[
                    artist_offset:
                    artist_offset
                    + max(
                        0,
                        artist_count,
                    )
                ],

            "album":
                album_objects[
                    album_offset:
                    album_offset
                    + max(
                        0,
                        album_count,
                    )
                ],

            "song":
                song_objects[
                    song_offset:
                    song_offset
                    + max(
                        0,
                        song_count,
                    )
                ],
        }

    return make_subsonic_response(
        response_data,
        request,
    )


# ============================================================
# RANDOM / GENRES
# ============================================================

@app.get(
    "/rest/getRandomSongs.view"
)
async def rest_random_songs(
    request: Request,
    size: int = Query(10),
):

    error = require_auth(
        request
    )

    if error:
        return error

    library = await build_library()

    songs = list(
        library[
            "songs"
        ]
    )

    random.shuffle(
        songs
    )

    return make_subsonic_response(
        {
            "status":
                "ok",

            "version":
                SUBSONIC_VERSION,

            "serverVersion":
                SERVER_VERSION,

            "openSubsonic":
                True,

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


@app.get(
    "/rest/getGenres.view"
)
async def rest_genres(
    request: Request,
):

    error = require_auth(
        request
    )

    if error:
        return error

    library = await build_library()

    result = []

    for genre, count in sorted(
        library[
            "genres"
        ].items()
    ):

        result.append(
            {
                "value":
                    genre,

                "songCount":
                    count,

                "albumCount":
                    0,
            }
        )

    return make_subsonic_response(
        {
            "status":
                "ok",

            "version":
                SUBSONIC_VERSION,

            "serverVersion":
                SERVER_VERSION,

            "openSubsonic":
                True,

            "genres": {
                "genre":
                    result,
            },
        },
        request,
    )


@app.get(
    "/rest/getSongsByGenre.view"
)
async def rest_songs_by_genre(
    request: Request,
    genre: str = Query(""),
    count: int = Query(50),
    offset: int = Query(0),
):

    error = require_auth(
        request
    )

    if error:
        return error

    library = await build_library()

    songs = [
        song
        for song
        in library[
            "songs"
        ]
        if song[
            "genre"
        ].lower()
        == genre.lower()
    ]

    return make_subsonic_response(
        {
            "status":
                "ok",

            "version":
                SUBSONIC_VERSION,

            "serverVersion":
                SERVER_VERSION,

            "openSubsonic":
                True,

            "songsByGenre": {
                "song": [
                    song_to_subsonic(
                        song
                    )
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
# STREAM / DOWNLOAD
# ============================================================

@app.get(
    "/rest/stream.view"
)
async def rest_stream(
    request: Request,
    id: str = Query(...),
):

    error = require_auth(
        request
    )

    if error:
        return error

    song = await find_song(
        id
    )

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
        filename=song["path"].name,
        headers={
            "Accept-Ranges":
                "bytes",
            "Access-Control-Allow-Origin":
                "*",
        },
    )


@app.get(
    "/rest/download.view"
)
async def rest_download(
    request: Request,
    id: str = Query(...),
):

    error = require_auth(
        request
    )

    if error:
        return error

    song = await find_song(
        id
    )

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

@app.get(
    "/rest/getCoverArt.view"
)
async def rest_cover_art(
    request: Request,
    id: str = Query(...),
    size: int = Query(0),
):

    error = require_auth(
        request
    )

    if error:
        return error

    path = await resolve_cover_id(
        id
    )

    if not path:
        return Response(
            status_code=404
        )

    cover = await ensure_cover(
        path
    )

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
# STARRED
# ============================================================

@app.get(
    "/rest/star.view"
)
async def rest_star(
    request: Request,
    id: str = Query(...),
):

    error = require_auth(
        request
    )

    if error:
        return error

    await asyncio.to_thread(
        set_star_sync,
        id,
        True,
    )

    return make_subsonic_response(
        {
            "status":
                "ok",

            "version":
                SUBSONIC_VERSION,

            "serverVersion":
                SERVER_VERSION,

            "openSubsonic":
                True,
        },
        request,
    )


@app.get(
    "/rest/unstar.view"
)
async def rest_unstar(
    request: Request,
    id: str = Query(...),
):

    error = require_auth(
        request
    )

    if error:
        return error

    await asyncio.to_thread(
        set_star_sync,
        id,
        False,
    )

    return make_subsonic_response(
        {
            "status":
                "ok",

            "version":
                SUBSONIC_VERSION,

            "serverVersion":
                SERVER_VERSION,

            "openSubsonic":
                True,
        },
        request,
    )


@app.get(
    "/rest/getStarred2.view"
)
async def rest_starred2(
    request: Request,
):

    error = require_auth(
        request
    )

    if error:
        return error

    with sqlite3.connect(
        DB_FILE
    ) as conn:

        rows = conn.execute(
            "SELECT item_id FROM stars"
        ).fetchall()

    starred_ids = {
        row[0]
        for row in rows
    }

    library = await build_library()

    songs = [
        song_to_subsonic(
            song
        )
        for song
        in library[
            "songs"
        ]
        if song["id"]
        in starred_ids
    ]

    return make_subsonic_response(
        {
            "status":
                "ok",

            "version":
                SUBSONIC_VERSION,

            "serverVersion":
                SERVER_VERSION,

            "openSubsonic":
                True,

            "starred2": {
                "song":
                    songs,
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

        return [
            dict(row)
            for row
            in conn.execute(
                """
                SELECT *
                FROM playlists
                ORDER BY name
                """
            )
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
            (
                playlist_id,
            ),
        ).fetchone()

        return (
            dict(row)
            if row
            else None
        )


def save_playlist_sync(
    playlist,
):

    with sqlite3.connect(
        DB_FILE
    ) as conn:

        conn.execute(
            """
            INSERT OR REPLACE INTO playlists
            (
                id,
                name,
                comment,
                owner,
                public,
                song_ids,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                playlist["id"],
                playlist["name"],
                playlist.get(
                    "comment",
                    "",
                ),
                playlist.get(
                    "owner",
                    "admin",
                ),
                int(
                    playlist.get(
                        "public",
                        False,
                    )
                ),
                json.dumps(
                    playlist.get(
                        "song_ids",
                        [],
                    )
                ),
                playlist.get(
                    "created_at",
                    time.time(),
                ),
                time.time(),
            ),
        )

        conn.commit()


@app.get(
    "/rest/getPlaylists.view"
)
async def rest_playlists(
    request: Request,
):

    error = require_auth(
        request
    )

    if error:
        return error

    playlists = await asyncio.to_thread(
        playlists_sync
    )

    items = []

    for playlist in playlists:

        ids = json.loads(
            playlist.get(
                "song_ids",
                "[]",
            )
        )

        items.append(
            {
                "id":
                    playlist["id"],

                "name":
                    playlist["name"],

                "comment":
                    playlist["comment"],

                "owner":
                    playlist["owner"],

                "public":
                    bool(
                        playlist[
                            "public"
                        ]
                    ),

                "songCount":
                    len(ids),
            }
        )

    return make_subsonic_response(
        {
            "status":
                "ok",

            "version":
                SUBSONIC_VERSION,

            "serverVersion":
                SERVER_VERSION,

            "openSubsonic":
                True,

            "playlists": {
                "playlist":
                    items,
            },
        },
        request,
    )


@app.get(
    "/rest/getPlaylist.view"
)
async def rest_playlist(
    request: Request,
    id: str = Query(...),
):

    error = require_auth(
        request
    )

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
                for item
                in library[
                    "songs"
                ]
                if item[
                    "id"
                ] == song_id
            ),
            None,
        )

        if song:
            songs.append(
                song_to_subsonic(
                    song
                )
            )

    return make_subsonic_response(
        {
            "status":
                "ok",

            "version":
                SUBSONIC_VERSION,

            "serverVersion":
                SERVER_VERSION,

            "openSubsonic":
                True,

            "playlist": {
                "id":
                    playlist["id"],

                "name":
                    playlist["name"],

                "comment":
                    playlist["comment"],

                "owner":
                    playlist["owner"],

                "public":
                    bool(
                        playlist[
                            "public"
                        ]
                    ),

                "songCount":
                    len(songs),

                "entry":
                    songs,
            },
        },
        request,
    )


@app.get(
    "/rest/createPlaylist.view"
)
@app.post(
    "/rest/createPlaylist.view"
)
async def rest_create_playlist(
    request: Request,
    name: str = Query(
        "New Playlist"
    ),
    songId: Optional[list[str]] = Query(
        default=None
    ),
):

    error = require_auth(
        request
    )

    if error:
        return error

    settings = load_settings()

    playlist = {
        "id":
            "playlist-"
            + uuid.uuid4().hex[:12],

        "name":
            name,

        "comment":
            "",

        "owner":
            settings.get(
                "subsonic_user",
                "admin",
            ),

        "public":
            False,

        "song_ids":
            songId or [],

        "created_at":
            time.time(),
    }

    await asyncio.to_thread(
        save_playlist_sync,
        playlist,
    )

    return make_subsonic_response(
        {
            "status":
                "ok",

            "version":
                SUBSONIC_VERSION,

            "serverVersion":
                SERVER_VERSION,

            "openSubsonic":
                True,
        },
        request,
    )


@app.get(
    "/rest/updatePlaylist.view"
)
@app.post(
    "/rest/updatePlaylist.view"
)
async def rest_update_playlist(
    request: Request,
    playlistId: str = Query(...),
    name: Optional[str] = Query(
        None
    ),
    comment: Optional[str] = Query(
        None
    ),
    songId: Optional[list[str]] = Query(
        default=None
    ),
):

    error = require_auth(
        request
    )

    if error:
        return error

    playlist = await asyncio.to_thread(
        playlist_sync,
        playlistId,
    )

    if not playlist:

        return subsonic_error(
            request,
            70,
            "Playlist not found.",
        )

    if name is not None:
        playlist["name"] = name

    if comment is not None:
        playlist["comment"] = comment

    if songId is not None:
        playlist["song_ids"] = songId

    await asyncio.to_thread(
        save_playlist_sync,
        playlist,
    )

    return make_subsonic_response(
        {
            "status":
                "ok",

            "version":
                SUBSONIC_VERSION,

            "serverVersion":
                SERVER_VERSION,

            "openSubsonic":
                True,
        },
        request,
    )


@app.get(
    "/rest/deletePlaylist.view"
)
@app.post(
    "/rest/deletePlaylist.view"
)
async def rest_delete_playlist(
    request: Request,
    id: str = Query(...),
):

    error = require_auth(
        request
    )

    if error:
        return error

    with sqlite3.connect(
        DB_FILE
    ) as conn:

        conn.execute(
            """
            DELETE FROM playlists
            WHERE id = ?
            """,
            (id,),
        )

        conn.commit()

    return make_subsonic_response(
        {
            "status":
                "ok",

            "version":
                SUBSONIC_VERSION,

            "serverVersion":
                SERVER_VERSION,

            "openSubsonic":
                True,
        },
        request,
    )


# ============================================================
# SCROBBLING
# ============================================================

@app.get(
    "/rest/scrobble.view"
)
async def rest_scrobble(
    request: Request,
    id: str = Query(""),
    submission: bool = Query(True),
):

    error = require_auth(
        request
    )

    if error:
        return error

    return make_subsonic_response(
        {
            "status":
                "ok",

            "version":
                SUBSONIC_VERSION,

            "serverVersion":
                SERVER_VERSION,

            "openSubsonic":
                True,
        },
        request,
    )


@app.get(
    "/rest/getNowPlaying.view"
)
async def rest_now_playing(
    request: Request,
):

    error = require_auth(
        request
    )

    if error:
        return error

    return make_subsonic_response(
        {
            "status":
                "ok",

            "version":
                SUBSONIC_VERSION,

            "serverVersion":
                SERVER_VERSION,

            "openSubsonic":
                True,

            "nowPlaying": {
                "entry": [],
            },
        },
        request,
    )


# ============================================================
# EXTRA OPENSONIC COMPATIBILITY
# ============================================================

@app.get(
    "/rest/getSimilarSongs2.view"
)
async def rest_similar_songs(
    request: Request,
    id: str = Query(...),
    count: int = Query(20),
):

    error = require_auth(
        request
    )

    if error:
        return error

    target = await find_song(
        id
    )

    if not target:

        return subsonic_error(
            request,
            70,
            "Song not found.",
        )

    library = await build_library()

    similar = [
        song
        for song
        in library[
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
            "status":
                "ok",

            "version":
                SUBSONIC_VERSION,

            "serverVersion":
                SERVER_VERSION,

            "openSubsonic":
                True,

            "similarSongs2": {
                "song": [
                    song_to_subsonic(
                        song
                    )
                    for song
                    in similar[
                        :max(
                            0,
                            count,
                        )
                    ]
                ],
            },
        },
        request,
    )


@app.get(
    "/rest/getLyricsBySongId.view"
)
async def rest_lyrics(
    request: Request,
    id: str = Query(""),
):

    error = require_auth(
        request
    )

    if error:
        return error

    return make_subsonic_response(
        {
            "status":
                "ok",

            "version":
                SUBSONIC_VERSION,

            "serverVersion":
                SERVER_VERSION,

            "openSubsonic":
                True,

            "lyricsList": {
                "structuredLyrics":
                    [],
            },
        },
        request,
    )
