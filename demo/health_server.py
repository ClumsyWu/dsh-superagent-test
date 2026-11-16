#!/usr/bin/env python3
"""
Health check HTTP server for probe endpoints.

Provides /health, /metrics, and /ready endpoints for service health monitoring.
Uses Python's built-in http.server to avoid external dependencies.

Endpoints:
  GET /health  - Overall health status (always 200 when server is alive)
  GET /metrics - Basic operational metrics (request count, uptime, status)
  GET /ready   - Readiness check (200 when server accepts requests)

Usage:
  python -m demo.health_server [--port PORT]
"""

from __future__ import annotations

import argparse
import json
import time
import threading
from http import HTTPStatus
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import ClassVar

__all__ = ["HealthHandler", "HealthServer", "main"]


class HealthHandler(BaseHTTPRequestHandler):
    """HTTP request handler for health check endpoints."""

    # Shared server state
    server_start_time: ClassVar[float] = time.time()
    request_count: ClassVar[int] = 0
    _lock: ClassVar[threading.Lock] = threading.Lock()

    def _respond_json(self, data: dict, status: int = HTTPStatus.OK) -> None:
        """Send a JSON response with the given status code."""
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _increment_counter(self) -> None:
        """Thread-safe increment of request counter."""
        with self._lock:
            self.__class__.request_count += 1

    def _uptime_seconds(self) -> float:
        """Return server uptime in seconds."""
        return time.time() - self.__class__.server_start_time

    # --- Endpoint handlers ---

    def do_GET(self) -> None:  # noqa: N802
        """Route GET requests to the appropriate handler."""
        self._increment_counter()

        path = self.path.rstrip("/") or "/"
        handlers = {
            "/health": self._handle_health,
            "/metrics": self._handle_metrics,
            "/ready": self._handle_ready,
        }
        handler = handlers.get(path)
        if handler:
            handler()
        else:
            self._respond_json(
                {"error": f"Not found: {self.path}"},
                HTTPStatus.NOT_FOUND,
            )

    def _handle_health(self) -> None:
        """Return overall health status. Always 200 when server is alive."""
        self._respond_json({
            "status": "ok",
            "service": "dsh-superagent-test",
        })

    def _handle_metrics(self) -> None:
        """Return basic operational metrics."""
        self._respond_json({
            "uptime_seconds": round(self._uptime_seconds(), 2),
            "total_requests": self.__class__.request_count,
            "status": "ok",
        })

    def _handle_ready(self) -> None:
        """Return readiness status. 200 when the server accepts traffic."""
        self._respond_json({
            "status": "ready",
            "service": "dsh-superagent-test",
        })

    # --- Silence default logging ---

    # --- Handle unsupported methods ---

    def do_POST(self) -> None:  # noqa: N802
        """Reject POST with 405 Method Not Allowed."""
        self._respond_json(
            {"error": "Method not allowed"},
            HTTPStatus.METHOD_NOT_ALLOWED,
        )

    # --- Silence default logging ---

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Suppress default request logging; errors still go to stderr."""
        if args and args[0] in ("404", "500"):
            import sys
            sys.stderr.write(f"[{self.log_date_time_string()}] {format % args}\n")


class HealthServer:
    """A lightweight health check HTTP server.

    Serves /health, /metrics, and /ready on a configurable port.
    Runs in daemon thread mode by default for easy test integration.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8080) -> None:
        self.host = host
        self.port = port
        self._server = HTTPServer((host, port), HealthHandler)
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the server in a background daemon thread."""
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Shut down the server."""
        self._server.shutdown()

    @property
    def url(self) -> str:
        """Base URL of the running server."""
        return f"http://{self.host}:{self.port}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Health check HTTP server",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port to listen on (default: 8080)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind to (default: 127.0.0.1)",
    )
    args = parser.parse_args()

    server = HealthServer(host=args.host, port=args.port)
    try:
        print(f"🚀 Health server starting on {server.url}")
        print(f"   Endpoints: {server.url}/health")
        print(f"              {server.url}/metrics")
        print(f"              {server.url}/ready")
        print("   Press Ctrl+C to stop.")
        server.start()
        # Block on the main thread
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n👋 Shutting down...")
    finally:
        server.stop()
        print("✅ Server stopped.")


if __name__ == "__main__":
    main()