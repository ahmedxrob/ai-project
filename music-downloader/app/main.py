from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
import asyncio
import json
import os
import re
import shutil
import uuid
from pathlib import Path

app = FastAPI(title="Music Downloader")

DOWNLOAD_DIR = Path(
    os.getenv("DOWNLOAD_DIR", "/downloads")
)

DOWNLOAD_DIR.mkdir(
    parents=True,
    exist_ok=True
)

MAX_RESULTS = 20


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

    return (value or "Unknown")[:180]


def format_duration(seconds):
    try:
        seconds = int(seconds or 0)

        minutes = seconds // 60
        seconds = seconds % 60

        return f"{minutes}:{seconds:02d}"

    except Exception:
        return "0:00"


# ============================================================
# YOUTUBE SEARCH
# ============================================================

async def youtube_search(query: str):

    command = [
        "yt-dlp",

        "--flat-playlist",

        "--dump-single-json",

        "--skip-download",

        "--no-warnings",

        f"ytsearch{MAX_RESULTS}:{query}"
    ]

    try:

        process = await asyncio.create_subprocess_exec(

            *command,

            stdout=asyncio.subprocess.PIPE,

            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await process.communicate()

        if process.returncode != 0:

            error = stderr.decode(
                "utf-8",
                errors="ignore"
            )

            raise RuntimeError(
                error[-2000:]
            )

        data = json.loads(
            stdout.decode(
                "utf-8",
                errors="ignore"
            )
        )

        results = []

        for item in data.get("entries", []):

            if not item:
                continue

            video_id = item.get("id")

            if not video_id:
                continue

            results.append({

                "id": video_id,

                "title": item.get(
                    "title",
                    "Unknown"
                ),

                "channel": item.get(
                    "channel"
                    or item.get("uploader"),
                    "Unknown Artist"
                ),

                "duration": item.get(
                    "duration",
                    0
                ),

                "duration_text":
                    format_duration(
                        item.get("duration", 0)
                    ),

                "thumbnail":
                    item.get("thumbnail")
                    or
                    f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",

                "url":
                    f"https://www.youtube.com/watch?v={video_id}"
            })

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

    margin: auto;
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

    padding: 14px 20px;

    border: none;

    border-radius: 12px;

    background: #6366f1;

    color: white;

    cursor: pointer;

    font-size: 15px;

    font-weight: bold;
}

button:hover {

    opacity: .9;
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

    width: 100px;

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

.duration {

    color: #9ca3af;

    margin-top: 4px;

    font-size: 14px;
}

.actions {

    display: flex;

    gap: 8px;

    align-items: center;
}

.open {

    background: #374151;
}

.download {

    background: #10b981;
}

#status {

    color: #9ca3af;

    margin-bottom: 15px;
}

@media(max-width: 700px) {

    body {
        padding: 15px;
    }

    .result {
        align-items: flex-start;
    }

    .actions {
        flex-direction: column;
    }

    .cover {
        width: 80px;
        height: 60px;
    }

}

</style>

</head>

<body>

<div class="container">

<h1>🎵 Music Downloader</h1>

<div class="subtitle">
Search YouTube music and download audio.
</div>

<div class="search-box">

<input
    id="query"
    placeholder="Search song or artist..."
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
        "🔎 Searching YouTube...";

    results.innerHTML = "";

    try {

        const response =
            await fetch(
                "./api/search?q=" +
                encodeURIComponent(query)
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
                text.substring(0, 300)
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

        if (!data.length) {

            status.textContent =
                "No results found.";

            return;
        }

        status.textContent =
            "🎵 " +
            data.length +
            " results";

        for (const item of data) {

            const div =
                document.createElement(
                    "div"
                );

            div.className =
                "result";

            div.innerHTML = `

                <img
                    class="cover"
                    src="${escapeHtml(
                        item.thumbnail
                    )}"
                >

                <div class="info">

                    <div class="title">
                        ${escapeHtml(
                            item.title
                        )}
                    </div>

                    <div class="artist">
                        👤 ${escapeHtml(
                            item.channel
                        )}
                    </div>

                    <div class="duration">
                        ⏱ ${escapeHtml(
                            item.duration_text
                        )}
                    </div>

                </div>

                <div class="actions">

                    <a
                        href="${escapeHtml(
                            item.url
                        )}"
                        target="_blank"
                    >

                        <button class="open">
                            ▶ YouTube
                        </button>

                    </a>

                    <button
                        class="download"
                        onclick="downloadMusic(
                            '${escapeHtml(
                                item.url
                            )}'
                        )"
                    >
                        ⬇ Download
                    </button>

                </div>
            `;

            results.appendChild(div);
        }

    } catch(error) {

        status.textContent =
            "❌ " +
            error.message;
    }
}


async function downloadMusic(url) {

    const status =
        document.getElementById("status");

    status.textContent =
        "⬇️ Downloading...";

    try {

        const response =
            await fetch(
                "./api/download?url=" +
                encodeURIComponent(url)
            );

        const contentType =
            response.headers.get(
                "content-type"
            ) || "";

        if (!response.ok) {

            if (
                contentType.includes(
                    "application/json"
                )
            ) {

                const error =
                    await response.json();

                throw new Error(
                    error.detail ||
                    "Download failed"
                );

            } else {

                throw new Error(
                    await response.text()
                );
            }
        }

        const blob =
            await response.blob();

        const blobUrl =
            URL.createObjectURL(blob);

        const a =
            document.createElement("a");

        a.href =
            blobUrl;

        a.download =
            "music.mp3";

        document.body.appendChild(a);

        a.click();

        a.remove();

        URL.revokeObjectURL(blobUrl);

        status.textContent =
            "✅ Download complete";

    } catch(error) {

        status.textContent =
            "❌ " +
            error.message;
    }
}


function escapeHtml(text) {

    const div =
        document.createElement("div");

    div.textContent =
        text || "";

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

@app.get("/api/search")
async def search(
    q: str = Query(
        ...,
        min_length=1
    )
):

    try:

        return await youtube_search(q)

    except Exception as error:

        raise HTTPException(
            status_code=500,
            detail=
                "YouTube search failed: "
                + str(error)
        )


# ============================================================
# DOWNLOAD
# ============================================================

@app.get("/api/download")
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
            detail="Invalid YouTube URL."
        )

    job_id =
        uuid.uuid4().hex

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

        process =
            await asyncio.create_subprocess_exec(

                *command,

                stdout=
                    asyncio.subprocess.PIPE,

                stderr=
                    asyncio.subprocess.PIPE
            )

        stdout, stderr =
            await process.communicate()

        if process.returncode != 0:

            error_text =
                stderr.decode(
                    "utf-8",
                    errors="ignore"
                )

            raise HTTPException(
                status_code=500,
                detail=
                    "Download failed: " +
                    error_text[-2000:]
            )

        possible_files =
            list(
                DOWNLOAD_DIR.glob(
                    f"{job_id}.*"
                )
            )

        if not possible_files:

            raise HTTPException(
                status_code=500,
                detail=
                    "Download finished but no file was created."
            )

        audio_file =
            possible_files[0]

        return FileResponse(

            path=str(audio_file),

            media_type="audio/mpeg",

            filename=
                clean_filename(
                    audio_file.name
                )
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
                "Download error: " +
                str(error)
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
