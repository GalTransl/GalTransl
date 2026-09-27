"""缓存读取与搜索的回落：没有缓存文件时读原文。

「缓存与问题」页要看还没翻译的文件，而这类文件在 Cache/ 里根本没有：
① 列表接口把它们一并给出来（uncached_files，带 has_cache=false）；
② 读缓存接口在缓存缺失时回落到读 gt_input 的原文，条目只有 pre_src/post_src、译文为空；
③ 搜索接口传 include_uncached 时把这些文件的原文也搜进来（界面默认开，Agent 默认关：
   它另有 search_input 专门搜原文）。
这里用真项目 + 真文件插件把这条链路钉住（命名规则要和运行时一致，见
Frontend/LLMTranslate._build_runtime_file_maps 与 Cache.save_transCache_to_json）。
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from urllib.parse import quote

from GalTransl import CACHE_FOLDERNAME, INPUT_FOLDERNAME

try:
    from GalTransl.server import (
        JobRegistry,
        _cache_key_for_input_name,
        _cache_name_bases,
        _input_name_candidates,
        build_handler,
    )
    from GalTransl.server_runtime import encode_project_dir
except ModuleNotFoundError:  # 精简环境（如系统 python）没有 yaml/orjson，跳过
    raise unittest.SkipTest("server 依赖不可用")


class CacheNameMappingTests(unittest.TestCase):
    """缓存名 ↔ 输入文件名的正反换算（回落全靠它找对文件）。"""

    def test_input_name_to_cache_key(self):
        self.assertEqual(_cache_key_for_input_name("a.json"), "a.json")
        self.assertEqual(_cache_key_for_input_name("chapter/scene.json"), "chapter-}scene.json")
        # 非 json 输入：缓存名补 .json 后缀
        self.assertEqual(_cache_key_for_input_name("script.ks"), "script.ks.json")

    def test_cache_key_to_input_name_candidates(self):
        self.assertEqual(
            _input_name_candidates("chapter-}scene.json"), ["chapter/scene", "chapter/scene.json"]
        )
        self.assertEqual(_input_name_candidates("script.ks.json"), ["script.ks", "script.ks.json"])
        # 增量缓存（.append.jsonl）
        self.assertEqual(_input_name_candidates("a.json.append.jsonl")[:2], ["a", "a.json"])
        # 切块缓存是 foo.json_1.json（_<n> 在 .json 之后再来一个 .json）
        self.assertIn("long.json", _input_name_candidates("long.json_2.json"))

    def test_cache_bases_fold_append_and_chunks(self):
        bases = _cache_name_bases({"a.json", "a.json.append.jsonl", "long.json_1.json", "long.json_2.json"})
        self.assertEqual(bases, {"a.json", "long.json"})


class CacheSourceFallbackHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = tempfile.mkdtemp(prefix="galtransl-cache-source-")
        cls.project = os.path.join(cls.root, "proj")
        os.makedirs(os.path.join(cls.project, INPUT_FOLDERNAME, "chapter"), exist_ok=True)
        os.makedirs(os.path.join(cls.project, CACHE_FOLDERNAME), exist_ok=True)
        with open(os.path.join(cls.project, "config.yaml"), "w", encoding="utf-8") as f:
            f.write("common:\n  language: ja\nplugin:\n  filePlugin: file_galtransl_json\n  textPlugins: []\n")
        cls._write_input(
            "a.json",
            [{"name": "少女", "message": "おはよう"}, {"name": "", "message": "ドルード、待って"}],
        )
        cls._write_input("chapter/b.json", [{"message": "やあ"}])

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(JobRegistry()))
        cls.base = (
            f"http://127.0.0.1:{cls.httpd.server_address[1]}/api/projects/"
            f"{encode_project_dir(cls.project)}"
        )
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()

    @classmethod
    def _write_input(cls, name: str, entries: list[dict]) -> None:
        path = os.path.join(cls.project, INPUT_FOLDERNAME, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False)

    @classmethod
    def _write_cache(cls, name: str, entries: list[dict]) -> None:
        with open(os.path.join(cls.project, CACHE_FOLDERNAME, name), "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False)

    def _get(self, path: str):
        with urllib.request.urlopen(f"{self.base}{path}", timeout=30) as resp:
            return json.load(resp)

    def _post(self, path: str, payload: dict):
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as resp:
            return json.load(resp)

    def _search(self, payload: dict):
        return self._post("/cache/search", {"config_file_name": "config.yaml", **payload})

    def _read_cache_file(self, filename: str):
        return self._get(f"/cache/{quote(filename)}")

    @staticmethod
    def _uncached_by_name(payload) -> dict:
        return {str(item.get("name") or ""): item for item in payload.get("uncached_files", [])}

    def test_uncached_input_files_are_listed(self):
        payload = self._get("/cache")

        cache_names = {str(f.get("name") or "") for f in payload["files"]}
        self.assertNotIn("a.json", cache_names)  # 还没翻译：Cache/ 里当然没有它
        uncached = self._uncached_by_name(payload)
        self.assertEqual(sorted(uncached), ["a.json", "chapter-}b.json"])
        self.assertFalse(uncached["a.json"]["has_cache"])
        # 界面点开的是缓存键，tooltip 里显示原文路径
        self.assertEqual(uncached["chapter-}b.json"]["input_name"], "chapter/b.json")

    def test_reading_a_missing_cache_returns_the_source(self):
        payload = self._read_cache_file("a.json")

        self.assertFalse(payload["has_cache"])
        self.assertEqual(payload["input_name"], "a.json")
        self.assertEqual(
            payload["entries"],
            [
                {"index": 1, "name": "少女", "pre_src": "おはよう", "post_src": "おはよう", "pre_dst": ""},
                {"index": 2, "name": "", "pre_src": "ドルード、待って", "post_src": "ドルード、待って", "pre_dst": ""},
            ],
        )

    def test_nested_input_file_is_found_through_the_escaped_cache_key(self):
        payload = self._read_cache_file("chapter-}b.json")

        self.assertFalse(payload["has_cache"])
        self.assertEqual(payload["input_name"], "chapter/b.json")
        self.assertEqual([e["post_src"] for e in payload["entries"]], ["やあ"])

    def test_existing_cache_wins_over_the_source(self):
        self._write_cache("a.json", [{"index": 1, "name": "少女", "pre_src": "おはよう", "post_src": "おはよう", "pre_dst": "早上好"}])
        try:
            payload = self._read_cache_file("a.json")
            self.assertTrue(payload["has_cache"])
            self.assertEqual([e["pre_dst"] for e in payload["entries"]], ["早上好"])
            self.assertNotIn("a.json", self._uncached_by_name(self._get("/cache")))
        finally:
            os.remove(os.path.join(self.project, CACHE_FOLDERNAME, "a.json"))

    def test_chunked_or_incremental_cache_counts_as_translated(self):
        # 切块缓存长这样：foo.json → foo.json_1.json；有它就不该再列成「未翻译」
        self._write_cache("chapter-}b.json_1.json", [{"index": 1, "post_src": "やあ", "pre_dst": "呀"}])
        try:
            self.assertNotIn("chapter-}b.json", self._uncached_by_name(self._get("/cache")))
        finally:
            os.remove(os.path.join(self.project, CACHE_FOLDERNAME, "chapter-}b.json_1.json"))

    def test_unknown_file_without_cache_is_still_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._read_cache_file("nope.json")
        self.assertEqual(ctx.exception.code, 404)

    # ── 搜索：默认只搜缓存；界面传 include_uncached 才连原文一起搜 ──

    def test_search_ignores_uncached_source_by_default(self):
        # Agent 的 search_transl_cache 不带这个参数（它有 search_input 专门搜原文）
        payload = self._search({"query": "ドルード"})

        self.assertEqual(payload["total"], 0)
        self.assertEqual(payload["results"], [])

    def test_search_matches_uncached_source_when_asked(self):
        payload = self._search({"query": "ドルード", "include_uncached": True})

        self.assertEqual(payload["total"], 1)
        hit = payload["results"][0]
        self.assertEqual(hit["filename"], "a.json")  # 用缓存键命名：点开结果时前端按它读
        self.assertEqual(hit["index"], 2)
        self.assertEqual(hit["post_src"], "ドルード、待って")
        self.assertEqual(hit["pre_dst"], "")
        self.assertTrue(hit["match_src"])
        self.assertFalse(hit["match_dst"])
        self.assertFalse(hit["has_cache"])

    def test_search_dst_field_never_matches_uncached_source(self):
        # 还没翻译就没有译文可搜：仅译文的搜索不该把原文捞出来
        payload = self._search({"query": "ドルード", "field": "dst", "include_uncached": True})

        self.assertEqual(payload["total"], 0)

    def test_search_merges_cache_hits_and_source_hits(self):
        self._write_cache(
            "chapter-}b.json",
            [{"index": 1, "name": "", "post_src": "やあ", "pre_dst": "呀，你好"}],
        )
        try:
            # 有缓存的文件只按缓存搜（不会既出缓存命中又出原文命中）
            payload = self._search({"query": "やあ", "include_uncached": True})
            self.assertEqual(payload["total"], 1)
            self.assertTrue(payload["results"][0]["has_cache"])
            self.assertEqual(payload["results"][0]["filename"], "chapter-}b.json")

            # 译文里的关键词命中缓存条目
            payload = self._search({"query": "你好", "include_uncached": True})
            self.assertEqual([(r["filename"], r["match_dst"]) for r in payload["results"]], [("chapter-}b.json", True)])

            # 同一张表里，未翻译文件的原文命中也在
            payload = self._search({"query": "おはよう", "include_uncached": True})
            self.assertEqual([(r["filename"], r["has_cache"]) for r in payload["results"]], [("a.json", False)])
        finally:
            os.remove(os.path.join(self.project, CACHE_FOLDERNAME, "chapter-}b.json"))

    def test_search_can_target_a_single_uncached_file(self):
        payload = self._search({"query": "おはよう", "filename": "a.json", "include_uncached": True})
        self.assertEqual([r["filename"] for r in payload["results"]], ["a.json"])

        # 指名一个没有缓存、也没有同名输入文件的文件：什么也搜不到
        payload = self._search({"query": "おはよう", "filename": "nope.json", "include_uncached": True})
        self.assertEqual(payload["total"], 0)


if __name__ == "__main__":
    unittest.main()
