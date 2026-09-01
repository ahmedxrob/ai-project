#!/usr/bin/env python3
import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer

PC_IP = os.environ.get("PC_IP", "")
PC_USER = os.environ.get("PC_USER", "")
PC_PASS = os.environ.get("PC_PASS", "")
API_TOKEN = os.environ.get("API_TOKEN", "")
HOST = "0.0.0.0"
PORT = 8099

SSH_COMMAND = [
    "sshpass", "-p", PC_PASS,
    "ssh",
    "-o", "StrictHostKeyChecking=no",
    "-o", "ConnectTimeout=10",
    f"{PC_USER}@{PC_IP}",
    'powershell.exe -Command "rundll32.exe powrprof.dll,SetSuspendState 0,1,0"',
]


def json_response(handler, status, payload):
    data = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class Handler(BaseHTTPRequestHandler):
    server_version = "XrobPCSleep/1.0"

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.client_address[0], fmt % args), flush=True)

    def _authorized(self):
        return self.headers.get("X-API-Token", "") == API_TOKEN

    def do_GET(self):
        if self.path == "/health":
            json_response(self, 200, {"ok": True, "service": "xrob_pc_sleep"})
            return
        if self.path != "/sleep":
            json_response(self, 404, {"ok": False, "error": "not_found"})
            return
        if not self._authorized():
            json_response(self, 401, {"ok": False, "error": "unauthorized"})
            return

        try:
            result = subprocess.run(
                SSH_COMMAND,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except subprocess.TimeoutExpired:
            json_response(self, 504, {"ok": False, "error": "ssh_timeout"})
            return
        except Exception as exc:
            json_response(self, 500, {"ok": False, "error": str(exc)})
            return

        if result.returncode == 0:
            json_response(self, 200, {"ok": True, "message": "Sleep command sent"})
        else:
            json_response(
                self,
                502,
                {
                    "ok": False,
                    "error": "ssh_failed",
                    "returncode": result.returncode,
                    "stderr": result.stderr[-1000:],
                },
            )


if __name__ == "__main__":
    if not all([PC_IP, PC_USER, PC_PASS, API_TOKEN]):
        raise SystemExit("Missing required configuration")
    print(f"Listening on {HOST}:{PORT}", flush=True)
    HTTPServer((HOST, PORT), Handler).serve_forever()
