"""Real release downloads, file replacement, dictionary preservation and restart."""
import ctypes
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from dataclasses import asdict
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from application_update import apply_update, read_plan
from GalTransl import AppUpdate
from GalTransl.RuntimePaths import get_active_dict_dir

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def update_server(tmp_path, monkeypatch):
    installed = tmp_path / "installed app"
    installed.mkdir()
    executable = installed / ("GalTransl Desktop.exe" if os.name == "nt" else "galtransl-desktop")
    executable.write_bytes(b"old desktop")
    for name in AppUpdate.RESOURCE_NAMES:
        (installed / name).mkdir()
        (installed / name / "existing.txt").write_text("original", encoding="utf-8")
    (installed / "Dict/common.txt").write_text("original bundled dictionary")
    (installed / "app_settings.json").write_text("custom settings")
    (installed / "my-project/gt_cache").mkdir(parents=True)
    (installed / "my-project/gt_cache/result.json").write_text("saved translations")
    monkeypatch.setenv("GALTRANSL_DESKTOP_EXECUTABLE", str(executable))
    monkeypatch.setenv("GALTRANSL_RESOURCE_DIR", str(installed))
    monkeypatch.setenv("GALTRANSL_DATA_DIR", str(tmp_path / "user data"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(AppUpdate.tempfile, "gettempdir", lambda: str(tmp_path))
    dictionary = get_active_dict_dir()
    (dictionary / "common.txt").write_text("custom global dictionary")
    (dictionary / "local.txt").write_text("user-only dictionary")
    manager = AppUpdate.UpdateManager()
    monkeypatch.setattr(AppUpdate, "APPLICATION_UPDATE", manager)
    target = AppUpdate.installation().platform
    archive_name = f"GalTransl_99.0.0_{target}.{'zip' if os.name == 'nt' else 'tar.gz'}"
    frontend_bytes = Path(os.environ["SystemRoot"], "System32", "cmd.exe").read_bytes() if os.name == "nt" else Path(sys.executable).read_bytes()
    files = {executable.name: frontend_bytes, "backend/galtransl_backend.exe" if os.name == "nt" else "backend/galtransl_backend": b"new backend",
             **{f"{name}/new.txt": b"new resource" for name in AppUpdate.RESOURCE_NAMES},
             "Dict/common.txt": b"updated dictionary", "plugins/existing.txt": b"updated plugin"}
    prefix = archive_name.removesuffix(".zip").removesuffix(".tar.gz")

    def make_archive():
        stream = io.BytesIO()
        if os.name == "nt":
            with zipfile.ZipFile(stream, "w") as bundle:
                for name, content in files.items():
                    bundle.writestr(f"{prefix}/{name}", content)
            return stream.getvalue()
        with tarfile.open(fileobj=stream, mode="w:gz") as bundle:
            for name, content in files.items():
                entry = tarfile.TarInfo(f"{prefix}/{name}")
                entry.size, entry.mode = len(content), 0o755
                bundle.addfile(entry, io.BytesIO(content))
        return stream.getvalue()

    upstream = {"archive": make_archive(), "status": 200, "digest": "", "calls": [], "manifest": False}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if AppUpdate.route_update(self, "GET", "1.0.0"):
                return
            upstream["calls"].append(self.path)
            digest = upstream["digest"] or hashlib.sha256(upstream["archive"]).hexdigest()
            if self.path == "/latest":
                asset = {"name": archive_name, "browser_download_url": url + "/downloads/" + archive_name,
                         "size": len(upstream["archive"]), "digest": None if upstream["manifest"] else "sha256:" + digest}
                checksum = {"name": "SHA256SUMS.txt", "browser_download_url": url + "/downloads/SHA256SUMS.txt"}
                content = json.dumps({"tag_name": "v99.0.0", "assets": [asset, checksum]}).encode()
            elif self.path.endswith("SHA256SUMS.txt"):
                content = f"{digest}  {archive_name}\n".encode()
            else:
                content = upstream["archive"]
            self.send_response(upstream["status"])
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self):
            AppUpdate.route_update(self, "POST", "1.0.0")

        def _read_json_body(self):
            return json.loads(self.rfile.read(int(self.headers["Content-Length"])))

        def _send_json(self, value, status=200):
            content = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(AppUpdate, "RELEASE_API", url + "/latest")
    monkeypatch.setattr(AppUpdate, "DOWNLOAD_PREFIX", url + "/downloads/")
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
    worker.start()
    yield server.server_port, installed, dictionary, manager, upstream, tmp_path
    server.shutdown()
    server.server_close()
    worker.join(timeout=2)


def api(port, action, payload=None, origin="http://localhost"):
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("GET" if payload is None else "POST", "/api/update/" + action,
                       body=None if payload is None else json.dumps(payload),
                       headers={"Content-Type": "application/json", "Origin": origin})
    response = connection.getresponse()
    result = response.status, json.load(response)
    connection.close()
    return result


def prepared(fixture, preserve=True):
    port, _, _, manager, _, temp = fixture
    status, result = api(port, "start", {"preserve_dict": preserve})
    assert status == 200, result
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = manager.status()
        if current["phase"] in {"ready", "failed"}:
            return current, temp / f"galtransl-update-{current['id']}"
        time.sleep(0.02)
    pytest.fail("Update preparation did not finish")


@pytest.mark.parametrize("preserve", [True, False])
def test_download_and_replacement_preserve_projects_and_selected_dictionaries(update_server, preserve):
    _, installed, dictionary, _, upstream, _ = update_server
    before = {path.name: path.read_bytes() for path in dictionary.iterdir()}
    state, stage = prepared(update_server, preserve)
    assert state["phase"] == "ready", state
    assert state["downloaded"] == state["total"] == len(upstream["archive"])
    assert (installed / "plugins/existing.txt").read_text() == "original"
    plan = read_plan(stage / "plan.json")
    apply_update(plan, state["id"])
    assert (installed / "plugins/existing.txt").read_text() == "updated plugin"
    assert (installed / "app_settings.json").read_text() == "custom settings"
    assert (installed / "my-project/gt_cache/result.json").read_text() == "saved translations"
    assert (dictionary / "local.txt").read_text() == "user-only dictionary"
    if preserve:
        assert {path.name: path.read_bytes() for path in dictionary.iterdir()} == before
        assert (installed / "Dict/common.txt").read_text() == "original bundled dictionary"
        assert not (installed / "Dict/new.txt").exists()
        return
    assert (dictionary / "common.txt").read_text() == "updated dictionary"
    assert (installed / "Dict/common.txt").read_text() == "updated dictionary"
    assert (dictionary / "new.txt").read_text() == "new resource"


def test_checksum_manifest_is_used_for_older_release_metadata(update_server):
    update_server[4]["manifest"] = True
    state, _ = prepared(update_server)
    assert state["phase"] == "ready", state
    assert any(path.endswith("SHA256SUMS.txt") for path in update_server[4]["calls"])


@pytest.mark.parametrize("failure", ["checksum", "archive", "network"])
def test_failed_downloads_never_change_the_installation(update_server, failure):
    _, installed, _, _, upstream, _ = update_server
    if failure == "checksum":
        upstream["digest"] = "0" * 64
    if failure == "archive":
        upstream["archive"] = b"not an archive"
    if failure == "network":
        upstream["status"] = 503
    state, _ = prepared(update_server)
    assert state["phase"] == "failed" and state["error"]
    assert (installed / "plugins/existing.txt").read_text() == "original"
    assert (installed / "Dict/common.txt").read_text() == "original bundled dictionary"


def test_updates_reject_web_origins_and_invalid_options(update_server):
    port = update_server[0]
    assert api(port, "start", {"preserve_dict": True}, "https://example.invalid")[0] == 403
    assert api(port, "start", {"preserve_dict": True}, "null")[0] == 403
    for payload in ({}, {"preserve_dict": "false"}, {"preserve_dict": 0}):
        assert api(port, "start", payload)[0] == 400
    assert update_server[4]["calls"] == []


@pytest.mark.skipif(os.name != "nt", reason="Exercises Windows executable replacement locks")
def test_locked_files_roll_back_previously_replaced_files(update_server):
    _, installed, _, _, _, _ = update_server
    state, stage = prepared(update_server, False)
    assert state["phase"] == "ready"
    plan = read_plan(stage / "plan.json")
    locked = installed / "plugins/existing.txt"
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                  ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.CreateFileW(str(locked), 0x80000000, 1, None, 3, 0, None)
    assert handle != ctypes.c_void_p(-1).value
    try:
        with pytest.raises(OSError):
            apply_update(plan, state["id"])
    finally:
        kernel.CloseHandle(handle)
    assert (installed / "GalTransl Desktop.exe").read_bytes() == b"old desktop"
    assert (installed / "Dict/common.txt").read_text() == "original bundled dictionary"
    assert locked.read_text() == "original"
    assert not list(installed.rglob("*.bak"))


def test_standalone_installer_waits_for_exit_and_restarts_without_backend_initialization(update_server):
    _, installed, _, _, _, _ = update_server
    state, stage = prepared(update_server)
    assert state["phase"] == "ready"
    result_file = Path(read_plan(stage / "plan.json").result_file)
    parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.5)"],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    installer = subprocess.Popen([sys.executable, str(ROOT / "run_backend.py"), "--apply-update",
                                  str(stage / "plan.json"), "--parent-pid", str(parent.pid)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert (installed / "plugins/existing.txt").read_text() == "original"
    parent.wait(timeout=5)
    stdout, stderr = installer.communicate(timeout=10)
    assert installer.returncode == 0, stdout + stderr
    assert (installed / "plugins/existing.txt").read_text() == "updated plugin"
    assert json.loads(result_file.read_text())["phase"] == "completed"


def test_update_plan_rejects_paths_outside_the_payload(update_server):
    state, stage = prepared(update_server)
    assert state["phase"] == "ready"
    plan = read_plan(stage / "plan.json")
    invalid = {**asdict(plan), "files": ["../outside.txt"]}
    (stage / "plan.json").write_text(json.dumps(invalid))
    with pytest.raises(ValueError, match="Invalid application update path"):
        read_plan(stage / "plan.json")
