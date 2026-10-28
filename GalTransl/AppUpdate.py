"""Download the existing portable release and prepare an isolated installer."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import platform
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import threading
import zipfile
from dataclasses import asdict, dataclass
from http import HTTPStatus
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse
from uuid import uuid4

import requests
from packaging.version import Version

from application_update import RELEASE_API, UpdatePlan
from GalTransl.RuntimePaths import get_active_dict_dir, get_resource_root, get_user_data_dir

DOWNLOAD_PREFIX = "https://github.com/GalTransl/GalTransl/releases/download/"
RESOURCE_NAMES = {"backend", "plugins", "Dict", "translation_guidelines", "res", "skills", "extraction-python"}


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class Installation:
    root: Path
    executable: Path
    platform: str


def installation() -> Installation | None:
    desktop = os.environ.get("GALTRANSL_DESKTOP_EXECUTABLE", "")
    if not getattr(sys, "frozen", False) or not desktop:
        return None
    executable = Path(desktop).resolve(strict=True)
    root = executable.parent
    if root != get_resource_root().resolve() or not (root / "backend").is_dir():
        return None
    machine = platform.machine().lower()
    targets = {
        "win32": {"amd64": "win", "x86_64": "win"},
        "linux": {"amd64": "linux_x86_64", "x86_64": "linux_x86_64"},
        "darwin": {"x86_64": "macos_x86_64", "arm64": "macos_arm64", "aarch64": "macos_arm64"},
    }
    target = targets.get(sys.platform, {}).get(machine, "")
    return Installation(root, executable, target) if target else None


@dataclass(frozen=True)
class ReleaseAsset:
    version: str
    name: str
    url: str
    digest: str
    size: int


def release_asset(current_version: str, target: str) -> ReleaseAsset:
    with requests.get(RELEASE_API, timeout=20) as response:
        response.raise_for_status()
        release = response.json()
    if not isinstance(release, dict) or not isinstance(release.get("tag_name"), str) or not isinstance(release.get("assets"), list):
        raise UpdateError("GitHub returned invalid release metadata")
    version = release["tag_name"].removeprefix("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version) or Version(version) <= Version(current_version):
        raise UpdateError("No newer stable application release is available")
    extension = "zip" if target == "win" else "tar.gz"
    name = f"GalTransl_{version}_{target}.{extension}"
    asset = next((value for value in release["assets"] if isinstance(value, dict) and value.get("name") == name), None)
    if not asset or not isinstance(asset.get("browser_download_url"), str) or not asset["browser_download_url"].startswith(DOWNLOAD_PREFIX):
        raise UpdateError(f"The release does not contain the portable package {name}")
    size = asset.get("size")
    if type(size) is not int or not 0 < size <= 1024 * 1024 * 1024:
        raise UpdateError("Invalid release download size")
    digest = asset.get("digest", "")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        checksum = next((value for value in release["assets"] if isinstance(value, dict) and value.get("name") == "SHA256SUMS.txt"), None)
        if not checksum or not isinstance(checksum.get("browser_download_url"), str) or not checksum["browser_download_url"].startswith(DOWNLOAD_PREFIX):
            raise UpdateError("The release has no SHA256 checksum for its portable package")
        with requests.get(checksum["browser_download_url"], timeout=20) as response:
            response.raise_for_status()
            digest = next(("sha256:" + match.group(1) for line in response.text.splitlines()
                           if (match := re.fullmatch(r"([0-9a-f]{64})\s+\*?" + re.escape(name), line))), "")
        if not digest:
            raise UpdateError("The release checksum file does not list its portable package")
    return ReleaseAsset(version, name, asset["browser_download_url"], digest.removeprefix("sha256:"), size)


def unpack_release(archive: Path, payload: Path, asset: ReleaseAsset, frontend: str) -> list[str]:
    expected = asset.name.removesuffix(".zip").removesuffix(".tar.gz")
    allowed = {*RESOURCE_NAMES, frontend}
    payload.mkdir()
    files: list[str] = []
    with zipfile.ZipFile(archive) if asset.name.endswith(".zip") else tarfile.open(archive, "r:gz") as bundle:
        entries = bundle.infolist() if isinstance(bundle, zipfile.ZipFile) else bundle.getmembers()
        total = sum(entry.file_size if isinstance(entry, zipfile.ZipInfo) else entry.size for entry in entries)
        if total > 2 * 1024 * 1024 * 1024:
            raise UpdateError("Release archive is too large to unpack")
        for entry in entries:
            name = entry.filename if isinstance(entry, zipfile.ZipInfo) else entry.name
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name or not relative.parts or relative.parts[0] != expected:
                raise UpdateError(f"Invalid release archive path: {name}")
            directory = entry.is_dir() if isinstance(entry, zipfile.ZipInfo) else entry.isdir()
            if directory:
                continue
            if len(relative.parts) < 2 or relative.parts[1] not in allowed:
                raise UpdateError(f"Unexpected file in application release: {name}")
            regular = not stat.S_ISLNK(entry.external_attr >> 16) if isinstance(entry, zipfile.ZipInfo) else entry.isfile()
            if not regular:
                raise UpdateError(f"Release archive contains a link or special file: {name}")
            local = Path(*relative.parts[1:])
            target = payload / local
            target.parent.mkdir(parents=True, exist_ok=True)
            source = bundle.open(entry) if isinstance(bundle, zipfile.ZipFile) else bundle.extractfile(entry)
            if source is None:
                raise UpdateError(f"Cannot read release file: {name}")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            mode = entry.external_attr >> 16 if isinstance(entry, zipfile.ZipInfo) else entry.mode
            if os.name != "nt":
                target.chmod(mode & 0o777)
            files.append(local.as_posix())
    backend = "galtransl_backend.exe" if asset.name.endswith(".zip") else "galtransl_backend"
    if not {frontend, f"backend/{backend}"} <= set(files) or not all((payload / name).is_dir() for name in RESOURCE_NAMES):
        raise UpdateError("The application release is incomplete")
    return sorted(files, key=lambda name: (name != frontend, not name.startswith("backend/"), name))


class UpdateManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"phase": "idle", "id": "", "version": "", "downloaded": 0, "total": 0, "error": ""}

    def status(self):
        available = installation()
        with self.lock:
            state = dict(self.state)
        result = get_user_data_dir() / "update-result.json"
        if state["phase"] == "idle" and result.is_file():
            previous = json.loads(result.read_text(encoding="utf-8"))
            if isinstance(previous, dict) and previous.get("phase") == "failed":
                state.update(phase="failed", error=str(previous.get("error", "Application update failed")))
        return {**state, "supported": available is not None,
                "reason": "" if available else "development" if not getattr(sys, "frozen", False) else "installation"}

    def set_state(self, **values):
        with self.lock:
            self.state.update(values)

    def start(self, current_version: str, preserve_dict: bool):
        available = installation()
        if not available:
            raise UpdateError("Automatic updates require a packaged portable desktop application")
        with tempfile.TemporaryFile(dir=available.root):
            pass
        result_file = get_user_data_dir() / "update-result.json"
        if result_file.is_file():
            previous = json.loads(result_file.read_text(encoding="utf-8"))
            previous_id = previous.get("id") if isinstance(previous, dict) else None
            if isinstance(previous_id, str) and re.fullmatch(r"[0-9a-f]{32}", previous_id):
                previous_stage = Path(tempfile.gettempdir()) / f"galtransl-update-{previous_id}"
                if previous_stage.resolve().parent == Path(tempfile.gettempdir()).resolve():
                    shutil.rmtree(previous_stage, ignore_errors=True)
        result_file.unlink(missing_ok=True)
        with self.lock:
            if self.state["phase"] in {"downloading", "verifying", "extracting", "ready"}:
                raise UpdateError("An application update is already in progress")
            update_id = uuid4().hex
            self.state = {"phase": "downloading", "id": update_id, "version": "", "downloaded": 0, "total": 0, "error": ""}
        threading.Thread(target=self.prepare, args=(available, current_version, preserve_dict, update_id), daemon=True).start()
        return self.status()

    def prepare(self, available: Installation, current_version: str, preserve_dict: bool, update_id: str):
        stage = Path(tempfile.gettempdir()) / f"galtransl-update-{update_id}"
        try:
            asset = release_asset(current_version, available.platform)
            self.set_state(version=asset.version, total=asset.size)
            stage.mkdir()
            archive = stage / asset.name
            digest = hashlib.sha256()
            with requests.get(asset.url, stream=True, timeout=(20, 60)) as response, archive.open("wb") as output:
                response.raise_for_status()
                downloaded = 0
                for chunk in response.iter_content(256 * 1024):
                    downloaded += len(chunk)
                    if downloaded > asset.size:
                        raise UpdateError("Release download exceeds its declared size")
                    output.write(chunk)
                    digest.update(chunk)
                    self.set_state(downloaded=downloaded)
            self.set_state(phase="verifying")
            if downloaded != asset.size or digest.hexdigest() != asset.digest:
                raise UpdateError("Release download failed SHA256 verification; the installed application was not changed")
            self.set_state(phase="extracting")
            payload = stage / "payload"
            files = unpack_release(archive, payload, asset, available.executable.name)
            helper = stage / "helper"
            helper.mkdir()
            shutil.copy2(sys.executable, helper / ("galtransl_backend.exe" if os.name == "nt" else "galtransl_backend"))
            internal = Path(sys.executable).parent / "_internal"
            if internal.is_dir():
                shutil.copytree(internal, helper / "_internal")
            plan = UpdatePlan(str(available.root), str(available.executable), str(payload), preserve_dict,
                              str(get_active_dict_dir()), files, str(get_user_data_dir() / "update-result.json"))
            (stage / "plan.json").write_text(json.dumps(asdict(plan)), encoding="utf-8")
            archive.unlink()
            self.set_state(phase="ready")
        except (UpdateError, requests.RequestException, OSError, ValueError, zipfile.BadZipFile, tarfile.TarError) as exc:
            if stage.resolve().parent == Path(tempfile.gettempdir()).resolve():
                shutil.rmtree(stage, ignore_errors=True)
            self.set_state(phase="failed", error=str(exc))


APPLICATION_UPDATE = UpdateManager()


def route_update(handler, method: str, current_version: str) -> bool:
    path = urlparse(handler.path).path
    if not path.startswith("/api/update/"):
        return False
    raw_origin = handler.headers.get("Origin", "")
    origin = urlparse(raw_origin)
    if not ipaddress.ip_address(handler.client_address[0]).is_loopback or (
        raw_origin and (origin.scheme not in {"http", "https", "tauri"} or origin.hostname not in {"localhost", "127.0.0.1", "::1", "tauri.localhost"})):
        handler._send_json({"error": "Application updates are only available to the local desktop client"}, status=HTTPStatus.FORBIDDEN)
        return True
    try:
        if method == "GET" and path == "/api/update/status":
            handler._send_json(APPLICATION_UPDATE.status())
            return True
        if method == "POST" and path == "/api/update/start":
            length = int(handler.headers.get("Content-Length", "0"))
            if handler.headers.get("Content-Type", "").split(";", 1)[0] != "application/json" or not 0 < length <= 1024:
                raise ValueError("Expected a small JSON update request")
            payload = handler._read_json_body()
            if not isinstance(payload, dict) or type(payload.get("preserve_dict")) is not bool:
                raise ValueError("preserve_dict must be a boolean")
            handler._send_json(APPLICATION_UPDATE.start(current_version, payload["preserve_dict"]))
            return True
        handler._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
    except (UpdateError, OSError, ValueError) as exc:
        handler._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
    return True
