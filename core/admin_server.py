"""Pod-local admin endpoint for the rollout machinery.

Listens on SPEAKING_ADMIN_PORT (7015), which no Service or ingress exposes:
only the preStop hook and the rollout janitor reach it, through the pod IP.
That isolation is its only access control.

GET  /status     drain flag, live bot count, pod name
GET  /live-bots  the count alone, as text
POST /drain      stop taking new bots
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core import drain
from meetingbaas_pipecat.utils.logger import logger

DEFAULT_ADMIN_PORT = 7015


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json")

    def do_GET(self) -> None:
        if self.path == "/status":
            self._json(200, drain.status())
        elif self.path == "/live-bots":
            self._send(200, str(drain.live_bot_count()).encode(), "text/plain")
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path == "/drain":
            drain.start_draining()
            logger.info("Draining: no new bots on this pod, live ones run to the end")
            self._json(200, drain.status())
        else:
            self._json(404, {"error": "not found"})


def admin_port() -> int:
    """0 disables the server."""
    try:
        return int(os.getenv("SPEAKING_ADMIN_PORT", str(DEFAULT_ADMIN_PORT)))
    except ValueError:
        return DEFAULT_ADMIN_PORT


def start_admin_server(port: int, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    """Serve in a daemon thread; port 0 picks a free one (tests)."""
    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="speaking-admin", daemon=True).start()
    return server


def start_admin_server_from_env() -> ThreadingHTTPServer | None:
    port = admin_port()
    if port <= 0:
        return None
    try:
        server = start_admin_server(port)
    except OSError as e:
        logger.warning(f"Admin server not started on port {port}: {e}")
        return None
    logger.info(f"Admin server listening on port {port}")
    return server
