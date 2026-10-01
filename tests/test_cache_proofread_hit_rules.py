"""缓存命中规则：有校对稿就算最终稿，原文改过也照样命中；没校对稿才去比对 post_src / pre_dst。

背景：`proofread_dst` 字段**整个缺失**时 `_cache_get` 拿到的默认值是 None，而 `None == ""`
是 False —— 这一条于是被判成"有校对稿"，post_src / pre_dst / 翻译失败那几项检查整段跳过。
结果：很老的缓存（还没有校对稿这个字段）或手改过的缓存，即使原文早就改过，也会静默算命中。
字段缺失应当作"没校对过"，该走的检查一步都不能少。

两侧都要钉住：缺失字段照常检查；真有校对稿时原文变了也照常命中——后者是设计意图（校对稿
是最终稿），别被后来人当成 bug"顺手修掉"。
"""

import asyncio
import json
import os
import tempfile
import unittest

from GalTransl.Cache import (
    MISS_POST_SRC_CHANGED,
    MISS_PRE_DST_EMPTY,
    MISS_TRANSLATE_FAILED,
    get_transCache_from_json,
)
from GalTransl.CSentense import CSentense


def _tran(pre_src: str, post_src: str | None = None) -> CSentense:
    tran = CSentense(pre_src=pre_src, speaker="", index=1)
    if post_src is not None:
        tran.post_src = post_src
    return tran


class _CacheCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="cache-proofread-")
        self.cache_path = os.path.join(self.dir, "sc_2_st01.txt.json")

    def _write_cache(self, entry: dict) -> None:
        with open(self.cache_path, "w", encoding="utf8") as f:
            json.dump([entry], f, ensure_ascii=False)

    def _lookup(self, tran: CSentense, **kwargs):
        hit, unhit = asyncio.run(
            get_transCache_from_json([tran], self.cache_path, **kwargs)
        )
        return hit, unhit


class MissingProofreadFieldTests(_CacheCase):
    """缺字段 = 没校对过：post_src / pre_dst 照查。"""

    # 注意：这条没有 proofread_dst（老缓存的样子）
    def test_changed_source_is_not_a_hit(self) -> None:
        tran = _tran("こんにちは", post_src="こんにちは。")
        self._write_cache(
            {"name": "", "pre_src": "こんにちは", "post_src": "こんにちは", "pre_dst": "你好"}
        )

        hit, unhit = self._lookup(tran)

        self.assertEqual(hit, [])
        self.assertEqual([t.cache_miss_reason for t in unhit], [MISS_POST_SRC_CHANGED])

    def test_empty_translation_is_not_a_hit(self) -> None:
        tran = _tran("こんにちは")
        self._write_cache(
            {"name": "", "pre_src": "こんにちは", "post_src": "こんにちは", "pre_dst": ""}
        )

        _, unhit = self._lookup(tran)

        self.assertEqual([t.cache_miss_reason for t in unhit], [MISS_PRE_DST_EMPTY])

    def test_failed_translation_is_retried(self) -> None:
        """重试失败句：以前这个分支里还套了一层 `no_proofread or ...`（恒真），现已拉平。"""
        tran = _tran("こんにちは")
        self._write_cache(
            {
                "name": "",
                "pre_src": "こんにちは",
                "post_src": "こんにちは",
                "pre_dst": "翻译失败(Failed)",
            }
        )

        _, unhit = self._lookup(tran, retry_failed=True)

        self.assertEqual([t.cache_miss_reason for t in unhit], [MISS_TRANSLATE_FAILED])

    def test_matching_entry_still_hits(self) -> None:
        """该命中的还是要命中：别把缺字段一刀切成"永远未命中"。"""
        tran = _tran("こんにちは")
        self._write_cache(
            {"name": "", "pre_src": "こんにちは", "post_src": "こんにちは", "pre_dst": "你好"}
        )

        hit, unhit = self._lookup(tran)

        self.assertEqual(unhit, [])
        self.assertEqual(tran.pre_dst, "你好")
        self.assertEqual(tran.cache_miss_reason, "")


class ProofreadResultWinsTests(_CacheCase):
    """有校对稿 = 最终稿：原文改过也照样命中，直接拿校对稿。"""

    def test_changed_source_still_hits_with_the_proofread_result(self) -> None:
        tran = _tran("こんにちは", post_src="こんにちは。")
        self._write_cache(
            {
                "name": "",
                "pre_src": "こんにちは",
                "post_src": "こんにちは",
                "pre_dst": "你好",
                "proofread_dst": "您好（校对稿）",
            }
        )

        hit, unhit = self._lookup(tran)

        self.assertEqual(unhit, [])
        self.assertEqual(len(hit), 1)
        self.assertEqual(tran.proofread_zh, "您好（校对稿）")
        self.assertEqual(tran.post_dst, "您好（校对稿）")

    def test_legacy_field_names_count_as_proofread(self) -> None:
        """老 key 名（proofread_zh / post_jp / …）同样算"有校对稿"。"""
        tran = _tran("こんにちは", post_src="こんにちは。")
        self._write_cache(
            {
                "name": "",
                "pre_jp": "こんにちは",
                "post_jp": "こんにちは",
                "pre_zh": "你好",
                "proofread_zh": "您好",
            }
        )

        hit, unhit = self._lookup(tran)

        self.assertEqual(unhit, [])
        self.assertEqual(tran.proofread_zh, "您好")


if __name__ == "__main__":
    unittest.main()
