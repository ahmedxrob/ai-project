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
from typing import Optional

from fastapi import (
    FastAPI,
    Query,
    HTTPException,
    Body,
    WebSocket,
    WebSocketDisconnect,
    Request,
)
from fastapi.responses import (
    RedirectResponse,
    FileResponse,
    Response,
    StreamingResponse,
    JSONResponse,
)
from fastapi.staticfiles import StaticFiles

# ============================================================
# APP INITIALIZATION
# ============================================================

app = FastAPI(title="Xrob Music")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ============================================================
# PATHS & CONFIGURATION
# ============================================================

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/share/navidrome/music"))
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
DOWNLOAD_LOCK = asyncio.Lock()
WORKERS = []

# ============================================================
# SETTINGS FUNCTIONS
# ============================================================

def load_settings() -> dict:
    if not SETTINGS_FILE.exists():
        return dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            merged = dict(DEFAULT_SETTINGS)
            merged.update(data)
            return merged
    except Exception:
        return dict(DEFAULT_SETTINGS)

def save_settings_to_file(settings: dict):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)

# ============================================================
# DATABASE FUNCTIONS
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
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
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
            (id, title, artist, album, url, elementId, status, percent, speed, step, error, last_updated, final_name, cancel_requested)
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
        await asyncio.to_thread(_db_save_task_sync, dict(task))

def _db_load_tasks_sync():
    if not DB_FILE.exists():
        return {}
    tasks = {}
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM tasks").fetchall()
        for row in rows:
            task = dict(row)
            task["cancel_requested"] = bool(task.get("cancel_requested", 0))
            tasks[task["id"]] = task
    return tasks

# ============================================================
# WEBSOCKET MANAGER
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

async def notify_task_update(task: dict, force_save: bool = False):
    await db_save_task(task, force=force_save)
    await manager.broadcast({"type": "task_update", "task": dict(task)})

# ============================================================
# WORKER PROCESS & YT-DLP EXECUTION
# ============================================================

async def ping_navidrome():
    settings = load_settings()
    url = settings.get("navidrome_url", "").rstrip("/")
    user = settings.get("navidrome_user", "")
    token = settings.get("navidrome_token", "")
    if not url or not user or not token:
        return
    salt = settings.get("navidrome_salt", "xrob")
    token_hash = hashlib.md5((token + salt).encode("utf-8")).hexdigest()
    ping_url = f"{url}/rest/startScan?u={user}&t={token_hash}&s={salt}&v=1.16.1&c=XrobMusic"
    try:
        await asyncio.to_thread(urllib.request.urlopen, ping_url, timeout=5)
    except Exception:
        pass

async def process_task(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        return

    settings = load_settings()
    fmt = settings.get("audio_format", "mp3")
    quality = settings.get("audio_quality", "320K").replace("K", "")
    organize = settings.get("organize_by_artist", False)

    out_tmpl = str(
        DOWNLOAD_DIR
        / (
            "%(artist)s/%(title)s.%(ext)s"
            if organize
            else "%(title)s.%(ext)s"
        )
    )

    cmd = [
        "yt-dlp",
        "--newline",
        "--extract-audio",
        "--audio-format",
        fmt,
        "--audio-quality",
        quality,
        "-o",
        out_tmpl,
    ]

    if settings.get("embed_thumbnail"):
        cmd.append("--embed-thumbnail")
    if settings.get("embed_metadata"):
        cmd.append("--add-metadata")

    cmd.append(task["url"])

    task["status"] = "downloading"
    task["step"] = "Downloading"
    await notify_task_update(task, force_save=True)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        ACTIVE_PROCESSES[task_id] = proc

        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="ignore").strip()

            if task.get("cancel_requested"):
                proc.kill()
                task["status"] = "cancelled"
                task["step"] = "Cancelled"
                await notify_task_update(task, force_save=True)
                return

            percent_match = re.search(r"(\d+(?:\.\d+)?)%", text)
            if percent_match:
                task["percent"] = float(percent_match.group(1))

            speed_match = re.search(r"at\s+([0-9\.]+[KiMB/s]+)", text)
            if speed_match:
                task["speed"] = speed_match.group(1)

            if "Destination:" in text:
                task["final_name"] = Path(text.split("Destination:", 1)[1].strip()).name

            await notify_task_update(task)

        await proc.wait()

        if task.get("cancel_requested"):
            task["status"] = "cancelled"
            task["step"] = "Cancelled"
            await notify_task_update(task, force_save=True)
            return

        if proc.returncode == 0:
            task["status"] = "completed"
            task["percent"] = 100
            task["step"] = "Ready"
            await notify_task_update(task, force_save=True)
            await ping_navidrome()
        else:
            err = (await proc.stderr.read()).decode("utf-8", errors="ignore")
            task["status"] = "error"
            task["error"] = err[-200:] if err else "Download process failed."
            await notify_task_update(task, force_save=True)

    except Exception as e:
        task["status"] = "error"
        task["error"] = str(e)
        await notify_task_update(task, force_save=True)
    finally:
        ACTIVE_PROCESSES.pop(task_id, None)

async def download_worker():
    while True:
        task_id = await task_queue.get()
        try:
            await process_task(task_id)
        finally:
            task_queue.task_done()

# ============================================================
# APP LIFECYCLE
# ============================================================

@app.on_event("startup")
async def startup_event():
    init_db()
    loaded_tasks = _db_load_tasks_sync()
    TASKS.update(loaded_tasks)

    for _ in range(MAX_CONCURRENT_DOWNLOADS):
        w = asyncio.create_task(download_worker())
        WORKERS.append(w)

@app.on_event("shutdown")
async def shutdown_event():
    for w in WORKERS:
        w.cancel()

# ============================================================
# API ROUTES
# ============================================================

@app.get("/")
async def get_index():
    index_path = BASE_DIR / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return Response("index.html not found", status_code=404)

@app.get("/api/search")
async def api_search(q: str = Query(...), page: int = Query(1)):
    settings = load_settings()
    max_res = settings.get("max_results", 20)

    cmd = [
        "yt-dlp",
        "--dump-json",
        "--default-search",
        "ytsearch",
        f"ytsearch{max_res * page}:{q}",
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        lines = stdout.decode("utf-8", errors="ignore").strip().split("\n")

        results = []
        for line in lines:
            if not line:
                continue
            try:
                data = json.loads(line)
                results.append(
                    {
                        "id": data.get("id"),
                        "title": data.get("title"),
                        "artist": data.get("uploader") or data.get("channel"),
                        "channel": data.get("uploader") or data.get("channel"),
                        "duration_text": time.strftime(
                            "%M:%S", time.gmtime(data.get("duration", 0))
                        ),
                        "url": data.get("webpage_url") or f"https://www.youtube.com/watch?v={data.get('id')}",
                        "thumbnail": data.get("thumbnail"),
                    }
                )
            except Exception:
                continue

        start_idx = (page - 1) * max_res
        return results[start_idx : start_idx + max_res]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/settings")
async def api_get_settings():
    return load_settings()

@app.post("/api/settings")
async def api_save_settings(data: dict = Body(...)):
    settings = load_settings()
    settings.update(data)
    save_settings_to_file(settings)
    return {"status": "ok", "settings": settings}

@app.post("/api/download")
async def api_download(data: dict = Body(...)):
    async with DOWNLOAD_LOCK:
        url = data.get("url")
        if not url:
            raise HTTPException(status_code=400, detail="Missing URL")

        task_id = str(uuid.uuid4())
        task = {
            "id": task_id,
            "title": data.get("title", "Unknown Track"),
            "artist": data.get("artist", "Unknown Artist"),
            "album": data.get("album", "Single"),
            "url": url,
            "elementId": data.get("elementId", ""),
            "status": "queued",
            "percent": 0,
            "speed": "",
            "step": "Queued",
            "error": "",
            "last_updated": time.time(),
            "final_name": "",
            "cancel_requested": False,
        }

        TASKS[task_id] = task
        await db_save_task(task, force=True)
        await task_queue.put(task_id)
        return task

@app.get("/api/tasks")
async def api_get_tasks():
    return list(TASKS.values())

@app.post("/api/tasks/{task_id}/cancel")
async def api_cancel_task(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    task["cancel_requested"] = True
    if task_id in ACTIVE_PROCESSES:
        try:
            ACTIVE_PROCESSES[task_id].kill()
        except Exception:
            pass
    task["status"] = "cancelled"
    task["step"] = "Cancelled"
    await notify_task_update(task, force_save=True)
    return {"status": "ok"}

@app.get("/api/library")
async def api_get_library():
    files = []
    total_size = 0

    for root, _, filenames in os.walk(DOWNLOAD_DIR):
        for name in filenames:
            ext = Path(name).suffix.lower()
            if ext in AUDIO_EXTENSIONS:
                filepath = Path(root) / name
                stat = filepath.stat()
                total_size += stat.st_size
                rel_path = filepath.relative_to(DOWNLOAD_DIR)
                files.append(
                    {
                        "name": str(rel_path),
                        "size": f"{stat.st_size / (1024*1024):.1f} MB",
                        "size_bytes": stat.st_size,
                    }
                )

    mb = total_size / (1024 * 1024)
    gb = mb / 1024
    size_str = f"{gb:.2f} GB" if gb >= 1 else f"{mb:.1f} MB"
    return {"files": files, "total_size": size_str}

@app.delete("/api/library/{filename:path}")
async def api_delete_library_file(filename: str):
    file_path = DOWNLOAD_DIR / filename
    if file_path.exists() and file_path.is_file():
        file_path.unlink()
        return {"status": "ok"}
    raise HTTPException(status_code=404, detail="File not found")

@app.get("/api/stats")
async def api_get_stats():
    tracks = 0
    artists = set()
    albums = set()

    for root, dirs, files in os.walk(DOWNLOAD_DIR):
        for f in files:
            if Path(f).suffix.lower() in AUDIO_EXTENSIONS:
                tracks += 1
                rel = Path(root).relative_to(DOWNLOAD_DIR)
                parts = rel.parts
                if len(parts) >= 1 and parts[0] != ".covers":
                    artists.add(parts[0])
                if len(parts) >= 2:
                    albums.add(parts[1])

    return {"tracks": tracks, "artists": len(artists), "albums": len(albums)}

@app.get("/api/library/stream/{filename:path}")
async def api_stream_file(filename: str, request: Request):
    file_path = DOWNLOAD_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    file_size = file_path.stat().st_size
    mime_type, _ = mimetypes.guess_type(file_path)
    mime_type = mime_type or MEDIA_TYPES.get(file_path.suffix.lower(), "audio/mpeg")

    range_header = request.headers.get("range")
    if range_header:
        bytes_type, bytes_range = range_header.split("=")
        if bytes_type.strip() == "bytes":
            start, end = bytes_range.split("-")
            start = int(start) if start else 0
            end = int(end) if end else file_size - 1
            if end >= file_size:
                end = file_size - 1
            chunk_size = (end - start) + 1

            def iterfile():
                with open(file_path, "rb") as f:
                    f.seek(start)
                    bytes_read = 0
                    while bytes_read < chunk_size:
                        read_len = min(64 * 1024, chunk_size - bytes_read)
                        data = f.read(read_len)
                        if not data:
                            break
                        bytes_read += len(data)
                        yield data

            headers = {
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(chunk_size),
                "Content-Type": mime_type,
            }
            return StreamingResponse(iterfile(), status_code=206, headers=headers)

    return FileResponse(file_path, media_type=mime_type)

@app.get("/api/preview")
async def api_preview(url: str = Query(...)):
    cmd = ["yt-dlp", "-g", "-f", "bestaudio/best", url]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        stream_url = stdout.decode("utf-8", errors="ignore").strip().split("\n")[0]
        if stream_url:
            return RedirectResponse(stream_url)
        raise HTTPException(status_code=400, detail="Could not extract stream URL")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/library/cover/{filename:path}")
async def api_get_cover(filename: str):
    file_path = DOWNLOAD_DIR / filename
    cover_file = COVER_CACHE_DIR / f"{hashlib.md5(filename.encode()).hexdigest()}.jpg"

    if cover_file.exists():
        return FileResponse(cover_file)

    if file_path.exists():
        cmd = ["ffmpeg", "-y", "-i", str(file_path), "-an", "-vcodec", "copy", str(cover_file)]
        try:
            proc = await asyncio.create_subprocess_exec(*cmd)
            await proc.wait()
            if cover_file.exists() and cover_file.stat().st_size > 0:
                return FileResponse(cover_file)
        except Exception:
            pass

    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="110" height="65"><rect width="100%" height="100%" fill="#1e293b"/><text x="50%" y="55%" text-anchor="middle" font-size="24" fill="#ffffff">♪</text></svg>"""
    return Response(content=svg, media_type="image/svg+xml")

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)
