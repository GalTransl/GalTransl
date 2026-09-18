import unittest
from types import SimpleNamespace

from GalTransl.Agent.runtime import AgentToolError, _dict_line_key, _tool_save_dict


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

    def test_condition_and_situation_lines_use_search_word(self) -> None:
        self.assertEqual(_dict_line_key("pre_src\t场景A[or]场景B\tアイテム\t道具"), "アイテム")
        self.assertEqual(_dict_line_key("mono\t独白词\t独白译"), "独白词")


class SaveDictActionTests(unittest.TestCase):
    def test_overwrite_is_default_and_replaces_whole_file(self) -> None:
        result, runner = _run(["a\tA"], "b\tB\nc\tC")
        self.assertEqual(result["action"], "overwrite")
        self.assertEqual(runner.saved[-1], "b\tB\nc\tC")
        self.assertEqual(result["line_count_after"], 2)

    def test_append_adds_new_entries_and_skips_duplicates(self) -> None:
        result, runner = _run(["a\tA"], "a\tA2\nb\tB", "append")
        # a 已存在 → 跳过并回报；只追加 b
        self.assertEqual(result["appended_keys"], ["b"])
        self.assertEqual(result["skipped_duplicate_keys"], ["a"])
        self.assertEqual(runner.saved[-1], "a\tA\nb\tB")

    def test_replace_updates_matching_key_only(self) -> None:
        result, runner = _run(["a\tA", "b\tB"], "b\tB2\nz\tZ", "replace")
        self.assertEqual(result["replaced_keys"], ["b"])
        self.assertEqual(result["not_found_keys"], ["z"])  # 不新增
        self.assertEqual(runner.saved[-1], "a\tA\nb\tB2")

    def test_no_effective_change_skips_write(self) -> None:
        result, runner = _run(["a\tA"], "a\tA", "append")
        self.assertIn("note", result)
        self.assertEqual(runner.saved, [])
        self.assertEqual(result["skipped_duplicate_keys"], ["a"])

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
