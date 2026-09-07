import json
import os
import re
import signal
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory

APP_DIR = Path("/app")
DATA_DIR = Path("/data")
CONFIG_FILE = DATA_DIR / "services.json"
CLOUDFLARED = APP_DIR / "cloudflared"
PORT = 8099

app = Flask(__name__, static_folder=str(APP_DIR / "static"), static_url_path="")

lock = threading.RLock()
tunnels = {}

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.I)


def load_services():
    if not CONFIG_FILE.exists():
        return []
    try:
        data = json.loads(CONFIG_FILE.read_text())
        if isinstance(data, list):
            return data
    except Exception as exc:
        print(f"[config] Failed to read services.json: {exc}", flush=True)
    return []


def save_services(services):
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(services, indent=2))
    tmp.replace(CONFIG_FILE)


services = load_services()


def normalize_target(target):
    target = (target or "").strip()
    parsed = urllib.parse.urlparse(target)

    if parsed.scheme not in ("http", "https"):
        raise ValueError("Target must start with http:// or https://")

    if not parsed.hostname:
        raise ValueError("Target must contain a hostname or IP address")

    if parsed.username or parsed.password:
        raise ValueError("Username/password in target URLs are not allowed")

    if any(c in target for c in "\r\n\t"):
        raise ValueError("Invalid target URL")

    return target


def service_public(s):
    tid = s["id"]
    with lock:
        t = tunnels.get(tid)
        if not t:
            return {
                **s,
                "status": "stopped",
                "url": "",
                "error": "",
            }

        return {
            **s,
            "status": "running" if t["process"].poll() is None and t.get("url") else "starting",
            "url": t.get("url", ""),
            "error": t.get("error", ""),
        }


def start_tunnel(service):
    sid = service["id"]

    with lock:
        existing = tunnels.get(sid)
        if existing and existing["process"].poll() is None:
            return

        target = service["target"]

        cmd = [
            str(CLOUDFLARED),
            "tunnel",
            "--no-autoupdate",
            "--url",
            target,
        ]

        print(f"[tunnel:{sid}] Starting: {target}", flush=True)

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        tunnels[sid] = {
            "process": proc,
            "url": "",
            "error": "",
            "started": time.time(),
        }

    thread = threading.Thread(
        target=read_tunnel_output,
        args=(sid, proc),
        daemon=True,
    )
    thread.start()


def read_tunnel_output(sid, proc):
    try:
        for line in proc.stdout:
            line = line.rstrip()
            print(f"[tunnel:{sid}] {line}", flush=True)

            match = URL_RE.search(line)
            if match:
                url = match.group(0)
                with lock:
                    if sid in tunnels:
                        tunnels[sid]["url"] = url
                        tunnels[sid]["error"] = ""

                notify_url(sid, url)

    except Exception as exc:
        print(f"[tunnel:{sid}] Reader error: {exc}", flush=True)

    finally:
        code = proc.poll()
        with lock:
            if sid in tunnels:
                if code not in (None, 0):
                    tunnels[sid]["error"] = f"cloudflared exited with code {code}"
                tunnels[sid]["process"] = proc


def stop_tunnel(sid):
    with lock:
        t = tunnels.get(sid)

    if not t:
        return

    proc = t["process"]

    if proc.poll() is None:
        print(f"[tunnel:{sid}] Stopping", flush=True)
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    with lock:
        tunnels.pop(sid, None)


def notify_url(sid, url):
    service = next((x for x in services if x["id"] == sid), None)
    if not service:
        return

    try:
        options_file = Path("/data/options.json")
        if not options_file.exists():
            return

        options = json.loads(options_file.read_text())
        token = options.get("telegram_bot_token", "")
        chat_id = options.get("telegram_chat_id", "")

        if not token or not chat_id:
            return

        import urllib.request
        endpoint = f"https://api.telegram.org/bot{token}/sendMessage"

        text = f"🌐 {service['name']} is public\n{url}"

        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": text,
        }).encode()

        req = urllib.request.Request(endpoint, data=data, method="POST")
        urllib.request.urlopen(req, timeout=10).read()

        print(f"[telegram] Notification sent for {service['name']}", flush=True)

    except Exception as exc:
        print(f"[telegram] Notification failed: {exc}", flush=True)


def restart_all():
    current = list(services)

    for service in current:
        stop_tunnel(service["id"])

    time.sleep(1)

    for service in current:
        start_tunnel(service)


@app.get("/")
def index():
    return send_from_directory(APP_DIR / "static", "index.html")


@app.get("/api/services")
def api_services():
    with lock:
        result = [service_public(s) for s in services]
    return jsonify(result)


@app.post("/api/services")
def api_add_service():
    global services

    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    target = data.get("target", "")

    if not name:
        return jsonify({"error": "Service name is required"}), 400

    if len(name) > 100:
        return jsonify({"error": "Service name is too long"}), 400

    try:
        target = normalize_target(target)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    sid = os.urandom(6).hex()

    service = {
        "id": sid,
        "name": name,
        "target": target,
        "enabled": bool(data.get("enabled", True)),
    }

    with lock:
        services.append(service)
        save_services(services)

    if service["enabled"]:
        start_tunnel(service)

    return jsonify(service_public(service)), 201


@app.put("/api/services/<sid>")
def api_update_service(sid):
    global services

    data = request.get_json(silent=True) or {}

    with lock:
        service = next((x for x in services if x["id"] == sid), None)

    if not service:
        return jsonify({"error": "Service not found"}), 404

    name = str(data.get("name", service["name"])).strip()
    target = data.get("target", service["target"])
    enabled = bool(data.get("enabled", service.get("enabled", True)))

    if not name:
        return jsonify({"error": "Service name is required"}), 400

    try:
        target = normalize_target(target)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    stop_tunnel(sid)

    with lock:
        service["name"] = name
        service["target"] = target
        service["enabled"] = enabled
        save_services(services)

    if enabled:
        start_tunnel(service)

    return jsonify(service_public(service))


@app.delete("/api/services/<sid>")
def api_delete_service(sid):
    global services

    stop_tunnel(sid)

    with lock:
        services = [x for x in services if x["id"] != sid]
        save_services(services)

    return jsonify({"ok": True})


@app.post("/api/services/<sid>/restart")
def api_restart_service(sid):
    with lock:
        service = next((x for x in services if x["id"] == sid), None)

    if not service:
        return jsonify({"error": "Service not found"}), 404

    stop_tunnel(sid)
    start_tunnel(service)

    return jsonify(service_public(service))


@app.post("/api/services/<sid>/stop")
def api_stop_service(sid):
    with lock:
        service = next((x for x in services if x["id"] == sid), None)

    if not service:
        return jsonify({"error": "Service not found"}), 404

    stop_tunnel(sid)

    return jsonify(service_public({
        **service,
        "enabled": False,
    }))


@app.get("/api/health")
def api_health():
    return jsonify({
        "ok": True,
        "services": len(services),
        "tunnels": len(tunnels),
    })


def startup():
    print(f"[app] Loaded {len(services)} service(s)", flush=True)

    for service in services:
        if service.get("enabled", True):
            start_tunnel(service)


def shutdown(*_):
    print("[app] Shutting down tunnels...", flush=True)

    for sid in list(tunnels):
        stop_tunnel(sid)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    startup()

    app.run(
        host="0.0.0.0",
        port=PORT,
        threaded=True,
    )
