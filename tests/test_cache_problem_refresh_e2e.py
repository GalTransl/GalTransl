"""更贴近真实流程的 problem 刷新回归测试。

场景：翻译完成后的一次"空跑"（所有条目都命中缓存）应重新计算每个
tran 的 problem 并写回 .json。
"""

import os
import tempfile
import unittest

import orjson

from GalTransl.Cache import (
    get_transCache_from_json,
    save_transCache_to_json,
)
from GalTransl.CSentense import CSentense
from GalTransl.Problem import find_problems


class _FakeDic:
    def do_replace(self, text, tran):
        return text


class FakeProblemConfig:
    target_lang = "zh-cn"
    select_translator = "gpt4"

    def getProblemAnalyzeArinashiDict(self):
        return {}

    def getProblemAnalyzeConfig(self, key):
        if key == "problemList":
            return ["残留日文"]
        return []

    def getlbSymbol(self):
        return "auto"

    def getFilePlugin(self):
        return ""

    def getDictCfgSection(self, key=None):
        if key == "usePreDictInName":
            return False
        return {}


async def _run_refresh_cycle(cache_file_path: str, pre_src: str, post_src: str):
    """模拟 doLLMTranslSingleChunk + postprocess_results 的缓存刷新步骤。"""
    from GalTransl.Frontend.LLMTranslate import (
        preprocess_trans_list,
        postprocess_trans_list,
    )

    # 构造 chunk 的 trans_list（就 1 句）
    tran = CSentense(pre_src, speaker="", index=0)
    tran.post_src = pre_src
    trans_list = [tran]

    config = FakeProblemConfig()
    pre_dic = _FakeDic()
    post_dic = _FakeDic()

    preprocess_trans_list(trans_list, config, pre_dic, None)

    hit, unhit = await get_transCache_from_json(trans_list, cache_file_path)
    assert len(hit) == 1 and len(unhit) == 0, (hit, unhit)

    postprocess_trans_list(trans_list, config, post_dic, None)

    find_problems(trans_list, config, None)
    await save_transCache_to_json(trans_list, cache_file_path, post_save=True)


class CacheProblemRefreshE2ETests(unittest.IsolatedAsyncioTestCase):
    async def test_problem_cleared_when_user_fixes_pre_dst_via_translation_pass(self) -> None:
        with tempfile.TemporaryDirectory() as cache_dir:
            cache_file_path = os.path.join(cache_dir, "demo.json")

            pre_src = "おはよう"
            # 用户在缓存里把译文从"早上好おはよう"改成"早上好"以消除残留日文
            snapshot = [
                {
                    "index": 0,
                    "name": "",
                    "pre_src": pre_src,
                    "post_src": pre_src,
                    "pre_dst": "早上好",
                    "proofread_dst": "",
                    "problem": "残留日文：おはよう",
                    "trans_by": "model",
                    "proofread_by": "",
                    "post_dst_preview": "早上好",
                }
            ]
            with open(cache_file_path, "wb") as f:
                f.write(orjson.dumps(snapshot, option=orjson.OPT_INDENT_2))

            await _run_refresh_cycle(cache_file_path, pre_src, pre_src)

            with open(cache_file_path, "rb") as f:
                refreshed = orjson.loads(f.read())

            self.assertEqual(len(refreshed), 1)
            self.assertNotIn(
                "problem",
                refreshed[0],
                msg=f"problem 字段未被刷新/清理: {refreshed[0]}",
            )


if __name__ == "__main__":
    unittest.main()
