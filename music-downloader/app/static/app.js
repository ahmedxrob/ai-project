(() => {
    "use strict";

    /* =========================================================
       API ROUTES & STATE MANAGEMENT
       ========================================================= */

    const API = {
        search: "/api/search",
        settings: "/api/settings",
        library: "/api/library",
        tasks: "/api/tasks",
        download: "/api/download",
        preview: "/api/preview",
        stats: "/api/stats"
    };

    let socket = null;
    let pollTimer = null;
    let rawLibraryFiles = [];
    let libraryNormalizedSet = new Set();
    let currentPreviewBtn = null;

    /* DOM Cache */
    const $ = (id) => document.getElementById(id);
    const globalAudio = $("global-audio-element");
    const gpBar = $("global-player-bar");
    const gpPlayBtn = $("gp-play-btn");
    const gpSeek = $("gp-seek");
    const gpVolume = $("gp-volume");
    const gpCurTime = $("gp-cur-time");
    const gpDurTime = $("gp-dur-time");
    const gpTitle = $("gp-title");
    const gpArtist = $("gp-artist");
    const gpArt = $("gp-art");

    /* =========================================================
       UTILITY & HELPER FUNCTIONS
       ========================================================= */

    function escapeHtml(val) {
        return String(val ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function formatSecs(sec) {
        sec = Math.floor(Number(sec) || 0);
        const m = Math.floor(sec / 60);
        const s = sec % 60;
        return `${m}:${String(s).padStart(2, "0")}`;
    }

    function normalizeKey(value) {
        return String(value || "")
            .normalize("NFKD")
            .replace(/[\u0300-\u036f]/g, "")
            .toLowerCase()
            .replace(/\b(official|video|audio|music|lyrics?|hd|4k|remastered?)\b/gi, " ")
            .replace(/[^a-z0-9]+/g, "")
            .trim();
    }

    function isDuplicate(title) {
        const key = normalizeKey(title);
        if (!key) return false;
        for (const item of libraryNormalizedSet) {
            if (item === key || item.includes(key) || key.includes(item)) return true;
        }
        return false;
    }

    function showToast(message, type = "normal") {
        let container = $("toast-container");
        if (!container) {
            container = document.createElement("div");
            container.id = "toast-container";
            document.body.appendChild(container);
        }
        const toast = document.createElement("div");
        toast.className = `status-msg ${type}`;
        toast.style.margin = "8px 0";
        toast.textContent = message;
        container.appendChild(toast);
        setTimeout(() => toast.remove(), 3500);
    }

    /* =========================================================
       NAVIGATION
       ========================================================= */

    function navigate(tabName) {
        const tabs = ["search", "downloads", "library", "settings"];
        tabs.forEach(tab => {
            const el = $(`tab-${tab}`);
            const btn = $(`btn-${tab}`);
            const mobBtn = $(`mob-btn-${tab}`);

            if (el) el.classList.toggle("active", tab === tabName);
            if (btn) btn.classList.toggle("active", tab === tabName);
            if (mobBtn) mobBtn.classList.toggle("active", tab === tabName);
        });

        if (tabName === "library") loadLibrary();
        if (tabName === "downloads") loadTasks();
        if (tabName === "settings") loadSettings();
    }

    /* =========================================================
       SEARCH
       ========================================================= */

    async function performSearch() {
        const input = $("searchQuery");
        const query = input ? input.value.trim() : "";
        if (!query) return;

        const container = $("searchResults");
        if (container) container.innerHTML = `<div style="text-align:center; padding: 40px;">Searching...</div>`;

        try {
            const res = await fetch(`${API.search}?q=${encodeURIComponent(query)}`);
            if (!res.ok) throw new Error("Search request failed");
            const data = await res.json();
            renderSearchResults(data);
        } catch (err) {
            if (container) container.innerHTML = `<div style="text-align:center; padding: 40px; color: var(--danger, #ff4d4d);">Error: ${escapeHtml(err.message)}</div>`;
        }
    }

    function renderSearchResults(results) {
        const container = $("searchResults");
        if (!container) return;
        if (!results || results.length === 0) {
            container.innerHTML = `<div style="text-align:center; padding: 40px;">No results found.</div>`;
            return;
        }

        container.innerHTML = results.map(item => {
            const inLib = isDuplicate(item.title);
            return `
                <div class="result-card">
                    <div class="thumb-wrapper">
                        <img src="${escapeHtml(item.thumbnail)}" alt="Art" />
                        <span class="badge-duration">${escapeHtml(item.duration_text)}</span>
                    </div>
                    <div class="track-info">
                        <div class="track-title" title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</div>
                        <div class="track-artist">${escapeHtml(item.channel)}</div>
                    </div>
                    <div class="btn-group">
                        <button class="btn-preview" onclick="playPreview('${escapeHtml(item.url)}', '${escapeHtml(item.title)}', '${escapeHtml(item.channel)}', '${escapeHtml(item.thumbnail)}', this)">Preview</button>
                        <button class="btn-download" ${inLib ? "disabled" : ""} onclick="enqueueDownload('${escapeHtml(item.url)}', '${escapeHtml(item.title)}', '${escapeHtml(item.channel)}')">
                            ${inLib ? "In Library" : "Download"}
                        </button>
                    </div>
                </div>
            `;
        }).join("");
    }

    /* =========================================================
       AUDIO PLAYER & PREVIEW
       ========================================================= */

    async function playPreview(url, title, artist, art, btn) {
        if (!globalAudio) return;

        if (currentPreviewBtn === btn && !globalAudio.paused) {
            globalAudio.pause();
            btn.classList.remove("playing");
            btn.textContent = "Preview";
            return;
        }

        if (currentPreviewBtn && currentPreviewBtn !== btn) {
            currentPreviewBtn.classList.remove("playing");
            currentPreviewBtn.textContent = "Preview";
        }

        currentPreviewBtn = btn;
        if (btn) {
            btn.classList.add("playing");
            btn.textContent = "Loading...";
        }

        if (gpBar) gpBar.style.display = "grid";
        if (gpTitle) gpTitle.textContent = title;
        if (gpArtist) gpArtist.textContent = artist;
        if (gpArt) gpArt.src = art || "";

        globalAudio.src = `${API.preview}?url=${encodeURIComponent(url)}`;
        try {
            await globalAudio.play();
            if (btn) btn.textContent = "Pause";
            if (gpPlayBtn) gpPlayBtn.textContent = "❚❚";
        } catch (e) {
            showToast("Failed to stream audio preview", "error");
            if (btn) {
                btn.classList.remove("playing");
                btn.textContent = "Preview";
            }
        }
    }

    function setupAudioEvents() {
        if (!globalAudio) return;

        globalAudio.addEventListener("timeupdate", () => {
            if (gpCurTime) gpCurTime.textContent = formatSecs(globalAudio.currentTime);
            if (gpDurTime && !isNaN(globalAudio.duration)) gpDurTime.textContent = formatSecs(globalAudio.duration);
            if (gpSeek && !isNaN(globalAudio.duration)) {
                gpSeek.value = (globalAudio.currentTime / globalAudio.duration) * 100;
            }
        });

        globalAudio.addEventListener("ended", () => {
            if (gpPlayBtn) gpPlayBtn.textContent = "▶";
            if (currentPreviewBtn) {
                currentPreviewBtn.classList.remove("playing");
                currentPreviewBtn.textContent = "Preview";
            }
        });

        if (gpPlayBtn) {
            gpPlayBtn.addEventListener("click", () => {
                if (globalAudio.paused) {
                    globalAudio.play();
                    gpPlayBtn.textContent = "❚❚";
                    if (currentPreviewBtn) currentPreviewBtn.textContent = "Pause";
                } else {
                    globalAudio.pause();
                    gpPlayBtn.textContent = "▶";
                    if (currentPreviewBtn) currentPreviewBtn.textContent = "Preview";
                }
            });
        }

        if (gpSeek) {
            gpSeek.addEventListener("input", () => {
                if (!isNaN(globalAudio.duration)) {
                    globalAudio.currentTime = (gpSeek.value / 100) * globalAudio.duration;
                }
            });
        }

        if (gpVolume) {
            gpVolume.addEventListener("input", () => {
                globalAudio.volume = gpVolume.value;
            });
        }
    }

    /* =========================================================
       TASKS & QUEUE MANAGEMENT
       ========================================================= */

    async function enqueueDownload(url, title, artist) {
        try {
            const res = await fetch(API.download, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ url, title, artist })
            });

            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || "Download failed to enqueue");

            showToast(`Queued: ${title}`, "success");
            loadTasks();
        } catch (err) {
            showToast(err.message, "error");
        }
    }

    async function loadTasks() {
        try {
            const res = await fetch(API.tasks);
            if (!res.ok) return;
            const tasks = await res.json();
            renderTasks(tasks);
        } catch (e) {}
    }

    function renderTasks(tasks) {
        const container = $("downloadList");
        if (!container) return;

        if (!tasks || tasks.length === 0) {
            container.innerHTML = `<div style="text-align:center; padding: 40px; color: var(--text-muted, #888);">Queue is empty.</div>`;
            return;
        }

        container.innerHTML = tasks.map(task => `
            <div class="queue-item">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom: 6px;">
                    <div class="track-title">${escapeHtml(task.title || "Unknown Task")}</div>
                    <span class="eyebrow">${escapeHtml(task.status)}</span>
                </div>
                <div style="font-size:0.75rem; color: var(--text-secondary, #aaa); margin-bottom: 8px;">
                    ${escapeHtml(task.step || "")} ${task.speed ? `(${escapeHtml(task.speed)})` : ""}
                </div>
                <div style="background: var(--input-bg, #222); height: 6px; border-radius: 3px; overflow: hidden;">
                    <div style="width: ${task.percent || 0}%; background: var(--accent, #007acc); height: 100%; transition: width 0.3s;"></div>
                </div>
                ${task.status === "downloading" || task.status === "queued" ? `
                    <button class="btn-danger" style="margin-top:8px;" onclick="cancelTask('${task.id}')">Cancel Task</button>
                ` : ""}
            </div>
        `).join("");
    }

    async function cancelTask(taskId) {
        try {
            await fetch(`/api/tasks/${taskId}/cancel`, { method: "POST" });
            loadTasks();
        } catch (e) {}
    }

    async function clearCompletedDownloads() {
        loadTasks();
    }

    /* =========================================================
       LIBRARY MANAGEMENT
       ========================================================= */

    async function loadLibrary() {
        try {
            const [libRes, statsRes] = await Promise.all([
                fetch(API.library),
                fetch(API.stats)
            ]);

            if (libRes.ok) {
                const data = await libRes.json();
                rawLibraryFiles = data.files || [];
                libraryNormalizedSet.clear();
                rawLibraryFiles.forEach(f => libraryNormalizedSet.add(normalizeKey(f.name)));

                if ($("libCountDetail")) $("libCountDetail").textContent = rawLibraryFiles.length;
                if ($("mobLibCount")) $("mobLibCount").textContent = rawLibraryFiles.length;
                if ($("libFolderSize")) $("libFolderSize").textContent = data.total_size || "0 B";

                renderLibrary(rawLibraryFiles);
            }

            if (statsRes.ok) {
                const stats = await statsRes.json();
                if ($("statTracks")) $("statTracks").textContent = stats.tracks || 0;
                if ($("statArtists")) $("statArtists").textContent = stats.artists || 0;
                if ($("statAlbums")) $("statAlbums").textContent = stats.albums || 0;
            }
        } catch (e) {}
    }

    function renderLibrary(files) {
        const container = $("libraryList");
        if (!container) return;

        const query = ($("libSearchQuery")?.value || "").toLowerCase();
        const filtered = files.filter(f => f.name.toLowerCase().includes(query));

        if (filtered.length === 0) {
            container.innerHTML = `<div style="text-align:center; padding: 40px; color: var(--text-muted, #888);">No library files found.</div>`;
            return;
        }

        container.innerHTML = filtered.map(file => `
            <div class="result-card">
                <div class="thumb-wrapper">
                    <img src="/api/library/cover/${encodeURIComponent(file.name)}" alt="Cover" />
                </div>
                <div class="track-info">
                    <div class="track-title">${escapeHtml(file.name)}</div>
                    <div class="track-artist">${escapeHtml(file.size)}</div>
                </div>
                <div class="btn-group">
                    <button class="btn-preview" onclick="playLibraryFile('${escapeHtml(file.name)}')">Play</button>
                    <button class="btn-danger" onclick="deleteLibraryFile('${escapeHtml(file.name)}')">Delete</button>
                </div>
            </div>
        `).join("");
    }

    function playLibraryFile(filename) {
        if (!globalAudio) return;
        if (gpBar) gpBar.style.display = "grid";
        if (gpTitle) gpTitle.textContent = filename;
        if (gpArtist) gpArtist.textContent = "Local Library";
        if (gpArt) gpArt.src = `/api/library/cover/${encodeURIComponent(filename)}`;

        globalAudio.src = `/api/library/stream/${encodeURIComponent(filename)}`;
        globalAudio.play();
        if (gpPlayBtn) gpPlayBtn.textContent = "❚❚";
    }

    async function deleteLibraryFile(filename) {
        if (!confirm(`Delete ${filename}?`)) return;
        try {
            await fetch(`/api/library/${encodeURIComponent(filename)}`, { method: "DELETE" });
            loadLibrary();
        } catch (e) {}
    }

    /* =========================================================
       SETTINGS MANAGEMENT
       ========================================================= */

    async function loadSettings() {
        try {
            const res = await fetch(API.settings);
            if (!res.ok) return;
            const settings = await res.json();

            if ($("set_format")) $("set_format").value = settings.audio_format || "mp3";
            if ($("set_quality")) $("set_quality").value = settings.audio_quality || "320K";
            if ($("set_max_results")) $("set_max_results").value = settings.max_results || 20;
            if ($("set_thumb")) $("set_thumb").checked = !!settings.embed_thumbnail;
            if ($("set_meta")) $("set_meta").checked = !!settings.embed_metadata;
            if ($("set_organize")) $("set_organize").checked = !!settings.organize_by_artist;
            if ($("set_navidrome_url")) $("set_navidrome_url").value = settings.navidrome_url || "";
            if ($("set_navidrome_user")) $("set_navidrome_user").value = settings.navidrome_user || "";
            if ($("set_navidrome_token")) $("set_navidrome_token").value = settings.navidrome_token || "";
        } catch (e) {}
    }

    async function saveSettings() {
        const payload = {
            audio_format: $("set_format")?.value,
            audio_quality: $("set_quality")?.value,
            max_results: parseInt($("set_max_results")?.value || "20", 10),
            embed_thumbnail: $("set_thumb")?.checked,
            embed_metadata: $("set_meta")?.checked,
            organize_by_artist: $("set_organize")?.checked,
            navidrome_url: $("set_navidrome_url")?.value,
            navidrome_user: $("set_navidrome_user")?.value,
            navidrome_token: $("set_navidrome_token")?.value
        };

        const msg = $("settingsMsg");
        try {
            const res = await fetch(API.settings, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });
            if (res.ok) {
                if (msg) msg.textContent = "Settings saved successfully!";
                setTimeout(() => { if (msg) msg.textContent = ""; }, 3000);
            }
        } catch (e) {
            if (msg) msg.textContent = "Error saving settings.";
        }
    }

    /* =========================================================
       WEBSOCKET INITIALIZATION
       ========================================================= */

    function initWebSocket() {
        const protocol = location.protocol === "https:" ? "wss:" : "ws:";
        socket = new WebSocket(`${protocol}//${location.host}/ws`);

        socket.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                if (data.type === "task_update") {
                    loadTasks();
                }
            } catch (e) {}
        };

        socket.onclose = () => {
            setTimeout(initWebSocket, 3000);
        };
    }

    /* =========================================================
       WINDOW EXPORTS & INITIALIZATION
       ========================================================= */

    window.navigate = navigate;
    window.performSearch = performSearch;
    window.enqueueDownload = enqueueDownload;
    window.playPreview = playPreview;
    window.cancelTask = cancelTask;
    window.clearCompletedDownloads = clearCompletedDownloads;
    window.loadLibrary = loadLibrary;
    window.playLibraryFile = playLibraryFile;
    window.deleteLibraryFile = deleteLibraryFile;

    document.addEventListener("DOMContentLoaded", () => {
        setupAudioEvents();
        initWebSocket();
        loadLibrary();

        const searchInput = $("searchQuery");
        if (searchInput) {
            searchInput.addEventListener("keypress", (e) => {
                if (e.key === "Enter") performSearch();
            });
        }

        const libSearchInput = $("libSearchQuery");
        if (libSearchInput) {
            libSearchInput.addEventListener("input", () => {
                renderLibrary(rawLibraryFiles);
            });
        }

        const saveBtn = $("saveSettingsBtn");
        if (saveBtn) {
            saveBtn.addEventListener("click", saveSettings);
        }

        pollTimer = setInterval(loadTasks, 4000);
    });
})();
