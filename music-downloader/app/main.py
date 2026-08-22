from fastapi import FastAPI, Query, HTTPException, Body, Depends, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from typing import Optional
import asyncio
import json
import os
import re
import uuid
import secrets
from pathlib import Path

app = FastAPI(title="Navidrome Music Downloader Pro")

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/share/navidrome/music"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

TEMP_DIR = DOWNLOAD_DIR / ".tmp"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

SETTINGS_FILE = DOWNLOAD_DIR / ".settings.json"
AUDIO_EXTENSIONS = {'.mp3', '.flac', '.m4a', '.ogg', '.wav', '.opus', '.aac', '.alac'}

JOBS = {}

DEFAULT_SETTINGS = {
    "audio_format": "mp3",
    "audio_quality": "320K",
    "embed_thumbnail": True,
    "embed_metadata": True,
    "embed_lyrics": True,
    "normalize_audio": True,
    "max_results": 20,
    "organize_by_artist": True,
    "auth_enabled": False,
    "auth_user": "admin",
    "auth_pass": "admin123"
}

security = HTTPBasic(auto_error=False)

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


def verify_auth(credentials: Optional[HTTPBasicCredentials] = Depends(security)):
    settings = load_settings()
    if not settings.get("auth_enabled", False):
        return True
    
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )

    correct_user = secrets.compare_digest(credentials.username, settings.get("auth_user", "admin"))
    correct_pass = secrets.compare_digest(credentials.password, settings.get("auth_pass", "admin123"))
    
    if not (correct_user and correct_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True


def clean_filename(value: str) -> str:
    value = value or "Unknown"
    value = re.sub(r'[\\/:*?"<>|]', "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return (value[:180]) if value else "Unknown"


def strip_youtube_junk(title: str) -> str:
    patterns = [
        r'\s*[\(\[]\s*(official\s*)?(music\s*)?(video|audio|lyric\s*video|hd|4k|clip\s*officiel)\s*[\)\]]',
        r'\s*[\(\[]\s*lyrics?\s*[\)\]]',
        r'\s*[\(\[]\s*hd\s*[\)\]]',
        r'\s*[\(\[]\s*4k\s*[\)\]]',
    ]
    for p in patterns:
        title = re.sub(p, '', title, flags=re.IGNORECASE)
    return title.strip()


def format_duration(seconds):
    try:
        seconds = int(seconds or 0)
        minutes = seconds // 60
        sec = seconds % 60
        return f"{minutes}:{sec:02d}"
    except Exception:
        return "0:00"


def format_size(size_bytes):
    try:
        mb = size_bytes / (1024 * 1024)
        return f"{mb:.1f} MB"
    except Exception:
        return "0 MB"


def get_existing_filenames():
    existing = set()
    for path in DOWNLOAD_DIR.rglob("*"):
        if path.is_file() and not any(part.startswith(".") for part in path.relative_to(DOWNLOAD_DIR).parts):
            if path.suffix.lower() in AUDIO_EXTENSIONS:
                existing.add(path.stem.lower())
    return existing


# ============================================================
# BACKGROUND WORKER & SEARCH ENGINE
# ============================================================

async def run_download_job(job_id: str, url: str, title: str, artist: str, album: str):
    settings = load_settings()
    fmt = settings.get("audio_format", "mp3")
    quality = settings.get("audio_quality", "320K")
    
    output_template = str(TEMP_DIR / f"{job_id}.%(ext)s")

    JOBS[job_id]["status"] = "downloading"

    command = [
        "yt-dlp", "--no-playlist", "-x",
        "--audio-format", fmt,
        "--audio-quality", quality,
        "--newline", "-o", output_template,
    ]

    if settings.get("embed_lyrics", True):
        command.extend(["--write-subs", "--sub-langs", "all", "--embed-subs"])

    command.append(url)

    try:
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        progress_regex = re.compile(r"\[download\]\s+(\d+\.\d+)%\s+of\s+\S+\s+at\s+(\S+)")

        while True:
            line = await process.stdout.readline()
            if not line: break
            line_str = line.decode("utf-8", errors="ignore").strip()
            match = progress_regex.search(line_str)
            if match:
                JOBS[job_id]["percent"] = float(match.group(1))
                JOBS[job_id]["speed"] = match.group(2)

        await process.wait()

        possible_files = list(TEMP_DIR.glob(f"{job_id}.*"))
        if not possible_files:
            JOBS[job_id]["status"] = "failed"
            JOBS[job_id]["error"] = "Download failed"
            return

        downloaded_file = possible_files[0]
        JOBS[job_id]["status"] = "tagging & normalizing"
        JOBS[job_id]["percent"] = 98.0

        if settings.get("organize_by_artist", True):
            target_dir = DOWNLOAD_DIR / clean_filename(artist) / clean_filename(album)
        else:
            target_dir = DOWNLOAD_DIR
        target_dir.mkdir(parents=True, exist_ok=True)

        final_path = target_dir / f"{clean_filename(title)}.{fmt}"

        ffmpeg_cmd = ["ffmpeg", "-y", "-i", str(downloaded_file)]
        if settings.get("normalize_audio", True):
            ffmpeg_cmd.extend(["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"])

        ffmpeg_cmd.extend([
            "-metadata", f"title={title}",
            "-metadata", f"artist={artist}",
            "-metadata", f"album={album}",
            str(final_path)
        ])

        norm_proc = await asyncio.create_subprocess_exec(*ffmpeg_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await norm_proc.wait()

        if downloaded_file.exists():
            downloaded_file.unlink()

        JOBS[job_id]["status"] = "completed"
        JOBS[job_id]["percent"] = 100.0
    except Exception as e:
        JOBS[job_id]["status"] = "failed"
        JOBS[job_id]["error"] = str(e)


async def youtube_search(query: str, max_results: int):
    is_url = query.startswith("http://") or query.startswith("https://")
    search_target = query if is_url else f"ytsearch{max_results}:{query}"

    command = [
        "yt-dlp",
        "--flat-playlist",
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
        search_target,
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            raise RuntimeError(stderr.decode("utf-8", errors="ignore")[-1000:])

        data = json.loads(stdout.decode("utf-8", errors="ignore"))
        results = []
        entries = data.get("entries", [data]) if "entries" in data else [data]
        existing_library = get_existing_filenames()

        for item in entries:
            if not item: continue
            video_id = item.get("id")
            if not video_id: continue

            raw_title = item.get("title", "Unknown Track")
            clean_title = strip_youtube_junk(raw_title)
            channel = item.get("channel") or item.get("uploader") or "Unknown Artist"
            
            artist, title = channel, clean_title
            if " - " in clean_title:
                parts = clean_title.split(" - ", 1)
                artist, title = parts[0].strip(), parts[1].strip()

            duration = item.get("duration", 0) or 0
            file_stem = clean_filename(title).lower()

            results.append({
                "id": video_id,
                "title": title,
                "artist": artist,
                "album": "Single",
                "duration": duration,
                "duration_text": format_duration(duration),
                "thumbnail": item.get("thumbnail") or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "in_library": file_stem in existing_library
            })

        return results
    except Exception as e:
        raise RuntimeError(f"Search failed: {str(e)}")


# ============================================================
# FRONTEND UI
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def home(authenticated: bool = Depends(verify_auth)):
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
    --accent-glow: rgba(99, 102, 241, 0.35);
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

header { display: flex; align-items: center; justify-content: center; gap: 14px; margin-bottom: 25px; }
header h1 {
    font-size: 2.2rem; font-weight: 700;
    background: linear-gradient(135deg, #ffffff 0%, #a5b4fc 100%);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
}

.nav-tabs {
    display: flex; justify-content: center; gap: 8px; margin-bottom: 25px;
    background: var(--card-bg); padding: 6px; border-radius: 16px;
    border: 1px solid var(--card-border); backdrop-filter: blur(12px);
}

.tab-btn {
    background: transparent; border: none; color: var(--text-secondary);
    padding: 10px 24px; border-radius: 12px; font-weight: 600; font-size: 0.9rem;
    cursor: pointer; transition: all 0.2s;
}
.tab-btn:hover { color: #fff; }
.tab-btn.active { background: var(--accent); color: #fff; box-shadow: 0 4px 12px var(--accent-glow); }

.tab-content { display: none; }
.tab-content.active { display: block; }

.search-card {
    background: var(--card-bg); backdrop-filter: blur(16px);
    border: 1px solid var(--card-border); padding: 12px; border-radius: 20px;
    display: flex; gap: 10px; box-shadow: 0 20px 40px rgba(0, 0, 0, 0.4); margin-bottom: 25px;
}

.search-card input {
    flex: 1; background: var(--input-bg); border: 1px solid var(--card-border);
    padding: 14px 18px; border-radius: 14px; color: #fff; font-size: 1rem; outline: none;
}
.search-card input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-glow); }

.search-card button {
    background: linear-gradient(135deg, var(--accent) 0%, #4338ca 100%);
    color: #fff; border: none; padding: 0 26px; border-radius: 14px;
    font-weight: 600; font-size: 0.95rem; cursor: pointer; transition: all 0.2s;
}

#progressContainer { display: flex; flex-direction: column; gap: 12px; margin-bottom: 25px; }

.progress-panel {
    background: var(--card-bg); backdrop-filter: blur(16px); border: 1px solid var(--card-border);
    border-radius: 16px; padding: 16px 20px;
}

.progress-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
.progress-title { font-size: 0.95rem; font-weight: 700; }
.progress-percent { font-size: 0.9rem; font-weight: 700; color: var(--accent); }

.progress-track { width: 100%; height: 8px; background: rgba(255, 255, 255, 0.08); border-radius: 10px; overflow: hidden; margin-bottom: 10px; }
.progress-fill { height: 100%; width: 0%; background: linear-gradient(90deg, var(--accent) 0%, #a855f7 100%); transition: width 0.3s; }

.progress-details { display: flex; justify-content: space-between; font-size: 0.8rem; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.5px; }

.results-grid { display: flex; flex-direction: column; gap: 12px; }
.result-card {
    background: var(--card-bg); backdrop-filter: blur(12px); border: 1px solid var(--card-border);
    border-radius: 16px; padding: 12px 16px; display: flex; align-items: center; gap: 16px;
}

.thumb-wrapper { position: relative; width: 110px; height: 65px; border-radius: 10px; overflow: hidden; flex-shrink: 0; background: #1e293b; }
.thumb-wrapper img { width: 100%; height: 100%; object-fit: cover; }
.badge-duration { position: absolute; bottom: 4px; right: 4px; background: rgba(0, 0, 0, 0.75); padding: 2px 5px; border-radius: 4px; font-size: 0.7rem; }

.track-info { flex: 1; min-width: 0; }
.track-title { font-size: 0.98rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.track-artist { font-size: 0.85rem; color: var(--text-secondary); }

.btn-group { display: flex; gap: 8px; align-items: center; }

.btn-preview, .btn-secondary, .btn-download, .btn-danger {
    padding: 8px 14px; border-radius: 10px; font-weight: 600; font-size: 0.82rem; cursor: pointer; border: none; transition: all 0.2s;
}
.btn-preview { background: rgba(255, 255, 255, 0.06); color: var(--text-primary); border: 1px solid var(--card-border); }
.btn-preview.playing { background: rgba(99, 102, 241, 0.3); border-color: var(--accent); color: #a5b4fc; }
.btn-download { background: linear-gradient(135deg, #10b981 0%, #059669 100%); color: #fff; }
.btn-download:disabled { opacity: 0.6; cursor: not-allowed; }
.btn-danger { background: rgba(239, 68, 68, 0.15); border: 1px solid rgba(239, 68, 68, 0.3); color: #fca5a5; }

.badge-library { background: rgba(16, 185, 129, 0.15); color: #6ee7b7; padding: 6px 12px; border-radius: 10px; font-size: 0.82rem; font-weight: 600; }

.settings-card { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 20px; padding: 25px; display: flex; flex-direction: column; gap: 20px; }
.setting-row { display: flex; justify-content: space-between; align-items: center; padding-bottom: 15px; border-bottom: 1px solid rgba(255, 255, 255, 0.05); }
.setting-label { font-weight: 600; font-size: 0.95rem; }
.setting-desc { font-size: 0.82rem; color: var(--text-secondary); margin-top: 2px; }

select, input[type="number"], input[type="text"], input[type="password"] {
    background: var(--input-bg); border: 1px solid var(--card-border); color: #fff; padding: 8px 12px; border-radius: 10px; outline: none;
}

.switch { position: relative; display: inline-block; width: 46px; height: 24px; }
.switch input { opacity: 0; width: 0; height: 0; }
.slider { position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background-color: #374151; transition: .3s; border-radius: 24px; }
.slider:before { position: absolute; content: ""; height: 18px; width: 18px; left: 3px; bottom: 3px; background-color: white; transition: .3s; border-radius: 50%; }
input:checked + .slider { background-color: var(--accent); }
input:checked + .slider:before { transform: translateX(22px); }

.status-msg { text-align: center; color: var(--text-secondary); margin: 15px 0; font-size: 0.9rem; }
.bulk-bar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; padding: 10px 16px; background: rgba(255,255,255,0.03); border-radius: 12px; }
</style>
</head>
<body>
<div class="container">
    <header>
        <h1>Navidrome Downloader Pro</h1>
    </header>

    <div class="nav-tabs">
        <button class="tab-btn active" onclick="switchTab('search')">🔍 Search / Link</button>
        <button class="tab-btn" onclick="switchTab('library')">📂 Library (<span id="libCount">0</span>)</button>
        <button class="tab-btn" onclick="switchTab('settings')">⚙️ Settings</button>
    </div>

    <!-- TAB 1: SEARCH -->
    <div id="tab-search" class="tab-content active">
        <div class="search-card">
            <input id="query" placeholder="Search track, or paste YouTube video / playlist link..." autocomplete="off" />
            <button id="searchBtn" type="button" onclick="searchMusic()">Search</button>
        </div>

        <div id="progressContainer"></div>

        <div id="statusMsg" class="status-msg"></div>
        <div id="results" class="results-grid"></div>
    </div>

    <!-- TAB 2: LIBRARY -->
    <div id="tab-library" class="tab-content">
        <div class="bulk-bar" id="bulkBar" style="display:none;">
            <span><input type="checkbox" id="selectAll" onchange="toggleSelectAll(this)"> Select All</span>
            <button class="btn-danger" onclick="deleteSelected()">🗑 Delete Selected</button>
        </div>
        <div id="libraryList" class="results-grid"></div>
    </div>

    <!-- TAB 3: SETTINGS -->
    <div id="tab-settings" class="tab-content">
        <div class="settings-card">
            <div class="setting-row">
                <div>
                    <div class="setting-label">Audio Format & Quality</div>
                    <div class="setting-desc">Preferred output audio quality</div>
                </div>
                <div style="display:flex; gap:8px;">
                    <select id="set_format"><option value="mp3">MP3</option><option value="flac">FLAC</option><option value="m4a">M4A</option></select>
                    <select id="set_quality"><option value="320K">320 Kbps</option><option value="256K">256 Kbps</option><option value="128K">128 Kbps</option></select>
                </div>
            </div>

            <div class="setting-row">
                <div><div class="setting-label">Folder Hierarchy</div><div class="setting-desc">Save files as Music/Artist/Album/Track</div></div>
                <label class="switch"><input type="checkbox" id="set_folder"><span class="slider"></span></label>
            </div>

            <div class="setting-row">
                <div><div class="setting-label">Volume Normalization</div><div class="setting-desc">Normalize playback audio levels via FFmpeg</div></div>
                <label class="switch"><input type="checkbox" id="set_norm"><span class="slider"></span></label>
            </div>

            <div class="setting-row">
                <div><div class="setting-label">Embed Lyrics & Artwork</div><div class="setting-desc">Fetch and embed timed lyrics & cover art</div></div>
                <label class="switch"><input type="checkbox" id="set_lyrics"><span class="slider"></span></label>
            </div>

            <div class="setting-row">
                <div><div class="setting-label">Web UI Security</div><div class="setting-desc">Require HTTP Basic Auth credentials</div></div>
                <label class="switch"><input type="checkbox" id="set_auth"><span class="slider"></span></label>
            </div>

            <div class="setting-row">
                <div><div class="setting-label">Admin Username & Password</div></div>
                <div style="display:flex; gap:8px;">
                    <input type="text" id="set_user" placeholder="User" style="width:100px;">
                    <input type="password" id="set_pass" placeholder="Pass" style="width:100px;">
                </div>
            </div>

            <button class="btn-download" onclick="saveSettings()" style="padding: 12px; margin-top: 10px;">Save Settings</button>
            <div id="settingsMsg" class="status-msg"></div>
        </div>
    </div>
</div>

<script>
let activeAudio = null;
let activePreviewBtn = null;

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
        document.getElementById('set_folder').checked = s.organize_by_artist;
        document.getElementById('set_norm').checked = s.normalize_audio;
        document.getElementById('set_lyrics').checked = s.embed_lyrics;
        document.getElementById('set_auth').checked = s.auth_enabled;
        document.getElementById('set_user').value = s.auth_user || 'admin';
        document.getElementById('set_pass').value = s.auth_pass || 'admin123';
    } catch(e) {}
}

async function saveSettings() {
    const btn = event.target;
    btn.textContent = "⏳ Saving...";
    btn.disabled = true;

    const data = {
        audio_format: document.getElementById('set_format').value,
        audio_quality: document.getElementById('set_quality').value,
        organize_by_artist: document.getElementById('set_folder').checked,
        normalize_audio: document.getElementById('set_norm').checked,
        embed_lyrics: document.getElementById('set_lyrics').checked,
        auth_enabled: document.getElementById('set_auth').checked,
        auth_user: document.getElementById('set_user').value,
        auth_pass: document.getElementById('set_pass').value
    };
    await fetch('api/settings', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data) });
    
    btn.textContent = "Save Settings";
    btn.disabled = false;
    document.getElementById('settingsMsg').textContent = "✅ Settings saved!";
    setTimeout(() => document.getElementById('settingsMsg').textContent = "", 3000);
}

async function searchMusic() {
    const q = document.getElementById("query").value.trim();
    if (!q) return;
    document.getElementById("statusMsg").textContent = "🔍 Fetching YouTube metadata...";
    document.getElementById("results").innerHTML = "";

    try {
        const res = await fetch("api/search?q=" + encodeURIComponent(q));
        const data = await res.json();
        document.getElementById("statusMsg").textContent = "";

        data.forEach(item => {
            const card = document.createElement("div");
            card.className = "result-card";
            card.innerHTML = `
                <div class="thumb-wrapper">
                    <img src="${item.thumbnail}" />
                    <span class="badge-duration">${item.duration_text}</span>
                </div>
                <div class="track-info">
                    <div class="track-title">${escapeHtml(item.title)}</div>
                    <div class="track-artist">👤 ${escapeHtml(item.artist)}</div>
                </div>
                <div class="btn-group">
                    <button class="btn-preview" onclick="togglePreview(this, '${escapeJs(item.url)}')">▶ Preview</button>
                    ${item.in_library 
                        ? `<span class="badge-library">✓ In Library</span>`
                        : `<button class="btn-download" onclick='directDownload(this, ${JSON.stringify(item)})'>⬇️ Save</button>`
                    }
                </div>
            `;
            document.getElementById("results").appendChild(card);
        });
    } catch (err) {
        document.getElementById("statusMsg").textContent = "❌ Search failed.";
    }
}

async function directDownload(btn, item) {
    btn.disabled = true;
    btn.textContent = "⏳ Starting...";
    
    await fetch('api/download', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(item)
    });
    
    btn.textContent = "⏳ Queued";
    pollJobs();
}

async function pollJobs() {
    try {
        const res = await fetch('api/jobs');
        const jobs = await res.json();
        const container = document.getElementById('progressContainer');
        const activeJobs = jobs.filter(j => j.status !== 'completed' && j.status !== 'failed');
        
        if (!activeJobs.length) {
            container.innerHTML = '';
            return;
        }

        container.innerHTML = activeJobs.map(job => `
            <div class="progress-panel">
                <div class="progress-header">
                    <span class="progress-title">${escapeHtml(job.title)}</span>
                    <span class="progress-percent">${job.percent.toFixed(0)}%</span>
                </div>
                <div class="progress-track"><div class="progress-fill" style="width: ${job.percent}%"></div></div>
                <div class="progress-details">
                    <span>${job.status}</span>
                    <span>${job.speed}</span>
                </div>
            </div>
        `).join('');
    } catch(e) {}
}

function togglePreview(btn, url) {
    if (activeAudio) { activeAudio.pause(); activeAudio = null; }
    if (activePreviewBtn) { activePreviewBtn.classList.remove('playing'); activePreviewBtn.textContent = '▶ Preview'; }
    if (activePreviewBtn === btn) { activePreviewBtn = null; return; }

    activePreviewBtn = btn;
    btn.classList.add('playing');
    btn.textContent = '⏳ Loading...';
    
    activeAudio = new Audio("api/preview?url=" + encodeURIComponent(url));
    activeAudio.play().then(() => { btn.textContent = '⏸ Playing'; }).catch(() => { btn.textContent = '▶ Preview'; });
}

async function loadLibrary() {
    const list = document.getElementById('libraryList');
    list.innerHTML = "Loading...";
    const res = await fetch('api/library');
    const files = await res.json();
    document.getElementById('libCount').textContent = files.length;
    
    if (files.length === 0) { list.innerHTML = "No files downloaded."; return; }
    document.getElementById('bulkBar').style.display = files.length ? 'flex' : 'none';
    
    list.innerHTML = "";
    files.forEach(f => {
        const card = document.createElement('div');
        card.className = 'result-card';
        card.innerHTML = `
            <input type="checkbox" class="file-select" value="${escapeHtml(f.name)}" />
            <div class="track-info">
                <div class="track-title">🎵 ${escapeHtml(f.name)}</div>
                <div class="track-artist">📦 ${f.size}</div>
            </div>
            <button class="btn-danger" onclick="deleteSingle('${escapeJs(f.name)}')">🗑 Delete</button>
        `;
        list.appendChild(card);
    });
}

function toggleSelectAll(master) {
    document.querySelectorAll('.file-select').forEach(cb => cb.checked = master.checked);
}

async function deleteSelected() {
    const selected = Array.from(document.querySelectorAll('.file-select:checked')).map(cb => cb.value);
    if (!selected.length || !confirm(`Delete ${selected.length} items?`)) return;
    await fetch('api/library/batch-delete', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(selected) });
    loadLibrary();
}

async function deleteSingle(name) {
    if (confirm("Delete " + name + "?")) {
        await fetch('api/library/' + encodeURIComponent(name), { method: 'DELETE' });
        loadLibrary();
    }
}

function escapeHtml(text) { return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
function escapeJs(text) { return text.replace(/'/g, "\\'").replace(/"/g, '\\"'); }
document.getElementById("query").addEventListener("keydown", e => { if (e.key === "Enter") searchMusic(); });

setInterval(pollJobs, 1500);
pollJobs();
</script>
</body>
</html>
"""


# ============================================================
# API ENDPOINTS
# ============================================================

@app.get("/api/settings")
async def get_settings(auth: bool = Depends(verify_auth)):
    return load_settings()


@app.post("/api/settings")
async def update_settings(data: dict = Body(...), auth: bool = Depends(verify_auth)):
    return save_settings(data)


@app.get("/api/jobs")
async def get_jobs(auth: bool = Depends(verify_auth)):
    return list(JOBS.values())


@app.post("/api/download")
async def start_download(payload: dict = Body(...), auth: bool = Depends(verify_auth)):
    job_id = uuid.uuid4().hex
    url = payload.get("url")
    title = payload.get("title", "Unknown Title")
    artist = payload.get("artist", "Unknown Artist")
    album = payload.get("album", "Single")

    JOBS[job_id] = {
        "id": job_id,
        "title": title,
        "artist": artist,
        "album": album,
        "status": "queued",
        "percent": 0.0,
        "speed": "-- MB/s",
        "error": None
    }

    asyncio.create_task(run_download_job(job_id, url, title, artist, album))
    return {"job_id": job_id, "status": "started"}


@app.get("/api/library")
async def get_library(auth: bool = Depends(verify_auth)):
    files = []
    for path in DOWNLOAD_DIR.rglob("*"):
        if path.is_file() and not any(part.startswith(".") for part in path.relative_to(DOWNLOAD_DIR).parts):
            if path.suffix.lower() in AUDIO_EXTENSIONS:
                files.append({
                    "name": str(path.relative_to(DOWNLOAD_DIR)),
                    "size": format_size(path.stat().st_size)
                })
    return sorted(files, key=lambda x: x["name"])


@app.delete("/api/library/{filename:path}")
async def delete_library_file(filename: str, auth: bool = Depends(verify_auth)):
    file_path = DOWNLOAD_DIR / filename
    if file_path.exists() and file_path.is_file():
        file_path.unlink()
        return {"status": "deleted"}
    raise HTTPException(status_code=404, detail="File not found")


@app.post("/api/library/batch-delete")
async def batch_delete_files(files: list[str] = Body(...), auth: bool = Depends(verify_auth)):
    deleted_count = 0
    for filename in files:
        file_path = DOWNLOAD_DIR / filename
        if file_path.exists() and file_path.is_file():
            file_path.unlink()
            deleted_count += 1
    return {"status": "success", "deleted": deleted_count}


@app.get("/api/search")
async def search(q: str = Query(..., min_length=1), auth: bool = Depends(verify_auth)):
    settings = load_settings()
    try:
        return await youtube_search(q, settings.get("max_results", 20))
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))


@app.get("/api/preview")
async def preview_audio(url: str = Query(...), auth: bool = Depends(verify_auth)):
    try:
        process = await asyncio.create_subprocess_exec(
            "yt-dlp", "-g", "-f", "ba/b", url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await process.communicate()
        stream_url = stdout.decode("utf-8", errors="ignore").strip().split("\n")[0]
        if stream_url:
            return RedirectResponse(url=stream_url)
        raise HTTPException(status_code=400, detail="Preview URL failed")
    except Exception as err:
        raise HTTPException(status_code=500, detail=str(err))
