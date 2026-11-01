"""Small Python execution entry shared by the frozen backend and system Python.

This is an execution adapter, not a sandbox or a complete Python installation.
It deliberately imports no GalTransl modules, so running a script never starts
the HTTP server, translation jobs, or update-check threads.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import runpy
import sys
import traceback
from pathlib import Path

MAX_REQUEST_BYTES = 1024 * 1024


def main(argv: list[str] | None = None) -> int:
    # PyInstaller ignores Python's environment flags. Explicitly configure the
    # adapter's pipes so frozen Windows builds emit the same UTF-8 as source runs.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description="GalTransl bundled Python executor")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--request-stdin", action="store_true")
    cli_arguments = sys.argv[1:] if argv is None else argv
    options = parser.parse_args(cli_arguments) if cli_arguments[:1] == ["--request-stdin"] else None
    if options is not None and options.version:
        print(f"Python {sys.version.split()[0]}")
        return 0
    try:
        request = _cli_request(cli_arguments) if options is None else None
        if options is None and request is None:
            print(f"Python {sys.version.split()[0]}")
            return 0
        if options is not None:
            # Task code sees EOF after the tool's JSON request has been consumed.
            raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
            if len(raw) > MAX_REQUEST_BYTES:
                raise ValueError("Python execution request exceeds 1 MiB")
            request = json.loads(raw)
        if not isinstance(request, dict):
            raise ValueError("Expected a Python execution request object")
        kind = [key for key in ("code", "script", "module") if key in request]
        if len(kind) != 1:
            raise ValueError("Supply exactly one of code, script or module")
        if not isinstance(request[kind[0]], str):
            raise ValueError("Python code, script or module must be a string")
        arguments = request.get("args", [])
        if not isinstance(arguments, list) or any(not isinstance(arg, str) for arg in arguments):
            raise ValueError("Python arguments must be strings")
        argv0 = request.get("argv0", "-c")
        if not isinstance(argv0, str):
            raise ValueError("Python argv0 must be a string")
        skills = request.get("skill_dirs")
        if skills is None:
            skill = request.get("skill_dir")
            skills = [skill] if skill else json.loads(os.environ.get("GALTRANSL_SKILL_DIRS", "[]"))
            if skills == [] and os.environ.get("GALTRANSL_SKILL_DIR"):
                skills = [os.environ["GALTRANSL_SKILL_DIR"]]
        if not isinstance(skills, list) or any(not isinstance(skill, str) or not skill for skill in skills):
            raise ValueError("Python skill_dirs must be a list of directory paths")
        paths = [str(Path.cwd())]
        for skill in reversed(skills):
            directory = Path(skill).resolve(strict=True)
            if not directory.is_dir():
                raise ValueError("Invalid extraction Skill directory")
            paths.insert(0, str(directory))
        sys.path[:0] = paths
        if kind[0] == "script":
            script = Path(request["script"]).resolve(strict=True)
            sys.path.insert(0, str(script.parent))
            sys.argv = [str(script), *arguments]
            runpy.run_path(str(script), run_name="__main__")
        elif kind[0] == "module":
            sys.argv = [str(request["module"]), *arguments]
            runpy.run_module(str(request["module"]), run_name="__main__", alter_sys=True)
        else:
            sys.argv = [argv0, *arguments]
            namespace = {"__name__": "__main__", "__package__": None, "__file__": "<extraction-agent>"}
            exec(compile(request["code"], "<extraction-agent>", "exec"), namespace)
        return 0
    except SystemExit as exc:
        if exc.code is None:
            return 0
        if isinstance(exc.code, int):
            return exc.code
        print(exc.code, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except ModuleNotFoundError as exc:
        _print_task_exception(exc)
        if getattr(sys, "frozen", False):
            print("This module is unavailable in bundled Python. You may try run_python with runtime='system' and a locally installed interpreter.", file=sys.stderr)
        return 1
    except Exception as exc:
        _print_task_exception(exc)
        return 1


def _print_task_exception(exc):
    # Trim only leading executor/runpy frames, preserving task and import frames
    # as well as Python's normal exception chains and syntax-error diagnostics.
    wrapper_files = {main.__code__.co_filename, runpy.run_path.__code__.co_filename,
                     runpy.run_module.__code__.co_filename}
    tb = exc.__traceback__
    while tb is not None and tb.tb_frame.f_code.co_filename in wrapper_files:
        tb = tb.tb_next
    traceback.print_exception(type(exc), exc, tb)


def _cli_request(argv):
    arguments = iter(argv)
    for option in arguments:
        if option == "-B":
            sys.dont_write_bytecode = True
            continue
        if option == "-u":
            for name in ("stdout", "stderr"):
                stream = getattr(sys, name)
                if stream is not None:
                    stream.flush()
                    raw = os.fdopen(stream.fileno(), "wb", buffering=0, closefd=False)
                    setattr(sys, name, io.TextIOWrapper(raw, encoding="utf-8",
                            errors="backslashreplace", write_through=True))
            continue
        if option in ("-V", "--version"):
            return None
        if option in ("-c", "-m"):
            value = next(arguments, None)
            if value is None:
                raise ValueError(f"{option} requires an argument")
            return {"code" if option == "-c" else "module": value, "args": list(arguments)}
        if option == "--":
            script = next(arguments, "-")
            if script != "-":
                return {"script": script, "args": list(arguments)}
            break
        if option == "-":
            break
        if option.startswith("-"):
            raise ValueError(f"Unsupported bundled Python option: {option}")
        return {"script": option, "args": list(arguments)}
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("Python stdin code exceeds 1 MiB")
    return {"code": raw.decode("utf-8-sig"), "args": list(arguments), "argv0": "-"}


if __name__ == "__main__":
    raise SystemExit(main())
