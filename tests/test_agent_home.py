"""Conversations stay in ~/.galtransl; skills are read from the application."""
import json
import runpy
from pathlib import Path

import pytest

from GalTransl import RuntimePaths
from GalTransl.Extraction import environment
from GalTransl.Extraction.store import SessionStore


@pytest.fixture
def home(tmp_path, monkeypatch):
    user = tmp_path / 'user'
    user.mkdir()
    resource = tmp_path / 'app'
    resource.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: user)
    monkeypatch.setenv('GALTRANSL_DATA_DIR', '')
    monkeypatch.setenv('GALTRANSL_APP_SETTINGS_PATH', '')
    monkeypatch.setenv('GALTRANSL_RESOURCE_DIR', str(resource))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    monkeypatch.setenv('APPDATA', str(tmp_path / 'roaming'))
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'xdg-data'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg-config'))
    monkeypatch.setattr(environment, 'system_pythons', lambda configured: [])
    return user / '.galtransl', resource


def save_file(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')


@pytest.mark.parametrize('platform', ['win32', 'linux', 'darwin'])
def test_default_home_migrates_conversations_settings_without_moving_skills(home, monkeypatch, platform):
    monkeypatch.setattr('sys.platform', platform)
    directory, resource = home
    legacy = RuntimePaths.get_legacy_user_data_dir()
    session_id = 'a' * 32
    save_file(legacy / 'extraction/sessions' / session_id / 'session.json', json.dumps({'id': session_id, 'title': 'Original session'}))
    save_file(legacy / 'extraction/sessions' / session_id / 'execution' / 'messages.jsonl', 'original history')
    save_file(legacy / 'extraction/skills/engine/version/SKILL.md', '---\nname: engine\ndescription: Engine\n---\n')
    save_file(legacy / 'extraction/settings.json', json.dumps({'skill_dir': str(legacy / 'extraction/skills/engine/version'), 'runtime_preferences_version': 3}))
    save_file(resource / 'agent_sessions/project/session.jsonl', 'translation history')
    save_file(resource / 'app_settings.json', '{"maxConcurrentJobs": 3}')
    save_file(directory / 'agent_sessions/project/session.jsonl', 'new history wins')
    assert RuntimePaths.get_user_data_dir() == directory
    assert RuntimePaths.get_app_settings_path() == directory / 'app_settings.json'
    assert json.loads((directory / 'app_settings.json').read_text()) == {'maxConcurrentJobs': 3}
    assert SessionStore().read(session_id)['title'] == 'Original session'
    assert (directory / 'extraction/sessions' / session_id / 'execution/messages.jsonl').read_text() == 'original history'
    assert (directory / 'agent_sessions/project/session.jsonl').read_text() == 'new history wins'
    assert environment.load_settings()['skill_dir'] == str(legacy / 'extraction/skills/engine/version')
    assert not (resource / 'skills/engine/version').exists()
    assert not (directory / 'skills').exists()
    namespace = runpy.run_path(str(Path(RuntimePaths.__file__).parent / 'Agent/session_store.py'))
    assert namespace['SESSIONS_ROOT'] == str(directory / 'agent_sessions')
    (directory / 'agent_sessions/project/session.jsonl').unlink()
    RuntimePaths.get_user_data_dir()
    assert not (directory / 'agent_sessions/project/session.jsonl').exists()
    assert (resource / 'agent_sessions/project/session.jsonl').read_text() == 'translation history'


def test_skills_are_read_in_place_without_seeding_user_home(home):
    directory, resource = home
    skill = resource / 'skills/galtransl-galgamereverse-skill'
    save_file(skill / 'SKILL.md', '---\nname: engine\ndescription: Detect engine\n---\nOriginal instructions')
    save_file(skill / 'helper.py', "print('helper')")
    save_file(skill / '.git', 'gitdir: private path')
    save_file(resource / 'skills/sources.json', '{}')
    assert RuntimePaths.get_skills_dir() == resource / 'skills'
    installed = skill
    assert (installed / 'helper.py').read_text() == "print('helper')"
    assert (installed / '.git').exists()
    (installed / 'SKILL.md').write_text('User instructions', encoding='utf-8')
    (installed / 'helper.py').unlink()
    RuntimePaths.get_skills_dir()
    assert (installed / 'SKILL.md').read_text() == 'User instructions'
    assert not (installed / 'helper.py').exists()
    save_file(resource / 'skills/extra/SKILL.md', '---\nname: extra\ndescription: Extra skill\n---\n')
    RuntimePaths.get_skills_dir()
    assert (resource / 'skills/extra/SKILL.md').is_file()
    assert (installed / 'SKILL.md').read_text() == 'User instructions'
    assert not directory.exists()


def test_extra_skill_can_make_environment_ready_without_fixed_extraction_package(home):
    directory, resource = home
    save_file(resource / 'skills/extra/SKILL.md', '---\nname: extra\ndescription: Extra skill\n---\n')
    save_file(resource / 'extraction_python.py', '# Adapter')
    available = environment.environment()
    assert available['ready']
    assert [skill['name'] for skill in available['skills']] == ['extra']
    assert not available['skill']['installed']
    save_file(resource / 'skills/invalid/SKILL.md', 'No description')
    assert len(environment.environment()['skill_diagnostics']) == 1


@pytest.mark.parametrize('location', ['home', 'cache'])
@pytest.mark.parametrize('installed', [True, False])
def test_saved_skill_paths_are_read_without_migration(home, location, installed):
    from GalTransl.Extraction.skill import SKILL_NAME

    directory, resource = home
    old_root = directory / 'skills' if location == 'home' else resource / 'skills' / '.downloaded'
    selected = old_root / SKILL_NAME / ('a' * 40)
    instructions = f'---\nname: {SKILL_NAME}\ndescription: Selected download\n---\n'
    save_file(selected / 'SKILL.md', instructions)
    save_file(selected / 'helper.py', 'Downloaded helper')
    bundled = resource / 'skills' / SKILL_NAME / 'SKILL.md'
    if installed:
        save_file(bundled, f'---\nname: {SKILL_NAME}\ndescription: Bundled Skill\n---\n')
    save_file(directory / 'extraction/settings.json', json.dumps({'skill_dir': str(selected)}))
    settings = environment.load_settings()
    assert settings['skill_dir'] == str(selected)
    assert json.loads((directory / 'extraction/settings.json').read_text()) == {'skill_dir': str(selected)}
    assert bundled.exists() == installed
    if installed:
        assert 'Bundled Skill' in bundled.read_text()
    assert not (resource / 'skills' / SKILL_NAME / 'helper.py').exists()
    assert (selected / 'SKILL.md').read_text() == instructions


def test_explicit_data_root_is_honored_without_migrating_unrelated_home(home, monkeypatch, tmp_path):
    directory, resource = home
    override = tmp_path / 'override'
    monkeypatch.setenv('GALTRANSL_DATA_DIR', str(override))
    assert RuntimePaths.get_user_data_dir() == override
    assert RuntimePaths.get_app_settings_path() == override / 'app_settings.json'
    assert RuntimePaths.get_skills_dir() == resource / 'skills'
    assert not directory.exists()
