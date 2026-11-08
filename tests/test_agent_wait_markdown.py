"""translation(action="wait"/"status") 给模型的结果渲染成精简 Markdown。"""

import json
import unittest

from GalTransl.Agent.tools.render_md import _render_tool_result_table

_ERROR_GROUP = {
    "kind": "parse",
    "level": "error",
    "message": "TranslationParseError: The prompt could not be submitted.",
    "count": 4,
    "files": ["ACT_G.json", "ACT_F.json"],
    "files_total": 2,
    "first_ts": "2026-11-08T03:08:43Z",
    "last_ts": "2026-11-08T03:11:32Z",
    "text": "parse 错误 × 4 次：TranslationParseError: The prompt could not be submitted.（涉及文件：ACT_G.json、ACT_F.json）",
}

_RUNTIME = {
    "job_status": "running",
    "job_translator": "auto-translate",
    "stage": "translating",
    "current_file": "ACT_F.json",
    "summary": {"total": 3400, "translated": 1200, "percent": 35.3, "problems": 12,
                "failed": 3, "eta_seconds": 754, "workers_active": 8},
    "recent_errors": [_ERROR_GROUP],
    "recent_errors_pending": 3,
}


class WaitMarkdownTests(unittest.TestCase):
    def test_timeout_with_snapshot_is_shorter_than_json(self) -> None:
        result = {"waited_seconds": 600.0, "job_id": "768b35a9d42f", "job_status": "running",
                  "job_finished": False, "wait_completed": True, "runtime": _RUNTIME,
                  "note": "等待时长到了，任务还在跑：下面附了当前运行时快照……"}
        text = _render_tool_result_table("wait", result)

        self.assertIn("等满 600s，任务 768b35a9d42f 仍在 running", text)
        self.assertIn("进度：1200/3400 句（35.3%）", text)
        self.assertIn("eta_seconds 754（约 12 分 34 秒）", text)
        self.assertIn("- parse 错误 ×4：TranslationParseError: The prompt could not be submitted.（ACT_G.json、ACT_F.json）", text)
        self.assertIn("还有 3 条新报错没列出", text)
        self.assertNotIn("first_ts", text)
        self.assertNotIn("2026-11-08", text)  # 时间戳不给模型
        self.assertEqual(text.count("could not be submitted"), 1)  # message 与 text 不再重复
        self.assertLess(len(text), len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) * 0.7)

    def test_finished_job_reports_status_and_error(self) -> None:
        text = _render_tool_result_table("wait", {
            "waited_seconds": 12.3, "job_id": "j1", "job_status": "failed", "job_success": False,
            "wait_completed": True, "job_finished": True, "job_error": "API 配置错误", "note": "...",
        })
        self.assertIn("任务 j1 已结束：failed（未成功），等了 12.3s", text)
        self.assertIn("job_error：API 配置错误", text)

    def test_other_wait_outcomes(self) -> None:
        self.assertIn("停止打断", _render_tool_result_table("wait", {"waited_seconds": 3.0, "wait_interrupted": True}))
        self.assertIn("找不到 jx", _render_tool_result_table("wait", {"waited_seconds": 0.0, "job_id": "jx", "job_found": False}))
        self.assertIn("计时结束（30s）", _render_tool_result_table("wait", {"waited_seconds": 30.0, "wait_completed": True}))
        no_snapshot = _render_tool_result_table("wait", {"waited_seconds": 60.0, "job_id": "j2", "job_status": "running",
                                                         "job_finished": False, "wait_completed": True})
        self.assertIn('translation(action="status")', no_snapshot)


class RuntimeMarkdownTests(unittest.TestCase):
    def test_status_without_errors_or_eta(self) -> None:
        text = _render_tool_result_table("get_runtime", {
            "job_status": "running", "summary": {"total": 10, "translated": 0, "eta_seconds": None},
            "recent_errors": [],
        })
        self.assertIn("eta_seconds 未知（估算中）", text)
        self.assertIn("recent_errors：无新报错", text)
        self.assertNotIn("recent_errors_pending", text)

    def test_many_files_are_summarised(self) -> None:
        group = dict(_ERROR_GROUP, files=["a", "b", "c", "d", "e"], files_total=9)
        text = _render_tool_result_table("get_runtime", {"job_status": "running", "summary": {}, "recent_errors": [group]})
        self.assertIn("（a、b、c 等 9 个文件）", text)


if __name__ == "__main__":
    unittest.main()
