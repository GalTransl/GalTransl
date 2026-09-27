"""工具合并：read_transl_cache 一个工具管列文件 / 读条目 / 搜索，create_dict_file 并入 save_dict。

- read_transl_cache 的 action 显式给了就按它；不给按参数推断（有 query 是 search，有 filename 是
  read，否则 list），结果带上 action，渲染器据此选表格；
- 子代理的锁定范围只卡 action=read（list 只列锁定的文件，search 照旧全项目）；
- save_dict 带 category 且文件不存在时先新建并登记，再写入；没带 category 写不存在的文件要报错；
- 旧工具名（list_transl_cache / search_transl_cache / create_dict_file）调用时回明确的改法。
"""

import unittest
import urllib.parse
from types import SimpleNamespace

from GalTransl.Agent.handlers import _RETIRED_TOOLS, _TOOL_HANDLERS
from GalTransl.Agent.models import AgentToolError
from GalTransl.Agent.subagent import SUBAGENT_AGENT_PROOFREAD, _subagent_handlers
from GalTransl.Agent.tools.cache import _cache_read_action, _tool_read_transl_cache
from GalTransl.Agent.tools.dicts import _tool_save_dict
from GalTransl.Agent.tools.preview import _preview_dict_write
from GalTransl.Agent.tools.render_md import _render_tool_result_table


class _CacheRunner:
    """内存里的假缓存：/cache 列文件，/cache/<name> 读条目，/cache/search 记下请求体。"""

    def __init__(self, files):
        self.state = SimpleNamespace(config_file_name="config.yaml", project_dir="/fake/project")
        self.files = files
        self.searches: list[dict] = []

    def _project_id(self):
        return "proj"

    def _http_get(self, path):
        if path.endswith("/cache"):
            return {"files": [{"name": n, "size": 1, "entry_count": len(e)} for n, e in self.files.items()]}
        name = urllib.parse.unquote(path.rsplit("/", 1)[-1])
        return {"entries": [dict(e) for e in self.files[name]]}

    def _http_post(self, path, body):
        assert path.endswith("/cache/search"), path
        self.searches.append(body)
        return {"results": [{"filename": "a.json", "index": 1, "post_src": "ドルード", "pre_dst": "多鲁德"}], "total": 1}


FILES = {
    "a.json": [{"index": 1, "post_src": "ドルード", "pre_dst": "多鲁德", "problem": ""}],
    "b.json": [{"index": 1, "post_src": "原文", "pre_dst": "译文", "problem": ""}],
}


class ActionInferenceTests(unittest.TestCase):
    def test_explicit_action_wins(self):
        self.assertEqual(_cache_read_action({"action": "LIST", "filename": "a.json"}), "list")

    def test_inferred_from_arguments(self):
        self.assertEqual(_cache_read_action({"query": "x", "filename": "a.json"}), "search")
        self.assertEqual(_cache_read_action({"filename": "a.json"}), "read")
        self.assertEqual(_cache_read_action({}), "list")

    def test_unknown_action_is_rejected(self):
        with self.assertRaises(AgentToolError):
            _cache_read_action({"action": "grep"})


class UnifiedToolTests(unittest.TestCase):
    def test_list_read_search_dispatch_and_render(self):
        runner = _CacheRunner(FILES)

        listed = _tool_read_transl_cache(runner, {"action": "list"})
        self.assertEqual(listed["action"], "list")
        self.assertEqual([f["name"] for f in listed["cache_files"]], ["a.json", "b.json"])
        self.assertIn("共 2 个缓存文件", _render_tool_result_table("read_transl_cache", listed))

        read = _tool_read_transl_cache(runner, {"filename": "a.json"})
        self.assertEqual(read["action"], "read")
        self.assertIn("文件 a.json", _render_tool_result_table("read_transl_cache", read))

        found = _tool_read_transl_cache(runner, {"query": "ドルード", "field": "src"})
        self.assertEqual(found["action"], "search")
        self.assertEqual(runner.searches[-1]["query"], "ドルード")
        self.assertIn("共 1 条命中", _render_tool_result_table("read_transl_cache", found))

    def test_search_without_query_and_read_without_filename_fail_clearly(self):
        runner = _CacheRunner(FILES)
        with self.assertRaisesRegex(AgentToolError, "query"):
            _tool_read_transl_cache(runner, {"action": "search"})
        with self.assertRaisesRegex(AgentToolError, "filename"):
            _tool_read_transl_cache(runner, {"action": "read"})


class SubagentScopeTests(unittest.TestCase):
    def test_read_is_locked_list_is_scoped_search_is_open(self):
        runner = _CacheRunner(FILES)
        handler = _subagent_handlers(SUBAGENT_AGENT_PROOFREAD, "a.json")["read_transl_cache"]

        handler(runner, {"filename": "a.json"})  # 自己那份
        with self.assertRaisesRegex(AgentToolError, "不在范围里"):
            handler(runner, {"filename": "b.json"})

        listed = handler(runner, {"action": "list"})
        self.assertEqual([f["name"] for f in listed["cache_files"]], ["a.json"])
        self.assertIn("a.json", listed["note"])

        # 搜索是全项目的，带别的文件名也放行
        handler(runner, {"action": "search", "query": "ドルード", "filename": "b.json"})
        self.assertEqual(runner.searches[-1]["filename"], "b.json")


class _DictRunner:
    def __init__(self, contents):
        self.state = SimpleNamespace(config_file_name="config.yaml")
        self.contents = contents
        self.posts: list[tuple[str, dict]] = []

    def _project_id(self):
        return "proj"

    def _http_get(self, _path):
        return {"dict_contents": self.contents}

    def _http_post(self, path, body):
        self.posts.append((path, body))
        if path.endswith("/create"):
            key = "(project_dir)" + body["filename"]
            self.contents[key] = {"lines": [], "count": 0}
            return {"success": True, "file_key": key}
        return {"success": True, "file_key": body["file_key"]}


EXISTING = {"(project_dir)项目GPT字典.txt": {"lines": ["a\tA"], "count": 1}}


class SaveDictCreateTests(unittest.TestCase):
    def test_category_creates_then_writes(self):
        runner = _DictRunner(dict(EXISTING))
        result = _tool_save_dict(
            runner, {"file_key": "新字典.txt", "category": "gpt", "content": "b\tB", "action": "append"}
        )
        paths = [p for p, _ in runner.posts]
        self.assertTrue(paths[0].endswith("/dictionary/project/create"))
        self.assertEqual(runner.posts[0][1]["category"], "gpt")
        self.assertEqual(runner.posts[0][1]["filename"], "新字典.txt")
        self.assertTrue(paths[1].endswith("/dictionary/project/save"))
        self.assertEqual(result["file_key"], "(project_dir)新字典.txt")
        self.assertTrue(result["created"])
        self.assertEqual(result["line_count_after"], 1)

    def test_empty_content_just_creates_the_file(self):
        runner = _DictRunner(dict(EXISTING))
        result = _tool_save_dict(runner, {"file_key": "空字典.txt", "category": "pre", "content": ""})
        self.assertEqual(len(runner.posts), 1)  # 只建文件，不写
        self.assertTrue(result["created"])
        self.assertIn("译前", result["note"])

    def test_existing_file_ignores_category(self):
        runner = _DictRunner(dict(EXISTING))
        _tool_save_dict(
            runner,
            {"file_key": "(project_dir)项目GPT字典.txt", "category": "post", "content": "b\tB", "action": "append"},
        )
        self.assertEqual([p.rsplit("/", 1)[-1] for p, _ in runner.posts], ["save"])

    def test_missing_file_without_category_is_an_error(self):
        runner = _DictRunner(dict(EXISTING))
        with self.assertRaisesRegex(AgentToolError, "category"):
            _tool_save_dict(runner, {"file_key": "不存在.txt", "content": "b\tB"})
        self.assertEqual(runner.posts, [])

    def test_replace_or_delete_cannot_create(self):
        runner = _DictRunner(dict(EXISTING))
        with self.assertRaisesRegex(AgentToolError, "overwrite 或 append"):
            _tool_save_dict(runner, {"file_key": "新.txt", "category": "gpt", "content": "a", "action": "delete"})
        self.assertEqual(runner.posts, [])

    def test_preview_marks_creation(self):
        runner = _DictRunner(dict(EXISTING))
        preview = _preview_dict_write(runner, {"file_key": "新.txt", "category": "gpt", "content": "b\tB"})
        self.assertEqual(preview["create"], {"category": "gpt"})
        self.assertEqual(preview["file_key"], "(project_dir)新.txt")
        self.assertIn("line_diff", preview)
        self.assertEqual(runner.posts, [])  # 预览只读


class RetiredToolTests(unittest.TestCase):
    def test_old_names_are_gone_but_explained(self):
        for name in ("list_transl_cache", "search_transl_cache", "create_dict_file"):
            self.assertNotIn(name, _TOOL_HANDLERS)
            self.assertIn(name, _RETIRED_TOOLS)
        self.assertIn('action="list"', _RETIRED_TOOLS["list_transl_cache"])
        self.assertIn("category", _RETIRED_TOOLS["create_dict_file"])


if __name__ == "__main__":
    unittest.main()
