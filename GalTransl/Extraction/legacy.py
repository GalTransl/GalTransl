"""One-time, read-only import of development sessions from the removed sidecar."""
from __future__ import annotations

import json
from pathlib import Path


def previous_messages(session, directory):
    raw = session.get("pi_session_file")
    if not raw:
        return []
    path = Path(raw).resolve(strict=True)
    if not path.is_relative_to(directory.resolve()) or path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("Legacy extraction history is outside its session or exceeds the import budget")
    entries = []
    with path.open("rb") as stream:
        while line := stream.readline(1024 * 1024 + 1):
            if len(line) > 1024 * 1024:
                raise ValueError("Legacy extraction history has an oversized record")
            entry = json.loads(line)
            if isinstance(entry, dict) and entry.get("id"):
                entries.append(entry)
    # Restore the selected leaf's ancestry instead of merging abandoned branches.
    by_id = {entry["id"]: entry for entry in entries}
    branch, seen = [], set()
    current = entries[-1] if entries else None
    while current and current["id"] not in seen:
        branch.append(current)
        seen.add(current["id"])
        current = by_id.get(current.get("parentId"))
    branch.reverse()
    messages = []
    for entry in branch:
        if entry.get("type") in {"compaction", "branch_summary"}:
            summary = str(entry.get("summary", ""))
            if summary:
                first = entry.get("firstKeptEntryId")
                kept_start = next((i for i, item in enumerate(branch) if item["id"] == first), len(branch))
                kept_ids = {item["id"] for item in branch[kept_start:]}
                messages = [(ident, value) for ident, value in messages if ident in kept_ids]
                messages.insert(0, (entry["id"], {"role": "user", "content": "Previous extraction summary:\n" + summary}))
            continue
        message = entry.get("message") if entry.get("type") == "message" else None
        if not isinstance(message, dict):
            continue
        content = message.get("content", [])
        text = content if isinstance(content, str) else "\n".join(str(part.get("text", "")) for part in content if part.get("type") == "text")
        role = message.get("role")
        if role in {"user", "assistant"}:
            converted = {"role": role, "content": text}
            calls = [{"id": part["id"], "type": "function", "function": {"name": part["name"],
                     "arguments": json.dumps(part.get("arguments", {}), ensure_ascii=False)}}
                     for part in content if isinstance(part, dict) and part.get("type") == "toolCall"] if isinstance(content, list) else []
            if calls and role == "assistant":
                converted["tool_calls"] = calls
            messages.append((entry["id"], converted))
        elif role == "toolResult":
            messages.append((entry["id"], {"role": "tool", "tool_call_id": message["toolCallId"], "content": text}))
    return [message for _, message in messages]
