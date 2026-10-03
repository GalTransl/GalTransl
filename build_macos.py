#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DESKTOP_DIR = ROOT / "desktop"
TAURI_DIR = DESKTOP_DIR / "src-tauri"
RELEASE_DIR = ROOT / "release"
PORTABLE_NAME = "galtransl-desktop"
SIDECAR_NAME = "galtransl_backend"


def get_version() -> str:
    for line in (ROOT / "GalTransl" / "__init__.py").read_text(encoding="utf-8").splitlines():
        if line.startswith("GALTRANSL_VERSION"):
            return line.split('"')[1]
    raise RuntimeError("GALTRANSL_VERSION not found")


VERSION = get_version()


def architecture() -> tuple[str, str]:
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "x86_64", "x86_64-apple-darwin"
    if machine in {"arm64", "aarch64"}:
        return "arm64", "aarch64-apple-darwin"
    raise SystemExit(f"unsupported architecture: {machine}; expected x86_64 or arm64")


ARCH_TAG, TARGET_TRIPLE = architecture()
RELEASE_NAME = f"GalTransl_{VERSION}_macos_{ARCH_TAG}"
RELEASE_APP_DIR = RELEASE_DIR / RELEASE_NAME
SOURCE_RELEASE_NAME = f"GalTransl_{VERSION}_darwin"


def run(command: list[str], cwd: Path | None = None) -> None:
    print(f"\033[36m> {' '.join(command)}\033[0m", flush=True)
    subprocess.run(command, cwd=cwd or ROOT, check=True)


def ensure_platform() -> None:
    if sys.platform != "darwin":
        raise SystemExit("build_macos.py only supports macOS")


def ensure_commands() -> None:
    missing = [command for command in ("npm", "npx", "cargo") if shutil.which(command) is None]
    if missing:
        raise SystemExit(f"missing build commands: {', '.join(missing)}")


def build_backend_release() -> Path:
    run(
        [
            sys.executable,
            str(ROOT / "build_release.py"),
            "--skip-fe",
            "--no-archive",
            "--onefile",
        ]
    )
    source = RELEASE_DIR / SOURCE_RELEASE_NAME
    backend = source / "backend" / SIDECAR_NAME
    if not backend.is_file():
        raise SystemExit(f"backend executable not found: {backend}")
    if RELEASE_APP_DIR.exists():
        shutil.rmtree(RELEASE_APP_DIR)
    source.rename(RELEASE_APP_DIR)
    return RELEASE_APP_DIR / "backend" / SIDECAR_NAME


def stage_sidecar(backend: Path) -> Path:
    binaries_dir = TAURI_DIR / "binaries"
    binaries_dir.mkdir(parents=True, exist_ok=True)
    sidecar = binaries_dir / f"{SIDECAR_NAME}-{TARGET_TRIPLE}"
    shutil.copy2(backend, sidecar)
    sidecar.chmod(sidecar.stat().st_mode | 0o111)
    return sidecar


def build_tauri_bundle() -> None:
    bundle_root = TAURI_DIR / "target" / "release" / "bundle"
    if bundle_root.exists():
        shutil.rmtree(bundle_root)

    config_path = TAURI_DIR / ".tauri-macos-build.json"
    config = {
        "version": VERSION,
        "bundle": {
            "targets": ["dmg"],
            "externalBin": [f"binaries/{SIDECAR_NAME}"],
        },
    }
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        run(
            [
                "npx",
                "tauri",
                "build",
                "--ci",
                "--bundles",
                "dmg",
                "--config",
                str(config_path),
            ],
            cwd=DESKTOP_DIR,
        )
    finally:
        config_path.unlink(missing_ok=True)


def copy_bundle_outputs() -> list[Path]:
    binary = TAURI_DIR / "target" / "release" / PORTABLE_NAME
    if not binary.is_file():
        raise SystemExit(f"Tauri executable not found: {binary}")

    RELEASE_APP_DIR.mkdir(parents=True, exist_ok=True)
    portable = RELEASE_APP_DIR / PORTABLE_NAME
    shutil.copy2(binary, portable)
    portable.chmod(portable.stat().st_mode | 0o111)

    dmg_matches = sorted((TAURI_DIR / "target" / "release" / "bundle" / "dmg").glob("*.dmg"))
    if not dmg_matches:
        raise SystemExit("Tauri did not produce a macOS DMG")
    dmg = RELEASE_DIR / f"{RELEASE_NAME}.dmg"
    shutil.copy2(dmg_matches[-1], dmg)

    archive = RELEASE_DIR / f"{RELEASE_NAME}.tar.gz"
    archive.unlink(missing_ok=True)
    shutil.make_archive(
        str(RELEASE_DIR / RELEASE_NAME),
        "gztar",
        root_dir=str(RELEASE_DIR),
        base_dir=RELEASE_NAME,
    )
    return [dmg, archive]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build GalTransl for macOS")
    parser.add_argument("--keep-staging", action="store_true", help="keep the staged Tauri sidecar")
    args = parser.parse_args()

    ensure_platform()
    ensure_commands()
    backend = build_backend_release()
    sidecar = stage_sidecar(backend)
    try:
        build_tauri_bundle()
        outputs = copy_bundle_outputs()
        for output in outputs:
            print(f"artifact: {output}")
    finally:
        if not args.keep_staging:
            sidecar.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
