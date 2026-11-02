"""Compact global/project memory inspired by dsh-mnemon (MIT; plugins/agent-mnemon)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from GalTransl.Agent.context import _estimate_text_tokens
from GalTransl.Agent.core import AgentStopRequested
from GalTransl.Agent.models import AgentToolError
from GalTransl.Agent.mnemon_prompts import MNEMON_GUIDANCE
from GalTransl.Agent.mnemon_store import Forget, MnemonStore, Remember, memory_directory


def _string(maximum):
    return {"type": "string", "minLength": 1, "maxLength": maximum}


def _tool(name, description, properties, required):
    return {"type": "function", "function": {"name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
            "required": required, "additionalProperties": False}}}


class MnemonSession:
    tools = [
        _tool("mnemon_recall", "Search this Agent type's global preferences and this project's durable facts, including Chinese/Japanese. "
            "Returns complete entries with ids and scope; output shares one available-context budget, with no query-count quota.",
            {"query": _string(1000), "limit": {"type": "integer", "minimum": 1, "maximum": 5}}, ["query"]),
        _tool("mnemon_remember", "Save a short durable fact or preference (max 1000 characters). "
            "Supply an existing id to correct it. critical preferences/conventions are automatically included "
            "in the bounded system snapshot; normal facts are searched on demand. "
            "Snapshot changes apply in new sessions; recall reads current stored facts. "
            "New entries default to normal and project scope. Use global scope for cross-project preferences within this Agent type. "
            "Translation and extraction memories are isolated. "
            "Updates infer scope from id and preserve importance unless explicitly supplied; scope cannot be changed.",
            {"content": _string(1000), "id": _string(100),
             "scope": {"type": "string", "enum": ["project", "global"]},
             "importance": {"type": "string", "enum": ["normal", "critical"]}}, ["content"]),
        _tool("mnemon_forget", "Delete an explicitly withdrawn or obsolete memory by its exact id; scope is inferred from id.",
            {"id": _string(100)}, ["id"]),
    ]
    names = frozenset(tool["function"]["name"] for tool in tools)
    write_names = frozenset({"mnemon_remember", "mnemon_forget"})

    def __init__(self, project_dir, session_id="", stop_event=None, *, directory=None, context_window=128000,
            agent: Literal["translation", "extraction"] = "translation"):
        project_directory = memory_directory(project_dir, agent=agent)
        self.store = MnemonStore(Path(directory) if directory is not None else project_directory, stop_event)
        self.global_store = MnemonStore(memory_directory(agent=agent), self.store.stop_event, scope="global")
        self.session_id = f"{project_directory.name}:{session_id}"
        self.projection_limit = max(0, min(1024, context_window // 16) - _estimate_text_tokens(MNEMON_GUIDANCE) - 64)
        self.recall_limit = min(2048, context_window // 16)
        self._projection: str | None = None

    def close(self):
        self.store.close()
        self.global_store.close()

    def projection(self):
        # The host saves this rendered prefix once for the entire session.
        if self._projection is not None:
            return self._projection
        entries = [entry for entry in [*self.store.entries(), *self.global_store.entries()] if entry["importance"] == "critical"]
        lines = []
        remaining = self.projection_limit
        for entry in entries:
            line = f"[{entry['scope']}; {entry['id']}] {entry['content']}\n"
            cost = _estimate_text_tokens(line)
            if cost > remaining:
                continue
            lines.append(line)
            remaining -= cost
        self._projection = MNEMON_GUIDANCE + f"\n[AGENT MEMORY SNAPSHOT; omitted={len(entries) - len(lines)}]\n" + "".join(lines)
        return self._projection

    def call(self, name, arguments, call_id="", *, token_budget: int | None = None):
        if self.store.stop_event.is_set():
            raise AgentStopRequested()
        if name not in self.names:
            raise AgentToolError("Unknown Mnemon tool")
        schema = next(tool["function"]["parameters"] for tool in self.tools if tool["function"]["name"] == name)
        args = _validate(arguments, schema)
        if name == "mnemon_recall":
            return self._recall(args, self.recall_limit if token_budget is None else min(self.recall_limit, token_budget))
        scope = ("global" if args["id"].startswith("global-") else "project") if "id" in args else args.get("scope", "project")
        if "scope" in args and args["scope"] != scope:
            raise AgentToolError("Memory scope cannot be changed when updating an existing id")
        command_args = {field: value for field, value in args.items() if field != "scope"}
        command = Forget(**command_args) if name == "mnemon_forget" else Remember(**command_args)
        receipt = f"{self.session_id}:{call_id}" if call_id else ""
        selected_store = self.global_store if scope == "global" else self.store
        result = selected_store.mutate(command, receipt)
        return result

    def _recall(self, args, token_budget):
        if token_budget < 64:
            return {"results": [], "notRun": True, "reason": "insufficient_context"}
        matches = sorted([*self.store.search(args["query"]), *self.global_store.search(args["query"])], key=lambda entry: -entry["score"])
        results = []
        for entry in matches[:args.get("limit", 5)]:
            candidate = {"results": [*results, entry], "totalMatches": len(matches), "truncated": len(results) + 1 < len(matches)}
            if _estimate_text_tokens(json.dumps(candidate, ensure_ascii=False)) > token_budget:
                continue
            results.append(entry)
        if matches and not results:
            return {"results": [], "totalMatches": len(matches), "truncated": True, "notRun": True, "reason": "insufficient_context"}
        return {"results": results, "totalMatches": len(matches), "truncated": len(results) < len(matches)}


def _validate(arguments, schema):
    if not isinstance(arguments, dict):
        raise AgentToolError("arguments must be an object")
    unknown = set(arguments) - set(schema["properties"])
    missing = set(schema["required"]) - set(arguments)
    if unknown or missing:
        raise AgentToolError(f"Invalid arguments: unknown fields {sorted(unknown)}, missing fields {sorted(missing)}")
    values = {}
    for field, value in arguments.items():
        spec = schema["properties"][field]
        if spec["type"] == "integer":
            if not isinstance(value, int) or isinstance(value, bool) or not spec["minimum"] <= value <= spec["maximum"]:
                raise AgentToolError(f"Invalid {field}; expected integer {spec['minimum']}..{spec['maximum']}")
            values[field] = value
            continue
        if not isinstance(value, str):
            raise AgentToolError(f"{field} must be a string")
        text = value.strip()
        if not spec.get("minLength", 1) <= len(text) <= spec.get("maxLength", 1000):
            raise AgentToolError(f"Invalid {field} length")
        if "enum" in spec and text not in spec["enum"]:
            raise AgentToolError(f"Invalid {field}; expected {spec['enum']}")
        values[field] = text
    return values
