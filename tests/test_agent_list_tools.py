"""清单类工具（list_transl_cache / list_input_files）的 grep 与 limit。

回归背景：两个工具原本把整份文件清单倒给模型。大项目几百上千个文件，既费 token 又淹掉
重点；而"只给前 N 个"更糟——清单按名字排序，后半段等于不存在，模型会以为项目里没有那些
文件。所以按 limit（默认 100）**均匀采样**（含首尾、等距摊满整个清单），要精确定位用
grep 缩小范围。这里钉的就是这两件事：采样均匀且首尾都在、grep 命中数与过滤口径。
"""

import unittest
from types import SimpleNamespace

from GalTransl.Agent import runtime as rt
from GalTransl.Agent.runtime import AgentToolError, _tool_list_input_files, _tool_list_transl_cache


class _ListRunner:
    """备好 /cache 与 /files 两份清单的只读替身。"""

    def __init__(self, *, cache_files=None, input_files=None):
        self.state = SimpleNamespace(config_file_name="config.yaml", project_dir=r"C:\proj")
        self.cache_files = cache_files if cache_files is not None else []
        self.input_files = input_files if input_files is not None else []
        self.gets: list[str] = []

    def _project_id(self) -> str:
        return "proj"

    def _http_get(self, url: str):
        self.gets.append(url)
        if url.endswith("/cache"):
            return {"files": [dict(f) for f in self.cache_files]}
        if "/files?" in url:
            return {"input_files": [dict(f) for f in self.input_files]}
        raise AssertionError(f"不该读这个地址：{url}")


def _cache_files(count: int) -> list[dict]:
    return [
        {"name": f"sc_{i:03d}.json", "size": 10, "entry_count": i + 1} for i in range(count)
    ]


def _input_files(count: int, *, sentences: int = 7) -> list[dict]:
    return [
        {"name": f"sc_{i:03d}.txt.json", "is_file": True, "size": 10, "sentences": sentences}
        for i in range(count)
    ]


class SampleEvenlyTests(unittest.TestCase):
    """采样本身：短清单原样给、长清单首尾都在且等距。"""

    def test_short_list_is_returned_as_is(self):
        items = list(range(3))
        self.assertEqual(rt._sample_evenly(items, 100), items)

    def test_long_list_keeps_first_last_and_is_evenly_spaced(self):
        items = list(range(250))
        picked = rt._sample_evenly(items, 100)

        self.assertEqual(len(picked), 100)
        self.assertEqual(picked[0], 0)
        self.assertEqual(picked[-1], 249)
        self.assertEqual(picked, sorted(picked))
        self.assertEqual(len(set(picked)), 100)  # 不重复
        steps = {b - a for a, b in zip(picked, picked[1:])}
        self.assertLessEqual(max(steps) - min(steps), 1)  # 间距最多差 1 = 均匀

    def test_edge_cases(self):
        self.assertEqual(rt._sample_evenly([], 5), [])
        self.assertEqual(rt._sample_evenly([1, 2, 3], 1), [1])


class ListTranslCacheTests(unittest.TestCase):
    def test_default_limit_samples_instead_of_truncating(self):
        runner = _ListRunner(cache_files=_cache_files(250))

        result = _tool_list_transl_cache(runner, {})

        self.assertEqual(result["count"], 250)
        self.assertEqual(result["returned"], 100)
        self.assertTrue(result["sampled"])
        names = [f["name"] for f in result["cache_files"]]
        self.assertEqual(names[0], "sc_000.json")
        self.assertEqual(names[-1], "sc_249.json")  # 尾部文件没有被藏起来
        self.assertIn("均匀采样", result["note"])
        self.assertIn("grep", result["note"])

    def test_short_list_has_no_sampling_note(self):
        runner = _ListRunner(cache_files=_cache_files(3))

        result = _tool_list_transl_cache(runner, {})

        self.assertEqual(result["returned"], 3)
        self.assertFalse(result["sampled"])
        self.assertNotIn("note", result)

    def test_grep_filters_by_filename_case_insensitively(self):
        runner = _ListRunner(
            cache_files=[
                {"name": "sc_1_st01.json", "size": 1, "entry_count": 3},
                {"name": "SC_10_st02.json", "size": 1, "entry_count": 4},
                {"name": "common.json", "size": 1, "entry_count": 5},
            ]
        )

        result = _tool_list_transl_cache(runner, {"grep": "SC_1"})

        self.assertEqual([f["name"] for f in result["cache_files"]], ["sc_1_st01.json", "SC_10_st02.json"])
        self.assertEqual(result["count"], 2)
        self.assertIn('grep="SC_1"', result["note"])

    def test_grep_and_limit_work_together(self):
        runner = _ListRunner(cache_files=_cache_files(250))

        result = _tool_list_transl_cache(runner, {"grep": "sc_1", "limit": 10})

        # sc_1xx 共 100 个（sc_100~sc_199）→ 采样 10 个，且只在这个子集里取
        self.assertEqual(result["count"], 100)
        self.assertEqual(result["returned"], 10)
        for item in result["cache_files"]:
            self.assertTrue(item["name"].startswith("sc_1"))

    def test_entry_counts_survive_and_incremental_logs_are_filtered(self):
        runner = _ListRunner(
            cache_files=[
                {"name": "a.json", "size": 1, "entry_count": 0},
                {"name": "b.append.jsonl", "size": 2},
            ]
        )

        result = _tool_list_transl_cache(runner, {})

        by_name = {f["name"]: f for f in result["cache_files"]}
        self.assertEqual(list(by_name), ["a.json"])  # 增量日志不进清单
        self.assertEqual(result["count"], 1)
        self.assertEqual(by_name["a.json"]["entries"], 0)  # 0 条也要如实给
        self.assertNotIn("status", by_name["a.json"])
        self.assertNotIn("translating", result)

    def test_grep_cannot_bring_back_incremental_logs(self):
        runner = _ListRunner(cache_files=[{"name": "sc_2.append.jsonl", "size": 1}])

        result = _tool_list_transl_cache(runner, {"grep": "sc_2"})

        self.assertEqual(result["cache_files"], [])
        self.assertEqual(result["count"], 0)

    def test_limit_is_clamped_and_validated(self):
        runner = _ListRunner(cache_files=_cache_files(600))
        self.assertEqual(_tool_list_transl_cache(runner, {"limit": 9999})["returned"], 500)

        with self.assertRaises(AgentToolError):
            _tool_list_transl_cache(runner, {"limit": "abc"})

    def test_limit_below_one_clamps_to_one(self):
        runner = _ListRunner(cache_files=_cache_files(5))
        result = _tool_list_transl_cache(runner, {"limit": 0})
        self.assertEqual(result["returned"], 1)


class ListInputFilesTests(unittest.TestCase):
    def test_default_limit_samples_but_total_stays_complete(self):
        runner = _ListRunner(input_files=_input_files(150, sentences=7))

        result = _tool_list_input_files(runner, {})

        self.assertEqual(result["count"], 150)
        self.assertEqual(result["returned"], 100)
        self.assertTrue(result["sampled"])
        # 句数合计是全量口径：少显示几行不该让工作量估计变小
        self.assertEqual(result["sentences_total"], 150 * 7)
        self.assertIn("均匀采样", result["note"])

    def test_grep_narrows_the_list_and_the_total(self):
        runner = _ListRunner(input_files=_input_files(150, sentences=7))

        result = _tool_list_input_files(runner, {"grep": "sc_1"})

        self.assertEqual(result["count"], 50)  # sc_100~sc_149
        self.assertEqual(result["sentences_total"], 50 * 7)

    def test_unparsable_sentences_stay_null_and_out_of_the_total(self):
        runner = _ListRunner(
            input_files=[
                {"name": "a.json", "is_file": True, "size": 1, "sentences": 4},
                {"name": "b.json", "is_file": True, "size": 1, "sentences": None},
            ]
        )

        result = _tool_list_input_files(runner, {})

        by_name = {f["name"]: f for f in result["input_files"]}
        self.assertIsNone(by_name["b.json"]["sentences"])
        self.assertEqual(result["sentences_total"], 4)

    def test_directories_are_skipped(self):
        runner = _ListRunner(
            input_files=[
                {"name": "sub", "is_file": False, "size": 0},
                {"name": "a.json", "is_file": True, "size": 1, "sentences": 2},
            ]
        )

        result = _tool_list_input_files(runner, {})

        self.assertEqual([f["name"] for f in result["input_files"]], ["a.json"])
        self.assertEqual(result["count"], 1)


class LockedInputListingTests(unittest.TestCase):
    """子代理的"文件锁定"包装：先按锁定名单过滤、再采样——顺序反了就会误报"文件不在清单里"。"""

    def test_locked_file_is_found_even_though_sampling_would_skip_it(self):
        runner = _ListRunner(input_files=_input_files(500))
        # 500 个文件默认采样 100 个，这个在尾部、采样也摇不到它
        out = rt._lock_input_listing(("sc_480.txt.json",))(runner, {})

        self.assertEqual([f["name"] for f in out["input_files"]], ["sc_480.txt.json"])
        self.assertNotIn("都不在原文清单里", out["note"])  # 它在清单里，别说反话
        self.assertIn("sc_480.txt.json", out["note"])

    def test_long_locked_subset_is_sampled_within_the_subset(self):
        runner = _ListRunner(input_files=_input_files(50))
        locked = tuple(f"sc_{i:03d}.txt.json" for i in range(50))

        out = rt._lock_input_listing(locked)(runner, {"limit": 5})

        self.assertEqual(out["count"], 50)  # 职责范围是 50 个
        self.assertEqual(out["returned"], 5)
        self.assertTrue(all(f["name"] in locked for f in out["input_files"]))
        self.assertIn("本次只派你看这 50 个文件", out["note"])

    def test_unknown_locked_names_fall_back_to_the_full_list(self):
        runner = _ListRunner(input_files=_input_files(3))

        out = rt._lock_input_listing(("nope.json",))(runner, {})

        self.assertEqual(len(out["input_files"]), 3)
        self.assertIn("都不在原文清单里", out["note"])

    def test_grep_still_narrows_the_locked_subset(self):
        runner = _ListRunner(input_files=_input_files(20))
        locked = tuple(f"sc_{i:03d}.txt.json" for i in range(20))

        out = rt._lock_input_listing(locked)(runner, {"grep": "sc_01"})

        self.assertEqual([f["name"] for f in out["input_files"]], [f"sc_01{i}.txt.json" for i in range(10)])


class ListOrderTests(unittest.TestCase):
    """order：怎么从长清单里挑出 limit 个。默认 even；其余模式各有明确用途。"""

    def _sized(self, count: int = 6) -> list[dict]:
        # f0 最大、往后递减（size 互不相同，排序断言才不含糊）
        return [{"name": f"f{i}.json", "size": (count - i) * 10} for i in range(count)]

    def test_size_desc_lists_the_largest_first(self):
        runner = _ListRunner(cache_files=self._sized(6))
        out = _tool_list_transl_cache(runner, {"order": "size_desc", "limit": 3})

        self.assertEqual([f["name"] for f in out["cache_files"]], ["f0.json", "f1.json", "f2.json"])
        self.assertTrue(out["sampled"])
        self.assertIn("从大到小", out["note"])

    def test_size_asc_lists_the_smallest_first(self):
        runner = _ListRunner(cache_files=self._sized(6))
        out = _tool_list_transl_cache(runner, {"order": "size_asc", "limit": 3})

        self.assertEqual([f["name"] for f in out["cache_files"]], ["f5.json", "f4.json", "f3.json"])
        self.assertIn("从小到大", out["note"])

    def test_size_order_applies_even_without_truncation(self):
        """用不着截断时也要按 size 排：'从大到小'说的是顺序，不只是挑哪些。"""
        runner = _ListRunner(cache_files=self._sized(3))
        out = _tool_list_transl_cache(runner, {"order": "size_desc"})

        self.assertEqual([f["name"] for f in out["cache_files"]], ["f0.json", "f1.json", "f2.json"])
        self.assertFalse(out["sampled"])

    def test_name_order_takes_the_first_n(self):
        runner = _ListRunner(cache_files=_cache_files(250))
        out = _tool_list_transl_cache(runner, {"order": "name"})

        self.assertEqual(out["returned"], 100)
        self.assertEqual(out["cache_files"][0]["name"], "sc_000.json")
        self.assertNotIn("sc_249.json", [f["name"] for f in out["cache_files"]])
        self.assertIn("文件名顺序", out["note"])

    def test_random_returns_a_unique_subset_of_the_right_size(self):
        runner = _ListRunner(cache_files=_cache_files(250))
        out = _tool_list_transl_cache(runner, {"order": "random"})

        names = [f["name"] for f in out["cache_files"]]
        self.assertEqual(len(names), 100)
        self.assertEqual(len(set(names)), 100)  # 随机采样也不重复
        self.assertTrue(all(n.startswith("sc_") for n in names))
        self.assertIn("随机采样", out["note"])
        # 挑中哪些是随机的，但清单本身仍按文件名排好，读起来是有序的
        self.assertEqual(names, sorted(names))

    def test_random_without_truncation_is_just_the_whole_list(self):
        runner = _ListRunner(cache_files=_cache_files(3))
        out = _tool_list_transl_cache(runner, {"order": "random"})
        self.assertEqual(out["returned"], 3)

    def test_unknown_order_is_rejected(self):
        runner = _ListRunner(cache_files=_cache_files(2))
        with self.assertRaises(AgentToolError):
            _tool_list_transl_cache(runner, {"order": "biggest"})

    def test_order_applies_to_input_files_too(self):
        runner = _ListRunner(
            input_files=[
                {"name": f"f{i}.json", "is_file": True, "size": (10 - i) * 100, "sentences": i + 1}
                for i in range(10)
            ]
        )

        out = _tool_list_input_files(runner, {"order": "size_asc", "limit": 4})

        self.assertEqual([f["name"] for f in out["input_files"]], [f"f{i}.json" for i in range(9, 5, -1)])
        self.assertEqual(out["count"], 10)
        self.assertEqual(out["sentences_total"], sum(range(1, 11)))  # 合计不受截取影响


class SelectListItemsTests(unittest.TestCase):
    """_select_list_items 的边界（两个清单工具共用的那一份实现）。"""

    def test_empty_list_stays_empty_in_every_mode(self):
        for order in rt.LIST_ORDER_MODES:
            self.assertEqual(rt._select_list_items([], 5, order), [], order)

    def test_size_ties_break_by_name(self):
        items = [{"name": "b.json", "size": 5}, {"name": "a.json", "size": 5}]
        self.assertEqual(
            [i["name"] for i in rt._select_list_items(items, 10, "size_desc")],
            ["a.json", "b.json"],  # 同尺寸按名字，输出稳定可复现
        )

    def test_size_missing_counts_as_zero(self):
        items = [{"name": "a.json"}, {"name": "b.json", "size": 1}]
        self.assertEqual(
            [i["name"] for i in rt._select_list_items(items, 10, "size_desc")],
            ["b.json", "a.json"],
        )

    def test_even_mode_keeps_first_and_last(self):
        items = list(range(250))
        picked = rt._select_list_items(items, 100, "even")
        self.assertEqual((picked[0], picked[-1]), (0, 249))


class ListSchemaTests(unittest.TestCase):
    """schema 里得真有这些入参，否则模型不会传。"""

    def test_both_list_tools_advertise_grep_limit_order(self):
        for name in ("list_transl_cache", "list_input_files"):
            schema = next(t for t in rt.AGENT_TOOLS if t["function"]["name"] == name)
            props = schema["function"]["parameters"]["properties"]
            self.assertEqual(sorted(props), ["grep", "limit", "order"], name)
            self.assertEqual(
                props["order"]["enum"], ["even", "name", "random", "size_desc", "size_asc"], name
            )
            self.assertIn("均匀采样", schema["function"]["description"], name)

    def test_default_limit_constant_is_100(self):
        self.assertEqual(rt.LIST_ITEMS_DEFAULT_LIMIT, 100)


if __name__ == "__main__":
    unittest.main()
