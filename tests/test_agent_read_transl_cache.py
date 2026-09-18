"""read_transl_cache 的字段裁剪。

背景：缓存条目固定带一堆字段，但 post_dst_preview 基本等于 pre_dst、post_src 约等于
pre_src、proofread_* 常年为空——一次读几十条时一半以上是重复或空值，白占模型上下文。

这里锁住三件事：

1. 不传 fields = 默认精简集（原文/译文/说话人/问题/批注内容），空值省略；
2. post_dst_preview 只在译后处理真的改了内容时才返回——只差补回来的首尾「」不算，
   而那是"对话条目几乎必然不同"的原因，不排掉这个字段就等于默认都带上；
3. 传 fields 就只给这几列（index 永远在，定位要用），未知字段直接报错。
"""

import unittest
from types import SimpleNamespace

from GalTransl.Agent.runtime import (
    CACHE_ENTRY_FIELDS_DEFAULT,
    AgentToolError,
    _parse_index_spec,
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
    def test_default_is_lean(self) -> None:
        """默认一条只回：谁说的 + 原文一列 + 译文一列 + 问题。"""
        item = _project_cache_entries([ENTRY], None)[0]
        self.assertEqual(
            sorted(item),
            sorted(["index", "name", "post_src", "pre_dst", "problem"]),
        )
        # 原文不再发两遍（pre_src 默认不给）、译者是噪音（trans_by 默认不给）
        self.assertNotIn("pre_src", item)
        self.assertNotIn("trans_by", item)
        # post_dst_preview 与 pre_dst 相同 → 不占位置
        self.assertNotIn("post_dst_preview", item)
        # 空的校对字段不返回
        self.assertNotIn("proofread_dst", item)
        self.assertNotIn("proofread_by", item)

    def test_post_dst_preview_only_when_it_differs(self) -> None:
        changed = {**ENTRY, "post_dst_preview": "您好（替换后）"}
        item = _project_cache_entries([changed], None)[0]
        self.assertEqual(item["post_dst_preview"], "您好（替换后）")

    def test_post_dst_preview_ignored_when_only_brackets_differ(self) -> None:
        """译后处理会把首尾「」补回来：只差这一对括号不算改过内容，别占上下文。"""
        bracketed = {**ENTRY, "post_dst_preview": "「你好」"}
        item = _project_cache_entries([bracketed], None)[0]
        self.assertNotIn("post_dst_preview", item)

    def test_post_dst_preview_kept_when_brackets_hide_a_real_change(self) -> None:
        """括号之外还有改动（如译后字典替换）→ 照旧带上。"""
        bracketed = {**ENTRY, "post_dst_preview": "「您好」"}
        item = _project_cache_entries([bracketed], None)[0]
        self.assertEqual(item["post_dst_preview"], "「您好」")

    def test_post_dst_preview_compares_against_proofread_when_present(self) -> None:
        """有校对稿时最终译文是校对稿（与"proofread_dst ＞ pre_dst"同口径）：
        预览与校对稿只差括号，同样不返回。"""
        entry = {**ENTRY, "proofread_dst": "你好呀", "post_dst_preview": "「你好呀」"}
        item = _project_cache_entries([entry], None)[0]
        self.assertNotIn("post_dst_preview", item)

    def test_default_keeps_post_src_even_when_it_equals_pre_src(self) -> None:
        """post_src 是默认的原文列：与 pre_src 相同也要给（否则这条就没有原文了）。"""
        item = _project_cache_entries([ENTRY], None)[0]
        self.assertEqual(ENTRY["pre_src"], ENTRY["post_src"])
        self.assertEqual(item["post_src"], "こんにちは")

    def test_fields_can_ask_for_pre_src_and_trans_by(self) -> None:
        """想对照原始原文/看译者，显式传 fields 还是拿得到。"""
        item = _project_cache_entries([ENTRY], ["pre_src", "trans_by", "pre_dst"])[0]
        self.assertEqual(item["pre_src"], "こんにちは")
        self.assertEqual(item["trans_by"], "ForGal-json")
        self.assertEqual(item["pre_dst"], "你好")

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
        entry = {**ENTRY, "proofread_comment": "批注：这句像漏译"}  # 默认列里也有批注内容
        item = _project_cache_entries([entry], list(CACHE_ENTRY_FIELDS_DEFAULT))[0]
        self.assertEqual(sorted(item), sorted(CACHE_ENTRY_FIELDS_DEFAULT))


class ReadTranslCacheToolTests(unittest.TestCase):
    def test_default_call_reports_fields_and_note(self) -> None:
        out = _tool_read_transl_cache(_Runner([ENTRY]), {"filename": "01.json", "index": "7"})
        self.assertEqual(out["returned"], 1)
        self.assertEqual(out["fields"], list(CACHE_ENTRY_FIELDS_DEFAULT))
        self.assertIn("fields_note", out)
        entry = out["entries"][0]
        self.assertEqual(entry["pre_dst"], "你好")
        # 默认只给一列原文（post_src），不再同时带 pre_src
        self.assertEqual(entry["post_src"], "こんにちは")
        self.assertNotIn("pre_src", entry)

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
        # trans_by 是个例外：逐条只给少数派，多数派挪到顶层报一次（见 test_agent_trans_by_filter）
        self.assertNotIn("trans_by", out["entries"][0])
        self.assertEqual(out["majority_trans_by"], "ForGal-json")

    def test_pipeline_only_fields_are_not_exposed(self) -> None:
        """缓存里由管道写入、Agent 用不到的字段（trans_conf/unknown_proper_noun）
        不该出现在返回里——连 fields=["*"] 也不给。"""
        entry = {**ENTRY, "proofread_comment": "批注内容", "unknown_proper_noun": "某名词"}
        out = _tool_read_transl_cache(
            _Runner([entry]), {"filename": "01.json", "index": "7", "fields": ["*"]}
        )
        for name in ("trans_conf", "unknown_proper_noun"):
            self.assertNotIn(name, out["entries"][0])
        # proofread_comment 例外：它是校对子代理的产物，主 Agent 要读它才知道改哪儿
        self.assertEqual(out["entries"][0]["proofread_comment"], "批注内容")

    def test_context_rows_come_from_above_and_carry_a_star(self) -> None:
        """默认只给上文（only_preceding）：上下文行的 index 带 *，点名的条目原样。"""
        entries = [
            {**ENTRY, "index": i, "pre_dst": f"译{i}", "problem": ""} for i in range(1, 6)
        ]
        out = _tool_read_transl_cache(
            _Runner(entries), {"filename": "01.json", "index": "3", "context": 1}
        )
        self.assertEqual([e["index"] for e in out["entries"]], ["2*", 3])
        self.assertTrue(out["only_preceding"])
        for e in out["entries"]:
            self.assertNotIn("in_context", e)

    def test_only_preceding_false_gives_both_sides(self) -> None:
        entries = [
            {**ENTRY, "index": i, "pre_dst": f"译{i}", "problem": ""} for i in range(1, 6)
        ]
        out = _tool_read_transl_cache(
            _Runner(entries),
            {"filename": "01.json", "index": "3", "context": 1, "only_preceding": False},
        )
        self.assertEqual([e["index"] for e in out["entries"]], ["2*", 3, "4*"])
        self.assertFalse(out["only_preceding"])

    def test_index_spec_tolerates_the_context_star(self) -> None:
        """上下文行的 index 带 *（如 12*）：模型照抄过来按 12 处理，不该为这个报错。"""
        self.assertEqual(_parse_index_spec("12*,34"), {12, 34})
        self.assertEqual(_parse_index_spec("3*-5*"), {3, 4, 5})

    def test_no_index_returns_first_30_projected(self) -> None:
        entries = [{**ENTRY, "index": i, "problem": ""} for i in range(1, 40)]
        out = _tool_read_transl_cache(_Runner(entries), {"filename": "01.json"})
        self.assertEqual(out["count"], 39)
        self.assertEqual(out["returned"], 30)
        self.assertNotIn("trans_by", out["entries"][0])

    def test_old_cache_keys_are_read(self) -> None:
        """老项目缓存里是 pre_jp/post_jp/pre_zh 那套旧键名，读的时候要认。"""
        old = {"index": 1, "name": "少女", "pre_jp": "こんにちは", "post_jp": "こんにちは", "pre_zh": "你好"}
        entry = _project_cache_entries([old], None)[0]
        self.assertEqual(entry["post_src"], "こんにちは")
        self.assertEqual(entry["pre_dst"], "你好")
        # 显式指定字段时同样认旧键名
        picked = _project_cache_entries([old], ["pre_src", "pre_dst"])[0]
        self.assertEqual(picked["pre_src"], "こんにちは")

    def test_unknown_field_is_rejected(self) -> None:
        for bad in ({"fields": ["nope"]}, {"fields": []}, {"fields": "pre_dst"}):
            with self.assertRaises(AgentToolError):
                _tool_read_transl_cache(_Runner([ENTRY]), {"filename": "01.json", **bad})

    def test_missing_indexes_still_reported(self) -> None:
        out = _tool_read_transl_cache(_Runner([ENTRY]), {"filename": "01.json", "index": "7,99"})
        self.assertEqual(out["missing_indexes"], [99])


class ReadTranslCacheGrepTests(unittest.TestCase):
    """grep：字符串按字段内容搜索；数组按字段非空过滤。"""

    @staticmethod
    def _entries():
        return [
            {**ENTRY, "index": 1, "problem": "残留日文", "proofread_comment": "批注A"},
            {**ENTRY, "index": 2, "problem": "残留日文"},
            {**ENTRY, "index": 3, "problem": "", "proofread_comment": "批注B"},
            {**ENTRY, "index": 4, "problem": ""},
        ]

    def test_string_grep_searches_default_fields(self) -> None:
        out = _tool_read_transl_cache(_Runner(self._entries()), {"filename": "01.json", "grep": "残留日文"})
        self.assertEqual([e["index"] for e in out["entries"]], [1, 2])
        self.assertEqual(out["count"], 2)  # count 是命中数
        self.assertIn("文件共 4 条", out["grep_note"])

    def test_string_grep_is_case_insensitive(self) -> None:
        entries = [
            {**ENTRY, "index": 1, "pre_dst": "Hello world", "problem": ""},
            {**ENTRY, "index": 2, "pre_dst": "こんにちは", "problem": ""},
        ]
        out = _tool_read_transl_cache(_Runner(entries), {"filename": "01.json", "grep": "HELLO"})
        self.assertEqual([e["index"] for e in out["entries"]], [1])

    def test_string_grep_only_searches_the_selected_fields(self) -> None:
        # pre_src 与 post_src 不同：默认精简集不含 pre_src，搜不到；显式传 fields 才搜得到
        entries = [{**ENTRY, "index": 1, "pre_src": "特別な言葉", "post_src": "普通の言葉", "problem": ""}]
        default_out = _tool_read_transl_cache(_Runner(entries), {"filename": "01.json", "grep": "特別"})
        self.assertEqual(default_out["returned"], 0)
        fields_out = _tool_read_transl_cache(
            _Runner(entries), {"filename": "01.json", "grep": "特別", "fields": ["pre_src"]}
        )
        self.assertEqual([e["index"] for e in fields_out["entries"]], [1])

    def test_array_grep_keeps_entries_where_all_named_fields_are_non_empty(self) -> None:
        out = _tool_read_transl_cache(
            _Runner(self._entries()), {"filename": "01.json", "grep": ["problem", "proofread_comment"]}
        )
        self.assertEqual([e["index"] for e in out["entries"]], [1])
        self.assertIn("grep 非空：problem、proofread_comment", out["grep_note"])

    def test_array_grep_checks_raw_fields_even_if_not_selected(self) -> None:
        # problem 没被 fields 选中，但数组模式仍按原始条目判定非空
        out = _tool_read_transl_cache(
            _Runner(self._entries()),
            {"filename": "01.json", "grep": ["problem"], "fields": ["pre_dst"]},
        )
        self.assertEqual([e["index"] for e in out["entries"]], [1, 2])

    def test_grep_then_index(self) -> None:
        out = _tool_read_transl_cache(
            _Runner(self._entries()), {"filename": "01.json", "grep": "残留日文", "index": "2"}
        )
        self.assertEqual([e["index"] for e in out["entries"]], [2])
        self.assertNotIn("missing_indexes", out)
        out2 = _tool_read_transl_cache(
            _Runner(self._entries()), {"filename": "01.json", "grep": "残留日文", "index": "4"}
        )
        self.assertEqual(out2["returned"], 0)
        self.assertEqual(out2["missing_indexes"], [4])  # 被 grep 过滤掉的也算缺

    def test_empty_grep_is_a_noop(self) -> None:
        for grep in ("", "   ", []):
            out = _tool_read_transl_cache(_Runner(self._entries()), {"filename": "01.json", "grep": grep})
            self.assertEqual(out["count"], 4)
            self.assertNotIn("grep_note", out)

    def test_bad_grep_is_rejected(self) -> None:
        for bad in (["nope"], 123, {"a": 1}):
            with self.assertRaises(AgentToolError):
                _tool_read_transl_cache(_Runner(self._entries()), {"filename": "01.json", "grep": bad})


if __name__ == "__main__":
    unittest.main()
