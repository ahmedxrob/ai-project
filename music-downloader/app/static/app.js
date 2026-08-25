/* =========================================
   GLOBAL STATE & STORAGE
========================================= */

let activeAudio = null;
let currentPlayingId = null;

let ws = null;
let reconnectTimer = null;
let wsHeartbeat = null;

let isSearching = false;
let currentSearchQuery = "";
let currentPage = 1;

let libraryCache = [];
let tasksCache = [];

let audioCtx = null;
let analyser = null;
let sourceNode = null;
let animationFrameId = null;

/* =========================================
   INITIALIZATION
========================================= */

document.addEventListener("DOMContentLoaded", () => {
    initTheme();
    initWebSocket();
    initGlobalPlayer();
    loadSettings();

    const queryInput = document.getElementById("query");
    if (queryInput) {
        queryInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter") triggerSearch();
        });
    }

    const searchBtn = document.getElementById("searchBtn");
    if (searchBtn) {
        searchBtn.addEventListener("click", triggerSearch);
    }

    window.addEventListener("scroll", handleInfiniteScroll);

    // Initial data fetch
    loadTasks();
    loadLibrary();
});

/* =========================================
   WEBSOCKET MANAGER
========================================= */

function initWebSocket() {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${protocol}//${window.location.host}/ws`;

    if (ws) {
        try { ws.close(); } catch (e) {}
    }

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
        console.log("WebSocket connected.");
        if (reconnectTimer) clearTimeout(reconnectTimer);
        
        // Keep-alive heartbeat
        wsHeartbeat = setInterval(() => {
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send("ping");
            }
        }, 20000);
    };

    ws.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            if (data.type === "task_update") {
                handleTaskUpdate(data.task);
            }
        } catch (e) {
            // Ignore non-json responses like pong
        }
    };

    ws.onclose = () => {
        if (wsHeartbeat) clearInterval(wsHeartbeat);
        reconnectTimer = setTimeout(initWebSocket, 3000);
    };

    ws.onerror = (err) => {
        console.error("WebSocket error:", err);
        ws.close();
    };
}

/* =========================================
   ROUTING & NAVIGATION
========================================= */

function navigate(tabName) {
    document.querySelectorAll(".tab-content").forEach((el) => {
        el.classList.remove("active");
    });
    
    document.querySelectorAll(".nav-link").forEach((el) => {
        el.classList.remove("active");
        el.setAttribute("aria-selected", "false");
    });

    const targetTab = document.getElementById(`tab-${tabName}`);
    if (targetTab) targetTab.classList.add("active");

    const activeBtn = document.getElementById(`btn-${tabName}`);
    const activeMobBtn = document.getElementById(`mob-btn-${tabName}`);

    if (activeBtn) {
        activeBtn.classList.add("active");
        activeBtn.setAttribute("aria-selected", "true");
    }
    if (activeMobBtn) {
        activeMobBtn.classList.add("active");
        activeMobBtn.setAttribute("aria-selected", "true");
    }

    if (tabName === "downloads") loadTasks();
    if (tabName === "library") loadLibrary();
    if (tabName === "settings") loadSettings();
}

/* =========================================
   TOAST NOTIFICATIONS
========================================= */

function showToast(message, type = "info") {
    const container = document.getElementById("toast-container");
    if (!container) return;

    const toast = document.createElement("div");
    toast.className = `toast toast-${type}`;
    toast.innerText = message;

    container.appendChild(toast);

    setTimeout(() => {
        toast.style.opacity = "0";
        setTimeout(() => toast.remove(), 300);
    }, 3500);
}

function requestNotificationPermission() {
    if (!("Notification" in window)) {
        showToast("Desktop notifications are not supported by your browser.", "error");
        return;
    }
    Notification.requestPermission().then((permission) => {
        if (permission === "granted") {
            showToast("Desktop notifications enabled!", "success");
        } else {
            showToast("Notification permission denied.", "error");
        }
    });
}

function sendDesktopNotification(title, body) {
    if ("Notification" in window && Notification.permission === "granted") {
        new Notification(title, { body, icon: "/static/favicon.ico" });
    }
}

/* =========================================
   SEARCH & INFINITE SCROLL
========================================= */

async function triggerSearch() {
    const input = document.getElementById("query");
    if (!input) return;

    const query = input.value.trim();
    if (!query) {
        showToast("Please enter a search query.", "error");
        return;
    }

    currentSearchQuery = query;
    currentPage = 1;
    document.getElementById("results").innerHTML = "";
    document.getElementById("statusMsg").innerText = "";

    await performSearch(false);
}

async function performSearch(isAppend = false) {
    if (isSearching) return;
    isSearching = true;

    const loader = document.getElementById("infiniteLoader");
    const statusMsg = document.getElementById("statusMsg");

    if (isAppend && loader) loader.style.display = "block";
    if (!isAppend && statusMsg) statusMsg.innerText = "🔍 Searching YouTube...";

    try {
        const resp = await fetch(`/api/search?q=${encodeURIComponent(currentSearchQuery)}&page=${currentPage}`);
        if (!resp.ok) throw new Error("Failed to fetch search results.");

        const tracks = await resp.json();

        if (statusMsg) statusMsg.innerText = "";
        if (loader) loader.style.display = "none";

        if (!tracks || tracks.length === 0) {
            if (!isAppend && statusMsg) statusMsg.innerText = "No tracks found.";
            isSearching = false;
            return;
        }

        renderSearchResults(tracks, isAppend);
    } catch (err) {
        if (statusMsg) statusMsg.innerText = "Search failed: " + err.message;
        if (loader) loader.style.display = "none";
        showToast("Search failed: " + err.message, "error");
    } finally {
        isSearching = false;
    }
}

function handleInfiniteScroll() {
    const searchTab = document.getElementById("tab-search");
    if (!searchTab || !searchTab.classList.contains("active") || !currentSearchQuery || isSearching) {
        return;
    }

    if (window.innerHeight + window.scrollY >= document.body.offsetHeight - 400) {
        currentPage++;
        performSearch(true);
    }
}

function renderSearchResults(tracks, isAppend) {
    const container = document.getElementById("results");
    if (!container) return;

    const fragment = document.createDocumentFragment();

    tracks.forEach((track) => {
        const card = document.createElement("div");
        card.className = "result-card";
        card.id = `search-item-${track.id}`;

        card.innerHTML = `
            <div class="thumb-wrapper" onclick="playPreview('${track.id}', '${escapeHtml(track.title)}', '${escapeHtml(track.channel)}', '${track.thumbnail}')">
                <img src="${track.thumbnail}" loading="lazy" alt="Cover" />
                <span class="badge-duration">${track.duration_text || '0:00'}</span>
            </div>
            <div class="track-info">
                <div class="track-title" title="${escapeHtml(track.title)}">${escapeHtml(track.title)}</div>
                <div class="track-artist">${escapeHtml(track.channel)}</div>
            </div>
            <div class="btn-group">
                <button class="btn-preview" id="prev-btn-${track.id}" onclick="playPreview('${track.id}', '${escapeHtml(track.title)}', '${escapeHtml(track.channel)}', '${track.thumbnail}')">
                    ▶ Preview
                </button>
                <button class="btn-download" id="dl-btn-${track.id}" onclick="enqueueDownload('${track.url}', '${escapeHtml(track.title)}', '${escapeHtml(track.channel)}', '${track.id}')">
                    ⬇️ Download
                </button>
            </div>
        `;
        fragment.appendChild(card);
    });

    if (!isAppend) container.innerHTML = "";
    container.appendChild(fragment);
}

/* =========================================
   PREVIEW & GLOBAL AUDIO PLAYER
========================================= */

function initGlobalPlayer() {
    const audio = document.getElementById("global-audio-element");
    const playBtn = document.getElementById("gp-play-btn");
    const seekInput = document.getElementById("gp-seek");
    const volumeInput = document.getElementById("gp-volume");

    if (!audio) return;

    playBtn.addEventListener("click", togglePlayPause);

    audio.addEventListener("timeupdate", () => {
        if (!isNaN(audio.duration) && audio.duration > 0) {
            seekInput.value = (audio.currentTime / audio.duration) * 100;
            document.getElementById("gp-cur-time").innerText = formatDuration(audio.currentTime);
            document.getElementById("gp-dur-time").innerText = formatDuration(audio.duration);
        }
    });

    seekInput.addEventListener("input", () => {
        if (!isNaN(audio.duration) && audio.duration > 0) {
            audio.currentTime = (seekInput.value / 100) * audio.duration;
        }
    });

    volumeInput.addEventListener("input", (e) => {
        audio.volume = e.target.value;
    });

    audio.addEventListener("ended", () => {
        playBtn.innerText = "▶";
        resetPreviewButtons();
    });

    audio.addEventListener("pause", () => {
        playBtn.innerText = "▶";
        resetPreviewButtons();
    });

    audio.addEventListener("play", () => {
        playBtn.innerText = "⏸";
        setupVisualizer();
    });
}

async function playPreview(trackId, title, artist, artUrl) {
    const audio = document.getElementById("global-audio-element");
    const playerBar = document.getElementById("global-player-bar");

    if (currentPlayingId === trackId && !audio.paused) {
        audio.pause();
        return;
    }

    resetPreviewButtons();
    const btn = document.getElementById(`prev-btn-${trackId}`);
    if (btn) {
        btn.classList.add("playing");
        btn.innerText = "⏳ Loading...";
    }

    try {
        const previewUrl = `/api/preview?url=${encodeURIComponent(trackId)}`;
        audio.src = previewUrl;

        document.getElementById("gp-title").innerText = title;
        document.getElementById("gp-artist").innerText = artist;
        document.getElementById("gp-art").src = artUrl || "https://via.placeholder.com/46?text=🎵";
        playerBar.style.display = "flex";

        await audio.play();
        currentPlayingId = trackId;
        if (btn) btn.innerText = "⏸ Pause";
    } catch (err) {
        showToast("Failed to load audio preview.", "error");
        resetPreviewButtons();
    }
}

function playLocalTrack(filePath, title, artist, coverUrl) {
    const audio = document.getElementById("global-audio-element");
    const playerBar = document.getElementById("global-player-bar");

    resetPreviewButtons();

    audio.src = `/api/library/stream/${encodeURIComponent(filePath)}`;
    document.getElementById("gp-title").innerText = title;
    document.getElementById("gp-artist").innerText = artist || "Local Track";
    document.getElementById("gp-art").src = coverUrl || `/api/library/cover/${encodeURIComponent(filePath)}`;
    playerBar.style.display = "flex";

    audio.play();
    currentPlayingId = filePath;
}

function togglePlayPause() {
    const audio = document.getElementById("global-audio-element");
    if (!audio.src) return;

    if (audio.paused) {
        audio.play();
    } else {
        audio.pause();
    }
}

function resetPreviewButtons() {
    document.querySelectorAll(".btn-preview").forEach((b) => {
        b.classList.remove("playing");
        b.innerText = "▶ Preview";
    });
}

/* =========================================
   AUDIO VISUALIZER (Web Audio API)
========================================= */

function setupVisualizer() {
    const audio = document.getElementById("global-audio-element");
    const canvas = document.getElementById("visualizer-canvas");
    if (!canvas || !audio) return;

    const ctx = canvas.getContext("2d");

    if (!audioCtx) {
        audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        analyser = audioCtx.createAnalyser();
        analyser.fftSize = 64;
        sourceNode = audioCtx.createMediaElementSource(audio);
        sourceNode.connect(analyser);
        analyser.connect(audioCtx.destination);
    }

    if (audioCtx.state === "suspended") {
        audioCtx.resume();
    }

    const bufferLength = analyser.frequencyBinCount;
    const dataArray = new Uint8Array(bufferLength);

    function renderFrame() {
        animationFrameId = requestAnimationFrame(renderFrame);
        analyser.getByteFrequencyData(dataArray);

        ctx.clearRect(0, 0, canvas.width, canvas.height);
        const barWidth = (canvas.width / bufferLength) * 1.5;
        let x = 0;

        for (let i = 0; i < bufferLength; i++) {
            const barHeight = (dataArray[i] / 255) * canvas.height;
            ctx.fillStyle = "#8b5cf6";
            ctx.fillRect(x, canvas.height - barHeight, barWidth - 1, barHeight);
            x += barWidth + 1;
        }
    }

    if (animationFrameId) cancelAnimationFrame(animationFrameId);
    renderFrame();
}

/* =========================================
   DOWNLOAD ENQUEUE & MANAGEMENT
========================================= */

async function enqueueDownload(url, title, artist, elementId) {
    const btn = document.getElementById(`dl-btn-${elementId}`);
    if (btn) {
        btn.disabled = true;
        btn.innerText = "⏳ Queuing...";
    }

    try {
        const resp = await fetch("/api/download", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url, title, artist, elementId })
        });

        const data = await resp.json();

        if (!resp.ok) {
            throw new Error(data.detail || "Download request failed.");
        }

        showToast(`Queued: ${title}`, "success");
        if (btn) btn.innerText = "✓ Queued";
        loadTasks();
    } catch (err) {
        showToast(err.message, "error");
        if (btn) {
            btn.disabled = false;
            btn.innerText = "⬇️ Download";
        }
    }
}

async function cancelTask(taskId) {
    try {
        const resp = await fetch(`/api/tasks/${taskId}/cancel`, { method: "POST" });
        if (resp.ok) {
            showToast("Download cancelled.", "info");
            loadTasks();
        }
    } catch (err) {
        showToast("Failed to cancel download.", "error");
    }
}

/* =========================================
   DOWNLOAD TASKS & WEBSOCKET UPDATES
========================================= */

async function loadTasks() {
    try {
        const resp = await fetch("/api/tasks");
        if (!resp.ok) return;
        tasksCache = await resp.json();
        renderActivePanel();
        renderDownloadsTab();
    } catch (e) {
        console.error("Error loading tasks:", e);
    }
}

function handleTaskUpdate(updatedTask) {
    const index = tasksCache.findIndex((t) => t.id === updatedTask.id);
    if (index !== -1) {
        tasksCache[index] = updatedTask;
    } else {
        tasksCache.push(updatedTask);
    }

    renderActivePanel();
    
    const downloadsTab = document.getElementById("tab-downloads");
    if (downloadsTab && downloadsTab.classList.contains("active")) {
        renderDownloadsTab();
    }

    if (updatedTask.status === "completed" && updatedTask.percent === 100) {
        sendDesktopNotification("Download Complete!", updatedTask.title);
        loadLibrary();
    }
}

function renderActivePanel() {
    const panel = document.getElementById("progressPanel");
    const container = document.getElementById("activeDownloadsList");
    const queueBadge = document.getElementById("queueCount");

    if (!panel || !container) return;

    const activeTasks = tasksCache.filter((t) =>
        ["queued", "downloading", "processing"].includes(t.status)
    );

    if (queueBadge) queueBadge.innerText = activeTasks.length;

    if (activeTasks.length === 0) {
        panel.style.display = "none";
        container.innerHTML = "";
        return;
    }

    panel.style.display = "block";
    container.innerHTML = activeTasks.map((t) => `
        <div style="display:flex; flex-direction:column; gap:6px;">
            <div style="display:flex; justify-content:space-between; font-size:0.85rem; font-weight:700;">
                <span style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:70%;">${escapeHtml(t.title)}</span>
                <span style="color:var(--accent);">${t.percent ? t.percent.toFixed(1) : 0}%</span>
            </div>
            <progress value="${t.percent || 0}" max="100"></progress>
            <div style="display:flex; justify-content:space-between; font-size:0.75rem; color:var(--text-secondary);">
                <span>${escapeHtml(t.step || "Processing...")} ${t.speed ? `(${t.speed})` : ""}</span>
                <button class="btn-danger" style="padding:2px 8px; font-size:0.7rem;" onclick="cancelTask('${t.id}')">Cancel</button>
            </div>
        </div>
    `).join("");
}

function renderDownloadsTab() {
    const container = document.getElementById("downloadsList");
    if (!container) return;

    if (tasksCache.length === 0) {
        container.innerHTML = `<div class="status-msg">No active or recent downloads.</div>`;
        return;
    }

    container.innerHTML = tasksCache.map((t) => `
        <div class="result-card">
            <div class="track-info">
                <div class="track-title">${escapeHtml(t.title)}</div>
                <div class="track-artist">${escapeHtml(t.artist)} · <strong style="color:var(--accent);">${t.status.toUpperCase()}</strong></div>
                <div style="font-size:0.75rem; color:var(--text-muted); margin-top:4px;">${escapeHtml(t.step || "")}</div>
            </div>
            <div class="btn-group">
                ${["queued", "downloading", "processing"].includes(t.status) ? 
                    `<button class="btn-danger" onclick="cancelTask('${t.id}')">Cancel</button>` : 
                    `<span style="font-size:0.8rem; color:var(--text-secondary);">${t.status === 'completed' ? '✓ Finished' : '❌ Failed'}</span>`
                }
            </div>
        </div>
    `).join("");
}

/* =========================================
   LIBRARY MANAGEMENT
========================================= */

async function loadLibrary() {
    try {
        const [libResp, statsResp] = await Promise.all([
            fetch("/api/library"),
            fetch("/api/stats")
        ]);

        if (libResp.ok) {
            const data = await libResp.json();
            libraryCache = data.files || [];
            document.getElementById("libFolderSize").innerText = data.total_size || "0 MB";
            document.getElementById("libCountDetail").innerText = libraryCache.length;
            document.getElementById("sideLibCount").innerText = libraryCache.length;
            document.getElementById("mobLibCount").innerText = libraryCache.length;
            filterLibrary();
        }

        if (statsResp.ok) {
            const stats = await statsResp.json();
            document.getElementById("statTracks").innerText = stats.tracks;
            document.getElementById("statArtists").innerText = stats.artists;
            document.getElementById("statAlbums").innerText = stats.albums;
        }
    } catch (err) {
        showToast("Failed to load library data.", "error");
    }
}

function filterLibrary() {
    const queryInput = document.getElementById("libSearchQuery");
    const query = queryInput ? queryInput.value.toLowerCase().trim() : "";
    const container = document.getElementById("libraryList");

    if (!container) return;

    const filtered = libraryCache.filter((f) => f.name.toLowerCase().includes(query));

    if (filtered.length === 0) {
        container.innerHTML = `<div class="status-msg">No local tracks match your filter.</div>`;
        return;
    }

    container.innerHTML = filtered.map((f) => {
        const fileName = f.name.split("/").pop();
        const artist = f.name.includes("/") ? f.name.split("/")[0] : "Local Track";
        const coverUrl = `/api/library/cover/${encodeURIComponent(f.name)}`;

        return `
            <div class="result-card">
                <div class="thumb-wrapper" onclick="playLocalTrack('${escapeHtml(f.name)}', '${escapeHtml(fileName)}', '${escapeHtml(artist)}', '${coverUrl}')">
                    <img src="${coverUrl}" loading="lazy" onerror="this.src='https://via.placeholder.com/58?text=🎵'" alt="Cover" />
                </div>
                <div class="track-info">
                    <div class="track-title" title="${escapeHtml(fileName)}">${escapeHtml(fileName)}</div>
                    <div class="track-artist">${escapeHtml(artist)} · ${f.size}</div>
                </div>
                <div class="btn-group">
                    <button class="btn-preview" onclick="playLocalTrack('${escapeHtml(f.name)}', '${escapeHtml(fileName)}', '${escapeHtml(artist)}', '${coverUrl}')">▶ Play</button>
                    <button class="btn-danger" onclick="deleteLibraryFile('${escapeHtml(f.name)}')">🗑 Delete</button>
                </div>
            </div>
        `;
    }).join("");
}

async function deleteLibraryFile(filename) {
    if (!confirm(`Are you sure you want to delete "${filename}"?`)) return;

    try {
        const resp = await fetch(`/api/library/${encodeURIComponent(filename)}`, { method: "DELETE" });
        if (resp.ok) {
            showToast("File deleted successfully.", "success");
            loadLibrary();
        } else {
            throw new Error("Deletion failed.");
        }
    } catch (err) {
        showToast("Failed to delete file.", "error");
    }
}

/* =========================================
   SETTINGS & THEME
========================================= */

function initTheme() {
    const savedTheme = localStorage.getItem("xrob_theme") || "dark";
    document.documentElement.setAttribute("data-theme", savedTheme);
    const select = document.getElementById("set_theme");
    if (select) select.value = savedTheme;
}

function toggleTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("xrob_theme", theme);
}

async function loadSettings() {
    try {
        const resp = await fetch("/api/settings");
        if (!resp.ok) return;

        const settings = await resp.json();

        setValue("set_format", settings.audio_format);
        setValue("set_quality", settings.audio_quality);
        setValue("set_max_results", settings.max_results);
        setValue("set_navidrome_url", settings.navidrome_url);
        setValue("set_navidrome_user", settings.navidrome_user);
        setValue("set_navidrome_token", settings.navidrome_token);

        setChecked("set_thumb", settings.embed_thumbnail);
        setChecked("set_meta", settings.embed_metadata);
        setChecked("set_organize", settings.organize_by_artist);
    } catch (e) {
        console.error("Failed to load settings:", e);
    }
}

async function saveSettings() {
    const payload = {
        audio_format: getValue("set_format"),
        audio_quality: getValue("set_quality"),
        max_results: parseInt(getValue("set_max_results") || 20),
        navidrome_url: getValue("set_navidrome_url"),
        navidrome_user: getValue("set_navidrome_user"),
        navidrome_token: getValue("set_navidrome_token"),
        embed_thumbnail: getChecked("set_thumb"),
        embed_metadata: getChecked("set_meta"),
        organize_by_artist: getChecked("set_organize")
    };

    try {
        const resp = await fetch("/api/settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });

        if (resp.ok) {
            const msg = document.getElementById("settingsMsg");
            if (msg) msg.innerText = "✓ Settings saved successfully!";
            showToast("Settings saved!", "success");
            setTimeout(() => { if (msg) msg.innerText = ""; }, 3000);
        }
    } catch (err) {
        showToast("Failed to save settings.", "error");
    }
}

/* =========================================
   UTILITIES
========================================= */

function getValue(id) {
    const el = document.getElementById(id);
    return el ? el.value : "";
}

function setValue(id, val) {
    const el = document.getElementById(id);
    if (el && val !== undefined) el.value = val;
}

function getChecked(id) {
    const el = document.getElementById(id);
    return el ? el.checked : false;
}

function setChecked(id, val) {
    const el = document.getElementById(id);
    if (el && val !== undefined) el.checked = !!val;
}

function formatDuration(seconds) {
    const sec = Math.floor(seconds || 0);
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return `${m}:${s < 10 ? "0" : ""}${s}`;
}

function escapeHtml(str) {
    if (!str) return "";
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}
