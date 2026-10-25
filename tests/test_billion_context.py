"""Python core invariants and both host adapters; no network or Node process."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from GalTransl.Agent.billion_context import BillionContextSession, text_tokens
from GalTransl.Agent.core import AgentStopRequested
from GalTransl.Agent.models import AgentState, AgentToolError
from GalTransl.Agent.runner import AgentRunner
from GalTransl.Agent.session_store import SessionStore
from GalTransl.Extraction.runner import ExtractionRunner


def history(count=18):
    return [{"role": "system", "content": "Original system prompt"}] + [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"记录{i}日本語原文路径C:/项目。" * 350}
        for i in range(count)]


def unpack(result):
    assert result["isError"] is False
    return json.loads(result["content"][0]["text"])


def fold(plugin, start="m00001", end="m00004", summary=None, call_id=""):
    return unpack(plugin.call("compress", {"content": [{"startId": start, "endId": end,
        "summary": summary or ("保留用户目标、关键路径与决定；已完成历史检查，剩余工作仍待处理。" * 4),
        "topic": "原文检查"}]}, call_id))["blocks"][0]


@pytest.fixture
def plugin(tmp_path):
    item = BillionContextSession(path=tmp_path / "context.sqlite3", context_window=12000)
    item.sync(history())
    yield item
    item.close()


def test_archive_projection_restart_refs_and_exact_unicode(plugin):
    source = history()
    saved = copy.deepcopy(source)
    before = plugin.project(source)
    receipt = fold(plugin, call_id="exactly-once")
    after = plugin.project(source)
    assert text_tokens(json.dumps(after, ensure_ascii=False)) < text_tokens(json.dumps(before, ensure_ascii=False))
    restored = unpack(plugin.call("decompress", {"blockId": receipt["id"], "full": True}))
    assert [record["message"] for record in restored["records"]] == source[1:5]
    assert source == saved
    original_path = plugin.path
    # A separate connection simulates a restarted process (also reads committed data).
    resumed = BillionContextSession(path=original_path, context_window=12000)
    try:
        resumed.sync(source)
        assert resumed.project(source) == after
        assert fold(resumed, call_id="exactly-once") == receipt
        with pytest.raises(AgentToolError, match="different arguments"):
            resumed.call("acp_status", {}, "exactly-once")
        assert resumed.status()["activeBlockCount"] == 1
        resumed.sync([*source, {"role": "user", "content": "继续"}])
        assert resumed.status()["recentRefs"][-1]["ref"] == "m00019"
    finally:
        resumed.close()


def test_tiers_and_one_tier_or_full_retrieval(plugin):
    long_summary = "第一阶段用户要求准确提取原文，保留术语和原文路径，已检查工具输出。" * 50
    assert fold(plugin, end="m00002", summary=long_summary)["tier"] == 1
    fold(plugin, "m00003", "m00004", long_summary)
    fold(plugin, "m00005", "m00006", long_summary)
    tier2 = fold(plugin, "b1", "b2", "关键决定与原文约束。" * 40)
    assert tier2["tier"] == 2
    children = unpack(plugin.call("decompress", {"blockId": "b4"}))["records"]
    assert [record["blockId"] for record in children] == ["b1", "b2"]
    assert children[0]["summary"] == long_summary
    tier3 = fold(plugin, "b4", "b3", "Open objectives: 精确提取仍待验证 (m00001)。保留控制码与原文，不得覆盖游戏资源。")
    assert tier3["tier"] == 3
    originals = unpack(plugin.call("decompress", {"blockId": "b5", "full": True, "limit": 2}))
    assert originals["total"] == 6
    assert originals["nextOffset"] == 2
    assert originals["records"][0]["message"] == history()[1]
    page2 = unpack(plugin.call("decompress", {"blockId": "b5", "full": True, "offset": 2, "limit": 100}))
    assert page2["records"][0]["ref"] == "m00003"
    assert plugin.status()["activeBlockCount"] == 1


def test_tool_boundaries_and_signed_reasoning_are_untouched(plugin):
    source = history()
    assistant = {"role": "assistant", "content": None, "reasoning_content": "签名推理☃\n original ",
                 "reasoning_details": [{"type": "signature", "data": "unchanged"}], "tool_calls": [
        {"id": "a", "type": "function", "function": {"name": "read", "arguments": ' { "path": "日本語" } '}},
        {"id": "b", "type": "function", "function": {"name": "read", "arguments": '{"path":"b"}'}}]}
    source[2:3] = [assistant, {"role": "tool", "tool_call_id": "a", "content": "結果☃" * 1500},
                   {"role": "tool", "tool_call_id": "b", "content": "第二結果" * 1500}]
    wire = plugin.project(source)
    assert next(message for message in wire if message.get("tool_calls")) == assistant
    # m00001 survives reconciliation; the replaced assistant/results receive new refs.
    refs = plugin._db.execute("SELECT ref,payload FROM messages ORDER BY rowid").fetchall()
    by_call = {json.loads(payload).get("tool_call_id"): ref for ref, payload in refs if json.loads(payload).get("role") == "tool"}
    block = fold(plugin, by_call["a"], by_call["a"])
    originals = unpack(plugin.call("decompress", {"blockId": block["id"], "full": True}))["records"]
    assert [record["message"] for record in originals] == source[2:5]
    wire = plugin.project(source)
    assert not any(message.get("tool_calls") or message.get("role") == "tool" for message in wire)


def test_failed_batch_rolls_back_blocks_ids_and_receipts(plugin):
    before = plugin.status()
    entries = [{"startId": "m00001", "endId": "m00002", "summary": "摘要" * 80},
               {"startId": "m00002", "endId": "m00003", "summary": "摘要" * 80}]
    with pytest.raises(AgentToolError):
        plugin.call("compress", {"content": entries}, "failed-batch")
    assert plugin.status() == before
    assert plugin._db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0
    assert fold(plugin)["id"] == "b1"


@pytest.mark.parametrize("start,end,summary", [
    ("m00018", "m00018", "recent tail" * 10),
    ("m00006", "m00001", "reversed range" * 10),
    ("m99999", "m99999", "unknown refs" * 10),
    ("m00001", "m00002", "short"),
    ("m00001", "m00001", "大" * 19000),
], ids=["protected", "reversed", "unknown", "short", "nonshrinking"])
def test_invalid_nonshrinking_and_protected_ranges_leave_history(plugin, start, end, summary):
    with pytest.raises(AgentToolError):
        fold(plugin, start, end, summary)
    assert plugin.status()["activeBlockCount"] == 0


def test_cancel_during_transaction_rolls_back(plugin, monkeypatch):
    original = plugin._compress
    def cancelled(*args):
        result = original(*args)
        plugin.stop_event.set()
        return result
    monkeypatch.setattr(plugin, "_compress", cancelled)
    with pytest.raises(AgentStopRequested):
        fold(plugin, call_id="cancelled")
    plugin.stop_event.clear()
    assert plugin.status()["activeBlockCount"] == 0
    assert plugin._db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0


def test_rewritten_history_retains_archives_without_stale_folds_or_ref_reuse(plugin):
    fold(plugin)
    rewritten = [{"role": "system", "content": "Original system prompt"},
                 {"role": "user", "content": "Host summary while plugin was disabled"}, *history()[-3:]]
    wire = plugin.project(rewritten)
    assert plugin.status()["activeBlockCount"] == 0
    assert not any("Historical summary b1" in str(message) for message in wire)
    assert plugin.status()["archivedMessages"] == 19
    found = unpack(plugin.call("search_context", {"query": "日本語原文路径"}))
    assert found["total"] == 18
    assert unpack(plugin.call("decompress", {"blockId": "b1", "full": True}))["records"][0]["ref"] == "m00001"
    assert "m00019" in str(wire)


def test_parser_text_aliases_and_cjk_search(plugin):
    summary = "Open objectives: 保留日本語原文，用户目标尚未完成 (m00001)。" * 3
    response = unpack(plugin.call("compress", {"content": f"m00001–m00002 原文\n{summary}"}))
    assert response["blocks"][0]["id"] == "b1"
    response = unpack(plugin.call("compress", {"content": [{"startRef": "m00003", "endRef": "m00004", "summary": summary}]}))
    assert response["blocks"][0]["id"] == "b2"
    result = unpack(plugin.call("search_context", {"query": "日本語原文", "limit": 1}))
    assert result["results"][0]["ref"] == "b2"
    assert result["nextOffset"] == 1
    rules = unpack(plugin.call("acp_status", {"tier": 2}))["rules"]
    assert "Open objectives" in rules


def test_session_isolation_and_delete_cleanup(tmp_path):
    stores = [SessionStore("project", "same-id", directory=str(tmp_path / domain)) for domain in ("extraction", "translation")]
    plugins = [BillionContextSession(store=store) for store in stores]
    try:
        for item in plugins:
            item.sync(history())
        fold(plugins[0])
        assert plugins[1].status()["activeBlockCount"] == 0
    finally:
        for item in plugins:
            item.close()
    for store in stores:
        store.clear()
        assert not Path(str(store.path) + ".billion-context.sqlite3").exists()


def test_request_hints_do_not_accumulate_and_usage_calibrates(plugin):
    source = history()
    first = plugin.project(source, 100)
    assert "EMERGENCY" in str(first[-1])
    assert plugin.project(source, 100) == first
    plugin.record_usage(5000)
    assert plugin.project(source, 100)
    assert plugin.status()["contextTokens"] < 5100
    fold(plugin)
    plugin.project(source, 100)
    assert plugin.status()["contextTokens"] < 5000
    assert source == history()


@pytest.mark.parametrize("runner_type", [AgentRunner, ExtractionRunner])
def test_both_domains_share_core_dispatch_and_request_projection(tmp_path, runner_type, monkeypatch):
    # Construct the common state; extraction has its own schemas and message projection.
    state = AgentState(messages=history())
    runner = AgentRunner(state)
    runner.__class__ = runner_type
    if runner_type is ExtractionRunner:
        runner._system_prompt = lambda: "Extraction prompt"
    plugin = BillionContextSession(path=tmp_path / "shared.sqlite3", context_window=1000000)
    runner._billion_context = plugin
    runner._context_window = 1000000
    runner._model = "fake"
    runner._openai_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(return_value="stream"))))
    try:
        assert plugin.names <= {tool["function"]["name"] for tool in runner._request_tools()}
        runner._active_tool_call_id = "fold-call"
        result = runner._execute_tool("compress", {"content": [{"startId": "m00001", "endId": "m00004", "summary": "保留原文和用户意图；" * 20}]})
        assert unpack(result)["blocks"][0]["id"] == "b1"
        assert runner._create_stream(include_usage=True) == "stream"
        request = runner._openai_client.chat.completions.create.call_args.kwargs
        assert "read_history_archive" not in {tool["function"]["name"] for tool in request["tools"]}
        assert "Historical summary b1" in str(request["messages"])
        assert request["messages"][0]["content"] == ("Extraction prompt" if runner_type is ExtractionRunner else "Original system prompt")
        assert state.messages == history()
        assert runner._begin_compaction(force=True) is False
    finally:
        plugin.close()


def test_switch_off_default_and_next_turn_activation(tmp_path, monkeypatch):
    from GalTransl.Agent import runner as module
    backend = SimpleNamespace(client=object(), model="model", context_window=12000, prompt_caching=False, reasoning_kwargs={})
    monkeypatch.setattr(module, "resolve_llm_backend", lambda _: backend)
    enabled = False
    monkeypatch.setattr(module, "load_app_settings", lambda: {"billionContextEnabled": enabled})
    runner = AgentRunner(AgentState())
    runner._store = SessionStore("project", "id", directory=str(tmp_path))
    runner._resolve_llm()
    assert runner._billion_context is None
    assert not BillionContextSession.names & {tool["function"]["name"] for tool in runner._request_tools()}
    enabled = True
    runner._resolve_llm()
    assert runner._billion_context is not None
    assert runner._openai_client is backend.client
    enabled = False
    runner._resolve_llm()
    assert runner._billion_context is None


@pytest.mark.parametrize("finish_reason,content,valid", [
    ("stop", "保留用户目标和约束，历史阶段已结束；Open objectives: 原文与译文校验仍待完成 (m00001)。" * 2, True),
    ("length", "incomplete summary" * 5, False),
    ("stop", "too short", False),
], ids=["complete", "truncated", "short"])
def test_bounded_emergency_summary_failure_preserves_history(plugin, finish_reason, content, valid):
    source = history()
    plugin.context_window = 64000  # The translation tool schema alone needs ~12K tokens.
    runner = AgentRunner(AgentState(messages=copy.deepcopy(source)))
    runner._billion_context = plugin
    runner._context_window = plugin.context_window
    runner._model = "fake"
    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish_reason,
        message=SimpleNamespace(content=content, tool_calls=None, refusal=None))])
    create = Mock(return_value=response)
    runner._openai_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    if valid:
        runner._recover_billion_context(force=True)
        assert plugin.status()["activeBlockCount"] > 0
    else:
        with pytest.raises(AgentToolError, match="摘要未完整"):
            runner._recover_billion_context(force=True)
        assert plugin.status()["activeBlockCount"] == 0
    assert runner.state.messages == source
    for call in create.call_args_list:
        request = call.kwargs
        assert request["stream"] is False
        assert text_tokens(json.dumps(request["messages"], ensure_ascii=False)) + request["max_tokens"] < runner._context_window


def test_irreducible_recent_exchange_fails_without_removing_history(plugin):
    source = history(3)
    plugin.sync(source)
    assert plugin.recovery_candidate() is None
    runner = AgentRunner(AgentState(messages=copy.deepcopy(source)))
    runner._billion_context = plugin
    runner._context_window = plugin.context_window
    with pytest.raises(AgentToolError, match="无法继续安全压缩"):
        runner._recover_billion_context(force=True)
    assert runner.state.messages == source


def configured_runner(tmp_path, runner_type, monkeypatch, create, window=256000):
    from GalTransl.Agent import runner as module
    state = AgentState(messages=history())
    store = SessionStore("project", "integration", directory=str(tmp_path / "sessions"))
    if runner_type is ExtractionRunner:
        runner = ExtractionRunner(state, store,
            {"workspace": str(tmp_path), "game_dir": str(tmp_path)},
            {"settings": {"skill_dir": str(tmp_path)}}, lambda event: None)
    else:
        runner = AgentRunner(state)
        runner._store = store
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    backend = SimpleNamespace(client=client, model="fixture", context_window=window,
                              prompt_caching=False, reasoning_kwargs={})
    monkeypatch.setattr(module, "resolve_llm_backend", lambda _: backend)
    monkeypatch.setattr(module, "load_app_settings", lambda: {"billionContextEnabled": True})
    return runner


@pytest.mark.parametrize("runner_type", [AgentRunner, ExtractionRunner])
@pytest.mark.parametrize("strip_internal", [True, False])
def test_history_archive_tool_and_prompt_follow_plugin_switch(tmp_path, runner_type, monkeypatch, strip_internal):
    from GalTransl.Agent import runner as module
    runner = configured_runner(tmp_path, runner_type, monkeypatch, Mock())
    enabled = False
    monkeypatch.setattr(module, "load_app_settings", lambda: {"billionContextEnabled": enabled})
    # Simulate a session saved in ordinary mode and resumed with the plugin.
    runner.state.messages = [
        {"role": "system", "content": runner._system_prompt()},
        {"role": "user", "content": "历史说明 和 read_history_archive(query=...)"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "old-archive", "type": "function", "function": {
                "name": "read_history_archive", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "old-archive", "content": "旧归档结果"},
    ]
    original = copy.deepcopy(runner.state.messages)
    definitions = copy.deepcopy(runner._tool_definitions())
    try:
        runner._resolve_llm()
        ordinary_tools = runner._request_tools()
        assert "read_history_archive" in {tool["function"]["name"] for tool in ordinary_tools}
        assert "read_history_archive" in runner._messages_for_request(strip_internal=strip_internal)[0]["content"]

        enabled = True
        runner._resolve_llm()
        names = {tool["function"]["name"] for tool in runner._request_tools()}
        assert "read_history_archive" not in names
        assert BillionContextSession.names <= names
        messages = runner._messages_for_request(strip_internal=strip_internal)
        assert "read_history_archive" not in messages[0]["content"]
        assert messages[1:] == original[1:]  # Past tool exchanges must remain valid.
        assert runner._tool_definitions() == definitions
        assert runner.state.messages == original

        enabled = False
        runner._resolve_llm()
        assert runner._request_tools() == ordinary_tools
        assert runner._messages_for_request(strip_internal=strip_internal) == original
    finally:
        runner.close_store()


def response_chunk(name=None, args=None, call_id="", content=None):
    calls = [SimpleNamespace(index=0, id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False)))] if name else None
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=calls),
        finish_reason="tool_calls" if calls else "stop")], usage=None)


@pytest.mark.parametrize("runner_type", [AgentRunner, ExtractionRunner])
def test_complete_turn_compress_retrieve_and_restart_in_both_domains(tmp_path, runner_type, monkeypatch):
    requests = []
    chunks = iter([
        response_chunk("compress", {"content": [{"startId": "m00001", "endId": "m00004",
            "summary": "保留原文与用户需求，历史阶段已完成，当前仍需验证提取及译文一致性。" * 4}]}, "compress-id"),
        response_chunk("decompress", {"blockId": "b1", "full": True, "limit": 1}, "retrieve-id"),
        response_chunk(content="完成检查"),
    ])
    def create(**kwargs):
        requests.append(kwargs)
        return iter([next(chunks)])
    runner = configured_runner(tmp_path, runner_type, monkeypatch, create)
    originals = copy.deepcopy(runner.state.messages)
    runner.run()
    assert runner.state.turn_end == "done", runner.state.error
    assert len(requests) == 3
    assert "Historical summary b1" in str(requests[1]["messages"])
    reply = next(message for message in requests[2]["messages"] if message.get("tool_call_id") == "retrieve-id")
    payload = json.loads(reply["content"].split("\n", 1)[1])  # Strip the request-only mNNNNN marker.
    assert payload["records"][0]["message"] == originals[1]
    assert runner.state.messages[:len(originals)] == originals
    assert runner._billion_context is None
    archive = BillionContextSession(store=runner._store)
    try:
        archive.sync(runner._messages_for_request())
        assert archive.status()["activeBlockCount"] == 1
        assert unpack(archive.call("decompress", {"blockId": "b1", "full": True}))["total"] == 4
    finally:
        archive.close()


@pytest.mark.parametrize("runner_type", [AgentRunner, ExtractionRunner])
@pytest.mark.parametrize("recovery", ["proactive", "overflow"])
def test_complete_turn_recovers_context_in_both_domains(tmp_path, runner_type, recovery, monkeypatch):
    requests, summary_requests = [], []
    def create(**kwargs):
        if not kwargs["stream"]:
            summary_requests.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                content="Open objectives: 继续验证原文提取和翻译一致性 (m00001)。保留用户约束与原文路径；较早读取结果已检查。" * 2,
                tool_calls=None, refusal=None))])
        requests.append(kwargs)
        if recovery == "overflow" and len(requests) == 1:
            error = RuntimeError("maximum context length exceeded")
            error.status_code = 400
            raise error
        return iter([response_chunk(content="恢复后完成")])
    runner = configured_runner(tmp_path, runner_type, monkeypatch, create,
                               window=64000 if recovery == "proactive" else 256000)
    originals = copy.deepcopy(runner.state.messages)
    runner.run()
    assert runner.state.turn_end == "done", runner.state.error
    assert summary_requests
    assert len(requests) == (1 if recovery == "proactive" else 2)
    assert "Historical summary b1" in str(requests[-1]["messages"])
    assert runner.state.messages[:len(originals)] == originals


def test_deleted_session_cannot_be_recreated_by_late_backend_resolution(tmp_path, monkeypatch):
    from GalTransl.Agent import runner as module
    runner = AgentRunner(AgentState(messages=history()))
    runner._store = SessionStore("project", "deleted", directory=str(tmp_path))
    def resolve(_):
        runner.stop_event.set()
        runner.close_store()
        runner._store.clear()
        return SimpleNamespace(client=object(), model="fixture", context_window=128000,
                               prompt_caching=False, reasoning_kwargs={})
    monkeypatch.setattr(module, "resolve_llm_backend", resolve)
    monkeypatch.setattr(module, "load_app_settings", lambda: {"billionContextEnabled": True})
    with pytest.raises(AgentStopRequested):
        runner._resolve_llm()
    assert not list(tmp_path.rglob("*.sqlite3*"))
    assert not list(tmp_path.rglob("*.jsonl"))


def test_closed_archive_during_deletion_stops_without_recreating_files(tmp_path):
    runner = AgentRunner(AgentState())
    runner._store = SessionStore("project", "deleted", directory=str(tmp_path))
    runner._billion_context = BillionContextSession(store=runner._store, stop_event=runner.stop_event)
    runner._billion_context.sync(history())
    runner.stop_event.set()
    runner.close_store()
    runner._store.clear()
    with pytest.raises(AgentStopRequested):
        runner._billion_context.project(history())
    runner._billion_context.close()  # Turn finalizer may close it again.
    assert not list(tmp_path.rglob("*.sqlite3*"))
