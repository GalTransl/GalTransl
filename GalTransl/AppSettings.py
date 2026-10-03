from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from GalTransl.RuntimePaths import get_app_settings_path, get_legacy_app_settings_path


DEFAULT_APP_SETTINGS: dict[str, Any] = {
    "printTranslationLogInTerminal": True,
    "maxConcurrentJobs": 4,
}

# Kept for callers that imported the old constant. Runtime reads and writes use
# get_app_settings_path() so XDG/environment overrides remain effective.
_SETTINGS_PATH = str(get_app_settings_path())


def _normalize_settings(data: dict[str, Any] | None) -> dict[str, Any]:
    source = data or {}
    return {
        "printTranslationLogInTerminal": bool(
            source.get(
                "printTranslationLogInTerminal",
                DEFAULT_APP_SETTINGS["printTranslationLogInTerminal"],
            )
        ),
        "maxConcurrentJobs": max(1, int(
            source.get(
                "maxConcurrentJobs",
                DEFAULT_APP_SETTINGS["maxConcurrentJobs"],
            )
        )),
    }


def _existing_settings_path() -> Path:
    settings_path = get_app_settings_path()
    if settings_path.is_file():
        return settings_path
    legacy_path = get_legacy_app_settings_path()
    if legacy_path.is_file():
        return legacy_path
    return settings_path


def load_app_settings() -> dict[str, Any]:
    settings_path = _existing_settings_path()
    if not settings_path.is_file():
        return dict(DEFAULT_APP_SETTINGS)
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return dict(DEFAULT_APP_SETTINGS)
        return _normalize_settings(data)
    except Exception:
        return dict(DEFAULT_APP_SETTINGS)


def save_app_settings(settings: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_settings(settings)
    settings_path = get_app_settings_path()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    # Each backend instance owns its temporary file; the last completed save
    # replaces the destination atomically without consuming another writer's file.
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=settings_path.parent,
            prefix=f".{settings_path.name}.", suffix=".tmp", delete=False,
        ) as f:
            tmp_path = Path(f.name)
            json.dump(normalized, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, settings_path)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
    return normalized
