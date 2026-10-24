"""缓存搜索的同条目 AND、精确字段、HTTP 校验及有界续查。"""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from GalTransl import CACHE_FOLDERNAME
from GalTransl.server import JobRegistry, build_handler
from GalTransl.server_runtime import encode_project_dir


class CacheSearchConditionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="cache-search-conditions-")
        cls.project = cls.tmp.name
        os.makedirs(os.path.join(cls.project, CACHE_FOLDERNAME))
        with open(os.path.join(cls.project, "config.yaml"), "w", encoding="utf-8") as f:
            f.write("common:\n  language: zh-cn\n")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(JobRegistry()))
        cls.url = f"http://127.0.0.1:{cls.httpd.server_address[1]}/api/projects/{encode_project_dir(cls.project)}/cache/search"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join()
        cls.tmp.cleanup()

    def setUp(self):
        self.write([
            {"index": 1, "pre_src": "原始词", "post_src": "悶え ABC", "pre_dst": "挣扎", "proofread_dst": "润色"},
            {"index": 2, "post_src": "悶え", "pre_dst": "呻吟"},
            {"index": 3, "post_src": "普通", "pre_dst": "挣扎"},
            {"index": 4, "post_src": "悶え", "pre_dst": "", "proofread_dst": "挣扎"},
            {"index": 5, "post_jp": "悶え", "pre_zh": "挣扎"},
        ])

    def write(self, entries):
        with open(os.path.join(self.project, CACHE_FOLDERNAME, "a.json"), "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False)

    def search(self, **body):
        request = urllib.request.Request(self.url, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)

    def test_conditions_match_the_same_entry_and_old_keys(self):
        out = self.search(conditions=[{"field": "post_src", "query": "悶え"}, {"field": "pre_dst", "query": "挣扎"}])
        self.assertEqual([r["index"] for r in out["results"]], [1, 5])
        self.assertTrue(all(r["match_src"] and r["match_dst"] for r in out["results"]))

    def test_primary_query_is_an_additional_condition_and_case_insensitive(self):
        out = self.search(query="abc", field="post_src", conditions=[{"field": "pre_dst", "query": "挣扎"}])
        self.assertEqual([r["index"] for r in out["results"]], [1])

    def test_exact_fields_do_not_fall_back_to_other_columns(self):
        out = self.search(conditions=[{"field": "post_src", "query": "原始词"}])
        self.assertEqual(out["total"], 0)
        out = self.search(conditions=[{"field": "pre_src", "query": "原始词"}])
        self.assertEqual([r["index"] for r in out["results"]], [1])
        out = self.search(query="挣扎", field="dst")
        self.assertIn(4, [r["index"] for r in out["results"]])

    def test_each_condition_can_use_regex_independently(self):
        out = self.search(conditions=[{"field": "post_src", "query": "^悶. ABC$", "options": {"re": True}},
                                      {"field": "pre_dst", "query": "挣扎"}])
        self.assertEqual([r["index"] for r in out["results"]], [1])

    def test_invalid_conditions_are_bad_requests(self):
        for conditions in ([], {}, [None], [{"field": "unknown", "query": "x"}],
                           [{"field": "pre_dst", "query": ""}],
                           [{"field": "pre_dst", "query": "[", "options": {"re": True}}],
                           [{"field": "pre_dst", "query": "x"}] * 21):
            with self.subTest(conditions=conditions):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.search(conditions=conditions)
                self.assertEqual(caught.exception.code, 400)

    def test_budgets_keep_all_matches_reachable_without_duplicates(self):
        self.write([{"index": i, "post_src": "hit", "pre_dst": "译文" * 100} for i in range(1, 206)])
        indexes = []
        offset = 0
        while True:
            out = self.search(query="hit", max_results=9999, offset=offset)
            self.assertLessEqual(len(out["results"]), 100)
            self.assertLessEqual(len(json.dumps(out, ensure_ascii=False)), 10000)
            self.assertEqual(out["total"], 205)
            indexes.extend(r["index"] for r in out["results"])
            if not out["has_more"]:
                break
            self.assertGreater(out["next_offset"], offset)
            offset = out["next_offset"]
        self.assertEqual(indexes, list(range(1, 206)))

    def test_frontend_returns_all_matches_and_full_text_without_budgets(self):
        text = "译文" * 10000
        self.write([{"index": i, "post_src": "hit", "pre_dst": text} for i in range(1, 206)]
                   + [{"index": 206, "post_src": "hit", "pre_dst": "不匹配"}])
        out = self.search(query="hit", result_mode="all",
                          conditions=[{"field": "pre_dst", "query": "译文"}])
        self.assertEqual(out["total"], 205)
        self.assertEqual([r["index"] for r in out["results"]], list(range(1, 206)))
        self.assertTrue(all(r["pre_dst"] == text and not r.get("truncated") for r in out["results"]))
        self.assertFalse(out["has_more"])
        self.assertNotIn("truncated", out)

    def test_frontend_empty_results_are_complete(self):
        out = self.search(query="不存在", result_mode="all")
        self.assertEqual(out["results"], [])
        self.assertEqual(out["total"], 0)
        self.assertFalse(out["has_more"])

    def test_invalid_result_mode_is_bad_request(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.search(query="hit", result_mode="unknown")
        self.assertEqual(caught.exception.code, 400)

    def test_context_counts_toward_both_budgets_and_paging_counts_only_hits(self):
        self.write([{"index": i, "post_src": "hit" if i % 5 == 0 else "context", "pre_dst": "译文" * 50}
                    for i in range(1, 121)])
        indexes = []
        offset = 0
        while True:
            out = self.search(query="hit", context=3, max_results=100, offset=offset)
            hits = [r for r in out["results"] if r["match_src"]]
            self.assertEqual(len(hits), out["returned_hits"])
            self.assertLessEqual(len(out["results"]), 100)
            self.assertLessEqual(len(json.dumps(out, ensure_ascii=False)), 10000)
            indexes.extend(r["index"] for r in hits)
            if not out["has_more"]:
                break
            self.assertGreater(out["next_offset"], offset)
            offset = out["next_offset"]
        self.assertEqual(indexes, list(range(5, 121, 5)))

    def test_one_oversized_hit_returns_a_preview_and_advances(self):
        self.write([{"index": 1, "post_src": "hit" * 20000}, {"index": 2, "post_src": "hit"}])
        out = self.search(query="hit", context=2)
        self.assertEqual(out["total"], 2)
        self.assertEqual(out["next_offset"], 1)
        self.assertTrue(out["results"][0]["truncated"])
        self.assertLessEqual(len(json.dumps(out, ensure_ascii=False)), 10000)
        self.assertEqual(self.search(query="hit", offset=1)["results"][0]["index"], 2)
