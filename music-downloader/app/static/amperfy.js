/* ============================================================
   XROB MUSIC — DIRECT AMPERFY / SUBSONIC CLIENT HELPER
   ============================================================ */

(function () {
    "use strict";

    const STORAGE_KEY = "xrob_amperfy_settings";

    const DEFAULTS = {
        host: window.location.origin,
        username: "admin",
        password: "",
        serverName: "Xrob Music"
    };

    // =========================================================
    // HELPERS
    // =========================================================

    function normalizeHost(host) {
        host = String(host || "").trim();

        if (!host) {
            host = window.location.origin;
        }

        return host.replace(/\/+$/, "");
    }

    function loadSettings() {
        try {
            const raw = localStorage.getItem(STORAGE_KEY);

            if (!raw) {
                return { ...DEFAULTS };
            }

            const parsed = JSON.parse(raw);

            return {
                ...DEFAULTS,
                ...(parsed || {})
            };
        } catch (error) {
            console.warn(
                "[Xrob Amperfy] Failed to load settings:",
                error
            );

            return { ...DEFAULTS };
        }
    }

    function saveSettings(settings) {
        try {
            const clean = {
                ...DEFAULTS,
                ...(settings || {})
            };

            clean.host = normalizeHost(clean.host);

            localStorage.setItem(
                STORAGE_KEY,
                JSON.stringify(clean)
            );

            return clean;
        } catch (error) {
            console.warn(
                "[Xrob Amperfy] Failed to save settings:",
                error
            );

            return settings;
        }
    }

    function getServerUrl() {
        const settings = loadSettings();

        return normalizeHost(
            settings.host || window.location.origin
        );
    }

    // =========================================================
    // SUBSONIC API
    // =========================================================

    /*
     * IMPORTANT:
     *
     * Xrob Music exposes Subsonic through:
     *
     *     /api/subsonic
     *
     * Therefore Amperfy must NOT use:
     *
     *     /rest
     *
     * or Navidrome.
     */

    function getSubsonicUrl() {
        return `${getServerUrl()}/api/subsonic`;
    }

    function getPingUrl() {
        const settings = loadSettings();

        const params = new URLSearchParams({
            u: settings.username || "admin",
            p: settings.password || "",
            v: "1.16.1",
            c: "Amperfy",
            f: "json"
        });

        return `${getSubsonicUrl()}/ping.view?${params.toString()}`;
    }

    // =========================================================
    // CONNECTION INFO
    // =========================================================

    function getConnectionInfo() {
        const settings = loadSettings();

        return {
            server: getServerUrl(),

            /*
             * This is the address Amperfy should use.
             */
            subsonic: getSubsonicUrl(),

            username: settings.username || "admin",

            serverName:
                settings.serverName || "Xrob Music"
        };
    }

    // =========================================================
    // TEST CONNECTION
    // =========================================================

    async function testConnection() {
        const settings = loadSettings();

        const username =
            settings.username || "admin";

        const password =
            settings.password || "";

        const params = new URLSearchParams({
            u: username,
            p: password,
            v: "1.16.1",
            c: "Amperfy",
            f: "json"
        });

        const url =
            `${getSubsonicUrl()}/ping.view?${params.toString()}`;

        try {
            const response = await fetch(url, {
                method: "GET",
                cache: "no-store",
                headers: {
                    "Accept": "application/json"
                }
            });

            if (!response.ok) {
                throw new Error(
                    `HTTP ${response.status}`
                );
            }

            const data = await response.json();

            /*
             * Subsonic normally returns:
             *
             * {
             *   "subsonic-response": {
             *      "status": "ok",
             *      ...
             *   }
             * }
             */

            const root =
                data["subsonic-response"] ||
                data.subsonicResponse ||
                data;

            if (
                root.status &&
                String(root.status).toLowerCase() !== "ok"
            ) {
                const errorMessage =
                    root.error?.message ||
                    root.error?.[0]?.message ||
                    "Subsonic server returned an error.";

                throw new Error(errorMessage);
            }

            return {
                ok: true,
                data,
                server: getServerUrl(),
                api: getSubsonicUrl()
            };
        } catch (error) {
            console.error(
                "[Xrob Amperfy] Connection test failed:",
                error
            );

            throw error;
        }
    }

    // =========================================================
    // SERVER STATUS
    // =========================================================

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
                throw new Error(
                    `HTTP ${response.status}`
                );
            }

            return await response.json();
        } catch (error) {
            return {
                status: "error",
                server: "Xrob Music",
                subsonic: false,
                music_files: 0,
                error: error.message
            };
        }
    }

    // =========================================================
    // CONFIGURE
    // =========================================================

    function configure(options = {}) {
        const current = loadSettings();

        const updated = {
            ...current,
            ...options
        };

        return saveSettings(updated);
    }

    // =========================================================
    // SETUP INFORMATION
    // =========================================================

    function getSetupInfo() {
        const info = getConnectionInfo();

        return {
            serverUrl: info.server,

            /*
             * Direct Subsonic endpoint.
             */
            apiUrl: info.subsonic,

            username: info.username,

            instructions: [
                "Open Amperfy on iOS.",
                "Add a new Subsonic server.",
                `Server URL: ${info.subsonic}`,
                `Username: ${info.username}`,
                "Password: Your Xrob Music Subsonic password.",
                "Do not use Navidrome.",
                "Do not use /rest."
            ]
        };
    }

    // =========================================================
    // COPY
    // =========================================================

    async function copyText(text) {
        if (
            navigator.clipboard &&
            navigator.clipboard.writeText
        ) {
            await navigator.clipboard.writeText(text);
            return text;
        }

        const textarea =
            document.createElement("textarea");

        textarea.value = text;

        textarea.style.position = "fixed";
        textarea.style.left = "-9999px";

        document.body.appendChild(textarea);

        textarea.focus();
        textarea.select();

        document.execCommand("copy");

        textarea.remove();

        return text;
    }

    async function copyServerUrl() {
        return copyText(getServerUrl());
    }

    async function copySubsonicUrl() {
        return copyText(getSubsonicUrl());
    }

    // =========================================================
    // OPEN SERVER
    // =========================================================

    function openServer() {
        window.open(
            getServerUrl(),
            "_blank",
            "noopener,noreferrer"
        );
    }

    // =========================================================
    // UI HELPERS
    // =========================================================

    function showMessage(
        message,
        type = "info"
    ) {
        const element =
            document.getElementById("amperfyStatus");

        if (!element) {
            return;
        }

        element.textContent = message;

        element.className =
            `amperfy-status ${type}`;
    }

    async function testConnectionUI() {
        const button =
            document.getElementById(
                "amperfyTestBtn"
            );

        if (button) {
            button.disabled = true;
            button.textContent = "⏳ Testing...";
        }

        showMessage(
            "Connecting to Xrob Music Subsonic API...",
            "loading"
        );

        try {
            const result =
                await testConnection();

            showMessage(
                "✓ Amperfy / Subsonic connection successful.",
                "success"
            );

            return result;
        } catch (error) {
            showMessage(
                `✕ Connection failed: ${error.message}`,
                "error"
            );

            throw error;
        } finally {
            if (button) {
                button.disabled = false;
                button.textContent = "🔌 Test Connection";
            }
        }
    }

    function loadUI() {
        const settings = loadSettings();

        const host =
            document.getElementById("set_amperfy_host");

        const user =
            document.getElementById("set_amperfy_user");

        const password =
            document.getElementById("set_amperfy_password");

        const serverName =
            document.getElementById("set_amperfy_server_name");

        const api =
            document.getElementById("amperfyApiUrl");

        if (host) {
            host.value =
                settings.host || window.location.origin;
        }

        if (user) {
            user.value =
                settings.username || "admin";
        }

        if (password) {
            password.value =
                settings.password || "";
        }

        if (serverName) {
            serverName.value =
                settings.serverName || "Xrob Music";
        }

        if (api) {
            api.value = getSubsonicUrl();
        }
    }

    function saveUI() {
        const host =
            document.getElementById("set_amperfy_host");

        const user =
            document.getElementById("set_amperfy_user");

        const password =
            document.getElementById("set_amperfy_password");

        const serverName =
            document.getElementById(
                "set_amperfy_server_name"
            );

        const settings = {
            host:
                host?.value ||
                window.location.origin,

            username:
                user?.value ||
                "admin",

            password:
                password?.value ||
                "",

            serverName:
                serverName?.value ||
                "Xrob Music"
        };

        saveSettings(settings);

        const api =
            document.getElementById("amperfyApiUrl");

        if (api) {
            api.value = getSubsonicUrl();
        }

        showMessage(
            "✓ Amperfy settings saved.",
            "success"
        );

        return settings;
    }

    // =========================================================
    // AUTO INITIALIZATION
    // =========================================================

    document.addEventListener(
        "DOMContentLoaded",
        function () {
            loadUI();

            const host =
                document.getElementById(
                    "set_amperfy_host"
                );

            if (host) {
                host.addEventListener(
                    "input",
                    function () {
                        const api =
                            document.getElementById(
                                "amperfyApiUrl"
                            );

                        if (api) {
                            api.value =
                                `${normalizeHost(
                                    host.value
                                )}/api/subsonic`;
                        }
                    }
                );
            }
        }
    );

    // =========================================================
    // PUBLIC API
    // =========================================================

    window.XrobAmperfy = {
        loadSettings,
        saveSettings,
        configure,

        getServerUrl,
        getSubsonicUrl,
        getRestUrl: getSubsonicUrl,

        getConnectionInfo,
        getSetupInfo,

        getServerStatus,

        testConnection,
        testConnectionUI,

        copyServerUrl,
        copySubsonicUrl,

        openServer,

        loadUI,
        saveUI,

        showMessage
    };

    console.info(
        "[Xrob Music] Direct Amperfy/Subsonic helper loaded."
    );

})();
