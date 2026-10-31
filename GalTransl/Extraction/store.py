from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .environment import atomic_json, checked_directory, data_root, workspace_path


SESSION_ID = re.compile(r"^[0-9a-f]{32}$")
MAX_EVENT_BYTES = 256 * 1024
MAX_PAGE_BYTES = 1024 * 1024
ACTIVE_STATUSES = {"starting", "running", "awaiting_permission", "awaiting_input", "stopping"}


def _repair_records(path: Path) -> list[dict[str, Any]]:
    # Recovery is optional: refuse oversized or damaged logs rather than guessing.
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("Recovery journal exceeds its budget")
    records = []
    with path.open("rb") as stream:
        while line := stream.readline(1024 * 1024 + 1):
            if len(line) > 1024 * 1024:
                raise ValueError("Recovery record exceeds its budget")
            if line.strip():
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("Invalid recovery record")
                records.append(record)
    return records


def _work_item_keys(event: dict[str, Any]) -> set[str]:
    # Only retain identities while scanning the truncated turn, never its output.
    kind = event["type"]
    if kind in {"tool_start", "tool_end"}:
        return {f"tool:{event.get('toolCallId') or 'tool-' + str(event['seq'])}"}
    if kind in {"assistant_message", "thinking_message"} and event.get("text"):
        row_id = event.get("messageId") or f"assistant-{event['seq']}"
        return {f"{'content' if kind == 'assistant_message' else 'reasoning'}:{row_id}"}
    return set()


class SessionStore:
    def __init__(self, root: Path | None = None):
        self.root = root or data_root() / "sessions"
        self.lock = threading.RLock()

    def directory(self, session_id: str) -> Path:
        if not SESSION_ID.fullmatch(session_id):
            raise ValueError("Invalid extraction session id")
        return self.root / session_id

    def read(self, session_id: str) -> dict[str, Any]:
        with self.lock:
            value = json.loads((self.directory(session_id) / "session.json").read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("Invalid extraction session")
            return value

    def update(self, session_id: str, **changes: Any) -> dict[str, Any]:
        with self.lock:
            value = self.read(session_id)
            value.update(changes, updated_at=time.time())
            atomic_json(self.directory(session_id) / "session.json", value)
            return value

    def list(self, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        with self.lock:
            closed = self.closed_folders()
            rows = []
            if self.root.exists():
                for path in self.root.glob("*/session.json"):
                    if SESSION_ID.fullmatch(path.parent.name):
                        row = self.read(path.parent.name)
                        if self.folder_key(row["game_dir"]) not in closed:
                            rows.append(row)
            rows.sort(key=lambda row: row["updated_at"], reverse=True)
            offset, limit = max(0, offset), min(100, max(1, limit))
            return {"sessions": rows[offset:offset + limit], "total": len(rows),
                    "next_offset": offset + limit if offset + limit < len(rows) else None}

    @staticmethod
    def folder_key(game_dir: str) -> str:
        return os.path.normcase(str(Path(game_dir).resolve()))

    def closed_folders(self) -> set[str]:
        path = self.root / "closed-folders.json"
        return set(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else set()

    def set_folder_closed(self, game_dir: str, closed: bool) -> None:
        with self.lock:
            folders = self.closed_folders()
            key = self.folder_key(game_dir)
            if closed:
                folders.add(key)
            else:
                folders.discard(key)
            atomic_json(self.root / "closed-folders.json", sorted(folders))

    def delete(self, session_id: str) -> None:
        with self.lock:
            directory = self.directory(session_id)
            if directory.resolve().parent != self.root.resolve() or directory.is_symlink():
                raise ValueError("Invalid extraction session directory")
            self.read(session_id)
            # Session history is separate from the shared output workspace.
            shutil.rmtree(directory)

    def create(self, game_dir: str, output_dir: str = "") -> dict[str, Any]:
        game = checked_directory(game_dir)
        if output_dir:
            workspace = Path(output_dir).expanduser().absolute()
            parent = checked_directory(str(workspace.parent))
            workspace = parent / workspace.name
        else:
            workspace = game / f"{game.name or 'game'}_extract"
        workspace.mkdir(exist_ok=True)
        for name in ("gt_input", "gt_output", "original", "metadata", "reports", "scripts"):
            (workspace / name).mkdir(exist_ok=True)
        session_id = uuid.uuid4().hex
        directory = self.directory(session_id)
        directory.mkdir(parents=True, exist_ok=False)
        now = time.time()
        value = {"id": session_id, "title": game.name or "Extraction", "game_dir": str(game),
                 "workspace": str(workspace.resolve()), "created_at": now, "updated_at": now,
                 "status": "idle", "seq": 0, "model_name": "", "profile_name": "",
                 "permission_mode": "auto", "error": ""}
        atomic_json(directory / "session.json", value)
        self.set_folder_closed(str(game), False)
        return value

    def append(self, session_id: str, event: dict[str, Any]) -> None:
        data = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
        if len(data) > MAX_EVENT_BYTES:
            raise ValueError("Extraction event exceeds the journal budget")
        with self.lock:
            with (self.directory(session_id) / "events.jsonl").open("ab") as stream:
                stream.write(data)

    def repair_user_messages(self, session_id: str) -> bool:
        """Recover omitted bubbles from the shared runner's original UI events.

        Only an exact match of turn boundaries is safe. Model messages can carry
        internal prefixes/compaction summaries, so never use them as bubble text.
        Call only while the session is idle; sequence numbers change on repair.
        """
        with self.lock:
            session = self.read(session_id)
            if session.get("user_message_projection") == 1:
                return False
            directory = self.directory(session_id)
            source = directory / "execution" / f"{session_id}.jsonl"
            journal = directory / "events.jsonl"
            if not source.is_file() or not journal.is_file():
                return False
            try:
                users = [record["event"] for record in _repair_records(source)
                         if record.get("t") == "event" and isinstance(record.get("event"), dict)
                         and record["event"].get("type") == "user_message"]
                events = _repair_records(journal)
            except (OSError, ValueError):
                return False
            starts = [index for index, event in enumerate(events)
                      if event.get("type") == "state" and event.get("status") == "starting"]
            if not users or len(users) != len(starts):
                return False
            insertions = {}
            for turn, start in enumerate(starts):
                end = starts[turn + 1] if turn + 1 < len(starts) else len(events)
                existing = [event for event in events[start:end] if event.get("type") == "user_message"]
                text = users[turn].get("message")
                if not isinstance(text, str) or len(existing) > 1 or (existing and existing[0].get("text") != text):
                    return False
                if not existing:
                    insertions[start] = {"type": "user_message", "text": text}
            if any(event.get("type") == "user_message" for event in events[:starts[0]]):
                return False
            if insertions:
                repaired = []
                for index, event in enumerate(events):
                    repaired.append({**event, "seq": len(repaired) + 1})
                    if index in insertions:
                        repaired.append({**insertions[index], "seq": len(repaired) + 1})
                backup = directory / "events.before-user-messages.jsonl"
                if not backup.exists():
                    with journal.open("rb") as original, backup.open("xb") as saved:
                        shutil.copyfileobj(original, saved)
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                                     prefix=".bubbles-", suffix=".tmp", delete=False) as stream:
                        temporary = Path(stream.name)
                        for event in repaired:
                            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
                    os.replace(temporary, journal)
                finally:
                    if temporary:
                        temporary.unlink(missing_ok=True)
                session["seq"] = repaired[-1]["seq"]
            session["user_message_projection"] = 1
            atomic_json(directory / "session.json", session)
            return bool(insertions)

    def history(self, session_id: str, before: int | None = None, limit: int = 100) -> dict[str, Any]:
        """Read backwards in blocks; never load an entire command transcript."""
        path = self.directory(session_id) / "events.jsonl"
        limit = min(200, max(1, limit))
        rows: list[dict[str, Any]] = []
        used = 0
        more = False
        prefix_open = True
        prefix_done = False
        prefix_anchor = "history"
        prefix_items: set[str] = set()
        with self.lock:
            if path.exists():
                with path.open("rb") as stream:
                    stream.seek(0, 2)
                    position, pending = stream.tell(), b""
                    while position or pending:
                        if position:
                            size = min(64 * 1024, position)
                            position -= size
                            stream.seek(position)
                            pending = stream.read(size) + pending
                        lines = pending.split(b"\n")
                        pending = lines.pop(0) if position else b""
                        for line in reversed(lines):
                            if not line:
                                continue
                            event = json.loads(line)
                            if before is not None and event["seq"] >= before:
                                continue
                            if not more and (len(rows) >= limit or used + len(line) > MAX_PAGE_BYTES):
                                more = True
                            if more:
                                if not prefix_open:
                                    prefix_done = True
                                    break
                                if event["type"] in {"user_message", "notice", "error"}:
                                    prefix_anchor = f"{'user' if event['type'] == 'user_message' else 'notice'}-{event['seq']}"
                                    prefix_done = True
                                    break
                                prefix_items.update(_work_item_keys(event))
                                continue
                            if event["type"] in {"user_message", "notice", "error"}:
                                prefix_open = False
                            elif event["type"] in {"assistant_start", "assistant_message", "thinking_message", "tool_start", "tool_end"}:
                                prefix_open = True
                            rows.append(event)
                            used += len(line)
                        if prefix_done:
                            break
                        if not position and pending:
                            raise ValueError("Invalid extraction journal framing")
            rows.reverse()
        return {"events": rows, "has_more": more,
                "before_seq": rows[0]["seq"] if more and rows else None,
                "activity_prefix": {"anchor": prefix_anchor, "item_keys": sorted(prefix_items)} if more and prefix_open else None}

    def files(self, session_id: str, relative: str = "", offset: int = 0) -> dict[str, Any]:
        root = Path(self.read(session_id)["workspace"])
        path = workspace_path(root, relative)
        if not path.is_dir():
            raise ValueError("Expected a directory")
        import os
        entries = []
        with os.scandir(path) as iterator:
            for entry in iterator:
                if entry.is_symlink() or (getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400):
                    continue
                entries.append({"name": entry.name, "is_dir": entry.is_dir(follow_symlinks=False),
                                "size": entry.stat(follow_symlinks=False).st_size})
                if len(entries) > 10000:
                    raise ValueError("Directory has too many entries; select a smaller output directory")
        entries.sort(key=lambda entry: (not entry["is_dir"], entry["name"].casefold()))
        offset = max(0, offset)
        return {"path": relative, "entries": entries[offset:offset + 200], "total": len(entries),
                "next_offset": offset + 200 if len(entries) > offset + 200 else None}

    def preview(self, session_id: str, relative: str, offset: int = 0) -> dict[str, Any]:
        path = workspace_path(Path(self.read(session_id)["workspace"]), relative)
        if not path.is_file():
            raise ValueError("Expected a file")
        offset = max(0, offset)
        with path.open("rb") as stream:
            stream.seek(offset)
            data = stream.read(64 * 1024)
        binary = b"\0" in data
        return {"path": relative, "offset": offset, "content": data.hex(" ") if binary else data.decode("utf-8", errors="replace"),
                "binary": binary, "size": path.stat().st_size,
                "next_offset": offset + len(data) if offset + len(data) < path.stat().st_size else None}
