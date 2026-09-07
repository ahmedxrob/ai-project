import json
import os
import re
import signal
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory


APP_DIR = Path("/app")
STATIC_DIR = APP_DIR / "static"
DATA_DIR = Path("/data")
CONFIG_FILE = DATA_DIR / "services.json"
OPTIONS_FILE = Path("/data/options.json")

CLOUDFLARED = APP_DIR / "cloudflared"
PORT = 8055

app = Flask(
    __name__,
    static_folder=str(STATIC_DIR),
    static_url_path="",
)

lock = threading.RLock()
tunnels = {}

URL_RE = re.compile(
    r"https://[a-z0-9-]+\.trycloudflare\.com",
    re.IGNORECASE,
)


# ---------------------------------------------------------
# Persistence
# ---------------------------------------------------------

def load_services():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not CONFIG_FILE.exists():
        return []

    try:
        data = json.loads(CONFIG_FILE.read_text())

        if isinstance(data, list):
            print(
                f"[config] Loaded {len(data)} service(s)",
                flush=True,
            )
            return data

        print(
            "[config] services.json is not a list; starting empty",
            flush=True,
        )

    except Exception as exc:
        print(
            f"[config] Failed to read services.json: {exc}",
            flush=True,
        )

    return []


def save_services(current_services):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    tmp = CONFIG_FILE.with_suffix(".tmp")

    tmp.write_text(
        json.dumps(
            current_services,
            indent=2,
            ensure_ascii=False,
        )
    )

    tmp.replace(CONFIG_FILE)


services = load_services()


# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------

def normalize_target(target):
    target = (target or "").strip()

    parsed = urllib.parse.urlparse(target)

    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            "Target must start with http:// or https://"
        )

    if not parsed.hostname:
        raise ValueError(
            "Target must contain a hostname or IP address"
        )

    if parsed.username or parsed.password:
        raise ValueError(
            "Username/password in target URLs are not allowed"
        )

    if any(c in target for c in "\r\n\t"):
        raise ValueError("Invalid target URL")

    return target


def get_service(sid):
    with lock:
        return next(
            (service for service in services if service["id"] == sid),
            None,
        )


def service_public(service):
    sid = service["id"]

    with lock:
        tunnel = tunnels.get(sid)

        if not tunnel:
            return {
                **service,
                "status": "stopped",
                "url": "",
                "error": "",
            }

        process = tunnel["process"]
        url = tunnel.get("url", "")
        error = tunnel.get("error", "")

        if process.poll() is not None:
            status = "stopped"
        elif url:
            status = "running"
        else:
            status = "starting"

        return {
            **service,
            "status": status,
            "url": url,
            "error": error,
        }


# ---------------------------------------------------------
# Cloudflare Tunnel
# ---------------------------------------------------------

def start_tunnel(service):
    sid = service["id"]

    with lock:
        existing = tunnels.get(sid)

        if existing:
            process = existing.get("process")

            if process and process.poll() is None:
                return

        target = service["target"]

        cmd = [
            str(CLOUDFLARED),
            "tunnel",
            "--no-autoupdate",
            "--url",
            target,
        ]

        print(
            f"[tunnel:{sid}] Starting: {target}",
            flush=True,
        )

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

        except Exception as exc:
            print(
                f"[tunnel:{sid}] Failed to start: {exc}",
                flush=True,
            )

            tunnels[sid] = {
                "process": None,
                "url": "",
                "error": str(exc),
                "started": time.time(),
            }

            return

        tunnels[sid] = {
            "process": process,
            "url": "",
            "error": "",
            "started": time.time(),
        }

    thread = threading.Thread(
        target=read_tunnel_output,
        args=(sid, process),
        daemon=True,
    )

    thread.start()


def read_tunnel_output(sid, process):
    try:
        if process.stdout is None:
            return

        for line in process.stdout:
            line = line.rstrip()

            if line:
                print(
                    f"[tunnel:{sid}] {line}",
                    flush=True,
                )

            match = URL_RE.search(line)

            if match:
                url = match.group(0)

                with lock:
                    if sid in tunnels:
                        tunnels[sid]["url"] = url
                        tunnels[sid]["error"] = ""

                print(
                    f"[tunnel:{sid}] Public URL: {url}",
                    flush=True,
                )

                notify_url(sid, url)

    except Exception as exc:
        print(
            f"[tunnel:{sid}] Reader error: {exc}",
            flush=True,
        )

    finally:
        code = process.poll()

        with lock:
            if sid in tunnels:
                tunnels[sid]["process"] = process

                if code not in (None, 0):
                    tunnels[sid]["error"] = (
                        f"cloudflared exited with code {code}"
                    )


def stop_tunnel(sid):
    with lock:
        tunnel = tunnels.get(sid)

    if not tunnel:
        return

    process = tunnel.get("process")

    if process and process.poll() is None:
        print(
            f"[tunnel:{sid}] Stopping",
            flush=True,
        )

        try:
            process.terminate()
            process.wait(timeout=5)

        except Exception:
            try:
                process.kill()
                process.wait(timeout=2)
            except Exception:
                pass

    with lock:
        tunnels.pop(sid, None)


# ---------------------------------------------------------
# Telegram
# ---------------------------------------------------------

def notify_url(sid, url):
    service = get_service(sid)

    if not service:
        return

    try:
        if not OPTIONS_FILE.exists():
            return

        options = json.loads(
            OPTIONS_FILE.read_text()
        )

        token = options.get(
            "telegram_bot_token",
            "",
        )

        chat_id = options.get(
            "telegram_chat_id",
            "",
        )

        if not token or not chat_id:
            return

        endpoint = (
            f"https://api.telegram.org/"
            f"bot{token}/sendMessage"
        )

        text = (
            f"🌐 {service['name']} is public\n"
            f"{url}"
        )

        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": text,
        }).encode()

        req = urllib.request.Request(
            endpoint,
            data=data,
            method="POST",
        )

        urllib.request.urlopen(
            req,
            timeout=10,
        ).read()

        print(
            f"[telegram] Notification sent for "
            f"{service['name']}",
            flush=True,
        )

    except Exception as exc:
        print(
            f"[telegram] Notification failed: {exc}",
            flush=True,
        )


# ---------------------------------------------------------
# Web UI
# ---------------------------------------------------------

@app.get("/")
def index():
    index_file = STATIC_DIR / "index.html"

    if not index_file.exists():
        return jsonify({
            "error": "Port Publisher UI is missing",
            "expected": str(index_file),
        }), 500

    return send_from_directory(
        str(STATIC_DIR),
        "index.html",
    )


@app.get("/api/services")
def api_services():
    with lock:
        result = [
            service_public(service)
            for service in services
        ]

    return jsonify(result)


@app.post("/api/services")
def api_add_service():
    global services

    data = request.get_json(silent=True) or {}

    name = str(
        data.get("name", "")
    ).strip()

    target = data.get(
        "target",
        "",
    )

    if not name:
        return jsonify({
            "error": "Service name is required",
        }), 400

    if len(name) > 100:
        return jsonify({
            "error": "Service name is too long",
        }), 400

    try:
        target = normalize_target(target)

    except ValueError as exc:
        return jsonify({
            "error": str(exc),
        }), 400

    sid = os.urandom(6).hex()

    service = {
        "id": sid,
        "name": name,
        "target": target,
        "enabled": bool(
            data.get(
                "enabled",
                True,
            )
        ),
    }

    with lock:
        services.append(service)
        save_services(services)

    print(
        f"[app] Added service: "
        f"{name} → {target}",
        flush=True,
    )

    if service["enabled"]:
        start_tunnel(service)

    return jsonify(
        service_public(service)
    ), 201


@app.put("/api/services/<sid>")
def api_update_service(sid):
    global services

    data = request.get_json(silent=True) or {}

    with lock:
        service = get_service(sid)

    if not service:
        return jsonify({
            "error": "Service not found",
        }), 404

    name = str(
        data.get(
            "name",
            service["name"],
        )
    ).strip()

    target = data.get(
        "target",
        service["target"],
    )

    enabled = bool(
        data.get(
            "enabled",
            service.get(
                "enabled",
                True,
            ),
        )
    )

    if not name:
        return jsonify({
            "error": "Service name is required",
        }), 400

    try:
        target = normalize_target(target)

    except ValueError as exc:
        return jsonify({
            "error": str(exc),
        }), 400

    stop_tunnel(sid)

    with lock:
        service["name"] = name
        service["target"] = target
        service["enabled"] = enabled

        save_services(services)

    print(
        f"[app] Updated service: "
        f"{name} → {target}",
        flush=True,
    )

    if enabled:
        start_tunnel(service)

    return jsonify(
        service_public(service)
    )


@app.delete("/api/services/<sid>")
def api_delete_service(sid):
    global services

    service = get_service(sid)

    if not service:
        return jsonify({
            "error": "Service not found",
        }), 404

    stop_tunnel(sid)

    with lock:
        services = [
            service
            for service in services
            if service["id"] != sid
        ]

        save_services(services)

    print(
        f"[app] Deleted service: "
        f"{service['name']}",
        flush=True,
    )

    return jsonify({
        "ok": True,
    })


@app.post("/api/services/<sid>/restart")
def api_restart_service(sid):
    service = get_service(sid)

    if not service:
        return jsonify({
            "error": "Service not found",
        }), 404

    stop_tunnel(sid)

    time.sleep(0.3)

    start_tunnel(service)

    return jsonify(
        service_public(service)
    )


@app.post("/api/services/<sid>/stop")
def api_stop_service(sid):
    global services

    service = get_service(sid)

    if not service:
        return jsonify({
            "error": "Service not found",
        }), 404

    stop_tunnel(sid)

    with lock:
        service["enabled"] = False
        save_services(services)

    return jsonify(
        service_public(service)
    )


@app.post("/api/services/<sid>/start")
def api_start_service(sid):
    service = get_service(sid)

    if not service:
        return jsonify({
            "error": "Service not found",
        }), 404

    with lock:
        service["enabled"] = True
        save_services(services)

    start_tunnel(service)

    return jsonify(
        service_public(service)
    )


@app.get("/api/health")
def api_health():
    with lock:
        running = 0

        for tunnel in tunnels.values():
            process = tunnel.get("process")

            if process and process.poll() is None:
                running += 1

        return jsonify({
            "ok": True,
            "services": len(services),
            "tunnels": running,
        })


# ---------------------------------------------------------
# Startup / Shutdown
# ---------------------------------------------------------

def startup():
    global services

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    print(
        "=========================================",
        flush=True,
    )

    print(
        " Xrob Port Publisher",
        flush=True,
    )

    print(
        "=========================================",
        flush=True,
    )

    print(
        f"[app] Loaded {len(services)} service(s)",
        flush=True,
    )

    for service in services:
        if service.get("enabled", True):
            print(
                f"[app] Restoring: "
                f"{service['name']} → "
                f"{service['target']}",
                flush=True,
            )

            start_tunnel(service)

        else:
            print(
                f"[app] Skipping disabled service: "
                f"{service['name']}",
                flush=True,
            )


def shutdown(*_):
    print(
        "[app] Shutting down tunnels...",
        flush=True,
    )

    with lock:
        ids = list(tunnels.keys())

    for sid in ids:
        stop_tunnel(sid)


if __name__ == "__main__":
    signal.signal(
        signal.SIGTERM,
        shutdown,
    )

    signal.signal(
        signal.SIGINT,
        shutdown,
    )

    startup()

    app.run(
        host="0.0.0.0",
        port=PORT,
        threaded=True,
    )
