from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from pathlib import Path
from urllib.parse import urlparse
import asyncio
import json
import os
import re
import unicodedata
import uuid

import httpx


# ============================================================
# APP
# ============================================================

app = FastAPI(
    title="Music Downloader",
    version="1.0.0"
)


# ============================================================
# CONFIGURATION
# ============================================================

DEEZER_SEARCH_URL = "https://api.deezer.com/search"

DOWNLOAD_DIR = Path(
    os.getenv(
        "DOWNLOAD_DIR",
        "/downloads"
    )
)

DOWNLOAD_DIR.mkdir(
    parents=True,
    exist_ok=True
)

MAX_RESULTS = 25

DOWNLOAD_TIMEOUT = int(
    os.getenv(
        "DOWNLOAD_TIMEOUT",
        "600"
    )
)


# ============================================================
# HELPERS
# ============================================================

def clean_filename(value: str) -> str:
    """
    Create a safe filename.
    """

    value = str(value or "Unknown")

    value = unicodedata.normalize(
        "NFKC",
        value
    )

    value = re.sub(
        r'[\\/:*?"<>|]',
        "",
        value
    )

    value = re.sub(
        r"[\x00-\x1f]",
        "",
        value
    )

    value = re.sub(
        r"\s+",
        " ",
        value
    ).strip()

    value = value.rstrip(".")

    if not value:
        value = "Unknown"

    return value[:180]


def normalize_text(value: str) -> str:
    """
    Normalize text while preserving Unicode letters.
    """

    value = str(value or "")

    value = unicodedata.normalize(
        "NFKD",
        value
    )

    value = "".join(
        char
        for char in value
        if not unicodedata.combining(char)
    )

    value = value.lower()

    value = re.sub(
        r"[^\w\s]",
        " ",
        value,
        flags=re.UNICODE
    )

    value = re.sub(
        r"\s+",
        " ",
        value
    ).strip()

    return value


def calculate_score(
    item: dict,
    query: str
) -> float:

    query_normalized = normalize_text(
        query
    )

    title = normalize_text(
        item.get("title", "")
    )

    artist = normalize_text(
        item.get("artist", "")
    )

    album = normalize_text(
        item.get("album", "")
    )

    score = 0.0

    # Exact artist
    if artist == query_normalized:
        score += 1000

    # Exact title
    if title == query_normalized:
        score += 900

    # Artist starts with query
    if artist.startswith(query_normalized):
        score += 700

    # Title starts with query
    if title.startswith(query_normalized):
        score += 600

    # Artist contains query
    if query_normalized in artist:
        score += 400

    # Title contains query
    if query_normalized in title:
        score += 350

    # Album contains query
    if query_normalized in album:
        score += 100

    # Word matching
    for word in query_normalized.split():

        if word in artist:
            score += 120

        if word in title:
            score += 80

        if word in album:
            score += 30

    # Deezer rank
    try:

        rank = int(
            item.get(
                "rank",
                0
            ) or 0
        )

        score += min(
            rank / 10000,
            50
        )

    except (
        TypeError,
        ValueError
    ):
        pass

    return score


def is_youtube_url(url: str) -> bool:
    """
    Validate YouTube URL.
    """

    try:

        parsed = urlparse(
            url
        )

        hostname = (
            parsed.hostname
            or ""
        ).lower()

        return hostname in {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "youtu.be",
            "www.youtu.be"
        }

    except Exception:

        return False


def json_error(
    status_code: int,
    message: str
):
    """
    Always return a JSON error.
    """

    return JSONResponse(
        status_code=status_code,
        content={
            "error": True,
            "detail": message
        }
    )


# ============================================================
# DEEZER SEARCH
# ============================================================

async def deezer_search(
    query: str,
    limit: int = MAX_RESULTS
):

    query = query.strip()

    if not query:
        return []

    params = {
        "q": query,
        "limit": limit
    }

    headers = {
        "User-Agent":
            "MusicDownloader/1.0"
    }

    async with httpx.AsyncClient(
        timeout=20,
        follow_redirects=True
    ) as client:

        response = await client.get(
            DEEZER_SEARCH_URL,
            params=params,
            headers=headers
        )

        response.raise_for_status()

        # Decode explicitly.
        text = response.text

        try:

            data = json.loads(
                text
            )

        except json.JSONDecodeError as error:

            raise RuntimeError(
                "Deezer returned invalid JSON: "
                + str(error)
            )

    if not isinstance(
        data,
        dict
    ):

        raise RuntimeError(
            "Unexpected Deezer response."
        )

    results = []

    tracks = data.get(
        "data",
        []
    )

    if not isinstance(
        tracks,
        list
    ):

        return []

    for track in tracks:

        if not isinstance(
            track,
            dict
        ):
            continue

        artist_data = (
            track.get(
                "artist"
            )
            or {}
        )

        album_data = (
            track.get(
                "album"
            )
            or {}
        )

        if not isinstance(
            artist_data,
            dict
        ):
            artist_data = {}

        if not isinstance(
            album_data,
            dict
        ):
            album_data = {}

        result = {

            "id": track.get(
                "id"
            ),

            "title": (
                track.get(
                    "title"
                )
                or "Unknown"
            ),

            "artist": (
                artist_data.get(
                    "name"
                )
                or "Unknown Artist"
            ),

            "artist_id": artist_data.get(
                "id"
            ),

            "album": (
                album_data.get(
                    "title"
                )
                or "Unknown Album"
            ),

            "album_id": album_data.get(
                "id"
            ),

            "duration": track.get(
                "duration",
                0
            ),

            "rank": track.get(
                "rank",
                0
            ),

            "preview": (
                track.get(
                    "preview"
                )
                or ""
            ),

            "deezer_url": (
                track.get(
                    "link"
                )
                or ""
            ),

            "cover": (
                album_data.get(
                    "cover_medium"
                )
                or ""
            ),

            "cover_big": (
                album_data.get(
                    "cover_big"
                )
                or ""
            ),

            "cover_xl": (
                album_data.get(
                    "cover_xl"
                )
                or ""
            ),

            "isrc": (
                track.get(
                    "isrc"
                )
                or ""
            ),

            "explicit": bool(
                track.get(
                    "explicit_lyrics",
                    False
                )
            )
        }

        result["score"] = calculate_score(
            result,
            query
        )

        results.append(
            result
        )

    results.sort(
        key=lambda item: item.get(
            "score",
            0
        ),
        reverse=True
    )

    return results


# ============================================================
# YOUTUBE SEARCH
# ============================================================

async def youtube_search(
    artist: str,
    title: str
) -> str:

    search_query = (
        f"{artist} - {title}"
    )

    command = [

        "yt-dlp",

        "--flat-playlist",

        "--skip-download",

        "--no-warnings",

        "--print",
        "%(webpage_url)s",

        f"ytsearch1:{search_query}"
    ]

    try:

        process = (
            await asyncio.create_subprocess_exec(

                *command,

                stdout=asyncio.subprocess.PIPE,

                stderr=asyncio.subprocess.PIPE
            )
        )

        stdout, stderr = (
            await asyncio.wait_for(
                process.communicate(),
                timeout=60
            )
        )

    except FileNotFoundError:

        raise RuntimeError(
            "yt-dlp is not installed."
        )

    except asyncio.TimeoutError:

        try:
            process.kill()
        except Exception:
            pass

        raise RuntimeError(
            "YouTube search timed out."
        )

    if process.returncode != 0:

        error_text = (
            stderr.decode(
                "utf-8",
                errors="replace"
            ).strip()
        )

        raise RuntimeError(
            "YouTube search failed: "
            + error_text[-1000:]
        )

    output = (
        stdout.decode(
            "utf-8",
            errors="replace"
        )
    )

    for line in output.splitlines():

        line = line.strip()

        if is_youtube_url(
            line
        ):

            return line

    raise RuntimeError(
        "No YouTube result found."
    )


# ============================================================
# DOWNLOAD FROM YOUTUBE
# ============================================================

async def download_youtube_audio(
    url: str,
    artist: str = "",
    title: str = ""
) -> Path:

    if not is_youtube_url(
        url
    ):

        raise HTTPException(
            status_code=400,
            detail="Invalid YouTube URL."
        )

    job_id = uuid.uuid4().hex

    artist_clean = clean_filename(
        artist
    )

    title_clean = clean_filename(
        title
    )

    if artist_clean and title_clean:

        filename_base = (
            f"{artist_clean} - "
            f"{title_clean}"
        )

    elif title_clean:

        filename_base = title_clean

    else:

        filename_base = job_id

    filename_base = clean_filename(
        filename_base
    )

    output_template = str(
        DOWNLOAD_DIR /
        f"{job_id}.%(ext)s"
    )

    command = [

        "yt-dlp",

        "--no-playlist",

        "--no-warnings",

        "--extract-audio",

        "--audio-format",
        "mp3",

        "--audio-quality",
        "192K",

        "--prefer-ffmpeg",

        "--no-progress",

        "--newline",

        "--restrict-filenames",

        "-o",
        output_template,

        url
    ]

    try:

        process = (
            await asyncio.create_subprocess_exec(

                *command,

                stdout=asyncio.subprocess.PIPE,

                stderr=asyncio.subprocess.PIPE
            )
        )

    except FileNotFoundError:

        raise HTTPException(
            status_code=500,
            detail="yt-dlp is not installed."
        )

    try:

        stdout, stderr = (
            await asyncio.wait_for(
                process.communicate(),
                timeout=DOWNLOAD_TIMEOUT
            )
        )

    except asyncio.TimeoutError:

        try:
            process.kill()
        except Exception:
            pass

        try:
            await process.wait()
        except Exception:
            pass

        # Cleanup partial files
        for file in DOWNLOAD_DIR.glob(
            f"{job_id}.*"
        ):

            try:
                file.unlink()
            except Exception:
                pass

        raise HTTPException(
            status_code=504,
            detail="Download timed out."
        )

    stdout_text = stdout.decode(
        "utf-8",
        errors="replace"
    )

    stderr_text = stderr.decode(
        "utf-8",
        errors="replace"
    )

    if process.returncode != 0:

        # Cleanup
        for file in DOWNLOAD_DIR.glob(
            f"{job_id}.*"
        ):

            try:
                file.unlink()
            except Exception:
                pass

        error_text = (
            stderr_text.strip()
            or stdout_text.strip()
            or "Unknown yt-dlp error."
        )

        raise HTTPException(
            status_code=500,
            detail=(
                "YouTube download failed: "
                + error_text[-2000:]
            )
        )

    # Prefer generated MP3
    mp3_files = list(
        DOWNLOAD_DIR.glob(
            f"{job_id}.mp3"
        )
    )

    if mp3_files:

        source_file = mp3_files[0]

    else:

        # Look for any generated file
        possible_files = [
            file
            for file in DOWNLOAD_DIR.glob(
                f"{job_id}.*"
            )
            if file.is_file()
        ]

        if not possible_files:

            raise HTTPException(
                status_code=500,
                detail=(
                    "yt-dlp completed successfully "
                    "but no audio file was created."
                )
            )

        source_file = possible_files[0]

    # Rename to a user-friendly filename.
    extension = source_file.suffix.lower()

    final_file = (
        DOWNLOAD_DIR /
        f"{filename_base}{extension}"
    )

    # Avoid collisions
    if final_file.exists():

        final_file = (
            DOWNLOAD_DIR /
            f"{filename_base} "
            f"({job_id[:8]})"
            f"{extension}"
        )

    try:

        source_file.rename(
            final_file
        )

    except Exception:

        final_file = source_file

    return final_file


# ============================================================
# HOME PAGE
# ============================================================

@app.get(
    "/",
    response_class=HTMLResponse
)
async def home():

    return """
<!DOCTYPE html>

<html lang="en">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>Music Downloader</title>

<style>

* {
    box-sizing: border-box;
}

body {

    margin: 0;

    padding: 30px;

    background:
        linear-gradient(
            135deg,
            #111827,
            #0f172a
        );

    color: white;

    font-family:
        Arial,
        Helvetica,
        sans-serif;
}

.container {

    max-width: 1100px;

    margin: 0 auto;
}

h1 {

    margin-bottom: 5px;
}

.subtitle {

    color: #9ca3af;

    margin-bottom: 25px;
}

.search-box {

    display: flex;

    gap: 10px;

    margin-bottom: 20px;
}

input {

    flex: 1;

    padding: 15px;

    border: none;

    border-radius: 12px;

    background: #1f2937;

    color: white;

    font-size: 16px;

    outline: none;
}

button {

    padding:
        12px 18px;

    border: none;

    border-radius: 10px;

    background: #6366f1;

    color: white;

    cursor: pointer;

    font-size: 14px;

    font-weight: bold;
}

button:hover {

    background: #4f46e5;
}

button:disabled {

    opacity: 0.5;

    cursor: not-allowed;
}

.result {

    display: flex;

    align-items: center;

    gap: 15px;

    background: #1f2937;

    padding: 12px;

    margin-top: 10px;

    border-radius: 14px;
}

.cover {

    width: 75px;

    height: 75px;

    border-radius: 10px;

    object-fit: cover;

    background: #374151;

    flex-shrink: 0;
}

.info {

    flex: 1;

    min-width: 0;
}

.title {

    font-size: 17px;

    font-weight: bold;

    white-space: nowrap;

    overflow: hidden;

    text-overflow: ellipsis;
}

.artist {

    color: #a78bfa;

    margin-top: 5px;
}

.album {

    color: #9ca3af;

    margin-top: 4px;

    font-size: 14px;
}

.actions {

    display: flex;

    gap: 8px;

    align-items: center;

    flex-wrap: wrap;

    justify-content: flex-end;
}

.preview {

    background: #374151;
}

.download {

    background: #10b981;
}

.download:hover {

    background: #059669;
}

audio {

    width: 180px;
}

#status {

    color: #9ca3af;

    margin-bottom: 15px;

    min-height: 20px;
}

.badge {

    display: inline-block;

    padding: 3px 7px;

    border-radius: 6px;

    background: #374151;

    color: #d1d5db;

    font-size: 11px;

    margin-left: 5px;
}

.error {

    color: #fca5a5;
}

.success {

    color: #86efac;
}

@media(max-width: 700px) {

    body {
        padding: 15px;
    }

    .result {
        align-items: flex-start;
        flex-wrap: wrap;
    }

    .info {
        width: calc(100% - 100px);
    }

    .actions {
        width: 100%;
        justify-content: flex-start;
    }

    audio {
        width: 100%;
    }
}

</style>

</head>

<body>

<div class="container">

<h1>🎵 Music Downloader</h1>

<div class="subtitle">
Search artists, songs and albums.
</div>

<div class="search-box">

<input
    id="query"
    placeholder="Search music..."
    autocomplete="off"
/>

<button
    id="searchButton"
    onclick="searchMusic()"
>
🔍 Search
</button>

</div>

<div id="status"></div>

<div id="results"></div>

</div>


<script>

async function readApiResponse(response) {

    const text =
        await response.text();

    if (!text) {

        throw new Error(
            "The server returned an empty response."
        );
    }

    try {

        return JSON.parse(text);

    } catch(error) {

        console.error(
            "Invalid JSON response:",
            text
        );

        throw new Error(
            "Server returned invalid JSON."
        );
    }
}


async function searchMusic() {

    const query =
        document
            .getElementById("query")
            .value
            .trim();

    const status =
        document.getElementById("status");

    const results =
        document.getElementById("results");

    const button =
        document.getElementById("searchButton");

    if (!query) {

        status.textContent =
            "Enter a song or artist.";

        return;
    }

    status.className = "";

    status.textContent =
        "🔎 Searching...";

    results.innerHTML = "";

    button.disabled = true;

    try {

        const response =
            await fetch(
                "/api/search?q=" +
                encodeURIComponent(query),
                {
                    headers: {
                        "Accept":
                            "application/json"
                    }
                }
            );

        const data =
            await readApiResponse(
                response
            );

        if (!response.ok) {

            throw new Error(
                data.detail ||
                "Search failed."
            );
        }

        if (!Array.isArray(data)) {

            throw new Error(
                "The search API returned an unexpected response."
            );
        }

        if (!data.length) {

            status.textContent =
                "No music found.";

            return;
        }

        status.textContent =
            "🎵 " +
            data.length +
            " results";

        for (
            const item of data
        ) {

            renderResult(
                item,
                results
            );
        }

    } catch(error) {

        console.error(
            error
        );

        status.className =
            "error";

        status.textContent =
            "❌ " +
            error.message;

    } finally {

        button.disabled = false;
    }
}


function renderResult(
    item,
    results
) {

    const div =
        document.createElement(
            "div"
        );

    div.className =
        "result";

    const cover =
        item.cover_xl ||
        item.cover_big ||
        item.cover ||
        "";

    const preview =
        item.preview ||
        "";

    const title =
        item.title ||
        "Unknown";

    const artist =
        item.artist ||
        "Unknown Artist";

    const album =
        item.album ||
        "Unknown Album";

    const id =
        Number(
            item.id || 0
        );

    div.innerHTML = `

        <img
            class="cover"
            src="${escapeHtml(cover)}"
            alt=""
            onerror="
                this.style.visibility='hidden'
            "
        >

        <div class="info">

            <div class="title">
                ${escapeHtml(title)}
            </div>

            <div class="artist">
                👤 ${escapeHtml(artist)}
            </div>

            <div class="album">
                💿 ${escapeHtml(album)}
            </div>

        </div>

        <div class="actions">

            ${
                preview
                ?
                `
                <audio
                    controls
                    preload="none"
                    src="${escapeHtml(preview)}"
                ></audio>
                `
                :
                ""
            }

            ${
                preview
                ?
                `
                <a
                    href="${escapeHtml(preview)}"
                    target="_blank"
                    rel="noopener"
                >
                    <button class="preview">
                        ▶ Preview
                    </button>
                </a>
                `
                :
                ""
            }

            <button
                class="download"
                data-id="${id}"
            >
                ⬇ Download
            </button>

        </div>
    `;

    const downloadButton =
        div.querySelector(
            ".download"
        );

    downloadButton.addEventListener(
        "click",
        function() {

            downloadTrack(
                title,
                artist,
                downloadButton
            );

        }
    );

    results.appendChild(
        div
    );
}


async function downloadTrack(
    title,
    artist,
    button
) {

    const status =
        document.getElementById(
            "status"
        );

    button.disabled = true;

    button.textContent =
        "🔎 Finding...";

    status.className = "";

    status.textContent =
        "🔎 Finding YouTube version of " +
        artist +
        " - " +
        title +
        "...";

    try {

        const response =
            await fetch(
                "/api/download/search?" +
                new URLSearchParams({
                    artist: artist,
                    title: title
                })
            );

        const data =
            await readApiResponse(
                response
            );

        if (!response.ok) {

            throw new Error(
                data.detail ||
                "Could not find the song."
            );
        }

        if (!data.url) {

            throw new Error(
                "No YouTube result was found."
            );
        }

        status.textContent =
            "⬇ Downloading " +
            artist +
            " - " +
            title +
            "...";

        button.textContent =
            "⬇ Downloading...";

        const downloadUrl =
            "/api/download?" +
            new URLSearchParams({
                url: data.url,
                artist: artist,
                title: title
            });

        const downloadResponse =
            await fetch(
                downloadUrl
            );

        if (!downloadResponse.ok) {

            const errorData =
                await readApiResponse(
                    downloadResponse
                );

            throw new Error(
                errorData.detail ||
                "Download failed."
            );
        }

        const blob =
            await downloadResponse.blob();

        const blobUrl =
            URL.createObjectURL(
                blob
            );

        const link =
            document.createElement(
                "a"
            );

        link.href =
            blobUrl;

        link.download =
            artist +
            " - " +
            title +
            ".mp3";

        document.body.appendChild(
            link
        );

        link.click();

        link.remove();

        URL.revokeObjectURL(
            blobUrl
        );

        status.className =
            "success";

        status.textContent =
            "✅ Download complete: " +
            artist +
            " - " +
            title;

    } catch(error) {

        console.error(
            error
        );

        status.className =
            "error";

        status.textContent =
            "❌ " +
            error.message;

    } finally {

        button.disabled = false;

        button.textContent =
            "⬇ Download";
    }
}


function escapeHtml(
    text
) {

    const div =
        document.createElement(
            "div"
        );

    div.textContent =
        String(
            text || ""
        );

    return div.innerHTML;
}


document
    .getElementById("query")
    .addEventListener(
        "keydown",
        function(event) {

            if (
                event.key === "Enter"
            ) {

                searchMusic();

            }

        }
    );

</script>

</body>

</html>
"""


# ============================================================
# SEARCH API
# ============================================================

@app.get(
    "/api/search"
)
async def search(
    q: str = Query(
        ...,
        min_length=1
    )
):

    try:

        results = await deezer_search(
            q,
            MAX_RESULTS
        )

        return JSONResponse(
            content=results
        )

    except httpx.HTTPError as error:

        return json_error(
            502,
            "Deezer request failed: "
            + str(error)
        )

    except Exception as error:

        return json_error(
            500,
            "Search failed: "
            + str(error)
        )


# ============================================================
# FIND YOUTUBE VERSION
# ============================================================

@app.get(
    "/api/download/search"
)
async def download_search(
    artist: str = Query(
        ...,
        min_length=1
    ),
    title: str = Query(
        ...,
        min_length=1
    )
):

    try:

        url = await youtube_search(
            artist,
            title
        )

        return {
            "success": True,
            "url": url,
            "artist": artist,
            "title": title
        }

    except Exception as error:

        return json_error(
            500,
            str(error)
        )


# ============================================================
# DOWNLOAD AUDIO
# ============================================================

@app.get(
    "/api/download"
)
async def download_audio(
    url: str = Query(
        ...,
        min_length=1
    ),
    artist: str = Query(
        "",
        max_length=200
    ),
    title: str = Query(
        "",
        max_length=200
    )
):

    try:

        file_path = (
            await download_youtube_audio(
                url=url,
                artist=artist,
                title=title
            )
        )

        return FileResponse(

            path=str(
                file_path
            ),

            media_type="audio/mpeg",

            filename=file_path.name
        )

    except HTTPException:

        raise

    except Exception as error:

        return json_error(
            500,
            "Download error: "
            + str(error)
        )


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get(
    "/health"
)
async def health():

    yt_dlp_available = False
    ffmpeg_available = False

    try:

        process = (
            await asyncio.create_subprocess_exec(

                "yt-dlp",
                "--version",

                stdout=asyncio.subprocess.PIPE,

                stderr=asyncio.subprocess.PIPE
            )
        )

        await process.communicate()

        yt_dlp_available = (
            process.returncode == 0
        )

    except Exception:
        pass

    try:

        process = (
            await asyncio.create_subprocess_exec(

                "ffmpeg",
                "-version",

                stdout=asyncio.subprocess.PIPE,

                stderr=asyncio.subprocess.PIPE
            )
        )

        await process.communicate()

        ffmpeg_available = (
            process.returncode == 0
        )

    except Exception:
        pass

    return {

        "status": "ok",

        "service":
            "music-downloader",

        "yt_dlp":
            yt_dlp_available,

        "ffmpeg":
            ffmpeg_available,

        "download_directory":
            str(DOWNLOAD_DIR),

        "download_directory_exists":
            DOWNLOAD_DIR.exists()
    }
