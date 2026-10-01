"""首条输入只属于用户消息，旧字段仅在读取边界兼容。"""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request
from http.server import ThreadingHTTPServer

from GalTransl.Agent import session_store
from GalTransl.Agent.models import AgentState
from GalTransl.Agent.prompts import _build_system_prompt
from GalTransl.Agent.registry import AgentRuntime
from GalTransl.Agent.runner import AgentRunner
from GalTransl.server import JobRegistry, build_handler


class FirstPromptTests(unittest.TestCase):
    def test_system_prompt_does_not_inject_user_request_or_turn_instructions(self):
        state = AgentState(project_dir="test-project", config_file_name="extract.yaml")
        prompt = _build_system_prompt(state)
        state.first_prompt = "仅检查第七章的人名"
        self.assertEqual(prompt, _build_system_prompt(state))
        self.assertNotIn("# 会话交互", prompt)
        self.assertNotIn("本次目标", prompt)
        self.assertNotIn("按标准流程完成本项目的翻译", prompt)
        self.assertIn("test-project", prompt)
        self.assertIn("extract.yaml", prompt)

    def test_first_user_message_is_preserved_verbatim_and_not_repeated(self):
        state = AgentState(first_prompt="  仅检查人名\n不要启动翻译  ")
        runner = AgentRunner(state)
        with patch.object(runner, "_resolve_llm"), patch.object(runner, "_stream_llm_response", return_value=("好的", [], "stop")):
            runner.run()
            self.assertEqual(state.messages[1], {"role": "user", "content": state.first_prompt})
            self.assertNotIn(state.first_prompt, state.messages[0]["content"])
            self.assertEqual([e.data["message"] for e in state.events if e.type == "user_message"], [state.first_prompt])
            state.messages.append({"role": "user", "content": "继续检查"})
            runner.run()
        self.assertEqual([m["content"] for m in state.messages if m["role"] == "user"], [state.first_prompt, "继续检查"])

    def test_empty_first_prompt_does_not_start_a_default_task(self):
        for text in ("", " \n "):
            with self.subTest(text=text):
                state = AgentState(first_prompt=text)
                runner = AgentRunner(state)
                with patch.object(runner, "_resolve_llm") as resolve, patch.object(runner, "_stream_llm_response") as stream:
                    runner.run()
                resolve.assert_not_called()
                stream.assert_not_called()
                self.assertEqual(state.status, "awaiting_input")
                self.assertEqual(state.messages, [])
                self.assertFalse(any(e.type == "user_message" for e in state.events))

    def test_new_session_metadata_and_status_use_first_prompt(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(session_store, "SESSIONS_ROOT", directory):
            project = str(Path(directory) / "project")
            with patch.object(AgentRunner, "run"):
                result = AgentRuntime().start(project, "config.yaml", {}, first_prompt="检查原文")
            meta = session_store.read_meta(project, result["session_id"])
            self.assertEqual(meta["first_prompt"], "检查原文")
            self.assertEqual(result["first_prompt"], "检查原文")
            self.assertNotIn("goal", meta)
            self.assertNotIn("goal", result)

    def test_legacy_metadata_restores_first_message_and_new_field_wins(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(session_store, "SESSIONS_ROOT", directory):
            project = str(Path(directory) / "project")
            sid = session_store.create_session(project, "old session")
            store = session_store.SessionStore(project, sid)
            store.append_meta(goal="旧会话首条输入")
            store.append_message({"role": "assistant", "content": "完成检查"})
            runtime = AgentRuntime()
            status = runtime.status(project, sid)
            self.assertEqual(status["first_prompt"], "旧会话首条输入")
            self.assertNotIn("goal", status)
            self.assertEqual(runtime.transcript(project, sid)[0]["message"], "旧会话首条输入")
            store.append_meta(first_prompt="新字段首条输入")
            restored = AgentRuntime().status(project, sid)
            self.assertEqual(restored["first_prompt"], "新字段首条输入")
            self.assertEqual(session_store.first_prompt_from_meta({"first_prompt": "", "goal": "旧输入"}), "")

    def test_http_start_accepts_new_field_and_legacy_alias(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(JobRegistry()))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            for fields, expected in (({"first_prompt": "新输入"}, "新输入"), ({"goal": "旧输入"}, "旧输入"),
                                     ({"first_prompt": "", "goal": "旧输入"}, "")):
                with self.subTest(fields=fields), patch("GalTransl.server.AGENT_REGISTRY.start", return_value={}) as start:
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{server.server_address[1]}/api/agent/start",
                        data=json.dumps({"project_dir": "project", "backend_profile_data": {"test": True}, **fields}).encode(),
                        headers={"Content-Type": "application/json"}, method="POST")
                    with urllib.request.urlopen(request, timeout=5) as response:
                        json.load(response)
                    self.assertEqual(start.call_args.kwargs["first_prompt"], expected)
                    self.assertNotIn("goal", start.call_args.kwargs)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
