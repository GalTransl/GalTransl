"""用户点停止要「秒级」生效：在途请求必须被打断（照 pi 的 AbortSignal 语义）。

pi 把 AbortSignal 一路传进 fetch，`abort()` 能真实中断在途 HTTP。Python 的
OpenAI SDK 没有可传的 signal，等价手段是关掉该会话的客户端：httpx 会让阻塞在
socket read 上的请求立刻抛 APIConnectionError（已用本地"黑洞"服务实测，close()
0ms 返回、在途读立即被打断）。

这里用一个"一直挂在读上"的假客户端验证接线：stop() → 在途请求立刻失败 →
回合落 stopped，而不是干等到读超时。
"""

import os
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from GalTransl.Agent import session_store as ss
from GalTransl.Agent.runtime import AgentRunner, AgentRuntime, AgentState

try:  # 开发解释器可能没装后端依赖（重建客户端那条用例需要）
    import openai  # noqa: F401

    _HAS_OPENAI = True
except ImportError:  # pragma: no cover - 取决于解释器
    _HAS_OPENAI = False

PROFILE = {
    "OpenAI-Compatible": {
        "tokens": [{"modelName": "fake", "token": "fake", "endpoint": "https://example.com/v1"}],
    }
}
# 假请求最长阻塞时间：接线若断了，测试也会在几秒内失败而不是永久挂住
HANG_SECONDS = 8.0


class _HangingClient:
    """假的 OpenAI 客户端：create() 一直阻塞在"读"上，close() 像 httpx 那样叫醒它。"""

    def __init__(self) -> None:
        self.entered = threading.Event()  # 已进入阻塞读
        self.closed = False
        self.chat = self
        self._release = threading.Event()  # close() 置位

    @property
    def completions(self) -> "_HangingClient":
        return self

    def create(self, *args: object, **kwargs: object) -> object:
        self.entered.set()
        if not self._release.wait(HANG_SECONDS):
            raise RuntimeError("假客户端：没人打断它")
        # 模仿 httpx 在客户端被关掉时抛出的错误
        raise RuntimeError("APIConnectionError: Connection error.")

    def close(self) -> None:
        self.closed = True
        self._release.set()


class StopAbortsInFlightTests(unittest.TestCase):
    def setUp(self) -> None:
        self._root = tempfile.mkdtemp(prefix="agent-stopabort-root-")
        self._orig_root = ss.SESSIONS_ROOT
        ss.SESSIONS_ROOT = self._root
        self.project = os.path.join(tempfile.mkdtemp(prefix="agent-stopabort-proj-"), "MyGame")
        os.makedirs(self.project, exist_ok=True)

    def tearDown(self) -> None:
        ss.SESSIONS_ROOT = self._orig_root

    def test_stop_interrupts_hanging_request_immediately(self):
        """stop() 要在秒级内让挂在读上的请求失败，而不是等超时。"""
        rt = AgentRuntime()
        sid = rt.create_session(self.project)["session_id"]
        client = _HangingClient()

        def fake_resolve(runner: AgentRunner) -> None:
            runner._openai_client = client
            runner._model = "fake"

        with patch.object(AgentRunner, "_resolve_llm", fake_resolve):
            rt.start(self.project, "config.yaml", PROFILE, goal="第一轮", session_id=sid)
            self.assertTrue(client.entered.wait(5), "请求没有进到阻塞读")

            started = time.monotonic()
            rt.stop(self.project, sid)
            status = "running"
            deadline = started + 3.0
            while time.monotonic() < deadline:
                status = rt.status(self.project, sid)["status"]
                if status != "running":
                    break
                time.sleep(0.05)
            elapsed = time.monotonic() - started

        self.assertTrue(client.closed, "stop() 应该关掉在途客户端")
        self.assertEqual(status, "stopped")
        self.assertLess(elapsed, 2.0, f"停止用了 {elapsed:.1f}s，没有即时打断在途请求")
        # 停止不该被当成错误：会话没有 error
        self.assertFalse(rt.status(self.project, sid).get("error"))

    def test_abort_in_flight_is_safe_without_client(self):
        """还没解析出客户端（或已被关过）时调用要无副作用。"""
        runner = AgentRunner(AgentState())
        runner.abort_in_flight()  # 不抛异常
        runner.abort_in_flight()


@unittest.skipUnless(_HAS_OPENAI, "需要 openai 包（后端运行环境）")
class RealHttpStopTests(unittest.TestCase):
    """用真实 httpx 证一遍：客户端被关掉时，挂在 read 上的请求确实会立刻失败。

    这是 abort_in_flight() 成立的前提（换 SDK/httpx 版本可能变），用本地"黑洞"
    服务（接受连接但永不回包）当端点，不依赖外网。
    """

    def setUp(self) -> None:
        self._root = tempfile.mkdtemp(prefix="agent-realstop-root-")
        self._orig_root = ss.SESSIONS_ROOT
        ss.SESSIONS_ROOT = self._root
        self.project = os.path.join(tempfile.mkdtemp(prefix="agent-realstop-proj-"), "MyGame")
        os.makedirs(self.project, exist_ok=True)
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.bind(("127.0.0.1", 0))  # 端口交给系统分配，避免和别的测试撞
        self._srv.listen(5)
        self.port = self._srv.getsockname()[1]
        self._conns: list[socket.socket] = []
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self) -> None:
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            self._conns.append(conn)  # 必须留着引用：回收会把连接关掉，就不是"挂死"了

    def tearDown(self) -> None:
        ss.SESSIONS_ROOT = self._orig_root
        self._srv.close()
        for conn in self._conns:
            conn.close()

    def test_stop_beats_hanging_socket(self):
        profile = {
            "OpenAI-Compatible": {
                "tokens": [
                    {
                        "modelName": "blackhole",
                        "token": "x",
                        "endpoint": f"http://127.0.0.1:{self.port}/v1",
                    }
                ]
            }
        }
        rt = AgentRuntime()
        sid = rt.create_session(self.project)["session_id"]
        rt.start(self.project, "config.yaml", profile, goal="你好", session_id=sid)
        time.sleep(1.0)  # 让请求真正挂到 socket read 上

        started = time.monotonic()
        rt.stop(self.project, sid)
        status = "running"
        while time.monotonic() - started < 5.0:
            status = rt.status(self.project, sid)["status"]
            if status != "running":
                break
            time.sleep(0.05)
        elapsed = time.monotonic() - started

        self.assertEqual(status, "stopped")
        self.assertLess(elapsed, 2.0, f"挂着 socket 时停止用了 {elapsed:.1f}s")


@unittest.skipUnless(_HAS_OPENAI, "需要 openai 包（后端运行环境）")
class ClientRebuildTests(unittest.TestCase):
    def test_resolve_llm_rebuilds_client_each_turn(self):
        """每回合都重建客户端：被 abort_in_flight() 关掉的那份绝不能复用。

        复用会直接抛 RuntimeError（client has been closed），下一回合就废了。
        """
        state = AgentState()
        state.session_id = ""  # 不落盘
        state.backend_profile_data = PROFILE
        runner = AgentRunner(state)

        runner._resolve_llm()
        first = runner._openai_client
        runner.abort_in_flight()

        runner._resolve_llm()
        self.assertIsNot(runner._openai_client, first)
        runner.abort_in_flight()


if __name__ == "__main__":
    unittest.main()
