"""Exercise cache history through real HTTP writes and agent tools."""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from GalTransl import CACHE_FOLDERNAME
from GalTransl.Agent.cache_edits import cache_edit_source
from GalTransl.Agent.models import AgentState
from GalTransl.Agent.runner import AgentRunner
from GalTransl.Agent.subagent import SubAgentRunner, _subagent_handlers
from GalTransl.Agent.session_store import SessionStore
from GalTransl.CacheHistory import history_dir, read_record
from GalTransl.server import JobRegistry, build_handler
from GalTransl.server_runtime import encode_project_dir


class CacheEditHistoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.project = Path(self.directory.name) / "project"
        self.cache = self.project / CACHE_FOLDERNAME
        self.cache.mkdir(parents=True)
        (self.project / "config.yaml").write_text("common: {}\n", encoding="utf-8")
        self.env = patch.dict(os.environ, {"GALTRANSL_DATA_DIR": str(Path(self.directory.name) / "data")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.session_root = patch("GalTransl.Agent.session_store.SESSIONS_ROOT", str(Path(self.directory.name) / "sessions"))
        self.session_root.start()
        self.addCleanup(self.session_root.stop)
        self.original = [{"index": 1, "pre_src": "原文", "post_src": "原文", "pre_dst": "旧译文", "name": "", "trans_by": "original"},
                         {"index": 9, "post_src": "第二句", "pre_dst": "未修改"}]
        self.write("a.json", self.original)
        self.write("b.json", self.original)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(JobRegistry()))
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        self.path = f"/api/projects/{encode_project_dir(str(self.project))}/cache"
        self.runner = AgentRunner(AgentState(project_dir=str(self.project), config_file_name="config.yaml", session_id="session", permission_mode="auto"))
        self.runner.base_url = self.base
        self.runner._cache_turn_id = "turn"
        self.runner._model = "agent-model"

    def stop_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(3)

    def write(self, filename, entries):
        (self.cache / filename).write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")

    def read(self, filename="a.json"):
        return json.loads((self.cache / filename).read_text(encoding="utf-8"))

    def request(self, suffix, body):
        request = urllib.request.Request(self.base + self.path + suffix, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    def history(self, action="list", **args):
        return self.request("/history", {"action": action, **args})

    def edit(self, filename="a.json", index=1, **fields):
        return self.runner._dispatch_tool("edit_transl_cache", {"action": "set", "filename": filename, "patches": [{"index": index, **fields}], "reason": "统一译文"})

    def test_main_and_child_history_is_searchable_and_restorable_after_restart(self):
        result = self.edit(pre_dst="主 Agent 修改")
        with cache_edit_source({"actor_id": "child-task", "actor_label": "校对", "turn_id": "turn", "reason": "子任务"}):
            body = self.read()
            body[1]["pre_dst"] = "子 Agent 修改"
            self.runner._http_post(self.path + "/save", {"filename": "a.json", "entries": body})
        records = self.history()["items"]
        self.assertEqual({row["actor_id"] for row in records}, {"main", "child-task"})
        self.assertEqual({row["turn_id"] for row in records}, {"turn"})
        self.assertEqual(self.history(query="子 Agent 修改")["total"], 1)
        self.assertEqual(self.history(actor_id="child-task")["total"], 1)
        self.assertEqual(result["files"][0]["change_id"], records[1]["change_id"])
        ids = [row["change_id"] for row in records]
        diff = self.history("diff", change_ids=ids)["items"][0]
        self.assertIn("旧译文", diff["diff"])
        self.assertIn("主 Agent 修改", diff["diff"])
        restarted = AgentRunner(self.runner.state)
        restarted.base_url = self.base
        reverted = restarted._dispatch_tool("cache_history", {"action": "revert", "session_id": "session", "turn_id": "turn"})
        self.assertEqual(reverted["updated"], 2)
        self.assertEqual(self.read()[0]["pre_dst"], "旧译文")
        self.assertEqual(self.read()[0]["trans_by"], "original")
        self.assertEqual(self.read()[1]["pre_dst"], "未修改")
        self.assertTrue(all(row.get("reverted_by") for row in self.history(turn_id="turn")["items"] if row["operation"] != "revert"))
        redone = self.history("revert", change_ids=[reverted["history"]["change_id"]])
        self.assertEqual(redone["status"], "reverted")
        self.assertEqual(self.read()[0]["pre_dst"], "主 Agent 修改")

    def test_conflict_blocks_whole_batch_and_independent_edits_survive(self):
        self.edit(pre_dst="第一步")
        self.edit(filename="b.json", pre_dst="第二步")
        ids = [row["change_id"] for row in self.history()["items"]]
        changed = self.read("b.json")
        changed[0]["pre_dst"] = "用户后续修改"
        self.write("b.json", changed)
        conflict = self.history("revert", change_ids=ids)
        self.assertEqual(conflict["status"], "conflict")
        self.assertEqual(self.read()[0]["pre_dst"], "第一步")
        self.assertEqual(self.read("b.json")[0]["pre_dst"], "用户后续修改")
        self.assertEqual(self.history()["total"], 2)
        changed = self.read()
        changed[0]["name"] = "新姓名"
        changed[1]["pre_dst"] = "无关条目修改"
        self.write("a.json", changed)
        undo = self.history("revert", change_ids=[ids[1]])
        self.assertEqual(undo["status"], "reverted")
        self.assertEqual(self.read()[0]["name"], "新姓名")
        self.assertEqual(self.read()[1]["pre_dst"], "无关条目修改")

    def test_delete_rows_files_and_undo_undo(self):
        self.runner._dispatch_tool("edit_transl_cache", {"action": "delete", "filename": "a.json", "indexes": "1"})
        self.assertEqual([row["index"] for row in self.read()], [9])
        deletion = self.history()["items"][0]
        self.assertEqual(self.history("revert", change_ids=[deletion["change_id"]])["status"], "reverted")
        self.assertEqual([row["index"] for row in self.read()], [1, 9])
        self.runner._dispatch_tool("edit_transl_cache", {"action": "delete", "filename": "a.json"})
        deletion = self.history()["items"][0]
        self.assertFalse((self.cache / "a.json").exists())
        undo = self.history("revert", change_ids=[deletion["change_id"]])
        self.assertEqual(self.read()[0]["pre_dst"], "旧译文")
        self.history("revert", change_ids=[undo["history"]["change_id"]])
        self.assertFalse((self.cache / "a.json").exists())

    def test_noop_preview_paging_and_invalid_selectors(self):
        result = self.runner._dispatch_tool("search_replace_cache", {"action": "replace", "query": "旧", "replacement": "新", "dry_run": True})
        self.assertEqual(result["would_update"], 2)
        self.assertEqual(self.history()["total"], 0)
        self.edit(pre_dst="旧译文")
        self.assertEqual(self.history()["total"], 0)
        self.runner._dispatch_tool("search_replace_cache", {"action": "replace", "query": "旧", "replacement": "新"})
        first = self.history(limit=1)
        self.assertTrue(first["has_more"])
        second = self.history(limit=1, offset=first["next_offset"])
        self.assertFalse(second["has_more"])
        preview = self.history("revert", change_ids=[first["items"][0]["change_id"]], dry_run=True)
        self.assertEqual(preview["updated"], 0)
        self.assertEqual(preview["would_update"], 1)
        self.assertEqual(self.history()["total"], 2)
        for args in ({"action": "revert"}, {"action": "revert", "change_ids": ["../bad"]}, {"action": "list", "limit": 0}, {"action": "list", "unused": True}):
            with self.subTest(args=args), self.assertRaises(urllib.error.HTTPError) as error:
                self.request("/history", args)
            self.assertEqual(error.exception.code, 400)

    def test_later_fields_are_not_in_a_selected_diff(self):
        first = self.edit(pre_dst="第一步")["files"][0]["change_id"]
        self.edit(proofread_comment="不选中的修改")
        last = self.edit(pre_dst="最后一步")["files"][0]["change_id"]
        diff = self.history("diff", change_ids=[first, last])["items"][0]
        self.assertNotIn("不选中的修改", diff["diff"])
        self.assertIn("旧译文", diff["diff"])
        self.assertIn("最后一步", diff["diff"])

    def test_turn_summary_and_transcript_keep_edit_ids(self):
        self.runner._store = SessionStore(str(self.project), "session", directory=str(Path(self.directory.name) / "sessions"))
        self.edit(pre_dst="新版")
        self.runner._close_turn("done", {"summary": "已完成"})
        events = [event.to_dict() for event in self.runner.state.events]
        self.assertEqual(events[-2]["type"], "finish")
        self.assertEqual(events[-1]["type"], "cache_edit_summary")
        self.assertEqual(events[-1]["edits"][0]["actor_id"], "main")
        restored = self.runner._store.load()["events"]
        self.assertTrue(any(event["type"] == "cache_edit" for event in restored))

    def test_actual_proofread_child_keeps_original_turn_after_parent_moves_on(self):
        child = SubAgentRunner(self.runner, agent="proofread", files=["a.json"], indexes="1", brief="修正术语", delegation_id="child")
        handlers = _subagent_handlers("proofread", ["a.json"], indexes="1", fixer=child.fixer)
        def call(name, args):
            return SimpleNamespace(id=name, function=SimpleNamespace(name=name, arguments=json.dumps(args)))
        child._run_tool(call("read_transl_cache", {"filename": "a.json", "index": "1"}), handlers)
        self.runner._cache_turn_id = "next-turn"
        result = child._run_tool(call("edit_transl_cache", {"action": "set", "filename": "a.json", "patches": [{"index": 1, "dst": "校对译文"}]}), handlers)
        self.assertNotIn("_is_error", result)
        record = self.history()["items"][0]
        self.assertEqual(record["actor_id"], "child")
        self.assertEqual(record["turn_id"], "turn")
        self.assertEqual(record["tool_call_id"], "edit_transl_cache")
        undo = self.history("revert", change_ids=[record["change_id"]])
        self.assertEqual(undo["updated"], 1)
        self.assertEqual(self.read()[0]["pre_dst"], "旧译文")

    def test_empty_deleted_file_can_be_restored(self):
        self.write("empty.json", [])
        self.request("/delete-file", {"filenames": ["empty.json"]})
        deleted = self.history()["items"][0]
        undo = self.history("revert", change_ids=[deleted["change_id"]])
        self.assertEqual(undo["status"], "reverted")
        self.assertEqual(self.read("empty.json"), [])

    def test_journal_stores_only_changed_rows_and_restores_their_positions(self):
        result = self.edit(pre_dst="新译文")
        record = read_record(str(self.project), result["files"][0]["change_id"])
        self.assertEqual(len(record["files"][0]["before"]), 1)
        self.assertEqual(len(record["files"][0]["after"]), 1)
        self.runner._dispatch_tool("edit_transl_cache", {"action": "delete", "filename": "a.json", "indexes": "9"})
        deleted = self.history()["items"][0]
        self.history("revert", change_ids=[deleted["change_id"]])
        self.assertEqual([row["index"] for row in self.read()], [1, 9])
        self.assertEqual(self.read()[0]["pre_dst"], "新译文")

    def test_failed_multi_file_revert_keeps_actual_changes_queryable(self):
        self.edit(pre_dst="修改一")
        self.edit(filename="b.json", pre_dst="修改二")
        ids = [record["change_id"] for record in self.history()["items"]]
        from GalTransl.server import _save_cache_entries
        def fail_second(project, filename, entries, config):
            if filename == "a.json":
                raise OSError("保存失败")
            return _save_cache_entries(project, filename, entries, config)
        with patch("GalTransl.server._save_cache_entries", side_effect=fail_second):
            with self.assertRaises(urllib.error.HTTPError):
                self.history("revert", change_ids=ids)
        actual = self.history()["items"][0]
        self.assertEqual(actual["reverts"], [])
        self.assertEqual(actual["files"][0]["filename"], "b.json")
        self.assertEqual(self.read("b.json")[0]["pre_dst"], "旧译文")
        self.assertEqual(self.read("a.json")[0]["pre_dst"], "修改一")
        self.assertEqual(self.history("revert", change_ids=[actual["change_id"]])["status"], "reverted")
        self.assertEqual(self.read("b.json")[0]["pre_dst"], "修改二")

    def test_turn_with_edit_and_revert_has_no_net_changes(self):
        changed = self.edit(pre_dst="错误修改")["files"][0]["change_id"]
        self.runner._dispatch_tool("cache_history", {"action": "revert", "change_ids": [changed]})
        rows = self.history(session_id="session", turn_id="turn")["items"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(self.history("diff", session_id="session", turn_id="turn")["items"][0]["diff"], "")
        result = self.history("revert", session_id="session", turn_id="turn")
        self.assertEqual(result["status"], "no_change")
        self.assertEqual(self.read()[0]["pre_dst"], "旧译文")

    def test_regex_search_preview_replace_and_history_undo_share_matches(self):
        entries = [{"index": 1, "post_src": "原文", "name": "対象", "pre_dst": "item-12 ITEM-34"},
                   {"index": 9, "post_src": "第二句", "name": "その他", "pre_dst": "item-56"}]
        self.write("a.json", entries)
        args = {"files": "a.json", "query": r"item-(?P<number>\d+)", "regex": True,
                "conditions": [{"field": "name", "query": "^対象$", "options": {"re": True}}]}
        found = self.runner._dispatch_tool("search_replace_cache", {"action": "search", **args})
        self.assertEqual([row["index"] for row in found["results"]], [1])
        replacement = {"action": "replace", "replacement": r"编号\g<number>", **args}
        preview = self.runner._dispatch_tool("search_replace_cache", {**replacement, "dry_run": True})
        self.assertEqual(self.history()["total"], 0)
        applied = self.runner._dispatch_tool("search_replace_cache", replacement)
        self.assertEqual(applied["changes"], preview["changes"])
        self.assertEqual((applied["matches"], applied["updated"]), (2, 1))
        self.assertEqual(self.read()[0]["pre_dst"], "编号12 编号34")
        self.assertEqual(self.read()[1]["pre_dst"], "item-56")
        record = self.history()["items"][0]
        self.history("revert", change_ids=[record["change_id"]])
        self.assertEqual(self.read()[0]["pre_dst"], "item-12 ITEM-34")

    def test_regex_search_preserves_whitespace_and_case_options_over_http(self):
        self.write("a.json", [{"index": 1, "post_src": "原文", "pre_dst": " word ABC "},
                              {"index": 9, "post_src": "第二句", "pre_dst": "word abc"}])
        for query, match_case, expected in ((" ", False, [1, 9]), (r" word ABC\ ", False, [1]),
                                             ("^word abc$", True, [9]), ("word abc", False, [1, 9])):
            with self.subTest(query=query):
                result = self.runner._dispatch_tool("search_replace_cache", {"action": "search", "files": "a.json", "query": query,
                                                                           "regex": True, "match_case": match_case})
                self.assertEqual([row["index"] for row in result["results"]], expected)


if __name__ == "__main__":
    unittest.main()
