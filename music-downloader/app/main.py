from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
import asyncio
import json
import os
import re
import uuid
from pathlib import Path

app = FastAPI(title="Music Downloader")

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
    return (value[:180]) if value else "Unknown"


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
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Navidrome Music Downloader</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {
    --bg-main: #0b0f19;
    --card-bg: rgba(22, 30, 46, 0.75);
    --card-border: rgba(255, 255, 255, 0.08);
    --accent: #6366f1;
    --accent-hover: #4f46e5;
    --accent-glow: rgba(99, 102, 241, 0.35);
    --success: #10b981;
    --text-primary: #f3f4f6;
    --text-secondary: #9ca3af;
    --input-bg: rgba(15, 23, 42, 0.8);
}

* { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Plus Jakarta Sans', sans-serif; }

body {
    background: radial-gradient(circle at top, #1e1b4b 0%, #0b0f19 50%, #05070c 100%);
    background-attachment: fixed;
    color: var(--text-primary);
    min-height: 100vh;
    padding: 40px 20px;
}

.container { max-width: 960px; margin: 0 auto; }

header {
    text-align: center;
    margin-bottom: 35px;
}

header h1 {
    font-size: 2.4rem;
    font-weight: 700;
    background: linear-gradient(135deg, #ffffff 0%, #a5b4fc 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    letter-spacing: -0.02em;
    margin-bottom: 8px;
}

header p {
    color: var(--text-secondary);
    font-size: 0.98rem;
}

.search-card {
    background: var(--card-bg);
    backdrop-filter: blur(16px);
    border: 1px solid var(--card-border);
    padding: 12px;
    border-radius: 20px;
    display: flex;
    gap: 10px;
    box-shadow: 0 20px 40px rgba(0, 0, 0, 0.4);
    margin-bottom: 30px;
}

.search-card input {
    flex: 1;
    background: var(--input-bg);
    border: 1px solid var(--card-border);
    padding: 16px 20px;
    border-radius: 14px;
    color: #fff;
    font-size: 1rem;
    outline: none;
    transition: all 0.2s ease;
}

.search-card input:focus {
    border-color: var(--accent);
    box-shadow: 0 0 0 3px var(--accent-glow);
}

.search-card button {
    background: linear-gradient(135deg, var(--accent) 0%, #4338ca 100%);
    color: #fff;
    border: none;
    padding: 0 28px;
    border-radius: 14px;
    font-weight: 600;
    font-size: 0.98rem;
    cursor: pointer;
    transition: all 0.2s ease;
    box-shadow: 0 4px 15px var(--accent-glow);
}

.search-card button:hover {
    transform: translateY(-1px);
    box-shadow: 0 6px 20px var(--accent-glow);
}

.search-card button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
    transform: none;
}

.progress-panel {
    background: var(--card-bg);
    backdrop-filter: blur(16px);
    border: 1px solid var(--card-border);
    border-radius: 16px;
    padding: 20px;
    margin-bottom: 25px;
    display: none;
}

.progress-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 12px;
}

.progress-title {
    font-size: 0.92rem;
    font-weight: 600;
    color: var(--text-primary);
}

.progress-percent {
    font-size: 0.92rem;
    font-weight: 700;
    color: var(--accent);
}

.progress-track {
    width: 100%;
    height: 8px;
    background: rgba(255, 255, 255, 0.08);
    border-radius: 10px;
    overflow: hidden;
    margin-bottom: 10px;
}

.progress-fill {
    height: 100%;
    width: 0%;
    background: linear-gradient(90deg, var(--accent) 0%, #a855f7 100%);
    border-radius: 10px;
    transition: width 0.3s ease;
}

.progress-details {
    display: flex;
    justify-content: space-between;
    font-size: 0.82rem;
    color: var(--text-secondary);
}

.results-grid {
    display: flex;
    flex-direction: column;
    gap: 14px;
}

.result-card {
    background: var(--card-bg);
    backdrop-filter: blur(12px);
    border: 1px solid var(--card-border);
    border-radius: 16px;
    padding: 14px;
    display: flex;
    align-items: center;
    gap: 18px;
    transition: all 0.25s ease;
}

.result-card:hover {
    border-color: rgba(255, 255, 255, 0.18);
    transform: translateY(-2px);
    box-shadow: 0 10px 25px rgba(0, 0, 0, 0.3);
}

.thumb-wrapper {
    position: relative;
    width: 120px;
    height: 72px;
    border-radius: 10px;
    overflow: hidden;
    flex-shrink: 0;
    background: #1e293b;
}

.thumb-wrapper img {
    width: 100%;
    height: 100%;
    object-fit: cover;
}

.badge-duration {
    position: absolute;
    bottom: 6px;
    right: 6px;
    background: rgba(0, 0, 0, 0.75);
    backdrop-filter: blur(4px);
    padding: 2px 6px;
    border-radius: 6px;
    font-size: 0.72rem;
    font-weight: 600;
}

.track-info {
    flex: 1;
    min-width: 0;
}

.track-title {
    font-size: 1.02rem;
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    margin-bottom: 4px;
}

.track-artist {
    font-size: 0.88rem;
    color: var(--text-secondary);
    display: flex;
    align-items: center;
    gap: 6px;
}

.btn-group {
    display: flex;
    gap: 8px;
}

.btn-secondary {
    background: rgba(255, 255, 255, 0.06);
    border: 1px solid var(--card-border);
    color: var(--text-primary);
    padding: 10px 16px;
    border-radius: 10px;
    font-weight: 600;
    font-size: 0.85rem;
    cursor: pointer;
    transition: all 0.2s;
    text-decoration: none;
    display: inline-flex;
    align-items: center;
}

.btn-secondary:hover { background: rgba(255, 255, 255, 0.12); }

.btn-download {
    background: linear-gradient(135deg, #10b981 0%, #059669 100%);
    color: #fff;
    border: none;
    padding: 10px 18px;
    border-radius: 10px;
    font-weight: 600;
    font-size: 0.85rem;
    cursor: pointer;
    transition: all 0.2s;
    box-shadow: 0 4px 12px rgba(16, 185, 129, 0.25);
    display: inline-flex;
    align-items: center;
    gap: 6px;
}

.btn-download:hover {
    transform: translateY(-1px);
    box-shadow: 0 6px 16px rgba(16, 185, 129, 0.35);
}

.status-msg {
    text-align: center;
    color: var(--text-secondary);
    margin: 20px 0;
    font-size: 0.95rem;
}

@media(max-width: 640px) {
    .result-card { flex-direction: column; align-items: flex-start; }
    .thumb-wrapper { width: 100%; height: 160px; }
    .btn-group { width: 100%; justify-content: flex-end; margin-top: 8px; }
}
</style>
</head>
<body>
<div class="container">
    <header>
        <h1>🎵 Navidrome Downloader</h1>
        <p>Search music, embed cover art & tags, and save directly to your server</p>
    </header>

    <div class="search-card">
        <input id="query" placeholder="Search track, artist, or album..." autocomplete="off" />
        <button id="searchBtn" type="button">Search</button>
    </div>

    <div class="progress-panel" id="progressPanel">
        <div class="progress-header">
            <span class="progress-title" id="progressTitle">Downloading...</span>
            <span class="progress-percent" id="progressPercent">0%</span>
        </div>
        <div class="progress-track">
            <div class="progress-fill" id="progressFill"></div>
        </div>
        <div class="progress-details">
            <span id="progressStatus">Initializing download...</span>
            <span id="progressSpeed">-- MB/s</span>
        </div>
    </div>

    <div id="statusMsg" class="status-msg"></div>
    <div id="results" class="results-grid"></div>
</div>

<script>
let currentEventSource = null;

async function searchMusic() {
    const query = document.getElementById("query").value.trim();
    const statusMsg = document.getElementById("statusMsg");
    const results = document.getElementById("results");
    const searchBtn = document.getElementById("searchBtn");

    if (!query) return;

    statusMsg.textContent = "🔍 Searching YouTube...";
    results.innerHTML = "";
    searchBtn.disabled = true;

    try {
        const response = await fetch("api/search?q=" + encodeURIComponent(query));
        if (!response.ok) throw new Error("Search failed");
        
        const data = await response.json();
        if (data.length === 0) {
            statusMsg.textContent = "No results found.";
            return;
        }

        statusMsg.textContent = "";

        data.forEach(item => {
            const card = document.createElement("div");
            card.className = "result-card";

            card.innerHTML = `
                <div class="thumb-wrapper">
                    <img src="${item.thumbnail}" onerror="this.src='https://via.placeholder.com/120x72?text=Music'" />
                    <span class="badge-duration">${item.duration_text}</span>
                </div>
                <div class="track-info">
                    <div class="track-title">${escapeHtml(item.title)}</div>
                    <div class="track-artist">👤 ${escapeHtml(item.channel)}</div>
                </div>
                <div class="btn-group">
                    <a class="btn-secondary" href="${item.url}" target="_blank">Preview</a>
                    <button class="btn-download" onclick="startDownload('${item.url}', '${escapeJs(item.title)}')">
                        ⬇️ Save
                    </button>
                </div>
            `;
            results.appendChild(card);
        });
    } catch (err) {
        statusMsg.textContent = "❌ " + err.message;
    } finally {
        searchBtn.disabled = false;
    }
}

function startDownload(url, title) {
    if (currentEventSource) {
        currentEventSource.close();
    }

    const panel = document.getElementById("progressPanel");
    const pTitle = document.getElementById("progressTitle");
    const pPercent = document.getElementById("progressPercent");
    const pFill = document.getElementById("progressFill");
    const pStatus = document.getElementById("progressStatus");
    const pSpeed = document.getElementById("progressSpeed");

    panel.style.display = "block";
    pTitle.textContent = "Downloading: " + title;
    pPercent.textContent = "0%";
    pFill.style.width = "0%";
    pStatus.textContent = "Connecting...";
    pSpeed.textContent = "";

    const apiUrl = "api/download-stream?url=" + encodeURIComponent(url) + "&title=" + encodeURIComponent(title);
    currentEventSource = new EventSource(apiUrl);

    currentEventSource.onmessage = function(e) {
        const data = JSON.parse(e.data);

        if (data.type === "progress") {
            pPercent.textContent = data.percent + "%";
            pFill.style.width = data.percent + "%";
            pSpeed.textContent = data.speed || "";
            pStatus.textContent = "Downloading audio stream...";
        } else if (data.type === "status") {
            pStatus.textContent = data.message;
            if (data.message.includes("Embedding")) {
                pPercent.textContent = "95%";
                pFill.style.width = "95%";
            }
        } else if (data.type === "complete") {
            pPercent.textContent = "100%";
            pFill.style.width = "100%";
            pStatus.textContent = "✅ Saved: " + data.file;
            pSpeed.textContent = "";
            currentEventSource.close();
        } else if (data.type === "error") {
            pStatus.textContent = "❌ Error: " + data.message;
            currentEventSource.close();
        }
    };

    currentEventSource.onerror = function() {
        pStatus.textContent = "❌ Download connection failed.";
        currentEventSource.close();
    };
}

function escapeHtml(text) {
    return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function escapeJs(text) {
    return text.replace(/'/g, "\\'").replace(/"/g, '\\"');
}

document.getElementById("searchBtn").addEventListener("click", searchMusic);
document.getElementById("query").addEventListener("keydown", e => { if (e.key === "Enter") searchMusic(); });
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
# REAL-TIME STREAMING DOWNLOAD API
# ============================================================

@app.get("/api/download-stream")
async def download_stream(
    url: str = Query(..., min_length=1),
    title: str = Query("Unknown", min_length=1)
):
    if not (url.startswith("https://www.youtube.com/") or url.startswith("https://youtube.com/") or url.startswith("https://youtu.be/")):
        raise HTTPException(status_code=400, detail="Invalid YouTube URL.")

    async def event_generator():
        job_id = uuid.uuid4().hex
        output_template = str(DOWNLOAD_DIR / f"{job_id}.%(ext)s")

        command = [
            "yt-dlp",
            "--no-playlist",
            "-x",
            "--audio-format", "mp3",
            "--audio-quality", "192K",
            "--embed-thumbnail",
            "--add-metadata",
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

            progress_regex = re.compile(r"\[download\]\s+(\d+\.\d+)%\s+of\s+\S+\s+at\s+(\S+)")

            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                line_str = line.decode("utf-8", errors="ignore").strip()

                match = progress_regex.search(line_str)
                if match:
                    percent = float(match.group(1))
                    speed = match.group(2)
                    data = json.dumps({"type": "progress", "percent": percent, "speed": speed})
                    yield f"data: {data}\n\n"
                elif "[ExtractAudio]" in line_str or "[EmbedThumbnail]" in line_str or "[Metadata]" in line_str:
                    data = json.dumps({"type": "status", "message": "Embedding cover art & ID3 metadata tags..."})
                    yield f"data: {data}\n\n"

            await process.wait()

            if process.returncode != 0:
                stderr_data = await process.stderr.read()
                err_text = stderr_data.decode("utf-8", errors="ignore")
                data = json.dumps({"type": "error", "message": err_text[-300:]})
                yield f"data: {data}\n\n"
                return

            possible_files = list(DOWNLOAD_DIR.glob(f"{job_id}.*"))
            if not possible_files:
                data = json.dumps({"type": "error", "message": "Downloaded file not found."})
                yield f"data: {data}\n\n"
                return

            audio_file = possible_files[0]
            ext = audio_file.suffix
            clean_title = clean_filename(title)
            final_name = f"{clean_title}{ext}"
            final_path = DOWNLOAD_DIR / final_name

            if final_path.exists():
                final_name = f"{clean_title}_{job_id[:4]}{ext}"
                final_path = DOWNLOAD_DIR / final_name

            audio_file.rename(final_path)

            data = json.dumps({"type": "complete", "file": final_name})
            yield f"data: {data}\n\n"

        except Exception as err:
            data = json.dumps({"type": "error", "message": str(err)})
            yield f"data: {data}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
async def health():
    return {"status": "ok", "service": "music-downloader"}
