"""manage_problem_white_list 工具：list/add/remove 与审批卡预览。

与 manage_problem_filter 同构，区别是操作的是 common.problemWhiteList（一组
「缓存文件名:index」），且 add 时会校验条目格式，格式不对直接报错。
"""

import unittest
from types import SimpleNamespace

from GalTransl.Agent.runtime import (
    AgentToolError,
    _preview_tool_changes,
    _tool_manage_problem_white_list,
)


class _Runner:
    """最小 runner：_http_get 返回项目配置，_http_put 记录写回。"""

    def __init__(self, entries=None):
        self.state = SimpleNamespace(config_file_name="config.yaml")
        self.config = {"common": {"problemWhiteList": list(entries or [])}}
        self.puts = []

    def _project_id(self):
        return "proj"

    def _http_get(self, _path):
        return {"config": self.config}

    def _http_put(self, _path, body):
        self.puts.append(body)
        return {"success": True}


class ManageProblemWhiteListTests(unittest.TestCase):
    def test_list_returns_entries(self):
        runner = _Runner(["a.json:1"])
        result = _tool_manage_problem_white_list(runner, {"action": "list"})
        self.assertEqual(result, {"white_list": ["a.json:1"], "count": 1})

    def test_add_accepts_array_and_dedupes(self):
        runner = _Runner(["a.json:1"])
        result = _tool_manage_problem_white_list(
            runner,
            {"action": "add", "entry": ["a.json:1", "b.json:2", "b.json:2", " ", "a.json:3-4"]},
        )
        self.assertEqual(result["white_list"], ["a.json:1", "b.json:2", "a.json:3-4"])
        self.assertEqual(result["added"], ["b.json:2", "a.json:3-4"])
        self.assertEqual(result["already_present"], ["a.json:1"])
        self.assertEqual(
            runner.config["common"]["problemWhiteList"], ["a.json:1", "b.json:2", "a.json:3-4"]
        )
        self.assertEqual(len(runner.puts), 1)

    def test_invalid_entry_is_rejected(self):
        runner = _Runner([])
        with self.assertRaises(AgentToolError):
            _tool_manage_problem_white_list(runner, {"action": "add", "entry": ["a.json:1", "nonsense"]})
        self.assertEqual(runner.puts, [])

    def test_remove_reports_not_found(self):
        runner = _Runner(["a.json:1", "b.json:2"])
        result = _tool_manage_problem_white_list(
            runner, {"action": "remove", "entry": ["a.json:1", "z.json:9"]}
        )
        self.assertEqual(result["white_list"], ["b.json:2"])
        self.assertEqual(result["removed"], ["a.json:1"])
        self.assertEqual(result["not_found"], ["z.json:9"])

    def test_no_effective_change_skips_write(self):
        runner = _Runner(["a.json:1"])
        result = _tool_manage_problem_white_list(runner, {"action": "add", "entry": ["a.json:1"]})
        self.assertIn("note", result)
        self.assertEqual(runner.puts, [])

    def test_empty_entry_raises(self):
        runner = _Runner([])
        with self.assertRaises(AgentToolError):
            _tool_manage_problem_white_list(runner, {"action": "add", "entry": [" ", ""]})

    def test_bad_action_raises(self):
        with self.assertRaises(AgentToolError):
            _tool_manage_problem_white_list(_Runner([]), {"action": "clear"})


class PreviewTests(unittest.TestCase):
    def test_preview_add_and_remove(self):
        runner = _Runner(["a.json:1"])
        preview = _preview_tool_changes(
            runner, "manage_problem_white_list", {"action": "add", "entry": ["a.json:1", "b.json:2"]}
        )
        self.assertEqual([(c["kind"], c["after"]) for c in preview["changes"]], [("add", "b.json:2")])

        preview = _preview_tool_changes(
            runner, "manage_problem_white_list", {"action": "remove", "entry": ["a.json:1"]}
        )
        self.assertEqual([(c["kind"], c["before"]) for c in preview["changes"]], [("remove", "a.json:1")])

    def test_preview_none_when_nothing_would_change(self):
        runner = _Runner(["a.json:1"])
        for args in (
            {"action": "list"},
            {"action": "add", "entry": ["a.json:1"]},
            {"action": "add"},
        ):
            self.assertIsNone(_preview_tool_changes(runner, "manage_problem_white_list", args), args)


if __name__ == "__main__":
    unittest.main()
