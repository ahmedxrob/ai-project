/* ============================================================
   XROB MUSIC - MAIN APP.JS
   Fixed version - preserves existing functionality
   ============================================================ */


/* ============================================================
   GLOBAL STATE
   ============================================================ */

let socket = null;

let socketReconnectTimer = null;

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

let audio =
    null;

let player =
    null;

let playBtn =
    null;

let seek =
    null;

let volume =
    null;

let curTime =
    null;

let durTime =
    null;

let playerTitle =
    null;

let playerArtist =
    null;

let playerArt =
    null;

let canvas =
    null;

let canvasCtx =
    null;

let audioContext =
    null;

let analyser =
    null;

let sourceNode =
    null;

let visualizerFrame =
    null;

let initialized =
    false;


/* ============================================================
   DOM REFERENCES
   ============================================================ */

function cacheDomElements() {

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
        String(message ?? "");

    container.appendChild(
        toast
    );

    setTimeout(
        () => {

            if (toast.parentNode) {
                toast.remove();
            }

        },
        3500
    );
}

window.showToast =
    showToast;


async function parseJsonResponse(
    response
) {

    const contentType =
        response.headers.get(
            "content-type"
        ) || "";

    if (
        contentType.includes(
            "application/json"
        )
    ) {

        return await response.json();
    }

    const text =
        await response.text();

    try {
        return JSON.parse(text);
    } catch {
        return {
            detail:
                text ||
                `Request failed with HTTP ${response.status}.`
        };
    }
}


/* ============================================================
   THEME
   ============================================================ */

function toggleTheme(
    theme
) {

    const selectedTheme =
        theme === "light"
            ? "light"
            : "dark";

    document.documentElement
        .setAttribute(
            "data-theme",
            selectedTheme
        );

    try {

        localStorage.setItem(
            "xrob_music_theme",
            selectedTheme
        );

    } catch {}
}


function initializeTheme() {

    let savedTheme =
        "dark";

    try {

        savedTheme =
            localStorage.getItem(
                "xrob_music_theme"
            ) || "dark";

    } catch {}

    toggleTheme(
        savedTheme
    );
}


/* ============================================================
   NAVIGATION
   ============================================================ */

function navigate(
    tab,
    updateHash = true
) {

    if (!tab) {
        return;
    }

    if (updateHash) {

        const newHash =
            String(tab);

        if (
            location.hash.replace(
                "#",
                ""
            ) !== newHash
        ) {

            location.hash =
                newHash;

        } else {

            switchTab(
                newHash
            );
        }

    } else {

        switchTab(
            tab
        );
    }
}


function switchTab(
    tab
) {

    const validTabs = [
        "home",
        "search",
        "downloads",
        "library",
        "settings",
    ];

    if (
        !validTabs.includes(tab)
    ) {

        tab =
            "home";
    }

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
            Number(seconds) || 0
        );

    return (
        Math.floor(
            seconds / 60
        )
        +
        ":"
        +
        String(
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
        !seek ||
        !curTime ||
        !durTime
    ) {
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

        durTime.textContent =
            "0:00";

        return;
    }

    const percentage =
        (
            audio.currentTime /
            audio.duration
        )
        * 100;

    seek.value =
        Math.max(
            0,
            Math.min(
                100,
                percentage
            )
        );

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
            Boolean(playing)
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

    const type =
        button.dataset.type ||
        "search";

    if (
        type === "library"
    ) {

        button.textContent =
            "▶ Play";

    } else if (
        type === "home"
    ) {

        button.textContent =
            "▶ Play";

    } else {

        button.textContent =
            "▶ Preview";
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

        audioContext =
            null;

        analyser =
            null;

        sourceNode =
            null;
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

    visualizerFrame =
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
                *
                canvas.height
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

        playerArt.onerror =
            () => {

                playerArt.onerror =
                    null;

                playerArt.src =
                    "https://via.placeholder.com/60?text=Music";
            };
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
        audioContext.state ===
            "suspended"
    ) {

        audioContext
            .resume()
            .catch(
                error =>
                    console.warn(
                        "AudioContext resume:",
                        error
                    )
            );
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
        activePreviewBtn ===
            button
        &&
        audio.src ===
            absoluteUrl
    ) {

        if (audio.paused) {

            audio.play().catch(
                error => {

                    console.error(
                        "Playback failed:",
                        error
                    );

                    showToast(
                        "❌ Unable to play audio"
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


    activePreviewBtn =
        button;

    button.dataset.type =
        type ||
        "search";

    button.classList.remove(
        "playing"
    );

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

    audio.removeAttribute(
        "src"
    );

    audio.src =
        absoluteUrl;

    audio.load();


    audio.play()
        .then(
            () => {

                button.textContent =
                    "❚❚ Pause";

                button.classList.add(
                    "playing"
                );
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

                button.classList.remove(
                    "playing"
                );

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
        );
}


function initializePlayerEvents() {

    if (!audio) {
        return;
    }


    audio.addEventListener(
        "timeupdate",
        updateProgress
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
        () =>
            updatePlayingState(
                true
            )
    );

    audio.addEventListener(
        "pause",
        () =>
            updatePlayingState(
                false
            )
    );

    audio.addEventListener(
        "ended",
        () => {

            updatePlayingState(
                false
            );

            if (seek) {

                seek.value =
                    0;
            }

            if (curTime) {

                curTime.textContent =
                    "0:00";
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

    audio.addEventListener(
        "error",
        () => {

            console.warn(
                "Audio element error:",
                audio.error
            );

            if (activePreviewBtn) {

                const failedButton =
                    activePreviewBtn;

                failedButton.textContent =
                    "❌ Error";

                setTimeout(
                    () =>
                        resetPreviewButton(
                            failedButton
                        ),
                    1800
                );
            }
        }
    );


    playBtn?.addEventListener(
        "click",
        () => {

            if (!audio) {
                return;
            }

            if (!audio.src) {

                showToast(
                    "🎵 Select a track first"
                );

                return;
            }

            if (audio.paused) {

                audio.play().catch(
                    error => {

                        console.error(
                            error
                        );

                        showToast(
                            "❌ Unable to play audio"
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
                audio &&
                Number.isFinite(
                    audio.duration
                )
            ) {

                audio.currentTime =
                    (
                        Number(
                            seek.value
                        ) /
                        100
                    )
                    *
                    audio.duration;
            }
        }
    );


    let savedVolume =
        null;

    try {

        savedVolume =
            localStorage.getItem(
                "xrob_music_volume"
            );

    } catch {}


    if (
        savedVolume !== null &&
        volume
    ) {

        const numericVolume =
            Math.max(
                0,
                Math.min(
                    1,
                    Number(savedVolume)
                )
            );

        volume.value =
            numericVolume;

        audio.volume =
            numericVolume;
    }


    if (
        volume &&
        audio
    ) {

        if (
            savedVolume === null
        ) {

            audio.volume =
                Number(
                    volume.value || 1
                );
        }

        volume.addEventListener(
            "input",
            () => {

                const newVolume =
                    Math.max(
                        0,
                        Math.min(
                            1,
                            Number(
                                volume.value
                            )
                        )
                    );

                audio.volume =
                    newVolume;

                try {

                    localStorage.setItem(
                        "xrob_music_volume",
                        String(
                            newVolume
                        )
                    );

                } catch {}
            }
        );
    }
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
                    cache:
                        "no-store",
                }
            );

        const settings =
            await parseJsonResponse(
                response
            );

        if (!response.ok) {

            throw new Error(
                settings.detail ||
                "Failed to load settings."
            );
        }


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
                ||
                (
                    location.protocol
                    +
                    "//"
                    +
                    location.hostname
                    +
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
        document
            .getElementById(id)
            ?.value;

    const getChecked = id =>
        document
            .getElementById(id)
            ?.checked;


    const data = {

        audio_format:
            getValue(
                "set_format"
            )
            || "mp3",

        audio_quality:
            getValue(
                "set_quality"
            )
            || "320K",

        embed_thumbnail:
            getChecked(
                "set_thumb"
            )
            ?? true,

        embed_metadata:
            getChecked(
                "set_meta"
            )
            ?? true,

        organize_by_artist:
            getChecked(
                "set_organize"
            )
            ?? false,

        max_results:
            Number(
                getValue(
                    "set_max_results"
                )
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
            await parseJsonResponse(
                response
            );


        if (!response.ok) {

            throw new Error(
                result.detail ||
                "Failed to save settings."
            );
        }


        const message =
            document.getElementById(
                "settingsMsg"
            );

        if (message) {

            message.textContent =
                "✅ Settings saved.";
        }

        showToast(
            "✅ Settings saved"
        );

    } catch (error) {

        const message =
            document.getElementById(
                "settingsMsg"
            );

        if (message) {

            message.textContent =
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
            await parseJsonResponse(
                response
            );


        if (!response.ok) {

            throw new Error(
                data.detail ||
                "Failed to load library."
            );
        }


        rawLibraryFiles =
            Array.isArray(
                data.files
            )
                ? data.files
                : [];


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
            await parseJsonResponse(
                response
            );


        if (!response.ok) {
            return;
        }


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

            const filename =
                String(
                    file.name || ""
                );

            const encoded =
                encodeURIComponent(
                    filename
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
                        src="${escapeHtml(cover)}"
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
                        📦 ${escapeHtml(
                            file.size
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

                play.onclick =
                    () =>
                        toggleAudioStream(
                            play,
                            stream,
                            "library",
                            filename,
                            "Local Library",
                            cover
                        );
            }


            if (remove) {

                remove.onclick =
                    () =>
                        deleteFile(
                            filename
                        );
            }


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
                +
                encodeURIComponent(
                    filename
                ),
                {
                    method:
                        "DELETE",
                }
            );


        const result =
            await parseJsonResponse(
                response
            );


        if (!response.ok) {

            throw new Error(
                result.detail ||
                "Delete failed."
            );
        }


        if (
            activePreviewBtn &&
            activePreviewBtn.dataset.type ===
                "library"
        ) {

            if (audio) {
                audio.pause();
            }

            activePreviewBtn =
                null;
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

    if (!input || !results) {
        return;
    }


    const query =
        input.value.trim();


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
            "🔍 Searching...";
    }


    results.innerHTML =
        "";


    await refreshLibraryCache();


    const button =
        document.getElementById(
            "searchBtn"
        );


    if (button) {

        button.disabled =
            true;
    }


    try {

        const response =
            await fetch(
                `api/search?q=${
                    encodeURIComponent(
                        query
                    )
                }&page=1`,
                {
                    cache:
                        "no-store",
                }
            );


        const data =
            await parseJsonResponse(
                response
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

    } catch (error) {

        if (status) {

            status.textContent =
                "❌ " +
                error.message;
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

    if (!results) {
        return;
    }


    if (!Array.isArray(items)) {
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


            const title =
                escapeHtml(
                    item.title ||
                    "Unknown Track"
                );

            const artist =
                escapeHtml(
                    item.channel ||
                    item.artist ||
                    "Unknown Artist"
                );

            const thumbnail =
                escapeHtml(
                    item.thumbnail ||
                    ""
                );

            const duration =
                escapeHtml(
                    item.duration_text ||
                    ""
                );

            const itemId =
                escapeHtml(
                    item.id ||
                    ""
                );


            const safeThumbnail =
                thumbnail ||
                "https://via.placeholder.com/100?text=Music";


            card.innerHTML = `
                <div class="thumb-wrapper">

                    <img
                        src="${safeThumbnail}"
                        alt=""
                    >

                    ${
                        duration
                            ? `
                                <span class="badge-duration">
                                    ${duration}
                                </span>
                            `
                            : ""
                    }

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
                    data-group-id="${itemId}"
                ></div>
            `;


            const image =
                card.querySelector(
                    "img"
                );


            if (image) {

                image.addEventListener(
                    "error",
                    () => {

                        image.onerror =
                            null;

                        image.src =
                            "https://via.placeholder.com/100?text=Music";
                    }
                );
            }


            const group =
                card.querySelector(
                    ".btn-group"
                );


            if (!group) {
                return;
            }


            const normalizedTitle =
                normalizeKey(
                    item.title
                );


            if (
                normalizedTitle &&
                libraryFilesSet.has(
                    normalizedTitle
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


                preview.onclick =
                    () =>
                        toggleAudioStream(
                            preview,
                            "api/preview?url="
                            +
                            encodeURIComponent(
                                item.url ||
                                ""
                            ),
                            "search",
                            item.title ||
                                "Unknown Track",
                            item.channel ||
                                item.artist ||
                                "Unknown Artist",
                            item.thumbnail ||
                                ""
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


                download.onclick =
                    () =>
                        startDownload(
                            item.url,
                            item.title,
                            item.id,
                            item.channel ||
                                item.artist ||
                                "",
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
        isLoadingMore ||
        !hasMoreResults ||
        !currentQuery
    ) {
        return;
    }


    isLoadingMore =
        true;


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
                    cache:
                        "no-store",
                }
            );


        const data =
            await parseJsonResponse(
                response
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

    } finally {

        if (loader) {

            loader.style.display =
                "none";
        }

        isLoadingMore =
            false;
    }
}


/* ============================================================
   SEARCH EVENTS
   ============================================================ */

function initializeSearchEvents() {

    const searchBtn =
        document.getElementById(
            "searchBtn"
        );

    const queryInput =
        document.getElementById(
            "query"
        );


    searchBtn?.addEventListener(
        "click",
        searchMusic
    );


    queryInput?.addEventListener(
        "keydown",
        event => {

            if (
                event.key ===
                "Enter"
            ) {

                event.preventDefault();

                searchMusic();
            }
        }
    );


    const librarySearch =
        document.getElementById(
            "libSearchQuery"
        );


    librarySearch?.addEventListener(
        "input",
        filterLibrary
    );
}


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
        task?.status
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
        task?.status
    );
}


function getTaskStatus(
    status
) {

    const normalizedStatus =
        String(
            status || "queued"
        ).toLowerCase();


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
            normalizedStatus
        ]
        ||
        map.queued
    );
}


function updateQueueCounters(
    tasks
) {

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

    task =
        task || {};


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
                        task.percent ||
                        0
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


    const taskId =
        escapeHtml(
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


        <div class="download-actions">

            ${
                isActiveTask(task)
                    ? `
                        <button
                            type="button"
                            class="btn-danger download-cancel-btn"
                        >
                            ✕ Cancel
                        </button>
                    `
                    : `
                        <button
                            type="button"
                            class="download-remove-btn"
                        >
                            Remove
                        </button>
                    `
            }

        </div>
    `;


    const cancelButton =
        card.querySelector(
            ".download-cancel-btn"
        );


    if (cancelButton) {

        cancelButton.onclick =
            () =>
                cancelTask(
                    task.id
                );
    }


    const removeButton =
        card.querySelector(
            ".download-remove-btn"
        );


    if (removeButton) {

        removeButton.onclick =
            () =>
                removeDownloadTask(
                    task.id
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
                type="button"
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

    return (
        Array.isArray(tasks)
            ? tasks
            : []
    )
        .map(
            task =>
                [
                    task?.id,
                    task?.status,
                    task?.percent,
                    task?.speed,
                    task?.step,
                    task?.error,
                    task?.last_updated,
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
            await parseJsonResponse(
                response
            );


        if (!response.ok) {

            throw new Error(
                tasks.detail ||
                "Failed to load tasks."
            );
        }


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

    if (!url) {

        showToast(
            "❌ No download URL available"
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
            await parseJsonResponse(
                response
            );


        if (!response.ok) {

            throw new Error(
                data.detail ||
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

    if (!taskId) {
        return;
    }


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
            await parseJsonResponse(
                response
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

    if (!taskId) {
        return;
    }


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
            await parseJsonResponse(
                response
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
            await parseJsonResponse(
                response
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


        updateQueueCounters(
            latestTasks
        );


        showToast(
            `🧹 Cleared ${
                data.count ||
                0
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
            await parseJsonResponse(
                response
            );


        if (!response.ok) {

            throw new Error(
                data.detail ||
                "Failed to load home."
            );
        }


        const stats =
            data.stats || {};


        const homeTracks =
            document.getElementById(
                "homeTracks"
            );

        if (homeTracks) {

            homeTracks.textContent =
                stats.tracks || 0;
        }


        const homeArtists =
            document.getElementById(
                "homeArtists"
            );

        if (homeArtists) {

            homeArtists.textContent =
                stats.artists || 0;
        }


        const homeAlbums =
            document.getElementById(
                "homeAlbums"
            );

        if (homeAlbums) {

            homeAlbums.textContent =
                stats.albums || 0;
        }


        const homeDownloads =
            document.getElementById(
                "homeDownloads"
            );

        if (homeDownloads) {

            homeDownloads.textContent =
                data.active_downloads || 0;
        }


        const container =
            document.getElementById(
                "recentTracks"
            );


        if (!container) {
            return;
        }


        container.innerHTML =
            "";


        const recent =
            Array.isArray(
                data.recently_added
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


        recent.forEach(
            track => {

                if (!track) {
                    return;
                }


                const card =
                    document.createElement(
                        "button"
                    );

                card.type =
                    "button";

                card.className =
                    "recent-card";


                const cover =
                    escapeHtml(
                        track.cover ||
                        "https://via.placeholder.com/100?text=Music"
                    );


                card.innerHTML = `
                    <img
                        src="${cover}"
                        alt=""
                    >

                    <div class="recent-card-title">
                        ${escapeHtml(
                            track.title ||
                            "Unknown Track"
                        )}
                    </div>

                    <div class="recent-card-artist">
                        ${escapeHtml(
                            track.artist ||
                            "Unknown Artist"
                        )}
                    </div>
                `;


                const image =
                    card.querySelector(
                        "img"
                    );


                image?.addEventListener(
                    "error",
                    () => {

                        image.onerror =
                            null;

                        image.src =
                            "https://via.placeholder.com/100?text=Music";
                    }
                );


                card.dataset.type =
                    "home";


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

    if (
        socket &&
        (
            socket.readyState ===
                WebSocket.OPEN
            ||
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

            if (
                socketReconnectTimer
            ) {

                clearTimeout(
                    socketReconnectTimer
                );

                socketReconnectTimer =
                    null;
            }
        };


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


            } catch (error) {

                console.warn(
                    "Invalid WebSocket message:",
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

            socket =
                null;

            scheduleWebSocketReconnect();
        };
}


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


/* ============================================================
   INFINITE SCROLL
   ============================================================ */

function initializeInfiniteScroll() {

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
                +
                window.scrollY
                >=
                document.body.offsetHeight
                -
                500
            ) {

                loadMoreResults();
            }

        },
        {
            passive:
                true,
        }
    );
}


/* ============================================================
   STARTUP
   ============================================================ */

async function initializeApp() {

    if (initialized) {
        return;
    }

    initialized =
        true;


    cacheDomElements();

    initializeTheme();

    initializePlayerEvents();

    initializeSearchEvents();

    initializeInfiniteScroll();


    try {

        await refreshLibraryCache();

    } catch (error) {

        console.warn(
            "Initial library load:",
            error
        );
    }


    try {

        await pollTasks(
            true
        );

    } catch (error) {

        console.warn(
            "Initial task load:",
            error
        );
    }


    try {

        await loadStats();

    } catch (error) {

        console.warn(
            "Initial stats load:",
            error
        );
    }


    handleHash();

    initWebSocket();


    setInterval(
        () =>
            pollTasks(),
        2000
    );
}


/* ============================================================
   DOM READY
   ============================================================ */

if (
    document.readyState ===
    "loading"
) {

    document.addEventListener(
        "DOMContentLoaded",
        initializeApp,
        {
            once:
                true,
        }
    );

} else {

    initializeApp();
}
