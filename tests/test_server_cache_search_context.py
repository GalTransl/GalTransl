"""POST /cache/search 的 context：命中条目前后各带 N 句（服务端实现）。

Agent 侧的 search_transl_cache 带 context 后，真正拼上下文的是服务端——这里起一个真的
ThreadingHTTPServer，用真缓存文件走 HTTP，把这几条锁住：

- context=0（默认）返回体与老版本一致（不带 in_context/context/returned 字段）；
- context=N 时命中 in_context=false、扩展出来的前后文 true，且重叠区间去重；
- 命中上限只算命中本身（前后文不占配额），total 始终是全部命中数；
- context 非法 → 400。
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from GalTransl import CACHE_FOLDERNAME


def _entries(count: int, query_indexes: dict[int, str]) -> list[dict]:
    """count 条缓存条目；query_indexes 里的 index 会把关键词写进原文。"""
    return [
        {
            "index": i,
            "name": "少女",
            "pre_src": query_indexes.get(i, f"JP{i}"),
            "pre_dst": f"ZH{i}",
        }
        for i in range(1, count + 1)
    ]


class CacheSearchContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            from GalTransl.server import JobRegistry, build_handler
            from GalTransl.server_runtime import encode_project_dir
        except ModuleNotFoundError:  # 精简环境（如系统 python）没有 yaml，跳过
            raise unittest.SkipTest("server 依赖不可用")

        cls.root = tempfile.mkdtemp(prefix="galtransl-search-ctx-")
        cls.project = os.path.join(cls.root, "proj")
        os.makedirs(os.path.join(cls.project, CACHE_FOLDERNAME), exist_ok=True)
        with open(os.path.join(cls.project, "config.yaml"), "w", encoding="utf-8") as f:
            f.write("common:\n  language: zh-cn\n")
        # a.json: 第 4、5 条含 ドルード（相邻，用于验证重叠去重）
        with open(
            os.path.join(cls.project, CACHE_FOLDERNAME, "a.json"), "w", encoding="utf-8"
        ) as f:
            json.dump(_entries(9, {4: "ドルードだ", 5: "ドルード、待って"}), f, ensure_ascii=False)

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(JobRegistry()))
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}/api/projects/{encode_project_dir(cls.project)}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()

    def _search(self, **body):
        payload = {"query": "ドルード", "field": "src", **body}
        req = urllib.request.Request(
            f"{self.base}/cache/search",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)

    def test_default_response_is_unchanged(self) -> None:
        out = self._search()
        self.assertEqual(out["total"], 2)
        self.assertEqual([r["index"] for r in out["results"]], [4, 5])
        for item in out["results"]:
            self.assertNotIn("in_context", item)
        self.assertNotIn("context", out)
        self.assertNotIn("returned", out)

    def test_context_expands_around_hits_and_dedupes(self) -> None:
        out = self._search(context=2)
        self.assertEqual(out["context"], 2)
        self.assertEqual(out["returned_hits"], 2)
        # 命中 4、5，前后各 2 句 → 2~7 去重后 6 条
        self.assertEqual([r["index"] for r in out["results"]], [2, 3, 4, 5, 6, 7])
        self.assertEqual(out["returned"], 6)
        by_index = {r["index"]: r for r in out["results"]}
        for item in out["results"]:
            self.assertNotIn("in_context", item)  # 上下文行不做任何标注
        # 上下文行不是命中：match_src 只在命中行上
        self.assertFalse(by_index[2]["match_src"])
        self.assertTrue(by_index[4]["match_src"])
        self.assertEqual(out["total"], 2)  # total 仍是命中数

    def test_context_clipped_at_file_edges(self) -> None:
        out = self._search(query="JP1", context=3)
        self.assertEqual([r["index"] for r in out["results"]], [1, 2, 3, 4])

    def test_hit_cap_counts_hits_only(self) -> None:
        """前后文不占命中配额：max_results=1 时给第 1 条命中 + 它的上下文。"""
        out = self._search(context=1, max_results=1)
        self.assertEqual(out["returned_hits"], 1)
        self.assertEqual(out["total"], 2)  # 总命中数照实报
        self.assertEqual([r["index"] for r in out["results"]], [3, 4, 5])

    def test_invalid_context_is_rejected(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._search(context="abc")
        self.assertEqual(ctx.exception.code, 400)

    def test_context_is_clamped_to_20(self) -> None:
        out = self._search(context=999)
        self.assertEqual(out["context"], 20)  # 服务端同样有上限


if __name__ == "__main__":
    unittest.main()
