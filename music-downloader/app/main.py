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


# ============================================================
# XROB MUSIC
# Native Subsonic-compatible music server
#
# Compatible with:
# - Amperfy
# - Arpeggi
# - Other Subsonic-compatible clients
#
# No Navidrome required.
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

    # Legacy Navidrome settings kept for compatibility.
    "navidrome_url": os.getenv("NAVIDROME_URL", ""),
    "navidrome_user": os.getenv("NAVIDROME_USER", ""),
    "navidrome_token": os.getenv("NAVIDROME_TOKEN", ""),
    "navidrome_salt": os.getenv("NAVIDROME_SALT", ""),

    # Xrob Music Subsonic credentials.
    "subsonic_user": os.getenv(
        "SUBSONIC_USER",
        "admin",
    ),
    "subsonic_password": os.getenv(
        "SUBSONIC_PASSWORD",
        "",
    ),
    "subsonic_token": os.getenv(
        "SUBSONIC_TOKEN",
        "",
    ),
    "subsonic_salt": os.getenv(
        "SUBSONIC_SALT",
        "",
    ),
}


# ============================================================
# GLOBAL STATE
# ============================================================

TASKS = {}

task_queue = asyncio.Queue()

ACTIVE_PROCESSES = {}

LAST_SAVED_TIME = {}


# ============================================================
# SUBSONIC LIBRARY CACHE
# ============================================================

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
    """
    Generate a stable ID for a music file.

    The ID does not expose the actual filesystem path.
    """

    try:
        relative = str(
            path.relative_to(DOWNLOAD_DIR)
        )
    except Exception:
        relative = str(path)

    digest = hashlib.sha1(
        relative.encode("utf-8")
    ).hexdigest()[:24]

    return f"song-{digest}"


def make_album_id(artist: str, album: str) -> str:
    value = f"{artist}\0{album}"

    digest = hashlib.sha1(
        value.encode("utf-8")
    ).hexdigest()[:24]

    return f"album-{digest}"


def make_artist_id(artist: str) -> str:
    digest = hashlib.sha1(
        artist.encode("utf-8")
    ).hexdigest()[:24]

    return f"artist-{digest}"


def make_cover_id(path: Path) -> str:
    return make_subsonic_id(path)


# ============================================================
# SUBSONIC XML / JSON HELPERS
# ============================================================

def xml_escape(value):
    if value is None:
        return ""

    return str(value)


def add_xml_value(parent, tag, value):
    if value is None:
        return None

    element = ET.SubElement(
        parent,
        tag,
    )

    element.text = str(value)

    return element


def subsonic_response(
    request: Request,
    root_element: ET.Element,
):
    """
    Return JSON when f=json / f=json2 is requested.
    Otherwise return Subsonic-compatible XML.
    """

    fmt = request.query_params.get(
        "f",
        "",
    ).lower()

    # --------------------------------------------------------
    # JSON
    # --------------------------------------------------------

    if fmt in {
        "json",
        "json2",
    }:

        def element_to_json(element):
            result = {}

            for key, value in element.attrib.items():
                result[key] = value

            children = list(element)

            if not children:
                if element.text:
                    result["value"] = element.text

                return result

            grouped = {}

            for child in children:
                child_data = element_to_json(child)

                grouped.setdefault(
                    child.tag,
                    [],
                ).append(
                    child_data
                )

            for key, values in grouped.items():

                if len(values) == 1:
                    result[key] = values[0]
                else:
                    result[key] = values

            return result

        data = element_to_json(
            root_element
        )

        return Response(
            content=json.dumps(
                data,
                ensure_ascii=False,
            ),
            media_type="application/json",
        )

    # --------------------------------------------------------
    # XML
    # --------------------------------------------------------

    xml_bytes = ET.tostring(
        root_element,
        encoding="utf-8",
        xml_declaration=True,
    )

    return Response(
        content=xml_bytes,
        media_type="application/xml",
    )


def subsonic_root(
    status="ok",
):
    root = ET.Element(
        "subsonic-response",
        {
            "status": status,
            "version": SUBSONIC_VERSION,
            "type": SUBSONIC_SERVER_TYPE,
            "serverVersion": SUBSONIC_SERVER_VERSION,
            "openSubsonic": "false",
        },
    )

    return root


def subsonic_error(
    request: Request,
    code: int,
    message: str,
):
    root = subsonic_root(
        status="failed"
    )

    ET.SubElement(
        root,
        "error",
        {
            "code": str(code),
            "message": message,
        },
    )

    return subsonic_response(
        request,
        root,
    )


# ============================================================
# SUBSONIC AUTHENTICATION
# ============================================================

def get_subsonic_credentials():
    settings = load_settings()

    username = (
        settings.get("subsonic_user")
        or os.getenv(
            "SUBSONIC_USER",
            "admin",
        )
    )

    password = (
        settings.get("subsonic_password")
        if settings.get("subsonic_password")
        is not None
        else os.getenv(
            "SUBSONIC_PASSWORD",
            "",
        )
    )

    token = (
        settings.get("subsonic_token")
        or os.getenv(
            "SUBSONIC_TOKEN",
            "",
        )
    )

    salt = (
        settings.get("subsonic_salt")
        or os.getenv(
            "SUBSONIC_SALT",
            "",
        )
    )

    return (
        username,
        password or "",
        token or "",
        salt or "",
    )


def verify_subsonic_auth(request: Request):
    username = request.query_params.get(
        "u",
        "",
    )

    password = request.query_params.get(
        "p",
        "",
    )

    token = request.query_params.get(
        "t",
        "",
    )

    expected_user, expected_password, expected_token, expected_salt = (
        get_subsonic_credentials()
    )

    # --------------------------------------------------------
    # Username
    # --------------------------------------------------------

    if not username:
        return False

    if username != expected_user:
        return False

    # --------------------------------------------------------
    # Token authentication
    #
    # Subsonic:
    # t = md5(password + salt)
    # --------------------------------------------------------

    if token and expected_token:
        if hmac_compare(
            token,
            expected_token,
        ):
            return True

    if token and expected_password:
        salt = (
            expected_salt
            or request.query_params.get(
                "s",
                "",
            )
        )

        if salt:
            expected = hashlib.md5(
                (
                    expected_password
                    + salt
                ).encode("utf-8")
            ).hexdigest()

            if hmac_compare(
                token,
                expected,
            ):
                return True

    # --------------------------------------------------------
    # Password authentication
    #
    # p can be:
    # - plaintext password
    # - enc:password
    # --------------------------------------------------------

    if password.startswith("enc:"):
        try:
            password = base64.b64decode(
                password[4:]
            ).decode("utf-8")
        except Exception:
            return False

    if hmac_compare(
        password,
        expected_password,
    ):
        return True

    return False


def hmac_compare(a, b):
    return hashlib.sha256(
        str(a).encode("utf-8")
    ).digest() == hashlib.sha256(
        str(b).encode("utf-8")
    ).digest()


# ============================================================
# PERSISTENT TASK STORAGE
# ============================================================

def init_db():
    with sqlite3.connect(
        DB_FILE
    ) as conn:

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

    with sqlite3.connect(
        DB_FILE
    ) as conn:

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

    with sqlite3.connect(
        DB_FILE
    ) as conn:

        conn.row_factory = sqlite3.Row

        cursor = conn.cursor()

        cursor.execute(
            "SELECT * FROM tasks"
        )

        for row in cursor.fetchall():
            task = dict(row)

            tasks[
                task["id"]
            ] = task

    return tasks


def _db_clear_completed_tasks_sync():

    with sqlite3.connect(
        DB_FILE
    ) as conn:

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
# GENERAL HELPERS
# ============================================================

def normalize_duplicate_key(
    value: str,
):

    key = Path(
        value or ""
    ).stem.lower()

    key = re.sub(
        r"\b(official\s*(video|audio|music video)|lyrics?|hd|4k|remaster(ed)?|audio)\b",
        " ",
        key,
        flags=re.I,
    )

    key = re.sub(
        r"[^a-z0-9]+",
        "",
        key,
    )

    return key


def clean_filename(
    value: str,
):

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
                1024 * 1024
            )
        )

        return f"{mb:.1f} MB"

    except Exception:

        return "0 MB"


# ============================================================
# FILE DISCOVERY
# ============================================================

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
):

    key = normalize_duplicate_key(
        title
    )

    files = (
        _get_all_audio_files_sync()
    )

    return any(
        normalize_duplicate_key(
            p.name
        )
        == key
        for p in files
    )


async def is_duplicate(
    title: str,
):

    return await asyncio.to_thread(
        _is_duplicate_sync,
        title,
    )


# ============================================================
# FFPROBE METADATA
# ============================================================

def ffprobe_metadata_sync(
    path: Path,
):

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

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )

        if result.returncode != 0:
            return {}

        return json.loads(
            result.stdout.decode(
                "utf-8",
                errors="ignore",
            )
        )

    except Exception:
        return {}


def first_metadata(
    tags,
    *names,
):

    if not tags:
        return ""

    lowered = {
        str(k).lower(): v
        for k, v in tags.items()
    }

    for name in names:

        value = lowered.get(
            name.lower()
        )

        if value is not None:
            return str(value)

    return ""


def parse_music_metadata(
    path: Path,
):

    data = ffprobe_metadata_sync(
        path
    )

    fmt = data.get(
        "format",
        {},
    )

    tags = fmt.get(
        "tags",
        {},
    )

    streams = data.get(
        "streams",
        [],
    )

    audio_stream = next(
        (
            stream
            for stream in streams
            if stream.get("codec_type")
            == "audio"
        ),
        {},
    )

    title = first_metadata(
        tags,
        "title",
    )

    artist = first_metadata(
        tags,
        "artist",
        "album_artist",
    )

    album_artist = first_metadata(
        tags,
        "album_artist",
    )

    album = first_metadata(
        tags,
        "album",
    )

    genre = first_metadata(
        tags,
        "genre",
    )

    year = first_metadata(
        tags,
        "date",
        "year",
    )

    track = first_metadata(
        tags,
        "track",
    )

    disc = first_metadata(
        tags,
        "disc",
    )

    composer = first_metadata(
        tags,
        "composer",
    )

    comment = first_metadata(
        tags,
        "comment",
    )

    duration = fmt.get(
        "duration",
        0,
    )

    try:
        duration = int(
            float(duration)
        )
    except Exception:
        duration = 0

    size = path.stat().st_size

    try:
        bitrate = int(
            float(
                fmt.get(
                    "bit_rate",
                    0,
                )
                or 0
            )
        )
    except Exception:
        bitrate = 0

    if not title:
        title = path.stem

    if not artist:
        artist = (
            path.parent.name
            if path.parent != DOWNLOAD_DIR
            else "Unknown Artist"
        )

    if not album_artist:
        album_artist = artist

    if not album:
        album = "Unknown Album"

    if not genre:
        genre = "Unknown"

    if not year:
        year = ""

    try:
        track_number = int(
            str(track).split("/")[0]
        )
    except Exception:
        track_number = 0

    try:
        disc_number = int(
            str(disc).split("/")[0]
        )
    except Exception:
        disc_number = 1

    return {
        "title": title,
        "artist": artist,
        "albumArtist": album_artist,
        "album": album,
        "genre": genre,
        "year": year,
        "track": track_number,
        "discNumber": disc_number,
        "duration": duration,
        "size": size,
        "bitRate": bitrate,
        "contentType": MEDIA_TYPES.get(
            path.suffix.lower(),
            mimetypes.guess_type(
                str(path)
            )[0]
            or "audio/mpeg",
        ),
        "suffix": path.suffix.lower(),
        "composer": composer,
        "comment": comment,
    }


async def scan_subsonic_library(
    force=False,
):

    now = time.time()

    if (
        not force
        and LIBRARY_CACHE["files"]
        and (
            now
            - LIBRARY_CACHE["last_scan"]
            < 15
        )
    ):
        return LIBRARY_CACHE

    files = await get_all_audio_files()

    previous_songs = LIBRARY_CACHE.get(
        "songs",
        {},
    )

    songs = {}

    for path in files:

        try:

            song_id = make_subsonic_id(
                path
            )

            stat = path.stat()

            cache_entry = previous_songs.get(
                song_id
            )

            if (
                cache_entry
                and cache_entry.get(
                    "_mtime"
                )
                == stat.st_mtime
                and cache_entry.get(
                    "_size"
                )
                == stat.st_size
            ):

                metadata = dict(
                    cache_entry
                )

            else:

                metadata = (
                    await asyncio.to_thread(
                        parse_music_metadata,
                        path,
                    )
                )

            metadata["_mtime"] = (
                stat.st_mtime
            )

            metadata["_size"] = (
                stat.st_size
            )

            metadata["_path"] = str(
                path
            )

            metadata["id"] = song_id

            songs[song_id] = metadata

        except Exception:
            continue

    artists = {}

    albums = {}

    for song_id, song in songs.items():

        artist = (
            song.get("albumArtist")
            or song.get("artist")
            or "Unknown Artist"
        )

        album = (
            song.get("album")
            or "Unknown Album"
        )

        artist_id = make_artist_id(
            artist
        )

        album_id = make_album_id(
            artist,
            album,
        )

        if artist_id not in artists:

            artists[artist_id] = {
                "id": artist_id,
                "name": artist,
                "albumIds": [],
                "songs": [],
            }

        if album_id not in albums:

            albums[album_id] = {
                "id": album_id,
                "name": album,
                "artist": artist,
                "artistId": artist_id,
                "songIds": [],
                "songs": [],
                "year": song.get(
                    "year",
                    "",
                ),
                "genre": song.get(
                    "genre",
                    "",
                ),
            }

            artists[
                artist_id
            ]["albumIds"].append(
                album_id
            )

        artists[
            artist_id
        ]["songs"].append(
            song_id
        )

        albums[
            album_id
        ]["songIds"].append(
            song_id
        )

    # Sort albums and songs.
    for album in albums.values():

        album["songIds"].sort(
            key=lambda sid: (
                songs[sid].get(
                    "discNumber",
                    1,
                ),
                songs[sid].get(
                    "track",
                    0,
                ),
                songs[sid].get(
                    "title",
                    "",
                ).lower(),
            )
        )

        album["songs"] = [
            songs[sid]
            for sid in album["songIds"]
            if sid in songs
        ]

    for artist in artists.values():

        artist["albumIds"].sort(
            key=lambda aid:
                albums[aid]["name"].lower()
        )

    LIBRARY_CACHE[
        "files"
    ] = files

    LIBRARY_CACHE[
        "songs"
    ] = songs

    LIBRARY_CACHE[
        "artists"
    ] = artists

    LIBRARY_CACHE[
        "albums"
    ] = albums

    LIBRARY_CACHE[
        "last_scan"
    ] = now

    return LIBRARY_CACHE


# ============================================================
# SUBSONIC OBJECT BUILDERS
# ============================================================

def add_cover_art(
    element,
    song,
):

    path = Path(
        song["_path"]
    )

    ET.SubElement(
        element,
        "coverArt",
        {
            "id": make_cover_id(
                path
            )
        },
    )


def song_to_xml(
    parent,
    song,
    include_album=True,
):

    attrs = {
        "id": song["id"],
        "parent": make_album_id(
            song.get(
                "albumArtist",
                "Unknown Artist",
            ),
            song.get(
                "album",
                "Unknown Album",
            ),
        ),
        "isDir": "false",
        "title": song.get(
            "title",
            "Unknown",
        ),
        "album": song.get(
            "album",
            "Unknown Album",
        ),
        "artist": song.get(
            "artist",
            "Unknown Artist",
        ),
        "albumArtist": song.get(
            "albumArtist",
            song.get(
                "artist",
                "Unknown Artist",
            ),
        ),
        "track": str(
            song.get(
                "track",
                0,
            )
        ),
        "year": str(
            song.get(
                "year",
                "",
            )
        ),
        "genre": song.get(
            "genre",
            "",
        ),
        "coverArt": make_cover_id(
            Path(song["_path"])
        ),
        "size": str(
            song.get(
                "size",
                0,
            )
        ),
        "contentType": song.get(
            "contentType",
            "audio/mpeg",
        ),
        "suffix": song.get(
            "suffix",
            ".mp3",
        ).lstrip("."),
        "duration": str(
            song.get(
                "duration",
                0,
            )
        ),
        "bitRate": str(
            int(
                song.get(
                    "bitRate",
                    0,
                )
                / 1000
            )
            if song.get(
                "bitRate",
                0,
            )
            else "0"
        ),
        "path": str(
            Path(song["_path"]).relative_to(
                DOWNLOAD_DIR
            )
        ),
        "isVideo": "false",
        "type": "music",
    }

    ET.SubElement(
        parent,
        "song",
        attrs,
    )


def song_to_json(song):

    return {
        "id": song["id"],
        "parent": make_album_id(
            song.get(
                "albumArtist",
                "Unknown Artist",
            ),
            song.get(
                "album",
                "Unknown Album",
            ),
        ),
        "isDir": False,
        "title": song.get(
            "title",
            "Unknown",
        ),
        "album": song.get(
            "album",
            "Unknown Album",
        ),
        "artist": song.get(
            "artist",
            "Unknown Artist",
        ),
        "albumArtist": song.get(
            "albumArtist",
            song.get(
                "artist",
                "Unknown Artist",
            ),
        ),
        "track": song.get(
            "track",
            0,
        ),
        "year": int(
            song.get(
                "year",
                0,
            )
            or 0
        ),
        "genre": song.get(
            "genre",
            "",
        ),
        "coverArt": make_cover_id(
            Path(song["_path"])
        ),
        "size": song.get(
            "size",
            0,
        ),
        "contentType": song.get(
            "contentType",
            "audio/mpeg",
        ),
        "suffix": song.get(
            "suffix",
            ".mp3",
        ).lstrip("."),
        "duration": song.get(
            "duration",
            0,
        ),
        "bitRate": int(
            song.get(
                "bitRate",
                0,
            )
            / 1000
        )
        if song.get(
            "bitRate",
            0,
        )
        else 0,
        "path": str(
            Path(song["_path"]).relative_to(
                DOWNLOAD_DIR
            )
        ),
        "isVideo": False,
        "type": "music",
    }


# ============================================================
# SUBSONIC ROUTE DECORATOR
# ============================================================

def subsonic_endpoint(path):
    """
    Register both:
      /rest/{path}
      /api/subsonic/rest/{path}
    """

    def decorator(func):

        app.get(
            f"/rest/{path}"
        )(func)

        app.post(
            f"/rest/{path}"
        )(func)

        app.get(
            f"/api/subsonic/rest/{path}"
        )(func)

        app.post(
            f"/api/subsonic/rest/{path}"
        )(func)

        return func

    return decorator


# ============================================================
# SUBSONIC AUTH DEPENDENCY
# ============================================================

async def require_subsonic(
    request: Request,
):

    if not verify_subsonic_auth(
        request
    ):

        return subsonic_error(
            request,
            40,
            "Wrong username or password.",
        )

    return None


# ============================================================
# SUBSONIC PING
# ============================================================

@subsonic_endpoint("ping")
async def subsonic_ping(
    request: Request,
):

    auth_error = await require_subsonic(
        request
    )

    if auth_error:
        return auth_error

    root = subsonic_root()

    return subsonic_response(
        request,
        root,
    )


# ============================================================
# GET LICENSE
# ============================================================

@subsonic_endpoint("getLicense")
async def subsonic_get_license(
    request: Request,
):

    auth_error = await require_subsonic(
        request
    )

    if auth_error:
        return auth_error

    root = subsonic_root()

    ET.SubElement(
        root,
        "license",
        {
            "valid": "true",
            "email": "local@xrob.music",
            "licenseExpires": "2099-12-31T23:59:59",
        },
    )

    return subsonic_response(
        request,
        root,
    )


# ============================================================
# GET MUSIC FOLDERS
# ============================================================

@subsonic_endpoint("getMusicFolders")
async def subsonic_get_music_folders(
    request: Request,
):

    auth_error = await require_subsonic(
        request
    )

    if auth_error:
        return auth_error

    root = subsonic_root()

    folders = ET.SubElement(
        root,
        "musicFolders",
    )

    ET.SubElement(
        folders,
        "musicFolder",
        {
            "id": "1",
            "name": "Music",
        },
    )

    return subsonic_response(
        request,
        root,
    )


# ============================================================
# GET ARTISTS
# ============================================================

@subsonic_endpoint("getArtists")
async def subsonic_get_artists(
    request: Request,
):

    auth_error = await require_subsonic(
        request
    )

    if auth_error:
        return auth_error

    library = await scan_subsonic_library()

    root = subsonic_root()

    artists_element = ET.SubElement(
        root,
        "artists",
        {
            "ignoredArticles": "",
        },
    )

    artists = sorted(
        library["artists"].values(),
        key=lambda x:
            x["name"].lower(),
    )

    indexes = {}

    for artist in artists:

        first = (
            artist["name"][:1]
            .upper()
            if artist["name"]
            else "#"
        )

        if not first.isalpha():
            first = "#"

        indexes.setdefault(
            first,
            [],
        ).append(
            artist
        )

    for index_name in sorted(
        indexes.keys()
    ):

        index_element = ET.SubElement(
            artists_element,
            "index",
            {
                "name": index_name,
            },
        )

        for artist in indexes[
            index_name
        ]:

            artist_element = ET.SubElement(
                index_element,
                "artist",
                {
                    "id": artist["id"],
                    "name": artist["name"],
                    "albumCount": str(
                        len(
                            artist[
                                "albumIds"
                            ]
                        )
                    ),
                },
            )

            # Cover art from first song.
            if artist["songs"]:

                first_song = library[
                    "songs"
                ].get(
                    artist["songs"][0]
                )

                if first_song:
                    add_cover_art(
                        artist_element,
                        first_song,
                    )

    return subsonic_response(
        request,
        root,
    )


# ============================================================
# GET INDEXES
# ============================================================

@subsonic_endpoint("getIndexes")
async def subsonic_get_indexes(
    request: Request,
):

    auth_error = await require_subsonic(
        request
    )

    if auth_error:
        return auth_error

    library = await scan_subsonic_library()

    root = subsonic_root()

    indexes_element = ET.SubElement(
        root,
        "indexes",
        {
            "lastModified": str(
                int(
                    LIBRARY_CACHE[
                        "last_scan"
                    ]
                    * 1000
                )
            ),
            "ignoredArticles": "",
        },
    )

    artists = sorted(
        library["artists"].values(),
        key=lambda x:
            x["name"].lower(),
    )

    grouped = {}

    for artist in artists:

        first = (
            artist["name"][:1]
            .upper()
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

    for letter in sorted(
        grouped.keys()
    ):

        index_element = ET.SubElement(
            indexes_element,
            "index",
            {
                "name": letter,
            },
        )

        for artist in grouped[
            letter
        ]:

            ET.SubElement(
                index_element,
                "artist",
                {
                    "id": artist["id"],
                    "name": artist["name"],
                },
            )

    return subsonic_response(
        request,
        root,
    )


# ============================================================
# GET ARTIST
# ============================================================

@subsonic_endpoint("getArtist")
async def subsonic_get_artist(
    request: Request,
    id: str = Query(...),
):

    auth_error = await require_subsonic(
        request
    )

    if auth_error:
        return auth_error

    library = await scan_subsonic_library()

    artist = library[
        "artists"
    ].get(id)

    if not artist:
        return subsonic_error(
            request,
            70,
            "Artist not found.",
        )

    root = subsonic_root()

    artist_element = ET.SubElement(
        root,
        "artist",
        {
            "id": artist["id"],
            "name": artist["name"],
            "albumCount": str(
                len(
                    artist[
                        "albumIds"
                    ]
                )
            ),
        },
    )

    if artist["songs"]:

        first_song = library[
            "songs"
        ].get(
            artist["songs"][0]
        )

        if first_song:
            add_cover_art(
                artist_element,
                first_song,
            )

    for album_id in artist[
        "albumIds"
    ]:

        album = library[
            "albums"
        ].get(album_id)

        if not album:
            continue

        attrs = {
            "id": album["id"],
            "name": album["name"],
            "artist": album["artist"],
            "artistId": album["artistId"],
            "songCount": str(
                len(
                    album[
                        "songIds"
                    ]
                )
            ),
        }

        if album.get("year"):
            attrs["year"] = str(
                album["year"]
            )

        if album.get("genre"):
            attrs["genre"] = album[
                "genre"
            ]

        album_element = ET.SubElement(
            artist_element,
            "album",
            attrs,
        )

        if album["songIds"]:

            song = library[
                "songs"
            ].get(
                album["songIds"][0]
            )

            if song:
                add_cover_art(
                    album_element,
                    song,
                )

    return subsonic_response(
        request,
        root,
    )


# ============================================================
# GET ALBUM
# ============================================================

@subsonic_endpoint("getAlbum")
async def subsonic_get_album(
    request: Request,
    id: str = Query(...),
):

    auth_error = await require_subsonic(
        request
    )

    if auth_error:
        return auth_error

    library = await scan_subsonic_library()

    album = library[
        "albums"
    ].get(id)

    if not album:
        return subsonic_error(
            request,
            70,
            "Album not found.",
        )

    root = subsonic_root()

    album_element = ET.SubElement(
        root,
        "album",
        {
            "id": album["id"],
            "name": album["name"],
            "artist": album["artist"],
            "artistId": album["artistId"],
            "songCount": str(
                len(
                    album[
                        "songIds"
                    ]
                )
            ),
        },
    )

    if album.get("year"):
        album_element.set(
            "year",
            str(album["year"]),
        )

    if album.get("genre"):
        album_element.set(
            "genre",
            album["genre"],
        )

    if album["songIds"]:

        first_song = library[
            "songs"
        ].get(
            album["songIds"][0]
        )

        if first_song:
            add_cover_art(
                album_element,
                first_song,
            )

    for song_id in album[
        "songIds"
    ]:

        song = library[
            "songs"
        ].get(song_id)

        if song:
            song_to_xml(
                album_element,
                song,
            )

    return subsonic_response(
        request,
        root,
    )


# ============================================================
# GET SONG
# ============================================================

@subsonic_endpoint("getSong")
async def subsonic_get_song(
    request: Request,
    id: str = Query(...),
):

    auth_error = await require_subsonic(
        request
    )

    if auth_error:
        return auth_error

    library = await scan_subsonic_library()

    song = library[
        "songs"
    ].get(id)

    if not song:
        return subsonic_error(
            request,
            70,
            "Song not found.",
        )

    root = subsonic_root()

    song_to_xml(
        root,
        song,
    )

    return subsonic_response(
        request,
        root,
    )


# ============================================================
# GET ALBUM LIST 2
# ============================================================

@subsonic_endpoint("getAlbumList2")
async def subsonic_get_album_list2(
    request: Request,
    type: str = Query(
        "alphabeticalByName"
    ),
    size: int = Query(
        50,
    ),
    offset: int = Query(
        0,
    ),
):

    auth_error = await require_subsonic(
        request
    )

    if auth_error:
        return auth_error

    library = await scan_subsonic_library()

    albums = list(
        library[
            "albums"
        ].values()
    )

    type_lower = type.lower()

    if type_lower in {
        "random",
    }:

        random.shuffle(
            albums
        )

    elif type_lower in {
        "newest",
        "recent",
    }:

        albums.sort(
            key=lambda album: max(
                [
                    library["songs"][sid].get(
                        "_mtime",
                        0,
                    )
                    for sid in album[
                        "songIds"
                    ]
                    if sid in library[
                        "songs"
                    ]
                ]
                or [0]
            ),
            reverse=True,
        )

    elif type_lower in {
        "frequent",
        "frequentByName",
    }:

        albums.sort(
            key=lambda album:
                len(
                    album[
                        "songIds"
                    ]
                ),
            reverse=True,
        )

    else:

        albums.sort(
            key=lambda album:
                album["name"].lower()
        )

    selected = albums[
        max(offset, 0):
        max(offset, 0)
        + max(size, 1)
    ]

    root = subsonic_root()

    albums_element = ET.SubElement(
        root,
        "albumList2",
    )

    for album in selected:

        attrs = {
            "id": album["id"],
            "name": album["name"],
            "artist": album["artist"],
            "artistId": album["artistId"],
            "songCount": str(
                len(
                    album[
                        "songIds"
                    ]
                )
            ),
        }

        if album.get("year"):
            attrs["year"] = str(
                album["year"]
            )

        if album.get("genre"):
            attrs["genre"] = album[
                "genre"
            ]

        album_element = ET.SubElement(
            albums_element,
            "album",
            attrs,
        )

        if album["songIds"]:

            song = library[
                "songs"
            ].get(
                album["songIds"][0]
            )

            if song:
                add_cover_art(
                    album_element,
                    song,
                )

    return subsonic_response(
        request,
        root,
    )


# ============================================================
# SEARCH 3
# ============================================================

@subsonic_endpoint("search3")
async def subsonic_search3(
    request: Request,
    query: str = Query(""),
    artistCount: int = Query(20),
    artistOffset: int = Query(0),
    albumCount: int = Query(20),
    albumOffset: int = Query(0),
    songCount: int = Query(20),
    songOffset: int = Query(0),
):

    auth_error = await require_subsonic(
        request
    )

    if auth_error:
        return auth_error

    library = await scan_subsonic_library()

    q = query.lower().strip()

    root = subsonic_root()

    result = ET.SubElement(
        root,
        "searchResult3",
    )

    artists = [
        artist
        for artist in library[
            "artists"
        ].values()
        if q in artist["name"].lower()
    ]

    artists.sort(
        key=lambda x:
            x["name"].lower()
    )

    for artist in artists[
        artistOffset:
        artistOffset
        + artistCount
    ]:

        ET.SubElement(
            result,
            "artist",
            {
                "id": artist["id"],
                "name": artist["name"],
                "albumCount": str(
                    len(
                        artist[
                            "albumIds"
                        ]
                    )
                ),
            },
        )

    albums = [
        album
        for album in library[
            "albums"
        ].values()
        if (
            q in album["name"].lower()
            or q
            in album["artist"].lower()
        )
    ]

    albums.sort(
        key=lambda x:
            x["name"].lower()
    )

    for album in albums[
        albumOffset:
        albumOffset
        + albumCount
    ]:

        ET.SubElement(
            result,
            "album",
            {
                "id": album["id"],
                "name": album["name"],
                "artist": album["artist"],
                "artistId": album["artistId"],
                "songCount": str(
                    len(
                        album[
                            "songIds"
                        ]
                    )
                ),
            },
        )

    songs = [
        song
        for song in library[
            "songs"
        ].values()
        if (
            q in song["title"].lower()
            or q
            in song["artist"].lower()
            or q
            in song["album"].lower()
        )
    ]

    songs.sort(
        key=lambda x:
            x["title"].lower()
    )

    for song in songs[
        songOffset:
        songOffset
        + songCount
    ]:

        song_to_xml(
            result,
            song,
        )

    return subsonic_response(
        request,
        root,
    )


# ============================================================
# RANDOM SONGS
# ============================================================

@subsonic_endpoint("getRandomSongs")
async def subsonic_random_songs(
    request: Request,
    size: int = Query(10),
    genre: Optional[str] = Query(None),
    fromYear: Optional[int] = Query(None),
    toYear: Optional[int] = Query(None),
):

    auth_error = await require_subsonic(
        request
    )

    if auth_error:
        return auth_error

    library = await scan_subsonic_library()

    songs = list(
        library[
            "songs"
        ].values()
    )

    if genre:
        songs = [
            song
            for song in songs
            if song.get(
                "genre",
                "",
            ).lower()
            == genre.lower()
        ]

    if fromYear:
        songs = [
            song
            for song in songs
            if str(
                song.get(
                    "year",
                    "",
                )
            ).startswith(
                str(fromYear)
            )
        ]

    if toYear:
        songs = [
            song
            for song in songs
            if (
                not song.get(
                    "year"
                )
                or int(
                    str(
                        song.get(
                            "year"
                        )
                    )[:4]
                )
                <= toYear
            )
        ]

    random.shuffle(
        songs
    )

    songs = songs[
        :max(size, 1)
    ]

    root = subsonic_root()

    random_element = ET.SubElement(
        root,
        "randomSongs",
    )

    for song in songs:
        song_to_xml(
            random_element,
            song,
        )

    return subsonic_response(
        request,
        root,
    )


# ============================================================
# STREAM
# ============================================================

@subsonic_endpoint("stream")
async def subsonic_stream(
    request: Request,
    id: str = Query(...),
    maxBitRate: Optional[int] = Query(None),
    format: Optional[str] = Query(None),
    estimateContentLength: bool = Query(False),
    **kwargs,
):

    auth_error = await require_subsonic(
        request
    )

    if auth_error:
        return auth_error

    library = await scan_subsonic_library()

    song = library[
        "songs"
    ].get(id)

    if not song:
        return subsonic_error(
            request,
            70,
            "Song not found.",
        )

    file_path = Path(
        song["_path"]
    )

    if not file_path.exists():
        return subsonic_error(
            request,
            70,
            "Music file no longer exists.",
        )

    media_type = song.get(
        "contentType",
        MEDIA_TYPES.get(
            file_path.suffix.lower(),
            "audio/mpeg",
        ),
    )

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": (
            f'inline; filename="{file_path.name}"'
        ),
        "Cache-Control": "no-cache",
        "Access-Control-Allow-Origin": "*",
    }

    return FileResponse(
        file_path,
        media_type=media_type,
        headers=headers,
    )


# ============================================================
# DOWNLOAD
# ============================================================

@subsonic_endpoint("download")
async def subsonic_download(
    request: Request,
    id: str = Query(...),
):

    auth_error = await require_subsonic(
        request
    )

    if auth_error:
        return auth_error

    library = await scan_subsonic_library()

    song = library[
        "songs"
    ].get(id)

    if not song:
        return subsonic_error(
            request,
            70,
            "Song not found.",
        )

    file_path = Path(
        song["_path"]
    )

    if not file_path.exists():
        return subsonic_error(
            request,
            70,
            "Music file no longer exists.",
        )

    return FileResponse(
        file_path,
        media_type=song.get(
            "contentType",
            "application/octet-stream",
        ),
        filename=file_path.name,
    )


# ============================================================
# COVER ART
# ============================================================

@subsonic_endpoint("getCoverArt")
async def subsonic_cover_art(
    request: Request,
    id: str = Query(...),
    size: Optional[int] = Query(None),
):

    auth_error = await require_subsonic(
        request
    )

    if auth_error:
        return auth_error

    library = await scan_subsonic_library()

    song = library[
        "songs"
    ].get(id)

    if song:
        file_path = Path(
            song["_path"]
        )

    else:

        # Search songs by cover ID.
        file_path = None

        for candidate in library[
            "songs"
        ].values():

            if (
                make_cover_id(
                    Path(
                        candidate[
                            "_path"
                        ]
                    )
                )
                == id
            ):

                file_path = Path(
                    candidate[
                        "_path"
                    ]
                )

                break

    if not file_path:
        return Response(
            content=b"",
            media_type="image/jpeg",
        )

    file_hash = hashlib.md5(
        str(file_path).encode(
            "utf-8"
        )
    ).hexdigest()

    cover_path = (
        COVER_CACHE_DIR
        / f"{file_hash}.jpg"
    )

    if not cover_path.exists():

        def extract():

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
            ]

            if size:
                command.extend(
                    [
                        "-vf",
                        (
                            f"scale={size}:"
                            f"{size}:"
                            "force_original_aspect_ratio=decrease"
                        ),
                    ]
                )

            command.append(
                str(cover_path)
            )

            try:

                result = subprocess.run(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )

                return (
                    result.returncode == 0
                    and cover_path.exists()
                )

            except Exception:
                return False

        await asyncio.to_thread(
            extract
        )

    if cover_path.exists():

        return FileResponse(
            cover_path,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "public, max-age=86400",
                "Access-Control-Allow-Origin": "*",
            },
        )

    # Fallback.
    svg = """
    <svg xmlns="http://www.w3.org/2000/svg"
         width="300"
         height="300"
         viewBox="0 0 300 300">
        <rect width="300"
              height="300"
              fill="#181818"/>
        <text x="150"
              y="155"
              text-anchor="middle"
              font-size="80"
              fill="#888">♫</text>
    </svg>
    """

    return Response(
        content=svg,
        media_type="image/svg+xml",
    )


# ============================================================
# STAR / RATING / SCROBBLE
# ============================================================

@subsonic_endpoint("star")
async def subsonic_star(
    request: Request,
    id: str = Query(...),
):

    auth_error = await require_subsonic(
        request
    )

    if auth_error:
        return auth_error

    # Currently accepted for client compatibility.
    root = subsonic_root()

    return subsonic_response(
        request,
        root,
    )


@subsonic_endpoint("unstar")
async def subsonic_unstar(
    request: Request,
    id: str = Query(...),
):

    auth_error = await require_subsonic(
        request
    )

    if auth_error:
        return auth_error

    root = subsonic_root()

    return subsonic_response(
        request,
        root,
    )


@subsonic_endpoint("setRating")
async def subsonic_set_rating(
    request: Request,
    id: str = Query(...),
    rating: int = Query(...),
):

    auth_error = await require_subsonic(
        request
    )

    if auth_error:
        return auth_error

    root = subsonic_root()

    return subsonic_response(
        request,
        root,
    )


@subsonic_endpoint("scrobble")
async def subsonic_scrobble(
    request: Request,
    id: str = Query(...),
    submission: bool = Query(True),
    time: Optional[int] = Query(None),
):

    auth_error = await require_subsonic(
        request
    )

    if auth_error:
        return auth_error

    root = subsonic_root()

    return subsonic_response(
        request,
        root,
    )


# ============================================================
# PLAYLIST COMPATIBILITY
# ============================================================

@subsonic_endpoint("getPlaylists")
async def subsonic_get_playlists(
    request: Request,
):

    auth_error = await require_subsonic(
        request
    )

    if auth_error:
        return auth_error

    root = subsonic_root()

    ET.SubElement(
        root,
        "playlists",
    )

    return subsonic_response(
        request,
        root,
    )


# ============================================================
# AMPERFY STATUS
# ============================================================

@app.get(
    "/api/amperfy/status"
)
async def amperfy_status():

    library = await scan_subsonic_library()

    return {
        "status": "ok",
        "server": "Xrob Music",
        "subsonic": True,
        "subsonic_version": SUBSONIC_VERSION,
        "server_version": SUBSONIC_SERVER_VERSION,
        "music_files": len(
            library["songs"]
        ),
        "artists": len(
            library["artists"]
        ),
        "albums": len(
            library["albums"]
        ),
        "rest_url": "/rest",
        "subsonic_url": "/api/subsonic/rest",
    }


@app.get(
    "/api/subsonic/status"
)
async def subsonic_status():

    return await amperfy_status()


# ============================================================
# NAVIDROME RESCAN
#
# Kept only for backward compatibility.
# No Navidrome is required anymore.
# ============================================================

async def trigger_navidrome_rescan():
    """
    Deprecated.

    Xrob Music now scans its own library directly,
    so Navidrome is no longer required.
    """

    try:

        await scan_subsonic_library(
            force=True
        )

    except Exception:
        pass


# ============================================================
# CLEANUP TASK FILES
# ============================================================

def cleanup_task_files(
    task_id: str,
):

    for p in DOWNLOAD_DIR.glob(
        f"*{task_id}*"
    ):

        try:

            if p.is_file():
                p.unlink()

        except Exception:
            pass


# ============================================================
# RESOLVE LIBRARY FILE
# ============================================================

def _resolve_file_sync(
    filename: str,
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

    target_name = Path(
        clean_name
    ).name

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
    filename: str,
):

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

                line_str = (
                    line.decode(
                        "utf-8",
                        errors="ignore",
                    ).strip()
                )

                pct_match = (
                    progress_regex.search(
                        line_str
                    )
                )

                if pct_match:

                    task["percent"] = (
                        float(
                            pct_match.group(1)
                        )
                    )

                    task["last_updated"] = (
                        time.time()
                        * 1000
                    )

                    spd_match = (
                        speed_regex.search(
                            line_str
                        )
                    )

                    if spd_match:

                        task["speed"] = (
                            spd_match.group(
                                1
                            ).replace(
                                "~",
                                "",
                            )
                        )

                    await notify_task_update(
                        task,
                        force_save=False,
                    )

                elif (
                    "[ExtractAudio]"
                    in line_str
                    or "[EmbedThumbnail]"
                    in line_str
                    or "[Metadata]"
                    in line_str
                ):

                    task["status"] = (
                        "processing"
                    )

                    task["step"] = (
                        "Embedding cover art & tags..."
                    )

                    task["percent"] = 92

                    task["last_updated"] = (
                        time.time()
                        * 1000
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
                    time.time()
                    * 1000
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
                    time.time()
                    * 1000
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
                    time.time()
                    * 1000
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
                time.time()
                * 1000
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
                process_clean.returncode
                == 0
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
                time.time()
                * 1000
            )

            await notify_task_update(
                task,
                force_save=True,
            )

            # Refresh Xrob's own music index.
            await scan_subsonic_library(
                force=True
            )

        except Exception as err:

            await asyncio.to_thread(
                cleanup_task_files,
                task_id,
            )

            task["status"] = (
                "error"
            )

            task["error"] = str(
                err
            )

            task["last_updated"] = (
                time.time()
                * 1000
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

@app.on_event(
    "startup"
)
async def startup_event():

    await asyncio.to_thread(
        init_db
    )

    global TASKS

    TASKS = await asyncio.to_thread(
        _db_load_tasks_sync
    )

    await scan_subsonic_library(
        force=True
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

@app.websocket(
    "/ws"
)
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

            error = (
                stderr.decode(
                    "utf-8",
                    errors="ignore",
                )
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
                item.get(
                    "channel"
                )
                or item.get(
                    "uploader"
                )
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
                        or
                        f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
                    ),
                    "url": (
                        f"https://www.youtube.com/watch?v={video_id}"
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
# WEB UI
# ============================================================

@app.get("/")
async def home():

    return FileResponse(
        STATIC_DIR
        / "index.html"
    )


# ============================================================
# SETTINGS API
# ============================================================

@app.get(
    "/api/settings"
)
async def get_settings():

    return load_settings()


@app.post(
    "/api/settings"
)
async def update_settings(
    data: dict = Body(...),
):

    return save_settings(
        data
    )


# ============================================================
# SEARCH
# ============================================================

@app.get(
    "/api/search"
)
async def search_endpoint(
    q: str = Query(...),
    page: int = Query(1),
):

    if not q.strip():
        return []

    settings = load_settings()

    max_results = settings.get(
        "max_results",
        20,
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

@app.get(
    "/api/preview"
)
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
                detail="Failed to fetch preview audio URL",
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
# DOWNLOAD
# ============================================================

@app.post(
    "/api/download"
)
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
        "last_updated": time.time()
        * 1000,
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
# TASKS
# ============================================================

@app.get(
    "/api/tasks"
)
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
        time.time()
        * 1000
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

    for tid in to_remove:

        TASKS.pop(
            tid,
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
# WEB LIBRARY
# ============================================================

@app.get(
    "/api/library"
)
async def get_library():

    audio_files = (
        await get_all_audio_files()
    )

    def _build():

        files = []

        total_bytes = 0

        for path in audio_files:

            sz = path.stat().st_size

            total_bytes += sz

            files.append(
                {
                    "name": str(
                        path.relative_to(
                            DOWNLOAD_DIR
                        )
                    ),
                    "size": format_size(
                        sz
                    ),
                    "bytes": sz,
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
        "files": sorted(
            files,
            key=lambda x:
                x["name"],
        ),
        "total_size": format_size(
            total_bytes
        ),
        "total_bytes": total_bytes,
    }


# ============================================================
# STATS
# ============================================================

@app.get(
    "/api/stats"
)
async def get_stats():

    files = (
        await get_all_audio_files()
    )

    def _build():

        total_bytes = sum(
            p.stat().st_size
            for p in files
        )

        artists = set()

        albums = set()

        for p in files:

            rel = p.relative_to(
                DOWNLOAD_DIR
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
        tracks_cnt,
        artists_cnt,
        albums_cnt,
        total_bytes,
    ) = await asyncio.to_thread(
        _build
    )

    return {
        "tracks": tracks_cnt,
        "artists": artists_cnt,
        "albums": albums_cnt,
        "total_bytes": total_bytes,
        "folder_size": format_size(
            total_bytes
        ),
    }


# ============================================================
# WEB COVER
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

    def _extract_cover():

        cmd = [
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

            res = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )

            if (
                res.returncode == 0
                and cover_path.exists()
                and cover_path.stat().st_size
                > 0
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
                "Access-Control-Allow-Origin": "*"
            },
        )

    svg_fallback = """
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
        content=svg_fallback,
        media_type="image/svg+xml",
        headers={
            "Access-Control-Allow-Origin": "*"
        },
    )


# ============================================================
# WEB STREAM
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
            str(file_path).encode(
                "utf-8"
            )
        ).hexdigest()

        cover_path = (
            COVER_CACHE_DIR
            / f"{file_hash}.jpg"
        )

        if cover_path.exists():
            cover_path.unlink()

        await scan_subsonic_library(
            force=True
        )

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
