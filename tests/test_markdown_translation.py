import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from GalTransl.Backend.BaseTranslate import TranslationParseError, _CHATBOT_STATE
from GalTransl.Backend.ForGalMarkdownTranslate import ForGalMarkdownTranslate, markdown_row, parse_markdown_row
from GalTransl.Backend.Prompts import FORGAL_MARKDOWN_TRANS_PROMPT_EN
from test_translation_multiturn import translator, sentences


class MarkdownTranslationTests(unittest.IsolatedAsyncioTestCase):
    async def test_table_input_output_escapes_and_br_streaming_and_nonstream(self):
        for stream in (False, True):
            engine = translator(ForGalMarkdownTranslate, FORGAL_MARKDOWN_TRANS_PROMPT_EN)
            engine.fail_fast = True
            rows = sentences(2)
            rows[0].post_src = 'source|path\\file\nnext'
            rows[1].post_src = 'already<br>split'
            emitted = []
            engine._record_runtime_success = lambda filename, row: emitted.append(row.index)
            async def reply(**kwargs):
                prompt = kwargs['messages'][-1]['content']
                self.assertIn('| ID | NAME | SRC |\n| --- | --- | --- |', prompt)
                self.assertIn(r'source\|path\\file<br>next', prompt)
                lines = ['| ID | NAME | DST |', '| :--- | ---: | --- |', markdown_row('1', 'null', 'translated|path\\file<br>next'), markdown_row('2', 'null', 'already<br>translated')]
                _CHATBOT_STATE.set((stream, 'model'))
                if stream:
                    for line in lines:
                        self.assertTrue(kwargs['stream_line_callback']([line], False))
                return '\n'.join(lines), SimpleNamespace(model_name='model')
            engine.ask_chatbot = reply
            count, result = await engine.translate(rows, filename='a')
            self.assertEqual(count, 2)
            self.assertEqual(result[0].pre_dst, 'translated|path\\file\nnext')
            self.assertEqual(result[1].pre_dst, 'already<br>translated')
            if stream:
                self.assertEqual(emitted, [1, 2])

    async def test_reject_tsv_bad_headers_bad_columns_and_wrong_id(self):
        header = '| ID | NAME | DST |\n| --- | --- | --- |\n'
        for text in ('null\ttext\t1', '| 1 | null | text |', '| ID | NAME | DST |\n| 1 | null | text |', header + '| 10 | null | text |', header + '| 1 | null | unescaped|pipe |'):
            engine = translator(ForGalMarkdownTranslate, FORGAL_MARKDOWN_TRANS_PROMPT_EN)
            engine.fail_fast = True
            engine.ask_chatbot = AsyncMock(return_value=(text, SimpleNamespace(model_name='model')))
            with self.assertRaises(TranslationParseError):
                await engine.translate(sentences(1), filename='a')
            self.assertEqual(engine.ask_chatbot.await_count, 1)

    def test_cell_round_trip(self):
        cells = ['name|alias', r'path\to\file | literal \| pipe', '42']
        self.assertEqual(parse_markdown_row(markdown_row(*cells)), cells)

    async def test_batch_uses_markdown_multiturn_with_history_and_reasoning(self):
        from test_translation_multiturn import Model
        engine = translator(ForGalMarkdownTranslate, FORGAL_MARKDOWN_TRANS_PROMPT_EN)
        model = Model(engine)
        engine.ask_chatbot = model
        result = await engine.batch_translate('a', '', sentences(3), 1, translist_unhit=sentences(3))
        self.assertEqual(len(result), 3)
        self.assertEqual([len(request) for request in model.requests], [2, 4, 6])
        for request in model.requests:
            self.assertIn('| ID | NAME | SRC |\n| --- | --- | --- |', request[-1]['content'])
        second = model.requests[1]
        self.assertIn('| ID | NAME | DST |', second[2]['content'])
        self.assertEqual(second[2]['reasoning_content'], 'thinking-1')
        self.assertIn('CUSTOM-GUIDELINE', second[1]['content'])
        self.assertNotIn('CUSTOM-GUIDELINE', second[-1]['content'])
        self.assertIn('columns ID, NAME, DST', second[-1]['content'])
        self.assertIn('Keep <br>', second[-1]['content'])
        self.assertIsNone(engine._session_scope().get())
