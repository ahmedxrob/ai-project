let services = [];

const $ = (id) => document.getElementById(id);

const dialog = $("dialog");

let toastTimer = null;


function toast(message) {
    const element = $("toast");

    element.textContent = message;

    element.classList.add("show");

    clearTimeout(toastTimer);

    toastTimer = setTimeout(() => {
        element.classList.remove("show");
    }, 2800);
}


function escapeHtml(value) {
    return String(value ?? "")
        .replace(/[&<>"']/g, (character) => ({
            "&": "&amp;",
            "<": "&lt;",
            ">": "&gt;",
            '"': "&quot;",
            "'": "&#039;"
        })[character]);
}


function escapeAttribute(value) {
    return escapeHtml(value)
        .replace(/`/g, "&#096;");
}


async function load() {

    try {

        const response = await fetch(
            "/api/services",
            {
                cache: "no-store"
            }
        );

        if (!response.ok) {
            throw new Error(
                `HTTP ${response.status}`
            );
        }

        services = await response.json();

        render();

    } catch (error) {

        console.error(error);

        toast(
            "Unable to contact Port Publisher"
        );
    }
}


function render() {

    const total = services.length;

    const online = services.filter(
        service => service.status === "running"
    ).length;

    const starting = services.filter(
        service => service.status === "starting"
    ).length;


    $("count").textContent = total;

    $("onlineCount").textContent = online;

    $("startingCount").textContent = starting;


    if (!services.length) {

        $("list").innerHTML = `
            <div class="empty">

                <div class="empty-icon">
                    ☁
                </div>

                <h3>
                    No published services
                </h3>

                <p>
                    Add a local HTTP or HTTPS service
                    and Xrob Port Publisher will create
                    a Cloudflare public URL for it.
                    Your services are saved automatically.
                </p>

            </div>
        `;

        return;
    }


    $("list").innerHTML = services
        .map(renderService)
        .join("");
}


function renderService(service) {

    const status = service.status || "stopped";

    const running = status === "running";

    const starting = status === "starting";


    const statusText =
        running
            ? "Online"
            : starting
                ? "Starting tunnel…"
                : "Stopped";


    const statusClass =
        running
            ? "running"
            : starting
                ? "starting"
                : "stopped";


    const url = service.url || "";


    return `
        <article class="service-card">

            <div class="service-top">

                <div class="service-info">

                    <div class="service-name">
                        ${escapeHtml(service.name)}
                    </div>

                    <div class="service-target">
                        ${escapeHtml(service.target)}
                    </div>

                    <div class="status">

                        <span
                            class="status-dot ${statusClass}"
                        ></span>

                        ${statusText}

                    </div>

                </div>


                <div class="service-actions">

                    ${
                        running
                            ? `
                                <button
                                    onclick="stopService('${escapeAttribute(service.id)}')"
                                >
                                    Stop
                                </button>
                            `
                            : `
                                <button
                                    onclick="startService('${escapeAttribute(service.id)}')"
                                >
                                    Start
                                </button>
                            `
                    }

                    <button
                        onclick="restartService('${escapeAttribute(service.id)}')"
                    >
                        ↻ Restart
                    </button>

                    <button
                        onclick="editService('${escapeAttribute(service.id)}')"
                    >
                        Edit
                    </button>

                    <button
                        class="danger"
                        onclick="deleteService('${escapeAttribute(service.id)}')"
                    >
                        Delete
                    </button>

                </div>

            </div>


            ${
                url
                    ? `
                        <div class="public-url">

                            <div class="public-url-icon">
                                ↗
                            </div>

                            <a
                                href="${escapeAttribute(url)}"
                                target="_blank"
                                rel="noopener noreferrer"
                                title="${escapeAttribute(url)}"
                            >
                                ${escapeHtml(url)}
                            </a>

                            <button
                                class="copy-button"
                                onclick="copyText('${escapeAttribute(url)}')"
                            >
                                Copy
                            </button>

                        </div>
                    `
                    : ""
            }


            ${
                service.error
                    ? `
                        <div class="service-error">
                            ${escapeHtml(service.error)}
                        </div>
                    `
                    : ""
            }

        </article>
    `;
}


function openAdd() {

    $("modalTitle").textContent =
        "Add service";

    $("serviceId").value = "";

    $("name").value = "";

    $("target").value = "";

    $("saveButton").textContent =
        "Save & publish";

    dialog.showModal();

    setTimeout(
        () => $("name").focus(),
        50
    );
}


function editService(id) {

    const service = services.find(
        item => item.id === id
    );

    if (!service) {
        return;
    }


    $("modalTitle").textContent =
        "Edit service";

    $("serviceId").value =
        service.id;

    $("name").value =
        service.name;

    $("target").value =
        service.target;

    $("saveButton").textContent =
        "Save changes";


    dialog.showModal();

    setTimeout(
        () => $("name").focus(),
        50
    );
}


async function saveService(event) {

    event.preventDefault();


    const id =
        $("serviceId").value.trim();


    const name =
        $("name").value.trim();


    const target =
        $("target").value.trim();


    if (!name || !target) {
        toast(
            "Please fill in all fields"
        );

        return false;
    }


    const button =
        $("saveButton");

    const originalText =
        button.textContent;


    button.disabled = true;

    button.textContent =
        id
            ? "Saving…"
            : "Publishing…";


    try {

        const response = await fetch(
            id
                ? `/api/services/${encodeURIComponent(id)}`
                : "/api/services",
            {
                method: id
                    ? "PUT"
                    : "POST",

                headers: {
                    "Content-Type":
                        "application/json"
                },

                body: JSON.stringify({
                    name,
                    target,
                    enabled: true
                })
            }
        );


        const data =
            await response.json();


        if (!response.ok) {

            toast(
                data.error ||
                "Failed to save service"
            );

            return false;
        }


        dialog.close();


        toast(
            id
                ? "Service updated"
                : "Service saved — starting tunnel"
        );


        await load();


        setTimeout(
            load,
            1500
        );

        setTimeout(
            load,
            3500
        );


    } catch (error) {

        console.error(error);

        toast(
            "Unable to save service"
        );

    } finally {

        button.disabled = false;

        button.textContent =
            originalText;
    }


    return false;
}


async function startService(id) {

    toast(
        "Starting tunnel…"
    );


    try {

        const response = await fetch(
            `/api/services/${encodeURIComponent(id)}/start`,
            {
                method: "POST"
            }
        );


        const data =
            await response.json();


        if (!response.ok) {

            toast(
                data.error ||
                "Unable to start tunnel"
            );

            return;
        }


        await load();


        setTimeout(
            load,
            1500
        );

        setTimeout(
            load,
            3500
        );


    } catch (error) {

        console.error(error);

        toast(
            "Start failed"
        );
    }
}


async function stopService(id) {

    toast(
        "Stopping tunnel…"
    );


    try {

        const response = await fetch(
            `/api/services/${encodeURIComponent(id)}/stop`,
            {
                method: "POST"
            }
        );


        const data =
            await response.json();


        if (!response.ok) {

            toast(
                data.error ||
                "Unable to stop tunnel"
            );

            return;
        }


        await load();

        toast(
            "Tunnel stopped"
        );


    } catch (error) {

        console.error(error);

        toast(
            "Stop failed"
        );
    }
}


async function restartService(id) {

    toast(
        "Restarting tunnel…"
    );


    try {

        const response = await fetch(
            `/api/services/${encodeURIComponent(id)}/restart`,
            {
                method: "POST"
            }
        );


        const data =
            await response.json();


        if (!response.ok) {

            toast(
                data.error ||
                "Restart failed"
            );

            return;
        }


        await load();


        setTimeout(
            load,
            1200
        );

        setTimeout(
            load,
            3000
        );

        setTimeout(
            load,
            5000
        );


    } catch (error) {

        console.error(error);

        toast(
            "Restart failed"
        );
    }
}


async function deleteService(id) {

    const service =
        services.find(
            item => item.id === id
        );


    if (!service) {
        return;
    }


    const confirmed =
        confirm(
            `Delete "${service.name}" and stop its Cloudflare tunnel?`
        );


    if (!confirmed) {
        return;
    }


    try {

        const response = await fetch(
            `/api/services/${encodeURIComponent(id)}`,
            {
                method: "DELETE"
            }
        );


        const data =
            await response.json();


        if (!response.ok) {

            toast(
                data.error ||
                "Delete failed"
            );

            return;
        }


        toast(
            "Service deleted"
        );


        await load();


    } catch (error) {

        console.error(error);

        toast(
            "Delete failed"
        );
    }
}


async function copyText(text) {

    try {

        await navigator.clipboard.writeText(
            text
        );

        toast(
            "Public URL copied"
        );

    } catch (error) {

        console.error(error);

        toast(
            "Copy failed"
        );
    }
}


dialog.addEventListener(
    "click",
    event => {

        if (event.target === dialog) {
            dialog.close();
        }

    }
);


load();


setInterval(
    load,
    3000
);
