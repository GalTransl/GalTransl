"""改了单文件分片设置后，已有缓存照样命中：本分片缓存里没有的句子，到同一文件的其它分片缓存里找。"""

import asyncio
import os
import tempfile
import unittest

from GalTransl.Cache import (
    SplitCachePool,
    get_transCache_from_json,
    list_file_cache_names,
    save_transCache_to_json,
)
from GalTransl.CSplitter import DictionaryCountSplitter, EqualPartsSplitter
from GalTransl.Frontend.LLMTranslate import (
    _chunk_cache_file_path,
    _remove_stale_chunk_caches,
    _SplitCachePools,
)


def _lines(count: int) -> list[dict]:
    return [{"name": "", "message": f"第{i}句"} for i in range(count)]


def _translate(chunks) -> None:
    for chunk in chunks:
        for tran in chunk.trans_list:
            tran.post_src = tran.pre_src
            tran.pre_dst = tran.pre_src.replace("句", "译")


def _prepare(chunks) -> None:
    for chunk in chunks:
        for tran in chunk.trans_list:
            tran.post_src = tran.pre_src


class SplitCacheReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="galtransl-split-cache-")
        self.addCleanup(temporary.cleanup)
        self.input_dir = os.path.join(temporary.name, "gt_input")
        self.cache_dir = os.path.join(temporary.name, "transl_cache")
        os.makedirs(self.input_dir)
        os.makedirs(self.cache_dir)
        self.file_path = os.path.join(self.input_dir, "a.json")

    def _save(self, chunks, post_save: bool = True) -> None:
        for chunk in chunks:
            asyncio.run(save_transCache_to_json(
                chunk.trans_list, _chunk_cache_file_path(chunk, self.input_dir, self.cache_dir), post_save=post_save
            ))

    def _pools(self) -> _SplitCachePools:
        return _SplitCachePools(self.input_dir, self.cache_dir, {"a.json"})

    def _lookup(self, chunks) -> tuple[list, list]:
        pools = self._pools()
        hits, misses = [], []
        for chunk in chunks:
            hit, miss = asyncio.run(get_transCache_from_json(
                chunk.trans_list,
                _chunk_cache_file_path(chunk, self.input_dir, self.cache_dir),
                fallback_pool=pools.get(chunk),
            ))
            pools.done(chunk)
            hits += hit
            misses += miss
        return hits, misses

    def _resplit_hits_everything(self, old_splitter, new_splitter, post_save: bool = True) -> None:
        old = old_splitter.split(_lines(10), self.file_path)
        _translate(old)
        self._save(old, post_save=post_save)
        new = new_splitter.split(_lines(10), self.file_path)
        _prepare(new)
        hits, misses = self._lookup(new)
        self.assertEqual(misses, [])
        self.assertEqual([tran.pre_dst for tran in hits if tran.pre_src],
                         [tran.pre_src.replace("句", "译") for tran in hits if tran.pre_src])

    def test_smaller_chunks_reuse_the_old_chunks(self) -> None:
        self._resplit_hits_everything(DictionaryCountSplitter(4), DictionaryCountSplitter(3))

    def test_turning_split_on_and_off_reuses_the_cache(self) -> None:
        self._resplit_hits_everything(EqualPartsSplitter(1), DictionaryCountSplitter(3))

    def test_turning_split_off_reuses_the_chunks(self) -> None:
        self._resplit_hits_everything(DictionaryCountSplitter(3), EqualPartsSplitter(1))

    def test_same_chunk_count_with_shifted_boundaries(self) -> None:
        """分片数没变、边界挪了：文件名都对得上，但内容对不上，也要能命中。"""
        self._resplit_hits_everything(EqualPartsSplitter(3), DictionaryCountSplitter(4))

    def test_cross_overlap_change(self) -> None:
        self._resplit_hits_everything(DictionaryCountSplitter(4, 2), DictionaryCountSplitter(5))

    def test_interrupted_run_with_only_append_logs(self) -> None:
        self._resplit_hits_everything(DictionaryCountSplitter(4), DictionaryCountSplitter(3), post_save=False)

    def test_unsplit_file_without_old_chunks_is_looked_up_as_before(self) -> None:
        chunks = EqualPartsSplitter(1).split(_lines(3), self.file_path)
        _translate(chunks)
        self._save(chunks)
        self.assertIsNone(self._pools().get(chunks[0]))

    def test_stale_chunk_caches_are_removed_after_the_file_is_saved(self) -> None:
        old = DictionaryCountSplitter(4).split(_lines(10), self.file_path)
        _translate(old)
        self._save(old)
        self._save(old[:1], post_save=False)  # 留一份增量日志
        new = DictionaryCountSplitter(6).split(_lines(10), self.file_path)
        _prepare(new)
        self._lookup(new)
        self._save(new)
        _remove_stale_chunk_caches(new[0], self._pools())
        self.assertEqual(sorted(os.listdir(self.cache_dir)), ["a.json_0.json", "a.json_1.json"])
        hits, misses = self._lookup(new)  # 并进新分片的译文还在
        self.assertEqual(misses, [])

    def test_another_input_named_like_a_chunk_is_not_borrowed(self) -> None:
        """输入文件 foo_1 的缓存 foo_1.json 看着像 foo 的分片，但不能算到 foo 头上。"""
        for name in ("foo.json", "foo_1.json", "foo_2.json"):
            open(os.path.join(self.cache_dir, name), "w", encoding="utf8").write("[]")
        self.assertEqual(list_file_cache_names(self.cache_dir, "foo"), ["foo.json", "foo_1.json", "foo_2.json"])
        self.assertEqual(list_file_cache_names(self.cache_dir, "foo", exclude={"foo_1.json"}), ["foo.json", "foo_2.json"])

    def test_both_sides_matching_beats_a_boundary_match(self) -> None:
        """同一句出现在不同上下文里：两侧都对得上的优先，不拿边界上的那份。"""
        path = os.path.join(self.cache_dir, "x.json")
        with open(path, "w", encoding="utf8") as f:
            f.write('[{"name": "", "pre_src": "嗯", "post_src": "嗯", "pre_dst": "边界", "__cache_key": "None嗯B"},'
                    ' {"name": "", "pre_src": "嗯", "post_src": "嗯", "pre_dst": "正解", "__cache_key": "A嗯B"}]')
        pool = SplitCachePool([path])
        asyncio.run(pool.load())
        self.assertEqual(pool.match("A", "嗯", "B")[0]["pre_dst"], "正解")
        self.assertEqual(pool.match("C", "嗯", "B")[0]["pre_dst"], "边界")
        self.assertIsNone(pool.match("C", "嗯", "D"))


if __name__ == "__main__":
    unittest.main()
