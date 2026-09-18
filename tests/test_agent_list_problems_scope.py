"""list_problems 的文件范围：校对子代理按派活锁定后，只列自己负责的那几份文件的问题。

统计、命中数、分页与 context 取的前后文都跟着收窄；主 Agent（不传 allowed_files）保持全项目。
"""

import unittest
from types import SimpleNamespace

from GalTransl.Agent.runtime import _subagent_handlers, _tool_list_problems


def _problem(fname, index, problem="残留日文"):
    return {
        "filename": fname,
        "index": index,
        "speaker": "少女",
        "post_src": f"原文{index}",
        "pre_dst": f"译文{index}",
        "problem": problem,
        "trans_by": "engine",
    }


class _Runner:
    """最小 runner：/problems 回全部问题。"""

    def __init__(self, problems):
        self.state = SimpleNamespace(config_file_name="config.yaml")
        self._problems = problems

    def _project_id(self):
        return "proj"

    def _http_get(self, _path):
        return {
            "problems": [dict(p) for p in self._problems],
            "total": len(self._problems),
            "filter_keys": [],
        }


class StatsScopeTests(unittest.TestCase):
    def test_stats_only_count_scoped_files(self):
        runner = _Runner([
            _problem("a.json", 1, "残留日文"),
            _problem("a.json", 2, "缺控制符：/ ]"),
            _problem("b.json", 3, "残留日文"),
            _problem("b.json", 4, "缺控制符：/ ]"),
        ])

        out = _tool_list_problems(runner, {}, allowed_files=["a.json"])

        self.assertEqual(out["mode"], "stats")
        self.assertEqual(out["total"], 2)
        self.assertEqual(
            out["types"],
            [{"type": "残留日文", "count": 1}, {"type": "缺控制符", "count": 1}],
        )
        self.assertIn("a.json", out["note"])

    def test_no_scope_keeps_project_wide(self):
        runner = _Runner([_problem("a.json", 1), _problem("b.json", 2)])

        out = _tool_list_problems(runner, {})

        self.assertEqual(out["total"], 2)
        self.assertNotIn("note", out)


class DetailScopeTests(unittest.TestCase):
    def test_detail_and_paging_are_scoped(self):
        problems = [_problem("a.json", i) for i in range(1, 4)]
        problems += [_problem("b.json", i) for i in range(1, 31)]
        runner = _Runner(problems)

        out = _tool_list_problems(
            runner, {"problem_type": "残留日文", "limit": 20}, allowed_files=["a.json"]
        )

        self.assertEqual(out["matched"], 3)
        self.assertEqual([p["filename"] for p in out["problems"]], ["a.json"] * 3)
        self.assertFalse(out["has_more"])
        self.assertIn("a.json", out["note"])

    def test_multiple_locked_files(self):
        runner = _Runner([_problem("a.json", 1), _problem("b.json", 2), _problem("c.json", 3)])

        out = _tool_list_problems(runner, {"problem_type": "*"}, allowed_files=["a.json", "b.json"])

        self.assertEqual([p["filename"] for p in out["problems"]], ["a.json", "b.json"])
        self.assertIn("2 个文件", out["note"])

    def test_locked_file_without_problems_is_empty(self):
        runner = _Runner([_problem("b.json", 1)])

        out = _tool_list_problems(runner, {"problem_type": "*"}, allowed_files=["a.json"])

        self.assertEqual(out["matched"], 0)
        self.assertEqual(out["problems"], [])


class SubagentWiringTests(unittest.TestCase):
    def test_proofread_handler_is_scoped(self):
        runner = _Runner([_problem("a.json", 1), _problem("b.json", 2)])

        handler = _subagent_handlers("proofread", ["a.json"])["list_problems"]
        out = handler(runner, {"problem_type": "*"})

        self.assertEqual([p["filename"] for p in out["problems"]], ["a.json"])

    def test_unlocked_handler_stays_project_wide(self):
        runner = _Runner([_problem("a.json", 1), _problem("b.json", 2)])

        handler = _subagent_handlers("proofread", [])["list_problems"]
        out = handler(runner, {"problem_type": "*"})

        self.assertEqual([p["filename"] for p in out["problems"]], ["a.json", "b.json"])


if __name__ == "__main__":
    unittest.main()
