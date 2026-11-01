"""缓存搜索替换的公共入口；两种动作共用查询和文件范围。"""

from GalTransl.Agent.models import AgentToolError
from GalTransl.Agent.tools.cache import _resolve_cache_replace_files, _tool_patch_transl_cache
from GalTransl.Agent.tools.problems import _load_problem_filter_keys
from GalTransl.Agent.tools.search import _tool_search_transl_cache
from GalTransl.Search import parse_cache_search_conditions


def _tool_search_replace_cache(runner, args):
    unsupported = args.keys() - {
        "action", "query", "field", "files", "conditions", "match_case", "regex", "replacement",
        "dry_run", "clear_comment", "limit", "offset", "order", "context",
        "only_preceding", "reason",
    }
    if unsupported:
        raise AgentToolError("search_replace_cache 不支持参数：" + ", ".join(sorted(unsupported)))
    action = args.get("action")
    if action not in ("search", "replace"):
        raise AgentToolError('search_replace_cache 必须指定 action="search" 或 "replace"')
    match_case = args.get("match_case", False)
    regex = args.get("regex", False)
    dry_run = args.get("dry_run", False)
    if not isinstance(match_case, bool) or not isinstance(dry_run, bool) or not isinstance(regex, bool):
        raise AgentToolError("match_case/regex/dry_run 必须是 boolean")
    if not isinstance(args.get("clear_comment", False), bool):
        raise AgentToolError("clear_comment 必须是 boolean")
    field = args.get("field", "pre_dst")
    query = args.get("query", "")
    if not isinstance(query, str):
        raise AgentToolError("query 必须是字符串")
    normalized = {**args, "field": field, "query": query if regex else query.strip(), "options": {"re": regex, "match_case": match_case}}
    if regex and not normalized["query"]:
        raise AgentToolError("regex=true 需要非空 query；条件自身的正则请用 conditions[].options.re")
    try:
        conditions = parse_cache_search_conditions(normalized)
    except ValueError as exc:
        raise AgentToolError(str(exc)) from exc
    if not conditions:
        raise AgentToolError("query 或 conditions 至少提供一个")
    selectors = args.get("files", "*")
    selectors = [selectors] if isinstance(selectors, str) else selectors
    if not isinstance(selectors, list) or not selectors or any(not isinstance(name, str) or not name.strip() for name in selectors):
        raise AgentToolError("files 必须是文件名/glob，或非空字符串数组")
    if action == "search":
        if any(key in args for key in ("replacement", "dry_run", "clear_comment")):
            raise AgentToolError("replacement/dry_run/clear_comment 仅用于 replace")
        filenames = None if selectors == ["*"] else _resolve_cache_replace_files(runner, [name.strip() for name in selectors])
        return _tool_search_transl_cache(runner, {**normalized, "filenames": filenames})
    if field not in ("pre_dst", "proofread_dst"):
        raise AgentToolError("replace 的 field 只能是 pre_dst 或 proofread_dst；其他字段请放在 conditions 中筛选")
    if not normalized["query"]:
        raise AgentToolError("replace 必须提供非空 query")
    if not isinstance(args.get("replacement"), str):
        raise AgentToolError("replace 必须提供 replacement 字符串；空串删除匹配文本")
    if any(key in args for key in ("limit", "offset", "order", "context", "only_preceding")):
        raise AgentToolError("分页和上下文参数仅用于 search；replace 作用于全部匹配，请用 dry_run 预览")
    filter_keys = _load_problem_filter_keys(runner, runner._project_id(), runner.state.config_file_name)[1] if any(
        condition.field in ("all", "problem") for condition in conditions
    ) else []
    return _tool_patch_transl_cache(runner, {
        "action": "replace", "files": selectors, "fields": [field],
        "old_text": normalized["query"], "new_text": args["replacement"],
        "match_case": match_case,
        "regex": regex,
        "conditions": args.get("conditions"), "_filter_keys": filter_keys,
        "clear_comment": args.get("clear_comment", False),
    }, dry_run=dry_run)
