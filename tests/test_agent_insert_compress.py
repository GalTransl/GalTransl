"""上下文压缩迁移：Insert-then-Compress、溢出恢复、缓存断点、归档召回。

对应 runtime 里这几件事：

- **Insert-then-Compress**：压缩不另开摘要请求，而是把指令（不落盘）拼在会话末尾，
  用下一轮正常请求拿摘要——system prompt / tools / 历史前缀全部复用；
- **摘要单独成条**：压缩后 system 原样保留（前缀缓存不废），摘要作为 system 之后的
  一条 user 消息，并附归档索引；
- **归档召回**：被压掉的历史写成 chunk 文件，read_history_archive 负责回查；
- **溢出恢复**：400 context too long → 强制压缩一次（先弹尾部腾空间）再重试；
- **缓存断点**：Anthropic 系注入 cache_control 双 marker，其它后端不注入。

这些都不打网络：压缩响应是脚本化的，存储落在临时目录里。
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from GalTransl.Agent import runtime as rt
from GalTransl.Agent import session_store as ss
from GalTransl.Agent.runtime import AgentRunner, AgentState, AgentToolError


def _state() -> AgentState:
    state = AgentState()
    state.project_dir = tempfile.mkdtemp(prefix="agent-ic-proj-")
    state.session_id = "sess-ic-1"
    return state


def _msg(role: str, content: str = "x", **extra) -> dict:
    return {"role": role, "content": content, **extra}


def _runner(state: AgentState, *, window: int = rt.DEFAULT_CONTEXT_WINDOW) -> AgentRunner:
    runner = AgentRunner(state)
    runner._model = "fake-model"
    runner._context_window = window
    return runner


def _oversized(n: int = 40) -> list[dict]:
    """远超压缩阈值的一组消息（带 system 打头）。"""
    return [_msg("system", "SYS")] + [_msg("user", "word " * 200) for _ in range(n)]


class _TempSessions(unittest.TestCase):
    """把会话根目录指到临时目录：归档会真的落盘，但不污染仓库。"""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="agent-ic-root-")
        patcher = patch.object(ss, "SESSIONS_ROOT", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)


# ---- 1. 缓存断点 ----


class PromptCacheTests(unittest.TestCase):
    def test_two_markers_from_tail_skipping_internal_messages(self) -> None:
        msgs = [
            _msg("user", "a"),
            _msg("assistant", "b"),
            _msg("user", "压缩指令", _compact_instruction=True),
        ]
        tools = [{"type": "function", "function": {"name": "t"}}]
        out, cached_tools = rt._apply_prompt_cache(msgs, tools)

        for index in (0, 1):
            blocks = out[index]["content"]
            self.assertIsInstance(blocks, list)
            self.assertEqual(blocks[-1]["cache_control"], {"type": "ephemeral"})
        # 瞬时指令消息不打标记（下一轮不会以同样形式出现，标了也是白写）
        self.assertEqual(out[2]["content"], "压缩指令")
        # 工具表末尾也挂一个断点
        self.assertEqual(cached_tools[-1]["cache_control"], {"type": "ephemeral"})
        # 原列表不被就地改写
        self.assertEqual(msgs[0]["content"], "a")

    def test_only_marks_the_real_tail_when_fewer_than_two(self) -> None:
        out, _tools = rt._apply_prompt_cache([_msg("user", "only")], [])
        self.assertEqual(len(out), 1)
        self.assertIsInstance(out[0]["content"], list)

    def test_auto_resolution(self) -> None:
        self.assertFalse(rt._resolve_prompt_caching("auto", "deepseek-v4", "https://api.deepseek.com/v1"))
        self.assertTrue(rt._resolve_prompt_caching("auto", "claude-sonnet-4", "https://relay/v1"))
        self.assertTrue(rt._resolve_prompt_caching("auto", "x", "https://openrouter.ai/api/v1"))
        self.assertTrue(rt._resolve_prompt_caching("on", "deepseek-v4", "https://api.deepseek.com"))
        self.assertFalse(rt._resolve_prompt_caching("off", "claude-sonnet-4", "https://api.anthropic.com"))


# ---- 2. 内部标记不外发 ----


class InternalFieldTests(unittest.TestCase):
    def test_request_messages_strip_marks(self) -> None:
        state = _state()
        state.messages = [
            _msg("system", "sys"),
            _msg("user", "摘要正文", _compact_summary=True, chunk_path="chunk-0001.md"),
        ]
        runner = _runner(state)

        out = runner._messages_for_request()

        self.assertNotIn("_compact_summary", out[1])
        self.assertEqual(out[1].get("content"), "摘要正文")
        # 内存里的历史不受影响（回滚、判断还要用标记）
        self.assertTrue(state.messages[1]["_compact_summary"])


# ---- 3. Insert-then-Compress ----


class InsertThenCompressTests(_TempSessions):
    def test_begin_injects_instruction_and_abort_rolls_back(self) -> None:
        state = _state()
        state.messages = _oversized()
        runner = _runner(state, window=2000)
        before = len(state.messages)

        self.assertTrue(runner._begin_compaction())

        self.assertEqual(len(state.messages), before + 1)
        self.assertTrue(state.messages[-1]["_compact_instruction"])
        # 指令只进内存、不落盘：压缩失败回滚时才不会把历史弄脏
        raw = ""
        if os.path.exists(runner._store.path):
            with open(runner._store.path, encoding="utf-8") as handle:
                raw = handle.read()
        self.assertNotIn("记忆压缩模式", raw)

        runner._abort_compaction()
        self.assertEqual(len(state.messages), before)
        self.assertIsNone(runner._pending_compaction)

    def test_finish_keeps_system_and_writes_archive(self) -> None:
        state = _state()
        state.messages = _oversized()
        runner = _runner(state, window=2000)
        runner._begin_compaction()

        ok = runner._finish_compaction(
            "<topics>术语, 序章</topics><summary>## 目标\n译完序章</summary>"
        )

        self.assertTrue(ok)
        # system 原样保留（不是重建的、也不含摘要）——前缀缓存不废的关键
        self.assertEqual(state.messages[0], {"role": "system", "content": "SYS"})
        self.assertEqual(state.messages[1]["role"], "user")
        self.assertIn("译完序章", state.messages[1]["content"])
        self.assertTrue(state.messages[1]["_compact_summary"])
        self.assertFalse(any(m.get("_compact_instruction") for m in state.messages))
        self.assertEqual(state.last_prompt_tokens, 0)
        self.assertEqual(state.anchored_message_count, 0)

        chunks = runner._store.list_chunks()
        self.assertEqual(len(chunks), 1)
        self.assertIn("术语", chunks[0]["topics"])
        self.assertIn(chunks[0]["name"], state.messages[1]["content"])
        self.assertIn("compacted", [event.type for event in state.events])
        # 压缩后的大小是重建后现场估的（不是摘要长度）：事件里要能拿到
        ev = [event for event in state.events if event.type == "compacted"][0]
        self.assertEqual(ev.data["tokens_after"], runner._estimate_context_tokens())

    def test_empty_summary_rolls_back_instead_of_breaking_history(self) -> None:
        state = _state()
        state.messages = _oversized()
        runner = _runner(state, window=2000)
        before = len(state.messages)
        runner._begin_compaction()

        self.assertFalse(runner._finish_compaction("   "))

        self.assertEqual(len(state.messages), before)
        self.assertFalse(runner._compacted_this_turn)
        self.assertFalse(any(m.get("_compact_instruction") for m in state.messages))

    def test_run_reuses_the_current_conversation_for_the_summary(self) -> None:
        """主循环：压缩那一轮的请求带着指令、且流式增量被静音。"""
        state = _state()
        state.messages = _oversized()
        runner = _runner(state, window=2000)
        rounds: list[bool] = []

        def fake_stream(*_args, **_kwargs):
            rounds.append(runner._stream_quiet)
            if len(rounds) == 1:
                self.assertTrue(state.messages[-1].get("_compact_instruction"))
                return "<topics>t</topics><summary>## 目标\n压缩完成</summary>", [], "stop"
            return "干完了", [], "stop"

        with (
            patch.object(runner, "_resolve_llm", lambda: None),
            patch.object(runner, "_stream_llm_response", side_effect=fake_stream),
        ):
            runner.run()

        self.assertEqual(rounds, [True, False])  # 压缩请求静音，正常请求不静音
        contents = " ".join(str(m.get("content")) for m in state.messages)
        self.assertIn("压缩完成", contents)
        self.assertFalse(any(m.get("_compact_instruction") for m in state.messages))

    def test_summary_failure_falls_back_to_a_separate_request(self) -> None:
        """压缩请求失败（返回工具调用）时降级为独立摘要请求，不让回合挂掉。"""
        state = _state()
        state.messages = _oversized()
        runner = _runner(state, window=2000)
        runner._begin_compaction()
        runner._abort_compaction()  # 模拟降级前的回滚已完成

        calls: list[str] = []

        class _FakeOpenAI:
            class chat:  # noqa: N801 - 贴近 SDK 形状
                class completions:  # noqa: N801
                    @staticmethod
                    def create(**_kwargs):
                        calls.append("separate")
                        message = type("M", (), {"content": "## 目标\n独立摘要"})()
                        return type("R", (), {"choices": [type("C", (), {"message": message})()]})()

        runner._openai_client = _FakeOpenAI()
        runner._compact_via_separate_request()

        self.assertEqual(calls, ["separate"])
        self.assertIn("独立摘要", state.messages[1]["content"])
        self.assertTrue(state.messages[1]["_compact_summary"])

    def test_failed_compaction_stops_retrying_within_the_turn(self) -> None:
        """压缩失败后本回合不再尝试：否则主循环会「注入 → 失败 → 回滚」空转到 MAX_STEPS。"""
        state = _state()
        state.messages = _oversized()
        runner = _runner(state, window=2000)

        runner._compact_via_separate_request(cut=0)  # 降级路径也没得压

        self.assertTrue(runner._compact_failed_this_turn)
        self.assertFalse(runner._begin_compaction())


# ---- 4. 溢出恢复 ----


class ContextOverflowTests(unittest.TestCase):
    def test_context_too_large_raises_overflow_once_then_reports(self) -> None:
        state = _state()
        state.messages = [_msg("user", "x")]
        runner = _runner(state)
        boom = Exception("This model's maximum context length is 8192 tokens: 400")
        boom.status_code = 400  # type: ignore[attr-defined]

        with patch.object(runner, "_stream_llm_attempt", side_effect=boom):
            with self.assertRaises(rt._ContextOverflow):
                runner._stream_llm_response()
            # 恢复机会只有一次：第二次按原错误抛出，交给上层报错
            with self.assertRaises(Exception) as ctx:
                runner._stream_llm_response()
        self.assertNotIsInstance(ctx.exception, rt._ContextOverflow)

    def test_overflow_recovery_is_reset_between_turns(self) -> None:
        state = _state()
        runner = _runner(state)
        runner._overflow_recovery_used = True
        runner._close_turn("done", {})
        self.assertFalse(runner._overflow_recovery_used)


# ---- 5. 归档召回 ----


class ReadHistoryArchiveTests(_TempSessions):
    def _runner(self) -> AgentRunner:
        runner = _runner(_state())
        runner._store.write_chunk(
            "chunk-0001.md",
            "---\nsession_id: s\ntopics: 术语, 序章\n---\n\n## 用户\n\n欧派怎么译\n",
        )
        return runner

    def test_lists_archives(self) -> None:
        out = rt._tool_read_history_archive(self._runner(), {})
        self.assertEqual(len(out["archives"]), 1)
        self.assertEqual(out["archives"][0]["name"], "chunk-0001.md")
        self.assertEqual(out["archives"][0]["topics"], "术语, 序章")

    def test_reads_by_index_and_searches_by_query(self) -> None:
        runner = self._runner()
        by_index = rt._tool_read_history_archive(runner, {"chunk": "1"})
        self.assertIn("欧派怎么译", by_index["content"])

        hits = rt._tool_read_history_archive(runner, {"query": "欧派"})
        self.assertEqual(len(hits["hits"]), 1)
        self.assertEqual(hits["hits"][0]["chunk"], "chunk-0001.md")

    def test_unknown_chunk_reports_available_names(self) -> None:
        with self.assertRaises(AgentToolError) as ctx:
            rt._tool_read_history_archive(self._runner(), {"chunk": "nope.md"})
        self.assertIn("chunk-0001.md", str(ctx.exception))

    def test_no_archives_is_not_an_error(self) -> None:
        out = rt._tool_read_history_archive(_runner(_state()), {})
        self.assertEqual(out["archives"], [])


if __name__ == "__main__":
    unittest.main()
