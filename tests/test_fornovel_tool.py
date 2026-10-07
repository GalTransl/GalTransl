import json
import unittest
from types import SimpleNamespace

from GalTransl.Backend.ForNovelToolTranslate import ForNovelToolTranslate
from GalTransl.Backend.Prompts import FORNOVEL_TOOL_TRANS_PROMPT
from test_translation_multiturn import translator, sentences
from test_forgal_tool import function_call, tool_input_rows


class NovelToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_novel_tool_omits_name_writes_immediately_and_finishes_without_closing_request(self):
        engine = translator(ForNovelToolTranslate, FORNOVEL_TOOL_TRANS_PROMPT)
        engine.multi_turn = False
        engine.eng_type = 'ForNovel-tool'
        requests, events = [], []
        engine._record_runtime_success = lambda filename, row: events.append((filename, row.index, row.pre_dst))
        async def reply(**kwargs):
            requests.append(kwargs['messages'])
            prompt = kwargs['messages'][-1]['content']
            self.assertIn('Translate the novel text', prompt)
            self.assertIn('without a name field', prompt)
            rows = []
            self.assertIn('| ID | SRC |', prompt)
            self.assertNotIn('| NAME |', prompt)
            for sig, obj in tool_input_rows(prompt):
                rows.append(f"@@ {sig}|{obj['id']}\n+translated-{obj['id']}")
            patch = '*** Begin Patch\n' + '\n'.join(rows) + '\n*** End Patch'
            kwargs['tool_response_holder'].update(tool_calls=[function_call('write_translation_result', patch)], finish_reason='tool_calls')
            return '', SimpleNamespace(model_name='novel-model')
        engine.ask_chatbot = reply
        rows = sentences(3)
        for row in rows:
            row.speaker = 'should not be sent'
        count, result = await engine.translate(rows[:2], filename='novel')
        self.assertEqual(count, 2)
        self.assertEqual([row.pre_dst for row in result], ['translated-1', 'translated-2'])
        self.assertEqual(len(requests), 1)
        self.assertEqual(events, [('novel', 1, 'translated-1'), ('novel', 2, 'translated-2')])
        await engine.translate(rows[2:], filename='novel')
        self.assertEqual(len(requests), 2)
        self.assertEqual([m['role'] for m in requests[1]], ['system', 'user'])

    def test_registration(self):
        from GalTransl import TRANSLATOR_SUPPORTED, NEED_OpenAITokenPool
        from GalTransl.server import _DEFAULT_TRANSLATOR_PROMPTS
        from GalTransl.Agent.tool_schemas import AGENT_TOOLS
        self.assertIn('ForNovel-tool', TRANSLATOR_SUPPORTED)
        self.assertIn('ForNovel-tool', NEED_OpenAITokenPool)
        self.assertEqual(_DEFAULT_TRANSLATOR_PROMPTS['ForNovel-tool']['user_prompt'], FORNOVEL_TOOL_TRANS_PROMPT)
        schema = next(t['function'] for t in AGENT_TOOLS if t['function']['name'] == 'start_translation')
        self.assertIn('ForNovel-tool', schema['parameters']['properties']['translator']['enum'])

    def test_both_tool_templates_render_markdown_input_and_history(self):
        from GalTransl.Backend.ForGalToolTranslate import ForGalToolTranslate
        from GalTransl.Backend.Prompts import FORGAL_TOOL_TRANS_PROMPT
        from GalTransl.Backend.ForGalMarkdownTranslate import parse_markdown_row
        for kind, prompt in [(ForGalToolTranslate, FORGAL_TOOL_TRANS_PROMPT), (ForNovelToolTranslate, FORNOVEL_TOOL_TRANS_PROMPT)]:
            engine = translator(kind, prompt)
            row = engine._encode_sig_jsonline('abc', {'id': 1, 'name': 'A|B', 'src': 'text|next<br>line', 'dst': 'draft'})
            table = engine._format_translation_input([row], proofread=True)
            cells = parse_markdown_row(table.splitlines()[2])
            self.assertEqual(cells[0], 'abc|1')
            self.assertEqual(cells[-2:], ['text|next<br>line', 'draft'])
            self.assertEqual(len(cells), 4 if kind is ForGalToolTranslate else 3)
            previous = sentences(1)[0]
            previous.pre_dst = 'history|text\nnext'
            history = engine._format_restore_context_payload([engine._format_restore_context_line(previous)])
            engine.last_translations['a'] = history
            rendered = engine._apply_history_result('<history_result>[history_result]</history_result>', 'a')
            self.assertIn('<br>', rendered)
            self.assertNotIn('jsonline', rendered)
            self.assertEqual(parse_markdown_row(history.splitlines()[2])[-1], 'history|text<br>next')
