from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from GalTransl.RuntimePaths import get_resource_root, get_user_data_dir

from .skill import skill_status


def data_root() -> Path:
    return get_user_data_dir() / "extraction"


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".extract-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def load_settings() -> dict[str, str]:
    root = get_resource_root()
    defaults = {
        "skill_dir": str(root / "skills" / "galtransl-galgamereverse-skill"),
        "python_path": "",  # Optional system Python; run_python defaults to bundled.
    }
    path = data_root() / "settings.json"
    if path.is_file():
        saved = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(saved, dict):
            raise ValueError("Invalid extraction settings")
        defaults.update({key: str(saved[key]) for key in defaults if key in saved})
        if saved.get("runtime_preferences_version") != 3:
            # Old releases stored PATH commands as defaults. Preserve explicit
            # custom paths while migrating automatic selections to bundled tools.
            if defaults["python_path"] in {"python", "python3", "py"}:
                defaults["python_path"] = ""
            if Path(defaults["skill_dir"]) == root.parent / "GalTransl-GalGameReverse-Skill":
                defaults["skill_dir"] = str(root / "skills" / "galtransl-galgamereverse-skill")
    return defaults


def save_settings(payload: dict[str, Any]) -> dict[str, str]:
    settings = load_settings()
    for key in settings:
        if key in payload:
            value = str(payload[key]).strip()
            if "\0" in value:
                raise ValueError(f"{key} is required")
            if key == "skill_dir":
                if not value:
                    value = str(get_resource_root() / "skills" / "galtransl-galgamereverse-skill")
                value = str(Path(value).expanduser().resolve(strict=True))
                if not (Path(value) / "SKILL.md").is_file():
                    raise ValueError("SKILL.md was not found in the skill directory")
            elif value:
                resolved = shutil.which(value)
                if not resolved or not Path(resolved).is_file():
                    raise ValueError(f"Executable not found: {key}")
                value = str(Path(resolved).resolve())
            settings[key] = value
    atomic_json(data_root() / "settings.json", {**settings, "runtime_preferences_version": 3})
    return settings


def builtin_python() -> dict[str, Any]:
    frozen = bool(getattr(sys, "frozen", False))
    args = [] if frozen else [str(get_resource_root() / "run_backend.py")]
    return {"executable": sys.executable, "args": [*args, "--extraction-python"],
            "source": "bundled" if frozen else "development"}


def python_runner_script() -> str:
    if getattr(sys, "frozen", False):
        return str(get_resource_root() / "extraction-python" / "runner.py")
    return str(get_resource_root() / "extraction_python.py")


def system_pythons(configured: str = "") -> list[dict[str, Any]]:
    candidates = []
    for command, args in [(configured, []), ("python3", []), ("python", []), ("py", ["-3"])]:
        executable = shutil.which(command) if command else None
        if not executable or ("windowsapps" in executable.casefold() and command != configured):
            continue
        executable = str(Path(executable).resolve())
        if not any(candidate["executable"] == executable for candidate in candidates):
            candidates.append({"executable": executable, "args": args})
    return candidates


def environment() -> dict[str, Any]:
    settings = load_settings()
    problems = []
    if sys.version_info < (3, 11):
        problems.append("python_version")
    if not (Path(settings["skill_dir"]) / "SKILL.md").is_file():
        problems.append("skill_missing")
    if not Path(python_runner_script()).is_file():
        problems.append("python_runner_missing")
    return {"settings": settings, "python_version": f"Python {sys.version.split()[0]}",
            "skill": skill_status(Path(settings["skill_dir"])),
            "agent_engine": "galtransl", "builtin_python": builtin_python(),
            "python_runner_script": python_runner_script(),
            "system_pythons": system_pythons(settings["python_path"]),
            "ready": not problems, "problems": problems}


def checked_directory(raw: Any) -> Path:
    if not isinstance(raw, str) or not raw.strip() or "\0" in raw:
        raise ValueError("Directory is required")
    path = Path(raw).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise ValueError("Directory does not exist")
    return path


def workspace_path(workspace: Path, relative: str) -> Path:
    if "\0" in relative or Path(relative).is_absolute() or ":" in relative:
        raise ValueError("Expected a relative workspace path")
    root = workspace.resolve(strict=True)
    current = root
    for part in relative.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError("Path escapes the workspace")
        current = current / part
        if current.is_symlink() or (getattr(current.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("Links are not allowed")
    resolved = current.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ValueError("Path escapes the workspace")
    return resolved
