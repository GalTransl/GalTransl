"""One local SQLite memory store; no generated views or external providers."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import uuid

from GalTransl.Agent.core import AgentStopRequested
from GalTransl.Agent.models import AgentToolError
from GalTransl.RuntimePaths import get_user_data_dir


def memory_directory(project_dir: str = "") -> Path:
    root = get_user_data_dir() / "mnemon"
    if not project_dir:
        return root / "global"
    identity = os.path.normcase(str(Path(project_dir).resolve()))
    return root / hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class Remember:
    content: str
    id: str = ""
    importance: str | None = None


@dataclass(frozen=True)
class Forget:
    id: str


class MnemonStore:
    def __init__(self, directory: Path, stop_event=None, *, scope="project"):
        self.directory = directory
        self.scope = scope
        self.stop_event = stop_event or threading.Event()
        directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(directory / "mnemon.sqlite3", timeout=15, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY, content TEXT NOT NULL, digest TEXT NOT NULL UNIQUE,
                    importance TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, request TEXT NOT NULL, result TEXT NOT NULL);
            """)

    def close(self):
        with self._lock:
            self._db.close()

    def entries(self):
        with self._lock:
            return [{**dict(row), "scope": self.scope} for row in self._db.execute(
                "SELECT id,content,importance,created_at,updated_at FROM memories "
                "ORDER BY importance='critical' DESC,updated_at DESC,id")]

    def search(self, query):
        terms = _terms(query)
        scored = [{"id": entry["id"], "scope": entry["scope"], "content": entry["content"], "importance": entry["importance"],
            "score": len(terms & _terms(entry["content"])) / max(1, len(terms))} for entry in self.entries()]
        return sorted([entry for entry in scored if entry["score"] > 0], key=lambda entry: -entry["score"])

    def mutate(self, command: Remember | Forget, receipt_id=""):
        request = json.dumps({"kind": type(command).__name__, "arguments": asdict(command)}, ensure_ascii=False, sort_keys=True)
        with self._lock, self._db:
            # Acquire the project write lock before checking receipts or duplicates.
            self._db.execute("BEGIN IMMEDIATE")
            if self.stop_event.is_set():
                raise AgentStopRequested()
            previous = self._db.execute("SELECT request,result FROM receipts WHERE id=?", (receipt_id,)).fetchone() if receipt_id else None
            if previous is not None:
                if previous["request"] != request:
                    raise AgentToolError("Mnemon tool call id was reused with different arguments")
                return json.loads(previous["result"])
            result = {**self._apply(command), "scope": self.scope}
            if self.stop_event.is_set():
                raise AgentStopRequested()
            if receipt_id:
                self._db.execute("INSERT INTO receipts VALUES (?,?,?)", (receipt_id, request, json.dumps(result, ensure_ascii=False)))
        return result

    def _apply(self, command):
        previous = self._db.execute("SELECT * FROM memories WHERE id=?", (command.id,)).fetchone() if command.id else None
        if command.id and previous is None:
            raise AgentToolError(f"Unknown memory id in {self.scope} scope")
        if isinstance(command, Forget):
            self._db.execute("DELETE FROM memories WHERE id=?", (command.id,))
            return {"success": True, "id": command.id}
        digest = hashlib.sha256(" ".join(command.content.split()).encode("utf-8")).hexdigest()
        duplicate = self._db.execute("SELECT * FROM memories WHERE digest=?", (digest,)).fetchone()
        if previous is not None and duplicate is not None and previous["id"] != duplicate["id"]:
            raise AgentToolError("Update duplicates another memory; remove the obsolete entry by id instead")
        existing = previous if previous is not None else duplicate
        importance = command.importance or (existing["importance"] if existing is not None else "normal")
        if previous is None and duplicate is not None:
            if importance != duplicate["importance"]:
                self._db.execute("UPDATE memories SET importance=?,updated_at=? WHERE id=?",
                    (importance, datetime.now(timezone.utc).isoformat(), duplicate["id"]))
            return {"success": True, "id": duplicate["id"], "importance": importance, "duplicate": True}
        now = datetime.now(timezone.utc).isoformat()
        memory_id = previous["id"] if previous is not None else ("global-" if self.scope == "global" else "mem-") + uuid.uuid4().hex
        self._db.execute("INSERT OR REPLACE INTO memories VALUES (?,?,?,?,?,?)", (memory_id, command.content, digest, importance,
            previous["created_at"] if previous is not None else now, now))
        return {"success": True, "id": memory_id, "importance": importance, "updated": previous is not None}


def _terms(text):
    # Bigrams keep unsegmented Chinese/Japanese searchable without a tokenizer.
    runs = re.findall(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]+|[^\W_\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]+", text.casefold())
    return {term for run in runs for term in (
        [run, *[run[index:index + 2] for index in range(len(run) - 1)]]
        if re.match(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", run) else [run])}
