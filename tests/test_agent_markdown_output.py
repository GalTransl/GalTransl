"""大清单类工具的 Markdown 输出（_render_tool_result_table，当前启用的渲染层）。

Markdown 表格是模型最熟的形状，单元格内只剩竖线一种转义。可表格化的用表格，
计数/提示/警告按文字写在表格前后——这里钉的就是"哪些进了表格、哪些是文字"。
"""

import re
import unittest
from types import SimpleNamespace

from GalTransl.Agent import runtime as rt
from GalTransl.Agent.runtime import (
    _render_tool_result_table,
    _tool_list_input_files,
    _tool_read_transl_cache,
)


def _table_rows(text: str, header_cell: str) -> list[list[str]]:
    """取 Markdown 表格的数据行（按列名定位表格），拆好单元格。

    拆列必须跳过转义的 \\|（负向断言），否则带竖线的单元格会被切开。"""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(f"| {header_cell} ") or line.startswith(f"| {header_cell} |"):
            rows = []
            for row in lines[i + 2 :]:  # 跳过分隔行
                if not row.startswith("|"):
                    break
                row = row.strip()
                rows.append([cell.strip() for cell in re.split(r"(?<!\\)\|", row[1:-1])])
            return rows
    raise AssertionError(f"找不到表格：{header_cell}\n{text}")


class ListTranslCacheMdTests(unittest.TestCase):
    def test_head_table_and_note(self):
        result = {
            "cache_files": [
                {"name": "a.json", "size": 10, "entries": 45},
                {"name": "b.append.jsonl", "size": 20, "status": "translating"},  # 增量日志没有 entries
            ],
            "count": 2,
            "returned": 2,
            "sampled": False,
            "translating": 1,
            "note": "有 1 个 .append.jsonl 增量缓存文件，说明对应文件正在翻译中。",
        }

        text = _render_tool_result_table("list_transl_cache", result)

        self.assertIn("共 2 个缓存文件", text)
        self.assertIn("1 个正在翻译", text)
        self.assertIn("备注：有 1 个 .append.jsonl", text)
        rows = _table_rows(text, "name")
        self.assertEqual(rows[0], ["a.json", "10", "45", ""])  # 没有的列就是空单元格
        self.assertEqual(rows[1], ["b.append.jsonl", "20", "", "translating"])

    def test_sampled_state_is_said_in_words(self):
        result = {
            "cache_files": [{"name": "a.json", "size": 1, "entries": 1}],
            "count": 250,
            "returned": 100,
            "sampled": True,
        }

        text = _render_tool_result_table("list_transl_cache", result)

        self.assertIn("共 250 个缓存文件", text)
        self.assertIn("均匀采样", text)
        self.assertIn("100", text)


class ListInputFilesMdTests(unittest.TestCase):
    def test_total_covers_files_not_shown(self):
        result = {
            "input_files": [
                {"name": "a.json", "size": 10, "sentences": 7},
                {"name": "b.json", "size": 20, "sentences": None},
            ],
            "count": 150,
            "returned": 100,
            "sampled": True,
            "sentences_total": 1050,
            "note": "sentences 是输入文件解析出的条数，只用来估工作量。",
        }

        text = _render_tool_result_table("list_input_files", result)

        self.assertIn("共 150 个输入文件", text)
        self.assertIn("句数合计 1050（含没列出来的文件）", text)
        rows = _table_rows(text, "name")
        self.assertEqual(rows[1], ["b.json", "20", ""])


class ListProblemsMdTests(unittest.TestCase):
    def test_stats_mode(self):
        result = {
            "total": 75,
            "mode": "stats",
            "types": [{"type": "残留日文", "count": 45}, {"type": "标点错漏", "count": 30}],
            "hint": "默认只返回类型统计。用 problem_type 指定类型查看具体条目。",
        }

        text = _render_tool_result_table("list_problems", result)

        self.assertIn("共 75 个问题，类型统计如下", text)
        self.assertIn("提示：默认只返回类型统计", text)
        rows = _table_rows(text, "type")
        self.assertEqual(rows, [["残留日文", "45"], ["标点错漏", "30"]])

    def test_list_mode_with_majority_and_paging(self):
        result = {
            "total": 90,
            "matched": 45,
            "problem_type": "残留日文",
            "offset": 0,
            "returned": 2,
            "has_more": True,
            "majority_trans_by": "demo-model",
            "problems": [
                {"filename": "a.json", "index": 33, "speaker": "少女", "post_src": "ドルードだ", "pre_dst": "多鲁德", "trans_by": "demo-model"},
                {"filename": "a.json", "index": 41, "speaker": "", "post_src": "……", "pre_dst": "…", "trans_by": "deepseek-chat"},
            ],
        }

        text = _render_tool_result_table("list_problems", result)

        self.assertIn("问题类型：残留日文", text)
        self.assertIn("命中 45 条", text)
        self.assertIn("还有更多（用 offset 翻页）", text)
        self.assertIn("多数派模型 demo-model（表里已省略，只留少数派/改过的来源）", text)
        rows = _table_rows(text, "filename")
        # 多数派的 problem/trans_by 都已省略（空单元格）；少数派的 trans_by 逐行保留
        self.assertEqual(rows[0][4], "多鲁德")
        self.assertEqual(rows[1][6], "deepseek-chat")


class ReadInputFileMdTests(unittest.TestCase):
    def test_head_missing_and_cell_escaping(self):
        result = {
            "filename": "a.json",
            "count": 5,
            "returned": 2,
            "missing_indexes": [4, 5],
            "entries": [
                {"index": 1, "name": "少女", "pre_src": "おはよう"},
                {"index": 2, "name": "", "pre_src": "第一行\n第二行 | 有竖线"},
            ],
        }

        text = _render_tool_result_table("read_input_file", result)

        self.assertIn("文件 a.json", text)
        self.assertIn("共 5 条，显示 2 条", text)
        self.assertIn("缺失 index：4,5", text)
        rows = _table_rows(text, "index")
        self.assertEqual(rows[1][2], "第一行<br>第二行 \\| 有竖线")


class ReadTranslCacheMdTests(unittest.TestCase):
    def test_head_context_majority_and_dynamic_columns(self):
        result = {
            "filename": "a.json",
            "count": 3,
            "returned": 3,
            "context": 2,
            "majority_trans_by": "demo-model",
            "fields": ["index", "post_src", "pre_dst", "trans_by"],
            "entries": [
                {"index": 1, "post_src": "a", "pre_dst": "A"},
                {"index": 2, "post_src": "b", "pre_dst": "B", "trans_by": "deepseek-chat"},
                {"index": 3, "post_src": "c", "pre_dst": "C"},
            ],
        }

        text = _render_tool_result_table("read_transl_cache", result)

        self.assertIn("文件 a.json", text)
        self.assertIn("共 3 条，显示 3 条", text)
        self.assertIn("含上下文（点名的条目前后各 2 句）", text)
        self.assertIn("多数派模型 demo-model", text)
        rows = _table_rows(text, "index")
        # trans_by 多数派已删（空单元格），少数派逐行保留
        self.assertEqual(rows[0][3], "")
        self.assertEqual(rows[1][3], "deepseek-chat")

    def test_warning_and_fields_note_are_written_as_text(self):
        result = {
            "filename": "a.append.jsonl",
            "count": 1,
            "returned": 1,
            "warning": "这是翻译中的增量缓存文件",
            "fields_note": "默认精简字段",
            "fields": ["index", "pre_dst"],
            "entries": [{"index": 1, "pre_dst": "x"}],
        }

        text = _render_tool_result_table("read_transl_cache", result)

        self.assertIn("警告：这是翻译中的增量缓存文件", text)
        self.assertIn("字段说明：默认精简字段", text)


class ManageProblemFilterMdTests(unittest.TestCase):
    """过滤清单的 list 结果：每条过滤项挡住了多少条问题。"""

    def test_list_shows_hits_per_key(self):
        result = {
            "filter_keys": ["缺失.*标点", "残留日文"],
            "count": 2,
            "filters": [
                {"key": "缺失.*标点", "problems": 12},
                {"key": "残留日文", "problems": 0},
            ],
            "problem_entries": 40,
            "visible_entries": 28,
        }

        text = _render_tool_result_table("manage_problem_filter", result)

        self.assertIn("共 2 条过滤项", text)
        self.assertIn("当前共 40 条问题，其中 12 条被过滤项挡住，list_problems 可见 28 条", text)
        self.assertEqual(_table_rows(text, "key"), [["缺失.*标点", "12"], ["残留日文", "0"]])
        self.assertIn("problems 为 0 的过滤项当前一条也挡不到，可考虑 remove", text)

    def test_all_keys_hit_has_no_zero_hint(self):
        result = {
            "filter_keys": ["缺失.*标点"],
            "count": 1,
            "filters": [{"key": "缺失.*标点", "problems": 3}],
        }

        text = _render_tool_result_table("manage_problem_filter", result)

        self.assertNotIn("problems 为 0", text)

    def test_add_remove_results_stay_json(self):
        # changes 是变更 diff、不是清单：没有可表格化的数据行 → 返回 None 走 JSON
        self.assertIsNone(
            _render_tool_result_table(
                "manage_problem_filter",
                {"filter_keys": ["a"], "count": 1, "added": ["a"]},
            )
        )


class HandlerPipelineTests(unittest.TestCase):
    """handler 仍返回 dict，Markdown 渲染接在后面：真实调用走一遍。"""

    def test_read_transl_cache_end_to_end(self):
        class _Runner:
            state = SimpleNamespace(config_file_name="config.yaml", project_dir=r"C:\proj")

            def _project_id(self):
                return "proj"

            def _http_get(self, url):
                return {
                    "entries": [
                        {"index": 1, "name": "少女", "post_src": "ドルードだ", "pre_dst": "多鲁德"},
                        {"index": 2, "name": "", "post_src": "またね", "pre_dst": "回见"},
                    ]
                }

        result = _tool_read_transl_cache(_Runner(), {"filename": "a.json"})

        self.assertIsInstance(result, dict)  # handler 口径不变
        text = _render_tool_result_table("read_transl_cache", result)
        self.assertIn("| index |", text)
        self.assertIn("| 1 | 少女 | ドルードだ | 多鲁德 |", text)


class DispatcherTests(unittest.TestCase):
    def test_unknown_tool_and_non_dict_fall_back_to_json(self):
        self.assertIsNone(_render_tool_result_table("search_transl_cache", {"results": []}))
        self.assertIsNone(_render_tool_result_table("list_problems", "不是 dict"))

    def test_active_renderer_set_covers_exactly_the_listed_tools(self):
        self.assertEqual(
            set(rt._MD_RENDERERS),
            {
                "list_transl_cache",
                "list_input_files",
                "list_problems",
                "read_input_file",
                "read_transl_cache",
                "manage_problem_filter",
            },
        )
        # ISON 渲染层已删：确认没有残留的旧渲染注册表
        self.assertFalse(any("ISON" in name or "ison" in name for name in dir(rt)))


if __name__ == "__main__":
    unittest.main()
