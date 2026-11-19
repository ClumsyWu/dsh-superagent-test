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

import base64
import gzip
import json
import socket
import ssl
import subprocess
import sys
import threading
import time
import unittest
import zlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, quote, urlsplit

from demo.http_tool import (
    ALLOWED_METHODS,
    DEFAULT_MAX_REDIRECTS,
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT,
    HttpTool,
    HttpToolError,
    TOOL_DESCRIPTION,
    TOOL_INPUT_SCHEMA,
    TOOL_NAME,
    _build_ssl_context,
    http_request,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

_COUNTERS: dict[str, int] = {}
_COUNTERS_LOCK = threading.Lock()


class _TestHandler(BaseHTTPRequestHandler):
    """Small HTTP server exercising the features of the HTTP tool."""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Keep test output clean."""

    # --- helpers ---

    def _send(
        self,
        status: int,
        body: bytes = b"",
        content_type: str | None = "application/json; charset=utf-8",
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> None:
        self.send_response(status)
        if content_type is not None:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in extra_headers or []:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD" and body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _send_json(self, status: int, data: object) -> None:
        self._send(status, json.dumps(data).encode("utf-8"))

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _request_headers(self) -> dict[str, str]:
        return {name.lower(): value for name, value in self.headers.items()}

    def _flaky(self, query: dict[str, list[str]]) -> None:
        self._read_body()  # Drain the request so the connection closes cleanly.
        tag = query.get("tag", [self.path])[0]
        failures = int(query.get("n", ["0"])[0])
        with _COUNTERS_LOCK:
            seen = _COUNTERS.get(tag, 0)
            _COUNTERS[tag] = seen + 1
        if seen < failures:
            self._send(
                HTTPStatus.SERVICE_UNAVAILABLE,
                b'{"retry": true}',
                extra_headers=[("Retry-After", "0")],
            )
        else:
            self._send_json(HTTPStatus.OK, {"attempt": seen + 1})

    # --- request handling ---

    def _handle(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/echo":
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

        if path == "/get":
            self._send_json(HTTPStatus.OK, {"method": self.command, "path": self.path})
            return

        if path == "/headers":
            self._send_json(HTTPStatus.OK, self._request_headers())
            return

        if path == "/rawheaders":
            self._send_json(
                HTTPStatus.OK,
                {"headers": [[name, value] for name, value in self.headers.items()]},
            )
            return

        if path == "/redirect":
            hops = int(query.get("hops", ["0"])[0])
            location = f"/redirect?hops={hops - 1}" if hops > 0 else "/get"
            self._send(HTTPStatus.FOUND, extra_headers=[("Location", location)])
            return

        if path == "/loop":
            self._send(HTTPStatus.FOUND, extra_headers=[("Location", "/loop")])
            return

        if path == "/redirect-to":
            target = query.get("url", [""])[0]
            if not target:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "missing url"})
                return
            self._send(HTTPStatus.FOUND, extra_headers=[("Location", target)])
            return

        if path == "/large":
            self._send(HTTPStatus.OK, b"x" * 5000, content_type="text/plain")
            return

        if path == "/utf8":
            self._send(
                HTTPStatus.OK,
                "你好，世界".encode("utf-8"),
                content_type="text/plain; charset=utf-8",
            )
            return

        if path == "/latin1":
            self._send(
                HTTPStatus.OK,
                "café".encode("iso-8859-1"),
                content_type="text/plain; charset=iso-8859-1",
            )
            return

        if path == "/no-content-type":
            self._send(HTTPStatus.OK, b"hello", content_type=None)
            return

        if path == "/gzip":
            payload = json.dumps({"compressed": True}).encode("utf-8")
            self._send(
                HTTPStatus.OK,
                gzip.compress(payload),
                extra_headers=[("Content-Encoding", "gzip")],
            )
            return

        if path == "/gzip-large":
            self._send(
                HTTPStatus.OK,
                gzip.compress(b"y" * 5000),
                content_type="text/plain",
                extra_headers=[("Content-Encoding", "gzip")],
            )
            return

        if path == "/deflate":
            payload = json.dumps({"compressed": "deflate"}).encode("utf-8")
            self._send(
                HTTPStatus.OK,
                zlib.compress(payload),
                extra_headers=[("Content-Encoding", "deflate")],
            )
            return

        if path == "/brotli":
            self._send(
                HTTPStatus.OK,
                b"not-really-brotli",
                extra_headers=[("Content-Encoding", "br")],
            )
            return

        if path == "/bad-gzip":
            self._send(
                HTTPStatus.OK,
                b"not-a-gzip-stream",
                extra_headers=[("Content-Encoding", "gzip")],
            )
            return

        if path == "/binary":
            self._send(
                HTTPStatus.OK,
                bytes(range(256)),
                content_type="application/octet-stream",
            )
            return

        if path == "/cookies":
            self._send(
                HTTPStatus.OK,
                b"cookies",
                content_type="text/plain",
                extra_headers=[
                    ("Set-Cookie", "session=abc; Path=/"),
                    ("Set-Cookie", "theme=dark; Path=/"),
                ],
            )
            return

        if path == "/slow":
            time.sleep(0.6)
            self._send_json(HTTPStatus.OK, {"status": "slow"})
            return

        if path == "/error":
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "boom"})
            return

        if path == "/empty":
            self._send(HTTPStatus.NO_CONTENT)
            return

        if path == "/flaky":
            self._flaky(query)
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


class _ServerTestCase(unittest.TestCase):
    """Base class that runs each test class against its own local server."""

    server: ClassVar[_QuietServer]
    base_url: ClassVar[str]
    thread: ClassVar[threading.Thread]

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
        cls.thread.join(timeout=5)


class TestHttpRequest(_ServerTestCase):
    """End-to-end tests for :func:`http_request`."""

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
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["redirects"], 0)
        self.assertEqual(result["body_format"], "text")
        self.assertIsInstance(result["elapsed_ms"], float)

    def test_result_contains_requested_and_final_url(self) -> None:
        result = http_request(f"{self.base_url}/get")
        self.assertEqual(result["url"], f"{self.base_url}/get")
        self.assertEqual(result["requested_url"], f"{self.base_url}/get")

    def test_default_headers_are_sent(self) -> None:
        result = http_request(f"{self.base_url}/rawheaders")
        self.assertTrue(result["ok"])
        pairs = json.loads(result["body"])["headers"]
        received = {name.lower(): value for name, value in pairs}
        self.assertTrue(received["user-agent"].startswith("dsh-superagent-http-tool"))
        self.assertEqual(received["accept"], "*/*")
        self.assertEqual(received["accept-encoding"], "gzip, deflate")

    def test_custom_headers_are_sent(self) -> None:
        result = http_request(
            f"{self.base_url}/headers",
            headers={"X-Test": "abc", "Accept": "text/plain"},
        )
        self.assertTrue(result["ok"])
        received = json.loads(result["body"])
        self.assertEqual(received["x-test"], "abc")
        self.assertEqual(received["accept"], "text/plain")

    def test_header_overrides_are_case_insensitive(self) -> None:
        result = http_request(
            f"{self.base_url}/rawheaders",
            headers={"User-Agent": "custom/1.0", "accept-encoding": "identity"},
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

    def test_query_params_are_merged_into_url(self) -> None:
        result = http_request(
            f"{self.base_url}/get?existing=1#fragment",
            params={
                "q": "dsh",
                "tag": ["a", "b"],
                "flag": True,
                "skip": None,
            },
        )
        self.assertTrue(result["ok"])
        self.assertNotIn("#", result["requested_url"])
        path = json.loads(result["body"])["path"]
        parsed = parse_qs(urlsplit(path).query)
        self.assertEqual(parsed["existing"], ["1"])
        self.assertEqual(parsed["q"], ["dsh"])
        self.assertEqual(parsed["tag"], ["a", "b"])
        self.assertEqual(parsed["flag"], ["true"])
        self.assertNotIn("skip", parsed)

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

    # --- redirects ---

    def test_redirect_is_followed(self) -> None:
        result = http_request(f"{self.base_url}/redirect?hops=2")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], HTTPStatus.OK)
        self.assertTrue(result["url"].endswith("/get"))
        self.assertEqual(result["redirects"], 3)

    def test_redirects_can_be_disabled(self) -> None:
        result = http_request(f"{self.base_url}/redirect?hops=1", follow_redirects=False)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], HTTPStatus.FOUND)
        self.assertEqual(result["headers"]["Location"], "/redirect?hops=0")
        self.assertEqual(result["body"], "")
        self.assertEqual(result["redirects"], 0)

    def test_redirect_limit_returns_structured_error(self) -> None:
        result = http_request(f"{self.base_url}/loop", max_redirects=3)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "redirect")
        self.assertIn("redirect", result["error"])

    def test_redirect_loop_is_bounded_by_default(self) -> None:
        result = http_request(f"{self.base_url}/loop")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "redirect")

    def test_redirect_with_credentials_is_rejected(self) -> None:
        target = f"http://user:pass@127.0.0.1:{urlsplit(self.base_url).port}/headers"
        result = http_request(
            f"{self.base_url}/redirect-to?url={quote(target, safe='')}"
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "redirect")

    def test_redirect_chain_respects_limit(self) -> None:
        result = http_request(f"{self.base_url}/redirect?hops=5", max_redirects=2)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "redirect")

    def test_cross_origin_redirect_strips_authorization(self) -> None:
        other = _QuietServer(("127.0.0.1", 0), _TestHandler)
        thread = threading.Thread(target=other.serve_forever, daemon=True)
        thread.start()
        try:
            target = f"http://127.0.0.1:{other.server_address[1]}/headers"
            url = f"{self.base_url}/redirect-to?url={quote(target, safe='')}"
            result = http_request(url, headers={"Authorization": "Bearer secret"})
            self.assertTrue(result["ok"])
            echoed = json.loads(result["body"])
            self.assertNotIn("authorization", echoed)
            self.assertEqual(result["redirects"], 1)
        finally:
            other.shutdown()
            other.server_close()
            thread.join(timeout=5)

    def test_same_origin_redirect_keeps_authorization(self) -> None:
        target = f"{self.base_url}/headers"
        url = f"{self.base_url}/redirect-to?url={quote(target, safe='')}"
        result = http_request(url, headers={"Authorization": "Bearer secret"})
        self.assertTrue(result["ok"])
        echoed = json.loads(result["body"])
        self.assertEqual(echoed["authorization"], "Bearer secret")

    def test_redirect_to_non_http_scheme_is_rejected(self) -> None:
        target = quote("file:///etc/passwd", safe="")
        result = http_request(f"{self.base_url}/redirect-to?url={target}")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "redirect")

    # --- response bodies ---

    def test_body_is_truncated_at_limit(self) -> None:
        result = http_request(f"{self.base_url}/large", max_body_bytes=100)
        self.assertTrue(result["ok"])
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["body"]), 100)
        self.assertEqual(result["size_bytes"], 100)

    def test_zero_body_limit(self) -> None:
        result = http_request(f"{self.base_url}/large", max_body_bytes=0)
        self.assertTrue(result["ok"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["body"], "")
        self.assertEqual(result["size_bytes"], 0)

    def test_utf8_body_is_decoded(self) -> None:
        result = http_request(f"{self.base_url}/utf8")
        self.assertTrue(result["ok"])
        self.assertEqual(result["body"], "你好，世界")
        self.assertEqual(result["body_format"], "text")
        self.assertEqual(result["charset"], "utf-8")

    def test_declared_charset_is_honoured(self) -> None:
        result = http_request(f"{self.base_url}/latin1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["body"], "café")
        self.assertEqual(result["charset"], "iso-8859-1")

    def test_missing_content_type_is_treated_as_text(self) -> None:
        result = http_request(f"{self.base_url}/no-content-type")
        self.assertTrue(result["ok"])
        self.assertEqual(result["body"], "hello")
        self.assertEqual(result["body_format"], "text")

    def test_binary_body_is_base64_encoded(self) -> None:
        result = http_request(f"{self.base_url}/binary")
        self.assertTrue(result["ok"])
        self.assertEqual(result["body_format"], "base64")
        self.assertIsNone(result["charset"])
        self.assertEqual(base64.b64decode(result["body"]), bytes(range(256)))
        self.assertEqual(result["size_bytes"], 256)

    def test_gzip_body_is_decompressed(self) -> None:
        result = http_request(f"{self.base_url}/gzip")
        self.assertTrue(result["ok"])
        self.assertEqual(json.loads(result["body"]), {"compressed": True})

    def test_gzip_body_is_truncated_after_decompression(self) -> None:
        result = http_request(f"{self.base_url}/gzip-large", max_body_bytes=100)
        self.assertTrue(result["ok"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["body"], "y" * 100)
        self.assertEqual(result["size_bytes"], 100)

    def test_deflate_body_is_decompressed(self) -> None:
        result = http_request(f"{self.base_url}/deflate")
        self.assertTrue(result["ok"])
        self.assertEqual(json.loads(result["body"]), {"compressed": "deflate"})

    def test_unsupported_content_encoding_returns_error(self) -> None:
        result = http_request(f"{self.base_url}/brotli")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "content_encoding")

    def test_invalid_compressed_body_returns_error(self) -> None:
        result = http_request(f"{self.base_url}/bad-gzip")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "content_encoding")

    def test_duplicate_response_headers_are_preserved(self) -> None:
        result = http_request(f"{self.base_url}/cookies")
        self.assertTrue(result["ok"])
        self.assertEqual(
            result["cookies"],
            ["session=abc; Path=/", "theme=dark; Path=/"],
        )
        set_cookie_items = [
            value for name, value in result["header_items"] if name.lower() == "set-cookie"
        ]
        self.assertEqual(len(set_cookie_items), 2)
        self.assertIn("session=abc", result["headers"]["Set-Cookie"])

    def test_head_request_has_no_body(self) -> None:
        result = http_request(f"{self.base_url}/get", method="HEAD")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], HTTPStatus.OK)
        self.assertEqual(result["body"], "")
        self.assertEqual(result["size_bytes"], 0)

    def test_no_content_response(self) -> None:
        result = http_request(f"{self.base_url}/empty")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], HTTPStatus.NO_CONTENT)
        self.assertEqual(result["body"], "")

    # --- transport failures ---

    def test_timeout_returns_structured_error(self) -> None:
        result = http_request(f"{self.base_url}/slow", timeout=0.15)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "timeout")
        self.assertLess(result["elapsed_ms"], 5000)
        self.assertEqual(result["attempts"], 1)

    def test_connection_refused_returns_structured_error(self) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            unused_port = sock.getsockname()[1]
        result = http_request(f"http://127.0.0.1:{unused_port}/", timeout=0.5)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "connection")

    # --- retries ---

    def test_retryable_status_is_retried(self) -> None:
        result = http_request(
            f"{self.base_url}/flaky?n=2&tag=retry-success",
            retries=2,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], HTTPStatus.OK)
        self.assertEqual(result["attempts"], 3)

    def test_retries_are_bounded(self) -> None:
        result = http_request(
            f"{self.base_url}/flaky?n=5&tag=retry-exhausted",
            retries=1,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual(result["attempts"], 2)

    def test_server_errors_are_retried_for_idempotent_methods(self) -> None:
        result = http_request(f"{self.base_url}/error", retries=1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], HTTPStatus.INTERNAL_SERVER_ERROR)
        self.assertEqual(result["attempts"], 2)

    def test_post_is_not_retried(self) -> None:
        result = http_request(
            f"{self.base_url}/flaky?n=1&tag=retry-post",
            method="POST",
            retries=3,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual(result["attempts"], 1)

    # --- argument validation ---

    def test_invalid_url_scheme_is_rejected(self) -> None:
        with self.assertRaises(HttpToolError):
            http_request("file:///etc/passwd")

    def test_url_without_host_is_rejected(self) -> None:
        with self.assertRaises(HttpToolError):
            http_request("http:///path")

    def test_url_with_credentials_is_rejected(self) -> None:
        with self.assertRaises(HttpToolError):
            http_request("http://user:pass@example.com/")

    def test_url_with_invalid_port_is_rejected(self) -> None:
        for url in (
            "http://example.com:abc/",
            "http://example.com:0/",
            "http://example.com:70000/",
        ):
            with self.subTest(url=url):
                with self.assertRaises(HttpToolError):
                    http_request(url)

    def test_url_with_whitespace_is_rejected(self) -> None:
        with self.assertRaises(HttpToolError):
            http_request("http://example.com/a b")

    def test_url_with_control_characters_is_rejected(self) -> None:
        with self.assertRaises(HttpToolError):
            http_request("http://example.com/\x00")

    def test_malformed_url_is_rejected(self) -> None:
        with self.assertRaises(HttpToolError):
            http_request("http://[::1/")

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

    def test_non_finite_json_body_is_rejected(self) -> None:
        with self.assertRaises(HttpToolError):
            http_request(
                "http://example.com",
                method="POST",
                json_body={"value": float("nan")},
            )

    def test_invalid_body_type_is_rejected(self) -> None:
        with self.assertRaises(HttpToolError):
            http_request("http://example.com", body=123)  # type: ignore[arg-type]

    def test_invalid_headers_are_rejected(self) -> None:
        for headers in (
            {"X-Test": 1},
            {"Bad Header": "x"},
            {"X-Test": "a\r\nInjected: 1"},
        ):
            with self.subTest(headers=headers):
                with self.assertRaises(HttpToolError):
                    http_request("http://example.com", headers=headers)  # type: ignore[dict-item]

    def test_invalid_params_are_rejected(self) -> None:
        for params in ("x", {"a": {"nested": 1}}, {1: "x"}):
            with self.subTest(params=params):
                with self.assertRaises(HttpToolError):
                    http_request("http://example.com", params=params)  # type: ignore[arg-type]

    def test_invalid_timeout_is_rejected(self) -> None:
        for timeout in (0, -1, float("nan"), float("inf"), 301):
            with self.subTest(timeout=timeout):
                with self.assertRaises(HttpToolError):
                    http_request("http://example.com", timeout=timeout)

    def test_invalid_max_body_bytes_is_rejected(self) -> None:
        for value in (-1, True, 1.5):
            with self.subTest(value=value):
                with self.assertRaises(HttpToolError):
                    http_request("http://example.com", max_body_bytes=value)  # type: ignore[arg-type]

    def test_invalid_redirect_options_are_rejected(self) -> None:
        with self.assertRaises(HttpToolError):
            http_request("http://example.com", follow_redirects="yes")  # type: ignore[arg-type]
        for value in (0, 51, True):
            with self.subTest(value=value):
                with self.assertRaises(HttpToolError):
                    http_request("http://example.com", max_redirects=value)  # type: ignore[arg-type]

    def test_invalid_retries_are_rejected(self) -> None:
        for value in (-1, 6, True):
            with self.subTest(value=value):
                with self.assertRaises(HttpToolError):
                    http_request("http://example.com", retries=value)  # type: ignore[arg-type]

    def test_invalid_verify_tls_is_rejected(self) -> None:
        with self.assertRaises(HttpToolError):
            http_request("http://example.com", verify_tls="no")  # type: ignore[arg-type]

    def test_ssl_context_honours_verify_flag(self) -> None:
        verified = _build_ssl_context(True)
        self.assertEqual(verified.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(verified.check_hostname)

        unverified = _build_ssl_context(False)
        self.assertEqual(unverified.verify_mode, ssl.CERT_NONE)
        self.assertFalse(unverified.check_hostname)


class TestHttpTool(_ServerTestCase):
    """Tests for the agent-facing :class:`HttpTool` wrapper."""

    def test_metadata_describes_the_tool(self) -> None:
        tool = HttpTool()
        self.assertEqual(tool.name, TOOL_NAME)
        self.assertEqual(tool.description, TOOL_DESCRIPTION)
        self.assertEqual(tool.input_schema["required"], ["url"])
        self.assertEqual(tool.input_schema["properties"]["method"]["enum"], list(ALLOWED_METHODS))
        self.assertIn("params", tool.input_schema["properties"])
        self.assertIn("retries", tool.input_schema["properties"])
        self.assertIn("verify_tls", tool.input_schema["properties"])
        self.assertIs(TOOL_INPUT_SCHEMA, tool.input_schema)

    def test_invoke_returns_tool_result(self) -> None:
        result = HttpTool().invoke({"url": f"{self.base_url}/get"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], HTTPStatus.OK)

    def test_invoke_accepts_query_params(self) -> None:
        result = HttpTool().invoke(
            {
                "url": f"{self.base_url}/get",
                "params": {"q": "dsh", "tag": ["a", "b"]},
            }
        )
        self.assertTrue(result["ok"])
        path = json.loads(result["body"])["path"]
        parsed = parse_qs(urlsplit(path).query)
        self.assertEqual(parsed["q"], ["dsh"])
        self.assertEqual(parsed["tag"], ["a", "b"])

    def test_invoke_honours_redirect_control(self) -> None:
        result = HttpTool().invoke(
            {
                "url": f"{self.base_url}/redirect?hops=1",
                "follow_redirects": False,
            }
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], HTTPStatus.FOUND)

    def test_invoke_missing_url_returns_error(self) -> None:
        result = HttpTool().invoke({})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "invalid_arguments")
        self.assertIn("invalid_arguments", result["error"])

    def test_invoke_malformed_url_returns_error(self) -> None:
        result = HttpTool().invoke({"url": "http://[::1/"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "invalid_arguments")

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

    def test_invoke_invalid_headers_returns_error(self) -> None:
        result = HttpTool().invoke(
            {"url": f"{self.base_url}/get", "headers": {"X-Test": 1}}
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "invalid_arguments")

    def test_invoke_transport_error_is_structured(self) -> None:
        result = HttpTool().invoke(
            {"url": f"{self.base_url}/slow", "timeout_seconds": 0.1}
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "timeout")


class TestHttpToolCli(_ServerTestCase):
    """Smoke tests for the command-line entry point."""

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

    def test_cli_query_params(self) -> None:
        completed = self._run(
            f"{self.base_url}/get",
            "--param",
            "q=dsh",
            "--param",
            "page=2",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(result["ok"])
        parsed = parse_qs(urlsplit(json.loads(result["body"])["path"]).query)
        self.assertEqual(parsed["q"], ["dsh"])
        self.assertEqual(parsed["page"], ["2"])

    def test_cli_no_redirect(self) -> None:
        completed = self._run(f"{self.base_url}/redirect?hops=1", "--no-redirect")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], HTTPStatus.FOUND)

    def test_cli_retries(self) -> None:
        completed = self._run(
            f"{self.base_url}/flaky?n=1&tag=cli-retry",
            "--retries",
            "1",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], HTTPStatus.OK)
        self.assertEqual(result["attempts"], 2)

    def test_cli_max_redirects(self) -> None:
        completed = self._run(
            f"{self.base_url}/redirect?hops=3",
            "--max-redirects",
            "1",
        )
        self.assertEqual(completed.returncode, 1)
        result = json.loads(completed.stdout)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "redirect")

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

    def test_cli_rejects_invalid_json(self) -> None:
        completed = self._run(f"{self.base_url}/echo", "-X", "POST", "--json", "{")
        self.assertEqual(completed.returncode, 2)
        self.assertIn("not valid JSON", completed.stderr)

    def test_cli_pretty_prints(self) -> None:
        completed = self._run(f"{self.base_url}/get", "--pretty")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn('\n  "ok": true', completed.stdout)
        self.assertTrue(json.loads(completed.stdout)["ok"])

    def test_cli_insecure_flag_is_accepted(self) -> None:
        completed = self._run(f"{self.base_url}/get", "-k")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(json.loads(completed.stdout)["ok"])


class TestDefaults(unittest.TestCase):
    """Sanity checks for the documented defaults."""

    def test_defaults_match_schema(self) -> None:
        self.assertEqual(DEFAULT_TIMEOUT, 30.0)
        self.assertEqual(DEFAULT_MAX_REDIRECTS, 10)
        self.assertEqual(DEFAULT_RETRIES, 0)
        self.assertEqual(
            TOOL_INPUT_SCHEMA["properties"]["timeout_seconds"]["default"],
            DEFAULT_TIMEOUT,
        )
        self.assertEqual(
            TOOL_INPUT_SCHEMA["properties"]["retries"]["default"],
            DEFAULT_RETRIES,
        )


if __name__ == "__main__":
    unittest.main()
