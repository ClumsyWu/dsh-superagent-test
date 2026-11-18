#!/usr/bin/env python3
"""
Unit tests for the HTTP request tool.

The tests run against a local HTTP server bound to an ephemeral port, so they
need no network access and no extra dependencies.

Usage:
  python -m unittest demo.test_http_tool -v
  python -m demo.test_http_tool   # Convenience alias
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
import unittest
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

from demo.http_tool import (
    ALLOWED_METHODS,
    HttpTool,
    HttpToolError,
    TOOL_DESCRIPTION,
    TOOL_NAME,
    http_request,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class _TestHandler(BaseHTTPRequestHandler):
    """Small HTTP server exercising the features of the HTTP tool."""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Keep test output clean."""

    # --- helpers ---

    def _send(
        self,
        status: int,
        body: bytes = b"",
        content_type: str = "application/json; charset=utf-8",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD" and body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _send_json(self, status: int, data: dict) -> None:
        self._send(status, json.dumps(data).encode("utf-8"))

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _request_headers(self) -> dict[str, str]:
        return {name.lower(): value for name, value in self.headers.items()}

    # --- request handling ---

    def _handle(self) -> None:
        if self.path.startswith("/echo"):
            body = self._read_body()
            self._send_json(
                HTTPStatus.OK,
                {
                    "method": self.command,
                    "body": body.decode("utf-8", errors="replace"),
                    "content_type": self.headers.get("Content-Type"),
                },
            )
            return

        if self.path.startswith("/get"):
            self._send_json(HTTPStatus.OK, {"method": self.command, "path": self.path})
            return

        if self.path.startswith("/headers"):
            self._send_json(HTTPStatus.OK, self._request_headers())
            return

        if self.path.startswith("/rawheaders"):
            self._send_json(
                HTTPStatus.OK,
                {"headers": [[name, value] for name, value in self.headers.items()]},
            )
            return

        if self.path.startswith("/redirect"):
            self._send(
                HTTPStatus.FOUND,
                extra_headers={"Location": "/get"},
            )
            return

        if self.path.startswith("/large"):
            self._send(HTTPStatus.OK, b"x" * 5000, content_type="text/plain")
            return

        if self.path.startswith("/utf8"):
            self._send(
                HTTPStatus.OK,
                "你好，世界".encode("utf-8"),
                content_type="text/plain; charset=utf-8",
            )
            return

        if self.path.startswith("/slow"):
            time.sleep(0.6)
            self._send_json(HTTPStatus.OK, {"status": "slow"})
            return

        if self.path.startswith("/error"):
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "boom"})
            return

        if self.path.startswith("/empty"):
            self._send(HTTPStatus.NO_CONTENT)
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": f"not found: {self.path}"})

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle
    do_HEAD = _handle
    do_OPTIONS = _handle


class _QuietServer(ThreadingHTTPServer):
    """Threading server that does not print handler tracebacks."""

    daemon_threads = True

    def handle_error(self, request: object, client_address: object) -> None:
        """Suppress noise from clients that time out mid-response."""


class TestHttpRequest(unittest.TestCase):
    """End-to-end tests for :func:`http_request`."""

    server: ClassVar[_QuietServer]

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _QuietServer(("127.0.0.1", 0), _TestHandler)
        cls.port = cls.server.server_address[1]
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    # --- successful exchanges ---

    def test_get_returns_status_body_and_headers(self) -> None:
        result = http_request(f"{self.base_url}/get?x=1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], HTTPStatus.OK)
        self.assertEqual(result["reason"], "OK")
        self.assertEqual(result["method"], "GET")
        self.assertIn('"method": "GET"', result["body"])
        self.assertIn('"/get?x=1"', result["body"])
        self.assertIn("Content-Type", result["headers"])
        self.assertFalse(result["truncated"])
        self.assertIsInstance(result["elapsed_ms"], float)

    def test_custom_headers_are_sent(self) -> None:
        result = http_request(
            f"{self.base_url}/headers",
            headers={"X-Test": "abc", "Accept": "text/plain"},
        )
        self.assertTrue(result["ok"])
        received = json.loads(result["body"])
        self.assertEqual(received["x-test"], "abc")
        self.assertEqual(received["accept"], "text/plain")
        self.assertTrue(received["user-agent"].startswith("dsh-superagent-http-tool"))

    def test_header_overrides_are_case_insensitive(self) -> None:
        result = http_request(
            f"{self.base_url}/rawheaders",
            headers={"User-Agent": "custom/1.0"},
        )
        self.assertTrue(result["ok"])
        pairs = json.loads(result["body"])["headers"]
        agents = [value for name, value in pairs if name.lower() == "user-agent"]
        encodings = [value for name, value in pairs if name.lower() == "accept-encoding"]
        self.assertEqual(agents, ["custom/1.0"])
        self.assertEqual(encodings, ["identity"])

    def test_post_json_body(self) -> None:
        result = http_request(
            f"{self.base_url}/echo",
            method="POST",
            json_body={"name": "dsh"},
        )
        self.assertTrue(result["ok"])
        received = json.loads(result["body"])
        self.assertEqual(received["method"], "POST")
        self.assertEqual(received["body"], '{"name": "dsh"}')
        self.assertEqual(received["content_type"], "application/json")

    def test_post_raw_body(self) -> None:
        result = http_request(
            f"{self.base_url}/echo",
            method="post",
            body="raw=1",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertTrue(result["ok"])
        received = json.loads(result["body"])
        self.assertEqual(received["method"], "POST")
        self.assertEqual(received["body"], "raw=1")
        self.assertEqual(
            received["content_type"],
            "application/x-www-form-urlencoded",
        )

    def test_http_error_status_is_returned_not_raised(self) -> None:
        result = http_request(f"{self.base_url}/error")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], HTTPStatus.INTERNAL_SERVER_ERROR)
        self.assertIn("boom", result["body"])

    def test_unknown_path_returns_404(self) -> None:
        result = http_request(f"{self.base_url}/missing")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], HTTPStatus.NOT_FOUND)
        self.assertIn("not found", result["body"])

    def test_redirect_is_followed(self) -> None:
        result = http_request(f"{self.base_url}/redirect")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], HTTPStatus.OK)
        self.assertTrue(result["url"].endswith("/get"))

    def test_body_is_truncated_at_limit(self) -> None:
        result = http_request(f"{self.base_url}/large", max_body_bytes=100)
        self.assertTrue(result["ok"])
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["body"]), 100)

    def test_utf8_body_is_decoded(self) -> None:
        result = http_request(f"{self.base_url}/utf8")
        self.assertTrue(result["ok"])
        self.assertEqual(result["body"], "你好，世界")

    def test_head_request_has_no_body(self) -> None:
        result = http_request(f"{self.base_url}/get", method="HEAD")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], HTTPStatus.OK)
        self.assertEqual(result["body"], "")

    def test_no_content_response(self) -> None:
        result = http_request(f"{self.base_url}/empty")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], HTTPStatus.NO_CONTENT)
        self.assertEqual(result["body"], "")

    # --- transport failures ---

    def test_timeout_returns_structured_error(self) -> None:
        result = http_request(f"{self.base_url}/slow", timeout=0.15)
        self.assertFalse(result["ok"])
        self.assertIn("error", result)
        self.assertLess(result["elapsed_ms"], 5000)

    def test_connection_refused_returns_structured_error(self) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            unused_port = sock.getsockname()[1]
        result = http_request(f"http://127.0.0.1:{unused_port}/", timeout=0.5)
        self.assertFalse(result["ok"])
        self.assertIn("error", result)

    # --- argument validation ---

    def test_invalid_url_scheme_is_rejected(self) -> None:
        with self.assertRaises(HttpToolError):
            http_request("file:///etc/passwd")

    def test_url_without_host_is_rejected(self) -> None:
        with self.assertRaises(HttpToolError):
            http_request("http:///path")

    def test_unsupported_method_is_rejected(self) -> None:
        with self.assertRaises(HttpToolError):
            http_request("http://example.com", method="TRACE")

    def test_body_and_json_are_mutually_exclusive(self) -> None:
        with self.assertRaises(HttpToolError):
            http_request(
                "http://example.com",
                method="POST",
                body="raw",
                json_body={"a": 1},
            )

    def test_invalid_headers_are_rejected(self) -> None:
        with self.assertRaises(HttpToolError):
            http_request("http://example.com", headers={"X-Test": 1})  # type: ignore[dict-item]

    def test_invalid_timeout_is_rejected(self) -> None:
        for timeout in (0, -1, float("nan"), float("inf")):
            with self.subTest(timeout=timeout):
                with self.assertRaises(HttpToolError):
                    http_request("http://example.com", timeout=timeout)

    def test_invalid_max_body_bytes_is_rejected(self) -> None:
        with self.assertRaises(HttpToolError):
            http_request("http://example.com", max_body_bytes=-1)


class TestHttpTool(unittest.TestCase):
    """Tests for the agent-facing :class:`HttpTool` wrapper."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _QuietServer(("127.0.0.1", 0), _TestHandler)
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_metadata_describes_the_tool(self) -> None:
        tool = HttpTool()
        self.assertEqual(tool.name, TOOL_NAME)
        self.assertEqual(tool.description, TOOL_DESCRIPTION)
        self.assertEqual(tool.input_schema["required"], ["url"])
        self.assertEqual(tool.input_schema["properties"]["method"]["enum"], list(ALLOWED_METHODS))

    def test_invoke_returns_tool_result(self) -> None:
        result = HttpTool().invoke({"url": f"{self.base_url}/get"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], HTTPStatus.OK)

    def test_invoke_missing_url_returns_error(self) -> None:
        result = HttpTool().invoke({})
        self.assertFalse(result["ok"])
        self.assertIn("invalid_arguments", result["error"])

    def test_invoke_unsupported_method_returns_error(self) -> None:
        result = HttpTool().invoke({"url": f"{self.base_url}/get", "method": "TRACE"})
        self.assertFalse(result["ok"])
        self.assertIn("invalid_arguments", result["error"])

    def test_invoke_non_mapping_returns_error(self) -> None:
        result = HttpTool().invoke("not-an-object")  # type: ignore[arg-type]
        self.assertFalse(result["ok"])
        self.assertIn("invalid_arguments", result["error"])

    def test_invoke_unknown_argument_returns_error(self) -> None:
        result = HttpTool().invoke({"url": f"{self.base_url}/get", "timeout": 1})
        self.assertFalse(result["ok"])
        self.assertIn("unknown argument(s): timeout", result["error"])

    def test_invoke_json_and_body_conflict_returns_error(self) -> None:
        result = HttpTool().invoke(
            {
                "url": f"{self.base_url}/echo",
                "method": "POST",
                "body": "raw",
                "json": {"a": 1},
            }
        )
        self.assertFalse(result["ok"])
        self.assertIn("mutually exclusive", result["error"])


class TestHttpToolCli(unittest.TestCase):
    """Smoke tests for the command-line entry point."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _QuietServer(("127.0.0.1", 0), _TestHandler)
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "demo.http_tool", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_cli_get_prints_json_result(self) -> None:
        completed = self._run(f"{self.base_url}/get")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], HTTPStatus.OK)

    def test_cli_post_json_and_headers(self) -> None:
        completed = self._run(
            f"{self.base_url}/echo",
            "-X",
            "POST",
            "--json",
            '{"name": "dsh"}',
            "-H",
            "X-Test: abc",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(result["ok"])
        received = json.loads(result["body"])
        self.assertEqual(received["method"], "POST")
        self.assertEqual(received["body"], '{"name": "dsh"}')
        self.assertEqual(received["content_type"], "application/json")

    def test_cli_invalid_url_exits_nonzero(self) -> None:
        completed = self._run("ftp://example.com")
        self.assertNotEqual(completed.returncode, 0)
        result = json.loads(completed.stdout)
        self.assertFalse(result["ok"])
        self.assertIn("invalid_arguments", result["error"])

    def test_cli_rejects_conflicting_bodies(self) -> None:
        completed = self._run(
            f"{self.base_url}/echo",
            "-X",
            "POST",
            "-d",
            "raw",
            "--json",
            '{"a": 1}',
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("mutually exclusive", completed.stderr)


if __name__ == "__main__":
    unittest.main()
