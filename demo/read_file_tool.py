#!/usr/bin/env python3
"""
A small, dependency-free text file reading tool.

The tool reads a file and returns a structured result describing the requested
lines: the resolved path, the file size, the encoding, the line range and the
content itself. It is exposed both as a command-line tool and as a tool object
(``ReadFileTool``) that can be plugged into an agent loop through its
``invoke(arguments)`` method.

Only the standard library is used, so the tool works in the same
zero-extra-dependency environment as the rest of this demo package.

The reader is line-oriented: ``offset`` is a 1-based line number and ``limit``
caps how many lines are returned. ``max_bytes`` caps the size of the returned
content so a single call cannot flood an agent's context. Content keeps its
original line endings. When a single line is larger than ``max_bytes``, the
line is truncated (``partial_line`` is set) and ``next_offset`` is ``None``:
raise ``max_bytes`` and retry the same ``offset``. Files that look binary (a
NUL byte in the first 8 KiB) are rejected unless a UTF-16/UTF-32 encoding is
requested.

Usage:
  python -m demo.read_file_tool README.md
  python -m demo.read_file_tool README.md --offset 10 --limit 20
"""

from __future__ import annotations

import argparse
import codecs
import io
import json
import stat
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = [
    "BINARY_SNIFF_BYTES",
    "DEFAULT_LIMIT",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_OFFSET",
    "MAX_LIMIT",
    "ReadFileTool",
    "ReadFileToolError",
    "TOOL_DESCRIPTION",
    "TOOL_INPUT_SCHEMA",
    "TOOL_NAME",
    "read_file",
]

DEFAULT_OFFSET = 1
DEFAULT_LIMIT = 2000
MAX_LIMIT = 100_000
DEFAULT_MAX_BYTES = 1_048_576  # 1 MiB
BINARY_SNIFF_BYTES = 8192

TOOL_NAME = "read_file"
TOOL_DESCRIPTION = (
    "Read a text file and return the requested lines. Supports a 1-based line "
    "offset, a line limit, an explicit text encoding and a byte cap for the "
    "returned content."
)
TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": (
                "Path to the file to read. Absolute, or relative to the current "
                "working directory; `~` is expanded."
            ),
        },
        "offset": {
            "type": "integer",
            "minimum": 1,
            "default": DEFAULT_OFFSET,
            "description": "1-based line number to start reading from.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_LIMIT,
            "default": DEFAULT_LIMIT,
            "description": "Maximum number of lines to return.",
        },
        "max_bytes": {
            "type": "integer",
            "minimum": 1,
            "default": DEFAULT_MAX_BYTES,
            "description": "Maximum size in bytes of the returned content.",
        },
        "encoding": {
            "type": "string",
            "default": "utf-8",
            "description": "Text encoding of the file.",
        },
    },
    "required": ["path"],
    "additionalProperties": False,
}
_ARGUMENT_NAMES = frozenset(TOOL_INPUT_SCHEMA["properties"])


class ReadFileToolError(ValueError):
    """Raised when the tool arguments are invalid."""


def _error(error_type: str, message: str, **extra: Any) -> dict[str, Any]:
    """Build a structured failure result."""
    return {"ok": False, "error_type": error_type, "error": message, **extra}


def _validate_path(path: object) -> Path:
    if not isinstance(path, str) or not path.strip():
        raise ReadFileToolError("`path` must be a non-empty string")
    if "\x00" in path:
        raise ReadFileToolError("`path` must not contain NUL bytes")
    return Path(path).expanduser()


def _validate_int(
    name: str,
    value: object,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReadFileToolError(f"`{name}` must be an integer")
    if value < minimum:
        raise ReadFileToolError(f"`{name}` must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ReadFileToolError(f"`{name}` must be at most {maximum}")
    return value


def _validate_encoding(encoding: object) -> str:
    if not isinstance(encoding, str) or not encoding.strip():
        raise ReadFileToolError("`encoding` must be a non-empty string")
    try:
        codecs.lookup(encoding)
        # ``codecs.lookup`` also accepts bytes-to-bytes codecs such as
        # ``base64``; ``str.encode`` rejects those with a LookupError.
        "".encode(encoding)
    except LookupError as exc:
        raise ReadFileToolError(f"unknown text encoding {encoding!r}") from exc
    return encoding


def _encoding_allows_nul(encoding: str) -> bool:
    """Return True when a NUL byte is expected in normal text of this encoding."""
    name = codecs.lookup(encoding).name
    return name.startswith("utf-16") or name.startswith("utf-32")


def _truncate_line(line: str, max_bytes: int, encoding: str) -> str:
    """Truncate ``line`` to at most ``max_bytes`` encoded bytes.

    The cut happens on a code-unit boundary so decoding does not produce a
    spurious replacement character.
    """
    encoded = line.encode(encoding, errors="replace")
    if len(encoded) <= max_bytes:
        return line
    name = codecs.lookup(encoding).name
    unit = 4 if name.startswith("utf-32") else 2 if name.startswith("utf-16") else 1
    kept = max_bytes - (max_bytes % unit)
    if kept <= 0:
        return ""
    return encoded[:kept].decode(encoding, errors="ignore")


def read_file(
    path: str,
    *,
    offset: int = DEFAULT_OFFSET,
    limit: int = DEFAULT_LIMIT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    encoding: str = "utf-8",
) -> dict[str, Any]:
    """Read lines from a text file and return a structured result.

    Invalid arguments raise :class:`ReadFileToolError`. Filesystem and file
    content problems are returned as ``{"ok": False, "error_type": ...,
    "error": ...}`` so a tool loop can hand them back to a model unchanged.
    """
    candidate = _validate_path(path)
    offset = _validate_int("offset", offset, minimum=1)
    limit = _validate_int("limit", limit, minimum=1, maximum=MAX_LIMIT)
    max_bytes = _validate_int("max_bytes", max_bytes, minimum=1)
    encoding = _validate_encoding(encoding)

    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError:
        return _error("not_found", f"file not found: {candidate}", path=str(candidate))
    except PermissionError:
        return _error("permission", f"permission denied: {candidate}", path=str(candidate))
    except (OSError, RuntimeError) as exc:
        return _error("read_error", f"cannot resolve path: {exc}", path=str(candidate))

    try:
        file_stat = resolved.stat()
    except PermissionError:
        return _error("permission", f"permission denied: {resolved}", path=str(resolved))
    except OSError as exc:
        return _error("read_error", f"cannot stat file: {exc}", path=str(resolved))

    if stat.S_ISDIR(file_stat.st_mode):
        return _error("is_directory", f"path is a directory: {resolved}", path=str(resolved))
    if not stat.S_ISREG(file_stat.st_mode):
        return _error(
            "not_a_file",
            f"path is not a regular file: {resolved}",
            path=str(resolved),
        )

    try:
        with resolved.open("rb") as probe:
            sample = probe.read(BINARY_SNIFF_BYTES)
    except PermissionError:
        return _error("permission", f"permission denied: {resolved}", path=str(resolved))
    except OSError as exc:
        return _error("read_error", f"cannot open file: {exc}", path=str(resolved))

    if b"\x00" in sample and not _encoding_allows_nul(encoding):
        return _error(
            "binary_file",
            (
                f"file appears to be binary (NUL byte in the first "
                f"{BINARY_SNIFF_BYTES} bytes): {resolved}"
            ),
            path=str(resolved),
        )

    selected: list[str] = []
    selected_bytes = 0
    stop_reason: str | None = None
    partial_line = False
    line_number = 0

    try:
        with io.open(
            resolved,
            "r",
            encoding=encoding,
            errors="replace",
            newline="",
        ) as text:
            for line in text:
                line_number += 1
                if line_number < offset:
                    continue
                line_bytes = len(line.encode(encoding, errors="replace"))
                if selected_bytes + line_bytes > max_bytes:
                    if not selected:
                        partial = _truncate_line(line, max_bytes, encoding)
                        selected.append(partial)
                        selected_bytes = len(partial.encode(encoding, errors="replace"))
                        partial_line = True
                    stop_reason = "max_bytes"
                    break
                selected.append(line)
                selected_bytes += line_bytes
                if len(selected) >= limit:
                    stop_reason = "limit"
                    break

            has_more = False
            if stop_reason == "max_bytes":
                # The current line was consumed but not fully returned, or the
                # remainder of a truncated line still needs to be read.
                has_more = True
            elif stop_reason == "limit":
                has_more = text.readline() != ""
    except OSError as exc:
        return _error("read_error", f"cannot read file: {exc}", path=str(resolved))

    if not has_more:
        stop_reason = None

    next_offset: int | None = None
    if has_more and not partial_line:
        next_offset = offset + len(selected)

    return {
        "ok": True,
        "path": str(resolved),
        "size_bytes": file_stat.st_size,
        "encoding": encoding,
        "offset": offset,
        "limit": limit,
        "line_count": len(selected),
        "next_offset": next_offset,
        "has_more": has_more,
        "truncated": has_more,
        "truncated_by": stop_reason,
        "partial_line": partial_line,
        "content": "".join(selected),
    }


class ReadFileTool:
    """Agent-facing file reading tool.

    ``name``, ``description`` and ``input_schema`` describe the tool to a model;
    ``invoke(arguments)`` accepts the decoded JSON arguments and always returns
    a JSON-serializable result instead of raising for bad input.
    """

    name = TOOL_NAME
    description = TOOL_DESCRIPTION
    input_schema = TOOL_INPUT_SCHEMA

    def invoke(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, Mapping):
            return _error("invalid_arguments", "arguments must be an object")
        unknown = set(arguments) - _ARGUMENT_NAMES
        if unknown:
            names = ", ".join(sorted(unknown))
            return _error("invalid_arguments", f"unknown argument(s): {names}")
        try:
            return read_file(
                path=arguments.get("path"),  # type: ignore[arg-type]
                offset=arguments.get("offset", DEFAULT_OFFSET),  # type: ignore[arg-type]
                limit=arguments.get("limit", DEFAULT_LIMIT),  # type: ignore[arg-type]
                max_bytes=arguments.get("max_bytes", DEFAULT_MAX_BYTES),  # type: ignore[arg-type]
                encoding=arguments.get("encoding", "utf-8"),  # type: ignore[arg-type]
            )
        except ReadFileToolError as exc:
            return _error("invalid_arguments", str(exc))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read a text file and print a JSON result.",
    )
    parser.add_argument("path", help="File to read")
    parser.add_argument(
        "--offset",
        type=int,
        default=DEFAULT_OFFSET,
        help=f"First line to read, 1-based (default: {DEFAULT_OFFSET})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Maximum lines to read (default: {DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help=f"Maximum bytes of returned content (default: {DEFAULT_MAX_BYTES})",
    )
    parser.add_argument(
        "--encoding",
        default="utf-8",
        help="Text encoding (default: utf-8)",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print the JSON result",
    )
    args = parser.parse_args(argv)

    try:
        result = read_file(
            args.path,
            offset=args.offset,
            limit=args.limit,
            max_bytes=args.max_bytes,
            encoding=args.encoding,
        )
    except ReadFileToolError as exc:
        result = _error("invalid_arguments", str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
