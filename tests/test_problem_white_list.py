"""问题白名单（ProblemWhiteList）的条目解析与匹配。

白名单条目是「缓存文件名:index」：单个 index 或闭区间；.append.jsonl 与对应 .json
视为同一份文件；不合法的条目在编译索引时忽略（配置可能被手改）。
"""

import unittest

from GalTransl.ProblemWhiteList import (
    build_problem_white_list_index,
    canonical_cache_name,
    is_problem_whitelisted,
    normalize_problem_white_list,
    parse_problem_white_list_entry,
)


class NormalizeTests(unittest.TestCase):
    def test_string_splits_lines_and_dedupes(self):
        self.assertEqual(
            normalize_problem_white_list("a.json:1\n b.json:2 \na.json:1\n\n"),
            ["a.json:1", "b.json:2"],
        )

    def test_list_and_non_list(self):
        self.assertEqual(normalize_problem_white_list(["a.json:1", " ", "a.json:1"]), ["a.json:1"])
        self.assertEqual(normalize_problem_white_list(None), [])
        self.assertEqual(normalize_problem_white_list(123), [])


class ParseTests(unittest.TestCase):
    def test_single_index(self):
        self.assertEqual(parse_problem_white_list_entry("a.json:12"), ("a.json", frozenset({12})))

    def test_range_and_reversed_range(self):
        self.assertEqual(parse_problem_white_list_entry("a.json:3-5"), ("a.json", frozenset({3, 4, 5})))
        self.assertEqual(parse_problem_white_list_entry("a.json:5-3"), ("a.json", frozenset({3, 4, 5})))

    def test_append_suffix_is_canonicalized(self):
        self.assertEqual(parse_problem_white_list_entry("a.json.append.jsonl:7"), ("a.json", frozenset({7})))

    def test_invalid_entries(self):
        for spec in ("a.json", ":12", "a.json:", "a.json:x", "a.json:3-x", ""):
            self.assertIsNone(parse_problem_white_list_entry(spec), spec)

    def test_canonical_cache_name(self):
        self.assertEqual(canonical_cache_name("a.json.append.jsonl"), "a.json")
        self.assertEqual(canonical_cache_name("a.json"), "a.json")
        self.assertEqual(canonical_cache_name(None), "")


class MatchTests(unittest.TestCase):
    def test_index_hit_and_miss(self):
        index = build_problem_white_list_index(["a.json:3-5", "b.json:9", "bad", "c.json:x"])
        self.assertTrue(is_problem_whitelisted(index, "a.json", 4))
        self.assertFalse(is_problem_whitelisted(index, "a.json", 6))
        self.assertTrue(is_problem_whitelisted(index, "b.json", 9))
        self.assertFalse(is_problem_whitelisted(index, "b.json", 3))
        self.assertFalse(is_problem_whitelisted(index, "c.json", 3))

    def test_append_file_matches_snapshot_entry(self):
        index = build_problem_white_list_index(["a.json:4"])
        self.assertTrue(is_problem_whitelisted(index, "a.json.append.jsonl", 4))

    def test_merges_same_file_specs(self):
        index = build_problem_white_list_index(["a.json:1", "a.json:1-2"])
        self.assertEqual(index["a.json"], frozenset({1, 2}))

    def test_bad_index_and_empty_map(self):
        self.assertFalse(is_problem_whitelisted({}, "a.json", 1))
        self.assertFalse(is_problem_whitelisted(build_problem_white_list_index(["a.json:1"]), "a.json", "x"))


if __name__ == "__main__":
    unittest.main()
