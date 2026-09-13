"""流式响应处理的单元测试：思考内容（reasoning_content）提取。

推理模型把思考内容放在非标准字段里，位置因平台而异：
- delta.reasoning_content 直接属性（部分平台/SDK 版本）；
- delta.model_extra['reasoning_content']（OpenAI SDK 把非标准字段收进
  model_extra，DeepSeek-R1/GLM 常见）；
- delta.model_extra['reasoning']（OpenRouter 等平台键名）。

约定：思考内容走独立的 reasoning_delta 事件流（前端渲染成可折叠的
「思考中」卡片），绝不进返回的 content、不写对话历史（发回 provider
会被拒收）。模型「说」的回复正文仍走 content_delta / content_end。

不依赖真实 OpenAI / 后端：用 SimpleNamespace 伪造流式 chunk。
"""

import unittest
from types import SimpleNamespace

from GalTransl.Agent.runtime import AgentRunner, AgentState


def _delta_chunk(content=None, reasoning=None, extra=None, tool_calls=None, finish=None):
    attrs = {"content": content, "tool_calls": tool_calls}
    if reasoning is not None:
        attrs["reasoning_content"] = reasoning
    if extra is not None:
        attrs["model_extra"] = extra
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(**attrs), finish_reason=finish)],
        usage=None,
    )


def _tool_call_chunk(finish="tool_calls"):
    return _delta_chunk(
        tool_calls=[SimpleNamespace(index=0, id="c1", function=SimpleNamespace(name="get_runtime", arguments="{}"))],
        finish=finish,
    )


class ReasoningStreamTests(unittest.TestCase):
    def _make_runner(self, chunks):
        state = AgentState()
        state.session_id = ""  # 不落盘
        state.messages = [{"role": "user", "content": "hi"}]
        runner = AgentRunner(state)
        runner._openai_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: iter(chunks)))
        )
        runner._model = "fake"
        return runner

    def _reasoning_text(self, runner):
        """思考流水合：reasoning_delta 是瞬态事件，只在 transient_events 里。"""
        return "".join(
            e.data.get("delta", "") for e in runner.state.transient_events if e.type == "reasoning_delta"
        )

    def _content_text(self, runner):
        """回复正文合：content_delta 也是瞬态事件。"""
        return "".join(
            e.data.get("delta", "") for e in runner.state.transient_events if e.type == "content_delta"
        )

    def test_model_extra_reasoning_content(self):
        """model_extra 里的 reasoning_content 进思考流，回复正文进 content 流，两者独立。"""
        r = self._make_runner([
            _delta_chunk(extra={"reasoning_content": "用户说了你好"}),
            _delta_chunk(content="我先查一下项目。"),
            _tool_call_chunk(),
        ])
        content, tool_calls, finish = r._stream_llm_response()
        self.assertEqual(content, "我先查一下项目。")
        self.assertEqual(tool_calls[0]["name"], "get_runtime")
        self.assertEqual(finish, "tool_calls")
        # 思考流与回复流各自独立，互不串
        self.assertEqual(self._reasoning_text(r), "用户说了你好")
        self.assertEqual(self._content_text(r), "我先查一下项目。")
        # 各发各的 end：content 有 -> content_end；reasoning 有 -> reasoning_end
        end_types = [e.type for e in r.state.events if e.type.endswith("_end")]
        self.assertIn("content_end", end_types)
        self.assertIn("reasoning_end", end_types)
        r_end = next(e for e in r.state.events if e.type == "reasoning_end")
        self.assertEqual(r_end.data["length"], len("用户说了你好"))

    def test_direct_reasoning_content_attribute(self):
        """delta.reasoning_content 直接属性（部分平台/SDK 版本）。"""
        r = self._make_runner([
            _delta_chunk(reasoning="直接属性上的思考"),
            _delta_chunk(content="回复正文", finish="stop"),
        ])
        content, tool_calls, _ = r._stream_llm_response()
        self.assertEqual(content, "回复正文")
        self.assertEqual(tool_calls, [])
        self.assertEqual(self._reasoning_text(r), "直接属性上的思考")
        self.assertEqual(self._content_text(r), "回复正文")

    def test_model_extra_reasoning_key(self):
        """model_extra['reasoning']（OpenRouter 等平台键名）。"""
        r = self._make_runner([_delta_chunk(content="ok", extra={"reasoning": "OR风格思考"}, finish="stop")])
        content, _, _ = r._stream_llm_response()
        self.assertEqual(content, "ok")
        self.assertIn("OR风格思考", self._reasoning_text(r))

    def test_reasoning_only_still_emits_reasoning_end(self):
        """只有思考、没有 content（+ tool_calls）：reasoning_end 仍发，content_end 不发。"""
        r = self._make_runner([
            _delta_chunk(extra={"reasoning_content": "只想不答"}),
            _tool_call_chunk(),
        ])
        content, tool_calls, _ = r._stream_llm_response()
        self.assertEqual(content, "")
        self.assertEqual(len(tool_calls), 1)
        end_types = [e.type for e in r.state.events if e.type.endswith("_end")]
        self.assertIn("reasoning_end", end_types)
        self.assertNotIn("content_end", end_types)
        self.assertIn("只想不答", self._reasoning_text(r))

    def test_reasoning_never_enters_history(self):
        """思考内容不写进对话历史（发回 provider 会被拒收）。"""
        r = self._make_runner([
            _delta_chunk(extra={"reasoning_content": "只想不答"}),
            _tool_call_chunk(),
        ])
        r._stream_llm_response()
        self.assertTrue(all("只想不答" not in str(m) for m in r.state.messages))

    def test_plain_stream_unchanged(self):
        """无思考内容：行为与之前一致，只发 content_end，无 reasoning_end。"""
        r = self._make_runner([_delta_chunk(content="普通回复", finish="stop")])
        content, _, _ = r._stream_llm_response()
        self.assertEqual(content, "普通回复")
        end = [e for e in r.state.events if e.type == "content_end"][0]
        self.assertEqual(end.data["length"], len("普通回复"))
        self.assertFalse([e for e in r.state.events if e.type == "reasoning_end"])

    def test_interleaved_stream_ends_per_segment(self):
        """交替思考（想→说→想→说）：每段切换当场收尾，各发各的 end。

        回归背景：GLM-4.6 等模型一路流里 reasoning/content 来回切换，旧实现
        在流末尾才发一对 end，前端光标永远撤不掉（「思考中」卡片不收起）。
        """
        r = self._make_runner([
            _delta_chunk(reasoning="想1"),
            _delta_chunk(content="说1"),
            _delta_chunk(reasoning="想2"),
            _delta_chunk(content="说2", finish="stop"),
        ])
        content, _, _ = r._stream_llm_response()
        self.assertEqual(content, "说1说2")
        ends = [(e.type, e.data["length"]) for e in r.state.events if e.type.endswith("_end")]
        # 每次切换收掉上一段，流结束收掉最后一段：共 4 个 end，按序成对
        self.assertEqual(
            ends,
            [
                ("reasoning_end", len("想1")),
                ("content_end", len("说1")),
                ("reasoning_end", len("想1想2")),
                ("content_end", len("说1说2")),
            ],
        )

    def test_reasoning_content_switch_closes_reasoning_segment(self):
        """最常见的一想一说：切换时收掉思考段，流结束收掉回复段。"""
        r = self._make_runner([
            _delta_chunk(reasoning="先想"),
            _delta_chunk(content="后说", finish="stop"),
        ])
        content, _, _ = r._stream_llm_response()
        self.assertEqual(content, "后说")
        ends = [e.type for e in r.state.events if e.type.endswith("_end")]
        self.assertEqual(ends, ["reasoning_end", "content_end"])
        r_end = next(e for e in r.state.events if e.type == "reasoning_end")
        self.assertEqual(r_end.data["length"], len("先想"))

    def test_segment_end_flushes_throttled_tail_first(self):
        """段收尾前先冲掉节流缓冲：end 之前该段增量必须全部送达。

        回归背景：25ms 节流窗口内的尾巴增量若不先冲掉，会晚于 end 到达，
        前端只能给它新起一张永远等不到 end 的孤立残段卡片。
        """
        r = self._make_runner([
            _delta_chunk(reasoning="R1"),  # 立即冲刷
            _delta_chunk(reasoning="R2"),  # 节流窗口内 -> 滞留缓冲
            _delta_chunk(content="C1"),    # 切换 -> 先冲 R2 再发 reasoning_end
        ])
        _, _, _ = r._stream_llm_response()
        deltas = [e.data.get("delta", "") for e in r.state.transient_events if e.type == "reasoning_delta"]
        self.assertEqual(deltas, ["R1", "R2"])
        # 两个思考增量都必须在 reasoning_end 之前到达
        types = [e.type for e in r.state.transient_events] + [e.type for e in r.state.events]
        # transient 与 events 是两条通道，按 step 排序看全局顺序
        all_events = sorted(
            [
                *(e for e in r.state.transient_events),
                *(e for e in r.state.events),
            ],
            key=lambda e: e.step,
        )
        seen_reasoning_end = False
        for e in all_events:
            if e.type == "reasoning_end":
                seen_reasoning_end = True
            elif e.type == "reasoning_delta" and seen_reasoning_end:
                self.fail(f"reasoning_delta 晚于 reasoning_end 到达: {e.data!r}")
        self.assertTrue(seen_reasoning_end)


if __name__ == "__main__":
    unittest.main()
