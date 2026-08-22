from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
import httpx

app = FastAPI(title="Music Downloader")

ARCHIVE_SEARCH_URL = "https://archive.org/advancedsearch.php"


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
            font-family: Arial, sans-serif;
            background: #111827;
            color: white;
            margin: 0;
            padding: 25px;
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
        }

        .search {
            display: flex;
            gap: 10px;
            margin-top: 30px;
        }

        input {
            flex: 1;
            padding: 15px;
            border: none;
            border-radius: 10px;
            font-size: 16px;
        }

        button {
            padding: 15px 25px;
            border: none;
            border-radius: 10px;
            cursor: pointer;
            font-size: 16px;
            background: #6366f1;
            color: white;
        }

        button:hover {
            background: #4f46e5;
        }

        #results {
            margin-top: 25px;
        }

        .result {
            background: #1f2937;
            padding: 20px;
            border-radius: 12px;
            margin-bottom: 12px;
        }

        .title {
            font-size: 18px;
            font-weight: bold;
        }

        .creator {
            color: #9ca3af;
            margin-top: 6px;
        }

        .identifier {
            color: #6b7280;
            font-size: 13px;
            margin-top: 8px;
        }

        .status {
            margin-top: 20px;
            color: #9ca3af;
        }
    </style>
</head>

<body>

<div class="container">

    <h1>🎵 Music Downloader</h1>

    <div class="subtitle">
        Search downloadable music and add it to Navidrome.
    </div>

    <div class="search">

        <input
            id="query"
            type="text"
            placeholder="Search artist, album or song..."
            onkeydown="if(event.key === 'Enter') searchMusic()"
        >

        <button onclick="searchMusic()">
            🔍 Search
        </button>

    </div>

    <div id="status" class="status"></div>

    <div id="results"></div>

</div>

<script>

async function searchMusic() {

    const query =
        document.getElementById("query").value.trim();

    const results =
        document.getElementById("results");

    const status =
        document.getElementById("status");

    if (!query) {
        status.textContent =
            "Enter something to search for.";
        return;
    }

    status.textContent = "Searching...";
    results.innerHTML = "";

    try {

        const response = await fetch(
            "/api/search?q=" +
            encodeURIComponent(query)
        );

        const text = await response.text();

        if (!response.ok) {
            throw new Error(text);
        }

        let data;

        try {
            data = JSON.parse(text);
        } catch (e) {
            console.error("Server response:", text);
            throw new Error(
                "Server returned invalid JSON."
            );
        }

        if (!Array.isArray(data)) {
            throw new Error(
                "Unexpected response from server."
            );
        }

        if (data.length === 0) {

            status.textContent =
                "No results found.";

            return;
        }

        status.textContent =
            data.length + " results found.";

        data.forEach(item => {

            const card =
                document.createElement("div");

            card.className = "result";

            const title =
                document.createElement("div");

            title.className = "title";
            title.textContent =
                item.title || "Unknown title";

            const creator =
                document.createElement("div");

            creator.className = "creator";
            creator.textContent =
                item.creator || "Unknown creator";

            const identifier =
                document.createElement("div");

            identifier.className = "identifier";
            identifier.textContent =
                item.identifier || "";

            card.appendChild(title);
            card.appendChild(creator);
            card.appendChild(identifier);

            results.appendChild(card);

        });

    } catch (error) {

        console.error(error);

        status.textContent =
            "Error: " + error.message;
    }
}

</script>

</body>
</html>
"""


@app.get("/api/search")
async def search_music(
    q: str = Query(..., min_length=1)
):

    params = [
        ("q", f"({q}) AND mediatype:audio"),
        ("fl[]", "identifier"),
        ("fl[]", "title"),
        ("fl[]", "creator"),
        ("rows", "25"),
        ("page", "1"),
        ("output", "json"),
    ]

    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=True
    ) as client:

        response = await client.get(
            ARCHIVE_SEARCH_URL,
            params=params,
            headers={
                "User-Agent":
                "HomeAssistant-Music-Downloader/0.1"
            }
        )

        response.raise_for_status()

        data = response.json()

    documents = (
        data
        .get("response", {})
        .get("docs", [])
    )

    results = []

    for item in documents:

        results.append({
            "identifier":
                item.get("identifier", ""),

            "title":
                item.get("title", "Unknown title"),

            "creator":
                item.get("creator", "")
        })

    return results


@app.get("/health")
async def health():

    return {
        "status": "ok"
    }
