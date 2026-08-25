import asyncio
import base64
import hashlib
import html
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.parse
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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles


# ============================================================
# XROB MUSIC
# Direct music library + YouTube downloader
# Subsonic-compatible API for Amperfy
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
# CONFIGURATION
# ============================================================

DOWNLOAD_DIR = Path(
    os.getenv("DOWNLOAD_DIR", "/share/mymusic/music")
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


# ============================================================
# AMPERFY / SUBSONIC CONFIG
#
# Amperfy is a Subsonic client.
#
# Configure through environment variables:
#
# AMPERFY_USER=xrob
# AMPERFY_PASSWORD=your-password
#
# Optional:
# AMPERFY_SERVER_NAME=Xrob Music
# SUBSONIC_VERSION=1.16.1
# ============================================================

AMPERFY_USER = os.getenv(
    "AMPERFY_USER",
    "xrob",
)

AMPERFY_PASSWORD = os.getenv(
    "AMPERFY_PASSWORD",
    "changeme",
)

AMPERFY_SERVER_NAME = os.getenv(
    "AMPERFY_SERVER_NAME",
    "Xrob Music",
)

SUBSONIC_VERSION = os.getenv(
    "SUBSONIC_VERSION",
    "1.16.1",
)

SERVER_VERSION = "1.0.0"


DEFAULT_SETTINGS = {
    "audio_format": "mp3",
    "audio_quality": "320K",
    "embed_thumbnail": True,
    "embed_metadata": True,
    "max_results": 20,
    "organize_by_artist": False,
    "poll_interval": 1500,
}


# ============================================================
# GLOBAL STATE
# ============================================================

TASKS = {}

task_queue = asyncio.Queue()

ACTIVE_PROCESSES = {}

LAST_SAVED_TIME = {}


# ============================================================
# SUBSONIC XML HELPERS
# ============================================================

def xml_escape(value):
    if value is None:
        return ""

    return html.escape(
        str(value),
        quote=True,
    )


def subsonic_response(
    body="",
    status="ok",
    version=SUBSONIC_VERSION,
):
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<subsonic-response '
        f'xmlns="http://subsonic.org/restapi" '
        f'status="{xml_escape(status)}" '
        f'version="{xml_escape(version)}" '
        f'type="Xrob Music" '
        f'serverVersion="{xml_escape(SERVER_VERSION)}">'
        f"{body}"
        "</subsonic-response>"
    )

    return Response(
        content=xml,
        media_type="application/xml; charset=utf-8",
    )


def subsonic_error(
    code=0,
    message="Unknown error",
):
    body = (
        f'<error code="{int(code)}" '
        f'message="{xml_escape(message)}"/>'
    )

    return subsonic_response(
        body=body,
        status="failed",
    )


# ============================================================
# SUBSONIC AUTHENTICATION
# ============================================================

def decode_subsonic_password(password):
    """
    Supports:
      p=plainpassword
      p=enc:<base64-password>
    """

    if not password:
        return ""

    if password.startswith("enc:"):
        encoded = password[4:]

        try:
            return base64.b64decode(
                encoded
            ).decode("utf-8")
        except Exception:
            return ""

    return password


def validate_subsonic_auth(
    username,
    password,
    token,
    salt,
):
    if not username:
        return False

    if username != AMPERFY_USER:
        return False

    # Password authentication
    decoded_password = decode_subsonic_password(password)

    if decoded_password:
        return decoded_password == AMPERFY_PASSWORD

    # Token authentication:
    # t = md5(password + salt)
    if token and salt:
        expected = hashlib.md5(
            (
                AMPERFY_PASSWORD
                + salt
            ).encode("utf-8")
        ).hexdigest()

        return (
            token.lower()
            == expected.lower()
        )

    return False


async def require_subsonic_auth(
    u: Optional[str],
    p: Optional[str],
    t: Optional[str],
    s: Optional[str],
):
    if not validate_subsonic_auth(
        u,
        p,
        t,
        s,
    ):
        return subsonic_error(
            40,
            "Wrong username or password",
        )

    return None


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


def _db_save_task_sync(task):
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
        or (
            now
            - LAST_SAVED_TIME.get(
                task_id,
                0,
            )
            > 0.5
        )
    ):
        LAST_SAVED_TIME[task_id] = now

        await asyncio.to_thread(
            _db_save_task_sync,
            task,
        )


def _db_load_tasks_sync():
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
                'error'
            )
            """
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
        message,
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


def save_settings(data):
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
# FILE HELPERS
# ============================================================

def clean_filename(value):
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
    )

    value = value.strip(" .")

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

        minutes = seconds // 60
        seconds = seconds % 60

        return (
            f"{minutes}:{seconds:02d}"
        )

    except Exception:
        return "0:00"


def format_size(size_bytes):
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
    task_id,
):
    for p in DOWNLOAD_DIR.glob(
        f"*{task_id}*"
    ):
        try:
            if p.is_file():
                p.unlink()

        except Exception:
            pass


def _resolve_file_sync(
    filename,
):
    clean_name = filename.strip()

    base_dir = (
        DOWNLOAD_DIR.resolve()
    )

    file_path = (
        DOWNLOAD_DIR
        / clean_name
    ).resolve()

    if not file_path.is_relative_to(
        base_dir
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

    target_name = (
        Path(clean_name).name
    )

    for match in DOWNLOAD_DIR.rglob(
        "*"
    ):
        if (
            match.is_file()
            and match.name
            == target_name
            and match.resolve().is_relative_to(
                base_dir
            )
        ):
            return match

    raise HTTPException(
        status_code=404,
        detail="File not found",
    )


async def resolve_file(
    filename,
):
    return await asyncio.to_thread(
        _resolve_file_sync,
        filename,
    )


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


# ============================================================
# DUPLICATE DETECTION
# ============================================================

def normalize_duplicate_key(
    value,
):
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


def _is_duplicate_sync(
    title,
):
    key = normalize_duplicate_key(
        title
    )

    files = _get_all_audio_files_sync()

    return any(
        normalize_duplicate_key(
            p.name
        )
        == key
        for p in files
    )


async def is_duplicate(
    title,
):
    return await asyncio.to_thread(
        _is_duplicate_sync,
        title,
    )


# ============================================================
# AUDIO METADATA
# ============================================================

async def ffprobe_metadata(
    path: Path,
):
    """
    Reads basic audio metadata using ffprobe.
    """

    command = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]

    try:
        process = (
            await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        )

        stdout, _ = await process.communicate()

        if process.returncode != 0:
            return {}

        return json.loads(
            stdout.decode(
                "utf-8",
                errors="ignore",
            )
        )

    except Exception:
        return {}


def metadata_from_filename(
    path: Path,
):
    """
    Fallback metadata.

    If organized as:
      Artist/song.mp3

    Artist is taken from directory.

    Otherwise:
      Artist = Unknown Artist
      Title = filename
    """

    title = path.stem

    artist = "Unknown Artist"
    album = "Unknown Album"

    try:
        relative = path.relative_to(
            DOWNLOAD_DIR
        )

        parts = relative.parts

        if len(parts) >= 2:
            artist = parts[0]

    except Exception:
        pass

    return {
        "title": title,
        "artist": artist,
        "album": album,
        "genre": "",
        "track": 0,
        "year": 0,
        "duration": 0,
        "bitrate": 0,
        "size": path.stat().st_size,
    }


async def get_audio_metadata(
    path: Path,
):
    fallback = metadata_from_filename(
        path
    )

    data = await ffprobe_metadata(
        path
    )

    fmt = data.get(
        "format",
        {},
    )

    tags = {
        str(k).lower(): v
        for k, v in (
            fmt.get("tags", {})
            or {}
        ).items()
    }

    streams = data.get(
        "streams",
        [],
    )

    audio_stream = None

    for stream in streams:
        if (
            stream.get("codec_type")
            == "audio"
        ):
            audio_stream = stream
            break

    title = (
        tags.get("title")
        or fallback["title"]
    )

    artist = (
        tags.get("artist")
        or tags.get("album_artist")
        or fallback["artist"]
    )

    album = (
        tags.get("album")
        or fallback["album"]
    )

    genre = (
        tags.get("genre")
        or ""
    )

    try:
        duration = int(
            float(
                fmt.get(
                    "duration",
                    0,
                )
                or 0
            )
        )
    except Exception:
        duration = 0

    try:
        bitrate = int(
            fmt.get(
                "bit_rate",
                0,
            )
            or 0
        )
    except Exception:
        bitrate = 0

    track_value = (
        tags.get("track")
        or "0"
    )

    try:
        track = int(
            str(
                track_value
            ).split("/")[0]
        )
    except Exception:
        track = 0

    year_value = (
        tags.get("date")
        or tags.get("year")
        or "0"
    )

    try:
        year = int(
            str(
                year_value
            )[:4]
        )
    except Exception:
        year = 0

    return {
        "title": title,
        "artist": artist,
        "album": album,
        "genre": genre,
        "track": track,
        "year": year,
        "duration": duration,
        "bitrate": bitrate,
        "size": path.stat().st_size,
        "codec": (
            audio_stream.get("codec_name")
            if audio_stream
            else path.suffix.lstrip(".")
        ),
    }


# ============================================================
# SUBSONIC IDs
# ============================================================

def make_subsonic_id(
    relative_path,
):
    raw = str(
        relative_path
    ).encode("utf-8")

    return (
        "x"
        + base64.urlsafe_b64encode(
            raw
        )
        .decode("ascii")
        .rstrip("=")
    )


def decode_subsonic_id(
    value,
):
    if not value:
        return None

    if value.startswith("x"):
        value = value[1:]

    try:
        padding = (
            "="
            * (
                4
                - len(value) % 4
            )
        )

        decoded = base64.urlsafe_b64decode(
            (
                value
                + padding
            ).encode("ascii")
        )

        relative = decoded.decode(
            "utf-8"
        )

        return awaitable_path_from_relative(
            relative
        )

    except Exception:
        return None


def awaitable_path_from_relative(
    relative,
):
    try:
        base = DOWNLOAD_DIR.resolve()

        path = (
            DOWNLOAD_DIR
            / relative
        ).resolve()

        if not path.is_relative_to(
            base
        ):
            return None

        if (
            not path.exists()
            or not path.is_file()
        ):
            return None

        if (
            path.suffix.lower()
            not in AUDIO_EXTENSIONS
        ):
            return None

        return path

    except Exception:
        return None


async def path_from_subsonic_id(
    value,
):
    return await asyncio.to_thread(
        decode_subsonic_id,
        value,
    )


# ============================================================
# SUBSONIC LIBRARY INDEX
# ============================================================

async def build_music_index():
    files = await get_all_audio_files()

    tracks = []

    for path in files:
        try:
            metadata = (
                await get_audio_metadata(
                    path
                )
            )

            relative = path.relative_to(
                DOWNLOAD_DIR
            )

            track = {
                "id": make_subsonic_id(
                    relative
                ),
                "path": path,
                "relative": str(
                    relative
                ),
                "metadata": metadata,
            }

            tracks.append(track)

        except Exception:
            continue

    return tracks


# ============================================================
# SUBSONIC ARTISTS
# ============================================================

async def build_artists():
    tracks = await build_music_index()

    artists = {}

    for track in tracks:
        meta = track["metadata"]

        artist = (
            meta.get("artist")
            or "Unknown Artist"
        )

        key = artist.lower()

        if key not in artists:
            artists[key] = {
                "name": artist,
                "album_ids": set(),
                "song_ids": [],
            }

        artists[key][
            "song_ids"
        ].append(
            track["id"]
        )

    for artist in artists.values():
        album_keys = set()

        for track in tracks:
            if (
                (
                    track["metadata"].get(
                        "artist"
                    )
                    or "Unknown Artist"
                ).lower()
                == artist["name"].lower()
            ):
                album_keys.add(
                    (
                        track["metadata"].get(
                            "album"
                        )
                        or "Unknown Album"
                    )
                )

        artist["album_count"] = len(
            album_keys
        )

    return artists


# ============================================================
# SUBSONIC ALBUMS
# ============================================================

async def build_albums():
    tracks = await build_music_index()

    albums = {}

    for track in tracks:
        meta = track["metadata"]

        artist = (
            meta.get("artist")
            or "Unknown Artist"
        )

        album = (
            meta.get("album")
            or "Unknown Album"
        )

        key = (
            artist.lower()
            + "|||"
            + album.lower()
        )

        if key not in albums:
            albums[key] = {
                "id": make_subsonic_id(
                    "album:"
                    + key
                ),
                "name": album,
                "artist": artist,
                "song_ids": [],
                "tracks": [],
            }

        albums[key]["song_ids"].append(
            track["id"]
        )

        albums[key]["tracks"].append(
            track
        )

    return albums


# ============================================================
# SUBSONIC SONG XML
# ============================================================

def song_xml(
    track,
    include_album=True,
):
    meta = track["metadata"]

    song_id = track["id"]

    title = (
        meta.get("title")
        or track["path"].stem
    )

    artist = (
        meta.get("artist")
        or "Unknown Artist"
    )

    album = (
        meta.get("album")
        or "Unknown Album"
    )

    duration = int(
        meta.get(
            "duration",
            0,
        )
        or 0
    )

    size = int(
        meta.get(
            "size",
            0,
        )
        or 0
    )

    track_number = int(
        meta.get(
            "track",
            0,
        )
        or 0
    )

    year = int(
        meta.get(
            "year",
            0,
        )
        or 0
    )

    bitrate = int(
        meta.get(
            "bitrate",
            0,
        )
        or 0
    )

    genre = (
        meta.get("genre")
        or ""
    )

    content_type = MEDIA_TYPES.get(
        track["path"].suffix.lower(),
        "audio/mpeg",
    )

    body = (
        f'<song '
        f'id="{xml_escape(song_id)}" '
        f'title="{xml_escape(title)}" '
        f'album="{xml_escape(album)}" '
        f'artist="{xml_escape(artist)}" '
        f'albumArtist="{xml_escape(artist)}" '
        f'year="{year}" '
        f'genre="{xml_escape(genre)}" '
        f'track="{track_number}" '
        f'duration="{duration}" '
        f'size="{size}" '
        f'bitRate="{max(0, bitrate // 1000)}" '
        f'contentType="{xml_escape(content_type)}" '
        f'suffix="{xml_escape(track["path"].suffix.lstrip("."))}" '
        f'isDir="false" '
        f'parent="xrob-library" '
        f'type="music" '
        f'starred="false" '
        f'discNumber="1" '
        f'path="{xml_escape(track["relative"])}"'
        f'/>'
    )

    return body


# ============================================================
# SUBSONIC API
# ============================================================

@app.get("/api/amperfy/rest/{endpoint}")
async def amperfy_subsonic_api(
    endpoint: str,
    u: Optional[str] = Query(None),
    p: Optional[str] = Query(None),
    t: Optional[str] = Query(None),
    s: Optional[str] = Query(None),
    v: Optional[str] = Query(None),
    c: Optional[str] = Query(None),
    f: Optional[str] = Query("xml"),
    id: Optional[str] = Query(None),
    artistId: Optional[str] = Query(None),
    albumId: Optional[str] = Query(None),
    songId: Optional[str] = Query(None),
    musicFolderId: Optional[str] = Query(None),
    query: Optional[str] = Query(None),
    searchTerm: Optional[str] = Query(None),
    genre: Optional[str] = Query(None),
    size: Optional[int] = Query(20),
    offset: Optional[int] = Query(0),
    count: Optional[int] = Query(20),
):
    # --------------------------------------------------------
    # AUTH
    # --------------------------------------------------------

    auth_error = await require_subsonic_auth(
        u,
        p,
        t,
        s,
    )

    if auth_error:
        return auth_error

    endpoint = endpoint.lower()

    # --------------------------------------------------------
    # PING
    # --------------------------------------------------------

    if endpoint == "ping":
        return subsonic_response()

    # --------------------------------------------------------
    # GET LICENSE
    # --------------------------------------------------------

    if endpoint == "getLicense":
        body = (
            '<license valid="true" '
            'email="" '
            'licenseExpires="2099-12-31T23:59:59"/>'
        )

        return subsonic_response(
            body
        )

    # --------------------------------------------------------
    # GET MUSIC FOLDERS
    # --------------------------------------------------------

    if endpoint == "getMusicFolders":
        body = (
            '<musicFolders>'
            '<musicFolder id="1" '
            f'name="{xml_escape(AMPERFY_SERVER_NAME)}"/>'
            '</musicFolders>'
        )

        return subsonic_response(
            body
        )

    # --------------------------------------------------------
    # GET INDEXES
    # --------------------------------------------------------

    if endpoint == "getIndexes":
        artists = await build_artists()

        body = "<indexes lastModified='0'>"

        grouped = {}

        for artist in artists.values():
            name = artist["name"]

            first = (
                name[:1].upper()
                if name
                else "#"
            )

            if not first.isalpha():
                first = "#"

            grouped.setdefault(
                first,
                [],
            ).append(
                artist
            )

        for index_name in sorted(
            grouped.keys()
        ):
            body += (
                f'<index name="{xml_escape(index_name)}">'
            )

            for artist in sorted(
                grouped[index_name],
                key=lambda x: x["name"].lower(),
            ):
                artist_id = (
                    "artist:"
                    + base64.urlsafe_b64encode(
                        artist["name"]
                        .encode("utf-8")
                    )
                    .decode("ascii")
                    .rstrip("=")
                )

                body += (
                    f'<artist '
                    f'id="{xml_escape(artist_id)}" '
                    f'name="{xml_escape(artist["name"])}" '
                    f'albumCount="{artist["album_count"]}"/>'
                )

            body += "</index>"

        body += "</indexes>"

        return subsonic_response(
            body
        )

    # --------------------------------------------------------
    # GET MUSIC DIRECTORY
    # --------------------------------------------------------

    if endpoint == "getMusicDirectory":
        if not id:
            return subsonic_error(
                10,
                "Missing id",
            )

        tracks = await build_music_index()

        body = "<directory>"

        # Root
        if id in (
            "1",
            "root",
            "xrob-library",
        ):
            artists = await build_artists()

            for artist in sorted(
                artists.values(),
                key=lambda x: x["name"].lower(),
            ):
                artist_id = (
                    "artist:"
                    + base64.urlsafe_b64encode(
                        artist["name"]
                        .encode("utf-8")
                    )
                    .decode("ascii")
                    .rstrip("=")
                )

                body += (
                    f'<child '
                    f'id="{xml_escape(artist_id)}" '
                    f'parent="xrob-library" '
                    f'title="{xml_escape(artist["name"])}" '
                    f'isDir="true" '
                    f'albumCount="{artist["album_count"]}" '
                    f'artist="{xml_escape(artist["name"])}" '
                    f'type="music"/>'
                )

            body += "</directory>"

            return subsonic_response(
                body
            )

        # Artist directory
        if id.startswith("artist:"):
            try:
                encoded = id[
                    len("artist:") :
                ]

                padding = (
                    "="
                    * (
                        4
                        - len(encoded) % 4
                    )
                )

                artist_name = base64.urlsafe_b64decode(
                    (
                        encoded
                        + padding
                    ).encode("ascii")
                ).decode("utf-8")

            except Exception:
                return subsonic_error(
                    70,
                    "Invalid artist",
                )

            albums = await build_albums()

            for album in sorted(
                albums.values(),
                key=lambda x: x["name"].lower(),
            ):
                if (
                    album["artist"].lower()
                    != artist_name.lower()
                ):
                    continue

                body += (
                    f'<child '
                    f'id="{xml_escape(album["id"])}" '
                    f'parent="{xml_escape(id)}" '
                    f'title="{xml_escape(album["name"])}" '
                    f'album="{xml_escape(album["name"])}" '
                    f'artist="{xml_escape(album["artist"])}" '
                    f'isDir="true" '
                    f'type="music"/>'
                )

            body += "</directory>"

            return subsonic_response(
                body
            )

        # Album directory
        if id.startswith("x"):
            album_tracks = []

            for track in tracks:
                if track["id"] == id:
                    album_tracks.append(
                        track
                    )

            if album_tracks:
                body += "".join(
                    song_xml(
                        track
                    )
                    for track in album_tracks
                )

                body += "</directory>"

                return subsonic_response(
                    body
                )

        # Search for matching album ID
        albums = await build_albums()

        for album in albums.values():
            if album["id"] == id:
                for track in sorted(
                    album["tracks"],
                    key=lambda x: (
                        x["metadata"].get(
                            "track",
                            0,
                        ),
                        x["metadata"].get(
                            "title",
                            "",
                        ).lower(),
                    ),
                ):
                    body += song_xml(
                        track
                    )

                body += "</directory>"

                return subsonic_response(
                    body
                )

        body += "</directory>"

        return subsonic_response(
            body
        )

    # --------------------------------------------------------
    # GET ARTISTS
    # --------------------------------------------------------

    if endpoint == "getArtists":
        artists = await build_artists()

        body = "<artists lastModified='0'>"

        grouped = {}

        for artist in artists.values():
            first = (
                artist["name"][:1].upper()
                if artist["name"]
                else "#"
            )

            if not first.isalpha():
                first = "#"

            grouped.setdefault(
                first,
                [],
            ).append(
                artist
            )

        for index_name in sorted(
            grouped.keys()
        ):
            body += (
                f'<index name="{xml_escape(index_name)}">'
            )

            for artist in sorted(
                grouped[index_name],
                key=lambda x: x["name"].lower(),
            ):
                artist_id = (
                    "artist:"
                    + base64.urlsafe_b64encode(
                        artist["name"]
                        .encode("utf-8")
                    )
                    .decode("ascii")
                    .rstrip("=")
                )

                body += (
                    f'<artist '
                    f'id="{xml_escape(artist_id)}" '
                    f'name="{xml_escape(artist["name"])}" '
                    f'albumCount="{artist["album_count"]}"/>'
                )

            body += "</index>"

        body += "</artists>"

        return subsonic_response(
            body
        )

    # --------------------------------------------------------
    # GET ARTIST
    # --------------------------------------------------------

    if endpoint == "getArtist":
        target_id = artistId or id

        if not target_id:
            return subsonic_error(
                10,
                "Missing artistId",
            )

        if target_id.startswith(
            "artist:"
        ):
            encoded = target_id[
                len("artist:") :
            ]

            try:
                padding = (
                    "="
                    * (
                        4
                        - len(encoded) % 4
                    )
                )

                artist_name = (
                    base64.urlsafe_b64decode(
                        (
                            encoded
                            + padding
                        ).encode("ascii")
                    )
                    .decode("utf-8")
                )

            except Exception:
                return subsonic_error(
                    70,
                    "Invalid artist",
                )
        else:
            artist_name = target_id

        albums = await build_albums()

        body = ""

        for album in sorted(
            albums.values(),
            key=lambda x: x["name"].lower(),
        ):
            if (
                album["artist"].lower()
                != artist_name.lower()
            ):
                continue

            album_id = album["id"]

            body += (
                f'<album '
                f'id="{xml_escape(album_id)}" '
                f'name="{xml_escape(album["name"])}" '
                f'artist="{xml_escape(album["artist"])}" '
                f'artistId="{xml_escape(target_id)}" '
                f'year="0" '
                f'genre="" '
                f'coverArt="{xml_escape(album_id)}" '
                f'playCount="0" '
                f'duration="0" '
                f'songCount="{len(album["tracks"])}"/>'
            )

        artist_id = (
            target_id
        )

        body = (
            f'<artist '
            f'id="{xml_escape(artist_id)}" '
            f'name="{xml_escape(artist_name)}">'
            f"{body}"
            "</artist>"
        )

        return subsonic_response(
            body
        )

    # --------------------------------------------------------
    # GET ALBUM
    # --------------------------------------------------------

    if endpoint == "getAlbum":
        target_id = albumId or id

        if not target_id:
            return subsonic_error(
                10,
                "Missing albumId",
            )

        albums = await build_albums()

        selected = None

        for album in albums.values():
            if album["id"] == target_id:
                selected = album
                break

        if not selected:
            return subsonic_error(
                70,
                "Album not found",
            )

        body = (
            f'<album '
            f'id="{xml_escape(selected["id"])}" '
            f'name="{xml_escape(selected["name"])}" '
            f'artist="{xml_escape(selected["artist"])}" '
            f'coverArt="{xml_escape(selected["id"])}" '
            f'year="0" '
            f'genre="" '
            f'songCount="{len(selected["tracks"])}">'
        )

        for track in sorted(
            selected["tracks"],
            key=lambda x: (
                x["metadata"].get(
                    "track",
                    0,
                ),
                x["metadata"].get(
                    "title",
                    "",
                ).lower(),
            ),
        ):
            body += song_xml(
                track
            )

        body += "</album>"

        return subsonic_response(
            body
        )

    # --------------------------------------------------------
    # GET SONG
    # --------------------------------------------------------

    if endpoint == "getSong":
        target_id = songId or id

        if not target_id:
            return subsonic_error(
                10,
                "Missing song id",
            )

        path = await path_from_subsonic_id(
            target_id
        )

        if not path:
            return subsonic_error(
                70,
                "Song not found",
            )

        tracks = await build_music_index()

        selected = None

        for track in tracks:
            if track["id"] == target_id:
                selected = track
                break

        if not selected:
            return subsonic_error(
                70,
                "Song not found",
            )

        return subsonic_response(
            song_xml(
                selected
            )
        )

    # --------------------------------------------------------
    # STREAM
    # --------------------------------------------------------

    if endpoint == "stream":
        target_id = id or songId

        if not target_id:
            return subsonic_error(
                10,
                "Missing id",
            )

        path = await path_from_subsonic_id(
            target_id
        )

        if not path:
            return subsonic_error(
                70,
                "Song not found",
            )

        media_type = MEDIA_TYPES.get(
            path.suffix.lower(),
            "audio/mpeg",
        )

        return FileResponse(
            path,
            media_type=media_type,
            headers={
                "Accept-Ranges": "bytes",
                "Cache-Control": "no-cache",
                "Content-Disposition": (
                    f'inline; filename="{path.name}"'
                ),
            },
        )

    # --------------------------------------------------------
    # DOWNLOAD
    # --------------------------------------------------------

    if endpoint == "download":
        target_id = id or songId

        if not target_id:
            return subsonic_error(
                10,
                "Missing id",
            )

        path = await path_from_subsonic_id(
            target_id
        )

        if not path:
            return subsonic_error(
                70,
                "Song not found",
            )

        return FileResponse(
            path,
            media_type=MEDIA_TYPES.get(
                path.suffix.lower(),
                "application/octet-stream",
            ),
            filename=path.name,
        )

    # --------------------------------------------------------
    # GET COVER ART
    # --------------------------------------------------------

    if endpoint == "getCoverArt":
        target_id = id

        if not target_id:
            return Response(
                content=await create_fallback_cover(),
                media_type="image/svg+xml",
            )

        # Song ID
        path = await path_from_subsonic_id(
            target_id
        )

        if path:
            return await serve_cover_for_file(
                path
            )

        # Album ID
        albums = await build_albums()

        for album in albums.values():
            if album["id"] == target_id:
                if album["tracks"]:
                    return await serve_cover_for_file(
                        album["tracks"][0]["path"]
                    )

        return Response(
            content=await create_fallback_cover(),
            media_type="image/svg+xml",
        )

    # --------------------------------------------------------
    # SEARCH
    # --------------------------------------------------------

    if endpoint in (
        "search2",
        "search3",
        "search",
    ):
        search_value = (
            query
            or searchTerm
            or ""
        ).strip().lower()

        tracks = await build_music_index()

        matching = []

        for track in tracks:
            meta = track["metadata"]

            haystack = " ".join(
                [
                    str(
                        meta.get(
                            "title",
                            "",
                        )
                    ),
                    str(
                        meta.get(
                            "artist",
                            "",
                        )
                    ),
                    str(
                        meta.get(
                            "album",
                            "",
                        )
                    ),
                ]
            ).lower()

            if (
                not search_value
                or search_value in haystack
            ):
                matching.append(
                    track
                )

        matching = matching[
            offset : offset
            + count
        ]

        body = "<searchResult3>"

        for track in matching:
            body += song_xml(
                track
            )

        body += "</searchResult3>"

        return subsonic_response(
            body
        )

    # --------------------------------------------------------
    # GET GENRES
    # --------------------------------------------------------

    if endpoint == "getGenres":
        tracks = await build_music_index()

        genres = set()

        for track in tracks:
            genre_value = (
                track["metadata"].get(
                    "genre"
                )
            )

            if genre_value:
                genres.add(
                    genre_value
                )

        body = "<genres>"

        for genre_value in sorted(
            genres,
            key=lambda x: x.lower(),
        ):
            body += (
                f'<genre '
                f'value="{xml_escape(genre_value)}" '
                f'songCount="0" '
                f'albumCount="0"/>'
            )

        body += "</genres>"

        return subsonic_response(
            body
        )

    # --------------------------------------------------------
    # GET SONGS BY GENRE
    # --------------------------------------------------------

    if endpoint == "getSongsByGenre":
        tracks = await build_music_index()

        target_genre = (
            genre
            or ""
        ).lower()

        matching = []

        for track in tracks:
            if (
                track["metadata"].get(
                    "genre",
                    "",
                ).lower()
                == target_genre
            ):
                matching.append(
                    track
                )

        matching = matching[
            offset : offset
            + count
        ]

        body = "<songsByGenre>"

        for track in matching:
            body += song_xml(
                track
            )

        body += "</songsByGenre>"

        return subsonic_response(
            body
        )

    # --------------------------------------------------------
    # GET RANDOM SONGS
    # --------------------------------------------------------

    if endpoint == "getRandomSongs":
        tracks = await build_music_index()

        import random

        random.shuffle(
            tracks
        )

        limit = min(
            int(size or 20),
            len(tracks),
        )

        body = "<randomSongs>"

        for track in tracks[:limit]:
            body += song_xml(
                track
            )

        body += "</randomSongs>"

        return subsonic_response(
            body
        )

    # --------------------------------------------------------
    # GET NOW PLAYING
    # --------------------------------------------------------

    if endpoint == "getNowPlaying":
        return subsonic_response(
            "<nowPlaying/>"
        )

    # --------------------------------------------------------
    # STAR / UNSTAR
    #
    # We don't need these for the local library yet.
    # Return success so clients don't break.
    # --------------------------------------------------------

    if endpoint in (
        "star",
        "unstar",
        "setRating",
        "scrobble",
    ):
        return subsonic_response()

    # --------------------------------------------------------
    # UNKNOWN ENDPOINT
    # --------------------------------------------------------

    return subsonic_error(
        0,
        f"Unsupported Subsonic endpoint: {endpoint}",
    )


# ============================================================
# COVER ART
# ============================================================

async def create_fallback_cover():
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'width="500" height="500" viewBox="0 0 500 500">'
        '<rect width="100%" height="100%" fill="#111827"/>'
        '<circle cx="250" cy="250" r="150" fill="#1f2937"/>'
        '<text x="250" y="285" '
        'fill="#9ca3af" '
        'font-size="120" '
        'text-anchor="middle">♪</text>'
        "</svg>"
    )


async def serve_cover_for_file(
    file_path,
):
    file_hash = hashlib.md5(
        str(file_path).encode(
            "utf-8"
        )
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
                "Access-Control-Allow-Origin": "*"
            },
        )

    def extract_cover():
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
            result = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )

            if (
                result.returncode == 0
                and cover_path.exists()
                and cover_path.stat().st_size > 0
            ):
                return True

        except Exception:
            pass

        return False

    success = await asyncio.to_thread(
        extract_cover
    )

    if success:
        return FileResponse(
            cover_path,
            media_type="image/jpeg",
            headers={
                "Access-Control-Allow-Origin": "*"
            },
        )

    return Response(
        content=await create_fallback_cover(),
        media_type="image/svg+xml",
        headers={
            "Access-Control-Allow-Origin": "*"
        },
    )


# ============================================================
# NORMAL XROB API
# ============================================================

@app.get("/")
async def home():
    return FileResponse(
        STATIC_DIR / "index.html"
    )


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
# YOUTUBE SEARCH
# ============================================================

async def youtube_search(
    query,
    max_results,
    page=1,
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
                    "duration_text": format_duration(
                        duration
                    ),
                    "thumbnail": (
                        item.get(
                            "thumbnail"
                        )
                        or (
                            "https://i.ytimg.com/vi/"
                            + video_id
                            + "/hqdefault.jpg"
                        )
                    ),
                    "url": (
                        "https://www.youtube.com/watch?v="
                        + video_id
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
    )

    try:
        return await youtube_search(
            q,
            max_results=max_results,
            page=page,
        )

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e),
        )


# ============================================================
# PREVIEW
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
            raise HTTPException(
                status_code=500,
                detail=(
                    "Failed to fetch preview audio URL"
                ),
            )

        direct_url = (
            stdout.decode()
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
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-cache",
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
# DOWNLOAD WORKER
# ============================================================

async def download_worker():
    while True:
        task_id = await task_queue.get()

        task = TASKS.get(
            task_id
        )

        if not task:
            task_queue.task_done()
            continue

        try:
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
                "--embed-subs",
                "--sub-langs",
                "all,-live_chat",
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

                    spd_match = (
                        speed_regex.search(
                            line_str
                        )
                    )

                    if spd_match:
                        task["speed"] = (
                            spd_match.group(1)
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

                task["status"] = (
                    "error"
                )

                task["error"] = (
                    err_text[-300:]
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

            await process_clean.wait()

            if (
                process_clean.returncode == 0
                and cleaned_file.exists()
            ):
                audio_file.unlink()

                audio_file = (
                    cleaned_file
                )

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
                final_dir = (
                    DOWNLOAD_DIR
                )

            final_name = (
                f"{clean_title}{ext}"
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
                    f"{ext}"
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

            task["step"] = (
                "Ready"
            )

            task["last_updated"] = (
                time.time() * 1000
            )

            await notify_task_update(
                task,
                force_save=True,
            )

        except Exception as err:
            await asyncio.to_thread(
                cleanup_task_files,
                task_id,
            )

            task["status"] = (
                "error"
            )

            task["error"] = str(err)

            task["last_updated"] = (
                time.time() * 1000
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

    # Old queued/downloading jobs should
    # not stay falsely active after restart.
    for task in TASKS.values():
        if task.get("status") in (
            "queued",
            "downloading",
            "processing",
        ):
            task["status"] = (
                "error"
            )

            task["step"] = (
                "Interrupted by restart"
            )

            task["error"] = (
                "Download interrupted because "
                "the server restarted."
            )

            task["last_updated"] = (
                time.time() * 1000
            )

    for _ in range(
        MAX_CONCURRENT_DOWNLOADS
    ):
        asyncio.create_task(
            download_worker()
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

    task_id = str(
        uuid.uuid4()
    )[:8]

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
        "last_updated": (
            time.time() * 1000
        ),
        "final_name": "",
    }

    TASKS[
        task_id
    ] = task

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
# TASK API
# ============================================================

@app.get("/api/tasks")
async def get_tasks():
    return list(
        TASKS.values()
    )


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

    task[
        "cancel_requested"
    ] = True

    if task_id in ACTIVE_PROCESSES:
        proc = ACTIVE_PROCESSES[
            task_id
        ]

        try:
            proc.terminate()
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
        "status": "cancelled"
    }


@app.delete(
    "/api/tasks/clear-completed"
)
async def clear_completed_tasks():
    global TASKS

    to_remove = [
        tid
        for tid, task in TASKS.items()
        if task.get("status")
        in (
            "completed",
            "cancelled",
            "error",
        )
    ]

    for task_id in to_remove:
        TASKS.pop(
            task_id,
            None,
        )

    await asyncio.to_thread(
        _db_clear_completed_tasks_sync
    )

    await manager.broadcast(
        {
            "type": "task_update"
        }
    )

    return {
        "status": "cleared",
        "count": len(to_remove),
    }


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
            size = path.stat().st_size

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

        return (
            files,
            total_bytes,
        )

    files, total_bytes = (
        await asyncio.to_thread(
            build
        )
    )

    return {
        "files": sorted(
            files,
            key=lambda x: x["name"],
        ),
        "total_size": format_size(
            total_bytes
        ),
        "total_bytes": total_bytes,
    }


# ============================================================
# STATS
# ============================================================

@app.get("/api/stats")
async def get_stats():
    files = (
        await get_all_audio_files()
    )

    def build():
        total_bytes = sum(
            p.stat().st_size
            for p in files
        )

        artists = set()
        albums = set()

        for path in files:
            relative = path.relative_to(
                DOWNLOAD_DIR
            )

            parts = relative.parts

            if len(parts) > 1:
                artists.add(
                    parts[0]
                )

            else:
                artists.add(
                    "Unknown Artist"
                )

            albums.add(
                path.stem
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
        build
    )

    return {
        "tracks": tracks_count,
        "artists": artists_count,
        "albums": albums_count,
        "total_bytes": total_bytes,
        "folder_size": format_size(
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

    return await serve_cover_for_file(
        file_path
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
    file_path = (
        await resolve_file(
            filename
        )
    )

    ext = (
        file_path.suffix.lower()
    )

    media_type = MEDIA_TYPES.get(
        ext,
        "audio/mpeg",
    )

    return FileResponse(
        file_path,
        media_type=media_type,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-cache",
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
    file_path = (
        await resolve_file(
            filename
        )
    )

    try:
        file_hash = hashlib.md5(
            str(file_path).encode(
                "utf-8"
            )
        ).hexdigest()

        cover_path = (
            COVER_CACHE_DIR
            / f"{file_hash}.jpg"
        )

        file_path.unlink()

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
                + str(e)
            ),
        )


# ============================================================
# AMPERFY INFO ENDPOINT
# ============================================================

@app.get("/api/amperfy")
async def amperfy_info():
    return {
        "name": AMPERFY_SERVER_NAME,
        "type": "Subsonic",
        "subsonic_version": SUBSONIC_VERSION,
        "api_base": "/api/amperfy/rest",
        "library": str(
            DOWNLOAD_DIR
        ),
        "navidrome": False,
    }
