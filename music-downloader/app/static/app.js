/* ============================================================
   XROB MUSIC
   Spotify / Deezer inspired frontend
   Backend/API endpoints preserved

   Download Queue:
   - Search -> Download
   - Automatically navigate to Downloads
   - Downloads page is the single queue/history location
   - Live updates via WebSocket + polling
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

let latestTasks = [];
let lastRenderedTaskSignature = "";


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

    visualizerAnimationFrame =
        requestAnimationFrame(drawVisualizer);

    const bufferLength = analyser.frequencyBinCount;
    const dataArray = new Uint8Array(bufferLength);

    analyser.getByteFrequencyData(dataArray);

    canvasCtx.clearRect(
        0,
        0,
        canvas.width,
        canvas.height
    );

    const barWidth =
        (canvas.width / bufferLength) * 1.6;

    let x = 0;

    for (let i = 0; i < bufferLength; i++) {

        const value =
            dataArray[i] / 255;

        const barHeight =
            Math.max(
                2,
                value * canvas.height
            );

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

    const m =
        Math.floor(sec / 60);

    const s =
        sec % 60;

    return `${m}:${s < 10 ? "0" : ""}${s}`;
}


function updatePlayerProgress() {

    if (!globalAudio) return;

    const duration =
        globalAudio.duration;

    if (
        !duration ||
        !isFinite(duration)
    ) {

        if (gpSeek)
            gpSeek.value = 0;

        if (gpCurTime)
            gpCurTime.textContent = "0:00";

        return;
    }

    const percentage =
        (globalAudio.currentTime / duration) * 100;

    if (gpSeek)
        gpSeek.value = percentage;

    if (gpCurTime) {
        gpCurTime.textContent =
            formatSecs(
                globalAudio.currentTime
            );
    }

    if (gpDurTime) {
        gpDurTime.textContent =
            formatSecs(duration);
    }
}


function setPlayerPlayingState(isPlaying) {

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

        if (gpSeek)
            gpSeek.value = 0;

        if (gpCurTime)
            gpCurTime.textContent = "0:00";

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

        setPlayerPlayingState(false);

        if (activePreviewBtn) {

            activePreviewBtn.innerHTML =
                "❌ Error";

            const failedBtn =
                activePreviewBtn;

            setTimeout(() => {

                resetPreviewButton(
                    failedBtn
                );

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


    if (savedVolume !== null) {

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
   GLOBAL PLAYER STREAM
   ============================================================ */

function stopCurrentPreview() {

    if (!globalAudio) return;

    globalAudio.pause();

    globalAudio.removeAttribute("src");
    globalAudio.load();

    setPlayerPlayingState(false);

    if (activePreviewBtn) {

        resetPreviewButton(
            activePreviewBtn
        );

        activePreviewBtn = null;
    }

    if (gpSeek)
        gpSeek.value = 0;

    if (gpCurTime)
        gpCurTime.textContent = "0:00";

    if (gpDurTime)
        gpDurTime.textContent = "0:00";
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


    const absoluteUrl =
        new URL(
            streamUrl,
            window.location.href
        ).href;


    /* Same track -> toggle */

    if (
        activePreviewBtn === btn &&
        globalAudio.src === absoluteUrl
    ) {

        if (globalAudio.paused) {

            globalAudio.play()
                .then(() => {

                    setPlayerPlayingState(true);

                    btn.innerHTML =
                        type === "library"
                            ? "❚❚ Pause"
                            : "❚❚ Pause";

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


    if (activePreviewBtn) {

        resetPreviewButton(
            activePreviewBtn
        );
    }


    btn.dataset.type =
        type;

    activePreviewBtn =
        btn;

    btn.innerHTML =
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


    if (gpSeek)
        gpSeek.value = 0;

    if (gpCurTime)
        gpCurTime.textContent = "0:00";

    if (gpDurTime)
        gpDurTime.textContent = "0:00";


    globalAudio.pause();

    globalAudio.src =
        streamUrl;

    globalAudio.load();


    globalAudio.play()
        .then(() => {

            if (gpBar) {

                gpBar.classList.remove(
                    "loading"
                );
            }

            setPlayerPlayingState(true);

            btn.innerHTML =
                "❚❚ Pause";

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

            btn.innerHTML =
                "❌ Error";

            setTimeout(() => {

                if (
                    btn ===
                    activePreviewBtn
                ) {

                    resetPreviewButton(
                        btn
                    );
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


    try {

        socket =
            new WebSocket(
                `${protocol}//${window.location.host}/ws`
            );

    } catch (e) {

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


function startTaskPolling() {

    if (pollTimer) return;

    pollTimer =
        setInterval(
            pollTasks,
            2000
        );
}


/* ============================================================
   NOTIFICATIONS
   ============================================================ */

function requestNotificationPermission() {

    if (!("Notification" in window)) {

        showToast(
            "⚠️ Browser notifications are not supported."
        );

        return;
    }


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
        `🎉 ${title} is ready in your library`
    );


    if (
        "Notification" in window &&
        Notification.permission === "granted"
    ) {

        new Notification(
            "Xrob Music",
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

    toast.className =
        "toast";

    toast.textContent =
        message;


    container.appendChild(
        toast
    );


    setTimeout(() => {

        toast.style.opacity = "0";
        toast.style.transform =
            "translateX(20px)";

        setTimeout(
            () => toast.remove(),
            250
        );

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


function switchTab(tab) {

    document
        .querySelectorAll(".tab-content")
        .forEach(c => {

            c.classList.remove(
                "active"
            );
        });


    document
        .querySelectorAll(".nav-link")
        .forEach(b => {

            b.classList.remove(
                "active"
            );

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

        sideBtn.classList.add(
            "active"
        );

        sideBtn.setAttribute(
            "aria-selected",
            "true"
        );
    }


    if (mobBtn) {

        mobBtn.classList.add(
            "active"
        );

        mobBtn.setAttribute(
            "aria-selected",
            "true"
        );
    }


    if (tab === "library") {

        loadLibrary();
    }


    if (tab === "downloads") {

        loadDownloads();
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

    return String(t ?? "")
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

        const res =
            await fetch(
                "api/settings"
            );


        const s =
            await res.json();


        const setValue =
            (id, value) => {

                const el =
                    document.getElementById(
                        id
                    );

                if (el)
                    el.value =
                        value;
            };


        const setChecked =
            (id, value) => {

                const el =
                    document.getElementById(
                        id
                    );

                if (el)
                    el.checked =
                        !!value;
            };


        setValue(
            "set_format",
            s.audio_format || "mp3"
        );


        setValue(
            "set_quality",
            s.audio_quality || "320K"
        );


        setChecked(
            "set_thumb",
            s.embed_thumbnail
        );


        setChecked(
            "set_meta",
            s.embed_metadata
        );


        setValue(
            "set_max_results",
            s.max_results || 20
        );


        setChecked(
            "set_organize",
            s.organize_by_artist
        );


        setValue(
            "set_theme",
            localStorage.getItem(
                "xrob_music_theme"
            ) || "dark"
        );


        setValue(
            "set_navidrome_url",
            s.navidrome_url || ""
        );


        setValue(
            "set_navidrome_user",
            s.navidrome_user || ""
        );


        setValue(
            "set_navidrome_token",
            s.navidrome_token || ""
        );

    } catch (e) {

        console.warn(
            "Settings load failed:",
            e
        );
    }
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

        navidrome_url:
            document.getElementById(
                "set_navidrome_url"
            ).value,

        navidrome_user:
            document.getElementById(
                "set_navidrome_user"
            ).value,

        navidrome_token:
            document.getElementById(
                "set_navidrome_token"
            ).value
    };


    const msg =
        document.getElementById(
            "settingsMsg"
        );


    if (msg)
        msg.textContent =
            "Saving...";


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


        if (!response.ok)
            throw new Error(
                "Failed to save settings"
            );


        if (msg)
            msg.textContent =
                "✅ Settings saved!";


        showToast(
            "✅ Settings saved"
        );


        setTimeout(() => {

            if (msg)
                msg.textContent = "";

        }, 3000);

    } catch (e) {

        if (msg)
            msg.textContent =
                "❌ Failed to save settings.";

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

        const res =
            await fetch(
                "api/library"
            );


        if (!res.ok)
            throw new Error(
                "Library request failed"
            );


        const data =
            await res.json();


        libraryFilesSet.clear();

        rawLibraryFiles =
            data.files || [];


        rawLibraryFiles.forEach(
            f => {

                const name =
                    String(
                        f.name || ""
                    );


                const dot =
                    name.lastIndexOf(".");


                const slash =
                    name.lastIndexOf("/");


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


        const mobCount =
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


        if (sideCount)
            sideCount.textContent =
                count;


        if (mobCount)
            mobCount.textContent =
                count;


        if (detailCount)
            detailCount.textContent =
                count;


        if (folderSize)
            folderSize.textContent =
                data.total_size || "0 MB";


    } catch (e) {

        console.warn(
            "Library cache update failed:",
            e
        );
    }
}


/* ============================================================
   SEARCH RESULTS
   ============================================================ */

function renderItems(data) {

    const results =
        document.getElementById(
            "results"
        );


    if (!results) return;


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
            document.createElement(
                "div"
            );


        card.className =
            "result-card";


        card.dataset.elementId =
            item.id || "";


        const thumbUrl =
            escapeHtml(
                item.thumbnail || ""
            );


        const titleHtml =
            escapeHtml(
                item.title || "Unknown Track"
            );


        const artistHtml =
            escapeHtml(
                item.channel || "Unknown Artist"
            );


        const durHtml =
            escapeHtml(
                item.duration_text || ""
            );


        card.innerHTML = `
            <div class="thumb-wrapper">
                <img
                    src="${thumbUrl}"
                    alt="Cover art for ${titleHtml}"
                    onerror="this.src='https://via.placeholder.com/110x65?text=Music'"
                />
                ${
                    durHtml
                        ? `<span class="badge-duration">
                            ${durHtml}
                        </span>`
                        : ""
                }
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
                data-group-id="${escapeHtml(item.id || "")}"
            ></div>
        `;


        const btnGroup =
            card.querySelector(
                ".btn-group"
            );


        if (isInLibrary) {

            renderLibraryBadge(
                btnGroup
            );

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


            dlBtn.dataset.id =
                item.id || "";


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
                    item.channel,
                    dlBtn
                );


            btnGroup.appendChild(
                prevBtn
            );


            btnGroup.appendChild(
                dlBtn
            );
        }


        results.appendChild(
            card
        );
    });
}


function renderLibraryBadge(container) {

    if (!container) return;


    container.innerHTML = `
        <div class="badge-library">
            ✅ In Library
        </div>
    `;
}


/* ============================================================
   SEARCH
   ============================================================ */

async function searchMusic() {

    const queryInput =
        document.getElementById(
            "query"
        );


    const statusMsg =
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

        if (statusMsg)
            statusMsg.textContent =
                "Type something to search.";

        return;
    }


    currentQuery =
        query;

    currentPage = 1;

    hasMoreResults = true;

    isLoadingMore = false;


    if (statusMsg)
        statusMsg.textContent =
            "🔍 Searching YouTube...";


    if (results)
        results.innerHTML = "";


    if (searchButton)
        searchButton.disabled = true;


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


        if (
            !Array.isArray(data) ||
            data.length === 0
        ) {

            if (statusMsg)
                statusMsg.textContent =
                    "No results found.";


            hasMoreResults = false;

            return;
        }


        if (statusMsg)
            statusMsg.textContent = "";


        renderItems(
            data
        );

    } catch (err) {

        if (statusMsg)
            statusMsg.textContent =
                "❌ " +
                (
                    err.message ||
                    "Search failed."
                );

    } finally {

        if (searchButton)
            searchButton.disabled = false;
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
        loader.style.display =
            "block";


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
            !Array.isArray(data) ||
            data.length === 0
        ) {

            hasMoreResults = false;

        } else {

            renderItems(
                data
            );
        }

    } catch (e) {

        hasMoreResults = false;

    } finally {

        if (loader)
            loader.style.display =
                "none";

        isLoadingMore = false;
    }
}


/* ============================================================
   DOWNLOAD QUEUE HELPERS
   ============================================================ */

function isActiveTask(task) {

    return [
        "queued",
        "downloading",
        "processing"
    ].includes(
        task.status
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
        task.status
    );
}


function getTaskStatusMeta(task) {

    const status =
        String(
            task.status || "queued"
        ).toLowerCase();


    const map = {

        queued: {
            label: "Queued",
            icon: "⏳",
            className: "status-queued"
        },

        downloading: {
            label: "Downloading",
            icon: "⬇️",
            className: "status-downloading"
        },

        processing: {
            label: "Processing",
            icon: "⚙️",
            className: "status-processing"
        },

        completed: {
            label: "Completed",
            icon: "✓",
            className: "status-completed"
        },

        error: {
            label: "Failed",
            icon: "⚠️",
            className: "status-error"
        },

        failed: {
            label: "Failed",
            icon: "⚠️",
            className: "status-error"
        },

        cancelled: {
            label: "Cancelled",
            icon: "✕",
            className: "status-cancelled"
        },

        canceled: {
            label: "Cancelled",
            icon: "✕",
            className: "status-cancelled"
        }
    };


    return (
        map[status] ||
        {
            label:
                status
                    .replace(
                        /[-_]/g,
                        " "
                    )
                    .replace(
                        /^\w/,
                        c =>
                            c.toUpperCase()
                    ),
            icon: "•",
            className: "status-queued"
        }
    );
}


function updateQueueCounters(tasks) {

    const activeCount =
        tasks.filter(
            isActiveTask
        ).length;


    const queueCount =
        document.getElementById(
            "queueCount"
        );


    const downloadQueueTitle =
        document.getElementById(
            "downloadQueueCount"
        );


    const mobileQueueCount =
        document.getElementById(
            "mobQueueCount"
        );


    if (queueCount)
        queueCount.textContent =
            activeCount;


    if (downloadQueueTitle)
        downloadQueueTitle.textContent =
            activeCount;


    if (mobileQueueCount)
        mobileQueueCount.textContent =
            activeCount;
}


function updateSearchDownloadButtons(
    tasks
) {

    const activeIds =
        new Set(
            tasks
                .filter(isActiveTask)
                .map(
                    t =>
                        String(
                            t.elementId || ""
                        )
                )
                .filter(Boolean)
        );


    document
        .querySelectorAll(
            ".btn-download[data-id]"
        )
        .forEach(btn => {

            const id =
                String(
                    btn.dataset.id || ""
                );


            if (!id) return;


            if (activeIds.has(id)) {

                btn.disabled =
                    true;

                btn.classList.add(
                    "is-queued"
                );

                btn.innerHTML =
                    "⏳ Queued";

            } else {

                if (
                    !btn.disabled ||
                    !btn.classList.contains(
                        "is-completed"
                    )
                ) {

                    btn.classList.remove(
                        "is-queued"
                    );
                }
            }
        });


    tasks
        .filter(
            t =>
                [
                    "completed"
                ].includes(
                    t.status
                )
        )
        .forEach(task => {

            if (!task.elementId)
                return;


            const group =
                document.querySelector(
                    `div[data-group-id="${CSS.escape(String(task.elementId))}"]`
                );


            if (group) {

                renderLibraryBadge(
                    group
                );
            }
        });
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
                await response
                    .json()
                    .catch(
                        () => ({})
                    );


            throw new Error(
                error.detail ||
                "Failed to enqueue download."
            );
        }


        showToast(
            `⬇️ Added "${title}" to Downloads`
        );


        /*
         * Downloads is now the single queue screen.
         * Open it immediately after enqueueing.
         */
        navigate(
            "downloads"
        );


        /*
         * Fetch tasks immediately so the new item
         * appears without waiting for WebSocket/polling.
         */
        await pollTasks(
            true
        );


    } catch (e) {

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
                e.message ||
                "Failed to enqueue download."
            )
        );
    }
}


/* ============================================================
   TASK SORTING
   ============================================================ */

function taskTimestamp(task) {

    const value =
        task.last_updated ??
        task.updated_at ??
        task.created_at ??
        task.timestamp ??
        0;


    if (
        typeof value === "number" &&
        value < 10000000000
    ) {

        return value * 1000;
    }


    const parsed =
        Date.parse(value);


    if (!Number.isNaN(parsed))
        return parsed;


    return Number(value) || 0;
}


function sortTasks(tasks) {

    return [...tasks].sort(
        (a, b) => {

            const activeA =
                isActiveTask(a);

            const activeB =
                isActiveTask(b);


            if (
                activeA &&
                !activeB
            )
                return -1;


            if (
                !activeA &&
                activeB
            )
                return 1;


            return (
                taskTimestamp(b) -
                taskTimestamp(a)
            );
        }
    );
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


    const error =
        [
            "error",
            "failed"
        ].includes(
            task.status
        );


    const cancelled =
        [
            "cancelled",
            "canceled"
        ].includes(
            task.status
        );


    const card =
        document.createElement(
            "article"
        );


    card.className =
        "download-card";


    card.dataset.taskId =
        task.id || "";


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
            (
                task.status === "queued"
                    ? "Waiting in queue..."
                    : ""
            )
        );


    const errorMessage =
        escapeHtml(
            task.error ||
            ""
        );


    const speed =
        escapeHtml(
            task.speed ||
            ""
        );


    const statusText =
        error
            ? (
                errorMessage ||
                "Download failed"
            )
            : cancelled
                ? "Download cancelled"
                : completed
                    ? "Saved to your library"
                    : (
                        step ||
                        meta.label
                    );


    const buttonHtml =
        active
            ? `
                <button
                    type="button"
                    class="btn-danger download-cancel-btn"
                    data-task-id="${escapeHtml(String(task.id || ""))}"
                >
                    ✕ Cancel
                </button>
            `
            : `
                <button
                    type="button"
                    class="download-remove-btn"
                    data-task-id="${escapeHtml(String(task.id || ""))}"
                >
                    Remove
                </button>
            `;


    const queueBadge =
        queuePosition !== null
            ? `
                <span class="queue-position">
                    #${queuePosition}
                </span>
            `
            : "";


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
                        class="download-status ${meta.className}"
                    >
                        <span class="status-dot"></span>
                        ${meta.label}
                    </span>

                </div>

            </div>


            <div class="download-progress-row">

                <div class="download-progress-track">

                    <div
                        class="download-progress-fill ${error || cancelled ? "is-error" : ""} ${completed ? "is-complete" : ""}"
                        style="width:${percent}%"
                    ></div>

                </div>

                <span class="download-percent">
                    ${percent}%
                </span>

            </div>


            <div class="download-bottom">

                <div class="download-message">
                    ${statusText}
                </div>

                <div class="download-meta">

                    ${
                        speed
                            ? `<span>${speed}</span>`
                            : ""
                    }

                    ${
                        active && task.status === "queued"
                            ? `<span>Waiting for downloader</span>`
                            : ""
                    }

                    ${
                        completed
                            ? `<span>✓ Ready in Library</span>`
                            : ""
                    }

                </div>

            </div>

        </div>


        <div class="download-actions">

            ${buttonHtml}

        </div>
    `;


    const cancelBtn =
        card.querySelector(
            ".download-cancel-btn"
        );


    if (cancelBtn) {

        cancelBtn.onclick = () =>
            cancelTask(
                task.id
            );
    }


    const removeBtn =
        card.querySelector(
            ".download-remove-btn"
        );


    if (removeBtn) {

        removeBtn.onclick = () =>
            removeDownloadCard(
                task.id
            );
    }


    return card;
}


/* ============================================================
   DOWNLOAD PAGE RENDER
   ============================================================ */

function renderDownloads(
    tasks
) {

    const list =
        document.getElementById(
            "downloadsList"
        );


    if (!list) return;


    const activeTasks =
        sortTasks(
            tasks.filter(
                isActiveTask
            )
        );


    const finishedTasks =
        sortTasks(
            tasks.filter(
                isFinishedTask
            )
        );


    updateQueueCounters(
        tasks
    );


    list.innerHTML = "";


    /*
     * ACTIVE QUEUE
     */

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


    if (activeTasks.length) {

        const activeList =
            document.createElement(
                "div"
            );


        activeList.className =
            "download-stack";


        activeTasks.forEach(
            (task, index) => {

                activeList.appendChild(
                    buildDownloadCard(
                        task,
                        index + 1
                    )
                );
            }
        );


        activeSection.appendChild(
            activeList
        );

    } else {

        const empty =
            document.createElement(
                "div"
            );


        empty.className =
            "downloads-empty";


        empty.innerHTML = `
            <div class="empty-icon">🎧</div>
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


        empty
            .querySelector(
                ".empty-action"
            )
            .onclick = () =>
                navigate(
                    "search"
                );


        activeSection.appendChild(
            empty
        );
    }


    list.appendChild(
        activeSection
    );


    /*
     * RECENT HISTORY
     */

    const recentSection =
        document.createElement(
            "section"
        );


    recentSection.className =
        "downloads-section downloads-history";


    recentSection.innerHTML = `

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


    if (finishedTasks.length) {

        const historyList =
            document.createElement(
                "div"
            );


        historyList.className =
            "download-stack";


        finishedTasks.forEach(
            task => {

                historyList.appendChild(
                    buildDownloadCard(
                        task
                    )
                );
            }
        );


        recentSection.appendChild(
            historyList
        );

    } else {

        const emptyHistory =
            document.createElement(
                "div"
            );


        emptyHistory.className =
            "downloads-history-empty";


        emptyHistory.innerHTML = `
            No completed downloads yet.
        `;


        recentSection.appendChild(
            emptyHistory
        );
    }


    list.appendChild(
        recentSection
    );


    lastRenderedTaskSignature =
        createTaskSignature(
            tasks
        );
}


function createTaskSignature(
    tasks
) {

    return tasks
        .map(
            t =>
                [
                    t.id,
                    t.status,
                    t.percent,
                    t.step,
                    t.error,
                    t.speed,
                    t.last_updated
                ].join(":")
        )
        .sort()
        .join("|");
}


/* ============================================================
   POLL TASKS
   ============================================================ */

async function pollTasks(
    forceRender = false
) {

    try {

        const res =
            await fetch(
                "api/tasks",
                {
                    cache: "no-store"
                }
            );


        if (!res.ok)
            throw new Error(
                "Failed to load tasks"
            );


        const tasks =
            await res.json();


        latestTasks =
            Array.isArray(tasks)
                ? tasks
                : [];


        /*
         * Notify once when a task becomes completed.
         */

        let libraryNeedsUpdate =
            false;


        latestTasks.forEach(
            task => {

                if (
                    task.status ===
                    "completed"
                ) {

                    if (
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
            }
        );


        /*
         * Update library cache only when needed.
         */

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

                await loadLibrary();
            }
        }


        updateQueueCounters(
            latestTasks
        );


        updateSearchDownloadButtons(
            latestTasks
        );


        /*
         * Only render downloads when needed
         * or when task state actually changed.
         */

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
            signature !== lastRenderedTaskSignature
        ) {

            renderDownloads(
                latestTasks
            );
        }

    } catch (e) {

        console.warn(
            "Task polling failed:",
            e
        );
    }
}


function markSearchItemAsLibrary(
    task
) {

    if (!task || !task.elementId)
        return;


    let group = null;


    try {

        group =
            document.querySelector(
                `div[data-group-id="${CSS.escape(String(task.elementId))}"]`
            );

    } catch (e) {

        group =
            document.querySelector(
                `div[data-group-id="${String(task.elementId).replace(/"/g, '\\"')}"]`
            );
    }


    if (group) {

        renderLibraryBadge(
            group
        );
    }
}


/* ============================================================
   DOWNLOAD MANAGEMENT
   ============================================================ */

async function cancelTask(
    taskId
) {

    if (!taskId) return;


    const button =
        document.querySelector(
            `.download-cancel-btn[data-task-id="${CSS.escape(String(taskId))}"]`
        );


    if (button) {

        button.disabled =
            true;

        button.textContent =
            "Cancelling...";
    }


    try {

        const response =
            await fetch(
                `api/tasks/${encodeURIComponent(taskId)}/cancel`,
                {
                    method: "POST"
                }
            );


        if (!response.ok)
            throw new Error(
                "Failed to cancel download"
            );


        showToast(
            "✕ Download cancelled"
        );


        await pollTasks(
            true
        );

    } catch (e) {

        showToast(
            "❌ " +
            (
                e.message ||
                "Failed to cancel download."
            )
        );


        await pollTasks(
            true
        );
    }
}


async function removeDownloadCard(
    taskId
) {

    if (!taskId) return;


    /*
     * Your current backend already exposes
     * clear-completed, but not a generic per-task
     * delete endpoint in the code you gave me.
     *
     * Until there is one, we remove only the visible
     * card and immediately re-sync.
     */

    const card =
        document.querySelector(
            `.download-card[data-task-id="${CSS.escape(String(taskId))}"]`
        );


    if (card) {

        card.style.opacity = "0";
        card.style.transform =
            "translateY(6px)";

        setTimeout(
            () => card.remove(),
            180
        );
    }
}


async function clearDoneTasks() {

    try {

        const response =
            await fetch(
                "api/tasks/clear-completed",
                {
                    method: "DELETE"
                }
            );


        if (!response.ok)
            throw new Error(
                "Failed to clear finished downloads."
            );


        showToast(
            "🧹 Completed downloads cleared"
        );


        completedSet.clear();


        await pollTasks(
            true
        );

    } catch (e) {

        showToast(
            "❌ " +
            (
                e.message ||
                "Failed to clear finished downloads."
            )
        );
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


    if (!list) return;


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

        const stats =
            await fetch(
                "api/stats",
                {
                    cache: "no-store"
                }
            )
            .then(
                r => r.json()
            );


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


        if (tracks)
            tracks.textContent =
                stats.tracks || 0;


        if (artists)
            artists.textContent =
                stats.artists || 0;


        if (albums)
            albums.textContent =
                stats.albums || 0;

    } catch (e) {}
}


/* ============================================================
   LIBRARY
   ============================================================ */

async function loadLibrary() {

    const list =
        document.getElementById(
            "libraryList"
        );


    if (!list) return;


    list.innerHTML =
        `
        <div class="library-loading">
            <div class="loader-spinner"></div>
            Loading library...
        </div>
        `;


    try {

        await refreshLibraryCache();

        await loadStats();

        filterLibrary();

    } catch (e) {

        list.innerHTML =
            `
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


    const searchInput =
        document.getElementById(
            "libSearchQuery"
        );


    if (!list) return;


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
                String(
                    f.name || ""
                )
                .toLowerCase()
                .includes(q)
        );


    if (
        filtered.length === 0
    ) {

        list.innerHTML =
            `
            <div class="downloads-empty library-empty">
                <div class="empty-icon">🎵</div>

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
        f => {

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
                `data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="110" height="110" viewBox="0 0 110 110"><rect width="100%" height="100%" fill="%231e293b"/><text x="50%" y="50%" fill="%239ca3af" font-size="24" text-anchor="middle" dominant-baseline="central">🎵</text></svg>`;


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
                        📦 ${escapeHtml(f.size || "Unknown size")}
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
    )
        return;


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


        if (!response.ok)
            throw new Error(
                "Delete failed"
            );


        showToast(
            "🗑 Track deleted"
        );


        await refreshLibraryCache();

        await loadStats();

        filterLibrary();


    } catch (e) {

        showToast(
            "❌ Failed to delete file."
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
        e => {

            if (e.key === "Enter") {

                e.preventDefault();

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
            !searchTab ||
            !searchTab.classList.contains(
                "active"
            )
        )
            return;


        if (
            window.innerHeight +
            window.scrollY >=
            document.body.offsetHeight -
            500
        ) {

            loadMoreResults();
        }
    },
    { passive: true }
);


/* ============================================================
   STARTUP
   ============================================================ */

async function initializeApp() {

    await refreshLibraryCache();

    await pollTasks(
        true
    );

    handleDeepLink();

    initWebSocket();

    /*
     * Fallback polling.
     * WebSocket remains the fast update mechanism.
     */
    startTaskPolling();
}


document.addEventListener(
    "DOMContentLoaded",
    initializeApp
);
