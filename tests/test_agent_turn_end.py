"""回合收尾窗口的插话处理（用户先停止、紧接着又发"继续"）。

回归背景：第一轮手动点了停止，随后在"停止生效前"发了「继续」。这条消息会被
message() 当成"运行中插话"排队（那一刻状态还是 running），而回合收尾的
_drain_pending_messages() 可能已经跑过——消息既没进历史、也没人开新回合，界面上
就只剩一个孤零零的用户气泡，紧接着是上一回合的「已停止 / 用户停止」，看起来像是
"第二轮没重试就变成用户停止了"。

修法：把「取插话 → 置 pending_followup → 落终态」与 message() 的「看状态 → 入队」
放进同一把注册表锁里，整段原子；并加一条兜底：drain 之后队列里又冒出消息时，
至少置上 followup 标记，留给下一回合取走。

后续（照 pi + 队列面板）：排队消息只活在队列里（界面显示在 composer 上方的
队列面板，不进聊天转录），**用户主动停止**时留在队列等用户决定（不代跑）；
普通收尾才写进历史并开新回合（steer）。用户点「立即」的那条会被打断当前回合
立刻发出（见 queue_send）。
"""

import os
import tempfile
import threading
import unittest
from typing import Any
from unittest.mock import patch

from GalTransl.Agent import session_store as ss
from GalTransl.Agent.runtime import AgentRunner, AgentRuntime, AgentState, PendingMessage

PROFILE = {"OpenAI-Compatible": {"tokens": [{"modelName": "fake-model", "token": "fake-token"}]}}


def _make_runner(registry: AgentRuntime | None = None) -> tuple[AgentRunner, AgentState]:
    state = AgentState()
    state.session_id = ""  # 不落盘，避免污染真实会话目录
    state.project_dir = ""
    return AgentRunner(state, registry=registry), state


def _queue(state: AgentState, text: str, item_id: str = "q1") -> PendingMessage:
    """往队列里放一条（模拟运行中用户发的消息）。"""
    item = PendingMessage(id=item_id, text=text)
    state.pending_messages.append(item)
    return item


def _queued_texts(state: AgentState) -> list[str]:
    return [m.text for m in state.pending_messages]


class TurnEndInterjectionTests(unittest.TestCase):
    """普通收尾（跑完/出错）：滞留插话写进历史并开新回合续跑（steer）。"""

    def test_queued_interjection_becomes_followup_on_done(self):
        runner, state = _make_runner()
        _queue(state, "继续")

        runner._end_turn("done", {"summary": "做完了", "total_steps": 1})

        self.assertTrue(state.pending_followup)
        self.assertEqual(state.status, "awaiting_input")
        self.assertIn("继续", [m["content"] for m in state.messages if m["role"] == "user"])
        # 被消费的这一刻才补发 user_message，且排在 finish 之后：
        # 界面上读作「本轮最终回复 → 你发的新消息」，不会把最终回复压到新消息下面
        self.assertEqual([e.type for e in state.events], ["finish", "user_message"])
        self.assertEqual(_queued_texts(state), [])  # 队列已消费，面板要撤掉
        # 带了 followup 标记：界面据此别把运行态打回停止（马上还要跑）
        self.assertTrue(state.events[0].data["followup"])

    def test_interjection_arriving_during_drain_is_not_lost(self):
        """drain 之后才到的插话不许丢：至少置 followup，留给下一回合取走。"""
        runner, state = _make_runner(registry=AgentRuntime())
        real_drain = runner._drain_pending_messages

        def drain_then_arrive():
            queued = real_drain()
            _queue(state, "继续", "q9")  # 模拟收尾窗口内到达
            return queued

        runner._drain_pending_messages = drain_then_arrive  # type: ignore[method-assign]
        runner._end_turn("done", {"summary": "做完了", "total_steps": 1})

        self.assertTrue(state.pending_followup)
        self.assertEqual(_queued_texts(state), ["继续"])

    def test_normal_turn_end_sets_no_followup(self):
        """没有插话的普通收尾：不该被标记成 followup（否则会白跑一回合）。"""
        runner, state = _make_runner(registry=AgentRuntime())
        runner._end_turn("done", {"summary": "做完了", "total_steps": 1})
        self.assertFalse(state.pending_followup)
        self.assertEqual(state.status, "awaiting_input")


class StoppedTurnKeepsQueueTests(unittest.TestCase):
    """用户主动停止：排队消息留在队列面板里等用户决定（立即/编辑/删除），不代跑。"""

    def test_stopped_turn_keeps_queued_messages(self):
        runner, state = _make_runner()
        _queue(state, "继续")

        runner._end_turn("stopped", {"reason": "用户停止"})

        self.assertFalse(state.pending_followup)  # 不代跑
        self.assertEqual(_queued_texts(state), ["继续"])  # 队列原样保留
        self.assertEqual(state.status, "stopped")
        # 模型从没见过这条消息：一个字都不进历史
        self.assertEqual([m for m in state.messages if m["role"] == "user"], [])
        # 终态事件照旧落持久 deque；队列变更走瞬态事件推给面板
        self.assertEqual([e.type for e in state.events], ["stopped"])
        queue_events = [e for e in state.transient_events if e.type == "queue"]
        self.assertEqual(queue_events[-1].data["queued"], [{"id": "q1", "text": "继续"}])
        # 没有 followup：界面应当真的收尾（停止按钮消失是对的）
        self.assertNotIn("followup", state.events[-1].data)

    def test_immediate_message_starts_followup_and_rest_stay_queued(self):
        """「立即」的那条随收尾发出；其余排队项留在队列（不挤进这一轮）。"""
        runner, state = _make_runner(registry=AgentRuntime())
        _queue(state, "别做了", "q7")
        state.immediate_message = "改成这样"

        runner._end_turn("stopped", {"reason": "用户停止"})

        self.assertTrue(state.pending_followup)  # 新回合马上发这条
        self.assertIn("改成这样", [m["content"] for m in state.messages if m["role"] == "user"])
        self.assertEqual(_queued_texts(state), ["别做了"])
        # 带回执：这次停止是「立即」触发的、且马上会开新回合——界面要据此
        # 保持运行态（曾经因为无条件 setRunning(false)，停止按钮消失而 Agent 还在跑）
        stopped = state.events[-1]
        self.assertEqual(stopped.type, "stopped")
        self.assertTrue(stopped.data["followup"])
        self.assertIn("立即", stopped.data["reason"])


class QueuedWaitsForTurnEndTests(unittest.TestCase):
    """排队消息要等「本轮工作做完」才发出：模型给出最终回复、不再调工具之后。

    回归背景：以前是在「每次 LLM 请求前」注入（安全点），于是模型刚跑完第一个
    工具调用就被插进一条新消息，把一轮任务劈成两半——用户看到工具行下面紧跟着
    一条用户气泡 + 已停止。
    """

    def test_queue_is_not_injected_between_tool_rounds(self):
        runner, state = _make_runner()
        state.messages = [{"role": "user", "content": "看一下项目状态"}]
        rounds: list[list[str]] = []

        def fake_stream(self: AgentRunner) -> tuple[str, list[dict[str, Any]], str]:
            rounds.append([m["content"] for m in state.messages if m["role"] == "user"])
            if len(rounds) == 1:
                # 第一轮：模型先调个工具
                return (
                    "我先看看。",
                    [{"id": "c1", "name": "wait", "arguments": "{}"}],
                    "tool_calls",
                )
            # 第二轮：不再调工具 → 本轮工作做完
            return ("本轮做完了。", [], "stop")

        with (
            patch.object(AgentRunner, "_resolve_llm", lambda self: None),
            patch.object(AgentRunner, "_stream_llm_response", fake_stream),
            patch.object(AgentRunner, "_dispatch_tool", lambda self, name, args: {"ok": True}),
        ):
            _queue(state, "测试一下工具使用")
            runner.run()

        self.assertEqual(len(rounds), 2)
        for sent in rounds:  # 本轮两次请求都不该带上它（第一个工具之后也不行）
            self.assertNotIn("测试一下工具使用", sent)
        # 本轮收尾（done）之后才写进历史，并开新回合把它发出去
        self.assertIn(
            "测试一下工具使用", [m["content"] for m in state.messages if m["role"] == "user"]
        )
        self.assertTrue(state.pending_followup)
        self.assertEqual(state.turn_end, "done")


class QueuedMessageEventTests(unittest.TestCase):
    """队列面板：数据来源（快照 + 瞬态事件）与三个操作（删除/编辑/立即）。

    排队消息在被真正消费前不发 user_message 事件，也不进转录——否则它明明还
    躺在队列里，聊天框里却已经有一条"已发送"的气泡了。
    """

    def setUp(self) -> None:
        self._root = tempfile.mkdtemp(prefix="agent-queue-root-")
        self._orig_root = ss.SESSIONS_ROOT
        ss.SESSIONS_ROOT = self._root
        self.project = os.path.join(tempfile.mkdtemp(prefix="agent-queue-proj-"), "MyGame")
        os.makedirs(self.project, exist_ok=True)

    def tearDown(self) -> None:
        ss.SESSIONS_ROOT = self._orig_root

    def _running_session(self, *messages: str) -> tuple[AgentRuntime, str]:
        """跑着一个回合（run 打桩）时发消息：它们会进队列（=面板上的那些）。"""
        rt = AgentRuntime()
        sid = rt.create_session(self.project)["session_id"]
        with patch.object(AgentRunner, "run", lambda self: None):
            rt.start(self.project, "config.yaml", PROFILE, goal="第一轮", session_id=sid)
            state = self._state(rt, sid)
            # run() 被打桩，不会自己写历史；补一条让它像跑过一轮的会话
            state.messages.append({"role": "user", "content": "第一轮"})
            for text in messages:
                rt.message(self.project, text, sid)
        return rt, sid

    def _state(self, rt: AgentRuntime, sid: str) -> AgentState:
        state = rt._get_state(self.project, sid)
        assert state is not None
        return state

    def _runner(self, rt: AgentRuntime, sid: str) -> AgentRunner:
        return rt._runners[rt._key(self.project)][sid]

    def _queued(self, rt: AgentRuntime, sid: str) -> list[dict[str, str]]:
        return rt.status(self.project, sid)["queued"]

    def _user_texts(self, rt: AgentRuntime, sid: str) -> list[str]:
        return [
            str(e.get("message"))
            for e in rt.status(self.project, sid)["events"]
            if e.get("type") == "user_message"
        ]

    def test_queued_message_only_shows_in_queue(self):
        """排队中：聊天框里没有它，队列快照与瞬态事件里有。"""
        rt, sid = self._running_session("继续")

        self.assertNotIn("继续", self._user_texts(rt, sid))
        self.assertEqual(self._queued(rt, sid), [{"id": "q1", "text": "继续"}])
        queue_events = [e for e in self._state(rt, sid).transient_events if e.type == "queue"]
        self.assertEqual(queue_events[-1].data["queued"], [{"id": "q1", "text": "继续"}])

    def test_consumed_message_emits_user_message(self):
        rt, sid = self._running_session("继续")
        self._runner(rt, sid)._end_turn("done", {"summary": "好", "total_steps": 1})  # 消费
        self.assertIn("继续", self._user_texts(rt, sid))
        self.assertEqual(self._queued(rt, sid), [])

    def test_queue_delete(self):
        rt, sid = self._running_session("甲", "乙")
        first = self._queued(rt, sid)[0]

        snap = rt.queue_delete(self.project, first["id"], sid)

        self.assertEqual(snap["queued"], [{"id": "q2", "text": "乙"}])
        self.assertEqual(_queued_texts(self._state(rt, sid)), ["乙"])

    def test_queue_update_trims_and_rejects_empty(self):
        rt, sid = self._running_session("甲")
        item_id = self._queued(rt, sid)[0]["id"]

        snap = rt.queue_update(self.project, item_id, "  改一下  ", sid)

        self.assertEqual([m["text"] for m in snap["queued"]], ["改一下"])
        self.assertEqual(self._queued(rt, sid)[0]["id"], item_id)  # 位置/id 不变
        with self.assertRaises(ValueError):
            rt.queue_update(self.project, item_id, "   ", sid)

    def test_queue_send_while_running_interrupts_then_sends(self):
        """「立即」：打断当前回合，这条随收尾发出；其余留在队列里。"""
        rt, sid = self._running_session("甲", "乙")
        first = self._queued(rt, sid)[0]

        snap = rt.queue_send(self.project, first["id"], sid)

        state = self._state(rt, sid)
        self.assertEqual(snap["queued"], [{"id": "q2", "text": "乙"}])  # 其余留在面板
        self.assertEqual(state.immediate_message, "甲")  # 等回合收尾时发出去
        self.assertTrue(rt._stop_events[rt._key(self.project)][sid].is_set())  # 已打断

        self._runner(rt, sid)._end_turn("stopped", {"reason": "用户停止"})  # 回合线程收尾
        self.assertIn("甲", self._user_texts(rt, sid))
        self.assertEqual(_queued_texts(state), ["乙"])

    def test_queue_send_when_idle_starts_new_turn(self):
        """空闲时点「立即」：直接当普通消息发出去（无需打断）。"""
        rt, sid = self._running_session("甲")
        state = self._state(rt, sid)
        state.status = "awaiting_input"  # 回合已结束
        item_id = self._queued(rt, sid)[0]["id"]

        snap = rt.queue_send(self.project, item_id, sid)

        self.assertEqual(snap["status"], "running")
        self.assertEqual(snap["queued"], [])
        self.assertIn("甲", self._user_texts(rt, sid))

    def test_queue_event_reaches_the_stream(self):
        """走一遍 SSE 的取事件路径：面板确实能收到队列快照。"""
        rt, sid = self._running_session("继续")
        after_step = max(
            (e.get("step", 0) for e in rt.status(self.project, sid)["events"]), default=0
        )

        self._runner(rt, sid).emit_queue()
        events = rt.drain_events(self.project, after_step=after_step, session_id=sid)

        queues = [e for e in events if e.get("type") == "queue"]
        self.assertEqual(queues[-1].get("queued"), [{"id": "q1", "text": "继续"}])


class MessageAfterTurnEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self._root = tempfile.mkdtemp(prefix="agent-turnend-root-")
        self._orig_root = ss.SESSIONS_ROOT
        ss.SESSIONS_ROOT = self._root
        self.project = os.path.join(tempfile.mkdtemp(prefix="agent-turnend-proj-"), "MyGame")
        os.makedirs(self.project, exist_ok=True)

    def tearDown(self) -> None:
        ss.SESSIONS_ROOT = self._orig_root

    def test_message_after_terminal_state_starts_a_new_turn(self):
        """回合已收尾后再发消息：走"开新回合"，不进插话队列（否则会被吞掉）。"""
        rt = AgentRuntime()
        sid = rt.create_session(self.project)["session_id"]
        with patch.object(AgentRunner, "run", lambda self: None):
            rt.start(self.project, "config.yaml", PROFILE, goal="第一轮", session_id=sid)
            state = rt._get_state(self.project, sid)
            assert state is not None
            state.status = "stopped"  # 模拟"用户点了停止、回合已收尾"
            # run() 被打桩，不会自己写历史；补一条让它像跑过一轮的会话
            state.messages.append({"role": "user", "content": "第一轮"})

            snap = rt.message(self.project, "继续", sid)

        self.assertEqual(snap["status"], "running")
        self.assertEqual(_queued_texts(state), [])
        self.assertIn("继续", [m["content"] for m in state.messages if m["role"] == "user"])


class StopSignalResetTests(unittest.TestCase):
    """停止信号不能泄漏到下一回合：每个新回合都必须拿到一个全新的事件。

    回归背景：用户第一轮点了停止、随后发消息继续，结果新回合一失败就被当成
    "用户停止"、不再重试。除了收尾窗口的插话竞态（见上），还有一种可能是停止
    信号被沿用了下来——所以这里把"新回合 = 干净信号"钉成契约，三种入口都覆盖。
    """

    def setUp(self) -> None:
        self._root = tempfile.mkdtemp(prefix="agent-stopsig-root-")
        self._orig_root = ss.SESSIONS_ROOT
        ss.SESSIONS_ROOT = self._root
        self.project = os.path.join(tempfile.mkdtemp(prefix="agent-stopsig-proj-"), "MyGame")
        os.makedirs(self.project, exist_ok=True)

    def tearDown(self) -> None:
        ss.SESSIONS_ROOT = self._orig_root

    def _key(self, rt: AgentRuntime) -> str:
        return rt._key(self.project)

    def _runner(self, rt: AgentRuntime, sid: str) -> AgentRunner:
        return rt._runners[self._key(rt)][sid]

    def _registered_event(self, rt: AgentRuntime, sid: str) -> threading.Event:
        return rt._stop_events[self._key(rt)][sid]

    def test_message_after_stopped_turn_gets_clean_stop_signal(self):
        """点了停止、回合已收尾后再发消息：新回合用的是全新事件（置位信号不沿用）。"""
        rt = AgentRuntime()
        sid = rt.create_session(self.project)["session_id"]
        with patch.object(AgentRunner, "run", lambda self: None):
            rt.start(self.project, "config.yaml", PROFILE, goal="第一轮", session_id=sid)
            state = rt._get_state(self.project, sid)
            assert state is not None
            state.messages.append({"role": "user", "content": "第一轮"})
            state.status = "stopped"  # 回合已收尾

            rt.stop(self.project, sid)  # 用户点了停止
            stopped_event = self._registered_event(rt, sid)
            self.assertTrue(stopped_event.is_set())

            rt.message(self.project, "继续", sid)  # 继续 → 开新回合

        runner = self._runner(rt, sid)
        self.assertIsNot(runner.stop_event, stopped_event)  # 换了个新事件
        self.assertFalse(runner.stop_event.is_set())  # 而且是干净的
        self.assertFalse(self._registered_event(rt, sid).is_set())  # 注册表里也是新的

    def test_followup_turn_gets_clean_stop_signal(self):
        """插话转 followup 开的回合，同样拿到干净信号（第一轮的停止不该影响它）。"""
        rt = AgentRuntime()
        sid = rt.create_session(self.project)["session_id"]
        with patch.object(AgentRunner, "run", lambda self: None):
            rt.start(self.project, "config.yaml", PROFILE, goal="第一轮", session_id=sid)
            state = rt._get_state(self.project, sid)
            assert state is not None
            state.status = "stopped"
            state.pending_followup = True  # 收尾发现滞留插话
            rt.stop(self.project, sid)
            stopped_event = self._registered_event(rt, sid)

            rt._begin_followup(self.project, sid)

        runner = self._runner(rt, sid)
        self.assertIsNot(runner.stop_event, stopped_event)
        self.assertFalse(runner.stop_event.is_set())
        self.assertEqual(state.status, "running")

    def test_interjection_while_winding_down_keeps_stop_signal(self):
        """回合仍在收尾（running）时发的消息只排队，不动停止信号。

        那一轮还在"停"的过程中：此处若把信号清掉，用户刚点的停止就失效了，
        正在挂起的请求会被当成"还要继续跑"。等回合收尾后由 followup 拿到新信号。
        """
        rt = AgentRuntime()
        sid = rt.create_session(self.project)["session_id"]
        with patch.object(AgentRunner, "run", lambda self: None):
            rt.start(self.project, "config.yaml", PROFILE, goal="第一轮", session_id=sid)
            state = rt._get_state(self.project, sid)
            assert state is not None
            state.messages.append({"role": "user", "content": "第一轮"})
            # 回合仍在运行（请求挂在网络上，收尾还没发生）
            rt.stop(self.project, sid)
            running_event = self._registered_event(rt, sid)

            snap = rt.message(self.project, "继续", sid)  # → 插话排队

        self.assertEqual(snap["status"], "running")
        self.assertEqual(_queued_texts(state), ["继续"])  # 排队等收尾时处理
        self.assertIs(self._runner(rt, sid).stop_event, running_event)  # 信号没被换掉
        self.assertTrue(running_event.is_set())  # 停止依然有效


if __name__ == "__main__":
    unittest.main()
