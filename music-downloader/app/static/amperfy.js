/* ============================================================
   XROB MUSIC — ARPEGGI / OPENSUBSONIC
   ============================================================ */

(function () {

    "use strict";


    function serverUrl() {

        /*
         * External Xrob Music port.
         * Xrob itself is running internally on 8099,
         * but config.yaml maps it to host port 8100.
         */

        return (
            window.location.protocol +
            "//" +
            window.location.hostname +
            ":8100"
        ).replace(
            /\/+$/,
            ""
        );
    }


    function restUrl(
        endpoint,
        params = {}
    ) {

        const url =
            new URL(
                `/rest/${endpoint}`,
                serverUrl()
            );

        Object.entries(
            params
        ).forEach(
            ([key, value]) => {

                if (
                    value !== undefined
                    &&
                    value !== null
                    &&
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


    function setStatus(
        message,
        type = "info"
    ) {

        const element =
            document.getElementById(
                "amperfy-status"
            );

        if (!element) {
            return;
        }

        element.textContent =
            message;

        element.dataset.state =
            type;
    }


    async function testConnection() {

        const button =
            document.getElementById(
                "amperfy-test-button"
            );

        if (button) {

            button.disabled =
                true;

            button.textContent =
                "Testing...";
        }

        setStatus(
            "Checking Xrob Music OpenSubsonic server..."
        );

        try {

            const response =
                await fetch(
                    restUrl(
                        "ping.view",
                        {
                            v:
                                "1.16.1",

                            c:
                                "Arpeggi",

                            f:
                                "json",
                        }
                    ),
                    {
                        cache:
                            "no-store",
                    }
                );

            const data =
                await response.json();

            const root =
                data[
                    "subsonic-response"
                ];

            if (
                !response.ok
                ||
                !root
                ||
                root.status !== "ok"
            ) {

                throw new Error(
                    "OpenSubsonic ping failed."
                );
            }

            setStatus(
                "✅ Xrob Music OpenSubsonic server is online.",
                "success"
            );

            const supported =
                document.getElementById(
                    "amperfy-open-subsonic"
                );

            if (supported) {

                supported.textContent =
                    "Supported";
            }

            return true;

        } catch (error) {

            console.error(
                error
            );

            setStatus(
                "❌ " +
                (
                    error.message
                    ||
                    "Connection failed."
                ),
                "error"
            );

            return false;

        } finally {

            if (button) {

                button.disabled =
                    false;

                button.textContent =
                    "🔌 Test Connection";
            }
        }
    }


    async function copyServerUrl() {

        const url =
            serverUrl();

        try {

            await navigator.clipboard.writeText(
                url
            );

            showToast?.(
                "📋 Server URL copied"
            );

        } catch {

            window.prompt(
                "Copy Xrob Music server URL:",
                url
            );
        }
    }


    function init() {

        const urlInput =
            document.getElementById(
                "amperfy-server-url"
            );

        if (urlInput) {
            urlInput.value =
                serverUrl();
        }


        const testButton =
            document.getElementById(
                "amperfy-test-button"
            );

        testButton?.addEventListener(
            "click",
            testConnection
        );


        const copyButton =
            document.getElementById(
                "amperfy-copy-url"
            );

        copyButton?.addEventListener(
            "click",
            copyServerUrl
        );


        testConnection();
    }


    window.XrobArpeggi = {

        getServerUrl:
            serverUrl,

        getRestUrl:
            restUrl,

        testConnection:
            testConnection,

        copyServerUrl:
            copyServerUrl,
    };


    if (
        document.readyState ===
        "loading"
    ) {

        document.addEventListener(
            "DOMContentLoaded",
            init
        );

    } else {

        init();
    }

})();
