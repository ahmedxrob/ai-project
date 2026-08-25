(() => {
    "use strict";

    /* =========================================================
       CONFIG
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

    const MAX_SEARCH_HISTORY = 10;
    let DOWNLOAD_CONCURRENCY = 3;

    /* =========================================================
       GLOBAL STATE
       ========================================================= */

    let pollTimer = null;
    let socket = null;
    let socketReconnectTimer = null;

    const completedSet = new Set();
    const notifiedTaskSet = new Set();

    let libraryFilesSet = new Set();
    let libraryNormalizedSet = new Set();
    let rawLibraryFiles = [];

    let activePreviewBtn = null;

    let currentPage = 1;
    let currentQuery = "";
    let isLoadingMore = false;
    let hasMoreResults = true;

    /* Client queue */
    const downloadQueue = [];
    const activeQueueJobs = new Map();

    /* Search suggestions */
    let searchHistory = [];
    let suggestionBox = null;

    /* =========================================================
       DOM HELPERS
       ========================================================= */

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

    const canvas = $("visualizer-canvas");
    const canvasCtx = canvas ? canvas.getContext("2d") : null;

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

    function formatSpeed(value) {
        if (!value) return "";
        if (typeof value === "number") return `${formatBytes(value)}/s`;
        return String(value);
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

    function normalizeArtist(value) {
        return normalizeKey(value).replace(/official/g, "");
    }

    function makeTrackKey(title, artist = "") {
        const t = normalizeKey(title);
        const a = normalizeArtist(artist);
        return `${a}::${t}`;
    }

    function filenameWithoutExtension(name) {
        const clean = String(name || "").split("/").pop();
        return clean.replace(/\.[^/.]+$/, "");
    }

    function isProbablyDuplicate(title, artist = "") {
        const titleKey = normalizeKey(title);
        const fullKey = makeTrackKey(title, artist);

        if (!titleKey) return false;
        if (libraryNormalizedSet.has(fullKey)) return true;
        if (libraryNormalizedSet.has(titleKey)) return true;

        for (const key of libraryNormalizedSet) {
            if (key === titleKey || key.endsWith(`::${titleKey}`)) {
                return true;
            }
        }
        return false;
    }

    function rebuildLibraryDuplicateIndex() {
        libraryNormalizedSet.clear();

        for (const file of rawLibraryFiles) {
            const base = filenameWithoutExtension(file.name);
            const normalized = normalizeKey(base);

            if (normalized) {
                libraryNormalizedSet.add(normalized);
            }
            if (file.title || file.artist) {
                libraryNormalizedSet.add(makeTrackKey(file.title, file.artist));
            }
        }
    }

    /* =========================================================
       THEME
       ========================================================= */

    function toggleTheme(theme) {
        const validTheme = theme === "light" ? "light" : "dark";
        document.documentElement.setAttribute("data-theme", validTheme);
        localStorage.setItem("xrob_music_theme", validTheme);
    }

    const savedTheme = localStorage.getItem("xrob_music_theme") || "dark";
    toggleTheme(savedTheme);

    /* =========================================================
       TOAST
       ========================================================= */

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

        requestAnimationFrame(() => {
            toast.classList.add("show");
        });

        setTimeout(() => {
            toast.classList.remove("show");
            setTimeout(() => {
                toast.remove();
            }, 250);
        }, 4000);
    }

    /* =========================================================
       NOTIFICATIONS
       ========================================================= */

    async function requestNotificationPermission() {
        if (!("Notification" in window)) {
            showToast("Browser notifications are not supported.", "error");
            return;
        }

        try {
            const permission = await Notification.requestPermission();
            if (permission === "granted") {
                showToast("Notifications enabled.", "success");
            } else {
                showToast("Notification permission denied.", "error");
            }
        } catch (error) {
            console.error(error);
        }
    }

    function notifyTrackComplete(title) {
        showToast(`✓ ${title} is ready in your library`, "success");

        if ("Notification" in window && Notification.permission === "granted") {
            try {
                new Notification("Track ready", {
                    body: `${title} is now in your library.`
                });
            } catch (error) {
                console.warn("Notification failed:", error);
            }
        }
    }

    /* =========================================================
       AUDIO PLAYER
       ========================================================= */

    let audioCtx = null;
    let analyser = null;
    let sourceNode = null;
    let visualizerAnimationFrame = null;

    function initAudioContext() {
        if (!globalAudio || audioCtx) return;

        try {
            const AudioContext = window.AudioContext || window.webkitAudioContext;
            if (!AudioContext) return;

            audioCtx = new AudioContext();
            analyser = audioCtx.createAnalyser();
            analyser.fftSize = 64;

            sourceNode = audioCtx.createMediaElementSource(globalAudio);
            sourceNode.connect(analyser);
            analyser.connect(audioCtx.destination);

            drawVisualizer();
        } catch (error) {
            console.warn("Audio visualizer unavailable:", error);
        }
    }

    function drawVisualizer() {
        if (!analyser || !canvasCtx || !canvas) return;

        visualizerAnimationFrame = requestAnimationFrame(drawVisualizer);

        if (
            canvas.width !== canvas.clientWidth * devicePixelRatio ||
            canvas.height !== canvas.clientHeight * devicePixelRatio
        ) {
            canvas.width = Math.max(1, canvas.clientWidth * devicePixelRatio);
            canvas.height = Math.max(1, canvas.clientHeight * devicePixelRatio);
            canvasCtx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
        }

        const width = canvas.clientWidth;
        const height = canvas.clientHeight;
        const bufferLength = analyser.frequencyBinCount;
        const dataArray = new Uint8Array(bufferLength);

        analyser.getByteFrequencyData(dataArray);
        canvasCtx.clearRect(0, 0, width, height);

        const barWidth = Math.max(2, width / bufferLength - 2);

        for (let i = 0; i < bufferLength; i++) {
            const value = dataArray[i] / 255;
            const barHeight = value * height;
            const x = i * (barWidth + 2);

            canvasCtx.fillStyle = "rgba(99,102,241,.85)";
            canvasCtx.fillRect(x, height - barHeight, barWidth, barHeight);
        }
    }

    function stopCurrentPreview() {
        if (!globalAudio) return;

        globalAudio.pause();
        gpPlayBtn && (gpPlayBtn.textContent = "▶");

        if (activePreviewBtn) {
            activePreviewBtn.classList.remove("playing");
            activePreviewBtn.innerHTML =
                activePreviewBtn.dataset.type === "library" ? "▶ Play" : "▶ Preview";
            activePreviewBtn = null;
        }
    }

    function updatePlayerUI() {
        if (!globalAudio) return;

        if (Number.isFinite(globalAudio.duration) && globalAudio.duration > 0) {
            const percent = (globalAudio.currentTime / globalAudio.duration) * 100;

            if (gpSeek) {
                gpSeek.value = Math.max(0, Math.min(100, percent));
            }
            if (gpDurTime) {
                gpDurTime.textContent = formatSecs(globalAudio.duration);
            }
        }

        if (gpCurTime) {
            gpCurTime.textContent = formatSecs(globalAudio.currentTime);
        }
    }

    if (globalAudio) {
        globalAudio.addEventListener("timeupdate", updatePlayerUI);
        globalAudio.addEventListener("loadedmetadata", updatePlayerUI);

        globalAudio.addEventListener("ended", () => {
            if (gpPlayBtn) gpPlayBtn.textContent = "▶";

            if (activePreviewBtn) {
                activePreviewBtn.classList.remove("playing");
                activePreviewBtn.innerHTML =
                    activePreviewBtn.dataset.type === "library" ? "▶ Play" : "▶ Preview";
            }
        });

        globalAudio.addEventListener("error", () => {
            if (activePreviewBtn) {
                activePreviewBtn.classList.remove("playing");
                activePreviewBtn.textContent =
                    activePreviewBtn.dataset.type === "library" ? "▶ Play" : "▶ Preview";
            }

            if (gpPlayBtn) gpPlayBtn.textContent = "▶";
            showToast("Unable to play this track.", "error");
        });
    }

    if (gpPlayBtn) {
        gpPlayBtn.addEventListener("click", async () => {
            if (!globalAudio) return;

            initAudioContext();
            if (audioCtx && audioCtx.state === "suspended") {
                try { await audioCtx.resume(); } catch {}
            }

            if (globalAudio.paused) {
                try {
                    await globalAudio.play();
                    gpPlayBtn.textContent = "⏸";
                    if (activePreviewBtn) {
                        activePreviewBtn.classList.add("playing");
                    }
                } catch {
                    showToast("Unable to play audio.", "error");
                }
            } else {
                globalAudio.pause();
                gpPlayBtn.textContent = "▶";
                if (activePreviewBtn) {
                    activePreviewBtn.classList.remove("playing");
                }
            }
        });
    }

    if (gpSeek) {
        gpSeek.addEventListener("input", () => {
            if (globalAudio && Number.isFinite(globalAudio.duration)) {
                globalAudio.currentTime = (Number(gpSeek.value) / 100) * globalAudio.duration;
            }
        });
    }

    if (gpVolume) {
        globalAudio && (globalAudio.volume = Number(gpVolume.value));
        gpVolume.addEventListener("input", () => {
            if (globalAudio) {
                globalAudio.volume = Number(gpVolume.value);
            }
        });
    }

    async function toggleAudioStream(
        btn,
        streamUrl,
        type = "search",
        title = "Track",
        artist = "Artist",
        artUrl = ""
    ) {
        if (!globalAudio || !btn) return;

        initAudioContext();
        if (audioCtx && audioCtx.state === "suspended") {
            try { await audioCtx.resume(); } catch {}
        }

        if (activePreviewBtn === btn && !globalAudio.paused) {
            globalAudio.pause();
            btn.classList.remove("playing");
            btn.innerHTML = type === "library" ? "▶ Play" : "▶ Preview";
            if (gpPlayBtn) gpPlayBtn.textContent = "▶";
            return;
        }

        if (activePreviewBtn) {
            activePreviewBtn.classList.remove("playing");
            activePreviewBtn.innerHTML =
                activePreviewBtn.dataset.type === "library" ? "▶ Play" : "▶ Preview";
        }

        btn.dataset.type = type;
        activePreviewBtn = btn;
        btn.innerHTML = "⏳ Loading...";

        if (gpTitle) gpTitle.textContent = title;
        if (gpArtist) gpArtist.textContent = artist;
        if (gpArt) {
            gpArt.src = artUrl || "data:image/svg+xml;charset=UTF-8," + encodeURIComponent(
                `<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><rect width="100%" height="100%" fill="#1e293b"/><text x="50%" y="55%" text-anchor="middle" font-size="28">♪</text></svg>`
            );
        }

        if (gpBar) gpBar.style.display = "flex";

        globalAudio.pause();
        globalAudio.currentTime = 0;
        globalAudio.src = streamUrl;

        try {
            await globalAudio.play();
            btn.classList.add("playing");
            btn.innerHTML = "⏸ Pause";
            if (gpPlayBtn) gpPlayBtn.textContent = "⏸";
        } catch (error) {
            if (!streamUrl.includes("transcode=true")) {
                const separator = streamUrl.includes("?") ? "&" : "?";
                const fallbackUrl = `${streamUrl}${separator}transcode=true`;
                await toggleAudioStream(btn, fallbackUrl, type, title, artist, artUrl);
                return;
            }

            btn.classList.remove("playing");
            btn.innerHTML = type === "library" ? "▶ Play" : "▶ Preview";
            showToast("Audio could not be played.", "error");
        }
    }

    /* =========================================================
       SEARCH HISTORY
       ========================================================= */

    function loadSearchHistory() {
        try {
            const value = JSON.parse(
                localStorage.getItem("xrob_music_search_history") || "[]"
            );
            searchHistory = Array.isArray(value) ? value.filter(Boolean) : [];
        } catch {
            searchHistory = [];
        }
    }

    function saveSearchHistory(query) {
        query = String(query || "").trim();
        if (!query) return;

        searchHistory = searchHistory.filter(
            item => item.toLowerCase() !== query.toLowerCase()
        );
        searchHistory.unshift(query);
        searchHistory = searchHistory.slice(0, MAX_SEARCH_HISTORY);

        localStorage.setItem("xrob_music_search_history", JSON.stringify(searchHistory));
    }

    function createSuggestionsBox() {
        const input = $("query");
        if (!input || suggestionBox) return;

        suggestionBox = document.createElement("div");
        suggestionBox.id = "searchSuggestions";
        suggestionBox.className = "search-suggestions";

        const parent = input.closest(".search-card") || input.parentElement;
        if (parent) {
            parent.style.position = "relative";
            parent.appendChild(suggestionBox);
        }
    }

    function hideSuggestions() {
        if (suggestionBox) {
            suggestionBox.classList.remove("visible");
        }
    }

    function showSuggestions(value = "") {
        if (!suggestionBox) createSuggestionsBox();
        if (!suggestionBox) return;

        const query = String(value).toLowerCase().trim();
        const suggestions = searchHistory
            .filter(item => !query || item.toLowerCase().includes(query))
            .slice(0, 6);

        if (!suggestions.length) {
            hideSuggestions();
            return;
        }

        suggestionBox.innerHTML = suggestions.map(item => `
            <button type="button" class="search-suggestion" data-query="${escapeAttr(item)}">
                <span>◷</span>
                <span>${escapeHtml(item)}</span>
            </button>
        `).join("");

        suggestionBox.querySelectorAll(".search-suggestion").forEach(button => {
            button.addEventListener("click", () => {
                const input = $("query");
                if (input) {
                    input.value = button.dataset.query;
                    hideSuggestions();
                    searchMusic();
                }
            });
        });

        suggestionBox.classList.add("visible");
    }

    loadSearchHistory();

    /* =========================================================
       NAVIGATION
       ========================================================= */

    function navigate(tab, updateHash = true) {
        if (updateHash) {
            window.location.hash = tab;
        } else {
            switchTab(tab);
        }
    }

    function switchTab(tab) {
        document.querySelectorAll(".tab-content").forEach(content => {
            content.classList.remove("active");
        });

        document.querySelectorAll(".nav-link").forEach(button => {
            button.classList.remove("active");
            button.setAttribute("aria-selected", "false");
        });

        const content = $(`tab-${tab}`);
        if (content) content.classList.add("active");

        const sideBtn = $(`btn-${tab}`);
        const mobBtn = $(`mob-btn-${tab}`);

        [sideBtn, mobBtn].filter(Boolean).forEach(button => {
            button.classList.add("active");
            button.setAttribute("aria-selected", "true");
        });

        if (tab === "library") loadLibrary();
        if (tab === "downloads") loadDownloads();
        if (tab === "settings") loadSettings();
    }

    function handleDeepLink() {
        const hash = window.location.hash.replace("#", "");
        const valid = ["search", "downloads", "library", "settings"];
        if (valid.includes(hash)) {
            switchTab(hash);
        } else {
            switchTab("search");
        }
    }

    window.addEventListener("hashchange", handleDeepLink);

    /* =========================================================
       SETTINGS
       ========================================================= */

    async function loadSettings() {
        try {
            const response = await fetch(API.settings);
            if (!response.ok) throw new Error("Settings request failed");

            const s = await response.json();
            const set = (id, value) => {
                const element = $(id);
                if (element) element.value = value ?? "";
            };

            const check = (id, value) => {
                const element = $(id);
                if (element) element.checked = !!value;
            };

            set("set_format", s.audio_format || "mp3");
            set("set_quality", s.audio_quality || "320K");
            check("set_thumb", s.embed_thumbnail);
            check("set_meta", s.embed_metadata);
            set("set_max_results", s.max_results || 20);
            check("set_organize", s.organize_by_artist);
            set("set_theme", localStorage.getItem("xrob_music_theme") || "dark");
            set("set_navidrome_url", s.navidrome_url || "");
            set("set_navidrome_user", s.navidrome_user || "");
            set("set_navidrome_token", s.navidrome_token || "");
        } catch (error) {
            console.error("loadSettings:", error);
        }
    }

    async function saveSettings() {
        const value = id => {
            const element = $(id);
            return element ? element.value : "";
        };

        const checked = id => {
            const element = $(id);
            return element ? element.checked : false;
        };

        const data = {
            audio_format: value("set_format"),
            audio_quality: value("set_quality"),
            embed_thumbnail: checked("set_thumb"),
            embed_metadata: checked("set_meta"),
            max_results: parseInt(value("set_max_results"), 10) || 20,
            organize_by_artist: checked("set_organize"),
            navidrome_url: value("set_navidrome_url"),
            navidrome_user: value("set_navidrome_user"),
            navidrome_token: value("set_navidrome_token")
        };

        const msg = $("settingsMsg");
        if (msg) msg.textContent = "Saving...";

        try {
            const response = await fetch(API.settings, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(data)
            });

            if (!response.ok) throw new Error("Failed to save settings");

            if (msg) msg.textContent = "✓ Settings saved!";
            showToast("Settings saved", "success");

            setTimeout(() => {
                if (msg) msg.textContent = "";
            }, 3000);
        } catch (error) {
            console.error(error);
            if (msg) msg.textContent = "✕ Failed to save settings.";
            showToast("Failed to save settings", "error");
        }
    }

    /* =========================================================
       LIBRARY
       ========================================================= */

    async function refreshLibraryCache() {
        try {
            const response = await fetch(`${API.library}?_=${Date.now()}`);
            if (!response.ok) throw new Error("Library request failed");

            const data = await response.json();
            rawLibraryFiles = Array.isArray(data.files) ? data.files : [];

            libraryFilesSet.clear();
            rawLibraryFiles.forEach(file => {
                const base = filenameWithoutExtension(file.name);
                const normalized = normalizeKey(base);
                if (normalized) libraryFilesSet.add(normalized);
            });

            rebuildLibraryDuplicateIndex();

            const count = rawLibraryFiles.length;
            ["sideLibCount", "mobLibCount", "libCountDetail"].forEach(id => {
                const el = $(id);
                if (el) el.textContent = count;
            });

            const size = $("libFolderSize");
            if (size) size.textContent = data.total_size || "0 B";

            return data;
        } catch (error) {
            console.error("refreshLibraryCache:", error);
            return null;
        }
    }

    /* =========================================================
       SEARCH RENDERING
       ========================================================= */

    function renderItems(data) {
        const results = $("results");
        if (!results || !Array.isArray(data)) return;

        data.forEach(item => {
            const title = item.title || "Unknown";
            const artist = item.channel || item.artist || "Unknown Artist";
            const duplicate = isProbablyDuplicate(title, artist);

            const card = document.createElement("div");
            card.className = "result-card";
            card.dataset.trackKey = makeTrackKey(title, artist);

            const thumb = item.thumbnail || "";

            card.innerHTML = `
                <div class="thumb-wrapper">
                    <img src="${escapeAttr(thumb)}" alt="${escapeAttr(title)}" loading="lazy">
                    <span class="badge-duration">${escapeHtml(item.duration_text || "")}</span>
                </div>
                <div class="track-info">
                    <div class="track-title">${escapeHtml(title)}</div>
                    <div class="track-artist">♪ ${escapeHtml(artist)}</div>
                </div>
                <div class="btn-group" data-group-id="${escapeAttr(item.id || "")}"></div>
            `;

            const image = card.querySelector("img");
            if (image) {
                image.addEventListener("error", () => {
                    image.src = "data:image/svg+xml;charset=UTF-8," + encodeURIComponent(`
                        <svg xmlns="http://www.w3.org/2000/svg" width="110" height="65">
                            <rect width="100%" height="100%" fill="#1e293b"/>
                            <text x="50%" y="55%" text-anchor="middle" font-size="24">♪</text>
                        </svg>
                    `);
                }, { once: true });
            }

            const btnGroup = card.querySelector(".btn-group");

            if (duplicate) {
                btnGroup.innerHTML = `
                    <div class="badge-library" title="A matching track already exists in your library">
                        ✓ In Library
                    </div>
                `;
            } else {
                const preview = document.createElement("button");
                preview.className = "btn-preview";
                preview.type = "button";
                preview.innerHTML = "▶ Preview";
                preview.onclick = () => toggleAudioStream(
                    preview,
                    `${API.preview}?url=${encodeURIComponent(item.url || "")}`,
                    "search",
                    title,
                    artist,
                    thumb
                );

                const download = document.createElement("button");
                download.className = "btn-download";
                download.type = "button";
                download.dataset.id = item.id || "";
                download.innerHTML = "↓ Save";
                download.onclick = () => {
                    enqueueDownload({
                        url: item.url,
                        title,
                        elementId: item.id || "",
                        artist
                    });
                };

                btnGroup.appendChild(preview);
                btnGroup.appendChild(download);
            }

            results.appendChild(card);
        });
    }

    /* =========================================================
       SEARCH
       ========================================================= */

    async function searchMusic() {
        const input = $("query");
        const query = input ? input.value.trim() : "";
        const statusMsg = $("statusMsg");
        const results = $("results");
        const searchBtn = $("searchBtn");

        if (!query) {
            showToast("Enter something to search.", "error");
            input?.focus();
            return;
        }

        currentQuery = query;
        currentPage = 1;
        hasMoreResults = true;
        isLoadingMore = false;

        saveSearchHistory(query);
        hideSuggestions();

        if (statusMsg) statusMsg.textContent = "Searching...";
        if (results) results.innerHTML = "";
        if (searchBtn) searchBtn.disabled = true;

        await refreshLibraryCache();

        try {
            const response = await fetch(`${API.search}?q=${encodeURIComponent(query)}&page=1`);
            if (!response.ok) throw new Error(`Search failed (${response.status})`);

            const data = await response.json();
            if (!Array.isArray(data) || !data.length) {
                hasMoreResults = false;
                if (statusMsg) statusMsg.textContent = "No results found.";
                return;
            }

            if (statusMsg) statusMsg.textContent = `${data.length} results`;
            renderItems(data);
        } catch (error) {
            console.error(error);
            if (statusMsg) statusMsg.textContent = `✕ ${error.message}`;
        } finally {
            if (searchBtn) searchBtn.disabled = false;
        }
    }

    async function loadMoreResults() {
        if (isLoadingMore || !hasMoreResults || !currentQuery) return;

        isLoadingMore = true;
        const nextPage = currentPage + 1;
        const loader = $("infiniteLoader");
        if (loader) loader.style.display = "block";

        try {
            const response = await fetch(`${API.search}?q=${encodeURIComponent(currentQuery)}&page=${nextPage}`);
            if (!response.ok) throw new Error("Load failed");

            const data = await response.json();
            if (!Array.isArray(data) || data.length === 0) {
                hasMoreResults = false;
            } else {
                currentPage = nextPage;
                renderItems(data);
            }
        } catch (error) {
            console.error(error);
        } finally {
            if (loader) loader.style.display = "none";
            isLoadingMore = false;
        }
    }

    /* =========================================================
       DOWNLOAD QUEUE
       ========================================================= */

    function getQueuedDuplicate(url, title, artist) {
        const key = makeTrackKey(title, artist);
        return (
            downloadQueue.find(job => job.key === key || job.url === url) ||
            [...activeQueueJobs.values()].find(job => job.key === key || job.url === url)
        );
    }

    function enqueueDownload(job) {
        if (!job || !job.url) {
            showToast("Invalid download URL.", "error");
            return;
        }

        if (isProbablyDuplicate(job.title, job.artist)) {
            showToast("This track is already in your library.", "normal");
            updateDownloadButton(job.elementId, "✓ In Library", true);
            return;
        }

        const existing = getQueuedDuplicate(job.url, job.title, job.artist);
        if (existing) {
            showToast("This track is already queued.", "normal");
            return;
        }

        const queuedJob = {
            id: crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`,
            url: job.url,
            title: job.title || "Unknown",
            artist: job.artist || "Unknown Artist",
            elementId: job.elementId || "",
            key: makeTrackKey(job.title, job.artist),
            addedAt: Date.now()
        };

        downloadQueue.push(queuedJob);
        updateDownloadButton(queuedJob.elementId, "⏳ Queued", true);
        showToast(`Added "${queuedJob.title}" to queue.`, "success");

        processDownloadQueue();
        renderQueueState();
    }

    async function processDownloadQueue() {
        while (activeQueueJobs.size < DOWNLOAD_CONCURRENCY && downloadQueue.length) {
            const job = downloadQueue.shift();
            if (!job) break;

            activeQueueJobs.set(job.id, job);

            runDownloadJob(job)
                .catch(error => { console.error("Download job:", error); })
                .finally(() => {
                    activeQueueJobs.delete(job.id);
                    processDownloadQueue();
                    renderQueueState();
                });
        }
    }

    async function runDownloadJob(job) {
        updateDownloadButton(job.elementId, "⏳ Starting...", true);

        try {
            const response = await fetch(API.download, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    url: job.url,
                    title: job.title,
                    elementId: job.elementId,
                    artist: job.artist
                })
            });

            if (!response.ok) {
                throw new Error(`Server rejected download (${response.status})`);
            }

            let result = null;
            try { result = await response.json(); } catch {}

            if (result && result.id) {
                job.serverTaskId = result.id;
            }

            updateDownloadButton(job.elementId, "⏳ Downloading", true);
            await pollTasks();
        } catch (error) {
            updateDownloadButton(job.elementId, "↓ Save", false);
            showToast(`Failed: ${job.title}`, "error");
            console.error("Download failed:", error);
        }
    }

    function updateDownloadButton(elementId, text, disabled) {
        if (!elementId) return;
        const buttons = document.querySelectorAll(`button[data-id="${CSS.escape(String(elementId))}"]`);
        buttons.forEach(button => {
            button.disabled = !!disabled;
            button.textContent = text;
        });
    }

    function removeQueuedJob(jobId) {
        const index = downloadQueue.findIndex(job => job.id === jobId);
        if (index === -1) return;

        const [job] = downloadQueue.splice(index, 1);
        updateDownloadButton(job.elementId, "↓ Save", false);
        renderQueueState();
    }

    /* =========================================================
       TASK POLLING
       ========================================================= */

    async function pollTasks() {
        try {
            const response = await fetch(`${API.tasks}?_=${Date.now()}`);
            if (!response.ok) throw new Error("Tasks request failed");

            const tasks = await response.json();
            if (!Array.isArray(tasks)) return;

            let libraryNeedsUpdate = false;

            for (const task of tasks) {
                if (task.status === "completed" && !completedSet.has(task.id)) {
                    completedSet.add(task.id);
                    libraryNeedsUpdate = true;

                    if (!notifiedTaskSet.has(task.id)) {
                        notifiedTaskSet.add(task.id);
                        notifyTrackComplete(task.title || "Track");
                    }

                    if (task.elementId) {
                        const group = document.querySelector(`div[data-group-id="${CSS.escape(String(task.elementId))}"]`);
                        if (group) {
                            group.innerHTML = `<div class="badge-library">✓ In Library</div>`;
                        }
                        updateDownloadButton(task.elementId, "✓ In Library", true);
                    }
                }

                if (task.status === "error" && task.elementId) {
                    updateDownloadButton(task.elementId, "↻ Retry", false);
                }
            }

            if (libraryNeedsUpdate) {
                await refreshLibraryCache();
                const libraryTab = $("tab-library");
                if (libraryTab && libraryTab.classList.contains("active")) {
                    loadLibrary();
                }
            }

            renderDownloadProgress(tasks);
            renderQueueState();
        } catch (error) {
            console.warn("pollTasks:", error);
        }
    }

    function getTaskPercent(task) {
        let percent = Number(task.percent);
        if (!Number.isFinite(percent)) percent = 0;

        if (task.status === "processing" && percent < 90) percent = 90;
        if (task.status === "completed") percent = 100;

        return Math.max(0, Math.min(100, Math.round(percent)));
    }

    function getTaskLabel(task) {
        switch (task.status) {
            case "queued": return "Waiting in queue";
            case "downloading": return task.step || "Downloading";
            case "processing": return task.step || "Processing audio";
            case "completed": return "Ready";
            case "error": return task.error || "Download failed";
            case "cancelled": return "Cancelled";
            default: return task.status || "Working";
        }
    }

    function renderDownloadProgress(tasks) {
        const panel = $("progressPanel");
        const list = $("activeDownloadsList");
        if (!panel || !list) return;

        const activeTasks = tasks.filter(task => ["queued", "downloading", "processing"].includes(task.status));
        const recentFinished = tasks.filter(task => ["completed", "error"].includes(task.status));
        const visibleTasks = [...activeTasks, ...recentFinished].slice(0, 8);

        if (!visibleTasks.length) {
            panel.style.display = "none";
            list.innerHTML = "";
            return;
        }

        panel.style.display = "block";
        list.innerHTML = visibleTasks.map(task => {
            const percent = getTaskPercent(task);
            const error = task.status === "error";
            const completed = task.status === "completed";
            const statusClass = error ? "error" : completed ? "completed" : "";
            const speed = formatSpeed(task.speed);
            const eta = task.eta ? ` · ETA ${escapeHtml(String(task.eta))}` : "";

            return `
                <div class="download-progress-item ${statusClass}" data-task-id="${escapeAttr(task.id)}">
                    <div class="progress-header">
                        <div class="progress-title" title="${escapeAttr(task.title)}">
                            ${error ? "✕" : completed ? "✓" : "♫"}
                            ${escapeHtml(task.title || "Unknown Track")}
                        </div>
                        <div class="progress-percent">${percent}%</div>
                    </div>
                    <div class="progress-track">
                        <div class="progress-fill ${error ? "progress-error" : completed ? "progress-complete" : ""}" style="width:${percent}%"></div>
                    </div>
                    <div class="progress-meta">
                        <span>${escapeHtml(getTaskLabel(task))}</span>
                        <span>${escapeHtml(speed)}${eta}</span>
                    </div>
                    <div class="progress-steps">
                        <span class="${percent >= 1 ? "done" : ""}"><i></i>Download</span>
                        <span class="${percent >= 90 ? "done" : ""}"><i></i>Clean tags</span>
                        <span class="${percent >= 100 ? "done" : ""}"><i></i>Ready</span>
                    </div>
                </div>
            `;
        }).join("");
    }

    /* =========================================================
       QUEUE UI
       ========================================================= */

    function renderQueueState() {
        const count = downloadQueue.length + activeQueueJobs.size;
        const queueCount = $("queueCount");
        if (queueCount) queueCount.textContent = count;
    }

    /* =========================================================
       DOWNLOAD PAGE
       ========================================================= */

    async function cancelTask(taskId) {
        if (!taskId) return;

        try {
            const response = await fetch(`${API.tasks}/${encodeURIComponent(taskId)}/cancel`, { method: "POST" });
            if (!response.ok) throw new Error("Cancel failed");

            showToast("Download cancelled.", "success");
            await loadDownloads();
        } catch (error) {
            showToast("Could not cancel download.", "error");
            console.error(error);
        }
    }

    async function loadDownloads() {
        const list = $("downloadsList");
        if (!list) return;

        try {
            const response = await fetch(`${API.tasks}?_=${Date.now()}`);
            if (!response.ok) throw new Error("Failed to load tasks");

            const tasks = await response.json();
            if (!Array.isArray(tasks)) throw new Error("Invalid tasks response");

            const activeCount = tasks.filter(task => ["queued", "downloading", "processing"].includes(task.status)).length;
            const queueCount = $("queueCount");
            if (queueCount) {
                queueCount.textContent = activeCount + downloadQueue.length + activeQueueJobs.size;
            }

            if (!tasks.length) {
                list.innerHTML = `<div class="status-msg">No downloads yet.</div>`;
                return;
            }

            list.innerHTML = tasks.map(task => {
                const percent = getTaskPercent(task);
                const active = ["queued", "downloading", "processing"].includes(task.status);
                const status = escapeHtml(task.status);

                return `
                    <div class="result-card download-card" data-task-id="${escapeAttr(task.id)}">
                        <div class="track-info">
                            <div class="track-title">${escapeHtml(task.title || "Unknown")}</div>
                            <div class="track-artist">${escapeHtml(task.artist || "Unknown Artist")} · ${status} · ${percent}%</div>
                            <div class="progress-track" style="margin-top:10px">
                                <div class="progress-fill" style="width:${percent}%"></div>
                            </div>
                        </div>
                        <div class="btn-group">
                            ${active ? `<button type="button" class="btn-danger" data-cancel-id="${escapeAttr(task.id)}">✕ Cancel</button>` : ""}
                        </div>
                    </div>
                `;
            }).join("");

            list.querySelectorAll("[data-cancel-id]").forEach(button => {
                button.addEventListener("click", () => {
                    cancelTask(button.dataset.cancelId);
                });
            });
        } catch (error) {
            console.error(error);
            list.innerHTML = `<div class="status-msg">Failed to load downloads.</div>`;
        }
    }

    /* =========================================================
       LIBRARY PAGE
       ========================================================= */

    async function loadStats() {
        try {
            const response = await fetch(`${API.stats}?_=${Date.now()}`);
            if (!response.ok) return;

            const stats = await response.json();
            const tracks = $("statTracks");
            const artists = $("statArtists");
            const albums = $("statAlbums");

            if (tracks) tracks.textContent = stats.tracks || 0;
            if (artists) artists.textContent = stats.artists || 0;
            if (albums) albums.textContent = stats.albums || 0;
        } catch (error) {
            console.warn("loadStats:", error);
        }
    }

    async function loadLibrary() {
        const list = $("libraryList");
        if (!list) return;

        list.innerHTML = `<div class="status-msg">Loading library...</div>`;

        try {
            await refreshLibraryCache();
            await loadStats();
            filterLibrary();
        } catch (error) {
            console.error(error);
            list.innerHTML = `<div class="status-msg">Failed to load library.</div>`;
        }
    }

    function filterLibrary() {
        const list = $("libraryList");
        if (!list) return;

        const search = $("libSearchQuery");
        const query = search ? search.value.toLowerCase().trim() : "";
        const filtered = rawLibraryFiles.filter(file => String(file.name || "").toLowerCase().includes(query));

        if (!filtered.length) {
            list.innerHTML = `
                <div class="status-msg">
                    ${rawLibraryFiles.length ? "No matching tracks found." : "No files downloaded yet."}
                </div>
            `;
            return;
        }

        list.innerHTML = "";

        filtered.forEach(file => {
            const card = document.createElement("div");
            card.className = "result-card";

            const filename = file.name || "";
            const encoded = encodeURIComponent(filename);
            const coverUrl = `/api/library/cover/${encoded}`;
            const streamUrl = `/api/library/stream/${encoded}`;

            card.innerHTML = `
                <div class="thumb-wrapper">
                    <img src="${escapeAttr(coverUrl)}" alt="${escapeAttr(filename)}" loading="lazy">
                </div>
                <div class="track-info">
                    <div class="track-title">${escapeHtml(filename)}</div>
                    <div class="track-artist">📦 ${escapeHtml(file.size || "Unknown size")}</div>
                </div>
                <div class="btn-group">
                    <button type="button" class="btn-preview">▶ Play</button>
                    <button type="button" class="btn-danger">🗑 Delete</button>
                </div>
            `;

            const image = card.querySelector("img");
            if (image) {
                image.addEventListener("error", () => {
                    image.src = "data:image/svg+xml;charset=UTF-8," + encodeURIComponent(`
                        <svg xmlns="http://www.w3.org/2000/svg" width="110" height="65">
                            <rect width="100%" height="100%" fill="#1e293b"/>
                            <text x="50%" y="55%" text-anchor="middle" font-size="24">♪</text>
                        </svg>
                    `);
                }, { once: true });
            }

            const play = card.querySelector(".btn-preview");
            play.addEventListener("click", () => {
                toggleAudioStream(play, streamUrl, "library", filename, "Local Library", coverUrl);
            });

            const del = card.querySelector(".btn-danger");
            del.addEventListener("click", () => {
                deleteFile(filename);
            });

            list.appendChild(card);
        });
    }

    async function deleteFile(filename) {
        if (!confirm(`Delete "${filename}"?`)) return;

        try {
            const response = await fetch(`${API.library}/${encodeURIComponent(filename)}`, { method: "DELETE" });
            if (!response.ok) throw new Error("Delete failed");

            showToast("Track deleted.", "success");
            await refreshLibraryCache();
            await loadStats();
            filterLibrary();
        } catch (error) {
            console.error(error);
            showToast("Failed to delete file.", "error");
        }
    }

    /* =========================================================
       WEBSOCKET
       ========================================================= */

    function initWebSocket() {
        if (socket) {
            try { socket.close(); } catch {}
        }

        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const url = `${protocol}//${window.location.host}/ws`;

        try {
            socket = new WebSocket(url);
        } catch {
            scheduleWebSocketReconnect();
            return;
        }

        socket.addEventListener("open", () => {
            console.log("Music WebSocket connected");
        });

        socket.addEventListener("message", event => {
            try {
                const data = JSON.parse(event.data);
                if (data.type === "task_update") {
                    pollTasks();
                    const downloadsTab = $("tab-downloads");
                    if (downloadsTab && downloadsTab.classList.contains("active")) {
                        loadDownloads();
                    }
                }
            } catch (error) {
                console.warn("Invalid WebSocket message", error);
            }
        });

        socket.addEventListener("close", () => { scheduleWebSocketReconnect(); });
        socket.addEventListener("error", () => { try { socket.close(); } catch {} });
    }

    function scheduleWebSocketReconnect() {
        if (socketReconnectTimer) return;
        socketReconnectTimer = setTimeout(() => {
            socketReconnectTimer = null;
            initWebSocket();
        }, 3000);
    }

    /* =========================================================
       POLLING FALLBACK
       ========================================================= */

    function startTaskPolling() {
        if (pollTimer) clearInterval(pollTimer);
        pollTimer = setInterval(() => { pollTasks(); }, 1500);
    }

    /* =========================================================
       EVENT LISTENERS
       ========================================================= */

    function initEvents() {
        const searchBtn = $("searchBtn");
        const query = $("query");

        if (searchBtn) searchBtn.addEventListener("click", searchMusic);

        if (query) {
            query.addEventListener("keydown", event => {
                if (event.key === "Enter") {
                    event.preventDefault();
                    searchMusic();
                }
                if (event.key === "Escape") {
                    hideSuggestions();
                }
            });

            query.addEventListener("input", () => { showSuggestions(query.value); });
            query.addEventListener("focus", () => { showSuggestions(query.value); });
        }

        const librarySearch = $("libSearchQuery");
        if (librarySearch) librarySearch.addEventListener("input", filterLibrary);

        const settingsSave = $("saveSettingsBtn");
        if (settingsSave) settingsSave.addEventListener("click", saveSettings);

        const themeSelect = $("set_theme");
        if (themeSelect) {
            themeSelect.addEventListener("change", () => {
                toggleTheme(themeSelect.value);
            });
        }

        document.addEventListener("click", event => {
            const queryInput = $("query");
            if (
                suggestionBox &&
                queryInput &&
                !event.target.closest("#searchSuggestions") &&
                event.target !== queryInput
            ) {
                hideSuggestions();
            }
        });

        window.addEventListener("scroll", () => {
            const searchTab = $("tab-search");
            if (!searchTab || !searchTab.classList.contains("active")) return;

            const distance = document.documentElement.scrollHeight - (window.scrollY + window.innerHeight);
            if (distance < 600) {
                loadMoreResults();
            }
        }, { passive: true });
    }

    /* =========================================================
       INITIALIZATION
       ========================================================= */

    async function init() {
        createSuggestionsBox();
        initEvents();
        handleDeepLink();
        await refreshLibraryCache();
        await pollTasks();
        initWebSocket();
        startTaskPolling();

        setInterval(refreshLibraryCache, 30000);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init, { once: true });
    } else {
        init();
    }

    /* =========================================================
       PUBLIC API
       ========================================================= */

    window.searchMusic = searchMusic;
    window.loadMoreResults = loadMoreResults;
    window.loadLibrary = loadLibrary;
    window.loadDownloads = loadDownloads;
    window.filterLibrary = filterLibrary;
    window.deleteFile = deleteFile;
    window.cancelTask = cancelTask;
    window.toggleTheme = toggleTheme;
    window.navigate = navigate;
    window.switchTab = switchTab;
    window.toggleAudioStream = toggleAudioStream;
    window.requestNotificationPermission = requestNotificationPermission;
    window.enqueueDownload = enqueueDownload;

})();
