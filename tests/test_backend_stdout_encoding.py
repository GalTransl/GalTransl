"""The backend must survive printing characters its stdout encoding cannot represent.

Regression: the packaged backend is a console exe that Tauri starts with stdout sent to NUL. Python then
encodes stdout with the locale code page (GBK on Chinese Windows), and the tool templates print the
model's output, so a translation containing "♪", "♡" or an emoji raised UnicodeEncodeError and the
batch failed (in auto mode it counted as a template failure).
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(code: str) -> subprocess.CompletedProcess:
    # PYTHONIOENCODING=gbk reproduces a Chinese-locale Windows on any machine.
    return subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, timeout=30,
        env={**os.environ, "PYTHONIOENCODING": "gbk"},
    )


class BackendStdoutEncodingTests(unittest.TestCase):
    def test_unencodable_characters_are_replaced_instead_of_raising(self):
        result = _run("import run_backend; run_backend._tolerate_unencodable_output(); print('译文♪'); print('♡', file=__import__('sys').stderr)")
        self.assertEqual(result.returncode, 0, result.stderr.decode("gbk", "replace"))
        # The locale encoding is kept so Chinese stays readable in a GBK console.
        self.assertEqual(result.stdout.decode("gbk").strip(), "译文?")
        self.assertEqual(result.stderr.decode("gbk").strip(), "?")

    def test_without_the_guard_the_same_print_fails(self):
        # Documents the failure the guard exists for; if Python ever stops raising here the guard can go.
        result = _run("print('♪')")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"UnicodeEncodeError", result.stderr)


if __name__ == "__main__":
    unittest.main()
