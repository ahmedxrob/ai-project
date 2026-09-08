"use strict";

/* ============================================================
   GLOBAL STATE
   ============================================================ */

let socket = null;
let socketReconnectTimer = null;

let completedSet = new Set();

let rawLibraryFiles = [];
let libraryArtists = [];
let libraryAlbums = [];
let libraryView = "tracks";
let selectedArtistId = null;
let selectedAlbumId = null;
let libraryPlaybackQueue = null;
let libraryFilesSet = new Set();
let playerShuffle = localStorage.getItem("xrob_music_shuffle") === "true";

let libraryLoadedFromCache = false;

const LIBRARY_CACHE_KEY =
    "xrob_music_library_cache";

const RECENT_CACHE_KEY =
    "xrob_music_recently_added_cache";
let recentTracksCache = [];

let activePreviewBtn = null;
let currentPlayerSource = null;
// "home" or "library"
let currentLibraryIndex = -1;

let currentPage = 1;
let currentQuery = "";
let isLoadingMore = false;
let hasMoreResults = true;

let latestTasks = [];
let lastTaskSignature = "";

let audio = null;
let player = null;
let playBtn = null;
let prevBtn = null;
let nextBtn = null;
let seek = null;
let isSeeking = false;
let volume = null;
let curTime = null;
let durTime = null;
let playerTitle = null;
let playerArtist = null;
let playerArt = null;
let canvas = null;
let canvasCtx = null;

let audioContext = null;
let analyser = null;
let sourceNode = null;

let savedPlayerState = {
    track: null,
    currentTime: 0,
    volume: 0.8,
    queueIndex: -1
};


/* ============================================================
   DOM INITIALIZATION
   ============================================================ */

function cacheDom() {

    audio =
        document.getElementById(
            "global-audio-element"
        );

    player =
        document.getElementById(
            "global-player-bar"
        );

    playBtn =
        document.getElementById(
            "gp-play-btn"
        );

    prevBtn =
        document.getElementById(
            "gp-prev-btn"
        );

    nextBtn =
        document.getElementById(
            "gp-next-btn"
        );

    seek =
        document.getElementById(
            "gp-seek"
        );

    volume =
        document.getElementById(
            "gp-volume"
        );

    curTime =
        document.getElementById(
            "gp-cur-time"
        );

    durTime =
        document.getElementById(
            "gp-dur-time"
        );

    playerTitle =
        document.getElementById(
            "gp-title"
        );

    playerArtist =
        document.getElementById(
            "gp-artist"
        );

    playerArt =
        document.getElementById(
            "gp-art"
        );

    canvas =
        document.getElementById(
            "visualizer-canvas"
        );

    canvasCtx =
        canvas
            ? canvas.getContext("2d")
            : null;
}


/* ============================================================
   HELPERS
   ============================================================ */

function getLibraryQueue() {
    return Array.isArray(libraryPlaybackQueue) ? libraryPlaybackQueue : (Array.isArray(rawLibraryFiles) ? rawLibraryFiles : []);
}

function shuffledCopy(items) {
    const copy = [...items];
    for (let i = copy.length - 1; i > 0; i -= 1) {
        const j = Math.floor(Math.random() * (i + 1));
        [copy[i], copy[j]] = [copy[j], copy[i]];
    }
    return copy;
}

function setShuffle(enabled) {
    playerShuffle = Boolean(enabled);
    localStorage.setItem("xrob_music_shuffle", String(playerShuffle));
    const buttons = [document.getElementById("gp-shuffle-btn"), document.getElementById("libraryShuffleButton")];
    buttons.forEach(button => button?.classList.toggle("active", playerShuffle));
}

function shuffleLibrary() {
    if (!rawLibraryFiles.length) { showToast("No tracks to shuffle"); return; }
    libraryView = "tracks";
    selectedArtistId = null; selectedAlbumId = null;
    document.querySelectorAll(".library-tab").forEach(btn => btn.classList.toggle("active", btn.dataset.libraryView === "tracks"));
    renderLibraryView();
    playQueue(rawLibraryFiles, 0, true);
}

/* ============================================================
   LOADING CIRCLE
   ============================================================ */
function updateLoadingCircle(
    type,
    percent,
    text = ""
) {

    const safePercent =
        Math.max(
            0,
            Math.min(
                100,
                Math.round(
                    Number(percent) || 0
                )
            )
        );

    const isLibrary =
        type === "library";

    const loading =
        document.getElementById(
            isLibrary
                ? "libraryLoading"
                : "recentTracksLoading"
        );

    const percentElement =
        document.getElementById(
            isLibrary
                ? "libraryLoadingPercent"
                : "recentLoadingPercent"
        );

    const circle =
        document.getElementById(
            isLibrary
                ? "libraryLoadingCircle"
                : "recentLoadingCircle"
        );

    const textElement =
        document.getElementById(
            isLibrary
                ? "libraryLoadingText"
                : "recentLoadingText"
        );

    if (!loading) {
        return;
    }

    loading.style.display =
        "flex";

    if (percentElement) {

        percentElement.textContent =
            `${safePercent}%`;
    }

    if (textElement && text) {

        textElement.textContent =
            text;
    }

    if (circle) {

        const circumference =
            263.9;

        circle.style.strokeDasharray =
            circumference;

        circle.style.strokeDashoffset =
            circumference -
            (
                safePercent / 100
            ) *
            circumference;
    }
}

/* ============================================================
   SEARCH LOADING CIRCLE
   ============================================================ */
function updateSearchLoading(
    percent,
    text = ""
) {

    const safePercent =
        Math.max(
            0,
            Math.min(
                100,
                Math.round(
                    Number(percent) || 0
                )
            )
        );

    const loading =
        document.getElementById(
            "searchLoading"
        );

    const percentElement =
        document.getElementById(
            "searchLoadingPercent"
        );

    const circle =
        document.getElementById(
            "searchLoadingCircle"
        );

    const textElement =
        document.getElementById(
            "searchLoadingText"
        );

    if (!loading) {
        return;
    }

    loading.style.display = "flex";

    if (percentElement) {
        percentElement.textContent =
            `${safePercent}%`;
    }

    if (textElement && text) {
        textElement.textContent =
            text;
    }

    if (circle) {

        const circumference =
            263.9;

        circle.style.strokeDasharray =
            circumference;

        circle.style.strokeDashoffset =
            circumference -
            (
                safePercent / 100
            ) *
            circumference;
    }
}

function smoothSearchLoading(
    from,
    to,
    text,
    duration = 400
) {

    const start =
        performance.now();

    function animate(now) {

        const progress =
            Math.min(
                (now - start) /
                duration,
                1
            );

        const percent =
            Math.round(
                from +
                (
                    to - from
                ) *
                progress
            );

        updateSearchLoading(
            percent,
            text
        );

        if (progress < 1) {

            requestAnimationFrame(
                animate
            );
        }
    }

    requestAnimationFrame(
        animate
    );
}

function hideSearchLoading() {

    const loading =
        document.getElementById(
            "searchLoading"
        );

    if (loading) {
        loading.style.display =
            "none";
    }
}

function smoothLoading(
    type,
    from,
    to,
    text,
    duration = 400
) {

    const start =
        performance.now();

    function animate(now) {

        const progress =
            Math.min(
                (now - start) /
                duration,
                1
            );

        const percent =
            Math.round(
                from +
                (to - from) *
                progress
            );

        updateLoadingCircle(
            type,
            percent,
            text
        );

        if (progress < 1) {

            requestAnimationFrame(
                animate
            );
        }
    }

    requestAnimationFrame(
        animate
    );
}

function hideLoadingCircle(
    type
) {

    const element =
        document.getElementById(
            type === "library"
                ? "libraryLoading"
                : "recentTracksLoading"
        );

    if (element) {

        element.style.display =
            "none";
    }
}

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
        .replace(
            /[^a-z0-9]+/g,
            ""
        );
}


function showToast(message) {

    const container =
        document.getElementById(
            "toast-container"
        );

    if (!container) {
        return;
    }

    const toast =
        document.createElement(
            "div"
        );

    toast.className = "toast";
    toast.textContent = String(message ?? "");

    container.appendChild(toast);

    setTimeout(
        () => toast.remove(),
        3500
    );
}

window.showToast = showToast;


/* ============================================================
   THEME
   ============================================================ */

function toggleTheme(theme) {

    const validThemes = [
        "dark",
        "light"
    ];

    if (!validThemes.includes(theme)) {
        theme = "dark";
    }

    document.documentElement.setAttribute(
        "data-theme",
        theme
    );

    localStorage.setItem(
        "xrob_music_theme",
        theme
    );
}


/* ============================================================
   NAVIGATION
   ============================================================ */

function navigate(
    tab,
    updateHash = true
) {

    if (updateHash) {

        if (location.hash !== `#${tab}`) {
            location.hash = tab;
        } else {
            switchTab(tab);
        }

    } else {

        switchTab(tab);
    }
}


function switchTab(tab) {

    const tabs = [
        "home",
        "search",
        "downloads",
        "library",
        "settings"
    ];

    if (!tabs.includes(tab)) {
        tab = "home";
    }

    document
        .querySelectorAll(".tab-content")
        .forEach(section => {

            section.classList.remove("active");

        });


    document
        .querySelectorAll(".nav-link")
        .forEach(button => {

            button.classList.remove("active");

        });


    const content =
        document.getElementById(
            `tab-${tab}`
        );

    if (content) {
        content.classList.add("active");
    }


    document
        .getElementById(`btn-${tab}`)
        ?.classList.add("active");


    document
        .getElementById(`mob-btn-${tab}`)
        ?.classList.add("active");


    if (tab === "home") {
        loadHome();
    }

    if (tab === "downloads") {
        loadDownloads();
    }

    if (tab === "library") {
        loadLibrary();
    }

    if (tab === "settings") {
        loadSettings();
    }
}


function handleHash() {

    const hash =
        location.hash
            .replace(/^#/, "")
            .trim();

    const tabs = [
        "home",
        "search",
        "downloads",
        "library",
        "settings"
    ];

    switchTab(
        tabs.includes(hash)
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

function savePlayerState() {

    if (!audio) {
        return;
    }

    const state = {
        src: audio.src || "",
        currentTime:
            Number(audio.currentTime || 0),

        volume:
            Number(audio.volume || 0.8),

        title:
            playerTitle?.textContent || "",

        artist:
            playerArtist?.textContent || "",

        art:
            playerArt?.src || "",

        queueIndex:
            Number.isInteger(
                window.xrobHomeQueueIndex
            )
                ? window.xrobHomeQueueIndex
                : -1
    };

    localStorage.setItem(
        "xrob_music_player_state",
        JSON.stringify(state)
    );
}


function restorePlayerState() {

    if (!audio) {
        return;
    }

    try {

        const raw =
            localStorage.getItem(
                "xrob_music_player_state"
            );

        if (!raw) {
            return;
        }

        const state =
            JSON.parse(raw);

        if (
            state.volume !== undefined &&
            Number.isFinite(
                Number(state.volume)
            )
        ) {

            audio.volume =
                Number(state.volume);

            if (volume) {
                volume.value =
                    Number(state.volume);
            }
        }

        if (!state.src) {
            return;
        }

        audio.src =
            state.src;

        audio.load();

        updatePlayerInfo(
            state.title,
            state.artist,
            state.art
        );

        if (player) {
            player.style.display =
                "grid";
        }

        /*
         * Restore position after metadata loads.
         */
        audio.addEventListener(
            "loadedmetadata",
            function restorePosition() {

                if (
                    Number.isFinite(
                        Number(state.currentTime)
                    )
                ) {

                    audio.currentTime =
                        Math.min(
                            Number(
                                state.currentTime
                            ),
                            audio.duration || 0
                        );
                }

                audio.removeEventListener(
                    "loadedmetadata",
                    restorePosition
                );

                updateProgress();
            }
        );

    } catch (error) {

        console.warn(
            "Could not restore player:",
            error
        );
    }
}


function formatSeconds(seconds) {

    seconds =
        Math.floor(
            Number(seconds) || 0
        );

    if (seconds < 0) {
        seconds = 0;
    }

    return (
        Math.floor(seconds / 60)
        +
        ":"
        +
        String(seconds % 60).padStart(2, "0")
    );
}


function updateProgress() {

    if (!audio || !seek) {
        return;
    }

    if (
        !audio.duration ||
        !Number.isFinite(audio.duration)
    ) {

        seek.value = 0;

        if (curTime) {
            curTime.textContent = "0:00";
        }

        if (durTime) {
            durTime.textContent = "0:00";
        }

        return;
    }


    if (!isSeeking) {
        seek.value = (audio.currentTime / audio.duration) * 100;
    }


    if (curTime) {
        curTime.textContent =
            formatSeconds(
                audio.currentTime
            );
    }


    if (durTime) {
        durTime.textContent =
            formatSeconds(
                audio.duration
            );
    }
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
            Boolean(playing)
        );
    }
}


function resetPreviewButton(button) {

    if (!button) {
        return;
    }

    button.classList.remove("playing");

    const type =
        button.dataset?.type || "search";

    if (
        button.classList.contains("btn-preview")
    ) {

        button.textContent =
            type === "library"
                ? "▶ Play"
                : "▶ Preview";
    }
}


function initAudioContext() {

    if (
        audioContext ||
        !audio
    ) {
        return;
    }

    try {

        const AudioContextClass =
            window.AudioContext ||
            window.webkitAudioContext;

        if (!AudioContextClass) {
            return;
        }

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
    }
}


function drawVisualizer() {

    if (
        !canvasCtx ||
        !analyser
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
        i++
    ) {

        const height =
            Math.max(
                2,
                (
                    data[i] / 255
                ) * canvas.height
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


function toggleAudioStream(
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
        audioContext.state === "suspended"
    ) {

        audioContext.resume()
            .catch(() => {});
    }


    let absoluteUrl;

    try {

        absoluteUrl =
            new URL(
                url,
                location.href
            ).href;

    } catch (error) {

        console.error(
            "Invalid audio URL:",
            error
        );

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

            audio.play()
                .catch(error => {

                    console.error(
                        "Playback failed:",
                        error
                    );

                });

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

    button.dataset.type =
        type || "search";


    if (
        button.classList.contains("btn-preview")
    ) {

        button.textContent =
            "⏳ Loading...";
    }


    updatePlayerInfo(
        title,
        artist,
        art
    );


    if (player) {
        player.style.display = "grid";
    }


    audio.pause();

    audio.removeAttribute("src");

    audio.src = absoluteUrl;

    audio.load();


    audio.play()
        .then(() => {

            if (
                button.classList.contains(
                    "btn-preview"
                )
            ) {

                button.textContent =
                    "❚❚ Pause";
            }

        })
        .catch(error => {

            console.error(
                "Playback failed:",
                error
            );

            if (
                button.classList.contains(
                    "btn-preview"
                )
            ) {

                button.textContent =
                    "❌ Error";

                setTimeout(
                    () =>
                        resetPreviewButton(
                            button
                        ),
                    1800
                );

            } else {

                button.classList.remove(
                    "playing"
                );
            }
        });
}


/* ============================================================
   AUDIO EVENTS
   ============================================================ */

function bindAudioEvents() {

    if (!audio) {
        return;
    }


    audio.addEventListener(
        "timeupdate",
        () => {

            updateProgress();
            savePlayerState();

        }
    );


    audio.addEventListener(
        "loadedmetadata",
        updateProgress
    );


    audio.addEventListener(
        "durationchange",
        updateProgress
    );


    audio.addEventListener(
        "play",
        () => {

            updatePlayingState(true);

        }
    );


    audio.addEventListener(
        "pause",
        () => {

            updatePlayingState(false);

        }
    );


    audio.addEventListener(
        "ended",
        () => {

            updatePlayingState(
                false
            );

            if (seek) {
                seek.value = 0;
            }

            if (curTime) {
                curTime.textContent =
                    "0:00";
            }

            if (
                currentPlayerSource ===
                "home"
            ) {

                const queue =
                    window.xrobHomeQueue || [];

                const currentIndex =
                    Number.isInteger(
                        window.xrobHomeQueueIndex
                    )
                        ? window.xrobHomeQueueIndex
                        : -1;

                if (
                    queue.length &&
                    currentIndex >= 0 &&
                    currentIndex <
                        queue.length - 1
                ) {

                    playHomeTrack(
                        currentIndex + 1
                    );

                    return;
                }
            }


            if (
                currentPlayerSource ===
                "library"
            ) {

                const queue = getLibraryQueue();
                const nextIndex = playerShuffle
                    ? Math.floor(Math.random() * queue.length)
                    : currentLibraryIndex + 1;

                if (
                    queue.length &&
                    nextIndex < queue.length
                ) {

                    playLibraryTrack(
                        nextIndex
                    );

                    return;
                }
            }

            if (activePreviewBtn) {

                resetPreviewButton(
                    activePreviewBtn
                );

                activePreviewBtn = null;
            }

            window.xrobHomeQueueIndex = -1;
        }
    );


    audio.addEventListener(
        "error",
        () => {

            console.warn(
                "Audio element error:",
                audio.error
            );

            if (activePreviewBtn) {

                resetPreviewButton(
                    activePreviewBtn
                );
            }
        }
    );
}


function bindPlayerControls() {

    playBtn?.addEventListener(
        "click",
        () => {

            if (!audio) {
                return;
            }

            if (!audio.src) {
                return;
            }

            if (audio.paused) {

                audio.play()
                    .catch(
                        console.error
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

    document.getElementById("gp-shuffle-btn")?.addEventListener("click", () => setShuffle(!playerShuffle));
    document.getElementById("libraryShuffleButton")?.addEventListener("click", shuffleLibrary);
    document.getElementById("libraryPlayAllButton")?.addEventListener("click", () => playQueue(rawLibraryFiles, 0, false));
    setShuffle(playerShuffle);


    seek?.addEventListener("pointerdown", () => { isSeeking = true; });
    seek?.addEventListener("input", () => {
        if (audio && Number.isFinite(audio.duration) && audio.duration > 0) {
            const ratio = Math.max(0, Math.min(1, Number(seek.value) / 100));
            audio.currentTime = ratio * audio.duration;
            if (curTime) curTime.textContent = formatSeconds(audio.currentTime);
        }
    });
    seek?.addEventListener("change", () => { isSeeking = false; updateProgress(); });
    seek?.addEventListener("pointerup", () => { isSeeking = false; updateProgress(); });


    const savedVolume =
        localStorage.getItem(
            "xrob_music_volume"
        );


    if (volume && audio) {

        const initialVolume =
            savedVolume !== null
                ? Number(savedVolume)
                : Number(volume.value || 0.8);


        const safeVolume =
            Number.isFinite(initialVolume)
                ? Math.max(
                    0,
                    Math.min(
                        1,
                        initialVolume
                    )
                )
                : 0.8;


        volume.value = safeVolume;
        audio.volume = safeVolume;
    }


    volume?.addEventListener(
        "input",
        () => {

            audio.volume =
                Number(
                    volume.value
                );

            localStorage.setItem(
                "xrob_music_volume",
                volume.value
            );

            savePlayerState();
        }
    );
}


/* ============================================================
   SETTINGS
   ============================================================ */

function renderStorage(storage) {
    const data = storage || {};
    const path = document.getElementById("storagePath");
    const status = document.getElementById("storageStatus");
    const free = document.getElementById("storageFree");
    if (path) path.textContent = data.path || "—";
    if (free) free.textContent = data.free || "—";
    if (status) {
        if (!data.exists) {
            status.textContent = "❌ Library path is not mounted or does not exist.";
            status.dataset.state = "error";
        } else if (!data.writable) {
            status.textContent = "⚠️ Library is mounted but not writable.";
            status.dataset.state = "error";
        } else {
            status.textContent = `✅ Library available • ${data.used || "0 MB"} used of ${data.total || "unknown"}`;
            status.dataset.state = "success";
        }
    }
}

async function resetSettings() {
    const defaults = {
        audio_format: "mp3",
        audio_quality: "320K",
        embed_thumbnail: true,
        embed_metadata: true,
        organize_by_artist: false
    };
    try {
        const response = await fetch("api/settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(defaults)
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || "Failed to reset settings.");
        applySettingsToForm(data);
        showToast("↺ Settings reset to defaults");
    } catch (error) {
        showToast("❌ " + error.message);
    }
}

function applySettingsToForm(settings) {
    const setValue = (id, value) => { const element = document.getElementById(id); if (element) element.value = value ?? ""; };
    const setChecked = (id, value) => { const element = document.getElementById(id); if (element) element.checked = Boolean(value); };
    setValue("set_format", settings.audio_format || "mp3");
    setValue("set_quality", settings.audio_quality || "320K");
    setChecked("set_thumb", settings.embed_thumbnail);
    setChecked("set_meta", settings.embed_metadata);
    setChecked("set_organize", settings.organize_by_artist);
    renderStorage(settings.storage);
    updateQualityState();
}

function updateQualityState() {
    const format = document.getElementById("set_format")?.value;
    const quality = document.getElementById("set_quality");
    if (!quality) return;
    const lossless = format === "flac";
    quality.disabled = lossless;
    quality.title = lossless ? "FLAC is lossless; bitrate is not used." : "";
}

async function loadSettings() {
    try {
        const response = await fetch("api/settings", { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const settings = await response.json();
        applySettingsToForm(settings);
    } catch (error) {
        console.warn("Settings load:", error);
    }
}


async function saveSettings() {

    const getValue = id =>
        document.getElementById(id)?.value || "";


    const getChecked = id =>
        document.getElementById(id)?.checked ?? false;
    const data = {

        audio_format:
            getValue("set_format") || "mp3",

        audio_quality:
            getValue("set_quality") || "320K",

        embed_thumbnail:
            getChecked("set_thumb"),

        embed_metadata:
            getChecked("set_meta"),

        organize_by_artist:
            getChecked("set_organize"),
    };


    try {

        const response =
            await fetch(
                "api/settings",
                {
                    method: "POST",

                    headers: {
                        "Content-Type":
                            "application/json"
                    },

                    body:
                        JSON.stringify(data)
                }
            );


        const result =
            await response.json()
                .catch(
                    () => ({})
                );


        if (!response.ok) {

            throw new Error(
                result.detail ||
                "Failed to save settings."
            );
        }


        const msg =
            document.getElementById(
                "settingsMsg"
            );


        if (msg) {

            msg.textContent =
                "✅ Settings saved.";
        }


        showToast(
            "✅ Settings saved"
        );

    } catch (error) {

        const msg =
            document.getElementById(
                "settingsMsg"
            );


        if (msg) {

            msg.textContent =
                "❌ " +
                error.message;
        }


        showToast(
            "❌ " +
            error.message
        );
    }
}


/* ============================================================
   CACHE HELPERS
   ============================================================ */

function saveLibraryCache() {

    try {

        localStorage.setItem(
            LIBRARY_CACHE_KEY,
            JSON.stringify({
                files: rawLibraryFiles,
                artists: libraryArtists,
                albums: libraryAlbums,
                savedAt: Date.now()
            })
        );

    } catch (error) {

        console.warn(
            "Library cache save failed:",
            error
        );
    }
}


function loadLibraryCache() {

    try {

        const raw =
            localStorage.getItem(
                LIBRARY_CACHE_KEY
            );

        if (!raw) {
            return false;
        }

        const cache =
            JSON.parse(raw);

        if (
            !cache ||
            !Array.isArray(
                cache.files
            )
        ) {
            return false;
        }

        rawLibraryFiles = cache.files;
        libraryArtists = Array.isArray(cache.artists) ? cache.artists : [];
        libraryAlbums = Array.isArray(cache.albums) ? cache.albums : [];

        libraryLoadedFromCache =
            true;

        libraryFilesSet.clear();

        rawLibraryFiles.forEach(
            file => {

                const name =
                    String(
                        file.name || ""
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

        return true;

    } catch (error) {

        console.warn(
            "Library cache load failed:",
            error
        );

        return false;
    }
}


function saveRecentlyAddedCache(
    tracks
) {

    try {

        localStorage.setItem(
            RECENT_CACHE_KEY,
            JSON.stringify({
                tracks:
                    Array.isArray(tracks)
                        ? tracks
                        : [],
                savedAt:
                    Date.now()
            })
        );

    } catch (error) {

        console.warn(
            "Recently Added cache save failed:",
            error
        );
    }
}


function loadRecentlyAddedCache() {

    try {

        const raw =
            localStorage.getItem(
                RECENT_CACHE_KEY
            );

        if (!raw) {
            return [];
        }

        const cache =
            JSON.parse(raw);

        if (
            !cache ||
            !Array.isArray(
                cache.tracks
            )
        ) {
            return [];
        }

        return cache.tracks;

    } catch (error) {

        console.warn(
            "Recently Added cache load failed:",
            error
        );

        return [];
    }
}


/* ============================================================
   LIBRARY
   ============================================================ */

async function refreshLibraryCache() {

    try {

        const response =
            await fetch(
                "api/library",
                {
                    cache: "no-store"
                }
            );


        if (!response.ok) {

            throw new Error(
                `HTTP ${response.status}`
            );
        }


        const data =
            await response.json();


        rawLibraryFiles = data.files || [];
        libraryPlaybackQueue = rawLibraryFiles;
        libraryArtists = data.artists || [];
        libraryAlbums = data.albums || [];

        saveLibraryCache();

        libraryLoadedFromCache =
            false;

        libraryFilesSet.clear();


        rawLibraryFiles.forEach(
            file => {

                const name =
                    String(
                        file.name || ""
                    );


                const slash =
                    name.lastIndexOf("/");


                const dot =
                    name.lastIndexOf(".");


                const base =
                    name.substring(
                        slash + 1,
                        dot > slash
                            ? dot
                            : name.length
                    );


                libraryFilesSet.add(
                    normalizeKey(base)
                );
            }
        );


        const side =
            document.getElementById(
                "sideLibCount"
            );


        if (side) {
            side.textContent =
                rawLibraryFiles.length;
        }


        const statTracks =
            document.getElementById(
                "statTracks"
            );


        if (statTracks) {
            statTracks.textContent =
                rawLibraryFiles.length;
        }


        const mobile =
            document.getElementById(
                "mobLibCount"
            );


        if (mobile) {
            mobile.textContent =
                rawLibraryFiles.length;
        }


        const size =
            document.getElementById(
                "libFolderSize"
            );


        if (size) {
            size.textContent =
                data.total_size || "0 MB";
        }

    } catch (error) {

        console.warn(
            "Library:",
            error
        );
    }
}


async function loadStats() {

    const controller =
        new AbortController();

    const timeout =
        setTimeout(
            () =>
                controller.abort(),
            5000
        );

    try {

        const response =
            await fetch(
                "api/stats",
                {
                    cache:
                        "no-store",

                    signal:
                        controller.signal
                }
            );

        if (!response.ok) {
            throw new Error(
                `HTTP ${response.status}`
            );
        }

        const stats =
            await response.json();

        const values = {

            statTracks:
                stats.tracks || 0,

            statArtists:
                stats.artists || 0,

            statAlbums:
                stats.albums || 0,

            downloadStatTracks:
                stats.tracks || 0,

            downloadStatAlbums:
                stats.albums || 0,

            homeTracks:
                stats.tracks || 0,

            homeArtists:
                stats.artists || 0,

            homeAlbums:
                stats.albums || 0
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
                        value;
                }
            }
        );

    } catch (error) {

        if (
            error.name ===
            "AbortError"
        ) {

            console.warn(
                "Stats request timed out"
            );

        } else {

            console.warn(
                "Stats:",
                error
            );
        }

    } finally {

        clearTimeout(
            timeout
        );
    }
}


async function loadLibrary() {
    const list = document.getElementById("libraryList");
    if (!list) return;

    const hasCache = loadLibraryCache();
    if (hasCache) renderLibraryView();
    updateLoadingCircle("library", hasCache ? 20 : 5, "Loading music library...");

    try {
        await refreshLibraryCache();
        document.getElementById("statTracks")?.replaceChildren(String(rawLibraryFiles.length));
        document.getElementById("statArtists")?.replaceChildren(String(libraryArtists.length));
        document.getElementById("statAlbums")?.replaceChildren(String(libraryAlbums.length));
        renderLibraryView();
        updateLoadingCircle("library", 100, "Library ready");
        setTimeout(() => hideLoadingCircle("library"), 250);
    } catch (error) {
        hideLoadingCircle("library");
        if (rawLibraryFiles.length) {
            renderLibraryView();
            showToast("Showing cached library");
        } else {
            list.innerHTML = `<div class="downloads-empty"><div class="empty-icon">⚠️</div><div class="empty-title">Could not load library</div><div class="empty-text">${escapeHtml(error.message || "Unknown error")}</div></div>`;
        }
    }
}

function renderLibraryView() {
    const list = document.getElementById("libraryList");
    if (!list) return;
    const query = String(document.getElementById("libSearchQuery")?.value || "").trim().toLowerCase();
    if (libraryView === "artists") return renderArtists(list, query);
    if (libraryView === "albums") return renderAlbums(list, query);
    if (libraryView === "artist-detail") return renderArtistDetail(list, query);
    if (libraryView === "album-detail") return renderAlbumDetail(list, query);
    renderTracks(list, query);
}

function renderEmpty(list, icon, title, text = "") {
    list.innerHTML = `<div class="downloads-empty"><div class="empty-icon">${icon}</div><div class="empty-title">${escapeHtml(title)}</div>${text ? `<div class="empty-text">${escapeHtml(text)}</div>` : ""}</div>`;
}

function playQueue(queue, index = 0, shuffle = false) {
    if (!Array.isArray(queue) || !queue.length) { showToast("No playable tracks"); return false; }
    libraryPlaybackQueue = shuffle ? shuffledCopy(queue) : [...queue];
    currentLibraryIndex = Math.max(0, Math.min(index, libraryPlaybackQueue.length - 1));
    currentPlayerSource = "library";
    setShuffle(Boolean(shuffle));
    playLibraryTrack(currentLibraryIndex);
    return true;
}

function renderTracks(list, query) {
    const files = rawLibraryFiles.filter(file => {
        const hay = `${file.title || file.name || ""} ${file.artist || ""} ${file.album || ""} ${file.name || ""}`.toLowerCase();
        return !query || hay.includes(query);
    });
    list.innerHTML = "";
    if (!files.length) {
        renderEmpty(list, "🎵", rawLibraryFiles.length ? "No matching tracks" : "Your library is empty", rawLibraryFiles.length ? "Try another search." : "Downloaded tracks will appear here.");
        return;
    }
    files.forEach(file => list.appendChild(createTrackCard(file, files)));
}

function createTrackCard(file, queue = rawLibraryFiles) {
    const encoded = encodeURIComponent(file.name || "");
    const cover = file.cover || `api/library/cover/${encoded}`;
    const stream = file.stream || `api/library/stream/${encoded}`;
    const card = document.createElement("article");
    card.className = "result-card";
    card.dataset.libraryName = file.name || "";
    card.innerHTML = `<div class="thumb-wrapper"><img src="${escapeHtml(cover)}" alt="" loading="lazy"></div><div class="track-info"><div class="track-title">${escapeHtml(file.title || file.name || "Unknown Track")}</div><div class="track-artist">${escapeHtml(file.artist || "Unknown Artist")} · ${escapeHtml(file.album || "Unknown Album")}</div></div><div class="btn-group"><button type="button" class="btn-preview">▶ Play</button><button type="button" class="btn-danger">🗑 Delete</button></div>`;
    card.querySelector("img")?.addEventListener("error", e => e.currentTarget.removeAttribute("src"), { once: true });
    const play = () => { libraryPlaybackQueue = [...queue]; currentLibraryIndex = Math.max(0, queue.findIndex(x => x.id === file.id || x.name === file.name)); currentPlayerSource = "library"; toggleAudioStream(card.querySelector(".btn-preview"), stream, "library", file.title || file.name, file.artist || "Unknown Artist", cover); };
    card.querySelector(".btn-preview")?.addEventListener("click", e => { e.stopPropagation(); play(); });
    card.querySelector(".btn-danger")?.addEventListener("click", e => { e.stopPropagation(); deleteFile(file.name); });
    card.addEventListener("dblclick", play);
    return card;
}

function renderArtists(list, query) {
    const artists = libraryArtists.filter(a => !query || String(a.name || "").toLowerCase().includes(query));
    list.innerHTML = "";
    if (!artists.length) return renderEmpty(list, "👤", "No artists found", query ? "Try another search." : "Scan your library to build the artist catalog.");
    artists.forEach(artist => {
        const card = document.createElement("article");
        card.className = "catalog-card artist-card";
        card.innerHTML = `<button type="button" class="catalog-main-action"><div class="catalog-icon">♪</div><div><strong>${escapeHtml(artist.name)}</strong><span>${artist.album_count || 0} album${artist.album_count === 1 ? "" : "s"} · ${artist.song_count || 0} track${artist.song_count === 1 ? "" : "s"}</span></div></button><button type="button" class="btn-preview catalog-play">▶ Play</button>`;
        card.querySelector(".catalog-main-action")?.addEventListener("click", () => openArtist(artist.id));
        card.querySelector(".catalog-play")?.addEventListener("click", e => { e.stopPropagation(); const tracks = rawLibraryFiles.filter(f => (artist.song_ids || []).includes(f.id)); playQueue(tracks, 0, false); });
        list.appendChild(card);
    });
}

function renderAlbums(list, query) {
    const albums = libraryAlbums.filter(a => !query || `${a.name || ""} ${a.artist || ""}`.toLowerCase().includes(query));
    list.innerHTML = "";
    if (!albums.length) return renderEmpty(list, "💿", "No albums found", query ? "Try another search." : "Scan your library to build the album catalog.");
    albums.forEach(album => list.appendChild(createAlbumCard(album)));
}

function createAlbumCard(album) {
    const card = document.createElement("article");
    card.className = "catalog-card album-card";
    const cover = album.cover || "";
    card.innerHTML = `<img src="${escapeHtml(cover)}" alt="" loading="lazy"><div><strong>${escapeHtml(album.name)}</strong><span>${escapeHtml(album.artist || "Unknown Artist")} · ${album.song_count || 0} track${album.song_count === 1 ? "" : "s"}${album.year ? ` · ${escapeHtml(album.year)}` : ""}</span><button type="button" class="btn-preview">▶ Play album</button></div>`;
    card.querySelector("img")?.addEventListener("error", e => e.currentTarget.removeAttribute("src"), { once: true });
    card.querySelector(".btn-preview")?.addEventListener("click", e => { e.stopPropagation(); playAlbum(album.id); });
    card.querySelector("strong")?.addEventListener("click", () => openAlbum(album.id));
    card.querySelector("img")?.addEventListener("click", () => openAlbum(album.id));
    return card;
}

function renderArtistDetail(list, query) {
    const artist = libraryArtists.find(a => a.id === selectedArtistId);
    if (!artist) { libraryView = "artists"; return renderArtists(list, query); }
    const ids = new Set(artist.song_ids || []);
    const tracks = rawLibraryFiles.filter(f => ids.has(f.id));
    const albums = libraryAlbums.filter(a => (a.song_ids || []).some(id => ids.has(id)));
    list.innerHTML = `<div class="catalog-detail-header"><button type="button" class="btn-refresh library-back-button">← Artists</button><div><h3>${escapeHtml(artist.name)}</h3><p>${albums.length} album${albums.length === 1 ? "" : "s"} · ${tracks.length} track${tracks.length === 1 ? "" : "s"}</p></div><button type="button" class="btn-preview artist-detail-play">▶ Play artist</button></div>`;
    list.querySelector(".library-back-button")?.addEventListener("click", () => { selectedArtistId = null; libraryView = "artists"; renderLibraryView(); });
    list.querySelector(".artist-detail-play")?.addEventListener("click", () => playQueue(tracks, 0, false));
    if (albums.length) {
        const heading = document.createElement("h3"); heading.className = "catalog-section-heading"; heading.textContent = "Albums"; list.appendChild(heading);
        albums.forEach(album => list.appendChild(createAlbumCard(album)));
    }
    const filtered = tracks.filter(file => { const hay = `${file.title || ""} ${file.album || ""}`.toLowerCase(); return !query || hay.includes(query); });
    if (filtered.length) {
        const heading = document.createElement("h3"); heading.className = "catalog-section-heading"; heading.textContent = "Tracks"; list.appendChild(heading);
        filtered.forEach(file => list.appendChild(createTrackCard(file, tracks)));
    } else if (!albums.length) renderEmpty(list, "🎵", "No matching tracks", "Try another search.");
}

function renderAlbumDetail(list, query) {
    const album = libraryAlbums.find(a => a.id === selectedAlbumId);
    if (!album) { libraryView = "albums"; return renderAlbums(list, query); }
    const ids = new Set(album.song_ids || []);
    const tracks = rawLibraryFiles.filter(f => ids.has(f.id));
    list.innerHTML = `<div class="catalog-detail-header"><button type="button" class="btn-refresh library-back-button">← Albums</button><div><h3>${escapeHtml(album.name)}</h3><p>${escapeHtml(album.artist || "Unknown Artist")} · ${tracks.length} track${tracks.length === 1 ? "" : "s"}</p></div><button type="button" class="btn-preview album-detail-play">▶ Play album</button></div>`;
    list.querySelector(".library-back-button")?.addEventListener("click", () => { selectedAlbumId = null; libraryView = "albums"; renderLibraryView(); });
    list.querySelector(".album-detail-play")?.addEventListener("click", () => playAlbum(album.id));
    const filtered = tracks.filter(file => { const hay = `${file.title || ""} ${file.artist || ""}`.toLowerCase(); return !query || hay.includes(query); });
    if (filtered.length) filtered.forEach(file => list.appendChild(createTrackCard(file, tracks))); else renderEmpty(list, "💿", "No matching tracks", "Try another search.");
}

function filterLibrary() { renderLibraryView(); }
function openArtist(id) { if (!libraryArtists.some(a => a.id === id)) return; selectedArtistId = id; selectedAlbumId = null; libraryView = "artist-detail"; document.getElementById("libSearchQuery").value = ""; renderLibraryView(); }
function openAlbum(id) { if (!libraryAlbums.some(a => a.id === id)) return; selectedAlbumId = id; selectedArtistId = null; libraryView = "album-detail"; document.getElementById("libSearchQuery").value = ""; renderLibraryView(); }
function playAlbum(id) { const album = libraryAlbums.find(a => a.id === id); if (!album) return showToast("Album not found"); const ids = new Set(album.song_ids || []); const tracks = rawLibraryFiles.filter(f => ids.has(f.id)); playQueue(tracks, 0, false); }
function playLibraryTrack(index) { const queue = getLibraryQueue(); if (!queue.length || index < 0 || index >= queue.length) return; const file = queue[index]; currentPlayerSource = "library"; currentLibraryIndex = index; const encoded = encodeURIComponent(file.name || ""); const cover = file.cover || `api/library/cover/${encoded}`; const stream = file.stream || `api/library/stream/${encoded}`; const button = document.querySelector(`.result-card[data-library-name="${CSS.escape(file.name || "")}"] .btn-preview`) || document.createElement("button"); button.type = "button"; button.className = "btn-preview"; toggleAudioStream(button, stream, "library", file.title || file.name, file.artist || "Unknown Artist", cover); }

async function deleteFile(filename) {

    if (
        !confirm(
            `Delete "${filename}"?`
        )
    ) {
        return;
    }


    try {

        const response =
            await fetch(
                "api/library/" +
                encodeURIComponent(
                    filename
                ),
                {
                    method: "DELETE"
                }
            );


        if (!response.ok) {

            const error =
                await response.json()
                    .catch(
                        () => ({})
                    );


            throw new Error(
                error.detail ||
                "Delete failed."
            );
        }


        showToast(
            "🗑 Track deleted"
        );


        if (
            activePreviewBtn &&
            activePreviewBtn.dataset.type === "library"
        ) {

            audio?.pause();
        }


        await loadLibrary();

    } catch (error) {

        showToast(
            "❌ " +
            error.message
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

    if (!input || !results || !status) {
        return;
    }

    const query =
        input.value.trim();

    if (!query) {

        status.textContent =
            "Enter a search term.";

        hideSearchLoading();

        return;
    }

    currentQuery = query;
    currentPage = 1;
    hasMoreResults = true;
    isLoadingMore = false;

    /*
     * Hide the normal text status.
     */
    status.textContent = "";

    /*
     * Start circular search loader.
     */
    updateSearchLoading(
        5,
        "Synchronizing..."
    );

    results.innerHTML = "";

    const button =
        document.getElementById(
            "searchBtn"
        );

    if (button) {
        button.disabled = true;
    }

    try {

        /*
         * STEP 1
         * Synchronize library
         */
        smoothSearchLoading(
            5,
            20,
            "Synchronizing library...",
            300
        );

        await refreshLibraryCache();


        /*
         * STEP 2
         * Search server
         */
        smoothSearchLoading(
            20,
            45,
            "Searching for music...",
            400
        );

        const response =
            await fetch(
                `api/search?q=${
                    encodeURIComponent(query)
                }&page=1`,
                {
                    cache: "no-store"
                }
            );


        /*
         * Search request finished.
         */
        updateSearchLoading(
            65,
            "Processing results..."
        );


        const data =
            await response.json()
                .catch(
                    () => []
                );


        if (!response.ok) {

            throw new Error(
                data.detail ||
                "Search failed."
            );
        }


        /*
         * No results
         */
        if (
            !Array.isArray(data) ||
            !data.length
        ) {

            updateSearchLoading(
                100,
                "No results found"
            );

            setTimeout(
                hideSearchLoading,
                500
            );

            hasMoreResults = false;

            return;
        }


        /*
         * STEP 3
         * Render results
         */
        updateSearchLoading(
            80,
            "Loading results..."
        );

        renderItems(data);


        /*
         * Search ready.
         */
        updateSearchLoading(
            100,
            "Search ready"
        );

        setTimeout(
            hideSearchLoading,
            400
        );


    } catch (error) {

        console.error(
            "Search failed:",
            error
        );

        updateSearchLoading(
            100,
            "Search failed"
        );

        setTimeout(
            hideSearchLoading,
            1000
        );

        status.textContent =
            "❌ " +
            error.message;

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


    if (!results || !Array.isArray(items)) {
        return;
    }


    items.forEach(
        item => {

            if (!item) {
                return;
            }


            const card =
                document.createElement(
                    "article"
                );


            card.className =
                "result-card";


            const thumbnail =
                String(
                    item.thumbnail || ""
                );


            card.innerHTML = `

                <div class="thumb-wrapper">

                    <img
                        src="${escapeHtml(thumbnail)}"
                        alt=""
                        loading="lazy"
                    >

                    <span class="badge-duration">
                        ${escapeHtml(
                            item.duration_text || ""
                        )}
                    </span>

                </div>


                <div class="track-info">

                    <div class="track-title">
                        ${escapeHtml(
                            item.title || "Unknown Track"
                        )}
                    </div>

                    <div class="track-artist">
                        👤 ${escapeHtml(
                            item.channel || "Unknown Artist"
                        )}
                    </div>

                </div>


                <div class="btn-group"></div>
            `;


            const image =
                card.querySelector("img");


            image?.addEventListener(
                "error",
                () => {

                    image.src =
                        "https://via.placeholder.com/100?text=Music";

                },
                {
                    once: true
                }
            );


            const group =
                card.querySelector(
                    ".btn-group"
                );


            if (!group) {
                return;
            }


            const titleKey =
                normalizeKey(
                    item.title || ""
                );


            if (
                libraryFilesSet.has(
                    titleKey
                )
            ) {

                group.innerHTML = `
                    <div class="badge-library">
                        ✅ In Library
                    </div>
                `;

            } else {

                const preview =
                    document.createElement(
                        "button"
                    );


                preview.type =
                    "button";


                preview.className =
                    "btn-preview";


                preview.dataset.type =
                    "search";


                preview.textContent =
                    "▶ Preview";


                preview.addEventListener(
                    "click",
                    () =>
                        toggleAudioStream(
                            preview,
                            "api/preview?url=" +
                            encodeURIComponent(
                                item.url || ""
                            ),
                            "search",
                            item.title,
                            item.channel,
                            item.thumbnail
                        )
                );


                const download =
                    document.createElement(
                        "button"
                    );


                download.type =
                    "button";


                download.className =
                    "btn-download";


                download.dataset.id =
                    item.id || "";


                download.textContent =
                    "⬇️ Save";


                download.addEventListener(
                    "click",
                    () =>
                        startDownload(
                            item.url,
                            item.title,
                            item.id,
                            item.channel,
                            download
                        )
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
}


async function loadMoreResults() {

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
        loader.style.display = "block";
    }


    try {

        const response =
            await fetch(
                `api/search?q=${
                    encodeURIComponent(
                        currentQuery
                    )
                }&page=${
                    nextPage
                }`,
                {
                    cache: "no-store"
                }
            );


        const data =
            await response.json()
                .catch(
                    () => []
                );


        if (!response.ok) {

            throw new Error(
                data.detail ||
                "Failed to load more results."
            );
        }


        if (
            !Array.isArray(data) ||
            !data.length
        ) {

            hasMoreResults = false;

        } else {

            currentPage = nextPage;

            renderItems(data);
        }

    } catch (error) {

        console.warn(
            "Load more:",
            error
        );

        showToast(
            "⚠️ Could not load more results"
        );

    } finally {

        if (loader) {
            loader.style.display = "none";
        }

        isLoadingMore = false;
    }
}


function bindSearch() {

    document
        .getElementById("searchBtn")
        ?.addEventListener(
            "click",
            searchMusic
        );


    document
        .getElementById("query")
        ?.addEventListener(
            "keydown",
            event => {

                if (
                    event.key === "Enter" &&
                    !event.isComposing
                ) {

                    event.preventDefault();

                    searchMusic();
                }
            }
        );
}


/* ============================================================
   DOWNLOADS
   ============================================================ */

function isActiveTask(task) {

    return [
        "queued",
        "downloading",
        "processing"
    ].includes(
        String(
            task?.status || ""
        ).toLowerCase()
    );
}


function isFinishedTask(task) {

    return [
        "completed",
        "error",
        "failed",
        "cancelled",
        "canceled"
    ].includes(
        String(
            task?.status || ""
        ).toLowerCase()
    );
}


function getTaskStatus(status) {

    const normalized =
        String(
            status || "queued"
        ).toLowerCase();


    const map = {

        queued: [
            "Queued",
            "⏳",
            "status-queued"
        ],

        downloading: [
            "Downloading",
            "⬇️",
            "status-downloading"
        ],

        processing: [
            "Processing",
            "⚙️",
            "status-processing"
        ],

        completed: [
            "Completed",
            "✓",
            "status-completed"
        ],

        error: [
            "Failed",
            "⚠️",
            "status-error"
        ],

        failed: [
            "Failed",
            "⚠️",
            "status-error"
        ],

        cancelled: [
            "Cancelled",
            "✕",
            "status-cancelled"
        ],

        canceled: [
            "Cancelled",
            "✕",
            "status-cancelled"
        ]
    };


    return (
        map[normalized] ||
        map.queued
    );
}


function updateQueueCounters(tasks) {

    const safeTasks =
        Array.isArray(tasks)
            ? tasks
            : [];


    const count =
        safeTasks.filter(
            isActiveTask
        ).length;


    [
        "queueCount",
        "mobQueueCount",
        "downloadQueueCount",
        "homeDownloads"
    ].forEach(
        id => {

            const element =
                document.getElementById(id);

            if (element) {
                element.textContent =
                    count;
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
        statusClass
    ] =
        getTaskStatus(
            task.status
        );


    const percent =
        Math.max(
            0,
            Math.min(
                100,
                Math.round(
                    Number(
                        task.percent || 0
                    )
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
            task.id || ""
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
                            task.title ||
                            "Unknown Track"
                        )}
                    </div>

                    <div class="download-artist">
                        ${escapeHtml(
                            task.artist ||
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

                <div class="download-progress-track">

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
                        task.error ||
                        task.step ||
                        ""
                    )}
                </div>

                <div class="download-meta">
                    ${escapeHtml(
                        task.speed ||
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


    if (!actions) {
        return card;
    }


    const actionButton =
        document.createElement(
            "button"
        );


    actionButton.type =
        "button";


    if (isActiveTask(task)) {

        actionButton.className =
            "btn-danger";


        actionButton.textContent =
            "✕ Cancel";


        actionButton.addEventListener(
            "click",
            () =>
                cancelTask(
                    task.id
                )
        );

    } else if (["error", "failed", "cancelled", "canceled"].includes(String(task.status || "").toLowerCase())) {
        actionButton.className = "save-btn";
        actionButton.textContent = "↻ Retry";
        actionButton.addEventListener("click", () => retryTask(task.id));
    } else {
        actionButton.className = "download-remove-btn";
        actionButton.textContent = "Remove";
        actionButton.addEventListener("click", () => removeDownloadTask(task.id));
    }


    actions.appendChild(
        actionButton
    );


    return card;
}


function renderDownloads(tasks) {

    const list =
        document.getElementById(
            "downloadsList"
        );


    if (!list) {
        return;
    }


    const safeTasks =
        Array.isArray(tasks)
            ? tasks
            : [];


    const active =
        safeTasks.filter(
            isActiveTask
        );


    const finished =
        safeTasks.filter(
            isFinishedTask
        );


    list.innerHTML = "";


    /* ACTIVE */

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

            <button
                type="button"
                class="save-btn"
            >
                🔍 Search Music
            </button>
        `;


        empty
            .querySelector("button")
            ?.addEventListener(
                "click",
                () =>
                    navigate("search")
            );


        activeSection.appendChild(
            empty
        );
    }


    list.appendChild(
        activeSection
    );


    /* HISTORY */

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
            task =>
                stack.appendChild(
                    createDownloadCard(
                        task
                    )
                )
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


function taskSignature(tasks) {

    return tasks
        .map(
            task =>
                [
                    task.id,
                    task.status,
                    task.percent,
                    task.speed,
                    task.step,
                    task.error,
                    task.last_updated
                ].join("|")
        )
        .sort()
        .join(";");
}


async function pollTasks(force = false) {

    try {

        const response =
            await fetch(
                "api/tasks",
                {
                    cache: "no-store"
                }
            );


        if (!response.ok) {

            throw new Error(
                `HTTP ${response.status}`
            );
        }


        const tasks =
            await response.json();


        latestTasks =
            Array.isArray(tasks)
                ? tasks
                : [];


        latestTasks.forEach(
            task => {

                if (
                    task.status === "completed" &&
                    !completedSet.has(task.id)
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


        updateQueueCounters(
            latestTasks
        );


        const signature =
            taskSignature(
                latestTasks
            );


        if (
            force ||
            signature !== lastTaskSignature
        ) {

            renderDownloads(
                latestTasks
            );
        }


        lastTaskSignature =
            signature;

    } catch (error) {

        console.warn(
            "Tasks:",
            error
        );
    }
}


async function loadDownloads() {

    await pollTasks(true);
    await loadStats();
}


async function startDownload(
    url,
    title,
    elementId,
    artist,
    button
) {

    if (!url) {

        showToast(
            "❌ Invalid download URL"
        );

        return;
    }


    if (button) {

        button.disabled = true;

        button.textContent =
            "⏳ Queuing...";
    }


    try {

        const response =
            await fetch(
                "api/download",
                {
                    method: "POST",

                    headers: {
                        "Content-Type":
                            "application/json"
                    },

                    body:
                        JSON.stringify({
                            url,
                            title,
                            elementId,
                            artist
                        })
                }
            );


        const data =
            await response.json()
                .catch(
                    () => ({})
                );


        if (!response.ok) {

            throw new Error(
                data.detail ||
                "Failed to queue download."
            );
        }


        showToast(
            data.status === "already_queued"
                ? "⏳ Already in queue"
                : "⬇️ Added to Downloads"
        );


        navigate("downloads");


        await pollTasks(true);

    } catch (error) {

        showToast(
            "❌ " +
            error.message
        );


        if (button) {

            button.disabled = false;

            button.textContent =
                "⬇️ Save";
        }
    }
}


async function cancelTask(taskId) {

    try {

        const response =
            await fetch(
                `api/tasks/${
                    encodeURIComponent(taskId)
                }/cancel`,
                {
                    method: "POST"
                }
            );


        const data =
            await response.json()
                .catch(
                    () => ({})
                );


        if (!response.ok) {

            throw new Error(
                data.detail ||
                "Failed to cancel."
            );
        }


        showToast(
            "✕ Download cancelled"
        );


        await pollTasks(true);

    } catch (error) {

        showToast(
            "❌ " +
            error.message
        );
    }
}


async function removeDownloadTask(taskId) {

    try {

        const response =
            await fetch(
                `api/tasks/${
                    encodeURIComponent(taskId)
                }`,
                {
                    method: "DELETE"
                }
            );


        const data =
            await response.json()
                .catch(
                    () => ({})
                );


        if (!response.ok) {

            throw new Error(
                data.detail ||
                "Failed to remove."
            );
        }


        completedSet.delete(
            taskId
        );


        await pollTasks(true);


        showToast(
            "🗑 Removed from history"
        );

    } catch (error) {

        showToast(
            "❌ " +
            error.message
        );
    }
}


async function retryTask(taskId) {
    try {
        const response = await fetch(`api/tasks/${encodeURIComponent(taskId)}/retry`, { method: "POST" });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || "Retry failed.");
        completedSet.delete(taskId);
        await pollTasks(true);
        showToast("↻ Download queued again");
    } catch (error) {
        showToast("❌ " + error.message);
    }
}


async function clearDoneTasks() {

    try {

        const response =
            await fetch(
                "api/tasks/clear-completed",
                {
                    method: "DELETE",
                    cache: "no-store"
                }
            );


        const data =
            await response.json()
                .catch(
                    () => ({})
                );


        if (!response.ok) {

            throw new Error(
                data.detail ||
                "Failed to clear."
            );
        }


        completedSet.clear();


        latestTasks =
            latestTasks.filter(
                task =>
                    !isFinishedTask(task)
            );


        lastTaskSignature = "";


        renderDownloads(
            latestTasks
        );


        updateQueueCounters(
            latestTasks
        );


        showToast(
            `🧹 Cleared ${
                data.count || 0
            } downloads`
        );

    } catch (error) {

        showToast(
            "❌ " +
            error.message
        );
    }
}


/* ============================================================
   HOME
   ============================================================ */

function playNextTrack() {

    if (
        currentPlayerSource ===
        "home"
    ) {

        const queue =
            window.xrobHomeQueue || [];

        if (!queue.length) {
            return;
        }

        const currentIndex =
            Number.isInteger(
                window.xrobHomeQueueIndex
            )
                ? window.xrobHomeQueueIndex
                : -1;

        const nextIndex =
            currentIndex + 1;

        if (
            nextIndex >= queue.length
        ) {

            showToast(
                "🎵 End of Recently Added"
            );

            return;
        }

        playHomeTrack(
            nextIndex
        );

        return;
    }


    if (
        currentPlayerSource ===
        "library"
    ) {

        const queue =
            getLibraryQueue();

        if (!queue.length) {
            return;
        }

        const nextIndex = playerShuffle
            ? Math.floor(Math.random() * queue.length)
            : currentLibraryIndex + 1;

        if (
            nextIndex >= queue.length
        ) {

            showToast(
                "🎵 End of Library"
            );

            return;
        }

        playLibraryTrack(
            nextIndex
        );

        return;
    }
}

function playPreviousTrack() {

    if (
        currentPlayerSource ===
        "home"
    ) {

        const queue =
            window.xrobHomeQueue || [];

        if (!queue.length) {
            return;
        }

        const currentIndex =
            Number.isInteger(
                window.xrobHomeQueueIndex
            )
                ? window.xrobHomeQueueIndex
                : 0;

        if (
            audio &&
            audio.currentTime > 3
        ) {

            audio.currentTime = 0;

            return;
        }

        const previousIndex =
            currentIndex - 1;

        if (
            previousIndex < 0
        ) {

            showToast(
                "🎵 This is the first track"
            );

            return;
        }

        playHomeTrack(
            previousIndex
        );

        return;
    }


    if (
        currentPlayerSource ===
        "library"
    ) {

        const queue =
            getLibraryQueue();

        if (!queue.length) {
            return;
        }

        if (
            audio &&
            audio.currentTime > 3
        ) {

            audio.currentTime = 0;

            return;
        }

        const previousIndex =
            currentLibraryIndex - 1;

        if (
            previousIndex < 0
        ) {

            showToast(
                "🎵 This is the first library track"
            );

            return;
        }

        playLibraryTrack(
            previousIndex
        );
    }
}


function renderRecentlyAdded(
    recent
) {

    const container =
        document.getElementById(
            "recentTracks"
        );

    if (!container) {
        return;
    }

    container.innerHTML = "";

    if (
        !Array.isArray(recent) ||
        !recent.length
    ) {

        container.innerHTML = `
            <div class="home-empty">
                No music in your library yet.
            </div>
        `;

        return;
    }


    window.xrobHomeQueue =
        recent;

    if (
        !Number.isInteger(
            window.xrobHomeQueueIndex
        )
    ) {

        window.xrobHomeQueueIndex =
            -1;
    }


    recent.forEach(
        (
            track,
            index
        ) => {

            const card =
                document.createElement(
                    "button"
                );

            card.type =
                "button";

            card.className =
                "recent-card";

            card.dataset.type =
                "home";


            const img =
                document.createElement(
                    "img"
                );

            img.src =
                track.cover ||
                "https://via.placeholder.com/100?text=Music";

            img.alt = "";

            img.loading =
                "lazy";


            img.addEventListener(
                "error",
                () => {

                    img.src =
                        "https://via.placeholder.com/100?text=Music";

                },
                {
                    once: true
                }
            );


            const title =
                document.createElement(
                    "div"
                );

            title.className =
                "recent-card-title";

            title.textContent =
                track.title ||
                "Unknown Track";


            const artist =
                document.createElement(
                    "div"
                );

            artist.className =
                "recent-card-artist";

            artist.textContent =
                track.artist ||
                "Unknown Artist";


            card.appendChild(
                img
            );

            card.appendChild(
                title
            );

            card.appendChild(
                artist
            );


            track._card =
                card;


            card.addEventListener(
                "click",
                () => {

                    window.xrobHomeQueueIndex =
                        index;

                    playHomeTrack(
                        index
                    );
                }
            );


            container.appendChild(
                card
            );

        }
    );
}


async function loadHome() {

    const container =
        document.getElementById(
            "recentTracks"
        );

    if (!container) {
        return;
    }


    const cachedRecent =
        loadRecentlyAddedCache();


    if (
        cachedRecent.length
    ) {

        recentTracksCache =
            cachedRecent;

        renderRecentlyAdded(
            cachedRecent
        );

        hideLoadingCircle(
            "recent"
        );

    } else {

        updateLoadingCircle(
            "recent",
            5,
            "Loading Recently Added..."
        );

        container.innerHTML =
            "";
    }


    const controller =
        new AbortController();

    const timeout =
        setTimeout(
            () =>
                controller.abort(),
            15000
        );


    try {

        if (
            !cachedRecent.length
        ) {

            updateLoadingCircle(
                "recent",
                15,
                "Connecting to Xrob Music..."
            );
        }


        const response =
            await fetch(
                "api/home",
                {
                    cache:
                        "no-store",

                    signal:
                        controller.signal
                }
            );


        if (!response.ok) {

            throw new Error(
                `Home API returned HTTP ${response.status}`
            );
        }


        const data =
            await response.json();


        const stats =
            data.stats || {};


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
                    value ?? 0;
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
            data.active_downloads || 0
        );


        const recent =
            Array.isArray(
                data.recently_added
            )
                ? data.recently_added
                : [];


        recentTracksCache =
            recent;

        saveRecentlyAddedCache(
            recent
        );


        renderRecentlyAdded(
            recent
        );


        updateLoadingCircle(
            "recent",
            100,
            "Recently Added ready"
        );


        setTimeout(
            () =>
                hideLoadingCircle(
                    "recent"
                ),
            250
        );


    } catch (error) {

        console.error(
            "Home loading failed:",
            error
        );


        if (
            cachedRecent.length
        ) {

            renderRecentlyAdded(
                cachedRecent
            );

            hideLoadingCircle(
                "recent"
            );

            showToast(
                "⚠️ Showing cached Recently Added"
            );

        } else {

            hideLoadingCircle(
                "recent"
            );

            container.innerHTML = `
                <div class="home-empty">

                    <div class="empty-icon">
                        ⚠️
                    </div>

                    <div class="empty-title">
                        Could not load Recently Added
                    </div>

                    <div class="empty-text">
                        ${escapeHtml(
                            error.message ||
                            "Unknown error"
                        )}
                    </div>

                    <button
                        type="button"
                        class="save-btn"
                        onclick="loadHome()"
                    >
                        🔄 Try Again
                    </button>

                </div>
            `;
        }

    } finally {

        clearTimeout(
            timeout
        );
    }
}


/* ============================================================
   WEBSOCKET
   ============================================================ */

function initWebSocket() {

    if (
        socket &&
        (
            socket.readyState === WebSocket.OPEN ||
            socket.readyState === WebSocket.CONNECTING
        )
    ) {
        return;
    }


    const protocol =
        location.protocol === "https:"
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

            console.log(
                "Xrob Music WebSocket connected"
            );

        };


    socket.onmessage =
        event => {

            try {

                const data =
                    JSON.parse(
                        event.data
                    );


                if (
                    data.type === "task_update"
                ) {

                    pollTasks();

                }

            } catch (error) {

                console.warn(
                    "WebSocket message:",
                    error
                );
            }
        };


    socket.onerror =
        error => {

            console.warn(
                "WebSocket error:",
                error
            );
        };


    socket.onclose =
        () => {

            socket = null;

            scheduleWebSocketReconnect();
        };
}


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


/* ============================================================
   INFINITE SCROLL
   ============================================================ */

function bindInfiniteScroll() {

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


            const nearBottom =
                window.innerHeight +
                window.scrollY >=
                document.documentElement.scrollHeight -
                500;


            if (nearBottom) {
                loadMoreResults();
            }
        },
        {
            passive: true
        }
    );
}


function playHomeTrack(index) {

    currentPlayerSource = "home";

    const queue =
        window.xrobHomeQueue || [];

    if (
        index < 0 ||
        index >= queue.length
    ) {
        return;
    }

    const track =
        queue[index];

    const streamUrl =
        track.stream ||
        "";

    if (!streamUrl) {

        showToast(
            "❌ Track stream URL unavailable"
        );

        return;
    }

    window.xrobHomeQueueIndex =
        index;

    const card =
        track._card || null;

    if (activePreviewBtn) {

        resetPreviewButton(
            activePreviewBtn
        );
    }

    activePreviewBtn =
        card;

    if (card) {

        card.classList.add(
            "playing"
        );
    }

    toggleAudioStream(
        card ||
            document.createElement("button"),
        streamUrl,
        "home",
        track.title,
        track.artist,
        track.cover
    );
}

async function refreshLibrary() {
    const button = document.getElementById("libraryRefreshButton");
    if (button) button.disabled = true;
    try {
        updateLoadingCircle("library", 10, "Rescanning music library...");
        const response = await fetch("api/library/scan", { method: "POST", cache: "no-store" });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || "Library refresh failed.");
        await refreshLibraryCache();
        await loadStats();
        renderLibraryView();
        updateLoadingCircle("library", 100, "Library ready");
        showToast(`✅ Library refreshed • ${data.tracks || rawLibraryFiles.length} tracks`);
    } catch (error) {
        showToast("❌ " + (error.message || "Library refresh failed."));
    } finally {
        setTimeout(() => hideLoadingCircle("library"), 250);
        if (button) button.disabled = false;
    }
}

function renderLocalIcons() {
    const paths = {
        house: 'M3 10.5 12 3l9 7.5v9a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 19.5zM9 21v-6h6v6',
        search: 'm21 21-4.35-4.35M10.8 18a7.2 7.2 0 1 1 0-14.4 7.2 7.2 0 0 1 0 14.4z',
        download: 'M12 3v11m0 0 4-4m-4 4-4-4M4 19h16',
        library: 'M4 5.5A2.5 2.5 0 0 1 6.5 3H20v16H6.5A2.5 2.5 0 0 1 4 16.5zM4 16.5V5.5',
        settings: 'M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7zM19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-1.9 1.9-.06-.06A1.7 1.7 0 0 0 16 18.44a1.7 1.7 0 0 0-1 .56 1.7 1.7 0 0 0-.44 1.14V20H12v-.08A1.7 1.7 0 0 0 10.9 18.4a1.7 1.7 0 0 0-1.83.38L9 18.85l-1.9-1.9.06-.06A1.7 1.7 0 0 0 7.56 15a1.7 1.7 0 0 0-1.14-.44H6V12h.08A1.7 1.7 0 0 0 7.6 10.9a1.7 1.7 0 0 0-.38-1.83L7.15 9l1.9-1.9.06.06A1.7 1.7 0 0 0 11 7.56a1.7 1.7 0 0 0 .44-1.14V6H14v.08A1.7 1.7 0 0 0 15.1 7.6a1.7 1.7 0 0 0 1.83-.38L17 7.15l1.9 1.9-.06.06A1.7 1.7 0 0 0 18.44 11a1.7 1.7 0 0 0 1.14.44H20V14h-.08A1.7 1.7 0 0 0 19.4 15z',
        save: 'M5 3h12l3 3v15H4V3zm3 0v6h8V3M8 21v-6h8v6',
        rotate: 'M3 12a9 9 0 1 0 3-6.7M3 4v5h5',
        music: 'M9 18V5l10-2v13M9 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0m10-2a3 3 0 1 1-6 0 3 3 0 0 1 6 0',
        user: 'M20 21a8 8 0 0 0-16 0M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8',
        disc: 'M12 12h.01M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z',
        play: 'm8 5 11 7-11 7z',
        hard: 'M3 5h18v14H3zM7 9h10M7 13h5',
        smartphone: 'M7 2h10v20H7zM11 18h2',
        broom: 'm3 21 9-9m2-9 7 7M16 3l5 5',
        'skip-back': 'M19 20 9 12l10-8v16M5 19V5',
        'skip-forward': 'm5 4 10 8-10 8V4m14 15V5',
        'volume-2': 'M11 5 6 9H3v6h3l5 4zM15.5 8.5a5 5 0 0 1 0 7M18.5 5.5a9 9 0 0 1 0 13',
        shuffle: 'm3 3 18 18M16 3h5v5M3 21l5-5m8 0h5v5',
        refresh: 'M20 11a8 8 0 0 0-14.9-4M4 5v4h4M4 13a8 8 0 0 0 14.9 4M20 19v-4h-4',
    }
    document.querySelectorAll('[data-lucide]').forEach(el => {
        const name = el.getAttribute('data-lucide') || '';
        const path = paths[name] || paths[name.replace(/-(.)/g, (_, c) => c)] || null;
        const svg = document.createElementNS('http://www.w3.org/2000/svg','svg');
        svg.setAttribute('viewBox','0 0 24 24');
        svg.setAttribute('fill','none');
        svg.setAttribute('stroke','currentColor');
        svg.setAttribute('stroke-width','2');
        svg.setAttribute('stroke-linecap','round');
        svg.setAttribute('stroke-linejoin','round');
        svg.setAttribute('aria-hidden','true');
        if (path) {
            const pathNode = document.createElementNS('http://www.w3.org/2000/svg','path');
            pathNode.setAttribute('d', path);
            svg.appendChild(pathNode);
        }
        el.replaceWith(svg);
    });
}

async function initializeApp() {

    renderLocalIcons();
    cacheDom();

    toggleTheme(
        localStorage.getItem(
            "xrob_music_theme"
        ) || "dark"
    );


    bindAudioEvents();
    bindPlayerControls();
    bindSearch();
    bindInfiniteScroll();
    document.getElementById("set_format")?.addEventListener("change", updateQualityState);
    document.getElementById("settings-save")?.addEventListener("click", saveSettings);
    document.getElementById("settings-reset")?.addEventListener("click", resetSettings);
    document.getElementById("libraryRefreshButton")?.addEventListener("click", refreshLibrary);
    document.getElementById("libSearchQuery")?.addEventListener("input", () => {
        const input = document.getElementById("libSearchQuery");
        const clear = document.getElementById("librarySearchClear");
        if (clear) clear.hidden = !(input?.value || "").trim();
        filterLibrary();
    });
    document.getElementById("librarySearchClear")?.addEventListener("click", () => {
        const input = document.getElementById("libSearchQuery");
        if (input) input.value = "";
        const clear = document.getElementById("librarySearchClear");
        if (clear) clear.hidden = true;
        filterLibrary();
        input?.focus();
    });
    document.querySelectorAll(".library-tab").forEach(button => button.addEventListener("click", () => {
        libraryView = button.dataset.libraryView || "tracks";
        selectedArtistId = null;
        selectedAlbumId = null;
        document.querySelectorAll(".library-tab").forEach(item => item.classList.toggle("active", item === button));
        filterLibrary();
    }));

    await refreshLibraryCache();
    await loadSettings();

    await pollTasks(true);

    await loadStats();


    handleHash();


    initWebSocket();


    restorePlayerState();


    setInterval(
        () => pollTasks(),
        2000
    );
}


if (
    document.readyState === "loading"
) {

    document.addEventListener(
        "DOMContentLoaded",
        initializeApp,
        {
            once: true
        }
    );

} else {

    initializeApp();
}


/* ============================================================
   GLOBAL FUNCTIONS
   ============================================================ */

window.navigate = navigate;
window.switchTab = switchTab;
window.filterLibrary = filterLibrary;
window.openArtist = openArtist;
window.playAlbum = playAlbum;
window.playLibraryTrack = playLibraryTrack;
window.shuffleLibrary = shuffleLibrary;

window.toggleTheme = toggleTheme;

window.searchMusic = searchMusic;
window.loadMoreResults = loadMoreResults;

window.loadLibrary = loadLibrary;
window.refreshLibrary = refreshLibrary;
window.openAlbum = openAlbum;
window.filterLibrary = filterLibrary;
window.deleteFile = deleteFile;

window.loadDownloads = loadDownloads;
window.startDownload = startDownload;
window.cancelTask = cancelTask;
window.removeDownloadTask =
    removeDownloadTask;
window.clearDoneTasks =
    clearDoneTasks;

window.loadSettings = loadSettings;
window.saveSettings = saveSettings;

window.toggleAudioStream =
    toggleAudioStream;
