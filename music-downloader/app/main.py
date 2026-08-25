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
            with open(
                SETTINGS_FILE,
                "r",
                encoding="utf-8",
            ) as file:
