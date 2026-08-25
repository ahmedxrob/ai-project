from fastapi import FastAPI, Query, HTTPException, Body, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse, FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

import asyncio
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


# ============================================================
# APP
# ============================================================

app = FastAPI(title="Xrob Music")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ============================================================
# PATHS / CONFIG
# ============================================================

DOWNLOAD_DIR = Path(
    os.getenv("DOWNLOAD_DIR", "/share/navidrome/music")
)

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

COVER_CACHE_DIR = DOWNLOAD_DIR / ".covers"
COVER_CACHE_DIR.mkdir(parents=True, exist_ok=True)

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

# Maximum simultaneous yt-dlp downloads.
MAX_CONCURRENT_DOWNLOADS = 3


DEFAULT_SETTINGS = {
    "audio_format": "mp3",
    "audio_quality": "320K",
    "embed_thumbnail": True,
    "embed_metadata": True,
    "max_results": 20,
    "organize_by_artist": False,
    "poll_interval": 1500,
    "navidrome_url": os.getenv("NAVIDROME_URL", ""),
    "navidrome_user": os.getenv("NAVIDROME_USER", ""),
    "navidrome_token": os.getenv("NAVIDROME_TOKEN", ""),
    "navidrome_salt": os.getenv("NAVIDROME_SALT", ""),
}


# ============================================================
# GLOBAL STATE
# ============================================================

TASKS = {}

task_queue = asyncio.Queue()

ACTIVE_PROCESSES = {}

LAST_SAVED_TIME = {}

# Prevent two requests from enqueueing the same song simultaneously.
DOWNLOAD_LOCK = asyncio.Lock()

# Background worker tasks.
WORKERS = []


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
                final_name TEXT,
                cancel_requested INTEGER DEFAULT 0
            )
            """
        )

        # Upgrade old installations which don't have cancel_requested.
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
        }

        if "cancel_requested" not in columns:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN cancel_requested INTEGER DEFAULT 0"
            )

        conn.commit()


def _db_save_task_sync(task: dict):
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
                final_name,
                cancel_requested
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
                int(bool(task.get("cancel_requested", False))),
            ),
        )

        conn.commit()


async def db_save_task(task: dict, force: bool = False):
    task_id = task.get("id")

    now = time.time()

    if force or now - LAST_SAVED_TIME.get(task_id, 0) >= 0.5:
        LAST_SAVED_TIME[task_id] = now

        await asyncio.to_thread(
            _db_save_task_sync,
            dict(task),
        )


def _db_load_tasks_sync():
    if not DB_FILE.exists():
        return {}

    tasks = {}

    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row

        rows = conn.execute(
            "SELECT * FROM tasks"
        ).fetchall()

        for row in rows:
            task = dict(row)

            task["cancel_requested"] = bool(
                task.get("cancel_requested", 0)
            )

            tasks[task["id"]] = task

    return tasks


# ============================================================
# WEBSOCKET
# ============================================================

class ConnectionManager:
    def __init__(self):
        self.active_connections = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()

        if websocket not in self.active_connections:
            self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        dead = []

        for connection in list(self.active_connections):
            try:
                await connection.send_json(message)
            except Exception:
                dead.append(connection)

        for connection in dead:
            self.disconnect(connection)


manager = ConnectionManager()


async def notify_task_update(
    task: dict,
    force_save: bool = False,
):
    await db_save_task(task, force=force_save)

    await manager.broadcast(
        {
            "type": "task_update",
            "task": dict(task),
        }
    )


# ============================================================
# SETTINGS
# ============================================================

def load_settings():
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as file:
                data = json.load(file)

            settings = DEFAULT_SETTINGS.copy()
            settings.update(data)

            return settings

        except Exception:
            pass

    return DEFAULT_SETTINGS.copy()


def save_settings(data: dict):
    settings = load_settings()

    allowed = set(DEFAULT_SETTINGS.keys())

    for key, value in data.items():
        if key in allowed:
            settings[key] = value

    # Basic validation.
    try:
        settings["max_results"] = max(
            1,
            min(
                int(settings.get("max_results", 20)),
                100,
            ),
        )
    except Exception:
        settings["max_results"] = 20

    if settings.get("audio_quality") not in {
        "128K",
        "192K",
        "256K",
        "320K",
    }:
        settings["audio_quality"] = "320K"

    if settings.get("audio_format") not in {
        "mp3",
        "m4a",
        "opus",
        "ogg",
        "wav",
        "flac",
    }:
        settings["audio_format"] = "mp3"

    with open(SETTINGS_FILE, "w", encoding="utf-8") as file:
        json.dump(
            settings,
            file,
            indent=2,
            ensure_ascii=False,
        )

    return settings


# ============================================================
# HELPERS
# ============================================================

def normalize_duplicate_key(value: str) -> str:
    """
    Normalize title for duplicate detection.

    Example:
        Artist - Song (Official Video) 4K
    becomes approximately:
        artistsong
    """

    value = Path(value or "").stem.lower()

    value = re.sub(
        r"\b(?:official\s+)?(?:video|audio|music\s+video|lyrics?)\b",
        " ",
        value,
        flags=re.I,
    )

    value = re.sub(
        r"\b(?:hd|4k|8k|remastered|remaster|audio)\b",
        " ",
        value,
        flags=re.I,
    )

    value = re.sub(
        r"[^a-z0-9]+",
        "",
        value,
    )

    return value


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

    return value[:180] if value else "Unknown"


def format_duration(seconds):
    try:
        seconds = int(seconds or 0)

        minutes = seconds // 60
        seconds %= 60

        return f"{minutes}:{seconds:02d}"

    except Exception:
        return "0:00"


def format_size(size_bytes):
    try:
        if size_bytes >= 1024 ** 3:
            return f"{size_bytes / 1024 ** 3:.2f} GB"

        if size_bytes >= 1024 ** 2:
            return f"{size_bytes / 1024 ** 2:.1f} MB"

        if size_bytes >= 1024:
            return f"{size_bytes / 1024:.1f} KB"

        return f"{size_bytes} B"

    except Exception:
        return "0 B"


def cleanup_task_files(task_id: str):
    patterns = [
        f"{task_id}.*",
        f"clean_{task_id}.*",
    ]

    for pattern in patterns:
        for path in DOWNLOAD_DIR.glob(pattern):
            try:
                if path.is_file():
                    path.unlink()
            except Exception:
                pass


# ============================================================
# LIBRARY
# ============================================================

def _get_all_audio_files_sync():
    files = []

    for path in DOWNLOAD_DIR.rglob("*"):
        if not path.is_file():
            continue

        if path.name.startswith("."):
            continue

        if ".covers" in path.parts:
            continue

        if path.suffix.lower() not in AUDIO_EXTENSIONS:
            continue

        files.append(path)

    return files


async def get_all_audio_files():
    return await asyncio.to_thread(
        _get_all_audio_files_sync
    )


def _is_duplicate_sync(title: str):
    key = normalize_duplicate_key(title)

    if not key:
        return False

    for path in _get_all_audio_files_sync():
        if normalize_duplicate_key(path.name) == key:
            return True

    return False


async def is_duplicate(title: str):
    return await asyncio.to_thread(
        _is_duplicate_sync,
        title,
    )


def _is_duplicate_with_artist_sync(
    title: str,
    artist: str = "",
):
    """
    More reliable duplicate detection.

    First checks title.
    If title matches, that's enough to avoid duplicate downloads.
    """

    title_key = normalize_duplicate_key(title)

    if not title_key:
        return False

    for path in _get_all_audio_files_sync():
        file_key = normalize_duplicate_key(path.name)

        if file_key == title_key:
            return True

    return False


async def is_duplicate_with_artist(
    title: str,
    artist: str = "",
):
    return await asyncio.to_thread(
        _is_duplicate_with_artist_sync,
        title,
        artist,
    )


# ============================================================
# SECURE FILE RESOLUTION
# ============================================================

def _resolve_file_sync(filename: str) -> Path:
    filename = urllib.parse.unquote(
        filename.strip()
    )

    base_dir = DOWNLOAD_DIR.resolve()

    requested = (DOWNLOAD_DIR / filename).resolve()

    try:
        requested.relative_to(base_dir)
    except ValueError:
        raise HTTPException(
            status_code=403,
            detail="Access denied",
        )

    if requested.exists() and requested.is_file():
        return requested

    # Fallback by basename for old library paths.
    target_name = Path(filename).name

    for match in DOWNLOAD_DIR.rglob("*"):
        if not match.is_file():
            continue

        if match.name != target_name:
            continue

        try:
            match.resolve().relative_to(base_dir)
        except ValueError:
            continue

        return match

    raise HTTPException(
        status_code=404,
        detail="File not found",
    )


async def resolve_file(filename: str):
    return await asyncio.to_thread(
        _resolve_file_sync,
        filename,
    )


# ============================================================
# NAVIDROME
# ============================================================

async def trigger_navidrome_rescan():
    settings = load_settings()

    url = (
        settings.get("navidrome_url")
        or os.getenv("NAVIDROME_URL", "")
    )

    if not url:
        return

    user = (
        settings.get("navidrome_user")
        or os.getenv("NAVIDROME_USER", "")
    )

    token = (
        settings.get("navidrome_token")
        or os.getenv("NAVIDROME_TOKEN", "")
    )

    salt = (
        settings.get("navidrome_salt")
        or os.getenv("NAVIDROME_SALT", "")
    )

    endpoint = (
        f"{url.rstrip('/')}/rest/startScan"
    )

    params = {
        "u": user,
        "t": token,
        "v": "1.16.1",
        "c": "XrobMusic",
        "f": "json",
    }

    if salt:
        params["s"] = salt

    request_url = (
        f"{endpoint}?"
        f"{urllib.parse.urlencode(params)}"
    )

    try:
        def ping():
            request = urllib.request.Request(
                request_url,
                headers={
                    "User-Agent": "XrobMusic/1.0"
                },
            )

            with urllib.request.urlopen(request, timeout=5):
                pass

        await asyncio.to_thread(ping)

    except Exception:
        pass


# ============================================================
# TASK STATUS HELPERS
# ============================================================

ACTIVE_STATUSES = {
    "queued",
    "downloading",
    "processing",
}

FINAL_STATUSES = {
    "completed",
    "cancelled",
    "error",
}


def update_task(
    task,
    *,
    status=None,
    step=None,
    percent=None,
    speed=None,
    error=None,
):
    if status is not None:
        task["status"] = status

    if step is not None:
        task["step"] = step

    if percent is not None:
        task["percent"] = max(
            0,
            min(float(percent), 100),
        )

    if speed is not None:
        task["speed"] = speed

    if error is not None:
        task["error"] = error

    task["last_updated"] = time.time() * 1000


# ============================================================
# DOWNLOAD WORKER
# ============================================================

async def download_worker():
    while True:

        task_id = await task_queue.get()

        try:
            task = TASKS.get(task_id)

            if not task:
                continue

            # IMPORTANT:
            # A queued task may have been cancelled before
            # reaching the worker.
            if (
                task.get("cancel_requested")
                or task.get("status") == "cancelled"
            ):
                update_task(
                    task,
                    status="cancelled",
                    step="Cancelled",
                )

                await notify_task_update(
                    task,
                    force_save=True,
                )

                continue

            try:
                update_task(
                    task,
                    status="downloading",
                    step="Downloading stream...",
                    percent=max(
                        float(task.get("percent", 0)),
                        0,
                    ),
                )

                await notify_task_update(
                    task,
                    force_save=True,
                )

                settings = load_settings()

                fmt = settings.get(
                    "audio_format",
                    "mp3",
                )

                quality = settings.get(
                    "audio_quality",
                    "320K",
                )

                embed_thumb = bool(
                    settings.get(
                        "embed_thumbnail",
                        True,
                    )
                )

                embed_meta = bool(
                    settings.get(
                        "embed_metadata",
                        True,
                    )
                )

                output_template = str(
                    DOWNLOAD_DIR
                    / f"{task_id}.%(ext)s"
                )

                command = [
                    "yt-dlp",
                    "--no-playlist",
                    "--newline",
                    "--progress",
                    "-x",
                    "--audio-format",
                    fmt,
                    "--audio-quality",
                    quality,
                    "-o",
                    output_template,
                ]

                if embed_thumb:
                    command.append("--embed-thumbnail")

                if embed_meta:
                    command.append("--add-metadata")

                command.append(task["url"])

                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                ACTIVE_PROCESSES[task_id] = process

                progress_regex = re.compile(
                    r"\[download\]\s+.*?(\d+(?:\.\d+)?)%"
                )

                speed_regex = re.compile(
                    r"at\s+([~0-9.]+[A-Za-z]+/s)"
                )

                eta_regex = re.compile(
                    r"ETA\s+([0-9:]+)"
                )

                while True:

                    line = await process.stdout.readline()

                    if not line:
                        break

                    line_str = line.decode(
                        "utf-8",
                        errors="ignore",
                    ).strip()

                    # Cancellation.
                    if task.get("cancel_requested"):
                        try:
                            if process.returncode is None:
                                process.terminate()
                        except Exception:
                            pass

                    # Progress.
                    pct_match = progress_regex.search(
                        line_str
                    )

                    if pct_match:
                        percent = float(
                            pct_match.group(1)
                        )

                        speed = task.get(
                            "speed",
                            "",
                        )

                        speed_match = speed_regex.search(
                            line_str
                        )

                        if speed_match:
                            speed = speed_match.group(1)

                        eta_match = eta_regex.search(
                            line_str
                        )

                        if eta_match:
                            eta = eta_match.group(1)
                            task["eta"] = eta

                        update_task(
                            task,
                            status="downloading",
                            step="Downloading...",
                            percent=percent,
                            speed=speed,
                        )

                        await notify_task_update(
                            task,
                            force_save=False,
                        )

                    elif (
                        "[ExtractAudio]" in line_str
                        or "[EmbedThumbnail]" in line_str
                        or "[Metadata]" in line_str
                    ):
                        update_task(
                            task,
                            status="processing",
                            step="Processing audio...",
                            percent=92,
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

                # ------------------------------------------------
                # CANCELLED
                # ------------------------------------------------

                if task.get("cancel_requested"):
                    await asyncio.to_thread(
                        cleanup_task_files,
                        task_id,
                    )

                    update_task(
                        task,
                        status="cancelled",
                        step="Cancelled",
                        percent=0,
                    )

                    await notify_task_update(
                        task,
                        force_save=True,
                    )

                    continue

                # ------------------------------------------------
                # PROCESS ERROR
                # ------------------------------------------------

                if process.returncode != 0:

                    stderr_data = (
                        await process.stderr.read()
                    )

                    error_text = stderr_data.decode(
                        "utf-8",
                        errors="ignore",
                    ).strip()

                    if not error_text:
                        error_text = (
                            "yt-dlp download failed."
                        )

                    await asyncio.to_thread(
                        cleanup_task_files,
                        task_id,
                    )

                    update_task(
                        task,
                        status="error",
                        step="Download failed",
                        error=error_text[-1000:],
                    )

                    await notify_task_update(
                        task,
                        force_save=True,
                    )

                    continue

                # ------------------------------------------------
                # FIND DOWNLOADED FILE
                # ------------------------------------------------

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

                    update_task(
                        task,
                        status="error",
                        step="Download failed",
                        error="Downloaded file not found.",
                    )

                    await notify_task_update(
                        task,
                        force_save=True,
                    )

                    continue

                audio_file = possible_files[0]

                ext = (
                    audio_file.suffix
                    if audio_file.suffix
                    else f".{fmt}"
                )

                # ------------------------------------------------
                # PROCESSING
                # ------------------------------------------------

                update_task(
                    task,
                    status="processing",
                    step="Cleaning metadata...",
                    percent=95,
                )

                await notify_task_update(
                    task,
                    force_save=True,
                )

                clean_title = clean_filename(
                    task.get("title", "Unknown")
                )

                clean_artist = clean_filename(
                    task.get(
                        "artist",
                        "Unknown Artist",
                    )
                )

                clean_album = clean_filename(
                    task.get(
                        "album",
                        "",
                    )
                )

                cleaned_file = (
                    DOWNLOAD_DIR
                    / f"clean_{task_id}{ext}"
                )

                # Only run FFmpeg metadata rewrite when
                # metadata embedding is enabled.
                if embed_meta:
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
                        f"artist={clean_artist}",
                        "-metadata",
                        f"album={clean_album}",
                        "-metadata",
                        "comment=",
                        "-metadata",
                        "description=",
                        "-metadata",
                        "purl=",
                        str(cleaned_file),
                    ]

                    process_clean = (
                        await asyncio.create_subprocess_exec(
                            *clean_command,
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.PIPE,
                        )
                    )

                    await process_clean.wait()

                    if (
                        process_clean.returncode == 0
                        and cleaned_file.exists()
                    ):
                        try:
                            audio_file.unlink()
                        except Exception:
                            pass

                        audio_file = cleaned_file

                # ------------------------------------------------
                # FINAL LOCATION
                # ------------------------------------------------

                if settings.get(
                    "organize_by_artist",
                    False,
                ):
                    final_dir = (
                        DOWNLOAD_DIR
                        / clean_artist
                    )

                    final_dir.mkdir(
                        parents=True,
                        exist_ok=True,
                    )

                else:
                    final_dir = DOWNLOAD_DIR

                final_name = (
                    f"{clean_title}{ext}"
                )

                final_path = (
                    final_dir
                    / final_name
                )

                # Don't use is_duplicate(clean_title)
                # here because it can find the current file
                # itself or another file with the same title.
                if final_path.exists():

                    stem = final_path.stem

                    counter = 2

                    while True:
                        candidate = (
                            final_dir
                            / f"{stem} ({counter}){ext}"
                        )

                        if not candidate.exists():
                            final_path = candidate
                            final_name = candidate.name
                            break

                        counter += 1

                # ------------------------------------------------
                # FINAL MOVE
                # ------------------------------------------------

                shutil.move(
                    str(audio_file),
                    str(final_path),
                )

                task["final_name"] = str(
                    final_path.relative_to(
                        DOWNLOAD_DIR
                    )
                )

                update_task(
                    task,
                    status="completed",
                    step="Ready",
                    percent=100,
                    speed="",
                )

                await notify_task_update(
                    task,
                    force_save=True,
                )

                # Navidrome scan after successful download.
                await trigger_navidrome_rescan()

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

                update_task(
                    task,
                    status="error",
                    step="Error",
                    error=str(error)[-1000:],
                )

                await notify_task_update(
                    task,
                    force_save=True,
                )

        finally:
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

    # Recover interrupted downloads after restart.
    for task in TASKS.values():

        if task.get("status") in {
            "downloading",
            "processing",
        }:
            task["status"] = "queued"
            task["step"] = "Waiting in queue..."
            task["cancel_requested"] = False
            task["error"] = ""

        if task.get("status") == "queued":
            task["cancel_requested"] = False

            await task_queue.put(
                task["id"]
            )

            await db_save_task(
                task,
                force=True,
            )

    # Start workers.
    for _ in range(
        MAX_CONCURRENT_DOWNLOADS
    ):
        worker = asyncio.create_task(
            download_worker()
        )

        WORKERS.append(worker)


# ============================================================
# WEBSOCKET ENDPOINT
# ============================================================

@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
):
    await manager.connect(websocket)

    try:
        while True:
            await websocket.receive_text()

    except WebSocketDisconnect:
        manager.disconnect(websocket)

    except Exception:
        manager.disconnect(websocket)


# ============================================================
# YOUTUBE SEARCH
# ============================================================

async def youtube_search(
    query: str,
    max_results: int,
    page: int = 1,
):
    max_results = max(
        1,
        min(int(max_results), 100),
    )

    page = max(
        1,
        int(page),
    )

    start_idx = (
        (page - 1) * max_results
    ) + 1

    end_idx = (
        page * max_results
    )

    command = [
        "yt-dlp",
        "--flat-playlist",
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
        "--match-filter",
        "duration > 0",
        "--playlist-start",
        str(start_idx),
        "--playlist-end",
        str(end_idx),
        f"ytsearch{end_idx}:{query}",
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = (
            await process.communicate()
        )

        if process.returncode != 0:

            error = stderr.decode(
                "utf-8",
                errors="ignore",
            )

            raise RuntimeError(
                error[-2000:]
            )

        raw = stdout.decode(
            "utf-8",
            errors="ignore",
        ).strip()

        if not raw:
            return []

        data = json.loads(raw)

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

            channel = (
                item.get("channel")
                or item.get("uploader")
                or "Unknown Artist"
            )

            duration = (
                item.get("duration")
                or 0
            )

            title = (
                item.get("title")
                or "Unknown"
            )

            thumbnail = (
                item.get("thumbnail")
                or
                f"https://i.ytimg.com/vi/"
                f"{video_id}/hqdefault.jpg"
            )

            results.append(
                {
                    "id": video_id,
                    "title": title,
                    "channel": channel,
                    "duration": duration,
                    "duration_text": format_duration(
                        duration
                    ),
                    "thumbnail": thumbnail,
                    "url": (
                        "https://www.youtube.com/"
                        f"watch?v={video_id}"
                    ),
                }
            )

        return results

    except FileNotFoundError:
        raise RuntimeError(
            "yt-dlp is not installed."
        )

    except json.JSONDecodeError:
        raise RuntimeError(
            "YouTube returned invalid search data."
        )


# ============================================================
# HOME
# ============================================================

@app.get("/")
async def home():
    return FileResponse(
        STATIC_DIR / "index.html"
    )


# ============================================================
# SETTINGS API
# ============================================================

@app.get("/api/settings")
async def get_settings():
    return load_settings()


@app.post("/api/settings")
async def update_settings(
    data: dict = Body(...),
):
    return save_settings(data)


# ============================================================
# LIBRARY API
# ============================================================

@app.get("/api/library")
async def get_library():

    audio_files = (
        await get_all_audio_files()
    )

    def build():

        files = []

        total_bytes = 0

        for path in audio_files:

            try:
                size = path.stat().st_size
            except OSError:
                continue

            total_bytes += size

            files.append(
                {
                    "name": str(
                        path.relative_to(
                            DOWNLOAD_DIR
                        )
                    ),
                    "size": format_size(
                        size
                    ),
                    "bytes": size,
                }
            )

        files.sort(
            key=lambda x: x["name"].lower()
        )

        return files, total_bytes

    files, total_bytes = (
        await asyncio.to_thread(build)
    )

    return {
        "files": files,
        "total_size": format_size(
            total_bytes
        ),
        "total_bytes": total_bytes,
    }


@app.get("/api/stats")
async def get_stats():

    files = (
        await get_all_audio_files()
    )

    def build():

        total_bytes = 0
        artists = set()

        for path in files:

            try:
                total_bytes += path.stat().st_size
            except OSError:
                pass

            relative = path.relative_to(
                DOWNLOAD_DIR
            )

            if len(relative.parts) > 1:
                artists.add(
                    relative.parts[0]
                )

        return (
            len(files),
            len(artists),
            total_bytes,
        )

    tracks, artists, total_bytes = (
        await asyncio.to_thread(build)
    )

    return {
        "tracks": tracks,
        "artists": artists,
        "albums": 0,
        "total_size": format_size(
            total_bytes
        ),
    }


# ============================================================
# STREAM
# ============================================================

@app.get(
    "/api/library/stream/{filename:path}"
)
async def stream_library_file(
    filename: str,
    transcode: bool = Query(False),
):

    file_path = await resolve_file(
        filename
    )

    if transcode:

        async def transcode_generator():

            process = (
                await asyncio.create_subprocess_exec(
                    "ffmpeg",
                    "-i",
                    str(file_path),
                    "-vn",
                    "-ab",
                    "192k",
                    "-f",
                    "mp3",
                    "pipe:1",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            )

            try:
                while True:

                    chunk = await process.stdout.read(
                        65536
                    )

                    if not chunk:
                        break

                    yield chunk

            finally:
                if process.returncode is None:
                    try:
                        process.terminate()
                    except Exception:
                        pass

                await process.wait()

        return StreamingResponse(
            transcode_generator(),
            media_type="audio/mpeg",
        )

    extension = (
        file_path.suffix.lower()
    )

    media_type = (
        MEDIA_TYPES.get(extension)
        or mimetypes.guess_type(
            file_path.name
        )[0]
        or "audio/mpeg"
    )

    return FileResponse(
        path=file_path,
        filename=file_path.name,
        media_type=media_type,
    )


# ============================================================
# COVER
# ============================================================

@app.get(
    "/api/library/cover/{filename:path}"
)
async def get_library_cover(
    filename: str,
):

    file_path = await resolve_file(
        filename
    )

    cache_key = (
        hashlib.md5(
            str(
                file_path.relative_to(
                    DOWNLOAD_DIR
                )
            ).encode()
        ).hexdigest()
        + ".jpg"
    )

    cache_path = (
        COVER_CACHE_DIR / cache_key
    )

    if cache_path.exists():
        return FileResponse(
            cache_path,
            media_type="image/jpeg",
        )

    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(file_path),
        "-an",
        "-c:v",
        "mjpeg",
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-",
    ]

    try:

        process = (
            await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        )

        stdout, _ = (
            await process.communicate()
        )

        if (
            process.returncode == 0
            and stdout
        ):

            await asyncio.to_thread(
                cache_path.write_bytes,
                stdout,
            )

            return Response(
                content=stdout,
                media_type="image/jpeg",
            )

    except Exception:
        pass

    svg_placeholder = """
    <svg xmlns="http://www.w3.org/2000/svg"
         width="110"
         height="65"
         viewBox="0 0 110 65">
        <rect width="100%"
              height="100%"
              fill="#1e293b"/>
        <text x="50%"
              y="50%"
              fill="#9ca3af"
              font-size="20"
              text-anchor="middle"
              dominant-baseline="central">
            🎵
        </text>
    </svg>
    """

    return Response(
        content=svg_placeholder,
        media_type="image/svg+xml",
    )


# ============================================================
# DELETE LIBRARY FILE
# ============================================================

@app.delete(
    "/api/library/{filename:path}"
)
async def delete_library_file(
    filename: str,
):

    file_path = await resolve_file(
        filename
    )

    cache_key = (
        hashlib.md5(
            str(
                file_path.relative_to(
                    DOWNLOAD_DIR
                )
            ).encode()
        ).hexdigest()
        + ".jpg"
    )

    cache_path = (
        COVER_CACHE_DIR / cache_key
    )

    try:
        if cache_path.exists():
            cache_path.unlink()
    except Exception:
        pass

    file_path.unlink()

    await trigger_navidrome_rescan()

    return {
        "status": "deleted"
    }


# ============================================================
# SEARCH
# ============================================================

@app.get("/api/search")
async def search(
    q: str = Query(
        ...,
        min_length=1,
    ),
    page: int = Query(
        1,
        ge=1,
    ),
):

    settings = load_settings()

    try:

        results = await youtube_search(
            q.strip(),
            settings.get(
                "max_results",
                20,
            ),
            page,
        )

        return results

    except Exception as error:

        raise HTTPException(
            status_code=500,
            detail=(
                "YouTube search failed: "
                + str(error)
            ),
        )


# ============================================================
# PREVIEW
# ============================================================

@app.get("/api/preview")
async def preview_audio(
    url: str = Query(
        ...,
        min_length=1,
    ),
):

    url = url.strip()

    if not url.startswith("http"):
        url = (
            "https://www.youtube.com/"
            f"watch?v={url}"
        )

    parsed = urllib.parse.urlparse(url)

    allowed_hosts = {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "youtu.be",
        "www.youtu.be",
    }

    hostname = (
        parsed.hostname or ""
    ).lower()

    if hostname not in allowed_hosts:
        raise HTTPException(
            status_code=400,
            detail="Invalid YouTube URL.",
        )

    try:

        process = (
            await asyncio.create_subprocess_exec(
                "yt-dlp",
                "-g",
                "-f",
                "ba/b",
                "--no-playlist",
                url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        )

        stdout, stderr = (
            await process.communicate()
        )

        if process.returncode != 0:

            error = stderr.decode(
                "utf-8",
                errors="ignore",
            )

            raise RuntimeError(
                error[-500:]
                or "Failed to extract preview stream."
            )

        stream_url = (
            stdout.decode(
                "utf-8",
                errors="ignore",
            )
            .strip()
            .splitlines()[0]
            if stdout
            else ""
        )

        if not stream_url:
            raise RuntimeError(
                "Could not retrieve audio stream."
            )

        return RedirectResponse(
            url=stream_url
        )

    except HTTPException:
        raise

    except Exception as error:

        raise HTTPException(
            status_code=500,
            detail=str(error),
        )


# ============================================================
# DOWNLOAD QUEUE
# ============================================================

@app.post("/api/download")
async def enqueue_download(
    payload: dict = Body(...),
):

    url = str(
        payload.get("url") or ""
    ).strip()

    title = str(
        payload.get(
            "title",
            "Unknown",
        )
    ).strip()

    artist = str(
        payload.get(
            "artist",
            "Unknown Artist",
        )
    ).strip()

    album = str(
        payload.get(
            "album",
            "",
        )
    ).strip()

    element_id = str(
        payload.get(
            "elementId",
            "",
        )
    ).strip()

    if not url:
        raise HTTPException(
            status_code=400,
            detail="Missing URL.",
        )

    # Validate URL before creating task.
    parsed = urllib.parse.urlparse(url)

    if parsed.scheme not in {
        "http",
        "https",
    }:
        raise HTTPException(
            status_code=400,
            detail="Invalid URL.",
        )

    # Critical lock:
    # prevents two simultaneous requests from
    # downloading the same song.
    async with DOWNLOAD_LOCK:

        # Existing library.
        if await is_duplicate_with_artist(
            title,
            artist,
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "This track already "
                    "exists in your library."
                ),
            )

        # Existing queued/active tasks.
        requested_key = normalize_duplicate_key(
            title
        )

        for existing in TASKS.values():

            if existing.get("status") not in {
                "queued",
                "downloading",
                "processing",
            }:
                continue

            existing_key = (
                normalize_duplicate_key(
                    existing.get(
                        "title",
                        "",
                    )
                )
            )

            if (
                existing_key
                and existing_key == requested_key
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "This track is "
                        "already in the queue."
                    ),
                )

        task_id = uuid.uuid4().hex

        task_info = {
            "id": task_id,
            "title": title,
            "artist": artist,
            "album": album,
            "url": url,
            "elementId": element_id,
            "status": "queued",
            "percent": 0,
            "speed": "",
            "eta": "",
            "step": "Waiting in queue...",
            "error": "",
            "final_name": "",
            "cancel_requested": False,
            "created_at": time.time() * 1000,
            "last_updated": time.time() * 1000,
        }

        TASKS[task_id] = task_info

        await notify_task_update(
            task_info,
            force_save=True,
        )

        await task_queue.put(
            task_id
        )

    return {
        "status": "ok",
        "task_id": task_id,
        "queue_position": task_queue.qsize(),
    }


# ============================================================
# CANCEL
# ============================================================

@app.post(
    "/api/tasks/{task_id}/cancel"
)
async def cancel_task(
    task_id: str,
):

    task = TASKS.get(task_id)

    if not task:
        raise HTTPException(
            status_code=404,
            detail="Task not found.",
        )

    if task["status"] in {
        "completed",
        "cancelled",
    }:
        return {
            "status": task["status"]
        }

    task["cancel_requested"] = True

    process = ACTIVE_PROCESSES.get(
        task_id
    )

    if (
        process
        and process.returncode is None
    ):
        try:
            process.terminate()
        except Exception:
            pass

    update_task(
        task,
        status="cancelled",
        step="Cancelled",
        percent=0,
    )

    await notify_task_update(
        task,
        force_save=True,
    )

    return {
        "status": "cancelled"
    }


# ============================================================
# TASKS
# ============================================================

@app.get("/api/tasks")
async def get_tasks():

    status_weight = {
        "downloading": 0,
        "processing": 1,
        "queued": 2,
        "completed": 3,
        "error": 4,
        "cancelled": 5,
    }

    tasks = list(
        TASKS.values()
    )

    tasks.sort(
        key=lambda task: (
            status_weight.get(
                task.get(
                    "status",
                    "",
                ),
                99,
            ),
            -float(
                task.get(
                    "created_at",
                    0,
                )
                or 0
            ),
        )
    )

    return [
        dict(task)
        for task in tasks
    ]


# ============================================================
# RETRY
# ============================================================

@app.post(
    "/api/tasks/{task_id}/retry"
)
async def retry_task(
    task_id: str,
):

    original = TASKS.get(task_id)

    if not original:
        raise HTTPException(
            status_code=404,
            detail="Task not found.",
        )

    if original.get("status") not in {
        "error",
        "cancelled",
    }:
        raise HTTPException(
            status_code=409,
            detail=(
                "Only failed or "
                "cancelled tasks can be retried."
            ),
        )

    if await is_duplicate_with_artist(
        original.get("title", ""),
        original.get("artist", ""),
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "This track already "
                "exists in your library."
            ),
        )

    new_id = uuid.uuid4().hex

    task = {
        "id": new_id,
        "title": original.get(
            "title",
            "Unknown",
        ),
        "artist": original.get(
            "artist",
            "Unknown Artist",
        ),
        "album": original.get(
            "album",
            "",
        ),
        "url": original.get(
            "url",
            "",
        ),
        "elementId": original.get(
            "elementId",
            "",
        ),
        "status": "queued",
        "percent": 0,
        "speed": "",
        "eta": "",
        "step": "Waiting in queue...",
        "error": "",
        "final_name": "",
        "cancel_requested": False,
        "created_at": time.time() * 1000,
        "last_updated": time.time() * 1000,
    }

    TASKS[new_id] = task

    await notify_task_update(
        task,
        force_save=True,
    )

    await task_queue.put(
        new_id
    )

    return {
        "status": "ok",
        "task_id": new_id,
    }


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
async def health():
    active = sum(
        1
        for task in TASKS.values()
        if task.get("status")
        in {
            "downloading",
            "processing",
        }
    )

    queued = sum(
        1
        for task in TASKS.values()
        if task.get("status")
        == "queued"
    )

    return {
        "status": "ok",
        "service": "music-downloader",
        "workers": MAX_CONCURRENT_DOWNLOADS,
        "active_downloads": active,
        "queued_downloads": queued,
        "queue_size": task_queue.qsize(),
    }
