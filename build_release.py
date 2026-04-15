#!/usr/bin/env python3
"""
GalTransl Windows 发布版构建脚本 (Python)

建议在 Windows 上使用 PowerShell 脚本 build_release.ps1 构建。
此 Python 脚本适用于 WSL/Linux 环境，可以单独构建后端部分。

用法:
  python build_release.py           # 构建全部
  python build_release.py --skip-fe # 跳过前端构建（WSL 下推荐）
  python build_release.py --skip-be # 跳过后端构建
  python build_release.py --clean   # 构建前清理旧产物
  python build_release.py --no-zip  # 不创建 zip 压缩包

产出目录:
  release/
    GalTransl-Desktop-{version}-win64/
      GalTransl Desktop.exe          # Tauri 前端 (仅 Windows 构建)
      backend/galtransl_backend.exe  # Python 后端 (PyInstaller)
      plugins/                       # 插件目录
      启动 GalTransl Desktop.bat     # 启动脚本
      README.txt
    GalTransl-Desktop-{version}-win64.zip
"""

import argparse
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


def get_version() -> str:
    """从 GalTransl/__init__.py 读取版本号"""
    init_py = ROOT / "GalTransl" / "__init__.py"
    for line in init_py.read_text(encoding="utf-8").splitlines():
        if line.startswith("GALTRANSL_VERSION"):
            return line.split('"')[1]
    return "0.0.0"


VERSION = get_version()
BUILD_NAME = f"GalTransl-Desktop-{VERSION}-win64"
BUILD_DIR = RELEASE_DIR / BUILD_NAME
ZIP_NAME = f"{BUILD_NAME}.zip"

BACKEND_ENTRY = ROOT / "run_backend.py"
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
    """构建 Tauri 前端 (需要 Windows 环境 + Rust 工具链)"""
    print("\n\033[32m═══ 构建前端 (Tauri Desktop) ═══\033[0m")

    if not (DESKTOP_DIR / "node_modules").exists():
        print("  安装前端依赖...")
        run("npm install", cwd=DESKTOP_DIR)

    print("  执行 tauri build...")
    run("npx tauri build", cwd=DESKTOP_DIR)

    bundle_dir = TAURI_DIR / "target" / "release" / "bundle"
    exe_path = TAURI_DIR / "target" / "release" / "GalTransl Desktop.exe"

    if not exe_path.exists():
        print(f"\033[31m前端 exe 未找到: {exe_path}\033[0m")
        sys.exit(1)

    print(f"\033[32m  前端 exe 构建成功: {exe_path}\033[0m")
    return exe_path, bundle_dir


def build_backend():
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
        run(f'"{venv_pip}" install -r "{req_file}" --quiet')
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

    hidden_args = " ".join(f'--hidden-import="{m}"' for m in hidden_imports)

    cmd = (
        f'"{venv_python}" -m PyInstaller '
        f"--onefile "
        f"--noupx "
        f"--noconfirm "
        f"--clean "
        f"--name galtransl_backend "
        f"{hidden_args} "
        f'--collect-data="GalTransl" '
        f"--distpath dist "
        f"--workpath build "
        f"{BACKEND_ENTRY}"
    )
    run(cmd)

    # PyInstaller 在 Windows 上输出 .exe，在 Linux 上输出 ELF
    ext = ".exe" if sys.platform == "win32" else ""
    backend_exe = ROOT / "dist" / f"galtransl_backend{ext}"

    if not backend_exe.exists():
        print(f"\033[31m后端可执行文件未找到: {backend_exe}\033[0m")
        sys.exit(1)

    print(f"\033[32m  后端构建成功: {backend_exe}\033[0m")
    return backend_exe


def assemble_release(frontend_exe: Path | None, bundle_dir: Path | None, backend_exe: Path):
    """组装发布目录"""
    print("\n\033[32m═══ 组装发布包 ═══\033[0m")

    BUILD_DIR.mkdir(parents=True, exist_ok=True)

    # 1. 复制前端 exe（如果有）
    if frontend_exe and frontend_exe.exists():
        dst_exe = BUILD_DIR / "GalTransl Desktop.exe"
        shutil.copy2(frontend_exe, dst_exe)
        print(f"  复制前端 exe -> {dst_exe}")

    # 2. 复制 NSIS 安装包（如果有）
    if bundle_dir and bundle_dir.exists():
        nsis_dir = bundle_dir / "nsis"
        if nsis_dir.exists():
            for installer in nsis_dir.glob("*.exe"):
                dst = BUILD_DIR / installer.name
                shutil.copy2(installer, dst)
                print(f"  复制 NSIS 安装包 -> {dst}")

    # 3. 复制后端
    dst_backend_dir = BUILD_DIR / "backend"
    dst_backend_dir.mkdir(exist_ok=True)
    dst_name = "galtransl_backend.exe"
    shutil.copy2(backend_exe, dst_backend_dir / dst_name)
    print(f"  复制后端 -> backend/{dst_name}")

    # 4. 复制插件
    if PLUGINS_DIR.exists():
        dst_plugins = BUILD_DIR / "plugins"
        copy_dir_filtered(PLUGINS_DIR, dst_plugins)
        print(f"  复制插件目录 -> plugins/")

    # 5. 复制 CLI 入口
    cli_entry = ROOT / "run_GalTransl.py"
    if cli_entry.exists():
        shutil.copy2(cli_entry, BUILD_DIR / "run_GalTransl.py")
        print(f"  复制 CLI 入口 -> run_GalTransl.py")

    # 6. 生成启动脚本
    launcher = BUILD_DIR / "启动 GalTransl Desktop.bat"
    launcher.write_text(
        "@echo off\n"
        "chcp 65001 >nul\n"
        "echo 正在启动 GalTransl Desktop...\n"
        'start "" "backend\\galtransl_backend.exe"\n'
        "timeout /t 2 /nobreak >nul\n"
        'start "" "GalTransl Desktop.exe"\n',
        encoding="utf-8",
    )
    print(f"  生成启动脚本 -> 启动 GalTransl Desktop.bat")

    # 7. 生成 README
    readme = BUILD_DIR / "README.txt"
    readme.write_text(
        f"GalTransl Desktop v{VERSION}\n"
        "=" * 40 + "\n\n"
        "启动方式:\n"
        "  方式1: 双击「启动 GalTransl Desktop.bat」同时启动前后端\n"
        "  方式2: 先运行 backend\\galtransl_backend.exe，再运行 GalTransl Desktop.exe\n\n"
        "CLI 模式:\n"
        "  python run_GalTransl.py\n\n"
        "插件目录: plugins/\n",
        encoding="utf-8",
    )
    print(f"  生成 README -> README.txt")

    print(f"\n\033[32m发布包组装完成: {BUILD_DIR}\033[0m")


def create_zip():
    """创建 zip 压缩包"""
    print("\n\033[32m═══ 创建压缩包 ═══\033[0m")
    zip_path = RELEASE_DIR / ZIP_NAME
    if zip_path.exists():
        zip_path.unlink()

    shutil.make_archive(
        str(zip_path.with_suffix("")),
        "zip",
        root_dir=str(RELEASE_DIR),
        base_dir=BUILD_NAME,
    )
    print(f"  压缩包已创建: {zip_path}")


# ─── 主流程 ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="GalTransl Windows 发布版构建脚本")
    parser.add_argument("--skip-fe", action="store_true", help="跳过前端构建（WSL 下推荐）")
    parser.add_argument("--skip-be", action="store_true", help="跳过后端构建")
    parser.add_argument("--clean", action="store_true", help="构建前清理旧产物")
    parser.add_argument("--no-zip", action="store_true", help="不创建 zip 压缩包")
    args = parser.parse_args()

    print(f"\033[1mGalTransl v{VERSION} Windows 发布版构建\033[0m")
    print(f"输出目录: {RELEASE_DIR}\n")

    if args.clean:
        clean()

    frontend_exe = None
    bundle_dir = None
    backend_exe = None

    # 前端构建
    if not args.skip_fe:
        frontend_exe, bundle_dir = build_frontend()
    else:
        # 尝试从已有构建中找
        candidate = TAURI_DIR / "target" / "release" / "GalTransl Desktop.exe"
        if candidate.exists():
            frontend_exe = candidate
            bundle_dir = TAURI_DIR / "target" / "release" / "bundle"
            print(f"跳过前端构建，使用已有 exe: {frontend_exe}")
        else:
            print("跳过前端构建（无已有 exe，最终发布包将不含前端）")

    # 后端构建
    if not args.skip_be:
        backend_exe = build_backend()
    else:
        ext = ".exe" if sys.platform == "win32" else ""
        candidate = ROOT / "dist" / f"galtransl_backend{ext}"
        if candidate.exists():
            backend_exe = candidate
            print(f"跳过后端构建，使用已有: {backend_exe}")
        else:
            print("\033[31m跳过后端构建但未找到已有可执行文件\033[0m")
            sys.exit(1)

    # 组装
    assemble_release(frontend_exe, bundle_dir, backend_exe)

    # 压缩
    if not args.no_zip:
        create_zip()

    print(f"\n\033[32m✅ 构建完成！发布包位于: {BUILD_DIR}\033[0m")
    if not args.no_zip:
        print(f"   压缩包: {RELEASE_DIR / ZIP_NAME}")


if __name__ == "__main__":
    main()
