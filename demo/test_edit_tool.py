#!/usr/bin/env python3
"""
Unit tests for the text file editing tool.

The tests use temporary files only, so they need no network access and no
extra dependencies.

Usage:
  python -m unittest demo.test_edit_tool -v
  python -m demo.test_edit_tool   # Convenience alias
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from demo import edit_tool
from demo.edit_tool import (
    EditArgumentError,
    EditFileError,
    EditTool,
    TOOL_DESCRIPTION,
    TOOL_NAME,
    edit_file,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class _TempFileTestCase(unittest.TestCase):
    """Base class providing an isolated directory and helpers."""

    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.root = Path(self._tempdir.name).resolve()

    def write_file(
        self,
        name: str = "sample.txt",
        content: str = "",
        encoding: str = "utf-8",
    ) -> Path:
        path = self.root / name
        path.write_bytes(content.encode(encoding))
        return path

    def read_file(self, path: Path, encoding: str = "utf-8") -> str:
        return path.read_bytes().decode(encoding)


class TestEditFile(_TempFileTestCase):
    def test_replaces_a_single_occurrence(self) -> None:
        path = self.write_file(content="hello world\n")
        result = edit_file(str(path), "world", "there")
        self.assertTrue(result["ok"])
        self.assertEqual(result["replacements"], 1)
        self.assertEqual(result["line"], 1)
        self.assertEqual(result["file_path"], str(path))
        self.assertEqual(self.read_file(path), "hello there\n")

    def test_replaces_only_the_requested_occurrence_by_default(self) -> None:
        path = self.write_file(content="one two one\n")
        with self.assertRaises(EditFileError) as caught:
            edit_file(str(path), "one", "three")
        self.assertIn("occurs 2 times", str(caught.exception))
        self.assertEqual(self.read_file(path), "one two one\n")

    def test_replace_all_replaces_every_occurrence(self) -> None:
        path = self.write_file(content="a a a\n")
        result = edit_file(str(path), "a", "b", replace_all=True)
        self.assertEqual(result["replacements"], 3)
        self.assertEqual(self.read_file(path), "b b b\n")

    def test_missing_old_string_fails_without_writing(self) -> None:
        path = self.write_file(content="hello\n")
        with self.assertRaises(EditFileError) as caught:
            edit_file(str(path), "absent", "present")
        self.assertIn("not found", str(caught.exception))
        self.assertEqual(self.read_file(path), "hello\n")

    def test_identical_old_and_new_strings_fail(self) -> None:
        path = self.write_file(content="hello\n")
        with self.assertRaises(EditFileError) as caught:
            edit_file(str(path), "hello", "hello")
        self.assertIn("identical", str(caught.exception))

    def test_empty_old_string_is_rejected(self) -> None:
        path = self.write_file(content="hello\n")
        with self.assertRaises(EditArgumentError):
            edit_file(str(path), "", "x")

    def test_missing_file_fails(self) -> None:
        with self.assertRaises(EditFileError) as caught:
            edit_file(str(self.root / "missing.txt"), "a", "b")
        self.assertIn("does not exist", str(caught.exception))

    def test_directory_is_rejected(self) -> None:
        with self.assertRaises(EditArgumentError) as caught:
            edit_file(str(self.root), "a", "b")
        self.assertIn("directory", str(caught.exception))

    def test_binary_file_is_rejected(self) -> None:
        path = self.root / "image.bin"
        path.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
        with self.assertRaises(EditFileError) as caught:
            edit_file(str(path), "PNG", "JPG")
        self.assertIn("binary", str(caught.exception))
        self.assertTrue(path.read_bytes().startswith(b"\x89PNG"))

    def test_wrong_encoding_is_reported(self) -> None:
        path = self.write_file(content="héllo", encoding="latin-1")
        with self.assertRaises(EditFileError) as caught:
            edit_file(str(path), "héllo", "hello")
        self.assertIn("not valid utf-8 text", str(caught.exception))

    def test_named_encoding_round_trips(self) -> None:
        path = self.write_file(content="café\n", encoding="latin-1")
        result = edit_file(str(path), "café", "cafe", encoding="latin-1")
        self.assertEqual(result["encoding"], "latin-1")
        self.assertEqual(self.read_file(path, "latin-1"), "cafe\n")

    def test_unknown_encoding_is_rejected(self) -> None:
        path = self.write_file(content="hello\n")
        with self.assertRaises(EditArgumentError) as caught:
            edit_file(str(path), "hello", "hi", encoding="not-a-codec")
        self.assertIn("unknown encoding", str(caught.exception))

    def test_new_string_must_fit_the_encoding(self) -> None:
        path = self.write_file(content="café\n", encoding="latin-1")
        with self.assertRaises(EditFileError) as caught:
            edit_file(str(path), "café", "你好", encoding="latin-1")
        self.assertIn("cannot be encoded", str(caught.exception))
        self.assertEqual(self.read_file(path, "latin-1"), "café\n")

    def test_crlf_line_endings_are_preserved(self) -> None:
        path = self.write_file(content="first\r\nsecond\r\n")
        edit_file(str(path), "second", "third")
        self.assertEqual(self.read_file(path), "first\r\nthird\r\n")

    def test_missing_trailing_newline_is_preserved(self) -> None:
        path = self.write_file(content="no newline")
        edit_file(str(path), "newline", "newline!")
        self.assertEqual(self.read_file(path), "no newline!")

    def test_multiline_replacement(self) -> None:
        path = self.write_file(content="alpha\nbeta\ngamma\n")
        edit_file(str(path), "beta\ngamma", "delta\nepsilon")
        self.assertEqual(self.read_file(path), "alpha\ndelta\nepsilon\n")

    def test_empty_new_string_deletes_text(self) -> None:
        path = self.write_file(content="keep remove keep\n")
        edit_file(str(path), " remove", "")
        self.assertEqual(self.read_file(path), "keep keep\n")

    def test_reports_line_of_the_match(self) -> None:
        path = self.write_file(content="one\ntwo\nthree\n")
        result = edit_file(str(path), "three", "THREE")
        self.assertEqual(result["line"], 3)

    def test_dry_run_reports_change_without_writing(self) -> None:
        path = self.write_file(content="hello\n")
        result = edit_file(str(path), "hello", "hi", dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["changed"])
        self.assertEqual(result["replacements"], 1)
        self.assertEqual(self.read_file(path), "hello\n")

    def test_file_mode_is_preserved(self) -> None:
        path = self.write_file(content="hello\n")
        os.chmod(path, 0o640)
        edit_file(str(path), "hello", "hi")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_unicode_content_round_trips(self) -> None:
        path = self.write_file(content="你好，世界\n")
        edit_file(str(path), "世界", "飞书")
        self.assertEqual(self.read_file(path), "你好，飞书\n")

    def test_symlink_is_preserved_and_target_is_edited(self) -> None:
        target = self.write_file("target.txt", "hello\n")
        link = self.root / "link.txt"
        link.symlink_to(target)
        result = edit_file(str(link), "hello", "hi")
        self.assertEqual(result["file_path"], str(target))
        self.assertTrue(link.is_symlink())
        self.assertEqual(self.read_file(target), "hi\n")

    def test_concurrent_change_is_detected(self) -> None:
        path = self.write_file(content="hello\n")
        real_read = edit_tool._read_text

        def read_then_tamper(p: Path, encoding: str):
            text, info = real_read(p, encoding)
            # Simulate another writer between the read and the write.
            p.write_bytes(b"hello, world\n")
            return text, info

        with mock.patch.object(edit_tool, "_read_text", side_effect=read_then_tamper):
            with self.assertRaises(EditFileError) as caught:
                edit_file(str(path), "hello", "hi")
        self.assertIn("changed", str(caught.exception))
        self.assertEqual(self.read_file(path), "hello, world\n")

    def test_no_temporary_file_is_left_behind(self) -> None:
        path = self.write_file(content="hello\n")
        edit_file(str(path), "hello", "hi")
        leftovers = [item.name for item in self.root.iterdir() if item.name != path.name]
        self.assertEqual(leftovers, [])

    def test_result_is_json_serializable(self) -> None:
        path = self.write_file(content="hello\n")
        result = edit_file(str(path), "hello", "hi")
        json.dumps(result)


class TestEditTool(_TempFileTestCase):
    def test_metadata_describes_the_tool(self) -> None:
        tool = EditTool()
        self.assertEqual(tool.name, TOOL_NAME)
        self.assertEqual(tool.description, TOOL_DESCRIPTION)
        self.assertEqual(
            tool.input_schema["required"], ["file_path", "old_string", "new_string"]
        )

    def test_invoke_returns_tool_result(self) -> None:
        path = self.write_file(content="hello\n")
        result = EditTool().invoke(
            {"file_path": str(path), "old_string": "hello", "new_string": "hi"}
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["replacements"], 1)
        self.assertEqual(self.read_file(path), "hi\n")

    def test_invoke_non_mapping_returns_error(self) -> None:
        result = EditTool().invoke("not-an-object")  # type: ignore[arg-type]
        self.assertFalse(result["ok"])
        self.assertIn("invalid_arguments", result["error"])

    def test_invoke_unknown_argument_returns_error(self) -> None:
        result = EditTool().invoke(
            {"file_path": "x", "old_string": "a", "new_string": "b", "oops": 1}
        )
        self.assertFalse(result["ok"])
        self.assertIn("unknown argument(s): oops", result["error"])

    def test_invoke_missing_required_argument_returns_error(self) -> None:
        result = EditTool().invoke({"file_path": "x"})
        self.assertFalse(result["ok"])
        self.assertIn("invalid_arguments", result["error"])

    def test_invoke_non_boolean_flag_returns_error(self) -> None:
        path = self.write_file(content="hello\n")
        result = EditTool().invoke(
            {
                "file_path": str(path),
                "old_string": "hello",
                "new_string": "hi",
                "replace_all": "yes",
            }
        )
        self.assertFalse(result["ok"])
        self.assertIn("must be a boolean", result["error"])

    def test_invoke_edit_failure_returns_error(self) -> None:
        path = self.write_file(content="hello\n")
        result = EditTool().invoke(
            {"file_path": str(path), "old_string": "absent", "new_string": "x"}
        )
        self.assertFalse(result["ok"])
        self.assertIn("edit_failed", result["error"])


class TestEditToolCli(_TempFileTestCase):
    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "demo.edit_tool", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_cli_edits_file_and_prints_json(self) -> None:
        path = self.write_file(content="hello world\n")
        completed = self._run(str(path), "--old", "world", "--new", "there")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(result["ok"])
        self.assertEqual(result["replacements"], 1)
        self.assertEqual(self.read_file(path), "hello there\n")

    def test_cli_dry_run_does_not_modify_file(self) -> None:
        path = self.write_file(content="hello\n")
        completed = self._run(
            str(path), "--old", "hello", "--new", "hi", "--dry-run"
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(result["dry_run"])
        self.assertEqual(self.read_file(path), "hello\n")

    def test_cli_empty_new_string_deletes_text(self) -> None:
        path = self.write_file(content="keep remove\n")
        completed = self._run(str(path), "--old", " remove", "--new", "")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.read_file(path), "keep\n")

    def test_cli_missing_file_exits_nonzero(self) -> None:
        completed = self._run(
            str(self.root / "missing.txt"), "--old", "a", "--new", "b"
        )
        self.assertEqual(completed.returncode, 1)
        result = json.loads(completed.stdout)
        self.assertFalse(result["ok"])
        self.assertIn("edit_failed", result["error"])

    def test_cli_unknown_option_exits_two(self) -> None:
        path = self.write_file(content="hello\n")
        completed = self._run(str(path), "--old", "hello", "--new", "hi", "--bogus")
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(self.read_file(path), "hello\n")

    def test_cli_pretty_prints_json(self) -> None:
        path = self.write_file(content="hello\n")
        completed = self._run(
            str(path), "--old", "hello", "--new", "hi", "--pretty"
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("\n  ", completed.stdout)


if __name__ == "__main__":
    unittest.main()
