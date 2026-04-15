from __future__ import annotations

import json
import os
from typing import Any


DEFAULT_APP_SETTINGS: dict[str, Any] = {
    "printTranslationLogInTerminal": True,
    "maxConcurrentJobs": 4,
}

_SETTINGS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "app_settings.json",
)


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


def load_app_settings() -> dict[str, Any]:
    if not os.path.isfile(_SETTINGS_PATH):
        return dict(DEFAULT_APP_SETTINGS)
    try:
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return dict(DEFAULT_APP_SETTINGS)
        return _normalize_settings(data)
    except Exception:
        return dict(DEFAULT_APP_SETTINGS)


def save_app_settings(settings: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_settings(settings)
    tmp_path = _SETTINGS_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, _SETTINGS_PATH)
    return normalized
