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
    "billionContextEnabled": False,
    "extractionBillionContextEnabled": False,
    "mnemonEnabled": True,
    "extractionMnemonEnabled": True,
    # off / success / all: write each translation request into <project>/transl_record/<template>.csv
    "requestRecordMode": "off",
}
_REQUEST_RECORD_MODES = ("off", "success", "all")

# Files written before _STORED_FORMAT existed held every setting, defaults included,
# so a value there may be the user's choice or just the default of that release.
# Values equal to the defaults of that last release are read as unset, letting the
# current defaults apply (e.g. billion-context, which was on then and is off now).
# Keep this table as it is: it describes old files, not today's defaults.
_LEGACY_DEFAULTS: dict[str, Any] = {
    "printTranslationLogInTerminal": True,
    "maxConcurrentJobs": 4,
    "billionContextEnabled": True,
    "extractionBillionContextEnabled": True,
    "mnemonEnabled": True,
    "extractionMnemonEnabled": True,
}
_FORMAT_KEY = "storedFormat"
_STORED_FORMAT = 2  # only settings that differ from the defaults are written

# Kept for callers that imported the old constant. Runtime reads and writes use
# get_app_settings_path() so user-home/environment overrides remain effective.
_SETTINGS_PATH = str(get_app_settings_path())


def _normalize_settings(data: dict[str, Any] | None) -> dict[str, Any]:
    source = data or {}
    record_mode = source.get("requestRecordMode", DEFAULT_APP_SETTINGS["requestRecordMode"])
    return {
        "requestRecordMode": record_mode if record_mode in _REQUEST_RECORD_MODES else "off",
        "mnemonEnabled": source.get("mnemonEnabled", DEFAULT_APP_SETTINGS["mnemonEnabled"]) is True,
        "extractionMnemonEnabled": source.get("extractionMnemonEnabled", DEFAULT_APP_SETTINGS["extractionMnemonEnabled"]) is True,
        "billionContextEnabled": source.get("billionContextEnabled", DEFAULT_APP_SETTINGS["billionContextEnabled"]) is True,
        # Existing installations used one switch for both agents.
        "extractionBillionContextEnabled": source.get(
            "extractionBillionContextEnabled", source.get("billionContextEnabled", DEFAULT_APP_SETTINGS["extractionBillionContextEnabled"])
        ) is True,
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
        if data.get(_FORMAT_KEY) != _STORED_FORMAT:
            data = {key: value for key, value in data.items()
                    if key not in _LEGACY_DEFAULTS or value != _LEGACY_DEFAULTS[key]}
        return _normalize_settings(data)
    except Exception:
        return dict(DEFAULT_APP_SETTINGS)


def _without_defaults(normalized: dict[str, Any]) -> dict[str, Any]:
    """Drop every key that loading would fill back with the same value.

    Only user changes are persisted, so a later change to a default reaches
    everyone who never touched that setting. A key is dropped only when it
    equals its default *and* reloading still yields the same settings: some
    keys fall back to another key (extractionBillionContextEnabled ->
    billionContextEnabled), and a non-default value kept only through that
    fallback would start following the other switch.
    """
    stored = dict(normalized)
    for key in list(stored):
        if stored[key] != DEFAULT_APP_SETTINGS.get(key):
            continue
        candidate = {name: value for name, value in stored.items() if name != key}
        if _normalize_settings(candidate) == normalized:
            stored = candidate
    return stored


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
            json.dump({**_without_defaults(normalized), _FORMAT_KEY: _STORED_FORMAT}, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, settings_path)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
    return normalized
