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
            "mode": "patch", "names": [{"src_name": "B", "dst_name": "New Beta"}],
        })
        expected = deepcopy(self.old)
        expected[1]["dst_name"] = "New Beta"
        self.assertEqual(runner.saved, [expected])
        self.assertEqual(runner.names, self.old)
        self.assertEqual(result["mode"], "patch")
        self.assertEqual(result["names_removed"], [])
        self.assertEqual(result["names_added"], [])
        self.assertEqual([c["path"] for c in result["changes"]], ["B"])

    def test_patch_adds_new_rows_and_preserves_explicit_counts(self):
        runner = _Runner(self.old)
        result = _tool_save_name_table(runner, {
            "mode": "patch", "names": [
                {"src_name": "D", "dst_name": "Delta"},
                {"src_name": "E", "dst_name": "Echo", "count": 5},
            ],
        })
        self.assertEqual(runner.saved[-1], self.old + [
            {"src_name": "D", "dst_name": "Delta", "count": 0},
            {"src_name": "E", "dst_name": "Echo", "count": 5},
        ])
        self.assertEqual(result["names_added"], ["D", "E"])
        self.assertEqual(result["total"], 5)

    def test_patch_can_clear_translation_and_update_only_count(self):
        runner = _Runner(self.old)
        _tool_save_name_table(runner, {"mode": "patch", "names": [
            {"src_name": "A", "dst_name": ""},
            {"src_name": "B", "count": 0},
        ]})
        expected = deepcopy(self.old)
        expected[0]["dst_name"] = ""
        expected[1]["count"] = 0
        self.assertEqual(runner.saved[-1], expected)

    def test_duplicate_patches_merge_fields_in_order(self):
        runner = _Runner(self.old)
        _tool_save_name_table(runner, {"mode": "patch", "names": [
            {"src_name": "A", "dst_name": "First", "count": 2},
            {"src_name": "A", "dst_name": "Last"},
        ]})
        self.assertEqual(runner.saved[-1][0], {"src_name": "A", "dst_name": "Last", "count": 2})
        self.assertEqual(len(runner.saved[-1]), len(self.old))

    def test_empty_patch_keeps_whole_table(self):
        runner = _Runner(self.old)
        result = _tool_save_name_table(runner, {"mode": "patch", "names": []})
        self.assertEqual(runner.saved[-1], self.old)
        self.assertEqual(result["changes"], [])
        self.assertIsNone(_preview_tool_changes(_Runner(self.old), "save_name_table", {
            "mode": "patch", "names": [],
        }))

    def test_patch_creates_table_when_empty(self):
        runner = _Runner([])
        result = _tool_save_name_table(runner, {"mode": "patch", "names": [
            {"src_name": "A", "dst_name": "Alpha"},
        ]})
        self.assertEqual(runner.saved[-1], [{"src_name": "A", "dst_name": "Alpha", "count": 0}])
        self.assertEqual(result["names_added"], ["A"])

    def test_overwrite_remains_default_and_can_remove_rows(self):
        for mode in (None, "overwrite"):
            for names in ([self.old[0]], []):
                with self.subTest(mode=mode, names=names):
                    runner = _Runner(self.old)
                    args = {"names": names}
                    if mode:
                        args["mode"] = mode
                    result = _tool_save_name_table(runner, args)
                    self.assertEqual(runner.saved[-1], names)
                    self.assertEqual(result["mode"], "overwrite")
                    self.assertEqual(result["names_removed"], [n["src_name"] for n in self.old[len(names):]])

    def test_patch_preview_matches_written_table_without_removals(self):
        args = {"mode": "patch", "names": [
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

    def test_invalid_mode_or_patch_entries_never_write(self):
        for args in (
            {"mode": "merge", "names": []},
            {"mode": "patch", "names": "A"},
            {"mode": "patch", "names": ["A"]},
            {"mode": "patch", "names": [{"dst_name": "Alpha"}]},
            {"mode": "patch", "names": [{"src_name": " "}]},
        ):
            with self.subTest(args=args):
                runner = _Runner(self.old)
                with self.assertRaises(AgentToolError):
                    _tool_save_name_table(runner, args)
                self.assertEqual(runner.saved, [])

    def test_schema_exposes_patch_mode(self):
        schema = next(t["function"] for t in AGENT_TOOLS if t["function"]["name"] == "save_name_table")
        self.assertEqual(schema["parameters"]["properties"]["mode"]["enum"], ["overwrite", "patch"])


if __name__ == "__main__":
    unittest.main()
