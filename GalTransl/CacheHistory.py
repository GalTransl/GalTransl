"""Durable cache edit history shared by HTTP writers and the agent tools."""

from __future__ import annotations

import copy
import difflib
import json
import os
import re
import tempfile
import threading
import time
from base64 import urlsafe_b64encode
from pathlib import Path
from uuid import uuid4

from GalTransl import CACHE_FOLDERNAME
from GalTransl.RuntimePaths import get_user_data_dir


_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_DERIVED_FIELDS = {"problem", "post_dst_preview"}


def project_lock(project_dir: str):
    key = os.path.normcase(os.path.abspath(project_dir))
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def history_dir(project_dir: str) -> Path:
    key = urlsafe_b64encode(os.path.abspath(project_dir).encode()).decode().rstrip("=")
    return get_user_data_dir() / "cache_history" / key


def cache_path(project_dir: str, filename: str) -> Path:
    if not isinstance(filename, str) or not filename or filename != os.path.basename(filename) or filename in (".", ".."):
        raise ValueError("无效的缓存文件名")
    path = Path(project_dir) / CACHE_FOLDERNAME / filename
    if path.is_symlink():
        raise ValueError("缓存历史不支持符号链接文件")
    return path


def read_cache(project_dir: str, filename: str) -> list[dict] | None:
    path = cache_path(project_dir, filename)
    if not path.exists():
        return None
    data = json.loads(path.read_bytes())
    indexed_entries(data)
    return data


def indexed_entries(entries) -> dict[int, dict]:
    if not isinstance(entries, list) or any(not isinstance(row, dict) or type(row.get("index")) is not int for row in entries):
        raise ValueError("缓存 entries 必须是带整数 index 的条目数组")
    indexed = {row["index"]: row for row in entries}
    if len(indexed) != len(entries):
        raise ValueError("缓存 index 重复，无法记录或撤销编辑")
    return indexed


def atomic_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".cache-history-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def begin_edit(project_dir: str, filenames: list[str], metadata: dict, operation: str, config_name: str = "config.yaml") -> dict:
    record = {
        "change_id": uuid4().hex, "at": time.time(), "status": "pending",
        "operation": operation, "config_file_name": config_name,
        **{key: str(metadata.get(key) or "") for key in ("session_id", "turn_id", "actor_id", "actor_label", "tool_call_id", "reason")},
        "reverts": metadata.get("reverts", []),
        "files": [{"filename": name, "before": read_cache(project_dir, name)} for name in dict.fromkeys(filenames)],
    }
    atomic_json(history_dir(project_dir) / (record["change_id"] + ".json"), record)
    return record


def finish_edit(project_dir: str, record: dict) -> dict | None:
    files = [{**file, "after": read_cache(project_dir, file["filename"])} for file in record["files"]]
    changed = [file for file in files if field_changes(file) or (file["before"] is None) != (file["after"] is None)]
    path = history_dir(project_dir) / (record["change_id"] + ".json")
    if not changed:
        path.unlink(missing_ok=True)
        return None
    compacted = []
    for file in changed:
        if file["before"] is None or file["after"] is None:
            compacted.append(file)
            continue
        indexes = {row["index"] for row in field_changes(file)}
        compacted.append({**file,
                          "positions": {str(row["index"]): position for position, row in enumerate(file["before"]) if row["index"] in indexes},
                          "before": [row for row in file["before"] if row["index"] in indexes],
                          "after": [row for row in file["after"] if row["index"] in indexes]})
    completed = {**record, "status": "applied", "files": compacted}
    atomic_json(path, completed)
    return record_summary(completed)


def field_changes(file: dict) -> list[dict]:
    before = {row["index"]: row for row in file.get("before") or []}
    after = {row["index"]: row for row in file.get("after") or []}
    rows = []
    for index in dict.fromkeys([*before, *after]):
        old, new = before.get(index), after.get(index)
        if old is None or new is None:
            rows.append({"file": file["filename"], "index": index, "field": "entry", "path": f"{file['filename']}#{index}", "before": old, "after": new})
            continue
        rows.extend({"file": file["filename"], "index": index, "field": field, "path": f"{file['filename']}#{index}.{field}",
                     "before": old.get(field), "after": new.get(field),
                     "before_exists": field in old, "after_exists": field in new}
                    for field in sorted((old.keys() | new.keys()) - _DERIVED_FIELDS)
                    if field not in _DERIVED_FIELDS and (old.get(field) != new.get(field) or (field in old) != (field in new)))
    return rows


def record_summary(record: dict) -> dict:
    files = [{"filename": file["filename"], "updated": len({row["index"] for row in field_changes(file)}),
              "deleted": file.get("after", True) is None, "created": file["before"] is None}
             for file in record["files"]]
    return {**{key: value for key, value in record.items() if key != "files"}, "files": files,
            "updated": sum(file["updated"] for file in files)}


def read_record(project_dir: str, change_id: str) -> dict:
    if not isinstance(change_id, str) or not re.fullmatch(r"[0-9a-f]{32}", change_id):
        raise ValueError("无效的 change_id；请先检索缓存编辑历史")
    path = history_dir(project_dir) / (change_id + ".json")
    if not path.is_file():
        raise ValueError(f"缓存编辑记录不存在：{change_id}")
    record = json.loads(path.read_bytes())
    if not isinstance(record, dict) or record.get("change_id") != change_id or not isinstance(record.get("files"), list):
        raise ValueError(f"缓存编辑记录损坏：{change_id}")
    for file in record["files"]:
        cache_path(project_dir, file["filename"])
        for key in ("before", "after"):
            if file.get(key) is not None:
                indexed_entries(file[key])
    return record


def matching_records(project_dir: str, args: dict) -> list[dict]:
    directory = history_dir(project_dir)
    paths = sorted(directory.glob("*.json")) if directory.exists() else []
    records = sorted([read_record(project_dir, path.stem) for path in paths],
                     key=lambda record: (record["at"], record["change_id"]), reverse=True)
    reverted = {}
    for record in records:
        if record["change_id"] not in reverted and record["status"] == "applied":
            reverted.update({change_id: record["change_id"] for change_id in record.get("reverts", [])})
    records = [{**record, "reverted_by": reverted.get(record["change_id"])} for record in records]
    query = str(args.get("query") or "").casefold()
    return sorted([record for record in records
                   if all(key not in args or record.get(key) == args[key] for key in ("session_id", "turn_id", "actor_id"))
                   and (not args.get("filename") or any(file["filename"] == args["filename"] for file in record["files"]))
                   and (not query or query in json.dumps(record, ensure_ascii=False).casefold())],
                  key=lambda record: (record["at"], record["change_id"]), reverse=True)


def selected_records(project_dir: str, args: dict) -> list[dict]:
    ids = args.get("change_ids")
    if ids is not None:
        if not isinstance(ids, list) or not ids or any(not isinstance(item, str) for item in ids):
            raise ValueError("change_ids 必须是非空字符串数组")
        if args.get("turn_id") or args.get("session_id"):
            raise ValueError("change_ids 与 session_id/turn_id 不可混用")
        records = [read_record(project_dir, change_id) for change_id in dict.fromkeys(ids)]
    else:
        if not args.get("session_id") or not args.get("turn_id"):
            raise ValueError("需要 change_ids，或 session_id 与 turn_id")
        records = matching_records(project_dir, args)
    if not records:
        raise ValueError("没有匹配的缓存编辑记录")
    if any(record["status"] != "applied" for record in records):
        raise ValueError("记录包含未完成的编辑，请先核对缓存当前内容")
    return sorted(records, key=lambda record: (record["at"], record["change_id"]), reverse=True)


def page(items: list, args: dict) -> dict:
    limit, offset = args.get("limit", 50), args.get("offset", 0)
    if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
        raise ValueError("limit 必须为 1-200 的整数，offset 必须为非负整数")
    returned = items[offset:offset + limit]
    has_more = offset + len(returned) < len(items)
    return {"total": len(items), "returned": len(returned), "offset": offset, "has_more": has_more,
            **({"next_offset": offset + len(returned)} if has_more else {}), "items": returned}


def history_diff(records: list[dict], args: dict) -> dict:
    combined = {}
    for record in reversed(records):
        for file in record["files"]:
            for row in field_changes(file):
                key = (row["file"], row["index"], row["field"])
                previous = combined.get(key, row)
                combined[key] = {**row, "before": previous["before"],
                                 "before_exists": previous.get("before_exists", previous["before"] is not None)}
    filenames = list(dict.fromkeys(file["filename"] for record in records for file in record["files"]))
    files = []
    for name in filenames:
        if args.get("filename") and name != args["filename"]:
            continue
        changes = [row for row in combined.values() if row["file"] == name and (
            row["before"] != row["after"] or row.get("before_exists") != row.get("after_exists", row["after"] is not None)
        )]
        texts = [json.dumps([{"index": row["index"], "field": row["field"], "value": row[key]}
                             for row in changes if row.get(key + "_exists", row[key] is not None)],
                            ensure_ascii=False, indent=2).splitlines() for key in ("before", "after")]
        diff = list(difflib.unified_diff(*texts, fromfile="before/" + name, tofile="after/" + name, lineterm=""))
        files.append({"filename": name, "updated": len({row["index"] for row in changes}), "changes": changes,
                      "diff": "\n".join(diff), "added": sum(line.startswith("+") and not line.startswith("+++") for line in diff),
                      "removed": sum(line.startswith("-") and not line.startswith("---") for line in diff)})
    return {"change_ids": [record["change_id"] for record in records], **page(files, {"limit": 1, **args})}


def plan_revert(project_dir: str, records: list[dict]) -> dict:
    filenames = list(dict.fromkeys(file["filename"] for record in records for file in record["files"]))
    original = {name: read_cache(project_dir, name) for name in filenames}
    planned = copy.deepcopy(original)
    conflicts = []
    for record in records:
        for file in record["files"]:
            name = file["filename"]
            current = planned[name]
            if file["before"] is None or file["after"] is None:
                if current != file["after"]:
                    conflicts.append({"file": name, "change_id": record["change_id"], "reason": "文件在这次编辑后已改变"})
                    continue
                planned[name] = copy.deepcopy(file["before"])
                continue
            indexed = {row["index"]: row for row in current or []}
            for row in field_changes(file):
                index, field = row["index"], row["field"]
                entry = indexed.get(index)
                if field == "entry":
                    if entry != row["after"]:
                        conflicts.append({"file": name, "index": index, "reason": "条目在这次编辑后已改变"})
                        continue
                    if row["before"] is None:
                        indexed.pop(index, None)
                    else:
                        indexed[index] = copy.deepcopy(row["before"])
                    continue
                if entry is None or entry.get(field) != row["after"] or (field in entry) != row["after_exists"]:
                    conflicts.append({"file": name, "index": index, "field": field, "reason": "字段在这次编辑后已改变"})
                    continue
                if row["before_exists"]:
                    entry[field] = copy.deepcopy(row["before"])
                else:
                    entry.pop(field, None)
            # Preserve the current ordering and put restored rows at their former positions.
            order = dict.fromkeys([row["index"] for row in current or []])
            for fallback_position, entry in enumerate(file["before"]):
                position = file.get("positions", {}).get(str(entry["index"]), fallback_position)
                if entry["index"] not in order and entry["index"] in indexed:
                    values = list(order)
                    values.insert(min(position, len(values)), entry["index"])
                    order = dict.fromkeys(values)
            planned[name] = [indexed[index] for index in order if index in indexed]
    changes = [row for name in filenames for row in field_changes({"filename": name, "before": original[name], "after": planned[name]})]
    return {"files": [{"filename": name, "before": original[name], "after": planned[name]} for name in filenames],
            "changes": changes, "conflicts": conflicts, "updated": len({(row["file"], row["index"]) for row in changes})}
