#!/usr/bin/env python3
"""
A small, dependency-free text file editing tool.

The tool edits an existing text file by replacing one exact string with
another. By default the string must occur exactly once, so an ambiguous
search string fails instead of silently editing the wrong place. Files are
decoded with an explicit encoding, rejected when they look binary, and written
back atomically (temporary file + ``os.replace``) with the original file mode
preserved.

It is exposed both as a command-line tool and as a tool object (``EditTool``)
that can be plugged into an agent loop through its ``invoke(arguments)``
method.

Only the standard library is used, so the tool works in the same
zero-extra-dependency environment as the rest of this demo package.

Usage:
  python -m demo.edit_tool notes.txt --old "hello" --new "hi"
  python -m demo.edit_tool notes.txt --old "TODO" --new "DONE" --replace-all
  python -m demo.edit_tool notes.txt --old "old" --new "new" --dry-run
"""

from __future__ import annotations

import argparse
import codecs
import json
import os
import stat
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_ENCODING",
    "EditArgumentError",
    "EditFileError",
    "EditTool",
    "TOOL_DESCRIPTION",
    "TOOL_INPUT_SCHEMA",
    "TOOL_NAME",
    "edit_file",
]

DEFAULT_ENCODING = "utf-8"

TOOL_NAME = "edit_file"
TOOL_DESCRIPTION = (
    "Edit an existing text file by replacing an exact string with new text. "
    "By default `old_string` must occur exactly once, which prevents editing "
    "the wrong occurrence; set `replace_all` to replace every occurrence. "
    "Read the file first so the match is exact, including whitespace and line "
    "endings. The file is written atomically and its permissions are kept."
)
TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file_path": {
            "type": "string",
            "description": "Path to an existing text file to edit.",
        },
        "old_string": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Exact text to replace. Must occur exactly once unless "
                "`replace_all` is true."
            ),
        },
        "new_string": {
            "type": "string",
            "description": "Replacement text; use an empty string to delete.",
        },
        "replace_all": {
            "type": "boolean",
            "default": False,
            "description": "Replace every occurrence instead of exactly one.",
        },
        "encoding": {
            "type": "string",
            "default": DEFAULT_ENCODING,
            "description": "Text encoding used to read and write the file.",
        },
        "dry_run": {
            "type": "boolean",
            "default": False,
            "description": "Report the change without writing the file.",
        },
    },
    "required": ["file_path", "old_string", "new_string"],
    "additionalProperties": False,
}
_ARGUMENT_NAMES = frozenset(TOOL_INPUT_SCHEMA["properties"])


class EditArgumentError(ValueError):
    """Raised when the tool arguments are invalid."""


class EditFileError(Exception):
    """Raised when the file cannot be edited."""


def _validate_path(file_path: object) -> Path:
    if not isinstance(file_path, str) or not file_path:
        raise EditArgumentError("`file_path` must be a non-empty string")
    try:
        path = Path(file_path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise EditArgumentError(f"`file_path` is invalid: {exc}") from exc
    if path.is_dir():
        raise EditArgumentError(f"`file_path` is a directory, not a file: {path}")
    if not path.exists():
        raise EditFileError(f"file does not exist: {path}")
    return path


def _validate_text(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise EditArgumentError(f"`{name}` must be a string")
    return value


def _validate_old_string(old_string: object) -> str:
    old_string = _validate_text("old_string", old_string)
    if not old_string:
        raise EditArgumentError("`old_string` must not be empty")
    return old_string


def _validate_bool(name: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise EditArgumentError(f"`{name}` must be a boolean")
    return value


def _validate_encoding(encoding: object) -> str:
    encoding = _validate_text("encoding", encoding)
    try:
        codecs.lookup(encoding)
    except LookupError as exc:
        raise EditArgumentError(f"unknown encoding {encoding!r}") from exc
    return encoding


def _read_text(path: Path, encoding: str) -> tuple[str, os.stat_result]:
    try:
        info = path.stat()
    except OSError as exc:
        raise EditFileError(f"cannot stat {path}: {exc.strerror or exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise EditFileError(f"not a regular file: {path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise EditFileError(f"cannot read {path}: {exc.strerror or exc}") from exc
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError as exc:
        raise EditFileError(
            f"{path} is not valid {encoding} text (binary file or wrong encoding)"
        ) from exc
    if "\x00" in text:
        raise EditFileError(f"{path} looks like a binary file (contains NUL bytes)")
    return text, info


def _write_atomically(path: Path, data: bytes, mode: int) -> None:
    """Replace ``path`` with ``data`` without exposing a partially written file."""
    directory = path.parent
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=directory
        )
    except OSError as exc:
        raise EditFileError(
            f"cannot create a temporary file next to {path}: {exc.strerror or exc}"
        ) from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    except OSError as exc:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise EditFileError(f"cannot write {path}: {exc.strerror or exc}") from exc


def edit_file(
    file_path: str,
    old_string: str,
    new_string: str,
    *,
    replace_all: bool = False,
    encoding: str = DEFAULT_ENCODING,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Replace exact text in ``file_path`` and return a structured result.

    ``old_string`` must occur exactly once unless ``replace_all`` is true.
    Invalid arguments raise :class:`EditArgumentError`; unreadable or
    unwritable files and failed matches raise :class:`EditFileError`.
    """
    old_string = _validate_old_string(old_string)
    new_string = _validate_text("new_string", new_string)
    replace_all = _validate_bool("replace_all", replace_all)
    dry_run = _validate_bool("dry_run", dry_run)
    encoding = _validate_encoding(encoding)
    path = _validate_path(file_path)

    if old_string == new_string:
        raise EditFileError(
            "`old_string` and `new_string` are identical; nothing to edit"
        )

    text, info = _read_text(path, encoding)
    occurrences = text.count(old_string)
    if occurrences == 0:
        raise EditFileError(
            f"`old_string` was not found in {path} "
            "(check exact text, whitespace and line endings)"
        )
    if occurrences > 1 and not replace_all:
        raise EditFileError(
            f"`old_string` occurs {occurrences} times in {path}; "
            "add surrounding context or set `replace_all`"
        )

    first_index = text.find(old_string)
    line = text.count("\n", 0, first_index) + 1
    replacements = occurrences if replace_all else 1
    new_text = (
        text.replace(old_string, new_string)
        if replace_all
        else text.replace(old_string, new_string, 1)
    )
    try:
        new_data = new_text.encode(encoding)
    except UnicodeEncodeError as exc:
        raise EditFileError(
            f"`new_string` cannot be encoded as {encoding}: {exc}"
        ) from exc

    if not dry_run:
        # Re-check the file between reading and writing so a concurrent change
        # is not silently overwritten.
        try:
            current = path.stat()
        except OSError as exc:
            raise EditFileError(
                f"cannot stat {path}: {exc.strerror or exc}"
            ) from exc
        if (current.st_mtime_ns, current.st_size) != (info.st_mtime_ns, info.st_size):
            raise EditFileError(f"{path} changed while it was being edited")
        _write_atomically(path, new_data, stat.S_IMODE(info.st_mode))

    return {
        "ok": True,
        "file_path": str(path),
        "replacements": replacements,
        "line": line,
        "encoding": encoding,
        "dry_run": dry_run,
        "changed": not dry_run,
        "bytes_before": info.st_size,
        "bytes_after": len(new_data),
    }


class EditTool:
    """Agent-facing text editing tool.

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
            return edit_file(
                file_path=arguments.get("file_path"),  # type: ignore[arg-type]
                old_string=arguments.get("old_string"),  # type: ignore[arg-type]
                new_string=arguments.get("new_string"),  # type: ignore[arg-type]
                replace_all=arguments.get("replace_all", False),  # type: ignore[arg-type]
                encoding=arguments.get("encoding", DEFAULT_ENCODING),  # type: ignore[arg-type]
                dry_run=arguments.get("dry_run", False),  # type: ignore[arg-type]
            )
        except EditArgumentError as exc:
            return {"ok": False, "error": f"invalid_arguments: {exc}"}
        except EditFileError as exc:
            return {"ok": False, "error": f"edit_failed: {exc}"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replace exact text in an existing file and print a JSON result.",
    )
    parser.add_argument("file_path", help="Path to an existing text file")
    parser.add_argument(
        "--old",
        required=True,
        help="Exact text to replace (shell-quote multiline text)",
    )
    parser.add_argument(
        "--new",
        required=True,
        help="Replacement text; use '' to delete the matched text",
    )
    parser.add_argument(
        "--replace-all",
        action="store_true",
        help="Replace every occurrence instead of requiring exactly one",
    )
    parser.add_argument(
        "--encoding",
        default=DEFAULT_ENCODING,
        help=f"Text encoding (default: {DEFAULT_ENCODING})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the change without writing the file",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print the JSON result",
    )
    args = parser.parse_args(argv)

    try:
        result = edit_file(
            args.file_path,
            args.old,
            args.new,
            replace_all=args.replace_all,
            encoding=args.encoding,
            dry_run=args.dry_run,
        )
    except EditArgumentError as exc:
        result = {"ok": False, "error": f"invalid_arguments: {exc}"}
    except EditFileError as exc:
        result = {"ok": False, "error": f"edit_failed: {exc}"}
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
