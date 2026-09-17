"""会话落盘（JSONL）的单元测试。

验证 SessionStore 的写读往返、损坏行容错、会话列表/新建/删除，
以及"标题取首条用户消息"的生成逻辑。
"""

import json
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
        store.append_event({"type": "content", "step": 1, "content": "x"})
        store.append_compact(removed=5, summary_chars=100, tokens_before=900, tokens_after=320)

        data = store.load()
        self.assertEqual(data["meta"]["title"], "MyGame1")
        self.assertEqual(len(data["messages"]), 2)
        self.assertEqual(data["messages"][0]["content"], "你好")
        self.assertEqual(len(data["events"]), 1)
        self.assertEqual(len(data["compactions"]), 1)
        # 压缩记录带上压缩前后两个估算值（tokens_after 含保留的尾部）
        self.assertEqual(data["compactions"][0]["tokens_before"], 900)
        self.assertEqual(data["compactions"][0]["tokens_after"], 320)

    def test_high_frequency_events_are_not_persisted(self) -> None:
        """content_delta / wait_tick 是高频增量，落盘会被丢弃。"""
        sid = ss.create_session(self.project, "t")
        store = ss.SessionStore(self.project, sid)
        store.append_event({"type": "content_delta", "step": 1, "delta": "a"})
        store.append_event({"type": "wait_tick", "step": 2, "remaining_ms": 1})
        store.append_event({"type": "content", "step": 3, "content": "kept"})
        self.assertEqual(len(store.load()["events"]), 1)

    def test_tool_result_payload_is_stored_once_and_restored(self) -> None:
        sid = ss.create_session(self.project, "t")
        store = ss.SessionStore(self.project, sid)
        result = {"filename": "scene.json", "entries": [{"index": 1, "pre_src": "很长的内容"}]}
        store.append_message({
            "role": "tool",
            "tool_call_id": "call-1",
            "content": json.dumps(result, ensure_ascii=False),
        })
        store.append_event({
            "type": "tool_result",
            "step": 2,
            "id": "call-1",
            "name": "read_input_file",
            "ok": True,
            "result": result,
            "duration_ms": 20,
        })

        with open(store.path, encoding="utf-8") as f:
            raw = f.read()
        # 外层 JSON 会转义 message.content 内层 JSON 的引号；按记录解析后确认
        # 大结果只存在于 tool message，event 本身不再带 result。
        records = [json.loads(line) for line in raw.splitlines()]
        message_record = next(rec for rec in records if rec.get("t") == "message")
        event_record = next(rec for rec in records if rec.get("t") == "event")
        self.assertEqual(json.loads(message_record["msg"]["content"]), result)
        self.assertNotIn("result", event_record["event"])
        event = store.load()["events"][0]
        self.assertEqual(event["result"], result)

    def test_tool_error_is_restored_from_tool_message(self) -> None:
        sid = ss.create_session(self.project, "t")
        store = ss.SessionStore(self.project, sid)
        store.append_message({"role": "tool", "tool_call_id": "call-2", "content": '{"error":"boom"}'})
        store.append_event({"type": "tool_result", "step": 2, "id": "call-2", "ok": False})
        event = store.load()["events"][0]
        self.assertEqual(event["error"], "boom")
        self.assertFalse(event["ok"])

    def test_tool_error_without_message_is_kept(self) -> None:
        """截断等没有对应 tool message 的事件仍保留错误文本。"""
        sid = ss.create_session(self.project, "t")
        store = ss.SessionStore(self.project, sid)
        store.append_event({"type": "tool_result", "step": 2, "id": "truncated", "ok": False, "error": "响应被截断"})
        event = store.load()["events"][0]
        self.assertEqual(event["error"], "响应被截断")

    def test_meta_records_are_merged(self) -> None:
        """收尾写入 running=false 不能覆盖首条 meta 的会话信息。"""
        sid = ss.create_session(self.project, "MyGame1")
        store = ss.SessionStore(self.project, sid)
        store.append_meta(goal="首条用户输入", config_file_name="config.yaml", running=True)
        store.append_meta(running=False)

        meta = store.load()["meta"]
        self.assertEqual(meta["title"], "MyGame1")
        self.assertEqual(meta["goal"], "首条用户输入")
        self.assertEqual(meta["config_file_name"], "config.yaml")
        self.assertFalse(meta["running"])

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

    def test_meta_sidecar_is_written_and_kept_in_sync(self) -> None:
        """列表用的 sidecar：写 meta 时同步维护，读的时候直接命中它。"""
        sid = ss.create_session(self.project, "MyGame1")
        store = ss.SessionStore(self.project, sid)
        self.assertTrue(os.path.isfile(store.meta_path))

        store.append_meta(goal="接着翻", running=True)
        store.append_meta(running=False)

        meta = ss._read_meta(store.path)
        self.assertEqual(meta["title"], "MyGame1")
        self.assertEqual(meta["goal"], "接着翻")
        self.assertFalse(meta["running"])
        # sidecar 不该被误认成一个会话
        self.assertEqual(len(ss.list_sessions(self.project)), 1)

    def test_legacy_session_without_sidecar_keeps_title(self) -> None:
        """老会话第一次写 meta 时 sidecar 还不存在：必须先从文件补齐再合并。

        否则 sidecar 里只剩这次写的 running=false，title/created_at 全丢——
        会话列表的标题就退化成 session_id 了。
        """
        sid = ss.create_session(self.project, "MyGame1")
        store = ss.SessionStore(self.project, sid)
        created_at = ss._read_meta(store.path)["created_at"]
        os.remove(store.meta_path)  # 模拟"本版本之前建的会话"

        store.append_meta(running=False)

        meta = ss._read_meta(store.path)
        self.assertEqual(meta["title"], "MyGame1")
        self.assertEqual(meta["created_at"], created_at)
        self.assertEqual(ss.list_sessions(self.project)[0]["title"], "MyGame1")

    def test_stale_sidecar_is_ignored(self) -> None:
        """会话文件变了（size/mtime 对不上）就重新扫一遍，不认过期缓存。"""
        sid = ss.create_session(self.project, "MyGame1")
        store = ss.SessionStore(self.project, sid)
        ss._read_meta(store.path)  # 先建出 sidecar

        # 绕过 append_meta 直接追加一行 meta（模拟别处写了文件、sidecar 没跟上）
        with open(store.path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": "meta", "at": 1.0, "goal": "外部写入"}, ensure_ascii=False) + "\n")

        self.assertEqual(ss._read_meta(store.path)["goal"], "外部写入")

    def test_meta_sidecar_survives_bulk_message_appends(self) -> None:
        """会话跑起来后 message/event 一直在追加，标题不能因为"文件变了"就丢。

        水位设计下只会读水位之后新追加的那一段；收尾写 running=false 之后再读
        应当直接命中缓存（内容一致）。
        """
        sid = ss.create_session(self.project, "MyGame1")
        store = ss.SessionStore(self.project, sid)
        ss._read_meta(store.path)  # 先建出 sidecar，水位 = 当前文件大小
        for i in range(200):
            store.append_message({"role": "tool", "tool_call_id": f"c{i}", "content": "x" * 500})
        store.append_meta(running=False)

        meta = ss._read_meta(store.path)
        self.assertEqual(meta["title"], "MyGame1")
        self.assertFalse(meta["running"])
        self.assertEqual(ss._read_meta(store.path), meta)
        self.assertEqual(ss.list_sessions(self.project)[0]["title"], "MyGame1")

    def test_clear_removes_sidecar_too(self) -> None:
        """删会话要把 sidecar 一起删掉，不然会话没了它还留在目录里。"""
        sid = ss.create_session(self.project, "t")
        store = ss.SessionStore(self.project, sid)
        self.assertTrue(os.path.isfile(store.meta_path))
        store.clear()
        self.assertFalse(os.path.isfile(store.path))
        self.assertFalse(os.path.isfile(store.meta_path))

    def test_session_title_from_first_message(self) -> None:
        """标题取首条用户消息：折叠换行/空白，超长截断。"""
        self.assertEqual(ss.title_from_message("帮我翻译这个项目"), "帮我翻译这个项目")
        self.assertEqual(ss.title_from_message("第一行\n第二行"), "第一行 第二行")
        self.assertEqual(ss.title_from_message("  多个   空格  "), "多个 空格")
        long_text = "字" * 50
        title = ss.title_from_message(long_text)
        self.assertEqual(len(title), ss.TITLE_MAX_CHARS + 1)  # 截断 + 省略号
        self.assertTrue(title.endswith("…"))
        self.assertTrue(long_text.startswith(title[:-1]))

    def test_session_title_falls_back_when_message_empty(self) -> None:
        self.assertEqual(ss.title_from_message(""), ss.DEFAULT_TITLE)
        self.assertEqual(ss.title_from_message("   \n "), ss.DEFAULT_TITLE)

    def test_new_session_gets_placeholder_title(self) -> None:
        """新建会话时用户还没输入，先用占位标题（不能再用"项目名+序号"）。"""
        sid = ss.create_session(self.project)
        meta = ss._read_meta(ss.SessionStore(self.project, sid).path)
        self.assertEqual(meta["title"], ss.DEFAULT_TITLE)
        self.assertEqual(ss.session_title(self.project, sid), ss.DEFAULT_TITLE)

    def test_has_user_message_tracks_first_message(self) -> None:
        """首条用户消息落盘后 has_user_message 变真（标题不再重算）。"""
        sid = ss.create_session(self.project)
        self.assertFalse(ss.has_user_message(self.project, sid))
        store = ss.SessionStore(self.project, sid)
        store.append_message({"role": "system", "content": "system"})
        self.assertFalse(ss.has_user_message(self.project, sid))  # system 不算
        store.append_message({"role": "user", "content": "帮我翻译"})
        self.assertTrue(ss.has_user_message(self.project, sid))

    def test_has_user_message_false_for_missing_session(self) -> None:
        self.assertFalse(ss.has_user_message(self.project, "no-such-session"))

    def test_meta_title_update_is_visible_to_list(self) -> None:
        """标题是后补写的 meta（首条消息时改），列表/标题读取必须看到最新值。"""
        sid = ss.create_session(self.project)
        ss.SessionStore(self.project, sid).append_meta(title="帮我翻译这个项目")
        self.assertEqual(ss.list_sessions(self.project)[0]["title"], "帮我翻译这个项目")
        self.assertEqual(ss.session_title(self.project, sid), "帮我翻译这个项目")

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
