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

    def test_replace_mode_lists_changed_paths_as_plain_lines(self):
        result = {"action": "replace", "files": [
            {"filename": "a.json", "updated": 2, "changes": _changes(2, "a.json#12.pre_dst")},
        ]}
        text = _render_tool_result_table("patch_transl_cache", result)
        # 纯文本行，没有表格外壳；小节标题已有文件名，path 前缀也去掉
        self.assertNotIn("|", text)
        self.assertIn("- #12.pre_dst0\n- #12.pre_dst1", text)
        self.assertNotIn("before", text)
        self.assertNotIn("after", text)
        # 值本身也不再回传
        self.assertNotIn("old0", text)
        self.assertNotIn("new0", text)

    def test_multifile_replace_reports_global_preview_omission_once(self):
        result = {"action": "replace", "files": [
            {"filename": f"chapter{i}.json", "updated": 8, "changes": _changes(8, f"chapter{i}.json#")}
            for i in range(3)
        ]}

        text = _render_tool_result_table("patch_transl_cache", result)

        self.assertEqual(len([line for line in text.splitlines() if line.startswith("- #")]), 10)
        self.assertEqual(text.count("后续修改为节省上下文，不展示 diff（仅省略预览）。"), 1)
        self.assertNotIn("变更预览共", text)
        self.assertNotIn("本次展示 0 行", text)

    def test_patch_mode_keeps_before_and_after(self):
        result = {"files": [
            {"filename": "a.json", "updated": 1, "changes": _changes(1, "a.json#7.pre_dst")},
        ]}
        text = _render_tool_result_table("patch_transl_cache", result)
        self.assertIn("| path | before | after |", text)
        for detail in ("old0", "new0"):
            self.assertIn(detail, text)

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

    def test_multifile_patch_omission_is_announced_once_and_keeps_late_diagnostics(self):
        result = {"files": [
            {"filename": f"chapter{i}.json", "updated": 4, "changes": _changes(4, f"chapter{i}#"),
             "problems": [{"index": i, "problem": "译名未统一"}]}
            for i in range(12)
        ]}
        result["files"][-1].update({
            "verification": "unknown", "audit_error": "记录失败",
            "skipped": [{"index": 99, "reason": "字段不可写"}],
            "not_found": [{"index": 100}], "error": "未保存文件",
        })
        original = copy.deepcopy(result)

        text = _render_tool_result_table("patch_transl_cache", result)

        self.assertEqual(len(_table_data(text)), 10)
        self.assertEqual(text.count("后续修改为节省上下文，不展示 diff"), 1)
        self.assertNotIn("本次展示 0 行", text)
        self.assertNotIn("变更预览共", text)
        self.assertNotIn("没列出来的就是", text)
        self.assertIn("共改动 48 条", text)
        self.assertEqual(text.count("译名未统一"), 12)
        for detail in ("chapter11.json", "未验证", "pending", "字段不可写", "100", "未保存文件"):
            self.assertIn(detail, text)
        self.assertEqual(result, original)  # 完整结果仍供前端变更卡和回放使用

    def test_multifile_patch_exact_budget_has_no_omission_notice(self):
        result = {"files": [
            {"filename": "a.json", "updated": 10, "changes": _changes(10)},
            {"filename": "b.json", "updated": 0, "changes": [], "error": "文件不存在"},
        ]}
        text = _render_tool_result_table("patch_transl_cache", result)
        self.assertEqual(len(_table_data(text)), 10)
        self.assertNotIn("不展示 diff", text)
        self.assertIn("文件不存在", text)
        result["files"].append({"filename": "c.json", "updated": 1, "changes": _changes(1, "hidden")})
        text = _render_tool_result_table("patch_transl_cache", result)
        self.assertEqual(text.count("不展示 diff"), 1)
        self.assertNotIn("hidden", text)

    def test_multifile_replace_skips_files_without_any_change(self):
        # replace 模式会把项目里全部缓存文件都走一遍，没改动的那些不该逐份列节。
        result = {"action": "replace", "files": [
            {"filename": "a.json", "updated": 3, "changes": _changes(3, "a")},
            {"filename": "b.json", "updated": 0, "changes": [], "note": "内容没有变化，未保存文件"},
            {"filename": "c.json", "updated": 0, "changes": [], "note": "内容没有变化，未保存文件"},
            {"filename": "d.json", "updated": 0, "changes": [], "error": "文件不存在"},
            {"filename": "e.json", "updated": 0, "changes": [],
             "not_found": [{"index": 7}], "error": "没有条目被更新（skipped=0, not_found=1）"},
        ]}
        text = _render_tool_result_table("patch_transl_cache", result)
        self.assertIn("共改动 3 条，涉及 1 个缓存文件", text)
        self.assertIn("其余 2 个文件没有内容变化", text)
        # 失败的文件仍然逐份交代，且编号只数真正成节的
        self.assertIn("## 1. a.json（改 3 条）", text)
        self.assertIn("## 2. d.json（改 0 条）", text)
        self.assertIn("## 3. e.json（改 0 条）", text)
        for detail in ("文件不存在", "没找到这些 index：7"):
            self.assertIn(detail, text)
        self.assertNotIn("b.json", text)
        self.assertNotIn("c.json", text)

    def test_revert_shares_the_patch_rendering_and_its_budget(self):
        # 撤销的返回体和 patch 同构：必须有渲染器，且 before/after 受同一个 10 行上限约束。
        result = {
            "updated": 15,
            "files": [{"filename": "a.json", "updated": 15, "changes": _changes(15, "a.json#")}],
            "changes": _changes(15, "a.json#"),
        }
        text = _render_tool_result_table("revert_proofread_changes", result)
        self.assertIsNotNone(text)
        self.assertIn("## 1. a.json（改 15 条）", text)
        self.assertIn("| path | before | after |", text)
        self.assertEqual(len(_table_data(text)), 10)
        self.assertIn("不展示 diff", text)
        # 小节标题已经写了文件名，表里不再重复 `文件名#` 前缀
        self.assertIn("| #0 | old0 | new0 |", text)
        self.assertNotIn("a.json#0", text)

    def test_white_list_lists_entries_as_plain_lines(self):
        result = {"white_list": ["01.json:12", "01.json:13"], "count": 2}
        text = _render_tool_result_table("manage_problem_white_list", result)
        self.assertIn("共 2 条白名单", text)
        self.assertIn("- 01.json:12\n- 01.json:13", text)
        self.assertNotIn("|", text)

    def test_white_list_is_bounded_for_the_model(self):
        result = {"white_list": [f"f.json:{i}" for i in range(150)], "count": 150}
        text = _render_tool_result_table("manage_problem_white_list", result)
        self.assertEqual(len([line for line in text.splitlines() if line.startswith("- ")]), 100)
        self.assertIn("其余 50 条未展开", text)

    def test_read_page_note_does_not_repeat_the_entry_count(self):
        base = {
            "filename": "a.json", "count": 3, "matched": 3, "returned": 3, "offset": 0,
            "entries": [{"index": 1, "name": "", "pre_src": "s1"}], "has_more": False,
        }
        text = _render_tool_result_table("read_input_file", base)
        self.assertIn("共 3 条，显示 3 条", text)
        self.assertNotIn("筛选后共", text)
        self.assertNotIn("offset=", text)
        # 真的筛过 / 真的翻页了才说
        filtered = {**base, "matched": 1, "returned": 1, "offset": 2}
        text = _render_tool_result_table("read_input_file", filtered)
        self.assertIn("筛选后共 1 条", text)
        self.assertIn("本页 offset=2", text)

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

    def test_noop_dictionary_patch_is_reported(self):
        result, runner = save_dict(["a\tA"], "a\tA", "set")
        text = _render_tool_result_table("save_dict", result)
        self.assertEqual(runner.saved, [])
        self.assertIn("内容没有变化", text)
        self.assertIn("替换词条：0", text)


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
                    return "", [{"id": "c1", "name": "save_dict", "arguments": "{}"}], "tool_calls"
                received.append(next(m["content"] for m in runner.state.messages if m["role"] == "tool"))
                return "done", [], "stop"

            with patch.object(runner, "_resolve_llm"), patch.object(runner, "_stream_llm_response", side_effect=response), patch.object(runner, "_dispatch_tool", return_value=result):
                runner.run()
            self.assertEqual(len(received), 2)
            self.assertEqual(len(_table_data(received[1])), 10)
            event = next(e for e in runner.state.events if e.type == "tool_result")
            self.assertEqual(event.data["result"], result)
            self.assertEqual(event.data["result_markdown"], _render_tool_result_table("save_dict", result))
            loaded = runner._store.load()
            self.assertEqual(next(e for e in loaded["events"] if e["type"] == "tool_result")["result"], result)
            self.assertEqual(
                next(e for e in loaded["events"] if e["type"] == "tool_result")["result_markdown"],
                _render_tool_result_table("save_dict", result),
            )
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
