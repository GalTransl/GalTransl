import copy
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from GalTransl.server import _scan_plugins
from GalTransl.Agent.models import AgentToolError, AgentState
from GalTransl.Agent.runner import AgentRunner
from GalTransl.Agent.permissions import _tool_risk, _permission_needed
from GalTransl.Agent.tools.plugin_settings import _tool_get_plugin_settings, encoding_recovery
from GalTransl.Agent.tools.project import _tool_update_project_config
from GalTransl.Agent.tools.preview import _preview_config_update
from GalTransl.Agent.tools.jobs import _tool_get_runtime, _tool_wait


class Runner:
    def __init__(self, plugins, config=None):
        self.plugins = plugins
        self.config = copy.deepcopy(config if config is not None else {"plugin": {"filePlugin": "auto"}})
        self.state = SimpleNamespace(config_file_name="custom.yaml")
        self.writes = []

    def _project_id(self):
        return "project-id"

    def _http_get(self, path):
        if path == "/api/projects/project-id/plugins":
            return {"plugins": copy.deepcopy(self.plugins)}
        if path == "/api/projects/project-id/config?config=custom.yaml":
            return {"config": copy.deepcopy(self.config)}
        raise AssertionError(path)

    def _http_put(self, path, body):
        assert path == "/api/projects/project-id/config"
        assert body["config_file_name"] == "custom.yaml"
        self.writes.append(copy.deepcopy(body))
        self.config = copy.deepcopy(body["config"])


class PluginSettingsAgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugins = _scan_plugins()

    def test_discovery_defaults_overrides_paths_and_schema(self):
        runner = Runner(self.plugins, {"plugin": {"filePlugin": "file_msgtool_script",
                          "file_msgtool_script": {"patched_encoding": "utf8"}}})
        result = _tool_get_plugin_settings(runner, {"plugin_name": "file_msgtool_script"})
        self.assertEqual(len(result["plugins"]), 1)
        fields = {f["key"]: f for f in result["plugins"][0]["settings"]}
        jis = fields["plugin.file_msgtool_script.jis_substitution"]
        self.assertIs(jis["value"], False)
        self.assertFalse(jis["overridden"])
        self.assertEqual(jis["schema"]["label"], "JIS 替换")
        enc = fields["plugin.file_msgtool_script.patched_encoding"]
        self.assertEqual(enc["value"], "utf8")
        self.assertEqual(enc["default"], "")
        self.assertTrue(enc["overridden"])
        self.assertEqual(runner.writes, [])

    def test_add_default_key_preview_matches_write_preserves_other_values(self):
        runner = Runner(self.plugins, {"common": {"language": "zh-cn"}, "plugin": {
            "filePlugin": "auto", "textPlugins": ["text_common_normalfix"],
            "file_msgtool_script": {"timeout": 500}}})
        args = {"updates": [{"key": "plugin.file_msgtool_script.jis_substitution", "value": True}]}
        preview = _preview_config_update(runner, args)
        self.assertEqual(runner.writes, [])
        result = _tool_update_project_config(runner, args)
        self.assertEqual(preview["changes"], result["changes"])
        self.assertEqual(result["updated"], 1)
        self.assertEqual(runner.config["plugin"]["file_msgtool_script"], {"timeout": 500, "jis_substitution": True})
        self.assertEqual(runner.config["common"], {"language": "zh-cn"})
        self.assertEqual(runner.config["plugin"]["filePlugin"], "auto")

    def test_missing_plugin_objects_and_repeated_updates(self):
        runner = Runner(self.plugins, {"common": {"language": "zh-cn"}})
        args = {"updates": [
            {"key": "plugin.file_msgtool_script.jis_substitution", "value": True},
            {"key": "plugin.file_msgtool_script.jis_substitution", "value": False}]}
        preview = _preview_config_update(runner, args)
        result = _tool_update_project_config(runner, args)
        self.assertEqual(preview["changes"], result["changes"])
        self.assertIs(result["changes"][1]["before"], True)
        self.assertIs(runner.config["plugin"]["file_msgtool_script"]["jis_substitution"], False)

    def test_invalid_keys_types_options_ranges_and_bulk_overwrite_do_not_write(self):
        for key, value in (
            ("plugin.file_msgtool_script.typo", True),
            ("plugin.file_msgtool_script.jis_substitution", "true"),
            ("plugin.file_msgtool_script.patched_encoding", "made-up"),
            ("plugin.file_msgtool_script.timeout", 0),
            ("plugin.file_msgtool_script.timeout", True),
            ("plugin.file_msgtool_script.timeout", float("nan")),
            ("plugin.file_msgtool_script", {"typo": True}),
            ("plugin", {"file_msgtool_script": {"typo": True}}),
            ("common.made_up", 1),
        ):
            with self.subTest(key=key, value=value):
                runner = Runner(self.plugins)
                args = {"updates": [{"key": key, "value": value}]}
                self.assertIsNone(_preview_config_update(runner, args))
                result = _tool_update_project_config(runner, args)
                self.assertEqual(result["updated"], 0)
                self.assertTrue(result["skipped"])
                self.assertEqual(runner.writes, [])

    def test_literal_dotted_setting_and_secret_redaction(self):
        plugin = {"name": "custom", "module": "custom", "settings": {"a.b": False, "token": "DEFAULT_SECRET"},
                  "settings_schema": {"token": {"secret": True}}}
        runner = Runner([plugin], {"plugin": {"custom": {"token": "PROJECT_SECRET"}}})
        info = _tool_get_plugin_settings(runner, {"plugin_name": "custom"})
        self.assertNotIn("PROJECT_SECRET", json.dumps(info))
        self.assertNotIn("DEFAULT_SECRET", json.dumps(info))
        result = _tool_update_project_config(runner, {"updates": [
            {"key": "plugin.custom.a.b", "value": True},
            {"key": "plugin.custom.token", "value": "other"}]})
        self.assertEqual(result["updated"], 1)
        self.assertIs(runner.config["plugin"]["custom"]["a.b"], True)
        self.assertEqual(runner.config["plugin"]["custom"]["token"], "PROJECT_SECRET")

    def test_project_plugin_overrides_global_declaration(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "plugins/file_msgtool_script/file_msgtool_script.yaml"
            target.parent.mkdir(parents=True)
            target.write_text(yaml.safe_dump({"Core": {"Name": "本地版本", "Module": "file_msgtool_script", "Type": "file"},
                                              "Settings": {"custom": 1}}), encoding="utf-8")
            catalog = _scan_plugins(directory)
            plugin = next(p for p in catalog if p["module"] == "file_msgtool_script")
            self.assertEqual(plugin["settings"], {"custom": 1})
            result = _tool_update_project_config(Runner(catalog), {"updates": [
                {"key": "plugin.file_msgtool_script.jis_substitution", "value": True}]})
            self.assertEqual(result["updated"], 0)

    def test_discovery_failure_cannot_create_keys(self):
        runner = Runner(self.plugins)
        runner._http_get = lambda path: {"config": runner.config}
        with self.assertRaises(AgentToolError):
            _tool_update_project_config(runner, {"updates": [
                {"key": "plugin.file_msgtool_script.jis_substitution", "value": True}]})
        self.assertEqual(runner.writes, [])

    def test_permissions_retained(self):
        self.assertEqual(_tool_risk("get_plugin_settings"), "read")
        for mode in ("ask", "accept-edits"):
            self.assertTrue(_permission_needed(_tool_risk("update_project_config"), mode))
        self.assertFalse(_permission_needed(_tool_risk("update_project_config"), "auto"))

    def test_wait_and_runtime_deliver_error_and_same_actionable_recovery(self):
        runner = AgentRunner(AgentState())
        runner._project_id = lambda: "test"
        error = "脚本回填编码失败：01_YU02.cst\nFailed to encode Shift-JIS"
        job = {"job_id": "j1", "status": "failed", "error": error}
        runner._http_get = lambda path: {"jobs": [job]} if path == "/api/jobs" else {"job": job}
        waited = _tool_wait(runner, {"job_id": "j1", "seconds": 0.1})
        runtime = _tool_get_runtime(runner, {})
        self.assertEqual(runtime["job_error"], error)
        self.assertEqual(waited["recovery"], runtime["recovery"])
        recovery = waited["recovery"]
        self.assertEqual(recovery["inspect"]["tool"], "get_plugin_settings")
        self.assertEqual(recovery["alternatives"][0]["arguments"]["updates"][0]["key"],
                         "plugin.file_msgtool_script.jis_substitution")
        self.assertEqual(recovery["rebuild"]["arguments"]["translator"], "rebuildr")
        self.assertIsNone(encoding_recovery("Failed to decode Shift-JIS"))


if __name__ == "__main__":
    unittest.main()
