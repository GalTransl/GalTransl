"""助手消息 parts 模型 + 会话转录回放的单元测试。

背景：思考/正文以前只存在于瞬态的 content_delta / reasoning_delta 事件里，
既不落盘也不进状态快照，于是刷新页面或切换会话后，重建出来的转录里思考块、
正文块全部消失，只剩工具调用。这里锁定 pi 式的做法：

- 助手响应落定时提交一条持久的 assistant_message 事件，内容是**有序 parts**
  （reasoning / text / tool_call）；
- 「正在生成的助手消息」单独作为进行中状态（status().streaming），与已提交转录分离；
- 转录从会话日志回放（read_transcript），不受内存事件窗口/前端缓存条数限制。
"""

import os
import tempfile
import unittest

from GalTransl.Agent import session_store as ss
from GalTransl.Agent.runtime import (
    AgentRunner,
    AgentState,
    _assistant_parts,
    _TRANSIENT_EVENT_TYPES,
)


def _acc(*, reasoning=(), content=(), tools=()):
    return {
        "reasoning": list(reasoning),
        "content": list(content),
        "tools": {
            i: {"id": tid, "name": name, "arguments_parts": list(args)}
            for i, (tid, name, args) in enumerate(tools)
        },
    }


class AssistantPartsTests(unittest.TestCase):
    def test_parts_are_ordered_and_aggregated(self):
        """顺序固定为 思考 → 正文 → 工具调用；同类文本归并成一段。"""
        parts = _assistant_parts(_acc(
            reasoning=["先想", "再想"],
            content=["正文", "续写"],
            tools=[("c1", "wait", ['{"seconds"']), ("c2", "get_progress", ["{}"])],
        ))
        self.assertEqual(parts, [
            {"type": "reasoning", "text": "先想再想"},
            {"type": "text", "text": "正文续写"},
            {"type": "tool_call", "id": "c1", "name": "wait", "arguments": '{"seconds"'},
            {"type": "tool_call", "id": "c2", "name": "get_progress", "arguments": "{}"},
        ])

    def test_empty_sections_are_omitted(self):
        """只有工具调用时不应冒出空的思考/正文段落。"""
        parts = _assistant_parts(_acc(tools=[("c1", "wait", ["{}"])]))
        self.assertEqual([p["type"] for p in parts], ["tool_call"])
        self.assertEqual(_assistant_parts(None), [])
        self.assertEqual(_assistant_parts(_acc()), [])


class StreamingMessageTests(unittest.TestCase):
    def test_live_snapshot_reflects_stream_accumulator(self):
        """进行中的消息：status 随时能拍到当前 parts（刷新/切会话时用它补半条消息）。"""
        state = AgentState()
        runner = AgentRunner(state)  # 无 session -> 不落盘
        self.assertIsNone(runner.live_streaming())

        runner._stream_acc = _acc(reasoning=["想想"], content=["你好"])
        snap = runner.live_streaming()
        assert snap is not None
        self.assertEqual(snap["step"], state.step)
        self.assertEqual([p["type"] for p in snap["parts"]], ["reasoning", "text"])

    def test_take_clears_snapshot(self):
        """响应落定后再也拍不到进行中消息（内容已作为持久化事件提交）。"""
        runner = AgentRunner(AgentState())
        acc = _acc(content=["x"])
        runner._stream_acc = acc
        self.assertIs(runner._take_stream_acc(), acc)
        self.assertIsNone(runner.live_streaming())


class EmitAssistantMessageTests(unittest.TestCase):
    def test_emits_durable_event_with_parts(self):
        runner = AgentRunner(AgentState())
        runner._emit_assistant_message(_acc(reasoning=["想"], content=["说"], tools=[("c1", "wait", ["{}"])]))
        events = list(runner.state.events)
        self.assertEqual([e.type for e in events], ["assistant_message"])
        self.assertEqual([p["type"] for p in events[0].data["parts"]], ["reasoning", "text", "tool_call"])
        self.assertNotIn("assistant_message", _TRANSIENT_EVENT_TYPES)

    def test_drop_tools_for_truncated_response(self):
        """响应被截断、工具调用已丢弃时：思考/正文照常进转录，工具段落不写。"""
        runner = AgentRunner(AgentState())
        runner._emit_assistant_message(_acc(content=["说"], tools=[("c1", "wait", ["{}"])]), drop_tools=True)
        parts = list(runner.state.events)[0].data["parts"]
        self.assertEqual([p["type"] for p in parts], ["text"])

    def test_nothing_emitted_when_response_is_empty(self):
        runner = AgentRunner(AgentState())
        runner._emit_assistant_message(_acc())
        self.assertEqual(list(runner.state.events), [])


class ReadTranscriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self._root = tempfile.mkdtemp(prefix="agent-tx-root-")
        self._orig_root = ss.SESSIONS_ROOT
        ss.SESSIONS_ROOT = self._root
        self.project = os.path.join(tempfile.mkdtemp(prefix="agent-tx-proj-"), "MyGame")
        os.makedirs(self.project, exist_ok=True)

    def tearDown(self) -> None:
        ss.SESSIONS_ROOT = self._orig_root

    def _store(self) -> ss.SessionStore:
        sid = ss.create_session(self.project, "t")
        return ss.SessionStore(self.project, sid)

    def test_replays_durable_events_in_order_and_skips_transient(self):
        """思考/正文的来源（assistant_message）要能回放；delta 等瞬态事件不进来。"""
        store = self._store()
        store.append_event({"type": "user_message", "step": 1, "message": "你好"})
        store.append_event({"type": "content_delta", "step": 2, "delta": "你"})
        store.append_event({"type": "reasoning_delta", "step": 3, "delta": "想"})
        store.append_event({"type": "context_usage", "step": 4, "context": {"used_tokens": 1}})
        store.append_event({"type": "assistant_message", "step": 5, "parts": [{"type": "text", "text": "你好"}]})
        store.append_event({"type": "tool_call", "step": 6, "id": "c1", "name": "wait"})

        types = [ev["type"] for ev in ss.read_transcript(self.project, store.session_id)]
        self.assertEqual(types, ["user_message", "assistant_message", "tool_call"])

    def test_keeps_first_user_message_when_truncated(self):
        """超长会话只留最近 limit 条，但首条用户消息（会话锚点）必须留住。"""
        store = self._store()
        store.append_event({"type": "user_message", "step": 1, "message": "最初的目标"})
        for i in range(2, 30):
            store.append_event({"type": "tool_result", "step": i, "id": f"c{i}"})
        events = ss.read_transcript(self.project, store.session_id, limit=5)
        self.assertEqual(events[0]["type"], "user_message")
        self.assertEqual(events[0]["message"], "最初的目标")
        self.assertEqual(len(events), 6)  # 锚点 + 最近 5 条

    def test_missing_session_returns_empty(self):
        self.assertEqual(ss.read_transcript(self.project, "no-such-session"), [])


class RuntimeTranscriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self._root = tempfile.mkdtemp(prefix="agent-tx2-root-")
        self._orig_root = ss.SESSIONS_ROOT
        ss.SESSIONS_ROOT = self._root
        self.project = os.path.join(tempfile.mkdtemp(prefix="agent-tx2-proj-"), "MyGame")
        os.makedirs(self.project, exist_ok=True)

    def tearDown(self) -> None:
        ss.SESSIONS_ROOT = self._orig_root

    def test_synthesizes_first_user_message_from_meta_goal(self):
        """异常退出可能没落下首条 user_message 事件：用 meta.goal 补一条。"""
        from GalTransl.Agent.runtime import AgentRuntime

        rt = AgentRuntime()
        session = rt.create_session(self.project)
        store = ss.SessionStore(self.project, session["session_id"])
        store.append_meta(project_dir=self.project, title="t", goal="把第一章翻完")
        store.append_event({"type": "assistant_message", "step": 2, "parts": [{"type": "text", "text": "好的"}]})

        events = rt.transcript(self.project, session["session_id"])
        self.assertEqual(events[0]["type"], "user_message")
        self.assertEqual(events[0]["message"], "把第一章翻完")
        self.assertEqual(events[1]["type"], "assistant_message")

    def test_unknown_session_returns_empty(self):
        from GalTransl.Agent.runtime import AgentRuntime

        rt = AgentRuntime()
        self.assertEqual(rt.transcript(self.project, "no-such-session"), [])

    def test_status_has_no_streaming_snapshot_when_idle(self):
        from GalTransl.Agent.runtime import AgentRuntime

        rt = AgentRuntime()
        session = rt.create_session(self.project)
        ss.SessionStore(self.project, session["session_id"]).append_message(
            {"role": "user", "content": "x" * 400}
        )
        self.assertIsNone(rt.status(self.project, session["session_id"])["streaming"])


class ReloadAfterTurnTests(unittest.TestCase):
    """验收：一个回合跑完后"重开会话"，思考/正文仍在转录里。

    回归背景（用户实际撞到的现象）：跑了一段时间、刷新页面或切回会话后，思考块
    与正文块全部消失，只剩工具调用——因为文本以前只存在于瞬态 delta 事件里。
    """

    def setUp(self) -> None:
        self._root = tempfile.mkdtemp(prefix="agent-tx3-root-")
        self._orig_root = ss.SESSIONS_ROOT
        ss.SESSIONS_ROOT = self._root
        self.project = os.path.join(tempfile.mkdtemp(prefix="agent-tx3-proj-"), "MyGame")
        os.makedirs(self.project, exist_ok=True)

    def tearDown(self) -> None:
        ss.SESSIONS_ROOT = self._orig_root

    def test_assistant_parts_survive_reload(self):
        from types import SimpleNamespace

        from GalTransl.Agent.runtime import AgentRuntime

        rt = AgentRuntime()
        session = rt.create_session(self.project)
        sid = session["session_id"]

        state = AgentState(
            status="running",
            goal="跑一个回合",
            project_dir=self.project,
            session_id=sid,
            config_file_name="config.yaml",
        )
        runner = AgentRunner(state)
        runner._model = "fake"
        runner._resolve_llm = lambda: None  # 跳过真实后端配置解析
        runner._http_get = lambda path: {}  # 工具不真的打后端
        calls = {"n": 0}

        def make_chunk(*, content=None, reasoning=None, tool_calls=None, finish=None):
            attrs = {"content": content, "tool_calls": tool_calls}
            if reasoning is not None:
                attrs["reasoning_content"] = reasoning
            return SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(**attrs), finish_reason=finish)],
                usage=None,
            )

        def create(**_kw):
            calls["n"] += 1
            if calls["n"] == 1:
                yield make_chunk(reasoning="先想一下")
                yield make_chunk(content="我来调用工具")
                yield make_chunk(
                    tool_calls=[SimpleNamespace(
                        index=0, id="c1", function=SimpleNamespace(name="get_runtime", arguments="{}")
                    )],
                    finish="tool_calls",
                )
            else:
                yield make_chunk(content="做完了", finish="stop")

        runner._openai_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        runner.run()

        # 模拟界面重开/切回会话：全新的 Runtime（内存全空），转录从会话日志重建
        events = AgentRuntime().transcript(self.project, sid)
        parts = [p for ev in events if ev["type"] == "assistant_message" for p in ev["parts"]]
        self.assertEqual([p["type"] for p in parts[:2]], ["reasoning", "text"])
        self.assertEqual(parts[0]["text"], "先想一下")
        self.assertEqual(parts[1]["text"], "我来调用工具")
        # 工具调用与回合收尾也都在
        self.assertTrue(any(p["type"] == "tool_call" for p in parts))
        self.assertEqual([p["text"] for p in parts if p["type"] == "text"][-1], "做完了")
        self.assertTrue(any(ev["type"] == "finish" for ev in events))


if __name__ == "__main__":
    unittest.main()
