/* ============================================================
   XROB MUSIC - WEB APP
   Downloader + Library + Player + OpenSubsonic companion UI
   ============================================================ */

"use strict";

/* ============================================================
   STATE
   ============================================================ */

let socket = null;
let socketReconnectTimer = null;
let socketReconnectAttempt = 0;

const completedSet = new Set();

let rawLibraryFiles = [];
const libraryFilesSet = new Set();

let activePreviewBtn = null;
let playerQueue = [];
let playerQueueIndex = -1;

let currentPage = 1;
let currentQuery = "";
let isLoadingMore = false;
let hasMoreResults = true;

let latestTasks = [];
let lastTaskSignature = "";
let firstTaskPoll = true;

let audioContext = null;
let analyser = null;
let sourceNode = null;

/* ============================================================
   DOM
   ============================================================ */

const audio = document.getElementById("global-audio-element");
const player = document.getElementById("global-player-bar");
const playBtn = document.getElementById("gp-play-btn");
const prevBtn = document.getElementById("gp-prev-btn");
const nextBtn = document.getElementById("gp-next-btn");
const seek = document.getElementById("gp-seek");
const volume = document.getElementById("gp-volume");
const curTime = document.getElementById("gp-cur-time");
const durTime = document.getElementById("gp-dur-time");
const playerTitle = document.getElementById("gp-title");
const playerArtist = document.getElementById("gp-artist");
const playerArt = document.getElementById("gp-art");
const canvas = document.getElementById("visualizer-canvas");
const canvasCtx = canvas ? canvas.getContext("2d") : null;

/* ============================================================
   HELPERS
   ============================================================ */

function escapeHtml(value) {
    return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}

function normalizeKey(value) {
    return String(value || "")
        .toLowerCase()
        .replace(
            /\b(official\s*(video|audio|music video)|lyrics?|hd|4k|remaster(ed)?|audio)\b/gi,
            " "
        )
        .replace(/[^a-z0-9]+/g, "");
}

function formatSeconds(seconds) {
    const value = Math.max(0, Math.floor(Number(seconds) || 0));
    return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, "0")}`;
}

async function fetchJson(url, options = {}) {
    const response = await fetch(url, {
        cache: "no-store",
        ...options,
    });

    const contentType = response.headers.get("content-type") || "";
    let data = null;

    if (contentType.includes("application/json")) {
        data = await response.json().catch(() => null);
    } else {
        const text = await response.text().catch(() => "");
        data = text ? { detail: text } : null;
    }

    if (!response.ok) {
        throw new Error(
            data?.detail ||
            data?.message ||
            `Request failed (${response.status})`
        );
    }

    return data;
}

function showToast(message) {
    const container = document.getElementById("toast-container");

    if (!container) {
        return;
    }

    const toast = document.createElement("div");
    toast.className = "toast";
    toast.textContent = message;
    container.appendChild(toast);

    setTimeout(() => toast.remove(), 3500);
}

window.showToast = showToast;

/* ============================================================
   THEME
   ============================================================ */

function toggleTheme(theme) {
    const value = theme === "light" ? "light" : "dark";

    document.documentElement.setAttribute("data-theme", value);
    localStorage.setItem("xrob_music_theme", value);
}

toggleTheme(
    localStorage.getItem("xrob_music_theme") || "dark"
);

/* ============================================================
   LOADING UI
   ============================================================ */

const LOADER_CIRCUMFERENCE = 263.9;

function updateLoadingCircle(type, percent, text = "") {
    const isLibrary = type === "library";
    const isSearch = type === "search";

    const ids = isLibrary
        ? { loading: "libraryLoading", percent: "libraryLoadingPercent", circle: "libraryLoadingCircle", text: "libraryLoadingText" }
        : isSearch
            ? { loading: "searchLoading", percent: "searchLoadingPercent", circle: "searchLoadingCircle", text: "searchLoadingText" }
            : { loading: "recentTracksLoading", percent: "recentLoadingPercent", circle: "recentLoadingCircle", text: "recentLoadingText" };

    const loading = document.getElementById(ids.loading);
    const percentElement = document.getElementById(ids.percent);
    const circle = document.getElementById(ids.circle);
    const textElement = document.getElementById(ids.text);

    if (!loading) {
        return;
    }

    const safePercent = Math.max(0, Math.min(100, Math.round(Number(percent) || 0)));
    loading.style.display = safePercent >= 100 ? "none" : "flex";

    if (percentElement) {
        percentElement.textContent = `${safePercent}%`;
    }

    if (circle) {
        circle.style.strokeDasharray = String(LOADER_CIRCUMFERENCE);
        circle.style.strokeDashoffset = String(
            LOADER_CIRCUMFERENCE * (1 - safePercent / 100)
        );
    }

    if (textElement && text) {
        textElement.textContent = text;
    }
}

function finishLoading(type) {
    updateLoadingCircle(type, 100);
}

/* ============================================================
   NAVIGATION
   ============================================================ */

const VALID_TABS = [
    "home",
    "search",
    "downloads",
    "library",
    "settings",
];

function navigate(tab, updateHash = true) {
    if (!VALID_TABS.includes(tab)) {
        tab = "home";
    }

    if (updateHash) {
        const newHash = `#${tab}`;

        if (location.hash !== newHash) {
            location.hash = tab;
            return;
        }
    }

    switchTab(tab);
}

window.navigate = navigate;

function switchTab(tab) {
    if (!VALID_TABS.includes(tab)) {
        tab = "home";
    }

    document.querySelectorAll(".tab-content").forEach((section) => {
        section.classList.toggle(
            "active",
            section.id === `tab-${tab}`
        );
    });

    document.querySelectorAll(".nav-link").forEach((button) => {
        const isSelected =
            button.id === `btn-${tab}` ||
            button.id === `mob-btn-${tab}`;

        button.classList.toggle("active", isSelected);
    });

    if (tab === "home") {
        loadHome();
    } else if (tab === "downloads") {
        loadDownloads();
    } else if (tab === "library") {
        loadLibrary();
    } else if (tab === "settings") {
        loadSettings();
    }
}

function handleHash() {
    const hash = location.hash.replace(/^#/, "");
    switchTab(VALID_TABS.includes(hash) ? hash : "home");
}

window.addEventListener("hashchange", handleHash);

/* ============================================================
   PLAYER
   ============================================================ */

function updateProgress() {
    if (!audio || !seek || !curTime || !durTime) {
        return;
    }

    if (!Number.isFinite(audio.duration) || audio.duration <= 0) {
        seek.value = 0;
        curTime.textContent = "0:00";
        durTime.textContent = "0:00";
        return;
    }

    const progress = Math.min(100, Math.max(0, (audio.currentTime / audio.duration) * 100));
    seek.value = String(progress);
    curTime.textContent = formatSeconds(audio.currentTime);
    durTime.textContent = formatSeconds(audio.duration);
}

function updatePlayingState(playing) {
    if (playBtn) {
        playBtn.textContent = playing ? "❚❚" : "▶";
    }

    if (activePreviewBtn) {
        activePreviewBtn.classList.toggle("playing", playing);
        activePreviewBtn.textContent = playing
            ? "❚❚ Pause"
            : (activePreviewBtn.dataset.type === "library" ? "▶ Play" : "▶ Preview");
    }

    if (prevBtn) {
        prevBtn.disabled = playerQueueIndex <= 0;
    }

    if (nextBtn) {
        nextBtn.disabled = playerQueueIndex < 0 || playerQueueIndex >= playerQueue.length - 1;
    }
}

function resetPreviewButton(button) {
    if (!button) {
        return;
    }

    button.classList.remove("playing");
    button.textContent = button.dataset.type === "library" ? "▶ Play" : "▶ Preview";
}

function initAudioContext() {
    if (audioContext || !audio) {
        return;
    }

    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) {
        return;
    }

    try {
        audioContext = new AudioContextClass();
        analyser = audioContext.createAnalyser();
        analyser.fftSize = 64;
        analyser.smoothingTimeConstant = 0.8;

        sourceNode = audioContext.createMediaElementSource(audio);
        sourceNode.connect(analyser);
        analyser.connect(audioContext.destination);
        drawVisualizer();
    } catch (error) {
        console.warn("Audio visualizer unavailable:", error);
        audioContext = null;
        analyser = null;
        sourceNode = null;
    }
}

function drawVisualizer() {
    if (!canvasCtx || !analyser || !canvas) {
        return;
    }

    requestAnimationFrame(drawVisualizer);

    const length = analyser.frequencyBinCount;
    const data = new Uint8Array(length);
    analyser.getByteFrequencyData(data);

    canvasCtx.clearRect(0, 0, canvas.width, canvas.height);

    const barWidth = canvas.width / length;
    for (let i = 0; i < length; i += 1) {
        const height = Math.max(2, (data[i] / 255) * canvas.height);
        canvasCtx.fillStyle = "#1ed760";
        canvasCtx.fillRect(
            i * barWidth,
            canvas.height - height,
            Math.max(1, barWidth - 1),
            height
        );
    }
}

function updatePlayerInfo(title, artist, art) {
    if (playerTitle) {
        playerTitle.textContent = title || "Unknown Track";
    }
    if (playerArtist) {
        playerArtist.textContent = artist || "Unknown Artist";
    }
    if (playerArt) {
        playerArt.src = art || "https://via.placeholder.com/60?text=Music";
        playerArt.onerror = () => {
            playerArt.onerror = null;
            playerArt.src = "https://via.placeholder.com/60?text=Music";
        };
    }
}

function setPlayerQueue(queue, currentIndex = -1) {
    playerQueue = Array.isArray(queue) ? queue.filter(Boolean) : [];
    playerQueueIndex = Math.max(-1, Math.min(currentIndex, playerQueue.length - 1));    updatePlayingState(!audio?.paused);
}

function registerPlayerTrack(button, track) {
    if (!button || !track?.url) {
        return;
    }

    button._xrobTrack = track;
}

function syncPlayerQueueIndex(button) {
    if (!button?._xrobTrack) {
        return;
    }

    const index = playerQueue.findIndex(
        (item) => item.button === button
    );

    if (index >= 0) {
        playerQueueIndex = index;

        updatePlayingState(
            !audio?.paused
        );
    }
}

async function toggleAudioStream(
    button,
    url,
    type,
    title,
    artist,
    art
) {
    if (!audio || !button || !url) {
        return;
    }

    initAudioContext();

    if (audioContext?.state === "suspended") {
        await audioContext.resume().catch(
            () => {}
        );
    }

    let absoluteUrl;

    try {
        absoluteUrl = new URL(
            url,
            location.href
        ).href;
    } catch {
        showToast(
            "❌ Invalid audio URL"
        );

        return;
    }

    if (
        activePreviewBtn === button &&
        audio.src === absoluteUrl
    ) {
        try {
            if (audio.paused) {
                await audio.play();
            } else {
                audio.pause();
            }
        } catch (error) {
            console.error(
                "Playback failed:",
                error
            );

            showToast(
                "❌ Playback failed"
            );
        }

        return;
    }

    if (
        activePreviewBtn &&
        activePreviewBtn !== button
    ) {
        resetPreviewButton(
            activePreviewBtn
        );
    }

    activePreviewBtn = button;

    button.dataset.type = type;
    button.textContent = "⏳ Loading...";

    updatePlayerInfo(
        title,
        artist,
        art
    );

    if (button._xrobTrack) {
        syncPlayerQueueIndex(
            button
        );
    }

    if (player) {
        player.style.display = "grid";
    }

    audio.pause();
    audio.removeAttribute("src");
    audio.src = absoluteUrl;
    audio.load();

    try {
        await audio.play();

        button.textContent =
            "❚❚ Pause";

        updatePlayingState(
            true
        );
    } catch (error) {
        console.error(
            "Playback failed:",
            error
        );

        button.textContent =
            "❌ Error";

        showToast(
            "❌ Unable to play this track"
        );

        setTimeout(
            () =>
                resetPreviewButton(
                    button
                ),
            1800
        );
    }
}

function playPlayerQueueIndex(
    index
) {
    if (
        index < 0 ||
        index >= playerQueue.length
    ) {
        return;
    }

    const track =
        playerQueue[index];

    if (
        !track?.url ||
        !track?.button
    ) {
        return;
    }

    playerQueueIndex =
        index;

    toggleAudioStream(
        track.button,
        track.url,
        track.type,
        track.title,
        track.artist,
        track.art
    );
}

function playPreviousTrack() {
    if (
        playerQueueIndex > 0
    ) {
        playPlayerQueueIndex(
            playerQueueIndex - 1
        );
    }
}

function playNextTrack() {
    if (
        playerQueueIndex >= 0 &&
        playerQueueIndex <
            playerQueue.length - 1
    ) {
        playPlayerQueueIndex(
            playerQueueIndex + 1
        );
    }
}

audio?.addEventListener(
    "timeupdate",
    updateProgress
);

audio?.addEventListener(
    "loadedmetadata",
    updateProgress
);

audio?.addEventListener(
    "durationchange",
    updateProgress
);

audio?.addEventListener(
    "play",
    () => {
        updatePlayingState(
            true
        );
    }
);

audio?.addEventListener(
    "pause",
    () => {
        updatePlayingState(
            false
        );
    }
);

audio?.addEventListener(
    "ended",
    () => {
        updatePlayingState(
            false
        );

        if (seek) {
            seek.value = "0";
        }

        if (curTime) {
            curTime.textContent =
                "0:00";
        }

        if (
            playerQueueIndex >= 0 &&
            playerQueueIndex <
                playerQueue.length - 1
        ) {
            playNextTrack();
            return;
        }

        if (activePreviewBtn) {
            resetPreviewButton(
                activePreviewBtn
            );

            activePreviewBtn =
                null;
        }
    }
);

playBtn?.addEventListener(
    "click",
    () => {
        if (!audio) {
            return;
        }

        if (
            !audio.src &&
            playerQueueIndex >= 0
        ) {
            playPlayerQueueIndex(
                playerQueueIndex
            );

            return;
        }

        if (audio.paused) {
            audio.play().catch(
                (error) => {
                    console.error(
                        "Playback failed:",
                        error
                    );

                    showToast(
                        "❌ Playback failed"
                    );
                }
            );
        } else {
            audio.pause();
        }
    }
);

prevBtn?.addEventListener(
    "click",
    playPreviousTrack
);

nextBtn?.addEventListener(
    "click",
    playNextTrack
);

seek?.addEventListener(
    "input",
    () => {
        if (
            !audio ||
            !Number.isFinite(
                audio.duration
            ) ||
            audio.duration <= 0
        ) {
            return;
        }

        audio.currentTime =
            (Number(seek.value) / 100) *
            audio.duration;
    }
);

const savedVolume =
    localStorage.getItem(
        "xrob_music_volume"
    );

if (volume && audio) {
    const parsed =
        Number(savedVolume);

    const initialVolume =
        savedVolume === null
            ? 0.8
            : (
                Number.isFinite(
                    parsed
                )
                    ? Math.min(
                        1,
                        Math.max(
                            0,
                            parsed
                        )
                    )
                    : 0.8
            );

    volume.value =
        String(initialVolume);

    audio.volume =
        initialVolume;

    volume.addEventListener(
        "input",
        () => {
            const value =
                Math.min(
                    1,
                    Math.max(
                        0,
                        Number(
                            volume.value
                        ) || 0
                    )
                );

            audio.volume =
                value;

            localStorage.setItem(
                "xrob_music_volume",
                String(value)
            );
        }
    );
}

/* ============================================================
   SETTINGS
   ============================================================ */

const DEFAULT_CLIENT_SETTINGS = {
    audio_format: "mp3",
    audio_quality: "320K",
    embed_thumbnail: true,
    embed_metadata: true,
    organize_by_artist: false,
    max_results: 20,
    theme: "dark",
};

function getStoredTheme() {
    return localStorage.getItem(
        "xrob_music_theme"
    ) === "light"
        ? "light"
        : "dark";
}

async function loadSettings() {
    const settingsMessage =
        document.getElementById(
            "settingsMsg"
        );

    try {
        const settings =
            await fetchJson(
                "/api/settings"
            );

        const setValue = (
            id,
            value
        ) => {
            const element =
                document.getElementById(
                    id
                );

            if (element) {
                element.value =
                    value ?? "";
            }
        };

        const setChecked = (
            id,
            value
        ) => {
            const element =
                document.getElementById(
                    id
                );

            if (element) {
                element.checked =
                    Boolean(value);
            }
        };

        setValue(
            "set_format",
            settings.audio_format ||
                DEFAULT_CLIENT_SETTINGS.audio_format
        );

        setValue(
            "set_quality",
            settings.audio_quality ||
                DEFAULT_CLIENT_SETTINGS.audio_quality
        );

        setValue(
            "set_max_results",
            settings.max_results ||
                DEFAULT_CLIENT_SETTINGS.max_results
        );

        setChecked(
            "set_thumb",
            settings.embed_thumbnail ??
                DEFAULT_CLIENT_SETTINGS.embed_thumbnail
        );

        setChecked(
            "set_meta",
            settings.embed_metadata ??
                DEFAULT_CLIENT_SETTINGS.embed_metadata
        );

        setChecked(
            "set_organize",
            settings.organize_by_artist ??
                DEFAULT_CLIENT_SETTINGS.organize_by_artist
        );

        setValue(
            "set_subsonic_user",
            settings.subsonic_user ||
                "admin"
        );

        setValue(
            "set_theme",
            getStoredTheme()
        );

        const serverUrl =
            window.XrobArpeggi
                ?.getServerUrl?.() ||
            `${location.protocol}//${location.host}`;

        setValue(
            "amperfy-server-url",
            serverUrl
        );

        if (settingsMessage) {
            settingsMessage.textContent =
                "";
        }
    } catch (error) {
        console.warn(
            "Settings load:",
            error
        );

        if (settingsMessage) {
            settingsMessage.textContent =
                `❌ ${error.message}`;
        }
    }
}

window.loadSettings =
    loadSettings;

function collectSettings() {
    const maxResultsElement =
        document.getElementById(
            "set_max_results"
        );

    let maxResults =
        Number(
            maxResultsElement?.value ??
                DEFAULT_CLIENT_SETTINGS.max_results
        );

    if (
        !Number.isFinite(
            maxResults
        )
    ) {
        maxResults =
            DEFAULT_CLIENT_SETTINGS.max_results;
    }

    maxResults =
        Math.max(
            5,
            Math.min(
                50,
                Math.round(
                    maxResults
                )
            )
        );

    if (maxResultsElement) {
        maxResultsElement.value =
            String(maxResults);
    }

    return {
        audio_format:
            document.getElementById(
                "set_format"
            )?.value ||
            DEFAULT_CLIENT_SETTINGS.audio_format,

        audio_quality:
            document.getElementById(
                "set_quality"
            )?.value ||
            DEFAULT_CLIENT_SETTINGS.audio_quality,

        embed_thumbnail:
            document.getElementById(
                "set_thumb"
            )?.checked ??
            DEFAULT_CLIENT_SETTINGS.embed_thumbnail,

        embed_metadata:
            document.getElementById(
                "set_meta"
            )?.checked ??
            DEFAULT_CLIENT_SETTINGS.embed_metadata,

        organize_by_artist:
            document.getElementById(
                "set_organize"
            )?.checked ??
            DEFAULT_CLIENT_SETTINGS.organize_by_artist,

        max_results:
            maxResults,
    };
}

async function saveSettings() {
    const message =
        document.getElementById(
            "settingsMsg"
        );

    const saveButton =
        document.querySelector(
            ".save-btn"
        );

    const theme =
        document.getElementById(
            "set_theme"
        )?.value === "light"
            ? "light"
            : "dark";

    const data =
        collectSettings();

    if (saveButton) {
        saveButton.disabled =
            true;

        saveButton.dataset
            .originalText =
            saveButton.textContent;

        saveButton.textContent =
            "Saving...";
    }

    try {
        await fetchJson(
            "/api/settings",
            {
                method: "POST",

                headers: {
                    "Content-Type":
                        "application/json",
                },

                body:
                    JSON.stringify(
                        data
                    ),
            }
        );

        toggleTheme(
            theme
        );

        if (message) {
            message.dataset.state =
                "success";

            message.textContent =
                "✅ Settings saved.";
        }

        showToast(
            "✅ Settings saved"
        );
    } catch (error) {
        if (message) {
            message.dataset.state =
                "error";

            message.textContent =
                `❌ ${error.message}`;
        }

        showToast(
            `❌ ${error.message}`
        );
    } finally {
        if (saveButton) {
            saveButton.disabled =
                false;

            saveButton.textContent =
                saveButton.dataset
                    .originalText ||
                "Save Settings";
        }
    }
}

window.saveSettings =
    saveSettings;

function resetSettings() {
    const setValue = (
        id,
        value
    ) => {
        const element =
            document.getElementById(
                id
            );

        if (element) {
            element.value =
                value;
        }
    };

    const setChecked = (
        id,
        value
    ) => {
        const element =
            document.getElementById(
                id
            );

        if (element) {
            element.checked =
                value;
        }
    };

    setValue(
        "set_format",
        DEFAULT_CLIENT_SETTINGS.audio_format
    );

    setValue(
        "set_quality",
        DEFAULT_CLIENT_SETTINGS.audio_quality
    );

    setValue(
        "set_max_results",
        DEFAULT_CLIENT_SETTINGS.max_results
    );

    setChecked(
        "set_thumb",
        DEFAULT_CLIENT_SETTINGS.embed_thumbnail
    );

    setChecked(
        "set_meta",
        DEFAULT_CLIENT_SETTINGS.embed_metadata
    );

    setChecked(
        "set_organize",
        DEFAULT_CLIENT_SETTINGS.organize_by_artist
    );

    setValue(
        "set_theme",
        DEFAULT_CLIENT_SETTINGS.theme
    );

    toggleTheme(
        DEFAULT_CLIENT_SETTINGS.theme
    );

    const message =
        document.getElementById(
            "settingsMsg"
        );

    if (message) {
        message.dataset.state =
            "";

        message.textContent =
            "Defaults loaded. Click Save Settings to apply downloader defaults.";
    }
}

document
    .getElementById(
        "settings-reset"
    )
    ?.addEventListener(
        "click",
        resetSettings
    );

document
    .getElementById(
        "set_theme"
    )
    ?.addEventListener(
        "change",
        (event) => {
            toggleTheme(
                event.target.value ===
                    "light"
                    ? "light"
                    : "dark"
            );
        }
    );

/* ============================================================
   LIBRARY
   ============================================================ */

async function refreshLibraryCache() {
    try {
        const data =
            await fetchJson(
                "/api/library"
            );

        rawLibraryFiles =
            Array.isArray(
                data?.files
            )
                ? data.files
                : [];

        libraryFilesSet.clear();

        rawLibraryFiles.forEach(
            (file) => {
                const name =
                    String(
                        file?.name || ""
                    );

                const slash =
                    name.lastIndexOf(
                        "/"
                    );

                const dot =
                    name.lastIndexOf(
                        "."
                    );

                const base =
                    name.substring(
                        slash + 1,
                        dot > slash
                            ? dot
                            : name.length
                    );

                libraryFilesSet.add(
                    normalizeKey(
                        base
                    )
                );
            }
        );

        const side =
            document.getElementById(
                "sideLibCount"
            );

        if (side) {
            side.textContent =
                String(
                    rawLibraryFiles.length
                );
        }

        const mobile =
            document.getElementById(
                "mobLibCount"
            );

        if (mobile) {
            mobile.textContent =
                String(
                    rawLibraryFiles.length
                );
        }

        const size =
            document.getElementById(
                "libFolderSize"
            );

        if (size) {
            size.textContent =
                data?.total_size ||
                "0 MB";
        }
    } catch (error) {
        console.warn(
            "Library:",
            error
        );
    }
}

async function loadStats() {
    try {
        const stats =
            await fetchJson(
                "/api/stats"
            );

        const values = {
            statTracks:
                stats?.tracks || 0,

            statArtists:
                stats?.artists || 0,

            statAlbums:
                stats?.albums || 0,

            downloadStatTracks:
                stats?.tracks || 0,

            downloadStatAlbums:
                stats?.albums || 0,

            homeTracks:
                stats?.tracks || 0,

            homeArtists:
                stats?.artists || 0,

            homeAlbums:
                stats?.albums || 0,
        };

        Object.entries(
            values
        ).forEach(
            ([id, value]) => {
                const element =
                    document.getElementById(
                        id
                    );

                if (element) {
                    element.textContent =
                        String(
                            value
                        );
                }
            }
        );

        const folderSize =
            document.getElementById(
                "libFolderSize"
            );

        if (
            folderSize &&
            stats?.folder_size
        ) {
            folderSize.textContent =
                stats.folder_size;
        }
    } catch (error) {
        console.warn(
            "Stats:",
            error
        );
    }
}

async function loadLibrary() {
    const list =
        document.getElementById(
            "libraryList"
        );

    if (!list) {
        return;
    }

    updateLoadingCircle(
        "library",
        8,
        "Loading Library..."
    );

    list.innerHTML = "";

    try {
        await refreshLibraryCache();

        updateLoadingCircle(
            "library",
            55,
            "Reading Library..."
        );

        await loadStats();

        updateLoadingCircle(
            "library",
            82,
            "Building Track List..."
        );

        filterLibrary();

        updateLoadingCircle(
            "library",
            100,
            "Ready"
        );
    } catch (error) {
        updateLoadingCircle(
            "library",
            100
        );

        list.innerHTML = `
            <div class="downloads-empty">
                <div class="empty-icon">⚠️</div>
                <div class="empty-title">
                    Unable to load library
                </div>
                <div class="empty-text">
                    ${escapeHtml(
                        error.message ||
                        "Unknown error"
                    )}
                </div>
            </div>
        `;
    }
}

window.loadLibrary =
    loadLibrary;

function filterLibrary() {
    const list =
        document.getElementById(
            "libraryList"
        );

    const input =
        document.getElementById(
            "libSearchQuery"
        );

    if (!list) {
        return;
    }

    const query =
        String(
            input?.value || ""
        )
            .toLowerCase()
            .trim();

    const files =
        rawLibraryFiles.filter(
            (file) =>
                String(
                    file?.name || ""
                )
                    .toLowerCase()
                    .includes(query)
        );

    list.innerHTML = "";

    if (!files.length) {
        list.innerHTML = `
            <div class="downloads-empty">
                <div class="empty-icon">🎵</div>

                <div class="empty-title">
                    ${
                        rawLibraryFiles.length
                            ? "No matching tracks"
                            : "Your library is empty"
                    }
                </div>

                <div class="empty-text">
                    ${
                        rawLibraryFiles.length
                            ? "Try another search."
                            : "Downloaded tracks will appear here."
                    }
                </div>
            </div>
        `;

        return;
    }

    const nextQueue = [];

    files.forEach(
        (file) => {
            const filename =
                String(
                    file.name || ""
                );

            const encoded =
                encodeURIComponent(
                    filename
                );

            const cover =
                `/api/library/cover/${encoded}`;

            const stream =
                `/api/library/stream/${encoded}`;

            const card =
                document.createElement(
                    "article"
                );

            card.className =
                "result-card";

            card.innerHTML = `
                <div class="thumb-wrapper">
                    <img
                        class="library-cover"
                        alt=""
                    >
                </div>

                <div class="track-info">
                    <div class="track-title">
                        ${escapeHtml(
                            filename
                        )}
                    </div>

                    <div class="track-artist">
                        📦 ${
                            escapeHtml(
                                file.size ||
                                "0 MB"
                            )
                        }
                    </div>
                </div>

                <div class="btn-group">
                    <button class="btn-preview">
                        ▶ Play
                    </button>

                    <button class="btn-danger">
                        🗑 Delete
                    </button>
                </div>
            `;

            const image =
                card.querySelector(
                    ".library-cover"
                );

            if (image) {
                image.src =
                    cover;

                image.onerror =
                    () => {
                        image.onerror =
                            null;

                        image.src =
                            "https://via.placeholder.com/100?text=Music";
                    };
            }

            const play =
                card.querySelector(
                    ".btn-preview"
                );

            const remove =
                card.querySelector(
                    ".btn-danger"
                );

            const track = {
                button: play,
                url: stream,
                type: "library",
                title: filename,
                artist: "Local Library",
                art: cover,
            };

            registerPlayerTrack(
                play,
                track
            );

            nextQueue.push(
                track
            );

            play?.addEventListener(
                "click",
                () => {
                    setPlayerQueue(
                        nextQueue,
                        nextQueue.indexOf(
                            track
                        )
                    );

                    toggleAudioStream(
                        play,
                        stream,
                        "library",
                        filename,
                        "Local Library",
                        cover
                    );
                }
            );

            remove?.addEventListener(
                "click",
                () => {
                    deleteFile(
                        filename
                    );
                }
            );

            list.appendChild(
                card
            );
        }
    );

    setPlayerQueue(
        nextQueue,
        nextQueue.findIndex(
            (item) =>
                item.button ===
                activePreviewBtn
        )
    );
}

window.filterLibrary =
    filterLibrary;

async function deleteFile(
    filename
) {
    if (
        !confirm(
            `Delete "${filename}"?`
        )
    ) {
        return;
    }

    try {
        await fetchJson(
            `/api/library/${encodeURIComponent(
                filename
            )}`,
            {
                method: "DELETE",
            }
        );

        if (
            activePreviewBtn &&
            activePreviewBtn.closest(
                ".result-card"
            ) &&
            activePreviewBtn
                .closest(
                    ".result-card"
                )
                ?.querySelector(
                    ".track-title"
                )
                ?.textContent
                ?.trim() === filename
        ) {
            audio?.pause();
            activePreviewBtn =
                null;
        }

        showToast(
            "🗑 Track deleted"
        );

        await loadLibrary();
        await loadHome();
    } catch (error) {
        showToast(
            `❌ ${error.message}`
        );
    }
}

/* ============================================================
   SEARCH
   ============================================================ */

async function searchMusic() {
    const input =
        document.getElementById(
            "query"
        );

    const results =
        document.getElementById(
            "results"
        );

    const status =
        document.getElementById(
            "statusMsg"
        );

    const button =
        document.getElementById(
            "searchBtn"
        );

    const query =
        String(
            input?.value || ""
        ).trim();

    if (!query) {
        if (status) {
            status.textContent =
                "Enter a search term.";
        }

        return;
    }

    currentQuery =
        query;

    currentPage =
        1;

    hasMoreResults =
        true;

    isLoadingMore =
        false;

    if (status) {
        status.textContent =
            "";
    }

    if (results) {
        results.innerHTML =
            "";
    }

    setPlayerQueue([]);

    updateLoadingCircle(
        "search",
        8,
        "Searching..."
    );

    await refreshLibraryCache();

    updateLoadingCircle(
        "search",
        35,
        "Searching YouTube..."
    );

    if (button) {
        button.disabled =
            true;
    }

    try {
        const data =
            await fetchJson(
                `/api/search?q=${encodeURIComponent(
                    query
                )}&page=1`
            );

        updateLoadingCircle(
            "search",
            78,
            "Preparing results..."
        );

        if (
            !Array.isArray(data) ||
            !data.length
        ) {
            updateLoadingCircle(
                "search",
                100,
                "No results"
            );

            if (status) {
                status.textContent =
                    "No results found.";
            }

            hasMoreResults =
                false;

            return;
        }

        if (status) {
            status.textContent =
                "";
        }

        renderItems(
            data
        );

        updateLoadingCircle(
            "search",
            100,
            "Ready"
        );
    } catch (error) {
        updateLoadingCircle(
            "search",
            100
        );

        if (status) {
            status.textContent =
                `❌ ${error.message}`;
        }
    } finally {
        if (button) {
            button.disabled =
                false;
        }
    }
}

function renderItems(
    items
) {
    const results =
        document.getElementById(
            "results"
        );

    if (
        !results ||
        !Array.isArray(items)
    ) {
        return;
    }

    const nextQueue =
        playerQueue.slice();

    items.forEach(
        (item) => {
            const card =
                document.createElement(
                    "article"
                );

            card.className =
                "result-card";

            card.innerHTML = `
                <div class="thumb-wrapper">
                    <img
                        class="search-thumbnail"
                        alt=""
                    >

                    <span class="badge-duration">
                        ${escapeHtml(
                            item?.duration_text ||
                            ""
                        )}
                    </span>
                </div>

                <div class="track-info">
                    <div class="track-title">
                        ${escapeHtml(
                            item?.title ||
                            "Unknown Track"
                        )}
                    </div>

                    <div class="track-artist">
                        👤 ${
                            escapeHtml(
                                item?.channel ||
                                "Unknown Artist"
                            )
                        }
                    </div>
                </div>

                <div class="btn-group"></div>
            `;

            const image =
                card.querySelector(
                    ".search-thumbnail"
                );

            if (image) {
                image.src =
                    item?.thumbnail ||
                    "https://via.placeholder.com/100?text=Music";

                image.onerror =
                    () => {
                        image.onerror =
                            null;

                        image.src =
                            "https://via.placeholder.com/100?text=Music";
                    };
            }

            const group =
                card.querySelector(
                    ".btn-group"
                );

            const itemId =
                String(
                    item?.id || ""
                );

            if (
                libraryFilesSet.has(
                    normalizeKey(
                        item?.title || ""
                    )
                )
            ) {
                group.innerHTML = `
                    <div class="badge-library">
                        ✅ In Library
                    </div>
                `;
            } else if (item?.url) {
                const preview =
                    document.createElement(
                        "button"
                    );

                preview.className =
                    "btn-preview";

                preview.dataset.type =
                    "search";

                preview.textContent =
                    "▶ Preview";

                const track = {
                    button: preview,

                    url:
                        `/api/preview?url=${encodeURIComponent(
                            item.url
                        )}`,

                    type: "search",

                    title:
                        item.title,

                    artist:
                        item.channel,

                    art:
                        item.thumbnail,
                };

                registerPlayerTrack(
                    preview,
                    track
                );

                nextQueue.push(
                    track
                );

                preview.addEventListener(
                    "click",
                    () => {
                        const index =
                            nextQueue.indexOf(
                                track
                            );

                        setPlayerQueue(
                            nextQueue,
                            index
                        );

                        toggleAudioStream(
                            preview,
                            track.url,
                            track.type,
                            track.title,
                            track.artist,
                            track.art
                        );
                    }
                );

                const download =
                    document.createElement(
                        "button"
                    );

                download.className =
                    "btn-download";

                download.dataset.id =
                    itemId;

                download.textContent =
                    "⬇️ Save";

                download.addEventListener(
                    "click",
                    () => {
                        startDownload(
                            item.url,
                            item.title,
                            item.id,
                            item.channel,
                            download
                        );
                    }
                );

                group.appendChild(
                    preview
                );

                group.appendChild(
                    download
                );
            }

            results.appendChild(
                card
            );
        }
    );

    setPlayerQueue(
        nextQueue,
        nextQueue.findIndex(
            (item) =>
                item.button ===
                activePreviewBtn
        )
    );
}async function loadMoreResults() {
    if (
        isLoadingMore ||
        !hasMoreResults ||
        !currentQuery
    ) {
        return;
    }

    isLoadingMore = true;

    const nextPage =
        currentPage + 1;

    const loader =
        document.getElementById(
            "infiniteLoader"
        );

    if (loader) {
        loader.style.display =
            "block";
    }

    try {
        const data =
            await fetchJson(
                `/api/search?q=${encodeURIComponent(
                    currentQuery
                )}&page=${nextPage}`
            );

        if (
            !Array.isArray(data) ||
            !data.length
        ) {
            hasMoreResults =
                false;
        } else {
            currentPage =
                nextPage;

            renderItems(
                data
            );
        }
    } catch (error) {
        console.warn(
            "Load more results:",
            error
        );

        hasMoreResults =
            false;
    } finally {
        if (loader) {
            loader.style.display =
                "none";
        }

        isLoadingMore =
            false;
    }
}

window.loadMoreResults =
    loadMoreResults;

document
    .getElementById(
        "searchBtn"
    )
    ?.addEventListener(
        "click",
        searchMusic
    );

document
    .getElementById(
        "query"
    )
    ?.addEventListener(
        "keydown",
        (event) => {
            if (
                event.key ===
                "Enter"
            ) {
                event.preventDefault();
                searchMusic();
            }
        }
    );

/* ============================================================
   DOWNLOADS
   ============================================================ */

function isActiveTask(task) {
    return [
        "queued",
        "downloading",
        "processing",
    ].includes(
        task?.status
    );
}

function isFinishedTask(task) {
    return [
        "completed",
        "error",
        "failed",
        "cancelled",
        "canceled",
    ].includes(
        task?.status
    );
}

function getTaskStatus(
    status
) {
    const map = {
        queued: [
            "Queued",
            "⏳",
            "status-queued",
        ],

        downloading: [
            "Downloading",
            "⬇️",
            "status-downloading",
        ],

        processing: [
            "Processing",
            "⚙️",
            "status-processing",
        ],

        completed: [
            "Completed",
            "✓",
            "status-completed",
        ],

        error: [
            "Failed",
            "⚠️",
            "status-error",
        ],

        failed: [
            "Failed",
            "⚠️",
            "status-error",
        ],

        cancelled: [
            "Cancelled",
            "✕",
            "status-cancelled",
        ],

        canceled: [
            "Cancelled",
            "✕",
            "status-cancelled",
        ],
    };

    return (
        map[status] ||
        map.queued
    );
}

function updateQueueCounters(
    tasks
) {
    const count =
        tasks.filter(
            isActiveTask
        ).length;

    [
        "queueCount",
        "mobQueueCount",
        "downloadQueueCount",
        "homeDownloads",
    ].forEach(
        (id) => {
            const element =
                document.getElementById(
                    id
                );

            if (element) {
                element.textContent =
                    String(count);
            }
        }
    );
}

function createDownloadCard(
    task,
    position = null
) {
    const [
        label,
        icon,
        statusClass,
    ] =
        getTaskStatus(
            task?.status
        );

    const percent =
        Math.max(
            0,
            Math.min(
                100,
                Math.round(
                    Number(
                        task?.percent
                    ) || 0
                )
            )
        );

    const card =
        document.createElement(
            "article"
        );

    card.className =
        "download-card";

    card.dataset.taskId =
        String(
            task?.id || ""
        );

    const taskId =
        String(
            task?.id || ""
        );

    card.innerHTML = `
        <div class="download-art">
            <div class="download-art-icon">
                🎵
            </div>

            <div class="download-art-overlay">
                ${icon}
            </div>
        </div>

        <div class="download-main">
            <div class="download-top">

                <div>
                    <div class="download-title">
                        ${escapeHtml(
                            task?.title ||
                            "Unknown Track"
                        )}
                    </div>

                    <div class="download-artist">
                        ${escapeHtml(
                            task?.artist ||
                            "Unknown Artist"
                        )}
                    </div>
                </div>

                <div class="download-status-wrap">

                    ${
                        position !== null
                            ? `
                                <span class="queue-position">
                                    #${position}
                                </span>
                              `
                            : ""
                    }

                    <span
                        class="download-status ${statusClass}"
                    >
                        <span class="status-dot"></span>
                        ${label}
                    </span>

                </div>

            </div>

            <div class="download-progress-row">

                <div
                    class="download-progress-track"
                >
                    <div
                        class="download-progress-fill"
                        style="width:${percent}%"
                    ></div>
                </div>

                <span class="download-percent">
                    ${percent}%
                </span>

            </div>

            <div class="download-bottom">

                <div class="download-message">
                    ${escapeHtml(
                        task?.error ||
                        task?.step ||
                        ""
                    )}
                </div>

                <div class="download-meta">
                    ${escapeHtml(
                        task?.speed ||
                        ""
                    )}
                </div>

            </div>
        </div>

        <div class="download-actions"></div>
    `;

    const actions =
        card.querySelector(
            ".download-actions"
        );

    if (
        isActiveTask(task)
    ) {
        const cancel =
            document.createElement(
                "button"
            );

        cancel.className =
            "btn-danger";

        cancel.textContent =
            "✕ Cancel";

        cancel.addEventListener(
            "click",
            () =>
                cancelTask(
                    taskId
                )
        );

        actions.appendChild(
            cancel
        );
    } else {
        const remove =
            document.createElement(
                "button"
            );

        remove.className =
            "download-remove-btn";

        remove.textContent =
            "Remove";

        remove.addEventListener(
            "click",
            () =>
                removeDownloadTask(
                    taskId
                )
        );

        actions.appendChild(
            remove
        );
    }

    return card;
}

function renderDownloads(
    tasks
) {
    const list =
        document.getElementById(
            "downloadsList"
        );

    if (!list) {
        return;
    }

    const active =
        tasks.filter(
            isActiveTask
        );

    const finished =
        tasks.filter(
            isFinishedTask
        );

    list.innerHTML =
        "";

    const activeSection =
        document.createElement(
            "section"
        );

    activeSection.className =
        "downloads-section";

    activeSection.innerHTML = `
        <div class="downloads-section-header">

            <div>

                <div class="downloads-section-title">
                    Active Queue
                </div>

                <div class="downloads-section-subtitle">
                    ${
                        active.length
                            ? "Tracks waiting or downloading"
                            : "Nothing is currently downloading"
                    }
                </div>

            </div>

            <span class="section-count">
                ${active.length}
            </span>

        </div>
    `;

    if (active.length) {

        const stack =
            document.createElement(
                "div"
            );

        stack.className =
            "download-stack";

        active.forEach(
            (
                task,
                index
            ) => {
                stack.appendChild(
                    createDownloadCard(
                        task,
                        index + 1
                    )
                );
            }
        );

        activeSection.appendChild(
            stack
        );

    } else {

        const empty =
            document.createElement(
                "div"
            );

        empty.className =
            "downloads-empty";

        empty.innerHTML = `
            <div class="empty-icon">
                🎧
            </div>

            <div class="empty-title">
                Queue is empty
            </div>

            <div class="empty-text">
                Search for music and press Download.
            </div>

            <button class="save-btn">
                🔍 Search Music
            </button>
        `;

        empty
            .querySelector("button")
            ?.addEventListener(
                "click",
                () =>
                    navigate(
                        "search"
                    )
            );

        activeSection.appendChild(
            empty
        );
    }

    list.appendChild(
        activeSection
    );

    const history =
        document.createElement(
            "section"
        );

    history.className =
        "downloads-section";

    history.innerHTML = `
        <div class="downloads-section-header">

            <div>

                <div class="downloads-section-title">
                    Recent Downloads
                </div>

                <div class="downloads-section-subtitle">
                    Completed and previous jobs
                </div>

            </div>

            <span class="section-count">
                ${finished.length}
            </span>

        </div>
    `;

    if (finished.length) {

        const stack =
            document.createElement(
                "div"
            );

        stack.className =
            "download-stack";

        finished.forEach(
            (task) => {
                stack.appendChild(
                    createDownloadCard(
                        task
                    )
                );
            }
        );

        history.appendChild(
            stack
        );

    } else {

        const empty =
            document.createElement(
                "div"
            );

        empty.className =
            "downloads-history-empty";

        empty.textContent =
            "No completed downloads yet.";

        history.appendChild(
            empty
        );
    }

    list.appendChild(
        history
    );
}

function taskSignature(
    tasks
) {
    return tasks
        .map(
            (task) =>
                [
                    task?.id,
                    task?.status,
                    task?.percent,
                    task?.speed,
                    task?.step,
                    task?.error,
                    task?.last_updated,
                ].join("|")
        )
        .sort()
        .join(";");
}

async function pollTasks(
    force = false
) {
    try {
        const tasks =
            await fetchJson(
                "/api/tasks"
            );

        latestTasks =
            Array.isArray(tasks)
                ? tasks
                : [];

        /*
         * Do not show "Track is ready"
         * for every historical completed
         * task when the page is first opened.
         */

        if (firstTaskPoll) {

            latestTasks.forEach(
                (task) => {
                    if (
                        task?.status ===
                        "completed"
                    ) {
                        completedSet.add(
                            task.id
                        );
                    }
                }
            );

            firstTaskPoll =
                false;

        } else {

            latestTasks.forEach(
                (task) => {

                    if (
                        task?.status ===
                            "completed" &&
                        !completedSet.has(
                            task.id
                        )
                    ) {
                        completedSet.add(
                            task.id
                        );

                        showToast(
                            `🎉 ${
                                task.title ||
                                "Track"
                            } is ready`
                        );
                    }
                }
            );
        }

        updateQueueCounters(
            latestTasks
        );

        const signature =
            taskSignature(
                latestTasks
            );

        if (
            force ||
            signature !==
                lastTaskSignature
        ) {
            renderDownloads(
                latestTasks
            );

            lastTaskSignature =
                signature;
        }

    } catch (error) {

        console.warn(
            "Tasks:",
            error
        );
    }
}

async function loadDownloads() {
    await pollTasks(
        true
    );

    await loadStats();
}

window.loadDownloads =
    loadDownloads;

async function startDownload(
    url,
    title,
    elementId,
    artist,
    button
) {
    if (!url) {
        showToast(
            "❌ Download URL is missing"
        );

        return;
    }

    if (button) {
        button.disabled =
            true;

        button.textContent =
            "⏳ Queuing...";
    }

    try {

        const data =
            await fetchJson(
                "/api/download",
                {
                    method: "POST",

                    headers: {
                        "Content-Type":
                            "application/json",
                    },

                    body:
                        JSON.stringify({
                            url,
                            title,
                            elementId,
                            artist,
                        }),
                }
            );

        showToast(
            data?.status ===
                "already_queued"
                ? "⏳ Already in queue"
                : "⬇️ Added to Downloads"
        );

        navigate(
            "downloads"
        );

        await pollTasks(
            true
        );

    } catch (error) {

        showToast(
            `❌ ${error.message}`
        );

        if (button) {
            button.disabled =
                false;

            button.textContent =
                "⬇️ Save";
        }
    }
}

async function cancelTask(
    taskId
) {
    if (!taskId) {
        return;
    }

    try {

        await fetchJson(
            `/api/tasks/${encodeURIComponent(
                taskId
            )}/cancel`,
            {
                method: "POST",
            }
        );

        showToast(
            "✕ Download cancelled"
        );

        await pollTasks(
            true
        );

    } catch (error) {

        showToast(
            `❌ ${error.message}`
        );
    }
}

window.cancelTask =
    cancelTask;

async function removeDownloadTask(
    taskId
) {
    if (!taskId) {
        return;
    }

    try {

        await fetchJson(
            `/api/tasks/${encodeURIComponent(
                taskId
            )}`,
            {
                method: "DELETE",
            }
        );

        completedSet.delete(
            taskId
        );

        await pollTasks(
            true
        );

        showToast(
            "🗑 Removed from history"
        );

    } catch (error) {

        showToast(
            `❌ ${error.message}`
        );
    }
}

window.removeDownloadTask =
    removeDownloadTask;

async function clearDoneTasks() {
    try {

        const data =
            await fetchJson(
                "/api/tasks/clear-completed",
                {
                    method: "DELETE",
                }
            );

        completedSet.clear();

        latestTasks =
            latestTasks.filter(
                (task) =>
                    !isFinishedTask(
                        task
                    )
            );

        lastTaskSignature =
            "";

        renderDownloads(
            latestTasks
        );

        updateQueueCounters(
            latestTasks
        );

        showToast(
            `🧹 Cleared ${
                data?.count || 0
            } downloads`
        );

    } catch (error) {

        showToast(
            `❌ ${error.message}`
        );
    }
}

window.clearDoneTasks =
    clearDoneTasks;/* ============================================================
   HOME
   ============================================================ */

async function loadHome() {
    updateLoadingCircle(
        "recent",
        8,
        "Loading Recently Added..."
    );

    try {
        const data =
            await fetchJson(
                "/api/home"
            );

        updateLoadingCircle(
            "recent",
            55,
            "Preparing Recently Added..."
        );

        const stats =
            data?.stats || {};

        const setText = (
            id,
            value
        ) => {
            const element =
                document.getElementById(
                    id
                );

            if (element) {
                element.textContent =
                    String(
                        value ?? 0
                    );
            }
        };

        setText(
            "homeTracks",
            stats.tracks || 0
        );

        setText(
            "homeArtists",
            stats.artists || 0
        );

        setText(
            "homeAlbums",
            stats.albums || 0
        );

        setText(
            "homeDownloads",
            data?.active_downloads || 0
        );

        const container =
            document.getElementById(
                "recentTracks"
            );

        if (!container) {
            return;
        }

        container.innerHTML =
            "";

        const recentQueue =
            [];

        const recent =
            Array.isArray(
                data?.recently_added
            )
                ? data.recently_added
                : [];

        if (!recent.length) {
            container.innerHTML = `
                <div class="home-empty">
                    No music in your library yet.
                </div>
            `;

            updateLoadingCircle(
                "recent",
                100,
                "Ready"
            );

            return;
        }

        recent.forEach(
            (track) => {
                const card =
                    document.createElement(
                        "button"
                    );

                card.className =
                    "recent-card";

                card.type =
                    "button";

                card.innerHTML = `
                    <img
                        class="recent-cover"
                        alt=""
                    >

                    <div class="recent-card-title">
                        ${escapeHtml(
                            track?.title ||
                            "Unknown Track"
                        )}
                    </div>

                    <div class="recent-card-artist">
                        ${escapeHtml(
                            track?.artist ||
                            "Unknown Artist"
                        )}
                    </div>
                `;

                const image =
                    card.querySelector(
                        ".recent-cover"
                    );

                if (image) {
                    image.src =
                        track?.cover ||
                        "https://via.placeholder.com/100?text=Music";

                    image.onerror =
                        () => {
                            image.onerror =
                                null;

                            image.src =
                                "https://via.placeholder.com/100?text=Music";
                        };
                }

                const playerTrack = {
                    button: card,

                    url:
                        `/rest/stream.view?id=${encodeURIComponent(
                            track.id
                        )}`,

                    type: "home",

                    title:
                        track.title,

                    artist:
                        track.artist,

                    art:
                        track.cover,
                };

                registerPlayerTrack(
                    card,
                    playerTrack
                );

                recentQueue.push(
                    playerTrack
                );

                card.addEventListener(
                    "click",
                    () => {
                        setPlayerQueue(
                            recentQueue,
                            recentQueue.indexOf(
                                playerTrack
                            )
                        );

                        toggleAudioStream(
                            card,
                            playerTrack.url,
                            playerTrack.type,
                            playerTrack.title,
                            playerTrack.artist,
                            playerTrack.art
                        );
                    }
                );

                container.appendChild(
                    card
                );
            }
        );

        setPlayerQueue(
            recentQueue,
            recentQueue.findIndex(
                (item) =>
                    item.button ===
                    activePreviewBtn
            )
        );

        updateLoadingCircle(
            "recent",
            100,
            "Ready"
        );

    } catch (error) {

        updateLoadingCircle(
            "recent",
            100
        );

        console.warn(
            "Home:",
            error
        );
    }
}

/* ============================================================
   WEBSOCKET
   ============================================================ */

function scheduleWebSocketReconnect() {
    if (
        socketReconnectTimer
    ) {
        return;
    }

    socketReconnectTimer =
        setTimeout(
            () => {
                socketReconnectTimer =
                    null;

                initWebSocket();
            },
            3000
        );
}

function initWebSocket() {

    if (
        socket &&
        (
            socket.readyState ===
                WebSocket.OPEN ||
            socket.readyState ===
                WebSocket.CONNECTING
        )
    ) {
        return;
    }

    const protocol =
        location.protocol ===
        "https:"
            ? "wss:"
            : "ws:";

    try {

        socket =
            new WebSocket(
                `${protocol}//${location.host}/ws`
            );

    } catch (error) {

        console.warn(
            "WebSocket:",
            error
        );

        scheduleWebSocketReconnect();

        return;
    }

    socket.onopen =
        () => {
            socketReconnectAttempt =
                0;
        };

    socket.onmessage =
        (event) => {

            try {

                const data =
                    JSON.parse(
                        event.data
                    );

                if (
                    data?.type ===
                    "task_update"
                ) {
                    pollTasks();
                }

            } catch {
                // Ignore malformed WebSocket messages.
            }
        };

    socket.onerror =
        () => {

            try {
                socket.close();
            } catch {
                // Ignore close errors.
            }
        };

    socket.onclose =
        () => {

            socket =
                null;

            socketReconnectAttempt +=
                1;

            scheduleWebSocketReconnect();
        };
}

/* ============================================================
   INFINITE SCROLL
   ============================================================ */

window.addEventListener(
    "scroll",
    () => {

        const searchTab =
            document.getElementById(
                "tab-search"
            );

        if (
            !searchTab ||
            !searchTab.classList.contains(
                "active"
            )
        ) {
            return;
        }

        if (
            window.innerHeight +
                window.scrollY >=
            document.documentElement
                .scrollHeight -
                500
        ) {
            loadMoreResults();
        }
    },
    {
        passive: true,
    }
);

/* ============================================================
   STARTUP
   ============================================================ */

async function initializeApp() {

    toggleTheme(
        getStoredTheme()
    );

    await Promise.allSettled([
        refreshLibraryCache(),
        loadStats(),
        pollTasks(true),
    ]);

    handleHash();

    initWebSocket();

    setInterval(
        () => {
            pollTasks();
        },
        2000
    );
}

document.addEventListener(
    "DOMContentLoaded",
    initializeApp,
);/* ============================================================
   HOME
   ============================================================ */

async function loadHome() {
    try {
        const data = await fetchJson("/api/home");

        const stats = data?.stats || {};

        const setText = (id, value) => {
            const element = document.getElementById(id);

            if (element) {
                element.textContent = String(value ?? 0);
            }
        };

        setText("homeTracks", stats.tracks || 0);
        setText("homeArtists", stats.artists || 0);
        setText("homeAlbums", stats.albums || 0);

        setText(
            "homeDownloads",
            data?.active_downloads || 0
        );

        const container =
            document.getElementById("recentTracks");

        if (!container) {
            return;
        }

        container.innerHTML = "";

        const recent = Array.isArray(
            data?.recently_added
        )
            ? data.recently_added
            : [];

        if (!recent.length) {
            container.innerHTML = `
                <div class="home-empty">
                    No music in your library yet.
                </div>
            `;

            return;
        }

        recent.forEach((track) => {
            const card =
                document.createElement("button");

            card.className =
                "recent-card";

            card.innerHTML = `
                <img
                    class="recent-cover"
                    alt=""
                >

                <div class="recent-card-title">
                    ${escapeHtml(
                        track?.title ||
                        "Unknown Track"
                    )}
                </div>

                <div class="recent-card-artist">
                    ${escapeHtml(
                        track?.artist ||
                        "Unknown Artist"
                    )}
                </div>
            `;

            const image =
                card.querySelector(
                    ".recent-cover"
                );

            if (image) {
                image.src =
                    track?.cover ||
                    "https://via.placeholder.com/100?text=Music";

                image.onerror = () => {
                    image.onerror = null;

                    image.src =
                        "https://via.placeholder.com/100?text=Music";
                };
            }

            card.addEventListener(
                "click",
                () => {
                    toggleAudioStream(
                        card,

                        `/rest/stream.view?id=${encodeURIComponent(
                            track.id
                        )}`,

                        "home",

                        track.title,

                        track.artist,

                        track.cover
                    );
                }
            );

            container.appendChild(
                card
            );
        });

    } catch (error) {
        console.warn(
            "Home:",
            error
        );
    }
}


/* ============================================================
   WEBSOCKET
   ============================================================ */

function scheduleWebSocketReconnect() {
    if (socketReconnectTimer) {
        return;
    }

    socketReconnectTimer =
        setTimeout(
            () => {
                socketReconnectTimer =
                    null;

                initWebSocket();
            },
            3000
        );
}

function initWebSocket() {
    if (
        socket &&
        (
            socket.readyState ===
                WebSocket.OPEN ||
            socket.readyState ===
                WebSocket.CONNECTING
        )
    ) {
        return;
    }

    const protocol =
        location.protocol ===
        "https:"
            ? "wss:"
            : "ws:";

    try {
        socket =
            new WebSocket(
                `${protocol}//${location.host}/ws`
            );
    } catch (error) {
        console.warn(
            "WebSocket:",
            error
        );

        scheduleWebSocketReconnect();

        return;
    }

    socket.onopen =
        () => {
            socketReconnectAttempt =
                0;
        };

    socket.onmessage =
        (event) => {
            try {
                const data =
                    JSON.parse(
                        event.data
                    );

                if (
                    data?.type ===
                    "task_update"
                ) {
                    pollTasks();
                }

            } catch {
                // Ignore malformed WebSocket messages.
            }
        };

    socket.onerror =
        () => {
            try {
                socket.close();
            } catch {
                // Ignore close errors.
            }
        };

    socket.onclose =
        () => {
            socket = null;

            socketReconnectAttempt +=
                1;

            scheduleWebSocketReconnect();
        };
}


/* ============================================================
   INFINITE SCROLL
   ============================================================ */

window.addEventListener(
    "scroll",
    () => {
        const searchTab =
            document.getElementById(
                "tab-search"
            );

        if (
            !searchTab ||
            !searchTab.classList.contains(
                "active"
            )
        ) {
            return;
        }

        if (
            window.innerHeight +
                window.scrollY >=
            document.documentElement
                .scrollHeight -
                500
        ) {
            loadMoreResults();
        }
    },
    {
        passive: true
    }
);


/* ============================================================
   STARTUP
   ============================================================ */

async function initializeApp() {
    await Promise.allSettled([
        refreshLibraryCache(),
        loadStats(),
        pollTasks(true),
    ]);

    handleHash();

    initWebSocket();

    setInterval(
        () => {
            pollTasks();
        },
        2000
    );
}

document.addEventListener(
    "DOMContentLoaded",
    initializeApp,
);
