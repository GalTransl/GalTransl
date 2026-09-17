"""wait 工具事件的调用归属。

回归背景：界面以前用"找第一个 wait 行"来挂倒计时。同一个活动组里等待过多次时，
第二次的 wait_start/tick/end 全打到第一次那一行上——第一次的行被反复更新，第二次
只剩一个笼统的"进行中"、没有倒计时条。事件带上工具调用 id 后，界面才能按 id 精确
挂载（老日志没有 id 时退回"最近一个还没收到 wait_end 的 wait 行"）。
"""

import unittest
from unittest.mock import patch

from GalTransl.Agent import runtime as rt
from GalTransl.Agent.runtime import AgentRunner, AgentState, AgentToolError, _tool_wait


class WaitEventIdTests(unittest.TestCase):
    def test_wait_events_carry_tool_call_id(self):
        state = AgentState()
        runner = AgentRunner(state)  # 无 session -> 不落盘
        runner._active_tool_call_id = "call_wait_2"

        result = _tool_wait(runner, {"seconds": 0.1, "reason": "等待试译完成"})

        self.assertTrue(result["wait_completed"])
        durable = [e for e in state.events if e.type.startswith("wait_")]
        ticks = [e for e in state.transient_events if e.type == "wait_tick"]
        self.assertEqual([e.type for e in durable], ["wait_start", "wait_end"])
        self.assertTrue(ticks, "等待期间应至少推一次 wait_tick（界面倒计时靠它）")
        for ev in [*durable, *ticks]:
            self.assertEqual(ev.data.get("id"), "call_wait_2")

    def test_wait_outside_tool_loop_emits_empty_id(self):
        """工具循环外直接调用（测试/旧路径）时 id 为空串，界面自行退回兜底匹配。"""
        runner = AgentRunner(AgentState())
        _tool_wait(runner, {"seconds": 0.05})
        wait_events = [e for e in runner.state.events if e.type.startswith("wait_")]
        self.assertEqual([e.type for e in wait_events], ["wait_start", "wait_end"])
        self.assertTrue(all(e.data.get("id") == "" for e in wait_events))


class WaitForJobTests(unittest.TestCase):
    """wait(job_id=...)：等某个任务——它先结束就提前返回，时长先到就照常返回（谁先到算谁）。

    查任务状态走 /api/jobs 列表（不是 /api/jobs/{id}：后者对未知 id 直接 404 抛错，
    而"id 不在列表里"对等待来说是个正常结局）。轮询间隔与倒计时步长在测试里都调小。
    """

    def _runner(self) -> AgentRunner:
        runner = AgentRunner(AgentState())  # 无 session -> 不落盘
        runner._active_tool_call_id = "call_wait_job"
        return runner

    # 时长先到时 wait 会顺手带一份运行时快照（等同 get_runtime）：这个假后端也把它备好
    _RUNTIME = {
        "job": {"status": "running", "translator": "ForGal-json"},
        "stage": "translating",
        "current_file": "a.json",
        "summary": {"total": 100, "translated": 40, "percent": 40, "eta_seconds": 321},
        "recent_errors": [],
    }

    def _jobs_getter(
        self, statuses: list[str], *, job_id: str = "j1", error: str = "", runtime_available: bool = True
    ):
        """按顺序给出任务状态（用完后一直重复最后一条）的假 /api/jobs；/runtime 给一份最小快照。

        runtime_available=False 表示"这份快照拿不到"（走查运行时失败的分支）。
        """
        calls = {"n": 0}

        def fake_get(path: str):
            if path.endswith("/runtime"):
                if not runtime_available:
                    raise RuntimeError("runtime down")
                return self._RUNTIME
            self.assertEqual(path, "/api/jobs")
            idx = min(calls["n"], len(statuses) - 1)
            calls["n"] += 1
            status = statuses[idx]
            return {
                "jobs": [
                    {"job_id": job_id, "status": status, "success": status == "completed", "error": error}
                ]
            }

        return fake_get

    def _fast(self):
        """轮询与倒计时都快一点，测试别真等秒。"""
        return (
            patch.object(rt, "WAIT_JOB_POLL_SECONDS", 0.01),
            patch.object(rt, "WAIT_TICK", 0.01),
        )

    def _wait_end(self, runner: AgentRunner) -> dict:
        return [e for e in runner.state.events if e.type == "wait_end"][0].data

    def test_job_finishing_first_ends_the_wait_early(self):
        runner = self._runner()
        runner._http_get = self._jobs_getter(["running", "completed"])

        fast_a, fast_b = self._fast()
        with fast_a, fast_b:
            result = _tool_wait(runner, {"job_id": "j1", "seconds": 30, "reason": "等待翻译任务完成"})

        self.assertTrue(result["job_finished"])
        self.assertEqual(result["job_status"], "completed")
        self.assertTrue(result["job_success"])
        self.assertLess(result["waited_seconds"], 5)  # 没等满 30 秒
        # 事件里说清"为什么结束"：界面与模型都能区分"任务完成"与"计时到"
        end = self._wait_end(runner)
        self.assertEqual(end["job_end_reason"], "done")
        self.assertEqual(end["job_status"], "completed")

    def test_timeout_returns_the_current_job_status(self):
        runner = self._runner()
        runner._http_get = self._jobs_getter(["running"])

        fast_a, fast_b = self._fast()
        with fast_a, fast_b:
            result = _tool_wait(runner, {"job_id": "j1", "seconds": 0.2})

        self.assertTrue(result["wait_completed"])
        self.assertFalse(result["job_finished"])
        self.assertEqual(result["job_status"], "running")
        self.assertEqual(self._wait_end(runner)["job_end_reason"], "timeout")

    def test_timeout_carries_a_runtime_snapshot(self):
        """时长先到时直接带上运行时快照（等同 get_runtime）——省模型一个来回。"""
        runner = self._runner()
        runner._http_get = self._jobs_getter(["running"])

        fast_a, fast_b = self._fast()
        with fast_a, fast_b:
            result = _tool_wait(runner, {"job_id": "j1", "seconds": 0.2})

        self.assertEqual(result["runtime"]["summary"]["eta_seconds"], 321)
        self.assertEqual(result["runtime"]["job_status"], "running")
        self.assertEqual(result["runtime"]["current_file"], "a.json")
        self.assertIn("eta_seconds", result["note"])

    def test_timeout_without_a_runtime_snapshot_still_succeeds(self):
        """快照取不到（后端抖了）只是少带一块：wait 本身的结论不受影响。"""
        runner = self._runner()
        runner._http_get = self._jobs_getter(["running"], runtime_available=False)

        fast_a, fast_b = self._fast()
        with fast_a, fast_b:
            result = _tool_wait(runner, {"job_id": "j1", "seconds": 0.2})

        self.assertTrue(result["wait_completed"])
        self.assertNotIn("runtime", result)
        self.assertIn("get_runtime", result["note"])  # 没带快照就让它自己去查

    def test_failed_job_returns_its_error_and_stops_early(self):
        runner = self._runner()
        runner._http_get = self._jobs_getter(["failed"], error="配置读取失败")

        fast_a, fast_b = self._fast()
        with fast_a, fast_b:
            result = _tool_wait(runner, {"job_id": "j1", "minutes": 5})

        self.assertTrue(result["job_finished"])
        self.assertEqual(result["job_status"], "failed")
        self.assertEqual(result["job_error"], "配置读取失败")
        self.assertLess(result["waited_seconds"], 5)

    def test_unknown_job_id_ends_the_wait_without_waiting_it_out(self):
        runner = self._runner()
        runner._http_get = lambda _path: {"jobs": [{"job_id": "other", "status": "running"}]}

        fast_a, fast_b = self._fast()
        with fast_a, fast_b:
            result = _tool_wait(runner, {"job_id": "nope", "seconds": 30})

        self.assertFalse(result["job_found"])
        self.assertLess(result["waited_seconds"], 5)
        self.assertEqual(self._wait_end(runner)["job_end_reason"], "missing")

    def test_status_query_failure_does_not_break_the_wait(self):
        """查状态失败（后端抖了）当"还在跑"处理：时长到了照样正常收尾。"""
        def boom(_path: str):
            raise RuntimeError("backend down")

        runner = self._runner()
        runner._http_get = boom

        fast_a, fast_b = self._fast()
        with fast_a, fast_b:
            result = _tool_wait(runner, {"job_id": "j1", "seconds": 0.15})

        self.assertTrue(result["wait_completed"])
        self.assertEqual(result["job_status"], "running")

    def test_wait_start_carries_the_job_id(self):
        runner = self._runner()
        runner._http_get = self._jobs_getter(["completed"])

        with patch.object(rt, "WAIT_JOB_POLL_SECONDS", 0.01):
            _tool_wait(runner, {"job_id": "j1", "seconds": 30})

        start = [e for e in runner.state.events if e.type == "wait_start"][0].data
        self.assertEqual(start["job_id"], "j1")
        self.assertEqual(start["reason"], "")

    def test_without_job_id_behaviour_is_unchanged_and_no_jobs_are_queried(self):
        runner = self._runner()
        asked: list[str] = []
        runner._http_get = lambda path: (asked.append(path), {})[1]

        result = _tool_wait(runner, {"seconds": 0.05})

        self.assertTrue(result["wait_completed"])
        self.assertNotIn("job_id", result)
        self.assertEqual(asked, [])  # 没给 job_id 就不该去查任务列表

    def test_job_id_alone_still_requires_a_duration(self):
        runner = self._runner()
        with self.assertRaises(AgentToolError) as ctx:
            _tool_wait(runner, {"job_id": "j1"})
        self.assertIn("等待时长", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
