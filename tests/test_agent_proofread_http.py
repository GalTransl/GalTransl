"""Cache saving must distinguish verified checks from a failed detector."""

import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from GalTransl import CACHE_FOLDERNAME
from GalTransl.server import JobRegistry, build_handler
from GalTransl.server_runtime import encode_project_dir


class ProofreadHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.project = Path(cls.directory.name)
        (cls.project / CACHE_FOLDERNAME).mkdir()
        (cls.project / "config.yaml").write_text("common: {}\n", encoding="utf-8")
        cls.cache = cls.project / CACHE_FOLDERNAME / "a.json"
        cls.cache.write_text("[]", encoding="utf-8")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(JobRegistry()))
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.httpd.server_port}/api/projects/{encode_project_dir(str(cls.project))}/cache/save"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(3)
        cls.directory.cleanup()

    def save(self, detector):
        config = SimpleNamespace(getDictCfgSection=lambda: {}, tPlugins=[])
        body = {"filename": "a.json", "entries": [{
            "index": 1, "pre_src": "原文", "post_src": "原文", "pre_dst": "修复后的译文", "problem": "old warning",
        }]}
        with (
            patch("GalTransl.ConfigHelper.CProjectConfig", return_value=config),
            patch("GalTransl.Frontend.LLMTranslate.preprocess_trans_list"),
            patch("GalTransl.Frontend.LLMTranslate.postprocess_trans_list"),
            patch("GalTransl.Problem.finalize_problem_plugins"),
            patch("GalTransl.Problem.find_problems", side_effect=detector),
        ):
            request = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.load(response)

    def test_failed_detection_preserves_warning_and_reports_unverified(self):
        result = self.save(RuntimeError("detector failed"))
        self.assertTrue(result["success"])
        self.assertEqual(result["verification"], "unknown")
        saved = json.loads(self.cache.read_text(encoding="utf-8"))[0]
        self.assertEqual(saved["pre_dst"], "修复后的译文")
        self.assertEqual(saved["problem"], "old warning")

    def test_successful_detection_can_clear_old_warning(self):
        result = self.save(lambda *args: None)
        self.assertEqual(result["verification"], "checked")
        self.assertNotIn("problem", result["entries"][0])


if __name__ == "__main__":
    unittest.main()
