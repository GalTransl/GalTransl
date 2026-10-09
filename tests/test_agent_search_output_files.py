import unittest

from GalTransl.Agent import runtime as rt
from GalTransl.Agent.tools.search import _tool_search_output_files
from tests.test_agent_search_input import _SearchRunner


def _row(**kw):
    return {"filename": "chapter/a.json", "index": 1, "speaker": "", "dst": "最终译文",
            "match_dst": False, "match_name": False, **kw}


class SearchOutputFilesTests(unittest.TestCase):
    def test_request_forwards_scope_paging_and_context_budget(self):
        runner = _SearchRunner({"results": [], "total": 0})
        _tool_search_output_files(runner, {"query": "译名", "field": "dst", "filename": "chapter/a.json",
                                          "context": 3, "offset": 2, "limit": 100, "order": "reverse"})
        url, body = runner.posts[0]
        self.assertEqual(url, "/api/projects/proj/output/search")
        self.assertEqual(body, {"query": "译名", "field": "dst", "filename": "chapter/a.json",
                                "context": 3, "offset": 2, "max_results": 50, "order": "reverse",
                                "preceding_only": True, "options": {"re": False}, "config_file_name": "config.yaml"})
        self.assertEqual(runner.gets, [])

    def test_two_sided_context_reduces_hit_budget(self):
        runner = _SearchRunner()
        _tool_search_output_files(runner, {"query": "x", "context": 3, "only_preceding": False})
        self.assertEqual(runner.posts[0][1]["max_results"], 28)
        self.assertNotIn("preceding_only", runner.posts[0][1])

    def test_result_marks_context_and_renders_delivered_text(self):
        runner = _SearchRunner({"results": [_row(), _row(index=2, match_dst=True),
                                             _row(index=3, speaker="译名", match_name=True)],
                                "total": 10, "returned_hits": 2, "returned": 3})
        result = _tool_search_output_files(runner, {"query": "译名", "context": 1, "offset": 4})
        self.assertEqual(result["source"], "output")
        self.assertEqual(result["returned"], 2)
        self.assertEqual(result["returned_rows"], 3)
        self.assertEqual(result["matched_in"], {"dst": 1, "name": 1})
        self.assertEqual([row["index"] for row in result["results"]], ["1*", 2, 3])
        self.assertTrue(result["has_more"])
        text = rt._render_tool_result_table("search_output_files", result)
        self.assertIn("| filename | index | speaker | dst |", text)
        self.assertIn("最终译文", text)
        self.assertIn("本页 2 条命中", text)
        self.assertNotIn("match_dst", text)

    def test_failed_output_files_are_not_reported_as_no_matches(self):
        runner = _SearchRunner({"results": [], "total": 0, "files_failed": ["broken.ks"]})
        result = _tool_search_output_files(runner, {"query": "x"})
        text = rt._render_tool_result_table("search_output_files", result)
        self.assertIn("输出文件解析失败", text)
        self.assertIn("read_output", text)
        self.assertIn("未知", text)
        self.assertNotIn("输入文件解析失败", text)

    def test_invalid_arguments_fail_before_http(self):
        for args in ({"query": ""}, {"query": "x", "field": "src"},
                     {"query": "x", "context": "bad"}, {"query": "x", "order": "bad"}):
            with self.subTest(args=args):
                runner = _SearchRunner()
                with self.assertRaises(rt.AgentToolError):
                    _tool_search_output_files(runner, args)
                self.assertEqual(runner.posts, [])

    def test_registration_permissions_and_schema_match_input_search(self):
        self.assertIs(rt._TOOL_HANDLERS["search_output_files"], _tool_search_output_files)
        self.assertEqual(rt._tool_risk("search_output_files"), rt.PERMISSION_READ)
        schemas = {t["function"]["name"]: t["function"]["parameters"] for t in rt.AGENT_TOOLS}
        schema = schemas["search_output_files"]
        self.assertEqual(schema["required"], ["query"])
        self.assertEqual(set(schema["properties"]), set(schemas["search_input"]["properties"]))
        self.assertEqual(schema["properties"]["field"]["enum"], ["all", "dst", "name"])


if __name__ == "__main__":
    unittest.main()
