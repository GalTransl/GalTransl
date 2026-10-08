"""重启、停止及切换模型后，沿用同一历史前缀最后一次实测统计。"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from GalTransl.Agent import session_store
from GalTransl.Agent.context import _estimate_message_tokens, _restore_compacted_history, _restore_usage_anchor
from GalTransl.Agent.llm_backend import LLMBackend
from GalTransl.Agent.models import AgentState
from GalTransl.Agent.registry import AgentRuntime
from GalTransl.Agent.runner import AgentRunner


class UsageRestoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.project = str(Path(directory.name) / "project")
        root = patch.object(session_store, "SESSIONS_ROOT", str(Path(directory.name) / "sessions"))
        root.start()
        self.addCleanup(root.stop)
        self.sid = session_store.create_session(self.project, "test")
        self.state = AgentState(project_dir=self.project, session_id=self.sid)
        self.runner = AgentRunner(self.state)
        self.runner._model = "test-model"
        self.store = self.runner._store

    def restore(self):
        runtime = AgentRuntime()
        state = runtime._get_state(self.project, self.sid)
        return state, runtime.status(self.project, self.sid)["context"]

    def test_stream_usage_survives_restart_and_only_new_messages_are_estimated(self):
        self.runner._persist_message({"role": "system", "content": "rules"})
        self.runner._persist_message({"role": "user", "content": "中" * 240000})
        self.assertGreater(self.runner._estimate_context_tokens(), 128000)
        self.runner._openai_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kwargs: iter([SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=60000))]))))
        self.runner._stream_llm_response()
        self.runner._persist_message({"role": "assistant", "content": "done", "reasoning_content": "thoughts"})
        self.runner._persist_message({"role": "user", "content": "continue"})
        before = self.runner._estimate_context_tokens()
        restored, context = self.restore()
        self.assertEqual(context["used_tokens"], before)
        self.assertEqual(restored.anchored_message_count, 2)
        self.assertEqual(restored.last_prompt_tokens, 60000)
        self.assertFalse(AgentRunner(restored)._begin_compaction())
        checkpoint = self.store.load()["meta"]["context_usage_anchor"]
        self.assertNotIn("content", checkpoint)

    def test_checkpoint_after_compaction_uses_restored_history_not_full_log(self):
        for message in [
            {"role": "system", "content": "rules"}, {"role": "user", "content": "old"},
            {"role": "assistant", "content": "old answer"}, {"role": "user", "content": "recent"},
            {"role": "user", "content": "summary", "_compact_summary": True, "_compact_kept": 1},
        ]:
            self.runner._persist_message(message)
        self.state.messages = _restore_compacted_history(self.state.messages)
        self.runner._record_context_usage(42000)
        self.runner._persist_message({"role": "assistant", "content": "new answer"})
        restored, context = self.restore()
        self.assertEqual(len(restored.messages), 4)
        self.assertEqual(context["used_tokens"], self.runner._estimate_context_tokens())

    def test_old_checkpoint_is_invalidated_by_compaction_or_prefix_edit(self):
        self.runner._persist_message({"role": "system", "content": "rules"})
        self.runner._persist_message({"role": "user", "content": "old"})
        self.runner._record_context_usage(90000)
        checkpoint = self.store.load()["meta"]["context_usage_anchor"]
        changed = [self.state.messages[0], {"role": "user", "content": "different"}]
        self.assertEqual(_restore_usage_anchor(changed, checkpoint), (0, 0, ""))
        self.runner._persist_message({"role": "user", "content": "summary", "_compact_summary": True, "_compact_kept": 0})
        restored, _ = self.restore()
        self.assertEqual(restored.last_prompt_tokens, 0)

    def test_missing_or_invalid_checkpoint_falls_back_without_inventing_usage(self):
        self.runner._persist_message({"role": "user", "content": "old session"})
        for value in (None, {}, {"prompt_tokens": "60000", "message_count": 1},
                      {"prompt_tokens": 60000, "message_count": 99}):
            self.store.append_meta(context_usage_anchor=value)
            restored, _ = self.restore()
            self.assertEqual(restored.last_prompt_tokens, 0)

    def test_changing_model_preserves_usage_until_a_new_measurement(self):
        self.runner._persist_message({"role": "user", "content": "hello"})
        self.runner._record_context_usage(60000)
        restored, _ = self.restore()
        backend = LLMBackend(Mock(), "different-model", 200000, False)
        runner = AgentRunner(restored)
        with patch("GalTransl.Agent.runner.resolve_llm_backend", return_value=backend):
            runner._resolve_llm()
        self.assertEqual(restored.last_prompt_tokens, 60000)
        self.assertEqual(restored.usage_model, "test-model")
        self.assertEqual(self.restore()[1]["used_tokens"], 60000)
        self.assertEqual(self.restore()[1]["window_tokens"], 200000)
        backend.client.chat.completions.create.return_value = iter([
            SimpleNamespace(choices=[], usage=None),
            SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=0)),
        ])
        runner._stream_llm_response()
        self.assertEqual(self.restore()[1]["used_tokens"], 60000)
        backend.client.chat.completions.create.return_value = iter([
            SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=61000)),
        ])
        runner._stream_llm_response()
        latest, context = self.restore()
        self.assertEqual(context["used_tokens"], 61000)
        self.assertEqual(latest.usage_model, "different-model")

    def test_restart_stop_and_resend_without_usage_preserves_last_measurement(self):
        self.runner._persist_message({"role": "system", "content": "rules"})
        self.runner._persist_message({"role": "user", "content": "中" * 240000})
        self.runner._record_context_usage(60000)
        checkpoint = self.store.load()["meta"]["context_usage_anchor"]
        runtime = AgentRuntime()

        def stop_before_response(**kwargs):
            runtime.stop(self.project, self.sid)
            raise RuntimeError("Connection error after stop")

        # Run synchronously so each send observes the previous turn's completed stop.
        with patch("GalTransl.Agent.registry.threading.Thread") as thread:
            thread.side_effect = lambda **kwargs: SimpleNamespace(start=kwargs["target"])
            for index, (model, text) in enumerate([
                ("test-model", "continue"), ("model-alias", "retry"),
                ("model-alias", "retry again"),
            ]):
                with self.subTest(model=model, text=text):
                    backend = LLMBackend(Mock(), model, 128000, False)
                    backend.client.chat.completions.create.side_effect = stop_before_response
                    with patch("GalTransl.Agent.runner.resolve_llm_backend", return_value=backend):
                        status = runtime.message(self.project, text, self.sid)
                    state = runtime._get_state(self.project, self.sid)
                    runner = runtime._runners[runtime._key(self.project)][self.sid]
                    expected = 60000 + sum(
                        _estimate_message_tokens(message) for message in state.messages[2:]
                    )
                    self.assertEqual(status["status"], "stopped")
                    self.assertEqual(status["context"]["used_tokens"], expected)
                    self.assertEqual(runner._estimate_context_tokens(), expected)
                    usage = next(event for event in reversed(state.transient_events)
                                 if event.type == "context_usage")
                    self.assertEqual(usage.data["context"]["used_tokens"], expected)
                    self.assertFalse(runner._begin_compaction())
                    self.assertEqual(self.store.load()["meta"]["context_usage_anchor"], checkpoint)
                    self.assertEqual(self.restore()[1]["used_tokens"], expected)
                    if index == 1:
                        runtime = AgentRuntime()

    def test_temporary_compaction_request_does_not_overwrite_last_checkpoint(self):
        self.runner._persist_message({"role": "user", "content": "hello"})
        self.runner._record_context_usage(60000)
        self.runner._pending_compaction = {"instruction": {}}
        self.state.messages.append({"role": "user", "content": "temporary instruction", "_compact_instruction": True})
        self.runner._openai_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kwargs: iter([SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=70000))]))))
        self.runner._stream_llm_response()
        self.assertEqual(self.store.load()["meta"]["context_usage_anchor"]["prompt_tokens"], 60000)
        self.assertEqual(self.state.last_prompt_tokens, 60000)


if __name__ == "__main__":
    unittest.main()
