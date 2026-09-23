"use strict";

/* ============================================================
   GLOBAL STATE
   ============================================================ */

let socket = null;
let socketReconnectTimer = null;
let socketReconnectAttempt = 0;
let socketPingTimer = null;

let completedSet = new Set();

let rawLibraryFiles = [];
let libraryArtists = [];
let libraryAlbums = [];
let libraryView = "tracks";
let selectedArtistId = null;
let selectedAlbumId = null;
let libraryPlaybackQueue = null;
let libraryFilesSet = new Set();
let playerShuffle = storageGet("xrob_music_shuffle") === "true";
let shuffleRestoreQueue = null;
let shuffleRestoreCurrentId = null;

let libraryLoadedFromCache = false;

const LIBRARY_CACHE_KEY =
    "xrob_music_library_cache";

const RECENT_CACHE_KEY =
    "xrob_music_recently_added_cache";
let recentTracksCache = [];
let latestSearchItems = [];

let activePreviewBtn = null;
let currentPlayerSource = null;
// "home" or "library"
let currentLibraryIndex = -1;

let currentPage = 1;
let currentQuery = "";
let isLoadingMore = false;
let hasMoreResults = true;
let searchRequestId = 0;
let searchAbortController = null;

let latestTasks = [];
let lastTaskSignature = "";

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
let playerSettings = { replaygain_enabled: true, replaygain_mode: "track", replaygain_preamp_db: 0, replaygain_prevent_clipping: true, crossfade_seconds: 0, gapless_playback: true, fade_on_pause: true };

let savedPlayerState = {
    track: null,
    currentTime: 0,
    volume: 0.8,
    queueIndex: -1
};

let playerRepeatMode = storageGet("xrob_music_repeat") || "off";
let enhancedQueue = [];
let enhancedQueueIndex = -1;
let enhancedSongPositions = {};
let playSessionTrackId = null;
let playSessionRecorded = false;
const PLAY_COUNT_THRESHOLD_SECONDS = 60;
const ENHANCED_QUEUE_KEY = "xrob_music_up_next_queue";
const ENHANCED_REPEAT_KEY = "xrob_music_repeat";
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
            if (retryable && attempt + 1 < attempts && [408, 429, 502, 503, 504].includes(response.status)) {
                await new Promise(resolve => window.setTimeout(resolve, 350 * (attempt + 1)));
                continue;
            }
            return response;
        } catch (error) {
            if (timeoutId) window.clearTimeout(timeoutId);
            if (detachCallerAbort) detachCallerAbort();
            lastError = error;
            if (!retryable || attempt + 1 >= attempts) throw error;
            // Only retry a request when the caller did not explicitly abort it.
            if (baseOptions.signal?.aborted) throw error;
            await new Promise(resolve => window.setTimeout(resolve, 350 * (attempt + 1)));
        }
    }
    throw lastError || new Error("Request failed");
}


const PLAYER_DEVICE_ID = (() => {
    const key = "xrob_music_device_id";
    try {
        const saved = window.localStorage.getItem(key);
        if (saved) return saved;
        const value = `device-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
        window.localStorage.setItem(key, value);
        return value;
    } catch (_) {
        return `device-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
    }
})();
const PLAYER_CLIENT_ID = (() => {
    const key = "xrob_music_player_client_id_v2";
    try {
        const saved = window.sessionStorage.getItem(key);
        if (saved) return saved;
        const value = `client-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
        window.sessionStorage.setItem(key, value);
        return value;
    } catch (_) {
        return `client-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
    }
})();
const PLAYER_SERVER_STATE_STALE_MS = 12000;
const PLAYER_HEARTBEAT_MS = 2500;
const PLAYER_PROGRESS_BROADCAST_MS = 450;
const PLAYER_SERVER_SYNC_MS = 1500;
const PLAYER_HANDOFF_TIMEOUT_MS = 15000;
const PLAYER_DEVICE_HEARTBEAT_MS = 8000;
let playerProgressBroadcastTimer = null;
let playerServerSyncTimer = null;
let playerSyncHeartbeat = null;
let playerDeviceHeartbeatTimer = null;
let playerHandoffInFlight = false;
let playerHandoffStoppingRemote = false;
let serverPlayerStateLoaded = false;
let remoteDisplayTime = 0;
const DAILY_MIX_STATE_KEY = "xrob_music_daily_mix_state_v2";
let lastDeviceList = [];
let playerOwnerId = null;
let applyingRemotePlayerCommand = false;
let suppressLocalOwnershipUntil = 0;
let playerTakeoverPending = false;
let remotePlayerState = null;
let remotePlayerTimer = null;
let remoteRangeLastUpdatedAt = 0;
let visualizerFrame = null;
let visualizerData = null;
let playerSyncSequence = 0;
let lastRemoteOwnerId = null;
let lastRemoteSequence = -1;
let dailyMixTracks = [];
let dailyMixVariant = Number(storageGet("xrob_daily_mix_variant") || 0);

function localPlayerSessionMatches(state) {
    if (!state || typeof state !== "object") return false;
    const ownerId = String(state.ownerId || state.deviceId || "");
    const deviceId = String(state.deviceId || ownerId || "");
    const clientId = String(state.clientId || "");
    return ownerId === PLAYER_DEVICE_ID && deviceId === PLAYER_DEVICE_ID && clientId === PLAYER_CLIENT_ID;
}

function isLocalPlayerDevice(state) {
    if (!state || typeof state !== "object") return false;
    return String(state.deviceId || state.ownerId || "") === PLAYER_DEVICE_ID;
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

async function sendDeviceHeartbeat() {
    try {
        const response = await apiFetch("api/player/devices/heartbeat", {
            method: "POST", credentials: "same-origin", cache: "no-store", timeoutMs: 7000,
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ deviceId: PLAYER_DEVICE_ID, clientId: PLAYER_CLIENT_ID, deviceName: localDeviceLabel() })
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) return false;
        const requests = Array.isArray(data.transfer_requests) ? data.transfer_requests : [];
        if (requests.length) {
            const request = requests[0];
            const remote = remotePlayerState;
            if (remote?.src && !localPlayerSessionMatches(remote)) {
                const moved = await takeoverRemotePlayer(false, request.id);
                if (moved) showToast(`▶ Playback moved to ${localDeviceLabel()}`);
            } else if (!remote && !isLocalPlayerDevice(data.state)) {
                await loadServerPlayerState();
            }
        }
        return true;
    } catch (_) { return false; }
}

function deviceIcon(name) {
    const text = String(name || "").toLowerCase();
    if (/iphone|ipad|android|phone|mobile/.test(text)) return "smartphone";
    if (/windows|linux|mac|desktop|chrome|edge|firefox|safari|opera/.test(text)) return "monitor";
    return "cast";
}

function closeDevicePicker() {
    const modal = document.getElementById("devices-modal");
    if (!modal) return;
    modal.hidden = true;
    modal.setAttribute("aria-hidden", "true");
}

function renderDeviceList(devices) {
    const list = document.getElementById("devicesList");
    if (!list) return;
    lastDeviceList = Array.isArray(devices) ? devices : [];
    const all = [...lastDeviceList];
    if (!all.some(d => d.device_id === PLAYER_DEVICE_ID)) {
        all.unshift({ device_id: PLAYER_DEVICE_ID, client_id: PLAYER_CLIENT_ID, device_name: localDeviceLabel(), online: true, owner: localPlayerSessionMatches(remotePlayerState), owner_id: localPlayerSessionMatches(remotePlayerState) ? PLAYER_DEVICE_ID : "" });
    }
    if (!all.length) {
        list.innerHTML = '<div class="devices-empty">No devices have checked in yet.</div>';
        return;
    }
    const currentState = remotePlayerState;
    const currentOwnerId = String(currentState?.ownerId || playerOwnerId || "");
    list.innerHTML = "";
    all.forEach(device => {
        const row = document.createElement("button");
        row.type = "button";
        row.className = "device-row";
        const isCurrentDevice = device.device_id === PLAYER_DEVICE_ID;
        const isCurrentClient = isCurrentDevice && (!device.client_id || device.client_id === PLAYER_CLIENT_ID);
        const isPlayingDevice = Boolean(device.owner || (device.owner_id && device.owner_id === currentOwnerId));
        const status = isCurrentDevice
            ? (isCurrentClient && isPlayingDevice ? "Playing here" : (isPlayingDevice ? "Playing in another tab" : "This device"))
            : (isPlayingDevice ? (device.paused ? "Paused" : "Playing") : (device.online ? "Available" : "Offline"));
        const trackLine = device.track_title ? ` · ${escapeHtml(device.track_title)}${device.track_artist ? ` — ${escapeHtml(device.track_artist)}` : ""}` : "";
        const action = !isCurrentDevice && device.online ? "Connect" : (isCurrentDevice && isPlayingDevice && !isCurrentClient ? "Use here" : (isCurrentDevice && isCurrentClient ? "✓" : ""));
        row.innerHTML = `<span class="device-row-icon"><i data-lucide="${deviceIcon(device.device_name)}" aria-hidden="true"></i></span><span class="device-row-main"><strong>${escapeHtml(device.device_name || "Xrob Music Device")}</strong><span>${escapeHtml(status)}${trackLine}</span></span><span class="device-row-action">${action}</span>`;
        const canConnect = (isCurrentDevice && isPlayingDevice && !isCurrentClient) || (!isCurrentDevice && device.online);
        row.disabled = !canConnect && !isCurrentDevice;
        row.addEventListener("click", async () => {
            if (isCurrentDevice && isCurrentClient && !isPlayingDevice) { closeDevicePicker(); return; }
            const remoteState = remotePlayerState;
            if (isCurrentDevice && isPlayingDevice && !isCurrentClient) {
                if (remoteState?.src) await takeoverRemotePlayer(false);
                else await loadServerPlayerState();
                closeDevicePicker();
                return;
            }
            if (!device.device_id || device.device_id === PLAYER_DEVICE_ID) { closeDevicePicker(); return; }
            try {
                await apiFetch("api/player/device-transfer", {
                    method: "POST", credentials: "same-origin", timeoutMs: 7000,
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ targetDeviceId: device.device_id, requesterDeviceId: PLAYER_DEVICE_ID, requesterName: localDeviceLabel() })
                });
                showToast(`↗ Requesting playback on ${device.device_name}`);
            } catch (error) {
                showToast(`❌ ${error.message || "Could not contact device"}`);
            }
            closeDevicePicker();
        });
        list.appendChild(row);
    });
    renderLocalIcons(list);
}

async function refreshDevices() {
    const updated = document.getElementById("devicesUpdatedAt");
    try {
        const response = await apiFetch("api/player/devices", { cache: "no-store", credentials: "same-origin", timeoutMs: 7000 });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
        renderDeviceList(data.devices || []);
        if (updated) updated.textContent = `Updated ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
    } catch (_) {
        if (updated) updated.textContent = "Could not refresh devices";
    }
}

function openDevicePicker() {
    const modal = document.getElementById("devices-modal");
    if (!modal) return;
    modal.hidden = false;
    modal.setAttribute("aria-hidden", "false");
    refreshDevices();
}

function updateDeviceOwnershipUI() {
    const status = document.getElementById("gp-device-status");
    const btn = document.getElementById("gp-device-takeover");
    const remote = Boolean(remotePlayerState?.src && !localPlayerSessionMatches(remotePlayerState));
    const stale = Boolean(remotePlayerState?._serverStale);
    const remoteName = String(remotePlayerState?.deviceName || "another device").trim();
    if (status) {
        if (remote) {
            const paused = Boolean(remotePlayerState?.paused);
            status.textContent = stale ? `Last known on ${remoteName}` : `${paused ? "Paused on" : "Playing on"} ${remoteName}`;
            status.title = stale ? "Saved player session from another device" : "Another Xrob Music device currently controls playback";
        } else {
            status.textContent = `Playing on ${localDeviceLabel()}`;
            status.title = "This device controls playback";
        }
        status.dataset.remote = String(remote);
    }
    if (btn) {
        btn.hidden = !remote;
        btn.disabled = !remote || playerHandoffInFlight;
        btn.textContent = stale ? "Resume here" : "Take over";
    }
}

function clearPlayerOwner() {
    playerOwnerId = null;
    updateDeviceOwnershipUI();
}

function isRemotePlayerOwner() {
    if (remotePlayerState?.src && !localPlayerSessionMatches(remotePlayerState)) {
        playerOwnerId = String(remotePlayerState.ownerId || remotePlayerState.deviceId || "") || null;
        return true;
    }
    return Boolean(playerOwnerId && playerOwnerId !== PLAYER_DEVICE_ID);
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
    window.xrobHomeQueue = normalized;
    window.xrobHomeQueueIndex = safeIndex;
    enhancedQueue = [...normalized];
    enhancedQueueIndex = safeIndex;
    libraryPlaybackQueue = [...normalized];
    currentLibraryIndex = safeIndex;
    saveEnhancedQueue();
}

function buildPlayerSyncState(includeQueue = true) {
    if (!audio) return null;
    const state = {
        ownerId: PLAYER_DEVICE_ID,
        clientId: PLAYER_CLIENT_ID,
        deviceId: PLAYER_DEVICE_ID,
        src: syncResourceUrl(audio.src || ""),
        currentTime: Number(audio.currentTime || 0),
        duration: Number(audio.duration || 0),
        volume: Number.isFinite(Number(audio.volume)) ? Number(audio.volume) : 0.8,
        title: playerTitle?.textContent || "",
        artist: playerArtist?.textContent || "",
        art: syncResourceUrl(playerArt?.src || ""),
        songId: audio.dataset.xrobSongId || currentSongId() || "",
        source: currentPlayerSource || "",
        deviceName: localDeviceLabel(),
        queueIndex: currentPlayerSource === "library" ? enhancedQueueIndex : (Number.isInteger(window.xrobHomeQueueIndex) ? window.xrobHomeQueueIndex : -1),
        paused: Boolean(audio.paused),
        muted: Boolean(audio.muted),
        repeatMode: ["off", "track", "queue"].includes(playerRepeatMode) ? playerRepeatMode : "off",
        shuffle: Boolean(playerShuffle),
        at: Date.now()
    };
    if (includeQueue) {
        state.queue = normalizeSyncQueue(currentPlayerSource === "library" ? enhancedQueue : (window.xrobHomeQueue || []));
        state.dailyMix = {
            tracks: normalizeSyncQueue(Array.isArray(dailyMixTracks) ? dailyMixTracks : []),
            variant: Number(dailyMixVariant || 0),
            title: document.getElementById("dailyMixTitle")?.textContent || "Daily Mix",
            subtitle: document.getElementById("dailyMixSubtitle")?.textContent || "Personalized from your listening",
            scrollLeft: Number(document.getElementById("dailyMixTracks")?.scrollLeft || 0),
            date: getLocalDateKey(),
        };
    }
    return state;
}

function broadcastPlayerState(force = false, unload = false) {
    if (!audio || isRemotePlayerOwner() || playerOwnerId !== PLAYER_DEVICE_ID) return;
    const state = buildPlayerSyncState(force);
    if (!state) return;
    state.seq = ++playerSyncSequence;
    if (force) state.force = true;
    if (playerTakeoverPending) { state.takeover = true; playerTakeoverPending = false; }
    publishPlayerStateToServer(state, force, unload);
}

function publishPlayerStateToServer(state, force = false, unload = false) {
    if (!state || state.ownerId !== PLAYER_DEVICE_ID || state.deviceId !== PLAYER_DEVICE_ID || state.clientId !== PLAYER_CLIENT_ID) return;
    const send = (nextState, full, useUnloadTransport = false) => {
        const payload = JSON.stringify({ state: nextState, full, takeover: Boolean(nextState?.takeover) });
        try {
            if (useUnloadTransport && typeof navigator !== "undefined" && typeof navigator.sendBeacon === "function" && typeof Blob !== "undefined") {
                if (navigator.sendBeacon(apiUrl("api/player/state"), new Blob([payload], { type: "application/json" }))) return;
            }
            apiFetch("api/player/state", {
                method: "POST", credentials: "same-origin", keepalive: useUnloadTransport,
                timeoutMs: useUnloadTransport ? 5000 : API_DEFAULT_TIMEOUT_MS,
                headers: { "Content-Type": "application/json" }, body: payload
            }).then(async response => {
                let details = null;
                try { details = await response.clone().json(); } catch (_) {}
                const serverState = details?.state;
                const serverSeq = Number(details?.seq ?? serverState?.seq);
                if (Number.isFinite(serverSeq)) playerSyncSequence = Math.max(playerSyncSequence, serverSeq);
                if (response.status === 409 && !useUnloadTransport) {
                    try { audio?.pause(); } catch (_) {}
                    clearPlayerOwner();
                    await loadServerPlayerState();
                }
            }).catch(() => {});
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
        if (!audio || isRemotePlayerOwner() || playerOwnerId !== PLAYER_DEVICE_ID) return;
        const fresh = buildPlayerSyncState(false);
        if (fresh) { fresh.seq = ++playerSyncSequence; send(fresh, false); }
    }, PLAYER_SERVER_SYNC_MS);
}

function applyAuthoritativeOwnedPlayerState(state, force = false) {
    if (!audio || !localPlayerSessionMatches(state)) return false;
    const incomingSeq = Number(state.seq || 0);
    if (!force && incomingSeq && incomingSeq <= playerSyncSequence) return false;
    playerSyncSequence = Math.max(playerSyncSequence, incomingSeq);
    const previousApplying = applyingRemotePlayerCommand;
    applyingRemotePlayerCommand = true;
    try {
        playerOwnerId = PLAYER_DEVICE_ID;
        remotePlayerState = null;
        stopRemoteProgressTicker();
        if (state.repeatMode && ["off", "track", "queue"].includes(String(state.repeatMode))) {
            playerRepeatMode = String(state.repeatMode); storageSet(ENHANCED_REPEAT_KEY, playerRepeatMode); applyRepeatLabel();
        }
        if (state.shuffle !== undefined && Boolean(state.shuffle) !== playerShuffle) setShuffle(Boolean(state.shuffle));
        if (Array.isArray(state.queue) && state.queue.length) {
            const queue = normalizeSyncQueue(state.queue);
            if (state.source === "home") setSynchronizedHomeQueue(queue, state.queueIndex);
            else syncLibraryQueue(queue, state.queueIndex);
        }
        updatePlayerInfo(state.title, state.artist, state.art);
        if (player) player.style.display = "grid";
        if (volume && Number.isFinite(Number(state.volume))) { audio.volume = Math.max(0, Math.min(1, Number(state.volume))); volume.value = audio.volume; }
        audio.muted = Boolean(state.muted);
        currentPlayerSource = state.source === "home" ? "home" : (state.source ? "library" : currentPlayerSource);
        audio.dataset.xrobSongId = String(state.songId || "");
        const incomingSrc = syncResourceUrl(state.src || "");
        const currentSrc = syncResourceUrl(audio.src || "");
        const target = Number.isFinite(Number(state._serverCurrentTime)) ? Math.max(0, Number(state._serverCurrentTime)) : Math.max(0, Number(state.currentTime || 0));
        const trackChanged = Boolean(incomingSrc) && incomingSrc !== currentSrc;
        if (trackChanged) {
            stopCrossfadePreload();
            const expectedSource = new URL(incomingSrc, location.href).href;
            const generation = ++audioLoadGeneration;
            audio.src = expectedSource;
            audio.load();
            audio.addEventListener("loadedmetadata", () => {
                if (generation !== audioLoadGeneration || audio.src !== expectedSource) return;
                try { if (Number.isFinite(audio.duration) && audio.duration > 0) audio.currentTime = Math.min(target, Math.max(0, audio.duration - 0.25)); } catch (_) {}
                if (state.paused) audio.pause(); else audio.play().catch(() => {});
                applyReplayGainToActiveAudio(activeQueueTrack());
            }, { once: true });
        } else {
            const diff = Math.abs(Number(audio.currentTime || 0) - target);
            if (diff > 1.75 && (state.paused || force)) { try { audio.currentTime = target; } catch (_) {} }
            if (state.paused && !audio.paused) audio.pause();
            else if (!state.paused && audio.paused && audio.src && !playerTakeoverPending) audio.play().catch(() => {});
        }
        updateProgress(); updatePlayingState(!Boolean(state.paused)); updateMediaSession(); updateDeviceOwnershipUI();
        return true;
    } finally { applyingRemotePlayerCommand = previousApplying; }
}

async function loadServerPlayerState() {
    try {
        const response = await apiFetch("api/player/state", { cache: "no-store", credentials: "same-origin", timeoutMs: 7000 });
        if (!response.ok) return false;
        const data = await response.json().catch(() => ({}));
        if (!data?.state) {
            playerOwnerId = null;
            remotePlayerState = null;
            serverPlayerStateLoaded = false;
            updateDeviceOwnershipUI();
            return false;
        }
        const state = { ...data.state, _serverUpdatedAt: Number(data.updated_at || 0), _serverLastSeenAt: Number(data.last_seen_at || data.updated_at || 0), _serverActive: Boolean(data.active), _serverStale: Boolean(data.stale), _serverPersistent: Boolean(data.persistent), _serverCurrentTime: Number(data.state._serverCurrentTime ?? data.state.currentTime ?? 0) };
        serverPlayerStateLoaded = true;
        if (localPlayerSessionMatches(state)) {
            playerOwnerId = PLAYER_DEVICE_ID;
            applyAuthoritativeOwnedPlayerState(state, true);
            return true;
        }
        playerOwnerId = String(state.ownerId || state.deviceId || "") || null;
        applyRemotePlayerState(state, true);
        return true;
    } catch (_) { return false; }
}

function claimLocalPlayerWhenOwnerIsGone() {
    if (remotePlayerState?.src && !remotePlayerState._serverStale) return false;
    if (!remotePlayerState?.src) {
        playerOwnerId = PLAYER_DEVICE_ID;
        return true;
    }
    return false;
}

function schedulePlayerStateBroadcast(force = false) {
    if (force) { broadcastPlayerState(true); return; }
    if (playerProgressBroadcastTimer) return;
    playerProgressBroadcastTimer = window.setTimeout(() => {
        playerProgressBroadcastTimer = null;
        broadcastPlayerState(false);
    }, PLAYER_PROGRESS_BROADCAST_MS);
}

function sendPlayerCommand(command, payload = {}) {
    const targetDeviceId = String(remotePlayerState?.deviceId || remotePlayerState?.ownerId || "");
    const targetClientId = String(remotePlayerState?.clientId || "");
    if (!targetDeviceId || !command || localPlayerSessionMatches(remotePlayerState)) return false;
    const message = { targetDeviceId, targetClientId, command, payload, requestId: `${PLAYER_DEVICE_ID}:${PLAYER_CLIENT_ID}:${Date.now()}:${Math.random().toString(36).slice(2)}` };
    apiFetch("api/player/command", { method: "POST", credentials: "same-origin", timeoutMs: 7000, headers: { "Content-Type": "application/json" }, body: JSON.stringify(message) })
        .then(async response => {
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                await loadServerPlayerState();
                throw new Error(data.detail?.status === "owned" ? "Playback changed on another device" : (data.detail || "Remote control failed"));
            }
            if (data.state) applyRemotePlayerState({ ...data.state, _serverUpdatedAt: Number(data.updated_at || Date.now()/1000), _serverCurrentTime: Number(data.state.currentTime || 0) }, true);
        })
        .catch(error => console.warn("Remote player command:", error));
    return true;
}

function updateRemoteProgress(animationFrame = false) {
    if (!remotePlayerState?.ownerId || localPlayerSessionMatches(remotePlayerState)) return;
    const anchorSeconds = Number(remotePlayerState._serverUpdatedAt || 0);
    const anchorMs = anchorSeconds > 1e12 ? anchorSeconds : anchorSeconds * 1000;
    const base = Math.max(0, Number(remotePlayerState._serverCurrentTime ?? remotePlayerState.currentTime ?? 0));
    const elapsed = !remotePlayerState.paused && anchorMs ? Math.max(0, (Date.now() - anchorMs) / 1000) : 0;
    const duration = Math.max(0, Number(remotePlayerState.duration || 0));
    const current = duration > 0 ? Math.min(duration, base + elapsed) : base + elapsed;
    remoteDisplayTime = current;
    if (seek && !isSeeking) { const pct = duration > 0 ? Math.max(0, Math.min(100, current / duration * 100)) : 0; seek.value = pct; renderSeekVisual(pct); }
    if (curTime) curTime.textContent = formatSeconds(current);
    if (durTime && duration > 0) durTime.textContent = formatSeconds(duration);
    if (animationFrame && remotePlayerTimer) window.requestAnimationFrame(() => {});
}

function startRemoteProgressTicker() {
    stopRemoteProgressTicker();
    if (!remotePlayerState || localPlayerSessionMatches(remotePlayerState)) return;
    const tick = () => {
        if (!remotePlayerState || localPlayerSessionMatches(remotePlayerState)) { remotePlayerTimer = null; return; }
        updateRemoteProgress(true);
        remotePlayerTimer = window.setTimeout(tick, 1000);
    };
    tick();
}

function stopRemoteProgressTicker() {
    if (remotePlayerTimer) window.clearTimeout(remotePlayerTimer);
    remotePlayerTimer = null;
}

function updateRemotePlayerOptimistic(patch = {}) {
    if (!remotePlayerState) return;
    remotePlayerState = { ...remotePlayerState, ...patch };
    if (patch.currentTime !== undefined) { remotePlayerState._serverCurrentTime = Number(patch.currentTime) || 0; remotePlayerState._serverUpdatedAt = Date.now()/1000; }
    if (patch.paused !== undefined) remotePlayerState._serverUpdatedAt = Date.now()/1000;
    updateRemoteProgress();
    if (patch.repeatMode) applyRepeatLabel();
    if (patch.shuffle !== undefined) updateShuffleButtons();
    updateDeviceOwnershipUI();
}

function applyRemotePlayerState(state, fromServer = false) {
    if (!state || localPlayerSessionMatches(state)) return false;
    const sequence = Number(state.seq ?? 0);
    const ownerId = String(state.ownerId || state.deviceId || "");
    const receivedServerAt = Number(state._serverUpdatedAt || 0);
    const receivedMs = receivedServerAt > 1e12 ? receivedServerAt : receivedServerAt * 1000;
    const staleByTime = receivedMs > 0 && (Date.now() - receivedMs) > PLAYER_SERVER_STATE_STALE_MS;
    if (fromServer && staleByTime && !state._serverPersistent && !state.src) return false;
    if (ownerId && lastRemoteOwnerId === ownerId && sequence && sequence < lastRemoteSequence && !state.force) return false;
    const ownerChanged = lastRemoteOwnerId !== ownerId;
    const base = Number(state._serverCurrentTime ?? state.currentTime ?? 0);
    const merged = { ...(ownerChanged ? {} : (remotePlayerState || {})), ...state, ownerId, deviceId: String(state.deviceId || ownerId), currentTime: Math.max(0, base), _serverSynced: Boolean(fromServer || state._serverSynced) };
    lastRemoteOwnerId = ownerId;
    lastRemoteSequence = Math.max(lastRemoteSequence, sequence);
    remotePlayerState = merged;
    playerOwnerId = ownerId || null;
    if (merged.repeatMode && ["off", "track", "queue"].includes(String(merged.repeatMode))) { playerRepeatMode = String(merged.repeatMode); storageSet(ENHANCED_REPEAT_KEY, playerRepeatMode); applyRepeatLabel(); }
    if (merged.shuffle !== undefined) { playerShuffle = Boolean(merged.shuffle); storageSet("xrob_music_shuffle", String(playerShuffle)); updateShuffleButtons(); }
    if (Array.isArray(merged.queue) && merged.queue.length) {
        const q = normalizeSyncQueue(merged.queue);
        if (merged.source === "home") setSynchronizedHomeQueue(q, merged.queueIndex); else syncLibraryQueue(q, merged.queueIndex);
    }
    if (merged.dailyMix) applyRemoteDailyMixState(merged.dailyMix);
    updatePlayerInfo(merged.title, merged.artist, merged.art);
    if (player) player.style.display = "grid";
    if (volume && Number.isFinite(Number(merged.volume))) volume.value = Math.max(0, Math.min(1, Number(merged.volume)));
    updateRemoteProgress(false);
    updatePlayingState(!Boolean(merged.paused));
    startRemoteProgressTicker();
    updateDeviceOwnershipUI();
    return true;
}

async function takeoverRemotePlayer(force = false, transferRequestId = "") {
    const state = remotePlayerState;
    if (!audio || !state?.src || localPlayerSessionMatches(state) || playerHandoffInFlight) return false;
    playerHandoffInFlight = true;
    const button = document.getElementById("gp-device-takeover");
    const oldButtonText = button?.textContent;
    if (button) { button.disabled = true; button.textContent = "Connecting…"; }
    try {
        const response = await apiFetch("api/player/handoff", {
            method: "POST", credentials: "same-origin", timeoutMs: PLAYER_HANDOFF_TIMEOUT_MS,
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ newOwnerId: PLAYER_DEVICE_ID, deviceId: PLAYER_DEVICE_ID, clientId: PLAYER_CLIENT_ID, deviceName: localDeviceLabel(), expectedOwnerId: state.ownerId, force: Boolean(force), requestId: transferRequestId || "" })
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok || !result?.state?.src) { await loadServerPlayerState(); return false; }
        const synced = { ...result.state };
        const serverSeq = Number(synced.seq); if (Number.isFinite(serverSeq)) playerSyncSequence = Math.max(playerSyncSequence, serverSeq);
        const target = Math.max(0, Number(synced.currentTime || 0));
        const duration = Math.max(0, Number(synced.duration || 0));
        const shouldPlay = !Boolean(synced.paused);
        const expectedSource = new URL(syncResourceUrl(synced.src), location.href).href;
        const generation = ++audioLoadGeneration;
        playerOwnerId = PLAYER_DEVICE_ID;
        suppressLocalOwnershipUntil = Date.now() + 8000;
        remotePlayerState = null;
        stopRemoteProgressTicker();
        if (Array.isArray(synced.queue) && synced.queue.length) {
            const queue = normalizeSyncQueue(synced.queue);
            if (synced.source === "home") setSynchronizedHomeQueue(queue, synced.queueIndex); else syncLibraryQueue(queue, Number(synced.queueIndex ?? 0));
        }
        currentPlayerSource = synced.source === "home" ? "home" : (synced.source ? "library" : currentPlayerSource);
        updatePlayerInfo(synced.title, synced.artist, synced.art);
        audio.dataset.xrobSongId = String(synced.songId || "");
        activePreviewBtn = null;
        if (Number.isFinite(Number(synced.volume))) { audio.volume = Math.max(0, Math.min(1, Number(synced.volume))); if (volume) volume.value = audio.volume; }
        audio.muted = Boolean(synced.muted);
        playerTakeoverPending = true;
        if (player) player.style.display = "grid";
        stopCrossfadePreload();
        const loaded = await new Promise(resolve => {
            let settled = false;
            const finish = ok => { if (!settled) { settled = true; resolve(ok); } };
            audio.addEventListener("loadedmetadata", () => finish(true), { once: true });
            audio.addEventListener("error", () => finish(false), { once: true });
            window.setTimeout(() => finish(false), PLAYER_HANDOFF_TIMEOUT_MS);
            audio.src = expectedSource; audio.load();
            if (audio.readyState >= 1) queueMicrotask(() => finish(true));
        });
        if (!loaded || generation !== audioLoadGeneration || audio.src !== expectedSource) { await loadServerPlayerState(); return false; }
        try { audio.currentTime = duration > 0 ? Math.min(target, Math.max(0, Number(audio.duration || duration) - 0.25)) : target; } catch (_) {}
        applyReplayGainToActiveAudio(activeQueueTrack()); updateProgress(); updateMediaSession(); updateDeviceOwnershipUI();
        if (shouldPlay) {
            initAudioContext();
            try { await audio.play(); schedulePlayerStateBroadcast(true); }
            catch (error) { updatePlayingState(false); showToast("▶ Tap Play to continue on this device"); console.debug("Playback handoff needs user gesture:", error); }
        } else { audio.pause(); updatePlayingState(false); schedulePlayerStateBroadcast(true); }
        return true;
    } catch (error) {
        console.warn("Playback handoff failed:", error); await loadServerPlayerState().catch(() => {}); return false;
    } finally {
        playerHandoffInFlight = false;
        if (button) { button.disabled = false; button.textContent = oldButtonText || "Take over"; }
        updateDeviceOwnershipUI();
    }
}

function applyRemoteHandoff(message) {
    const fromOwnerId = String(message?.fromOwnerId || "");
    const toOwnerId = String(message?.toOwnerId || "");
    const state = message?.state;
    if (!fromOwnerId || !toOwnerId || !state) return;
    if (fromOwnerId === PLAYER_DEVICE_ID && toOwnerId !== PLAYER_DEVICE_ID) {
        const previousApplying = applyingRemotePlayerCommand;
        applyingRemotePlayerCommand = true;
        playerHandoffStoppingRemote = true;
        try { audio?.pause(); } catch (_) {}
        finally { playerHandoffStoppingRemote = false; applyingRemotePlayerCommand = previousApplying; }
        playerOwnerId = toOwnerId;
        applyRemotePlayerState(state, true);
        return;
    }
    if (toOwnerId !== PLAYER_DEVICE_ID) applyRemotePlayerState(state, true);
}

async function sendPlayerHeartbeat() {
    if (!audio || playerOwnerId !== PLAYER_DEVICE_ID || isRemotePlayerOwner()) return;
    try {
        const response = await apiFetch("api/player/heartbeat", { method: "POST", credentials: "same-origin", timeoutMs: 7000, headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ownerId: PLAYER_DEVICE_ID, clientId: PLAYER_CLIENT_ID, deviceId: PLAYER_DEVICE_ID, deviceName: localDeviceLabel() }) });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) { if (response.status === 409) { try { audio.pause(); } catch (_) {} clearPlayerOwner(); await loadServerPlayerState(); } return; }
        const serverState = result?.state;
        const serverSeq = Number(serverState?.seq); if (Number.isFinite(serverSeq)) playerSyncSequence = Math.max(playerSyncSequence, serverSeq);
    } catch (_) {}
}

async function initPlayerSync() {
    if (typeof window === "undefined") return;
    if (playerSyncHeartbeat) return;
    serverPlayerStateLoaded = false;
    await loadServerPlayerState();
    await sendDeviceHeartbeat();
    playerSyncHeartbeat = window.setInterval(() => { sendPlayerHeartbeat().catch(() => {}); }, PLAYER_HEARTBEAT_MS);
    playerDeviceHeartbeatTimer = window.setInterval(() => { sendDeviceHeartbeat().catch(() => {}); }, PLAYER_DEVICE_HEARTBEAT_MS);
    window.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") { loadServerPlayerState().catch(() => {}); sendDeviceHeartbeat().catch(() => {}); }
    }, { passive: true });
    window.addEventListener("online", () => { loadServerPlayerState().catch(() => {}); sendDeviceHeartbeat().catch(() => {}); }, { passive: true });
    window.addEventListener("beforeunload", () => {
        try { persistCurrentPosition(true, true); } catch (_) {}
        try { broadcastPlayerState(true, true); } catch (_) {}
        if (playerSyncHeartbeat) window.clearInterval(playerSyncHeartbeat);
        if (playerDeviceHeartbeatTimer) window.clearInterval(playerDeviceHeartbeatTimer);
        playerSyncHeartbeat = null; playerDeviceHeartbeatTimer = null;
        if (playerProgressBroadcastTimer) window.clearTimeout(playerProgressBroadcastTimer);
        if (playerServerSyncTimer) window.clearTimeout(playerServerSyncTimer);
        playerProgressBroadcastTimer = playerServerSyncTimer = null;
        stopRemoteProgressTicker();
    }, { once: true });
}

function currentSongId() {
    const useEnhanced = currentPlayerSource === "library" && enhancedQueue.length;
    const q = useEnhanced ? enhancedQueue : (currentPlayerSource === "library" ? getLibraryQueue() : (window.xrobHomeQueue || []));
    const idx = useEnhanced ? enhancedQueueIndex : (currentPlayerSource === "library" ? currentLibraryIndex : window.xrobHomeQueueIndex);
    const item = Number.isInteger(idx) && idx >= 0 ? q[idx] : null;
    return item?.id || null;
}

function saveEnhancedQueue() {
    try { storageSet(ENHANCED_QUEUE_KEY, JSON.stringify({queue: enhancedQueue, index: enhancedQueueIndex})); } catch (_) {}
}

function loadEnhancedQueue() {
    try {
        const v = JSON.parse(storageGet(ENHANCED_QUEUE_KEY) || "null");
        if (Array.isArray(v?.queue) && v.queue.length) {
            enhancedQueue = [...v.queue];
            enhancedQueueIndex = Math.max(
                0,
                Math.min(Number.isInteger(v.index) ? v.index : 0, enhancedQueue.length - 1)
            );
            libraryPlaybackQueue = [...enhancedQueue];
            currentLibraryIndex = enhancedQueueIndex;
        } else {
            enhancedQueue = [];
            enhancedQueueIndex = -1;
            libraryPlaybackQueue = [];
            currentLibraryIndex = -1;
        }
    } catch (_) {
        enhancedQueue = [];
        enhancedQueueIndex = -1;
        libraryPlaybackQueue = [];
        currentLibraryIndex = -1;
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
    const completion_pct = duration > 0 ? Math.max(0, Math.min(100, position / duration * 100)) : 0;
    enhancedSongPositions[id]={position,duration,updated_at:Date.now()/1000,device_id:PLAYER_DEVICE_ID,device_name:localDeviceLabel(),last_played_at:Date.now()/1000,completion_pct};
    const payload = JSON.stringify({song_id:id,position,duration,device_id:PLAYER_DEVICE_ID,device_name:localDeviceLabel()});
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
    return Array.isArray(enhancedQueue) && enhancedQueue.length
        ? enhancedQueue
        : (Array.isArray(libraryPlaybackQueue) && libraryPlaybackQueue.length ? libraryPlaybackQueue : (Array.isArray(rawLibraryFiles) ? rawLibraryFiles : []));
}

function syncLibraryQueue(queue, index) {
    const normalized = normalizeQueue(queue);
    enhancedQueue = normalized;
    enhancedQueueIndex = normalized.length
        ? Math.max(0, Math.min(Number.isInteger(Number(index)) ? Number(index) : 0, normalized.length - 1))
        : -1;
    libraryPlaybackQueue = [...normalized];
    currentLibraryIndex = enhancedQueueIndex;
    if (normalized.length) saveEnhancedQueue();
    else storageRemove(ENHANCED_QUEUE_KEY);
    if (!applyingRemotePlayerCommand && !isRemotePlayerOwner()) schedulePlayerStateBroadcast(true);
}

function reconcileEnhancedQueue() {
    if (!enhancedQueue.length) return;
    const currentId = trackKey(enhancedQueue[enhancedQueueIndex]);
    const valid = new Set(rawLibraryFiles.map(trackKey));
    const filtered = enhancedQueue.filter(track => valid.has(trackKey(track)));
    if (!filtered.length) {
        syncLibraryQueue([], -1);
        return;
    }
    const nextIndex = filtered.findIndex(track => trackKey(track) === currentId);
    syncLibraryQueue(filtered, nextIndex >= 0 ? nextIndex : Math.min(enhancedQueueIndex, filtered.length - 1));
}

function addTrackToQueue(track, playNext = false) {
    if (!track) return false;
    const key = trackKey(track);
    if (!key) return false;
    const queue = getLibraryQueue();
    const currentIndex = currentPlayerSource === 'library' ? getQueueIndex() : -1;
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

function getQueueIndex() { return enhancedQueueIndex; }

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
    const index = Number.isInteger(currentLibraryIndex) ? currentLibraryIndex : -1;
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
    if (currentPlayerSource === "library" && enhancedQueue.length) {
        const currentId = currentSongId();
        if (nextValue) {
            shuffleRestoreQueue = [...enhancedQueue];
            shuffleRestoreCurrentId = currentId;
            const currentIndex = enhancedQueueIndex;
            const current = enhancedQueue[currentIndex];
            syncLibraryQueue([...enhancedQueue.slice(0, currentIndex), current, ...shuffledCopy(enhancedQueue.slice(currentIndex + 1))], currentIndex);
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
    if (!rawLibraryFiles.length) { showToast("No tracks to shuffle"); return; }
    libraryView = "tracks";
    selectedArtistId = null;
    selectedAlbumId = null;
    document.querySelectorAll(".library-tab").forEach(btn => btn.classList.toggle("active", btn.dataset.libraryView === "tracks"));
    renderLibraryView();
    if (currentPlayerSource === "library" && enhancedQueue.length) {
        const { queue, index } = getActiveLibraryQueueState();
        if (!playerShuffle) {
            shuffleRestoreQueue = [...queue];
            shuffleRestoreCurrentId = queue[index]?.id || queue[index]?.name || null;
        }
        const result = shuffleQueueAfterCurrent(queue, index);
        enhancedQueue = result.queue;
        enhancedQueueIndex = result.index;
        libraryPlaybackQueue = [...enhancedQueue];
        currentLibraryIndex = enhancedQueueIndex;
        playerShuffle = true;
        storageSet("xrob_music_shuffle", "true");
        updateShuffleButtons();
        saveEnhancedQueue();
        renderEnhancedQueue();
        return;
    }
    const randomStartIndex = Math.floor(Math.random() * rawLibraryFiles.length);
    playQueue(rawLibraryFiles, randomStartIndex, true);
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
        source: currentPlayerSource || "",
        queueIndex: currentPlayerSource === "library" ? enhancedQueueIndex : (Number.isInteger(window.xrobHomeQueueIndex) ? window.xrobHomeQueueIndex : -1),
        wasPlaying: !audio.paused,
    };
    try { storageSet("xrob_music_player_state", JSON.stringify(state)); lastPlayerStateSavedAt = now; } catch (_) {}
}

function restorePlayerState() {
    if (!audio || serverPlayerStateLoaded) return;
    if (isRemotePlayerOwner()) {
        // A remote owner must have a live state message; an orphaned owner key
        // from a closed/crashed tab should never block the restored local player.
        if (remotePlayerState) return;
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
        if (Array.isArray(enhancedQueue) && enhancedQueue.length && state.queueIndex >= 0) {
            const savedId = String(state.songId || "");
            const found = savedId ? enhancedQueue.findIndex(item => trackKey(item) === savedId || String(item.id || "") === savedId) : -1;
            enhancedQueueIndex = found >= 0 ? found : Math.max(0, Math.min(Number(state.queueIndex) || 0, enhancedQueue.length - 1));
            currentLibraryIndex = enhancedQueueIndex;
            currentPlayerSource = "library";
            saveEnhancedQueue();
        } else if (state.source === "home" && Array.isArray(window.xrobHomeQueue) && window.xrobHomeQueue.length) {
            currentPlayerSource = "home";
            window.xrobHomeQueueIndex = Math.max(0, Math.min(Number(state.queueIndex) || 0, window.xrobHomeQueue.length - 1));
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
        crossfadeGainNode.connect(audioContext.destination);
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
    const q = currentPlayerSource === "library" ? getLibraryQueue() : (window.xrobHomeQueue || []);
    const idx = currentPlayerSource === "library" ? enhancedQueueIndex : window.xrobHomeQueueIndex;
    return q?.[Number(idx)] || null;
}

function fadeMainAudio(target = 1, ms = 140) {
    if (!audioContext || !replayGainNode) return;
    try {
        const now = audioContext.currentTime;
        replayGainNode.gain.cancelScheduledValues(now);
        const current = Math.max(0, Number(replayGainNode.gain.value) || 0);
        replayGainNode.gain.setValueAtTime(current, now);
        replayGainNode.gain.linearRampToValueAtTime(Math.max(0, Number(target) || 0), now + Math.max(0.02, ms / 1000));
    } catch (_) {}
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
    const q = currentPlayerSource === "library" ? getLibraryQueue() : (window.xrobHomeQueue || []);
    const idx = currentPlayerSource === "library" ? enhancedQueueIndex : window.xrobHomeQueueIndex;
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
    const idx = currentPlayerSource === "library"
        ? getLibraryQueue().findIndex(t => trackKey(t) === trackKey(prepared.track))
        : (window.xrobHomeQueue || []).findIndex(t => trackKey(t) === trackKey(prepared.track));
    if (idx < 0) return false;
    crossfadeAudio.pause();
    stopCrossfadePreload();
    if (currentPlayerSource === "library") { enhancedQueueIndex = idx; currentLibraryIndex = idx; }
    else window.xrobHomeQueueIndex = idx;
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
    updateMediaSession();
}


function updateMediaSession() {
    if (!("mediaSession" in navigator) || !("MediaMetadata" in window)) return;
    const title = playerTitle?.textContent || "Unknown Track";
    const artist = playerArtist?.textContent || "Unknown Artist";
    const artwork = playerArt?.src ? [{ src: playerArt.src, sizes: "512x512" }] : [];
    try {
        navigator.mediaSession.metadata = new MediaMetadata({ title, artist, album: "Xrob Music", artwork });
        const remotePlayback = isRemotePlayerOwner() && remotePlayerState;
        navigator.mediaSession.playbackState = (remotePlayback ? Boolean(remotePlayerState.paused) : Boolean(audio?.paused)) ? "paused" : "playing";
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
            if (isRemotePlayerOwner()) { const current = Number(remotePlayerState?.currentTime || 0); if (current > 3) sendPlayerCommand("seek", { time: 0 }); else sendPlayerCommand("previous", { repeat: playerRepeatMode }); }
            else { setPlayerOwner(); playPreviousTrack(); }
        },
        nexttrack: () => {
            if (isRemotePlayerOwner()) sendPlayerCommand("next", { repeat: playerRepeatMode });
            else { setPlayerOwner(); playNextTrack(); }
        },
        seekbackward: details => {
            const offset = Math.max(1, Number(details.seekOffset || 10));
            if (isRemotePlayerOwner()) { const time = Math.max(0, Number(remotePlayerState?.currentTime || 0) - offset); if (sendPlayerCommand("seek", { time })) updateRemotePlayerOptimistic({ currentTime: time }); }
            else if (audio) audio.currentTime = Math.max(0, audio.currentTime - offset);
        },
        seekforward: details => {
            const offset = Math.max(1, Number(details.seekOffset || 10));
            if (isRemotePlayerOwner()) { const base = Number(remotePlayerState?.currentTime || 0); const duration = Number(remotePlayerState?.duration || 0); const time = Math.min(duration > 0 ? duration : base + offset, base + offset); if (sendPlayerCommand("seek", { time })) updateRemotePlayerOptimistic({ currentTime: time }); }
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
        const queue = sourceName === "library" ? enhancedQueue : (window.xrobHomeQueue || []);
        const queueIndex = sourceName === "library" ? enhancedQueueIndex : (Number.isInteger(window.xrobHomeQueueIndex) ? window.xrobHomeQueueIndex : -1);
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


    // Persist the outgoing track before replacing its source. Browser pause events are not
    // guaranteed to run at the moment a new source is assigned.
    persistCurrentPosition(true);
    audio.pause();

    audio.removeAttribute("src");

    audio.dataset.xrobSongId = String(songId || "");
    if (type) currentPlayerSource = type === "search" ? "home" : type;
    savePlayerState();
    stopCrossfadePreload();
    const loadGeneration = ++audioLoadGeneration;
    audio.src = absoluteUrl;

    audio.load();


    const selectedTrack = activeQueueTrack() || { id: songId, title, artist, cover: art };
    const savedResume = songId ? enhancedSongPositions[String(songId)] : null;
    const resumePosition = Number(savedResume?.position || 0);
    audio.addEventListener("loadedmetadata", () => {
        if (loadGeneration !== audioLoadGeneration || audio.src !== absoluteUrl) return;
        applyReplayGainToActiveAudio(selectedTrack);
        if (!fromRemote && resumePosition > 2 && Number.isFinite(Number(audio.duration)) && Number(audio.duration) > 0 && resumePosition < Number(audio.duration) * 0.98) {
            try { audio.currentTime = Math.min(resumePosition, Math.max(0, Number(audio.duration) - 0.25)); } catch (_) {}
        }
    }, { once: true });

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

    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible" && !audio.paused) startVisualizer();
        else stopVisualizer();
    }, { passive: true });


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
            applyReplayGainToActiveAudio(activeQueueTrack(), 1);
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
            if (playerSettings.fade_on_pause !== false && !playerHandoffStoppingRemote) fadeMainAudio(0, 120);
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

            if (currentPlayerSource === "home") {
                if (advanceHomeQueue(1)) return;
            }

            if (currentPlayerSource === "library") {
                if (advanceLibraryQueue(1, true)) return;
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

            const remoteOwner = isRemotePlayerOwner();
            if (remoteOwner && !audio.src && remotePlayerState?.src) {
                const remotePaused = Boolean(remotePlayerState.paused);
                if (sendPlayerCommand(remotePaused ? "play" : "pause")) {
                    updateRemotePlayerOptimistic({ paused: !remotePaused });
                }
                return;
            }
            if (remoteOwner) {
                const remotePaused = remotePlayerState ? Boolean(remotePlayerState.paused) : Boolean(audio.paused);
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
            const current = Number(remotePlayerState?.currentTime || 0);
            if (current > 3) { if (sendPlayerCommand("seek", { time: 0 })) updateRemotePlayerOptimistic({ currentTime: 0 }); }
            else sendPlayerCommand("previous", { repeat: playerRepeatMode });
        } else { setPlayerOwner(); playPreviousTrack(); }
    });

    nextBtn?.addEventListener("click", () => {
        if (isRemotePlayerOwner()) sendPlayerCommand("next", { repeat: playerRepeatMode });
        else { setPlayerOwner(); playNextTrack(); }
    });

    document.getElementById("gp-devices-btn")?.addEventListener("click", openDevicePicker);
    document.getElementById("devicesClose")?.addEventListener("click", closeDevicePicker);
    document.querySelector("[data-close-devices]")?.addEventListener("click", closeDevicePicker);
    document.getElementById("devicesRefresh")?.addEventListener("click", refreshDevices);
    document.addEventListener("keydown", e => { if (e.key === "Escape") closeDevicePicker(); });

    document.getElementById("gp-device-takeover")?.addEventListener("click", async () => {
        const moved = await takeoverRemotePlayer();
        if (moved) showToast("▶ Playback moved to this device");
    });

    document.getElementById("gp-shuffle-btn")?.addEventListener("click", () => setShuffle(!playerShuffle));
    document.getElementById("libraryShuffleButton")?.addEventListener("click", shuffleLibrary);
    document.getElementById("libraryPlayAllButton")?.addEventListener("click", () => playQueue(rawLibraryFiles, 0, false));
    setShuffle(playerShuffle);


    const seekWrap = document.getElementById("gp-seek-wrap") || seek;
    const seekFromPointer = event => {
        const duration = isRemotePlayerOwner() ? Number(remotePlayerState?.duration || 0) : Number(audio?.duration || 0);
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
        const duration = isRemotePlayerOwner() ? Number(remotePlayerState?.duration || 0) : Number(audio?.duration || 0);
        if (duration > 0) {
            const ratio = Math.max(0, Math.min(1, Number(seek.value) / 100));
            const targetTime = ratio * duration;
            if (isRemotePlayerOwner()) scheduleRemoteSeek(targetTime);
            else { setPlayerOwner(); try { audio.currentTime = targetTime; } catch (_) {} }
            if (isRemotePlayerOwner() && remotePlayerState) updateRemotePlayerOptimistic({ currentTime: targetTime });
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
        scan_interval_minutes: 60,
        title_cleanup_rules: "(Visualizer)\n[Visualizer]\nOfficial Video\nOfficial Music Video\nVideo Clip",
        daily_mix_track_count: 30,
        replaygain_enabled: true,
        replaygain_mode: "track",
        replaygain_preamp_db: 0,
        replaygain_prevent_clipping: true,
        crossfade_seconds: 0,
        gapless_playback: true,
        fade_on_pause: true
    };
    try {
        const response = await apiFetch("api/settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(defaults)
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || "Failed to reset settings.");
        applySettingsToForm(data);
        if (document.getElementById("tab-home")?.classList.contains("active")) loadDailyMix();
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
    setValue("set_title_cleanup_rules", settings.title_cleanup_rules || "");
    const dailyMixCount = Math.max(5, Math.min(50, Number(settings.daily_mix_track_count || 30)));
    setValue("set_daily_mix_count", dailyMixCount);
    storageSet("xrob_music_daily_mix_count", String(dailyMixCount));
    playerSettings = { ...playerSettings, replaygain_enabled: settings.replaygain_enabled !== false, replaygain_mode: settings.replaygain_mode || "track", replaygain_preamp_db: Number(settings.replaygain_preamp_db || 0), replaygain_prevent_clipping: settings.replaygain_prevent_clipping !== false, crossfade_seconds: Number(settings.crossfade_seconds || 0), gapless_playback: settings.gapless_playback !== false, fade_on_pause: settings.fade_on_pause !== false };
    try { const localPrefs = JSON.parse(storageGet("xrob_music_playback_prefs") || "null"); if (localPrefs && typeof localPrefs === "object") { playerSettings.fade_on_pause = localPrefs.fade_on_pause !== false; } } catch (_) {}
    setChecked("set_replaygain_enabled", playerSettings.replaygain_enabled);
    setValue("set_replaygain_mode", playerSettings.replaygain_mode);
    setValue("set_replaygain_preamp", playerSettings.replaygain_preamp_db);
    setChecked("set_replaygain_clip", playerSettings.replaygain_prevent_clipping);
    setValue("set_crossfade", playerSettings.crossfade_seconds);
    setChecked("set_gapless", playerSettings.gapless_playback);
    setChecked("set_fade_pause", settings.fade_on_pause !== false);
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
        const response = await apiFetch("api/settings", { cache: "no-store" });
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
        title_cleanup_rules: getValue("set_title_cleanup_rules"),
        daily_mix_track_count: Math.max(5, Math.min(50, Number(getValue("set_daily_mix_count") || 30))),
        replaygain_enabled: getChecked("set_replaygain_enabled"),
        replaygain_mode: getValue("set_replaygain_mode") || "track",
        replaygain_preamp_db: Math.max(-12, Math.min(12, Number(getValue("set_replaygain_preamp") || 0))),
        replaygain_prevent_clipping: getChecked("set_replaygain_clip"),
        crossfade_seconds: Math.max(0, Math.min(12, Number(getValue("set_crossfade") || 0))),
        gapless_playback: getChecked("set_gapless"),
        fade_on_pause: getChecked("set_fade_pause"),
        web_username: getValue("set_web_username") || "admin",
        ...(getValue("set_web_password") ? {web_password:getValue("set_web_password")} : {}),
    };


    try {

        const response =
            await apiFetch(
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


        storageSet("xrob_music_daily_mix_count", String(result.daily_mix_track_count || data.daily_mix_track_count || 30));
        playerSettings = {
            ...playerSettings,
            replaygain_enabled: Boolean(data.replaygain_enabled),
            replaygain_mode: data.replaygain_mode === "album" ? "album" : "track",
            replaygain_preamp_db: Number(data.replaygain_preamp_db || 0),
            replaygain_prevent_clipping: Boolean(data.replaygain_prevent_clipping),
            crossfade_seconds: Math.max(0, Math.min(12, Number(data.crossfade_seconds || 0))),
            gapless_playback: Boolean(data.gapless_playback),
            fade_on_pause: data.fade_on_pause !== false,
        };
        try { storageSet("xrob_music_playback_prefs", JSON.stringify({ fade_on_pause: playerSettings.fade_on_pause })); } catch (_) {}
        if (msg) {

            msg.textContent =
                "✅ Settings saved.";
        }
        if (document.getElementById("tab-home")?.classList.contains("active")) loadDailyMix();


        applyReplayGainToActiveAudio(activeQueueTrack());
        stopCrossfadePreload();
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

        storageSet(
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


        rawLibraryFiles = data.files || [];
        libraryPlaybackQueue = enhancedQueue.length ? [...enhancedQueue] : rawLibraryFiles;
        libraryArtists = data.artists || [];
        libraryAlbums = data.albums || [];
        reconcileEnhancedQueue();

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


const liveStats = {
    tracks: null,
    artists: null,
    albums: null,
    all_play_count: null,
    total_bytes: null,
    folder_size: null
};

function setLiveCounter(id, value) {
    const element = document.getElementById(id);
    if (!element) return;
    const number = Number(value);
    if (!Number.isFinite(number) || number < 0) return;
    element.textContent = String(Math.trunc(number));
}

function applyLiveStats(stats) {
    if (!stats || stats.ready === false) return;

    ["tracks", "artists", "albums", "all_play_count", "total_bytes"].forEach(key => {
        const value = Number(stats[key]);
        if (Number.isFinite(value) && value >= 0) liveStats[key] = value;
    });
    if (typeof stats.folder_size === "string" && stats.folder_size.trim()) liveStats.folder_size = stats.folder_size;

    if (liveStats.tracks !== null) ["statTracks", "downloadStatTracks", "homeTracks", "statusTracks", "subsonicTracks"].forEach(id => setLiveCounter(id, liveStats.tracks));
    if (liveStats.artists !== null) ["statArtists", "homeArtists", "statusArtists"].forEach(id => setLiveCounter(id, liveStats.artists));
    if (liveStats.albums !== null) ["statAlbums", "downloadStatAlbums", "homeAlbums", "statusAlbums"].forEach(id => setLiveCounter(id, liveStats.albums));
    if (liveStats.all_play_count !== null) ["homePlays", "statusPlays"].forEach(id => setLiveCounter(id, liveStats.all_play_count));
    if (liveStats.folder_size !== null) {
        const el = document.getElementById("statusSize");
        if (el) el.textContent = liveStats.folder_size;
    }
}

function applyLivePlayCount(value) {
    const count = Number(value);
    if (!Number.isFinite(count) || count < 0) return;
    if (liveStats.all_play_count === null || count > liveStats.all_play_count) liveStats.all_play_count = count;
    ["homePlays", "statusPlays"].forEach(id => setLiveCounter(id, liveStats.all_play_count));
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
            await apiFetch(
                "/api/stats",
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

        applyLiveStats(stats);

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
        loadDetailedLibraryStats();
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
    const dashboard = document.getElementById("libraryStatsDashboard");
    if (!list) return;

    const showStatistics = libraryView === "statistics";
    if (dashboard) dashboard.hidden = !showStatistics;
    list.hidden = showStatistics;

    if (showStatistics) {
        loadDetailedLibraryStats();
        return;
    }

    const query = String(document.getElementById("libSearchQuery")?.value || "").trim().toLowerCase();
    if (libraryView === "artists") return renderArtists(list, query);
    if (libraryView === "albums") return renderAlbums(list, query);
    if (libraryView === "artist-detail") return renderArtistDetail(list, query);
    if (libraryView === "album-detail") return renderAlbumDetail(list, query);
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
    currentPlayerSource = "library";
    playerShuffle = Boolean(shuffle);
    storageSet("xrob_music_shuffle", String(playerShuffle));
    updateShuffleButtons();
    renderEnhancedQueue();
    playLibraryTrack(playbackIndex);
    return true;
}

function renderTracks(list, query) {
    const files = rawLibraryFiles.filter(file => {
        const hay = `${file.title || file.name || ""} ${file.artist || ""} ${file.album || ""} ${file.name || ""}`.toLowerCase();
        return !query || hay.includes(query);
    });
    list.innerHTML = "";
    if (!files.length) {
        renderEmpty(list, "music-2", rawLibraryFiles.length ? "No matching tracks" : "Your library is empty", rawLibraryFiles.length ? "Try another search." : "Downloaded tracks will appear here.");
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
    card.innerHTML = `<div class="thumb-wrapper"><img src="${escapeHtml(cover)}" alt="" loading="lazy"><span class="track-play-count" title="${plays} play${plays === 1 ? "" : "s"}"><i data-lucide="play" aria-hidden="true"></i> ${plays}</span></div><div class="track-info"><div class="track-title">${escapeHtml(file.title || file.name || "Unknown Track")}</div><div class="track-artist">${escapeHtml(file.artist || "Unknown Artist")} · ${escapeHtml(file.album || "Unknown Album")}</div><div class="track-meta-line"><span>${plays === 1 ? "1 play" : `${plays} plays`}</span></div></div><div class="btn-group"><button type="button" class="btn-preview"><i data-lucide="play" aria-hidden="true"></i> Play</button><button type="button" class="btn-refresh btn-queue-next" title="Play this track next"><i data-lucide="list-plus" aria-hidden="true"></i> Next</button><button type="button" class="btn-refresh btn-queue-add" title="Add this track to the end of the queue"><i data-lucide="plus" aria-hidden="true"></i> Queue</button><button type="button" class="btn-danger"><i data-lucide="trash-2" aria-hidden="true"></i> Delete</button></div>`;
    card.querySelector("img")?.addEventListener("error", e => e.currentTarget.removeAttribute("src"), { once: true });
    const play = () => {
        const activeQueue = getLibraryQueue();
        const activeIndex = activeQueue.findIndex(x => x.id === file.id || x.name === file.name);
        if (currentPlayerSource === "library" && activeQueue.length && activeIndex >= 0) {
            libraryPlaybackQueue = [...activeQueue];
            currentLibraryIndex = activeIndex;
            enhancedQueue = [...activeQueue];
            enhancedQueueIndex = activeIndex;
            saveEnhancedQueue();
            renderEnhancedQueue();
        } else {
            const startIndex = Math.max(0, queue.findIndex(x => x.id === file.id || x.name === file.name));
            if (playerShuffle) {
                playQueue(queue, startIndex, true);
            } else {
                setEnhancedQueue(queue, startIndex);
                currentPlayerSource = "library";
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
        currentPlayerSource = "library";
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
    const artists = libraryArtists.filter(a => !query || String(a.name || "").toLowerCase().includes(query));
    list.innerHTML = "";
    if (!artists.length) return renderEmpty(list, "user-round", "No artists found", query ? "Try another search." : "Scan your library to build the artist catalog.");
    artists.forEach(artist => {
        const card = document.createElement("article");
        card.className = "catalog-card artist-card";
        card.innerHTML = `<button type="button" class="catalog-main-action"><img class="artist-cover" src="${escapeHtml(artist.cover||"")}" alt="" loading="lazy" onerror="this.style.display='none'"/><div><strong>${escapeHtml(artist.name)}</strong><span>${artist.album_count || 0} album${artist.album_count === 1 ? "" : "s"} · ${artist.song_count || 0} track${artist.song_count === 1 ? "" : "s"}</span></div></button><div class="catalog-actions"><button type="button" class="btn-refresh artist-art-btn">Cover</button><button type="button" class="btn-preview catalog-play">▶ Play</button></div>`;
        card.querySelector(".catalog-main-action")?.addEventListener("click", () => openArtist(artist.id));
        card.querySelector(".catalog-play")?.addEventListener("click", e => { e.stopPropagation(); const tracks = rawLibraryFiles.filter(f => (artist.song_ids || []).includes(f.id)); playQueue(tracks, 0, false); });
        card.querySelector(".artist-art-btn")?.addEventListener("click", e => { e.stopPropagation(); const input=document.createElement("input"); input.type="file"; input.accept="image/jpeg,image/png,image/webp"; input.onchange=async()=>{const file=input.files?.[0]; if(!file)return; const fd=new FormData(); fd.append("upload",file); const rr=await apiFetch(`api/library/artist-artwork/${encodeURIComponent(artist.id)}`,{method:"POST",body:fd}); if(rr.ok){showToast("✅ Artist cover saved"); renderArtists(list,query);} else showToast("❌ Could not save artist cover");}; input.click(); });
        list.appendChild(card);
    });
}

function renderAlbums(list, query) {
    const albums = libraryAlbums.filter(a => !query || `${a.name || ""} ${a.artist || ""}`.toLowerCase().includes(query));
    list.innerHTML = "";
    if (!albums.length) return renderEmpty(list, "disc-3", "No albums found", query ? "Try another search." : "Scan your library to build the album catalog.");
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
function playLibraryTrack(index) {
    const queue = getLibraryQueue();
    if (!queue.length || index < 0 || index >= queue.length) return;
    if (!enhancedQueue.length) syncLibraryQueue(queue, index);
    currentPlayerSource = "library";
    enhancedQueueIndex = index;
    currentLibraryIndex = index;
    libraryPlaybackQueue = [...enhancedQueue];
    saveEnhancedQueue();
    renderEnhancedQueue();
    const file = enhancedQueue[index] || queue[index];
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
    const requestId = ++searchRequestId;
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

    if (!query) {
        currentQuery = "";
        currentPage = 1;
        hasMoreResults = false;
        isLoadingMore = false;
        results.innerHTML = "";
        if (searchAbortController === requestController) searchAbortController = null;
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

        if (requestId !== searchRequestId) return;

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

        hasMoreResults = data.length >= 20;
        latestSearchItems = data.filter(item => item && item.url);
        const batchButton = document.getElementById("downloadSearchResults");
        if (batchButton) batchButton.disabled = !latestSearchItems.length;
        renderItems(data);
        // Refresh the local library cache in the background for the Library view,
        // without delaying the search results themselves.
        refreshLibraryCache().catch(() => {});


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

        if (requestId !== searchRequestId || error?.name === "AbortError") return;
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

        if (requestId === searchRequestId && button) {
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
                    <div class="track-info"><div class="track-title">${escapeHtml(title)}</div><div class="track-artist">👤 ${escapeHtml(artist)} · ${escapeHtml(album)}</div></div>
                    <div class="btn-group"><button type="button" class="btn-preview">▶ Play</button><button type="button" class="btn-download queue-local-btn">＋ Queue</button></div>`;
                const play = card.querySelector(".btn-preview");
                play?.addEventListener("click", e => { e.stopPropagation(); toggleAudioStream(play, item.stream || "", "library", title, artist, thumb, item.id || null); });
                card.querySelector(".queue-local-btn")?.addEventListener("click", e => { e.stopPropagation(); addTrackToQueue({...item, name:item.name}, false); });
                card.querySelector("img")?.addEventListener("error", e => e.currentTarget.removeAttribute("src"), {once:true});
                results.appendChild(card);
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


            const titleKey =
                normalizeKey(
                    item.title || ""
                );


            if (item.already_downloaded || libraryFilesSet.has(titleKey)) {

                group.innerHTML = `
                    <div class="badge-library">
                        ✅ In Library
                    </div>
                `;

            } else if (item.already_queued) {

                group.innerHTML = `
                    <div class="badge-library">
                        ⏳ In Download Queue
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
    const requestId = searchRequestId;
    const queryAtStart = currentQuery;
    const requestController = typeof AbortController !== "undefined" ? new AbortController() : null;
    searchAbortController = requestController;

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
            await apiFetch(
                `api/search?q=${
                    encodeURIComponent(
                        currentQuery
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

        if (requestId !== searchRequestId || queryAtStart !== currentQuery) return;

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
            if (data.length < 20) hasMoreResults = false;
            renderItems(data);
        }

    } catch (error) {

        if (requestId !== searchRequestId || error?.name === "AbortError") return;
        console.warn(
            "Load more:",
            error
        );

        showToast(
            "⚠️ Could not load more results"
        );

    } finally {

        if (requestId === searchRequestId && loader) {
            loader.style.display = "none";
        }
        if (searchAbortController === requestController) searchAbortController = null;

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
            ${(task.thumbnail || task.thumbnail_url || task.cover) ? `<img src="${escapeHtml(task.thumbnail || task.thumbnail_url || task.cover)}" alt="" loading="lazy" onerror="this.closest('.download-art')?.classList.add('image-failed'); this.remove();">` : '<div class="download-art-icon"><i data-lucide="music-2" aria-hidden="true"></i></div>'}
            <div class="download-art-overlay">${icon}</div>
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
            "download-action-btn danger";

        actionButton.innerHTML = '<i data-lucide="x" aria-hidden="true"></i><span>Cancel</span>';


        actionButton.addEventListener(
            "click",
            () =>
                cancelTask(
                    task.id
                )
        );

    } else if (["error", "failed", "cancelled", "canceled"].includes(String(task.status || "").toLowerCase())) {
        actionButton.className = "download-action-btn";
        actionButton.innerHTML = '<i data-lucide="rotate-cw" aria-hidden="true"></i><span>Retry</span>';
        actionButton.addEventListener("click", () => retryTask(task.id));
    } else {
        actionButton.className = "download-action-btn ghost";
        actionButton.innerHTML = '<i data-lucide="trash-2" aria-hidden="true"></i><span class="sr-only">Remove</span>';
        actionButton.addEventListener("click", () => removeDownloadTask(task.id));
    }


    actions.appendChild(actionButton);
    return card;
}


let downloadDrawerFilter = "all";

function renderDownloads(tasks) {
    const list = document.getElementById("downloadsList");
    if (!list) return;
    const allTasks = Array.isArray(tasks) ? [...tasks] : [];
    const active = allTasks.filter(isActiveTask);
    const completed = allTasks.filter(task => String(task?.status || "").toLowerCase() === "completed");
    const failed = allTasks.filter(task => ["error", "failed", "cancelled", "canceled"].includes(String(task?.status || "").toLowerCase()));
    const filters = {
        all: allTasks,
        active,
        completed,
        failed,
    };
    const visible = filters[downloadDrawerFilter] || allTasks;

    const setText = (id, value) => { const el = document.getElementById(id); if (el) el.textContent = String(value); };
    setText("downloadsAllCount", allTasks.length);
    setText("downloadsActiveCount", active.length);
    setText("downloadsCompletedCount", completed.length);
    setText("downloadsFailedCount", failed.length);
    setText("downloadsSummary", `${active.length} active · ${completed.length} complete${failed.length ? ` · ${failed.length} attention` : ""}`);
    document.querySelectorAll(".downloads-filter").forEach(btn => {
        const activeTab = btn.dataset.downloadFilter === downloadDrawerFilter;
        btn.classList.toggle("active", activeTab);
        btn.setAttribute("role", "tab");
        btn.setAttribute("aria-selected", activeTab ? "true" : "false");
        btn.tabIndex = activeTab ? 0 : -1;
    });

    list.innerHTML = "";
    if (!visible.length) {
        const empty = document.createElement("div");
        empty.className = "downloads-empty downloads-empty-modern";
        const title = downloadDrawerFilter === "all" ? "No downloads yet" : downloadDrawerFilter === "active" ? "Nothing is downloading" : downloadDrawerFilter === "completed" ? "No completed downloads" : "No failed downloads";
        const body = downloadDrawerFilter === "failed" ? "Failed jobs will appear here with a Retry action." : "Search for music and press Download to add a job.";
        empty.innerHTML = `<div class="downloads-empty-icon"><i data-lucide="download-cloud" aria-hidden="true"></i></div><strong>${title}</strong><span>${body}</span>${downloadDrawerFilter === "all" ? '<button type="button" class="save-btn downloads-empty-action"><i data-lucide="search" aria-hidden="true"></i> Search Music</button>' : ''}`;
        empty.querySelector(".downloads-empty-action")?.addEventListener("click", () => { closeDownloadsDrawer(); navigate("search"); });
        list.appendChild(empty);
        renderLocalIcons(list);
        return;
    }

    const stack = document.createElement("div");
    stack.className = "download-stack download-stack-modern";
    visible.forEach((task, index) => stack.appendChild(createDownloadCard(task, isActiveTask(task) ? index + 1 : null)));
    list.appendChild(stack);
    renderLocalIcons(list);
}

function setDownloadDrawerFilter(filter) {
    if (!["all", "active", "completed", "failed"].includes(filter)) return;
    downloadDrawerFilter = filter;
    renderDownloads(latestTasks);
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


let taskPollInFlight = null;
let taskPollQueuedForce = false;

async function pollTasks(force = false) {
    if (taskPollInFlight) {
        taskPollQueuedForce = taskPollQueuedForce || Boolean(force);
        return taskPollInFlight;
    }
    taskPollInFlight = (async () => {
        try {
            const response = await apiFetch("/api/tasks", { cache: "no-store", timeoutMs: 7000 });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const tasks = await response.json();
            latestTasks = Array.isArray(tasks) ? tasks : [];
            latestTasks.forEach(task => {
                if (task.status === "completed" && !completedSet.has(task.id)) {
                    completedSet.add(task.id);
                    showToast(`🎉 ${task.title || "Track"} is ready`);
                }
            });
            updateQueueCounters(latestTasks);
            const signature = taskSignature(latestTasks);
            const changed = signature !== lastTaskSignature;
            if (force || changed) renderDownloads(latestTasks);
            if (changed && latestTasks.some(task => task.status === "completed")) {
                loadStats().catch(() => {});
                loadHome().catch(() => {});
            }
            lastTaskSignature = signature;
        } catch (error) {
            console.warn("Tasks:", error);
        }
    })();
    try { await taskPollInFlight; }
    finally {
        taskPollInFlight = null;
        if (taskPollQueuedForce) {
            taskPollQueuedForce = false;
            queueMicrotask(() => pollTasks(false));
        }
    }
}


async function loadDownloads() {

    await pollTasks(true);
    await loadStats();
}


async function downloadVisibleSearchResults() {
    const candidates = latestSearchItems.filter(item => !item.already_downloaded && !item.already_queued).slice(0, 50).map(item => ({ url: item.url, title: item.title, artist: item.channel, album: item.album || "" }));
    if (!candidates.length) { showToast("✓ Everything visible is already handled"); return; }
    const button = document.getElementById("downloadSearchResults");
    if (button) { button.disabled = true; button.textContent = "⏳ Adding…"; }
    try {
        const response = await apiFetch("api/download/batch", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ urls: candidates }) });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || "Batch download failed");
        showToast(`⬇️ Added ${Number(data.created_count || 0)} download${Number(data.created_count || 0) === 1 ? "" : "s"}`);
        openDownloadsDrawer(); pollTasks(true).catch(() => {});
        latestSearchItems = latestSearchItems.map(item => ({ ...item, already_queued: candidates.some(c => c.url === item.url) || item.already_queued }));
        renderItems(latestSearchItems);
    } catch (error) { showToast("❌ " + (error.message || "Batch download failed")); }
    finally { if (button) { button.disabled = !latestSearchItems.length; button.textContent = "⬇ Download visible"; } }
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


        if (data.task) {
            const existingIndex = latestTasks.findIndex(t => String(t.id) === String(data.task.id));
            if (existingIndex >= 0) latestTasks[existingIndex] = data.task;
            else latestTasks.unshift(data.task);
            lastTaskSignature = taskSignature(latestTasks);
            updateQueueCounters(latestTasks);
            renderDownloads(latestTasks);
        }

        if (data.status === "already_downloaded" && button) {
            button.disabled = true;
            button.textContent = "✅ In Library";
            button.className = "btn-refresh";
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
        pollTasks(true).catch(() => {});

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
    const queue = window.xrobHomeQueue || [];
    if (!queue.length) return false;
    const current = Number.isInteger(window.xrobHomeQueueIndex) ? window.xrobHomeQueueIndex : -1;
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
    if (currentPlayerSource === "home") return advanceHomeQueue(1);
    if (currentPlayerSource === "library") return advanceLibraryQueue(1);
    return false;
}

function playPreviousTrack() {
    if (audio && audio.currentTime > 3) {
        audio.currentTime = 0;
        return true;
    }
    if (currentPlayerSource === "home") return advanceHomeQueue(-1);
    if (currentPlayerSource === "library") return advanceLibraryQueue(-1);
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
            await apiFetch(
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


        applyLiveStats({ ...stats, ready: true });


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


    try {
        socket = new WebSocket(websocketUrl());

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
            socketReconnectAttempt = 0;
            if (socketPingTimer) window.clearInterval(socketPingTimer);
            socketPingTimer = window.setInterval(() => {
                if (socket?.readyState === WebSocket.OPEN) {
                    try { socket.send("ping"); } catch (_) {}
                }
            }, 20000);
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

                } else if (data.type === "player_state") {
                    if (localPlayerSessionMatches(data.state)) {
                        applyAuthoritativeOwnedPlayerState(data.state, false);
                    } else {
                        applyRemotePlayerState(data.state, true);
                    }
                } else if (data.type === "player_handoff") {
                    applyRemoteHandoff(data);
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
        event => {
            socket = null;
            if (socketPingTimer) { window.clearInterval(socketPingTimer); socketPingTimer = null; }
            // 1008 is an authentication rejection; do not hammer the server until login succeeds.
            if (event?.code !== 1008 && navigator.onLine !== false) scheduleWebSocketReconnect();
        };
}


function scheduleWebSocketReconnect() {

    if (socketReconnectTimer) {
        return;
    }


    if (navigator.onLine === false) return;
    const delay = Math.min(30000, 1000 * (2 ** Math.min(socketReconnectAttempt, 5)));
    socketReconnectAttempt += 1;
    socketReconnectTimer = setTimeout(() => {
        socketReconnectTimer = null;
        initWebSocket();
    }, delay);
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

    // Keep Recently Added and Up Next synchronized with one persisted queue.
    setEnhancedQueue(queue, index);
    renderEnhancedQueue();

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
        track.cover,
        track.id || null
    );
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
        showToast(`✅ Quick scan complete • ${data.tracks || rawLibraryFiles.length} tracks`);
    } catch (error) {
        showToast("❌ " + (error.message || "Quick scan failed."));
    } finally {
        setTimeout(() => hideLoadingCircle("library"), 250);
        if (button) button.disabled = false;
    }
}


function renderLocalIcons(root = document) {
    const paths = {
        house: [['path','M3 10.5 12 3l9 7.5v9a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 19.5z'],['path','M9 21v-6h6v6']],
        search: [['circle','11 11 7 7'],['path','m20 20-4-4']],
        download: [['path','M12 3v12'],['path','m7 10 5 5 5-5'],['path','M5 21h14']],
        library: [['path','M4 19.5V6.5A2.5 2.5 0 0 1 6.5 4H20v16H6.5A2.5 2.5 0 0 1 4 17.5'],['path','M4 17.5A2.5 2.5 0 0 1 6.5 15H20']],
        settings: [['circle','12 12 3'],['path','M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-1.9 1.9-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.1h-2.7v-.1a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1-1.9-1.9.1-.1A1.7 1.7 0 0 0 7.7 15 1.7 1.7 0 0 0 6 14H5.9v-2.7H6a1.7 1.7 0 0 0 1.7-1 1.7 1.7 0 0 0-.3-1.9l-.1-.1 1.9-1.9.1.1a1.7 1.7 0 0 0 1.9.3 1.7 1.7 0 0 0 1-1.6v-.1h2.7v.1a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1 1.9 1.9-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.1V14h-.1a1.7 1.7 0 0 0-1.6 1z']],
        'sliders-horizontal': [['path','M4 7h16'],['path','M4 17h16'],['circle','9 7 2'],['circle','15 17 2']],
        save: [['path','M5 3h12l3 3v15H4V3z'],['path','M8 3v6h8V3'],['path','M8 21v-6h8v6']],
        'rotate-ccw': [['path','M3 12a9 9 0 1 0 3-6.7'],['path','M3 4v5h5']],
        'rotate-cw': [['path','M21 12a9 9 0 1 1-3-6.7'],['path','M21 4v5h-5']],
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
        'skip-back': [['path','M19 20 9 12l10-8v16'],['path','M5 19V5']],
        'skip-forward': [['path','m5 4 10 8-10 8V4'],['path','M19 5v14']],
        shuffle: [['path','M3 6h3c3 0 4 6 7 6h8'],['path','m18 9 3 3-3 3'],['path','M3 18h3c3 0 4-6 7-6h2'],['path','m18 3 3 3-3 3']],
        cast: [['path','M3 18h.01'],['path','M3 14a4 4 0 0 1 4 4'],['path','M3 10a8 8 0 0 1 8 8'],['path','M5 3h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2h-6']],
        monitor: [['rect','3 4 18 14'],['path','M8 21h8'],['path','M12 18v3']],
        smartphone: [['rect','7 2 10 20'],['path','M11 18h2']],
        'list-music': [['path','M8 6h13'],['path','M8 12h13'],['path','M8 18h9'],['path','M3 6h.01'],['path','M3 12h.01'],['path','M3 18h.01']],
        'list-plus': [['path','M8 6h13'],['path','M8 12h13'],['path','M8 18h8'],['path','M3 6h.01'],['path','M3 12h.01'],['path','M3 18h.01'],['path','M19 16v6'],['path','M16 19h6']],
        'bar-chart-3': [['path','M4 20V10'],['path','M10 20V4'],['path','M16 20v-7'],['path','M22 20V7']],
        'folder-open': [['path','M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v1H5.5a2.5 2.5 0 0 0-2.4 1.8L2 16V7z'],['path','M2 16l1.4-4.2A2.5 2.5 0 0 1 5.8 10H21l-1.5 7.5A2 2 0 0 1 17.5 19H4a2 2 0 0 1-2-2.4L2 16z']],
        'settings-2': [['path','M20 7h-9'],['path','M14 17H4'],['circle','17 7 2.5'],['circle','7 17 2.5']],
        sparkles: [['path','m12 3-1.4 4.2L6.4 9 10.6 10.4 12 15l1.4-4.6L17.6 9l-4.2-1.8L12 3z'],['path','m19 14-.7 2.3L16 17l2.3.7L19 20l.7-2.3L22 17l-2.3-.7L19 14z']],
        'download-cloud': [['path','M12 3v10'],['path','m8 9 4 4 4-4'],['path','M20 18.5a4.5 4.5 0 0 0-2.2-8.4A6 6 0 0 0 6.2 8.5 4 4 0 0 0 6 16.5h14']],
    };
    const ns = 'http://www.w3.org/2000/svg';
    const scope = root && typeof root.querySelectorAll === 'function' ? root : document;
    scope.querySelectorAll('[data-lucide]').forEach(el => {
        const name = el.getAttribute('data-lucide') || '';
        const defs = paths[name];
        if (!defs) return;
        const svg = document.createElementNS(ns, 'svg');
        svg.setAttribute('viewBox','0 0 24 24'); svg.setAttribute('fill','none'); svg.setAttribute('stroke','currentColor');
        svg.setAttribute('stroke-width','2'); svg.setAttribute('stroke-linecap','round'); svg.setAttribute('stroke-linejoin','round'); svg.setAttribute('aria-hidden','true');
        defs.forEach(([kind, value]) => {
            const node = document.createElementNS(ns, kind);
            if (kind === 'circle') { const [cx,cy,r]=value.split(' '); node.setAttribute('cx',cx); node.setAttribute('cy',cy); node.setAttribute('r',r); }
            else node.setAttribute('d', value);
            svg.appendChild(node);
        });
        el.replaceWith(svg);
    });
}
async function checkWebAuth() {
    try { const r=await apiFetch("api/auth/status",{cache:"no-store"}); if(!r.ok) return false; const d=await r.json(); return !!d.authenticated; } catch (_) { return false; }
}

function showAuthenticatedApp() { document.getElementById("login-screen")?.classList.add("hidden"); const shell=document.getElementById("app-shell"); if(shell) shell.hidden=false; renderLocalIcons(); }

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


async function logoutWebAuth(){ await apiFetch("api/auth/logout",{method:"POST"}).catch(()=>{}); location.reload(); }

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
    document.getElementById("downloadSearchResults")?.addEventListener("click", downloadVisibleSearchResults);
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

        // Statistics is a dedicated Library view: never leave the catalog list visible.
        const list = document.getElementById("libraryList");
        const dashboard = document.getElementById("libraryStatsDashboard");
        const isStatistics = libraryView === "statistics";
        if (dashboard) dashboard.hidden = !isStatistics;
        if (list) list.hidden = isStatistics;

        renderLibraryView();
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
            const r = await apiFetch('api/library', {cache:'no-store'});
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
    window.addEventListener("online", () => {
        socketReconnectAttempt = 0;
        if (socketReconnectTimer) { clearTimeout(socketReconnectTimer); socketReconnectTimer = null; }
        initWebSocket();
    }, { passive: true });
    window.addEventListener("offline", () => {
        if (socketReconnectTimer) { clearTimeout(socketReconnectTimer); socketReconnectTimer = null; }
    }, { passive: true });


    installEnhancedFeatures();
    installMediaSession();
    const persistOnLeave = () => {
        persistCurrentPosition(true, true);
        if (!isRemotePlayerOwner()) {
            const state = buildPlayerSyncState(true);
            if (state) {
                state.seq = ++playerSyncSequence;
                state.force = true;
                publishPlayerStateToServer(state, true, true);
            }
        }
    };
    window.addEventListener("pagehide", persistOnLeave, { passive: true });
    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "hidden") persistOnLeave();
        else {
            loadServerPlayerState();
            if (!audio?.paused) startPlayerProgressFrame();
        }
    }, { passive: true });
    document.getElementById("errorsButton")?.addEventListener("click",async()=>{const r=await apiFetch("api/errors");const d=await r.json();document.getElementById("errorsContent").innerHTML=(d.errors||[]).length?`<pre>${escapeHtml(JSON.stringify(d.errors,null,2))}</pre>`:'<div class="queue-empty">No errors recorded.</div>';document.getElementById("errors-modal").hidden=false;});
    document.getElementById("errorsClose")?.addEventListener("click",()=>document.getElementById("errors-modal").hidden=true);
    restorePlayerState();


    setInterval(
        () => pollTasks(),
        2000
    );

    setInterval(
        () => loadStats().catch(() => {}),
        5000
    );
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
    if (!enhancedQueue.length) {
        box.innerHTML = '<div class="queue-empty">Queue is empty</div>';
        return;
    }
    enhancedQueue.forEach((t, i) => {
        const row = document.createElement("div");
        row.className = `queue-row ${i === enhancedQueueIndex ? "current" : ""}`;
        row.draggable = true;
        row.dataset.index = String(i);
        row.innerHTML = `<span class="queue-drag" aria-hidden="true"><i data-lucide="grip-vertical"></i></span><img src="${escapeHtml(t.cover || "")}" alt=""><div class="queue-row-info"><strong>${escapeHtml(t.title || t.name || "Unknown")}</strong><span>${escapeHtml(t.artist || "Unknown Artist")}</span></div><button class="queue-next btn-refresh" title="Play next">Next</button><button class="queue-remove icon-btn" title="Remove" aria-label="Remove track">×</button>`;
        const nextButton = row.querySelector(".queue-next");
        const removeButton = row.querySelector(".queue-remove");
        if (i === enhancedQueueIndex) { removeButton.disabled = true; nextButton.disabled = true; }
        nextButton.onclick = e => {
            e.stopPropagation();
            if (i === enhancedQueueIndex || i === enhancedQueueIndex + 1) return;
            const q = [...enhancedQueue]; const [item] = q.splice(i, 1);
            const currentId = currentSongId();
            const currentPos = q.findIndex(x => (x.id || x.name) === currentId);
            q.splice(Math.min(currentPos + 1, q.length), 0, item);
            syncLibraryQueue(q, q.findIndex(x => (x.id || x.name) === currentId));
            renderEnhancedQueue();
        };
        removeButton.onclick = e => {
            e.stopPropagation();
            if (i === enhancedQueueIndex) return showToast("Current track stays in the queue while playing");
            const q = [...enhancedQueue]; q.splice(i, 1);
            const currentId = currentSongId();
            syncLibraryQueue(q, q.findIndex(x => (x.id || x.name) === currentId));
            renderEnhancedQueue();
        };
        row.addEventListener("dblclick", () => playLibraryTrack(i));
        row.addEventListener("dragstart", e => { e.dataTransfer.setData("text/plain", String(i)); e.dataTransfer.effectAllowed = "move"; });
        row.addEventListener("dragover", e => e.preventDefault());
        row.addEventListener("drop", e => {
            e.preventDefault();
            const from = Number(e.dataTransfer.getData("text/plain")); const to = Number(row.dataset.index);
            if (!Number.isInteger(from) || !Number.isInteger(to) || from === to) return;
            const currentId = currentSongId(); const q = [...enhancedQueue]; const [item] = q.splice(from,1); q.splice(to,0,item);
            syncLibraryQueue(q, q.findIndex(x => (x.id || x.name) === currentId)); renderEnhancedQueue();
        });
        box.appendChild(row);
    });
    renderLocalIcons();
}

function setEnhancedQueue(queue, index = 0) {
    syncLibraryQueue(queue, index);
    renderEnhancedQueue();
}

function openQueueDrawer(){
    const drawer = document.getElementById("queue-drawer");
    if (!drawer) return;
    drawer.hidden = false;
    renderEnhancedQueue();
    applyRepeatLabel();
}

function closeQueueDrawer(){
    const drawer = document.getElementById("queue-drawer");
    if (drawer) drawer.hidden = true;
}

function openDownloadsDrawer(){
    const drawer = document.getElementById("downloads-drawer");
    if (!drawer) return;
    drawer.hidden = false;
    loadDownloads().catch(() => {});
    renderLocalIcons();
}

function closeDownloadsDrawer(){
    const drawer = document.getElementById("downloads-drawer");
    if (drawer) drawer.hidden = true;
}

async function saveQueueAsPlaylist(){ if(!enhancedQueue.length){showToast("Queue is empty");return;} const name=prompt("Playlist name", "My Queue"); if(!name)return; const r=await apiFetch("api/playlists",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name,song_ids:enhancedQueue.map(x=>x.id).filter(Boolean)})}); if(r.ok) showToast("✅ Playlist saved"); else showToast("❌ Could not save playlist"); }

async function renderLibraryCollections(mode){
    const list=document.getElementById("libraryList"); if(!list)return;
    list.innerHTML='<div class="downloads-empty"><div class="empty-title">Loading…</div></div>';
    let endpoint=mode==="recent"?"recent":mode==="most"?"most_played":null;
    if(!endpoint)return;
    const r=await apiFetch("api/library/recent-most",{cache:"no-store"}); const d=await r.json(); const rows=d[endpoint]||[]; list.innerHTML="";
    if(!rows.length){renderEmpty(list,"clock-3",mode==="recent"?"Nothing recently played":"No play history yet","Play some tracks to build this list.");return;}
    rows.forEach((t, rank)=>{ const f={...t,name:t.title,stream:t.stream,cover:t.cover,play_count:Number(t.plays||0)}; const card=createTrackCard(f,rows); card.classList.add("collection-track"); card.dataset.rank=String(rank+1); list.appendChild(card); });
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
    } catch (_) {}
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
    } catch (_) {}
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
            currentPlayerSource = 'library';
            playLibraryTrack(index);
        });
        card.querySelector('img')?.addEventListener('error', e => e.currentTarget.removeAttribute('src'), {once:true});
        row.appendChild(card);
    });
    renderLocalIcons();
}

async function loadDailyMix(forceVariation = false) {
    const row = document.getElementById('dailyMixTracks'); if (!row) return;
    try {
        if (forceVariation) {
            dailyMixVariant = (dailyMixVariant + 1) % 20;
            storageSet('xrob_daily_mix_variant', String(dailyMixVariant));
        }
        const r = await apiFetch(`api/daily-mix?variant=${dailyMixVariant}`, {cache:'no-store'});
        if (!r.ok) throw new Error('Daily Mix unavailable');
        const d = await r.json();
        dailyMixTracks = Array.isArray(d.tracks) ? d.tracks : [];
        renderDailyMixCards(d.title || 'Daily Mix', d.subtitle || 'Personalized from your listening');
        const state = {
            tracks: dailyMixTracks,
            variant: dailyMixVariant,
            title: d.title || 'Daily Mix',
            subtitle: d.subtitle || 'Personalized from your listening',
            scrollLeft: row.scrollLeft,
            date: d.date || new Date().toISOString().slice(0,10),
            savedAt: Date.now(),
        };
        try { storageSet(DAILY_MIX_STATE_KEY, JSON.stringify(state)); } catch (_) {}
        if (!isRemotePlayerOwner()) schedulePlayerStateBroadcast(true);
    } catch (e) {
        if (!dailyMixTracks.length) row.innerHTML = '<div class="daily-mix-empty">Daily Mix could not be loaded.</div>';
    }
}

function installEnhancedFeatures(){
    loadEnhancedQueue(); loadEnhancedPositions(); applyRepeatLabel();
    document.getElementById("libraryStatsRefresh")?.addEventListener("click", loadDetailedLibraryStats);
    document.getElementById("dailyMixRefresh")?.addEventListener("click", () => loadDailyMix(true));
    document.getElementById("dailyMixPlay")?.addEventListener("click", () => { if (!dailyMixTracks.length) return; setEnhancedQueue(dailyMixTracks, 0); currentPlayerSource="library"; playLibraryTrack(0); });
    loadDetailedLibraryStats();
    loadPersistedDailyMixState();
    loadDailyMix();
    installDailyMixSwipe();
    document.getElementById("gp-queue-btn")?.addEventListener("click", (event) => { event.preventDefault(); event.stopPropagation(); openQueueDrawer(); });
    document.getElementById("queueClose")?.addEventListener("click", (event) => { event.preventDefault(); event.stopPropagation(); closeQueueDrawer(); });
    document.getElementById("downloadsClose")?.addEventListener("click", (event) => { event.preventDefault(); event.stopPropagation(); closeDownloadsDrawer(); });
    document.getElementById("topbarDownloadsBtn")?.addEventListener("click", (event) => { event.preventDefault(); event.stopPropagation(); openDownloadsDrawer(); });
    document.querySelectorAll(".downloads-filter").forEach(button => button.addEventListener("click", () => setDownloadDrawerFilter(button.dataset.downloadFilter || "all")));
    document.getElementById("downloadsRefresh")?.addEventListener("click", () => pollTasks(true));
    document.getElementById("downloadsClear")?.addEventListener("click", clearDoneTasks);
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
    if (currentPlayerSource === "library" && enhancedQueue.length && enhancedQueueIndex >= 0) {
        const current = enhancedQueue[enhancedQueueIndex];
        syncLibraryQueue(current ? [current] : [], 0);
    } else {
        syncLibraryQueue([], -1);
    }

    shuffleRestoreQueue = null;
    shuffleRestoreCurrentId = null;
    saveEnhancedQueue();
    renderEnhancedQueue();
}); document.getElementById("queueSave")?.addEventListener("click",saveQueueAsPlaylist); document.getElementById("queueRepeat")?.addEventListener("click",cycleRepeatMode);
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
            const r = await apiFetch("api/library/health", {cache:"no-store"});
            const d = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(d.detail || "Could not check library health");
            const duplicates = Array.isArray(d.duplicates) ? d.duplicates : [];
            content.innerHTML = `<div class="health-summary"><strong>Unreadable: ${d.counts?.unreadable || 0}</strong><strong>Bad tags: ${d.counts?.bad_tags || 0}</strong><strong>Missing artwork: ${d.counts?.missing_artwork || 0}</strong><strong>Duplicate groups: ${d.counts?.duplicates || 0}</strong><strong>Duplicate files: ${d.counts?.duplicate_files || 0}</strong></div>`;
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
