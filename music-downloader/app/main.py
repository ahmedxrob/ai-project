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

from fastapi import (
    FastAPI,
    Query,
    HTTPException,
    Body,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles


# ============================================================
# APP SETUP
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
# PATHS / CONFIG
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


SETTINGS_FILE = (
    DOWNLOAD_DIR / ".settings.json"
)

DB_FILE = (
    DOWNLOAD_DIR / "tasks.db"
)


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


MAX_CONCURRENT_DOWNLOADS = 3


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
    "navidrome_url": os.getenv(
        "NAVIDROME_URL",
        "",
    ),
    "navidrome_user": os.getenv(
        "NAVIDROME_USER",
        "",
    ),
    "navidrome_token": os.getenv(
        "NAVIDROME_TOKEN",
        "",
    ),
    "navidrome_salt": os.getenv(
        "NAVIDROME_SALT",
        "",
    ),
}


# ============================================================
# RUNTIME STATE
# ============================================================

TASKS = {}

task_queue = asyncio.Queue()

ACTIVE_PROCESSES = {}

LAST_SAVED_TIME = {}


# ============================================================
# PERSISTENT TASK STORAGE
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
    task: dict,
    force: bool = False,
):

    task_id = task.get("id")

    now = time.time()

    last_saved = LAST_SAVED_TIME.get(
        task_id,
        0,
    )

    if (
        force
        or now - last_saved > 0.5
    ):

        LAST_SAVED_TIME[task_id] = now

        await asyncio.to_thread(
            _db_save_task_sync,
            task,
        )


def _db_load_tasks_sync() -> dict:

    if not DB_FILE.exists():
        return {}

    tasks = {}

    with sqlite3.connect(DB_FILE) as conn:

        conn.row_factory = sqlite3.Row

        cursor = conn.cursor()

        cursor.execute(
            "SELECT * FROM tasks"
        )

        for row in cursor.fetchall():

            task = dict(row)

            tasks[task["id"]] = task

    return tasks


def _db_clear_completed_tasks_sync():

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


def _db_delete_task_sync(
    task_id: str,
):

    with sqlite3.connect(DB_FILE) as conn:

        conn.execute(
            """
            DELETE FROM tasks
            WHERE id = ?
            """,
            (task_id,),
        )

        conn.commit()


# ============================================================
# WEBSOCKET CONNECTION MANAGER
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

                self.disconnect(
                    connection
                )


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
# NAVIDROME
# ============================================================

async def trigger_navidrome_rescan():

    settings = load_settings()

    url = (
        settings.get("navidrome_url")
        or os.getenv(
            "NAVIDROME_URL",
            "",
        )
    )

    if not url:
        return

    user = (
        settings.get("navidrome_user")
        or os.getenv(
            "NAVIDROME_USER",
            "",
        )
    )

    token = (
        settings.get("navidrome_token")
        or os.getenv(
            "NAVIDROME_TOKEN",
            "",
        )
    )

    salt = (
        settings.get("navidrome_salt")
        or os.getenv(
            "NAVIDROME_SALT",
            "",
        )
    )

    endpoint = (
        f"{url.rstrip('/')}"
        "/rest/startScan"
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

    req_url = (
        f"{endpoint}?"
        f"{urllib.parse.urlencode(params)}"
    )

    try:

        def _ping():

            req = urllib.request.Request(
                req_url,
                headers={
                    "User-Agent":
                        "XrobMusic/1.0"
                },
            )

            with urllib.request.urlopen(
                req,
                timeout=5,
            ):
                pass

        await asyncio.to_thread(
            _ping
        )

    except Exception:
        pass


# ============================================================
# HELPERS
# ============================================================

def normalize_duplicate_key(
    value: str,
) -> str:

    value = Path(
        value or ""
    ).stem.lower()

    value = re.sub(
        r"\b(official\s*(video|audio|music video)|lyrics?|hd|4k|remaster(ed)?|audio)\b",
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


def _get_all_audio_files_sync():

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
        _get_all_audio_files_sync
    )


def _is_duplicate_sync(
    title: str,
) -> bool:

    key = normalize_duplicate_key(
        title
    )

    files = _get_all_audio_files_sync()

    return any(
        normalize_duplicate_key(
            p.name
        ) == key
        for p in files
    )


async def is_duplicate(
    title: str,
) -> bool:

    return await asyncio.to_thread(
        _is_duplicate_sync,
        title,
    )


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


def save_settings(
    data: dict,
):

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


def clean_filename(
    value: str,
) -> str:

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
    ).strip(
        " ."
    )

    return (
        value[:180]
        if value
        else "Unknown"
    )


def format_duration(
    seconds,
):

    try:

        seconds = int(
            seconds or 0
        )

        minutes = seconds // 60

        seconds = seconds % 60

        return (
            f"{minutes}:"
            f"{seconds:02d}"
        )

    except Exception:

        return "0:00"


def format_size(
    size_bytes,
):

    try:

        if (
            size_bytes
            >= 1024 * 1024 * 1024
        ):

            gb = (
                size_bytes
                / (
                    1024
                    * 1024
                    * 1024
                )
            )

            return f"{gb:.2f} GB"

        mb = (
            size_bytes
            / (
                1024
                * 1024
            )
        )

        return f"{mb:.1f} MB"

    except Exception:

        return "0 MB"


def cleanup_task_files(
    task_id: str,
):

    patterns = [
        f"*{task_id}*",
        f"clean_{task_id}*",
    ]

    for pattern in patterns:

        for p in DOWNLOAD_DIR.glob(
            pattern
        ):

            try:

                if p.is_file():
                    p.unlink()

            except Exception:
                pass


def _resolve_file_sync(
    filename: str,
) -> Path:

    clean_name = filename.strip()

    base_dir = (
        DOWNLOAD_DIR.resolve()
    )

    file_path = (
        DOWNLOAD_DIR
        / clean_name
    ).resolve()

    try:

        if not file_path.is_relative_to(
            base_dir
        ):

            raise HTTPException(
                status_code=403,
                detail="Access denied",
            )

    except AttributeError:

        if (
            base_dir
            not in file_path.parents
            and file_path != base_dir
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


    target_name = Path(
        clean_name
    ).name


    for match in DOWNLOAD_DIR.rglob(
        "*"
    ):

        if (
            match.is_file()
            and match.name == target_name
        ):

            try:

                if match.resolve().is_relative_to(
                    base_dir
                ):
                    return match

            except AttributeError:

                if (
                    base_dir
                    in match.resolve().parents
                ):
                    return match


    raise HTTPException(
        status_code=404,
        detail="File not found",
    )


async def resolve_file(
    filename: str,
) -> Path:

    return await asyncio.to_thread(
        _resolve_file_sync,
        filename,
    )


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


            # ------------------------------------------------
            # IMPORTANT:
            # A queued task may have been cancelled before
            # a worker receives it.
            # ------------------------------------------------

            if (
                task.get("cancel_requested")
                or task.get("status")
                in {
                    "cancelled",
                    "canceled",
                }
            ):

                task["status"] = "cancelled"

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


            # ------------------------------------------------
            # START DOWNLOAD
            # ------------------------------------------------

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


            settings = load_settings()

            fmt = settings.get(
                "audio_format",
                "mp3",
            )

            quality = settings.get(
                "audio_quality",
                "320K",
            )

            embed_thumb = settings.get(
                "embed_thumbnail",
                True,
            )

            embed_meta = settings.get(
                "embed_metadata",
                True,
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


            if embed_thumb:

                command.append(
                    "--embed-thumbnail"
                )


            if embed_meta:

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


            progress_regex = re.compile(
                r"\[download\]\s+~?\s*(\d+(?:\.\d+)?)%"
            )

            speed_regex = re.compile(
                r"at\s+([~0-9a-zA-Z\.\/]+)"
            )


            # ------------------------------------------------
            # PROCESS OUTPUT
            # ------------------------------------------------

            while True:

                line = (
                    await process.stdout.readline()
                )

                if not line:
                    break


                line_str = line.decode(
                    "utf-8",
                    errors="ignore",
                ).strip()


                pct_match = (
                    progress_regex.search(
                        line_str
                    )
                )


                if pct_match:

                    task["percent"] = float(
                        pct_match.group(1)
                    )

                    task["last_updated"] = (
                        time.time() * 1000
                    )


                    speed_match = (
                        speed_regex.search(
                            line_str
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


                    await notify_task_update(
                        task,
                        force_save=False,
                    )


                elif any(
                    marker in line_str
                    for marker in [
                        "[ExtractAudio]",
                        "[EmbedThumbnail]",
                        "[Metadata]",
                        "[Fixup]"
                    ]
                ):

                    task["status"] = (
                        "processing"
                    )

                    task["step"] = (
                        "Embedding cover art & tags..."
                    )

                    task["percent"] = 92

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


            # ------------------------------------------------
            # CANCELLED DOWNLOAD
            # ------------------------------------------------

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


            # ------------------------------------------------
            # DOWNLOAD FAILED
            # ------------------------------------------------

            if process.returncode != 0:

                stderr_data = (
                    await process.stderr.read()
                )

                err_text = (
                    stderr_data.decode(
                        "utf-8",
                        errors="ignore",
                    )
                )


                await asyncio.to_thread(
                    cleanup_task_files,
                    task_id,
                )


                task["status"] = "error"

                task["error"] = (
                    err_text[-1000:]
                    or
                    "yt-dlp download failed."
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


            # ------------------------------------------------
            # FIND DOWNLOADED FILE
            # ------------------------------------------------

            possible_files = [
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


            if not possible_files:

                task["status"] = (
                    "error"
                )

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


            audio_file = (
                possible_files[0]
            )


            ext = (
                audio_file.suffix
                if audio_file.suffix
                else f".{fmt}"
            )


            # ------------------------------------------------
            # PROCESSING
            # ------------------------------------------------

            task["status"] = (
                "processing"
            )

            task["step"] = (
                "Cleaning tags & metadata..."
            )

            task["percent"] = 96

            task["last_updated"] = (
                time.time() * 1000
            )

            await notify_task_update(
                task,
                force_save=True,
            )


            clean_title = clean_filename(
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
                f"album={clean_title}",
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
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            )


            clean_stdout, clean_stderr = (
                await process_clean.communicate()
            )


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
            # FALLBACK IF FFMPEG PROCESSING FAILED
            # ------------------------------------------------

            if (
                process_clean.returncode != 0
                and not audio_file.exists()
            ):

                task["status"] = (
                    "error"
                )

                task["error"] = (
                    clean_stderr.decode(
                        "utf-8",
                        errors="ignore",
                    )[-1000:]
                    or
                    "Audio processing failed."
                )

                task["step"] = (
                    "Processing failed"
                )

                task["last_updated"] = (
                    time.time() * 1000
                )

                await notify_task_update(
                    task,
                    force_save=True,
                )

                continue


            # ------------------------------------------------
            # FINAL DESTINATION
            # ------------------------------------------------

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

                final_dir = (
                    DOWNLOAD_DIR
                )


            final_name = (
                f"{clean_title}{ext}"
            )


            final_path = (
                final_dir / final_name
            )


            # ------------------------------------------------
            # DUPLICATE HANDLING
            # ------------------------------------------------

            if (
                final_path.exists()
                or await is_duplicate(
                    clean_title
                )
            ):

                final_name = (
                    f"{clean_title}_"
                    f"{task_id[:4]}"
                    f"{ext}"
                )

                final_path = (
                    final_dir / final_name
                )


            # ------------------------------------------------
            # MOVE FINAL FILE
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


            # ------------------------------------------------
            # COMPLETE
            # ------------------------------------------------

            task["status"] = (
                "completed"
            )

            task["percent"] = 100

            task["speed"] = ""

            task["step"] = (
                "Ready"
            )

            task["error"] = ""

            task["last_updated"] = (
                time.time() * 1000
            )


            await notify_task_update(
                task,
                force_save=True,
            )


            # Trigger Navidrome scan

            await trigger_navidrome_rescan()


        except asyncio.CancelledError:

            raise


        except Exception as err:

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

                task["status"] = (
                    "error"
                )

                task["error"] = (
                    str(err)
                )

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


    # Reset stale active tasks from a previous
    # application/container restart.

    now_ms = (
        time.time() * 1000
    )


    stale_changed = False


    for task in TASKS.values():

        if task.get("status") in {
            "downloading",
            "processing",
        }:

            task["status"] = (
                "queued"
            )

            task["step"] = (
                "Recovered after restart"
            )

            task["percent"] = min(
                float(
                    task.get(
                        "percent",
                        0,
                    )
                    or 0
                ),
                90,
            )

            task["error"] = ""

            task["last_updated"] = (
                now_ms
            )

            stale_changed = True


    if stale_changed:

        for task in TASKS.values():

            await db_save_task(
                task,
                force=True,
            )


    # Start workers.

    for _ in range(
        MAX_CONCURRENT_DOWNLOADS
    ):

        asyncio.create_task(
            download_worker()
        )


    # Requeue tasks that were queued when
    # the application previously stopped.

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
    query: str,
    max_results: int,
    page: int = 1,
):

    start_idx = (
        (page - 1)
        * max_results
        + 1
    )

    end_idx = (
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
        str(start_idx),
        "--playlist-end",
        str(end_idx),
        f"ytsearch{end_idx}:{query}",
    ]


    try:

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

            error = stderr.decode(
                "utf-8",
                errors="ignore",
            )

            raise RuntimeError(
                error[-2000:]
                or "YouTube search failed."
            )


        data = json.loads(
            stdout.decode(
                "utf-8",
                errors="ignore",
            )
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


            channel = (
                item.get("channel")
                or item.get("uploader")
                or "Unknown Artist"
            )


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
                    "channel": channel,
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
                        (
                            "https://i.ytimg.com/"
                            f"vi/{video_id}/"
                            "hqdefault.jpg"
                        ),
                    "url":
                        (
                            "https://www.youtube.com/"
                            "watch?v="
                            f"{video_id}"
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
# FRONTEND
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

    return save_settings(
        data
    )


# ============================================================
# SEARCH API
# ============================================================

@app.get("/api/search")
async def search_endpoint(
    q: str = Query(...),
    page: int = Query(1),
):

    if not q.strip():
        return []


    settings = load_settings()


    max_results = int(
        settings.get(
            "max_results",
            20,
        )
        or 20
    )


    max_results = max(
        5,
        min(
            max_results,
            50,
        ),
    )


    try:

        results = await youtube_search(
            q,
            max_results=max_results,
            page=max(
                1,
                page,
            ),
        )

        return results

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=str(e),
        )


# ============================================================
# PREVIEW API
# ============================================================

@app.get("/api/preview")
async def preview_endpoint(
    url: str = Query(...),
):

    if not url:

        raise HTTPException(
            status_code=400,
            detail="URL missing",
        )


    try:

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

            err = stderr.decode(
                "utf-8",
                errors="ignore",
            )

            raise HTTPException(
                status_code=500,
                detail=(
                    "Failed to fetch preview audio URL"
                    if not err
                    else err[-500:]
                ),
            )


        direct_url = (
            stdout
            .decode(
                "utf-8",
                errors="ignore",
            )
            .strip()
            .split("\n")[0]
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


        async def stream_generator():

            try:

                while True:

                    chunk = (
                        await ffmpeg_proc.stdout.read(
                            64 * 1024
                        )
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
            stream_generator(),
            media_type="audio/mpeg",
            headers={
                "Access-Control-Allow-Origin":
                    "*",
                "Cache-Control":
                    "no-cache",
            },
        )


    except HTTPException:

        raise


    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=str(e),
        )


# ============================================================
# DOWNLOAD API
# ============================================================

@app.post("/api/download")
async def enqueue_download(
    payload: dict = Body(...),
):

    url = payload.get(
        "url"
    )

    title = payload.get(
        "title",
        "Unknown Track",
    )

    element_id = payload.get(
        "elementId",
        "",
    )

    artist = payload.get(
        "artist",
        "Unknown Artist",
    )


    if not url:

        raise HTTPException(
            status_code=400,
            detail="Missing URL",
        )


    title = str(
        title or "Unknown Track"
    )

    artist = str(
        artist or "Unknown Artist"
    )

    element_id = str(
        element_id or ""
    )


    # Prevent accidentally queueing the exact same
    # YouTube item multiple times while it is already
    # active.

    for existing in TASKS.values():

        if (
            existing.get("url") == url
            and existing.get("status")
            in {
                "queued",
                "downloading",
                "processing",
            }
        ):

            return {
                "status": "already_queued",
                "task_id":
                    existing.get("id"),
            }


    task_id = (
        str(uuid.uuid4())
        .replace("-", "")
        [:12]
    )


    task = {
        "id": task_id,
        "title": title,
        "artist": artist,
        "album": title,
        "url": url,
        "elementId": element_id,
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


# ============================================================
# TASK LIST
# ============================================================

@app.get("/api/tasks")
async def get_tasks():

    # Newest / active-friendly ordering.
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
            -float(
                t.get(
                    "last_updated",
                    0,
                )
                or 0
            ),
        )
    )

    return tasks


# ============================================================
# CANCEL TASK
# ============================================================

@app.post(
    "/api/tasks/{task_id}/cancel"
)
async def cancel_task(
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


    status = task.get(
        "status"
    )


    if status in {
        "completed",
        "cancelled",
        "canceled",
        "error",
        "failed",
    }:

        return {
            "status":
                status,
            "task_id":
                task_id,
        }


    task["cancel_requested"] = True


    if task_id in ACTIVE_PROCESSES:

        proc = ACTIVE_PROCESSES[
            task_id
        ]

        try:

            proc.terminate()

        except Exception:

            try:
                proc.kill()
            except Exception:
                pass


    task["status"] = (
        "cancelled"
    )

    task["step"] = (
        "Cancelled"
    )

    task["percent"] = (
        float(
            task.get(
                "percent",
                0,
            )
            or 0
        )
    )

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


# ============================================================
# DELETE ONE COMPLETED TASK
# ============================================================

@app.delete(
    "/api/tasks/{task_id}"
)
async def delete_task(
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


    status = task.get(
        "status"
    )


    if status in {
        "queued",
        "downloading",
        "processing",
    }:

        raise HTTPException(
            status_code=400,
            detail=(
                "Cannot remove an active "
                "download. Cancel it first."
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
# CLEAR COMPLETED
# ============================================================

@app.delete(
    "/api/tasks/clear-completed"
)
async def clear_completed_tasks():

    global TASKS


    removable_statuses = {
        "completed",
        "cancelled",
        "canceled",
        "error",
        "failed",
    }


    to_remove = [
        tid
        for tid, task
        in TASKS.items()
        if task.get("status")
        in removable_statuses
    ]


    for task_id in to_remove:

        TASKS.pop(
            task_id,
            None,
        )

        LAST_SAVED_TIME.pop(
            task_id,
            None,
        )


    await asyncio.to_thread(
        _db_clear_completed_tasks_sync
    )


    await manager.broadcast(
        {
            "type": "task_update",
            "action": "cleared",
            "count": len(
                to_remove
            ),
        }
    )


    return {
        "status": "cleared",
        "count": len(
            to_remove
        ),
    }


# ============================================================
# LIBRARY
# ============================================================

@app.get("/api/library")
async def get_library():

    audio_files = (
        await get_all_audio_files()
    )


    def _build():

        files = []

        total_bytes = 0


        for path in audio_files:

            try:

                sz = path.stat().st_size

            except Exception:

                continue


            total_bytes += sz


            files.append(
                {
                    "name":
                        str(
                            path.relative_to(
                                DOWNLOAD_DIR
                            )
                        ),
                    "size":
                        format_size(
                            sz
                        ),
                    "bytes":
                        sz,
                }
            )


        return (
            files,
            total_bytes,
        )


    files, total_bytes = (
        await asyncio.to_thread(
            _build
        )
    )


    return {
        "files":
            sorted(
                files,
                key=lambda x:
                    x["name"].lower(),
            ),
        "total_size":
            format_size(
                total_bytes
            ),
        "total_bytes":
            total_bytes,
    }


# ============================================================
# STATS
# ============================================================

@app.get("/api/stats")
async def get_stats():

    files = (
        await get_all_audio_files()
    )


    def _build():

        total_bytes = 0

        artists = set()

        albums = set()


        for p in files:

            try:

                total_bytes += (
                    p.stat().st_size
                )

            except Exception:

                continue


            rel = (
                p.relative_to(
                    DOWNLOAD_DIR
                )
            )


            parts = rel.parts


            if len(parts) > 1:

                artists.add(
                    parts[0]
                )

            else:

                artists.add(
                    "Unknown Artist"
                )


            # Keep album stats based on file
            # name as your original implementation.

            albums.add(
                p.stem
            )


        return (
            len(files),
            len(artists),
            len(albums),
            total_bytes,
        )


    (
        tracks_count,
        artists_count,
        albums_count,
        total_bytes,
    ) = await asyncio.to_thread(
        _build
    )


    return {
        "tracks":
            tracks_count,
        "artists":
            artists_count,
        "albums":
            albums_count,
        "total_bytes":
            total_bytes,
        "folder_size":
            format_size(
                total_bytes
            ),
    }


# ============================================================
# LIBRARY COVER
# ============================================================

@app.get(
    "/api/library/cover/{filename:path}"
)
async def get_library_cover(
    filename: str,
):

    try:

        file_path = (
            await resolve_file(
                filename
            )
        )

    except HTTPException:

        raise HTTPException(
            status_code=404,
            detail="File not found",
        )


    file_hash = hashlib.md5(
        str(file_path)
        .encode("utf-8")
    ).hexdigest()


    cover_path = (
        COVER_CACHE_DIR
        / f"{file_hash}.jpg"
    )


    if cover_path.exists():

        return FileResponse(
            cover_path,
            media_type="image/jpeg",
            headers={
                "Access-Control-Allow-Origin":
                    "*"
            },
        )


    def _extract_cover():

        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(file_path),
            "-an",
            "-vcodec",
            "mjpeg",
            "-vframes",
            "1",
            str(cover_path),
        ]


        try:

            import subprocess


            result = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )


            if (
                result.returncode == 0
                and cover_path.exists()
                and cover_path.stat().st_size > 0
            ):

                return cover_path


        except Exception:
            pass


        return None


    extracted = (
        await asyncio.to_thread(
            _extract_cover
        )
    )


    if (
        extracted
        and extracted.exists()
    ):

        return FileResponse(
            extracted,
            media_type="image/jpeg",
            headers={
                "Access-Control-Allow-Origin":
                    "*"
            },
        )


    svg_fallback = (
        '<svg '
        'xmlns="http://www.w3.org/2000/svg" '
        'width="110" '
        'height="110" '
        'viewBox="0 0 110 110">'
        '<rect '
        'width="100%" '
        'height="100%" '
        'fill="#1e293b"/>'
        '<text '
        'x="50%" '
        'y="50%" '
        'fill="#9ca3af" '
        'font-size="24" '
        'text-anchor="middle" '
        'dominant-baseline="central">'
        '🎵'
        '</text>'
        '</svg>'
    )


    return Response(
        content=svg_fallback,
        media_type="image/svg+xml",
        headers={
            "Access-Control-Allow-Origin":
                "*"
        },
    )


# ============================================================
# LIBRARY STREAM
# ============================================================

@app.get(
    "/api/library/stream/{filename:path}"
)
async def stream_library_file(
    filename: str,
):

    file_path = await resolve_file(
        filename
    )


    ext = (
        file_path.suffix.lower()
    )


    media_type = (
        MEDIA_TYPES.get(
            ext,
            "audio/mpeg",
        )
    )


    return FileResponse(
        file_path,
        media_type=media_type,
        headers={
            "Access-Control-Allow-Origin":
                "*",
            "Accept-Ranges":
                "bytes",
        },
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


    try:

        file_path.unlink()


        file_hash = hashlib.md5(
            str(file_path)
            .encode("utf-8")
        ).hexdigest()


        cover_path = (
            COVER_CACHE_DIR
            / f"{file_hash}.jpg"
        )


        if cover_path.exists():

            cover_path.unlink()


        return {
            "status": "deleted",
            "filename": filename,
        }


    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=(
                "Failed to delete file: "
                f"{str(e)}"
            ),
        )
