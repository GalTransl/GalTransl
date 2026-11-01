"""Execution contracts shared by source runs and the frozen extraction runtime."""
import io
import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import runpy
import shutil
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
        assert "ValueError:" in result.stderr
        assert "extraction_python.py" not in result.stderr


@pytest.mark.parametrize("entry", ["run_backend.py", "extraction_python.py"])
@pytest.mark.parametrize("kind", ["code", "script", "module"])
def test_task_tracebacks_preserve_user_frames_and_exception_chains(tmp_path, entry, kind):
    code = "def inner():\n    raise ValueError('日本語のエラー')\ntry:\n    inner()\nexcept ValueError as exc:\n    raise RuntimeError('outer failure') from exc\n"
    script = tmp_path / "traceback_probe.py"
    script.write_text(code, encoding="utf-8")
    request = {kind: code if kind == "code" else str(script) if kind == "script" else "traceback_probe"}
    result = execute(request, tmp_path, entry)
    assert result.returncode == 1
    assert "extraction_python.py" not in result.stderr and "runpy" not in result.stderr
    assert "ValueError: 日本語のエラー" in result.stderr
    assert "The above exception was the direct cause" in result.stderr
    assert "RuntimeError: outer failure" in result.stderr
    assert "line 2, in inner" in result.stderr and "line 6, in <module>" in result.stderr
    assert ('"<extraction-agent>"' if kind == "code" else str(script)) in result.stderr


@pytest.mark.parametrize("entry", ["run_backend.py", "extraction_python.py"])
def test_syntax_errors_keep_source_location_without_executor_frames(tmp_path, entry):
    result = execute({"code": "if True print('bad')"}, tmp_path, entry)
    assert result.returncode == 1
    assert "SyntaxError:" in result.stderr
    assert 'File "<extraction-agent>", line 1' in result.stderr
    assert "if True print('bad')" in result.stderr and "^" in result.stderr
    assert "extraction_python.py" not in result.stderr and "Traceback" not in result.stderr


def test_import_tracebacks_keep_imported_module_frames(tmp_path):
    script = tmp_path / "broken_import.py"
    script.write_text("raise LookupError('import failure')", encoding="utf-8")
    result = execute({"code": "import broken_import"}, tmp_path)
    assert result.returncode == 1
    assert 'File "<extraction-agent>", line 1' in result.stderr
    assert str(script) in result.stderr and "raise LookupError('import failure')" in result.stderr
    assert "LookupError: import failure" in result.stderr
    assert "extraction_python.py" not in result.stderr


def test_missing_bundled_module_keeps_system_runtime_hint(tmp_path):
    result = execute({"code": "import sys; sys.frozen = True; import galtransl_synthetic_missing_dependency"}, tmp_path)
    assert result.returncode == 1
    assert "ModuleNotFoundError:" in result.stderr
    assert "runtime='system'" in result.stderr
    assert "extraction_python.py" not in result.stderr


def test_bundled_defaults_and_legacy_migration(tmp_path, monkeypatch):
    root = tmp_path / "app"
    root.mkdir()
    monkeypatch.setattr(module, "get_resource_root", lambda: root)
    monkeypatch.setattr(module, "data_root", lambda: tmp_path / "data")
    monkeypatch.setattr(module, "get_skills_dir", lambda: tmp_path / "data" / "skills")
    settings = module.load_settings()
    assert settings == {"python_path": "",
                        "skill_dir": str(tmp_path / "data" / "skills")}
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
    (skill / "SKILL.md").write_text('---\nname: synthetic\ndescription: Synthetic extraction\n---\n')
    runner = tmp_path / "extraction_python.py"
    runner.touch()
    monkeypatch.setattr(module, "get_resource_root", lambda: tmp_path)
    monkeypatch.setattr(module, "data_root", lambda: tmp_path / "data")
    monkeypatch.setattr(module, "get_skills_dir", lambda: tmp_path / "skills")
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
    from GalTransl.Extraction import skill
    source = tmp_path / "source"
    source.mkdir()
    resource_git(source, "init", "-b", "main")
    (source / "skills").mkdir()
    (source / "skills/sources.json").write_text(json.dumps({skill.SKILL_NAME: {"source": skill.SOURCE, "branch": "main"}}))
    (source / "extraction_python.py").write_text("# Synthetic adapter\n")
    resource_git(source, "add", ".")
    resource_git(source, "commit", "-m", "synthetic application")
    upstream = {"commit": "a" * 40, "status": 200, "calls": [], "files": {
        "SKILL.md": "Synthetic resource", "LICENSE": "Synthetic license"}}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            upstream["calls"].append(self.path)
            if self.path == "/latest":
                content = json.dumps({"sha": upstream["commit"]}).encode()
            else:
                output = io.BytesIO()
                with zipfile.ZipFile(output, "w") as bundle:
                    for name, value in upstream["files"].items():
                        bundle.writestr("upstream/" + name, value)
                content = output.getvalue()
            self.send_response(upstream["status"])
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(skill, "LATEST_URL", url + "/latest")
    monkeypatch.setattr(skill, "ARCHIVE_URL", url + "/archive/{commit}")
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    worker.start()
    yield source, source / "skills" / skill.SKILL_NAME, upstream
    server.shutdown()
    server.server_close()
    worker.join(timeout=2)


def test_resource_export_downloads_latest_skill_in_place(tmp_path, skill_source):
    from scripts.prepare_extraction import prepare, SKILL_NAME
    from GalTransl.Extraction.skill import RECEIPT, SOURCE
    source, skill, upstream = skill_source
    output = prepare(source, tmp_path / "export")
    assert {path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()} == {
        f"skills/{SKILL_NAME}/SKILL.md", f"skills/{SKILL_NAME}/LICENSE", "skills/sources.json", "extraction-python/runner.py"}
    (skill / "old-tool.py").write_text("obsolete")
    upstream.update(commit="b" * 40, files={"SKILL.md": "Updated Skill", "LICENSE": "license", "new-tool.py": "# New tool"})
    prepare(source, output)
    assert (skill / "SKILL.md").read_text() == "Updated Skill"
    assert not (skill / "old-tool.py").exists()
    assert (output / "skills" / SKILL_NAME / "new-tool.py").is_file()
    assert json.loads((output / "skills/sources.json").read_text())[SKILL_NAME] == {
        "source": SOURCE, "branch": "main", "commit": "b" * 40, "files": 3}
    assert json.loads((source / "skills/sources.json").read_text())[SKILL_NAME] == {"source": SOURCE, "branch": "main"}
    assert json.loads((skill / RECEIPT).read_text())["commit"] == "b" * 40
    assert not (skill / ".git").exists()
    assert set((source / "skills").iterdir()) == {skill, source / "skills/sources.json"}


def test_skill_download_handles_shallow_application_checkout(tmp_path, skill_source):
    from scripts.prepare_extraction import prepare
    source, _, upstream = skill_source
    shallow = tmp_path / "shallow"
    resource_git(tmp_path, "clone", "--depth", "1", source.as_uri(), str(shallow))
    assert resource_git(shallow, "rev-parse", "--is-shallow-repository") == "true"
    output = prepare(shallow, tmp_path / "export")
    assert (output / "extraction-python/runner.py").is_file()
    assert upstream["calls"] == ["/latest", "/archive/" + "a" * 40]


@pytest.mark.parametrize("invalid_archive", [False, True])
def test_skill_fetch_failure_preserves_existing_skill_and_export(tmp_path, skill_source, invalid_archive):
    from scripts.prepare_extraction import prepare
    from GalTransl.Extraction.skill import SkillDownloadError
    source, skill, upstream = skill_source
    output = prepare(source, tmp_path / "export")
    before = {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()}
    upstream.update(commit="b" * 40)
    if invalid_archive:
        upstream["files"] = {"SKILL.md": "Missing license"}
    else:
        upstream["status"] = 503
    with pytest.raises(SkillDownloadError):
        prepare(source, output)
    assert {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()} == before
    assert (skill / "SKILL.md").read_text() == "Synthetic resource"


def test_frozen_application_never_prepares_resources(tmp_path, monkeypatch):
    from scripts import prepare_extraction
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    with pytest.raises(RuntimeError, match="bundled Skill without updating"):
        prepare_extraction.prepare(tmp_path, tmp_path / "export")
    assert not (tmp_path / "export").exists()


@pytest.mark.parametrize("receipt", [True, False])
def test_local_preparation_uses_plain_skill_without_network(tmp_path, skill_source, receipt):
    from scripts.prepare_extraction import prepare, SKILL_NAME
    from GalTransl.Extraction.skill import RECEIPT
    source, skill, upstream = skill_source
    prepare(source, tmp_path / "export")
    if not receipt:
        (skill / RECEIPT).unlink()
    upstream["status"] = 503
    upstream["calls"].clear()
    (skill / "SKILL.md").write_text("Local development edits", encoding="utf-8")
    output = prepare(source, tmp_path / "export", update=False)
    assert (output / "skills" / SKILL_NAME / "SKILL.md").read_text() == "Local development edits"
    assert (output / "extraction-python/runner.py").is_file()
    assert upstream["calls"] == []


def test_local_preparation_without_skill_still_exports_adapter(tmp_path, skill_source):
    from scripts.prepare_extraction import prepare, SKILL_NAME
    source, skill, upstream = skill_source
    output = prepare(source, tmp_path / "export", update=False)
    assert not skill.exists()
    assert not (output / "skills" / SKILL_NAME).exists()
    assert (output / "extraction-python/runner.py").is_file()
    assert json.loads((output / "skills/sources.json").read_text())[SKILL_NAME]["files"] == 0
    assert upstream["calls"] == []


def test_resource_export_includes_additional_skills_and_their_helpers(tmp_path, skill_source):
    from scripts.prepare_extraction import prepare
    source, _, _ = skill_source
    extra = source / 'skills' / 'extra'
    extra.mkdir()
    (extra / 'SKILL.md').write_text('---\nname: extra\ndescription: Extra skill\n---\n')
    (extra / 'helper.py').write_text("print('extra')")
    (extra / 'local-output.txt').write_text('untracked')
    resource_git(source, 'add', 'skills/extra/SKILL.md', 'skills/extra/helper.py')
    output = prepare(source, tmp_path / 'export', update=False)
    assert (output / 'skills/extra/SKILL.md').read_bytes() == (extra / 'SKILL.md').read_bytes()
    assert (output / 'skills/extra/helper.py').read_bytes() == (extra / 'helper.py').read_bytes()
    assert not (output / 'skills/extra/local-output.txt').exists()
