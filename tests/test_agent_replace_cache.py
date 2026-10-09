import threading
import unittest
import urllib.parse
from copy import deepcopy
from types import SimpleNamespace

from GalTransl.Agent.runtime import (
    AgentToolError, _preview_cache_patch, _render_tool_result_table, _tool_patch_transl_cache,
    _cache_file_lock, _subagent_patch_schema,
)
from GalTransl.Agent.tool_schemas import AGENT_TOOLS


class _Runner:
    def __init__(self, files):
        self.state = SimpleNamespace(config_file_name="config.yaml", project_dir="/replace-test")
        self._model = "agent-model"
        self.disk = deepcopy(files)
        self.saved = []
        self.reads = []
        self.expect_locked = False
        self.listings = 0

    def _project_id(self):
        return "proj"

    def _http_get(self, url):
        if url.endswith("/cache"):
            self.listings += 1
            return {"files": [{"name": name} for name in self.disk] + [{"name": "directory.json", "is_file": False}]}
        name = urllib.parse.unquote(url.rsplit("/", 1)[-1])
        if self.expect_locked:
            assert _cache_file_lock(self, name).locked()
        self.reads.append(name)
        if name not in self.disk:
            raise AgentToolError("missing file")
        return {"entries": deepcopy(self.disk[name])}

    def _http_post(self, url, body):
        assert url.endswith("/cache/save")
        name = body["filename"]
        self.disk[name] = deepcopy(body["entries"])
        self.saved.append(name)
        rows = deepcopy(body["entries"])
        for row in rows:
            if isinstance(row, dict):
                row["problem"] = "remaining issue" if row.get("index") == 1 else ""
        return {"success": True, "entries": rows}


def _args(**overrides):
    return {"action": "replace", "files": ["a.json", "b.json"],
            "query": "old", "replacement": "new", **overrides}


class ReplaceCacheTests(unittest.TestCase):
    def setUp(self):
        self.files = {
            "a.json": [
                {"index": 1, "pre_dst": "old and old", "proofread_dst": "old review",
                 "post_src": "old source", "proofread_comment": "old comment", "name": "old"},
                {"index": 2, "pre_dst": "keep", "trans_by": "translator", "proofread_comment": "keep"},
            ],
            "b.json": [{"index": 3, "pre_dst": "old"}],
            "outside.json": [{"index": 1, "pre_dst": "old"}],
        }

    def test_multiple_files_replace_all_occurrences_only_in_translation_fields(self):
        runner = _Runner(self.files)
        result = _tool_patch_transl_cache(runner, _args())
        row = runner.disk["a.json"][0]
        self.assertEqual(row["pre_dst"], "new and new")
        self.assertEqual(row["proofread_dst"], "new review")
        for field in ("post_src", "proofread_comment", "name"):
            self.assertEqual(row[field], self.files["a.json"][0][field])
        self.assertEqual(row["trans_by"], "agent-model")
        self.assertEqual(runner.disk["a.json"][1], self.files["a.json"][1])
        self.assertEqual(runner.disk["outside.json"], self.files["outside.json"])
        self.assertEqual(runner.reads, ["a.json", "b.json"])
        self.assertEqual(runner.saved, ["a.json", "b.json"])
        self.assertEqual(result["updated"], 2)
        self.assertEqual([c["path"] for c in result["changes"]],
                         ["a.json#1.pre_dst", "a.json#1.proofread_dst", "b.json#3.pre_dst"])
        self.assertEqual(result["files"][0]["verification"], "checked")
        self.assertEqual(result["files"][0]["problems"][0]["index"], 1)

    def test_can_limit_to_one_field(self):
        runner = _Runner(self.files)
        _tool_patch_transl_cache(runner, _args(fields=["proofread_dst"]))
        self.assertEqual(runner.disk["a.json"][0]["pre_dst"], "old and old")
        self.assertEqual(runner.disk["a.json"][0]["proofread_dst"], "new review")
        self.assertEqual(runner.saved, ["a.json"])

    def test_empty_replacement_deletes_matches(self):
        runner = _Runner(self.files)
        _tool_patch_transl_cache(runner, _args(replacement=""))
        self.assertEqual(runner.disk["a.json"][0]["pre_dst"], " and ")
        self.assertEqual(runner.disk["b.json"][0]["pre_dst"], "")

    def test_single_filename_shorthand_and_duplicate_files(self):
        for selection in ({"filename": "a.json"}, {"files": "a.json"}, {"files": ["a.json", "a.json"]}):
            runner = _Runner(self.files)
            args = _args(replacement="old-new")
            args.pop("files")
            args.update(selection)
            result = _tool_patch_transl_cache(runner, args)
            self.assertEqual(runner.reads, ["a.json"])
            self.assertEqual(runner.saved, ["a.json"])
            self.assertEqual(runner.disk["a.json"][0]["pre_dst"], "old-new and old-new")
            self.assertEqual(result["changes"][0]["path"], "#1.pre_dst")
            self.assertEqual(runner.listings, 0)

    def test_wildcard_selects_all_snapshots_excluding_logs_and_other_files(self):
        files = {**self.files, "a.json.append.jsonl": [], "notes.txt": []}
        for selector in ("*", ["*"]):
            with self.subTest(selector=selector):
                runner = _Runner(files)
                runner.expect_locked = True
                result = _tool_patch_transl_cache(runner, _args(files=selector))
                self.assertEqual(runner.saved, ["a.json", "b.json", "outside.json"])
                self.assertEqual(result["updated"], 3)
                self.assertEqual(runner.listings, 1)

    def test_globs_expand_in_order_and_overlaps_are_only_replaced_once(self):
        runner = _Runner(self.files)
        _tool_patch_transl_cache(runner, _args(files=["b.json", "[ab].json", "?.json"], replacement="old-new"))
        self.assertEqual(runner.saved, ["b.json", "a.json"])
        self.assertEqual(runner.disk["b.json"][0]["pre_dst"], "old-new")
        self.assertEqual(runner.disk["outside.json"], self.files["outside.json"])

    def test_glob_preview_matches_execution_and_has_concrete_filenames(self):
        runner = _Runner(self.files)
        args = _args(files="*.json", clear_comment=True)
        preview = _preview_cache_patch(runner, args)
        self.assertEqual(runner.saved, [])
        self.assertEqual(preview["files"], ["a.json", "b.json", "outside.json"])
        result = _tool_patch_transl_cache(runner, args)
        self.assertEqual(preview["changes"], result["changes"])

    def test_unmatched_globs_fail_before_any_file_is_modified(self):
        for files, selectors in ((self.files, "missing*"), (self.files, ["a.json", "A*.json"]), ({}, "*")):
            with self.subTest(selectors=selectors):
                runner = _Runner(files)
                with self.assertRaisesRegex(AgentToolError, "未匹配"):
                    _tool_patch_transl_cache(runner, _args(files=selectors))
                self.assertEqual(runner.saved, [])
                self.assertEqual(runner.reads, [])

    def test_filename_with_literal_brackets_is_preferred_to_glob(self):
        runner = _Runner({"[ab].json": [{"index": 1, "pre_dst": "old"}], **self.files})
        _tool_patch_transl_cache(runner, _args(files="[ab].json"))
        self.assertEqual(runner.saved, ["[ab].json"])

    def test_no_match_or_identical_replacement_is_success_without_writes(self):
        for overrides in ({"query": "missing"}, {"query": "OLD"}, {"replacement": "old"}):
            runner = _Runner(self.files)
            result = _tool_patch_transl_cache(runner, _args(clear_comment=True, **overrides))
            self.assertEqual(result["updated"], 0)
            self.assertEqual(result["changes"], [])
            self.assertEqual(runner.saved, [])
            self.assertEqual(runner.disk, self.files)
            self.assertIn("未保存文件", _render_tool_result_table("patch_transl_cache", result))

    def test_clear_comment_only_applies_to_changed_entries(self):
        runner = _Runner(self.files)
        _tool_patch_transl_cache(runner, _args(clear_comment=True))
        self.assertEqual(runner.disk["a.json"][0]["proofread_comment"], "")
        self.assertEqual(runner.disk["a.json"][1]["proofread_comment"], "keep")

    def test_literal_text_and_whitespace_and_linebreaks_are_preserved(self):
        before = "old \r\nline\\n<br>.* OLD old "
        runner = _Runner({"a.json": [{"index": 1, "pre_dst": before}]})
        _tool_patch_transl_cache(runner, _args(files=["a.json"], query="old ", replacement="new "))
        self.assertEqual(runner.disk["a.json"][0]["pre_dst"], before.replace("old ", "new "))
        runner = _Runner({"a.json": [{"index": 1, "pre_dst": before}]})
        _tool_patch_transl_cache(runner, _args(files=["a.json"], query=".*", replacement="\\1"))
        self.assertEqual(runner.disk["a.json"][0]["pre_dst"], before.replace(".*", "\\1"))

    def test_old_cache_keys_are_read_and_new_keys_are_written(self):
        runner = _Runner({"a.json": [{"index": 1, "pre_zh": "old", "proofread_zh": "old review"}]})
        result = _tool_patch_transl_cache(runner, _args(files=["a.json"]))
        self.assertEqual(runner.disk["a.json"][0]["pre_dst"], "new")
        self.assertEqual(runner.disk["a.json"][0]["proofread_dst"], "new review")
        self.assertEqual([c["before"] for c in result["changes"]], ["old", "old review"])

    def test_preview_is_read_only_and_matches_execution(self):
        runner = _Runner(self.files)
        args = _args(clear_comment=True)
        preview = _preview_cache_patch(runner, args)
        self.assertEqual(runner.saved, [])
        self.assertEqual(runner.disk, self.files)
        result = _tool_patch_transl_cache(runner, args)
        self.assertEqual(preview["changes"], result["changes"])

    def test_preview_returns_none_for_no_change(self):
        self.assertIsNone(_preview_cache_patch(_Runner(self.files), _args(query="missing")))

    def test_missing_file_does_not_prevent_other_files_from_updating(self):
        runner = _Runner(self.files)
        args = _args(files=["missing.json", "a.json"])
        preview = _preview_cache_patch(runner, args)
        result = _tool_patch_transl_cache(runner, args)
        self.assertEqual(result["updated"], 1)
        self.assertIn("error", result["files"][0])
        self.assertEqual(preview["changes"], result["changes"])

    def test_all_files_failing_raises(self):
        with self.assertRaises(AgentToolError):
            _tool_patch_transl_cache(_Runner(self.files), _args(files=["missing.json"]))

    def test_invalid_arguments_are_rejected_before_reading_or_writing(self):
        for overrides in (
            {"action": "unknown"}, {"query": ""}, {"query": None}, {"replacement": None},
            {"files": []}, {"files": ""}, {"files": [""]}, {"files": [1]}, {"files": 1},
            {"filename": "a.json"}, {"fields": []}, {"fields": "pre_dst"},
            {"fields": ["post_src"]}, {"fields": ["proofread_comment"]}, {"fields": [None]},
            {"patches": []},
        ):
            with self.subTest(overrides=overrides):
                runner = _Runner(self.files)
                with self.assertRaises(AgentToolError):
                    _tool_patch_transl_cache(runner, _args(**overrides))
                self.assertEqual(runner.reads, [])
                self.assertEqual(runner.saved, [])

    def test_internal_field_allowlist_is_respected(self):
        runner = _Runner(self.files)
        with self.assertRaises(AgentToolError):
            _tool_patch_transl_cache(runner, _args(fields=["proofread_dst"]), frozenset({"pre_dst"}))
        self.assertEqual(runner.saved, [])

    def test_replacements_are_planned_after_acquiring_file_lock(self):
        runner = _Runner(self.files)
        runner.expect_locked = True
        _tool_patch_transl_cache(runner, _args())

    def test_concurrent_replacements_preserve_both_changes(self):
        runner = _Runner({"a.json": [{"index": 1, "pre_dst": "one two"}]})
        errors = []

        def replace(query, replacement):
            try:
                _tool_patch_transl_cache(runner, _args(files=["a.json"], query=query, replacement=replacement))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=replace, args=("one", "1")),
                   threading.Thread(target=replace, args=("two", "2"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(runner.disk["a.json"][0]["pre_dst"], "1 2")

    def test_schema_exposes_replace_but_proofreaders_keep_patch_contract(self):
        schema = next(t["function"] for t in AGENT_TOOLS if t["function"]["name"] == "patch_transl_cache")
        self.assertEqual(schema["parameters"]["properties"]["action"]["enum"], ["patch", "replace"])
        self.assertEqual([s["type"] for s in schema["parameters"]["properties"]["files"]["anyOf"]], ["string", "array"])
        narrow = _subagent_patch_schema()["function"]["parameters"]
        self.assertEqual(narrow["required"], ["patches"])
        for field in ("action", "files", "query", "replacement", "fields"):
            self.assertNotIn(field, narrow["properties"])


if __name__ == "__main__":
    unittest.main()
