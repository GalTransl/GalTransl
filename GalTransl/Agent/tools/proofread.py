"""Scoped proofreading edits and their durable, queryable change records."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
import urllib.parse
from typing import Any
from uuid import uuid4

from GalTransl.Agent import session_store
from GalTransl.Agent.core import AgentStopRequested
from GalTransl.Agent.models import AgentToolError
from GalTransl.Agent.tools.cache import (
    _cache_file_lock, _group_cache_patches_by_file, _patch_one_cache_file_locked,
    _tool_read_transl_cache, _plan_cache_patches,
)
from GalTransl.Agent.tools.cache_fields import _cache_field_value, _PATCHABLE_FIELDS
from GalTransl.Agent.tools.common import _parse_index_spec


def _snapshot(entry: dict) -> dict:
    # Ignore derived problem/preview fields, but detect changes to the source,
    # speaker, translation and outstanding comments since the last explicit read.
    fields = ("pre_src", "post_src", "name", "pre_dst", "proofread_dst",
              "proofread_comment", "trans_by", "proofread_by")
    return {key: copy.deepcopy(_cache_field_value(entry, key)) for key in fields}


def _record_dir(runner: Any) -> str:
    return os.path.join(session_store.project_dir_for(runner.state.project_dir), "proofread")


def _record_path(runner: Any, change_id: str, *, task_id: str | None = None) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", change_id):
        raise AgentToolError("无效的 change_id；请从 read_proofread_changes 获取")
    directory = _record_dir(runner)
    if task_id is not None:
        # Task prefixes allow filtering before opening records, without a
        # separate mutable index that could drift from the audit log.
        return os.path.join(directory, hashlib.sha256(task_id.encode()).hexdigest() + "-" + change_id + ".json")
    legacy = os.path.join(directory, change_id + ".json")
    if os.path.isfile(legacy):
        return legacy
    for name in _record_names(runner):
        if re.fullmatch(r"[0-9a-f]{64}-" + change_id + r"\.json", name):
            return os.path.join(directory, name)
    return legacy


def _write_record(runner: Any, record: dict) -> None:
    path = _record_path(runner, record["change_id"], task_id=record["task_id"])
    _atomic_json(path, record)


def _atomic_json(path: str, record: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".proofread-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _review_path(runner: Any, task_id: str) -> str:
    return os.path.join(_record_dir(runner), "review-" + hashlib.sha256(task_id.encode()).hexdigest() + ".json")


def _indexed_entries(data: dict) -> dict[int, dict]:
    """Reject malformed caches before a whole-file save can misaddress a row."""
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        raise AgentToolError("缓存 entries 必须是数组")
    indexed = {}
    for position, entry in enumerate(entries, 1):
        if not isinstance(entry, dict) or type(entry.get("index")) is not int:
            raise AgentToolError(f"缓存第 {position} 个条目缺少有效的整数 index，请先修复缓存")
        index = entry["index"]
        if index in indexed:
            raise AgentToolError(f"缓存 index #{index} 重复，无法确定修改目标，请先修复缓存")
        indexed[index] = entry
    return indexed


class _ReadSnapshot:
    """Reuse the exact HTTP snapshot for projection and subsequent edit checks."""

    def __init__(self, parent: Any, path: str, data: dict) -> None:
        self.parent, self.path, self.data = parent, path, data

    def __getattr__(self, name: str) -> Any:
        return getattr(self.parent, name)

    def _http_get(self, path: str) -> Any:
        return copy.deepcopy(self.data) if path == self.path else self.parent._http_get(path)


class ProofreadFixer:
    def __init__(self, runner: Any, task_id: str, stop_event: threading.Event) -> None:
        self.runner, self.task_id, self.stop_event = runner, task_id, stop_event
        self.snapshots: dict[tuple[str, int], dict] = {}
        self.modified: set[tuple[str, int]] = set()
        self.unverified: set[tuple[str, int]] = set()
        self.remaining: dict[tuple[str, int], str] = {}
        self.change_ids: list[str] = []
        self.issues: dict[tuple[str, int], str] = {}

    def review_entries(self) -> list[dict]:
        keys = set(self.issues) | self.unverified | set(self.remaining)
        keys.update(key for key, value in self.snapshots.items() if value.get("proofread_comment"))
        rows = []
        for key in sorted(keys):
            reasons = [self.issues.get(key), self.snapshots.get(key, {}).get("proofread_comment"),
                       self.remaining.get(key), "自动检测未验证" if key in self.unverified else ""]
            rows.append({"file": key[0], "index": key[1], "reason": "；".join(dict.fromkeys(str(r) for r in reasons if r))})
        return rows

    def save_report(self, result: dict, review: list[dict]) -> None:
        _atomic_json(_review_path(self.runner, self.task_id), {
            "task_id": self.task_id, "at": time.time(), "status": result["status"],
            "modified_count": len(self.modified), "needs_review_count": len(review),
            "review": review,
        })

    def check_stopped(self) -> None:
        if self.stop_event.is_set():
            raise AgentStopRequested()

    def read(self, runner: Any, args: dict) -> Any:
        if str(args.get("action") or "read") != "read":
            return _tool_read_transl_cache(runner, args)
        filename = str(args.get("filename") or "").strip()
        path = f"/api/projects/{runner._project_id()}/cache/{urllib.parse.quote(filename)}"
        data = runner._http_get(path)
        originals = _indexed_entries(data)
        # Never authorize a write based on an intentionally hidden translation.
        fields = args.get("fields")
        if isinstance(fields, list) and "*" not in fields:
            args = {**args, "fields": list(dict.fromkeys([
                *fields, "post_src", "pre_dst", "proofread_dst", "proofread_comment",
            ]))}
        result = _tool_read_transl_cache(_ReadSnapshot(runner, path, data), args)
        for row in result.get("entries", []):
            # Context rows ("12*") are not edit targets.
            index = row.get("index")
            if isinstance(index, int) and index in originals:
                self.snapshots[filename, index] = _snapshot(originals[index])
        return result

    def patch(self, runner: Any, args: dict) -> dict:
        targets = _group_cache_patches_by_file(args)
        files: list[dict] = []
        for filename, patches in targets:
            self.check_stopped()
            try:
                with _cache_file_lock(runner, filename):
                    files.append(self._patch_file(filename, patches))
            except AgentStopRequested:
                raise
            except (AgentToolError, OSError) as exc:
                files.append({"filename": filename, "updated": 0, "changes": [], "error": str(exc)})
                for item in patches:
                    self.issues[filename, item["index"]] = str(exc)
        return {
            "updated": sum(row.get("updated", 0) for row in files), "files": files,
            "changes": [c for row in files for c in row.get("changes", [])],
        }

    def _patch_file(self, filename: str, patches: list[dict]) -> dict:
        runner = self.runner
        path = f"/api/projects/{runner._project_id()}/cache/{urllib.parse.quote(filename)}"
        data = runner._http_get(path)
        entries = _indexed_entries(data)
        converted: list[dict] = []
        seen: set[int] = set()
        for patch in patches:
            index = patch["index"]
            if index in seen:
                raise AgentToolError(f"#{index} 重复出现：一次只提交一个修改")
            seen.add(index)
            entry = entries.get(index)
            previous = self.snapshots.get((filename, index))
            if previous is None:
                raise AgentToolError(f"#{index} 尚未显式读取；先 read_transl_cache 再修改")
            if entry is None or _snapshot(entry) != previous:
                raise AgentToolError(f"#{index} 内容已变化，修改未提交；重新读取后再判断")
            unknown = set(patch) - {"file", "index", "dst", "proofread_comment"}
            if unknown:
                raise AgentToolError(f"校对子代理只允许 dst / proofread_comment，不接受：{', '.join(sorted(unknown))}")
            new = {"index": index}
            for key in ("dst", "proofread_comment"):
                if key not in patch:
                    continue
                value = patch[key]
                if not isinstance(value, str):
                    raise AgentToolError(f"#{index} 的 {key} 必须是字符串")
                field = ("proofread_dst" if _cache_field_value(entry, "proofread_dst") else "pre_dst") if key == "dst" else key
                if key == "dst" and not value.strip():
                    raise AgentToolError(f"#{index} 不允许把译文改为空")
                if value != (_cache_field_value(entry, field) or ""):
                    new[field] = value
            if new.get("proofread_comment") == "" and not ({"pre_dst", "proofread_dst"} & new.keys()):
                raise AgentToolError(f"#{index} 只能在实际修改译文时清空已处理的批注")
            if len(new) > 1:
                converted.append(new)
        if not converted:
            return {"filename": filename, "updated": 0, "changes": [], "note": "没有实际变化"}
        return self._commit(filename, path, data, converted)

    def _commit(self, filename: str, path: str, data: dict, patches: list[dict], *, undo_of: str = "") -> dict:
        runner, owner = self.runner, self
        before = _indexed_entries(data)
        record: dict = {}

        class Writer(_ReadSnapshot):
            def _http_post(self, url: str, body: dict) -> Any:
                owner.check_stopped()
                after = _indexed_entries(body)
                record.update({
                    "change_id": uuid4().hex, "task_id": owner.task_id,
                    "session_id": getattr(runner.state, "session_id", ""),
                    "filename": filename, "at": time.time(), "status": "pending",
                    "undo_of": undo_of,
                    "entries": [{"index": p["index"], "before": _snapshot(before[p["index"]]),
                                 "after": _snapshot(after[p["index"]])} for p in patches],
                })
                # Persist before sending: a failed/ambiguous HTTP response must
                # not erase the only copy of the old translation.
                _write_record(runner, record)
                owner.check_stopped()
                try:
                    response = runner._http_post(url, body)
                except Exception:
                    record["status"] = "uncertain"
                    _write_record(runner, record)
                    raise
                record["status"] = "applied"
                record["verification"] = response.get("verification", "checked" if isinstance(response.get("entries"), list) else "unknown")
                try:
                    _write_record(runner, record)
                except OSError as exc:
                    record["audit_error"] = str(exc)  # pending record still retains both versions
                owner.change_ids.append(record["change_id"])
                return response

        proxy = Writer(runner, path, data)
        result = _patch_one_cache_file_locked(
            proxy, runner._project_id(), filename, patches, _PATCHABLE_FIELDS, qualify=True,
        )
        result["change_id"] = record.get("change_id", "")
        if record.get("audit_error"):
            result["audit_error"] = record["audit_error"]
        for row in record.get("entries", []):
            key = (filename, row["index"])
            self.snapshots[key] = row["after"]
            self.issues.pop(key, None)
            if any(row["before"][f] != row["after"][f] for f in ("pre_dst", "proofread_dst")):
                self.modified.add(key)
            if result.get("verification") != "checked":
                self.unverified.add(key)
            else:
                self.unverified.discard(key)
            self.remaining.pop(key, None)
        for problem in result.get("problems", []):
            self.remaining[filename, problem["index"]] = problem["problem"]
        return result


def _record_names(runner: Any) -> list[str]:
    try:
        return os.listdir(_record_dir(runner))
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise AgentToolError(f"无法列出校对记录：{exc}") from exc


def _read_record(path: str, *, review: bool = False) -> dict:
    try:
        with open(path, encoding="utf-8") as stream:
            record = json.load(stream)
        if not isinstance(record, dict) or not isinstance(record.get("task_id"), str):
            raise ValueError("缺少 task_id")
        if type(record.get("at")) not in (int, float) or not math.isfinite(record["at"]):
            raise ValueError("无效的 at")
        if not isinstance(record.get("status"), str):
            raise ValueError("缺少 status")
        if review:
            rows = record.get("review")
            if not isinstance(rows, list) or any(
                not isinstance(row, dict) or type(row.get("index")) is not int
                or not isinstance(row.get("file"), str) or not isinstance(row.get("reason"), str)
                for row in rows
            ):
                raise ValueError("无效的 review")
            if any(type(record.get(key)) is not int or record[key] < 0
                   for key in ("modified_count", "needs_review_count")):
                raise ValueError("无效的审查计数")
        else:
            change_id = record.get("change_id")
            if not isinstance(change_id, str) or not re.fullmatch(r"[0-9a-f]{32}", change_id):
                raise ValueError("无效的 change_id")
            if not isinstance(record.get("filename"), str):
                raise ValueError("缺少 filename")
            rows = record.get("entries")
            fields = _snapshot({}).keys()
            if not isinstance(rows, list) or any(
                not isinstance(row, dict) or type(row.get("index")) is not int
                or any(not isinstance(row.get(key), dict) or not fields <= row[key].keys()
                       for key in ("before", "after")) for row in rows
            ):
                raise ValueError("无效的修改快照")
        return record
    except (OSError, ValueError, OverflowError) as exc:
        raise AgentToolError(f"无法读取校对记录 {os.path.basename(path)}：{exc}") from exc


def _try_read_record(path: str, warnings: list[str], *, review: bool = False,
                     missing_ok: bool = False) -> dict | None:
    try:
        return _read_record(path, review=review)
    except AgentToolError as exc:
        if not (missing_ok and isinstance(exc.__cause__, FileNotFoundError)):
            warnings.append(str(exc))
        return None


def _load_records(runner: Any, task_id: str, warnings: list[str]) -> list[dict]:
    directory = _record_dir(runner)
    prefix = hashlib.sha256(task_id.encode()).hexdigest() + "-" if task_id else ""
    records = []
    for name in _record_names(runner):
        legacy = re.fullmatch(r"[0-9a-f]{32}\.json", name)
        if not legacy and not re.fullmatch(r"[0-9a-f]{64}-[0-9a-f]{32}\.json", name):
            continue
        # Legacy UUID-only filenames still need inspection for compatibility.
        if not legacy and prefix and not name.startswith(prefix):
            continue
        record = _try_read_record(os.path.join(directory, name), warnings)
        if record is not None and (not task_id or record["task_id"] == task_id):
            records.append(record)
    return sorted(records, key=lambda r: (r["at"], r["change_id"]))


def _tool_read_proofread_changes(runner: Any, args: dict) -> dict:
    task_id = str(args.get("task_id") or "")
    view = str(args.get("view") or "changes")
    if view not in ("changes", "review"):
        raise AgentToolError("view 必须为 changes 或 review")
    warnings: list[str] = []

    def with_warnings(result: dict) -> dict:
        if warnings:
            result.update({"warnings": warnings[:10], "skipped_record_count": len(warnings),
                           "incomplete": True})
        return result

    if not task_id:
        groups: dict[str, dict] = {}
        records = _load_records(runner, "", warnings) if view == "changes" else []
        for record in records:
            group = groups.setdefault(record["task_id"], {"task_id": record["task_id"], "batches": 0, "entries": 0})
            group["batches"] += 1
            group["entries"] += len(record["entries"])
        for name in _record_names(runner):
            if not re.fullmatch(r"review-[0-9a-f]{64}\.json", name):
                continue
            report = _try_read_record(os.path.join(_record_dir(runner), name), warnings, review=True)
            if report is None:
                continue
            group = groups.setdefault(report["task_id"], {
                "task_id": report["task_id"], **({"batches": 0, "entries": 0} if view == "changes" else {}),
            })
            group.update({k: report[k] for k in ("modified_count", "needs_review_count", "status")})
        return with_warnings({"tasks": list(groups.values())[-50:], "total_tasks": len(groups),
                "note": "指定 task_id 分页查看修改前后内容；pending/uncertain 表示提交状态未确认。"})
    try:
        offset = max(0, int(args.get("offset", 0)))
        limit = max(1, min(50, int(args.get("limit", 20))))
    except (TypeError, ValueError) as exc:
        raise AgentToolError("offset/limit 必须为整数") from exc
    if view == "review":
        path = _review_path(runner, task_id)
        record = _try_read_record(path, warnings, review=True, missing_ok=True)
        rows = record["review"] if record is not None else []
        return with_warnings({"task_id": task_id, "total": len(rows), "offset": offset,
                "has_more": offset + limit < len(rows), "needs_review": rows[offset:offset + limit]})
    records = _load_records(runner, task_id, warnings)
    rows = [{**{k: v for k, v in record.items() if k != "entries"}, **entry}
            for record in records if record["task_id"] == task_id for entry in record["entries"]]
    return with_warnings({"task_id": task_id, "total": len(rows), "offset": offset,
            "has_more": offset + limit < len(rows), "changes": rows[offset:offset + limit]})


def _tool_revert_proofread_changes(runner: Any, args: dict, *, preview: bool = False) -> dict:
    change_id = str(args.get("change_id") or "")
    record = _read_record(_record_path(runner, change_id))
    spec = str(args.get("indexes") or "").strip()
    wanted = _parse_index_spec(spec) if spec else {e["index"] for e in record["entries"]}
    rows = [e for e in record["entries"] if e["index"] in wanted]
    if not rows or wanted - {e["index"] for e in rows}:
        raise AgentToolError("indexes 必须属于这份修改记录")
    filename = record["filename"]
    fixer = ProofreadFixer(runner, "revert-" + change_id, runner.stop_event)
    with _cache_file_lock(runner, filename):
        fixer.check_stopped()
        path = f"/api/projects/{runner._project_id()}/cache/{urllib.parse.quote(filename)}"
        data = runner._http_get(path)
        current = _indexed_entries(data)
        patches = []
        for row in rows:
            index = row["index"]
            if index not in current or _snapshot(current[index]) != row["after"]:
                raise AgentToolError(f"#{index} 已变化或已撤销，不能覆盖后续编辑")
            updates = {f: row["before"][f] or "" for f in _PATCHABLE_FIELDS
                       if row["before"][f] != row["after"][f]}
            if updates:
                patches.append({"index": index, **updates})
        if not patches:
            raise AgentToolError("这份记录没有可撤销的译文/批注修改")
        if preview:
            planned = _plan_cache_patches(data["entries"], patches, _PATCHABLE_FIELDS)
            return {"changes": [{**c, "file": filename, "path": filename + c["path"]} for c in planned["changes"]]}
        result = fixer._commit(filename, path, data, patches, undo_of=change_id)
    return {"updated": result["updated"], "files": [result], "changes": result["changes"]}
