"""list_problems 的 context：每条问题在表里并上 N 句上文（语义同 read_transl_cache）。

问题行自己往往看不出"为什么有问题"——修「残留日文」「译名不一致」要看着上文才敢动手，
逐条 read_transl_cache 又太碎。默认只给上文（only_preceding，省 token），传 false 才前后
都给。上下文行的 index 带 *（如 4*）：它没有 problem 字段，又和问题行混排在同一张表里，
得有个一眼认得出的标记。同一文件相邻问题的窗口合并；returned 只数问题行；缓存取不到的
文件只少带上下文、问题行照给。
"""

import unittest
import urllib.parse

from GalTransl.Agent.runtime import (
    AgentState,
    AgentToolError,
    _md_render_list_problems,
    _tool_list_problems,
)


class _Runner:
    """最小 runner：/problems 回问题清单，/cache/{file} 回缓存条目。"""

    def __init__(self, problems, caches=None):
        self.state = AgentState(project_dir=r"C:\proj", config_file_name="config.yaml")
        self._problems = problems
        self._caches = caches or {}
        self.cache_calls: list[str] = []

    def _project_id(self):
        return "proj"

    def _http_get(self, url):
        if "/problems" in url:
            return {
                "problems": [dict(p) for p in self._problems],
                "total": len(self._problems),
                "filter_keys": [],
            }
        prefix = "/api/projects/proj/cache/"
        if url.startswith(prefix):
            fname = urllib.parse.unquote(url[len(prefix):])
            self.cache_calls.append(fname)
            if fname not in self._caches:
                raise AgentToolError("no such cache file")
            return {"entries": [dict(e) for e in self._caches[fname]]}
        raise AssertionError(f"unexpected url: {url}")


def _problem(fname, index, problem="残留日文"):
    return {
        "filename": fname,
        "index": index,
        "speaker": "雪菜",
        "post_src": f"原文{index}",
        "pre_dst": f"译文{index}",
        "problem": problem,
        "trans_by": "engine",
    }


def _entry(index, name="雪菜", src=None, dst=None):
    return {
        "index": index,
        "name": name,
        "post_src": src if src is not None else f"原文{index}",
        "pre_dst": dst if dst is not None else f"译文{index}",
        "trans_by": "engine",
    }


class ContextMergeTests(unittest.TestCase):
    def test_context_rows_come_from_above_and_carry_a_star(self):
        runner = _Runner(
            [_problem("a.json", 5)],
            {"a.json": [_entry(i) for i in range(1, 9)]},
        )

        out = _tool_list_problems(runner, {"problem_type": "残留日文", "context": 2})

        self.assertEqual(out["returned"], 1)  # 只数问题行
        rows = out["problems"]
        # 默认只给上文：5 前面 2 句 → 3、4；上下文行的 index 带 *
        self.assertEqual([r["index"] for r in rows], ["3*", "4*", 5])
        self.assertEqual(rows[2]["problem"], "残留日文")
        for r in rows[:2]:
            self.assertNotIn("problem", r)
            self.assertNotIn("trans_by", r)
        self.assertEqual(out["context"], 2)
        self.assertTrue(out["only_preceding"])
        self.assertNotIn("in_context", out)

    def test_only_preceding_false_gives_both_sides(self):
        runner = _Runner([_problem("a.json", 5)], {"a.json": [_entry(i) for i in range(1, 9)]})

        out = _tool_list_problems(
            runner, {"problem_type": "残留日文", "context": 2, "only_preceding": False}
        )

        self.assertFalse(out["only_preceding"])
        self.assertEqual([r["index"] for r in out["problems"]], ["3*", "4*", 5, "6*", "7*"])

    def test_overlapping_windows_are_merged(self):
        # 相邻两条问题（5、6）：上文窗口 3-5 与 4-6 合并成 3-6，上下文行不重复
        runner = _Runner(
            [_problem("a.json", 5), _problem("a.json", 6)],
            {"a.json": [_entry(i) for i in range(1, 11)]},
        )

        out = _tool_list_problems(runner, {"problem_type": "残留日文", "context": 2})

        self.assertEqual(out["returned"], 2)
        self.assertEqual([r["index"] for r in out["problems"]], ["3*", "4*", 5, 6])
        problem_rows = [r["index"] for r in out["problems"] if r.get("problem")]
        self.assertEqual(problem_rows, [5, 6])

    def test_files_keep_their_own_windows(self):
        runner = _Runner(
            [_problem("a.json", 5), _problem("b.json", 20)],
            {
                "a.json": [_entry(i) for i in range(1, 9)],
                "b.json": [_entry(i) for i in range(18, 23)],
            },
        )

        out = _tool_list_problems(runner, {"problem_type": "残留日文", "context": 1})

        rows = out["problems"]
        self.assertEqual(
            [(r["filename"], r["index"]) for r in rows],
            [("a.json", "4*"), ("a.json", 5), ("b.json", "19*"), ("b.json", 20)],
        )
        self.assertEqual(runner.cache_calls, ["a.json", "b.json"])  # 每个文件只取一次

    def test_context_rows_fall_back_to_old_cache_keys(self):
        # 老缓存是 pre_jp/post_jp 那套键名：上下文行照样取得到
        runner = _Runner(
            [_problem("a.json", 2)],
            {"a.json": [{"index": 1, "name": "雪菜", "post_jp": "旧原文", "pre_zh": "旧译文"}, _entry(2), _entry(3)]},
        )

        out = _tool_list_problems(runner, {"problem_type": "残留日文", "context": 1})

        ctx = out["problems"][0]
        self.assertEqual(ctx["post_src"], "旧原文")
        self.assertEqual(ctx["pre_dst"], "旧译文")

    def test_problem_rows_missing_from_cache_are_kept(self):
        # 问题清单里有 index=9，缓存里只有 1~5（不同步）：问题行不能丢
        runner = _Runner(
            [_problem("a.json", 2), _problem("a.json", 9)],
            {"a.json": [_entry(i) for i in range(1, 6)]},
        )

        out = _tool_list_problems(runner, {"problem_type": "残留日文", "context": 1})

        problem_rows = [r for r in out["problems"] if r.get("problem")]
        self.assertEqual([r["index"] for r in problem_rows], [2, 9])

    def test_unreadable_cache_keeps_problem_rows_silently(self):
        runner = _Runner(
            [_problem("a.json", 5), _problem("b.json", 3)],
            {"a.json": [_entry(i) for i in range(1, 9)]},  # b.json 取不到
        )

        out = _tool_list_problems(runner, {"problem_type": "残留日文", "context": 2})

        b_rows = [r for r in out["problems"] if r["filename"] == "b.json"]
        self.assertEqual(len(b_rows), 1)  # 问题行照给，只是没有前后文
        self.assertNotIn("note", out)  # 不做任何提示
        self.assertNotIn("context_missing_files", out)

    def test_context_zero_fetches_nothing(self):
        runner = _Runner([_problem("a.json", 5)], {"a.json": [_entry(i) for i in range(1, 9)]})

        out = _tool_list_problems(runner, {"problem_type": "残留日文"})

        self.assertEqual(runner.cache_calls, [])
        self.assertEqual([r["index"] for r in out["problems"]], [5])

    def test_invalid_context_is_rejected(self):
        runner = _Runner([_problem("a.json", 5)])
        with self.assertRaises(AgentToolError):
            _tool_list_problems(runner, {"problem_type": "残留日文", "context": "abc"})

    def test_oversized_context_is_clamped(self):
        # 超上限不是报错而是收到 5（比 read/search 的 20 收得更紧：一页最多 20 条问题，
        # 每条再带上文，返回体得收得住）
        runner = _Runner([_problem("a.json", 5)], {"a.json": [_entry(i) for i in range(1, 9)]})

        out = _tool_list_problems(runner, {"problem_type": "残留日文", "context": 99})

        rows = out["problems"]
        self.assertEqual([r["index"] for r in rows], ["1*", "2*", "3*", "4*", 5])  # 收到的 5 句上文
        self.assertEqual(sum(1 for r in rows if r.get("problem")), 1)

    def test_default_limit_is_10_capped_at_20(self):
        problems = [_problem("a.json", i) for i in range(1, 31)]
        runner = _Runner(problems)

        out = _tool_list_problems(runner, {"problem_type": "残留日文"})
        self.assertEqual(out["returned"], 10)  # 默认 10
        self.assertTrue(out["has_more"])

        out2 = _tool_list_problems(runner, {"problem_type": "残留日文", "limit": 99})
        self.assertEqual(out2["returned"], 20)  # 上限收到 20
        self.assertTrue(out2["has_more"])

    def test_stats_mode_ignores_context(self):
        runner = _Runner([_problem("a.json", 5)], {"a.json": [_entry(5)]})

        out = _tool_list_problems(runner, {"context": 2})

        self.assertEqual(out["mode"], "stats")
        self.assertEqual(runner.cache_calls, [])


class MarkdownRenderTests(unittest.TestCase):
    def test_rendered_table_shows_context_rows_with_empty_problem_cell(self):
        runner = _Runner(
            [_problem("a.json", 5)],
            {"a.json": [_entry(i) for i in range(4, 7)]},
        )
        out = _tool_list_problems(runner, {"problem_type": "残留日文", "context": 1})

        rendered = _md_render_list_problems(out)

        # 上下文行：index 带 *、problem 列为空
        self.assertIn("| a.json | 4* | 雪菜 | 原文4 | 译文4 |  |  |", rendered)
        self.assertIn("| a.json | 5 | 雪菜 | 原文5 | 译文5 | 残留日文 |  |", rendered)
        self.assertIn("含上文（每条问题前面 1 句", rendered)
        self.assertIn("index 带 * 的是上下文行", rendered)
        self.assertNotIn("备注", rendered)

    def test_rendered_without_context_has_no_context_head(self):
        runner = _Runner([_problem("a.json", 5)])
        out = _tool_list_problems(runner, {"problem_type": "残留日文"})

        rendered = _md_render_list_problems(out)

        self.assertNotIn("含上下文", rendered)
        self.assertNotIn("含上文", rendered)


if __name__ == "__main__":
    unittest.main()
