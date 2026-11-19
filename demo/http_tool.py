#!/usr/bin/env python3
"""
A small, dependency-free HTTP request tool.

The tool sends one HTTP(S) request and returns a structured, JSON-serialisable
result describing the exchange. It is exposed both as a command-line tool and
as a tool object (``HttpTool``) that can be plugged into an agent loop through
its ``invoke(arguments)`` method.

Only the standard library is used (``urllib``, ``ssl`` and ``zlib``), so the
tool works in the same zero-extra-dependency environment as the rest of this
demo package.

Behaviour at a glance:

* ``GET``/``POST``/``PUT``/``PATCH``/``DELETE``/``HEAD``/``OPTIONS``;
* query parameters, custom headers, raw or JSON request bodies;
* transparent ``gzip``/``deflate`` response decompression;
* redirects (opt-out and a hard limit), with credentials stripped when a
  redirect crosses origins;
* bounded retries with exponential backoff for idempotent requests;
* TLS certificate verification (on by default) with an explicit opt-out;
* per-request timeout and response-body truncation;
* textual bodies decode to text, binary bodies are returned as base64 so the
  result is always lossless and JSON-serialisable.

Usage:
  python -m demo.http_tool https://example.com
  python -m demo.http_tool https://example.com/api -X POST --json '{"name": "dsh"}'
  python -m demo.http_tool https://example.com/search --param q=dsh --pretty
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import math
import re
import socket
import ssl
import sys
import time
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

__all__ = [
    "ALLOWED_METHODS",
    "DEFAULT_MAX_BODY_BYTES",
    "DEFAULT_MAX_REDIRECTS",
    "DEFAULT_RETRIES",
    "DEFAULT_TIMEOUT",
    "HttpTool",
    "HttpToolError",
    "TOOL_DESCRIPTION",
    "TOOL_INPUT_SCHEMA",
    "TOOL_NAME",
    "http_request",
]

DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 300.0
DEFAULT_MAX_BODY_BYTES = 1_048_576  # 1 MiB
DEFAULT_MAX_REDIRECTS = 10
MAX_REDIRECTS = 50
DEFAULT_RETRIES = 0
MAX_RETRIES = 5
DEFAULT_RETRY_BACKOFF = 0.25
MAX_RETRY_DELAY = 10.0

ALLOWED_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

USER_AGENT = "dsh-superagent-http-tool/2.0"
_DEFAULT_ACCEPT = "*/*"
_DEFAULT_ACCEPT_ENCODING = "gzip, deflate"

_READ_CHUNK = 64 * 1024
_MAX_COMPRESSED_RATIO = 4
_MIN_COMPRESSED_CAP = 1_048_576

_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

_TEXT_MEDIA_TYPES = frozenset(
    {
        "application/ecmascript",
        "application/graphql",
        "application/javascript",
        "application/json",
        "application/ld+json",
        "application/rtf",
        "application/sql",
        "application/x-httpd-php",
        "application/x-ndjson",
        "application/x-www-form-urlencoded",
        "application/x-yaml",
        "application/xml",
        "application/yaml",
    }
)
_BINARY_MEDIA_TYPES = frozenset(
    {
        "application/gzip",
        "application/octet-stream",
        "application/pdf",
        "application/wasm",
        "application/x-7z-compressed",
        "application/x-bzip2",
        "application/x-gzip",
        "application/x-rar-compressed",
        "application/x-tar",
        "application/zip",
    }
)
_BINARY_MEDIA_PREFIXES = ("audio/", "font/", "image/", "video/")

TOOL_NAME = "http_request"
TOOL_DESCRIPTION = (
    "Send an HTTP(S) request and return a structured result with the response "
    "status, headers and body. Supports GET, POST, PUT, PATCH, DELETE, HEAD and "
    "OPTIONS, query parameters, custom headers, raw or JSON request bodies, "
    "redirect control, retries, TLS verification control, compressed response "
    "bodies, timeouts and response-size limits."
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
        "params": {
            "type": "object",
            "additionalProperties": {
                "type": ["string", "number", "boolean", "array"],
                "items": {"type": ["string", "number", "boolean"]},
            },
            "description": (
                "Query parameters appended to `url`. Values may be a scalar or "
                "a list of scalars; list values repeat the parameter name."
            ),
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
            "description": "Timeout in seconds for each request attempt.",
        },
        "max_body_bytes": {
            "type": "integer",
            "minimum": 0,
            "default": DEFAULT_MAX_BODY_BYTES,
            "description": "Maximum number of response body bytes to return.",
        },
        "follow_redirects": {
            "type": "boolean",
            "default": True,
            "description": "Follow 3xx redirects (default: true).",
        },
        "max_redirects": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_REDIRECTS,
            "default": DEFAULT_MAX_REDIRECTS,
            "description": "Maximum number of redirects to follow.",
        },
        "retries": {
            "type": "integer",
            "minimum": 0,
            "maximum": MAX_RETRIES,
            "default": DEFAULT_RETRIES,
            "description": (
                "Additional attempts for idempotent requests that fail with a "
                "transport error or a retryable status (408/425/429/5xx)."
            ),
        },
        "verify_tls": {
            "type": "boolean",
            "default": True,
            "description": "Verify TLS certificates for https URLs (default: true).",
        },
    },
    "required": ["url"],
    "additionalProperties": False,
}
_ARGUMENT_NAMES = frozenset(TOOL_INPUT_SCHEMA["properties"])


class HttpToolError(ValueError):
    """Raised when the tool arguments are invalid."""


@dataclass(frozen=True, slots=True)
class _RequestSpec:
    """A fully validated request, ready to be executed."""

    url: str
    method: str
    headers: dict[str, str]
    data: bytes | None
    timeout: float
    max_body_bytes: int
    follow_redirects: bool
    max_redirects: int
    retries: int
    verify_tls: bool


@dataclass(frozen=True, slots=True)
class _Exchange:
    """A completed HTTP exchange before it is converted to a tool result."""

    status: int
    reason: str
    url: str
    headers: dict[str, str]
    header_items: list[list[str]]
    cookies: list[str]
    body: str
    body_format: str
    charset: str | None
    truncated: bool
    size_bytes: int
    redirects: int


class _TransportFailure(Exception):
    """A failed exchange, classified for the tool result."""

    def __init__(self, kind: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retryable = retryable


class _RedirectError(Exception):
    """A redirect that the tool refuses to follow."""


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


def _validate_url(url: object) -> str:
    if not isinstance(url, str) or not url.strip():
        raise HttpToolError("`url` must be a non-empty string")
    url = url.strip()
    if any(character.isspace() for character in url):
        raise HttpToolError("`url` must not contain whitespace; percent-encode it")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in url):
        raise HttpToolError("`url` must not contain control characters")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise HttpToolError(f"`url` is not a valid URL: {exc}") from exc
    if parts.scheme.lower() not in ("http", "https"):
        raise HttpToolError("`url` must use the http:// or https:// scheme")
    if not parts.hostname:
        raise HttpToolError("`url` must include a host")
    if parts.username is not None or parts.password is not None:
        raise HttpToolError(
            "`url` must not embed credentials; use an Authorization header instead"
        )
    try:
        port = parts.port
    except ValueError as exc:
        raise HttpToolError(f"`url` has an invalid port: {exc}") from exc
    if port is not None and not 1 <= port <= 65535:
        raise HttpToolError("`url` has an invalid port")
    return urlunsplit(parts._replace(fragment=""))


def _validate_method(method: object) -> str:
    if not isinstance(method, str):
        raise HttpToolError("`method` must be a string")
    method = method.strip().upper()
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
        if not isinstance(name, str) or not _HEADER_NAME_RE.match(name):
            raise HttpToolError(f"invalid header name {name!r}")
        if not isinstance(value, str):
            raise HttpToolError(f"header {name!r} must have a string value")
        if "\r" in value or "\n" in value:
            raise HttpToolError(f"header {name!r} must not contain newlines")
        result[name] = value
    return result


def _has_header(headers: Mapping[str, str], name: str) -> bool:
    return any(existing.lower() == name for existing in headers)


def _validate_params(params: object) -> dict[str, list[str]]:
    if params is None:
        return {}
    if not isinstance(params, Mapping):
        raise HttpToolError("`params` must be an object of query parameters")
    result: dict[str, list[str]] = {}
    for name, value in params.items():
        if not isinstance(name, str) or not name:
            raise HttpToolError("query parameter names must be non-empty strings")
        values = _normalize_param_values(name, value)
        if values:
            result[name] = values
    return result


def _normalize_param_values(name: str, value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        normalized = [_normalize_param_value(name, item) for item in value]
        return [item for item in normalized if item is not None]
    normalized = _normalize_param_value(name, value)
    return [] if normalized is None else [normalized]


def _normalize_param_value(name: str, value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    raise HttpToolError(
        f"query parameter {name!r} must be a string, number, boolean, or a list of those"
    )


def _apply_params(url: str, params: Mapping[str, list[str]]) -> str:
    if not params:
        return url
    parts = urlsplit(url)
    encoded = urlencode(params, doseq=True)
    query = f"{parts.query}&{encoded}" if parts.query and encoded else (parts.query or encoded)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


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


def _validate_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise HttpToolError(f"`{name}` must be a boolean")
    return value


def _validate_int_range(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HttpToolError(f"`{name}` must be an integer")
    if not minimum <= value <= maximum:
        raise HttpToolError(f"`{name}` must be between {minimum} and {maximum}")
    return value


def _encode_body(
    body: object, json_body: object, headers: dict[str, str]
) -> bytes | None:
    if body is not None and json_body is not None:
        raise HttpToolError("`body` and `json` are mutually exclusive")
    if json_body is not None:
        try:
            data = json.dumps(json_body, ensure_ascii=False, allow_nan=False).encode(
                "utf-8"
            )
        except (TypeError, ValueError) as exc:
            raise HttpToolError(f"`json` is not JSON-serializable: {exc}") from exc
        if not _has_header(headers, "content-type"):
            headers["Content-Type"] = "application/json"
        return data
    if body is None:
        return None
    if isinstance(body, str):
        return body.encode("utf-8")
    if isinstance(body, (bytes, bytearray, memoryview)):
        return bytes(body)
    raise HttpToolError("`body` must be a string or bytes")


def _prepare_request(
    *,
    url: object,
    method: object,
    headers: object,
    params: object,
    body: object,
    json_body: object,
    timeout: object,
    max_body_bytes: object,
    follow_redirects: object,
    max_redirects: object,
    retries: object,
    verify_tls: object,
) -> _RequestSpec:
    validated_url = _apply_params(_validate_url(url), _validate_params(params))
    validated_method = _validate_method(method)
    header_map = _validate_headers(headers)
    data = _encode_body(body, json_body, header_map)
    if not _has_header(header_map, "user-agent"):
        header_map["User-Agent"] = USER_AGENT
    if not _has_header(header_map, "accept"):
        header_map["Accept"] = _DEFAULT_ACCEPT
    if not _has_header(header_map, "accept-encoding"):
        header_map["Accept-Encoding"] = _DEFAULT_ACCEPT_ENCODING
    return _RequestSpec(
        url=validated_url,
        method=validated_method,
        headers=header_map,
        data=data,
        timeout=_validate_timeout(timeout),
        max_body_bytes=_validate_max_body_bytes(max_body_bytes),
        follow_redirects=_validate_bool(follow_redirects, "follow_redirects"),
        max_redirects=_validate_int_range(
            max_redirects, "max_redirects", 1, MAX_REDIRECTS
        ),
        retries=_validate_int_range(retries, "retries", 0, MAX_RETRIES),
        verify_tls=_validate_bool(verify_tls, "verify_tls"),
    )


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def _build_ssl_context(verify_tls: bool) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if not verify_tls:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def _origin(url: str) -> tuple[str, str, int] | None:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if port is None:
        port = 443 if parts.scheme.lower() == "https" else 80
    return (parts.scheme.lower(), (parts.hostname or "").lower(), port)


class _RedirectHandler(HTTPRedirectHandler):
    """Redirect handler with an explicit policy and a redirect counter."""

    def __init__(self, *, follow: bool, max_redirects: int) -> None:
        super().__init__()
        self._follow = follow
        self._max_redirects = max_redirects
        self.count = 0

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> Request | None:
        if not self._follow:
            return None
        try:
            newurl = _validate_url(newurl)
        except HttpToolError as exc:
            raise _RedirectError(str(exc)) from exc
        self.count += 1
        if self.count > self._max_redirects:
            raise _RedirectError(f"exceeded the limit of {self._max_redirects} redirects")
        new_request = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_request is not None:
            origin = _origin(req.full_url)
            new_origin = _origin(new_request.full_url)
            if origin is None or new_origin is None or origin != new_origin:
                for name in ("Authorization", "Cookie", "Cookie2", "Proxy-Authorization"):
                    new_request.remove_header(name)
        return new_request


def _make_opener(spec: _RequestSpec) -> tuple[Any, _RedirectHandler]:
    redirect_handler = _RedirectHandler(
        follow=spec.follow_redirects,
        max_redirects=spec.max_redirects,
    )
    context = _build_ssl_context(spec.verify_tls)
    opener = build_opener(redirect_handler, HTTPSHandler(context=context))
    return opener, redirect_handler


def _classify_error(exc: BaseException) -> tuple[str, bool]:
    """Return ``(error_type, retryable)`` for a transport-level exception."""
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "tls", False
    if isinstance(exc, ssl.SSLError):
        return "tls", False
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout", True
    if isinstance(exc, socket.gaierror):
        return "dns", True
    if isinstance(exc, http.client.IncompleteRead):
        return "protocol", True
    if isinstance(exc, http.client.HTTPException):
        return "protocol", False
    if isinstance(exc, URLError):
        reason = exc.reason
        if isinstance(reason, BaseException):
            return _classify_error(reason)
        return "network", True
    if isinstance(exc, ConnectionError):
        return "connection", True
    if isinstance(exc, ValueError):
        return "invalid_request", False
    if isinstance(exc, OSError):
        return "network", True
    return "network", False


def _describe_error(exc: BaseException) -> str:
    if isinstance(exc, URLError):
        reason = exc.reason
        if isinstance(reason, BaseException):
            return f"{type(reason).__name__}: {reason}"
        return str(reason) or repr(exc)
    detail = str(exc)
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


def _split_headers(message: Message) -> tuple[dict[str, str], list[list[str]], list[str]]:
    """Return merged headers, the exact header list, and Set-Cookie values."""
    items = [[str(name), str(value)] for name, value in message.items()]
    merged: dict[str, str] = {}
    canonical: dict[str, str] = {}
    for name, value in items:
        key = name.lower()
        if key in canonical:
            merged[canonical[key]] = f"{merged[canonical[key]]}, {value}"
        else:
            canonical[key] = name
            merged[name] = value
    cookies = [value for name, value in items if name.lower() == "set-cookie"]
    return merged, items, cookies


def _is_textual(content_type: str | None, raw: bytes) -> bool:
    if content_type:
        main = content_type.split(";", 1)[0].strip().lower()
        if main.startswith("text/"):
            return True
        if main.endswith(("+json", "+xml", "+yaml", "+text")):
            return True
        if main in _TEXT_MEDIA_TYPES:
            return True
        if main in _BINARY_MEDIA_TYPES or main.startswith(_BINARY_MEDIA_PREFIXES):
            return False
    if b"\x00" in raw[:4096]:
        return False
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _decode_body(message: Message, raw: bytes) -> tuple[str, str, str | None]:
    if not raw:
        return "", "text", None
    raw_content_type = message.get("Content-Type") if message is not None else None
    if not _is_textual(raw_content_type, raw):
        return base64.b64encode(raw).decode("ascii"), "base64", None
    charset = message.get_content_charset() if message is not None else None
    charset = (charset or "utf-8").strip() or "utf-8"
    try:
        text = raw.decode(charset, errors="replace")
    except LookupError:
        charset = "utf-8"
        text = raw.decode("utf-8", errors="replace")
    return text, "text", charset


def _read_body(
    stream: Any,
    message: Message,
    max_body_bytes: int,
    *,
    has_body: bool,
) -> tuple[bytes, bool]:
    """Read at most ``max_body_bytes`` decoded bytes from ``stream``."""
    if not has_body:
        return b"", False
    encoding = (message.get("Content-Encoding") or "").strip().lower()
    try:
        if encoding in ("", "identity"):
            raw = stream.read(max_body_bytes + 1)
            return raw[:max_body_bytes], len(raw) > max_body_bytes
        if encoding not in ("gzip", "deflate"):
            raise _TransportFailure(
                "content_encoding",
                f"unsupported Content-Encoding {encoding!r}",
                retryable=False,
            )

        window_bits = zlib.MAX_WBITS | 16 if encoding == "gzip" else zlib.MAX_WBITS
        decompressor = zlib.decompressobj(window_bits)
        decoded = bytearray()
        compressed_bytes = 0
        compressed_cap = max(max_body_bytes * _MAX_COMPRESSED_RATIO, _MIN_COMPRESSED_CAP)
        while len(decoded) <= max_body_bytes:
            chunk = stream.read(_READ_CHUNK)
            if not chunk:
                break
            compressed_bytes += len(chunk)
            if compressed_bytes > compressed_cap:
                raise _TransportFailure(
                    "content_encoding",
                    "compressed response body is too large",
                    retryable=False,
                )
            decoded.extend(
                decompressor.decompress(chunk, max_body_bytes + 1 - len(decoded))
            )
            if decompressor.unconsumed_tail or decompressor.eof:
                break
        truncated = len(decoded) > max_body_bytes
        if truncated:
            return bytes(decoded[:max_body_bytes]), True
        if compressed_bytes and not decompressor.eof:
            raise _TransportFailure(
                "content_encoding",
                f"incomplete {encoding} response body",
                retryable=False,
            )
        return bytes(decoded), False
    except _TransportFailure:
        raise
    except (OSError, TimeoutError, http.client.HTTPException) as exc:
        kind, retryable = _classify_error(exc)
        raise _TransportFailure(kind, _describe_error(exc), retryable=retryable) from exc
    except zlib.error as exc:
        raise _TransportFailure(
            "content_encoding",
            f"invalid {encoding} response body: {exc}",
            retryable=False,
        ) from exc


def _build_exchange(
    *,
    status: int,
    reason: str,
    url: str,
    message: Message,
    raw: bytes,
    truncated: bool,
    redirects: int,
) -> _Exchange:
    headers, header_items, cookies = _split_headers(message)
    body, body_format, charset = _decode_body(message, raw)
    return _Exchange(
        status=status,
        reason=str(reason or ""),
        url=urlunsplit(urlsplit(url)._replace(fragment="")),
        headers=headers,
        header_items=header_items,
        cookies=cookies,
        body=body,
        body_format=body_format,
        charset=charset,
        truncated=truncated,
        size_bytes=len(raw),
        redirects=redirects,
    )


def _perform(
    spec: _RequestSpec,
    opener: Any,
    redirect_handler: _RedirectHandler,
) -> _Exchange:
    try:
        request = Request(
            spec.url,
            data=spec.data,
            headers=spec.headers,
            method=spec.method,
        )
        response = opener.open(request, timeout=spec.timeout)
    except HTTPError as exc:
        try:
            message = exc.headers if exc.headers is not None else Message()
            if spec.follow_redirects and exc.code in (301, 302, 303, 307, 308):
                location = message.get("Location")
                if location:
                    try:
                        scheme = urlsplit(location).scheme.lower()
                    except ValueError as exc:
                        raise _TransportFailure(
                            "redirect",
                            f"invalid redirect URL {location!r}: {exc}",
                            retryable=False,
                        ) from exc
                else:
                    scheme = ""
                if scheme and scheme not in ("http", "https"):
                    raise _TransportFailure(
                        "redirect",
                        f"refusing redirect to unsupported URL: {location!r}",
                        retryable=False,
                    )
                if location:
                    raise _TransportFailure(
                        "redirect",
                        f"redirect could not be followed: {location!r}",
                        retryable=False,
                    )
            has_body = spec.method != "HEAD" and exc.code not in (204, 304)
            raw, truncated = _read_body(
                exc, message, spec.max_body_bytes, has_body=has_body
            )
            return _build_exchange(
                status=exc.code,
                reason=exc.reason or "",
                url=exc.geturl() or spec.url,
                message=message,
                raw=raw,
                truncated=truncated,
                redirects=redirect_handler.count,
            )
        finally:
            exc.close()
    except _RedirectError as exc:
        raise _TransportFailure("redirect", str(exc), retryable=False) from exc
    except (URLError, OSError, TimeoutError, http.client.HTTPException, ValueError) as exc:
        kind, retryable = _classify_error(exc)
        raise _TransportFailure(kind, _describe_error(exc), retryable=retryable) from exc

    with response:
        has_body = spec.method != "HEAD" and response.status not in (204, 304)
        raw, truncated = _read_body(
            response, response.headers, spec.max_body_bytes, has_body=has_body
        )
        return _build_exchange(
            status=response.status,
            reason=response.reason or "",
            url=response.geturl() or spec.url,
            message=response.headers,
            raw=raw,
            truncated=truncated,
            redirects=redirect_handler.count,
        )


# ---------------------------------------------------------------------------
# Retry handling
# ---------------------------------------------------------------------------


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, min(seconds, MAX_RETRY_DELAY))


def _retry_delay(attempts: int, retry_after: str | None) -> float:
    parsed = _parse_retry_after(retry_after)
    if parsed is not None:
        return parsed
    return min(DEFAULT_RETRY_BACKOFF * (2 ** (attempts - 1)), MAX_RETRY_DELAY)


def _should_retry(
    spec: _RequestSpec,
    attempts: int,
    *,
    failure: _TransportFailure | None = None,
    exchange: _Exchange | None = None,
) -> bool:
    if attempts > spec.retries or spec.method not in _IDEMPOTENT_METHODS:
        return False
    if failure is not None:
        return failure.retryable
    return exchange is not None and exchange.status in _RETRYABLE_STATUSES


# ---------------------------------------------------------------------------
# Result construction
# ---------------------------------------------------------------------------


def _elapsed_ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 2)


def _success_result(
    spec: _RequestSpec, exchange: _Exchange, attempts: int, started: float
) -> dict[str, Any]:
    return {
        "ok": True,
        "status": exchange.status,
        "reason": exchange.reason,
        "method": spec.method,
        "url": exchange.url,
        "requested_url": spec.url,
        "headers": exchange.headers,
        "header_items": exchange.header_items,
        "cookies": exchange.cookies,
        "body": exchange.body,
        "body_format": exchange.body_format,
        "charset": exchange.charset,
        "truncated": exchange.truncated,
        "size_bytes": exchange.size_bytes,
        "redirects": exchange.redirects,
        "attempts": attempts,
        "elapsed_ms": _elapsed_ms(started),
    }


def _failure_result(
    spec: _RequestSpec,
    failure: _TransportFailure,
    attempts: int,
    started: float,
) -> dict[str, Any]:
    return {
        "ok": False,
        "error": f"{failure.kind}: {failure.message}",
        "error_type": failure.kind,
        "method": spec.method,
        "url": spec.url,
        "attempts": attempts,
        "elapsed_ms": _elapsed_ms(started),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def http_request(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, Any] | None = None,
    body: str | bytes | bytearray | memoryview | None = None,
    json_body: Any = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    follow_redirects: bool = True,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    retries: int = DEFAULT_RETRIES,
    verify_tls: bool = True,
) -> dict[str, Any]:
    """Send one HTTP(S) request and return a structured result.

    The result has ``ok: True`` for a completed exchange (even for a 4xx/5xx
    response) and ``ok: False`` for an argument or transport failure. Invalid
    arguments raise :class:`HttpToolError`; use :class:`HttpTool` when arguments
    come from an agent as a JSON object.

    ``retries`` only applies to idempotent methods (GET, HEAD, OPTIONS, PUT,
    DELETE) and retries transport failures as well as the statuses 408, 425,
    429, 500, 502, 503 and 504. ``Retry-After`` is honoured when present.
    """
    started = time.monotonic()
    spec = _prepare_request(
        url=url,
        method=method,
        headers=headers,
        params=params,
        body=body,
        json_body=json_body,
        timeout=timeout,
        max_body_bytes=max_body_bytes,
        follow_redirects=follow_redirects,
        max_redirects=max_redirects,
        retries=retries,
        verify_tls=verify_tls,
    )

    attempts = 0
    while True:
        attempts += 1
        opener, redirect_handler = _make_opener(spec)
        try:
            exchange = _perform(spec, opener, redirect_handler)
        except _TransportFailure as failure:
            if _should_retry(spec, attempts, failure=failure):
                time.sleep(_retry_delay(attempts, None))
                continue
            return _failure_result(spec, failure, attempts, started)
        if _should_retry(spec, attempts, exchange=exchange):
            time.sleep(_retry_delay(attempts, exchange.headers.get("Retry-After")))
            continue
        return _success_result(spec, exchange, attempts, started)


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
                "error_type": "invalid_arguments",
            }
        unknown = set(arguments) - _ARGUMENT_NAMES
        if unknown:
            names = ", ".join(sorted(str(name) for name in unknown))
            return {
                "ok": False,
                "error": f"invalid_arguments: unknown argument(s): {names}",
                "error_type": "invalid_arguments",
            }
        try:
            return http_request(
                url=arguments.get("url"),  # type: ignore[arg-type]
                method=arguments.get("method", "GET"),  # type: ignore[arg-type]
                headers=arguments.get("headers"),  # type: ignore[arg-type]
                params=arguments.get("params"),
                body=arguments.get("body"),  # type: ignore[arg-type]
                json_body=arguments.get("json"),
                timeout=arguments.get("timeout_seconds", DEFAULT_TIMEOUT),  # type: ignore[arg-type]
                max_body_bytes=arguments.get("max_body_bytes", DEFAULT_MAX_BODY_BYTES),  # type: ignore[arg-type]
                follow_redirects=arguments.get("follow_redirects", True),  # type: ignore[arg-type]
                max_redirects=arguments.get("max_redirects", DEFAULT_MAX_REDIRECTS),  # type: ignore[arg-type]
                retries=arguments.get("retries", DEFAULT_RETRIES),  # type: ignore[arg-type]
                verify_tls=arguments.get("verify_tls", True),  # type: ignore[arg-type]
            )
        except HttpToolError as exc:
            return {
                "ok": False,
                "error": f"invalid_arguments: {exc}",
                "error_type": "invalid_arguments",
            }


# ---------------------------------------------------------------------------
# Command-line interface
# ---------------------------------------------------------------------------


def _parse_headers(values: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in values:
        name, separator, value = item.partition(":")
        if not separator or not name.strip():
            raise ValueError(f"invalid header {item!r}; expected 'Name: Value'")
        headers[name.strip()] = value.strip()
    return headers


def _parse_params(values: list[str]) -> dict[str, list[str]]:
    params: dict[str, list[str]] = {}
    for item in values:
        name, separator, value = item.partition("=")
        if not name.strip():
            raise ValueError(f"invalid parameter {item!r}; expected 'name=value'")
        params.setdefault(name.strip(), []).append(value)
    return params


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m demo.http_tool",
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
        "--param",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Query parameter; repeat for multiple parameters",
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
        help=f"Timeout in seconds per attempt (default: {DEFAULT_TIMEOUT:g})",
    )
    parser.add_argument(
        "--max-body-bytes",
        type=int,
        default=DEFAULT_MAX_BODY_BYTES,
        help=f"Maximum response body bytes to return (default: {DEFAULT_MAX_BODY_BYTES})",
    )
    parser.add_argument(
        "--no-redirect",
        dest="follow_redirects",
        action="store_false",
        help="Do not follow redirects",
    )
    parser.add_argument(
        "--max-redirects",
        type=int,
        default=DEFAULT_MAX_REDIRECTS,
        help=f"Maximum redirects to follow (default: {DEFAULT_MAX_REDIRECTS})",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help="Retries for idempotent requests (default: 0)",
    )
    parser.add_argument(
        "-k",
        "--insecure",
        action="store_true",
        help="Do not verify TLS certificates (dangerous)",
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
        params = _parse_params(args.param)
    except ValueError as exc:
        parser.error(str(exc))

    json_body: Any = None
    if args.json_body is not None:
        try:
            json_body = json.loads(args.json_body)
        except ValueError as exc:
            parser.error(f"--json is not valid JSON: {exc}")

    try:
        result = http_request(
            args.url,
            method=args.method,
            headers=headers,
            params=params,
            body=args.data,
            json_body=json_body,
            timeout=args.timeout,
            max_body_bytes=args.max_body_bytes,
            follow_redirects=args.follow_redirects,
            max_redirects=args.max_redirects,
            retries=args.retries,
            verify_tls=not args.insecure,
        )
    except HttpToolError as exc:
        result = {
            "ok": False,
            "error": f"invalid_arguments: {exc}",
            "error_type": "invalid_arguments",
        }
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
