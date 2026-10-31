"""Execution contracts shared by source runs and the frozen extraction runtime."""
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from GalTransl.Extraction import environment as module

ROOT = Path(__file__).resolve().parent.parent


def execute(request, cwd, entry="run_backend.py"):
    env = {**os.environ, "PATH": "", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    args = [sys.executable, str(ROOT / entry)]
    if entry == "run_backend.py":
        args.append("--extraction-python")
    return subprocess.run([*args, "--request-stdin"], input=json.dumps(request),
                          cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)


def test_default_code_runs_without_path_or_backend_initialization(tmp_path):
    result = execute({"code": "import sys; from pathlib import Path; assert 'GalTransl' not in sys.modules; Path('结果.txt').write_text('synthetic', encoding='utf-8'); print(sys.argv[1])",
                      "args": ["中文参数 ` $()"]}, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "中文参数 ` $()"
    assert (tmp_path / "结果.txt").read_text(encoding="utf-8") == "synthetic"


@pytest.mark.parametrize("entry", ["run_backend.py", "extraction_python.py"])
def test_scripts_modules_and_skill_imports(tmp_path, entry):
    skill = tmp_path / "测试 Skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("Synthetic fixture", encoding="utf-8")
    (skill / "synthetic_module.py").write_text("import sys; print('module', sys.argv[1])", encoding="utf-8")
    script = tmp_path / "测试 script.py"
    script.write_text("import sys; import synthetic_module; print(__file__)", encoding="utf-8")
    for request in ({"script": str(script)}, {"module": "synthetic_module"}):
        result = execute({**request, "skill_dir": str(skill), "args": ["参数"]}, tmp_path, entry)
        assert result.returncode == 0, result.stderr
        assert "module 参数" in result.stdout


def test_empty_code_errors_exit_codes_and_request_budget(tmp_path):
    assert execute({"code": ""}, tmp_path).returncode == 0
    assert execute({"code": "raise SystemExit(7)"}, tmp_path).returncode == 7
    for request in ({}, {"code": "", "module": "json"}, {"code": 3}, {"code": "pass", "args": [2]},
                    {"code": "#" * (1024 * 1024)}):
        result = execute(request, tmp_path)
        assert result.returncode != 0
        assert "Traceback" in result.stderr


def test_bundled_defaults_and_legacy_migration(tmp_path, monkeypatch):
    root = tmp_path / "app"
    root.mkdir()
    monkeypatch.setattr(module, "get_resource_root", lambda: root)
    monkeypatch.setattr(module, "data_root", lambda: tmp_path / "data")
    settings = module.load_settings()
    assert settings == {"python_path": "",
                        "skill_dir": str(root / "skills" / "galtransl-galgamereverse-skill")}
    module.atomic_json(tmp_path / "data/settings.json", {"node_path": "node", "python_path": "python",
        "skill_dir": str(root.parent / "GalTransl-GalGameReverse-Skill")})
    assert module.load_settings() == settings
    module.atomic_json(tmp_path / "data/settings.json", {"node_path": "custom-node", "python_path": "custom-python"})
    assert module.load_settings()["python_path"] == "custom-python"
    assert "node_path" not in module.load_settings()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert module.builtin_python() == {"executable": sys.executable, "args": ["--extraction-python"], "source": "bundled"}


def test_system_python_discovery_and_external_environment(monkeypatch):
    python = module.system_pythons(sys.executable)[0]
    assert os.path.normcase(python["executable"]) == os.path.normcase(sys.executable)
    assert python["version"] == ".".join(map(str, sys.version_info[:3]))
    assert python["args"] == []
    from GalTransl.Extraction.subprocess_env import external_environment
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", "private-bundle")
    monkeypatch.setenv("PYTHONHOME", "system-python")
    monkeypatch.setenv("SECRET_TEST_TOKEN", "synthetic")
    env = external_environment()
    assert all(key not in env for key in ("_PYI_APPLICATION_HOME_DIR", "PYTHONHOME", "SECRET_TEST_TOKEN"))


def test_packaging_collects_dynamic_standard_library_and_license():
    hook = runpy.run_path(str(ROOT / "packaging/hooks/hook-extraction_python.py"))
    assert {"argparse", "bz2", "sqlite3", "xml.dom.minidom", "concurrent.futures.thread", "statistics"} <= set(hook["hiddenimports"])
    assert not any(name.startswith(("tkinter", "test.", "site-packages")) for name in hook["hiddenimports"])
    assert hook["datas"] and Path(hook["datas"][0][0]).is_file()


def test_environment_uses_existing_python_without_node(tmp_path, monkeypatch):
    skill = tmp_path / "skills" / "galtransl-galgamereverse-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").touch()
    runner = tmp_path / "extraction_python.py"
    runner.touch()
    monkeypatch.setattr(module, "get_resource_root", lambda: tmp_path)
    monkeypatch.setattr(module, "data_root", lambda: tmp_path / "data")
    monkeypatch.setattr(module, "system_pythons", lambda configured: [])
    result = module.environment()
    assert result["ready"] and result["agent_engine"] == "galtransl"
    assert result["builtin_python"]["executable"] == sys.executable
    assert not any("node" in key for key in result)
    assert "node_path" not in result["settings"]


def resource_git(directory, *args):
    return subprocess.run(["git", "-C", str(directory), "-c", "user.name=Synthetic fixture",
                           "-c", "user.email=fixture@example.invalid", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def skill_source(tmp_path, monkeypatch):
    from scripts.prepare_extraction import SKILL_NAME
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    resource_git(upstream, "init", "-b", "main")
    for name in ("SKILL.md", "LICENSE"):
        (upstream / name).write_text("Synthetic resource", encoding="utf-8")
    resource_git(upstream, "add", ".")
    resource_git(upstream, "commit", "-m", "synthetic resources")
    source = tmp_path / "source"
    source.mkdir()
    resource_git(source, "init")
    resource_git(source, "submodule", "add", str(upstream), f"skills/{SKILL_NAME}")
    skill = source / "skills" / SKILL_NAME
    (source / "skills" / "sources.json").write_text(json.dumps({SKILL_NAME: {"branch": "main"}}))
    (source / "extraction_python.py").write_text("# Synthetic adapter\n")
    resource_git(source, "add", ".")
    resource_git(source, "commit", "-m", "synthetic application")
    return source, skill, upstream


def advance_skill(upstream):
    (upstream / "SKILL.md").write_text("Updated Skill", encoding="utf-8")
    (upstream / "new-tool.py").write_text("# New tool\n", encoding="utf-8")
    resource_git(upstream, "add", ".")
    resource_git(upstream, "commit", "-m", "update resources")
    return resource_git(upstream, "rev-parse", "HEAD")


def test_resource_export_follows_latest_skill_and_records_snapshot(tmp_path, skill_source):
    from scripts.prepare_extraction import prepare, SKILL_NAME
    source, skill, upstream = skill_source
    # Local outputs and Git metadata must never enter a release.
    (skill / "local-output.txt").touch()
    output = prepare(source, tmp_path / "export")
    assert {str(path.relative_to(output)).replace("\\", "/") for path in output.rglob("*") if path.is_file()} == {
        f"skills/{SKILL_NAME}/SKILL.md", f"skills/{SKILL_NAME}/LICENSE", "skills/sources.json", "extraction-python/runner.py"}
    commit = advance_skill(upstream)
    prepare(source, output)
    assert (output / "skills" / SKILL_NAME / "SKILL.md").read_text() == "Updated Skill"
    assert (output / "skills" / SKILL_NAME / "new-tool.py").is_file()
    assert json.loads((output / "skills/sources.json").read_text())[SKILL_NAME] == {
        "branch": "main", "commit": commit, "files": 3}
    assert json.loads((source / "skills/sources.json").read_text())[SKILL_NAME] == {"branch": "main"}
    assert resource_git(skill, "rev-parse", "HEAD") == commit
    assert (skill / "local-output.txt").is_file()


def test_skill_update_preserves_local_edits(tmp_path, skill_source):
    from scripts.prepare_extraction import prepare
    source, skill, upstream = skill_source
    advance_skill(upstream)
    original = resource_git(skill, "rev-parse", "HEAD")
    (skill / "SKILL.md").write_text("Uncommitted edits")
    with pytest.raises(RuntimeError, match="local changes"):
        prepare(source, tmp_path / "other-export")
    assert (skill / "SKILL.md").read_text() == "Uncommitted edits"
    assert resource_git(skill, "rev-parse", "HEAD") == original
    assert not (tmp_path / "other-export").exists()


@pytest.mark.parametrize("diverged", [False, True])
def test_skill_update_preserves_local_commits(tmp_path, skill_source, diverged):
    from scripts.prepare_extraction import prepare
    source, skill, upstream = skill_source
    (skill / "local-tool.py").write_text("# Local tool\n")
    resource_git(skill, "add", ".")
    resource_git(skill, "commit", "-m", "local work")
    original = resource_git(skill, "rev-parse", "HEAD")
    if diverged:
        advance_skill(upstream)
    with pytest.raises(RuntimeError, match="local commits or diverged history"):
        prepare(source, tmp_path / "export")
    assert resource_git(skill, "rev-parse", "HEAD") == original
    assert (skill / "local-tool.py").is_file()


def test_skill_update_initializes_missing_submodule(tmp_path, skill_source):
    from scripts.prepare_extraction import prepare, SKILL_NAME
    source, skill, upstream = skill_source
    resource_git(source, "submodule", "deinit", "--", f"skills/{SKILL_NAME}")
    assert not (skill / "SKILL.md").exists()
    commit = advance_skill(upstream)
    prepare(source, tmp_path / "export")
    assert resource_git(skill, "rev-parse", "HEAD") == commit


def test_skill_update_handles_shallow_checkout(tmp_path, skill_source):
    from scripts.prepare_extraction import prepare
    source, skill, upstream = skill_source
    commit = advance_skill(upstream)
    resource_git(skill, "fetch", "--depth", "1", "origin", "main")
    assert resource_git(skill, "rev-parse", "--is-shallow-repository") == "true"
    prepare(source, tmp_path / "export")
    assert resource_git(skill, "rev-parse", "HEAD") == commit


def test_skill_fetch_failure_preserves_existing_export(tmp_path, skill_source):
    from scripts.prepare_extraction import prepare
    source, skill, upstream = skill_source
    output = prepare(source, tmp_path / "export")
    before = {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()}
    resource_git(skill, "remote", "set-url", "origin", str(tmp_path / "missing-upstream"))
    with pytest.raises(subprocess.CalledProcessError):
        prepare(source, output)
    assert {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()} == before


def test_frozen_application_never_updates_skill(tmp_path, monkeypatch):
    from scripts import prepare_extraction
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    def unexpected_git(*args):
        pytest.fail("Packaged applications must not invoke Git")
    monkeypatch.setattr(prepare_extraction, "git", unexpected_git)
    with pytest.raises(RuntimeError, match="bundled Skill without updating"):
        prepare_extraction.prepare(tmp_path, tmp_path / "export")
    assert not (tmp_path / "export").exists()


def test_local_preparation_uses_existing_skill_without_network(tmp_path, skill_source):
    from scripts.prepare_extraction import prepare, SKILL_NAME
    source, skill, upstream = skill_source
    original = resource_git(skill, "rev-parse", "HEAD")
    advance_skill(upstream)
    (skill / "SKILL.md").write_text("Local development edits", encoding="utf-8")
    resource_git(skill, "remote", "set-url", "origin", str(tmp_path / "missing-upstream"))
    output = prepare(source, tmp_path / "export", update=False)
    assert resource_git(skill, "rev-parse", "HEAD") == original
    assert (output / "skills" / SKILL_NAME / "SKILL.md").read_text() == "Local development edits"
    assert (output / "extraction-python/runner.py").is_file()


def test_local_preparation_without_skill_still_exports_adapter(tmp_path, skill_source):
    from scripts.prepare_extraction import prepare, SKILL_NAME
    source, skill, _ = skill_source
    resource_git(source, "submodule", "deinit", "--", f"skills/{SKILL_NAME}")
    resource_git(source, "config", f"submodule.skills/{SKILL_NAME}.url", str(tmp_path / "missing-upstream"))
    output = prepare(source, tmp_path / "export", update=False)
    assert not (skill / "SKILL.md").exists()
    assert not (output / "skills" / SKILL_NAME).exists()
    assert (output / "extraction-python/runner.py").is_file()
    assert json.loads((output / "skills/sources.json").read_text())[SKILL_NAME]["files"] == 0
