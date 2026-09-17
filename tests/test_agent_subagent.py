"""校对子代理：并行派发、受限工具集、报告回传。

锁住四件事：
1. 一个子代理能"读缓存 → 写 doub_content → 交报告"，写进去的是缓存条目的 doub_content；
2. **它改不了译文**：patch_transl_cache 换成收窄版（入参里没有 pre_dst/proofread_dst），
   handler 那层也只放得住 doub_content——模型硬塞译文字段会被跳过，且一个字都不会落盘；
3. 一批可以有多个子代理并行跑，各自锁自己的文件，互不干扰；失败不影响别人；
4. 上限 16、角色/文件/后端就绪这些入参校验。

LLM 是脚本化的（patch `_subagent_chat`），HTTP 走内存里的假缓存——不打真网络。
"""

import json
import threading
import unittest
import urllib.parse
from types import SimpleNamespace
from unittest.mock import patch

from GalTransl.Agent import runtime as rt
from GalTransl.Agent.runtime import (
    SUBAGENT_AGENT_PROOFREAD,
    SUBAGENT_MAX_TASKS,
    SUBAGENT_PATCHABLE_FIELDS,
    AgentToolError,
    _patchable_fields_text,
    _subagent_handlers,
    _subagent_patch_schema,
    _subagent_tools,
    _tool_patch_transl_cache,
    _tool_read_transl_cache,
    _tool_run_subagents,
)

ENTRY = {"index": 1, "post_src": "原文", "pre_dst": "译文", "problem": ""}


class _Fn:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _Call:
    """OpenAI tool_call 的最小替身（subagent 只用到 id / function.name / function.arguments）。"""

    def __init__(self, call_id: str, name: str, arguments: str) -> None:
        self.id = call_id
        self.type = "function"
        self.function = _Fn(name, arguments)


class _Parent:
    """主 Agent 的最小替身：子代理要的东西只有这几样（HTTP 走内存里的假缓存）。"""

    def __init__(self, files: dict[str, list[dict]] | None = None) -> None:
        self.state = SimpleNamespace(config_file_name="config.yaml", project_dir=r"C:\proj")
        self._openai_client = object()  # 子代理只判空
        self._model = "agent-model"
        self.stop_event = threading.Event()
        self._active_tool_call_id = "call-1"
        self.files = {name: [dict(e) for e in entries] for name, entries in (files or {}).items()}
        self.saves: list[dict] = []
        self.events: list[tuple[str, dict]] = []

    def _project_id(self) -> str:
        return "proj"

    def _http_get(self, path: str):
        if path.endswith("/cache"):
            return {"files": [{"name": name} for name in self.files]}
        name = urllib.parse.unquote(path.rsplit("/", 1)[-1])
        return {"entries": [dict(e) for e in self.files.get(name, [])]}

    def _http_post(self, path: str, body: dict):
        if not path.endswith("/cache/save"):
            raise AssertionError(f"意外的 POST：{path}")
        self.saves.append(body)
        self.files[body["filename"]] = [dict(e) for e in body["entries"]]
        return {"success": True, "entries": body["entries"]}

    def _emit(self, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))

    def types(self) -> list[str]:
        return [event_type for event_type, _ in self.events]


def _make_chat(script: list[tuple[str, list[_Call]]]):
    """按脚本依次返回响应；脚本用完后一直重复最后一条。"""

    calls = {"n": 0}

    def fake(_client, _model, _messages, _tools):
        idx = min(calls["n"], len(script) - 1)
        calls["n"] += 1
        content, tool_calls = script[idx]
        return content, tool_calls, rt.REASONING_FIELD_NAMES[0], ""

    return fake


def _run(parent: _Parent, args: dict, script: list[tuple[str, list[_Call]]]) -> dict:
    with patch.object(rt, "_subagent_chat", _make_chat(script)):
        return _tool_run_subagents(parent, args)


class ProofreadAgentFlowTests(unittest.TestCase):
    def test_reads_writes_doubts_and_reports(self) -> None:
        parent = _Parent({"a.json": [ENTRY]})
        script = [
            ("我先读缓存。", [_Call("c1", "read_transl_cache", '{"filename": "a.json", "index": "1"}')]),
            (
                "",
                [
                    _Call(
                        "c2",
                        "patch_transl_cache",
                        json.dumps(
                            {
                                "filename": "a.json",
                                "patches": [{"index": 1, "doub_content": "漏译：原文缺了「欧派」"}],
                            }
                        ),
                    )
                ],
            ),
            ("报告：1 条漏译，无其它问题。", []),
        ]

        out = _run(parent, {"tasks": [{"agent": SUBAGENT_AGENT_PROOFREAD, "file": "a.json"}]}, script)

        self.assertEqual(out["total"], 1)
        self.assertEqual(out["total_doubts"], 1)
        task = out["tasks"][0]
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["doubts"], [1])
        self.assertIn("1 条漏译", task["report"])
        self.assertEqual(task["turns"], 3)
        # 意见真的写进了缓存
        self.assertEqual(parent.files["a.json"][0]["doub_content"], "漏译：原文缺了「欧派」")
        # 只写意见不算"改了译文"：不盖 trans_by 章
        self.assertNotIn("trans_by", parent.files["a.json"][0])
        # 事件：开始/结束 + 逐步活动（界面据此把子代理挂到发起它的那行下）
        self.assertIn("subagent_start", parent.types())
        self.assertIn("subagent_done", parent.types())
        self.assertIn("subagent_tool_call", parent.types())
        start = next(data for event_type, data in parent.events if event_type == "subagent_start")
        self.assertEqual(start["parent_id"], "call-1")
        self.assertEqual(start["file"], "a.json")
        self.assertEqual(start["label"], "校对")

    def test_translation_edits_are_refused_and_nothing_is_written(self) -> None:
        """子代理硬塞 pre_dst：工具层拒掉（只允许 doub_content），一个字都不落盘。"""
        parent = _Parent({"a.json": [ENTRY]})
        script = [
            (
                "",
                [
                    _Call(
                        "c1",
                        "patch_transl_cache",
                        json.dumps(
                            {"filename": "a.json", "patches": [{"index": 1, "pre_dst": "我自己改"}]}
                        ),
                    )
                ],
            ),
            ("改不了，交报告。", []),
        ]

        out = _run(parent, {"tasks": [{"agent": SUBAGENT_AGENT_PROOFREAD, "file": "a.json"}]}, script)

        self.assertEqual(parent.saves, [])
        self.assertEqual(parent.files["a.json"][0]["pre_dst"], "译文")
        self.assertEqual(out["tasks"][0]["doubts"], [])
        failures = [
            data
            for event_type, data in parent.events
            if event_type == "subagent_tool_result" and data["ok"] is False
        ]
        self.assertEqual(len(failures), 1)
        self.assertIn("doub_content", failures[0]["error"])

    def test_unknown_tool_is_refused(self) -> None:
        parent = _Parent({"a.json": [ENTRY]})
        script = [
            ("", [_Call("c1", "start_translation", '{"translator": "ForGal-json"}')]),
            ("没有这个工具，交报告。", []),
        ]

        out = _run(parent, {"tasks": [{"agent": SUBAGENT_AGENT_PROOFREAD, "file": "a.json"}]}, script)

        self.assertEqual(parent.saves, [])
        self.assertEqual(out["tasks"][0]["status"], "done")
        refused = [
            data
            for event_type, data in parent.events
            if event_type == "subagent_tool_result" and data["ok"] is False
        ]
        self.assertIn("子代理没有这个工具", refused[0]["error"])

    def test_llm_failure_is_isolated_to_that_subagent(self) -> None:
        parent = _Parent({"a.json": [ENTRY], "b.json": [ENTRY]})
        calls = {"n": 0}

        def flaky(_client, _model, messages, _tools):
            calls["n"] += 1
            if "a.json" in json.dumps(messages, ensure_ascii=False):
                raise RuntimeError("boom")
            return "报告：没问题。", [], rt.REASONING_FIELD_NAMES[0], ""

        with patch.object(rt, "_subagent_chat", flaky):
            out = _tool_run_subagents(
                parent,
                {
                    "tasks": [
                        {"agent": SUBAGENT_AGENT_PROOFREAD, "file": "a.json"},
                        {"agent": SUBAGENT_AGENT_PROOFREAD, "file": "b.json"},
                    ]
                },
            )

        statuses = {row["file"]: row["status"] for row in out["tasks"]}
        self.assertEqual(statuses, {"a.json": "failed", "b.json": "done"})
        self.assertIn("boom", out["tasks"][0]["error"])


class SubagentToolScopeTests(unittest.TestCase):
    """"改不了译文"是结构保证，不是提示词自觉。"""

    def test_patch_schema_has_no_translation_fields(self) -> None:
        schema = _subagent_patch_schema()
        patches = schema["function"]["parameters"]["properties"]["patches"]["items"]["properties"]
        self.assertEqual(set(patches), {"index", "doub_content"})
        self.assertIn("只能写 doub_content", schema["function"]["description"])

    def test_tool_table_has_no_delegation_or_write_tools(self) -> None:
        names = {str((tool.get("function") or {}).get("name") or "") for tool in _subagent_tools()}
        self.assertEqual(
            names,
            {"read_transl_cache", "search_transl_cache", "list_problems", "get_name_table",
             "read_guideline", "patch_transl_cache"},
        )
        # 不会递归、也拿不到改配置/字典/启动任务的工具
        for forbidden in ("run_subagents", "save_dict", "update_project_config", "start_translation"):
            self.assertNotIn(forbidden, names)

    def test_handler_whitelist_only_allows_doub_content(self) -> None:
        parent = _Parent({"a.json": [ENTRY]})
        handler = _subagent_handlers()["patch_transl_cache"]

        with self.assertRaises(AgentToolError) as ctx:
            handler(parent, {"filename": "a.json", "patches": [{"index": 1, "pre_dst": "改"}]})
        self.assertIn("只允许 doub_content", str(ctx.exception))
        self.assertEqual(parent.saves, [])

    def test_patchable_text_for_subagents(self) -> None:
        self.assertEqual(_patchable_fields_text(SUBAGENT_PATCHABLE_FIELDS), "doub_content")
        self.assertEqual(_patchable_fields_text(), "pre_dst / proofread_dst / doub_content")

    def test_main_agent_can_still_write_translations(self) -> None:
        """同一个 handler 在主 Agent 那边不受收窄影响。"""
        parent = _Parent({"a.json": [ENTRY]})
        out = _tool_patch_transl_cache(
            parent, {"filename": "a.json", "patches": [{"index": 1, "pre_dst": "新译文"}]}
        )
        self.assertEqual(out["updated"], 1)
        self.assertEqual(parent.files["a.json"][0]["pre_dst"], "新译文")
        self.assertEqual(parent.files["a.json"][0]["trans_by"], "agent-model")

    def test_read_cache_still_works_for_subagents(self) -> None:
        parent = _Parent({"a.json": [ENTRY]})
        out = _tool_read_transl_cache(parent, {"filename": "a.json", "index": "1"})
        self.assertEqual(out["entries"][0]["pre_dst"], "译文")


class RunSubagentsValidationTests(unittest.TestCase):
    def test_rejects_more_than_the_cap(self) -> None:
        parent = _Parent({"a.json": [ENTRY]})
        with self.assertRaises(AgentToolError) as ctx:
            _tool_run_subagents(parent, {"tasks": [{"agent": "proofread", "file": "a.json"}] * (SUBAGENT_MAX_TASKS + 1)})
        self.assertIn(str(SUBAGENT_MAX_TASKS), str(ctx.exception))

    def test_rejects_unknown_agent_and_missing_file(self) -> None:
        parent = _Parent({"a.json": [ENTRY]})
        for task in ({"agent": "reviewer", "file": "a.json"}, {"agent": "proofread", "file": "  "}):
            with self.assertRaises(AgentToolError):
                _tool_run_subagents(parent, {"tasks": [task]})

    def test_rejects_empty_tasks(self) -> None:
        parent = _Parent()
        with self.assertRaises(AgentToolError):
            _tool_run_subagents(parent, {"tasks": []})

    def test_requires_a_ready_backend(self) -> None:
        parent = _Parent({"a.json": [ENTRY]})
        parent._openai_client = None
        with self.assertRaises(AgentToolError):
            _tool_run_subagents(parent, {"tasks": [{"agent": "proofread", "file": "a.json"}]})

    def test_stop_signal_marks_unfinished_tasks(self) -> None:
        parent = _Parent({"a.json": [ENTRY]})
        parent.stop_event.set()
        script = [("", [_Call("c1", "read_transl_cache", '{"filename": "a.json"}')]), ("报告", [])]

        out = _run(parent, {"tasks": [{"agent": SUBAGENT_AGENT_PROOFREAD, "file": "a.json"}]}, script)

        self.assertEqual(out["tasks"][0]["status"], "stopped")
        self.assertIn("停止", out["tasks"][0]["error"])


if __name__ == "__main__":
    unittest.main()
