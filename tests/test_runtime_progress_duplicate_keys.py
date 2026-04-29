"""
回归测试：当缓存文件中存在多个上下文三元组完全相同的条目（如重复短句或连续相同名称）时，
RuntimeProgressCache 应正确统计每个位置的翻译进度，而不因 set 去重导致少计。

复现场景：
  - yst00125.json：46 条，大量重复短句（"中に出す"/"外に出す"），
    entries 6/8 均为 "中に出す" 且前后均为 "外に出す" → key 碰撞 → 23/46
  - yst00086.json：91 条，末尾 4 个连续 "二人" → 中间两条 key 相同 → 90/91
"""

import json
import os
import tempfile
import time
import unittest

import orjson

from GalTransl.server import RuntimeProgressCache


def _make_cache_entry(index: int, name: str, pre_src: str, pre_dst: str) -> dict:
    return {
        "index": index,
        "name": name,
        "pre_src": pre_src,
        "post_src": pre_src,
        "pre_dst": pre_dst,
        "proofread_dst": "",
        "trans_by": "test",
        "proofread_by": "",
    }


class TestRuntimeProgressDuplicateKeys(unittest.TestCase):
    """确保含重复上下文 key 的缓存文件进度被正确计算，而非 set 去重后少计。"""

    def _run_get_progress(
        self,
        project_dir: str,
        file_totals: dict,
        cache_file_display_map: dict,
    ) -> dict:
        cache = RuntimeProgressCache()
        return cache.get_progress(
            project_dir,
            file_totals=file_totals,
            cache_file_display_map=cache_file_display_map,
        )

    def test_duplicate_context_triplet_counted_separately_in_json(self):
        """
        模拟 yst00125 场景：
        entries 6/8 均为 {"name":"","pre_src":"中に出す"}，
        前后都是 "外に出す" → 旧实现 key 碰撞 → 只计 1 次。
        修复后应计 2 次。
        """
        entries = [
            _make_cache_entry(0, "", "A", "翻A"),
            _make_cache_entry(1, "", "B", "翻B"),
            _make_cache_entry(2, "", "C", "翻C"),
            _make_cache_entry(3, "", "D", "翻D"),
            _make_cache_entry(4, "", "D", "翻D"),  # D 重复（不同位置）
            _make_cache_entry(5, "", "E", "翻E"),
            _make_cache_entry(6, "", "C", "翻C"),  # C 再次出现，前后同 B/D，key 与 idx=2 碰撞
            _make_cache_entry(7, "", "B", "翻B"),
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = os.path.join(tmpdir, "transl_cache")
            os.makedirs(cache_dir)
            cache_file = os.path.join(cache_dir, "test.json")
            with open(cache_file, "wb") as f:
                f.write(orjson.dumps(entries, option=orjson.OPT_INDENT_2))

            result = self._run_get_progress(
                tmpdir,
                file_totals={"test.json": len(entries)},
                cache_file_display_map={"test.json": "test.json"},
            )

        files = {f["filename"]: f for f in result["files"]}
        self.assertIn("test.json", files)
        info = files["test.json"]
        self.assertEqual(info["total"], len(entries))
        # 全部 8 条都有 pre_dst，应计 8 而非因 key 碰撞少计
        self.assertEqual(
            info["translated"],
            len(entries),
            f"期望 {len(entries)} 条已翻，实际 {info['translated']}（key 碰撞未修复）",
        )

    def test_four_consecutive_same_entries_all_counted(self):
        """
        模拟 yst00086 末尾 4 个连续 "二人"：
        中间两条 prev=cur=next="二人"，key 完全相同，旧实现少计 1 条。
        """
        entries = [
            _make_cache_entry(85, "", "男子", "男孩"),
            _make_cache_entry(86, "", "男子", "男孩"),
            _make_cache_entry(87, "", "二人", "两人"),
            _make_cache_entry(88, "", "二人", "两人"),
            _make_cache_entry(89, "", "二人", "两人"),  # prev=cur=next="二人" 同 idx=88
            _make_cache_entry(90, "", "二人", "两人"),
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = os.path.join(tmpdir, "transl_cache")
            os.makedirs(cache_dir)
            cache_file = os.path.join(cache_dir, "yst00086.json")
            with open(cache_file, "wb") as f:
                f.write(orjson.dumps(entries, option=orjson.OPT_INDENT_2))

            result = self._run_get_progress(
                tmpdir,
                file_totals={"yst00086.json": len(entries)},
                cache_file_display_map={"yst00086.json": "yst00086.json"},
            )

        files = {f["filename"]: f for f in result["files"]}
        info = files["yst00086.json"]
        self.assertEqual(
            info["translated"],
            len(entries),
            f"期望 {len(entries)} 条，实际 {info['translated']}（连续相同条目 key 碰撞）",
        )

    def test_append_jsonl_duplicate_cache_key_counted_separately(self):
        """
        .append.jsonl 中两个不同 index 的条目拥有相同 __cache_key（重复短句），
        两者都应被计入，而非 set 去重后只计 1。
        """
        entries = [
            {**_make_cache_entry(6, "", "中に出す", "体内"), "__cache_key": "外に出す中に出す外に出す"},
            {**_make_cache_entry(8, "", "中に出す", "体内"), "__cache_key": "外に出す中に出す外に出す"},
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = os.path.join(tmpdir, "transl_cache")
            os.makedirs(cache_dir)
            append_file = os.path.join(cache_dir, "yst00125.json.append.jsonl")
            with open(append_file, "wb") as f:
                for entry in entries:
                    f.write(orjson.dumps(entry) + b"\n")

            result = self._run_get_progress(
                tmpdir,
                file_totals={"yst00125.json": 10},
                cache_file_display_map={"yst00125.json": "yst00125.json"},
            )

        files = {f["filename"]: f for f in result["files"]}
        info = files["yst00125.json"]
        self.assertEqual(
            info["translated"],
            2,
            f"期望 2 条（index 6 和 8 各算一条），实际 {info['translated']}（__cache_key 碰撞未修复）",
        )

    def test_current_run_snapshot_matching_retransl_key_still_counts_as_translated(self):
        """
        当前轮次已经重写过的 .json 快照，即使新结果仍命中 retranslKey，
        本轮 runtime 进度也应计为已完成；否则会出现 9954/9955 卡住不回满。
        """
        entries = [
            {
                "index": 1,
                "name": "",
                "pre_src": "原文",
                "post_src": "原文",
                "pre_dst": "这里仍然包含残留日文标记",
                "proofread_dst": "",
                "problem": "",
                "trans_by": "test",
                "proofread_by": "",
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = os.path.join(tmpdir, "transl_cache")
            os.makedirs(cache_dir)
            cache_file = os.path.join(cache_dir, "test.json")
            with open(cache_file, "wb") as f:
                f.write(orjson.dumps(entries, option=orjson.OPT_INDENT_2))

            started_at_ns = time.time_ns()
            time.sleep(0.01)
            now = time.time()
            os.utime(cache_file, (now, now))

            result = RuntimeProgressCache().get_progress(
                tmpdir,
                file_totals={"test.json": 1},
                cache_file_display_map={"test.json": "test.json"},
                retran_key="残留日文",
                current_job_started_at_ns=started_at_ns,
            )

        files = {f["filename"]: f for f in result["files"]}
        info = files["test.json"]
        self.assertEqual(info["translated"], 1)

    def test_retransl_stats_counts_each_matching_row(self):
        entries = [
            {
                **_make_cache_entry(1, "", "同一句", ""),
                "problem": "残留日文",
            },
            {
                **_make_cache_entry(2, "", "同一句", ""),
                "problem": "残留日文",
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = os.path.join(tmpdir, "transl_cache")
            os.makedirs(cache_dir)
            cache_file = os.path.join(cache_dir, "test.json")
            with open(cache_file, "wb") as f:
                f.write(orjson.dumps(entries, option=orjson.OPT_INDENT_2))

            result = RuntimeProgressCache().get_progress(
                tmpdir,
                file_totals={"test.json": 2},
                cache_file_display_map={"test.json": "test.json"},
                retran_terms=["残留日文"],
            )

        stats = {item["key"]: item["count"] for item in result["retransl_stats"]}
        self.assertEqual(stats["残留日文"], 2)


if __name__ == "__main__":
    unittest.main()
