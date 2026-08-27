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

    return `${Math.floor(value / 60)}:${String(
        value % 60
    ).padStart(2, "0")}`;
}

async function fetchJson(url, options = {}) {
    const response = await fetch(url, {
        cache: "no-store",
        ...options,
    });

    const contentType =
        response.headers.get("content-type") || "";

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
    const container =
        document.getElementById("toast-container");

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
    const value =
        theme === "light"
            ? "light"
            : "dark";

    document.documentElement.setAttribute(
        "data-theme",
        value
    );

    localStorage.setItem(
        "xrob_music_theme",
        value
    );
}

toggleTheme(
    localStorage.getItem("xrob_music_theme") ||
    "dark"
);

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

    document
        .querySelectorAll(".tab-content")
        .forEach((section) => {
            section.classList.toggle(
                "active",
                section.id === `tab-${tab}`
            );
        });

    document
        .querySelectorAll(".nav-link")
        .forEach((button) => {
            const isSelected =
                button.id === `btn-${tab}` ||
                button.id === `mob-btn-${tab}`;

            button.classList.toggle(
                "active",
                isSelected
            );
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
    const hash =
        location.hash.replace(/^#/, "");

    switchTab(
        VALID_TABS.includes(hash)
            ? hash
            : "home"
    );
}

window.addEventListener(
    "hashchange",
    handleHash
);

/* ============================================================
   PLAYER
   ============================================================ */

function updateProgress() {
    if (
        !audio ||
        !seek ||
        !curTime ||
        !durTime
    ) {
        return;
    }

    if (
        !Number.isFinite(audio.duration) ||
        audio.duration <= 0
    ) {
        seek.value = 0;
        curTime.textContent = "0:00";
        durTime.textContent = "0:00";
        return;
    }

    seek.value = String(
        Math.min(
            100,
            Math.max(
                0,
                (audio.currentTime /
                    audio.duration) *
                    100
            )
        )
    );

    curTime.textContent =
        formatSeconds(audio.currentTime);

    durTime.textContent =
        formatSeconds(audio.duration);
}

function updatePlayingState(playing) {
    if (playBtn) {
        playBtn.textContent =
            playing
                ? "❚❚"
                : "▶";
    }

    if (activePreviewBtn) {
        activePreviewBtn.classList.toggle(
            "playing",
            playing
        );

        if (playing) {
            if (
                activePreviewBtn.dataset.type ===
                    "library" ||
                activePreviewBtn.dataset.type ===
                    "search" ||
                activePreviewBtn.dataset.type ===
                    "home"
            ) {
                activePreviewBtn.textContent =
                    "❚❚ Pause";
            }
        }
    }
}

function resetPreviewButton(button) {
    if (!button) {
        return;
    }

    button.classList.remove("playing");

    button.textContent =
        button.dataset.type === "library"
            ? "▶ Play"
            : "▶ Preview";
}

function initAudioContext() {
    if (audioContext || !audio) {
        return;
    }

    const AudioContextClass =
        window.AudioContext ||
        window.webkitAudioContext;

    if (!AudioContextClass) {
        return;
    }

    try {
        audioContext =
            new AudioContextClass();

        analyser =
            audioContext.createAnalyser();

        analyser.fftSize = 64;
        analyser.smoothingTimeConstant = 0.8;

        sourceNode =
            audioContext.createMediaElementSource(
                audio
            );

        sourceNode.connect(analyser);
        analyser.connect(
            audioContext.destination
        );

        drawVisualizer();
    } catch (error) {
        console.warn(
            "Audio visualizer unavailable:",
            error
        );

        audioContext = null;
        analyser = null;
        sourceNode = null;
    }
}

function drawVisualizer() {
    if (
        !canvasCtx ||
        !analyser ||
        !canvas
    ) {
        return;
    }

    requestAnimationFrame(
        drawVisualizer
    );

    const length =
        analyser.frequencyBinCount;

    const data =
        new Uint8Array(length);

    analyser.getByteFrequencyData(data);

    canvasCtx.clearRect(
        0,
        0,
        canvas.width,
        canvas.height
    );

    const barWidth =
        canvas.width / length;

    for (
        let i = 0;
        i < length;
        i += 1
    ) {
        const height = Math.max(
            2,
            (data[i] / 255) *
                canvas.height
        );

        canvasCtx.fillStyle =
            "#1ed760";

        canvasCtx.fillRect(
            i * barWidth,
            canvas.height - height,
            Math.max(
                1,
                barWidth - 1
            ),
            height
        );
    }
}

function updatePlayerInfo(
    title,
    artist,
    art
) {
    if (playerTitle) {
        playerTitle.textContent =
            title ||
            "Unknown Track";
    }

    if (playerArtist) {
        playerArtist.textContent =
            artist ||
            "Unknown Artist";
    }

    if (playerArt) {
        playerArt.src =
            art ||
            "https://via.placeholder.com/60?text=Music";
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
    if (
        !audio ||
        !button ||
        !url
    ) {
        return;
    }

    initAudioContext();

    if (
        audioContext &&
        audioContext.state ===
            "suspended"
    ) {
        await audioContext
            .resume()
            .catch(() => {});
    }

    let absoluteUrl;

    try {
        absoluteUrl =
            new URL(
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

        return;
    }

    if (activePreviewBtn) {
        resetPreviewButton(
            activePreviewBtn
        );
    }

    activePreviewBtn = button;

    button.dataset.type = type;
    button.textContent =
        "⏳ Loading...";

    updatePlayerInfo(
        title,
        artist,
        art
    );

    if (player) {
        player.style.display =
            "grid";
    }

    audio.pause();
    audio.removeAttribute("src");
    audio.src = absoluteUrl;
    audio.load();

    try {
        await audio.play();

        button.textContent =
            "❚❚ Pause";
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

        setTimeout(() => {
            resetPreviewButton(
                button
            );
        }, 1800);
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
        updatePlayingState(true);
    }
);

audio?.addEventListener(
    "pause",
    () => {
        updatePlayingState(false);
    }
);

audio?.addEventListener(
    "ended",
    () => {
        updatePlayingState(false);

        if (seek) {
            seek.value = "0";
        }

        if (curTime) {
            curTime.textContent =
                "0:00";
        }

        if (activePreviewBtn) {
            resetPreviewButton(
                activePreviewBtn
            );

            activePreviewBtn = null;
        }
    }
);

playBtn?.addEventListener(
    "click",
    () => {
        if (!audio) {
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
            (Number(seek.value) /
                100) *
            audio.duration;
    }
);

const savedVolume =
    localStorage.getItem(
        "xrob_music_volume"
    );

if (volume && audio) {
    const initialVolume =
        savedVolume === null
            ? 0.8
            : Math.min(
                  1,
                  Math.max(
                      0,
                      Number(
                          savedVolume
                      )
                  )
              );

    volume.value = String(
        Number.isFinite(
            initialVolume
        )
            ? initialVolume
            : 0.8
    );

    audio.volume =
        Number(volume.value);

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
                        )
                    )
                );

            audio.volume = value;

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

async function loadSettings() {
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
                "mp3"
        );

        setValue(
            "set_quality",
            settings.audio_quality ||
                "320K"
        );

        setValue(
            "set_max_results",
            settings.max_results ||
                20
        );

        setChecked(
            "set_thumb",
            settings.embed_thumbnail
        );

        setChecked(
            "set_meta",
            settings.embed_metadata
        );

        setChecked(
            "set_organize",
            settings.organize_by_artist
        );

        setValue(
            "set_subsonic_user",
            settings.subsonic_user ||
                "admin"
        );

        const serverUrl =
            window.XrobArpeggi
                ?.getServerUrl?.();

        setValue(
            "amperfy-server-url",
            serverUrl ||
                `${location.protocol}//${location.host}`
        );
    } catch (error) {
        console.warn(
            "Settings load:",
            error
        );
    }
}

window.loadSettings =
    loadSettings;

async function saveSettings() {
    const maxResultsElement =
        document.getElementById(
            "set_max_results"
        );

    let maxResults = Number(
        maxResultsElement?.value ||
            20
    );

    if (
        !Number.isFinite(
            maxResults
        )
    ) {
        maxResults = 20;
    }

    maxResults = Math.max(
        5,
        Math.min(
            50,
            Math.round(
                maxResults
            )
        )
    );

    const data = {
        audio_format:
            document.getElementById(
                "set_format"
            )?.value ||
            "mp3",

        audio_quality:
            document.getElementById(
                "set_quality"
            )?.value ||
            "320K",

        embed_thumbnail:
            document.getElementById(
                "set_thumb"
            )?.checked ??
            true,

        embed_metadata:
            document.getElementById(
                "set_meta"
            )?.checked ??
            true,

        organize_by_artist:
            document.getElementById(
                "set_organize"
            )?.checked ??
            false,

        max_results:
            maxResults,
    };

    const message =
        document.getElementById(
            "settingsMsg"
        );

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

        if (message) {
            message.textContent =
                "✅ Settings saved.";
        }

        showToast(
            "✅ Settings saved"
        );
    } catch (error) {
        if (message) {
            message.textContent =
                `❌ ${error.message}`;
        }

        showToast(
            `❌ ${error.message}`
        );
    }
}

window.saveSettings =
    saveSettings;

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
                        file?.name ||
                            ""
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

    list.innerHTML =
        '<div class="library-loading">Loading library...</div>';

    await refreshLibraryCache();
    await loadStats();
    filterLibrary();
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
                    .includes(
                        query
                    )
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
                    <img class="library-cover" alt="">
                </div>

                <div class="track-info">
                    <div class="track-title">
                        ${escapeHtml(filename)}
                    </div>

                    <div class="track-artist">
                        📦 ${escapeHtml(file.size || "0 MB")}
                    </div>
                </div>

                <div class="btn-group">
                    <button class="btn-preview">▶ Play</button>
                    <button class="btn-danger">🗑 Delete</button>
                </div>
            `;

            const image =
                card.querySelector(
                    ".library-cover"
                );

            if (image) {
                image.src = cover;

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

            play?.addEventListener(
                "click",
                () => {
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

            list.appendChild(card);
        }
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
            `/api/library/${encodeURIComponent(filename)}`,
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
                ?.trim() ===
                filename
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

    currentQuery = query;
    currentPage = 1;
    hasMoreResults = true;
    isLoadingMore = false;

    if (status) {
        status.textContent =
            "🔍 Searching...";
    }

    if (results) {
        results.innerHTML = "";
    }

    await refreshLibraryCache();

    if (button) {
        button.disabled = true;
    }

    try {
        const data =
            await fetchJson(
                `/api/search?q=${encodeURIComponent(query)}&page=1`
            );

        if (
            !Array.isArray(data) ||
            !data.length
        ) {
            if (status) {
                status.textContent =
                    "No results found.";
            }

            hasMoreResults = false;
            return;
        }

        if (status) {
            status.textContent =
                "";
        }

        renderItems(data);
    } catch (error) {
        if (status) {
            status.textContent =
                `❌ ${error.message}`;
        }
    } finally {
        if (button) {
            button.disabled = false;
        }
    }
}

function renderItems(items) {
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
                    <img class="search-thumbnail" alt="">
                    <span class="badge-duration">
                        ${escapeHtml(item?.duration_text || "")}
                    </span>
                </div>

                <div class="track-info">
                    <div class="track-title">
                        ${escapeHtml(item?.title || "Unknown Track")}
                    </div>

                    <div class="track-artist">
                        👤 ${escapeHtml(item?.channel || "Unknown Artist")}
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

            const buttonGroup =
                card.querySelector(
                    ".btn-group"
                );

            const preview =
                document.createElement(
                    "button"
                );

            preview.className =
                "btn-preview";

            preview.textContent =
                "▶ Preview";

            preview.dataset.type =
                "search";

            preview.addEventListener(
                "click",
                () => {
                    toggleAudioStream(
                        preview,
                        item?.preview_url ||
                            item?.url ||
                            "",
                        "search",
                        item?.title ||
                            "Unknown Track",
                        item?.channel ||
                            "Unknown Artist",
                        item?.thumbnail ||
                            ""
                    );
                }
            );

            const download =
                document.createElement(
                    "button"
                );

            download.className =
                "btn-download";

            download.textContent =
                "⬇️ Save";

            download.addEventListener(
                "click",
                () => {
                    startDownload(
                        item?.url,
                        item?.title ||
                            "Unknown Track",
                        item?.id ||
                            item?.elementId ||
                            "",
                        item?.channel ||
                            "",
                        download
                    );
                }
            );

            buttonGroup?.appendChild(
                preview
            );

            buttonGroup?.appendChild(
                download
            );

            results.appendChild(
                card
            );
        }
    );
}/* ============================================================
   SEARCH - DOWNLOAD
   ============================================================ */

async function startDownload(
    url,
    title,
    videoId,
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
        button.disabled = true;
        button.dataset.originalText =
            button.textContent;
        button.textContent =
            "⏳ Starting...";
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
                            title:
                                title ||
                                "Unknown Track",
                            video_id:
                                videoId ||
                                "",
                            artist:
                                artist ||
                                "",
                        }),
                }
            );

        showToast(
            `⬇️ ${
                data?.message ||
                "Download started"
            }`
        );

        await loadDownloads();
        updateQueueCounts();

        if (button) {
            button.textContent =
                "✓ Queued";
        }

        setTimeout(() => {
            if (button) {
                button.disabled =
                    false;

                button.textContent =
                    button.dataset
                        .originalText ||
                    "⬇️ Save";
            }
        }, 2500);
    } catch (error) {
        console.error(
            "Download start failed:",
            error
        );

        showToast(
            `❌ ${error.message}`
        );

        if (button) {
            button.disabled =
                false;

            button.textContent =
                button.dataset
                    .originalText ||
                "⬇️ Save";
        }
    }
}

window.startDownload =
    startDownload;

/* ============================================================
   INFINITE SEARCH
   ============================================================ */

async function loadMoreResults() {
    if (
        isLoadingMore ||
        !hasMoreResults ||
        !currentQuery
    ) {
        return;
    }

    isLoadingMore = true;

    const loader =
        document.getElementById(
            "infiniteLoader"
        );

    if (loader) {
        loader.style.display =
            "block";
    }

    const nextPage =
        currentPage + 1;

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
            hasMoreResults = false;
            return;
        }

        currentPage = nextPage;

        renderItems(data);

        if (
            data.length < 1
        ) {
            hasMoreResults =
                false;
        }
    } catch (error) {
        console.warn(
            "Infinite search:",
            error
        );
    } finally {
        isLoadingMore = false;

        if (loader) {
            loader.style.display =
                "none";
        }
    }
}

/* ============================================================
   DOWNLOADS
   ============================================================ */

function taskIsActive(task) {
    return [
        "queued",
        "downloading",
        "processing",
    ].includes(
        String(
            task?.status || ""
        ).toLowerCase()
    );
}

function taskIsDone(task) {
    return (
        String(
            task?.status || ""
        ).toLowerCase() ===
        "done"
    );
}

function taskIsFailed(task) {
    return [
        "failed",
        "error",
    ].includes(
        String(
            task?.status || ""
        ).toLowerCase()
    );
}

function taskIsCancelled(task) {
    return (
        String(
            task?.status || ""
        ).toLowerCase() ===
        "cancelled"
    );
}

function getTaskId(task) {
    return String(
        task?.id ||
        task?.task_id ||
        ""
    );
}

function getTaskTitle(task) {
    return (
        task?.title ||
        task?.name ||
        "Unknown Track"
    );
}

function getTaskArtist(task) {
    return (
        task?.artist ||
        task?.channel ||
        ""
    );
}

function getTaskProgress(task) {
    const value = Number(
        task?.progress ??
        task?.percent ??
        0
    );

    if (!Number.isFinite(value)) {
        return 0;
    }

    return Math.max(
        0,
        Math.min(
            100,
            value
        )
    );
}

function getTaskStatusLabel(
    task
) {
    const status =
        String(
            task?.status || ""
        ).toLowerCase();

    if (status === "queued") {
        return "Queued";
    }

    if (
        status === "downloading"
    ) {
        return "Downloading";
    }

    if (
        status === "processing"
    ) {
        return "Processing";
    }

    if (status === "done") {
        return "Completed";
    }

    if (
        status === "cancelled"
    ) {
        return "Cancelled";
    }

    if (
        status === "failed" ||
        status === "error"
    ) {
        return "Failed";
    }

    return (
        task?.status ||
        "Unknown"
    );
}

function buildTaskSignature(
    tasks
) {
    return tasks
        .map(
            (task) =>
                [
                    getTaskId(task),
                    task?.status,
                    task?.progress,
                    task?.message,
                    task?.error,
                ].join("|")
        )
        .join(";");
}

async function loadDownloads() {
    const list =
        document.getElementById(
            "downloadsList"
        );

    try {
        const data =
            await fetchJson(
                "/api/downloads"
            );

        latestTasks =
            Array.isArray(data)
                ? data
                : Array.isArray(
                      data?.tasks
                  )
                ? data.tasks
                : [];

        renderDownloads(
            latestTasks
        );

        updateQueueCounts();

        const signature =
            buildTaskSignature(
                latestTasks
            );

        if (
            !firstTaskPoll &&
            signature !==
                lastTaskSignature
        ) {
            handleTaskChanges(
                latestTasks
            );
        }

        lastTaskSignature =
            signature;

        firstTaskPoll = false;

        return latestTasks;
    } catch (error) {
        console.warn(
            "Downloads:",
            error
        );

        if (list) {
            list.innerHTML = `
                <div class="downloads-empty">
                    <div class="empty-icon">⚠️</div>
                    <div class="empty-title">
                        Unable to load downloads
                    </div>
                    <div class="empty-text">
                        ${escapeHtml(
                            error.message
                        )}
                    </div>
                </div>
            `;
        }

        return [];
    }
}

window.loadDownloads =
    loadDownloads;

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

    list.innerHTML = "";

    if (!tasks.length) {
        list.innerHTML = `
            <div class="downloads-empty">
                <div class="empty-icon">⬇️</div>
                <div class="empty-title">
                    No downloads
                </div>
                <div class="empty-text">
                    Start a download from Search Music.
                </div>
            </div>
        `;

        return;
    }

    tasks.forEach(
        (task) => {
            const id =
                getTaskId(task);

            const title =
                getTaskTitle(task);

            const artist =
                getTaskArtist(task);

            const progress =
                getTaskProgress(
                    task
                );

            const status =
                getTaskStatusLabel(
                    task
                );

            const active =
                taskIsActive(
                    task
                );

            const done =
                taskIsDone(task);

            const failed =
                taskIsFailed(
                    task
                );

            const cancelled =
                taskIsCancelled(
                    task
                );

            const item =
                document.createElement(
                    "div"
                );

            item.className =
                "download-item";

            item.dataset.taskId =
                id;

            item.innerHTML = `
                <div class="download-item-main">

                    <div class="download-icon">
                        ${
                            done
                                ? "✅"
                                : failed
                                ? "❌"
                                : cancelled
                                ? "🚫"
                                : active
                                ? "⬇️"
                                : "🎵"
                        }
                    </div>

                    <div class="download-info">

                        <div class="download-title">
                            ${escapeHtml(
                                title
                            )}
                        </div>

                        <div class="download-artist">
                            ${escapeHtml(
                                artist
                            )}
                        </div>

                        <div class="download-status">
                            ${escapeHtml(
                                status
                            )}
                            ${
                                task?.message
                                    ? ` — ${escapeHtml(
                                          task.message
                                      )}`
                                    : ""
                            }
                        </div>

                        ${
                            active
                                ? `
                            <div class="progress-wrapper">
                                <div class="progress-bar">
                                    <div
                                        class="progress-fill"
                                        style="width:${progress}%"
                                    ></div>
                                </div>

                                <span class="progress-percent">
                                    ${Math.round(
                                        progress
                                    )}%
                                </span>
                            </div>
                        `
                                : ""
                        }

                    </div>

                    <div class="download-actions"></div>

                </div>
            `;

            const actions =
                item.querySelector(
                    ".download-actions"
                );

            if (active && id) {
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
                    () => {
                        cancelDownload(
                            id
                        );
                    }
                );

                actions?.appendChild(
                    cancel
                );
            }

            if (done) {
                const download =
                    document.createElement(
                        "button"
                    );

                download.className =
                    "btn-preview";

                download.textContent =
                    "⬇️ Open";

                download.addEventListener(
                    "click",
                    () => {
                        openCompletedTask(
                            task
                        );
                    }
                );

                actions?.appendChild(
                    download
                );
            }

            list.appendChild(
                item
            );
        }
    );
}

function openCompletedTask(
    task
) {
    const url =
        task?.download_url ||
        task?.file_url ||
        task?.url;

    if (!url) {
        showToast(
            "ℹ️ The completed file is already in your library."
        );

        navigate("library");

        return;
    }

    window.open(
        url,
        "_blank",
        "noopener"
    );
}

async function cancelDownload(
    taskId
) {
    if (!taskId) {
        return;
    }

    try {
        await fetchJson(
            `/api/download/${encodeURIComponent(
                taskId
            )}/cancel`,
            {
                method: "POST",
            }
        );

        showToast(
            "🚫 Download cancelled"
        );

        await loadDownloads();
    } catch (error) {
        showToast(
            `❌ ${error.message}`
        );
    }
}

window.cancelDownload =
    cancelDownload;

async function clearDoneTasks() {
    const doneTasks =
        latestTasks.filter(
            (task) =>
                taskIsDone(task) ||
                taskIsFailed(task) ||
                taskIsCancelled(task)
        );

    if (!doneTasks.length) {
        showToast(
            "ℹ️ Nothing to clear"
        );

        return;
    }

    if (
        !confirm(
            `Clear ${doneTasks.length} completed download record(s)?`
        )
    ) {
        return;
    }

    try {
        /*
         * The backend owns task cleanup.
         * Try the bulk endpoint first.
         */
        const response =
            await fetch(
                "/api/downloads/clear",
                {
                    method: "POST",
                    cache: "no-store",
                }
            );

        if (!response.ok) {
            /*
             * Some backend versions only expose
             * per-task deletion. Fall back to that.
             */
            for (
                const task of doneTasks
            ) {
                const id =
                    getTaskId(task);

                if (!id) {
                    continue;
                }

                await fetch(
                    `/api/download/${encodeURIComponent(
                        id
                    )}`,
                    {
                        method: "DELETE",
                        cache: "no-store",
                    }
                ).catch(
                    () => {}
                );
            }
        }

        completedSet.clear();

        showToast(
            "🧹 Completed downloads cleared"
        );

        await loadDownloads();
    } catch (error) {
        showToast(
            `❌ ${error.message}`
        );
    }
}

window.clearDoneTasks =
    clearDoneTasks;

/* ============================================================
   DOWNLOAD NOTIFICATIONS
   ============================================================ */

function handleTaskChanges(
    tasks
) {
    tasks.forEach(
        (task) => {
            const id =
                getTaskId(task);

            if (!id) {
                return;
            }

            if (
                taskIsDone(task) &&
                !completedSet.has(id)
            ) {
                completedSet.add(id);

                showToast(
                    `✅ Download completed: ${getTaskTitle(
                        task
                    )}`
                );

                refreshLibraryCache();
                loadStats();
            }

            if (
                taskIsFailed(task)
            ) {
                const errorKey =
                    `${id}:failed`;

                if (
                    !completedSet.has(
                        errorKey
                    )
                ) {
                    completedSet.add(
                        errorKey
                    );

                    showToast(
                        `❌ Download failed: ${getTaskTitle(
                            task
                        )}`
                    );
                }
            }
        }
    );
}

function updateQueueCounts() {
    const activeCount =
        latestTasks.filter(
            taskIsActive
        ).length;

    const ids = [
        "queueCount",
        "mobQueueCount",
        "downloadQueueCount",
        "homeDownloads",
    ];

    ids.forEach(
        (id) => {
            const element =
                document.getElementById(
                    id
                );

            if (element) {
                element.textContent =
                    String(
                        activeCount
                    );
            }
        }
    );
}

/* ============================================================
   HOME
   ============================================================ */

async function loadHome() {
    try {
        const data =
            await fetchJson(
                "/api/home"
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
            stats.tracks ||
                data?.tracks ||
                0
        );

        setText(
            "homeArtists",
            stats.artists ||
                data?.artists ||
                0
        );

        setText(
            "homeAlbums",
            stats.albums ||
                data?.albums ||
                0
        );

        const active =
            Array.isArray(
                latestTasks
            )
                ? latestTasks.filter(
                      taskIsActive
                  ).length
                : 0;

        setText(
            "homeDownloads",
            active
        );

        const recent =
            Array.isArray(
                data?.recent
            )
                ? data.recent
                : Array.isArray(
                      data?.songs
                  )
                ? data.songs
                : [];

        renderRecentTracks(
            recent
        );
    } catch (error) {
        console.warn(
            "Home:",
            error
        );

        /*
         * Home should still work if the optional
         * combined endpoint fails.
         */
        await loadStats();
    }
}

function renderRecentTracks(
    tracks
) {
    const container =
        document.getElementById(
            "recentTracks"
        );

    if (!container) {
        return;
    }

    container.innerHTML = "";

    if (!tracks.length) {
        container.innerHTML = `
            <div class="downloads-empty">
                <div class="empty-icon">🎵</div>
                <div class="empty-title">
                    No music yet
                </div>
                <div class="empty-text">
                    Download your first track to see it here.
                </div>
            </div>
        `;

        return;
    }

    tracks.forEach(
        (track) => {
            const card =
                document.createElement(
                    "article"
                );

            card.className =
                "recent-card";

            const id =
                track?.id ||
                track?.path ||
                "";

            const cover =
                track?.cover ||
                track?.thumbnail ||
                "https://via.placeholder.com/160?text=Music";

            const stream =
                track?.stream ||
                (
                    track?.id
                        ? `/rest/stream?id=${encodeURIComponent(
                              track.id
                          )}`
                        : ""
                );

            card.innerHTML = `
                <div class="recent-cover-wrap">
                    <img
                        class="recent-cover"
                        src="${escapeHtml(
                            cover
                        )}"
                        alt=""
                    >
                </div>

                <div class="recent-title">
                    ${escapeHtml(
                        track?.title ||
                            "Unknown Track"
                    )}
                </div>

                <div class="recent-artist">
                    ${escapeHtml(
                        track?.artist ||
                            "Unknown Artist"
                    )}
                </div>

                <button class="btn-preview recent-play">
                    ▶ Play
                </button>
            `;

            const image =
                card.querySelector(
                    ".recent-cover"
                );

            image?.addEventListener(
                "error",
                () => {
                    image.src =
                        "https://via.placeholder.com/160?text=Music";
                },
                {
                    once: true,
                }
            );

            const button =
                card.querySelector(
                    ".recent-play"
                );

            button?.addEventListener(
                "click",
                () => {
                    if (!stream) {
                        showToast(
                            "❌ Track stream is unavailable"
                        );

                        return;
                    }

                    toggleAudioStream(
                        button,
                        stream,
                        "home",
                        track?.title,
                        track?.artist,
                        cover
                    );
                }
            );

            container.appendChild(
                card
            );
        }
    );
}

/* ============================================================
   QUEUE / POLLING
   ============================================================ */

let downloadPollTimer = null;

function startDownloadPolling() {
    if (downloadPollTimer) {
        return;
    }

    downloadPollTimer =
        setInterval(
            () => {
                loadDownloads().catch(
                    () => {}
                );
            },
            2500
        );
}

function stopDownloadPolling() {
    if (!downloadPollTimer) {
        return;
    }

    clearInterval(
        downloadPollTimer
    );

    downloadPollTimer =
        null;
}

/* ============================================================
   WEBSOCKET
   ============================================================ */

function getWebSocketUrl() {
    const protocol =
        location.protocol ===
        "https:"
            ? "wss:"
            : "ws:";

    return `${protocol}//${location.host}/ws`;
}

function connectWebSocket() {
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

    if (
        !("WebSocket" in window)
    ) {
        return;
    }

    try {
        socket =
            new WebSocket(
                getWebSocketUrl()
            );
    } catch (error) {
        console.warn(
            "WebSocket:",
            error
        );

        scheduleSocketReconnect();

        return;
    }

    socket.addEventListener(
        "open",
        () => {
            socketReconnectAttempt =
                0;

            if (
                socketReconnectTimer
            ) {
                clearTimeout(
                    socketReconnectTimer
                );

                socketReconnectTimer =
                    null;
            }

            console.info(
                "Xrob Music WebSocket connected"
            );
        }
    );

    socket.addEventListener(
        "message",
        (event) => {
            handleSocketMessage(
                event.data
            );
        }
    );

    socket.addEventListener(
        "close",
        () => {
            socket = null;

            scheduleSocketReconnect();
        }
    );

    socket.addEventListener(
        "error",
        (error) => {
            console.warn(
                "WebSocket error:",
                error
            );

            /*
             * close() causes the reconnect path
             * to run exactly once.
             */
            try {
                socket?.close();
            } catch {
                // Ignore close errors.
            }
        }
    );
}

function scheduleSocketReconnect() {
    if (
        socketReconnectTimer
    ) {
        return;
    }

    socketReconnectAttempt += 1;

    const delay =
        Math.min(
            30000,
            1000 *
                Math.pow(
                    2,
                    Math.min(
                        5,
                        socketReconnectAttempt -
                            1
                    )
                )
        );

    socketReconnectTimer =
        setTimeout(
            () => {
                socketReconnectTimer =
                    null;

                connectWebSocket();
            },
            delay
        );
}

function handleSocketMessage(
    raw
) {
    let message;

    try {
        message =
            typeof raw ===
            "string"
                ? JSON.parse(raw)
                : raw;
    } catch {
        return;
    }

    if (!message) {
        return;
    }

    /*
     * Backend implementations may send either:
     * {type:"download", task:{...}}
     * or the task directly.
     */
    const task =
        message.task ||
        message.data ||
        (
            message.id &&
            message.status
                ? message
                : null
        );

    if (
        message.type ===
            "download" &&
        task
    ) {
        mergeSocketTask(task);

        return;
    }

    if (
        message.type ===
            "downloads" &&
        Array.isArray(
            message.tasks
        )
    ) {
        latestTasks =
            message.tasks;

        renderDownloads(
            latestTasks
        );

        updateQueueCounts();

        return;
    }

    if (
        task &&
        task.status
    ) {
        mergeSocketTask(task);
    }
}

function mergeSocketTask(
    task
) {
    const id =
        getTaskId(task);

    if (!id) {
        return;
    }

    const index =
        latestTasks.findIndex(
            (item) =>
                getTaskId(item) ===
                id
        );

    if (index >= 0) {
        latestTasks[
            index
        ] = {
            ...latestTasks[
                index
            ],
            ...task,
        };
    } else {
        latestTasks.push(
            task
        );
    }

    renderDownloads(
        latestTasks
    );

    updateQueueCounts();

    handleTaskChanges([
        task,
    ]);
}

/* ============================================================
   SEARCH EVENTS
   ============================================================ */

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
   INFINITE SCROLL
   ============================================================ */

window.addEventListener(
    "scroll",
    () => {
        if (
            isLoadingMore ||
            !hasMoreResults ||
            !currentQuery
        ) {
            return;
        }

        const distance =
            document.documentElement
                .scrollHeight -
            (
                window.innerHeight +
                window.scrollY
            );

        if (
            distance <
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
   COPY OPEN SUBSONIC URL
   ============================================================ */

document
    .getElementById(
        "amperfy-copy-url"
    )
    ?.addEventListener(
        "click",
        async () => {
            const input =
                document.getElementById(
                    "amperfy-server-url"
                );

            const value =
                input?.value || "";

            if (!value) {
                showToast(
                    "❌ Server URL is empty"
                );

                return;
            }

            try {
                await navigator.clipboard.writeText(
                    value
                );

                showToast(
                    "📋 Server URL copied"
                );
            } catch {
                input?.select();

                document.execCommand(
                    "copy"
                );

                showToast(
                    "📋 Server URL copied"
                );
            }
        }
    );

/* ============================================================
   OPEN SUBSONIC CONNECTION TEST
   ============================================================ */

document
    .getElementById(
        "amperfy-test-button"
    )
    ?.addEventListener(
        "click",
        testOpenSubsonic
    );

async function testOpenSubsonic() {
    const status =
        document.getElementById(
            "amperfy-status"
        );

    const compatibility =
        document.getElementById(
            "amperfy-open-subsonic"
        );

    const button =
        document.getElementById(
            "amperfy-test-button"
        );

    if (button) {
        button.disabled = true;
        button.textContent =
            "⏳ Testing...";
    }

    if (status) {
        status.textContent =
            "Checking...";
    }

    try {
        const response =
            await fetch(
                "/rest/ping.view?f=json",
                {
                    cache: "no-store",
                }
            );

        if (!response.ok) {
            throw new Error(
                `HTTP ${response.status}`
            );
        }

        const data =
            await response
                .json()
                .catch(
                    () => null
                );

        const success =
            data?.subsonicResponse
                ?.status ===
                "ok";

        if (!success) {
            throw new Error(
                "Invalid OpenSubsonic response"
            );
        }

        if (status) {
            status.textContent =
                "🟢 Xrob Music OpenSubsonic server is online.";
        }

        if (compatibility) {
            compatibility.textContent =
                "Compatible";
            compatibility.className =
                "integration-ok";
        }

        showToast(
            "🟢 OpenSubsonic connection successful"
        );
    } catch (error) {
        if (status) {
            status.textContent =
                `🔴 Connection failed: ${error.message}`;
        }

        if (compatibility) {
            compatibility.textContent =
                "Unavailable";
            compatibility.className =
                "integration-error";
        }

        showToast(
            `❌ ${error.message}`
        );
    } finally {
        if (button) {
            button.disabled =
                false;

            button.textContent =
                "🔌 Test Connection";
        }
    }
}

window.testOpenSubsonic =
    testOpenSubsonic;/* ============================================================
   LIBRARY
   ============================================================ */

let libraryTracks = [];

async function loadLibrary() {
    const container =
        document.getElementById("libraryList");

    if (!container) {
        return;
    }

    container.innerHTML = `
        <div class="status-msg">
            Loading library...
        </div>
    `;

    try {
        const data =
            await fetchJson("/api/library");

        libraryTracks =
            Array.isArray(data)
                ? data
                : Array.isArray(data?.tracks)
                    ? data.tracks
                    : Array.isArray(data?.songs)
                        ? data.songs
                        : [];

        updateLibraryStats(data);
        renderLibrary(libraryTracks);
    } catch (error) {
        console.error(
            "Library loading failed:",
            error
        );

        container.innerHTML = `
            <div class="downloads-empty">
                <div class="empty-icon">⚠️</div>

                <div class="empty-title">
                    Unable to load library
                </div>

                <div class="empty-text">
                    ${escapeHtml(error.message)}
                </div>
            </div>
        `;
    }
}

window.loadLibrary = loadLibrary;

function updateLibraryStats(data = {}) {
    const tracks =
        Array.isArray(libraryTracks)
            ? libraryTracks
            : [];

    const uniqueArtists =
        new Set(
            tracks
                .map(
                    track =>
                        track?.artist ||
                        track?.album_artist ||
                        ""
                )
                .filter(Boolean)
        );

    const uniqueAlbums =
        new Set(
            tracks
                .map(
                    track =>
                        track?.album ||
                        ""
                )
                .filter(Boolean)
        );

    const trackCount =
        Number(
            data?.stats?.tracks ??
            data?.tracks ??
            tracks.length
        );

    const artistCount =
        Number(
            data?.stats?.artists ??
            data?.artists ??
            uniqueArtists.size
        );

    const albumCount =
        Number(
            data?.stats?.albums ??
            data?.albums ??
            uniqueAlbums.size
        );

    const size =
        Number(
            data?.stats?.size ??
            data?.size ??
            data?.folder_size ??
            0
        );

    setElementText(
        "statTracks",
        Number.isFinite(trackCount)
            ? trackCount
            : tracks.length
    );

    setElementText(
        "statArtists",
        Number.isFinite(artistCount)
            ? artistCount
            : uniqueArtists.size
    );

    setElementText(
        "statAlbums",
        Number.isFinite(albumCount)
            ? albumCount
            : uniqueAlbums.size
    );

    setElementText(
        "sideLibCount",
        Number.isFinite(trackCount)
            ? trackCount
            : tracks.length
    );

    setElementText(
        "mobLibCount",
        Number.isFinite(trackCount)
            ? trackCount
            : tracks.length
    );

    setElementText(
        "downloadStatTracks",
        Number.isFinite(trackCount)
            ? trackCount
            : tracks.length
    );

    setElementText(
        "downloadStatAlbums",
        Number.isFinite(albumCount)
            ? albumCount
            : uniqueAlbums.size
    );

    const sizeElement =
        document.getElementById(
            "libFolderSize"
        );

    if (sizeElement) {
        sizeElement.textContent =
            formatFileSize(size);
    }

    setElementText(
        "homeTracks",
        Number.isFinite(trackCount)
            ? trackCount
            : tracks.length
    );

    setElementText(
        "homeArtists",
        Number.isFinite(artistCount)
            ? artistCount
            : uniqueArtists.size
    );

    setElementText(
        "homeAlbums",
        Number.isFinite(albumCount)
            ? albumCount
            : uniqueAlbums.size
    );
}

function renderLibrary(
    tracks
) {
    const container =
        document.getElementById(
            "libraryList"
        );

    if (!container) {
        return;
    }

    container.innerHTML = "";

    if (!tracks.length) {
        container.innerHTML = `
            <div class="downloads-empty">
                <div class="empty-icon">📂</div>

                <div class="empty-title">
                    Your library is empty
                </div>

                <div class="empty-text">
                    Download music from Search Music.
                </div>
            </div>
        `;

        return;
    }

    tracks.forEach(
        (track) => {
            container.appendChild(
                createLibraryCard(track)
            );
        }
    );
}

function createLibraryCard(
    track
) {
    const card =
        document.createElement(
            "article"
        );

    card.className =
        "music-card";

    const title =
        track?.title ||
        track?.name ||
        "Unknown Track";

    const artist =
        track?.artist ||
        track?.album_artist ||
        "Unknown Artist";

    const album =
        track?.album ||
        "";

    const cover =
        track?.cover ||
        track?.artwork ||
        track?.thumbnail ||
        track?.album_art ||
        "https://via.placeholder.com/300?text=Music";

    const streamUrl =
        track?.stream_url ||
        track?.stream ||
        (
            track?.id
                ? `/rest/stream?id=${encodeURIComponent(
                      track.id
                  )}`
                : track?.path
                    ? `/api/library/stream?path=${encodeURIComponent(
                          track.path
                      )}`
                    : ""
        );

    card.innerHTML = `
        <div class="music-card-cover-wrap">

            <img
                class="music-card-cover"
                src="${escapeHtml(cover)}"
                alt=""
                loading="lazy"
            >

        </div>

        <div class="music-card-content">

            <div class="music-card-title">
                ${escapeHtml(title)}
            </div>

            <div class="music-card-artist">
                ${escapeHtml(artist)}
            </div>

            ${
                album
                    ? `
                <div class="music-card-album">
                    ${escapeHtml(album)}
                </div>
            `
                    : ""
            }

            <div class="music-card-actions">

                ${
                    streamUrl
                        ? `
                    <button
                        class="btn-preview library-play"
                        type="button"
                    >
                        ▶ Play
                    </button>
                `
                        : ""
                }

            </div>

        </div>
    `;

    const image =
        card.querySelector(
            ".music-card-cover"
        );

    image?.addEventListener(
        "error",
        () => {
            image.src =
                "https://via.placeholder.com/300?text=Music";
        },
        {
            once: true,
        }
    );

    const playButton =
        card.querySelector(
            ".library-play"
        );

    playButton?.addEventListener(
        "click",
        () => {
            toggleAudioStream(
                playButton,
                streamUrl,
                "library",
                title,
                artist,
                cover
            );
        }
    );

    return card;
}

function filterLibrary() {
    const input =
        document.getElementById(
            "libSearchQuery"
        );

    const query =
        String(
            input?.value || ""
        )
            .trim()
            .toLowerCase();

    if (!query) {
        renderLibrary(
            libraryTracks
        );

        return;
    }

    const filtered =
        libraryTracks.filter(
            (track) => {
                const searchable =
                    [
                        track?.title,
                        track?.name,
                        track?.artist,
                        track?.album_artist,
                        track?.album,
                        track?.genre,
                        track?.path,
                    ]
                        .filter(Boolean)
                        .join(" ")
                        .toLowerCase();

                return searchable.includes(
                    query
                );
            }
        );

    renderLibrary(filtered);
}

window.filterLibrary =
    filterLibrary;

async function refreshLibraryCache() {
    try {
        await loadLibrary();

        if (
            currentTab ===
            "home"
        ) {
            await loadHome();
        }
    } catch {
        // Ignore background refresh failures.
    }
}

/* ============================================================
   STATS
   ============================================================ */

async function loadStats() {
    try {
        const data =
            await fetchJson(
                "/api/stats"
            );

        const stats =
            data?.stats ||
            data ||
            {};

        const tracks =
            Number(
                stats.tracks ??
                stats.track_count ??
                0
            );

        const artists =
            Number(
                stats.artists ??
                stats.artist_count ??
                0
            );

        const albums =
            Number(
                stats.albums ??
                stats.album_count ??
                0
            );

        setElementText(
            "homeTracks",
            tracks
        );

        setElementText(
            "homeArtists",
            artists
        );

        setElementText(
            "homeAlbums",
            albums
        );

        setElementText(
            "statTracks",
            tracks
        );

        setElementText(
            "statArtists",
            artists
        );

        setElementText(
            "statAlbums",
            albums
        );

        setElementText(
            "downloadStatTracks",
            tracks
        );

        setElementText(
            "downloadStatAlbums",
            albums
        );

        setElementText(
            "sideLibCount",
            tracks
        );

        setElementText(
            "mobLibCount",
            tracks
        );

        return stats;
    } catch (error) {
        console.warn(
            "Stats endpoint unavailable:",
            error
        );

        /*
         * Do not break the application when the backend
         * doesn't expose /api/stats.
         */
        return null;
    }
}

/* ============================================================
   SETTINGS
   ============================================================ */

async function loadSettings() {
    try {
        const data =
            await fetchJson(
                "/api/settings"
            );

        const settings =
            data?.settings ||
            data ||
            {};

        setInputValue(
            "set_format",
            settings.format ??
            settings.audio_format
        );

        setInputValue(
            "set_quality",
            settings.quality ??
            settings.audio_quality
        );

        setCheckbox(
            "set_thumb",
            settings.thumb ??
            settings.thumbnail ??
            settings.embed_album_art
        );

        setCheckbox(
            "set_meta",
            settings.meta ??
            settings.metadata ??
            settings.embed_metadata
        );

        setCheckbox(
            "set_organize",
            settings.organize ??
            settings.organize_by_artist
        );

        setInputValue(
            "set_max_results",
            settings.max_results
        );

        /*
         * OpenSubsonic information may be supplied by
         * the backend either inside settings or directly.
         */
        const username =
            settings.subsonic_user ??
            settings.username ??
            data?.subsonic_user ??
            data?.username;

        if (
            username !== undefined
        ) {
            setInputValue(
                "set_subsonic_user",
                username
            );
        }

        const serverUrl =
            settings.server_url ??
            settings.subsonic_url ??
            data?.server_url ??
            data?.subsonic_url;

        if (
            serverUrl !== undefined
        ) {
            setInputValue(
                "amperfy-server-url",
                serverUrl
            );
        }

        updateSettingsFromCurrentPage();
    } catch (error) {
        console.warn(
            "Settings:",
            error
        );
    }
}

window.loadSettings =
    loadSettings;

function updateSettingsFromCurrentPage() {
    /*
     * Do not overwrite values returned by the backend.
     * This only fills the OpenSubsonic URL when the
     * backend did not explicitly provide one.
     */
    const serverInput =
        document.getElementById(
            "amperfy-server-url"
        );

    if (
        serverInput &&
        !serverInput.value
    ) {
        serverInput.value =
            `${window.location.origin}/rest`;
    }
}

async function saveSettings() {
    const message =
        document.getElementById(
            "settingsMsg"
        );

    const format =
        document.getElementById(
            "set_format"
        )?.value;

    const quality =
        document.getElementById(
            "set_quality"
        )?.value;

    const thumb =
        Boolean(
            document.getElementById(
                "set_thumb"
            )?.checked
        );

    const meta =
        Boolean(
            document.getElementById(
                "set_meta"
            )?.checked
        );

    const organize =
        Boolean(
            document.getElementById(
                "set_organize"
            )?.checked
        );

    const maxResults =
        Number(
            document.getElementById(
                "set_max_results"
            )?.value
        );

    if (
        !Number.isFinite(
            maxResults
        ) ||
        maxResults < 5 ||
        maxResults > 50
    ) {
        showSettingsMessage(
            "❌ Max Search Results must be between 5 and 50.",
            true
        );

        return;
    }

    const payload = {
        format,
        quality,
        thumb,
        meta,
        organize,
        max_results:
            maxResults,
    };

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
                        payload
                    ),
            }
        );

        showSettingsMessage(
            "✅ Settings saved successfully.",
            false
        );

        showToast(
            "⚙️ Settings saved"
        );
    } catch (error) {
        console.error(
            "Save settings:",
            error
        );

        showSettingsMessage(
            `❌ ${error.message}`,
            true
        );

        showToast(
            `❌ ${error.message}`
        );
    }
}

window.saveSettings =
    saveSettings;

function showSettingsMessage(
    text,
    error
) {
    const element =
        document.getElementById(
            "settingsMsg"
        );

    if (!element) {
        return;
    }

    element.textContent =
        text;

    element.classList.toggle(
        "error",
        Boolean(error)
    );
}

/* ============================================================
   AUDIO PLAYER
   ============================================================ */

function setupAudioPlayer() {
    if (!audio) {
        return;
    }

    audio.volume = 0.8;

    audio.addEventListener(
        "loadedmetadata",
        () => {
            updatePlayerDuration();
        }
    );

    audio.addEventListener(
        "timeupdate",
        () => {
            updatePlayerProgress();
        }
    );

    audio.addEventListener(
        "play",
        () => {
            updatePlayerButton(
                true
            );
        }
    );

    audio.addEventListener(
        "pause",
        () => {
            updatePlayerButton(
                false
            );
        }
    );

    audio.addEventListener(
        "ended",
        () => {
            updatePlayerButton(
                false
            );

            updatePlayerProgress(
                true
            );
        }
    );

    audio.addEventListener(
        "error",
        () => {
            console.error(
                "Audio error:",
                audio.error
            );

            showToast(
                "❌ Unable to play this track"
            );

            updatePlayerButton(
                false
            );
        }
    );

    document
        .getElementById(
            "gp-play-btn"
        )
        ?.addEventListener(
            "click",
            () => {
                if (
                    audio.paused
                ) {
                    audio.play().catch(
                        (error) => {
                            console.warn(
                                "Play:",
                                error
                            );

                            showToast(
                                "❌ Playback could not start"
                            );
                        }
                    );
                } else {
                    audio.pause();
                }
            }
        );

    document
        .getElementById(
            "gp-seek"
        )
        ?.addEventListener(
            "input",
            (event) => {
                const percent =
                    Number(
                        event.target.value
                    );

                if (
                    !Number.isFinite(
                        percent
                    ) ||
                    !Number.isFinite(
                        audio.duration
                    ) ||
                    audio.duration <= 0
                ) {
                    return;
                }

                audio.currentTime =
                    (
                        percent /
                        100
                    ) *
                    audio.duration;
            }
        );

    document
        .getElementById(
            "gp-volume"
        )
        ?.addEventListener(
            "input",
            (event) => {
                const volume =
                    Number(
                        event.target.value
                    );

                if (
                    Number.isFinite(
                        volume
                    )
                ) {
                    audio.volume =
                        Math.max(
                            0,
                            Math.min(
                                1,
                                volume
                            )
                        );
                }
            }
        );
}

function toggleAudioStream(
    button,
    url,
    source,
    title,
    artist,
    art
) {
    if (
        !audio ||
        !url
    ) {
        showToast(
            "❌ Audio stream unavailable"
        );

        return;
    }

    const isSameTrack =
        currentAudioUrl ===
        url;

    if (
        isSameTrack &&
        !audio.paused
    ) {
        audio.pause();

        updatePlayerButton(
            false
        );

        return;
    }

    if (
        !isSameTrack
    ) {
        currentAudioUrl =
            url;

        audio.src = url;

        audio.load();

        setPlayerTrack(
            title,
            artist,
            art
        );
    }

    audio.play().then(
        () => {
            showGlobalPlayer();

            updatePlayerButton(
                true
            );
        }
    ).catch(
        (error) => {
            console.error(
                "Audio playback:",
                error
            );

            showToast(
                "❌ Unable to play this track"
            );

            updatePlayerButton(
                false
            );
        }
    );
}

function setPlayerTrack(
    title,
    artist,
    art
) {
    setElementText(
        "gp-title",
        title ||
            "Unknown Track"
    );

    setElementText(
        "gp-artist",
        artist ||
            "Unknown Artist"
    );

    const image =
        document.getElementById(
            "gp-art"
        );

    if (image) {
        image.src =
            art ||
            "https://via.placeholder.com/60?text=Music";
    }
}

function showGlobalPlayer() {
    const player =
        document.getElementById(
            "global-player-bar"
        );

    if (player) {
        player.style.display =
            "flex";
    }
}

function hideGlobalPlayer() {
    const player =
        document.getElementById(
            "global-player-bar"
        );

    if (player) {
        player.style.display =
            "none";
    }
}

function updatePlayerButton(
    playing
) {
    const button =
        document.getElementById(
            "gp-play-btn"
        );

    if (!button) {
        return;
    }

    button.textContent =
        playing
            ? "⏸"
            : "▶";

    button.setAttribute(
        "aria-label",
        playing
            ? "Pause"
            : "Play"
    );
}

function updatePlayerDuration() {
    if (!audio) {
        return;
    }

    const duration =
        Number(audio.duration);

    if (
        !Number.isFinite(
            duration
        )
    ) {
        return;
    }

    setElementText(
        "gp-dur-time",
        formatTime(duration)
    );
}

function updatePlayerProgress(
    reset = false
) {
    if (!audio) {
        return;
    }

    const seek =
        document.getElementById(
            "gp-seek"
        );

    const current =
        reset
            ? 0
            : Number(
                  audio.currentTime
              );

    const duration =
        Number(audio.duration);

    setElementText(
        "gp-cur-time",
        formatTime(current)
    );

    if (
        seek &&
        Number.isFinite(
            duration
        ) &&
        duration > 0
    ) {
        seek.value =
            String(
                (
                    current /
                    duration
                ) *
                    100
            );
    } else if (seek) {
        seek.value = "0";
    }
}

function formatTime(
    seconds
) {
    const value =
        Number(seconds);

    if (
        !Number.isFinite(
            value
        ) ||
        value < 0
    ) {
        return "0:00";
    }

    const minutes =
        Math.floor(
            value / 60
        );

    const remaining =
        Math.floor(
            value % 60
        );

    return `${minutes}:${String(
        remaining
    ).padStart(2, "0")}`;
}

/* ============================================================
   VISUALIZER
   ============================================================ */

function setupVisualizer() {
    const canvas =
        document.getElementById(
            "visualizer-canvas"
        );

    if (
        !canvas ||
        !audio
    ) {
        return;
    }

    try {
        audioContext =
            new (
                window.AudioContext ||
                window.webkitAudioContext
            )();

        analyser =
            audioContext.createAnalyser();

        analyser.fftSize =
            128;

        sourceNode =
            audioContext.createMediaElementSource(
                audio
            );

        sourceNode.connect(
            analyser
        );

        analyser.connect(
            audioContext.destination
        );

        visualizerData =
            new Uint8Array(
                analyser.frequencyBinCount
            );

        drawVisualizer(
            canvas
        );
    } catch (error) {
        /*
         * A media element can only be connected to one
         * MediaElementSourceNode. If the browser blocks
         * the visualizer, playback must still continue.
         */
        console.warn(
            "Visualizer unavailable:",
            error
        );

        audioContext =
            null;

        analyser =
            null;

        sourceNode =
            null;
    }
}

function drawVisualizer(
    canvas
) {
    if (
        !canvas ||
        !analyser
    ) {
        return;
    }

    const context =
        canvas.getContext(
            "2d"
        );

    if (!context) {
        return;
    }

    const width =
        canvas.width;

    const height =
        canvas.height;

    analyser.getByteFrequencyData(
        visualizerData
    );

    context.clearRect(
        0,
        0,
        width,
        height
    );

    const bars =
        Math.min(
            18,
            visualizerData.length
        );

    const barWidth =
        width / bars;

    for (
        let i = 0;
        i < bars;
        i++
    ) {
        const value =
            visualizerData[
                i
            ] || 0;

        const barHeight =
            (
                value /
                255
            ) *
            height;

        context.fillRect(
            i *
                barWidth,
            height -
                barHeight,
            Math.max(
                1,
                barWidth - 1
            ),
            barHeight
        );
    }

    requestAnimationFrame(
        () =>
            drawVisualizer(
                canvas
            )
    );
}

/* ============================================================
   INITIALIZATION HELPERS
   ============================================================ */

function setElementText(
    id,
    value
) {
    const element =
        document.getElementById(
            id
        );

    if (element) {
        element.textContent =
            String(
                value ?? ""
            );
    }
}

function setInputValue(
    id,
    value
) {
    const element =
        document.getElementById(
            id
        );

    if (
        element &&
        value !== undefined &&
        value !== null
    ) {
        element.value =
            String(value);
    }
}

function setCheckbox(
    id,
    value
) {
    const element =
        document.getElementById(
            id
        );

    if (
        !element ||
        value === undefined ||
        value === null
    ) {
        return;
    }

    if (
        typeof value ===
        "string"
    ) {
        element.checked =
            [
                "true",
                "1",
                "yes",
                "on",
            ].includes(
                value.toLowerCase()
            );
    } else {
        element.checked =
            Boolean(value);
    }
}

function formatFileSize(
    bytes
) {
    const value =
        Number(bytes);

    if (
        !Number.isFinite(
            value
        ) ||
        value <= 0
    ) {
        return "0 MB";
    }

    const units = [
        "B",
        "KB",
        "MB",
        "GB",
        "TB",
    ];

    let size = value;
    let index = 0;

    while (
        size >= 1024 &&
        index <
            units.length - 1
    ) {
        size /= 1024;
        index++;
    }

    const decimals =
        index === 0
            ? 0
            : size < 10
                ? 2
                : 1;

    return `${size.toFixed(
        decimals
    )} ${units[index]}`;
}

/* ============================================================
   PAGE VISIBILITY
   ============================================================ */

document.addEventListener(
    "visibilitychange",
    () => {
        if (
            document.hidden
        ) {
            /*
             * Keep download polling alive because downloads
             * must continue updating even when another tab
             * is active.
             */
            return;
        }

        loadDownloads().catch(
            () => {}
        );

        if (
            currentTab ===
            "library"
        ) {
            loadLibrary().catch(
                () => {}
            );
        }
    }
);

/* ============================================================
   WINDOW RESIZE
   ============================================================ */

window.addEventListener(
    "resize",
    () => {
        /*
         * Keep the canvas resolution stable.
         * CSS controls its visual size.
         */
        const canvas =
            document.getElementById(
                "visualizer-canvas"
            );

        if (!canvas) {
            return;
        }

        canvas.width = 90;
        canvas.height = 30;
    }
);/* ============================================================
   APPLICATION STARTUP
   ============================================================ */

async function initializeApp() {
    console.info(
        "Xrob Music starting..."
    );

    /*
     * Set the initial navigation state before loading
     * backend data.
     */
    navigate(
        currentTab,
        false
    );

    setupAudioPlayer();

    setupVisualizer();

    connectWebSocket();

    startDownloadPolling();

    /*
     * Load independent resources in parallel.
     * One failing endpoint must not prevent the rest
     * of the application from starting.
     */
    await Promise.allSettled([
        loadDownloads(),
        loadLibrary(),
        loadStats(),
        loadSettings(),
        loadHome(),
    ]);

    updateQueueCounts();

    console.info(
        "Xrob Music ready."
    );
}

/* ============================================================
   START
   ============================================================ */

if (
    document.readyState ===
    "loading"
) {
    document.addEventListener(
        "DOMContentLoaded",
        initializeApp,
        {
            once: true,
        }
    );
} else {
    initializeApp();
}

/* ============================================================
   CLEANUP
   ============================================================ */

window.addEventListener(
    "beforeunload",
    () => {
        stopDownloadPolling();

        if (
            socket &&
            socket.readyState ===
                WebSocket.OPEN
        ) {
            socket.close();
        }

        if (audio) {
            audio.pause();
        }

        if (audioContext) {
            audioContext.close().catch(
                () => {}
            );
        }
    }
);
