"""problems(action="list") 的明细聚合：类型统计带出每类最多的几种明细，group_by="detail" 给完整排行。

回归背景：统计只到类型（「残留日文 800」），而能不能批量处理取决于明细的分布——是
「♪」一种占了 600 条，还是 800 条各不相同。以前只能按每页最多 20 条一页页翻条目来看，
流程里"先找可批量解决的共同模式"实际做不到。
"""

import unittest
from types import SimpleNamespace

from GalTransl.Agent.runtime import AgentToolError, _subagent_handlers, _tool_list_problems
from GalTransl.Agent.tools.common import _problem_details
from GalTransl.Agent.tools.render_md import _render_tool_result_table


def _problem(fname, index, problem):
    return {
        "filename": fname, "index": index, "speaker": "少女",
        "post_src": f"原文{index}", "pre_dst": f"译文{index}", "problem": problem, "trans_by": "engine",
    }


class _Runner:
    def __init__(self, problems):
        self.state = SimpleNamespace(config_file_name="config.yaml")
        self._problems = problems

    def _project_id(self):
        return "proj"

    def _http_get(self, _path):
        return {"problems": [dict(p) for p in self._problems], "total": len(self._problems), "filter_keys": []}


PROBLEMS = [
    _problem("a.json", 1, "残留日文：♪"),
    _problem("a.json", 2, "残留日文：♪, 丢失换行"),
    _problem("a.json", 3, "残留日文：の"),
    _problem("b.json", 4, "残留日文：♪, 残留日文：♪"),  # 同一条里重复的明细只记一次
    _problem("b.json", 5, "残留日文：は"),
    _problem("b.json", 6, "残留日文：が"),
    _problem("b.json", 7, "缺控制符：/ ]"),
]


class ProblemDetailTests(unittest.TestCase):
    def test_details_are_split_per_type_and_deduplicated(self):
        self.assertEqual(
            _problem_details("残留日文：♪, 残留日文：♪, 残留日文：の, 丢失换行"),
            {"残留日文": ["♪", "の"], "丢失换行": [""]},
        )

    def test_stats_carry_the_most_frequent_details_of_each_type(self):
        out = _tool_list_problems(_Runner(PROBLEMS), {})
        types = {row["type"]: row for row in out["types"]}
        self.assertEqual(types["残留日文"]["count"], 6)
        self.assertEqual(types["残留日文"]["top_details"][0], {"detail": "♪", "count": 3})
        self.assertEqual(len(types["残留日文"]["top_details"]), 3)
        self.assertEqual(types["残留日文"]["details_total"], 4)
        # 不带明细的类型不多出空列
        self.assertNotIn("top_details", types["丢失换行"])

        text = _render_tool_result_table("list_problems", out)
        self.assertIn("| type | count | top_details |", text)
        self.assertIn("♪×3", text)
        self.assertIn("等4种", text)

    def test_stats_without_any_detail_keep_the_two_column_table(self):
        out = _tool_list_problems(_Runner([_problem("a.json", 1, "丢失换行")]), {})
        text = _render_tool_result_table("list_problems", out)
        self.assertIn("| type | count |", text)
        self.assertNotIn("top_details |", text)

    def test_group_by_detail_ranks_and_pages_the_full_list(self):
        runner = _Runner(PROBLEMS)
        out = _tool_list_problems(runner, {"problem_type": "残留日文", "group_by": "detail", "limit": 2})
        self.assertEqual(out["mode"], "details")
        self.assertEqual((out["matched"], out["details_total"]), (6, 4))
        self.assertEqual(
            out["details"],
            [{"detail": "♪", "count": 3, "example": "a.json:1"}, {"detail": "の", "count": 1, "example": "a.json:3"}],
        )
        self.assertEqual((out["has_more"], out["next_offset"]), (True, 2))
        text = _render_tool_result_table("list_problems", out)
        self.assertIn("| detail | count | example |", text)
        self.assertIn("offset=2", text)

        last = _tool_list_problems(runner, {"problem_type": "残留日文", "group_by": "detail", "limit": 2, "offset": 2})
        self.assertEqual([row["detail"] for row in last["details"]], ["は", "が"])
        self.assertFalse(last["has_more"])

    def test_group_by_needs_one_concrete_type(self):
        runner = _Runner(PROBLEMS)
        for args in (
            {"problem_type": "*", "group_by": "detail"},
            {"problem_type": "残留日文,缺控制符", "group_by": "detail"},
            {"problem_type": "残留日文", "group_by": "file"},
        ):
            with self.subTest(args=args), self.assertRaises(AgentToolError):
                _tool_list_problems(runner, args)

    def test_proofread_scope_narrows_the_detail_ranking_too(self):
        handler = _subagent_handlers("proofread", ["b.json"])["list_problems"]
        out = handler(_Runner(PROBLEMS), {"problem_type": "残留日文", "group_by": "detail"})
        self.assertEqual([row["detail"] for row in out["details"]], ["♪", "は", "が"])
        self.assertEqual(out["details"][0]["count"], 1)
        self.assertIn("b.json", out["note"])


if __name__ == "__main__":
    unittest.main()
