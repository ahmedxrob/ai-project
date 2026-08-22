from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
import httpx
import re

app = FastAPI()

DEEZER_SEARCH_URL = "https://api.deezer.com/search"


# ============================================================
# HELPERS
# ============================================================

def normalize(text: str) -> str:
    """
    Normalize text for better search matching.
    Supports Latin, Arabic, Cyrillic, etc.
    """
    text = (text or "").lower()

    text = re.sub(
        r"[^a-z0-9\u00C0-\u024F\u0400-\u04FF\u0600-\u06FF]+",
        " ",
        text
    )

    text = re.sub(r"\s+", " ", text)

    return text.strip()


def calculate_score(query: str, track: dict) -> float:

    title = track.get("title", "")
    artist = track.get("artist", {}).get("name", "")
    album = track.get("album", {}).get("title", "")

    query_normalized = normalize(query)

    title_normalized = normalize(title)
    artist_normalized = normalize(artist)
    album_normalized = normalize(album)

    query_words = set(query_normalized.split())
    title_words = set(title_normalized.split())
    artist_words = set(artist_normalized.split())
    album_words = set(album_normalized.split())

    score = 0

    # ========================================================
    # EXACT MATCHES
    # ========================================================

    if query_normalized == title_normalized:
        score += 1000

    if query_normalized == artist_normalized:
        score += 900

    if query_normalized == f"{artist_normalized} {title_normalized}":
        score += 1500

    if query_normalized == f"{title_normalized} {artist_normalized}":
        score += 1500

    # ========================================================
    # WORD MATCHING
    # ========================================================

    artist_matches = query_words.intersection(
        artist_words
    )

    title_matches = query_words.intersection(
        title_words
    )

    album_matches = query_words.intersection(
        album_words
    )

    score += len(artist_matches) * 350

    score += len(title_matches) * 250

    score += len(album_matches) * 75

    # ========================================================
    # PARTIAL MATCHES
    # ========================================================

    if query_normalized in artist_normalized:
        score += 400

    if query_normalized in title_normalized:
        score += 400

    # ========================================================
    # POPULARITY
    # ========================================================

    try:
        rank = int(track.get("rank", 0))
    except Exception:
        rank = 0

    # Small popularity bonus.
    # Matching quality remains much more important.
    score += min(rank / 10000, 100)

    return score


# ============================================================
# HOME PAGE
# ============================================================

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
                    #111827,
                    #0f172a
                );

            color: white;

            font-family:
                Arial,
                Helvetica,
                sans-serif;

            min-height: 100vh;
        }

        .container {

            max-width: 950px;

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

            width: 100%;
        }

        input {

            flex: 1;

            padding: 15px;

            border: none;

            outline: none;

            border-radius: 12px;

            background: #1f2937;

            color: white;

            font-size: 16px;
        }

        input::placeholder {

            color: #6b7280;
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

            transition: 0.2s;
        }

        button:hover {

            background: #818cf8;

            transform: translateY(-1px);
        }

        button:disabled {

            opacity: 0.5;

            cursor: not-allowed;

            transform: none;
        }

        #status {

            margin-top: 25px;

            margin-bottom: 15px;

            color: #9ca3af;
        }

        .result {

            background: #1f2937;

            padding: 15px;

            margin-top: 12px;

            border-radius: 14px;

            border:
                1px solid
                #374151;

            transition: 0.2s;
        }

        .result:hover {

            border-color: #6366f1;
        }

        .result-row {

            display: flex;

            gap: 15px;

            align-items: center;
        }

        .cover {

            width: 75px;

            height: 75px;

            border-radius: 10px;

            object-fit: cover;

            background: #111827;

            flex-shrink: 0;
        }

        .info {

            flex: 1;

            min-width: 0;
        }

        .title {

            font-size: 18px;

            font-weight: bold;

            overflow: hidden;

            text-overflow: ellipsis;

            white-space: nowrap;
        }

        .artist {

            color: #a78bfa;

            margin-top: 6px;
        }

        .album {

            color: #9ca3af;

            margin-top: 5px;

            font-size: 14px;
        }

        .rank {

            color: #6b7280;

            margin-top: 4px;

            font-size: 12px;
        }

        .actions {

            display: flex;

            gap: 8px;

            margin-top: 15px;

            flex-wrap: wrap;
        }

        .preview-button {

            background: #374151;
        }

        .preview-button:hover {

            background: #4b5563;
        }

        .deezer-button {

            background: #374151;

            text-decoration: none;

            color: white;

            padding:
                14px
                20px;

            border-radius: 12px;

            font-size: 14px;

            display: inline-flex;

            align-items: center;
        }

        .deezer-button:hover {

            background: #4b5563;
        }

        .empty {

            text-align: center;

            padding: 50px;

            color: #6b7280;
        }

        @media(max-width: 600px) {

            body {
                padding: 15px;
            }

            .search-box {
                flex-direction: column;
            }

            .search-box button {
                width: 100%;
            }

            .cover {
                width: 60px;
                height: 60px;
            }

            .title {
                font-size: 16px;
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
        document.getElementById("status");


    const results =
        document.getElementById("results");


    const button =
        document.getElementById("searchButton");


    if (!query) {

        status.textContent =
            "Enter a song or artist.";

        results.innerHTML = "";

        return;
    }


    button.disabled = true;

    status.textContent =
        "🔎 Searching Deezer...";

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


        if (!Array.isArray(data)) {

            throw new Error(
                "Invalid response from server"
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


        for (
            const item of data
        ) {

            createResult(
                item,
                results
            );

        }


    } catch (error) {

        console.error(error);

        status.textContent =
            "❌ " +
            error.message;

    } finally {

        button.disabled = false;

    }

}


// ============================================================
// CREATE RESULT
// ============================================================

function createResult(
    item,
    container
) {

    const div =
        document.createElement("div");


    div.className =
        "result";


    const cover =
        item.cover ||
        item.cover_big ||
        "";


    const preview =
        item.preview ||
        "";


    const deezer =
        item.deezer_url ||
        "#";


    div.innerHTML = `

        <div class="result-row">

            ${
                cover
                ?
                `
                <img
                    class="cover"
                    src="${escapeHtml(cover)}"
                    loading="lazy"
                    alt=""
                >
                `
                :
                `
                <div class="cover"></div>
                `
            }


            <div class="info">

                <div class="title">

                    🎵
                    ${escapeHtml(
                        item.title ||
                        "Unknown"
                    )}

                </div>


                <div class="artist">

                    👤
                    ${escapeHtml(
                        item.artist ||
                        "Unknown artist"
                    )}

                </div>


                <div class="album">

                    💿
                    ${escapeHtml(
                        item.album ||
                        "Unknown album"
                    )}

                </div>


                ${
                    item.rank
                    ?
                    `
                    <div class="rank">

                        ⭐ Popularity:
                        ${escapeHtml(
                            String(item.rank)
                        )}

                    </div>
                    `
                    :
                    ""
                }

            </div>

        </div>


        <div class="actions">

            ${
                preview
                ?
                `
                <button
                    class="preview-button"
                    onclick="previewMusic(
                        '${escapeJs(preview)}'
                    )"
                >
                    ▶️ Preview
                </button>
                `
                :
                ""
            }


            ${
                deezer !== "#"
                ?
                `
                <a
                    class="deezer-button"
                    href="${escapeHtml(deezer)}"
                    target="_blank"
                    rel="noopener"
                >
                    Open Deezer ↗
                </a>
                `
                :
                ""
            }

        </div>

    `;


    container.appendChild(div);

}


// ============================================================
// PREVIEW
// ============================================================

let currentAudio = null;


function previewMusic(url) {

    if (!url) {

        alert(
            "No preview available."
        );

        return;
    }


    if (currentAudio) {

        currentAudio.pause();

        currentAudio = null;
    }


    currentAudio =
        new Audio(url);


    currentAudio.play()
        .catch(error => {

            console.error(error);

            alert(
                "Unable to play preview."
            );

        });

}


// ============================================================
// HTML ESCAPE
// ============================================================

function escapeHtml(text) {

    const div =
        document.createElement("div");


    div.textContent =
        text || "";


    return div.innerHTML;
}


// ============================================================
// JAVASCRIPT STRING ESCAPE
// ============================================================

function escapeJs(text) {

    return String(text || "")
        .replace(/\\/g, "\\\\")
        .replace(/'/g, "\\'")
        .replace(/"/g, '\\"')
        .replace(/\n/g, "\\n")
        .replace(/\r/g, "\\r");

}


// ============================================================
// ENTER KEY
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
# DEEZER SEARCH API
# ============================================================

@app.get("/api/search")
async def search(
    q: str = Query(
        ...,
        min_length=1
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


        data =
            response.json()


    tracks =
        data.get(
            "data",
            []
        )


    # ========================================================
    # SCORE RESULTS
    # ========================================================

    scored = []


    for track in tracks:

        score =
            calculate_score(
                query,
                track
            )


        scored.append({

            "score": score,

            "track": track

        })


    # Best matches first
    scored.sort(

        key=lambda item:
            item["score"],

        reverse=True

    )


    # ========================================================
    # REMOVE DUPLICATES
    # ========================================================

    results = []

    seen = set()


    for item in scored:

        track =
            item["track"]


        track_id =
            track.get("id")


        if track_id in seen:

            continue


        seen.add(track_id)


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


        results.append({

            "id":
                track_id,


            "title":
                track.get(
                    "title",
                    "Unknown"
                ),


            "artist":
                artist_data.get(
                    "name",
                    "Unknown"
                ),


            "artist_id":
                artist_data.get(
                    "id"
                ),


            "album":
                album_data.get(
                    "title",
                    "Unknown"
                ),


            "album_id":
                album_data.get(
                    "id"
                ),


            "cover":
                album_data.get(
                    "cover_medium"
                ),


            "cover_big":
                album_data.get(
                    "cover_big"
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


            "deezer_url":
                track.get(
                    "link"
                ),


            "score":
                round(
                    item["score"],
                    2
                )

        })


        if len(results) >= 25:

            break


    return results


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/health")
async def health():

    return {
        "status": "ok"
    }
