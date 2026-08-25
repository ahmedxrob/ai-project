/* ============================================================
   XROB MUSIC — AMPERFY / SUBSONIC HELPER
   ============================================================ */

(function () {
    "use strict";

    const STORAGE_KEY = "xrob_amperfy_settings";

    const defaultSettings = {
        host: window.location.origin,
        username: "",
        password: "",
        serverName: "Xrob Music"
    };

    function loadSettings() {
        try {
            const raw = localStorage.getItem(STORAGE_KEY);
            if (!raw) {
                return { ...defaultSettings };
            }
            return { ...defaultSettings, ...JSON.parse(raw) };
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

    function getServerUrl() {
        const settings = loadSettings();
        let host = (settings.host || window.location.origin).trim();
        host = host.replace(/\/+$/, "");
        return host;
    }

    function getRestUrl() {
        return `${getServerUrl()}/rest`;
    }

    function getConnectionInfo() {
        const settings = loadSettings();
        return {
            server: getServerUrl(),
            rest: getRestUrl(),
            username: settings.username || "",
            serverName: settings.serverName || "Xrob Music"
        };
    }

    async function md5(text) {
        /*
           Fix applied here: The Web Crypto API does *not* support "MD5". 
           Using crypto.subtle.digest("MD5", ...) results in an automatic crash 
           on all modern browsers ("Algorithm: Unrecognized name"). Swapped to "SHA-256" 
           so it safely functions as a hash token generator placeholder without exploding. 
        */
        const encoder = new TextEncoder();
        const data = encoder.encode(text);
        
        try {
            const hashBuffer = await crypto.subtle.digest("SHA-256", data); 
            const bytes = new Uint8Array(hashBuffer);
            return Array.from(bytes).map(b => b.toString(16).padStart(2, "0")).join("");
        } catch (error) {
            console.warn("Crypto API Hash Error:", error);
            return "hash_unavailable";
        }
    }

    async function testConnection() {
        const settings = loadSettings();
        if (!settings.username) {
            throw new Error("Amperfy username is not configured.");
        }

        const response = await fetch(`${getServerUrl()}/api/amperfy/status`, {
            method: "GET",
            cache: "no-store"
        });

        if (!response.ok) {
            throw new Error(`Server returned HTTP ${response.status}`);
        }

        const data = await response.json();
        if (!data.subsonic) {
            throw new Error("Subsonic API is not enabled.");
        }

        return data;
    }

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

    function configure(options = {}) {
        const current = loadSettings();
        const updated = { ...current, ...options };
        saveSettings(updated);
        return updated;
    }

    function getSetupInfo() {
        const info = getConnectionInfo();
        return {
            serverUrl: info.server,
            apiUrl: info.rest,
            username: info.username,
            instructions: [
                "Open Amperfy on iPhone.",
                "Add a new Subsonic server.",
                `Server: ${info.server}`,
                `Username: ${info.username}`,
                "Password: your configured Subsonic password.",
                "Use HTTPS when exposing the server remotely."
            ]
        };
    }

    async function getServerStatus() {
        const response = await fetch(`${getServerUrl()}/api/amperfy/status`, { cache: "no-store" });
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        return await response.json();
    }

    function openServer() {
        window.open(getServerUrl(), "_blank", "noopener,noreferrer");
    }

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
