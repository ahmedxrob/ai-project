from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import HTMLResponse
import asyncio
import json
import os
import re
import uuid
from pathlib import Path

app = FastAPI(title="Music Downloader")

# Changed to save to the Home Assistant share folder
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/share/navidrome_music"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_RESULTS = 20


# ============================================================
# HELPERS
# ============================================================

def clean_filename(value: str) -> str:
    value = value or "Unknown"
    value = re.sub(r'[\\/:*?"<>|]', "", value)
    value = re.sub(r"\s+", " ", value).strip()

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
        f"ytsearch{MAX_RESULTS}:{query}",
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            error = stderr.decode("utf-8", errors="ignore")
            raise RuntimeError(error[-2000:])

        data = json.loads(stdout.decode("utf-8", errors="ignore"))
        results = []

        for item in data.get("entries", []):
            if not item:
                continue
            
            video_id = item.get("id")
            if not video_id:
                continue

            channel = item.get("channel") or item.get("uploader") or "Unknown Artist"
            duration = item.get("duration", 0) or 0

            results.append({
                "id": video_id,
                "title": item.get("title", "Unknown"),
                "channel": channel,
                "duration": duration,
                "duration_text": format_duration(duration),
                "thumbnail": item.get("thumbnail") or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                "url": f"https://www.youtube.com/watch?v={video_id}",
            })

        return results

    except FileNotFoundError:
        raise RuntimeError("yt-dlp is not installed.")
    except json.JSONDecodeError:
        raise RuntimeError("YouTube returned invalid search data.")


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
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Music Downloader</title>
<style>
* { box-sizing: border-box; }
body { margin: 0; padding: 30px; background: linear-gradient(135deg, #111827, #0f172a); color: white; font-family: Arial, Helvetica, sans-serif; }
.container { max-width: 1100px; margin: 0 auto; }
h1 { margin-bottom: 5px; }
.subtitle { color: #9ca3af; margin-bottom: 25px; }
.search-box { display: flex; gap: 10px; margin-bottom: 20px; }
input { flex: 1; padding: 15px; border: none; border-radius: 12px; background: #1f2937; color: white; font-size: 16px; outline: none; }
button { padding: 14px 20px; border: none; border-radius: 12px; background: #6366f1; color: white; cursor: pointer; font-size: 15px; font-weight: bold; }
button:hover { opacity: 0.9; }
button:disabled { opacity: 0.5; cursor: wait; }
.result { display: flex; align-items: center; gap: 15px; background: #1f2937; padding: 12px; margin-top: 10px; border-radius: 14px; }
.cover { width: 110px; height: 70px; border-radius: 10px; object-fit: cover; background: #374151; flex-shrink: 0; }
.info { flex: 1; min-width: 0; }
.title { font-size: 17px; font-weight: bold; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.artist { color: #a78bfa; margin-top: 5px; }
.duration { color: #9ca3af; margin-top: 4px; font-size: 14px; }
.actions { display: flex; gap: 8px; align-items: center; }
.open { background: #374151; }
.download { background: #10b981; }
#status { color: #9ca3af; margin-bottom: 15px; }
.progress { width: 100%; height: 6px; background: #374151; border-radius: 10px; overflow: hidden; margin-top: 10px; display: none; }
.progress-bar { height: 100%; width: 0%; background: #10b981; transition: width 0.2s; }
@media(max-width: 700px) {
    body { padding: 15px; }
    .result { align-items: flex-start; flex-wrap: wrap; }
    .cover { width: 80px; height: 60px; }
    .actions { width: 100%; justify-content: flex-end; }
}
</style>
</head>
<body>
<div class="container">
<h1>🎵 Music Downloader</h1>
<div class="subtitle">Search YouTube music and download directly to Navidrome folder.</div>
<div class="search-box">
<input id="query" placeholder="Search song or artist..." autocomplete="off" />
<button id="searchButton" type="button">🔍 Search</button>
</div>
<div id="status"></div>
<div class="progress" id="progress"><div class="progress-bar" id="progressBar"></div></div>
<div id="results"></div>
</div>

<script>
async function searchMusic() {
    const input = document.getElementById("query");
    const status = document.getElementById("status");
    const results = document.getElementById("results");
    const searchButton = document.getElementById("searchButton");
    const query = input.value.trim();

    if (!query) {
        status.textContent = "Enter a song or artist.";
        return;
    }

    status.textContent = "🔎 Searching YouTube...";
    results.innerHTML = "";
    searchButton.disabled = true;

    try {
        const apiUrl = "api/search?q=" + encodeURIComponent(query);
        const response = await fetch(apiUrl, {
            method: "GET",
            headers: { "Accept": "application/json" }
        });

        const text = await response.text();
        if (!response.ok) throw new Error("Server error: " + text.substring(0, 300));
        
        const data = JSON.parse(text);
        if (!Array.isArray(data)) throw new Error("Invalid search response.");
        if (data.length === 0) {
            status.textContent = "No results found.";
            return;
        }

        status.textContent = "🎵 " + data.length + " results";

        data.forEach(item => {
            const div = document.createElement("div");
            div.className = "result";

            const image = document.createElement("img");
            image.className = "cover";
            image.src = item.thumbnail || "";
            image.onerror = function() { this.src = "https://via.placeholder.com/110x70?text=Music"; };

            const info = document.createElement("div");
            info.className = "info";
            
            const title = document.createElement("div");
            title.className = "title";
            title.textContent = item.title || "Unknown";

            const artist = document.createElement("div");
            artist.className = "artist";
            artist.textContent = "👤 " + (item.channel || "Unknown Artist");

            const duration = document.createElement("div");
            duration.className = "duration";
            duration.textContent = "⏱ " + (item.duration_text || "0:00");

            info.appendChild(title);
            info.appendChild(artist);
            info.appendChild(duration);

            const actions = document.createElement("div");
            actions.className = "actions";

            const youtubeButton = document.createElement("button");
            youtubeButton.className = "open";
            youtubeButton.textContent = "▶ YouTube";
            youtubeButton.onclick = function() { window.open(item.url, "_blank"); };

            const downloadButton = document.createElement("button");
            downloadButton.className = "download";
            downloadButton.textContent = "⬇ Download to Server";
            downloadButton.onclick = function() { downloadMusic(item.url, item.title); };

            actions.appendChild(youtubeButton);
            actions.appendChild(downloadButton);
            div.appendChild(image);
            div.appendChild(info);
            div.appendChild(actions);
            results.appendChild(div);
        });
    } catch (error) {
        console.error(error);
        status.textContent = "❌ " + error.message;
    } finally {
        searchButton.disabled = false;
    }
}

async function downloadMusic(url, title) {
    const status = document.getElementById("status");
    const progress = document.getElementById("progress");
    const progressBar = document.getElementById("progressBar");

    status.textContent = "⬇️ Downloading to Home Assistant...";
    progress.style.display = "block";
    progressBar.style.width = "10%";

    try {
        const apiUrl = "api/download?url=" + encodeURIComponent(url);
        
        const response = await fetch(apiUrl);
        progressBar.style.width = "70%";

        if (!response.ok) {
            const text = await response.text();
            throw new Error("Download failed: " + text.substring(0, 500));
        }

        const data = await response.json();
        progressBar.style.width = "100%";

        status.textContent = "✅ Saved to Navidrome folder: " + data.file;

        setTimeout(() => {
            progress.style.display = "none";
            progressBar.style.width = "0%";
        }, 3000);

    } catch (error) {
        console.error(error);
        progress.style.display = "none";
        progressBar.style.width = "0%";
        status.textContent = "❌ " + error.message;
    }
}

document.getElementById("searchButton").addEventListener("click", searchMusic);
document.getElementById("query").addEventListener("keydown", function(event) {
    if (event.key === "Enter") searchMusic();
});
</script>
</body>
</html>
"""


# ============================================================
# SEARCH API
# ============================================================

@app.get("/api/search")
async def search(q: str = Query(..., min_length=1)):
    try:
        results = await youtube_search(q)
        return results
    except Exception as error:
        raise HTTPException(status_code=500, detail="YouTube search failed: " + str(error))


# ============================================================
# DOWNLOAD API
# ============================================================

@app.get("/api/download")
async def download_audio(url: str = Query(..., min_length=1)):
    if not (url.startswith("https://www.youtube.com/") or url.startswith("https://youtube.com/") or url.startswith("https://youtu.be/")):
        raise HTTPException(status_code=400, detail="Invalid YouTube URL.")

    job_id = uuid.uuid4().hex
    output_template = str(DOWNLOAD_DIR / f"{job_id}.%(ext)s")

    command = [
        "yt-dlp",
        "--no-playlist",
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "192K",
        "--no-progress",
        "--newline",
        "-o", output_template,
        url,
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            error_text = stderr.decode("utf-8", errors="ignore")
            raise HTTPException(status_code=500, detail="Download failed: " + error_text[-2000:])

        possible_files = list(DOWNLOAD_DIR.glob(f"{job_id}.*"))

        if not possible_files:
            raise HTTPException(status_code=500, detail="Download completed but no audio file was found.")

        audio_file = possible_files[0]
        
        clean_name = clean_filename(audio_file.name)
        final_path = DOWNLOAD_DIR / clean_name
        audio_file.rename(final_path)

        return {
            "status": "success",
            "message": "Saved to server",
            "file": clean_name
        }

    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="yt-dlp is not installed.")
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail="Download error: " + str(error))


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "music-downloader",
    }
