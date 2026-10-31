"""Pi 1.4.0 native contracts, with captured reference probes and real processes.

No Node or Pi runtime is used by these tests. The reference JSON predates the port.
"""
import base64
import io
import json
import os
import random
import subprocess
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from GalTransl.Agent.core import AgentStopRequested
from GalTransl.Agent.images import expand_message
from GalTransl.Agent.models import AgentToolError
from GalTransl.Extraction.pi_output import OutputAccumulator
from GalTransl.Extraction.pi_text import MAX_BYTES, apply_edits, edit_diff, truncate
from GalTransl.Extraction.runner import display_arguments
from GalTransl.Extraction.tool_schemas import EXTRACTION_TOOLS
from test_extraction_tool_parity import REFERENCE, fixture


def text(result):
    return result['content'][0]['text']


@pytest.mark.parametrize('reference', [item for item in REFERENCE['native_schemas'] if item['name'] in {'read', 'bash', 'edit', 'write'}], ids=lambda item: item['name'])
def test_native_argument_schema(reference):
    schema = next(item['function']['parameters'] for item in EXTRACTION_TOOLS if item['function']['name'] == reference['name'])
    schema = {**schema, 'properties': {key: value for key, value in schema['properties'].items() if key != 'reason'}}
    def shape(spec):
        return {key: ({name: shape(value) for name, value in spec[key].items()} if key == 'properties'
                      else shape(spec[key]) if key == 'items' else spec[key])
                for key in ('type', 'required', 'properties', 'items', 'description') if key in spec}
    assert shape(schema) == shape(reference['parameters'])


@pytest.mark.parametrize('name,args', [
    ('read', {'path': 'text'}),
    ('write', {'path': 'text', 'content': 'before'}),
    ('edit', {'path': 'text', 'edits': [{'oldText': 'before', 'newText': 'after'}]}),
    ('bash', {'command': 'printf checked'}),
    ('run_python', {'code': 'print("checked")'}),
])
def test_main_tools_accept_intent_without_changing_results(fixture, name, args):
    runner, _ = fixture
    runner.tools.approve = lambda *args: None
    (runner.tools.workspace / 'text').write_text('before')
    result = runner._dispatch_tool(name, {**args, 'reason': '检查剧本文件'})
    assert result
    if name in {'read', 'write', 'edit'}:
        assert 'reason' not in result
    for invalid in (None, 1, ['intent'], '中' * 201):
        with pytest.raises(AgentToolError, match='reason'):
            runner._dispatch_tool(name, {**args, 'reason': invalid})


@pytest.mark.parametrize('name,args', [
    ('write', {'path': 'scripts/large.txt', 'content': '中' * 30000}),
    ('edit', {'path': 'scripts/large.txt', 'edits': [{'oldText': '中' * 30000, 'newText': 'changed'}]}),
    ('run_python', {'code': '#' + '中' * 19000}),
])
def test_intent_survives_bounded_call_and_approval_previews(fixture, name, args):
    runner, _ = fixture
    events = []
    runner.publish = events.append
    arguments = {**args, 'reason': '检查大文件中的剧本格式'}
    original = json.dumps(arguments)
    preview = display_arguments(arguments)
    assert preview['reason'] == arguments['reason']
    assert 'truncated' in json.dumps(preview)
    assert len(json.dumps(preview, ensure_ascii=False).encode()) < 25000
    runner._emit('tool_call', {'name': name, 'id': 'large', 'arguments': arguments})
    runner._emit('permission_request', {'name': name, 'id': 'approval', 'tool_call_id': 'large', 'arguments': arguments})
    assert [event['type'] for event in events] == ['tool_start', 'permission_request']
    assert all(event['arguments']['reason'] == arguments['reason'] for event in events)
    assert all(event['arguments'] == preview for event in events)
    assert json.dumps(arguments) == original


def test_system_prompt_carries_pi_tool_snippets_and_guidelines(fixture):
    runner, _ = fixture
    prompt = runner._system_prompt()
    assert "include reason: one short sentence in the user's language" in prompt
    assert ('Available tools:\n- read: Read file contents\n- bash: Execute bash commands (ls, grep, find, etc.)\n'
            '- edit: Make precise file edits with exact text replacement, including multiple disjoint edits in one call\n'
            '- write: Create or overwrite files\n\n'
            'In addition to the tools above, you may have access to other custom tools depending on the project.\n\n'
            'Guidelines:\n- Use bash for file operations like ls, rg, find\n'
            '- Use read to examine files instead of cat or sed.\n- Use edit for precise changes (edits[].oldText must match exactly)\n') in prompt
    assert '- Keep edits[].oldText as small as possible while still being unique in the file. Do not pad with large unchanged regions.\n' \
           '- Use write only for new files or complete rewrites.\n- Be concise in your responses\n- Show file paths clearly when working with files' in prompt


def test_only_native_replacements_are_registered(fixture):
    runner, _ = fixture
    names = {item['function']['name'] for item in runner._tool_definitions()}
    assert names == {'read', 'bash', 'edit', 'write', 'run_python', 'ask_user', 'read_history_archive'}
    for old in ('read_file', 'run_shell', 'edit_file', 'write_file', 'list_files'):
        assert not hasattr(runner.tools, old)
        with pytest.raises(AgentToolError, match='Unknown'):
            runner._dispatch_tool(old, {})


def test_ask_user_shares_translation_question_contract():
    from GalTransl.Agent.tool_schemas import AGENT_TOOLS
    shared = next(item['function']['parameters'] for item in AGENT_TOOLS if item['function']['name'] == 'ask_user')
    extraction = next(item['function']['parameters'] for item in EXTRACTION_TOOLS if item['function']['name'] == 'ask_user')
    assert extraction['properties'] == shared['properties']
    assert extraction['required'] == shared['required']


def test_old_saved_system_instruction_uses_native_read_without_rewriting_history(fixture):
    runner, _ = fixture
    old = {'role': 'system', 'content': 'Before extraction, read_file, following next_line until done: SKILL.md'}
    runner.state.messages = [old, {'role': 'user', 'content': 'Continue'}]
    projected = runner._messages_for_request()
    assert 'read, following offset continuation notices until done' in projected[0]['content']
    assert 'read_file' in runner.state.messages[0]['content']


@pytest.mark.parametrize('probe', REFERENCE['native_probes'], ids=lambda probe: probe['name'])
def test_captured_native_edit_results_and_bytes(fixture, probe):
    runner, _ = fixture
    runner.tools.approve = lambda *args: None
    path = f"scripts/native-{probe['name']}.txt"
    target = runner.tools.workspace / path
    target.write_bytes(probe['before'].encode('utf-8'))
    result = runner._dispatch_tool('edit', {'path': path, 'edits': probe['edits']})
    assert result == probe['result']
    assert target.read_bytes().hex() == probe['after']


@pytest.mark.parametrize('args', [
    {'path': 'x', 'offset': True}, {'path': 'x', 'offset': float('nan')},
    {'path': 'x', 'limit': '1'}, {'path': 'x', 'offset': []},
])
def test_read_rejects_invalid_arguments(fixture, args):
    runner, _ = fixture
    with pytest.raises(AgentToolError):
        runner._dispatch_tool('read', args)


def test_text_read_offsets_utf8_bom_crlf_and_no_legacy_size_limit(fixture):
    runner, _ = fixture
    path = runner.tools.workspace / 'text'
    path.write_bytes('\ufeff甲\r\n乙\n'.encode() + b'\xff')
    assert text(runner._dispatch_tool('read', {'path': 'text'})) == '\ufeff甲\r\n乙\n�'
    assert text(runner._dispatch_tool('read', {'path': '@text', 'offset': 2, 'limit': 1})) == '乙\n\n[1 more lines in file. Use offset=3 to continue.]'
    with pytest.raises(AgentToolError, match=r'Offset 4 is beyond end of file \(3 lines total\)'):
        runner._dispatch_tool('read', {'path': 'text', 'offset': 4})
    path.write_bytes(b'x\n' * (2 * 1024 * 1024))
    result = runner._dispatch_tool('read', {'path': 'text'})
    assert result['details']['truncation']['outputLines'] == 2000
    assert 'offset=2001' in text(result)
    path.write_bytes(b'')
    assert text(runner._dispatch_tool('read', {'path': 'text'})) == ''


@pytest.mark.parametrize('value', ['中' * 18000, '😀' * 14000], ids=['cjk', 'emoji'])
def test_read_byte_truncation_and_oversized_first_line(fixture, value):
    runner, _ = fixture
    path = runner.tools.workspace / 'text'
    path.write_bytes(value.encode())
    result = runner._dispatch_tool('read', {'path': 'text'})
    assert result['details']['truncation']['firstLineExceedsLimit']
    assert text(result).startswith('[Line 1 is ')
    path.write_bytes(('first\n' + value).encode())
    result = runner._dispatch_tool('read', {'path': 'text'})
    assert text(result).startswith('first\n\n[Showing lines 1-1 of 2 (50.0KB limit).')


@pytest.mark.parametrize('fmt', ['PNG', 'JPEG', 'GIF', 'WEBP', 'BMP'])
def test_image_sniffing_without_extension_and_asset_request_roundtrip(fixture, fmt):
    runner, _ = fixture
    path = runner.tools.workspace / 'picture.bin'
    Image.new('RGB', (32, 18), 'blue').save(path, fmt)
    result = runner._dispatch_tool('read', {'path': 'picture.bin'})
    meta = result['content'][1]['image']
    assert (meta['width'], meta['height']) == (32, 18)
    assert 'data' not in result['content'][1]
    assert 'image/png' in text(result) if fmt == 'BMP' else 'Read image file' in text(result)
    content = runner._serialize_tool_result(result)
    runner._persist_message({'role': 'tool', 'tool_call_id': 'image', 'content': content})
    saved = runner.state.messages[-1]
    assert saved['_images'] == [meta] and 'base64' not in json.dumps(saved)
    projected = runner._messages_for_request()
    assert projected[-2] == {'role': 'tool', 'tool_call_id': 'image', 'content': content}
    expanded = projected[-1]
    assert expanded['role'] == 'user' and 'image' in expanded['content'][0]['text']
    url = expanded['content'][1]['image_url']['url']
    with Image.open(io.BytesIO(base64.b64decode(url.split(',')[1]))) as image:
        assert image.size == (32, 18)
    restored = runner._store.load()['messages'][-1]
    assert expand_message(restored, runner._store)['content'][1] == expanded['content'][1]
    assert runner.state.messages[-1] == saved


@pytest.mark.parametrize('strip_internal', [True, False])
@pytest.mark.parametrize('interrupted', [True, False])
def test_saved_tool_images_follow_all_replies_without_mutating_history(fixture, strip_internal, interrupted):
    runner, _ = fixture
    Image.new('RGB', (8, 4), 'blue').save(runner.tools.workspace / 'picture.png')
    result = runner._dispatch_tool('read', {'path': 'picture.png'})
    runner._persist_message({'role': 'assistant', 'content': None, 'reasoning_content': 'Inspect image', 'tool_calls': [
        {'id': ident, 'type': 'function', 'function': {'name': 'read', 'arguments': '{}'}}
        for ident in ('image', 'text')
    ]})
    runner._persist_message({'role': 'tool', 'tool_call_id': 'image', 'content': runner._serialize_tool_result(result)})
    if not interrupted:
        runner._persist_message({'role': 'tool', 'tool_call_id': 'text', 'content': 'Other tool output'})
    runner._persist_message({'role': 'user', 'content': 'Continue', '_custom': 'retained'})
    # Reproduce a restart with the old journal format, including tool _images.
    runner.state.messages = runner._store.load()['messages']
    saved = json.dumps(runner.state.messages)
    projected = runner._messages_for_request(strip_internal=strip_internal)
    assert [message['role'] for message in projected] == ['assistant', 'tool', 'tool', 'user', 'user']
    assert projected[0]['reasoning_content'] == 'Inspect image'
    assert [message['tool_call_id'] for message in projected[1:3]] == ['image', 'text']
    assert all(isinstance(message['content'], str) and '_images' not in message for message in projected[1:3])
    assert projected[3]['content'][1]['image_url']['url'].startswith('data:image/png;base64,')
    assert projected[4]['content'] == 'Continue'
    assert ('_custom' in projected[4]) is not strip_internal
    assert runner._messages_for_request(strip_internal=strip_internal) == projected
    assert json.dumps(runner.state.messages) == saved


def test_large_and_corrupt_image_handling(fixture):
    runner, _ = fixture
    path = runner.tools.workspace / 'large.png'
    Image.new('RGB', (4000, 1000), 'blue').save(path)
    result = runner._dispatch_tool('read', {'path': 'large.png'})
    meta = result['content'][1]['image']
    assert (meta['width'], meta['height']) == (2000, 500)
    assert 'Multiply coordinates by 2.00' in text(result)
    path.write_bytes(b'GIF89a' + b'garbage' * 10)
    assert 'Image omitted' in text(runner._dispatch_tool('read', {'path': 'large.png'}))


def test_valid_image_trailing_bytes_are_not_mistaken_for_text(fixture):
    runner, _ = fixture
    target = runner.tools.workspace / 'picture'
    for fmt in ('PNG', 'JPEG'):
        Image.new('RGB', (16, 8), 'blue').save(target, fmt)
        with target.open('ab') as stream:
            stream.write(b'trailing game metadata')
        result = runner._dispatch_tool('read', {'path': 'picture'})
        meta = result['content'][1]['image']
        content = runner._serialize_tool_result(result)
        runner._persist_message({'role': 'tool', 'tool_call_id': fmt, 'content': content})
        assert expand_message(runner.state.messages[-1], runner._store)['content'][1]['image_url']['url'].startswith(f"data:{meta['mime_type']};base64,")


@pytest.mark.parametrize('edits', [
    [], [{'oldText': '', 'newText': 'x'}], [{'oldText': 'a'}],
    [None], [{'oldText': True, 'newText': 'x'}],
    [{'oldText': 'a', 'newText': 'created'}, {'oldText': 'created', 'newText': 'c'}],
    [{'oldText': 'ab', 'newText': 'x'}, {'oldText': 'bc', 'newText': 'y'}],
    [{'oldText': 'abc', 'newText': 'abc'}],
])
def test_edit_validation_and_failures_are_atomic_before_approval(fixture, edits):
    runner, _ = fixture
    target = runner.tools.workspace / 'text'
    target.write_bytes(b'abc')
    approvals = []
    runner.tools.approve = lambda *args: approvals.append(args)
    with pytest.raises(AgentToolError):
        runner._dispatch_tool('edit', {'path': 'text', 'edits': edits})
    assert target.read_bytes() == b'abc' and not approvals


def test_edits_match_original_and_replacements_are_literal(fixture):
    runner, _ = fixture
    runner.tools.approve = lambda *args: None
    target = runner.tools.workspace / 'text'
    target.write_text('one two', encoding='utf-8')
    runner._dispatch_tool('edit', {'path': 'text', 'edits': [
        {'oldText': 'one', 'newText': 'two'}, {'oldText': 'two', 'newText': '$&'}]})
    assert target.read_text() == 'two $&'


def test_fuzzy_matching_preserves_unaffected_lines_and_refuses_duplicates():
    before = 'keep “original”  \nＡ “value” — now  \nkeep\u00a0space\n'
    after = apply_edits(before, [{'oldText': 'A "value" - now', 'newText': 'changed'}], 'text')
    assert after == 'keep “original”  \nchanged\nkeep\u00a0space\n'
    with pytest.raises(AgentToolError, match='Found 2 occurrences'):
        apply_edits('"value"\n“value”\n', [{'oldText': '"value"', 'newText': 'x'}], 'text')


@pytest.mark.parametrize('edits', [{'oldText': 'a', 'newText': 'b'}, '[{"oldText":"a","newText":"b"}]', '{"oldText":"a","newText":"b"}'])
def test_pi_edit_argument_preparation(fixture, edits):
    runner, _ = fixture
    runner.tools.approve = lambda *args: None
    (runner.tools.workspace / 'text').write_text('a')
    runner._dispatch_tool('edit', {'path': 'text', 'edits': edits})
    assert (runner.tools.workspace / 'text').read_text() == 'b'


def test_legacy_camelcase_edit_preparation_and_approval_conflict(fixture):
    runner, _ = fixture
    target = runner.tools.workspace / 'text'
    target.write_bytes(b'before')
    runner.tools.approve = lambda *args: target.write_bytes(b'external change')
    with pytest.raises(AgentToolError, match='changed while awaiting approval'):
        runner._dispatch_tool('edit', {'path': 'text', 'oldText': 'before', 'newText': 'after'})
    assert target.read_bytes() == b'external change'


def test_write_no_legacy_limits_and_parent_creation(fixture):
    runner, _ = fixture
    runner.tools.approve = lambda *args: None
    value = '中😀\r\n' * 300000
    result = runner._dispatch_tool('write', {'path': 'nested/deep/text', 'content': value})
    assert text(result) == 'Successfully wrote to nested/deep/text'
    assert (runner.tools.workspace / 'nested/deep/text').read_bytes() == value.encode()
    runner._dispatch_tool('write', {'path': 'nested/deep/text', 'content': ''})
    assert (runner.tools.workspace / 'nested/deep/text').read_bytes() == b''


def test_large_diff_full_evidence_and_ui_preview_are_available(fixture):
    runner, _ = fixture
    runner.tools.approve = lambda *args: None
    old, new = 'x' * 60000, 'y' * 60000
    (runner.tools.workspace / 'text').write_text(old)
    result = runner._dispatch_tool('edit', {'path': 'text', 'edits': [{'oldText': old, 'newText': new}]})
    details = result['details']
    assert 'Preview truncated' in details['diff'] and 'Preview truncated' in details['patch']
    full = json.loads(Path(details['fullDiffPath']).read_text(encoding='utf-8'))
    assert old in full['diff'] and new in full['patch']
    assert len(json.dumps(result, ensure_ascii=False).encode()) < 256 * 1024


def test_edit_has_no_legacy_file_size_limit(fixture):
    runner, _ = fixture
    runner.tools.approve = lambda *args: None
    target = runner.tools.workspace / 'text'
    target.write_bytes(b'padding\n' * 270000 + b'unique before\n')
    result = runner._dispatch_tool('edit', {'path': 'text', 'edits': [{'oldText': 'unique before', 'newText': 'unique after'}]})
    assert result['details']['firstChangedLine'] == 270001
    assert target.read_bytes().endswith(b'unique after\n')


def test_myers_diff_is_shortest_and_reconstructs_repeated_lines():
    from GalTransl.Extraction.pi_diff import line_opcodes
    rng = random.Random(17)
    for _ in range(200):
        before = [rng.choice('abcd') for _ in range(rng.randrange(20))]
        after = [rng.choice('abcd') for _ in range(rng.randrange(20))]
        opcodes = line_opcodes(before, after)
        restored, cost = [], 0
        for tag, a, b, c, d in opcodes:
            if tag == 'equal':
                assert before[a:b] == after[c:d]
            else:
                cost += b - a + d - c
            if tag != 'delete':
                restored.extend(after[c:d])
        assert restored == after
        # Independent dynamic-programming oracle for shortest insert/delete distance.
        row = list(range(len(after) + 1))
        for index, left in enumerate(before, 1):
            updated = [index]
            for other, right in enumerate(after, 1):
                updated.append(row[other - 1] if left == right else min(row[other], updated[-1]) + 1)
            row = updated
        assert cost == row[-1]


def test_unified_patch_applies_with_git_including_context_and_missing_newline(tmp_path):
    before = ''.join(f'line-{index}\n' for index in range(35)) + 'last'
    after = before.replace('line-2\n', 'inserted\nline-2\n').replace('line-20\n', '').replace('last', 'tail\n')
    target = tmp_path / 'text'
    target.write_bytes(before.encode())
    patch = tmp_path / 'change.patch'
    patch.write_bytes(edit_diff('text', before, after)['patch'].encode())
    result = subprocess.run(['git', '-c', 'core.autocrlf=false', 'apply', '--no-index', '--unsafe-paths', '-p0', str(patch)], cwd=tmp_path, capture_output=True)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    assert target.read_bytes() == after.encode()


@pytest.mark.parametrize('timeout', [0, -1, True, float('nan'), float('inf'), 2147483.648])
def test_invalid_shell_timeout_never_approves(fixture, timeout):
    runner, _ = fixture
    approvals = []
    runner.tools.approve = lambda *args: approvals.append(args)
    with pytest.raises(AgentToolError):
        runner._dispatch_tool('bash', {'command': 'echo invalid', 'timeout': timeout})
    assert not approvals


def test_bash_runs_a_real_shell_inherits_environment_and_reports_nonzero(fixture, monkeypatch):
    runner, _ = fixture
    runner.tools.approve = lambda *args: None
    monkeypatch.setenv('GALTRANSL_NATIVE_TEST', 'inherited')
    result = runner._dispatch_tool('bash', {'command': 'set -- one two; printf "%s:%s" "$2" "$GALTRANSL_NATIVE_TEST"; printf stderr >&2; exit 7'})
    assert result['isError'] is True
    assert result['structuredContent']['exit_code'] == 7
    assert text(result) == 'two:inheritedstderr\n\nCommand exited with code 7'
    assert text(runner._dispatch_tool('bash', {'command': ':'})) == '(no output)'


def test_bash_sends_non_ascii_commands_as_utf8(fixture):
    runner, _ = fixture
    runner.tools.approve = lambda *args: None
    runner._dispatch_tool('bash', {'command': "printf '中文' > chinese.txt; grep -c 中 chinese.txt"})
    assert (runner.tools.workspace / 'chinese.txt').read_bytes() == '中文'.encode('utf-8')


@pytest.mark.skipif(os.name != 'nt', reason='Windows drive-relative POSIX paths')
def test_bash_scratch_dir_lives_in_the_workspace(fixture):
    runner, _ = fixture
    runner.tools.approve = lambda *args: None
    scratch = (runner.tools.workspace / 'tmp').resolve()
    lines = text(runner._dispatch_tool('bash', {'command': 'echo "$TMPDIR"; mktemp'})).splitlines()
    assert Path(lines[0]).resolve() == scratch
    assert Path(lines[1]).parent == scratch and Path(lines[1]).is_file()


def test_bash_handles_non_ascii_names_and_output(fixture):
    runner, _ = fixture
    runner.tools.approve = lambda *args: None
    (runner.tools.workspace / '中文目录').mkdir()
    (runner.tools.workspace / '中文目录' / '日语脚本.txt').write_bytes('中文内容'.encode('utf-8'))
    assert '中文目录' in text(runner._dispatch_tool('bash', {'command': 'ls'}))
    assert text(runner._dispatch_tool('bash', {'command': "cat 中文目录/日语脚本.txt"})) == '中文内容'


def test_bash_optional_timeout_and_no_default(fixture, monkeypatch):
    runner, _ = fixture
    runner.tools.approve = lambda *args: None
    original = runner.tools.commands.execute
    seen = []
    def capture(*args, **kwargs):
        seen.append(args[3])
        return original(*args, **kwargs)
    monkeypatch.setattr(runner.tools.commands, 'execute', capture)
    assert text(runner._dispatch_tool('bash', {'command': 'printf ready'})) == 'ready'
    with pytest.raises(AgentToolError, match='Command timed out after 0.2 seconds'):
        runner._dispatch_tool('bash', {'command': 'printf before; sleep 30', 'timeout': .2})
    assert seen == [None, .2]


def test_bash_long_output_full_log_and_utf8_tail(fixture):
    runner, _ = fixture
    runner.tools.approve = lambda *args: None
    result = runner._dispatch_tool('bash', {'command': "i=0; while [ $i -lt 2105 ]; do printf '%s:中文\\n' $i; i=$((i+1)); done"})
    truncation = result['details']['truncation']
    assert truncation['outputLines'] == 2000 and truncation['totalLines'] == 2105
    assert text(result).startswith('105:中文\n') and 'Showing lines 106-2105 of 2105' in text(result)
    assert Path(result['details']['fullOutputPath']).read_bytes().startswith('0:中文'.encode())
    result = runner._dispatch_tool('bash', {'command': "printf '%011534336d' 0"})
    assert result['structuredContent']['truncated'] is True
    assert Path(result['details']['fullOutputPath']).stat().st_size == 11534336
    assert result['details']['truncation']['lastLinePartial']
    assert result['details']['truncation']['outputBytes'] <= MAX_BYTES


def test_output_accumulator_chunking_does_not_split_utf8_or_fake_line_boundaries():
    value = ('start\n' + '😀' * 30000 + '\n' + '终\n' * 2100).encode()
    accumulator = OutputAccumulator()
    for index in range(0, len(value), 7):
        accumulator.append(value[index:index + 7])
    accumulator.append(b'', final=True)
    expected = truncate(value.decode(), tail=True)
    assert accumulator.snapshot() == expected
    assert len(accumulator.tail.encode()) <= MAX_BYTES * 4


def test_output_bom_and_exact_utf8_byte_boundary():
    from GalTransl.Extraction.pi_text import format_size
    accumulator = OutputAccumulator()
    for data in (b'\xef', b'\xbb', b'\xbf', '中'.encode(), b'\n'):
        accumulator.append(data)
    accumulator.append(b'', final=True)
    assert accumulator.snapshot()['content'] == '中\n'
    assert accumulator.snapshot()['totalBytes'] == 4
    assert format_size(52480) == '51.3KB'
    exact = '😀' * (MAX_BYTES // 4)
    assert not truncate(exact)['truncated']
    assert truncate(exact + 'a')['firstLineExceedsLimit']


def test_bash_stop_kills_descendants_and_parent_exit_does_not_hang(fixture):
    runner, _ = fixture
    runner.tools.approve = lambda *args: None
    seen = threading.Event()
    runner.tools.commands.emit = lambda event: seen.set() if 'spawned' in event.get('text', '') else None
    errors = []
    def execute():
        try:
            runner._dispatch_tool('bash', {'command': '(sleep 2; touch must-not-survive) & printf spawned; sleep 30'})
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=execute, daemon=True)
    worker.start()
    assert seen.wait(5)
    runner.stop_event.set()
    runner.tools.commands.stop()
    worker.join(5)
    assert not worker.is_alive() and isinstance(errors[0], AgentStopRequested)
    assert runner.tools.commands.process is None
    time.sleep(2.1)
    assert not (runner.tools.workspace / 'must-not-survive').exists()


def test_bash_returns_without_waiting_for_background_process_and_leaves_it_running(fixture):
    runner, _ = fixture
    runner.tools.approve = lambda *args: None
    started = time.monotonic()
    result = runner._dispatch_tool('bash', {'command': '(sleep 1.5; touch survived) & printf done'})
    assert text(result) == 'done' and time.monotonic() - started < 1.2
    time.sleep(2.5)
    assert (runner.tools.workspace / 'survived').exists()


def test_bash_background_process_is_killed_by_timeout_and_stop(fixture):
    runner, _ = fixture
    runner.tools.approve = lambda *args: None
    with pytest.raises(AgentToolError, match='Command timed out'):
        runner._dispatch_tool('bash', {'command': '(sleep 1.5; touch late) & sleep 30', 'timeout': .3})
    time.sleep(2)
    assert not (runner.tools.workspace / 'late').exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows shell drive paths')
@pytest.mark.parametrize('raw, expected', [
    ('/c/Users/x/a.txt', 'C:\\Users\\x\\a.txt'), ('/mnt/d/a', 'D:\\a'), ('/cygdrive/e', 'E:\\'),
    ('/c', 'C:\\'), ('/c/', 'C:\\'), ('//server/share', '//server/share'), ('/c\\a', '/c\\a'),
    ('/usr/bin', '/usr/bin'), ('/mnt', '/mnt'), ('rel/c/x', 'rel/c/x'), ('@/c/a', 'C:\\a'),
])
def test_windows_shell_drive_paths_follow_pi(raw, expected):
    from GalTransl.Extraction.tools import native_path
    assert native_path(raw) == expected


@pytest.mark.skipif(os.name != 'nt', reason='Windows uses the bundled BusyBox shell')
def test_windows_shell_is_the_bundled_busybox_on_stdin():
    from GalTransl.Extraction.tools import bash_config
    executable, prefix = bash_config()
    assert Path(executable).name == 'busybox64u.exe' and Path(executable).is_file()
    assert prefix == ['sh', '-s']


def test_mutation_queue_serializes_across_tool_instances(fixture):
    from GalTransl.Extraction.tools import ExtractionTools
    runner, _ = fixture
    first = runner.tools
    second = ExtractionTools(first.workspace, first.game, first.skill, first.available, first.stop_event, lambda event: None)
    target = first.workspace / 'text'
    target.write_bytes(b'one')
    entered, release = threading.Event(), threading.Event()
    def approve(*args):
        entered.set()
        assert release.wait(5)
    first.approve = approve
    errors = []
    def edit(tool, old, new):
        try:
            tool.edit({'path': 'text', 'edits': [{'oldText': old, 'newText': new}]}, '')
        except Exception as exc:
            errors.append(exc)
    a = threading.Thread(target=edit, args=(first, 'one', 'two'))
    b = threading.Thread(target=edit, args=(second, 'two', 'three'))
    a.start()
    assert entered.wait(5)
    b.start()
    time.sleep(.1)
    release.set()
    a.join(5)
    b.join(5)
    assert not errors and target.read_bytes() == b'three'


@pytest.mark.parametrize('old,new,first', [('a\nb\n', 'a\n', 2), ('a\n', 'a\nb', 2), ('a', 'b\n', 1)])
def test_diff_line_numbers_and_missing_newline_marker(old, new, first):
    result = edit_diff('text', old, new)
    assert result['firstChangedLine'] == first
    assert result['patch'].startswith('--- text\n+++ text\n@@ ')
    if not old.endswith('\n') or not new.endswith('\n'):
        assert '\\ No newline at end of file' in result['patch']
