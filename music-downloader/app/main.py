import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import random
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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    RedirectResponse,
    FileResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware


# ============================================================
# XROB MUSIC
# Native Subsonic-compatible music server
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


# ============================================================
# MIDDLEWARE FOR SUBSONIC .view EXTENSION REMOVAL
# ============================================================

class SubsonicViewSuffixMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path.endswith(".view"):
            request.scope["path"] = request.url.path[:-5]
        return await call_next(request)


app.add_middleware(SubsonicViewSuffixMiddleware)

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


# ============================================================
# AUDIO
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


# ============================================================
# SUBSONIC
# ============================================================

SUBSONIC_VERSION = "1.16.1"
SUBSONIC_SERVER_TYPE = "Xrob Music"
SUBSONIC_SERVER_VERSION = "1.0.0"

DEFAULT_SETTINGS = {
    "audio_format": "mp3",
    "audio_quality": "320K",
    "embed_thumbnail": True,
    "embed_metadata": True,
    "max_results": 20,
    "organize_by_artist": False,
    "poll_interval": 1500,
    "subsonic_user": os.getenv("SUBSONIC_USER", "admin"),
    "subsonic_password": os.getenv("SUBSONIC_PASSWORD", ""),
}


# ============================================================
# GLOBAL STATE
# ============================================================

TASKS = {}
task_queue = asyncio.Queue()
ACTIVE_PROCESSES = {}
LAST_SAVED_TIME = {}
MAX_CONCURRENT_DOWNLOADS = 2

LIBRARY_CACHE = {
    "files": [],
    "songs": {},
    "artists": {},
    "albums": {},
    "last_scan": 0,
}


# ============================================================
# SUBSONIC ID HELPERS
# ============================================================

def make_subsonic_id(path: Path) -> str:
    try:
        relative = str(path.relative_to(DOWNLOAD_DIR))
    except Exception:
        relative = str(path)
    digest = hashlib.sha1(relative.encode("utf-8")).hexdigest()[:24]
    return f"song-{digest}"

def make_album_id(artist: str, album: str) -> str:
    value = f"{artist}\0{album}"
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:24]
    return f"album-{digest}"

def make_artist_id(artist: str) -> str:
    digest = hashlib.sha1(artist.encode("utf-8")).hexdigest()[:24]
    return f"artist-{digest}"

def make_cover_id(path: Path) -> str:
    return make_subsonic_id(path)


# ============================================================
# SUBSONIC XML / JSON HELPERS
# ============================================================

def subsonic_response(request: Request, root_element: ET.Element):
    fmt = request.query_params.get("f", "").lower()

    if fmt in {"json", "json2"}:
        def element_to_dict(element):
            res = {}
            for k, v in element.attrib.items():
                res[k] = v

            children = list(element)
            if not children:
                return res

            for child in children:
                child_data = element_to_dict(child)
                tag = child.tag
                if tag in res:
                    if not isinstance(res[tag], list):
                        res[tag] = [res[tag]]
                    res[tag].append(child_data)
                else:
                    if tag in {"song", "album", "artist", "index", "musicFolder", "entry", "playlist"}:
                        res[tag] = [child_data]
                    else:
                        res[tag] = child_data
            return res

        data = element_to_dict(root_element)
        return Response(
            content=json.dumps({"subsonic-response": data}, ensure_ascii=False),
            media_type="application/json",
        )

    xml_bytes = ET.tostring(root_element, encoding="utf-8", xml_declaration=True)
    return Response(content=xml_bytes, media_type="application/xml")


def subsonic_root(status="ok"):
    return ET.Element(
        "subsonic-response",
        {
            "status": status,
            "version": SUBSONIC_VERSION,
            "type": SUBSONIC_SERVER_TYPE,
            "serverVersion": SUBSONIC_SERVER_VERSION,
            "openSubsonic": "false",
        },
    )


def subsonic_error(request: Request, code: int, message: str):
    root = subsonic_root(status="failed")
    ET.SubElement(root, "error", {"code": str(code), "message": message})
    return subsonic_response(request, root)


# ============================================================
# SUBSONIC AUTHENTICATION
# ============================================================

def get_subsonic_credentials():
    settings = load_settings()
    username = settings.get("subsonic_user") or os.getenv("SUBSONIC_USER", "admin")
    password = settings.get("subsonic_password") if settings.get("subsonic_password") is not None else os.getenv("SUBSONIC_PASSWORD", "")
    return username, password or ""


def verify_subsonic_auth(request: Request):
    username = request.query_params.get("u", "")
    password = request.query_params.get("p", "")
    token = request.query_params.get("t", "")
    salt = request.query_params.get("s", "")

    expected_user, expected_password = get_subsonic_credentials()

    if not username or username != expected_user:
        return False

    # If no password set on server, allow access
    if not expected_password:
        return True

    # Check MD5 Token Auth (t = md5(password + salt))
    if token and salt:
        expected_token = hashlib.md5((expected_password + salt).encode("utf-8")).hexdigest()
        if token.lower() == expected_token.lower():
            return True

    # Check Plaintext / Encoded Password Auth
    if password:
        if password.startswith("enc:"):
            try:
                password = base64.b64decode(password[4:]).decode("utf-8")
            except Exception:
                return False
        if password == expected_password:
            return True

    return False


# ============================================================
# PERSISTENT TASK STORAGE & SETTINGS
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


def load_settings():
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return {**DEFAULT_SETTINGS, **json.load(f)}
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
# WEBSOCKET MANAGER
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
# HELPERS & METADATA
# ============================================================

def clean_filename(value: str):
    value = value or "Unknown"
    value = re.sub(r'[\\/:*?"<>|]', "", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:180] if value else "Unknown"


def format_duration(seconds):
    try:
        seconds = int(seconds or 0)
        return f"{seconds // 60}:{seconds % 60:02d}"
    except Exception:
        return "0:00"


def format_size(size_bytes):
    try:
        if size_bytes >= 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    except Exception:
        return "0 MB"


def _get_all_audio_files_sync():
    return [
        p for p in DOWNLOAD_DIR.rglob("*")
        if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in AUDIO_EXTENSIONS
    ]


async def get_all_audio_files():
    return await asyncio.to_thread(_get_all_audio_files_sync)


def parse_music_metadata(path: Path):
    command = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", str(path)
    ]
    try:
        res = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
        data = json.loads(res.stdout.decode("utf-8", errors="ignore")) if res.returncode == 0 else {}
    except Exception:
        data = {}

    fmt = data.get("format", {})
    tags = {str(k).lower(): v for k, v in fmt.get("tags", {}).items()}

    title = tags.get("title") or path.stem
    artist = tags.get("artist") or tags.get("album_artist") or (path.parent.name if path.parent != DOWNLOAD_DIR else "Unknown Artist")
    album_artist = tags.get("album_artist") or artist
    album = tags.get("album") or "Unknown Album"
    genre = tags.get("genre") or "Unknown"
    year = tags.get("date") or tags.get("year") or ""

    try:
        duration = int(float(fmt.get("duration", 0)))
    except Exception:
        duration = 0

    return {
        "title": title,
        "artist": artist,
        "albumArtist": album_artist,
        "album": album,
        "genre": genre,
        "year": year,
        "track": 1,
        "discNumber": 1,
        "duration": duration,
        "size": path.stat().st_size,
        "bitRate": int(float(fmt.get("bit_rate", 0) or 0)),
        "contentType": MEDIA_TYPES.get(path.suffix.lower(), "audio/mpeg"),
        "suffix": path.suffix.lower(),
    }


async def scan_subsonic_library(force=False):
    now = time.time()
    if not force and LIBRARY_CACHE["files"] and (now - LIBRARY_CACHE["last_scan"] < 15):
        return LIBRARY_CACHE

    files = await get_all_audio_files()
    songs, artists, albums = {}, {}, {}

    for path in files:
        try:
            song_id = make_subsonic_id(path)
            stat = path.stat()
            metadata = await asyncio.to_thread(parse_music_metadata, path)
            metadata["_mtime"] = stat.st_mtime
            metadata["_size"] = stat.st_size
            metadata["_path"] = str(path)
            metadata["id"] = song_id
            songs[song_id] = metadata

            art_name = metadata["albumArtist"] or metadata["artist"]
            alb_name = metadata["album"]
            art_id = make_artist_id(art_name)
            alb_id = make_album_id(art_name, alb_name)

            if art_id not in artists:
                artists[art_id] = {"id": art_id, "name": art_name, "albumIds": [], "songs": []}
            if alb_id not in albums:
                albums[alb_id] = {
                    "id": alb_id, "name": alb_name, "artist": art_name,
                    "artistId": art_id, "songIds": [], "songs": [],
                    "year": metadata["year"], "genre": metadata["genre"]
                }
                artists[art_id]["albumIds"].append(alb_id)

            artists[art_id]["songs"].append(song_id)
            albums[alb_id]["songIds"].append(song_id)
        except Exception:
            continue

    LIBRARY_CACHE.update({"files": files, "songs": songs, "artists": artists, "albums": albums, "last_scan": now})
    return LIBRARY_CACHE


# ============================================================
# SUBSONIC OBJECT BUILDERS
# ============================================================

def song_to_xml(parent, song):
    attrs = {
        "id": song["id"],
        "parent": make_album_id(song.get("albumArtist", "Unknown Artist"), song.get("album", "Unknown Album")),
        "isDir": "false",
        "title": song.get("title", "Unknown"),
        "album": song.get("album", "Unknown Album"),
        "artist": song.get("artist", "Unknown Artist"),
        "albumArtist": song.get("albumArtist", song.get("artist", "Unknown Artist")),
        "track": str(song.get("track", 1)),
        "year": str(song.get("year", "")),
        "genre": song.get("genre", ""),
        "coverArt": make_cover_id(Path(song["_path"])),
        "size": str(song.get("size", 0)),
        "contentType": song.get("contentType", "audio/mpeg"),
        "suffix": song.get("suffix", ".mp3").lstrip("."),
        "duration": str(song.get("duration", 0)),
        "bitRate": str(int(song.get("bitRate", 0) / 1000) if song.get("bitRate", 0) else "320"),
        "path": str(Path(song["_path"]).relative_to(DOWNLOAD_DIR)),
        "isVideo": "false",
        "type": "music",
    }
    ET.SubElement(parent, "song", attrs)


def subsonic_endpoint(path):
    def decorator(func):
        app.get(f"/rest/{path}")(func)
        app.post(f"/rest/{path}")(func)
        app.get(f"/api/subsonic/rest/{path}")(func)
        app.post(f"/api/subsonic/rest/{path}")(func)
        return func
    return decorator


async def require_subsonic(request: Request):
    if not verify_subsonic_auth(request):
        return subsonic_error(request, 40, "Wrong username or password.")
    return None


# ============================================================
# SUBSONIC ENDPOINTS
# ============================================================

@app.get("/rest/")
@app.get("/rest")
@app.get("/api/subsonic/rest/")
@app.get("/api/subsonic/rest")
async def subsonic_root_endpoint(request: Request):
    auth_err = await require_subsonic(request)
    if auth_err:
        return auth_err
    return subsonic_response(request, subsonic_root())


@subsonic_endpoint("ping")
async def subsonic_ping(request: Request):
    auth_err = await require_subsonic(request)
    if auth_err:
        return auth_err
    return subsonic_response(request, subsonic_root())


@subsonic_endpoint("getLicense")
async def subsonic_get_license(request: Request):
    auth_err = await require_subsonic(request)
    if auth_err:
        return auth_err
    root = subsonic_root()
    ET.SubElement(root, "license", {"valid": "true", "email": "admin@xrob.music", "licenseExpires": "2099-12-31T23:59:59"})
    return subsonic_response(request, root)


@subsonic_endpoint("getMusicFolders")
async def subsonic_get_music_folders(request: Request):
    auth_err = await require_subsonic(request)
    if auth_err:
        return auth_err
    root = subsonic_root()
    folders = ET.SubElement(root, "musicFolders")
    ET.SubElement(folders, "musicFolder", {"id": "1", "name": "Music"})
    return subsonic_response(request, root)


@subsonic_endpoint("getArtists")
async def subsonic_get_artists(request: Request):
    auth_err = await require_subsonic(request)
    if auth_err:
        return auth_err
    library = await scan_subsonic_library()
    root = subsonic_root()
    artists_elem = ET.SubElement(root, "artists", {"ignoredArticles": ""})

    for artist in sorted(library["artists"].values(), key=lambda x: x["name"].lower()):
        index_elem = ET.SubElement(artists_elem, "index", {"name": artist["name"][:1].upper() if artist["name"] else "#"})
        ET.SubElement(index_elem, "artist", {"id": artist["id"], "name": artist["name"], "albumCount": str(len(artist["albumIds"]))})

    return subsonic_response(request, root)


@subsonic_endpoint("getArtist")
async def subsonic_get_artist(request: Request, id: str = Query(...)):
    auth_err = await require_subsonic(request)
    if auth_err:
        return auth_err
    library = await scan_subsonic_library()
    artist = library["artists"].get(id)
    if not artist:
        return subsonic_error(request, 70, "Artist not found.")

    root = subsonic_root()
    art_elem = ET.SubElement(root, "artist", {"id": artist["id"], "name": artist["name"], "albumCount": str(len(artist["albumIds"]))})
    for alb_id in artist["albumIds"]:
        album = library["albums"].get(alb_id)
        if album:
            ET.SubElement(art_elem, "album", {"id": album["id"], "name": album["name"], "artist": album["artist"], "artistId": album["artistId"], "songCount": str(len(album["songIds"]))})
    return subsonic_response(request, root)


@subsonic_endpoint("getAlbum")
async def subsonic_get_album(request: Request, id: str = Query(...)):
    auth_err = await require_subsonic(request)
    if auth_err:
        return auth_err
    library = await scan_subsonic_library()
    album = library["albums"].get(id)
    if not album:
        return subsonic_error(request, 70, "Album not found.")

    root = subsonic_root()
    alb_elem = ET.SubElement(root, "album", {"id": album["id"], "name": album["name"], "artist": album["artist"], "artistId": album["artistId"], "songCount": str(len(album["songIds"]))})
    for song_id in album["songIds"]:
        song = library["songs"].get(song_id)
        if song:
            song_to_xml(alb_elem, song)
    return subsonic_response(request, root)


@subsonic_endpoint("getSong")
async def subsonic_get_song(request: Request, id: str = Query(...)):
    auth_err = await require_subsonic(request)
    if auth_err:
        return auth_err
    library = await scan_subsonic_library()
    song = library["songs"].get(id)
    if not song:
        return subsonic_error(request, 70, "Song not found.")

    root = subsonic_root()
    song_to_xml(root, song)
    return subsonic_response(request, root)


@subsonic_endpoint("stream")
async def subsonic_stream(request: Request, id: str = Query(...), **kwargs):
    auth_err = await require_subsonic(request)
    if auth_err:
        return auth_err
    library = await scan_subsonic_library()
    song = library["songs"].get(id)
    if not song or not Path(song["_path"]).exists():
        return subsonic_error(request, 70, "Song file not found.")

    file_path = Path(song["_path"])
    return FileResponse(file_path, media_type=song.get("contentType", "audio/mpeg"))


@subsonic_endpoint("getCoverArt")
async def subsonic_cover_art(request: Request, id: str = Query(...), **kwargs):
    auth_err = await require_subsonic(request)
    if auth_err:
        return auth_err

    library = await scan_subsonic_library()
    file_path = None
    if id in library["songs"]:
        file_path = Path(library["songs"][id]["_path"])
    else:
        for cand in library["songs"].values():
            if make_cover_id(Path(cand["_path"])) == id:
                file_path = Path(cand["_path"])
                break

    if not file_path or not file_path.exists():
        return Response(content=b"", media_type="image/jpeg")

    file_hash = hashlib.md5(str(file_path).encode("utf-8")).hexdigest()
    cover_path = COVER_CACHE_DIR / f"{file_hash}.jpg"

    if not cover_path.exists():
        subprocess.run(["ffmpeg", "-y", "-i", str(file_path), "-an", "-vcodec", "mjpeg", "-vframes", "1", str(cover_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if cover_path.exists():
        return FileResponse(cover_path, media_type="image/jpeg")

    return Response(content=b"", media_type="image/jpeg")


# Stubs for secondary Subsonic calls
@subsonic_endpoint("getPlaylists")
@subsonic_endpoint("star")
@subsonic_endpoint("unstar")
@subsonic_endpoint("setRating")
@subsonic_endpoint("scrobble")
async def subsonic_generic_ok(request: Request, **kwargs):
    auth_err = await require_subsonic(request)
    if auth_err:
        return auth_err
    return subsonic_response(request, subsonic_root())


# ============================================================
# STARTUP & DOWNLOAD WORKER
# ============================================================

async def download_worker():
    while True:
        task_id = await task_queue.get()
        task = TASKS.get(task_id)
        if not task:
            task_queue.task_done()
            continue
        try:
            task.update({"status": "downloading", "step": "Downloading stream...", "last_updated": time.time() * 1000})
            await notify_task_update(task, force_save=True)

            settings = load_settings()
            fmt, quality = settings.get("audio_format", "mp3"), settings.get("audio_quality", "320K")
            output_template = str(DOWNLOAD_DIR / f"{task_id}.%(ext)s")

            command = ["yt-dlp", "--no-playlist", "-x", "--audio-format", fmt, "--audio-quality", quality, "-o", output_template, task["url"]]
            proc = await asyncio.create_subprocess_exec(*command)
            await proc.wait()

            possible_files = list(DOWNLOAD_DIR.glob(f"{task_id}.*"))
            if possible_files:
                audio_file = possible_files[0]
                clean_title = clean_filename(task["title"])
                final_path = DOWNLOAD_DIR / f"{clean_title}{audio_file.suffix}"
                shutil.move(str(audio_file), str(final_path))
                task.update({"status": "completed", "percent": 100, "step": "Ready", "final_name": final_path.name})
                await scan_subsonic_library(force=True)
            else:
                task.update({"status": "error", "error": "Download failed"})
        except Exception as e:
            task.update({"status": "error", "error": str(e)})
        finally:
            await notify_task_update(task, force_save=True)
            task_queue.task_done()


@app.on_event("startup")
async def startup_event():
    await asyncio.to_thread(init_db)
    global TASKS
    TASKS = await asyncio.to_thread(_db_load_tasks_sync)
    await scan_subsonic_library(force=True)
    for _ in range(MAX_CONCURRENT_DOWNLOADS):
        asyncio.create_task(download_worker())


# ============================================================
# MANAGEMENT APIs
# ============================================================

@app.get("/api/tasks")
async def get_tasks():
    return list(TASKS.values())

@app.get("/api/library")
async def get_library():
    files = await get_all_audio_files()
    res = [{"name": str(p.relative_to(DOWNLOAD_DIR)), "size": format_size(p.stat().st_size)} for p in files]
    return {"files": res, "total": len(res)}
