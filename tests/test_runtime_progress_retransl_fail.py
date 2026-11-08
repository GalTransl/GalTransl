import os
import tempfile
import time
import unittest
from pathlib import Path

import orjson

from GalTransl import CACHE_FOLDERNAME
from GalTransl.server_runtime import RuntimeProgressCache


def _entries():
    return [
        {"index": 1, "pre_src": "a", "post_src": "a", "pre_dst": "A", "proofread_dst": ""},
        {"index": 2, "pre_src": "b", "post_src": "b", "pre_dst": "(Failed)b", "proofread_dst": ""},
        {"index": 3, "pre_src": "c", "post_src": "c", "pre_dst": "(Failed)c", "proofread_dst": "C"},
    ]


class RetranslFailProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = self.temp.name
        cache_dir = Path(self.project) / CACHE_FOLDERNAME
        cache_dir.mkdir()
        self.cache_path = cache_dir / "chapter.json"
        self.cache_path.write_bytes(orjson.dumps(_entries()))
        old = time.time() - 100
        os.utime(self.cache_path, (old, old))
        self.job_started_ns = int((old + 50) * 1_000_000_000)

    def _translated(self, tracker, **kwargs):
        progress = tracker.get_progress(
            self.project, {"chapter.json": 3}, {"chapter.json": "chapter.json"}, **kwargs
        )
        return progress["translated"]

    def test_failed_lines_pending_retry_are_not_counted(self):
        tracker = RuntimeProgressCache()
        # 任务开始前：(Failed) 也有译文，照旧算已翻
        self.assertEqual(self._translated(tracker), 3)
        # 任务开始、retranslFail 打开：同一个 tracker 不能复用旧结果；有校对稿的那条不重翻
        self.assertEqual(
            self._translated(tracker, retry_failed=True, current_job_started_at_ns=self.job_started_ns),
            2,
        )

    def test_failed_filtered_by_problem_filter_key_is_counted(self):
        self.assertEqual(
            self._translated(
                RuntimeProgressCache(),
                retry_failed=True,
                current_job_started_at_ns=self.job_started_ns,
                problem_filter_keys=["翻译失败"],
            ),
            3,
        )

    def test_retry_result_in_append_log_counts(self):
        append_path = Path(str(self.cache_path) + ".append.jsonl")
        append_path.write_bytes(orjson.dumps({
            "index": 2, "pre_src": "b", "post_src": "b", "pre_dst": "(Failed)b",
            # 与快照里由上下文拼出的 key 一致：前句 a、本句 b、后句 c
            "__cache_key": "abc",
        }) + b"\n")
        self.assertEqual(
            self._translated(
                RuntimeProgressCache(), retry_failed=True, current_job_started_at_ns=self.job_started_ns
            ),
            3,
        )

    def test_snapshot_rewritten_after_job_start_counts(self):
        self.assertEqual(
            self._translated(RuntimeProgressCache(), retry_failed=True, current_job_started_at_ns=1),
            3,
        )


class SplitChunkProgressTests(unittest.TestCase):
    def test_overlap_counted_per_chunk_and_append_deduped(self):
        with tempfile.TemporaryDirectory() as project:
            cache_dir = Path(project) / CACHE_FOLDERNAME
            cache_dir.mkdir()
            rows = [{"index": 1, "pre_src": "x", "post_src": "x", "pre_dst": "X"}]
            # 两块缓存里同位置同上下文的一句（交叉句），外加第 0 块的增量日志重复写了一次
            (cache_dir / "f.json_0.json").write_bytes(orjson.dumps(rows))
            (cache_dir / "f.json_1.json").write_bytes(orjson.dumps(rows))
            (cache_dir / "f.json_0.json.append.jsonl").write_bytes(
                orjson.dumps({**rows[0], "__cache_key": "NonexNone"}) + b"\n"
            )
            progress = RuntimeProgressCache().get_progress(
                project, {"f.json": 2}, {"f.json_0.json": "f.json", "f.json_1.json": "f.json"}
            )
            self.assertEqual(progress["translated"], 2)

    def test_append_key_with_trailing_newline_matches_legacy_snapshot(self):
        with tempfile.TemporaryDirectory() as project:
            cache_dir = Path(project) / CACHE_FOLDERNAME
            cache_dir.mkdir()
            # 旧快照没有 __cache_key，key 由上下文现拼；原文以换行结尾
            rows = [
                {"index": 1, "name": "A", "pre_src": "a\n", "post_src": "a", "pre_dst": "(Failed)a"},
                {"index": 2, "name": "B", "pre_src": "b\n", "post_src": "b", "pre_dst": "B"},
            ]
            (cache_dir / "f.json").write_bytes(orjson.dumps(rows))
            # 重翻第 1 句后写进增量日志，__cache_key 由 Cache._build_cache_key_for_tran 生成
            (cache_dir / "f.json.append.jsonl").write_bytes(
                orjson.dumps({**rows[0], "pre_dst": "A", "__cache_key": "NoneAa\nBb\n"}) + b"\n"
            )
            progress = RuntimeProgressCache().get_progress(project, {"f.json": 2}, {"f.json": "f.json"})
            self.assertEqual(progress["files"][0]["translated"], 2)
            tracker = RuntimeProgressCache()
            tracker.get_progress(project, {}, {})
            stats = next(iter(tracker._project_files.values()))
            self.assertEqual(
                stats["f.json.append.jsonl"].translated_keys - stats["f.json"].translated_keys, frozenset()
            )


if __name__ == "__main__":
    unittest.main()
