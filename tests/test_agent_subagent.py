"""子代理：并行派发、按角色受限的工具集、报告回传。

两个角色（见 runtime.SUBAGENT_ROLES）：
- **校对（proofread）**：读缓存 → 写 proofread_comment → 交报告；**改不了译文**（patch_transl_cache
  换成收窄版，入参里没有 pre_dst/proofread_dst，handler 那层也只放得住 proofread_comment）；
- **原文探索（explore）**：只读原文与 GPT 字典、**不写任何文件**，报告里给字典候选与规范建议，
  由主 Agent 汇总后落地。

锁住的事：一个子代理能按角色干完活并交报告；越权（改译文、写文件、用白名单外的工具）一个字都
落不了盘；一批可以多个并行、各自锁自己的文件、失败互不影响；上限 16、角色/文件/后端就绪这些
入参校验；子代理调用不过权限门禁；file="*" 的自动均分（按角色把全部文件平分给同批任务）；
file 的选择器（list/glob/regex/select/random）与 count×indexes 的正交组合。

LLM 是脚本化的（patch `_subagent_chat`），HTTP 走内存里的假缓存与假字典——不打真网络。
"""

import json
import tempfile
import threading
import unittest
import urllib.parse
from types import SimpleNamespace
from unittest.mock import patch

from GalTransl.Agent import runtime as rt
from GalTransl.Agent import session_store as ss
from GalTransl.Agent.runtime import (
    SUBAGENT_AGENT_EXPLORE,
    SUBAGENT_AGENT_PROOFREAD,
    SUBAGENT_MAX_TASKS,
    SUBAGENT_PATCHABLE_FIELDS,
    SUBAGENT_ROLES,
    AgentToolError,
    _patchable_fields_text,
    _render_tool_result_table,
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
    """主 Agent 的最小替身：子代理要的东西只有这几样（HTTP 走内存里的假缓存）。

    permission_checks 记「主 Agent 的门禁被问过几次」：子代理的工具调用不该走到那里
    （见 SubagentPermissionTests）。档位固定成最严的 ask——子代理照样直接执行。
    """

    def __init__(
        self,
        files: dict[str, list[dict]] | None = None,
        inputs: dict[str, list[dict]] | None = None,
        dicts: dict[str, dict] | None = None,
        problems: list[dict] | None = None,
    ) -> None:
        self.state = SimpleNamespace(
            config_file_name="config.yaml", project_dir=r"C:\proj", permission_mode="ask"
        )
        self._openai_client = object()  # 子代理只判空
        self._model = "agent-model"
        self.stop_event = threading.Event()
        self._active_tool_call_id = "call-1"
        self.files = {name: [dict(e) for e in entries] for name, entries in (files or {}).items()}
        # 原文（输入目录）与 GPT 字典：原文探索子代理要这两样
        self.inputs = {name: [dict(e) for e in entries] for name, entries in (inputs or {}).items()}
        self.dicts = dict(dicts or {})
        # 问题清单：file:"select:has_problem" 直接吃它（list_problems 的同源数据）
        self.problems = [dict(p) for p in (problems or [])]
        self.saves: list[dict] = []
        self.events: list[tuple[str, dict]] = []
        self.permission_checks: list[str] = []

    def _project_id(self) -> str:
        return "proj"

    def _require_permission(self, name: str, _args: dict) -> None:
        """真 AgentRunner 在这里可能挂起等用户点；子代理不该碰它，碰了就记一笔。"""
        self.permission_checks.append(name)

    def _http_get(self, path: str):
        if path.endswith("/cache"):
            return {
                "files": [
                    {"name": name, "entry_count": len(entries)} for name, entries in self.files.items()
                ]
            }
        if "/problems" in path:
            return {"problems": [dict(p) for p in self.problems], "total": len(self.problems)}
        if "/dictionary/project" in path:
            return {"dict_contents": self.dicts, "gpt_dict_files": list(self.dicts)}
        if "/files?" in path:
            return {
                "input_files": [
                    {"name": name, "is_file": True, "size": 10, "sentences": len(entries)}
                    for name, entries in self.inputs.items()
                ],
                "cache_files": [{"name": name} for name in self.files],
            }
        if "/input/" in path:
            name = urllib.parse.unquote(path.split("/input/")[1].split("?")[0])
            return {"entries": [dict(e) for e in self.inputs.get(name, [])]}
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
    def test_reads_writes_proofread_comments_and_reports(self) -> None:
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
                                "patches": [{"index": 1, "proofread_comment": "漏译：原文缺了「欧派」"}],
                            }
                        ),
                    )
                ],
            ),
            ("报告：1 条漏译，无其它问题。", []),
        ]

        out = _run(parent, {"tasks": [{"agent": SUBAGENT_AGENT_PROOFREAD, "file": "a.json"}]}, script)

        self.assertEqual(out["total"], 1)
        self.assertEqual(out["total_proofread_comment"], 1)
        task = out["tasks"][0]
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["proofread_comment"], [{"file": "a.json", "index": 1}])
        self.assertEqual(task["files"], ["a.json"])
        self.assertIn("1 条漏译", task["report"])
        self.assertEqual(task["turns"], 3)
        # 意见真的写进了缓存
        self.assertEqual(parent.files["a.json"][0]["proofread_comment"], "漏译：原文缺了「欧派」")
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
        # 绝对时间戳：start/done 是持久事件，界面刷新/切页重放后要按它还原耗时区间——
        # 拿"事件到达时间"会让进行中的计时每次重建都归零。
        self.assertIsInstance(start["started_at"], float)
        done = next(data for event_type, data in parent.events if event_type == "subagent_done")
        self.assertIsInstance(done["finished_at"], float)
        self.assertGreaterEqual(done["finished_at"], start["started_at"])

    def test_translation_edits_are_refused_and_nothing_is_written(self) -> None:
        """子代理硬塞 pre_dst：工具层拒掉（只允许 proofread_comment），一个字都不落盘。"""
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
        self.assertEqual(out["tasks"][0]["proofread_comment"], [])
        failures = [
            data
            for event_type, data in parent.events
            if event_type == "subagent_tool_result" and data["ok"] is False
        ]
        self.assertEqual(len(failures), 1)
        self.assertIn("proofread_comment", failures[0]["error"])

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


class SubagentRetryTests(unittest.TestCase):
    """子代理与主 Agent 同规则重试：一次网络抖动不该让整份报告作废。

    只重试瞬态错误（超时/限流/5xx/连接断），鉴权、参数、上下文超限不重试；
    退避期间父回合被停止则立刻收尾。退避时长在测试里 patch 成 0，不真等。
    """

    def _run_one(self, parent: _Parent, chat) -> dict:
        with (
            patch.object(rt, "_subagent_chat", chat),
            patch.object(rt, "_llm_retry_delay_ms", lambda attempt, info: 0),
        ):
            return _tool_run_subagents(
                parent, {"tasks": [{"agent": SUBAGENT_AGENT_PROOFREAD, "file": "a.json"}]}
            )

    def test_transient_error_is_retried_until_it_succeeds(self) -> None:
        parent = _Parent({"a.json": [ENTRY]})
        calls = {"n": 0}

        def flaky(_client, _model, _messages, _tools):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("Request timed out.")
            return "报告：没问题。", [], rt.REASONING_FIELD_NAMES[0], ""

        out = self._run_one(parent, flaky)

        self.assertEqual(out["tasks"][0]["status"], "done")
        self.assertEqual(calls["n"], 2)
        # 界面上能看到它在退避重试（瞬态事件，不进长期窗口）
        retry = next(data for kind, data in parent.events if kind == "subagent_retry")
        self.assertEqual(retry["attempt"], 1)
        self.assertEqual(retry["max_attempts"], rt.LLM_MAX_RETRIES)

    def test_non_retriable_error_fails_immediately(self) -> None:
        """鉴权/参数这类错误重试多少次都一样，一次就收。"""
        parent = _Parent({"a.json": [ENTRY]})
        calls = {"n": 0}

        def boom(_client, _model, _messages, _tools):
            calls["n"] += 1
            raise PermissionError("invalid api key")

        out = self._run_one(parent, boom)

        self.assertEqual(out["tasks"][0]["status"], "failed")
        self.assertEqual(calls["n"], 1)
        self.assertNotIn("subagent_retry", parent.types())

    def test_gives_up_after_the_retry_budget(self) -> None:
        parent = _Parent({"a.json": [ENTRY]})
        calls = {"n": 0}

        def always_timeout(_client, _model, _messages, _tools):
            calls["n"] += 1
            raise TimeoutError("Request timed out.")

        out = self._run_one(parent, always_timeout)

        self.assertEqual(out["tasks"][0]["status"], "failed")
        self.assertEqual(calls["n"], rt.LLM_MAX_RETRIES + 1)  # 首次 + 重试
        self.assertIn("已重试", out["tasks"][0]["error"])

    def test_stop_during_backoff_stops_the_subagent(self) -> None:
        """父回合被停止时不继续重试，按 stopped 收尾（别在无人值守时干等）。"""
        parent = _Parent({"a.json": [ENTRY]})

        def timeout_and_stop(_client, _model, _messages, _tools):
            parent.stop_event.set()  # 失败的同时用户点了停止
            raise TimeoutError("Request timed out.")

        out = self._run_one(parent, timeout_and_stop)

        self.assertEqual(out["tasks"][0]["status"], "stopped")


class SubagentStepPersistenceTests(unittest.TestCase):
    """子代理的逐步活动要落盘。

    它们量太大、不能进内存 events 窗口（否则把主转录挤出去），但必须写进会话 JSONL：
    切页/刷新后前端重建转录时要靠它还原子代理的动作，否则展开只剩 start/done 两条。
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="agent-sub-persist-root-")
        patcher = patch.object(ss, "SESSIONS_ROOT", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_steps_are_persisted_but_stay_out_of_the_memory_window(self) -> None:
        project = tempfile.mkdtemp(prefix="agent-sub-persist-proj-")
        state = rt.AgentState(project_dir=project, session_id="sess-sub")
        runner = rt.AgentRunner(state)

        runner._emit("subagent_message", {"id": "d1", "round": 1, "text": "在读原文"})
        runner._emit("subagent_tool_result", {"id": "d1", "name": "read_input_file", "ok": True})
        runner._emit("content_delta", {"delta": "x"})  # 对照组：纯瞬态，不该落盘

        # 内存窗口里都没有（都是瞬态事件）
        self.assertEqual(list(state.events), [])
        # 落盘：子代理步骤写进去了，流式增量没有
        with open(runner._store.path, encoding="utf-8") as handle:
            raw = handle.read()
        self.assertIn("subagent_message", raw)
        self.assertIn("subagent_tool_result", raw)
        self.assertNotIn("content_delta", raw)
        # 转录回放能读回来——切页/刷新后界面就是走这条路重建的
        types = [event["type"] for event in ss.read_transcript(project, "sess-sub")]
        self.assertIn("subagent_message", types)
        self.assertIn("subagent_tool_result", types)

    def test_replay_caps_steps_per_subagent_without_crowding_out_the_chat(self) -> None:
        """每个子代理回放最多 SUBAGENT_STEP_REPLAY_LIMIT 步，且不挤占主转录窗口。"""
        project = tempfile.mkdtemp(prefix="agent-sub-persist-cap-")
        state = rt.AgentState(project_dir=project, session_id="sess-cap")
        runner = rt.AgentRunner(state)

        runner._emit("user_message", {"message": "开始"})
        runner._emit(
            "subagent_start",
            {"id": "d1", "agent": "proofread", "label": "校对", "file": "a.json"},
        )
        for i in range(ss.SUBAGENT_STEP_REPLAY_LIMIT + 20):
            runner._emit(
                "subagent_tool_call",
                {"id": "d1", "name": "read_transl_cache", "tool_call_id": f"c{i}"},
            )
        runner._emit("subagent_done", {"id": "d1", "status": "done", "report": "好了"})

        types = [event["type"] for event in ss.read_transcript(project, "sess-cap")]

        self.assertEqual(types.count("subagent_tool_call"), ss.SUBAGENT_STEP_REPLAY_LIMIT)
        # start/done 与主转录都在（它们占主窗口，不该被子代理步骤挤出去）
        self.assertIn("subagent_start", types)
        self.assertIn("subagent_done", types)
        self.assertIn("user_message", types)
        # 步骤按 step 插回原位置：done 最大，排最后
        self.assertEqual(types[-1], "subagent_done")


class SubagentToolScopeTests(unittest.TestCase):
    """"改不了译文"是结构保证，不是提示词自觉。"""

    def test_patch_schema_has_no_translation_fields(self) -> None:
        schema = _subagent_patch_schema()
        patches = schema["function"]["parameters"]["properties"]["patches"]["items"]["properties"]
        # file 留着：负责一组文件时一次把意见写完（跨文件批量），译文两列照旧摘掉
        self.assertEqual(set(patches), {"file", "index", "proofread_comment"})
        self.assertNotIn("pre_dst", schema["function"]["parameters"]["properties"])
        # 顶层 clear_comment 也摘掉：那是主 Agent 复核完清批注用的，子代理只写意见
        # （它若能把成批批注清空，等于把别人刚写下、还没处理的意见抹了）
        self.assertNotIn("clear_comment", schema["function"]["parameters"]["properties"])
        self.assertIn("只能写 proofread_comment", schema["function"]["description"])

    def test_tool_table_has_no_delegation_or_write_tools(self) -> None:
        names = {
            str((tool.get("function") or {}).get("name") or "")
            for tool in _subagent_tools(SUBAGENT_AGENT_PROOFREAD)
        }
        self.assertEqual(
            names,
            {"read_transl_cache", "search_transl_cache", "list_problems", "get_name_table",
             "read_guideline", "patch_transl_cache"},
        )
        # 不会递归、也拿不到改配置/字典/启动任务的工具
        for forbidden in ("run_subagents", "save_dict", "update_project_config", "start_translation"):
            self.assertNotIn(forbidden, names)

    def test_handler_whitelist_only_allows_proofread_comment(self) -> None:
        parent = _Parent({"a.json": [ENTRY]})
        handler = _subagent_handlers(SUBAGENT_AGENT_PROOFREAD)["patch_transl_cache"]

        with self.assertRaises(AgentToolError) as ctx:
            handler(parent, {"filename": "a.json", "patches": [{"index": 1, "pre_dst": "改"}]})
        self.assertIn("只允许 proofread_comment", str(ctx.exception))
        self.assertEqual(parent.saves, [])

    def test_patchable_text_for_subagents(self) -> None:
        self.assertEqual(_patchable_fields_text(SUBAGENT_PATCHABLE_FIELDS), "proofread_comment")
        self.assertEqual(_patchable_fields_text(), "pre_dst / proofread_dst / proofread_comment")

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


class ExploreAgentTests(unittest.TestCase):
    """原文探索子代理：只看原文与 GPT 字典，不写任何文件，产出只有报告。

    定位是 GenDic 的补充（昵称 / 低频专有名词 / 特殊称呼）+ 翻译规范建议；报告回到主 Agent 后
    由主 Agent 落地（save_dict / write_project_guideline）。它很费 token，属于可选步骤——
    "派之前先用 ask_user 征得用户同意"是提示词行为，这里只钉代码侧的边界。
    """

    def _parent(self) -> _Parent:
        return _Parent(
            {"a.json": [ENTRY]},
            inputs={
                "src_a.json": [
                    {"index": 1, "post_src": "「お兄ちゃん」と妹が呼んだ。"},
                    {"index": 2, "post_src": "「兄貴、また負けたの？」"},
                ]
            },
            dicts={"gpt_dict": {"lines": ["多鲁德,ドルード"], "count": 1}},
        )

    def test_tool_table_is_read_only(self) -> None:
        names = {
            str((tool.get("function") or {}).get("name") or "")
            for tool in _subagent_tools(SUBAGENT_AGENT_EXPLORE)
        }
        self.assertEqual(
            names,
            {"list_input_files", "read_input_file", "search_input", "list_dict_files", "read_dict"},
        )
        for forbidden in ("patch_transl_cache", "read_transl_cache", "run_subagents", "start_translation"):
            self.assertNotIn(forbidden, names)

    def test_file_is_optional_unlike_proofread(self) -> None:
        parent = self._parent()
        with self.assertRaises(AgentToolError) as ctx:
            _tool_run_subagents(parent, {"tasks": [{"agent": SUBAGENT_AGENT_PROOFREAD}]})
        self.assertIn("缺 file", str(ctx.exception))

        out = _run(parent, {"tasks": [{"agent": SUBAGENT_AGENT_EXPLORE}]}, [("报告：字典没缺口。", [])])

        self.assertEqual(out["tasks"][0]["status"], "done")
        self.assertEqual(out["tasks"][0]["agent"], SUBAGENT_AGENT_EXPLORE)
        self.assertEqual(out["tasks"][0]["label"], "原文探索")
        self.assertEqual(out["tasks"][0]["proofread_comment"], [])

    def test_reads_source_and_dict_then_reports_candidates(self) -> None:
        parent = self._parent()
        report = (
            "## 字典候选\n"
            "お兄ちゃん → 哥哥 ｜ src_a.json#1 ｜ 妹妹对兄长的称呼，GenDic 多半不会收\n"
            "兄貴 → 大哥 ｜ src_a.json#2 ｜ 同上，语气更随便\n"
            "## 规范建议\n- 称呼：原文的「」保留到译文\n"
        )
        script = [
            ("", [_Call("c1", "list_dict_files", "{}")]),
            ("", [_Call("c2", "read_input_file", '{"filename": "src_a.json", "index": "1-2"}')]),
            (report, []),
        ]
        captured: list[list[dict]] = []
        base = _make_chat(script)

        def fake_chat(client, model, messages, tools):
            captured.append(messages)
            return base(client, model, messages, tools)

        with patch.object(rt, "_subagent_chat", fake_chat):
            out = _tool_run_subagents(parent, {"tasks": [{"agent": SUBAGENT_AGENT_EXPLORE}]})

        task = out["tasks"][0]
        self.assertIn("お兄ちゃん → 哥哥", task["report"])
        self.assertIn("规范建议", task["report"])
        self.assertEqual(task["tool_calls"], 2)
        # 一个文件都没写：字典与项目规范由主 Agent 汇总后落地
        self.assertEqual(parent.saves, [])
        # 用的是原文探索那套提示词与任务说明（不是校对那套）
        self.assertIn("原文探索子代理", captured[0][0]["content"])
        self.assertIn("负责的原文", captured[0][1]["content"])
        self.assertNotIn("proofread_comment", captured[0][1]["content"])
        failures = [
            data for kind, data in parent.events if kind == "subagent_tool_result" and not data["ok"]
        ]
        self.assertEqual(failures, [])

    def test_it_cannot_write_even_if_it_tries(self) -> None:
        """模型硬要写：白名单外一律拒，而且什么都落不了盘。"""
        parent = self._parent()
        script = [
            (
                "",
                [
                    _Call(
                        "c1",
                        "patch_transl_cache",
                        json.dumps(
                            {"filename": "a.json", "patches": [{"index": 1, "proofread_comment": "顺手写一条"}]}
                        ),
                    )
                ],
            ),
            ("写不了，交报告。", []),
        ]

        out = _run(parent, {"tasks": [{"agent": SUBAGENT_AGENT_EXPLORE}]}, script)

        self.assertEqual(parent.saves, [])
        self.assertEqual(out["tasks"][0]["proofread_comment"], [])
        refused = [
            data for kind, data in parent.events if kind == "subagent_tool_result" and not data["ok"]
        ]
        self.assertIn("子代理没有这个工具", refused[0]["error"])

    def test_report_cap_is_larger_than_proofread(self) -> None:
        """它交回来的就是字典候选清单，800 字符装不下（但仍有上限）。"""
        self.assertGreater(
            SUBAGENT_ROLES[SUBAGENT_AGENT_EXPLORE].report_chars,
            SUBAGENT_ROLES[SUBAGENT_AGENT_PROOFREAD].report_chars,
        )


class LockedFileTests(unittest.TestCase):
    """锁定文件（任务里的 file）由**工具层**保证，不只是提示词里的一句。

    校对：read/patch 只能碰自己那份缓存（否则两个子代理可能写同一条）；
    原文探索：list_input_files 只列它、read_input_file 只能读它。
    跨文件的 search_transl_cache / list_problems / get_name_table 不受影响，file 留空则照旧放开。
    """

    def _parent(self) -> _Parent:
        return _Parent(
            {"a.json": [ENTRY], "b.json": [ENTRY]},
            inputs={
                "src_a.json": [{"index": 1, "post_src": "原文 A"}],
                "src_b.json": [{"index": 1, "post_src": "原文 B"}],
            },
        )

    def test_explore_listing_only_shows_the_locked_file(self) -> None:
        parent = self._parent()
        handler = _subagent_handlers(SUBAGENT_AGENT_EXPLORE, "src_a.json")["list_input_files"]

        out = handler(parent, {})

        self.assertEqual([f["name"] for f in out["input_files"]], ["src_a.json"])
        self.assertEqual(out["count"], 1)
        self.assertIn("只派你看", out["note"])

    def test_explore_cannot_read_another_file(self) -> None:
        parent = self._parent()
        handler = _subagent_handlers(SUBAGENT_AGENT_EXPLORE, "src_a.json")["read_input_file"]

        self.assertEqual(
            handler(parent, {"filename": "src_a.json"})["entries"][0]["post_src"], "原文 A"
        )
        with self.assertRaises(AgentToolError) as ctx:
            handler(parent, {"filename": "src_b.json"})
        self.assertIn("只负责「src_a.json」", str(ctx.exception))

    def test_proofread_cannot_touch_another_cache_file(self) -> None:
        parent = self._parent()
        handlers = _subagent_handlers(SUBAGENT_AGENT_PROOFREAD, "a.json")

        handlers["read_transl_cache"](parent, {"filename": "a.json"})  # 自己那份没问题
        with self.assertRaises(AgentToolError):
            handlers["read_transl_cache"](parent, {"filename": "b.json"})
        with self.assertRaises(AgentToolError):
            handlers["patch_transl_cache"](
                parent, {"filename": "b.json", "patches": [{"index": 1, "proofread_comment": "越界"}]}
            )
        self.assertEqual(parent.saves, [])  # 一个字都没写进别的文件

    def test_unknown_locked_name_is_reported_not_silently_empty(self) -> None:
        """锁定的名字不在清单里（多半是文件名写错）：照旧全列，但要说清。"""
        parent = self._parent()
        handler = _subagent_handlers(SUBAGENT_AGENT_EXPLORE, "nope.json")["list_input_files"]

        out = handler(parent, {})

        self.assertEqual(len(out["input_files"]), 2)
        self.assertIn("不在原文清单里", out["note"])

    def test_unlocked_subagent_keeps_the_wide_view(self) -> None:
        """file 留空 = 不锁定，行为与以前一样（校对仍可读任意缓存文件）。"""
        parent = self._parent()
        handlers = _subagent_handlers(SUBAGENT_AGENT_PROOFREAD)

        handlers["read_transl_cache"](parent, {"filename": "b.json"})

    def test_locked_run_rejects_out_of_scope_reads_end_to_end(self) -> None:
        """整条链路：任务带 file → 子代理串到别的文件时拿到的是工具错误（不是静默读到）。"""
        parent = self._parent()
        script = [
            ("", [_Call("c1", "read_input_file", '{"filename": "src_b.json"}')]),
            ("报告：范围外读不到，按锁定的文件交。", []),
        ]

        out = _run(parent, {"tasks": [{"agent": SUBAGENT_AGENT_EXPLORE, "file": "src_a.json"}]}, script)

        self.assertEqual(out["tasks"][0]["status"], "done")
        refused = [
            data for kind, data in parent.events if kind == "subagent_tool_result" and not data["ok"]
        ]
        self.assertIn("只负责「src_a.json」", refused[0]["error"])


class AutoSplitTests(unittest.TestCase):
    """file="*" 的自动均分：同批**同角色**的 "*" 任务平分该角色的全部文件。

    - proofread 的候选是缓存文件（跳过条目为 0 的；.append.jsonl 本来就不是 .json）；
      explore 的候选是原文文件；
    - 本批里已具体点名的文件不会再分给 "*"（点名优先，避免两个子代理抢同一份）；
    - 文件比任务少时，没分到文件的任务直接跳过（空跑一轮照样烧 token），note 里要说清；
    - 一个任务可能领到一组文件：锁定范围跟着变一组，proofread_comment 也要带上文件名。
    """

    def test_split_files_evenly(self) -> None:
        # 256 个文件 16 份 → 每个 16 个（不重不漏、顺序不乱）
        files = [f"f{i:03d}.json" for i in range(256)]
        groups = rt._split_files_evenly(files, 16)
        self.assertEqual([len(group) for group in groups], [16] * 16)
        self.assertEqual([name for group in groups for name in group], files)
        # 除不尽：前面的多一个（10 个文件 3 份 → 4/3/3）
        self.assertEqual([len(g) for g in rt._split_files_evenly(files[:10], 3)], [4, 3, 3])
        # 文件比份数少：后面几份是空的（调用方据此跳过这些任务）
        self.assertEqual(rt._split_files_evenly(["a.json", "b.json"], 4), [["a.json"], ["b.json"], [], []])

    def test_star_splits_cache_files_among_proofread_tasks(self) -> None:
        parent = _Parent({f"c{i}.json": [ENTRY] for i in range(4)})
        captured: list[list[dict]] = []
        base = _make_chat([("报告：没问题。", [])])

        def fake_chat(client, model, messages, tools):
            captured.append(messages)
            return base(client, model, messages, tools)

        with patch.object(rt, "_subagent_chat", fake_chat):
            out = _tool_run_subagents(
                parent, {"tasks": [{"agent": SUBAGENT_AGENT_PROOFREAD, "file": "*"}] * 2}
            )

        self.assertEqual(out["total"], 2)
        first, second = out["tasks"]
        self.assertEqual(first["files"], ["c0.json", "c1.json"])
        self.assertEqual(second["files"], ["c2.json", "c3.json"])
        self.assertEqual(first["file"], "c0.json 等 2 个文件")  # 展示用的一行
        self.assertIn("自动均分", out["note"])
        self.assertIn("每个 2 个", out["note"])
        # 任务说明里是完整清单：子代理没有列表类工具，只能从这里知道自己负责哪些
        briefs = " | ".join(msgs[1]["content"] for msgs in captured)
        self.assertIn("c0.json、c1.json", briefs)
        self.assertIn("c2.json、c3.json", briefs)

    def test_star_respects_named_files(self) -> None:
        """点名优先：b.json 已具体派给一个任务，"*" 只分剩下的。"""
        parent = _Parent({name: [ENTRY] for name in ("a.json", "b.json", "c.json")})
        out = _run(
            parent,
            {
                "tasks": [
                    {"agent": SUBAGENT_AGENT_PROOFREAD, "file": "b.json"},
                    {"agent": SUBAGENT_AGENT_PROOFREAD, "file": "*"},
                ]
            },
            [("报告。", [])],
        )
        self.assertEqual(out["tasks"][0]["files"], ["b.json"])
        self.assertEqual(out["tasks"][1]["files"], ["a.json", "c.json"])

    def test_star_with_more_tasks_than_files_skips_the_extras(self) -> None:
        parent = _Parent({"a.json": [ENTRY]})
        out = _run(
            parent,
            {"tasks": [{"agent": SUBAGENT_AGENT_PROOFREAD, "file": "*"}] * 3},
            [("报告。", [])],
        )
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["skipped"], 2)
        self.assertEqual(out["tasks"][0]["files"], ["a.json"])
        self.assertIn("没分到文件", out["note"])

    def test_star_with_no_candidates_is_an_error(self) -> None:
        parent = _Parent()  # 一个缓存文件都没有
        with self.assertRaises(AgentToolError) as ctx:
            _tool_run_subagents(parent, {"tasks": [{"agent": SUBAGENT_AGENT_PROOFREAD, "file": "*"}]})
        self.assertIn("自动均分拿不到文件", str(ctx.exception))

    def test_star_ignores_empty_cache_files(self) -> None:
        """条目数为 0 的缓存文件不参与分派：派过去也没东西可校对。"""
        parent = _Parent({"a.json": [ENTRY], "empty.json": []})
        out = _run(
            parent,
            {"tasks": [{"agent": SUBAGENT_AGENT_PROOFREAD, "file": "*"}] * 2},
            [("报告。", [])],
        )
        self.assertEqual(out["tasks"][0]["files"], ["a.json"])
        self.assertEqual(out["skipped"], 1)

    def test_star_splits_input_files_for_explore(self) -> None:
        parent = _Parent(
            {},
            inputs={f"src_{i}.json": [{"index": 1, "post_src": "原文"}] for i in range(3)},
        )
        out = _run(
            parent,
            {"tasks": [{"agent": SUBAGENT_AGENT_EXPLORE, "file": "*"}] * 2},
            [("报告。", [])],
        )
        self.assertEqual(out["tasks"][0]["files"], ["src_0.json", "src_1.json"])
        self.assertEqual(out["tasks"][1]["files"], ["src_2.json"])

    def test_count_expands_one_task_into_several(self) -> None:
        """少写几条任务：一条 file:"*" + count:N 展开成 N 个子代理，brief 只写一遍。

        真实踩过：要求派 2 个 explore，模型因为不想把上千字的 brief 复制两份，只写了一条
        task、结果只派出去 1 个。count 就是给这种情况准备的。
        """
        parent = _Parent({f"c{i}.json": [ENTRY] for i in range(4)})

        out = _run(
            parent,
            {
                "tasks": [
                    {
                        "agent": SUBAGENT_AGENT_PROOFREAD,
                        "file": "*",
                        "count": 2,
                        "brief": "重点看漏译",
                    }
                ]
            },
            [("报告：没问题。", [])],
        )

        self.assertEqual(out["total"], 2)
        first, second = out["tasks"]
        self.assertEqual(first["files"], ["c0.json", "c1.json"])
        self.assertEqual(second["files"], ["c2.json", "c3.json"])
        self.assertIn("每个 2 个", out["note"])
        # 一份 brief 由展开出来的两个子代理共用（模型不用重复写）
        starts = [data for kind, data in parent.events if kind == "subagent_start"]
        self.assertEqual(len(starts), 2)
        self.assertTrue(all("重点看漏译" in data["brief"] for data in starts))

    def test_count_on_a_single_file_slices_its_index_range(self) -> None:
        """count 不再绑死在 "*" 上：点名单个文件也能切——按条数切成 N 段并行。

        真实场景：03_RE13.json 有 412 条，一个子代理啃完又慢又重，切 4 段给 4 个代理。
        """
        parent = _Parent({"big.json": [dict(ENTRY, index=i) for i in range(1, 5)]})
        out = _run(
            parent,
            {"tasks": [{"agent": SUBAGENT_AGENT_PROOFREAD, "file": "big.json", "count": 2}]},
            [("报告。", [])],
        )

        self.assertEqual(out["total"], 2)
        self.assertEqual([t["files"] for t in out["tasks"]], [["big.json"], ["big.json"]])
        self.assertEqual([t["indexes"] for t in out["tasks"]], ["1-2", "3-4"])  # 按条数均分

    def test_count_uses_indexes_as_the_slicing_window(self) -> None:
        """indexes 与 count 正交：只在前 200 条里切 4 段。"""
        parent = _Parent({"big.json": [dict(ENTRY, index=i) for i in range(1, 401)]})
        out = _run(
            parent,
            {
                "tasks": [
                    {
                        "agent": SUBAGENT_AGENT_PROOFREAD,
                        "file": "big.json",
                        "indexes": "1-200",
                        "count": 4,
                    }
                ]
            },
            [("报告。", [])],
        )

        self.assertEqual([t["indexes"] for t in out["tasks"]], ["1-50", "51-100", "101-150", "151-200"])

    def test_count_cannot_slice_more_than_the_entries(self) -> None:
        parent = _Parent({"a.json": [ENTRY]})  # 只有 1 条
        with self.assertRaises(AgentToolError) as ctx:
            _tool_run_subagents(
                parent,
                {"tasks": [{"agent": SUBAGENT_AGENT_PROOFREAD, "file": "a.json", "count": 2}]},
            )
        self.assertIn("切不成", str(ctx.exception))

    def test_rejects_bad_count_values(self) -> None:
        parent = _Parent({"a.json": [ENTRY]})
        for bad in (0, -1, "x", 2.5, True, SUBAGENT_MAX_TASKS + 1):
            with self.assertRaises(AgentToolError):
                _tool_run_subagents(
                    parent,
                    {"tasks": [{"agent": SUBAGENT_AGENT_PROOFREAD, "file": "*", "count": bad}]},
                )

    def test_expanded_total_respects_the_task_cap(self) -> None:
        """单条 count 合法、但展开后总量超上限时要拦下来。"""
        parent = _Parent({"a.json": [ENTRY]})
        with self.assertRaises(AgentToolError) as ctx:
            _tool_run_subagents(
                parent,
                {"tasks": [{"agent": SUBAGENT_AGENT_PROOFREAD, "file": "*", "count": 9}] * 2},
            )
        self.assertIn(str(SUBAGENT_MAX_TASKS), str(ctx.exception))

    def test_multi_file_proofread_comments_carry_the_filename(self) -> None:
        """一组文件里写的意见要能认出在哪份里——只给 index，主 Agent 没法定位。"""
        parent = _Parent({"a.json": [ENTRY], "b.json": [ENTRY]})
        script = [
            (
                "",
                [
                    _Call(
                        "c1",
                        "patch_transl_cache",
                        json.dumps(
                            {"filename": "b.json", "patches": [{"index": 1, "proofread_comment": "漏译"}]}
                        ),
                    )
                ],
            ),
            ("报告：1 条。", []),
        ]
        out = _run(parent, {"tasks": [{"agent": SUBAGENT_AGENT_PROOFREAD, "file": "*"}]}, script)
        self.assertEqual(out["tasks"][0]["files"], ["a.json", "b.json"])
        self.assertEqual(out["tasks"][0]["proofread_comment"], [{"file": "b.json", "index": 1}])

    def test_multi_file_lock_scope_message(self) -> None:
        """一组的锁定：范围内的放行，范围外的报错把整组范围列出来。"""
        parent = _Parent({"a.json": [ENTRY], "b.json": [ENTRY], "c.json": [ENTRY]})
        handlers = _subagent_handlers(SUBAGENT_AGENT_PROOFREAD, ["a.json", "b.json"])

        handlers["read_transl_cache"](parent, {"filename": "b.json"})  # 自己那组没问题
        with self.assertRaises(AgentToolError) as ctx:
            handlers["read_transl_cache"](parent, {"filename": "c.json"})
        self.assertIn("2 个文件", str(ctx.exception))
        self.assertIn("「a.json」", str(ctx.exception))


class FileSelectorTests(unittest.TestCase):
    """file 的选择器：选谁不再只有"单个文件名 / *"两档（选择器 × count × indexes 正交）。

    list / glob / regex / select:has_problem / select:problem_type / random，选中的集合再交给
    count 均分或切片；已被具体选择器选中的文件不会再分给 "*"（点名优先）。
    """

    def _parent(self) -> _Parent:
        return _Parent(
            {name: [ENTRY] for name in ("SW_01.json", "SW_02.json", "AB_01.json")},
            problems=[
                {"filename": "SW_01.json", "problem": "残留日文：x"},
                {"filename": "AB_01.json", "problem": "漏译：y"},
            ],
        )

    def _files(self, parent: _Parent, spec: str) -> list[str]:
        out = _run(
            parent, {"tasks": [{"agent": SUBAGENT_AGENT_PROOFREAD, "file": spec}]}, [("报告。", [])]
        )
        return out["tasks"][0]["files"]

    def test_list_selector_keeps_the_named_order(self) -> None:
        parent = self._parent()
        self.assertEqual(
            self._files(parent, "list:AB_01.json,SW_01.json"), ["AB_01.json", "SW_01.json"]
        )

    def test_glob_and_regex_selectors(self) -> None:
        parent = self._parent()
        self.assertEqual(self._files(parent, "glob:SW_*"), ["SW_01.json", "SW_02.json"])
        self.assertEqual(self._files(parent, "regex:^SW_0[12]"), ["SW_01.json", "SW_02.json"])

    def test_select_has_problem_and_problem_type(self) -> None:
        parent = self._parent()
        self.assertEqual(self._files(parent, "select:has_problem"), ["AB_01.json", "SW_01.json"])
        self.assertEqual(self._files(parent, "select:problem_type=残留日文"), ["SW_01.json"])

    def test_random_selector_picks_a_subset(self) -> None:
        parent = self._parent()
        picked = self._files(parent, "random:2")
        self.assertEqual(len(picked), 2)
        self.assertTrue(set(picked) <= {"SW_01.json", "SW_02.json", "AB_01.json"})

    def test_selector_subset_is_split_by_count(self) -> None:
        """选择的子集再按 count 均分：有问题的那批文件分给 2 个代理。"""
        parent = self._parent()
        out = _run(
            parent,
            {
                "tasks": [
                    {"agent": SUBAGENT_AGENT_PROOFREAD, "file": "select:has_problem", "count": 2}
                ]
            },
            [("报告。", [])],
        )
        self.assertEqual([t["files"] for t in out["tasks"]], [["AB_01.json"], ["SW_01.json"]])
        self.assertIn("均分给 2 个", out["note"])

    def test_star_skips_files_claimed_by_a_selector(self) -> None:
        """点名优先：已被选择器选中的文件不再分给 "*"。"""
        parent = self._parent()
        out = _run(
            parent,
            {
                "tasks": [
                    {"agent": SUBAGENT_AGENT_PROOFREAD, "file": "regex:^SW_"},
                    {"agent": SUBAGENT_AGENT_PROOFREAD, "file": "*"},
                ]
            },
            [("报告。", [])],
        )
        self.assertEqual(out["tasks"][0]["files"], ["SW_01.json", "SW_02.json"])
        self.assertEqual(out["tasks"][1]["files"], ["AB_01.json"])

    def test_empty_selection_is_an_error(self) -> None:
        parent = self._parent()
        with self.assertRaises(AgentToolError) as ctx:
            _tool_run_subagents(
                parent, {"tasks": [{"agent": SUBAGENT_AGENT_PROOFREAD, "file": "glob:NOPE_*"}]}
            )
        self.assertIn("都没选中", str(ctx.exception))

    def test_bad_selector_is_reported(self) -> None:
        parent = self._parent()
        for spec in ("select:whatever", "random:0"):
            with self.assertRaises(AgentToolError):
                _tool_run_subagents(
                    parent, {"tasks": [{"agent": SUBAGENT_AGENT_PROOFREAD, "file": spec}]}
                )

    def test_multi_file_with_indexes_is_ambiguous(self) -> None:
        """多文件 + indexes：分不清区间属于哪份，直接报错而不是猜。"""
        parent = self._parent()
        with self.assertRaises(AgentToolError):
            _tool_run_subagents(
                parent,
                {
                    "tasks": [
                        {
                            "agent": SUBAGENT_AGENT_PROOFREAD,
                            "file": "list:SW_01.json,SW_02.json",
                            "indexes": "1-50",
                        }
                    ]
                },
            )


class RunSubagentsMarkdownTests(unittest.TestCase):
    """run_subagents 的返回渲染成一篇 Markdown（不再是一大坨 JSON）。

    每个子代理一个小节：统计行 + 「file × index」批注表 + 报告正文；失败/中止也照样看得见。
    """

    def test_end_to_end_renders_article_and_comment_table(self) -> None:
        parent = _Parent({"a.json": [ENTRY]})
        script = [
            (
                "",
                [
                    _Call(
                        "c1",
                        "patch_transl_cache",
                        json.dumps(
                            {
                                "filename": "a.json",
                                "patches": [{"index": 1, "proofread_comment": "漏译"}],
                            }
                        ),
                    )
                ],
            ),
            ("## 报告\n\n读了 1 条，写了 1 条意见。", []),
        ]

        out = _run(
            parent, {"tasks": [{"agent": SUBAGENT_AGENT_PROOFREAD, "file": "a.json"}]}, script
        )
        text = _render_tool_result_table("run_subagents", out)

        self.assertIsNotNone(text)
        self.assertIn("共派出 1 个子代理（完成 1），合计 1 条校对批注", text)
        self.assertIn("## 1. 校对 · a.json", text)
        self.assertIn("状态 完成", text)
        # 批注写成表格：file × index（主 Agent 据此去读 proofread_comment）
        self.assertIn("| file | index |", text)
        self.assertIn("| a.json | 1 |", text)
        # 报告正文原样贴
        self.assertIn("## 报告", text)

    def test_failed_and_skipped_tasks_are_reported(self) -> None:
        result = {
            "tasks": [
                {
                    "agent": "proofread",
                    "label": "校对",
                    "file": "a.json",
                    "files": ["a.json"],
                    "indexes": "",
                    "status": "failed",
                    "report": "",
                    "turns": 0,
                    "tool_calls": 0,
                    "proofread_comment": [],
                    "duration_ms": 0,
                    "error": "RuntimeError: boom",
                }
            ],
            "total": 1,
            "skipped": 2,
            "total_proofread_comment": 0,
            "note": "这批子代理没有提出任何疑问（没有条目被写入 proofread_comment）。",
        }

        text = _render_tool_result_table("run_subagents", result)

        self.assertIsNotNone(text)
        self.assertIn("共派出 1 个子代理（失败 1）", text)
        self.assertIn("另有 2 个任务因文件不够分被跳过", text)
        self.assertIn("## 1. 校对 · a.json", text)
        self.assertIn("状态 失败", text)
        self.assertIn("错误：RuntimeError: boom", text)


class SubagentPermissionTests(unittest.TestCase):
    """子代理不受权限模式约束：白名单里的工具一律直接执行（连 ask 档也不问）。

    这是刻意的（见 runtime._subagent_handlers 的说明）：一批 16 个子代理逐条弹审批卡会把
    界面淹掉，而它们能写的只有 proofread_comment（改不了译文）。主 Agent 那道门禁仍然管着
    「派子代理」这件事本身——ask 档下用户批的是这次委派，卡上能看到派给谁、看哪个文件，
    不是子代理的每一次读写。
    """

    def _subagent(self, parent: _Parent) -> rt.SubAgentRunner:
        return rt.SubAgentRunner(
            parent,
            agent=SUBAGENT_AGENT_PROOFREAD,
            files=["a.json"],
            indexes="",
            brief="",
            delegation_id="d1",
        )

    def test_patch_is_not_gated_even_in_ask_mode(self) -> None:
        parent = _Parent({"a.json": [ENTRY]})
        call = _Call(
            "c1",
            "patch_transl_cache",
            json.dumps({"filename": "a.json", "patches": [{"index": 1, "proofread_comment": "漏译"}]}),
        )

        sub = self._subagent(parent)
        out = sub._run_tool(call, _subagent_handlers(SUBAGENT_AGENT_PROOFREAD))

        # 写进去了（结果直接回给子代理，没有"等批准"这回事）；给模型看的是 Markdown
        self.assertIn("共改动 1 条", out["content"])
        self.assertIn("#1.proofread_comment", out["content"])
        self.assertEqual(parent.files["a.json"][0]["proofread_comment"], "漏译")
        # 门禁一次都没被问过，也没发审批事件
        self.assertEqual(parent.permission_checks, [])
        self.assertNotIn("permission_request", parent.types())

    def test_a_whole_delegation_never_asks(self) -> None:
        parent = _Parent({"a.json": [ENTRY]})
        script = [
            (
                "",
                [
                    _Call(
                        "c1",
                        "patch_transl_cache",
                        json.dumps({"filename": "a.json", "patches": [{"index": 1, "proofread_comment": "漏译"}]}),
                    )
                ],
            ),
            ("报告：1 条漏译。", []),
        ]

        out = _run(parent, {"tasks": [{"agent": SUBAGENT_AGENT_PROOFREAD, "file": "a.json"}]}, script)

        self.assertEqual(out["tasks"][0]["status"], "done")
        self.assertEqual(parent.files["a.json"][0]["proofread_comment"], "漏译")
        self.assertEqual(parent.permission_checks, [])
        self.assertNotIn("permission_request", parent.types())

    def test_the_delegation_itself_is_gated_for_the_parent(self) -> None:
        """子代理内部不问，但「派子代理」这件事本身要问——连「允许编辑」档也要问。

        派活是「要不要开始干这件事」：一次最多 16 个并行跑、每个都调模型、都会写缓存，
        所以它按 high 归类（同改配置 / 启动翻译），不是"改译文数据"那种自动放行。
        """
        self.assertEqual(rt._tool_risk("run_subagents"), rt.PERMISSION_HIGH)
        self.assertTrue(rt._permission_needed(rt.PERMISSION_HIGH, "ask"))
        self.assertTrue(rt._permission_needed(rt.PERMISSION_HIGH, "accept-edits"))
        self.assertFalse(rt._permission_needed(rt.PERMISSION_HIGH, "auto"))
        self.assertFalse(rt._permission_needed(rt.PERMISSION_HIGH, "auto-quiet"))

    def test_the_whitelist_is_the_security_boundary(self) -> None:
        """白名单是唯一的边界：子代理不过门禁，所以塞进去的高风险工具等于全自动放行。

        钉住三件事：① 白名单里的名字都真实存在（写错一个名字 = 给了个空工具，模型会一直撞墙）；
        ② 除校对那支的 patch_transl_cache（改译文数据级）外，所有角色的工具都是读类；
        ③ 原文探索那支连 patch 都没有。
        """
        non_read: set[str] = set()
        for agent, role in SUBAGENT_ROLES.items():
            for name in role.tools:
                self.assertIn(name, rt._TOOL_HANDLERS, f"{agent} 的白名单里有不存在的工具：{name}")
                if rt._tool_risk(name) != rt.PERMISSION_READ:
                    non_read.add(name)
        self.assertEqual(non_read, {"patch_transl_cache"})
        self.assertIn("patch_transl_cache", SUBAGENT_ROLES[SUBAGENT_AGENT_PROOFREAD].tools)
        self.assertNotIn("patch_transl_cache", SUBAGENT_ROLES[SUBAGENT_AGENT_EXPLORE].tools)


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


class SubagentCompactionTests(unittest.TestCase):
    """子代理走与父 Agent 同一套压缩：**Insert-then-Compress**。

    窗口取自 parent._context_window、阈值走 COMPACT_TRIGGER_RATIO、切点走
    _find_compaction_cut（保证 tool_calls/tool 成对）；头部 2 条（system + 任务说明）
    永远保留，保留尾部取 SUBAGENT_COMPACT_KEEP_RECENT。压缩那一轮**不带 tools**。
    插入式失败或没拿到摘要时，退回独立摘要请求（复用 parent._summarize_messages），
    再兜本地摘要——整条路都不该把子代理卡死。
    """

    def _parent(self, window: int, summarizer=None) -> _Parent:
        parent = _Parent({"a.json": [ENTRY]})
        parent._context_window = window
        if summarizer is not None:
            parent._summarize_messages = summarizer
        return parent

    def _sub(self, parent: _Parent) -> rt.SubAgentRunner:
        return rt.SubAgentRunner(
            parent,
            agent=SUBAGENT_AGENT_PROOFREAD,
            files=["a.json"],
            indexes="",
            brief="",
            delegation_id="d1",
        )

    @staticmethod
    def _history(rounds: int, chars: int = 4000) -> list[dict]:
        """构造 rounds 轮合法的 assistant(tool_calls) + tool 往返，每轮工具结果很长。"""
        messages: list[dict] = [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "BRIEF"},
        ]
        for i in range(rounds):
            messages.append({
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": f"c{i}",
                    "type": "function",
                    "function": {"name": "read_transl_cache", "arguments": "{}"},
                }],
            })
            messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": "x" * chars})
        return messages

    def test_reuses_parent_window_with_default_fallback(self) -> None:
        self.assertEqual(self._sub(self._parent(50_000))._parent_context_window(), 50_000)
        # 父 Agent 还没解析出窗口（或替身没有这个属性）时不炸：退回默认窗口
        self.assertEqual(
            self._sub(_Parent({"a.json": [ENTRY]}))._parent_context_window(),
            rt.DEFAULT_CONTEXT_WINDOW,
        )

    def test_begin_appends_the_instruction_and_abort_rolls_back(self) -> None:
        sub = self._sub(self._parent(20_000))  # 阈值 7808
        sub.messages = self._history(12)
        before = [dict(m) for m in sub.messages]

        self.assertGreater(sub._estimate_context_tokens(), 7808)  # 前提：确实超了
        self.assertTrue(sub._begin_compaction())

        # 指令是挂在末尾的一条瞬时消息（下一轮请求带着它一起发出去）
        self.assertEqual(sub.messages[-1]["content"], rt.SUBAGENT_COMPACT_INSTRUCTION_PROMPT)
        self.assertIsNotNone(sub._pending_compaction)

        sub._abort_compaction()  # 压缩没成：历史回到原样，指令不留下
        self.assertEqual(sub.messages, before)
        self.assertIsNone(sub._pending_compaction)

    def test_below_threshold_history_is_untouched(self) -> None:
        sub = self._sub(self._parent(1_000_000))
        sub.messages = self._history(3)
        before = [dict(m) for m in sub.messages]

        self.assertFalse(sub._begin_compaction())
        self.assertEqual(sub.messages, before)

    def test_finish_rebuilds_head_summary_and_tail(self) -> None:
        sub = self._sub(self._parent(20_000))
        sub.messages = self._history(12)
        sub._begin_compaction()

        self.assertTrue(
            sub._finish_compaction("前言\n<summary>## 目标\n已校对 8 轮。</summary>\n后记")
        )

        # 头部 2 条原样保留，随后一条摘要；尾部按 token 预算挑（来自原文尾部、配对完整）
        self.assertEqual(sub.messages[0], {"role": "system", "content": "SYS"})
        self.assertEqual(sub.messages[1], {"role": "user", "content": "BRIEF"})
        self.assertIn("压缩摘要", sub.messages[2]["content"])
        self.assertIn("已校对 8 轮", sub.messages[2]["content"])
        self.assertNotIn("前言", sub.messages[2]["content"])  # <summary> 之外的不进历史
        tail = sub.messages[3:]
        original = self._history(12)
        self.assertTrue(tail)
        self.assertEqual(tail, original[-len(tail):])  # 保留段就是原文的末尾一段
        budget = rt._keep_recent_tokens(7808, rt.SUBAGENT_COMPACT_KEEP_RECENT_RATIO)
        self.assertLessEqual(sum(rt._estimate_message_tokens(m) for m in tail), budget)
        self.assertEqual(sub.messages[-1]["content"], "x" * 4000)
        self.assertNotIn(
            rt.SUBAGENT_COMPACT_INSTRUCTION_PROMPT, [m.get("content") for m in sub.messages]
        )
        self.assertIsNone(sub._pending_compaction)

    def test_finish_without_a_summary_falls_back_to_a_separate_request(self) -> None:
        seen: list[list[dict]] = []

        def summarize(head: list[dict]) -> str:
            seen.append(head)
            return "## 目标\n独立摘要。"

        parent = self._parent(20_000, summarizer=summarize)
        sub = self._sub(parent)
        sub.messages = self._history(12)
        self.assertTrue(sub._begin_compaction())
        head_len = int(sub._pending_compaction["cut"]) - 2  # 头部 2 条不进摘要

        with patch.object(rt, "_subagent_chat", _make_chat([("", [])])):  # 回了个空正文
            sub._run_compaction_request(None, "fake-model")

        # 摘要只吃被压缩掉的那段（切点之前的部分），不是整份历史
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(seen[0]), head_len)
        self.assertGreater(head_len, 0)
        self.assertIn("独立摘要", sub.messages[2]["content"])
        self.assertIsNone(sub._pending_compaction)

    def test_compaction_request_failure_falls_back_to_a_separate_request(self) -> None:
        def boom(_client, _model, _messages, _tools):
            raise RuntimeError("compaction down")

        parent = self._parent(20_000, summarizer=lambda head: "## 目标\n独立摘要。")
        sub = self._sub(parent)
        sub.messages = self._history(12)
        sub._begin_compaction()

        with patch.object(rt, "_subagent_chat", boom):
            sub._run_compaction_request(None, "fake-model")

        self.assertIn("独立摘要", sub.messages[2]["content"])

    def test_falls_back_to_local_summary_without_parent_summarizer(self) -> None:
        sub = self._sub(self._parent(20_000))  # 不给 _summarize_messages
        sub.messages = self._history(12)

        sub._compact_via_separate_request()

        self.assertIn("因上下文超限被压缩", sub.messages[2]["content"])

    def test_falls_back_to_local_summary_when_summarizer_raises(self) -> None:
        def boom(_head):
            raise RuntimeError("summarizer down")

        sub = self._sub(self._parent(20_000, summarizer=boom))
        sub.messages = self._history(12)

        sub._compact_via_separate_request()

        self.assertIn("因上下文超限被压缩", sub.messages[2]["content"])

    def test_run_compresses_with_the_current_conversation_and_without_tools(self) -> None:
        """整条路：挂指令 → 那一轮**不带 tools**把摘要拿回来 → 历史被压 → 下一轮继续干活。"""
        parent = self._parent(rt.DEFAULT_CONTEXT_WINDOW)
        calls: list[tuple[list[dict], object]] = []
        script = [
            ("", [_Call("c1", "read_transl_cache", '{"filename": "a.json", "index": "1"}')]),
            ("", [_Call("c2", "read_transl_cache", '{"filename": "a.json", "index": "1"}')]),
            ("<summary>## 已完成\n读过 #1。</summary>", []),  # 压缩那一轮
            ("报告：没问题。", []),
        ]
        base = _make_chat(script)

        def recording(client, model, messages, tools):
            calls.append(([dict(m) for m in messages], tools))
            return base(client, model, messages, tools)

        def fake_estimate(sub: rt.SubAgentRunner) -> int:
            # 第 3 轮开始算"超阈值"（真阈值判定见 _begin_compaction 的用例）
            return 99_999 if len(sub.messages) >= 6 else 10

        with (
            patch.object(rt, "_subagent_chat", recording),
            patch.object(rt.SubAgentRunner, "_estimate_context_tokens", fake_estimate),
            # 保留段预算压到 1 token：6 条历史也能切出安全切点（只留最后那次工具往返）
            patch.object(rt, "_keep_recent_tokens", lambda _limit, _ratio=None: 1),
        ):
            out = _tool_run_subagents(
                parent, {"tasks": [{"agent": SUBAGENT_AGENT_PROOFREAD, "file": "a.json"}]}
            )

        self.assertEqual(out["tasks"][0]["status"], "done")
        self.assertEqual(out["tasks"][0]["report"], "报告：没问题。")
        # 4 次请求：两轮干活 → 一轮压缩（不带 tools）→ 一轮收尾
        self.assertEqual([tools is None for _, tools in calls], [False, False, True, False])
        # 压缩那一轮发出去的是"当前会话 + 那条指令"（复用前缀，不是另开一份输入）
        self.assertEqual(calls[2][0][-1]["content"], rt.SUBAGENT_COMPACT_INSTRUCTION_PROMPT)
        # 收尾那轮的上下文里已经是摘要 + 尾部，指令不留在历史里
        last = calls[3][0]
        self.assertTrue(any("# 早前工作的压缩摘要" in str(m.get("content") or "") for m in last))
        self.assertNotIn(rt.SUBAGENT_COMPACT_INSTRUCTION_PROMPT, [m.get("content") for m in last])
        # 界面上看得见这次压缩（子代理的每一步都作为一个事件推出去）
        notes = [data["text"] for kind, data in parent.events if kind == "subagent_message"]
        self.assertTrue(any("上下文压缩" in text for text in notes), notes)


class ProofreadSuggestionModeTests(unittest.TestCase):
    """校对子代理写哪一类意见（校对 / 润色 / 两者）**由主 Agent 问过用户后写进任务说明**。

    提示词这层钉三件事：子代理知道自己能写两类、写哪类以任务说明为准、任务说明没提时
    默认只写校对建议（保守，不会拿风格噪音淹掉硬伤）；任务说明模板里必须给这句话留位置；
    主 Agent 的流程里必须写明"先 ask_user 问用户，再把答案写进 brief"。
    """

    def test_subagent_prompt_covers_both_kinds_and_defers_to_the_brief(self) -> None:
        prompt = rt.SUBAGENT_PROOFREAD_PROMPT

        self.assertIn("校对建议", prompt)
        self.assertIn("润色建议", prompt)
        self.assertIn("以任务说明为准", prompt)
        self.assertIn("硬伤优先", prompt)  # 两类都要时先保证硬伤被抓出来
        self.assertIn("给出具体改法", prompt)  # 润色建议不许只说"不够好"
        self.assertNotIn("不要报风格偏好", prompt)  # 旧的"一律不许提风格"已被任务说明取代

    def test_brief_template_reminds_where_the_kind_goes(self) -> None:
        brief = rt._SUBAGENT_BRIEF_TEMPLATE

        self.assertIn("校对建议 / 润色建议 / 两者都要", brief)
        self.assertIn("默认只写校对建议", brief)

    def test_subagent_patch_tool_points_back_at_the_brief(self) -> None:
        tools = rt._subagent_tools(SUBAGENT_AGENT_PROOFREAD)
        schema = next(t for t in tools if t["function"]["name"] == "patch_transl_cache")

        self.assertIn("以任务说明为准", schema["function"]["description"])

    def test_main_agent_asks_the_user_before_delegating(self) -> None:
        prompt = rt.AGENT_SYSTEM_PROMPT

        self.assertIn("ask_user 问清意见类型", prompt)
        self.assertIn("只写润色建议", prompt)
        self.assertIn("再把答案写进 brief", prompt)


if __name__ == "__main__":
    unittest.main()
