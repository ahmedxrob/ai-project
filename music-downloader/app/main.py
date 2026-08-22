from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from pathlib import Path
import asyncio
import os
import re
import uuid
import html

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

    q = normalize_text(query)

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

    if artist == q:
        score += 1000

    if title == q:
        score += 900

    if artist.startswith(q):
        score += 700

    if title.startswith(q):
        score += 600

    if q in artist:
        score += 400

    if q in title:
        score += 350

    if q in album:
        score += 100

    for word in q.split():

        if word in artist:
            score += 120

        if word in title:
            score += 80

        if word in album:
            score += 30

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
        "User-Agent": "MusicDownloader/1.0"
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

        try:

            data = response.json()

        except Exception:

            raise RuntimeError(
                "Deezer returned invalid JSON."
            )

    results = []

    for track in data.get("data", []):

        artist_data = (
            track.get("artist")
            or {}
        )

        album_data = (
            track.get("album")
            or {}
        )

        result = {

            "id": track.get("id"),

            "title": track.get(
                "title",
                "Unknown"
            ),

            "artist": artist_data.get(
                "name",
                "Unknown Artist"
            ),

            "artist_id": artist_data.get(
                "id"
            ),

            "album": album_data.get(
                "title",
                "Unknown Album"
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

            "preview": track.get(
                "preview"
            ),

            "deezer_url": track.get(
                "link"
            ),

            "cover": album_data.get(
                "cover_medium"
            ),

            "cover_big": album_data.get(
                "cover_big"
            ),

            "cover_xl": album_data.get(
                "cover_xl"
            ),

            "isrc": track.get(
                "isrc"
            ),

            "explicit": track.get(
                "explicit_lyrics",
                False
            )
        }

        result["score"] = calculate_score(
            result,
            query
        )

        results.append(result)

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

    return HTMLResponse(
        content="""
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

    max-width: 1000px;

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

    width: 70px;

    height: 70px;

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

    background: #451a1a;

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

    .actions {
        width: 100%;
        flex-wrap: wrap;
    }

    audio {
        width: 140px;
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

        status.innerHTML =
            '<div class="error">Enter a song or artist.</div>';

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
                        "Accept": "application/json"
                    }
                }
            );

        const contentType =
            response.headers.get(
                "content-type"
            ) || "";

        if (!contentType.includes(
            "application/json"
        )) {

            const text =
                await response.text();

            throw new Error(
                "Server returned non-JSON response: " +
                text.substring(0, 200)
            );
        }

        const data =
            await response.json();

        if (!response.ok) {

            throw new Error(
                data.detail ||
                "Search failed"
            );
        }

        if (!Array.isArray(data)) {

            throw new Error(
                "Invalid API response."
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
                item.cover || "";

            const preview =
                item.preview || "";

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
                        onclick="showDownloadInfo()"
                    >
                        ⬇ Download
                    </button>

                </div>
            `;

            results.appendChild(div);
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


function showDownloadInfo() {

    alert(
        "Use the YouTube download endpoint:\\n\\n" +
        "/api/download?url=YOUR_YOUTUBE_URL"
    );
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
    )


# ============================================================
# SEARCH API
# ============================================================

@app.get(
    "/api/search",
    response_class=JSONResponse
)
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
            detail="Search query cannot be empty."
        )

    try:

        results = await deezer_search(
            q,
            MAX_RESULTS
        )

        return JSONResponse(
            content=results
        )

    except httpx.HTTPStatusError as error:

        return JSONResponse(
            status_code=502,
            content={
                "detail":
                    "Deezer returned HTTP "
                    + str(
                        error.response.status_code
                    )
            }
        )

    except httpx.RequestError as error:

        return JSONResponse(
            status_code=502,
            content={
                "detail":
                    "Could not connect to Deezer: "
                    + str(error)
            }
        )

    except Exception as error:

        return JSONResponse(
            status_code=500,
            content={
                "detail":
                    "Search failed: "
                    + str(error)
            }
        )


# ============================================================
# DOWNLOAD
# ============================================================

@app.get(
    "/api/download"
)
async def download_audio(
    url: str = Query(
        ...,
        min_length=1
    )
):

    if not (
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
    ):

        raise HTTPException(
            status_code=400,
            detail="Please provide a valid YouTube URL."
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
            await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
        )

        stdout, stderr = (
            await process.communicate()
        )

        if process.returncode != 0:

            error_text = stderr.decode(
                "utf-8",
                errors="ignore"
            )

            raise HTTPException(
                status_code=500,
                detail=
                    "Download failed: "
                    + error_text[-2000:]
            )

        files = list(
            DOWNLOAD_DIR.glob(
                f"{job_id}.*"
            )
        )

        if not files:

            raise HTTPException(
                status_code=500,
                detail=
                    "Download completed but no output file was found."
            )

        mp3_file = next(
            (
                f for f in files
                if f.suffix.lower() == ".mp3"
            ),
            files[0]
        )

        return FileResponse(

            path=str(mp3_file),

            media_type="audio/mpeg",

            filename=mp3_file.name
        )

    except FileNotFoundError:

        raise HTTPException(
            status_code=500,
            detail="yt-dlp is not installed."
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
# HEALTH
# ============================================================

@app.get("/health")
async def health():

    return {
        "status": "ok",
        "service": "music-downloader"
    }


# ============================================================
# API TEST
# ============================================================

@app.get("/api/test")
async def api_test():

    return {
        "status": "ok",
        "message": "Music Downloader API is working"
    }
