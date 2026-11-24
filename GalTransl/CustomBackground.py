from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from threading import RLock
from uuid import uuid4

from GalTransl.RuntimePaths import DATA_DIR_ENV, get_resource_root, get_user_data_dir

_LOCK = RLock()
_DEFAULT = {"imageVersion": "", "imageName": "", "opacity": 45, "surfaceOpacity": 25}
# Files without _STORED_FORMAT held the defaults of their release alongside real
# choices; values equal to those old defaults are read as unset so today's defaults
# apply. Keep this table as it is: it describes old files, not today's defaults.
_LEGACY_DEFAULTS = {"opacity": 35, "surfaceOpacity": 33}
_FORMAT_KEY = "storedFormat"
_STORED_FORMAT = 2  # only values that differ from _DEFAULT are written
_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp", "image/avif", "image/svg+xml", "image/x-icon"}


def background_directory() -> Path:
    root = get_user_data_dir() if os.environ.get(DATA_DIR_ENV, "").strip() else get_resource_root()
    return root / "desktop-background"


def _read() -> dict:
    path = background_directory() / "settings.json"
    if not path.is_file():
        return dict(_DEFAULT)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("invalid background settings")
    version = str(value.get("imageVersion", ""))
    if version and not re.fullmatch(r"[0-9a-f]{32}", version):
        raise ValueError("invalid background image version")
    if value.get(_FORMAT_KEY) != _STORED_FORMAT:
        value = {key: item for key, item in value.items()
                 if key not in _LEGACY_DEFAULTS or item != _LEGACY_DEFAULTS[key]}
    return {**_DEFAULT, **value}


def _write(value: dict) -> None:
    # Persist only what differs from _DEFAULT (_read fills the rest back in), so a
    # later change to a default reaches users who never moved that slider. The
    # file itself is still written: its existence marks the localStorage migration done.
    value = {key: item for key, item in value.items() if key not in _DEFAULT or item != _DEFAULT[key]}
    value[_FORMAT_KEY] = _STORED_FORMAT
    directory = background_directory()
    directory.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False)
        os.replace(temporary, directory / "settings.json")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _public(value: dict) -> dict:
    return {**{key: value[key] for key in _DEFAULT}, "configured": (background_directory() / "settings.json").is_file()}


def load_custom_background() -> dict:
    with _LOCK:
        return _public(_read())


def _opacity(value, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, int(value)))


def save_custom_background_settings(settings: dict) -> dict:
    with _LOCK:
        value = _read()
        value["opacity"] = _opacity(settings.get("opacity", value["opacity"]), 0, 80)
        value["surfaceOpacity"] = _opacity(settings.get("surfaceOpacity", value["surfaceOpacity"]), 18, 92)
        _write(value)
        return _public(value)


def _remove_image(version: str) -> None:
    if version:
        try:
            (background_directory() / f"image-{version}").unlink(missing_ok=True)
        except OSError:
            pass  # The committed settings remain authoritative if old-file cleanup fails.


def save_custom_background_image(data: bytes, name: str, content_type: str, settings: dict | None = None) -> dict:
    if not data or content_type not in _IMAGE_TYPES:
        raise ValueError("unsupported or empty background image")
    with _LOCK:
        previous = _read()
        value = dict(previous)
        version = uuid4().hex
        image_path = background_directory() / f"image-{version}"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            image_path.write_bytes(data)
            value.update(imageVersion=version, imageName=name.replace("\\", "/").rsplit("/", 1)[-1], imageType=content_type)
            if settings:
                value["opacity"] = _opacity(settings.get("opacity", value["opacity"]), 0, 80)
                value["surfaceOpacity"] = _opacity(settings.get("surfaceOpacity", value["surfaceOpacity"]), 18, 92)
            # Publish the new image only after its complete file is on disk.
            _write(value)
        except Exception:
            image_path.unlink(missing_ok=True)
            raise
        _remove_image(previous["imageVersion"])
        return _public(value)


def read_custom_background_image(version: str) -> tuple[bytes, str]:
    with _LOCK:
        value = _read()
        if not version or version != value["imageVersion"]:
            raise FileNotFoundError("background image not found")
        return (background_directory() / f"image-{version}").read_bytes(), value["imageType"]


def clear_custom_background() -> dict:
    with _LOCK:
        previous = _read()
        # Clearing only drops the image; opacity choices stay for the next image.
        value = {**_DEFAULT, "opacity": previous["opacity"], "surfaceOpacity": previous["surfaceOpacity"]}
        _write(value)
        _remove_image(previous["imageVersion"])
        return _public(value)
