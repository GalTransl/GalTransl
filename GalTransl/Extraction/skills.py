"""Pi-style skill discovery and progressive loading, independent of the runner."""
from __future__ import annotations

import re
from html import escape
from pathlib import Path
from typing import TypedDict


class Skill(TypedDict):
    name: str
    description: str
    file_path: str
    base_dir: str
    source: str
    disable_model_invocation: bool


class SkillDiagnostic(TypedDict):
    path: str
    message: str


def discover_skills(roots: list[Path]) -> tuple[list[Skill], list[SkillDiagnostic]]:
    skills: list[Skill] = []
    diagnostics: list[SkillDiagnostic] = []
    seen_files: set[Path] = set()
    seen_names: dict[str, str] = {}
    for root in roots:
        for path in _skill_files(root.expanduser(), include_root_files=True):
            canonical = path.resolve()
            if canonical in seen_files:
                continue
            seen_files.add(canonical)
            try:
                metadata, _ = parse_skill_file(path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, ValueError) as exc:
                diagnostics.append({"path": str(path), "message": str(exc)})
                continue
            description = metadata.get("description")
            if not isinstance(description, str) or not description.strip():
                diagnostics.append({"path": str(path), "message": "A non-empty description is required"})
                continue
            name = metadata.get("name", path.parent.name)
            if not isinstance(name, str) or not name.strip():
                diagnostics.append({"path": str(path), "message": "Skill name must be a non-empty string"})
                continue
            name = name.strip()
            if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 64:
                diagnostics.append({"path": str(path), "message": "Skill names should use lowercase letters, digits and hyphens, up to 64 characters"})
            if len(description) > 1024:
                diagnostics.append({"path": str(path), "message": "Skill descriptions should not exceed 1024 characters"})
            if name in seen_names:
                diagnostics.append({"path": str(path), "message": f"Duplicate skill name {name}; using {seen_names[name]}"})
                continue
            seen_names[name] = str(canonical)
            skills.append({"name": name, "description": description.strip(), "file_path": str(canonical),
                           "base_dir": str(canonical.parent), "source": str(root),
                           "disable_model_invocation": metadata.get("disable-model-invocation") is True})
    return skills, diagnostics


def _skill_files(directory: Path, *, include_root_files=False):
    if not directory.is_dir() or directory.is_symlink() or getattr(directory.stat(), "st_file_attributes", 0) & 0x400:
        return
    entry = directory / "SKILL.md"
    if entry.is_file() and not entry.is_symlink():
        yield entry
        return  # References under a skill belong to it, rather than becoming more skills.
    for child in sorted(directory.iterdir(), key=lambda item: item.name):
        if child.name.startswith(".") or child.name in {"node_modules", "__pycache__"} or child.is_symlink():
            continue
        if child.is_dir():
            yield from _skill_files(child)
            continue
        if include_root_files and child.is_file() and child.suffix == ".md":
            yield child


def parse_skill_file(content: str) -> tuple[dict[str, object], str]:
    from yaml import YAMLError, safe_load

    text = content.lstrip("\ufeff").replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return {}, text
    match = re.search(r"^---[ \t]*$", text[4:], re.MULTILINE)
    if match is None:
        raise ValueError("Unterminated skill frontmatter")
    end = 4 + match.start()
    try:
        metadata = safe_load(text[4:end])
    except YAMLError as exc:
        raise ValueError(f"Invalid skill frontmatter: {exc}") from exc
    if not isinstance(metadata, dict) or any(not isinstance(key, str) for key in metadata):
        raise ValueError("Skill frontmatter must be a mapping with string keys")
    return metadata, text[4 + match.end():].lstrip("\n")


def format_skills_for_prompt(skills: list[Skill]) -> str:
    visible = [skill for skill in skills if not skill["disable_model_invocation"]]
    if not visible:
        return ""
    entries = "\n".join(
        "  <skill>\n"
        f"    <name>{escape(skill['name'])}</name>\n"
        f"    <description>{escape(skill['description'])}</description>\n"
        f"    <location>{escape(Path(skill['file_path']).as_posix())}</location>\n"
        "  </skill>" for skill in visible)
    return (
        "The following skills provide specialized instructions for specific tasks.\n"
        "Use the read tool to load a skill's file when the task matches its description.\n"
        "When a skill file references a relative path, resolve it against the skill directory "
        "(parent of SKILL.md / dirname of the path) and use that absolute path in tool commands.\n\n"
        f"<available_skills>\n{entries}\n</available_skills>")


def render_skill_block(skill: Skill) -> str:
    """The skill's full body (frontmatter stripped), wrapped with its location for relative references."""
    _, body = parse_skill_file(Path(skill["file_path"]).read_text(encoding="utf-8-sig"))
    return (f'<skill name="{escape(skill["name"])}" location="{escape(Path(skill["file_path"]).as_posix())}">\n'
            f"References are relative to {Path(skill['base_dir']).as_posix()}.\n\n{body.strip()}\n</skill>")


def expand_skill_command(text: str, skills: list[Skill]) -> str:
    if not text.startswith("/skill:"):
        return text
    command, _, arguments = text.partition(" ")
    skill = next((skill for skill in skills if skill["name"] == command[7:]), None)
    if skill is None:
        return text
    block = render_skill_block(skill)
    return f"{block}\n\n{arguments.strip()}" if arguments.strip() else block
