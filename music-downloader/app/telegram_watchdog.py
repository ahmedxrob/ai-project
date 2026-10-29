#!/usr/bin/env python3
"""Independent Xrob Music reachability watchdog.

Run this in a separate, always-on process/host. It intentionally does not depend
on Xrob Music's process or its private settings file, so it can alert when the
music app/container itself is stopped.
"""
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

HEALTH_URL = os.getenv("XROB_HEALTH_URL", "http://homeassistant.local:8100/api/health").strip()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
INTERVAL = max(10, min(300, int(os.getenv("XROB_WATCHDOG_INTERVAL_SECONDS", "30"))))
FAILURES_TO_ALERT = max(1, min(10, int(os.getenv("XROB_WATCHDOG_FAILURES", "3"))))
DAILY_REPORT_HOUR = max(0, min(23, int(os.getenv("XROB_WATCHDOG_DAILY_HOUR", "9"))))
STATE_FILE = Path(os.getenv("XROB_WATCHDOG_STATE_FILE", "./xrob-watchdog-state.json"))


def send_message(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram watchdog is not configured: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.", flush=True)
        return False
    endpoint = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    body = json.dumps({"chat_id": CHAT_ID, "text": message[:3900], "disable_web_page_preview": True}).encode("utf-8")
    request = urllib.request.Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read(128 * 1024).decode("utf-8", errors="replace"))
        if response.status == 200 and payload.get("ok") is True:
            return True
        print("Telegram rejected a watchdog notification:", str(payload.get("description") or "unknown error")[:200], flush=True)
    except Exception as exc:
        # Some urllib errors include the requested URL; redact the token before logging.
        detail = str(exc).replace(BOT_TOKEN, "[redacted]") if BOT_TOKEN else str(exc)
        print("Telegram notification failed:", type(exc).__name__, detail[:200], flush=True)
    return False


def check_health():
    request = urllib.request.Request(HEALTH_URL, headers={"Accept": "application/json", "User-Agent": "Xrob-Music-Watchdog/1.0"})
    with urllib.request.urlopen(request, timeout=8) as response:
        if response.status != 200:
            raise RuntimeError(f"health endpoint returned HTTP {response.status}")
        payload = json.loads(response.read(128 * 1024).decode("utf-8", errors="replace"))
    if payload.get("server") != "Xrob Music" or payload.get("status") != "ok":
        raise RuntimeError("health endpoint returned an unexpected response")
    return payload


def load_state():
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, STATE_FILE)


def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID before starting the watchdog.")
    state = load_state()
    failures = int(state.get("consecutive_failures") or 0)
    was_down = bool(state.get("down", False))
    started_notice = False
    # Make the first due report attempt immediate, including after a machine reboot.
    last_daily_attempt_monotonic = float("-inf")
    while True:
        now = datetime.now()
        success = False
        details = ""
        try:
            health = check_health()
            success = True
            storage = health.get("storage") or {}
            if not storage.get("mounted"):
                details = "The app is reachable, but the media storage mount is unavailable."
            elif not storage.get("writable"):
                details = "The app is reachable, but the media storage is not writable."
        except Exception as exc:
            failures += 1
            details = f"{type(exc).__name__}: {str(exc)[:200]}"

        if success:
            failures = 0
            if was_down:
                if send_message("✅ Xrob Music is reachable again.\n" + (details or "The health endpoint is responding normally.")):
                    was_down = False
            elif not started_notice:
                started_notice = send_message("🟢 Xrob Music external watchdog is running and the app is reachable.\nHealth URL: " + HEALTH_URL)
            if details and state.get("storage_warning") != details:
                if send_message("🚨 Xrob Music storage warning\n" + details):
                    state["storage_warning"] = details
            elif not details and state.get("storage_warning"):
                if send_message("✅ Xrob Music storage warning cleared. The media mount reports healthy again."):
                    state["storage_warning"] = ""
            elif not details:
                state["storage_warning"] = ""
            state["last_success_at"] = now.isoformat(timespec="seconds")
        else:
            if failures >= FAILURES_TO_ALERT and not was_down:
                if send_message(f"🚨 Xrob Music appears to be DOWN.\nHealth check failed {failures} times.\nURL: {HEALTH_URL}\nLast error: {details}"):
                    was_down = True
            state["last_failure_at"] = now.isoformat(timespec="seconds")
            state["last_failure"] = details

        date_key = now.date().isoformat()
        if (now.hour >= DAILY_REPORT_HOUR and state.get("last_daily_report_date") != date_key
                and time.monotonic() - last_daily_attempt_monotonic >= 3600):
            if success and not details:
                status_text = "✅ App is reachable and its health endpoint reports mounted, writable media storage."
            elif success:
                status_text = "⚠️ App is reachable, but it reports a storage problem: " + details
            else:
                status_text = f"🚨 App appears down or unreachable after {failures} failed checks: {details}"
            delivered = send_message(f"📊 Xrob Music external watchdog daily report — {date_key}\n{status_text}\nHealth URL: {HEALTH_URL}")
            last_daily_attempt_monotonic = time.monotonic()
            if delivered:
                state["last_daily_report_date"] = date_key

        state["consecutive_failures"] = failures
        state["down"] = was_down
        state["last_check_at"] = now.isoformat(timespec="seconds")
        try:
            save_state(state)
        except Exception as exc:
            print("Could not save watchdog state:", type(exc).__name__, str(exc)[:200], flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
