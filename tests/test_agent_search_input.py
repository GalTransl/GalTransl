"""search_input 工具：在待翻译原文里搜关键词/说话人（search_transl_cache 的原文侧对应）。

与 search_transl_cache 共用一套用法与精简规则（context 收紧上限、逐行命中标记收成顶层
matched_in、in_context 只留 true），所以这里钉的是"发出去的请求对不对、回来的东西有没有
被按同一套规则收拾过"，以及参数校验与 0 命中的兜底提示。
"""

import unittest
from types import SimpleNamespace

from GalTransl.Agent import runtime as rt
from GalTransl.Agent.runtime import AgentToolError, _tool_search_input


class _SearchRunner:
    """只记录请求、回一份预置结果的替身。"""

    def __init__(self, response=None, input_files=None):
        self.state = SimpleNamespace(config_file_name="config.yaml", project_dir=r"C:\proj")
        self.response = response if response is not None else {"results": [], "total": 0}
        self.input_files = input_files if input_files is not None else [{"name": "a.json", "is_file": True}]
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[str] = []

    def _project_id(self) -> str:
        return "proj"

    def _http_post(self, url: str, body: dict):
        self.posts.append((url, body))
        return dict(self.response) if isinstance(self.response, dict) else self.response

    def _http_get(self, url: str):
        self.gets.append(url)
        return {"input_files": self.input_files}


def _row(**kw):
    row = {"filename": "a.json", "index": 1, "speaker": "", "src": "JP", "match_src": False, "match_name": False}
    row.update(kw)
    return row


class SearchInputRequestTests(unittest.TestCase):
    """发出去的请求：地址、入参、命中上限。"""

    def test_posts_to_the_input_search_endpoint(self):
        runner = _SearchRunner()

        _tool_search_input(runner, {"query": "ドルード", "field": "src"})

        url, body = runner.posts[0]
        self.assertEqual(url, "/api/projects/proj/input/search")
        self.assertEqual(body["query"], "ドルード")
        self.assertEqual(body["field"], "src")
        self.assertFalse(body["options"]["re"])  # 关键词搜索：当前只做子串匹配
        self.assertEqual(body["config_file_name"], "config.yaml")
        # 没给 context / filename 就不发这两个键（服务端按默认处理）
        self.assertNotIn("context", body)
        self.assertNotIn("filename", body)

    def test_context_and_filename_are_forwarded(self):
        runner = _SearchRunner()

        _tool_search_input(runner, {"query": "アリス", "context": 2, "filename": "a.json"})

        _, body = runner.posts[0]
        self.assertEqual(body["context"], 2)
        self.assertEqual(body["filename"], "a.json")

    def test_hit_cap_tightens_with_context(self):
        """命中 × (2N+1) 行一起返回：带上下文时收紧上限，没带时给 100 条。"""
        for context, expected in ((0, 100), (1, 100), (3, 42), (20, 7)):
            runner = _SearchRunner()
            args = {"query": "x"}
            if context:
                args["context"] = context
            _tool_search_input(runner, args)
            self.assertEqual(runner.posts[0][1]["max_results"], expected, context)

    def test_default_field_is_all(self):
        runner = _SearchRunner()
        _tool_search_input(runner, {"query": "x"})
        self.assertEqual(runner.posts[0][1]["field"], "all")


class SearchInputArgumentTests(unittest.TestCase):
    def test_query_is_required(self):
        with self.assertRaises(AgentToolError):
            _tool_search_input(_SearchRunner(), {"query": "   "})

    def test_field_must_be_known(self):
        with self.assertRaises(AgentToolError) as ctx:
            _tool_search_input(_SearchRunner(), {"query": "x", "field": "dst"})  # 原文侧没有译文列
        self.assertIn("all, src, name", str(ctx.exception))

    def test_context_must_be_an_integer_in_range(self):
        with self.assertRaises(AgentToolError):
            _tool_search_input(_SearchRunner(), {"query": "x", "context": "abc"})

    def test_context_is_clamped(self):
        runner = _SearchRunner()
        _tool_search_input(runner, {"query": "x", "context": 999})
        self.assertEqual(runner.posts[0][1]["context"], 20)


class SearchInputSlimmingTests(unittest.TestCase):
    """返回体按与 search_transl_cache 同一套规则收拾：命中标记收顶层、in_context 删掉。"""

    def test_match_flags_collapse_into_matched_in(self):
        runner = _SearchRunner(
            response={
                "results": [
                    _row(index=3, src="ドルードだ", match_src=True),
                    _row(index=4, speaker="ドルード", match_name=True),
                ],
                "total": 2,
            }
        )

        result = _tool_search_input(runner, {"query": "ドルード"})

        self.assertEqual(result["matched_in"], {"src": 1, "name": 1})
        for row in result["results"]:
            self.assertNotIn("match_src", row)
            self.assertNotIn("match_name", row)
        self.assertEqual(result["results"][0]["src"], "ドルードだ")

    def test_matched_in_is_not_added_for_a_single_field_search(self):
        """指定 field 的搜索本来就只有那一侧会命中，汇总没有信息量。"""
        runner = _SearchRunner(
            response={"results": [_row(match_src=True)], "total": 1}
        )

        result = _tool_search_input(runner, {"query": "x", "field": "src"})

        self.assertNotIn("matched_in", result)

    def test_in_context_is_stripped(self):
        """上下文行不做标注：服务端即便还发 in_context 也一律删掉。"""
        runner = _SearchRunner(
            response={
                "results": [
                    _row(index=3, match_src=True, in_context=False),
                    _row(index=4, in_context=True),
                ],
                "total": 1,
                "context": 1,
            }
        )

        result = _tool_search_input(runner, {"query": "x", "context": 1})

        for row in result["results"]:
            self.assertNotIn("in_context", row)
        self.assertEqual(result["context"], 1)
        self.assertIn("包含关键词的那行是命中", result["note"])
        self.assertIn("带上下文时命中上限收紧", result["note"])

    def test_no_context_means_no_context_note(self):
        runner = _SearchRunner(response={"results": [_row(match_src=True)], "total": 1})
        result = _tool_search_input(runner, {"query": "x"})
        self.assertNotIn("note", result)


class SearchInputFailedFileTests(unittest.TestCase):
    """解析失败的文件被跳过时必须说明：否则"这里没有"和"这里没读"看起来一样。"""

    def test_failed_files_are_reported(self):
        runner = _SearchRunner(
            response={"results": [_row(match_src=True)], "total": 1, "files_failed": ["broken.json"]}
        )

        result = _tool_search_input(runner, {"query": "x"})

        self.assertIn("broken.json", result["note"])
        self.assertIn("解析失败", result["note"])
        self.assertIn("未知", result["note"])  # 这些文件里有没有命中是未知的


class SearchInputEmptyResultTests(unittest.TestCase):
    """0 命中时：指了文件就去确认文件名写没写错，别让模型以为关键词不匹配。"""

    def test_unknown_filename_gets_a_hint(self):
        runner = _SearchRunner(response={"results": [], "total": 0})

        result = _tool_search_input(runner, {"query": "x", "filename": "b.json"})

        self.assertIn("输入文件 b.json 不存在", result["note"])
        self.assertEqual(runner.gets, ["/api/projects/proj/files"])

    def test_known_filename_needs_no_hint(self):
        runner = _SearchRunner(response={"results": [], "total": 0})

        result = _tool_search_input(runner, {"query": "x", "filename": "a.json"})

        self.assertNotIn("note", result)

    def test_no_filename_means_no_lookup(self):
        runner = _SearchRunner(response={"results": [], "total": 0})

        result = _tool_search_input(runner, {"query": "x"})

        self.assertNotIn("note", result)
        self.assertEqual(runner.gets, [])  # 没指文件就不必多问一次文件清单

    def test_total_keeps_reporting_every_hit_after_capping(self):
        runner = _SearchRunner(
            response={"results": [_row(match_src=True, in_context=False)], "total": 137, "context": 1}
        )

        result = _tool_search_input(runner, {"query": "x", "context": 1})

        self.assertEqual(result["total"], 137)


class SearchInputRegistrationTests(unittest.TestCase):
    def test_tool_is_read_only_so_it_never_asks_for_permission(self):
        self.assertEqual(rt._tool_risk("search_input"), rt.PERMISSION_READ)

    def test_schema_and_handler_are_registered(self):
        names = [t["function"]["name"] for t in rt.AGENT_TOOLS]
        self.assertIn("search_input", names)
        self.assertIn("search_input", rt._TOOL_HANDLERS)

    def test_schema_advertises_the_same_fields_the_handler_accepts(self):
        schema = next(t for t in rt.AGENT_TOOLS if t["function"]["name"] == "search_input")
        props = schema["function"]["parameters"]["properties"]
        self.assertEqual(props["field"]["enum"], ["all", "src", "name"])
        self.assertEqual(schema["function"]["parameters"]["required"], ["query"])

    def test_explore_subagent_can_search_the_source_text(self):
        role = rt.SUBAGENT_ROLES[rt.SUBAGENT_AGENT_EXPLORE]
        self.assertIn("search_input", role.tools)

    def test_search_is_not_file_locked(self):
        """跨文件的搜索不该被子代理的文件边界锁住（与 search_transl_cache 同理）。"""
        self.assertNotIn("search_input", rt._LOCKED_FILENAME_TOOLS)


if __name__ == "__main__":
    unittest.main()
