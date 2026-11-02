"""Real local HTTP memory management with isolated SQLite stores."""
from contextlib import closing
from http.server import ThreadingHTTPServer
import json
import threading
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from GalTransl.Agent.mnemon import MnemonSession
from GalTransl.server import JobRegistry, build_handler


@pytest.fixture
def memory_api(tmp_path, monkeypatch):
    monkeypatch.setenv("GALTRANSL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GALTRANSL_APP_SETTINGS_PATH", str(tmp_path / "settings.json"))
    project = tmp_path / "project"
    project.mkdir()
    server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(JobRegistry()))
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}/api/agent/memories"

    def request(project_dir="", payload=None, origin="http://tauri.localhost"):
        url = base + "/delete" if payload is not None else base + "?" + urlencode({"project_dir": project_dir})
        body = json.dumps(payload).encode() if payload is not None else None
        with urlopen(Request(url, data=body, headers={"Content-Type": "application/json", "Origin": origin}), timeout=3) as response:
            return json.load(response)

    yield project, request
    server.shutdown()
    server.server_close()
    worker.join(timeout=3)


def test_list_and_delete_global_and_project_memories_without_model(memory_api, tmp_path):
    project, request = memory_api
    other = tmp_path / "other"
    other.mkdir()
    with closing(MnemonSession(project)) as memory:
        global_entry = memory.call("mnemon_remember", {"content": "共享偏好", "scope": "global", "importance": "critical"})
        local_entry = memory.call("mnemon_remember", {"content": "SJIS编码"})
        with closing(MnemonSession(other)) as second:
            second.call("mnemon_remember", {"content": "另一个项目的事实"})
        listed = request(str(project))["entries"]
        assert {entry["id"] for entry in listed} == {global_entry["id"], local_entry["id"]}
        assert all(entry["created_at"] and entry["updated_at"] for entry in listed)
        assert {entry["importance"] for entry in listed} == {"critical", "normal"}
        assert request()["entries"][0]["id"] == global_entry["id"]
        snapshot = memory.projection()
        assert request(payload={"id": global_entry["id"]})["success"]
        assert memory.projection() == snapshot
        with closing(MnemonSession(project, "new")) as fresh:
            assert global_entry["id"] not in fresh.projection()
        assert request()["entries"] == []
        assert request(payload={"project_dir": str(project), "id": local_entry["id"]})["success"]
        assert request(str(project))["entries"] == []
        assert len(request(str(other))["entries"]) == 1


@pytest.mark.parametrize("payload", [
    [], {}, {"id": None}, {"id": True}, {"id": " "}, {"id": "x" * 101},
    {"id": "mem-missing"}, {"id": "global-missing"}, {"id": "global-missing", "project_dir": []},
])
def test_invalid_delete_cannot_mutate_storage(memory_api, payload):
    project, request = memory_api
    with closing(MnemonSession(project)) as memory:
        memory.call("mnemon_remember", {"content": "保留的偏好", "scope": "global"})
        with pytest.raises(HTTPError) as error:
            request(payload=payload)
        assert error.value.code == 400
        error.value.close()
        assert len(memory.global_store.entries()) == 1


def test_project_id_from_another_project_cannot_be_deleted(memory_api, tmp_path):
    project, request = memory_api
    other = tmp_path / "other"
    other.mkdir()
    with closing(MnemonSession(project)) as memory:
        saved = memory.call("mnemon_remember", {"content": "项目事实"})
        with pytest.raises(HTTPError) as error:
            request(payload={"project_dir": str(other), "id": saved["id"]})
        assert error.value.code == 400
        error.value.close()
        assert len(memory.store.entries()) == 1


@pytest.mark.parametrize("delete", [False, True])
def test_external_origin_cannot_read_or_delete_memories(memory_api, delete):
    project, request = memory_api
    with closing(MnemonSession(project)) as memory:
        saved = memory.call("mnemon_remember", {"content": "私有偏好", "scope": "global"})
        with pytest.raises(HTTPError) as error:
            request(payload={"id": saved["id"]} if delete else None, origin="https://example.com")
        assert error.value.code == 403
        error.value.close()
        assert len(memory.global_store.entries()) == 1
