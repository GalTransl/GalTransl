"""Direct proofreading: scope, stale reads, durable records and bounded reports."""

import copy
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from GalTransl.Agent import session_store, subagent
from GalTransl.Agent.core import AgentStopRequested
from GalTransl.Agent.models import AgentToolError
from GalTransl.Agent.permissions import _tool_risk, PERMISSION_EDIT, PERMISSION_READ
from GalTransl.Agent.tools import proofread
from GalTransl.Agent.tools.render_md import _render_tool_result_table
from tests.test_agent_subagent import _Parent, _Call, _run, ENTRY


class ProofreadFixTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = patch.object(session_store, "SESSIONS_ROOT", self.directory.name)
        self.store.start()
        self.addCleanup(self.store.stop)
        self.parent = _Parent({"a.json": [dict(ENTRY, index=1), dict(ENTRY, index=2)]})
        self.fixer = proofread.ProofreadFixer(self.parent, "test-task", self.parent.stop_event)
        self.handlers = subagent._subagent_handlers(
            "proofread", ["a.json"], indexes="1", fixer=self.fixer,
            stop_event=self.parent.stop_event,
        )

    def read(self, index="1", **kwargs):
        return self.handlers["read_transl_cache"](self.parent, {"filename": "a.json", "index": index, **kwargs})

    def edit(self, **kwargs):
        return self.handlers["patch_transl_cache"](self.parent, {
            "filename": "a.json", "patches": [{"index": 1, **kwargs}],
        })

    def test_proofread_schema_always_allows_effective_translation_edits(self):
        fields = subagent._subagent_patch_schema()["function"]["parameters"]["properties"]["patches"]["items"]["properties"]
        self.assertEqual(set(fields), {"file", "index", "proofread_comment", "dst"})
        schema = next(t for t in subagent.AGENT_TOOLS if t["function"]["name"] == "run_subagents")
        self.assertNotIn("mode", schema["function"]["parameters"]["properties"]["tasks"]["items"]["properties"])
        self.assertEqual(_tool_risk("read_proofread_changes"), PERMISSION_READ)
        self.assertEqual(_tool_risk("revert_proofread_changes"), PERMISSION_EDIT)

    def test_fix_requires_read_and_detects_changes_since_read(self):
        self.assertEqual(self.edit(dst="new")["updated"], 0)
        self.read()
        self.parent.files["a.json"][0]["pre_dst"] = "manual edit"
        result = self.edit(dst="new")
        self.assertIn("内容已变化", result["files"][0]["error"])
        self.assertEqual(self.parent.saves, [])
        self.read()
        self.assertEqual(self.edit(dst="new")["updated"], 1)

    def test_source_and_comment_changes_also_conflict(self):
        for field in ("post_src", "proofread_comment", "name"):
            with self.subTest(field=field):
                self.read()
                self.parent.files["a.json"][0][field] = "changed"
                self.assertEqual(self.edit(dst="new")["updated"], 0)
        self.assertFalse(self.parent.saves)

    def test_active_proofread_translation_is_edited_and_undo_is_guarded(self):
        self.parent.files["a.json"][0].update(proofread_dst="old proofread", proofread_comment="fix this")
        self.read()
        result = self.edit(dst="fixed", proofread_comment="")
        entry = self.parent.files["a.json"][0]
        self.assertEqual(entry["pre_dst"], ENTRY["pre_dst"])
        self.assertEqual(entry["proofread_dst"], "fixed")
        self.assertEqual(entry["proofread_comment"], "")
        self.assertEqual(result["files"][0]["verification"], "checked")
        records = proofread._tool_read_proofread_changes(self.parent, {"task_id": "test-task"})
        row = records["changes"][0]
        self.assertEqual(row["status"], "applied")
        self.assertEqual(row["before"]["proofread_dst"], "old proofread")
        self.assertEqual(row["after"]["proofread_dst"], "fixed")
        undone = proofread._tool_revert_proofread_changes(self.parent, {"change_id": row["change_id"]})
        self.assertEqual(undone["updated"], 1)
        self.assertEqual(self.parent.files["a.json"][0]["proofread_dst"], "old proofread")
        self.assertEqual(self.parent.files["a.json"][0]["proofread_comment"], "fix this")
        with self.assertRaises(AgentToolError):
            proofread._tool_revert_proofread_changes(self.parent, {"change_id": row["change_id"]})

    def test_no_clearing_comments_without_translation_change(self):
        self.parent.files["a.json"][0]["proofread_comment"] = "pending"
        self.read()
        self.assertEqual(self.edit(proofread_comment="")["updated"], 0)
        self.assertEqual(self.parent.files["a.json"][0]["proofread_comment"], "pending")

    def test_translation_field_bypass_and_empty_translation_are_rejected(self):
        self.read()
        for payload in ({"pre_dst": "bypass"}, {"proofread_dst": "bypass"}, {"dst": ""}, {"dst": 2}):
            with self.subTest(payload=payload):
                self.assertEqual(self.edit(**payload)["updated"], 0)
        self.assertFalse(self.parent.saves)

    def test_context_and_cross_file_writes_are_rejected_before_any_write(self):
        self.read(context=2, only_preceding=False)
        for patch_row in ({"index": 2, "dst": "bad"}, {"index": 1, "file": "b.json", "dst": "bad"}, {"index": True, "dst": "bad"}):
            with self.subTest(patch=patch_row), self.assertRaises(AgentToolError):
                self.handlers["patch_transl_cache"](self.parent, {
                    "filename": "a.json", "patches": [{"index": 1, "dst": "good"}, patch_row],
                })
        self.assertFalse(self.parent.saves)

    def test_scope_applies_to_comments_and_problem_statistics(self):
        handlers = subagent._subagent_handlers("proofread", ["a.json"], indexes="1")
        with self.assertRaises(AgentToolError):
            handlers["patch_transl_cache"](self.parent, {"filename": "a.json", "patches": [{"index": 2, "proofread_comment": "bad"}]})
        self.parent.problems = [{"filename": "a.json", "index": i, "problem": "type: detail"} for i in (1, 2)]
        self.assertEqual(handlers["list_problems"](self.parent, {})["total"], 1)

    def test_overlap_and_invalid_ranges_rejected_before_dispatch(self):
        for tasks in (
            [{"agent": "proofread", "file": "a.json"}] * 2,
            [{"agent": "proofread", "file": "a.json", "indexes": "1-2"}, {"agent": "proofread", "file": "list:a.json", "indexes": "2"}],
            [{"agent": "proofread", "file": "a.json", "indexes": "oops"}],
            [{"agent": "explore", "mode": "fix"}],
        ):
            with self.subTest(tasks=tasks), self.assertRaises(AgentToolError):
                subagent._tool_run_subagents(self.parent, {"tasks": tasks})
        self.assertFalse(self.parent.events)

    def test_sparse_indexes_are_split_using_actual_entries(self):
        self.parent.files["a.json"][1]["index"] = 13
        result = _run(self.parent, {"tasks": [{"agent": "proofread", "file": "a.json", "count": 2}]}, [("done", [])])
        self.assertEqual([r["indexes"] for r in result["tasks"]], ["1", "13"])

    def test_stop_after_read_or_while_waiting_for_file_lock_prevents_commit(self):
        self.read()
        lock = proofread._cache_file_lock(self.parent, "a.json")
        errors = []
        def work():
            try:
                self.edit(dst="late")
            except AgentStopRequested:
                errors.append("stopped")
        with lock:
            thread = threading.Thread(target=work)
            thread.start()
            self.parent.stop_event.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ["stopped"])
        self.assertFalse(self.parent.saves)

    def test_failed_audit_does_not_save_and_ambiguous_save_keeps_record(self):
        self.read()
        with patch.object(proofread, "_write_record", side_effect=OSError("disk full")):
            self.assertEqual(self.edit(dst="new")["updated"], 0)
        self.assertFalse(self.parent.saves)
        with patch.object(self.parent, "_http_post", side_effect=AgentToolError("network lost")):
            self.assertEqual(self.edit(dst="new")["updated"], 0)
        records = proofread._tool_read_proofread_changes(self.parent, {"task_id": "test-task"})
        self.assertEqual(records["changes"][0]["status"], "uncertain")

    def test_unverified_save_is_not_reported_as_checked(self):
        self.read()
        original = self.parent._http_post
        def save(path, body):
            original(path, body)
            return {"success": True, "entries": body["entries"], "verification": "unknown"}
        with patch.object(self.parent, "_http_post", save):
            result = self.edit(dst="new")
        self.assertEqual(result["files"][0]["verification"], "unknown")
        self.assertEqual(len(self.fixer.unverified), 1)
        self.assertIn("未验证", _render_tool_result_table("patch_transl_cache", result))

    def test_complete_flow_returns_summary_and_persists_full_ui_changes(self):
        script = [
            ("read", [_Call("read", "read_transl_cache", '{"filename":"a.json","index":"1"}')]),
            ("fix", [_Call("fix", "patch_transl_cache", '{"filename":"a.json","patches":[{"index":1,"dst":"new"}]}')]),
            ("done", []),
        ]
        result = _run(self.parent, {"tasks": [{"agent": "proofread", "file": "a.json", "indexes": "1"}]}, script)
        row = result["tasks"][0]
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["modified_count"], 1)
        self.assertEqual(row["read_count"], 1)
        self.assertEqual(self.parent.permission_checks, [])
        event = next(data for kind, data in self.parent.events if kind == "subagent_tool_result" and data["name"] == "patch_transl_cache")
        self.assertEqual(event["result"]["changes"][0]["after"], "new")
        text = _render_tool_result_table("run_subagents", result)
        self.assertIn("直接修改 1 条译文", text)
        self.assertNotIn("→", text)

    def test_large_report_caps_comment_locations_without_losing_count(self):
        agent = subagent.SubAgentRunner(self.parent, agent="proofread", files=["a.json"], indexes="", brief="", delegation_id="report-task")
        agent.proofread_comments = [{"file": "a.json", "index": i, "content": "pending"} for i in range(1000)]
        result = agent._finish("done", "summary")
        self.assertEqual(result["comment_count"], 1000)
        self.assertEqual(len(result["proofread_comment"]), 20)
        text = _render_tool_result_table("run_subagents", {"tasks": [result]})
        self.assertLess(len(text), 2500)

    def test_edited_translation_can_still_be_flagged_for_second_review(self):
        script = [
            ("read", [_Call("read", "read_transl_cache", '{"filename":"a.json","index":"1"}')]),
            ("fix", [_Call("fix", "patch_transl_cache", json.dumps({"filename": "a.json", "patches": [
                {"index": 1, "dst": "fixed", "proofread_comment": "称谓需要结合后文二次确认"},
            ]}))]),
            ("done", []),
        ]
        result = _run(self.parent, {"tasks": [{"agent": "proofread", "file": "a.json"}]}, script)
        task = result["tasks"][0]
        self.assertEqual(task["modified_count"], 1)
        self.assertEqual(task["needs_review_count"], 1)
        self.assertIn("称谓", task["needs_review"][0]["reason"])
        self.assertEqual(self.parent.files["a.json"][0]["pre_dst"], "fixed")
        review = proofread._tool_read_proofread_changes(self.parent, {"task_id": task["id"], "view": "review"})
        self.assertEqual(review["total"], 1)
        self.assertEqual(review["needs_review"][0]["index"], 1)
        self.assertIn("需二次审查的译文", _render_tool_result_table("run_subagents", result))

    def test_review_pagination_includes_existing_comments_without_rewriting(self):
        for index in range(1, 26):
            self.fixer.snapshots["a.json", index] = {"proofread_comment": "still uncertain"}
        agent = subagent.SubAgentRunner(self.parent, agent="proofread", files=["a.json"], indexes="", brief="", delegation_id="review-task")
        agent.fixer = self.fixer
        result = agent._finish("done", "summary")
        self.assertEqual(result["needs_review_count"], 25)
        self.assertEqual(len(result["needs_review"]), 20)
        page = proofread._tool_read_proofread_changes(self.parent, {"task_id": "test-task", "view": "review", "offset": 20, "limit": 10})
        self.assertEqual([r["index"] for r in page["needs_review"]], list(range(21, 26)))
        self.assertFalse(page["has_more"])
        self.assertFalse(self.parent.saves)

    def test_revert_preview_is_read_only_and_later_edit_is_protected(self):
        from GalTransl.Agent.tools.preview import _preview_tool_changes
        self.read()
        result = self.edit(dst="new")
        change_id = result["files"][0]["change_id"]
        count = len(self.parent.saves)
        preview = _preview_tool_changes(self.parent, "revert_proofread_changes", {"change_id": change_id})
        self.assertEqual(preview["changes"][0]["after"], ENTRY["pre_dst"])
        self.assertEqual(len(self.parent.saves), count)
        self.parent.files["a.json"][0]["pre_dst"] = "manual later"
        with self.assertRaises(AgentToolError):
            proofread._tool_revert_proofread_changes(self.parent, {"change_id": change_id})
        self.assertEqual(len(self.parent.saves), count)

    def test_one_file_conflict_does_not_discard_other_file_success(self):
        self.parent.files["b.json"] = [dict(ENTRY, index=1)]
        fixer = proofread.ProofreadFixer(self.parent, "multi", self.parent.stop_event)
        handlers = subagent._subagent_handlers("proofread", ["a.json", "b.json"], fixer=fixer)
        for filename in ("a.json", "b.json"):
            handlers["read_transl_cache"](self.parent, {"filename": filename, "index": "1"})
        self.parent.files["b.json"][0]["pre_dst"] = "changed"
        result = handlers["patch_transl_cache"](self.parent, {"patches": [
            {"file": "a.json", "index": 1, "dst": "fixed"},
            {"file": "b.json", "index": 1, "dst": "stale"},
        ]})
        self.assertEqual(result["updated"], 1)
        self.assertEqual(self.parent.files["a.json"][0]["pre_dst"], "fixed")
        self.assertEqual(self.parent.files["b.json"][0]["pre_dst"], "changed")
        self.assertEqual(fixer.review_entries()[0]["file"], "b.json")

    def test_corrupt_history_is_skipped_with_visible_incomplete_warning(self):
        self.read()
        self.edit(dst="fixed")
        self.fixer.save_report({"status": "done"}, [])
        directory = Path(proofread._record_dir(self.parent))
        bad_records = [b"", b"{", b"\xff", b"[]", b"{}", b'{"task_id":"x","at":null}']
        for number, data in enumerate(bad_records):
            (directory / f"{number:032x}.json").write_bytes(data)
        Path(proofread._review_path(self.parent, "broken-task")).write_text("[]", encoding="utf-8")
        result = proofread._tool_read_proofread_changes(self.parent, {})
        self.assertEqual(result["total_tasks"], 1)
        self.assertEqual(result["tasks"][0]["entries"], 1)
        self.assertEqual(result["tasks"][0]["modified_count"], 1)
        self.assertTrue(result["incomplete"])
        self.assertEqual(result["skipped_record_count"], len(bad_records) + 1)
        page = proofread._tool_read_proofread_changes(self.parent, {"task_id": "test-task"})
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["changes"][0]["after"]["pre_dst"], "fixed")

    def test_corrupt_review_is_distinguished_from_an_empty_review(self):
        path = Path(proofread._review_path(self.parent, "test-task"))
        path.parent.mkdir(parents=True)
        for bad in ("{", "[]", '{"task_id":"test-task","at":1,"status":"done","review":null}'):
            with self.subTest(bad=bad):
                path.write_text(bad, encoding="utf-8")
                result = proofread._tool_read_proofread_changes(self.parent, {"task_id": "test-task", "view": "review"})
                self.assertTrue(result["incomplete"])
                self.assertEqual(result["skipped_record_count"], 1)
                self.assertEqual(result["needs_review"], [])
        missing = proofread._tool_read_proofread_changes(self.parent, {"task_id": "missing", "view": "review"})
        self.assertEqual(missing["needs_review"], [])
        self.assertNotIn("incomplete", missing)

    def test_history_read_error_does_not_hide_healthy_records(self):
        self.read()
        self.edit(dst="fixed")
        denied = Path(proofread._record_dir(self.parent)) / ("0" * 32 + ".json")
        denied.write_text("{}", encoding="utf-8")
        original_open = open

        def guarded_open(path, *args, **kwargs):
            if Path(path) == denied:
                raise PermissionError("access denied")
            return original_open(path, *args, **kwargs)

        with patch.object(proofread, "open", guarded_open, create=True):
            result = proofread._tool_read_proofread_changes(self.parent, {"task_id": "test-task"})
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["skipped_record_count"], 1)
        self.assertIn("access denied", result["warnings"][0])

    def test_review_queries_never_load_change_records(self):
        self.fixer.save_report({"status": "done"}, [])
        with patch.object(proofread, "_load_records", side_effect=AssertionError("must not load changes")):
            overview = proofread._tool_read_proofread_changes(self.parent, {"view": "review"})
            self.assertEqual(overview["total_tasks"], 1)
            with patch.object(proofread, "_record_names", side_effect=AssertionError("must not scan")):
                page = proofread._tool_read_proofread_changes(self.parent, {"task_id": "test-task", "view": "review"})
                self.assertEqual(page["total"], 0)

    def test_task_query_opens_only_matching_new_records(self):
        self.read()
        self.edit(dst="fixed")
        other = proofread.ProofreadFixer(self.parent, "other-task", self.parent.stop_event)
        other.read(self.parent, {"filename": "a.json", "index": "2"})
        other.patch(self.parent, {"filename": "a.json", "patches": [{"index": 2, "dst": "other"}]})
        with patch.object(proofread, "_read_record", wraps=proofread._read_record) as reader:
            result = proofread._tool_read_proofread_changes(self.parent, {"task_id": "test-task"})
        self.assertEqual(result["total"], 1)
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(result["changes"][0]["index"], 1)

    def test_legacy_records_remain_queryable_and_revertible(self):
        self.read()
        change_id = self.edit(dst="fixed")["files"][0]["change_id"]
        new_path = Path(proofread._record_path(self.parent, change_id))
        legacy_path = new_path.parent / (change_id + ".json")
        new_path.rename(legacy_path)
        result = proofread._tool_read_proofread_changes(self.parent, {"task_id": "test-task"})
        self.assertEqual(result["changes"][0]["change_id"], change_id)
        reverted = proofread._tool_revert_proofread_changes(self.parent, {"change_id": change_id})
        self.assertEqual(reverted["updated"], 1)
        self.assertEqual(self.parent.files["a.json"][0]["pre_dst"], ENTRY["pre_dst"])

    def test_revert_of_corrupt_record_fails_before_touching_cache(self):
        self.read()
        change_id = self.edit(dst="fixed")["files"][0]["change_id"]
        path = Path(proofread._record_path(self.parent, change_id))
        for bad in ("{", "[]", "{}"):
            with self.subTest(bad=bad):
                path.write_text(bad, encoding="utf-8")
                with patch.object(self.parent, "_http_get", side_effect=AssertionError("must not read cache")):
                    with self.assertRaisesRegex(AgentToolError, "无法读取校对记录"):
                        proofread._tool_revert_proofread_changes(self.parent, {"change_id": change_id})
        self.assertEqual(len(self.parent.saves), 1)

    def test_invalid_query_is_rejected_before_loading_records(self):
        with patch.object(proofread, "_load_records", side_effect=AssertionError("must not scan")):
            for args in ({"view": "invalid"}, {"task_id": "test-task", "offset": "bad"}):
                with self.subTest(args=args), self.assertRaises(AgentToolError):
                    proofread._tool_read_proofread_changes(self.parent, args)

    def test_malformed_cache_rows_fail_cleanly_without_overwriting_or_dropping_rows(self):
        original = self.parent._http_get
        self.read()
        for bad in (None, [], "bad", {}, {"index": None}, {"index": "1"}, {"index": True},
                    {"index": 1.5}, dict(ENTRY, index=1)):
            with self.subTest(bad=bad):
                data = original("/api/projects/proj/cache/a.json")
                data["entries"].append(bad)
                before = copy.deepcopy(data)
                with patch.object(self.parent, "_http_get", return_value=data):
                    with self.assertRaises(AgentToolError):
                        self.read()
                    result = self.edit(dst="fixed")
                    self.assertEqual(result["updated"], 0)
                    self.assertIn("缓存", result["files"][0]["error"])
                self.assertEqual(data, before)
        self.assertFalse(self.parent.saves)

    def test_commit_and_revert_validate_cache_indexes(self):
        self.read()
        change_id = self.edit(dst="fixed")["files"][0]["change_id"]
        data = self.parent._http_get("/api/projects/proj/cache/a.json")
        data["entries"].append({"index": "bad"})
        with self.assertRaises(AgentToolError):
            self.fixer._commit("a.json", "/api/projects/proj/cache/a.json", data, [{"index": 1, "pre_dst": "bad"}])
        with patch.object(self.parent, "_http_get", return_value=data), self.assertRaises(AgentToolError):
            proofread._tool_revert_proofread_changes(self.parent, {"change_id": change_id})
        self.assertEqual(len(self.parent.saves), 1)

    def test_concurrent_finish_saves_and_emits_only_once(self):
        agent = subagent.SubAgentRunner(self.parent, agent="proofread", files=["a.json"], indexes="", brief="", delegation_id="finish-task")
        agent.fixer.snapshots["a.json", 1] = {"proofread_comment": "needs review"}
        saving, release, second_started = threading.Event(), threading.Event(), threading.Event()
        original = agent.fixer.save_report

        def save(result, review):
            saving.set()
            self.assertTrue(release.wait(3))
            original(result, review)

        def second_finish():
            second_started.set()
            return agent._finish("done", "later report")

        with patch.object(agent.fixer, "save_report", side_effect=save) as saver, ThreadPoolExecutor(2) as pool:
            first = pool.submit(agent._finish, "stopped", "first report")
            try:
                self.assertTrue(saving.wait(3))
                second = pool.submit(second_finish)
                self.assertTrue(second_started.wait(3))
            finally:
                release.set()
            result = first.result(timeout=3)
            self.assertEqual(second.result(timeout=3), result)
            self.assertEqual(saver.call_count, 1)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(len([e for e, _ in self.parent.events if e == "subagent_done"]), 1)

    def test_silent_finish_can_emit_cached_result_once(self):
        agent = subagent.SubAgentRunner(self.parent, agent="proofread", files=["a.json"], indexes="", brief="", delegation_id="finish-task")
        result = agent._finish("stopped", "first", emit=False)
        self.assertFalse(self.parent.events)
        self.assertEqual(agent._finish("done", "later"), result)
        self.assertEqual(agent._finish("failed", "again"), result)
        self.assertEqual(len(self.parent.events), 1)
        self.assertEqual(self.parent.events[0][1]["status"], "stopped")

    def test_stop_snapshot_does_not_freeze_report_before_inflight_save_returns(self):
        agent = subagent.SubAgentRunner(self.parent, agent="proofread", files=["a.json"], indexes="", brief="", delegation_id="inflight-task")
        handlers = subagent._subagent_handlers("proofread", ["a.json"], fixer=agent.fixer, stop_event=self.parent.stop_event)
        agent._run_tool(_Call("read", "read_transl_cache", '{"filename":"a.json","index":"1"}'), handlers)
        posting, release = threading.Event(), threading.Event()
        original = self.parent._http_post

        def delayed_post(path, body):
            posting.set()
            self.assertTrue(release.wait(3))
            return original(path, body)

        def worker():
            agent._run_tool(_Call("edit", "patch_transl_cache", json.dumps({
                "filename": "a.json", "patches": [{"index": 1, "dst": "fixed", "proofread_comment": "check context"}],
            })), handlers)
            return agent._finish("stopped", "")

        with patch.object(self.parent, "_http_post", side_effect=delayed_post), ThreadPoolExecutor(1) as pool:
            future = pool.submit(worker)
            try:
                self.assertTrue(posting.wait(3))
                self.parent.stop_event.set()
                provisional = agent._stopped_result()
                self.assertTrue(provisional["report_pending"])
                self.assertEqual(provisional["read_count"], 1)
                self.assertEqual(provisional["modified_count"], 0)
                self.assertFalse(Path(proofread._review_path(self.parent, agent.id)).exists())
            finally:
                release.set()
            final = future.result(timeout=3)
        self.assertEqual(final["modified_count"], 1)
        self.assertEqual(final["needs_review_count"], 1)
        self.assertEqual(agent._stopped_result(), final)
        report = proofread._read_record(proofread._review_path(self.parent, agent.id), review=True)
        self.assertEqual(report["modified_count"], 1)
        self.assertEqual(report["needs_review_count"], 1)
        self.assertEqual(report["status"], "stopped")

    def test_stop_during_last_chat_response_does_not_report_done(self):
        agent = subagent.SubAgentRunner(self.parent, agent="proofread", files=["a.json"], indexes="", brief="", delegation_id="late-chat")

        def stopped_response(*args):
            self.parent.stop_event.set()
            return "late completion", [], "", ""

        with patch.object(agent, "_chat_with_retry", side_effect=stopped_response):
            result = agent.run()
        self.assertEqual(result["status"], "stopped")

    def test_stop_during_final_tool_round_preserves_stopped_status_and_saved_changes(self):
        agent = subagent.SubAgentRunner(self.parent, agent="proofread", files=["a.json"], indexes="", brief="", delegation_id="last-round")
        original = self.parent._http_post

        def save_and_stop(path, body):
            result = original(path, body)
            self.parent.stop_event.set()
            return result

        responses = [
            ("", [_Call("read", "read_transl_cache", '{"filename":"a.json","index":"1"}')], "", ""),
            ("", [_Call("edit", "patch_transl_cache", '{"filename":"a.json","patches":[{"index":1,"dst":"fixed"}]}')], "", ""),
        ]
        with (patch.object(subagent, "SUBAGENT_MAX_ROUNDS", 2),
              patch.object(agent, "_chat_with_retry", side_effect=responses),
              patch.object(self.parent, "_http_post", side_effect=save_and_stop)):
            result = agent.run()
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["modified_count"], 1)
        self.assertEqual(proofread._read_record(proofread._review_path(self.parent, agent.id), review=True)["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
