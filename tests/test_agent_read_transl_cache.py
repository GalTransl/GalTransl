"""read_transl_cache 的字段裁剪。

背景：缓存条目固定带一堆字段，但 post_dst_preview 基本等于 pre_dst、post_src 约等于
pre_src、proofread_* 常年为空——一次读几十条时一半以上是重复或空值，白占模型上下文。

这里锁住三件事：

1. 不传 fields = 默认精简集（原文/译文/说话人/问题），空值省略；
2. post_dst_preview 只在真的与 pre_dst 不同（存在译后字典替换）时才返回；
3. 传 fields 就只给这几列（index 永远在，定位要用），未知字段直接报错。
"""

import unittest
from types import SimpleNamespace

from GalTransl.Agent.runtime import (
    CACHE_ENTRY_FIELDS_DEFAULT,
    AgentToolError,
    _project_cache_entries,
    _tool_read_transl_cache,
)

ENTRY = {
    "index": 7,
    "name": "男主",
    "pre_src": "こんにちは",
    "post_src": "こんにちは",
    "pre_dst": "你好",
    "post_dst_preview": "你好",
    "proofread_dst": "",
    "proofread_by": "",
    "trans_by": "ForGal-json",
    "trans_conf": 90,
    "problem": "残留日文",
}


class _Runner:
    """最小 runner：缓存读取接口返回给定条目。"""

    def __init__(self, entries):
        self.state = SimpleNamespace(config_file_name="config.yaml")
        self._entries = entries

    def _project_id(self):
        return "proj"

    def _http_get(self, _path):
        return {"entries": self._entries}


class CacheEntryProjectionTests(unittest.TestCase):
    def test_default_drops_redundant_and_empty(self) -> None:
        item = _project_cache_entries([ENTRY], None)[0]
        self.assertEqual(
            sorted(item),
            sorted(["index", "name", "pre_src", "pre_dst", "problem", "trans_by"]),
        )
        # post_dst_preview 与 pre_dst 相同 → 不占位置
        self.assertNotIn("post_dst_preview", item)
        # 空的校对字段不返回；trans_by 有内容才带上
        self.assertNotIn("proofread_dst", item)
        self.assertNotIn("proofread_by", item)
        self.assertEqual(item["trans_by"], "ForGal-json")

    def test_post_dst_preview_only_when_it_differs(self) -> None:
        changed = {**ENTRY, "post_dst_preview": "您好（替换后）"}
        item = _project_cache_entries([changed], None)[0]
        self.assertEqual(item["post_dst_preview"], "您好（替换后）")

    def test_post_src_only_when_it_differs(self) -> None:
        """post_src 与原文不同 = 译前字典动过，这时才值得带上。"""
        self.assertNotIn("post_src", _project_cache_entries([ENTRY], None)[0])
        changed = {**ENTRY, "post_src": "こんにちは（替换后）"}
        self.assertEqual(
            _project_cache_entries([changed], None)[0]["post_src"], "こんにちは（替换后）"
        )

    def test_proofread_fields_returned_when_filled(self) -> None:
        proofread = {**ENTRY, "proofread_dst": "你好呀", "proofread_by": "proofreader"}
        item = _project_cache_entries([proofread], None)[0]
        self.assertEqual(item["proofread_dst"], "你好呀")
        self.assertEqual(item["proofread_by"], "proofreader")

    def test_empty_index_is_kept(self) -> None:
        item = _project_cache_entries([{**ENTRY, "index": 0, "problem": ""}], None)[0]
        self.assertEqual(item["index"], 0)  # index 不能因为"空值"被省掉

    def test_explicit_fields_keep_only_those_and_index(self) -> None:
        item = _project_cache_entries([ENTRY], ["pre_dst", "problem"])[0]
        self.assertEqual(sorted(item), ["index", "pre_dst", "problem"])

    def test_default_field_list_is_lean(self) -> None:
        item = _project_cache_entries([ENTRY], list(CACHE_ENTRY_FIELDS_DEFAULT))[0]
        self.assertEqual(sorted(item), sorted(CACHE_ENTRY_FIELDS_DEFAULT))


class ReadTranslCacheToolTests(unittest.TestCase):
    def test_default_call_reports_fields_and_note(self) -> None:
        out = _tool_read_transl_cache(_Runner([ENTRY]), {"filename": "01.json", "index": "7"})
        self.assertEqual(out["returned"], 1)
        self.assertEqual(out["fields"], list(CACHE_ENTRY_FIELDS_DEFAULT))
        self.assertIn("fields_note", out)
        entry = out["entries"][0]
        self.assertEqual(entry["pre_dst"], "你好")
        self.assertNotIn("post_src", entry)

    def test_fields_param_controls_the_columns(self) -> None:
        out = _tool_read_transl_cache(
            _Runner([ENTRY]), {"filename": "01.json", "index": "7", "fields": ["pre_dst", "problem"]}
        )
        self.assertEqual(out["fields"], ["pre_dst", "problem"])
        self.assertNotIn("fields_note", out)
        self.assertEqual(sorted(out["entries"][0]), ["index", "pre_dst", "problem"])

    def test_star_returns_every_field(self) -> None:
        out = _tool_read_transl_cache(
            _Runner([ENTRY]), {"filename": "01.json", "index": "7", "fields": ["*"]}
        )
        self.assertIn("post_src", out["entries"][0])
        self.assertIn("proofread_by", out["entries"][0])

    def test_pipeline_only_fields_are_not_exposed(self) -> None:
        """缓存里由管道写入、Agent 用不到的字段（trans_conf/doub_content/
        unknown_proper_noun）不该出现在返回里——连 fields=["*"] 也不给。"""
        entry = {**ENTRY, "doub_content": "存疑内容", "unknown_proper_noun": "某名词"}
        out = _tool_read_transl_cache(
            _Runner([entry]), {"filename": "01.json", "index": "7", "fields": ["*"]}
        )
        for name in ("trans_conf", "doub_content", "unknown_proper_noun"):
            self.assertNotIn(name, out["entries"][0])

    def test_context_entries_keep_in_context_flag(self) -> None:
        entries = [
            {**ENTRY, "index": i, "pre_dst": f"译{i}", "problem": ""} for i in range(1, 6)
        ]
        out = _tool_read_transl_cache(
            _Runner(entries), {"filename": "01.json", "index": "3", "context": 1}
        )
        self.assertEqual([e["index"] for e in out["entries"]], [2, 3, 4])
        self.assertEqual([e["in_context"] for e in out["entries"]], [True, False, True])

    def test_no_index_returns_first_30_projected(self) -> None:
        entries = [{**ENTRY, "index": i, "problem": ""} for i in range(1, 40)]
        out = _tool_read_transl_cache(_Runner(entries), {"filename": "01.json"})
        self.assertEqual(out["count"], 39)
        self.assertEqual(out["returned"], 30)
        self.assertNotIn("post_src", out["entries"][0])

    def test_unknown_field_is_rejected(self) -> None:
        for bad in ({"fields": ["nope"]}, {"fields": []}, {"fields": "pre_dst"}):
            with self.assertRaises(AgentToolError):
                _tool_read_transl_cache(_Runner([ENTRY]), {"filename": "01.json", **bad})

    def test_missing_indexes_still_reported(self) -> None:
        out = _tool_read_transl_cache(_Runner([ENTRY]), {"filename": "01.json", "index": "7,99"})
        self.assertEqual(out["missing_indexes"], [99])


if __name__ == "__main__":
    unittest.main()
