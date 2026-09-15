"""项目翻译规范的路由：GET /api/projects/:id/guideline 与 PUT（覆写/增写/替换）。

前端「项目规范」编辑页和 Agent 的 write_project_guideline 工具都打这个路由，
这里起一个真的 ThreadingHTTPServer 走 HTTP，把读写与参数校验锁住。
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from GalTransl.ProjectGuideline import PROJECT_GUIDELINE_FILENAME, read_project_guideline


class ProjectGuidelineApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            from GalTransl.server import JobRegistry, build_handler
            from GalTransl.server_runtime import encode_project_dir
        except ModuleNotFoundError:  # 精简环境（如系统 python）没有 yaml，跳过
            raise unittest.SkipTest("server 依赖不可用")

        cls.encode_project_dir = staticmethod(encode_project_dir)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(JobRegistry()))
        cls.httpd_base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()

    def setUp(self) -> None:
        # 每个用例一个干净项目目录：规范是有状态的，共用目录会互相污染
        self.project = os.path.join(tempfile.mkdtemp(prefix="galtransl-guideline-"), "proj")
        os.makedirs(self.project, exist_ok=True)
        self.base = f"{self.httpd_base}/api/projects/{self.encode_project_dir(self.project)}"

    # ---- HTTP helpers ----

    def _get(self) -> dict:
        with urllib.request.urlopen(f"{self.base}/guideline", timeout=30) as resp:
            return json.load(resp)

    def _put(self, body: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base}/guideline",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)

    def _put_expect_400(self, body: dict) -> str:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._put(body)
        self.assertEqual(ctx.exception.code, 400)
        return json.loads(ctx.exception.read().decode("utf-8"))["error"]

    # ---- 用例 ----

    def test_get_reports_missing_file(self) -> None:
        out = self._get()
        self.assertEqual(out["filename"], PROJECT_GUIDELINE_FILENAME)
        self.assertFalse(out["exists"])
        self.assertEqual(out["content"], "")

    def test_overwrite_then_append_then_replace(self) -> None:
        out = self._put({"mode": "overwrite", "content": "## 称呼\n- お兄ちゃん→哥哥"})
        self.assertTrue(out["success"])
        self.assertTrue(out["created"])
        # 文件确实落在项目目录里（不是别处）
        self.assertEqual(read_project_guideline(self.project), "## 称呼\n- お兄ちゃん→哥哥")

        out = self._put({"mode": "append", "content": "## 语气\n- 书面语"})
        self.assertFalse(out["created"])
        text = self._get()["content"]
        self.assertIn("お兄ちゃん→哥哥", text)  # 增写不动原有内容
        self.assertIn("书面语", text)

        self._put({"mode": "replace", "old_text": "お兄ちゃん→哥哥", "new_text": "お兄ちゃん→兄长"})
        text = self._get()["content"]
        self.assertIn("兄长", text)
        self.assertNotIn("哥哥", text)
        self.assertIn("书面语", text)  # 只动了那一段

    def test_default_mode_is_overwrite(self) -> None:
        """不带 mode 时按覆写处理（前端编辑页就只传 content）。"""
        out = self._put({"content": "只有这段"})
        self.assertEqual(out["mode"], "overwrite")
        self.assertEqual(self._get()["content"], "只有这段")

    def test_replace_miss_is_readable_400(self) -> None:
        self._put({"mode": "overwrite", "content": "abc"})
        error = self._put_expect_400({"mode": "replace", "old_text": "不存在", "new_text": "x"})
        self.assertIn("没有找到", error)

    def test_replace_ambiguous_is_400(self) -> None:
        self._put({"mode": "overwrite", "content": "哥哥 … 哥哥"})
        error = self._put_expect_400({"mode": "replace", "old_text": "哥哥", "new_text": "兄长"})
        self.assertIn("2 次", error)

    def test_unknown_mode_is_400(self) -> None:
        error = self._put_expect_400({"mode": "prepend", "content": "x"})
        self.assertIn("mode", error)


if __name__ == "__main__":
    unittest.main()
