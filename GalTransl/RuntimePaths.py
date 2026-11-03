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
    if not os.environ.get(DATA_DIR_ENV, "").strip():
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
    return get_user_data_dir()


def get_legacy_user_data_dir() -> Path:
    return _platform_data_root() / APP_DIR_NAME


def get_user_data_dir() -> Path:
    override = os.environ.get(DATA_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    directory = Path.home() / ".galtransl"
    marker = directory / ".legacy_migration_v1"
    if not marker.exists():
        directory.mkdir(parents=True, exist_ok=True)
        legacy = get_legacy_user_data_dir()
        _copy_missing_tree(legacy / "agent_sessions", directory / "agent_sessions")
        _copy_missing_tree(legacy / "extraction" / "sessions", directory / "extraction" / "sessions")
        extraction_settings = directory / "extraction" / "settings.json"
        if (legacy / "extraction" / "settings.json").is_file() and not extraction_settings.exists():
            extraction_settings.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy / "extraction" / "settings.json", extraction_settings)
        _copy_missing_tree(get_resource_root() / "agent_sessions", directory / "agent_sessions")
        legacy_settings = [_platform_config_root() / APP_DIR_NAME / "app_settings.json", get_legacy_app_settings_path()]
        destination = directory / "app_settings.json"
        for source in legacy_settings:
            if source.is_file() and not destination.exists():
                shutil.copy2(source, destination)
        marker.touch()
    return directory


def _copy_missing_tree(source: Path, destination: Path) -> None:
    """Migrate/seed without overwriting user edits or traversing links."""
    if not source.is_dir() or source.is_symlink() or getattr(source.stat(), "st_file_attributes", 0) & 0x400:
        return
    destination.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or getattr(destination.stat(), "st_file_attributes", 0) & 0x400:
        raise ValueError(f"User data directory must not be a link: {destination}")
    for entry in source.iterdir():
        if entry.name in {".git", "__pycache__", "node_modules", ".bundled_skills_seed"} or entry.is_symlink():
            continue
        target = destination / entry.name
        if entry.is_dir():
            _copy_missing_tree(entry, target)
            continue
        if entry.is_file() and not target.exists():
            shutil.copy2(entry, target)


def get_skills_dir() -> Path:
    return get_resource_path("skills")


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
    """Use program dictionaries unless the user data directory is overridden.

    An explicit override is seeded when the bundled file set changes, without
    overwriting existing user files.
    """
    if not os.environ.get(DATA_DIR_ENV, "").strip():
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
    return get_user_config_dir() / "app_settings.json"


def get_legacy_app_settings_path() -> Path:
    return get_resource_path("app_settings.json")
