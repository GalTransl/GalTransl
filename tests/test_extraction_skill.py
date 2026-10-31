"""Exercise explicit Skill management with real HTTP and isolated downloads."""
import io
import json
import threading
import zipfile
from functools import cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from GalTransl.Extraction.http import route_extraction
from GalTransl.Extraction.skill import RECEIPT, SKILL_NAME


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
    return cache(environment.system_pythons)


@pytest.fixture
def skill_server(tmp_path, monkeypatch, discover_pythons):
    from GalTransl.Extraction import environment, skill
    monkeypatch.setattr(environment, "system_pythons", discover_pythons)

    app = tmp_path / "app"
    original = app / "skills" / SKILL_NAME
    original.mkdir(parents=True)
    (original / "SKILL.md").write_text("Original Skill", encoding="utf-8")
    (original / "LICENSE").write_text("Original license", encoding="utf-8")
    (original.parent / "sources.json").write_text(json.dumps({SKILL_NAME: {"commit": "a" * 40}}))
    (app / "extraction_python.py").touch()
    data = tmp_path / "data"
    monkeypatch.setenv("GALTRANSL_RESOURCE_DIR", str(app))
    monkeypatch.setenv("GALTRANSL_DATA_DIR", str(data))
    upstream = {"commit": "b" * 40, "status": 200, "calls": [], "archive": archive({
        "upstream/SKILL.md": "Downloaded Skill", "upstream/LICENSE": "Synthetic license",
        "upstream/python/示例.py": "print('synthetic')",
    })}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if route_extraction(self, "GET"):
                return
            upstream["calls"].append(self.path)
            content = json.dumps({"sha": upstream["commit"]}).encode() if self.path == "/upstream/latest" else upstream["archive"]
            self.send_response(upstream["status"])
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
    yield url, original, data, upstream
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


def test_environment_only_inspects_local_skill(skill_server):
    url, original, _, upstream = skill_server
    status, result = api(url, "environment")
    assert status == 200 and result["ready"]
    assert result["settings"]["skill_dir"] == str(original)
    assert result["skill"] == {"installed": True, "current_commit": "a" * 40,
                               "latest_commit": "", "update_available": False}
    assert upstream["calls"] == []


@pytest.mark.parametrize("installed,current", [(True, "a" * 40), (True, "b" * 40), (False, "")])
def test_update_check_does_not_download_or_change_selection(skill_server, tmp_path, installed, current):
    url, original, _, upstream = skill_server
    selected = tmp_path / "custom"
    selected.mkdir()
    if installed:
        (selected / "SKILL.md").touch()
        (selected / RECEIPT).write_text(json.dumps({"commit": current}))
    status, result = api(url, "skill-check", {"skill_dir": str(selected)})
    assert status == 200
    assert result == {"installed": installed, "current_commit": current, "latest_commit": "b" * 40,
                      "update_available": current != "b" * 40}
    assert upstream["calls"] == ["/upstream/latest"]
    assert api(url, "environment")[1]["settings"]["skill_dir"] == str(original)


def test_install_and_update_enable_downloads_and_preserve_previous_versions(skill_server):
    url, original, data, upstream = skill_server
    status, result = api(url, "skill-install", {})
    downloaded = data / "extraction" / "skills" / SKILL_NAME / ("b" * 40)
    assert status == 200 and result["ready"]
    assert result["settings"]["skill_dir"] == str(downloaded)
    assert result["skill"]["current_commit"] == "b" * 40
    assert (downloaded / "python/示例.py").read_text() == "print('synthetic')"
    assert json.loads((downloaded / RECEIPT).read_text())["commit"] == "b" * 40
    assert (original / "SKILL.md").read_text() == "Original Skill"
    upstream["commit"] = "c" * 40
    upstream["archive"] = archive({"upstream/SKILL.md": "Updated Skill", "upstream/LICENSE": "license"})
    assert api(url, "skill-check", {})[1]["update_available"]
    status, result = api(url, "skill-install", {})
    assert status == 200 and result["skill"]["current_commit"] == "c" * 40
    assert (downloaded / "SKILL.md").read_text() == "Downloaded Skill"
    assert (Path(result["settings"]["skill_dir"]) / "SKILL.md").read_text() == "Updated Skill"
    upstream["calls"].clear()
    assert api(url, "skill-install", {})[0] == 200
    assert upstream["calls"] == ["/upstream/latest"]


@pytest.mark.parametrize("action", ["skill-check", "skill-install"])
def test_network_failures_report_errors_and_keep_the_active_skill(skill_server, action):
    url, original, _, upstream = skill_server
    upstream["status"] = 503
    status, result = api(url, action, {})
    assert status == 502 and "Could not download Skill" in result["error"]
    assert api(url, "environment")[1]["settings"]["skill_dir"] == str(original)


@pytest.mark.parametrize("content", [b"not a zip", archive({"upstream/SKILL.md": "missing license"}),
    archive({"upstream/SKILL.md": "skill", "upstream/LICENSE": "license", "upstream/../../escape": "bad"}),
    archive({"upstream/SKILL.md": "skill", "upstream/LICENSE": "license", "upstream/C:/escape": "bad"})],
    ids=["invalid-zip", "missing-license", "parent-path", "drive-path"])
def test_invalid_archives_do_not_replace_the_active_skill(skill_server, content):
    url, original, data, upstream = skill_server
    upstream["archive"] = content
    status, result = api(url, "skill-install", {})
    assert status == 502 and result["error"]
    assert api(url, "environment")[1]["settings"]["skill_dir"] == str(original)
    assert list((data / "extraction/skills" / SKILL_NAME).iterdir()) == []


@pytest.mark.parametrize("commit", ["invalid", "../../escape", None])
def test_invalid_upstream_versions_are_rejected(skill_server, commit):
    url, original, _, upstream = skill_server
    upstream["commit"] = commit
    status, result = api(url, "skill-install", {})
    assert status == 502 and "invalid Skill version" in result["error"]
    assert api(url, "environment")[1]["settings"]["skill_dir"] == str(original)
