"""插件设置页的批量重新提取：强制刷新、使用保存的配置、保留译文。"""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import yaml

from GalTransl import CACHE_FOLDERNAME, INPUT_FOLDERNAME, OUTPUT_FOLDERNAME
from GalTransl.server import JobRegistry, build_handler
from GalTransl.server_runtime import encode_project_dir
from plugins.file_msgtool_script.file_msgtool_script import file_plugin


class InputReextractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(JobRegistry()))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.input = self.root / INPUT_FOLDERNAME
        (self.input / "sub").mkdir(parents=True)
        (self.input / "a.scn").write_bytes(b"source A")
        (self.input / "sub/b.cst").write_bytes(b"source B")
        (self.input / "other.json").write_text('[]', encoding="utf-8")
        tool = self.root / "msg_tool.exe"
        tool.write_bytes(b"test tool")
        self.config = {"common": {"language": "zh-cn"}, "plugin": {"filePlugin": "auto",
                       "file_msgtool_script": {"msg_tool_path": str(tool), "source_encoding": "cp932"}}}
        (self.root / "extract.yaml").write_text(yaml.safe_dump(self.config), encoding="utf-8")
        (self.root / "config.yaml").write_text("common: {}\nplugin:\n  filePlugin: file_galtransl_json\n", encoding="utf-8")
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/api/projects/{encode_project_dir(str(self.root))}"
        self.initialized = []

        def init_plugin(project_dir, cfg, name):
            self.assertEqual(name, "file_msgtool_script")
            plugin = file_plugin()
            plugin.gtp_init({"Core": {}, "Settings": cfg.getPluginConfigSection()[name]},
                            {"project_dir": project_dir})
            self.initialized.append(plugin)
            return plugin

        patcher = patch("GalTransl.server._init_file_plugin", side_effect=init_plugin)
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, path, method="POST", payload=None):
        request = urllib.request.Request(self.base + path, method=method,
                                        data=json.dumps(payload or {}).encode() if method != "GET" else None,
                                        headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)

    def reextract(self):
        return self.request("/plugins/file_msgtool_script/reextract", payload={"config_file_name": "extract.yaml"})

    def test_batch_uses_saved_settings_forces_refresh_and_preserves_translations(self):
        protected = []
        for folder in (CACHE_FOLDERNAME, OUTPUT_FOLDERNAME):
            (self.root / folder).mkdir()
            path = self.root / folder / "a.scn.json"
            path.write_bytes(b'[{"translation":"keep"}]')
            protected.append((path, path.read_bytes()))
        # 修改当前选用的配置（不是默认 config.yaml），再调用批量提取。
        self.config["plugin"]["file_msgtool_script"].update(source_encoding="utf8", script_type="cat-system")
        self.request("/config", method="PUT", payload={"config": self.config, "config_file_name": "extract.yaml"})
        with patch.object(file_plugin, "_load_uncached", return_value=[{"index": 1, "message": "old", "org_message": "old"}]) as export:
            first = self.reextract()
            export.return_value = [{"index": 1, "message": "new", "org_message": "new"}]
            second = self.reextract()
            self.assertEqual(export.call_count, 4)  # 相同指纹也必须重提取两个文件。
        self.assertEqual(first, second)
        self.assertEqual([f["filename"] for f in second["refreshed"]], ["a.scn", "sub/b.cst"])
        self.assertEqual(second["total_entries"], 2)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(second["errors"], [])
        self.assertTrue(all(p.source_encoding == "utf8" and p.script_type == "cat-system" for p in self.initialized))
        with patch.object(file_plugin, "_load_uncached", side_effect=AssertionError("应复用新缓存")):
            self.assertEqual(self.initialized[-1].load_file(str(self.input / "a.scn"))[0]["message"], "new")
        for path, before in protected:
            self.assertEqual(path.read_bytes(), before)

    def test_one_failed_file_keeps_old_cache_and_other_files_continue(self):
        with patch.object(file_plugin, "_load_uncached", return_value=[{"index": 1, "message": "old", "org_message": "old"}]):
            self.reextract()
        with patch.object(file_plugin, "_load_uncached", side_effect=[RuntimeError("wrong engine"), []]):
            result = self.reextract()
        self.assertEqual(result["refreshed"], [{"filename": "sub/b.cst", "entries": 0}])
        self.assertEqual(result["errors"], [{"filename": "a.scn", "error": "wrong engine"}])
        with patch.object(file_plugin, "_load_uncached", side_effect=AssertionError("应保留缓存")):
            self.assertEqual(self.initialized[-1].load_file(str(self.input / "a.scn"))[0]["message"], "old")

    def test_rejects_get_and_unrelated_plugin(self):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("/plugins/file_msgtool_script/reextract", method="GET")
        self.assertEqual(raised.exception.code, 405)
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("/plugins/file_msgtool_script/reextract", payload={"config_file_name": "config.yaml"})
        self.assertEqual(raised.exception.code, 400)
        self.assertIn("file_msgtool_script", json.load(raised.exception)["error"])


if __name__ == "__main__":
    unittest.main()
