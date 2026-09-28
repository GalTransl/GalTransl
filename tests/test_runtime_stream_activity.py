"""「文件进度」那颗小灯的数据源：某个文件此刻有没有请求在跑、跑到哪个阶段、出字多快。

一次 ask_chatbot 进门登记、出门注销（BaseTranslate._FileRequestProgress →
begin/note/end_runtime_request → RuntimeRegistry），快照里按文件合并成
{显示名: {phase, cps, requests}}：
- 同一个文件同时几个请求（切块并发、GenDic 多线程）合成一行，不在几个阶段之间来回跳；
- 在出字的请求停住超过 REQUEST_STALL_SECONDS 就退回「请求中」；
- 注销了就不再给——灯不会挂在一个已经不在跑的文件上。
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from GalTransl import INPUT_FOLDERNAME
from GalTransl.server import RUNTIME_REGISTRY, JobRegistry, RuntimeRegistry, build_handler
from GalTransl.server_runtime import (
    REQUEST_STALL_SECONDS,
    _REQUEST_ABANDON_SECONDS,
    _RequestActivity,
    encode_project_dir,
)


class RequestActivityTests(unittest.TestCase):
    """_RequestActivity 本身：速度按滑动窗口算、换阶段重起窗、停住退回请求中。"""

    @staticmethod
    def _activity(now: float = 100.0) -> _RequestActivity:
        return _RequestActivity(filename="a.json", phase="waiting", updated_at=now, phase_started_at=now)

    def test_rate_counts_from_phase_start_with_half_second_floor(self):
        activity = self._activity()
        activity.note("writing", 30, 100.0)
        # 只来了一笔：分母至少按 0.5s 算，别报出个天文数字
        self.assertEqual(activity.rate_per_second(100.0), 60.0)
        activity.note("writing", 30, 101.0)
        self.assertEqual(activity.rate_per_second(101.0), 60.0)  # 60 字 / 1s

    def test_phase_switch_restarts_the_window(self):
        activity = self._activity()
        activity.note("thinking", 400, 100.0)
        # 思考 → 正文：思考的字数不算进翻译速度，否则绿灯一上来就「飞快」
        activity.note("writing", 10, 102.0)
        self.assertEqual(activity.phase, "writing")
        self.assertEqual(activity.rate_per_second(102.0), 20.0)

    def test_old_samples_leave_the_window(self):
        activity = self._activity()
        activity.note("writing", 10, 100.0)
        activity.note("writing", 30, 110.0)
        # 10s 前那一笔早出了 3s 窗口；这一阶段开始得早，分母就是整个窗口
        self.assertEqual(activity.rate_per_second(110.0), 10.0)

    def test_stalled_output_falls_back_to_waiting(self):
        activity = self._activity()
        activity.note("writing", 5, 100.0)
        self.assertEqual(activity.shown_phase(100.0 + REQUEST_STALL_SECONDS), "writing")
        stalled = 100.0 + REQUEST_STALL_SECONDS + 0.1
        self.assertEqual(activity.shown_phase(stalled), "waiting")
        self.assertEqual(activity.rate_per_second(stalled), 0.0)

    def test_waiting_and_retrying_never_count_as_stalled(self):
        activity = self._activity()
        self.assertEqual(activity.shown_phase(1000.0), "waiting")
        activity.note("retrying", 0, 100.0)
        self.assertEqual(activity.shown_phase(1000.0), "retrying")


class RequestRegistryTests(unittest.TestCase):
    """注册表侧：名字要对得上「文件进度」那一行、多个请求合成一行、注销与重置要摘干净。"""

    def setUp(self) -> None:
        self.registry = RuntimeRegistry()
        self.project_dir = r"E:\tmp\request_project"
        self.registry.update_status(
            self.project_dir,
            file_totals={"sc_0.txt.json": 100, "big.json": 300},
            cache_file_display_map={
                "sc_0.txt.json": "sc_0.txt.json",
                # 切块：缓存名 big.json_<n>.json，都归 big.json 那一行
                "big.json_0.json": "big.json",
                "big.json_1.json": "big.json",
            },
        )

    def _activity(self):
        return self.registry.get_runtime_snapshot(self.project_dir)["activity"]

    def test_request_lifecycle(self):
        request_id = self.registry.begin_request(self.project_dir, filename="sc_0.txt.json")
        # 刚发出去、还没出字：请求中
        self.assertEqual(
            self._activity(), {"sc_0.txt.json": {"phase": "waiting", "cps": 0.0, "requests": 1}}
        )
        self.registry.note_request(request_id, phase="thinking", chars=10)
        self.assertEqual(self._activity()["sc_0.txt.json"]["phase"], "thinking")
        self.assertGreater(self._activity()["sc_0.txt.json"]["cps"], 0)
        self.registry.end_request(request_id)
        # 注销了就不再给：灯不会挂在一个已经不在跑的文件上
        self.assertEqual(self._activity(), {})

    def test_chunk_names_land_on_the_file_row(self):
        # 切块后引擎用的文件名是 big.json_1：灯要点在 big.json 那一行
        self.assertIsNotNone(self.registry.begin_request(self.project_dir, filename="big.json_1"))
        self.assertEqual(list(self._activity()), ["big.json"])

    def test_concurrent_requests_on_one_file_merge_into_one_row(self):
        first = self.registry.begin_request(self.project_dir, filename="big.json_0")
        second = self.registry.begin_request(self.project_dir, filename="big.json_1")
        self.registry.note_request(first, phase="thinking", chars=30)
        self.registry.note_request(second, phase="writing", chars=20)
        row = self._activity()["big.json"]
        # 有一个在出正文就是「翻译中」，字/秒加总（各自刚起步，都按 0.5s 算），两个请求
        self.assertEqual(row, {"phase": "writing", "cps": 100.0, "requests": 2})
        self.registry.end_request(second)
        self.assertEqual(self._activity()["big.json"]["phase"], "thinking")
        self.assertEqual(self._activity()["big.json"]["requests"], 1)

    def test_retrying_only_when_every_request_is_backing_off(self):
        first = self.registry.begin_request(self.project_dir, filename="big.json_0")
        second = self.registry.begin_request(self.project_dir, filename="big.json_1")
        self.registry.note_request(first, phase="retrying")
        self.assertEqual(self._activity()["big.json"]["phase"], "waiting")
        self.registry.note_request(second, phase="retrying")
        self.assertEqual(self._activity()["big.json"]["phase"], "retrying")

    def test_unknown_file_is_not_lit(self):
        # 认不出是哪一行就不登记：宁可没有灯，也别点到别的文件上
        self.assertIsNone(self.registry.begin_request(self.project_dir, filename="nope.json"))
        self.assertIsNone(self.registry.begin_request(r"E:\tmp\other", filename="sc_0.txt.json"))
        self.assertEqual(self._activity(), {})

    def test_requests_of_the_previous_job_are_ignored_after_reset(self):
        request_id = self.registry.begin_request(self.project_dir, filename="sc_0.txt.json")
        self.registry.reset_project(self.project_dir)
        self.registry.update_status(self.project_dir, file_totals={"sc_0.txt.json": 100})
        # 上一轮没收尾的请求再报上来 / 注销：对不上号，不会点到新任务的行上
        self.registry.note_request(request_id, phase="writing", chars=5)
        self.registry.end_request(request_id)
        self.assertEqual(self._activity(), {})
        self.assertEqual(self.registry._request_owners, {})

    def test_abandoned_request_is_dropped(self):
        request_id = self.registry.begin_request(self.project_dir, filename="sc_0.txt.json")
        state = self.registry.ensure_project(self.project_dir)
        state.requests[request_id].updated_at -= _REQUEST_ABANDON_SECONDS + 1
        self.assertEqual(self._activity(), {})
        self.assertNotIn(request_id, self.registry._request_owners)


class RuntimeActivityPayloadTests(unittest.TestCase):
    """GET /runtime 要把实时状态挂到对应的那一行上——界面那颗灯全靠这个字段。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = tempfile.mkdtemp(prefix="galtransl-activity-http-")
        cls.project = os.path.join(cls.root, "proj")
        os.makedirs(os.path.join(cls.project, INPUT_FOLDERNAME), exist_ok=True)
        with open(os.path.join(cls.project, "config.yaml"), "w", encoding="utf-8") as f:
            f.write("common:\n  language: ja\nplugin:\n  filePlugin: file_galtransl_json\n  textPlugins: []\n")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(JobRegistry()))
        cls.base = (
            f"http://127.0.0.1:{cls.httpd.server_address[1]}/api/projects/"
            f"{encode_project_dir(cls.project)}"
        )
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()

    def test_activity_lands_on_the_row(self):
        # 真实流程里这几步分别是 LLMTranslate 登记文件、BaseTranslate 进门登记与流式上报
        RUNTIME_REGISTRY.reset_project(self.project)
        try:
            RUNTIME_REGISTRY.update_status(
                self.project,
                file_totals={"sc_2_st02.txt.json": 42, "other.json": 7},
                cache_file_display_map={
                    "sc_2_st02.txt.json": "sc_2_st02.txt.json",
                    "other.json": "other.json",
                },
            )
            request_id = RUNTIME_REGISTRY.begin_request(self.project, filename="sc_2_st02.txt.json")
            RUNTIME_REGISTRY.note_request(request_id, phase="thinking", chars=8)

            with urllib.request.urlopen(f"{self.base}/runtime", timeout=30) as resp:
                payload = json.load(resp)

            rows = {row["filename"]: row for row in payload["files"]}
            activity = rows["sc_2_st02.txt.json"]["activity"]
            self.assertEqual(activity["phase"], "thinking")
            self.assertEqual(activity["requests"], 1)
            self.assertGreater(activity["cps"], 0)
            # 没有请求在跑的行不带这个字段
            self.assertNotIn("activity", rows["other.json"])
        finally:
            RUNTIME_REGISTRY.reset_project(self.project)


if __name__ == "__main__":
    unittest.main()
