import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional

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
# XROB MUSIC
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="Xrob Music",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if STATIC_DIR.exists():
    app.mount(
        "/static",
        StaticFiles(directory=STATIC_DIR),
        name="static",
    )

# ============================================================
# DIRECTORIES
# ============================================================

DOWNLOAD_DIR = Path(
    os.getenv(
        "DOWNLOAD_DIR",
        "/share/mymusic/music",
    )
)
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

COVER_CACHE_DIR = DOWNLOAD_DIR / ".covers"
COVER_CACHE_DIR.mkdir(parents=True, exist_ok=True)

SETTINGS_FILE = DOWNLOAD_DIR / ".settings.json"
DB_FILE = DOWNLOAD_DIR / "tasks.db"

# ============================================================
# AUDIO FORMATS
# ============================================================

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
    "subsonic_user": os.getenv("SUBSONIC_USER", "admin"),
    "subsonic_password": os.getenv("SUBSONIC_PASSWORD", "changeme"),
}

# ============================================================
# GLOBAL STATE
# ============================================================

TASKS = {}
task_queue = asyncio.Queue()
ACTIVE_PROCESSES = {}
LAST_SAVED_TIME = {}
METADATA_CACHE = {}
LIBRARY_CACHE = {"files": None, "timestamp": 0}
MAX_CONCURRENT_DOWNLOADS = 3

SUBSONIC_API_VERSION = "1.16.1"
SUBSONIC_SERVER_VERSION = "2.0.0"
SUBSONIC_STATUS_OK = "ok"

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
        conn.commit()

def _db_save_task_sync(task: dict):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO tasks
            (id, title, artist, album, url, elementId, status, percent, speed, step, error, last_updated, final_name)
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

async def db_save_task(task: dict, force: bool = False):
    task_id = task.get("id")
    now = time.time()
    if force or (now - LAST_SAVED_TIME.get(task_id, 0) > 0.5):
        LAST_SAVED_TIME[task_id] = now
        await asyncio.to_thread(_db_save_task_sync, task)

def _db_load_tasks_sync():
    if not DB_FILE.exists():
        return {}
    tasks = {}
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tasks")
        for row in cursor.fetchall():
            task = dict(row)
            tasks[task["id"]] = task
    return tasks

def _db_clear_completed_tasks_sync():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("DELETE FROM tasks WHERE status IN ('completed', 'cancelled', 'error')")
        conn.commit()

# ============================================================
# WEBSOCKET
# ============================================================

class ConnectionManager:
    def __init__(self):
        self.active_connections = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in list(self.active_connections):
            try:
                await connection.send_json(message)
            except Exception:
                self.disconnect(connection)

manager = ConnectionManager()

async def notify_task_update(task: dict, force_save: bool = False):
    await db_save_task(task, force=force_save)
    await manager.broadcast({"type": "task_update", "task": task})

# ============================================================
# SETTINGS
# ============================================================

def load_settings():
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {**DEFAULT_SETTINGS, **data}
        except Exception:
            pass
    return DEFAULT_SETTINGS.copy()

def save_settings(data: dict):
    settings = load_settings()
    settings.update(data)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    return settings

# ============================================================
# HELPERS
# ============================================================

def clean_filename(value: str) -> str:
    value = value or "Unknown"
    value = re.sub(r'[\\/:*?"<>|]', "", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:180] if value else "Unknown"

def format_duration(seconds):
    try:
        seconds = int(seconds or 0)
        minutes = seconds // 60
        sec = seconds % 60
        return f"{minutes}:{sec:02d}"
    except Exception:
        return "0:00"

def format_size(size_bytes):
    try:
        if size_bytes >= 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    except Exception:
        return "0 MB"

def normalize_duplicate_key(value: str):
    value = Path(value or "").stem.lower()
    value = re.sub(r"\b(official\s*(video|audio|music video)|lyrics?|hd|4k|remaster(ed)?|audio)\b", " ", value, flags=re.I)
    return re.sub(r"[^a-z0-9]+", "", value)

def _get_all_audio_files_sync():
    return [
        p for p in DOWNLOAD_DIR.rglob("*")
        if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in AUDIO_EXTENSIONS
    ]

async def get_all_audio_files():
    files = await asyncio.to_thread(_get_all_audio_files_sync)
    LIBRARY_CACHE["files"] = files
    LIBRARY_CACHE["timestamp"] = time.time()
    return files

def _is_duplicate_sync(title: str):
    key = normalize_duplicate_key(title)
    files = _get_all_audio_files_sync()
    return any(normalize_duplicate_key(p.name) == key for p in files)

async def is_duplicate(title: str):
    return await asyncio.to_thread(_is_duplicate_sync, title)

def cleanup_task_files(task_id: str):
    for p in DOWNLOAD_DIR.glob(f"*{task_id}*"):
        try:
            if p.is_file():
                p.unlink()
        except Exception:
            pass

def _resolve_file_sync(filename: str) -> Path:
    clean_name = filename.strip()
    base_dir = DOWNLOAD_DIR.resolve()
    file_path = (DOWNLOAD_DIR / clean_name).resolve()

    if not file_path.is_relative_to(base_dir):
        raise HTTPException(status_code=403, detail="Access denied")

    if file_path.exists() and file_path.is_file():
        return file_path

    target_name = Path(clean_name).name
    for match in DOWNLOAD_DIR.rglob("*"):
        if match.is_file() and match.name == target_name and match.resolve().is_relative_to(base_dir):
            return match

    raise HTTPException(status_code=404, detail="File not found")

async def resolve_file(filename: str):
    return await asyncio.to_thread(_resolve_file_sync, filename)

# ============================================================
# SUBSONIC ID HELPERS
# ============================================================

def encode_id(relative_path: str) -> str:
    raw = relative_path.encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

def decode_id(value: str) -> str:
    try:
        padding = "=" * (-len(value) % 4)
        raw = base64.urlsafe_b64decode(value + padding)
        return raw.decode("utf-8")
    except Exception:
        raise HTTPException(status_code=404, detail="Invalid music ID")

async def resolve_song_id(song_id: str):
    relative = decode_id(song_id)
    return await resolve_file(relative)

# ============================================================
# METADATA & COVER ART
# ============================================================

def _ffprobe_metadata_sync(path: Path):
    cache_key = str(path.resolve())
    cached = METADATA_CACHE.get(cache_key)

    try:
        stat = path.stat()
        if cached and cached.get("mtime") == stat.st_mtime:
            return cached
    except Exception:
        pass

    metadata = {
        "title": path.stem,
        "artist": "Unknown Artist",
        "album": "Unknown Album",
        "album_artist": "Unknown Artist",
        "genre": "",
        "year": "",
        "track": 0,
        "disc": 0,
        "duration": 0,
        "bitrate": 0,
    }

    try:
        command = [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        if result.returncode == 0:
            data = json.loads(result.stdout.decode("utf-8", errors="ignore"))
            fmt = data.get("format", {})
            tags = fmt.get("tags", {})
            normalized_tags = {str(k).lower(): v for k, v in tags.items()}

            metadata["title"] = normalized_tags.get("title") or path.stem
            metadata["artist"] = normalized_tags.get("artist") or normalized_tags.get("album_artist") or "Unknown Artist"
            metadata["album_artist"] = normalized_tags.get("album_artist") or metadata["artist"]
            metadata["album"] = normalized_tags.get("album") or "Unknown Album"
            metadata["genre"] = normalized_tags.get("genre") or ""
            metadata["year"] = normalized_tags.get("date") or normalized_tags.get("year") or ""

            try:
                metadata["track"] = int(str(normalized_tags.get("track", "0")).split("/")[0])
            except Exception:
                pass
            try:
                metadata["disc"] = int(str(normalized_tags.get("disc", "0")).split("/")[0])
            except Exception:
                pass
            try:
                metadata["duration"] = int(float(fmt.get("duration", 0)))
            except Exception:
                pass
            try:
                metadata["bitrate"] = int(fmt.get("bit_rate", 0))
            except Exception:
                pass
    except Exception:
        stem = path.stem
        if " - " in stem:
            artist, title = stem.split(" - ", 1)
            metadata["artist"] = artist.strip() or "Unknown Artist"
            metadata["title"] = title.strip() or stem

    try:
        metadata["mtime"] = path.stat().st_mtime
    except Exception:
        metadata["mtime"] = 0

    METADATA_CACHE[cache_key] = metadata
    return metadata

async def get_metadata(path: Path):
    return await asyncio.to_thread(_ffprobe_metadata_sync, path)

def cover_cache_path(file_path: Path):
    file_hash = hashlib.md5(str(file_path.resolve()).encode("utf-8")).hexdigest()
    return COVER_CACHE_DIR / f"{file_hash}.jpg"

async def extract_cover(file_path: Path):
    cover_path = cover_cache_path(file_path)
    if cover_path.exists() and cover_path.stat().st_size > 0:
        return cover_path

    def _extract():
        command = [
            "ffmpeg", "-y",
            "-i", str(file_path),
            "-an",
            "-vcodec", "mjpeg",
            "-vframes", "1",
            str(cover_path),
        ]
        try:
            res = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            if res.returncode == 0 and cover_path.exists() and cover_path.stat().st_size > 0:
                return cover_path
        except Exception:
            pass
        return None

    return await asyncio.to_thread(_extract)

# Resolve target audio file for cover art regardless of ID type (song, album, artist)
async def resolve_cover_target(id_str: str) -> Optional[Path]:
    if not id_str:
        return None
    try:
        decoded = decode_id(id_str)
        files = await get_all_audio_files()

        if decoded.startswith("artist:"):
            target_artist = decoded.split("artist:", 1)[1].lower()
            for f in files:
                meta = await get_metadata(f)
                if str(meta.get("artist", "")).lower() == target_artist:
                    return f
        elif decoded.startswith("album:"):
            parts = decoded.split(":", 2)
            if len(parts) == 3:
                _, art, alb = parts
                for f in files:
                    meta = await get_metadata(f)
                    if str(meta.get("artist", "")).lower() == art.lower() and str(meta.get("album", "")).lower() == alb.lower():
                        return f
        else:
            return await resolve_file(decoded)
    except Exception:
        pass
    return None

# ============================================================
# DOWNLOAD WORKER
# ============================================================

async def download_worker():
    while True:
        task_id = await task_queue.get()
        task = TASKS.get(task_id)
        if not task:
            task_queue.task_done()
            continue

        try:
            task["status"] = "downloading"
            task["step"] = "Downloading stream..."
            task["last_updated"] = time.time() * 1000
            await notify_task_update(task, force_save=True)

            settings = load_settings()
            fmt = settings.get("audio_format", "mp3")
            quality = settings.get("audio_quality", "320K")
            embed_thumb = settings.get("embed_thumbnail", True)
            embed_meta = settings.get("embed_metadata", True)

            output_template = str(DOWNLOAD_DIR / f"{task_id}.%(ext)s")
            command = [
                "yt-dlp",
                "--no-playlist",
                "-x",
                "--audio-format", fmt,
                "--audio-quality", quality,
                "--newline",
                "-o", output_template,
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

            progress_regex = re.compile(r"\[download\]\s+~?\s*(\d+(?:\.\d+)?)%")
            speed_regex = re.compile(r"at\s+([~0-9a-zA-Z\.\/]+)")

            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                line_str = line.decode("utf-8", errors="ignore").strip()

                pct_match = progress_regex.search(line_str)
                if pct_match:
                    task["percent"] = float(pct_match.group(1))
                    task["last_updated"] = time.time() * 1000
                    speed_match = speed_regex.search(line_str)
                    if speed_match:
                        task["speed"] = speed_match.group(1).replace("~", "")
                    await notify_task_update(task, force_save=False)
                elif any(x in line_str for x in ["[ExtractAudio]", "[EmbedThumbnail]", "[Metadata]"]):
                    task["status"] = "processing"
                    task["step"] = "Processing metadata & cover..."
                    task["percent"] = 92
                    task["last_updated"] = time.time() * 1000
                    await notify_task_update(task, force_save=True)

            await process.wait()
            ACTIVE_PROCESSES.pop(task_id, None)

            if task.get("cancel_requested"):
                await asyncio.to_thread(cleanup_task_files, task_id)
                task["status"] = "cancelled"
                task["step"] = "Cancelled"
                task["last_updated"] = time.time() * 1000
                await notify_task_update(task, force_save=True)
                continue

            if process.returncode != 0:
                stderr_data = await process.stderr.read()
                err_text = stderr_data.decode("utf-8", errors="ignore")
                await asyncio.to_thread(cleanup_task_files, task_id)
                task["status"] = "error"
                task["error"] = err_text[-1000:]
                task["last_updated"] = time.time() * 1000
                await notify_task_update(task, force_save=True)
                continue

            possible_files = [
                p for p in DOWNLOAD_DIR.glob(f"{task_id}.*")
                if p.is_file() and p.suffix.lower() not in {".part", ".ytdl", ".temp"}
            ]

            if not possible_files:
                task["status"] = "error"
                task["error"] = "Downloaded file not found."
                await notify_task_update(task, force_save=True)
                continue

            audio_file = possible_files[0]
            ext = audio_file.suffix if audio_file.suffix else f".{fmt}"

            task["status"] = "processing"
            task["step"] = "Cleaning tags & metadata..."
            task["percent"] = 96
            task["last_updated"] = time.time() * 1000
            await notify_task_update(task, force_save=True)

            clean_title = clean_filename(task["title"])
            cleaned_file = DOWNLOAD_DIR / f"clean_{task_id}{ext}"

            clean_command = [
                "ffmpeg", "-y",
                "-i", str(audio_file),
                "-map", "0",
                "-c", "copy",
                "-disposition:v:0", "attached_pic",
                "-metadata", f"title={clean_title}",
                "-metadata", f"artist={task.get('artist', 'Unknown Artist')}",
                "-metadata", f"album={task.get('album', clean_title)}",
                "-metadata", "comment=",
                "-metadata", "description=",
                "-metadata", "purl=",
                str(cleaned_file),
            ]

            process_clean = await asyncio.create_subprocess_exec(
                *clean_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await process_clean.wait()

            if process_clean.returncode == 0 and cleaned_file.exists():
                try:
                    audio_file.unlink()
                except Exception:
                    pass
                audio_file = cleaned_file

            artist = clean_filename(task.get("artist", "Unknown Artist"))
            final_dir = DOWNLOAD_DIR / artist if settings.get("organize_by_artist", False) else DOWNLOAD_DIR
            final_dir.mkdir(parents=True, exist_ok=True)

            final_name = f"{clean_title}{ext}"
            final_path = final_dir / final_name

            if final_path.exists() or await is_duplicate(clean_title):
                final_name = f"{clean_title}_{task_id[:4]}{ext}"
                final_path = final_dir / final_name

            shutil.move(str(audio_file), str(final_path))
            relative = final_path.relative_to(DOWNLOAD_DIR)

            task["final_name"] = str(relative)
            task["status"] = "completed"
            task["percent"] = 100
            task["step"] = "Ready"
            task["last_updated"] = time.time() * 1000
            await notify_task_update(task, force_save=True)

            LIBRARY_CACHE["files"] = None
            METADATA_CACHE.pop(str(final_path.resolve()), None)
            await manager.broadcast({"type": "library_updated"})

        except Exception as err:
            await asyncio.to_thread(cleanup_task_files, task_id)
            task["status"] = "error"
            task["error"] = str(err)
            task["last_updated"] = time.time() * 1000
            await notify_task_update(task, force_save=True)
        finally:
            task_queue.task_done()

# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
async def startup_event():
    await asyncio.to_thread(init_db)
    global TASKS
    TASKS = await asyncio.to_thread(_db_load_tasks_sync)
    for _ in range(MAX_CONCURRENT_DOWNLOADS):
        asyncio.create_task(download_worker())

# ============================================================
# WEBSOCKET
# ============================================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# ============================================================
# SEARCH & PREVIEW & TASKS API
# ============================================================

@app.get("/")
async def home():
    if (STATIC_DIR / "index.html").exists():
        return FileResponse(STATIC_DIR / "index.html")
    return {"message": "Xrob Music Server Running"}

@app.get("/api/settings")
async def get_settings():
    return load_settings()

@app.post("/api/settings")
async def update_settings(data: dict = Body(...)):
    return save_settings(data)

@app.get("/api/search")
async def search_endpoint(q: str = Query(...), page: int = Query(1, ge=1)):
    if not q.strip():
        return []
    settings = load_settings()
    max_results = settings.get("max_results", 20)
    start_idx = (page - 1) * max_results + 1
    end_idx = page * max_results

    command = [
        "yt-dlp",
        "--flat-playlist",
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
        "--playlist-start", str(start_idx),
        "--playlist-end", str(end_idx),
        f"ytsearch{end_idx}:{q}",
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(stderr.decode("utf-8", errors="ignore")[-2000:])

        data = json.loads(stdout.decode("utf-8", errors="ignore"))
        results = []
        for item in data.get("entries", []):
            if not item or not item.get("id"):
                continue
            video_id = item["id"]
            channel = item.get("channel") or item.get("uploader") or "Unknown Artist"
            duration = item.get("duration", 0) or 0
            results.append({
                "id": video_id,
                "title": item.get("title", "Unknown"),
                "channel": channel,
                "duration": duration,
                "duration_text": format_duration(duration),
                "thumbnail": item.get("thumbnail") or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                "url": f"https://www.youtube.com/watch?v={video_id}",
            })
        return results
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/preview")
async def preview_endpoint(url: str = Query(...)):
    if not url:
        raise HTTPException(status_code=400, detail="URL missing")
    try:
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp", "-g", "-f", "ba/bestaudio/b", url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0 or not stdout:
            raise HTTPException(status_code=500, detail="Failed to fetch preview audio URL")

        direct_url = stdout.decode().strip().split("\n")[0]
        ffmpeg_proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", direct_url, "-t", "120", "-f", "mp3", "-ab", "128k", "-",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )

        async def stream_generator():
            try:
                while True:
                    chunk = await ffmpeg_proc.stdout.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                if ffmpeg_proc.returncode is None:
                    try:
                        ffmpeg_proc.kill()
                    except Exception:
                        pass

        return StreamingResponse(
            stream_generator(),
            media_type="audio/mpeg",
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/download")
async def enqueue_download(payload: dict = Body(...)):
    url = payload.get("url")
    title = payload.get("title", "Unknown Track")
    element_id = payload.get("elementId", "")
    artist = payload.get("artist", "Unknown Artist")
    if not url:
        raise HTTPException(status_code=400, detail="Missing URL")

    task_id = str(uuid.uuid4())[:8]
    task = {
        "id": task_id,
        "title": title,
        "artist": artist,
        "album": payload.get("album", title),
        "url": url,
        "elementId": element_id,
        "status": "queued",
        "percent": 0,
        "speed": "",
        "step": "Queued...",
        "error": "",
        "last_updated": time.time() * 1000,
        "final_name": "",
    }
    TASKS[task_id] = task
    await notify_task_update(task, force_save=True)
    await task_queue.put(task_id)
    return {"status": "ok", "task_id": task_id}

@app.get("/api/tasks")
async def get_tasks():
    return list(TASKS.values())

@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    task["cancel_requested"] = True
    if task_id in ACTIVE_PROCESSES:
        try:
            ACTIVE_PROCESSES[task_id].terminate()
        except Exception:
            pass
    task["status"] = "cancelled"
    task["step"] = "Cancelled"
    task["last_updated"] = time.time() * 1000
    await notify_task_update(task, force_save=True)
    return {"status": "cancelled"}

@app.delete("/api/tasks/clear-completed")
async def clear_completed_tasks():
    global TASKS
    to_remove = [tid for tid, task in TASKS.items() if task.get("status") in ("completed", "cancelled", "error")]
    for tid in to_remove:
        TASKS.pop(tid, None)
    await asyncio.to_thread(_db_clear_completed_tasks_sync)
    await manager.broadcast({"type": "task_update"})
    return {"status": "cleared", "count": len(to_remove)}

@app.get("/api/library")
async def get_library():
    audio_files = await get_all_audio_files()
    def _build():
        files, total_bytes = [], 0
        for path in audio_files:
            try:
                sz = path.stat().st_size
                total_bytes += sz
                files.append({
                    "name": str(path.relative_to(DOWNLOAD_DIR)),
                    "size": format_size(sz),
                    "bytes": sz,
                })
            except Exception:
                continue
        return files, total_bytes

    files, total_bytes = await asyncio.to_thread(_build)
    return {
        "files": sorted(files, key=lambda x: x["name"].lower()),
        "total_size": format_size(total_bytes),
        "total_bytes": total_bytes,
    }

@app.get("/api/stats")
async def get_stats():
    files = await get_all_audio_files()
    def _build():
        total_bytes = sum(p.stat().st_size for p in files)
        artists, albums = set(), set()
        for p in files:
            meta = _ffprobe_metadata_sync(p)
            artists.add(meta.get("artist", "Unknown Artist"))
            albums.add(meta.get("album", "Unknown Album"))
        return len(files), len(artists), len(albums), total_bytes

    tracks, artists, albums, total_bytes = await asyncio.to_thread(_build)
    return {
        "tracks": tracks,
        "artists": artists,
        "albums": albums,
        "total_bytes": total_bytes,
        "folder_size": format_size(total_bytes),
    }

@app.get("/api/library/cover/{filename:path}")
async def get_library_cover(filename: str):
    file_path = await resolve_file(filename)
    cover = await extract_cover(file_path)
    if cover:
        return FileResponse(cover, media_type="image/jpeg", headers={"Access-Control-Allow-Origin": "*"})

    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300"><rect width="100%" height="100%" fill="#1e293b"/><text x="50%" y="50%" fill="#9ca3af" font-size="70" text-anchor="middle" dominant-baseline="central">♪</text></svg>"""
    return Response(content=svg, media_type="image/svg+xml", headers={"Access-Control-Allow-Origin": "*"})

@app.get("/api/library/stream/{filename:path}")
async def stream_library_file(filename: str):
    file_path = await resolve_file(filename)
    return FileResponse(
        file_path,
        media_type=MEDIA_TYPES.get(file_path.suffix.lower(), "audio/mpeg"),
        headers={"Access-Control-Allow-Origin": "*", "Accept-Ranges": "bytes"},
    )

@app.delete("/api/library/{filename:path}")
async def delete_library_file(filename: str):
    file_path = await resolve_file(filename)
    try:
        file_hash = hashlib.md5(str(file_path.resolve()).encode("utf-8")).hexdigest()
        cover_path = COVER_CACHE_DIR / f"{file_hash}.jpg"
        file_path.unlink()
        if cover_path.exists():
            cover_path.unlink()
        LIBRARY_CACHE["files"] = None
        METADATA_CACHE.pop(str(file_path.resolve()), None)
        await manager.broadcast({"type": "library_updated"})
        return {"status": "deleted", "filename": filename}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ============================================================
# SUBSONIC AUTHENTICATION & HELPERS
# ============================================================

def xml_escape(value):
    if value is None:
        return ""
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;")

def wants_json(request_format: str):
    return str(request_format or "").lower() == "json"

def subsonic_json(body: dict):
    return {
        "subsonic-response": {
            "status": SUBSONIC_STATUS_OK,
            "version": SUBSONIC_API_VERSION,
            "type": "Xrob Music",
            "serverVersion": SUBSONIC_SERVER_VERSION,
            "openSubsonic": True,
            **body,
        }
    }

def subsonic_error(code: int, message: str, request_format: str = "json"):
    if wants_json(request_format):
        return Response(
            content=json.dumps({
                "subsonic-response": {
                    "status": "failed",
                    "version": SUBSONIC_API_VERSION,
                    "error": {"code": code, "message": message},
                }
            }),
            media_type="application/json",
        )
    xml = f'<?xml version="1.0" encoding="UTF-8"?><subsonic-response status="failed" version="{xml_escape(SUBSONIC_API_VERSION)}"><error code="{code}" message="{xml_escape(message)}"/></subsonic-response>'
    return Response(content=xml, media_type="application/xml")

def xml_response(inner: str):
    xml = f'<?xml version="1.0" encoding="UTF-8"?><subsonic-response status="ok" version="{SUBSONIC_API_VERSION}">{inner}</subsonic-response>'
    return Response(content=xml, media_type="application/xml")

def verify_subsonic_auth(username: str, token: str = "", salt: str = "", password: str = ""):
    if not username:
        return False
    settings = load_settings()
    configured_user = str(settings.get("subsonic_user", "admin"))
    configured_password = str(settings.get("subsonic_password", "changeme"))

    if username.lower() != configured_user.lower():
        return False

    # Token auth (t = md5(password + salt))
    if token and salt:
        expected = hashlib.md5((configured_password + salt).encode("utf-8")).hexdigest()
        if token.lower() == expected.lower():
            return True

    # Plain / Hex password auth (p parameter)
    if password:
        if password == configured_password:
            return True
        if password.startswith("enc:"):
            try:
                decoded = bytes.fromhex(password[4:]).decode("utf-8")
                if decoded == configured_password:
                    return True
            except Exception:
                pass
        try:
            decoded = bytes.fromhex(password).decode("utf-8")
            if decoded == configured_password:
                return True
        except Exception:
            pass

    return False

def authenticate_subsonic(username: str, token: str, salt: str, password: str, request_format: str):
    if verify_subsonic_auth(username, token, salt, password):
        return None
    return subsonic_error(40, "Authentication failed", request_format)

async def song_to_subsonic(path: Path):
    metadata = await get_metadata(path)
    relative = str(path.relative_to(DOWNLOAD_DIR))
    song_id = encode_id(relative)
    stat = path.stat()

    duration = int(metadata.get("duration", 0) or 0)
    bitrate = int(metadata.get("bitrate", 0) or 0)
    if bitrate:
        bitrate = max(1, round(bitrate / 1000))

    artist = str(metadata.get("artist", "Unknown Artist"))
    album = str(metadata.get("album", "Unknown Album"))
    title = str(metadata.get("title", path.stem))
    year_val = metadata.get("year", "") or ""

    try:
        year = int(str(year_val)[:4])
    except Exception:
        year = 0

    return {
        "id": song_id,
        "parent": encode_id(str(path.parent.relative_to(DOWNLOAD_DIR))) if path.parent != DOWNLOAD_DIR else "root",
        "isDir": False,
        "title": title,
        "album": album,
        "artist": artist,
        "albumArtist": str(metadata.get("album_artist", artist)),
        "track": int(metadata.get("track", 0) or 0),
        "discNumber": int(metadata.get("disc", 0) or 0),
        "year": year,
        "genre": str(metadata.get("genre", "")),
        "coverArt": song_id,
        "duration": duration,
        "bitRate": bitrate,
        "size": stat.st_size,
        "contentType": MEDIA_TYPES.get(path.suffix.lower(), "audio/mpeg"),
        "suffix": path.suffix.lower().lstrip("."),
        "path": relative,
    }

async def build_artists():
    files = await get_all_audio_files()
    artists = {}
    for path in files:
        metadata = await get_metadata(path)
        artist = str(metadata.get("artist", "Unknown Artist")).strip() or "Unknown Artist"
        key = artist.lower()
        if key not in artists:
            artists[key] = {
                "id": encode_id(f"artist:{artist}"),
                "name": artist,
                "album_count": 0,
                "song_count": 0,
                "albums": set(),
            }
        artists[key]["song_count"] += 1
        album = str(metadata.get("album", "Unknown Album"))
        artists[key]["albums"].add(album)

    result = []
    for artist in artists.values():
        artist["album_count"] = len(artist["albums"])
        artist.pop("albums", None)
        result.append(artist)

    result.sort(key=lambda x: x["name"].lower())
    return result

# ============================================================
# SUBSONIC ENDPOINTS (REQUIRED BY AMPERFY)
# ============================================================

@app.get("/rest/ping.view")
async def subsonic_ping(
    u: str = Query(""), t: str = Query(""), s: str = Query(""), p: str = Query(""),
    v: str = Query(SUBSONIC_API_VERSION), c: str = Query("Amperfy"), f: str = Query("json"),
):
    err = authenticate_subsonic(u, t, s, p, f)
    if err:
        return err
    if wants_json(f):
        return Response(content=json.dumps(subsonic_json({})), media_type="application/json")
    return xml_response("")

@app.get("/rest/getLicense.view")
async def subsonic_license(
    u: str = Query(""), t: str = Query(""), s: str = Query(""), p: str = Query(""),
    v: str = Query(SUBSONIC_API_VERSION), c: str = Query("Amperfy"), f: str = Query("json"),
):
    err = authenticate_subsonic(u, t, s, p, f)
    if err:
        return err
    if wants_json(f):
        return Response(content=json.dumps(subsonic_json({"license": {"valid": True, "email": "user@xrob.music", "licenseExpires": 0}})), media_type="application/json")
    return xml_response('<license valid="true" email="user@xrob.music" licenseExpires="0"/>')

@app.get("/rest/getUser.view")
async def subsonic_get_user(
    u: str = Query(""), t: str = Query(""), s: str = Query(""), p: str = Query(""),
    username: str = Query(""), v: str = Query(SUBSONIC_API_VERSION), c: str = Query("Amperfy"), f: str = Query("json"),
):
    err = authenticate_subsonic(u, t, s, p, f)
    if err:
        return err
    target_user = username or u or "admin"
    user_data = {
        "username": target_user,
        "scrobbleEnabled": True,
        "adminRole": True,
        "settingsRole": True,
        "downloadRole": True,
        "uploadRole": True,
        "playlistRole": True,
        "coverArtRole": True,
        "commentRole": True,
        "podcastRole": True,
        "streamRole": True,
        "jukeboxRole": True,
        "shareRole": True,
    }
    if wants_json(f):
        return Response(content=json.dumps(subsonic_json({"user": user_data})), media_type="application/json")
    return xml_response(f'<user username="{xml_escape(target_user)}" scrobbleEnabled="true" adminRole="true" downloadRole="true" streamRole="true"/>')

@app.get("/rest/getMusicFolders.view")
async def subsonic_music_folders(
    u: str = Query(""), t: str = Query(""), s: str = Query(""), p: str = Query(""),
    v: str = Query(SUBSONIC_API_VERSION), c: str = Query("Amperfy"), f: str = Query("json"),
):
    err = authenticate_subsonic(u, t, s, p, f)
    if err:
        return err
    folders = [{"id": 1, "name": "Music"}]
    if wants_json(f):
        return Response(content=json.dumps(subsonic_json({"musicFolders": {"musicFolder": folders}})), media_type="application/json")
    return xml_response('<musicFolders><musicFolder id="1" name="Music"/></musicFolders>')

@app.get("/rest/getIndexes.view")
async def subsonic_indexes(
    u: str = Query(""), t: str = Query(""), s: str = Query(""), p: str = Query(""),
    v: str = Query(SUBSONIC_API_VERSION), c: str = Query("Amperfy"), f: str = Query("json"),
    musicFolderId: int = Query(1),
):
    err = authenticate_subsonic(u, t, s, p, f)
    if err:
        return err
    artists = await build_artists()

    groups = {}
    for item in artists:
        first = item["name"][0].upper() if item["name"] else "#"
        first = first if first.isalpha() else "#"
        if first not in groups:
            groups[first] = []
        groups[first].append({
            "id": item["id"],
            "name": item["name"],
            "albumCount": item["album_count"],
            "songCount": item["song_count"],
        })

    index_list = []
    for letter in sorted(groups.keys()):
        index_list.append({"name": letter, "artist": groups[letter]})

    if wants_json(f):
        return Response(content=json.dumps(subsonic_json({"indexes": {"lastModified": 0, "ignoredArticles": "", "index": index_list}})), media_type="application/json")

    xml_inner = '<indexes lastModified="0" ignoredArticles="">'
    for letter in sorted(groups.keys()):
        xml_inner += f'<index name="{xml_escape(letter)}">'
        for a in groups[letter]:
            xml_inner += f'<artist id="{xml_escape(a["id"])}" name="{xml_escape(a["name"])}" albumCount="{a["albumCount"]}" songCount="{a["songCount"]}"/>'
        xml_inner += '</index>'
    xml_inner += '</indexes>'
    return xml_response(xml_inner)

@app.get("/rest/getArtists.view")
async def subsonic_artists(
    u: str = Query(""), t: str = Query(""), s: str = Query(""), p: str = Query(""),
    v: str = Query(SUBSONIC_API_VERSION), c: str = Query("Amperfy"), f: str = Query("json"),
):
    err = authenticate_subsonic(u, t, s, p, f)
    if err:
        return err
    artists = await build_artists()

    groups = {}
    for item in artists:
        first = item["name"][0].upper() if item["name"] else "#"
        first = first if first.isalpha() else "#"
        if first not in groups:
            groups[first] = []
        groups[first].append({"id": item["id"], "name": item["name"], "albumCount": item["album_count"]})

    index_list = [{"name": letter, "artist": groups[letter]} for letter in sorted(groups.keys())]

    if wants_json(f):
        return Response(content=json.dumps(subsonic_json({"artists": {"ignoredArticles": "", "index": index_list}})), media_type="application/json")

    xml_inner = '<artists ignoredArticles="">'
    for letter in sorted(groups.keys()):
        xml_inner += f'<index name="{xml_escape(letter)}">'
        for a in groups[letter]:
            xml_inner += f'<artist id="{xml_escape(a["id"])}" name="{xml_escape(a["name"])}" albumCount="{a["albumCount"]}"/>'
        xml_inner += '</index>'
    xml_inner += '</artists>'
    return xml_response(xml_inner)

@app.get("/rest/getArtist.view")
async def subsonic_artist(
    u: str = Query(""), t: str = Query(""), s: str = Query(""), p: str = Query(""),
    id: str = Query(""), v: str = Query(SUBSONIC_API_VERSION), c: str = Query("Amperfy"), f: str = Query("json"),
):
    err = authenticate_subsonic(u, t, s, p, f)
    if err:
        return err

    artists = await build_artists()
    artist = next((x for x in artists if x["id"] == id), None)
    if not artist:
        return subsonic_error(70, "Artist not found", f)

    files = await get_all_audio_files()
    albums = {}
    for path in files:
        meta = await get_metadata(path)
        cur_artist = str(meta.get("artist", "Unknown Artist"))
        if cur_artist.lower() != artist["name"].lower():
            continue
        album_name = str(meta.get("album", "Unknown Album"))
        album_key = album_name.lower()
        if album_key not in albums:
            albums[album_key] = {
                "id": encode_id(f"album:{cur_artist}:{album_name}"),
                "name": album_name,
                "artist": cur_artist,
                "song": [],
            }
        albums[album_key]["song"].append(await song_to_subsonic(path))

    album_list = list(albums.values())
    if wants_json(f):
        return Response(content=json.dumps(subsonic_json({
            "artist": {
                "id": artist["id"],
                "name": artist["name"],
                "album": [
                    {
                        "id": alb["id"],
                        "name": alb["name"],
                        "artist": alb["artist"],
                        "songCount": len(alb["song"]),
                        "coverArt": alb["id"],
                    } for alb in album_list
                ]
            }
        })), media_type="application/json")

    return xml_response(
        f'<artist id="{xml_escape(artist["id"])}" name="{xml_escape(artist["name"])}">'
        + "".join(f'<album id="{xml_escape(alb["id"])}" name="{xml_escape(alb["name"])}" artist="{xml_escape(alb["artist"])}" songCount="{len(alb["song"])}" coverArt="{xml_escape(alb["id"])}"/>' for alb in album_list)
        + "</artist>"
    )

@app.get("/rest/getAlbum.view")
async def subsonic_album(
    u: str = Query(""), t: str = Query(""), s: str = Query(""), p: str = Query(""),
    id: str = Query(""), v: str = Query(SUBSONIC_API_VERSION), c: str = Query("Amperfy"), f: str = Query("json"),
):
    err = authenticate_subsonic(u, t, s, p, f)
    if err:
        return err

    files = await get_all_audio_files()
    album_info, songs = None, []

    for path in files:
        meta = await get_metadata(path)
        album_name = str(meta.get("album", "Unknown Album"))
        artist = str(meta.get("artist", "Unknown Artist"))
        candidate_id = encode_id(f"album:{artist}:{album_name}")

        if candidate_id != id:
            continue

        if album_info is None:
            album_info = {"id": id, "name": album_name, "artist": artist}

        songs.append(await song_to_subsonic(path))

    if album_info is None:
        return subsonic_error(70, "Album not found", f)

    songs.sort(key=lambda x: (x.get("discNumber", 0), x.get("track", 0), x.get("title", "").lower()))
    album_info["song"] = songs
    album_info["songCount"] = len(songs)
    album_info["duration"] = sum(x.get("duration", 0) for x in songs)
    album_info["coverArt"] = id

    if wants_json(f):
        return Response(content=json.dumps(subsonic_json({"album": album_info})), media_type="application/json")

    return xml_response(
        f'<album id="{xml_escape(id)}" name="{xml_escape(album_info["name"])}" artist="{xml_escape(album_info["artist"])}" songCount="{len(songs)}" coverArt="{xml_escape(id)}">'
        + "".join(f'<song id="{xml_escape(s["id"])}" title="{xml_escape(s["title"])}" artist="{xml_escape(s["artist"])}" album="{xml_escape(s["album"])}" duration="{s["duration"]}"/>' for s in songs)
        + "</album>"
    )

@app.get("/rest/getSong.view")
async def subsonic_song(
    u: str = Query(""), t: str = Query(""), s: str = Query(""), p: str = Query(""),
    id: str = Query(""), v: str = Query(SUBSONIC_API_VERSION), c: str = Query("Amperfy"), f: str = Query("json"),
):
    err = authenticate_subsonic(u, t, s, p, f)
    if err:
        return err

    try:
        path = await resolve_song_id(id)
    except Exception:
        return subsonic_error(70, "Song not found", f)

    song = await song_to_subsonic(path)
    if wants_json(f):
        return Response(content=json.dumps(subsonic_json({"song": song})), media_type="application/json")
    return xml_response("<song " + " ".join(f'{k}="{xml_escape(v)}"' for k, v in song.items() if k != "path") + "/>")

@app.get("/rest/getAlbumList.view")
@app.get("/rest/getAlbumList2.view")
async def subsonic_album_list(
    u: str = Query(""), t: str = Query(""), s: str = Query(""), p: str = Query(""),
    type: str = Query("alphabeticalByName"), size: int = Query(500), offset: int = Query(0),
    v: str = Query(SUBSONIC_API_VERSION), c: str = Query("Amperfy"), f: str = Query("json"),
):
    err = authenticate_subsonic(u, t, s, p, f)
    if err:
        return err

    files = await get_all_audio_files()
    albums = {}
    for path in files:
        meta = await get_metadata(path)
        artist = str(meta.get("artist", "Unknown Artist"))
        name = str(meta.get("album", "Unknown Album"))
        key = (artist.lower(), name.lower())

        if key not in albums:
            album_id = encode_id(f"album:{artist}:{name}")
            albums[key] = {
                "id": album_id,
                "name": name,
                "artist": artist,
                "songCount": 0,
                "coverArt": album_id,
            }
        albums[key]["songCount"] += 1

    album_list = list(albums.values())
    if type == "alphabeticalByArtist":
        album_list.sort(key=lambda x: (x["artist"].lower(), x["name"].lower()))
    else:
        album_list.sort(key=lambda x: x["name"].lower())

    album_list = album_list[offset: offset + size]
    key_name = "albumList2" if "2" in type or "2" in str(v) else "albumList"

    if wants_json(f):
        return Response(content=json.dumps(subsonic_json({key_name: {"album": album_list}})), media_type="application/json")

    return xml_response(f"<{key_name}>" + "".join(f'<album id="{xml_escape(a["id"])}" name="{xml_escape(a["name"])}" artist="{xml_escape(a["artist"])}" songCount="{a["songCount"]}" coverArt="{xml_escape(a["coverArt"])}"/>' for a in album_list) + f"</{key_name}>")

@app.get("/rest/search2.view")
@app.get("/rest/search3.view")
async def subsonic_search(
    u: str = Query(""), t: str = Query(""), s: str = Query(""), p: str = Query(""),
    query: str = Query(""), songCount: int = Query(20), albumCount: int = Query(20), artistCount: int = Query(20),
    v: str = Query(SUBSONIC_API_VERSION), c: str = Query("Amperfy"), f: str = Query("json"),
):
    err = authenticate_subsonic(u, t, s, p, f)
    if err:
        return err

    q = query.strip().lower()
    files = await get_all_audio_files()
    songs, artists, albums = [], {}, {}

    for path in files:
        meta = await get_metadata(path)
        title = str(meta.get("title", path.stem))
        artist = str(meta.get("artist", "Unknown Artist"))
        album = str(meta.get("album", "Unknown Album"))

        if q in title.lower():
            songs.append(await song_to_subsonic(path))
        if q in artist.lower():
            artists[artist.lower()] = {"id": encode_id(f"artist:{artist}"), "name": artist}
        if q in album.lower():
            albums[(artist.lower(), album.lower())] = {"id": encode_id(f"album:{artist}:{album}"), "name": album, "artist": artist}

    songs_res = songs[:songCount]
    artists_res = list(artists.values())[:artistCount]
    albums_res = list(albums.values())[:albumCount]

    if wants_json(f):
        return Response(content=json.dumps(subsonic_json({"searchResult3": {"artist": artists_res, "album": albums_res, "song": songs_res}})), media_type="application/json")

    return xml_response("<searchResult3>" + "".join(f'<artist id="{xml_escape(a["id"])}" name="{xml_escape(a["name"])}"/>' for a in artists_res) + "".join(f'<album id="{xml_escape(a["id"])}" name="{xml_escape(a["name"])}" artist="{xml_escape(a["artist"])}"/>' for a in albums_res) + "".join(f'<song id="{xml_escape(s["id"])}" title="{xml_escape(s["title"])}" artist="{xml_escape(s["artist"])}" album="{xml_escape(s["album"])}" duration="{s["duration"]}"/>' for s in songs_res) + "</searchResult3>")

@app.get("/rest/getRandomSongs.view")
async def subsonic_random_songs(
    u: str = Query(""), t: str = Query(""), s: str = Query(""), p: str = Query(""),
    size: int = Query(20), v: str = Query(SUBSONIC_API_VERSION), c: str = Query("Amperfy"), f: str = Query("json"),
):
    err = authenticate_subsonic(u, t, s, p, f)
    if err:
        return err

    files = await get_all_audio_files()
    import random
    random.shuffle(files)

    songs = []
    for path in files[:size]:
        songs.append(await song_to_subsonic(path))

    if wants_json(f):
        return Response(content=json.dumps(subsonic_json({"randomSongs": {"song": songs}})), media_type="application/json")
    return xml_response("<randomSongs>" + "".join(f'<song id="{xml_escape(s["id"])}" title="{xml_escape(s["title"])}" artist="{xml_escape(s["artist"])}" album="{xml_escape(s["album"])}" duration="{s["duration"]}"/>' for s in songs) + "</randomSongs>")

@app.get("/rest/stream.view")
async def subsonic_stream(
    u: str = Query(""), t: str = Query(""), s: str = Query(""), p: str = Query(""),
    id: str = Query(""), maxBitRate: int = Query(0), format: str = Query(""),
    v: str = Query(SUBSONIC_API_VERSION), c: str = Query("Amperfy"), f: str = Query("json"),
):
    err = authenticate_subsonic(u, t, s, p, f)
    if err:
        return err
    try:
        path = await resolve_song_id(id)
    except Exception:
        return subsonic_error(70, "Song not found", f)

    return FileResponse(
        path,
        media_type=MEDIA_TYPES.get(path.suffix.lower(), "audio/mpeg"),
        headers={"Accept-Ranges": "bytes", "Content-Disposition": f'inline; filename="{path.name}"'},
    )

@app.get("/rest/download.view")
async def subsonic_download(
    u: str = Query(""), t: str = Query(""), s: str = Query(""), p: str = Query(""),
    id: str = Query(""), v: str = Query(SUBSONIC_API_VERSION), c: str = Query("Amperfy"), f: str = Query("json"),
):
    err = authenticate_subsonic(u, t, s, p, f)
    if err:
        return err
    try:
        path = await resolve_song_id(id)
    except Exception:
        return subsonic_error(70, "Song not found", f)

    return FileResponse(path, media_type=MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream"), filename=path.name)

@app.get("/rest/getCoverArt.view")
async def subsonic_cover_art(
    u: str = Query(""), t: str = Query(""), s: str = Query(""), p: str = Query(""),
    id: str = Query(""), size: int = Query(0), v: str = Query(SUBSONIC_API_VERSION), c: str = Query("Amperfy"), f: str = Query("json"),
):
    err = authenticate_subsonic(u, t, s, p, f)
    if err:
        return err

    svg_fallback = """<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300"><rect width="100%" height="100%" fill="#1e293b"/><text x="50%" y="50%" fill="#9ca3af" font-size="70" text-anchor="middle" dominant-baseline="central">♪</text></svg>"""

    target_file = await resolve_cover_target(id)
    if not target_file:
        return Response(content=svg_fallback, media_type="image/svg+xml")

    cover = await extract_cover(target_file)
    if cover:
        return FileResponse(cover, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})

    return Response(content=svg_fallback, media_type="image/svg+xml")

@app.get("/rest/getStarred.view")
@app.get("/rest/getStarred2.view")
async def subsonic_starred(
    u: str = Query(""), t: str = Query(""), s: str = Query(""), p: str = Query(""),
    v: str = Query(SUBSONIC_API_VERSION), c: str = Query("Amperfy"), f: str = Query("json"),
):
    err = authenticate_subsonic(u, t, s, p, f)
    if err:
        return err
    key = "starred2" if "2" in str(v) else "starred"
    if wants_json(f):
        return Response(content=json.dumps(subsonic_json({key: {}})), media_type="application/json")
    return xml_response(f"<{key}/>")

@app.get("/rest/getPlaylists.view")
async def subsonic_playlists(
    u: str = Query(""), t: str = Query(""), s: str = Query(""), p: str = Query(""),
    v: str = Query(SUBSONIC_API_VERSION), c: str = Query("Amperfy"), f: str = Query("json"),
):
    err = authenticate_subsonic(u, t, s, p, f)
    if err:
        return err
    if wants_json(f):
        return Response(content=json.dumps(subsonic_json({"playlists": {"playlist": []}})), media_type="application/json")
    return xml_response("<playlists/>")

@app.get("/rest/getGenres.view")
async def subsonic_genres(
    u: str = Query(""), t: str = Query(""), s: str = Query(""), p: str = Query(""),
    v: str = Query(SUBSONIC_API_VERSION), c: str = Query("Amperfy"), f: str = Query("json"),
):
    err = authenticate_subsonic(u, t, s, p, f)
    if err:
        return err
    if wants_json(f):
        return Response(content=json.dumps(subsonic_json({"genres": {"genre": []}})), media_type="application/json")
    return xml_response("<genres/>")

@app.get("/rest/scrobble.view")
@app.get("/rest/star.view")
@app.get("/rest/unstar.view")
async def subsonic_actions(
    u: str = Query(""), t: str = Query(""), s: str = Query(""), p: str = Query(""),
    v: str = Query(SUBSONIC_API_VERSION), c: str = Query("Amperfy"), f: str = Query("json"),
):
    err = authenticate_subsonic(u, t, s, p, f)
    if err:
        return err
    if wants_json(f):
        return Response(content=json.dumps(subsonic_json({})), media_type="application/json")
    return xml_response("")

@app.get("/api/amperfy/status")
async def amperfy_status():
    settings = load_settings()
    files = await get_all_audio_files()
    return {
        "status": "ok",
        "server": "Xrob Music",
        "subsonic": True,
        "subsonic_version": SUBSONIC_API_VERSION,
        "music_files": len(files),
        "username": settings.get("subsonic_user", "admin"),
    }
