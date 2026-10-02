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
TARGET_TRIPLE = "x86_64-unknown-linux-gnu"
PORTABLE_NAME = "galtransl-desktop"
SIDECAR_NAME = "galtransl_backend"


def get_version() -> str:
    for line in (ROOT / "GalTransl" / "__init__.py").read_text(encoding="utf-8").splitlines():
        if line.startswith("GALTRANSL_VERSION"):
            return line.split('"')[1]
    raise RuntimeError("GALTRANSL_VERSION not found")


VERSION = get_version()
RELEASE_NAME = f"GalTransl_{VERSION}_linux_x86_64"
RELEASE_APP_DIR = RELEASE_DIR / RELEASE_NAME


def run(command: list[str], cwd: Path | None = None) -> None:
    printable = " ".join(command)
    print(f"\033[36m> {printable}\033[0m", flush=True)
    subprocess.run(command, cwd=cwd or ROOT, check=True)


def ensure_platform() -> None:
    if sys.platform != "linux":
        raise SystemExit("build_linux_x64.py only supports Linux")
    machine = platform.machine().lower()
    if machine not in {"x86_64", "amd64"}:
        raise SystemExit(f"unsupported architecture: {machine}; expected x86_64")


def ensure_build_dependencies() -> None:
    required_commands = ["npm", "npx", "cargo", "pkg-config", "xz"]
    missing_commands = [command for command in required_commands if shutil.which(command) is None]
    if missing_commands:
        raise SystemExit(f"missing build commands: {', '.join(missing_commands)}")

    missing_packages = []
    for package in ["webkit2gtk-4.1", "javascriptcoregtk-4.1", "librsvg-2.0"]:
        result = subprocess.run(
            ["pkg-config", "--exists", package],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            missing_packages.append(package)
    if missing_packages:
        raise SystemExit(
            "missing Tauri Linux development packages: "
            + ", ".join(missing_packages)
            + "\nUbuntu/Debian: sudo apt-get install libwebkit2gtk-4.1-dev "
            "libayatana-appindicator3-dev librsvg2-dev"
            + "\nFedora: sudo dnf install webkit2gtk4.1-devel "
            "libappindicator-gtk3-devel librsvg2-devel"
        )


def ensure_bundle_tools(formats: list[str]) -> None:
    tools = []
    if "deb" in formats:
        tools.append("dpkg-deb")
    if "rpm" in formats:
        tools.append("rpmbuild")
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing:
        raise SystemExit(f"missing packaging commands: {', '.join(missing)}")


def ensure_frontend_dependencies() -> None:
    if not (DESKTOP_DIR / "node_modules").exists():
        run(["npm", "ci", "--no-audit", "--no-fund"], cwd=DESKTOP_DIR)


def build_backend_release() -> Path:
    run(
        [
            sys.executable,
            str(ROOT / "build_release.py"),
            "--skip-fe",
            "--no-archive",
            "--onefile",
        ],
        cwd=ROOT,
    )
    backend = RELEASE_APP_DIR / "backend" / SIDECAR_NAME
    if not backend.is_file():
        raise SystemExit(f"backend executable not found: {backend}")
    return backend


def stage_sidecar(backend: Path) -> Path:
    binaries_dir = TAURI_DIR / "binaries"
    binaries_dir.mkdir(parents=True, exist_ok=True)
    sidecar = binaries_dir / f"{SIDECAR_NAME}-{TARGET_TRIPLE}"
    shutil.copy2(backend, sidecar)
    sidecar.chmod(sidecar.stat().st_mode | 0o111)
    return sidecar


def build_tauri_bundles(formats: list[str]) -> None:
    bundle_root = TAURI_DIR / "target" / "release" / "bundle"
    if bundle_root.exists():
        shutil.rmtree(bundle_root)

    config_path = TAURI_DIR / ".tauri-linux-build.json"
    config = {
        "version": VERSION,
        "bundle": {
            "targets": formats,
            "externalBin": [f"binaries/{SIDECAR_NAME}"],
        },
    }
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    try:
        run(
            [
                "npx",
                "tauri",
                "build",
                "--ci",
                "--bundles",
                ",".join(formats),
                "--config",
                str(config_path),
            ],
            cwd=DESKTOP_DIR,
        )
    finally:
        config_path.unlink(missing_ok=True)


def build_portable_frontend() -> None:
    run(["npx", "tauri", "build", "--no-bundle", "--ci"], cwd=DESKTOP_DIR)


def compress_appimage() -> None:
    appimage = RELEASE_DIR / f"{RELEASE_NAME}.AppImage"
    if not appimage.is_file():
        return
    run(["xz", "-T0", "-6", "-k", "-f", str(appimage)])
    compressed = appimage.with_suffix(appimage.suffix + ".xz")
    print(f"compressed AppImage: {compressed}")


def copy_portable_frontend() -> Path:
    source = TAURI_DIR / "target" / "release" / PORTABLE_NAME
    if not source.is_file():
        raise SystemExit(f"Tauri executable not found: {source}")
    destination = RELEASE_APP_DIR / PORTABLE_NAME
    shutil.copy2(source, destination)
    destination.chmod(destination.stat().st_mode | 0o111)
    return destination


def copy_bundle_artifacts(formats: list[str]) -> list[Path]:
    bundle_root = TAURI_DIR / "target" / "release" / "bundle"
    mapping = {
        "deb": ("deb", "*.deb"),
        "rpm": ("rpm", "*.rpm"),
        "appimage": ("appimage", "*.AppImage"),
    }
    copied: list[Path] = []
    RELEASE_DIR.mkdir(parents=True, exist_ok=True)
    for bundle_type, (folder, pattern) in mapping.items():
        if bundle_type not in formats:
            continue
        matches = sorted((bundle_root / folder).glob(pattern))
        if not matches:
            raise SystemExit(f"Tauri did not produce a {bundle_type} bundle")
        extension = ".AppImage" if bundle_type == "appimage" else f".{bundle_type}"
        destination = RELEASE_DIR / f"{RELEASE_NAME}{extension}"
        shutil.copy2(matches[-1], destination)
        copied.append(destination)
    return copied


def create_portable_archive() -> Path:
    archive_base = RELEASE_DIR / RELEASE_NAME
    archive_path = RELEASE_DIR / f"{RELEASE_NAME}.tar.gz"
    archive_path.unlink(missing_ok=True)
    shutil.make_archive(
        str(archive_base),
        "gztar",
        root_dir=str(RELEASE_DIR),
        base_dir=RELEASE_NAME,
    )
    return archive_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build GalTransl for Linux x86_64")
    parser.add_argument(
        "--formats",
        default="deb,rpm,appimage",
        help="comma-separated Tauri bundle formats",
    )
    parser.add_argument("--no-bundles", action="store_true", help="skip deb/rpm/AppImage")
    parser.add_argument("--no-portable", action="store_true", help="skip tar.gz")
    parser.add_argument(
        "--keep-staging",
        action="store_true",
        help="keep the staged Tauri sidecar after the build",
    )
    args = parser.parse_args()

    ensure_platform()
    ensure_build_dependencies()
    formats = [] if args.no_bundles else [item.strip() for item in args.formats.split(",") if item.strip()]
    ensure_bundle_tools(formats)
    ensure_frontend_dependencies()

    backend = build_backend_release()
    sidecar = stage_sidecar(backend)

    try:
        if args.no_bundles:
            build_portable_frontend()
        else:
            build_tauri_bundles(formats)
            for artifact in copy_bundle_artifacts(formats):
                print(f"bundle: {artifact}")
            if "appimage" in formats:
                compress_appimage()

        copy_portable_frontend()
        if not args.no_portable:
            print(f"portable archive: {create_portable_archive()}")
    finally:
        if not args.keep_staging:
            sidecar.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
