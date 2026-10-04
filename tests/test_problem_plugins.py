import asyncio
import json
import sys
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from GalTransl import CACHE_FOLDERNAME
from GalTransl.ConfigHelper import CProjectConfig
from GalTransl.CSentense import CSentense
from GalTransl.Problem import find_problems, load_problem_plugins, finalize_problem_plugins, list_problem_types
from GalTransl.server import JobRegistry, _scan_plugins, build_handler
from GalTransl.server_runtime import encode_project_dir


PLUGIN_SOURCE = '''from GalTransl.GTPlugin import GProblemPlugin

class CheckPlugin(GProblemPlugin):
    def gtp_init(self, plugin_conf, project_conf):
        self.settings = plugin_conf.get("Settings", {})
        self.project_conf = project_conf
        self.init_count = 1
        self.final_count = 0

    def check(self, tran, project_config, gpt_dict=None):
        return [self.settings["message"], "shared"] if "!" in tran.post_dst else []

    def gtp_final(self):
        self.final_count += 1
'''


class ProblemPluginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)

    def plugin(self, module, message=None, source=PLUGIN_SOURCE):
        root = self.project / "plugins" / module
        root.mkdir(parents=True)
        (root / f"{module}.py").write_text(source, encoding="utf-8")
        (root / f"{module}.yaml").write_text(yaml.safe_dump({
            "Core": {"Name": module, "Module": module, "Type": "problem"},
            "Documentation": {"Author": "test", "Version": "1.0"},
            "Settings": {"message": message or module},
        }), encoding="utf-8")
        return f"(project_dir){module}"

    def config(self, plugin_config=None, **extra):
        data = {"common": {"language": "zh-cn", "marker": "project"},
                "dictionary": {"preDict": [], "postDict": [], "gpt.dict": []},
                "problemAnalyze": {"problemList": ["残留日文"]}}
        if plugin_config is not None:
            data["plugin"] = plugin_config
        data.update(extra)
        (self.project / "config.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        return CProjectConfig(str(self.project), "config.yaml")

    def tran(self, destination="你好!", index=1):
        tran = CSentense("こんにちは", index=index)
        tran.pre_dst = tran.post_dst = destination
        return tran

    def test_legacy_default_and_explicit_empty_list(self):
        config = self.config()
        self.assertEqual(config.getProblemPluginList(), ["problem_common"])
        tran = self.tran("你好あ")
        find_problems([tran], config)
        self.assertEqual(tran.problem, "残留日文：あ")
        disabled = self.config({"problemPlugins": []})
        tran = self.tran("你好あ")
        find_problems([tran], disabled)
        self.assertEqual(tran.problem, "")
        self.assertEqual(disabled.pPlugins, [])

    def test_order_overrides_deduplication_and_lifecycle(self):
        first = self.plugin("problem_first")
        second = self.plugin("problem_second")
        config = self.config({"problemPlugins": [second, first, second],
                              "problem_first": {"message": "overridden"}})
        tran = self.tran()
        tran.problem = "existing"
        dictionary = object()
        find_problems([tran], config, dictionary)
        self.assertEqual(tran.problem, "existing, problem_second, shared, overridden")
        loaded = config.pPlugins
        find_problems([tran], config)
        self.assertIs(config.pPlugins, loaded)
        self.assertEqual(tran.problem, "existing, problem_second, shared, overridden")
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[1].plugin_object.settings["message"], "overridden")
        self.assertEqual(loaded[0].plugin_object.project_conf["project_dir"], str(self.project))
        self.assertNotIn("project_dir", config.getCommonConfigSection())
        finalize_problem_plugins(config)
        self.assertIsNone(config.pPlugins)
        self.assertEqual([p.plugin_object.final_count for p in loaded], [1, 1])
        finalize_problem_plugins(config)
        self.assertEqual([p.plugin_object.final_count for p in loaded], [1, 1])

    def test_skip_and_empty_translation_do_not_call_plugins(self):
        checker = SimpleNamespace(name="checker", plugin_object=SimpleNamespace(
            check=lambda *args: self.fail("skipped sentence reached plugin")))
        config = SimpleNamespace(pPlugins=[checker])
        skipped = self.tran()
        skipped.skip_check = True
        skipped.problem = "old"
        empty = self.tran("")
        find_problems([skipped, empty], config)
        self.assertEqual(skipped.problem, "")
        self.assertEqual(empty.problem, "")

    def test_plugin_errors_and_invalid_return_do_not_stop_other_checks(self):
        broken = self.plugin("problem_broken", source=PLUGIN_SOURCE.replace(
            'return [self.settings["message"], "shared"] if "!" in tran.post_dst else []',
            'raise RuntimeError("broken checker")'))
        invalid = self.plugin("problem_invalid", source=PLUGIN_SOURCE.replace(
            'return [self.settings["message"], "shared"] if "!" in tran.post_dst else []',
            'return "invalid result"'))
        healthy = self.plugin("problem_healthy")
        config = self.config({"problemPlugins": [broken, invalid, healthy]})
        with self.assertLogs("GalTransl", level="ERROR"):
            trans = [self.tran(index=1), self.tran(index=2)]
            find_problems(trans, config)
        self.assertEqual([t.problem for t in trans], ["problem_healthy, shared"] * 2)

    def test_initialization_failure_missing_plugin_and_final_failure(self):
        broken = self.plugin("problem_init_broken", source=PLUGIN_SOURCE.replace(
            'self.init_count = 1', 'raise RuntimeError("init failed")'))
        healthy = self.plugin("problem_healthy", source=PLUGIN_SOURCE.replace(
            'self.final_count += 1', 'raise RuntimeError("final failed")'))
        config = self.config({"problemPlugins": ["problem_missing", broken, healthy]})
        with self.assertLogs("GalTransl", level="WARNING"):
            plugins = load_problem_plugins(config)
        self.assertEqual(len(plugins), 1)
        with self.assertLogs("GalTransl", level="ERROR"):
            finalize_problem_plugins(config)
        self.assertIsNone(config.pPlugins)

    def test_catalog_exposes_type_and_local_origin(self):
        self.plugin("problem_custom")
        plugins = {p["name"]: p for p in _scan_plugins(str(self.project))}
        self.assertEqual(plugins["problem_common"]["type"], "problem")
        self.assertFalse(plugins["problem_common"]["project_local"])
        self.assertTrue(plugins["problem_custom"]["project_local"])

    def typed_plugin(self, name, types, source=PLUGIN_SOURCE):
        source = source.replace('    def gtp_init',
            f'    def get_problem_types(self):\n        return {types!r}\n\n    def gtp_init')
        return self.plugin(name, source=source)

    def test_common_types_keep_legacy_defaults_and_enum_aliases(self):
        from plugins.problem_common.problem_common import CProblemType

        catalog = list_problem_types()
        self.assertEqual({item["name"] for item in catalog}, {item.name for item in CProblemType})
        self.assertEqual({item["name"] for item in catalog if item["default_enabled"]}, {
            "词频过高", "标点错漏", "残留日文", "多加换行", "比日文长",
            "字典使用", "语言不通", "缺控制符", "独白男他",
        })
        self.assertIs(CProblemType.本无括号, CProblemType.标点错漏)
        self.assertEqual(CProblemType.标点错漏.value, 2)
        self.assertTrue(all(item["description"] for item in catalog))

    def test_discovery_includes_disabled_and_local_plugins_without_initializing(self):
        custom = self.typed_plugin("problem_custom_types", [
            {"name": "custom default", "description": "Custom check", "default_enabled": True},
            {"name": "custom optional", "default_enabled": False},
            {"name": "残留日文", "default_enabled": False},
        ])
        self.config({"problemPlugins": []})
        catalog = {item["name"]: item for item in list_problem_types(str(self.project))}
        self.assertEqual(catalog["custom default"]["plugins"], [custom])
        self.assertTrue(catalog["custom default"]["default_enabled"])
        self.assertFalse(catalog["custom optional"]["default_enabled"])
        self.assertTrue(catalog["残留日文"]["default_enabled"])
        self.assertEqual(set(catalog["残留日文"]["plugins"]), {"problem_common", custom})

    def test_plugin_defaults_explicit_lists_and_legacy_config(self):
        self.typed_plugin("problem_custom_defaults", [{"name": "custom default", "default_enabled": True}])
        for analyze in ({}, {"problemList": None}):
            config = self.config(problemAnalyze=analyze)
            enabled = config.getProblemAnalyzeConfig("problemList")
            self.assertIn("custom default", enabled)
            self.assertIn("残留日文", enabled)
            self.assertNotIn("单句过长", enabled)
        for analyze, expected in [
            ({"problemList": []}, []),
            ({"problemList": ["custom default"]}, ["custom default"]),
            ({"GPT35": ["残留日文"]}, ["残留日文"]),
            ({"problemList": [], "GPT35": ["残留日文"]}, []),
            ({"GPT35": []}, []),
        ]:
            config = self.config(problemAnalyze=analyze)
            self.assertEqual(config.getProblemAnalyzeConfig("problemList"), expected)

    def test_empty_yaml_sections_keep_defaults_and_initialize_common_plugin(self):
        for plugin in (None, {"problem_common": None}):
            with self.subTest(plugin=plugin):
                config = self.config(plugin=plugin, problemAnalyze=None)
                self.assertEqual(config.getProblemPluginList(), ["problem_common"])
                self.assertEqual(config.getTextPluginList(), [])
                self.assertEqual(config.getFilePlugin(), "file_galtransl_json")
                self.assertIn("残留日文", config.getProblemAnalyzeConfig("problemList"))
                self.assertEqual(config.getProblemAnalyzeArinashiDict(), {})
                plugins = load_problem_plugins(config)
                self.assertEqual(len(plugins), 1)
                self.assertEqual(plugins[0].plugin_object.sentence_length_threshold, 17)

    def test_problem_names_accept_multiline_strings_and_reject_malformed_values(self):
        for key in ("problemList", "GPT35"):
            config = self.config(problemAnalyze={key: " 残留日文 \n\n 单句过长\r\n"})
            self.assertEqual(config.getProblemAnalyzeConfig("problemList"), ["残留日文", "单句过长"])
            self.assertEqual(self.config(problemAnalyze={key: ""}).getProblemAnalyzeConfig("problemList"), [])
            for value in (17, True, {}, ["残留日文", 1]):
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, "problemAnalyze.problemList"):
                    self.config(problemAnalyze={key: value}).getProblemAnalyzeConfig("problemList")

    def test_catalog_caches_concurrent_requests_without_reimporting_modules(self):
        self.typed_plugin("problem_cached", [{"name": "cached check", "default_enabled": True}])
        from GalTransl.Problem import _discover_problem_types

        with patch("GalTransl.Problem._discover_problem_types", wraps=_discover_problem_types) as discover:
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: list_problem_types(str(self.project)), range(8)))
            self.assertEqual(discover.call_count, 1)
            before = {name for name in sys.modules if name.startswith("yapsy_loaded_plugin_")}
            results[0][0]["plugins"].append("mutated")
            results[0][0]["default_enabled"] = None
            fresh = list_problem_types(str(self.project))
            self.assertNotIn("mutated", fresh[0]["plugins"])
            self.assertIsInstance(fresh[0]["default_enabled"], bool)
            self.assertEqual(before, {name for name in sys.modules if name.startswith("yapsy_loaded_plugin_")})

    def test_catalog_refreshes_when_plugins_are_added_changed_or_removed(self):
        self.assertNotIn("new check", {item["name"] for item in list_problem_types(str(self.project))})
        self.typed_plugin("problem_changes", [{"name": "new check"}])
        self.assertIn("new check", {item["name"] for item in list_problem_types(str(self.project))})
        source = self.project / "plugins/problem_changes/problem_changes.py"
        source.write_text(source.read_text(encoding="utf-8").replace("new check", "changed check"), encoding="utf-8")
        self.assertIn("changed check", {item["name"] for item in list_problem_types(str(self.project))})
        info = source.with_suffix(".yaml")
        info.unlink()
        self.assertNotIn("changed check", {item["name"] for item in list_problem_types(str(self.project))})

    def test_base_metadata_supports_strings_dictionaries_and_enums(self):
        from GalTransl.GTPlugin import GProblemPlugin
        from plugins.problem_common.problem_common import CProblemType

        plugin = GProblemPlugin()
        plugin.problem_types = ("custom", {"name": "enabled", "default_enabled": True})
        types = plugin.get_problem_types()
        self.assertEqual(types[0], {"name": "custom", "description": "", "default_enabled": False})
        self.assertTrue(types[1]["default_enabled"])
        types[1]["name"] = "mutated"
        self.assertEqual(plugin.problem_types[1]["name"], "enabled")
        plugin.problem_types = CProblemType
        self.assertEqual({item["name"] for item in plugin.get_problem_types()}, {item.name for item in CProblemType})

    def test_common_plugin_accepts_custom_names_and_obeys_default_selection(self):
        for analyze, detected in [({}, True), ({"problemList": []}, False),
                                  ({"problemList": ["custom", "残留日文"]}, True)]:
            config = self.config(problemAnalyze=analyze)
            tran = self.tran("你好あ")
            find_problems([tran], config)
            self.assertEqual("残留日文" in tran.problem, detected)

    def test_custom_plugin_uses_its_declared_defaults_and_explicit_selection(self):
        source = PLUGIN_SOURCE.replace(
            'return [self.settings["message"], "shared"] if "!" in tran.post_dst else []',
            'return [name for name in ("custom default", "custom optional") '
            'if name in project_config.getProblemAnalyzeConfig("problemList")]')
        custom = self.typed_plugin("problem_selectable", [
            {"name": "custom default", "default_enabled": True},
            {"name": "custom optional", "default_enabled": False},
        ], source=source)
        for analyze, expected in [({}, "custom default"), ({"problemList": []}, ""),
                                  ({"problemList": ["custom optional"]}, "custom optional")]:
            config = self.config({"problemPlugins": [custom]}, problemAnalyze=analyze)
            tran = self.tran()
            find_problems([tran], config)
            self.assertEqual(tran.problem, expected)

    def test_sentence_threshold_settings_defaults_legacy_and_override_precedence(self):
        for overrides, legacy, expected in [({}, None, 17), ({}, 5, 5),
            ({"avgSentenceLengthThreshold": 30}, 5, 30),
            ({"avgSentenceLengthThreshold": 0}, 5, 17)]:
            analyze = {"problemList": ["单句过长"]}
            if legacy is not None:
                analyze["avgSentenceLengthThreshold"] = legacy
            config = self.config({"problem_common": overrides}, problemAnalyze=analyze)
            tran = CSentense("文。\n文。")
            tran.pre_dst = tran.post_dst = "这是用于测试插件阈值的一句译文。"
            find_problems([tran], config)
            self.assertEqual(config.pPlugins[0].plugin_object.sentence_length_threshold, expected)
            self.assertEqual("单句过长" in tran.problem, len(tran.post_dst) > expected)

    def test_threshold_is_project_scoped(self):
        configs = [self.config({"problem_common": {"avgSentenceLengthThreshold": value}})
                   for value in (5, 30)]
        loaded = [load_problem_plugins(config) for config in configs]
        self.assertEqual([plugins[0].plugin_object.sentence_length_threshold for plugins in loaded], [5, 30])

    def test_threshold_setting_is_discoverable_with_schema(self):
        plugin = next(p for p in _scan_plugins() if p["name"] == "problem_common")
        self.assertEqual(plugin["settings"]["avgSentenceLengthThreshold"], 17)
        schema = plugin["settings_schema"]["avgSentenceLengthThreshold"]
        self.assertEqual(schema["min"], 1)
        self.assertEqual(schema["step"], 1)
        self.assertEqual(schema["legacy_path"], "problemAnalyze.avgSentenceLengthThreshold")

    def test_problem_type_routes_discover_project_types_and_default_flags(self):
        custom = self.typed_plugin("problem_http_types", [{"name": "http custom", "default_enabled": True}])
        server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(JobRegistry()))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}/api"
            for path, includes_custom in [("/problem-types", False),
                (f"/projects/{encode_project_dir(str(self.project))}/problem-types", True)]:
                with urllib.request.urlopen(base + path, timeout=10) as response:
                    catalog = {item["name"]: item for item in json.load(response)["problem_types"]}
                self.assertEqual("http custom" in catalog, includes_custom)
                self.assertTrue(catalog["残留日文"]["default_enabled"])
                if includes_custom:
                    self.assertEqual(catalog["http custom"]["plugins"], [custom])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_bad_type_metadata_does_not_hide_other_plugins(self):
        self.typed_plugin("problem_bad_metadata", [{"name": "bad", "default_enabled": "yes"}])
        with self.assertLogs("GalTransl", level="ERROR"):
            catalog = list_problem_types(str(self.project))
        self.assertNotIn("bad", [item["name"] for item in catalog])
        self.assertIn("残留日文", [item["name"] for item in catalog])

    def test_skip_no_japanese_plugin_still_handles_kana_and_excluded_symbols(self):
        from plugins.text_common_skipNoJP.text_common_skipNoJP import skip_noJP

        plugin = skip_noJP()
        for text, skipped in [("hello", True), ("ー・", True), ("あ", False),
                              ("ア", False), ("ｱ", False), ("日本語", False)]:
            tran = CSentense(text)
            plugin.after_src_processed(tran)
            self.assertEqual(bool(tran.pre_dst), skipped)

    def test_cache_edit_rechecks_custom_plugin_and_clears_fixed_problem(self):
        custom = self.plugin("problem_custom")
        self.config({"problemPlugins": [custom]})
        cache = self.project / CACHE_FOLDERNAME
        cache.mkdir()
        entries = [{"index": 1, "name": "", "pre_src": "こんにちは", "post_src": "こんにちは",
                    "pre_dst": "你好!", "proofread_dst": "", "proofread_by": ""}]
        (cache / "scene.json").write_text(json.dumps(entries), encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(JobRegistry()))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = (f"http://127.0.0.1:{server.server_port}/api/projects/"
                   f"{encode_project_dir(str(self.project))}/cache/save")
            for destination, expected in [("你好!", "problem_custom, shared"), ("你好", "")]:
                entries[0]["pre_dst"] = destination
                request = urllib.request.Request(url, data=json.dumps({
                    "filename": "scene.json", "entries": entries, "config_file_name": "config.yaml"
                }).encode(), headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(request, timeout=10) as response:
                    result = json.load(response)
                self.assertTrue(result["success"])
                saved = json.loads((cache / "scene.json").read_text(encoding="utf-8"))
                self.assertEqual(saved[0].get("problem", ""), expected)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_runner_finalizes_plugins_when_translation_fails(self):
        from GalTransl.Runner import run_galtransl

        custom = self.plugin("problem_custom")
        config = self.config({"problemPlugins": [custom], "filePlugin": "file_galtransl_json", "textPlugins": []})
        config.non_interactive = True
        plugins = load_problem_plugins(config)

        async def fail_translation(_config):
            raise RuntimeError("translation failed")

        with patch("GalTransl.Runner.doLLMTranslate", fail_translation):
            with self.assertRaisesRegex(RuntimeError, "translation failed"):
                asyncio.run(run_galtransl(config, "rebuildr"))
        self.assertEqual(plugins[0].plugin_object.final_count, 1)
        self.assertIsNone(config.pPlugins)


if __name__ == "__main__":
    unittest.main()
