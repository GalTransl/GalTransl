"""ask_chatbot 往「文件进度」小灯报的状态。

一次 ask_chatbot 进门登记（请求中）、每次尝试发出前回到请求中、流式里按阶段报字数、失败退避时
报重试中、出门注销（BaseTranslate._FileRequestProgress）。这里用假 client + 假流把整条链路跑一遍，
记下报给 server 的每一笔：
- 只吐思考内容（reasoning_content）→ thinking（界面蓝灯）；开始出正文 → writing（绿灯）；
- 思考写在正文开头的 <think>…</think> 也算 thinking，标签被流式切成两半也认得出；
- 换阶段立刻报，同一阶段内 0.2s 攒一次（逐 chunk 报会把后端和界面都拖累）；
- 不管成功、重试到上限还是取消，最后都要注销——不能留一盏一直亮着的灯。
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock

from GalTransl.Backend.BaseTranslate import BaseTranslate, _InlineThinkDetector
from GalTransl.Service import JobCancelledError


class _DeltaChunk:
    def __init__(self, content=None, reasoning=None):
        self.choices = [
            SimpleNamespace(
                delta=SimpleNamespace(content=content, reasoning_content=reasoning)
            )
        ]


class _StreamResponse:
    """按脚本吐 chunk。脚本每项是 (先睡几秒, chunk)；chunk 是可调用对象时先调用它（用来模拟中途停止）。"""

    def __init__(self, script):
        self._script = list(script)
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        while self._index < len(self._script):
            sleep_seconds, chunk = self._script[self._index]
            self._index += 1
            if sleep_seconds:
                await asyncio.sleep(sleep_seconds)
            if callable(chunk):
                chunk()
                continue
            return chunk
        raise StopAsyncIteration


class _Recorder:
    """替掉 server 那三个函数，按顺序记下报了什么。"""

    def __init__(self):
        self.events = []

    def begin(self, project_dir, *, filename):
        self.events.append(("begin", filename))
        return 7

    def note(self, request_id, *, phase, chars=0):
        self.events.append((phase, chars))

    def end(self, request_id):
        self.events.append(("end",))

    def patched(self):
        return (
            mock.patch("GalTransl.server.begin_runtime_request", self.begin),
            mock.patch("GalTransl.server.note_runtime_request", self.note),
            mock.patch("GalTransl.server.end_runtime_request", self.end),
            # 失败路径会去记一条运行时错误，这里不关心
            mock.patch("GalTransl.server.record_runtime_error", lambda *a, **k: None),
        )


def _make_engine(responses, *, stream=True, stop=None):
    """responses 依次是每次 create() 的结果：_StreamResponse / 非流式响应 / 要抛的异常。"""

    class Token:
        model_name = "demo-model"
        domain = "https://example.com"

        def maskToken(self):
            return "sk-***"

    Token.stream = stream
    pending = list(responses)

    class Completions:
        async def create(self, **kwargs):
            outcome = pending.pop(0) if len(pending) > 1 else pending[0]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    stop = stop if stop is not None else {"on": False}
    return SimpleNamespace(
        client_list=[(client, Token())],
        tokenStrategy="random",
        api_timeout=5,
        apiErrorWait=0,
        max_api_retries=3,
        pj_config=SimpleNamespace(
            bar=SimpleNamespace(text=lambda *_: None),
            active_workers=1,
            non_interactive=True,
            stop_event=None,
            getProjectDir=lambda: "",
        ),
        _is_stop_requested=lambda _: stop["on"],
        # ask_chatbot 里几个直接取自身方法的调用点：静态方法直接挂上就行
        _coerce_positive_int=BaseTranslate._coerce_positive_int,
        _is_transport_error=BaseTranslate._is_transport_error,
        _wait_for_global_rpm_slot=AsyncMock(return_value=None),
        _interruptible_sleep=AsyncMock(return_value=None),
        _record_request_health=lambda *args, **kwargs: None,
    )


class RequestProgressReportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.recorder = _Recorder()
        for patch in self.recorder.patched():
            patch.start()
            self.addCleanup(patch.stop)

    async def test_thinking_then_writing_then_end(self):
        engine = _make_engine([
            _StreamResponse([
                (0, _DeltaChunk(reasoning="先想想")),  # 只有思考 → thinking，立刻报
                (0, _DeltaChunk(content="正文一")),    # 换阶段 → writing，立刻报
                (0, _DeltaChunk(content="正文二")),    # 同一阶段、0.2s 内 → 攒着
                (0.25, _DeltaChunk(content="三")),     # 跨过 0.2s → 连同攒着的一起报
            ])
        ])

        result, _token = await BaseTranslate.ask_chatbot(
            engine, prompt="hi", progress_file="sc_0.txt.json"
        )

        self.assertEqual(result, "正文一正文二三")
        self.assertEqual(
            self.recorder.events,
            [
                ("begin", "sc_0.txt.json"),
                ("thinking", 3),
                ("writing", 3),
                ("writing", 4),
                ("end",),
            ],
        )

    async def test_inline_think_markup_counts_as_thinking_even_when_split(self):
        # 思考写在正文开头（<think>…</think>，引擎最后剥掉）：标签被流式切成 `</thi` + `nk>` 也要认得出
        engine = _make_engine([
            _StreamResponse([
                (0, _DeltaChunk(content="<think>先想想")),
                (0, _DeltaChunk(content="再想想</thi")),
                (0, _DeltaChunk(content="nk>")),
                (0, _DeltaChunk(content="\n译文")),
            ])
        ])

        await BaseTranslate.ask_chatbot(engine, prompt="hi", progress_file="a.json")

        self.assertEqual(
            self.recorder.events,
            [
                ("begin", "a.json"),
                ("thinking", 10),  # 「<think>先想想」
                ("thinking", 11),  # 换阶段前把攒着的「再想想</thi」「nk>」记到思考名下
                ("writing", 3),    # 「\n译文」
                ("end",),
            ],
        )

    async def test_leading_whitespace_does_not_turn_the_lamp_green(self):
        # 有的接口在思考前先吐一个空白正文：不能因此判成「翻译中」
        engine = _make_engine([
            _StreamResponse([
                (0, _DeltaChunk(content="\n")),
                (0, _DeltaChunk(reasoning="想")),
                (0, _DeltaChunk(content="译文")),
            ])
        ])

        await BaseTranslate.ask_chatbot(engine, prompt="hi", progress_file="a.json")

        self.assertEqual(
            self.recorder.events,
            [("begin", "a.json"), ("thinking", 1), ("writing", 2), ("end",)],
        )

    async def test_backoff_reports_retrying_then_waiting_again(self):
        engine = _make_engine([
            TimeoutError("request timed out"),
            _StreamResponse([(0, _DeltaChunk(content="译文"))]),
        ])

        result, _token = await BaseTranslate.ask_chatbot(engine, prompt="hi", progress_file="a.json")

        self.assertEqual(result, "译文")
        self.assertEqual(
            self.recorder.events,
            [("begin", "a.json"), ("retrying", 0), ("waiting", 0), ("writing", 2), ("end",)],
        )

    async def test_request_is_ended_when_retries_run_out(self):
        engine = _make_engine([TimeoutError("request timed out")])

        with self.assertRaises(RuntimeError):
            await BaseTranslate.ask_chatbot(
                engine, prompt="hi", progress_file="a.json", max_retry_count=2
            )

        self.assertEqual(
            self.recorder.events,
            [("begin", "a.json"), ("retrying", 0), ("waiting", 0), ("end",)],
        )

    async def test_request_is_ended_when_the_job_is_stopped_mid_stream(self):
        stop = {"on": False}
        engine = _make_engine(
            [
                _StreamResponse([
                    (0, _DeltaChunk(content="译文")),
                    (0, lambda: stop.update(on=True)),  # 用户点了停止
                    (0, _DeltaChunk(content="不该再收")),
                ])
            ],
            stop=stop,
        )

        with self.assertRaises(JobCancelledError):
            await BaseTranslate.ask_chatbot(engine, prompt="hi", progress_file="a.json")

        self.assertEqual(self.recorder.events, [("begin", "a.json"), ("writing", 2), ("end",)])

    async def test_non_stream_request_is_waiting_until_it_returns(self):
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="译文"))],
            model_extra={},
        )
        engine = _make_engine([response], stream=False)

        result, _token = await BaseTranslate.ask_chatbot(engine, prompt="hi", progress_file="a.json")

        # 非流式：登记即「请求中」，拿到结果就注销，中间没有思考/正文阶段
        self.assertEqual(result, "译文")
        self.assertEqual(self.recorder.events, [("begin", "a.json"), ("end",)])

    async def test_nothing_is_reported_without_progress_file(self):
        engine = _make_engine([
            _StreamResponse([(0, _DeltaChunk(reasoning="先想想")), (0, _DeltaChunk(content="正文"))])
        ])

        await BaseTranslate.ask_chatbot(engine, prompt="hi")

        self.assertEqual(self.recorder.events, [])


class InlineThinkDetectorTests(unittest.TestCase):
    def _feed(self, *pieces):
        detector = _InlineThinkDetector()
        return [detector.feed(piece) for piece in pieces]

    def test_plain_output_is_writing(self):
        self.assertEqual(self._feed('abc|{"id": 1', "}\n"), ["writing", "writing"])

    def test_leading_whitespace_is_undecided(self):
        self.assertEqual(self._feed("\n", "  ", "译"), [None, None, "writing"])

    def test_split_open_tag(self):
        self.assertEqual(self._feed("<th", "ink>嗯", "</think>", "译"), ["thinking", "thinking", "thinking", "writing"])

    def test_not_a_think_tag_after_all(self):
        self.assertEqual(self._feed("<", "b>粗体"), ["thinking", "writing"])

    def test_close_tag_and_answer_in_the_same_piece(self):
        self.assertEqual(self._feed("<think>嗯</think>译文"), ["writing"])

    def test_empty_think_block(self):
        # 关了思考的 Qwen3 一类：先吐一个空的 <think></think>
        self.assertEqual(
            self._feed("<think>\n\n</think>\n\n", "译文"), ["thinking", "writing"]
        )

    def test_whitespace_after_answer_started_keeps_phase(self):
        self.assertEqual(self._feed("译文", "\n"), ["writing", None])


if __name__ == "__main__":
    unittest.main()
