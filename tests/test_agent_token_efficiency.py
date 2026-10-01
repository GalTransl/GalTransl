"""Token 优化的请求级回归；全部使用内存响应，不连接模型服务。"""

import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from GalTransl.Agent import context, subagent
from GalTransl.Agent.models import AgentState
from GalTransl.Agent.runner import AgentRunner
from GalTransl.Agent.tool_schemas import AGENT_TOOLS
from GalTransl.Agent.tools.render_md import _render_tool_result_table, _tool_result_json


def _call(arguments='{}'):
    return SimpleNamespace(
        id='call-1', type='function',
        function=SimpleNamespace(name='read_dict', arguments=arguments),
    )


def _response(tokens=None, *, calls=None, content='完成', reasoning=''):
    message = SimpleNamespace(content=content, tool_calls=calls or [], reasoning_content=reasoning)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(prompt_tokens=tokens) if tokens is not None else None,
    )


def _parent(responses=()):
    runner = AgentRunner(AgentState())
    runner._model = 'local-test'
    runner._openai_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(side_effect=responses)))
    )
    return runner


def _sub(parent):
    return subagent.SubAgentRunner(
        parent, agent='explore', files=[], indexes='', brief='只读测试', delegation_id='sub-test',
    )


def _has_cache(message):
    if 'cache_control' in message:
        return True
    content = message.get('content')
    return isinstance(content, list) and any('cache_control' in block for block in content)


class CompactToolJsonTests(unittest.TestCase):
    def test_round_trip_preserves_text_and_all_fields(self):
        result = {
            'text': '  中文\n日本語\r\n\t$str20 | <br> "引号" \\n  ',
            'nested': [{'ok': True, 'empty': None, 'failed': [1, 2]}],
            'reason': '不能省略的失败详情',
        }
        before = copy.deepcopy(result)
        text = _tool_result_json(result)
        self.assertEqual(json.loads(text), before)
        self.assertEqual(result, before)
        self.assertLess(len(text), len(json.dumps(result, ensure_ascii=False)))
        self.assertIn('日本語', text)

    def test_long_body_is_not_truncated(self):
        text = '保留全部正文与空白  \n' * 10000
        self.assertEqual(json.loads(_tool_result_json({'content': text}))['content'], text)

    def test_existing_markdown_remains_the_model_format(self):
        result = {'filename': 'a.json', 'count': 1, 'returned': 1,
                  'entries': [{'index': 1, 'name': '$str20', 'pre_src': '原文|内容\n下一行'}]}
        rendered = _render_tool_result_table('read_input_file', result)
        self.assertIn('$str20', rendered)
        self.assertIn('原文\\|内容<br>下一行', rendered)

    def test_subagent_compacts_history_but_keeps_ui_preview(self):
        parent = _parent()
        sub = _sub(parent)
        result = {'content': '正文  空白', 'entries': [1, 2], 'ok': True}
        message = sub._run_tool(_call(), {'read_dict': lambda _parent, _args: result})
        self.assertEqual(message['content'], _tool_result_json(result))
        event = next(e for e in parent.state.transient_events if e.type == 'subagent_tool_result')
        self.assertEqual(event.data['result'], json.dumps(result, ensure_ascii=False))


class PromptCacheRequestTests(unittest.TestCase):
    def test_final_request_skips_injected_message_and_preserves_history(self):
        parent = _parent([iter(())])
        parent._prompt_caching = True
        parent.state.messages = [
            {'role': 'system', 'content': '稳定前缀'},
            {'role': 'user', 'content': '读字典'},
            {'role': 'assistant', 'content': '', 'reasoning_content': '先读取', 'tool_calls': [
                {'id': 'call-1', 'type': 'function', 'function': {'name': 'read_dict', 'arguments': '{}'}},
            ]},
            {'role': 'tool', 'tool_call_id': 'call-1', 'content': '{"content":"字典"}'},
            {'role': 'user', 'content': '临时压缩指令', '_compact_instruction': True},
        ]
        original = copy.deepcopy(parent.state.messages)
        tools = copy.deepcopy(AGENT_TOOLS)
        parent._create_stream(include_usage=True)
        request = parent._openai_client.chat.completions.create.call_args.kwargs
        messages = request['messages']
        self.assertEqual([i for i, m in enumerate(messages) if _has_cache(m)], [2, 3])
        self.assertEqual(messages[-1]['content'], '临时压缩指令')
        self.assertTrue(all(not key.startswith('_') for m in messages for key in m))
        self.assertEqual(messages[2]['reasoning_content'], '先读取')
        self.assertTrue(_has_cache(request['tools'][-1]))
        self.assertEqual(parent.state.messages, original)
        self.assertEqual(AGENT_TOOLS, tools)

    def test_empty_messages_do_not_consume_marker_budget(self):
        messages = [
            {'role': 'user', 'content': 'a'}, {'role': 'assistant', 'content': 'b'},
            {'role': 'user', 'content': ''}, {'role': 'assistant', 'content': None},
        ]
        result, _ = context._apply_prompt_cache(messages, [])
        self.assertEqual([i for i, m in enumerate(result) if _has_cache(m)], [0, 1])

    def test_disabled_cache_keeps_plain_content_and_repairs_tool_pairs(self):
        parent = _parent([iter(())])
        parent.state.messages = [
            {'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': 'call-1', 'type': 'function', 'function': {'name': 'read_dict', 'arguments': '{}'}},
            ]},
            {'role': 'user', 'content': '继续', '_compact_summary': True},
        ]
        parent._reasoning_field = 'reasoning_content'
        parent._create_stream(include_usage=False)
        request = parent._openai_client.chat.completions.create.call_args.kwargs
        messages = request['messages']
        self.assertFalse(any(_has_cache(m) for m in messages + request['tools']))
        self.assertEqual(messages[0]['reasoning_content'], '')
        self.assertEqual(messages[1]['tool_call_id'], 'call-1')
        self.assertEqual(messages[-1]['content'], '继续')
        self.assertNotIn('_compact_summary', messages[-1])
        self.assertNotIn('stream_options', request)
        self.assertEqual(len(parent.state.messages), 2)


class SubagentBudgetTests(unittest.TestCase):
    def test_usage_anchors_before_new_messages_without_counting_schema_twice(self):
        parent = _parent([_response(32100), _response()])
        sub = _sub(parent)
        sub.messages = [{'role': 'system', 'content': '规则'}, {'role': 'user', 'content': '任务'}]
        tools = subagent._subagent_tools('explore')
        sub._chat_with_retry(parent._openai_client, parent._model, tools)
        self.assertEqual(sub._estimate_context_tokens(), 32100)
        self.assertEqual(sub._anchored_message_count, 2)
        added = {'role': 'assistant', 'content': '新增中文', 'reasoning_content': '需要保留的思考'}
        sub.messages.append(added)
        expected = 32100 + context._estimate_message_tokens(added)
        self.assertEqual(sub._estimate_context_tokens(), expected)
        # 本次无 usage 时，已有有效锚点仍可估算新增部分。
        sub._chat_with_retry(parent._openai_client, parent._model, tools)
        self.assertEqual(sub._estimate_context_tokens(), expected)
        self.assertEqual(parent.state.last_prompt_tokens, 0)

    def test_invalid_or_missing_usage_falls_back_to_estimation(self):
        for value in (None, 0, -1, True, '100', 1.5):
            with self.subTest(value=value):
                parent = _parent([_response(value)])
                sub = _sub(parent)
                sub.messages = [{'role': 'user', 'content': '任务'}]
                sub._chat_with_retry(parent._openai_client, parent._model, subagent._subagent_tools('explore'))
                self.assertEqual(sub._last_prompt_tokens, 0)
                self.assertEqual(sub._estimate_context_tokens(), context._estimate_usage_tokens(
                    sub.messages, overhead=sub._request_overhead_tokens(),
                ))

    def test_compaction_request_does_not_anchor_and_rebuild_resets_usage(self):
        parent = _parent([_response(99999)])
        sub = _sub(parent)
        sub.messages = [
            {'role': 'system', 'content': '规则'}, {'role': 'user', 'content': '任务'},
            {'role': 'assistant', 'content': '旧工作'}, {'role': 'user', 'content': '后续'},
        ]
        sub._last_prompt_tokens = 20000
        sub._anchored_message_count = 3
        sub._chat_with_retry(parent._openai_client, parent._model, None)
        self.assertEqual(sub._last_prompt_tokens, 20000)
        request = parent._openai_client.chat.completions.create.call_args.kwargs
        self.assertNotIn('tools', request)
        sub._apply_summary(2, 3, '已读完')
        self.assertEqual((sub._last_prompt_tokens, sub._anchored_message_count), (0, 0))

    def test_subagent_cache_selection_precedes_stripping(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                parent = _parent([_response(1000)])
                parent._prompt_caching = enabled
                sub = _sub(parent)
                sub.messages = [
                    {'role': 'system', 'content': '规则'}, {'role': 'user', 'content': '任务'},
                    {'role': 'user', 'content': '压缩指令', '_compact_instruction': True},
                ]
                original = copy.deepcopy(sub.messages)
                sub._chat_with_retry(parent._openai_client, parent._model, None)
                request = parent._openai_client.chat.completions.create.call_args.kwargs
                self.assertEqual([_has_cache(m) for m in request['messages']], [enabled, enabled, False])
                self.assertNotIn('_compact_instruction', request['messages'][-1])
                self.assertNotIn('tools', request)
                self.assertEqual(sub.messages, original)

    def test_sdk_tool_parameters_are_normalized_and_counted(self):
        raw_args = json.dumps({'file_key': '词典', 'note': '长参数 ' * 2000}, ensure_ascii=False)
        parent = _parent([
            _response(calls=[_call(raw_args)], content='', reasoning='先查字典'),
            _response(content='读完了'),
        ])
        sub = _sub(parent)
        with patch.object(subagent, '_subagent_handlers', return_value={'read_dict': lambda _p, _a: {'content': '词典内容'}}):
            result = sub.run()
        self.assertEqual(result['status'], 'done')
        assistant = sub.messages[2]
        call = assistant['tool_calls'][0]
        self.assertIsInstance(call, dict)
        self.assertEqual(call['function']['arguments'], raw_args)
        self.assertEqual(assistant['reasoning_content'], '先查字典')
        self.assertGreater(context._estimate_message_tokens(assistant), context._estimate_text_tokens(raw_args))
        # 真正发出的第二次请求同样是字典，而不只是计数时临时转换。
        request = parent._openai_client.chat.completions.create.call_args.kwargs
        self.assertEqual(request['messages'][2]['tool_calls'][0], call)
        self.assertEqual(request['messages'][3]['tool_call_id'], 'call-1')

    def test_retry_only_anchors_successful_response(self):
        parent = _parent([RuntimeError('temporary'), _response(12000)])
        sub = _sub(parent)
        sub.messages = [{'role': 'user', 'content': '任务'}]
        with (
            patch.object(subagent, '_classify_llm_error', return_value={
                'retriable': True, 'code': 'TEST', 'message': 'temporary', 'status': 503,
            }),
            patch.object(subagent, '_llm_retry_delay_ms', return_value=0),
        ):
            sub._chat_with_retry(parent._openai_client, parent._model, subagent._subagent_tools('explore'))
        self.assertEqual(sub._last_prompt_tokens, 12000)
        self.assertEqual(sub._anchored_message_count, 1)
        self.assertEqual(parent._openai_client.chat.completions.create.call_count, 2)


if __name__ == '__main__':
    unittest.main()
