"""工具名到实现函数的注册表，以及工具调用理由（reason）的附加规则。"""

from __future__ import annotations

from typing import Any, Callable, TYPE_CHECKING

from GalTransl.Agent.subagent import _tool_run_subagents
from GalTransl.Agent.tools.ask import _tool_ask_user, _tool_read_history_archive
from GalTransl.Agent.tools.cache import (
    _tool_delete_transl_cache,
    _tool_patch_transl_cache,
    _tool_read_output,
    _tool_read_transl_cache,
)
from GalTransl.Agent.tools.dicts import (
    _tool_list_dict_files,
    _tool_read_dict,
    _tool_save_dict,
)
from GalTransl.Agent.tools.input import _tool_list_input_files, _tool_read_input_file
from GalTransl.Agent.tools.jobs import (
    _tool_get_runtime,
    _tool_start_translation,
    _tool_stop_translation,
    _tool_wait,
)
from GalTransl.Agent.tools.names import _tool_get_name_table, _tool_save_name_table
from GalTransl.Agent.tools.problems import (
    _tool_list_problems,
    _tool_manage_problem_filter,
    _tool_manage_problem_white_list,
)
from GalTransl.Agent.tools.project import (
    _tool_get_project_overview,
    _tool_read_guideline,
    _tool_update_project_config,
    _tool_write_project_guideline,
)
from GalTransl.Agent.tools.search import _tool_search_input
from GalTransl.Agent.tools.plugin_settings import _tool_get_plugin_settings
from GalTransl.Agent.tools.proofread import _tool_read_proofread_changes, _tool_revert_proofread_changes

if TYPE_CHECKING:
    from GalTransl.Agent.runner import AgentRunner


_TOOL_HANDLERS: dict[str, Callable[[AgentRunner, dict[str, Any]], Any]] = {
    "get_project_overview": _tool_get_project_overview,
    "get_plugin_settings": _tool_get_plugin_settings,
    "update_project_config": _tool_update_project_config,
    "list_input_files": _tool_list_input_files,
    "read_input_file": _tool_read_input_file,
    "search_input": _tool_search_input,
    "read_guideline": _tool_read_guideline,
    "write_project_guideline": _tool_write_project_guideline,
    "list_dict_files": _tool_list_dict_files,
    "read_dict": _tool_read_dict,
    "save_dict": _tool_save_dict,
    "get_name_table": _tool_get_name_table,
    "save_name_table": _tool_save_name_table,
    "start_translation": _tool_start_translation,
    "stop_translation": _tool_stop_translation,
    "wait": _tool_wait,
    "get_runtime": _tool_get_runtime,
    "list_problems": _tool_list_problems,
    "manage_problem_filter": _tool_manage_problem_filter,
    "manage_problem_white_list": _tool_manage_problem_white_list,
    "read_transl_cache": _tool_read_transl_cache,
    "read_output": _tool_read_output,
    "delete_transl_cache": _tool_delete_transl_cache,
    "patch_transl_cache": _tool_patch_transl_cache,
    "read_history_archive": _tool_read_history_archive,
    "ask_user": _tool_ask_user,
    "run_subagents": _tool_run_subagents,
    "read_proofread_changes": _tool_read_proofread_changes,
    "revert_proofread_changes": _tool_revert_proofread_changes,
}


# 已合并掉的旧工具名 → 现在该怎么调。旧会话的历史里还留着这些调用，模型可能照着历史再调一次：
# 回一句明确的改法，比"未知工具"好接得多。
_RETIRED_TOOLS: dict[str, str] = {
    "list_transl_cache": 'list_transl_cache 已并入 read_transl_cache：改用 read_transl_cache(action="list", ...)。',
    "search_transl_cache": 'search_transl_cache 已并入 read_transl_cache：改用 read_transl_cache(action="search", query=..., ...)。',
    "create_dict_file": 'create_dict_file 已并入 save_dict：改用 save_dict(file_key="<文件名>", category="pre|gpt|post", content=...)，文件不存在时会先新建并登记。',
}


# 带 reason 入参的工具：写类（改配置/规范/字典/缓存）+ start_translation。读类工具没有这个
# 参数、模型也不会传，这里再列一次是为了在分发时确认"这次调用确实能填原因"，而不是只靠
# schema 声明。start_translation 的 reason 还多一个用处：启动翻译是要审批的动作，理由会跟着
# arguments 一起进那张权限卡（见 request_permission），用户批之前先看到为什么。
_TOOLS_WITH_REASON: frozenset[str] = frozenset({
    "update_project_config",
    "write_project_guideline",
    "save_dict",
    "save_name_table",
    "manage_problem_filter",
    "manage_problem_white_list",
    "patch_transl_cache",
    "revert_proofread_changes",
    "delete_transl_cache",
    "start_translation",
})


def _attach_reason(name: str, args: dict[str, Any], result: Any) -> Any:
    """把入参里的 reason 原样挂到工具结果上，供界面渲染（变更卡 / 启动翻译那一行）。

    模型不传（空串 / 只有空白）就什么都不加；结果不是 dict（读类工具可能返回列表）
    也不改形；结果里本来就有 reason 的以工具自己写的为准。
    """
    if name not in _TOOLS_WITH_REASON or not isinstance(result, dict):
        return result
    if "reason" in result:
        return result
    reason = str(args.get("reason") or "").strip()
    if not reason:
        return result
    return {**result, "reason": reason}
