"""翻译 Agent 读写通用字典：可读可改、不能新建；写入除「全自动-零打断」外都要确认。"""

import os
import tempfile
import unittest
from types import SimpleNamespace

from GalTransl.Agent.models import AgentToolError
from GalTransl.Agent.permissions import (
    AUTO_QUIET_MODE,
    PERMISSION_COMMON_DICT,
    PERMISSION_EDIT,
    PERMISSION_PER_CALL_RISKS,
    _permission_needed,
    _tool_risk,
)
from GalTransl.Agent.tools.dicts import _tool_list_dict_files, _tool_read_dict, _tool_save_dict
from GalTransl.Agent.tools.preview import _preview_tool_changes
from GalTransl.Agent.tools.render_md import _render_tool_result_table

COMMON = "00通用字典_译前.txt"
PROJECT = "(project_dir)项目GPT字典.txt"


class _Runner:
    def __init__(self):
        self.state = SimpleNamespace(config_file_name="config.yaml")
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict]] = []

    def _project_id(self):
        return "proj"

    def _http_get(self, path):
        self.gets.append(path)
        return {
            "pre_dict_files": [COMMON],
            "gpt_dict_files": [PROJECT],
            "post_dict_files": [],
            "common_dict_files": [COMMON],
            "dict_contents": {
                COMMON: {"lines": ["a\tA"], "count": 1},
                PROJECT: {"lines": ["b\tB"], "count": 1},
            },
        }

    def _http_post(self, path, body):
        self.posts.append((path, body))
        return {"success": True, "file_key": body.get("file_key")}


class CommonDictPermissionTests(unittest.TestCase):
    def test_common_dict_write_has_its_own_risk(self):
        self.assertEqual(_tool_risk("save_dict", {"file_key": COMMON, "action": "set"}), PERMISSION_COMMON_DICT)
        self.assertEqual(_tool_risk("save_dict", {"file_key": PROJECT, "action": "set"}), PERMISSION_EDIT)
        # 带 category 的裸文件名是「新建项目字典」，不是通用字典
        self.assertEqual(_tool_risk("save_dict", {"file_key": "新.txt", "category": "gpt"}), PERMISSION_EDIT)

    def test_only_auto_quiet_skips_confirmation(self):
        for mode in ("ask", "accept-edits", "auto"):
            self.assertTrue(_permission_needed(PERMISSION_COMMON_DICT, mode), mode)
        self.assertFalse(_permission_needed(PERMISSION_COMMON_DICT, AUTO_QUIET_MODE))
        # 「本会话允许」不适用：否则在全自动下批过一次就再也不问了
        self.assertIn(PERMISSION_COMMON_DICT, PERMISSION_PER_CALL_RISKS)


class CommonDictToolTests(unittest.TestCase):
    def test_list_and_read_include_common_dictionaries(self):
        runner = _Runner()
        listed = _tool_list_dict_files(runner, {})
        self.assertIn("include_common=1", runner.gets[0])
        self.assertEqual(listed["pre_dict_files"], [COMMON])
        self.assertEqual(listed["common_dict_files"], [COMMON])
        self.assertIn("通用字典", _render_tool_result_table("list_dict_files", listed))
        self.assertEqual(_tool_read_dict(runner, {"file_key": COMMON})["lines"], ["a\tA"])

    def test_write_goes_to_the_common_endpoint(self):
        runner = _Runner()
        result = _tool_save_dict(runner, {"file_key": COMMON, "action": "set", "content": "c\tC"})
        self.assertEqual([path for path, _ in runner.posts], ["/api/projects/proj/dictionary/common/save"])
        self.assertEqual(runner.posts[0][1]["content"], "a\tA\nc\tC")
        self.assertEqual(result["file_key"], COMMON)

    def test_project_write_still_uses_the_project_endpoint(self):
        runner = _Runner()
        _tool_save_dict(runner, {"file_key": PROJECT, "action": "set", "content": "c\tC"})
        self.assertEqual([path for path, _ in runner.posts], ["/api/projects/proj/dictionary/project/save"])

    def test_category_never_targets_a_common_dictionary(self):
        """带 category 时权限按「改项目字典」算，所以绝不能落到通用字典上。"""
        runner = _Runner()
        with self.assertRaisesRegex(AgentToolError, "通用字典"):
            _tool_save_dict(runner, {"file_key": COMMON, "category": "pre", "action": "set", "content": "c\tC"})
        self.assertEqual(runner.posts, [])

    def test_unknown_common_dictionary_cannot_be_created(self):
        runner = _Runner()
        with self.assertRaisesRegex(AgentToolError, "不存在"):
            _tool_save_dict(runner, {"file_key": "不存在.txt", "action": "set", "content": "c\tC"})
        self.assertEqual(runner.posts, [])

    def test_approval_card_previews_the_common_dictionary_diff(self):
        runner = _Runner()
        preview = _preview_tool_changes(runner, "save_dict", {"file_key": COMMON, "action": "set", "content": "c\tC"})
        self.assertEqual(preview["file_key"], COMMON)
        self.assertIn("line_diff", preview)
        self.assertEqual(runner.posts, [])


class CommonDictServerPayloadTests(unittest.TestCase):
    def test_payload_lists_enabled_common_dictionaries_only_when_asked(self):
        from GalTransl.server import _collect_project_dict_payload, _resolve_common_dict_path

        with tempfile.TemporaryDirectory() as root:
            project = os.path.join(root, "proj")
            dict_dir = os.path.join(root, "dicts")
            os.makedirs(project)
            os.makedirs(dict_dir)
            with open(os.path.join(dict_dir, "common.txt"), "w", encoding="utf-8") as f:
                f.write("a\tA\n")
            with open(os.path.join(project, "p.txt"), "w", encoding="utf-8") as f:
                f.write("b\tB\n")
            with open(os.path.join(project, "config.yaml"), "w", encoding="utf-8") as f:
                f.write(
                    "dictionary:\n"
                    f"  defaultDictFolder: '{dict_dir}'\n"
                    "  preDict: ['common.txt']\n"
                    "  gpt.dict: ['(project_dir)p.txt']\n"
                    "  postDict: []\n"
                )

            plain = _collect_project_dict_payload(project, "config.yaml")
            self.assertEqual(plain["pre_dict_files"], [])
            self.assertNotIn("common.txt", plain["dict_contents"])

            full = _collect_project_dict_payload(project, "config.yaml", include_common=True)
            self.assertEqual(full["pre_dict_files"], ["common.txt"])
            self.assertEqual(full["common_dict_files"], ["common.txt"])
            self.assertEqual(full["dict_contents"]["common.txt"]["lines"], ["a\tA"])
            self.assertEqual(full["dict_contents"]["(project_dir)p.txt"]["lines"], ["b\tB"])

            self.assertEqual(
                os.path.normcase(_resolve_common_dict_path(dict_dir, project, "common.txt")),
                os.path.normcase(os.path.realpath(os.path.join(dict_dir, "common.txt"))),
            )
            for bad in ("../x.txt", "sub/x.txt", "(project_dir)p.txt", ""):
                with self.assertRaises(ValueError):
                    _resolve_common_dict_path(dict_dir, project, bad)


if __name__ == "__main__":
    unittest.main()
