"""Bounded, cancellable local commands; no JavaScript runtime involved."""
from __future__ import annotations

import codecs
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from GalTransl.Agent.core import AgentStopRequested
from GalTransl.Agent.models import AgentToolError
from .process import CommandJob, terminate_tree
from .subprocess_env import external_environment, external_program

MAX_OUTPUT_BYTES = 10 * 1024 * 1024
PREVIEW_CHARS = 16000
EXIT_IDLE_SECONDS = .1


class Commands:
    def __init__(self, workspace, skill, available, stop_event, emit):
        self.workspace, self.skill = Path(workspace), str(skill)
        self.available, self.stop_event, self.emit = available, stop_event, emit
        self.lock = threading.Lock()
        self.process = None
        self.job = None

    def stop(self):
        with self.lock:
            if self.process is not None:
                if self.job:
                    self.job.kill()
                terminate_tree(self.process)

    def execute(self, executable, args, call_id, timeout=120, request=None, *, native=False):
        if self.stop_event.is_set():
            raise AgentStopRequested()
        # The reports directory must be rechecked: scripts can create links.
        from .tools import checked_path
        log_path = checked_path(f"reports/command-{uuid.uuid4().hex}.log", self.workspace, [self.workspace], True)
        env = external_environment()
        if native:
            # Native bash inherits the user's environment; frozen loader paths still need cleaning.
            env = dict(os.environ)
            if getattr(sys, 'frozen', False):
                if "LD_LIBRARY_PATH_ORIG" in os.environ:
                    env["LD_LIBRARY_PATH"] = os.environ["LD_LIBRARY_PATH_ORIG"]
                else:
                    env.pop("LD_LIBRARY_PATH", None)
        python = self.available["builtin_python"]
        if not native:
            env['PYTHONPATH'] = self.skill
        env.update(GALTRANSL_SKILL_DIR=self.skill,
                   GALTRANSL_OUTPUT_DIR=str(self.workspace), GALTRANSL_PYTHON=python["executable"],
                   GALTRANSL_PYTHON_ARGS=json.dumps(python["args"]))
        started = time.monotonic()
        output, live, total, failure = "", "", 0, ""
        chunks = queue.Queue(maxsize=16)
        reader_done = threading.Event()
        reader_stop = threading.Event()
        # Set when bash returns while a background descendant still holds the pipe open.
        detached = threading.Event()
        from .pi_output import OutputAccumulator, shell_result
        accumulator = OutputAccumulator() if native else None
        child = None
        survive = False
        # One merged pipe preserves command output order and bounds buffering.
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path = checked_path(str(log_path), self.workspace, [self.workspace], True)
        with log_path.open("xb") as log:
            try:
                with self.lock, external_program():
                    if self.stop_event.is_set():
                        raise AgentStopRequested()
                    self.job = CommandJob()
                    child = subprocess.Popen([executable, *args], cwd=self.workspace, env=env,
                        stdin=subprocess.PIPE if request is not None else subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        creationflags=0x08000000 if os.name == "nt" else 0, start_new_session=os.name != "nt")
                    self.process = child
                    self.job.attach(child)

                def read_output():
                    try:
                        while data := child.stdout.read1(4096):
                            while not (reader_stop.is_set() or detached.is_set()):
                                try:
                                    chunks.put(data, timeout=.1)
                                    break
                                except queue.Full:
                                    continue
                            if reader_stop.is_set() or detached.is_set():
                                return
                    except (OSError, ValueError):
                        pass
                    finally:
                        reader_done.set()
                        if detached.is_set():
                            child.stdout.close()

                reader = threading.Thread(target=read_output, daemon=True)
                reader.start()
                if request is not None:
                    def write_request():
                        try:
                            payload = request if isinstance(request, str) else json.dumps(request, ensure_ascii=False)
                            child.stdin.write(payload.encode("utf-8"))
                            child.stdin.close()
                        except (OSError, ValueError):
                            pass
                    threading.Thread(target=write_request, daemon=True).start()
                decoder = codecs.getincrementaldecoder("utf-8")("replace")
                last_flush = started
                killed_at = None
                parent_exited_at = None
                last_data = started
                while child.poll() is None or not reader_done.is_set() or not chunks.empty():
                    now = time.monotonic()
                    if self.stop_event.is_set():
                        failure = "Operation stopped"
                    elif timeout is not None and now - started > timeout and not (native and child.poll() is not None):
                        failure = f"Command timed out after {timeout:g} seconds" if native else "Command exceeded its time limit"
                    if child.poll() is not None and parent_exited_at is None:
                        parent_exited_at = now
                        if not native:
                            self.job.kill()
                            terminate_tree(child)
                    if native and parent_exited_at is not None and not failure \
                            and chunks.empty() and now - max(parent_exited_at, last_data) > EXIT_IDLE_SECONDS:
                        # Like Pi, a background descendant holding the pipe does not keep bash waiting.
                        detached.set()
                        break
                    if not native and parent_exited_at is not None and now - parent_exited_at > 4:
                        break
                    if failure and killed_at is None:
                        self.job.kill()
                        terminate_tree(child)
                        killed_at = now
                    if killed_at is not None and now - killed_at > 4:
                        break
                    try:
                        data = chunks.get(timeout=.05)
                    except queue.Empty:
                        data = b""
                    if data:
                        last_data = now
                        remaining = len(data) if native else max(0, MAX_OUTPUT_BYTES - total)
                        total += len(data)
                        log.write(data[:remaining])
                        text = decoder.decode(data[:remaining])
                        if accumulator:
                            accumulator.append(data)
                        output, live = (output + text)[-PREVIEW_CHARS:], (live + text)[-PREVIEW_CHARS:]
                        if not native and total > MAX_OUTPUT_BYTES:
                            failure = "Command exceeded the 10 MiB output budget"
                    if live and now - last_flush >= .08:
                        if native:
                            self.emit({"type": "tool_output", "toolCallId": call_id,
                                       "text": accumulator.snapshot()['content'], "replace": True})
                        else:
                            self.emit({"type": "tool_output", "toolCallId": call_id, "text": live})
                        live, last_flush = "", now
                output = (output + decoder.decode(b"", final=True))[-PREVIEW_CHARS:]
                if accumulator:
                    accumulator.append(b"", final=True)
                if native:
                    self.emit({"type": "tool_output", "toolCallId": call_id,
                               "text": accumulator.snapshot()['content'], "replace": True})
                elif live:
                    self.emit({"type": "tool_output", "toolCallId": call_id, "text": live})
                child.wait(timeout=5)
                # A finished bash leaves background descendants running; stop and failure still reap them.
                survive = native and not failure and not self.stop_event.is_set()
            except OSError as exc:
                raise AgentToolError(f"Cannot execute command: {exc}") from exc
            finally:
                if not survive:
                    reader_stop.set()
                with self.lock:
                    if self.job:
                        self.job.release() if survive else self.job.close()
                        self.job = None
                    if child is not None:
                        if not survive:
                            terminate_tree(child)
                        child.wait(timeout=8)
                        # A detached reader closes its own end after its next read; closing here would block on it.
                        for stream in (child.stdin, None if detached.is_set() else child.stdout):
                            if stream:
                                stream.close()
                    self.process = None
        if self.stop_event.is_set():
            raise AgentStopRequested()
        if native:
            exit_code = child.returncode
            if os.name != "nt" and exit_code < 0:
                exit_code = 128 - exit_code
            result = shell_result(accumulator, log_path, exit_code, time.monotonic() - started, failure)
            if failure:
                raise AgentToolError(result)
            return result
        result = {"exit_code": child.returncode, "signal": None, "output": output, "output_bytes": total,
                  "truncated": total > len(output.encode("utf-8")),
                  "log_path": str(log_path.relative_to(self.workspace)),
                  "wall_time_seconds": round(time.monotonic() - started, 3)}
        if os.name != "nt" and child.returncode < 0:
            import signal
            result.update(exit_code=None, signal=signal.Signals(-child.returncode).name)
        if failure:
            result["error"] = failure
        if failure or child.returncode:
            raise AgentToolError(json.dumps(result, ensure_ascii=False))
        return result
