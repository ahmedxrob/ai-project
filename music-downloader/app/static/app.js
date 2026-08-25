let pollTimer = null;
let completedSet = new Set();
let libraryFilesSet = new Set();
let rawLibraryFiles = [];
let activePreviewBtn = null;

let currentPage = 1;
let currentQuery = "";
let isLoadingMore = false;
let hasMoreResults = true;

// Persistent Global Player & Audio Visualizer Variables
const globalAudio = document.getElementById("global-audio-element");
const gpBar = document.getElementById("global-player-bar");
const gpPlayBtn = document.getElementById("gp-play-btn");
const gpSeek = document.getElementById("gp-seek");
const gpVolume = document.getElementById("gp-volume");
const gpCurTime = document.getElementById("gp-cur-time");
const gpDurTime = document.getElementById("gp-dur-time");
const gpTitle = document.getElementById("gp-title");
const gpArtist = document.getElementById("gp-artist");
const gpArt = document.getElementById("gp-art");

let audioCtx = null;
let analyser = null;
let sourceNode = null;
let visualizerAnimationFrame = null;
const canvas = document.getElementById('visualizer-canvas');
const canvasCtx = canvas ? canvas.getContext('2d') : null;

function initAudioContext() {
    if (audioCtx) return;
    try {
        audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        analyser = audioCtx.createAnalyser();
        analyser.fftSize = 64;
        sourceNode = audioCtx.createMediaElementSource(globalAudio);
        sourceNode.connect(analyser);
        analyser.connect(audioCtx.destination);
        drawVisualizer();
    } catch (e) {}
}

function drawVisualizer() {
    if (!analyser || !canvasCtx) return;
    visualizerAnimationFrame = requestAnimationFrame(drawVisualizer);
    const bufferLength = analyser.frequencyBinCount;
    const dataArray = new Uint8Array(bufferLength);
    analyser.getByteFrequencyData(dataArray);

    canvasCtx.clearRect(0, 0, canvas.width, canvas.height);
    const barWidth = (canvas.width / bufferLength) * 1.6;
    let x = 0;

    for (let i = 0; i < bufferLength; i++) {
        const barHeight = (dataArray[i] / 255) * canvas.height;
        canvasCtx.fillStyle = '#6366f1';
        canvasCtx.fillRect(x, canvas.height - barHeight, barWidth - 1, barHeight);
        x += barWidth;
    }
}

function formatSecs(sec) {
    sec = Math.floor(sec || 0);
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return `${m}:${s < 10 ? '0' : ''}${s}`;
}

globalAudio.ontimeupdate = () => {
    if (!globalAudio.duration) return;
    gpSeek.value = (globalAudio.currentTime / globalAudio.duration) * 100;
    gpCurTime.textContent = formatSecs(globalAudio.currentTime);
    gpDurTime.textContent = formatSecs(globalAudio.duration);
};

globalAudio.onended = () => {
    gpPlayBtn.textContent = '▶';
    if (activePreviewBtn) {
        activePreviewBtn.classList.remove('playing');
        activePreviewBtn.innerHTML = activePreviewBtn.dataset.type === 'library' ? `▶ Play` : `▶ Preview`;
    }
};

gpPlayBtn.onclick = () => {
    initAudioContext();
    if (audioCtx && audioCtx.state === 'suspended') audioCtx.resume();
    if (globalAudio.paused) {
        globalAudio.play();
        gpPlayBtn.textContent = '⏸';
        if (activePreviewBtn) activePreviewBtn.classList.add('playing');
    } else {
        globalAudio.pause();
        gpPlayBtn.textContent = '▶';
        if (activePreviewBtn) activePreviewBtn.classList.remove('playing');
    }
};

gpSeek.oninput = () => {
    if (globalAudio.duration) {
        globalAudio.currentTime = (gpSeek.value / 100) * globalAudio.duration;
    }
};

gpVolume.oninput = () => {
    globalAudio.volume = gpVolume.value;
};

// WebSocket Real-time Updates Setup
let socket = null;
function initWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    socket = new WebSocket(`${protocol}//${window.location.host}/ws`);

    socket.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            if (data.type === 'task_update') {
                pollTasks();
            }
        } catch(e) {}
    };

    socket.onclose = () => {
        setTimeout(initWebSocket, 3000);
    };
}

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

function toggleTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('xrob_music_theme', theme);
}

const savedTheme = localStorage.getItem('xrob_music_theme') || 'dark';
toggleTheme(savedTheme);

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
        document.getElementById('set_navidrome_url').value = s.navidrome_url || '';
        document.getElementById('set_navidrome_user').value = s.navidrome_user || '';
        document.getElementById('set_navidrome_token').value = s.navidrome_token || '';
    } catch(e) {}
}

async function saveSettings() {
    const data = {
        audio_format: document.getElementById('set_format').value,
        audio_quality: document.getElementById('set_quality').value,
        embed_thumbnail: document.getElementById('set_thumb').checked,
        embed_metadata: document.getElementById('set_meta').checked,
        max_results: parseInt(document.getElementById('set_max_results').value) || 20,
        organize_by_artist: document.getElementById('set_organize').checked,
        navidrome_url: document.getElementById('set_navidrome_url').value,
        navidrome_user: document.getElementById('set_navidrome_user').value,
        navidrome_token: document.getElementById('set_navidrome_token').value
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
    if (!globalAudio.paused) {
        globalAudio.pause();
    }
    gpPlayBtn.textContent = '▶';
    if (activePreviewBtn) {
        activePreviewBtn.classList.remove('playing');
        activePreviewBtn.innerHTML = activePreviewBtn.dataset.type === 'library' ? `▶ Play` : `▶ Preview`;
        activePreviewBtn = null;
    }
}

function toggleAudioStream(btn, streamUrl, type = 'search', title = 'Track', artist = 'Artist', artUrl = '') {
    initAudioContext();
    if (audioCtx && audioCtx.state === 'suspended') audioCtx.resume();

    if (activePreviewBtn === btn && !globalAudio.paused) {
        globalAudio.pause();
        btn.classList.remove('playing');
        btn.innerHTML = type === 'library' ? `▶ Play` : `▶ Preview`;
        gpPlayBtn.textContent = '▶';
        return;
    }

    if (activePreviewBtn) {
        activePreviewBtn.classList.remove('playing');
        activePreviewBtn.innerHTML = activePreviewBtn.dataset.type === 'library' ? `▶ Play` : `▶ Preview`;
    }

    btn.dataset.type = type;
    activePreviewBtn = btn;
    btn.innerHTML = `⏳ Loading...`;

    gpTitle.textContent = title;
    gpArtist.textContent = artist;
    gpArt.src = artUrl || 'https://via.placeholder.com/46?text=🎵';
    gpBar.style.display = 'flex';

    globalAudio.src = streamUrl;
    globalAudio.play().then(() => {
        btn.classList.add('playing');
        btn.innerHTML = `⏸ Pause`;
        gpPlayBtn.textContent = '⏸';
    }).catch(err => {
        btn.innerHTML = `❌ Error`;
        setTimeout(() => { btn.innerHTML = type === 'library' ? `▶ Play` : `▶ Preview`; }, 2000);
    });
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
            prevBtn.onclick = () => toggleAudioStream(prevBtn, "api/preview?url=" + encodeURIComponent(item.url), 'search', item.title, item.channel, item.thumbnail);

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

    } catch (e) {}
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

async function clearDoneTasks() {
    try {
        await fetch('api/tasks/clear-completed', { method: 'DELETE' });
        loadDownloads();
    } catch(e) {
        alert("Failed to clear finished downloads.");
    }
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
        playBtn.onclick = () => toggleAudioStream(playBtn, streamUrl, 'library', f.name, 'Local Library', coverUrl);

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
    initWebSocket();
});
