"""启动翻译任务要用「翻译任务会用」的后端，不是 Agent 自己那份。

用户就是这么发现的：翻译器做可用性检测时报的是 **Agent 那个模型**
（sk-7Bj…/deepseek-…），而任务本该用「项目选择 → 否则全局『翻译器默认』」那份。
原因是 _tool_start_translation 直接发了 runner.state.backend_profile_data（Agent 自己那份）。

顺带锁住：含 token 的 backend_profile_data 只发给后端，不放进工具调用事件——
界面上的工具卡片与会话文件不该出现明文密钥。
"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from GalTransl.Agent.runtime import (
    AgentRunner,
    AgentState,
    _sanitize_tool_args,
    _tool_start_translation,
)

AGENT_PROFILE = {
    "OpenAI-Compatible": {
        "tokens": [
            {"token": "sk-agent-secret", "endpoint": "https://agent.example.com/v1", "modelName": "agent-model"}
        ]
    }
}
TRANSLATOR_PROFILE = {
    "OpenAI-Compatible": {
        "tokens": [
            {"token": "sk-trans-secret", "endpoint": "https://trans.example.com/v1", "modelName": "translator-model"}
        ]
    }
}


class _Runner:
    def __init__(self, **state_fields) -> None:
        self.state = AgentState(config_file_name="config.yaml", project_dir=r"C:\proj")
        for key, value in state_fields.items():
            setattr(self.state, key, value)
        self.posts: list[tuple[str, dict]] = []

    def _http_post(self, path: str, body: dict):
        self.posts.append((path, body))
        return {"job_id": "job-1", "status": "pending"}


class StartTranslationBackendTests(unittest.TestCase):
    def test_uses_translator_profile(self) -> None:
        runner = _Runner(
            backend_profile_data=AGENT_PROFILE,
            backend_profile_name="Agent 默认",
            translator_profile_data=TRANSLATOR_PROFILE,
            translator_profile_name="翻译器默认",
        )
        out = _tool_start_translation(runner, {"translator": "ForGal-json"})

        path, body = runner.posts[0]
        self.assertEqual(path, "/api/jobs")
        # 关键：发给任务的是翻译器那份（可用性检测才会测对模型）
        self.assertIs(body["backend_profile_data"], TRANSLATOR_PROFILE)
        self.assertEqual(
            body["backend_profile_data"]["OpenAI-Compatible"]["tokens"][0]["modelName"],
            "translator-model",
        )
        self.assertEqual(out["backend"]["model"], "translator-model")
        self.assertEqual(out["backend"]["name"], "翻译器默认")
        self.assertNotIn("note", out)

    def test_falls_back_to_agent_profile_with_note(self) -> None:
        """前端没送翻译器配置时回落到 Agent 那份，并且明说。"""
        runner = _Runner(backend_profile_data=AGENT_PROFILE, translator_profile_data={})
        out = _tool_start_translation(runner, {"translator": "ForGal-json"})

        body = runner.posts[0][1]
        self.assertIs(body["backend_profile_data"], AGENT_PROFILE)
        self.assertEqual(out["backend"]["model"], "agent-model")
        self.assertIn("翻译器", out["note"])
        self.assertIn("Agent 自己的后端", out["note"])

    def test_files_are_forwarded(self) -> None:
        runner = _Runner(backend_profile_data=AGENT_PROFILE, translator_profile_data=TRANSLATOR_PROFILE)
        out = _tool_start_translation(
            runner, {"translator": "ForGal-json", "files": ["a.json", " b.json ", ""]}
        )

        self.assertEqual(runner.posts[0][1]["input_files"], ["a.json", "b.json"])
        self.assertEqual(out["files"], ["a.json", "b.json"])


class ToolArgSanitizingTests(unittest.TestCase):
    def test_backend_profile_is_masked(self) -> None:
        safe = _sanitize_tool_args({"translator": "ForGal-json", "backend_profile_data": AGENT_PROFILE})
        self.assertEqual(safe["translator"], "ForGal-json")
        self.assertEqual(safe["backend_profile_data"], "<含 token 的后端配置，已省略>")
        self.assertNotIn("sk-agent-secret", json.dumps(safe, ensure_ascii=False))

    def test_other_args_untouched(self) -> None:
        args = {"query": "ドルード", "context": 3}
        self.assertIs(_sanitize_tool_args(args), args)

    def test_token_never_reaches_tool_call_events(self) -> None:
        """跑一个回合：工具调用事件里不能出现明文 token（界面卡片 + 会话文件都会存它）。"""
        state = AgentState()
        state.session_id = ""  # 不落盘
        state.messages = [{"role": "user", "content": "开始翻译"}]
        state.backend_profile_data = AGENT_PROFILE
        state.translator_profile_data = TRANSLATOR_PROFILE
        state.translator_profile_name = "翻译器默认"
        runner = AgentRunner(state)

        calls = {"n": 0}

        def fake_stream(_self):
            calls["n"] += 1
            runner._stream_acc = {"content": [], "reasoning": [], "tools": {}}
            if calls["n"] == 1:
                return (
                    "",
                    [{"id": "c1", "name": "start_translation", "arguments": '{"translator": "ForGal-json"}'}],
                    "tool_calls",
                )
            return ("好", [], "stop")

        posted: list[dict] = []

        with (
            patch.object(AgentRunner, "_resolve_llm", lambda self: None),
            patch.object(AgentRunner, "_stream_llm_response", fake_stream),
            patch.object(AgentRunner, "_http_post", lambda self, path, body: posted.append(body) or {"job_id": "j"}),
        ):
            runner.run()

        # 真正发出去的那份仍然带着真配置（不然任务起不来）
        self.assertIs(posted[0]["backend_profile_data"], TRANSLATOR_PROFILE)
        # 事件流里只有模型自己给的那些参数（后端配置是工具内部补的，压根不进事件），
        # 也不该出现任何明文 token
        tool_call_events = [e for e in state.events if e.type == "tool_call"]
        self.assertEqual(len(tool_call_events), 1)
        arguments = tool_call_events[0].data["arguments"]
        self.assertEqual(arguments, {"translator": "ForGal-json"})
        dumped = json.dumps([e.to_dict() for e in state.events], ensure_ascii=False)
        self.assertNotIn("sk-trans-secret", dumped)
        self.assertNotIn("sk-agent-secret", dumped)
        self.assertNotIn("token", dumped)

    def test_sanitizer_covers_a_model_supplied_profile(self) -> None:
        """万一模型自己把整份配置塞进参数（含 token），事件里也要是占位。"""
        safe = _sanitize_tool_args(
            {"translator": "ForGal-json", "backend_profile_data": {"OpenAI-Compatible": {"tokens": [{"token": "sk-x"}]}}}
        )
        self.assertEqual(safe["backend_profile_data"], "<含 token 的后端配置，已省略>")


if __name__ == "__main__":
    unittest.main()
