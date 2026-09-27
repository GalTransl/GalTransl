"""流式请求里的「文件进度」小灯上报。

后端在 ask_chatbot 的流式循环里攒字数、每 0.2s 报一次：
- 只吐思考内容（reasoning_content）→ phase=thinking（界面蓝灯）
- 开始出正文 → phase=writing（绿灯）
这里用假 client + 假流验证这条链路，顺便锁住节流（逐 chunk 报会把后端和界面都拖累）。
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from GalTransl.Backend.BaseTranslate import BaseTranslate


class _DeltaChunk:
    def __init__(self, content=None, reasoning=None):
        self.choices = [
            SimpleNamespace(
                delta=SimpleNamespace(content=content, reasoning_content=reasoning)
            )
        ]


class _StreamResponse:
    """按脚本吐 chunk；每段前面可选 sleep，用来跨过 0.2s 的上报节流窗口。"""

    def __init__(self, script):
        self._script = list(script)
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._script):
            raise StopAsyncIteration
        sleep_seconds, chunk = self._script[self._index]
        self._index += 1
        if sleep_seconds:
            await asyncio.sleep(sleep_seconds)
        return chunk


def _make_engine(script, reports):
    class Token:
        model_name = "demo-model"
        domain = "https://example.com"
        stream = True

        def maskToken(self):
            return "sk-***"

    class Completions:
        async def create(self, **kwargs):
            return _StreamResponse(script)

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    return SimpleNamespace(
        client_list=[(client, Token())],
        tokenStrategy="random",
        api_timeout=5,
        apiErrorWait=0,
        max_api_retries=2,
        pj_config=SimpleNamespace(
            bar=SimpleNamespace(text=lambda *_: None),
            active_workers=1,
            non_interactive=True,
            stop_event=None,
            getProjectDir=lambda: "",
        ),
        _is_stop_requested=lambda _: False,
        # ask_chatbot 里几个直接取自身方法的调用点：静态方法直接挂上就行
        _coerce_positive_int=BaseTranslate._coerce_positive_int,
        _is_transport_error=BaseTranslate._is_transport_error,
        _wait_for_global_rpm_slot=AsyncMock(return_value=None),
        _interruptible_sleep=AsyncMock(return_value=None),
        _record_request_health=lambda *args, **kwargs: None,
        # 真实的那个会去调服务器那里的注册表，这里只关心「报了什么」
        _report_stream_progress=lambda filename, phase, chars: reports.append(
            (filename, phase, chars)
        ),
    )


class StreamProgressReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_thinking_then_writing_and_throttled(self):
        reports = []
        engine = _make_engine(
            [
                (0, _DeltaChunk(reasoning="先想想")),          # 只有思考 → thinking
                (0.25, _DeltaChunk(content="正文一")),         # 跨过节流窗口 → writing
                (0, _DeltaChunk(content="正文二")),            # 节流窗口内 → 不报
            ],
            reports,
        )

        result, _token = await BaseTranslate.ask_chatbot(
            engine, prompt="hi", max_retry_count=2, progress_file="sc_0.txt.json"
        )

        self.assertEqual(result, "正文一正文二")
        self.assertEqual(
            reports,
            [
                ("sc_0.txt.json", "thinking", 3),  # 「先想想」
                ("sc_0.txt.json", "writing", 3),   # 「正文一」
            ],
        )

    async def test_inline_think_markup_still_counts_as_writing(self):
        # 有的接口把思考直接写在正文里（<think>…</think>，引擎侧最后剥掉）。小灯只关心
        # 「在不在出字」：delta.content 一有内容就是 writing，不再解析标签——标签会被流式
        # 切成两半（`</thi` + `nk>`），靠它判阶段不可靠
        reports = []
        engine = _make_engine(
            [
                (0, _DeltaChunk(content="<think>先想想")),
                (0.25, _DeltaChunk(content="再想想</thi")),
                (0.25, _DeltaChunk(content="nk>")),
                (0.25, _DeltaChunk(content="译文")),
            ],
            reports,
        )

        await BaseTranslate.ask_chatbot(
            engine, prompt="hi", max_retry_count=2, progress_file="a.json"
        )

        # 全程 writing：正文 chunk 一来阶段就定了，标签切开也不影响
        self.assertEqual(
            [phase for _file, phase, _chars in reports],
            ["writing", "writing", "writing", "writing"],
        )

    async def test_no_reports_without_progress_file(self):
        reports = []
        engine = _make_engine(
            [
                (0, _DeltaChunk(reasoning="先想想")),
                (0.25, _DeltaChunk(content="正文")),
            ],
            reports,
        )

        # 没传 progress_file 的引擎（如 Sakura）：一个字都不该报
        await BaseTranslate.ask_chatbot(engine, prompt="hi", max_retry_count=2)

        self.assertEqual(reports, [])


if __name__ == "__main__":
    unittest.main()
