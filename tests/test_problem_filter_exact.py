"""问题过滤是「按问题项精准匹配」：只丢掉与某个 key 逐字相同的那一项。

不做子串匹配 —— 因此写大类名（如「残留日文」）不会滤掉「残留日文：おはよう」，
也就无法用一个词滤掉整个大类。
"""

import unittest

from GalTransl.ProblemFilter import filter_problem_text, normalize_problem_filter_keys


class FilterProblemTextExactTests(unittest.TestCase):
    def test_exact_item_is_removed(self):
        text = "残留日文：おはよう, 缺控制符：<A>"
        self.assertEqual(filter_problem_text(text, ["残留日文：おはよう"]), "缺控制符：<A>")

    def test_category_name_does_not_match(self):
        text = "残留日文：おはよう, 残留日文：ありがとう"
        self.assertEqual(filter_problem_text(text, ["残留日文"]), text)

    def test_substring_does_not_match(self):
        text = "本无括号, 本无冒号"
        self.assertEqual(filter_problem_text(text, ["本无"]), text)
        self.assertEqual(filter_problem_text(text, ["括号"]), text)

    def test_bare_item_matches_itself(self):
        text = "丢失换行, 独白男他"
        self.assertEqual(filter_problem_text(text, ["丢失换行"]), "独白男他")

    def test_only_matching_items_are_dropped(self):
        text = "本无括号, 本有括号"
        self.assertEqual(filter_problem_text(text, ["本无括号"]), "本有括号")

    def test_key_whitespace_is_trimmed_and_empty_keys_ignored(self):
        text = "残留日文：おはよう, 缺控制符：<A>"
        self.assertEqual(filter_problem_text(text, ["  残留日文：おはよう  "]), "缺控制符：<A>")
        self.assertEqual(filter_problem_text(text, []), text)
        self.assertEqual(filter_problem_text(text, ["", "   "]), text)

    def test_empty_or_missing_problem(self):
        self.assertEqual(filter_problem_text("", ["a"]), "")
        self.assertEqual(filter_problem_text(None, ["a"]), "")

    def test_normalize_keys_trims_and_dedupes(self):
        self.assertEqual(normalize_problem_filter_keys([" a ", "a", "", "b"]), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
