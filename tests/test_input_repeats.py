"""原文重复句统计：POST /input/repeats 的扫描逻辑与 Agent 的 input(action="repeats")。

背景：各批次独立送翻，同一句原文（系统文本、选项、口头禅）容易译出几种写法，而这类
不一致以前只能等全量翻完后在缓存里发现。这里守护的是"翻译前就能看到哪些整句重复"：
分组口径（去首尾空白的整句、不区分说话人）、次数排序、筛选与分页，以及工具层的登记。

原文要过文件插件才能解析，与 test_server_input_search 一样把 _load_input_file_entries
换成假的；工具层的假 runner 直接调用真实的 _input_repeats，渲染吃到的是真实返回体。
"""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

try:
    from GalTransl import INPUT_FOLDERNAME
    from GalTransl import server
except ModuleNotFoundError:  # 精简环境（如系统 python）没有 yaml，跳过
    raise unittest.SkipTest("server 依赖不可用")

from GalTransl.Agent import runtime as rt
from GalTransl.Agent.subagent import SUBAGENT_AGENT_EXPLORE, _subagent_tools
from GalTransl.Agent.tool_operations import tool_operation
from GalTransl.Agent.tools.input import _tool_input_repeats
from GalTransl.Agent.tools.render_md import _render_tool_result_table


def _entries(pairs: list[tuple[str, str]]) -> list[dict]:
    return [
        {"index": i, "name": name, "pre_src": src, "speaker": name}
        for i, (name, src) in enumerate(pairs, start=1)
    ]


FILES = {
    "a.json": _entries([
        ("少女", "おはよう"),
        ("", "……"),
        ("少女", "ただいま"),
        ("アリス", " おはよう "),  # 首尾空白不影响分组
        ("", "「……」"),  # 纯符号句：不参与统计
        ("少女", "はい"),
    ]),
    "b.json": _entries([
        ("少女", "おはよう"),
        ("", "……"),
        ("ボブ", "ただいま"),
        ("ボブ", "一度きり"),
        ("ボブ", ""),  # 空句不计
        ("ボブ", "はい"),
    ]),
}


class _Project(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="galtransl-input-repeats-")
        input_dir = os.path.join(self.root, INPUT_FOLDERNAME)
        os.makedirs(input_dir, exist_ok=True)
        for name in (*FILES, "boom.json"):
            with open(os.path.join(input_dir, name), "w", encoding="utf-8") as f:
                f.write("[]")
        self.broken: set[str] = {"boom.json"}

    def _repeats(self, **kwargs):
        def fake_loader(_project_dir, _config_name, name):
            if name in self.broken:
                raise RuntimeError("解析失败")
            return list(FILES[name])

        with patch.object(server, "_load_input_file_entries", fake_loader):
            return server._input_repeats(self.root, "config.yaml", **kwargs)


class InputRepeatsServerTests(_Project):
    def test_groups_whole_sentences_across_files_most_frequent_first(self):
        out = self._repeats()
        self.assertEqual(
            [(row["src"], row["count"], row["files"], row["first"]) for row in out["results"]],
            [("おはよう", 3, 2, "a.json:1"), ("ただいま", 2, 2, "a.json:3"), ("はい", 2, 2, "a.json:6")],
        )
        # 只出现一次的句子不算重复；空句不计入总数
        self.assertEqual(out["total"], 3)
        self.assertEqual(out["sentences_total"], 11)
        self.assertEqual(out["unique_sentences"], 4)
        self.assertEqual(out["repeated_sentences"], 3)
        self.assertEqual(out["repeated_occurrences"], 7)

    def test_symbol_only_sentences_are_ignored_but_counted(self):
        """「……」这类没有文字的句子在任何项目里都排最前，却没有译法可定：不进结果，只报数量。"""
        out = self._repeats(query="…")
        self.assertEqual(out["results"], [])
        self.assertEqual(out["symbol_only_sentences"], 3)

    def test_speakers_are_reported_not_split_into_groups(self):
        """同一句由不同角色说出来仍是一组：该不该同译由读的人看 speakers 判断。"""
        row = self._repeats(query="おはよう")["results"][0]
        self.assertEqual(row["speakers"], [{"name": "少女", "count": 2}, {"name": "アリス", "count": 1}])
        self.assertEqual(row["speakers_total"], 2)

    def test_thresholds_and_query_filter_but_keep_project_totals(self):
        out = self._repeats(min_count=2, min_length=3)
        self.assertEqual([row["src"] for row in out["results"]], ["おはよう", "ただいま"])
        self.assertEqual(out["total"], 2)
        self.assertEqual(out["repeated_sentences"], 3)  # 全项目口径，不受 min_length 影响
        self.assertEqual([row["src"] for row in self._repeats(min_count=3)["results"]], ["おはよう"])

    def test_paging_skips_groups_and_reports_the_full_total(self):
        out = self._repeats(max_results=1, offset=1)
        self.assertEqual([row["src"] for row in out["results"]], ["ただいま"])
        self.assertEqual((out["total"], out["offset"]), (3, 1))

    def test_unparsable_file_is_reported_and_does_not_fail_the_scan(self):
        self.assertEqual(self._repeats()["files_failed"], ["boom.json"])
        self.broken = set()
        with patch.dict(FILES, {"boom.json": []}):
            self.assertNotIn("files_failed", self._repeats())


class InputRepeatsToolTests(_Project):
    def _runner(self):
        test = self

        class Runner:
            state = SimpleNamespace(config_file_name="config.yaml")
            posts: list[tuple[str, dict]] = []

            def _project_id(self):
                return "project"

            def _http_post(self, url, body):
                self.posts.append((url, body))
                return test._repeats(**{
                    key: body[key] for key in ("query", "min_count", "min_length", "max_results", "offset")
                })

        return Runner()

    def test_tool_forwards_arguments_pages_and_renders_a_table(self):
        runner = self._runner()
        result = _tool_input_repeats(runner, {"limit": 2, "min_length": 2})
        self.assertIn("纯符号句 3 句不参与统计", _render_tool_result_table("input_repeats", result))
        url, body = runner.posts[0]
        self.assertTrue(url.endswith("/input/repeats"))
        self.assertEqual((body["max_results"], body["min_count"], body["min_length"]), (2, 2, 2))
        self.assertEqual((result["returned"], result["has_more"], result["next_offset"]), (2, True, 2))
        text = _render_tool_result_table("input_repeats", result)
        self.assertIn("| src | count | files | speakers | first |", text)
        self.assertIn("少女×2、アリス×1", text)
        self.assertIn("offset=2", text)
        self.assertIn("没参与统计：boom.json", text)

        last = _tool_input_repeats(runner, {"limit": 2, "offset": 2})
        self.assertFalse(last["has_more"])
        self.assertNotIn("next_offset", last)

    def test_invalid_numbers_are_rejected_before_any_request(self):
        runner = self._runner()
        for args in ({"min_count": "many"}, {"limit": None}, {"offset": "x"}):
            with self.subTest(args=args), self.assertRaises(rt.AgentToolError):
                _tool_input_repeats(runner, args)
        self.assertEqual(runner.posts, [])

    def test_registered_as_a_read_only_action_of_the_input_tool(self):
        self.assertEqual(tool_operation("input", {"action": "repeats"}), "input_repeats")
        self.assertIs(rt._TOOL_HANDLERS["input_repeats"], _tool_input_repeats)
        self.assertEqual(rt._tool_risk("input_repeats"), rt.PERMISSION_READ)
        schema = next(t["function"] for t in rt.AGENT_TOOLS if t["function"]["name"] == "input")
        self.assertIn("repeats", schema["parameters"]["properties"]["action"]["enum"])

    def test_explore_subagent_schema_does_not_advertise_it(self):
        """原文探索的白名单里没有这个操作，schema 里也不该留下它的动作和参数。"""
        schema = next(
            t["function"]["parameters"]["properties"]
            for t in _subagent_tools(SUBAGENT_AGENT_EXPLORE) if t["function"]["name"] == "input"
        )
        self.assertEqual(schema["action"]["enum"], ["list", "read", "search"])
        self.assertNotIn("min_count", schema)
        self.assertNotIn("min_length", schema)


class WorkflowPromptTests(unittest.TestCase):
    """提示词里这几条是产品行为：重复句在翻译前处理、译名核查不等到全量之后、按请求范围选入口。"""

    def test_prompt_covers_repeats_entry_routing_sampling_and_early_name_check(self):
        prompt = rt.AGENT_SYSTEM_PROMPT
        self.assertLess(prompt.index('input(action="repeats")'), prompt.index("3. **试译定稿"))
        self.assertLess(prompt.index("# 先判断任务范围与入口"), prompt.index("# 标准翻译流程"))
        self.assertIn("译名核查在这一步就做，不留到全量之后", prompt)
        self.assertIn("抽样精读（problems 之外必做）", prompt)
        self.assertNotIn("count > 500", prompt)


if __name__ == "__main__":
    unittest.main()
