import os
import sys
import json
import re
import asyncio
import sqlite3
import shutil
import urllib.parse
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Dict, Any, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, BackgroundTasks
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import aiohttp
import aiosqlite

# Configuration & Constants
BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "tasks.db"

DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

YTDLP_BIN = shutil.which("yt-dlp") or "yt-dlp"
FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"

# Modern Lifespan Manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    task_queue = asyncio.Queue()
    app.state.queue = task_queue
    
    # Start worker pool
    workers = [asyncio.create_task(download_worker(i, task_queue)) for i in range(3)]
    
    # Reload pending tasks from DB into queue
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, url, title, artist, album FROM tasks WHERE status IN ('pending', 'downloading')") as cursor:
            async for row in cursor:
                await task_queue.put({
                    "id": row[0],
                    "url": row[1],
                    "title": row[2],
                    "artist": row[3],
                    "album": row[4]
                })

    yield

    # Clean shutdown
    for w in workers:
        w.cancel()
    await asyncio.gather(*workers, return_exceptions=True)

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Active WebSockets Connection Manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                pass

manager = ConnectionManager()

# Database Setup
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                title TEXT,
                artist TEXT,
                album TEXT,
                status TEXT NOT NULL,
                progress REAL DEFAULT 0,
                speed TEXT,
                eta TEXT,
                error TEXT,
                filepath TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()

async def db_save_task(task: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO tasks (id, url, title, artist, album, status, progress, speed, eta, error, filepath)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status=excluded.status,
                progress=excluded.progress,
                speed=excluded.speed,
                eta=excluded.eta,
                error=excluded.error,
                filepath=excluded.filepath
        """, (
            task["id"], task.get("url", ""), task.get("title", "Unknown"),
            task.get("artist", "Unknown"), task.get("album", "Unknown"),
            task.get("status", "pending"), task.get("progress", 0.0),
            task.get("speed", ""), task.get("eta", ""),
            task.get("error", None), task.get("filepath", None)
        ))
        await db.commit()

# Helpers & Sanitization
def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

def extract_video_id(url: str) -> Optional[str]:
    pattern = r"(?:v=|\/|be\/|embed\/)([a-zA-Z0-9_-]{11})"
    match = re.search(pattern, url)
    return match.group(1) if match else None

# Background Worker Process
async def download_worker(worker_id: int, queue: asyncio.Queue):
    while True:
        task = await queue.get()
        task_id = task["id"]
        
        task["status"] = "downloading"
        task["progress"] = 0.0
        await db_save_task(task)
        await manager.broadcast({"type": "task_update", "task": task})

        out_template = str(DOWNLOADS_DIR / task.get("artist", "Unknown") / task.get("album", "Unknown") / "%(title)s.%(ext)s")
        
        cmd = [
            YTDLP_BIN,
            "-x",
            "--audio-format", "mp3",
            "--audio-quality", "0",
            "--embed-thumbnail",
            "--add-metadata",
            "-o", out_template,
            "--newline",
            f"https://www.youtube.com/watch?v={task_id}"
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                decoded_line = line.decode('utf-8', errors='ignore').strip()
                
                # Parse YTDLP progress
                if "[download]" in decoded_line and "%" in decoded_line:
                    try:
                        match = re.search(r"(\d+\.\d+)%", decoded_line)
                        if match:
                            task["progress"] = float(match.group(1))
                            await db_save_task(task)
                            await manager.broadcast({"type": "task_progress", "id": task_id, "progress": task["progress"]})
                    except Exception:
                        pass

            await proc.wait()

            if proc.returncode == 0:
                task["status"] = "completed"
                task["progress"] = 100.0
                await db_save_task(task)
                await manager.broadcast({"type": "task_update", "task": task})
                await trigger_navidrome_scan()
            else:
                err = (await proc.stderr.read()).decode('utf-8', errors='ignore')
                task["status"] = "failed"
                task["error"] = err[:200]
                await db_save_task(task)
                await manager.broadcast({"type": "task_update", "task": task})

        except Exception as e:
            task["status"] = "failed"
            task["error"] = str(e)
            await db_save_task(task)
            await manager.broadcast({"type": "task_update", "task": task})
        finally:
            queue.task_done()

async def trigger_navidrome_scan():
    nav_url = os.getenv("NAVIDROME_URL")
    nav_user = os.getenv("NAVIDROME_USER")
    nav_token = os.getenv("NAVIDROME_TOKEN")
    nav_salt = os.getenv("NAVIDROME_SALT")

    if nav_url and nav_user and nav_token:
        scan_endpoint = f"{nav_url.rstrip('/')}/rest/startScan?u={nav_user}&t={nav_token}&s={nav_salt}&v=1.16.1&c=XrobMusic"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(scan_endpoint) as resp:
                    pass
        except Exception:
            pass

# API Endpoints
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

@app.get("/api/search")
async def search_youtube(q: str = Query(..., min_length=1)):
    cmd = [
        YTDLP_BIN,
        f"ytsearch10:{q}",
        "--dump-single-json",
        "--flat-playlist"
    ]
    
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise HTTPException(status_code=500, detail="Search failed")

    data = json.loads(stdout.decode('utf-8', errors='ignore'))
    results = []
    
    for entry in data.get("entries", []):
        results.append({
            "id": entry.get("id"),
            "title": entry.get("title"),
            "artist": entry.get("uploader", "Unknown"),
            "duration": entry.get("duration", 0),
            "url": f"https://www.youtube.com/watch?v={entry.get('id')}"
        })

    return {"results": results}

@app.post("/api/download")
async def queue_download(payload: dict):
    url = payload.get("url")
    video_id = extract_video_id(url)
    if not video_id:
        raise HTTPException(status_code=400, detail="Invalid YouTube Video ID/URL")

    task = {
        "id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": payload.get("title", "Unknown"),
        "artist": payload.get("artist", "Unknown"),
        "album": payload.get("album", "Unknown"),
        "status": "pending",
        "progress": 0.0
    }

    await db_save_task(task)
    await app.state.queue.put(task)
    await manager.broadcast({"type": "task_update", "task": task})

    return {"status": "queued", "task": task}

@app.get("/api/preview")
async def preview_audio(v: str = Query(...), transcode: bool = Query(False)):
    if not re.match(r"^[a-zA-Z0-9_-]{11}$", v):
        raise HTTPException(status_code=400, detail="Invalid video ID")

    video_url = f"https://www.youtube.com/watch?v={v}"

    if transcode:
        # Stream live audio transcode via FFmpeg pipe
        ytdlp_cmd = [YTDLP_BIN, "-g", "-f", "bestaudio", video_url]
        proc = await asyncio.create_subprocess_exec(
            *ytdlp_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        
        stream_url = stdout.decode().strip()
        if not stream_url:
            raise HTTPException(status_code=500, detail="Failed to fetch direct audio stream")

        ffmpeg_cmd = [
            FFMPEG_BIN,
            "-i", stream_url,
            "-f", "mp3",
            "-acodec", "libmp3lame",
            "-ab", "128k",
            "pipe:1"
        ]

        ffmpeg_proc = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL
        )

        async def stream_generator():
            try:
                while True:
                    chunk = await ffmpeg_proc.stdout.read(65536)
                    if not chunk:
                        break
                    yield chunk
            finally:
                if ffmpeg_proc.returncode is None:
                    ffmpeg_proc.kill()

        return StreamingResponse(stream_generator(), media_type="audio/mpeg")
    else:
        # Get direct manifest media URL
        ytdlp_cmd = [YTDLP_BIN, "-g", "-f", "bestaudio[ext=m4a]/bestaudio", video_url]
        proc = await asyncio.create_subprocess_exec(
            *ytdlp_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        direct_url = stdout.decode().strip()

        if not direct_url:
            raise HTTPException(status_code=500, detail="Failed to resolve stream URL")

        return {"url": direct_url}

@app.get("/api/tasks")
async def get_tasks():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tasks ORDER BY created_at DESC") as cursor:
            rows = await cursor.fetchall()
            return {"tasks": [dict(r) for r in rows]}

# Serve static frontend
app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")
