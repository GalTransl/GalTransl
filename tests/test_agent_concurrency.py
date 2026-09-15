"""Agent 并发契约：不同项目（乃至同一项目的不同会话）可以各自独立跑。

回归背景：界面曾把「运行中」当成全局状态——任何 Agent 在跑就禁用所有「新建会话」，
于是没法一边让某个项目跑着、一边去别的项目开新会话。后端本来就是按
(项目, 会话) 各自一个线程、各自一套状态与落盘，这里把这条契约锁住；
真正被拒绝的只有「同一个会话再起一个回合」。
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from GalTransl.Agent import session_store as ss
from GalTransl.Agent.runtime import AgentRunner, AgentRuntime

# 只为通过 start() 的入参校验；run() 被打桩，不会真的发请求
PROFILE = {"OpenAI-Compatible": {"tokens": [{"modelName": "fake-model", "token": "fake-token"}]}}


class CrossProjectConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._root = tempfile.mkdtemp(prefix="agent-conc-root-")
        self._orig_root = ss.SESSIONS_ROOT
        ss.SESSIONS_ROOT = self._root
        self.proj_a = os.path.join(tempfile.mkdtemp(prefix="agent-conc-a-"), "Alpha")
        self.proj_b = os.path.join(tempfile.mkdtemp(prefix="agent-conc-b-"), "Beta")
        for p in (self.proj_a, self.proj_b):
            os.makedirs(p, exist_ok=True)

    def tearDown(self) -> None:
        ss.SESSIONS_ROOT = self._orig_root

    def test_other_project_can_start_while_one_is_running(self):
        rt = AgentRuntime()
        # run() 打桩：不真的跑回合，只验证"允不允许起第二个"
        with patch.object(AgentRunner, "run", lambda self: None):
            sid_a = rt.create_session(self.proj_a)["session_id"]
            a = rt.start(self.proj_a, "config.yaml", PROFILE, goal="A 项目", session_id=sid_a)
            sid_b = rt.create_session(self.proj_b)["session_id"]
            b = rt.start(self.proj_b, "config.yaml", PROFILE, goal="B 项目", session_id=sid_b)

        self.assertEqual(a["status"], "running")
        self.assertEqual(b["status"], "running")
        # 两个会话各有各的状态，互不覆盖
        self.assertNotEqual(a["session_id"], b["session_id"])
        self.assertEqual(rt.status(self.proj_a, sid_a)["goal"], "A 项目")
        self.assertEqual(rt.status(self.proj_b, sid_b)["goal"], "B 项目")

    def test_same_session_refuses_second_turn(self):
        """同一个会话同时只能跑一个回合：第二次 start 必须被拒。"""
        rt = AgentRuntime()
        sid = rt.create_session(self.proj_a)["session_id"]
        with patch.object(AgentRunner, "run", lambda self: None):
            rt.start(self.proj_a, "config.yaml", PROFILE, goal="第一次", session_id=sid)
            with self.assertRaises(ValueError):
                rt.start(self.proj_a, "config.yaml", PROFILE, goal="第二次", session_id=sid)

    def test_second_session_in_same_project_is_independent(self):
        """同一项目下另一个会话也能起：并发限制是按会话算的，不是按项目。"""
        rt = AgentRuntime()
        with patch.object(AgentRunner, "run", lambda self: None):
            sid_1 = rt.create_session(self.proj_a)["session_id"]
            sid_2 = rt.create_session(self.proj_a)["session_id"]
            rt.start(self.proj_a, "config.yaml", PROFILE, goal="会话一", session_id=sid_1)
            second = rt.start(self.proj_a, "config.yaml", PROFILE, goal="会话二", session_id=sid_2)

        self.assertEqual(second["status"], "running")
        self.assertEqual(rt.status(self.proj_a, sid_1)["goal"], "会话一")
        self.assertEqual(rt.status(self.proj_a, sid_2)["goal"], "会话二")


if __name__ == "__main__":
    unittest.main()
