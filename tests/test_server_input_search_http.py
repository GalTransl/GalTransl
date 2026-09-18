"""POST /input/search 的 HTTP 层：路由位置、入参校验、走真输入文件的端到端结果。

这条路由**必须排在 /input/:filename 之前**（后者是按前缀匹配的），否则 POST 会被当成
"读一个名叫 search 的输入文件"。这里用真项目 + 真文件插件跑一遍，把这件事钉住。
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from GalTransl import INPUT_FOLDERNAME

try:
    from GalTransl.server import JobRegistry, build_handler
    from GalTransl.server_runtime import encode_project_dir
except ModuleNotFoundError:  # 精简环境（如系统 python）没有 yaml/orjson，跳过
    raise unittest.SkipTest("server 依赖不可用")


class InputSearchHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = tempfile.mkdtemp(prefix="galtransl-input-search-http-")
        cls.project = os.path.join(cls.root, "proj")
        os.makedirs(os.path.join(cls.project, INPUT_FOLDERNAME), exist_ok=True)
        with open(os.path.join(cls.project, "config.yaml"), "w", encoding="utf-8") as f:
            # plugin 段是读输入文件的前提（_open_project_file_plugin 要用它初始化插件）
            f.write("common:\n  language: ja\nplugin:\n  filePlugin: file_galtransl_json\n  textPlugins: []\n")
        entries = [
            {"name": "少女", "message": "おはよう"},
            {"name": "ドルード", "message": "ドルード、待って"},
            {"name": "少女", "message": "ドルードだ"},
        ]
        with open(os.path.join(cls.project, INPUT_FOLDERNAME, "a.json"), "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False)

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(JobRegistry()))
        cls.base = (
            f"http://127.0.0.1:{cls.httpd.server_address[1]}/api/projects/"
            f"{encode_project_dir(cls.project)}"
        )
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()

    def _post(self, payload, *, path="/input/search"):
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)

    def test_search_reads_the_input_file_through_the_file_plugin(self):
        out = self._post({"query": "ドルード", "field": "src", "config_file_name": "config.yaml"})

        self.assertEqual(out["total"], 2)
        self.assertEqual([r["index"] for r in out["results"]], [2, 3])
        self.assertEqual(out["results"][0]["src"], "ドルード、待って")
        self.assertEqual(out["results"][0]["speaker"], "ドルード")
        self.assertTrue(out["results"][0]["match_src"])

    def test_context_expands_around_the_hit(self):
        out = self._post({"query": "ドルード", "field": "src", "context": 1})

        self.assertEqual(out["context"], 1)
        self.assertEqual([r["index"] for r in out["results"]], [1, 2, 3])
        for row in out["results"]:
            self.assertNotIn("in_context", row)  # 上下文行不做任何标注

    def test_search_by_speaker(self):
        out = self._post({"query": "少女", "field": "name"})
        self.assertEqual([r["index"] for r in out["results"]], [1, 3])

    def test_empty_query_is_an_empty_result_not_an_error(self):
        out = self._post({"query": "   "})
        self.assertEqual(out, {"results": [], "total": 0})

    def test_bad_field_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post({"query": "x", "field": "dst"})  # 原文侧没有译文列
        self.assertEqual(ctx.exception.code, 400)

    def test_bad_context_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post({"query": "x", "context": "abc"})
        self.assertEqual(ctx.exception.code, 400)

    def test_invalid_regex_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post({"query": "(", "options": {"re": True}})
        self.assertEqual(ctx.exception.code, 400)

    def test_get_falls_through_to_the_file_route(self):
        """GET 走 /input/:filename（读同名文件）——搜索分支只认 POST，不会把它吞掉。"""
        try:
            with urllib.request.urlopen(f"{self.base}/input/search", timeout=30) as resp:
                self.fail(f"不该读到文件：{resp.status}")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 404)  # "input file not found: search"


if __name__ == "__main__":
    unittest.main()
