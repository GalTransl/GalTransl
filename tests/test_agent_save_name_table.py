import unittest
from copy import deepcopy

from GalTransl.Agent.runtime import AgentToolError, _preview_tool_changes, _tool_save_name_table
from GalTransl.Agent.tool_schemas import AGENT_TOOLS


class _Runner:
    def __init__(self, names):
        self.names = deepcopy(names)
        self.saved = []

    def _project_id(self):
        return "proj"

    def _http_get(self, path):
        assert path == "/api/projects/proj/name-table"
        return {"names": self.names}

    def _http_post(self, path, body):
        assert path == "/api/projects/proj/name-table/save"
        self.saved.append(deepcopy(body["names"]))
        return {"success": True, "total": len(body["names"])}


class SaveNameTableTests(unittest.TestCase):
    def setUp(self):
        self.old = [
            {"src_name": "A", "dst_name": "Alpha", "count": 12},
            {"src_name": "B", "dst_name": "Beta", "count": 3},
            {"src_name": "C", "dst_name": "", "count": 7},
        ]

    def test_patch_preserves_other_rows_counts_and_raw_empty_translations(self):
        runner = _Runner(self.old)
        result = _tool_save_name_table(runner, {
            "action": "set", "names": [{"src_name": "B", "dst_name": "New Beta"}],
        })
        expected = deepcopy(self.old)
        expected[1]["dst_name"] = "New Beta"
        self.assertEqual(runner.saved, [expected])
        self.assertEqual(runner.names, self.old)
        self.assertEqual(result["action"], "set")
        self.assertEqual(result["names_removed"], [])
        self.assertEqual(result["names_added"], [])
        self.assertEqual([c["path"] for c in result["changes"]], ["B"])

    def test_patch_adds_new_rows_with_zero_initial_counts(self):
        runner = _Runner(self.old)
        result = _tool_save_name_table(runner, {
            "action": "set", "names": [
                {"src_name": "D", "dst_name": "Delta"},
                {"src_name": "E", "dst_name": "Echo"},
            ],
        })
        self.assertEqual(runner.saved[-1], self.old + [
            {"src_name": "D", "dst_name": "Delta", "count": 0},
            {"src_name": "E", "dst_name": "Echo", "count": 0},
        ])
        self.assertEqual(result["names_added"], ["D", "E"])
        self.assertEqual(result["total"], 5)

    def test_patch_can_clear_translation_and_omit_translation(self):
        runner = _Runner(self.old)
        _tool_save_name_table(runner, {"action": "set", "names": [
            {"src_name": "A", "dst_name": ""},
            {"src_name": "B"},
        ]})
        expected = deepcopy(self.old)
        expected[0]["dst_name"] = ""
        self.assertEqual(runner.saved[-1], expected)

    def test_duplicate_patches_merge_fields_in_order(self):
        runner = _Runner(self.old)
        _tool_save_name_table(runner, {"action": "set", "names": [
            {"src_name": "A", "dst_name": "First"},
            {"src_name": "A", "dst_name": "Last"},
        ]})
        self.assertEqual(runner.saved[-1][0], {"src_name": "A", "dst_name": "Last", "count": 12})
        self.assertEqual(len(runner.saved[-1]), len(self.old))

    def test_empty_patch_keeps_whole_table(self):
        runner = _Runner(self.old)
        result = _tool_save_name_table(runner, {"action": "set", "names": []})
        self.assertEqual(runner.saved[-1], self.old)
        self.assertEqual(result["changes"], [])
        self.assertIsNone(_preview_tool_changes(_Runner(self.old), "save_name_table", {
            "action": "set", "names": [],
        }))

    def test_patch_creates_table_when_empty(self):
        runner = _Runner([])
        result = _tool_save_name_table(runner, {"action": "set", "names": [
            {"src_name": "A", "dst_name": "Alpha"},
        ]})
        self.assertEqual(runner.saved[-1], [{"src_name": "A", "dst_name": "Alpha", "count": 0}])
        self.assertEqual(result["names_added"], ["A"])

    def test_explicit_overwrite_can_remove_rows(self):
        for mode in ("overwrite",):
            for names in ([{"src_name": "A", "dst_name": "Alpha"}], []):
                with self.subTest(mode=mode, names=names):
                    runner = _Runner(self.old)
                    args = {"names": names}
                    if mode:
                        args["action"] = mode
                    result = _tool_save_name_table(runner, args)
                    self.assertEqual(runner.saved[-1], self.old[:len(names)])
                    self.assertEqual(result["action"], "overwrite")
                    self.assertEqual(result["names_removed"], [n["src_name"] for n in self.old[len(names):]])

    def test_patch_preview_matches_written_table_without_removals(self):
        args = {"action": "set", "names": [
            {"src_name": "A", "dst_name": "New Alpha"},
            {"src_name": "D", "dst_name": "Delta"},
        ]}
        runner = _Runner(self.old)
        preview = _preview_tool_changes(runner, "save_name_table", args)
        self.assertEqual(runner.saved, [])
        result = _tool_save_name_table(runner, args)
        self.assertEqual(preview["changes"], result["changes"])
        self.assertEqual(result["names_removed"], [])
        self.assertEqual([c["kind"] for c in result["changes"]], ["add", "replace"])

    def test_overwrite_preserves_existing_counts_and_initializes_new_names(self):
        runner = _Runner(self.old)
        args = {"action": "overwrite", "names": [
            {"src_name": "B", "dst_name": "New Beta"}, {"src_name": "D", "dst_name": "Delta"},
        ]}
        preview = _preview_tool_changes(runner, "save_name_table", args)
        self.assertEqual(runner.saved, [])
        result = _tool_save_name_table(runner, args)
        self.assertEqual(runner.saved[-1], [
            {"src_name": "B", "dst_name": "New Beta", "count": 3},
            {"src_name": "D", "dst_name": "Delta", "count": 0},
        ])
        self.assertEqual(result["changes"], preview["changes"])

    def test_count_is_rejected_for_both_write_actions(self):
        for action in ("set", "overwrite"):
            runner = _Runner(self.old)
            args = {"action": action, "names": [{"src_name": "A", "dst_name": "Changed", "count": 99}]}
            with self.assertRaisesRegex(AgentToolError, "只读"):
                _tool_save_name_table(runner, args)
            self.assertIsNone(_preview_tool_changes(runner, "save_name_table", args))
            self.assertEqual(runner.saved, [])

    def test_invalid_mode_or_patch_entries_never_write(self):
        for args in (
            {"action": "merge", "names": []},
            {"action": "set", "names": "A"},
            {"action": "set", "names": ["A"]},
            {"action": "set", "names": [{"dst_name": "Alpha"}]},
            {"action": "set", "names": [{"src_name": " "}]},
        ):
            with self.subTest(args=args):
                runner = _Runner(self.old)
                with self.assertRaises(AgentToolError):
                    _tool_save_name_table(runner, args)
                self.assertEqual(runner.saved, [])

    def test_schema_exposes_patch_mode(self):
        schema = next(t["function"] for t in AGENT_TOOLS if t["function"]["name"] == "names")
        self.assertEqual(schema["parameters"]["properties"]["action"]["enum"], ["read", "set", "overwrite"])
        self.assertNotIn("count", schema["parameters"]["properties"]["names"]["items"]["properties"])


if __name__ == "__main__":
    unittest.main()
