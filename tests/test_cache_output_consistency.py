"""The optional project check replays real cache loading and output processing."""

import asyncio
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from urllib.request import Request, urlopen

import yaml

from GalTransl import CACHE_FOLDERNAME, INPUT_FOLDERNAME, OUTPUT_FOLDERNAME
from GalTransl.Agent.tools.project import _tool_get_project_overview
from GalTransl.Agent.tools.render_md import _render_tool_result_table
from GalTransl.Cache import save_transCache_to_json
from GalTransl.CSentense import CSentense
from GalTransl.server import JobRegistry, build_handler
from GalTransl.server_runtime import encode_project_dir
from http.server import ThreadingHTTPServer


class CacheOutputConsistencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="galtransl-consistency-test-")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        for folder in (CACHE_FOLDERNAME, INPUT_FOLDERNAME, OUTPUT_FOLDERNAME):
            (self.project / folder).mkdir()
        self.config = {
            "common": {"language": "zh-cn", "splitFile": "no"},
            "plugin": {"filePlugin": "file_galtransl_json", "textPlugins": []},
            "dictionary": {"preDict": [], "postDict": ["(project_dir)post.txt"], "gpt.dict": [], "sortDict": False},
        }
        self.save_config()
        (self.project / "post.txt").write_text("错误\t正确\n", encoding="utf-8")
        (self.project / "name替换表.csv").write_text("SRC_Name,DST_Name,Count\n猫,喵,1\n", encoding="utf-8")
        self.write(INPUT_FOLDERNAME, "a.json", [{"name": "猫", "message": "「原文」"}])
        self.cache = [{"index": 1, "name": "猫", "pre_src": "「原文」", "post_src": "原文",
                       "pre_dst": "初稿错误", "proofread_dst": "校对错误", "post_dst_preview": "过期预览"}]
        self.write(CACHE_FOLDERNAME, "a.json", self.cache)
        self.write(OUTPUT_FOLDERNAME, "a.json", [{"name": "喵", "message": "「校对正确」"}])
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(JobRegistry()))
        thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        self.requests = []

    def save_config(self, name="config.yaml"):
        (self.project / name).write_text(yaml.safe_dump(self.config, allow_unicode=True), encoding="utf-8")

    def write(self, folder, name, content):
        path = self.project / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")

    def check(self, config_name="config.yaml"):
        def get(path):
            self.requests.append(path)
            with urlopen(Request(self.base + path), timeout=10) as response:
                return json.load(response)
        runner = SimpleNamespace(state=SimpleNamespace(config_file_name=config_name),
                                 _project_id=lambda: encode_project_dir(str(self.project)), _http_get=get)
        result = _tool_get_project_overview(runner, {"include": ["cache_output"]})
        self.assertIn("cache_output", _render_tool_result_table("get_project", result))
        return result["cache_output"]

    def test_consistent_uses_proofread_dictionary_dialogue_and_names_without_writing_project(self):
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.project.rglob("*") if path.is_file()}
        result = self.check()
        self.assertEqual((result["status"], result["consistent"], result["checked_files"]), ("consistent", True, 1))
        self.assertEqual(result["files"], [])
        self.assertFalse(result["rebuild_required"])
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns)
                                  for path in self.project.rglob("*") if path.is_file()})
        self.assertEqual(len(self.requests), 1)
        self.assertIn("/cache-output?config=config.yaml", self.requests[0])

    def test_cache_edit_and_changed_dictionary_report_rebuild(self):
        self.cache[0]["proofread_dst"] = "改后错误"
        self.write(CACHE_FOLDERNAME, "a.json", self.cache)
        result = self.check()
        self.assertEqual(result["status"], "inconsistent")
        self.assertFalse(result["consistent"])
        self.assertTrue(result["rebuild_required"])
        self.assertIn("rebuilda", result["note"])
        self.assertEqual(result["mismatched_files"], 1)
        self.assertEqual(result["files"][0]["file"], "a.json")
        self.write(OUTPUT_FOLDERNAME, "a.json", [{"name": "喵", "message": "「改后正确」"}])
        self.assertTrue(self.check()["consistent"])
        (self.project / "post.txt").write_text("错误\t最新\n", encoding="utf-8")
        self.assertEqual(self.check()["status"], "inconsistent")

    def test_empty_proofread_falls_back_to_initial_translation_and_honors_selected_config(self):
        self.cache[0]["proofread_dst"] = ""
        self.write(CACHE_FOLDERNAME, "a.json", self.cache)
        self.write(OUTPUT_FOLDERNAME, "a.json", [{"name": "喵", "message": "「初稿正确」"}])
        self.assertTrue(self.check()["consistent"])
        self.config["dictionary"]["postDict"] = []
        self.save_config("custom.yaml")
        self.assertEqual(self.check("custom.yaml")["status"], "inconsistent")

    def test_missing_output_missing_cache_and_untranslated_have_distinct_results(self):
        output = self.project / OUTPUT_FOLDERNAME / "a.json"
        output.unlink()
        result = self.check()
        self.assertEqual(result["status"], "inconsistent")
        self.assertIn("缺失", result["files"][0]["reason"])
        (self.project / CACHE_FOLDERNAME / "a.json").unlink()
        result = self.check()
        self.assertEqual((result["status"], result["untranslated_files"]), ("not_checked", 1))
        self.assertIsNone(result["consistent"])
        self.write(OUTPUT_FOLDERNAME, "a.json", [{"message": "旧输出"}])
        result = self.check()
        self.assertEqual((result["status"], result["uncheckable_files"]), ("partial", 1))
        self.assertIsNone(result["consistent"])

    def test_changed_source_and_corrupt_cache_cannot_be_reported_consistent(self):
        self.cache[0]["proofread_dst"] = ""
        self.cache[0]["post_src"] = "过期原文"
        self.write(CACHE_FOLDERNAME, "a.json", self.cache)
        self.assertEqual(self.check()["status"], "partial")
        (self.project / CACHE_FOLDERNAME / "a.json").write_text("broken JSON", encoding="utf-8")
        self.assertEqual(self.check()["status"], "partial")

    def test_text_plugin_processing_is_included(self):
        self.config["plugin"]["textPlugins"] = ["text_common_full2Half"]
        self.config["plugin"]["text_common_full2Half"] = {"替换时机": "after_dst_processed"}
        self.save_config()
        self.cache[0]["proofread_dst"] = "校对Ａ"
        self.write(CACHE_FOLDERNAME, "a.json", self.cache)
        self.write(OUTPUT_FOLDERNAME, "a.json", [{"name": "喵", "message": "「校对A」"}])
        self.assertTrue(self.check()["consistent"])

    def test_name_dictionary_and_composite_names_follow_the_translation_pipeline(self):
        self.config["dictionary"].update({"gpt.dict": ["(project_dir)gpt.txt"], "useGPTDictInName": True})
        (self.project / "gpt.txt").write_text("猫\t小猫\n", encoding="utf-8")
        self.save_config()
        self.write(OUTPUT_FOLDERNAME, "a.json", [{"name": "小猫", "message": "「校对正确」"}])
        self.assertTrue(self.check()["consistent"])
        self.config["dictionary"]["useGPTDictInName"] = False
        self.save_config()
        self.write(INPUT_FOLDERNAME, "a.json", [{"names": ["猫", "犬"], "message": "「原文」"}])
        self.cache[0]["name"] = ["猫", "犬"]
        self.write(CACHE_FOLDERNAME, "a.json", self.cache)
        self.write(OUTPUT_FOLDERNAME, "a.json", [{"names": ["喵", "犬"], "message": "「校对正确」"}])
        self.assertTrue(self.check()["consistent"])

    def test_details_are_bounded_and_orphan_outputs_are_not_silently_ignored(self):
        for number in range(22):
            self.write(OUTPUT_FOLDERNAME, f"orphan-{number}.json", [{"message": "旧输出"}])
        result = self.check()
        self.assertEqual((result["status"], result["uncheckable_files"]), ("partial", 22))
        self.assertIsNone(result["consistent"])
        self.assertEqual((len(result["files"]), result["omitted_files"]), (20, 2))

    def test_nested_split_caches_are_combined_and_incomplete_chunks_reported(self):
        for folder in (INPUT_FOLDERNAME, CACHE_FOLDERNAME, OUTPUT_FOLDERNAME):
            (self.project / folder / "a.json").unlink()
        self.config["common"].update({"splitFile": "Equal", "splitFileNum": 2})
        self.save_config()
        self.write(INPUT_FOLDERNAME, "sub/b.json", [{"message": "第一句"}, {"message": "第二句"}])
        for part, source, target in ((0, "第一句", "第一译文"), (1, "第二句", "第二译文")):
            self.write(CACHE_FOLDERNAME, f"sub-}}b.json_{part}.json", [
                {"index": 1, "pre_src": source, "post_src": source, "pre_dst": target},
            ])
        self.write(OUTPUT_FOLDERNAME, "sub/b.json", [{"message": "第一译文"}, {"message": "第二译文"}])
        self.assertTrue(self.check()["consistent"])
        (self.project / CACHE_FOLDERNAME / "sub-}b.json_1.json").unlink()
        self.assertEqual(self.check()["status"], "partial")

    def test_latest_append_cache_is_used(self):
        tran = CSentense("「原文」", "猫", 1)
        tran.post_src = "原文"
        tran.pre_dst = "初稿错误"
        tran.proofread_zh = "增量错误"
        asyncio.run(save_transCache_to_json([tran], str(self.project / CACHE_FOLDERNAME / "a.json")))
        self.assertEqual(self.check()["status"], "inconsistent")
        self.write(OUTPUT_FOLDERNAME, "a.json", [{"name": "喵", "message": "「增量正确」"}])
        self.assertTrue(self.check()["consistent"])

    def test_plaintext_bilingual_output_compares_actual_serialization(self):
        for folder in (INPUT_FOLDERNAME, CACHE_FOLDERNAME, OUTPUT_FOLDERNAME):
            (self.project / folder / "a.json").unlink()
        self.config["plugin"] = {"filePlugin": "file_plaintext_txt", "textPlugins": [],
                                  "file_plaintext_txt": {"保存双语txt文件": True, "上下双语1左右双语2": 1}}
        self.save_config()
        (self.project / INPUT_FOLDERNAME / "a.txt").write_text("原文", encoding="utf-8")
        self.write(CACHE_FOLDERNAME, "a.txt.json", [{"index": 1, "pre_src": "原文", "post_src": "原文", "pre_dst": "译文"}])
        (self.project / OUTPUT_FOLDERNAME / "a.txt").write_text("译文\n原文", encoding="utf-8")
        self.assertTrue(self.check()["consistent"])
