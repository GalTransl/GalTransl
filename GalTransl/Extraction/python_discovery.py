from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from typing import TypedDict

from GalTransl.RuntimePaths import get_resource_root

from .subprocess_env import external_environment, external_program


class PythonInterpreter(TypedDict):
    executable: str
    args: list[str]
    version: str
    architecture: int


def discover_pythons(configured: str = "") -> list[PythonInterpreter]:
    paths = [shutil.which(configured) if configured else None,
             sys.executable if not getattr(sys, "frozen", False) else None,
             *_python_paths(), *_registered_pythons()]
    executables = list(dict.fromkeys(os.path.abspath(path) for path in paths
        if path and Path(path).is_file() and "windowsapps" not in path.casefold()))
    with external_program(), ThreadPoolExecutor(max_workers=4) as workers:
        inspected = [python for python in workers.map(_inspect_python, executables) if python]
    unique = {}
    for python in inspected:
        executable = Path(python["executable"])
        root = executable.parent.parent if executable.parent.name.casefold() in ["scripts", "bin"] else executable.parent
        # Preserve virtual environments even when their executable links to a system Python.
        identity = root if (root / "pyvenv.cfg").is_file() else executable.resolve()
        unique.setdefault(os.path.normcase(str(identity)), python)
    return list(unique.values())


def _python_paths() -> list[str]:
    home = Path.home()
    root = get_resource_root()
    path_directories = list(dict.fromkeys(Path(directory) for directory in os.get_exec_path() if directory))
    search = [*path_directories,
              home / ".pyenv" / "versions", home / ".pyenv" / "pyenv-win" / "versions",
              home / ".virtualenvs", home / ".local" / "share" / "virtualenvs",
              home / ".cache" / "pypoetry" / "virtualenvs"]
    if os.name == "nt":
        search.extend(Path(os.environ[variable]) / suffix for variable, suffix in [
            ("LOCALAPPDATA", "Programs/Python"), ("LOCALAPPDATA", "uv/python"),
            ("APPDATA", "uv/python"), ("LOCALAPPDATA", "pypoetry/Cache/virtualenvs"),
            ("ProgramFiles", ""), ("ProgramFiles(x86)", ""),
        ] if os.environ.get(variable))
        search.append(Path(os.environ.get("SystemDrive", "C:")) / "/")
    if os.name != "nt":
        search.extend(Path(directory) for directory in ["/usr/bin", "/usr/local/bin", "/opt/homebrew/bin",
            "/Library/Frameworks/Python.framework/Versions", "/usr/local/opt", "/opt/homebrew/opt"])
        search.append(home / ".local" / "share" / "uv" / "python")
    environments = [*(Path(os.environ[variable]) for variable in ["VIRTUAL_ENV", "CONDA_PREFIX"]
                      if os.environ.get(variable)),
                    *(directory for pattern in [".venv*", "venv", "env"] for directory in root.glob(pattern))]
    conda_roots = [home / name for name in ["miniconda3", "anaconda3", "miniforge3", "mambaforge"]]
    conda_roots.extend(Path(os.environ["ProgramData"]) / name for name in ["miniconda3", "anaconda3"]
                       if os.environ.get("ProgramData"))
    environments.extend(conda_roots)
    environments.extend(directory for conda in conda_roots for directory in (conda / "envs").glob("*"))
    with suppress(OSError):
        environments.extend(Path(line.strip()) for line in (home / ".conda" / "environments.txt").read_text(
            encoding="utf-8").splitlines() if line.strip())
    candidates = []
    for directory in dict.fromkeys(search):
        with suppress(OSError):
            for entry in directory.iterdir():
                if re.fullmatch(r"(?:python|pypy)(?:\d+(?:\.\d+)*)?(?:\.exe)?", entry.name, re.I) and entry.is_file():
                    candidates.append(str(entry))
                if directory not in path_directories and entry.is_dir():
                    environments.append(entry)
    candidates.extend(str(directory / relative) for directory in dict.fromkeys(environments)
                      for relative in ["python.exe", "Scripts/python.exe", "bin/python", "bin/python3"])
    return candidates


def _registered_pythons() -> list[str]:
    if os.name != "nt":
        return []
    import winreg
    candidates = []
    launcher = shutil.which("py")
    if launcher:
        with external_program(), suppress(OSError, subprocess.TimeoutExpired):
            result = subprocess.run([launcher, "-0p"], capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=3, env=external_environment(),
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            candidates.extend(match.group(0).strip() for line in result.stdout.splitlines()
                              if (match := re.search(r"[A-Za-z]:[/\\].+\.exe\s*$", line)))
    for hive in [winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE]:
        for view in [winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY]:
            with suppress(OSError), winreg.OpenKey(hive, r"SOFTWARE\Python", access=winreg.KEY_READ | view) as key:
                for company in [winreg.EnumKey(key, index) for index in range(winreg.QueryInfoKey(key)[0])]:
                    with winreg.OpenKey(key, company) as company_key:
                        for tag in [winreg.EnumKey(company_key, index) for index in range(winreg.QueryInfoKey(company_key)[0])]:
                            with suppress(OSError), winreg.OpenKey(company_key, tag + r"\InstallPath") as install:
                                with suppress(OSError):
                                    candidates.append(winreg.QueryValueEx(install, "ExecutablePath")[0])
                                with suppress(OSError):
                                    candidates.append(str(Path(winreg.QueryValueEx(install, "")[0]) / "python.exe"))
    return candidates


def _inspect_python(executable: str) -> PythonInterpreter | None:
    code = "import json, struct, sys; print(json.dumps({'executable': sys.executable, 'version': '.'.join(map(str, sys.version_info[:3])), 'architecture': struct.calcsize('P') * 8}))"
    with suppress(OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        result = subprocess.run([executable, "-I", "-S", "-c", code], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=3, env=external_environment(),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if result.returncode:
            return None
        metadata = json.loads(result.stdout)
        if not isinstance(metadata, dict) or not all(isinstance(metadata.get(key), str)
            for key in ["executable", "version"]) or metadata.get("architecture") not in [32, 64]:
            return None
        return {"executable": metadata["executable"], "args": [], "version": metadata["version"],
                "architecture": metadata["architecture"]}
    return None
