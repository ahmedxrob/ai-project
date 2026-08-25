/* ============================================================
   XROB MUSIC — FIXED + IMPROVED FRONTEND
   ============================================================ */

(() => {
    "use strict";

    /* =========================================================
       CONFIG
       ========================================================= */

    const API = {
        search: "api/search",
        settings: "api/settings",
        library: "api/library",
        tasks: "api/tasks",
        download: "api/download",
        preview: "api/preview",
        stats: "api/stats"
    };

    let socket = null;
    let socketReconnectTimer = null;
    let pollTimer = null;

    const completedSet = new Set();
    const notifiedTaskSet = new Set();

    let libraryNormalizedSet = new Set();
    let rawLibraryFiles = [];

    let activePreviewBtn = null;

    /* =========================================================
       DOM HELPERS
       ========================================================= */

    const $ = (id) => document.getElementById(id);

    let globalAudio = $("global-audio-element");
    if (!globalAudio) {
        globalAudio = document.createElement("audio");
        globalAudio.id = "global-audio-element";
        globalAudio.style.display = "none";
        document.body.appendChild(globalAudio);
    }

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
       SAFE HTML
       ========================================================= */

    function escapeHtml(value) {
        return String(value ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function escapeAttr(value) {
        return escapeHtml(value);
    }

    /* =========================================================
       FORMATTERS
       ========================================================= */

    function formatSecs(sec) {
        sec = Math.floor(Number(sec) || 0);
        const h = Math.floor(sec / 3600);
        const m = Math.floor((sec % 3600) / 60);
        const s = sec % 60;

        if (h > 0) {
            return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
        }
        return `${m}:${String(s).padStart(2, "0")}`;
    }

    function formatBytes(bytes) {
        const n = Number(bytes);
        if (!Number.isFinite(n) || n <= 0) return "0 B";

        const units = ["B", "KB", "MB", "GB", "TB"];
        let value = n;
        let index = 0;

        while (value >= 1024 && index < units.length - 1) {
            value /= 1024;
            index++;
        }

        return `${value.toFixed(value >= 10 || index === 0 ? 0 : 1)} ${units[index]}`;
    }

    /* =========================================================
       NORMALIZATION / DUPLICATE DETECTION
       ========================================================= */

    function normalizeKey(value) {
        return String(value || "")
            .normalize("NFKD")
            .replace(/[\u0300-\u036f]/g, "")
            .toLowerCase()
            .replace(
                /\b(official|video|audio|music|lyrics?|hd|4k|8k|remastered?|remaster|visualizer|topic|live|explicit|clean|version|edit)\b/gi,
                " "
            )
            .replace(/\bfeat\.?\b|\bft\.?\b/gi, " ")
            .replace(/\([^)]*\)/g, " ")
            .replace(/\[[^\]]*\]/g, " ")
            .replace(/[^a-z0-9]+/g, "")
            .trim();
    }

    function rebuildLibraryDuplicateIndex() {
        libraryNormalizedSet.clear();
        for (const file of rawLibraryFiles) {
            const base = String(file.name || "").split("/").pop().replace(/\.[^/.]+$/, "");
            const normalized = normalizeKey(base);
            if (normalized) {
                libraryNormalizedSet.add(normalized);
            }
        }
    }

    /* =========================================================
       THEME & TOAST
       ========================================================= */

    function toggleTheme(theme) {
        const validTheme = theme === "light" ? "light" : "dark";
        document.documentElement.setAttribute("data-theme", validTheme);
        localStorage.setItem("xrob_music_theme", validTheme);
    }

    const savedTheme = localStorage.getItem("xrob_music_theme") || "dark";
    toggleTheme(savedTheme);

    function showToast(message, type = "normal") {
        let container = $("toast-container");
        if (!container) {
            container = document.createElement("div");
            container.id = "toast-container";
            document.body.appendChild(container);
        }

        const toast = document.createElement("div");
        toast.className = `toast toast-${type}`;
        toast.textContent = message;

        container.appendChild(toast);
        requestAnimationFrame(() => toast.classList.add("show"));

        setTimeout(() => {
            toast.classList.remove("show");
            setTimeout(() => toast.remove(), 250);
        }, 4000);
    }

    function notifyTrackComplete(title) {
        showToast(`✓ ${title} is ready in your library`, "success");
        if ("Notification" in window && Notification.permission === "granted") {
            try {
                new Notification("Download Complete", {
                    body: title,
                    icon: "/static/favicon.ico"
                });
            } catch (e) {}
        }
    }

    /* =========================================================
       NAVIGATION
       ========================================================= */

    function navigate(tabName) {
        const tabs = document.querySelectorAll(".tab-content");
        tabs.forEach((tab) => tab.classList.remove("active"));

        const navLinks = document.querySelectorAll(".nav-link");
        navLinks.forEach((link) => link.classList.remove("active"));

        const activeTab = $(`tab-${tabName}`);
        if (activeTab) activeTab.classList.add("active");

        const btnDesk = $(`btn-${tabName}`);
        if (btnDesk) btnDesk.classList.add("active");

        const btnMob = $(`mob-btn-${tabName}`);
        if (btnMob) btnMob.classList.add("active");

        if (tabName === "downloads") loadTasks();
        if (tabName === "library") loadLibrary();
        if (tabName === "settings") loadSettingsUI();
    }

    /* =========================================================
       SEARCH
       ========================================================= */

    async function performSearch() {
        const queryInput = $("searchQuery");
        if (!queryInput) return;

        const query = queryInput.value.trim();
        if (!query) return;

        const resultsContainer = $("searchResults");
        if (resultsContainer) {
            resultsContainer.innerHTML = `<div class="status-msg">Searching YouTube...</div>`;
        }

        try {
            const res = await fetch(`/${API.search}?q=${encodeURIComponent(query)}`);
            if (!res.ok) throw new Error(`Search failed: ${res.statusText}`);

            const results = await res.json();
            renderSearchResults(results);
        } catch (err) {
            if (resultsContainer) {
                resultsContainer.innerHTML = `<div class="status-msg" style="color: var(--danger)">${escapeHtml(err.message)}</div>`;
            }
        }
    }

    function renderSearchResults(results) {
        const container = $("searchResults");
        if (!container) return;

        if (!results || results.length === 0) {
            container.innerHTML = `<div class="status-msg">No results found.</div>`;
            return;
        }

        container.innerHTML = results.map((item) => {
            const isDup = libraryNormalizedSet.has(normalizeKey(item.title));
            return `
                <div class="result-card">
                    <div class="thumb-wrapper">
                        <img src="${escapeAttr(item.thumbnail)}" alt="Thumb">
                        <span class="badge-duration">${escapeHtml(item.duration_text)}</span>
                    </div>
                    <div class="track-info">
                        <div class="track-title" title="${escapeAttr(item.title)}">${escapeHtml(item.title)}</div>
                        <div class="track-artist">${escapeHtml(item.channel)}</div>
                    </div>
                    <div class="btn-group">
                        <button class="btn-preview" onclick="playPreview('${escapeAttr(item.url)}', '${escapeAttr(item.title)}', '${escapeAttr(item.channel)}', '${escapeAttr(item.thumbnail)}', this)">Preview</button>
                        <button class="btn-download" ${isDup ? "disabled" : ""} onclick="enqueueDownload('${escapeAttr(item.url)}', '${escapeAttr(item.title)}', '${escapeAttr(item.channel)}')">
                            ${isDup ? "In Library" : "Download"}
                        </button>
                    </div>
                </div>
            `;
        }).join("");
    }

    /* =========================================================
       PLAYER & PREVIEW
       ========================================================= */

    async function playPreview(url, title, artist, art, btn) {
        if (activePreviewBtn) {
            activePreviewBtn.classList.remove("playing");
            activePreviewBtn.textContent = "Preview";
        }

        if (activePreviewBtn === btn && !globalAudio.paused) {
            globalAudio.pause();
            activePreviewBtn = null;
            if (gpBar) gpBar.style.display = "none";
            return;
        }

        activePreviewBtn = btn;
        if (btn) {
            btn.classList.add("playing");
            btn.textContent = "Loading...";
        }

        try {
            const streamUrl = `/${API.preview}?url=${encodeURIComponent(url)}`;
            globalAudio.src = streamUrl;
            await globalAudio.play();

            if (gpBar) gpBar.style.display = "grid";
            if (gpTitle) gpTitle.textContent = title;
            if (gpArtist) gpArtist.textContent = artist;
            if (gpArt) gpArt.src = art || "";
            if (gpPlayBtn) gpPlayBtn.textContent = "⏸";

            if (btn) btn.textContent = "Stop";
        } catch (err) {
            showToast("Failed to load audio preview.", "error");
            if (btn) {
                btn.classList.remove("playing");
                btn.textContent = "Preview";
            }
            activePreviewBtn = null;
        }
    }

    if (gpPlayBtn) {
        gpPlayBtn.addEventListener("click", () => {
            if (globalAudio.paused) {
                globalAudio.play();
                gpPlayBtn.textContent = "⏸";
            } else {
                globalAudio.pause();
                gpPlayBtn.textContent = "▶";
            }
        });
    }

    globalAudio.addEventListener("timeupdate", () => {
        if (gpCurTime) gpCurTime.textContent = formatSecs(globalAudio.currentTime);
        if (gpDurTime) gpDurTime.textContent = formatSecs(globalAudio.duration);
        if (gpSeek && globalAudio.duration) {
            gpSeek.value = (globalAudio.currentTime / globalAudio.duration) * 100;
        }
    });

    if (gpSeek) {
        gpSeek.addEventListener("input", () => {
            if (globalAudio.duration) {
                globalAudio.currentTime = (gpSeek.value / 100) * globalAudio.duration;
            }
        });
    }

    if (gpVolume) {
        gpVolume.addEventListener("input", () => {
            globalAudio.volume = gpVolume.value;
        });
    }

    /* =========================================================
       TASKS & QUEUE
       ========================================================= */

    async function enqueueDownload(url, title, artist, album = "") {
        try {
            const res = await fetch(`/${API.download}`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ url, title, artist, album })
            });

            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || "Download failed to enqueue.");

            showToast(`Added to queue: ${title}`, "success");
            loadTasks();
        } catch (err) {
            showToast(err.message, "error");
        }
    }

    async function loadTasks() {
        try {
            const res = await fetch(`/${API.tasks}`);
            if (!res.ok) return;
            const tasks = await res.json();
            renderTasks(tasks);
        } catch (err) {}
    }

    function renderTasks(tasks) {
        const list = $("downloadList");
        if (!list) return;

        if (!tasks || tasks.length === 0) {
            list.innerHTML = `<div class="status-msg">Queue is empty.</div>`;
            return;
        }

        list.innerHTML = tasks.map((task) => {
            if (task.status === "completed" && !notifiedTaskSet.has(task.id)) {
                notifiedTaskSet.add(task.id);
                notifyTrackComplete(task.title);
                loadLibrary();
            }

            return `
                <div class="queue-item">
                    <div class="track-info">
                        <div class="track-title">${escapeHtml(task.title)}</div>
                        <div class="track-artist">${escapeHtml(task.artist)} • <small>${escapeHtml(task.step || task.status)}</small></div>
                    </div>
                    <div class="btn-group">
                        ${task.status === "error" || task.status === "cancelled" 
                            ? `<button class="btn-preview" onclick="retryTask('${task.id}')">Retry</button>` 
                            : ""}
                        ${task.status === "queued" || task.status === "downloading" || task.status === "processing"
                            ? `<button class="btn-danger" onclick="cancelTask('${task.id}')">Cancel</button>` 
                            : ""}
                    </div>
                </div>
            `;
        }).join("");
    }

    async function cancelTask(taskId) {
        try {
            await fetch(`/api/tasks/${taskId}/cancel`, { method: "POST" });
            loadTasks();
        } catch (e) {}
    }

    async function retryTask(taskId) {
        try {
            await fetch(`/api/tasks/${taskId}/retry`, { method: "POST" });
            loadTasks();
        } catch (e) {}
    }

    async function clearCompletedDownloads() {
        loadTasks();
    }

    /* =========================================================
       LIBRARY
       ========================================================= */

    async function loadLibrary() {
        try {
            const res = await fetch(`/${API.library}`);
            if (!res.ok) return;

            const data = await res.json();
            rawLibraryFiles = data.files || [];
            rebuildLibraryDuplicateIndex();

            const countEl = $("statTracks");
            if (countEl) countEl.textContent = rawLibraryFiles.length;

            const detailEl = $("libCountDetail");
            if (detailEl) detailEl.textContent = rawLibraryFiles.length;

            const sizeEl = $("libFolderSize");
            if (sizeEl) sizeEl.textContent = data.total_size || "0 B";

            const mobCount = $("mobLibCount");
            if (mobCount) mobCount.textContent = rawLibraryFiles.length;

            renderLibraryFiles(rawLibraryFiles);
        } catch (err) {}
    }

    function renderLibraryFiles(files) {
        const container = $("libraryList");
        if (!container) return;

        if (!files || files.length === 0) {
            container.innerHTML = `<div class="status-msg">No files in library.</div>`;
            return;
        }

        container.innerHTML = files.map((file) => `
            <div class="result-card">
                <div class="thumb-wrapper">
                    <img src="/api/library/cover/${encodeURIComponent(file.name)}" alt="Cover">
                </div>
                <div class="track-info">
                    <div class="track-title">${escapeHtml(file.name)}</div>
                    <div class="track-artist">${escapeHtml(file.size)}</div>
                </div>
                <div class="btn-group">
                    <button class="btn-danger" onclick="deleteLibraryFile('${escapeAttr(file.name)}')">Delete</button>
                </div>
            </div>
        `).join("");
    }

    async function deleteLibraryFile(filename) {
        if (!confirm(`Delete ${filename}?`)) return;
        try {
            const res = await fetch(`/api/library/${encodeURIComponent(filename)}`, { method: "DELETE" });
            if (res.ok) {
                showToast("File deleted", "success");
                loadLibrary();
            }
        } catch (err) {}
    }

    /* =========================================================
       SETTINGS UI
       ========================================================= */

    async function loadSettingsUI() {
        try {
            const res = await fetch(`/${API.settings}`);
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

    const saveSettingsBtn = $("saveSettingsBtn");
    if (saveSettingsBtn) {
        saveSettingsBtn.addEventListener("click", async () => {
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

            const themeSelect = $("set_theme");
            if (themeSelect) toggleTheme(themeSelect.value);

            try {
                const res = await fetch(`/${API.settings}`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload)
                });

                if (res.ok) {
                    const msg = $("settingsMsg");
                    if (msg) msg.textContent = "Settings saved successfully.";
                    showToast("Settings saved", "success");
                }
            } catch (err) {}
        });
    }

    /* =========================================================
       WEBSOCKET CONNECTION
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
            socketReconnectTimer = setTimeout(initWebSocket, 3000);
        };
    }

    /* =========================================================
       EXPOSE TO WINDOW (FOR HTML INLINE LISTENERS)
       ========================================================= */

    window.navigate = navigate;
    window.performSearch = performSearch;
    window.enqueueDownload = enqueueDownload;
    window.playPreview = playPreview;
    window.cancelTask = cancelTask;
    window.retryTask = retryTask;
    window.clearCompletedDownloads = clearCompletedDownloads;
    window.loadLibrary = loadLibrary;
    window.deleteLibraryFile = deleteLibraryFile;

    /* =========================================================
       INIT
       ========================================================= */

    document.addEventListener("DOMContentLoaded", () => {
        loadLibrary();
        loadTasks();
        initWebSocket();
        pollTimer = setInterval(loadTasks, 5000);
    });

})();
