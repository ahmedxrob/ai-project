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
        * {
            box-sizing: border-box;
        }

        body {
            font-family: Arial, sans-serif;
            background: #111827;
            color: white;
            margin: 0;
            padding: 30px;
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
        }

        input {
            flex: 1;
            padding: 15px;
            border: none;
            border-radius: 10px;
            font-size: 16px;
            background: #ffffff;
            color: #111827;
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

        #status {
            margin-top: 20px;
            color: #9ca3af;
        }

        #results {
            margin-top: 20px;
        }

        .result {
            background: #1f2937;
            padding: 20px;
            border-radius: 12px;
            margin-bottom: 12px;
        }

        .title {
            font-size: 19px;
            font-weight: bold;
        }

        .creator {
            color: #9ca3af;
            margin-top: 8px;
        }

        .identifier {
            color: #6b7280;
            font-size: 13px;
            margin-top: 8px;
            word-break: break-all;
        }

        .error {
            color: #f87171;
        }

        .loading {
            color: #60a5fa;
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
        >

        <button id="searchButton" onclick="searchMusic()">
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

    const button =
        document.getElementById("searchButton");


    if (!query) {

        status.className = "error";
        status.textContent =
            "Please enter something to search.";

        return;
    }


    status.className = "loading";
    status.textContent = "🔎 Searching...";

    results.innerHTML = "";

    button.disabled = true;


    try {

        /*
         * "./api/search" is important because
         * Home Assistant uses an Ingress URL.
         */

        const response = await fetch(
            "./api/search?q=" +
            encodeURIComponent(query)
        );


        const text = await response.text();


        if (!response.ok) {

            throw new Error(
                "Server returned " +
                response.status +
                ": " +
                text
            );
        }


        let data;

        try {

            data = JSON.parse(text);

        } catch (error) {

            console.error(
                "Server response:",
                text
            );

            throw new Error(
                "Server returned invalid JSON."
            );
        }


        if (!Array.isArray(data)) {

            throw new Error(
                "Unexpected server response."
            );
        }


        if (data.length === 0) {

            status.className = "";
            status.textContent =
                "No results found.";

            return;
        }


        status.className = "";
        status.textContent =
            "🎵 " +
            data.length +
            " results found.";


        data.forEach(item => {

            const card =
                document.createElement("div");

            card.className = "result";


            const title =
                document.createElement("div");

            title.className = "title";

            title.textContent =
                item.title ||
                "Unknown title";


            const creator =
                document.createElement("div");

            creator.className = "creator";

            creator.textContent =
                item.creator ||
                "Unknown creator";


            const identifier =
                document.createElement("div");

            identifier.className =
                "identifier";

            identifier.textContent =
                item.identifier ||
                "";


            card.appendChild(title);

            card.appendChild(creator);

            card.appendChild(identifier);

            results.appendChild(card);

        });

    } catch (error) {

        console.error(error);

        status.className = "error";

        status.textContent =
            "❌ Error: " +
            error.message;

    } finally {

        button.disabled = false;

    }

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
async def search_music(
    q: str = Query(..., min_length=1)
):

    params = [
        (
            "q",
            f"({q}) AND mediatype:audio"
        ),
        (
            "fl[]",
            "identifier"
        ),
        (
            "fl[]",
            "title"
        ),
        (
            "fl[]",
            "creator"
        ),
        (
            "rows",
            "25"
        ),
        (
            "page",
            "1"
        ),
        (
            "output",
            "json"
        )
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
                item.get(
                    "identifier",
                    ""
                ),

            "title":
                item.get(
                    "title",
                    "Unknown title"
                ),

            "creator":
                item.get(
                    "creator",
                    ""
                )

        })


    return results


@app.get("/health")
async def health():

    return {
        "status": "ok"
    }
