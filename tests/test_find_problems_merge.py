"""find_problems 合并问题的回归：已有 problem 必须带 ", " 分隔追加并去重。

失败批次会先把「翻译失败」写进 tran.problem（BaseTranslate._merge_problem_message），
随后 find_problems 再追加其它问题。旧的 `tran.problem += ", ".join(...)` 与已有内容之间
没有分隔符，会黏成「翻译失败比日文长：…」；「翻译失败」也会被加两次。
"""

import unittest

from GalTransl.CSentense import CSentense
from GalTransl.Problem import CProblemType, find_problems


class FakeProblemConfig:
    target_lang = "zh-cn"

    def getProblemAnalyzeArinashiDict(self):
        return {}

    def getProblemAnalyzeConfig(self, key):
        if key == "problemList":
            return [CProblemType["比日文长严格"]]
        return []

    def getlbSymbol(self):
        return "auto"


SRC = "日本語の長い文章です。\nもっと続きますよ。"
# (Failed) 前缀会同时触发「翻译失败」，长中文会触发「比日文长严格」
DST_FAILED = "(Failed)这是一句非常长的中文翻译完全没有换行符来分割整句话。"


def _tran(problem: str = "") -> CSentense:
    tran = CSentense(SRC, speaker="", index=0)
    tran.post_src = SRC
    tran.pre_dst = DST_FAILED
    tran.post_dst = DST_FAILED
    tran.problem = problem
    return tran


class FindProblemsMergeTests(unittest.TestCase):
    def test_appends_with_separator_when_problem_already_present(self):
        tran = _tran("翻译失败")
        find_problems([tran], FakeProblemConfig(), None)

        parts = [p.strip() for p in tran.problem.split(",")]
        self.assertIn("翻译失败", parts)  # 不再黏在别的项上
        self.assertEqual(parts.count("翻译失败"), 1)  # 不重复
        self.assertTrue(any(p.startswith("比日文长：") for p in parts), tran.problem)

    def test_existing_problem_is_kept_verbatim_and_prefixed_by_a_separator(self):
        tran = _tran("翻译失败")
        find_problems([tran], FakeProblemConfig(), None)
        self.assertTrue(tran.problem.startswith("翻译失败, "), tran.problem)

    def test_failed_marker_is_not_added_twice_from_scratch(self):
        tran = _tran("")
        find_problems([tran], FakeProblemConfig(), None)
        self.assertEqual(tran.problem.count("翻译失败"), 1, tran.problem)
        self.assertIn("比日文长：", tran.problem)

    def test_calling_twice_is_idempotent(self):
        tran = _tran("")
        find_problems([tran], FakeProblemConfig(), None)
        once = tran.problem
        find_problems([tran], FakeProblemConfig(), None)
        self.assertEqual(tran.problem, once)


if __name__ == "__main__":
    unittest.main()
