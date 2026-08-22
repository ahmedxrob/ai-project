from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
import httpx
import re

app = FastAPI()

DEEZER_SEARCH_URL = "https://api.deezer.com/search"


def clean(value):
    if value is None:
        return ""
    return str(value).strip()


def normalize(value):
    value = clean(value).lower()
    value = re.sub(r"[^\w\s]", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def score_result(item, query):
    q = normalize(query)
    title = normalize(item.get("title"))
    artist = normalize(item.get("artist"))
    album = normalize(item.get("album"))

    score = 0

    if title == q:
        score += 1000

    if artist == q:
        score += 900

    if q in title:
        score += 500

    if q in artist:
        score += 450

    for word in q.split():
        if word in title:
            score += 100

        if word in artist:
            score += 80

        if word in album:
            score += 30

    rank = item.get("rank", 0)

    try:
        score += min(int(rank) / 10000, 50)
    except Exception:
        pass

    return score


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
    background: #111827;
    color: white;
    font-family: Arial, sans-serif;
}

.container {
    max-width: 1000px;
    margin: auto;
}

h1 {
    margin-bottom: 5px;
}

.subtitle {
    color: #9ca3af;
    margin-bottom: 25px;
}

.search {
    display: flex;
    gap: 10px;
    margin-bottom: 25px;
}

input {
    flex: 1;
    padding: 15px;
    border: 0;
    border-radius: 10px;
    background: #1f2937;
    color: white;
    font-size: 16px;
    outline: none;
}

button {
    border: 0;
    border-radius: 10px;
    padding: 12px 18px;
    background: #6366f1;
    color: white;
    cursor: pointer;
    font-weight: bold;
}

button:hover {
    background: #4f46e5;
}

button:disabled {
    opacity: 0.5;
}

.result {
    display: flex;
    align-items: center;
    gap: 15px;
    background: #1f2937;
    padding: 14px;
    margin-bottom: 10px;
    border-radius: 12px;
}

.cover {
    width: 75px;
    height: 75px;
    object-fit: cover;
    border-radius: 10px;
    background: #374151;
}

.info {
    flex: 1;
    min-width: 0;
}

.title {
    font-size: 18px;
    font-weight: bold;
}

.artist {
    color: #a78bfa;
    margin-top: 5px;
}

.album {
    color: #9ca3af;
    margin-top: 4px;
}

.duration {
    color: #6b7280;
    margin-top: 4px;
    font-size: 13px;
}

.actions {
    display: flex;
    gap: 8px;
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
    width: 100%;
    margin-top: 10px;
}

#status {
    color: #9ca3af;
    margin-bottom: 15px;
}

.empty {
    text-align: center;
    padding: 40px;
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

    .result .actions {
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

<div class="search">

<input
    id="query"
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

    const input =
        document.getElementById("query");

    const status =
        document.getElementById("status");

    const results =
        document.getElementById("results");

    const button =
        document.getElementById("searchButton");


    const query =
        input.value.trim();


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

            results.innerHTML =
                '<div class="empty">🎵 No results found</div>';

            return;
        }


        status.textContent =
            "🎵 " +
            data.length +
            " results";


        data.forEach(function(item) {

            const div =
                document.createElement("div");

            div.className =
                "result";


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


            const cover =
                item.cover || "";


            const duration =
                formatDuration(
                    item.duration
                );


            div.innerHTML = `

<img
    class="cover"
    src="${cover}"
    onerror="this.style.display='none'"
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

<div id="preview-${item.id}"></div>

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

        });


    } catch(error) {

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
            "No preview available."
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


function downloadTrack(item) {

    alert(
        "Download will be connected next.\\n\\n" +
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

            if (event.key === "Enter") {

                searchMusic();

            }

        }
    );

</script>

</body>
</html>
"""


@app.get("/api/search")
async def search(
    q: str = Query(
        ...,
        min_length=1,
        max_length=200
    )
):

    query = q.strip()


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


    for track in data.get("data", []):

        artist_data = track.get(
            "artist",
            {}
        )


        album_data = track.get(
            "album",
            {}
        )


        item = {

            "id": track.get("id"),

            "title": clean(
                track.get("title")
            ),

            "artist": clean(
                artist_data.get("name")
            ),

            "album": clean(
                album_data.get("title")
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

            "cover": album_data.get(
                "cover_medium"
            ),

            "deezer_url": track.get(
                "link"
            )
        }


        results.append(item)


    results.sort(
        key=lambda item:
            score_result(
                item,
                query
            ),
        reverse=True
    )


    # Remove duplicates
    unique = []

    seen = set()


    for item in results:

        key = (
            normalize(item["title"]),
            normalize(item["artist"])
        )


        if key in seen:
            continue


        seen.add(key)

        unique.append(item)


    return unique[:50]


@app.get("/health")
async def health():

    return {
        "status": "ok"
    }
