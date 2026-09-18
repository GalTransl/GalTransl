"""重建（rebuilda/rebuildr）遇到未命中缓存时，报错要说清"哪几句、为什么"。

原来只有一句 `xxx 缓存不完整，无法重构`：用户和 Agent 都不知道该动哪里；而最常见的一种
（改过译前字典 → post_src 变、整批缓存过期）还会被误读成"这个文件还没翻"。

这里锁住三件事：
1. 原因码由 Cache.get_transCache_from_json 写在句子上（tran.cache_miss_reason）；
2. 报错按原因分组，每组带句数与例子 index（能直接拿去 read/patch_transl_cache 定位）；
3. 全命中时不报错、原样返回。
"""

import asyncio
import json
import os
import tempfile
import unittest

from GalTransl.Backend.RebuildTranslate import CRebuildTranslate
from GalTransl.Cache import (
    MISS_KEY_NOT_FOUND,
    MISS_POST_SRC_CHANGED,
    MISS_PRE_DST_EMPTY,
    get_transCache_from_json,
)
from GalTransl.CSentense import CSentense


def _tran(index: int, pre_src: str, post_src: str | None = None) -> CSentense:
    tran = CSentense(pre_src=pre_src, speaker="", index=index)
    if post_src is not None:
        tran.post_src = post_src
    return tran


def _link(trans_list: list[CSentense]) -> list[CSentense]:
    """把句子串成链表（真实流程由 CSplitter 做）：缓存键含上下句，不连就对不上。"""
    for i, tran in enumerate(trans_list):
        tran.prev_tran = trans_list[i - 1] if i > 0 else None
        tran.next_tran = trans_list[i + 1] if i + 1 < len(trans_list) else None
    return trans_list


def _cache_obj(pre_src: str, pre_dst: str = "你好", post_src: str | None = None) -> dict:
    """一条缓存记录（键 = name + pre_src + 上下句）。

    proofread_dst 必须写：缺这个字段时 Cache 里那句 `no_proofread` 判不出来，
    post_src / pre_dst 的检查会被整段跳过——真实写出的缓存一直带它。
    """
    return {
        "name": "",
        "pre_src": pre_src,
        "post_src": pre_src if post_src is None else post_src,
        "pre_dst": pre_dst,
        "proofread_dst": "",
        "proofread_by": "",
    }


class _RebuildCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="rebuild-report-")
        self.cache_path = os.path.join(self.dir, "sc_2_st01.txt.json")

    def _write_cache(self, entries: list[dict]) -> None:
        with open(self.cache_path, "w", encoding="utf8") as f:
            json.dump(entries, f, ensure_ascii=False)

    def _rebuild(self, trans_list, filename: str = "sc_2_st01.txt.json"):
        """跑一遍"查缓存 → 重建"（与 LLMTranslate.translate_file 同序），返回 (hit, error)。"""
        hit, unhit = asyncio.run(
            get_transCache_from_json(trans_list, self.cache_path, eng_type="rebuilda")
        )
        engine = CRebuildTranslate(None, "rebuilda")
        try:
            out = asyncio.run(
                engine.batch_translate(
                    filename,
                    self.cache_path,
                    trans_list,
                    1,
                    translist_hit=hit,
                    translist_unhit=unhit,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 这里就是要看报错内容
            return None, str(exc)
        return out, None


class IncompleteReportTests(_RebuildCase):
    def test_missing_entries_are_counted_and_located(self) -> None:
        """缓存里一条都没有：按句数汇总，并把每句的 index 列出来。"""
        trans_list = _link([_tran(1, "こんにちは"), _tran(2, "おはよう")])
        self._write_cache([])

        out, error = self._rebuild(trans_list)

        self.assertIsNone(out)
        self.assertIn("sc_2_st01.txt.json 缓存不完整，无法重构", error)
        self.assertIn("共 2 句，2 句没命中缓存", error)
        self.assertIn("缓存里没有这一条", error)
        self.assertIn("#1「こんにちは」", error)
        self.assertIn("#2「おはよう」", error)
        # 下一步也要给出来，别只报错不给出路
        self.assertIn("patch_transl_cache", error)
        self.assertIn("delete_transl_cache", error)
        self.assertEqual(trans_list[0].cache_miss_reason, MISS_KEY_NOT_FOUND)

    def test_changed_post_src_names_the_pre_dictionary(self) -> None:
        """按键命中、但 post_src 对不上（译前字典改过）要指名道姓，别混进"没有这一条"。"""
        tran = _tran(1, "こんにちは", post_src="こんにちは。")
        self._write_cache([_cache_obj("こんにちは", post_src="こんにちは")])

        _, error = self._rebuild([tran])

        self.assertEqual(tran.cache_miss_reason, MISS_POST_SRC_CHANGED)
        self.assertIn("译前字典", error)
        self.assertIn("共 1 句，1 句没命中缓存", error)

    def test_empty_cached_translation_is_named(self) -> None:
        """缓存里译文是空的：说明是上次没翻成功，而不是原文变了。"""
        tran = _tran(1, "こんにちは")
        self._write_cache([_cache_obj("こんにちは", pre_dst="")])

        _, error = self._rebuild([tran])

        self.assertEqual(tran.cache_miss_reason, MISS_PRE_DST_EMPTY)
        self.assertIn("译文是空的", error)

    def test_mixed_reasons_are_grouped_separately(self) -> None:
        """一句命中、两句各因不同原因未命中：分组各自计数，例子挂在各自那组下。"""
        trans_list = _link(
            [
                _tran(1, "一句目"),
                _tran(2, "二句目"),
                _tran(3, "三句目", post_src="三句目。"),
            ]
        )
        self._write_cache(
            [
                _cache_obj("一句目", pre_dst="第一句"),
                _cache_obj("二句目", pre_dst=""),  # 译文空 → 未命中
                _cache_obj("三句目", post_src="三句目"),  # post_src 变 → 未命中
            ]
        )

        _, error = self._rebuild(trans_list)

        self.assertIn("共 3 句，2 句没命中缓存", error)
        self.assertIn("- 1 句：缓存里这条的译文是空的", error)
        self.assertIn("- 1 句：原文经译前字典替换后与缓存里记录的不一致", error)
        # 命中那句不该出现在例子或原因里
        self.assertNotIn("#1", error)
        self.assertIn("#2", error)
        self.assertIn("#3", error)

    def test_all_hit_returns_without_error(self) -> None:
        """全命中：原样返回、不报错（重建本来就该只干这件事）。"""
        trans_list = [_tran(1, "こんにちは")]
        self._write_cache([_cache_obj("こんにちは", pre_dst="你好")])

        out, error = self._rebuild(trans_list)

        self.assertIsNone(error)
        self.assertEqual(len(out), 1)
        self.assertEqual(trans_list[0].pre_dst, "你好")
        self.assertEqual(trans_list[0].cache_miss_reason, "")

    def test_hit_clears_a_stale_reason(self) -> None:
        """复用同一个句子对象再查一次并命中时，上一轮的原因码要清掉（否则会误报）。"""
        tran = _tran(1, "こんにちは")
        self._write_cache([])
        self._rebuild([tran])
        self.assertEqual(tran.cache_miss_reason, MISS_KEY_NOT_FOUND)

        self._write_cache([_cache_obj("こんにちは", pre_dst="你好")])
        _, error = self._rebuild([tran])

        self.assertIsNone(error)
        self.assertEqual(tran.cache_miss_reason, "")

    def test_examples_are_capped(self) -> None:
        """例子最多列 3 条：句数已在分组标题里，例子只是给个把手。"""
        trans_list = _link([_tran(i, f"第{i}句") for i in range(1, 7)])
        self._write_cache([])

        _, error = self._rebuild(trans_list)

        self.assertIn("- 6 句：", error)
        self.assertIn("#1", error)
        self.assertIn("#3", error)
        self.assertNotIn("#4", error)

    def test_long_source_is_truncated_in_the_example(self) -> None:
        long_src = "あ" * 200
        self._write_cache([])

        _, error = self._rebuild([_tran(1, long_src)])

        self.assertIn("#1「" + "あ" * 24 + "…」", error)


if __name__ == "__main__":
    unittest.main()
