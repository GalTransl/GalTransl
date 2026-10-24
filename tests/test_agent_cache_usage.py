"""提示缓存统计：provider 口径、流式去重与会话恢复。"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from GalTransl.Agent import session_store as ss
from GalTransl.Agent.cache_usage import _add_cache_usage, _provider_cache_usage, _restore_cache_usage
from GalTransl.Agent.models import AgentState
from GalTransl.Agent.runner import AgentRunner
from GalTransl.Agent.runtime import AgentRuntime


@pytest.mark.parametrize("usage, expected", [
    ({"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 800}}, (1000, 800)),
    ({"prompt_tokens": 1000, "prompt_cache_hit_tokens": 600, "prompt_cache_miss_tokens": 400}, (1000, 600)),
    ({"prompt_cache_hit_tokens": 600, "prompt_cache_miss_tokens": 400}, (1000, 600)),
    ({"prompt_tokens": 1000, "cache_read_input_tokens": 700, "cache_creation_input_tokens": 100}, (1000, 700)),
    ({"input_tokens": 200, "cache_read_input_tokens": 700, "cache_creation_input_tokens": 100}, (1000, 700)),
    ({"prompt_tokens": 1000}, (1000, None)),
    ({"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 0}}, (1000, 0)),
    ({"prompt_tokens": 1000, "prompt_cache_hit_tokens": 1001}, (1000, None)),
    ({"prompt_tokens": 1000, "prompt_cache_hit_tokens": -1}, (1000, None)),
    ({"prompt_tokens": 1000, "prompt_cache_hit_tokens": True}, (1000, None)),
    ({"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": None}, "prompt_cache_hit_tokens": 600}, (1000, 600)),
    ({"prompt_tokens": 0}, None), ({"prompt_tokens": True}, None), ({}, None),
])
def test_provider_formats_and_unknown_are_not_guessed(usage, expected):
    assert _provider_cache_usage(usage) == expected
    assert _provider_cache_usage(SimpleNamespace(**usage)) == expected


def test_average_is_weighted_and_unknown_is_excluded_but_zero_is_counted():
    stats = _add_cache_usage({}, (100, 100))
    stats = _add_cache_usage(stats, (900, 0))
    assert stats["hit_rate"] == 0.1  # 不是两个百分比的算术平均 50%
    assert stats["request_count"] == 2
    stats = _add_cache_usage(stats, (5000, None))
    assert stats["hit_rate"] == 0.1
    assert stats["request_count"] == 2
    assert stats["input_tokens"] == 1000
    assert stats["latest_hit_rate"] is None
    assert _add_cache_usage({}, (100, None))["hit_rate"] is None


@pytest.mark.parametrize("raw", [None, {}, {"request_count": -1}, {
    "request_count": 1, "input_tokens": 100, "cached_tokens": 200, "latest_input_tokens": 100,
}, {"request_count": 0, "input_tokens": 100, "cached_tokens": 0, "latest_input_tokens": 100}])
def test_corrupt_disk_stats_are_rejected(raw):
    assert _restore_cache_usage(raw) == {}


def run_stream(runner, usages):
    chunks = [SimpleNamespace(choices=[], usage=usage) for usage in usages]
    with patch.object(runner, "_open_stream", return_value=chunks):
        runner._stream_llm_attempt()


def reported(total, cached):
    return SimpleNamespace(prompt_tokens=total, prompt_tokens_details=SimpleNamespace(cached_tokens=cached))


def test_stream_counts_final_usage_once_and_compaction_does_not_corrupt_context_anchor():
    runner = AgentRunner(AgentState(messages=[{"role": "user", "content": "check"}]))
    run_stream(runner, [reported(500, 100), reported(1000, 800)])
    assert runner.state.cache_usage["request_count"] == 1
    assert runner.state.cache_usage["cached_tokens"] == 800
    assert runner.state.last_prompt_tokens == 1000
    event = runner.state.transient_events[-1]
    assert event.type == "context_usage"
    assert event.data["context"]["cache"] == runner.state.cache_usage
    runner._pending_compaction = {"cut": 1}
    run_stream(runner, [reported(500, 100)])
    assert runner.state.cache_usage["request_count"] == 2
    assert runner.state.last_prompt_tokens == 1000  # 摘要请求只含前缀，不能作整份历史的锚点
    assert runner.state.cache_usage["hit_rate"] == 900 / 1500
    assert not runner.state.events  # 统计不进入聊天记录


def test_interrupted_stream_does_not_double_count_retry_usage():
    runner = AgentRunner(AgentState(messages=[{"role": "user", "content": "check"}]))

    def broken():
        yield SimpleNamespace(choices=[], usage=reported(1000, 800))
        raise RuntimeError("stream disconnected")

    with patch.object(runner, "_open_stream", return_value=broken()), pytest.raises(RuntimeError):
        runner._stream_llm_attempt()
    assert runner.state.cache_usage == {}
    run_stream(runner, [reported(1000, 800)])
    assert runner.state.cache_usage["request_count"] == 1


def test_cache_stats_survive_reopen_are_session_scoped_and_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "SESSIONS_ROOT", str(tmp_path / "sessions"))
    project = str(tmp_path / "project")
    runtime = AgentRuntime()
    sid = runtime.create_session(project)["session_id"]
    store = ss.SessionStore(project, sid)
    message = {"role": "user", "content": "check"}
    store.append_message(message)
    runner = AgentRunner(AgentState(project_dir=project, session_id=sid, messages=[message]))
    run_stream(runner, [reported(1000, 800)])
    before = dict(runner.state.cache_usage)
    reopened = AgentRuntime()
    context = reopened.status(project, sid)["context"]
    assert context["cache"] == before
    other = reopened.create_session(project)["session_id"]
    ss.SessionStore(project, other).append_message(message)
    assert reopened.status(project, other)["context"]["cache"] is None
    reopened.reset(project, sid)
    assert reopened.status(project, sid).get("context") is None


def test_summary_rebuild_preserves_session_cache_totals():
    messages = [{"role": "system", "content": "system"},
                {"role": "user", "content": "old task"},
                {"role": "assistant", "content": "latest reply"}]
    runner = AgentRunner(AgentState(messages=messages))
    runner.state.cache_usage = _add_cache_usage({}, (1000, 800))
    before = dict(runner.state.cache_usage)
    assert runner._begin_compaction(force=True)
    assert runner._rebuild_after_compaction("summary", "")
    assert runner.state.cache_usage == before
    assert runner.state.last_prompt_tokens == 0
