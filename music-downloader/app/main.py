from fastapi import FastAPI, Query, HTTPException, Body
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, Response, StreamingResponse
import asyncio
import json
import mimetypes
import os
import re
import time
import uuid
from pathlib import Path

app = FastAPI(title="Xrob Music")

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
    "organize_by_artist": False,
    "poll_interval": 1500
}

TASKS = {}
task_queue = asyncio.Queue()
ACTIVE_PROCESSES = {}


def normalize_duplicate_key(value: str) -> str:
    value = Path(value or "").stem.lower()
    value = re.sub(r"\b(official\s*(video|audio|music video)|lyrics?|hd|4k|remaster(ed)?|audio)\b", " ", value, flags=re.I)
    value = re.sub(r"[^a-z0-9]+", "", value)
    return value


def get_all_audio_files():
    return [p for p in DOWNLOAD_DIR.rglob("*")
            if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in AUDIO_EXTENSIONS]


def is_duplicate(title: str) -> bool:
    key = normalize_duplicate_key(title)
    return any(normalize_duplicate_key(p.name) == key for p in get_all_audio_files())


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
    
    matches = list(DOWNLOAD_DIR.rglob(f"{Path(clean_name).name}*"))
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
            ACTIVE_PROCESSES[task_id] = process

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
            ACTIVE_PROCESSES.pop(task_id, None)

            if task.get("cancel_requested"):
                task["status"] = "cancelled"
                task["step"] = "Cancelled"
                task["last_updated"] = time.time() * 1000
                continue

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

            if settings.get("organize_by_artist", False):
                final_dir = DOWNLOAD_DIR / artist
                final_dir.mkdir(parents=True, exist_ok=True)
            else:
                final_dir = DOWNLOAD_DIR

            final_name = f"{clean_title}{ext}"
            final_path = final_dir / final_name

            if final_path.exists() or is_duplicate(clean_title):
                final_name = f"{clean_title}_{task_id[:4]}{ext}"
                final_path = final_dir / final_name

            audio_file.rename(final_path)
            task["final_name"] = str(final_path.relative_to(DOWNLOAD_DIR))

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
            
            # Keep task history visible for the Downloads page. The frontend can clear it explicitly.


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
        "--match-filter", "duration > 0",
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
<title>Xrob Music</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {
    --bg-main: #0b0f19;
    --card-bg: rgba(22, 30, 46, 0.8);
    --card-border: rgba(255, 255, 255, 0.12);
    --accent: #6366f1;
    --accent-hover: #4f46e5;
    --accent-glow: rgba(99, 102, 241, 0.35);
    --success: #10b981;
    --danger: #ef4444;
    --text-primary: #f3f4f6;
    --text-secondary: #9ca3af;
    --input-bg: rgba(15, 23, 42, 0.85);
    --focus-ring: #a5b4fc;
}

[data-theme="light"] {
    --bg-main: #f1f5f9;
    --card-bg: rgba(255, 255, 255, 0.85);
    --card-border: rgba(0, 0, 0, 0.1);
    --accent: #4f46e5;
    --accent-hover: #4338ca;
    --accent-glow: rgba(79, 70, 229, 0.2);
    --success: #059669;
    --danger: #dc2626;
    --text-primary: #0f172a;
    --text-secondary: #475569;
    --input-bg: #ffffff;
    --focus-ring: #4338ca;
}

* { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Plus Jakarta Sans', sans-serif; }

*:focus-visible {
    outline: 3px solid var(--focus-ring);
    outline-offset: 2px;
}

body {
    background: var(--bg-main);
    color: var(--text-primary);
    min-height: 100vh;
    display: flex;
    font-size: 1rem;
    transition: background-color 0.3s, color 0.3s;
}

.sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
}

/* App Shell Layout */
.app-shell { display: flex; width: 100%; min-height: 100vh; }

/* Side Navigation Drawer (Desktop) */
.side-nav {
    width: 260px;
    background: var(--card-bg);
    border-right: 1px solid var(--card-border);
    padding: 24px 16px;
    display: flex;
    flex-direction: column;
    gap: 20px;
    backdrop-filter: blur(12px);
}

.side-brand {
    display: flex;
    align-items: center;
    gap: 12px;
}

.side-brand h1 {
    font-size: 1.25rem;
    font-weight: 700;
    color: var(--text-primary);
}

.nav-list { display: flex; flex-direction: column; gap: 8px; list-style: none; }

.nav-link {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px 16px;
    border-radius: 12px;
    color: var(--text-secondary);
    text-decoration: none;
    font-weight: 600;
    font-size: 0.95rem;
    transition: all 0.2s;
    background: transparent;
    border: none;
    width: 100%;
    cursor: pointer;
    text-align: left;
}

.nav-link:hover { color: var(--text-primary); background: rgba(255, 255, 255, 0.05); }
.nav-link.active { background: var(--accent); color: #fff; box-shadow: 0 4px 12px var(--accent-glow); }

/* Main Content Area */
.main-content {
    flex: 1;
    padding: 30px 24px 90px 24px;
    max-width: 1000px;
    margin: 0 auto;
    width: 100%;
}

/* Mobile Header & Bottom Navigation */
.mobile-header { display: none; align-items: center; justify-content: space-between; padding: 16px 20px; background: var(--card-bg); border-bottom: 1px solid var(--card-border); }
.bottom-nav { display: none; position: fixed; bottom: 0; left: 0; right: 0; background: var(--card-bg); border-top: 1px solid var(--card-border); padding: 8px; backdrop-filter: blur(16px); z-index: 100; justify-content: space-around; }
.bottom-nav .nav-link { flex-direction: column; gap: 4px; padding: 8px; font-size: 0.75rem; align-items: center; justify-content: center; text-align: center; }

@media (max-width: 768px) {
    .app-shell { flex-direction: column; }
    .side-nav { display: none; }
    .mobile-header { display: flex; }
    .bottom-nav { display: flex; }
    .main-content { padding: 20px 16px 100px 16px; }
}

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
    background: linear-gradient(135deg, var(--accent) 0%, #4338ca 100%); color: #fff; border: none;
    padding: 0 26px; border-radius: 14px; font-weight: 600; font-size: 0.95rem; cursor: pointer;
    transition: all 0.2s; box-shadow: 0 4px 15px var(--accent-glow);
}
.search-card button:hover { transform: translateY(-1px); }

.progress-panel {
    background: var(--card-bg); backdrop-filter: blur(16px); border: 1px solid var(--card-border);
    border-radius: 20px; padding: 22px; margin-bottom: 25px; display: none; box-shadow: 0 10px 30px rgba(0,0,0,0.2);
}
.progress-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.progress-title { font-size: 0.92rem; font-weight: 700; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 80%; }
.progress-percent { font-size: 0.9rem; font-weight: 700; color: var(--accent); }
.progress-track { width: 100%; height: 8px; background: rgba(255, 255, 255, 0.1); border-radius: 10px; overflow: hidden; margin-bottom: 12px; }
.progress-fill { height: 100%; width: 0%; background: linear-gradient(90deg, var(--accent) 0%, #a855f7 100%); border-radius: 10px; transition: width 0.3s ease; }
.progress-steps { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-bottom: 10px; background: var(--input-bg); padding: 8px 10px; border-radius: 10px; border: 1px solid var(--card-border); }
.step-item { display: flex; align-items: center; gap: 6px; font-size: 0.75rem; color: var(--text-secondary); font-weight: 500; }
.step-dot { width: 7px; height: 7px; border-radius: 50%; background: #374151; flex-shrink: 0; }
.step-item.active { color: var(--text-primary); font-weight: 600; }
.step-item.active .step-dot { background: var(--accent); box-shadow: 0 0 8px var(--accent); }
.step-item.completed { color: var(--success); font-weight: 600; }
.step-item.completed .step-dot { background: var(--success); }
.progress-details { display: flex; justify-content: space-between; font-size: 0.8rem; color: var(--text-secondary); }

.results-grid { display: flex; flex-direction: column; gap: 12px; }
.result-card { background: var(--card-bg); backdrop-filter: blur(12px); border: 1px solid var(--card-border); border-radius: 16px; padding: 12px 16px; display: flex; align-items: center; gap: 16px; transition: all 0.2s; }
.result-card:hover { border-color: var(--accent); transform: translateY(-1px); }
.thumb-wrapper { position: relative; width: 110px; height: 65px; border-radius: 10px; overflow: hidden; flex-shrink: 0; background: var(--input-bg); }
.thumb-wrapper img { width: 100%; height: 100%; object-fit: cover; }
.badge-duration { position: absolute; bottom: 4px; right: 4px; background: rgba(0, 0, 0, 0.75); color: #fff; padding: 2px 5px; border-radius: 4px; font-size: 0.7rem; font-weight: 600; }
.track-info { flex: 1; min-width: 0; }
.track-title { font-size: 0.98rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-bottom: 3px; }
.track-artist { font-size: 0.85rem; color: var(--text-secondary); }
.btn-group { display: flex; gap: 8px; align-items: center; }

.btn-preview { background: rgba(255, 255, 255, 0.06); border: 1px solid var(--card-border); color: var(--text-primary); padding: 8px 14px; border-radius: 10px; font-weight: 600; font-size: 0.82rem; cursor: pointer; display: inline-flex; align-items: center; gap: 6px; transition: all 0.2s ease; }
.btn-preview:hover { background: var(--accent); color: #fff; }
.btn-preview.playing { background: var(--accent); color: #fff; }

.btn-download { background: linear-gradient(135deg, #10b981 0%, #059669 100%); color: #fff; border: none; padding: 8px 16px; border-radius: 10px; font-weight: 600; font-size: 0.82rem; cursor: pointer; box-shadow: 0 4px 12px rgba(16, 185, 129, 0.25); transition: all 0.2s; }
.btn-download:disabled { background: #374151; color: var(--text-secondary); cursor: not-allowed; box-shadow: none; }
.badge-library { background: rgba(16, 185, 129, 0.15); border: 1px solid rgba(16, 185, 129, 0.3); color: var(--success); padding: 6px 12px; border-radius: 10px; font-weight: 600; font-size: 0.82rem; display: inline-flex; align-items: center; gap: 4px; }
.btn-danger { background: rgba(239, 68, 68, 0.15); border: 1px solid rgba(239, 68, 68, 0.3); color: var(--danger); padding: 8px 14px; border-radius: 10px; font-weight: 600; font-size: 0.82rem; cursor: pointer; }
.btn-danger:hover { background: var(--danger); color: #fff; }

.settings-card { background: var(--card-bg); backdrop-filter: blur(16px); border: 1px solid var(--card-border); border-radius: 20px; padding: 25px; display: flex; flex-direction: column; gap: 20px; }
.setting-row { display: flex; justify-content: space-between; align-items: center; padding-bottom: 15px; border-bottom: 1px solid var(--card-border); }
.setting-row:last-child { border-bottom: none; padding-bottom: 0; }
.setting-label { font-weight: 600; font-size: 0.95rem; }
.setting-desc { font-size: 0.82rem; color: var(--text-secondary); margin-top: 2px; }
select, input[type="number"] { background: var(--input-bg); border: 1px solid var(--card-border); color: var(--text-primary); padding: 8px 12px; border-radius: 10px; outline: none; font-size: 0.9rem; }

.switch { position: relative; display: inline-block; width: 46px; height: 24px; }
.switch input { opacity: 0; width: 0; height: 0; }
.slider { position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background-color: #374151; transition: .3s; border-radius: 24px; }
.slider:before { position: absolute; content: ""; height: 18px; width: 18px; left: 3px; bottom: 3px; background-color: white; transition: .3s; border-radius: 50%; }
input:checked + .slider { background-color: var(--accent); }
input:checked + .slider:before { transform: translateX(22px); }
.save-btn { background: linear-gradient(135deg, var(--accent) 0%, #4338ca 100%); color: #fff; border: none; padding: 12px; border-radius: 12px; font-weight: 700; cursor: pointer; margin-top: 10px; }
.status-msg { text-align: center; color: var(--text-secondary); margin: 15px 0; font-size: 0.9rem; }

.library-header-bar {
    background: var(--card-bg); backdrop-filter: blur(12px); border: 1px solid var(--card-border);
    border-radius: 16px; padding: 14px 20px; margin-bottom: 16px; display: flex; justify-content: space-between;
    align-items: center; font-size: 0.9rem; color: var(--text-secondary);
}
.library-header-bar strong { color: var(--text-primary); }
.btn-refresh {
    background: rgba(255, 255, 255, 0.08); border: 1px solid var(--card-border); color: var(--text-primary);
    padding: 6px 14px; border-radius: 10px; font-weight: 600; font-size: 0.82rem; cursor: pointer;
    transition: all 0.2s; display: inline-flex; align-items: center; gap: 6px;
}
.btn-refresh:hover { background: var(--accent); color: #fff; }

/* Toast Notifications Container */
#toast-container {
    position: fixed;
    top: 20px;
    right: 20px;
    z-index: 1000;
    display: flex;
    flex-direction: column;
    gap: 10px;
}
.toast {
    background: var(--card-bg);
    border: 1px solid var(--accent);
    color: var(--text-primary);
    padding: 12px 18px;
    border-radius: 12px;
    box-shadow: 0 10px 25px rgba(0,0,0,0.3);
    backdrop-filter: blur(12px);
    display: flex;
    align-items: center;
    gap: 10px;
    animation: slideIn 0.3s cubic-bezier(0.16, 1, 0.3, 1);
}
@keyframes slideIn { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }

@media(max-width: 640px) { .result-card { flex-direction: column; align-items: flex-start; } .thumb-wrapper { width: 100%; height: 140px; } .btn-group { width: 100%; justify-content: space-between; } .progress-steps { grid-template-columns: 1fr; } }
</style>
</head>
<body>

<div id="toast-container" aria-live="polite" aria-atomic="true"></div>

<div class="app-shell">
    <!-- Desktop Side Navigation -->
    <nav class="side-nav" aria-label="Main Navigation">
        <div class="side-brand">
            <svg width="36" height="36" viewBox="0 0 512 512" aria-hidden="true">
              <defs><linearGradient id="waveGrad" x1="0%" y1="0%" x2="100%" y2="100%"><stop offset="0%" stop-color="#00F2FE" /><stop offset="100%" stop-color="#4FACFE" /></linearGradient></defs>
              <rect width="512" height="512" rx="112" fill="#0F172A" />
              <rect x="112" y="216" width="24" height="80" rx="12" fill="url(#waveGrad)" opacity="0.4" />
              <rect x="160" y="176" width="24" height="160" rx="12" fill="url(#waveGrad)" opacity="0.75" />
              <rect x="328" y="176" width="24" height="160" rx="12" fill="url(#waveGrad)" opacity="0.75" />
              <rect x="376" y="216" width="24" height="80" rx="12" fill="url(#waveGrad)" opacity="0.4" />
              <path d="M 256 128 V 300 M 196 248 L 256 312 L 316 248" stroke="url(#waveGrad)" stroke-width="28" stroke-linecap="round" stroke-linejoin="round" fill="none" />
              <path d="M 196 376 H 316" stroke="url(#waveGrad)" stroke-width="24" stroke-linecap="round" />
            </svg>
            <h1>Xrob Music</h1>
        </div>
        <ul class="nav-list" role="tablist">
            <li><button class="nav-link active" id="btn-search" role="tab" aria-selected="true" aria-controls="tab-search" onclick="navigate('search')"><span>🔍</span> Search & Download</button></li>
            <li><button class="nav-link" id="btn-downloads" role="tab" aria-selected="false" aria-controls="tab-downloads" onclick="navigate('downloads')"><span>⬇️</span> Downloads (<span id="queueCount">0</span>)</button></li>
            <li><button class="nav-link" id="btn-library" role="tab" aria-selected="false" aria-controls="tab-library" onclick="navigate('library')"><span>📂</span> Library (<span id="sideLibCount">0</span>)</button></li>
            <li><button class="nav-link" id="btn-settings" role="tab" aria-selected="false" aria-controls="tab-settings" onclick="navigate('settings')"><span>⚙️</span> Settings</button></li>
        </ul>
    </nav>

    <!-- App Content Shell -->
    <div style="flex: 1; display: flex; flex-direction: column;">
        <header class="mobile-header">
            <div class="side-brand">
                <svg width="30" height="30" viewBox="0 0 512 512" aria-hidden="true">
                  <rect width="512" height="512" rx="112" fill="#0F172A" />
                  <path d="M 256 128 V 300 M 196 248 L 256 312 L 316 248" stroke="#4FACFE" stroke-width="32" stroke-linecap="round" stroke-linejoin="round" fill="none" />
                </svg>
                <h1 style="font-size: 1.1rem;">Xrob Music</h1>
            </div>
        </header>

        <main class="main-content" id="main-content">
            <!-- TAB 1: SEARCH -->
            <section id="tab-search" class="tab-content active" role="tabpanel" aria-labelledby="btn-search">
                <div class="search-card">
                    <label for="query" class="sr-only">Search tracks, artists, or albums</label>
                    <input id="query" placeholder="Search track, artist, or album..." autocomplete="off" />
                    <button id="searchBtn" type="button" aria-label="Execute search">Search</button>
                </div>

                <div class="progress-panel" id="progressPanel" style="display:none;" aria-live="polite">
                    <div style="font-size: 0.95rem; font-weight: 700; margin-bottom: 14px; color: var(--text-primary); display: flex; align-items: center; gap: 8px;">
                        ⚡ Active Downloads
                    </div>
                    <div id="activeDownloadsList" style="display: flex; flex-direction: column; gap: 14px;"></div>
                </div>

                <div id="statusMsg" class="status-msg" aria-live="polite"></div>
                <div id="results" class="results-grid" aria-live="polite"></div>
                <div id="infiniteLoader" class="status-msg" style="display:none;">⏳ Loading more tracks...</div>
            </section>

            <!-- TAB 2: DOWNLOADS -->
            <section id="tab-downloads" class="tab-content" role="tabpanel" aria-labelledby="btn-downloads">
                <div class="library-header-bar">
                    <span>⬇️ Active & Recent Downloads</span>
                    <button class="btn-refresh" onclick="loadDownloads()">🔄 Refresh</button>
                </div>
                <div id="downloadsList" class="results-grid"></div>
            </section>

            <!-- TAB 3: LIBRARY -->
            <section id="tab-library" class="tab-content" role="tabpanel" aria-labelledby="btn-library">
                <div class="search-card" style="margin-bottom: 16px;">
                    <label for="libSearchQuery" class="sr-only">Filter local tracks</label>
                    <input id="libSearchQuery" placeholder="🔍 Filter local library tracks..." autocomplete="off" oninput="filterLibrary()" />
                </div>
                <div class="library-header-bar">
                    <span>🎵 Tracks: <strong id="statTracks">0</strong> · 👤 Artists: <strong id="statArtists">0</strong> · 💿 Albums: <strong id="statAlbums">0</strong></span>
                </div>
                <div class="library-header-bar">
                    <span>📁 Total Tracks: <strong id="libCountDetail">0</strong></span>
                    <span>💾 Folder Size: <strong id="libFolderSize">0 MB</strong></span>
                    <button class="btn-refresh" onclick="loadLibrary()" aria-label="Refresh library files">🔄 Refresh</button>
                </div>
                <div id="libraryList" class="results-grid" aria-live="polite"></div>
            </section>

            <!-- TAB 3: SETTINGS -->
            <section id="tab-settings" class="tab-content" role="tabpanel" aria-labelledby="btn-settings">
                <div class="settings-card">
                    <div class="setting-row">
                        <div><div class="setting-label">Theme Support</div><div class="setting-desc">Switch between Dark and Light mode</div></div>
                        <select id="set_theme" onchange="toggleTheme(this.value)">
                            <option value="dark">Dark Mode</option>
                            <option value="light">Light Mode</option>
                        </select>
                    </div>
                    <div class="setting-row">
                        <div><div class="setting-label">Desktop Notifications</div><div class="setting-desc">Alert when downloads finish successfully</div></div>
                        <button class="btn-refresh" onclick="requestNotificationPermission()">Enable Notifications</button>
                    </div>
                    <div class="setting-row">
                        <div><div class="setting-label">Audio Format</div><div class="setting-desc">Preferred output audio format for downloads</div></div>
                        <select id="set_format"><option value="mp3">MP3</option><option value="flac">FLAC (Lossless)</option><option value="m4a">M4A (AAC)</option><option value="opus">OPUS</option></select>
                    </div>
                    <div class="setting-row">
                        <div><div class="setting-label">Audio Quality / Bitrate</div><div class="setting-desc">Bitrate target for lossy formats</div></div>
                        <select id="set_quality"><option value="320K">320 Kbps</option><option value="256K">256 Kbps</option><option value="192K">192 Kbps</option><option value="128K">128 Kbps</option></select>
                    </div>
                    <div class="setting-row">
                        <div><div class="setting-label">Embed Album Art</div><div class="setting-desc">Embed cover art into audio files</div></div>
                        <label class="switch"><input type="checkbox" id="set_thumb"><span class="slider"></span></label>
                    </div>
                    <div class="setting-row">
                        <div><div class="setting-label">Embed Metadata Tags</div><div class="setting-desc">Write ID3 tags into the file</div></div>
                        <label class="switch"><input type="checkbox" id="set_meta"><span class="slider"></span></label>
                    </div>
                    <div class="setting-row">
                        <div><div class="setting-label">Organize by Artist</div><div class="setting-desc">Save new tracks in Artist/Track folders</div></div>
                        <label class="switch"><input type="checkbox" id="set_organize"><span class="slider"></span></label>
                    </div>
                    <div class="setting-row">
                        <div><div class="setting-label">Max Search Results</div><div class="setting-desc">Number of YouTube items returned per search page</div></div>
                        <input type="number" id="set_max_results" min="5" max="50" value="20" style="width:70px;">
                    </div>
                    <button class="save-btn" onclick="saveSettings()">Save Settings</button>
                    <div id="settingsMsg" class="status-msg"></div>
                </div>
            </section>
        </main>
    </div>
</div>

<!-- Mobile Bottom Tab Navigation -->
<nav class="bottom-nav" aria-label="Mobile Bottom Navigation">
    <button class="nav-link active" id="mob-btn-search" role="tab" aria-selected="true" aria-controls="tab-search" onclick="navigate('search')"><span>🔍</span> Search</button>
    <button class="nav-link" id="mob-btn-downloads" role="tab" aria-selected="false" aria-controls="tab-downloads" onclick="navigate('downloads')"><span>⬇️</span> Downloads</button>
    <button class="nav-link" id="mob-btn-library" role="tab" aria-selected="false" aria-controls="tab-library" onclick="navigate('library')"><span>📂</span> Library (<span id="mobLibCount">0</span>)</button>
    <button class="nav-link" id="mob-btn-settings" role="tab" aria-selected="false" aria-controls="tab-settings" onclick="navigate('settings')"><span>⚙️</span> Settings</button>
</nav>

<script>
let pollTimer = null;
let completedSet = new Set();
let libraryFilesSet = new Set();
let rawLibraryFiles = [];
let activeAudio = null;
let activePreviewBtn = null;

let currentPage = 1;
let currentQuery = "";
let isLoadingMore = false;
let hasMoreResults = true;

/* Notifications API Integration */
function requestNotificationPermission() {
    if ("Notification" in window) {
        Notification.requestPermission().then(permission => {
            if (permission === "granted") {
                showToast("✅ Notifications enabled!");
            } else {
                showToast("⚠️ Notification permission denied.");
            }
        });
    }
}

function notifyTrackComplete(title) {
    showToast(`🎉 Track ready: ${title}`);
    if ("Notification" in window && Notification.permission === "granted") {
        new Notification("Track Installed Successfully!", {
            body: `${title} is now downloaded and ready in your library.`,
            icon: "https://via.placeholder.com/64?text=🎵"
        });
    }
}

function showToast(message) {
    const container = document.getElementById("toast-container");
    const toast = document.createElement("div");
    toast.className = "toast";
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => toast.remove(), 4000);
}

/* Dynamic Dark Mode & Accessibility Helper */
function toggleTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('xrob_music_theme', theme);
}

const savedTheme = localStorage.getItem('xrob_music_theme') || 'dark';
toggleTheme(savedTheme);

/* Fast Navigation Structures & Deep Linking */
function navigate(tab, updateHash = true) {
    if (updateHash) {
        window.location.hash = tab;
    }
    switchTab(tab);
}

function switchTab(tab) {
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.querySelectorAll('.nav-link').forEach(b => {
        b.classList.remove('active');
        b.setAttribute('aria-selected', 'false');
    });

    const activeContent = document.getElementById(`tab-${tab}`);
    if (activeContent) {
        activeContent.classList.add('active');
    }

    const sideBtn = document.getElementById(`btn-${tab}`);
    const mobBtn = document.getElementById(`mob-btn-${tab}`);
    if (sideBtn) { sideBtn.classList.add('active'); sideBtn.setAttribute('aria-selected', 'true'); }
    if (mobBtn) { mobBtn.classList.add('active'); mobBtn.setAttribute('aria-selected', 'true'); }

    if (tab === 'library') loadLibrary();
    if (tab === 'downloads') loadDownloads();
    if (tab === 'settings') loadSettings();
}

function handleDeepLink() {
    const hash = window.location.hash.replace('#', '');
    if (['search', 'downloads', 'library', 'settings'].includes(hash)) {
        switchTab(hash);
    } else {
        switchTab('search');
    }
}

window.addEventListener('hashchange', handleDeepLink);

function normalizeKey(value) {
    return (value || "").toLowerCase()
        .replace(/\b(official\s*(video|audio|music video)|lyrics?|hd|4k|remaster(ed)?|audio)\b/gi, " ")
        .replace(/[^a-z0-9]+/g, "");
}

function escapeHtml(t) { return (t || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }

async function loadSettings() {
    try {
        const res = await fetch('api/settings');
        const s = await res.json();
        document.getElementById('set_format').value = s.audio_format || 'mp3';
        document.getElementById('set_quality').value = s.audio_quality || '320K';
        document.getElementById('set_thumb').checked = s.embed_thumbnail;
        document.getElementById('set_meta').checked = s.embed_metadata;
        document.getElementById('set_max_results').value = s.max_results || 20;
        document.getElementById('set_organize').checked = !!s.organize_by_artist;
        document.getElementById('set_theme').value = localStorage.getItem('xrob_music_theme') || 'dark';
    } catch(e) {}
}

async function saveSettings() {
    const data = {
        audio_format: document.getElementById('set_format').value,
        audio_quality: document.getElementById('set_quality').value,
        embed_thumbnail: document.getElementById('set_thumb').checked,
        embed_metadata: document.getElementById('set_meta').checked,
        max_results: parseInt(document.getElementById('set_max_results').value) || 20,
        organize_by_artist: document.getElementById('set_organize').checked
    };
    const msg = document.getElementById('settingsMsg');
    msg.textContent = "Saving...";
    try {
        await fetch('api/settings', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data) });
        msg.textContent = "✅ Settings saved!";
        setTimeout(() => msg.textContent = "", 3000);
    } catch(e) { msg.textContent = "❌ Failed to save settings."; }
}

async function refreshLibraryCache() {
    try {
        const res = await fetch('api/library');
        const data = await res.json();
        libraryFilesSet.clear();
        rawLibraryFiles = data.files || [];
        rawLibraryFiles.forEach(f => {
            const baseName = f.name.substring(f.name.lastIndexOf('/') + 1, f.name.lastIndexOf('.')) || f.name;
            libraryFilesSet.add(normalizeKey(baseName));
        });
        const count = rawLibraryFiles.length;
        if (document.getElementById('sideLibCount')) document.getElementById('sideLibCount').textContent = count;
        if (document.getElementById('mobLibCount')) document.getElementById('mobLibCount').textContent = count;
        if (document.getElementById('libCountDetail')) document.getElementById('libCountDetail').textContent = count;
        if (document.getElementById('libFolderSize')) document.getElementById('libFolderSize').textContent = data.total_size;
    } catch(e) {}
}

function stopCurrentPreview() {
    if (activeAudio) { activeAudio.pause(); activeAudio = null; }
    if (activePreviewBtn) {
        activePreviewBtn.classList.remove('playing', 'loading');
        activePreviewBtn.innerHTML = activePreviewBtn.dataset.type === 'library' ? `▶ Play` : `▶ Preview`;
        activePreviewBtn = null;
    }
}

function toggleAudioStream(btn, streamUrl, type = 'search') {
    if (activePreviewBtn === btn && activeAudio) {
        if (activeAudio.paused) {
            activeAudio.play();
            btn.classList.add('playing');
            btn.innerHTML = `⏸ Pause`;
        } else {
            activeAudio.pause();
            btn.classList.remove('playing');
            btn.innerHTML = type === 'library' ? `▶ Play` : `▶ Preview`;
        }
        return;
    }
    stopCurrentPreview();
    btn.dataset.type = type;
    activePreviewBtn = btn;
    btn.classList.add('loading');
    btn.innerHTML = `⏳ Loading...`;
    
    const audio = new Audio(streamUrl);
    activeAudio = audio;
    
    audio.play().then(() => {
        btn.classList.remove('loading');
        btn.classList.add('playing');
        btn.innerHTML = `⏸ Pause`;
    }).catch(err => {
        if (!streamUrl.includes('transcode=true')) {
            const transcodeUrl = streamUrl + (streamUrl.includes('?') ? '&' : '?') + 'transcode=true';
            toggleAudioStream(btn, transcodeUrl, type);
        } else {
            stopCurrentPreview();
            btn.innerHTML = `❌ Error`;
            setTimeout(() => { btn.innerHTML = type === 'library' ? `▶ Play` : `▶ Preview`; }, 2000);
        }
    });
    
    audio.onerror = () => {
        if (!streamUrl.includes('transcode=true')) {
            const transcodeUrl = streamUrl + (streamUrl.includes('?') ? '&' : '?') + 'transcode=true';
            toggleAudioStream(btn, transcodeUrl, type);
        } else {
            stopCurrentPreview();
            btn.innerHTML = `❌ Error`;
            setTimeout(() => { btn.innerHTML = type === 'library' ? `▶ Play` : `▶ Preview`; }, 2000);
        }
    };
    
    audio.onended = () => { stopCurrentPreview(); };
}

function renderItems(data) {
    const results = document.getElementById("results");
    data.forEach(item => {
        const cleanedTitle = normalizeKey(item.title || "Unknown");
        const isInLibrary = libraryFilesSet.has(cleanedTitle);

        const card = document.createElement("div");
        card.className = "result-card";

        const thumbUrl = escapeHtml(item.thumbnail);
        const titleHtml = escapeHtml(item.title);
        const artistHtml = escapeHtml(item.channel);
        const durHtml = escapeHtml(item.duration_text);

        card.innerHTML = `
            <div class="thumb-wrapper">
                <img src="${thumbUrl}" alt="Cover art for ${titleHtml}" onerror="this.src='https://via.placeholder.com/110x65?text=Music'" />
                <span class="badge-duration">${durHtml}</span>
            </div>
            <div class="track-info">
                <div class="track-title">${titleHtml}</div>
                <div class="track-artist">👤 ${artistHtml}</div>
            </div>
            <div class="btn-group" data-group-id="${item.id}"></div>
        `;

        const btnGroup = card.querySelector('.btn-group');

        if (isInLibrary) {
            btnGroup.innerHTML = `<div class="badge-library">✅ In Library</div>`;
        } else {
            const prevBtn = document.createElement('button');
            prevBtn.className = 'btn-preview';
            prevBtn.setAttribute('aria-label', `Preview ${titleHtml}`);
            prevBtn.innerHTML = '▶ Preview';
            prevBtn.onclick = () => toggleAudioStream(prevBtn, "api/preview?url=" + encodeURIComponent(item.url), 'search');

            const dlBtn = document.createElement('button');
            dlBtn.className = 'btn-download';
            dlBtn.setAttribute('data-id', item.id);
            dlBtn.setAttribute('aria-label', `Download ${titleHtml}`);
            dlBtn.innerHTML = '⬇️ Save';
            dlBtn.onclick = () => startDownload(item.url, item.title, item.id, item.channel);

            btnGroup.appendChild(prevBtn);
            btnGroup.appendChild(dlBtn);
        }

        results.appendChild(card);
    });
}

async function searchMusic() {
    const query = document.getElementById("query").value.trim();
    const statusMsg = document.getElementById("statusMsg");
    const results = document.getElementById("results");
    const searchBtn = document.getElementById("searchBtn");

    if (!query) return;
    stopCurrentPreview();
    
    currentQuery = query;
    currentPage = 1;
    hasMoreResults = true;
    isLoadingMore = false;

    statusMsg.textContent = "🔍 Searching YouTube...";
    results.innerHTML = ""; searchBtn.disabled = true;
    await refreshLibraryCache();

    try {
        const response = await fetch(`api/search?q=${encodeURIComponent(query)}&page=1`);
        if (!response.ok) throw new Error("Search failed");
        const data = await response.json();
        
        if (data.length === 0) { statusMsg.textContent = "No results found."; hasMoreResults = false; return; }
        statusMsg.textContent = "";

        renderItems(data);
    } catch (err) { statusMsg.textContent = "❌ " + err.message; }
    finally { searchBtn.disabled = false; }
}

async function loadMoreResults() {
    if (isLoadingMore || !hasMoreResults || !currentQuery) return;
    
    isLoadingMore = true;
    currentPage++;
    const loader = document.getElementById("infiniteLoader");
    loader.style.display = "block";

    try {
        const response = await fetch(`api/search?q=${encodeURIComponent(currentQuery)}&page=${currentPage}`);
        if (!response.ok) throw new Error("Load failed");
        const data = await response.json();

        if (!data || data.length === 0) {
            hasMoreResults = false;
        } else {
            renderItems(data);
        }
    } catch (e) {
        hasMoreResults = false;
    } finally {
        loader.style.display = "none";
        isLoadingMore = false;
    }
}

async function startDownload(url, title, elementId, artist = 'Unknown Artist') {
    if (elementId) {
        const btn = document.querySelector(`button[data-id="${elementId}"]`);
        if (btn) { btn.disabled = true; btn.textContent = "⏳ Queued"; }
    }
    try {
        await fetch('api/download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url, title, elementId, artist })
        });
        clearTimeout(pollTimer);
        pollTasks();
    } catch (e) { alert("Failed to enqueue download."); }
}

async function pollTasks() {
    try {
        const res = await fetch('api/tasks');
        const tasks = await res.json();
        
        let libraryNeedsUpdate = false;

        tasks.forEach(t => {
            if (t.status === 'completed' && !completedSet.has(t.id)) {
                completedSet.add(t.id);
                libraryNeedsUpdate = true;
                
                // Trigger Web Notification & Toast
                notifyTrackComplete(t.title);

                if (t.elementId) {
                    const grp = document.querySelector(`div[data-group-id="${t.elementId}"]`);
                    if (grp) grp.innerHTML = `<div class="badge-library">✅ In Library</div>`;
                }
            }
        });

        if (libraryNeedsUpdate) {
            await refreshLibraryCache();
            if (document.getElementById('tab-library').classList.contains('active')) {
                loadLibrary();
            }
        }

        const activeTasks = tasks.filter(t => 
            t.status === 'queued' || 
            t.status === 'downloading' || 
            t.status === 'processing' || 
            (t.status === 'error' && (Date.now() - t.last_updated < 4000)) ||
            (t.status === 'completed' && (Date.now() - t.last_updated < 2000))
        );

        const panel = document.getElementById("progressPanel");
        const listContainer = document.getElementById("activeDownloadsList");

        if (activeTasks.length === 0) {
            panel.style.display = "none";
            listContainer.innerHTML = "";
            pollTimer = setTimeout(pollTasks, 3000);
            return;
        }

        panel.style.display = "block";
        listContainer.innerHTML = "";

        activeTasks.forEach(task => {
            const isError = task.status === 'error';
            const barColor = isError ? "var(--danger)" : "linear-gradient(90deg, var(--accent) 0%, #a855f7 100%)";
            const percent = Math.round(task.percent || 0);
            
            let step1 = "step-item", step2 = "step-item", step3 = "step-item";
            if (task.status === 'downloading') {
                step1 += " active";
            } else if (task.status === 'processing') {
                step1 += " completed"; step2 += " active";
            } else if (task.status === 'completed') {
                step1 += " completed"; step2 += " completed"; step3 += " completed";
            } else if (isError) {
                step1 += " active";
            }

            const itemHtml = `
                <div style="background: var(--input-bg); border: 1px solid var(--card-border); padding: 14px; border-radius: 12px;">
                    <div class="progress-header">
                        <span class="progress-title">${isError ? "❌ " : "🎵 "}${escapeHtml(task.title)}</span>
                        <div class="progress-right-header">
                            <span class="progress-percent" style="color: ${isError ? 'var(--danger)' : 'var(--accent)'}">${percent}%</span>
                        </div>
                    </div>
                    
                    <div class="progress-track">
                        <div class="progress-fill" style="width: ${percent}%; background: ${barColor};"></div>
                    </div>

                    <div class="progress-steps">
                        <div class="${step1}"><span class="step-dot"></span> 1. Download</div>
                        <div class="${step2}"><span class="step-dot"></span> 2. Clean Tags</div>
                        <div class="${step3}"><span class="step-dot"></span> 3. Ready</div>
                    </div>

                    <div class="progress-details">
                        <span>${escapeHtml(task.error || task.step || "Queued...")}</span>
                        <span>${escapeHtml(task.speed || "")}</span>
                    </div>
                </div>
            `;
            listContainer.insertAdjacentHTML('beforeend', itemHtml);
        });

        pollTimer = setTimeout(pollTasks, 1500);

    } catch (e) {
        pollTimer = setTimeout(pollTasks, 3000);
    }
}

async function loadStats() {
    try {
        const stats = await fetch('api/stats').then(r => r.json());
        document.getElementById('statTracks').textContent = stats.tracks || 0;
        document.getElementById('statArtists').textContent = stats.artists || 0;
        document.getElementById('statAlbums').textContent = stats.albums || 0;
    } catch(e) {}
}

async function cancelTask(taskId) {
    await fetch(`api/tasks/${encodeURIComponent(taskId)}/cancel`, {method: 'POST'});
    loadDownloads();
}

async function loadDownloads() {
    const list = document.getElementById('downloadsList');
    try {
        const tasks = await fetch('api/tasks').then(r => r.json());
        document.getElementById('queueCount').textContent =
            tasks.filter(t => ['queued','downloading','processing'].includes(t.status)).length;
        if (!tasks.length) {
            list.innerHTML = '<div class="status-msg">No downloads yet.</div>';
            return;
        }
        list.innerHTML = tasks.map(t => `
            <div class="result-card">
                <div class="track-info">
                    <div class="track-title">${escapeHtml(t.title)}</div>
                    <div class="track-artist">${escapeHtml(t.artist || 'Unknown Artist')} · ${escapeHtml(t.status)} · ${Math.round(t.percent || 0)}%</div>
                    <div class="progress-track" style="margin-top:8px"><div class="progress-fill" style="width:${Math.round(t.percent || 0)}%"></div></div>
                </div>
                <div class="btn-group">
                    ${['queued','downloading','processing'].includes(t.status) ? `<button class="btn-danger" onclick="cancelTask('${t.id}')">✕ Cancel</button>` : ''}
                </div>
            </div>`).join('');
    } catch(e) {
        list.innerHTML = '<div class="status-msg">Failed to load downloads.</div>';
    }
}

async function loadLibrary() {
    const list = document.getElementById('libraryList');
    list.innerHTML = `<div class="status-msg">Loading library...</div>`;
    try {
        await refreshLibraryCache();
        await loadStats();
        filterLibrary();
    } catch(e) { list.innerHTML = `<div class="status-msg">Failed to load library.</div>`; }
}

function filterLibrary() {
    const list = document.getElementById('libraryList');
    const q = (document.getElementById('libSearchQuery').value || "").toLowerCase().trim();

    const filtered = rawLibraryFiles.filter(f => f.name.toLowerCase().includes(q));

    if (filtered.length === 0) {
        list.innerHTML = `<div class="status-msg">${rawLibraryFiles.length === 0 ? "No files downloaded yet." : "No matching tracks found."}</div>`;
        return;
    }

    list.innerHTML = "";
    filtered.forEach(f => {
        const card = document.createElement('div');
        card.className = 'result-card';

        const encName = encodeURIComponent(f.name);
        const coverUrl = "api/library/cover/" + encName;
        const streamUrl = "api/library/stream/" + encName;
        const fallbackSvg = `data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="110" height="65" viewBox="0 0 110 65"><rect width="100%" height="100%" fill="%231e293b"/><text x="50%" y="50%" fill="%239ca3af" font-size="20" text-anchor="middle" dominant-baseline="central">🎵</text></svg>`;

        card.innerHTML = `
            <div class="thumb-wrapper">
                <img src="${coverUrl}" alt="Album cover for ${escapeHtml(f.name)}" onerror="this.onerror=null; this.src='${fallbackSvg}'" />
            </div>
            <div class="track-info">
                <div class="track-title">${escapeHtml(f.name)}</div>
                <div class="track-artist">📦 ${f.size}</div>
            </div>
            <div class="btn-group">
                <button class="btn-preview" aria-label="Play ${escapeHtml(f.name)}">▶ Play</button>
                <button class="btn-danger" aria-label="Delete ${escapeHtml(f.name)}">🗑 Delete</button>
            </div>
        `;

        const playBtn = card.querySelector('.btn-preview');
        playBtn.onclick = () => toggleAudioStream(playBtn, streamUrl, 'library');

        const delBtn = card.querySelector('.btn-danger');
        delBtn.onclick = () => deleteFile(f.name);

        list.appendChild(card);
    });
}

async function deleteFile(filename) {
    if (!confirm("Delete " + filename + "?")) return;
    try {
        await fetch('api/library/' + encodeURIComponent(filename), { method: 'DELETE' });
        await refreshLibraryCache();
        await loadStats();
        filterLibrary();
    } catch(e) { alert("Failed to delete file."); }
}

document.getElementById("searchBtn").addEventListener("click", searchMusic);
document.getElementById("query").addEventListener("keydown", e => { if (e.key === "Enter") searchMusic(); });

window.addEventListener("scroll", () => {
    if (document.getElementById("tab-search").classList.contains("active")) {
        if ((window.innerHeight + window.scrollY) >= (document.body.offsetHeight - 500)) {
            loadMoreResults();
        }
    }
});

refreshLibraryCache();

document.addEventListener("DOMContentLoaded", () => {
    handleDeepLink();
    pollTasks();
});
</script>
</body>
</html>
"""


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
    for path in get_all_audio_files():
        sz = path.stat().st_size
        total_bytes += sz
        files.append({
            "name": str(path.relative_to(DOWNLOAD_DIR)),
            "size": format_size(sz),
            "bytes": sz
        })
    return {
        "files": sorted(files, key=lambda x: x["name"]),
        "total_size": format_size(total_bytes),
        "total_bytes": total_bytes
    }



@app.get("/api/stats")
async def get_stats():
    files = get_all_audio_files()
    total_bytes = sum(p.stat().st_size for p in files)
    artists = set()
    albums = set()
    for p in files:
        rel = p.relative_to(DOWNLOAD_DIR)
        if len(rel.parts) > 1:
            artists.add(rel.parts[0])
    return {
        "tracks": len(files),
        "artists": len(artists),
        "albums": len(albums),
        "total_size": format_size(total_bytes)
    }


@app.get("/api/library/stream/{filename:path}")
async def stream_library_file(filename: str, transcode: bool = Query(False)):
    file_path = resolve_file(filename)

    if transcode:
        async def transcode_generator():
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-i", str(file_path), "-vn", "-ab", "192k", "-f", "mp3", "pipe:1",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
            await proc.wait()

        return StreamingResponse(transcode_generator(), media_type="audio/mpeg")

    ext = file_path.suffix.lower()
    media_type = MEDIA_TYPES.get(ext, mimetypes.guess_type(file_path)[0] or "audio/mpeg")
    return FileResponse(path=file_path, filename=file_path.name, media_type=media_type)


@app.get("/api/library/cover/{filename:path}")
async def get_library_cover(filename: str):
    file_path = resolve_file(filename)
    
    command = [
        "ffmpeg",
        "-y",
        "-i", str(file_path),
        "-an",
        "-c:v", "mjpeg",
        "-frames:v", "1",
        "-f", "image2pipe",
        "-"
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await process.communicate()
        if process.returncode == 0 and len(stdout) > 0:
            return Response(content=stdout, media_type="image/jpeg")
    except Exception:
        pass

    svg_placeholder = """<svg xmlns="http://www.w3.org/2000/svg" width="110" height="65" viewBox="0 0 110 65"><rect width="100%" height="100%" fill="#1e293b"/><text x="50%" y="50%" fill="#9ca3af" font-size="20" text-anchor="middle" dominant-baseline="central">🎵</text></svg>"""
    return Response(content=svg_placeholder, media_type="image/svg+xml")


@app.delete("/api/library/{filename:path}")
async def delete_library_file(filename: str):
    file_path = resolve_file(filename)
    file_path.unlink()
    return {"status": "deleted"}


@app.get("/api/search")
async def search(q: str = Query(..., min_length=1), page: int = Query(1, ge=1)):
    settings = load_settings()
    try:
        results = await youtube_search(q, settings.get("max_results", 20), page)
        return results
    except Exception as error:
        raise HTTPException(status_code=500, detail="YouTube search failed: " + str(error))


@app.get("/api/preview")
async def preview_audio(url: str = Query(..., min_length=1)):
    if not (url.startswith("https://www.youtube.com/") or url.startswith("https://youtube.com/") or url.startswith("https://youtu.be/")):
        raise HTTPException(status_code=400, detail="Invalid YouTube URL.")
    try:
        process = await asyncio.create_subprocess_exec(
            "yt-dlp",
            "-g",
            "-f", "ba/b",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError("Failed to extract preview stream.")
        
        stream_url = stdout.decode("utf-8", errors="ignore").strip().split("\n")[0]
        if stream_url:
            return RedirectResponse(url=stream_url)
        raise HTTPException(status_code=400, detail="Could not retrieve audio stream.")
    except Exception as err:
        raise HTTPException(status_code=500, detail=str(err))


@app.post("/api/download")
async def enqueue_download(payload: dict = Body(...)):
    url = payload.get("url")
    title = payload.get("title", "Unknown")
    artist = payload.get("artist", "Unknown Artist")
    album = payload.get("album", "")
    element_id = payload.get("elementId", "")

    if is_duplicate(title):
        raise HTTPException(status_code=409, detail="This track already exists in your library.")
    
    if not url:
        raise HTTPException(status_code=400, detail="Missing URL")

    task_id = uuid.uuid4().hex
    task_info = {
        "id": task_id,
        "title": title,
        "artist": artist,
        "album": album,
        "url": url,
        "elementId": element_id,
        "status": "queued",
        "percent": 0,
        "speed": "",
        "step": "Waiting in queue...",
        "error": "",
        "last_updated": time.time() * 1000
    }
    
    TASKS[task_id] = task_info
    await task_queue.put(task_id)
    return {"status": "ok", "task_id": task_id}



@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"] in ["completed", "cancelled"]:
        return {"status": task["status"]}

    task["cancel_requested"] = True
    proc = ACTIVE_PROCESSES.get(task_id)
    if proc and proc.returncode is None:
        proc.terminate()
    task["status"] = "cancelled"
    task["step"] = "Cancelled"
    task["last_updated"] = time.time() * 1000
    return {"status": "cancelled"}


@app.get("/api/tasks")
async def get_tasks():
    now = time.time() * 1000
    for t in TASKS.values():
        if t["status"] in ["queued", "downloading", "processing"]:
            t["last_updated"] = now
            
    def sort_key(task):
        status_weight = {"downloading": 0, "processing": 1, "queued": 2, "completed": 3, "error": 4, "cancelled": 5}
        return status_weight.get(task["status"], 99)
        
    sorted_tasks = sorted(list(TASKS.values()), key=sort_key)
    return sorted_tasks


@app.get("/health")
async def health():
    return {"status": "ok", "service": "music-downloader"}
