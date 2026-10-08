"""Output reads resolve cache aliases without confusing output with cached text."""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch
from urllib.parse import quote

from GalTransl import OUTPUT_FOLDERNAME
from GalTransl.server import JobRegistry, build_handler
from GalTransl.server_runtime import encode_project_dir


class OutputCacheNameHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="galtransl-output-cache-name-")
        cls.project = cls.temp.name
        cls.output_dir = os.path.join(cls.project, OUTPUT_FOLDERNAME)
        os.makedirs(cls.output_dir)
        with open(os.path.join(cls.project, "config.yaml"), "w", encoding="utf-8") as file:
            file.write("common:\n  language: ja\nplugin:\n  filePlugin: file_galtransl_json\n  textPlugins: []\n")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(JobRegistry()))
        cls.base = (
            f"http://127.0.0.1:{cls.httpd.server_address[1]}/api/projects/"
            f"{encode_project_dir(cls.project)}"
        )
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join()
        cls.httpd.server_close()
        cls.temp.cleanup()

    def _write_output(self, name, message="final translation"):
        path = os.path.join(self.output_dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as file:
            json.dump([{"message": message}], file)
        self.addCleanup(os.remove, path)

    def _read(self, name):
        with urllib.request.urlopen(f"{self.base}/output/{quote(name, safe='')}", timeout=30) as response:
            return json.load(response)

    def test_cache_alias_reads_final_output_with_real_filename(self):
        def load_file(path):
            with open(path, encoding="utf-8") as file:
                return json.load(file)

        # Stub native script parsing; the remaining tests use the real JSON plugin.
        plugin = Mock(load_file=load_file)
        for name in ("quest_flags.ks", "sc_0_pr00.txt"):
            self._write_output(name)
            with self.subTest(name=name), patch("GalTransl.server._init_file_plugin", return_value=plugin):
                result = self._read(f"{name}.json")
                self.assertEqual(result["filename"], name)
                self.assertEqual(result["requested_filename"], f"{name}.json")
                self.assertEqual(result["entries"][0]["pre_src"], "final translation")

    def test_exact_output_name_takes_priority_over_cache_alias(self):
        self._write_output("script.ks", "other output")
        self._write_output("script.ks.json", "exact output")
        result = self._read("script.ks.json")
        self.assertEqual(result["filename"], "script.ks.json")
        self.assertNotIn("requested_filename", result)
        self.assertEqual(result["entries"][0]["pre_src"], "exact output")

    def test_nested_split_and_append_cache_names(self):
        self._write_output("chapter/scene.json")
        for name in (
            "chapter-}scene.json", "chapter-}scene.json_2.json",
            "chapter-}scene.json.append.jsonl",
        ):
            with self.subTest(name=name):
                result = self._read(name)
                self.assertEqual(result["filename"], "chapter/scene.json")
                self.assertEqual(result["count"], 1)

    def test_ambiguous_alias_suggests_candidates(self):
        self._write_output("chapter/scene")
        self._write_output("chapter/scene.json")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._read("chapter-}scene.json")
        self.assertEqual(ctx.exception.code, 409)
        payload = json.load(ctx.exception)
        self.assertEqual(payload["candidates"], ["chapter/scene", "chapter/scene.json"])
        self.assertIn("你是不是想读取", payload["error"])
        self.assertEqual(self._read("chapter/scene.json")["filename"], "chapter/scene.json")

    def test_missing_output_does_not_fall_back_to_input(self):
        from GalTransl import INPUT_FOLDERNAME

        os.makedirs(os.path.join(self.project, INPUT_FOLDERNAME), exist_ok=True)
        path = os.path.join(self.project, INPUT_FOLDERNAME, "missing.ks")
        with open(path, "w", encoding="utf-8") as file:
            json.dump([{"message": "source text"}], file)
        self.addCleanup(os.remove, path)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._read("missing.ks.json")
        self.assertEqual(ctx.exception.code, 404)

    def test_empty_output_suggests_rebuilding_even_with_empty_subdirectories(self):
        os.makedirs(os.path.join(self.output_dir, "empty"), exist_ok=True)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._read("quest_flags.ks.json")
        self.assertEqual(ctx.exception.code, 404)
        self.assertEqual(json.load(ctx.exception)["error"], "gt_output 是空的，请用rebuild重建结果。")

    def test_nonempty_output_keeps_missing_filename_error(self):
        self._write_output("chapter/other.json")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._read("quest_flags.ks.json")
        self.assertEqual(ctx.exception.code, 404)
        self.assertEqual(json.load(ctx.exception)["error"], "output file not found: quest_flags.ks.json")

    def test_alias_cannot_escape_output_directory(self):
        path = os.path.join(self.project, "outside.json")
        with open(path, "w", encoding="utf-8") as file:
            json.dump([{"message": "outside output directory"}], file)
        self.addCleanup(os.remove, path)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._read("..-}outside.json")
        self.assertEqual(ctx.exception.code, 404)

    def test_direct_paths_cannot_escape_output_directory(self):
        for name in ("../config.yaml", "..\\config.yaml", os.path.join(self.project, "config.yaml")):
            with self.subTest(name=name), self.assertRaises(urllib.error.HTTPError) as ctx:
                self._read(name)
            self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
