"""读取工具的 Markdown 输出：内容保真、去重，以及真实模型消息路径。"""

import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from GalTransl.Agent.models import AgentState
from GalTransl.Agent.runner import AgentRunner
from GalTransl.Agent.subagent import SubAgentRunner
from GalTransl.Agent.tools.cache import _tool_read_output
from GalTransl.Agent.tools.dicts import _tool_list_dict_files, _tool_read_dict
from GalTransl.Agent.tools.project import _tool_get_project_overview
from GalTransl.Agent.tools.render_md import _render_tool_result_table
from tests.test_agent_markdown_output import _table_rows


class ReadDictionaryMarkdownTests(unittest.TestCase):
    def test_dictionary_code_block_preserves_all_lines_and_whitespace(self):
        lines = ['// 注释', '', '  アリス\t爱丽丝\t人名  ', '```', '````text', '\\n\t<br>|', '']
        lines += [f'词{i}\t译{i}' for i in range(30)]
        result = {'file_key': '(project_dir)GPT.txt', 'lines': lines, 'count': len(lines)}
        original = copy.deepcopy(result)
        text = _render_tool_result_table('read_dict', result)
        raw = text.split('`````text\n', 1)[1].rsplit('\n`````', 1)[0]
        self.assertEqual(raw, '\n'.join(lines))
        self.assertEqual(result, original)
        self.assertIn(f'共 {len(lines)} 行', text)

    def test_dictionary_handler_and_empty_file(self):
        runner = SimpleNamespace(
            state=SimpleNamespace(config_file_name='config.yaml'),
            _project_id=lambda: 'test',
            _http_get=lambda _: {'dict_contents': {'GPT.txt': {'lines': [], 'count': 0}}},
        )
        result = _tool_read_dict(runner, {'file_key': 'GPT.txt'})
        text = _render_tool_result_table('read_dict', result)
        self.assertIn('共 0 行', text)
        self.assertIn('字典为空', text)

    def test_list_merges_counts_preserving_category_order_and_missing_counts(self):
        result = {
            'pre_dict_files': ['b.txt', 'a.txt'],
            'gpt_dict_files': ['a.txt', 'b.txt', 'missing.txt'],
            'post_dict_files': [],
            'line_counts': {'a.txt': 0, 'b.txt': 3, 'orphan.txt': 7},
        }
        text = _render_tool_result_table('list_dict_files', result)
        self.assertEqual(_table_rows(text, 'category'), [
            ['pre', 'b.txt', '3'], ['pre', 'a.txt', '0'],
            ['gpt', 'a.txt', '0'], ['gpt', 'b.txt', '3'], ['gpt', 'missing.txt', '未知'],
            ['未分类', 'orphan.txt', '7'],
        ])
        self.assertIn('post 0 个', text)

    def test_list_handler_does_not_echo_dictionary_contents_or_duplicate_filenames(self):
        runner = SimpleNamespace(
            state=SimpleNamespace(config_file_name='config.yaml'), _project_id=lambda: 'test',
            _http_get=lambda _: {
                'gpt_dict_files': ['GPT.txt'],
                'dict_contents': {'GPT.txt': {'count': 1, 'lines': ['secret\t正文']}},
            },
        )
        text = _render_tool_result_table('list_dict_files', _tool_list_dict_files(runner, {}))
        self.assertEqual(text.count('GPT.txt'), 1)
        self.assertNotIn('secret', text)
        empty = _render_tool_result_table('list_dict_files', {'pre_dict_files': [], 'gpt_dict_files': [], 'post_dict_files': [], 'line_counts': {}})
        self.assertIn('没有配置字典文件', empty)


class ReadOutputMarkdownTests(unittest.TestCase):
    def test_handler_preserves_final_text_source_indexes_and_missing_indexes(self):
        runner = SimpleNamespace(
            state=SimpleNamespace(config_file_name='config.yaml'), _project_id=lambda: 'test',
            _http_get=lambda _: {'entries': [
                {'index': 1, 'name': '甲', 'pre_src': '第一句'},
                {'index': 13, 'name': '乙', 'pre_src': '最终|译文\n第二行'},
            ]},
        )
        result = _tool_read_output(runner, {'filename': 'out.json', 'index': '13,99'})
        text = _render_tool_result_table('read_output', result)
        self.assertEqual(_table_rows(text, 'index'), [['13', '乙', '最终\\|译文<br>第二行']])
        self.assertIn('共 2 条，显示 1 条', text)
        self.assertIn('缺失 index：99', text)

    def test_empty_output_still_reports_file_and_zero(self):
        text = _render_tool_result_table('read_output', {'filename': 'empty.json', 'count': 0, 'returned': 0, 'entries': []})
        self.assertIn('empty.json', text)
        self.assertIn('共 0 条，显示 0 条', text)


class OverviewMarkdownTests(unittest.TestCase):
    def test_config_values_and_descriptions_share_one_row_without_losing_types(self):
        config = {'common': {'gpt.contextNum': 0, 'enabled': False, 'empty': '', 'unset': None, 'array': [], 'object': {}, 'order': ['b', 'a'], 'nested': {'text': 'a|b\nnext'}}}
        descriptions = {'common': '通用设置', 'common.gpt.contextNum': '上下文句数', 'common.nested': '嵌套设置'}
        result = {'config': config, 'config_field_descriptions': descriptions}
        original = copy.deepcopy(result)
        text = _render_tool_result_table('get_project_overview', result)
        rows = {row[0]: row[1:] for row in _table_rows(text, 'key')}
        self.assertEqual(rows['common.gpt.contextNum'], ['0', '上下文句数'])
        self.assertEqual(rows['common.enabled'][0], 'false')
        self.assertEqual(rows['common.empty'][0], '""')
        self.assertEqual(rows['common.unset'][0], 'null')
        self.assertEqual(rows['common.array'][0], '[]')
        self.assertEqual(rows['common.object'][0], '{}')
        self.assertEqual(rows['common.order'][0], '["b","a"]')
        self.assertEqual(rows['common.nested.text'][0], '"a\\|b\\nnext"')
        self.assertEqual(rows['common'][1], '通用设置')
        self.assertEqual(rows['common.nested'][1], '嵌套设置')
        self.assertEqual(text.count('common.gpt.contextNum'), 1)
        self.assertEqual(result, original)

    def test_include_config_and_descriptions_work_independently(self):
        config = _render_tool_result_table('get_project_overview', {'config': {'common': {'language': 'zh-cn'}}})
        self.assertNotIn('description', config)
        descriptions = _render_tool_result_table('get_project_overview', {'config_field_descriptions': {'common.language': '语言'}})
        self.assertNotIn('| value |', descriptions)
        self.assertEqual(_table_rows(descriptions, 'key'), [['common.language', '语言']])
        empty = _render_tool_result_table('get_project_overview', {'config': {}})
        self.assertIn('没有配置项', empty)

    def test_progress_only_keeps_scope_note_and_skips_config_fetch(self):
        paths = []
        def get(path):
            paths.append(path)
            if '/progress?' in path:
                return {'total': 10, 'translated': 10, 'failed': 0, 'problems': 0, 'files': []}
            if path.endswith('/files'):
                return {'input_files': [{'name': 'a.json'}, {'name': 'b.json'}]}
            raise AssertionError(path)
        runner = AgentRunner(AgentState())
        runner.state.project_dir = 'test'
        with patch.object(runner, '_http_get', side_effect=get):
            result = _tool_get_project_overview(runner, {'include': ['progress']})
        text = _render_tool_result_table('get_project_overview', result)
        rows = dict(_table_rows(text, 'field'))
        self.assertEqual(rows['total'], '10')
        self.assertEqual(rows['files_untranslated'], '2')
        self.assertIn('不代表整个项目翻完', text)
        self.assertIn('本次只返回了progress', text)
        self.assertNotIn('## config', text)
        self.assertEqual(len(paths), 2)

    def test_backend_roles_and_note_are_kept(self):
        text = _render_tool_result_table('get_project_overview', {'backend': {
            'agent': {'name': 'Agent配置', 'type': 'OpenAI-Compatible', 'model': 'a'},
            'translator': {'name': '翻译配置', 'type': 'OpenAI-Compatible', 'model': 'b'},
            'note': '实际生效的配置',
        }})
        self.assertEqual(_table_rows(text, 'role'), [
            ['agent', 'Agent配置', 'OpenAI-Compatible', 'a'],
            ['translator', '翻译配置', 'OpenAI-Compatible', 'b'],
        ])
        self.assertIn('实际生效的配置', text)


class ReadMarkdownPipelineTests(unittest.TestCase):
    def test_main_agent_receives_markdown_for_all_four_tools(self):
        results = {
            'read_dict': {'file_key': 'GPT.txt', 'lines': ['a\tA'], 'count': 1},
            'read_output': {'filename': 'a.json', 'count': 1, 'returned': 1, 'entries': [{'index': 1, 'name': '', 'message': 'A'}]},
            'list_dict_files': {'gpt_dict_files': ['GPT.txt'], 'line_counts': {'GPT.txt': 1}},
            'get_project_overview': {'config': {'common': {'language': 'zh-cn'}}},
        }
        runner = AgentRunner(AgentState())
        runner.state.messages = [{'role': 'user', 'content': 'read'}]
        calls = [{'id': name, 'name': name, 'arguments': '{}'} for name in results]
        with patch.object(runner, '_resolve_llm'), patch.object(runner, '_dispatch_tool', side_effect=lambda name, _: results[name]), patch.object(runner, '_stream_llm_response', side_effect=[('', calls, 'tool_calls'), ('done', [], 'stop')]):
            runner.run()
        messages = {message['tool_call_id']: message['content'] for message in runner.state.messages if message['role'] == 'tool'}
        self.assertEqual(set(messages), set(results))
        for name, result in results.items():
            self.assertEqual(messages[name], _render_tool_result_table(name, result))
            with self.assertRaises(json.JSONDecodeError):
                json.loads(messages[name])

    def test_subagent_receives_raw_dictionary_in_markdown(self):
        parent = AgentRunner(AgentState())
        sub = SubAgentRunner(parent, agent='explore', files=[], indexes='', brief='', delegation_id='test')
        call = SimpleNamespace(id='c1', function=SimpleNamespace(name='read_dict', arguments='{}'))
        result = {'file_key': 'GPT.txt', 'count': 2, 'lines': ['// keep', 'a\tA']}
        message = sub._run_tool(call, {'read_dict': lambda *_: result})
        self.assertIn('```text\n// keep\na\tA\n```', message['content'])


if __name__ == '__main__':
    unittest.main()
