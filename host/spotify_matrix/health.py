"""Tiny stdlib HTTP health endpoint for k8s probes.

This app is a poll loop, not a server, so the only reason to listen on a port at
all is so the cluster can tell "up and polling" from "wedged", and can mark the
pod ready the moment the first poll lands instead of guessing at a delay.
Deliberately stdlib-only: no framework belongs in an image this small.

Liveness is intentionally forgiving about *what* a tick did. A tick that failed
to reach Spotify or the panel still counts as alive; those are outages we log and
retry, not reasons to restart the process. Liveness only fails if the loop itself
stops turning.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger(__name__)

DEFAULT_PORT = 8080


class HealthState:
    """Thread-safe view of the poll loop, shared with the probe handler."""

    def __init__(self, stale_after: float = 60.0):
        self._lock = threading.Lock()
        self._stale_after = stale_after
        self._last_tick: float | None = None

    def configure(self, stale_after: float) -> None:
        """Set how long without a tick counts as wedged (known after env parsing)."""
        with self._lock:
            self._stale_after = stale_after

    def tick(self) -> None:
        """Record that the poll loop completed an iteration."""
        with self._lock:
            self._last_tick = time.monotonic()

    def live(self) -> tuple[bool, str]:
        with self._lock:
            last, stale_after = self._last_tick, self._stale_after
        if last is None:
            # Still starting. Not dead, so don't hand liveness a reason to
            # restart us; the startup probe is what gates traffic instead.
            return True, "starting"
        age = time.monotonic() - last
        if age > stale_after:
            return False, f"no poll for {age:.0f}s (limit {stale_after:.0f}s)"
        return True, f"last poll {age:.0f}s ago"

    def ready(self) -> tuple[bool, str]:
        with self._lock:
            last = self._last_tick
        return (True, "polling") if last is not None else (False, "starting")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: HealthState

    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler naming)
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/healthz":
            ok, detail = self.state.live()
        elif path == "/readyz":
            ok, detail = self.state.ready()
        else:
            self._respond(404, {"error": "not found"})
            return
        self._respond(200 if ok else 503, {"ok": ok, "detail": detail})

    def _respond(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # Probes hit this every few seconds; keep them out of the normal log.
        log.debug("health: " + fmt, *args)


def start(state: HealthState, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """Serve /healthz and /readyz on a daemon thread and return the server."""
    handler = type("Handler", (_Handler,), {"state": state})
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="health", daemon=True).start()
    log.info("health endpoints on :%d (/healthz, /readyz)", port)
    return server
