document.addEventListener("DOMContentLoaded", () => {
    const wsScheme = window.location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${wsScheme}://${window.location.host}/ws`);

    const searchInput = document.getElementById("search-input");
    const searchBtn = document.getElementById("search-btn");
    const resultsContainer = document.getElementById("results-container");
    const tasksContainer = document.getElementById("tasks-container");
    const audioPlayer = document.getElementById("audio-player");

    let currentAudioId = null;

    // Sanitization Utility
    function escapeHtml(str) {
        if (!str) return "";
        return String(str)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    // WebSocket Handling
    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === "task_update") {
            renderOrUpdateTask(data.task);
        } else if (data.type === "task_progress") {
            updateTaskProgress(data.id, data.progress);
        }
    };

    // Search Actions
    searchBtn.addEventListener("click", performSearch);
    searchInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") performSearch();
    });

    async function performSearch() {
        const query = searchInput.value.trim();
        if (!query) return;

        resultsContainer.innerHTML = '<div class="loading">Searching YouTube...</div>';

        try {
            const res = await fetch(`/api/search?q=${encodeURIComponent(query)}`);
            const data = await res.json();
            renderItems(data.results);
        } catch (err) {
            resultsContainer.innerHTML = '<div class="error">Failed to perform search.</div>';
        }
    }

    function renderItems(items) {
        if (!items || items.length === 0) {
            resultsContainer.innerHTML = '<div class="empty">No results found.</div>';
            return;
        }

        resultsContainer.innerHTML = items.map(item => `
            <div class="result-card" data-id="${escapeHtml(item.id)}">
                <div class="result-info">
                    <h4>${escapeHtml(item.title)}</h4>
                    <p>${escapeHtml(item.artist)}</p>
                </div>
                <div class="result-actions">
                    <button class="btn-preview" onclick="toggleAudioStream('${escapeHtml(item.id)}')">Preview</button>
                    <button class="btn-download" onclick="triggerDownload('${escapeHtml(item.id)}', '${escapeHtml(item.title)}', '${escapeHtml(item.artist)}')">Download</button>
                </div>
            </div>
        `).join('');
    }

    // Safe Playback Handler
    window.toggleAudioStream = async function(videoId, isRetry = false) {
        if (currentAudioId === videoId && !audioPlayer.paused && !isRetry) {
            audioPlayer.pause();
            return;
        }

        currentAudioId = videoId;

        try {
            if (isRetry) {
                // Direct fallback to live transcoding pipeline
                audioPlayer.src = `/api/preview?v=${encodeURIComponent(videoId)}&transcode=true`;
                await audioPlayer.play();
            } else {
                const res = await fetch(`/api/preview?v=${encodeURIComponent(videoId)}`);
                if (!res.ok) throw new Error("Manifest URL resolution failed");
                
                const data = await res.json();
                audioPlayer.src = data.url;
                
                await audioPlayer.play().catch(async (err) => {
                    console.warn("Direct stream playback failed. Falling back to transcoding...", err);
                    // Single retry attempt through direct parameter flag
                    await toggleAudioStream(videoId, true);
                });
            }
        } catch (err) {
            console.error("Playback error:", err);
            alert("Unable to play preview audio stream.");
        }
    };

    // Download Queueing
    window.triggerDownload = async function(videoId, title, artist) {
        try {
            const res = await fetch('/api/download', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    url: `https://www.youtube.com/watch?v=${videoId}`,
                    title: title,
                    artist: artist
                })
            });
            if (!res.ok) throw new Error("Failed to queue download");
        } catch (err) {
            alert(err.message);
        }
    };

    // Task List Handling
    function renderOrUpdateTask(task) {
        let taskEl = document.getElementById(`task-${task.id}`);
        if (!taskEl) {
            taskEl = document.createElement("div");
            taskEl.id = `task-${task.id}`;
            taskEl.className = "task-card";
            tasksContainer.prepend(taskEl);
        }

        taskEl.innerHTML = `
            <div class="task-details">
                <span class="task-title">${escapeHtml(task.title)}</span>
                <span class="task-status status-${escapeHtml(task.status)}">${escapeHtml(task.status)}</span>
            </div>
            <div class="progress-bar-container">
                <div class="progress-bar" id="progress-${task.id}" style="width: ${task.progress || 0}%"></div>
            </div>
        `;
    }

    function updateTaskProgress(id, progress) {
        const bar = document.getElementById(`progress-${id}`);
        if (bar) {
            bar.style.width = `${progress}%`;
        }
    }

    // Initial Load
    async function loadTasks() {
        try {
            const res = await fetch('/api/tasks');
            const data = await res.json();
            data.tasks.forEach(renderOrUpdateTask);
        } catch (err) {
            console.error("Failed to load tasks", err);
        }
    }

    loadTasks();
});
