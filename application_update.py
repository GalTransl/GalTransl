"""Standalone installer entry point; runs outside the installation being updated."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

RELEASE_API = "https://api.github.com/repos/GalTransl/GalTransl/releases/latest"


@dataclass(frozen=True)
class UpdatePlan:
    install_dir: str
    executable: str
    payload_dir: str
    preserve_dict: bool
    dict_dir: str
    files: list[str]
    result_file: str


def read_plan(path: Path) -> UpdatePlan:
    value = json.loads(path.read_text(encoding="utf-8"))
    strings = ("install_dir", "executable", "payload_dir", "dict_dir", "result_file")
    if not isinstance(value, dict) or any(not isinstance(value.get(key), str) or not value[key] for key in strings):
        raise ValueError("Invalid application update plan")
    if type(value.get("preserve_dict")) is not bool or not isinstance(value.get("files"), list):
        raise ValueError("Invalid application update options")
    if not value["files"] or any(not isinstance(name, str) or not name for name in value["files"]):
        raise ValueError("Application update has no files")
    root = Path(value["install_dir"]).resolve(strict=True)
    payload = Path(value["payload_dir"]).resolve(strict=True)
    if not Path(value["executable"]).resolve().is_relative_to(root) or payload != path.parent.resolve() / "payload":
        raise ValueError("Application update paths do not match the installation")
    for name in value["files"]:
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name:
            raise ValueError(f"Invalid application update path: {name}")
        source = payload.joinpath(*relative.parts)
        target = root.joinpath(*relative.parts)
        if not source.is_file() or not source.resolve().is_relative_to(payload) or not target.resolve().is_relative_to(root):
            raise ValueError(f"Application update path leaves its directory: {name}")
    return UpdatePlan(**{key: value[key] for key in (*strings, "preserve_dict", "files")})


def apply_update(plan: UpdatePlan, update_id: str) -> None:
    root = Path(plan.install_dir)
    payload = Path(plan.payload_dir)
    dictionary = Path(plan.dict_dir)
    operations = [(payload / name, root / name) for name in plan.files
                  if not (plan.preserve_dict and PurePosixPath(name).parts[0].casefold() == "dict")]
    bundled_dict = payload / "Dict"
    if not plan.preserve_dict and dictionary.resolve() != (root / bundled_dict.relative_to(payload)).resolve():
        operations.extend((source, dictionary / source.relative_to(bundled_dict))
                          for source in bundled_dict.rglob("*") if source.is_file())
    if any(not target.resolve().is_relative_to(root.resolve()) and not target.resolve().is_relative_to(dictionary.resolve())
           for _, target in operations):
        raise ValueError("An update destination leaves its application or dictionary directory")
    changed: list[tuple[Path, Path, bool]] = []
    try:
        for source, target in operations:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{update_id}.tmp")
            backup = target.with_name(f".{target.name}.{update_id}.bak")
            existed = target.exists()
            try:
                if existed:
                    shutil.copy2(target, backup)
                shutil.copy2(source, temporary)
                os.replace(temporary, target)
            except OSError:
                temporary.unlink(missing_ok=True)
                backup.unlink(missing_ok=True)
                raise
            changed.append((target, backup, existed))
    except OSError:
        for target, backup, existed in reversed(changed):
            target.with_name(f".{target.name}.{update_id}.tmp").unlink(missing_ok=True)
            if existed and backup.exists():
                os.replace(backup, target)
            if not existed:
                target.unlink(missing_ok=True)
        raise
    for _, backup, _ in changed:
        backup.unlink(missing_ok=True)


def wait_for_exit(pid: int) -> None:
    if sys.platform == "win32":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:
                return
            raise OSError("Could not wait for the desktop process to exit")
        try:
            if kernel.WaitForSingleObject(handle, 90_000) != 0:
                raise TimeoutError("Desktop process did not exit; no update files were replaced")
        finally:
            kernel.CloseHandle(handle)
        return
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    raise TimeoutError("Desktop process did not exit; no update files were replaced")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--parent-pid", type=int, required=True)
    args = parser.parse_args(argv)
    plan = read_plan(args.plan)
    update_id = args.plan.parent.name.removeprefix("galtransl-update-")
    (args.plan.parent / "installer-ready").write_text("ready", encoding="utf-8")
    try:
        wait_for_exit(args.parent_pid)
        apply_update(plan, update_id)
        result = {"phase": "completed", "error": "", "id": update_id}
    except (OSError, ValueError) as exc:
        result = {"phase": "failed", "error": str(exc), "id": update_id}
    result_file = Path(plan.result_file)
    result_file.parent.mkdir(parents=True, exist_ok=True)
    result_file.write_text(json.dumps(result), encoding="utf-8")
    if result["phase"] == "completed":
        # The installer runs from helper/, so unpacked payload files are free.
        shutil.rmtree(Path(plan.payload_dir), ignore_errors=True)
    if result["phase"] == "failed":
        print(result["error"], file=sys.stderr)
    if Path(plan.executable).is_file():
        subprocess.Popen([plan.executable], cwd=plan.install_dir, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=0x08000000 if os.name == "nt" else 0)
    return 0 if result["phase"] == "completed" else 1
