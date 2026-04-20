import unittest
from types import SimpleNamespace

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
        translator._last_chatbot_was_stream = False
        translator._last_chatbot_model_name = ""
        translator.restore_context = lambda trans_list, num_pre_request, filename="": None
        translator._check_stop_requested = lambda: None
        translator._record_runtime_success = lambda filename, trans: None

        async def fake_ask_chatbot(**kwargs):
            translator._last_chatbot_model_name = "stream-model"
            translator._last_chatbot_was_stream = True
            kwargs["stream_line_callback"]([r'{"id": 1, "dst": "hello"}'], True)
            return r'{"id": 1, "dst": "hello"}', SimpleNamespace(model_name="fallback-model")

        translator.ask_chatbot = fake_ask_chatbot

        trans_list = [CSentense("こんにちは", index=1)]
        success_count, result_trans_list = await translator.translate(
            trans_list,
            filename="demo.json",
        )

        self.assertEqual(success_count, 1)
        self.assertEqual(len(result_trans_list), 1)
        self.assertEqual(result_trans_list[0].pre_zh, "hello")
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
        trans.pre_zh = "译文"
        trans.post_zh = "译文"
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
