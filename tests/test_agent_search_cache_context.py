"""search_transl_cache 要支持 context（像 read_transl_cache 那样带上下文）。

背景：查「ドルード」时只给命中行，无法判断该统一译成「多鲁德」还是「杜罗德」——译名
一致性恰恰要看每处出现的前后对话。所以 search 增加 context=N（语义与 read 的一致：
命中条目 in_context=false，扩展出来的前后文 true）。

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
    """/cache/search 的返回给模型前先瘦身：逐行 match_* 收成一条汇总，in_context 只标上下文行。

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

    def test_in_context_is_only_marked_on_context_rows(self) -> None:
        runner = _SearchRunner({
            "results": [{"index": 3, "in_context": True}, {"index": 4, "in_context": False}],
            "total": 1,
        })

        out = _tool_search_transl_cache(runner, {"query": "x", "context": 1})

        self.assertIs(out["results"][0]["in_context"], True)
        self.assertNotIn("in_context", out["results"][1])


class SearchContextForwardingTests(unittest.TestCase):
    def test_context_is_forwarded_and_hits_tightened(self) -> None:
        runner = _SearchRunner({"results": [], "total": 0})
        _tool_search_transl_cache(runner, {"query": "ドルード", "context": 3})

        body = runner.bodies[0]
        self.assertEqual(body["context"], 3)
        self.assertEqual(body["max_results"], 300 // 7)  # 收紧到 42，避免 命中×7 行刷屏
        self.assertEqual(body["query"], "ドルード")

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

    def test_note_explains_in_context_and_cap(self) -> None:
        runner = _SearchRunner({"results": [{"index": 4, "in_context": False}], "total": 1})
        out = _tool_search_transl_cache(runner, {"query": "ドルード", "context": 2})

        self.assertEqual(out["context"], 2)
        note = out["note"]
        self.assertIn("in_context=true", note)
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
        self.assertIn("in_context=true", note)

    def test_no_note_without_context(self) -> None:
        runner = _SearchRunner({"results": [{"index": 1}], "total": 1})
        out = _tool_search_transl_cache(runner, {"query": "x"})
        self.assertNotIn("note", out)


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
