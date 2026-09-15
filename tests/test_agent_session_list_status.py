"""会话列表的实时状态（侧边栏状态灯的数据源）。

侧边栏据 list_sessions 里的 status 决定亮什么灯：running 蓝灯常亮、跑完绿灯、
failed 橙灯、无状态不亮。这里锁住两点：

1. status 跟着内存里那个回合的状态走（start 后 running、收尾后终态）；
2. **只认内存**——磁盘上的 running 标记在进程重启后是过期的（没有 runner 在跑），
   拿它当"运行中"会让灯一直亮着蓝的。
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from GalTransl.Agent import session_store as ss
from GalTransl.Agent.runtime import AgentRunner, AgentRuntime

PROFILE = {
    "OpenAI-Compatible": {
        "tokens": [{"modelName": "fake-model", "token": "fake-token", "endpoint": "https://example.com/v1"}],
    }
}


class SessionListStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self._root = tempfile.mkdtemp(prefix="agent-liststatus-root-")
        self._orig_root = ss.SESSIONS_ROOT
        ss.SESSIONS_ROOT = self._root
        self.project = os.path.join(tempfile.mkdtemp(prefix="agent-liststatus-proj-"), "MyGame")
        os.makedirs(self.project, exist_ok=True)

    def tearDown(self) -> None:
        ss.SESSIONS_ROOT = self._orig_root

    def _status(self, rt: AgentRuntime, sid: str) -> str:
        match = [it for it in rt.list_sessions(self.project) if it.get("session_id") == sid]
        self.assertEqual(len(match), 1, "该会话应该出现在列表里")
        return str(match[0].get("status") or "")

    def test_status_follows_live_turn(self) -> None:
        rt = AgentRuntime()
        sid = rt.create_session(self.project)["session_id"]
        self.assertEqual(self._status(rt, sid), "")  # 还没跑过 → 不亮灯

        with patch.object(AgentRunner, "run", lambda self: None):
            rt.start(self.project, "config.yaml", PROFILE, goal="跑一轮", session_id=sid)
            self.assertEqual(self._status(rt, sid), "running")  # 蓝灯
            state = rt._get_state(self.project, sid)
            assert state is not None
            state.status = "failed"  # 收尾成失败

        self.assertEqual(self._status(rt, sid), "failed")  # 橙灯

    def test_disk_running_flag_is_not_live_status(self) -> None:
        """上次进程被强杀留下的 running=True 不算"运行中"（内存里没有 runner）。"""
        rt = AgentRuntime()
        sid = rt.create_session(self.project)["session_id"]
        ss.SessionStore(self.project, sid).append_meta(running=True)

        self.assertEqual(self._status(rt, sid), "")

    def test_status_is_per_session(self) -> None:
        """同一项目下多个会话各带自己的状态：一个在跑，另一个不跟着亮。"""
        rt = AgentRuntime()
        running_sid = rt.create_session(self.project)["session_id"]
        idle_sid = rt.create_session(self.project)["session_id"]

        with patch.object(AgentRunner, "run", lambda self: None):
            rt.start(self.project, "config.yaml", PROFILE, goal="跑一轮", session_id=running_sid)

        self.assertEqual(self._status(rt, running_sid), "running")
        self.assertEqual(self._status(rt, idle_sid), "")


if __name__ == "__main__":
    unittest.main()
