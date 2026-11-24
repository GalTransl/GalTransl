"""Concurrent backend instances must each replace the settings file with their own whole document."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Lock
from unittest import TestCase
from unittest.mock import patch

from GalTransl.AppSettings import load_app_settings, save_app_settings


class AtomicSettingsTests(TestCase):
    def test_concurrent_saves_each_replace_their_own_document(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            barrier = Barrier(2)
            lock = Lock()
            original_replace = os.replace
            written = []

            def synchronized_replace(source, destination):
                barrier.wait(timeout=10)
                with lock:
                    document = json.loads(Path(source).read_text(encoding="utf-8"))
                    written.append(document)
                    original_replace(source, destination)

            with patch.dict(os.environ, {"GALTRANSL_APP_SETTINGS_PATH": str(path)}):
                with patch("GalTransl.AppSettings.os.replace", side_effect=synchronized_replace):
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        saved = list(executor.map(save_app_settings, [
                            {"maxConcurrentJobs": 3, "printTranslationLogInTerminal": False},
                            {"maxConcurrentJobs": 7, "printTranslationLogInTerminal": True},
                        ]))
                # Only values that differ from the defaults are written.
                self.assertCountEqual(written, [
                    {"maxConcurrentJobs": 3, "printTranslationLogInTerminal": False, "storedFormat": 2},
                    {"maxConcurrentJobs": 7, "storedFormat": 2},
                ])
                # The last completed replace wins as a whole document.
                self.assertIn(load_app_settings(), saved)
                self.assertEqual(load_app_settings()["maxConcurrentJobs"], written[-1]["maxConcurrentJobs"])
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_failed_write_or_replace_preserves_previous_settings_and_cleans_up(self):
        for operation in ("json.dump", "os.replace"):
            with self.subTest(operation=operation), TemporaryDirectory() as tmp:
                path = Path(tmp) / "settings.json"
                with patch.dict(os.environ, {"GALTRANSL_APP_SETTINGS_PATH": str(path)}):
                    previous = save_app_settings({"maxConcurrentJobs": 3})
                    with patch(f"GalTransl.AppSettings.{operation}", side_effect=OSError("write failed")):
                        with self.assertRaises(OSError):
                            save_app_settings({"maxConcurrentJobs": 7})
                    self.assertEqual(load_app_settings(), previous)
                    self.assertEqual(list(Path(tmp).iterdir()), [path])
