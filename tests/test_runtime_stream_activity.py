"""「文件进度」那颗小灯的数据源：某文件正在流式输出时的阶段与速度。

流式请求里每 0.2 秒报一次（BaseTranslate._report_stream_progress → record_runtime_stream →
RuntimeRegistry.note_stream_activity），快照里以 {显示名: {phase, cps}} 给出来；停下来超过
STREAM_ACTIVITY_TTL_SECONDS 就不再给——免得灯一直闪着一个其实没在动的文件。
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
from GalTransl.server_runtime import STREAM_ACTIVITY_TTL_SECONDS, _StreamActivity, encode_project_dir


class StreamRateTests(unittest.TestCase):
    """_StreamActivity 本身：阶段切换重起窗、速度按滑动窗口算、过期判定。"""

    def test_rate_uses_the_window_and_min_half_second(self):
        activity = _StreamActivity()
        # 一次报 30 字：时间跨度按最少 0.5s 算，别报出个天文数字
        activity.note("thinking", 30, 100.0)
        self.assertEqual(activity.phase, "thinking")
        self.assertEqual(activity.rate_per_second(100.0), 60.0)

        activity.note("thinking", 30, 101.0)
        self.assertEqual(activity.rate_per_second(101.0), 60.0)  # 60 字 / 1s

    def test_phase_switch_starts_a_new_window(self):
        activity = _StreamActivity()
        activity.note("thinking", 40, 100.0)
        # 思考 → 翻译：不把思考阶段的字数算进翻译速度，否则绿灯一上来就「飞快」
        activity.note("writing", 10, 101.0)
        self.assertEqual(activity.phase, "writing")
        self.assertEqual(activity.rate_per_second(101.0), 20.0)

    def test_samples_outside_the_window_are_dropped(self):
        activity = _StreamActivity()
        activity.note("writing", 10, 100.0)
        activity.note("writing", 10, 110.0)  # 10s 后：第一笔已超出 3s 窗口
        self.assertEqual(activity.rate_per_second(110.0), 20.0)

    def test_freshness(self):
        activity = _StreamActivity()
        self.assertFalse(activity.is_fresh(100.0))  # 一次都没报过
        activity.note("writing", 1, 100.0)
        self.assertTrue(activity.is_fresh(100.0 + STREAM_ACTIVITY_TTL_SECONDS))
        self.assertFalse(activity.is_fresh(100.0 + STREAM_ACTIVITY_TTL_SECONDS + 0.1))


class StreamRegistryTests(unittest.TestCase):
    """注册表侧：名字要对得上「文件进度」那一行、过期要摘掉、认不出就别点灯。"""

    def setUp(self) -> None:
        self.registry = RuntimeRegistry()
        self.project_dir = r"E:\tmp\stream_project"
        self.registry.update_status(
            self.project_dir,
            file_totals={"sc_0.txt.json": 100},
            cache_file_display_map={"sc_0.txt.json": "sc_0.txt.json"},
        )

    def _streams(self):
        return self.registry.get_runtime_snapshot(self.project_dir)["streams"]

    def test_reports_phase_and_rate_for_the_row(self):
        self.registry.note_stream_activity(
            self.project_dir, filename="sc_0.txt.json", phase="writing", chars=10
        )
        self.assertEqual(self._streams(), {"sc_0.txt.json": {"phase": "writing", "cps": 20.0}})

    def test_engine_style_name_with_index_range_lands_on_the_row(self):
        # ForGalJson / ForGalTsv / ForNovel 传的是 f"{filename}:{idx_tip}"
        self.registry.note_stream_activity(
            self.project_dir, filename="sc_0.txt.json:1-100", phase="thinking", chars=5
        )
        self.assertEqual(list(self._streams()), ["sc_0.txt.json"])

    def test_unknown_file_is_not_lit(self):
        # 名字认不出就宁可不点灯，也别点到别的文件上
        self.registry.note_stream_activity(
            self.project_dir, filename="nope.json", phase="writing", chars=5
        )
        self.assertEqual(self._streams(), {})

    def test_stale_activity_is_dropped_from_the_snapshot(self):
        self.registry.note_stream_activity(
            self.project_dir, filename="sc_0.txt.json", phase="writing", chars=5
        )
        state = self.registry.ensure_project(self.project_dir)
        state.streams["sc_0.txt.json"].updated_at -= STREAM_ACTIVITY_TTL_SECONDS + 1
        self.assertEqual(self._streams(), {})


class RuntimeStreamPayloadTests(unittest.TestCase):
    """GET /runtime 要把流式状态挂到对应的那一行上——界面那颗灯全靠这个字段。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = tempfile.mkdtemp(prefix="galtransl-stream-http-")
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

    def test_stream_state_lands_on_the_row(self):
        # 真实流程里这两步分别是 LLMTranslate 登记文件、BaseTranslate 逐 chunk 上报
        RUNTIME_REGISTRY.reset_project(self.project)
        RUNTIME_REGISTRY.update_status(
            self.project,
            file_totals={"sc_2_st02.txt.json": 42},
            cache_file_display_map={"sc_2_st02.txt.json": "sc_2_st02.txt.json"},
        )
        RUNTIME_REGISTRY.note_stream_activity(
            self.project, filename="sc_2_st02.txt.json:1-33", phase="thinking", chars=8
        )

        with urllib.request.urlopen(f"{self.base}/runtime", timeout=30) as resp:
            payload = json.load(resp)

        rows = {row["filename"]: row for row in payload["files"]}
        self.assertIn("sc_2_st02.txt.json", rows)
        self.assertEqual(rows["sc_2_st02.txt.json"]["stream"], {"phase": "thinking", "cps": 16.0})
        RUNTIME_REGISTRY.reset_project(self.project)


if __name__ == "__main__":
    unittest.main()
