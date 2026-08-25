/* ============================================================
   XROB MUSIC — AMPERFY / SUBSONIC HELPER
   ============================================================

   IMPORTANT:

   Amperfy iOS does NOT load this JavaScript.

   Amperfy communicates directly with:

       http://YOUR-SERVER:8099/rest/...

   This file is only for the Xrob Music web interface.

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
            console.warn(
                "Failed to load Amperfy settings:",
                error
            );

            return { ...defaultSettings };
        }
    }


    function saveSettings(settings) {
        try {
            localStorage.setItem(
                STORAGE_KEY,
                JSON.stringify(settings)
            );

            return true;
        } catch (error) {
            console.warn(
                "Failed to save Amperfy settings:",
                error
            );

            return false;
        }
    }


    // ========================================================
    // SERVER URL
    // ========================================================

    function getServerUrl() {
        const settings = loadSettings();

        let host = (
            settings.host ||
            window.location.origin
        ).trim();

        host = host.replace(
            /\/+$/,
            ""
        );

        return host;
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
            username: settings.username || "",
            serverName:
                settings.serverName ||
                "Xrob Music"
        };
    }


    // ========================================================
    // SUBSONIC TOKEN
    // ========================================================

    async function md5(text) {
        const encoder = new TextEncoder();

        const data = encoder.encode(text);

        const hashBuffer =
            await crypto.subtle.digest(
                "MD5",
                data
            );

        const bytes =
            new Uint8Array(hashBuffer);

        return Array.from(bytes)
            .map(
                b =>
                    b
                        .toString(16)
                        .padStart(2, "0")
            )
            .join("");
    }


    /*
       Note:

       Modern browsers may not expose MD5 through
       crypto.subtle.

       Therefore this helper is mainly informational.

       Amperfy itself calculates the Subsonic token.
    */


    // ========================================================
    // TEST SUBSONIC CONNECTION
    // ========================================================

    async function testConnection() {
        const settings = loadSettings();

        if (!settings.username) {
            throw new Error(
                "Amperfy username is not configured."
            );
        }

        /*
           We cannot generate a Subsonic token reliably
           using browser crypto on every browser.

           Instead use the backend health endpoint.
        */

        const response = await fetch(
            `${getServerUrl()}/api/amperfy/status`,
            {
                method: "GET",
                cache: "no-store"
            }
        );

        if (!response.ok) {
            throw new Error(
                `Server returned HTTP ${response.status}`
            );
        }

        const data =
            await response.json();

        if (!data.subsonic) {
            throw new Error(
                "Subsonic API is not enabled."
            );
        }

        return data;
    }


    // ========================================================
    // COPY SERVER URL
    // ========================================================

    async function copyServerUrl() {
        const url = getServerUrl();

        if (
            navigator.clipboard &&
            navigator.clipboard.writeText
        ) {
            await navigator.clipboard.writeText(
                url
            );

            return url;
        }

        const textarea =
            document.createElement(
                "textarea"
            );

        textarea.value = url;

        textarea.style.position =
            "fixed";

        textarea.style.opacity = "0";

        document.body.appendChild(
            textarea
        );

        textarea.select();

        document.execCommand(
            "copy"
        );

        textarea.remove();

        return url;
    }


    // ========================================================
    // CONFIGURE
    // ========================================================

    function configure(options = {}) {
        const current =
            loadSettings();

        const updated = {
            ...current,
            ...options
        };

        saveSettings(
            updated
        );

        return updated;
    }


    // ========================================================
    // AMPERFY SETUP INFO
    // ========================================================

    function getSetupInfo() {
        const info =
            getConnectionInfo();

        return {
            serverUrl: info.server,
            apiUrl: info.rest,
            username: info.username,

            /*
               Amperfy needs the server address,
               not /rest in the server field if
               the app automatically handles
               Subsonic endpoints.

               If Amperfy asks for the API/base URL,
               use the server URL and let the client
               append /rest.
            */

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


    // ========================================================
    // SERVER DISCOVERY
    // ========================================================

    async function getServerStatus() {
        const response = await fetch(
            `${getServerUrl()}/api/amperfy/status`,
            {
                cache: "no-store"
            }
        );

        if (!response.ok) {
            throw new Error(
                `HTTP ${response.status}`
            );
        }

        return await response.json();
    }


    // ========================================================
    // OPEN SERVER
    // ========================================================

    function openServer() {
        window.open(
            getServerUrl(),
            "_blank",
            "noopener,noreferrer"
        );
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


    // ========================================================
    // DEBUG
    // ========================================================

    console.info(
        "Xrob Amperfy/Subsonic helper loaded."
    );

})();
