"""项目概览统计通过真实 HTTP 接口与人名表工具核对。"""

from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from urllib.request import urlopen

import yaml

from GalTransl.Agent.tools.names import _tool_get_name_table
from GalTransl.Agent.tools.project import _tool_get_project_overview
from GalTransl.Agent.tools.render_md import _render_tool_result_table
from GalTransl.server import JobRegistry, build_handler
from GalTransl.server_runtime import encode_project_dir


class ProjectStatisticsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="galtransl-project-stats-")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        self.common_dicts = self.project / "common-dictionaries"
        self.common_dicts.mkdir()
        self.config = {
            "dictionary": {
                "defaultDictFolder": str(self.common_dicts),
                "preDict": ["pre.txt"],
                "gpt.dict": ["(project_dir)a.txt", "(project_dir)b.txt"],
                "postDict": ["(project_dir)missing.txt"],
                "useGPTDictInName": True,
            },
        }
        self.save_config()
        (self.common_dicts / "pre.txt").write_text(
            "# 格式说明\t不计\n// 注释\t不计\n\n猫\t喵\n犬\t狗\n", encoding="utf-8"
        )
        (self.project / "a.txt").write_text("アリス\t爱丽丝\nボブ\t字典译名\n", encoding="utf-8")
        (self.project / "b.txt").write_text("ドルード\t多鲁德\n", encoding="utf-8")
        self.names_path = self.project / "name替换表.csv"
        self.names_path.write_text(
            "SRC_Name,DST_Name,Count\nアリス,,200\nボブ,已有译名,10\nドルード,,30\n未收录,  ,5\n",
            encoding="utf-8",
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(JobRegistry()))
        thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.httpd.shutdown)
        self.requests = []
        self.runner = SimpleNamespace(
            state=SimpleNamespace(config_file_name="config.yaml"),
            _project_id=lambda: encode_project_dir(str(self.project)),
            _http_get=self.get,
        )

    def save_config(self, name="config.yaml"):
        (self.project / name).write_text(yaml.safe_dump(self.config, allow_unicode=True), encoding="utf-8")

    def get(self, path):
        self.requests.append(path)
        with urlopen(f"http://127.0.0.1:{self.httpd.server_port}{path}", timeout=10) as response:
            return json.load(response)

    def test_enabled_name_summary_matches_effective_table_without_writing(self):
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.project.rglob("*") if path.is_file()}
        result = _tool_get_project_overview(self.runner, {"include": ["names"]})
        summary = result["names"]
        table = _tool_get_name_table(self.runner, {"limit": 1, "only_missing": True})
        self.assertEqual((summary["count"], summary["missing_translations"]), (4, 1))
        self.assertTrue(summary["use_gpt_dict_in_name"])
        self.assertEqual(summary["count"], table["total"])
        self.assertEqual(summary["missing_translations"], table["matched"])
        self.assertEqual(table["names"][0]["src_name"], "未收录")
        all_names = _tool_get_name_table(self.runner, {})["names"]
        # GPT 字典优先于表里已有的译名（与 Name.py 翻译时的口径一致）
        self.assertEqual([row["dst_name"] for row in all_names], ["爱丽丝", "字典译名", "多鲁德", "  "])
        self.assertEqual(all_names[1]["table_dst_name"], "已有译名")
        self.assertNotIn("still_empty", summary)
        self.assertNotIn("names", summary)
        rendered = _render_tool_result_table("get_project", result)
        self.assertIn("missing_translations", rendered)
        self.assertIn("GPT 字典补齐", rendered)
        after = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.project.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_missing_flag_counts_as_enabled(self):
        """useGPTDictInName 默认开启：配置里没写也按开启统计。"""
        self.config["dictionary"].pop("useGPTDictInName", None)
        self.save_config()
        summary = _tool_get_project_overview(self.runner, {"include": ["names"]})["names"]
        self.assertTrue(summary["use_gpt_dict_in_name"])
        self.assertEqual(summary["missing_translations"], 1)

    def test_disabled_flag_preserves_raw_missing_count_and_pagination_parity(self):
        self.config["dictionary"]["useGPTDictInName"] = False
        self.save_config()
        self.requests.clear()
        summary = _tool_get_project_overview(self.runner, {"include": ["names"]})["names"]
        table = _tool_get_name_table(self.runner, {"limit": 1, "offset": 1, "only_missing": True})
        self.assertEqual(summary["missing_translations"], 3)
        self.assertEqual(summary["missing_translations"], table["matched"])
        self.assertEqual(table["returned"], 1)
        self.assertNotIn("use_gpt_dict_in_name", summary)
        self.assertFalse(any("/dictionary" in path for path in self.requests))

    def test_selected_config_controls_overlay_and_dictionary_summary(self):
        self.config["dictionary"]["gpt.dict"] = ["(project_dir)b.txt"]
        self.save_config("其他配置.yaml")
        self.runner.state.config_file_name = "其他配置.yaml"
        result = _tool_get_project_overview(self.runner, {"include": ["dictionaries", "names"]})
        self.assertEqual(result["names"]["missing_translations"], 2)
        gpt = [entry for entry in result["dictionaries"]["files"] if entry["category"] == "gpt"]
        self.assertEqual(gpt, [{"category": "gpt", "file_key": "(project_dir)b.txt", "count": 1}])

    def test_dictionary_summary_counts_configured_common_and_project_entries_without_contents(self):
        result = _tool_get_project_overview(self.runner, {"include": ["dictionaries"]})
        summary = result["dictionaries"]
        self.assertEqual(summary["files"][:3], [
            {"category": "pre", "file_key": "pre.txt", "count": 2},
            {"category": "gpt", "file_key": "(project_dir)a.txt", "count": 2},
            {"category": "gpt", "file_key": "(project_dir)b.txt", "count": 1},
        ])
        self.assertIsNone(summary["files"][3]["count"])
        self.assertEqual(summary["files"][3]["error"], "file not found")
        self.assertNotIn("dict_contents", summary)
        self.assertNotIn("爱丽丝", json.dumps(summary, ensure_ascii=False))
        self.assertEqual(len(self.requests), 1)
        self.assertIn("当前配置字典", _render_tool_result_table("get_project", result))

    def test_no_name_table_returns_zero_without_config_or_dictionary_reads(self):
        empty_project = self.project / "no-name-table"
        empty_project.mkdir()
        self.runner._project_id = lambda: encode_project_dir(str(empty_project))
        summary = _tool_get_project_overview(self.runner, {"include": ["names"]})["names"]
        self.assertEqual((summary["source_file"], summary["count"], summary["missing_translations"]), (None, 0, 0))
        self.assertEqual(len(self.requests), 1)

    def test_empty_name_table_returns_zero_without_overlay_reads(self):
        self.names_path.write_text("SRC_Name,DST_Name,Count\n", encoding="utf-8")
        summary = _tool_get_project_overview(self.runner, {"include": ["names"]})["names"]
        self.assertEqual((summary["count"], summary["missing_translations"]), (0, 0))
        self.assertEqual(summary["source_file"], "name替换表.csv")
        self.assertEqual(len(self.requests), 1)


if __name__ == "__main__":
    unittest.main()
