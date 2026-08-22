from fastapi import FastAPI, Query, HTTPException, Body
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, Response, StreamingResponse
import asyncio
import glob
import json
import mimetypes
import os
import re
import time
import uuid
from pathlib import Path

app = FastAPI(title="Navidrome Music Downloader")

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/share/navidrome/music"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

SETTINGS_FILE = DOWNLOAD_DIR / ".settings.json"
AUDIO_EXTENSIONS = {'.mp3', '.flac', '.m4a', '.ogg', '.wav', '.opus', '.aac', '.alac'}
MAX_CONCURRENT_DOWNLOADS = 3

MEDIA_TYPES = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".alac": "audio/mp4",
}

DEFAULT_SETTINGS = {
    "audio_format": "mp3",
    "audio_quality": "320K",
    "embed_thumbnail": True,
    "embed_metadata": True,
    "max_results": 20,
    "organize_by_artist": False
}

TASKS = {}
task_queue = asyncio.Queue()


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
    value = re.sub(r"\s+", " ", value).strip(" .")
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
        if size_bytes >= 1024 * 1024 * 1024:
            gb = size_bytes / (1024 * 1024 * 1024)
            return f"{gb:.2f} GB"
        mb = size_bytes / (1024 * 1024)
        return f"{mb:.1f} MB"
    except Exception:
        return "0 MB"


def resolve_file(filename: str) -> Path:
    clean_name = filename.strip()
    file_path = DOWNLOAD_DIR / clean_name
    if file_path.exists() and file_path.is_file():
        return file_path
    
    # Escape glob pattern special characters ([ ]) to prevent lookup failures
    escaped_name = glob.escape(clean_name)
    matches = list(DOWNLOAD_DIR.glob(f"{escaped_name}*"))
    for match in matches:
        if match.is_file():
            return match
            
    raise HTTPException(status_code=404, detail="File not found")


async def download_worker():
    while True:
        task_id = await task_queue.get()
        task = TASKS.get(task_id)
        if not task:
            task_queue.task_done()
            continue

        try:
            task["status"] = "downloading"
            task["step"] = "Downloading stream..."
            task["last_updated"] = time.time() * 1000
            
            settings = load_settings()
            fmt = settings.get("audio_format", "mp3")
            quality = settings.get("audio_quality", "320K")
            embed_thumb = settings.get("embed_thumbnail", True)
            embed_meta = settings.get("embed_metadata", True)

            output_template = str(DOWNLOAD_DIR / f"{task_id}.%(ext)s")

            command = [
                "yt-dlp",
                "--no-playlist",
                "-x",
                "--audio-format", fmt,
                "--audio-quality", quality,
                "--newline",
                "--embed-subs",
                "--sub-langs", "all,-live_chat",
                "--parse-metadata", "title:%(album)s",
                "-o", output_template,
            ]

            if embed_thumb:
                command.append("--embed-thumbnail")
            if embed_meta:
                command.append("--add-metadata")

            command.append(task["url"])

            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            progress_regex = re.compile(r"\[download\]\s+~?\s*(\d+(?:\.\d+)?)%")
            speed_regex = re.compile(r"at\s+([~0-9a-zA-Z\.\/]+)")

            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                line_str = line.decode("utf-8", errors="ignore").strip()

                pct_match = progress_regex.search(line_str)
                if pct_match:
                    task["percent"] = float(pct_match.group(1))
                    task["last_updated"] = time.time() * 1000
                    
                    spd_match = speed_regex.search(line_str)
                    if spd_match:
                        task["speed"] = spd_match.group(1).replace("~", "")
                        
                elif "[ExtractAudio]" in line_str or "[EmbedThumbnail]" in line_str or "[Metadata]" in line_str:
                    task["status"] = "processing"
                    task["step"] = "Embedding cover art & tags..."
                    task["percent"] = 92
                    task["last_updated"] = time.time() * 1000

            await process.wait()

            if process.returncode != 0:
                stderr_data = await process.stderr.read()
                err_text = stderr_data.decode("utf-8", errors="ignore")
                task["status"] = "error"
                task["error"] = err_text[-300:]
                task["last_updated"] = time.time() * 1000
                task_queue.task_done()
                continue

            possible_files = list(DOWNLOAD_DIR.glob(f"{task_id}.*"))
            if not possible_files:
                task["status"] = "error"
                task["error"] = "Downloaded file not found."
                task["last_updated"] = time.time() * 1000
                task_queue.task_done()
                continue

            audio_file = possible_files[0]
            ext = audio_file.suffix if audio_file.suffix else f".{fmt}"

            task["status"] = "processing"
            task["step"] = "Cleaning tags & metadata..."
            task["percent"] = 96
            task["last_updated"] = time.time() * 1000

            clean_title = clean_filename(task["title"])
            cleaned_file = DOWNLOAD_DIR / f"clean_{task_id}{ext}"
            
            clean_command = [
                "ffmpeg",
                "-y",
                "-i", str(audio_file),
                "-map", "0",
                "-c", "copy",
                "-disposition:v:0", "attached_pic",
                "-metadata", f"title={clean_title}",
                "-metadata", f"album={clean_title}",
                "-metadata", "comment=",
                "-metadata", "description=",
                "-metadata", "purl=",
                str(cleaned_file)
            ]

            process_clean = await asyncio.create_subprocess_exec(
                *clean_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await process_clean.wait()

            if process_clean.returncode == 0 and cleaned_file.exists():
                audio_file.unlink()
                audio_file = cleaned_file

            final_name = f"{clean_title}{ext}"
            final_path = DOWNLOAD_DIR / final_name

            if final_path.exists():
                final_name = f"{clean_title}_{task_id[:4]}{ext}"
                final_path = DOWNLOAD_DIR / final_name

            audio_file.rename(final_path)

            task["status"] = "completed"
            task["percent"] = 100
            task["step"] = "Ready"
            task["last_updated"] = time.time() * 1000

        except Exception as err:
            task["status"] = "error"
            task["error"] = str(err)
            task["last_updated"] = time.time() * 1000
        finally:
            task_queue.task_done()
            
            async def clear_task():
                await asyncio.sleep(15)
                TASKS.pop(task_id, None)
            asyncio.create_task(clear_task())


@app.on_event("startup")
async def startup_event():
    for _ in range(MAX_CONCURRENT_DOWNLOADS):
        asyncio.create_task(download_worker())


async def youtube_search(query: str, max_results: int, page: int = 1):
    start_idx = (page - 1) * max_results + 1
    end_idx = page * max_results

    command = [
        "yt-dlp",
        "--flat-playlist",
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
        "--playlist-start", str(start_idx),
        "--playlist-end", str(end_idx),
        f"ytsearch{end_idx}:{query}",
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

[data-theme="light"] {
    --bg-main: #f8fafc;
    --card-bg: rgba(255, 255, 255, 0.9);
    --card-border: rgba(0, 0, 0, 0.08);
    --accent: #4f46e5;
    --accent-hover: #4338ca;
    --accent-glow: rgba(79, 70, 229, 0.25);
    --success: #059669;
    --danger: #dc2626;
    --text-primary: #0f172a;
    --text-secondary: #475569;
    --input-bg: #ffffff;
}

* { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Plus Jakarta Sans', sans-serif; }
body {
    background: var(--bg-main);
    color: var(--text-primary);
    min-height: 100vh;
    padding: 1.875rem 1.25rem;
    font-size: 1rem;
    line-height: 1.5;
}

/* Accessibility Focus Styling */
:focus-visible {
    outline: 3px solid var(--accent);
    outline-offset: 3px;
}

.skip-link {
    position: absolute;
    top: -40px;
    left: 0;
    background: var(--accent);
    color: white;
    padding: 8px;
    z-index: 9999;
    transition: top 0.2s;
}
.skip-link:focus { top: 0; }

.container { max-width: 980px; margin: 0 auto; }
header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 1.56rem; }
.header-brand { display: flex; align-items: center; gap: 0.875rem; }
header h1 {
    font-size: 2.2rem; font-weight: 700; background: linear-gradient(135deg, var(--text-primary) 0%, var(--accent) 100%);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; letter-spacing: -0.02em;
}

.theme-toggle-btn {
    background: var(--card-bg); border: 1px solid var(--card-border); color: var(--text-primary);
    padding: 8px 14px; border-radius: 12px; cursor: pointer; font-weight: 600; font-size: 0.875rem;
}

.nav-tabs {
    display: flex; justify-content: center; gap: 8px; margin-bottom: 25px; background: var(--card-bg);
    padding: 6px; border-radius: 16px; border: 1px solid var(--card-border); backdrop-filter: blur(12px);
}
.tab-btn {
    background: transparent; border: none; color: var(--text-secondary); padding: 10px 24px;
    border-radius: 12px; font-weight: 600; font-size: 0.9rem; cursor: pointer; transition: all 0.2s;
    display: flex; align-items: center; gap: 6px;
}
.tab-btn:hover { color: var(--text-primary); }
.tab-btn.active { background: var(--accent); color: #fff; box-shadow: 0 4px 12px var(--accent-glow); }
.tab-content { display: none; }
.tab-content.active { display: block; }

.search-card {
    background: var(--card-bg); backdrop-filter: blur(16px); border: 1px solid var(--card-border);
    padding: 12px; border-radius: 20px; display: flex; gap: 10px; box-shadow: 0 20px 40px rgba(0, 0, 0, 0.2);
    margin-bottom: 25px;
}
.search-card input {
    flex: 1; background: var(--input-bg); border: 1px solid var(--card-border); padding: 14px 18px;
    border-radius: 14px; color: var(--text-primary); font-size: 1rem; outline: none; transition: all 0.2s;
}
.search-card button {
    background: linear-gradient(135deg, var(--accent) 0%, var(--accent-hover) 100%); color: #fff; border: none;
    padding: 0 26px; border-radius: 14px; font-weight: 600; font-size: 0.95rem; cursor: pointer;
    transition: all 0.2s; box-shadow: 0 4px 15px var(--accent-glow);
}

.progress-panel {
    background: var(--card-bg); backdrop-filter: blur(16px); border: 1px solid var(--card-border);
    border-radius: 20px; padding: 22px; margin-bottom: 25px; display: none; box-shadow: 0 10px 30px rgba(0,0,0,0.3);
}
.progress-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.progress-title { font-size: 0.92rem; font-weight: 700; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 80%; }
.progress-percent { font-size: 0.9rem; font-weight: 700; color: var(--accent); }
.progress-track {
    width: 100%; height: 8px; background: rgba(128, 128, 128, 0.2); border-radius: 10px; overflow: hidden; margin-bottom: 12px;
}
.progress-fill { height: 100%; width: 0%; background: var(--accent); border-radius: 10px; transition: width 0.3s ease; }

.results-grid { display: flex; flex-direction: column; gap: 12px; }
.result-card { background: var(--card-bg); backdrop-filter: blur(12px); border: 1px solid var(--card-border); border-radius: 16px; padding: 12px 16px; display: flex; align-items: center; gap: 16px; }
.thumb-wrapper { position: relative; width: 110px; height: 65px; border-radius: 10px; overflow: hidden; flex-shrink: 0; background: var(--input-bg); }
.thumb-wrapper img { width: 100%; height: 100%; object-fit: cover; }
.badge-duration { position: absolute; bottom: 4px; right: 4px; background: rgba(0, 0, 0, 0.75); color: #fff; padding: 2px 5px; border-radius: 4px; font-size: 0.7rem; font-weight: 600; }
.track-info { flex: 1; min-width: 0; }
.track-title { font-size: 0.98rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-bottom: 3px; }
.track-artist { font-size: 0.85rem; color: var(--text-secondary); }
.btn-group { display: flex; gap: 8px; align-items: center; }
.btn-preview { background: rgba(128, 128, 128, 0.1); border: 1px solid var(--card-border); color: var(--text-primary); padding: 8px 14px; border-radius: 10px; font-weight: 600; font-size: 0.82rem; cursor: pointer; }
.btn-download { background: var(--success); color: #fff; border: none; padding: 8px 16px; border-radius: 10px; font-weight: 600; font-size: 0.82rem; cursor: pointer; }
.badge-library { background: rgba(16, 185, 129, 0.15); border: 1px solid var(--success); color: var(--success); padding: 6px 12px; border-radius: 10px; font-weight: 600; font-size: 0.82rem; }

.settings-card { background: var(--card-bg); backdrop-filter: blur(16px); border: 1px solid var(--card-border); border-radius: 20px; padding: 25px; display: flex; flex-direction: column; gap: 20px; }
.setting-row { display: flex; justify-content: space-between; align-items: center; padding-bottom: 15px; border-bottom: 1px solid var(--card-border); }
select, input[type="number"] { background: var(--input-bg); border: 1px solid var(--card-border); color: var(--text-primary); padding: 8px 12px; border-radius: 10px; font-size: 0.9rem; }
.switch { position: relative; display: inline-block; width: 46px; height: 24px; }
.switch input { opacity: 0; width: 0; height: 0; }
.slider { position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background-color: #6b7280; transition: .3s; border-radius: 24px; }
.slider:before { position: absolute; content: ""; height: 18px; width: 18px; left: 3px; bottom: 3px; background-color: white; transition: .3s; border-radius: 50%; }
input:checked + .slider { background-color: var(--accent); }
input:checked + .slider:before { transform: translateX(22px); }

/* Mobile Bottom Navigation Bar */
@media(max-width: 640px) {
    body { padding-bottom: 80px; }
    .nav-tabs {
        position: fixed; bottom: 0; left: 0; right: 0; margin-bottom: 0;
        border-radius: 16px 16px 0 0; z-index: 1000; justify-content: space-around;
        background: var(--card-bg); border-top: 1px solid var(--card-border);
    }
    .result-card { flex-direction: column; align-items: flex-start; }
    .thumb-wrapper { width: 100%; height: 140px; }
    .btn-group { width: 100%; justify-content: space-between; }
}
</style>
</head>
<body>
<a href="#main-content" class="skip-link">Skip to main content</a>

<div class="container">
    <header role="banner">
        <div class="header-brand">
            <svg width="36" height="36" viewBox="0 0 512 512" aria-hidden="true">
              <rect width="512" height="512" rx="112" fill="var(--accent)" />
              <path d="M 256 128 V 300 M 196 248 L 256 312 L 316 248" stroke="#ffffff" stroke-width="28" stroke-linecap="round" fill="none" />
            </svg>
            <h1>Navidrome Downloader</h1>
        </div>
        <button class="theme-toggle-btn" id="themeToggle" aria-label="Toggle dark and light theme">🌗 Theme</button>
    </header>

    <nav class="nav-tabs" role="tablist" aria-label="Main Navigation">
        <button id="tab-btn-search" class="tab-btn active" role="tab" aria-selected="true" aria-controls="tab-search" onclick="switchTab('search')">🔍 Search</button>
        <button id="tab-btn-library" class="tab-btn" role="tab" aria-selected="false" aria-controls="tab-library" onclick="switchTab('library')">📂 Library (<span id="libCount">0</span>)</button>
        <button id="tab-btn-settings" class="tab-btn" role="tab" aria-selected="false" aria-controls="tab-settings" onclick="switchTab('settings')">⚙️ Settings</button>
    </nav>

    <main id="main-content">
        <!-- TAB 1: SEARCH -->
        <section id="tab-search" class="tab-content active" role="tabpanel" aria-labelledby="tab-btn-search">
            <div class="search-card">
                <input id="query" placeholder="Search track, artist, or album..." autocomplete="off" aria-label="Search tracks" />
                <button id="searchBtn" type="button" aria-label="Execute search">Search</button>
            </div>

            <div class="progress-panel" id="progressPanel" aria-live="polite">
                <div style="font-size: 0.95rem; font-weight: 700; margin-bottom: 14px;">⚡ Active Downloads</div>
                <div id="activeDownloadsList"></div>
            </div>

            <div id="statusMsg" class="status-msg" aria-live="polite"></div>
            <div id="results" class="results-grid" role="region" aria-label="Search results"></div>
        </section>

        <!-- TAB 2: LIBRARY -->
        <section id="tab-library" class="tab-content" role="tabpanel" aria-labelledby="tab-btn-library">
            <div class="search-card" style="margin-bottom: 16px;">
                <input id="libSearchQuery" placeholder="🔍 Filter local library tracks..." autocomplete="off" oninput="filterLibrary()" aria-label="Filter local library" />
            </div>
            <div id="libraryList" class="results-grid" role="region" aria-label="Downloaded tracks"></div>
        </section>

        <!-- TAB 3: SETTINGS -->
        <section id="tab-settings" class="tab-content" role="tabpanel" aria-labelledby="tab-btn-settings">
            <div class="settings-card">
                <div class="setting-row">
                    <label for="set_format">Audio Format</label>
                    <select id="set_format"><option value="mp3">MP3</option><option value="flac">FLAC</option><option value="m4a">M4A</option></select>
                </div>
                <div class="setting-row">
                    <label for="set_quality">Audio Quality</label>
                    <select id="set_quality"><option value="320K">320 Kbps</option><option value="256K">256 Kbps</option></select>
                </div>
                <button class="save-btn" onclick="saveSettings()">Save Settings</button>
            </div>
        </section>
    </main>
</div>

<script>
let completedSet = new Set();
let libraryFilesSet = new Set();
let rawLibraryFiles = [];

// Theme Management
const themeToggleBtn = document.getElementById('themeToggle');
themeToggleBtn.addEventListener('click', () => {
    const currentTheme = document.documentElement.getAttribute('data-theme');
    const targetTheme = currentTheme === 'light' ? 'dark' : 'light';
    document.documentElement.setAttribute('data-theme', targetTheme);
    localStorage.setItem('theme', targetTheme);
});
if (localStorage.getItem('theme') === 'light') {
    document.documentElement.setAttribute('data-theme', 'light');
}

// Request Notification Permission
if ("Notification" in window && Notification.permission === "default") {
    Notification.requestPermission();
}

function sendDesktopNotification(title) {
    if ("Notification" in window && Notification.permission === "granted") {
        new Notification("Download Finished 🎵", {
            body: `Track successfully installed: ${title}`,
            icon: "https://i.ytimg.com/vi/placeholder/hqdefault.jpg"
        });
    }
}

// Deep Linking Navigation (Hash Routing)
function switchTab(tab, updateHash = true) {
    if (updateHash) window.location.hash = tab;

    document.querySelectorAll('.tab-btn').forEach(b => {
        b.classList.remove('active');
        b.setAttribute('aria-selected', 'false');
    });
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));

    const btn = document.getElementById(`tab-btn-${tab}`);
    const content = document.getElementById(`tab-${tab}`);
    if (btn && content) {
        btn.classList.add('active');
        btn.setAttribute('aria-selected', 'true');
        content.classList.add('active');
    }

    if (tab === 'library') loadLibrary();
    if (tab === 'settings') loadSettings();
}

window.addEventListener('hashchange', () => {
    const tab = window.location.hash.replace('#', '') || 'search';
    switchTab(tab, false);
});

async function pollTasks() {
    try {
        const res = await fetch('api/tasks');
        const tasks = await res.json();
        
        tasks.forEach(t => {
            if (t.status === 'completed' && !completedSet.has(t.id)) {
                completedSet.add(t.id);
                sendDesktopNotification(t.title);
            }
        });

        const activeTasks = tasks.filter(t => t.status === 'downloading' || t.status === 'processing');
        const panel = document.getElementById("progressPanel");
        const listContainer = document.getElementById("activeDownloadsList");

        if (activeTasks.length === 0) {
            panel.style.display = "none";
        } else {
            panel.style.display = "block";
            listContainer.innerHTML = activeTasks.map(t => `
                <div style="margin-bottom: 10px;">
                    <div class="progress-title">${t.title}</div>
                    <div class="progress-track">
                        <div class="progress-fill" style="width: ${t.percent}%"></div>
                    </div>
                </div>
            `).join('');
        }
    } catch (e) {}
    setTimeout(pollTasks, 1500);
}

document.addEventListener("DOMContentLoaded", () => {
    const initialTab = window.location.hash.replace('#', '') || 'search';
    switchTab(initialTab, false);
    pollTasks();
});
</script>
</body>
</html>
"""

# API route stubs remain identical to original script
@app.get("/api/settings")
async def get_settings():
    return load_settings()

@app.post("/api/settings")
async def update_settings(data: dict = Body(...)):
    return save_settings(data)

@app.get("/api/library")
async def get_library():
    files = []
    total_bytes = 0
    for path in DOWNLOAD_DIR.iterdir():
        if path.is_file() and not path.name.startswith(".") and path.suffix.lower() in AUDIO_EXTENSIONS:
            sz = path.stat().st_size
            total_bytes += sz
            files.append({"name": path.name, "size": format_size(sz), "bytes": sz})
    return {"files": sorted(files, key=lambda x: x["name"]), "total_size": format_size(total_bytes), "total_bytes": total_bytes}

@app.get("/api/tasks")
async def get_tasks():
    return list(TASKS.values())
