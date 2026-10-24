"""摘要只有结构完整且正常结束才能覆盖历史；接续原文不依赖模型记忆。"""

import copy
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from GalTransl.Agent import session_store as ss
from GalTransl.Agent.context import (
    _find_compaction_cut,
    _render_task_continuation,
    _restore_compacted_history,
    _serialize_for_summary,
    _strip_internal_fields,
    _task_continuation,
)
from GalTransl.Agent.core import AgentStopRequested
from GalTransl.Agent.llm_backend import summarize_messages
from GalTransl.Agent.models import AgentState
from GalTransl.Agent.prompts import _validated_compact_summary, build_initial_user_message
from GalTransl.Agent.runner import AgentRunner
from GalTransl.Agent.subagent import SubAgentRunner


SUMMARY = """## 目标
校对序章。
## 已完成的工作
已读取 a.json 的 1-20。
## 关键决策
无。
## 当前进度与项目状态
只读阶段。
## 待办与注意事项
继续检查 21-40，尚未修改文件。"""
SUB_SUMMARY = """## 任务与范围
a.json，1-40。
## 已完成
已读 1-20。
## 关键发现
无。
## 待办
检查 21-40。"""
REQUEST = "校对 transl_cache/a.json 的 1-40，只读，不要改预算。"
CORRECTION = "修正：忍不住统一为按捺不住；任务 job-42 未完成，只处理 21-40。"


def history():
    return [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": build_initial_user_message(REQUEST)},
        {"role": "assistant", "content": "已读 1-20，下一步查看 21-40；没有写入文件。"},
        {"role": "user", "content": CORRECTION},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "read-1", "type": "function", "function": {
                "name": "read_transl_cache", "arguments": '{"filename":"a.json","token":"SECRET"}',
            },
        }]},
        {"role": "tool", "tool_call_id": "read-1", "content": "BULK_PRIVATE_RESULT" * 100},
        {"role": "user", "content": "继续"},
    ]


def make_runner():
    state = AgentState(messages=history())
    return AgentRunner(state)


def begin(runner):
    assert runner._begin_compaction(force=True)


def apply_summary(runner, summary=SUMMARY, cut=6):
    runner._pending_compaction = {"cut": cut, "pulled": [], "instruction": None, "estimated": 1000}
    assert runner._rebuild_after_compaction(summary, "")


def backend(content=SUMMARY, finish_reason="stop", **message_fields):
    client = Mock()
    client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
        finish_reason=finish_reason,
        message=SimpleNamespace(content=content, tool_calls=[], **message_fields),
    )])
    return client


@pytest.mark.parametrize("content", [
    "", "只有一句话", "## 目标\n半份摘要", SUMMARY.replace("## 关键决策\n无。\n", ""),
    SUMMARY.replace("## 关键决策\n无。", "## 关键决策"),
    "<summary>" + SUMMARY, SUMMARY + "</summary>",
    "</summary>" + SUMMARY + "<summary>",
    "<summary>" + SUMMARY + "</summary><summary",
    "<summary>" + SUMMARY + "</summary><topics>未闭合",
    "<summary></summary>", "<summary>" + SUMMARY + "</summary><summary>重复</summary>",
    SUMMARY + "\n## 目标\n重复章节", SUMMARY + "\n```json\n{",
])
def test_invalid_summary_rolls_back_without_archiving(content):
    runner = make_runner()
    original = copy.deepcopy(runner.state.messages)
    begin(runner)
    runner._store = Mock()

    assert not runner._finish_compaction(content)

    assert runner.state.messages == original
    assert runner._pending_compaction is None
    runner._store.write_chunk.assert_not_called()
    runner._store.append_message.assert_not_called()
    assert not any(e.type == "compacted" for e in runner.state.events)


@pytest.mark.parametrize("finish", ["length", "content_filter", "tool_calls", "error", ""])
def test_stream_completion_reason_is_checked_before_history_replacement(finish):
    runner = make_runner()
    original = copy.deepcopy(runner.state.messages)
    begin(runner)

    def fallback():
        assert runner.state.messages == original
        assert runner._pending_compaction is None

    with (
        patch.object(runner, "_stream_llm_response", return_value=(SUMMARY, [], finish)),
        patch.object(runner, "_microcompact", side_effect=fallback) as separate,
    ):
        runner._run_compaction_request()
    separate.assert_called_once()
    assert not runner._stream_quiet
    assert runner._stream_acc is None


@pytest.mark.parametrize("finish", ["length", "content_filter", "tool_calls"])
def test_separate_summary_rejects_incomplete_transport(finish):
    assert summarize_messages(backend(finish_reason=finish), "test", history()) == ""


def test_separate_summary_validates_structure_refusal_and_tool_calls():
    assert summarize_messages(backend("## 目标\n只有目标"), "test", history()) == ""
    assert summarize_messages(backend(refusal="filtered"), "test", history()) == ""
    client = backend()
    client.chat.completions.create.return_value.choices[0].message.tool_calls = [object()]
    assert summarize_messages(client, "test", history()) == ""
    # 未提供结束原因的兼容端点仍必须通过完整章节校验。
    assert summarize_messages(backend(finish_reason=None), "test", history()) == SUMMARY
    assert _validated_compact_summary("<TOPICS>校对</TOPICS><SUMMARY>" + SUMMARY + "</SUMMARY>") == SUMMARY


def test_requirements_and_progress_survive_model_summary():
    runner = make_runner()
    system, tail = runner.state.messages[0], runner.state.messages[-1]
    apply_summary(runner)

    checkpoint = runner.state.messages[1]
    assert runner.state.messages[0] is system
    assert runner.state.messages[-1] is tail
    assert REQUEST in checkpoint["content"]
    assert CORRECTION in checkpoint["content"]
    assert "没有写入文件" in checkpoint["content"]
    assert "SECRET" not in checkpoint["content"]
    assert "BULK_PRIVATE_RESULT" not in checkpoint["content"]
    assert "二次判断" not in checkpoint["content"]
    assert "_compact_continuation" not in _strip_internal_fields(checkpoint)


def test_successful_stream_keeps_latest_assistant_round_and_tool_pairs():
    runner = make_runner()
    original = copy.deepcopy(runner.state.messages)
    begin(runner)
    assert runner._finish_compaction("<summary>" + SUMMARY + "</summary>", "stop")
    assert runner.state.messages[0] == original[0]
    # read 调用在安全切点的尾部，工具响应与最终用户消息也原样接回。
    assert runner.state.messages[-3:] == original[-3:]
    assert CORRECTION in runner.state.messages[1]["content"]
    assert runner._pending_compaction is None


@pytest.mark.parametrize("ending", ["tool", "user", "assistant"])
@pytest.mark.parametrize("caching", [False, True])
def test_summary_provider_request_excludes_retained_round_without_mutating_history(ending, caching):
    runner = make_runner()
    # 一次并行调用的所有结果都属于最后一个助手起始轮次。
    runner.state.messages[4]["tool_calls"].append({
        "id": "read-2", "type": "function", "function": {
            "name": "read_transl_cache", "arguments": '{"filename":"b.json"}',
        },
    })
    runner.state.messages.insert(6, {"role": "tool", "tool_call_id": "read-2", "content": "TAIL_RESULT"})
    if ending == "tool":
        runner.state.messages.pop()
    elif ending == "assistant":
        runner.state.messages.append({"role": "assistant", "content": "最新最终回复", "reasoning_content": "TAIL_THINKING"})
    original = copy.deepcopy(runner.state.messages)
    cut = 8 if ending == "assistant" else 4
    runner._openai_client = Mock()
    runner._prompt_caching = caching
    begin(runner)
    assert runner._pending_compaction["cut"] == cut
    assert runner.state.messages[:-1] == original

    runner._create_stream(include_usage=True)
    sent = runner._openai_client.chat.completions.create.call_args.kwargs["messages"]
    # 在实际 provider 边界检查，缓存标记和思考字段补齐都不能把保留段带回去。
    assert len(sent) == cut + 1
    for actual, expected in zip(sent[:-1], original[:cut]):
        content = actual["content"]
        if isinstance(content, list):
            content = "".join(block.get("text", "") for block in content)
        assert content == expected["content"]
        assert actual.get("tool_calls") == expected.get("tool_calls")
    assert "最近一个助手轮次" in sent[-1]["content"]
    assert all(not key.startswith("_") for message in sent for key in message)
    assert runner.state.messages[:-1] == original

    runner._abort_compaction()
    assert runner.state.messages == original
    assert len(runner._messages_for_request()) == len(original)


def test_round_selection_preserves_following_user_messages_and_does_not_use_token_tail():
    messages = history()
    messages[3:3] = [{"role": "user", "content": "旧历史" * 20000}]
    assert _find_compaction_cut(messages) == 5
    assert _find_compaction_cut(messages[:4]) == 2  # 最终回复及其后的用户消息一起保留
    assert _find_compaction_cut(messages[:2]) == 0  # 只有 system 和用户请求，不能压空


def test_selected_round_restores_from_disk_and_next_request_contains_retained_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "SESSIONS_ROOT", str(tmp_path))
    runner = make_runner()
    runner._store = ss.SessionStore(str(tmp_path / "project"), "selected-round")
    original = copy.deepcopy(runner.state.messages)
    for message in original:
        runner._store.append_message(message)
    begin(runner)
    assert runner._finish_compaction(SUMMARY, "stop")

    assert runner.state.messages[2:] == original[4:]
    assert runner.state.messages[1]["_compact_kept"] == 3
    restored = _restore_compacted_history(runner._store.load()["messages"])
    assert restored == runner.state.messages
    runner.state.messages = restored
    sent = runner._messages_for_request()
    assert sent[2:] == original[4:]
    assert not any(message.get("_compact_instruction") for message in runner._store.load()["messages"])


def test_continuation_survives_repeated_compaction_and_disk_restore(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "SESSIONS_ROOT", str(tmp_path))
    runner = make_runner()
    runner._store = ss.SessionStore(str(tmp_path / "project"), "integrity")
    for message in runner.state.messages:
        runner._store.append_message(message)
    apply_summary(runner)
    card = runner.state.messages[1]["_compact_continuation"]
    for _ in range(4):
        apply_summary(runner, cut=2)
        assert runner.state.messages[1]["_compact_continuation"] == card
        assert runner.state.messages[1]["content"].count("## 任务接续原文摘录") == 1
    restored = _restore_compacted_history(runner._store.load()["messages"])
    assert restored == runner.state.messages
    assert REQUEST in restored[1]["content"]
    assert CORRECTION in restored[1]["content"]
    assert restored[1]["_compact_continuation"] == card


def test_continuation_and_summary_input_keep_long_user_message_tail_with_bounds():
    text = "任务目标：a.json" + "正文" * 10000 + "最后修正：只读，不许写文件。"
    messages = [{"role": "user", "content": text}]
    serialized = _serialize_for_summary(messages)
    card = _task_continuation(messages)
    assert text[:11] in serialized and text[-18:] in serialized
    assert text[:11] in card[0]["text"] and text[-18:] in card[0]["text"]
    assert len(card[0]["text"]) <= 1000
    messages += [{"role": "user", "content": f"新要求 {i}：" + "长" * 3000} for i in range(30)]
    card = _task_continuation(messages)
    assert sum(len(item["text"]) for item in card) <= 6000
    assert len([item for item in card if item["role"] == "user"]) == 4
    assert "新要求 29" in _render_task_continuation(card)


@pytest.mark.parametrize("auto", [False, True])
def test_ask_user_result_survives_but_auto_answer_is_not_user_authorization(auto):
    messages = [{"role": "assistant", "content": "", "tool_calls": [{
        "id": "ask", "function": {"name": "ask_user", "arguments": "{}"},
    }]}, {"role": "tool", "tool_call_id": "ask", "content": json.dumps({
        "summary": "只读检查，先不要写入", "answers": [{"selected": "只读"}], "auto_answered": auto,
    }, ensure_ascii=False)}]
    text = _render_task_continuation(_task_continuation(messages))
    assert "只读检查，先不要写入" in text
    assert ("系统自动代答（非用户确认）" in text) == auto


def test_job_identifier_and_observed_state_survive_without_copying_backend_secrets():
    runner = make_runner()
    runner.state.messages[4]["tool_calls"][0]["function"]["name"] = "start_translation"
    runner.state.messages[5]["content"] = json.dumps({
        "job_id": "job-99", "status": "running", "translator": "ForGal-json",
        "backend": {"token": "NEVER_COPY_TOKEN"}, "log": "BULK_LOG" * 1000,
    })
    apply_summary(runner)
    checkpoint = runner.state.messages[1]
    assert '"job_id": "job-99"' in checkpoint["content"]
    assert '"status": "running"' in checkpoint["content"]
    assert "历史值，继续前需刷新" in checkpoint["content"]
    assert "NEVER_COPY_TOKEN" not in checkpoint["content"]
    assert "BULK_LOG" not in checkpoint["content"]
    carried = _task_continuation([checkpoint])
    assert carried == checkpoint["_compact_continuation"]


def test_stop_during_summary_never_replaces_history():
    runner = make_runner()
    original = copy.deepcopy(runner.state.messages)

    def stop():
        runner.stop_event.set()
        return SUMMARY, [], "stop"

    with pytest.raises(AgentStopRequested):
        begin(runner)
        with patch.object(runner, "_stream_llm_response", side_effect=stop):
            runner._run_compaction_request()
    assert runner.state.messages == original
    assert runner._pending_compaction is None


def test_subagent_rejects_truncated_response_without_requesting_another_summary():
    parent = SimpleNamespace(stop_event=threading.Event(), _context_window=32000,
                             _summarize_messages=Mock(return_value=SUMMARY), _emit=Mock())
    sub = SubAgentRunner(parent, agent="explore", files=[], indexes="", brief="check", delegation_id="test")
    sub.messages = history()
    before = copy.deepcopy(sub.messages)
    sub._pending_compaction = {"head_keep": 2, "cut": 6}
    sub.messages.append({"role": "user", "content": "compress", "_compact_instruction": True})
    # 四节齐全仍不能接受 provider 标记 length 的响应。
    sub._run_compaction_request(backend(SUB_SUMMARY, finish_reason="length"), "test")
    parent._summarize_messages.assert_not_called()
    assert sub.messages == before
    assert sub._pending_compaction is None
