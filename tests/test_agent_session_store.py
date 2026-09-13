"""会话落盘（JSONL）的单元测试。

验证 SessionStore 的写读往返、损坏行容错、会话列表/新建/删除，
以及"项目名+序号"标题的递增逻辑。
"""

import os
import tempfile
import unittest

from GalTransl.Agent import session_store as ss


class SessionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例一个独立根目录，避免污染真实 agent_sessions
        self._root = tempfile.mkdtemp(prefix="agent-store-test-")
        self._orig_root = ss.SESSIONS_ROOT
        ss.SESSIONS_ROOT = self._root
        self.project = os.path.join(tempfile.mkdtemp(prefix="agent-proj-"), "MyGame")
        os.makedirs(self.project, exist_ok=True)

    def tearDown(self) -> None:
        ss.SESSIONS_ROOT = self._orig_root

    def test_round_trip(self) -> None:
        sid = ss.create_session(self.project, "MyGame1")
        store = ss.SessionStore(self.project, sid)
        store.append_message({"role": "user", "content": "你好"})
        store.append_message({"role": "assistant", "content": "hi"})
        store.append_event({"type": "thought", "step": 1, "content": "x"})
        store.append_compact(removed=5, summary_chars=100, tokens_before=900)

        data = store.load()
        self.assertEqual(data["meta"]["title"], "MyGame1")
        self.assertEqual(len(data["messages"]), 2)
        self.assertEqual(data["messages"][0]["content"], "你好")
        self.assertEqual(len(data["events"]), 1)
        self.assertEqual(len(data["compactions"]), 1)

    def test_high_frequency_events_are_not_persisted(self) -> None:
        """thought_delta / wait_tick 是高频增量，落盘会被丢弃。"""
        sid = ss.create_session(self.project, "t")
        store = ss.SessionStore(self.project, sid)
        store.append_event({"type": "thought_delta", "step": 1, "delta": "a"})
        store.append_event({"type": "wait_tick", "step": 2, "remaining_ms": 1})
        store.append_event({"type": "thought", "step": 3, "content": "kept"})
        self.assertEqual(len(store.load()["events"]), 1)

    def test_corrupted_line_is_skipped(self) -> None:
        """进程被强杀写了一半的行不能让整个会话不可读。"""
        sid = ss.create_session(self.project, "t")
        store = ss.SessionStore(self.project, sid)
        store.append_message({"role": "user", "content": "before"})
        with open(store.path, "a", encoding="utf-8") as f:
            f.write('{"t":"message","msg":{"role":"us')  # 半截 JSON
        store.append_message({"role": "user", "content": "after"})

        data = store.load()
        self.assertEqual(len(data["messages"]), 2)
        self.assertEqual(data["messages"][1]["content"], "after")

    def test_load_missing_file_returns_empty(self) -> None:
        store = ss.SessionStore(self.project, "does-not-exist")
        data = store.load()
        self.assertEqual(data["messages"], [])
        self.assertEqual(data["events"], [])

    def test_clear_removes_file(self) -> None:
        sid = ss.create_session(self.project, "t")
        store = ss.SessionStore(self.project, sid)
        store.append_message({"role": "user", "content": "x"})
        self.assertTrue(os.path.isfile(store.path))
        store.clear()
        self.assertFalse(os.path.isfile(store.path))

    def test_list_sessions_and_delete(self) -> None:
        sid1 = ss.create_session(self.project, "MyGame1")
        sid2 = ss.create_session(self.project, "MyGame2")
        titles = {s["title"] for s in ss.list_sessions(self.project)}
        self.assertEqual(titles, {"MyGame1", "MyGame2"})

        ss.delete_session(self.project, sid2)
        remaining = ss.list_sessions(self.project)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["session_id"], sid1)

    def test_next_title_increments_numeric_suffix(self) -> None:
        self.assertEqual(ss.next_session_title(self.project, "MyGame"), "MyGame1")
        ss.create_session(self.project, "MyGame1")
        self.assertEqual(ss.next_session_title(self.project, "MyGame"), "MyGame2")
        ss.create_session(self.project, "MyGame2")
        self.assertEqual(ss.next_session_title(self.project, "MyGame"), "MyGame3")

    def test_next_title_ignores_non_numeric_suffix(self) -> None:
        """手动改过的标题（如"我的存档"）不应参与序号计算。"""
        ss.create_session(self.project, "MyGame")
        ss.create_session(self.project, "我的存档")
        self.assertEqual(ss.next_session_title(self.project, "MyGame"), "MyGame1")

    def test_project_dir_encoding_is_filename_safe(self) -> None:
        """含中文/空格/盘符的项目路径必须编码成安全文件名。"""
        weird = os.path.join(tempfile.mkdtemp(), "我的 游戏 (v2)")
        os.makedirs(weird, exist_ok=True)
        token = ss.encode_project_dir(weird)
        self.assertNotIn(":", token)
        self.assertNotIn("\\", token)
        self.assertNotIn("/", token)
        self.assertNotIn(" ", token)
        # 同一路径编码稳定
        self.assertEqual(token, ss.encode_project_dir(weird))

    def test_sessions_isolated_per_project(self) -> None:
        other = os.path.join(tempfile.mkdtemp(prefix="agent-proj2-"), "Other")
        os.makedirs(other, exist_ok=True)
        ss.create_session(self.project, "Mine")
        ss.create_session(other, "Theirs")
        self.assertEqual([s["title"] for s in ss.list_sessions(self.project)], ["Mine"])
        self.assertEqual([s["title"] for s in ss.list_sessions(other)], ["Theirs"])


if __name__ == "__main__":
    unittest.main()