"""Include non-GUI standard-library modules used by dynamically executed scripts.

This hook runs inside the build interpreter, so native modules and the Python
license match the interpreter frozen into the backend. Third-party packages are
still limited to the backend's dependencies; this is not a pip installation.
"""
import os
import sys
import sysconfig
from pathlib import Path

excluded = {"test", "tests", "__pycache__", "site-packages", "dist-packages",
            "idlelib", "tkinter", "_tkinter", "turtledemo", "turtle", "ensurepip", "antigravity", "this"}
modules = set(sys.stdlib_module_names) - excluded - {"__main__"}
stdlib = Path(sysconfig.get_path("stdlib"))
for directory, children, files in os.walk(stdlib):
    children[:] = [name for name in children if name not in excluded and not name.startswith(".")]
    for filename in files:
        if not filename.endswith(".py"):
            continue
        relative = (Path(directory) / filename).relative_to(stdlib).with_suffix("")
        parts = list(relative.parts)
        if parts[-1] == "__init__":
            parts.pop()
        if parts and all(part.isidentifier() and part not in excluded for part in parts):
            modules.add(".".join(parts))
hiddenimports = sorted(modules)

datas = []
for license_file in (Path(sys.base_prefix) / "LICENSE.txt", stdlib / "LICENSE.txt",
                     Path(sys.base_prefix) / "LICENSE", stdlib / "LICENSE"):
    if license_file.is_file():
        datas.append((str(license_file), "python-license"))
        break
if not datas:
    raise RuntimeError("Cannot bundle Python without its license file")
