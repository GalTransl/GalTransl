from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path

APP_DIR_NAME = "GalTransl"
RESOURCE_DIR_ENV = "GALTRANSL_RESOURCE_DIR"
DATA_DIR_ENV = "GALTRANSL_DATA_DIR"
CONFIG_DIR_ENV = "XDG_CONFIG_HOME"
DATA_ROOT_ENV = "XDG_DATA_HOME"
BUNDLED_DICT_SEED_MARKER = ".bundled_dict_seed"


def _source_root() -> Path:
    return Path(__file__).resolve().parent.parent


def get_resource_root() -> Path:
    """Return the directory containing bundled runtime resources.

    Packaged builds normally set ``GALTRANSL_RESOURCE_DIR``.  When it is not
    set, derive a sensible default instead of relying on the process working
    directory so launching from a desktop shortcut or another folder works.
    """
    override = os.environ.get(RESOURCE_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve()

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates = [exe_dir, exe_dir.parent]
        for candidate in candidates:
            if any((candidate / name).exists() for name in ("plugins", "Dict", "res")):
                return candidate
        if exe_dir.name == "backend":
            return exe_dir.parent
        return exe_dir

    return _source_root()


def get_resource_path(*parts: str | os.PathLike[str]) -> Path:
    return get_resource_root().joinpath(*parts)


def get_plugins_dir() -> Path:
    return get_resource_path("plugins")


def get_dict_dir() -> Path:
    return get_resource_path("Dict")


def get_translation_guidelines_dir() -> Path:
    return get_resource_path("translation_guidelines")


def get_active_translation_guidelines_dir() -> Path:
    if sys.platform == "win32" and not os.environ.get(DATA_DIR_ENV, "").strip():
        return get_translation_guidelines_dir()
    return _seed_bundled_files(
        get_translation_guidelines_dir(),
        get_user_data_dir() / "translation_guidelines",
        ".bundled_guidelines_seed",
    )


def get_res_dir() -> Path:
    return get_resource_path("res")


def _platform_config_root() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", "").strip()
        return Path(base).expanduser() if base else Path.home() / "AppData" / "Roaming"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    base = os.environ.get(CONFIG_DIR_ENV, "").strip()
    return Path(base).expanduser() if base else Path.home() / ".config"


def _platform_data_root() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA", "").strip()
        return Path(base).expanduser() if base else Path.home() / "AppData" / "Local"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    base = os.environ.get(DATA_ROOT_ENV, "").strip()
    return Path(base).expanduser() if base else Path.home() / ".local" / "share"


def get_user_config_dir() -> Path:
    return _platform_config_root() / APP_DIR_NAME


def get_user_data_dir() -> Path:
    override = os.environ.get(DATA_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return _platform_data_root() / APP_DIR_NAME


def get_user_dict_dir() -> Path:
    return get_user_data_dir() / "Dict"


def _bundled_dict_signature(bundled_dir: Path) -> str:
    if not bundled_dir.is_dir():
        return "missing"
    digest = hashlib.sha256()
    for source in sorted(bundled_dir.iterdir(), key=lambda item: item.name):
        if not source.is_file():
            continue
        digest.update(source.name.encode("utf-8"))
        digest.update(source.read_bytes())
    return digest.hexdigest()


def get_active_dict_dir() -> Path:
    """Return the writable common dictionary directory.

    Bundled dictionaries are copied as seeds when their file set changes.
    Existing user files are never overwritten.
    """
    if sys.platform == "win32" and not os.environ.get(DATA_DIR_ENV, "").strip():
        return get_dict_dir()
    return _seed_bundled_files(get_dict_dir(), get_user_dict_dir(), BUNDLED_DICT_SEED_MARKER)


def _seed_bundled_files(bundled_dir: Path, user_dir: Path, marker_name: str) -> Path:
    user_dir.mkdir(parents=True, exist_ok=True)
    if bundled_dir.resolve() == user_dir.resolve():
        return user_dir
    signature = _bundled_dict_signature(bundled_dir)
    seed_marker = user_dir / marker_name
    if seed_marker.is_file() and seed_marker.read_text(encoding="utf-8").strip() == signature:
        return user_dir

    if bundled_dir.is_dir():
        for source in bundled_dir.iterdir():
            if not source.is_file():
                continue
            destination = user_dir / source.name
            if not destination.exists():
                shutil.copy2(source, destination)
    seed_marker.write_text(signature + "\n", encoding="utf-8")
    return user_dir


def resolve_dict_dir(dict_dir: str | os.PathLike[str] | None) -> Path:
    configured = Path(dict_dir or ".").expanduser()
    if configured.is_absolute():
        return configured
    if configured.parts and configured.parts[0] == "Dict":
        return get_active_dict_dir().parent / configured
    return get_resource_path(configured)


def get_app_settings_path() -> Path:
    override = os.environ.get("GALTRANSL_APP_SETTINGS_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        return get_legacy_app_settings_path()
    return get_user_config_dir() / "app_settings.json"


def get_legacy_app_settings_path() -> Path:
    return get_resource_path("app_settings.json")
