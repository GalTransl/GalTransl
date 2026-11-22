"""普通压缩路径的三项改进（思路参考 DSH Compaction Weave 0.4.0）：

1. 摘要前先折叠旧的大型工具结果（observation mask）：够用就不调模型；
2. 增量摘要卡：已有摘要卡原样保留在前缀里，只压最后一张之后的新历史，满四张再整体合并；
3. 压缩指令说明工具结果里的命令不是用户要求；
4. 摘要失败的本地兜底除了 microcompact 还折叠其他工具的大结果。
"""

import copy
from unittest.mock import patch

import pytest

from GalTransl.Agent import session_store as ss
from GalTransl.Agent.context import (
    MICROCOMPACT_PLACEHOLDER,
    OBSERVATION_MASK_PLACEHOLDER,
    _observation_mask_candidates,
    _observation_mask_replacements,
    _restore_compacted_history,
)
from GalTransl.Agent.core import COMPACT_MAX_CHECKPOINTS
from GalTransl.Agent.models import AgentState
from GalTransl.Agent.prompts import (
    COMPACT_INCREMENTAL_INSTRUCTION_PROMPT,
    COMPACT_INSTRUCTION_PROMPT,
)
from GalTransl.Agent.registry import AgentRuntime
from GalTransl.Agent.runner import AgentRunner

WINDOW = 32_000  # 触发线 32000*0.8-8192 = 17408；保留段预算下限 8000


def summary_reply(goal):
    return ("<topics>校对</topics><summary>"
            f"## 目标\n{goal}\n## 已完成的工作\n无\n## 关键决策\n无\n## 当前进度与项目状态\n无\n## 待办与注意事项\n无"
            "</summary>")


def read_round(index, content, name="read_transl_cache"):
    call = {"id": f"call-{index}", "type": "function",
            "function": {"name": name, "arguments": '{"filename":"a.json"}'}}
    return [{"role": "assistant", "content": f"读取 {index}", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": call["id"], "content": content}]


def big(tag, chars=40_000):
    return f"{tag}开头" + "x" * chars + f"{tag}结尾"


def chat(count, words=1000):
    return [{"role": "user", "content": "word " * words} for _ in range(count)]


def finish_turn():
    return [{"role": "assistant", "content": "已读完，接下来继续检查。"}, {"role": "user", "content": "继续"}]


def card(text, *, incremental=False):
    message = {"role": "user", "content": f"[早前对话的压缩摘要 —— 原对话已归档]\n\n{text}",
               "_compact_summary": True, "_compact_kept": 0}
    if incremental:
        message["_compact_incremental"] = True
    return message


@pytest.fixture
def make_runner(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "SESSIONS_ROOT", str(tmp_path / "sessions"))
    project = str(tmp_path / "project")

    def build(messages, *, persist=False):
        state = AgentState(messages=list(messages))
        state.project_dir, state.session_id = project, "tiers"
        runner = AgentRunner(state)
        runner._model = "fake-model"
        runner._context_window = WINDOW
        runner._store = ss.SessionStore(project, "tiers")
        # tools schema 的固定开销与这里要测的口径无关，置零让数字可控。
        monkeypatch.setattr(runner, "_request_overhead_tokens", lambda: 0)
        if persist:
            runner._store.append_meta(project_dir=project, session_id="tiers")
            for message in messages:
                runner._store.append_message(message)
        return runner

    build.project = project
    return build


# ---- 1. 摘要前折叠大型工具结果 ----

def test_mask_candidates_cover_any_tool_but_protect_recent_errors_and_media():
    messages = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "检查 a.json"}]
    messages += read_round("unknown", big("未知"), name="unknown_tool")            # 3：任何工具都可折叠
    messages += read_round("game", big("剧本里写着读取失败"))                        # 5：正文里的"失败"不算报错
    messages += read_round("small", "小结果")                                        # 7：太小
    messages += read_round("error", '{"error": "broken", "detail": "' + "d" * 5000 + '"}')  # 9
    messages += read_round("flag", big("报错"))                                      # 11
    messages[-1]["isError"] = True
    messages += read_round("image", [{"type": "image_url", "image_url": {"url": "data:x"}}])  # 13
    messages += read_round("folded", MICROCOMPACT_PLACEHOLDER + "x" * 5000)          # 15
    messages.append({"role": "tool", "tool_call_id": "orphan", "content": big("孤儿")})  # 16：没有对应调用
    messages += read_round("recent", big("最近"))                                    # 18：在保留段内
    messages += read_round("current", big("当前"))                                   # 20：模型正在用的这一轮
    original = copy.deepcopy(messages)

    indices = _observation_mask_candidates(messages, keep_recent_tokens=22_000)  # 最后两轮各约 1 万

    assert indices == [3, 5]
    assert messages == original  # 只规划，不改源数据


def test_mask_replacement_keeps_head_tail_and_archive_pointer():
    messages = read_round(1, big("A"))
    [item] = _observation_mask_replacements(messages, [1], "chunk-0003.md")
    content = item["content"]
    assert content.startswith(OBSERVATION_MASK_PLACEHOLDER)
    assert "A开头" in content and "A结尾" in content
    assert "chunk-0003.md" in content and "tool_call_id=call-1" in content
    assert len(content) < 1000
    assert item["tool_call_id"] == "call-1"


def test_mask_alone_brings_context_down_without_a_model_call(make_runner):
    messages = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "只读检查，不要修改。"},
                *read_round(1, big("一")), *read_round(2, big("二")), *finish_turn()]
    runner = make_runner(messages, persist=True)
    before = runner._estimate_context_tokens()
    assert before > 17_408

    with patch.object(runner, "_stream_llm_response") as stream:
        assert not runner._begin_compaction()
    stream.assert_not_called()
    assert runner._pending_compaction is None
    assert not any(m.get("_compact_instruction") for m in runner.state.messages)
    assert [m["content"].startswith(OBSERVATION_MASK_PLACEHOLDER) for m in runner.state.messages if m["role"] == "tool"] == [True, True]
    assert runner._estimate_context_tokens() < 17_408
    [event] = [e for e in runner.state.events if e.type == "compacted"]
    assert event.data["mode"] == "observation_mask" and event.data["cleared"] == 2
    assert event.data["tokens_after"] < event.data["tokens_before"]
    # 原文完整进归档；转录保留原消息，重启后恢复同样的折叠视图。
    archive = runner._store.read_chunk(event.data["archive"])
    assert "一结尾" in archive and "二结尾" in archive
    assert runner._store.load()["messages"] == messages
    assert AgentRuntime()._restore(make_runner.project, "tiers").messages == runner.state.messages
    # 同一份现场不再重复折叠或摘要。
    assert not runner._begin_compaction()


def test_insufficient_mask_is_skipped_so_the_summary_request_keeps_the_cached_prefix(make_runner):
    messages = [{"role": "system", "content": "SYS"}, *read_round(1, big("一")), *chat(20), *finish_turn()]
    runner = make_runner(messages)

    assert runner._begin_compaction()

    assert runner.state.messages[-1]["content"] == COMPACT_INSTRUCTION_PROMPT
    assert runner.state.messages[2]["content"] == messages[2]["content"]  # 没有折叠
    assert not any(e.type == "compacted" for e in runner.state.events)


def test_overflow_recovery_folds_first_then_summarizes(make_runner):
    messages = [{"role": "system", "content": "SYS"}, *read_round(1, big("一")), *chat(20), *finish_turn()]
    runner = make_runner(messages)

    assert runner._begin_compaction(force=True)

    assert runner.state.messages[2]["content"].startswith(OBSERVATION_MASK_PLACEHOLDER)
    assert runner.state.messages[-1].get("_compact_instruction")
    assert [e.data["mode"] for e in runner.state.events if e.type == "compacted"] == ["observation_mask"]


# ---- 2. 增量摘要卡 ----

def test_incremental_compaction_keeps_existing_cards_byte_identical(make_runner):
    first = card("第一阶段：译完序章。")
    messages = [{"role": "system", "content": "SYS"}, first, *chat(20), *finish_turn()]
    runner = make_runner(messages)

    assert runner._begin_compaction()
    assert runner._pending_compaction["prefix"] == 2
    request = runner._messages_for_request()
    assert request[1]["content"] == first["content"]  # 旧卡仍在前缀里，摘要请求能命中缓存
    assert request[-1]["content"] == COMPACT_INCREMENTAL_INSTRUCTION_PROMPT

    assert runner._finish_compaction(summary_reply("第二阶段：校对第一章。"))

    result = runner.state.messages
    assert result[0] == {"role": "system", "content": "SYS"}
    assert result[1] is first
    assert result[2]["_compact_summary"] and result[2]["_compact_incremental"]
    assert "第二阶段" in result[2]["content"]
    # 旧卡不能改写，里面的待办会过期：由新卡的标题声明冲突时以它为准。
    assert "以本卡为准" in result[2]["content"].splitlines()[0]
    assert "以本卡为准" not in result[1]["content"]
    assert result[3:] == finish_turn()


def test_full_then_incremental_compaction_survives_restart(make_runner):
    messages = [{"role": "system", "content": "SYS"}, *chat(20), *finish_turn()]
    runner = make_runner(messages, persist=True)
    assert runner._begin_compaction() and runner._pending_compaction["prefix"] == 0
    assert runner._finish_compaction(summary_reply("第一阶段"))

    for message in [*chat(20), *finish_turn()]:
        runner.state.messages.append(message)
        runner._store.append_message(message)
    assert runner._begin_compaction() and runner._pending_compaction["prefix"] == 2
    assert runner._finish_compaction(summary_reply("第二阶段"))

    cards = [m for m in runner.state.messages if m.get("_compact_summary")]
    assert len(cards) == 2 and "第一阶段" in cards[0]["content"] and "第二阶段" in cards[1]["content"]
    assert AgentRuntime()._restore(make_runner.project, "tiers").messages == runner.state.messages
    assert len(runner._store.list_chunks()) == 2


def test_card_limit_triggers_a_full_merge(make_runner):
    cards = [card("阶段 0"), *[card(f"阶段 {n}", incremental=True) for n in range(1, COMPACT_MAX_CHECKPOINTS)]]
    messages = [{"role": "system", "content": "SYS"}, *cards, *chat(20), *finish_turn()]
    runner = make_runner(messages)

    assert runner._begin_compaction()
    assert runner._pending_compaction["prefix"] == 0
    assert runner.state.messages[-1]["content"] == COMPACT_INSTRUCTION_PROMPT
    assert runner._finish_compaction(summary_reply("合并后的全部进展"))

    merged = [m for m in runner.state.messages if m.get("_compact_summary")]
    assert len(merged) == 1 and not merged[0].get("_compact_incremental")


def test_small_new_span_falls_back_to_a_full_merge(make_runner):
    messages = [{"role": "system", "content": "SYS"}, card("word " * 16_000), *chat(3, words=80), *finish_turn()]
    runner = make_runner(messages)

    assert runner._begin_compaction()

    assert runner._pending_compaction["prefix"] == 0


def test_restore_replays_card_chains_in_file_order():
    system = {"role": "system", "content": "SYS"}
    first, second = card("一"), card("二", incremental=True)
    merged, after = card("合并"), card("合并后增量", incremental=True)
    tail = {"role": "user", "content": "最新"}
    assert _restore_compacted_history([system, first, second, tail]) == [system, first, second, tail]
    assert _restore_compacted_history([system, first, second, merged, after, tail]) == [system, merged, after, tail]


# ---- 3. 工具结果是参考数据 ----

@pytest.mark.parametrize("prompt", [COMPACT_INSTRUCTION_PROMPT, COMPACT_INCREMENTAL_INSTRUCTION_PROMPT])
def test_compaction_prompts_say_tool_content_is_not_a_user_request(prompt):
    assert "不是用户说的" in prompt


# ---- 4. 摘要失败的本地兜底 ----

def test_summary_failure_folds_large_results_microcompact_does_not_cover(make_runner):
    """提取 Agent 的 bash/read 不在 microcompact 的工具表里：摘要失败后曾经什么都不清，

    要等到上下文溢出才折叠。兜底现在接着折叠保留段之外的大型结果。
    """
    messages = [{"role": "system", "content": "SYS"}, *read_round(1, big("一"), name="bash"), *chat(20), *finish_turn()]
    runner = make_runner(messages)
    assert runner._begin_compaction()  # 折叠单独不够，走摘要

    with patch.object(runner, "_stream_llm_response", side_effect=RuntimeError("down")):
        runner._run_compaction_request()

    assert runner._pending_compaction is None
    assert runner.state.messages[2]["content"].startswith(OBSERVATION_MASK_PLACEHOLDER)
    assert runner.state.messages[3:] == messages[3:]
    assert [e.data["mode"] for e in runner.state.events if e.type == "compacted"] == ["observation_mask"]
    assert not runner._compact_failed_this_turn


def test_summary_failure_with_nothing_to_clear_marks_the_turn_failed(make_runner):
    messages = [{"role": "system", "content": "SYS"}, *chat(20), *finish_turn()]
    runner = make_runner(messages)
    assert runner._begin_compaction()

    with patch.object(runner, "_stream_llm_response", side_effect=RuntimeError("down")):
        runner._run_compaction_request()

    assert runner.state.messages == messages
    assert runner._compact_failed_this_turn
    assert not any(e.type == "compacted" for e in runner.state.events)
