/* ============================================================
   XROB MUSIC — MAIN FRONTEND
   Home + Search + Downloads + Library + Settings + Player
   ============================================================ */

let socket = null;
let pollTimer = null;

let completedSet = new Set();

let libraryFilesSet = new Set();
let rawLibraryFiles = [];

let latestTasks = [];
let lastRenderedTaskSignature = "";

let activePreviewBtn = null;

let currentPage = 1;
let currentQuery = "";
let isLoadingMore = false;
let hasMoreResults = true;


/* ============================================================
   PLAYER
   ============================================================ */

const globalAudio =
    document.getElementById("global-audio-element");

const gpBar =
    document.getElementById("global-player-bar");

const gpPlayBtn =
    document.getElementById("gp-play-btn");

const gpSeek =
    document.getElementById("gp-seek");

const gpVolume =
    document.getElementById("gp-volume");

const gpCurTime =
    document.getElementById("gp-cur-time");

const gpDurTime =
    document.getElementById("gp-dur-time");

const gpTitle =
    document.getElementById("gp-title");

const gpArtist =
    document.getElementById("gp-artist");

const gpArt =
    document.getElementById("gp-art");

const visualizerCanvas =
    document.getElementById(
        "visualizer-canvas"
    );

const visualizerCtx =
    visualizerCanvas
        ? visualizerCanvas.getContext("2d")
        : null;

let audioCtx = null;
let analyser = null;
let sourceNode = null;


/* ============================================================
   PLAYER HELPERS
   ============================================================ */

function formatSecs(seconds) {

    seconds =
        Math.floor(seconds || 0);

    const minutes =
        Math.floor(
            seconds / 60
        );

    const secs =
        seconds % 60;

    return (
        `${minutes}:` +
        `${secs < 10 ? "0" : ""}` +
        `${secs}`
    );
}


function initAudioContext() {

    if (
        audioCtx ||
        !globalAudio
    ) {
        return;
    }

    try {

        audioCtx =
            new (
                window.AudioContext ||
                window.webkitAudioContext
            )();

        analyser =
            audioCtx.createAnalyser();

        analyser.fftSize = 64;

        analyser.smoothingTimeConstant =
            .8;

        sourceNode =
            audioCtx.createMediaElementSource(
                globalAudio
            );

        sourceNode.connect(
            analyser
        );

        analyser.connect(
            audioCtx.destination
        );

        drawVisualizer();

    } catch (error) {

        console.warn(
            "Visualizer unavailable:",
            error
        );
    }
}


function drawVisualizer() {

    if (
        !visualizerCtx ||
        !analyser ||
        !visualizerCanvas
    ) {
        return;
    }

    requestAnimationFrame(
        drawVisualizer
    );

    const size =
        analyser.frequencyBinCount;

    const data =
        new Uint8Array(size);

    analyser.getByteFrequencyData(
        data
    );

    visualizerCtx.clearRect(
        0,
        0,
        visualizerCanvas.width,
        visualizerCanvas.height
    );

    const width =
        (
            visualizerCanvas.width /
            size
        ) * 1.6;

    let x = 0;

    for (
        let i = 0;
        i < size;
        i++
    ) {

        const height =
            Math.max(
                2,
                (
                    data[i] / 255
                ) *
                visualizerCanvas.height
            );

        visualizerCtx.fillStyle =
            "#1ed760";

        visualizerCtx.fillRect(
            x,
            visualizerCanvas.height - height,
            Math.max(
                1,
                width - 1
            ),
            height
        );

        x += width;
    }
}


function updatePlayerProgress() {

    if (!globalAudio) {
        return;
    }

    const duration =
        globalAudio.duration;

    if (
        !duration ||
        !isFinite(duration)
    ) {

        if (gpSeek) {
            gpSeek.value = 0;
        }

        if (gpCurTime) {
            gpCurTime.textContent =
                "0:00";
        }

        return;
    }

    const percentage =
        (
            globalAudio.currentTime /
            duration
        ) * 100;

    if (gpSeek) {
        gpSeek.value =
            percentage;
    }

    if (gpCurTime) {
        gpCurTime.textContent =
            formatSecs(
                globalAudio.currentTime
            );
    }

    if (gpDurTime) {
        gpDurTime.textContent =
            formatSecs(
                duration
            );
    }
}


function updatePlayerState(
    playing
) {

    if (gpPlayBtn) {

        gpPlayBtn.textContent =
            playing
                ? "❚❚"
                : "▶";
    }

    if (gpBar) {

        gpBar.classList.toggle(
            "is-playing",
            playing
        );
    }

    if (activePreviewBtn) {

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


function updatePlayerInfo(
    title,
    artist,
    cover
) {

    if (gpTitle) {
        gpTitle.textContent =
            title ||
            "Unknown Track";
    }

    if (gpArtist) {
        gpArtist.textContent =
            artist ||
            "Unknown Artist";
    }

    if (gpArt) {

        gpArt.src =
            cover ||
            "https://via.placeholder.com/60?text=🎵";
    }
}


if (globalAudio) {

    globalAudio.addEventListener(
        "timeupdate",
        updatePlayerProgress
    );

    globalAudio.addEventListener(
        "loadedmetadata",
        updatePlayerProgress
    );

    globalAudio.addEventListener(
        "play",
        () =>
            updatePlayerState(true)
    );

    globalAudio.addEventListener(
        "pause",
        () =>
            updatePlayerState(false)
    );

    globalAudio.addEventListener(
        "ended",
        () => {

            updatePlayerState(false);

            if (gpSeek) {
                gpSeek.value = 0;
            }

            if (gpCurTime) {
                gpCurTime.textContent =
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
}


if (gpPlayBtn) {

    gpPlayBtn.onclick =
        async () => {

            if (!globalAudio) {
                return;
            }

            initAudioContext();

            if (
                audioCtx &&
                audioCtx.state ===
                    "suspended"
            ) {

                await audioCtx.resume();
            }

            if (
                globalAudio.paused
            ) {

                try {
                    await globalAudio.play();
                } catch (error) {
                    console.error(
                        error
                    );
                }

            } else {

                globalAudio.pause();
            }
        };
}


if (gpSeek) {

    gpSeek.addEventListener(
        "input",
        () => {

            if (
                globalAudio &&
                globalAudio.duration &&
                isFinite(
                    globalAudio.duration
                )
            ) {

                globalAudio.currentTime =
                    (
                        Number(
                            gpSeek.value
                        ) / 100
                    ) *
                    globalAudio.duration;
            }
        }
    );
}


if (gpVolume) {

    const saved =
        localStorage.getItem(
            "xrob_music_volume"
        );

    if (saved !== null) {

        gpVolume.value =
            saved;

        if (globalAudio) {
            globalAudio.volume =
                Number(saved);
        }

    } else if (globalAudio) {

        globalAudio.volume =
            .8;
    }

    gpVolume.addEventListener(
        "input",
        () => {

            const volume =
                Number(
                    gpVolume.value
                );

            if (globalAudio) {
                globalAudio.volume =
                    volume;
            }

            localStorage.setItem(
                "xrob_music_volume",
                String(volume)
            );
        }
    );
}


function toggleAudioStream(
    button,
    url,
    type,
    title,
    artist,
    cover
) {

    if (
        !globalAudio ||
        !button
    ) {
        return;
    }

    initAudioContext();

    if (
        audioCtx &&
        audioCtx.state ===
            "suspended"
    ) {

        audioCtx.resume();
    }


    let absolute;

    try {

        absolute =
            new URL(
                url,
                window.location.href
            ).href;

    } catch {
        return;
    }


    if (
        activePreviewBtn === button &&
        globalAudio.src === absolute
    ) {

        if (
            globalAudio.paused
        ) {

            globalAudio.play()
                .catch(
                    console.error
                );

        } else {

            globalAudio.pause();
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
        cover
    );


    if (gpBar) {

        gpBar.style.display =
            "grid";
    }


    globalAudio.pause();

    globalAudio.src =
        url;

    globalAudio.load();

    globalAudio.play()
        .then(
            () => {
                button.textContent =
                    "❚❚ Pause";
            }
        )
        .catch(
            error => {

                console.error(
                    error
                );

                button.textContent =
                    "❌ Error";

                setTimeout(
                    () => {
                        resetPreviewButton(
                            button
                        );
                    },
                    1800
                );
            }
        );
}


/* ============================================================
   TOAST
   ============================================================ */

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
        () => {

            toast.style.opacity =
                "0";

            setTimeout(
                () =>
                    toast.remove(),
                250
            );

        },
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

        window.location.hash =
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
            button => {

                button.classList.remove(
                    "active"
                );
            }
        );


    const section =
        document.getElementById(
            `tab-${tab}`
        );


    if (section) {
        section.classList.add(
            "active"
        );
    }


    const desktop =
        document.getElementById(
            `btn-${tab}`
        );

    const mobile =
        document.getElementById(
            `mob-btn-${tab}`
        );


    if (desktop) {
        desktop.classList.add(
            "active"
        );
    }

    if (mobile) {
        mobile.classList.add(
            "active"
        );
    }


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


function handleDeepLink() {

    const hash =
        location.hash.replace(
            "#",
            ""
        );

    const allowed = [
        "home",
        "search",
        "downloads",
        "library",
        "settings",
    ];

    switchTab(
        allowed.includes(
            hash
        )
            ? hash
            : "home"
    );
}


window.addEventListener(
    "hashchange",
    handleDeepLink
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


        const setValue =
            (
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


        const setChecked =
            (
                id,
                value
            ) => {

                const element =
                    document.getElementById(
                        id
                    );

                if (element) {
                    element.checked =
                        !!value;
                }
            };


        setValue(
            "set_theme",
            localStorage.getItem(
                "xrob_music_theme"
            ) || "dark"
        );

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
            "set_max_results",
            settings.max_results ||
                20
        );

        setValue(
            "set_subsonic_user",
            settings.subsonic_user ||
                "admin"
        );

        setValue(
            "set_subsonic_password",
            settings.subsonic_password ||
                ""
        );

        setValue(
            "amperfy-server-url",
            location.origin
        );

    } catch (error) {

        console.warn(
            error
        );
    }
}


async function saveSettings() {

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
            Number(
                document.getElementById(
                    "set_max_results"
                )?.value ||
                20
            ),

        subsonic_user:
            document.getElementById(
                "set_subsonic_user"
            )?.value ||
            "admin",

        subsonic_password:
            document.getElementById(
                "set_subsonic_password"
            )?.value ||
            "",
    };


    const message =
        document.getElementById(
            "settingsMsg"
        );


    if (message) {
        message.textContent =
            "Saving...";
    }


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


        if (!response.ok) {
            throw new Error(
                "Failed to save settings."
            );
        }


        if (message) {
            message.textContent =
                "✅ Settings saved.";
        }


        showToast(
            "✅ Settings saved"
        );


        setTimeout(
            () => {

                if (message) {
                    message.textContent =
                        "";
                }

            },
            3000
        );


        if (
            window.XrobArpeggi
        ) {

            window.XrobArpeggi.refreshStatus();
        }

    } catch (error) {

        if (message) {
            message.textContent =
                "❌ Failed to save settings.";
        }


        showToast(
            "❌ Failed to save settings"
        );
    }
}


function requestNotificationPermission() {

    if (
        !("Notification" in window)
    ) {

        showToast(
            "Notifications are not supported."
        );

        return;
    }


    Notification
        .requestPermission()
        .then(
            permission => {

                showToast(
                    permission ===
                        "granted"
                        ? "✅ Notifications enabled"
                        : "⚠️ Notifications denied"
                );
            }
        );
}


/* ============================================================
   LIBRARY CACHE
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


        const count =
            rawLibraryFiles.length;


        const sideCount =
            document.getElementById(
                "sideLibCount"
            );

        const mobileCount =
            document.getElementById(
                "mobLibCount"
            );

        const size =
            document.getElementById(
                "libFolderSize"
            );


        if (sideCount) {
            sideCount.textContent =
                count;
        }

        if (mobileCount) {
            mobileCount.textContent =
                count;
        }

        if (size) {
            size.textContent =
                data.total_size ||
                "0 MB";
        }


    } catch (error) {

        console.warn(
            "Library cache:",
            error
        );
    }
}


function normalizeKey(
    value
) {

    return (
        String(
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
        )
    );
}


function escapeHtml(
    value
) {

    return String(
        value ?? ""
    )
        .replace(
            /&/g,
            "&amp;"
        )
        .replace(
            /</g,
            "&lt;"
        )
        .replace(
            />/g,
            "&gt;"
        )
        .replace(
            /"/g,
            "&quot;"
        )
        .replace(
            /'/g,
            "&#039;"
        );
}


/* ============================================================
   SEARCH
   ============================================================ */

function renderLibraryBadge(
    group
) {

    if (!group) {
        return;
    }

    group.innerHTML = `
        <div class="badge-library">
            ✅ In Library
        </div>
    `;
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


    for (
        const item of items
    ) {

        const card =
            document.createElement(
                "article"
            );

        card.className =
            "result-card";

        card.dataset.elementId =
            item.id || "";


        const title =
            escapeHtml(
                item.title ||
                "Unknown Track"
            );

        const artist =
            escapeHtml(
                item.channel ||
                "Unknown Artist"
            );

        const thumbnail =
            escapeHtml(
                item.thumbnail ||
                ""
            );


        card.innerHTML = `

            <div class="thumb-wrapper">

                <img
                    src="${thumbnail}"
                    alt="${title}"
                    onerror="
                        this.src='https://via.placeholder.com/110x110?text=Music'
                    "
                >

                ${
                    item.duration_text
                        ? `
                            <span class="badge-duration">
                                ${escapeHtml(
                                    item.duration_text
                                )}
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
                data-group-id="${escapeHtml(
                    item.id || ""
                )}"
            ></div>
        `;


        const group =
            card.querySelector(
                ".btn-group"
            );


        const inLibrary =
            libraryFilesSet.has(
                normalizeKey(
                    item.title
                )
            );


        if (inLibrary) {

            renderLibraryBadge(
                group
            );

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
                        "api/preview?url=" +
                            encodeURIComponent(
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
                item.id || "";

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
}


async function searchMusic() {

    const input =
        document.getElementById(
            "query"
        );

    const status =
        document.getElementById(
            "statusMsg"
        );

    const results =
        document.getElementById(
            "results"
        );


    const query =
        input
            ? input.value.trim()
            : "";


    if (!query) {

        if (status) {
            status.textContent =
                "Enter a search term.";
        }

        return;
    }


    currentQuery =
        query;

    currentPage = 1;
    hasMoreResults = true;
    isLoadingMore = false;


    if (status) {
        status.textContent =
            "🔍 Searching...";
    }

    if (results) {
        results.innerHTML =
            "";
    }


    await refreshLibraryCache();


    const searchButton =
        document.getElementById(
            "searchBtn"
        );


    if (searchButton) {
        searchButton.disabled =
            true;
    }


    try {

        const response =
            await fetch(
                `api/search?q=${encodeURIComponent(
                    query
                )}&page=1`
            );


        const data =
            await response.json();


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

        if (searchButton) {
            searchButton.disabled =
                false;
        }
    }
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
    currentPage++;


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
                `api/search?q=${encodeURIComponent(
                    currentQuery
                )}&page=${currentPage}`
            );


        const data =
            await response.json();


        if (
            !Array.isArray(data) ||
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

        if (loader) {
            loader.style.display =
                "none";
        }

        isLoadingMore =
            false;
    }
}


const searchButton =
    document.getElementById(
        "searchBtn"
    );

if (searchButton) {
    searchButton.onclick =
        searchMusic;
}


const queryInput =
    document.getElementById(
        "query"
    );

if (queryInput) {

    queryInput.addEventListener(
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


function taskStatusMeta(
    task
) {

    const status =
        task.status ||
        "queued";


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

    const active =
        tasks.filter(
            isActiveTask
        ).length;


    const ids = [
        "queueCount",
        "mobQueueCount",
        "downloadQueueCount",
        "homeDownloads",
    ];


    ids.forEach(
        id => {

            const element =
                document.getElementById(
                    id
                );

            if (element) {
                element.textContent =
                    active;
            }
        }
    );
}


function buildDownloadCard(
    task,
    queuePosition = null
) {

    const [
        label,
        icon,
        statusClass,
    ] =
        taskStatusMeta(
            task
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


    const title =
        escapeHtml(
            task.title ||
            "Unknown Track"
        );

    const artist =
        escapeHtml(
            task.artist ||
            "Unknown Artist"
        );

    const message =
        escapeHtml(
            task.error ||
            task.step ||
            ""
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

                <div class="download-title-wrap">

                    <div class="download-title">
                        ${title}
                    </div>

                    <div class="download-artist">
                        ${artist}
                    </div>

                </div>


                <div class="download-status-wrap">

                    ${
                        queuePosition !== null
                            ? `
                                <span class="queue-position">
                                    #${queuePosition}
                                </span>
                            `
                            : ""
                    }

                    <span
                        class="
                            download-status
                            ${statusClass}
                        "
                    >
                        <span
                            class="status-dot"
                        ></span>
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
                    ${message}
                </div>

                <div class="download-meta">
                    ${
                        task.speed
                            ? escapeHtml(
                                task.speed
                            )
                            : ""
                    }

                    ${
                        task.status ===
                            "completed"
                            ? "✓ Ready in Library"
                            : ""
                    }
                </div>

            </div>

        </div>


        <div class="download-actions">

            ${
                isActiveTask(task)
                    ? `
                        <button
                            class="btn-danger download-cancel-btn"
                            data-task-id="${escapeHtml(
                                task.id
                            )}"
                        >
                            ✕ Cancel
                        </button>
                    `
                    : `
                        <button
                            class="download-remove-btn"
                            data-task-id="${escapeHtml(
                                task.id
                            )}"
                        >
                            Remove
                        </button>
                    `
            }

        </div>
    `;


    const cancel =
        card.querySelector(
            ".download-cancel-btn"
        );

    if (cancel) {

        cancel.onclick =
            () =>
                cancelTask(
                    task.id
                );
    }


    const remove =
        card.querySelector(
            ".download-remove-btn"
        );

    if (remove) {

        remove.onclick =
            () =>
                removeDownloadTask(
                    task.id
                );
    }


    return card;
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
                    task.step,
                    task.error,
                    task.speed,
                    task.last_updated,
                ].join("|")
        )
        .sort()
        .join(";");
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
                            ? "Tracks waiting or downloading now"
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
                    buildDownloadCard(
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


    const historySection =
        document.createElement(
            "section"
        );

    historySection.className =
        "downloads-section";


    historySection.innerHTML = `
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
                    buildDownloadCard(
                        task
                    )
                )
        );


        historySection.appendChild(
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

        historySection.appendChild(
            empty
        );
    }


    list.appendChild(
        historySection
    );


    lastRenderedTaskSignature =
        taskSignature(
            tasks
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
            Array.isArray(tasks)
                ? tasks
                : [];


        for (
            const task
            of latestTasks
        ) {

            if (
                task.status ===
                    "completed" &&
                !completedSet.has(
                    task.id
                )
            ) {

                completedSet.add(
                    task.id
                );

                showToast(
                    `🎉 ${task.title || "Track"} is ready`
                );
            }
        }


        updateQueueCounters(
            latestTasks
        );


        const signature =
            taskSignature(
                latestTasks
            );


        const downloads =
            document.getElementById(
                "tab-downloads"
            );


        if (
            force ||
            (
                downloads &&
                downloads.classList.contains(
                    "active"
                )
            ) ||
            signature !==
                lastRenderedTaskSignature
        ) {

            renderDownloads(
                latestTasks
            );
        }

    } catch (error) {

        console.warn(
            "Task polling:",
            error
        );
    }
}


async function loadDownloads() {

    const list =
        document.getElementById(
            "downloadsList"
        );


    if (
        list &&
        !latestTasks.length
    ) {

        list.innerHTML = `
            <div class="downloads-page-loader">
                Loading downloads...
            </div>
        `;
    }


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

    try {

        const response =
            await fetch(
                `api/tasks/${encodeURIComponent(
                    taskId
                )}/cancel`,
                {
                    method:
                        "POST",
                }
            );


        const data =
            await response.json();


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

    try {

        const response =
            await fetch(
                `api/tasks/${encodeURIComponent(
                    taskId
                )}`,
                {
                    method:
                        "DELETE",
                }
            );


        const data =
            await response.json();


        if (!response.ok) {
            throw new Error(
                data.detail ||
                "Failed to remove."
            );
        }


        completedSet.delete(
            taskId
        );


        showToast(
            "🗑 Removed from history"
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


async function clearDoneTasks() {

    const button =
        document.querySelector(
            ".downloads-header-actions .btn-refresh:first-child"
        );


    if (button) {

        button.disabled =
            true;

        button.textContent =
            "🧹 Clearing...";
    }


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
                data.detail ||
                "Failed to clear."
            );
        }


        completedSet.clear();
        latestTasks = [];
        lastRenderedTaskSignature = "";


        showToast(
            `🧹 Cleared ${data.count || 0} downloads`
        );


        await pollTasks(
            true
        );


    } catch (error) {

        showToast(
            "❌ " +
            error.message
        );

    } finally {

        if (button) {

            button.disabled =
                false;

            button.textContent =
                "🧹 Clear Completed";
        }
    }
}


/* ============================================================
   LIBRARY
   ============================================================ */

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


        const mapping = {
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
            mapping
        ).forEach(
            (
                [id, value]
            ) => {

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


    list.innerHTML = `
        <div class="library-loading">
            Loading library...
        </div>
    `;


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


    if (!list) {
        return;
    }


    const query =
        (
            input
                ? input.value
                : ""
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
                .includes(
                    query
                )
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
                        alt="${escapeHtml(
                            file.name
                        )}"
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
                            file.size ||
                            ""
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
                "api/library/" +
                encodeURIComponent(
                    filename
                ),
                {
                    method:
                        "DELETE",
                }
            );


        if (!response.ok) {
            throw new Error(
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
            data.stats ||
            {};


        const map = {
            homeTracks:
                stats.tracks || 0,

            homeArtists:
                stats.artists || 0,

            homeAlbums:
                stats.albums || 0,

            homeDownloads:
                data.active_downloads ||
                0,
        };


        Object.entries(
            map
        ).forEach(
            (
                [id, value]
            ) => {

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
            data.recently_added ||
            [];


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

                card.type =
                    "button";


                card.innerHTML = `

                    <img
                        src="${escapeHtml(
                            track.cover
                        )}"
                        alt=""
                        onerror="
                            this.src='https://via.placeholder.com/180?text=Music'
                        "
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


                card.onclick = () =>
                    toggleAudioStream(
                        card,
                        `rest/stream.view?id=${encodeURIComponent(
                            track.id
                        )}`,
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
            "Home failed:",
            error
        );
    }
}


/* ============================================================
   INFINITE SCROLL
   ============================================================ */

window.addEventListener(
    "scroll",
    () => {

        const search =
            document.getElementById(
                "tab-search"
            );


        if (
            search &&
            search.classList.contains(
                "active"
            )
        ) {

            if (
                window.innerHeight +
                window.scrollY >=
                document.body.offsetHeight -
                500
            ) {

                loadMoreResults();
            }
        }
    },
    {
        passive:
            true,
    }
);


/* ============================================================
   THEME SELECT
   ============================================================ */

const themeSelect =
    document.getElementById(
        "set_theme"
    );

if (themeSelect) {

    themeSelect.addEventListener(
        "change",
        event =>
            toggleTheme(
                event.target.value
            )
    );
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
   STARTUP
   ============================================================ */

async function initializeApp() {

    await refreshLibraryCache();


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
            Array.isArray(tasks)
                ? tasks
                : [];


        latestTasks
            .filter(
                task =>
                    task.status ===
                    "completed"
            )
            .forEach(
                task =>
                    completedSet.add(
                        task.id
                    )
            );

    } catch {}


    await pollTasks(
        true
    );

    await loadStats();

    handleDeepLink();

    initWebSocket();

    pollTimer =
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
