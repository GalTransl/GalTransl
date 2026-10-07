import json
import unittest
from types import SimpleNamespace

from GalTransl.Agent.runtime import AGENT_TOOLS, AgentToolError, _tool_manage_problem_filter
from GalTransl.Agent.tools.preview import _preview_problem_filter
from GalTransl.ProblemFilter import filter_problem_text


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


class EncodedFilterInputTests(unittest.TestCase):
    patterns = ["^GPT字典未使用", "^项目GPT字典-生成未使用", "^项目GPT字典未使用"]

    def test_encoded_inputs_are_decoded_before_preview_and_write(self):
        encoded = json.dumps(self.patterns, ensure_ascii=False)
        cases = [(json.dumps(self.patterns[0], ensure_ascii=False), self.patterns[:1]),
                 (json.dumps(self.patterns[:1]), self.patterns[:1]), (encoded, self.patterns),
                 (json.dumps(self.patterns, ensure_ascii=False, indent=2), self.patterns),
                 ([self.patterns[0], encoded], self.patterns),
                 (self.patterns[0] + "\n" + encoded, self.patterns),
                 (json.dumps(encoded), self.patterns)]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                runner = _Runner()
                args = {"action": "add", "keyword": raw}
                preview = _preview_problem_filter(runner, args)
                result = _tool_manage_problem_filter(runner, args)
                self.assertEqual(result["added"], expected)
                self.assertEqual(result["count"], len(expected))
                self.assertEqual(preview["changes"], result["changes"])
                self.assertEqual(runner.config["common"]["problemFilterKey"], expected)
                # 用户给出的三个词仍只过滤字典问题，不能变成一个巨大的正则字符集。
                text = "GPT字典未使用：A, 本无括号, 本无冒号, 比日文长：1.5倍"
                self.assertEqual(filter_problem_text(text, result["filter_keys"]),
                                 "本无括号, 本无冒号, 比日文长：1.5倍")
                again = _tool_manage_problem_filter(runner, args)
                self.assertNotIn("changes", again)
                self.assertEqual(len(runner.puts), 1)

    def test_invalid_encoded_batch_is_rejected_atomically(self):
        too_deep = self.patterns
        for _ in range(10):
            too_deep = json.dumps(too_deep)
        cases = [{"patterns": self.patterns}, ["^合法规则", 42],
                 json.dumps(["^合法规则", None]), json.dumps(["^合法规则", "("]),
                 '["^GPT字典未使用",', too_deep]
        for raw in cases:
            with self.subTest(raw=str(raw)[:80]):
                runner = _Runner(["^已有规则"])
                args = {"action": "add", "keyword": raw}
                self.assertIsNone(_preview_problem_filter(runner, args))
                with self.assertRaises(AgentToolError):
                    _tool_manage_problem_filter(runner, args)
                self.assertEqual(runner.puts, [])
                self.assertEqual(runner.config["common"]["problemFilterKey"], ["^已有规则"])

    def test_encoded_remove_uses_decoded_keys_unless_original_rule_exists(self):
        encoded = json.dumps(self.patterns)
        runner = _Runner(self.patterns)
        args = {"action": "remove", "keyword": encoded}
        preview = _preview_problem_filter(runner, args)
        result = _tool_manage_problem_filter(runner, args)
        self.assertEqual(result["removed"], self.patterns)
        self.assertEqual(result["filter_keys"], [])
        self.assertEqual(preview["changes"], result["changes"])
        runner = _Runner([encoded, *self.patterns])
        result = _tool_manage_problem_filter(runner, args)
        self.assertEqual(result["removed"], [encoded])
        self.assertEqual(result["filter_keys"], self.patterns)

    def test_real_array_filters_only_requested_dictionary_problems(self):
        runner = _Runner()
        args = {"action": "add", "keyword": self.patterns}
        preview = _preview_problem_filter(runner, args)
        result = _tool_manage_problem_filter(runner, args)
        self.assertEqual(result["count"], 3)
        self.assertEqual(preview["changes"], result["changes"])
        text = "GPT字典未使用：A, 项目GPT字典-生成未使用：B, 项目GPT字典未使用：C, 本无括号, 本无冒号, 比日文长：1.5倍"
        self.assertEqual(filter_problem_text(text, runner.config["common"]["problemFilterKey"]),
                         "本无括号, 本无冒号, 比日文长：1.5倍")

    def test_bad_saved_rules_remain_visible_and_removable_by_exact_value(self):
        for bad in (json.dumps(self.patterns[0], ensure_ascii=False), json.dumps(self.patterns, ensure_ascii=False)):
            with self.subTest(bad=bad):
                runner = _Runner([bad, "^合法规则"])
                self.assertEqual(_tool_manage_problem_filter(runner, {"action": "list"})["filter_keys"][0], bad)
                args = {"action": "remove", "keyword": bad}
                preview = _preview_problem_filter(runner, args)
                result = _tool_manage_problem_filter(runner, args)
                self.assertEqual(result["removed"], [bad])
                self.assertEqual(result["filter_keys"], ["^合法规则"])
                self.assertEqual(preview["changes"], result["changes"])

    def test_regex_character_classes_and_escaped_quotes_still_work(self):
        patterns = [r"^[AB]类", r"^\[标记\]", r'\"带引号\"']
        runner = _Runner()
        _tool_manage_problem_filter(runner, {"action": "add", "keyword": patterns})
        self.assertEqual(filter_problem_text('A类问题, [标记]问题, "带引号"问题, 本无括号',
                                            runner.config["common"]["problemFilterKey"]), "本无括号")


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
