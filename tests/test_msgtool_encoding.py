"""回填编码预检与真实 CatSystem2 编码失败的回归测试。"""

import copy
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zlib

from plugins.file_msgtool_script.file_msgtool_script import file_plugin


TOOL = Path(__file__).resolve().parents[1] / "res/msg_tool.exe"


def cst_fixture(compressed=False):
    """按 CatScene 格式构造一个含人名和正文的脚本，不依赖游戏资源。"""
    name = b"\x01\x21" + "太郎".encode("cp932") + b"\0"
    message = b"\x01\x20" + "こんにちは。".encode("cp932") + b"\0"
    content = struct.pack("<4I", 8 + len(name + message), 0, 0, 8)
    content += struct.pack("<2I", 0, len(name)) + name + message
    data = zlib.compress(content) if compressed else content
    return b"CatScene" + struct.pack("<2I", len(data) if compressed else 0, len(content)) + data


class MsgToolEncodingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "gt_input").mkdir()
        (self.root / "gt_output").mkdir()
        self.src = self.root / "gt_input/01_YU02.cst"
        self.src.write_bytes(cst_fixture())
        self.dst = self.root / "gt_output/01_YU02.cst"
        self.dst.write_bytes(b"previous output")
        self.rows = [{"name": "你", "message": "这是中文。"}]
        self.trans = self.root / "trans.json"
        self.trans.write_text(json.dumps(self.rows, ensure_ascii=False), encoding="utf-8")

    def plugin(self, **settings):
        plugin = file_plugin()
        plugin.gtp_init({"Core": {}, "Settings": settings}, {"project_dir": str(self.root)})
        return plugin

    def test_default_cst_preflight_stops_before_tool_and_identifies_fields(self):
        plugin = self.plugin()
        original = self.src.read_bytes()
        rows = copy.deepcopy(self.rows)
        with patch.object(plugin, "_run") as run:
            with self.assertRaises(RuntimeError) as caught:
                plugin.save_file(str(self.dst), rows)
            run.assert_not_called()
        error = str(caught.exception)
        for detail in (str(self.src), "回填前", "CP932", "第 1 条人名", "第 1 条正文",
                       "U+4F60", "JIS 替换", "jis_substitution", "patched_encoding", "无需重新翻译"):
            self.assertIn(detail, error)
        self.assertEqual(rows, self.rows)
        self.assertEqual(self.src.read_bytes(), original)
        self.assertEqual(self.dst.read_bytes(), b"previous output")

    def test_source_encoding_does_not_change_default_patched_encoding(self):
        plugin = self.plugin(source_encoding="utf8", script_type="cat-system")
        with patch.object(plugin, "_run") as run:
            with self.assertRaisesRegex(RuntimeError, "CP932"):
                plugin.save_file(str(self.dst), self.rows)
            run.assert_not_called()

    def test_bilingual_original_and_separator_are_checked(self):
        plugin = self.plugin(keep_bilingual=True, bilingual_sep="🙂")
        with patch.object(plugin, "_run") as run:
            with self.assertRaisesRegex(RuntimeError, r"U\+1F642"):
                plugin.save_file(str(self.dst), [{"message": "日本", "org_message": "你"}])
            run.assert_not_called()

    def test_encoding_errors_on_both_streams_and_nonzero_warning_exit(self):
        args = ["import", str(self.src), str(self.trans), str(self.root / "patched.cst")]
        for returncode, problem in (
            (2, f"Error exporting {self.src}: Failed to encode Shift-JIS"),
            (2, "Some characters could not be encoded in code page 932: 你"),
            (0, "Warning: Some characters could not be encoded in Shift-JIS: 你"),
            (1, "Warning: Some characters could not be encoded in Shift-JIS: 你"),
        ):
            for stream in ("stdout", "stderr"):
                with self.subTest(returncode=returncode, problem=problem, stream=stream):
                    proc = subprocess.CompletedProcess([], returncode, stdout=b"Importing script", stderr=b"summary")
                    setattr(proc, stream, problem.encode("utf-8"))
                    with patch("subprocess.run", return_value=proc), self.assertRaises(RuntimeError) as caught:
                        self.plugin()._run(args)
                    error = str(caught.exception)
                    for detail in ("脚本回填编码失败", "CP932", "U+4F60", "第 1 条人名",
                                   "JIS 替换", "patched_encoding", "原始诊断", problem):
                        self.assertIn(detail, error)

    def test_decode_and_export_failures_are_not_mislabeled_as_import_encoding(self):
        for command, problem in (("import", "Failed to decode Shift-JIS"),
                                 ("export", "Failed to encode Shift-JIS"),
                                 ("import", "Invalid CST script magic")):
            with self.subTest(command=command, problem=problem):
                proc = subprocess.CompletedProcess([], 2, stdout=problem.encode(), stderr=b"summary")
                with patch("subprocess.run", return_value=proc), self.assertRaises(RuntimeError) as caught:
                    self.plugin()._run([command])
                self.assertIn(problem, str(caught.exception))
                self.assertIn("summary", str(caught.exception))
                self.assertNotIn("jis_substitution", str(caught.exception))

    def test_gbk_error_and_enabled_jis_do_not_recommend_enabling_jis(self):
        for settings, problem in (({}, "Failed to encode GB2312"),
                                  ({"jis_substitution": True}, "Failed to encode Shift-JIS")):
            with self.subTest(settings=settings):
                proc = subprocess.CompletedProcess([], 2, stdout=b"", stderr=problem.encode())
                with patch("subprocess.run", return_value=proc), self.assertRaises(RuntimeError) as caught:
                    self.plugin(**settings)._run(["import"])
                self.assertNotIn("开启「JIS 替换」", str(caught.exception))
                self.assertIn("patched_encoding", str(caught.exception))

    def test_output_overrides_prevent_false_cp932_preflight_and_duplicate_flags(self):
        for extra_args in (["-pgb2312"], ["--patched-encoding=gb2312"], ["-P", "936"]):
            with self.subTest(extra_args=extra_args):
                plugin = self.plugin(patched_encoding="cp932", extra_args=extra_args)

                def fake_import(args):
                    self.assertNotIn("cp932", args)
                    Path(args[-1]).write_bytes(b"new output")

                with patch.object(plugin, "_run", side_effect=fake_import) as run:
                    plugin.save_file(str(self.dst), self.rows)
                    run.assert_called_once()

    def test_gbk_extension_and_rust_compatibility_characters_are_not_rejected(self):
        for encoding, text in (("gb2312", "镕€"), ("cp932", "¥‾")):
            with self.subTest(encoding=encoding):
                plugin = self.plugin(patched_encoding=encoding)
                plugin._preflight_encoding(str(self.src), [{"message": text}], ["import", "-p", encoding])

    def test_unicode_kag_and_unknown_default_formats_are_deferred_to_tool(self):
        for header in (b"\xff\xfe", b"\xfe\xff", b"\xef\xbb\xbf", b"\xfe\xfe\x01", b"mdf\0"):
            self.src.write_bytes(header)
            self.plugin()._preflight_encoding(str(self.src), self.rows, ["import", "-t", "kirikiri"])
        self.src.write_bytes(b"unknown")
        self.plugin()._preflight_encoding(str(self.src), self.rows, ["import"])
        self.plugin()._preflight_encoding(str(self.src), self.rows, ["import", "-t", "cat-system-cstl"])

    def test_character_examples_are_bounded(self):
        samples = self.plugin()._encoding_samples([{"message": "你🙂" * 5000}] * 100, "cp932")
        self.assertEqual(len(samples), 5)
        self.assertLess(len("\n".join(samples)), 500)

    def test_tool_replacement_tables_are_allowed_to_resolve_encoding(self):
        for args in (["--replacement-json", "replacements.json"], ["--name-csv=names.csv"]):
            with self.subTest(args=args):
                self.plugin()._preflight_encoding(str(self.src), self.rows, ["import", *args])

    @unittest.skipUnless(os.name == "nt" and TOOL.is_file(), "requires bundled Windows msg-tool")
    def test_real_cst_strict_failure_is_explained_and_outputs_preserved(self):
        plugin = self.plugin()
        rows = plugin.load_file(str(self.src))
        self.assertEqual(rows[0]["name"], "太郎")
        rows[0]["message"] = "你好，这是中文。"
        original = self.src.read_bytes()
        # 绕过预检，验证实际工具的 exit=2 也能转成同样的可操作诊断。
        with patch.object(plugin, "_preflight_encoding"), self.assertRaises(RuntimeError) as caught:
            plugin.save_file(str(self.dst), rows)
        error = str(caught.exception)
        self.assertIn("Failed to encode Shift-JIS", error)
        self.assertIn("msg-tool exit=2", error)
        self.assertIn("JIS 替换", error)
        self.assertIn("U+4F60", error)
        self.assertEqual(self.src.read_bytes(), original)
        self.assertEqual(self.dst.read_bytes(), b"previous output")

    @unittest.skipUnless(os.name == "nt" and TOOL.is_file(), "requires bundled Windows msg-tool")
    def test_real_cst_both_suggested_fixes_roundtrip(self):
        for compressed in (False, True):
            for settings, read_encoding in (({"jis_substitution": True}, "cp932"),
                                            ({"patched_encoding": "gb2312"}, "gb2312"),
                                            ({"patched_encoding": "utf8"}, "utf8")):
                with self.subTest(compressed=compressed, settings=settings):
                    self.src.write_bytes(cst_fixture(compressed))
                    original = self.src.read_bytes()
                    plugin = self.plugin(**settings)
                    rows = plugin.load_file(str(self.src))
                    rows[0].update(name="你", message="你好，这是中文。")
                    plugin.save_file(str(self.dst), rows)
                    actual = self.plugin(source_encoding=read_encoding).load_file(str(self.dst))[0]
                    if settings.get("jis_substitution"):
                        config = json.loads((self.root / "gt_output/uif_config.json").read_text(encoding="utf-8"))
                        sub = config["character_substitution"]
                        table = str.maketrans(sub["source_characters"], sub["target_characters"])
                        actual = {key: value.translate(table) if isinstance(value, str) else value
                                  for key, value in actual.items()}
                    self.assertEqual(actual["name"], rows[0]["name"])
                    self.assertEqual(actual["message"], rows[0]["message"])
                    self.assertEqual(self.src.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
