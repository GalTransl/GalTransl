"""回归测试：用户在缓存里改好译文后再启动翻译，`problem` 字段应被清空。

场景：文件翻译完成后，用户在 .json 缓存里手动把一句有问题的 pre_dst 改好，
然后重新启动翻译希望刷新缓存。预期：这次运行结束时快照中该句的 `problem`
字段应消失。
"""

import asyncio
import os
import tempfile
import unittest

import orjson

from GalTransl.Cache import (
    get_transCache_from_json,
    save_transCache_to_json,
)
from GalTransl.CSentense import CSentense


class FakeProblemConfig:
    """最小化的 projectConfig，用于驱动 find_problems。"""

    target_lang = "zh-cn"

    def getProblemAnalyzeArinashiDict(self):
        return {}

    def getProblemAnalyzeConfig(self, key):
        if key == "problemList":
            return ["残留日文"]
        return []

    def getlbSymbol(self):
        return "auto"


class CacheProblemRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_problem_cleared_when_user_fixes_pre_dst(self) -> None:
        from GalTransl.Problem import find_problems

        with tempfile.TemporaryDirectory() as cache_dir:
            cache_file_path = os.path.join(cache_dir, "demo.json")

            pre_src = "おはよう"
            post_src = pre_src
            # 用户已经在缓存里把译文改好（无残留日文），但 problem 字段还在
            snapshot = [
                {
                    "index": 0,
                    "name": "",
                    "pre_src": pre_src,
                    "post_src": post_src,
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

            # 构造一个与缓存 key 匹配的 CSentense
            tran = CSentense(pre_src, speaker="", index=0)
            tran.post_src = post_src
            trans_list = [tran]

            hit, unhit = await get_transCache_from_json(
                trans_list,
                cache_file_path,
            )
            self.assertEqual(len(hit), 1)
            self.assertEqual(len(unhit), 0)
            # 缓存命中后 pre_dst 应为用户修好的译文
            self.assertEqual(tran.pre_dst, "早上好")

            # 模拟 postprocess_results 的核心步骤
            config = FakeProblemConfig()
            find_problems(trans_list, config, None)

            # 修好之后 find_problems 应不再产生任何 problem
            self.assertEqual(tran.problem, "")

            await save_transCache_to_json(
                trans_list, cache_file_path, post_save=True
            )

            with open(cache_file_path, "rb") as f:
                refreshed = orjson.loads(f.read())

            self.assertEqual(len(refreshed), 1)
            # 预期：problem 字段应被刷新/移除
            self.assertNotIn(
                "problem",
                refreshed[0],
                msg=f"problem 字段未被刷新: {refreshed[0]}",
            )


if __name__ == "__main__":
    unittest.main()
