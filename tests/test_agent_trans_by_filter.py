"""trans_by 的显示过滤：大头记一次，少数派逐条留。

整本缓存的 trans_by 多半都是同一个模型的（翻译引擎那份），逐条给出来等于每次读缓存都白带
一列；真正要看的是异常来源——本会话 Agent 用 patch_transl_cache 改过的（记的是 Agent 的
模型名）、手工改的。两个读缓存的工具（read_transl_cache / search_transl_cache）同一套规则：

- **多数派**（这批里出现最多的那个值）逐条删掉，值本身记在顶层 `majority_trans_by` 一次；
- **少数派**逐条保留在条目的 `trans_by` 上；
- 空值逐条删掉，也不参与统计。

判"大头"按**数据本身**，不跟配置里「翻译任务会用」那份模型名比：项目当初是哪个模型翻的
只有条目自己知道，用户换过「翻译器默认」之后按配置比会把老条目全当成异常来源显示。

search 那列是服务端每行硬塞的，所以过滤在 Agent 侧做（服务端那份还要给界面用）；
read 默认不返回这列，只有 fields 点名要时才涉及。
"""

import unittest

from GalTransl.Agent.runtime import (
    AgentState,
    _dominant_trans_by,
    _strip_dominant_trans_by,
    _tool_read_transl_cache,
    _tool_search_transl_cache,
)

ENGINE_MODEL = "google/gemma-4-31B-it"
AGENT_MODEL = "deepseek-chat"


class _Runner:
    """最小 runner：读 /cache/{file} 与 /cache/search 各回一份固定数据。"""

    def __init__(self, entries=None, results=None) -> None:
        self.state = AgentState(project_dir=r"C:\proj", config_file_name="config.yaml")
        self._entries = entries or []
        self._results = results or []

    def _project_id(self):
        return "proj"

    def _http_get(self, url):
        return {"entries": [dict(e) for e in self._entries]}

    def _http_post(self, url, body):
        return {"results": [dict(r) for r in self._results], "total": len(self._results)}


class DominantTransByTests(unittest.TestCase):
    def test_most_frequent_value_wins(self) -> None:
        rows = [
            {"trans_by": ENGINE_MODEL},
            {"trans_by": ENGINE_MODEL},
            {"trans_by": AGENT_MODEL},
            {"trans_by": "manual"},
        ]
        self.assertEqual(_dominant_trans_by(rows), ENGINE_MODEL)

    def test_blank_values_do_not_count(self) -> None:
        rows = [{"trans_by": ""}, {"trans_by": "   "}, {"trans_by": AGENT_MODEL}]
        self.assertEqual(_dominant_trans_by(rows), AGENT_MODEL)

    def test_no_values_at_all(self) -> None:
        self.assertEqual(_dominant_trans_by([{"index": 1}, {"trans_by": ""}]), "")


class StripDominantTransByTests(unittest.TestCase):
    def test_dominant_and_blank_are_dropped(self) -> None:
        dominant_row = {"index": 1, "trans_by": ENGINE_MODEL}
        blank_row = {"index": 2, "trans_by": ""}
        other_row = {"index": 3, "trans_by": AGENT_MODEL}

        for row in (dominant_row, blank_row, other_row):
            _strip_dominant_trans_by(row, ENGINE_MODEL)

        self.assertNotIn("trans_by", dominant_row)
        self.assertNotIn("trans_by", blank_row)
        self.assertEqual(other_row["trans_by"], AGENT_MODEL)

    def test_unknown_dominant_only_drops_blanks(self) -> None:
        """判不出"大头"（这批一个标记都没有）时别乱删。"""
        row = {"index": 1, "trans_by": ENGINE_MODEL}
        _strip_dominant_trans_by(row, "")
        self.assertEqual(row["trans_by"], ENGINE_MODEL)


class SearchToolTransByTests(unittest.TestCase):
    def test_majority_is_hidden_and_reported_once(self) -> None:
        runner = _Runner(results=[
            {"index": 1, "trans_by": ENGINE_MODEL},
            {"index": 2, "trans_by": ENGINE_MODEL},
            {"index": 3, "trans_by": ENGINE_MODEL},
            {"index": 4, "trans_by": AGENT_MODEL},
        ])

        out = _tool_search_transl_cache(runner, {"query": "x"})

        # 多数派（引擎翻的）逐行不显示，改在顶层记一次
        self.assertNotIn("trans_by", out["results"][0])
        self.assertEqual(out["majority_trans_by"], ENGINE_MODEL)
        # 少数派逐条留着自己的来源
        self.assertEqual(out["results"][3]["trans_by"], AGENT_MODEL)

    def test_several_minority_sources_keep_their_own_value(self) -> None:
        runner = _Runner(results=[
            {"index": 1, "trans_by": ENGINE_MODEL},
            {"index": 2, "trans_by": ENGINE_MODEL},
            {"index": 3, "trans_by": "manual"},
            {"index": 4, "trans_by": AGENT_MODEL},
        ])

        out = _tool_search_transl_cache(runner, {"query": "x"})

        self.assertEqual(out["majority_trans_by"], ENGINE_MODEL)
        self.assertEqual(out["results"][2]["trans_by"], "manual")
        self.assertEqual(out["results"][3]["trans_by"], AGENT_MODEL)

    def test_single_source_reports_only_the_majority(self) -> None:
        runner = _Runner(results=[
            {"index": 1, "trans_by": ENGINE_MODEL},
            {"index": 2, "trans_by": ENGINE_MODEL},
        ])

        out = _tool_search_transl_cache(runner, {"query": "x"})

        self.assertEqual(out["majority_trans_by"], ENGINE_MODEL)
        for row in out["results"]:
            self.assertNotIn("trans_by", row)

    def test_blank_trans_by_is_dropped(self) -> None:
        runner = _Runner(results=[{"index": 1, "trans_by": ""}, {"index": 2, "trans_by": "   "}])

        out = _tool_search_transl_cache(runner, {"query": "x"})

        for row in out["results"]:
            self.assertNotIn("trans_by", row)
        self.assertNotIn("majority_trans_by", out)

    def test_slimming_keeps_other_fields_intact(self) -> None:
        runner = _Runner(results=[{"index": 1, "post_src": "原文", "pre_dst": "译文", "trans_by": ENGINE_MODEL}])

        out = _tool_search_transl_cache(runner, {"query": "x"})

        self.assertEqual(out["results"][0], {"index": 1, "post_src": "原文", "pre_dst": "译文"})


class ReadToolTransByTests(unittest.TestCase):
    def test_majority_is_stripped_and_minority_is_kept(self) -> None:
        runner = _Runner(entries=[
            {"index": 1, "post_src": "a", "pre_dst": "A", "trans_by": ENGINE_MODEL},
            {"index": 2, "post_src": "b", "pre_dst": "B", "trans_by": ENGINE_MODEL},
            {"index": 3, "post_src": "c", "pre_dst": "C", "trans_by": AGENT_MODEL},
        ])

        out = _tool_read_transl_cache(
            runner,
            {"filename": "01.json", "index": "1-3", "fields": ["post_src", "pre_dst", "trans_by"]},
        )

        by_index = {e["index"]: e for e in out["entries"]}
        self.assertNotIn("trans_by", by_index[1])  # 引擎翻的（多数派）
        self.assertNotIn("trans_by", by_index[2])
        self.assertEqual(by_index[3]["trans_by"], AGENT_MODEL)  # 别的来源：逐条留着
        self.assertEqual(out["majority_trans_by"], ENGINE_MODEL)

    def test_single_entry_keeps_the_answer_in_the_summary(self) -> None:
        """只点名一条时"多数派"就是它：逐条不给，但顶层记着，答案不丢。"""
        runner = _Runner(entries=[
            {"index": 7, "post_src": "a", "pre_dst": "A", "trans_by": AGENT_MODEL},
        ])

        out = _tool_read_transl_cache(
            runner, {"filename": "01.json", "index": "7", "fields": ["post_src", "trans_by"]}
        )

        self.assertNotIn("trans_by", out["entries"][0])
        self.assertEqual(out["majority_trans_by"], AGENT_MODEL)

    def test_default_fields_are_untouched(self) -> None:
        """默认精简集里没有 trans_by，这套处理不该给默认返回添任何键。"""
        runner = _Runner(entries=[
            {"index": 1, "post_src": "a", "pre_dst": "A", "trans_by": ENGINE_MODEL},
        ])

        out = _tool_read_transl_cache(runner, {"filename": "01.json", "index": "1"})

        self.assertNotIn("majority_trans_by", out)


if __name__ == "__main__":
    unittest.main()
