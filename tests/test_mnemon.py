"""Real SQLite stores, global sharing, project isolation and host integration."""
import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time

import pytest

from GalTransl.Agent.billion_context import BillionContextSession
from GalTransl.Agent.context import _estimate_message_tokens, _estimate_text_tokens, _restore_usage_anchor
from GalTransl.Agent.core import AgentStopRequested
from GalTransl.Agent.mnemon import MnemonSession
from GalTransl.Agent.mnemon_prompts import MNEMON_GUIDANCE
from GalTransl.Agent.models import AgentState, AgentToolError
from GalTransl.Agent.permissions import _tool_risk
from GalTransl.Agent.mnemon_store import Forget, MnemonStore, memory_directory
from GalTransl.Agent.runner import AgentRunner
from GalTransl.Agent.session_store import SessionStore
from GalTransl.AppSettings import load_app_settings, save_app_settings
from GalTransl.Extraction.runner import ExtractionRunner


@pytest.fixture(autouse=True)
def isolated_memory_data(tmp_path, monkeypatch):
    monkeypatch.setenv("GALTRANSL_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture
def memory(tmp_path):
    session = MnemonSession(tmp_path, "first", directory=tmp_path / "memory")
    yield session
    session.close()


def test_save_update_and_delete_change_storage_without_refreshing_snapshot(memory):
    created = memory.call("mnemon_remember", {"content": "日文人名保留姓氏。", "importance": "critical"})
    memory_id = created["id"]
    before = memory.store.entries()[0]
    assert memory_id in memory.projection()
    snapshot = memory.projection()
    updated = memory.call("mnemon_remember", {"id": memory_id, "content": "日文姓名保留全名。"})
    assert updated == {"success": True, "id": memory_id, "scope": "project", "importance": "critical", "updated": True}
    assert memory.store.entries()[0]["created_at"] == before["created_at"]
    assert memory.projection() == snapshot
    with closing(MnemonSession(memory.store.directory.parent, "new", directory=memory.store.directory)) as fresh:
        assert "日文姓名保留全名。" in fresh.projection()
        assert "日文人名保留姓氏。" not in fresh.projection()
    memory.call("mnemon_remember", {"id": memory_id, "content": "日文姓名保留全名。", "importance": "normal"})
    assert memory.projection() == snapshot
    assert memory.call("mnemon_recall", {"query": "日文姓名"})["results"][0]["id"] == memory_id
    memory.call("mnemon_forget", {"id": memory_id})
    assert memory.store.entries() == []
    assert memory.call("mnemon_recall", {"query": "日文姓名"})["totalMatches"] == 0
    assert memory.projection() == snapshot
    with closing(MnemonSession(memory.store.directory.parent, "new", directory=memory.store.directory)) as fresh:
        assert memory_id not in fresh.projection()


def test_duplicate_text_keeps_id_and_can_be_promoted(memory):
    first = memory.call("mnemon_remember", {"content": "工具路径 必须加引号"})
    duplicate = memory.call("mnemon_remember", {"content": " 工具路径  必须加引号 ", "importance": "critical"})
    assert duplicate["duplicate"] is True
    assert duplicate["id"] == first["id"]
    assert len(memory.store.entries()) == 1
    assert first["id"] in memory.projection()
    assert memory.call("mnemon_remember", {"content": "工具路径 必须加引号"})["importance"] == "critical"


def test_receipts_survive_reopening_and_replay_updates_and_deletes(memory):
    args = {"content": "始终使用中文回复", "importance": "critical"}
    first = memory.call("mnemon_remember", args, "save")
    assert memory.call("mnemon_remember", args, "save") == first
    update = {"id": first["id"], "content": "始终使用简体中文回复"}
    updated = memory.call("mnemon_remember", update, "update")
    assert memory.call("mnemon_remember", update, "update") == updated
    with pytest.raises(AgentToolError, match="different arguments"):
        memory.call("mnemon_remember", {**args, "content": "changed"}, "save")
    resumed = MnemonSession(memory.store.directory.parent, "first", directory=memory.store.directory)
    try:
        assert first["id"] in resumed.projection()
        assert resumed.call("mnemon_remember", update, "update") == updated
        deleted = resumed.call("mnemon_forget", {"id": first["id"]}, "delete")
        assert resumed.call("mnemon_forget", {"id": first["id"]}, "delete") == deleted
        assert resumed.store.entries() == []
    finally:
        resumed.close()


def test_failed_update_preserves_originals_and_does_not_claim_receipt(memory):
    first = memory.call("mnemon_remember", {"content": "保留中文标点"})
    second = memory.call("mnemon_remember", {"content": "保留完整姓名"})
    before = memory.store.entries()
    with pytest.raises(AgentToolError, match="duplicates"):
        memory.call("mnemon_remember", {"id": second["id"], "content": "保留中文标点"}, "correction")
    assert memory.store.entries() == before
    corrected = memory.call("mnemon_remember", {"id": second["id"], "content": "采用全角标点"}, "correction")
    assert corrected["id"] == second["id"]
    assert {entry["id"] for entry in memory.store.entries()} == {first["id"], second["id"]}


@pytest.mark.parametrize("name,args", [
    ("mnemon_remember", []),
    ("mnemon_remember", {"content": "规则", "tags": []}),
    ("mnemon_remember", {}),
    ("mnemon_remember", {"content": " "}),
    ("mnemon_remember", {"content": "中" * 1001}),
    ("mnemon_remember", {"content": "规则", "importance": "low"}),
    ("mnemon_remember", {"content": "规则", "scope": "other"}),
    ("mnemon_remember", {"content": "规则", "id": "missing"}),
    ("mnemon_recall", {"query": "test", "limit": True}),
    ("mnemon_recall", {"query": "test", "limit": 6}),
    ("mnemon_recall", {"query": ""}),
    ("mnemon_forget", {"id": "missing"}),
])
def test_invalid_calls_leave_storage_unchanged(memory, name, args):
    with pytest.raises(AgentToolError):
        memory.call(name, args)
    assert memory.store.entries() == []
    assert memory.global_store.entries() == []


def test_only_three_tools_are_exposed():
    assert MnemonSession.names == {"mnemon_recall", "mnemon_remember", "mnemon_forget"}
    assert MnemonSession.write_names == {"mnemon_remember", "mnemon_forget"}


def test_snapshot_is_bounded_and_overflow_remains_searchable(memory):
    memory.projection_limit = 60
    short = memory.call("mnemon_remember", {"content": "应显示的简短事实", "importance": "critical"})
    long_text = "编码规则" * 200
    long = memory.call("mnemon_remember", {"content": long_text, "importance": "critical"})
    memory.call("mnemon_remember", {"content": "只应按需检索的普通事实"})
    projection = memory.projection()
    assert short["id"] in projection
    assert long["id"] not in projection
    assert "普通事实" not in projection
    assert "omitted=1" in projection
    assert memory.projection() == projection
    assert len(memory.store.entries()) == 3
    assert memory.call("mnemon_recall", {"query": "编码规则"})["results"][0]["content"] == long_text


def test_small_window_bounds_snapshot_without_truncating_entries(tmp_path):
    session = MnemonSession(tmp_path, directory=tmp_path / "memory", context_window=8192)
    try:
        for index in range(6):
            session.call("mnemon_remember", {"content": f"常驻规则{index}" + "日本語" * 100, "importance": "critical"})
        assert _estimate_text_tokens(session.projection()) <= 8192 // 16
        assert len(session.store.entries()) == 6
    finally:
        session.close()


def test_recall_cjk_and_english_has_no_query_quota_or_stale_replays(memory):
    first = memory.call("mnemon_remember", {"content": "剧本编码采用 UTF-8。"})
    memory.call("mnemon_remember", {"content": "日本語の名前を保持する。"})
    for query in ("剧本编码", "日本語", "utf", "UTF-8"):
        assert memory.call("mnemon_recall", {"query": query})["results"]
    assert memory.call("mnemon_recall", {"query": "没有相关内容"})["results"] == []
    memory.call("mnemon_remember", {"id": first["id"], "content": "剧本编码采用 CP932。"})
    recall = memory.call("mnemon_recall", {"query": "剧本编码"})
    assert recall["results"][0]["content"] == "剧本编码采用 CP932。"
    assert memory.call("mnemon_recall", {"query": "UTF-8"})["results"] == []


@pytest.mark.parametrize("content,queries", [
    ("文件是SJIS编码", ("编码", "sjis", "SJIS编码")),
    ("UTF-8编码", ("编码", "UTF-8", "UTF-8编码")),
    ("GalTransl项目", ("项目", "galtransl", "GalTransl项目")),
    ("ID列不要翻译", ("列不要", "翻译", "id", "ID列")),
    ("v2版本", ("版本", "V2", "v2版本")),
    ("UTF8エンコード", ("エンコード", "utf8", "UTF8エンコード")),
    ("SJIS文字コード", ("文字", "コード", "sjis", "SJIS文字")),
    ("UTF8인코딩", ("인코딩", "utf8", "UTF8인코딩")),
    ("café编码", ("编码", "CAFÉ", "café编码")),
])
def test_recall_mixed_latin_and_cjk_text(memory, content, queries):
    saved = memory.call("mnemon_remember", {"content": content})
    for query in queries:
        recalled = memory.call("mnemon_recall", {"query": query})
        assert recalled["totalMatches"] == 1
        assert recalled["results"][0]["id"] == saved["id"]


def test_recall_limits_whole_response_and_returns_complete_entries(memory):
    originals = [f"剧本编码规则{index}：" + "日本語" * 180 for index in range(8)]
    for content in originals:
        memory.call("mnemon_remember", {"content": content})
    result = memory.call("mnemon_recall", {"query": "剧本编码"}, token_budget=500)
    assert 0 < len(result["results"]) < 5
    assert result["totalMatches"] == 8
    assert result["truncated"] is True
    assert _estimate_text_tokens(json.dumps(result, ensure_ascii=False)) <= 500
    assert all(entry["content"] in originals for entry in result["results"])
    one = memory.call("mnemon_recall", {"query": "剧本编码", "limit": 1})
    assert len(one["results"]) == 1
    assert memory.call("mnemon_recall", {"query": "剧本编码"}, token_budget=0)["notRun"] is True
    assert memory.call("mnemon_recall", {"query": "剧本编码"}, token_budget=100)["reason"] == "insufficient_context"


def test_stopped_tool_leaves_data_unchanged(memory):
    memory.store.stop_event.set()
    with pytest.raises(AgentStopRequested):
        memory.call("mnemon_remember", {"content": "should never be committed"})
    assert memory.store.entries() == []


def test_projects_isolate_and_sessions_share_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("GALTRANSL_DATA_DIR", str(tmp_path / "data"))
    sessions = [MnemonSession(tmp_path / project, session) for project, session in
        (("a", "one"), ("a", "two"), ("b", "three"))]
    try:
        sessions[0].call("mnemon_remember", {"content": "项目甲的人名", "importance": "critical"}, "same-call")
        assert sessions[1].store.entries()[0]["content"] == "项目甲的人名"
        assert "项目甲的人名" in sessions[1].projection()
        assert sessions[2].store.entries() == []
        assert sessions[1].call("mnemon_remember", {"content": "项目甲的编码"}, "same-call")["success"]
        assert "项目甲的人名" in sessions[1].projection()
    finally:
        for session in sessions:
            session.close()


def test_global_memory_is_shared_and_project_facts_remain_isolated(tmp_path):
    first = MnemonSession(tmp_path / "a", "one")
    try:
        global_entry = first.call("mnemon_remember", {"content": "始终使用简体中文", "scope": "global", "importance": "critical"})
        project_entry = first.call("mnemon_remember", {"content": "甲项目使用 CP932", "importance": "critical"})
        normal = first.call("mnemon_remember", {"content": "通用工具需要 UTF-8", "scope": "global"})
        second = MnemonSession(tmp_path / "b", "two")
        try:
            assert global_entry["id"] in second.projection()
            assert project_entry["id"] not in second.projection()
            assert normal["id"] not in second.projection()
            assert second.call("mnemon_recall", {"query": "简体中文"})["results"][0]["scope"] == "global"
            assert second.call("mnemon_recall", {"query": "通用工具"})["results"][0]["id"] == normal["id"]
            assert second.call("mnemon_recall", {"query": "CP932"})["results"] == []
        finally:
            second.close()
    finally:
        first.close()


def test_global_update_and_delete_infer_scope_from_id_across_projects(tmp_path):
    first = MnemonSession(tmp_path / "a", "one")
    second = MnemonSession(tmp_path / "b", "two")
    try:
        saved = first.call("mnemon_remember", {"content": "偏好简短回答", "scope": "global", "importance": "critical"}, "save")
        updated = second.call("mnemon_remember", {"id": saved["id"], "content": "偏好详细回答"}, "update")
        assert updated["scope"] == "global"
        assert updated["importance"] == "critical"
        assert "偏好详细回答" in second.projection()
        assert "偏好简短回答" not in second.projection()
        assert first.call("mnemon_recall", {"query": "详细回答"})["results"][0]["id"] == saved["id"]
        assert second.call("mnemon_remember", {"id": saved["id"], "content": "偏好详细回答"}, "update") == updated
        deleted = second.call("mnemon_forget", {"id": saved["id"]}, "delete")
        assert deleted["scope"] == "global"
        assert second.call("mnemon_forget", {"id": saved["id"]}, "delete") == deleted
        assert first.call("mnemon_recall", {"query": "详细回答"})["results"] == []
        assert saved["id"] in second.projection()
        with closing(MnemonSession(tmp_path / "b", "new")) as fresh:
            assert saved["id"] not in fresh.projection()
    finally:
        first.close()
        second.close()


def test_scopes_deduplicate_separately_and_cannot_move_existing_entries(memory):
    project = memory.call("mnemon_remember", {"content": "保留完整人名"})
    global_entry = memory.call("mnemon_remember", {"content": "保留完整人名", "scope": "global"})
    assert project["id"] != global_entry["id"]
    assert global_entry["id"].startswith("global-")
    assert memory.call("mnemon_remember", {"content": "保留完整人名", "scope": "global"})["duplicate"] is True
    for entry, other_scope in ((project, "global"), (global_entry, "project")):
        with pytest.raises(AgentToolError, match="scope cannot be changed"):
            memory.call("mnemon_remember", {"id": entry["id"], "content": "其他事实", "scope": other_scope})
    results = memory.call("mnemon_recall", {"query": "完整人名"})["results"]
    assert {entry["scope"] for entry in results} == {"project", "global"}
    memory.call("mnemon_forget", {"id": global_entry["id"]})
    assert memory.call("mnemon_recall", {"query": "完整人名"})["results"][0]["id"] == project["id"]


def test_global_receipts_are_namespaced_by_project_and_session(tmp_path):
    sessions = [MnemonSession(tmp_path / project, "same-session") for project in ("a", "b")]
    try:
        first = sessions[0].call("mnemon_remember", {"content": "中文回答", "scope": "global"}, "same-call")
        second = sessions[1].call("mnemon_remember", {"content": "日文姓名完整", "scope": "global"}, "same-call")
        assert first["id"] != second["id"]
        assert len(sessions[1].global_store.entries()) == 2
        assert sessions[0].call("mnemon_remember", {"content": "中文回答", "scope": "global"}, "same-call") == first
        with pytest.raises(AgentToolError, match="different arguments"):
            sessions[0].call("mnemon_remember", {"content": "不同回答", "scope": "global"}, "same-call")
    finally:
        for session in sessions:
            session.close()


def test_global_and_project_results_share_snapshot_and_recall_budgets(memory):
    memory.projection_limit = 160
    for scope in ("global", "project"):
        for index in range(4):
            memory.call("mnemon_remember", {"content": f"{scope} 编码规则{index}：" + "日本語" * 80,
                "scope": scope, "importance": "critical"})
    assert _estimate_text_tokens(memory.projection()) <= _estimate_text_tokens(MNEMON_GUIDANCE) + 160 + 64
    assert len(memory.store.entries()) + len(memory.global_store.entries()) == 8
    result = memory.call("mnemon_recall", {"query": "编码规则"}, token_budget=600)
    assert result["totalMatches"] == 8
    assert 0 < len(result["results"]) < 5
    assert _estimate_text_tokens(json.dumps(result, ensure_ascii=False)) <= 600


@pytest.fixture
def model_server(request):
    requests = []
    scope = getattr(request, "param", "project")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            if len(requests) == 1:
                delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "remember-call", "type": "function",
                    "function": {"name": "mnemon_remember", "arguments": json.dumps({"content": "新回合要记住的编码约定", "importance": "critical", "scope": scope}, ensure_ascii=False)}}]}
                finish = "tool_calls"
            else:
                delta = {"role": "assistant", "content": "已完成"}
                finish = "stop"
            events = [{"id": "local", "object": "chat.completion.chunk", "model": "local", "created": 1,
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"id": "local", "object": "chat.completion.chunk", "model": "local", "created": 1,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}]
            body = ("".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events) + "data: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    worker.start()
    yield f"http://127.0.0.1:{server.server_port}", requests
    server.shutdown()
    server.server_close()
    worker.join(timeout=3)


def make_runner(tmp_path, runner_type, endpoint):
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    profile = {"OpenAI-Compatible": {"tokens": [{"token": "local-test", "modelName": "local", "endpoint": endpoint}]}}
    state = AgentState(project_dir=str(project), backend_profile_data=profile, permission_mode="auto",
        messages=[{"role": "system", "content": "Original domain prompt."}, {"role": "user", "content": "请记住编码约定"}])
    store = SessionStore(str(project), "host", directory=str(tmp_path / "sessions"))
    if runner_type is ExtractionRunner:
        return ExtractionRunner(state, store, {"workspace": str(project), "game_dir": str(project)},
            {"settings": {"skill_dir": str(tmp_path)}}, lambda event: None)
    runner = AgentRunner(state)
    runner._store = store
    return runner


@contextmanager
def pending_approval(runner, action):
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(action)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not future.done():
                with runner.state.event_lock:
                    if any(event.type == "permission_request" and event.data["id"] ==
                            (runner._pending_permission or {}).get("request_id") for event in runner.state.events):
                        break
                time.sleep(.01)
            assert runner._pending_permission is not None, future.result() if future.done() else "No approval request"
            yield future
        finally:
            if not future.done():
                runner.stop_event.set()


@pytest.mark.parametrize("runner_type", [AgentRunner, ExtractionRunner])
@pytest.mark.parametrize("mode", ["ask", "accept-edits", "auto", "auto-quiet"])
def test_global_memory_mutations_require_each_approval(tmp_path, runner_type, mode):
    runner = make_runner(tmp_path, runner_type, "http://127.0.0.1:1")
    runner.state.permission_mode = mode
    runner._mnemon = MnemonSession(tmp_path / "project")
    try:
        runner.state.permission_grants.update({"mnemon_remember", "mnemon_forget"})
        args = {"content": "全局偏好", "scope": " global ", "importance": "critical"}
        with pending_approval(runner, lambda: runner._execute_tool("mnemon_remember", args)) as future:
            assert runner._mnemon.global_store.entries() == []
            assert runner._pending_permission["risk"] == "global-memory"
            runner.apply_permission_mode("auto-quiet")
            assert runner._pending_permission["decision"] == ""
            runner.resolve_permission("allow-session")
            saved = future.result(timeout=3)
        with pending_approval(runner, lambda: runner._execute_tool("mnemon_remember", {
                "id": " " + saved["id"] + " ", "content": "更新后的偏好"})) as future:
            assert runner._mnemon.global_store.entries()[0]["content"] == "全局偏好"
            runner.resolve_permission("allow-once")
            assert future.result(timeout=3)["id"] == saved["id"]
        with pending_approval(runner, lambda: runner._execute_tool("mnemon_forget", {"id": saved["id"]})) as future:
            runner.resolve_permission("deny", "保留这条记忆")
            with pytest.raises(AgentToolError, match="保留这条记忆"):
                future.result(timeout=3)
        assert runner._mnemon.global_store.entries()[0]["content"] == "更新后的偏好"
        with pending_approval(runner, lambda: runner._execute_tool("mnemon_forget", {"id": saved["id"]})) as future:
            runner.resolve_permission("allow-once")
            assert future.result(timeout=3)["success"]
        assert runner._mnemon.global_store.entries() == []
    finally:
        runner.close_store()


@pytest.mark.parametrize("runner_type", [AgentRunner, ExtractionRunner])
def test_snapshot_is_in_system_prompt_and_external_deletion_keeps_prefix(tmp_path, runner_type):
    runner = make_runner(tmp_path, runner_type, "http://127.0.0.1:1")
    domain_prompt = runner._messages_for_request()[0]["content"]
    runner._mnemon = MnemonSession(tmp_path / "project")
    original = copy.deepcopy(runner.state.messages)
    content = "使用简体中文。\n保留完整的人名。"
    saved = runner._mnemon.call("mnemon_remember", {"content": content, "scope": "global", "importance": "critical"})
    try:
        messages = runner._messages_for_request()
        assert len(messages) == len(original)
        assert messages[0]["role"] == "system"
        assert messages[0]["content"].startswith(domain_prompt)
        assert messages[0]["content"].count("[AGENT MEMORY SNAPSHOT;") == 1
        assert f"[global; {saved['id']}] {content}" in messages[0]["content"]
        assert messages[-1] == original[-1]
        assert runner.state.messages == original
        external = MnemonStore(memory_directory(), scope="global")
        try:
            external.mutate(Forget(saved["id"]))
        finally:
            external.close()
        assert runner._messages_for_request()[0]["content"] == messages[0]["content"]
        assert runner._mnemon.call("mnemon_recall", {"query": "简体中文"})["results"] == []
        with closing(MnemonSession(tmp_path / "project", "new")) as fresh:
            assert saved["id"] not in fresh.projection()
    finally:
        runner.close_store()


@pytest.mark.parametrize("runner_type", [AgentRunner, ExtractionRunner])
def test_host_limits_recall_by_current_request_context(tmp_path, runner_type):
    runner = make_runner(tmp_path, runner_type, "http://127.0.0.1:1")
    runner._mnemon = MnemonSession(tmp_path, directory=tmp_path / "memory")
    runner._context_window = 16384
    try:
        saved = runner._mnemon.call("mnemon_remember", {"content": "剧本编码采用 UTF-8。"})
        runner.state.messages.append({"role": "user", "content": "满" * 24000})
        exhausted = runner._execute_tool("mnemon_recall", {"query": "剧本编码"})
        assert exhausted["notRun"] is True
        assert exhausted["reason"] == "insufficient_context"
        runner.state.messages.pop()
        result = runner._execute_tool("mnemon_recall", {"query": "剧本编码"})
        assert result["results"][0]["id"] == saved["id"]
    finally:
        runner.close_store()


@pytest.mark.parametrize("runner_type", [AgentRunner, ExtractionRunner])
@pytest.mark.parametrize("critical", [False, True])
def test_memory_keeps_measured_usage_and_estimates_only_new_messages(tmp_path, runner_type, critical):
    runner = make_runner(tmp_path, runner_type, "http://127.0.0.1:1")
    runner._context_window = 200000
    runner._mnemon = MnemonSession(tmp_path / "project")
    try:
        if critical:
            runner._mnemon.call("mnemon_remember", {"content": "保留完整人名", "importance": "critical"})
        runner.state.messages.append({"role": "user", "content": "中" * 240000})
        assert runner._estimate_context_tokens() > 160000
        runner._record_context_usage(60000)
        assert runner._estimate_context_tokens() == 60000
        assert runner._begin_compaction() is False
        appended = {"role": "assistant", "content": "已完成", "reasoning_content": "检查结果"}
        runner.state.messages.append(appended)
        assert runner._estimate_context_tokens() == 60000 + _estimate_message_tokens(appended)
        runner._record_context_usage(61000)
        assert runner._estimate_context_tokens() == 61000
        assert runner._store.load()["meta"]["context_usage_anchor"]["overhead_tokens"] == runner.state.usage_overhead_tokens
    finally:
        runner.close_store()


@pytest.mark.parametrize("runner_type", [AgentRunner, ExtractionRunner])
def test_new_runner_reuses_saved_snapshot_and_new_session_reads_latest(tmp_path, runner_type):
    runner = make_runner(tmp_path, runner_type, "http://127.0.0.1:1")
    runner._mnemon = MnemonSession(tmp_path / "project")
    saved = runner._mnemon.call("mnemon_remember", {"content": "最初的项目约定", "importance": "critical"})
    prefix = runner._messages_for_request()[0]["content"]
    for message in runner.state.messages:
        runner._store.append_message(message)
    runner._record_context_usage(60000)
    runner._mnemon.call("mnemon_remember", {"id": saved["id"], "content": "更新后的项目约定"})
    runner._mnemon.call("mnemon_remember", {"content": "新增加的常驻记忆", "importance": "critical"})
    reopened = make_runner(tmp_path, runner_type, "http://127.0.0.1:1")
    reopened._mnemon = MnemonSession(tmp_path / "project")
    fresh = make_runner(tmp_path, runner_type, "http://127.0.0.1:1")
    fresh._store = SessionStore(str(tmp_path / "project"), "new-session", directory=str(tmp_path / "sessions"))
    fresh._mnemon = MnemonSession(tmp_path / "project")
    try:
        checkpoint = reopened._store.load()["meta"]["context_usage_anchor"]
        usage = _restore_usage_anchor(reopened.state.messages, checkpoint)
        reopened.state.last_prompt_tokens = usage[0]
        reopened.state.anchored_message_count = usage[1]
        reopened.state.usage_model = usage[2]
        reopened.state.usage_overhead_tokens = usage[3]
        assert runner._messages_for_request()[0]["content"] == prefix
        assert reopened._messages_for_request()[0]["content"] == prefix
        assert reopened._estimate_context_tokens() == 60000
        assert reopened._mnemon.call("mnemon_recall", {"query": "更新后的项目约定"})["results"][0]["id"] == saved["id"]
        new_prefix = fresh._messages_for_request()[0]["content"]
        assert "更新后的项目约定" in new_prefix
        assert "新增加的常驻记忆" in new_prefix
        assert "最初的项目约定" not in new_prefix
        assert new_prefix != prefix
    finally:
        runner.close_store()
        reopened.close_store()
        fresh.close_store()


@pytest.mark.parametrize("runner_type", [AgentRunner, ExtractionRunner])
def test_old_session_without_snapshot_does_not_read_new_memory(tmp_path, runner_type):
    runner = make_runner(tmp_path, runner_type, "http://127.0.0.1:1")
    for message in runner.state.messages:
        runner._store.append_message(message)
    runner._mnemon = MnemonSession(tmp_path / "project")
    runner._mnemon.call("mnemon_remember", {"content": "新的常驻规则", "importance": "critical"})
    try:
        assert runner._messages_for_request()[0] == runner.state.messages[0]
        assert runner.state.mnemon_snapshot == ""
        assert runner._mnemon.call("mnemon_recall", {"query": "常驻规则"})["results"]
    finally:
        runner.close_store()


def test_extraction_restore_keeps_snapshot_and_usage_overhead(tmp_path):
    from GalTransl.Extraction.runner import restore_state
    runner = make_runner(tmp_path, ExtractionRunner, "http://127.0.0.1:1")
    runner._store = SessionStore(str(tmp_path / "project"), "host", directory=str(tmp_path / "execution"))
    runner._mnemon = MnemonSession(tmp_path / "project")
    saved = runner._mnemon.call("mnemon_remember", {"content": "提取约定", "importance": "critical"})
    prefix = runner._messages_for_request()[0]["content"]
    for message in runner.state.messages:
        runner._store.append_message(message)
    runner._record_context_usage(78000)
    runner._mnemon.call("mnemon_remember", {"id": saved["id"], "content": "修改后的提取约定"})
    session = {"workspace": str(tmp_path / "project"), "game_dir": str(tmp_path / "project"), "id": "host", "title": "test"}
    state, store = restore_state(session, tmp_path, {}, "继续", "auto")
    restored = ExtractionRunner(state, store, session, {"settings": {"skill_dir": str(tmp_path)}}, lambda event: None)
    restored._mnemon = MnemonSession(tmp_path / "project")
    try:
        assert state.mnemon_snapshot == runner.state.mnemon_snapshot
        assert state.usage_overhead_tokens == runner.state.usage_overhead_tokens
        assert restored._messages_for_request()[0]["content"] == prefix
        assert restored._estimate_context_tokens() == 78000
    finally:
        runner.close_store()
        restored.close_store()


@pytest.mark.parametrize("runner_type", [AgentRunner, ExtractionRunner])
@pytest.mark.parametrize("billion", [False, True])
@pytest.mark.parametrize("model_server", ["project", "global"], indirect=True)
def test_real_model_loop_keeps_session_snapshot_and_journal(tmp_path, monkeypatch, model_server, runner_type, billion, request):
    monkeypatch.setenv("GALTRANSL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GALTRANSL_APP_SETTINGS_PATH", str(tmp_path / "settings.json"))
    save_app_settings({"mnemonEnabled": True, "extractionMnemonEnabled": True,
        "billionContextEnabled": billion, "extractionBillionContextEnabled": billion})
    endpoint, requests = model_server
    runner = make_runner(tmp_path, runner_type, endpoint)
    original = copy.deepcopy(runner.state.messages)
    scope = request.node.callspec.params["model_server"]
    if scope == "global":
        with pending_approval(runner, runner.run) as future:
            runner.resolve_permission("allow-once")
            future.result(timeout=5)
    if scope == "project":
        runner.run()
    try:
        assert runner.state.status == "awaiting_input", runner.state.error
        assert len(requests) == 2
        assert requests[0]["messages"][0]["content"].count("AGENT MEMORY\n") == 1
        assert requests[0]["messages"][0]["content"] == requests[1]["messages"][0]["content"]
        assert "新回合要记住的编码约定" not in requests[1]["messages"][0]["content"]
        assert not any("[AGENT MEMORY SNAPSHOT;" in message.get("content", "")
            for message in requests[1]["messages"] if message["role"] == "user")
        names = {tool["function"]["name"] for tool in requests[0]["tools"]}
        assert MnemonSession.names <= names
        assert (BillionContextSession.names <= names) is billion
        assert runner.state.messages[:2] == original
        assert runner._mnemon is None
        assert _tool_risk("mnemon_remember") == "high"
        assert _tool_risk("mnemon_recall") == "read"
        runner.state.messages.append({"role": "user", "content": "继续"})
        runner.run()
        assert len(requests) == 3
        assert requests[2]["messages"][0]["content"] == requests[0]["messages"][0]["content"]
        with closing(MnemonSession(tmp_path / "project", "new")) as fresh:
            assert "新回合要记住的编码约定" in fresh.projection()
    finally:
        runner.abort_in_flight()
        runner.close_store()


@pytest.mark.parametrize("translation,extraction", [(False, False), (True, False), (False, True), (True, True)])
def test_agent_switches_are_independent_and_pinned_for_turn(tmp_path, monkeypatch, model_server, translation, extraction):
    monkeypatch.setenv("GALTRANSL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GALTRANSL_APP_SETTINGS_PATH", str(tmp_path / "settings.json"))
    save_app_settings({"mnemonEnabled": translation, "extractionMnemonEnabled": extraction})
    endpoint, _ = model_server
    runners = [make_runner(tmp_path / name, kind, endpoint) for name, kind in (("translation", AgentRunner), ("extraction", ExtractionRunner))]
    try:
        for runner, enabled in zip(runners, (translation, extraction)):
            runner._resolve_llm()
            assert (runner._mnemon is not None) is enabled
        prefixes = [runner._messages_for_request()[0]["content"] for runner in runners]
        save_app_settings({"mnemonEnabled": not translation, "extractionMnemonEnabled": not extraction})
        for runner, enabled, prefix in zip(runners, (translation, extraction), prefixes):
            assert (runner._mnemon is not None) is enabled
            runner.abort_in_flight()
            runner._resolve_llm()
            assert (runner._mnemon is not None) is (not enabled)
            assert runner._messages_for_request()[0]["content"] == prefix
    finally:
        for runner in runners:
            runner.abort_in_flight()
            runner.close_store()


def test_partial_settings_preserve_both_memory_switches(tmp_path, monkeypatch):
    monkeypatch.setenv("GALTRANSL_APP_SETTINGS_PATH", str(tmp_path / "settings.json"))
    save_app_settings({"mnemonEnabled": True, "extractionMnemonEnabled": False})
    save_app_settings({**load_app_settings(), "billionContextEnabled": True})
    assert load_app_settings()["mnemonEnabled"] is True
    assert load_app_settings()["extractionMnemonEnabled"] is False
