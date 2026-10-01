"""通用翻译规范（translation_guidelines/）的接口：清单 / 读 / 新建 / 保存 / 删除。

设置页的「通用翻译规范管理」和 Agent 的 read_guideline(scope="global") 都打这几个路由，
这里起一个真的 ThreadingHTTPServer 走 HTTP，把读写、重名、路径穿越与"兜底文件不许删"锁住。

规范目录是**相对程序根目录**的（server._guidelines_dir 用 abspath），所以每个用例都在
一个临时目录里跑：chdir 过去，结束再回来。
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer


class CommonGuidelineApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            from GalTransl import DEFAULT_GUIDELINE_NAME
            from GalTransl.server import JobRegistry, build_handler
        except ModuleNotFoundError:  # 精简环境（如系统 python）没有 yaml，跳过
            raise unittest.SkipTest("server 依赖不可用")

        cls.default_name = DEFAULT_GUIDELINE_NAME
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(JobRegistry()))
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}/api/translation-guidelines"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()

    def setUp(self) -> None:
        # 规范目录按 CWD 解析：每个用例在自己的临时目录里跑，互不污染
        self._cwd = os.getcwd()
        self.root = tempfile.mkdtemp(prefix="galtransl-guidelines-")
        os.chdir(self.root)
        self.guidelines_dir = os.path.join(self.root, "translation_guidelines")
        self.addCleanup(self._restore_cwd)

    def _restore_cwd(self) -> None:
        os.chdir(self._cwd)

    def _write_file(self, name: str, content: str) -> str:
        os.makedirs(self.guidelines_dir, exist_ok=True)
        path = os.path.join(self.guidelines_dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    # ---- HTTP helpers ----

    def _get(self, suffix: str = "") -> dict:
        with urllib.request.urlopen(f"{self.base}{suffix}", timeout=30) as resp:
            return json.load(resp)

    def _post(self, action: str, body: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base}/{action}",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)

    def _post_expect_error(self, action: str, body: dict, code: int) -> str:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(action, body)
        self.assertEqual(ctx.exception.code, code)
        return json.loads(ctx.exception.read().decode("utf-8"))["error"]

    def _get_expect_error(self, suffix: str, code: int) -> str:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get(suffix)
        self.assertEqual(ctx.exception.code, code)
        return json.loads(ctx.exception.read().decode("utf-8"))["error"]

    # ---- 清单 ----

    def test_list_reports_files_and_meta(self) -> None:
        self._write_file("Basic.md", "## 基础\n")
        self._write_file("MyStyle.md", "## 我的风格\n- 口语一点")
        # 目录里的杂项不该出现在清单/选项里
        self._write_file("notes.log", "无关文件")
        self._write_file(".hidden.md", "隐藏")

        out = self._get()

        self.assertEqual(out["guidelines"], ["Basic.md", "MyStyle.md"])
        self.assertEqual([item["name"] for item in out["files"]], ["Basic.md", "MyStyle.md"])
        self.assertEqual(out["dir"], os.path.abspath("translation_guidelines"))
        self.assertEqual(out["default"], self.default_name)
        by_name = {item["name"]: item for item in out["files"]}
        # size 是**磁盘上的字节数**（不是字符数：中文一个字符占 3 字节，且 Windows 上写盘会把
        # \n 落成 \r\n）。直接跟 stat 对照，别在测试里重算一遍编码。
        self.assertEqual(
            by_name["MyStyle.md"]["size"],
            os.path.getsize(os.path.join(self.guidelines_dir, "MyStyle.md")),
        )
        self.assertGreater(by_name["MyStyle.md"]["mtime"], 0)
        self.assertTrue(by_name["Basic.md"]["builtin"])
        self.assertFalse(by_name["MyStyle.md"]["builtin"])

    def test_list_on_missing_dir_is_empty(self) -> None:
        out = self._get()
        self.assertEqual(out["guidelines"], [])
        self.assertEqual(out["files"], [])

    # ---- 新建 / 读 / 保存 ----

    def test_create_appends_md_suffix_and_writes_content(self) -> None:
        out = self._post("create", {"filename": "MyStyle", "content": "## 称呼\n"})

        self.assertTrue(out["success"])
        self.assertEqual(out["filename"], "MyStyle.md")
        path = os.path.join(self.guidelines_dir, "MyStyle.md")
        self.assertTrue(os.path.isfile(path))
        with open(path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "## 称呼\n")
        # 新建完就能读到（管理页新建后立刻选中它）
        self.assertEqual(self._get("/MyStyle.md")["content"], "## 称呼\n")

    def test_create_keeps_an_existing_suffix(self) -> None:
        out = self._post("create", {"filename": "Plain.txt"})
        self.assertEqual(out["filename"], "Plain.txt")
        self.assertTrue(os.path.isfile(os.path.join(self.guidelines_dir, "Plain.txt")))

    def test_create_refuses_to_overwrite(self) -> None:
        self._write_file("Basic.md", "原文")
        message = self._post_expect_error("create", {"filename": "Basic.md", "content": "改掉"}, 409)
        self.assertIn("已存在", message)
        with open(os.path.join(self.guidelines_dir, "Basic.md"), "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "原文")  # 一个字都没动

    def test_create_rejects_unsafe_names(self) -> None:
        for name in ("../escape.md", "sub/dir.md", ".hidden.md", "", "   "):
            with self.subTest(name=name):
                self._post_expect_error("create", {"filename": name}, 400)
        # 没有写到目录外面去
        self.assertFalse(os.path.exists(os.path.join(self.root, "escape.md")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "sub")))

    def test_save_overwrites_existing_file(self) -> None:
        path = self._write_file("MyStyle.md", "旧内容")

        out = self._post("save", {"filename": "MyStyle.md", "content": "新内容\n第二行"})

        self.assertTrue(out["success"])
        self.assertEqual(out["length"], len("新内容\n第二行"))
        with open(path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "新内容\n第二行")

    def test_save_does_not_create_a_missing_file(self) -> None:
        """文件名打错不该顺手新建一份空规范——报错，让调用方看清写错了。"""
        message = self._post_expect_error("save", {"filename": "Typo.md", "content": "x"}, 404)
        self.assertIn("Typo.md", message)
        self.assertFalse(os.path.exists(os.path.join(self.guidelines_dir, "Typo.md")))

    def test_read_rejects_odd_names(self) -> None:
        self._write_file("Basic.md", "x")
        self._get_expect_error("/.hidden.md", 400)
        self._get_expect_error("/notes.log", 400)
        self._get_expect_error("/Missing.md", 404)

    # ---- 删除 ----

    def test_delete_removes_file(self) -> None:
        path = self._write_file("MyStyle.md", "内容")

        out = self._post("delete", {"filename": "MyStyle.md"})

        self.assertTrue(out["success"])
        self.assertFalse(os.path.exists(path))
        self.assertEqual(self._get()["guidelines"], [])

    def test_delete_refuses_the_default_guideline(self) -> None:
        """兜底那份删了，未配置规范的项目会直接报「读不到规范」。"""
        path = self._write_file(self.default_name, "兜底内容")

        message = self._post_expect_error("delete", {"filename": self.default_name}, 400)

        self.assertIn("兜底", message)
        self.assertTrue(os.path.exists(path))

    def test_delete_missing_is_404(self) -> None:
        self._post_expect_error("delete", {"filename": "Missing.md"}, 404)

    def test_delete_rejects_unsafe_names(self) -> None:
        self._write_file("Basic.md", "x")
        self._post_expect_error("delete", {"filename": "../Basic.md"}, 400)
        self.assertTrue(os.path.isfile(os.path.join(self.guidelines_dir, "Basic.md")))


if __name__ == "__main__":
    unittest.main()
