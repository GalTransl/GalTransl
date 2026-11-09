import asyncio
import unittest
from contextvars import ContextVar
from unittest.mock import AsyncMock

import httpx

from GalTransl.Backend.AutoTranslate import AutoTranslate, GAL_MODES, classify_failure
from GalTransl.Backend.BaseTranslate import TranslationParseError, TranslationRequestError
from GalTransl.Backend.MultiTurnTranslate import JAILBREAK_OVERRIDE
from GalTransl.Loader import load_transList
from GalTransl.Service import JobCancelledError
from test_translation_multiturn import translator, sentences, ENGINES, Model


def auto_engine(actions, settings=None):
    engine = translator(AutoTranslate, '')
    if settings is not None:
        engine.pj_config.getKey = lambda key, default=None: settings.get(key, default)
    engine._batch_scope = ContextVar('test_auto', default=None)
    engine.calls = []
    engine.plans = []  # (模板, 句数, 改善拒答方式)
    engines = {}
    for mode in (*GAL_MODES, 'ForNovel-tool', 'ForNovel'):
        delegate = translator(ENGINES[0][0], ENGINES[0][1])
        async def translate(rows, glossary='', *, filename='', proofread=False, mode=mode):
            engine.calls.append((mode, len(rows)))
            engine.plans.append((mode, len(rows), JAILBREAK_OVERRIDE.get()))
            await asyncio.sleep(0)  # 让并发批次真正交错
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
    async def test_api_error_keeps_strategy_format_error_switches_and_success_rate_is_learned(self):
        engine = auto_engine([httpx.ReadError('network'), TranslationParseError('parse')])
        rows = named(16)
        count, result = await engine.translate(rows, filename='a')
        # 网络错误：模板没问题，原样重试、不缩小；格式错误：换模板并缩小
        self.assertEqual(engine.calls, [('ForGal-tool', 16), ('ForGal-tool', 16), ('ForGal-markdown', 8)])
        self.assertEqual(count, 8)
        self.assertIs(result[0], rows[0])
        self.assertEqual(rows[8].pre_dst, '')
        await engine.translate(rows[8:], filename='a')
        # 新文件没有历史：tool 刚格式出错过，按成功率直接从 markdown 开始；小说统计独立
        await engine.translate(named(1), filename='b')
        await engine.translate(sentences(1), filename='novel')
        self.assertEqual(engine.calls[-3:], [('ForGal-markdown', 8), ('ForGal-markdown', 1), ('ForNovel-tool', 1)])

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
            self.assertEqual(engine._state()['template'], {})
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
        self.assertEqual([event['index_range'] for event in events], ['1~64', '1~64', '1~32', '1~16', '1~8'])
        self.assertTrue(all(event['filename'] == 'chapter.json' for event in events))
        self.assertTrue(all(call.args == ('test-project',) for call in report.call_args_list))
        self.assertIn('ForGal-tool', events[0]['message'])
        self.assertIn('API 错误，保持 ForGal-tool', events[0]['message'])
        self.assertIn('格式错误，切换 ForGal-markdown', events[1]['message'])
        self.assertIn('标记翻译失败', events[-1]['message'])

    async def test_error_reporting_failure_does_not_interrupt_retry(self):
        from unittest.mock import patch
        engine = auto_engine([TranslationParseError('bad')])
        with patch('GalTransl.server.record_runtime_error', side_effect=RuntimeError('unavailable')):
            count, rows = await engine.translate(named(4), filename='a')
        self.assertEqual(count, 2)
        self.assertEqual(rows[0].pre_dst, 'translated')
        self.assertEqual(len(engine.calls), 2)


IMAGE_ALLOWED = {'gpt.enhance_jailbreak': False, 'gpt.enhance_jailbreak.mode': 'image'}


def refused():
    return TranslationParseError('输出为空/被拦截', reply='')


class AutoStrategyTests(unittest.IsolatedAsyncioTestCase):
    async def test_refusal_escalates_jailbreak_and_learns_which_one_works(self):
        engine = auto_engine([refused(), refused()], IMAGE_ALLOWED)
        count, _ = await engine.translate(named(16), filename='a')
        # 被拒：换模板没用，先升到便宜的预置补全（只有 json 支持），还被拒再升到图片
        self.assertEqual(engine.plans, [
            ('ForGal-tool', 16, ''), ('ForGal-json', 8, 'prefill'), ('ForGal-tool', 4, 'image'),
        ])
        self.assertEqual(count, 4)
        # 同一文件之后直接沿用图片方式
        await engine.translate(named(4), filename='a')
        self.assertEqual(engine.plans[-1], ('ForGal-tool', 4, 'image'))
        # 新文件按自己的经验从不改善拒答开始；被拒后按已学到的效果直接跳到图片，跳过无效的预置补全
        engine.plans.clear()
        engine_actions(engine, [refused()])
        await engine.translate(named(4), filename='b')
        self.assertEqual(engine.plans, [('ForGal-tool', 4, ''), ('ForGal-tool', 2, 'image')])

    async def test_high_jailbreak_success_rate_alone_never_turns_it_on(self):
        engine = auto_engine([TranslationParseError('bad'), httpx.ReadError('network')], IMAGE_ALLOWED)
        model = engine._model_name(0)
        engine._state()['jailbreak'][('gal', model, 'prefill')] = (50, 0)
        engine._state()['jailbreak'][('gal', model, 'image')] = (50, 0)
        engine._state()['jailbreak'][('gal', model, 'none')] = (0, 5)
        for i in range(3):
            await engine.translate(named(4), filename=f'f{i}')
        # 只有被拒才会升级；格式错误、API 错误和历史成功率都不会让它改善拒答
        self.assertEqual({plan[2] for plan in engine.plans}, {''})

    async def test_escalation_skips_jailbreak_known_to_be_refused(self):
        engine = auto_engine([refused()], IMAGE_ALLOWED)
        model = engine._model_name(0)
        engine._state()['jailbreak'][('gal', model, 'prefill')] = (0, 3)
        engine._state()['jailbreak'][('gal', model, 'image')] = (3, 0)
        await engine.translate(named(4), filename='a')
        # 预置补全一直被拒、图片有效：直接跳到图片，跳过无效的预置补全
        self.assertEqual(engine.plans, [('ForGal-tool', 4, ''), ('ForGal-tool', 2, 'image')])

    async def test_concurrent_refusals_never_downgrade_shared_level(self):
        engine = auto_engine([refused(), refused(), refused()], IMAGE_ALLOWED)
        model = engine._model_name(0)
        engine._state()['jailbreak'][('gal', model, 'image')] = (3, 0)
        # 同一文件的三个片段同时以「不改善拒答」发出、同时被拒，都只会把该文件的等级往上调
        await asyncio.gather(*(engine.translate(named(4), filename='a') for _ in range(3)))
        strategy = next(iter(engine._state()['strategies'].values()))
        self.assertEqual(strategy.level, 2)
        self.assertEqual([plan[2] for plan in engine.plans[3:]], ['image'] * 3)

    async def test_without_image_mode_refusal_moves_to_prefill_capable_template(self):
        actions = [refused()]
        engine = auto_engine(actions, {'gpt.enhance_jailbreak.mode': 'prefill'})
        await engine.translate(named(4), filename='gal')
        actions.append(refused())
        await engine.translate(sentences(4), filename='novel')
        self.assertEqual(engine.plans, [
            ('ForGal-tool', 4, ''), ('ForGal-json', 2, 'prefill'),
            ('ForNovel-tool', 4, ''), ('ForNovel', 2, 'prefill'),
        ])
        # 已无更强方式：继续缩小批次隔离问题句，而不是换回不改善拒答的模板
        engine = auto_engine([refused()] * 3, {'gpt.enhance_jailbreak.mode': 'prefill'})
        await engine.translate(named(8), filename='gal')
        self.assertEqual(engine.plans, [
            ('ForGal-tool', 8, ''), ('ForGal-json', 4, 'prefill'),
            ('ForGal-json', 2, 'prefill'), ('ForGal-json', 1, 'prefill'),
        ])

    async def test_always_on_uses_configured_mode_and_never_probes_below_it(self):
        engine = auto_engine([], {'gpt.enhance_jailbreak': True, 'gpt.enhance_jailbreak.mode': 'image'})
        for _ in range(5):
            await engine.translate(named(2), filename='a')
        self.assertEqual({plan[2] for plan in engine.plans}, {'image'})
        engine = auto_engine([], {'gpt.enhance_jailbreak': True, 'gpt.enhance_jailbreak.mode': 'prefill'})
        await engine.translate(named(2), filename='a')
        await engine.translate(sentences(2), filename='novel')
        self.assertEqual(engine.plans, [('ForGal-json', 2, 'prefill'), ('ForNovel', 2, 'prefill')])

    async def test_probes_back_down_after_streak_and_backs_off_when_probe_is_refused(self):
        engine = auto_engine([refused()], IMAGE_ALLOWED)
        await engine.translate(named(8), filename='a')
        self.assertEqual(engine.plans[-1], ('ForGal-json', 4, 'prefill'))
        with self.assertLogs('GalTransl', level='INFO') as logs:
            await engine.translate(named(4), filename='a')
            await engine.translate(named(4), filename='a')
        self.assertTrue(any('已连续成功 3 批，派下一批试着降回 不改善拒答' in line for line in logs.output))
        # 试降：只有这一批不改善拒答；被拒说明还需要预置补全，原样大小恢复重试，下次试降间隔加倍
        engine.plans.clear()
        engine_actions(engine, [refused()])
        await engine.translate(named(4), filename='a')
        self.assertEqual(engine.plans, [('ForGal-json', 4, ''), ('ForGal-json', 4, 'prefill')])
        strategy = next(iter(engine._state()['strategies'].values()))
        self.assertEqual((strategy.probe_after, strategy.probe_level, strategy.probe_inflight), (6, -1, False))

    async def test_only_one_concurrent_batch_probes_and_success_lowers_everyone(self):
        engine = auto_engine([refused()], IMAGE_ALLOWED)
        for _ in range(3):
            await engine.translate(named(4), filename='a')
        engine.plans.clear()
        # 同一文件的并发片段里只有一个去试降
        await asyncio.gather(*(engine.translate(named(4), filename='a') for _ in range(3)))
        self.assertEqual(sorted(plan[2] for plan in engine.plans), ['', 'prefill', 'prefill'])
        # 试降成功：本文件之后不再改善拒答
        engine.plans.clear()
        await engine.translate(named(4), filename='a')
        self.assertEqual(engine.plans, [('ForGal-json', 4, '')])
        strategy = next(iter(engine._state()['strategies'].values()))
        self.assertEqual((strategy.level, strategy.probe_level), (0, -1))

    async def test_image_unsupported_disables_image_without_shrinking(self):
        error = TranslationRequestError('Error code: 400 - image_url is not supported by this model')
        engine = auto_engine([error], {'gpt.enhance_jailbreak': True, 'gpt.enhance_jailbreak.mode': 'image'})
        with self.assertLogs('GalTransl', level='WARNING') as logs:
            await engine.translate(named(4), filename='a')
        self.assertEqual(engine.plans, [('ForGal-tool', 4, 'image'), ('ForGal-json', 4, 'prefill')])
        self.assertTrue(any('停用图片方式' in line for line in logs.output))
        await engine.translate(named(4), filename='b')
        self.assertEqual(engine.plans[-1], ('ForGal-json', 4, 'prefill'))

    async def test_strategy_summary_reports_success_rates(self):
        engine = auto_engine([TranslationParseError('bad'), refused()], IMAGE_ALLOWED)
        await engine.translate(named(4), filename='a')
        lines = engine.strategy_summary()
        self.assertEqual(len(lines), 1)
        self.assertIn('ForGal-tool 0/1', lines[0])
        # 被拒只记到改善拒答方式上，不算模板的格式成败
        self.assertNotIn('ForGal-markdown', lines[0])
        self.assertIn('ForGal-json 1/1', lines[0])
        self.assertIn('不改善拒答未被拒 0/1', lines[0])
        self.assertIn('预置补全未被拒 1/1', lines[0])

    def test_classify_failure(self):
        cases = [
            (TranslationParseError('输出为空/被拦截'), 'none', 'refusal'),
            (TranslationParseError('finish_reason=content_filter'), 'none', 'refusal'),
            (TranslationParseError('缺少工具调用', reply=''), 'none', 'refusal'),
            (TranslationParseError('缺少工具调用', reply='<think>x</think>  '), 'none', 'refusal'),
            (TranslationParseError('缺少工具调用', reply="I'm sorry, but I can't help with that."), 'none', 'refusal'),
            (TranslationParseError('缺少工具调用', reply='抱歉，我无法翻译这段内容。'), 'none', 'refusal'),
            (TranslationParseError('解析失败'), 'none', 'format'),
            (TranslationParseError('缺句', reply='| {"id": 1, "dst": "抱歉"}'), 'none', 'format'),
            (TranslationParseError('缺句', reply='对不起，' * 200), 'none', 'format'),
            (TranslationParseError('缺句', reply='好的，这是译文'), 'none', 'format'),
            (TranslationRequestError('The prompt could not be submitted: PROHIBITED_CONTENT'), 'none', 'refusal'),
            (TranslationRequestError('Error code: 400 - image_url is not supported'), 'image', 'image_unsupported'),
            (TranslationRequestError('Error code: 400 - image_url is not supported'), 'prefill', 'api'),
            (httpx.ReadError('network'), 'image', 'api'),
            (TranslationRequestError('Error code: 429 - rate limited'), 'none', 'api'),
        ]
        for exc, jb, expected in cases:
            with self.subTest(exc=str(exc)[:40], jb=jb):
                self.assertEqual(classify_failure(exc, jb), expected)


def engine_actions(engine, actions):
    """给已有的假引擎换一组新的结果序列（各模板共用）。"""
    for mode in (*GAL_MODES, 'ForNovel-tool', 'ForNovel'):
        delegate = engine._get_engine(mode)
        async def translate(rows, glossary='', *, filename='', proofread=False, mode=mode):
            engine.calls.append((mode, len(rows)))
            engine.plans.append((mode, len(rows), JAILBREAK_OVERRIDE.get()))
            await asyncio.sleep(0)  # 让并发批次真正交错
            action = actions.pop(0) if actions else None
            if isinstance(action, BaseException):
                raise action
            for row in rows:
                row.pre_dst = row.post_dst = 'translated'
            return len(rows), rows
        delegate.translate = translate
