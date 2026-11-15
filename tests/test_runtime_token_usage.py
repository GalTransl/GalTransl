"""工作台「Token 用量」的数据源：翻译请求的 usage 怎样采集、累计、交给界面。

背景：翻译主链路原先完全不读 usage，提示缓存有没有命中、思考花了多少都看不见，
省 token 的改动无从验证。这里守住三段：
- TokenUsage.parse_request_usage：各家字段换成统一口径，没报的项是 None 而不是 0；
- RuntimeRegistry.record_usage：按任务累计，命中率/思考占比只拿报了这一项的请求当分母；
- BaseTranslate._ask_chatbot：流式请求带 stream_options 要 usage，接口不认时摘掉重发且不算失败。
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock

from GalTransl import INPUT_FOLDERNAME
from GalTransl.Backend.BaseTranslate import BaseTranslate
from GalTransl.server import RUNTIME_REGISTRY, JobRegistry, RuntimeRegistry, build_handler
from GalTransl.server_runtime import encode_project_dir
from GalTransl.TokenUsage import parse_request_usage


class ParseRequestUsageTests(unittest.TestCase):
    def test_openai_shape_with_cache_and_reasoning(self):
        usage = {
            "prompt_tokens": 1000,
            "completion_tokens": 300,
            "prompt_tokens_details": {"cached_tokens": 800},
            "completion_tokens_details": {"reasoning_tokens": 120},
        }
        expected = {"input_tokens": 1000, "cached_tokens": 800, "output_tokens": 300, "reasoning_tokens": 120}
        self.assertEqual(parse_request_usage(usage), expected)
        # SDK 返回的是对象而不是字典
        as_object = SimpleNamespace(
            prompt_tokens=1000,
            completion_tokens=300,
            prompt_tokens_details=SimpleNamespace(cached_tokens=800),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=120),
        )
        self.assertEqual(parse_request_usage(as_object), expected)

    def test_deepseek_cache_fields(self):
        usage = {"prompt_tokens": 500, "prompt_cache_hit_tokens": 400, "prompt_cache_miss_tokens": 100, "completion_tokens": 50}
        self.assertEqual(
            parse_request_usage(usage),
            {"input_tokens": 500, "cached_tokens": 400, "output_tokens": 50, "reasoning_tokens": None},
        )

    def test_unreported_items_are_none_not_zero(self):
        # 中转站常常只给 prompt/completion：缓存和思考是「没报」，不能当成 0 命中
        self.assertEqual(
            parse_request_usage({"prompt_tokens": 500, "completion_tokens": 50}),
            {"input_tokens": 500, "cached_tokens": None, "output_tokens": 50, "reasoning_tokens": None},
        )

    def test_missing_usage(self):
        self.assertIsNone(parse_request_usage(None))
        self.assertIsNone(parse_request_usage({}))
        # 测试替身 / 异常接口给的不是整数
        self.assertIsNone(parse_request_usage(mock.MagicMock()))


class RegistryUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = RuntimeRegistry()
        self.project_dir = r"E:\tmp\usage_project"

    def _usage(self):
        return self.registry.get_runtime_snapshot(self.project_dir)["usage"]

    def test_empty_project_reports_unknown_cache_and_reasoning(self):
        usage = self._usage()
        self.assertEqual(usage["requests"], 0)
        self.assertEqual(usage["input_tokens"], 0)
        self.assertIsNone(usage["cached_tokens"])
        self.assertIsNone(usage["reasoning_tokens"])

    def test_accumulates_and_keeps_separate_denominators(self):
        self.registry.record_usage(
            self.project_dir,
            {"input_tokens": 1000, "cached_tokens": 800, "output_tokens": 300, "reasoning_tokens": 100},
        )
        # 这次请求没报缓存和思考：总量照加，但不进命中率/思考占比的分母
        self.registry.record_usage(
            self.project_dir,
            {"input_tokens": 500, "cached_tokens": None, "output_tokens": 50, "reasoning_tokens": None},
        )
        # 流被中途掐断或接口不支持：只算一次请求
        self.registry.record_usage(self.project_dir, None)
        self.assertEqual(
            self._usage(),
            {
                "requests": 3,
                "reported_requests": 2,
                "input_tokens": 1500,
                "cached_tokens": 800,
                "cache_reported_input_tokens": 1000,
                "output_tokens": 350,
                "reasoning_tokens": 100,
                "reasoning_reported_output_tokens": 300,
            },
        )

    def test_new_job_starts_from_zero(self):
        self.registry.record_usage(
            self.project_dir,
            {"input_tokens": 10, "cached_tokens": 0, "output_tokens": 5, "reasoning_tokens": 0},
        )
        self.registry.reset_project(self.project_dir)
        self.assertEqual(self._usage()["requests"], 0)
        self.assertIsNone(self._usage()["cached_tokens"])


class RuntimeApiUsageTests(unittest.TestCase):
    """GET /api/projects/:id/runtime 把累计用量带给界面。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = tempfile.mkdtemp(prefix="galtransl-usage-http-")
        cls.project = os.path.join(cls.root, "proj")
        os.makedirs(os.path.join(cls.project, INPUT_FOLDERNAME), exist_ok=True)
        with open(os.path.join(cls.project, "config.yaml"), "w", encoding="utf-8") as f:
            f.write("common:\n  language: ja\nplugin:\n  filePlugin: file_galtransl_json\n  textPlugins: []\n")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(JobRegistry()))
        cls.base = (
            f"http://127.0.0.1:{cls.httpd.server_address[1]}/api/projects/"
            f"{encode_project_dir(cls.project)}"
        )
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def test_runtime_response_carries_usage(self):
        RUNTIME_REGISTRY.reset_project(self.project)
        try:
            RUNTIME_REGISTRY.record_usage(
                self.project,
                {"input_tokens": 1000, "cached_tokens": 800, "output_tokens": 300, "reasoning_tokens": None},
            )
            with urllib.request.urlopen(f"{self.base}/runtime", timeout=30) as resp:
                payload = json.load(resp)
            self.assertEqual(payload["usage"]["input_tokens"], 1000)
            self.assertEqual(payload["usage"]["cached_tokens"], 800)
            self.assertEqual(payload["usage"]["output_tokens"], 300)
            self.assertIsNone(payload["usage"]["reasoning_tokens"])
        finally:
            RUNTIME_REGISTRY.reset_project(self.project)


class _Chunk:
    def __init__(self, content=None, usage=None):
        self.choices = [] if content is None else [
            SimpleNamespace(delta=SimpleNamespace(content=content), finish_reason=None)
        ]
        self.usage = usage


class _Stream:
    def __init__(self, chunks):
        self._chunks = iter(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration from None


class _BadRequest(Exception):
    status_code = 400


def _make_engine(outcomes, *, stream=True):
    """outcomes 依次是每次 create() 的结果；calls 记下每次请求的参数。"""

    class Token:
        model_name = "demo-model"
        domain = "https://example.com"

        def maskToken(self):
            return "sk-***"

    Token.stream = stream
    pending = list(outcomes)
    calls = []

    class Completions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            outcome = pending.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    engine = SimpleNamespace(
        client_list=[(client, Token())],
        tokenStrategy="random",
        api_timeout=5,
        apiErrorWait=0,
        max_api_retries=1,
        pj_config=SimpleNamespace(
            bar=SimpleNamespace(text=lambda *_: None),
            active_workers=1,
            non_interactive=True,
            stop_event=None,
            getProjectDir=lambda: "proj",
        ),
        _is_stop_requested=lambda _: False,
        _coerce_positive_int=BaseTranslate._coerce_positive_int,
        _is_transport_error=BaseTranslate._is_transport_error,
        _wait_for_global_rpm_slot=AsyncMock(return_value=None),
        _interruptible_sleep=AsyncMock(return_value=None),
        _record_request_health=lambda *args, **kwargs: None,
    )
    return engine, calls


class AskChatbotUsageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.recorded = []
        patches = (
            mock.patch("GalTransl.server.record_runtime_usage", lambda project_dir, usage: self.recorded.append((project_dir, usage))),
            mock.patch("GalTransl.server.record_runtime_error", lambda *a, **k: None),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    async def test_stream_asks_for_usage_and_records_the_final_chunk(self):
        usage = SimpleNamespace(
            prompt_tokens=1000,
            completion_tokens=40,
            prompt_tokens_details=SimpleNamespace(cached_tokens=900),
            completion_tokens_details=None,
        )
        engine, calls = _make_engine([_Stream([_Chunk("译"), _Chunk("文"), _Chunk(usage=usage)])])

        result, _token = await BaseTranslate.ask_chatbot(engine, prompt="hi")

        self.assertEqual(result, "译文")
        self.assertEqual(calls[0]["stream_options"], {"include_usage": True})
        self.assertEqual(
            self.recorded,
            [("proj", {"input_tokens": 1000, "cached_tokens": 900, "output_tokens": 40, "reasoning_tokens": None})],
        )

    async def test_non_stream_reads_usage_from_the_response(self):
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="译文"), finish_reason="stop")],
            usage={"prompt_tokens": 20, "completion_tokens": 5},
        )
        engine, calls = _make_engine([response], stream=False)

        await BaseTranslate.ask_chatbot(engine, prompt="hi")

        # stream_options 只对流式请求有意义，非流式带上会被部分接口拒收
        self.assertNotIn("stream_options", calls[0])
        self.assertEqual(self.recorded[0][1]["input_tokens"], 20)

    async def test_stream_without_usage_still_counts_the_request(self):
        engine, _calls = _make_engine([_Stream([_Chunk("译文")])])

        await BaseTranslate.ask_chatbot(engine, prompt="hi")

        self.assertEqual(self.recorded, [("proj", None)])

    async def test_provider_rejecting_stream_options_is_retried_without_it(self):
        """接口不认 stream_options 时摘掉重发：不算一次失败（max_api_retries=1 也能成功），之后也不再带。"""
        engine, calls = _make_engine([
            _BadRequest("Unrecognized request argument supplied: stream_options"),
            _Stream([_Chunk("译文")]),
            _Stream([_Chunk("再来")]),
        ])

        result, _token = await BaseTranslate.ask_chatbot(engine, prompt="hi")
        self.assertEqual(result, "译文")
        self.assertIn("stream_options", calls[0])
        self.assertNotIn("stream_options", calls[1])

        await BaseTranslate.ask_chatbot(engine, prompt="hi")
        self.assertNotIn("stream_options", calls[2])
        engine._interruptible_sleep.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
