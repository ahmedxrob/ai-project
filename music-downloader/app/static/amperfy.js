/* ============================================================
XROB MUSIC — AMPERFY / SUBSONIC CONNECTION HELPER
Direct connection to Xrob Music — NO NAVIDROME REQUIRED
============================================================ */

(function () {
"use strict";

```
const STORAGE_KEY = "xrob_amperfy_settings";

const defaultSettings = {
    host: window.location.origin,
    username: "admin",
    password: "",
    serverName: "Xrob Music"
};

// ============================================================
// STORAGE
// ============================================================

function loadSettings() {
    try {
        const raw = localStorage.getItem(STORAGE_KEY);

        if (!raw) {
            return { ...defaultSettings };
        }

        const parsed = JSON.parse(raw);

        return {
            ...defaultSettings,
            ...(parsed || {})
        };
    } catch (error) {
        console.warn("Xrob Amperfy: failed to load settings:", error);
        return { ...defaultSettings };
    }
}

function saveSettings(settings) {
    try {
        const merged = {
            ...defaultSettings,
            ...(settings || {})
        };

        localStorage.setItem(
            STORAGE_KEY,
            JSON.stringify(merged)
        );

        return true;
    } catch (error) {
        console.warn("Xrob Amperfy: failed to save settings:", error);
        return false;
    }
}

// ============================================================
// SERVER URL
// ============================================================

function normalizeHost(host) {
    host = String(host || "").trim();

    if (!host) {
        host = window.location.origin;
    }

    /*
     * Remove trailing slashes.
     */
    host = host.replace(/\/+$/, "");

    return host;
}

function getServerUrl() {
    const settings = loadSettings();
    return normalizeHost(settings.host);
}

/*
 * Xrob Music exposes its Subsonic-compatible API here:
 *
 *     /api/subsonic
 *
 * This is the URL that should be used by Amperfy.
 */
function getSubsonicUrl() {
    return `${getServerUrl()}/api/subsonic`;
}

/*
 * Kept for compatibility with older frontend code.
 */
function getRestUrl() {
    return getSubsonicUrl();
}

// ============================================================
// CONNECTION INFORMATION
// ============================================================

function getConnectionInfo() {
    const settings = loadSettings();

    return {
        server: getServerUrl(),

        /*
         * Direct Xrob Music Subsonic endpoint.
         */
        subsonic: getSubsonicUrl(),

        /*
         * Compatibility aliases.
         */
        rest: getSubsonicUrl(),
        api: getSubsonicUrl(),

        username: settings.username || "admin",
        password: settings.password || "",
        serverName: settings.serverName || "Xrob Music"
    };
}

// ============================================================
// SUBSONIC API URL BUILDER
// ============================================================

function buildSubsonicUrl(endpoint, params = {}) {
    const base = getSubsonicUrl().replace(/\/+$/, "");
    const cleanEndpoint = String(endpoint || "")
        .replace(/^\/+/, "");

    const url = `${base}/${cleanEndpoint}`;

    const query = new URLSearchParams();

    Object.entries(params || {}).forEach(([key, value]) => {
        if (
            value !== undefined &&
            value !== null &&
            value !== ""
        ) {
            query.set(key, value);
        }
    });

    return query.toString()
        ? `${url}?${query.toString()}`
        : url;
}

// ============================================================
// TEST DIRECT SUBSONIC CONNECTION
// ============================================================

async function testConnection() {
    const settings = loadSettings();

    const username = settings.username || "admin";
    const password = settings.password || "";

    /*
     * The backend status endpoint is useful for checking that
     * the Xrob Music Subsonic service is enabled.
     */
    try {
        const statusResponse = await fetch(
            `${getServerUrl()}/api/amperfy/status`,
            {
                method: "GET",
                cache: "no-store",
                headers: {
                    "Accept": "application/json"
                }
            }
        );

        if (!statusResponse.ok) {
            throw new Error(
                `Xrob Music returned HTTP ${statusResponse.status}`
            );
        }

        const status = await statusResponse.json();

        if (status.subsonic === false) {
            throw new Error(
                "Xrob Music Subsonic API is disabled."
            );
        }

        /*
         * Also test the actual Subsonic endpoint.
         *
         * We use ping, which is supported by the Subsonic API.
         */
        const params = {
            u: username,
            v: "1.16.1",
            c: "XrobMusic",
            f: "json"
        };

        /*
         * Password is intentionally not sent directly here.
         *
         * The backend may support token/salt authentication.
         * The actual Amperfy client will perform authentication.
         */
        if (password) {
            params.p = password;
        }

        const pingUrl = buildSubsonicUrl("ping.view", params);

        const pingResponse = await fetch(pingUrl, {
            method: "GET",
            cache: "no-store",
            headers: {
                "Accept": "application/json"
            }
        });

        if (!pingResponse.ok) {
            throw new Error(
                `Subsonic API returned HTTP ${pingResponse.status}`
            );
        }

        const pingData = await pingResponse.json();

        /*
         * Subsonic responses normally contain:
         *
         * {
         *   "subsonic-response": {
         *      "status": "ok"
         *   }
         * }
         */
        const subsonicResponse =
            pingData?.["subsonic-response"];

        if (
            subsonicResponse &&
            subsonicResponse.status &&
            subsonicResponse.status !== "ok"
        ) {
            const message =
                subsonicResponse.error?.message ||
                "Subsonic authentication/API error.";

            throw new Error(message);
        }

        return {
            status: "ok",
            subsonic: true,
            server: status.server || "Xrob Music",
            endpoint: getSubsonicUrl(),
            username: username,
            response: pingData
        };

    } catch (error) {
        console.error(
            "Xrob Amperfy direct connection failed:",
            error
        );

        throw error;
    }
}

// ============================================================
// GET SERVER STATUS
// ============================================================

async function getServerStatus() {
    try {
        const response = await fetch(
            `${getServerUrl()}/api/amperfy/status`,
            {
                method: "GET",
                cache: "no-store",
                headers: {
                    "Accept": "application/json"
                }
            }
        );

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        const data = await response.json();

        return {
            status: data.status || "ok",
            server: data.server || "Xrob Music",
            subsonic: data.subsonic !== false,
            music_files: Number(data.music_files || 0),
            endpoint: getSubsonicUrl(),
            error: data.error || null
        };

    } catch (error) {
        return {
            status: "error",
            server: "Xrob Music",
            subsonic: false,
            music_files: 0,
            endpoint: getSubsonicUrl(),
            error: error.message
        };
    }
}

// ============================================================
// SETUP INFORMATION FOR AMPERFY
// ============================================================

function getSetupInfo() {
    const info = getConnectionInfo();

    return {
        serverUrl: info.server,

        /*
         * IMPORTANT:
         *
         * Amperfy should connect directly to:
         *
         *     http://YOUR-IP:PORT/api/subsonic
         *
         * depending on how Amperfy handles the Subsonic
         * server base URL.
         */
        apiUrl: info.subsonic,

        subsonicUrl: info.subsonic,

        username: info.username,

        serverName: info.serverName,

        instructions: [
            "Open Amperfy on iOS.",
            "Add a new Subsonic server.",
            `Server Address: ${info.subsonic}`,
            `Username: ${info.username}`,
            "Password: Your Xrob Music password.",
            "Save the server and test the connection."
        ]
    };
}

// ============================================================
// COPY SUBSONIC URL
// ============================================================

async function copyServerUrl() {
    const url = getSubsonicUrl();

    if (
        navigator.clipboard &&
        typeof navigator.clipboard.writeText === "function"
    ) {
        await navigator.clipboard.writeText(url);
        return url;
    }

    const textarea = document.createElement("textarea");

    textarea.value = url;
    textarea.style.position = "fixed";
    textarea.style.left = "-9999px";
    textarea.style.top = "0";
    textarea.style.opacity = "0";

    document.body.appendChild(textarea);

    textarea.focus();
    textarea.select();

    try {
        document.execCommand("copy");
    } catch (error) {
        console.warn(
            "Xrob Amperfy: clipboard fallback failed:",
            error
        );
    }

    textarea.remove();

    return url;
}

// ============================================================
// COPY ROOT SERVER URL
// ============================================================

async function copyRootServerUrl() {
    const url = getServerUrl();

    if (
        navigator.clipboard &&
        typeof navigator.clipboard.writeText === "function"
    ) {
        await navigator.clipboard.writeText(url);
        return url;
    }

    const textarea = document.createElement("textarea");

    textarea.value = url;
    textarea.style.position = "fixed";
    textarea.style.left = "-9999px";
    textarea.style.top = "0";
    textarea.style.opacity = "0";

    document.body.appendChild(textarea);

    textarea.focus();
    textarea.select();

    try {
        document.execCommand("copy");
    } catch (error) {
        console.warn(
            "Xrob Amperfy: clipboard fallback failed:",
            error
        );
    }

    textarea.remove();

    return url;
}

// ============================================================
// OPEN SERVER
// ============================================================

function openServer() {
    window.open(
        getServerUrl(),
        "_blank",
        "noopener,noreferrer"
    );
}

function openSubsonicApi() {
    window.open(
        getSubsonicUrl(),
        "_blank",
        "noopener,noreferrer"
    );
}

// ============================================================
// CONFIGURE
// ============================================================

function configure(options = {}) {
    const current = loadSettings();

    const updated = {
        ...current,
        ...(options || {})
    };

    saveSettings(updated);

    return updated;
}

// ============================================================
// RESET
// ============================================================

function resetSettings() {
    try {
        localStorage.removeItem(STORAGE_KEY);
    } catch (error) {
        console.warn(
            "Xrob Amperfy: failed to reset settings:",
            error
        );
    }

    return {
        ...defaultSettings
    };
}

// ============================================================
// AUTHENTICATION HELPERS
// ============================================================

function getUsername() {
    return loadSettings().username || "admin";
}

function getPassword() {
    return loadSettings().password || "";
}

function setCredentials(username, password) {
    return configure({
        username: username || "admin",
        password: password || ""
    });
}

// ============================================================
// PUBLIC API
// ============================================================

window.XrobAmperfy = {

    // Settings
    loadSettings,
    saveSettings,
    configure,
    resetSettings,

    // Server
    getServerUrl,
    getSubsonicUrl,
    getRestUrl,

    // Connection
    getConnectionInfo,
    getServerStatus,
    testConnection,

    // Subsonic
    buildSubsonicUrl,

    // Credentials
    getUsername,
    getPassword,
    setCredentials,

    // Amperfy setup
    getSetupInfo,

    // Clipboard
    copyServerUrl,
    copyRootServerUrl,

    // Browser
    openServer,
    openSubsonicApi
};

console.info(
    "Xrob Music — Direct Amperfy/Subsonic helper loaded."
);
```

})();
