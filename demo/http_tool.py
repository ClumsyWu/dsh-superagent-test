#!/usr/bin/env python3
"""
A small, dependency-free HTTP request tool.

The tool sends an HTTP(S) request and returns a structured result describing
the response (status, headers, body) or the transport error. It is exposed
both as a command-line tool and as a tool object (``HttpTool``) that can be
plugged into an agent loop through its ``invoke(arguments)`` method.

Only the standard library is used (``urllib``), so the tool works in the same
zero-extra-dependency environment as the rest of this demo package.

Usage:
  python -m demo.http_tool https://example.com
  python -m demo.http_tool https://example.com/api -X POST --json '{"name": "dsh"}'
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

__all__ = [
    "ALLOWED_METHODS",
    "DEFAULT_MAX_BODY_BYTES",
    "DEFAULT_TIMEOUT",
    "HttpTool",
    "HttpToolError",
    "TOOL_DESCRIPTION",
    "TOOL_INPUT_SCHEMA",
    "TOOL_NAME",
    "http_request",
]

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_BODY_BYTES = 1_048_576  # 1 MiB
MAX_TIMEOUT = 300.0
ALLOWED_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")

TOOL_NAME = "http_request"
TOOL_DESCRIPTION = (
    "Send an HTTP(S) request and return the response status, headers and body. "
    "Supports GET, POST, PUT, PATCH, DELETE, HEAD and OPTIONS, custom headers, "
    "raw or JSON request bodies, and a per-request timeout."
)
TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "description": "Absolute http:// or https:// URL to request.",
        },
        "method": {
            "type": "string",
            "enum": list(ALLOWED_METHODS),
            "default": "GET",
            "description": "HTTP method to use (default: GET).",
        },
        "headers": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": "Optional request headers.",
        },
        "body": {
            "type": "string",
            "description": "Raw request body. Mutually exclusive with `json`.",
        },
        "json": {
            "type": ["object", "array", "string", "number", "boolean"],
            "description": (
                "JSON-serializable request body. Serialized and sent with "
                "Content-Type: application/json. Mutually exclusive with `body`."
            ),
        },
        "timeout_seconds": {
            "type": "number",
            "minimum": 0.1,
            "maximum": MAX_TIMEOUT,
            "default": DEFAULT_TIMEOUT,
            "description": "Request timeout in seconds.",
        },
        "max_body_bytes": {
            "type": "integer",
            "minimum": 0,
            "default": DEFAULT_MAX_BODY_BYTES,
            "description": "Maximum number of response body bytes to return.",
        },
    },
    "required": ["url"],
    "additionalProperties": False,
}
_ARGUMENT_NAMES = frozenset(TOOL_INPUT_SCHEMA["properties"])


class HttpToolError(ValueError):
    """Raised when the tool arguments are invalid."""


@dataclass(slots=True)
class _Response:
    """A completed HTTP exchange before it is converted to a tool result."""

    status: int
    reason: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    truncated: bool = False


def _validate_url(url: object) -> str:
    if not isinstance(url, str) or not url.strip():
        raise HttpToolError("`url` must be a non-empty string")
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        raise HttpToolError("`url` must use the http:// or https:// scheme")
    if not parts.hostname:
        raise HttpToolError("`url` must include a host")
    try:
        parts.port  # noqa: B018 - validates that the port is numeric
    except ValueError as exc:
        raise HttpToolError(f"`url` has an invalid port: {exc}") from exc
    return url


def _validate_method(method: object) -> str:
    if not isinstance(method, str):
        raise HttpToolError("`method` must be a string")
    method = method.upper()
    if method not in ALLOWED_METHODS:
        raise HttpToolError(
            f"unsupported method {method!r}; expected one of {', '.join(ALLOWED_METHODS)}"
        )
    return method


def _validate_headers(headers: object) -> dict[str, str]:
    if headers is None:
        return {}
    if not isinstance(headers, Mapping):
        raise HttpToolError("`headers` must be an object of header name/value pairs")
    result: dict[str, str] = {}
    for name, value in headers.items():
        if not isinstance(name, str) or not name.strip():
            raise HttpToolError("header names must be non-empty strings")
        if not isinstance(value, str):
            raise HttpToolError(f"header {name!r} must have a string value")
        result[name] = value
    return result


def _validate_timeout(timeout: object) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise HttpToolError("`timeout_seconds` must be a number")
    timeout = float(timeout)
    if not math.isfinite(timeout) or timeout <= 0 or timeout > MAX_TIMEOUT:
        raise HttpToolError(
            f"`timeout_seconds` must be greater than 0 and at most {MAX_TIMEOUT:g}"
        )
    return timeout


def _validate_max_body_bytes(max_body_bytes: object) -> int:
    if isinstance(max_body_bytes, bool) or not isinstance(max_body_bytes, int):
        raise HttpToolError("`max_body_bytes` must be an integer")
    if max_body_bytes < 0:
        raise HttpToolError("`max_body_bytes` must not be negative")
    return max_body_bytes


def _collect_headers(message: Any) -> dict[str, str]:
    """Return response headers as a plain dict, joining duplicate names."""
    headers: dict[str, str] = {}
    for name, value in message.items():
        headers[name] = f"{headers[name]}, {value}" if name in headers else value
    return headers


def _decode_body(raw: bytes, charset: str | None) -> str:
    try:
        return raw.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def http_request(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    body: str | bytes | None = None,
    json_body: Any = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
) -> dict[str, Any]:
    """Send one HTTP(S) request and return a structured result.

    The result has ``ok: True`` for a completed exchange (even for a 4xx/5xx
    response) and ``ok: False`` for a transport failure. Invalid arguments
    raise :class:`HttpToolError`. Use :class:`HttpTool` when arguments come
    from an agent as a JSON object.
    """
    started = time.monotonic()

    # Validate everything before touching the network.
    url = _validate_url(url)
    method = _validate_method(method)
    header_map = _validate_headers(headers)
    timeout = _validate_timeout(timeout)
    max_body_bytes = _validate_max_body_bytes(max_body_bytes)

    data: bytes | None
    if json_body is not None and body is not None:
        raise HttpToolError("`body` and `json` are mutually exclusive")
    if json_body is not None:
        try:
            data = json.dumps(json_body).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise HttpToolError(f"`json` is not JSON-serializable: {exc}") from exc
        if not any(name.lower() == "content-type" for name in header_map):
            header_map["Content-Type"] = "application/json"
    elif body is not None:
        if isinstance(body, str):
            data = body.encode("utf-8")
        elif isinstance(body, bytes):
            data = body
        else:
            raise HttpToolError("`body` must be a string or bytes")
    else:
        data = None

    lower_case_names = {name.lower() for name in header_map}
    if "user-agent" not in lower_case_names:
        header_map["User-Agent"] = "dsh-superagent-http-tool/1.0"
    if "accept" not in lower_case_names:
        header_map["Accept"] = "*/*"
    if "accept-encoding" not in lower_case_names:
        header_map["Accept-Encoding"] = "identity"

    try:
        request = Request(url, data=data, headers=header_map, method=method)
    except ValueError as exc:
        raise HttpToolError(f"invalid request: {exc}") from exc

    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(max_body_bytes + 1)
            truncated = len(raw) > max_body_bytes
            if truncated:
                raw = raw[:max_body_bytes]
            result = _Response(
                status=response.status,
                reason=response.reason or "",
                url=response.geturl(),
                headers=_collect_headers(response.headers),
                body=_decode_body(raw, response.headers.get_content_charset()),
                truncated=truncated,
            )
    except HTTPError as exc:
        # An HTTP error is still a valid exchange: return status and body.
        try:
            raw = exc.read(max_body_bytes + 1)
            truncated = len(raw) > max_body_bytes
            if truncated:
                raw = raw[:max_body_bytes]
        finally:
            exc.close()
        result = _Response(
            status=exc.code,
            reason=exc.reason or "",
            url=exc.geturl() or url,
            headers=_collect_headers(exc.headers or {}),
            body=_decode_body(raw, (exc.headers.get_content_charset() if exc.headers else None)),
            truncated=truncated,
        )
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {reason}",
            "method": method,
            "url": url,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
        }

    return {
        "ok": True,
        "status": result.status,
        "reason": result.reason,
        "method": method,
        "url": result.url,
        "headers": result.headers,
        "body": result.body,
        "truncated": result.truncated,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
    }


class HttpTool:
    """Agent-facing HTTP tool.

    ``name``, ``description`` and ``input_schema`` describe the tool to a model;
    ``invoke(arguments)`` accepts the decoded JSON arguments and always returns
    a JSON-serializable result instead of raising for bad input.
    """

    name = TOOL_NAME
    description = TOOL_DESCRIPTION
    input_schema = TOOL_INPUT_SCHEMA

    def invoke(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, Mapping):
            return {
                "ok": False,
                "error": "invalid_arguments: arguments must be an object",
            }
        unknown = set(arguments) - _ARGUMENT_NAMES
        if unknown:
            names = ", ".join(sorted(unknown))
            return {
                "ok": False,
                "error": f"invalid_arguments: unknown argument(s): {names}",
            }
        try:
            return http_request(
                url=arguments.get("url"),  # type: ignore[arg-type]
                method=arguments.get("method", "GET"),  # type: ignore[arg-type]
                headers=arguments.get("headers"),  # type: ignore[arg-type]
                body=arguments.get("body"),  # type: ignore[arg-type]
                json_body=arguments.get("json"),
                timeout=arguments.get("timeout_seconds", DEFAULT_TIMEOUT),  # type: ignore[arg-type]
                max_body_bytes=arguments.get("max_body_bytes", DEFAULT_MAX_BODY_BYTES),  # type: ignore[arg-type]
            )
        except HttpToolError as exc:
            return {"ok": False, "error": f"invalid_arguments: {exc}"}


def _parse_headers(values: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in values:
        name, separator, value = item.partition(":")
        if not separator or not name.strip():
            raise ValueError(f"invalid header {item!r}; expected 'Name: Value'")
        headers[name.strip()] = value.strip()
    return headers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Send an HTTP(S) request and print a JSON result.",
    )
    parser.add_argument("url", help="Absolute http:// or https:// URL to request")
    parser.add_argument(
        "-X",
        "--method",
        default="GET",
        help=f"HTTP method (default: GET; one of {', '.join(ALLOWED_METHODS)})",
    )
    parser.add_argument(
        "-H",
        "--header",
        action="append",
        default=[],
        metavar="'Name: Value'",
        help="Request header; repeat for multiple headers",
    )
    parser.add_argument(
        "-d",
        "--data",
        default=None,
        help="Raw request body",
    )
    parser.add_argument(
        "--json",
        dest="json_body",
        default=None,
        help="JSON request body (mutually exclusive with --data)",
    )
    parser.add_argument(
        "-t",
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"Request timeout in seconds (default: {DEFAULT_TIMEOUT:g})",
    )
    parser.add_argument(
        "--max-body-bytes",
        type=int,
        default=DEFAULT_MAX_BODY_BYTES,
        help=f"Maximum response body bytes to return (default: {DEFAULT_MAX_BODY_BYTES})",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print the JSON result",
    )
    args = parser.parse_args(argv)

    if args.data is not None and args.json_body is not None:
        parser.error("--data and --json are mutually exclusive")

    try:
        headers = _parse_headers(args.header)
    except ValueError as exc:
        parser.error(str(exc))

    json_body: Any = None
    if args.json_body is not None:
        try:
            json_body = json.loads(args.json_body)
        except json.JSONDecodeError as exc:
            parser.error(f"--json is not valid JSON: {exc}")

    result = None
    try:
        result = http_request(
            args.url,
            method=args.method,
            headers=headers,
            body=args.data,
            json_body=json_body,
            timeout=args.timeout,
            max_body_bytes=args.max_body_bytes,
        )
    except HttpToolError as exc:
        result = {"ok": False, "error": f"invalid_arguments: {exc}"}
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
