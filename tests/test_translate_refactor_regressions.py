import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from GalTransl.Backend.BaseTranslate import BaseTranslate
from GalTransl.Backend.ForGalJsonTranslate import ForGalJsonTranslate
from GalTransl.Backend.Prompts import FORGAL_JSON_TRANS_PROMPT
from GalTransl.CSentense import CSentense


class DummyBar:
    def __call__(self, *args, **kwargs):
        return None

    def text(self, *args, **kwargs):
        return None


class TranslateRefactorRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_ask_chatbot_honours_max_retry_count(self) -> None:
        class DummyToken:
            model_name = "demo-model"
            domain = "https://example.com"
            stream = False

            def maskToken(self) -> str:
                return "sk-***"

        class DummyCompletions:
            def __init__(self) -> None:
                self.calls = 0

            async def create(self, **kwargs):
                self.calls += 1
                raise TimeoutError("request timed out")

        dummy_completions = DummyCompletions()
        dummy_client = SimpleNamespace(chat=SimpleNamespace(completions=dummy_completions))
        dummy = SimpleNamespace(
            client_list=[(dummy_client, DummyToken())],
            tokenStrategy="random",
            api_timeout=1,
            apiErrorWait=0,
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

        with self.assertRaises(RuntimeError):
            await BaseTranslate.ask_chatbot(
                dummy,
                prompt="hello",
                system="system",
                max_retry_count=2,
            )

        self.assertEqual(dummy_completions.calls, 2)

    async def test_ask_chatbot_non_stream_none_content_retries_and_fails(self) -> None:
        class DummyToken:
            model_name = "demo-model"
            domain = "https://example.com"
            stream = False

            def maskToken(self) -> str:
                return "sk-***"

        class DummyCompletions:
            def __init__(self) -> None:
                self.calls = 0

            async def create(self, **kwargs):
                self.calls += 1
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=None))],
                    model_extra={},
                )

        dummy_completions = DummyCompletions()
        dummy_client = SimpleNamespace(chat=SimpleNamespace(completions=dummy_completions))
        dummy = SimpleNamespace(
            client_list=[(dummy_client, DummyToken())],
            tokenStrategy="random",
            api_timeout=1,
            apiErrorWait=0,
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

        with self.assertRaises(RuntimeError):
            await BaseTranslate.ask_chatbot(
                dummy,
                prompt="hello",
                system="system",
                max_retry_count=2,
            )

        self.assertEqual(dummy_completions.calls, 2)

    async def test_ask_chatbot_aborts_stream_immediately_when_callback_returns_false(self) -> None:
        class DummyToken:
            model_name = "demo-model"
            domain = "https://example.com"
            stream = True

            def maskToken(self) -> str:
                return "sk-***"

        class DummyStreamResponse:
            def __init__(self) -> None:
                self._chunks = [
                    SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(
                                    content='aaa|{"id": 1, "dst": "hello"}\n',
                                    reasoning_content=None,
                                )
                            )
                        ]
                    ),
                    SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(
                                    content='aaa|{"id": 2, "dst": "should_not_be_consumed"}\n',
                                    reasoning_content=None,
                                )
                            )
                        ]
                    ),
                ]
                self.next_calls = 0
                self.aclose_calls = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.next_calls >= len(self._chunks):
                    raise StopAsyncIteration
                chunk = self._chunks[self.next_calls]
                self.next_calls += 1
                return chunk

            async def aclose(self):
                self.aclose_calls += 1

        class DummyCompletions:
            def __init__(self, response: DummyStreamResponse) -> None:
                self.response = response
                self.calls = 0

            async def create(self, **kwargs):
                self.calls += 1
                return self.response

        stream_response = DummyStreamResponse()
        dummy_completions = DummyCompletions(stream_response)
        dummy_client = SimpleNamespace(chat=SimpleNamespace(completions=dummy_completions))
        dummy = SimpleNamespace(
            client_list=[(dummy_client, DummyToken())],
            tokenStrategy="random",
            api_timeout=1,
            apiErrorWait=0,
            pj_config=SimpleNamespace(
                bar=DummyBar(),
                active_workers=1,
                stop_event=None,
                non_interactive=True,
                getProjectDir=lambda: "",
            ),
            _is_stop_requested=lambda _: False,
            _wait_for_global_rpm_slot=AsyncMock(return_value=None),
            _interruptible_sleep=AsyncMock(return_value=None),
            _record_request_health=lambda *args, **kwargs: None,
        )

        callback_calls = []

        def stream_line_callback(lines, is_final_chunk):
            callback_calls.append((list(lines), is_final_chunk))
            return False

        result, _ = await BaseTranslate.ask_chatbot(
            dummy,
            messages=[{"role": "user", "content": "demo"}],
            stream_line_callback=stream_line_callback,
        )

        self.assertEqual(dummy_completions.calls, 1)
        self.assertEqual(stream_response.next_calls, 1)
        self.assertEqual(stream_response.aclose_calls, 1)
        self.assertEqual(callback_calls, [([r'aaa|{"id": 1, "dst": "hello"}'], False)])
        self.assertEqual(result, 'aaa|{"id": 1, "dst": "hello"}\n')

    async def test_forgal_json_streaming_uses_runtime_model_name(self) -> None:
        translator = ForGalJsonTranslate.__new__(ForGalJsonTranslate)
        translator.pj_config = SimpleNamespace(active_workers=0, translation_guideline="")
        translator.enhance_jailbreak = False
        translator.system_prompt = "system"
        translator.trans_prompt = "[Input]\n[Glossary]\n[history_result]"
        translator.contextNum = 0
        translator.last_translations = {}
        translator.target_lang = "English"
        translator.source_lang = "Japanese"
        translator.smartRetry = False
        translator._SIGCHARS = "a"
        translator._last_chatbot_was_stream = False
        translator._last_chatbot_model_name = ""
        translator.restore_context = lambda trans_list, num_pre_request, filename="": None
        translator._check_stop_requested = lambda: None
        translator._record_runtime_success = lambda filename, trans: None

        async def fake_ask_chatbot(**kwargs):
            translator._last_chatbot_model_name = "stream-model"
            translator._last_chatbot_was_stream = True
            kwargs["stream_line_callback"]([r'aaa|{"id": 1, "dst": "hello"}'], True)
            return r'aaa|{"id": 1, "dst": "hello"}', SimpleNamespace(model_name="fallback-model")

        translator.ask_chatbot = fake_ask_chatbot

        trans_list = [CSentense("こんにちは", index=1)]
        success_count, result_trans_list = await translator.translate(
            trans_list,
            filename="demo.json",
        )

        self.assertEqual(success_count, 1)
        self.assertEqual(len(result_trans_list), 1)
        self.assertEqual(result_trans_list[0].pre_dst, "hello")
        self.assertEqual(result_trans_list[0].trans_by, "stream-model")

    async def test_batch_translate_common_skips_duplicate_runtime_success_record(self) -> None:
        recorded: list[tuple[str, int]] = []

        class DummyTranslator:
            skipH = False
            save_steps = 999

            def __init__(self) -> None:
                self.pj_config = SimpleNamespace(bar=DummyBar(), stop_event=None)

            def _check_stop_requested(self) -> None:
                return None

            def _record_runtime_success(self, filename: str, trans: CSentense) -> None:
                recorded.append((filename, trans.index))

            async def translate(self, trans_list_split, dic_prompt, proofread=False, filename=""):
                return len(trans_list_split), trans_list_split

        trans = CSentense("line-1", index=1)
        trans.pre_dst = "译文"
        trans.post_dst = "译文"
        trans.trans_by = "stream-model"
        trans._runtime_success_recorded = True

        translator = DummyTranslator()
        result = await BaseTranslate._batch_translate_common(
            translator,
            filename="demo.json",
            cache_file_path="demo_cache.json",
            translist_unhit=[trans],
            num_pre_request=1,
            proofread=False,
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(recorded, [])

    def test_forgal_json_prompt_does_not_contain_literal_output_recipe_backslash_n(self) -> None:
        self.assertNotIn(
            '### Output Recipe = { "id": int, (optional)"name": string, "dst": string }\\\\n',
            FORGAL_JSON_TRANS_PROMPT,
        )


if __name__ == "__main__":
    unittest.main()
