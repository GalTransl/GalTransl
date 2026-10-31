"""Contracts for the retained custom tools, captured from the archived integration.

Regenerate fixtures with scripts/audit_extraction_tools.mjs against the unchanged
old source/dist. Node is needed only to recapture a reference, never for tests or
the shipped application. Native replacements are covered in test_extraction_native_tools.
"""
import copy
import json
import os
from pathlib import Path

import pytest

from GalTransl.Agent.core import AgentStopRequested
from GalTransl.Agent.models import AgentToolError
from GalTransl.Extraction.environment import builtin_python, python_runner_script
from GalTransl.Extraction.runner import ExtractionRunner, restore_state
from GalTransl.Extraction.store import SessionStore
from GalTransl.Extraction.tool_schemas import EXTRACTION_TOOLS

REFERENCE = json.loads((Path(__file__).parent / 'fixtures/extraction_legacy_tools.json').read_text(encoding='utf-8'))


@pytest.fixture
def fixture(tmp_path):
    game, skill = tmp_path / 'game', tmp_path / 'skill'
    game.mkdir()
    skill.mkdir()
    (skill / 'SKILL.md').write_text('Synthetic audit Skill', encoding='utf-8')
    (skill / 'audit_module.py').write_text('import sys; print("module", sys.argv[1])', encoding='utf-8')
    store = SessionStore(tmp_path / 'sessions')
    session = store.create(str(game), str(tmp_path / 'workspace'))
    available = {'settings': {'skill_dir': str(skill)}, 'builtin_python': builtin_python(),
                 'python_runner_script': python_runner_script(),
                 'system_pythons': [{'executable': builtin_python()['executable'], 'args': []}]}
    state, history = restore_state(session, store.directory(session['id']), {}, 'audit', 'ask')
    return ExtractionRunner(state, history, session, available, lambda event: None), tmp_path


@pytest.mark.parametrize('legacy', [item for item in REFERENCE['schemas'] if item['name'] == 'run_python'], ids=lambda item: item['name'])
def test_all_original_tools_and_argument_fields_are_present(legacy):
    current = next(item['function']['parameters'] for item in EXTRACTION_TOOLS
                   if item['function']['name'] == legacy['name'])
    assert current['properties'].keys() - {'reason'} == legacy['parameters']['properties'].keys()
    assert set(current.get('required', [])) == set(legacy['parameters'].get('required', []))
    for name, old in legacy['parameters']['properties'].items():
        for constraint in ('type', 'minimum', 'maximum', 'minLength', 'maxLength', 'maxItems'):
            if constraint in old:
                assert current['properties'][name].get(constraint) == old[constraint]


def test_captured_old_tool_executions(fixture, subtests):
    runner, root = fixture
    workspace = Path(runner.session['workspace'])

    def normalize(value):
        if isinstance(value, str):
            return value.replace(str(root), '<ROOT>')
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, dict):
            return {key: normalize(item).replace(os.sep, '/') if key in {'path', 'log_path'}
                    else normalize(item) for key, item in value.items()}
        return value

    for reference in REFERENCE['cases']:
        if reference['tool'] != 'run_python':
            continue
        with subtests.test(case=reference['name']):
            expected = copy.deepcopy(reference)
            args = expected['args']
            if args.get('python_path') == '<PYTHON>':
                args['python_path'] = runner.available['builtin_python']['executable']
            for name, content in reference['files'].items():
                (workspace / name).write_bytes(content.encode('utf-8'))
            approvals = []

            def approve(name, arguments):
                approvals.append((name, arguments))
                for name, content in reference.get('duringApproval', {}).items():
                    (workspace / name).write_bytes(content.encode('utf-8'))

            runner.tools.approve = approve
            runner.ask_user = lambda *args: [['fixture A\n中文']]
            value, error = None, None
            try:
                value = runner._dispatch_tool(reference['tool'], args)
            except AgentToolError as exc:
                error = str(exc)
                if reference.get('command'):
                    try:
                        error = json.loads(error)
                    except ValueError:
                        pass
            if reference.get('command'):
                for result in (value, error):
                    if isinstance(result, dict):
                        result.pop('wall_time_seconds')
                        assert (workspace / result.pop('log_path')).is_file()
                if os.name != 'nt':
                    for key in ('value', 'error'):
                        result = expected.get(key)
                        if isinstance(result, dict):
                            result['output'] = result['output'].replace('\r\n', '\n')
                            result['output_bytes'] = len(result['output'].encode('utf-8'))
            if reference['name'] in {'binary-start', 'binary-eof'}:
                # Deliberate correction: EOF has no continuation (old code looped forever).
                expected['value']['next_offset'] = None
            assert normalize(value) == expected.get('value')
            assert normalize(error) == expected.get('error')
            assert len(approvals) == expected['approvals']
            for name, content in reference['after'].items():
                if reference['name'] == 'edit-literal-dollar':
                    # Old JS String.replace expanded $&; scripts must retain literal replacement text.
                    content = '$&'.encode().hex()
                assert (workspace / name).read_bytes().hex() == content


def test_invalid_file_or_python_requests_do_not_prompt_for_approval(fixture):
    runner, _ = fixture
    approvals = []
    runner.tools.approve = lambda *args: approvals.append(args)
    cases = [
        ('write', {'path': str(Path(runner.session['game_dir']) / 'source.txt'), 'content': ''}),
        ('edit', {'path': 'scripts/missing.txt', 'edits': [{'oldText': 'x', 'newText': 'y'}]}),
        ('run_python', {'code': '', 'module': 'json'}),
        ('run_python', {'script': str(Path(runner.session['game_dir']).parent / 'outside.py')}),
        ('run_python', {'runtime': 'system', 'python_path': str(Path(runner.session['game_dir']) / 'absent.exe'), 'code': ''}),
    ]
    for name, args in cases:
        with pytest.raises(AgentToolError):
            runner._dispatch_tool(name, args)
    assert not approvals


@pytest.mark.parametrize('name,args', [
    ('write', {'path': 'scripts/stopped.txt', 'content': 'must not write'}),
    ('edit', {'path': 'scripts/stopped.txt', 'edits': [{'oldText': 'before', 'newText': 'after'}]}),
    ('run_python', {'code': 'raise AssertionError("must not run")'}),
    ('bash', {'command': 'must-not-run'}),
])
def test_stop_arriving_with_approval_prevents_mutation(fixture, name, args):
    runner, _ = fixture
    target = Path(runner.session['workspace']) / 'scripts/stopped.txt'
    target.write_text('before', encoding='utf-8')
    runner.tools.approve = lambda *args: runner.stop_event.set()
    with pytest.raises(AgentStopRequested):
        runner._dispatch_tool(name, args)
    assert target.read_text() == 'before'


def test_stopping_ask_does_not_return_a_successful_empty_answer(fixture):
    runner, _ = fixture
    def stopped(*args):
        runner.stop_event.set()
        return [None]
    runner.ask_user = stopped
    with pytest.raises(AgentStopRequested):
        runner._dispatch_tool('ask_user', {'questions': [{'question': 'Which fixture?', 'options': ['A', 'B']}]})


@pytest.mark.parametrize('mode,needs_edit,needs_command', [
    ('ask', True, True), ('accept-edits', False, True), ('auto', False, False),
])
def test_approval_modes_preserve_the_old_tool_matrix(fixture, mode, needs_edit, needs_command):
    runner, _ = fixture
    runner.state.permission_mode = mode
    approvals = []
    runner.request_permission = lambda *args: (approvals.append(args[2]) or 'allow-once', '')
    runner.tools.commands.execute = lambda *args, **kwargs: {'exit_code': 0}
    for name, args in [
        ('write', {'path': 'scripts/edit.txt', 'content': 'before'}),
        ('edit', {'path': 'scripts/edit.txt', 'edits': [{'oldText': 'before', 'newText': 'after'}]}),
        ('run_python', {'code': 'pass'}), ('bash', {'command': 'fixture'}),
    ]:
        runner._dispatch_tool(name, args)
    assert approvals == (['write', 'edit'] if needs_edit else []) + (['run_python', 'bash'] if needs_command else [])


def test_allow_turn_grants_only_the_selected_tool(fixture):
    runner, _ = fixture
    approvals = []
    runner.request_permission = lambda *args: (approvals.append(args[2]) or 'allow-session', '')
    for name in ('one.txt', 'two.txt'):
        runner._dispatch_tool('write', {'path': 'scripts/' + name, 'content': 'before'})
    runner._dispatch_tool('edit', {'path': 'scripts/one.txt', 'edits': [{'oldText': 'before', 'newText': 'after'}]})
    assert approvals == ['write', 'edit']
