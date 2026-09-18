"""doub_content → proofread_comment 重命名：只读兼容旧缓存，写回一律用新名。

- 写缓存：只写 proofread_comment，不再写 doub_content；
- 读缓存：新键优先，旧缓存里的 doub_content 也能读出来（Cache 与 Agent 两侧都要认）。
"""

import asyncio
import json
import os
import tempfile
import unittest

from GalTransl.Agent.runtime import _cache_field_value
from GalTransl.Cache import _build_cache_obj, get_transCache_from_json
from GalTransl.CSentense import CSentense

SRC = "おはよう"
DST = "早上好"


def _tran() -> CSentense:
    tran = CSentense(SRC, speaker="", index=0)
    tran.post_src = SRC
    tran.pre_dst = DST
    tran.post_dst = DST
    return tran


def _write_cache(path: str, entry: dict) -> None:
    with open(path, "w", encoding="utf8") as f:
        json.dump([entry], f)


def _read_cache(path: str) -> CSentense:
    tran = CSentense(SRC, speaker="", index=0)
    tran.post_src = SRC
    asyncio.run(get_transCache_from_json([tran], path))
    return tran


class CacheWriteTests(unittest.TestCase):
    def test_writes_new_key_only(self) -> None:
        tran = _tran()
        tran.proofread_comment = "漏译：原文「おっぱい」没对应词，建议补为「欧派」"
        obj = _build_cache_obj(tran)
        self.assertEqual(obj["proofread_comment"], "漏译：原文「おっぱい」没对应词，建议补为「欧派」")
        self.assertNotIn("doub_content", obj)

    def test_empty_comment_is_not_written(self) -> None:
        obj = _build_cache_obj(_tran())
        self.assertNotIn("proofread_comment", obj)
        self.assertNotIn("doub_content", obj)


class CacheReadCompatTests(unittest.TestCase):
    def test_reads_new_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.json")
            _write_cache(
                path,
                {
                    "index": 0,
                    "name": "",
                    "pre_src": SRC,
                    "post_src": SRC,
                    "pre_dst": DST,
                    "proofread_comment": "新键批注",
                    "trans_by": "unit-test",
                },
            )
            tran = _read_cache(path)
        self.assertEqual(tran.trans_by, "unit-test")  # 命中缓存的前提
        self.assertEqual(tran.proofread_comment, "新键批注")

    def test_reads_old_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.json")
            _write_cache(
                path,
                {
                    "index": 0,
                    "name": "",
                    "pre_src": SRC,
                    "post_src": SRC,
                    "pre_dst": DST,
                    "doub_content": "旧键批注",
                    "trans_by": "unit-test",
                },
            )
            tran = _read_cache(path)
        self.assertEqual(tran.trans_by, "unit-test")
        self.assertEqual(tran.proofread_comment, "旧键批注")


class AgentFieldCompatTests(unittest.TestCase):
    def test_agent_reads_both_keys(self) -> None:
        self.assertEqual(_cache_field_value({"proofread_comment": "新批注"}, "proofread_comment"), "新批注")
        self.assertEqual(_cache_field_value({"doub_content": "旧批注"}, "proofread_comment"), "旧批注")

    def test_new_key_wins_over_old(self) -> None:
        entry = {"proofread_comment": "新批注", "doub_content": "旧批注"}
        self.assertEqual(_cache_field_value(entry, "proofread_comment"), "新批注")


if __name__ == "__main__":
    unittest.main()
