from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from pathlib import Path
import asyncio
import os
import re
import uuid
import json

import httpx


app = FastAPI(title="Music Downloader")


# ============================================================
# CONFIG
# ============================================================

DEEZER_SEARCH_URL = "https://api.deezer.com/search"

DOWNLOAD_DIR = Path(
    os.getenv("DOWNLOAD_DIR", "/downloads")
)

DOWNLOAD_DIR.mkdir(
    parents=True,
    exist_ok=True
)

MAX_RESULTS = 25


# ============================================================
# HELPERS
# ============================================================

def clean_filename(value: str) -> str:

    value = value or "Unknown"

    value = re.sub(
        r'[\\/:*?"<>|]',
        "",
        value
    )

    value = re.sub(
        r"\s+",
        " ",
        value
    ).strip()

    if not value:
        value = "Unknown"

    return value[:180]


def normalize_text(value: str) -> str:

    value = value or ""

    value = value.lower()

    value = re.sub(
        r"[^a-z0-9\s]",
        " ",
        value
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

    query_normalized = normalize_text(query)

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

    if artist == query_normalized:
        score += 1000

    if title == query_normalized:
        score += 900

    if artist.startswith(query_normalized):
        score += 700

    if title.startswith(query_normalized):
        score += 600

    if query_normalized in artist:
        score += 400

    if query_normalized in title:
        score += 350

    if query_normalized in album:
        score += 100

    for word in query_normalized.split():

        if word in artist:
            score += 120

        if word in title:
            score += 80

    try:

        rank = int(
            item.get("rank", 0)
        )

        score += min(
            rank / 10000,
            50
        )

    except Exception:

        pass

    return score


# ============================================================
# DEEZER SEARCH
# ============================================================

async def deezer_search(
    query: str,
    limit: int = MAX_RESULTS
):

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
        # This prevents weird response handling.
        raw_text = response.text

        try:

            data = json.loads(
                raw_text
            )

        except json.JSONDecodeError as error:

            raise RuntimeError(
                "Deezer returned invalid JSON: "
                + str(error)
            )

    if not isinstance(data, dict):

        raise RuntimeError(
            "Unexpected Deezer response format."
        )

    results = []

    for track in data.get(
        "data",
        []
    ):

        if not isinstance(
            track,
            dict
        ):
            continue

        artist_data = (
            track.get("artist")
            or {}
        )

        album_data = (
            track.get("album")
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

            "title": str(
                track.get(
                    "title",
                    "Unknown"
                )
            ),

            "artist": str(
                artist_data.get(
                    "name",
                    "Unknown Artist"
                )
            ),

            "artist_id":
                artist_data.get("id"),

            "album": str(
                album_data.get(
                    "title",
                    "Unknown Album"
                )
            ),

            "album_id":
                album_data.get("id"),

            "duration":
                track.get("duration", 0),

            "rank":
                track.get("rank", 0),

            "preview":
                track.get("preview"),

            "deezer_url":
                track.get("link"),

            "cover":
                album_data.get(
                    "cover_medium"
                ),

            "cover_big":
                album_data.get(
                    "cover_big"
                ),

            "cover_xl":
                album_data.get(
                    "cover_xl"
                ),

            "isrc":
                track.get("isrc"),

            "explicit":
                bool(
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
        key=lambda x: x.get(
            "score",
            0
        ),
        reverse=True
    )

    return results


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

<html>

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
        14px 20px;

    border: none;

    border-radius: 12px;

    background: #6366f1;

    color: white;

    cursor: pointer;

    font-size: 15px;

    font-weight: bold;
}

button:hover {

    background: #4f46e5;
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
}

.preview {

    background: #374151;
}

.download {

    background: #10b981;
}

audio {

    width: 180px;
}

#status {

    color: #9ca3af;

    margin-bottom: 15px;
}

.error {

    color: #f87171;

    background: #3f1d1d;

    padding: 12px;

    border-radius: 10px;

    margin-bottom: 15px;
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
        min-width: calc(100% - 100px);
    }

    .actions {
        width: 100%;
        flex-direction: column;
        align-items: stretch;
    }

    audio {
        width: 100%;
    }

    button {
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

<button onclick="searchMusic()">
🔍 Search
</button>

</div>

<div id="status"></div>

<div id="results"></div>

</div>


<script>

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

    if (!query) {

        status.textContent =
            "Enter a song or artist.";

        return;
    }

    status.textContent =
        "🔎 Searching...";

    results.innerHTML = "";

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

        /*
         * IMPORTANT:
         * Read the response as TEXT first.
         *
         * If the server sends broken JSON,
         * we can see exactly what happened.
         */

        const raw =
            await response.text();

        if (!response.ok) {

            let message =
                "Server error " +
                response.status;

            try {

                const errorData =
                    JSON.parse(raw);

                if (
                    errorData &&
                    errorData.detail
                ) {

                    message =
                        errorData.detail;
                }

            } catch (_) {

                if (raw) {
                    message =
                        raw.substring(
                            0,
                            500
                        );
                }
            }

            throw new Error(
                message
            );
        }

        let data;

        try {

            data =
                JSON.parse(raw);

        } catch (jsonError) {

            console.error(
                "Invalid JSON from server:",
                raw
            );

            throw new Error(
                "Server returned invalid JSON."
            );
        }

        if (!Array.isArray(data)) {

            throw new Error(
                "Server returned an unexpected response."
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

            const div =
                document.createElement(
                    "div"
                );

            div.className =
                "result";

            const cover =
                item.cover ||
                "";

            const preview =
                item.preview ||
                "";

            const title =
                escapeHtml(
                    item.title
                );

            const artist =
                escapeHtml(
                    item.artist
                );

            const album =
                escapeHtml(
                    item.album
                );

            const safePreview =
                escapeHtml(
                    preview
                );

            div.innerHTML = `

                <img
                    class="cover"
                    src="${escapeHtml(cover)}"
                    onerror="
                        this.style.visibility='hidden'
                    "
                >

                <div class="info">

                    <div class="title">
                        ${title}
                    </div>

                    <div class="artist">
                        👤 ${artist}
                    </div>

                    <div class="album">
                        💿 ${album}
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
                            src="${safePreview}"
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
                            href="${safePreview}"
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
                        onclick="downloadSong(
                            '${escapeJs(
                                item.title
                            )}',
                            '${escapeJs(
                                item.artist
                            )}'
                        )"
                    >
                        ⬇ Download
                    </button>

                </div>
            `;

            results.appendChild(
                div
            );
        }

    } catch(error) {

        console.error(error);

        status.innerHTML =
            '<div class="error">❌ ' +
            escapeHtml(
                error.message
            ) +
            '</div>';
    }
}


function downloadSong(
    title,
    artist
) {

    const text =
        "Download for: " +
        artist +
        " - " +
        title +
        "\\n\\n" +
        "Use /api/download with an authorized YouTube URL.";

    alert(text);
}


function escapeHtml(text) {

    const div =
        document.createElement(
            "div"
        );

    div.textContent =
        text == null
            ? ""
            : String(text);

    return div.innerHTML;
}


function escapeJs(text) {

    return String(
        text == null
            ? ""
            : text
    )
    .replace(
        /\\\\/g,
        "\\\\\\\\"
    )
    .replace(
        /'/g,
        "\\\\'"
    )
    .replace(
        /\\n/g,
        "\\\\n"
    )
    .replace(
        /\\r/g,
        "\\\\r"
    );
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

@app.get("/api/search")
async def search(
    q: str = Query(
        ...,
        min_length=1
    )
):

    q = q.strip()

    if not q:

        raise HTTPException(
            status_code=400,
            detail="Search query is empty."
        )

    try:

        results = await deezer_search(
            q,
            MAX_RESULTS
        )

        # Explicit JSON response.
        return JSONResponse(
            content=results,
            status_code=200
        )

    except httpx.HTTPStatusError as error:

        raise HTTPException(
            status_code=502,
            detail=
                "Deezer HTTP error: "
                + str(error)
        )

    except httpx.RequestError as error:

        raise HTTPException(
            status_code=502,
            detail=
                "Could not connect to Deezer: "
                + str(error)
        )

    except json.JSONDecodeError as error:

        raise HTTPException(
            status_code=502,
            detail=
                "Deezer returned invalid JSON: "
                + str(error)
        )

    except Exception as error:

        print(
            "SEARCH ERROR:",
            repr(error)
        )

        raise HTTPException(
            status_code=500,
            detail=
                "Search failed: "
                + str(error)
        )


# ============================================================
# DOWNLOAD AUDIO
# ============================================================

@app.get("/api/download")
async def download_audio(
    url: str = Query(
        ...,
        min_length=1
    )
):

    """
    Download audio from a public YouTube URL
    supported by yt-dlp.

    Use only for content you are authorized
    to download.
    """

    allowed = (

        url.startswith(
            "https://www.youtube.com/"
        )

        or

        url.startswith(
            "https://youtube.com/"
        )

        or

        url.startswith(
            "https://youtu.be/"
        )

    )

    if not allowed:

        raise HTTPException(
            status_code=400,
            detail=
                "Please provide a valid YouTube URL."
        )

    job_id = uuid.uuid4().hex

    output_template = str(
        DOWNLOAD_DIR /
        f"{job_id}.%(ext)s"
    )

    command = [

        "yt-dlp",

        "--no-playlist",

        "-x",

        "--audio-format",
        "mp3",

        "--audio-quality",
        "192K",

        "--no-progress",

        "--newline",

        "-o",
        output_template,

        url
    ]

    try:

        process = (
            await
            asyncio.create_subprocess_exec(

                *command,

                stdout=asyncio.subprocess.PIPE,

                stderr=asyncio.subprocess.PIPE
            )
        )

        stdout, stderr = (
            await process.communicate()
        )

        if process.returncode != 0:

            error_text = (
                stderr.decode(
                    "utf-8",
                    errors="ignore"
                )
            )

            raise HTTPException(
                status_code=500,
                detail=
                    "Download failed: "
                    + error_text[-2000:]
            )

        mp3_file = (
            DOWNLOAD_DIR /
            f"{job_id}.mp3"
        )

        if not mp3_file.exists():

            possible_files = list(
                DOWNLOAD_DIR.glob(
                    f"{job_id}.*"
                )
            )

            if not possible_files:

                raise HTTPException(
                    status_code=500,
                    detail=
                        "Download completed but "
                        "the MP3 file was not found."
                )

            mp3_file = possible_files[0]

        return FileResponse(

            path=str(
                mp3_file
            ),

            media_type="audio/mpeg",

            filename=mp3_file.name
        )

    except FileNotFoundError:

        raise HTTPException(
            status_code=500,
            detail=
                "yt-dlp is not installed."
        )

    except HTTPException:

        raise

    except Exception as error:

        raise HTTPException(
            status_code=500,
            detail=
                "Download error: "
                + str(error)
        )


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/health")
async def health():

    return {
        "status": "ok",
        "service": "music-downloader"
    }
