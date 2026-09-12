import unittest

from GalTransl.CSentense import CSentense
from GalTransl.Problem import find_problems


class FakeProblemConfig:
    target_lang = "zh-cn"

    def __init__(self, problem_list=None):
        # 使用 BASE 已存在的检测项，避免依赖 feature-1（单句过长）的枚举成员
        self._problem_list = problem_list or ["比日文长严格"]

    def getProblemAnalyzeArinashiDict(self):
        return {}

    def getProblemAnalyzeConfig(self, key):
        if key == "problemList":
            from GalTransl.Problem import CProblemType
            return [CProblemType[name] for name in self._problem_list]
        return []

    def getlbSymbol(self):
        return "auto"


def rebuild_entries(entries, config):
    trans_list = []
    for e in entries:
        speaker = e.get("name", "")
        if isinstance(speaker, list):
            speaker = "/".join(speaker)
        pre_src = e.get("pre_src", "") or e.get("pre_jp", "")
        post_src = e.get("post_src", "") or e.get("post_jp", "")
        pre_dst = e.get("pre_dst", "") or e.get("pre_zh", "")
        proofread_dst = e.get("proofread_dst", "") or e.get("proofread_zh", "")
        if post_src == "":
            continue
        s = CSentense(pre_src, speaker if speaker else "", e.get("index", 0))
        s.post_src = pre_src
        s.pre_dst = pre_dst
        s.proofread_zh = proofread_dst
        s.post_dst = proofread_dst if proofread_dst else pre_dst
        s.trans_by = e.get("trans_by", "")
        s.proofread_by = e.get("proofread_by", "")
        s.trans_conf = e.get("trans_conf", 0)
        s.doub_content = e.get("doub_content", "")
        s.unknown_proper_noun = e.get("unknown_proper_noun", "")
        s.skip_check = bool(e.get("skip_check", False))
        trans_list.append(s)

    for i, s in enumerate(trans_list):
        if i > 0:
            s.prev_tran = trans_list[i - 1]
        if i < len(trans_list) - 1:
            s.next_tran = trans_list[i + 1]

    if trans_list:
        find_problems(trans_list, config, None)

    idx = 0
    for e in entries:
        post_src_val = e.get("post_src", "") or e.get("post_jp", "")
        if post_src_val == "":
            continue
        if idx < len(trans_list):
            tran = trans_list[idx]
            if tran.problem:
                e["problem"] = tran.problem
            elif "problem" in e:
                del e["problem"]
            e["post_dst_preview"] = tran.post_dst
            idx += 1

    return entries


SRC_WITH_BREAK = "日本語の長い文章です。\nもっと続きますよ。"
DST_LONG_NO_BREAK = "这是一句非常长的中文翻译完全没有换行符来分割整句话。"


class TestCacheSaveRespectsSkipCheck(unittest.TestCase):
    def _make_entry(self, skip_check, problem=""):
        return {
            "index": 0,
            "name": "",
            "pre_src": SRC_WITH_BREAK,
            "post_src": SRC_WITH_BREAK,
            "pre_dst": DST_LONG_NO_BREAK,
            "proofread_dst": DST_LONG_NO_BREAK,
            "skip_check": skip_check,
            "problem": problem,
        }

    def test_skip_check_prevents_rebuild_flagging(self) -> None:
        entry = self._make_entry(skip_check=True, problem="")
        entries = rebuild_entries([entry], FakeProblemConfig())
        self.assertNotIn("problem", entries[0])
        self.assertTrue(entries[0]["skip_check"])

    def test_no_skip_check_still_flagged(self) -> None:
        entry = self._make_entry(skip_check=False, problem="")
        entries = rebuild_entries([entry], FakeProblemConfig())
        self.assertIn("problem", entries[0])
        self.assertIn("比日文长", entries[0]["problem"])

    def test_skip_check_clears_existing_problem(self) -> None:
        entry = self._make_entry(skip_check=True, problem="残留日文")
        entries = rebuild_entries([entry], FakeProblemConfig())
        self.assertNotIn("problem", entries[0])

    def test_build_cache_obj_only_writes_skip_check_when_true(self) -> None:
        from GalTransl.Cache import _build_cache_obj

        tran = CSentense(SRC_WITH_BREAK, speaker="", index=0)
        tran.post_src = SRC_WITH_BREAK
        tran.pre_dst = DST_LONG_NO_BREAK
        tran.post_dst = DST_LONG_NO_BREAK
        self.assertNotIn("skip_check", _build_cache_obj(tran))
        tran.skip_check = True
        self.assertIs(_build_cache_obj(tran).get("skip_check"), True)

    def test_read_cache_normalizes_skip_check_to_bool(self) -> None:
        import asyncio
        import json
        import os
        import tempfile

        from GalTransl.Cache import get_transCache_from_json

        # bool() 语义：非空字符串恒为真（"false" 亦为 True），0 / 空串 / None 为假。
        # 本用例锁定读出的值一定是真正的 bool，而不是缓存里存的原始类型。
        for raw_value, expected in [("false", True), ("true", True), (1, True), (0, False)]:
            with tempfile.TemporaryDirectory() as tmp_dir:
                cache_path = os.path.join(tmp_dir, "cache.json")
                entry = {
                    "index": 0,
                    "name": "",
                    "pre_src": SRC_WITH_BREAK,
                    "post_src": SRC_WITH_BREAK,
                    "pre_dst": DST_LONG_NO_BREAK,
                    "post_dst": DST_LONG_NO_BREAK,
                    "trans_by": "unit-test",
                    "skip_check": raw_value,
                }
                with open(cache_path, "w", encoding="utf8") as f:
                    json.dump([entry], f)
                tran = CSentense(SRC_WITH_BREAK, speaker="", index=0)
                tran.post_src = SRC_WITH_BREAK
                asyncio.run(get_transCache_from_json([tran], cache_path))
            # trans_by 被读到说明缓存确实命中，否则下面的断言是假通过
            self.assertEqual(tran.trans_by, "unit-test", f"缓存未命中，用例 {raw_value!r} 无效")
            self.assertIs(tran.skip_check, expected, f"raw={raw_value!r}")


if __name__ == "__main__":
    unittest.main()
