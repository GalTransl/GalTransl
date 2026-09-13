"""上下文压缩（compaction）与会话生命周期的单元测试。

覆盖三类逻辑：
1. 切点安全 —— `_find_compaction_cut` 永远不会切断 assistant.tool_calls 与后续
   tool 响应的配对（否则发给 LLM 的 messages 会因孤儿 tool_calls / 孤儿 tool 响应
   而被拒）。
2. 用量估算 —— 有 usage 锚点时只估锚点之后的增量，无锚点时整体按字符数估算。
3. 压缩执行 —— 超阈值触发、消息被替换为摘要、每回合最多一次；LLM 摘要失败时
   回退本地截断且 messages 仍合法（无孤儿 tool 响应）。
4. 会话生命周期 —— 通过 AgentRuntime 验证标题递增、多会话隔离、删除后列表减少。

不依赖真实 OpenAI / 后端：用 FakeOpenAI stub 掉 chat.completions.create，
_run_events 不触发。
"""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from GalTransl.Agent import session_store as ss
from GalTransl.Agent.runtime import (
    AgentRunner,
    AgentState,
    COMPACT_KEEP_RECENT_MSGS,
    COMPACT_TRIGGER_RATIO,
    CONTEXT_RESERVE_TOKENS,
    DEFAULT_CONTEXT_WINDOW,
    _estimate_message_tokens,
    _find_compaction_cut,
    _local_fallback_summary,
)


# ---- 测试用 stub ----

class FakeOpenAI:
    """替身 OpenAI 客户端：摘要调用可配置返回值或抛异常。"""

    def __init__(self, summary_text="## 目标\n测试摘要", raise_exc=None):
        self._summary_text = summary_text
        self._raise = raise_exc
        self.calls = []

        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                if outer._raise is not None:
                    raise outer._raise
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content=outer._summary_text)
                        )
                    ]
                )

        self.chat = SimpleNamespace(completions=_Completions())


def _make_runner(messages, *, window=DEFAULT_CONTEXT_WINDOW, summary_text="abc",
                 raise_exc=None, session_id="test-session"):
    """构造一个已注入 FakeOpenAI 的 AgentRunner，绕过 _resolve_llm。

    直接给 runner._openai_client / _model / _context_window 赋值，
    这样 _maybe_compact / _summarize_messages 不需要真实后端配置。
    """
    state = AgentState()
    state.project_dir = os.path.join(tempfile.gettempdir(), "agent-comp-test")
    state.session_id = session_id
    state.messages = list(messages)
    runner = AgentRunner(state)
    runner._openai_client = FakeOpenAI(summary_text=summary_text, raise_exc=raise_exc)
    runner._model = "fake-model"
    runner._context_window = window
    runner._store = None  # 测试不落盘，避免污染
    return runner, state


def _msg(role, content="x", tool_calls=None):
    m = {"role": role, "content": content}
    if tool_calls is not None:
        m["tool_calls"] = tool_calls
    return m


def _tool_call(tid="call_1", name="get_runtime", args="{}"):
    return {
        "id": tid,
        "type": "function",
        "function": {"name": name, "arguments": args},
    }


# ---- 1. 切点安全 ----

class FindCompactionCutTests(unittest.TestCase):
    def test_cut_never_splits_tool_call_and_tool_response(self):
        """结尾一段 assistant(tool_calls) + tool 响应必须整体保留。"""
        # 20 条普通消息后跟 assistant.tool_calls + 对应 tool 响应
        msgs = [_msg("user", "hello") for _ in range(20)]
        msgs.append(_msg("assistant", "调用工具", tool_calls=[_tool_call()]))
        msgs.append(_msg("tool", "结果"))
        cut = _find_compaction_cut(msgs, COMPACT_KEEP_RECENT_MSGS)
        self.assertGreater(cut, 0)
        # 切点前一条绝不能是带 tool_calls 的 assistant
        self.assertNotEqual(msgs[cut - 1].get("role") + str(bool(msgs[cut - 1].get("tool_calls"))),
                            "assistantTrue")
        # 切点本身（保留段第一条）绝不能是 tool 响应
        self.assertNotEqual(msgs[cut].get("role"), "tool")

    def test_cut_backs_off_when_only_tool_pairs_at_tail(self):
        """尾部恰好全是 tool 配对时，切点会往前退到安全位置。"""
        msgs = [_msg("user", str(i)) for i in range(8)]
        # 把最近 16 条全部塞成 tool 配对，逼切点退到普通消息区
        for i in range(20):
            msgs.append(_msg("assistant", f"call{i}", tool_calls=[_tool_call()]))
            msgs.append(_msg("tool", f"res{i}"))
        cut = _find_compaction_cut(msgs, COMPACT_KEEP_RECENT_MSGS)
        if cut > 0:
            # 退出的安全位置不能落在 tool 配对中间
            self.assertNotEqual(msgs[cut - 1].get("role") + str(bool(msgs[cut - 1].get("tool_calls"))),
                                "assistantTrue")
            self.assertNotEqual(msgs[cut].get("role"), "tool")

    def test_too_few_messages_returns_zero(self):
        """消息数 <= keep_recent + 1 时没有可裁的，返回 0。"""
        msgs = [_msg("user", "x") for _ in range(COMPACT_KEEP_RECENT_MSGS)]
        self.assertEqual(_find_compaction_cut(msgs, COMPACT_KEEP_RECENT_MSGS), 0)

    def test_alternating_tool_pairs_still_find_safe_cut_between_pairs(self):
        """全是 assistant(tool)+tool 交替配对时，切点落在两对之间（tool 之后、
        下一个 assistant 之前），仍属安全位置。"""
        msgs = []
        for _ in range(40):
            msgs.append(_msg("assistant", "c", tool_calls=[_tool_call()]))
            msgs.append(_msg("tool", "r"))
        cut = _find_compaction_cut(msgs, COMPACT_KEEP_RECENT_MSGS)
        self.assertGreater(cut, 0)
        # 切点前一条不是带 tool_calls 的 assistant，切点后第一条不是 tool 响应
        self.assertFalse(msgs[cut - 1].get("tool_calls"))
        self.assertNotEqual(msgs[cut].get("role"), "tool")


# ---- 2. 用量估算 ----

class EstimateContextTokensTests(unittest.TestCase):
    def test_no_anchor_estimates_all_messages(self):
        """无锚点时按全部消息字符数估算。"""
        msgs = [
            _msg("user", "a" * 400),   # 100 + 4
            _msg("assistant", "b" * 800),  # 200 + 4
        ]
        runner, _ = _make_runner(msgs)
        # 无锚点：totally char-based
        est = runner._estimate_context_tokens()
        expected = (400 + 800) // 4 + 8
        self.assertEqual(est, expected)

    def test_with_anchor_only_estimates_tail(self):
        """有锚点时：anchor + 锚点之后新增消息的字符估算。"""
        msgs = [
            _msg("user", "a" * 400),
            _msg("assistant", "b" * 800),
            _msg("user", "c" * 1200),  # 锚点之后新增
        ]
        runner, state = _make_runner(msgs)
        state.last_prompt_tokens = 500
        state.anchored_message_count = 2  # 锚点覆盖前 2 条
        est = runner._estimate_context_tokens()
        # 500 + 第3条的估算 (1200//4 + 4)
        self.assertEqual(est, 500 + (1200 // 4 + 4))

    def test_anchor_out_of_range_falls_back_to_full(self):
        """anchored_message_count 越界时退回整体估算（防呆）。"""
        msgs = [_msg("user", "a" * 400)]
        runner, state = _make_runner(msgs)
        state.last_prompt_tokens = 500
        state.anchored_message_count = 99  # 越界
        est = runner._estimate_context_tokens()
        self.assertEqual(est, 400 // 4 + 4)


class EstimateMessageTokensHelperTests(unittest.TestCase):
    def test_includes_tool_call_arguments(self):
        """tool_calls 的 arguments JSON 计入字符数。"""
        m = _msg("assistant", "go", tool_calls=[
            _tool_call(args='{"project_dir": "/x"}' * 10),
        ])
        est = _estimate_message_tokens(m)
        self.assertGreater(est, len("go") // 4 + 4)


# ---- 3. 压缩执行 ----

class MaybeCompactTests(unittest.TestCase):
    def _oversized_messages(self, n=40):
        """构造一组远超阈值的普通消息。"""
        return [_msg("user", "word " * 200) for _ in range(n)]

    def test_under_threshold_does_not_compact(self):
        """估算用量未超阈值时不压缩。"""
        msgs = [_msg("user", "hi") for _ in range(5)]
        runner, state = _make_runner(msgs, window=DEFAULT_CONTEXT_WINDOW)
        runner._maybe_compact()
        self.assertEqual(len(state.messages), 5)
        self.assertFalse(runner._compacted_this_turn)

    def test_over_threshold_compacts_and_replaces_prefix(self):
        """超阈值时压缩：前缀被替换成 system 摘要 + user 锚点消息。"""
        msgs = self._oversized_messages(40)
        runner, state = _make_runner(msgs, window=2000, summary_text="我是摘要")
        before = len(state.messages)
        runner._maybe_compact()
        self.assertTrue(runner._compacted_this_turn)
        # 压缩后：1 system + 1 user + 尾部保留 >= 3，且总条数减少
        self.assertLess(len(state.messages), before)
        self.assertEqual(state.messages[0]["role"], "system")
        self.assertIn("摘要", state.messages[0]["content"])
        self.assertEqual(state.messages[1]["role"], "user")
        # 锚点重置（旧 usage 失效）
        self.assertEqual(state.last_prompt_tokens, 0)
        self.assertEqual(state.anchored_message_count, 0)

    def test_at_most_once_per_turn(self):
        """同一回合第二次调用立即返回，不再压缩。"""
        msgs = self._oversized_messages(40)
        runner, state = _make_runner(msgs, window=2000)
        runner._maybe_compact()
        first_count = len(state.messages)
        # 把消息再撑大，第二次也不该动
        state.messages.extend(self._oversized_messages(40))
        runner._maybe_compact()
        self.assertEqual(len(state.messages), first_count + 40)

    def test_tool_pairs_compacted_safely_without_orphans(self):
        """前缀全是 assistant(tool)+tool 配对时也能压缩，且压缩后尾部无孤儿 tool 响应。"""
        msgs = []
        for _ in range(40):
            msgs.append(_msg("assistant", "c", tool_calls=[_tool_call()]))
            msgs.append(_msg("tool", "r"))
        runner, state = _make_runner(msgs, window=2000)
        runner._maybe_compact()
        self.assertTrue(runner._compacted_this_turn)
        self.assertLess(len(state.messages), len(msgs))
        # 校验尾部保留区无孤儿 tool 响应（前面必须有带 tool_calls 的 assistant）
        for i, m in enumerate(state.messages):
            if m.get("role") == "tool":
                self.assertGreater(i, 0)
                self.assertTrue(
                    state.messages[i - 1].get("tool_calls"),
                    "tool 响应前一条必须是带 tool_calls 的 assistant",
                )

    def test_llm_summary_failure_falls_back_and_keeps_messages_valid(self):
        """摘要 LLM 抛异常时回退本地截断，且 messages 无孤儿 tool 响应。"""
        # 前缀里混入 tool 配对，回退后尾部保留必须仍成对
        msgs = [_msg("user", "w" * 800) for _ in range(30)]
        msgs.append(_msg("assistant", "call", tool_calls=[_tool_call()]))
        msgs.append(_msg("tool", "res"))
        runner, state = _make_runner(
            msgs, window=3000, raise_exc=RuntimeError("summary down")
        )
        runner._maybe_compact()
        self.assertTrue(runner._compacted_this_turn)
        # 回退后首条是 system 摘要（本地兜底）
        self.assertEqual(state.messages[0]["role"], "system")
        # 没有任何 tool 响应缺少前导的 assistant.tool_calls（孤儿检测）
        for i, m in enumerate(state.messages):
            if m.get("role") == "tool":
                self.assertGreater(i, 0)
                self.assertTrue(
                    state.messages[i - 1].get("tool_calls"),
                    "tool 响应前一条必须是带 tool_calls 的 assistant",
                )

    def test_empty_summary_triggers_fallback(self):
        """LLM 返回空字符串时也要回退本地兜底，不能留空摘要。"""
        msgs = self._oversized_messages(40)
        runner, state = _make_runner(msgs, window=2000, summary_text="   ")
        runner._maybe_compact()
        self.assertTrue(runner._compacted_this_turn)
        self.assertGreater(len(state.messages[0]["content"]), 10)

    def test_compacted_emits_event(self):
        """压缩成功后发 compacted 事件，带 removed/summary_chars/tokens_before。"""
        msgs = self._oversized_messages(40)
        runner, state = _make_runner(msgs, window=2000)
        # 不通过 _store 落盘，但事件应进 state.events
        runner._maybe_compact()
        types = [e.type for e in state.events]
        self.assertIn("compacted", types)
        ev = [e for e in state.events if e.type == "compacted"][0]
        self.assertIn("removed", ev.data)
        self.assertIn("summary_chars", ev.data)
        self.assertIn("tokens_before", ev.data)


class LocalFallbackSummaryTests(unittest.TestCase):
    def test_fallback_lists_tools_and_keeps_skeleton(self):
        msgs = [
            _msg("assistant", "a", tool_calls=[_tool_call(name="get_project_overview")]),
            _msg("tool", "{}"),
            _msg("assistant", "b", tool_calls=[_tool_call(name="start_translation")]),
            _msg("tool", "{}"),
        ]
        text = _local_fallback_summary(msgs)
        self.assertIn("已完成的工作", text)
        self.assertIn("get_project_overview", text)
        self.assertIn("start_translation", text)
        # 重复工具名去重
        self.assertEqual(text.count("get_project_overview"), 1)


# ---- 4. 会话生命周期（经 AgentRuntime）----

class SessionLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._root = tempfile.mkdtemp(prefix="agent-life-root-")
        self._orig_root = ss.SESSIONS_ROOT
        ss.SESSIONS_ROOT = self._root
        self.project = os.path.join(tempfile.mkdtemp(prefix="agent-life-proj-"), "MyGame")
        os.makedirs(self.project, exist_ok=True)

    def tearDown(self) -> None:
        ss.SESSIONS_ROOT = self._orig_root

    def test_create_session_title_increments(self):
        from GalTransl.Agent.runtime import AgentRuntime

        rt = AgentRuntime()
        s1 = rt.create_session(self.project)
        s2 = rt.create_session(self.project)
        s3 = rt.create_session(self.project)
        self.assertEqual(s1["title"], "MyGame1")
        self.assertEqual(s2["title"], "MyGame2")
        self.assertEqual(s3["title"], "MyGame3")

    def test_delete_session_reduces_list(self):
        from GalTransl.Agent.runtime import AgentRuntime

        rt = AgentRuntime()
        s1 = rt.create_session(self.project)
        s2 = rt.create_session(self.project)
        self.assertEqual(len(rt.list_sessions(self.project)), 2)
        rt.delete_session(self.project, s1["session_id"])
        remaining = rt.list_sessions(self.project)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["session_id"], s2["session_id"])

    def test_sessions_isolated_messages_do_not_cross(self):
        """两个会话的消息互不串读。"""
        from GalTransl.Agent.runtime import AgentRuntime

        rt = AgentRuntime()
        s1 = rt.create_session(self.project)
        s2 = rt.create_session(self.project)
        store1 = ss.SessionStore(self.project, s1["session_id"])
        store2 = ss.SessionStore(self.project, s2["session_id"])
        store1.append_message({"role": "user", "content": "A"})
        store2.append_message({"role": "user", "content": "B"})
        self.assertEqual(len(ss.SessionStore(self.project, s1["session_id"]).load()["messages"]), 1)
        self.assertEqual(len(ss.SessionStore(self.project, s2["session_id"]).load()["messages"]), 1)
        self.assertEqual(store1.load()["messages"][0]["content"], "A")
        self.assertEqual(store2.load()["messages"][0]["content"], "B")

    def test_status_restores_from_disk_after_in_memory_eviction(self):
        """模拟进程重启：内存清空后 status() 应从磁盘恢复。"""
        from GalTransl.Agent.runtime import AgentRuntime

        rt = AgentRuntime()
        s = rt.create_session(self.project)
        store = ss.SessionStore(self.project, s["session_id"])
        store.append_message({"role": "user", "content": "重启前的消息"})
        store.append_meta(title=s["title"], project_dir=self.project)

        # 模拟重启：换一个全新 Runtime（内存全空）
        rt2 = AgentRuntime()
        st = rt2.status(self.project, s["session_id"])
        self.assertEqual(st["session_id"], s["session_id"])
        self.assertEqual(st["title"], s["title"])
        # 恢复后会带上之前的消息历史
        self.assertNotEqual(st["status"], "running")  # 没 running 标记 -> awaiting_input


if __name__ == "__main__":
    unittest.main()
