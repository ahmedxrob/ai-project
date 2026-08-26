/* ============================================================
   XROB MUSIC
   COMPLETE FRONTEND
   Search + Player + Downloads Queue + Library + Settings
   ============================================================ */


/* ============================================================
   GLOBAL STATE
   ============================================================ */

let pollTimer = null;
let socket = null;

let completedSet = new Set();

let libraryFilesSet = new Set();
let rawLibraryFiles = [];

let activePreviewBtn = null;

let currentPage = 1;
let currentQuery = "";
let isLoadingMore = false;
let hasMoreResults = true;

let latestTasks = [];
let lastRenderedTaskSignature = "";


/* ============================================================
   GLOBAL PLAYER ELEMENTS
   ============================================================ */

const globalAudio =
    document.getElementById(
        "global-audio-element"
    );

const gpBar =
    document.getElementById(
        "global-player-bar"
    );

const gpPlayBtn =
    document.getElementById(
        "gp-play-btn"
    );

const gpSeek =
    document.getElementById(
        "gp-seek"
    );

const gpVolume =
    document.getElementById(
        "gp-volume"
    );

const gpCurTime =
    document.getElementById(
        "gp-cur-time"
    );

const gpDurTime =
    document.getElementById(
        "gp-dur-time"
    );

const gpTitle =
    document.getElementById(
        "gp-title"
    );

const gpArtist =
    document.getElementById(
        "gp-artist"
    );

const gpArt =
    document.getElementById(
        "gp-art"
    );


/* ============================================================
   AUDIO VISUALIZER
   ============================================================ */

let audioCtx = null;
let analyser = null;
let sourceNode = null;
let visualizerAnimationFrame = null;

const canvas =
    document.getElementById(
        "visualizer-canvas"
    );

const canvasCtx =
    canvas
        ? canvas.getContext("2d")
        : null;


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

        analyser.smoothingTimeConstant = 0.8;

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
        !analyser ||
        !canvasCtx ||
        !canvas
    ) {
        return;
    }

    visualizerAnimationFrame =
        requestAnimationFrame(
            drawVisualizer
        );

    const bufferLength =
        analyser.frequencyBinCount;

    const dataArray =
        new Uint8Array(
            bufferLength
        );

    analyser.getByteFrequencyData(
        dataArray
    );

    canvasCtx.clearRect(
        0,
        0,
        canvas.width,
        canvas.height
    );

    const barWidth =
        (
            canvas.width /
            bufferLength
        ) * 1.6;

    let x = 0;

    for (
        let i = 0;
        i < bufferLength;
        i++
    ) {

        const value =
            dataArray[i] / 255;

        const barHeight =
            Math.max(
                2,
                value *
                    canvas.height
            );

        canvasCtx.fillStyle =
            "#1db954";

        canvasCtx.fillRect(
            x,
            canvas.height - barHeight,
            Math.max(
                1,
                barWidth - 1
            ),
            barHeight
        );

        x += barWidth;
    }
}


/* ============================================================
   PLAYER HELPERS
   ============================================================ */

function formatSecs(seconds) {

    seconds =
        Math.floor(
            seconds || 0
        );

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


function setPlayerPlayingState(
    isPlaying
) {

    if (gpPlayBtn) {

        gpPlayBtn.textContent =
            isPlaying
                ? "❚❚"
                : "▶";

        gpPlayBtn.classList.toggle(
            "is-playing",
            isPlaying
        );
    }

    if (gpBar) {

        gpBar.classList.toggle(
            "is-playing",
            isPlaying
        );
    }

    if (activePreviewBtn) {

        activePreviewBtn.classList.toggle(
            "playing",
            isPlaying
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

    button.innerHTML =
        button.dataset.type ===
        "library"
            ? "▶ Play"
            : "▶ Preview";
}


function updatePlayerInfo(
    title,
    artist,
    artUrl
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
            artUrl ||
            "https://via.placeholder.com/300?text=🎵";
    }
}


/* ============================================================
   AUDIO EVENTS
   ============================================================ */

if (globalAudio) {

    globalAudio.ontimeupdate = () => {

        updatePlayerProgress();
    };


    globalAudio.onloadedmetadata = () => {

        updatePlayerProgress();
    };


    globalAudio.onplay = () => {

        setPlayerPlayingState(
            true
        );
    };


    globalAudio.onpause = () => {

        setPlayerPlayingState(
            false
        );
    };


    globalAudio.onended = () => {

        setPlayerPlayingState(
            false
        );

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

        if (gpBar) {

            gpBar.classList.remove(
                "is-playing"
            );
        }
    };


    globalAudio.onerror = () => {

        setPlayerPlayingState(
            false
        );

        if (activePreviewBtn) {

            const failedButton =
                activePreviewBtn;

            failedButton.innerHTML =
                "❌ Error";

            setTimeout(
                () => {

                    if (
                        failedButton ===
                        activePreviewBtn
                    ) {

                        resetPreviewButton(
                            failedButton
                        );
                    }
                },
                2000
            );
        }
    };
}


/* ============================================================
   PLAYER PLAY / PAUSE
   ============================================================ */

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

                    setPlayerPlayingState(
                        true
                    );

                } catch (error) {

                    console.error(
                        "Playback failed:",
                        error
                    );
                }

            } else {

                globalAudio.pause();

                setPlayerPlayingState(
                    false
                );
            }
        };
}


/* ============================================================
   PLAYER SEEK
   ============================================================ */

if (gpSeek) {

    gpSeek.oninput = () => {

        if (
            globalAudio &&
            globalAudio.duration &&
            isFinite(
                globalAudio.duration
            )
        ) {

            globalAudio.currentTime =
                (
                    gpSeek.value /
                    100
                ) *
                globalAudio.duration;
        }
    };
}


/* ============================================================
   PLAYER VOLUME
   ============================================================ */

if (gpVolume) {

    gpVolume.oninput = () => {

        if (!globalAudio) {
            return;
        }

        globalAudio.volume =
            parseFloat(
                gpVolume.value
            );

        localStorage.setItem(
            "xrob_music_volume",
            gpVolume.value
        );
    };


    const savedVolume =
        localStorage.getItem(
            "xrob_music_volume"
        );


    if (
        savedVolume !== null
    ) {

        gpVolume.value =
            savedVolume;

        globalAudio.volume =
            parseFloat(
                savedVolume
            );

    } else {

        globalAudio.volume = 0.8;
        gpVolume.value = 0.8;
    }
}


/* ============================================================
   STOP PREVIEW
   ============================================================ */

function stopCurrentPreview() {

    if (!globalAudio) {
        return;
    }

    globalAudio.pause();

    globalAudio.removeAttribute(
        "src"
    );

    globalAudio.load();

    setPlayerPlayingState(
        false
    );

    if (activePreviewBtn) {

        resetPreviewButton(
            activePreviewBtn
        );

        activePreviewBtn = null;
    }

    if (gpSeek) {
        gpSeek.value = 0;
    }

    if (gpCurTime) {
        gpCurTime.textContent =
            "0:00";
    }

    if (gpDurTime) {
        gpDurTime.textContent =
            "0:00";
    }
}


/* ============================================================
   AUDIO STREAM
   ============================================================ */

function toggleAudioStream(
    button,
    streamUrl,
    type = "search",
    title = "Track",
    artist = "Artist",
    artUrl = ""
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

    let absoluteUrl = "";

    try {

        absoluteUrl =
            new URL(
                streamUrl,
                window.location.href
            ).href;

    } catch (error) {

        console.error(
            "Invalid audio URL:",
            error
        );

        return;
    }


    /* Same track */

    if (
        activePreviewBtn === button &&
        globalAudio.src ===
            absoluteUrl
    ) {

        if (
            globalAudio.paused
        ) {

            globalAudio
                .play()
                .then(
                    () => {

                        setPlayerPlayingState(
                            true
                        );

                        button.innerHTML =
                            "❚❚ Pause";
                    }
                )
                .catch(() => {});

        } else {

            globalAudio.pause();

            setPlayerPlayingState(
                false
            );

            button.innerHTML =
                type === "library"
                    ? "▶ Play"
                    : "▶ Preview";
        }

        return;
    }


    /* Stop old */

    if (activePreviewBtn) {

        resetPreviewButton(
            activePreviewBtn
        );
    }


    button.dataset.type =
        type;

    activePreviewBtn =
        button;

    button.innerHTML =
        "⏳ Loading...";


    updatePlayerInfo(
        title,
        artist,
        artUrl
    );


    if (gpBar) {

        gpBar.style.display =
            "flex";

        gpBar.classList.add(
            "loading"
        );
    }


    if (gpSeek) {
        gpSeek.value = 0;
    }

    if (gpCurTime) {
        gpCurTime.textContent =
            "0:00";
    }

    if (gpDurTime) {
        gpDurTime.textContent =
            "0:00";
    }


    globalAudio.pause();

    globalAudio.src =
        streamUrl;

    globalAudio.load();


    globalAudio
        .play()
        .then(
            () => {

                if (gpBar) {

                    gpBar.classList.remove(
                        "loading"
                    );
                }

                setPlayerPlayingState(
                    true
                );

                button.innerHTML =
                    "❚❚ Pause";
            }
        )
        .catch(
            error => {

                console.error(
                    "Audio playback error:",
                    error
                );

                if (gpBar) {

                    gpBar.classList.remove(
                        "loading"
                    );
                }

                button.innerHTML =
                    "❌ Error";

                setTimeout(
                    () => {

                        if (
                            button ===
                            activePreviewBtn
                        ) {

                            resetPreviewButton(
                                button
                            );
                        }
                    },
                    2000
                );
            }
        );
}


/* ============================================================
   WEBSOCKET
   ============================================================ */

function initWebSocket() {

    const protocol =
        window.location.protocol ===
        "https:"
            ? "wss:"
            : "ws:";


    try {

        socket =
            new WebSocket(
                `${protocol}//${window.location.host}/ws`
            );

    } catch (error) {

        startTaskPolling();

        return;
    }


    socket.onopen = () => {

        console.log(
            "Xrob Music WebSocket connected"
        );
    };


    socket.onmessage = event => {

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
                "Invalid WebSocket data:",
                error
            );
        }
    };


    socket.onclose = () => {

        setTimeout(
            initWebSocket,
            3000
        );
    };


    socket.onerror = () => {

        try {

            socket.close();

        } catch (error) {}
    };
}


function startTaskPolling() {

    if (pollTimer) {
        return;
    }

    pollTimer =
        setInterval(
            () => pollTasks(),
            2000
        );
}


/* ============================================================
   NOTIFICATIONS
   ============================================================ */

function requestNotificationPermission() {

    if (
        !("Notification" in window)
    ) {

        showToast(
            "⚠️ Browser notifications are not supported."
        );

        return;
    }


    Notification
        .requestPermission()
        .then(
            permission => {

                if (
                    permission ===
                    "granted"
                ) {

                    showToast(
                        "✅ Notifications enabled!"
                    );

                } else {

                    showToast(
                        "⚠️ Notification permission denied."
                    );
                }
            }
        );
}


function notifyTrackComplete(
    title
) {

    showToast(
        `🎉 ${title} is ready in your library`
    );


    if (
        "Notification" in window &&
        Notification.permission ===
            "granted"
    ) {

        new Notification(
            "Xrob Music",
            {
                body:
                    `${title} is now downloaded and ready in your library.`,
                icon:
                    "https://via.placeholder.com/64?text=🎵",
            }
        );
    }
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
        () => {

            toast.style.opacity =
                "0";

            toast.style.transform =
                "translateX(20px)";

            setTimeout(
                () => toast.remove(),
                250
            );
        },
        4000
    );
}


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


const savedTheme =
    localStorage.getItem(
        "xrob_music_theme"
    ) || "dark";

toggleTheme(
    savedTheme
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

    switchTab(tab);
}


function switchTab(
    tab
) {

    document
        .querySelectorAll(
            ".tab-content"
        )
        .forEach(
            content =>
                content.classList.remove(
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

                button.setAttribute(
                    "aria-selected",
                    "false"
                );
            }
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


    const sideButton =
        document.getElementById(
            `btn-${tab}`
        );


    const mobileButton =
        document.getElementById(
            `mob-btn-${tab}`
        );


    if (sideButton) {

        sideButton.classList.add(
            "active"
        );

        sideButton.setAttribute(
            "aria-selected",
            "true"
        );
    }


    if (mobileButton) {

        mobileButton.classList.add(
            "active"
        );

        mobileButton.setAttribute(
            "aria-selected",
            "true"
        );
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
        window.location.hash.replace(
            "#",
            ""
        );


    const allowedTabs = [
        "search",
        "downloads",
        "library",
        "settings",
    ];


    if (
        allowedTabs.includes(
            hash
        )
    ) {

        switchTab(
            hash
        );

    } else {

        switchTab(
            "search"
        );
    }
}


window.addEventListener(
    "hashchange",
    handleDeepLink
);


/* ============================================================
   HELPERS
   ============================================================ */

function normalizeKey(
    value
) {

    return (
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
                        value;
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
            "set_theme",
            localStorage.getItem(
                "xrob_music_theme"
            ) || "dark"
        );

        setValue(
            "set_navidrome_url",
            settings.navidrome_url ||
                ""
        );

        setValue(
            "set_navidrome_user",
            settings.navidrome_user ||
                ""
        );

        setValue(
            "set_navidrome_token",
            settings.navidrome_token ||
                ""
        );

    } catch (error) {

        console.warn(
            "Failed to load settings:",
            error
        );
    }
}


async function saveSettings() {

    const data = {

        audio_format:
            document.getElementById(
                "set_format"
            )?.value || "mp3",

        audio_quality:
            document.getElementById(
                "set_quality"
            )?.value || "320K",

        embed_thumbnail:
            document.getElementById(
                "set_thumb"
            )?.checked ?? true,

        embed_metadata:
            document.getElementById(
                "set_meta"
            )?.checked ?? true,

        max_results:
            parseInt(
                document.getElementById(
                    "set_max_results"
                )?.value || "20",
                10
            ) || 20,

        organize_by_artist:
            document.getElementById(
                "set_organize"
            )?.checked ?? false,

        navidrome_url:
            document.getElementById(
                "set_navidrome_url"
            )?.value || "",

        navidrome_user:
            document.getElementById(
                "set_navidrome_user"
            )?.value || "",

        navidrome_token:
            document.getElementById(
                "set_navidrome_token"
            )?.value || "",
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


        showToast(
            "✅ Settings saved"
        );


        if (message) {

            message.textContent =
                "✅ Settings saved!";

            setTimeout(
                () => {
                    message.textContent =
                        "";
                },
                3000
            );
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


        if (!response.ok) {
            throw new Error(
                "Library request failed."
            );
        }


        const data =
            await response.json();


        libraryFilesSet.clear();

        rawLibraryFiles =
            data.files || [];


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

                const baseName =
                    name.substring(
                        slash + 1,
                        dot > slash
                            ? dot
                            : name.length
                    ) || name;

                libraryFilesSet.add(
                    normalizeKey(
                        baseName
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

        const detailCount =
            document.getElementById(
                "libCountDetail"
            );

        const folderSize =
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

        if (detailCount) {
            detailCount.textContent =
                count;
        }

        if (folderSize) {
            folderSize.textContent =
                data.total_size ||
                "0 MB";
        }

    } catch (error) {

        console.warn(
            "Library cache failed:",
            error
        );
    }
}


/* ============================================================
   SEARCH RENDERING
   ============================================================ */

function renderLibraryBadge(
    container
) {

    if (!container) {
        return;
    }

    container.innerHTML = `
        <div class="badge-library">
            ✅ In Library
        </div>
    `;
}


function markSearchItemAsLibrary(
    task
) {

    if (
        !task ||
        !task.elementId
    ) {
        return;
    }


    let group = null;


    try {

        group =
            document.querySelector(
                `div[data-group-id="${CSS.escape(
                    String(
                        task.elementId
                    )
                )}"]`
            );

    } catch (error) {

        group =
            document.querySelector(
                `div[data-group-id="${String(
                    task.elementId
                ).replace(
                    /"/g,
                    '\\"'
                )}"]`
            );
    }


    if (group) {

        renderLibraryBadge(
            group
        );
    }
}


function updateSearchDownloadButtons(
    tasks
) {

    const activeIds =
        new Set(
            tasks
                .filter(
                    task =>
                        [
                            "queued",
                            "downloading",
                            "processing",
                        ].includes(
                            task.status
                        )
                )
                .map(
                    task =>
                        String(
                            task.elementId ||
                            ""
                        )
                )
                .filter(Boolean)
        );


    document
        .querySelectorAll(
            ".btn-download[data-id]"
        )
        .forEach(
            button => {

                const id =
                    String(
                        button.dataset.id ||
                        ""
                    );

                if (!id) {
                    return;
                }


                if (
                    activeIds.has(
                        id
                    )
                ) {

                    button.disabled =
                        true;

                    button.classList.add(
                        "is-queued"
                    );

                    button.innerHTML =
                        "⏳ Queued";

                } else if (
                    !button.classList.contains(
                        "is-completed"
                    )
                ) {

                    button.disabled =
                        false;

                    button.classList.remove(
                        "is-queued"
                    );

                    if (
                        button.textContent
                            .includes(
                                "Queued"
                            )
                    ) {

                        button.innerHTML =
                            "⬇️ Save";
                    }
                }
            }
        );


    tasks
        .filter(
            task =>
                task.status ===
                "completed"
        )
        .forEach(
            markSearchItemAsLibrary
        );
}


function renderItems(
    data
) {

    const results =
        document.getElementById(
            "results"
        );

    if (!results) {
        return;
    }


    data.forEach(
        item => {

            const cleanedTitle =
                normalizeKey(
                    item.title ||
                    "Unknown"
                );


            const isInLibrary =
                libraryFilesSet.has(
                    cleanedTitle
                );


            const card =
                document.createElement(
                    "div"
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


            const duration =
                escapeHtml(
                    item.duration_text ||
                    ""
                );


            card.innerHTML = `
                <div class="thumb-wrapper">

                    <img
                        src="${thumbnail}"
                        alt="Cover art for ${title}"
                        onerror="
                            this.src='https://via.placeholder.com/110x65?text=Music'
                        "
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
                    data-group-id="${escapeHtml(
                        item.id || ""
                    )}"
                ></div>
            `;


            const group =
                card.querySelector(
                    ".btn-group"
                );


            if (isInLibrary) {

                renderLibraryBadge(
                    group
                );

            } else {

                const previewButton =
                    document.createElement(
                        "button"
                    );


                previewButton.className =
                    "btn-preview";

                previewButton.dataset.type =
                    "search";

                previewButton.innerHTML =
                    "▶ Preview";


                previewButton.setAttribute(
                    "aria-label",
                    `Preview ${title}`
                );


                previewButton.onclick =
                    () =>
                        toggleAudioStream(
                            previewButton,
                            "api/preview?url=" +
                                encodeURIComponent(
                                    item.url
                                ),
                            "search",
                            item.title,
                            item.channel,
                            item.thumbnail
                        );


                const downloadButton =
                    document.createElement(
                        "button"
                    );


                downloadButton.className =
                    "btn-download";

                downloadButton.dataset.id =
                    item.id || "";

                downloadButton.innerHTML =
                    "⬇️ Save";


                downloadButton.setAttribute(
                    "aria-label",
                    `Download ${title}`
                );


                downloadButton.onclick =
                    () =>
                        startDownload(
                            item.url,
                            item.title,
                            item.id,
                            item.channel,
                            downloadButton
                        );


                group.appendChild(
                    previewButton
                );

                group.appendChild(
                    downloadButton
                );
            }


            results.appendChild(
                card
            );
        }
    );
}


/* ============================================================
   SEARCH
   ============================================================ */

async function searchMusic() {

    const queryInput =
        document.getElementById(
            "query"
        );

    const statusMessage =
        document.getElementById(
            "statusMsg"
        );

    const results =
        document.getElementById(
            "results"
        );

    const searchButton =
        document.getElementById(
            "searchBtn"
        );


    const query =
        queryInput
            ? queryInput.value.trim()
            : "";


    if (!query) {

        if (statusMessage) {

            statusMessage.textContent =
                "Type something to search.";
        }

        return;
    }


    currentQuery =
        query;

    currentPage = 1;
    hasMoreResults = true;
    isLoadingMore = false;


    if (statusMessage) {

        statusMessage.textContent =
            "🔍 Searching YouTube...";
    }


    if (results) {

        results.innerHTML =
            "";
    }


    if (searchButton) {

        searchButton.disabled =
            true;
    }


    await refreshLibraryCache();


    try {

        const response =
            await fetch(
                `api/search?q=${encodeURIComponent(
                    query
                )}&page=1`
            );


        if (!response.ok) {

            const data =
                await response
                    .json()
                    .catch(
                        () => ({})
                    );

            throw new Error(
                data.detail ||
                "Search failed."
            );
        }


        const data =
            await response.json();


        if (
            !Array.isArray(data) ||
            data.length === 0
        ) {

            if (statusMessage) {

                statusMessage.textContent =
                    "No results found.";
            }

            hasMoreResults =
                false;

            return;
        }


        if (statusMessage) {

            statusMessage.textContent =
                "";
        }


        renderItems(
            data
        );


    } catch (error) {

        if (statusMessage) {

            statusMessage.textContent =
                "❌ " +
                (
                    error.message ||
                    "Search failed."
                );
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


        if (!response.ok) {

            throw new Error(
                "Load failed."
            );
        }


        const data =
            await response.json();


        if (
            !Array.isArray(data) ||
            data.length === 0
        ) {

            hasMoreResults =
                false;

        } else {

            renderItems(
                data
            );
        }


    } catch (error) {

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


/* ============================================================
   DOWNLOAD HELPERS
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


function getTaskStatusMeta(
    task
) {

    const status =
        String(
            task.status ||
            "queued"
        ).toLowerCase();


    const statuses = {

        queued: {
            label: "Queued",
            icon: "⏳",
            className:
                "status-queued",
        },

        downloading: {
            label: "Downloading",
            icon: "⬇️",
            className:
                "status-downloading",
        },

        processing: {
            label: "Processing",
            icon: "⚙️",
            className:
                "status-processing",
        },

        completed: {
            label: "Completed",
            icon: "✓",
            className:
                "status-completed",
        },

        error: {
            label: "Failed",
            icon: "⚠️",
            className:
                "status-error",
        },

        failed: {
            label: "Failed",
            icon: "⚠️",
            className:
                "status-error",
        },

        cancelled: {
            label: "Cancelled",
            icon: "✕",
            className:
                "status-cancelled",
        },

        canceled: {
            label: "Cancelled",
            icon: "✕",
            className:
                "status-cancelled",
        },
    };


    return (
        statuses[status] ||
        {
            label: "Queued",
            icon: "•",
            className:
                "status-queued",
        }
    );
}


function updateQueueCounters(
    tasks
) {

    const activeCount =
        tasks.filter(
            isActiveTask
        ).length;


    const queueCount =
        document.getElementById(
            "queueCount"
        );

    const downloadQueueCount =
        document.getElementById(
            "downloadQueueCount"
        );

    const mobileQueueCount =
        document.getElementById(
            "mobQueueCount"
        );


    if (queueCount) {

        queueCount.textContent =
            activeCount;
    }

    if (downloadQueueCount) {

        downloadQueueCount.textContent =
            activeCount;
    }

    if (mobileQueueCount) {

        mobileQueueCount.textContent =
            activeCount;
    }
}


/* ============================================================
   START DOWNLOAD
   ============================================================ */

async function startDownload(
    url,
    title,
    elementId,
    artist = "Unknown Artist",
    sourceButton = null
) {

    if (sourceButton) {

        sourceButton.disabled =
            true;

        sourceButton.classList.add(
            "is-queued"
        );

        sourceButton.innerHTML =
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
            await response
                .json()
                .catch(
                    () => ({})
                );


        if (!response.ok) {

            throw new Error(
                data.detail ||
                "Failed to enqueue download."
            );
        }


        if (
            data.status ===
            "already_queued"
        ) {

            showToast(
                "⏳ This track is already in Downloads"
            );

        } else {

            showToast(
                `⬇️ Added "${title}" to Downloads`
            );
        }


        navigate(
            "downloads"
        );


        await pollTasks(
            true
        );


    } catch (error) {

        if (sourceButton) {

            sourceButton.disabled =
                false;

            sourceButton.classList.remove(
                "is-queued"
            );

            sourceButton.innerHTML =
                "⬇️ Save";
        }


        showToast(
            "❌ " +
            (
                error.message ||
                "Failed to enqueue download."
            )
        );
    }
}


/* ============================================================
   TASK SIGNATURE
   ============================================================ */

function createTaskSignature(
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
                ].join(":")
        )
        .sort()
        .join("|");
}


/* ============================================================
   DOWNLOAD CARD
   ============================================================ */

function buildDownloadCard(
    task,
    queuePosition = null
) {

    const meta =
        getTaskStatusMeta(
            task
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


    const active =
        isActiveTask(task);

    const completed =
        task.status ===
        "completed";

    const failed =
        [
            "error",
            "failed",
        ].includes(
            task.status
        );

    const cancelled =
        [
            "cancelled",
            "canceled",
        ].includes(
            task.status
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


    const step =
        escapeHtml(
            task.step ||
            ""
        );


    const error =
        escapeHtml(
            task.error ||
            ""
        );


    const speed =
        escapeHtml(
            task.speed ||
            ""
        );


    const statusMessage =
        failed
            ? (
                error ||
                "Download failed"
            )
            : cancelled
                ? "Download cancelled"
                : completed
                    ? "Saved to your library"
                    : step ||
                        meta.label;


    const card =
        document.createElement(
            "article"
        );


    card.className =
        "download-card";


    card.dataset.taskId =
        task.id || "";


    const queueBadge =
        queuePosition !== null
            ? `
                <span class="queue-position">
                    #${queuePosition}
                </span>
            `
            : "";


    const action =
        active
            ? `
                <button
                    type="button"
                    class="btn-danger download-cancel-btn"
                    data-task-id="${escapeHtml(
                        String(
                            task.id || ""
                        )
                    )}"
                >
                    ✕ Cancel
                </button>
            `
            : `
                <button
                    type="button"
                    class="download-remove-btn"
                    data-task-id="${escapeHtml(
                        String(
                            task.id || ""
                        )
                    )}"
                >
                    Remove
                </button>
            `;


    card.innerHTML = `

        <div class="download-art">

            <div class="download-art-icon">
                🎵
            </div>

            <div class="download-art-overlay">
                ${meta.icon}
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

                    ${queueBadge}

                    <span
                        class="
                            download-status
                            ${meta.className}
                        "
                    >

                        <span
                            class="status-dot"
                        ></span>

                        ${meta.label}

                    </span>

                </div>

            </div>


            <div class="download-progress-row">

                <div class="download-progress-track">

                    <div
                        class="
                            download-progress-fill
                            ${failed || cancelled
                                ? "is-error"
                                : ""}
                            ${completed
                                ? "is-complete"
                                : ""}
                        "
                        style="
                            width:${percent}%;
                        "
                    ></div>

                </div>


                <span class="download-percent">
                    ${percent}%
                </span>

            </div>


            <div class="download-bottom">

                <div class="download-message">
                    ${statusMessage}
                </div>


                <div class="download-meta">

                    ${
                        speed
                            ? `
                                <span>
                                    ${speed}
                                </span>
                            `
                            : ""
                    }


                    ${
                        active &&
                        task.status ===
                            "queued"
                            ? `
                                <span>
                                    Waiting for downloader
                                </span>
                            `
                            : ""
                    }


                    ${
                        completed
                            ? `
                                <span>
                                    ✓ Ready in Library
                                </span>
                            `
                            : ""
                    }

                </div>

            </div>

        </div>


        <div class="download-actions">
            ${action}
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


/* ============================================================
   RENDER DOWNLOADS
   ============================================================ */

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


    const activeTasks =
        tasks
            .filter(
                isActiveTask
            )
            .sort(
                (a, b) =>
                    Number(
                        b.last_updated || 0
                    ) -
                    Number(
                        a.last_updated || 0
                    )
            );


    const finishedTasks =
        tasks
            .filter(
                isFinishedTask
            )
            .sort(
                (a, b) =>
                    Number(
                        b.last_updated || 0
                    ) -
                    Number(
                        a.last_updated || 0
                    )
            );


    updateQueueCounters(
        tasks
    );


    list.innerHTML =
        "";


    /* --------------------------------------------------------
       ACTIVE QUEUE
       -------------------------------------------------------- */

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
                        activeTasks.length
                            ? "Tracks waiting or downloading now"
                            : "Nothing is currently downloading"
                    }
                </div>

            </div>


            <span class="section-count">
                ${activeTasks.length}
            </span>

        </div>
    `;


    if (
        activeTasks.length
    ) {

        const stack =
            document.createElement(
                "div"
            );

        stack.className =
            "download-stack";


        activeTasks.forEach(
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
                Search for a track and press Download to add it here.
            </div>

            <button
                type="button"
                class="save-btn empty-action"
            >
                🔍 Search Music
            </button>
        `;


        const searchButton =
            empty.querySelector(
                ".empty-action"
            );


        if (searchButton) {

            searchButton.onclick =
                () =>
                    navigate(
                        "search"
                    );
        }


        activeSection.appendChild(
            empty
        );
    }


    list.appendChild(
        activeSection
    );


    /* --------------------------------------------------------
       RECENT DOWNLOADS
       -------------------------------------------------------- */

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
                    Completed and previous download jobs
                </div>

            </div>


            <span class="section-count">
                ${finishedTasks.length}
            </span>

        </div>
    `;


    if (
        finishedTasks.length
    ) {

        const stack =
            document.createElement(
                "div"
            );

        stack.className =
            "download-stack";


        finishedTasks.forEach(
            task => {

                stack.appendChild(
                    buildDownloadCard(
                        task
                    )
                );
            }
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
        createTaskSignature(
            tasks
        );
}


/* ============================================================
   TASK POLLING
   ============================================================ */

async function pollTasks(
    forceRender = false
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


        if (!response.ok) {
            throw new Error(
                "Failed to load tasks."
            );
        }


        const tasks =
            await response.json();


        latestTasks =
            Array.isArray(tasks)
                ? tasks
                : [];


        let libraryNeedsUpdate =
            false;


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

                libraryNeedsUpdate =
                    true;

                notifyTrackComplete(
                    task.title ||
                    "Track"
                );

                markSearchItemAsLibrary(
                    task
                );
            }
        }


        if (
            libraryNeedsUpdate
        ) {

            await refreshLibraryCache();


            const libraryTab =
                document.getElementById(
                    "tab-library"
                );


            if (
                libraryTab &&
                libraryTab.classList.contains(
                    "active"
                )
            ) {

                await loadLibrary();
            }
        }


        updateQueueCounters(
            latestTasks
        );


        updateSearchDownloadButtons(
            latestTasks
        );


        const signature =
            createTaskSignature(
                latestTasks
            );


        const downloadsTab =
            document.getElementById(
                "tab-downloads"
            );


        const downloadsActive =
            downloadsTab &&
            downloadsTab.classList.contains(
                "active"
            );


        if (
            forceRender ||
            downloadsActive ||
            signature !==
                lastRenderedTaskSignature
        ) {

            renderDownloads(
                latestTasks
            );
        }


    } catch (error) {

        console.warn(
            "Task polling failed:",
            error
        );
    }
}


/* ============================================================
   CANCEL TASK
   ============================================================ */

async function cancelTask(
    taskId
) {

    if (!taskId) {
        return;
    }


    const card =
        document.querySelector(
            `.download-card[data-task-id="${CSS.escape(
                String(taskId)
            )}"]`
        );


    const button =
        card
            ? card.querySelector(
                  ".download-cancel-btn"
              )
            : null;


    if (button) {

        button.disabled =
            true;

        button.textContent =
            "Cancelling...";
    }


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
            await response
                .json()
                .catch(
                    () => ({})
                );


        if (!response.ok) {

            throw new Error(
                data.detail ||
                "Failed to cancel download."
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
            (
                error.message ||
                "Failed to cancel download."
            )
        );


        await pollTasks(
            true
        );
    }
}


/* ============================================================
   REMOVE ONE FINISHED TASK
   ============================================================ */

async function removeDownloadTask(
    taskId
) {

    if (!taskId) {
        return;
    }


    try {

        const response =
            await fetch(
                `api/tasks/${encodeURIComponent(
                    taskId
                )}`,
                {
                    method:
                        "DELETE",
                    cache:
                        "no-store",
                }
            );


        const data =
            await response
                .json()
                .catch(
                    () => ({})
                );


        if (!response.ok) {

            throw new Error(
                data.detail ||
                "Failed to remove download."
            );
        }


        completedSet.delete(
            taskId
        );


        showToast(
            "🗑 Download removed from history"
        );


        await pollTasks(
            true
        );


    } catch (error) {

        showToast(
            "❌ " +
            (
                error.message ||
                "Failed to remove download."
            )
        );
    }
}


/* ============================================================
   CLEAR COMPLETED
   IMPORTANT FIX
   ============================================================ */

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
            await response
                .json()
                .catch(
                    () => ({})
                );


        if (!response.ok) {

            throw new Error(
                data.detail ||
                "Failed to clear completed downloads."
            );
        }


        completedSet.clear();


        latestTasks = [];


        lastRenderedTaskSignature =
            "";


        showToast(
            `🧹 Cleared ${
                data.count || 0
            } finished downloads`
        );


        /*
         * Immediately reload from backend.
         */
        await loadDownloads();


    } catch (error) {

        console.error(
            "Clear completed error:",
            error
        );


        showToast(
            "❌ " +
            (
                error.message ||
                "Failed to clear completed downloads."
            )
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
   LOAD DOWNLOADS
   ============================================================ */

async function loadDownloads() {

    const list =
        document.getElementById(
            "downloadsList"
        );


    if (!list) {
        return;
    }


    if (
        latestTasks.length === 0
    ) {

        list.innerHTML = `
            <div class="downloads-page-loader">
                <div class="loader-spinner"></div>
                Loading downloads...
            </div>
        `;
    }


    await pollTasks(
        true
    );
}


/* ============================================================
   STATS
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


        const tracks =
            document.getElementById(
                "statTracks"
            );

        const artists =
            document.getElementById(
                "statArtists"
            );

        const albums =
            document.getElementById(
                "statAlbums"
            );


        /*
         * These IDs are used by Library.
         */
        if (tracks) {

            tracks.textContent =
                stats.tracks || 0;
        }

        if (artists) {

            artists.textContent =
                stats.artists || 0;
        }

        if (albums) {

            albums.textContent =
                stats.albums || 0;
        }


        /*
         * These IDs are used by Downloads.
         * This prevents duplicate-ID problems.
         */
        const downloadTracks =
            document.getElementById(
                "downloadStatTracks"
            );

        const downloadAlbums =
            document.getElementById(
                "downloadStatAlbums"
            );


        if (downloadTracks) {

            downloadTracks.textContent =
                stats.tracks || 0;
        }

        if (downloadAlbums) {

            downloadAlbums.textContent =
                stats.albums || 0;
        }


    } catch (error) {

        console.warn(
            "Stats load failed:",
            error
        );
    }
}


/* ============================================================
   LIBRARY
   ============================================================ */

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
            <div class="loader-spinner"></div>
            Loading library...
        </div>
    `;


    try {

        await refreshLibraryCache();

        await loadStats();

        filterLibrary();


    } catch (error) {

        list.innerHTML = `
            <div class="status-msg">
                Failed to load library.
            </div>
        `;
    }
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


    const filtered =
        rawLibraryFiles.filter(
            file =>
                String(
                    file.name || ""
                )
                .toLowerCase()
                .includes(query)
        );


    if (
        filtered.length === 0
    ) {

        list.innerHTML = `
            <div class="downloads-empty">

                <div class="empty-icon">
                    🎵
                </div>

                <div class="empty-title">
                    ${
                        rawLibraryFiles.length === 0
                            ? "Your library is empty"
                            : "No matching tracks"
                    }
                </div>

                <div class="empty-text">
                    ${
                        rawLibraryFiles.length === 0
                            ? "Downloaded tracks will appear here."
                            : "Try a different search."
                    }
                </div>

            </div>
        `;

        return;
    }


    list.innerHTML =
        "";


    filtered.forEach(
        file => {

            const card =
                document.createElement(
                    "div"
                );


            card.className =
                "result-card";


            const encodedName =
                encodeURIComponent(
                    file.name
                );


            const coverUrl =
                "api/library/cover/" +
                encodedName;


            const streamUrl =
                "api/library/stream/" +
                encodedName;


            const fallback =
                `data:image/svg+xml;utf8,` +
                `<svg xmlns="http://www.w3.org/2000/svg" ` +
                `width="110" height="110">` +
                `<rect width="100%" height="100%" ` +
                `fill="%231e293b"/>` +
                `<text x="50%" y="50%" ` +
                `fill="%239ca3af" font-size="24" ` +
                `text-anchor="middle" ` +
                `dominant-baseline="central">` +
                `🎵</text></svg>`;


            card.innerHTML = `

                <div class="thumb-wrapper">

                    <img
                        src="${coverUrl}"
                        alt="Album cover for ${escapeHtml(
                            file.name
                        )}"
                        onerror="
                            this.onerror=null;
                            this.src='${fallback}'
                        "
                    >

                </div>


                <div class="track-info">

                    <div class="track-title">
                        ${escapeHtml(
                            file.name
                        )}
                    </div>

                    <div class="track-artist">
                        📦 ${
                            escapeHtml(
                                file.size ||
                                "Unknown size"
                            )
                        }
                    </div>

                </div>


                <div class="btn-group">

                    <button
                        class="btn-preview"
                        type="button"
                    >
                        ▶ Play
                    </button>


                    <button
                        class="btn-danger"
                        type="button"
                    >
                        🗑 Delete
                    </button>

                </div>
            `;


            const playButton =
                card.querySelector(
                    ".btn-preview"
                );


            playButton.onclick =
                () =>
                    toggleAudioStream(
                        playButton,
                        streamUrl,
                        "library",
                        file.name,
                        "Local Library",
                        coverUrl
                    );


            const deleteButton =
                card.querySelector(
                    ".btn-danger"
                );


            deleteButton.onclick =
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


/* ============================================================
   DELETE LIBRARY FILE
   ============================================================ */

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

            const data =
                await response
                    .json()
                    .catch(
                        () => ({})
                    );

            throw new Error(
                data.detail ||
                "Delete failed."
            );
        }


        showToast(
            "🗑 Track deleted"
        );


        await refreshLibraryCache();

        await loadStats();

        filterLibrary();


    } catch (error) {

        showToast(
            "❌ " +
            (
                error.message ||
                "Failed to delete file."
            )
        );
    }
}


/* ============================================================
   SEARCH EVENTS
   ============================================================ */

const searchButton =
    document.getElementById(
        "searchBtn"
    );


if (searchButton) {

    searchButton.addEventListener(
        "click",
        searchMusic
    );
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
   THEME SELECT
   ============================================================ */

const themeSelect =
    document.getElementById(
        "set_theme"
    );


if (themeSelect) {

    themeSelect.addEventListener(
        "change",
        event => {

            toggleTheme(
                event.target.value
            );
        }
    );
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
            document.body.offsetHeight -
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


/* ============================================================
   STARTUP
   ============================================================ */

async function initializeApp() {

    await refreshLibraryCache();


    /*
     * Load existing tasks first and mark old
     * completed tasks as already known so they
     * don't trigger fake notifications after reload.
     */
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


    } catch (error) {

        console.warn(
            "Initial task load failed:",
            error
        );
    }


    await pollTasks(
        true
    );


    handleDeepLink();

    initWebSocket();

    startTaskPolling();
}


document.addEventListener(
    "DOMContentLoaded",
    initializeApp
);
