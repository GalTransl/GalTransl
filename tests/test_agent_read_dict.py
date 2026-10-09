import unittest

from GalTransl.Agent.runtime import AgentToolError, _render_tool_result_table, _tool_read_dict
from GalTransl.Agent.tool_schemas import AGENT_TOOLS
from tests.test_agent_save_dict import _Runner


def _read(lines, **args):
    return _tool_read_dict(_Runner(lines), {"file_key": "(project_dir)项目GPT字典.txt", **args})


class ReadDictTests(unittest.TestCase):
    def test_default_page_bounds_large_dictionary_and_reports_next_offset(self):
        lines = [f"key{i}\t译名{i}" for i in range(10001)]
        result = _read(lines)
        self.assertEqual(result["lines"], lines[:100])
        self.assertEqual(result["line_numbers"], list(range(1, 101)))
        self.assertEqual(result["count"], 10001)
        self.assertEqual(result["matched"], 10001)
        self.assertEqual(result["returned"], 100)
        self.assertTrue(result["has_more"])
        self.assertEqual(result["next_offset"], 100)
        last = _read(lines, offset=10000, limit=500)
        self.assertEqual(last["lines"], lines[-1:])
        self.assertFalse(last["has_more"])
        self.assertNotIn("next_offset", last)

    def test_searches_source_translation_notes_and_comments_before_paging(self):
        lines = ["Alice\t爱丽丝", "无关\t词", "人名\tALICE", "别名\t译名\tAlice的昵称", "// alice"]
        result = _read(lines, query="alice", offset=1, limit=2)
        self.assertEqual(result["lines"], [lines[2], lines[3]])
        self.assertEqual(result["line_numbers"], [3, 4])
        self.assertEqual(result["matched"], 4)
        self.assertEqual(result["count"], 5)
        self.assertEqual(result["next_offset"], 3)
        last = _read(lines, query="alice", offset=result["next_offset"], limit=2)
        self.assertEqual(last["lines"], [lines[4]])
        self.assertFalse(last["has_more"])

    def test_search_is_literal_and_preserves_whitespace(self):
        lines = ["a.*\tA", "abc\tB", "// 标题", "", "   ", "````"]
        self.assertEqual(_read(lines, query=".*")["lines"], [lines[0]])
        self.assertEqual(_read(lines, query="   ")["lines"], [lines[4]])
        result = _read(lines)
        self.assertEqual(result["lines"], lines)
        text = _render_tool_result_table("read_dict", result)
        self.assertIn("\n".join(lines), text)
        self.assertIn("`````text", text)

    def test_empty_dictionary_no_match_and_past_end_are_distinguished(self):
        for lines, args, expected in (
            ([], {}, "字典为空"),
            (["a\tA"], {"query": "missing"}, "本页没有匹配行"),
            (["a\tA"], {"offset": 20}, "本页没有匹配行"),
        ):
            with self.subTest(args=args):
                result = _read(lines, **args)
                self.assertEqual(result["returned"], 0)
                self.assertEqual(result["lines"], [])
                self.assertFalse(result["has_more"])
                self.assertIn(expected, _render_tool_result_table("read_dict", result))

    def test_markdown_shows_search_counts_original_lines_and_continuation(self):
        result = _read(["a\tA", "b\tB", "aa\tAA"], query="a", limit=1)
        text = _render_tool_result_table("read_dict", result)
        for expected in ("共 3 行", "匹配 2 行", "本页 1 行", "原文件行号", "offset=1", 'query="a"'):
            self.assertIn(expected, text)
        self.assertNotIn("aa\tAA", text)

    def test_invalid_arguments_raise(self):
        for args in ({"query": None}, {"query": []}, {"offset": -1}, {"offset": True},
                     {"offset": 1.5}, {"offset": "1"}, {"limit": 0}, {"limit": 501},
                     {"limit": True}, {"limit": 1.5}, {"file_key": ""}, {"file_key": "missing"}):
            with self.subTest(args=args), self.assertRaises(AgentToolError):
                _read(["a\tA"], **args)

    def test_schema_exposes_query_and_bounded_paging(self):
        schema = next(t["function"] for t in AGENT_TOOLS if t["function"]["name"] == "read_dict")
        props = schema["parameters"]["properties"]
        self.assertEqual(props["query"]["type"], "string")
        self.assertEqual(props["offset"]["default"], 0)
        self.assertEqual(props["limit"]["default"], 100)
        self.assertEqual(props["limit"]["maximum"], 500)


if __name__ == "__main__":
    unittest.main()
