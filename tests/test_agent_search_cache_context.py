"""search_transl_cache 要支持 context（像 read_transl_cache 那样带上下文）。

背景：查「ドルード」时只给命中行，无法判断该统一译成「多鲁德」还是「杜罗德」——译名
一致性恰恰要看每处出现的前后对话。所以 search 增加 context=N（语义与 read 的一致：
点名的命中条目前后各多带 N 句，不做任何标注）。

带上下文会让返回体变成 命中 × (2N+1) 行，所以命中上限要跟着收紧（total 不受影响）。
"""

import unittest
from unittest.mock import patch

from GalTransl.Agent.runtime import AgentRunner, AgentState, AgentToolError, _tool_search_transl_cache


class _SearchRunner:
    """最小 runner：记录 POST 的 body，按需返回命中。"""

    def __init__(self, payload: dict | None = None, cache_files: list[dict] | None = None) -> None:
        self.state = AgentState(config_file_name="config.yaml")
        self.payload = payload if payload is not None else {"results": [], "total": 0}
        self.cache_files = cache_files or []
        self.bodies: list[dict] = []

    def _project_id(self) -> str:
        return "proj"

    def _http_post(self, path: str, body: dict):
        assert path.endswith("/cache/search"), path
        self.bodies.append(body)
        return dict(self.payload)

    def _http_get(self, path: str):
        return {"files": list(self.cache_files)}


class SearchResultSlimmingTests(unittest.TestCase):
    """/cache/search 的返回给模型前先瘦身：逐行 match_* 收成一条汇总，in_context 删掉。

    服务端那份是给界面用的（缓存页拿 match_* 画「原文/译文/问题」小徽标），逐行丢给模型
    只是把每行都撑长一截——命中的位置从行内容（post_src / pre_dst / problem）直接看得到。
    """

    def test_match_flags_collapse_into_one_summary(self) -> None:
        runner = _SearchRunner({
            "results": [
                {"index": 1, "match_src": True, "match_dst": False, "match_problem": False},
                {"index": 2, "match_src": False, "match_dst": True, "match_problem": True},
            ],
            "total": 2,
        })

        out = _tool_search_transl_cache(runner, {"query": "ドルード", "field": "all"})

        self.assertEqual(out["matched_in"], {"src": 1, "dst": 1, "problem": 1})
        for row in out["results"]:
            for key in ("match_src", "match_dst", "match_problem"):
                self.assertNotIn(key, row)

    def test_single_field_search_has_no_summary(self) -> None:
        """指定了 field 的搜索只有那一侧会命中，汇总没有信息量，不给。"""
        runner = _SearchRunner({"results": [{"index": 1, "match_src": True}], "total": 1})

        out = _tool_search_transl_cache(runner, {"query": "x", "field": "src"})

        self.assertNotIn("matched_in", out)

    def test_in_context_is_stripped(self) -> None:
        """逐行的 in_context 字段删掉，改用 index 上的 * 表示"这行是上下文"。"""
        runner = _SearchRunner({
            "results": [{"index": 3, "in_context": True}, {"index": 4, "in_context": False}],
            "total": 1,
        })

        out = _tool_search_transl_cache(runner, {"query": "x", "context": 1})

        for row in out["results"]:
            self.assertNotIn("in_context", row)

    def test_context_rows_get_a_starred_index(self) -> None:
        """带上下文时：没有任何命中标记的行就是搭着给的上文，index 标 *。"""
        runner = _SearchRunner({
            "results": [
                {"index": 2, "match_src": False},
                {"index": 3, "match_src": True},
            ],
            "total": 1,
            "context": 1,
        })

        out = _tool_search_transl_cache(runner, {"query": "x", "context": 1})

        self.assertEqual([row["index"] for row in out["results"]], ["2*", 3])

    def test_without_context_no_row_is_starred(self) -> None:
        runner = _SearchRunner({"results": [{"index": 2, "match_src": False}], "total": 1})

        out = _tool_search_transl_cache(runner, {"query": "x"})

        self.assertEqual([row["index"] for row in out["results"]], [2])


class SearchContextForwardingTests(unittest.TestCase):
    def test_context_is_forwarded_and_hits_tightened(self) -> None:
        runner = _SearchRunner({"results": [], "total": 0})
        _tool_search_transl_cache(runner, {"query": "ドルード", "context": 3})

        body = runner.bodies[0]
        self.assertEqual(body["context"], 3)
        self.assertTrue(body["preceding_only"])  # 默认只给上文（省 token）
        self.assertEqual(body["max_results"], 200 // 4)  # 每条只搭 3 行 → 50，整页压在 200 行内
        self.assertEqual(body["query"], "ドルード")

    def test_only_preceding_false_asks_for_both_sides(self) -> None:
        runner = _SearchRunner({"results": [], "total": 0})
        _tool_search_transl_cache(
            runner, {"query": "ドルード", "context": 3, "only_preceding": False}
        )

        body = runner.bodies[0]
        self.assertNotIn("preceding_only", body)  # 服务端默认两边都给，不必显式发
        self.assertEqual(body["max_results"], 200 // 7)  # 每条 7 行 → 28

    def test_without_context_behaviour_unchanged(self) -> None:
        runner = _SearchRunner({"results": [], "total": 0})
        _tool_search_transl_cache(runner, {"query": "アクメ"})

        body = runner.bodies[0]
        self.assertNotIn("context", body)  # 不带就不发，老行为
        self.assertEqual(body["max_results"], 100)

    def test_context_is_clamped_and_validated(self) -> None:
        runner = _SearchRunner()
        _tool_search_transl_cache(runner, {"query": "x", "context": 99})
        self.assertEqual(runner.bodies[0]["context"], 20)  # 上限 20

        _tool_search_transl_cache(runner, {"query": "x", "context": -3})
        self.assertNotIn("context", runner.bodies[1])  # 负数按 0（不带）

        with self.assertRaises(AgentToolError) as ctx:
            _tool_search_transl_cache(runner, {"query": "x", "context": "abc"})
        self.assertIn("context", str(ctx.exception))

    def test_note_explains_context_and_cap(self) -> None:
        runner = _SearchRunner({"results": [{"index": 4}], "total": 1})
        out = _tool_search_transl_cache(runner, {"query": "ドルード", "context": 2})

        self.assertEqual(out["context"], 2)
        note = out["note"]
        self.assertIn("包含关键词的那行是命中", note)
        self.assertIn("命中上限收紧为", note)
        self.assertIn("total 仍是全部命中数", note)

    def test_missing_file_note_and_context_note_are_both_kept(self) -> None:
        """0 命中 + 文件不存在时的提示不能把上下文说明顶掉。"""
        runner = _SearchRunner({"results": [], "total": 0}, cache_files=[{"name": "other.json"}])
        out = _tool_search_transl_cache(
            runner, {"query": "ドルード", "context": 1, "filename": "sc_2_st01.json"}
        )

        note = out["note"]
        self.assertIn("不存在", note)
        self.assertIn("包含关键词的那行是命中", note)

    def test_no_note_without_context(self) -> None:
        runner = _SearchRunner({"results": [{"index": 1}], "total": 1})
        out = _tool_search_transl_cache(runner, {"query": "x"})
        self.assertNotIn("note", out)


class SearchPagingTests(unittest.TestCase):
    """limit / offset：与 list_problems 同一套分页口径（本页命中数 returned + has_more）。

    默认 100 是历史行为（"一次看遍某个词的所有出现处"是搜索的常用姿势）；上限 200 免得
    一次把返回体撑爆，带 context 时还会按总行数进一步收紧。
    """

    def test_default_page_keeps_history_and_sends_no_offset(self) -> None:
        runner = _SearchRunner({"results": [{"index": 1}], "total": 3})

        out = _tool_search_transl_cache(runner, {"query": "x"})

        self.assertEqual(runner.bodies[0]["max_results"], 100)
        self.assertNotIn("offset", runner.bodies[0])  # 不翻页就不发这个键
        self.assertEqual(out["offset"], 0)
        self.assertEqual(out["returned"], 1)
        self.assertTrue(out["has_more"])

    def test_limit_and_offset_are_forwarded_and_clamped(self) -> None:
        runner = _SearchRunner({"results": [], "total": 0})
        _tool_search_transl_cache(runner, {"query": "x", "limit": 9999, "offset": 100})

        body = runner.bodies[0]
        self.assertEqual(body["max_results"], 200)  # 上限
        self.assertEqual(body["offset"], 100)

    def test_explicit_limit_wins_over_the_row_budget(self) -> None:
        runner = _SearchRunner({"results": [], "total": 0})
        _tool_search_transl_cache(runner, {"query": "x", "context": 1, "limit": 5})

        self.assertEqual(runner.bodies[0]["max_results"], 5)  # 行数上限 100，取更小的 5

    def test_bad_limit_and_offset_fall_back_to_defaults(self) -> None:
        runner = _SearchRunner({"results": [], "total": 0})
        _tool_search_transl_cache(runner, {"query": "x", "limit": "abc", "offset": "abc"})

        self.assertEqual(runner.bodies[0]["max_results"], 100)
        self.assertNotIn("offset", runner.bodies[0])

    def test_last_page_has_no_more(self) -> None:
        runner = _SearchRunner({"results": [{"index": 9}], "total": 101})

        out = _tool_search_transl_cache(runner, {"query": "x", "offset": 100})

        self.assertEqual(out["offset"], 100)
        self.assertEqual(out["returned"], 1)
        self.assertFalse(out["has_more"])

    def test_page_rows_never_exceed_the_line_budget(self) -> None:
        """整页最多 200 行（limit 拉满也一样）：只给上文时每条搭 N 行，两边都给时 2N+1 行。"""
        for context in (1, 2, 3, 5, 10, 20):
            for only_preceding, rows_per_hit in ((True, context + 1), (False, 2 * context + 1)):
                runner = _SearchRunner({"results": [], "total": 0})

                _tool_search_transl_cache(
                    runner,
                    {"query": "x", "context": context, "limit": 200, "only_preceding": only_preceding},
                )

                hits = runner.bodies[0]["max_results"]
                self.assertLessEqual(hits * rows_per_hit, 200, f"context={context} / {only_preceding}")

    def test_context_keeps_hits_and_rows_apart(self) -> None:
        runner = _SearchRunner({
            "results": [{"index": 3}, {"index": 4}, {"index": 5}],
            "total": 9,
            "context": 1,
            "returned_hits": 1,
            "returned": 3,  # 1 条命中 + 前后各 1 句
        })

        out = _tool_search_transl_cache(runner, {"query": "x", "context": 1})

        self.assertEqual(out["returned"], 1)  # 命中数
        self.assertEqual(out["returned_rows"], 3)  # 含前后文的行数
        self.assertTrue(out["has_more"])


class RealRunnerBodyTests(unittest.TestCase):
    """用真 AgentRunner 走一遍 body 组装（config 名进 body、filename 过滤）。"""

    def test_filename_filters_and_config_travels(self) -> None:
        state = AgentState(config_file_name="config v2.yaml")
        runner = AgentRunner(state)
        captured: list[tuple[str, dict]] = []

        def fake_post(_self, path: str, body: dict):
            captured.append((path, body))
            return {"results": [], "total": 0}

        with patch.object(AgentRunner, "_http_post", fake_post):
            _tool_search_transl_cache(
                runner, {"query": "ドルード", "field": "src", "filename": "a b.json", "context": 1}
            )

        path, body = captured[0]
        self.assertTrue(path.endswith("/cache/search"), path)
        self.assertEqual(body["filename"], "a b.json")
        self.assertEqual(body["field"], "src")
        self.assertEqual(body["config_file_name"], "config v2.yaml")
        self.assertEqual(body["context"], 1)


if __name__ == "__main__":
    unittest.main()
