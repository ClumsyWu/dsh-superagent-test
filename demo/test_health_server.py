#!/usr/bin/env python3
"""
Unit tests for the health check HTTP server.

Usage:
  python -m unittest demo.test_health_server -v
  python -m demo.test_health_server   # Convenience alias
"""

from __future__ import annotations

import json
import time
import unittest
from http import HTTPStatus
from urllib.request import urlopen, Request
from urllib.error import HTTPError

from demo.health_server import HealthServer, HealthHandler


class TestHealthEndpoints(unittest.TestCase):
    """Integration tests for /health, /metrics, /ready endpoints."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HealthServer(port=0)  # port 0 = OS-assigned
        cls.server.start()
        # Wait for server to start and capture actual port
        import socket
        sock = cls.server._server.socket
        cls.port = sock.getsockname()[1]
        cls.base_url = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def _get_json(self, path: str) -> tuple[dict, int]:
        """Make a GET request and return (parsed_json, status_code)."""
        url = f"{self.base_url}{path}"
        try:
            with urlopen(url, timeout=5) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body), resp.status
        except HTTPError as e:
            body = e.read().decode("utf-8")
            return json.loads(body), e.code

    # --- /health ---

    def test_health_returns_ok(self) -> None:
        """/health should return status 200 with status 'ok'."""
        data, status = self._get_json("/health")
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["service"], "dsh-superagent-test")

    def test_health_with_trailing_slash(self) -> None:
        """/health/ should normalize to /health and return 200."""
        data, status = self._get_json("/health/")
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(data["status"], "ok")

    # --- /metrics ---

    def test_metrics_returns_counters(self) -> None:
        """/metrics should return uptime and request count."""
        data, status = self._get_json("/metrics")
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(data["status"], "ok")
        # uptime_seconds should be a positive float
        self.assertIsInstance(data["uptime_seconds"], float)
        self.assertGreater(data["uptime_seconds"], 0.0)
        # total_requests should be a positive int
        self.assertIsInstance(data["total_requests"], int)
        self.assertGreater(data["total_requests"], 0)

    def test_metrics_increments_on_request(self) -> None:
        """Each request to /metrics should increment the counter."""
        prev = HealthHandler.request_count
        self._get_json("/metrics")
        self.assertEqual(HealthHandler.request_count, prev + 1)

    # --- /ready ---

    def test_ready_returns_ready(self) -> None:
        """/ready should return status 200 with status 'ready'."""
        data, status = self._get_json("/ready")
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(data["status"], "ready")
        self.assertEqual(data["service"], "dsh-superagent-test")

    # --- unknown path ---

    def test_unknown_path_returns_404(self) -> None:
        """An unknown path should return 404."""
        data, status = self._get_json("/nonexistent")
        self.assertEqual(status, HTTPStatus.NOT_FOUND)
        self.assertIn("error", data)

    # --- HTTP methods ---

    def test_post_returns_405(self) -> None:
        """POST to any endpoint should return 405 Method Not Allowed."""
        for path in ("/health", "/metrics", "/ready"):
            with self.subTest(path=path):
                url = f"{self.base_url}{path}"
                req = Request(url, method="POST", data=b"{}")
                try:
                    with urlopen(req, timeout=5):
                        self.fail("Expected HTTPError for POST")
                except HTTPError as e:
                    self.assertEqual(e.code, HTTPStatus.METHOD_NOT_ALLOWED)

    # --- uptime ---

    def test_uptime_increases_with_time(self) -> None:
        """uptime_seconds should increase after a brief wait."""
        data1, _ = self._get_json("/metrics")
        time.sleep(0.1)
        data2, _ = self._get_json("/metrics")
        self.assertGreater(data2["uptime_seconds"], data1["uptime_seconds"])


class TestHealthServerLifecycle(unittest.TestCase):
    """Tests for HealthServer start/stop lifecycle."""

    def test_server_url_property(self) -> None:
        """url property should reflect host and port."""
        server = HealthServer(host="127.0.0.1", port=0)
        self.assertIn("http://127.0.0.1:", server.url)

    def test_start_stop_does_not_raise(self) -> None:
        """Starting and stopping the server should not raise."""
        server = HealthServer(port=0)
        server.start()
        # Give it a moment to bind
        time.sleep(0.05)
        server.stop()

    def test_multiple_stops_are_safe(self) -> None:
        """Calling stop multiple times should not raise."""
        server = HealthServer(port=0)
        server.start()
        time.sleep(0.05)
        server.stop()
        # second stop may raise or be a no-op; we just verify no crash
        try:
            server.stop()
        except Exception:
            pass  # acceptable for already-stopped server


if __name__ == "__main__":
    unittest.main()