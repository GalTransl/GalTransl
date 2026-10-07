"""子代理前缀缓存预热：首个模型响应后等两秒，再并行启动其余任务。"""

import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from GalTransl.Agent import subagent
from GalTransl.Agent.core import SUBAGENT_CACHE_WARMUP_SECONDS
from tests.test_agent_subagent import _Parent, _Call


def _response(text="完成", calls=None):
    return text, calls or [], "", "", 10


class SubagentStartupTests(unittest.TestCase):
    def setUp(self):
        self.parent = _Parent()
        self.tasks = [
            {"agent": "explore", "brief": label}
            for label in ("first", "second", "third")
        ]

    def run_batch(self, tasks=None):
        return subagent._tool_run_subagents(self.parent, {"tasks": tasks or self.tasks})

    def test_tool_only_first_response_starts_peers_after_two_seconds_before_completion(self):
        entered, release_response, first_still_running = (threading.Event() for _ in range(3))
        release_completion, peers_started = threading.Event(), threading.Event()
        response_at = []
        peer_times = []
        first_calls = 0

        def chat(_client, _model, messages, _tools):
            nonlocal first_calls
            if "first" in messages[1]["content"]:
                first_calls += 1
                if first_calls == 1:
                    entered.set()
                    self.assertTrue(release_response.wait(5))
                    response_at.append(time.monotonic())
                    return _response("", [_Call("list", "list_dict_files", "{}")])
                first_still_running.set()
                self.assertTrue(release_completion.wait(5))
            else:
                peer_times.append(time.monotonic())
                if len(peer_times) == 2:
                    peers_started.set()
            return _response()

        self.assertEqual(SUBAGENT_CACHE_WARMUP_SECONDS, 2.0)
        with patch.object(subagent, "_subagent_chat", chat), \
                patch.object(subagent, "SUBAGENT_CACHE_WARMUP_SECONDS", SUBAGENT_CACHE_WARMUP_SECONDS), \
                ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.run_batch)
            try:
                self.assertTrue(entered.wait(3))
                self.assertFalse(peers_started.wait(0.1))
                self.assertEqual(len([e for e, _ in self.parent.events if e == "subagent_start"]), 1)
                release_response.set()
                self.assertTrue(first_still_running.wait(3))
                self.assertTrue(peers_started.wait(4))
                self.assertTrue(all(t - response_at[0] >= 2.0 for t in peer_times))
                self.assertFalse(future.done())  # 第一个仍在执行，其他两个已启动。
            finally:
                release_response.set()
                release_completion.set()
            result = future.result(timeout=3)
        self.assertEqual([task["status"] for task in result["tasks"]], ["done"] * 3)

    def test_single_agent_does_not_wait_for_cache_warmup(self):
        with patch.object(subagent, "_subagent_chat", return_value=_response()), \
                patch.object(self.parent.stop_event, "wait", side_effect=AssertionError("single agent must not wait")):
            result = self.run_batch(self.tasks[:1])
        self.assertEqual(result["tasks"][0]["status"], "done")

    def test_empty_first_response_also_releases_peers(self):
        with patch.object(subagent, "_subagent_chat", return_value=_response("")) as chat, \
                patch.object(subagent, "SUBAGENT_CACHE_WARMUP_SECONDS", 0):
            result = self.run_batch()
        self.assertEqual(chat.call_count, 3)
        self.assertEqual(len(result["tasks"]), 3)

    def test_first_agent_startup_failure_does_not_block_peers(self):
        real_runner = subagent.SubAgentRunner

        def build(parent, **kwargs):
            if kwargs["brief"] == "first":
                raise RuntimeError("first startup failed")
            return real_runner(parent, **kwargs)

        with patch.object(subagent, "SubAgentRunner", side_effect=build), \
                patch.object(subagent, "_subagent_chat", return_value=_response()) as chat, \
                patch.object(subagent, "SUBAGENT_CACHE_WARMUP_SECONDS", 0):
            result = self.run_batch()
        self.assertEqual(chat.call_count, 2)
        self.assertEqual([task["status"] for task in result["tasks"]], ["failed", "done", "done"])

    def test_stop_before_first_response_does_not_start_peers(self):
        entered, release = threading.Event(), threading.Event()

        def chat(*_args):
            entered.set()
            self.assertTrue(release.wait(3))
            return _response()

        with patch.object(subagent, "_subagent_chat", chat), ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.run_batch)
            try:
                self.assertTrue(entered.wait(3))
                self.parent.stop_event.set()
            finally:
                release.set()
            result = future.result(timeout=3)
        self.assertEqual([task["status"] for task in result["tasks"]], ["stopped"] * 3)
        self.assertEqual(len([e for e, _ in self.parent.events if e == "subagent_start"]), 1)

    def test_first_request_failure_does_not_block_peers(self):
        def chat(_client, _model, messages, _tools):
            if "first" in messages[1]["content"]:
                raise ValueError("first request failed")
            return _response()

        with patch.object(subagent, "_subagent_chat", chat), \
                patch.object(subagent, "SUBAGENT_CACHE_WARMUP_SECONDS", 0):
            result = self.run_batch()
        self.assertEqual([task["status"] for task in result["tasks"]], ["failed", "done", "done"])

    def test_stop_during_warmup_does_not_start_peers(self):
        waiting = threading.Event()
        real_wait = self.parent.stop_event.wait

        def wait(timeout=None):
            if timeout == SUBAGENT_CACHE_WARMUP_SECONDS:
                waiting.set()
            return real_wait(timeout)

        with patch.object(subagent, "_subagent_chat", return_value=_response()) as chat, \
                patch.object(self.parent.stop_event, "wait", side_effect=wait), \
                ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.run_batch)
            try:
                self.assertTrue(waiting.wait(3))
            finally:
                self.parent.stop_event.set()
            result = future.result(timeout=3)
        self.assertEqual(chat.call_count, 1)
        self.assertEqual([task["status"] for task in result["tasks"][1:]], ["stopped"] * 2)


if __name__ == "__main__":
    unittest.main()
