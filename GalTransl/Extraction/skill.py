"""Explicit Skill downloads; startup only inspects local resources."""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import TypedDict
from urllib.error import URLError
from urllib.request import Request, urlopen

from .subprocess_env import external_environment, external_program

SKILL_NAME = "galtransl-galgamereverse-skill"
SOURCE = "https://github.com/GalTransl/GalTransl-GalGameReverse-Skill"
LATEST_URL = "https://api.github.com/repos/GalTransl/GalTransl-GalGameReverse-Skill/commits/main"
ARCHIVE_URL = "https://codeload.github.com/GalTransl/GalTransl-GalGameReverse-Skill/zip/{commit}"
RECEIPT = ".galtransl-skill.json"


class SkillDownloadError(RuntimeError):
    pass


class SkillStatus(TypedDict):
    installed: bool
    current_commit: str
    latest_commit: str
    update_available: bool


def skill_status(directory: Path) -> SkillStatus:
    installed = (directory / "SKILL.md").is_file()
    receipt = directory / RECEIPT
    manifest = directory.parent / "sources.json"
    version_file = receipt if receipt.is_file() else manifest
    version_data = json.loads(version_file.read_text(encoding="utf-8")) if version_file.is_file() else {}
    if not isinstance(version_data, dict):
        raise ValueError("Invalid Skill version metadata")
    metadata = version_data if version_file == receipt else version_data.get(SKILL_NAME, {})
    if not isinstance(metadata, dict):
        raise ValueError("Invalid Skill version metadata")
    commit = metadata.get("commit", "")
    git = shutil.which("git") if installed and (directory / ".git").exists() else None
    if git:
        with external_program():
            result = subprocess.run([git, "-C", str(directory), "rev-parse", "HEAD"],
                                    capture_output=True, encoding="utf-8", timeout=10,
                                    env=external_environment(),
                                    creationflags=0x08000000 if os.name == "nt" else 0)
        commit = result.stdout.strip() if result.returncode == 0 else ""
    return {"installed": installed, "current_commit": commit if installed and isinstance(commit, str) else "",
            "latest_commit": "", "update_available": False}


def check_skill(directory: Path) -> SkillStatus:
    status = skill_status(directory)
    commit = latest_commit()
    return {**status, "latest_commit": commit,
            "update_available": not status["installed"] or status["current_commit"] != commit}


def install_skill(data: Path) -> Path:
    commit = latest_commit()
    versions = data / "skills" / SKILL_NAME
    versions.mkdir(parents=True, exist_ok=True)
    destination = versions / commit
    # Each version has its own directory, preserving bundled/custom files and
    # the previously selected version if a download or extraction fails.
    if destination.exists():
        if not (destination / "SKILL.md").is_file() or not (destination / "LICENSE").is_file():
            raise SkillDownloadError(f"Incomplete downloaded Skill: {destination}")
        return destination
    archive = request_bytes(ARCHIVE_URL.format(commit=commit), 128 * 1024 * 1024)
    with tempfile.TemporaryDirectory(prefix=".skill-download-", dir=versions) as temporary:
        stage = Path(temporary) / "skill"
        stage.mkdir()
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                files = bundle.infolist()
                if sum(entry.file_size for entry in files) > 256 * 1024 * 1024:
                    raise SkillDownloadError("Skill archive exceeds its size budget")
                for entry in files:
                    relative = PurePosixPath(entry.filename)
                    if relative.is_absolute() or ".." in relative.parts or "\\" in entry.filename or ":" in entry.filename or stat.S_ISLNK(entry.external_attr >> 16):
                        raise SkillDownloadError(f"Invalid Skill archive path: {entry.filename}")
                    if entry.is_dir():
                        continue
                    if len(relative.parts) < 2:
                        raise SkillDownloadError("Skill archive is missing its root directory")
                    target = stage.joinpath(*relative.parts[1:])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(entry) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
        except zipfile.BadZipFile as exc:
            raise SkillDownloadError("GitHub returned an invalid Skill archive") from exc
        if not (stage / "SKILL.md").is_file() or not (stage / "LICENSE").is_file():
            raise SkillDownloadError("Downloaded Skill is missing SKILL.md or LICENSE")
        (stage / RECEIPT).write_text(json.dumps({"source": SOURCE, "commit": commit}), encoding="utf-8")
        stage.rename(destination)
    return destination


def latest_commit() -> str:
    content = request_bytes(LATEST_URL, 1024 * 1024)
    try:
        metadata = json.loads(content)
    except ValueError as exc:
        raise SkillDownloadError("GitHub returned invalid Skill version metadata") from exc
    commit = metadata.get("sha") if isinstance(metadata, dict) else None
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise SkillDownloadError("GitHub returned an invalid Skill version")
    return commit


def request_bytes(url: str, maximum: int) -> bytes:
    request = Request(url, headers={"User-Agent": "GalTransl-Skill", "Accept": "application/vnd.github+json"})
    try:
        with urlopen(request, timeout=60) as response:
            content = response.read(maximum + 1)
    except (URLError, TimeoutError) as exc:
        raise SkillDownloadError(f"Could not download Skill from GitHub: {exc}") from exc
    if len(content) > maximum:
        raise SkillDownloadError("Skill download exceeds its size budget")
    return content
