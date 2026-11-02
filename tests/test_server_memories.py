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

    def request(project_dir="", payload=None, origin="http://tauri.localhost", action="delete", agent="translation"):
        url = base + "/" + action if payload is not None else base + "?" + urlencode({"project_dir": project_dir, "agent": agent})
        body = json.dumps({"agent": agent, **payload} if isinstance(payload, dict) else payload).encode() if payload is not None else None
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


@pytest.mark.parametrize("action", ["list", "delete", "save"])
def test_external_origin_cannot_manage_memories(memory_api, action):
    project, request = memory_api
    with closing(MnemonSession(project)) as memory:
        saved = memory.call("mnemon_remember", {"content": "私有偏好", "scope": "global"})
        with pytest.raises(HTTPError) as error:
            request(payload={"id": saved["id"], "content": "被修改的偏好"} if action != "list" else None,
                action=action, origin="https://example.com")
        assert error.value.code == 403
        error.value.close()
        assert len(memory.global_store.entries()) == 1


@pytest.mark.parametrize("scope", ["global", "project"])
def test_save_edit_and_deduplicate_memories_without_model(memory_api, scope):
    project, request = memory_api
    with closing(MnemonSession(project)) as memory:
        snapshot = memory.projection()
        saved = request(payload={"project_dir": str(project), "scope": scope,
            "content": "  手动保存的偏好\n保留换行  ", "importance": "critical"}, action="save")["entry"]
        assert saved["scope"] == scope
        assert saved["content"] == "手动保存的偏好\n保留换行"
        assert saved["importance"] == "critical"
        edited = request(payload={"project_dir": str(project), "id": saved["id"],
            "content": "修改后的偏好"}, action="save")["entry"]
        assert edited["id"] == saved["id"]
        assert edited["created_at"] == saved["created_at"]
        assert edited["importance"] == "critical"
        assert edited["content"] == "修改后的偏好"
        assert memory.projection() == snapshot
        with closing(MnemonSession(project, "new")) as fresh:
            assert "修改后的偏好" in fresh.projection()
        duplicate = request(payload={"project_dir": str(project), "scope": scope,
            "content": "修改后的偏好", "importance": "normal"}, action="save")
        assert duplicate["duplicate"] is True
        assert duplicate["entry"]["id"] == saved["id"]
        assert duplicate["entry"]["importance"] == "normal"
        assert request(str(project))["entries"] == [duplicate["entry"]]


@pytest.mark.parametrize("payload", [
    [], {}, {"content": None}, {"content": True}, {"content": " "}, {"content": "x" * 1001},
    {"content": "规则", "scope": "invalid"}, {"content": "规则", "scope": []},
    {"content": "规则", "importance": None}, {"content": "规则", "importance": "invalid"},
    {"content": "规则", "id": ""}, {"content": "规则", "id": True},
    {"content": "规则", "id": "global-missing"}, {"content": "规则", "id": "x" * 101},
    {"content": "规则", "project_dir": []},
])
def test_invalid_save_cannot_mutate_storage(memory_api, payload):
    project, request = memory_api
    with closing(MnemonSession(project)) as memory:
        memory.call("mnemon_remember", {"content": "保留的偏好", "scope": "global"})
        with pytest.raises(HTTPError) as error:
            request(payload={"project_dir": str(project), "scope": "global", **payload} if isinstance(payload, dict) else payload,
                action="save")
        assert error.value.code == 400
        error.value.close()
        assert memory.global_store.entries()[0]["content"] == "保留的偏好"
        assert memory.store.entries() == []


def test_save_requires_project_and_cannot_move_scope_or_edit_another_project(memory_api, tmp_path):
    project, request = memory_api
    other = tmp_path / "other"
    other.mkdir()
    with closing(MnemonSession(project)) as memory:
        saved = memory.call("mnemon_remember", {"content": "项目事实"})
        global_entry = memory.call("mnemon_remember", {"content": "全局偏好", "scope": "global"})
        for payload in [
            {"scope": "project"},
            {"project_dir": str(other), "id": saved["id"]},
            {"project_dir": str(project), "id": saved["id"], "scope": "global"},
            {"project_dir": str(project), "id": global_entry["id"], "scope": "project"},
            {"project_dir": str(project), "id": saved["id"], "content": "全局偏好", "scope": "global"},
        ]:
            with pytest.raises(HTTPError) as error:
                request(payload={"content": "尝试修改", **payload}, action="save")
            assert error.value.code == 400
            error.value.close()
        assert memory.store.entries()[0]["content"] == "项目事实"
        assert memory.global_store.entries()[0]["content"] == "全局偏好"


@pytest.mark.parametrize("agent,other_agent", [("translation", "extraction"), ("extraction", "translation")])
def test_http_memories_are_isolated_by_agent_and_project(memory_api, tmp_path, agent, other_agent):
    project, request = memory_api
    other_project = tmp_path / "other"
    other_project.mkdir()
    saved = [request(payload={"project_dir": str(project), "scope": scope,
        "content": f"{agent} {scope} 记忆", "importance": "critical"}, action="save", agent=agent)["entry"]
        for scope in ("global", "project")]
    assert request(str(project), agent=other_agent)["entries"] == []
    assert {entry["id"] for entry in request(str(project), agent=agent)["entries"]} == {entry["id"] for entry in saved}
    assert request(str(other_project), agent=agent)["entries"] == [saved[0]]
    for entry in saved:
        for action in ("save", "delete"):
            with pytest.raises(HTTPError) as error:
                request(payload={"project_dir": str(project), "id": entry["id"], "content": "越界修改"},
                    action=action, agent=other_agent)
            assert error.value.code == 400
            error.value.close()
    assert {entry["content"] for entry in request(str(project), agent=agent)["entries"]} == {entry["content"] for entry in saved}
    own = request(payload={"scope": "global", "content": saved[0]["content"]}, action="save", agent=other_agent)["entry"]
    assert own["id"] != saved[0]["id"]
    assert request(agent=other_agent)["entries"] == [own]


@pytest.mark.parametrize("agent", [None, "", "other", "../translation", True, []])
@pytest.mark.parametrize("action", ["list", "save", "delete"])
def test_invalid_agent_cannot_access_memory(memory_api, agent, action):
    project, request = memory_api
    with closing(MnemonSession(project)) as memory:
        saved = memory.call("mnemon_remember", {"content": "保留的记忆", "scope": "global"})
        with pytest.raises(HTTPError) as error:
            request(payload=None if action == "list" else {"id": saved["id"], "content": "尝试修改"},
                agent=agent, action=action)
        assert error.value.code == 400
        error.value.close()
        assert memory.global_store.entries()[0]["content"] == "保留的记忆"
