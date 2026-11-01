"""Cache history HTTP actions; the server supplies its normal cache save path."""

from GalTransl.CacheHistory import (
    begin_edit, cache_path, finish_edit, history_diff, matching_records,
    page, plan_revert, project_lock, record_summary, selected_records,
)


def cache_history_action(project_dir: str, args: dict, save_entries) -> dict:
    action = args.get("action")
    allowed = {
        "list": {"action", "query", "filename", "session_id", "turn_id", "actor_id", "limit", "offset"},
        "diff": {"action", "change_ids", "session_id", "turn_id", "filename", "limit", "offset"},
        "revert": {"action", "change_ids", "session_id", "turn_id", "dry_run", "reason", "_cache_edit"},
    }
    if action not in allowed or args.keys() - allowed[action]:
        raise ValueError("cache_history 参数或 action 无效；支持 list / diff / revert")
    if any(not isinstance(args[key], str) for key in ("query", "filename", "session_id", "turn_id", "actor_id", "reason") if key in args):
        raise ValueError("检索条件和 reason 必须是字符串")
    with project_lock(project_dir):
        if action == "list":
            records = matching_records(project_dir, args)
            return {"action": action, **page([record_summary(record) for record in records], args)}
        records = selected_records(project_dir, args)
        if action == "diff":
            return {"action": action, **history_diff(records, args)}
        if not isinstance(args.get("dry_run", False), bool):
            raise ValueError("dry_run 必须是 boolean")
        planned = plan_revert(project_dir, records)
        result = {"action": action, "updated": 0, "would_update": planned["updated"],
                  "conflicts": planned["conflicts"], "changes": planned["changes"],
                  "reverts": [record["change_id"] for record in records], "dry_run": args.get("dry_run", False)}
        if planned["conflicts"]:
            return {**result, "status": "conflict", "note": "内容已有后续修改，整次撤销未写入；请查看冲突并选择更近的编辑"}
        changed_files = [file for file in planned["files"] if file["before"] != file["after"]]
        if args.get("dry_run") or not changed_files:
            return {**result, "status": "preview" if args.get("dry_run") else "no_change"}
        metadata = {**args.get("_cache_edit", {}), "reason": args.get("reason") or "撤销缓存编辑", "reverts": result["reverts"]}
        config_name = records[0]["config_file_name"]
        journal = begin_edit(project_dir, [file["filename"] for file in planned["files"]], metadata, "revert", config_name)
        # Keep the preimage durable even if one of the ordinary save operations fails.
        completed = False
        try:
            for file in planned["files"]:
                if file["after"] == file["before"]:
                    continue
                path = cache_path(project_dir, file["filename"])
                if file["after"] is None:
                    path.unlink()
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                save_entries(project_dir, file["filename"], file["after"], config_name)
            completed = True
        finally:
            committed = finish_edit(project_dir, journal if completed else {**journal, "reverts": []})
        return {**result, "status": "reverted", "updated": planned["updated"], "history": committed}
