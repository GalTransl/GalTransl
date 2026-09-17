"""POST /input/search 的扫描逻辑（_search_input_dir）。

原文要过文件插件才能解析，所以这里把 _load_input_file_entries 换成假的—被测的是**搜索
本身的规则**，与读文件那套解耦：与 /cache/search 同语义（命中上限只算命中本身、context
是"顺带带出来的前后文"、total 照实报、单文件解析失败不影响其它文件）。
"""

import os
import re
import tempfile
import unittest
from unittest.mock import patch

try:
    from GalTransl import INPUT_FOLDERNAME
    from GalTransl import server
except ModuleNotFoundError:  # 精简环境（如系统 python）没有 yaml，跳过
    raise unittest.SkipTest("server 依赖不可用")


def _entries(pairs: list[tuple[str, str]]) -> list[dict]:
    """[(说话人, 原文)] → 规整过的输入条目（与 _normalize_input_entries 同形状）。"""
    return [
        {"index": i, "name": name, "pre_src": src, "speaker": name}
        for i, (name, src) in enumerate(pairs, start=1)
    ]


class InputSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="galtransl-input-search-")
        self.input_dir = os.path.join(self.root, INPUT_FOLDERNAME)
        os.makedirs(self.input_dir, exist_ok=True)
        with open(os.path.join(self.input_dir, "a.json"), "w", encoding="utf-8") as f:
            f.write("[]")
        with open(os.path.join(self.input_dir, "b.json"), "w", encoding="utf-8") as f:
            f.write("[]")
        self.files = {
            "a.json": _entries(
                [
                    ("少女", "おはよう"),
                    ("ドルード", "ドルード、待って"),  # 命中（原文）
                    ("少女", "ドルードだ"),
                    ("アリス", "アリスと呼んで"),
                ]
            ),
            "b.json": _entries([("ボブ", "ドルード、またね")]),
        }

    def _search(self, **kwargs):
        def fake_loader(_project_dir, _config_name, name):
            if name == "boom.json":
                raise RuntimeError("解析失败")
            return list(self.files[name])

        kwargs.setdefault("query", "ドルード")
        with patch.object(server, "_load_input_file_entries", fake_loader):
            return server._search_input_dir(self.root, "config.yaml", **kwargs)

    # ---- 匹配 ----

    def test_substring_match_is_case_insensitive_across_files(self):
        out = self._search(field="src")
        self.assertEqual(out["total"], 3)
        self.assertEqual(
            [(r["filename"], r["index"]) for r in out["results"]], [("a.json", 2), ("a.json", 3), ("b.json", 1)]
        )

    def test_name_field_matches_the_speaker_only(self):
        out = self._search(query="少女", field="name")
        self.assertEqual([(r["filename"], r["index"]) for r in out["results"]], [("a.json", 1), ("a.json", 3)])
        self.assertTrue(all(r["match_name"] and not r["match_src"] for r in out["results"]))

    def test_all_field_matches_either_side(self):
        """field=all 同时搜原文与说话人：说话人命中、原文不含关键词的那条也要出来。"""
        out = self._search(query="アリス", field="all")
        rows = {r["index"]: r for r in out["results"] if r["filename"] == "a.json"}
        self.assertEqual(sorted(rows), [4])
        self.assertTrue(rows[4]["match_name"])
        self.assertTrue(rows[4]["match_src"])

    def test_filename_narrows_the_scan(self):
        out = self._search(field="src", filename="b.json")
        self.assertEqual([(r["filename"], r["index"]) for r in out["results"]], [("b.json", 1)])

    def test_regular_expression_mode(self):
        """pattern 非空时按正则匹配：用 ^ 锚定——当普通子串搜（关键词里带个字面量 ^）一条都不命中。"""
        out = self._search(query=r"^ドルード、", field="src", pattern=re.compile(r"^ドルード、"))
        self.assertEqual(
            [(r["filename"], r["index"]) for r in out["results"]], [("a.json", 2), ("b.json", 1)]
        )

    def test_missing_query_target_is_not_an_error(self):
        out = self._search(query="谁都不认识", field="src")
        self.assertEqual(out["results"], [])
        self.assertEqual(out["total"], 0)

    def test_unparsable_file_is_skipped_but_reported(self):
        """一个文件解析不了（插件/格式问题）不该让整次搜索失败，但必须在返回里说明。"""
        out = self._search(query="x", field="src")
        self.assertEqual(out["total"], 0)  # 两个正常文件里没有 x
        self.assertNotIn("files_failed", out)  # 没人失败就别加这个键

        # 让 a.json 变成解析失败的文件，b.json 的命中仍要出来
        def fake_loader(_project_dir, _config_name, name):
            if name == "a.json":
                raise RuntimeError("解析失败")
            return list(self.files[name])

        with patch.object(server, "_load_input_file_entries", fake_loader):
            out2 = server._search_input_dir(self.root, "config.yaml", query="ドルード", field="src")
        self.assertEqual([(r["filename"], r["index"]) for r in out2["results"]], [("b.json", 1)])
        self.assertEqual(out2["files_failed"], ["a.json"])  # "没读"与"没命中"必须分得开

    # ---- 上下文 ----

    def test_context_expands_around_hits_without_marking(self):
        out = self._search(field="src", context=1)
        self.assertEqual(out["context"], 1)
        # a.json 命中 2、3 → 扩成 1~4；b.json 命中 1 → 只有 1
        self.assertEqual(
            [(r["filename"], r["index"]) for r in out["results"]],
            [("a.json", 1), ("a.json", 2), ("a.json", 3), ("a.json", 4), ("b.json", 1)],
        )
        by_key = {(r["filename"], r["index"]): r for r in out["results"]}
        for row in out["results"]:
            self.assertNotIn("in_context", row)  # 上下文行不做任何标注
        self.assertFalse(by_key[("a.json", 1)]["match_src"])  # 上下文行不是命中
        self.assertTrue(by_key[("a.json", 2)]["match_src"])
        self.assertEqual(out["returned_hits"], 3)
        self.assertEqual(out["returned"], 5)

    def test_context_is_clipped_at_file_edges(self):
        out = self._search(field="src", filename="b.json", context=3)
        self.assertEqual([r["index"] for r in out["results"]], [1])  # b.json 只有一条

    def test_context_rows_render_the_source_text_and_speaker(self):
        out = self._search(field="src", context=1)
        row = next(r for r in out["results"] if (r["filename"], r["index"]) == ("a.json", 1))
        self.assertEqual(row["src"], "おはよう")
        self.assertEqual(row["speaker"], "少女")

    def test_no_context_means_no_context_fields(self):
        out = self._search(field="src")
        self.assertNotIn("context", out)
        self.assertNotIn("returned", out)
        for row in out["results"]:
            self.assertNotIn("in_context", row)

    # ---- 上限 ----

    def test_hit_cap_counts_hits_only_and_total_stays_honest(self):
        out = self._search(field="src", context=1, max_results=1)
        self.assertEqual(out["returned_hits"], 1)
        self.assertEqual(out["total"], 3)  # 全部命中数照实报
        # 只给第 1 条命中（a.json#2）＋它的上下文
        self.assertEqual(
            [(r["filename"], r["index"]) for r in out["results"]], [("a.json", 1), ("a.json", 2), ("a.json", 3)]
        )


if __name__ == "__main__":
    unittest.main()
