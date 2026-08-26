/* ============================================================
   XROB MUSIC — OPEN SUBSONIC / ARPEGGI INTEGRATION
   File: static/amperfy.js

   Purpose:
   - Manage Xrob Music's OpenSubsonic connection UI
   - Test server availability
   - Show OpenSubsonic server information
   - Copy connection URL
   - Generate connection instructions
   - Keep Arpeggi-related browser logic separate
     from the main application logic

   IMPORTANT:
   Arpeggi connects directly to main.py's OpenSubsonic
   endpoints. This file does NOT try to act as an
   OpenSubsonic server.
   ============================================================ */


(function () {

    "use strict";


    /* ========================================================
       CONFIG
       ======================================================== */

    const API_PREFIX = "/api";


    /* ========================================================
       HELPERS
       ======================================================== */

    function byId(id) {
        return document.getElementById(id);
    }


    function getServerBaseUrl() {

        /*
         * Remove trailing slash.
         *
         * Example:
         * http://192.168.1.73:8100/
         *
         * becomes:
         * http://192.168.1.73:8100
         */

        return window.location.origin.replace(
            /\/+$/,
            ""
        );
    }


    function showMessage(
        elementId,
        message,
        type = "info"
    ) {

        const element =
            byId(elementId);

        if (!element) {
            return;
        }


        element.textContent =
            message;


        element.dataset.state =
            type;


        element.classList.remove(
            "success",
            "error",
            "info"
        );


        element.classList.add(
            type
        );
    }


    function showToastSafe(
        message
    ) {

        if (
            typeof window.showToast ===
            "function"
        ) {

            window.showToast(
                message
            );

            return;
        }


        console.log(
            message
        );
    }


    function setButtonLoading(
        button,
        loading,
        loadingText = "Checking..."
    ) {

        if (!button) {
            return;
        }


        if (loading) {

            if (
                !button.dataset.originalText
            ) {

                button.dataset.originalText =
                    button.textContent;
            }


            button.disabled =
                true;


            button.textContent =
                loadingText;

        } else {

            button.disabled =
                false;


            if (
                button.dataset.originalText
            ) {

                button.textContent =
                    button.dataset.originalText;
            }
        }
    }


    async function safeJson(
        response
    ) {

        try {

            return await response.json();

        } catch (error) {

            return {};
        }
    }


    /* ========================================================
       OPEN SUBSONIC SERVER URL
       ======================================================== */

    function getOpenSubsonicUrl() {

        /*
         * OpenSubsonic clients such as Arpeggi normally
         * connect to the server root, then call:
         *
         * /rest/ping.view
         *
         * Therefore we return the Xrob Music server root.
         */

        return getServerBaseUrl();
    }


    /* ========================================================
       REST ENDPOINT HELPERS
       ======================================================== */

    function getRestUrl(
        endpoint,
        params = {}
    ) {

        const url =
            new URL(
                `/rest/${endpoint}`,
                getServerBaseUrl()
            );


        Object.entries(
            params
        ).forEach(
            ([key, value]) => {

                if (
                    value !== undefined &&
                    value !== null &&
                    value !== ""
                ) {

                    url.searchParams.set(
                        key,
                        value
                    );
                }
            }
        );


        return url.toString();
    }


    /* ========================================================
       TEST OPEN SUBSONIC
       ======================================================== */

    async function testOpenSubsonic() {

        const button =
            byId(
                "amperfy-test-button"
            );


        setButtonLoading(
            button,
            true,
            "Testing..."
        );


        showMessage(
            "amperfy-status",
            "Connecting to Xrob Music OpenSubsonic server...",
            "info"
        );


        try {

            const url =
                getRestUrl(
                    "ping.view",
                    {
                        v: "1.16.1",
                        c: "XrobMusic",
                        f: "json"
                    }
                );


            const response =
                await fetch(
                    url,
                    {
                        method: "GET",
                        cache: "no-store"
                    }
                );


            const data =
                await safeJson(
                    response
                );


            if (!response.ok) {

                throw new Error(
                    `HTTP ${response.status}`
                );
            }


            const root =
                data["subsonic-response"] ||
                data["subsonicResponse"] ||
                data;


            const status =
                root.status;


            if (
                status &&
                status.toLowerCase() !==
                    "ok"
            ) {

                const message =
                    root.error?.message ||
                    "OpenSubsonic server returned an error.";

                throw new Error(
                    message
                );
            }


            showMessage(
                "amperfy-status",
                "✅ Xrob Music OpenSubsonic server is online.",
                "success"
            );


            showToastSafe(
                "✅ Arpeggi/OpenSubsonic connection works"
            );


            await loadOpenSubsonicInfo();


            return true;


        } catch (error) {

            console.error(
                "OpenSubsonic test failed:",
                error
            );


            showMessage(
                "amperfy-status",
                "❌ OpenSubsonic connection failed: " +
                    (
                        error.message ||
                        "Unknown error"
                    ),
                "error"
            );


            showToastSafe(
                "❌ OpenSubsonic connection failed"
            );


            return false;


        } finally {

            setButtonLoading(
                button,
                false
            );
        }
    }


    /* ========================================================
       LOAD SERVER INFORMATION
       ======================================================== */

    async function loadOpenSubsonicInfo() {

        try {

            const url =
                getRestUrl(
                    "getOpenSubsonicExtensions.view",
                    {
                        v: "1.16.1",
                        c: "XrobMusic",
                        f: "json"
                    }
                );


            const response =
                await fetch(
                    url,
                    {
                        cache: "no-store"
                    }
                );


            if (!response.ok) {
                return;
            }


            const data =
                await safeJson(
                    response
                );


            const root =
                data["subsonic-response"] ||
                data;


            const serverVersion =
                root.serverVersion ||
                "";


            const openSubsonic =
                root.openSubsonic;


            const versionElement =
                byId(
                    "amperfy-server-version"
                );


            const openSubsonicElement =
                byId(
                    "amperfy-open-subsonic"
                );


            if (versionElement) {

                versionElement.textContent =
                    serverVersion ||
                    "Xrob Music";
            }


            if (openSubsonicElement) {

                openSubsonicElement.textContent =
                    openSubsonic
                        ? "Supported"
                        : "Standard Subsonic";
            }


        } catch (error) {

            console.warn(
                "Could not load OpenSubsonic info:",
                error
            );
        }
    }


    /* ========================================================
       GET MUSIC FOLDERS TEST
       ======================================================== */

    async function testMusicFolders() {

        try {

            const url =
                getRestUrl(
                    "getMusicFolders.view",
                    {
                        v: "1.16.1",
                        c: "XrobMusic",
                        f: "json"
                    }
                );


            const response =
                await fetch(
                    url,
                    {
                        cache: "no-store"
                    }
                );


            const data =
                await safeJson(
                    response
                );


            if (!response.ok) {
                return false;
            }


            const root =
                data["subsonic-response"] ||
                data;


            return (
                root.status === "ok"
            );


        } catch (error) {

            console.warn(
                "Music folders test failed:",
                error
            );

            return false;
        }
    }


    /* ========================================================
       COPY SERVER URL
       ======================================================== */

    async function copyServerUrl() {

        const url =
            getOpenSubsonicUrl();


        try {

            await navigator.clipboard.writeText(
                url
            );


            showToastSafe(
                "📋 Xrob Music server URL copied"
            );


        } catch (error) {

            console.warn(
                "Clipboard unavailable:",
                error
            );


            window.prompt(
                "Copy your Xrob Music server URL:",
                url
            );
        }
    }


    /* ========================================================
       ARPEGGI CONNECTION INFORMATION
       ======================================================== */

    function getArpeggiConnectionInfo() {

        return {
            serverUrl:
                getOpenSubsonicUrl(),

            apiUrl:
                `${getOpenSubsonicUrl()}/rest`,

            pingUrl:
                `${getOpenSubsonicUrl()}/rest/ping.view`,
        };
    }


    /* ========================================================
       OPEN ARPEGGI HELP
       ======================================================== */

    function showArpeggiInstructions() {

        const info =
            getArpeggiConnectionInfo();


        const message = [
            "Xrob Music → Arpeggi",
            "",
            `Server URL: ${info.serverUrl}`,
            "",
            "In Arpeggi:",
            "1. Add a new server.",
            "2. Enter the Xrob Music server URL.",
            "3. Enter your Xrob Music username.",
            "4. Enter your Xrob Music password.",
            "5. Connect.",
            "",
            "Arpeggi will communicate directly with Xrob Music's OpenSubsonic API."
        ].join(
            "\n"
        );


        window.alert(
            message
        );
    }


    /* ========================================================
       SERVER HEALTH
       ======================================================== */

    async function checkServerHealth() {

        try {

            const response =
                await fetch(
                    `${API_PREFIX}/health`,
                    {
                        cache:
                            "no-store"
                    }
                );


            if (
                response.ok
            ) {

                return await safeJson(
                    response
                );
            }


        } catch (error) {

            console.warn(
                "Health check failed:",
                error
            );
        }


        return null;
    }


    /* ========================================================
       REFRESH ARPEGGI STATUS
       ======================================================== */

    async function refreshArpeggiStatus() {

        const statusElement =
            byId(
                "amperfy-status"
            );


        if (statusElement) {

            statusElement.textContent =
                "Checking Xrob Music...";
        }


        const health =
            await checkServerHealth();


        if (
            health &&
            statusElement
        ) {

            statusElement.textContent =
                "✅ Xrob Music server is running.";
        }


        await testOpenSubsonic();
    }


    /* ========================================================
       INITIALIZE UI
       ======================================================== */

    function initAmperfyUI() {

        const testButton =
            byId(
                "amperfy-test-button"
            );


        if (
            testButton &&
            !testButton.dataset.bound
        ) {

            testButton.dataset.bound =
                "true";


            testButton.addEventListener(
                "click",
                testOpenSubsonic
            );
        }


        const copyButton =
            byId(
                "amperfy-copy-url"
            );


        if (
            copyButton &&
            !copyButton.dataset.bound
        ) {

            copyButton.dataset.bound =
                "true";


            copyButton.addEventListener(
                "click",
                copyServerUrl
            );
        }


        const helpButton =
            byId(
                "amperfy-help-button"
            );


        if (
            helpButton &&
            !helpButton.dataset.bound
        ) {

            helpButton.dataset.bound =
                "true";


            helpButton.addEventListener(
                "click",
                showArpeggiInstructions
            );
        }


        const serverUrlElement =
            byId(
                "amperfy-server-url"
            );


        if (serverUrlElement) {

            serverUrlElement.value =
                getOpenSubsonicUrl();
        }


        const serverUrlText =
            byId(
                "amperfy-server-url-text"
            );


        if (serverUrlText) {

            serverUrlText.textContent =
                getOpenSubsonicUrl();
        }


        /*
         * Don't automatically spam the server on every page
         * load. Only test when the Settings page/UI exists.
         */

        if (
            testButton
        ) {

            setTimeout(
                () => {
                    loadOpenSubsonicInfo();
                },
                300
            );
        }
    }


    /* ========================================================
       PUBLIC API
       ======================================================== */

    window.XrobArpeggi = {

        getServerUrl:
            getOpenSubsonicUrl,

        getRestUrl:
            getRestUrl,

        testConnection:
            testOpenSubsonic,

        testMusicFolders:
            testMusicFolders,

        loadServerInfo:
            loadOpenSubsonicInfo,

        copyServerUrl:
            copyServerUrl,

        showInstructions:
            showArpeggiInstructions,

        refreshStatus:
            refreshArpeggiStatus,

        getConnectionInfo:
            getArpeggiConnectionInfo,
    };


    /* ========================================================
       START
       ======================================================== */

    if (
        document.readyState ===
        "loading"
    ) {

        document.addEventListener(
            "DOMContentLoaded",
            initAmperfyUI
        );

    } else {

        initAmperfyUI();
    }


})();
