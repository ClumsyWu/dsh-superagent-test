#!/usr/bin/env python3
"""
Unit tests for the read file tool.

The tests use temporary files, so they need no network access and no extra
dependencies.

Usage:
  python -m unittest demo.test_read_file_tool -v
  python -m demo.test_read_file_tool   # Convenience alias
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from demo.read_file_tool import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    ReadFileTool,
    ReadFileToolError,
    TOOL_DESCRIPTION,
    TOOL_INPUT_SCHEMA,
    TOOL_NAME,
    read_file,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class _TempFileTestCase(unittest.TestCase):
    """Base class providing a temporary directory and file helpers."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmpdir = Path(self._tmpdir.name)

    def write(self, data: str | bytes, name: str = "file.txt") -> Path:
        path = self.tmpdir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, str):
            path.write_text(data, encoding="utf-8")
        else:
            path.write_bytes(data)
        return path


class ReadFileTests(_TempFileTestCase):
    def test_reads_whole_file(self) -> None:
        path = self.write("alpha\nbeta\ngamma\n")

        result = read_file(str(path))

        self.assertTrue(result["ok"])
        self.assertEqual(result["content"], "alpha\nbeta\ngamma\n")
        self.assertEqual(result["path"], str(path.resolve()))
        self.assertEqual(result["size_bytes"], len(b"alpha\nbeta\ngamma\n"))
        self.assertEqual(result["encoding"], "utf-8")
        self.assertEqual(result["offset"], 1)
        self.assertEqual(result["limit"], DEFAULT_LIMIT)
        self.assertEqual(result["line_count"], 3)
        self.assertIsNone(result["next_offset"])
        self.assertFalse(result["has_more"])
        self.assertFalse(result["truncated"])
        self.assertIsNone(result["truncated_by"])
        self.assertFalse(result["partial_line"])

    def test_preserves_missing_trailing_newline(self) -> None:
        path = self.write("one\ntwo")

        result = read_file(str(path))

        self.assertEqual(result["content"], "one\ntwo")
        self.assertEqual(result["line_count"], 2)
        self.assertFalse(result["has_more"])

    def test_preserves_crlf_line_endings(self) -> None:
        path = self.write(b"first\r\nsecond\r\n")

        result = read_file(str(path))

        self.assertEqual(result["content"], "first\r\nsecond\r\n")
        self.assertEqual(result["line_count"], 2)

    def test_reads_unicode_content(self) -> None:
        path = self.write("你好\n世界\n")

        result = read_file(str(path))

        self.assertEqual(result["content"], "你好\n世界\n")
        self.assertEqual(result["size_bytes"], len("你好\n世界\n".encode()))

    def test_empty_file(self) -> None:
        path = self.write("")

        result = read_file(str(path))

        self.assertTrue(result["ok"])
        self.assertEqual(result["content"], "")
        self.assertEqual(result["line_count"], 0)
        self.assertEqual(result["size_bytes"], 0)
        self.assertFalse(result["has_more"])
        self.assertIsNone(result["next_offset"])

    def test_offset_and_limit(self) -> None:
        path = self.write("one\ntwo\nthree\nfour\n")

        result = read_file(str(path), offset=2, limit=2)

        self.assertEqual(result["content"], "two\nthree\n")
        self.assertEqual(result["offset"], 2)
        self.assertEqual(result["line_count"], 2)
        self.assertTrue(result["has_more"])
        self.assertEqual(result["next_offset"], 4)
        self.assertEqual(result["truncated_by"], "limit")

    def test_limit_reaching_eof_is_not_truncation(self) -> None:
        path = self.write("one\ntwo\nthree\n")

        result = read_file(str(path), offset=2, limit=2)

        self.assertEqual(result["content"], "two\nthree\n")
        self.assertFalse(result["has_more"])
        self.assertFalse(result["truncated"])
        self.assertIsNone(result["truncated_by"])
        self.assertIsNone(result["next_offset"])

    def test_offset_beyond_eof_returns_empty_content(self) -> None:
        path = self.write("one\ntwo\n")

        result = read_file(str(path), offset=10)

        self.assertTrue(result["ok"])
        self.assertEqual(result["content"], "")
        self.assertEqual(result["line_count"], 0)
        self.assertFalse(result["has_more"])

    def test_pagination_walk_reconstructs_file(self) -> None:
        full = "".join(f"line{i}\n" for i in range(1, 8))
        path = self.write(full)

        chunks: list[str] = []
        offset = 1
        for _ in range(10):
            result = read_file(str(path), offset=offset, limit=2)
            self.assertTrue(result["ok"])
            chunks.append(result["content"])
            if not result["has_more"]:
                break
            self.assertIsNotNone(result["next_offset"])
            offset = result["next_offset"]
        else:  # pragma: no cover - guards against a pagination loop bug
            self.fail("pagination did not terminate")

        self.assertEqual("".join(chunks), full)

    def test_default_limit_caps_long_files(self) -> None:
        lines = 2500
        path = self.write("".join(f"{i}\n" for i in range(lines)))

        result = read_file(str(path))

        self.assertEqual(result["line_count"], DEFAULT_LIMIT)
        self.assertTrue(result["has_more"])
        self.assertEqual(result["truncated_by"], "limit")
        self.assertEqual(result["next_offset"], DEFAULT_LIMIT + 1)

    def test_max_bytes_stops_before_a_line(self) -> None:
        path = self.write("aaaa\nbbbb\ncccc\n")

        result = read_file(str(path), max_bytes=10)

        self.assertEqual(result["content"], "aaaa\nbbbb\n")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["truncated_by"], "max_bytes")
        self.assertTrue(result["has_more"])
        self.assertEqual(result["next_offset"], 3)
        self.assertFalse(result["partial_line"])

    def test_max_bytes_continues_from_next_offset(self) -> None:
        path = self.write("aaaa\nbbbb\ncccc\n")

        first = read_file(str(path), max_bytes=10)
        second = read_file(str(path), offset=first["next_offset"], max_bytes=10)

        self.assertEqual(second["content"], "cccc\n")
        self.assertFalse(second["has_more"])

    def test_max_bytes_exact_fit_is_not_truncated(self) -> None:
        path = self.write("abcde\n")

        result = read_file(str(path), max_bytes=6)

        self.assertEqual(result["content"], "abcde\n")
        self.assertFalse(result["has_more"])
        self.assertFalse(result["partial_line"])

    def test_single_line_larger_than_max_bytes_is_truncated(self) -> None:
        path = self.write("x" * 100 + "\n")

        result = read_file(str(path), max_bytes=10)

        self.assertEqual(result["content"], "x" * 10)
        self.assertEqual(result["line_count"], 1)
        self.assertTrue(result["partial_line"])
        self.assertTrue(result["has_more"])
        self.assertEqual(result["truncated_by"], "max_bytes")
        self.assertIsNone(result["next_offset"])

    def test_partial_line_truncation_keeps_multibyte_characters_intact(self) -> None:
        path = self.write("你" * 100)

        result = read_file(str(path), max_bytes=10)

        self.assertEqual(result["content"], "你" * 3)
        self.assertNotIn("\ufffd", result["content"])
        self.assertTrue(result["partial_line"])

    def test_utf16_encoding_is_readable(self) -> None:
        path = self.write("héllo\nworld\n".encode("utf-16"))

        result = read_file(str(path), encoding="utf-16")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["content"], "héllo\nworld\n")
        self.assertEqual(result["line_count"], 2)

    def test_binary_file_is_rejected(self) -> None:
        path = self.write(b"\x00\x01\x02binary data")

        result = read_file(str(path))

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "binary_file")
        self.assertIn("binary", result["error"])

    def test_missing_file_returns_structured_error(self) -> None:
        missing = self.tmpdir / "does-not-exist.txt"

        result = read_file(str(missing))

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "not_found")
        self.assertIn("does-not-exist.txt", result["error"])

    def test_directory_is_rejected(self) -> None:
        directory = self.tmpdir / "a-directory"
        directory.mkdir()

        result = read_file(str(directory))

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "is_directory")

    def test_symlink_is_resolved(self) -> None:
        target = self.write("linked content\n", name="target.txt")
        link = self.tmpdir / "link.txt"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):  # pragma: no cover - platform
            self.skipTest("symlinks are not supported on this platform")

        result = read_file(str(link))

        self.assertTrue(result["ok"])
        self.assertEqual(result["content"], "linked content\n")
        self.assertEqual(result["path"], str(target.resolve()))

    def test_permission_denied_returns_structured_error(self) -> None:
        if os.name != "posix" or os.geteuid() == 0:  # pragma: no cover - platform
            self.skipTest("permission checks need a non-root POSIX user")
        path = self.write("secret\n")
        path.chmod(0o000)
        self.addCleanup(path.chmod, 0o600)

        result = read_file(str(path))

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "permission")

    def test_invalid_arguments_raise(self) -> None:
        path = self.write("content\n")
        cases = [
            ("empty path", {"path": ""}),
            ("non-string path", {"path": 123}),
            ("NUL in path", {"path": "a\x00b"}),
            ("zero offset", {"path": str(path), "offset": 0}),
            ("boolean offset", {"path": str(path), "offset": True}),
            ("float offset", {"path": str(path), "offset": 1.5}),
            ("zero limit", {"path": str(path), "limit": 0}),
            ("limit over maximum", {"path": str(path), "limit": MAX_LIMIT + 1}),
            ("zero max_bytes", {"path": str(path), "max_bytes": 0}),
            ("unknown encoding", {"path": str(path), "encoding": "not-a-codec"}),
            ("non-text encoding", {"path": str(path), "encoding": "base64"}),
            ("non-string encoding", {"path": str(path), "encoding": 7}),
        ]
        for label, arguments in cases:
            with self.subTest(label):
                with self.assertRaises(ReadFileToolError):
                    read_file(**arguments)


class ReadFileToolTests(_TempFileTestCase):
    def test_tool_metadata(self) -> None:
        tool = ReadFileTool()

        self.assertEqual(tool.name, TOOL_NAME)
        self.assertEqual(tool.description, TOOL_DESCRIPTION)
        self.assertEqual(tool.input_schema, TOOL_INPUT_SCHEMA)
        self.assertEqual(tool.input_schema["required"], ["path"])
        self.assertFalse(tool.input_schema["additionalProperties"])

    def test_tool_invoke_reads_file(self) -> None:
        path = self.write("one\ntwo\nthree\n")

        result = ReadFileTool().invoke({"path": str(path), "offset": 2, "limit": 1})

        self.assertTrue(result["ok"])
        self.assertEqual(result["content"], "two\n")
        self.assertEqual(result["next_offset"], 3)

    def test_tool_rejects_unknown_arguments(self) -> None:
        path = self.write("content\n")

        result = ReadFileTool().invoke({"path": str(path), "bogus": True})

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "invalid_arguments")
        self.assertIn("bogus", result["error"])

    def test_tool_rejects_missing_path(self) -> None:
        result = ReadFileTool().invoke({})

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "invalid_arguments")
        self.assertIn("path", result["error"])

    def test_tool_rejects_non_object_arguments(self) -> None:
        result = ReadFileTool().invoke("not an object")  # type: ignore[arg-type]

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "invalid_arguments")


class ReadFileCliTests(_TempFileTestCase):
    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "demo.read_file_tool", *arguments],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_cli_prints_json_result(self) -> None:
        path = self.write("one\ntwo\nthree\n")

        completed = self.run_cli(str(path), "--offset", "2", "--limit", "1", "--pretty")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(result["ok"])
        self.assertEqual(result["content"], "two\n")
        self.assertEqual(result["line_count"], 1)

    def test_cli_uses_custom_encoding(self) -> None:
        path = self.tmpdir / "utf16.txt"
        path.write_bytes("hello\n".encode("utf-16"))

        completed = self.run_cli(str(path), "--encoding", "utf-16")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["content"], "hello\n")

    def test_cli_missing_file_exits_nonzero(self) -> None:
        completed = self.run_cli(str(self.tmpdir / "missing.txt"))

        self.assertEqual(completed.returncode, 1)
        result = json.loads(completed.stdout)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "not_found")

    def test_cli_invalid_arguments_exit_nonzero(self) -> None:
        path = self.write("content\n")

        completed = self.run_cli(str(path), "--offset", "0")

        self.assertEqual(completed.returncode, 1)
        result = json.loads(completed.stdout)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "invalid_arguments")


if __name__ == "__main__":
    unittest.main()
