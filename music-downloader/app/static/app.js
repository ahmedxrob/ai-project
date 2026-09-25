"use strict";

/* ============================================================
   GLOBAL STATE
   ============================================================ */

let socket = null;
let socketReconnectTimer = null;
let socketReconnectAttempt = 0;
let socketPingTimer = null;

let completedSet = new Set();

let playerShuffle = storageGet("xrob_music_shuffle") === "true";
let shuffleRestoreQueue = null;
let shuffleRestoreCurrentId = null;

let libraryLoadedFromCache = false;

const LIBRARY_CACHE_KEY =
    "xrob_music_library_cache";

const RECENT_CACHE_KEY =
    "xrob_music_recently_added_cache";

let activePreviewBtn = null;
let searchAbortController = null;
let homeRefreshInFlight = null;
let homeRefreshQueued = false;
let homeRefreshRequestId = 0;


let audio = null;
let player = null;
let playBtn = null;
let prevBtn = null;
let nextBtn = null;
let seek = null;
let seekFill = null;
let isSeeking = false;
let playerProgressFrame = null;
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
let replayGainNode = null;
let crossfadeAudio = null;
let crossfadeSourceNode = null;
let crossfadeGainNode = null;
let crossfadePrepared = null;
let crossfadeTimer = null;
let crossfadeActive = false;
let playerSettings = { replaygain_enabled: true, replaygain_mode: "track", replaygain_preamp_db: 0, replaygain_prevent_clipping: true, crossfade_seconds: 0, gapless_playback: true, keep_playing: true };

let savedPlayerState = {
    track: null,
    currentTime: 0,
    volume: 0.8,
    queueIndex: -1
};

let playerRepeatMode = storageGet("xrob_music_repeat") || "off";
let enhancedSongPositions = {};
let playSessionTrackId = null;
let playSessionRecorded = false;
const PLAY_COUNT_THRESHOLD_SECONDS = 60;
const ENHANCED_QUEUE_KEY = "xrob_music_up_next_queue";
const ENHANCED_REPEAT_KEY = "xrob_music_repeat";
let taskPollTimer = null;
let statsPollTimer = null;
/* ============================================================
   PLATFORM-SAFE STORAGE + API TRANSPORT
   ============================================================ */

function storageGet(key, fallback = null) {
    try {
        const value = window.localStorage.getItem(key);
        return value === null ? fallback : value;
    } catch (_) {
        return fallback;
    }
}

function storageSet(key, value) {
    try {
        window.localStorage.setItem(key, String(value));
        return true;
    } catch (_) {
        return false;
    }
}

function storageRemove(key) {
    try {
        window.localStorage.removeItem(key);
        return true;
    } catch (_) {
        return false;
    }
}

const nativeFetch = typeof window.fetch === "function" ? window.fetch.bind(window) : (...args) => Promise.reject(new Error("Fetch unavailable"));
const API_DEFAULT_TIMEOUT_MS = 15000;

function appBaseUrl() {
    try {
        const base = new URL(document.baseURI || window.location.href);
        if (!base.pathname.endsWith("/")) base.pathname += "/";
        return base;
    } catch (_) {
        return new URL("/", window.location.href);
    }
}

function apiUrl(path) {
    try {
        return new URL(String(path || ""), appBaseUrl()).href;
    } catch (_) {
        return String(path || "");
    }
}

function websocketUrl() {
    const base = appBaseUrl();
    base.protocol = base.protocol === "https:" ? "wss:" : "ws:";
    return new URL("ws", base).href;
}

async function apiFetch(input, options = {}) {
    const baseOptions = { ...options };
    const method = String(baseOptions.method || (typeof Request !== "undefined" && input instanceof Request ? input.method : "GET")).toUpperCase();
    const retryable = method === "GET" || method === "HEAD";
    const attempts = retryable ? 2 : 1;
    let lastError = null;

    for (let attempt = 0; attempt < attempts; attempt += 1) {
        let timeoutId = null;
        let controller = null;
        let detachCallerAbort = null;
        try {
            const requestOptions = { ...baseOptions };
            const callerSignal = requestOptions.signal;
            delete requestOptions.timeoutMs;
            if (typeof AbortController !== "undefined") {
                controller = new AbortController();
                if (callerSignal) {
                    if (callerSignal.aborted) {
                        controller.abort();
                    } else if (typeof callerSignal.addEventListener === "function") {
                        const onAbort = () => controller.abort();
                        callerSignal.addEventListener("abort", onAbort, { once: true });
                        detachCallerAbort = () => {
                            try { callerSignal.removeEventListener("abort", onAbort); } catch (_) {}
                        };
                    }
                }
                requestOptions.signal = controller.signal;
                const timeout = Number(baseOptions.timeoutMs || API_DEFAULT_TIMEOUT_MS);
                timeoutId = window.setTimeout(() => controller.abort(), Math.max(1000, timeout));
            }
            const target = typeof input === "string" ? apiUrl(input) : input;
            const response = await nativeFetch(target, {
                credentials: requestOptions.credentials || "same-origin",
                ...requestOptions,
            });
            if (timeoutId) window.clearTimeout(timeoutId);
            if (detachCallerAbort) detachCallerAbort();
            if (!response.ok) {
                emitAppEvent("api:error", { status: response.status, method, input: typeof input === "string" ? input : "request" });
                if (response.status === 401) {
                    appState.auth.authenticated = false;
                    emitAppEvent("auth:expired", { status: 401 });
                }
            }
            if (retryable && attempt + 1 < attempts && [408, 502, 503, 504].includes(response.status)) {
                await new Promise(resolve => window.setTimeout(resolve, 350 * (attempt + 1)));
                continue;
            }
            return response;
        } catch (error) {
            if (timeoutId) window.clearTimeout(timeoutId);
            if (detachCallerAbort) detachCallerAbort();
            lastError = error;
            if (error?.name !== "AbortError") reportAppError(error, {scope:"apiFetch", method, attempt: attempt + 1, input: typeof input === "string" ? input : "request"});
            if (!retryable || attempt + 1 >= attempts) throw error;
            // Only retry a request when the caller did not explicitly abort it.
            if (baseOptions.signal?.aborted) throw error;
            await new Promise(resolve => window.setTimeout(resolve, 350 * (attempt + 1)));
        }
    }
    throw lastError || new Error("Request failed");
}

async function apiFetchJson(input, options = {}, context = {}) {
    const response = await apiFetch(input, options);
    let data = null;
    try { data = await response.json(); } catch (_) { data = {}; }
    if (!response.ok) {
        const detail = typeof data?.detail === "string" ? data.detail : `HTTP ${response.status}`;
        const error = new Error(detail);
        reportAppError(error, {scope: context.scope || "api", action: context.action || "request", status: response.status, input: typeof input === "string" ? input : "request"});
        throw error;
    }
    return data;
}

const PLAYER_SYNC_CHANNEL = "xrob_music_player_sync_v2";
const PLAYER_SYNC_STATE_KEY = "xrob_music_player_sync_state";
const PLAYER_SYNC_COMMAND_KEY = "xrob_music_player_sync_command";
const PLAYER_OWNER_KEY = "xrob_music_player_owner";
const PLAYER_TAB_ID = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
const PLAYER_CLIENT_ID = (() => {
    const key = "xrob_music_player_client_id";
    try {
        const saved = storageGet(key);
        if (saved) return saved;
        const value = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
        storageSet(key, value);
        return value;
    } catch (_) {
        return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
    }
})();
const PLAYER_OWNER_STALE_MS = 15000;
const PLAYER_SERVER_STATE_STALE_MS = 12000;
const PLAYER_HEARTBEAT_MS = 2500;
const PLAYER_OWNER_CLAIM_DELAY_MS = 650;
const PLAYER_PROGRESS_BROADCAST_MS = 450;
const PLAYER_SERVER_SYNC_MS = 1500;
const PLAYER_HANDOFF_TIMEOUT_MS = 15000;
let playerOwnerClaimTimer = null;
let playerProgressBroadcastTimer = null;
let playerServerSyncTimer = null;
let playerHeartbeatTimer = null;
let playerHandoffInFlight = false;
let linkedRecoveryInFlight = false;
let playerHandoffStoppingRemote = false;
let serverPlayerStateLoaded = false;
let remoteDisplayTime = 0;
const DAILY_MIX_STATE_KEY = "xrob_music_daily_mix_state_v2";
const SLEEP_TIMER_KEY = "xrob_music_sleep_timer_v1";
const SLEEP_TIMER_PRESETS = [0, 15, 30, 60, 90];
let sleepTimerDeadline = Number(storageGet(SLEEP_TIMER_KEY) || 0) || 0;
let sleepTimerInterval = null;
let lyricsRequestId = 0;
let lyricsAnimationFrame = null;

/* ============================================================
   SHARED APPLICATION STATE + EVENT BUS
   ============================================================ */
const appState = {
    version: 1,
    auth: { authenticated: false, user: null },
    network: { online: navigator.onLine !== false, visibility: document.visibilityState || "visible", lastTransitionAt: Date.now(), lastReason: "startup" },
    lifecycle: { installed: false, featuresInstalled: false, lastLeaveAt: 0, lastRecoveryAt: 0 },
    search: { query: "", page: 1, loadingMore: false, hasMore: true, requestId: 0, pending: false, lastCompletedAt: 0, lastError: null },
    library: { ready: false, revision: 0, lastRefreshAt: 0, status: "unknown", files: [], artists: [], albums: [], view: "tracks", selectedArtistId: null, selectedAlbumId: null, playbackQueue: [], currentIndex: -1, recentTracksCache: [] },
    downloads: { filter: "active", history: [], tasks: [], lastSignature: "", lastUpdatedAt: 0 },
    devices: { items: [], lastUpdatedAt: 0 },
    player: { ownerId: null, source: null, playing: false, songId: null, currentTime: 0, duration: 0, lastEventAt: 0, queue: [], queueIndex: -1, homeQueue: [], homeQueueIndex: -1, remoteState: null },
    stats: { tracks: null, artists: null, albums: null, plays: null, totalBytes: null, folderSize: null },
    ui: { activePage: null, queueOpen: false, downloadsOpen: false },
    errors: { recent: [], last: null }
};
const appEvents = new EventTarget();
function emitAppEvent(type, detail = {}) {
    const payload = { type, at: Date.now(), ...detail };
    try { appEvents.dispatchEvent(new CustomEvent(type, { detail: payload })); } catch (_) {}
    return payload;
}

function syncLibraryUiState() {
    // Keep every Library surface in sync with the single source of truth in
    // appState.library. This function is intentionally idempotent because it is
    // called by refresh, cache restore, player queue sync and navigation.
    const library = appState.library || {};
    const files = Array.isArray(library.files) ? library.files : [];
    const artists = Array.isArray(library.artists) ? library.artists : [];
    const albums = Array.isArray(library.albums) ? library.albums : [];
    const count = files.length;
    const view = String(library.view || "tracks");

    // Navigation counters.
    ["sideLibCount", "mobLibCount", "statTracks"].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.textContent = String(count);
    });
    const statusTracks = document.getElementById("statusTracks");
    if (statusTracks && appState.stats?.tracks == null) {
        statusTracks.textContent = String(count);
    }
    const statusArtists = document.getElementById("statusArtists");
    if (statusArtists && appState.stats?.artists == null) {
        statusArtists.textContent = String(artists.length);
    }
    const statusAlbums = document.getElementById("statusAlbums");
    if (statusAlbums && appState.stats?.albums == null) {
        statusAlbums.textContent = String(albums.length);
    }

    // Library tabs use data-library-view in the canonical HTML. Do not read
    // dataset.appState (which is a string and was the source of the runtime
    // TypeError that stopped library rendering).
    document.querySelectorAll(".library-tab").forEach(tab => {
        const tabView = String(tab.dataset.libraryView || "");
        const active = tabView === view;
        tab.classList.toggle("active", active);
        tab.setAttribute("aria-selected", active ? "true" : "false");
    });

    const list = document.getElementById("libraryList");
    const dashboard = document.getElementById("libraryStatsDashboard");
    const isStatistics = view === "statistics";
    if (dashboard) dashboard.hidden = !isStatistics;
    if (list) list.hidden = isStatistics;

    const search = document.getElementById("libSearchQuery");
    const clear = document.getElementById("librarySearchClear");
    if (clear && search) clear.hidden = !String(search.value || "").trim();

    // Keep player/library queue metadata coherent without replacing an active
    // queue with the complete catalog.
    if (appState.player && Array.isArray(appState.player.queue)) {
        library.playbackQueue = appState.player.queue.length
            ? [...appState.player.queue]
            : files;
        library.currentIndex = Number.isInteger(appState.player.queueIndex)
            ? appState.player.queueIndex
            : -1;
    }
}
function onAppEvent(type, handler) { appEvents.addEventListener(type, handler); return () => appEvents.removeEventListener(type, handler); }
function setAppState(section, key, value, meta = {}) {
    if (!appState[section] || typeof appState[section] !== "object") appState[section] = {};
    appState[section][key] = value;
    emitAppEvent(`state:${section}:${key}`, { section, key, value, ...meta });
    return value;
}
function normalizeClientError(error) {
    if (error instanceof Error) return error;
    if (typeof error === "string") return new Error(error);
    try { return new Error(JSON.stringify(error)); } catch (_) { return new Error("Unknown application error"); }
}
function reportAppError(error, context = {}) {
    const err = normalizeClientError(error);
    const item = { message: String(err.message || "Application error"), name: String(err.name || "Error"), context: { ...context }, at: Date.now() };
    appState.errors.last = item;
    appState.errors.recent.push(item);
    if (appState.errors.recent.length > 50) appState.errors.recent.splice(0, appState.errors.recent.length - 50);
    emitAppEvent("app:error", item);
    try { console.error("Xrob Music", context.scope ? `[${context.scope}]` : "", err); } catch (_) {}
    return err;
}
window.xrobApp = { state: appState, events: appEvents, emit: emitAppEvent, on: onAppEvent, error: reportAppError };

let playerSyncChannel = null;
let playerSyncMode = "off";
let playerSyncDeviceIds = [];
let playerSyncGroupId = "";
let playerSyncHeartbeat = null;
let applyingRemotePlayerCommand = false;
let suppressLocalOwnershipUntil = 0;
let playerTakeoverPending = false;
let remotePlayerReceivedAt = 0;
let remotePlayerTimer = null;
let remoteRangeLastUpdatedAt = 0;
let visualizerFrame = null;
let visualizerData = null;
let playerSyncSequence = 0;
let lastRemoteOwnerId = null;
let lastRemoteSequence = -1;
const processedPlayerCommandIds = new Set();
let dailyMixTracks = [];
let dailyMixVariant = Number(storageGet("xrob_daily_mix_variant") || 0);
let dailyMixGeneration = String(storageGet("xrob_daily_mix_generation") || "");
let dailyMixLoadSequence = 0;



function getLocalDateKey(date = new Date()) {
    const y = date.getFullYear();
    const m = String(date.getMonth() + 1).padStart(2, "0");
    const d = String(date.getDate()).padStart(2, "0");
    return `${y}-${m}-${d}`;
}

function getPlayerOwner() {
    try {
        const raw = storageGet(PLAYER_OWNER_KEY);
        return raw ? JSON.parse(raw) : null;
    } catch (_) { return null; }
}

function ownerIsFresh(owner) {
    return Boolean(owner?.id && Number.isFinite(Number(owner.at)) && (Date.now() - Number(owner.at)) < PLAYER_OWNER_STALE_MS);
}

function setPlayerOwner(force = false) {
    const current = getPlayerOwner();
    if (!force && current?.id && current.id !== PLAYER_TAB_ID && ownerIsFresh(current)) {
        appState.player.ownerId = current.id;
        return false;
    }
    appState.player.ownerId = PLAYER_TAB_ID;
    appState.player.remoteState = null;
    lastRemoteOwnerId = null;
    lastRemoteSequence = -1;
    stopRemoteProgressTicker();
    try { storageSet(PLAYER_OWNER_KEY, JSON.stringify({ id: PLAYER_TAB_ID, at: Date.now() })); } catch (_) {}
    updateDeviceOwnershipUI();
    if (typeof sendPlayerHeartbeat === "function") {
        window.setTimeout(() => { sendPlayerHeartbeat().catch(() => {}); }, 0);
    }
    return true;
}

function heartbeatPlayerOwner() {
    const current = getPlayerOwner();
    if (!current?.id || current.id === PLAYER_TAB_ID) {
        if (appState.player.ownerId === PLAYER_TAB_ID) {
            try { storageSet(PLAYER_OWNER_KEY, JSON.stringify({ id: PLAYER_TAB_ID, at: Date.now() })); } catch (_) {}
        }
        return;
    }
    appState.player.ownerId = current.id;
}

function localDeviceLabel() {
    try {
        const platformRaw = navigator.userAgentData?.platform || navigator.platform || "Device";
        const platform = String(platformRaw)
            .replace(/^Win.*$/i, "Windows")
            .replace(/^Mac.*$/i, "Mac")
            .replace(/^Linux.*$/i, "Linux")
            .replace(/^Android.*$/i, "Android")
            .replace(/^iPhone.*$/i, "iPhone")
            .replace(/^iPad.*$/i, "iPad");
        const ua = String(navigator.userAgent || "");
        const browser = /Edg\//i.test(ua) ? "Edge" : /OPR\//i.test(ua) ? "Opera" : /Chrome\//i.test(ua) ? "Chrome" : /Firefox\//i.test(ua) ? "Firefox" : /Safari\//i.test(ua) && !/Chrome\//i.test(ua) ? "Safari" : "Browser";
        return `${platform} · ${browser}`;
    } catch (_) { return "This device"; }
}

function clearPlayerOwner() {
    const owner = getPlayerOwner();
    if (!owner || owner.id !== PLAYER_TAB_ID) return;
    try { storageRemove(PLAYER_OWNER_KEY); } catch (_) {}
    appState.player.ownerId = null;
    updateDeviceOwnershipUI();
}

function isRemotePlayerOwner() {
    const owner = getPlayerOwner();
    const localOwnerIsFresh = Boolean(owner?.id && owner.id !== PLAYER_TAB_ID && ownerIsFresh(owner));
    if (localOwnerIsFresh) {
        appState.player.ownerId = owner.id;
        return true;
    }
    if (appState.player.remoteState?._serverSynced && appState.player.remoteState.ownerId && appState.player.remoteState.ownerId !== PLAYER_TAB_ID) {
        const sameClient = appState.player.remoteState.clientId && appState.player.remoteState.clientId === PLAYER_CLIENT_ID;
        const age = Date.now() - remotePlayerReceivedAt;
        if (!sameClient && age < PLAYER_SERVER_STATE_STALE_MS) {
            appState.player.ownerId = appState.player.remoteState.ownerId;
            return true;
        }
    }
    if (owner?.id && owner.id !== PLAYER_TAB_ID && ownerIsFresh(owner)) {
        appState.player.ownerId = owner.id;
        return true;
    }
    return false;
}

function syncResourceUrl(value) {
    const raw = String(value || "").trim();
    if (!raw) return "";
    try {
        const parsed = new URL(raw, location.href);
        return parsed.origin === location.origin ? `${parsed.pathname}${parsed.search}${parsed.hash}` : parsed.href;
    } catch (_) { return raw; }
}

function normalizeSyncTrack(track) {
    if (!track || typeof track !== "object") return track;
    const out = { ...track };
    if (out.stream) out.stream = syncResourceUrl(out.stream);
    if (out.cover) out.cover = syncResourceUrl(out.cover);
    return out;
}

function normalizeSyncQueue(queue) {
    return normalizeQueue(Array.isArray(queue) ? queue.map(normalizeSyncTrack) : []);
}

function setSynchronizedHomeQueue(queue, index = -1) {
    const normalized = normalizeSyncQueue(queue);
    const safeIndex = normalized.length ? Math.max(0, Math.min(Number.isInteger(Number(index)) ? Number(index) : 0, normalized.length - 1)) : -1;
    appState.player.homeQueue = normalized;
    appState.player.homeQueueIndex = safeIndex;
    appState.player.queue = [...normalized];
    appState.player.queueIndex = safeIndex;
    appState.library.playbackQueue = [...normalized];
    appState.library.currentIndex = safeIndex;
    saveEnhancedQueue();
}

function buildPlayerSyncState(includeQueue = true) {
    if (!audio) return null;
    const state = {
        ownerId: PLAYER_TAB_ID,
        clientId: PLAYER_CLIENT_ID,
        src: syncResourceUrl(audio.src || ""),
        currentTime: Number(audio.currentTime || 0),
        duration: Number(audio.duration || 0),
        volume: Number.isFinite(Number(audio.volume)) ? Number(audio.volume) : 0.8,
        title: playerTitle?.textContent || "",
        artist: playerArtist?.textContent || "",
        art: syncResourceUrl(playerArt?.src || ""),
        songId: audio.dataset.xrobSongId || currentSongId() || "",
        source: appState.player.source || "",
        deviceName: localDeviceLabel(),
        queueIndex: appState.player.source === "library" ? appState.player.queueIndex : (Number.isInteger(appState.player.homeQueueIndex) ? appState.player.homeQueueIndex : -1),
        paused: Boolean(audio.paused),
        muted: Boolean(audio.muted),
        repeatMode: ["off", "track", "queue"].includes(playerRepeatMode) ? playerRepeatMode : "off",
        shuffle: Boolean(playerShuffle),
        syncMode: playerSyncMode,
        syncDeviceIds: [...playerSyncDeviceIds],
        syncGroupId: playerSyncGroupId,
        at: Date.now()
    };
    if (includeQueue) {
        state.queue = normalizeSyncQueue(appState.player.source === "library" ? appState.player.queue : (appState.player.homeQueue || []));
        state.dailyMix = {
            tracks: normalizeSyncQueue(Array.isArray(dailyMixTracks) ? dailyMixTracks : []),
            variant: Number(dailyMixVariant || 0),
            title: document.getElementById("dailyMixTitle")?.textContent || "Daily Mix",
            subtitle: document.getElementById("dailyMixSubtitle")?.textContent || "Personalized from your listening",
            scrollLeft: Number(document.getElementById("dailyMixTracks")?.scrollLeft || 0),
            date: getLocalDateKey(),
            generation: dailyMixGeneration,
        };
    }
    return state;
}

function broadcastPlayerState(force = false, unload = false) {
    if (!audio || (appState.player.ownerId && appState.player.ownerId !== PLAYER_TAB_ID) || isRemotePlayerOwner()) return;
    const state = buildPlayerSyncState(force);
    if (!state) return;
    state.seq = ++playerSyncSequence;
    if (force) state.force = true;
    if (playerTakeoverPending) { state.takeover = true; playerTakeoverPending = false; }
    if (force) {
        try { storageSet(PLAYER_SYNC_STATE_KEY, JSON.stringify(state)); } catch (_) {}
    }
    try { playerSyncChannel?.postMessage({ type: "state", state }); } catch (_) {}
    publishPlayerStateToServer(state, force, unload);
}

function publishPlayerStateToServer(state, force = false, unload = false) {
    if (!state || state.ownerId !== PLAYER_TAB_ID) return;
    const send = (nextState, full, useUnloadTransport = false) => {
        const payload = JSON.stringify({ state: nextState, full, takeover: Boolean(nextState?.takeover) });
        try {
            if (useUnloadTransport && typeof navigator !== "undefined" && typeof navigator.sendBeacon === "function" && typeof Blob !== "undefined") {
                const ok = navigator.sendBeacon(
                    apiUrl("api/player/state"),
                    new Blob([payload], { type: "application/json" })
                );
                if (ok) return;
            }
            apiFetch("api/player/state", {
                method: "POST",
                credentials: "same-origin",
                keepalive: useUnloadTransport,
                timeoutMs: useUnloadTransport ? 5000 : API_DEFAULT_TIMEOUT_MS,
                headers: { "Content-Type": "application/json" },
                body: payload
            }).then(async response => {
                let details = null;
                try { details = await response.clone().json(); } catch (_) {}
                const serverSeq = Number(details?.seq ?? details?.state?.seq);
                if (Number.isFinite(serverSeq)) playerSyncSequence = Math.max(playerSyncSequence, serverSeq);
                if (response.status === 409 && !useUnloadTransport) {
                    const detail = details?.detail;
                    const isOwnedByAnotherDevice = detail?.status === "owned" &&
                        detail?.ownerId && detail.ownerId !== PLAYER_TAB_ID;
                    // A stale/out-of-order state from this same tab must never pause
                    // playback. Only a real ownership conflict can stop the local player.
                    if (isOwnedByAnotherDevice) {
                        try { audio?.pause(); } catch (_) {}
                        try { clearPlayerOwner(); } catch (_) {}
                        loadServerPlayerState();
                    }
                }
            }).catch(error => reportAppError(error, {scope:"player", action:"publish-state"}));
        } catch (_) {}
    };
    if (force || unload) {
        if (playerServerSyncTimer) { window.clearTimeout(playerServerSyncTimer); playerServerSyncTimer = null; }
        send(state, true, unload);
        return;
    }
    if (playerServerSyncTimer) return;
    playerServerSyncTimer = window.setTimeout(() => {
        playerServerSyncTimer = null;
        if (!audio || isRemotePlayerOwner()) return;
        const fresh = buildPlayerSyncState(false);
        if (fresh) send(fresh, false);
    }, PLAYER_SERVER_SYNC_MS);
}

function applyAuthoritativeOwnedPlayerState(state, force = false) {
    if (!audio || !state || state.ownerId !== PLAYER_TAB_ID) return false;
    const incomingSeq = Number(state.seq || 0);
    const shouldReconcile = force || incomingSeq > playerSyncSequence;
    playerSyncSequence = Math.max(playerSyncSequence, incomingSeq);
    if (!shouldReconcile) return false;

    playerSyncMode = state.syncMode === "linked" ? "linked" : "off";
    playerSyncDeviceIds = Array.isArray(state.syncDeviceIds) ? state.syncDeviceIds.map(String) : [];
    playerSyncGroupId = String(state.syncGroupId || "");

    const previousApplying = applyingRemotePlayerCommand;
    applyingRemotePlayerCommand = true;
    try {
        if (state.repeatMode && ["off", "track", "queue"].includes(String(state.repeatMode))) {
            playerRepeatMode = String(state.repeatMode);
            storageSet(ENHANCED_REPEAT_KEY, playerRepeatMode);
            applyRepeatLabel();
        }
        if (state.shuffle !== undefined && Boolean(state.shuffle) !== playerShuffle) {
            setShuffle(Boolean(state.shuffle));
        }
        if (Array.isArray(state.queue) && state.queue.length) {
            const queue = normalizeSyncQueue(state.queue);
            if (state.source === "home") setSynchronizedHomeQueue(queue, state.queueIndex);
            else syncLibraryQueue(queue, state.queueIndex);
        }
        updatePlayerInfo(state.title, state.artist, state.art);
        if (player) { player.hidden = false; player.style.display = "grid"; }
        if (volume && Number.isFinite(Number(state.volume))) {
            volume.value = Math.max(0, Math.min(1, Number(state.volume)));
            audio.volume = Math.max(0, Math.min(1, Number(state.volume)));
        }
        audio.muted = Boolean(state.muted);
        appState.player.source = state.source === "home" ? "home" : (state.source ? "library" : appState.player.source);
        audio.dataset.xrobSongId = String(state.songId || "");

        const incomingSrc = syncResourceUrl(state.src || "");
        const currentSrc = syncResourceUrl(audio.src || "");
        const trackChanged = Boolean(incomingSrc) && incomingSrc !== currentSrc;
        const target = Number.isFinite(Number(state.currentTime)) ? Math.max(0, Number(state.currentTime)) : 0;

        if (trackChanged) {
            persistCurrentPosition(true);
            stopCrossfadePreload();
            const expectedSource = new URL(incomingSrc, location.href).href;
            const loadGeneration = ++audioLoadGeneration;
            audio.src = expectedSource;
            audio.load();
            const restore = () => {
                if (loadGeneration !== audioLoadGeneration || audio.src !== expectedSource) return;
                if (Number.isFinite(audio.duration) && audio.duration > 0) audio.currentTime = Math.min(target, Math.max(0, audio.duration - 0.25));
                if (state.paused) audio.pause();
                else { initAudioContext(); audio.play().catch(() => {}); }
                applyReplayGainToActiveAudio(activeQueueTrack());
            };
            audio.addEventListener("loadedmetadata", restore, { once: true });
        } else {
            const diff = Math.abs(Number(audio.currentTime || 0) - target);
            if (diff > 1.5 && (state.paused || force)) {
                try { audio.currentTime = target; } catch (_) {}
            }
            if (state.paused && !audio.paused) audio.pause();
            else if (!state.paused && audio.paused && audio.src) audio.play().catch(() => {});
        }
        updateProgress();
        updatePlayingState(!Boolean(state.paused));
        updateMediaSession();
        updateDeviceOwnershipUI();
        return true;
    } finally {
        applyingRemotePlayerCommand = previousApplying;
    }
}

async function loadServerPlayerState() {
    try {
        const response = await apiFetch("api/player/state", { cache: "no-store", credentials: "same-origin" });
        if (!response.ok) return false;
        const data = await response.json().catch(() => ({}));
        if (data?.state?.ownerId === PLAYER_TAB_ID) {
            const firstServerState = !serverPlayerStateLoaded;
            applyAuthoritativeOwnedPlayerState(data.state, firstServerState);
            serverPlayerStateLoaded = true;
            return true;
        }
        if (data?.state?.ownerId && data.state.ownerId !== PLAYER_TAB_ID) {
            const serverUpdatedAt = Number(data.updated_at || 0);
            const serverLastSeenAt = Number(data.last_seen_at || data.state?._serverLastSeenAt || serverUpdatedAt || 0);
            const ageMs = serverLastSeenAt ? (Date.now() - serverLastSeenAt * 1000) : Infinity;
            const persistent = Boolean(data.persistent || data.stale);
            if (!persistent && serverLastSeenAt && ageMs > PLAYER_SERVER_STATE_STALE_MS) return false;
            const enriched = { ...data.state, _serverUpdatedAt: serverUpdatedAt, _serverLastSeenAt: serverLastSeenAt, _serverActive: Boolean(data.active), _serverPersistent: persistent, _serverStale: Boolean(data.stale) || ageMs > PLAYER_SERVER_STATE_STALE_MS };
            serverPlayerStateLoaded = true;
            if (enriched.clientId && enriched.clientId === PLAYER_CLIENT_ID && enriched.src) {
                appState.player.remoteState = enriched;
                return takeoverRemotePlayer(true);
            }
            applyRemotePlayerState(enriched, true);
            updateDeviceOwnershipUI();
            return true;
        }
    } catch (_) {}
    return false;
}

function schedulePlayerStateBroadcast(force = false) {
    if (force) {
        if (playerProgressBroadcastTimer) {
            window.clearTimeout(playerProgressBroadcastTimer);
            playerProgressBroadcastTimer = null;
        }
        broadcastPlayerState(true);
        return;
    }
    if (playerProgressBroadcastTimer || typeof window === "undefined") return;
    playerProgressBroadcastTimer = window.setTimeout(() => {
        playerProgressBroadcastTimer = null;
        broadcastPlayerState(false);
    }, PLAYER_PROGRESS_BROADCAST_MS);
}

function claimLocalPlayerWhenOwnerIsGone() {
    const owner = getPlayerOwner();
    if (!owner?.id || owner.id === PLAYER_TAB_ID || !ownerIsFresh(owner)) {
        setPlayerOwner();
        return true;
    }
    if (appState.player.remoteState?.ownerId === owner.id) return false;
    setPlayerOwner();
    appState.player.remoteState = null;
    appState.player.ownerId = PLAYER_TAB_ID;
    return true;
}

function sendPlayerCommand(command, payload = {}) {
    let targetId = null;
    if (appState.player.remoteState?._serverSynced && appState.player.remoteState.ownerId && appState.player.remoteState.ownerId !== PLAYER_TAB_ID) {
        targetId = appState.player.remoteState.ownerId;
    } else {
        const owner = getPlayerOwner();
        if (owner?.id && owner.id !== PLAYER_TAB_ID && ownerIsFresh(owner)) targetId = owner.id;
    }
    if (!targetId) return false;
    const message = { type: "command", targetId, command, payload, id: `${PLAYER_TAB_ID}:${Date.now()}:${Math.random().toString(36).slice(2)}` };
    let sent = false;
    if (playerSyncChannel) {
        try { playerSyncChannel.postMessage(message); sent = true; } catch (_) {}
    }
    try { storageSet(PLAYER_SYNC_COMMAND_KEY, JSON.stringify(message)); sent = true; } catch (_) {}
    try {
        apiFetch("api/player/command", {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(message)
        }).catch(() => {});
        sent = true;
    } catch (_) {}
    return sent;
}

function updateRemoteProgress(animationFrame = false) {
    if (!appState.player.remoteState || !appState.player.remoteState.ownerId || appState.player.remoteState.ownerId === PLAYER_TAB_ID) return;
    const base = Number(appState.player.remoteState._serverCurrentTime ?? appState.player.remoteState.currentTime ?? 0);
    const duration = Number(appState.player.remoteState.duration || 0);
    const elapsed = appState.player.remoteState.paused ? 0 : Math.max(0, (Date.now() - remotePlayerReceivedAt) / 1000);
    let current = duration > 0 ? Math.min(duration, base + elapsed) : base + elapsed;
    if (!appState.player.remoteState.paused) current = Math.max(current, remoteDisplayTime);
    remoteDisplayTime = current;
    if (curTime) {
        const formatted = formatSeconds(current);
        if (curTime.textContent !== formatted) curTime.textContent = formatted;
    }
    if (durTime) {
        const formattedDuration = formatSeconds(duration);
        if (durTime.textContent !== formattedDuration) durTime.textContent = formattedDuration;
    }
    if (seek && duration > 0 && !isSeeking) {
        const percent = Math.max(0, Math.min(100, current / duration * 100));
        if (!animationFrame || (Date.now() - remoteRangeLastUpdatedAt) >= 200) {
            seek.value = percent.toFixed(3);
            remoteRangeLastUpdatedAt = Date.now();
        }
        renderSeekVisual(percent);
    }
}

function startRemoteProgressTicker() {
    if (remotePlayerTimer) return;
    const tick = () => {
        remotePlayerTimer = requestAnimationFrame(tick);
        const linkedMember = appState.player.remoteState?.syncMode === "linked"
            && Array.isArray(appState.player.remoteState?.syncDeviceIds)
            && appState.player.remoteState.syncDeviceIds.includes(String(PLAYER_TAB_ID))
            && appState.player.remoteState.ownerId !== PLAYER_TAB_ID;
        if (linkedMember) {
            const age = Date.now() - Number(remotePlayerReceivedAt || 0);
            if (age > PLAYER_SERVER_STATE_STALE_MS && !linkedRecoveryInFlight) {
                linkedRecoveryInFlight = true;
                takeoverRemotePlayer(true).finally(() => { linkedRecoveryInFlight = false; });
            }
            updateRemoteProgress(true);
            return;
        }
        if (appState.player.remoteState && !isRemotePlayerOwner()) {
            stopRemoteProgressTicker();
            return;
        }
        updateRemoteProgress(true);
    };
    remotePlayerTimer = requestAnimationFrame(tick);
}

function stopRemoteProgressTicker() {
    if (remotePlayerTimer) {
        cancelAnimationFrame(remotePlayerTimer);
        remotePlayerTimer = null;
    }
}


function updateRemotePlayerOptimistic(patch = {}) {
    if (!appState.player.remoteState) return;
    appState.player.remoteState = { ...appState.player.remoteState, ...patch };
    remotePlayerReceivedAt = Date.now();
    if (patch.currentTime !== undefined && Number.isFinite(Number(patch.currentTime))) {
        remoteDisplayTime = Math.max(0, Number(patch.currentTime));
    }
    if (patch.title !== undefined || patch.artist !== undefined || patch.art !== undefined) {
        updatePlayerInfo(appState.player.remoteState.title, appState.player.remoteState.artist, appState.player.remoteState.art);
    }
    if (patch.volume !== undefined && volume) volume.value = Math.max(0, Math.min(1, Number(appState.player.remoteState.volume || 0)));
    updateRemoteProgress();
    if (patch.paused !== undefined) updatePlayingState(!appState.player.remoteState.paused);
}

function applyRemoteDailyMixState(state, persist = true) {
    if (!state || !Array.isArray(state.tracks) || !state.tracks.length) return false;
    dailyMixTracks = state.tracks.map(track => ({ ...track }));
    dailyMixVariant = Number.isFinite(Number(state.variant)) ? Number(state.variant) : dailyMixVariant;
    dailyMixGeneration = String(state.generation || dailyMixGeneration || "");
    renderDailyMixCards(state.title || "Daily Mix", state.subtitle || "Personalized from your listening");
    const row = document.getElementById("dailyMixTracks");
    if (row && Number.isFinite(Number(state.scrollLeft))) {
        row.scrollLeft = Math.max(0, Math.min(Number(state.scrollLeft), Math.max(0, row.scrollWidth - row.clientWidth)));
    }
    if (persist) {
        try { storageSet(DAILY_MIX_STATE_KEY, JSON.stringify({ ...state, tracks: dailyMixTracks, generation: dailyMixGeneration, trackCount: dailyMixTracks.length, savedAt: Date.now() })); } catch (_) {}
    }
    return true;
}

function loadPersistedDailyMixState() {
    try {
        const raw = storageGet(DAILY_MIX_STATE_KEY);
        if (!raw) return false;
        const state = JSON.parse(raw);
        const configured = Math.max(5, Math.min(50, Number(storageGet("xrob_music_daily_mix_count") || 30)));
        if (!Array.isArray(state?.tracks) || !state.tracks.length) return false;
        if (state.date && state.date !== getLocalDateKey()) return false;
        const trackCount = Number(state.trackCount || state.tracks.length);
        if (trackCount < 1 || trackCount > configured) return false;
        if (!state.generation && trackCount !== configured) return false;
        return applyRemoteDailyMixState(state, false);
    } catch (_) {
        return false;
    }
}

function applyRemotePlayerState(state, fromServer = false) {
    if (!state || state.ownerId === PLAYER_TAB_ID) return;
    const serverUpdatedAt = Number(state._serverUpdatedAt || 0);
    const serverUpdatedAtMs = serverUpdatedAt > 0
        ? (serverUpdatedAt > 1e12 ? serverUpdatedAt : serverUpdatedAt * 1000)
        : 0;
    const serverLastSeenAtRaw = Number(state._serverLastSeenAt || 0);
    const serverLastSeenAtMs = serverLastSeenAtRaw > 1e12 ? serverLastSeenAtRaw : serverLastSeenAtRaw * 1000;
    const freshnessAnchor = serverLastSeenAtMs || serverUpdatedAtMs;
    const serverStale = Boolean(state._serverStale || (freshnessAnchor && (Date.now() - freshnessAnchor) > PLAYER_SERVER_STATE_STALE_MS));
    if (fromServer && serverStale && !state._serverPersistent) return;

    const owner = getPlayerOwner();
    if (!fromServer && owner?.id && owner.id !== state.ownerId && ownerIsFresh(owner)) return;
    const sequence = Number(state.seq ?? 0);
    if (lastRemoteOwnerId === state.ownerId && sequence && sequence <= lastRemoteSequence && !state.force) return;

    const previous = appState.player.remoteState;
    const ownerChanged = lastRemoteOwnerId !== state.ownerId;
    // Server state carries a timestamped playback clock. Use it as the anchor instead
    // of the browser message-arrival time, which can be delayed by network jitter.
    const serverClockTime = Number.isFinite(Number(state._serverCurrentTime))
        ? Math.max(0, Number(state._serverCurrentTime))
        : Math.max(0, Number(state.currentTime || 0));
    const serverClockAnchor = serverUpdatedAtMs || Date.now();
    // Never carry a previous owner's queue/Daily Mix into a newly claimed player.
    const merged = { ...(ownerChanged ? {} : (previous || {})), ...state, currentTime: serverClockTime, _serverSynced: Boolean(fromServer || state._serverSynced) };
    const nextSyncMode = merged.syncMode === "linked" ? "linked" : "off";
    const nextSyncIds = Array.isArray(merged.syncDeviceIds) ? merged.syncDeviceIds.map(String) : [];
    const wasSyncMember = playerSyncMode === "linked" && playerSyncDeviceIds.includes(String(PLAYER_TAB_ID));
    const isSyncMember = nextSyncMode === "linked" && nextSyncIds.includes(String(PLAYER_TAB_ID));
    playerSyncMode = nextSyncMode;
    playerSyncDeviceIds = nextSyncIds;
    playerSyncGroupId = String(merged.syncGroupId || "");
    if (wasSyncMember && !isSyncMember) { try { audio?.pause(); } catch (_) {} remoteDisplayTime = serverClockTime; }
    if (ownerChanged) {
        lastRemoteSequence = -1;
        remoteDisplayTime = serverClockTime;
    }
    const nextTime = serverClockTime;
    const previousTime = Number(previous?.currentTime || 0);
    const wasPlaying = Boolean(previous && !previous.paused);
    const isNormalPlaybackTick = wasPlaying && !merged.paused && Math.abs(nextTime - previousTime) <= 2.0;
    if (!isNormalPlaybackTick || merged.paused || ownerChanged) remoteDisplayTime = nextTime;
    else remoteDisplayTime = Math.max(remoteDisplayTime, nextTime);

    lastRemoteOwnerId = state.ownerId;
    lastRemoteSequence = sequence;
    appState.player.remoteState = merged;
    remotePlayerReceivedAt = serverClockAnchor;
    appState.player.ownerId = state.ownerId;
    if (merged.repeatMode && ["off", "track", "queue"].includes(String(merged.repeatMode))) {
        playerRepeatMode = String(merged.repeatMode);
        storageSet(ENHANCED_REPEAT_KEY, playerRepeatMode);
        applyRepeatLabel();
    }
    if (merged.shuffle !== undefined) {
        playerShuffle = Boolean(merged.shuffle);
        storageSet("xrob_music_shuffle", String(playerShuffle));
        updateShuffleButtons();
    }
    if (Array.isArray(merged.queue)) {
        const syncedQueue = normalizeSyncQueue(merged.queue);
        const previousApplyingQueue = applyingRemotePlayerCommand;
        applyingRemotePlayerCommand = true;
        try {
            if (merged.source === "home") setSynchronizedHomeQueue(syncedQueue, merged.queueIndex);
            else syncLibraryQueue(syncedQueue, merged.queueIndex);
        } finally {
            applyingRemotePlayerCommand = previousApplyingQueue;
        }
    }
    if (merged.dailyMix) applyRemoteDailyMixState(merged.dailyMix);
    updatePlayerInfo(merged.title, merged.artist, merged.art);
    if (player) { player.style.display = "grid"; updatePlayerBarMode(); }
    syncLibraryUiState();
    if (volume && Number.isFinite(Number(merged.volume))) volume.value = Math.max(0, Math.min(1, Number(merged.volume)));
    const shouldFollowLinkedPlayback = playerSyncMode === "linked" && playerSyncDeviceIds.includes(String(PLAYER_TAB_ID)) && Boolean(merged.src);
    if (shouldFollowLinkedPlayback && !applyingRemotePlayerCommand) syncRemoteAudioState(merged);
    updateRemoteProgress(false);
    updatePlayingState(!merged.paused);
    startRemoteProgressTicker();
    updateDeviceOwnershipUI();
}

async function syncRemoteAudioState(state) {
    if (!audio || !state?.src) return;
    const expectedSource = new URL(syncResourceUrl(state.src), location.href).href;
    const target = Math.max(0, Number(state._serverCurrentTime ?? state.currentTime ?? 0));
    const shouldPlay = !Boolean(state.paused);
    const generation = ++audioLoadGeneration;
    const previousApplying = applyingRemotePlayerCommand;
    applyingRemotePlayerCommand = true; suppressLocalOwnershipUntil = Date.now() + 5000;
    try {
        if (audio.src !== expectedSource) {
            audio.src = expectedSource; audio.load();
            await new Promise(resolve => { let done=false; const finish=()=>{ if(done) return; done=true; resolve(); }; audio.addEventListener("loadedmetadata",finish,{once:true}); audio.addEventListener("error",finish,{once:true}); window.setTimeout(finish,7000); });
        }
        if (generation !== audioLoadGeneration || audio.src !== expectedSource) return;
        if (Number.isFinite(audio.duration) && audio.duration > 0) { const safe=Math.min(target,Math.max(0,audio.duration-0.25)); if (Math.abs(Number(audio.currentTime||0)-safe)>0.75) audio.currentTime=safe; }
        else { try { audio.currentTime=target; } catch (_) {} }
        if (shouldPlay) { initAudioContext(); await audio.play().catch(()=>{}); } else audio.pause();
    } catch (_) {} finally { applyingRemotePlayerCommand=previousApplying; }
}

async function takeoverRemotePlayer(force = false) {
    const state = appState.player.remoteState;
    if (!audio || !state?.src || (!force && isRemotePlayerOwner()) || playerHandoffInFlight) return false;

    playerHandoffInFlight = true;
    const button = document.getElementById("gp-device-takeover");
    const oldButtonText = button?.textContent;
    if (button) { button.disabled = true; button.textContent = "Connecting…"; }

    try {
        // Ownership changes atomically on the server. This is the critical handoff
        // step: no local pause/claim/state race can reset the source to 0:00.
        const response = await apiFetch("api/player/handoff", {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            timeoutMs: PLAYER_HANDOFF_TIMEOUT_MS,
            body: JSON.stringify({
                newOwnerId: PLAYER_TAB_ID,
                clientId: PLAYER_CLIENT_ID,
                deviceName: localDeviceLabel(),
                expectedOwnerId: state.ownerId,
            })
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok || !result?.state?.src) {
            await loadServerPlayerState();
            return false;
        }

        const synced = { ...result.state };
        const serverSeq = Number(synced.seq);
        if (Number.isFinite(serverSeq)) playerSyncSequence = Math.max(playerSyncSequence, serverSeq);
        const target = Math.max(0, Number(synced._serverCurrentTime ?? synced.currentTime ?? 0));
        const duration = Math.max(0, Number(synced.duration || 0));
        const shouldPlay = !Boolean(synced.paused);
        const expectedSource = new URL(syncResourceUrl(synced.src), location.href).href;
        const loadGeneration = ++audioLoadGeneration;

        // Now that the server has granted ownership, this tab becomes the only
        // controller. Suppress the normal play-event owner claim during loading.
        setPlayerOwner(true);
        suppressLocalOwnershipUntil = Date.now() + 8000;
        appState.player.remoteState = null;
        stopRemoteProgressTicker();

        if (Array.isArray(synced.queue) && synced.queue.length) {
            const queue = normalizeSyncQueue(synced.queue);
            if (synced.source === "home") setSynchronizedHomeQueue(queue, synced.queueIndex);
            else syncLibraryQueue(queue, Number(synced.queueIndex ?? 0));
        }
        appState.player.source = synced.source === "home" ? "home" : (synced.source ? "library" : appState.player.source);
        updatePlayerInfo(synced.title, synced.artist, synced.art);
        audio.dataset.xrobSongId = String(synced.songId || "");
        activePreviewBtn = null;
        if (Number.isFinite(Number(synced.volume))) {
            audio.volume = Math.max(0, Math.min(1, Number(synced.volume)));
            if (volume) volume.value = audio.volume;
        }
        audio.muted = Boolean(synced.muted);
        playerTakeoverPending = true;
        if (player) player.style.display = "grid";
        stopCrossfadePreload();

        const loaded = await new Promise(resolve => {
            let settled = false;
            const finish = ok => {
                if (settled) return;
                settled = true;
                resolve(ok);
            };
            const onMetadata = () => finish(true);
            const onError = () => finish(false);
            audio.addEventListener("loadedmetadata", onMetadata, { once: true });
            audio.addEventListener("error", onError, { once: true });
            window.setTimeout(() => finish(false), PLAYER_HANDOFF_TIMEOUT_MS);
            audio.src = expectedSource;
            audio.load();
            if (audio.readyState >= 1) queueMicrotask(() => finish(true));
        });
        if (!loaded || loadGeneration !== audioLoadGeneration || audio.src !== expectedSource) {
            await loadServerPlayerState();
            return false;
        }

        const safeTarget = duration > 0
            ? Math.min(target, Math.max(0, Number(audio.duration || duration) - 0.25))
            : target;
        try { audio.currentTime = safeTarget; } catch (_) {}
        applyReplayGainToActiveAudio(activeQueueTrack());
        updateProgress();
        updateMediaSession();
        updateDeviceOwnershipUI();
        if (shouldPlay) {
            initAudioContext();
            try {
                await audio.play();
            } catch (error) {
                // Browser autoplay policy may require one explicit tap on the new device.
                updatePlayingState(false);
                showToast("▶ Tap Play to continue from the current position");
                console.debug("Playback handoff requires user gesture:", error);
            }
        } else {
            audio.pause();
            updatePlayingState(false);
        }
        playerTakeoverPending = true;
        schedulePlayerStateBroadcast(true);
        return true;
    } catch (error) {
        console.warn("Playback handoff failed:", error);
        try { await loadServerPlayerState(); } catch (_) {}
        return false;
    } finally {
        playerHandoffInFlight = false;
        if (button) {
            button.disabled = false;
            button.textContent = oldButtonText || "Take over";
        }
        updateDeviceOwnershipUI();
    }
}

function applyRemoteHandoff(message) {
    const fromOwnerId = String(message?.fromOwnerId || "");
    const toOwnerId = String(message?.toOwnerId || "");
    const state = message?.state;
    if (!fromOwnerId || !toOwnerId || !state) return;

    if (fromOwnerId === PLAYER_TAB_ID && toOwnerId !== PLAYER_TAB_ID) {
        // Server already granted another device ownership. Stop locally without
        // publishing an obsolete paused/0:00 snapshot back over the handoff.
        const previousApplying = applyingRemotePlayerCommand;
        applyingRemotePlayerCommand = true;
        try {
            playerHandoffStoppingRemote = true;
            audio?.pause();
            clearPlayerOwner();
        } catch (_) {}
        finally {
            playerHandoffStoppingRemote = false;
            applyingRemotePlayerCommand = previousApplying;
        }
        appState.player.remoteState = { ...state, _serverSynced: true };
        remotePlayerReceivedAt = Number(state._serverUpdatedAt || Date.now());
        applyRemotePlayerState(appState.player.remoteState, true);
        return;
    }

    if (toOwnerId !== PLAYER_TAB_ID) {
        applyRemotePlayerState(state, true);
    }
}

function applyRemoteCommand(message) {
    if (!audio || message?.targetId !== PLAYER_TAB_ID) return;
    if (message.id) {
        if (processedPlayerCommandIds.has(message.id)) return;
        processedPlayerCommandIds.add(message.id);
        if (processedPlayerCommandIds.size > 200) processedPlayerCommandIds.delete(processedPlayerCommandIds.values().next().value);
    }
    const p = message.payload || {};
    applyingRemotePlayerCommand = true;
    suppressLocalOwnershipUntil = Date.now() + 5000;
    try {
        if (message.command === "play") audio.play().catch(() => {});
        else if (message.command === "pause") audio.pause();
        else if (message.command === "stop") { audio.pause(); try { audio.currentTime = 0; } catch (_) {} }
        else if (message.command === "seek" && Number.isFinite(Number(p.time))) audio.currentTime = Math.max(0, Number(p.time));
        else if (message.command === "next") playNextTrack();
        else if (message.command === "previous") playPreviousTrack();
        else if (message.command === "shuffle") setShuffle(Boolean(p.enabled));
        else if (message.command === "repeat") { const mode = String(p.mode || "off"); if (["off", "track", "queue"].includes(mode)) { playerRepeatMode = mode; storageSet(ENHANCED_REPEAT_KEY, playerRepeatMode); applyRepeatLabel(); } }
        else if (message.command === "volume" && Number.isFinite(Number(p.volume))) { audio.volume = Math.max(0, Math.min(1, Number(p.volume))); if (volume) volume.value = audio.volume; }
        else if (message.command === "queue") {
            if (Array.isArray(p.queue)) {
                const nextQueue = normalizeSyncQueue(p.queue);
                if (p.source === "home") setSynchronizedHomeQueue(nextQueue, Number(p.queueIndex ?? 0));
                else syncLibraryQueue(nextQueue, Number(p.queueIndex ?? 0));
            }
        } else if (message.command === "load-play") {
            if (Array.isArray(p.queue) && p.queue.length) {
                if (p.source === "home") setSynchronizedHomeQueue(p.queue, Number(p.queueIndex ?? 0));
                else syncLibraryQueue(normalizeSyncQueue(p.queue), Number(p.queueIndex ?? 0));
            }
            toggleAudioStream(document.createElement("button"), p.src, p.source || "library", p.title, p.artist, p.art, p.songId || null, true);
        } else if (message.command === "mirror-play") {
            const src = p.src ? syncResourceUrl(p.src) : "";
            if (src) {
                appState.player.source = p.source === "home" ? "home" : (p.source || appState.player.source || "library");
                updatePlayerInfo(p.title, p.artist, syncResourceUrl(p.art || ""));
                if (player) player.style.display = "grid";
                const absolute = new URL(src, location.href).href;
                const requestedTime = Math.max(0, Number(p.currentTime || 0));
                const applyPosition = () => {
                    if (audio.readyState >= 1 && Number.isFinite(audio.duration) && audio.duration > 0) audio.currentTime = Math.min(requestedTime, Math.max(0, audio.duration - 0.25));
                    else { try { audio.currentTime = requestedTime; } catch (_) {} }
                    initAudioContext();
                    audio.play().catch(() => showToast("▶ Tap Play to continue mirror playback"));
                    updatePlayingState(true);
                };
                if (audio.src !== absolute) {
                    audio.src = absolute;
                    audio.load();
                    audio.addEventListener("loadedmetadata", applyPosition, {once:true});
                } else applyPosition();
            }
        }
    } finally {
        applyingRemotePlayerCommand = false;
    }
    if (!["load-play","mirror-play"].includes(message.command)) schedulePlayerStateBroadcast(true);
}

async function sendPlayerHeartbeat() {
    if (!audio || appState.player.ownerId !== PLAYER_TAB_ID || isRemotePlayerOwner()) return;
    try {
        const response = await apiFetch("api/player/heartbeat", {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            timeoutMs: 7000,
            body: JSON.stringify({ ownerId: PLAYER_TAB_ID, clientId: PLAYER_CLIENT_ID })
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) {
            if (response.status === 409) {
                try { audio.pause(); } catch (_) {}
                clearPlayerOwner();
                await loadServerPlayerState();
            }
            return;
        }
        const serverState = result?.state;
        const serverSeq = Number(serverState?.seq);
        if (Number.isFinite(serverSeq)) playerSyncSequence = Math.max(playerSyncSequence, serverSeq);
    } catch (_) {}
}

async function initPlayerSync() {
    if (playerSyncChannel || typeof window === "undefined") return;
    try { playerSyncChannel = typeof BroadcastChannel !== "undefined" ? new BroadcastChannel(PLAYER_SYNC_CHANNEL) : null; } catch (_) { playerSyncChannel = null; }

    // BroadcastChannel is unavailable in some embedded/webview environments.
    // The storage event gives same-origin desktop/mobile tabs a reliable fallback.
    if (!window.__xrobPlayerStorageFallbackInstalled) {
        window.__xrobPlayerStorageFallbackInstalled = true;
        window.addEventListener("storage", event => {
            if (event.storageArea !== localStorage) return;
            if (event.key === PLAYER_SYNC_STATE_KEY && event.newValue) {
                try { const state = JSON.parse(event.newValue); if (state?.ownerId && state.ownerId !== PLAYER_TAB_ID) applyRemotePlayerState(state); } catch (_) {}
            } else if (event.key === PLAYER_SYNC_COMMAND_KEY && event.newValue) {
                try { const message = JSON.parse(event.newValue); if (!message.targetId || message.targetId === PLAYER_TAB_ID) applyRemoteCommand(message); } catch (_) {}
            } else if (event.key === PLAYER_OWNER_KEY && event.newValue) {
                try { const owner = JSON.parse(event.newValue); if (owner?.id) appState.player.ownerId = owner.id; } catch (_) {}
                heartbeatPlayerOwner();
                loadServerPlayerState();
            }
        }, { passive: true });
    }
    playerSyncChannel?.addEventListener("message", (event) => {
        const msg = event.data || {};
        if (msg.type === "request-state") {
            const owner = getPlayerOwner();
            if (owner?.id === PLAYER_TAB_ID) schedulePlayerStateBroadcast(true);
        } else if (msg.type === "state") {
            if (msg.state?.ownerId !== PLAYER_TAB_ID) applyRemotePlayerState(msg.state);
        } else if (msg.type === "command") {
            applyRemoteCommand(msg);
        } else if (msg.type === "player_handoff") {
            applyRemoteHandoff(msg);
        } else if (msg.type === "owner-closing" && msg.ownerId === appState.player.ownerId) {
            appState.player.remoteState = null;
            appState.player.ownerId = null;
            stopRemoteProgressTicker();
            updatePlayingState(false);
        }
    });
    const owner = getPlayerOwner();
    if (ownerIsFresh(owner)) appState.player.ownerId = owner.id;
    await loadServerPlayerState();
    const raw = storageGet(PLAYER_SYNC_STATE_KEY);
    // When another tab owns the player, trust a live BroadcastChannel response
    // instead of blindly restoring an old state snapshot from localStorage.
    if (!serverPlayerStateLoaded && !(ownerIsFresh(owner) && owner.id !== PLAYER_TAB_ID) && raw) {
        try { applyRemotePlayerState(JSON.parse(raw)); } catch (_) {}
    }
    try { playerSyncChannel?.postMessage({ type: "request-state", requesterId: PLAYER_TAB_ID }); } catch (_) {}
    if (ownerIsFresh(owner) && owner.id !== PLAYER_TAB_ID && !appState.player.remoteState) {
        playerOwnerClaimTimer = window.setTimeout(() => {
            playerOwnerClaimTimer = null;
            if (!appState.player.remoteState) claimLocalPlayerWhenOwnerIsGone();
        }, PLAYER_OWNER_CLAIM_DELAY_MS);
    }
    playerSyncHeartbeat = window.setInterval(() => {
        heartbeatPlayerOwner();
        sendPlayerHeartbeat();
    }, PLAYER_HEARTBEAT_MS);
}


function currentSongId() {
    const useEnhanced = appState.player.source === "library" && appState.player.queue.length;
    const q = useEnhanced ? appState.player.queue : (appState.player.source === "library" ? getLibraryQueue() : (appState.player.homeQueue || []));
    const idx = useEnhanced ? appState.player.queueIndex : (appState.player.source === "library" ? appState.library.currentIndex : appState.player.homeQueueIndex);
    const item = Number.isInteger(idx) && idx >= 0 ? q[idx] : null;
    return item?.id || null;
}

function saveEnhancedQueue() {
    try { storageSet(ENHANCED_QUEUE_KEY, JSON.stringify({queue: appState.player.queue, index: appState.player.queueIndex})); } catch (_) {}
}

async function resolveEnhancedQueueIds() {
    if (!Array.isArray(appState.player.queue) || !appState.player.queue.length) return;
    const ids = appState.player.queue.map(item => item?.id).filter(Boolean);
    if (!ids.length) return;
    try {
        const r = await apiFetch("api/player/resolve-queue", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ids}), timeoutMs:5000});
        if (!r.ok) return;
        const data = await r.json();
        const mapping = data?.mapping || {};
        let changed = false;
        appState.player.queue = appState.player.queue.map(item => {
            const oldId = String(item?.id || "");
            const newId = mapping[oldId];
            if (newId && newId !== oldId) { changed = true; return {...item, id:newId}; }
            return item;
        });
        if (changed) { saveEnhancedQueue(); appState.library.playbackQueue=[...appState.player.queue]; syncLibraryUiState(); appState.player.queue=[...appState.player.queue]; appState.player.queueIndex=appState.player.queueIndex; }
    } catch (_) {}
}

function loadEnhancedQueue() {
    try {
        const v = JSON.parse(storageGet(ENHANCED_QUEUE_KEY) || "null");
        if (Array.isArray(v?.queue) && v.queue.length) {
            appState.player.queue = [...v.queue];
            appState.player.queueIndex = Math.max(
                0,
                Math.min(Number.isInteger(v.index) ? v.index : 0, appState.player.queue.length - 1)
            );
            appState.library.playbackQueue = [...appState.player.queue];
            appState.library.currentIndex = appState.player.queueIndex;
            syncLibraryUiState();
            appState.player.queue=[...appState.player.queue];
            appState.player.queueIndex=appState.player.queueIndex;
            resolveEnhancedQueueIds().catch(() => {});
        } else {
            appState.player.queue = [];
            appState.player.queueIndex = -1;
            appState.library.playbackQueue = [];
            appState.library.currentIndex = -1;
        }
    } catch (_) {
        appState.player.queue = [];
        appState.player.queueIndex = -1;
        appState.library.playbackQueue = [];
        appState.library.currentIndex = -1;
    }
}

async function loadEnhancedPositions() {
    try { const r=await apiFetch("api/player/positions",{cache:"no-store"}); if(r.ok) enhancedSongPositions=await r.json(); } catch (_) {}
}

let lastPositionPersistId = "";
let lastPositionPersistSecond = -1;
function persistCurrentPosition(force = false, unload = false) {
    const id = audio?.dataset?.xrobSongId || currentSongId();
    if(!id || !audio) return;
    const position=Number(audio.currentTime||0), duration=Number(audio.duration||0);
    const second = Math.floor(Math.max(0, position));
    if (!force && id === lastPositionPersistId && Math.abs(second - lastPositionPersistSecond) < 5) return;
    lastPositionPersistId = id;
    lastPositionPersistSecond = second;
    enhancedSongPositions[id]={position,duration,updated_at:Date.now()/1000};
    const payload = JSON.stringify({song_id:id,position,duration});
    if (unload && typeof navigator !== "undefined" && typeof navigator.sendBeacon === "function" && typeof Blob !== "undefined") {
        try {
            if (navigator.sendBeacon(apiUrl("api/player/position"), new Blob([payload], {type:"application/json"}))) return;
        } catch (_) {}
    }
    apiFetch("api/player/position",{method:"POST",headers:{"Content-Type":"application/json"},credentials:"same-origin",keepalive:unload,timeoutMs:unload ? 5000 : API_DEFAULT_TIMEOUT_MS,body:payload}).catch(()=>{});
}

function beginPlaySession(id) {
    if (!id) return;
    playSessionTrackId = id;
    playSessionRecorded = false;
}

function resetPlaySession() {
    playSessionTrackId = null;
    playSessionRecorded = false;
}

function playCountThreshold() {
    const duration = Number(audio?.duration || 0);
    if (Number.isFinite(duration) && duration > 0 && duration < PLAY_COUNT_THRESHOLD_SECONDS) {
        // Short clips can still earn a play after roughly half has been heard.
        return Math.max(5, duration * 0.5);
    }
    return PLAY_COUNT_THRESHOLD_SECONDS;
}

function recordPlay(id) {
    if (!id || playSessionRecorded || playSessionTrackId !== id) return;
    const position = Number(audio?.currentTime || 0);
    if (position < playCountThreshold()) return;

    playSessionRecorded = true;
    try {
        apiFetch("api/player/history", {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                song_id: id,
                duration: Number(audio?.duration || 0),
                position
            })
        })
        .then(response => response.ok ? response.json() : null)
        .then(data => {
            if (data?.all_play_count !== undefined) applyLivePlayCount(data.all_play_count);
        })
        .catch(() => {});
    } catch (_) {}
}

function applyRepeatLabel() { const b=document.getElementById("queueRepeat"); if(b) b.textContent=`Repeat: ${playerRepeatMode === "track" ? "Track" : playerRepeatMode === "queue" ? "Queue" : "Off"}`; }
function cycleRepeatMode() {
    const next = playerRepeatMode === "off" ? "track" : playerRepeatMode === "track" ? "queue" : "off";
    if (isRemotePlayerOwner()) {
        if (sendPlayerCommand("repeat", { mode: next })) {
            playerRepeatMode = next; storageSet(ENHANCED_REPEAT_KEY, playerRepeatMode); applyRepeatLabel(); updateRemotePlayerOptimistic({ repeatMode: next });
        }
        return;
    }
    playerRepeatMode = next;
    storageSet(ENHANCED_REPEAT_KEY,playerRepeatMode);
    applyRepeatLabel();
    schedulePlayerStateBroadcast(true);
}


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

    seekFill = document.getElementById("gp-seek-fill");

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

function trackKey(track) {
    if (!track) return '';
    return String(track.id || track.name || track.stream || `${track.title || ''}\0${track.artist || ''}`);
}

function normalizeQueue(queue) {
    const seen = new Set();
    const out = [];
    (Array.isArray(queue) ? queue : []).forEach(track => {
        const key = trackKey(track);
        if (!key || seen.has(key)) return;
        seen.add(key);
        out.push(track);
    });
    return out;
}

function getLibraryQueue() {
    return Array.isArray(appState.player.queue) && appState.player.queue.length
        ? appState.player.queue
        : (Array.isArray(appState.library.playbackQueue) && appState.library.playbackQueue.length ? appState.library.playbackQueue : (Array.isArray(appState.library.files) ? appState.library.files : []));
}

function syncLibraryQueue(queue, index) {
    const normalized = normalizeQueue(queue);
    appState.player.queue = normalized;
    appState.player.queueIndex = normalized.length
        ? Math.max(0, Math.min(Number.isInteger(Number(index)) ? Number(index) : 0, normalized.length - 1))
        : -1;
    appState.library.playbackQueue = [...normalized];
    appState.library.currentIndex = appState.player.queueIndex;
    if (normalized.length) saveEnhancedQueue();
    else storageRemove(ENHANCED_QUEUE_KEY);
    if (!applyingRemotePlayerCommand) {
        if (playerSyncMode === "linked" && playerSyncDeviceIds.length > 1 && !isRemotePlayerOwner()) {
            const sentToOwner = sendPlayerCommand("queue", { queue: normalized, queueIndex: appState.player.queueIndex, source: appState.player.source });
            if (!sentToOwner) schedulePlayerStateBroadcast(true);
        } else if (!isRemotePlayerOwner()) {
            schedulePlayerStateBroadcast(true);
        }
    }
}

function reconcileEnhancedQueue() {
    if (!appState.player.queue.length) return;
    const currentId = trackKey(appState.player.queue[appState.player.queueIndex]);
    const valid = new Set(appState.library.files.map(trackKey));
    const filtered = appState.player.queue.filter(track => valid.has(trackKey(track)));
    if (!filtered.length) {
        syncLibraryQueue([], -1);
        return;
    }
    const nextIndex = filtered.findIndex(track => trackKey(track) === currentId);
    syncLibraryQueue(filtered, nextIndex >= 0 ? nextIndex : Math.min(appState.player.queueIndex, filtered.length - 1));
}

function addTrackToQueue(track, playNext = false) {
    if (!track) return false;
    const key = trackKey(track);
    if (!key) return false;
    const queue = getLibraryQueue();
    const currentIndex = appState.player.source === 'library' ? getQueueIndex() : -1;
    const already = queue.findIndex(item => trackKey(item) === key);
    if (already >= 0) {
        if (playNext && currentIndex >= 0 && already !== currentIndex + 1) {
            const q = [...queue];
            const [item] = q.splice(already, 1);
            const adjustedCurrent = q.findIndex(item2 => trackKey(item2) === trackKey(queue[currentIndex]));
            q.splice(Math.max(0, adjustedCurrent + 1), 0, item);
            syncLibraryQueue(q, q.findIndex(item2 => trackKey(item2) === trackKey(queue[currentIndex])));
            renderEnhancedQueue();
            showToast('▶ Next in queue');
            return true;
        }
        showToast('Already in queue');
        return false;
    }
    let q = [...queue];
    let newIndex = currentIndex;
    if (currentIndex >= 0) {
        const insertAt = playNext ? currentIndex + 1 : q.length;
        q.splice(insertAt, 0, track);
        newIndex = q.findIndex(item => trackKey(item) === trackKey(queue[currentIndex]));
    } else if (q.length) {
        // No active library track: preserve an existing persisted queue and append.
        q.push(track);
        newIndex = -1;
    } else {
        q = [track];
        newIndex = 0;
    }
    syncLibraryQueue(q, newIndex);
    renderEnhancedQueue();
    showToast(playNext ? '▶ Added to Up Next' : '＋ Added to queue');
    return true;
}

function getQueueIndex() { return appState.player.queueIndex; }

function shuffledCopy(items) {
    const copy = [...items];
    for (let i = copy.length - 1; i > 0; i -= 1) {
        const j = Math.floor(Math.random() * (i + 1));
        [copy[i], copy[j]] = [copy[j], copy[i]];
    }
    return copy;
}

function getActiveLibraryQueueState() {
    const queue = getLibraryQueue();
    const index = Number.isInteger(appState.library.currentIndex) ? appState.library.currentIndex : -1;
    return { queue, index };
}

function shuffleQueueAfterCurrent(queue, index) {
    const items = normalizeQueue(queue);
    if (!items.length) return { queue: [], index: -1 };
    const currentIndex = Math.max(0, Math.min(Number.isInteger(index) ? index : 0, items.length - 1));
    const current = items[currentIndex];
    // Preserve history/previous tracks. Only Up Next is randomized, matching music-player behavior.
    return {
        queue: [...items.slice(0, currentIndex), current, ...shuffledCopy(items.slice(currentIndex + 1))],
        index: currentIndex
    };
}

function updateShuffleButtons() {
    const buttons = [document.getElementById("gp-shuffle-btn"), document.getElementById("libraryShuffleButton")];
    buttons.forEach(button => {
        button?.classList.toggle("active", playerShuffle);
        button?.setAttribute("aria-pressed", String(playerShuffle));
    });
}

function setShuffle(enabled) {
    const nextValue = Boolean(enabled);
    if (nextValue === playerShuffle) { updateShuffleButtons(); return; }
    if (isRemotePlayerOwner() && !applyingRemotePlayerCommand) {
        if (sendPlayerCommand("shuffle", { enabled: nextValue })) {
            playerShuffle = nextValue; storageSet("xrob_music_shuffle", String(playerShuffle)); updateShuffleButtons(); updateRemotePlayerOptimistic({ shuffle: nextValue });
        }
        return;
    }
    if (appState.player.source === "library" && appState.player.queue.length) {
        const currentId = currentSongId();
        if (nextValue) {
            shuffleRestoreQueue = [...appState.player.queue];
            shuffleRestoreCurrentId = currentId;
            const currentIndex = appState.player.queueIndex;
            const current = appState.player.queue[currentIndex];
            syncLibraryQueue([...appState.player.queue.slice(0, currentIndex), current, ...shuffledCopy(appState.player.queue.slice(currentIndex + 1))], currentIndex);
        } else if (Array.isArray(shuffleRestoreQueue) && shuffleRestoreQueue.length) {
            const restored = [...shuffleRestoreQueue];
            const restoredIndex = restored.findIndex(item => (item.id || item.name) === currentId || (item.id || item.name) === shuffleRestoreCurrentId);
            syncLibraryQueue(restored, restoredIndex >= 0 ? restoredIndex : 0);
            shuffleRestoreQueue = null;
            shuffleRestoreCurrentId = null;
        }
        renderEnhancedQueue();
    }
    playerShuffle = nextValue;
    storageSet("xrob_music_shuffle", String(playerShuffle));
    updateShuffleButtons();
    saveEnhancedQueue();
    if (!applyingRemotePlayerCommand && !isRemotePlayerOwner()) schedulePlayerStateBroadcast(true);
}

function shuffleLibrary() {
    if (isRemotePlayerOwner()) { setShuffle(true); return; }
    if (!appState.library.files.length) { showToast("No tracks to shuffle"); return; }
    appState.library.view = "tracks";
    appState.library.selectedArtistId = null;
    appState.library.selectedAlbumId = null;
    document.querySelectorAll(".library-tab").forEach(btn => btn.classList.toggle("active", btn.dataset.libraryView === "tracks"));
    renderLibraryView();
    if (appState.player.source === "library" && appState.player.queue.length) {
        const { queue, index } = getActiveLibraryQueueState();
        if (!playerShuffle) {
            shuffleRestoreQueue = [...queue];
            shuffleRestoreCurrentId = queue[index]?.id || queue[index]?.name || null;
        }
        const result = shuffleQueueAfterCurrent(queue, index);
        appState.player.queue = result.queue;
        appState.player.queueIndex = result.index;
        appState.library.playbackQueue = [...appState.player.queue];
        appState.library.currentIndex = appState.player.queueIndex;
        playerShuffle = true;
        storageSet("xrob_music_shuffle", "true");
        updateShuffleButtons();
        saveEnhancedQueue();
        renderEnhancedQueue();
        return;
    }
    const randomStartIndex = Math.floor(Math.random() * appState.library.files.length);
    playQueue(appState.library.files, randomStartIndex, true);
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

    let text = String(value || "").trim().toLowerCase();

    // Keep search-side duplicate detection in sync with the server/catalog
    // title normalization: strip track numbers and upload-only decorations.
    text = text.replace(/^\s*\[?\d{1,3}\]?\s*[-–—.)_:]+\s*/i, "");
    text = text.replace(/\s+#\d{1,4}\s*album\b.*$/i, "");
    text = text.replace(/\s*[\(\[]\s*(?:official\s+)?(?:lyric|lyrics|music\s+video|video|mv|visualizer|audio)(?:\s+video|\s+clip)?\s*[\)\]]/gi, " ");
    text = text.replace(/\s+(?:official\s+)?(?:music\s+)?video(?:\s+clip)?\s*$/i, "");
    text = text.replace(/\s+mv\s*$/i, "");
    text = text.replace(/\s+(?:lyric|lyrics)\s*(?:video|clip)?\s*$/i, "");
    text = text.replace(/\s+prod(?:uced)?\.?\s*by\b.*$/i, "");
    text = text.replace(/[^a-z0-9]+/g, "");

    return text;
}


function showToast(message) {
    emitAppEvent("ui:toast", { message: String(message ?? "") });

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

    storageSet(
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
    setAppState("ui", "activePage", tab);

    document
        .querySelectorAll(".tab-content")
        .forEach(section => {

            section.classList.remove("active");

        });


    document
        .querySelectorAll(".nav-link")
        .forEach(button => {
            const isActive = button.id === `btn-${tab}` || button.id === `mob-btn-${tab}`;
            button.classList.toggle("active", isActive);
            if (isActive) button.setAttribute("aria-current", "page");
            else button.removeAttribute("aria-current");
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
        openDownloadsDrawer();
        return;
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

let lastPlayerStateSavedAt = 0;
let audioLoadGeneration = 0;
function savePlayerState(force = false) {
    if (!audio) return;
    const now = Date.now();
    if (!force && now - lastPlayerStateSavedAt < 1200) return;
    const state = {
        clientId: PLAYER_CLIENT_ID,
        src: syncResourceUrl(audio.src || ""),
        currentTime: Number(audio.currentTime || 0),
        volume: Number.isFinite(Number(audio.volume)) ? Number(audio.volume) : 0.8,
        title: playerTitle?.textContent || "",
        artist: playerArtist?.textContent || "",
        art: syncResourceUrl(playerArt?.src || ""),
        songId: audio.dataset.xrobSongId || currentSongId() || "",
        source: appState.player.source || "",
        queueIndex: appState.player.source === "library" ? appState.player.queueIndex : (Number.isInteger(appState.player.homeQueueIndex) ? appState.player.homeQueueIndex : -1),
        wasPlaying: !audio.paused,
    };
    try { storageSet("xrob_music_player_state", JSON.stringify(state)); lastPlayerStateSavedAt = now; } catch (_) {}
}

function restorePlayerState() {
    if (!audio) return;
    if (isRemotePlayerOwner()) {
        // A remote owner must have a live state message; an orphaned owner key
        // from a closed/crashed tab should never block the restored local player.
        if (appState.player.remoteState) return;
        claimLocalPlayerWhenOwnerIsGone();
    }
    try {
        const raw = storageGet("xrob_music_player_state");
        if (!raw) return;
        const state = JSON.parse(raw);
        if (Number.isFinite(Number(state.volume))) {
            audio.volume = Math.max(0, Math.min(1, Number(state.volume)));
            if (volume) volume.value = audio.volume;
        }
        if (!state.src) return;

        // Restore queue/source identity before loading the media so Next/Previous
        // continue from the same queue after a browser refresh.
        if (Array.isArray(appState.player.queue) && appState.player.queue.length && state.queueIndex >= 0) {
            const savedId = String(state.songId || "");
            const found = savedId ? appState.player.queue.findIndex(item => trackKey(item) === savedId || String(item.id || "") === savedId) : -1;
            appState.player.queueIndex = found >= 0 ? found : Math.max(0, Math.min(Number(state.queueIndex) || 0, appState.player.queue.length - 1));
            appState.library.currentIndex = appState.player.queueIndex;
            appState.player.source = "library";
            saveEnhancedQueue();
        } else if (state.source === "home" && Array.isArray(appState.player.homeQueue) && appState.player.homeQueue.length) {
            appState.player.source = "home";
            appState.player.homeQueueIndex = Math.max(0, Math.min(Number(state.queueIndex) || 0, appState.player.homeQueue.length - 1));
        }

        const restorePosition = () => {
            if (Number.isFinite(Number(state.currentTime)) && Number.isFinite(audio.duration) && audio.duration > 0) {
                audio.currentTime = Math.min(Math.max(0, Number(state.currentTime)), Math.max(0, audio.duration - 0.25));
            }
            updateProgress();
            if (state.wasPlaying) {
                // Do not force autoplay on page load; modern browsers may block it.
                updatePlayingState(false);
            }
        };

        audio.dataset.xrobSongId = String(state.songId || "");
        updatePlayerInfo(state.title, state.artist, state.art);
        if (player) player.style.display = "grid";
        const loadGeneration = ++audioLoadGeneration;
        const expectedSource = new URL(state.src, location.href).href;
        audio.addEventListener("loadedmetadata", () => {
            if (loadGeneration !== audioLoadGeneration || audio.src !== expectedSource) return;
            restorePosition();
        }, { once: true });
        audio.src = expectedSource;
        audio.load();
    } catch (error) {
        console.warn("Could not restore player:", error);
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


function renderSeekVisual(percent) {
    const safe = Math.max(0, Math.min(100, Number(percent) || 0));
    if (seekFill) seekFill.style.setProperty("--seek-ratio", String(safe / 100));
}

function stopPlayerProgressFrame() {
    if (playerProgressFrame) {
        cancelAnimationFrame(playerProgressFrame);
        playerProgressFrame = null;
    }
}

function tickPlayerProgressFrame() {
    playerProgressFrame = null;
    if (!audio || audio.paused) return;
    updateProgress(true);
    playerProgressFrame = requestAnimationFrame(tickPlayerProgressFrame);
}

function startPlayerProgressFrame() {
    if (!audio || audio.paused || playerProgressFrame) return;
    playerProgressFrame = requestAnimationFrame(tickPlayerProgressFrame);
}

function updateProgress(animationFrame = false) {
    if (!audio || !seek) return;

    if (!audio.duration || !Number.isFinite(audio.duration)) {
        if (!isSeeking && !animationFrame) seek.value = 0;
        renderSeekVisual(0);
        if (curTime && curTime.textContent !== "0:00") curTime.textContent = "0:00";
        if (durTime && durTime.textContent !== "0:00") durTime.textContent = "0:00";
        return;
    }

    const currentTime = Math.max(0, Number(audio.currentTime) || 0);
    const percent = Math.max(0, Math.min(100, (currentTime / audio.duration) * 100));
    if (!isSeeking) {
        if (!animationFrame) seek.value = percent.toFixed(3);
        renderSeekVisual(percent);
    }

    const currentText = formatSeconds(currentTime);
    const durationText = formatSeconds(audio.duration);
    if (curTime && curTime.textContent !== currentText) curTime.textContent = currentText;
    if (durTime && durTime.textContent !== durationText) durTime.textContent = durationText;
}


function updatePlayingState(playing) {

    if (playBtn) {
        playBtn.innerHTML = `<i aria-hidden="true" data-lucide="${playing ? "pause" : "play"}"></i>`;
        playBtn.setAttribute("aria-label", playing ? "Pause" : "Play");
        playBtn.setAttribute("title", playing ? "Pause" : "Play");
        renderLocalIcons();
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

        button.innerHTML = `<i aria-hidden="true" data-lucide="play"></i> ${type === "library" ? "Play" : "Preview"}`;
        renderLocalIcons();
    }
}


function initAudioContext() {
    if (audioContext || !audio) return;
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass || typeof audioContext === "object" && audioContext) return;
    try {
        audioContext = new AudioContextClass();
        analyser = audioContext.createAnalyser();
        analyser.fftSize = 64;
        analyser.smoothingTimeConstant = 0.8;
        sourceNode = audioContext.createMediaElementSource(audio);
        replayGainNode = audioContext.createGain();
        replayGainNode.gain.value = 1;
        sourceNode.connect(replayGainNode);
        replayGainNode.connect(analyser);
        analyser.connect(audioContext.destination);
        visualizerData = new Uint8Array(analyser.frequencyBinCount);
        if (crossfadeAudio) initCrossfadeAudioGraph();
        applyReplayGainToActiveAudio();
        if (audio && !audio.paused && document.visibilityState === "visible") startVisualizer();
    } catch (error) {
        console.warn("Audio visualizer unavailable:", error);
    }
}

function initCrossfadeAudio() {
    if (crossfadeAudio || typeof Audio === "undefined") return;
    try {
        crossfadeAudio = new Audio();
        crossfadeAudio.crossOrigin = "anonymous";
        crossfadeAudio.preload = "auto";
        crossfadeAudio.volume = 1;
        if (audioContext) initCrossfadeAudioGraph();
    } catch (_) { crossfadeAudio = null; }
}

function initCrossfadeAudioGraph() {
    if (!audioContext || !crossfadeAudio || crossfadeSourceNode) return;
    try {
        crossfadeSourceNode = audioContext.createMediaElementSource(crossfadeAudio);
        crossfadeGainNode = audioContext.createGain();
        crossfadeGainNode.gain.value = 0;
        crossfadeSourceNode.connect(crossfadeGainNode);
        crossfadeGainNode.connect(analyser);
    } catch (_) {}
}

function dbToLinear(db) { return Math.pow(10, Number(db || 0) / 20); }

function currentReplayGain(track) {
    if (!playerSettings.replaygain_enabled || !track) return 0;
    const mode = playerSettings.replaygain_mode === "album" ? "album" : "track";
    const gain = mode === "album" ? Number(track.replaygain_album_gain) : Number(track.replaygain_track_gain);
    if (!Number.isFinite(gain)) return 0;
    const preamp = Number(playerSettings.replaygain_preamp_db) || 0;
    let db = gain + preamp;
    if (playerSettings.replaygain_prevent_clipping) {
        const peak = mode === "album" ? Number(track.replaygain_album_peak) : Number(track.replaygain_track_peak);
        if (Number.isFinite(peak) && peak > 0) db = Math.min(db, -20 * Math.log10(peak));
    }
    return Math.max(-30, Math.min(12, db));
}

function activeQueueTrack() {
    const q = appState.player.source === "library" ? getLibraryQueue() : (appState.player.homeQueue || []);
    const idx = appState.player.source === "library" ? appState.player.queueIndex : appState.player.homeQueueIndex;
    return q?.[Number(idx)] || null;
}

function applyReplayGainToActiveAudio(track = activeQueueTrack(), fade = 1) {
    const gain = dbToLinear(currentReplayGain(track));
    if (replayGainNode) replayGainNode.gain.setTargetAtTime(gain * Math.max(0, Math.min(1, fade)), audioContext.currentTime, 0.015);
    if (audio) audio.dataset.replayGainDb = String(currentReplayGain(track));
}

function stopCrossfadePreload() {
    if (crossfadeAudio) { try { crossfadeAudio.pause(); } catch (_) {} }
    if (crossfadeGainNode && audioContext) crossfadeGainNode.gain.setValueAtTime(0, audioContext.currentTime);
    if (crossfadeTimer) { clearTimeout(crossfadeTimer); crossfadeTimer = null; }
    crossfadeActive = false;
    crossfadePrepared = null;
}

function nextQueueTrack() {
    const q = appState.player.source === "library" ? getLibraryQueue() : (appState.player.homeQueue || []);
    const idx = appState.player.source === "library" ? appState.player.queueIndex : appState.player.homeQueueIndex;
    if (!q.length || !Number.isInteger(Number(idx))) return null;
    let next = Number(idx) + 1;
    if (next >= q.length) {
        if (playerRepeatMode !== "queue") return null;
        next = 0;
    }
    return q[next] || null;
}

function prepareNextTrack() {
    const next = nextQueueTrack();
    if (!next?.stream || !crossfadeAudio) return false;
    const url = new URL(next.stream, location.href).href;
    if (crossfadePrepared?.url === url && crossfadeAudio.readyState >= 2) return true;
    try {
        crossfadeAudio.pause();
        crossfadeAudio.src = url;
        crossfadeAudio.load();
        crossfadePrepared = { url, track: next };
        return true;
    } catch (_) {
        crossfadePrepared = null;
        return false;
    }
}

function maybeStartCrossfade() {
    if (!audio || audio.paused || !playerSettings.crossfade_seconds || !crossfadeAudio || !crossfadePrepared) return false;
    const duration = Number(audio.duration || 0);
    const current = Number(audio.currentTime || 0);
    const fade = Math.max(0, Math.min(Number(playerSettings.crossfade_seconds) || 0, duration / 2));
    if (!(duration > 0 && fade > 0 && duration - current > fade + 0.02)) return false;
    if (crossfadeTimer) return true;
    const targetUrl = crossfadePrepared.url;
    crossfadeAudio.currentTime = 0;
    crossfadeAudio.volume = Number.isFinite(Number(audio.volume)) ? Number(audio.volume) : 1;
    if (crossfadeSourceNode && audioContext) crossfadeGainNode.gain.setValueAtTime(0, audioContext.currentTime);
    crossfadeAudio.play().catch(() => { crossfadeTimer = null; crossfadeActive = false; });
    const remaining = Math.max(0, duration - current - fade);
    crossfadeTimer = window.setTimeout(() => {
        crossfadeTimer = null;
        crossfadeActive = true;
        if (!audio || audio.paused || !crossfadePrepared || crossfadePrepared.url !== targetUrl) return;
        const start = performance.now();
        const tick = () => {
            const elapsed = (performance.now() - start) / 1000;
            const p = Math.max(0, Math.min(1, elapsed / fade));
            if (replayGainNode && audioContext) replayGainNode.gain.setTargetAtTime(dbToLinear(currentReplayGain(activeQueueTrack())) * (1 - p), audioContext.currentTime, 0.015);
            if (crossfadeGainNode && audioContext) crossfadeGainNode.gain.setTargetAtTime(dbToLinear(currentReplayGain(crossfadePrepared.track)) * p, audioContext.currentTime, 0.015);
            if (p < 1 && !audio.paused) requestAnimationFrame(tick);
        };
        tick();
    }, remaining * 1000);
    return true;
}

function finalizePreparedTrackIfNeeded() {
    if ((!playerSettings.gapless_playback && !crossfadeActive) || !crossfadePrepared || !crossfadeAudio) return false;
    if (crossfadeAudio.paused && crossfadeAudio.readyState < 2) return false;
    const prepared = crossfadePrepared;
    const carried = Math.max(0, Number(crossfadeAudio.currentTime || 0));
    const idx = appState.player.source === "library"
        ? getLibraryQueue().findIndex(t => trackKey(t) === trackKey(prepared.track))
        : (appState.player.homeQueue || []).findIndex(t => trackKey(t) === trackKey(prepared.track));
    if (idx < 0) return false;
    crossfadeAudio.pause();
    stopCrossfadePreload();
    if (appState.player.source === "library") { appState.player.queueIndex = idx; appState.library.currentIndex = idx; }
    else appState.player.homeQueueIndex = idx;
    audio.pause();
    const loadGeneration = ++audioLoadGeneration;
    const expectedSource = new URL(prepared.url, location.href).href;
    audio.src = expectedSource;
    audio.load();
    audio.addEventListener("loadedmetadata", () => {
        if (loadGeneration !== audioLoadGeneration || audio.src !== expectedSource || crossfadePrepared) return;
        audio.currentTime = Math.min(carried, Math.max(0, Number(audio.duration || carried) - 0.05));
        updatePlayerInfo(prepared.track.title, prepared.track.artist, prepared.track.cover);
        audio.dataset.xrobSongId = String(prepared.track.id || "");
        applyReplayGainToActiveAudio(prepared.track);
        beginPlaySession(currentSongId());
        audio.play().catch(() => {});
    }, {once:true});
    return true;
}

function finalizeCrossfadeIfNeeded() {
    if (!crossfadeActive || !crossfadePrepared || !crossfadeAudio || crossfadeAudio.paused) return false;
    return finalizePreparedTrackIfNeeded();
}


function sleepTimerRemainingMs() { return sleepTimerDeadline > Date.now() ? sleepTimerDeadline - Date.now() : 0; }
function sleepTimerLabel() {
    const remaining = sleepTimerRemainingMs();
    if (!remaining) return "Sleep: Off";
    const mins = Math.max(1, Math.ceil(remaining / 60000));
    return `Sleep: ${mins}m`;
}
function updateSleepTimerUI() {
    const label = sleepTimerLabel();
    ["gp-sleep-btn", "queueSleep"].forEach(id => { const el=document.getElementById(id); if(el) { el.textContent=label; el.setAttribute("aria-label", label); el.title=label; } });
}
function clearSleepTimer() { sleepTimerDeadline = 0; storageRemove(SLEEP_TIMER_KEY); if (sleepTimerInterval) { clearInterval(sleepTimerInterval); sleepTimerInterval=null; } updateSleepTimerUI(); }
function setSleepTimer(minutes) {
    const safe = Math.max(0, Number(minutes)||0);
    if (!safe) { clearSleepTimer(); showToast("Sleep timer off"); return; }
    sleepTimerDeadline = Date.now() + safe * 60000;
    storageSet(SLEEP_TIMER_KEY, String(sleepTimerDeadline));
    if (!sleepTimerInterval) sleepTimerInterval = setInterval(() => {
        if (!sleepTimerRemainingMs()) {
            clearSleepTimer();
            if (isRemotePlayerOwner()) sendPlayerCommand("pause");
            else audio?.pause();
            showToast("⏰ Sleep timer paused playback");
            return;
        }
        updateSleepTimerUI();
    }, 1000);
    updateSleepTimerUI();
    showToast(`⏰ Sleep timer: ${safe} minutes`);
}
function cycleSleepTimer() {
    const remaining = sleepTimerRemainingMs();
    if (!remaining) return setSleepTimer(15);
    const current = Math.round(remaining / 60000);
    const idx = SLEEP_TIMER_PRESETS.findIndex(v => v > current);
    const next = idx >= 0 ? SLEEP_TIMER_PRESETS[idx] : 0;
    setSleepTimer(next);
}
async function openLyricsPanel() {
    const songId = audio?.dataset?.xrobSongId || currentSongId();
    if (!songId) { showToast("No track is playing"); return; }
    const modal = document.getElementById("lyrics-modal");
    const body = document.getElementById("lyricsContent");
    const title = document.getElementById("lyricsTitle");
    if (!modal || !body) return;
    const requestId = ++lyricsRequestId;
    modal.hidden = false;
    body.innerHTML = '<div class="lyrics-loading">Loading lyrics…</div>';
    if (title) title.textContent = playerTitle?.textContent || "Lyrics";
    try {
        const response = await apiFetch(`api/lyrics/${encodeURIComponent(songId)}`, {cache:"no-store"});
        const data = await response.json().catch(()=>({}));
        if (requestId !== lyricsRequestId) return;
        if (!response.ok) throw new Error(data.detail || "Lyrics unavailable");
        renderLyricsPanel(data);
    } catch (error) {
        if (requestId !== lyricsRequestId) return;
        reportAppError(error, {scope:"lyrics", action:"load"});
        body.innerHTML = `<div class="lyrics-empty"><i data-lucide="file-question" aria-hidden="true"></i><strong>No lyrics available</strong><span>${escapeHtml(error.message || "Lyrics could not be loaded")}</span></div>`;
        renderLocalIcons();
    }
}
function renderLyricsPanel(data) {
    const body=document.getElementById("lyricsContent"); if(!body) return;
    if (lyricsAnimationFrame) cancelAnimationFrame(lyricsAnimationFrame);
    lyricsAnimationFrame = null;
    const synced = Array.isArray(data.structuredLyrics?.[0]?.line) ? data.structuredLyrics[0].line.slice().sort((a,b)=>Number(a?.start||0)-Number(b?.start||0)) : [];
    const plain = String(data.plainLyrics || "");
    if (!synced.length && !plain) {
        body.innerHTML = '<div class="lyrics-empty"><i data-lucide="file-question" aria-hidden="true"></i><strong>No lyrics found</strong><span>Embedded lyrics and LRCLIB did not return a result.</span></div>'; renderLocalIcons(); return;
    }
    if (synced.length) {
        body.innerHTML = `<div class="lyrics-source">${escapeHtml(data.source || "Lyrics")}</div><div class="lyrics-lines"></div>`;
        const list=body.querySelector(".lyrics-lines");
        const rows=[];
        synced.forEach(line => { const row=document.createElement("div"); row.className="lyrics-line"; row.dataset.start=String(Number(line?.start||0)); row.textContent=String(line?.value||""); list.appendChild(row); rows.push(row); });
        let activeIndex=-1;
        const findActiveIndex=(pos)=>{ let lo=0,hi=rows.length-1,best=-1; while(lo<=hi){const mid=(lo+hi)>>1;const startMs=Number(rows[mid].dataset.start||0);if(startMs<=pos){best=mid;lo=mid+1;}else hi=mid-1;} return best; };
        const tick = () => {
            if (body.closest(".xrob-modal")?.hidden) { lyricsAnimationFrame=null; return; }
            const media = audio;
            if (!media || media.paused || media.ended) { lyricsAnimationFrame=requestAnimationFrame(tick); return; }
            const nextIndex=findActiveIndex(Number(media.currentTime||0)*1000);
            if(nextIndex!==activeIndex){
                if(activeIndex>=0) rows[activeIndex].classList.remove("active");
                activeIndex=nextIndex;
                if(activeIndex>=0){ rows[activeIndex].classList.add("active"); rows[activeIndex].scrollIntoView({block:"center",behavior:"smooth"}); }
            }
            lyricsAnimationFrame = requestAnimationFrame(tick);
        };
        lyricsAnimationFrame = requestAnimationFrame(tick);
    } else {
        body.innerHTML = `<div class="lyrics-source">${escapeHtml(data.source || "Lyrics")}</div><pre class="lyrics-plain">${escapeHtml(plain)}</pre>`;
    }
    renderLocalIcons();
}

function drawVisualizer() {

    if (
        !canvasCtx ||
        !analyser ||
        !audio ||
        audio.paused ||
        document.visibilityState !== "visible"
    ) {
        visualizerFrame = null;
        return;
    }

    const length = analyser.frequencyBinCount;
    if (!visualizerData || visualizerData.length !== length) {
        visualizerData = new Uint8Array(length);
    }
    analyser.getByteFrequencyData(visualizerData);

    canvasCtx.clearRect(
        0,
        0,
        canvas.width,
        canvas.height
    );

    const barWidth = canvas.width / length;
    canvasCtx.fillStyle = "#1ed760";

    for (let i = 0; i < length; i++) {
        const height = Math.max(2, (visualizerData[i] / 255) * canvas.height);
        canvasCtx.fillRect(
            i * barWidth,
            canvas.height - height,
            Math.max(1, barWidth - 1),
            height
        );
    }

    visualizerFrame = requestAnimationFrame(drawVisualizer);
}

function startVisualizer() {
    if (visualizerFrame || !audio || audio.paused || document.visibilityState !== "visible") return;
    visualizerFrame = requestAnimationFrame(drawVisualizer);
}

function stopVisualizer() {
    if (visualizerFrame) {
        cancelAnimationFrame(visualizerFrame);
        visualizerFrame = null;
    }
}


function updatePlayerBarMode() {
    if (!player) return;
    const active = Boolean((audio && audio.src) || String(appState.player.source || "").trim());
    player.classList.toggle("is-idle", !active);
    player.dataset.mode = active ? "active" : "idle";
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
        playerArt.src = art || "";
        playerArt.alt = title || "";
    }
    updatePlayerBarMode();
    updateMediaSession();
}


function updateMediaSession() {
    if (!("mediaSession" in navigator) || !("MediaMetadata" in window)) return;
    const title = playerTitle?.textContent || "Unknown Track";
    const artist = playerArtist?.textContent || "Unknown Artist";
    const artwork = playerArt?.src ? [{ src: playerArt.src, sizes: "512x512" }] : [];
    try {
        navigator.mediaSession.metadata = new MediaMetadata({ title, artist, album: "Xrob Music", artwork });
        const remotePlayback = isRemotePlayerOwner() && appState.player.remoteState;
        navigator.mediaSession.playbackState = (remotePlayback ? Boolean(appState.player.remoteState.paused) : Boolean(audio?.paused)) ? "paused" : "playing";
    } catch (_) {}
}

function installMediaSession() {
    if (!("mediaSession" in navigator)) return;
    const actions = {
        play: () => {
            if (isRemotePlayerOwner()) { if (sendPlayerCommand("play")) updateRemotePlayerOptimistic({ paused: false }); }
            else if (audio?.src) { setPlayerOwner(); audio.play().catch(() => {}); }
        },
        pause: () => {
            if (isRemotePlayerOwner()) { if (sendPlayerCommand("pause")) updateRemotePlayerOptimistic({ paused: true }); }
            else audio?.pause();
        },
        previoustrack: () => {
            if (isRemotePlayerOwner()) { const current = Number(appState.player.remoteState?.currentTime || 0); if (current > 3) sendPlayerCommand("seek", { time: 0 }); else sendPlayerCommand("previous", { repeat: playerRepeatMode }); }
            else { setPlayerOwner(); playPreviousTrack(); }
        },
        nexttrack: () => {
            if (isRemotePlayerOwner()) sendPlayerCommand("next", { repeat: playerRepeatMode });
            else { setPlayerOwner(); playNextTrack(); }
        },
        seekbackward: details => {
            const offset = Math.max(1, Number(details.seekOffset || 10));
            if (isRemotePlayerOwner()) { const time = Math.max(0, Number(appState.player.remoteState?.currentTime || 0) - offset); if (sendPlayerCommand("seek", { time })) updateRemotePlayerOptimistic({ currentTime: time }); }
            else if (audio) audio.currentTime = Math.max(0, audio.currentTime - offset);
        },
        seekforward: details => {
            const offset = Math.max(1, Number(details.seekOffset || 10));
            if (isRemotePlayerOwner()) { const base = Number(appState.player.remoteState?.currentTime || 0); const duration = Number(appState.player.remoteState?.duration || 0); const time = Math.min(duration > 0 ? duration : base + offset, base + offset); if (sendPlayerCommand("seek", { time })) updateRemotePlayerOptimistic({ currentTime: time }); }
            else if (audio && Number.isFinite(audio.duration)) audio.currentTime = Math.min(audio.duration, audio.currentTime + offset);
        },
        seekto: details => {
            if (!Number.isFinite(Number(details.seekTime))) return;
            const time = Math.max(0, Number(details.seekTime));
            if (isRemotePlayerOwner()) { if (sendPlayerCommand("seek", { time })) updateRemotePlayerOptimistic({ currentTime: time }); }
            else if (audio) audio.currentTime = Math.min(audio.duration || time, time);
        },
    };
    Object.entries(actions).forEach(([action, handler]) => {
        try { navigator.mediaSession.setActionHandler(action, handler); } catch (_) {}
    });
}

function toggleAudioStream(
    button,
    url,
    type,
    title,
    artist,
    art,
    songId = null,
    fromRemote = false
) {

    if (
        !audio ||
        !button ||
        !url
    ) {
        return;
    }

    if (!fromRemote && isRemotePlayerOwner()) {
        const sourceName = type === "search" ? "home" : (type || "library");
        const queue = sourceName === "library" ? appState.player.queue : (appState.player.homeQueue || []);
        const queueIndex = sourceName === "library" ? appState.player.queueIndex : (Number.isInteger(appState.player.homeQueueIndex) ? appState.player.homeQueueIndex : -1);
        sendPlayerCommand("load-play", { src: syncResourceUrl(new URL(url, location.href).href), title, artist, art: syncResourceUrl(art), songId, source: sourceName, queue: normalizeSyncQueue(queue), queueIndex });
        updateRemotePlayerOptimistic({ src: syncResourceUrl(new URL(url, location.href).href), title, artist, art: syncResourceUrl(art), songId, source: sourceName, paused: false, queueIndex, currentTime: 0 });
        return;
    }

    if (!fromRemote) setPlayerOwner();
    stopCrossfadePreload();
    initAudioContext();
    initCrossfadeAudio();

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
        button.innerHTML = `<i data-lucide="clock-3" aria-hidden="true"></i> Loading...`;
        renderLocalIcons();
    }


    updatePlayerInfo(
        title,
        artist,
        art
    );


    if (player) {
        player.style.display = "grid";
    }


    // Persist the outgoing track before replacing its source. Browser pause events are not
    // guaranteed to run at the moment a new source is assigned.
    persistCurrentPosition(true);
    audio.pause();

    audio.removeAttribute("src");

    audio.dataset.xrobSongId = String(songId || "");
    if (type) appState.player.source = type === "search" ? "home" : type;
    savePlayerState();
    stopCrossfadePreload();
    const loadGeneration = ++audioLoadGeneration;
    audio.src = absoluteUrl;

    audio.load();


    const selectedTrack = activeQueueTrack() || { id: songId, title, artist, cover: art };
    audio.addEventListener("loadedmetadata", () => {
        if (loadGeneration !== audioLoadGeneration || audio.src !== absoluteUrl) return;
        applyReplayGainToActiveAudio(selectedTrack);
    }, { once: true });

    audio.play()
        .then(() => {

            if (
                button.classList.contains(
                    "btn-preview"
                )
            ) {

                button.innerHTML = `<i data-lucide="pause" aria-hidden="true"></i> Pause`;
                renderLocalIcons();
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

                button.innerHTML = `<i data-lucide="circle-alert" aria-hidden="true"></i> Error`;
                renderLocalIcons();

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
            if (!applyingRemotePlayerCommand && !isRemotePlayerOwner()) recordPlay(audio.dataset.xrobSongId || currentSongId());
            const duration = Number(audio?.duration || 0);
            const remaining = duration > 0 ? duration - Number(audio?.currentTime || 0) : Infinity;
            const fadeWindow = Number(playerSettings.crossfade_seconds || 0);
            if (playerSettings.gapless_playback || fadeWindow > 0) {
                if (remaining < Math.max(8, fadeWindow + 4)) prepareNextTrack();
                if (fadeWindow > 0 && remaining <= fadeWindow + 0.25) maybeStartCrossfade();
                if (playerSettings.gapless_playback && fadeWindow <= 0 && remaining <= 0.12 && finalizePreparedTrackIfNeeded()) return;
            }
            if (!applyingRemotePlayerCommand && !isRemotePlayerOwner()) schedulePlayerStateBroadcast(false);

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
            if (!applyingRemotePlayerCommand && Date.now() >= suppressLocalOwnershipUntil) setPlayerOwner();
            initAudioContext();
            if (audioContext?.state === "suspended") audioContext.resume().catch(() => {});
            startVisualizer();
            updatePlayingState(true);
            const playingTrackId = audio.dataset.xrobSongId || currentSongId();
            if (playingTrackId && playingTrackId !== playSessionTrackId) beginPlaySession(playingTrackId);
            updateMediaSession();
            startPlayerProgressFrame();
            schedulePlayerStateBroadcast(true);
        }
    );


    audio.addEventListener(
        "pause",
        () => {
            if (!playerHandoffStoppingRemote) persistCurrentPosition(true);
            stopVisualizer();
            updatePlayingState(false);
            updateMediaSession();
            stopPlayerProgressFrame();
            if (!applyingRemotePlayerCommand && !isRemotePlayerOwner()) schedulePlayerStateBroadcast(true);
        }
    );


    audio.addEventListener(
        "ended",
        () => {

            if (isRemotePlayerOwner()) { persistCurrentPosition(true); return; }
            persistCurrentPosition(true);
            updatePlayingState(
                false
            );
            stopVisualizer();
            stopPlayerProgressFrame();

            if (seek) {
                seek.value = 0;
                renderSeekVisual(0);
            }

            if (curTime) {
                curTime.textContent =
                    "0:00";
            }

            if (typeof playerRepeatMode !== "undefined" && playerRepeatMode === "track") {
                audio.currentTime = 0;
                applyReplayGainToActiveAudio(activeQueueTrack());
                beginPlaySession(currentSongId());
                audio.play().catch(console.error);
                return;
            }

            if (finalizeCrossfadeIfNeeded()) {
                return;
            }
            if (finalizePreparedTrackIfNeeded()) {
                return;
            }

            if (appState.player.source === "home") {
                if (advanceHomeQueue(1)) return;
            }

            if (appState.player.source === "library") {
                if (advanceLibraryQueue(1, true)) return;
            }

            if (playerSettings.keep_playing) {
                continuePlayingAutomatically().catch(error => reportAppError(error, {scope:"player", action:"keep-playing"}));
                return;
            }

            if (activePreviewBtn) {

                resetPreviewButton(
                    activePreviewBtn
                );

                activePreviewBtn = null;
            }

            appState.player.homeQueueIndex = -1;
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

            const remoteOwner = isRemotePlayerOwner();
            if (remoteOwner && !audio.src && appState.player.remoteState?.src) {
                takeoverRemotePlayer().then(moved => {
                    if (moved && audio.paused && !appState.player.remoteState) audio.play().catch(() => {});
                }).catch(() => {});
                return;
            }
            if (remoteOwner) {
                const remotePaused = appState.player.remoteState ? Boolean(appState.player.remoteState.paused) : Boolean(audio.paused);
                if (sendPlayerCommand(remotePaused ? "play" : "pause")) {
                    updateRemotePlayerOptimistic({ paused: !remotePaused });
                }
                return;
            }
            if (!audio.src) return;
            setPlayerOwner();
            initAudioContext();
            if (audio.paused) {
                audio.play().catch(console.error);
            } else {
                audio.pause();
            }
        }
    );

    prevBtn?.addEventListener("click", () => {
        if (isRemotePlayerOwner()) {
            const current = Number(appState.player.remoteState?.currentTime || 0);
            if (current > 3) { if (sendPlayerCommand("seek", { time: 0 })) updateRemotePlayerOptimistic({ currentTime: 0 }); }
            else sendPlayerCommand("previous", { repeat: playerRepeatMode });
        } else { setPlayerOwner(); playPreviousTrack(); }
    });

    nextBtn?.addEventListener("click", () => {
        if (isRemotePlayerOwner()) sendPlayerCommand("next", { repeat: playerRepeatMode });
        else { setPlayerOwner(); playNextTrack(); }
    });

    document.getElementById("gp-device-takeover")?.addEventListener("click", async () => {
        const moved = await takeoverRemotePlayer();
        if (moved) showToast("▶ Playback moved to this device");
    });

    document.getElementById("gp-shuffle-btn")?.addEventListener("click", () => setShuffle(!playerShuffle));
    document.getElementById("libraryShuffleButton")?.addEventListener("click", shuffleLibrary);
    document.getElementById("libraryPlayAllButton")?.addEventListener("click", () => playQueue(appState.library.files, 0, false));
    setShuffle(playerShuffle);


    const seekWrap = document.getElementById("gp-seek-wrap") || seek;
    const seekFromPointer = event => {
        const duration = isRemotePlayerOwner() ? Number(appState.player.remoteState?.duration || 0) : Number(audio?.duration || 0);
        if (!seek || !seekWrap || !(duration > 0) || !Number.isFinite(Number(event.clientX))) return;
        const rect = seekWrap.getBoundingClientRect();
        if (!(rect.width > 0)) return;
        const ratio = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
        seek.value = String(ratio * 100);
        const targetTime = ratio * duration;
        if (isRemotePlayerOwner()) {
            scheduleRemoteSeek(targetTime);
            updateRemotePlayerOptimistic({ currentTime: targetTime });
        } else {
            setPlayerOwner();
            try { audio.currentTime = targetTime; } catch (_) {}
        }
        renderSeekVisual(ratio * 100);
        if (curTime) curTime.textContent = formatSeconds(targetTime);
        persistCurrentPosition(false);
    };
    const releaseSeek = event => {
        if (!isSeeking) return;
        isSeeking = false;
        try { seekWrap.releasePointerCapture?.(event.pointerId); } catch (_) {}
        flushRemoteSeek();
        updateProgress();
        savePlayerState(true);
    };
    seekWrap?.addEventListener("pointerdown", event => {
        if (event.button !== undefined && event.button !== 0) return;
        isSeeking = true;
        try { seekWrap.setPointerCapture?.(event.pointerId); } catch (_) {}
        seekFromPointer(event);
        event.preventDefault();
    });
    seekWrap?.addEventListener("pointermove", event => {
        if (!isSeeking) return;
        seekFromPointer(event);
        event.preventDefault();
    });
    seekWrap?.addEventListener("pointerup", releaseSeek);
    seekWrap?.addEventListener("pointercancel", releaseSeek);
    let seekRemoteTimer = null;
    let pendingRemoteSeek = null;
    const flushRemoteSeek = () => {
        if (seekRemoteTimer) { window.clearTimeout(seekRemoteTimer); seekRemoteTimer = null; }
        if (pendingRemoteSeek === null) return;
        const time = pendingRemoteSeek;
        pendingRemoteSeek = null;
        sendPlayerCommand("seek", { time });
    };
    const scheduleRemoteSeek = time => {
        pendingRemoteSeek = time;
        if (seekRemoteTimer) return;
        seekRemoteTimer = window.setTimeout(() => {
            seekRemoteTimer = null;
            flushRemoteSeek();
        }, 90);
    };
    seek?.addEventListener("input", () => {
        const duration = isRemotePlayerOwner() ? Number(appState.player.remoteState?.duration || 0) : Number(audio?.duration || 0);
        if (duration > 0) {
            const ratio = Math.max(0, Math.min(1, Number(seek.value) / 100));
            const targetTime = ratio * duration;
            if (isRemotePlayerOwner()) scheduleRemoteSeek(targetTime);
            else { setPlayerOwner(); try { audio.currentTime = targetTime; } catch (_) {} }
            if (isRemotePlayerOwner() && appState.player.remoteState) updateRemotePlayerOptimistic({ currentTime: targetTime });
            renderSeekVisual(ratio * 100);
            if (curTime) curTime.textContent = formatSeconds(targetTime);
            persistCurrentPosition(false);
        }
    });
    seek?.addEventListener("change", () => { isSeeking = false; flushRemoteSeek(); updateProgress(); savePlayerState(true); });


    const savedVolume =
        storageGet(
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

            const nextVolume = Number(volume.value);
            if (isRemotePlayerOwner()) {
                sendPlayerCommand("volume", { volume: nextVolume });
                updateRemotePlayerOptimistic({ volume: nextVolume });
            } else { setPlayerOwner(); audio.volume = nextVolume; }

            storageSet(
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
    const topStatus=document.getElementById("contentStorageStatus");
    const topText=document.getElementById("contentStorageStatusText");
    if (topStatus) topStatus.dataset.state = String(data.state || "unknown");
    if (topText) {
        if (data.state === "offline" || !data.exists) topText.textContent = "Music storage offline";
        else if (!data.writable) topText.textContent = "Music storage read-only";
        else if (data.state === "unknown") topText.textContent = "Checking music storage…";
        else topText.textContent = `Music storage online · ${data.free || "0 B"} free`;
    }
    if (status) {
        if (data.state === "offline" || !data.exists) { status.textContent = data.error ? `Library storage is offline: ${data.error}` : "Library storage is unavailable."; status.dataset.state = "error"; }
        else if (data.state === "unknown") { status.textContent = "Checking library storage…"; status.dataset.state = "warning"; }
        else if (!data.writable) { status.textContent = "Library storage is read-only."; status.dataset.state = "error"; }
        else { status.textContent = "Library storage is online and ready."; status.dataset.state = "success"; }
    }
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
        const settings = await apiFetchJson("api/settings", { cache: "no-store" }, {scope:"settings", action:"load"});
        applySettingsToForm(settings);
    } catch (error) {
        // apiFetchJson already reports the transport/application error.
    }
}


/* ============================================================
   CACHE HELPERS
   ============================================================ */

function saveLibraryCache() {

    try {

        storageSet(
            LIBRARY_CACHE_KEY,
            JSON.stringify({
                files: appState.library.files,
                artists: appState.library.artists,
                albums: appState.library.albums,
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
            storageGet(
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

        appState.library.files = cache.files;
        appState.library.artists = Array.isArray(cache.artists) ? cache.artists : [];
        appState.library.albums = Array.isArray(cache.albums) ? cache.albums : [];

        libraryLoadedFromCache =
            true;


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

        storageSet(
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
            storageGet(
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

let libraryRefreshTimer = null;
function scheduleLibraryRefresh() {
    if (libraryRefreshTimer) window.clearTimeout(libraryRefreshTimer);
    libraryRefreshTimer = window.setTimeout(async () => {
        libraryRefreshTimer = null;
        try {
            await refreshLibraryCache();
            renderLibraryView();
        } catch (error) {
            reportAppError(error, {scope:"library", action:"live-refresh"});
        }
    }, 180);
}

async function refreshLibraryCache() {

    try {

        const response =
            await apiFetch(
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


        appState.library.files = data.files || [];
        appState.library.playbackQueue = appState.player.queue.length ? [...appState.player.queue] : appState.library.files;
        appState.library.artists = data.artists || [];
        appState.library.albums = data.albums || [];
        syncLibraryUiState();
        appState.library.ready = data.ready !== false;
        appState.library.status = String(data.library_state || data.storage_state || data.storage?.state || (appState.library.ready ? "ready" : "loading"));
        appState.library.revision = Number.isFinite(Number(data.revision)) ? Number(data.revision) : appState.library.revision;
        appState.library.storageError = String(data.storage?.error || "");
        renderStorage(data.storage || {state: data.storage_state || appState.library.status});
        appState.library.lastRefreshAt = Date.now();
        emitAppEvent("library:updated", {revision: appState.library.revision, count: appState.library.files.length, status: appState.library.status});
        await resolveEnhancedQueueIds();
        reconcileEnhancedQueue();

        saveLibraryCache();

        libraryLoadedFromCache =
            false;



        const side =
            document.getElementById(
                "sideLibCount"
            );


        if (side) {
            side.textContent =
                appState.library.files.length;
        }


        const statTracks =
            document.getElementById(
                "statTracks"
            );


        if (statTracks) {
            statTracks.textContent =
                appState.library.files.length;
        }


        const mobile =
            document.getElementById(
                "mobLibCount"
            );


        if (mobile) {
            mobile.textContent =
                appState.library.files.length;
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
        reportAppError(error, {scope:"library", action:"refresh"});
        throw error;
    }
}


function applyLiveStats(stats) {
    if (!stats || stats.ready === false) return;
    appState.stats = {
        tracks: Number.isFinite(Number(stats.tracks)) ? Number(stats.tracks) : appState.stats.tracks,
        artists: Number.isFinite(Number(stats.artists)) ? Number(stats.artists) : appState.stats.artists,
        albums: Number.isFinite(Number(stats.albums)) ? Number(stats.albums) : appState.stats.albums,
        plays: Number.isFinite(Number(stats.all_play_count)) ? Number(stats.all_play_count) : appState.stats.plays,
        totalBytes: Number.isFinite(Number(stats.total_bytes)) ? Number(stats.total_bytes) : appState.stats.totalBytes,
        folderSize: typeof stats.folder_size === "string" ? stats.folder_size : appState.stats.folderSize
    };
    emitAppEvent("stats:updated", appState.stats);
    if (appState.stats.tracks !== null) ["statTracks","downloadStatTracks","homeTracks","statusTracks","subsonicTracks"].forEach(id => setLiveCounter(id, appState.stats.tracks));
    if (appState.stats.artists !== null) ["statArtists","homeArtists","statusArtists"].forEach(id => setLiveCounter(id, appState.stats.artists));
    if (appState.stats.albums !== null) ["statAlbums","downloadStatAlbums","homeAlbums","statusAlbums"].forEach(id => setLiveCounter(id, appState.stats.albums));
    if (appState.stats.plays !== null) ["homePlays","statusPlays"].forEach(id => setLiveCounter(id, appState.stats.plays));
    if (appState.stats.folderSize) { const el=document.getElementById("statusSize"); if(el) el.textContent=appState.stats.folderSize; }
}
function applyLivePlayCount(value) {
    const count=Number(value); if(!Number.isFinite(count)||count<0) return;
    if(appState.stats.plays===null || count>appState.stats.plays) appState.stats.plays=count;
    ["homePlays","statusPlays"].forEach(id=>setLiveCounter(id,appState.stats.plays));
}
let statsRefreshTimer = null;
function scheduleStatsRefresh(delay=150) {
    if(statsRefreshTimer) window.clearTimeout(statsRefreshTimer);
    statsRefreshTimer = window.setTimeout(()=>{ statsRefreshTimer=null; if(!document.hidden) loadStats().catch(error=>reportAppError(error,{scope:"stats",action:"event-refresh"})); }, Math.max(0, Number(delay)||0));
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

        const stats = await apiFetchJson("/api/stats", {cache:"no-store", signal:controller.signal}, {scope:"stats", action:"load"});
        applyLiveStats(stats);

    } catch (error) {

        if (
            error.name ===
            "AbortError"
        ) {

            reportAppError(error, {scope:"stats", action:"timeout"});

        } else {

            reportAppError(error, {scope:"stats", action:"load"});
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
        document.getElementById("statTracks")?.replaceChildren(String(appState.library.files.length));
        document.getElementById("statArtists")?.replaceChildren(String(appState.library.artists.length));
        document.getElementById("statAlbums")?.replaceChildren(String(appState.library.albums.length));
        renderLibraryView();
        loadDetailedLibraryStats();
        updateLoadingCircle("library", 100, "Library ready");
        setTimeout(() => hideLoadingCircle("library"), 250);
    } catch (error) {
        hideLoadingCircle("library");
        if (appState.library.files.length) {
            renderLibraryView();
            showToast("Showing cached library");
        } else {
            list.innerHTML = `<div class="downloads-empty"><div class="empty-icon"><i data-lucide="circle-alert" aria-hidden="true"></i></div><div class="empty-title">Could not load library</div><div class="empty-text">${escapeHtml(error.message || "Unknown error")}</div></div>`;
            renderLocalIcons();
        }
    }
}

function renderLibraryView() {
    syncLibraryUiState();
    const list = document.getElementById("libraryList");
    const dashboard = document.getElementById("libraryStatsDashboard");
    if (!list) return;

    const showStatistics = appState.library.view === "statistics";
    if (dashboard) dashboard.hidden = !showStatistics;
    list.hidden = showStatistics;

    if (showStatistics) {
        loadDetailedLibraryStats();
        return;
    }
    if (!appState.library.files.length) {
        if (appState.library.status === "offline") {
            renderEmpty(list, "cloud-off", "Music storage is offline", "Reconnect your NAS or music storage, then run a scan.");
            return;
        }
        if (appState.library.status === "scanning" || appState.library.status === "loading") {
            renderEmpty(list, "loader-circle", "Library is scanning", "Your music catalog will appear here when the scan finishes.");
            return;
        }
        if (appState.library.status === "error") {
            renderEmpty(list, "circle-alert", "Library scan failed", "Open Settings or run a new scan after checking the music storage.");
            return;
        }
        if (appState.library.status === "empty") {
            renderEmpty(list, "music-2", "Your library is empty", "Add music to the configured folder, then run a library scan.");
            return;
        }
    }
    if (appState.library.view === "playlists") return loadPlaylistsView();
    if (appState.library.view === "recent") return renderLibraryCollections("recent");
    if (appState.library.view === "most") return renderLibraryCollections("most");

    const query = String(document.getElementById("libSearchQuery")?.value || "").trim().toLowerCase();
    if (appState.library.view === "artists") return renderArtists(list, query);
    if (appState.library.view === "albums") return renderAlbums(list, query);
    if (appState.library.view === "artist-detail") return renderArtistDetail(list, query);
    if (appState.library.view === "album-detail") return renderAlbumDetail(list, query);
    renderTracks(list, query);
}

function renderEmpty(list, icon, title, text = "") {
    const iconName = /^[a-z0-9-]+$/i.test(String(icon || "")) ? String(icon) : "music-2";
    list.innerHTML = `<div class="downloads-empty"><div class="empty-icon"><i data-lucide="${escapeHtml(iconName)}" aria-hidden="true"></i></div><div class="empty-title">${escapeHtml(title)}</div>${text ? `<div class="empty-text">${escapeHtml(text)}</div>` : ""}</div>`;
    renderLocalIcons();
}

function playQueue(queue, index = 0, shuffle = false) {
    if (!Array.isArray(queue) || !queue.length) { showToast("No playable tracks"); return false; }
    const requestedIndex = Math.max(0, Math.min(Number(index) || 0, queue.length - 1));
    const originalQueue = [...queue];
    let playbackQueue = [...queue];
    let playbackIndex = requestedIndex;
    if (shuffle) {
        const current = playbackQueue[requestedIndex];
        const before = playbackQueue.slice(0, requestedIndex);
        const after = shuffledCopy(playbackQueue.slice(requestedIndex + 1));
        playbackQueue = [...before, current, ...after];
        playbackIndex = before.length;
        shuffleRestoreQueue = originalQueue;
        shuffleRestoreCurrentId = current?.id || current?.name || null;
    } else {
        shuffleRestoreQueue = null;
        shuffleRestoreCurrentId = null;
    }
    syncLibraryQueue(playbackQueue, playbackIndex);
    appState.player.source = "library";
    
    appState.player.lastEventAt = Date.now();
    playerShuffle = Boolean(shuffle);
    storageSet("xrob_music_shuffle", String(playerShuffle));
    updateShuffleButtons();
    renderEnhancedQueue();
    playLibraryTrack(playbackIndex);
    return true;
}

function createTrackCard(file, queue = appState.library.files) {
    const encoded = encodeURIComponent(file.name || "");
    const cover = file.cover || `api/library/cover/${encoded}`;
    const stream = file.stream || `api/library/stream/${encoded}`;
    const card = document.createElement("article");
    card.className = "result-card";
    card.dataset.libraryName = file.name || "";
    const plays = Number(file.play_count ?? file.plays ?? 0);
    card.innerHTML = `<div class="thumb-wrapper"><img src="${escapeHtml(cover)}" alt="" loading="lazy"><span class="track-play-count" title="${plays} play${plays === 1 ? "" : "s"}"><i data-lucide="play" aria-hidden="true"></i> ${plays}</span></div><div class="track-info"><div class="track-title">${escapeHtml(file.title || file.name || "Unknown Track")}</div><div class="track-artist">${escapeHtml(file.artist || "Unknown Artist")} · ${escapeHtml(file.album || "Unknown Album")}</div><div class="track-meta-line"><span>${plays === 1 ? "1 play" : `${plays} plays`}</span></div></div><div class="btn-group"><button type="button" class="btn-preview"><i data-lucide="play" aria-hidden="true"></i> Play</button><button type="button" class="btn-refresh btn-queue-next" title="Play this track next"><i data-lucide="list-plus" aria-hidden="true"></i> Next</button><button type="button" class="btn-refresh btn-queue-add" title="Add this track to the end of the queue"><i data-lucide="plus" aria-hidden="true"></i> Queue</button><button type="button" class="btn-danger"><i data-lucide="trash-2" aria-hidden="true"></i> Delete</button></div>`;
    card.querySelector("img")?.addEventListener("error", e => e.currentTarget.removeAttribute("src"), { once: true });
    const play = () => {
        const activeQueue = getLibraryQueue();
        const activeIndex = activeQueue.findIndex(x => x.id === file.id || x.name === file.name);
        if (appState.player.source === "library" && activeQueue.length && activeIndex >= 0) {
            appState.library.playbackQueue = [...activeQueue];
            appState.library.currentIndex = activeIndex;
            appState.player.queue = [...activeQueue];
            appState.player.queueIndex = activeIndex;
            saveEnhancedQueue();
            renderEnhancedQueue();
        } else {
            const startIndex = Math.max(0, queue.findIndex(x => x.id === file.id || x.name === file.name));
            if (playerShuffle) {
                playQueue(queue, startIndex, true);
            } else {
                setEnhancedQueue(queue, startIndex);
                appState.player.source = "library";
                toggleAudioStream(
                    card.querySelector(".btn-preview"),
                    stream,
                    "library",
                    file.title || file.name,
                    file.artist || "Unknown Artist",
                    cover,
                    file.id || null
                );
            }
            return;
        }
        appState.player.source = "library";
        toggleAudioStream(card.querySelector(".btn-preview"), stream, "library", file.title || file.name, file.artist || "Unknown Artist", cover, file.id || null);
    };
    card.querySelector(".btn-preview")?.addEventListener("click", e => { e.stopPropagation(); play(); });
    card.querySelector(".btn-queue-next")?.addEventListener("click", e => { e.stopPropagation(); addTrackToQueue(file, true); });
    card.querySelector(".btn-queue-add")?.addEventListener("click", e => { e.stopPropagation(); addTrackToQueue(file, false); });
    card.querySelector(".btn-danger")?.addEventListener("click", e => { e.stopPropagation(); deleteFile(file.name); });
    card.addEventListener("dblclick", play);
    return card;
}

function renderArtists(list, query) {
    const artists = appState.library.artists.filter(a => !query || String(a.name || "").toLowerCase().includes(query));
    list.innerHTML = "";
    if (!artists.length) return renderEmpty(list, "user-round", "No artists found", query ? "Try another search." : "Scan your library to build the artist catalog.");
    artists.forEach(artist => {
        const card = document.createElement("article");
        card.className = "catalog-card artist-card";
        card.innerHTML = `<button type="button" class="catalog-main-action"><img class="artist-cover" src="${escapeHtml(artist.cover||"")}" alt="" loading="lazy" onerror="this.style.display='none'"/><div><strong>${escapeHtml(artist.name)}</strong><span>${artist.album_count || 0} album${artist.album_count === 1 ? "" : "s"} · ${artist.song_count || 0} track${artist.song_count === 1 ? "" : "s"}</span></div></button><div class="catalog-actions"><button type="button" class="btn-refresh artist-art-btn">Cover</button><button type="button" class="btn-preview catalog-play"><i data-lucide="play" aria-hidden="true"></i> Play</button></div>`;
        card.querySelector(".catalog-main-action")?.addEventListener("click", () => openArtist(artist.id));
        card.querySelector(".catalog-play")?.addEventListener("click", e => { e.stopPropagation(); const tracks = appState.library.files.filter(f => (artist.song_ids || []).includes(f.id)); playQueue(tracks, 0, false); });
        card.querySelector(".artist-art-btn")?.addEventListener("click", e => { e.stopPropagation(); const input=document.createElement("input"); input.type="file"; input.accept="image/jpeg,image/png,image/webp"; input.onchange=async()=>{const file=input.files?.[0]; if(!file)return; const fd=new FormData(); fd.append("upload",file); const rr=await apiFetch(`api/library/artist-artwork/${encodeURIComponent(artist.id)}`,{method:"POST",body:fd}); if(rr.ok){showToast("✅ Artist cover saved"); renderArtists(list,query);} else showToast("❌ Could not save artist cover");}; input.click(); });
        list.appendChild(card);
    });
    renderLocalIcons();
}

function renderAlbums(list, query) {
    const albums = appState.library.albums.filter(a => !query || `${a.name || ""} ${a.artist || ""}`.toLowerCase().includes(query));
    list.innerHTML = "";
    if (!albums.length) return renderEmpty(list, "disc-3", "No albums found", query ? "Try another search." : "Scan your library to build the album catalog.");
    albums.forEach(album => list.appendChild(createAlbumCard(album)));
    renderLocalIcons();
}

function createAlbumCard(album) {
    const card = document.createElement("article");
    card.className = "catalog-card album-card";
    const cover = album.cover || "";
    card.innerHTML = `<img src="${escapeHtml(cover)}" alt="" loading="lazy"><div><strong>${escapeHtml(album.name)}</strong><span>${escapeHtml(album.artist || "Unknown Artist")} · ${album.song_count || 0} track${album.song_count === 1 ? "" : "s"}${album.year ? ` · ${escapeHtml(album.year)}` : ""}</span><button type="button" class="btn-preview"><i data-lucide="play" aria-hidden="true"></i> Play album</button></div>`;
    card.querySelector("img")?.addEventListener("error", e => e.currentTarget.removeAttribute("src"), { once: true });
    card.querySelector(".btn-preview")?.addEventListener("click", e => { e.stopPropagation(); playAlbum(album.id); });
    card.querySelector("strong")?.addEventListener("click", () => openAlbum(album.id));
    card.querySelector("img")?.addEventListener("click", () => openAlbum(album.id));
    return card;
}

function renderArtistDetail(list, query) {
    const artist = appState.library.artists.find(a => a.id === appState.library.selectedArtistId);
    if (!artist) { appState.library.view = "artists"; return renderArtists(list, query); }
    const ids = new Set(artist.song_ids || []);
    const tracks = appState.library.files.filter(f => ids.has(f.id));
    const albums = appState.library.albums.filter(a => (a.song_ids || []).some(id => ids.has(id)));
    list.innerHTML = `<div class="catalog-detail-header"><button type="button" class="btn-refresh library-back-button"><i data-lucide="arrow-left" aria-hidden="true"></i> Artists</button><div><h3>${escapeHtml(artist.name)}</h3><p>${albums.length} album${albums.length === 1 ? "" : "s"} · ${tracks.length} track${tracks.length === 1 ? "" : "s"}</p></div><button type="button" class="btn-preview artist-detail-play"><i data-lucide="play" aria-hidden="true"></i> Play artist</button></div>`;
    list.querySelector(".library-back-button")?.addEventListener("click", () => { appState.library.selectedArtistId = null; appState.library.view = "artists"; renderLibraryView(); });
    list.querySelector(".artist-detail-play")?.addEventListener("click", () => playQueue(tracks, 0, false));
    if (albums.length) {
        const heading = document.createElement("h3"); heading.className = "catalog-section-heading"; heading.textContent = "Albums"; list.appendChild(heading);
        albums.forEach(album => list.appendChild(createAlbumCard(album)));
    }
    const filtered = tracks.filter(file => { const hay = `${file.title || ""} ${file.album || ""}`.toLowerCase(); return !query || hay.includes(query); });
    if (filtered.length) {
        const heading = document.createElement("h3"); heading.className = "catalog-section-heading"; heading.textContent = "Tracks"; list.appendChild(heading);
        filtered.forEach(file => list.appendChild(createTrackCard(file, tracks)));
    } else if (!albums.length) renderEmpty(list, "music-2", "No matching tracks", "Try another search.");
    renderLocalIcons();
}

function renderAlbumDetail(list, query) {
    const album = appState.library.albums.find(a => a.id === appState.library.selectedAlbumId);
    if (!album) { appState.library.view = "albums"; return renderAlbums(list, query); }
    const ids = new Set(album.song_ids || []);
    const tracks = appState.library.files.filter(f => ids.has(f.id));
    list.innerHTML = `<div class="catalog-detail-header"><button type="button" class="btn-refresh library-back-button"><i data-lucide="arrow-left" aria-hidden="true"></i> Albums</button><div><h3>${escapeHtml(album.name)}</h3><p>${escapeHtml(album.artist || "Unknown Artist")} · ${tracks.length} track${tracks.length === 1 ? "" : "s"}</p></div><button type="button" class="btn-preview album-detail-play"><i data-lucide="play" aria-hidden="true"></i> Play album</button></div>`;
    list.querySelector(".library-back-button")?.addEventListener("click", () => { appState.library.selectedAlbumId = null; appState.library.view = "albums"; renderLibraryView(); });
    list.querySelector(".album-detail-play")?.addEventListener("click", () => playAlbum(album.id));
    const filtered = tracks.filter(file => { const hay = `${file.title || ""} ${file.artist || ""}`.toLowerCase(); return !query || hay.includes(query); });
    if (filtered.length) filtered.forEach(file => list.appendChild(createTrackCard(file, tracks))); else renderEmpty(list, "disc-3", "No matching tracks", "Try another search.");
    renderLocalIcons();
}

function filterLibrary() { renderLibraryView(); }
function openArtist(id) { if (!appState.library.artists.some(a => a.id === id)) return; appState.library.selectedArtistId = id; appState.library.selectedAlbumId = null; appState.library.view = "artist-detail"; document.getElementById("libSearchQuery").value = ""; renderLibraryView(); }
function openAlbum(id) { if (!appState.library.albums.some(a => a.id === id)) return; appState.library.selectedAlbumId = id; appState.library.selectedArtistId = null; appState.library.view = "album-detail"; document.getElementById("libSearchQuery").value = ""; renderLibraryView(); }
function playAlbum(id) { const album = appState.library.albums.find(a => a.id === id); if (!album) return showToast("Album not found"); const ids = new Set(album.song_ids || []); const tracks = appState.library.files.filter(f => ids.has(f.id)); playQueue(tracks, 0, false); }
function playLibraryTrack(index) {
    const queue = getLibraryQueue();
    if (!queue.length || index < 0 || index >= queue.length) return;
    if (!appState.player.queue.length) syncLibraryQueue(queue, index);
    appState.player.source = "library";
    appState.player.queueIndex = index;
    appState.library.currentIndex = index;
    appState.library.playbackQueue = [...appState.player.queue];
    saveEnhancedQueue();
    renderEnhancedQueue();
    const file = appState.player.queue[index] || queue[index];
    const encoded = encodeURIComponent(file.name || "");
    const cover = file.cover || `api/library/cover/${encoded}`;
    const stream = file.stream || `api/library/stream/${encoded}`;
    const button = document.querySelector(`.result-card[data-library-name="${CSS.escape(file.name || "")}"] .btn-preview`) || document.createElement("button");
    button.type = "button";
    button.className = "btn-preview";
    toggleAudioStream(button, stream, "library", file.title || file.name, file.artist || "Unknown Artist", cover, file.id || null);
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
            await apiFetch(
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
    const requestId = ++appState.search.requestId;
    if (searchAbortController) { try { searchAbortController.abort(); } catch (_) {} }
    const requestController = typeof AbortController !== "undefined" ? new AbortController() : null;
    searchAbortController = requestController;


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
    appState.search.requestId = requestId;
    appState.search.query = query;
    appState.search.pending = Boolean(query);

    if (!query) {
        appState.search.query = "";
        appState.search.page = 1;
        appState.search.hasMore = false;
        appState.search.loadingMore = false;
        appState.search.pending = false;
        results.innerHTML = "";
        if (searchAbortController === requestController) searchAbortController = null;
        status.textContent =
            "Enter a search term.";

        hideSearchLoading();

        return;
    }

    appState.search.query = query;
    appState.search.page = 1;
    appState.search.hasMore = true;
    appState.search.loadingMore = false;

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
         * Search the external catalog immediately. Duplicate status is supplied
         * by the server's lightweight persisted library index, so a full library
         * metadata sync never blocks the search request.
         */
        smoothSearchLoading(
            5,
            25,
            "Searching for music...",
            250
        );

        smoothSearchLoading(
            20,
            45,
            "Searching for music...",
            400
        );

        const response =
            await apiFetch(
                `api/search?q=${
                    encodeURIComponent(query)
                }&source=youtube&page=1`,
                {
                    cache: "no-store",
                    ...(requestController ? { signal: requestController.signal } : {})
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

        if (requestId !== appState.search.requestId) return;

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

            appState.search.hasMore = false;

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

        appState.search.hasMore = response.headers.get("X-Search-Has-More") === "1";
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

        if (requestId !== appState.search.requestId || error?.name === "AbortError") return;
        reportAppError(error, {scope:"search"});

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

        if (requestId === appState.search.requestId && button) {
            button.disabled = false;
        }
        if (searchAbortController === requestController) searchAbortController = null;
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

            if (item.source === "library") {
                const title = item.title || item.name || "Unknown Track";
                const artist = item.artist || "Unknown Artist";
                const album = item.album || "Unknown Album";
                const thumb = String(item.cover || "");
                card.dataset.libraryName = item.name || "";
                card.innerHTML = `
                    <div class="thumb-wrapper"><img src="${escapeHtml(thumb)}" alt="" loading="lazy">${item.duration_text ? `<span class="badge-duration">${escapeHtml(item.duration_text)}</span>` : ""}</div>
                    <div class="track-info"><div class="track-title">${escapeHtml(title)}</div><div class="track-artist"><i data-lucide="user-round" aria-hidden="true"></i> ${escapeHtml(artist)} · ${escapeHtml(album)}</div></div>
                    <div class="btn-group"><button type="button" class="btn-preview"><i data-lucide="play" aria-hidden="true"></i> Play</button><button type="button" class="btn-download queue-local-btn"><i data-lucide="plus" aria-hidden="true"></i> Queue</button></div>`;
                const play = card.querySelector(".btn-preview");
                play?.addEventListener("click", e => { e.stopPropagation(); toggleAudioStream(play, item.stream || "", "library", title, artist, thumb, item.id || null); });
                card.querySelector(".queue-local-btn")?.addEventListener("click", e => { e.stopPropagation(); addTrackToQueue({...item, name:item.name}, false); });
                card.querySelector("img")?.addEventListener("error", e => e.currentTarget.removeAttribute("src"), {once:true});
                results.appendChild(card);
                renderLocalIcons();
                return;
            }


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
                        <i data-lucide="user-round" aria-hidden="true"></i> ${escapeHtml(
                            item.artist || item.channel || "Unknown Artist"
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
                        apiUrl("static/logo.png");

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


            if (item.already_downloaded) {

                const match = item.library_match;
                const matchText = match?.path ? ` · ${escapeHtml(match.path)}` : "";
                group.innerHTML = `
                    <div class="badge-library"><i data-lucide="circle-check" aria-hidden="true"></i> In Library${matchText}</div>
                `;

            } else if (item.possible_match) {

                const match = item.library_match || {};
                const confidence = Math.round((Number(item.match_confidence || 0)) * 100);
                group.innerHTML = `
                    <div class="badge-library badge-possible"><i data-lucide="triangle-alert" aria-hidden="true"></i> Possible Match${confidence ? ` · ${confidence}%` : ""}</div>
                    <button type="button" class="btn-download btn-download-anyway" data-id="${escapeHtml(item.id || "")}"><i data-lucide="download" aria-hidden="true"></i> Download Anyway</button>
                `;
                group.querySelector(".btn-download-anyway")?.addEventListener("click", () =>
                    startDownload(item.url, item.title, item.id, item.artist || item.channel, group.querySelector(".btn-download-anyway"), item.album || "", item.duration || 0)
                );
                if (match.path) {
                    const note=document.createElement("div"); note.className="search-match-path"; note.textContent=`Existing: ${match.path}`; group.appendChild(note);
                }

            } else if (item.already_queued) {

                group.innerHTML = `
                    <div class="badge-library"><i data-lucide="clock-3" aria-hidden="true"></i> In Download Queue</div>
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


                preview.innerHTML = `<i data-lucide="play" aria-hidden="true"></i> Preview`;


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
                            item.artist || item.channel,
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


                download.innerHTML = `<i data-lucide="download" aria-hidden="true"></i> Save`;


                download.addEventListener(
                    "click",
                    () =>
                        startDownload(
                            item.url,
                            item.title,
                            item.id,
                            item.artist || item.channel,
                            download,
                            item.album || "",
                            item.duration || 0
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
    renderLocalIcons();
}


async function loadMoreResults() {

    if (
        appState.search.loadingMore ||
        !appState.search.hasMore ||
        !appState.search.query
    ) {
        return;
    }


    appState.search.loadingMore = true;
    const requestId = appState.search.requestId;
    const queryAtStart = appState.search.query;
    const requestController = typeof AbortController !== "undefined" ? new AbortController() : null;
    searchAbortController = requestController;

    const nextPage =
        appState.search.page + 1;


    const loader =
        document.getElementById(
            "infiniteLoader"
        );


    if (loader) {
        loader.style.display = "block";
    }


    try {

        const response =
            await apiFetch(
                `api/search?q=${
                    encodeURIComponent(
                        appState.search.query
                    )
                }&source=youtube&page=${
                    nextPage
                }`,
                {
                    cache: "no-store",
                    ...(requestController ? { signal: requestController.signal } : {})
                }
            );


        const data =
            await response.json()
                .catch(
                    () => []
                );

        if (requestId !== appState.search.requestId || queryAtStart !== appState.search.query) return;

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

            appState.search.hasMore = false;

        } else {

            appState.search.page = nextPage;
            appState.search.hasMore = response.headers.get("X-Search-Has-More") === "1";
            renderItems(data);
        }

    } catch (error) {

        if (requestId !== appState.search.requestId || error?.name === "AbortError") return;
        reportAppError(error, {scope:"search", action:"load-more"});
        showToast(
            "⚠️ Could not load more results"
        );

    } finally {

        if (requestId === appState.search.requestId && loader) {
            loader.style.display = "none";
        }
        if (searchAbortController === requestController) searchAbortController = null;

        appState.search.loadingMore = false;
    }
}


function bindSearch() {
    const searchButton = document.getElementById("searchBtn");
    if (searchButton && !searchButton.dataset.bound) {
        searchButton.dataset.bound = "1";
        searchButton.addEventListener("click", searchMusic);
    }
    const input = document.getElementById("query");
    if (input && !input.dataset.searchBound) {
        input.dataset.searchBound = "1";
        input.addEventListener("input", () => {
            if (searchDebounceTimer) clearTimeout(searchDebounceTimer);
            const q = input.value.trim();
            if (!q) { searchMusic(); return; }
            searchDebounceTimer = setTimeout(() => searchMusic(), 420);
        });
        input.addEventListener("keydown", event => {
            if (event.key !== "Enter" || event.isComposing) return;
            event.preventDefault();
            if (searchDebounceTimer) { clearTimeout(searchDebounceTimer); searchDebounceTimer = null; }
            searchMusic();
        });
    }
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
            "clock-3",
            "status-queued"
        ],

        downloading: [
            "Downloading",
            "download",
            "status-downloading"
        ],

        processing: [
            "Processing",
            "settings",
            "status-processing"
        ],

        completed: [
            "Completed",
            "circle-check",
            "status-completed"
        ],

        error: [
            "Failed",
            "circle-alert",
            "status-error"
        ],

        failed: [
            "Failed",
            "circle-alert",
            "status-error"
        ],

        cancelled: [
            "Cancelled",
            "x",
            "status-cancelled"
        ],

        canceled: [
            "Cancelled",
            "x",
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
            await apiFetch(
                "/api/tasks",
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


        appState.downloads.tasks =
            Array.isArray(tasks)
                ? tasks
                : [];
        
        appState.downloads.lastUpdatedAt = Date.now();


        appState.downloads.tasks.forEach(
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
            appState.downloads.tasks
        );


        const signature =
            taskSignature(
                appState.downloads.tasks
            );


        const taskChanged = signature !== appState.downloads.lastSignature;
        if (
            force ||
            taskChanged
        ) {

            renderDownloads(
                appState.downloads.tasks
            );
        }

        if (taskChanged && appState.downloads.tasks.some(task => task.status === "completed")) {
            loadStats().catch(error => reportAppError(error, {scope:"stats", action:"refresh-after-download"}));
            queueHomeRefreshAfterDownload();
            // A completed download is already committed to the catalog. Refresh the
            // in-memory Library once the filesystem/catalog settle, without forcing
            // the user through a manual scan or replacing the visible list with a
            // transient empty/error state.
            refreshLibraryCache().then(() => {
                renderLibraryView();
            }).catch(error => reportAppError(error, {scope:"library", action:"refresh-after-download"}));
        }

        appState.downloads.lastSignature = signature;
        appState.downloads.lastUpdatedAt = Date.now();
        emitAppEvent("downloads:updated", {count: appState.downloads.tasks.length, signature});

    } catch (error) {
        reportAppError(error, {scope:"downloads", action:"poll-tasks"});
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
    button,
    album = "",
    duration = 0
) {

    if (!url) {

        showToast(
            "❌ Invalid download URL"
        );

        return;
    }


    if (button) {

        button.disabled = true;

        button.innerHTML = '<i data-lucide="clock-3" aria-hidden="true"></i> Queuing...';
        renderLocalIcons();
    }


    try {

        // Authoritative click-time library check. Search badges are only advisory;
        // this check resolves provider metadata and prevents downloading an existing track.
        const checkResponse = await apiFetch("/api/download/check", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url, title, artist, album, duration })
        });
        const check = await checkResponse.json().catch(() => ({}));
        if (!checkResponse.ok) throw new Error(check.detail || "Could not verify library duplicate state.");
        if (check.status === "already_downloaded") {
            if (button) { button.disabled = true; button.className = "btn-refresh"; button.innerHTML = '<i data-lucide="circle-check" aria-hidden="true"></i> In Library'; renderLocalIcons(); }
            showToast("✓ Already in library — download skipped");
            return;
        }
        if (check.status === "already_queued") {
            if (button) { button.disabled = true; button.className = "btn-refresh"; button.innerHTML = '<i data-lucide="clock-3" aria-hidden="true"></i> In Queue'; renderLocalIcons(); }
            showToast("⏳ Already in download queue");
            return;
        }

        const response =
            await apiFetch(
                "/api/download",
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
                            artist,
                            album,
                            duration
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


        if (data.task) {
            const existingIndex = appState.downloads.tasks.findIndex(t => String(t.id) === String(data.task.id));
            if (existingIndex >= 0) appState.downloads.tasks[existingIndex] = data.task;
            else appState.downloads.tasks.unshift(data.task);
            appState.downloads.lastSignature = taskSignature(appState.downloads.tasks);
            updateQueueCounters(appState.downloads.tasks);
            renderDownloads(appState.downloads.tasks);
        }

        if (data.status === "already_downloaded" && button) {
            button.disabled = true;
            button.innerHTML = '<i data-lucide="circle-check" aria-hidden="true"></i> In Library';
            button.className = "btn-refresh";
            renderLocalIcons();
        }

        showToast(
            data.status === "already_queued"
                ? "⏳ Already in queue"
                : data.status === "already_downloaded"
                    ? "✓ Already downloaded"
                    : "⬇️ Added to Downloads"
        );


        openDownloadsDrawer();
        // The API has already enqueued the task. Refresh the drawer in the
        // background so the Save button never waits on a second round-trip.
        pollTasks(true).catch(error => reportAppError(error, {scope:"downloads", action:"poll-fallback"}));

    } catch (error) {

        showToast(
            "❌ " +
            error.message
        );


        if (button) {

            button.disabled = false;

            button.innerHTML = '<i data-lucide="download" aria-hidden="true"></i> Save';
            renderLocalIcons();
        }
    }
}


async function cancelTask(taskId) {

    try {

        const response =
            await apiFetch(
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
            await apiFetch(
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
        const response = await apiFetch(`api/tasks/${encodeURIComponent(taskId)}/retry`, { method: "POST" });
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
            await apiFetch(
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


        appState.downloads.tasks =
            appState.downloads.tasks.filter(
                task =>
                    !isFinishedTask(task)
            );


        appState.downloads.lastSignature = "";


        renderDownloads(
            appState.downloads.tasks
        );


        updateQueueCounters(
            appState.downloads.tasks
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

async function continuePlayingAutomatically() {
    const currentId = currentSongId();
    const response = await apiFetch(`api/daily-mix?limit=12&variant=${Date.now()}&exclude=${encodeURIComponent(currentId || "")}`, {cache:"no-store"});
    const data = await response.json().catch(()=>({}));
    if (!response.ok) throw new Error(data.detail || "Could not continue playback");
    const currentKeys = new Set(getLibraryQueue().map(trackKey));
    const candidates = (Array.isArray(data.tracks) ? data.tracks : []).filter(track => track?.stream && !currentKeys.has(trackKey(track)));
    if (!candidates.length) { showToast("🎵 Nothing else to play"); return false; }
    const queue = [...getLibraryQueue(), ...candidates];
    const startIndex = queue.findIndex(item => trackKey(item) === trackKey(candidates[0]));
    syncLibraryQueue(queue, startIndex);
    appState.player.source = "library";
    renderEnhancedQueue();
    playLibraryTrack(startIndex);
    showToast("▶ Keeping playback going");
    return true;
}

function advanceLibraryQueue(direction = 1, fromEnded = false) {
    const queue = getLibraryQueue();
    if (!queue.length) return false;
    const current = getQueueIndex();
    const step = direction >= 0 ? 1 : -1;
    let next = current + step;
    const repeatQueue = playerRepeatMode === "queue";

    if (next >= queue.length || next < 0) {
        if (!repeatQueue) {
            showToast(step > 0 ? "🎵 End of queue" : "🎵 This is the first track");
            return false;
        }
        next = step > 0 ? 0 : queue.length - 1;
    }
    playLibraryTrack(next);
    return true;
}

function advanceHomeQueue(direction = 1) {
    const queue = appState.player.homeQueue || [];
    if (!queue.length) return false;
    const current = Number.isInteger(appState.player.homeQueueIndex) ? appState.player.homeQueueIndex : -1;
    let next = current + (direction >= 0 ? 1 : -1);
    if (next >= queue.length || next < 0) {
        if (playerRepeatMode !== "queue") {
            showToast(direction >= 0 ? "🎵 End of Recently Added" : "🎵 This is the first track");
            return false;
        }
        next = direction >= 0 ? 0 : queue.length - 1;
    }
    if (direction < 0 && audio && audio.currentTime > 3) { audio.currentTime = 0; return true; }
    playHomeTrack(next);
    return true;
}

function playNextTrack() {
    if (appState.player.source === "home") return advanceHomeQueue(1);
    if (appState.player.source === "library") return advanceLibraryQueue(1);
    return false;
}

function playPreviousTrack() {
    if (audio && audio.currentTime > 3) {
        audio.currentTime = 0;
        return true;
    }
    if (appState.player.source === "home") return advanceHomeQueue(-1);
    if (appState.player.source === "library") return advanceLibraryQueue(-1);
    return false;
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


    appState.player.homeQueue =
        recent;

    if (
        !Number.isInteger(
            appState.player.homeQueueIndex
        )
    ) {

        appState.player.homeQueueIndex =
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
                apiUrl("static/logo.png");

            img.alt = "";

            img.loading =
                "lazy";


            img.addEventListener(
                "error",
                () => {

                    img.src =
                        apiUrl("static/logo.png");

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

                    appState.player.homeQueueIndex =
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


async function loadHome(options = {}) {
    const container = document.getElementById("recentTracks");
    if (!container) return;

    const silentFallback = Boolean(options.silentFallback);
    const requestId = ++homeRefreshRequestId;
    const cachedRecent = loadRecentlyAddedCache();

    // Only the first request owns the loading UI. Background refreshes must never
    // clear a healthy Recently Added view while a download is being finalized.
    if (cachedRecent.length) {
        appState.library.recentTracksCache = cachedRecent;
        renderRecentlyAdded(cachedRecent);
        hideLoadingCircle("recent");
    } else if (!silentFallback) {
        updateLoadingCircle("recent", 5, "Loading Recently Added...");
        container.innerHTML = "";
    }

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);

    try {
        if (!cachedRecent.length && !silentFallback) {
            updateLoadingCircle("recent", 15, "Connecting to Xrob Music...");
        }

        let response = null;
        let lastError = null;
        // Download completion can briefly overlap the catalog commit/filesystem
        // reconciliation. Retry the read instead of falling back immediately.
        for (let attempt = 0; attempt < 3; attempt += 1) {
            try {
                response = await apiFetch("api/home", {
                    cache: "no-store",
                    signal: controller.signal,
                    timeoutMs: 10000,
                });
                if (response.ok) break;
                lastError = new Error(`Home API returned HTTP ${response.status}`);
                if (![408, 429, 500, 502, 503, 504].includes(response.status) || attempt === 2) break;
            } catch (error) {
                lastError = error;
                if (error?.name === "AbortError" || attempt === 2 || controller.signal.aborted) throw error;
            }
            await new Promise(resolve => setTimeout(resolve, 400 * (attempt + 1)));
        }
        if (!response?.ok) throw lastError || new Error("Could not load Recently Added");

        const data = await response.json();
        const stats = data.stats || {};
        applyLiveStats({ ...stats, ready: true });
        const recent = Array.isArray(data.recently_added) ? data.recently_added : [];

        // Ignore an older response that lost a race with a newer home refresh.
        if (requestId !== homeRefreshRequestId) return;

        appState.library.recentTracksCache = recent;
        saveRecentlyAddedCache(recent);
        renderRecentlyAdded(recent);
        updateLoadingCircle("recent", 100, "Recently Added ready");
        setTimeout(() => hideLoadingCircle("recent"), 250);
    } catch (error) {
        if (requestId !== homeRefreshRequestId) return;
        console.error("Home loading failed:", error);

        if (cachedRecent.length) {
            // Cached content is intentionally retained silently during background
            // download/catalog transitions. A toast here looked like a download
            // failure even though the cached UI was still valid.
            renderRecentlyAdded(cachedRecent);
            hideLoadingCircle("recent");
            if (!silentFallback && error?.name !== "AbortError") {
                // Do not surface transient API failures as a scary warning. The
                // next lifecycle/task refresh will retry automatically.
                reportAppError(error, {scope:"home", action:"refresh-with-cache"});
            }
        } else {
            hideLoadingCircle("recent");
            container.innerHTML = `
                <div class="home-empty">
                    <div class="empty-icon"><i data-lucide="circle-alert" aria-hidden="true"></i></div>
                    <div class="empty-title">Could not load Recently Added</div>
                    <div class="empty-text">${escapeHtml(error.message || "Unknown error")}</div>
                </div>`;
            renderLocalIcons();
        }
    } finally {
        clearTimeout(timeout);
    }
}

function queueHomeRefreshAfterDownload() {
    if (homeRefreshInFlight) {
        homeRefreshQueued = true;
        return homeRefreshInFlight;
    }
    homeRefreshInFlight = (async () => {
        // Let the catalog commit and filesystem metadata settle before reading Home.
        await new Promise(resolve => setTimeout(resolve, 650));
        await loadHome({ silentFallback: true });
    })().catch(error => {
        reportAppError(error, {scope:"home", action:"refresh-after-download"});
    }).finally(() => {
        homeRefreshInFlight = null;
        if (homeRefreshQueued) {
            homeRefreshQueued = false;
            queueHomeRefreshAfterDownload();
        }
    });
    return homeRefreshInFlight;
}


async function refreshLibrary() {
    const button = document.getElementById("libraryRefreshButton");
    if (button) button.disabled = true;
    try {
        updateLoadingCircle("library", 10, "Quick scan…");
        const response = await apiFetch("api/library/scan/quick", { method: "POST", cache: "no-store" });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || "Quick scan failed.");
        await refreshLibraryCache();
        await loadStats();
        renderLibraryView();
        loadDetailedLibraryStats();
        updateLoadingCircle("library", 100, "Library ready");
        showToast(`✅ Quick scan complete • ${data.tracks || appState.library.files.length} tracks`);
    } catch (error) {
        showToast("❌ " + (error.message || "Quick scan failed."));
    } finally {
        setTimeout(() => hideLoadingCircle("library"), 250);
        if (button) button.disabled = false;
    }
}


function renderLocalIcons() {
    const paths = {
        house: [['path','M3 10.5 12 3l9 7.5v9a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 19.5z'],['path','M9 21v-6h6v6']],
        search: [['circle','11 11 7 7'],['path','m20 20-4-4']],
        download: [['path','M12 3v12'],['path','m7 10 5 5 5-5'],['path','M5 21h14']],
        library: [['path','M4 19.5V6.5A2.5 2.5 0 0 1 6.5 4H20v16H6.5A2.5 2.5 0 0 1 4 17.5'],['path','M4 17.5A2.5 2.5 0 0 1 6.5 15H20']],
        settings: [['circle','12 12 3'],['path','M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-1.9 1.9-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.1h-2.7v-.1a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1-1.9-1.9.1-.1A1.7 1.7 0 0 0 7.7 15 1.7 1.7 0 0 0 6 14H5.9v-2.7H6a1.7 1.7 0 0 0 1.7-1 1.7 1.7 0 0 0-.3-1.9l-.1-.1 1.9-1.9.1.1a1.7 1.7 0 0 0 1.9.3 1.7 1.7 0 0 0 1-1.6v-.1h2.7v.1a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1 1.9 1.9-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.1V14h-.1a1.7 1.7 0 0 0-1.6 1z']],
        'sliders-horizontal': [['path','M4 7h16'],['path','M4 17h16'],['circle','9 7 2'],['circle','15 17 2']],
        save: [['path','M5 3h12l3 3v15H4V3z'],['path','M8 3v6h8V3'],['path','M8 21v-6h8v6']],
        'rotate-ccw': [['path','M3 12a9 9 0 1 0 3-6.7'],['path','M3 4v5h5']],
        plus: [['path','M12 5v14'],['path','M5 12h14']],
        'music-2': [['path','M9 18V5l10-2v13'],['circle','6 18 3'],['circle','16 16 3']],
        'user-round': [['circle','12 7 4'],['path','M18 20a6 6 0 0 0-12 0']],
        'disc-3': [['circle','12 12 9'],['circle','12 12 1'],['path','M15.5 8.5 12 12']],
        'hard-drive': [['path','M4 5h16a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z'],['path','M6 15h.01M10 15h.01M14 15h.01']],
        'pencil-line': [['path','M12 20h9'],['path','M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z']],
        'square-pen': [['path','M12 20h9'],['path','M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z']],
        'log-out': [['path','m10 17 5-5-5-5'],['path','M15 12H3'],['path','M21 19V5a2 2 0 0 0-2-2h-5']],
        'grip-vertical': [['circle','9 5 1'],['circle','15 5 1'],['circle','9 12 1'],['circle','15 12 1'],['circle','9 19 1'],['circle','15 19 1']],
        'trash-2': [['path','M3 6h18'],['path','M8 6V4h8v2'],['path','M19 6l-1 14H6L5 6'],['path','M10 11v5'],['path','M14 11v5']],
        'x': [['path','M18 6 6 18'],['path','m6 6 12 12']],
        'arrow-left': [['path','m12 19-7-7 7-7'],['path','M5 12h14']],
        'clock-3': [['circle','12 12 9'],['path','M12 7v5l3 2']],
        'circle-check': [['circle','12 12 9'],['path','m9 12 2 2 4-4']],
        copy: [['rect','6 6 12 12'],['path','M9 3h9a3 3 0 0 1 3 3v9']],
        tag: [['path','M20 13 13 20 4 11V4h7z'],['circle','8 8 1']],
        'image-off': [['path','m3 3 18 18'],['path','M8.5 8.5a2 2 0 1 0 0 4'],['path','M21 15l-4-4-4 4'],['path','M3 15l4-4']],
        'volume-x': [['path','M11 5 6 9H3v6h3l5 4z'],['path','m19 9-6 6'],['path','m13 9 6 6']],
        'search-x': [['circle','11 11 7'],['path','m20 20-4-4'],['path','m8.5 8.5 5 5'],['path','m13.5 8.5-5 5']],

        'refresh-cw': [['path','M20 11a8 8 0 0 0-14.9-4'],['path','M4 5v4h4'],['path','M4 13a8 8 0 0 0 14.9 4'],['path','M20 19v-4h-4']],
        broom: [['path','m3 21 9-9'],['path','m14 3 7 7'],['path','m16 3 5 5']],
        'volume-2': [['path','M11 5 6 9H3v6h3l5 4z'],['path','M15.5 8.5a5 5 0 0 1 0 7'],['path','M18.5 5.5a9 9 0 0 1 0 13']],
        play: [['path','m8 5 11 7-11 7z']],
        pause: [['path','M8 5v14'],['path','M16 5v14']],
        'skip-back': [['path','M19 20 9 12l10-8v16'],['path','M5 19V5']],
        'skip-forward': [['path','m5 4 10 8-10 8V4'],['path','M19 5v14']],
        shuffle: [['path','M3 6h3c3 0 4 6 7 6h8'],['path','m18 9 3 3-3 3'],['path','M3 18h3c3 0 4-6 7-6h2'],['path','m18 3 3 3-3 3']],
        'list-music': [['path','M21 15V6'],['path','M18 8h3'],['path','M18 12h3'],['path','M18 16h3'],['circle','6 18 3'],['path','M9 18V6l9-2']],
        'monitor-smartphone': [['rect','3 4 12 11'],['path','M7 19h4'],['path','M9 15v4'],['rect','17 9 4 10']],
        radio: [['circle','12 12 2'],['path','M16.2 7.8a6 6 0 0 1 0 8.4'],['path','M7.8 16.2a6 6 0 0 1 0-8.4']],
        'copy-plus': [['rect','8 8 12 12'],['path','M4 16V4h12'],['path','M14 14h6'],['path','M17 11v6']],
        'check-circle-2': [['path','m9 12 2 2 4-4'],['circle','12 12 9']],
        'wifi-off': [['path','M3 3l18 18'],['path','M10.5 5.4A9.4 9.4 0 0 1 21 12.5'],['path','M3 9.8A9.2 9.2 0 0 1 6 7.1'],['path','M8.5 16.5a5 5 0 0 1 7 0'],['path','M12 20h.01']],
        monitor: [['rect','3 4 18 12'],['path','M8 20h8'],['path','M12 16v4']],
        smartphone: [['rect','7 2 10 20'],['path','M11 18h2']],
        tablet: [['rect','5 2 14 20'],['path','M11 18h2']],
        tv: [['rect','2 5 20 14'],['path','M8 21h8'],['path','M12 19v2']],
    };
    paths["bar-chart-3"] = [['path','M4 20V10'],['path','M10 20V4'],['path','M16 20v-7'],['path','M22 20H2']];
    paths["database"] = [['path','M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3'],['path','M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6'],['path','M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6']];
    paths["folder-open"] = [['path','M3 7.5A1.5 1.5 0 0 1 4.5 6h5l2 2h8A1.5 1.5 0 0 1 21 9.5l-1.2 8A1.5 1.5 0 0 1 18.3 19H5.2a1.5 1.5 0 0 1-1.5-1.3z'],['path','M3.5 10h17']];
    paths["layers-2"] = [['path','m12 2 9 5-9 5-9-5z'],['path','m3 12 9 5 9-5'],['path','m3 17 9 5 9-5']];
    paths["ellipsis"] = [['circle','5 12 1'],['circle','12 12 1'],['circle','19 12 1']];
    paths["list-plus"] = [['path','M8 6h13'],['path','M8 12h13'],['path','M8 18h5'],['path','M3 6h.01'],['path','M3 12h.01'],['path','M18 18h3'],['path','M18.5 15.5v5']];
    paths["sparkles"] = [['path','m12 3-1.1 3.2L7.5 7.3l3.4 1.1L12 12l1.1-3.6 3.4-1.1-3.4-1.1z'],['path','m19 12-.7 2.3-2.3.7 2.3.7.7 2.3.7-2.3 2.3-.7-2.3-.7z'],['path','m5 14-.6 1.9-1.9.6 1.9.6.6 1.9.6-1.9 1.9-.6-1.9-.6z']];
    paths["settings-2"] = paths.settings;
    paths["chevron-up"] = [['path','m18 15-6-6-6 6']];
    paths["circle-alert"] = [['circle','12 12 9'],['path','M12 8v5'],['path','M12 16h.01']];
    paths["download-cloud"] = [['path','M12 3v11'],['path','m7 10 5 5 5-5'],['path','M5 21h14'],['path','M4 16a4 4 0 0 1 1-7.9A6 6 0 0 1 17 7a5 5 0 0 1 1 9']];
    const ns = 'http://www.w3.org/2000/svg';
    document.querySelectorAll('[data-lucide]').forEach(el => {
        const name = el.getAttribute('data-lucide') || '';
        const defs = paths[name];
        if (!defs) return;
        const svg = document.createElementNS(ns, 'svg');
        svg.setAttribute('viewBox','0 0 24 24'); svg.setAttribute('width','18'); svg.setAttribute('height','18'); svg.setAttribute('fill','none'); svg.setAttribute('stroke','currentColor');
        svg.setAttribute('stroke-width','2'); svg.setAttribute('stroke-linecap','round'); svg.setAttribute('stroke-linejoin','round'); svg.setAttribute('aria-hidden','true'); svg.classList.add('xrob-icon');
        defs.forEach(([kind, value]) => {
            const node = document.createElementNS(ns, kind);
            const parts = String(value).trim().split(/\s+/);
            if (kind === 'circle') {
                const [cx,cy,r] = parts; node.setAttribute('cx',cx); node.setAttribute('cy',cy); node.setAttribute('r',r);
            } else if (kind === 'rect') {
                const [x,y,width,height,rx] = parts; node.setAttribute('x',x); node.setAttribute('y',y); node.setAttribute('width',width); node.setAttribute('height',height); if (rx) node.setAttribute('rx',rx);
            } else if (kind === 'ellipse') {
                const [cx,cy,rx,ry] = parts; node.setAttribute('cx',cx); node.setAttribute('cy',cy); node.setAttribute('rx',rx); node.setAttribute('ry',ry);
            } else {
                node.setAttribute('d', value);
            }
            svg.appendChild(node);
        });
        el.replaceWith(svg);
    });
}

let globalErrorHandlersInstalled = false;
function installGlobalErrorHandlers() {
    if (globalErrorHandlersInstalled) return;
    globalErrorHandlersInstalled = true;
    window.addEventListener("error", event => {
        reportAppError(event?.error || new Error(event?.message || "Unhandled browser error"), {scope:"window", action:"error", source: event?.filename || ""});
    });
    window.addEventListener("unhandledrejection", event => {
        reportAppError(event?.reason || new Error("Unhandled promise rejection"), {scope:"window", action:"unhandledrejection"});
    });
}
installGlobalErrorHandlers();

async function checkWebAuth() {
    try { const r=await apiFetch("api/auth/status",{cache:"no-store"}); if(!r.ok) return false; const d=await r.json(); return !!d.authenticated; } catch (_) { return false; }
}

function showAuthenticatedApp() { appState.auth.authenticated = true; emitAppEvent("auth:authenticated", {}); document.getElementById("login-screen")?.classList.add("hidden"); const shell=document.getElementById("app-shell"); if(shell) shell.hidden=false; renderLocalIcons(); }

async function handleLoginSubmit(e){
    e.preventDefault();
    const error=document.getElementById("loginError");
    const btn=document.querySelector(".login-submit");
    if(error) error.textContent="";
    const body={username:String(document.getElementById("loginUsername")?.value||"").trim(),password:document.getElementById("loginPassword")?.value||""};
    storageSet("xrob_music_login_user", body.username);
    if(btn){btn.disabled=true; btn.dataset.originalText=btn.textContent; btn.textContent="Signing in…";}
    try{const r=await apiFetch("api/auth/login",{method:"POST",headers:{"Content-Type":"application/json"},credentials:"same-origin",body:JSON.stringify(body)}); const d=await r.json().catch(()=>({})); if(!r.ok) throw new Error(d.detail||"Sign in failed"); document.getElementById("loginPassword").value=""; showAuthenticatedApp(); await startAppAfterAuth(); }catch(err){if(error)error.textContent=err.message||"Sign in failed";} finally{if(btn){btn.disabled=false;btn.textContent=btn.dataset.originalText||"Sign in";}}
}


async function logoutWebAuth(){ appState.auth.authenticated=false; emitAppEvent("auth:logout", {}); await apiFetch("api/auth/logout",{method:"POST"}).catch(()=>{}); location.reload(); }

async function initializeApp() {

    renderLocalIcons();
    const savedLoginUser = storageGet("xrob_music_login_user");
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
        storageGet(
            "xrob_music_theme"
        ) || "dark"
    );


    // The player is a persistent app surface, not something that only appears after playback.
    // Keep it visible at startup with its existing empty-state labels.
    if (player) player.style.display = "grid";

    await initPlayerSync();
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
        appState.library.view = button.dataset.libraryView || "tracks";
        appState.library.selectedArtistId = null;
        appState.library.selectedAlbumId = null;
        syncLibraryUiState();
        document.querySelectorAll(".library-tab").forEach(item => item.classList.toggle("active", item === button));

        // Statistics is a dedicated Library view: never leave the catalog list visible.
        const list = document.getElementById("libraryList");
        const dashboard = document.getElementById("libraryStatsDashboard");
        const isStatistics = appState.library.view === "statistics";
        if (dashboard) dashboard.hidden = !isStatistics;
        if (list) list.hidden = isStatistics;

        renderLibraryView();
    }));

    const cached = loadLibraryCache();
    if (cached) renderLibraryView();
    // Fast first paint: cached catalog data may be shown before the fresh catalog arrives.
    // Poll briefly for the background metadata warmup to finish, then refresh once.
    const startupJobs = [refreshLibraryCache(), loadSettings(), loadSongEditor(), pollTasks(true), loadStats(), loadHome()];
    await Promise.allSettled(startupJobs);
    if (appState.library.files.length) renderLibraryView();
    let libraryWarmupChecks = 0;
    const warmupTimer = setInterval(async () => {
        libraryWarmupChecks += 1;
        if (libraryWarmupChecks > 30) return clearInterval(warmupTimer);
        try {
            const r = await apiFetch('api/library', {cache:'no-store'});
            if (!r.ok) return;
            const d = await r.json();
            if (d.library_state && d.library_state !== "scanning") {
                clearInterval(warmupTimer);
                appState.library.files = d.files || [];
                appState.library.playbackQueue = appState.library.files;
                appState.library.artists = d.artists || appState.library.artists;
                appState.library.albums = d.albums || appState.library.albums;
                saveLibraryCache();
                renderLibraryView();
                scheduleStatsRefresh(120);
                loadSongEditor();
            }
        } catch (error) { reportAppError(error, {scope:"library", action:"warmup"}); }
    }, 1000);
    handleHash();


    initWebSocket();


    installEnhancedFeatures();
    installAppFeatures();
    installLifecycleHandlers();
    installMediaSession();
    document.getElementById("errorsButton")?.addEventListener("click",async()=>{const d=await apiFetchJson("api/errors",{}, {scope:"errors",action:"load"});document.getElementById("errorsContent").innerHTML=(d.errors||[]).length?`<pre>${escapeHtml(JSON.stringify(d.errors,null,2))}</pre>`:'<div class="queue-empty">No errors recorded.</div>';document.getElementById("errors-modal").hidden=false;});
    document.getElementById("errorsClose")?.addEventListener("click",()=>document.getElementById("errors-modal").hidden=true);
    restorePlayerState();


    if (taskPollTimer) window.clearInterval(taskPollTimer);
    taskPollTimer = window.setInterval(() => {
        if (!socket || socket.readyState !== WebSocket.OPEN) pollTasks(true).catch(error => reportAppError(error, {scope:"downloads", action:"poll-fallback"}));
    }, 5000);

    if (statsPollTimer) window.clearInterval(statsPollTimer);
    statsPollTimer = window.setInterval(() => {
        if (!document.hidden && !statsRefreshTimer) loadStats().catch(error => reportAppError(error, {scope:"stats", action:"safety-refresh"}));
    }, 120000);
    scheduleStatsRefresh(0);
}



/* ============================================================
   ENHANCED PLAYER / LIBRARY FEATURES
   ============================================================ */

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
    const box = document.getElementById("queueList");
    if (!box) return;
    box.innerHTML = "";
    appState.player.queue = [...appState.player.queue];
    appState.player.queueIndex = appState.player.queueIndex;
    if (!appState.player.queue.length) {
        box.innerHTML = '<div class="queue-empty"><i data-lucide="list-music" aria-hidden="true"></i><span>Queue is empty</span></div>';
        renderLocalIcons();
        return;
    }
    appState.player.queue.forEach((t, i) => {
        const row = document.createElement("div");
        row.className = `queue-row ${i === appState.player.queueIndex ? "current" : ""}`;
        row.draggable = true;
        row.dataset.index = String(i);
        row.innerHTML = `<span class="queue-drag" aria-hidden="true"><i data-lucide="grip-vertical"></i></span><img src="${escapeHtml(t.cover || "")}" alt=""><div class="queue-row-info"><strong>${escapeHtml(t.title || t.name || "Unknown")}</strong><span>${escapeHtml(t.artist || "Unknown Artist")}</span></div><button class="queue-next btn-refresh" title="Play next">Next</button><button class="queue-remove icon-btn" title="Remove" aria-label="Remove track"><i data-lucide="x" aria-hidden="true"></i></button>`;
        const nextButton = row.querySelector(".queue-next");
        const removeButton = row.querySelector(".queue-remove");
        if (i === appState.player.queueIndex) { removeButton.disabled = true; nextButton.disabled = true; }
        nextButton.onclick = e => {
            e.stopPropagation();
            if (i === appState.player.queueIndex || i === appState.player.queueIndex + 1) return;
            const q = [...appState.player.queue]; const [item] = q.splice(i, 1);
            const currentId = currentSongId();
            const currentPos = q.findIndex(x => (x.id || x.name) === currentId);
            q.splice(Math.min(currentPos + 1, q.length), 0, item);
            syncLibraryQueue(q, q.findIndex(x => (x.id || x.name) === currentId));
            renderEnhancedQueue();
        };
        removeButton.onclick = e => {
            e.stopPropagation();
            if (i === appState.player.queueIndex) return showToast("Current track stays in the queue while playing");
            const q = [...appState.player.queue]; q.splice(i, 1);
            const currentId = currentSongId();
            syncLibraryQueue(q, q.findIndex(x => (x.id || x.name) === currentId));
            renderEnhancedQueue();
        };
        row.addEventListener("dblclick", () => playLibraryTrack(i));
        row.addEventListener("click", e => { if (e.target.closest("button")) return; playLibraryTrack(i); });
        row.addEventListener("dragstart", e => { e.dataTransfer.setData("text/plain", String(i)); e.dataTransfer.effectAllowed = "move"; });
        row.addEventListener("dragover", e => e.preventDefault());
        row.addEventListener("drop", e => {
            e.preventDefault();
            const from = Number(e.dataTransfer.getData("text/plain")); const to = Number(row.dataset.index);
            if (!Number.isInteger(from) || !Number.isInteger(to) || from === to) return;
            const currentId = currentSongId(); const q = [...appState.player.queue]; const [item] = q.splice(from,1); q.splice(to,0,item);
            syncLibraryQueue(q, q.findIndex(x => (x.id || x.name) === currentId)); renderEnhancedQueue();
        });
        box.appendChild(row);
    });
    renderLocalIcons();
    updateQueueIndicators();
}

function setEnhancedQueue(queue, index = 0) {
    syncLibraryQueue(queue, index);
    renderEnhancedQueue();
    updateQueueIndicators();
}

function updateQueueIndicators(){
    const total = Array.isArray(appState.player.queue) ? appState.player.queue.length : 0;
    const nextCount = appState.player.queueIndex >= 0 ? Math.max(0, total - appState.player.queueIndex - 1) : total;
    ["topbarQueueCount","mobQueueTrackCount"].forEach(id=>{ const el=document.getElementById(id); if(el) el.textContent=String(nextCount); });
    ["topbarQueueBtn","gp-queue-btn"].forEach(id=>{ const el=document.getElementById(id); if(el) el.setAttribute("aria-label", nextCount ? `Open queue · ${nextCount} up next` : "Open queue"); });
}

function openQueueDrawer(){
    const drawer = document.getElementById("queue-drawer");
    if (!drawer) return;
    appState.ui.queueOpen = true;
    emitAppEvent("ui:drawer", {drawer:"queue", open:true});
    drawer.hidden = false;
    renderEnhancedQueue();
    updateQueueIndicators();
    applyRepeatLabel();
}

function closeQueueDrawer(){
    const drawer = document.getElementById("queue-drawer");
    if (drawer) drawer.hidden = true;
    appState.ui.queueOpen = false;
    emitAppEvent("ui:drawer", {drawer:"queue", open:false});
}

function openDownloadsDrawer(){
    const drawer = document.getElementById("downloads-drawer");
    if (!drawer) return;
    appState.ui.downloadsOpen = true;
    emitAppEvent("ui:drawer", {drawer:"downloads", open:true});
    drawer.hidden = false;
    loadDownloads().catch(() => {});
    renderLocalIcons();
}

function closeDownloadsDrawer(){
    const drawer = document.getElementById("downloads-drawer");
    if (drawer) drawer.hidden = true;
    appState.ui.downloadsOpen = false;
    emitAppEvent("ui:drawer", {drawer:"downloads", open:false});
}

async function saveQueueAsPlaylist(){ if(!appState.player.queue.length){showToast("Queue is empty");return;} const name=prompt("Playlist name", "My Queue"); if(!name)return; const r=await apiFetch("api/playlists",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name,song_ids:appState.player.queue.map(x=>x.id).filter(Boolean)})}); if(r.ok) showToast("✅ Playlist saved"); else showToast("❌ Could not save playlist"); }

async function renderLibraryCollections(mode){
    const list=document.getElementById("libraryList"); if(!list)return;
    list.innerHTML='<div class="downloads-empty"><div class="empty-title">Loading…</div></div>';
    let endpoint=mode==="recent"?"recent":mode==="most"?"most_played":null;
    if(!endpoint)return;
    let d; try { d = await apiFetchJson("api/library/recent-most",{cache:"no-store"},{scope:"library",action:"recent-most"}); } catch (_) { renderEmpty(list,"circle-alert","Could not load history","Try again after checking the library."); return; } const rows=d[endpoint]||[]; list.innerHTML="";
    if(!rows.length){renderEmpty(list,"clock-3",mode==="recent"?"Nothing recently played":"No play history yet","Play some tracks to build this list.");return;}
    rows.forEach((t, rank)=>{ const f={...t,name:t.title,stream:t.stream,cover:t.cover,play_count:Number(t.plays||0)}; const card=createTrackCard(f,rows); card.classList.add("collection-track"); card.dataset.rank=String(rank+1); list.appendChild(card); });
    renderLocalIcons();
}

async function loadPlaylistsView(){
    const list=document.getElementById("libraryList"); if(!list)return; const r=await apiFetch("api/playlists",{cache:"no-store"}); const rows=await r.json(); list.innerHTML="";
    const head=document.createElement("div"); head.className="catalog-detail-header"; head.innerHTML='<div><h3>Playlists</h3><p>Create manual or smart playlists.</p></div><button class="btn-preview" id="newPlaylistBtn"><i data-lucide="plus" aria-hidden="true"></i> New playlist</button>'; list.appendChild(head);
    rows.forEach(p=>{const c=document.createElement("article");c.className="catalog-card";c.innerHTML=`<div><strong>${escapeHtml(p.name)}</strong><span>${p.kind==='smart'?'Smart':'Manual'} · ${p.song_count} tracks</span></div><div class="btn-group"><button class="btn-preview"><i data-lucide="play" aria-hidden="true"></i> Play</button><button class="btn-danger"><i data-lucide="trash-2" aria-hidden="true"></i> Delete</button></div>`;c.querySelector('.btn-preview').onclick=async()=>{const rr=await apiFetch(`api/playlists/${encodeURIComponent(p.id)}`);const full=await rr.json();setEnhancedQueue(full.tracks,0);playLibraryTrack(0);};c.querySelector('.btn-danger').onclick=async()=>{if(confirm(`Delete ${p.name}?`)){await apiFetch(`api/playlists/${encodeURIComponent(p.id)}`,{method:'DELETE'});loadPlaylistsView();}};list.appendChild(c);});
    renderLocalIcons();
    const newPlaylistButton = head.querySelector("#newPlaylistBtn");
    newPlaylistButton?.addEventListener("click", async () => {
        const name=prompt("Playlist name","New Playlist");
        if(!name) return;
        const kind=confirm("Make this a smart playlist?\nOK = smart, Cancel = manual") ? 'smart' : 'manual';
        let rules={};
        if(kind==='smart'){const genre=prompt("Genre rule (optional)","");const artist=prompt("Artist rule (optional)","");if(genre)rules.genre=genre;if(artist)rules.artist=artist;}
        const response = await apiFetch('api/playlists',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,kind,rules,song_ids:[]})});
        if (response.ok) loadPlaylistsView(); else showToast("❌ Could not create playlist");
    });
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
        list.innerHTML = `<div class="editor-empty"><div class="empty-icon"><i data-lucide="${q ? 'search-x' : 'circle-check'}" aria-hidden="true"></i></div><div class="empty-title">${q ? "No matching tracks" : "All caught up"}</div><div>${q ? "Try another search." : "New downloads will appear here automatically."}</div></div>`;
        renderLocalIcons();
        return;
    }
    tracks.forEach(track => {
        const card = document.createElement("article");
        card.className = "song-editor-card";
        card.dataset.songId = track.id;
        card.innerHTML = `<img class="song-editor-art" src="${escapeHtml(track.cover || "")}" alt="" loading="lazy"><div class="song-editor-info"><div class="song-editor-title">${escapeHtml(track.title || track.name || "Unknown Track")}</div><div class="song-editor-artist">${escapeHtml(track.artist || "Unknown Artist")} <span aria-hidden="true">•</span> ${escapeHtml(track.album || "Unknown Album")}</div><div class="song-editor-file">${escapeHtml(track.name || "")}</div></div><div class="song-editor-actions"><button class="btn-preview editor-edit" type="button"><i data-lucide="square-pen" aria-hidden="true"></i> Edit</button><button class="btn-secondary editor-skip" type="button">Skip</button></div>`;
        card.querySelector(".editor-edit").onclick = () => openMetadataEditor(track);
        card.querySelector(".editor-skip").onclick = async () => {
            const r = await apiFetch(`api/song-editor/${encodeURIComponent(track.id)}/skip`, {method:"POST"});
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
        const r = await apiFetch("api/song-editor", {cache:"no-store"});
        if (!r.ok) throw new Error("Could not load Songs Editor");
        const d = await r.json();
        songEditorTracks = Array.isArray(d.tracks) ? d.tracks : [];
        document.getElementById("songEditorCount")?.replaceChildren(String(songEditorTracks.length));
        document.getElementById("songEditorBadge")?.replaceChildren(String(songEditorTracks.length));
        const select = document.getElementById("songEditorImportSelect");
        if (select) {
            const existing = select.value;
            select.innerHTML = '<option value="">Choose an edited library track…</option>';
            const editedTracks = Array.isArray(d.recently_edited_tracks)
                ? d.recently_edited_tracks
                : (Array.isArray(d.edited_tracks) ? d.edited_tracks : []);
            editedTracks.forEach(t => {
                const o=document.createElement('option');
                o.value=t.id||'';
                o.textContent=`${t.title||t.name||'Unknown Track'} — ${t.artist||'Unknown Artist'}`;
                select.appendChild(o);
            });
            if(existing && [...select.options].some(o=>o.value===existing)) select.value=existing;
            // Keep the selector interactive even when there are currently no edited tracks.
            // The previous disabled state made the control look broken and prevented the native
            // dropdown from opening during startup/warmup refreshes.
            select.disabled = false;
            select.title = editedTracks.length ? 'Choose a previously edited library track to reopen it' : 'No previously edited tracks yet';
        }
        renderSongEditorTracks(document.getElementById("songEditorSearch")?.value || "");
    } catch (err) {
        list.innerHTML = `<div class="editor-empty">${escapeHtml(err.message || "Could not load editor")}</div>`;
    }
}


function formatBytes(bytes) { const n=Math.max(0,Number(bytes)||0); if(n<1024) return `${Math.round(n)} B`; if(n<1024**2) return `${(n/1024).toFixed(1)} KB`; if(n<1024**3) return `${(n/1024**2).toFixed(1)} MB`; return `${(n/1024**3).toFixed(2)} GB`; }

function formatLongDuration(seconds) {
    const total = Math.max(0, Math.round(Number(seconds) || 0));
    const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60);
    return h ? `${h}h ${m}m` : `${m}m`;
}

function renderDashboardRows(id, rows) {
    const el = document.getElementById(id); if (!el) return;
    el.innerHTML = '';
    const data = Array.isArray(rows) ? rows.slice(0, 8) : [];
    if (!data.length) { el.innerHTML = '<div class="queue-empty">No data yet</div>'; return; }
    const max = Math.max(1, ...data.map(x => Number(x.count || x.plays || 0)));
    data.forEach(x => { const row = document.createElement('div'); row.innerHTML = `<div class="dashboard-row"><span>${escapeHtml(x.name || 'Unknown')}</span><strong>${Number(x.count ?? x.plays ?? 0)}</strong></div><div class="dashboard-bar"><i style="width:${Math.max(3, Math.round((Number(x.count ?? x.plays ?? 0) / max) * 100))}%"></i></div>`; el.appendChild(row); });
}

async function loadDetailedLibraryStats() {
    try {
        const r = await apiFetch('api/library/statistics', {cache:'no-store'}); if (!r.ok) return;
        const d = await r.json();
        const set = (id, value) => document.getElementById(id)?.replaceChildren(String(value));
        set('detailStatDuration', formatLongDuration(d.total_duration));
        set('detailStatAvg', formatSeconds(d.average_duration));
        set('detailStatPlayed', d.unique_played || 0);
        set('detailStat7d', d.recent_7d_plays || 0);
        set('detailStatListening', formatLongDuration(d.listened_seconds));
        set('detailStatFormats', d.formats?.length || 0);
        set('detailStatSize', formatBytes(Number(d.total_bytes || 0)));
        set('detailStatBitrate', `${Math.round(Number(d.average_bitrate || 0))} kbps`);
        renderDashboardRows('detailTopArtists', d.top_artists);
        renderDashboardRows('detailGenres', d.genres_breakdown);
        renderDashboardRows('detailFormats', d.formats);
        renderDashboardRows('detailBitrates', d.bitrates);
        renderDashboardRows('detailYears', d.years);
        renderDashboardRows('detailSampleRates', d.sample_rates);
        renderLocalIcons();
        loadLibraryIntelligence();
    } catch (error) { reportAppError(error, {scope:"library-stats"}); }
}

async function loadLibraryIntelligence() {
    try {
        const r = await apiFetch('api/library/intelligence', {cache:'no-store'});
        if (!r.ok) return;
        const d = await r.json();
        const set = (id, value) => document.getElementById(id)?.replaceChildren(String(value));
        set('intelDuplicateCount', d.duplicate_groups?.length || 0);
        set('intelMissingMeta', d.missing_metadata_count || 0);
        set('intelMissingArtwork', d.missing_artwork_count || 0);
        set('intelReplayGain', d.replaygain_missing_count || 0);
        const list = document.getElementById('libraryIntelligenceList');
        if (!list) return;
        const items = [];
        (d.duplicate_groups || []).slice(0, 4).forEach(group => items.push({icon:'copy', title:`Duplicate: ${group.files?.[0]?.title || 'Untitled'}`, detail:`${group.count} matching files`}));
        (d.missing_metadata || []).slice(0, 4).forEach(item => items.push({icon:'tag', title:`Metadata: ${item.title}`, detail:`Missing ${item.issues.join(', ')}`}));
        (d.missing_artwork || []).slice(0, 4).forEach(item => items.push({icon:'image-off', title:`Artwork: ${item.title}`, detail:item.artist || 'Unknown Artist'}));
        (d.replaygain_missing || []).slice(0, 4).forEach(item => items.push({icon:'volume-x', title:`ReplayGain: ${item.title}`, detail:item.artist || 'Unknown Artist'}));
        list.innerHTML = items.length ? items.map(item => `<div class="library-intel-row"><i data-lucide="${escapeHtml(item.icon)}" aria-hidden="true"></i><div><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.detail)}</span></div></div>`).join('') : '<div class="queue-empty">Library looks clean.</div>';
        renderLocalIcons();
    } catch (error) { reportAppError(error, {scope:"library-intelligence"}); }
}

function installDailyMixSwipe() {
    const row = document.getElementById("dailyMixTracks");
    if (!row || row.dataset.swipeBound === "true") return;
    row.dataset.swipeBound = "true";
    let pointerId = null;
    let startX = 0;
    let startY = 0;
    let lastX = 0;
    let lastTime = 0;
    let velocity = 0;
    let dragging = false;
    let suppressClick = false;
    let momentumFrame = 0;
    const INTENT_THRESHOLD = 8;
    const MAX_VELOCITY = 2.8;
    const FRICTION = 0.93;

    const clampScroll = value => Math.max(0, Math.min(Math.max(0, row.scrollWidth - row.clientWidth), value));
    const stopMomentum = () => {
        if (momentumFrame) cancelAnimationFrame(momentumFrame);
        momentumFrame = 0;
    };
    let captureTarget = null;
    const releasePointer = () => {
        if (pointerId !== null && captureTarget?.hasPointerCapture?.(pointerId)) {
            try { captureTarget.releasePointerCapture(pointerId); } catch (_) {}
        }
        captureTarget = null;
    };
    const resetGesture = () => {
        releasePointer();
        pointerId = null;
        dragging = false;
        velocity = 0;
        row.classList.remove("is-swipe-dragging");
    };
    const runMomentum = () => {
        if (Math.abs(velocity) < 0.03) {
            stopMomentum();
            return;
        }
        row.scrollLeft = clampScroll(row.scrollLeft - velocity * 28);
        const atStart = row.scrollLeft <= 0 && velocity > 0;
        const atEnd = row.scrollLeft >= row.scrollWidth - row.clientWidth - 1 && velocity < 0;
        if (atStart || atEnd) {
            stopMomentum();
            return;
        }
        velocity *= FRICTION;
        momentumFrame = requestAnimationFrame(runMomentum);
    };

    row.addEventListener("pointerdown", event => {
        if (!event.isPrimary || (event.pointerType === "mouse" && event.button !== 0)) return;
        if (!event.target.closest?.(".daily-mix-track")) return;
        stopMomentum();
        pointerId = event.pointerId;
        startX = lastX = event.clientX;
        startY = event.clientY;
        lastTime = performance.now();
        velocity = 0;
        dragging = false;
        suppressClick = false;
        captureTarget = event.target.closest?.(".daily-mix-track") || null;
        try { captureTarget?.setPointerCapture(pointerId); } catch (_) {}
    }, true);

    row.addEventListener("pointermove", event => {
        if (event.pointerId !== pointerId) return;
        const dx = event.clientX - startX;
        const dy = event.clientY - startY;
        if (!dragging) {
            if (Math.hypot(dx, dy) < INTENT_THRESHOLD) return;
            if (Math.abs(dx) <= Math.abs(dy) * 1.15) {
                resetGesture();
                return;
            }
            dragging = true;
            suppressClick = true;
            row.classList.add("is-swipe-dragging");
        }
        const now = performance.now();
        const dt = Math.max(8, now - lastTime);
        const deltaX = event.clientX - lastX;
        velocity = Math.max(-MAX_VELOCITY, Math.min(MAX_VELOCITY, deltaX / dt));
        lastX = event.clientX;
        lastTime = now;
        row.scrollLeft = clampScroll(row.scrollLeft - deltaX);
        if (event.cancelable) event.preventDefault();
    }, { passive: false });

    row.addEventListener("pointerup", event => {
        if (event.pointerId !== pointerId) return;
        const wasDragging = dragging;
        releasePointer();
        pointerId = null;
        dragging = false;
        row.classList.remove("is-swipe-dragging");
        if (wasDragging) runMomentum();
        else suppressClick = false;
    }, true);

    row.addEventListener("pointercancel", event => {
        if (event.pointerId !== pointerId) return;
        resetGesture();
        suppressClick = false;
    }, true);

    row.addEventListener("lostpointercapture", event => {
        if (pointerId === null || (captureTarget && event.target !== captureTarget)) return;
        pointerId = null;
        captureTarget = null;
        dragging = false;
        row.classList.remove("is-swipe-dragging");
    }, true);

    row.addEventListener("scroll", () => {
        const state = {
            tracks: dailyMixTracks,
            variant: dailyMixVariant,
            title: document.getElementById("dailyMixTitle")?.textContent || "Daily Mix",
            subtitle: document.getElementById("dailyMixSubtitle")?.textContent || "Personalized from your listening",
            scrollLeft: row.scrollLeft,
            date: getLocalDateKey(),
            generation: dailyMixGeneration,
        };
        try { storageSet(DAILY_MIX_STATE_KEY, JSON.stringify({ ...state, trackCount: dailyMixTracks.length, savedAt: Date.now() })); } catch (_) {}
    }, { passive: true });

    row.addEventListener("click", event => {
        const clickedTrack = event.target.closest?.(".daily-mix-track");
        if (!clickedTrack || !suppressClick) return;
        suppressClick = false;
        event.preventDefault();
        event.stopPropagation();
        event.stopImmediatePropagation();
    }, true);
}

function renderDailyMixCards(title = "Daily Mix", subtitle = "Personalized from your listening") {
    const row = document.getElementById('dailyMixTracks');
    if (!row) return;
    document.getElementById('dailyMixTitle')?.replaceChildren(title);
    document.getElementById('dailyMixSubtitle')?.replaceChildren(subtitle);
    row.innerHTML = '';
    if (!dailyMixTracks.length) {
        row.innerHTML = '<div class="daily-mix-empty">Play some music to start building your Daily Mix.</div>';
        return;
    }
    dailyMixTracks.forEach((track, index) => {
        const card = document.createElement('button');
        card.type = 'button';
        card.className = 'daily-mix-track';
        card.innerHTML = `<img src="${escapeHtml(track.cover || '')}" alt="" loading="lazy"><strong>${escapeHtml(track.title || 'Unknown Track')}</strong><span>${escapeHtml(track.artist || 'Unknown Artist')}</span>`;
        card.addEventListener('click', () => {
            setEnhancedQueue(dailyMixTracks, index);
            appState.player.source = 'library';
            playLibraryTrack(index);
        });
        card.querySelector('img')?.addEventListener('error', e => e.currentTarget.removeAttribute('src'), {once:true});
        row.appendChild(card);
    });
    renderLocalIcons();
}

async function loadDailyMix(forceVariation = false) {
    const row = document.getElementById('dailyMixTracks'); if (!row) return;
    const requestId = ++dailyMixLoadSequence;
    let refreshToken = "";
    let excluded = [];
    try {
        if (forceVariation) {
            excluded = Array.from(new Set(dailyMixTracks.map(trackKey).filter(Boolean)));
            dailyMixVariant += 1;
            refreshToken = `${Date.now()}-${typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID().slice(0,8) : Math.random().toString(36).slice(2,10)}`;
            dailyMixGeneration = refreshToken;
            storageSet('xrob_daily_mix_variant', String(dailyMixVariant));
            storageSet('xrob_daily_mix_generation', dailyMixGeneration);
            dailyMixTracks = []; renderDailyMixCards('Daily Mix', 'Refreshing with a completely new set…');
            try { storageRemove(DAILY_MIX_STATE_KEY); } catch (_) {}
        }
        const params = new URLSearchParams({ variant: String(dailyMixVariant) });
        if (refreshToken) { params.set('refresh_token', refreshToken); if (excluded.length) params.set('exclude', excluded.join('|')); }
        const r = await apiFetch(`api/daily-mix?${params.toString()}`, {cache:'no-store'});
        if (requestId !== dailyMixLoadSequence) return;
        if (!r.ok) throw new Error('Daily Mix unavailable');
        const d = await r.json();
        if (requestId !== dailyMixLoadSequence) return;
        const nextTracks = Array.isArray(d.tracks) ? d.tracks : [];
        const oldIds = new Set(excluded);
        dailyMixTracks = forceVariation && oldIds.size ? nextTracks.filter(track => !oldIds.has(trackKey(track))) : nextTracks;
        dailyMixGeneration = String(d.generation || refreshToken || dailyMixGeneration || "");
        if (dailyMixGeneration) storageSet('xrob_daily_mix_generation', dailyMixGeneration);
        renderDailyMixCards(d.title || 'Daily Mix', d.subtitle || 'Personalized from your listening');
        const state = {tracks:dailyMixTracks,variant:dailyMixVariant,generation:dailyMixGeneration,title:d.title || 'Daily Mix',subtitle:d.subtitle || 'Personalized from your listening',scrollLeft:row.scrollLeft,date:d.date || new Date().toISOString().slice(0,10),savedAt:Date.now()};
        try { storageSet(DAILY_MIX_STATE_KEY, JSON.stringify(state)); } catch (_) {}
        if (!isRemotePlayerOwner()) schedulePlayerStateBroadcast(true);
    } catch (e) {
        if (requestId !== dailyMixLoadSequence) return;
        dailyMixTracks = [];
        renderDailyMixCards('Daily Mix', forceVariation ? 'Could not refresh Daily Mix.' : 'Daily Mix could not be loaded.');
    }
}

function installEnhancedFeatures(){
    loadEnhancedQueue(); loadEnhancedPositions(); applyRepeatLabel();
    document.getElementById("libraryStatsRefresh")?.addEventListener("click", loadDetailedLibraryStats);
    document.getElementById("dailyMixRefresh")?.addEventListener("click", () => loadDailyMix(true));
    document.getElementById("dailyMixPlay")?.addEventListener("click", () => { if (!dailyMixTracks.length) return; setEnhancedQueue(dailyMixTracks, 0); appState.player.source="library"; playLibraryTrack(0); });
    loadDetailedLibraryStats();
    const restoredDailyMix = loadPersistedDailyMixState();
    if (!restoredDailyMix) loadDailyMix();
    installDailyMixSwipe();
    document.getElementById("gp-queue-btn")?.addEventListener("click", (event) => { event.preventDefault(); event.stopPropagation(); openQueueDrawer(); });
    document.getElementById("topbarQueueBtn")?.addEventListener("click", (event) => { event.preventDefault(); event.stopPropagation(); openQueueDrawer(); });
    document.getElementById("queueClose")?.addEventListener("click", (event) => { event.preventDefault(); event.stopPropagation(); closeQueueDrawer(); });
    document.getElementById("downloadsClose")?.addEventListener("click", (event) => { event.preventDefault(); event.stopPropagation(); closeDownloadsDrawer(); });
    document.getElementById("topbarDownloadsBtn")?.addEventListener("click", (event) => { event.preventDefault(); event.stopPropagation(); openDownloadsDrawer(); });
    // Drawers remain independent, but retain the familiar click-outside behavior.
    // Clicking inside one drawer never closes it; clicking outside a drawer closes
    // that drawer only, so Queue and Downloads never become coupled again.
    document.addEventListener("pointerdown", (event) => {
        const target = event.target;
        const queue = document.getElementById("queue-drawer");
        const downloads = document.getElementById("downloads-drawer");
        const queueButton = document.getElementById("gp-queue-btn");
        const downloadsButton = document.getElementById("topbarDownloadsBtn");
        if (queue && !queue.hidden && !queue.contains(target) && !queueButton?.contains(target)) {
            closeQueueDrawer();
        }
        if (downloads && !downloads.hidden && !downloads.contains(target) && !downloadsButton?.contains(target)) {
            closeDownloadsDrawer();
        }
    });
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") { closeDownloadsDrawer(); closeQueueDrawer(); }
    });
    document.getElementById("queueClear")?.addEventListener("click",()=>{
    if (appState.player.source === "library" && appState.player.queue.length && appState.player.queueIndex >= 0) {
        const current = appState.player.queue[appState.player.queueIndex];
        syncLibraryQueue(current ? [current] : [], 0);
    } else {
        syncLibraryQueue([], -1);
    }

    shuffleRestoreQueue = null;
    shuffleRestoreCurrentId = null;
    saveEnhancedQueue();
    renderEnhancedQueue();
}); document.getElementById("queueSave")?.addEventListener("click",saveQueueAsPlaylist); document.getElementById("queueRepeat")?.addEventListener("click",cycleRepeatMode); document.getElementById("queueSleep")?.addEventListener("click",cycleSleepTimer); document.getElementById("gp-sleep-btn")?.addEventListener("click",cycleSleepTimer); document.getElementById("gp-lyrics-btn")?.addEventListener("click",openLyricsPanel); document.getElementById("lyricsClose")?.addEventListener("click",()=>{const m=document.getElementById("lyrics-modal");if(m)m.hidden=true;if(lyricsAnimationFrame){cancelAnimationFrame(lyricsAnimationFrame);lyricsAnimationFrame=null;}}); updateSleepTimerUI(); if(sleepTimerRemainingMs() && !sleepTimerInterval) sleepTimerInterval=setInterval(()=>{if(!sleepTimerRemainingMs()){clearSleepTimer();if(isRemotePlayerOwner())sendPlayerCommand("pause");else audio?.pause();showToast("⏰ Sleep timer paused playback");}updateSleepTimerUI();},1000);
    document.getElementById("metadataClose")?.addEventListener("click",()=>document.getElementById("metadata-modal").hidden=true); document.getElementById("healthClose")?.addEventListener("click",()=>document.getElementById("health-modal").hidden=true);
    document.getElementById("metadataForm")?.addEventListener("submit",async e=>{e.preventDefault();const id=document.getElementById('metadataId').value;const body={id,title:document.getElementById('metadataTitle').value,artist:document.getElementById('metadataArtist').value,album:document.getElementById('metadataAlbum').value};const r=await apiFetch('api/library/metadata',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(r.ok){showToast('✅ Metadata saved and removed from editor');document.getElementById('metadata-modal').hidden=true;await refreshLibraryCache();renderLibraryView();await loadSongEditor();}else{const d=await r.json().catch(()=>({}));showToast('❌ '+(d.detail||'Metadata update failed'));}});
    document.getElementById("libraryFullScanButton")?.addEventListener("click",async()=>{
        const btn=document.getElementById("libraryFullScanButton"); if(btn) btn.disabled=true;
        showToast('⏳ Full metadata rebuild…');
        try { const r=await apiFetch('api/library/scan/full',{method:'POST'}); if(!r.ok) throw new Error('Full scan failed'); showToast('✅ Full scan complete'); await refreshLibraryCache(); await loadStats(); renderLibraryView(); }
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
        try{ const r=await apiFetch('api/song-editor/reset',{method:'POST'}); const d=await r.json().catch(()=>({})); if(!r.ok) throw new Error(d.detail||'Reset failed'); await loadSongEditor(); showToast(`✅ ${d.count||0} tracks added to editor`); }
        catch(err){ showToast('❌ '+(err.message||'Reset failed')); }
        finally{ if(btn) btn.disabled=false; }
    });
    document.getElementById("songEditorImport")?.addEventListener("click", async()=>{
        const pick=document.getElementById('songEditorImportSelect');
        if(!pick){ showToast('❌ Import selector unavailable'); return; }
        const id=pick.value; if(!id){ showToast('Select a track to import'); return; }
        const r=await apiFetch(`api/song-editor/${encodeURIComponent(id)}/import`,{method:'POST'});
        if(r.ok){ const label=pick.options[pick.selectedIndex]?.text||'Track'; showToast(`✅ ${label} added to editor`); await loadSongEditor(); }
        else { const d=await r.json().catch(()=>({})); showToast('❌ '+(d.detail||'Could not import track')); }
    });
    async function loadLibraryHealth() {
        const content = document.getElementById("healthContent");
        if (!content) return;
        content.innerHTML = '<div class="queue-empty">Checking library health…</div>';
        try {
            const [r, dr] = await Promise.all([
                apiFetch("api/library/health", {cache:"no-store"}),
                apiFetch("api/library/duplicates", {cache:"no-store"})
            ]);
            const d = await r.json().catch(() => ({}));
            const duplicateData = await dr.json().catch(() => ({}));
            if (!r.ok) throw new Error(d.detail || "Could not check library health");
            const duplicates = Array.isArray(duplicateData.duplicates) ? duplicateData.duplicates : [];
            const duplicateFiles = duplicates.reduce((sum, group) => sum + Math.max(0, Number(group.count || 0)), 0);
            content.innerHTML = `<div class="health-summary"><strong>Unreadable: ${d.counts?.unreadable || 0}</strong><strong>Bad tags: ${d.counts?.bad_tags || 0}</strong><strong>Missing artwork: ${d.counts?.missing_artwork || 0}</strong><strong>Duplicate groups: ${duplicates.length}</strong><strong>Duplicate files: ${duplicateFiles}</strong></div>`;
            if (duplicates.length) {
                const section = document.createElement("section");
                section.className = "duplicate-groups";
                section.innerHTML = `<div class="catalog-section-heading"><h3>Duplicate files</h3><p>Review each group and delete only the copy you no longer need.</p></div>`;
                duplicates.forEach((group) => {
                    const card = document.createElement("article");
                    card.className = "duplicate-group";
                    card.innerHTML = `<div class="duplicate-group-head"><div><strong>${escapeHtml(group.title || "Duplicate track")}</strong><span>${escapeHtml(group.artist || "Unknown Artist")} · ${group.files.length} files</span></div></div><div class="duplicate-file-list"></div>`;
                    const list = card.querySelector(".duplicate-file-list");
                    group.files.forEach((file, index) => {
                        const row = document.createElement("div");
                        row.className = "duplicate-file-row";
                        const size = Number(file.size || 0);
                        const mb = size >= 1024 * 1024 ? `${(size / (1024 * 1024)).toFixed(1)} MB` : `${Math.round(size / 1024)} KB`;
                        row.innerHTML = `<div class="duplicate-file-copy"><strong>${escapeHtml(file.path)}</strong><span>${escapeHtml(file.album || "Unknown Album")} · ${escapeHtml(formatSeconds(file.duration || 0))}${index === 0 ? " · first found" : ""} · ${mb}</span></div><button type="button" class="btn-danger duplicate-delete-btn">Delete copy</button>`;
                        row.querySelector("button")?.addEventListener("click", async () => {
                            if (!confirm(`Delete duplicate file "${file.path}"?`)) return;
                            const response = await apiFetch(`api/library/${encodeURIComponent(file.path).replace(/%2F/g, "/")}`, {method:"DELETE"});
                            const result = await response.json().catch(() => ({}));
                            if (!response.ok) { showToast("❌ " + (result.detail || "Could not delete duplicate")); return; }
                            showToast("✅ Duplicate copy deleted");
                            await refreshLibraryCache();
                            renderLibraryView();
                            await loadLibraryHealth();
                        });
                        list?.appendChild(row);
                    });
                    section.appendChild(card);
                });
                content.appendChild(section);
            } else {
                const empty = document.createElement("div");
                empty.className = "queue-empty";
                empty.textContent = "No duplicate groups found.";
                content.appendChild(empty);
            }
        } catch (err) {
            content.innerHTML = `<div class="queue-empty">${escapeHtml(err.message || "Library health unavailable")}</div>`;
        }
    }
    document.getElementById("libraryHealthButton")?.addEventListener("click",async()=>{document.getElementById("health-modal").hidden=false;await loadLibraryHealth();});

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
   CROSS-PLATFORM UI + DOWNLOAD CENTER + MOBILE CONTROLS
   ============================================================ */
let deviceHeartbeatTimer = null;
let deviceRefreshTimer = null;
let devices = [];
const etaSamples = new Map();
const libraryRenderStates = new WeakMap();
let searchDebounceTimer = null;

function deviceId() {
    return PLAYER_CLIENT_ID;
}
function deviceType() {
    const ua = String(navigator.userAgent || "").toLowerCase();
    if (/smart-tv|hbbtv|appletv|googletv|netcast|webos.tv/.test(ua)) return "tv";
    if (/ipad|tablet|android(?!.*mobile)/.test(ua)) return "tablet";
    if (/iphone|ipod|android.*mobile|windows phone/.test(ua)) return "phone";
    return "desktop";
}
function deviceName() {
    return storageGet("xrob_music_device_name") || (deviceType() === "phone" ? "Phone" : deviceType() === "tablet" ? "Tablet" : deviceType() === "tv" ? "Living Room" : "This PC");
}
function deviceCapabilities() {
    return { audio: true, mediaSession: Boolean("mediaSession" in navigator), websocket: Boolean(window.WebSocket), touch: navigator.maxTouchPoints > 0, remoteControl: true };
}
function devicePayload() {
    return { deviceId:deviceId(), clientId:PLAYER_CLIENT_ID, tabId:PLAYER_TAB_ID, name:deviceName(), deviceType:deviceType(), platform:String(navigator.userAgentData?.platform || navigator.platform || ""), browser:localDeviceLabel().split(" · ").pop() || "Browser", capabilities:deviceCapabilities() };
}
async function registerDevice() {
    try {
        const r = await apiFetch("api/devices/register", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(devicePayload()), timeoutMs:8000 });
        if (!r.ok) return;
        const d = await r.json().catch(()=>({}));
        devices = Array.isArray(d.devices) ? d.devices : devices;
        appState.devices.items = devices;
        appState.devices.lastUpdatedAt = Date.now();
        renderDevices();
        updateDeviceOwnershipUI();
    } catch (error) { reportAppError(error, {scope:"devices", action:"register"}); }
}
async function heartbeatDevice() {
    if (document.visibilityState === "hidden" || navigator.onLine === false) return;
    try { await apiFetch("api/devices/heartbeat", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(devicePayload()), timeoutMs:7000}); } catch (_) {}
}
async function loadDevices() {
    try {
        const r = await apiFetch("api/devices", {cache:"no-store", timeoutMs:8000});
        if (!r.ok) throw new Error("Device list unavailable");
        const d = await r.json();
        devices = Array.isArray(d.devices) ? d.devices : [];
        appState.devices.items = devices;
        appState.devices.lastUpdatedAt = Date.now();
        renderDevices();
        updateDeviceOwnershipUI();
    } catch (err) { console.warn("Devices:", err); }
}
function deviceIcon(type) {
    return type === "phone" ? "smartphone" : type === "tablet" ? "tablet" : type === "tv" ? "tv" : "monitor";
}
function deviceStateLabel(device) {
    if (!device?.online) return "Offline";
    if (device.isSynced) return device.state === "paused" ? "Synced · Paused" : "Synced · Playing";
    if (device.isOwner) return device.state === "paused" ? "Paused · Playing here" : "Playing now";
    return device.state === "paused" ? "Paused" : device.state === "playing" ? "Playing" : "Available";
}
function deviceTrackLabel(device) {
    if (!device?.track?.title) return "Ready for playback";
    return `${device.track.title}${device.track.artist ? ` · ${device.track.artist}` : ""}`;
}
function renderDevices() {
    const list=document.getElementById("deviceList"); if(!list) return;
    const currentId=deviceId();
    const rows=[...devices];
    if (!rows.some(d=>d.deviceId===currentId)) rows.unshift({...devicePayload(),online:true,isOwner:!isRemotePlayerOwner(),state:isRemotePlayerOwner()?"available":"playing",ageSeconds:0,track:null});
    rows.sort((a,b)=>{ const score=d=>d.deviceId===currentId?0:d.isOwner?1:d.online?2:3; return score(a)-score(b)||String(a.name||"").localeCompare(String(b.name||"")); });
    list.innerHTML="";
    if (!rows.length) { list.innerHTML='<div class="device-empty"><i data-lucide="wifi-off"></i><strong>No devices found</strong><span>Open Xrob Music on another device to connect it automatically.</span></div>'; renderLocalIcons(); return; }
    rows.forEach(device=>{
        const card=document.createElement("article"); card.className=`device-card${device.deviceId===currentId?" is-current":""}${device.isOwner?" is-owner":""}${device.online?"":" is-offline"}`;
        const actions=document.createElement("div"); actions.className="device-card-actions";
        const makeBtn=(label,icon,cls,fn)=>{const b=document.createElement("button"); b.type="button"; b.className=cls; b.innerHTML=`<i data-lucide="${icon}"></i><span>${label}</span>`; b.addEventListener("click",e=>{e.preventDefault();e.stopPropagation();fn();}); actions.appendChild(b);};
        const status = device.deviceId===currentId ? (isRemotePlayerOwner()?"Connected · Remote":"Connected · This device") : deviceStateLabel(device);
        if(device.deviceId===currentId){
            const badge=document.createElement("span"); badge.className="device-current-badge"; badge.innerHTML='<i data-lucide="check-circle-2"></i><span>This device</span>'; actions.appendChild(badge);
            if(isRemotePlayerOwner()) makeBtn("Resume here","play","btn-refresh compact",()=>takeoverRemotePlayer().then(ok=>ok&&showToast("▶ Playback moved here")));
        } else if(device.online && device.tabId){
            makeBtn("Switch here","radio","save-btn compact",()=>switchToDevice(device));
            if(device.isOwner) makeBtn(device.state==="playing"?"Pause":"Play",device.state==="playing"?"pause":"play","btn-refresh compact",()=>remoteCommand(device,device.state==="playing"?"pause":"play"));
            if (device.isSynced) makeBtn("Stop sync","unlink","btn-secondary compact",()=>stopSyncDevice(device));
            else makeBtn("Play on both","copy-plus","btn-secondary compact",()=>mirrorToDevice(device));
            makeBtn("Remove","trash-2","btn-danger compact",()=>removeDevice(device));
        } else {
            makeBtn("Remove","trash-2","btn-danger compact",()=>removeDevice(device));
        }
        const browserLabel=device.browser?" · "+escapeHtml(device.browser):"";
        const heartbeatLabel=device.online?`Online · heartbeat ${Math.max(0,Math.round(device.ageSeconds||0))}s ago`:"Offline · last seen previously";
        card.innerHTML=`<div class="device-icon ${device.online?"online":"offline"}"><i data-lucide="${deviceIcon(device.deviceType)}"></i><span></span></div><div class="device-copy"><div class="device-title-row"><strong>${escapeHtml(device.name||"Device")}</strong><span class="device-status-pill ${device.online?"online":"offline"}">${escapeHtml(status)}</span></div><span class="device-meta">${escapeHtml(device.platform||"")}${browserLabel}</span><span class="device-track">${escapeHtml(deviceTrackLabel(device))}</span><span class="device-heartbeat">${escapeHtml(heartbeatLabel)}</span></div>`; card.appendChild(actions); list.appendChild(card);
    });
    renderLocalIcons();
}
async function mirrorToDevice(device) {
    if(!device?.deviceId || !device.online || !device.tabId){showToast("⚠️ Device is offline");return;}
    const state=isRemotePlayerOwner()?appState.player.remoteState:null; const src=state?.src||syncResourceUrl(audio?.src||"");
    if(!src){showToast("▶ Start a track first");return;}
    const payload={targetId:device.tabId,src,title:state?.title||playerTitle?.textContent||"Unknown Track",artist:state?.artist||playerArtist?.textContent||"Unknown Artist",art:state?.art||playerArt?.src||"",songId:state?.songId||audio?.dataset?.xrobSongId||"",source:state?.source||appState.player.source||"library",currentTime:state?Number(state.currentTime||0):Number(audio?.currentTime||0)};
    try{const r=await apiFetch("api/player/mirror",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.detail||"Could not mirror playback");showToast(`▶ Playing on ${device.name||"device"} too`);}catch(err){showToast("❌ "+(err.message||"Mirror playback failed"));}
}
async function stopSyncDevice(device) {
    if (!device?.tabId) return;
    try {
        const r = await apiFetch("api/player/sync-group", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:"remove",targetId:device.tabId})});
        const d = await r.json().catch(()=>({}));
        if (!r.ok) throw new Error(typeof d.detail === "object" ? "Could not stop sync" : (d.detail || "Could not stop sync"));
        showToast(`↔ ${device.name || "Device"} no longer follows playback`); await loadDevices();
    } catch (err) { showToast("❌ " + (err.message || "Could not stop sync")); }
}

async function removeDevice(device){
    if(!device?.deviceId||device.deviceId===deviceId()){showToast("This device cannot remove itself from Connect");return;}
    try{const r=await apiFetch(`api/devices/${encodeURIComponent(device.deviceId)}`,{method:"DELETE"});const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.detail||"Could not remove device");devices=devices.filter(x=>x.deviceId!==device.deviceId);appState.devices.items=devices;appState.devices.lastUpdatedAt=Date.now();renderDevices();showToast(`✓ ${device.name||"Device"} removed from Connect`);}catch(err){showToast("❌ "+(err.message||"Could not remove device"));}
}

async function remoteCommand(device, command, payload={}) {
    if (!device?.tabId || !device.online) { showToast("⚠️ Device is offline"); return; }
    try {
        const r=await apiFetch("api/player/command",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({targetId:device.tabId,command,payload,id:`${PLAYER_CLIENT_ID}-${Date.now()}-${Math.random().toString(36).slice(2)}`})});
        const d=await r.json().catch(()=>({}));
        if(!r.ok) throw new Error(d.detail||"Remote player command failed");
        showToast(command==="play"?"▶ Resumed on device":command==="pause"?"⏸ Paused on device":"✅ Command sent");
        setTimeout(loadDevices,250);
    } catch(err){ showToast("❌ "+(err.message||"Device command failed")); }
}
async function switchToDevice(device) {
    if (!device?.tabId || device.deviceId===deviceId()) return;
    const currentOwner = appState.player.remoteState?.ownerId || getPlayerOwner()?.id || PLAYER_TAB_ID;
    if (!device.online) { showToast("⚠️ Device is offline"); return; }
    try {
        const r=await apiFetch("api/player/handoff",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({newOwnerId:device.tabId,clientId:device.clientId||device.deviceId,deviceName:device.name||"Device",expectedOwnerId:currentOwner})});
        const d=await r.json().catch(()=>({}));
        if(!r.ok) throw new Error(typeof d.detail==="object"?`Playback changed on another device`:(d.detail||"Could not transfer playback"));
        document.getElementById("connect-modal").hidden=true;
        showToast(`▶ Playback moved to ${device.name||"device"}`);
        await loadDevices();
    } catch(err){ showToast("❌ "+(err.message||"Playback transfer failed")); await loadDevices(); }
}
function updateDeviceOwnershipUI() {
    const button=document.getElementById("gp-connect-btn");
    const takeover=document.getElementById("gp-device-takeover");
    const local = !isRemotePlayerOwner();
    const ownerName = devices.find(d=>d.tabId===String(appState.player.remoteState?.ownerId||"") || d.isOwner)?.name || appState.player.remoteState?.deviceName || "another device";
    const label = document.getElementById("gp-device-status");
    if (label) label.textContent = local ? deviceName() : ownerName;
    if (button) { button.dataset.remote=String(!local); button.title=local?"Choose a playback device":"Playback device"; }
    if (takeover) { const hasRemote=Boolean(appState.player.remoteState?.src && appState.player.remoteState?.ownerId && appState.player.remoteState.ownerId!==PLAYER_TAB_ID); takeover.hidden=!hasRemote; takeover.disabled=!hasRemote; takeover.textContent="Resume here"; }
}
function openConnect() { const m=document.getElementById("connect-modal"); if(!m)return; m.hidden=false; loadDevices(); renderDevices(); }
function closeConnect() { const m=document.getElementById("connect-modal"); if(m)m.hidden=true; }

// Remote command helper for direct device-picker controls.
function sendPlayerCommandToTarget(targetId, command, payload={}) { const d=devices.find(x=>x.tabId===targetId); if(d) return remoteCommand(d,command,payload); return false; }

function downloadPipelineIndex(task) {
    const s=String(task?.step||"").toLowerCase(), status=String(task?.status||"").toLowerCase();
    if(status==="queued") return 0;
    if(s.includes("download")) return 1;
    if(s.includes("processing") || s.includes("audio") || s.includes("finalizing")) return 2;
    if(s.includes("metadata")) return 3;
    if(s.includes("artwork") || s.includes("thumbnail")) return 4;
    if(s.includes("library") || status==="completed") return 5;
    return status==="downloading"?1:2;
}
function downloadPipelineHtml(task) {
    const stages=["Queued","Downloading","Processing","Metadata","Artwork","Library"], idx=downloadPipelineIndex(task);
    return `<div class="download-pipeline">${stages.map((stage,i)=>`<span class="${i<idx?"done ":""}${i===idx?"current":""}${i>idx?"pending":""}"><i></i>${stage}</span>`).join("")}</div>`;
}
function downloadEta(task) {
    const p=Math.max(0,Math.min(100,Number(task?.percent)||0)), id=String(task?.id||task?.task_id||""); if(!id || !(p>0 && p<100)) return p>=100?"Complete":"—";
    const now=performance.now(), previous=etaSamples.get(id);
    etaSamples.set(id,{p,t:now});
    if(!previous || p<=previous.p || now<=previous.t) return "Calculating…";
    const rate=(p-previous.p)/((now-previous.t)/1000); if(!(rate>0)) return "Calculating…";
    const seconds=(100-p)/rate; if(!Number.isFinite(seconds)||seconds>86400) return "Calculating…";
    const m=Math.floor(seconds/60), s=Math.round(seconds%60); return m?`${m}m ${s}s`:`${s}s`;
}
function downloadStatusLabel(task) {
    const st=String(task?.status||"").toLowerCase();
    if(st==="queued") return "Queued"; if(st==="downloading") return "Downloading"; if(st==="processing") return task.step||"Processing"; if(st==="completed") return "Ready"; if(st==="cancelled"||st==="canceled") return "Cancelled"; return "Failed";
}
function createDownloadCard(task, index=0) {
    const card=document.createElement("article"); card.className="download-card download-card";
    const isHistory=Boolean(task?.history), failed=["error","failed","cancelled","canceled"].includes(String(task?.status||"").toLowerCase());
    const title=task?.title||task?.final_name||"Unknown Track", artist=task?.artist||"Unknown Artist", album=task?.album||"";
    const progress=Math.max(0,Math.min(100,Number(task?.percent)||0));
    const cover=task?.cover||"static/logo.png";
    card.innerHTML=`<div class="download-art"><img src="${escapeHtml(cover)}" alt="" loading="lazy"><span class="download-status-dot ${isHistory?"history":failed?"failed":"active"}"></span></div><div class="download-copy"><div class="download-topline"><strong>${escapeHtml(title)}</strong><span class="download-percent">${Math.round(progress)}%</span></div><span class="download-artist">${escapeHtml(artist)}${album?` · ${escapeHtml(album)}`:""}</span><span class="download-source">${escapeHtml(task?.url||"Local source")}</span><div class="download-progress-track"><i style="width:${progress}%"></i></div><div class="download-metrics"><span>${escapeHtml(downloadStatusLabel(task))}</span><span>${escapeHtml(task?.speed||"")}</span><span>ETA ${escapeHtml(downloadEta(task))}</span></div>${downloadPipelineHtml(task)}${task?.error?`<div class="download-error-line"><i data-lucide="circle-alert"></i>${escapeHtml(String(task.error).slice(0,280))}</div>`:""}</div><div class="download-actions download-actions"></div>`;
    const actions=card.querySelector(".download-actions");
    if(failed && !isHistory){const b=document.createElement("button");b.className="save-btn compact";b.type="button";b.innerHTML=task.resume_available?'<i data-lucide="play"></i> Resume':'<i data-lucide="refresh-cw"></i> Retry';b.onclick=()=>retryTask(task.id);actions.appendChild(b);}
    if(isActiveTask(task)){const b=document.createElement("button");b.className="btn-danger compact";b.type="button";b.innerHTML='<i data-lucide="x"></i> Cancel';b.onclick=()=>cancelTask(task.id);actions.appendChild(b);}
    const detail=document.createElement("button"); detail.className="btn-refresh compact"; detail.type="button"; detail.innerHTML='<i data-lucide="ellipsis"></i> Details'; detail.onclick=()=>showDownloadDetails(task); actions.appendChild(detail);
    if(isHistory){detail.title="View download details";}
    card.querySelector("img")?.addEventListener("error",e=>{e.currentTarget.src="static/logo.png"},{once:true});
    return card;
}
async function showDownloadDetails(task) {
    const modal=document.getElementById("download-detail-modal"); if(!modal)return;
    document.getElementById("downloadDetailTitle").textContent=task?.title||task?.final_name||"Download details";
    document.getElementById("downloadDetailSubtitle").textContent=`${task?.artist||"Unknown Artist"}${task?.album?` · ${task.album}`:""}`;
    const lines=[`Status: ${downloadStatusLabel(task)}`,`Pipeline: ${task?.step||"—"}`,`Progress: ${Number(task?.percent||0).toFixed(1)}%`,`Speed: ${task?.speed||"—"}`,`ETA: ${downloadEta(task)}`,`URL: ${task?.url||"—"}`,`File: ${task?.final_name||"—"}`,`Retries: ${task?.retry_count||0}`,`Resume available: ${task?.resume_available?"Yes":"No"}`,`Metadata source: ${task?.metadata_source||"—"}`,`Metadata confidence: ${task?.metadata_confidence??"—"}${task?.metadata_confidence!==undefined?"%":""}`,`Error: ${task?.error||"—"}`];
    document.getElementById("downloadDetailContent").textContent=lines.join("\n"); modal.hidden=false;
}
async function loadDownloadHistory(){
    try{const r=await apiFetch("api/downloads/history",{cache:"no-store",timeoutMs:10000});if(!r.ok)throw new Error("History unavailable");const d=await r.json();appState.downloads.history=(Array.isArray(d.history)?d.history:[]).map(x=>({...x,id:x.task_id,history:true}));updateDownloadSummary();if(appState.downloads.filter==="history")renderDownloads(appState.downloads.tasks);}catch(err){reportAppError(err,{scope:"downloads",action:"history"});}
}
function updateDownloadSummary(){
    const active=appState.downloads.tasks.filter(isActiveTask).length, queued=appState.downloads.tasks.filter(t=>String(t.status||"")==="queued").length, failed=appState.downloads.tasks.filter(t=>["error","failed","cancelled","canceled"].includes(String(t.status||"").toLowerCase())).length;
    [["downloadsActiveCount",active],["downloadsQueuedCount",queued],["downloadsFailedCount",failed],["downloadsHistoryCount",appState.downloads.history.length]].forEach(([id,n])=>{const e=document.getElementById(id);if(e)e.textContent=n;});
    const head=document.getElementById("downloadsHeadStatus");if(head)head.textContent=active?`${active} active · ${queued} queued`:`${appState.downloads.history.length} in history`;
}
function renderDownloads(tasks){
    const list=document.getElementById("downloadsList"); if(!list)return;
    updateDownloadSummary();
    const filter=appState.downloads.filter;
    const rows=filter==="history"?appState.downloads.history.slice():filter==="queued"?tasks.filter(t=>String(t.status||"")==="queued"):filter==="failed"?tasks.filter(t=>["error","failed","cancelled","canceled"].includes(String(t.status||"").toLowerCase())):tasks.filter(t=>isActiveTask(t)&&String(t.status||"")!=="queued");
    const hint=document.getElementById("downloadsFilterHint"); if(hint)hint.textContent=filter==="active"?"Currently running jobs":filter==="queued"?"Waiting to start":filter==="failed"?"Retryable failures and cancellations":"Persistent download history";
    const clear=document.getElementById("downloadsClearHistory");if(clear)clear.hidden=filter!=="history";
    list.innerHTML="";
    if(!rows.length){list.innerHTML=`<div class="downloads-empty "><div class="empty-icon"><i data-lucide="download-cloud"></i></div><div class="empty-title">${filter==="history"?"No download history":filter==="failed"?"No failed jobs":filter==="queued"?"Queue is clear":"No active downloads"}</div><div class="empty-text">${filter==="active"?"Start a download from Search or use Batch.":filter==="history"?"Completed and previous jobs will appear here.":"Everything is up to date."}</div></div>`;renderLocalIcons();return;}
    const stack=document.createElement("div");stack.className="download-stack";rows.forEach((task,i)=>stack.appendChild(createDownloadCard(task,i+1)));list.appendChild(stack);renderLocalIcons();
}
async function openBatchDownloads(){const modal=document.getElementById("batch-download-modal");if(modal)modal.hidden=false;}
async function submitBatchDownload(event){event.preventDefault();const urls=(document.getElementById("batchDownloadUrls")?.value||"").split(/\r?\n/).map(x=>x.trim()).filter(Boolean);if(!urls.length){showToast("❌ Add at least one URL");return;}if(urls.length>200){showToast("❌ Maximum 200 URLs per batch");return;}const body={urls,artist:document.getElementById("batchDownloadArtist")?.value||"",album:document.getElementById("batchDownloadAlbum")?.value||""};const btn=document.querySelector("#batchDownloadForm button[type=submit]");if(btn)btn.disabled=true;try{const r=await apiFetch("api/download/batch",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body),timeoutMs:20000});const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.detail||"Batch download failed");const queued=(d.results||[]).filter(x=>["ok","already_queued"].includes(x.status)).length;document.getElementById("batch-download-modal").hidden=true;document.getElementById("batchDownloadUrls").value="";showToast(`✅ ${queued} download${queued===1?"":"s"} added`);await pollTasks(true);}catch(err){reportAppError(err,{scope:"downloads",action:"batch"});showToast("❌ "+(err.message||"Batch download failed"));}finally{if(btn)btn.disabled=false;}}
async function clearDownloadHistory(){try{const r=await apiFetch("api/downloads/history",{method:"DELETE"});if(!r.ok)throw new Error("Could not clear history");appState.downloads.history=[];renderDownloads(appState.downloads.tasks);showToast("🧹 Download history cleared");}catch(err){reportAppError(err,{scope:"downloads",action:"clear-history"});showToast("❌ "+(err.message||"Clear history failed"));}}

async function backupStandard(){try{const r=await apiFetch("api/backup",{cache:"no-store",timeoutMs:30000});if(!r.ok)throw new Error("Backup failed");const blob=await r.blob();const url=URL.createObjectURL(blob),a=document.createElement("a");a.href=url;a.download=`xrob-music-backup-${new Date().toISOString().replace(/[:.]/g,"-")}.zip`;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);showToast("✅ Standard backup created");}catch(err){reportAppError(err,{scope:"backup",action:"standard"});showToast("❌ "+(err.message||"Backup failed"));}}
async function backupEncrypted(){const password=prompt("Create an encrypted backup password (12+ characters):");if(password===null)return;if(password.length<12){showToast("❌ Backup password must be at least 12 characters");return;}try{const r=await apiFetch("api/backup/encrypted",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({password}),timeoutMs:30000});if(!r.ok){const d=await r.json().catch(()=>({}));throw new Error(d.detail||"Encrypted backup failed");}const blob=await r.blob();const url=URL.createObjectURL(blob),a=document.createElement("a");a.href=url;a.download=`xrob-music-backup-encrypted-${new Date().toISOString().replace(/[:.]/g,"-")}.xrbk`;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);showToast("✅ Encrypted backup created");}catch(err){reportAppError(err,{scope:"backup",action:"encrypted"});showToast("❌ "+(err.message||"Encrypted backup failed"));}}
async function restoreBackup(){const input=document.getElementById("restoreFile");const file=input?.files?.[0];if(!file){showToast("Select a backup first");return;}if(!confirm("Restore this backup? A safety copy of the current database will be kept."))return;const form=new FormData();form.append("file",file);if(file.name.toLowerCase().endsWith(".xrbk")){const password=prompt("Enter the encrypted backup password:");if(password===null)return;form.append("password",password);}try{const r=await apiFetch("api/restore",{method:"POST",body:form,timeoutMs:30000});const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.detail||"Restore failed");showToast("✅ Backup restored. Reloading…");setTimeout(()=>location.reload(),700);}catch(err){reportAppError(err,{scope:"backup",action:"restore"});showToast("❌ "+(err.message||"Restore failed"));}}
async function openDiagnostics(){const modal=document.getElementById("diagnostics-modal"),box=document.getElementById("diagnosticsContent");if(!modal||!box)return;modal.hidden=false;box.innerHTML='<div class="queue-empty">Running checks…</div>';try{const r=await apiFetch("api/diagnostics",{cache:"no-store",timeoutMs:30000});const d=await r.json();if(!r.ok)throw new Error(d.detail||"Diagnostics unavailable");const sections=[];for(const [key,val] of Object.entries(d)){if(key==="runtime"&&val){sections.push(`<section class="diag-section"><h3>Runtime</h3><pre>${escapeHtml(JSON.stringify(val,null,2))}</pre></section>`);continue;}if(key==="tools"&&val){sections.push(`<section class="diag-section"><h3>Tools</h3><pre>${escapeHtml(JSON.stringify(val,null,2))}</pre></section>`);continue;}if(val&&typeof val==="object")sections.push(`<section class="diag-section"><h3>${escapeHtml(key)}</h3><pre>${escapeHtml(JSON.stringify(val,null,2))}</pre></section>`);}box.innerHTML=sections.join("");}catch(err){reportAppError(err,{scope:"diagnostics"});box.innerHTML=`<div class="queue-empty">${escapeHtml(err.message||"Diagnostics failed")}</div>`;}}

function renderLibraryTracksWindowed(list, query){
    const files=appState.library.files.filter(file=>{const hay=`${file.title||file.name||""} ${file.artist||""} ${file.album||""} ${file.name||""}`.toLowerCase();return !query||hay.includes(query);});
    list.innerHTML=""; if(!files.length){renderEmpty(list,"music-2",appState.library.files.length?"No matching tracks":"Your library is empty",appState.library.files.length?"Try another search.":"Downloaded tracks will appear here.");return;}
    const token={files, index:0, query};libraryRenderStates.set(list,token);
    const sentinel=document.createElement("div"); sentinel.className="library-window-sentinel";
    const observer=new IntersectionObserver(entries=>{if(!entries.some(e=>e.isIntersecting))return;const state=libraryRenderStates.get(list);if(!state||state!==token)return;const fragment=document.createDocumentFragment();const end=Math.min(state.index+60,state.files.length);for(;state.index<end;state.index++)fragment.appendChild(createTrackCard(state.files[state.index],state.files));list.insertBefore(fragment,sentinel);renderLocalIcons();if(state.index>=state.files.length)observer.disconnect();},{rootMargin:"900px"});
    list.appendChild(sentinel);observer.observe(sentinel);const initial=token.files.slice(0,60);const fragment=document.createDocumentFragment();initial.forEach(f=>fragment.appendChild(createTrackCard(f,files)));list.insertBefore(fragment,sentinel);renderLocalIcons();token.index=initial.length;if(token.index>=token.files.length)observer.disconnect();
}
function renderTracks(list,query){return renderLibraryTracksWindowed(list,query);}


function installKeyboardShortcuts(){document.addEventListener("keydown",event=>{if(event.target?.matches?.("input,textarea,select,[contenteditable=true]"))return;if(event.key===" "){event.preventDefault();playBtn?.click();}else if(event.key==="ArrowRight"&&event.shiftKey){event.preventDefault();seekFromKeyboard(10);}else if(event.key==="ArrowLeft"&&event.shiftKey){event.preventDefault();seekFromKeyboard(-10);}else if(event.key.toLowerCase()==="m"){event.preventDefault();if(audio)audio.muted=!audio.muted;}});}
function seekFromKeyboard(delta){const current=isRemotePlayerOwner()?Number(appState.player.remoteState?.currentTime||0):Number(audio?.currentTime||0),duration=isRemotePlayerOwner()?Number(appState.player.remoteState?.duration||0):Number(audio?.duration||0),next=Math.max(0,Math.min(duration||Infinity,current+delta));if(isRemotePlayerOwner())sendPlayerCommand("seek",{time:next});else if(audio){audio.currentTime=next;persistCurrentPosition(true);schedulePlayerStateBroadcast(true);}}

function installDrawerSwipe(){["downloads-drawer","queue-drawer"].forEach(id=>{const el=document.getElementById(id);if(!el||el.dataset.swipeBound)return;el.dataset.swipeBound="1";let startX=0;let startY=0;el.addEventListener("touchstart",e=>{const t=e.touches[0];if(!t)return;startX=t.clientX;startY=t.clientY;},{passive:true});el.addEventListener("touchend",e=>{const t=e.changedTouches[0];if(!t)return;const dx=t.clientX-startX,dy=t.clientY-startY;if(window.innerWidth<=700&&Math.abs(dx)>90&&Math.abs(dx)>Math.abs(dy)*1.25){if(id==="downloads-drawer")closeDownloadsDrawer();else closeQueueDrawer();}},{passive:true});});}
function applyNoHorizontalOverflow(){document.documentElement.style.overflowX="hidden";document.body.style.overflowX="hidden";}

function saveDeviceName(){const value=(document.getElementById("set_device_name")?.value||"").trim();if(value){storageSet("xrob_music_device_name",value);apiFetch("api/devices/rename",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({deviceId:deviceId(),name:value})}).then(async r=>{if(!r.ok){let d={};try{d=await r.json()}catch(_){};throw new Error(d.detail||`HTTP ${r.status}`)}}).catch(error=>reportAppError(error,{scope:"devices",action:"rename"}));}else storageRemove("xrob_music_device_name");}
function applySettingsToForm(settings){
    const setValue=(id,v)=>{const e=document.getElementById(id);if(e)e.value=v??""}; const setChecked=(id,v)=>{const e=document.getElementById(id);if(e)e.checked=Boolean(v)};
    setValue("set_format",settings.audio_format||"mp3");setValue("set_quality",settings.audio_quality||"320K");setValue("set_metadata_mode",settings.metadata_mode||"auto");const artworkMode=settings.artwork_behavior||((settings.embed_thumbnail!==false)?"embed":"none");setValue("set_artwork_behavior",artworkMode);setChecked("set_thumb",artworkMode!=="none");setChecked("set_meta",settings.embed_metadata);setChecked("set_organize",settings.organize_by_artist);setChecked("set_scan_enabled",settings.scan_enabled!==false);setValue("set_scan_interval",settings.scan_interval_minutes||60);setValue("set_health_scan_interval",settings.health_scan_interval_minutes||360);setValue("set_title_cleanup_rules",settings.title_cleanup_rules||"");setValue("set_daily_mix_count",Math.max(5,Math.min(50,Number(settings.daily_mix_track_count||30))));storageSet("xrob_music_daily_mix_count",String(settings.daily_mix_track_count||30));
    playerSettings={...playerSettings,replaygain_enabled:settings.replaygain_enabled!==false,replaygain_mode:settings.replaygain_mode||"track",replaygain_preamp_db:Number(settings.replaygain_preamp_db||0),replaygain_prevent_clipping:settings.replaygain_prevent_clipping!==false,crossfade_seconds:Number(settings.crossfade_seconds||0),gapless_playback:settings.gapless_playback!==false,keep_playing:settings.keep_playing!==false};
    setChecked("set_replaygain_enabled",playerSettings.replaygain_enabled);setValue("set_replaygain_mode",playerSettings.replaygain_mode);setValue("set_replaygain_preamp",playerSettings.replaygain_preamp_db);setChecked("set_replaygain_clip",playerSettings.replaygain_prevent_clipping);setValue("set_crossfade",playerSettings.crossfade_seconds);setChecked("set_gapless",playerSettings.gapless_playback);setChecked("set_keep_playing",playerSettings.keep_playing);
    setValue("set_download_location",settings.download_location || "/media/xrob-music");setValue("set_max_concurrent",settings.max_concurrent_downloads||3);setValue("set_max_pending",settings.max_pending_downloads||500);setChecked("set_auto_retry",settings.auto_retry_downloads!==false);setValue("set_retry_limit",settings.download_retry_limit??2);setValue("set_retry_backoff",settings.download_retry_backoff_seconds||3);setValue("set_filename_mode",settings.filename_mode||"title");setValue("set_cache_size",settings.cache_size_mb||256);setValue("set_stats_retention",settings.stats_retention_days||365);setValue("set_device_name",deviceName());setValue("set_web_username",settings.web_username||"admin");setValue("set_web_password","");renderStorage(settings.storage);updateQualityState();
}
async function saveSettings(){
    const gv=id=>document.getElementById(id)?.value||"",gc=id=>document.getElementById(id)?.checked??false;
    const artwork=gv("set_artwork_behavior")|| (gc("set_thumb")?"embed":"none");
    const data={audio_format:gv("set_format")||"mp3",audio_quality:gv("set_quality")||"320K",metadata_mode:gv("set_metadata_mode")||"auto",embed_thumbnail:artwork==="embed",embed_metadata:gc("set_meta"),organize_by_artist:gc("set_organize"),scan_enabled:gc("set_scan_enabled"),scan_interval_minutes:Math.max(5,Number(gv("set_scan_interval")||60)),health_scan_interval_minutes:Math.max(30,Math.min(10080,Number(gv("set_health_scan_interval")||360))),title_cleanup_rules:gv("set_title_cleanup_rules"),daily_mix_track_count:Math.max(5,Math.min(50,Number(gv("set_daily_mix_count")||30))),replaygain_enabled:gc("set_replaygain_enabled"),replaygain_mode:gv("set_replaygain_mode")||"track",replaygain_preamp_db:Math.max(-12,Math.min(12,Number(gv("set_replaygain_preamp")||0))),replaygain_prevent_clipping:gc("set_replaygain_clip"),crossfade_seconds:Math.max(0,Math.min(12,Number(gv("set_crossfade")||0))),gapless_playback:gc("set_gapless"),keep_playing:gc("set_keep_playing"),web_username:gv("set_web_username")||"admin",download_location:gv("set_download_location").trim(),max_concurrent_downloads:Math.max(1,Math.min(8,Number(gv("set_max_concurrent")||3))),max_pending_downloads:Math.max(50,Math.min(5000,Number(gv("set_max_pending")||500))),auto_retry_downloads:gc("set_auto_retry"),download_retry_limit:Math.max(0,Math.min(5,Number(gv("set_retry_limit")||2))),download_retry_backoff_seconds:Math.max(1,Math.min(60,Number(gv("set_retry_backoff")||3))),artwork_behavior:artwork,filename_mode:gv("set_filename_mode")||"title",cache_size_mb:Math.max(32,Math.min(2048,Number(gv("set_cache_size")||256))),stats_retention_days:Math.max(30,Math.min(3650,Number(gv("set_stats_retention")||365))),...(gv("set_web_password")?{web_password:gv("set_web_password")}: {})};
    try{const r=await apiFetch("api/settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)}),d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.detail||"Failed to save settings.");applySettingsToForm(d);saveDeviceName();showToast("✅ Settings saved");const msg=document.getElementById("settingsMsg");if(msg)msg.textContent=data.download_location?"Settings saved. Changed download location applies to new downloads.":"Settings saved.";loadDevices();}catch(err){const msg=document.getElementById("settingsMsg");if(msg)msg.textContent="❌ "+(err.message||"Failed to save settings.");showToast("❌ "+(err.message||"Failed to save settings."));}
}
async function resetSettings(){const defaults={audio_format:"mp3",audio_quality:"320K",metadata_mode:"auto",embed_thumbnail:true,embed_metadata:true,organize_by_artist:false,scan_enabled:true,scan_interval_minutes:60,health_scan_interval_minutes:360,title_cleanup_rules:"(Visualizer)\n[Visualizer]\nOfficial Video\nOfficial Music Video\nVideo Clip",daily_mix_track_count:30,replaygain_enabled:true,replaygain_mode:"track",replaygain_preamp_db:0,replaygain_prevent_clipping:true,crossfade_seconds:0,gapless_playback:true,keep_playing:true,download_location:"",max_concurrent_downloads:3,max_pending_downloads:500,auto_retry_downloads:true,download_retry_limit:2,download_retry_backoff_seconds:3,artwork_behavior:"embed",filename_mode:"title",cache_size_mb:256,stats_retention_days:365};try{const r=await apiFetch("api/settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(defaults)}),d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.detail||"Reset failed");applySettingsToForm(d);showToast("↺ Settings reset");}catch(err){showToast("❌ "+(err.message||"Reset failed"));}}


function installLifecycleHandlers(){
    if (appState.lifecycle.installed) return;
    appState.lifecycle.installed = true;
    let leavePersisted = false;
    let recoveryPromise = null;

    const updateNetworkState = (online, reason) => {
        appState.network.online = Boolean(online);
        appState.network.lastTransitionAt = Date.now();
        appState.network.lastReason = reason || "unknown";
        setAppState("network", "online", appState.network.online, {reason});
        emitAppEvent(appState.network.online ? "network:online" : "network:offline", {reason});
    };

    const persistOnLeave = () => {
        if (leavePersisted) return;
        leavePersisted = true;
        appState.lifecycle.lastLeaveAt = Date.now();
        try { persistCurrentPosition(true, true); } catch (error) { reportAppError(error, {scope:"lifecycle", action:"persist-position"}); }
        try {
            if (!isRemotePlayerOwner()) {
                const state = buildPlayerSyncState(true);
                if (state) {
                    state.seq = ++playerSyncSequence;
                    state.force = true;
                    publishPlayerStateToServer(state, true, true);
                }
            }
        } catch (error) { reportAppError(error, {scope:"lifecycle", action:"persist-player-state"}); }
        try { heartbeatDevice(); } catch (error) { reportAppError(error, {scope:"lifecycle", action:"device-heartbeat"}); }
        try { playerSyncChannel?.postMessage({ type: "owner-closing", ownerId: PLAYER_TAB_ID }); } catch (error) { reportAppError(error, {scope:"lifecycle", action:"player-broadcast"}); }
    };

    const recover = async (reason) => {
        if (recoveryPromise) return recoveryPromise;
        if (navigator.onLine === false) return;
        recoveryPromise = (async () => {
            try {
                appState.lifecycle.lastRecoveryAt = Date.now();
                socketReconnectAttempt = 0;
                if (socketReconnectTimer) { clearTimeout(socketReconnectTimer); socketReconnectTimer = null; }
                initWebSocket();
                registerDevice();
                await loadDevices();
                const stateLoaded = await loadServerPlayerState();
                if (stateLoaded) updateDeviceOwnershipUI?.();
                if (!audio?.paused) startPlayerProgressFrame();
                emitAppEvent("lifecycle:recovered", {reason});
            } catch (error) {
                reportAppError(error, {scope:"lifecycle", action:"recover", reason});
            }
        })().finally(() => { recoveryPromise = null; });
        return recoveryPromise;
    };

    const onVisible = (reason = "visible") => {
        leavePersisted = false;
        appState.network.visibility = "visible";
        if (!audio?.paused) startVisualizer();
        updateNetworkState(navigator.onLine !== false, reason);
        emitAppEvent("lifecycle:visible", {reason});
        void recover(reason);
    };
    const onHidden = (reason = "hidden") => {
        appState.network.visibility = "hidden";
        stopVisualizer();
        emitAppEvent("lifecycle:hidden", {reason});
        persistOnLeave();
    };
    const onOnline = () => { leavePersisted = false; updateNetworkState(true, "online"); void recover("online"); };
    const onOffline = () => {
        updateNetworkState(false, "offline");
        if (socketReconnectTimer) { clearTimeout(socketReconnectTimer); socketReconnectTimer = null; }
        emitAppEvent("socket:offline", {});
    };

    updateNetworkState(navigator.onLine !== false, "startup");
    appState.network.visibility = document.visibilityState || "visible";
    window.addEventListener("online", onOnline, {passive:true});
    window.addEventListener("offline", onOffline, {passive:true});
    window.addEventListener("pageshow", () => onVisible("pageshow"), {passive:true});
    window.addEventListener("pagehide", () => onHidden("pagehide"), {passive:true});
    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") onVisible("visibilitychange");
        else onHidden("visibilitychange");
    }, {passive:true});
    window.addEventListener("beforeunload", persistOnLeave, {passive:true});
}


function installAppFeatures(){
    if (appState.lifecycle.featuresInstalled) return;
    appState.lifecycle.featuresInstalled = true;
    document.getElementById("gp-connect-btn")?.addEventListener("click",e=>{e.preventDefault();e.stopPropagation();openConnect();});
    document.getElementById("connectClose")?.addEventListener("click",closeConnect);
    document.getElementById("openDevicesButton")?.addEventListener("click",openConnect);
    document.getElementById("downloadsBatchButton")?.addEventListener("click",openBatchDownloads);
    document.getElementById("batchDownloadClose")?.addEventListener("click",()=>document.getElementById("batch-download-modal").hidden=true);
    document.getElementById("downloadDetailClose")?.addEventListener("click",()=>document.getElementById("download-detail-modal").hidden=true);
    document.getElementById("diagnosticsClose")?.addEventListener("click",()=>document.getElementById("diagnostics-modal").hidden=true);
    document.getElementById("batchDownloadForm")?.addEventListener("submit",submitBatchDownload);
    document.getElementById("downloadsClearHistory")?.addEventListener("click",clearDownloadHistory);
    document.querySelectorAll("[data-download-filter]").forEach(btn=>btn.addEventListener("click",()=>{appState.downloads.filter=btn.dataset.downloadFilter||"active";document.querySelectorAll("[data-download-filter]").forEach(b=>b.classList.toggle("active",b===btn));renderDownloads(appState.downloads.tasks);if(appState.downloads.filter==="history")loadDownloadHistory();}));
    document.getElementById("backupButton")?.addEventListener("click",backupStandard);document.getElementById("encryptedBackupButton")?.addEventListener("click",backupEncrypted);document.getElementById("restoreButton")?.addEventListener("click",()=>document.getElementById("restoreFile")?.click());document.getElementById("restoreFile")?.addEventListener("change",()=>{if(document.getElementById("restoreFile")?.files?.[0])restoreBackup();});document.getElementById("diagnosticsButton")?.addEventListener("click",openDiagnostics);
    document.getElementById("set_device_name")?.addEventListener("change",saveDeviceName);
    const artworkSelect=document.getElementById("set_artwork_behavior");
    const artworkToggle=document.getElementById("set_thumb");
    artworkSelect?.addEventListener("change",()=>{if(artworkToggle)artworkToggle.checked=artworkSelect.value!=="none";});
    artworkToggle?.addEventListener("change",()=>{if(artworkSelect)artworkSelect.value=artworkToggle.checked?"embed":"none";});
    // Function bindings used by existing listeners resolve these latest function declarations.
    updateQueueIndicators();renderDevices();installKeyboardShortcuts();installDrawerSwipe();applyNoHorizontalOverflow();
    registerDevice();heartbeatDevice();loadDevices();loadDownloadHistory();
    if(deviceHeartbeatTimer)clearInterval(deviceHeartbeatTimer);deviceHeartbeatTimer=setInterval(heartbeatDevice,8000);
    if(deviceRefreshTimer)clearInterval(deviceRefreshTimer);deviceRefreshTimer=setInterval(()=>{if(!document.getElementById("connect-modal")?.hidden)loadDevices();},5000);
}


/* ============================================================
   GLOBAL FUNCTIONS
   ============================================================ */

window.navigate = navigate;
window.switchTab = switchTab;
window.openArtist = openArtist;
window.playAlbum = playAlbum;
window.playLibraryTrack = playLibraryTrack;
window.shuffleLibrary = shuffleLibrary;

window.toggleTheme = toggleTheme;
window.openDownloadsDrawer = openDownloadsDrawer;
window.closeDownloadsDrawer = closeDownloadsDrawer;
window.openQueueDrawer = openQueueDrawer;
window.closeQueueDrawer = closeQueueDrawer;

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
