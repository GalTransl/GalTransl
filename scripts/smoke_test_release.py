#!/usr/bin/env python3
"""Check final release archives without a source checkout or translation API."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import ProxyHandler, build_opener


FORMATS = {
    "win": ("zip",),
    "linux_x86_64": ("tar.gz",),
    "macos_x86_64": ("dmg", "tar.gz"),
    "macos_arm64": ("dmg", "tar.gz"),
}
RESOURCE_DIRS = ("plugins", "Dict", "translation_guidelines", "res")


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
    for path in (frontend, backend):
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"Missing or empty packaged executable: {path}")
        if platform != "win" and not os.access(path, os.X_OK):
            raise RuntimeError(f"Packaged executable lost its execute permission: {path}")
    for name in RESOURCE_DIRS:
        directory = root / name
        if not directory.is_dir() or not any(directory.iterdir()):
            raise RuntimeError(f"Missing or empty packaged resources: {directory}")
    return backend


def isolated_environment(directory: Path) -> dict[str, str]:
    env = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME", "GALTRANSL_RESOURCE_DIR", "GALTRANSL_DATA_DIR", "GALTRANSL_APP_SETTINGS_PATH"):
        env.pop(name, None)
    for name, folder in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                         ("APPDATA", "roaming"), ("LOCALAPPDATA", "local")):
        env[name] = str(directory / folder)
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


def smoke_backend(backend: Path, directory: Path, version: str, timeout: float) -> None:
    ready_file = directory / "backend-ready.json"
    log_path = directory / "backend.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [str(backend), "--host", "127.0.0.1", "--port", "0", "--ready-file", str(ready_file)],
            cwd=directory,
            env=isolated_environment(directory),
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=sys.platform != "win32",
        )
        try:
            base_url = wait_for_backend(process, ready_file, timeout)
            check_api(base_url, version)
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
        smoke_backend(backend, directory, args.version, args.timeout)
    print(f"Release smoke passed: {archive.name}")


if __name__ == "__main__":
    main()
