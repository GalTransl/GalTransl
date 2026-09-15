"""thinking 模式下必须把 reasoning_content 回传给 API（带 tools 的请求的硬要求）。

报错原文（DeepSeek / 走它校验的中转）：
    Error code: 400 - The `reasoning_content` in the thinking mode must be passed back to
    the API.  即使该轮模型未实际进行工具调用。
官方要求：只要请求带了 tools，后续所有请求都要带上此前产生的 reasoning_content。

原来的实现是反的——注释写着"思考绝不进对话历史（发回 provider 会被拒收）"，流里的思考
只走 reasoning_delta 给界面看，assistant 消息里没有这个字段，于是下一轮请求 400。

现在三层保证：
1. 流里收到思考就把它连同**原字段名**（reasoning_content / reasoning）存进 assistant 消息；
2. 发请求前 _messages_for_request 给缺字段的 assistant 消息补空串（thinking 会话才补，
   非 thinking 会话原样发，不给 provider 塞陌生字段）；
3. 历史是修复前存的（根本没这个字段）→ API 报这个 400 时认出字段名、补上重试一次。
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from GalTransl.Agent.runtime import (
    REASONING_FIELD_NAMES,
    AgentRunner,
    AgentState,
    _reasoning_echo,
    _requested_reasoning_field,
)

PROFILE = {
    "OpenAI-Compatible": {
        "tokens": [{"token": "sk-x", "endpoint": "https://api.example.com", "modelName": "deepseek-flash"}]
    }
}

# 走 DeepSeek 校验的转中，报错文案就是这句
REASONING_ERROR_TEXT = (
    "Error code: 400 - {'error': {'message': 'The `reasoning_content` in the thinking mode "
    "must be passed back to the API.', 'type': 'invalid_request_error'}}"
)


def _runner(messages: list[dict]) -> AgentRunner:
    state = AgentState()
    state.session_id = ""  # 不落盘
    state.messages = messages
    return AgentRunner(state)


def _fake_client(behaviors: list) -> tuple[SimpleNamespace, list[dict]]:
    """假 OpenAI 客户端：第 n 次 create 用 behaviors[n-1]（异常就抛，否则当流迭代）。"""
    calls: list[dict] = []

    def create(**kwargs):
        calls.append(kwargs)
        behavior = behaviors[min(len(calls) - 1, len(behaviors) - 1)]
        if isinstance(behavior, BaseException):
            raise behavior
        return iter(behavior)

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return client, calls


def _http_error(message: str, status: int = 400) -> Exception:
    """带 status_code 的假 HTTP 错误（_is_unsupported_param_error 会看状态码）。"""
    exc = RuntimeError(message)
    exc.status_code = status  # type: ignore[attr-defined]
    return exc


def _chunk(content=None, reasoning=None, extra=None, tool_calls=None, finish=None):
    attrs = {"content": content, "tool_calls": tool_calls}
    if reasoning is not None:
        attrs["reasoning_content"] = reasoning
    if extra is not None:
        attrs["model_extra"] = extra
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(**attrs), finish_reason=finish)],
        usage=None,
    )


class ReasoningEchoHelperTests(unittest.TestCase):
    def test_echo_uses_the_field_name_seen_in_the_stream(self):
        acc = {"reasoning": ["想一下"], "reasoning_field": "reasoning"}
        self.assertEqual(_reasoning_echo(acc), {"reasoning": "想一下"})

        acc = {"reasoning": ["想一下"], "reasoning_field": "reasoning_content"}
        self.assertEqual(_reasoning_echo(acc), {"reasoning_content": "想一下"})

    def test_no_reasoning_means_no_field(self):
        self.assertEqual(_reasoning_echo(None), {})
        self.assertEqual(_reasoning_echo({"content": ["话"]}), {})
        self.assertEqual(_reasoning_echo({"reasoning": []}), {})

    def test_requested_field_is_read_from_the_error(self):
        self.assertEqual(_requested_reasoning_field(Exception(REASONING_ERROR_TEXT)), "reasoning_content")
        self.assertEqual(
            _requested_reasoning_field(Exception("thinking mode needs `reasoning` back")), "reasoning"
        )
        self.assertEqual(_requested_reasoning_field(Exception("rate limit exceeded")), "")
        self.assertEqual(_requested_reasoning_field(Exception("connection reset by peer")), "")


class MessagesForRequestTests(unittest.TestCase):
    """发请求前给 assistant 消息补齐思考字段，且不影响非 thinking 会话。"""

    HISTORY = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "先看看", "reasoning_content": "想过了"},
        {
            "role": "assistant",
            "reasoning_content": "",
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "wait", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "{}"},
        {"role": "assistant", "content": "收尾"},
    ]

    def test_missing_reasoning_is_padded_and_existing_kept(self):
        runner = _runner([dict(m) for m in self.HISTORY])
        runner._reasoning_field = "reasoning_content"

        messages = runner._messages_for_request()

        self.assertEqual(messages[2]["reasoning_content"], "想过了")  # 原有的不动
        self.assertEqual(messages[5]["reasoning_content"], "")  # 缺的补空串
        # 非 assistant 消息不塞字段
        for index in (0, 1, 4):
            self.assertNotIn("reasoning_content", messages[index])
        # 不改动历史本身（补的是发出去的那份）
        self.assertNotIn("reasoning_content", runner.state.messages[5])

    def test_field_is_detected_from_history(self):
        """进程内还没见过思考（刚重启/刚恢复会话），历史里有也能认出来。"""
        runner = _runner([dict(m) for m in self.HISTORY])
        self.assertEqual(runner._reasoning_field, "")

        messages = runner._messages_for_request()

        self.assertEqual(messages[5]["reasoning_content"], "")

    def test_openrouter_style_field_name_is_respected(self):
        history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "答", "reasoning": "OR 风格思考"},
            {"role": "assistant", "content": "再答"},
        ]
        runner = _runner(history)
        messages = runner._messages_for_request()

        self.assertEqual(messages[1]["reasoning"], "OR 风格思考")
        self.assertEqual(messages[2]["reasoning"], "")
        self.assertNotIn("reasoning_content", messages[2])  # 不混用另一套键名

    def test_non_thinking_session_is_untouched(self):
        history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "答"},
            {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": {}}]},
        ]
        runner = _runner([dict(m) for m in history])
        messages = runner._messages_for_request()

        self.assertEqual(messages, history)
        for name in REASONING_FIELD_NAMES:
            self.assertFalse(any(name in m for m in messages))


class StreamEchoTests(unittest.TestCase):
    """流式响应落定的 assistant 消息要带上这一轮的思考。"""

    def _run_one_turn(self, acc: dict, *, with_tools: bool):
        runner = _runner([{"role": "user", "content": "hi"}])
        calls = {"n": 0}

        def fake_stream(_self):
            calls["n"] += 1
            # 真实现里 acc 由 _stream_llm_attempt 挂上，这里照着模拟
            runner._stream_acc = acc
            if calls["n"] == 1 and with_tools:
                return ("先看看。", [{"id": "c1", "name": "wait", "arguments": "{}"}], "tool_calls")
            return ("做完了。", [], "stop")

        with (
            patch.object(AgentRunner, "_resolve_llm", lambda self: None),
            patch.object(AgentRunner, "_stream_llm_response", fake_stream),
            patch.object(AgentRunner, "_dispatch_tool", lambda self, name, args: {"ok": True}),
        ):
            runner.run()
        return runner.state.messages

    def test_tool_call_message_carries_reasoning(self):
        acc = {"content": ["先看看。"], "reasoning": ["查一下再决定"], "tools": {}, "reasoning_field": "reasoning_content"}
        messages = self._run_one_turn(acc, with_tools=True)

        tool_call_msg = next(m for m in messages if m.get("tool_calls"))
        self.assertEqual(tool_call_msg["reasoning_content"], "查一下再决定")
        # 未调工具的那条收尾回复同样要带（官方明确要求"即使该轮未实际进行工具调用"）
        final_msg = messages[-1]
        self.assertEqual(final_msg["role"], "assistant")
        self.assertEqual(final_msg["reasoning_content"], "查一下再决定")

    def test_openrouter_style_reasoning_key(self):
        acc = {"content": ["先看看。"], "reasoning": ["OR 思考"], "tools": {}, "reasoning_field": "reasoning"}
        messages = self._run_one_turn(acc, with_tools=True)

        tool_call_msg = next(m for m in messages if m.get("tool_calls"))
        self.assertEqual(tool_call_msg["reasoning"], "OR 思考")
        self.assertNotIn("reasoning_content", tool_call_msg)

    def test_no_reasoning_no_extra_field(self):
        acc = {"content": ["先看看。"], "reasoning": [], "tools": {}}
        messages = self._run_one_turn(acc, with_tools=True)

        for message in messages:
            self.assertNotIn("reasoning_content", message)
            self.assertNotIn("reasoning", message)


class RealStreamRoundTripTests(unittest.TestCase):
    """走真实流式路径跑一个回合：产生的思考必须出现在下一轮请求里。"""

    def test_reasoning_survives_into_next_request(self):
        runner = _runner([{"role": "user", "content": "hi"}])
        runner._model = "deepseek-flash"
        first = [
            _chunk(extra={"reasoning_content": "先想一下"}),
            _chunk(
                tool_calls=[
                    SimpleNamespace(
                        index=0, id="c1", function=SimpleNamespace(name="get_runtime", arguments="{}")
                    )
                ],
                finish="tool_calls",
            ),
        ]
        second = [
            _chunk(extra={"reasoning_content": "再想一下"}),
            _chunk(content="做完了", finish="stop"),
        ]
        client, calls = _fake_client([first, second])
        runner._openai_client = client

        with (
            patch.object(AgentRunner, "_resolve_llm", lambda self: None),
            patch.object(AgentRunner, "_dispatch_tool", lambda self, name, args: {"ok": True}),
        ):
            runner.run()

        # 第一轮请求时历史里还没有 assistant 消息
        self.assertNotIn("reasoning_content", calls[0]["messages"][-1])
        # 第二轮必须把上一轮 assistant 的思考原样带回 —— 这就是 400 的修法
        assistant_tool_msg = next(m for m in calls[1]["messages"] if m.get("tool_calls"))
        self.assertEqual(assistant_tool_msg["reasoning_content"], "先想一下")
        # 落盘的历史里也带着（刷新/重启后接着跑还得回传）
        persisted = next(m for m in runner.state.messages if m.get("tool_calls"))
        self.assertEqual(persisted["reasoning_content"], "先想一下")
        # 字段名取自流里，回传用的是同一个键
        self.assertNotIn("reasoning", assistant_tool_msg)


class ReasoningRetryTests(unittest.TestCase):
    """老会话（历史里没有该字段）被 API 拒了：认出字段名、补齐后重试一次。"""

    def test_reasoning_400_triggers_one_padded_retry(self):
        runner = _runner([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": {}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "{}"},
        ])
        # 老历史 / 上线前压进去的断点会话：不带思考字段，先被 400 拒
        history_without_field = [dict(m) for m in runner.state.messages]
        chunks = [_chunk(content="好", finish="stop")]
        client, calls = _fake_client([_http_error(REASONING_ERROR_TEXT), chunks])
        runner._openai_client = client
        runner._model = "deepseek-flash"

        stream = runner._open_stream(include_usage=True)
        self.assertEqual(list(stream), chunks)

        self.assertEqual(len(calls), 2)  # 补上后重试了一次
        self.assertEqual(runner._reasoning_field, "reasoning_content")
        self.assertNotIn("reasoning_content", history_without_field[1])  # 原始历史没被改
        self.assertEqual(calls[1]["messages"][1]["reasoning_content"], "")  # 重试的那份补上了

    def test_retry_happens_only_once(self):
        runner = _runner([{"role": "assistant", "content": "答"}])
        client, calls = _fake_client([_http_error(REASONING_ERROR_TEXT)])
        runner._openai_client = client

        with self.assertRaises(RuntimeError):
            runner._open_stream(include_usage=True)

        self.assertEqual(len(calls), 2)  # 补过之后再报同样的错就直说，不无限重试

    def test_unrelated_error_is_not_retried(self):
        runner = _runner([{"role": "assistant", "content": "答"}])
        client, calls = _fake_client([_http_error("502 bad gateway", 502)])
        runner._openai_client = client

        with self.assertRaises(RuntimeError):
            runner._open_stream(include_usage=True)

        self.assertEqual(len(calls), 1)
        self.assertEqual(runner._reasoning_field, "")

    def test_stream_options_fallback_still_works(self):
        """旧的兜底没被新分支挤掉：不认 stream_options 时摘掉它重试。"""
        runner = _runner([{"role": "user", "content": "hi"}])
        chunks = [_chunk(content="好", finish="stop")]
        client, calls = _fake_client(
            [_http_error("400 - Unsupported parameter: stream_options"), chunks]
        )
        runner._openai_client = client

        stream = runner._open_stream(include_usage=True)
        self.assertEqual(list(stream), chunks)
        self.assertIn("stream_options", calls[0])
        self.assertNotIn("stream_options", calls[1])


if __name__ == "__main__":
    unittest.main()
