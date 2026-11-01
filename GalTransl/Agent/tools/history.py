"""Queryable cache history and reversible edits, including child agents."""

from GalTransl.Agent.models import AgentToolError


def _tool_cache_history(runner, args):
    if "_cache_edit" in args:
        raise AgentToolError("_cache_edit 是内部编辑来源，不能由工具指定")
    result = runner._http_post(f"/api/projects/{runner._project_id()}/cache/history", args)
    return result
