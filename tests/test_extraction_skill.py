"""Exercise explicit Skill management with real HTTP and isolated downloads."""
import io
import json
import threading
import zipfile
from collections import deque
from functools import cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from GalTransl.Extraction.http import route_extraction
from GalTransl.Extraction.skill import RECEIPT, SKILL_NAME, SOURCE, prune_skill_versions


FRONTMATTER = f"---\nname: {SKILL_NAME}\ndescription: Synthetic extraction\n---\n"


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        for name, content in files.items():
            bundle.writestr(name, content)
    return output.getvalue()


@pytest.fixture(scope="module")
def discover_pythons():
    from GalTransl.Extraction import environment
    # These tests change Skill files only; interpreter discovery can reuse real results.
    discover = cache(environment.system_pythons)
    # Windows CI's first scan can exceed the HTTP client's five-second timeout.
    discover("")
    return discover


@pytest.fixture
def skill_server(tmp_path, monkeypatch, discover_pythons):
    from GalTransl.Extraction import environment, skill
    monkeypatch.setattr(environment, "system_pythons", discover_pythons)

    app = tmp_path / "app"
    original = app / "skills" / SKILL_NAME
    original.mkdir(parents=True)
    (original / "SKILL.md").write_text(FRONTMATTER + "Original Skill", encoding="utf-8")
    (original / "LICENSE").write_text("Original license", encoding="utf-8")
    (original.parent / "sources.json").write_text(json.dumps({SKILL_NAME: {"commit": "a" * 40}}))
    (app / "extraction_python.py").touch()
    data = tmp_path / "data"
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("GALTRANSL_RESOURCE_DIR", str(app))
    monkeypatch.setenv("GALTRANSL_DATA_DIR", str(data))
    from GalTransl.Extraction import http, runtime
    monkeypatch.setattr(http, "EXTRACTION_RUNTIME", runtime.ExtractionRuntime())
    upstream = {"commit": "b" * 40, "status": 200, "calls": [], "archive": archive({
        "upstream/SKILL.md": FRONTMATTER + "Downloaded Skill", "upstream/LICENSE": "Synthetic license",
        "upstream/python/示例.py": "print('synthetic')",
    })}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if route_extraction(self, "GET"):
                return
            upstream["calls"].append(self.path)
            content = json.dumps({"sha": upstream["commit"]}).encode() if self.path == "/upstream/latest" else upstream["archive"]
            upstream["authorization"] = self.headers.get("Authorization")
            required = upstream.get("required_authorization")
            self.send_response(403 if required and upstream["authorization"] != required else upstream["status"])
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self):
            route_extraction(self, "POST")

        def _read_json_body(self):
            return json.loads(self.rfile.read(int(self.headers["Content-Length"])))

        def _send_json(self, value, status=200):
            content = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(skill, "LATEST_URL", url + "/upstream/latest")
    monkeypatch.setattr(skill, "ARCHIVE_URL", url + "/upstream/archive/{commit}")
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
    worker.start()
    yield url, original, app, upstream
    server.shutdown()
    server.server_close()
    worker.join(timeout=2)


def api(url, action, payload=None):
    request = Request(url + "/api/extraction/" + action,
                      data=None if payload is None else json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except HTTPError as response:
        return response.code, json.load(response)


@pytest.mark.parametrize("tokens,url,authenticated", [
    ({}, "https://api.github.com/commits/main", False),
    ({"GH_TOKEN": "test-token"}, "https://api.github.com/commits/main", True),
    ({"GITHUB_TOKEN": "test-token"}, "https://api.github.com/commits/main", True),
    ({"GH_TOKEN": "test-token", "GITHUB_TOKEN": "unused-token"}, "https://api.github.com/commits/main", True),
    ({"GH_TOKEN": "", "GITHUB_TOKEN": "test-token"}, "https://api.github.com/commits/main", True),
    ({"GH_TOKEN": "test-token"}, "http://api.github.com/commits/main", False),
    ({"GH_TOKEN": "test-token"}, "https://api.github.com.example.org/commits/main", False),
])
def test_api_credentials_avoid_anonymous_rate_limit_and_stay_on_github(skill_server, monkeypatch, tokens, url, authenticated):
    from GalTransl.Extraction import skill

    local, _, _, upstream = skill_server
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    for name, value in tokens.items():
        monkeypatch.setenv(name, value)
    upstream["required_authorization"] = "Bearer test-token"
    monkeypatch.setattr(skill, "LATEST_URL", url)

    def local_request(request, timeout):
        # Route the real request headers to a local server that rejects anonymous calls.
        return urlopen(Request(local + "/upstream/latest", headers=dict(request.header_items())), timeout=timeout)

    monkeypatch.setattr(skill, "urlopen", local_request)
    if authenticated:
        assert skill.latest_commit() == upstream["commit"]
        assert upstream["authorization"] == "Bearer test-token"
        return
    with pytest.raises(skill.SkillDownloadError, match="403"):
        skill.latest_commit()
    assert upstream["authorization"] is None


def test_environment_only_inspects_local_skill(skill_server):
    url, original, _, upstream = skill_server
    status, result = api(url, "environment")
    assert status == 200 and result["ready"]
    assert result["settings"]["skill_dir"] == str(original.parent)
    assert result["skill"] == {"installed": True, "current_commit": "a" * 40,
                               "latest_commit": "", "update_available": False}
    assert upstream["calls"] == []


def test_selected_skills_root_is_saved_and_scanned_without_bundled_fallback(skill_server, tmp_path):
    url, _, _, upstream = skill_server
    selected = tmp_path / "selected-skills"
    selected.mkdir()
    status, result = api(url, "settings", {"skill_dir": str(selected)})
    assert status == 200 and result["settings"]["skill_dir"] == str(selected)
    assert not result["ready"] and not result["skill"]["installed"]
    for name in ["first", "nested/second"]:
        directory = selected / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(f"---\nname: {directory.name}\ndescription: Selected Skill\n---\n")
    result = api(url, "environment")[1]
    assert not result["ready"] and not result["skill"]["installed"]
    assert result["problems"] == ["skill_missing"]
    assert [item["name"] for item in result["skills"]] == ["first", "second"]
    assert upstream["calls"] == []


def test_download_installs_into_the_unsaved_selected_skills_root(skill_server, tmp_path):
    url, original, _, _ = skill_server
    selected = tmp_path / "selected-skills"
    selected.mkdir()
    status, result = api(url, "skill-install", {"skill_dir": str(selected)})
    assert status == 200 and result["ready"]
    assert result["settings"]["skill_dir"] == str(selected)
    assert result["skill"]["installed"] and result["skill"]["current_commit"] == "b" * 40
    assert [item["base_dir"] for item in result["skills"]] == [str(selected / SKILL_NAME)]
    assert (original / "SKILL.md").read_text() == FRONTMATTER + "Original Skill"


@pytest.mark.parametrize("mode", ["new", "queued", "active", "immediate"])
def test_missing_extraction_skill_rejects_messages_and_preserves_queues(skill_server, tmp_path, monkeypatch, mode):
    from GalTransl.Agent.models import PendingMessage
    from GalTransl.Extraction import environment, http
    from GalTransl.Extraction.runner import ExtractionRunner, restore_state

    monkeypatch.setattr("GalTransl.Agent.runner.load_app_settings", lambda: {"billionContextEnabled": False})
    url, _, _, _ = skill_server
    selected = tmp_path / "selected-skills"
    extra = selected / "extra"
    extra.mkdir(parents=True)
    (extra / "SKILL.md").write_text("---\nname: extra\ndescription: Additional Skill\n---\n")
    assert api(url, "settings", {"skill_dir": str(selected)})[0] == 200
    game = tmp_path / "game"
    game.mkdir()
    runtime = http.EXTRACTION_RUNTIME
    session = runtime.store.create(str(game))
    state, store = restore_state(session, runtime.store.directory(session["id"]), {}, "Inspect", "auto")
    runner = ExtractionRunner(state, store, session, environment.environment(), lambda event: None)
    if mode in {"active", "immediate"}:
        runtime.runs[session["id"]] = runner
    item = PendingMessage("queued", "Inspect later", [])
    if mode in {"queued", "immediate"}:
        runtime.queued[session["id"]] = deque([item])
    action = "queue-send" if mode in {"queued", "immediate"} else "message"
    status, result = api(url, action, {"session_id": session["id"], "message": "Inspect", "item_id": item.id})
    assert status == 400 and "skill_missing" in result["error"]
    assert not runner.state.pending_messages and not runner.state.immediate_message
    assert not runner.stop_event.is_set()
    if mode in {"queued", "immediate"}:
        assert list(runtime.queued[session["id"]]) == [item]
    assert runtime.store.read(session["id"])["seq"] == 0


@pytest.mark.parametrize("installed,current", [(True, "a" * 40), (True, "b" * 40), (False, "")])
def test_update_check_does_not_download_or_change_selection(skill_server, tmp_path, installed, current):
    url, original, _, upstream = skill_server
    selected = tmp_path / "custom"
    package = selected / SKILL_NAME
    package.mkdir(parents=True)
    if installed:
        (package / "SKILL.md").touch()
        (package / RECEIPT).write_text(json.dumps({"commit": current}))
    status, result = api(url, "skill-check", {"skill_dir": str(selected)})
    assert status == 200
    assert result == {"installed": installed, "current_commit": current, "latest_commit": "b" * 40,
                      "update_available": current != "b" * 40}
    assert upstream["calls"] == ["/upstream/latest"]
    assert api(url, "environment")[1]["settings"]["skill_dir"] == str(original.parent)


def test_install_and_update_enable_downloads_and_remove_previous_versions(skill_server):
    url, original, data, upstream = skill_server
    status, result = api(url, "skill-install", {})
    downloaded = data / "skills" / SKILL_NAME
    assert status == 200 and result["ready"]
    assert result["settings"]["skill_dir"] == str(downloaded.parent)
    assert result["skill"]["current_commit"] == "b" * 40
    assert (downloaded / "python/示例.py").read_text() == "print('synthetic')"
    assert json.loads((downloaded / RECEIPT).read_text())["commit"] == "b" * 40
    assert downloaded == original
    upstream["commit"] = "c" * 40
    upstream["archive"] = archive({"upstream/SKILL.md": FRONTMATTER + "Updated Skill", "upstream/LICENSE": "license"})
    assert api(url, "skill-check", {})[1]["update_available"]
    status, result = api(url, "skill-install", {})
    assert status == 200 and result["skill"]["current_commit"] == "c" * 40
    assert result["settings"]["skill_dir"] == str(downloaded.parent)
    assert not (downloaded / "python/示例.py").exists()
    assert set((data / "skills").iterdir()) == {original, original.parent / "sources.json"}
    assert (Path(result["settings"]["skill_dir"]) / SKILL_NAME / "SKILL.md").read_text() == FRONTMATTER + "Updated Skill"
    upstream["calls"].clear()
    assert api(url, "skill-install", {})[0] == 200
    assert upstream["calls"] == ["/upstream/latest"]


def test_auto_update_setting_defaults_on_and_spares_git_checkouts(skill_server):
    url, original, _, upstream = skill_server
    assert api(url, "environment")[1]["settings"]["skill_auto_update"] is True
    assert api(url, "settings", {"skill_auto_update": "no"})[0] == 400
    status, result = api(url, "settings", {"skill_auto_update": False})
    assert status == 200 and result["settings"]["skill_auto_update"] is False
    assert api(url, "environment")[1]["settings"]["skill_auto_update"] is False
    # A later path-only save keeps the switch off.
    assert api(url, "settings", {"python_path": ""})[1]["settings"]["skill_auto_update"] is False

    (original / ".git").mkdir()
    status, result = api(url, "skill-install", {"automatic": True})
    assert status == 200 and upstream["calls"] == []
    assert (original / "SKILL.md").read_text() == FRONTMATTER + "Original Skill"
    (original / ".git").rmdir()
    status, result = api(url, "skill-install", {"automatic": True})
    assert status == 200 and result["skill"]["current_commit"] == "b" * 40
    assert (original / "SKILL.md").read_text() == FRONTMATTER + "Downloaded Skill"


def test_install_removes_legacy_home_hashes_and_keeps_custom_files(skill_server):
    from GalTransl.RuntimePaths import get_user_data_dir

    url, original, app, _ = skill_server
    old_root = get_user_data_dir() / "skills" / SKILL_NAME
    previous = old_root / ("a" * 40)
    previous.mkdir(parents=True)
    (previous / "SKILL.md").write_text("Previous download")
    (previous / RECEIPT).write_text(json.dumps({"source": SOURCE, "commit": previous.name}))
    custom = app / "skills" / "extra"
    custom.mkdir()
    (custom / "SKILL.md").write_text("Custom Skill")
    status, result = api(url, "skill-install", {})
    assert status == 200 and result["settings"]["skill_dir"] == str(original.parent)
    assert not old_root.exists()
    assert (custom / "SKILL.md").read_text() == "Custom Skill"


def test_failed_update_keeps_the_previous_downloaded_version(skill_server):
    url, original, app, upstream = skill_server
    assert api(url, "skill-install", {})[0] == 200
    downloaded = app / "skills" / SKILL_NAME
    upstream.update(commit="c" * 40, status=503)
    assert api(url, "skill-install", {})[0] == 502
    assert downloaded.is_dir()
    assert api(url, "environment")[1]["settings"]["skill_dir"] == str(downloaded.parent)
    assert (original / "SKILL.md").is_file()


def test_failed_archive_routes_preserve_installed_skill_and_settings(skill_server, monkeypatch):
    from GalTransl import GitHubDownload

    url, original, _, upstream = skill_server
    upstream["archive"] = b"<html>proxy error</html>"
    monkeypatch.setattr(GitHubDownload, "download_candidates", lambda url: [url + "?proxy", url])
    status, result = api(url, "skill-install", {})
    assert status == 502 and "did not return an archive" in result["error"]
    assert upstream["calls"][0] == "/upstream/latest"
    assert len(upstream["calls"]) == 3
    assert (original / "SKILL.md").read_text() == FRONTMATTER + "Original Skill"
    assert api(url, "environment")[1]["settings"]["skill_dir"] == str(original.parent)
    assert not list(original.parent.glob(".skill-download-*"))


@pytest.mark.parametrize("layout", ["cache", "direct"])
def test_pruning_keeps_active_versions_bundled_and_custom_skills(tmp_path, layout):
    skills = tmp_path / "skills"
    versions = skills / ".downloaded" / SKILL_NAME if layout == "cache" else skills / SKILL_NAME
    versions.mkdir(parents=True)
    bundled = skills / SKILL_NAME
    bundled.mkdir(exist_ok=True)
    (bundled / "SKILL.md").write_text("Bundled Skill")
    current, active, old, custom, foreign, damaged = [versions / (letter * 40) for letter in "abcdef"]
    for directory in [current, active, old, custom, foreign, damaged]:
        directory.mkdir()
        (directory / "SKILL.md").write_text(directory.name)
    for directory in [current, active, old]:
        (directory / RECEIPT).write_text(json.dumps({"source": SOURCE, "commit": directory.name}))
    (foreign / RECEIPT).write_text(json.dumps({"source": "custom", "commit": foreign.name}))
    (damaged / RECEIPT).write_text("invalid metadata")
    prune_skill_versions(skills, [current, active / "references"])
    assert not old.exists()
    assert all(directory.is_dir() for directory in [current, active, custom, foreign, damaged])
    assert (bundled / "SKILL.md").read_text() == "Bundled Skill"
    prune_skill_versions(skills, [current])
    assert not active.exists()
    assert current.is_dir()


def test_update_waits_for_running_extraction_turns(skill_server, tmp_path):
    from GalTransl.Extraction import environment, http
    from GalTransl.Extraction.runner import ExtractionRunner, restore_state

    url, _, app, upstream = skill_server
    assert api(url, "skill-install", {})[0] == 200
    previous = app / "skills" / SKILL_NAME
    runtime = http.EXTRACTION_RUNTIME
    game = tmp_path / "game"
    game.mkdir()
    session = runtime.store.create(str(game))
    state, store = restore_state(session, runtime.store.directory(session["id"]), {}, "Check", "auto")
    runner = ExtractionRunner(state, store, session, environment.environment(), lambda event: runtime._on_event(session["id"], event))
    runtime.runs[session["id"]] = runner
    upstream.update(commit="c" * 40)
    status, result = api(url, "skill-install", {})
    assert status == 400 and "Wait for running extraction turns" in result["error"]
    assert json.loads((previous / RECEIPT).read_text())["commit"] == "b" * 40
    runtime.runs.pop(session["id"])
    status, result = api(url, "skill-install", {})
    assert status == 200 and result["settings"]["skill_dir"] == str(previous.parent)
    assert json.loads((previous / RECEIPT).read_text())["commit"] == "c" * 40


@pytest.mark.parametrize("action", ["skill-check", "skill-install"])
def test_network_failures_report_errors_and_keep_the_active_skill(skill_server, action):
    url, original, _, upstream = skill_server
    upstream["status"] = 503
    status, result = api(url, action, {})
    assert status == 502 and "Could not download Skill" in result["error"]
    assert api(url, "environment")[1]["settings"]["skill_dir"] == str(original.parent)


@pytest.mark.parametrize("content", [b"not a zip", archive({"upstream/SKILL.md": "missing license"}),
    archive({"upstream/SKILL.md": "skill", "upstream/LICENSE": "license", "upstream/../../escape": "bad"}),
    archive({"upstream/SKILL.md": "skill", "upstream/LICENSE": "license", "upstream/C:/escape": "bad"})],
    ids=["invalid-zip", "missing-license", "parent-path", "drive-path"])
def test_invalid_archives_do_not_replace_the_active_skill(skill_server, content):
    url, original, data, upstream = skill_server
    upstream["archive"] = content
    status, result = api(url, "skill-install", {})
    assert status == 502 and result["error"]
    assert api(url, "environment")[1]["settings"]["skill_dir"] == str(original.parent)
    assert (original / "SKILL.md").read_text() == FRONTMATTER + "Original Skill"
    assert set((data / "skills").iterdir()) == {original, original.parent / "sources.json"}


@pytest.mark.parametrize("commit", ["invalid", "../../escape", None])
def test_invalid_upstream_versions_are_rejected(skill_server, commit):
    url, original, _, upstream = skill_server
    upstream["commit"] = commit
    status, result = api(url, "skill-install", {})
    assert status == 502 and "invalid Skill version" in result["error"]
    assert api(url, "environment")[1]["settings"]["skill_dir"] == str(original.parent)
