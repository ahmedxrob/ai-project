from fastapi import FastAPI, Query, HTTPException, Body
from fastapi.responses import HTMLResponse, StreamingResponse
import asyncio
import json
import os
import re
import shutil
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
    "organize_by_artist": False,
    "normalize_audio": False
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
        if mb > 1024:
            gb = mb / 1024
            return f"{gb:.2f} GB"
        return f"{mb:.1f} MB"
    except Exception:
        return "0 MB"


# ============================================================
# YOUTUBE SEARCH
# ============================================================

async def youtube_search(query: str, max_results: int):
    # If the query is a direct URL, handle it specially or pass to yt-dlp flat playlist
    is_url = query.startswith("http://") or query.startswith("https://")
    
    command = [
        "yt-dlp",
        "--flat-playlist",
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
    ]
    
    if is_url:
        command.append(query)
    else:
        command.append(f"ytsearch{max_results}:{query}")

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

        entries = data.get("entries", [])
        # If it's a single video URL rather than playlist/search
        if not entries and "id" in data:
            entries = [data]

        for item in entries:
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
                "url": item.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}",
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

/* SEARCH & URL CARDS */
.search-card {
    background: var(--card-bg);
    backdrop-filter: blur(16px);
    border: 1px solid var(--card-border);
    padding: 12px;
    border-radius: 20px;
    display: flex;
    gap: 10px;
    box-shadow: 0 20px 40px rgba(0, 0, 0, 0.4);
    margin-bottom: 15px;
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

.url-card {
    background: var(--card-bg);
    backdrop-filter: blur(16px);
    border: 1px solid var(--card-border);
    padding: 12px;
    border-radius: 20px;
    display: flex;
    gap: 10px;
    margin-bottom: 25px;
}

.url-card input {
    flex: 1;
    background: var(--input-bg);
    border: 1px solid var(--card-border);
    padding: 12px 16px;
    border-radius: 14px;
    color: #fff;
    font-size: 0.92rem;
    outline: none;
}

.url-card button {
    background: linear-gradient(135deg, #10b981 0%, #059669 100%);
    color: #fff;
    border: none;
    padding: 0 20px;
    border-radius: 14px;
    font-weight: 600;
    font-size: 0.9rem;
    cursor: pointer;
}

/* STORAGE WIDGET */
.storage-widget {
    background: var(--card-bg);
    border: 1px solid var(--card-border);
    border-radius: 16px;
    padding: 16px 20px;
    margin-bottom: 20px;
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.storage-header {
    display: flex;
    justify-content: space-between;
    font-size: 0.88rem;
    font-weight: 600;
    color: var(--text-secondary);
}

.storage-bar-bg {
    width: 100%;
    height: 8px;
    background: rgba(255, 255, 255, 0.08);
    border-radius: 10px;
    overflow: hidden;
}

.storage-bar-fill {
    height: 100%;
    width: 0%;
    background: linear-gradient(90deg, var(--accent) 0%, #10b981 100%);
    border-radius: 10px;
    transition: width 0.4s ease;
}

/* INTERACTIVE PROGRESS PANEL */
.progress-panel {
    background: var(--card-bg);
    backdrop-filter: blur(16px);
    border: 1px solid var(--card-border);
    border-radius: 20px;
    padding: 22px;
    margin-bottom: 25px;
    display: none;
    box-shadow: 0 10px 30px rgba(0,0,0,0.5);
    animation: fadeIn 0.3s ease;
}

@keyframes fadeIn {
    from { opacity: 0; transform: translateY(-8px); }
    to { opacity: 1; transform: translateY(0); }
}

.progress-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 12px;
}

.progress-title { font-size: 0.98rem; font-weight: 700; color: var(--text-primary); }

.progress-right-header {
    display: flex;
    align-items: center;
    gap: 12px;
}

.progress-percent { font-size: 0.92rem; font-weight: 700; color: var(--accent); }

.btn-close-progress {
    background: rgba(255, 255, 255, 0.08);
    border: none;
    color: var(--text-secondary);
    width: 26px;
    height: 26px;
    border-radius: 50%;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.85rem;
    transition: all 0.2s;
}

.btn-close-progress:hover { background: rgba(239, 68, 68, 0.2); color: #fca5a5; }

.progress-track {
    width: 100%;
    height: 8px;
    background: rgba(255, 255, 255, 0.08);
    border-radius: 10px;
    overflow: hidden;
    margin-bottom: 15px;
}

.progress-fill {
    height: 100%;
    width: 0%;
    background: linear-gradient(90deg, var(--accent) 0%, #a855f7 100%);
    border-radius: 10px;
    transition: width 0.3s ease;
}

/* INTERACTIVE PIPELINE STEPS */
.progress-steps {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 8px;
    margin-bottom: 15px;
    background: rgba(15, 23, 42, 0.6);
    padding: 10px;
    border-radius: 12px;
    border: 1px solid rgba(255, 255, 255, 0.04);
}

.step-item {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 0.78rem;
    color: var(--text-secondary);
    font-weight: 500;
}

.step-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #374151;
    transition: all 0.3s;
    flex-shrink: 0;
}

.step-item.active { color: #fff; font-weight: 600; }
.step-item.active .step-dot {
    background: var(--accent);
    box-shadow: 0 0 10px var(--accent);
    animation: pulseDot 1.2s infinite alternate;
}

.step-item.completed { color: var(--success); font-weight: 600; }
.step-item.completed .step-dot { background: var(--success); }

@keyframes pulseDot {
    0% { transform: scale(1); opacity: 0.8; }
    100% { transform: scale(1.3); opacity: 1; }
}

.progress-details {
    display: flex;
    justify-content: space-between;
    font-size: 0.82rem;
    color: var(--text-secondary);
}

.queue-badge {
    background: rgba(99, 102, 241, 0.15);
    border: 1px solid rgba(99, 102, 241, 0.3);
    color: #a5b4fc;
    padding: 4px 10px;
    border-radius: 8px;
    font-size: 0.78rem;
    font-weight: 600;
    margin-top: 10px;
    display: inline-block;
}

.progress-actions {
    margin-top: 14px;
    padding-top: 12px;
    border-top: 1px solid rgba(255, 255, 255, 0.05);
    display: none;
    gap: 10px;
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
    transition: all 0.2s;
}
.btn-secondary:hover { background: rgba(255, 255, 255, 0.12); color: #fff; }

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
    transition: all 0.2s;
}

.btn-download:disabled {
    background: #374151;
    color: var(--text-secondary);
    cursor: not-allowed;
    box-shadow: none;
}

.badge-library {
    background: rgba(16, 185, 129, 0.15);
    border: 1px solid rgba(16, 185, 129, 0.3);
    color: #6ee7b7;
    padding: 6px 12px;
    border-radius: 10px;
    font-weight: 600;
    font-size: 0.82rem;
    display: inline-flex;
    align-items: center;
    gap: 4px;
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

.setting-label { font-weight: 600; font-size: 0.95rem; }
.setting-desc { font-size: 0.82rem; color: var(--text-secondary); margin-top: 2px; }

select, input[type="number"] {
    background: var(--input-bg);
    border: 1px solid var(--card-border);
    color: #fff;
    padding: 8px 12px;
    border-radius: 10px;
    outline: none;
    font-size: 0.9rem;
}

.switch { position: relative; display: inline-block; width: 46px; height: 24px; }
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
    .progress-steps { grid-template-columns: 1fr; }
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

        <div class="url-card">
            <input id="directUrl" placeholder="Or paste YouTube video / playlist URL directly..." autocomplete="off" />
            <button id="directUrlBtn" type="button" onclick="downloadDirectUrl()">📥 Download URL</button>
        </div>

        <div class="progress-panel" id="progressPanel">
            <div class="progress-header">
                <span class="progress-title" id="progressTitle">Downloading...</span>
                <div class="progress-right-header">
                    <span class="progress-percent" id="progressPercent">0%</span>
                    <button class="btn-close-progress" onclick="dismissProgressPanel()" title="Dismiss">✕</button>
                </div>
            </div>
            
            <div class="progress-track">
                <div class="progress-fill" id="progressFill"></div>
            </div>

            <div class="progress-steps">
                <div class="step-item active" id="stepDownload">
                    <span class="step-dot"></span> 1. Downloading Stream
                </div>
                <div class="step-item" id="stepProcess">
                    <span class="step-dot"></span> 2. Cleaning Tags & Artwork
                </div>
                <div class="step-item" id="stepDone">
                    <span class="step-dot"></span> 3. Ready for Navidrome
                </div>
            </div>

            <div class="progress-details">
                <span id="progressStatus">Connecting to stream...</span>
                <span id="progressSpeed">-- MB/s</span>
            </div>

            <div id="queueBadgeContainer"></div>

            <div class="progress-actions" id="progressActions">
                <button class="btn-secondary" onclick="switchTab('library')">📂 View in Library</button>
            </div>
        </div>

        <div id="statusMsg" class="status-msg"></div>
        <div id="results" class="results-grid"></div>
    </div>

    <!-- TAB 2: LIBRARY -->
    <div id="tab-library" class="tab-content">
        <div class="storage-widget" id="storageWidget">
            <div class="storage-header">
                <span id="storageText">Storage: Loading...</span>
                <span id="storagePercent">0%</span>
            </div>
            <div class="storage-bar-bg">
                <div class="storage-bar-fill" id="storageFill"></div>
            </div>
        </div>
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
                    <div class="setting-label">Audio Normalization (Loudness)</div>
                    <div class="setting-desc">Standardize track volume levels using ffmpeg loudnorm</div>
                </div>
                <label class="switch">
                    <input type="checkbox" id="set_normalize">
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
let downloadQueue = [];
let activeDownload = null;
let libraryFilesSet = new Set();

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
        loadDiskSpace();
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
        document.getElementById('set_normalize').checked = s.normalize_audio;
        document.getElementById('set_max_results').value = s.max_results || 20;
    } catch(e) {}
}

async function saveSettings() {
    const data = {
        audio_format: document.getElementById('set_format').value,
        audio_quality: document.getElementById('set_quality').value,
        embed_thumbnail: document.getElementById('set_thumb').checked,
        embed_metadata: document.getElementById('set_meta').checked,
        normalize_audio: document.getElementById('set_normalize').checked,
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

async function refreshLibraryCache() {
    try {
        const res = await fetch('api/library');
        const files = await res.json();
        libraryFilesSet.clear();
        files.forEach(f => {
            const baseName = f.name.substring(0, f.name.lastIndexOf('.')) || f.name;
            libraryFilesSet.add(baseName.toLowerCase());
        });
        document.getElementById('libCount').textContent = files.length;
    } catch(e) {}
}

async function loadDiskSpace() {
    try {
        const res = await fetch('api/disk-space');
        const data = await res.json();
        document.getElementById('storageText').textContent = `Storage: ${data.used} used / ${data.free} free (Total: ${data.total})`;
        document.getElementById('storagePercent').textContent = data.percent + '%';
        document.getElementById('storageFill').style.width = data.percent + '%';
    } catch(e) {}
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

    await refreshLibraryCache();

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
            const cleanedTitle = cleanFilenameJs(item.title).toLowerCase();
            const isInLibrary = libraryFilesSet.has(cleanedTitle);

            const card = document.createElement("div");
            card.className = "result-card";
            
            let actionHtml = '';
            if (isInLibrary) {
                actionHtml = `<div class="badge-library">✅ In Library</div>`;
            } else {
                actionHtml = `
                    <a class="btn-secondary" href="${item.url}" target="_blank">Preview</a>
                    <button class="btn-download" id="btn-${CSS.escape(item.id)}" onclick="startDownload('${item.url}', '${escapeJs(item.title)}', '${CSS.escape(item.id)}')">
                        ⬇️ Save
                    </button>
                `;
            }

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
                    ${actionHtml}
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

function downloadDirectUrl() {
    const urlInput = document.getElementById("directUrl");
    const url = urlInput.value.trim();
    if (!url) return;
    
    // Extract title or use default based on URL
    const title = "Direct Download " + Math.random().toString(36.substring(2, 7));
    startDownload(url, title, null);
    urlInput.value = "";
}

function cleanFilenameJs(val) {
    return (val || "Unknown").replace(/[\\/:*?"<>|]/g, "").replace(/\s+/g, " ").trim().substring(0, 180);
}

function updatePipelineStep(stepName) {
    const s1 = document.getElementById("stepDownload");
    const s2 = document.getElementById("stepProcess");
    const s3 = document.getElementById("stepDone");

    s1.className = "step-item";
    s2.className = "step-item";
    s3.className = "step-item";

    if (stepName === 'download') {
        s1.classList.add("active");
    } else if (stepName === 'process') {
        s1.classList.add("completed");
        s2.classList.add("active");
    } else if (stepName === 'done') {
        s1.classList.add("completed");
        s2.classList.add("completed");
        s3.classList.add("completed");
    }
}

function updateQueueDisplay() {
    const container = document.getElementById("queueBadgeContainer");
    if (downloadQueue.length > 0) {
        container.innerHTML = `<div class="queue-badge">📋 Queue: ${downloadQueue.length} track(s) waiting</div>`;
    } else {
        container.innerHTML = "";
    }
}

function dismissProgressPanel() {
    if (downloadQueue.length === 0) {
        if (currentEventSource) currentEventSource.close();
        document.getElementById("progressPanel").style.display = "none";
        activeDownload = null;
    } else {
        alert("Cannot dismiss while downloads are queued.");
    }
}

function startDownload(url, title, elementId) {
    if (elementId) {
        const btn = document.getElementById("btn-" + elementId);
        if (btn) {
            btn.disabled = true;
            btn.textContent = "⏳ Queued";
        }
    }

    downloadQueue.push({ url, title, elementId });
    updateQueueDisplay();

    if (!activeDownload) {
        processNextQueueItem();
    }
}

function processNextQueueItem() {
    if (downloadQueue.length === 0) {
        activeDownload = null;
        updateQueueDisplay();
        return;
    }

    activeDownload = downloadQueue.shift();
    updateQueueDisplay();

    if (currentEventSource) {
        currentEventSource.close();
    }

    const panel = document.getElementById("progressPanel");
    const pTitle = document.getElementById("progressTitle");
    const pPercent = document.getElementById("progressPercent");
    const pFill = document.getElementById("progressFill");
    const pStatus = document.getElementById("progressStatus");
    const pSpeed = document.getElementById("progressSpeed");
    const pActions = document.getElementById("progressActions");

    panel.style.display = "block";
    pActions.style.display = "none";
    pTitle.textContent = "Downloading: " + activeDownload.title;
    pPercent.textContent = "0%";
    pFill.style.width = "0%";
    pStatus.textContent = "Connecting to stream...";
    pSpeed.textContent = "-- MB/s";
    updatePipelineStep('download');

    const apiUrl = "api/download-stream?url=" + encodeURIComponent(activeDownload.url) + "&title=" + encodeURIComponent(activeDownload.title);
    currentEventSource = new EventSource(apiUrl);

    currentEventSource.onmessage = function(e) {
        const data = JSON.parse(e.data);

        if (data.type === "progress") {
            pPercent.textContent = data.percent + "%";
            pFill.style.width = data.percent + "%";
            pSpeed.textContent = data.speed || "";
            pStatus.textContent = "Downloading audio stream...";
            updatePipelineStep('download');
        } else if (data.type === "status") {
            pStatus.textContent = data.message;
            pPercent.textContent = "92%";
            pFill.style.width = "92%";
            pSpeed.textContent = "Processing";
            updatePipelineStep('process');

            let tick = 92;
            const procInterval = setInterval(() => {
                if (tick < 98) {
                    tick += 1;
                    pPercent.textContent = tick + "%";
                    pFill.style.width = tick + "%";
                } else {
                    clearInterval(procInterval);
                }
            }, 600);

        } else if (data.type === "complete") {
            pPercent.textContent = "100%";
            pFill.style.width = "100%";
            pStatus.textContent = "✅ Saved successfully!";
            pSpeed.textContent = "";
            updatePipelineStep('done');
            pActions.style.display = "flex";
            currentEventSource.close();
            refreshLibraryCache();

            if (activeDownload && activeDownload.elementId) {
                const btn = document.getElementById("btn-" + activeDownload.elementId);
                if (btn) {
                    const parent = btn.parentElement;
                    if (parent) {
                        parent.innerHTML = `<div class="badge-library">✅ In Library</div>`;
                    }
                }
            }

            setTimeout(processNextQueueItem, 1500);
        } else if (data.type === "error") {
            pStatus.textContent = "❌ Error: " + data.message;
            pSpeed.textContent = "";
            currentEventSource.close();
            setTimeout(processNextQueueItem, 2000);
        }
    };

    currentEventSource.onerror = function() {
        pStatus.textContent = "❌ Connection interrupted.";
        currentEventSource.close();
        setTimeout(processNextQueueItem, 2000);
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

async function deleteFile(filename) {
    if (!confirm("Delete " + filename + "?")) return;
    try {
        await fetch('api/library/' + encodeURIComponent(filename), { method: 'DELETE' });
        refreshLibraryCache();
        loadLibrary();
        loadDiskSpace();
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
document.getElementById("directUrl").addEventListener("keydown", e => { if (e.key === "Enter") downloadDirectUrl(); });
refreshLibraryCache();
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


@app.get("/api/disk-space")
async def get_disk_space():
    try:
        usage = shutil.disk_usage(DOWNLOAD_DIR)
        return {
            "total": format_size(usage.total),
            "used": format_size(usage.used),
            "free": format_size(usage.free),
            "percent": round((usage.used / usage.total) * 100, 1)
        }
    except Exception:
        return {"total": "Unknown", "used": "Unknown", "free": "Unknown", "percent": 0}


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
    if not (url.startswith("https://www.youtube.com/") or url.startswith("https://youtube.com/") or url.startswith("https://youtu.be/") or url.startswith("http://")):
        raise HTTPException(status_code=400, detail="Invalid YouTube URL.")

    settings = load_settings()
    fmt = settings.get("audio_format", "mp3")
    quality = settings.get("audio_quality", "320K")
    embed_thumb = settings.get("embed_thumbnail", True)
    embed_meta = settings.get("embed_metadata", True)
    normalize = settings.get("normalize_audio", False)

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

            data = json.dumps({"type": "status", "message": "Cleaning tags & metadata..."})
            yield f"data: {data}\n\n"

            cleaned_file = DOWNLOAD_DIR / f"clean_{job_id}{ext}"
            
            clean_command = [
                "ffmpeg",
                "-y",
                "-i", str(audio_file),
            ]
            
            if normalize:
                clean_command.extend(["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"])
            else:
                clean_command.extend(["-c", "copy"])

            clean_command.extend([
                "-metadata", "comment=",
                "-metadata", "description=",
                "-metadata", "purl=",
                str(cleaned_file)
            ])

            process_clean = await asyncio.create_subprocess_exec(
                *clean_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await process_clean.wait()

            if process_clean.returncode == 0 and cleaned_file.exists():
                audio_file.unlink()
                audio_file = cleaned_file

            # If title is generic (from direct URL), try to extract real title from metadata or fallback
            extracted_title = title
            if title.startswith("Direct Download"):
                extracted_title = audio_file.stem.replace(job_id, "").strip("_") or "Unknown"

            clean_title = clean_filename(extracted_title)
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
