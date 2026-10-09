import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from GalTransl import CACHE_FOLDERNAME, INPUT_FOLDERNAME, OUTPUT_FOLDERNAME
from GalTransl.server import JobRegistry, build_handler, _search_output_dir
from GalTransl.server_runtime import encode_project_dir


class OutputSearchHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="galtransl-output-search-")
        cls.project = Path(cls.temp.name)
        cls.output = cls.project / OUTPUT_FOLDERNAME
        for folder in (INPUT_FOLDERNAME, OUTPUT_FOLDERNAME, CACHE_FOLDERNAME):
            (cls.project / folder).mkdir()
        (cls.project / "config.yaml").write_text(
            "common:\n  language: ja\nplugin:\n  filePlugin: file_galtransl_json\n  textPlugins: []\n", encoding="utf-8")
        (cls.output / "chapter").mkdir()
        (cls.output / "a.json").write_text(json.dumps([
            {"name": "少女", "message": "上文"},
            {"name": "爱丽丝", "message": "最终译名 FINAL、再见"},
            {"name": "少女", "message": "下文 final"},
        ], ensure_ascii=False), encoding="utf-8")
        (cls.output / "chapter" / "b.json").write_text(
            json.dumps([{"name": "final", "message": "另一处 FINAL"}]), encoding="utf-8")
        (cls.project / INPUT_FOLDERNAME / "a.json").write_text(
            json.dumps([{"message": "原文独有"}]), encoding="utf-8")
        (cls.project / CACHE_FOLDERNAME / "a.json").write_text(
            json.dumps([{"index": 1, "pre_dst": "缓存独有"}]), encoding="utf-8")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(JobRegistry()))
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}/api/projects/{encode_project_dir(str(cls.project))}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join()
        cls.httpd.server_close()
        cls.temp.cleanup()

    def _search(self, **payload):
        request = urllib.request.Request(f"{self.base}/output/search", method="POST",
                                         data=json.dumps({"query": "final", **payload}).encode(),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    def test_reads_actual_output_including_nested_files_and_not_input_or_cache(self):
        result = self._search(field="dst")
        self.assertEqual(result["total"], 3)
        self.assertEqual([row["filename"] for row in result["results"]], ["a.json", "a.json", "chapter/b.json"])
        self.assertEqual(result["results"][0]["dst"], "最终译名 FINAL、再见")
        self.assertEqual(result["results"][0]["index"], 2)
        self.assertEqual(result["source"], "output")
        for query in ("原文独有", "缓存独有"):
            self.assertEqual(self._search(query=query)["total"], 0)

    def test_speaker_filter_and_output_read_use_the_same_indexes(self):
        result = self._search(query="爱丽丝", field="name")
        self.assertEqual(result["total"], 1)
        hit = result["results"][0]
        with urllib.request.urlopen(f"{self.base}/output/{quote(hit['filename'])}", timeout=30) as response:
            entries = json.load(response)["entries"]
        self.assertEqual(next(row["pre_src"] for row in entries if row["index"] == hit["index"]), hit["dst"])

    def test_context_paging_counts_hits_and_deduplicates_context(self):
        result = self._search(filename="a.json", field="dst", context=1, max_results=1)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["returned_hits"], 1)
        self.assertEqual([row["index"] for row in result["results"]], [1, 2, 3])
        self.assertEqual([row["match_dst"] for row in result["results"]], [False, True, False])
        page = self._search(filename="a.json", field="dst", context=1, preceding_only=True, offset=1, max_results=1)
        self.assertEqual([row["index"] for row in page["results"]], [2, 3])
        self.assertEqual(page["total"], 2)
        self.assertEqual(self._search(context=1)["returned"], 4)

    def test_filename_scopes_to_actual_nested_output_path(self):
        result = self._search(filename="chapter/b.json")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["results"][0]["filename"], "chapter/b.json")
        missing = self._search(filename="missing.json")
        self.assertEqual(missing["total"], 0)
        self.assertIn("不存在", missing["note"])
        self.assertEqual(self._search(filename="../config.yaml")["total"], 0)

    def test_sampling_uses_all_hits_before_limiting(self):
        result = self._search(order="reverse", max_results=1)
        self.assertEqual(result["results"][0]["filename"], "chapter/b.json")
        result = self._search(order="even", max_results=2)
        self.assertEqual([row["filename"] for row in result["results"]], ["a.json", "chapter/b.json"])
        with patch("GalTransl.Search.random.sample", side_effect=lambda population, count: population[-count:]):
            result = self._search(order="random", max_results=1)
        self.assertEqual(result["results"][0]["filename"], "chapter/b.json")
        self.assertEqual(result["total"], 3)

    def test_parse_failures_are_reported_and_other_files_still_searchable(self):
        broken = self.output / "broken.json"
        broken.write_text("not json", encoding="utf-8")
        self.addCleanup(broken.unlink)
        result = self._search()
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["files_failed"], ["broken.json"])

    def test_invalid_fields_context_order_and_regex_are_rejected(self):
        for payload in ({"field": "src"}, {"context": "bad"}, {"order": "bad"},
                        {"query": "(", "options": {"re": True}}):
            with self.subTest(payload=payload), self.assertRaises(urllib.error.HTTPError) as ctx:
                self._search(**payload)
            self.assertEqual(ctx.exception.code, 400)

    def test_regex_and_empty_query_follow_input_search_contract(self):
        self.assertEqual(self._search(query="FINAL|final", options={"re": True})["total"], 3)
        self.assertEqual(self._search(query="   "), {"results": [], "total": 0})

    def test_get_search_is_still_a_file_read(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"{self.base}/output/search", timeout=30)
        self.assertEqual(ctx.exception.code, 404)

    def test_missing_or_empty_output_suggests_rebuilding(self):
        with tempfile.TemporaryDirectory(prefix="galtransl-empty-output-") as project:
            for create in (False, True):
                if create:
                    (Path(project) / OUTPUT_FOLDERNAME / "empty").mkdir(parents=True)
                result = _search_output_dir(project, "config.yaml", query="final")
                self.assertEqual(result["total"], 0)
                self.assertIn("gt_output 是空的", result["note"])

    def test_search_reads_updated_delivery_instead_of_reusing_old_text(self):
        path = self.output / "updated.json"
        self.addCleanup(path.unlink)
        for message in ("before", "after"):
            path.write_text(json.dumps([{"message": message}]), encoding="utf-8")
            result = self._search(filename="updated.json", query=message)
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["results"][0]["dst"], message)
        self.assertEqual(self._search(filename="updated.json", query="before")["total"], 0)


if __name__ == "__main__":
    unittest.main()
