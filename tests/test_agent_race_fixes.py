"""Agent 运行时的几处并发回归。

- 事件窗口：回合线程 _emit 写、HTTP 线程 status()/drain_events() 读，必须同一把锁，
  否则 deque 边遍历边追加会抛 "mutated during iteration"，瞬态增量也会被 drain 吞掉；
- 删除/重置运行中的会话：回合线程收尾时的写入不能把刚删掉的会话文件建回来；
- running 标记：旧回合迟到的"清标记"不能盖掉已经开跑的新回合；
- 缓存文件读-改-写：并行子代理 patch 同一个文件的不同条目，不能互相覆盖。
"""

import os
import tempfile
import threading
import time
import unittest
import urllib.parse
from types import SimpleNamespace
from unittest.mock import patch

from GalTransl.Agent import session_store as ss
from GalTransl.Agent.runtime import AgentRunner, AgentRuntime, _tool_patch_transl_cache

PROFILE = {"OpenAI-Compatible": {"tokens": [{"modelName": "fake-model", "token": "fake-token"}]}}


class _RuntimeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._root = tempfile.mkdtemp(prefix="agent-race-root-")
        self._orig_root = ss.SESSIONS_ROOT
        ss.SESSIONS_ROOT = self._root
        self.project = os.path.join(tempfile.mkdtemp(prefix="agent-race-"), "Game")
        os.makedirs(self.project, exist_ok=True)

    def tearDown(self) -> None:
        ss.SESSIONS_ROOT = self._orig_root

    def _start(self, rt: AgentRuntime, run=lambda self: None) -> tuple[str, AgentRunner]:
        with patch.object(AgentRunner, "run", run):
            sid = rt.create_session(self.project)["session_id"]
            rt.start(self.project, "config.yaml", PROFILE, goal="翻译", session_id=sid)
        return sid, rt._runners[rt._key(self.project)][sid]


class EventWindowLockTests(_RuntimeCase):
    def test_status_and_drain_while_emitting(self) -> None:
        rt = AgentRuntime()
        sid, runner = self._start(rt)
        errors: list[BaseException] = []
        n_deltas = 400  # 小于瞬态旁路的 maxlen(512)：一条都不该丢

        def emit() -> None:
            try:
                for i in range(n_deltas):
                    runner._emit("content_delta", {"delta": "x", "index": i})
                    runner._emit("tool_call", {"name": "noop"})
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=emit)
        thread.start()
        seen_deltas = 0
        after = 0
        while thread.is_alive() or after < runner.state.step:
            try:
                rt.status(self.project, sid)
                batch = rt.drain_events(self.project, after, sid)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
                break
            for ev in batch:
                after = max(after, int(ev.get("step", 0)))
                if ev.get("type") == "content_delta":
                    seen_deltas += 1
        thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(seen_deltas, n_deltas)


class DeleteRunningSessionTests(_RuntimeCase):
    def _assert_no_resurrection(self, remove) -> None:
        rt = AgentRuntime()
        sid, runner = self._start(rt)
        path = ss.SessionStore(self.project, sid).path
        self.assertTrue(os.path.isfile(path))

        remove(rt, sid)
        # 回合线程此刻才收尾：发终态事件、清 running 标记
        runner._emit("stopped", {"reason": "用户停止"})
        runner.state.status = "stopped"
        runner._write_running_meta(False)

        self.assertFalse(os.path.exists(path))
        self.assertFalse(os.path.exists(f"{path}.meta.json"))
        self.assertNotIn(sid, [s["session_id"] for s in ss.list_sessions(self.project)])

    def test_delete(self) -> None:
        self._assert_no_resurrection(lambda rt, sid: rt.delete_session(self.project, sid))

    def test_reset(self) -> None:
        self._assert_no_resurrection(lambda rt, sid: rt.reset(self.project, sid))


class RunningMetaTests(_RuntimeCase):
    def test_late_clear_does_not_override_new_turn(self) -> None:
        rt = AgentRuntime()
        sid, runner = self._start(rt)
        # 新回合已经开跑（status=running 且标记已写 True），旧线程的 finally 才来清标记
        runner.state.status = "running"
        runner._write_running_meta(True)
        runner._write_running_meta(False)
        self.assertTrue(ss.read_meta(self.project, sid).get("running"))

    def test_clear_after_turn_end(self) -> None:
        rt = AgentRuntime()
        sid, runner = self._start(rt)
        runner.state.status = "failed"
        runner._write_running_meta(False)
        self.assertFalse(ss.read_meta(self.project, sid).get("running"))


class _SlowDiskRunner:
    """/cache/save 整文件覆盖；读与写之间故意留个空档，放大读-改-写竞争。"""

    def __init__(self, entries):
        self.state = SimpleNamespace(config_file_name="config.yaml", project_dir="/fake/project")
        self._model = "agent-model"
        self.disk = {"a.json": [dict(e) for e in entries]}

    def _project_id(self):
        return "proj"

    def _http_get(self, url):
        name = urllib.parse.unquote(url.rsplit("/", 1)[-1])
        rows = [dict(e) for e in self.disk[name]]
        time.sleep(0.05)
        return {"entries": rows}

    def _http_post(self, url, body):
        self.disk[str(body["filename"])] = [dict(e) for e in body["entries"]]
        return {"success": True, "entries": body["entries"]}


class CacheFileLockTests(unittest.TestCase):
    def test_parallel_patches_on_one_file_both_land(self) -> None:
        runner = _SlowDiskRunner(
            [{"index": 1, "pre_dst": "旧一"}, {"index": 2, "pre_dst": "旧二"}]
        )

        def patch_one(index: int, text: str) -> None:
            _tool_patch_transl_cache(
                runner, {"filename": "a.json", "patches": [{"index": index, "pre_dst": text}]}
            )

        threads = [
            threading.Thread(target=patch_one, args=(1, "新一")),
            threading.Thread(target=patch_one, args=(2, "新二")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        by_index = {e["index"]: e["pre_dst"] for e in runner.disk["a.json"]}
        self.assertEqual(by_index, {1: "新一", 2: "新二"})


if __name__ == "__main__":
    unittest.main()
