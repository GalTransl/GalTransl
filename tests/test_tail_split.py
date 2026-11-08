"""收尾拆分（splitTailChunks）：空闲 worker 拆走正在翻的段还没发出的后半段。"""

import asyncio
import os
import tempfile
import unittest
from collections import Counter
from types import SimpleNamespace

from GalTransl.Backend.BaseTranslate import BaseTranslate
from GalTransl.CSentense import CSentense
from GalTransl.Frontend.LLMTranslate import _tail_split_lines, _tail_split_loop
from GalTransl.Frontend.TranslationEta import TranslationEtaEstimator
from GalTransl.TailSplit import (
    TailSplitHandle,
    TailSplitRegistry,
    TailSplitTask,
    reset_current_tail_split,
    set_current_tail_split,
)


class _FakeBackend(BaseTranslate):
    """只留 _batch_translate_common 用到的那几样：每批等一会儿，译文 = 原文加前缀。"""

    def __init__(self, pj_config, delay: float = 0.005) -> None:
        self.skipH = False
        self.save_steps = 1
        self.pj_config = pj_config
        self.delay = delay
        self.sent = Counter()  # 每句被发出几次
        self.filenames: dict[int, str] = {}  # 句子 → 翻它的后端文件名

    def _check_stop_requested(self) -> None:
        pass

    def _get_effective_num_per_request(self, configured_value, proofread=False):
        return int(configured_value)

    def _update_dynamic_num_per_request(self, **kwargs) -> None:
        pass

    async def translate(self, trans_list, gptdict="", proofread=False, filename=""):
        await asyncio.sleep(self.delay)
        for tran in trans_list:
            self.sent[id(tran)] += 1
            self.filenames[id(tran)] = filename
            tran.pre_dst = "译" + tran.pre_src
            tran.trans_by = "fake"
        return len(trans_list), list(trans_list)

    async def batch_translate(self, filename, cache_file_path, trans_list, num_pre_request,
                              retry_failed=False, gpt_dic=None, proofread=False, retran_key="",
                              translist_hit=None, translist_unhit=None):
        return await self._batch_translate_common(
            filename=filename, cache_file_path=cache_file_path, translist_unhit=translist_unhit,
            num_pre_request=num_pre_request, proofread=proofread,
        )


def _sentences(count: int) -> list[CSentense]:
    trans = []
    for i in range(count):
        tran = CSentense(f"句{i}", "", i + 1)
        tran.post_src = tran.pre_src
        if trans:
            tran.prev_tran = trans[-1]
            trans[-1].next_tran = tran
        trans.append(tran)
    return trans


class HandleTests(unittest.TestCase):
    def test_unbound_handle_is_never_split(self) -> None:
        handle = TailSplitHandle(10)
        self.assertEqual(handle.spare, 0)
        self.assertEqual(handle.split(), [])

    def test_split_takes_the_unsent_second_half(self) -> None:
        items = list(range(100))
        handle = TailSplitHandle(10).bind(items)
        self.assertEqual(handle.reserve(0, 10), list(range(10)))
        stolen = handle.split()  # 未发出 90 句：留 45，拆走 45
        self.assertEqual(stolen, list(range(55, 100)))
        self.assertEqual(handle.end, 55)
        self.assertEqual(handle.reserve(50, 10), list(range(50, 55)))  # 不越过新终点

    def test_too_little_left_is_not_split(self) -> None:
        handle = TailSplitHandle(10).bind(list(range(30)))
        handle.reserve(0, 11)
        self.assertEqual(handle.split(), [])  # 只剩 19 句，不够两边各留 10 句

    def test_registry_picks_the_segment_with_most_unsent_lines(self) -> None:
        small = TailSplitTask(None, "", "a", [], "a", TailSplitHandle(5).bind(list(range(20))))
        large = TailSplitTask(None, "", "b", [], "b", TailSplitHandle(5).bind(list(range(50))))
        unbound = TailSplitTask(None, "", "c", [], "c", TailSplitHandle(5))
        registry = TailSplitRegistry([small, large, unbound])
        self.assertIs(registry.pick(), large)

    def test_setting_is_off_by_default_and_off_for_rebuild(self) -> None:
        def config(values):
            return SimpleNamespace(getKey=lambda key, default=None: values.get(key, default))

        self.assertEqual(_tail_split_lines(config({}), "ForGal-json"), 0)
        on = config({"splitTailChunks": "true", "gpt.numPerRequestTranslate": 8})
        self.assertEqual(_tail_split_lines(on, "ForGal-json"), 16)
        self.assertEqual(_tail_split_lines(on, "rebuilda"), 0)


class TailSplitRunTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="galtransl-tail-split-")
        self.addCleanup(temporary.cleanup)
        self.cache_path = os.path.join(temporary.name, "a.json_0")

    def _run(self, lines: int, thieves: int, per_request: int = 4):
        estimator = TranslationEtaEstimator(workers=thieves + 1)
        config = SimpleNamespace(
            getKey=lambda key, default=None: {"gpt.numPerRequestTranslate": per_request}.get(key, default),
            getProjectDir=lambda: "",
            tail_split_registry=TailSplitRegistry(),
            eta_estimator=estimator,
            gpt_dic=None,
            select_translator="",
        )
        config.bar = lambda count=1, *args, **kwargs: None
        backend = _FakeBackend(config)
        trans = _sentences(lines)
        victim = TailSplitTask(None, self.cache_path, "a.json_0", trans, self.cache_path,
                               TailSplitHandle(2 * per_request))
        config.tail_split_registry.add(victim)
        estimator.start(self.cache_path, lines)

        async def translate_victim():
            token = set_current_tail_split(victim.handle)
            try:
                await backend.batch_translate("a.json_0", self.cache_path, trans, per_request,
                                              translist_unhit=list(trans))
            finally:
                reset_current_tail_split(token)
            config.tail_split_registry.remove(victim)
            await victim.handle.wait_children()

        async def main():
            semaphore = asyncio.Semaphore(thieves + 1)
            async with semaphore:
                victim_task = asyncio.create_task(translate_victim())
                await asyncio.sleep(0)  # 让 victim 先绑定句柄
                thief_tasks = [asyncio.create_task(_tail_split_loop(semaphore, config, backend))
                               for _ in range(thieves)]
                await victim_task
            await asyncio.gather(*thief_tasks)

        asyncio.run(main())
        return backend, trans, config

    def test_every_line_is_translated_exactly_once(self) -> None:
        backend, trans, config = self._run(lines=200, thieves=3)
        self.assertEqual([backend.sent[id(tran)] for tran in trans], [1] * len(trans))
        self.assertTrue(all(tran.pre_dst == "译" + tran.pre_src for tran in trans))
        self.assertGreater(config.tail_split_registry.pieces, 0)
        self.assertEqual(config.tail_split_registry.active, [])

    def test_pieces_report_under_the_same_file(self) -> None:
        """拆出来的段多带一个 _{段号}：运行时按 `_数字` 剥一层就归回同一个分片。"""
        backend, trans, _ = self._run(lines=200, thieves=3)
        names = set(backend.filenames.values())
        self.assertIn("a.json_0", names)
        self.assertTrue(all(name == "a.json_0" or name.rsplit("_", 1)[0] == "a.json_0" for name in names))

    def test_work_is_spread_across_workers(self) -> None:
        backend, trans, _ = self._run(lines=400, thieves=3)
        by_name = Counter(backend.filenames.values())
        self.assertLess(by_name["a.json_0"], 300)  # 不拆的话 victim 自己翻 400 句

    def test_small_chunks_are_left_alone(self) -> None:
        backend, trans, config = self._run(lines=10, thieves=2)
        self.assertEqual(config.tail_split_registry.pieces, 0)
        self.assertEqual(set(backend.filenames.values()), {"a.json_0"})

    def test_cache_append_log_stays_valid_with_concurrent_pieces(self) -> None:
        self._run(lines=200, thieves=3)
        import orjson

        with open(self.cache_path + ".json.append.jsonl", "rb") as f:
            rows = [orjson.loads(line) for line in f.read().splitlines() if line]
        self.assertEqual(sorted(row["pre_src"] for row in rows), sorted(f"句{i}" for i in range(200)))


class EstimatorTailSplitTests(unittest.TestCase):
    def test_tail_split_spreads_the_remaining_work(self) -> None:
        clock = [0.0]
        estimator = TranslationEtaEstimator(workers=4, clock=lambda: clock[0])
        estimator.start("probe", 1000)
        clock[0] += 10
        estimator.record("probe", 10)  # 1 句/秒
        estimator.finish("probe")
        estimator.queue([("big", 400)])
        self.assertAlmostEqual(estimator.estimate(), 400.0)
        estimator.min_split_lines = 20
        self.assertAlmostEqual(estimator.estimate(), 100.0)

    def test_split_moves_lines_to_the_new_piece(self) -> None:
        clock = [0.0]
        estimator = TranslationEtaEstimator(workers=2, clock=lambda: clock[0])
        estimator.start("a", 100)
        clock[0] += 10
        estimator.record("a", 10)
        estimator.split("a", "a#1", 45)
        self.assertAlmostEqual(estimator.estimate(), 45.0)


if __name__ == "__main__":
    unittest.main()
