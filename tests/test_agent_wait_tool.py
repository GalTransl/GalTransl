"""wait 工具事件的调用归属。

回归背景：界面以前用"找第一个 wait 行"来挂倒计时。同一个活动组里等待过多次时，
第二次的 wait_start/tick/end 全打到第一次那一行上——第一次的行被反复更新，第二次
只剩一个笼统的"进行中"、没有倒计时条。事件带上工具调用 id 后，界面才能按 id 精确
挂载（老日志没有 id 时退回"最近一个还没收到 wait_end 的 wait 行"）。
"""

import unittest

from GalTransl.Agent.runtime import AgentRunner, AgentState, _tool_wait


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


if __name__ == "__main__":
    unittest.main()
