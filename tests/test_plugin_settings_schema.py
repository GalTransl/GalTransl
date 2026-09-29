import unittest
from io import StringIO
from pathlib import Path

import yaml

from GalTransl.PluginSettings import normalize_settings_schema
from GalTransl.server import _scan_plugins
from GalTransl.yapsy.PluginFileLocator import PluginFileAnalyzerWithInfoFile


class PluginSettingsSchemaTests(unittest.TestCase):
    def test_yaml_loader_preserves_literal_percent_in_metadata_and_settings(self):
        info = {
            "Core": {"Name": "测试插件", "Module": "test_plugin"},
            "Documentation": {"Description": "原文字号为 80%"},
            "Settings": {"template": "%(name)s: 100%", "ratio": 0.8},
            "SettingsSchema": {"ratio": {"label": "字号比例", "description": "0.8 表示 80%"}},
        }
        analyzer = PluginFileAnalyzerWithInfoFile("test")
        name, module, parser = analyzer.getPluginNameAndModuleFromStream(
            StringIO(yaml.safe_dump(info, allow_unicode=True))
        )
        self.assertEqual((name, module), ("测试插件", "test_plugin"))
        self.assertEqual(parser.get("Settings", "template"), "%(name)s: 100%")
        self.assertEqual(parser.get("Documentation", "Description"), "原文字号为 80%")
        self.assertIn("80%", parser.get("SettingsSchema", "ratio"))
        self.assertEqual(analyzer.yaml_dict, info)

    def test_legacy_and_invalid_metadata_fall_back(self):
        self.assertEqual(normalize_settings_schema({"old": True}, None), {})
        self.assertEqual(normalize_settings_schema({}, {"unknown": {"label": "未知"}}), {})
        self.assertEqual(normalize_settings_schema({"old": True}, {"old": "bad"}), {})

    def test_options_keep_scalar_types_and_reject_invalid_metadata(self):
        schema = normalize_settings_schema({"mode": 1, "enabled": False}, {
            "mode": {"label": "排列方式", "advanced": True, "min": 1, "options": [
                {"value": 1, "label": "上下"}, {"value": 2, "label": "左右"},
                {"value": "3", "label": "错误类型"}, {"value": True, "label": "错误布尔"},
            ]},
            "enabled": {"advanced": "false", "options": [{"value": False, "label": "关闭"}]},
        })
        self.assertEqual([item["value"] for item in schema["mode"]["options"]], [1, 2])
        self.assertIs(schema["mode"]["advanced"], True)
        self.assertNotIn("advanced", schema["enabled"])
        self.assertIs(schema["enabled"]["options"][0]["value"], False)

    def test_all_bundled_settings_have_labels_and_explanations(self):
        root = Path(__file__).resolve().parents[1] / "plugins"
        for path in root.glob("*/*.yaml"):
            info = yaml.safe_load(path.read_text(encoding="utf-8"))
            settings = info.get("Settings") or {}
            schema = normalize_settings_schema(settings, info.get("SettingsSchema"))
            with self.subTest(plugin=path.parent.name):
                self.assertEqual(set(settings), set(schema))
                for key in settings:
                    self.assertTrue(schema[key].get("label"))
                    self.assertTrue(schema[key].get("description"))
                    if schema[key].get("options"):
                        values = settings[key] if isinstance(settings[key], list) else [settings[key]]
                        for value in values:
                            self.assertIn(value, [o["value"] for o in schema[key]["options"]])

    def test_text_plugin_multiselect_and_secret_metadata(self):
        plugins = {p["name"]: p for p in _scan_plugins()}
        notify = plugins["text_message_serverchan_tgbot"]
        schema = notify["settings_schema"]
        self.assertEqual([item["value"] for item in schema["推送渠道"]["options"]], ["ServerChan", "Telegram Bot"])
        self.assertTrue(schema["Telegram_Bot_Token"]["secret"])
        self.assertTrue(schema["OpenAI_TTS_Voice"]["advanced"])
        self.assertEqual(plugins["text_common_full2Half"]["settings"]["自定义替换表"], {})
        from GalTransl import DEBUG_LEVEL
        levels = plugins["text_bgi_fixruby"]["settings_schema"]["process_log_level"]["options"]
        self.assertTrue(all(option["value"] in DEBUG_LEVEL for option in levels))

    def test_plugin_api_exposes_schema_without_changing_settings(self):
        plugins = {p["name"]: p for p in _scan_plugins()}
        msgtool = plugins["file_msgtool_script"]
        self.assertIn("直接读取和写入 galgame 脚本文件", msgtool["description"])
        self.assertEqual(msgtool["settings"]["script_type"], "")
        self.assertIn("bgi", [o["value"] for o in msgtool["settings_schema"]["script_type"]["options"]])
        self.assertTrue(msgtool["settings_schema"]["msg_tool_path"]["advanced"])
        self.assertFalse(msgtool["settings_schema"]["script_type"]["advanced"])
        self.assertEqual(plugins["file_i18n_json"]["settings_schema"], {})


if __name__ == "__main__":
    unittest.main()
