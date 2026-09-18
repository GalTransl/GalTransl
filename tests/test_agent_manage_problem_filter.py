import unittest
from types import SimpleNamespace

from GalTransl.Agent.runtime import AGENT_TOOLS, AgentToolError, _tool_manage_problem_filter


class _Runner:
    """最小 runner：_http_get 返回项目配置与过滤统计，_http_put 记录写回。"""

    def __init__(self, filter_keys=None, filters=None, problem_entries=None, visible_entries=None):
        self.state = SimpleNamespace(config_file_name="config.yaml")
        self.config = {"common": {"problemFilterKey": list(filter_keys or [])}}
        self.puts = []
        # filters 不给就是"服务端拿不到统计"（老服务端/接口异常）：list 应退回只有清单的结果
        self.stats = None
        if filters is not None:
            self.stats = {
                "filter_keys": list(filter_keys or []),
                "filters": list(filters),
                "problem_entries": problem_entries,
                "visible_entries": visible_entries,
            }

    def _project_id(self):
        return "proj"

    def _http_get(self, path):
        if "problem_filter_stats" in path:
            if self.stats is None:
                raise RuntimeError("stats endpoint unavailable")
            return dict(self.stats)
        return {"config": self.config}

    def _http_put(self, _path, body):
        self.puts.append(body)
        return {"success": True}


class ManageProblemFilterKeywordArrayTests(unittest.TestCase):
    """keyword 支持字符串或数组；数组一次增删多个，去重、空串忽略。"""

    def test_add_accepts_array_and_dedupes(self) -> None:
        runner = _Runner(["A"])
        result = _tool_manage_problem_filter(
            runner, {"action": "add", "keyword": ["A", "B", "B", " ", "C"]}
        )
        self.assertEqual(result["filter_keys"], ["A", "B", "C"])
        self.assertEqual(result["added"], ["B", "C"])
        self.assertEqual(result["already_present"], ["A"])
        self.assertEqual([c["after"] for c in result["changes"]], ["B", "C"])
        self.assertEqual(runner.config["common"]["problemFilterKey"], ["A", "B", "C"])
        self.assertEqual(len(runner.puts), 1)

    def test_single_string_still_supported(self) -> None:
        runner = _Runner([])
        result = _tool_manage_problem_filter(runner, {"action": "add", "keyword": "X"})
        self.assertEqual(result["added"], ["X"])
        self.assertNotIn("already_present", result)

    def test_remove_array_reports_not_found(self) -> None:
        runner = _Runner(["A", "B", "C"])
        result = _tool_manage_problem_filter(runner, {"action": "remove", "keyword": ["B", "Z"]})
        self.assertEqual(result["filter_keys"], ["A", "C"])
        self.assertEqual(result["removed"], ["B"])
        self.assertEqual(result["not_found"], ["Z"])
        self.assertEqual([c["before"] for c in result["changes"]], ["B"])

    def test_no_effective_change_skips_write(self) -> None:
        runner = _Runner(["A"])
        result = _tool_manage_problem_filter(runner, {"action": "add", "keyword": ["A"]})
        self.assertIn("note", result)
        self.assertNotIn("changes", result)
        self.assertEqual(runner.puts, [])

    def test_empty_keyword_raises(self) -> None:
        runner = _Runner([])
        with self.assertRaises(AgentToolError):
            _tool_manage_problem_filter(runner, {"action": "add", "keyword": [" ", ""]})

    def test_list_returns_keys_with_hit_counts(self) -> None:
        runner = _Runner(
            ["残留日文", "缺失.*标点"],
            filters=[
                {"key": "残留日文", "problems": 5},
                {"key": "缺失.*标点", "problems": 0},
            ],
            problem_entries=12,
            visible_entries=7,
        )

        result = _tool_manage_problem_filter(runner, {"action": "list"})

        self.assertEqual(result["filter_keys"], ["残留日文", "缺失.*标点"])
        self.assertEqual(result["count"], 2)
        self.assertEqual(
            result["filters"],
            [
                {"key": "残留日文", "problems": 5},
                {"key": "缺失.*标点", "problems": 0},
            ],
        )
        self.assertEqual(result["problem_entries"], 12)
        self.assertEqual(result["visible_entries"], 7)

    def test_list_empty_does_not_ask_for_stats(self) -> None:
        # 清单为空时没有"每条命中多少"可算，也不必多打一次接口
        runner = _Runner([])
        self.assertEqual(
            _tool_manage_problem_filter(runner, {"action": "list"}),
            {"filter_keys": [], "count": 0},
        )

    def test_list_falls_back_when_stats_unavailable(self) -> None:
        # 统计只是额外信息：拿不到也要给出清单本身
        runner = _Runner(["A"])
        self.assertEqual(
            _tool_manage_problem_filter(runner, {"action": "list"}),
            {"filter_keys": ["A"], "count": 1},
        )

    def test_add_accepts_regex_patterns(self) -> None:
        runner = _Runner([])
        result = _tool_manage_problem_filter(
            runner, {"action": "add", "keyword": ["^残留日文：", r"缺控制符：<\w+>"]}
        )
        self.assertEqual(result["added"], ["^残留日文：", r"缺控制符：<\w+>"])

    def test_add_rejects_invalid_regex(self) -> None:
        runner = _Runner([])
        with self.assertRaises(AgentToolError) as ctx:
            _tool_manage_problem_filter(runner, {"action": "add", "keyword": ["^残留日文：", "("]})
        self.assertIn("(", str(ctx.exception))
        self.assertIn("转义", str(ctx.exception))
        self.assertEqual(runner.puts, [])

    def test_remove_does_not_validate_regex(self) -> None:
        # 清单里可能已经躺着一条坏模式（手改配置留下的）：必须还能删掉它
        runner = _Runner(["("])
        result = _tool_manage_problem_filter(runner, {"action": "remove", "keyword": ["("]})
        self.assertEqual(result["removed"], ["("])


class ManageProblemFilterPromptTests(unittest.TestCase):
    """提示层限制：原则上只过滤小类，不要整类过滤（大类里混着真问题）。"""

    @staticmethod
    def _description() -> str:
        schema = next(t for t in AGENT_TOOLS if t["function"]["name"] == "manage_problem_filter")
        return schema["function"]["description"]

    def test_description_forbids_category_wide_filtering(self) -> None:
        description = self._description()
        self.assertIn("原则上只过滤小类", description)
        self.assertIn("不要过滤大类", description)

    def test_description_points_to_white_list_for_single_entries(self) -> None:
        self.assertIn("manage_problem_white_list", self._description())


if __name__ == "__main__":
    unittest.main()
