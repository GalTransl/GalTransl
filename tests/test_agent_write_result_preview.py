"""写入工具：模型仅收有限 Markdown diff，实际写入和前端回放不受截断影响。"""

import copy
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from GalTransl.Agent import session_store as ss
from GalTransl.Agent.models import AgentState
from GalTransl.Agent.runner import AgentRunner
from GalTransl.Agent.subagent import SubAgentRunner
from GalTransl.Agent.tools.common import _diff_lines
from GalTransl.Agent.tools.render_md import _render_tool_result_table
from tests.test_agent_save_dict import _run as save_dict


def _changes(count, prefix="name"):
    return [{"path": f"{prefix}{i}", "before": f"old{i}", "after": f"new{i}", "kind": "replace"} for i in range(count)]


def _table_data(text):
    return [line for line in text.splitlines() if line.startswith("| ") and not line.startswith(("| path |", "| index |", "| ---"))]


class WritePreviewTests(unittest.TestCase):
    def test_large_dictionary_write_is_complete_but_model_sees_ten_lines(self):
        content = "\n".join(f"word{i}\t译名{i}" for i in range(350))
        result, runner = save_dict([], content)
        original = copy.deepcopy(result)
        text = _render_tool_result_table("save_dict", result)
        self.assertEqual(runner.saved, [content])
        self.assertEqual(result["lines_added"], 350)
        self.assertEqual(result["lines_removed"], 0)
        self.assertEqual(len(result["line_diff"]["rows"]), 200)
        self.assertEqual(len(text.split("```diff\n")[1].split("\n```")[0].splitlines()), 10)
        self.assertIn("新增行数：350", text)
        self.assertIn("共 350 行", text)
        self.assertIn("省略", text)
        self.assertNotIn("word10\t", text)
        self.assertEqual(result, original)

    def test_field_changes_share_limit_and_do_not_echo_success_lists(self):
        for name in ("save_name_table", "update_project_config", "manage_problem_filter", "manage_problem_white_list"):
            with self.subTest(tool=name):
                result = {"updated": 25, "count": 25, "changes": _changes(25), "names_added": [f"hidden{i}" for i in range(25)], "applied": [{"value": "hidden payload"}]}
                text = _render_tool_result_table(name, result)
                self.assertEqual(len(_table_data(text)), 10)
                self.assertIn("共 25 项字段变更", text)
                self.assertIn("省略", text)
                self.assertNotIn("name10", text)
                self.assertNotIn("hidden", text)

    def test_exact_limit_has_no_false_omission_and_zero_changes_stays_markdown(self):
        for count in (0, 1, 10, 11):
            with self.subTest(count=count):
                text = _render_tool_result_table("save_name_table", {"success": True, "changes": _changes(count)})
                self.assertEqual(len(_table_data(text)), min(count, 10))
                self.assertEqual("省略" in text, count > 10)
                self.assertIn("### 写入结果", text)

    def test_guideline_totals_count_both_sides_beyond_ui_limit(self):
        diff = _diff_lines("\n".join(f"old{i}" for i in range(250)), "\n".join(f"new{i}" for i in range(300)))
        self.assertEqual((diff["added"], diff["removed"]), (300, 250))
        text = _render_tool_result_table("write_project_guideline", {"filename": "translation_guideline.md", "line_diff": diff, "lines_added": diff["added"], "lines_removed": diff["removed"]})
        self.assertIn("共 550 行", text)
        self.assertIn("删除行数：250", text)
        self.assertNotIn("-old10\n", text)

    def test_multifile_patch_uses_one_budget_and_preserves_late_errors(self):
        result = {"files": [
            {"filename": "a.json", "updated": 8, "changes": _changes(8, "a")},
            {"filename": "b.json", "updated": 8, "changes": _changes(8, "b"), "skipped": [{"index": 99, "reason": "字段不可写"}], "not_found": [{"index": 100}], "problems": [{"index": 5, "problem": "仍有残留"}]},
            {"filename": "c.json", "updated": 0, "error": "文件不存在", "changes": []},
        ]}
        text = _render_tool_result_table("patch_transl_cache", result)
        self.assertEqual(len(_table_data(text)), 10)
        self.assertIn("共改动 16 条", text)
        for detail in ("字段不可写", "100", "仍有残留", "文件不存在"):
            self.assertIn(detail, text)
        self.assertNotIn("| b2 |", text)

    def test_delete_preview_is_limited_but_missing_indexes_are_kept(self):
        result = {"deleted_indexes": list(range(80)), "deleted_preview": [{"index": i, "text": f"line{i}"} for i in range(50)], "missing_indexes": [999]}
        text = _render_tool_result_table("delete_transl_cache", result)
        self.assertEqual(len(_table_data(text)), 10)
        self.assertIn("删除条目：80", text)
        self.assertIn("共 80 行", text)
        self.assertIn("999", text)
        self.assertNotIn("line10", text)

    def test_long_multiline_values_cannot_bypass_limit(self):
        text = _render_tool_result_table("save_name_table", {"changes": [{"path": "a|b", "before": "old\nvalue", "after": "很长\n" * 1000}]})
        self.assertEqual(len(_table_data(text)), 1)
        self.assertIn("a\\|b", text)
        self.assertIn("old<br>value", text)
        self.assertIn("内容已截断", text)
        self.assertLess(len(text), 2000)

    def test_empty_name_additions_and_removals_keep_operation_kind(self):
        text = _render_tool_result_table("save_name_table", {"changes": [
            {"path": "新角色", "before": None, "after": None, "kind": "add"},
            {"path": "旧角色", "before": None, "after": None, "kind": "remove"},
        ]})
        self.assertIn("| 新角色 |  |  | add |", text)
        self.assertIn("| 旧角色 |  |  | remove |", text)

    def test_noop_and_skipped_dictionary_entries_are_reported(self):
        result, runner = save_dict(["a\tA"], "a\tB", "append")
        text = _render_tool_result_table("save_dict", result)
        self.assertEqual(runner.saved, [])
        self.assertIn("内容没有变化", text)
        self.assertIn("跳过的重复词条：a", text)


class WritePreviewPipelineTests(unittest.TestCase):
    def test_main_agent_sends_markdown_but_keeps_full_ui_result_and_replay(self):
        result = {"total": 25, "changes": _changes(25)}
        with tempfile.TemporaryDirectory() as root, patch.object(ss, "SESSIONS_ROOT", root):
            project = os.path.join(root, "project")
            os.makedirs(project)
            sid = ss.create_session(project, "test")
            runner = AgentRunner(AgentState())
            runner.state.project_dir = project
            runner.state.session_id = sid
            runner.state.messages = [{"role": "user", "content": "save"}]
            runner._store = ss.SessionStore(project, sid)
            received = []

            def response():
                if not received:
                    received.append(None)
                    return "", [{"id": "c1", "name": "save_name_table", "arguments": "{}"}], "tool_calls"
                received.append(next(m["content"] for m in runner.state.messages if m["role"] == "tool"))
                return "done", [], "stop"

            with patch.object(runner, "_resolve_llm"), patch.object(runner, "_stream_llm_response", side_effect=response), patch.object(runner, "_dispatch_tool", return_value=result):
                runner.run()
            self.assertEqual(len(received), 2)
            self.assertEqual(len(_table_data(received[1])), 10)
            event = next(e for e in runner.state.events if e.type == "tool_result")
            self.assertEqual(event.data["result"], result)
            loaded = runner._store.load()
            self.assertEqual(next(e for e in loaded["events"] if e["type"] == "tool_result")["result"], result)
            self.assertEqual(next(m for m in loaded["messages"] if m["role"] == "tool")["content"], received[1])
            replay = ss.read_transcript(project, sid)
            self.assertEqual(next(e for e in replay if e["type"] == "tool_result")["result"], result)

    def test_subagent_uses_same_global_diff_limit(self):
        parent = AgentRunner(AgentState())
        sub = SubAgentRunner(parent, agent="proofread", files=["a.json"], indexes="", brief="", delegation_id="test")
        call = SimpleNamespace(id="c1", function=SimpleNamespace(name="patch_transl_cache", arguments="{}"))
        result = {"files": [{"filename": "a.json", "updated": 25, "changes": _changes(25)}]}
        message = sub._run_tool(call, {"patch_transl_cache": lambda *_: result})
        self.assertEqual(len(_table_data(message["content"])), 10)
        self.assertIn("省略", message["content"])


if __name__ == "__main__":
    unittest.main()
