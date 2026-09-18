"""上下文用量指示器（后端侧）的单元测试。

覆盖四件事：
1. 上下文窗口的解析口径（OpenAI-Compatible.tokens[0].contextWindow）；
2. 用量估算函数 _estimate_usage_tokens（锚点法）；
3. status() 里的 context 快照（界面指示器的兜底来源）；
4. context_usage 事件必须走瞬态通道（不占对话转录，刷新后由快照兜底）。
"""

import os
import tempfile
import unittest

from GalTransl.Agent import session_store as ss
from GalTransl.Agent.runtime import (
    AGENT_TOOLS,
    AgentRunner,
    AgentState,
    DEFAULT_CONTEXT_WINDOW,
    _TRANSIENT_EVENT_TYPES,
    _estimate_usage_tokens,
    _profile_context_window,
    _tools_overhead_tokens,
)


class ProfileContextWindowTests(unittest.TestCase):
    def test_reads_window_from_nested_token_config(self):
        profile = {"OpenAI-Compatible": {"tokens": [{"modelName": "m", "contextWindow": "1000k"}]}}
        self.assertEqual(_profile_context_window(profile), 1_000_000)
        self.assertEqual(
            _profile_context_window({"OpenAI-Compatible": {"tokens": [{"contextWindow": 200000}]}}),
            200_000,
        )

    def test_missing_or_bad_config_falls_back_to_default(self):
        for profile in (
            None,
            {},
            {"OpenAI-Compatible": {"tokens": []}},
            {"OpenAI-Compatible": {"tokens": [{"contextWindow": 10}]}},  # 明显不合理
            {"OpenAI-Compatible": []},
        ):
            self.assertEqual(_profile_context_window(profile), DEFAULT_CONTEXT_WINDOW)

    def test_only_first_token_window_counts(self):
        """Agent 只拿 tokens[0] 发请求，窗口也只认它：配在第二个令牌上不算数。"""
        profile = {
            "OpenAI-Compatible": {"tokens": [{"modelName": "m"}, {"contextWindow": 200000}]}
        }
        self.assertEqual(_profile_context_window(profile), DEFAULT_CONTEXT_WINDOW)

    def test_default_window_matches_the_backend_editor_default(self):
        """桌面端「上下文大小」留空即按 128000 处理（常量写在 BackendConfigEditor.tsx）。

        两处是同一个默认值：改这里就得同步改那边，否则界面提示与 Agent 实际窗口对不上。
        """
        self.assertEqual(DEFAULT_CONTEXT_WINDOW, 128_000)


class EstimateUsageTokensTests(unittest.TestCase):
    def test_without_anchor_sums_all_messages(self):
        msgs = [
            {"role": "user", "content": "a" * 400},
            {"role": "assistant", "content": "b" * 800},
        ]
        self.assertEqual(_estimate_usage_tokens(msgs), (400 + 800) // 4 + 8)

    def test_anchor_only_counts_messages_after_it(self):
        """有锚点时沿用上次 prompt_tokens，只补估锚点之后的新消息。"""
        msgs = [
            {"role": "user", "content": "a" * 400},
            {"role": "user", "content": "b" * 800},
        ]
        self.assertEqual(_estimate_usage_tokens(msgs, anchor=100, anchored=1), 100 + 800 // 4 + 4)

    def test_out_of_range_anchor_falls_back_to_full_estimate(self):
        msgs = [{"role": "user", "content": "a" * 400}]
        self.assertEqual(_estimate_usage_tokens(msgs, anchor=500, anchored=99), 400 // 4 + 4)


class ContextUsageStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self._root = tempfile.mkdtemp(prefix="agent-ctx-root-")
        self._orig_root = ss.SESSIONS_ROOT
        ss.SESSIONS_ROOT = self._root
        self.project = os.path.join(tempfile.mkdtemp(prefix="agent-ctx-proj-"), "MyGame")
        os.makedirs(self.project, exist_ok=True)

    def tearDown(self) -> None:
        ss.SESSIONS_ROOT = self._orig_root

    def test_status_reports_used_and_window_from_disk(self):
        """刷新/重开页面：按当前历史现场估算用量，窗口取会话落盘的那个值。"""
        from GalTransl.Agent.runtime import AgentRuntime

        rt = AgentRuntime()
        session = rt.create_session(self.project)
        store = ss.SessionStore(self.project, session["session_id"])
        store.append_meta(project_dir=self.project, title="t", context_window=1_000_000)
        store.append_message({"role": "user", "content": "x" * 400})

        ctx = rt.status(self.project, session["session_id"])["context"]
        self.assertEqual(ctx["window_tokens"], 1_000_000)
        # 消息估算 + tools schema 的固定开销（它也是这次请求的一部分）
        self.assertEqual(ctx["used_tokens"], _tools_overhead_tokens(AGENT_TOOLS) + 400 // 4 + 4)

    def test_status_without_persisted_window_uses_default(self):
        from GalTransl.Agent.runtime import AgentRuntime

        rt = AgentRuntime()
        session = rt.create_session(self.project)
        ss.SessionStore(self.project, session["session_id"]).append_message(
            {"role": "user", "content": "x" * 400}
        )
        ctx = rt.status(self.project, session["session_id"])["context"]
        self.assertEqual(ctx["window_tokens"], DEFAULT_CONTEXT_WINDOW)

    def test_empty_session_has_no_context_snapshot(self):
        """刚建的空会话还没有任何消息，status 不带 context —— 界面据此隐藏指示器。"""
        from GalTransl.Agent.runtime import AgentRuntime

        rt = AgentRuntime()
        session = rt.create_session(self.project)
        self.assertIsNone(rt.status(self.project, session["session_id"]).get("context"))


class ContextUsageEventTests(unittest.TestCase):
    def _runner(self, messages: list[dict]) -> tuple[AgentRunner, AgentState]:
        state = AgentState()
        state.messages = list(messages)
        runner = AgentRunner(state)  # 无 project/session -> _store 为 None，不落盘
        runner._context_window = 1_000_000
        return runner, state

    def test_event_is_transient_and_not_in_transcript(self):
        """事件走瞬态通道：只给实时流，不进对话转录（否则界面会多出无意义记录）。"""
        runner, state = self._runner([{"role": "user", "content": "x" * 400}])
        self.assertIn("context_usage", _TRANSIENT_EVENT_TYPES)
        runner._emit_context_usage()
        self.assertEqual(list(state.events), [])
        ev = list(state.transient_events)[-1]
        self.assertEqual(ev.type, "context_usage")
        self.assertEqual(ev.data["context"]["window_tokens"], 1_000_000)
        # 与 status() 的快照同口径：都要带上 tools schema 那笔固定开销
        self.assertEqual(
            ev.data["context"]["used_tokens"],
            _tools_overhead_tokens(AGENT_TOOLS) + 400 // 4 + 4,
        )

    def test_event_uses_anchor_for_estimation(self):
        runner, state = self._runner([
            {"role": "user", "content": "x" * 400},
            {"role": "user", "content": "y" * 800},
        ])
        state.last_prompt_tokens = 100
        state.anchored_message_count = 1
        runner._emit_context_usage()
        ev = list(state.transient_events)[-1]
        self.assertEqual(ev.data["context"]["used_tokens"], 100 + 800 // 4 + 4)


if __name__ == "__main__":
    unittest.main()
