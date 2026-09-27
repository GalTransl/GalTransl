"""发布包打包的回归测试：动态导入的模块必须进 hidden-import 清单。

报错的样子：release 里全新编译的 exe 一启动就
ModuleNotFoundError: No module named 'GalTransl.Agent.core'。
原因是 GalTransl/Agent/runtime.py 按模块名动态 import 一批子模块再把它们的名字重导出的
（见它顶部的 _MODULES），PyInstaller 的静态分析看不见这些字符串常量，只会顺着
server.py -> Agent/__init__.py -> Agent/runtime.py 的静态引用把别的东西打进去，
于是 core 这些模块在源码里好好的、在包里却不存在。所以构建脚本改成扫目录自动补。
"""

import ast
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import build_release


ROOT = Path(__file__).resolve().parent.parent


def runtime_dynamic_modules() -> set[str]:
    """从 runtime.py 的 _MODULES 里读出动态导入的模块名（用 ast 解析，不真的去 import）。"""
    source = (ROOT / "GalTransl" / "Agent" / "runtime.py").read_text(encoding="utf-8")
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_MODULES" for target in node.targets
        ):
            return {ast.literal_eval(element) for element in node.value.elts}
    raise AssertionError("GalTransl/Agent/runtime.py 里找不到 _MODULES")


class AgentHiddenImportsTests(unittest.TestCase):
    def test_runtime_dynamically_imported_modules_are_all_covered(self):
        missing = runtime_dynamic_modules() - set(build_release.scan_agent_hidden_imports())
        self.assertEqual(missing, set(), f"这些动态导入的模块没进 hidden-import: {sorted(missing)}")

    def test_scan_covers_package_root_and_nested_subpackages(self):
        names = build_release.scan_agent_hidden_imports()
        self.assertIn("GalTransl.Agent", names)
        self.assertIn("GalTransl.Agent.core", names)
        self.assertIn("GalTransl.Agent.tools.common", names)
        self.assertNotIn("GalTransl.Agent.runtime.__init__", names)


class PackageScanTests(unittest.TestCase):
    def test_scan_maps_init_to_package_and_skips_hidden_files(self):
        with TemporaryDirectory() as tmp:
            package_dir = Path(tmp) / "pkg"
            (package_dir / "sub").mkdir(parents=True)
            (package_dir / "__init__.py").write_text("", encoding="utf-8")
            (package_dir / "a.py").write_text("", encoding="utf-8")
            (package_dir / "sub" / "__init__.py").write_text("", encoding="utf-8")
            (package_dir / "sub" / "b.py").write_text("", encoding="utf-8")
            (package_dir / ".hidden.py").write_text("", encoding="utf-8")
            names = build_release.scan_package_hidden_imports("pkg", package_dir)
        self.assertEqual(names, ["pkg", "pkg.a", "pkg.sub", "pkg.sub.b"])

    def test_scan_of_missing_dir_is_empty(self):
        self.assertEqual(
            build_release.scan_package_hidden_imports("nope", Path("no") / "such" / "dir"), []
        )


if __name__ == "__main__":
    unittest.main()
