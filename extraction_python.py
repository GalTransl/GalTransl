"""Small Python execution entry shared by the frozen backend and system Python.

This is an execution adapter, not a sandbox or a complete Python installation.
It deliberately imports no GalTransl modules, so running a script never starts
the HTTP server, translation jobs, or update-check threads.
"""
from __future__ import annotations

import argparse
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
    options = parser.parse_args(argv)
    if options.version:
        print(f"Python {sys.version.split()[0]}")
        return 0
    if not options.request_stdin:
        parser.error("--request-stdin is required")
    try:
        # Read the request before replacing stdin: task code sees normal EOF.
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
        skill = request.get("skill_dir") or os.environ.get("GALTRANSL_SKILL_DIR")
        paths = [str(Path.cwd())]
        if skill:
            skill = Path(skill).resolve(strict=True)
            if not (skill / "SKILL.md").is_file():
                raise ValueError("Invalid extraction Skill directory")
            paths.insert(0, str(skill))
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
            sys.argv = ["-c", *arguments]
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
    except ModuleNotFoundError:
        traceback.print_exc()
        if getattr(sys, "frozen", False):
            print("This module is unavailable in bundled Python. You may try run_python with runtime='system' and a locally installed interpreter.", file=sys.stderr)
        return 1
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
