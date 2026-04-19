"""回归测试：append-jsonl 合并入快照时不应抹掉快照中已有的 `problem` 字段。

覆盖场景：重翻中途被打断后，再次启动时 compact_cache_append_logs 合并
append 条目到快照。append 条目不带 `problem`（中间增量不会写该派生字段），
旧实现直接整条替换快照条目，导致 `problem` 被丢掉，进而使
retranslKey-by-problem 失效，用户感知为 "problem 不刷新"。
"""

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import orjson

from GalTransl.Cache import (
    _append_cache_file_path,
    compact_cache_append_logs,
    get_transCache_from_json,
)
from GalTransl.CSentense import CSentense
from GalTransl.Service import JobCancelledError, JobSpec, run_job_async


def _make_cache_key(speaker: str, pre_src: str, prev: str = "None", nxt: str = "None") -> str:
    return f"{prev}{speaker}{pre_src}{nxt}"


class CompactPreservesProblemTests(unittest.IsolatedAsyncioTestCase):
    async def test_compact_preserves_problem_when_append_lacks_it(self) -> None:
        with tempfile.TemporaryDirectory() as cache_dir:
            cache_file_path = os.path.join(cache_dir, "demo.json")
            append_file_path = _append_cache_file_path(cache_file_path)

            speaker = ""
            pre_src = "こんにちは"
            cache_key = _make_cache_key(speaker, pre_src)

            snapshot = [
                {
                    "index": 0,
                    "name": speaker,
                    "pre_src": pre_src,
                    "post_src": pre_src,
                    "pre_dst": "旧译文",
                    "proofread_dst": "",
                    "problem": "残留日文",
                    "trans_by": "model(old)",
                    "proofread_by": "",
                    "post_dst_preview": "旧译文",
                }
            ]
            with open(cache_file_path, "wb") as f:
                f.write(orjson.dumps(snapshot, option=orjson.OPT_INDENT_2))

            # 模拟重翻中途写入的 append 条目：只有新 pre_dst，没有 problem。
            append_entry = {
                "index": 0,
                "name": speaker,
                "pre_src": pre_src,
                "post_src": pre_src,
                "pre_dst": "新译文",
                "proofread_dst": "",
                "trans_by": "model(new)",
                "proofread_by": "",
                "__cache_key": cache_key,
            }
            with open(append_file_path, "ab") as f:
                f.write(orjson.dumps(append_entry))
                f.write(b"\n")

            compacted = await compact_cache_append_logs(cache_dir)
            self.assertEqual(compacted, 1)
            self.assertFalse(os.path.exists(append_file_path))

            with open(cache_file_path, "rb") as f:
                merged = orjson.loads(f.read())

            self.assertEqual(len(merged), 1)
            entry = merged[0]
            # append 提供的字段应覆盖快照
            self.assertEqual(entry["pre_dst"], "新译文")
            self.assertEqual(entry["trans_by"], "model(new)")
            # append 未提供的派生字段（problem）应保留
            self.assertEqual(entry["problem"], "残留日文")

    async def test_get_transCache_preserves_problem_when_reading_append(self) -> None:
        with tempfile.TemporaryDirectory() as cache_dir:
            cache_file_path = os.path.join(cache_dir, "demo.json")
            append_file_path = _append_cache_file_path(cache_file_path)

            speaker = ""
            pre_src = "こんにちは"
            cache_key = _make_cache_key(speaker, pre_src)

            snapshot = [
                {
                    "index": 0,
                    "name": speaker,
                    "pre_src": pre_src,
                    "post_src": pre_src,
                    "pre_dst": "旧译文",
                    "proofread_dst": "",
                    "problem": "残留日文",
                    "trans_by": "model(old)",
                    "proofread_by": "",
                }
            ]
            with open(cache_file_path, "wb") as f:
                f.write(orjson.dumps(snapshot, option=orjson.OPT_INDENT_2))

            append_entry = {
                "index": 0,
                "name": speaker,
                "pre_src": pre_src,
                "post_src": pre_src,
                "pre_dst": "新译文",
                "proofread_dst": "",
                "trans_by": "model(new)",
                "proofread_by": "",
                "__cache_key": cache_key,
            }
            with open(append_file_path, "ab") as f:
                f.write(orjson.dumps(append_entry))
                f.write(b"\n")

            # 单句：构造一个会触发 retranslKey-by-problem 的 CSentense
            tran = CSentense(pre_src, speaker=speaker, index=0)
            trans_list = [tran]

            hit, unhit = await get_transCache_from_json(
                trans_list,
                cache_file_path,
                retran_key="残留日文",
            )

            # 因快照中的 problem 在合并后仍保留，所以 retranslKey-by-problem
            # 仍应命中并将此句标记为需要重翻。
            self.assertEqual(len(unhit), 1)
            self.assertEqual(len(hit), 0)

    async def test_run_job_async_compacts_append_cache_when_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as cache_dir:
            cache_file_path = os.path.join(cache_dir, "demo.json")
            append_file_path = _append_cache_file_path(cache_file_path)

            snapshot = [
                {
                    "index": 0,
                    "name": "",
                    "pre_src": "こんにちは",
                    "post_src": "こんにちは",
                    "pre_dst": "旧译文",
                    "proofread_dst": "",
                    "trans_by": "model(old)",
                    "proofread_by": "",
                }
            ]
            with open(cache_file_path, "wb") as f:
                f.write(orjson.dumps(snapshot, option=orjson.OPT_INDENT_2))

            append_entry = {
                "index": 0,
                "name": "",
                "pre_src": "こんにちは",
                "post_src": "こんにちは",
                "pre_dst": "新译文",
                "proofread_dst": "",
                "trans_by": "model(new)",
                "proofread_by": "",
                "__cache_key": "NoneこんにちはNone",
            }
            with open(append_file_path, "ab") as f:
                f.write(orjson.dumps(append_entry))
                f.write(b"\n")

            fake_cfg = SimpleNamespace(
                projectConfig={"backendSpecific": {}},
                keyValues={},
                non_interactive=False,
                runtime_project_dir="",
                print_translation_log_in_terminal=True,
                getCommonConfigSection=lambda: {"loggingLevel": "info"},
                getKey=lambda key, default=None: 1 if key == "workersPerProject" else default,
                getCachePath=lambda: cache_dir,
            )

            spec = JobSpec(
                job_id="job123",
                project_dir="dummy-project",
                config_file_name="config.inc.yaml",
                translator="gpt4",
            )

            with patch("GalTransl.server.reset_runtime_project"), patch(
                "GalTransl.server.update_runtime_status"
            ), patch("GalTransl.Service.CProjectConfig", return_value=fake_cfg), patch(
                "GalTransl.Service.load_app_settings", return_value={}
            ), patch(
                "GalTransl.Service.run_galtransl",
                new=AsyncMock(side_effect=JobCancelledError()),
            ):
                state = await run_job_async(spec)

            self.assertEqual(state.status, "cancelled")
            self.assertFalse(os.path.exists(append_file_path))

            with open(cache_file_path, "rb") as f:
                merged = orjson.loads(f.read())

            self.assertEqual(merged[0]["pre_dst"], "新译文")
            self.assertEqual(merged[0]["trans_by"], "model(new)")


if __name__ == "__main__":
    unittest.main()
