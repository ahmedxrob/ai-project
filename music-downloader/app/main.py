from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
import httpx
import re

app = FastAPI(title="Music Downloader")

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
        * {
            box-sizing: border-box;
        }

        body {
            margin: 0;
            padding: 25px;
            font-family: Arial, sans-serif;
            background: #111827;
            color: white;
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
        }

        button {
            padding: 15px 22px;
            border: none;
            border-radius: 10px;
            background: #6366f1;
            color: white;
            font-size: 16px;
            cursor: pointer;
        }

        button:hover {
            background: #4f46e5;
        }

        button:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }

        #status {
            margin-top: 20px;
            color: #9ca3af;
        }

        .result {
            background: #1f2937;
            border-radius: 14px;
            padding: 18px;
            margin-top: 12px;
        }

        .title {
            font-size: 20px;
            font-weight: bold;
        }

        .artist {
            margin-top: 7px;
            color: #c4b5fd;
        }

        .album {
            margin-top: 5px;
            color: #9ca3af;
        }

        .year {
            margin-top: 5px;
            color: #6b7280;
        }

        .mbid {
            margin-top: 8px;
            color: #4b5563;
            font-size: 12px;
        }

        .download {
            margin-top: 15px;
            background: #10b981;
        }

        .download:hover {
            background: #059669;
        }

        .notice {
            margin-top: 20px;
            padding: 15px;
            border-radius: 10px;
            background: #1e293b;
            color: #94a3b8;
            font-size: 14px;
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
            type="text"
            placeholder="Search song or artist..."
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

    <div class="notice">
        ℹ️ MusicBrainz provides music metadata. Audio downloading
        will be connected separately to sources that permit downloading.
    </div>

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

        status.textContent =
            "Enter an artist or song.";

        return;
    }


    button.disabled = true;

    status.textContent =
        "🔎 Searching music...";

    results.innerHTML = "";


    try {

        const response = await fetch(
            "./api/search?q=" +
            encodeURIComponent(query)
        );


        const text =
            await response.text();


        if (!response.ok) {

            throw new Error(
                response.status +
                ": " +
                text
            );
        }


        const data =
            JSON.parse(text);


        if (data.length === 0) {

            status.textContent =
                "No music found.";

            return;
        }


        status.textContent =
            "🎵 " +
            data.length +
            " results";


        data.forEach(item => {

            const card =
                document.createElement("div");

            card.className =
                "result";


            const title =
                document.createElement("div");

            title.className =
                "title";

            title.textContent =
                "🎵 " +
                item.title;


            const artist =
                document.createElement("div");

            artist.className =
                "artist";

            artist.textContent =
                item.artist;


            const album =
                document.createElement("div");

            album.className =
                "album";

            album.textContent =
                item.album ?
                "💿 " + item.album :
                "Album unknown";


            const year =
                document.createElement("div");

            year.className =
                "year";

            year.textContent =
                item.year ?
                "📅 " + item.year :
                "";


            const mbid =
                document.createElement("div");

            mbid.className =
                "mbid";

            mbid.textContent =
                item.mbid;


            const download =
                document.createElement("button");

            download.className =
                "download";

            download.textContent =
                "⬇️ Download";


            download.onclick =
                function() {

                    alert(
                        "Download source will be connected next."
                    );

                };


            card.appendChild(title);
            card.appendChild(artist);
            card.appendChild(album);
            card.appendChild(year);
            card.appendChild(mbid);
            card.appendChild(download);


            results.appendChild(card);

        });


    } catch (error) {

        console.error(error);

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

    # Clean the user's search.
    query = re.sub(
        r"\s+",
        " ",
        q.strip()
    )


    # MusicBrainz supports Lucene-style
    # recording and artist searches.
    mb_query = (
        f'recording:"{query}" '
        f'OR artist:"{query}"'
    )


    params = {
        "query": mb_query,
        "fmt": "json",
        "limit": 25,
        "offset": 0
    }


    headers = {
        "User-Agent":
        "HomeAssistant-Music-Downloader/0.1 "
        "(https://github.com/ahmedxrob/ai-project)"
    }


    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=True
    ) as client:

        response = await client.get(
            MUSICBRAINZ_URL,
            params=params,
            headers=headers
        )

        response.raise_for_status()

        data = response.json()


    recordings =
        data.get(
            "recordings",
            []
        )


    results = []


    for recording in recordings:

        title = recording.get(
            "title",
            "Unknown"
        )


        artist_names = []


        for credit in recording.get(
            "artist-credit",
            []
        ):

            if isinstance(credit, dict):

                artist = credit.get(
                    "artist",
                    {}
                )

                name = artist.get(
                    "name"
                )

                if name:
                    artist_names.append(name)


        artist_name = (
            ", ".join(artist_names)
            if artist_names
            else "Unknown artist"
        )


        releases = recording.get(
            "release-list",
            []
        )


        album = ""

        year = ""


        if releases:

            first_release =
                releases[0]

            album =
                first_release.get(
                    "title",
                    ""
                )

            date =
                first_release.get(
                    "date",
                    ""
                )

            if date:

                year =
                    date[:4]


        results.append({

            "title":
                title,

            "artist":
                artist_name,

            "album":
                album,

            "year":
                year,

            "mbid":
                recording.get(
                    "id",
                    ""
                )

        })


    return results


@app.get("/health")
async def health():

    return {
        "status": "ok"
    }
