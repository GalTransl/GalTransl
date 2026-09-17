"""白名单在服务端三个出口的表现：/problems、/progress、缓存重建（skip_check 合流）。

/problems 与 /progress 是只读统计，必须即时排除白名单条目；缓存重建那支由
_cache_entries_to_trans_list 把白名单合流成 skip_check，这里直接测该函数
（避开起完整 find_problems 配置的成本）。
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

import orjson

try:
    from GalTransl import CACHE_FOLDERNAME
    from GalTransl.server import (
        JobRegistry,
        RuntimeProgressCache,
        _cache_entries_to_trans_list,
        build_handler,
    )
    from GalTransl.server_runtime import encode_project_dir
except ModuleNotFoundError:  # 精简环境（如系统 python）没有 yaml/orjson，跳过
    raise unittest.SkipTest("server 依赖不可用")

from GalTransl.ProblemWhiteList import build_problem_white_list_index


def _entry(index, problem):
    return {
        "index": index,
        "name": "少女",
        "pre_src": f"原文{index}",
        "post_src": f"原文{index}",
        "pre_dst": f"译文{index}",
        "proofread_dst": "",
        "problem": problem,
    }


class CacheEntriesToTransListTests(unittest.TestCase):
    """缓存重建时白名单与条目自身的 skip_check 在同一个入口合流。"""

    def _entries(self):
        return [
            {"index": 1, "name": "少女", "post_src": "a", "pre_dst": "A"},
            {"index": 2, "name": "", "post_src": "b", "pre_dst": "B", "skip_check": True},
            {"index": 3, "name": "", "post_src": "c", "pre_dst": "C"},
        ]

    def test_white_list_marks_skip_check(self):
        white = build_problem_white_list_index(["a.json:3"])
        trans = _cache_entries_to_trans_list(self._entries(), "a.json", white)
        self.assertEqual([t.skip_check for t in trans], [False, True, True])

    def test_append_file_name_matches_snapshot_spec(self):
        white = build_problem_white_list_index(["a.json:1"])
        trans = _cache_entries_to_trans_list(self._entries(), "a.json.append.jsonl", white)
        self.assertTrue(trans[0].skip_check)


class ProgressWhiteListTests(unittest.TestCase):
    def test_whitelisted_entry_is_not_counted_as_problem(self):
        entries = [_entry(1, "残留日文"), _entry(2, "残留日文")]
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = os.path.join(tmp, CACHE_FOLDERNAME)
            os.makedirs(cache_dir)
            with open(os.path.join(cache_dir, "a.json"), "wb") as f:
                f.write(orjson.dumps(entries))

            result = RuntimeProgressCache().get_progress(
                tmp,
                file_totals={"a.json": 2},
                cache_file_display_map={"a.json": "a.json"},
                problem_white_list=["a.json:1"],
            )

        files = {f["filename"]: f for f in result["files"]}
        self.assertEqual(files["a.json"]["problems"], 1)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="galtransl-white-list-http-")
        cls.project = os.path.join(cls.root, "proj")
        os.makedirs(os.path.join(cls.project, CACHE_FOLDERNAME), exist_ok=True)
        with open(os.path.join(cls.project, "config.yaml"), "w", encoding="utf-8") as f:
            f.write('common:\n  language: ja\n  problemWhiteList:\n    - "a.json:1"\n')
        with open(os.path.join(cls.project, CACHE_FOLDERNAME, "a.json"), "wb") as f:
            f.write(orjson.dumps([_entry(1, "残留日文"), _entry(2, "残留日文")]))

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(JobRegistry()))
        cls.base = (
            f"http://127.0.0.1:{cls.httpd.server_address[1]}/api/projects/"
            f"{encode_project_dir(cls.project)}"
        )
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def _get(self, path):
        with urllib.request.urlopen(f"{self.base}{path}", timeout=30) as resp:
            return json.load(resp)

    def test_problems_endpoint_hides_whitelisted_entry(self):
        out = self._get("/problems?config=config.yaml")
        self.assertEqual([p["index"] for p in out["problems"]], [2])

    def test_progress_endpoint_excludes_whitelisted_problem(self):
        out = self._get("/progress?config=config.yaml")
        self.assertEqual(out["problems"], 1)


if __name__ == "__main__":
    unittest.main()
