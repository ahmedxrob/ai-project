from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
import httpx
import re
import html

app = FastAPI()

DEEZER_SEARCH_URL = "https://api.deezer.com/search"


# ============================================================
# Helpers
# ============================================================

def clean_text(value):
    if not value:
        return ""
    return str(value).strip()


def normalize(value):
    value = clean_text(value).lower()
    value = re.sub(r"[^\w\s]", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def score_result(item, query):
    """
    Rank results so exact artist/title matches appear first.
    """

    query_norm = normalize(query)

    title = normalize(item.get("title", ""))
    artist = normalize(item.get("artist", ""))
    album = normalize(item.get("album", ""))

    score = 0

    # Exact title
    if title == query_norm:
        score += 1000

    # Exact artist
    if artist == query_norm:
        score += 900

    # Query appears in title
    if query_norm and query_norm in title:
        score += 500

    # Query appears in artist
    if query_norm and query_norm in artist:
        score += 450

    # Query words
    words = query_norm.split()

    for word in words:
        if word in title:
            score += 100

        if word in artist:
            score += 80

        if word in album:
            score += 30

    # Deezer rank
    try:
        rank = int(item.get("rank", 0))
        score += min(rank / 10000, 50)
    except Exception:
        pass

    return score


# ============================================================
# HTML UI
# ============================================================

@app.get("/", response_class=HTMLResponse)
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
            #0f172a,
            #111827
        );

    color: white;

    font-family:
        Arial,
        Helvetica,
        sans-serif;

    min-height: 100vh;
}

.container {

    max-width: 1000px;

    margin: auto;
}

h1 {

    margin-bottom: 5px;

    font-size: 32px;
}

.subtitle {

    color: #9ca3af;

    margin-bottom: 25px;
}

.search-box {

    display: flex;

    gap: 10px;

    margin-bottom: 25px;
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

input:focus {

    box-shadow:
        0 0 0 2px #6366f1;
}

button {

    padding:
        14px
        20px;

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

button:disabled {

    opacity: 0.5;

    cursor: not-allowed;
}

#status {

    color: #9ca3af;

    margin-bottom: 15px;
}

.result {

    display: flex;

    align-items: center;

    gap: 16px;

    background: #1f2937;

    padding: 14px;

    margin-bottom: 10px;

    border-radius: 14px;

    transition:
        transform 0.15s,
        background 0.15s;
}

.result:hover {

    background: #273449;

    transform: translateY(-1px);
}

.cover {

    width: 75px;

    height: 75px;

    object-fit: cover;

    border-radius: 10px;

    background: #374151;

    flex-shrink: 0;
}

.info {

    flex: 1;

    min-width: 0;
}

.title {

    font-size: 18px;

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

.duration {

    color: #6b7280;

    font-size: 13px;

    margin-top: 4px;
}

.actions {

    display: flex;

    gap: 8px;

    flex-shrink: 0;
}

.preview {

    background: #374151;
}

.preview:hover {

    background: #4b5563;
}

.download {

    background: #10b981;
}

.download:hover {

    background: #059669;
}

audio {

    width: 100%;

    margin-top: 10px;
}

.empty {

    padding: 40px;

    text-align: center;

    color: #9ca3af;
}

@media (max-width: 700px) {

    body {

        padding: 15px;
    }

    .search-box {

        flex-direction: column;
    }

    .result {

        align-items: flex-start;
    }

    .actions {

        flex-direction: column;
    }

    .cover {

        width: 60px;

        height: 60px;
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
            type="text"
            placeholder="Search music..."
            autocomplete="off"
        >

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

let currentAudio = null;


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


    status.textContent =
        "🔎 Searching...";

    results.innerHTML = "";

    button.disabled = true;


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


        if (!Array.isArray(data)) {

            throw new Error(
                "Invalid server response"
            );
        }


        if (data.length === 0) {

            status.textContent =
                "No music found.";

            results.innerHTML = `
                <div class="empty">
                    🎵 No results found
                </div>
            `;

            return;
        }


        status.textContent =
            "🎵 " +
            data.length +
            " results";


        for (const item of data) {

            const div =
                document.createElement("div");

            div.className = "result";


            const cover =
                item.cover ||
                "";


            const title =
                escapeHtml(
                    item.title ||
                    "Unknown"
                );


            const artist =
                escapeHtml(
                    item.artist ||
                    "Unknown artist"
                );


            const album =
                escapeHtml(
                    item.album ||
                    "Album unknown"
                );


            const duration =
                formatDuration(
                    item.duration
                );


            div.innerHTML = `

                <img
                    class="cover"
                    src="${cover}"
                    onerror="this.style.visibility='hidden'"
                >

                <div class="info">

                    <div class="title">
                        🎵 ${title}
                    </div>

                    <div class="artist">
                        👤 ${artist}
                    </div>

                    <div class="album">
                        💿 ${album}
                    </div>

                    <div class="duration">
                        ⏱ ${duration}
                    </div>

                    <div
                        class="preview-container"
                        id="preview-${item.id}"
                    ></div>

                </div>

                <div class="actions">

                    <button
                        class="preview"
                        onclick='previewTrack(${JSON.stringify(item)})'
                    >
                        ▶️ Preview
                    </button>

                    <button
                        class="download"
                        onclick='downloadTrack(${JSON.stringify(item)})'
                    >
                        ⬇️ Download
                    </button>

                </div>
            `;


            results.appendChild(div);
        }


    } catch (error) {

        status.textContent =
            "❌ " +
            error.message;

    } finally {

        button.disabled = false;
    }
}


function previewTrack(item) {

    if (!item.preview) {

        alert(
            "No preview available for this track."
        );

        return;
    }


    if (currentAudio) {

        currentAudio.pause();

        currentAudio = null;
    }


    const container =
        document.getElementById(
            "preview-" + item.id
        );


    if (!container) {
        return;
    }


    container.innerHTML = `

        <audio
            controls
            autoplay
        >
            <source
                src="${item.preview}"
                type="audio/mpeg"
            >
        </audio>

    `;


    currentAudio =
        container.querySelector("audio");
}


async function downloadTrack(item) {

    /*
     * Download will be connected in the next step.
     */

    alert(
        "Download system is not connected yet.\\n\\n" +
        item.artist +
        " - " +
        item.title
    );
}


function formatDuration(seconds) {

    if (!seconds) {
        return "--:--";
    }


    seconds =
        Number(seconds);


    const minutes =
        Math.floor(
            seconds / 60
        );


    const remaining =
        Math.floor(
            seconds % 60
        );


    return (
        minutes +
        ":" +
        String(remaining)
            .padStart(2, "0")
    );
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
# Deezer Search API
# ============================================================

@app.get("/api/search")
async def search(
    q: str = Query(
        ...,
        min_length=1,
        max_length=200
    )
):

    query = q.strip()


    if not query:

        return []


    params = {

        "q": query,

        "limit": 50

    }


    headers = {

        "User-Agent":
            "MusicDownloader/1.0"

    }


    async with httpx.AsyncClient(
        timeout=30
    ) as client:

        response = await client.get(

            DEEZER_SEARCH_URL,

            params=params,

            headers=headers
        )


        response.raise_for_status()


        data = response.json()


    results = []


    for track in data.get(
        "data",
        []
    ):

        artist_data =
            track.get(
                "artist",
                {}
            )


        album_data =
            track.get(
                "album",
                {}
            )


        item = {

            "id":
                track.get(
                    "id"
                ),

            "title":
                clean_text(
                    track.get(
                        "title"
                    )
                ),

            "artist":
                clean_text(
                    artist_data.get(
                        "name"
                    )
                ),

            "album":
                clean_text(
                    album_data.get(
                        "title"
                    )
                ),

            "duration":
                track.get(
                    "duration",
                    0
                ),

            "rank":
                track.get(
                    "rank",
                    0
                ),

            "preview":
                track.get(
                    "preview"
                ),

            "cover":
                album_data.get(
                    "cover_medium"
                ),

            "deezer_url":
                track.get(
                    "link"
                )
        }


        results.append(item)


    # Rank the results
    results.sort(
        key=lambda item:
            score_result(
                item,
                query
            ),
        reverse=True
    )


    # Remove duplicate tracks
    unique = []

    seen = set()


    for item in results:

        key = (

            normalize(
                item["title"]
            ),

            normalize(
                item["artist"]
            )

        )


        if key in seen:
            continue


        seen.add(key)

        unique.append(item)


    return unique[:50]


# ============================================================
# Health
# ============================================================

@app.get("/health")
async def health():

    return {
        "status": "ok"
    }
