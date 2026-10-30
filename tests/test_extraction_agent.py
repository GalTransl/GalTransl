import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from GalTransl.Extraction.environment import workspace_path
from GalTransl.Extraction.runtime import ExtractionRuntime, model_config
from GalTransl.Extraction.store import SessionStore


@pytest.fixture
def extraction(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    store = SessionStore(tmp_path / "sessions")
    session = store.create(str(game))
    return store, session, ExtractionRuntime(store)


def test_shared_output_and_scoped_paths(extraction):
    store, session, _ = extraction
    assert session['permission_mode'] == 'auto'
    root = Path(session["workspace"])
    assert root == Path(session["game_dir"]) / "game_extract"
    saved = root / "gt_input" / "existing.json"
    saved.write_text("existing extraction", encoding="utf-8")
    (root / "config.yaml").write_text("# existing project", encoding="utf-8")
    second = store.create(session["game_dir"])
    third = store.create(session["game_dir"], session["workspace"])
    assert second["workspace"] == third["workspace"] == session["workspace"]
    assert len({row["id"] for row in (session, second, third)}) == 3
    assert saved.read_text(encoding="utf-8") == "existing extraction"
    assert (root / "config.yaml").read_text(encoding="utf-8") == "# existing project"
    assert not root.with_name("game_extract_2").exists()
    for row in (session, second, third):
        store.append(row["id"], {"seq": 1, "type": "user_message", "text": row["id"]})
        assert store.history(row["id"])["events"][0]["text"] == row["id"]
    for invalid in ("../game", "reports/../../", str(root), "C:/Windows", "reports\0"):
        with pytest.raises(ValueError):
            workspace_path(root, invalid)


def test_existing_output_without_a_session_is_reused_and_initialized(tmp_path):
    game = tmp_path / "game"
    root = game / "game_extract"
    root.mkdir(parents=True)
    (root / "saved.txt").write_text("keep", encoding="utf-8")
    session = SessionStore(tmp_path / "sessions").create(str(game))
    assert Path(session["workspace"]) == root
    assert {p.name for p in root.iterdir()} == {"saved.txt", "gt_input", "gt_output", "original", "metadata", "reports", "scripts"}
    assert (root / "saved.txt").read_text(encoding="utf-8") == "keep"


def test_output_file_collision_does_not_create_a_numbered_directory(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    output = game / "game_extract"
    output.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        SessionStore(tmp_path / "sessions").create(str(game))
    assert output.read_text(encoding="utf-8") == "keep"
    assert not (game / "game_extract_2").exists()


@pytest.fixture
def controlled_extraction(extraction, monkeypatch):
    import GalTransl.Extraction.runtime as module

    class ControlledRunner:
        def __init__(self, state, history, session, available, callback):
            self.state = state
            self.stop_event = threading.Event()
            self.finished = threading.Event()

        def run(self):
            if not self.finished.wait(5):
                raise RuntimeError("Test runner timed out")
            if self.state.first_prompt == "fail":
                raise RuntimeError("Synthetic extraction failure")

        def abort_in_flight(self):
            # Stopping is asynchronous: retain ownership until run() exits.
            pass

    monkeypatch.setattr(module, "ExtractionRunner", ControlledRunner)
    monkeypatch.setattr(module, "environment", lambda: {"ready": True})
    store, session, runtime = extraction
    payload = {"message": "extract", "profile": {"OpenAI-Compatible": {
        "tokens": [{"token": "synthetic-key", "modelName": "model"}]}}}
    yield store, session, runtime, payload
    for runner in list(runtime.runs.values()):
        runner.finished.set()
    runtime.close()


@pytest.mark.parametrize("status", ["starting", "running", "awaiting_permission", "awaiting_input", "stopping"])
def test_game_directory_is_exclusive_for_all_active_states(controlled_extraction, status):
    store, session, runtime, payload = controlled_extraction
    # Different outputs for the same game must still be mutually exclusive,
    # including older sessions whose workspaces had numeric suffixes.
    second = store.create(session["game_dir"], str(Path(session["workspace"]).with_name("game_extract_2")))
    runtime.start(session["id"], payload)
    with runtime.lock:
        store.update(session["id"], status=status)
        with pytest.raises(ValueError, match="already running for this game directory"):
            runtime.start(second["id"], payload)
        assert store.read(second["id"])["status"] == "idle"
        assert store.history(second["id"])["events"] == []
        assert not (store.directory(second["id"]) / "execution").exists()


@pytest.mark.parametrize("finish", ["complete", "stop", "fail"])
def test_game_directory_is_released_only_after_runner_finishes(controlled_extraction, finish):
    store, session, runtime, payload = controlled_extraction
    second = store.create(session["game_dir"])
    runtime.start(session["id"], {**payload, "message": "fail" if finish == "fail" else "extract"})
    runner = runtime.runs[session["id"]]
    if finish == "stop":
        runtime.stop(session["id"])
        assert store.read(session["id"])["status"] == "stopping"
        with pytest.raises(ValueError, match="already running for this game directory"):
            runtime.start(second["id"], payload)
    runner.finished.set()
    runner.thread.join(timeout=2)
    assert not runner.thread.is_alive()
    assert session["id"] not in runtime.runs
    assert store.read(session["id"])["status"] == {"complete": "idle", "stop": "stopped", "fail": "failed"}[finish]
    runtime.start(second["id"], payload)
    assert second["id"] in runtime.runs


def test_simultaneous_starts_for_same_game_only_launch_one_runner(controlled_extraction):
    store, session, runtime, payload = controlled_extraction
    second = store.create(session["game_dir"])
    gate = threading.Barrier(2)

    def start(sid):
        gate.wait(timeout=2)
        try:
            runtime.start(sid, payload)
            return "started"
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(start, [session["id"], second["id"]]))
    assert results.count("started") == 1
    assert sum("already running for this game directory" in result for result in results) == 1
    assert len(runtime.runs) == 1


def test_different_games_can_run_concurrently_with_existing_global_limit(controlled_extraction, tmp_path):
    store, session, runtime, payload = controlled_extraction
    others = []
    for name in ("other", "third"):
        game = tmp_path / name
        game.mkdir()
        others.append(store.create(str(game)))
    shared = store.create(others[0]["game_dir"], session["workspace"])
    runtime.start(session["id"], payload)
    with pytest.raises(ValueError, match="already running in this output workspace"):
        runtime.start(shared["id"], payload)
    runtime.start(others[0]["id"], payload)
    assert len(runtime.runs) == 2
    with pytest.raises(ValueError, match="At most two"):
        runtime.start(others[1]["id"], payload)


def test_delete_session_removes_only_its_history_and_runtime_caches(extraction):
    store, session, runtime = extraction
    second = store.create(session["game_dir"])
    sid = session["id"]
    output = Path(session["workspace"]) / "gt_input" / "saved.json"
    output.write_text("keep", encoding="utf-8")
    runtime._emit(sid, {"type": "notice", "text": "history"})
    runtime.session(sid)
    assert runtime.delete_session(sid) == {"deleted": sid}
    assert not store.directory(sid).exists()
    assert store.read(second["id"])["workspace"] == session["workspace"]
    assert output.read_text(encoding="utf-8") == "keep"
    assert runtime.list_sessions()["total"] == 1
    assert sid not in runtime.recovered
    for cache in (runtime.events, runtime.event_bytes, runtime.sequences, runtime.pending, runtime.live):
        assert sid not in cache
    with pytest.raises(ValueError, match="Invalid extraction session id"):
        runtime.delete_session("../outside")


def test_close_folder_preserves_history_persists_and_reopens_on_create(extraction, tmp_path):
    store, session, runtime = extraction
    second = store.create(session["game_dir"])
    game = tmp_path / "other"
    game.mkdir()
    other = store.create(str(game))
    runtime._emit(session["id"], {"type": "notice", "text": "keep history"})
    root = Path(session["workspace"])
    (root / "saved.txt").write_text("keep", encoding="utf-8")
    runtime.close_folder(session["game_dir"])
    assert [row["id"] for row in runtime.list_sessions()["sessions"]] == [other["id"]]
    restored = ExtractionRuntime(SessionStore(store.root))
    assert restored.list_sessions()["total"] == 1
    assert restored.snapshot(session["id"])["events"][0]["text"] == "keep history"
    new = restored.store.create(session["game_dir"])
    assert restored.list_sessions()["total"] == 4
    assert {row["id"] for row in restored.list_sessions()["sessions"]} == {session["id"], second["id"], other["id"], new["id"]}
    assert (root / "saved.txt").read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("status", ["starting", "running", "awaiting_permission", "awaiting_input", "stopping"])
def test_running_session_and_folder_cannot_be_closed_even_outside_page(controlled_extraction, status, monkeypatch):
    store, session, runtime, payload = controlled_extraction
    runtime.start(session["id"], payload)
    with runtime.lock:
        store.update(session["id"], status=status)
        # A coarse clock can give all sessions the same updated_at. Explicitly
        # put the 51 new sessions later so pagination never relies on glob order.
        later = store.read(session["id"])["updated_at"] + 1
        with monkeypatch.context() as clock:
            clock.setattr("GalTransl.Extraction.store.time.time", lambda: later)
            for _ in range(51):
                store.create(session["game_dir"])
        page = runtime.list_sessions()
        assert session["id"] not in {row["id"] for row in page["sessions"]}
        assert page["running_game_dirs"] == [session["game_dir"]]
        with pytest.raises(ValueError, match="before deleting its session"):
            runtime.delete_session(session["id"])
        with pytest.raises(ValueError, match="before closing its game folder"):
            runtime.close_folder(session["game_dir"])
        assert runtime.list_sessions()["total"] == 52
        assert store.directory(session["id"]).exists()


def test_windows_junction_is_not_followed(extraction, tmp_path):
    import os
    if os.name != "nt":
        pytest.skip("Windows junction regression")
    _, session, _ = extraction
    root = Path(session["workspace"])
    outside = tmp_path / "outside"
    outside.mkdir()
    junction = root / "linked"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)], check=True, capture_output=True)
    try:
        with pytest.raises(ValueError, match="Links"):
            workspace_path(root, "linked")
    finally:
        junction.rmdir()


def test_bounded_journal_pages_and_recovery(extraction):
    store, session, runtime = extraction
    sid = session["id"]
    for seq in range(1, 251):
        store.append(sid, {"seq": seq, "type": "user_message", "text": "测试" * 1000})
    recent = store.history(sid)
    assert len(recent["events"]) == 100
    assert [e["seq"] for e in recent["events"]] == list(range(151, 251))
    previous = store.history(sid, recent["before_seq"])
    assert [e["seq"] for e in previous["events"]] == list(range(51, 151))
    store.update(sid, status="running", seq=249)
    snapshot = runtime.snapshot(sid)
    assert snapshot["session"]["status"] == "interrupted"
    assert snapshot["cursor"] == 251
    assert runtime.poll(sid, 100)["reset"]


def test_live_snapshot_restores_partial_output(extraction):
    store, session, runtime = extraction
    sid = session["id"]
    runtime._emit(sid, {"type": "assistant_start", "messageId": "a"})
    runtime._emit(sid, {"type": "assistant_delta", "messageId": "a", "text": "hello "})
    runtime._emit(sid, {"type": "assistant_delta", "messageId": "a", "text": "world"})
    snapshot = runtime.snapshot(sid)
    assert snapshot["live_events"][0]["text"] == "hello world"
    assert all(e["type"] != "assistant_delta" for e in store.history(sid)["events"])
    runtime._emit(sid, {"type": "assistant_message", "messageId": "a", "text": "hello world"})
    assert runtime.snapshot(sid)["live_events"] == []


def test_missing_user_bubbles_are_recovered_in_turn_order_once(extraction):
    from GalTransl.Agent.session_store import SessionStore as SharedStore

    store, session, runtime = extraction
    sid = session["id"]
    directory = store.directory(sid)
    shared = SharedStore(session["workspace"], sid, directory=str(directory / "execution"))
    for turn, text in enumerate(("原始问题\n第二行", "继续", "继续")):
        shared.append_event({"type": "user_message", "step": turn * 10 + 1, "message": text})
        runtime._emit(sid, {"type": "state", "status": "starting"})
        runtime._emit(sid, {"type": "state", "status": "running"})
        if turn == 1:
            # Already fixed turns must not receive a second bubble.
            runtime._emit(sid, {"type": "user_message", "text": text})
        runtime._emit(sid, {"type": "assistant_message", "messageId": str(turn), "text": f"reply {turn}"})
        runtime._emit(sid, {"type": "state", "status": "idle"})
    original = (directory / "events.jsonl").read_bytes()
    snapshot = runtime.snapshot(sid)
    events = snapshot["events"]
    assert [(event["type"], event["text"]) for event in events if "text" in event] == [
        ("user_message", "原始问题\n第二行"), ("assistant_message", "reply 0"),
        ("user_message", "继续"), ("assistant_message", "reply 1"),
        ("user_message", "继续"), ("assistant_message", "reply 2"),
    ]
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert snapshot["cursor"] == events[-1]["seq"]
    assert runtime.poll(sid, snapshot["cursor"] - 1)["reset"]
    assert (directory / "events.before-user-messages.jsonl").read_bytes() == original
    repaired = (directory / "events.jsonl").read_bytes()
    assert ExtractionRuntime(store).snapshot(sid)["events"] == events
    assert (directory / "events.jsonl").read_bytes() == repaired
    assert store.read(sid)["updated_at"] == session["updated_at"]
    runtime._emit(sid, {"type": "notice", "message": "after repair"})
    assert runtime.snapshot(sid)["events"][-1]["seq"] == snapshot["cursor"] + 1


@pytest.mark.parametrize("mismatch", ["turn-count", "existing-text", "damaged-source", "active"])
def test_user_bubble_recovery_does_not_guess_or_rewrite_active_sessions(extraction, mismatch):
    from GalTransl.Agent.session_store import SessionStore as SharedStore

    store, session, runtime = extraction
    sid = session["id"]
    directory = store.directory(sid)
    shared = SharedStore(session["workspace"], sid, directory=str(directory / "execution"))
    shared.append_event({"type": "user_message", "step": 1, "message": "original"})
    runtime._emit(sid, {"type": "state", "status": "starting"})
    if mismatch == "turn-count":
        runtime._emit(sid, {"type": "state", "status": "starting"})
    elif mismatch == "existing-text":
        runtime._emit(sid, {"type": "user_message", "text": "different"})
    elif mismatch == "damaged-source":
        with Path(shared.path).open("ab") as stream:
            stream.write(b'{"incomplete"')
    else:
        runtime.runs[sid] = object()
    original = (directory / "events.jsonl").read_bytes()
    runtime.snapshot(sid)
    assert (directory / "events.jsonl").read_bytes() == original
    assert not (directory / "events.before-user-messages.jsonl").exists()


def test_context_usage_survives_snapshot_and_compaction_invalidates_old_count(extraction):
    _, session, runtime = extraction
    sid = session["id"]
    for used in (32000, None):
        runtime._on_event(sid, {"type": "context_usage", "used_tokens": used, "window_tokens": 128000})
        event = runtime.snapshot(sid)["events"][-1]
        assert event["type"] == "context_usage"
        assert event["used_tokens"] == used
        assert event["window_tokens"] == 128000


def test_open_unconfigured_output_uses_wizard_without_writing_files(extraction):
    _, session, runtime = extraction
    root, sid = Path(session["workspace"]), session["id"]
    before = set(root.rglob("*"))
    assert runtime.import_project(sid) == {"project_dir": str(root), "config_file_name": None}
    assert set(root.rglob("*")) == before
    file = root / "gt_input" / "sample.json"
    file.write_text('[{"wrong":"synthetic"}]', encoding="utf8")
    assert runtime.import_project(sid)["config_file_name"] is None
    assert not (root / "config.yaml").exists()
    assert not (root / "transl_cache").exists()


@pytest.mark.parametrize("name", ["config.yaml", "config.yml"])
def test_open_existing_output_preserves_configuration_without_revalidating_inputs(extraction, name):
    _, session, runtime = extraction
    root, sid = Path(session["workspace"]), session["id"]
    config = root / name
    config.write_text("# preserve edits\n", encoding="utf8")
    assert runtime.import_project(sid) == {"project_dir": str(root), "config_file_name": name}
    assert config.read_text(encoding="utf8") == "# preserve edits\n"
    assert not (root / "transl_cache").exists()


def test_open_output_rejects_configuration_directory(extraction):
    _, session, runtime = extraction
    (Path(session["workspace"]) / "config.yaml").mkdir()
    with pytest.raises(ValueError, match="not a file"):
        runtime.import_project(session["id"])


def test_model_profile_uses_explicit_effort_only():
    profile = {"OpenAI-Compatible": {"tokens": [{"token": "synthetic-key", "modelName": "model", "endpoint": "http://localhost:1234/v1", "reasoningEffort": "high"}]}}
    config = model_config(profile)
    assert config["reasoningEffort"] == "high"
    assert config["apiKey"] == "synthetic-key"


def test_approval_lifecycle_and_no_duplicate_turns(extraction, monkeypatch):
    import GalTransl.Extraction.runtime as module
    store, session, runtime = extraction
    ready, finished = threading.Event(), threading.Event()

    class FakeRunner:
        def __init__(self, state, history, session, available, callback):
            self.callback = callback
            self.state = state
            self.stop_event = threading.Event()
            self.commands = []

        def run(self, *args):
            self.callback({"type": "permission_request", "requestId": "one", "toolName": "run_python", "arguments": {"code": "print('synthetic')"}})
            ready.set()
            finished.wait(3)

        def resolve_permission(self, decision):
            self.commands.append({"decision": decision})

        def apply_permission_mode(self, mode):
            self.state.permission_mode = mode

        def abort_in_flight(self):
            finished.set()

    monkeypatch.setattr(module, "ExtractionRunner", FakeRunner)
    monkeypatch.setattr(module, "environment", lambda: {"ready": True,
        "builtin_python": {"executable": "bundled-python", "args": ["--extraction-python"]},
        "python_runner_script": "runner.py", "system_pythons": [],
        "settings": {"python_path": "", "skill_dir": "skill"}})
    profile = {"OpenAI-Compatible": {"tokens": [{"token": "synthetic-key", "modelName": "model"}]}}
    payload = {"message": "Extract synthetic fixture", "profile": profile, "permission_mode": "ask"}
    sid = session["id"]
    runtime.start(sid, payload)
    try:
        assert ready.wait(2)
        assert runtime.snapshot(sid)["pending"]["requestId"] == "one"
        with pytest.raises(ValueError, match="already running"):
            runtime.start(sid, payload)
        with pytest.raises(ValueError, match="no longer pending"):
            runtime.answer(sid, {"request_id": "stale", "decision": "allow-once"})
        sidecar = runtime.runs[sid]
        runtime.answer(sid, {"request_id": "one", "decision": "deny"})
        assert sidecar.commands[-1]["decision"] == "deny"
        assert runtime.snapshot(sid)["pending"] is None
        assert "synthetic-key" not in json.dumps(store.read(sid))
    finally:
        runtime.stop(sid)
        deadline = time.monotonic() + 3
        while sid in runtime.runs and time.monotonic() < deadline:
            time.sleep(.01)
    assert store.read(sid)["status"] == "stopped"


def test_http_origin_is_local_only():
    from GalTransl.Extraction.http import trusted_request
    class Request:
        client_address = ("127.0.0.1", 1000)
        headers = {}
    request = Request()
    assert trusted_request(request)
    for origin in ("http://localhost:1420", "http://tauri.localhost", "tauri://localhost"):
        request.headers = {"Origin": origin}
        assert trusted_request(request)
    for origin in ("https://malicious.example", "http://localhost.malicious.example", "null"):
        request.headers = {"Origin": origin}
        assert not trusted_request(request)
