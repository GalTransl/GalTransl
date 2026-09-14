import unittest
from types import SimpleNamespace

from GalTransl.Agent.runtime import _tool_read_output, _tool_read_input_file


class _Runner:
    state = SimpleNamespace(config_file_name="config.yaml")

    def __init__(self, payload):
        self.payload = payload

    def _project_id(self):
        return "project"

    def _http_get(self, _path):
        return self.payload


class AgentIndexSemanticsTests(unittest.TestCase):
    def test_output_index_uses_source_index_and_one_based_missing_check(self):
        runner = _Runner({"entries": [
            {"index": 1, "message": "one", "name": ""},
            {"index": 13, "message": "thirteen", "name": ""},
        ]})
        result = _tool_read_output(runner, {"filename": "scene.json", "index": "13"})
        self.assertEqual(result["entries"][0]["index"], 13)
        self.assertNotIn("missing_indexes", result)

    def test_input_missing_indexes_are_checked_against_actual_indexes(self):
        runner = _Runner({"entries": [
            {"index": 1, "pre_src": "one"},
            {"index": 13, "pre_src": "thirteen"},
        ]})
        result = _tool_read_input_file(runner, {"filename": "scene.json", "index": "13"})
        self.assertEqual(result["entries"][0]["index"], 13)
        self.assertNotIn("missing_indexes", result)


if __name__ == "__main__":
    unittest.main()
