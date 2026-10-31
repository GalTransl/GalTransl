"""Exercise the public object/action tools through the real dispatch and permission gates."""

import json
from types import SimpleNamespace

import pytest

from GalTransl.Agent.billion_context import BillionContextSession
from GalTransl.Agent.context import _microcompact_tool_results, _task_continuation
from GalTransl.Agent.handlers import _RETIRED_TOOLS
from GalTransl.Agent.models import AgentState, AgentToolError
from GalTransl.Agent.runner import AgentRunner
from GalTransl.Agent.subagent import SubAgentRunner, _subagent_handlers, _subagent_tools
from GalTransl.Agent.tool_operations import tool_operation
from GalTransl.Agent.tool_schemas import AGENT_TOOLS
from GalTransl.Agent.tools.render_md import _render_tool_result_table
from tests import test_agent_replace_cache, test_agent_save_dict, test_agent_plugin_settings


class MemoryAgent(AgentRunner):
    def __init__(self, transport, mode="auto"):
        super().__init__(AgentState(permission_mode=mode, config_file_name=transport.state.config_file_name))
        self.transport = transport
        self.approvals = []
        self.decision = "deny"

    def _project_id(self):
        return self.transport._project_id()

    def _http_get(self, path):
        return self.transport._http_get(path)

    def _http_post(self, path, body):
        return self.transport._http_post(path, body)

    def _http_put(self, path, body):
        return self.transport._http_put(path, body)

    def request_permission(self, request_id, tool_call_id, name, args, mode):
        self.approvals.append((name, args))
        return self.decision, ""


def test_public_tools_and_optional_context_plugin(tmp_path):
    runner = AgentRunner(AgentState())
    names = {tool["function"]["name"] for tool in runner._request_tools()}
    assert names == {"get_project", "update_project_config", "input", "guideline", "dictionary", "names",
                     "translation", "read_transl_cache", "edit_transl_cache", "output", "problems", "subagents",
                     "ask_user", "read_history_archive"}
    assert not names & _RETIRED_TOOLS.keys()
    plugin = BillionContextSession(path=tmp_path / "context.sqlite3", context_window=128000)
    runner._billion_context = plugin
    tools = runner._request_tools()
    plugin.close()
    assert len(tools) == 17
    assert "read_history_archive" not in {tool["function"]["name"] for tool in tools}


def test_dictionary_reads_do_not_authorize_writes_and_results_keep_previews():
    transport = test_agent_save_dict._Runner(["Alice\told\tnote", "Bob\tkeep"])
    runner = MemoryAgent(transport, "ask")
    key = "(project_dir)项目GPT字典.txt"
    assert runner._dispatch_tool("dictionary", {"action": "read", "file_key": key})["returned"] == 2
    assert runner.approvals == []
    args = {"action": "set", "file_key": key, "content": "Alice\tnew", "reason": "统一译名"}
    with pytest.raises(AgentToolError, match="拒绝"):
        runner._dispatch_tool("dictionary", args)
    assert transport.saved == []
    runner.decision = "allow-session"
    result = runner._dispatch_tool("dictionary", args)
    assert transport.saved == ["Alice\tnew\tnote\nBob\tkeep"]
    assert result["reason"] == "统一译名"
    assert "new" in _render_tool_result_table(tool_operation("dictionary", args), result)
    with pytest.raises(AgentToolError, match="拒绝"):
        runner.decision = "deny"
        runner._dispatch_tool("translation", {"action": "start"})
    assert runner.approvals[-1][0] == "start_translation"


def test_cache_set_replace_delete_keep_scope_and_require_explicit_actions():
    transport = test_agent_replace_cache._Runner({
        "a.json": [{"index": 1, "pre_dst": "old", "post_src": "原文"}],
        "b.json": [{"index": 1, "pre_dst": "keep", "post_src": "原文"}],
    })
    runner = MemoryAgent(transport)
    result = runner._dispatch_tool("edit_transl_cache", {
        "action": "set", "filename": "a.json", "patches": [{"index": 1, "pre_dst": "new"}],
    })
    assert result["updated"] == 1
    assert transport.disk["b.json"][0]["pre_dst"] == "keep"
    runner._dispatch_tool("edit_transl_cache", {
        "action": "replace", "files": "a.json", "old_text": "new", "new_text": "done",
    })
    assert transport.disk["a.json"][0]["pre_dst"] == "done"
    for args in ({"filename": "a.json"}, {"action": "patch"},
                 {"action": "delete", "filename": "a.json", "patches": [{"index": 1}]}):
        with pytest.raises(AgentToolError):
            runner._dispatch_tool("edit_transl_cache", args)
    assert transport.saved == ["a.json", "a.json"]
    result = runner._dispatch_tool("edit_transl_cache", {"action": "delete", "filename": "a.json", "indexes": "1"})
    assert result["deleted_indexes"] == [1]
    assert transport.disk["a.json"] == []
    assert transport.disk["b.json"][0]["pre_dst"] == "keep"


def test_get_project_combines_config_and_plugins_in_one_call():
    transport = test_agent_plugin_settings.Runner([], {"common": {}, "plugin": {"filePlugin": "auto"}})
    runner = MemoryAgent(transport, "ask")
    result = runner._dispatch_tool("get_project", {"include": ["config", "plugins"]})
    assert {"config", "plugins"} <= result.keys()
    rendered = _render_tool_result_table("get_project", result)
    assert "config" in rendered and "plugins" in rendered
    assert runner.approvals == []


def test_subagents_cannot_escape_the_operation_whitelist():
    parent = MemoryAgent(test_agent_save_dict._Runner(["a\tA"]))
    sub = SubAgentRunner(parent, agent="explore", files=[], indexes="", brief="test", delegation_id="test")
    handlers = _subagent_handlers("explore")
    call = SimpleNamespace(id="read", function=SimpleNamespace(name="dictionary", arguments=json.dumps({
        "action": "read", "file_key": "(project_dir)项目GPT字典.txt",
    })))
    assert "a" in sub._run_tool(call, handlers)["content"]
    call.function.arguments = json.dumps({"action": "set", "file_key": "(project_dir)项目GPT字典.txt", "content": "a\tB"})
    assert "权限" in sub._run_tool(call, handlers)["content"]
    assert parent.transport.saved == []
    explore = {tool["function"]["name"]: tool["function"] for tool in _subagent_tools("explore")}
    assert set(explore) == {"input", "dictionary"}
    assert explore["dictionary"]["parameters"]["properties"]["action"]["enum"] == ["list", "read"]
    proofread = {tool["function"]["name"]: tool["function"] for tool in _subagent_tools("proofread")}
    assert proofread["edit_transl_cache"]["parameters"]["properties"]["action"]["enum"] == ["set"]
    assert "target" not in proofread["problems"]["parameters"]["properties"]


def test_problem_reads_are_free_but_rule_writes_still_need_approval():
    transport = test_agent_plugin_settings.Runner([], {"problemAnalyze": {"problemFilter": [], "problemWhiteList": []}})
    runner = MemoryAgent(transport, "ask")
    for target, field, value in (("filter", "keyword", "missing.*comma"), ("white_list", "entry", "a.json:1")):
        runner._dispatch_tool("problems", {"action": "list", "target": target})
        with pytest.raises(AgentToolError, match="拒绝"):
            runner._dispatch_tool("problems", {"action": "set", "target": target, field: value})
    assert len(runner.approvals) == 2
    assert transport.writes == []


def test_history_cleanup_distinguishes_read_and_write_actions():
    messages = []
    for index in range(8):
        for action in ("read", "set"):
            call_id = f"{index}-{action}"
            messages.extend([
                {"role": "assistant", "tool_calls": [{"id": call_id, "function": {"name": "dictionary", "arguments": json.dumps({"action": action})}}]},
                {"role": "tool", "tool_call_id": call_id, "content": "正文" * 2000},
            ])
    cleared = _microcompact_tool_results(messages)
    assert cleared
    assert all(messages[index]["tool_call_id"].endswith("read") for index in cleared)


def test_job_continuation_recognizes_the_new_tool_name():
    entries = _task_continuation([
        {"role": "assistant", "tool_calls": [{"id": "start", "function": {"name": "translation", "arguments": '{"action":"start"}'}}]},
        {"role": "tool", "tool_call_id": "start", "content": '{"job_id":"job-1","status":"pending"}'},
    ])
    assert any("job-1" in entry["text"] for entry in entries)
