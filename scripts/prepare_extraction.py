"""Sync the latest extraction Skill for development/builds and export a snapshot."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "build" / "extraction-resources"
SKILL_NAME = "galtransl-galgamereverse-skill"


def git(directory, *args):
    result = subprocess.run(["git", "-C", str(directory), *args], check=True,
                            capture_output=True, encoding="utf-8", timeout=180,
                            creationflags=0x08000000 if os.name == "nt" else 0)
    return result.stdout


def prepare(root=ROOT, output=OUTPUT):
    if getattr(sys, "frozen", False):
        raise RuntimeError("Packaged applications use their bundled Skill without updating it")
    root, output = Path(root).resolve(), Path(output).absolute()
    manifest = json.loads((root / "skills" / "sources.json").read_text(encoding="utf-8"))
    source = manifest[SKILL_NAME]
    skill = root / "skills" / SKILL_NAME
    if not (skill / "SKILL.md").is_file():
        git(root, "submodule", "update", "--init", "--", f"skills/{SKILL_NAME}")
    if git(skill, "status", "--porcelain", "--untracked-files=no").strip():
        raise RuntimeError("Extraction Skill has local changes; preserve them before packaging")
    # Fetch the branch each time, even if this checkout already has SKILL.md.
    # Full ancestry is needed for a safe fast-forward from shallow CI checkouts.
    fetch_options = ["--unshallow"] if git(skill, "rev-parse", "--is-shallow-repository").strip() == "true" else []
    git(skill, "fetch", *fetch_options, "origin", f"refs/heads/{source['branch']}")
    commit = git(skill, "rev-parse", "FETCH_HEAD").strip()
    if int(git(skill, "rev-list", "--count", f"{commit}..HEAD").strip()):
        raise RuntimeError("Extraction Skill has local commits or diverged history; preserve them before updating")
    git(skill, "merge", "--ff-only", commit)
    files = [name for name in git(skill, "ls-files", "-z").split("\0") if name]
    if not {"SKILL.md", "LICENSE"} <= set(files):
        raise RuntimeError("Incomplete extraction Skill")
    # Record the resolved version only in the export, never as an update pin.
    source.update(commit=commit, files=len(files))
    output.parent.mkdir(parents=True, exist_ok=True)
    # Only our exact staging directory may be replaced; never traverse a link.
    if output.is_symlink() or output.resolve() != output:
        raise RuntimeError("Extraction staging directory must not be a link")
    with tempfile.TemporaryDirectory(prefix="extraction-stage-", dir=output.parent) as temporary:
        stage = Path(temporary)
        for name in files:
            relative = Path(name)
            origin = skill / relative
            if relative.is_absolute() or ".." in relative.parts or origin.is_symlink() or not origin.is_file() or not origin.resolve().is_relative_to(skill.resolve()):
                raise RuntimeError(f"Invalid Skill resource: {name}")
            target = stage / "skills" / SKILL_NAME / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, target)
        (stage / "skills" / "sources.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        python = stage / "extraction-python"
        python.mkdir()
        shutil.copy2(root / "extraction_python.py", python / "runner.py")
        # Replace only the prepared export; no SDKs or interpreters are downloaded.
        if output.exists():
            shutil.rmtree(output)
        shutil.copytree(stage, output)
    print(f"Prepared extraction resources: {len(files)} Skill files at {commit}; shared Python agent")
    return output


if __name__ == "__main__":
    prepare()
