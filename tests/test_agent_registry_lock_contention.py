"""注册表那把全局锁不能被慢操作占住。

用户反馈过"整页卡死：点停止没反应、新建会话也点不了、别的项目的会话列表一直加载中"——
所有请求都在等同一把注册表锁。会往锁里塞慢活的有两处：

- status() 的上下文用量估算（开了 billion-context / mnemon 时要进上下文插件的锁、跑投影和
  sqlite 事务），而 SSE 每 0.5s 就 status() 一次、message()/stop() 的回包也走它；
- stop() / 「立即」里关在途连接（要进 httpx 连接池逐个关 socket）。

这两步都要在锁外做：慢最多只让这一次请求慢，不能让会话列表、停止、发消息全排队。
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from GalTransl.Agent import session_store as ss
from GalTransl.Agent.runtime import AgentRunner, AgentRuntime

PROFILE = {"OpenAI-Compatible": {"tokens": [{"modelName": "fake-model", "token": "fake-token"}]}}

# 被占住的那一步最长挂多久（测试里由 release 事件提前放行）
HOLD_SECONDS = 5.0
# 其余请求必须在这么短的时间里回来，才算没被连带锁死
FAST_SECONDS = 1.0


class _RuntimeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._root = tempfile.mkdtemp(prefix="agent-lock-root-")
        self._orig_root = ss.SESSIONS_ROOT
        ss.SESSIONS_ROOT = self._root
        self.project = os.path.join(tempfile.mkdtemp(prefix="agent-lock-"), "Game")
        os.makedirs(self.project, exist_ok=True)

    def tearDown(self) -> None:
        ss.SESSIONS_ROOT = self._orig_root

    def _start_running(self, rt: AgentRuntime) -> tuple[str, AgentRunner]:
        """开一个回合但不让它跑完：run 置空后状态停在 running，runner 留在内存里。"""
        with patch.object(AgentRunner, "run", lambda self: None):
            sid = rt.create_session(self.project)["session_id"]
            rt.start(self.project, "config.yaml", PROFILE, first_prompt="翻译", session_id=sid)
        return sid, rt._runners[rt._key(self.project)][sid]

    def _run_in_thread(self, fn) -> threading.Thread:
        thread = threading.Thread(target=fn, daemon=True)
        thread.start()
        return thread


class StatusContextEstimateTests(_RuntimeCase):
    def test_slow_context_estimate_does_not_block_other_registry_calls(self) -> None:
        rt = AgentRuntime()
        sid, runner = self._start_running(rt)
        entered = threading.Event()
        release = threading.Event()

        def slow_estimate(self_runner, *, refresh=True):  # noqa: ARG001
            entered.set()
            release.wait(HOLD_SECONDS)
            return 123

        runner._mnemon = object()  # 让 status() 走 runner._estimate_context_tokens 这条路
        try:
            with patch.object(AgentRunner, "_estimate_context_tokens", slow_estimate):
                blocked = self._run_in_thread(lambda: rt.status(self.project, sid))
                self.assertTrue(entered.wait(2), "status() 没进到上下文估算")
                started = time.monotonic()
                # 会话列表（侧边栏）与事件拉取（SSE）都要拿注册表锁；估算占着锁它们就全卡
                sessions = rt.list_sessions(self.project)
                rt.drain_events(self.project, after_step=0, session_id=sid)
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, FAST_SECONDS, f"status() 的上下文估算占着注册表锁：别的请求等了 {elapsed:.2f}s")
                self.assertEqual(sessions[0]["status"], "running")
                release.set()
                blocked.join(HOLD_SECONDS)
                self.assertFalse(blocked.is_alive())
        finally:
            release.set()
            runner._mnemon = None


class StopAbortTests(_RuntimeCase):
    def test_slow_abort_in_flight_does_not_block_other_registry_calls(self) -> None:
        rt = AgentRuntime()
        sid, runner = self._start_running(rt)
        entered = threading.Event()
        release = threading.Event()

        def slow_abort(self_runner):  # noqa: ARG001
            entered.set()
            release.wait(HOLD_SECONDS)

        try:
            with patch.object(AgentRunner, "abort_in_flight", slow_abort):
                stopping = self._run_in_thread(lambda: rt.stop(self.project, sid))
                self.assertTrue(entered.wait(2), "stop() 没走到关连接")
                # 停止信号是锁内那一步，关连接之前就该置好位
                self.assertTrue(runner.stop_event.is_set())
                started = time.monotonic()
                rt.list_sessions(self.project)
                snap = rt.status(self.project, sid)
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, FAST_SECONDS, f"stop() 关连接时占着注册表锁：别的请求等了 {elapsed:.2f}s")
                self.assertEqual(snap["status"], "running")  # 收尾由回合线程自己做，stop 不直接改状态
                release.set()
                stopping.join(HOLD_SECONDS)
                self.assertFalse(stopping.is_alive())
        finally:
            release.set()

    def test_queue_send_marks_immediate_before_slow_abort_and_keeps_registry_free(self) -> None:
        rt = AgentRuntime()
        sid, runner = self._start_running(rt)
        # run 被置空，首条消息没写进历史；message() 以"历史非空"判定会话存在，这里补上
        runner._persist_message({"role": "user", "content": "翻译"})
        queued = rt.message(self.project, "插一句", sid)["queued"]
        self.assertEqual(len(queued), 1)
        entered = threading.Event()
        release = threading.Event()

        def slow_abort(self_runner):  # noqa: ARG001
            entered.set()
            release.wait(HOLD_SECONDS)

        try:
            with patch.object(AgentRunner, "abort_in_flight", slow_abort):
                sending = self._run_in_thread(lambda: rt.queue_send(self.project, queued[0]["id"], sid))
                self.assertTrue(entered.wait(2), "queue_send 没走到关连接")
                state = rt._states[rt._key(self.project)][sid]
                # 「立即」的那条已经摘出队列、挂到 immediate_message 上，停止信号也置好了——
                # 这些都在锁内完成，回合线程收尾时取得到
                self.assertEqual(state.immediate_message, "插一句")
                self.assertEqual(len(state.pending_messages), 0)
                self.assertTrue(runner.stop_event.is_set())
                started = time.monotonic()
                rt.list_sessions(self.project)
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, FAST_SECONDS, f"queue_send 关连接时占着注册表锁：别的请求等了 {elapsed:.2f}s")
                release.set()
                sending.join(HOLD_SECONDS)
                self.assertFalse(sending.is_alive())
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main()
