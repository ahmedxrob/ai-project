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
    
    # Fallback search for missing extensions or stripped spaces
    matches = list(DOWNLOAD_DIR.glob(f"{clean_name}*"))
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
    font-size: 2.2rem; font-weight: 700; background: linear-gradient(135deg, #ffffff 0%, #a5b4fc 100%);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; letter-spacing: -0.02em;
}
.nav-tabs {
    display: flex; justify-content: center; gap: 8px; margin-bottom: 25px; background: var(--card-bg);
    padding: 6px; border-radius: 16px; border: 1px solid var(--card-border); backdrop-filter: blur(12px);
}
.tab-btn {
    background: transparent; border: none; color: var(--text-secondary); padding: 10px 24px;
    border-radius: 12px; font-weight: 600; font-size: 0.9rem; cursor: pointer; transition: all 0.2s;
}
.tab-btn:hover { color: #fff; }
.tab-btn.active { background: var(--accent); color: #fff; box-shadow: 0 4px 12px var(--accent-glow); }
.tab-content { display: none; }
.tab-content.active { display: block; }
.search-card {
    background: var(--card-bg); backdrop-filter: blur(16px); border: 1px solid var(--card-border);
    padding: 12px; border-radius: 20px; display: flex; gap: 10px; box-shadow: 0 20px 40px rgba(0, 0, 0, 0.4);
    margin-bottom: 25px;
}
.search-card input {
    flex: 1; background: var(--input-bg); border: 1px solid var(--card-border); padding: 14px 18px;
    border-radius: 14px; color: #fff; font-size: 1rem; outline: none; transition: all 0.2s;
}
.search-card input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-glow); }
.search-card button {
    background: linear-gradient(135deg, var(--accent) 0%, #4338ca 100%); color: #fff; border: none;
    padding: 0 26px; border-radius: 14px; font-weight: 600; font-size: 0.95rem; cursor: pointer;
    transition: all 0.2s; box-shadow: 0 4px 15px var(--accent-glow);
}
.search-card button:hover { transform: translateY(-1px); }
.progress-panel {
    background: var(--card-bg); backdrop-filter: blur(16px); border: 1px solid var(--card-border);
    border-radius: 20px; padding: 22px; margin-bottom: 25px; display: none; box-shadow: 0 10px 30px rgba(0,0,0,0.5);
    animation: fadeIn 0.3s ease;
}
@keyframes fadeIn { from { opacity: 0; transform: translateY(-8px); } to { opacity: 1; transform: translateY(0); } }
.progress-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.progress-title { font-size: 0.98rem; font-weight: 700; color: var(--text-primary); }
.progress-right-header { display: flex; align-items: center; gap: 12px; }
.progress-percent { font-size: 0.92rem; font-weight: 700; color: var(--accent); }
.progress-track {
    width: 100%; height: 8px; background: rgba(255, 255, 255, 0.08); border-radius: 10px; overflow: hidden; margin-bottom: 15px;
}
.progress-fill {
    height: 100%; width: 0%; background: linear-gradient(90deg, var(--accent) 0%, #a855f7 100%);
    border-radius: 10px; transition: width 0.3s ease;
}
.progress-steps {
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-bottom: 15px; background: rgba(15, 23, 42, 0.6);
    padding: 10px; border-radius: 12px; border: 1px solid rgba(255, 255, 255, 0.04);
}
.step-item { display: flex; align-items: center; gap: 8px; font-size: 0.78rem; color: var(--text-secondary); font-weight: 500; }
.step-dot { width: 8px; height: 8px; border-radius: 50%; background: #374151; transition: all 0.3s; flex-shrink: 0; }
.step-item.active { color: #fff; font-weight: 600; }
.step-item.active .step-dot { background: var(--accent); box-shadow: 0 0 10px var(--accent); animation: pulseDot 1.2s infinite alternate; }
.step-item.completed { color: var(--success); font-weight: 600; }
.step-item.completed .step-dot { background: var(--success); }
@keyframes pulseDot { 0% { transform: scale(1); opacity: 0.8; } 100% { transform: scale(1.3); opacity: 1; } }
.progress-details { display: flex; justify-content: space-between; font-size: 0.82rem; color: var(--text-secondary); }
.queue-container { margin-top: 16px; padding-top: 14px; border-top: 1px solid rgba(255, 255, 255, 0.06); display: flex; flex-direction: column; gap: 8px; }
.queue-header-title { font-size: 0.78rem; font-weight: 700; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 2px; }
.queue-item { background: rgba(15, 23, 42, 0.6); border: 1px solid var(--card-border); padding: 8px 12px; border-radius: 10px; display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.queue-item-title { font-size: 0.85rem; font-weight: 500; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1; }
.queue-item-badge { background: rgba(99, 102, 241, 0.2); color: #a5b4fc; border: 1px solid rgba(99, 102, 241, 0.3); font-size: 0.72rem; font-weight: 600; padding: 2px 8px; border-radius: 6px; white-space: nowrap; }
.results-grid { display: flex; flex-direction: column; gap: 12px; }
.result-card { background: var(--card-bg); backdrop-filter: blur(12px); border: 1px solid var(--card-border); border-radius: 16px; padding: 12px 16px; display: flex; align-items: center; gap: 16px; transition: all 0.2s; }
.result-card:hover { border-color: rgba(255, 255, 255, 0.18); transform: translateY(-1px); }
.thumb-wrapper { position: relative; width: 110px; height: 65px; border-radius: 10px; overflow: hidden; flex-shrink: 0; background: #1e293b; }
.thumb-wrapper img { width: 100%; height: 100%; object-fit: cover; }
.badge-duration { position: absolute; bottom: 4px; right: 4px; background: rgba(0, 0, 0, 0.75); backdrop-filter: blur(4px); padding: 2px 5px; border-radius: 4px; font-size: 0.7rem; font-weight: 600; }
.track-info { flex: 1; min-width: 0; }
.track-title { font-size: 0.98rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-bottom: 3px; }
.track-artist { font-size: 0.85rem; color: var(--text-secondary); }
.btn-group { display: flex; gap: 8px; align-items: center; }
.btn-preview { background: rgba(255, 255, 255, 0.06); border: 1px solid var(--card-border); color: var(--text-primary); padding: 8px 14px; border-radius: 10px; font-weight: 600; font-size: 0.82rem; cursor: pointer; display: inline-flex; align-items: center; gap: 6px; transition: all 0.2s ease; user-select: none; }
.btn-preview:hover { background: rgba(99, 102, 241, 0.18); border-color: rgba(99, 102, 241, 0.4); color: #fff; }
.btn-preview.playing { background: linear-gradient(135deg, rgba(99, 102, 241, 0.3) 0%, rgba(168, 85, 247, 0.3) 100%); border-color: var(--accent); color: #a5b4fc; box-shadow: 0 0 12px rgba(99, 102, 241, 0.25); }
.btn-preview.loading { opacity: 0.7; cursor: wait; }
.wave-bars { display: inline-flex; align-items: flex-end; gap: 2px; height: 12px; }
.wave-bar { width: 2px; height: 100%; background-color: currentColor; border-radius: 2px; animation: waveBounce 0.8s ease-in-out infinite alternate; }
.wave-bar:nth-child(2) { animation-delay: 0.2s; }
.wave-bar:nth-child(3) { animation-delay: 0.4s; }
@keyframes waveBounce { 0% { height: 20%; } 100% { height: 100%; } }
.btn-download { background: linear-gradient(135deg, #10b981 0%, #059669 100%); color: #fff; border: none; padding: 8px 16px; border-radius: 10px; font-weight: 600; font-size: 0.82rem; cursor: pointer; box-shadow: 0 4px 12px rgba(16, 185, 129, 0.25); transition: all 0.2s; }
.btn-download:disabled { background: #374151; color: var(--text-secondary); cursor: not-allowed; box-shadow: none; }
.badge-library { background: rgba(16, 185, 129, 0.15); border: 1px solid rgba(16, 185, 129, 0.3); color: #6ee7b7; padding: 6px 12px; border-radius: 10px; font-weight: 600; font-size: 0.82rem; display: inline-flex; align-items: center; gap: 4px; }
.btn-danger { background: rgba(239, 68, 68, 0.15); border: 1px solid rgba(239, 68, 68, 0.3); color: #fca5a5; padding: 8px 14px; border-radius: 10px; font-weight: 600; font-size: 0.82rem; cursor: pointer; }
.btn-danger:hover { background: rgba(239, 68, 68, 0.3); }
.settings-card { background: var(--card-bg); backdrop-filter: blur(16px); border: 1px solid var(--card-border); border-radius: 20px; padding: 25px; display: flex; flex-direction: column; gap: 20px; }
.setting-row { display: flex; justify-content: space-between; align-items: center; padding-bottom: 15px; border-bottom: 1px solid rgba(255, 255, 255, 0.05); }
.setting-row:last-child { border-bottom: none; padding-bottom: 0; }
.setting-label { font-weight: 600; font-size: 0.95rem; }
.setting-desc { font-size: 0.82rem; color: var(--text-secondary); margin-top: 2px; }
select, input[type="number"] { background: var(--input-bg); border: 1px solid var(--card-border); color: #fff; padding: 8px 12px; border-radius: 10px; outline: none; font-size: 0.9rem; }
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
.library-header-bar strong { color: #fff; }
.btn-refresh {
    background: rgba(255, 255, 255, 0.08); border: 1px solid var(--card-border); color: var(--text-primary);
    padding: 6px 14px; border-radius: 10px; font-weight: 600; font-size: 0.82rem; cursor: pointer;
    transition: all 0.2s; display: inline-flex; align-items: center; gap: 6px;
}
.btn-refresh:hover { background: rgba(99, 102, 241, 0.2); border-color: var(--accent); color: #fff; }
@media(max-width: 640px) { .result-card { flex-direction: column; align-items: flex-start; } .thumb-wrapper { width: 100%; height: 140px; } .btn-group { width: 100%; justify-content: space-between; } .progress-steps { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<div class="container">
    <header>
        <svg width="44" height="44" viewBox="0 0 512 512" style="flex-shrink: 0;">
          <defs><linearGradient id="waveGrad" x1="0%" y1="0%" x2="100%" y2="100%"><stop offset="0%" stop-color="#00F2FE" /><stop offset="100%" stop-color="#4FACFE" /></linearGradient></defs>
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
                <div class="progress-right-header">
                    <span class="progress-percent" id="progressPercent">0%</span>
                </div>
            </div>
            
            <div class="progress-track">
                <div class="progress-fill" id="progressFill"></div>
            </div>

            <div class="progress-steps">
                <div class="step-item" id="stepDownload"><span class="step-dot"></span> 1. Download</div>
                <div class="step-item" id="stepProcess"><span class="step-dot"></span> 2. Clean Tags</div>
                <div class="step-item" id="stepDone"><span class="step-dot"></span> 3. Ready</div>
            </div>

            <div class="progress-details">
                <span id="progressStatus">Connecting...</span>
                <span id="progressSpeed">-- MB/s</span>
            </div>

            <div id="queueBadgeContainer"></div>
        </div>

        <div id="statusMsg" class="status-msg"></div>
        <div id="results" class="results-grid"></div>
        <div id="infiniteLoader" class="status-msg" style="display:none;">⏳ Loading more tracks...</div>
    </div>

    <!-- TAB 2: LIBRARY -->
    <div id="tab-library" class="tab-content">
        <div class="search-card" style="margin-bottom: 16px;">
            <input id="libSearchQuery" placeholder="🔍 Filter local library tracks..." autocomplete="off" oninput="filterLibrary()" />
        </div>
        <div class="library-header-bar">
            <span>📁 Total Tracks: <strong id="libCountDetail">0</strong></span>
            <span>💾 Folder Size: <strong id="libFolderSize">0 MB</strong></span>
            <button class="btn-refresh" onclick="loadLibrary()">🔄 Refresh</button>
        </div>
        <div id="libraryList" class="results-grid"></div>
    </div>

    <!-- TAB 3: SETTINGS -->
    <div id="tab-settings" class="tab-content">
        <div class="settings-card">
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
                <div><div class="setting-label">Max Search Results</div><div class="setting-desc">Number of YouTube items returned per search page</div></div>
                <input type="number" id="set_max_results" min="5" max="50" value="20" style="width:70px;">
            </div>
            <button class="save-btn" onclick="saveSettings()">Save Settings</button>
            <div id="settingsMsg" class="status-msg"></div>
        </div>
    </div>
</div>

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

function escapeHtml(t) { return (t || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }

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
            const baseName = f.name.substring(0, f.name.lastIndexOf('.')) || f.name;
            libraryFilesSet.add(baseName.toLowerCase());
        });
        document.getElementById('libCount').textContent = rawLibraryFiles.length;
        if (document.getElementById('libCountDetail')) document.getElementById('libCountDetail').textContent = rawLibraryFiles.length;
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
            btn.innerHTML = `<span class="wave-bars"><span class="wave-bar"></span><span class="wave-bar"></span><span class="wave-bar"></span></span> Playing`;
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
        btn.innerHTML = `<span class="wave-bars"><span class="wave-bar"></span><span class="wave-bar"></span><span class="wave-bar"></span></span> Playing`;
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
        const cleanedTitle = (item.title || "Unknown").replace(/[\\\\/:*?"<>|]/g, "").replace(/\\s+/g, " ").trim().replace(/[ .]+$/, "").substring(0, 180).toLowerCase();
        const isInLibrary = libraryFilesSet.has(cleanedTitle);

        const card = document.createElement("div");
        card.className = "result-card";

        const thumbUrl = escapeHtml(item.thumbnail);
        const titleHtml = escapeHtml(item.title);
        const artistHtml = escapeHtml(item.channel);
        const durHtml = escapeHtml(item.duration_text);

        card.innerHTML = `
            <div class="thumb-wrapper">
                <img src="${thumbUrl}" onerror="this.src='https://via.placeholder.com/110x65?text=Music'" />
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
            prevBtn.innerHTML = '▶ Preview';
            prevBtn.onclick = () => toggleAudioStream(prevBtn, "api/preview?url=" + encodeURIComponent(item.url), 'search');

            const dlBtn = document.createElement('button');
            dlBtn.className = 'btn-download';
            dlBtn.setAttribute('data-id', item.id);
            dlBtn.innerHTML = '⬇️ Save';
            dlBtn.onclick = () => startDownload(item.url, item.title, item.id);

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

async function startDownload(url, title, elementId) {
    if (elementId) {
        const btn = document.querySelector(`button[data-id="${elementId}"]`);
        if (btn) { btn.disabled = true; btn.textContent = "⏳ Queued"; }
    }
    try {
        await fetch('api/download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url, title, elementId })
        });
        clearTimeout(pollTimer);
        pollTasks();
    } catch (e) { alert("Failed to enqueue download."); }
}

function updatePipelineStep(status) {
    const s1 = document.getElementById("stepDownload");
    const s2 = document.getElementById("stepProcess");
    const s3 = document.getElementById("stepDone");

    s1.className = "step-item"; s2.className = "step-item"; s3.className = "step-item";
    
    if (status === 'downloading') {
        s1.classList.add("active");
    } else if (status === 'processing') {
        s1.classList.add("completed"); s2.classList.add("active");
    } else if (status === 'completed') {
        s1.classList.add("completed"); s2.classList.add("completed"); s3.classList.add("completed");
    } else if (status === 'error') {
        s1.classList.add("active");
    }
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

        const activeOrQueued = tasks.filter(t => t.status === 'queued' || t.status === 'downloading' || t.status === 'processing');
        const recentlyErrored = tasks.filter(t => t.status === 'error' && (Date.now() - t.last_updated < 4000));
        const recentlyCompleted = tasks.filter(t => t.status === 'completed' && (Date.now() - t.last_updated < 2000));

        const displayTask = activeOrQueued.length > 0 ? activeOrQueued[0] : 
                            (recentlyErrored.length > 0 ? recentlyErrored[0] : 
                            (recentlyCompleted.length > 0 ? recentlyCompleted[0] : null));

        const panel = document.getElementById("progressPanel");

        if (!displayTask) {
            panel.style.display = "none";
            pollTimer = setTimeout(pollTasks, 2500);
            return;
        }

        panel.style.display = "block";
        document.getElementById("progressTitle").textContent = (displayTask.status === 'error' ? "❌ Failed: " : "Downloading: ") + displayTask.title;
        document.getElementById("progressPercent").textContent = Math.round(displayTask.percent) + "%";
        document.getElementById("progressFill").style.width = displayTask.percent + "%";
        document.getElementById("progressSpeed").textContent = displayTask.speed || "";
        document.getElementById("progressStatus").textContent = displayTask.error || displayTask.step || "Queued...";
        
        if (displayTask.status === "error") {
            document.getElementById("progressFill").style.background = "var(--danger)";
        } else {
            document.getElementById("progressFill").style.background = "linear-gradient(90deg, var(--accent) 0%, #a855f7 100%)";
        }

        updatePipelineStep(displayTask.status);

        const remaining = activeOrQueued.slice(1);
        const container = document.getElementById("queueBadgeContainer");
        
        if (remaining.length > 0) {
            let html = `<div class="queue-container"><div class="queue-header-title">📋 Queue / Downloading Active (${remaining.length})</div>`;
            remaining.forEach((item, idx) => { 
                const statusBadge = item.status === 'downloading' ? '⚡ Downloading' : '⏳ Waiting';
                html += `<div class="queue-item"><span class="queue-item-title">🎵 ${escapeHtml(item.title)}</span><span class="queue-item-badge">${statusBadge}</span></div>`; 
            });
            html += `</div>`;
            container.innerHTML = html;
        } else { 
            container.innerHTML = ""; 
        }

        pollTimer = setTimeout(pollTasks, 400);

    } catch (e) {
        pollTimer = setTimeout(pollTasks, 3000);
    }
}

async function loadLibrary() {
    const list = document.getElementById('libraryList');
    list.innerHTML = `<div class="status-msg">Loading library...</div>`;
    try {
        await refreshLibraryCache();
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
                <img src="${coverUrl}" onerror="this.onerror=null; this.src='${fallbackSvg}'" />
            </div>
            <div class="track-info">
                <div class="track-title">${escapeHtml(f.name)}</div>
                <div class="track-artist">📦 ${f.size}</div>
            </div>
            <div class="btn-group">
                <button class="btn-preview">▶ Play</button>
                <button class="btn-danger">🗑 Delete</button>
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
    for path in DOWNLOAD_DIR.iterdir():
        if path.is_file() and not path.name.startswith("."):
            if path.suffix.lower() in AUDIO_EXTENSIONS:
                sz = path.stat().st_size
                total_bytes += sz
                files.append({
                    "name": path.name,
                    "size": format_size(sz),
                    "bytes": sz
                })
    return {
        "files": sorted(files, key=lambda x: x["name"]),
        "total_size": format_size(total_bytes),
        "total_bytes": total_bytes
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
    element_id = payload.get("elementId", "")
    
    if not url:
        raise HTTPException(status_code=400, detail="Missing URL")

    task_id = uuid.uuid4().hex
    task_info = {
        "id": task_id,
        "title": title,
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


@app.get("/api/tasks")
async def get_tasks():
    now = time.time() * 1000
    for t in TASKS.values():
        if t["status"] in ["queued", "downloading", "processing"]:
            t["last_updated"] = now
            
    def sort_key(task):
        status_weight = {"downloading": 0, "processing": 1, "queued": 2, "completed": 3, "error": 4}
        return status_weight.get(task["status"], 99)
        
    sorted_tasks = sorted(list(TASKS.values()), key=sort_key)
    return sorted_tasks


@app.get("/health")
async def health():
    return {"status": "ok", "service": "music-downloader"}
