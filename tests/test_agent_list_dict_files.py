import unittest
from types import SimpleNamespace

from GalTransl.Agent.runtime import _tool_list_dict_files


class _Runner:
    """最小 runner：/dictionary/project 返回带 dict_contents 的载荷。"""

    def __init__(self):
        self.state = SimpleNamespace(config_file_name="config.yaml")

    def _project_id(self):
        return "proj"

    def _http_get(self, _path):
        return {
            "pre_dict_files": ["(project_dir)项目字典_译前.txt"],
            "gpt_dict_files": ["(project_dir)项目GPT字典.txt"],
            "post_dict_files": ["(project_dir)项目字典_译后.txt"],
            "dict_contents": {
                "(project_dir)项目字典_译前.txt": {"lines": ["a\tA"], "count": 1},
                "(project_dir)项目GPT字典.txt": {"lines": ["b\tB", "c\tC"], "count": 2},
                "(project_dir)项目字典_译后.txt": {"lines": [], "count": 0},
            },
        }


class ListDictFilesTests(unittest.TestCase):
    """list_dict_files 只列清单，不回传字典全文（全文用 read_dict 读）。"""

    def test_lists_files_and_line_counts_without_contents(self) -> None:
        result = _tool_list_dict_files(_Runner(), {})

        self.assertNotIn("contents", result)
        self.assertEqual(result["pre_dict_files"], ["(project_dir)项目字典_译前.txt"])
        self.assertEqual(result["gpt_dict_files"], ["(project_dir)项目GPT字典.txt"])
        self.assertEqual(result["post_dict_files"], ["(project_dir)项目字典_译后.txt"])
        self.assertEqual(
            result["line_counts"],
            {
                "(project_dir)项目字典_译前.txt": 1,
                "(project_dir)项目GPT字典.txt": 2,
                "(project_dir)项目字典_译后.txt": 0,
            },
        )

    def test_no_dictionary_lines_leak_into_result(self) -> None:
        result = _tool_list_dict_files(_Runner(), {})
        dumped = str(result)
        self.assertNotIn("a\\tA", dumped)
        self.assertNotIn("b\\tB", dumped)


if __name__ == "__main__":
    unittest.main()
