"""进度只有一个来源，且两套口径各自说清自己是哪一套。

背景：Agent 曾经同时有 get_progress 与 get_project_overview.progress，给的是同一批数字；
更要命的是 get_runtime 的 summary.total 与它们口径不同——前者按**任务计划**统计（含正在
翻译、缓存尚未落盘的文件），后者只算**已落盘缓存**，同一次全量中途查询是 2627 vs 2205，
调用方为了对齐这两个数白搭了一轮。

这里锁两件事：

1. get_progress 已合并（工具表与 schema 里都不再有它），进度统一从 overview 的 progress 拿；
2. 两套口径的说明都随返回给出，互相点明"分母不同、不要为了对齐多查一轮"。
"""

import unittest

from GalTransl.Agent.runtime import (
    AGENT_TOOLS,
    AgentState,
    _TOOL_HANDLERS,
    _tool_get_project_overview,
    _tool_get_runtime,
)


class _Runner:
    """最小 runner：/progress 与 /runtime 给各自口径的数字（任务计划 2627 / 已落盘 2205）。"""

    def __init__(self) -> None:
        self.state = AgentState(config_file_name="config.yaml")

    def _project_id(self) -> str:
        return "proj"

    def _http_get(self, path: str):
        if path.endswith("/runtime"):
            return {
                "job": {"status": "running", "translator": "ForGal-json"},
                "stage": "translate",
                "current_file": "01.json",
                "summary": {
                    "total": 2627,
                    "translated": 2205,
                    "percent": 83.9,
                    "problems": 3,
                    "failed": 0,
                    "eta_seconds": 120,
                    "workers_active": 4,
                },
                "recent_errors": [],
            }
        if "/progress" in path:
            return {"total": 2205, "translated": 1800, "problems": 3, "failed": 0, "files": []}
        if path.endswith("/files"):
            return {"input_files": []}
        raise AssertionError(f"未预期的请求: {path}")


class ProgressSourceMergeTests(unittest.TestCase):
    """工具侧不再有两份进度：get_progress 已合并进 overview。"""

    def test_get_progress_tool_is_gone(self) -> None:
        names = {tool["function"]["name"] for tool in AGENT_TOOLS}
        self.assertNotIn("get_progress", names)
        self.assertNotIn("get_progress", _TOOL_HANDLERS)
        # 进度统一从「了解项目」拿（include 里点 progress 即可）
        self.assertIn("get_project_overview", names)

    def test_overview_is_the_progress_source(self) -> None:
        out = _tool_get_project_overview(_Runner(), {"include": ["progress"]})
        self.assertEqual(out["progress"]["total"], 2205)  # 已落盘口径
        self.assertIn("files_translated", out["progress"])


class ProgressScaleNotesTests(unittest.TestCase):
    """两套口径都必须自报家门，避免以后再为对齐数字多查一轮。"""

    def test_overview_progress_note_points_at_get_runtime(self) -> None:
        out = _tool_get_project_overview(_Runner(), {"include": ["progress"]})
        note = out["progress"]["note"]
        self.assertIn("已落盘", note)  # 说明自己是哪套口径
        self.assertIn("get_runtime", note)  # 另一套去哪看
        self.assertIn("两个分母不同", note)

    def test_runtime_summary_note_points_back_at_overview(self) -> None:
        out = _tool_get_runtime(_Runner(), {})
        self.assertEqual(out["summary"]["total"], 2627)  # 任务计划口径，原样报出
        note = out["summary_note"]
        self.assertIn("本轮任务", note)
        self.assertIn("get_project_overview", note)
        self.assertIn("不要为了对齐", note)

    def test_both_scales_are_reported_without_reconciling(self) -> None:
        """同一时刻两个 total 可以不等——工具不做"抹平"，只把口径讲清楚。"""
        progress = _tool_get_project_overview(_Runner(), {"include": ["progress"]})["progress"]
        runtime = _tool_get_runtime(_Runner(), {})["summary"]
        self.assertNotEqual(progress["total"], runtime["total"])
        self.assertEqual(progress["total"], 2205)
        self.assertEqual(runtime["total"], 2627)


if __name__ == "__main__":
    unittest.main()
