#!/usr/bin/env python3
"""Check final release archives without a source checkout or translation API."""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import zipfile
from base64 import urlsafe_b64encode
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener


FORMATS = {
    "win": ("zip",),
    "linux_x86_64": ("tar.gz",),
    "macos_x86_64": ("dmg", "tar.gz"),
    "macos_arm64": ("dmg", "tar.gz"),
}
RESOURCE_DIRS = ("plugins", "Dict", "translation_guidelines", "res")
SAMPLE_TEXT = "こんにちは、世界。"


def check_executables(paths: tuple[Path, ...], platform: str) -> None:
    for path in paths:
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"Missing or empty packaged executable: {path}")
        if platform != "win" and not os.access(path, os.X_OK):
            raise RuntimeError(f"Packaged executable lost its execute permission: {path}")


def check_resources(root: Path) -> None:
    for name in RESOURCE_DIRS:
        directory = root / name
        if not directory.is_dir() or not any(directory.iterdir()):
            raise RuntimeError(f"Missing or empty packaged resources: {directory}")


def expected_artifacts(directory: Path, version: str, platform: str) -> list[Path]:
    paths = [directory / f"GalTransl_{version}_{platform}.{ext}" for ext in FORMATS[platform]]
    for path in paths:
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"Missing or empty release artifact: {path}")
    return paths


def unpack_release(archive: Path, destination: Path, version: str, platform: str) -> Path:
    if platform == "win":
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(destination)
    else:
        with tarfile.open(archive, "r:gz") as bundle:
            bundle.extractall(destination, filter="data")
    root = destination / f"GalTransl_{version}_{platform}"
    frontend = root / ("GalTransl Desktop.exe" if platform == "win" else "galtransl-desktop")
    backend = root / "backend" / ("galtransl_backend.exe" if platform == "win" else "galtransl_backend")
    check_executables((frontend, backend), platform)
    check_resources(root)
    return backend


@contextmanager
def installed_dmg_app(dmg: Path, directory: Path):
    """Inspect an installed copy, including cleanup when any check fails."""
    mount = directory / "mount"
    mount.mkdir(parents=True)
    subprocess.run([
        "hdiutil", "attach", str(dmg.resolve()), "-readonly", "-nobrowse",
        "-mountpoint", str(mount), "-quiet",
    ], check=True)
    try:
        apps = list(mount.glob("*.app"))
        if len(apps) != 1:
            raise RuntimeError(f"Expected exactly one application in {dmg}, found {len(apps)}")
        app = directory / "installed" / apps[0].name
        shutil.copytree(apps[0], app, symlinks=True)
        yield app
    finally:
        subprocess.run(["hdiutil", "detach", str(mount), "-quiet"], check=True)


def check_macos_app(app: Path, version: str, platform: str) -> tuple[Path, Path]:
    contents = app / "Contents"
    with (contents / "Info.plist").open("rb") as source:
        info = plistlib.load(source)
    if info.get("CFBundleShortVersionString") != version:
        raise RuntimeError(f"macOS app version does not match {version}: {app}")
    executable = info.get("CFBundleExecutable")
    if not isinstance(executable, str) or not executable or Path(executable).name != executable:
        raise RuntimeError(f"Invalid macOS bundle executable: {executable!r}")
    frontend = contents / "MacOS" / executable
    backend = contents / "MacOS" / "galtransl_backend"
    resources = contents / "Resources"
    check_executables((frontend, backend), platform)
    check_resources(resources)
    architecture = "arm64" if platform == "macos_arm64" else "x86_64"
    for binary in (frontend, backend):
        subprocess.run(["lipo", "-verify_arch", architecture, str(binary)], check=True)
    return backend, resources


def isolated_environment(directory: Path) -> dict[str, str]:
    directory = directory.resolve()
    env = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME", "GALTRANSL_RESOURCE_DIR", "GALTRANSL_DATA_DIR", "GALTRANSL_APP_SETTINGS_PATH"):
        env.pop(name, None)
    for name, folder in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                         ("APPDATA", "roaming"), ("LOCALAPPDATA", "local")):
        env[name] = str(directory / folder)
    # macOS ignores XDG, and Windows normally uses the program directory.
    env["GALTRANSL_DATA_DIR"] = str(directory / "data")
    env["GALTRANSL_APP_SETTINGS_PATH"] = str(directory / "config" / "app_settings.json")
    env["PYTHONUTF8"] = "1"
    return env


def wait_for_backend(process: subprocess.Popen, ready_file: Path, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Packaged backend exited with code {process.returncode}")
        if ready_file.is_file():
            ready = json.loads(ready_file.read_text(encoding="utf-8"))
            if ready.get("host") != "127.0.0.1" or not isinstance(ready.get("port"), int) or not 0 < ready["port"] < 65536:
                raise RuntimeError(f"Invalid backend ready file: {ready}")
            return f"http://127.0.0.1:{ready['port']}"
        time.sleep(0.1)
    raise TimeoutError(f"Packaged backend did not become ready within {timeout}s")


def check_api(base_url: str, version: str) -> None:
    # Never send these localhost requests through a runner's HTTP proxy.
    opener = build_opener(ProxyHandler({}))
    with opener.open(f"{base_url}/api/version", timeout=10) as response:
        actual_version = json.load(response).get("version")
    if actual_version != version:
        raise RuntimeError(f"Packaged version {actual_version!r} != expected {version!r}")
    with opener.open(f"{base_url}/api/plugins", timeout=10) as response:
        plugins = json.load(response).get("plugins")
    if not isinstance(plugins, list) or not plugins:
        raise RuntimeError("Packaged backend did not find any plugins")


def create_sample_epub(path: Path) -> None:
    """A real EPUB fixture using only the smoke runner's standard library."""
    with zipfile.ZipFile(path, "w") as book:
        book.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        book.writestr("META-INF/container.xml", '''<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="EPUB/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>''')
        book.writestr("EPUB/content.opf", '''<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="id">galtransl-smoke</dc:identifier><dc:title>Smoke</dc:title><dc:language>ja</dc:language>
    <meta property="dcterms:modified">2026-01-01T00:00:00Z</meta>
  </metadata>
  <manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/></manifest>
  <spine><itemref idref="chapter"/></spine>
</package>''')
        book.writestr("EPUB/chapter.xhtml", f'''<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Smoke</title></head>
<body><p>{SAMPLE_TEXT}</p></body></html>''')


def check_file_plugins(base_url: str, directory: Path) -> None:
    project = directory / "plugin-project"
    inputs = project / "gt_input"
    inputs.mkdir(parents=True)
    (project / "config.yaml").write_text("common: {}\nplugin:\n  filePlugin: auto\n", encoding="utf-8")
    (inputs / "smoke.txt").write_text(SAMPLE_TEXT + "\n", encoding="utf-8")
    (inputs / "smoke.json").write_text(json.dumps([{"message": SAMPLE_TEXT}]), encoding="utf-8")
    create_sample_epub(inputs / "smoke.epub")
    project_id = urlsafe_b64encode(str(project.resolve()).encode("utf-8")).decode("ascii").rstrip("=")
    opener = build_opener(ProxyHandler({}))
    for filename in ("smoke.txt", "smoke.json", "smoke.epub"):
        try:
            with opener.open(f"{base_url}/api/projects/{project_id}/input/{filename}", timeout=30) as response:
                result = json.load(response)
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Packaged file plugin failed for {filename}: {detail}") from error
        entries = result.get("entries")
        if not isinstance(entries, list) or not any(entry.get("pre_src") == SAMPLE_TEXT for entry in entries):
            raise RuntimeError(f"Packaged file plugin did not parse {filename}: {result}")


def stop_backend(process: subprocess.Popen) -> None:
    if sys.platform == "win32":
        if process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        # The Linux onefile bootloader may have a child; terminate its whole group.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if sys.platform == "win32":
            process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=10)


def smoke_backend(backend: Path, directory: Path, version: str, timeout: float,
                  resource_dir: Path | None = None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    ready_file = directory / "backend-ready.json"
    log_path = directory / "backend.log"
    env = isolated_environment(directory)
    if resource_dir is not None:
        # The desktop launcher sets this to Contents/Resources for a macOS app.
        env["GALTRANSL_RESOURCE_DIR"] = str(resource_dir.resolve())
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [str(backend), "--host", "127.0.0.1", "--port", "0", "--ready-file", str(ready_file)],
            cwd=directory,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=sys.platform != "win32",
        )
        try:
            base_url = wait_for_backend(process, ready_file, timeout)
            check_api(base_url, version)
            check_file_plugins(base_url, directory)
        finally:
            stop_backend(process)
            # Also keep successful startup logs in Actions for diagnostics.
            print(log_path.read_text(encoding="utf-8", errors="replace"), end="")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-dir", type=Path, default=Path("release"))
    parser.add_argument("--version", required=True)
    parser.add_argument("--platform", choices=FORMATS, required=True)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    expected_artifacts(args.release_dir, args.version, args.platform)
    extension = "zip" if args.platform == "win" else "tar.gz"
    archive = args.release_dir / f"GalTransl_{args.version}_{args.platform}.{extension}"
    with TemporaryDirectory(prefix="galtransl-release-smoke-") as tmp:
        directory = Path(tmp)
        backend = unpack_release(archive, directory / "unpacked", args.version, args.platform)
        smoke_backend(backend, directory / "portable-smoke", args.version, args.timeout)
        if args.platform.startswith("macos_"):
            dmg = args.release_dir / f"GalTransl_{args.version}_{args.platform}.dmg"
            with installed_dmg_app(dmg, directory / "dmg") as app:
                backend, resources = check_macos_app(app, args.version, args.platform)
                smoke_backend(backend, directory / "app-smoke", args.version, args.timeout, resources)
    print(f"Release smoke passed: {archive.name}")


if __name__ == "__main__":
    main()
