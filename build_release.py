#!/usr/bin/env python3
"""
GalTransl 发布版构建脚本 (Python)

支持 Windows x64 和 Linux x86_64。Linux 正式构建建议在 Ubuntu 22.04
或兼容的较旧 glibc 环境中执行，以扩大发行包兼容范围。

用法:
  python build_release.py           # 构建全部
  python build_release.py --skip-fe # 跳过前端构建（WSL 下推荐）
  python build_release.py --skip-be # 跳过后端构建
  python build_release.py --clean   # 构建前清理旧产物
  python build_release.py --no-zip  # 不创建 zip 压缩包

产出目录:
  release/
    GalTransl_{version}_{platform}/
      GalTransl Desktop.exe 或 galtransl-desktop
      backend/galtransl_backend[.exe]
      plugins/
      res/
    GalTransl_{version}_{platform}.zip 或 .tar.gz
"""

import argparse
import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

# ─── 配置 ───────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent
DESKTOP_DIR = ROOT / "desktop"
TAURI_DIR = DESKTOP_DIR / "src-tauri"
RELEASE_DIR = ROOT / "release"
PLUGINS_DIR = ROOT / "plugins"
DICT_DIR = ROOT / "Dict"
GUIDELINES_DIR = ROOT / "translation_guidelines"
RES_DIR = ROOT / "res"
AGENT_DIR = ROOT / "GalTransl" / "Agent"


def get_version() -> str:
    """从 GalTransl/__init__.py 读取版本号"""
    init_py = ROOT / "GalTransl" / "__init__.py"
    for line in init_py.read_text(encoding="utf-8").splitlines():
        if line.startswith("GALTRANSL_VERSION"):
            return line.split('"')[1]
    return "0.0.0"


VERSION = get_version()


def get_platform_tag() -> str:
    if sys.platform == "win32":
        return "win"
    if sys.platform.startswith("linux"):
        machine = os.uname().machine.lower()
        if machine in {"x86_64", "amd64"}:
            return "linux_x86_64"
        return f"linux_{machine}"
    return sys.platform


PLATFORM_TAG = get_platform_tag()
BUILD_NAME = f"GalTransl_{VERSION}_{PLATFORM_TAG}"
BUILD_DIR = RELEASE_DIR / BUILD_NAME
ARCHIVE_NAME = f"{BUILD_NAME}{'.zip' if sys.platform == 'win32' else '.tar.gz'}"
# Backwards-compatible name for callers importing the old constant.
ZIP_NAME = ARCHIVE_NAME

BACKEND_ENTRY = ROOT / "run_backend.py"


# Windows 控制台默认是 GBK，脚本里最后那句 ✅ 会让 print 抛 UnicodeEncodeError——构建其实
# 已经成功，却以一段 traceback 收尾，看着像失败了。保留控制台原有的编码（中文才不乱码），
# 只把编不出的字符降级成 '?'，任何一行 print 都不再能中断构建。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

BACKEND_DIST_NAME = "galtransl_backend"
VENV_DIR = ROOT / ".venv-build"  # 构建用虚拟环境（不提交到 git）


# ─── 工具函数 ───────────────────────────────────────────

def run(cmd: str, cwd: Path | None = None, check: bool = True) -> int:
    """执行命令并实时输出，返回 exit code"""
    print(f"\033[36m> {cmd}\033[0m")
    result = subprocess.run(cmd, shell=True, cwd=cwd or ROOT)
    if check and result.returncode != 0:
        print(f"\033[31m命令执行失败 (exit code {result.returncode})\033[0m")
        sys.exit(1)
    return result.returncode


def copy_dir_filtered(src: Path, dst: Path):
    """复制目录，过滤 __pycache__ 和 .pyc"""
    shutil.copytree(
        str(src), str(dst),
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        dirs_exist_ok=True,
    )


def frontend_executable_name() -> str:
    return "GalTransl Desktop.exe" if sys.platform == "win32" else "galtransl-desktop"


def find_frontend_exe() -> Path | None:
    """查找前端可执行文件路径（兼容不同构建命名）"""
    candidates = [
        TAURI_DIR / "target" / "release" / frontend_executable_name(),
        TAURI_DIR / "target" / "release" / "GalTransl Desktop.exe",
        TAURI_DIR / "target" / "release" / "galtransl-desktop.exe",
        TAURI_DIR / "target" / "release" / "galtransl-desktop",
    ]
    for exe_path in candidates:
        if exe_path.exists():
            return exe_path
    return None


def backend_executable_name() -> str:
    ext = ".exe" if sys.platform == "win32" else ""
    return f"{BACKEND_DIST_NAME}{ext}"


def find_backend_executable(onefile: bool | None = None) -> Path | None:
    candidates = [
        ROOT / "dist" / BACKEND_DIST_NAME / backend_executable_name(),
        ROOT / "dist" / backend_executable_name(),
    ]
    if onefile is not None:
        candidates = [candidates[1] if onefile else candidates[0]]
    for exe_path in candidates:
        if exe_path.exists():
            return exe_path
    return None


def scan_plugin_hidden_imports() -> list[str]:
    """扫描 plugins/*/*.py 中的 import，提取第三方模块名用于 hidden-import。"""
    if not PLUGINS_DIR.exists():
        return []

    stdlib_names = set(getattr(sys, "stdlib_module_names", ()))
    stdlib_names.update({"__future__", "typing_extensions"})
    skipped_roots = {"GalTransl", "plugins"}
    discovered: set[str] = set()

    for plugin_dir in PLUGINS_DIR.iterdir():
        if not plugin_dir.is_dir():
            continue

        py_files = list(plugin_dir.glob("*.py"))
        for py_file in py_files:
            try:
                source = py_file.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(py_file))
            except Exception:
                continue

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.level and node.level > 0:
                        continue
                    if not node.module:
                        continue
                    names = [node.module]
                else:
                    continue

                for name in names:
                    root = name.split(".", 1)[0]
                    if not root or root in skipped_roots or root in stdlib_names:
                        continue

                    local_py = plugin_dir / f"{root}.py"
                    local_pkg = plugin_dir / root / "__init__.py"
                    if local_py.exists() or local_pkg.exists():
                        continue

                    discovered.add(root)

    return sorted(discovered)


def scan_package_hidden_imports(package: str, package_dir: Path) -> list[str]:
    """列出一个包目录下的所有模块名（含包自身），用于 hidden-import。"""
    if not package_dir.exists():
        return []

    discovered: set[str] = set()
    for py_file in package_dir.rglob("*.py"):
        rel = py_file.relative_to(package_dir)
        parts = list(rel.with_suffix("").parts)
        if any(part.startswith(".") for part in parts):
            continue
        if parts[-1] == "__init__":
            parts.pop()
        discovered.add(".".join([package, *parts]) if parts else package)

    return sorted(discovered)


def scan_agent_hidden_imports() -> list[str]:
    """扫描 GalTransl/Agent 下的模块，用于 hidden-import。

    GalTransl/Agent/runtime.py 是按模块名动态 import 一批子模块、再把它们的名字重导出的
    （见它顶部的 _MODULES），PyInstaller 的静态分析看不见这些字符串，只会顺着
    server.py -> Agent/__init__.py -> Agent/runtime.py 的静态引用把它们之外的东西打进去，
    于是打包出来的 exe 一启动就报 ModuleNotFoundError: No module named 'GalTransl.Agent.core'。
    这里直接扫目录补进 hidden-import，以后往 Agent 里加模块不用再手改清单。
    """
    return scan_package_hidden_imports("GalTransl.Agent", AGENT_DIR)


# ─── 构建步骤 ───────────────────────────────────────────

def clean():
    """清理旧构建产物"""
    print(f"\033[33m清理旧产物: {RELEASE_DIR}\033[0m")
    if RELEASE_DIR.exists():
        shutil.rmtree(RELEASE_DIR)
    tauri_target = TAURI_DIR / "target" / "release"
    if tauri_target.exists():
        print("  清理 Tauri release target...")
        shutil.rmtree(tauri_target)
    fe_dist = DESKTOP_DIR / "dist"
    if fe_dist.exists():
        shutil.rmtree(fe_dist)
    for d in ["build", "dist"]:
        p = ROOT / d
        if p.exists():
            shutil.rmtree(p)
    if VENV_DIR.exists():
        print(f"  清理构建虚拟环境: {VENV_DIR}")
        shutil.rmtree(VENV_DIR)
    print("  清理完成")


def build_frontend():
    """构建当前平台的 Tauri 前端 (需要 Rust 和对应系统依赖)"""
    print("\n\033[32m═══ 构建前端 (Tauri Desktop) ═══\033[0m")

    if not (DESKTOP_DIR / "node_modules").exists():
        print("  安装前端依赖...")
        run("npm ci --no-audit --no-fund", cwd=DESKTOP_DIR)

    print("  执行 tauri build（不生成安装包）...")
    run("npx tauri build --no-bundle", cwd=DESKTOP_DIR)

    exe_path = find_frontend_exe()
    if not exe_path:
        print("\033[31m前端可执行文件未找到: target/release 下不存在可识别产物\033[0m")
        sys.exit(1)

    print(f"\033[32m  前端构建成功: {exe_path}\033[0m")
    return exe_path


def build_backend(onefile: bool = False):
    """构建 Python 后端 (PyInstaller 打包，在虚拟环境中)"""
    print("\n\033[32m═══ 构建后端 (PyInstaller) ═══\033[0m")

    # 虚拟环境 python 路径
    if sys.platform == "win32":
        venv_python = VENV_DIR / "Scripts" / "python.exe"
        venv_pip = VENV_DIR / "Scripts" / "pip.exe"
    else:
        venv_python = VENV_DIR / "bin" / "python"
        venv_pip = VENV_DIR / "bin" / "pip"

    # 创建虚拟环境（如果不存在）
    if not venv_python.exists():
        print(f"  创建构建虚拟环境: {VENV_DIR}")
        run(f'"{sys.executable}" -m venv "{VENV_DIR}"')

    # 安装依赖
    req_file = ROOT / "requirements.txt"
    if req_file.exists():
        print("  安装项目依赖到虚拟环境...")
        run(f'"{venv_pip}" install -r "{req_file}"')
    else:
        print("  未找到 requirements.txt，跳过依赖安装")

    # 安装 PyInstaller
    print("  安装 PyInstaller...")
    run(f'"{venv_pip}" install pyinstaller --quiet')

    # 需要隐藏导入的模块列表
    hidden_imports = [
        "GalTransl", "GalTransl.server", "GalTransl.Service",
        "GalTransl.Runner", "GalTransl.Cache", "GalTransl.CSentense",
        "GalTransl.CSerialize", "GalTransl.CSplitter",
        "GalTransl.Dictionary", "GalTransl.ConfigHelper",
        "GalTransl.AppSettings", "GalTransl.COpenAI",
        "GalTransl.Name", "GalTransl.i18n", "GalTransl.Problem",
        "GalTransl.Utils", "GalTransl.TerminalOutput",
        "GalTransl.yapsy", "GalTransl.Frontend",
        "GalTransl.Frontend.LLMTranslate",
    ]

    plugin_runtime_imports = [
        "requests",
        "playsound3",
        "budoux",
        "openpyxl",
        "orjson",
        "bs4",
        "yaml",
    ]

    auto_plugin_imports = scan_plugin_hidden_imports()
    if auto_plugin_imports:
        print(f"  自动扫描插件依赖: {', '.join(auto_plugin_imports)}")

    agent_imports = scan_agent_hidden_imports()
    if agent_imports:
        print(f"  自动扫描 Agent 模块: {len(agent_imports)} 个")

    hidden_imports.extend(plugin_runtime_imports)
    hidden_imports.extend(auto_plugin_imports)
    hidden_imports.extend(agent_imports)
    hidden_imports = sorted(set(hidden_imports))

    hidden_args = " ".join(f'--hidden-import="{m}"' for m in hidden_imports)

    cmd = (
        f'"{venv_python}" -m PyInstaller '
        f"--noupx "
        f"--noconfirm "
        f"--clean "
        f"--name {BACKEND_DIST_NAME} "
        f"{'--onefile ' if onefile else ''}"
        f"{hidden_args} "
        f'--collect-data="GalTransl" '
        f"--distpath dist "
        f"--workpath build "
        f'"{BACKEND_ENTRY}"'
    )
    run(cmd)

    backend_exe = find_backend_executable(onefile=onefile)

    if not backend_exe or not backend_exe.exists():
        expected_path = ROOT / "dist" / BACKEND_DIST_NAME / backend_executable_name()
        print(f"\033[31m后端可执行文件未找到: {expected_path}\033[0m")
        sys.exit(1)

    if not backend_exe.parent.exists():
        print(f"\033[31m后端可执行文件未找到: {backend_exe}\033[0m")
        sys.exit(1)

    print(f"\033[32m  后端构建成功: {backend_exe}\033[0m")
    return backend_exe


def assemble_release(frontend_exe: Path | None, backend_exe: Path):
    """组装发布目录"""
    print("\n\033[32m═══ 组装发布包 ═══\033[0m")

    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)

    # 1. 复制前端可执行文件（如果有）
    if frontend_exe and frontend_exe.exists():
        dst_exe = BUILD_DIR / frontend_executable_name()
        shutil.copy2(frontend_exe, dst_exe)
        if sys.platform != "win32":
            dst_exe.chmod(dst_exe.stat().st_mode | 0o111)
        print(f"  复制前端可执行文件 -> {dst_exe}")

    # 2. 复制后端
    dst_backend_dir = BUILD_DIR / "backend"
    dst_backend_dir.mkdir(exist_ok=True)
    if backend_exe.parent.name == BACKEND_DIST_NAME:
        copy_dir_filtered(backend_exe.parent, dst_backend_dir)
        print("  复制后端运行时目录 -> backend/")
    else:
        dst_name = backend_executable_name()
        shutil.copy2(backend_exe, dst_backend_dir / dst_name)
        print(f"  复制后端 -> backend/{dst_name}")

    # 3. 复制插件
    if PLUGINS_DIR.exists():
        dst_plugins = BUILD_DIR / "plugins"
        copy_dir_filtered(PLUGINS_DIR, dst_plugins)
        print(f"  复制插件目录 -> plugins/")

    # 4. 复制字典
    if DICT_DIR.exists():
        dst_dict = BUILD_DIR / "Dict"
        copy_dir_filtered(DICT_DIR, dst_dict)
        print("  复制字典目录 -> Dict/")

    # 5. 复制翻译指南
    if GUIDELINES_DIR.exists():
        dst_guidelines = BUILD_DIR / "translation_guidelines"
        copy_dir_filtered(GUIDELINES_DIR, dst_guidelines)
        print("  复制翻译指南目录 -> translation_guidelines/")

    # 6. 复制运行时资源
    if RES_DIR.exists():
        dst_res = BUILD_DIR / "res"
        copy_dir_filtered(RES_DIR, dst_res)
        print("  复制运行时资源目录 -> res/")

    print(f"\n\033[32m发布包组装完成: {BUILD_DIR}\033[0m")


def create_archive():
    """创建平台压缩包，Linux 使用 tar.gz 以保留可执行权限。"""
    print("\n\033[32m═══ 创建压缩包 ═══\033[0m")
    archive_path = RELEASE_DIR / ARCHIVE_NAME
    if archive_path.exists():
        archive_path.unlink()

    archive_format = "zip" if sys.platform == "win32" else "gztar"
    archive_base = RELEASE_DIR / BUILD_NAME
    shutil.make_archive(
        str(archive_base),
        archive_format,
        root_dir=str(RELEASE_DIR),
        base_dir=BUILD_NAME,
    )
    print(f"  压缩包已创建: {archive_path}")


# ─── 主流程 ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="GalTransl 平台发布版构建脚本")
    parser.add_argument("--skip-fe", action="store_true", help="跳过 Tauri 前端构建")
    parser.add_argument("--skip-be", action="store_true", help="跳过后端构建")
    parser.add_argument("--clean", action="store_true", help="构建前清理旧产物")
    parser.add_argument("--no-archive", "--no-zip", dest="no_archive", action="store_true", help="不创建发布压缩包")
    parser.add_argument("--onefile", action="store_true", help="将 Python 后端打包为单文件 sidecar")
    args = parser.parse_args()

    print(f"\033[1mGalTransl v{VERSION} {PLATFORM_TAG} 发布版构建\033[0m")
    print(f"输出目录: {RELEASE_DIR}\n")

    if args.clean:
        clean()

    frontend_exe = None
    backend_exe = None

    # 前端构建
    if not args.skip_fe:
        frontend_exe = build_frontend()
    else:
        # 尝试从已有构建中找
        candidate = find_frontend_exe()
        if candidate:
            frontend_exe = candidate
            print(f"跳过前端构建，使用已有可执行文件: {frontend_exe}")
        else:
            print("跳过前端构建（无已有可执行文件，最终发布包将不含前端）")

    # 后端构建
    if not args.skip_be:
        backend_exe = build_backend(onefile=args.onefile)
    else:
        candidate = find_backend_executable()
        if candidate:
            backend_exe = candidate
            print(f"跳过后端构建，使用已有: {backend_exe}")
        else:
            print("\033[31m跳过后端构建但未找到已有可执行文件\033[0m")
            sys.exit(1)

    # 组装
    assemble_release(frontend_exe, backend_exe)

    # 压缩
    if not args.no_archive:
        create_archive()

    print(f"\n\033[32m✅ 构建完成！发布包位于: {BUILD_DIR}\033[0m")
    if not args.no_archive:
        print(f"   压缩包: {RELEASE_DIR / ARCHIVE_NAME}")

    # 清理构建临时目录
    dist_dir = ROOT / "dist"
    if dist_dir.exists():
        print(f"\n清理临时目录: {dist_dir}")
        shutil.rmtree(dist_dir)


if __name__ == "__main__":
    main()
