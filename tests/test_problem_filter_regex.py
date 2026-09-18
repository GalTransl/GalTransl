"""问题过滤是**正则匹配**：每个过滤项是一条正则，命中（re.search）的问题项丢掉。

不是逐字相等——`残留日文` 能命中 `残留日文：おはよう`（整类），`^缺控制符：` 能锚定开头。
想按字面过滤某条的原文写法，用 re.escape（界面/工具里"精确过滤某条"的入口会自动转义）。
"""

import re
import unittest

from GalTransl.ProblemFilter import (
    compile_problem_filter_patterns,
    filter_problem_text,
    summarize_problem_filter_hits,
)


class FilterProblemTextRegexTests(unittest.TestCase):
    def test_plain_pattern_matches_anywhere(self):
        text = "残留日文：おはよう, 缺控制符：<A>"
        self.assertEqual(filter_problem_text(text, ["残留日文"]), "缺控制符：<A>")

    def test_anchored_pattern(self):
        text = "残留日文：おはよう, 缺控制符：<A>"
        self.assertEqual(filter_problem_text(text, ["^残留日文："]), "缺控制符：<A>")
        self.assertEqual(filter_problem_text(text, ["^缺控制符"]), "残留日文：おはよう")

    def test_wildcard_pattern(self):
        text = "缺控制符：<A>, 缺控制符：<B>, 残留日文：x"
        self.assertEqual(filter_problem_text(text, [r"^缺控制符：<\w+>$"]), "残留日文：x")

    def test_only_matching_items_are_dropped(self):
        text = "本无括号, 本有括号"
        self.assertEqual(filter_problem_text(text, ["^本无"]), "本有括号")

    def test_invalid_regex_falls_back_to_literal(self):
        # "(" 不是合法正则 → 退回按字面匹配，仍能命中含 ( 的那一项
        text = "缺控制符：<(, 残留日文：x"
        self.assertEqual(filter_problem_text(text, ["("]), "残留日文：x")

    def test_escaped_literal_matches_exactly(self):
        text = "比日文长：1.5倍(10字符), 残留日文：x"
        pattern = re.escape("比日文长：1.5倍(10字符)")
        self.assertEqual(filter_problem_text(text, [pattern]), "残留日文：x")

    def test_unescaped_metachar_pattern_does_not_match_literal(self):
        # 反例：转义前的写法当正则会"吃掉"括号，反而匹配不到字面那一项
        text = "比日文长：1.5倍(10字符)"
        self.assertEqual(filter_problem_text(text, ["比日文长：1.5倍(10字符)"]), text)

    def test_empty_keys_and_text(self):
        text = "残留日文：x, 缺控制符：y"
        self.assertEqual(filter_problem_text(text, []), text)
        self.assertEqual(filter_problem_text(text, ["", "  "]), text)
        self.assertEqual(filter_problem_text("", ["a"]), "")
        self.assertEqual(filter_problem_text(None, ["a"]), "")


class CompilePatternsTests(unittest.TestCase):
    def test_dedupes_and_keeps_order(self):
        patterns = compile_problem_filter_patterns([" a ", "a", "", "b"])
        self.assertEqual([p.pattern for p in patterns], ["a", "b"])

    def test_string_input_splits_lines(self):
        patterns = compile_problem_filter_patterns("^残留日文：\n^缺控制符")
        self.assertEqual([p.pattern for p in patterns], ["^残留日文：", "^缺控制符"])


class SummarizeFilterHitsTests(unittest.TestCase):
    """每条过滤项各挡住了多少条问题（manage_problem_filter 的 list 显示用）。

    按**条目**计：一条问题多个项命中同一过滤项也只算一条；0 说明这条过滤项现在没用了。
    """

    def test_counts_entries_per_key(self):
        problems = [
            "残留日文：おはよう, 缺控制符：<A>",
            "残留日文：x",
            "标点错漏：。",
        ]

        summary = summarize_problem_filter_hits(problems, [r"^缺控制符：<\w+>$", "残留日文"])

        self.assertEqual(
            summary["filters"],
            [{"key": r"^缺控制符：<\w+>$", "problems": 1}, {"key": "残留日文", "problems": 2}],
        )
        self.assertEqual(summary["problem_entries"], 3)
        self.assertEqual(summary["visible_entries"], 1)  # 只剩「标点错漏：。」

    def test_same_entry_counts_for_every_matching_key(self):
        summary = summarize_problem_filter_hits(["缺控制符：<A>"], ["缺控制符", r"<\w+>"])

        self.assertEqual([f["problems"] for f in summary["filters"]], [1, 1])

    def test_partially_filtered_entry_stays_visible(self):
        # 只挡住其中一个问题项：清单里还看得到这条，visible 要算上
        summary = summarize_problem_filter_hits(["残留日文：x, 标点错漏：。"], ["残留日文"])

        self.assertEqual(summary["problem_entries"], 1)
        self.assertEqual(summary["visible_entries"], 1)

    def test_unused_key_counts_zero(self):
        summary = summarize_problem_filter_hits(["残留日文：x"], ["缺失.*标点"])

        self.assertEqual(summary["filters"], [{"key": "缺失.*标点", "problems": 0}])

    def test_invalid_regex_falls_back_to_literal(self):
        # 与 filter_problem_text 同一套：坏模式退回按字面匹配，统计不能因此空掉
        summary = summarize_problem_filter_hits(["缺控制符：<(, 残留日文：x"], ["("])

        self.assertEqual(summary["filters"], [{"key": "(", "problems": 1}])
        self.assertEqual(summary["problem_entries"], 1)
        self.assertEqual(summary["visible_entries"], 1)

    def test_empty_inputs(self):
        self.assertEqual(
            summarize_problem_filter_hits([], ["a"]),
            {"filters": [{"key": "a", "problems": 0}], "problem_entries": 0, "visible_entries": 0},
        )
        # 没有过滤项时一条都不挡：全部可见
        self.assertEqual(
            summarize_problem_filter_hits(["残留日文"], []),
            {"filters": [], "problem_entries": 1, "visible_entries": 1},
        )

    def test_visible_count_agrees_with_filter_problem_text(self):
        problems = ["残留日文：x, 标点错漏：。", "缺失标点：，", "残留日文：y"]
        keys = ["残留日文", "缺失.*标点"]

        summary = summarize_problem_filter_hits(problems, keys)

        visible = sum(1 for p in problems if filter_problem_text(p, keys))
        self.assertEqual(summary["visible_entries"], visible)


if __name__ == "__main__":
    unittest.main()
