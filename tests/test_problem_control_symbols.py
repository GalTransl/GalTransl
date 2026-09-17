"""缺控制符检测：按「子串包含」判断控制符是否保留，而不是 token 精确相等。

`extract_control_substrings` 是按 ASCII 连续段切词的：源文 `[石浦城跡/いしうらじょうあと]`
（括号内是日文，非 ASCII）切出 ['[', '/', ']']，译文 `[石浦城迹/shipuchengji]`（括号内是
罗马字，`]` 又在允许字符集里）会被并成一个 token `/shipuchengji]` —— 精确比较就会误报
「缺控制符：/ ]」。真正的控制符丢了，子串包含同样判得出来。
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
            return [CProblemType["缺控制符"]]
        return []

    def getlbSymbol(self):
        return "auto"


def _tran(src: str, dst: str) -> CSentense:
    tran = CSentense(src, speaker="", index=0)
    tran.post_src = src
    tran.pre_dst = dst
    tran.post_dst = dst
    return tran


def _run(src: str, dst: str) -> str:
    tran = _tran(src, dst)
    find_problems([tran], FakeProblemConfig(), None)
    return tran.problem


class ControlSymbolTests(unittest.TestCase):
    def test_ruby_annotation_with_romaji_reading_is_not_flagged(self):
        # 括号内读音由假名改写成罗马字：不算丢控制符
        problem = _run(
            "[石浦城跡/いしうらじょうあと]のバス停まで行きたいんですけど",
            "我想去[石浦城迹/shipuchengji]的巴士站",
        )
        self.assertEqual(problem, "")

    def test_ascii_run_in_translation_absorbs_punctuation(self):
        problem = _run("三人でながれ[茶屋街/ちゃやがい]へと向かう", "三人向流[茶屋街/chayagai]走去")
        self.assertEqual(problem, "")

    def test_preserved_control_tag_is_not_flagged(self):
        problem = _run("<color=red>こんにちは</color>", "<color=red>你好</color>")
        self.assertEqual(problem, "")

    def test_dropped_control_tag_is_still_flagged(self):
        problem = _run("<color=red>こんにちは</color>", "你好")
        self.assertIn("缺控制符", problem)
        self.assertIn("<color=red>", problem)
        self.assertIn("</color>", problem)

    def test_dropped_bracket_annotation_is_still_flagged(self):
        # 括号注解整体被丢掉：仅剩控制符 [ / ] 全不在译文里
        problem = _run("[茶屋街/ちゃやがい]へ", "去茶屋街")
        self.assertIn("缺控制符", problem)


if __name__ == "__main__":
    unittest.main()
