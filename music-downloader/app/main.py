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

from fastapi import FastAPI, Query, HTTPException, Body, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse, FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Xrob Music")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/share/navidrome/music"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

COVER_CACHE_DIR = DOWNLOAD_DIR / ".covers"
COVER_CACHE_DIR.mkdir(parents=True, exist_ok=True)

SETTINGS_FILE = DOWNLOAD_DIR / ".settings.json"
DB_FILE = DOWNLOAD_DIR / "tasks.db"
AUDIO_EXTENSIONS = {'.mp3', '.flac', '.m4a', '.ogg', '.wav', '.opus', '.aac', '.alac'}
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
    "navidrome_url": os.getenv("NAVIDROME_URL", ""),
    "navidrome_user": os.getenv("NAVIDROME_USER", ""),
    "navidrome_token": os.getenv("NAVIDROME_TOKEN", ""),
    "navidrome_salt": os.getenv("NAVIDROME_SALT", "")
}

TASKS = {}
task_queue = asyncio.Queue()
ACTIVE_PROCESSES = {}
LAST_SAVED_TIME = {}


# --- PERSISTENT TASK STORAGE (SQLite) ---

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
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
        """)
        conn.commit()


def _db_save_task_sync(task: dict):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO tasks 
            (id, title, artist, album, url, elementId, status, percent, speed, step, error, last_updated, final_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task.get("id"), task.get("title"), task.get("artist"), task.get("album"),
            task.get("url"), task.get("elementId"), task.get("status"), task.get("percent", 0),
            task.get("speed", ""), task.get("step", ""), task.get("error", ""),
            task.get("last_updated", 0), task.get("final_name", "")
        ))
        conn.commit()


async def db_save_task(task: dict, force: bool = False):
    task_id = task.get("id")
    now = time.time()
    if force or (now - LAST_SAVED_TIME.get(task_id, 0) > 0.5):
        LAST_SAVED_TIME[task_id] = now
        await asyncio.to_thread(_db_save_task_sync, task)


def _db_load_tasks_sync() -> dict:
    if not DB_FILE.exists():
        return {}
    tasks = {}
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tasks")
        for row in cursor.fetchall():
            t = dict(row)
            tasks[t["id"]] = t
    return tasks


def _db_clear_completed_tasks_sync():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("DELETE FROM tasks WHERE status IN ('completed', 'cancelled', 'error')")
        conn.commit()


# --- WEBSOCKET CONNECTION MANAGER ---

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

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


# --- NAVIDROME AUTO-RESCAN TRIGGER ---

async def trigger_navidrome_rescan():
    settings = load_settings()
    url = settings.get("navidrome_url") or os.getenv("NAVIDROME_URL", "")
    if not url:
        return

    user = settings.get("navidrome_user") or os.getenv("NAVIDROME_USER", "")
    token = settings.get("navidrome_token") or os.getenv("NAVIDROME_TOKEN", "")
    salt = settings.get("navidrome_salt") or os.getenv("NAVIDROME_SALT", "")

    endpoint = f"{url.rstrip('/')}/rest/startScan"
    params = {"u": user, "t": token, "v": "1.16.1", "c": "XrobMusic", "f": "json"}
    if salt:
        params["s"] = salt

    req_url = f"{endpoint}?{urllib.parse.urlencode(params)}"

    try:
        def _ping():
            req = urllib.request.Request(req_url, headers={"User-Agent": "XrobMusic/1.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                pass
        await asyncio.to_thread(_ping)
    except Exception:
        pass


def normalize_duplicate_key(value: str) -> str:
    value = Path(value or "").stem.lower()
    value = re.sub(r"\b(official\s*(video|audio|music video)|lyrics?|hd|4k|remaster(ed)?|audio)\b", " ", value, flags=re.I)
    value = re.sub(r"[^a-z0-9]+", "", value)
    return value


def _get_all_audio_files_sync():
    return [p for p in DOWNLOAD_DIR.rglob("*")
            if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in AUDIO_EXTENSIONS]


async def get_all_audio_files():
    return await asyncio.to_thread(_get_all_audio_files_sync)


def _is_duplicate_sync(title: str) -> bool:
    key = normalize_duplicate_key(title)
    files = _get_all_audio_files_sync()
    return any(normalize_duplicate_key(p.name) == key for p in files)


async def is_duplicate(title: str) -> bool:
    return await asyncio.to_thread(_is_duplicate_sync, title)


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


def clean_filename(value: str) -> str:
    value = value or "Unknown"
    value = re.sub(r'[\\/:*?"<>|]', "", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value[:180]) if value else "Unknown"


def format_duration(seconds):
    try:
        seconds = int(seconds or 0)
        minutes = seconds // 60
        seconds = seconds % 60
        return f"{minutes}:{seconds:02d}"
    except Exception:
        return "0:00"


def format_size(size_bytes):
    try:
        if size_bytes >= 1024 * 1024 * 1024:
            gb = size_bytes / (1024 * 1024 * 1024)
            return f"{gb:.2f} GB"
        mb = size_bytes / (1024 * 1024)
        return f"{mb:.1f} MB"
    except Exception:
        return "0 MB"


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


async def resolve_file(filename: str) -> Path:
    return await asyncio.to_thread(_resolve_file_sync, filename)


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
                "--embed-subs",
                "--sub-langs", "all,-live_chat",
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
                    
                    spd_match = speed_regex.search(line_str)
                    if spd_match:
                        task["speed"] = spd_match.group(1).replace("~", "")
                    await notify_task_update(task, force_save=False)
                        
                elif "[ExtractAudio]" in line_str or "[EmbedThumbnail]" in line_str or "[Metadata]" in line_str:
                    task["status"] = "processing"
                    task["step"] = "Embedding cover art & tags..."
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
                task["error"] = err_text[-300:]
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
                task["last_updated"] = time.time() * 1000
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
                "ffmpeg",
                "-y",
                "-i", str(audio_file),
                "-map", "0",
                "-c", "copy",
                "-disposition:v:0", "attached_pic",
                "-metadata", f"album={clean_title}",
                "-metadata", "comment=",
                "-metadata", "description=",
                "-metadata", "purl=",
                str(cleaned_file)
            ]

            process_clean = await asyncio.create_subprocess_exec(
                *clean_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await process_clean.wait()

            if process_clean.returncode == 0 and cleaned_file.exists():
                audio_file.unlink()
                audio_file = cleaned_file

            artist = clean_filename(task.get("artist", "Unknown Artist"))
            if settings.get("organize_by_artist", False):
                final_dir = DOWNLOAD_DIR / artist
                final_dir.mkdir(parents=True, exist_ok=True)
            else:
                final_dir = DOWNLOAD_DIR

            final_name = f"{clean_title}{ext}"
            final_path = final_dir / final_name

            if final_path.exists() or await is_duplicate(clean_title):
                final_name = f"{clean_title}_{task_id[:4]}{ext}"
                final_path = final_dir / final_name

            shutil.move(str(audio_file), str(final_path))
            task["final_name"] = str(final_path.relative_to(DOWNLOAD_DIR))

            task["status"] = "completed"
            task["percent"] = 100
            task["step"] = "Ready"
            task["last_updated"] = time.time() * 1000
            await notify_task_update(task, force_save=True)

            # Trigger Navidrome automatic scan ping
            await trigger_navidrome_rescan()

        except Exception as err:
            await asyncio.to_thread(cleanup_task_files, task_id)
            task["status"] = "error"
            task["error"] = str(err)
            task["last_updated"] = time.time() * 1000
            await notify_task_update(task, force_save=True)
        finally:
            task_queue.task_done()


@app.on_event("startup")
async def startup_event():
    await asyncio.to_thread(init_db)
    global TASKS
    TASKS = await asyncio.to_thread(_db_load_tasks_sync)
    for _ in range(MAX_CONCURRENT_DOWNLOADS):
        asyncio.create_task(download_worker())


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


async def youtube_search(query: str, max_results: int, page: int = 1):
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
        f"ytsearch{end_idx}:{query}",
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            error = stderr.decode("utf-8", errors="ignore")
            raise RuntimeError(error[-2000:])

        data = json.loads(stdout.decode("utf-8", errors="ignore"))
        results = []

        for item in data.get("entries", []):
            if not item:
                continue
            
            video_id = item.get("id")
            if not video_id:
                continue

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

    except FileNotFoundError:
        raise RuntimeError("yt-dlp is not installed.")
    except json.JSONDecodeError:
        raise RuntimeError("YouTube returned invalid search data.")


@app.get("/")
async def home():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/settings")
async def get_settings():
    return load_settings()


@app.post("/api/settings")
async def update_settings(data: dict = Body(...)):
    return save_settings(data)


@app.get("/api/library")
async def get_library():
    audio_files = await get_all_audio_files()
    def _build():
        files = []
        total_bytes = 0
        for path in audio_files:
            sz = path.stat().st_size
            total_bytes += sz
            files.append({
                "name": str(path.relative_to(DOWNLOAD_DIR)),
                "size": format_size(sz),
                "bytes": sz
            })
        return files, total_bytes
    files, total_bytes = await asyncio.to_thread(_build)
    return {
        "files": sorted(files, key=lambda x: x["name"]),
        "total_size": format_size(total_bytes),
        "total_bytes": total_bytes
    }


@app.get("/api/stats")
async def get_stats():
    files = await get_all_audio_files()
    def _build():
        total_bytes = sum(p.stat().st_size for p in files)
        artists = set()
        albums = set()
        for p in files:
            rel = p.relative_to(DOWNLOAD_DIR)
            if len(rel.parts) > 1:
                artists.add(rel.parts[0])
        return len(files), len(artists), len(albums), format_size(total_bytes)
    tracks_cnt, artists_cnt, albums_cnt, total_sz = await asyncio.to_thread(_build)
    return {
        "tracks": tracks_cnt,
        "artists": artists_cnt,
        "albums": albums_cnt,
        "total_size": total_sz
    }


@app.get("/api/library/stream/{filename:path}")
async def stream_library_file(filename: str, transcode: bool = Query(False)):
    file_path = await resolve_file(filename)

    if transcode:
        async def transcode_generator():
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-i", str(file_path), "-vn", "-ab", "192k", "-f", "mp3", "pipe:1",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
            await proc.wait()

        return StreamingResponse(transcode_generator(), media_type="audio/mpeg")

    ext = file_path.suffix.lower()
    media_type = MEDIA_TYPES.get(ext, mimetypes.guess_type(file_path)[0] or "audio/mpeg")
    return FileResponse(path=file_path, filename=file_path.name, media_type=media_type)


@app.get("/api/library/cover/{filename:path}")
async def get_library_cover(filename: str):
    file_path = await resolve_file(filename)
    
    cache_key = hashlib.md5(str(file_path.relative_to(DOWNLOAD_DIR)).encode()).hexdigest() + ".jpg"
    cache_path = COVER_CACHE_DIR / cache_key
    
    if cache_path.exists():
        return FileResponse(cache_path, media_type="image/jpeg")

    command = [
        "ffmpeg",
        "-y",
        "-i", str(file_path),
        "-an",
        "-c:v", "mjpeg",
        "-frames:v", "1",
        "-f", "image2pipe",
        "-"
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await process.communicate()
        if process.returncode == 0 and len(stdout) > 0:
            await asyncio.to_thread(cache_path.write_bytes, stdout)
            return Response(content=stdout, media_type="image/jpeg")
    except Exception:
        pass

    svg_placeholder = """<svg xmlns="http://www.w3.org/2000/svg" width="110" height="65" viewBox="0 0 110 65"><rect width="100%" height="100%" fill="#1e293b"/><text x="50%" y="50%" fill="#9ca3af" font-size="20" text-anchor="middle" dominant-baseline="central">🎵</text></svg>"""
    return Response(content=svg_placeholder, media_type="image/svg+xml")


@app.delete("/api/library/{filename:path}")
async def delete_library_file(filename: str):
    file_path = await resolve_file(filename)
    
    cache_key = hashlib.md5(str(file_path.relative_to(DOWNLOAD_DIR)).encode()).hexdigest() + ".jpg"
    cache_path = COVER_CACHE_DIR / cache_key
    if cache_path.exists():
        try:
            cache_path.unlink()
        except Exception:
            pass
            
    file_path.unlink()
    return {"status": "deleted"}


@app.get("/api/search")
async def search(q: str = Query(..., min_length=1), page: int = Query(1, ge=1)):
    settings = load_settings()
    try:
        results = await youtube_search(q, settings.get("max_results", 20), page)
        return results
    except Exception as error:
        raise HTTPException(status_code=500, detail="YouTube search failed: " + str(error))


@app.get("/api/preview")
async def preview_audio(url: str = Query(..., min_length=1)):
    if not (url.startswith("https://www.youtube.com/") or url.startswith("https://youtube.com/") or url.startswith("https://youtu.be/")):
        raise HTTPException(status_code=400, detail="Invalid YouTube URL.")
    try:
        process = await asyncio.create_subprocess_exec(
            "yt-dlp",
            "-g",
            "-f", "ba/b",
            "--no-playlist",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError("Failed to extract preview stream URL.")
        
        stream_url = stdout.decode("utf-8", errors="ignore").strip().split("\n")[0]
        if not stream_url:
            raise HTTPException(status_code=400, detail="Could not retrieve audio stream.")

        ffmpeg_proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-headers", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)\r\n",
            "-i", stream_url,
            "-vn",
            "-acodec", "libmp3lame",
            "-ab", "128k",
            "-f", "mp3",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL
        )

        async def media_stream():
            try:
                while True:
                    chunk = await ffmpeg_proc.stdout.read(65536)
                    if not chunk:
                        break
                    yield chunk
            finally:
                if ffmpeg_proc.returncode is None:
                    try:
                        ffmpeg_proc.kill()
                    except Exception:
                        pass
                await ffmpeg_proc.wait()

        return StreamingResponse(media_stream(), media_type="audio/mpeg")
    except Exception as err:
        raise HTTPException(status_code=500, detail=str(err))


@app.post("/api/download")
async def enqueue_download(payload: dict = Body(...)):
    url = payload.get("url")
    title = payload.get("title", "Unknown")
    artist = payload.get("artist", "Unknown Artist")
    album = payload.get("album", "")
    element_id = payload.get("elementId", "")

    if await is_duplicate(title):
        raise HTTPException(status_code=409, detail="This track already exists in your library.")
    
    if not url:
        raise HTTPException(status_code=400, detail="Missing URL")

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
        "step": "Waiting in queue...",
        "error": "",
        "last_updated": time.time() * 1000
    }
    
    TASKS[task_id] = task_info
    await notify_task_update(task_info, force_save=True)
    await task_queue.put(task_id)
    return {"status": "ok", "task_id": task_id}


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"] in ["completed", "cancelled"]:
        return {"status": task["status"]}

    task["cancel_requested"] = True
    proc = ACTIVE_PROCESSES.get(task_id)
    if proc and proc.returncode is None:
        proc.terminate()
    task["status"] = "cancelled"
    task["step"] = "Cancelled"
    task["last_updated"] = time.time() * 1000
    await notify_task_update(task, force_save=True)
    return {"status": "cancelled"}


@app.delete("/api/tasks/clear-completed")
async def clear_completed_tasks():
    global TASKS
    to_remove = [
        task_id for task_id, task in TASKS.items() 
        if task.get("status") in ["completed", "cancelled", "error"]
    ]
    for task_id in to_remove:
        TASKS.pop(task_id, None)

    await asyncio.to_thread(_db_clear_completed_tasks_sync)
    return {"status": "ok", "cleared": len(to_remove)}


@app.get("/api/tasks")
async def get_tasks():
    now = time.time() * 1000
    for t in TASKS.values():
        if t["status"] in ["queued", "downloading", "processing"]:
            t["last_updated"] = now
            
    def sort_key(task):
        status_weight = {"downloading": 0, "processing": 1, "queued": 2, "completed": 3, "error": 4, "cancelled": 5}
        return status_weight.get(task["status"], 99)
        
    sorted_tasks = sorted(list(TASKS.values()), key=sort_key)
    return sorted_tasks


@app.get("/health")
async def health():
    return {"status": "ok", "service": "music-downloader"}
