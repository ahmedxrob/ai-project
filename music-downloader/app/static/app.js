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

let playerRepeatMode = localStorage.getItem("xrob_music_repeat") || "off";
let enhancedQueue = [];
let enhancedQueueIndex = -1;
let enhancedNaturalQueue = [];
let homeNaturalQueue = [];
let enhancedSongPositions = {};
let lastRecordedTrackId = null;
const ENHANCED_QUEUE_KEY = "xrob_music_up_next_queue";
const ENHANCED_REPEAT_KEY = "xrob_music_repeat";

function currentSongId() {
    const useEnhanced = currentPlayerSource === "library" && enhancedQueue.length;
    const q = useEnhanced ? enhancedQueue : (currentPlayerSource === "library" ? getLibraryQueue() : (window.xrobHomeQueue || []));
    const idx = useEnhanced ? enhancedQueueIndex : (currentPlayerSource === "library" ? currentLibraryIndex : window.xrobHomeQueueIndex);
    const item = Number.isInteger(idx) && idx >= 0 ? q[idx] : null;
    return item?.id || null;
}

function saveEnhancedQueue() {
    try { localStorage.setItem(ENHANCED_QUEUE_KEY, JSON.stringify({queue: enhancedQueue, natural: enhancedNaturalQueue, index: enhancedQueueIndex})); } catch (_) {}
}

function loadEnhancedQueue() {
    try { const v=JSON.parse(localStorage.getItem(ENHANCED_QUEUE_KEY)||"null"); if(v?.queue?.length) { enhancedQueue=v.queue; enhancedNaturalQueue=Array.isArray(v.natural)&&v.natural.length?v.natural:[...v.queue]; enhancedQueueIndex=Number.isInteger(v.index)?v.index:-1; libraryPlaybackQueue=[...enhancedQueue]; currentLibraryIndex=enhancedQueueIndex; } } catch (_) {}
}

async function loadEnhancedPositions() {
    try { const r=await fetch("api/player/positions",{cache:"no-store"}); if(r.ok) enhancedSongPositions=await r.json(); } catch (_) {}
}

function persistCurrentPosition() {
    const id=currentSongId();
    if(!id || !audio) return;
    const position=Number(audio.currentTime||0), duration=Number(audio.duration||0);
    enhancedSongPositions[id]={position,duration,updated_at:Date.now()/1000};
    try { fetch("api/player/position",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({song_id:id,position,duration})}); } catch (_) {}
}

function recordPlay(id) {
    if(!id || lastRecordedTrackId===id) return;
    lastRecordedTrackId=id;
    try { fetch("api/player/history",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({song_id:id,duration:Number(audio?.duration||0),position:Number(audio?.currentTime||0)})}); } catch (_) {}
}

function applyRepeatLabel() { const b=document.getElementById("queueRepeat"); if(b) b.textContent=`Repeat: ${playerRepeatMode === "track" ? "Track" : playerRepeatMode === "queue" ? "Queue" : "Off"}`; }
function cycleRepeatMode() { playerRepeatMode = playerRepeatMode === "off" ? "track" : playerRepeatMode === "track" ? "queue" : "off"; localStorage.setItem(ENHANCED_REPEAT_KEY,playerRepeatMode); applyRepeatLabel(); }


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
   LOCAL ICON RENDERER
   ============================================================ */
const LOCAL_ICON_PATHS = {
    house: '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/><path d="M9 21v-6h6v6"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>',
    download: '<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/>',
    library: '<path d="M4 19.5V6.5a2 2 0 0 1 2-2h13v15H6a2 2 0 0 0-2 2Z"/><path d="M6 19.5h13"/>',
    'pencil-line': '<path d="m12 20 9-9"/><path d="m16 4 4 4"/><path d="M5 20h4l10-10-4-4L5 16v4Z"/>',
    settings: '<path d="M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z"/><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-1.42 1.42-.06-.06a1.7 1.7 0 0 0-1.88-.34 1.7 1.7 0 0 0-1.03 1.56V19.6h-2v-.09a1.7 1.7 0 0 0-1.03-1.56 1.7 1.7 0 0 0-1.88.34l-.06.06-1.42-1.42.06-.06A1.7 1.7 0 0 0 9.4 15a1.7 1.7 0 0 0-1.56-1.03H7.75v-2h.09A1.7 1.7 0 0 0 9.4 10.44a1.7 1.7 0 0 0-.34-1.88L9 8.5l1.42-1.42.06.06a1.7 1.7 0 0 0 1.88.34A1.7 1.7 0 0 0 13.39 6V5.9h2V6a1.7 1.7 0 0 0 1.03 1.48 1.7 1.7 0 0 0 1.88-.34l.06-.06L19.78 8.5l-.06.06a1.7 1.7 0 0 0-.34 1.88A1.7 1.7 0 0 0 20.94 11.5H21v2h-.06A1.7 1.7 0 0 0 19.4 15Z"/>',
    'music-2': '<path d="M9 18V5l10-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="16" cy="16" r="3"/>',
    'user-round': '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    'disc-3': '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2"/><path d="M12 5v2"/>',
    broom: '<path d="m16 3 5 5"/><path d="m14 5 5 5"/><path d="m17 8-9 9"/><path d="M6 21h5"/><path d="M3 18 8 13l3 3-5 5H3Z"/>',
    'refresh-cw': '<path d="M20 11a8 8 0 0 0-14.9-4L3 10"/><path d="M3 4v6h6"/><path d="M4 13a8 8 0 0 0 14.9 4l2.1-3"/><path d="M21 20v-6h-6"/>',
    'square-pen': '<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L8 18l-4 1 1-4Z"/>',
    save: '<path d="M5 3h12l3 3v15H4V3Z"/><path d="M8 3v5h8V3"/><path d="M8 21v-6h8v6"/>',
    'rotate-ccw': '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/>',
    'arrow-down': '<path d="M12 5v14"/><path d="m18 13-6 6-6-6"/>',
};

function renderLocalIcons(root = document) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('[data-lucide]').forEach((el) => {
        const name = el.getAttribute('data-lucide') || '';
        const paths = LOCAL_ICON_PATHS[name] || '<circle cx="12" cy="12" r="9"/><path d="M12 8v4"/><path d="M12 16h.01"/>';
        const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('viewBox', '0 0 24 24');
        svg.setAttribute('fill', 'none');
        svg.setAttribute('stroke', 'currentColor');
        svg.setAttribute('stroke-width', '2');
        svg.setAttribute('stroke-linecap', 'round');
        svg.setAttribute('stroke-linejoin', 'round');
        svg.setAttribute('aria-hidden', el.getAttribute('aria-hidden') || 'true');
        svg.innerHTML = paths;
        for (const attr of el.attributes) {
            if (!['data-lucide', 'aria-hidden'].includes(attr.name)) svg.setAttribute(attr.name, attr.value);
        }
        el.replaceWith(svg);
    });
}

/* ============================================================
   HELPERS
   ============================================================ */

function getLibraryQueue() {
    return Array.isArray(libraryPlaybackQueue) ? libraryPlaybackQueue : (Array.isArray(rawLibraryFiles) ? rawLibraryFiles : []);
}

function queueId(track) {
    return track?.id || track?.name || track?.path || null;
}

function shuffledCopy(items) {
    const copy = [...items];
    for (let i = copy.length - 1; i > 0; i -= 1) {
        const j = Math.floor(Math.random() * (i + 1));
        [copy[i], copy[j]] = [copy[j], copy[i]];
    }
    return copy;
}

function syncLibraryQueue() {
    if (enhancedQueue.length) {
        libraryPlaybackQueue = enhancedQueue;
        currentLibraryIndex = enhancedQueueIndex;
    } else if (!Array.isArray(libraryPlaybackQueue) || !libraryPlaybackQueue.length) {
        libraryPlaybackQueue = Array.isArray(rawLibraryFiles) ? [...rawLibraryFiles] : [];
    }
}

function getActiveQueue() {
    if (currentPlayerSource === "library") {
        syncLibraryQueue();
        return getLibraryQueue();
    }
    return Array.isArray(window.xrobHomeQueue) ? window.xrobHomeQueue : [];
}

function getActiveQueueIndex() {
    return currentPlayerSource === "library"
        ? currentLibraryIndex
        : (Number.isInteger(window.xrobHomeQueueIndex) ? window.xrobHomeQueueIndex : -1);
}

function setActiveQueueIndex(index) {
    if (currentPlayerSource === "library") {
        currentLibraryIndex = index;
        if (enhancedQueue.length) enhancedQueueIndex = index;
    } else {
        window.xrobHomeQueueIndex = index;
    }
}

function setShuffle(enabled, {rebuild = true} = {}) {
    const next = Boolean(enabled);
    if (next === playerShuffle && !rebuild) {
        updateShuffleButtons();
        return;
    }
    playerShuffle = next;
    localStorage.setItem("xrob_music_shuffle", String(playerShuffle));
    updateShuffleButtons();

    if (!rebuild) return;

    const queue = getActiveQueue();
    const index = getActiveQueueIndex();
    if (!queue.length) return;

    if (playerShuffle) {
        if (currentPlayerSource === "library" && enhancedQueue.length && !enhancedNaturalQueue.length) enhancedNaturalQueue = [...queue];
        if (currentPlayerSource === "home" && !homeNaturalQueue.length) homeNaturalQueue = [...queue];
        const current = index >= 0 && index < queue.length ? queue[index] : null;
        const remaining = queue.filter((track, i) => i !== index);
        const ordered = current ? [current, ...shuffledCopy(remaining)] : shuffledCopy(queue);
        if (currentPlayerSource === "library") {
            libraryPlaybackQueue = ordered;
            currentLibraryIndex = current ? 0 : 0;
            if (enhancedQueue.length) {
                enhancedQueue = [...ordered];
                enhancedQueueIndex = currentLibraryIndex;
            }
        } else {
            window.xrobHomeQueue = ordered;
            window.xrobHomeQueueIndex = current ? 0 : 0;
        }
    } else {
        const current = index >= 0 && index < queue.length ? queue[index] : null;
        const base = currentPlayerSource === "library" ? (enhancedQueue.length ? enhancedNaturalQueue : rawLibraryFiles) : (homeNaturalQueue.length ? homeNaturalQueue : (window.xrobHomeQueue || []));
        if (Array.isArray(base) && base.length) {
            const currentId = queueId(current);
            const baseIndex = base.findIndex(t => queueId(t) === currentId);
            if (currentPlayerSource === "library") {
                libraryPlaybackQueue = [...base];
                currentLibraryIndex = Math.max(0, baseIndex);
                if (enhancedQueue.length) {
                    enhancedQueue = [...base];
                    enhancedQueueIndex = currentLibraryIndex;
                }
            } else {
                window.xrobHomeQueue = [...base];
                window.xrobHomeQueueIndex = Math.max(0, baseIndex);
            }
        }
    }
    if (currentPlayerSource === "library") saveEnhancedQueue();
    if (typeof renderEnhancedQueue === "function") renderEnhancedQueue();
}

function updateShuffleButtons() {
    [document.getElementById("gp-shuffle-btn"), document.getElementById("libraryShuffleButton")]
        .forEach(button => button?.classList.toggle("active", playerShuffle));
}

function toggleShuffle() {
    if (!getActiveQueue().length) {
        showToast("No tracks in the current queue");
        return;
    }
    setShuffle(!playerShuffle, {rebuild: true});
}

function shuffleLibrary() {
    if (!rawLibraryFiles.length) { showToast("No tracks to shuffle"); return; }
    libraryView = "tracks";
    selectedArtistId = null;
    selectedAlbumId = null;
    document.querySelectorAll(".library-tab").forEach(btn => btn.classList.toggle("active", btn.dataset.libraryView === "tracks"));
    renderLibraryView();
    currentPlayerSource = "library";
    libraryPlaybackQueue = [...rawLibraryFiles];
    currentLibraryIndex = 0;
    playerShuffle = false;
    setShuffle(true, {rebuild: true});
    playLibraryTrack(0);
}

/* ============================================================
   LOADING CIRCLE
   ============================================================ */
function updateLoadingCircle(type, percent, text = "") {
    const id = type === "library" ? "libraryLoading" : "recentTracksLoading";
    const loading = document.getElementById(id);
    const textElement = document.getElementById(type === "library" ? "libraryLoadingText" : "recentLoadingText");
    if (!loading) return;
    loading.style.display = "flex";
    if (textElement && text) textElement.textContent = text;
}
function updateSearchLoading(percent, text = "") {
    const loading = document.getElementById("searchLoading");
    const textElement = document.getElementById("searchLoadingText");
    if (!loading) return;
    loading.style.display = "flex";
    if (textElement && text) textElement.textContent = text;
}
function smoothSearchLoading(from, to, text) { updateSearchLoading(to, text); }
function hideSearchLoading() { document.getElementById("searchLoading")?.style && (document.getElementById("searchLoading").style.display = "none"); }
function smoothLoading(type, from, to, text) { updateLoadingCircle(type, to, text); }
function hideLoadingCircle(type) { const el = document.getElementById(type === "library" ? "libraryLoading" : "recentTracksLoading"); if (el) el.style.display = "none"; }

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
        "songs-editor",
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

    if (tab === "songs-editor") {
        loadSongEditor();
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
        "songs-editor",
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

            if (typeof playerRepeatMode !== "undefined" && playerRepeatMode === "track") {
                audio.currentTime = 0;
                audio.play().catch(console.error);
                return;
            }

            const queue = getActiveQueue();
            const currentIndex = getActiveQueueIndex();
            if (queue.length && currentIndex >= 0) {
                const atEnd = currentIndex >= queue.length - 1;
                if (playerRepeatMode === "queue" && atEnd) {
                    setActiveQueueIndex(0);
                    if (currentPlayerSource === "library") playLibraryTrack(0);
                    else playHomeTrack(0);
                    return;
                }
                if (!atEnd) {
                    const nextIndex = currentIndex + 1;
                    if (currentPlayerSource === "library") playLibraryTrack(nextIndex);
                    else playHomeTrack(nextIndex);
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

    document.getElementById("gp-shuffle-btn")?.addEventListener("click", toggleShuffle);
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
    const usedLabel = document.getElementById("storageUsedLabel");
    const usedMeta = document.getElementById("storageUsedMeta");
    const fill = document.getElementById("storageProgressFill");
    if (path) path.textContent = data.path || "Not available";
    if (free) free.textContent = `${data.free || "0 B"} free`;
    if (usedLabel) usedLabel.textContent = `${data.used || "0 B"} / ${data.total || "0 B"}`;
    if (usedMeta) usedMeta.textContent = `${data.used || "0 B"} used`;
    const total = Number(data.total_bytes) || 0;
    const used = Number(data.used_bytes) || 0;
    const pct = total > 0 ? Math.min(100, Math.max(0, used / total * 100)) : 0;
    if (fill) fill.style.width = `${pct.toFixed(1)}%`;
    const progress = fill?.parentElement;
    if (progress) progress.setAttribute("aria-valuenow", String(Math.round(pct)));
    if (status) {
        if (!data.exists) { status.textContent = "Library storage is unavailable."; status.dataset.state = "error"; }
        else if (!data.writable) { status.textContent = "Library storage is read-only."; status.dataset.state = "error"; }
        else { status.textContent = "Library storage is ready."; status.dataset.state = "success"; }
    }
}

async function resetSettings() {
    const defaults = {
        audio_format: "mp3",
        audio_quality: "320K",
        embed_thumbnail: true,
        embed_metadata: true,
        organize_by_artist: false,
        scan_enabled: true,
        scan_interval_minutes: 60
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
    setChecked("set_scan_enabled", settings.scan_enabled !== false);
    setValue("set_scan_interval", settings.scan_interval_minutes || 60);
    setValue("set_web_username", settings.web_username || "admin");
    setValue("set_web_password", "");
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
        scan_enabled: getChecked("set_scan_enabled"),
        scan_interval_minutes: Math.max(5, Number(getValue("set_scan_interval") || 60)),
        web_username: getValue("set_web_username") || "admin",
        ...(getValue("set_web_password") ? {web_password:getValue("set_web_password")} : {}),
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

        const statusMap = {
            statusTracks: stats.tracks || 0,
            statusAlbums: stats.albums || 0,
            statusArtists: stats.artists || 0,
            statusPlays: stats.all_play_count || 0,
            statusSize: stats.folder_size || "0 MB"
        };
        Object.entries(statusMap).forEach(([id, value]) => {
            const el = document.getElementById(id);
            if (el) el.textContent = String(value);
        });
        const subsonicStatus = document.getElementById("subsonicStatusValue");
        if (subsonicStatus) subsonicStatus.textContent = `${stats.tracks || 0} tracks ready`;

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
    currentPlayerSource = "library";
    enhancedQueue = [];
    enhancedQueueIndex = -1;
    enhancedNaturalQueue = [...queue];
    libraryPlaybackQueue = [...queue];
    currentLibraryIndex = Math.max(0, Math.min(index, queue.length - 1));
    if (shuffle) {
        playerShuffle = false;
        setShuffle(true, {rebuild: true});
    } else {
        setShuffle(false, {rebuild: false});
        updateShuffleButtons();
    }
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
    const plays = Number(file.play_count ?? file.plays ?? 0);
    card.innerHTML = `<div class="thumb-wrapper"><img src="${escapeHtml(cover)}" alt="" loading="lazy"><span class="track-play-count" title="${plays} play${plays === 1 ? "" : "s"}">▶ ${plays}</span></div><div class="track-info"><div class="track-title">${escapeHtml(file.title || file.name || "Unknown Track")}</div><div class="track-artist">${escapeHtml(file.artist || "Unknown Artist")} · ${escapeHtml(file.album || "Unknown Album")}</div><div class="track-meta-line"><span>${plays === 1 ? "1 play" : `${plays} plays`}</span></div></div><div class="btn-group"><button type="button" class="btn-preview">▶ Play</button><button type="button" class="btn-danger">🗑 Delete</button></div>`;
    card.querySelector("img")?.addEventListener("error", e => e.currentTarget.removeAttribute("src"), { once: true });
    const play = () => { libraryPlaybackQueue = [...queue]; currentLibraryIndex = Math.max(0, queue.findIndex(x => x.id === file.id || x.name === file.name)); currentPlayerSource = "library"; if (typeof setEnhancedQueue === "function") setEnhancedQueue(queue, currentLibraryIndex); toggleAudioStream(card.querySelector(".btn-preview"), stream, "library", file.title || file.name, file.artist || "Unknown Artist", cover); };
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
        card.innerHTML = `<button type="button" class="catalog-main-action"><img class="artist-cover" src="${escapeHtml(artist.cover||"")}" alt="" loading="lazy" onerror="this.style.display='none'"/><div><strong>${escapeHtml(artist.name)}</strong><span>${artist.album_count || 0} album${artist.album_count === 1 ? "" : "s"} · ${artist.song_count || 0} track${artist.song_count === 1 ? "" : "s"}</span></div></button><div class="catalog-actions"><button type="button" class="btn-refresh artist-art-btn">Cover</button><button type="button" class="btn-preview catalog-play">▶ Play</button></div>`;
        card.querySelector(".catalog-main-action")?.addEventListener("click", () => openArtist(artist.id));
        card.querySelector(".catalog-play")?.addEventListener("click", e => { e.stopPropagation(); const tracks = rawLibraryFiles.filter(f => (artist.song_ids || []).includes(f.id)); playQueue(tracks, 0, false); });
        card.querySelector(".artist-art-btn")?.addEventListener("click", e => { e.stopPropagation(); const input=document.createElement("input"); input.type="file"; input.accept="image/jpeg,image/png,image/webp"; input.onchange=async()=>{const file=input.files?.[0]; if(!file)return; const fd=new FormData(); fd.append("upload",file); const rr=await fetch(`api/library/artist-artwork/${encodeURIComponent(artist.id)}`,{method:"POST",body:fd}); if(rr.ok){showToast("✅ Artist cover saved"); renderArtists(list,query);} else showToast("❌ Could not save artist cover");}; input.click(); });
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
    const queue = getActiveQueue();
    if (!queue.length) return;
    const currentIndex = getActiveQueueIndex();
    if (playerRepeatMode === "queue" && currentIndex >= queue.length - 1) {
        if (currentPlayerSource === "library") playLibraryTrack(0);
        else if (currentPlayerSource === "home") playHomeTrack(0);
        return;
    }
    const nextIndex = currentIndex < 0 ? 0 : currentIndex + 1;
    if (nextIndex >= queue.length) {
        showToast(`🎵 End of ${currentPlayerSource === "home" ? "Recently Added" : "Library"}`);
        return;
    }
    if (currentPlayerSource === "library") playLibraryTrack(nextIndex);
    else if (currentPlayerSource === "home") playHomeTrack(nextIndex);
}

function playPreviousTrack() {
    const queue = getActiveQueue();
    if (!queue.length) return;
    const currentIndex = getActiveQueueIndex();

    if (audio && audio.currentTime > 3) {
        audio.currentTime = 0;
        return;
    }

    if (playerRepeatMode === "queue" && currentIndex <= 0) {
        const last = queue.length - 1;
        if (currentPlayerSource === "library") playLibraryTrack(last);
        else if (currentPlayerSource === "home") playHomeTrack(last);
        return;
    }

    const previousIndex = currentIndex - 1;
    if (previousIndex < 0) {
        showToast("🎵 This is the first track");
        return;
    }
    if (currentPlayerSource === "library") playLibraryTrack(previousIndex);
    else if (currentPlayerSource === "home") playHomeTrack(previousIndex);
}

async function checkWebAuth() {
    try { const r=await fetch("api/auth/status",{cache:"no-store"}); if(!r.ok) return false; const d=await r.json(); return !!d.authenticated; } catch (_) { return false; }
}

function showAuthenticatedApp() { document.getElementById("login-screen")?.classList.add("hidden"); const shell=document.getElementById("app-shell"); if(shell) shell.hidden=false; renderLocalIcons(); }

async function handleLoginSubmit(e){
    e.preventDefault();
    const error=document.getElementById("loginError");
    const btn=document.querySelector(".login-submit");
    if(error) error.textContent="";
    const body={username:String(document.getElementById("loginUsername")?.value||"").trim(),password:document.getElementById("loginPassword")?.value||""};
    localStorage.setItem("xrob_music_login_user", body.username);
    if(btn){btn.disabled=true; btn.dataset.originalText=btn.textContent; btn.textContent="Signing in…";}
    try{const r=await fetch("api/auth/login",{method:"POST",headers:{"Content-Type":"application/json"},credentials:"same-origin",body:JSON.stringify(body)}); const d=await r.json().catch(()=>({})); if(!r.ok) throw new Error(d.detail||"Sign in failed"); document.getElementById("loginPassword").value=""; showAuthenticatedApp(); await startAppAfterAuth(); }catch(err){if(error)error.textContent=err.message||"Sign in failed";} finally{if(btn){btn.disabled=false;btn.textContent=btn.dataset.originalText||"Sign in";}}
}


async function logoutWebAuth(){ await fetch("api/auth/logout",{method:"POST"}).catch(()=>{}); location.reload(); }

async function initializeApp() {

    renderLocalIcons();
    const savedLoginUser = localStorage.getItem("xrob_music_login_user");
    if(savedLoginUser && document.getElementById("loginUsername")) document.getElementById("loginUsername").value=savedLoginUser;
    document.getElementById("loginForm")?.addEventListener("submit",handleLoginSubmit);
    setTimeout(()=>document.getElementById("loginUsername")?.focus(),50);
    document.getElementById("logoutButton")?.addEventListener("click",logoutWebAuth);
    if(!(await checkWebAuth())) return;
    showAuthenticatedApp();
    await startAppAfterAuth();
}

async function startAppAfterAuth() {

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
    document.getElementById("songEditorRefresh")?.addEventListener("click",loadSongEditor);
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

    const cached = loadLibraryCache();
    if (cached) renderLibraryView();
    // Fast first paint: library/stats may initially come from the filesystem index.
    // Poll briefly for the background metadata warmup to finish, then refresh once.
    const startupJobs = [refreshLibraryCache(), loadSettings(), loadSongEditor(), pollTasks(true), loadStats(), loadHome()];
    await Promise.allSettled(startupJobs);
    if (rawLibraryFiles.length) renderLibraryView();
    let libraryWarmupChecks = 0;
    const warmupTimer = setInterval(async () => {
        libraryWarmupChecks += 1;
        if (libraryWarmupChecks > 30) return clearInterval(warmupTimer);
        try {
            const r = await fetch('api/library', {cache:'no-store'});
            if (!r.ok) return;
            const d = await r.json();
            if (d.ready) {
                clearInterval(warmupTimer);
                rawLibraryFiles = d.files || [];
                libraryPlaybackQueue = rawLibraryFiles;
                libraryArtists = d.artists || libraryArtists;
                libraryAlbums = d.albums || libraryAlbums;
                saveLibraryCache();
                renderLibraryView();
                loadStats();
                loadSongEditor();
            }
        } catch (_) {}
    }, 1000);
    handleHash();


    initWebSocket();


    installEnhancedFeatures();
    document.getElementById("errorsButton")?.addEventListener("click",async()=>{const r=await fetch("api/errors");const d=await r.json();document.getElementById("errorsContent").innerHTML=(d.errors||[]).length?`<pre>${escapeHtml(JSON.stringify(d.errors,null,2))}</pre>`:'<div class="queue-empty">No errors recorded.</div>';document.getElementById("errors-modal").hidden=false;});
    document.getElementById("errorsClose")?.addEventListener("click",()=>document.getElementById("errors-modal").hidden=true);
    restorePlayerState();


    setInterval(
        () => pollTasks(),
        2000
    );
}




async function openMetadataEditor(file) {
    const modal=document.getElementById("metadata-modal"); if(!modal) return;
    document.getElementById("metadataId").value=file.id||"";
    document.getElementById("metadataTitle").value=file.title||"";
    document.getElementById("metadataArtist").value=file.artist||"";
    document.getElementById("metadataAlbum").value=file.album||"";
    const name=document.getElementById("metadataFileName"); if(name) name.textContent=file.name||file.path||"";
    modal.hidden=false;
}

function renderEnhancedQueue() {
    const box=document.getElementById("queueList"); if(!box) return;
    box.innerHTML="";
    if(!enhancedQueue.length) { box.innerHTML='<div class="queue-empty">Queue is empty</div>'; return; }
    enhancedQueue.forEach((t,i)=>{
        const row=document.createElement("div"); row.className=`queue-row ${i===enhancedQueueIndex?'current':''}`; row.draggable=true; row.dataset.index=String(i);
        row.innerHTML=`<span class="queue-drag">⋮⋮</span><img src="${escapeHtml(t.cover||'')}" alt=""><div class="queue-row-info"><strong>${escapeHtml(t.title||t.name||'Unknown')}</strong><span>${escapeHtml(t.artist||'Unknown Artist')}</span></div><button class="queue-next btn-refresh" title="Play next">Next</button><button class="queue-remove icon-btn" title="Remove">×</button>`;
        row.querySelector(".queue-next").onclick=()=>{ if(i===enhancedQueueIndex || i===enhancedQueueIndex+1) return; const [x]=enhancedQueue.splice(i,1); const target=Math.min(enhancedQueueIndex+1,enhancedQueue.length); enhancedQueue.splice(target,0,x); if(i<enhancedQueueIndex) enhancedQueueIndex--; libraryPlaybackQueue=[...enhancedQueue]; currentLibraryIndex=enhancedQueueIndex; saveEnhancedQueue(); renderEnhancedQueue(); };
        row.querySelector(".queue-remove").onclick=()=>{ if(i===enhancedQueueIndex){ showToast("Stop playback before removing the current track"); return; } enhancedQueue.splice(i,1); if(i<enhancedQueueIndex) enhancedQueueIndex--; else if(i===enhancedQueueIndex) enhancedQueueIndex=Math.min(enhancedQueueIndex,enhancedQueue.length-1); libraryPlaybackQueue=[...enhancedQueue]; currentLibraryIndex=enhancedQueueIndex; saveEnhancedQueue(); renderEnhancedQueue(); };
        row.addEventListener("dragstart",e=>e.dataTransfer.setData("text/plain",String(i)));
        row.addEventListener("dragover",e=>e.preventDefault());
        row.addEventListener("drop",e=>{e.preventDefault(); const from=Number(e.dataTransfer.getData("text/plain")); const to=Number(row.dataset.index); if(!Number.isInteger(from)||from===to)return; const [x]=enhancedQueue.splice(from,1); enhancedQueue.splice(to,0,x); if(enhancedQueueIndex===from) enhancedQueueIndex=to; else if(from<enhancedQueueIndex&&to>=enhancedQueueIndex) enhancedQueueIndex--; else if(from>enhancedQueueIndex&&to<=enhancedQueueIndex) enhancedQueueIndex++; libraryPlaybackQueue=[...enhancedQueue]; currentLibraryIndex=enhancedQueueIndex; saveEnhancedQueue(); renderEnhancedQueue(); });
        box.appendChild(row);
    });
}

function setEnhancedQueue(queue,index=0) { enhancedNaturalQueue=Array.isArray(queue)?[...queue]:[]; enhancedQueue=[...enhancedNaturalQueue]; enhancedQueueIndex=Math.max(0,Math.min(index,enhancedQueue.length-1)); libraryPlaybackQueue=[...enhancedQueue]; currentLibraryIndex=enhancedQueueIndex; currentPlayerSource="library"; saveEnhancedQueue(); renderEnhancedQueue(); }

function openQueueDrawer(){ const d=document.getElementById("queue-drawer"); if(d){d.hidden=false;renderEnhancedQueue();applyRepeatLabel();} }
function closeQueueDrawer(){const d=document.getElementById("queue-drawer"); if(d)d.hidden=true;}

async function saveQueueAsPlaylist(){ if(!enhancedQueue.length){showToast("Queue is empty");return;} const name=prompt("Playlist name", "My Queue"); if(!name)return; const r=await fetch("api/playlists",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name,song_ids:enhancedQueue.map(x=>x.id).filter(Boolean)})}); if(r.ok) showToast("✅ Playlist saved"); else showToast("❌ Could not save playlist"); }

async function renderLibraryCollections(mode){
    const list=document.getElementById("libraryList"); if(!list)return;
    list.innerHTML='<div class="downloads-empty"><div class="empty-title">Loading…</div></div>';
    let endpoint=mode==="recent"?"recent":mode==="most"?"most_played":null;
    if(!endpoint)return;
    const r=await fetch("api/library/recent-most",{cache:"no-store"}); const d=await r.json(); const rows=d[endpoint]||[]; list.innerHTML="";
    if(!rows.length){renderEmpty(list,"🎧",mode==="recent"?"Nothing recently played":"No play history yet","Play some tracks to build this list.");return;}
    rows.forEach((t, rank)=>{ const f={...t,name:t.title,stream:t.stream,cover:t.cover,play_count:Number(t.plays||0)}; const card=createTrackCard(f,rows); card.classList.add("collection-track"); card.dataset.rank=String(rank+1); list.appendChild(card); });
}

async function loadPlaylistsView(){
    const list=document.getElementById("libraryList"); if(!list)return; const r=await fetch("api/playlists",{cache:"no-store"}); const rows=await r.json(); list.innerHTML="";
    const head=document.createElement("div"); head.className="catalog-detail-header"; head.innerHTML='<div><h3>Playlists</h3><p>Create manual or smart playlists.</p></div><button class="btn-preview" id="newPlaylistBtn">＋ New playlist</button>'; list.appendChild(head);
    rows.forEach(p=>{const c=document.createElement("article");c.className="catalog-card";c.innerHTML=`<div><strong>${escapeHtml(p.name)}</strong><span>${p.kind==='smart'?'Smart':'Manual'} · ${p.song_count} tracks</span></div><div class="btn-group"><button class="btn-preview">▶ Play</button><button class="btn-danger">Delete</button></div>`;c.querySelector('.btn-preview').onclick=async()=>{const rr=await fetch(`api/playlists/${encodeURIComponent(p.id)}`);const full=await rr.json();setEnhancedQueue(full.tracks,0);playLibraryTrack(0);};c.querySelector('.btn-danger').onclick=async()=>{if(confirm(`Delete ${p.name}?`)){await fetch(`api/playlists/${encodeURIComponent(p.id)}`,{method:'DELETE'});loadPlaylistsView();}};list.appendChild(c);});
    document.getElementById("newPlaylistBtn").onclick=async()=>{const name=prompt("Playlist name","New Playlist");if(!name)return;const kind=confirm("Make this a smart playlist?\nOK = smart, Cancel = manual")?'smart':'manual';let rules={};if(kind==='smart'){const genre=prompt("Genre rule (optional)","");const artist=prompt("Artist rule (optional)","");if(genre)rules.genre=genre;if(artist)rules.artist=artist;}await fetch('api/playlists',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,kind,rules,song_ids:[]})});loadPlaylistsView();};
}

let songEditorTracks = [];

function renderSongEditorTracks(query = "") {
    const list = document.getElementById("songEditorList");
    if (!list) return;
    const q = String(query || "").trim().toLowerCase();
    const tracks = songEditorTracks.filter(track => {
        if (!q) return true;
        return `${track.title || ""} ${track.artist || ""} ${track.album || ""} ${track.name || ""}`.toLowerCase().includes(q);
    });
    list.innerHTML = "";
    if (!tracks.length) {
        list.innerHTML = `<div class="editor-empty"><div class="empty-title">${q ? "No matching tracks" : "All caught up"}</div><div>${q ? "Try another search." : "New downloads will appear here automatically."}</div></div>`;
        return;
    }
    tracks.forEach(track => {
        const card = document.createElement("article");
        card.className = "song-editor-card";
        card.dataset.songId = track.id;
        card.innerHTML = `<img class="song-editor-art" src="${escapeHtml(track.cover || "")}" alt="" loading="lazy"><div class="song-editor-info"><div class="song-editor-title">${escapeHtml(track.title || track.name || "Unknown Track")}</div><div class="song-editor-artist">${escapeHtml(track.artist || "Unknown Artist")} <span aria-hidden="true">•</span> ${escapeHtml(track.album || "Unknown Album")}</div><div class="song-editor-file">${escapeHtml(track.name || "")}</div></div><div class="song-editor-actions"><button class="btn-preview editor-edit" type="button"><i data-lucide="square-pen" aria-hidden="true"></i> Edit</button><button class="btn-secondary editor-skip" type="button">Skip</button></div>`;
        card.querySelector(".editor-edit").onclick = () => openMetadataEditor(track);
        card.querySelector(".editor-skip").onclick = async () => {
            const r = await fetch(`api/song-editor/${encodeURIComponent(track.id)}/skip`, {method:"POST"});
            if (!r.ok) return showToast("❌ Could not skip track");
            songEditorTracks = songEditorTracks.filter(x => x.id !== track.id);
            const input = document.getElementById("songEditorSearch");
            document.getElementById("songEditorCount")?.replaceChildren(String(songEditorTracks.length));
            document.getElementById("songEditorBadge")?.replaceChildren(String(songEditorTracks.length));
            renderSongEditorTracks(input?.value || "");
            showToast("Skipped");
        };
        card.querySelector("img")?.addEventListener("error", e => { e.currentTarget.removeAttribute("src"); e.currentTarget.style.visibility = "hidden"; }, {once:true});
        list.appendChild(card);
    });
    renderLocalIcons();
}

async function loadSongEditor(){
    const list = document.getElementById("songEditorList"); if (!list) return;
    if (!songEditorTracks.length) list.innerHTML = '<div class="editor-empty">Loading tracks waiting for review…</div>';
    try {
        const r = await fetch("api/song-editor", {cache:"no-store"});
        if (!r.ok) throw new Error("Could not load Songs Editor");
        const d = await r.json();
        songEditorTracks = Array.isArray(d.tracks) ? d.tracks : [];
        document.getElementById("songEditorCount")?.replaceChildren(String(songEditorTracks.length));
        document.getElementById("songEditorBadge")?.replaceChildren(String(songEditorTracks.length));
        const select = document.getElementById("songEditorImportSelect");
        if (select) {
            const existing = select.value;
            select.innerHTML = '<option value="">Choose a library track…</option>';
            const allTracks = Array.isArray(rawLibraryFiles) && rawLibraryFiles.length ? rawLibraryFiles : songEditorTracks;
            allTracks.forEach(t => {
                const o=document.createElement('option'); o.value=t.id||''; o.textContent=`${t.title||t.name||'Unknown Track'} — ${t.artist||'Unknown Artist'}`; select.appendChild(o);
            });
            if(existing && [...select.options].some(o=>o.value===existing)) select.value=existing;
        }
        renderSongEditorTracks(document.getElementById("songEditorSearch")?.value || "");
    } catch (err) {
        list.innerHTML = `<div class="editor-empty">${escapeHtml(err.message || "Could not load editor")}</div>`;
    }
}

function updateSongEditorCount(delta=0){const el=document.getElementById("songEditorCount"),badge=document.getElementById("songEditorBadge"); const cur=Math.max(0,(parseInt(el?.textContent||"0",10)||0)+delta); if(el)el.textContent=String(cur); if(badge)badge.textContent=String(cur);}

function installEnhancedFeatures(){
    loadEnhancedQueue(); loadEnhancedPositions(); applyRepeatLabel();
    document.getElementById("gp-queue-btn")?.addEventListener("click",openQueueDrawer); document.getElementById("queueClose")?.addEventListener("click",closeQueueDrawer); document.getElementById("queueClear")?.addEventListener("click",()=>{enhancedQueue=[];enhancedQueueIndex=-1;libraryPlaybackQueue=[];saveEnhancedQueue();renderEnhancedQueue();}); document.getElementById("queueSave")?.addEventListener("click",saveQueueAsPlaylist); document.getElementById("queueRepeat")?.addEventListener("click",cycleRepeatMode);
    document.getElementById("metadataClose")?.addEventListener("click",()=>document.getElementById("metadata-modal").hidden=true); document.getElementById("healthClose")?.addEventListener("click",()=>document.getElementById("health-modal").hidden=true);
    document.getElementById("metadataForm")?.addEventListener("submit",async e=>{e.preventDefault();const id=document.getElementById('metadataId').value;const body={id,title:document.getElementById('metadataTitle').value,artist:document.getElementById('metadataArtist').value,album:document.getElementById('metadataAlbum').value};const r=await fetch('api/library/metadata',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(r.ok){showToast('✅ Metadata saved and removed from editor');document.getElementById('metadata-modal').hidden=true;await refreshLibraryCache();renderLibraryView();loadSongEditor();}else{const d=await r.json().catch(()=>({}));showToast('❌ '+(d.detail||'Metadata update failed'));}});
    document.getElementById("libraryFullScanButton")?.addEventListener("click",async()=>{
        const btn=document.getElementById("libraryFullScanButton"); if(btn) btn.disabled=true;
        showToast('⏳ Full metadata rebuild…');
        try { const r=await fetch('api/library/scan/full',{method:'POST'}); if(!r.ok) throw new Error('Full scan failed'); showToast('✅ Full scan complete'); await refreshLibraryCache(); await loadStats(); renderLibraryView(); }
        catch(err){ showToast('❌ '+(err.message||'Full scan failed')); }
        finally { if(btn) btn.disabled=false; }
    });
    document.getElementById("songEditorSearch")?.addEventListener("input", e => {
        const clear = document.getElementById("songEditorSearchClear");
        if (clear) clear.hidden = !e.target.value;
        renderSongEditorTracks(e.target.value);
    });
    document.getElementById("songEditorSearchClear")?.addEventListener("click", () => {
        const input=document.getElementById("songEditorSearch");
        if(input){input.value="";input.focus();}
        document.getElementById("songEditorSearchClear")?.setAttribute("hidden", "");
        renderSongEditorTracks("");
    });
    document.getElementById("songEditorReset")?.addEventListener("click", async()=>{
        if(!confirm('Re-add all library tracks to Songs Editor? This marks every track as pending again.')) return;
        const btn=document.getElementById('songEditorReset'); if(btn) btn.disabled=true;
        try{ const r=await fetch('api/song-editor/reset',{method:'POST'}); const d=await r.json().catch(()=>({})); if(!r.ok) throw new Error(d.detail||'Reset failed'); await loadSongEditor(); showToast(`✅ ${d.count||0} tracks added to editor`); }
        catch(err){ showToast('❌ '+(err.message||'Reset failed')); }
        finally{ if(btn) btn.disabled=false; }
    });
    document.getElementById("songEditorImport")?.addEventListener("click", async()=>{
        const pick=document.getElementById('songEditorImportSelect');
        if(!pick){ showToast('❌ Import selector unavailable'); return; }
        const id=pick.value; if(!id){ showToast('Select a track to import'); return; }
        const r=await fetch(`api/song-editor/${encodeURIComponent(id)}/import`,{method:'POST'});
        if(r.ok){ const label=pick.options[pick.selectedIndex]?.text||'Track'; showToast(`✅ ${label} added to editor`); await loadSongEditor(); }
        else { const d=await r.json().catch(()=>({})); showToast('❌ '+(d.detail||'Could not import track')); }
    });
    document.getElementById("libraryHealthButton")?.addEventListener("click",async()=>{const r=await fetch('api/library/health');const d=await r.json();document.getElementById('healthContent').innerHTML=`<div class="health-summary"><strong>Unreadable: ${d.counts.unreadable}</strong><strong>Bad tags: ${d.counts.bad_tags}</strong><strong>Missing artwork: ${d.counts.missing_artwork}</strong><strong>Duplicate groups: ${d.counts.duplicates}</strong></div><pre>${escapeHtml(JSON.stringify(d,null,2))}</pre>`;document.getElementById('health-modal').hidden=false;});

    const originalPlayLibraryTrack=playLibraryTrack;
    playLibraryTrack=function(index){
        const q=enhancedQueue.length ? enhancedQueue : getLibraryQueue();
        if (enhancedQueue.length) {
            if(index<0 || index>=enhancedQueue.length) return;
            libraryPlaybackQueue=[...q];
            currentLibraryIndex=index;
            enhancedQueueIndex=index;
            saveEnhancedQueue();
            renderEnhancedQueue();
        }
        originalPlayLibraryTrack(index);
    };
    const originalPlayHomeTrack=window.playHomeTrack; if(typeof originalPlayHomeTrack==='function'){ window.playHomeTrack=originalPlayHomeTrack; }
    if(audio){
        audio.addEventListener('loadedmetadata',()=>{const id=currentSongId();const pos=enhancedSongPositions[id]?.position; if(id&&Number.isFinite(pos)&&pos>2&&pos<(audio.duration||Infinity)-2){try{audio.currentTime=pos;}catch(_){}}});
        audio.addEventListener('timeupdate',()=>{if(Math.floor(audio.currentTime)%5===0)persistCurrentPosition();});
        audio.addEventListener('play',()=>recordPlay(currentSongId()));
        audio.addEventListener('pause',persistCurrentPosition); window.addEventListener('beforeunload',persistCurrentPosition);
    }
    const originalRenderLibraryView=renderLibraryView; window._xrobOriginalRenderLibraryView=originalRenderLibraryView;
    renderLibraryView=function(){if(libraryView==='playlists')return loadPlaylistsView();if(libraryView==='recent')return renderLibraryCollections('recent');if(libraryView==='most')return renderLibraryCollections('most');return originalRenderLibraryView();};
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
window.loadSongEditor = loadSongEditor;
window.saveSettings = saveSettings;

window.toggleAudioStream =
    toggleAudioStream;
