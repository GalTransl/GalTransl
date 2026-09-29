"""Agent first turns and restored sessions must target their own backend."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from GalTransl.Agent import session_store
from GalTransl.Agent.registry import AgentRuntime
from GalTransl.Agent.runner import AgentRunner


class AgentBackendAddressTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.project = str(Path(directory.name) / "project")
        Path(self.project).mkdir()
        self.sessions = patch.object(session_store, "SESSIONS_ROOT", str(Path(directory.name) / "sessions"))
        self.sessions.start()
        self.addCleanup(self.sessions.stop)
        self.run = patch.object(AgentRunner, "run", lambda self: None)
        self.run.start()
        self.addCleanup(self.run.stop)

    def stored_session(self, runtime):
        sid = runtime.create_session(self.project)["session_id"]
        session_store.SessionStore(self.project, sid).append_message({"role": "user", "content": "previous turn"})
        return sid

    def test_new_turn_uses_runtime_address(self):
        runtime = AgentRuntime(host="127.0.0.1", port=42123)
        result = runtime.start(self.project, "config.yaml", {}, goal="test")
        runner = runtime._runners[runtime._key(self.project)][result["session_id"]]
        self.assertEqual(runner.base_url, "http://127.0.0.1:42123")

    def test_restored_message_uses_new_process_port(self):
        previous = AgentRuntime(port=42123)
        sid = self.stored_session(previous)
        restarted = AgentRuntime(port=43210)
        restarted.message(self.project, "continue", sid)
        runner = restarted._runners[restarted._key(self.project)][sid]
        self.assertEqual(runner.base_url, "http://127.0.0.1:43210")

    def test_restored_event_runner_keeps_address_when_resumed(self):
        runtime = AgentRuntime(port=43210)
        sid = self.stored_session(runtime)
        state = runtime._get_state(self.project, sid)
        runner = runtime._runner_for_state(self.project, state)
        self.assertEqual(runner.base_url, "http://127.0.0.1:43210")
        runtime.message(self.project, "continue", sid)
        self.assertIs(runtime._runners[runtime._key(self.project)][sid], runner)
        self.assertEqual(runner.base_url, "http://127.0.0.1:43210")


if __name__ == "__main__":
    unittest.main()
