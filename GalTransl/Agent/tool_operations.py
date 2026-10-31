"""Public tool actions mapped to the existing operation handlers."""

from GalTransl.Agent.models import AgentToolError


TOOL_OPERATIONS = {
    "input": {"list": "list_input_files", "read": "read_input_file", "search": "search_input"},
    "guideline": {"read": "read_guideline", "append": "write_project_guideline", "replace": "write_project_guideline", "overwrite": "write_project_guideline"},
    "dictionary": {"list": "list_dict_files", "read": "read_dict", "set": "save_dict", "delete": "save_dict", "overwrite": "save_dict"},
    "names": {"read": "get_name_table", "set": "save_name_table", "overwrite": "save_name_table"},
    "translation": {"start": "start_translation", "status": "get_runtime", "wait": "wait", "stop": "stop_translation"},
    "edit_transl_cache": {"set": "patch_transl_cache", "replace": "patch_transl_cache", "delete": "delete_transl_cache"},
    "output": {"read": "read_output", "search": "search_output_files"},
    "subagents": {"run": "run_subagents", "changes": "read_proofread_changes", "revert": "revert_proofread_changes"},
}

PROBLEM_OPERATIONS = {"items": "list_problems", "filter": "manage_problem_filter", "white_list": "manage_problem_white_list"}


def tool_operation(name: str, args: dict[str, object]) -> str:
    """Resolve before permission checks; a malformed action never reaches a handler."""
    if name == "problems":
        target = args.get("target", "items")
        if not isinstance(target, str) or target not in PROBLEM_OPERATIONS:
            raise AgentToolError("problems.target 必须为 items / filter / white_list")
        actions = ("list",) if target == "items" else ("list", "set", "delete")
        if args.get("action") not in actions:
            raise AgentToolError(f"problems({target}) 必须显式指定 action：{' / '.join(actions)}")
        return PROBLEM_OPERATIONS[target]
    actions = TOOL_OPERATIONS.get(name)
    if actions is None:
        return name
    action = args.get("action")
    if not isinstance(action, str) or action not in actions:
        raise AgentToolError(f"{name} 必须显式指定 action：{' / '.join(actions)}")
    if name == "guideline" and action != "read" and args.get("scope", "project") != "project":
        raise AgentToolError("只能修改 scope=project 的项目规范")
    if name == "edit_transl_cache" and action == "delete" and any(
        key in args for key in ("patches", "files", "old_text", "new_text", "replace_all", "fields", "clear_comment")
    ):
        raise AgentToolError("delete 只使用 filename/indexes，不能混用 set/replace 参数")
    return actions[action]
