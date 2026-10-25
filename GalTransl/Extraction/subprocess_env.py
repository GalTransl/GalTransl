"""Launch external tools without inheriting PyInstaller's private DLL paths."""
from __future__ import annotations

import os
import sys
import threading
from contextlib import contextmanager

_spawn_lock = threading.RLock()


def external_environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if key.upper() in {
        "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "HOME", "USERPROFILE",
        "APPDATA", "LOCALAPPDATA", "LANG", "LC_ALL", "PATHEXT", "PROGRAMFILES", "PROGRAMFILES(X86)",
    }}
    if getattr(sys, "frozen", False) and os.environ.get("LD_LIBRARY_PATH_ORIG"):
        environment["LD_LIBRARY_PATH"] = os.environ["LD_LIBRARY_PATH_ORIG"]
    environment.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", NO_COLOR="1")
    return environment


@contextmanager
def external_program():
    with _spawn_lock:
        if os.name != "nt" or not getattr(sys, "frozen", False):
            yield
            return
        import ctypes
        kernel = ctypes.windll.kernel32
        size = kernel.GetDllDirectoryW(0, None)
        previous = ctypes.create_unicode_buffer(size + 1)
        kernel.GetDllDirectoryW(len(previous), previous)
        kernel.SetDllDirectoryW(None)
        try:
            yield
        finally:
            kernel.SetDllDirectoryW(previous.value or None)
