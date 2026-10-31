"""Filesystem-backed skill discovery, prompt loading and execution contracts."""
import threading
from pathlib import Path

import pytest

from GalTransl.Agent.models import AgentToolError
from GalTransl.Extraction.environment import builtin_python, python_runner_script
from GalTransl.Extraction.runner import ExtractionRunner, INITIAL_USER_MESSAGE_PREFIX, restore_state
from GalTransl.Extraction.skills import discover_skills, expand_skill_command, format_skills_for_prompt
from GalTransl.Extraction.store import SessionStore
from GalTransl.Extraction.tools import ExtractionTools


def write_skill(path, metadata, body="Distinct skill instructions"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{metadata}\n---\n{body}\n", encoding="utf-8")
    return path


def test_recursive_discovery_stops_at_skill_roots_and_accepts_root_markdown(tmp_path):
    write_skill(tmp_path / "nested" / "engine" / "SKILL.md", "description: Detect an engine")
    write_skill(tmp_path / "nested" / "engine" / "references" / "SKILL.md", "name: reference\ndescription: Not a separate skill")
    write_skill(tmp_path / "one-file.md", "name: one-file\ndescription: A single file skill")
    write_skill(tmp_path / ".hidden" / "SKILL.md", "description: Hidden")
    write_skill(tmp_path / "node_modules" / "engine" / "SKILL.md", "description: Dependency")
    write_skill(tmp_path / "nested" / "guide.md", "description: Reference, not root Markdown")
    skills, diagnostics = discover_skills([tmp_path, tmp_path / "missing"])
    assert {skill['name'] for skill in skills} == {'engine', 'one-file'}
    assert all(Path(skill['file_path']).is_absolute() for skill in skills)
    assert diagnostics == []


def test_selected_skill_wins_duplicates_and_same_file_is_only_registered_once(tmp_path):
    selected = write_skill(tmp_path / "selected" / "SKILL.md", "name: engine\ndescription: Selected version")
    duplicate = write_skill(tmp_path / "bundled" / "engine" / "SKILL.md", "name: engine\ndescription: Bundled version")
    skills, diagnostics = discover_skills([selected.parent, tmp_path])
    assert len(skills) == len(diagnostics) == 1
    assert skills[0]['file_path'] == str(selected.resolve())
    assert diagnostics[0]['path'] == str(duplicate)
    assert 'Duplicate skill name' in diagnostics[0]['message']


@pytest.mark.parametrize('metadata', ['', 'description: ""', 'description: 42', 'description: [broken',
                                       'name: 42\ndescription: Valid description', '- not-a-mapping'])
def test_invalid_frontmatter_is_reported_and_other_skills_are_loaded(tmp_path, metadata):
    write_skill(tmp_path / "invalid" / "SKILL.md", metadata)
    write_skill(tmp_path / "valid" / "SKILL.md", "description: Valid skill")
    skills, diagnostics = discover_skills([tmp_path])
    assert [skill['name'] for skill in skills] == ['valid']
    assert len(diagnostics) == 1
    assert Path(diagnostics[0]['path']) == tmp_path / 'invalid' / 'SKILL.md'


def test_bom_folded_yaml_prompt_escaping_and_manual_invocation(tmp_path):
    visible = write_skill(tmp_path / "visible" / "SKILL.md", 'description: >-\n  Inspect <files> &\n  "quoted paths"')
    visible.write_bytes(b'\xef\xbb\xbf' + visible.read_text(encoding='utf-8').replace('\n', '\r\n').encode('utf-8'))
    write_skill(tmp_path / "manual" / "SKILL.md", "description: Manual skill\ndisable-model-invocation: true", "Manual body")
    skills, diagnostics = discover_skills([tmp_path])
    assert diagnostics == []
    prompt = format_skills_for_prompt(skills)
    assert 'Inspect &lt;files&gt; &amp; &quot;quoted paths&quot;' in prompt
    assert '<name>manual</name>' not in prompt
    assert 'Distinct skill instructions' not in prompt
    assert 'Manual body' in expand_skill_command('/skill:manual inspect this game', skills)
    assert expand_skill_command('/skill:manual inspect this game', skills).endswith('inspect this game')
    assert 'References are relative to' in expand_skill_command('/skill:manual', skills)
    assert expand_skill_command('/skill:unknown test', skills) == '/skill:unknown test'
    assert expand_skill_command('ordinary input', skills) == 'ordinary input'


def test_registered_extra_skills_are_readable_and_importable_with_bundled_python(tmp_path):
    game, workspace = tmp_path / "game", tmp_path / "workspace"
    game.mkdir()
    workspace.mkdir()
    primary = write_skill(tmp_path / "skills" / "primary" / "SKILL.md", "description: Primary")
    extra = write_skill(tmp_path / "skills" / "extra" / "SKILL.md", "description: Extra")
    (primary.parent / 'primary_module.py').write_text("VALUE = 'primary'", encoding='utf-8')
    (extra.parent / 'extra_module.py').write_text("VALUE = 'extra'", encoding='utf-8')
    script = extra.parent / 'probe.py'
    script.write_text('from primary_module import VALUE; import extra_module; print(VALUE, extra_module.VALUE)', encoding='utf-8')
    skills, _ = discover_skills([tmp_path / 'skills'])
    available = {'skills': skills, 'builtin_python': builtin_python(),
                 'python_runner_script': python_runner_script(), 'system_pythons': []}
    tools = ExtractionTools(workspace, game, primary.parent, available, threading.Event(), lambda event: None)
    assert tools.read_path(str(extra)) == extra
    with pytest.raises(AgentToolError, match='outside'):
        tools.write_path(str(extra))
    for arguments in ({'script': str(script)}, {'code': 'import primary_module, extra_module; print(primary_module.VALUE, extra_module.VALUE)'}):
        result = tools.run_python(arguments, 'probe')
        assert result['exit_code'] == 0
        assert result['output'].strip() == 'primary extra'


def test_skill_command_is_saved_expanded_on_first_and_later_turns_with_images(tmp_path, monkeypatch):
    monkeypatch.setattr('GalTransl.Agent.runner.load_app_settings', lambda: {'billionContextEnabled': False})
    game = tmp_path / 'game'
    game.mkdir()
    skill = write_skill(tmp_path / 'manual' / 'SKILL.md', 'description: Manual\ndisable-model-invocation: true', 'Saved instruction')
    skills, _ = discover_skills([skill.parent])
    available = {'skills': skills, 'settings': {'skill_dir': str(skill.parent)}, 'builtin_python': builtin_python()}
    store = SessionStore(tmp_path / 'sessions')
    session = store.create(str(game))
    state, history = restore_state(session, store.directory(session['id']), {}, 'test', 'auto')
    runner = ExtractionRunner(state, history, session, available, lambda event: None)
    runner._persist_message({'role': 'system', 'content': runner._system_prompt()})
    runner._persist_message({'role': 'user', 'content': runner._initial_user_message('/skill:manual first task')})
    image = {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,fixture'}}
    runner._persist_message({'role': 'user', 'content': [{'type': 'text', 'text': '/skill:manual second task'}, image]})
    messages = history.load()['messages']
    assert messages[1]['content'].startswith(INITIAL_USER_MESSAGE_PREFIX)
    assert 'Saved instruction' in messages[1]['content']
    assert messages[1]['content'].endswith('first task')
    assert 'Saved instruction' in messages[2]['content'][0]['text']
    assert messages[2]['content'][1] == image
    skill.write_text('changed file', encoding='utf-8')
    assert 'Saved instruction' in state.messages[1]['content']
    assert 'Manual body' not in runner._system_prompt()
