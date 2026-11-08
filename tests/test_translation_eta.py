"""翻译 ETA：按调度模拟剩余时间，以及开翻前统计各分片实际待翻句数。"""

import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace

from GalTransl.CSplitter import DictionaryCountSplitter
from GalTransl.Frontend.LLMTranslate import _count_pending_lines
from GalTransl.Frontend.TranslationEta import TranslationEtaEstimator


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _estimator_with_rate(workers: int, rate: float) -> tuple[TranslationEtaEstimator, _Clock]:
    """造一个已测到「单 worker 每秒 rate 句」的估算器（用一个探针分片采样后移除）。"""
    clock = _Clock()
    estimator = TranslationEtaEstimator(workers=workers, clock=clock)
    estimator.start("probe", 1000)
    clock.now += 10
    estimator.record("probe", int(rate * 10))
    estimator.finish("probe")
    return estimator, clock


class EstimatorTests(unittest.TestCase):
    def test_no_estimate_before_the_first_batch(self) -> None:
        estimator = TranslationEtaEstimator(workers=4)
        estimator.queue([("a", 100)])
        self.assertIsNone(estimator.estimate())

    def test_tail_is_bounded_by_the_longest_chunk_not_total_over_workers(self) -> None:
        """4 个 worker、一个 400 句大分片 + 三个 10 句小分片：大分片独占一个 worker 串行翻完。

        「剩余 430 句 ÷ 4 个 worker 的速度」会给出 107.5 秒，实际要 400 秒。
        """
        estimator, _ = _estimator_with_rate(workers=4, rate=1.0)
        estimator.queue([("big", 400), ("s1", 10), ("s2", 10), ("s3", 10)])

        self.assertAlmostEqual(estimator.estimate(), 400.0)

    def test_queued_chunks_go_to_the_earliest_free_worker(self) -> None:
        estimator, _ = _estimator_with_rate(workers=2, rate=1.0)
        estimator.queue([("a", 100), ("b", 50), ("c", 50), ("d", 50)])
        # a: 0→100；b: 0→50；c 接 b: 50→100；d 接 a 或 c: 100→150
        self.assertAlmostEqual(estimator.estimate(), 150.0)

    def test_running_chunk_counts_remaining_lines_and_time_in_flight(self) -> None:
        estimator, clock = _estimator_with_rate(workers=1, rate=1.0)
        estimator.start("a", 100)
        clock.now += 30
        estimator.record("a", 30)  # 30 秒翻 30 句，速度不变
        clock.now += 5  # 当前这批已经等了 5 秒
        self.assertAlmostEqual(estimator.estimate(), 65.0)

    def test_fewer_workers_lengthen_the_estimate(self) -> None:
        estimator, _ = _estimator_with_rate(workers=4, rate=1.0)
        estimator.queue([(str(i), 100) for i in range(4)])
        self.assertAlmostEqual(estimator.estimate(), 100.0)
        estimator.workers = 2
        self.assertAlmostEqual(estimator.estimate(), 200.0)

    def test_finished_and_empty_chunks_add_no_time(self) -> None:
        estimator, _ = _estimator_with_rate(workers=1, rate=1.0)
        estimator.queue([("a", 0), ("b", 20)])
        estimator.start("a", 0)
        estimator.finish("a")
        self.assertAlmostEqual(estimator.estimate(), 20.0)

    def test_rate_is_smoothed_across_batches(self) -> None:
        estimator, clock = _estimator_with_rate(workers=1, rate=1.0)
        estimator.start("a", 1000)
        clock.now += 1
        estimator.record("a", 100)  # 一批异常快（样本 100 句/秒）
        self.assertLess(estimator.rate, 20.0)


class CountPendingLinesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="eta-pending-")
        self.cache_path = os.path.join(self.dir, "a.json")
        json_list = [
            {"name": "", "message": "一"},
            {"name": "", "message": "二"},
            {"name": "", "message": "三"},
        ]
        self.chunk = DictionaryCountSplitter(100).split(json_list, "a.json")[0]
        self.config = SimpleNamespace(
            runtime_sentence_range=None,
            getKey=lambda key, default=None: {"retranslFail": False, "retranslKey": ""}.get(key, default),
        )

    def _write_cache(self, translations: list[str]) -> None:
        entries = [
            {"index": i + 1, "name": "", "pre_src": src, "post_src": src, "pre_dst": dst}
            for i, (src, dst) in enumerate(zip(["一", "二", "三"], translations))
        ]
        with open(self.cache_path, "w", encoding="utf8") as f:
            json.dump(entries, f, ensure_ascii=False)

    def _count(self) -> int:
        return asyncio.run(_count_pending_lines(self.chunk, self.cache_path, self.config, "ForGal-json"))

    def test_counts_only_cache_misses(self) -> None:
        self._write_cache(["1", "", "3"])
        self.assertEqual(self._count(), 1)

    def test_no_cache_means_everything_is_pending(self) -> None:
        self.assertEqual(self._count(), 3)

    def test_does_not_touch_the_real_sentences(self) -> None:
        """统计在副本上做：原句留给 worker 正式判定，命中的译文不能提前填进去。"""
        self._write_cache(["1", "2", "3"])
        self.assertEqual(self._count(), 0)
        self.assertEqual([tran.pre_dst for tran in self.chunk.trans_list], ["", "", ""])
        self.assertIs(self.chunk.trans_list[1].prev_tran, self.chunk.trans_list[0])


if __name__ == "__main__":
    unittest.main()
