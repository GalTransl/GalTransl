import unittest
from types import SimpleNamespace

from GalTransl.Agent import runtime as rt


def _chunk(text: str):
    delta = SimpleNamespace(content=text, tool_calls=None, model_extra=None, reasoning_content=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason="stop")], usage=None)


class _StatusError(Exception):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status_code = status


class _StubCompletions:
    """按脚本依次返回：异常则抛出，否则作为可迭代的 chunk 流返回。"""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def create(self, **_kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return iter(outcome)


class _StubClient:
    def __init__(self, outcomes):
        self.chat = SimpleNamespace(completions=_StubCompletions(outcomes))


class _StopDuringWait:
    """模拟「退避等待期间用户点了停止」：wait() 立刻置位并返回 True。"""

    def __init__(self) -> None:
        self.flag = False

    def is_set(self) -> bool:
        return self.flag

    def wait(self, _timeout=None) -> bool:
        self.flag = True
        return True

    def set(self) -> None:
        self.flag = True


def _runner(outcomes, stop_event=None) -> rt.AgentRunner:
    state = rt.AgentState(project_dir="", session_id="")
    runner = rt.AgentRunner(state, stop_event=stop_event)
    runner._model = "test-model"
    runner._openai_client = _StubClient(outcomes)
    return runner


def _retry_events(runner) -> list[rt.AgentEvent]:
    return [e for e in runner.state.events if e.type.startswith("llm_retry")]


class LLMRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        # 退避时长压到毫秒级，别让测试真的等 1s/2s/4s
        self._orig_initial = rt.LLM_RETRY_INITIAL_DELAY_MS
        self._orig_max = rt.LLM_RETRY_MAX_DELAY_MS
        rt.LLM_RETRY_INITIAL_DELAY_MS = 1
        rt.LLM_RETRY_MAX_DELAY_MS = 2
        self.addCleanup(self._restore_delays)

    def _restore_delays(self) -> None:
        rt.LLM_RETRY_INITIAL_DELAY_MS = self._orig_initial
        rt.LLM_RETRY_MAX_DELAY_MS = self._orig_max

    def test_transient_failure_is_retried_and_visible(self) -> None:
        runner = _runner([ConnectionError("connection reset by peer"), [_chunk("hi")]])

        content, tool_calls, finish = runner._stream_llm_response()

        self.assertEqual(content, "hi")
        self.assertEqual(tool_calls, [])
        self.assertEqual(finish, "stop")
        events = _retry_events(runner)
        self.assertEqual([e.type for e in events], ["llm_retry_start", "llm_retry_end"])
        start = events[0].data
        self.assertEqual(start["attempt"], 1)
        self.assertEqual(start["max_attempts"], rt.LLM_MAX_RETRIES)
        self.assertEqual(start["code"], "NETWORK_ERROR")
        self.assertIn("delay_ms", start)
        self.assertIn("ts", start)

    def test_non_retriable_error_fails_immediately(self) -> None:
        runner = _runner([_StatusError("unauthorized", 401), [_chunk("hi")]])

        with self.assertRaises(_StatusError):
            runner._stream_llm_response()

        self.assertEqual(_retry_events(runner), [])
        self.assertEqual(runner._openai_client.chat.completions.calls, 1)

    def test_exhausted_retries_surface_attempt_count(self) -> None:
        runner = _runner([ConnectionError("connection timed out")] * (rt.LLM_MAX_RETRIES + 1))

        with self.assertRaises(RuntimeError) as ctx:
            runner._stream_llm_response()

        self.assertIn(f"已重试 {rt.LLM_MAX_RETRIES} 次", str(ctx.exception))
        starts = [e for e in _retry_events(runner) if e.type == "llm_retry_start"]
        self.assertEqual(
            [e.data["attempt"] for e in starts],
            list(range(1, rt.LLM_MAX_RETRIES + 1)),
        )
        self.assertEqual(runner._openai_client.chat.completions.calls, rt.LLM_MAX_RETRIES + 1)

    def test_stop_during_backoff_raises_stop_requested(self) -> None:
        runner = _runner([ConnectionError("connection reset")], stop_event=_StopDuringWait())

        with self.assertRaises(rt.AgentStopRequested):
            runner._stream_llm_response()

        ends = [e for e in _retry_events(runner) if e.type == "llm_retry_end"]
        self.assertEqual(len(ends), 1)
        self.assertTrue(ends[0].data["aborted"])

    def test_unsupported_stream_options_falls_back_without_retry(self) -> None:
        runner = _runner([
            _StatusError("unknown parameter: stream_options", 400),
            [_chunk("ok")],
        ])

        content, _, _ = runner._stream_llm_response()

        self.assertEqual(content, "ok")
        self.assertEqual(_retry_events(runner), [])


class ClassifyLLMErrorTests(unittest.TestCase):
    def test_status_mapping(self) -> None:
        cases = {
            401: ("PROVIDER_UNAUTHORIZED", False),
            403: ("PROVIDER_UNAUTHORIZED", False),
            404: ("MODEL_NOT_CONFIGURED", False),
            413: ("CONTEXT_TOO_LARGE", False),
            429: ("RATE_LIMITED", True),
            500: ("PROVIDER_ERROR", True),
            503: ("PROVIDER_ERROR", True),
            400: ("PROVIDER_BAD_REQUEST", False),
        }
        for status, expected in cases.items():
            info = rt._classify_llm_error(_StatusError("boom", status))
            self.assertEqual((info["code"], info["retriable"]), expected, status)

    def test_keyword_fallback(self) -> None:
        self.assertEqual(rt._classify_llm_error(ConnectionError("connection reset"))["code"], "NETWORK_ERROR")
        self.assertEqual(rt._classify_llm_error(TimeoutError("request timed out"))["code"], "TIMEOUT")
        self.assertTrue(rt._classify_llm_error(ConnectionError("connection reset"))["retriable"])

    def test_retry_after_header_wins_for_backoff(self) -> None:
        exc = _StatusError("slow down", 429)
        exc.response = SimpleNamespace(headers={"retry-after": "3"}, status_code=429)
        info = rt._classify_llm_error(exc)
        self.assertEqual(info["retry_after_ms"], 3000)
        self.assertEqual(rt._llm_retry_delay_ms(1, info), 3000)

    def test_backoff_doubles_and_caps(self) -> None:
        info = {"retry_after_ms": None}
        delays = [rt._llm_retry_delay_ms(n, info) for n in (1, 2, 3)]
        self.assertEqual(delays, [1_000, 2_000, 4_000])
        self.assertEqual(rt._llm_retry_delay_ms(10, info), rt.LLM_RETRY_MAX_DELAY_MS)


if __name__ == "__main__":
    unittest.main()
