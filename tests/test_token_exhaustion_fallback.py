import asyncio
import unittest
from contextvars import ContextVar
from types import SimpleNamespace
from unittest.mock import AsyncMock

from GalTransl.Backend.AutoTranslate import AutoTranslate, GAL_MODES
from GalTransl.Backend.BaseTranslate import BaseTranslate, TranslationParseError, TranslationRequestError
from GalTransl.COpenAI import COpenAIToken
from test_translation_multiturn import sentences


class DummyBar:
    def __call__(self, *args, **kwargs):
        pass

    def text(self, *args, **kwargs):
        pass


class TokenStrategyRotationTests(unittest.IsolatedAsyncioTestCase):
    def test_select_client_token_fallback(self) -> None:
        """测试 fallback 策略下顺延切换 token，且不永久剔除令牌。"""
        t1 = COpenAIToken("key1", "https://api1.com", "model1")
        t2 = COpenAIToken("key2", "https://api2.com", "model2")
        t3 = COpenAIToken("key3", "https://api3.com", "model3")
        c1, c2, c3 = object(), object(), object()

        dummy = SimpleNamespace(
            client_list=[(c1, t1), (c2, t2), (c3, t3)],
            tokenStrategy="fallback",
            _active_token_index=0,
        )

        # 初始调用应获取 t1
        client, token = BaseTranslate._select_client_token(dummy)
        self.assertIs(token, t1)
        self.assertIs(client, c1)

        # t1 报错，fallback 切换至下一个 token t2
        client, token = BaseTranslate._select_client_token(dummy, failed_token=t1)
        self.assertIs(token, t2)
        self.assertIs(client, c2)

        # t2 报错，fallback 切换至下一个 token t3
        client, token = BaseTranslate._select_client_token(dummy, failed_token=t2)
        self.assertIs(token, t3)
        self.assertIs(client, c3)

        # t3 报错，环形 fallback 回到 t1
        client, token = BaseTranslate._select_client_token(dummy, failed_token=t3)
        self.assertIs(token, t1)
        self.assertIs(client, c1)

        # 验证所有 token 未被剔除，isAvailable 仍为 True
        self.assertEqual(len(dummy.client_list), 3)
        self.assertTrue(t1.isAvailable)
        self.assertTrue(t2.isAvailable)
        self.assertTrue(t3.isAvailable)

    def test_select_client_token_random(self) -> None:
        """测试 random 策略下排除报错令牌随机切换另一个，且不永久剔除令牌。"""
        t1 = COpenAIToken("key1", "https://api1.com", "model1")
        t2 = COpenAIToken("key2", "https://api2.com", "model2")
        c1, c2 = object(), object()

        dummy = SimpleNamespace(
            client_list=[(c1, t1), (c2, t2)],
            tokenStrategy="random",
            _active_token_index=0,
        )

        # 当 t1 报错时，由于只有两个令牌，排除 t1 后必选 t2
        for _ in range(5):
            client, token = BaseTranslate._select_client_token(dummy, failed_token=t1)
            self.assertIs(token, t2)
            self.assertIs(client, c2)

        # 当 t2 报错时，排除 t2 后必选 t1
        for _ in range(5):
            client, token = BaseTranslate._select_client_token(dummy, failed_token=t2)
            self.assertIs(token, t1)
            self.assertIs(client, c1)

        # 验证所有 token 未被剔除，isAvailable 仍为 True
        self.assertEqual(len(dummy.client_list), 2)
        self.assertTrue(t1.isAvailable)
        self.assertTrue(t2.isAvailable)

    async def test_ask_chatbot_standalone_rotates_on_error(self) -> None:
        """测试独立引擎（非 fail_fast）在 API 报错时顺延到下一个 token 重试并成功。"""
        t1 = COpenAIToken("key1", "https://api1.com", "model1")
        t2 = COpenAIToken("key2", "https://api2.com", "model2")

        c1 = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=AsyncMock(side_effect=RuntimeError("Error code: 403 - Free quota exhausted"))
                )
            )
        )

        c2_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="translated text", tool_calls=None),
                    finish_reason="stop",
                )
            ],
            model_extra={},
        )
        c2 = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=AsyncMock(return_value=c2_response)
                )
            )
        )

        dummy = SimpleNamespace(
            client_list=[(c1, t1), (c2, t2)],
            tokenStrategy="fallback",
            _active_token_index=0,
            api_timeout=1,
            apiErrorWait=0,
            fail_fast=False,
            _coerce_positive_int=BaseTranslate._coerce_positive_int,
            pj_config=SimpleNamespace(
                bar=DummyBar(),
                active_workers=1,
                stop_event=None,
                getProjectDir=lambda: "",
            ),
            _is_stop_requested=lambda _: False,
            _wait_for_global_rpm_slot=AsyncMock(return_value=None),
            _interruptible_sleep=AsyncMock(return_value=None),
            _record_request_health=lambda *args, **kwargs: None,
        )

        resp, used_token = await BaseTranslate.ask_chatbot(
            dummy,
            prompt="test",
            stream=False,
            max_retry_count=3,
        )

        self.assertEqual(resp, "translated text")
        self.assertIs(used_token, t2)
        # 验证 t1 依然在列表中，没有被删除
        self.assertEqual(len(dummy.client_list), 2)
        self.assertTrue(t1.isAvailable)
        self.assertTrue(t2.isAvailable)

    async def test_auto_translate_switches_mode_and_token_simultaneously_fallback(self) -> None:
        """测试 auto-translate 下，重试时一边更换翻译模板，一边 fallback 切换令牌。"""
        t1 = COpenAIToken("key1", "https://api1.com", "model1")
        t2 = COpenAIToken("key2", "https://api2.com", "model2")
        c1, c2 = object(), object()

        engine = AutoTranslate.__new__(AutoTranslate)
        engine._preferred_modes = {}
        engine._preferred_tokens = {}
        engine._active_token_index = 0
        engine.tokenStrategy = "fallback"
        engine.client_list = [(c1, t1), (c2, t2)]
        engine._batch_scope = ContextVar("test_auto_token", default=None)
        engine._check_stop_requested = lambda: None
        engine._build_idx_tip = lambda current: f"1~{len(current)}"
        engine._interruptible_sleep = AsyncMock(return_value=None)
        engine.pj_config = SimpleNamespace(
            runtime_project_dir="",
            getProjectDir=lambda: "",
            bar=DummyBar(),
        )

        calls = []
        failed_once = False

        def make_delegate(mode_name):
            delegate = SimpleNamespace()
            delegate.reset_conversation = lambda filename: None
            delegate._session_scope = lambda: ContextVar(f"session_{mode_name}", default=None)
            delegate._get_chatbot_state = lambda: (False, "mock_model")

            async def translate(rows, glossary="", *, filename="", proofread=False):
                nonlocal failed_once
                token_idx = getattr(delegate, "_active_token_index", 0)
                calls.append((mode_name, token_idx))
                if not failed_once:
                    failed_once = True
                    # 第一次请求：ForGal-tool + token 0 失败（模拟额度耗尽）
                    raise TranslationRequestError("Error code: 403 - Free quota exhausted")
                # 后续请求：应使用 ForGal-markdown + token 1，成功返回
                for r in rows:
                    r.pre_dst = r.post_dst = "translated"
                return len(rows), rows

            delegate.translate = translate
            return delegate

        delegates = {mode: make_delegate(mode) for mode in GAL_MODES}
        engine._get_engine = lambda mode: delegates[mode]

        rows = sentences(4)
        for r in rows:
            r.source_file_has_name = True

        count, result = await engine.translate(rows, filename="test.json")

        self.assertEqual(count, 2)  # 第一次失败后切成一半（4 // 2 = 2）
        # 验证两次调用的模式与令牌索引：
        # 第 1 次：ForGal-tool，token_index=0
        # 第 2 次：ForGal-markdown，token_index=1（同时切换模板与令牌！）
        self.assertEqual(calls, [("ForGal-tool", 0), ("ForGal-markdown", 1)])

        # 验证令牌列表未被破坏
        self.assertEqual(len(engine.client_list), 2)
        self.assertTrue(t1.isAvailable)
        self.assertTrue(t2.isAvailable)

        # 验证下一次请求使用已切换的有效模式与有效令牌
        calls.clear()
        count2, result2 = await engine.translate(rows[:2], filename="test.json")
        self.assertEqual(count2, 2)
        self.assertEqual(calls, [("ForGal-markdown", 1)])

    async def test_auto_translate_switches_mode_and_token_simultaneously_random(self) -> None:
        """测试 auto-translate 下，重试时一边更换翻译模板，一边 random 切换令牌。"""
        t1 = COpenAIToken("key1", "https://api1.com", "model1")
        t2 = COpenAIToken("key2", "https://api2.com", "model2")
        c1, c2 = object(), object()

        engine = AutoTranslate.__new__(AutoTranslate)
        engine._preferred_modes = {}
        engine._preferred_tokens = {}
        engine._active_token_index = 0
        engine.tokenStrategy = "random"
        engine.client_list = [(c1, t1), (c2, t2)]
        engine._batch_scope = ContextVar("test_auto_token_rand", default=None)
        engine._check_stop_requested = lambda: None
        engine._build_idx_tip = lambda current: f"1~{len(current)}"
        engine._interruptible_sleep = AsyncMock(return_value=None)
        engine.pj_config = SimpleNamespace(
            runtime_project_dir="",
            getProjectDir=lambda: "",
            bar=DummyBar(),
        )

        calls = []

        def make_delegate(mode_name):
            delegate = SimpleNamespace()
            delegate.reset_conversation = lambda filename: None
            delegate._session_scope = lambda: ContextVar(f"session_{mode_name}", default=None)
            delegate._get_chatbot_state = lambda: (False, "mock_model")

            async def translate(rows, glossary="", *, filename="", proofread=False):
                token_idx = getattr(delegate, "_active_token_index", 0)
                calls.append((mode_name, token_idx))
                if len(calls) == 1:
                    raise TranslationParseError("parse failed")
                for r in rows:
                    r.pre_dst = r.post_dst = "translated"
                return len(rows), rows

            delegate.translate = translate
            return delegate

        delegates = {mode: make_delegate(mode) for mode in GAL_MODES}
        engine._get_engine = lambda mode: delegates[mode]

        rows = sentences(4)
        for r in rows:
            r.source_file_has_name = True

        count, result = await engine.translate(rows, filename="test.json")

        self.assertEqual(count, 2)
        # 验证重试时同时更换翻译模板和令牌（且令牌切换为另一个）
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], "ForGal-tool")
        self.assertEqual(calls[1][0], "ForGal-markdown")
        self.assertNotEqual(calls[0][1], calls[1][1])
        self.assertIn(calls[0][1], (0, 1))
        self.assertIn(calls[1][1], (0, 1))
        self.assertEqual(len(engine.client_list), 2)
        self.assertTrue(t1.isAvailable)
        self.assertTrue(t2.isAvailable)
