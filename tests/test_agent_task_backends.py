"""专属默认后端：任务分流、压缩与署名、连接释放，以及会话上下文恢复。"""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import urllib.request
from http.server import ThreadingHTTPServer

from GalTransl.Agent import session_store, subagent
from GalTransl.Agent.context import _resume_context, _sanitize_tool_args
from GalTransl.Agent.core import AgentStopRequested
from GalTransl.Agent.models import AgentState
from GalTransl.Agent.registry import AgentRuntime
from GalTransl.Agent.runner import AgentRunner
from GalTransl.Agent.tools.proofread import ProofreadFixer
from GalTransl.Agent.tools.jobs import _tool_start_translation
from tests.test_agent_subagent import _Parent, ENTRY
from GalTransl.server import JobRegistry, build_handler


def profile(name, window=64000, caching="off"):
    return {"OpenAI-Compatible": {"promptCaching": caching, "tokens": [{
        "token": f"secret-{name}", "modelName": name,
        "endpoint": f"https://{name}.example/v1", "contextWindow": window,
    }]}}


class TaskBackendTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.project = str(Path(directory.name) / "project")
        Path(self.project).mkdir()
        root = patch.object(session_store, "SESSIONS_ROOT", str(Path(directory.name) / "sessions"))
        root.start()
        self.addCleanup(root.stop)
        self.state = AgentState(
            project_dir=self.project, config_file_name="config.yaml",
            backend_profile_name="main", backend_profile_data=profile("main"),
            translator_profile_name="translator", translator_profile_data=profile("translator"),
            gendic_profile_name="dictionary", gendic_profile_data=profile("dictionary"),
            subagent_profile_name="child", subagent_profile_data=profile("child", 32000, "on"),
        )
        self.runner = AgentRunner(self.state)
        self.runner._model = "main"
        self.runner._openai_client = Mock()
        self.main_client = self.runner._openai_client
        self.clients = []

        def client(**kwargs):
            result = Mock()
            result.options = kwargs
            result.chat.completions.create.return_value = SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="summary"))])
            self.clients.append(result)
            return result

        factory = patch("openai.OpenAI", side_effect=client)
        factory.start()
        self.addCleanup(factory.stop)
        self.parent = _Parent(files={"a.json": [ENTRY]})
        self.parent.state = self.state
        for name in ("_http_get", "_http_post", "_project_id", "_emit"):
            setattr(self.runner, name, getattr(self.parent, name))

    def test_gendic_and_translation_use_distinct_backends(self):
        self.runner._http_post = Mock(return_value={"job_id": "job-1"})
        for engine, expected in (("gendic", "dictionary"), ("auto-translate", "translator")):
            out = _tool_start_translation(self.runner, {"translator": engine})
            body = self.runner._http_post.call_args.args[1]
            self.assertEqual(body["backend_profile_data"], profile(expected))
            self.assertEqual(out["backend"]["name"], expected)
            self.assertNotIn("secret-", json.dumps(out))
        self.state.gendic_profile_name, self.state.gendic_profile_data = "", {}
        out = _tool_start_translation(self.runner, {"translator": "GenDic"})
        self.assertEqual(out["backend"]["name"], "translator")

    def test_both_roles_use_child_model_and_cache_settings(self):
        def chat(client, model, messages, tools):
            self.assertEqual(model, "child")
            self.assertEqual(client.options["api_key"], "secret-child")
            self.assertEqual(client.options["base_url"], "https://child.example/v1")
            self.assertIn("cache_control", tools[-1])
            return "done", [], "", "", 100

        with patch.object(subagent, "_subagent_chat", side_effect=chat), \
                patch.object(subagent, "SUBAGENT_CACHE_WARMUP_SECONDS", 0):
            out = subagent._tool_run_subagents(self.runner, {"tasks": [
                {"agent": "proofread", "file": "a.json"}, {"agent": "explore", "brief": "check"},
            ]})
        self.assertEqual([row["status"] for row in out["tasks"]], ["done", "done"])
        self.assertEqual(len(self.clients), 2)
        for client in self.clients:
            client.close.assert_called_once()
        self.assertEqual(self.runner._subagent_clients, [])
        self.assertIs(self.runner._openai_client, self.main_client)
        self.main_client.close.assert_not_called()
        self.assertEqual(self.runner._model, "main")
        starts = [data for kind, data in self.parent.events if kind == "subagent_start"]
        self.assertEqual([row["model"] for row in starts], ["child", "child"])
        self.assertNotIn("secret-", json.dumps(self.parent.events))

    def test_compaction_and_proofread_stamp_use_child_backend(self):
        with self.runner._subagent_backend(self.state.subagent_profile_data, self.runner.stop_event) as view:
            child = subagent.SubAgentRunner(view, agent="proofread", files=["a.json"], indexes="", brief="", delegation_id="test")
            self.assertEqual(child._parent_context_window(), 32000)
            child.messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "task"}, {"role": "assistant", "content": "old work"}]
            child._compact_via_separate_request(3)
            self.assertEqual(self.clients[0].chat.completions.create.call_args.kwargs["model"], "child")
            self.main_client.chat.completions.create.assert_not_called()
            fixer = ProofreadFixer(view, "test", self.runner.stop_event)
            fixer.read(view, {"filename": "a.json", "start": 1, "end": 1})
            fixer.patch(view, {"filename": "a.json", "patches": [{"index": 1, "dst": "fixed"}]})
            self.assertEqual(self.parent.files["a.json"][0]["trans_by"], "child")

    def test_stop_closes_active_child_and_does_not_close_next_turn_client_on_cleanup(self):
        original_stop = self.runner.stop_event
        with self.runner._subagent_backend(self.state.subagent_profile_data, original_stop) as view:
            original_stop.set()
            self.runner.abort_in_flight()
            view._openai_client.close.assert_called_once()
            self.main_client.close.assert_called_once()
            self.runner.stop_event = threading.Event()
            next_client = Mock()
            self.runner._openai_client = next_client
            self.assertIs(view.stop_event, original_stop)
        next_client.close.assert_not_called()
        self.assertEqual(self.runner._subagent_clients, [])
        with self.assertRaises(AgentStopRequested):
            with self.runner._subagent_backend(self.state.subagent_profile_data, original_stop):
                self.fail("stopped worker must not start")
        self.clients[-1].close.assert_called_once()

    def test_client_is_closed_when_worker_fails(self):
        with patch.object(subagent, "SubAgentRunner", side_effect=RuntimeError("failed")):
            out = subagent._tool_run_subagents(self.runner, {"tasks": [{"agent": "explore"}]})
        self.assertEqual(out["tasks"][0]["status"], "failed")
        self.clients[0].close.assert_called_once()
        self.assertEqual(self.runner._subagent_clients, [])

    def test_context_survives_messages_and_restored_answers_without_persisting_secrets(self):
        runtime = AgentRuntime()
        context = {f"{role}_profile_{field}": getattr(self.state, f"{role}_profile_{field}")
                   for role in ("gendic", "subagent") for field in ("name", "data")}
        with patch.object(AgentRunner, "run", lambda runner: None):
            sid = runtime.start(self.project, "config.yaml", profile("main"), first_prompt="start", **context)["session_id"]
            state = runtime._get_state(self.project, sid)
            state.messages.append({"role": "user", "content": "start"})
            state.status = "awaiting_input"
            runtime.message(self.project, "continue", sid)
            self.assertEqual(state.subagent_profile_data, context["subagent_profile_data"])
            self.assertEqual(state.gendic_profile_data, context["gendic_profile_data"])
            state.status = "awaiting_input"
            runtime.message(self.project, "clear", sid, **_resume_context(gendic_profile_data={}, subagent_profile_data={}))
            self.assertEqual(state.gendic_profile_data, {})
            self.assertEqual(state.subagent_profile_data, {})
            self.assertEqual(state.subagent_profile_name, "")
            restarted = AgentRuntime()
            restarted.answer_ask(self.project, sid, [["continue"]], backend_profile_data=profile("main"), **context)
            restored = restarted._get_state(self.project, sid)
            self.assertEqual(restored.gendic_profile_data, context["gendic_profile_data"])
            self.assertEqual(restored.subagent_profile_data, context["subagent_profile_data"])
            restarted.answer_permission(self.project, sid, "allow-once", gendic_profile_data={}, subagent_profile_data={})
            self.assertEqual(restored.gendic_profile_data, {})
            self.assertEqual(restored.subagent_profile_data, {})
        text = Path(session_store.SessionStore(self.project, sid).path).read_text(encoding="utf-8")
        self.assertNotIn("secret-", text)
        self.assertNotIn("secret-", json.dumps(_sanitize_tool_args(context)))

    def test_all_http_entrypoints_forward_task_context_including_explicit_clear(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(JobRegistry()))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        for route, method in (("start", "start"), ("message", "message"), ("answer", "answer_ask"), ("permission", "answer_permission")):
            for data in (profile("child"), {}, None):
                with self.subTest(route=route, data=bool(data)), patch(f"GalTransl.server.AGENT_REGISTRY.{method}", return_value={}) as handler:
                    context = {} if data is None else {
                        "gendic_profile_name": "dictionary" if data else "", "gendic_profile_data": data,
                        "subagent_profile_name": "child" if data else "", "subagent_profile_data": data,
                    }
                    payload = {"project_dir": self.project, "backend_profile_data": profile("main"),
                               "first_prompt": "start", "message": "continue", "answers": [["yes"]],
                               "decision": "allow-once", **context}
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{server.server_address[1]}/api/agent/{route}",
                        data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
                    with urllib.request.urlopen(request, timeout=5) as response:
                        self.assertEqual(response.status, 200)
                    for role in ("gendic", "subagent"):
                        self.assertEqual(handler.call_args.kwargs[f"{role}_profile_data"], data)


if __name__ == "__main__":
    unittest.main()
