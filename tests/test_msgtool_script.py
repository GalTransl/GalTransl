import os
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from plugins.file_msgtool_script.file_msgtool_script import file_plugin


TOOL = Path(__file__).resolve().parents[1] / "res" / "msg_tool.exe"
SCRIPT = "*start\nこんにちは。[p]\n"


class MsgToolScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def plugin(self, **settings):
        plugin = file_plugin()
        plugin.gtp_init(
            {"Core": {}, "Settings": settings}, {"project_dir": str(self.root)}
        )
        return plugin

    def test_directory_selection_matches_project_config(self):
        for input_name in ("gt_input", "json_jp"):
            for output_name in ("gt_output", "json_cn"):
                with self.subTest(input=input_name, output=output_name):
                    with tempfile.TemporaryDirectory(dir=self.root) as directory:
                        root = Path(directory)
                        src = root / input_name / "nested" / "gt_input" / "test.ks"
                        src.parent.mkdir(parents=True)
                        src.write_bytes(b"source")
                        (root / output_name).mkdir()
                        plugin = file_plugin()
                        plugin.gtp_init({"Core": {}}, {"project_dir": directory})
                        dst = root / output_name / "nested" / "gt_input" / "test.ks"
                        self.assertEqual(plugin._output_path_for(str(src)), str(dst))
                        self.assertEqual(plugin._source_path_for(str(dst)), str(src))
                        self.assertIsNone(plugin._source_path_for(str(root / "outside.ks")))

    def test_modern_directories_take_precedence(self):
        for folder in ("gt_input", "gt_output", "json_jp", "json_cn"):
            (self.root / folder).mkdir()
        for folder in ("gt_input", "json_jp"):
            (self.root / folder / "test.ks").write_bytes(folder.encode())
        plugin = self.plugin()
        self.assertEqual(
            plugin._source_path_for(str(self.root / "gt_output" / "test.ks")),
            str(self.root / "gt_input" / "test.ks"),
        )

    def test_explicit_options_override_ks_defaults(self):
        plugin = self.plugin(extra_args=["--script-type=kirikiri", "-ecp932"])
        self.assertEqual(plugin._common_args("test.KS"), plugin.extra_args)
        plugin = self.plugin(script_type="kirikiri-simple-crypt", source_encoding="cp932")
        self.assertEqual(plugin._common_args("test.ks"), ["-t", "kirikiri-simple-crypt", "-e", "cp932"])

    def test_encoding_warning_is_fatal_on_either_stream(self):
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream):
                result = subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")
                setattr(result, stream, b"Warning: Some characters could not be encoded in Shift-JIS\n")
                with patch("os.path.isfile", return_value=True), patch("subprocess.run", return_value=result):
                    with self.assertRaisesRegex(RuntimeError, "patched_encoding"):
                        self.plugin()._run(["import"])

    @unittest.skipUnless(os.name == "nt" and TOOL.is_file(), "requires bundled Windows msg-tool")
    def test_real_ks_encoding_roundtrips(self):
        (self.root / "gt_input").mkdir()
        for encoding in ("cp932", "utf-8", "utf-16"):
            with self.subTest(encoding=encoding):
                src = self.root / "gt_input" / "test.ks"
                src.write_text(SCRIPT, encoding=encoding)
                original = src.read_bytes()
                plugin = self.plugin(patched_encoding="utf8")
                rows = plugin.load_file(str(src))
                self.assertIn("こんにちは。", rows[0]["message"])
                rows[0]["message"] = "你好，世界。"
                dst = self.root / "gt_output" / "test.ks"
                plugin.save_file(str(dst), rows)
                self.assertEqual(plugin.load_file(str(dst))[0]["message"], "你好，世界。")
                self.assertEqual(src.read_bytes(), original)

    @unittest.skipUnless(os.name == "nt" and TOOL.is_file(), "requires bundled Windows msg-tool")
    def test_real_mixed_legacy_directory_roundtrips(self):
        for input_name, output_name in (("json_jp", "gt_output"), ("gt_input", "json_cn")):
            with self.subTest(input=input_name, output=output_name):
                with tempfile.TemporaryDirectory(dir=self.root) as directory:
                    root = Path(directory)
                    (root / input_name).mkdir()
                    # gt_output 允许尚未创建，符合旧项目首次运行的情况。
                    if output_name == "json_cn":
                        (root / output_name).mkdir()
                    src = root / input_name / "test.ks"
                    src.write_text(SCRIPT, encoding="utf-16")
                    plugin = file_plugin()
                    plugin.gtp_init({"Core": {}}, {"project_dir": directory})
                    rows = plugin.load_file(str(src))
                    rows[0]["message"] = "你好，世界。"
                    dst = root / output_name / "test.ks"
                    plugin.save_file(str(dst), rows)
                    self.assertEqual(plugin.load_file(str(dst))[0]["message"], "你好，世界。")

    @unittest.skipUnless(os.name == "nt" and TOOL.is_file(), "requires bundled Windows msg-tool")
    def test_real_encoding_loss_preserves_existing_output(self):
        (self.root / "gt_input").mkdir()
        (self.root / "gt_output").mkdir()
        src = self.root / "gt_input" / "test.ks"
        src.write_text(SCRIPT, encoding="cp932")
        original = src.read_bytes()
        plugin = self.plugin()
        rows = plugin.load_file(str(src))
        rows[0]["message"] = "你好，世界。"
        dst = self.root / "gt_output" / "test.ks"
        dst.write_bytes(b"existing output")
        with self.assertRaisesRegex(RuntimeError, "patched_encoding"):
            plugin.save_file(str(dst), rows)
        self.assertEqual(dst.read_bytes(), b"existing output")
        self.assertEqual(src.read_bytes(), original)


class MsgToolCacheTests(unittest.TestCase):
    # 单独的缓存行为测试使用假的提取器，不依赖 Windows 工具。
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "gt_input").mkdir()
        self.src = self.root / "gt_input" / "sample.scn"
        self.src.write_bytes(b"source")
        self.tool = self.root / "tool.exe"
        self.tool.write_bytes(b"tool")
        self.rows = [{"message": "original", "org_message": "original", "index": 1, "name": "name"}]

    def plugin(self, **settings):
        plugin = file_plugin()
        plugin.gtp_init(
            {"Core": {}, "Settings": {"msg_tool_path": str(self.tool), **settings}},
            {"project_dir": str(self.root)},
        )
        return plugin

    def test_persistent_hit_returns_independent_rows_and_maps_source(self):
        with patch.object(file_plugin, "_load_uncached", side_effect=lambda _: copy.deepcopy(self.rows)) as export:
            first = self.plugin().load_file(str(self.src))
            first[0]["message"] = "translated"
            second = self.plugin()
            self.assertEqual(second.load_file(str(self.src)), self.rows)
            export.assert_called_once()
            self.assertEqual(second._source_path_for(str(self.root / "gt_output" / self.src.name)), str(self.src))

    def test_content_change_even_with_preserved_timestamp_invalidates(self):
        plugin = self.plugin()
        with patch.object(file_plugin, "_load_uncached", side_effect=lambda _: copy.deepcopy(self.rows)) as export:
            plugin.load_file(str(self.src))
            stat = self.src.stat()
            self.src.write_bytes(b"change")
            os.utime(self.src, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            plugin.load_file(str(self.src))
            self.assertEqual(export.call_count, 2)

    def test_config_tool_and_sibling_changes_invalidate(self):
        with patch.object(file_plugin, "_load_uncached", side_effect=lambda _: copy.deepcopy(self.rows)) as export:
            self.plugin().load_file(str(self.src))
            plugin = self.plugin(source_encoding="utf8")
            plugin.load_file(str(self.src))
            self.tool.write_bytes(b"new tool version")
            plugin.load_file(str(self.src))
            (self.src.parent / "metadata.bin").write_bytes(b"metadata")
            plugin.load_file(str(self.src))
            self.assertEqual(export.call_count, 4)

    def test_corrupt_cache_is_rebuilt(self):
        plugin = self.plugin()
        with patch.object(file_plugin, "_load_uncached", side_effect=lambda _: copy.deepcopy(self.rows)) as export:
            plugin.load_file(str(self.src))
            cached = next(Path(plugin.cache_dir).glob("*.json"))
            cached.write_text("{broken", encoding="utf-8")
            self.assertEqual(plugin.load_file(str(self.src)), self.rows)
            payload = json.loads(cached.read_text(encoding="utf-8"))
            payload["rows"] = [{"message": 7}]
            cached.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(plugin.load_file(str(self.src)), self.rows)
            self.assertEqual(export.call_count, 3)

    def test_disable_and_extra_args_bypass_cache(self):
        with patch.object(file_plugin, "_load_uncached", side_effect=lambda _: copy.deepcopy(self.rows)) as export:
            for settings in ({"read_cache": False}, {"extra_args": ["--kirikiri-export-chat"]}):
                plugin = self.plugin(**settings)
                plugin.load_file(str(self.src))
                plugin.load_file(str(self.src))
            self.assertEqual(export.call_count, 4)

    def test_concurrent_instances_export_same_file_once(self):
        with patch.object(file_plugin, "_load_uncached", side_effect=lambda _: copy.deepcopy(self.rows)) as export:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: self.plugin().load_file(str(self.src)), range(16)))
            self.assertTrue(all(rows == self.rows for rows in results))
            export.assert_called_once()

    def test_unwritable_cache_falls_back_to_export(self):
        plugin = self.plugin()
        blocker = self.root / "not_a_directory"
        blocker.write_bytes(b"blocker")
        plugin.cache_dir = str(blocker / "cache")
        with patch.object(file_plugin, "_load_uncached", return_value=self.rows):
            self.assertEqual(plugin.load_file(str(self.src)), self.rows)

    def test_failed_export_is_not_cached_but_empty_result_is(self):
        plugin = self.plugin()
        with patch.object(file_plugin, "_load_uncached", side_effect=[RuntimeError("failed"), []]) as export:
            with self.assertRaisesRegex(RuntimeError, "failed"):
                plugin.load_file(str(self.src))
            self.assertEqual(plugin.load_file(str(self.src)), [])
            self.assertEqual(self.plugin().load_file(str(self.src)), [])
            self.assertEqual(export.call_count, 2)

    def test_source_changed_during_export_is_not_cached(self):
        plugin = self.plugin()
        def changing_export(_):
            if self.src.read_bytes() == b"source":
                self.src.write_bytes(b"changed during export")
            return copy.deepcopy(self.rows)
        with patch.object(file_plugin, "_load_uncached", side_effect=changing_export) as export:
            plugin.load_file(str(self.src))
            plugin.load_file(str(self.src))
            plugin.load_file(str(self.src))
            self.assertEqual(export.call_count, 2)


if __name__ == "__main__":
    unittest.main()
