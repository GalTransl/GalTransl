"""主摘要失败时本地清理旧读取结果，保护任务现场及原始转录。"""

import copy
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from GalTransl.Agent import session_store as ss
from GalTransl.Agent.context import (
    MICROCOMPACT_PLACEHOLDER,
    _history_fingerprint,
    _microcompact_tool_results,
    _restore_compacted_history,
    _restore_microcompacted_history,
)
from GalTransl.Agent.core import AgentStopRequested
from GalTransl.Agent.models import AgentState
from GalTransl.Agent.registry import AgentRuntime
from GalTransl.Agent.runner import AgentRunner
from GalTransl.Agent.subagent import SubAgentRunner


def tool_round(index, *, name="read_transl_cache", content=None, parallel=1):
    calls = [{"id": f"c-{index}-{n}", "type": "function", "function": {
        "name": name, "arguments": '{"filename":"a.json","start":1,"end":20}',
    }} for n in range(parallel)]
    return [{"role": "assistant", "content": f"进展 {index}", "tool_calls": calls}, *[
        {"role": "tool", "tool_call_id": call["id"],
         "content": content if content is not None else (f"数据 {index}\n" * 1000) + "完整结果末尾"}
        for call in calls
    ]]


def history(rounds=8, parallel=1):
    messages = [{"role": "system", "content": "STABLE"},
                {"role": "user", "content": "只读检查 a.json 的 1-40，不要修改文件。"}]
    for index in range(rounds):
        messages.extend(tool_round(index, parallel=parallel))
    messages += [{"role": "assistant", "content": "已读完 1-20，接下来检查 21-40。"},
                 {"role": "user", "content": "先不要写，继续检查。"}]
    return messages


def runner_for(messages=None):
    return AgentRunner(AgentState(messages=history() if messages is None else messages))


def restored(store):
    loaded = store.load()
    projected = _restore_microcompacted_history(loaded["messages"], loaded.get("microcompactions") or [])
    return _restore_compacted_history(projected)


@pytest.mark.parametrize("failure", ["exception", "length", "empty", "invalid", "tool_calls"])
def test_summary_failure_uses_microcompact_without_another_model_request(failure):
    runner = runner_for()
    original = copy.deepcopy(runner.state.messages)
    instruction = {"role": "user", "content": "compress", "_compact_instruction": True}
    pulled = runner.state.messages.pop()
    runner.state.messages.append(instruction)
    runner._pending_compaction = {"cut": 8, "instruction": instruction, "pulled": [pulled]}
    runner.state.last_prompt_tokens = 90000
    runner.state.anchored_message_count = 9
    response = ("", [], "stop")
    if failure == "length":
        response = ("partial", [], "length")
    elif failure == "invalid":
        response = ("<summary>半截", [], "stop")
    elif failure == "tool_calls":
        response = ("", [{"id": "unexpected", "name": "start_translation"}], "tool_calls")
    with (
        patch.object(runner, "_stream_llm_response", side_effect=RuntimeError("down") if failure == "exception" else None,
                     return_value=response) as stream,
        patch.object(runner, "_summarize_messages") as old_fallback,
        patch.object(runner, "_dispatch_tool") as dispatch,
    ):
        runner._run_compaction_request()
    stream.assert_called_once()
    old_fallback.assert_not_called()
    dispatch.assert_not_called()
    assert len(runner.state.messages) == len(original)
    for index, message in enumerate(original):
        if index in (3, 5, 7):
            assert runner.state.messages[index]["content"].startswith(MICROCOMPACT_PLACEHOLDER)
            assert runner.state.messages[index]["tool_call_id"] == message["tool_call_id"]
        else:
            assert runner.state.messages[index] == message
    assert runner._pending_compaction is None
    assert runner.state.last_prompt_tokens == runner.state.anchored_message_count == 0
    assert not runner._begin_compaction(force=False)
    assert not runner._microcompact()  # 同一现场不反复清理、也不产生假成功事件
    compacted = [event for event in runner.state.events if event.type == "compacted"]
    assert len(compacted) == 1 and compacted[0].data["mode"] == "microcompact"
    assert compacted[0].data["tokens_before"] > compacted[0].data["tokens_after"]


def test_parallel_calls_are_kept_as_rounds_not_individual_results():
    messages = history(rounds=7, parallel=6)
    original = copy.deepcopy(messages)
    indices = _microcompact_tool_results(messages)
    assert len(indices) == 12  # 两轮 × 六条；最近五轮的三十条结果完整保留
    assert all(index < 16 for index in indices)
    assert messages == original  # 规划不改源数据


@pytest.mark.parametrize("name,content,extra", [
    ("patch_transl_cache", "修改成功" * 2000, {}),
    ("ask_user", "用户确认" * 2000, {}),
    ("get_runtime", '{"job_id":"job-42","status":"running"}', {}),
    ("start_translation", "启动结果" * 2000, {}),
    ("unknown_tool", "未知工具" * 2000, {}),
    ("read_transl_cache", '{"error":"broken"}', {}),
    ("read_transl_cache", '{"status":"failed"}', {}),
    ("read_transl_cache", '{"ok":false,"data":"partial"}', {}),
    ("read_transl_cache", "读取失败：" + "detail" * 2000, {}),
    ("read_transl_cache", [{"type":"image_url", "image_url":{"url":"data:example"}}], {}),
    ("read_transl_cache", [{"type":"video", "source":{}}], {}),
    ("read_transl_cache", "error" * 2000, {"isError": True}),
], ids=["write", "ask", "runtime", "start", "unknown", "error", "failed", "ok-false", "error-text", "image", "video", "error-flag"])
def test_protected_results_are_never_cleared(name, content, extra):
    protected = tool_round("protected", name=name, content=content)
    protected[-1].update(extra)
    messages = [*protected, *history()]
    assert 1 not in _microcompact_tool_results(messages)


def test_small_savings_are_noop_and_history_is_untouched():
    runner = runner_for(history(rounds=3))
    original = copy.deepcopy(runner.state.messages)
    assert not runner._microcompact()
    assert runner.state.messages == original
    assert not any(event.type == "compacted" for event in runner.state.events)
    messages = [message for i in range(8) for message in tool_round(i, content="ok")]
    assert _microcompact_tool_results(messages) == []


def test_archive_and_restart_keep_projection_but_transcript_retains_full_results(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "SESSIONS_ROOT", str(tmp_path))
    runner = runner_for()
    original = copy.deepcopy(runner.state.messages)
    project = str(tmp_path / "project")
    runner.state.project_dir, runner.state.session_id = project, "micro"
    store = runner._store = ss.SessionStore(project, "micro")
    store.append_meta(project_dir=project, session_id="micro")
    for message in original:
        store.append_message(message)
    assert runner._microcompact()
    loaded = store.load()
    assert loaded["messages"] == original
    assert restored(store) == runner.state.messages
    # 真正的运行时恢复入口也使用清理后的模型上下文。
    state = AgentRuntime()._restore(project, "micro")
    assert state.messages == runner.state.messages
    archive = store.list_chunks()[0]["name"]
    # 归档内容必须保留超过常规 2000 字截断线的末尾。
    raw = Path(store.chunks_dir, archive).read_text(encoding="utf-8")
    assert "完整结果末尾" in raw
    assert original[3]["content"] in raw
    assert archive in state.messages[3]["content"]
    # 重启前后追加新结果，同一组原结果不能被重复清理。
    for message in tool_round("new"):
        runner.state.messages.append(message)
        store.append_message(message)
    assert runner._microcompact()
    assert restored(store) == runner.state.messages


def test_later_reused_tool_id_is_not_changed_by_an_earlier_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "SESSIONS_ROOT", str(tmp_path))
    runner = runner_for()
    store = runner._store = ss.SessionStore(str(tmp_path), "reuse")
    original = copy.deepcopy(runner.state.messages)
    for message in original:
        store.append_message(message)
    assert runner._microcompact()
    for message in original[2:4]:
        store.append_message(message)
    assert restored(store)[-1] == original[3]


def test_microcompact_then_full_summary_restores_the_kept_projection(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "SESSIONS_ROOT", str(tmp_path))
    runner = runner_for()
    store = runner._store = ss.SessionStore(str(tmp_path), "full")
    for message in runner.state.messages:
        store.append_message(message)
    assert runner._microcompact()
    runner._pending_compaction = {"cut": 4, "pulled": [], "instruction": None, "estimated": 10000}
    assert runner._rebuild_after_compaction("一份完整摘要", "校对")
    assert restored(store) == runner.state.messages
    assert any(message.get("content", "").startswith(MICROCOMPACT_PLACEHOLDER) for message in restored(store))


def test_failed_archive_keeps_original_history_and_no_success_event():
    runner = runner_for()
    original = copy.deepcopy(runner.state.messages)
    runner._store = Mock()
    with patch.object(runner, "_archive_compacted", return_value=""):
        assert not runner._microcompact()
    assert runner.state.messages == original
    runner._store.append_microcompact.assert_not_called()
    assert not any(event.type == "compacted" for event in runner.state.events)


def test_stopped_microcompact_does_not_archive_or_mutate():
    runner = runner_for()
    original = copy.deepcopy(runner.state.messages)
    runner.stop_event.set()
    with pytest.raises(AgentStopRequested):
        runner._microcompact()
    assert runner.state.messages == original


def test_checkpoint_write_failure_keeps_original_context(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "SESSIONS_ROOT", str(tmp_path))
    runner = runner_for()
    original = copy.deepcopy(runner.state.messages)
    runner._store = ss.SessionStore(str(tmp_path), "write-failed")
    with patch.object(runner._store, "append_microcompact", return_value=False):
        assert not runner._microcompact()
    assert runner.state.messages == original
    assert not any(event.type == "compacted" for event in runner.state.events)


def test_subagent_microcompact_preserves_assignment_and_recent_rounds():
    parent = SimpleNamespace(stop_event=threading.Event(), _context_window=32000, _emit=Mock())
    sub = SubAgentRunner(parent, agent="explore", files=[], indexes="", brief="check", delegation_id="micro")
    sub.messages = history()
    before = copy.deepcopy(sub.messages)
    assert sub._microcompact()
    assert sub.messages[:2] == before[:2]
    assert sub.messages[-12:] == before[-12:]
    assert sub.messages[3]["content"].startswith(MICROCOMPACT_PLACEHOLDER)
    assert sub._last_compaction_fingerprint == _history_fingerprint(sub.messages)
