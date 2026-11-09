"""POST /api/projects/:id/config/preview：配置页「YAML源码」视图用它把（未保存的）配置渲染成 YAML。

要钉住的是：预览文本与 PUT /config 实际写盘的内容逐字一致，且不会写文件；
另外这条路由要排在 /config 之前，否则 POST 会落到读配置的分支。
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

try:
    from GalTransl.server import JobRegistry, build_handler
    from GalTransl.server_runtime import encode_project_dir
except ModuleNotFoundError:  # 精简环境（如系统 python）没有 yaml/orjson，跳过
    raise unittest.SkipTest("server 依赖不可用")


ORIGINAL = "common:\n  language: ja\n"


class ConfigPreviewHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="galtransl-config-preview-")
        self.project = os.path.join(self.root, "proj")
        os.makedirs(self.project)
        self.config_path = os.path.join(self.project, "config.yaml")
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write(ORIGINAL)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(JobRegistry()))
        self.base = (
            f"http://127.0.0.1:{self.httpd.server_address[1]}/api/projects/"
            f"{encode_project_dir(self.project)}"
        )
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def _request(self, method, path, payload=None):
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=None if payload is None else json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_preview_matches_saved_file_and_does_not_write(self):
        config = {
            "backendSpecific": {"OpenAI-Compatible": {"tokens": [{"token": "sk-1", "contextWindow": 200000}], "stream": True}},
            "common": {"gpt.translation_guideline": "规范.md", "language": "ja2zh-cn"},
        }
        status, body = self._request("POST", "/config/preview", {"config": config})
        self.assertEqual(status, 200)
        preview = body["yaml"]
        self.assertNotIn("{", preview)  # 块格式而非 JSON
        self.assertIn("backendSpecific:\n  OpenAI-Compatible:\n", preview)
        self.assertIn("规范.md", preview)  # allow_unicode
        with open(self.config_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), ORIGINAL)

        status, _ = self._request("PUT", "/config", {"config": config, "config_file_name": "config.yaml"})
        self.assertEqual(status, 200)
        with open(self.config_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), preview)

    def test_preview_rejects_non_object_and_get(self):
        status, _ = self._request("POST", "/config/preview", {"config": [1, 2]})
        self.assertEqual(status, 400)
        status, _ = self._request("GET", "/config/preview")
        self.assertEqual(status, 405)


if __name__ == "__main__":
    unittest.main()
