"use strict";

/* ============================================================
   GLOBAL STATE
   ============================================================ */

let socket = null;
let socketReconnectTimer = null;

let completedSet = new Set();

let rawLibraryFiles = [];
let libraryFilesSet = new Set();

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

    return Array.isArray(
        rawLibraryFiles
    )
        ? rawLibraryFiles
        : [];
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


    seek.value =
        (
            audio.currentTime /
            audio.duration
        ) * 100;


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

    /*
     * Only toggle the visual state.
     *
     * IMPORTANT:
     * Do NOT change textContent here.
     *
     * Home recent cards contain an image and text.
     * Changing textContent on those cards destroys their HTML.
     */

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

    /*
     * Search/library buttons are normal buttons.
     * Home cards are NOT normal preview buttons.
     */
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


    /*
     * Clicking the same track toggles play/pause.
     */
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


    /*
     * Stop previous track.
     */
    if (activePreviewBtn) {

        resetPreviewButton(
            activePreviewBtn
        );
    }


    activePreviewBtn = button;

    button.dataset.type =
        type || "search";


    /*
     * Only actual preview buttons receive loading text.
     *
     * Home recent cards should remain intact.
     */
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

                const queue =
                    getLibraryQueue();

                const nextIndex =
                    currentLibraryIndex + 1;

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

            /*
             * No more Home tracks.
             */
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


    seek?.addEventListener(
        "input",
        () => {

            if (
                audio &&
                Number.isFinite(
                    audio.duration
                )
            ) {

                audio.currentTime =
                    (
                        Number(
                            seek.value
                        ) / 100
                    ) *
                    audio.duration;
            }
        }
    );


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

async function loadSettings() {

    try {

        const response =
            await fetch(
                "api/settings",
                {
                    cache: "no-store"
                }
            );


        if (!response.ok) {

            throw new Error(
                `HTTP ${response.status}`
            );
        }


        const settings =
            await response.json();


        const setValue = (
            id,
            value
        ) => {

            const element =
                document.getElementById(id);

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
                document.getElementById(id);

            if (element) {

                element.checked =
                    Boolean(value);
            }
        };


        setValue(
            "set_format",
            settings.audio_format || "mp3"
        );


        setValue(
            "set_quality",
            settings.audio_quality || "320K"
        );


        setValue(
            "set_max_results",
            settings.max_results || 20
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
            settings.subsonic_user || "admin"
        );


        const serverUrl =
            window.XrobArpeggi?.getServerUrl?.();


        setValue(
            "amperfy-server-url",
            serverUrl ||
            (
                location.protocol +
                "//" +
                location.hostname +
                ":8100"
            )
        );

    } catch (error) {

        console.warn(
            "Settings load:",
            error
        );
    }
}


async function saveSettings() {

    const getValue = id =>
        document.getElementById(id)?.value || "";


    const getChecked = id =>
        document.getElementById(id)?.checked ?? false;


    const maxResultsRaw =
        Number(
            getValue("set_max_results") || 20
        );


    const maxResults =
        Math.max(
            5,
            Math.min(
                50,
                Number.isFinite(maxResultsRaw)
                    ? maxResultsRaw
                    : 20
            )
        );


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

        max_results:
            maxResults
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

        rawLibraryFiles =
            cache.files;

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


        rawLibraryFiles =
            data.files || [];

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
                    cache: "no-store",
                    signal: controller.signal
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

    const list =
        document.getElementById(
            "libraryList"
        );

    if (!list) {
        return;
    }

    const hasCache =
        loadLibraryCache();


    if (hasCache) {

        /*
         * Show cached library immediately.
         */
        filterLibrary();

        updateLoadingCircle(
            "library",
            60,
            "Refreshing library..."
        );

    } else {

        /*
         * First ever load.
         */
        updateLoadingCircle(
            "library",
            5,
            "Preparing library..."
        );

        list.innerHTML = "";
    }


    try {

        updateLoadingCircle(
            "library",
            25,
            "Checking music files..."
        );

        await refreshLibraryCache();


        updateLoadingCircle(
            "library",
            80,
            "Preparing tracks..."
        );


        filterLibrary();


        updateLoadingCircle(
            "library",
            95,
            "Updating statistics..."
        );


        loadStats().catch(
            error =>
                console.warn(
                    "Library stats:",
                    error
                )
        );


        updateLoadingCircle(
            "library",
            100,
            "Library ready"
        );


        setTimeout(
            () =>
                hideLoadingCircle(
                    "library"
                ),
            300
        );


    } catch (error) {

        console.error(
            "Library loading failed:",
            error
        );

        /*
         * If the server is unavailable but we have
         * cached data, KEEP showing the library.
         */
        if (
            hasCache &&
            rawLibraryFiles.length
        ) {

            filterLibrary();

            hideLoadingCircle(
                "library"
            );

            showToast(
                "⚠️ Showing cached library"
            );

            return;
        }


        hideLoadingCircle(
            "library"
        );


        list.innerHTML = `
            <div class="downloads-empty">

                <div class="empty-icon">
                    ⚠️
                </div>

                <div class="empty-title">
                    Could not load library
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
                    onclick="loadLibrary()"
                >
                    🔄 Try Again
                </button>

            </div>
        `;
    }
}


function filterLibrary() {

    const list =
        document.getElementById(
            "libraryList"
        );


    if (!list) {
        return;
    }


    const input =
        document.getElementById(
            "libSearchQuery"
        );


    const query =
        String(
            input?.value || ""
        )
        .toLowerCase()
        .trim();


    const files =
        rawLibraryFiles.filter(
            file =>
                String(
                    file.name || ""
                )
                .toLowerCase()
                .includes(query)
        );


    list.innerHTML = "";


    if (!files.length) {

        list.innerHTML = `
            <div class="downloads-empty">

                <div class="empty-icon">
                    🎵
                </div>

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
        file => {

            const encoded =
                encodeURIComponent(
                    file.name || ""
                );


            const cover =
                `api/library/cover/${encoded}`;


            const stream =
                `api/library/stream/${encoded}`;


            const card =
                document.createElement(
                    "article"
                );


            card.className =
                "result-card";

            card.dataset.libraryName =
                file.name;


            card.innerHTML = `

                <div class="thumb-wrapper">

                    <img
                        src="${escapeHtml(cover)}"
                        alt=""
                        loading="lazy"
                    >

                </div>


                <div class="track-info">

                    <div class="track-title">
                        ${escapeHtml(
                            file.name
                        )}
                    </div>

                    <div class="track-artist">
                        📦 ${escapeHtml(
                            file.size || ""
                        )}
                    </div>

                </div>


                <div class="btn-group">

                    <button
                        type="button"
                        class="btn-preview"
                    >
                        ▶ Play
                    </button>

                    <button
                        type="button"
                        class="btn-danger"
                    >
                        🗑 Delete
                    </button>

                </div>
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


            const play =
                card.querySelector(
                    ".btn-preview"
                );


            const remove =
                card.querySelector(
                    ".btn-danger"
                );


            if (play) {

                play.dataset.type =
                    "library";


                play.addEventListener(
                    "click",
                    () =>
                        toggleAudioStream(
                            play,
                            stream,
                            "library",
                            file.name,
                            "Local Library",
                            cover
                        )
                );
            }

            /* PLAY LIBRARY TRACK BY CLICKING THE CARD */
            card.addEventListener(
                "click",
                (event) => {

                    if (
                        event.target.closest(".btn-danger")
                    ) {
                        return;
                    }

                    if (
                        event.target.closest(".btn-preview")
                    ) {
                        return;
                    }

                    currentPlayerSource =
                        "library";

                    currentLibraryIndex =
                        rawLibraryFiles.findIndex(
                            libraryFile =>
                                libraryFile.name ===
                                file.name
                        );

                    if (play) {

                        toggleAudioStream(
                            play,
                            stream,
                            "library",
                            file.name,
                            "Local Library",
                            cover
                        );
                    }
                }
            );


            if (remove) {

                remove.addEventListener(
                    "click",
                    () =>
                        deleteFile(
                            file.name
                        )
                );
            }


            list.appendChild(card);
        }
    );
}


function playLibraryTrack(
    index
) {

    const queue =
        getLibraryQueue();

    if (
        index < 0 ||
        index >= queue.length
    ) {
        return;
    }

    const file =
        queue[index];

    currentPlayerSource =
        "library";

    currentLibraryIndex =
        index;

    const encoded =
        encodeURIComponent(
            file.name
        );

    const cover =
        `api/library/cover/${encoded}`;

    const stream =
        `api/library/stream/${encoded}`;

    /*
     * We need a real button because
     * toggleAudioStream expects one.
     */
    let button =
        document.querySelector(
            `.result-card[data-library-name="${CSS.escape(file.name)}"] .btn-preview`
        );

    if (!button) {

        button =
            document.createElement(
                "button"
            );

        button.className =
            "btn-preview";

        button.dataset.type =
            "library";
    }

    toggleAudioStream(
        button,
        stream,
        "library",
        file.name,
        "Local Library",
        cover
    );
}


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

        return;
    }


    currentQuery = query;
    currentPage = 1;
    hasMoreResults = true;
    isLoadingMore = false;


    status.textContent =
        "🔍 Searching...";


    results.innerHTML = "";


    await refreshLibraryCache();


    const button =
        document.getElementById(
            "searchBtn"
        );


    if (button) {
        button.disabled = true;
    }


    try {

        const response =
            await fetch(
                `api/search?q=${
                    encodeURIComponent(query)
                }&page=1`,
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
                "Search failed."
            );
        }


        if (
            !Array.isArray(data) ||
            !data.length
        ) {

            status.textContent =
                "No results found.";

            hasMoreResults = false;

            return;
        }


        status.textContent = "";

        renderItems(data);

    } catch (error) {

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

        /*
         * Do not permanently disable infinite scroll
         * just because one request failed.
         */
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

    } else {

        actionButton.className =
            "download-remove-btn";


        actionButton.textContent =
            "Remove";


        actionButton.addEventListener(
            "click",
            () =>
                removeDownloadTask(
                    task.id
                )
        );
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

    /*
     * HOME
     */
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


    /*
     * LIBRARY
     */
    if (
        currentPlayerSource ===
        "library"
    ) {

        const queue =
            getLibraryQueue();

        if (!queue.length) {
            return;
        }

        const nextIndex =
            currentLibraryIndex + 1;

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

    /*
     * HOME QUEUE
     */
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

        /*
         * More than 3 seconds:
         * restart current track.
         */
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


    /*
     * LIBRARY QUEUE
     */
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


    /*
     * Save queue for Previous / Next / Auto-next.
     */
    window.xrobHomeQueue =
        recent;

    /*
     * Don't reset the queue position if
     * we're refreshing an existing queue.
     */
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


            /*
             * Save card reference.
             */
            track._card =
                card;


            /*
             * Click = play track.
             */
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


    /*
     * ---------------------------------------------
     * LOAD CACHED RECENTLY ADDED IMMEDIATELY
     * ---------------------------------------------
     */

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

        /*
         * Hide the loader immediately because
         * we already have usable cached content.
         */
        hideLoadingCircle(
            "recent"
        );

    } else {

        /*
         * First load.
         */
        updateLoadingCircle(
            "recent",
            5,
            "Loading Recently Added..."
        );

        container.innerHTML =
            "";
    }


    /*
     * ---------------------------------------------
     * FETCH FRESH HOME DATA
     * ---------------------------------------------
     */

    const controller =
        new AbortController();

    const timeout =
        setTimeout(
            () =>
                controller.abort(),
            15000
        );


    try {

        /*
         * Only show loading progress when
         * there is no cached data.
         */
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


        /*
         * ---------------------------------------------
         * SAVE FRESH RECENTLY ADDED CACHE
         * ---------------------------------------------
         */

        recentTracksCache =
            recent;

        saveRecentlyAddedCache(
            recent
        );


        /*
         * Refresh the visible tracks.
         */
        renderRecentlyAdded(
            recent
        );


        /*
         * Fresh data is ready.
         */
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


        /*
         * If cached Recently Added exists,
         * KEEP IT visible.
         */
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


/* ============================================================
   COPY ARPEGGI SERVER URL
   ============================================================ */

function bindArpeggiCopy() {

    const button =
        document.getElementById(
            "amperfy-copy-url"
        );


    if (!button) {
        return;
    }


    button.addEventListener(
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
                    "❌ Server URL unavailable"
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

                /*
                 * Fallback for older browsers / HTTP.
                 */
                try {

                    input.focus();
                    input.select();

                    document.execCommand(
                        "copy"
                    );

                    showToast(
                        "📋 Server URL copied"
                    );

                } catch {

                    showToast(
                        "❌ Could not copy URL"
                    );
                }
            }
        }
    );
}


/* ============================================================
   STARTUP
   ============================================================ */

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

async function initializeApp() {

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
    bindArpeggiCopy();


    /*
     * Load initial data.
     */
    await refreshLibraryCache();

    await pollTasks(true);

    await loadStats();


    handleHash();


    initWebSocket();


    restorePlayerState();


    /*
     * Fallback polling remains active even if
     * WebSocket isn't available.
     */
    setInterval(
        () => pollTasks(),
        2000
    );
}


/*
 * The script is loaded at the end of <body>,
 * but this also works safely if it is moved.
 */
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

window.toggleTheme = toggleTheme;

window.searchMusic = searchMusic;
window.loadMoreResults = loadMoreResults;

window.loadLibrary = loadLibrary;
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
