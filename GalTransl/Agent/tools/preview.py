"""审批卡的「将要变更」预览：写类工具执行前先算出 diff。"""

from __future__ import annotations

import urllib.parse
from typing import Any, TYPE_CHECKING

from GalTransl.Agent.core import DEFAULT_CONFIG_FILE, _log
from GalTransl.Agent.tools.cache import (
    _group_cache_patches_by_file,
    _plan_cache_delete,
    _plan_cache_patches,
)
from GalTransl.Agent.tools.cache_fields import _PATCHABLE_FIELDS
from GalTransl.Agent.tools.common import _diff_lines, _parse_index_spec
from GalTransl.Agent.models import AgentToolError
from GalTransl.Agent.tools.dicts import _dict_new_lines, _dict_save_plan
from GalTransl.Agent.tools.names import _name_table_changes
from GalTransl.Agent.tools.problems import (
    _is_valid_regex,
    _load_problem_filter_keys,
    _load_problem_white_list,
    _parse_filter_keywords,
    _plan_problem_filter,
)
from GalTransl.Agent.tools.project import _plan_config_updates
from GalTransl.Agent.tools.plugin_settings import catalog_for_updates

if TYPE_CHECKING:
    from GalTransl.Agent.runner import AgentRunner


# ---- 审批卡的「将要变更」预览 ----
# 写类工具真正执行前会挂起等用户批准。那张卡以前只有一句参数摘要，要看"它准备改成什么"
# 就得先展开工具行的原始 JSON；现在在挂起之前就把 before→after 算出来摆到卡上——用户点的，
# 就是自己看到的那份 diff。
#
# **只读**：只算不写，绝不落盘（写由获批后的 handler 做）。为此每个工具都走"先算出结果、
# 再决定写"的路子：缓存/字典/人名表在只读计划函数上算，配置在副本上算，项目规范让后端
# 带 dry_run 算（见 _preview_guideline_write）。也因此预览和真执行是两次独立取数：中间
# 用户自己改了文件、两条 patch 打同一个 index 这类情况都由真执行那份计划重新算，结果里的
# changes 才是最终事实。
#
# 覆盖范围＝所有"改完有 diff 可看"的写类工具（含 high 档的改配置 / 写规范 / 改问题过滤——
# 它们同样要用户点允许，同样该看到要改什么）。不在列的都是没有可比对 diff 的：整文件删缓存
# （delete_transl_cache 不传 indexes 时没有单个文件名可对）、启动翻译、派子代理。
PREVIEW_TOOLS: frozenset[str] = frozenset({
    "patch_transl_cache",
    "delete_transl_cache",
    "save_dict",
    "save_name_table",
    "update_project_config",
    "manage_problem_filter",
    "manage_problem_white_list",
    "write_project_guideline",
})


def _preview_tool_changes(runner: AgentRunner, name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """算「这次调用将要改成什么」（只读），给审批卡提前显示 diff。

    返回的结构与工具结果里那份一致（changes / line_diff / deleted_preview），前端用同一个
    ChangeListCard 渲染。没有可预览的改动（工具不在 PREVIEW_TOOLS、入参不全、index 没命中、
    内容其实没变）时返回 None——卡上就只剩摘要，别拿一个空 diff 骗用户。
    取数失败同样返回 None：预览只是锦上添花，绝不能因此挡住审批。
    """
    if name not in PREVIEW_TOOLS:
        return None
    try:
        if name == "patch_transl_cache":
            return _preview_cache_patch(runner, args)
        if name == "delete_transl_cache":
            return _preview_cache_delete(runner, args)
        if name == "save_dict":
            return _preview_dict_write(runner, args)
        if name == "update_project_config":
            return _preview_config_update(runner, args)
        if name == "manage_problem_filter":
            return _preview_problem_filter(runner, args)
        if name == "manage_problem_white_list":
            return _preview_problem_white_list(runner, args)
        if name == "write_project_guideline":
            return _preview_guideline_write(runner, args)
        return _preview_name_table(runner, args)
    except Exception as exc:  # noqa: BLE001 - 预览拿不到数据不影响审批流程
        _log(f"  ⚠ 变更预览失败（{name}）：{exc}")
        return None


def _preview_cache_patch(runner: AgentRunner, args: dict[str, Any]) -> dict[str, Any] | None:
    """patch_transl_cache 的预览：读条目（不改），按同一份判断算出 before→after（支持跨文件）。

    changes 与真执行那份逐条一致（同一个 _plan_cache_patches、同一条前缀规则，clear_comment 也
    照传），所以卡上看到的 before→after 就是获批后会写下去的东西——包括要清掉的那些批注。
    """
    targets = _group_cache_patches_by_file(args)
    qualify = len(targets) > 1
    clear_comment = bool(args.get("clear_comment"))
    pid = runner._project_id()
    files: list[str] = []
    changes: list[dict[str, Any]] = []
    not_found: list[dict[str, Any]] = []
    for filename, patches in targets:
        data = runner._http_get(f"/api/projects/{pid}/cache/{urllib.parse.quote(filename)}")
        entries = data.get("entries", []) if isinstance(data, dict) else []
        if not isinstance(entries, list):
            continue
        planned = _plan_cache_patches(
            entries, patches, _PATCHABLE_FIELDS, clear_comment=clear_comment
        )
        files.append(filename)
        for change in planned["changes"]:
            row = {**change, "file": filename}
            if qualify and isinstance(row.get("path"), str):
                # path 本来就以 `#` 开头（`#33.pre_dst`），前缀只补文件名
                row["path"] = f"{filename}{row['path']}"
            changes.append(row)
        not_found += [{"file": filename, "index": idx} for idx in sorted(planned["not_found"])]
    if not changes:
        return None
    out: dict[str, Any] = {"files": files, "changes": changes}
    if not_found:
        out["not_found"] = not_found
    return out


def _preview_cache_delete(runner: AgentRunner, args: dict[str, Any]) -> dict[str, Any] | None:
    """delete_transl_cache 的预览（只覆盖"按 index 删条目"；整文件删除没有 diff 可看）。"""
    filename = str(args.get("filename", "")).strip()
    index_spec = str(args.get("indexes", "") or "").strip()
    if not filename or not index_spec:
        return None
    wanted = _parse_index_spec(index_spec)
    if not wanted:
        return None
    pid = runner._project_id()
    data = runner._http_get(f"/api/projects/{pid}/cache/{urllib.parse.quote(filename)}")
    entries = data.get("entries", []) if isinstance(data, dict) else []
    if not isinstance(entries, list):
        return None
    _, deleted_indexes, previews = _plan_cache_delete(entries, wanted)
    if not deleted_indexes:
        return None
    return {
        "filename": filename,
        "deleted_indexes": deleted_indexes,
        "deleted_preview": previews[:50],
    }


def _preview_dict_write(runner: AgentRunner, args: dict[str, Any]) -> dict[str, Any] | None:
    """save_dict 的预览：读旧内容 + 按同一份合并规则算新内容，做行级 diff。

    文件还不存在（带 category 新建）时按空文件算 diff，并在预览里标出要新建。
    """
    pid = runner._project_id()
    cfg = urllib.parse.quote(runner.state.config_file_name)
    data = runner._http_get(f"/api/projects/{pid}/dictionary/project?config={cfg}")
    contents = data.get("dict_contents", {}) if isinstance(data, dict) else {}
    try:
        plan = _dict_save_plan(contents, args)
    except AgentToolError:
        return None  # 入参本身有问题：真执行时会报错，这里不画预览
    new_lines, _ = _dict_new_lines(plan["before_lines"], plan["content"], plan["action"])
    before_text = "\n".join(plan["before_lines"])
    new_text = "\n".join(new_lines)
    if new_text == before_text and not plan["create"]:
        return None
    preview: dict[str, Any] = {"file_key": plan["file_key"], "action": plan["action"]}
    if plan["create"]:
        preview["create"] = {"category": plan["category"]}
    if new_text != before_text:
        preview["line_diff"] = _diff_lines(before_text, new_text)
    return preview


def _preview_config_update(runner: AgentRunner, args: dict[str, Any]) -> dict[str, Any] | None:
    """update_project_config 的预览：读配置 + 在副本上算一遍改完的结果（一行都不写回）。"""
    updates = args.get("updates")
    if not isinstance(updates, list) or not updates:
        return None
    pid = runner._project_id()
    config_name = runner.state.config_file_name or DEFAULT_CONFIG_FILE
    data = runner._http_get(f"/api/projects/{pid}/config?config={urllib.parse.quote(config_name)}")
    config = data.get("config") if isinstance(data, dict) else None
    if not isinstance(config, dict):
        return None
    _, _, changes = _plan_config_updates(config, updates, catalog_for_updates(runner, updates))
    if not changes:
        return None
    return {"changes": changes}


def _preview_problem_filter(runner: AgentRunner, args: dict[str, Any]) -> dict[str, Any] | None:
    """manage_problem_filter 的预览：读现在的清单，算出这次会加/删哪几个关键字。

    只覆盖 add / remove：list 不改任何东西，没有 diff 可看。
    """
    action = str(args.get("action", "")).strip()
    if action not in ("add", "remove"):
        return None
    keywords = _parse_filter_keywords(args.get("keyword"))
    if not keywords:
        return None
    if action == "add" and any(not _is_valid_regex(k) for k in keywords):
        return None  # 真执行会因正则不合法报错，卡上不必先画一份不会发生的变更
    pid = runner._project_id()
    config_name = runner.state.config_file_name or DEFAULT_CONFIG_FILE
    _, keys = _load_problem_filter_keys(runner, pid, config_name)
    _, _, changes = _plan_problem_filter(keys, action, keywords)
    if not changes:
        return None  # 全都在清单里（或本来就不在）：这次调用不会改变什么
    return {"changes": changes}


def _preview_problem_white_list(runner: AgentRunner, args: dict[str, Any]) -> dict[str, Any] | None:
    """manage_problem_white_list 的预览：读现在的白名单，算出这次会加/删哪几条。

    只覆盖 add / remove：list 不改任何东西，没有 diff 可看。
    """
    action = str(args.get("action", "")).strip()
    if action not in ("add", "remove"):
        return None
    entries = _parse_filter_keywords(args.get("entry"))
    if not entries:
        return None
    pid = runner._project_id()
    config_name = runner.state.config_file_name or DEFAULT_CONFIG_FILE
    _, current = _load_problem_white_list(runner, pid, config_name)
    _, _, changes = _plan_problem_filter(current, action, entries, field="problemWhiteList")
    if not changes:
        return None  # 全都在白名单里（或本来就不在）：这次调用不会改变什么
    return {"changes": changes}


def _preview_guideline_write(runner: AgentRunner, args: dict[str, Any]) -> dict[str, Any] | None:
    """write_project_guideline 的预览：让后端按 dry_run 算一遍"写完会是什么样"，再和现在比。

    三种 mode 的拼接规则（overwrite / append 的空行与结尾换行 / replace 的命中唯一性）只在
    ProjectGuideline.apply_project_guideline_edit 那一份实现里——在 Agent 侧重算一遍就是
    第二份，早晚对不上。所以预览也走那个入口，只是带 dry_run（服务端算完就走，不落盘）：
    连 replace 没命中、超长这类校验结果都跟真执行一致。
    """
    mode = str(args.get("mode", "") or "").strip()
    if mode not in ("overwrite", "append", "replace"):
        return None
    endpoint = f"/api/projects/{runner._project_id()}/guideline"
    before = str((runner._http_get(endpoint) or {}).get("content") or "")
    preview = runner._http_put(endpoint, {
        "mode": mode,
        "content": str(args.get("content", "") or ""),
        "old_text": str(args.get("old_text", "") or ""),
        "new_text": str(args.get("new_text", "") or ""),
        "dry_run": True,
    })
    after = str((preview or {}).get("content") or "") if isinstance(preview, dict) else ""
    if not after or after == before:
        return None  # 内容没变（如 append 一段已有的文字）：没有 diff 可显示
    return {"changed": True, "line_diff": _diff_lines(before, after)}


def _preview_name_table(runner: AgentRunner, args: dict[str, Any]) -> dict[str, Any] | None:
    """save_name_table 的预览：拿旧表按 src_name 比出新增/移除/改译名。

    整表覆写的写法下"改了哪几个名字"只能比对才看得出来（见 _name_table_changes）；
    原样回传同一张表时不产生任何 changes，卡上就不显示这块。
    """
    names = args.get("names", [])
    if not isinstance(names, list):
        return None
    pid = runner._project_id()
    old = runner._http_get(f"/api/projects/{pid}/name-table")
    _, _, changes = _name_table_changes(old.get("names", []), names)
    if not changes:
        return None
    return {"changes": changes}
