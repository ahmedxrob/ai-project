const state = {
    services: [],
    editingId: null,
};


const $ = (selector) =>
    document.querySelector(selector);


const modal = $("#modal");
const form = $("#serviceForm");
const formError = $("#formError");
const saveBtn = $("#saveBtn");


function escapeHtml(value) {
    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}


function showToast(message) {
    const toast = $("#toast");

    toast.textContent = message;
    toast.classList.add("show");

    clearTimeout(
        showToast.timer
    );

    showToast.timer = setTimeout(
        () => {
            toast.classList.remove("show");
        },
        2800
    );
}


function statusClass(status) {
    switch (status) {
        case "running":
            return "status-running";

        case "starting":
            return "status-starting";

        case "error":
            return "status-error";

        default:
            return "status-stopped";
    }
}


function statusText(status) {
    switch (status) {
        case "running":
            return "Online";

        case "starting":
            return "Starting";

        case "error":
            return "Error";

        default:
            return "Stopped";
    }
}


function renderStats() {
    const total = state.services.length;

    const online = state.services.filter(
        (service) =>
            service.status === "running"
    ).length;

    const starting = state.services.filter(
        (service) =>
            service.status === "starting"
    ).length;

    $("#serviceCount").textContent = total;
    $("#onlineCount").textContent = online;
    $("#startingCount").textContent = starting;
}


function renderServices() {
    const grid = $("#servicesGrid");
    const empty = $("#emptyState");

    grid.innerHTML = "";

    renderStats();

    if (!state.services.length) {
        empty.classList.remove("hidden");
        return;
    }

    empty.classList.add("hidden");

    state.services.forEach(
        (service) => {
            const card = document.createElement(
                "article"
            );

            card.className = "service-card";

            const status = service.status || "stopped";
            const url = service.url || "";

            const publicContent = url
                ? `
                    <div class="public-label">
                        Public URL
                    </div>

                    <div class="public-url">
                        ${escapeHtml(url)}
                    </div>
                `
                : `
                    <div class="public-label">
                        Public URL
                    </div>

                    <div class="public-empty">
                        ${
                            service.error
                                ? escapeHtml(
                                    service.error
                                )
                                : "No public URL yet."
                        }
                    </div>
                `;

            card.innerHTML = `
                <div class="service-head">

                    <div>
                        <div class="service-name">
                            ${escapeHtml(
                                service.name
                            )}
                        </div>

                        <div class="service-target">
                            ${escapeHtml(
                                service.target
                            )}
                        </div>
                    </div>

                    <div
                        class="status ${statusClass(
                            status
                        )}"
                    >
                        <span class="status-dot"></span>
                        ${statusText(status)}
                    </div>

                </div>

                <div class="public-box">
                    ${publicContent}
                </div>

                <div class="card-actions">

                    ${
                        url
                            ? `
                                <button
                                    class="btn btn-primary small-btn"
                                    data-action="open"
                                    data-id="${service.id}"
                                >
                                    Open
                                </button>

                                <button
                                    class="btn btn-secondary small-btn"
                                    data-action="copy"
                                    data-id="${service.id}"
                                >
                                    Copy URL
                                </button>
                            `
                            : ""
                    }

                    ${
                        status === "running" ||
                        status === "starting"
                            ? `
                                <button
                                    class="btn btn-secondary small-btn"
                                    data-action="stop"
                                    data-id="${service.id}"
                                >
                                    Stop
                                </button>
                            `
                            : `
                                <button
                                    class="btn btn-primary small-btn"
                                    data-action="start"
                                    data-id="${service.id}"
                                >
                                    Start
                                </button>
                            `
                    }

                    <button
                        class="btn btn-secondary small-btn"
                        data-action="restart"
                        data-id="${service.id}"
                    >
                        Restart
                    </button>

                    <button
                        class="btn btn-secondary small-btn"
                        data-action="edit"
                        data-id="${service.id}"
                    >
                        Edit
                    </button>

                    <button
                        class="btn btn-danger small-btn"
                        data-action="delete"
                        data-id="${service.id}"
                    >
                        Delete
                    </button>

                </div>
            `;

            grid.appendChild(card);
        }
    );
}


async function api(
    url,
    options = {}
) {
    const response = await fetch(
        url,
        {
            headers: {
                "Content-Type":
                    "application/json",
                ...(options.headers || {}),
            },
            ...options,
        }
    );

    let data = null;

    try {
        data = await response.json();
    } catch {
        data = null;
    }

    if (!response.ok) {
        throw new Error(
            data?.error ||
            `Request failed (${response.status})`
        );
    }

    return data;
}


async function refresh() {
    try {
        const services = await api(
            "/api/services"
        );

        state.services = Array.isArray(
            services
        )
            ? services
            : [];

        renderServices();

    } catch (error) {
        console.error(error);
    }
}


function openModal(service = null) {
    state.editingId = service
        ? service.id
        : null;

    $("#modalTitle").textContent =
        service
            ? "Edit Service"
            : "Add Service";

    $("#serviceId").value =
        service?.id || "";

    $("#serviceName").value =
        service?.name || "";

    $("#serviceTarget").value =
        service?.target || "";

    $("#serviceEnabled").checked =
        service?.enabled ?? true;

    formError.classList.add(
        "hidden"
    );

    formError.textContent = "";

    saveBtn.disabled = false;

    saveBtn.textContent =
        service
            ? "Save Changes"
            : "Save Service";

    modal.classList.remove(
        "hidden"
    );

    setTimeout(
        () =>
            $("#serviceName").focus(),
        50
    );
}


function closeModal() {
    modal.classList.add(
        "hidden"
    );

    state.editingId = null;

    form.reset();

    $("#serviceEnabled").checked =
        true;
}


async function submitForm(event) {
    event.preventDefault();

    const name =
        $("#serviceName").value.trim();

    const target =
        $("#serviceTarget").value.trim();

    const enabled =
        $("#serviceEnabled").checked;

    formError.classList.add(
        "hidden"
    );

    if (!name || !target) {
        formError.textContent =
            "Please fill in all fields.";

        formError.classList.remove(
            "hidden"
        );

        return;
    }

    saveBtn.disabled = true;

    saveBtn.textContent =
        state.editingId
            ? "Saving..."
            : "Creating...";

    try {
        const payload = {
            name,
            target,
            enabled,
        };

        if (state.editingId) {
            await api(
                `/api/services/${state.editingId}`,
                {
                    method: "PUT",
                    body: JSON.stringify(
                        payload
                    ),
                }
            );

            showToast(
                "Service updated."
            );

        } else {
            await api(
                "/api/services",
                {
                    method: "POST",
                    body: JSON.stringify(
                        payload
                    ),
                }
            );

            showToast(
                "Service created."
            );
        }

        closeModal();

        await refresh();

    } catch (error) {
        formError.textContent =
            error.message;

        formError.classList.remove(
            "hidden"
        );

    } finally {
        saveBtn.disabled = false;

        saveBtn.textContent =
            state.editingId
                ? "Save Changes"
                : "Save Service";
    }
}


async function serviceAction(
    action,
    id
) {
    const service =
        state.services.find(
            (item) =>
                item.id === id
        );

    if (!service) {
        return;
    }

    try {
        if (action === "open") {
            if (service.url) {
                window.open(
                    service.url,
                    "_blank",
                    "noopener,noreferrer"
                );
            }

            return;
        }


        if (action === "copy") {
            if (!service.url) {
                return;
            }

            await navigator.clipboard.writeText(
                service.url
            );

            showToast(
                "Public URL copied."
            );

            return;
        }


        if (action === "edit") {
            openModal(service);
            return;
        }


        if (action === "delete") {
            const confirmed =
                window.confirm(
                    `Delete "${service.name}"?`
                );

            if (!confirmed) {
                return;
            }

            await api(
                `/api/services/${id}`,
                {
                    method: "DELETE",
                }
            );

            showToast(
                "Service deleted."
            );

            await refresh();
            return;
        }


        await api(
            `/api/services/${id}/${action}`,
            {
                method: "POST",
            }
        );

        const messages = {
            start: "Service started.",
            stop: "Service stopped.",
            restart: "Service restarted.",
        };

        showToast(
            messages[action] ||
                "Done."
        );

        await refresh();

    } catch (error) {
        showToast(
            error.message
        );
    }
}


document.addEventListener(
    "click",
    (event) => {
        const button =
            event.target.closest(
                "[data-action]"
            );

        if (!button) {
            return;
        }

        serviceAction(
            button.dataset.action,
            button.dataset.id
        );
    }
);


$("#addServiceBtn").addEventListener(
    "click",
    () => openModal()
);


$("#emptyAddBtn").addEventListener(
    "click",
    () => openModal()
);


$("#closeModalBtn").addEventListener(
    "click",
    closeModal
);


$("#cancelBtn").addEventListener(
    "click",
    closeModal
);


modal.addEventListener(
    "click",
    (event) => {
        if (
            event.target === modal
        ) {
            closeModal();
        }
    }
);


form.addEventListener(
    "submit",
    submitForm
);


document.addEventListener(
    "keydown",
    (event) => {
        if (
            event.key === "Escape" &&
            !modal.classList.contains(
                "hidden"
            )
        ) {
            closeModal();
        }
    }
);


// Initial load
refresh();


// Refresh statuses / URLs
setInterval(
    refresh,
    3000
);

