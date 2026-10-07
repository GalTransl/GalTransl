import asyncio
import unittest
from contextvars import ContextVar
from unittest.mock import AsyncMock

import httpx

from GalTransl.Backend.AutoTranslate import AutoTranslate, GAL_MODES
from GalTransl.Backend.BaseTranslate import TranslationParseError
from GalTransl.Loader import load_transList
from GalTransl.Service import JobCancelledError
from test_translation_multiturn import translator, sentences, ENGINES, Model


def auto_engine(actions):
    engine = translator(AutoTranslate, '')
    engine._preferred_modes = {}
    engine._batch_scope = ContextVar('test_auto', default=None)
    engine.calls = []
    engines = {}
    for mode in (*GAL_MODES, 'ForNovel-tool', 'ForNovel'):
        delegate = translator(ENGINES[0][0], ENGINES[0][1])
        async def translate(rows, glossary='', *, filename='', proofread=False, mode=mode):
            engine.calls.append((mode, len(rows)))
            action = actions.pop(0) if actions else None
            rows[0].pre_dst = 'temporary'
            if isinstance(action, BaseException):
                raise action
            for row in rows:
                row.pre_dst = row.post_dst = 'translated'
            return len(rows), rows
        delegate.translate = translate
        engines[mode] = delegate
    engine._get_engine = engines.__getitem__
    return engine


def named(count):
    rows = sentences(count)
    for row in rows:
        row.source_file_has_name = True
    return rows


class AutoTranslateTests(unittest.IsolatedAsyncioTestCase):
    async def test_switch_split_sticky_and_file_isolation(self):
        engine = auto_engine([httpx.ReadError('network'), TranslationParseError('parse')])
        rows = named(16)
        count, result = await engine.translate(rows, filename='a')
        self.assertEqual(engine.calls, [('ForGal-tool', 16), ('ForGal-markdown', 8), ('ForGal-json', 4)])
        self.assertEqual(count, 4)
        self.assertIs(result[0], rows[0])
        self.assertEqual(rows[4].pre_dst, '')
        await engine.translate(rows[4:], filename='a')
        await engine.translate(named(1), filename='b')
        await engine.translate(sentences(1), filename='novel')
        self.assertEqual(engine.calls[-3:], [('ForGal-json', 12), ('ForGal-tool', 1), ('ForNovel-tool', 1)])

    async def test_five_total_attempts_and_failure(self):
        engine = auto_engine([TranslationParseError('bad')] * 5)
        rows = named(64)
        count, result = await engine.translate(rows, filename='a')
        self.assertEqual(engine.calls, list(zip(['ForGal-tool', 'ForGal-markdown', 'ForGal-json', 'ForGal-tool', 'ForGal-markdown'], [64, 32, 16, 8, 4])))
        self.assertEqual(engine._interruptible_sleep.await_count, 4)
        self.assertEqual(count, 4)
        self.assertTrue(all(r.pre_dst.startswith('(Failed)') for r in result))
        self.assertEqual(rows[4].pre_dst, '')

    async def test_one_sentence_never_shrinks_to_empty(self):
        engine = auto_engine([TranslationParseError('bad')] * 5)
        count, _ = await engine.translate(sentences(1), filename='novel')
        self.assertEqual(count, 1)
        self.assertEqual(engine.calls, [(mode, 1) for mode in ('ForNovel-tool', 'ForNovel', 'ForNovel-tool', 'ForNovel', 'ForNovel-tool')])

    async def test_cancellation_propagates_without_retry_or_mutation(self):
        for error in (JobCancelledError(), asyncio.CancelledError()):
            engine = auto_engine([error])
            rows = named(2)
            with self.assertRaises(type(error)):
                await engine.translate(rows, filename='a')
            self.assertEqual(len(engine.calls), 1)
            self.assertEqual(engine._preferred_modes, {})
            self.assertEqual(rows[0].pre_dst, '')

    async def test_real_parsers_fail_fast_on_partial_and_invalid_output(self):
        for kind, prompt in ENGINES:
            for action in ('bad', 'partial'):
                engine = translator(kind, prompt)
                engine.fail_fast = True
                model = Model(engine, actions=[action])
                engine.ask_chatbot = model
                with self.assertRaises(TranslationParseError):
                    await engine.translate(sentences(3), filename='a')
                self.assertEqual(len(model.requests), 1)

    async def test_batch_finishes_without_hidden_retry_after_fifth_failure(self):
        engine = auto_engine([TranslationParseError('bad')] * 5)
        result = await engine.batch_translate('a', '', named(1), 1)
        self.assertEqual(len(engine.calls), 5)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].pre_dst.startswith('(Failed)'))

    def test_presence_of_empty_name_classifies_entire_file(self):
        for field in ('name', 'names'):
            rows, _ = load_transList([{'message': 'narration'}, {'message': 'dialogue', field: ''}])
            self.assertTrue(all(row.source_file_has_name for row in rows))
        rows, _ = load_transList([{'message': 'novel'}])
        self.assertFalse(rows[0].source_file_has_name)

    async def test_api_fail_fast_disables_internal_retry(self):
        from GalTransl.Backend.BaseTranslate import BaseTranslate, TranslationRequestError
        from test_translate_stream_progress import _make_engine
        engine = _make_engine([RuntimeError('broken stream')])
        engine.fail_fast = True
        create = AsyncMock(wraps=engine.client_list[0][0].chat.completions.create)
        engine.client_list[0][0].chat.completions.create = create
        with self.assertRaises(TranslationRequestError):
            await BaseTranslate.ask_chatbot(engine, messages=[])
        self.assertEqual(create.await_count, 1)
        engine._interruptible_sleep.assert_not_awaited()

    async def test_tool_invalid_patch_fails_without_internal_retry(self):
        from GalTransl.Backend.ForGalToolTranslate import ForGalToolTranslate
        from GalTransl.Backend.Prompts import FORGAL_TOOL_TRANS_PROMPT
        from test_forgal_tool import function_call
        from types import SimpleNamespace
        engine = translator(ForGalToolTranslate, FORGAL_TOOL_TRANS_PROMPT)
        engine.fail_fast = True
        async def reply(**kwargs):
            kwargs['tool_response_holder'].update(tool_calls=[function_call('write_translation_result', 'invalid')])
            return '', SimpleNamespace(model_name='test')
        engine.ask_chatbot = AsyncMock(side_effect=reply)
        with self.assertRaises(TranslationParseError):
            await engine.translate(sentences(2), filename='a')
        self.assertEqual(engine.ask_chatbot.await_count, 1)

    def test_special_model_detection_is_case_insensitive_and_precedes_file_type(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from GalTransl.Backend.BaseTranslate import BaseTranslate
        for model, expected in [('org/SAKURA-1.0', 'sakura-v1.0'), ('GalTransl-7B', 'galtransl-v3'), ('generic', None)]:
            config = SimpleNamespace(getBackendConfigSection=lambda section: {'rewriteModelName': model})
            with patch.object(BaseTranslate, '__init__', return_value=None):
                engine = AutoTranslate(config, 'auto-translate', None, None)
            self.assertEqual(engine._special_mode, expected)
        config = SimpleNamespace(getBackendConfigSection=lambda section: {})
        pool = SimpleNamespace(get_available_token=lambda: [SimpleNamespace(model_name='GALTRANSL')])
        with patch.object(BaseTranslate, '__init__', return_value=None):
            engine = AutoTranslate(config, 'auto-translate', None, pool)
        self.assertEqual(engine._special_mode, 'galtransl-v3')

    async def test_special_templates_split_without_switching_and_parse_real_responses(self):
        from GalTransl.Backend.AutoTranslate import _AutoSakuraTranslate
        from GalTransl.Backend.Prompts import Sakura_TRANS_PROMPT010, GalTransl_TRANS_PROMPT_V3
        from types import SimpleNamespace
        for mode, prompt in [('sakura-v1.0', Sakura_TRANS_PROMPT010), ('galtransl-v3', GalTransl_TRANS_PROMPT_V3)]:
            engine = auto_engine([])
            engine._special_mode = mode
            delegate = translator(_AutoSakuraTranslate, prompt)
            delegate.eng_type = mode
            delegate.model_name = mode
            delegate.temperature, delegate.frequency_penalty, delegate.top_p = .1, .1, .8
            delegate._current_temp_type = 'precise'
            delegate.opencc = SimpleNamespace(convert=lambda text: text)
            delegate.fail_fast = True
            calls = []
            async def reply(**kwargs):
                calls.append(kwargs)
                return 'one line', SimpleNamespace(model_name=mode)
            delegate.ask_chatbot = reply
            modes = []
            def get_engine(selected):
                modes.append(selected)
                return delegate
            engine._get_engine = get_engine
            count, rows = await engine.translate(sentences(32), filename='novel')
            self.assertEqual(modes, [mode] * 5)
            self.assertEqual(len(calls), 5)
            self.assertEqual(count, 2)
            self.assertTrue(all(row.pre_dst.startswith('(Failed)') for row in rows))
            count, rows = await engine.translate(named(1), filename='gal')
            self.assertEqual(count, 1)
            self.assertEqual(rows[0].pre_dst, 'one line')
            self.assertEqual(modes[-1], mode)

    async def test_reports_each_auto_failure_to_recent_errors(self):
        from unittest.mock import patch
        from GalTransl.Backend.BaseTranslate import TranslationRequestError
        engine = auto_engine([TranslationRequestError('connection lost')] + [TranslationParseError('bad output')] * 4)
        engine.pj_config.runtime_project_dir = 'test-project'
        with patch('GalTransl.server.record_runtime_error') as report:
            await engine.translate(named(64), filename='chapter.json')
        self.assertEqual(report.call_count, 5)
        events = [call.kwargs for call in report.call_args_list]
        self.assertEqual([event['kind'] for event in events], ['api'] + ['parse'] * 4)
        self.assertEqual([event['retry_count'] for event in events], list(range(5)))
        self.assertEqual([event['level'] for event in events], ['warning'] * 4 + ['error'])
        self.assertEqual([event['index_range'] for event in events], ['1~64', '1~32', '1~16', '1~8', '1~4'])
        self.assertTrue(all(event['filename'] == 'chapter.json' for event in events))
        self.assertTrue(all(call.args == ('test-project',) for call in report.call_args_list))
        self.assertIn('ForGal-tool', events[0]['message'])
        self.assertIn('切换 ForGal-markdown', events[0]['message'])
        self.assertIn('标记翻译失败', events[-1]['message'])

    async def test_error_reporting_failure_does_not_interrupt_retry(self):
        from unittest.mock import patch
        engine = auto_engine([TranslationParseError('bad')])
        with patch('GalTransl.server.record_runtime_error', side_effect=RuntimeError('unavailable')):
            count, rows = await engine.translate(named(4), filename='a')
        self.assertEqual(count, 2)
        self.assertEqual(rows[0].pre_dst, 'translated')
        self.assertEqual(len(engine.calls), 2)
