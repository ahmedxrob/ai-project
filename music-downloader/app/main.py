from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
import httpx

app = FastAPI()

MUSICBRAINZ_URL = "https://musicbrainz.org/ws/2/recording"


@app.get("/", response_class=HTMLResponse)
async def home():
    return """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">

    <title>Music Downloader</title>

    <style>
        body {
            margin: 0;
            padding: 30px;
            background: #111827;
            color: white;
            font-family: Arial, sans-serif;
        }

        .container {
            max-width: 900px;
            margin: auto;
        }

        input {
            width: 70%;
            padding: 14px;
            border: 0;
            border-radius: 10px;
            font-size: 16px;
        }

        button {
            padding: 14px 20px;
            border: 0;
            border-radius: 10px;
            background: #6366f1;
            color: white;
            cursor: pointer;
            font-size: 16px;
        }

        button:hover {
            background: #4f46e5;
        }

        .result {
            background: #1f2937;
            padding: 18px;
            margin-top: 15px;
            border-radius: 12px;
        }

        .title {
            font-size: 20px;
            font-weight: bold;
        }

        .artist {
            color: #a78bfa;
            margin-top: 6px;
        }

        .album {
            color: #9ca3af;
            margin-top: 6px;
        }

        #status {
            margin-top: 20px;
            color: #9ca3af;
        }
    </style>
</head>

<body>

<div class="container">

    <h1>🎵 Music Downloader</h1>

    <p>Search artists, songs and albums.</p>

    <div>
        <input
            id="query"
            placeholder="Search music..."
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
        document.getElementById("query").value.trim();

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

        const response = await fetch(
            "./api/search?q=" +
            encodeURIComponent(query)
        );

        const data = await response.json();

        if (!response.ok) {
            throw new Error(
                data.detail || "Search failed"
            );
        }

        if (data.length === 0) {

            status.textContent =
                "No results found.";

            return;
        }

        status.textContent =
            "🎵 " + data.length + " results";

        for (const item of data) {

            const div =
                document.createElement("div");

            div.className = "result";

            div.innerHTML = `
                <div class="title">
                    🎵 ${escapeHtml(item.title)}
                </div>

                <div class="artist">
                    👤 ${escapeHtml(item.artist)}
                </div>

                <div class="album">
                    💿 ${escapeHtml(
                        item.album || "Album unknown"
                    )}
                </div>
            `;

            results.appendChild(div);
        }

    } catch (error) {

        console.error(error);

        status.textContent =
            "❌ " + error.message;
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


@app.get("/api/search")
async def search(
    q: str = Query(..., min_length=1)
):

    params = {
        "query": q,
        "fmt": "json",
        "limit": 25
    }

    headers = {
        "User-Agent":
            "MusicDownloader/0.1"
    }

    async with httpx.AsyncClient(
        timeout=30
    ) as client:

        response = await client.get(
            MUSICBRAINZ_URL,
            params=params,
            headers=headers
        )

        response.raise_for_status()

        data = response.json()

    results = []

    for recording in data.get(
        "recordings",
        []
    ):

        artists = []

        for credit in recording.get(
            "artist-credit",
            []
        ):

            if isinstance(credit, dict):

                artist = credit.get(
                    "artist"
                )

                if artist:

                    name = artist.get(
                        "name"
                    )

                    if name:
                        artists.append(name)

        releases = recording.get(
            "release-list",
            []
        )

        album = ""

        if releases:

            album = releases[0].get(
                "title",
                ""
            )

        results.append({
            "title": recording.get(
                "title",
                "Unknown"
            ),

            "artist": ", ".join(
                artists
            ),

            "album": album
        })

    return results


@app.get("/health")
async def health():

    return {
        "status": "ok"
    }


@app.get("/api/deezer-test")
async def deezer_test():

    url = "https://api.deezer.com/search"

    params = {
        "q": "moon stormy",
        "limit": 10
    }

    async with httpx.AsyncClient(
        timeout=20
    ) as client:

        response = await client.get(
            url,
            params=params
        )

        response.raise_for_status()

        return response.json()
