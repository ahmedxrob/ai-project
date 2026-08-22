from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
import httpx
import yt_dlp
import os
import re
import asyncio
from pathlib import Path

app = FastAPI(title="Music Downloader")

# ---------------------------------------------------------
# CONFIG
# ---------------------------------------------------------

DEEZER_SEARCH_URL = "https://api.deezer.com/search"

# Change this if you want another music folder.
# For your Navidrome setup this can be:
# /share/navidrome/music
DOWNLOAD_DIR = Path("/downloads")

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------
# HELPERS
# ---------------------------------------------------------

def clean_filename(text: str) -> str:
    """
    Make a safe filename.
    """

    text = text or "Unknown"

    text = re.sub(r'[<>:"/\\|?*]', "", text)

    text = text.replace("\n", " ")
    text = text.replace("\r", " ")

    text = re.sub(r"\s+", " ", text)

    return text.strip()[:180]


def format_duration(seconds):
    """
    Convert seconds into MM:SS.
    """

    if not seconds:
        return "Unknown"

    minutes = int(seconds) // 60
    secs = int(seconds) % 60

    return f"{minutes}:{secs:02d}"


def normalize_result(track):
    """
    Convert Deezer track response into our own consistent format.
    """

    artist = track.get("artist") or {}
    album = track.get("album") or {}

    return {
        "id": track.get("id"),
        "title": track.get("title") or "Unknown",
        "artist": artist.get("name") or "Unknown artist",
        "artist_id": artist.get("id"),
        "album": album.get("title") or "Unknown album",
        "album_id": album.get("id"),
        "duration": track.get("duration") or 0,
        "duration_text": format_duration(track.get("duration")),
        "rank": track.get("rank") or 0,
        "isrc": track.get("isrc"),
        "cover": album.get("cover_big")
                 or album.get("cover_medium")
                 or album.get("cover")
                 or "",
        "artist_image": artist.get("picture_big")
                        or artist.get("picture_medium")
                        or "",
        "deezer_url": track.get("link") or "",
        "preview": track.get("preview") or "",
        "source": "deezer",
    }


# ---------------------------------------------------------
# HOME PAGE
# ---------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
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
            #0f172a,
            #111827
        );

    color: white;

    font-family:
        Arial,
        Helvetica,
        sans-serif;
}

.container {

    max-width: 1000px;

    margin: auto;
}

h1 {

    font-size: 32px;

    margin-bottom: 5px;
}

.subtitle {

    color: #9ca3af;

    margin-bottom: 25px;
}

.search {

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

    border: none;

    border-radius: 12px;

    padding: 14px 20px;

    background: #6366f1;

    color: white;

    font-size: 15px;

    cursor: pointer;

    font-weight: bold;
}

button:hover {

    background: #4f46e5;
}

.result {

    display: flex;

    gap: 15px;

    align-items: center;

    background: #1f2937;

    padding: 14px;

    margin-top: 10px;

    border-radius: 14px;
}

.cover {

    width: 70px;

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

.album {

    color: #9ca3af;

    margin-top: 3px;

    font-size: 14px;
}

.meta {

    color: #6b7280;

    font-size: 13px;

    margin-top: 4px;
}

.download {

    background: #10b981;

    white-space: nowrap;
}

.download:hover {

    background: #059669;
}

#status {

    margin: 15px 0;

    color: #9ca3af;
}

@media(max-width:700px) {

    body {
        padding: 15px;
    }

    .search {
        flex-direction: column;
    }

    .result {
        align-items: flex-start;
    }

    .download {
        padding: 10px;
    }
}

</style>

</head>


<body>

<div class="container">

<h1>🎵 Music Downloader</h1>

<div class="subtitle">
Search music and download it to your server.
</div>


<div class="search">

<input
    id="query"
    placeholder="Search song, artist or album..."
>

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
            "Enter something to search.";

        return;
    }


    status.textContent =
        "🔎 Searching...";

    results.innerHTML = "";


    try {

        const response =
            await fetch(
                "/api/search?q=" +
                encodeURIComponent(query)
            );


        const data =
            await response.json();


        if (!response.ok) {

            throw new Error(
                data.detail ||
                "Search failed"
            );
        }


        if (!data.results ||
            data.results.length === 0) {

            status.textContent =
                "❌ No music found.";

            return;
        }


        status.textContent =
            "🎵 " +
            data.results.length +
            " results";


        for (const item of data.results) {

            const div =
                document.createElement("div");

            div.className =
                "result";


            const cover =
                item.cover ||
                "";


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
                        ${escapeHtml(item.title)}
                    </div>

                    <div class="artist">
                        👤 ${escapeHtml(item.artist)}
                    </div>

                    <div class="album">
                        💿 ${escapeHtml(item.album)}
                    </div>

                    <div class="meta">
                        ⏱ ${escapeHtml(item.duration_text)}
                        &nbsp; • &nbsp;
                        Deezer rank:
                        ${item.rank || "N/A"}
                    </div>

                </div>

                <button
                    class="download"
                    onclick='downloadTrack(${JSON.stringify(item)})'
                >
                    ⬇️ Download
                </button>
            `;


            results.appendChild(div);
        }

    }

    catch(error) {

        console.error(error);

        status.textContent =
            "❌ " +
            error.message;
    }
}


async function downloadTrack(item) {

    const status =
        document.getElementById("status");


    status.textContent =
        "⏳ Finding and downloading: " +
        item.artist +
        " - " +
        item.title;


    try {

        const response =
            await fetch(
                "/api/download",
                {

                    method: "POST",

                    headers: {
                        "Content-Type":
                            "application/json"
                    },

                    body:
                        JSON.stringify({
                            title: item.title,
                            artist: item.artist,
                            album: item.album
                        })
                }
            );


        const data =
            await response.json();


        if (!response.ok) {

            throw new Error(
                data.detail ||
                "Download failed"
            );
        }


        status.textContent =
            "✅ Downloaded: " +
            data.filename;


        /*
         * Start browser download.
         */

        const link =
            document.createElement("a");

        link.href =
            data.download_url;

        link.download =
            data.filename;

        document.body.appendChild(link);

        link.click();

        link.remove();

    }

    catch(error) {

        console.error(error);

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

            if (event.key === "Enter") {

                searchMusic();

            }

        }
    );

</script>

</body>

</html>
"""


# ---------------------------------------------------------
# DEEZER SEARCH
# ---------------------------------------------------------

@app.get("/api/search")
async def search(
    q: str = Query(
        ...,
        min_length=1,
        max_length=200
    )
):

    params = {
        "q": q,
        "limit": 50
    }


    headers = {

        "User-Agent":
            "MusicDownloader/1.0",

        "Accept":
            "application/json"
    }


    try:

        async with httpx.AsyncClient(
            timeout=20,
            follow_redirects=True
        ) as client:

            response =
                await client.get(
                    DEEZER_SEARCH_URL,
                    params=params,
                    headers=headers
                )


            response.raise_for_status()


            content_type =
                response.headers.get(
                    "content-type",
                    ""
                ).lower()


            if "json" not in content_type:

                raise HTTPException(
                    status_code=502,
                    detail=
                    "Deezer returned a non-JSON response."
                )


            data =
                response.json()


    except httpx.HTTPError as error:

        raise HTTPException(
            status_code=502,
            detail=
            f"Deezer request failed: {error}"
        )


    except ValueError:

        raise HTTPException(
            status_code=502,
            detail=
            "Deezer returned invalid JSON."
        )


    tracks =
        data.get("data", [])


    results = []


    seen = set()


    for track in tracks:

        item =
            normalize_result(track)


        # Remove duplicate tracks.

        unique_key = (
            item["id"],
            item["artist"],
            item["title"]
        )


        if unique_key in seen:
            continue


        seen.add(unique_key)


        results.append(item)


    return {

        "query": q,

        "total":
            data.get(
                "total",
                len(results)
            ),

        "results":
            results

    }


# ---------------------------------------------------------
# YOUTUBE SEARCH
# ---------------------------------------------------------

def youtube_search(query: str):

    """
    Search YouTube using yt-dlp.

    This does NOT download the video.
    It only finds candidate videos.
    """

    options = {

        "quiet": True,

        "no_warnings": True,

        "skip_download": True,

        "extract_flat": True,

        "noplaylist": True,

        "default_search":
            "ytsearch5",

    }


    with yt_dlp.YoutubeDL(options) as ydl:

        info =
            ydl.extract_info(
                f"ytsearch5:{query}",
                download=False
            )


    entries =
        info.get("entries", [])


    return [
        entry
        for entry in entries
        if entry
    ]


def choose_youtube_result(
    entries,
    title,
    artist
):

    """
    Pick the most likely matching YouTube result.
    """

    target =
        f"{artist} {title}".lower()


    target_words =
        set(
            re.findall(
                r"[a-z0-9]+",
                target
            )
        )


    best = None

    best_score = -1


    for entry in entries:

        entry_title =
            entry.get(
                "title",
                ""
            )


        entry_channel =
            entry.get(
                "channel",
                ""
            )


        text =
            (
                entry_title +
                " " +
                entry_channel
            ).lower()


        words =
            set(
                re.findall(
                    r"[a-z0-9]+",
                    text
                )
            )


        score =
            len(
                target_words &
                words
            )


        # Prefer exact title pieces.

        if title.lower() in text:

            score += 5


        if artist.lower() in text:

            score += 5


        if score > best_score:

            best_score = score

            best = entry


    return best


# ---------------------------------------------------------
# DOWNLOAD
# ---------------------------------------------------------

@app.post("/api/download")
async def download(payload: dict):

    title =
        str(
            payload.get(
                "title",
                ""
            )
        ).strip()


    artist =
        str(
            payload.get(
                "artist",
                ""
            )
        ).strip()


    album =
        str(
            payload.get(
                "album",
                ""
            )
        ).strip()


    if not title or not artist:

        raise HTTPException(
            status_code=400,
            detail=
            "Missing title or artist."
        )


    search_query =
        f"{artist} {title}"


    try:

        entries =
            await asyncio.to_thread(
                youtube_search,
                search_query
            )


    except Exception as error:

        raise HTTPException(
            status_code=500,
            detail=
            f"YouTube search failed: {error}"
        )


    if not entries:

        raise HTTPException(
            status_code=404,
            detail=
            "No matching YouTube result found."
        )


    selected =
        choose_youtube_result(
            entries,
            title,
            artist
        )


    if not selected:

        raise HTTPException(
            status_code=404,
            detail=
            "Could not find a matching video."
        )


    video_url =
        selected.get("url")


    if not video_url:

        video_id =
            selected.get("id")

        if video_id:

            video_url =
                f"https://www.youtube.com/watch?v={video_id}"


    if not video_url:

        raise HTTPException(
            status_code=500,
            detail=
            "YouTube result has no URL."
        )


    safe_artist =
        clean_filename(artist)


    safe_title =
        clean_filename(title)


    filename =
        f"{safe_artist} - {safe_title}"


    output_template =
        str(
            DOWNLOAD_DIR /
            (
                filename +
                ".%(ext)s"
            )
        )


    ydl_options = {

        "format":
            "bestaudio/best",

        "outtmpl":
            output_template,

        "noplaylist":
            True,

        "quiet":
            True,

        "no_warnings":
            True,

        "postprocessors": [

            {

                "key":
                    "FFmpegExtractAudio",

                "preferredcodec":
                    "mp3",

                "preferredquality":
                    "192"

            }

        ],

        "postprocessor_args": [

            "-id3v2_version",
            "3"

        ],

        "writethumbnail":
            False,

        "addmetadata":
            True,

    }


    try:

        await asyncio.to_thread(
            run_ytdlp_download,
            video_url,
            ydl_options
        )


    except Exception as error:

        raise HTTPException(
            status_code=500,
            detail=
            f"Download failed: {error}"
        )


    final_file =
        DOWNLOAD_DIR /
        (
            filename +
            ".mp3"
        )


    if not final_file.exists():

        # Sometimes yt-dlp sanitizes the filename.
        # Find the newest MP3 instead.

        candidates =
            list(
                DOWNLOAD_DIR.glob(
                    "*.mp3"
                )
            )


        if candidates:

            final_file =
                max(
                    candidates,
                    key=lambda p:
                        p.stat().st_mtime
                )

        else:

            raise HTTPException(
                status_code=500,
                detail=
                "Download completed but MP3 was not found."
            )


    return {

        "success":
            True,

        "filename":
            final_file.name,

        "artist":
            artist,

        "title":
            title,

        "album":
            album,

        "youtube_url":
            video_url,

        "download_url":
            f"/api/file/{final_file.name}"

    }


def run_ytdlp_download(
    video_url,
    options
):

    with yt_dlp.YoutubeDL(options) as ydl:

        ydl.download(
            [video_url]
        )


# ---------------------------------------------------------
# SERVE DOWNLOADED FILE
# ---------------------------------------------------------

@app.get("/api/file/{filename}")
async def get_file(filename: str):

    safe_name =
        os.path.basename(filename)


    file_path =
        DOWNLOAD_DIR /
        safe_name


    if not file_path.exists():

        raise HTTPException(
            status_code=404,
            detail="File not found."
        )


    return FileResponse(

        path=file_path,

        filename=file_path.name,

        media_type="audio/mpeg"

    )


# ---------------------------------------------------------
# HEALTH
# ---------------------------------------------------------

@app.get("/health")
async def health():

    return {

        "status":
            "ok",

        "deezer":
            True,

        "yt_dlp":
            True,

        "download_directory":
            str(DOWNLOAD_DIR)

    }
