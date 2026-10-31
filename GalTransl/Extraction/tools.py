"""Python ports of the extraction sidecar's read/write/edit and command tools."""
from __future__ import annotations

import os
import math
import re
import shutil
import tempfile
import threading
import unicodedata
from contextlib import contextmanager
from weakref import WeakValueDictionary
from pathlib import Path

from GalTransl.Agent.core import AgentStopRequested
from GalTransl.Agent.models import AgentToolError
from GalTransl.RuntimePaths import get_res_dir
from .commands import Commands
from .pi_text import MAX_BYTES, apply_edits, edit_diff, format_size, normalize_lf, truncate

_mutation_guard = threading.Lock()
_mutation_locks = WeakValueDictionary()


@contextmanager
def mutation_queue(path):
    key = os.path.normcase(str(path.resolve()))
    with _mutation_guard:
        lock = _mutation_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _mutation_locks[key] = lock
    with lock:
        yield


def native_result(text, details=None):
    return {"content": [{"type": "text", "text": text}], **({"details": details} if details is not None else {})}


def windows_shell_path(raw):
    """MSYS, Cygwin and WSL drive paths (/c/x, /mnt/c/x, /cygdrive/c/x) as native Windows paths."""
    if not raw.startswith("/") or raw.startswith("//") or "\\" in raw:
        return raw
    match = re.fullmatch(r"/(?:mnt/|cygdrive/)?([a-zA-Z])(?:/(.*))?", raw)
    if not match:
        return raw
    return f"{match.group(1).upper()}:\\{(match.group(2) or '').replace('/', chr(92))}"


def native_path(raw):
    if not isinstance(raw, str):
        raise AgentToolError("A file path is required")
    raw = raw.removeprefix("@")
    raw = re.sub("[\u00a0\u2000-\u200a\u202f\u205f\u3000]", " ", raw)
    if os.name == "nt":
        raw = windows_shell_path(raw)
    return os.path.expanduser(raw)


def bash_config():
    """Return the shell executable and the arguments that precede its script.

    A trailing ``-s`` means the script is piped through stdin, which keeps long
    commands clear of the Windows command-line limit. Windows uses the bundled
    Unicode BusyBox so every path and byte stays UTF-8.
    """
    if os.name == "nt":
        busybox = get_res_dir() / "busybox64u.exe"
        if not busybox.is_file():
            raise AgentToolError("busybox64u.exe is missing from the res directory")
        return str(busybox), ["sh", "-s"]
    return ("/bin/bash" if Path("/bin/bash").is_file() else shutil.which("bash") or "sh"), ["-c"]


def checked_path(raw, workspace, roots, allow_missing=False):
    if not isinstance(raw, str) or not raw or "\0" in raw:
        raise AgentToolError("A file path is required")
    target = Path(os.path.abspath(Path(workspace) / raw))
    permitted = sorted((Path(root).resolve(strict=True) for root in roots), key=lambda root: len(str(root)), reverse=True)
    root = next((root for root in permitted if target.is_relative_to(root)), None)
    if root is None:
        raise AgentToolError("Path is outside the permitted game, Skill or output directories")
    current = root
    for part in target.relative_to(root).parts:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if allow_missing:
                continue
            raise
        if current.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise AgentToolError("Symbolic links and junctions are not allowed")
    if not target.resolve(strict=not allow_missing).is_relative_to(root):
        raise AgentToolError("Path escapes its root")
    return target


class ExtractionTools:
    def __init__(self, workspace, game, skill, available, stop_event, emit, approve=None):
        self.workspace, self.game, self.skill = map(Path, (workspace, game, skill))
        self.available = available
        self.stop_event, self.approve = stop_event, approve
        self.commands = Commands(workspace, skill, available, stop_event, emit)

    def require_permission(self, name, args):
        self.check_stop()
        if self.approve:
            self.approve(name, args)
        self.check_stop()

    def check_stop(self):
        if self.stop_event.is_set():
            raise AgentStopRequested()

    def read_path(self, raw):
        return checked_path(raw, self.workspace, [self.workspace, self.game, self.skill])

    def write_path(self, raw):
        return checked_path(raw, self.workspace, [self.workspace], True)

    def save(self, target, text):
        if self.stop_event.is_set():
            raise AgentStopRequested()
        target = self.write_path(str(target))
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=target.parent,
                                             prefix=".extract-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(text)
            if self.stop_event.is_set():
                raise AgentStopRequested()
            os.replace(temporary, self.write_path(str(target)))
            temporary = None  # the rename consumed the temporary file
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    def read(self, args, _id):
        raw = native_path(args["path"])
        variants = [raw, re.sub(r" (AM|PM)\.", "\u202f\\1.", raw, flags=re.I),
                    unicodedata.normalize("NFD", raw), raw.replace("'", "’"),
                    unicodedata.normalize("NFD", raw).replace("'", "’")]
        target = None
        for variant in variants:
            try:
                target = self.read_path(variant)
                break
            except FileNotFoundError:
                continue
        if target is None:
            target = self.read_path(raw)
        from .pi_images import read_image_result
        image = read_image_result(target, self.stop_event)
        if image is not None:
            return image
        # Pi reads the entire file, decodes UTF-8 with replacement, and splits only LF.
        lines = target.read_bytes().decode("utf-8", errors="replace").split("\n")
        if self.stop_event.is_set():
            raise AgentStopRequested()
        start = max(0, args.get("offset", 1) - 1)
        if start >= len(lines):
            raise AgentToolError(f"Offset {args['offset']:g} is beyond end of file ({len(lines)} lines total)")
        end = min(start + args["limit"], len(lines)) if "limit" in args else len(lines)
        # JS Array.slice truncates fractional indices and accepts negative limits.
        begin_index, end_index = math.trunc(start), math.trunc(end)
        if end_index < 0:
            end_index = max(0, len(lines) + end_index)
        selected = "\n".join(lines[begin_index:max(begin_index, end_index)])
        truncation = truncate(selected)
        text, details = truncation["content"], None
        display = start + 1
        def number(value):
            return f"{value:g}"
        if truncation["firstLineExceedsLimit"]:
            size = format_size(len(lines[begin_index].encode("utf-8")))
            text = f"[Line {number(display)} is {size}, exceeds {format_size(MAX_BYTES)} limit. Use bash: sed -n '{number(display)}p' {args['path']} | head -c {MAX_BYTES}]"
            details = {"truncation": truncation}
        elif truncation["truncated"]:
            last = display + truncation["outputLines"] - 1
            budget = f" ({format_size(MAX_BYTES)} limit)" if truncation["truncatedBy"] == "bytes" else ""
            text += f"\n\n[Showing lines {number(display)}-{number(last)} of {len(lines)}{budget}. Use offset={number(last + 1)} to continue.]"
            details = {"truncation": truncation}
        elif "limit" in args and end < len(lines):
            text += f"\n\n[{number(len(lines) - end)} more lines in file. Use offset={number(end + 1)} to continue.]"
        return {**native_result(text, details), "structuredContent": text}

    def write(self, args, _id):
        target = self.write_path(native_path(args["path"]))
        with mutation_queue(target):
            self.require_permission("write", args)
            self.save(target, args["content"])
        return native_result(f"Successfully wrote to {args['path']}")

    def edit(self, args, _id):
        target = self.write_path(native_path(args["path"]))
        with mutation_queue(target):
            try:
                original = target.read_bytes()
            except OSError as exc:
                raise AgentToolError(f"Could not edit file: {args['path']}. Error code: {exc.errno}.") from exc
            raw = original.decode("utf-8", errors="replace")
            bom = "\ufeff" if raw.startswith("\ufeff") else ""
            text = raw[len(bom):]
            ending = "\r\n" if text.find("\r\n") >= 0 and text.find("\r\n") < text.find("\n") else "\n"
            base = normalize_lf(text)
            updated = apply_edits(base, args["edits"], args["path"])
            details = edit_diff(args["path"], base, updated, self.check_stop)
            self.require_permission("edit", args)
            if self.write_path(str(target)).read_bytes() != original:
                raise AgentToolError("The file changed while awaiting approval")
            self.save(target, bom + (updated.replace("\n", "\r\n") if ending == "\r\n" else updated))
        return native_result(f"Successfully replaced {len(args['edits'])} block(s) in {args['path']}.", details)

    def run_python(self, args, call_id):
        kinds = [key for key in ("code", "script", "module") if key in args]
        if len(kinds) != 1:
            raise AgentToolError("Supply exactly one of code, script or module")
        system = args.get("runtime", "bundled") == "system"
        if args.get("python_path") and not system:
            raise AgentToolError('python_path requires runtime="system"')
        python = self.available["builtin_python"]
        executable, prefix = python["executable"], python["args"]
        if system:
            candidates = self.available["system_pythons"]
            explicit = args.get("python_path")
            candidate = next((item for item in candidates if item["executable"] == explicit), None) if explicit else next(iter(candidates), None)
            if candidate:
                executable, prefix = candidate["executable"], [*candidate["args"], self.available["python_runner_script"]]
            elif explicit and Path(explicit).is_absolute() and Path(explicit).is_file():
                executable, prefix = explicit, [self.available["python_runner_script"]]
            else:
                raise AgentToolError("No local Python found. Configure a system Python executable or provide an absolute python_path")
        request = {kinds[0]: str(self.read_path(args["script"])) if kinds[0] == "script" else args[kinds[0]],
                   "args": args.get("args", []), "skill_dir": str(self.skill)}
        self.require_permission("run_python", args)
        return self.commands.execute(executable, [*prefix, "--request-stdin"], call_id, args.get("timeout_seconds", 120), request)

    def bash(self, args, call_id):
        timeout = args.get("timeout")
        if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
            raise AgentToolError("Invalid timeout: must be a finite number of seconds")
        if timeout is not None and timeout > 2147483.647:
            raise AgentToolError("Invalid timeout: maximum is 2147483.647 seconds")
        executable, prefix = bash_config()
        self.require_permission("bash", args)
        stdin_command = prefix[-1] == "-s"
        return self.commands.execute(executable, prefix if stdin_command else [*prefix, args["command"]],
                                     call_id, timeout, args["command"] if stdin_command else None, native=True)
