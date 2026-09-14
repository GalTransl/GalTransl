import unittest
from types import SimpleNamespace

from GalTransl.Agent.runtime import AgentToolError, _tool_manage_problem_filter


class _Runner:
    """最小 runner：_http_get 返回项目配置，_http_put 记录写回。"""

    def __init__(self, filter_keys=None):
        self.state = SimpleNamespace(config_file_name="config.yaml")
        self.config = {"common": {"problemFilterKey": list(filter_keys or [])}}
        self.puts = []

    def _project_id(self):
        return "proj"

    def _http_get(self, _path):
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

    def test_list_returns_keys(self) -> None:
        runner = _Runner(["A", "B"])
        result = _tool_manage_problem_filter(runner, {"action": "list"})
        self.assertEqual(result, {"filter_keys": ["A", "B"], "count": 2})


if __name__ == "__main__":
    unittest.main()
