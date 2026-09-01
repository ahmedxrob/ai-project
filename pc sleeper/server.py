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
    "sshpass",
    "-p",
    PC_PASS,
    "ssh",
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "ConnectTimeout=10",
    f"{PC_USER}@{PC_IP}",
    'powershell.exe -Command "rundll32.exe powrprof.dll,SetSuspendState 0,1,0"',
]


def send_json(handler, status, payload):
    data = json.dumps(payload).encode("utf-8")

    handler.send_response(status)
    handler.send_header(
        "Content-Type",
        "application/json; charset=utf-8",
    )
    handler.send_header(
        "Content-Length",
        str(len(data)),
    )
    handler.end_headers()

    handler.wfile.write(data)


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(
            f"{self.client_address[0]} - {fmt % args}",
            flush=True,
        )

    def authorized(self):
        token = self.headers.get("X-API-Token", "")
        return bool(API_TOKEN) and token == API_TOKEN

    def do_GET(self):

        if self.path == "/health":
            send_json(
                self,
                200,
                {
                    "ok": True,
                    "service": "xrob_pc_sleep",
                },
            )
            return

        if self.path != "/sleep":
            send_json(
                self,
                404,
                {
                    "ok": False,
                    "error": "not_found",
                },
            )
            return

        if not self.authorized():
            send_json(
                self,
                401,
                {
                    "ok": False,
                    "error": "unauthorized",
                },
            )
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
            send_json(
                self,
                504,
                {
                    "ok": False,
                    "error": "ssh_timeout",
                },
            )
            return

        except Exception as exc:
            send_json(
                self,
                500,
                {
                    "ok": False,
                    "error": str(exc),
                },
            )
            return

        if result.returncode == 0:
            send_json(
                self,
                200,
                {
                    "ok": True,
                    "message": "Sleep command sent",
                },
            )
            return

        send_json(
            self,
            502,
            {
                "ok": False,
                "error": "ssh_failed",
                "returncode": result.returncode,
                "stderr": result.stderr[-2000:],
                "stdout": result.stdout[-2000:],
            },
        )


if __name__ == "__main__":

    if not PC_IP:
        raise SystemExit("Missing pc_ip")

    if not PC_USER:
        raise SystemExit("Missing username")

    if not PC_PASS:
        raise SystemExit("Missing password")

    if not API_TOKEN:
        raise SystemExit("Missing api_token")

    print(
        f"Xrob PC Sleep starting",
        flush=True,
    )

    print(
        f"Target PC: {PC_USER}@{PC_IP}",
        flush=True,
    )

    print(
        f"Listening on {HOST}:{PORT}",
        flush=True,
    )

    server = HTTPServer(
        (HOST, PORT),
        Handler,
    )

    server.serve_forever()
