from __future__ import annotations

import json
import threading
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any

from GalTransl.Agent.backend_http import _normalize_endpoint
from GalTransl.Agent.context import _profile_context_window
from GalTransl.Reasoning import build_reasoning_kwargs
from GalTransl.Agent.images import save_images, user_message, user_event
from GalTransl.Agent.permissions import AUTO_QUIET_MODE, PERMISSION_MODES, _permission_needed, _tool_risk

from .environment import environment, workspace_path
from .runner import ExtractionRunner, restore_state
from .legacy import previous_messages
from .store import ACTIVE_STATUSES, SessionStore


def model_config(profile: Any) -> dict[str, Any]:
    if not isinstance(profile, dict):
        raise ValueError("Select an OpenAI-compatible model profile")
    section = profile.get("OpenAI-Compatible")
    if not isinstance(section, dict):
        raise ValueError("Select an OpenAI-compatible model profile")
    tokens = section.get("tokens")
    if not isinstance(tokens, list) or not tokens or not isinstance(tokens[0], dict):
        raise ValueError("The profile needs an API token and model name")
    token = tokens[0]
    api_key, model = str(token.get("token", "")).strip(), str(token.get("modelName", "")).strip()
    if not api_key or not model:
        raise ValueError("The profile needs an API token and model name")
    return {"apiKey": api_key, "id": model,
            "baseUrl": _normalize_endpoint(str(token.get("endpoint", ""))),
            "contextWindow": _profile_context_window(profile),
            "reasoningEffort": build_reasoning_kwargs(section, token).get("reasoning_effort")}


class ExtractionRuntime:
    def __init__(self, store: SessionStore | None = None):
        self.store = store or SessionStore()
        self.lock = threading.RLock()
        self.runs: dict[str, ExtractionRunner] = {}
        self.events: OrderedDict[str, deque] = OrderedDict()
        self.event_bytes: dict[str, int] = {}
        self.sequences: dict[str, int] = {}
        self.pending: dict[str, dict[str, Any]] = {}
        self.recovered: set[str] = set()
        self.live: dict[str, dict[str, dict[str, Any]]] = {}

    def _emit(self, session_id: str, event: dict[str, Any]) -> None:
        with self.lock:
            if session_id not in self.sequences:
                tail = self.store.history(session_id, limit=1)["events"]
                self.sequences[session_id] = max(self.store.read(session_id).get("seq", 0),
                                                 tail[-1]["seq"] if tail else 0)
            self.sequences[session_id] += 1
            record = {**event, "seq": self.sequences[session_id]}
            events = self.events.setdefault(session_id, deque())
            events.append(record)
            self.event_bytes[session_id] = self.event_bytes.get(session_id, 0) + len(json.dumps(record, ensure_ascii=False).encode("utf-8"))
            while len(events) > 1024 or (len(events) > 1 and self.event_bytes[session_id] > 2 * 1024 * 1024):
                self.event_bytes[session_id] -= len(json.dumps(events.popleft(), ensure_ascii=False).encode("utf-8"))
            self.events.move_to_end(session_id)
            while len(self.events) > 16:
                oldest = next((key for key in self.events if key not in self.runs and key != session_id), None)
                if oldest is None:
                    break
                self.events.pop(oldest)
                self.event_bytes.pop(oldest, None)
                self.sequences.pop(oldest, None)
                self.live.pop(oldest, None)
            live = self.live.setdefault(session_id, {})
            kind = record["type"]
            if kind in {"assistant_delta", "thinking_delta"}:
                key = kind + str(record.get("messageId", ""))
                previous = live.get(key, {})
                live[key] = {**record, "text": (record.get("text", "") if record.get('replace') else previous.get("text", "") + record.get("text", ""))[-64000:]}
            elif kind == "assistant_message":
                live.pop("assistant_delta" + str(record.get("messageId", "")), None)
            elif kind == "thinking_message":
                live.pop("thinking_delta" + str(record.get("messageId", "")), None)
            elif kind == "tool_output":
                key = kind + str(record.get("toolCallId", ""))
                previous = live.get(key, {})
                live[key] = {**record, "text": (previous.get("text", "") + record.get("text", ""))[-16000:]}
            elif kind == "tool_end":
                live.pop("tool_output" + str(record.get("toolCallId", "")), None)
            # Model history is persisted by the shared AgentRunner. This journal is a UI projection.
            if record["type"] not in {"assistant_delta", "thinking_delta", "tool_output"}:
                self.store.append(session_id, record)

    def session(self, session_id: str) -> dict[str, Any]:
        with self.lock:
            value = self.store.read(session_id)
            if session_id not in self.recovered:
                self.recovered.add(session_id)
                if value["status"] in ACTIVE_STATUSES and session_id not in self.runs:
                    value = self.store.update(session_id, status="interrupted", error="")
                    self._emit(session_id, {"type": "state", "status": "interrupted"})
            return value

    def list_sessions(self, offset: int = 0) -> dict[str, Any]:
        with self.lock:
            result = self.store.list(offset)
            result["sessions"] = [self.session(row["id"]) for row in result["sessions"]]
            result["running_game_dirs"] = sorted({self.store.read(sid)["game_dir"] for sid in self.runs})
            return result

    def delete_session(self, session_id: str) -> dict[str, Any]:
        with self.lock:
            if session_id in self.runs:
                raise ValueError("Stop the extraction agent before deleting its session")
            self.store.delete(session_id)
            for cache in (self.events, self.event_bytes, self.sequences, self.pending, self.live):
                cache.pop(session_id, None)
            self.recovered.discard(session_id)
            return {"deleted": session_id}

    def close_folder(self, game_dir: str) -> dict[str, Any]:
        if not game_dir.strip():
            raise ValueError("Game directory is required")
        with self.lock:
            key = self.store.folder_key(game_dir)
            if any(self.store.folder_key(self.store.read(sid)["game_dir"]) == key for sid in self.runs):
                raise ValueError("Stop the extraction agent before closing its game folder")
            self.store.set_folder_closed(game_dir, True)
            return {"closed": game_dir}

    def snapshot(self, session_id: str, before: int | None = None) -> dict[str, Any]:
        with self.lock:
            value = self.session(session_id)
            if before is None and session_id not in self.runs and self.store.repair_user_messages(session_id):
                # Rebuilding the journal changes sequence numbers; discard its old SSE window.
                self.events.pop(session_id, None)
                self.event_bytes.pop(session_id, None)
                self.sequences.pop(session_id, None)
                value = self.store.read(session_id)
            history = self.store.history(session_id, before)
            return {"session": value, **history,
                    "cursor": self.sequences.get(session_id, value["seq"]),
                    "live_events": list(self.live.get(session_id, {}).values()) if before is None else [],
                    "pending": self.pending.get(session_id)}

    def poll(self, session_id: str, after: int) -> dict[str, Any]:
        with self.lock:
            self.session(session_id)
            events = self.events.get(session_id, deque())
            cursor = self.sequences.get(session_id, self.store.read(session_id)["seq"])
            reset = after > cursor or (bool(events) and after < events[0]["seq"] - 1) or (not events and after < cursor)
            return {"events": [event for event in events if event["seq"] > after] if not reset else [],
                    "cursor": cursor, "reset": reset, "running": session_id in self.runs}

    def start(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        prompt = str(payload.get("message", "")).strip()
        if (not prompt and not payload.get('images')) or len(prompt) > 60000:
            raise ValueError("Message must contain between 1 and 60000 characters")
        model = model_config(payload.get("profile"))
        mode = str(payload.get("permission_mode", "auto"))
        if mode not in PERMISSION_MODES:
            raise ValueError("Invalid permission mode")
        available = environment()
        if not available["ready"]:
            raise ValueError("Extraction environment is incomplete: " + ", ".join(available["problems"]))
        with self.lock:
            if session_id in self.runs:
                raise ValueError("This extraction session is already running")
            session = self.session(session_id)
            game = Path(session["game_dir"]).resolve(strict=True)
            workspace = Path(session["workspace"]).resolve(strict=True)
            for active_id in self.runs:
                active = self.store.read(active_id)
                if Path(active["game_dir"]).resolve() == game:
                    raise ValueError("An extraction agent is already running for this game directory; stop it before starting another session")
                if Path(active["workspace"]).resolve() == workspace:
                    raise ValueError("An extraction agent is already running in this output workspace; stop it before starting another session")
            if len(self.runs) >= 2:
                raise ValueError("At most two extraction sessions can run concurrently")
            if not game.is_dir():
                raise ValueError("The game directory no longer exists")
            session_dir = self.store.directory(session_id).resolve()
            state, history = restore_state(session, session_dir, payload["profile"], prompt, mode)
            state.first_images = save_images(history, payload.get('images'))
            runner = ExtractionRunner(state, history, session, available, lambda event: self._on_event(session_id, event))
            if not state.messages:
                legacy = previous_messages(session, session_dir)
                if legacy:
                    runner._persist_message({"role": "system", "content": runner._system_prompt()})
                    for message in legacy:
                        runner._persist_message(message)
            self.runs[session_id] = runner
            self.live[session_id] = {}
            self.store.update(session_id, status="starting", error="", model_name=model["id"],
                              profile_name=str(payload.get("profile_name", ""))[:200], permission_mode=mode,
                              title=prompt[:50] if session["seq"] == 0 else session["title"])
            self._emit(session_id, {"type": "state", "status": "starting"})
            if state.messages:
                runner._persist_message(user_message(prompt, state.first_images))
                runner._emit("user_message", user_event(prompt, state.first_images))
            runner.thread = threading.Thread(target=self._run, args=(session_id, runner),
                                            daemon=True, name=f"extraction-{session_id[:8]}")
            runner.thread.start()
            return self.snapshot(session_id)

    def _run(self, session_id: str, runner: ExtractionRunner) -> None:
        failed = False
        try:
            with self.lock:
                if not runner.stop_event.is_set():
                    self.store.update(session_id, status="running")
                    self._emit(session_id, {"type": "state", "status": "running"})
            runner.run()
        except Exception as exc:
            failed = True
            self.store.update(session_id, error=str(exc)[:2000])
            self._emit(session_id, {"type": "error", "message": str(exc)[:2000]})
        finally:
            runner.abort_in_flight()
            with self.lock:
                self.pending.pop(session_id, None)
                for event in list(self.live.pop(session_id, {}).values()):
                    if event["type"] in {"assistant_delta", "thinking_delta"}:
                        self._emit(session_id, {**event, "type": "assistant_message" if event["type"] == "assistant_delta" else "thinking_message", "partial": True})
                self.live.pop(session_id, None)
                status = "stopped" if runner.stop_event.is_set() else "failed" if failed or runner.state.status == "failed" else "idle"
                self._emit(session_id, {"type": "state", "status": status})
                self.store.update(session_id, status=status, seq=self.sequences[session_id])
                # Publish the terminal state before poll() may close its stream.
                self.runs.pop(session_id, None)

    def _on_event(self, session_id: str, event: dict[str, Any]) -> None:
        with self.lock:
            kind = event["type"]
            if kind in {"permission_request", "ask_request"}:
                self.pending[session_id] = event
                status = "awaiting_permission" if kind == "permission_request" else "awaiting_input"
                self.store.update(session_id, status=status)
                self._emit(session_id, event)
                self._emit(session_id, {"type": "state", "status": status})
                # Mode changes may occur between the tool's permission check and
                # registering its pending request. Reapply against the live mode.
                runner = self.runs.get(session_id)
                if runner is not None:
                    runner.apply_permission_mode(runner.state.permission_mode)
                    self._clear_allowed_request(session_id, runner)
            elif kind == "error":
                self.store.update(session_id, status="failed", error=str(event.get("message", ""))[:2000])
                self._emit(session_id, event)
            elif kind in {"user_message", "assistant_start", "assistant_delta", "assistant_message", "thinking_delta", "thinking_message",
                          "tool_start", "tool_output", "tool_end", "compaction", "usage", "context_usage", "notice"}:
                self._emit(session_id, event)

    def answer(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            pending = self.pending.get(session_id)
            request_id = str(payload.get("request_id", ""))
            if not pending or pending["requestId"] != request_id or session_id not in self.runs:
                raise ValueError("This request is no longer pending")
            if pending["type"] == "permission_request":
                decision = payload.get("decision")
                if decision not in {"allow-once", "allow-turn", "deny"}:
                    raise ValueError("Invalid approval decision")
                command = {"type": "answer", "requestId": request_id, "decision": decision}
            else:
                if 'answers' in payload:
                    command = {"type": "answer", "requestId": request_id, "answers": payload['answers']}
                else:
                    text = str(payload.get("text", "")).strip()
                    if not text or len(text) > 10000:
                        raise ValueError("An answer is required (maximum 10000 characters)")
                    command = {"type": "answer", "requestId": request_id, "text": text, "answers": [[text]]}
            runner = self.runs[session_id]
            if pending["type"] == "permission_request":
                runner.resolve_permission("allow-session" if command["decision"] == "allow-turn" else command["decision"])
            else:
                runner.resolve_ask(command['answers'])
            self.pending.pop(session_id, None)
            self._emit(session_id, {"type": "request_answered", "requestId": request_id,
                                    "decision": command.get("decision"), "text": command.get("text"),
                                    "answers": command.get('answers')})
            self.store.update(session_id, status="running")
            self._emit(session_id, {"type": "state", "status": "running"})
            return self.snapshot(session_id)

    def _clear_allowed_request(self, session_id, runner):
        pending = self.pending.get(session_id)
        if not pending:
            return
        allowed = (runner.state.permission_mode == AUTO_QUIET_MODE if pending['type'] == 'ask_request'
                   else not _permission_needed(_tool_risk(pending['toolName']), runner.state.permission_mode))
        if allowed:
            self.pending.pop(session_id, None)
            self._emit(session_id, {'type': 'request_answered', 'requestId': pending['requestId'], 'automatic': True})
            self.store.update(session_id, status='running')
            self._emit(session_id, {'type': 'state', 'status': 'running'})

    def set_permission_mode(self, session_id: str, mode: Any) -> dict[str, Any]:
        if mode not in PERMISSION_MODES:
            raise ValueError('Invalid permission mode')
        with self.lock:
            self.session(session_id)
            runner = self.runs.get(session_id)
            if runner is not None:
                runner.apply_permission_mode(mode)
                self._clear_allowed_request(session_id, runner)
            self.store.update(session_id, permission_mode=mode)
            self._emit(session_id, {'type': 'permission_mode', 'permission_mode': mode})
            return {'permission_mode': mode, 'session_id': session_id}

    def stop(self, session_id: str) -> dict[str, Any]:
        with self.lock:
            self.session(session_id)
            run = self.runs.get(session_id)
            if run:
                self.pending.pop(session_id, None)
                self.store.update(session_id, status="stopping")
                self._emit(session_id, {"type": "state", "status": "stopping"})
                run.stop_event.set()
                run.abort_in_flight()
            return self.snapshot(session_id)

    def import_project(self, session_id: str) -> dict[str, Any]:
        """Resolve the output's project destination without creating configuration or cache files."""
        with self.lock:
            if session_id in self.runs:
                raise ValueError("Stop the extraction turn before opening its output as a project")
            workspace = Path(self.session(session_id)["workspace"])
            workspace = workspace.resolve(strict=True)
            for name in ("config.yaml", "config.yml"):
                config = workspace / name
                if config.exists() or config.is_symlink():
                    if not workspace_path(workspace, name).is_file():
                        raise ValueError(f"Project configuration is not a file: {name}")
                    return {"project_dir": str(workspace), "config_file_name": name}
            return {"project_dir": str(workspace), "config_file_name": None}

    def close(self) -> None:
        with self.lock:
            runs = list(self.runs.values())
        for runner in runs:
            runner.stop_event.set()
            runner.abort_in_flight()
        for runner in runs:
            runner.thread.join(timeout=12)


EXTRACTION_RUNTIME = ExtractionRuntime()
