"""Discover actual interpreters and preserve virtual-environment selections."""
import os
import sys
import venv
from pathlib import Path

import pytest

from GalTransl.Extraction import environment
from GalTransl.Extraction import python_discovery


@pytest.fixture
def interpreters(tmp_path, monkeypatch):
    root = tmp_path / "application"
    root.mkdir()
    first = root / ".venv-first"
    second = root / ".venv-second"
    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(first)
    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(second)
    relative = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    monkeypatch.setattr(python_discovery, "get_resource_root", lambda: root)
    return first / relative, second / relative


def test_distinct_virtual_environments_are_discovered_and_configured_python_is_first(interpreters):
    first, second = interpreters
    found = python_discovery.discover_pythons(str(second))
    paths = [os.path.normcase(python["executable"]) for python in found]
    assert paths[0] == os.path.normcase(str(second))
    assert os.path.normcase(str(first)) in paths
    assert paths.count(os.path.normcase(str(second))) == 1
    assert found[0]["version"] == ".".join(map(str, sys.version_info[:3]))
    assert found[0]["architecture"] in [32, 64]
    assert found[0]["args"] == []


def test_saving_virtual_environment_keeps_its_executable_path(interpreters, tmp_path, monkeypatch):
    first, _ = interpreters
    monkeypatch.setattr(environment, "data_root", lambda: tmp_path / "data")
    saved = environment.save_settings({"python_path": str(first)})
    assert saved["python_path"] == str(first)
    assert environment.load_settings()["python_path"] == str(first)


def test_invalid_interpreter_is_not_offered(tmp_path):
    broken = tmp_path / "python.exe"
    broken.write_text("This is not a Python executable", encoding="utf-8")
    assert python_discovery._inspect_python(str(broken)) is None


def test_conda_environment_list_is_included(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    conda = tmp_path / ".conda"
    conda.mkdir()
    custom = tmp_path / "custom-environment"
    (conda / "environments.txt").write_text(str(custom) + "\n", encoding="utf-8")
    assert str(custom / "python.exe") in python_discovery._python_paths()
    assert str(custom / "bin/python") in python_discovery._python_paths()
