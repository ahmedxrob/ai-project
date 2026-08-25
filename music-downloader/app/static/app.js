/* ============================================================
   XROB MUSIC — SPOTIFY STYLE PLAYER + EXISTING FUNCTIONALITY
   ============================================================ */

let pollTimer = null;
let completedSet = new Set();
let libraryFilesSet = new Set();
let rawLibraryFiles = [];
let activePreviewBtn = null;

let currentPage = 1;
let currentQuery = "";
let isLoadingMore = false;
let hasMoreResults = true;


/* ============================================================
   GLOBAL PLAYER
   ============================================================ */

const globalAudio = document.getElementById("global-audio-element");
const gpBar = document.getElementById("global-player-bar");
const gpPlayBtn = document.getElementById("gp-play-btn");
const gpSeek = document.getElementById("gp-seek");
const gpVolume = document.getElementById("gp-volume");
const gpCurTime = document.getElementById("gp-cur-time");
const gpDurTime = document.getElementById("gp-dur-time");
const gpTitle = document.getElementById("gp-title");
const gpArtist = document.getElementById("gp-artist");
const gpArt = document.getElementById("gp-art");

let audioCtx = null;
let analyser = null;
let sourceNode = null;
let visualizerAnimationFrame = null;

const canvas = document.getElementById("visualizer-canvas");
const canvasCtx = canvas ? canvas.getContext("2d") : null;


/* ============================================================
   AUDIO CONTEXT / VISUALIZER
   ============================================================ */

function initAudioContext() {
    if (audioCtx || !globalAudio) return;

    try {
        audioCtx = new (window.AudioContext || window.webkitAudioContext)();

        analyser = audioCtx.createAnalyser();
        analyser.fftSize = 64;
        analyser.smoothingTimeConstant = 0.8;

        sourceNode = audioCtx.createMediaElementSource(globalAudio);

        sourceNode.connect(analyser);
        analyser.connect(audioCtx.destination);

        drawVisualizer();
    } catch (e) {
        console.warn("Audio visualizer unavailable:", e);
    }
}


function drawVisualizer() {
    if (!analyser || !canvasCtx || !canvas) return;

    visualizerAnimationFrame = requestAnimationFrame(drawVisualizer);

    const bufferLength = analyser.frequencyBinCount;
    const dataArray = new Uint8Array(bufferLength);

    analyser.getByteFrequencyData(dataArray);

    canvasCtx.clearRect(0, 0, canvas.width, canvas.height);

    const barWidth = (canvas.width / bufferLength) * 1.6;

    let x = 0;

    for (let i = 0; i < bufferLength; i++) {
        const value = dataArray[i] / 255;

        const barHeight =
            Math.max(2, value * canvas.height);

        canvasCtx.fillStyle = "#1db954";

        canvasCtx.fillRect(
            x,
            canvas.height - barHeight,
            Math.max(1, barWidth - 1),
            barHeight
        );

        x += barWidth;
    }
}


/* ============================================================
   PLAYER HELPERS
   ============================================================ */

function formatSecs(sec) {
    sec = Math.floor(sec || 0);

    const m = Math.floor(sec / 60);
    const s = sec % 60;

    return `${m}:${s < 10 ? "0" : ""}${s}`;
}


function updatePlayerProgress() {
    if (!globalAudio) return;

    const duration = globalAudio.duration;

    if (!duration || !isFinite(duration)) {
        if (gpSeek) gpSeek.value = 0;
        if (gpCurTime) gpCurTime.textContent = "0:00";
        return;
    }

    const percentage =
        (globalAudio.currentTime / duration) * 100;

    if (gpSeek) {
        gpSeek.value = percentage;
    }

    if (gpCurTime) {
        gpCurTime.textContent =
            formatSecs(globalAudio.currentTime);
    }

    if (gpDurTime) {
        gpDurTime.textContent =
            formatSecs(duration);
    }
}


function setPlayerPlayingState(isPlaying) {
    if (!gpPlayBtn) return;

    gpPlayBtn.textContent = isPlaying ? "❚❚" : "▶";

    gpPlayBtn.classList.toggle(
        "is-playing",
        isPlaying
    );

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


function resetPreviewButton(btn) {
    if (!btn) return;

    btn.classList.remove("playing");

    btn.innerHTML =
        btn.dataset.type === "library"
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
            title || "Unknown Track";
    }

    if (gpArtist) {
        gpArtist.textContent =
            artist || "Unknown Artist";
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
        setPlayerPlayingState(true);
    };


    globalAudio.onpause = () => {
        setPlayerPlayingState(false);
    };


    globalAudio.onended = () => {

        setPlayerPlayingState(false);

        if (gpSeek) {
            gpSeek.value = 0;
        }

        if (gpCurTime) {
            gpCurTime.textContent = "0:00";
        }

        if (activePreviewBtn) {
            resetPreviewButton(activePreviewBtn);
            activePreviewBtn = null;
        }

        if (gpBar) {
            gpBar.classList.remove("is-playing");
        }
    };


    globalAudio.onerror = () => {

        setPlayerPlayingState(false);

        if (activePreviewBtn) {
            activePreviewBtn.innerHTML = "❌ Error";

            const failedBtn = activePreviewBtn;

            setTimeout(() => {
                resetPreviewButton(failedBtn);
            }, 2000);
        }
    };
}


/* ============================================================
   PLAY / PAUSE
   ============================================================ */

if (gpPlayBtn) {

    gpPlayBtn.onclick = async () => {

        if (!globalAudio) return;

        initAudioContext();

        if (
            audioCtx &&
            audioCtx.state === "suspended"
        ) {
            await audioCtx.resume();
        }

        if (globalAudio.paused) {

            try {
                await globalAudio.play();

                setPlayerPlayingState(true);

            } catch (err) {
                console.error(
                    "Playback failed:",
                    err
                );
            }

        } else {

            globalAudio.pause();

            setPlayerPlayingState(false);
        }
    };
}


/* ============================================================
   SEEK
   ============================================================ */

if (gpSeek) {

    gpSeek.oninput = () => {

        if (
            globalAudio &&
            globalAudio.duration &&
            isFinite(globalAudio.duration)
        ) {
            globalAudio.currentTime =
                (gpSeek.value / 100) *
                globalAudio.duration;
        }
    };
}


/* ============================================================
   VOLUME
   ============================================================ */

if (gpVolume) {

    gpVolume.oninput = () => {

        if (!globalAudio) return;

        globalAudio.volume =
            parseFloat(gpVolume.value);

        localStorage.setItem(
            "xrob_music_volume",
            gpVolume.value
        );
    };

    const savedVolume =
        localStorage.getItem(
            "xrob_music_volume"
        );

    if (savedVolume !== null) {
        gpVolume.value = savedVolume;

        globalAudio.volume =
            parseFloat(savedVolume);
    } else {
        globalAudio.volume = 0.8;
        gpVolume.value = 0.8;
    }
}


/* ============================================================
   GLOBAL PLAYER STREAM
   ============================================================ */

function stopCurrentPreview() {

    if (!globalAudio) return;

    globalAudio.pause();

    globalAudio.removeAttribute("src");
    globalAudio.load();

    setPlayerPlayingState(false);

    if (activePreviewBtn) {
        resetPreviewButton(activePreviewBtn);
        activePreviewBtn = null;
    }

    if (gpSeek) {
        gpSeek.value = 0;
    }

    if (gpCurTime) {
        gpCurTime.textContent = "0:00";
    }

    if (gpDurTime) {
        gpDurTime.textContent = "0:00";
    }
}


function toggleAudioStream(
    btn,
    streamUrl,
    type = "search",
    title = "Track",
    artist = "Artist",
    artUrl = ""
) {

    if (!globalAudio || !btn) return;

    initAudioContext();

    if (
        audioCtx &&
        audioCtx.state === "suspended"
    ) {
        audioCtx.resume();
    }


    /* Same track → toggle pause/play */

    if (
        activePreviewBtn === btn &&
        globalAudio.src ===
        new URL(
            streamUrl,
            window.location.href
        ).href
    ) {

        if (globalAudio.paused) {

            globalAudio.play()
                .then(() => {
                    setPlayerPlayingState(true);
                    btn.innerHTML = "❚❚ Pause";
                })
                .catch(() => {});

        } else {

            globalAudio.pause();

            setPlayerPlayingState(false);

            btn.innerHTML =
                type === "library"
                    ? "▶ Play"
                    : "▶ Preview";
        }

        return;
    }


    /* Stop previous track */

    if (activePreviewBtn) {
        resetPreviewButton(
            activePreviewBtn
        );
    }


    /* Activate new button */

    btn.dataset.type = type;
    activePreviewBtn = btn;

    btn.innerHTML = "⏳ Loading...";


    /* Update player */

    updatePlayerInfo(
        title,
        artist,
        artUrl
    );


    if (gpBar) {
        gpBar.style.display = "flex";
        gpBar.classList.add("loading");
    }


    if (gpSeek) {
        gpSeek.value = 0;
    }

    if (gpCurTime) {
        gpCurTime.textContent = "0:00";
    }

    if (gpDurTime) {
        gpDurTime.textContent = "0:00";
    }


    /* Load audio */

    globalAudio.pause();

    globalAudio.src = streamUrl;

    globalAudio.load();


    globalAudio.play()
        .then(() => {

            if (gpBar) {
                gpBar.classList.remove(
                    "loading"
                );
            }

            setPlayerPlayingState(true);

            btn.innerHTML = "❚❚ Pause";

        })
        .catch(err => {

            console.error(
                "Audio playback error:",
                err
            );

            if (gpBar) {
                gpBar.classList.remove(
                    "loading"
                );
            }

            btn.innerHTML = "❌ Error";

            setTimeout(() => {

                if (btn === activePreviewBtn) {
                    resetPreviewButton(btn);
                }

            }, 2000);
        });
}


/* ============================================================
   WEBSOCKET
   ============================================================ */

let socket = null;

function initWebSocket() {

    const protocol =
        window.location.protocol === "https:"
            ? "wss:"
            : "ws:";

    socket = new WebSocket(
        `${protocol}//${window.location.host}/ws`
    );


    socket.onopen = () => {
        console.log(
            "Xrob Music WebSocket connected"
        );
    };


    socket.onmessage = event => {

        try {

            const data =
                JSON.parse(event.data);

            if (
                data.type ===
                "task_update"
            ) {
                pollTasks();
            }

        } catch (e) {}
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
        } catch (e) {}
    };
}


/* ============================================================
   NOTIFICATIONS
   ============================================================ */

function requestNotificationPermission() {

    if (!("Notification" in window))
        return;

    Notification
        .requestPermission()
        .then(permission => {

            if (permission === "granted") {

                showToast(
                    "✅ Notifications enabled!"
                );

            } else {

                showToast(
                    "⚠️ Notification permission denied."
                );
            }
        });
}


function notifyTrackComplete(title) {

    showToast(
        `🎉 Track ready: ${title}`
    );

    if (
        "Notification" in window &&
        Notification.permission === "granted"
    ) {

        new Notification(
            "Track Installed Successfully!",
            {
                body:
                    `${title} is now downloaded and ready in your library.`,
                icon:
                    "https://via.placeholder.com/64?text=🎵"
            }
        );
    }
}


function showToast(message) {

    const container =
        document.getElementById(
            "toast-container"
        );

    if (!container) return;

    const toast =
        document.createElement("div");

    toast.className = "toast";
    toast.textContent = message;

    container.appendChild(toast);

    setTimeout(() => {
        toast.remove();
    }, 4000);
}


/* ============================================================
   THEME
   ============================================================ */

function toggleTheme(theme) {

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

toggleTheme(savedTheme);


/* ============================================================
   NAVIGATION
   ============================================================ */

function navigate(
    tab,
    updateHash = true
) {

    if (updateHash) {
        window.location.hash = tab;
    }

    switchTab(tab);
}


function switchTab(tab) {

    document
        .querySelectorAll(".tab-content")
        .forEach(c =>
            c.classList.remove("active")
        );

    document
        .querySelectorAll(".nav-link")
        .forEach(b => {

            b.classList.remove("active");

            b.setAttribute(
                "aria-selected",
                "false"
            );
        });


    const activeContent =
        document.getElementById(
            `tab-${tab}`
        );

    if (activeContent) {
        activeContent.classList.add(
            "active"
        );
    }


    const sideBtn =
        document.getElementById(
            `btn-${tab}`
        );

    const mobBtn =
        document.getElementById(
            `mob-btn-${tab}`
        );


    if (sideBtn) {

        sideBtn.classList.add("active");

        sideBtn.setAttribute(
            "aria-selected",
            "true"
        );
    }


    if (mobBtn) {

        mobBtn.classList.add("active");

        mobBtn.setAttribute(
            "aria-selected",
            "true"
        );
    }


    if (tab === "library")
        loadLibrary();

    if (tab === "downloads")
        loadDownloads();

    if (tab === "settings")
        loadSettings();
}


function handleDeepLink() {

    const hash =
        window.location.hash.replace(
            "#",
            ""
        );

    if (
        [
            "search",
            "downloads",
            "library",
            "settings"
        ].includes(hash)
    ) {

        switchTab(hash);

    } else {

        switchTab("search");
    }
}


window.addEventListener(
    "hashchange",
    handleDeepLink
);


/* ============================================================
   HELPERS
   ============================================================ */

function normalizeKey(value) {

    return (value || "")
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


function escapeHtml(t) {

    return (t || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(
            /"/g,
            "&quot;"
        );
}


/* ============================================================
   SETTINGS
   ============================================================ */

async function loadSettings() {

    try {

        const res =
            await fetch(
                "api/settings"
            );

        const s =
            await res.json();


        document.getElementById(
            "set_format"
        ).value =
            s.audio_format || "mp3";


        document.getElementById(
            "set_quality"
        ).value =
            s.audio_quality || "320K";


        document.getElementById(
            "set_thumb"
        ).checked =
            s.embed_thumbnail;


        document.getElementById(
            "set_meta"
        ).checked =
            s.embed_metadata;


        document.getElementById(
            "set_max_results"
        ).value =
            s.max_results || 20;


        document.getElementById(
            "set_organize"
        ).checked =
            !!s.organize_by_artist;


        document.getElementById(
            "set_theme"
        ).value =
            localStorage.getItem(
                "xrob_music_theme"
            ) || "dark";


        if (document.getElementById("set_subsonic_user")) {
            document.getElementById("set_subsonic_user").value = s.subsonic_user || "admin";
        }

        if (document.getElementById("set_subsonic_pass")) {
            document.getElementById("set_subsonic_pass").value = s.subsonic_pass || "admin";
        }

    } catch (e) {}
}


async function saveSettings() {

    const data = {

        audio_format:
            document.getElementById(
                "set_format"
            ).value,

        audio_quality:
            document.getElementById(
                "set_quality"
            ).value,

        embed_thumbnail:
            document.getElementById(
                "set_thumb"
            ).checked,

        embed_metadata:
            document.getElementById(
                "set_meta"
            ).checked,

        max_results:
            parseInt(
                document.getElementById(
                    "set_max_results"
                ).value
            ) || 20,

        organize_by_artist:
            document.getElementById(
                "set_organize"
            ).checked,

        subsonic_user:
            document.getElementById("set_subsonic_user")
                ? document.getElementById("set_subsonic_user").value
                : "admin",

        subsonic_pass:
            document.getElementById("set_subsonic_pass")
                ? document.getElementById("set_subsonic_pass").value
                : "admin"
    };


    const msg =
        document.getElementById(
            "settingsMsg"
        );

    msg.textContent = "Saving...";


    try {

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

        msg.textContent =
            "✅ Settings saved!";

        setTimeout(
            () =>
                msg.textContent = "",
            3000
        );

    } catch (e) {

        msg.textContent =
            "❌ Failed to save settings.";
    }
}


/* ============================================================
   LIBRARY CACHE
   ============================================================ */

async function refreshLibraryCache() {

    try {

        const res =
            await fetch(
                "api/library"
            );

        const data =
            await res.json();


        libraryFilesSet.clear();

        rawLibraryFiles =
            data.files || [];


        rawLibraryFiles.forEach(f => {

            const baseName =
                f.name.substring(
                    f.name.lastIndexOf("/") + 1,
                    f.name.lastIndexOf(".")
                ) || f.name;

            libraryFilesSet.add(
                normalizeKey(baseName)
            );
        });


        const count =
            rawLibraryFiles.length;


        if (
            document.getElementById(
                "sideLibCount"
            )
        )
            document.getElementById(
                "sideLibCount"
            ).textContent = count;


        if (
            document.getElementById(
                "mobLibCount"
            )
        )
            document.getElementById(
                "mobLibCount"
            ).textContent = count;


        if (
            document.getElementById(
                "libCountDetail"
            )
        )
            document.getElementById(
                "libCountDetail"
            ).textContent = count;


        if (
            document.getElementById(
                "libFolderSize"
            )
        )
            document.getElementById(
                "libFolderSize"
            ).textContent =
                data.total_size;

    } catch (e) {}
}


/* ============================================================
   SEARCH RESULTS
   ============================================================ */

function renderItems(data) {

    const results =
        document.getElementById(
            "results"
        );


    data.forEach(item => {

        const cleanedTitle =
            normalizeKey(
                item.title || "Unknown"
            );


        const isInLibrary =
            libraryFilesSet.has(
                cleanedTitle
            );


        const card =
            document.createElement("div");

        card.className =
            "result-card";


        const thumbUrl =
            escapeHtml(
                item.thumbnail
            );

        const titleHtml =
            escapeHtml(
                item.title
            );

        const artistHtml =
            escapeHtml(
                item.channel
            );

        const durHtml =
            escapeHtml(
                item.duration_text
            );


        card.innerHTML = `
            <div class="thumb-wrapper">
                <img
                    src="${thumbUrl}"
                    alt="Cover art for ${titleHtml}"
                    onerror="this.src='https://via.placeholder.com/110x65?text=Music'"
                />
                <span class="badge-duration">
                    ${durHtml}
                </span>
            </div>

            <div class="track-info">
                <div class="track-title">
                    ${titleHtml}
                </div>

                <div class="track-artist">
                    👤 ${artistHtml}
                </div>
            </div>

            <div
                class="btn-group"
                data-group-id="${item.id}"
            ></div>
        `;


        const btnGroup =
            card.querySelector(
                ".btn-group"
            );


        if (isInLibrary) {

            btnGroup.innerHTML =
                `<div class="badge-library">
                    ✅ In Library
                </div>`;

        } else {

            const prevBtn =
                document.createElement(
                    "button"
                );

            prevBtn.className =
                "btn-preview";

            prevBtn.setAttribute(
                "aria-label",
                `Preview ${titleHtml}`
            );

            prevBtn.innerHTML =
                "▶ Preview";


            prevBtn.onclick = () =>
                toggleAudioStream(
                    prevBtn,
                    "api/preview?url=" +
                        encodeURIComponent(
                            item.url
                        ),
                    "search",
                    item.title,
                    item.channel,
                    item.thumbnail
                );


            const dlBtn =
                document.createElement(
                    "button"
                );

            dlBtn.className =
                "btn-download";

            dlBtn.setAttribute(
                "data-id",
                item.id
            );

            dlBtn.setAttribute(
                "aria-label",
                `Download ${titleHtml}`
            );

            dlBtn.innerHTML =
                "⬇️ Save";


            dlBtn.onclick = () =>
                startDownload(
                    item.url,
                    item.title,
                    item.id,
                    item.channel
                );


            btnGroup.appendChild(
                prevBtn
            );

            btnGroup.appendChild(
                dlBtn
            );
        }


        results.appendChild(card);
    });
}


/* ============================================================
   SEARCH
   ============================================================ */

async function searchMusic() {

    const query =
        document.getElementById(
            "query"
        ).value.trim();


    const statusMsg =
        document.getElementById(
            "statusMsg"
        );

    const results =
        document.getElementById(
            "results"
        );

    const searchBtn =
        document.getElementById(
            "searchBtn"
        );


    if (!query) return;


    currentQuery = query;
    currentPage = 1;
    hasMoreResults = true;
    isLoadingMore = false;


    statusMsg.textContent =
        "🔍 Searching YouTube...";


    results.innerHTML = "";

    searchBtn.disabled = true;


    await refreshLibraryCache();


    try {

        const response =
            await fetch(
                `api/search?q=${encodeURIComponent(query)}&page=1`
            );


        if (!response.ok)
            throw new Error(
                "Search failed"
            );


        const data =
            await response.json();


        if (data.length === 0) {

            statusMsg.textContent =
                "No results found.";

            hasMoreResults = false;

            return;
        }


        statusMsg.textContent = "";

        renderItems(data);

    } catch (err) {

        statusMsg.textContent =
            "❌ " + err.message;

    } finally {

        searchBtn.disabled = false;
    }
}


async function loadMoreResults() {

    if (
        isLoadingMore ||
        !hasMoreResults ||
        !currentQuery
    )
        return;


    isLoadingMore = true;

    currentPage++;


    const loader =
        document.getElementById(
            "infiniteLoader"
        );


    if (loader)
        loader.style.display = "block";


    try {

        const response =
            await fetch(
                `api/search?q=${encodeURIComponent(currentQuery)}&page=${currentPage}`
            );


        if (!response.ok)
            throw new Error(
                "Load failed"
            );


        const data =
            await response.json();


        if (
            !data ||
            data.length === 0
        ) {

            hasMoreResults = false;

        } else {

            renderItems(data);
        }

    } catch (e) {

        hasMoreResults = false;

    } finally {

        if (loader)
            loader.style.display = "none";

        isLoadingMore = false;
    }
}


/* ============================================================
   DOWNLOADS
   ============================================================ */

async function startDownload(
    url,
    title,
    elementId,
    artist = "Unknown Artist"
) {

    if (elementId) {

        const btn =
            document.querySelector(
                `button[data-id="${elementId}"]`
            );

        if (btn) {

            btn.disabled = true;
            btn.textContent =
                "⏳ Queued";
        }
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


        if (!response.ok) {

            const error =
                await response.json()
                    .catch(() => ({}));

            throw new Error(
                error.detail ||
                "Failed to enqueue download."
            );
        }


        pollTasks();

    } catch (e) {

        alert(
            e.message ||
            "Failed to enqueue download."
        );
    }
}


/* ============================================================
   TASK POLLING
   ============================================================ */

async function pollTasks() {

    try {

        const res =
            await fetch(
                "api/tasks"
            );

        const tasks =
            await res.json();


        let libraryNeedsUpdate =
            false;


        tasks.forEach(t => {

            if (
                t.status === "completed" &&
                !completedSet.has(t.id)
            ) {

                completedSet.add(t.id);

                libraryNeedsUpdate =
                    true;

                notifyTrackComplete(
                    t.title
                );


                if (t.elementId) {

                    const grp =
                        document.querySelector(
                            `div[data-group-id="${t.elementId}"]`
                        );


                    if (grp) {

                        grp.innerHTML =
                            `<div class="badge-library">
                                ✅ In Library
                            </div>`;
                    }
                }
            }
        });


        if (libraryNeedsUpdate) {

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
                loadLibrary();
            }
        }


        const activeTasks =
            tasks.filter(t =>

                t.status === "queued" ||

                t.status === "downloading" ||

                t.status === "processing" ||

                (
                    t.status === "error" &&
                    (
                        Date.now() -
                        t.last_updated <
                        4000
                    )
                ) ||

                (
                    t.status === "completed" &&
                    (
                        Date.now() -
                        t.last_updated <
                        2000
                    )
                )
            );


        const panel =
            document.getElementById(
                "progressPanel"
            );

        const listContainer =
            document.getElementById(
                "activeDownloadsList"
            );


        if (!panel || !listContainer)
            return;


        if (
            activeTasks.length === 0
        ) {

            panel.style.display =
                "none";

            listContainer.innerHTML =
                "";

            return;
        }


        panel.style.display =
            "block";

        listContainer.innerHTML =
            "";


        activeTasks.forEach(task => {

            const isError =
                task.status === "error";


            const barColor =
                isError
                    ? "var(--danger)"
                    : "linear-gradient(90deg, var(--accent) 0%, #a855f7 100%)";


            const percent =
                Math.round(
                    task.percent || 0
                );


            let step1 =
                "step-item";

            let step2 =
                "step-item";

            let step3 =
                "step-item";


            if (
                task.status ===
                "downloading"
            ) {

                step1 += " active";

            } else if (
                task.status ===
                "processing"
            ) {

                step1 +=
                    " completed";

                step2 +=
                    " active";

            } else if (
                task.status ===
                "completed"
            ) {

                step1 +=
                    " completed";

                step2 +=
                    " completed";

                step3 +=
                    " completed";

            } else if (isError) {

                step1 += " active";
            }


            const itemHtml = `

                <div
                    style="
                        background: var(--input-bg);
                        border: 1px solid var(--card-border);
                        padding: 14px;
                        border-radius: 12px;
                    "
                >

                    <div class="progress-header">

                        <span class="progress-title">
                            ${
                                isError
                                    ? "❌ "
                                    : "🎵 "
                            }

                            ${escapeHtml(
                                task.title
                            )}
                        </span>

                        <div class="progress-right-header">

                            <span
                                class="progress-percent"
                                style="
                                    color:
                                    ${
                                        isError
                                            ? "var(--danger)"
                                            : "var(--accent)"
                                    }
                                "
                            >
                                ${percent}%
                            </span>

                        </div>

                    </div>


                    <div class="progress-track">

                        <div
                            class="progress-fill"
                            style="
                                width: ${percent}%;
                                background: ${barColor};
                            "
                        ></div>

                    </div>


                    <div class="progress-steps">

                        <div class="${step1}">
                            <span class="step-dot"></span>
                            1. Download
                        </div>

                        <div class="${step2}">
                            <span class="step-dot"></span>
                            2. Clean Tags
                        </div>

                        <div class="${step3}">
                            <span class="step-dot"></span>
                            3. Ready
                        </div>

                    </div>


                    <div class="progress-details">

                        <span>
                            ${escapeHtml(
                                task.error ||
                                task.step ||
                                "Queued..."
                            )}
                        </span>

                        <span>
                            ${escapeHtml(
                                task.speed || ""
                            )}
                        </span>

                    </div>

                </div>
            `;


            listContainer.insertAdjacentHTML(
                "beforeend",
                itemHtml
            );
        });


    } catch (e) {}
}


/* ============================================================
   STATS
   ============================================================ */

async function loadStats() {

    try {

        const stats =
            await fetch(
                "api/stats"
            ).then(r => r.json());


        document.getElementById(
            "statTracks"
        ).textContent =
            stats.tracks || 0;


        document.getElementById(
            "statArtists"
        ).textContent =
            stats.artists || 0;


        document.getElementById(
            "statAlbums"
        ).textContent =
            stats.albums || 0;

    } catch (e) {}
}


/* ============================================================
   DOWNLOAD MANAGEMENT
   ============================================================ */

async function cancelTask(taskId) {

    await fetch(
        `api/tasks/${encodeURIComponent(taskId)}/cancel`,
        {
            method: "POST"
        }
    );

    loadDownloads();
}


async function clearDoneTasks() {

    try {

        await fetch(
            "api/tasks/clear-completed",
            {
                method: "DELETE"
            }
        );

        loadDownloads();

    } catch (e) {

        alert(
            "Failed to clear finished downloads."
        );
    }
}


async function loadDownloads() {

    const list =
        document.getElementById(
            "downloadsList"
        );


    try {

        const tasks =
            await fetch(
                "api/tasks"
            ).then(r => r.json());


        document.getElementById(
            "queueCount"
        ).textContent =

            tasks.filter(t =>
                [
                    "queued",
                    "downloading",
                    "processing"
                ].includes(
                    t.status
                )
            ).length;


        if (!tasks.length) {

            list.innerHTML =
                '<div class="status-msg">No downloads yet.</div>';

            return;
        }


        list.innerHTML =
            tasks.map(t => `

                <div class="result-card">

                    <div class="track-info">

                        <div class="track-title">
                            ${escapeHtml(
                                t.title
                            )}
                        </div>

                        <div class="track-artist">
                            ${escapeHtml(
                                t.artist ||
                                "Unknown Artist"
                            )}
                            ·
                            ${escapeHtml(
                                t.status
                            )}
                            ·
                            ${Math.round(
                                t.percent || 0
                            )}%
                        </div>


                        <div
                            class="progress-track"
                            style="margin-top:8px"
                        >

                            <div
                                class="progress-fill"
                                style="
                                    width:
                                    ${Math.round(
                                        t.percent ||
                                        0
                                    )}%
                                "
                            ></div>

                        </div>

                    </div>


                    <div class="btn-group">

                        ${
                            [
                                "queued",
                                "downloading",
                                "processing"
                            ].includes(
                                t.status
                            )

                            ?

                            `<button
                                class="btn-danger"
                                onclick="cancelTask('${t.id}')"
                            >
                                ✕ Cancel
                            </button>`

                            : ""
                        }

                    </div>

                </div>

            `).join("");


    } catch (e) {

        list.innerHTML =
            '<div class="status-msg">Failed to load downloads.</div>';
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


    list.innerHTML =
        `<div class="status-msg">
            Loading library...
        </div>`;


    try {

        await refreshLibraryCache();

        await loadStats();

        filterLibrary();

    } catch (e) {

        list.innerHTML =
            `<div class="status-msg">
                Failed to load library.
            </div>`;
    }
}


function filterLibrary() {

    const list =
        document.getElementById(
            "libraryList"
        );


    const searchInput =
        document.getElementById(
            "libSearchQuery"
        );


    const q =
        (
            searchInput
                ? searchInput.value
                : ""
        )
        .toLowerCase()
        .trim();


    const filtered =
        rawLibraryFiles.filter(
            f =>
                f.name
                    .toLowerCase()
                    .includes(q)
        );


    if (
        filtered.length === 0
    ) {

        list.innerHTML =
            `<div class="status-msg">
                ${
                    rawLibraryFiles.length === 0
                        ? "No files downloaded yet."
                        : "No matching tracks found."
                }
            </div>`;

        return;
    }


    list.innerHTML = "";


    filtered.forEach(f => {

        const card =
            document.createElement(
                "div"
            );

        card.className =
            "result-card";


        const encName =
            encodeURIComponent(
                f.name
            );


        const coverUrl =
            "api/library/cover/" +
            encName;


        const streamUrl =
            "api/library/stream/" +
            encName;


        const fallbackSvg =
            `data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="110" height="65" viewBox="0 0 110 65"><rect width="100%" height="100%" fill="%231e293b"/><text x="50%" y="50%" fill="%239ca3af" font-size="20" text-anchor="middle" dominant-baseline="central">🎵</text></svg>`;


        card.innerHTML = `

            <div class="thumb-wrapper">

                <img
                    src="${coverUrl}"
                    alt="Album cover for ${escapeHtml(f.name)}"
                    onerror="
                        this.onerror=null;
                        this.src='${fallbackSvg}'
                    "
                />

            </div>


            <div class="track-info">

                <div class="track-title">
                    ${escapeHtml(f.name)}
                </div>

                <div class="track-artist">
                    📦 ${f.size}
                </div>

            </div>


            <div class="btn-group">

                <button
                    class="btn-preview"
                    aria-label="Play ${escapeHtml(f.name)}"
                >
                    ▶ Play
                </button>


                <button
                    class="btn-danger"
                    aria-label="Delete ${escapeHtml(f.name)}"
                >
                    🗑 Delete
                </button>

            </div>
        `;


        const playBtn =
            card.querySelector(
                ".btn-preview"
            );


        playBtn.onclick = () =>
            toggleAudioStream(
                playBtn,
                streamUrl,
                "library",
                f.name,
                "Local Library",
                coverUrl
            );


        const delBtn =
            card.querySelector(
                ".btn-danger"
            );


        delBtn.onclick = () =>
            deleteFile(
                f.name
            );


        list.appendChild(
            card
        );
    });
}


/* ============================================================
   DELETE LIBRARY FILE
   ============================================================ */

async function deleteFile(filename) {

    if (
        !confirm(
            "Delete " + filename + "?"
        )
    )
        return;


    try {

        await fetch(
            "api/library/" +
                encodeURIComponent(
                    filename
                ),
            {
                method: "DELETE"
            }
        );


        await refreshLibraryCache();

        await loadStats();

        filterLibrary();


    } catch (e) {

        alert(
            "Failed to delete file."
        );
    }
}


/* ============================================================
   SEARCH EVENTS
   ============================================================ */

const searchBtn =
    document.getElementById(
        "searchBtn"
    );


if (searchBtn) {

    searchBtn.addEventListener(
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
        e => {

            if (
                e.key === "Enter"
            ) {
                searchMusic();
            }
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
            searchTab &&
            searchTab.classList.contains(
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
    }
);


/* ============================================================
   STARTUP
   ============================================================ */

refreshLibraryCache();


document.addEventListener(
    "DOMContentLoaded",
    () => {

        handleDeepLink();

        pollTasks();

        initWebSocket();

    }
);
