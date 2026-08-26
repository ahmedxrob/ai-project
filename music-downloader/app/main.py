import asyncio
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

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
# APP
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Xrob Music")

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
# STORAGE
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

MAX_CONCURRENT_DOWNLOADS = 3

SUBSONIC_VERSION = "1.16.1"
SERVER_VERSION = "1.0.0"


DEFAULT_SETTINGS = {
    "audio_format": "mp3",
    "audio_quality": "320K",
    "embed_thumbnail": True,
    "embed_metadata": True,
    "max_results": 20,
    "organize_by_artist": False,
    "poll_interval": 1500,

    # Xrob Music OpenSubsonic account
    "subsonic_user": os.getenv(
        "SUBSONIC_USER",
        "admin",
    ),
    "subsonic_password": os.getenv(
        "SUBSONIC_PASSWORD",
        "",
    ),
}


# ============================================================
# RUNTIME
# ============================================================

TASKS = {}
task_queue = asyncio.Queue()

ACTIVE_PROCESSES = {}
LAST_SAVED_TIME = {}

METADATA_CACHE = {}


# ============================================================
# SETTINGS
# ============================================================

def load_settings():
    if SETTINGS_FILE.exists():
        try:
            with open(
                SETTINGS_FILE,
                "r",
                encoding="utf-8",
            ) as f:
                data = json.load(f)

            return {
                **DEFAULT_SETTINGS,
                **data,
            }

        except Exception:
            pass

    return DEFAULT_SETTINGS.copy()


def save_settings(data: dict):
    settings = load_settings()
    settings.update(data)

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


def _db_save_task_sync(task: dict):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO tasks
            (
                id,title,artist,album,url,elementId,status,
                percent,speed,step,error,last_updated,final_name
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
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
    task: dict,
    force: bool = False,
):
    task_id = task.get("id")
    now = time.time()

    if (
        force
        or now - LAST_SAVED_TIME.get(task_id, 0) > 0.5
    ):
        LAST_SAVED_TIME[task_id] = now

        await asyncio.to_thread(
            _db_save_task_sync,
            task,
        )


def _db_load_tasks_sync():
    if not DB_FILE.exists():
        return {}

    result = {}

    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row

        for row in conn.execute(
            "SELECT * FROM tasks"
        ):
            result[row["id"]] = dict(row)

    return result


def _db_clear_completed_sync():
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


def _db_delete_task_sync(task_id: str):
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
        self.active_connections = []

    async def connect(
        self,
        websocket: WebSocket,
    ):
        await websocket.accept()

        if websocket not in self.active_connections:
            self.active_connections.append(
                websocket
            )

    def disconnect(
        self,
        websocket: WebSocket,
    ):
        if websocket in self.active_connections:
            self.active_connections.remove(
                websocket
            )

    async def broadcast(
        self,
        message: dict,
    ):
        for connection in list(
            self.active_connections
        ):
            try:
                await connection.send_json(
                    message
                )
            except Exception:
                self.disconnect(connection)


manager = ConnectionManager()


async def notify_task_update(
    task: dict,
    force_save: bool = False,
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
# GENERAL HELPERS
# ============================================================

def clean_filename(value: str) -> str:
    value = value or "Unknown"

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


def format_duration(seconds):
    try:
        seconds = int(
            seconds or 0
        )

        return (
            f"{seconds // 60}:"
            f"{seconds % 60:02d}"
        )

    except Exception:
        return "0:00"


def format_size(size):
    try:
        if size >= 1024 ** 3:
            return f"{size / 1024 ** 3:.2f} GB"

        return f"{size / 1024 ** 2:.1f} MB"

    except Exception:
        return "0 MB"


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


# ============================================================
# AUDIO FILES
# ============================================================

def _audio_files_sync():
    return [
        p
        for p in DOWNLOAD_DIR.rglob("*")
        if (
            p.is_file()
            and not p.name.startswith(".")
            and p.suffix.lower()
            in AUDIO_EXTENSIONS
        )
    ]


async def get_all_audio_files():
    return await asyncio.to_thread(
        _audio_files_sync
    )


# ============================================================
# AUDIO METADATA
# ============================================================

def read_audio_metadata_sync(
    path: Path,
):
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
            capture_output=True,
            text=True,
            timeout=8,
        )

        metadata = {}

        if result.returncode == 0:
            raw = json.loads(
                result.stdout or "{}"
            )

            tags = (
                raw.get("format", {})
                .get("tags", {})
            )

            duration = (
                raw.get("format", {})
                .get("duration")
            )

            metadata = {
                "title":
                    tags.get("title")
                    or path.stem,

                "artist":
                    tags.get("artist")
                    or tags.get("album_artist")
                    or (
                        path.parent.name
                        if path.parent != DOWNLOAD_DIR
                        else "Unknown Artist"
                    ),

                "album":
                    tags.get("album")
                    or path.stem,

                "genre":
                    tags.get("genre")
                    or "",

                "year":
                    tags.get("date")
                    or tags.get("year")
                    or "",

                "track":
                    tags.get("track")
                    or "",

                "disc":
                    tags.get("disc")
                    or "",

                "duration":
                    safe_float(
                        duration,
                        0,
                    ),
            }

        if not metadata:
            metadata = {
                "title": path.stem,
                "artist":
                    (
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
            }

        METADATA_CACHE[cache_key] = (
            stat.st_mtime,
            metadata,
        )

        return metadata

    except Exception:

        return {
            "title": path.stem,
            "artist":
                (
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
        }


async def read_audio_metadata(
    path: Path,
):
    return await asyncio.to_thread(
        read_audio_metadata_sync,
        path,
    )


# ============================================================
# LIBRARY INDEX
# ============================================================

def song_id_for_path(
    path: Path,
):
    rel = str(
        path.relative_to(
            DOWNLOAD_DIR
        )
    )

    return (
        "song-" +
        hashlib.sha1(
            rel.encode(
                "utf-8"
            )
        ).hexdigest()[:16]
    )


def artist_id(
    artist: str,
):
    return (
        "artist-" +
        hashlib.sha1(
            artist.encode(
                "utf-8"
            )
        ).hexdigest()[:16]
    )


def album_id(
    artist: str,
    album: str,
):
    raw = (
        f"{artist}\x00{album}"
    )

    return (
        "album-" +
        hashlib.sha1(
            raw.encode(
                "utf-8"
            )
        ).hexdigest()[:16]
    )


async def build_library_index():

    files = await get_all_audio_files()

    songs = []
    artists = {}
    albums = {}
    genres = {}

    for path in files:

        meta = await read_audio_metadata(
            path
        )

        sid = song_id_for_path(path)
        aid = artist_id(
            meta["artist"]
        )
        alid = album_id(
            meta["artist"],
            meta["album"],
        )

        song = {
            "id": sid,
            "title": meta["title"],
            "artist": meta["artist"],
            "artistId": aid,
            "album": meta["album"],
            "albumId": alid,
            "genre": meta["genre"],
            "year": meta["year"],
            "duration": int(
                meta["duration"] or 0
            ),
            "track": meta["track"],
            "discNumber": meta["disc"],
            "path": path,
            "suffix": path.suffix.lower(),
            "size": path.stat().st_size,
            "created": path.stat().st_ctime,
            "modified": path.stat().st_mtime,
        }

        songs.append(song)

        artists.setdefault(
            aid,
            {
                "id": aid,
                "name": meta["artist"],
                "albumIds": set(),
                "songIds": [],
            },
        )

        artists[aid]["albumIds"].add(
            alid
        )
        artists[aid]["songIds"].append(
            sid
        )

        albums.setdefault(
            alid,
            {
                "id": alid,
                "name": meta["album"],
                "artist": meta["artist"],
                "artistId": aid,
                "year": meta["year"],
                "songIds": [],
                "path": path,
            },
        )

        albums[alid]["songIds"].append(
            sid
        )

        if meta["genre"]:
            genres.setdefault(
                meta["genre"],
                0,
            )
            genres[meta["genre"]] += 1

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


async def find_song(
    song_id: str,
):
    library = await build_library_index()

    for song in library["songs"]:
        if song["id"] == song_id:
            return song

    return None


async def find_artist(
    artist_id_value: str,
):
    library = await build_library_index()

    return library["artists"].get(
        artist_id_value
    )


async def find_album(
    album_id_value: str,
):
    library = await build_library_index()

    return library["albums"].get(
        album_id_value
    )


# ============================================================
# FILE RESOLUTION
# ============================================================

def _resolve_file_sync(
    filename: str,
):

    base = DOWNLOAD_DIR.resolve()

    file_path = (
        DOWNLOAD_DIR / filename
    ).resolve()

    try:
        if not file_path.is_relative_to(
            base
        ):
            raise HTTPException(
                status_code=403,
                detail="Access denied",
            )

    except AttributeError:

        if (
            base not in file_path.parents
            and file_path != base
        ):
            raise HTTPException(
                status_code=403,
                detail="Access denied",
            )

    if (
        file_path.exists()
        and file_path.is_file()
    ):
        return file_path

    target = Path(
        filename
    ).name

    for path in DOWNLOAD_DIR.rglob("*"):

        if (
            path.is_file()
            and path.name == target
        ):
            return path.resolve()

    raise HTTPException(
        status_code=404,
        detail="File not found",
    )


async def resolve_file(
    filename: str,
):
    return await asyncio.to_thread(
        _resolve_file_sync,
        filename,
    )


# ============================================================
# DOWNLOAD HELPERS
# ============================================================

def cleanup_task_files(
    task_id: str,
):
    for path in DOWNLOAD_DIR.glob(
        f"*{task_id}*"
    ):
        try:
            if path.is_file():
                path.unlink()
        except Exception:
            pass


async def is_duplicate(
    title: str,
):

    files = await get_all_audio_files()

    key = normalize_duplicate_key(
        title
    )

    return any(
        normalize_duplicate_key(
            p.name
        ) == key
        for p in files
    )


# ============================================================
# NAVIDROME REMOVED
# ============================================================
# Xrob Music is now its own OpenSubsonic server.
# ============================================================


# ============================================================
# DOWNLOAD WORKER
# ============================================================

async def download_worker():

    while True:

        task_id = await task_queue.get()

        try:

            task = TASKS.get(
                task_id
            )

            if not task:
                continue

            if (
                task.get(
                    "cancel_requested"
                )
                or task.get(
                    "status"
                ) in {
                    "cancelled",
                    "canceled",
                }
            ):

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

                match = progress_re.search(
                    text
                )

                if match:

                    task["percent"] = (
                        float(
                            match.group(1)
                        )
                    )

                    speed_match = (
                        speed_re.search(
                            text
                        )
                    )

                    if speed_match:
                        task["speed"] = (
                            speed_match.group(1)
                            .replace(
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

                    task["status"] = "processing"
                    task["percent"] = 92
                    task["step"] = (
                        "Embedding cover art & tags..."
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

                stderr = await process.stderr.read()

                error = stderr.decode(
                    "utf-8",
                    errors="ignore",
                )

                await asyncio.to_thread(
                    cleanup_task_files,
                    task_id,
                )

                task["status"] = "error"
                task["error"] = (
                    error[-1200:]
                    or "Download failed."
                )
                task["step"] = (
                    "Download failed"
                )
                task["last_updated"] = (
                    time.time() * 1000
                )

                await notify_task_update(
                    task,
                    force_save=True,
                )

                continue

            possible = [
                p
                for p in DOWNLOAD_DIR.glob(
                    f"{task_id}.*"
                )
                if (
                    p.is_file()
                    and p.suffix.lower()
                    not in {
                        ".part",
                        ".ytdl",
                        ".temp",
                    }
                )
            ]

            if not possible:

                task["status"] = "error"
                task["error"] = (
                    "Downloaded file not found."
                )
                task["step"] = (
                    "Download failed"
                )
                task["last_updated"] = (
                    time.time() * 1000
                )

                await notify_task_update(
                    task,
                    force_save=True,
                )

                continue

            audio_file = possible[0]
            ext = (
                audio_file.suffix
                or f".{fmt}"
            )

            task["status"] = "processing"
            task["percent"] = 96
            task["step"] = (
                "Cleaning tags & metadata..."
            )
            task["last_updated"] = (
                time.time() * 1000
            )

            await notify_task_update(
                task,
                force_save=True,
            )

            title = clean_filename(
                task["title"]
            )

            cleaned_file = (
                DOWNLOAD_DIR
                / f"clean_{task_id}{ext}"
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
                "-disposition:v:0",
                "attached_pic",
                "-metadata",
                f"title={title}",
                "-metadata",
                f"album={title}",
                "-metadata",
                f"artist={task.get('artist', 'Unknown Artist')}",
                str(cleaned_file),
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
                and cleaned_file.exists()
            ):

                audio_file.unlink(
                    missing_ok=True
                )

                audio_file = cleaned_file

            elif not audio_file.exists():

                task["status"] = "error"
                task["error"] = (
                    clean_stderr.decode(
                        "utf-8",
                        errors="ignore",
                    )[-1000:]
                    or "Processing failed."
                )
                task["step"] = (
                    "Processing failed"
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
                f"{title}{ext}"
            )

            final_path = (
                final_dir / final_name
            )

            if (
                final_path.exists()
                or await is_duplicate(title)
            ):

                final_name = (
                    f"{title}_{task_id[:4]}{ext}"
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

            ACTIVE_PROCESSES.pop(
                task_id,
                None,
            )

            await asyncio.to_thread(
                cleanup_task_files,
                task_id,
            )

            task = TASKS.get(
                task_id
            )

            if task:

                task["status"] = "error"
                task["error"] = str(error)
                task["step"] = (
                    "Unexpected error"
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

            task_queue.task_done()


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
        _db_load_tasks_sync
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
            task["last_updated"] = now
            task["cancel_requested"] = False

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

            await task_queue.put(
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

    stdout, stderr = (
        await process.communicate()
    )

    if process.returncode != 0:

        raise RuntimeError(
            stderr.decode(
                "utf-8",
                errors="ignore",
            )[-2000:]
            or "YouTube search failed."
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
            "YouTube returned invalid search data."
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

        duration = (
            item.get(
                "duration",
                0,
            )
            or 0
        )

        results.append(
            {
                "id": video_id,
                "title": item.get(
                    "title",
                    "Unknown",
                ),
                "channel":
                    (
                        item.get("channel")
                        or item.get("uploader")
                        or "Unknown Artist"
                    ),
                "duration": duration,
                "duration_text":
                    format_duration(
                        duration
                    ),
                "thumbnail":
                    item.get(
                        "thumbnail"
                    )
                    or
                    f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                "url":
                    f"https://www.youtube.com/watch?v={video_id}",
            }
        )

    return results


# ============================================================
# WEB API
# ============================================================

@app.get("/")
async def home():
    return FileResponse(
        STATIC_DIR / "index.html"
    )


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "server": "Xrob Music",
        "version": SERVER_VERSION,
        "openSubsonic": True,
    }


@app.get("/api/settings")
async def api_get_settings():
    return load_settings()


@app.post("/api/settings")
async def api_save_settings(
    data: dict = Body(...),
):

    return save_settings(
        data
    )


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


@app.get("/api/preview")
async def api_preview(
    url: str = Query(...),
):

    if not url:
        raise HTTPException(
            status_code=400,
            detail="URL missing",
        )

    proc = (
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
        await proc.communicate()
    )

    if (
        proc.returncode != 0
        or not stdout
    ):

        raise HTTPException(
            status_code=500,
            detail="Failed to get preview.",
        )

    direct_url = (
        stdout.decode(
            "utf-8",
            errors="ignore",
        )
        .strip()
        .splitlines()[0]
    )

    ffmpeg_proc = (
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

                chunk = await ffmpeg_proc.stdout.read(
                    64 * 1024
                )

                if not chunk:
                    break

                yield chunk

        finally:

            if (
                ffmpeg_proc.returncode
                is None
            ):

                try:
                    ffmpeg_proc.kill()
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
            and task.get("status")
            in {
                "queued",
                "downloading",
                "processing",
            }
        ):

            return {
                "status": "already_queued",
                "task_id": task["id"],
            }

    task_id = (
        uuid.uuid4()
        .hex[:12]
    )

    task = {
        "id": task_id,
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
        "url": url,
        "elementId":
            str(
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
        "last_updated":
            time.time() * 1000,
        "final_name": "",
        "cancel_requested": False,
    }

    TASKS[task_id] = task

    await notify_task_update(
        task,
        force_save=True,
    )

    await task_queue.put(
        task_id
    )

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
        key=lambda t: (
            0
            if t.get("status")
            in {
                "queued",
                "downloading",
                "processing",
            }
            else 1,
            -safe_float(
                t.get(
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

    if task.get("status") in {
        "completed",
        "cancelled",
        "canceled",
        "error",
        "failed",
    }:

        return {
            "status":
                task.get("status"),
            "task_id":
                task_id,
        }

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


# IMPORTANT:
# STATIC ROUTE BEFORE /{task_id}
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

    task_ids = [
        task_id
        for task_id, task in TASKS.items()
        if task.get("status")
        in removable
    ]

    for task_id in task_ids:

        TASKS.pop(
            task_id,
            None
        )

        LAST_SAVED_TIME.pop(
            task_id,
            None,
        )

    await asyncio.to_thread(
        _db_clear_completed_sync
    )

    await manager.broadcast(
        {
            "type": "task_update",
            "action": "cleared",
            "count": len(task_ids),
        }
    )

    return {
        "status": "cleared",
        "count": len(task_ids),
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
        _db_delete_task_sync,
        task_id,
    )

    await manager.broadcast(
        {
            "type": "task_update",
            "action": "deleted",
            "task_id": task_id,
        }
    )

    return {
        "status": "deleted",
        "task_id": task_id,
    }


# ============================================================
# LIBRARY APIs
# ============================================================

@app.get("/api/library")
async def api_library():

    files = await get_all_audio_files()

    output = []
    total = 0

    for path in files:

        try:
            size = path.stat().st_size
        except Exception:
            continue

        total += size

        output.append(
            {
                "name":
                    str(
                        path.relative_to(
                            DOWNLOAD_DIR
                        )
                    ),
                "size":
                    format_size(size),
                "bytes":
                    size,
            }
        )

    output.sort(
        key=lambda x:
            x["name"].lower()
    )

    return {
        "files": output,
        "total_size":
            format_size(total),
        "total_bytes": total,
    }


@app.get("/api/stats")
async def api_stats():

    library = await build_library_index()

    files = library["songs"]

    artists = {
        song["artist"]
        for song in files
    }

    albums = {
        (
            song["artist"],
            song["album"],
        )
        for song in files
    }

    total = sum(
        song["size"]
        for song in files
    )

    return {
        "tracks":
            len(files),
        "artists":
            len(artists),
        "albums":
            len(albums),
        "total_bytes":
            total,
        "folder_size":
            format_size(total),
    }


@app.get("/api/home")
async def api_home():

    library = await build_library_index()

    songs = library["songs"]

    songs.sort(
        key=lambda s:
            s["modified"],
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
                "cover":
                    (
                        f"/rest/getCoverArt.view"
                        f"?id={urllib.parse.quote(song['id'])}"
                    ),
                "duration":
                    song["duration"],
            }
        )

    stats = await api_stats()

    active = sum(
        1
        for task in TASKS.values()
        if task.get("status")
        in {
            "queued",
            "downloading",
            "processing",
        }
    )

    return {
        "stats": stats,
        "active_downloads":
            active,
        "recently_added":
            recent,
    }


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

        path.unlink()

        return {
            "status": "deleted",
            "filename": filename,
        }

    except Exception as error:

        raise HTTPException(
            status_code=500,
            detail=str(error),
        )


@app.get(
    "/api/library/cover/{filename:path}"
)
async def api_library_cover(
    filename: str,
):

    path = await resolve_file(
        filename
    )

    cover_hash = hashlib.md5(
        str(path)
        .encode()
    ).hexdigest()

    cover_path = (
        COVER_CACHE_DIR
        / f"{cover_hash}.jpg"
    )

    if not cover_path.exists():

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
            str(cover_path),
        ]

        try:

            await asyncio.to_thread(
                subprocess.run,
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
            )

        except Exception:
            pass

    if cover_path.exists():

        return FileResponse(
            cover_path,
            media_type="image/jpeg",
        )

    return Response(
        content=(
            "<svg xmlns='http://www.w3.org/2000/svg' "
            "width='300' height='300'>"
            "<rect width='100%' height='100%' "
            "fill='#202020'/>"
            "<text x='50%' y='50%' "
            "fill='#999' font-size='55' "
            "text-anchor='middle' "
            "dominant-baseline='central'>🎵"
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


# ============================================================
# OPEN SUBSONIC AUTH
# ============================================================

def get_subsonic_credentials():

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
    user, password = (
        get_subsonic_credentials()
    )

    supplied_user =
        request.query_params.get(
            "u",
            "",
        )

    supplied_password =
        request.query_params.get(
            "p",
            "",
        )

    token =
        request.query_params.get(
            "t",
            "",
        )

    salt =
        request.query_params.get(
            "s",
            "",
        )

    api_key =
        request.query_params.get(
            "apiKey",
            "",
        )

    if not user or not password:

        return False

    if supplied_user != user:

        return False

    # OpenSubsonic API-key style auth
    if api_key:

        expected = hashlib.sha256(
            password.encode()
        ).hexdigest()

        return (
            api_key == expected
        )

    # Token authentication
    if token and salt:

        expected = hashlib.md5(
            (
                password +
                salt
            ).encode(
                "utf-8"
            )
        ).hexdigest()

        return (
            token.lower()
            == expected.lower()
        )

    # Legacy password auth
    if supplied_password:

        if supplied_password == password:
            return True

        if (
            supplied_password.startswith(
                "enc:"
            )
            and supplied_password[4:]
            == password
        ):
            return True

        if (
            len(supplied_password)
            == 32
        ):

            return (
                supplied_password.lower()
                ==
                hashlib.md5(
                    password.encode(
                        "utf-8"
                    )
                ).hexdigest()
            )

    return False


def subsonic_error(
    code,
    message,
    request: Request,
):

    payload = {
        "status": "failed",
        "version": SUBSONIC_VERSION,
        "serverVersion": SERVER_VERSION,
        "openSubsonic": True,
        "error": {
            "code": str(code),
            "message": message,
        },
    }

    return subsonic_response(
        payload,
        request,
    )


def ensure_auth(
    request: Request,
):

    if not validate_subsonic_auth(
        request
    ):

        return subsonic_error(
            40,
            "Wrong username or password.",
            request,
        )

    return None


# ============================================================
# SUBSONIC SERIALIZATION
# ============================================================

def scalar_string(value):

    if isinstance(value, bool):
        return (
            "true"
            if value
            else "false"
        )

    if value is None:
        return ""

    return str(value)


def element_from_data(
    parent,
    key,
    value,
):
    if isinstance(value, dict):

        element = ET.SubElement(
            parent,
            key,
        )

        for child_key, child_value in value.items():

            if isinstance(
                child_value,
                (
                    dict,
                    list,
                ),
            ):

                element_from_data(
                    element,
                    child_key,
                    child_value,
                )

            else:

                element.set(
                    child_key,
                    scalar_string(
                        child_value
                    ),
                )

        return element

    if isinstance(value, list):

        for item in value:

            if isinstance(
                item,
                dict,
            ):

                element_from_data(
                    parent,
                    key,
                    item,
                )

            else:

                child = ET.SubElement(
                    parent,
                    key,
                )

                child.text =
                    scalar_string(
                        item
                    )

        return parent

    child = ET.SubElement(
        parent,
        key,
    )

    child.text = scalar_string(
        value
    )

    return child


def subsonic_response(
    payload: dict,
    request: Request,
):

    params = request.query_params

    fmt = (
        params.get(
            "f",
            "xml",
        )
        or "xml"
    ).lower()


    # JSON
    if fmt == "json":

        return JSONResponse(
            {
                "subsonic-response":
                    payload
            }
        )


    # XML
    root_attrs = {}

    for key in (
        "status",
        "version",
        "serverVersion",
        "openSubsonic",
        "type",
    ):

        if key in payload:
            root_attrs[key] =
                scalar_string(
                    payload[key]
                )

    if "type" not in root_attrs:
        root_attrs["type"] = (
            "Xrob Music"
        )

    root = ET.Element(
        "subsonic-response",
        root_attrs,
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

        if isinstance(
            value,
            dict,
        ):

            element_from_data(
                root,
                key,
                value,
            )

        elif isinstance(
            value,
            list,
        ):

            element_from_data(
                root,
                key,
                value,
            )

        else:

            root.set(
                key,
                scalar_string(
                    value
                ),
            )

    xml = ET.tostring(
        root,
        encoding="utf-8",
        xml_declaration=True,
    )

    return Response(
        content=xml,
        media_type="application/xml",
    )


# ============================================================
# SUBSONIC / REST COMMON
# ============================================================

def now_ms():
    return int(
        time.time() * 1000
    )


def song_to_api(
    song,
    request: Request,
):

    return {
        "id": song["id"],
        "parent": song["albumId"],
        "isDir": False,
        "title": song["title"],
        "album": song["album"],
        "artist": song["artist"],
        "track": safe_int(
            song["track"],
            0,
        ),
        "year": safe_int(
            song["year"],
            0,
        ),
        "genre": song["genre"],
        "coverArt": song["id"],
        "size": song["size"],
        "contentType":
            MEDIA_TYPES.get(
                song["suffix"],
                "audio/mpeg",
            ),
        "suffix":
            song["suffix"]
            .lstrip("."),
        "duration": song["duration"],
        "bitRate": 0,
        "path":
            str(
                song["path"].relative_to(
                    DOWNLOAD_DIR
                )
            ),
        "isVideo": False,
        "type": "music",
        "starred": is_starred_sync(
            song["id"]
        ),
    }


def artist_to_api(
    artist,
):
    return {
        "id": artist["id"],
        "name": artist["name"],
        "albumCount":
            len(
                artist["albumIds"]
            ),
    }


def album_to_api(
    album,
):

    return {
        "id": album["id"],
        "parent":
            album["artistId"],
        "isDir": True,
        "title": album["name"],
        "name": album["name"],
        "album": album["name"],
        "artist": album["artist"],
        "artistId": album["artistId"],
        "year":
            safe_int(
                album["year"],
                0,
            ),
    }


# ============================================================
# STARS
# ============================================================

def is_starred_sync(
    item_id: str,
):

    with sqlite3.connect(DB_FILE) as conn:

        row = conn.execute(
            "SELECT 1 FROM stars WHERE item_id = ?",
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
                (item_id, starred_at)
                VALUES (?,?)
                """,
                (
                    item_id,
                    time.time(),
                ),
            )

        else:

            conn.execute(
                "DELETE FROM stars WHERE item_id = ?",
                (item_id,),
            )

        conn.commit()


# ============================================================
# PLAYLISTS
# ============================================================

def _get_playlists_sync():

    with sqlite3.connect(DB_FILE) as conn:

        conn.row_factory = sqlite3.Row

        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM playlists ORDER BY name"
            )
        ]


def _get_playlist_sync(
    playlist_id,
):

    with sqlite3.connect(DB_FILE) as conn:

        conn.row_factory = sqlite3.Row

        row = conn.execute(
            """
            SELECT * FROM playlists
            WHERE id = ?
            """,
            (playlist_id,),
        ).fetchone()

        return (
            dict(row)
            if row
            else None
        )


def _save_playlist_sync(
    playlist,
):

    with sqlite3.connect(DB_FILE) as conn:

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
            VALUES (?,?,?,?,?,?,?,?)
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


# ============================================================
# OPEN SUBSONIC ENDPOINTS
# ============================================================

@app.get("/rest/ping.view")
@app.get("/rest/ping")
async def rest_ping(
    request: Request,
):

    return subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
        },
        request,
    )


@app.get(
    "/rest/getOpenSubsonicExtensions.view"
)
async def rest_extensions(
    request: Request,
):

    return subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "openSubsonicExtensions": {
                "extension": [
                    {
                        "name": "transcodeOffset",
                        "versions": "1",
                    },
                    {
                        "name": "songLyrics",
                        "versions": "1",
                    },
                    {
                        "name": "playerControl",
                        "versions": "1",
                    },
                ],
            },
        },
        request,
    )


@app.get("/rest/getLicense.view")
async def rest_license(
    request: Request,
):

    return subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "license": {
                "valid": True,
            },
        },
        request,
    )


@app.get("/rest/getMusicFolders.view")
async def rest_music_folders(
    request: Request,
):

    auth_error = ensure_auth(
        request
    )

    if auth_error:
        return auth_error

    return subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "musicFolders": {
                "musicFolder": [
                    {
                        "id": "1",
                        "name": "Music",
                    }
                ]
            },
        },
        request,
    )


@app.get("/rest/getArtists.view")
async def rest_artists(
    request: Request,
):

    auth_error = ensure_auth(
        request
    )

    if auth_error:
        return auth_error

    library = await build_library_index()

    grouped = {}

    for artist in library["artists"].values():

        first = (
            artist["name"][:1]
            .upper()
            or "#"
        )

        grouped.setdefault(
            first,
            [],
        ).append(
            artist_to_api(
                artist
            )
        )

    indexes = []

    for letter in sorted(
        grouped.keys()
    ):

        indexes.append(
            {
                "name": letter,
                "artist":
                    sorted(
                        grouped[letter],
                        key=lambda x:
                            x["name"].lower(),
                    ),
            }
        )

    return subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "artists": {
                "ignoredArticles": "",
                "index": indexes,
            },
        },
        request,
    )


@app.get("/rest/getIndexes.view")
async def rest_indexes(
    request: Request,
):

    return await rest_artists(
        request
    )


@app.get("/rest/getArtist.view")
async def rest_artist(
    request: Request,
    id: str = Query(...),
):

    auth_error = ensure_auth(
        request
    )

    if auth_error:
        return auth_error

    artist = await find_artist(
        id
    )

    if not artist:

        return subsonic_error(
            70,
            "Artist not found.",
            request,
        )

    library = await build_library_index()

    albums = []

    for album_id_value in artist["albumIds"]:

        album =
            library["albums"].get(
                album_id_value
            )

        if album:
            albums.append(
                album_to_api(
                    album
                )
            )

    return subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "artist": {
                **artist_to_api(
                    artist
                ),
                "album": albums,
            },
        },
        request,
    )


@app.get("/rest/getAlbum.view")
async def rest_album(
    request: Request,
    id: str = Query(...),
):

    auth_error = ensure_auth(
        request
    )

    if auth_error:
        return auth_error

    album = await find_album(
        id
    )

    if not album:

        return subsonic_error(
            70,
            "Album not found.",
            request,
        )

    library = await build_library_index()

    songs = []

    for sid in album["songIds"]:

        for song in library["songs"]:

            if song["id"] == sid:

                songs.append(
                    song_to_api(
                        song,
                        request,
                    )
                )

                break

    return subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "album": {
                **album_to_api(
                    album
                ),
                "song": songs,
            },
        },
        request,
    )


@app.get("/rest/getSong.view")
async def rest_song(
    request: Request,
    id: str = Query(...),
):

    auth_error = ensure_auth(
        request
    )

    if auth_error:
        return auth_error

    song = await find_song(
        id
    )

    if not song:

        return subsonic_error(
            70,
            "Song not found.",
            request,
        )

    return subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "song":
                song_to_api(
                    song,
                    request,
                ),
        },
        request,
    )


@app.get("/rest/getMusicDirectory.view")
async def rest_music_directory(
    request: Request,
    id: str = Query(...),
):

    auth_error = ensure_auth(
        request
    )

    if auth_error:
        return auth_error

    library = await build_library_index()

    children = []

    if id == "1":

        for artist in sorted(
            library["artists"].values(),
            key=lambda x:
                x["name"].lower(),
        ):

            children.append(
                {
                    "id": artist["id"],
                    "parent": "1",
                    "isDir": True,
                    "title": artist["name"],
                    "name": artist["name"],
                    "type": "artist",
                }
            )

    elif id.startswith(
        "artist-"
    ):

        artist =
            library["artists"].get(
                id
            )

        if artist:

            for album_id_value in artist[
                "albumIds"
            ]:

                album =
                    library["albums"].get(
                        album_id_value
                    )

                if album:

                    children.append(
                        {
                            **album_to_api(
                                album
                            ),
                            "type": "album",
                        }
                    )

    elif id.startswith(
        "album-"
    ):

        album =
            library["albums"].get(
                id
            )

        if album:

            for sid in album[
                "songIds"
            ]:

                song = next(
                    (
                        x
                        for x
                        in library["songs"]
                        if x["id"] == sid
                    ),
                    None,
                )

                if song:

                    children.append(
                        song_to_api(
                            song,
                            request,
                        )
                    )

    return subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "directory": {
                "id": id,
                "name": "Music",
                "child": children,
            },
        },
        request,
    )


@app.get("/rest/getAlbumList2.view")
async def rest_album_list(
    request: Request,
    type: str = Query(
        "alphabeticalByName"
    ),
    size: int = Query(50),
    offset: int = Query(0),
):

    auth_error = ensure_auth(
        request
    )

    if auth_error:
        return auth_error

    library = await build_library_index()

    albums = list(
        library["albums"].values()
    )

    albums.sort(
        key=lambda x:
            (
                x["name"]
                if type != "random"
                else uuid.uuid4().hex
            ).lower()
    )

    sliced = albums[
        max(0, offset):
        max(0, offset)
        + max(1, size)
    ]

    return subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "albumList2": {
                "album": [
                    album_to_api(
                        album
                    )
                    for album in sliced
                ]
            },
        },
        request,
    )


@app.get("/rest/getRandomSongs.view")
async def rest_random_songs(
    request: Request,
    size: int = Query(10),
):

    auth_error = ensure_auth(
        request
    )

    if auth_error:
        return auth_error

    library = await build_library_index()

    songs = library["songs"]

    import random

    random.shuffle(
        songs
    )

    songs = songs[
        :max(1, size)
    ]

    return subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "randomSongs": {
                "song": [
                    song_to_api(
                        song,
                        request,
                    )
                    for song in songs
                ]
            },
        },
        request,
    )


@app.get("/rest/search3.view")
async def rest_search3(
    request: Request,
    query: str = Query(""),
    artistCount: int = Query(20),
    albumCount: int = Query(20),
    songCount: int = Query(20),
):

    auth_error = ensure_auth(
        request
    )

    if auth_error:
        return auth_error

    library = await build_library_index()

    q = query.lower().strip()

    artists = []
    albums = []
    songs = []

    for artist in library["artists"].values():

        if q in artist["name"].lower():
            artists.append(
                artist_to_api(
                    artist
                )
            )

    for album in library["albums"].values():

        if (
            q in album["name"].lower()
            or q in album["artist"].lower()
        ):

            albums.append(
                album_to_api(
                    album
                )
            )

    for song in library["songs"]:

        text = " ".join(
            [
                song["title"],
                song["artist"],
                song["album"],
            ]
        ).lower()

        if q in text:
            songs.append(
                song_to_api(
                    song,
                    request,
                )
            )

    return subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "searchResult3": {
                "artist":
                    artists[
                        :max(0, artistCount)
                    ],
                "album":
                    albums[
                        :max(0, albumCount)
                    ],
                "song":
                    songs[
                        :max(0, songCount)
                    ],
            },
        },
        request,
    )


@app.get("/rest/getGenres.view")
async def rest_genres(
    request: Request,
):

    auth_error = ensure_auth(
        request
    )

    if auth_error:
        return auth_error

    library = await build_library_index()

    genres = [
        {
            "songCount": count,
            "albumCount": 0,
            "value": genre,
        }
        for genre, count
        in sorted(
            library["genres"].items()
        )
    ]

    return subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "genres": {
                "genre": genres,
            },
        },
        request,
    )


@app.get("/rest/getSongsByGenre.view")
async def rest_songs_by_genre(
    request: Request,
    genre: str = Query(""),
    count: int = Query(50),
    offset: int = Query(0),
):

    auth_error = ensure_auth(
        request
    )

    if auth_error:
        return auth_error

    library = await build_library_index()

    songs = [
        song
        for song in library["songs"]
        if song["genre"].lower()
        == genre.lower()
    ]

    songs = songs[
        offset:
        offset + count
    ]

    return subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "songsByGenre": {
                "song": [
                    song_to_api(
                        song,
                        request,
                    )
                    for song in songs
                ]
            },
        },
        request,
    )


# ============================================================
# STREAM / DOWNLOAD / COVER ART
# ============================================================

@app.get("/rest/stream.view")
async def rest_stream(
    request: Request,
    id: str = Query(...),
):

    auth_error = ensure_auth(
        request
    )

    if auth_error:
        return auth_error

    song = await find_song(
        id
    )

    if not song:

        return subsonic_error(
            70,
            "Song not found.",
            request,
        )

    return FileResponse(
        song["path"],
        media_type=MEDIA_TYPES.get(
            song["suffix"],
            "audio/mpeg",
        ),
        filename=song["path"].name,
    )


@app.get("/rest/download.view")
async def rest_download(
    request: Request,
    id: str = Query(...),
):

    auth_error = ensure_auth(
        request
    )

    if auth_error:
        return auth_error

    song = await find_song(
        id
    )

    if not song:

        return subsonic_error(
            70,
            "Song not found.",
            request,
        )

    return FileResponse(
        song["path"],
        media_type=MEDIA_TYPES.get(
            song["suffix"],
            "application/octet-stream",
        ),
        filename=song["path"].name,
    )


@app.get("/rest/getCoverArt.view")
async def rest_cover_art(
    request: Request,
    id: str = Query(...),
    size: int = Query(0),
):

    auth_error = ensure_auth(
        request
    )

    if auth_error:
        return auth_error

    path = None

    song = await find_song(
        id
    )

    if song:

        path = song["path"]

    elif id.startswith(
        "album-"
    ):

        album = await find_album(
            id
        )

        if album:
            path = album["path"]

    if not path:

        return Response(
            status_code=404
        )

    cover_hash = hashlib.md5(
        str(path)
        .encode()
    ).hexdigest()

    cover_path = (
        COVER_CACHE_DIR
        / f"{cover_hash}.jpg"
    )

    if not cover_path.exists():

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
            str(cover_path),
        ]

        try:

            await asyncio.to_thread(
                subprocess.run,
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
            )

        except Exception:
            pass

    if not cover_path.exists():

        return Response(
            status_code=404
        )

    return FileResponse(
        cover_path,
        media_type="image/jpeg",
    )


# ============================================================
# STARS
# ============================================================

@app.get("/rest/star.view")
async def rest_star(
    request: Request,
    id: str = Query(...),
):

    auth_error = ensure_auth(
        request
    )

    if auth_error:
        return auth_error

    await asyncio.to_thread(
        set_star_sync,
        id,
        True,
    )

    return subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
        },
        request,
    )


@app.get("/rest/unstar.view")
async def rest_unstar(
    request: Request,
    id: str = Query(...),
):

    auth_error = ensure_auth(
        request
    )

    if auth_error:
        return auth_error

    await asyncio.to_thread(
        set_star_sync,
        id,
        False,
    )

    return subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
        },
        request,
    )


@app.get("/rest/getStarred2.view")
async def rest_starred2(
    request: Request,
):

    auth_error = ensure_auth(
        request
    )

    if auth_error:
        return auth_error

    with sqlite3.connect(DB_FILE) as conn:

        rows = conn.execute(
            "SELECT item_id FROM stars"
        ).fetchall()

    ids = {
        row[0]
        for row in rows
    }

    library = await build_library_index()

    songs = [
        song_to_api(
            song,
            request,
        )
        for song in library["songs"]
        if song["id"] in ids
    ]

    return subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "starred2": {
                "song": songs,
            },
        },
        request,
    )


# ============================================================
# SCROBBLE
# ============================================================

@app.get("/rest/scrobble.view")
async def rest_scrobble(
    request: Request,
    id: str = Query(""),
    submission: bool = Query(True),
):

    auth_error = ensure_auth(
        request
    )

    if auth_error:
        return auth_error

    return subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
        },
        request,
    )


# ============================================================
# NOW PLAYING
# ============================================================

@app.get("/rest/getNowPlaying.view")
async def rest_now_playing(
    request: Request,
):

    auth_error = ensure_auth(
        request
    )

    if auth_error:
        return auth_error

    return subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "nowPlaying": {
                "entry": [],
            },
        },
        request,
    )


# ============================================================
# PLAYLISTS
# ============================================================

@app.get("/rest/getPlaylists.view")
async def rest_playlists(
    request: Request,
):

    auth_error = ensure_auth(
        request
    )

    if auth_error:
        return auth_error

    playlists = await asyncio.to_thread(
        _get_playlists_sync
    )

    result = []

    for playlist in playlists:

        song_ids = json.loads(
            playlist.get(
                "song_ids",
                "[]",
            )
        )

        result.append(
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
                        playlist["public"]
                    ),
                "songCount":
                    len(song_ids),
            }
        )

    return subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "playlists": {
                "playlist": result,
            },
        },
        request,
    )


@app.get("/rest/getPlaylist.view")
async def rest_playlist(
    request: Request,
    id: str = Query(...),
):

    auth_error = ensure_auth(
        request
    )

    if auth_error:
        return auth_error

    playlist = await asyncio.to_thread(
        _get_playlist_sync,
        id,
    )

    if not playlist:

        return subsonic_error(
            70,
            "Playlist not found.",
            request,
        )

    song_ids = json.loads(
        playlist.get(
            "song_ids",
            "[]",
        )
    )

    library = await build_library_index()

    songs = []

    for sid in song_ids:

        song = next(
            (
                item
                for item in library["songs"]
                if item["id"] == sid
            ),
            None,
        )

        if song:

            songs.append(
                song_to_api(
                    song,
                    request,
                )
            )

    return subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
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
                        playlist["public"]
                    ),
                "songCount":
                    len(songs),
                "entry":
                    songs,
            },
        },
        request,
    )


@app.post("/rest/createPlaylist.view")
@app.get("/rest/createPlaylist.view")
async def rest_create_playlist(
    request: Request,
    name: str = Query("New Playlist"),
    songId: list[str] = Query(default=[]),
):

    auth_error = ensure_auth(
        request
    )

    if auth_error:
        return auth_error

    playlist = {
        "id":
            "playlist-" +
            uuid.uuid4().hex[:12],
        "name":
            name or "New Playlist",
        "comment":
            "",
        "owner":
            load_settings().get(
                "subsonic_user",
                "admin",
            ),
        "public":
            False,
        "song_ids":
            songId,
        "created_at":
            time.time(),
    }

    await asyncio.to_thread(
        _save_playlist_sync,
        playlist,
    )

    return subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
        },
        request,
    )


@app.post("/rest/updatePlaylist.view")
@app.get("/rest/updatePlaylist.view")
async def rest_update_playlist(
    request: Request,
    playlistId: str = Query(...),
    name: str | None = Query(None),
    comment: str | None = Query(None),
    songId: list[str] = Query(default=[]),
):

    auth_error = ensure_auth(
        request
    )

    if auth_error:
        return auth_error

    playlist = await asyncio.to_thread(
        _get_playlist_sync,
        playlistId,
    )

    if not playlist:

        return subsonic_error(
            70,
            "Playlist not found.",
            request,
        )

    if name is not None:
        playlist["name"] = name

    if comment is not None:
        playlist["comment"] = comment

    if songId:
        playlist["song_ids"] = songId

    await asyncio.to_thread(
        _save_playlist_sync,
        playlist,
    )

    return subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
        },
        request,
    )


@app.post("/rest/deletePlaylist.view")
@app.get("/rest/deletePlaylist.view")
async def rest_delete_playlist(
    request: Request,
    id: str = Query(...),
):

    auth_error = ensure_auth(
        request
    )

    if auth_error:
        return auth_error

    with sqlite3.connect(DB_FILE) as conn:

        conn.execute(
            "DELETE FROM playlists WHERE id = ?",
            (id,),
        )

        conn.commit()

    return subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
        },
        request,
    )


# ============================================================
# OPTIONAL INFORMATION ENDPOINTS
# ============================================================

@app.get("/rest/getLyricsBySongId.view")
async def rest_lyrics(
    request: Request,
    id: str = Query(""),
):

    auth_error = ensure_auth(
        request
    )

    if auth_error:
        return auth_error

    return subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "lyricsList": {
                "structuredLyrics": [],
            },
        },
        request,
    )


@app.get("/rest/getSimilarSongs2.view")
async def rest_similar(
    request: Request,
    id: str = Query(...),
    count: int = Query(20),
):

    auth_error = ensure_auth(
        request
    )

    if auth_error:
        return auth_error

    target = await find_song(
        id
    )

    if not target:

        return subsonic_error(
            70,
            "Song not found.",
            request,
        )

    library = await build_library_index()

    candidates = [
        song
        for song in library["songs"]
        if (
            song["id"] != id
            and song["artist"]
            == target["artist"]
        )
    ]

    return subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "similarSongs2": {
                "song": [
                    song_to_api(
                        song,
                        request,
                    )
                    for song
                    in candidates[:count]
                ],
            },
        },
        request,
    )


@app.get("/rest/getArtistInfo2.view")
async def rest_artist_info(
    request: Request,
    id: str = Query(...),
):

    auth_error = ensure_auth(
        request
    )

    if auth_error:
        return auth_error

    artist = await find_artist(
        id
    )

    return subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "artistInfo2": {
                "biography": "",
                "musicBrainzId": "",
                "lastFmUrl": "",
                "similarArtist": [],
            },
        },
        request,
    )


@app.get("/rest/getAlbumInfo2.view")
async def rest_album_info(
    request: Request,
    id: str = Query(...),
):

    auth_error = ensure_auth(
        request
    )

    if auth_error:
        return auth_error

    return subsonic_response(
        {
            "status": "ok",
            "version": SUBSONIC_VERSION,
            "serverVersion": SERVER_VERSION,
            "openSubsonic": True,
            "albumInfo": {
                "notes": "",
                "musicBrainzId": "",
                "lastFmUrl": "",
            },
        },
        request,
    )
