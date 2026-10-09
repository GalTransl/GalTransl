import unittest
from types import SimpleNamespace

from GalTransl.Agent.runtime import AgentToolError, _dict_line_key, _tool_save_dict, _preview_tool_changes
from GalTransl.Agent.tool_schemas import AGENT_TOOLS


class _Runner:
    """最小 runner：读项目字典，记录写回内容。"""

    def __init__(self, lines=None):
        self.state = SimpleNamespace(config_file_name="config.yaml")
        self.lines = list(lines or [])
        self.saved = []

    def _project_id(self):
        return "proj"

    def _http_get(self, _path):
        return {"dict_contents": {"(project_dir)项目GPT字典.txt": {"lines": list(self.lines), "count": len(self.lines)}}}

    def _http_post(self, _path, body):
        self.saved.append(body["content"])
        return {"success": True, "file_key": body["file_key"]}


def _run(lines, content, action=None):
    runner = _Runner(lines)
    args = {"file_key": "(project_dir)项目GPT字典.txt", "content": content}
    if action:
        args["action"] = action
    return _tool_save_dict(runner, args), runner


class DictLineKeyTests(unittest.TestCase):
    def test_normal_line_uses_first_column(self) -> None:
        self.assertEqual(_dict_line_key("アリス\t爱丽丝\t人名"), "アリス")

    def test_comment_and_blank_have_no_key(self) -> None:
        self.assertEqual(_dict_line_key("// 注释"), "")
        self.assertEqual(_dict_line_key("   "), "")
        self.assertEqual(_dict_line_key("\\\\注释"), "")

    def test_condition_and_situation_keys_include_scope(self) -> None:
        self.assertEqual(_dict_line_key("pre_src\t场景A[or]场景B\tアイテム\t道具"), "pre_src\t场景A[or]场景B\tアイテム")
        self.assertEqual(_dict_line_key("mono\t独白词\t独白译"), "mono\t独白词")


class SaveDictActionTests(unittest.TestCase):
    def test_explicit_overwrite_replaces_whole_file(self) -> None:
        result, runner = _run(["a\tA"], "b\tB\nc\tC", "overwrite")
        self.assertEqual(result["action"], "overwrite")
        self.assertEqual(runner.saved[-1], "b\tB\nc\tC")
        self.assertEqual(result["line_count_after"], 2)

    def test_patch_updates_translation_and_note_and_appends_new_entries(self) -> None:
        result, runner = _run(["// 注释", "a\tA\t旧备注", "keep\tK"], "a\tA2\t新备注\nb\tB", "patch")
        self.assertEqual(result["appended_keys"], ["b"])
        self.assertEqual(result["replaced_keys"], ["a"])
        self.assertNotIn("skipped_duplicate_keys", result)
        self.assertNotIn("not_found_keys", result)
        self.assertEqual(runner.saved[-1], "// 注释\na\tA2\t新备注\nkeep\tK\nb\tB")

    def test_patch_repeated_keys_use_last_value_without_duplicate_rows(self) -> None:
        result, runner = _run(["a\tA"], "a\tA2\nb\tB\na\tA3\nb\tB2", "patch")
        self.assertEqual(result["replaced_keys"], ["a"])
        self.assertEqual(result["appended_keys"], ["b"])
        self.assertEqual(runner.saved[-1], "a\tA3\nb\tB2")

    def test_patch_preserves_blank_lines_comments_and_special_dictionary_keys(self) -> None:
        result, runner = _run(
            ["// 标题", "", "mono\t独白词\t旧译", "pre_src\t条件\t词\t旧译"],
            "mono\t独白词\t新译\r\npre_src\t条件\t词\t新译\r\n// 新注释\r\n新词\t译名", "patch",
        )
        self.assertEqual(result["replaced_keys"], ["mono\t独白词", "pre_src\t条件\t词"])
        self.assertEqual(runner.saved[-1], "// 标题\n\nmono\t独白词\t新译\npre_src\t条件\t词\t新译\n// 新注释\n新词\t译名")

    def test_patch_preview_matches_mixed_updates_and_additions(self) -> None:
        runner = _Runner(["a\tA", "b\tB"])
        args = {"file_key": "(project_dir)项目GPT字典.txt", "action": "patch", "content": "b\tB2\nc\tC"}
        preview = _preview_tool_changes(runner, "save_dict", args)
        self.assertEqual(runner.saved, [])
        result = _tool_save_dict(runner, args)
        self.assertEqual(preview["line_diff"], result["line_diff"])

    def test_schema_only_exposes_unified_patch_action(self) -> None:
        schema = next(t["function"] for t in AGENT_TOOLS if t["function"]["name"] == "save_dict")
        self.assertEqual(schema["parameters"]["properties"]["action"]["enum"], ["overwrite", "patch", "delete"])
        for action in ("append", "replace"):
            with self.subTest(action=action), self.assertRaisesRegex(AgentToolError, "patch"):
                _run(["a\tA"], "a\tB", action)

    def test_no_effective_change_skips_write(self) -> None:
        result, runner = _run(["a\tA"], "a\tA", "patch")
        self.assertIn("note", result)
        self.assertEqual(runner.saved, [])
        self.assertEqual(result["replaced_keys"], [])
        self.assertEqual(result["appended_keys"], [])

    def test_delete_removes_lines_by_key(self) -> None:
        # 整行粘贴与只写 key 两种写法都要认
        result, runner = _run(["a\tA", "b\tB", "c\tC"], "b\tB\nc", "delete")
        self.assertEqual(result["deleted_keys"], ["b", "c"])
        self.assertEqual(result["lines_removed"], 2)
        self.assertEqual(runner.saved[-1], "a\tA")

    def test_delete_reports_not_found_keys(self) -> None:
        result, runner = _run(["a\tA"], "a\nzz", "delete")
        self.assertEqual(result["deleted_keys"], ["a"])
        self.assertEqual(result["not_found_keys"], ["zz"])

    def test_delete_all_entries_keeps_comments(self) -> None:
        result, runner = _run(["// 注释", "a\tA"], "a", "delete")
        self.assertEqual(result["deleted_keys"], ["a"])
        self.assertEqual(runner.saved[-1], "// 注释")

    def test_delete_without_content_raises(self) -> None:
        with self.assertRaises(AgentToolError):
            _run(["a\tA"], "", "delete")

    def test_bad_action_raises(self) -> None:
        with self.assertRaises(AgentToolError):
            _run(["a\tA"], "b\tB", "merge")


if __name__ == "__main__":
    unittest.main()
