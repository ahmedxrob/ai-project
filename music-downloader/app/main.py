from fastapi import FastAPI, Query, HTTPException, Body
from fastapi.responses import HTMLResponse, StreamingResponse
import asyncio
import json
import os
import re
import uuid
from pathlib import Path

app = FastAPI(title="Navidrome Music Downloader")

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/share/navidrome/music"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

SETTINGS_FILE = DOWNLOAD_DIR / ".settings.json"

AUDIO_EXTENSIONS = {'.mp3', '.flac', '.m4a', '.ogg', '.wav', '.opus', '.aac', '.alac'}

DEFAULT_SETTINGS = {
    "audio_format": "mp3",
    "audio_quality": "320K",
    "embed_thumbnail": True,
    "embed_metadata": True,
    "max_results": 20,
    "organize_by_artist": False
}


# ============================================================
# HELPERS & SETTINGS
# ============================================================

def load_settings():
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {**DEFAULT_SETTINGS, **data}
        except Exception:
            pass
    return DEFAULT_SETTINGS.copy()


def save_settings(data: dict):
    settings = load_settings()
    settings.update(data)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    return settings


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


def format_size(size_bytes):
    try:
        mb = size_bytes / (1024 * 1024)
        return f"{mb:.1f} MB"
    except Exception:
        return "0 MB"


# ============================================================
# YOUTUBE SEARCH
# ============================================================

async def youtube_search(query: str, max_results: int):
    command = [
        "yt-dlp",
        "--flat-playlist",
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
        f"ytsearch{max_results}:{query}",
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
# FRONTEND UI
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def home():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Navidrome Downloader Pro</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {
    --bg-main: #0b0f19;
    --card-bg: rgba(22, 30, 46, 0.8);
    --card-border: rgba(255, 255, 255, 0.08);
    --accent: #6366f1;
    --accent-hover: #4f46e5;
    --accent-glow: rgba(99, 102, 241, 0.35);
    --success: #10b981;
    --danger: #ef4444;
    --text-primary: #f3f4f6;
    --text-secondary: #9ca3af;
    --input-bg: rgba(15, 23, 42, 0.85);
}

* { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Plus Jakarta Sans', sans-serif; }

body {
    background: radial-gradient(circle at top, #1e1b4b 0%, #0b0f19 50%, #05070c 100%);
    background-attachment: fixed;
    color: var(--text-primary);
    min-height: 100vh;
    padding: 30px 20px;
}

.container { max-width: 980px; margin: 0 auto; }

header {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 14px;
    margin-bottom: 25px;
}

header h1 {
    font-size: 2.2rem;
    font-weight: 700;
    background: linear-gradient(135deg, #ffffff 0%, #a5b4fc 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    letter-spacing: -0.02em;
}

.nav-tabs {
    display: flex;
    justify-content: center;
    gap: 8px;
    margin-bottom: 25px;
    background: var(--card-bg);
    padding: 6px;
    border-radius: 16px;
    border: 1px solid var(--card-border);
    backdrop-filter: blur(12px);
}

.tab-btn {
    background: transparent;
    border: none;
    color: var(--text-secondary);
    padding: 10px 24px;
    border-radius: 12px;
    font-weight: 600;
    font-size: 0.9rem;
    cursor: pointer;
    transition: all 0.2s;
}

.tab-btn:hover { color: #fff; }
.tab-btn.active {
    background: var(--accent);
    color: #fff;
    box-shadow: 0 4px 12px var(--accent-glow);
}

.tab-content { display: none; }
.tab-content.active { display: block; }

/* SEARCH TAB */
.search-card {
    background: var(--card-bg);
    backdrop-filter: blur(16px);
    border: 1px solid var(--card-border);
    padding: 12px;
    border-radius: 20px;
    display: flex;
    gap: 10px;
    box-shadow: 0 20px 40px rgba(0, 0, 0, 0.4);
    margin-bottom: 25px;
}

.search-card input {
    flex: 1;
    background: var(--input-bg);
    border: 1px solid var(--card-border);
    padding: 14px 18px;
    border-radius: 14px;
    color: #fff;
    font-size: 1rem;
    outline: none;
    transition: all 0.2s;
}

.search-card input:focus {
    border-color: var(--accent);
    box-shadow: 0 0 0 3px var(--accent-glow);
}

.search-card button {
    background: linear-gradient(135deg, var(--accent) 0%, #4338ca 100%);
    color: #fff;
    border: none;
    padding: 0 26px;
    border-radius: 14px;
    font-weight: 600;
    font-size: 0.95rem;
    cursor: pointer;
    transition: all 0.2s;
    box-shadow: 0 4px 15px var(--accent-glow);
}

.search-card button:hover { transform: translateY(-1px); }

/* PROGRESS PANEL */
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
    margin-bottom: 10px;
}

.progress-title { font-size: 0.92rem; font-weight: 600; color: var(--text-primary); }
.progress-percent { font-size: 0.92rem; font-weight: 700; color: var(--accent); }

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

/* RESULTS & CARDS */
.results-grid { display: flex; flex-direction: column; gap: 12px; }

.result-card {
    background: var(--card-bg);
    backdrop-filter: blur(12px);
    border: 1px solid var(--card-border);
    border-radius: 16px;
    padding: 12px 16px;
    display: flex;
    align-items: center;
    gap: 16px;
    transition: all 0.2s;
}

.result-card:hover { border-color: rgba(255, 255, 255, 0.18); transform: translateY(-1px); }

.thumb-wrapper {
    position: relative;
    width: 110px;
    height: 65px;
    border-radius: 10px;
    overflow: hidden;
    flex-shrink: 0;
    background: #1e293b;
}

.thumb-wrapper img { width: 100%; height: 100%; object-fit: cover; }

.badge-duration {
    position: absolute;
    bottom: 4px;
    right: 4px;
    background: rgba(0, 0, 0, 0.75);
    backdrop-filter: blur(4px);
    padding: 2px 5px;
    border-radius: 4px;
    font-size: 0.7rem;
    font-weight: 600;
}

.track-info { flex: 1; min-width: 0; }
.track-title { font-size: 0.98rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-bottom: 3px; }
.track-artist { font-size: 0.85rem; color: var(--text-secondary); }

.btn-group { display: flex; gap: 8px; }

.btn-secondary {
    background: rgba(255, 255, 255, 0.06);
    border: 1px solid var(--card-border);
    color: var(--text-primary);
    padding: 8px 14px;
    border-radius: 10px;
    font-weight: 600;
    font-size: 0.82rem;
    cursor: pointer;
    text-decoration: none;
    display: inline-flex;
    align-items: center;
}

.btn-download {
    background: linear-gradient(135deg, #10b981 0%, #059669 100%);
    color: #fff;
    border: none;
    padding: 8px 16px;
    border-radius: 10px;
    font-weight: 600;
    font-size: 0.82rem;
    cursor: pointer;
    box-shadow: 0 4px 12px rgba(16, 185, 129, 0.25);
}

.btn-danger {
    background: rgba(239, 68, 68, 0.15);
    border: 1px solid rgba(239, 68, 68, 0.3);
    color: #fca5a5;
    padding: 8px 14px;
    border-radius: 10px;
    font-weight: 600;
    font-size: 0.82rem;
    cursor: pointer;
}

.btn-danger:hover { background: rgba(239, 68, 68, 0.3); }

/* SETTINGS TAB */
.settings-card {
    background: var(--card-bg);
    backdrop-filter: blur(16px);
    border: 1px solid var(--card-border);
    border-radius: 20px;
    padding: 25px;
    display: flex;
    flex-direction: column;
    gap: 20px;
}

.setting-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding-bottom: 15px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.05);
}

.setting-row:last-child { border-bottom: none; padding-bottom: 0; }

.setting-label {
    font-weight: 600;
    font-size: 0.95rem;
}

.setting-desc {
    font-size: 0.82rem;
    color: var(--text-secondary);
    margin-top: 2px;
}

select, input[type="number"] {
    background: var(--input-bg);
    border: 1px solid var(--card-border);
    color: #fff;
    padding: 8px 12px;
    border-radius: 10px;
    outline: none;
    font-size: 0.9rem;
}

.switch {
    position: relative;
    display: inline-block;
    width: 46px;
    height: 24px;
}

.switch input { opacity: 0; width: 0; height: 0; }

.slider {
    position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0;
    background-color: #374151; transition: .3s; border-radius: 24px;
}

.slider:before {
    position: absolute; content: ""; height: 18px; width: 18px; left: 3px; bottom: 3px;
    background-color: white; transition: .3s; border-radius: 50%;
}

input:checked + .slider { background-color: var(--accent); }
input:checked + .slider:before { transform: translateX(22px); }

.save-btn {
    background: linear-gradient(135deg, var(--accent) 0%, #4338ca 100%);
    color: #fff;
    border: none;
    padding: 12px;
    border-radius: 12px;
    font-weight: 700;
    cursor: pointer;
    margin-top: 10px;
}

.status-msg { text-align: center; color: var(--text-secondary); margin: 15px 0; font-size: 0.9rem; }

@media(max-width: 640px) {
    .result-card { flex-direction: column; align-items: flex-start; }
    .thumb-wrapper { width: 100%; height: 140px; }
    .btn-group { width: 100%; justify-content: flex-end; }
}
</style>
</head>
<body>
<div class="container">
    <header>
        <svg width="44" height="44" viewBox="0 0 512 512" style="flex-shrink: 0;">
          <defs>
            <linearGradient id="waveGrad" x1="0%" y1="0%" x2="100%" y2="100%">
              <stop offset="0%" stop-color="#00F2FE" />
              <stop offset="100%" stop-color="#4FACFE" />
            </linearGradient>
          </defs>
          <rect width="512" height="512" rx="112" fill="#0F172A" />
          <rect x="112" y="216" width="24" height="80" rx="12" fill="url(#waveGrad)" opacity="0.4" />
          <rect x="160" y="176" width="24" height="160" rx="12" fill="url(#waveGrad)" opacity="0.75" />
          <rect x="328" y="176" width="24" height="160" rx="12" fill="url(#waveGrad)" opacity="0.75" />
          <rect x="376" y="216" width="24" height="80" rx="12" fill="url(#waveGrad)" opacity="0.4" />
          <path d="M 256 128 V 300 M 196 248 L 256 312 L 316 248" stroke="url(#waveGrad)" stroke-width="28" stroke-linecap="round" stroke-linejoin="round" fill="none" />
          <path d="M 196 376 H 316" stroke="url(#waveGrad)" stroke-width="24" stroke-linecap="round" />
        </svg>
        <h1>Navidrome Downloader Pro</h1>
    </header>

    <div class="nav-tabs">
        <button class="tab-btn active" onclick="switchTab('search')">🔍 Search & Download</button>
        <button class="tab-btn" onclick="switchTab('library')">📂 Library (<span id="libCount">0</span>)</button>
        <button class="tab-btn" onclick="switchTab('settings')">⚙️ Settings</button>
    </div>

    <!-- TAB 1: SEARCH -->
    <div id="tab-search" class="tab-content active">
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
                <span id="progressStatus">Initializing...</span>
                <span id="progressSpeed">-- MB/s</span>
            </div>
        </div>

        <div id="statusMsg" class="status-msg"></div>
        <div id="results" class="results-grid"></div>
    </div>

    <!-- TAB 2: LIBRARY -->
    <div id="tab-library" class="tab-content">
        <div id="libraryList" class="results-grid"></div>
    </div>

    <!-- TAB 3: SETTINGS -->
    <div id="tab-settings" class="tab-content">
        <div class="settings-card">
            <div class="setting-row">
                <div>
                    <div class="setting-label">Audio Format</div>
                    <div class="setting-desc">Preferred output audio format for downloads</div>
                </div>
                <select id="set_format">
                    <option value="mp3">MP3</option>
                    <option value="flac">FLAC (Lossless)</option>
                    <option value="m4a">M4A (AAC)</option>
                    <option value="opus">OPUS</option>
                </select>
            </div>

            <div class="setting-row">
                <div>
                    <div class="setting-label">Audio Quality / Bitrate</div>
                    <div class="setting-desc">Bitrate target for lossy formats (MP3/M4A)</div>
                </div>
                <select id="set_quality">
                    <option value="320K">320 Kbps (Highest)</option>
                    <option value="256K">256 Kbps (High)</option>
                    <option value="192K">192 Kbps (Medium)</option>
                    <option value="128K">128 Kbps (Low)</option>
                </select>
            </div>

            <div class="setting-row">
                <div>
                    <div class="setting-label">Embed Album Art</div>
                    <div class="setting-desc">Embed cover art into audio files for Navidrome</div>
                </div>
                <label class="switch">
                    <input type="checkbox" id="set_thumb">
                    <span class="slider"></span>
                </label>
            </div>

            <div class="setting-row">
                <div>
                    <div class="setting-label">Embed Metadata Tags</div>
                    <div class="setting-desc">Write ID3 tags (Artist, Title, Album) into the file</div>
                </div>
                <label class="switch">
                    <input type="checkbox" id="set_meta">
                    <span class="slider"></span>
                </label>
            </div>

            <div class="setting-row">
                <div>
                    <div class="setting-label">Max Search Results</div>
                    <div class="setting-desc">Number of YouTube items returned per search</div>
                </div>
                <input type="number" id="set_max_results" min="5" max="50" value="20" style="width:70px;">
            </div>

            <button class="save-btn" onclick="saveSettings()">Save Settings</button>
            <div id="settingsMsg" class="status-msg"></div>
        </div>
    </div>
</div>

<script>
let currentEventSource = null;

function switchTab(tab) {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    
    if (tab === 'search') {
        document.querySelectorAll('.tab-btn')[0].classList.add('active');
        document.getElementById('tab-search').classList.add('active');
    } else if (tab === 'library') {
        document.querySelectorAll('.tab-btn')[1].classList.add('active');
        document.getElementById('tab-library').classList.add('active');
        loadLibrary();
    } else if (tab === 'settings') {
        document.querySelectorAll('.tab-btn')[2].classList.add('active');
        document.getElementById('tab-settings').classList.add('active');
        loadSettings();
    }
}

async function loadSettings() {
    try {
        const res = await fetch('api/settings');
        const s = await res.json();
        document.getElementById('set_format').value = s.audio_format || 'mp3';
        document.getElementById('set_quality').value = s.audio_quality || '320K';
        document.getElementById('set_thumb').checked = s.embed_thumbnail;
        document.getElementById('set_meta').checked = s.embed_metadata;
        document.getElementById('set_max_results').value = s.max_results || 20;
    } catch(e) {}
}

async function saveSettings() {
    const data = {
        audio_format: document.getElementById('set_format').value,
        audio_quality: document.getElementById('set_quality').value,
        embed_thumbnail: document.getElementById('set_thumb').checked,
        embed_metadata: document.getElementById('set_meta').checked,
        max_results: parseInt(document.getElementById('set_max_results').value) || 20
    };
    const msg = document.getElementById('settingsMsg');
    msg.textContent = "Saving...";
    try {
        await fetch('api/settings', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(data)
        });
        msg.textContent = "✅ Settings saved!";
        setTimeout(() => msg.textContent = "", 3000);
    } catch(e) {
        msg.textContent = "❌ Failed to save settings.";
    }
}

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
                    <img src="${item.thumbnail}" onerror="this.src='https://via.placeholder.com/110x65?text=Music'" />
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
    if (currentEventSource) currentEventSource.close();

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
            loadLibraryCount();
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

async function loadLibrary() {
    const list = document.getElementById('libraryList');
    list.innerHTML = "Loading...";
    try {
        const res = await fetch('api/library');
        const files = await res.json();
        document.getElementById('libCount').textContent = files.length;

        if (files.length === 0) {
            list.innerHTML = `<div class="status-msg">No files downloaded yet.</div>`;
            return;
        }

        list.innerHTML = "";
        files.forEach(f => {
            const card = document.createElement('div');
            card.className = 'result-card';
            card.innerHTML = `
                <div class="track-info">
                    <div class="track-title">🎵 ${escapeHtml(f.name)}</div>
                    <div class="track-artist">📦 ${f.size}</div>
                </div>
                <div class="btn-group">
                    <button class="btn-danger" onclick="deleteFile('${escapeJs(f.name)}')">🗑 Delete</button>
                </div>
            `;
            list.appendChild(card);
        });
    } catch(e) {
        list.innerHTML = `<div class="status-msg">Failed to load library.</div>`;
    }
}

async function loadLibraryCount() {
    try {
        const res = await fetch('api/library');
        const files = await res.json();
        document.getElementById('libCount').textContent = files.length;
    } catch(e) {}
}

async function deleteFile(filename) {
    if (!confirm("Delete " + filename + "?")) return;
    try {
        await fetch('api/library/' + encodeURIComponent(filename), { method: 'DELETE' });
        loadLibrary();
    } catch(e) {
        alert("Failed to delete file.");
    }
}

function escapeHtml(text) {
    return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function escapeJs(text) {
    return text.replace(/'/g, "\\'").replace(/"/g, '\\"');
}

document.getElementById("searchBtn").addEventListener("click", searchMusic);
document.getElementById("query").addEventListener("keydown", e => { if (e.key === "Enter") searchMusic(); });
loadLibraryCount();
</script>
</body>
</html>
"""


# ============================================================
# API ENDPOINTS
# ============================================================

@app.get("/api/settings")
async def get_settings():
    return load_settings()


@app.post("/api/settings")
async def update_settings(data: dict = Body(...)):
    return save_settings(data)


@app.get("/api/library")
async def get_library():
    files = []
    for path in DOWNLOAD_DIR.iterdir():
        if path.is_file() and not path.name.startswith("."):
            if path.suffix.lower() in AUDIO_EXTENSIONS:
                files.append({
                    "name": path.name,
                    "size": format_size(path.stat().st_size)
                })
    return sorted(files, key=lambda x: x["name"])


@app.delete("/api/library/{filename}")
async def delete_library_file(filename: str):
    file_path = DOWNLOAD_DIR / filename
    if file_path.exists() and file_path.is_file():
        file_path.unlink()
        return {"status": "deleted"}
    raise HTTPException(status_code=404, detail="File not found")


@app.get("/api/search")
async def search(q: str = Query(..., min_length=1)):
    settings = load_settings()
    try:
        results = await youtube_search(q, settings.get("max_results", 20))
        return results
    except Exception as error:
        raise HTTPException(status_code=500, detail="YouTube search failed: " + str(error))


@app.get("/api/download-stream")
async def download_stream(
    url: str = Query(..., min_length=1),
    title: str = Query("Unknown", min_length=1)
):
    if not (url.startswith("https://www.youtube.com/") or url.startswith("https://youtube.com/") or url.startswith("https://youtu.be/")):
        raise HTTPException(status_code=400, detail="Invalid YouTube URL.")

    settings = load_settings()
    fmt = settings.get("audio_format", "mp3")
    quality = settings.get("audio_quality", "320K")
    embed_thumb = settings.get("embed_thumbnail", True)
    embed_meta = settings.get("embed_metadata", True)

    async def event_generator():
        job_id = uuid.uuid4().hex
        output_template = str(DOWNLOAD_DIR / f"{job_id}.%(ext)s")

        command = [
            "yt-dlp",
            "--no-playlist",
            "-x",
            "--audio-format", fmt,
            "--audio-quality", quality,
            "--newline",
            "-o", output_template,
        ]

        if embed_thumb:
            command.append("--embed-thumbnail")
        if embed_meta:
            command.append("--add-metadata")

        command.append(url)

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
                    data = json.dumps({"type": "status", "message": "Embedding cover art & tags..."})
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
