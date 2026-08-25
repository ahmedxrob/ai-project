/* ============================================================
   XROB MUSIC — AMPERFY / SUBSONIC HELPER (FIXED)
   ============================================================ */

(function () {
    "use strict";

    const STORAGE_KEY = "xrob_amperfy_settings";

    const defaultSettings = {
        host: window.location.origin,
        username: "admin",
        password: "",
        serverName: "Xrob Music"
    };

    // ========================================================
    // STORAGE
    // ========================================================

    function loadSettings() {
        try {
            const raw = localStorage.getItem(STORAGE_KEY);
            if (!raw) {
                return { ...defaultSettings };
            }
            return {
                ...defaultSettings,
                ...JSON.parse(raw)
            };
        } catch (error) {
            console.warn("Failed to load Amperfy settings:", error);
            return { ...defaultSettings };
        }
    }

    function saveSettings(settings) {
        try {
            localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
            return true;
        } catch (error) {
            console.warn("Failed to save Amperfy settings:", error);
            return false;
        }
    }

    // ========================================================
    // SERVER URL
    // ========================================================

    function getServerUrl() {
        const settings = loadSettings();
        let host = (settings.host || window.location.origin).trim();
        return host.replace(/\/+$/, "");
    }

    function getRestUrl() {
        return `${getServerUrl()}/rest`;
    }

    // ========================================================
    // AMPERFY CONNECTION INFORMATION
    // ========================================================

    function getConnectionInfo() {
        const settings = loadSettings();
        return {
            server: getServerUrl(),
            rest: getRestUrl(),
            username: settings.username || "admin",
            serverName: settings.serverName || "Xrob Music"
        };
    }

    // ========================================================
    // TEST SUBSONIC CONNECTION
    // ========================================================

    async function testConnection() {
        const settings = loadSettings();

        try {
            const response = await fetch(`${getServerUrl()}/api/amperfy/status`, {
                method: "GET",
                cache: "no-store",
                headers: { "Accept": "application/json" }
            });

            if (!response.ok) {
                throw new Error(`Server returned HTTP ${response.status}`);
            }

            const data = await response.json();

            if (!data.subsonic) {
                throw new Error("Subsonic API is disabled on backend.");
            }

            return data;
        } catch (err) {
            console.error("Amperfy connection test failed:", err);
            throw err;
        }
    }

    // ========================================================
    // COPY SERVER URL
    // ========================================================

    async function copyServerUrl() {
        const url = getServerUrl();
        if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(url);
            return url;
        }

        const textarea = document.createElement("textarea");
        textarea.value = url;
        textarea.style.position = "fixed";
        textarea.style.opacity = "0";
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand("copy");
        textarea.remove();
        return url;
    }

    // ========================================================
    // CONFIGURE
    // ========================================================

    function configure(options = {}) {
        const current = loadSettings();
        const updated = { ...current, ...options };
        saveSettings(updated);
        return updated;
    }

    // ========================================================
    // AMPERFY SETUP INFO
    // ========================================================

    function getSetupInfo() {
        const info = getConnectionInfo();
        return {
            serverUrl: info.server,
            apiUrl: info.rest,
            username: info.username,
            instructions: [
                "Open Amperfy on iOS.",
                "Add a new Subsonic Server.",
                `Server Address: ${info.server}`,
                `Username: ${info.username}`,
                "Password: (Your configured Subsonic password)",
                "Ensure server port and protocol (HTTP/HTTPS) match."
            ]
        };
    }

    // ========================================================
    // SERVER DISCOVERY / STATUS
    // ========================================================

    async function getServerStatus() {
        try {
            const response = await fetch(`${getServerUrl()}/api/amperfy/status`, {
                cache: "no-store",
                headers: { "Accept": "application/json" }
            });

            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }

            return await response.json();
        } catch (err) {
            return {
                status: "error",
                server: "Xrob Music",
                subsonic: false,
                music_files: 0,
                error: err.message
            };
        }
    }

    function openServer() {
        window.open(getServerUrl(), "_blank", "noopener,noreferrer");
    }

    // ========================================================
    // PUBLIC API
    // ========================================================

    window.XrobAmperfy = {
        loadSettings,
        saveSettings,
        configure,
        getServerUrl,
        getRestUrl,
        getConnectionInfo,
        getSetupInfo,
        getServerStatus,
        testConnection,
        copyServerUrl,
        openServer
    };

    console.info("Xrob Amperfy/Subsonic helper loaded.");
})();
