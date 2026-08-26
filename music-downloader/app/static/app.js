let socket = null;

let completedSet =
    new Set();

let rawLibraryFiles =
    [];

let libraryFilesSet =
    new Set();

let activePreviewBtn =
    null;

let currentPage =
    1;

let currentQuery =
    "";

let isLoadingMore =
    false;

let hasMoreResults =
    true;

let latestTasks =
    [];

let lastTaskSignature =
    "";


const audio =
    document.getElementById(
        "global-audio-element"
    );

const player =
    document.getElementById(
        "global-player-bar"
    );

const playBtn =
    document.getElementById(
        "gp-play-btn"
    );

const seek =
    document.getElementById(
        "gp-seek"
    );

const volume =
    document.getElementById(
        "gp-volume"
    );

const curTime =
    document.getElementById(
        "gp-cur-time"
    );

const durTime =
    document.getElementById(
        "gp-dur-time"
    );

const playerTitle =
    document.getElementById(
        "gp-title"
    );

const playerArtist =
    document.getElementById(
        "gp-artist"
    );

const playerArt =
    document.getElementById(
        "gp-art"
    );

const canvas =
    document.getElementById(
        "visualizer-canvas"
    );

const canvasCtx =
    canvas
        ? canvas.getContext("2d")
        : null;

let audioContext =
    null;

let analyser =
    null;

let sourceNode =
    null;


/* ============================================================
   HELPERS
   ============================================================ */

function escapeHtml(value) {

    return String(
        value ?? ""
    )
        .replaceAll(
            "&",
            "&amp;"
        )
        .replaceAll(
            "<",
            "&lt;"
        )
        .replaceAll(
            ">",
            "&gt;"
        )
        .replaceAll(
            '"',
            "&quot;"
        )
        .replaceAll(
            "'",
            "&#039;"
        );
}


function normalizeKey(
    value
) {

    return String(
        value || ""
    )
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


function showToast(
    message
) {

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

    toast.className =
        "toast";

    toast.textContent =
        message;

    container.appendChild(
        toast
    );

    setTimeout(
        () => toast.remove(),
        3500
    );
}

window.showToast =
    showToast;


/* ============================================================
   THEME
   ============================================================ */

function toggleTheme(
    theme
) {

    document.documentElement
        .setAttribute(
            "data-theme",
            theme
        );

    localStorage.setItem(
        "xrob_music_theme",
        theme
    );
}

toggleTheme(
    localStorage.getItem(
        "xrob_music_theme"
    ) || "dark"
);


/* ============================================================
   NAVIGATION
   ============================================================ */

function navigate(
    tab,
    updateHash = true
) {

    if (updateHash) {
        location.hash =
            tab;
    }

    switchTab(
        tab
    );
}


function switchTab(
    tab
) {

    document
        .querySelectorAll(
            ".tab-content"
        )
        .forEach(
            section =>
                section.classList.remove(
                    "active"
                )
        );

    document
        .querySelectorAll(
            ".nav-link"
        )
        .forEach(
            button =>
                button.classList.remove(
                    "active"
                )
        );

    const content =
        document.getElementById(
            `tab-${tab}`
        );

    if (content) {
        content.classList.add(
            "active"
        );
    }

    document
        .getElementById(
            `btn-${tab}`
        )
        ?.classList.add(
            "active"
        );

    document
        .getElementById(
            `mob-btn-${tab}`
        )
        ?.classList.add(
            "active"
        );


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
        location.hash.replace(
            "#",
            ""
        );

    const tabs = [
        "home",
        "search",
        "downloads",
        "library",
        "settings",
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

function formatSeconds(
    seconds
) {

    seconds =
        Math.floor(
            seconds || 0
        );

    return (
        Math.floor(
            seconds / 60
        )
        + ":"
        + String(
            seconds % 60
        ).padStart(
            2,
            "0"
        )
    );
}


function updateProgress() {

    if (!audio) {
        return;
    }

    if (
        !audio.duration ||
        !Number.isFinite(
            audio.duration
        )
    ) {

        seek.value =
            0;

        curTime.textContent =
            "0:00";

        return;
    }

    seek.value =
        (
            audio.currentTime
            /
            audio.duration
        )
        * 100;

    curTime.textContent =
        formatSeconds(
            audio.currentTime
        );

    durTime.textContent =
        formatSeconds(
            audio.duration
        );
}


function updatePlayingState(
    playing
) {

    if (playBtn) {

        playBtn.textContent =
            playing
                ? "❚❚"
                : "▶";
    }

    if (
        activePreviewBtn
    ) {

        activePreviewBtn.classList.toggle(
            "playing",
            playing
        );
    }
}


function resetPreviewButton(
    button
) {

    if (!button) {
        return;
    }

    button.classList.remove(
        "playing"
    );

    button.textContent =
        button.dataset.type ===
            "library"
            ? "▶ Play"
            : "▶ Preview";
}


function initAudioContext() {

    if (
        audioContext ||
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
            audioContext
                .createAnalyser();

        analyser.fftSize =
            64;

        analyser.smoothingTimeConstant =
            0.8;

        sourceNode =
            audioContext
                .createMediaElementSource(
                    audio
                );

        sourceNode.connect(
            analyser
        );

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
        new Uint8Array(
            length
        );

    analyser.getByteFrequencyData(
        data
    );

    canvasCtx.clearRect(
        0,
        0,
        canvas.width,
        canvas.height
    );

    const barWidth =
        canvas.width /
        length;

    for (
        let i = 0;
        i < length;
        i++
    ) {

        const height =
            Math.max(
                2,
                (
                    data[i] /
                    255
                )
                * canvas.height
            );

        canvasCtx.fillStyle =
            "#1ed760";

        canvasCtx.fillRect(
            i * barWidth,
            canvas.height -
                height,
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

    playerTitle.textContent =
        title ||
        "Unknown Track";

    playerArtist.textContent =
        artist ||
        "Unknown Artist";

    playerArt.src =
        art ||
        "https://via.placeholder.com/60?text=Music";
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
        !button
    ) {
        return;
    }

    initAudioContext();

    if (
        audioContext &&
        audioContext.state ===
            "suspended"
    ) {

        audioContext.resume();
    }

    const absoluteUrl =
        new URL(
            url,
            location.href
        ).href;

    if (
        activePreviewBtn ===
            button
        &&
        audio.src ===
            absoluteUrl
    ) {

        if (audio.paused) {
            audio.play().catch(
                console.error
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

    activePreviewBtn =
        button;

    button.dataset.type =
        type;

    button.textContent =
        "⏳ Loading...";

    updatePlayerInfo(
        title,
        artist,
        art
    );

    player.style.display =
        "grid";

    audio.pause();

    audio.src =
        absoluteUrl;

    audio.load();

    audio.play()
        .then(
            () => {
                button.textContent =
                    "❚❚ Pause";
            }
        )
        .catch(
            error => {

                console.error(
                    "Playback failed:",
                    error
                );

                button.textContent =
                    "❌ Error";

                setTimeout(
                    () =>
                        resetPreviewButton(
                            button
                        ),
                    1800
                );
            }
        );
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
    "play",
    () =>
        updatePlayingState(
            true
        )
);

audio?.addEventListener(
    "pause",
    () =>
        updatePlayingState(
            false
        )
);

audio?.addEventListener(
    "ended",
    () => {

        updatePlayingState(
            false
        );

        seek.value =
            0;

        curTime.textContent =
            "0:00";

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

        if (audio.paused) {

            audio.play().catch(
                console.error
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
            audio &&
            Number.isFinite(
                audio.duration
            )
        ) {

            audio.currentTime =
                (
                    Number(
                        seek.value
                    )
                    / 100
                )
                *
                audio.duration;
        }
    }
);

const savedVolume =
    localStorage.getItem(
        "xrob_music_volume"
    );

if (savedVolume !== null) {

    volume.value =
        savedVolume;

    audio.volume =
        Number(
            savedVolume
        );
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
    }
);


/* ============================================================
   SETTINGS
   ============================================================ */

async function loadSettings() {

    try {

        const response =
            await fetch(
                "api/settings",
                {
                    cache:
                        "no-store",
                }
            );

        const settings =
            await response.json();

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
                    Boolean(
                        value
                    );
            }
        };

        setValue(
            "set_format",
            settings.audio_format
                || "mp3"
        );

        setValue(
            "set_quality",
            settings.audio_quality
                || "320K"
        );

        setValue(
            "set_max_results",
            settings.max_results
                || 20
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
            settings.subsonic_user
                || "admin"
        );

        setValue(
            "amperfy-server-url",
            window.XrobArpeggi
                ?.getServerUrl?.()
                || (
                    location.protocol
                    + "//"
                    + location.hostname
                    + ":8100"
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

    const data = {

        audio_format:
            document
                .getElementById(
                    "set_format"
                )
                ?.value
            || "mp3",

        audio_quality:
            document
                .getElementById(
                    "set_quality"
                )
                ?.value
            || "320K",

        embed_thumbnail:
            document
                .getElementById(
                    "set_thumb"
                )
                ?.checked
            ?? true,

        embed_metadata:
            document
                .getElementById(
                    "set_meta"
                )
                ?.checked
            ?? true,

        organize_by_artist:
            document
                .getElementById(
                    "set_organize"
                )
                ?.checked
            ?? false,

        max_results:
            Number(
                document
                    .getElementById(
                        "set_max_results"
                    )
                    ?.value
                || 20
            ),
    };

    try {

        const response =
            await fetch(
                "api/settings",
                {
                    method:
                        "POST",

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

        const result =
            await response.json();

        if (!response.ok) {

            throw new Error(
                result.detail ||
                "Failed to save settings."
            );
        }

        document.getElementById(
            "settingsMsg"
        ).textContent =
            "✅ Settings saved.";

        showToast(
            "✅ Settings saved"
        );

    } catch (error) {

        document.getElementById(
            "settingsMsg"
        ).textContent =
            "❌ " +
            error.message;

        showToast(
            "❌ " +
            error.message
        );
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
                    cache:
                        "no-store",
                }
            );

        const data =
            await response.json();

        rawLibraryFiles =
            data.files || [];

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
                data.total_size
                || "0 MB";
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

        const response =
            await fetch(
                "api/stats",
                {
                    cache:
                        "no-store",
                }
            );

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
                stats.albums || 0,
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

    } catch {}
}


async function loadLibrary() {

    const list =
        document.getElementById(
            "libraryList"
        );

    list.innerHTML =
        '<div class="library-loading">Loading library...</div>';

    await refreshLibraryCache();
    await loadStats();

    filterLibrary();
}


function filterLibrary() {

    const list =
        document.getElementById(
            "libraryList"
        );

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
                    file.name
                )
                .toLowerCase()
                .includes(query)
        );

    list.innerHTML =
        "";

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
                    file.name
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

            card.innerHTML = `
                <div class="thumb-wrapper">

                    <img
                        src="${cover}"
                        alt=""
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
                            file.size
                        )}
                    </div>

                </div>

                <div class="btn-group">

                    <button
                        class="btn-preview"
                    >
                        ▶ Play
                    </button>

                    <button
                        class="btn-danger"
                    >
                        🗑 Delete
                    </button>

                </div>
            `;

            const play =
                card.querySelector(
                    ".btn-preview"
                );

            const remove =
                card.querySelector(
                    ".btn-danger"
                );

            play.onclick =
                () =>
                    toggleAudioStream(
                        play,
                        stream,
                        "library",
                        file.name,
                        "Local Library",
                        cover
                    );

            remove.onclick =
                () =>
                    deleteFile(
                        file.name
                    );

            list.appendChild(
                card
            );
        }
    );
}


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

        const response =
            await fetch(
                "api/library/"
                + encodeURIComponent(
                    filename
                ),
                {
                    method:
                        "DELETE",
                }
            );

        if (!response.ok) {

            const error =
                await response.json()
                    .catch(
                        () => ({})
                    );

            throw new Error(
                error.detail
                ||
                "Delete failed."
            );
        }

        showToast(
            "🗑 Track deleted"
        );

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

    const query =
        input.value.trim();

    if (!query) {

        status.textContent =
            "Enter a search term.";

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

    status.textContent =
        "🔍 Searching...";

    results.innerHTML =
        "";

    await refreshLibraryCache();

    const button =
        document.getElementById(
            "searchBtn"
        );

    button.disabled =
        true;

    try {

        const response =
            await fetch(
                `api/search?q=${
                    encodeURIComponent(
                        query
                    )
                }&page=1`
            );

        const data =
            await response.json();

        if (!response.ok) {

            throw new Error(
                data.detail ||
                "Search failed."
            );
        }

        if (!data.length) {

            status.textContent =
                "No results found.";

            hasMoreResults =
                false;

            return;
        }

        status.textContent =
            "";

        renderItems(
            data
        );

    } catch (error) {

        status.textContent =
            "❌ " +
            error.message;

    } finally {

        button.disabled =
            false;
    }
}


function renderItems(
    items
) {

    const results =
        document.getElementById(
            "results"
        );

    items.forEach(
        item => {

            const card =
                document.createElement(
                    "article"
                );

            card.className =
                "result-card";

            const title =
                escapeHtml(
                    item.title
                );

            const artist =
                escapeHtml(
                    item.channel
                );

            const thumbnail =
                escapeHtml(
                    item.thumbnail
                    || ""
                );

            card.innerHTML = `
                <div class="thumb-wrapper">

                    <img
                        src="${thumbnail}"
                        alt=""
                        onerror="
                            this.src='https://via.placeholder.com/100?text=Music'
                        "
                    >

                    <span class="badge-duration">
                        ${escapeHtml(
                            item.duration_text
                        )}
                    </span>

                </div>

                <div class="track-info">

                    <div class="track-title">
                        ${title}
                    </div>

                    <div class="track-artist">
                        👤 ${artist}
                    </div>

                </div>

                <div
                    class="btn-group"
                    data-group-id="${
                        escapeHtml(
                            item.id
                        )
                    }"
                ></div>
            `;

            const group =
                card.querySelector(
                    ".btn-group"
                );

            if (
                libraryFilesSet.has(
                    normalizeKey(
                        item.title
                    )
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

                preview.className =
                    "btn-preview";

                preview.dataset.type =
                    "search";

                preview.textContent =
                    "▶ Preview";

                preview.onclick =
                    () =>
                        toggleAudioStream(
                            preview,
                            "api/preview?url="
                            + encodeURIComponent(
                                item.url
                            ),
                            "search",
                            item.title,
                            item.channel,
                            item.thumbnail
                        );


                const download =
                    document.createElement(
                        "button"
                    );

                download.className =
                    "btn-download";

                download.dataset.id =
                    item.id;

                download.textContent =
                    "⬇️ Save";

                download.onclick =
                    () =>
                        startDownload(
                            item.url,
                            item.title,
                            item.id,
                            item.channel,
                            download
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
        isLoadingMore
        ||
        !hasMoreResults
        ||
        !currentQuery
    ) {
        return;
    }

    isLoadingMore =
        true;

    currentPage++;

    const loader =
        document.getElementById(
            "infiniteLoader"
        );

    loader.style.display =
        "block";

    try {

        const response =
            await fetch(
                `api/search?q=${
                    encodeURIComponent(
                        currentQuery
                    )
                }&page=${
                    currentPage
                }`
            );

        const data =
            await response.json();

        if (
            !data
            ||
            !data.length
        ) {

            hasMoreResults =
                false;

        } else {

            renderItems(
                data
            );
        }

    } catch {

        hasMoreResults =
            false;

    } finally {

        loader.style.display =
            "none";

        isLoadingMore =
            false;
    }
}


document.getElementById(
    "searchBtn"
)?.addEventListener(
    "click",
    searchMusic
);


document.getElementById(
    "query"
)?.addEventListener(
    "keydown",
    event => {

        if (
            event.key ===
            "Enter"
        ) {

            searchMusic();
        }
    }
);


/* ============================================================
   DOWNLOADS
   ============================================================ */

function isActiveTask(
    task
) {

    return [
        "queued",
        "downloading",
        "processing",
    ].includes(
        task.status
    );
}


function isFinishedTask(
    task
) {

    return [
        "completed",
        "error",
        "failed",
        "cancelled",
        "canceled",
    ].includes(
        task.status
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
        map[
            status
        ]
        ||
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
        id => {

            const element =
                document.getElementById(
                    id
                );

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
        statusClass,
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
                        task.percent
                        || 0
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
        task.id;

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
                            task.title
                            || "Unknown Track"
                        )}
                    </div>

                    <div class="download-artist">
                        ${escapeHtml(
                            task.artist
                            || "Unknown Artist"
                        )}
                    </div>

                </div>


                <div
                    class="download-status-wrap"
                >

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
                        task.error
                        || task.step
                        || ""
                    )}
                </div>

                <div class="download-meta">
                    ${escapeHtml(
                        task.speed
                        || ""
                    )}
                </div>

            </div>

        </div>


        <div class="download-actions">

            ${
                isActiveTask(task)
                    ? `
                        <button
                            class="btn-danger"
                            onclick="cancelTask('${escapeHtml(
                                task.id
                            )}')"
                        >
                            ✕ Cancel
                        </button>
                    `
                    : `
                        <button
                            class="download-remove-btn"
                            onclick="removeDownloadTask('${escapeHtml(
                                task.id
                            )}')"
                        >
                            Remove
                        </button>
                    `
            }

        </div>
    `;

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

            <button
                class="save-btn"
                onclick="navigate('search')"
            >
                🔍 Search Music
            </button>
        `;

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


function taskSignature(
    tasks
) {

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
                    task.last_updated,
                ].join(
                    "|"
                )
        )
        .sort()
        .join(
            ";"
        );
}


async function pollTasks(
    force = false
) {

    try {

        const response =
            await fetch(
                "api/tasks",
                {
                    cache:
                        "no-store",
                }
            );

        const tasks =
            await response.json();

        latestTasks =
            Array.isArray(
                tasks
            )
                ? tasks
                : [];

        latestTasks.forEach(
            task => {

                if (
                    task.status ===
                        "completed"
                    &&
                    !completedSet.has(
                        task.id
                    )
                ) {

                    completedSet.add(
                        task.id
                    );

                    showToast(
                        `🎉 ${
                            task.title
                            || "Track"
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
            force
            ||
            signature !==
                lastTaskSignature
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

    await pollTasks(
        true
    );

    await loadStats();
}


async function startDownload(
    url,
    title,
    elementId,
    artist,
    button
) {

    if (button) {

        button.disabled =
            true;

        button.textContent =
            "⏳ Queuing...";
    }

    try {

        const response =
            await fetch(
                "api/download",
                {
                    method:
                        "POST",

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

        const data =
            await response.json();

        if (!response.ok) {

            throw new Error(
                data.detail
                ||
                "Failed to queue download."
            );
        }

        showToast(
            data.status ===
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
            "❌ " +
            error.message
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

    try {

        const response =
            await fetch(
                `api/tasks/${
                    encodeURIComponent(
                        taskId
                    )
                }/cancel`,
                {
                    method:
                        "POST",
                }
            );

        const data =
            await response.json();

        if (!response.ok) {

            throw new Error(
                data.detail
                ||
                "Failed to cancel."
            );
        }

        showToast(
            "✕ Download cancelled"
        );

        await pollTasks(
            true
        );

    } catch (error) {

        showToast(
            "❌ " +
            error.message
        );
    }
}


async function removeDownloadTask(
    taskId
) {

    try {

        const response =
            await fetch(
                `api/tasks/${
                    encodeURIComponent(
                        taskId
                    )
                }`,
                {
                    method:
                        "DELETE",
                }
            );

        const data =
            await response.json();

        if (!response.ok) {

            throw new Error(
                data.detail
                ||
                "Failed to remove."
            );
        }

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
                    method:
                        "DELETE",

                    cache:
                        "no-store",
                }
            );

        const data =
            await response.json();

        if (!response.ok) {

            throw new Error(
                data.detail
                ||
                "Failed to clear."
            );
        }

        completedSet.clear();

        latestTasks =
            latestTasks.filter(
                task =>
                    ![
                        "completed",
                        "cancelled",
                        "canceled",
                        "error",
                        "failed",
                    ].includes(
                        task.status
                    )
            );

        lastTaskSignature =
            "";

        renderDownloads(
            latestTasks
        );

        showToast(
            `🧹 Cleared ${
                data.count
                || 0
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

async function loadHome() {

    try {

        const response =
            await fetch(
                "api/home",
                {
                    cache:
                        "no-store",
                }
            );

        const data =
            await response.json();

        const stats =
            data.stats || {};

        document.getElementById(
            "homeTracks"
        ).textContent =
            stats.tracks || 0;

        document.getElementById(
            "homeArtists"
        ).textContent =
            stats.artists || 0;

        document.getElementById(
            "homeAlbums"
        ).textContent =
            stats.albums || 0;

        document.getElementById(
            "homeDownloads"
        ).textContent =
            data.active_downloads || 0;


        const container =
            document.getElementById(
                "recentTracks"
            );

        container.innerHTML =
            "";

        const recent =
            data.recently_added || [];

        if (!recent.length) {

            container.innerHTML = `
                <div class="home-empty">
                    No music in your library yet.
                </div>
            `;

            return;
        }

        recent.forEach(
            track => {

                const card =
                    document.createElement(
                        "button"
                    );

                card.className =
                    "recent-card";

                card.innerHTML = `
                    <img
                        src="${escapeHtml(
                            track.cover
                        )}"
                        alt=""
                    >

                    <div class="recent-card-title">
                        ${escapeHtml(
                            track.title
                        )}
                    </div>

                    <div class="recent-card-artist">
                        ${escapeHtml(
                            track.artist
                        )}
                    </div>
                `;

                card.onclick =
                    () =>
                        toggleAudioStream(
                            card,
                            `rest/stream.view?id=${
                                encodeURIComponent(
                                    track.id
                                )
                            }`,
                            "home",
                            track.title,
                            track.artist,
                            track.cover
                        );

                container.appendChild(
                    card
                );
            }
        );

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

function initWebSocket() {

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

    } catch {

        return;
    }

    socket.onmessage =
        event => {

            try {

                const data =
                    JSON.parse(
                        event.data
                    );

                if (
                    data.type ===
                    "task_update"
                ) {

                    pollTasks();
                }

            } catch {}
        };

    socket.onclose =
        () => {

            setTimeout(
                initWebSocket,
                3000
            );
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
            searchTab
            &&
            searchTab.classList.contains(
                "active"
            )
            &&
            window.innerHeight
            + window.scrollY
            >=
            document.body.offsetHeight
            - 500
        ) {

            loadMoreResults();
        }
    },
    {
        passive:
            true,
    }
);


/* ============================================================
   STARTUP
   ============================================================ */

async function initializeApp() {

    await refreshLibraryCache();

    await pollTasks(
        true
    );

    await loadStats();

    handleHash();

    initWebSocket();

    setInterval(
        () =>
            pollTasks(),
        2000
    );
}


document.addEventListener(
    "DOMContentLoaded",
    initializeApp
);
