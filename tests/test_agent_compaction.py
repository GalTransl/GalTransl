"""上下文压缩（compaction）与会话生命周期的单元测试。

覆盖三类逻辑：
1. 切点安全 —— `_find_compaction_cut` 按 **token 预算**挑保留段，且永远不会切断
   assistant.tool_calls 与后续 tool 响应的配对（否则发给 LLM 的 messages 会因孤儿
   tool_calls / 孤儿 tool 响应而被拒）。
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
    """切点：保留段按 token 预算从尾部往前凑，永远不切断 tool 配对。"""

    def test_keeps_the_tail_that_fits_the_budget(self):
        """保留段装到装不下为止（每条 ~104 tokens，预算 500 → 只留最后几条）。"""
        msgs = [_msg("user", "w" * 400) for _ in range(20)]

        cut = _find_compaction_cut(msgs, 500)

        self.assertGreater(cut, 0)
        kept = sum(_estimate_message_tokens(m) for m in msgs[cut:])
        self.assertLessEqual(kept, 500)
        # 再往前一条就超预算：说明切点取的是"装得下"的最前位置
        self.assertGreater(kept + _estimate_message_tokens(msgs[cut - 1]), 500)

    def test_history_that_fits_the_budget_has_nothing_to_cut(self):
        msgs = [_msg("user", "hi") for _ in range(20)]
        self.assertEqual(_find_compaction_cut(msgs, 10_000), 0)

    def test_last_message_is_kept_even_if_it_blows_the_budget(self):
        """单条就超预算（几万字符的工具结果）：整条留下，不截断内容，压缩仍要能启动。"""
        msgs = [_msg("user", "w" * 400) for _ in range(5)]
        msgs.append(_msg("assistant", "call", tool_calls=[_tool_call()]))
        msgs.append(_msg("tool", "A" * 40_000))

        cut = _find_compaction_cut(msgs, 500)

        # 保留段 = 那次调用 + 结果（配对完整），前面 5 条进摘要
        self.assertEqual(cut, 5)
        self.assertEqual(msgs[cut]["role"], "assistant")
        self.assertEqual(msgs[cut + 1]["content"], "A" * 40_000)

    def test_cut_never_splits_tool_call_and_tool_response(self):
        """结尾一段 assistant(tool_calls) + tool 响应必须整体保留。"""
        # 20 条普通消息后跟 assistant.tool_calls + 对应 tool 响应
        msgs = [_msg("user", "hello") for _ in range(20)]
        msgs.append(_msg("assistant", "调用工具", tool_calls=[_tool_call()]))
        msgs.append(_msg("tool", "结果"))

        # 预算只够最后一条：切点会往前退到 assistant 之前，pair 整体保留
        cut = _find_compaction_cut(msgs, 6)

        self.assertEqual(cut, 20)
        # 切点前一条绝不能是带 tool_calls 的 assistant
        self.assertNotEqual(msgs[cut - 1].get("role") + str(bool(msgs[cut - 1].get("tool_calls"))),
                            "assistantTrue")
        # 切点本身（保留段第一条）绝不能是 tool 响应
        self.assertNotEqual(msgs[cut].get("role"), "tool")

    def test_only_system_in_the_head_means_nothing_to_summarize(self):
        """预算只够留下 system 之后的内容：没东西可摘，返回 0（别白跑一次摘要请求）。"""
        msgs = [_msg("system", "SYS"), _msg("user", "a")]
        self.assertEqual(_find_compaction_cut(msgs, 4), 0)

    def test_single_message_returns_zero(self):
        self.assertEqual(_find_compaction_cut([_msg("user", "x")], 10), 0)

    def test_alternating_tool_pairs_still_find_safe_cut_between_pairs(self):
        """全是 assistant(tool)+tool 交替配对时，切点落在两对之间（tool 之后、
        下一个 assistant 之前），仍属安全位置。"""
        msgs = []
        for _ in range(40):
            msgs.append(_msg("assistant", "c" * 2000, tool_calls=[_tool_call()]))
            msgs.append(_msg("tool", "r" * 2000))
        cut = _find_compaction_cut(msgs, 3_000)
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
        # 压缩后：system（原样）+ 摘要消息 + 尾部保留，且总条数减少
        self.assertLess(len(state.messages), before)
        self.assertEqual(state.messages[0]["role"], "system")
        # 摘要单独成条：system 保持稳定，system + tools 这段前缀缓存才不会被一次压缩全废掉
        self.assertNotIn("我是摘要", state.messages[0]["content"])
        self.assertEqual(state.messages[1]["role"], "user")
        self.assertIn("我是摘要", state.messages[1]["content"])
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

    def test_history_within_the_keep_budget_is_not_compacted(self):
        """整份历史都装得进保留预算：压了只会把摘要再摘要一遍，直接跳过。"""
        msgs = [_msg("user", "w" * 400) for _ in range(10)]  # 约 1k tokens，预算 8k
        runner, state = _make_runner(msgs, window=2000)

        runner._maybe_compact()

        self.assertFalse(runner._compacted_this_turn)
        self.assertEqual(len(state.messages), 10)

    def test_tail_that_alone_hits_the_line_skips_compaction(self):
        """尾部压着一条压不掉的大结果（保留段自身就到触发线）：压缩救不了，别白压一次。

        （PI-Desktop 在同样的情形下直接判 oversized 失败；这里选择跳过。）
        """
        msgs = [_msg("user", "hello") for _ in range(40)]
        msgs.append(_msg("assistant", "call", tool_calls=[_tool_call()]))
        msgs.append(_msg("tool", "A" * 400_000))  # ~100k tokens
        runner, state = _make_runner(msgs, window=DEFAULT_CONTEXT_WINDOW)  # 阈值 94208

        self.assertFalse(runner._begin_compaction())
        self.assertIsNone(runner._pending_compaction)
        self.assertEqual(len(state.messages), 42)  # 历史原样，指令没挂上去

    def test_force_compaction_ignores_the_tail_hits_the_line_guard(self):
        """溢出恢复（force）不走这条：那时只有压缩一条路，压不动也得上。"""
        msgs = [_msg("user", "hello") for _ in range(40)]
        msgs.append(_msg("assistant", "call", tool_calls=[_tool_call()]))
        msgs.append(_msg("tool", "A" * 400_000))
        runner, state = _make_runner(msgs, window=DEFAULT_CONTEXT_WINDOW)

        self.assertTrue(runner._begin_compaction(force=True))
        self.assertIsNotNone(runner._pending_compaction)

    def test_tool_pairs_compacted_safely_without_orphans(self):
        """前缀全是 assistant(tool)+tool 配对时也能压缩，且压缩后尾部无孤儿 tool 响应。"""
        msgs = []
        for _ in range(40):
            msgs.append(_msg("assistant", "c" * 2000, tool_calls=[_tool_call()]))
            msgs.append(_msg("tool", "r" * 2000))
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
        msgs = [_msg("user", "w" * 4000) for _ in range(30)]
        msgs.append(_msg("assistant", "call", tool_calls=[_tool_call()]))
        msgs.append(_msg("tool", "res"))
        runner, state = _make_runner(
            msgs, window=3000, raise_exc=RuntimeError("summary down")
        )
        runner._maybe_compact()
        self.assertTrue(runner._compacted_this_turn)
        # 回退后仍是合法结构：system 打头 + 本地兜底摘要消息
        self.assertEqual(state.messages[0]["role"], "system")
        self.assertIn("[早前对话的压缩摘要", state.messages[1]["content"])
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
        self.assertGreater(len(state.messages[1]["content"]), 10)

    def test_compacted_emits_event(self):
        """压缩成功后发 compacted 事件，带 removed/summary_chars/tokens_before/tokens_after。"""
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
        self.assertIn("tokens_after", ev.data)

    def test_tokens_after_estimates_the_rebuilt_history_not_just_the_summary(self):
        """压缩后的大小 = 在重建出来的真实历史上估算（含保留尾部的大工具结果）。

        回归背景：界面上曾用「摘要字符数 / 4」当压缩后的大小，于是"尾部还压着一条几万
        字符的工具结果"这种情形看起来像压完只剩摘要那么大——实际下一轮请求可能仍贴着上限。
        """
        huge = "A" * 40000  # 约 10k tokens，落在保留的尾部里（压不掉）
        msgs = [_msg("user", "word " * 200) for _ in range(30)]
        msgs.append(_msg("assistant", "call", tool_calls=[_tool_call()]))
        msgs.append(_msg("tool", huge))
        runner, state = _make_runner(msgs, window=2000, summary_text="摘要")

        runner._maybe_compact()

        ev = [e for e in state.events if e.type == "compacted"][0]
        tokens_after = ev.data["tokens_after"]
        # 摘要只有两个字：after 必须远大于"只算摘要"，且至少覆盖那条工具结果本身
        self.assertGreaterEqual(tokens_after, len(huge) // 4)
        self.assertGreater(tokens_after, ev.data["summary_chars"] * 100)
        # 与重建后的现场估算一致（同一个函数、同一份历史）
        self.assertEqual(tokens_after, runner._estimate_context_tokens())
        # 大结果没被压掉：压缩后仍与压缩前同量级
        self.assertGreater(tokens_after, ev.data["tokens_before"] // 2)


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

    def test_create_session_title_is_placeholder(self):
        """新建空会话只有占位标题：此刻用户还没输入，标题等首条消息再定。"""
        from GalTransl.Agent.runtime import AgentRuntime

        rt = AgentRuntime()
        s1 = rt.create_session(self.project)
        s2 = rt.create_session(self.project)
        self.assertEqual(s1["title"], ss.DEFAULT_TITLE)
        self.assertEqual(s2["title"], ss.DEFAULT_TITLE)

    def test_first_message_becomes_session_title(self):
        """首条消息决定会话标题；已经有用户消息的会话不重算。"""
        from GalTransl.Agent.runtime import AgentRuntime, _initial_session_title

        rt = AgentRuntime()
        s = rt.create_session(self.project)
        sid = s["session_id"]
        title = _initial_session_title(
            self.project, sid, "帮我翻译这个游戏\n风格轻松一点", s["title"]
        )
        self.assertEqual(title, "帮我翻译这个游戏 风格轻松一点")

        # 首条消息已落盘（首个回合结束）→ 后续 start 不覆盖已有标题
        ss.SessionStore(self.project, sid).append_message(
            {"role": "user", "content": "帮我翻译这个游戏"}
        )
        self.assertEqual(
            _initial_session_title(self.project, sid, "再翻一次", title),
            title,
        )

    def test_empty_goal_keeps_placeholder_title(self):
        from GalTransl.Agent.runtime import AgentRuntime, _initial_session_title

        rt = AgentRuntime()
        s = rt.create_session(self.project)
        self.assertEqual(
            _initial_session_title(self.project, s["session_id"], "  ", s["title"]),
            ss.DEFAULT_TITLE,
        )

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

    def test_restore_reconstructs_missing_initial_user_event(self):
        """磁盘有消息但首条 user_message 事件缺失时，恢复仍显示首条输入。"""
        from GalTransl.Agent.runtime import AgentRuntime

        rt = AgentRuntime()
        s = rt.create_session(self.project)
        store = ss.SessionStore(self.project, s["session_id"])
        store.append_meta(project_dir=self.project, title=s["title"], goal="首条用户输入")
        store.append_message({"role": "system", "content": "system"})
        store.append_message({"role": "user", "content": "首条用户输入"})
        # 模拟旧版本/异常退出：只有后续事件，没有首条 user_message 事件。
        store.append_event({"type": "content", "step": 2, "content": "已开始处理"})

        st = rt.status(self.project, s["session_id"])
        user_events = [e for e in st["events"] if e.get("type") == "user_message"]
        self.assertEqual(len(user_events), 1)
        self.assertEqual(user_events[0]["message"], "首条用户输入")
        self.assertLess(user_events[0]["step"], st["events"][1]["step"])


if __name__ == "__main__":
    unittest.main()
