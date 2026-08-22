from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
import asyncio
import json
import os
import re
import uuid
from pathlib import Path

app = FastAPI(title="Music Downloader")


# ============================================================
# CONFIGURATION
# ============================================================

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

    if not value:
        value = "Unknown"

    return value[:180]


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

        for item in data.get(
            "entries",
            []
        ):

            if not item:
                continue

            video_id = item.get("id")

            if not video_id:
                continue

            channel = (
                item.get("channel")
                or
                item.get("uploader")
                or
                "Unknown Artist"
            )

            results.append({

                "id": video_id,

                "title": item.get(
                    "title",
                    "Unknown"
                ),

                "channel": channel,

                "duration": item.get(
                    "duration",
                    0
                ),

                "duration_text":
                    format_duration(
                        item.get(
                            "duration",
                            0
                        )
                    ),

                "thumbnail":
                    item.get(
                        "thumbnail"
                    )
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

    opacity: 0.9;
}

button:disabled {

    opacity: 0.5;

    cursor: wait;
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

    width: 110px;

    height: 70px;

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

.progress {

    width: 100%;

    height: 6px;

    background: #374151;

    border-radius: 10px;

    overflow: hidden;

    margin-top: 10px;

    display: none;
}

.progress-bar {

    height: 100%;

    width: 0%;

    background: #10b981;

    transition: width 0.2s;
}

@media(max-width: 700px) {

    body {

        padding: 15px;
    }

    .result {

        align-items: flex-start;

        flex-wrap: wrap;
    }

    .cover {

        width: 80px;

        height: 60px;
    }

    .actions {

        width: 100%;

        justify-content: flex-end;
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

<button
    id="searchButton"
    onclick="searchMusic()"
>
🔍 Search
</button>

</div>

<div id="status"></div>

<div class="progress" id="progress">

    <div
        class="progress-bar"
        id="progressBar"
    ></div>

</div>

<div id="results"></div>

</div>


<script>


// ============================================================
// SEARCH
// ============================================================

async function searchMusic() {

    const query =
        document
            .getElementById("query")
            .value
            .trim();

    const status =
        document.getElementById(
            "status"
        );

    const results =
        document.getElementById(
            "results"
        );

    const searchButton =
        document.getElementById(
            "searchButton"
        );

    if (!query) {

        status.textContent =
            "Enter a song or artist.";

        return;
    }

    status.textContent =
        "🔎 Searching YouTube...";

    results.innerHTML = "";

    searchButton.disabled = true;

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

        if (
            !contentType.includes(
                "application/json"
            )
        ) {

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

        for (
            const item of data
        ) {

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
                    onerror="
                        this.src='https://via.placeholder.com/110x70?text=Music'
                    "
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

                        <button
                            class="open"
                        >

                            ▶ YouTube

                        </button>

                    </a>

                    <button
                        class="download"
                        onclick="downloadMusic(
                            '${escapeJs(
                                item.url
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

    } catch (error) {

        status.textContent =
            "❌ " +
            error.message;

    } finally {

        searchButton.disabled =
            false;
    }
}


// ============================================================
// DOWNLOAD
// ============================================================

async function downloadMusic(url) {

    const status =
        document.getElementById(
            "status"
        );

    const progress =
        document.getElementById(
            "progress"
        );

    const progressBar =
        document.getElementById(
            "progressBar"
        );

    status.textContent =
        "⬇️ Downloading audio...";

    progress.style.display =
        "block";

    progressBar.style.width =
        "10%";

    try {

        const response =
            await fetch(
                "./api/download?url=" +
                encodeURIComponent(url)
            );

        progressBar.style.width =
            "70%";

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

            }

            throw new Error(
                await response.text()
            );
        }

        const blob =
            await response.blob();

        progressBar.style.width =
            "100%";

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
            "music.mp3";

        document.body.appendChild(
            link
        );

        link.click();

        link.remove();

        URL.revokeObjectURL(
            blobUrl
        );

        status.textContent =
            "✅ Download complete";

        setTimeout(
            function() {

                progress.style.display =
                    "none";

                progressBar.style.width =
                    "0%";

            },
            1500
        );

    } catch (error) {

        progress.style.display =
            "none";

        progressBar.style.width =
            "0%";

        status.textContent =
            "❌ " +
            error.message;
    }
}


// ============================================================
// HTML ESCAPING
// ============================================================

function escapeHtml(text) {

    const div =
        document.createElement(
            "div"
        );

    div.textContent =
        text || "";

    return div.innerHTML;
}


// ============================================================
// JAVASCRIPT STRING ESCAPING
// ============================================================

function escapeJs(text) {

    return String(
        text || ""
    )
    .replace(
        /\\/g,
        "\\\\"
    )
    .replace(
        /'/g,
        "\\'"
    )
    .replace(
        /"/g,
        '\\"'
    );
}


// ============================================================
// ENTER TO SEARCH
// ============================================================

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

        results =
            await youtube_search(q)

        return results

    except Exception as error:

        raise HTTPException(
            status_code=500,
            detail=
                "YouTube search failed: "
                + str(error)
        )


# ============================================================
# DOWNLOAD API
# ============================================================

@app.get("/api/download")
async def download_audio(
    url: str = Query(
        ...,
        min_length=1
    )
):

    # Only accept YouTube URLs

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


    # Unique filename

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
                    "Download failed: "
                    +
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
                    "Download completed but no audio file was found."
            )


        audio_file =
            possible_files[0]


        return FileResponse(

            path=str(
                audio_file
            ),

            media_type=
                "audio/mpeg",

            filename=
                clean_filename(
                    audio_file.name
                )
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
                +
                str(error)
        )


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/health")
async def health():

    return {

        "status": "ok",

        "service":
            "music-downloader"

    }
