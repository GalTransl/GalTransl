"""Download the latest built-in Skill and export extraction resources."""
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


def prepare(root=ROOT, output=OUTPUT, *, update=True):
    if getattr(sys, "frozen", False):
        raise RuntimeError("Packaged applications use their bundled Skill without updating it")
    from GalTransl.Extraction.skill import SOURCE, RECEIPT, install_skill, prune_skill_versions, skill_status

    root, output = Path(root).resolve(), Path(output).absolute()
    manifest = json.loads((root / "skills" / "sources.json").read_text(encoding="utf-8"))
    source = manifest[SKILL_NAME]
    if source["source"] != SOURCE:
        raise RuntimeError("The extraction Skill source does not match the application's download source")
    skill = install_skill(root / "skills", branch=source["branch"]) if update else root / "skills" / SKILL_NAME
    present = (skill / "SKILL.md").is_file()
    commit = skill_status(skill)["current_commit"] if present else ""
    files = [path.relative_to(skill).as_posix() for path in sorted(skill.rglob("*"))
             if path.is_file() and not {".git", "__pycache__"}.intersection(path.relative_to(skill).parts)
             and path.name != RECEIPT] if present else []
    if (update or files) and not {"SKILL.md", "LICENSE"} <= set(files):
        raise RuntimeError("Incomplete extraction Skill")
    # Record the resolved version only in the export, never as an update pin.
    source.update(commit=commit, files=len(files))
    resources = [(skill / name, Path("skills") / SKILL_NAME / name) for name in files]
    # Other skills and helpers come from tracked application files.
    for name in filter(None, git(root, "ls-files", "-z", "--", "skills").split("\0")):
        relative = Path(name)
        if relative.is_relative_to(Path("skills") / SKILL_NAME) or relative == Path("skills/sources.json"):
            continue
        origin = root / relative
        resources.append((origin, relative))
    output.parent.mkdir(parents=True, exist_ok=True)
    # Only our exact staging directory may be replaced; never traverse a link.
    if output.is_symlink() or output.resolve() != output:
        raise RuntimeError("Extraction staging directory must not be a link")
    with tempfile.TemporaryDirectory(prefix="extraction-stage-", dir=output.parent) as temporary:
        stage = Path(temporary)
        for origin, relative in resources:
            if relative.is_absolute() or ".." in relative.parts or origin.is_symlink() or not origin.is_file() or not origin.resolve().is_relative_to((root / "skills").resolve()):
                raise RuntimeError(f"Invalid Skill resource: {relative}")
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, target)
        (stage / "skills").mkdir(exist_ok=True)
        (stage / "skills" / "sources.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        python = stage / "extraction-python"
        python.mkdir()
        shutil.copy2(root / "extraction_python.py", python / "runner.py")
        # Replace only the prepared export; no SDKs or interpreters are downloaded.
        if output.exists():
            shutil.rmtree(output)
        shutil.copytree(stage, output)
    if update:
        prune_skill_versions(root / "skills", [])
    print(f"Prepared extraction resources: {len(files)} Skill files at {commit}; shared Python agent")
    return output


if __name__ == "__main__":
    import argparse
    sys.path.insert(0, str(ROOT))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", action="store_true", help="Prepare local resources without downloading or updating Skill")
    prepare(update=not parser.parse_args().local)
