"""问题类工具：列出问题（带上下文）、问题过滤与白名单管理。"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any, Sequence, TYPE_CHECKING

from GalTransl.ProblemWhiteList import parse_problem_white_list_entry
from GalTransl.Agent.core import DEFAULT_CONFIG_FILE
from GalTransl.Agent.models import AgentToolError
from GalTransl.Agent.tools.cache_fields import _cache_field_value
from GalTransl.Agent.tools.common import (
    _change,
    _dominant_trans_by,
    _mark_context_row,
    _only_preceding_arg,
    _split_problem_types,
    _strip_dominant_trans_by,
)

if TYPE_CHECKING:
    from GalTransl.Agent.runner import AgentRunner


def _parse_filter_keywords(raw: Any) -> list[str]:
    """字符串列表入参（单个字符串 / 字符串数组 / 配置里那串）→ 去空白、去重保序的列表。

    manage_problem_filter / manage_problem_white_list 与审批卡上的「将要变更」预览共用：
    字符串既可能是单个项，也可能是换行分隔的一串（模型两种都爱写）。
    """
    items = raw.split("\n") if isinstance(raw, str) else raw
    if not isinstance(items, list):
        return []
    cleaned = [k.strip() for k in items if isinstance(k, str) and k.strip()]
    return list(dict.fromkeys(cleaned))  # 去重保序


def _load_problem_filter_keys(
    runner: AgentRunner, pid: str, config_name: str
) -> tuple[dict[str, Any], list[str]]:
    """读配置里的 common.problemFilterKey：返回（整份 config, 去重保序的关键字清单）。

    manage_problem_filter 的 list/add/remove 三支与预览都走它——"现在的清单是什么"
    只能有一处口径。
    """
    data = runner._http_get(f"/api/projects/{pid}/config?config={urllib.parse.quote(config_name)}")
    config = data.get("config") if isinstance(data, dict) else None
    if not isinstance(config, dict):
        raise AgentToolError("项目配置读取失败")
    common = config.get("common")
    if not isinstance(common, dict):
        common = {}
        config["common"] = common
    return config, _parse_filter_keywords(common.get("problemFilterKey", []))


def _load_problem_filter_stats(
    runner: AgentRunner, pid: str, keys: list[str], config_name: str
) -> dict[str, Any]:
    """list 的结果：过滤清单 + 每条当前各挡住了多少条问题。

    条数由服务端扫缓存算（/problem_filter_stats），与 list_problems / 进度统计同一口径
    （白名单命中的条目不算）。数字是「现在」的快照：0 说明这条过滤项当前一条也挡不到，
    多半已经没用了。统计取不到（老服务端/接口异常）时退回只有清单的结果——list 是只读查询，
    不该因为统计挂掉。
    """
    result: dict[str, Any] = {"filter_keys": keys, "count": len(keys)}
    if not keys:
        return result
    try:
        data = runner._http_get(
            f"/api/projects/{pid}/problem_filter_stats?config={urllib.parse.quote(config_name)}"
        )
    except Exception:  # noqa: BLE001
        return result
    if not isinstance(data, dict):
        return result
    for key in ("filters", "problem_entries", "visible_entries"):
        if data.get(key) is not None:
            result[key] = data[key]
    return result


def _plan_problem_filter(
    keys: list[str], action: str, keywords: list[str], field: str = "problemFilterKey"
) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    """算出这次 add/remove 实际会动到哪些项（**只读**）：返回（命中, 未命中, changes）。

    与 _tool_manage_problem_filter / _tool_manage_problem_white_list 共用：卡上列出的
    增删就是真执行会写进去的那些；field 决定变更卡上显示的是哪个配置键。
    """
    existing = set(keys)
    if action == "add":
        hit = [k for k in keywords if k not in existing]  # 实际新增
        miss = [k for k in keywords if k in existing]  # 本来就有
        changes = [_change(field, None, k, "add") for k in hit]
    else:  # remove
        hit = [k for k in keywords if k in existing]  # 实际移除
        miss = [k for k in keywords if k not in existing]  # 本来就没有
        changes = [_change(field, k, None, "remove") for k in hit]
    return hit, miss, changes


def _is_valid_regex(pattern: str) -> bool:
    """过滤项是正则：写坏的模式在 add 时就拒掉（并提示转义），别留到过滤时才发现。"""
    try:
        re.compile(pattern)
    except re.error:
        return False
    return True


def _tool_manage_problem_filter(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """增/删/查项目配置 common.problemFilterKey（问题过滤关键字）。

    与桌面端「缓存与问题」页同一套配置。**正则匹配**：keyword 是一条正则，按 re.search
    命中问题项的那一项才会在 list_problems / 进度统计里被过滤掉（如 `缺失.*标点`）。
    **原则上只过滤小类、不过滤大类**（`残留日文`、`^残留日文：` 这类整类写法等于放弃复核，
    要在提示里挡住）。add/remove 是对清单里字符串的精确增删（区分大小写）；
    add 时校验正则可编译，写坏了直接报错并提示转义。
    """
    action = str(args.get("action", "")).strip()
    if action not in ("list", "add", "remove"):
        raise AgentToolError("action must be one of: list, add, remove")
    pid = runner._project_id()
    config_name = runner.state.config_file_name or DEFAULT_CONFIG_FILE

    if action == "list":
        _, keys = _load_problem_filter_keys(runner, pid, config_name)
        # 每条过滤项当前各挡住了多少条问题：判断哪条已经没用了（problems 为 0）
        return _load_problem_filter_stats(runner, pid, keys, config_name)

    # keyword 支持单个字符串或字符串数组（一次增删多个）：去重保序、忽略空串
    keywords = _parse_filter_keywords(args.get("keyword"))
    if not keywords:
        raise AgentToolError("keyword is required for add/remove（字符串或字符串数组）")
    if action == "add":
        invalid = [k for k in keywords if not _is_valid_regex(k)]
        if invalid:
            raise AgentToolError(
                "这些过滤项不是合法正则：" + "、".join(invalid)
                + "。过滤项按正则匹配；想按字面过滤请转义特殊字符（\\. \\( \\[ \\*）。"
            )

    config, keys = _load_problem_filter_keys(runner, pid, config_name)
    hit, miss, changes = _plan_problem_filter(keys, action, keywords)
    if action == "add":
        hit_key, miss_key, miss_note = "added", "already_present", "已在列表中"
    else:  # remove
        hit_key, miss_key, miss_note = "removed", "not_found", "不在列表中"

    if not hit:
        return {
            "filter_keys": keys,
            "count": len(keys),
            "note": f"这些关键字{miss_note}，过滤清单未变化",
        }

    if action == "add":
        keys.extend(hit)
    else:
        removing = set(hit)
        keys = [k for k in keys if k not in removing]

    config["common"]["problemFilterKey"] = keys
    runner._http_put(
        f"/api/projects/{pid}/config",
        {"config": config, "config_file_name": config_name},
    )
    # 配置已写回：进度缓存按 mtime 自动失效，后续 list_problems 立即用新过滤
    result: dict[str, Any] = {"filter_keys": keys, "count": len(keys), hit_key: hit, "changes": changes}
    if miss:
        result[miss_key] = miss
    return result


def _load_problem_white_list(
    runner: AgentRunner, pid: str, config_name: str
) -> tuple[dict[str, Any], list[str]]:
    """读配置里的 common.problemWhiteList：返回（整份 config, 去重保序的条目清单）。

    manage_problem_white_list 的 list/add/remove 三支与审批卡预览都走它——"现在的
    白名单是什么"只能有一处口径。
    """
    data = runner._http_get(f"/api/projects/{pid}/config?config={urllib.parse.quote(config_name)}")
    config = data.get("config") if isinstance(data, dict) else None
    if not isinstance(config, dict):
        raise AgentToolError("项目配置读取失败")
    common = config.get("common")
    if not isinstance(common, dict):
        common = {}
        config["common"] = common
    return config, _parse_filter_keywords(common.get("problemWhiteList", []))


_WHITE_LIST_ENTRY_HINT = '条目格式为 "<缓存文件名>:<index>"（如 "01.json:12"），区间写 "01.json:12-15"'


def _tool_manage_problem_white_list(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """增/删/查项目配置 common.problemWhiteList（问题白名单）。

    白名单是「缓存文件 + 条目 index」的名单，命中的条目等价于勾了 skip_check：
    list_problems / 进度统计不再显示它的问题，缓存重建时也不再检测。与
    manage_problem_filter（按问题文本子串整类过滤）互补：白名单按具体位置豁免。
    """
    action = str(args.get("action", "")).strip()
    if action not in ("list", "add", "remove"):
        raise AgentToolError("action must be one of: list, add, remove")
    pid = runner._project_id()
    config_name = runner.state.config_file_name or DEFAULT_CONFIG_FILE

    if action == "list":
        _, entries = _load_problem_white_list(runner, pid, config_name)
        return {"white_list": entries, "count": len(entries)}

    entries = _parse_filter_keywords(args.get("entry"))
    if not entries:
        raise AgentToolError(f"entry is required for add/remove（{_WHITE_LIST_ENTRY_HINT}）")
    if action == "add":
        invalid = [e for e in entries if parse_problem_white_list_entry(e) is None]
        if invalid:
            raise AgentToolError(f"这些条目格式不对：{'、'.join(invalid)}；{_WHITE_LIST_ENTRY_HINT}")

    config, current = _load_problem_white_list(runner, pid, config_name)
    hit, miss, changes = _plan_problem_filter(current, action, entries, field="problemWhiteList")
    if action == "add":
        hit_key, miss_key, miss_note = "added", "already_present", "已在白名单里"
    else:  # remove
        hit_key, miss_key, miss_note = "removed", "not_found", "不在白名单里"

    if not hit:
        return {
            "white_list": current,
            "count": len(current),
            "note": f"这些条目{miss_note}，白名单未变化",
        }

    if action == "add":
        current.extend(hit)
    else:
        removing = set(hit)
        current = [k for k in current if k not in removing]

    config["common"]["problemWhiteList"] = current
    runner._http_put(
        f"/api/projects/{pid}/config",
        {"config": config, "config_file_name": config_name},
    )
    result: dict[str, Any] = {
        "white_list": current, "count": len(current), hit_key: hit, "changes": changes
    }
    if miss:
        result[miss_key] = miss
    return result


def _merge_problem_context(
    runner: AgentRunner, page: list[dict[str, Any]], context: int, only_preceding: bool = True
) -> list[dict[str, Any]]:
    """给问题行并上文（list_problems 的 context，与 read_transl_cache 同一语义）。

    问题行自己往往看不出"为什么有问题"——修「残留日文」「译名不一致」要看着上文才敢动手，
    逐条 read_transl_cache 又太碎，所以把上文直接并进这张表。上下文行的 index 带 *（见
    _mark_context_row）：一眼分得开哪些是本页的问题行、哪些只是搭着给的上文。
    - 同一文件里相邻问题的窗口**合并**（重叠的上下文行只给一份）；
    - only_preceding（默认 true）只给上文；传 false 才前后各 N 句；
    - 上下文按每个文件取一次缓存，取不到（文件被删/翻写中）只少带一块：该文件的问题行照给，
      绝不因此把整次列表弄失败。
    """
    merged: list[dict[str, Any]] = []
    # 按文件分组（保持首次出现顺序）：跨文件的问题行不能混进同一个窗口
    order: list[str] = []
    by_file: dict[str, list[dict[str, Any]]] = {}
    for row in page:
        fname = str(row.get("filename") or "")
        if fname not in by_file:
            by_file[fname] = []
            order.append(fname)
        by_file[fname].append(row)
    pid = runner._project_id()
    for fname in order:
        rows = by_file[fname]
        try:
            data = runner._http_get(f"/api/projects/{pid}/cache/{urllib.parse.quote(fname)}")
            entries = [e for e in (data.get("entries") or []) if isinstance(e, dict)]
        except Exception:  # noqa: BLE001 - 上下文只是附加信息，取不到就只给问题行
            entries = []
        if not entries:
            merged.extend(rows)
            continue
        problem_by_index: dict[int, dict[str, Any]] = {}
        for r in rows:
            raw_idx = r.get("index")
            if raw_idx is None:
                continue
            try:
                problem_by_index[int(raw_idx)] = r
            except (TypeError, ValueError):
                continue
        spans: set[int] = set()
        after = 0 if only_preceding else context
        for idx in problem_by_index:
            spans.update(range(idx - context, idx + after + 1))
        file_rows: list[dict[str, Any]] = []
        emitted: set[int] = set()
        for e in entries:
            raw_idx = e.get("index")
            if raw_idx is None:
                continue
            try:
                idx = int(raw_idx)
            except (TypeError, ValueError):
                continue
            if idx in problem_by_index:
                file_rows.append(problem_by_index[idx])
                emitted.add(idx)
            elif idx in spans:
                # 上下文行只要「谁说的 + 原文 + 译文」，problem 列空着；index 带 * 标明它不是本页的
                # 问题行（问题行另有判据：它在 problem_by_index 里）
                file_rows.append(_mark_context_row({
                    "filename": fname,
                    "index": idx,
                    "speaker": str(_cache_field_value(e, "name") or ""),
                    "post_src": str(_cache_field_value(e, "post_src") or ""),
                    "pre_dst": str(_cache_field_value(e, "pre_dst") or ""),
                }))
        # 缓存里找不到的问题行（缓存与问题清单不同步）：原样保留，别把问题弄丢
        for idx, row in problem_by_index.items():
            if idx not in emitted:
                file_rows.append(row)

        def _row_sort_index(row: dict[str, Any]) -> int:
            idx = row.get("index")
            if isinstance(idx, int):
                return idx
            # 上下文行的 index 是 "12*"（见 _mark_context_row）：排序还按数字来
            try:
                return int(str(idx).rstrip("*"))
            except (TypeError, ValueError):
                return 0

        file_rows.sort(key=_row_sort_index)
        merged.extend(file_rows)
    return merged


def _tool_list_problems(
    runner: AgentRunner, args: dict[str, Any], allowed_files: Sequence[str] | None = None
) -> Any:
    """查问题清单。

    allowed_files（校对子代理按派活锁定）：只列这几份文件的问题——统计、命中数、分页与
    context 取的上文一并收窄，免得别人文件的问题混进来带偏"我这份还剩什么"。
    """
    pid = runner._project_id()
    cfg = urllib.parse.quote(runner.state.config_file_name)
    data = runner._http_get(f"/api/projects/{pid}/problems?config={cfg}")
    problems = data.get("problems", [])
    total = data.get("total", len(problems))
    scope_note = ""
    if allowed_files is not None:
        allowed = tuple(str(name).strip() for name in allowed_files if str(name).strip())
        allowed_set = set(allowed)
        problems = [p for p in problems if str(p.get("filename") or "") in allowed_set]
        total = len(problems)
        if len(allowed) == 1:
            scope_note = f"本次只派你看「{allowed[0]}」这一个文件，这里只列它的问题。"
        else:
            scope_note = f"本次只派你看这 {len(allowed)} 个文件，这里只列它们的问题。"

    # 不带 problem_type：先给类型统计（大项目问题上千条，全量列出没有意义），
    # Agent 据此决定看哪一类。
    problem_type = str(args.get("problem_type", "") or "").strip()
    if not problem_type:
        stats: dict[str, int] = {}
        for p in problems:
            for t in _split_problem_types(p.get("problem", "")):
                stats[t] = stats.get(t, 0) + 1
        ranked = sorted(stats.items(), key=lambda kv: -kv[1])
        out: dict[str, Any] = {
            "total": total,
            "mode": "stats",
            "types": [{"type": t, "count": c} for t, c in ranked],
            "hint": "默认只返回类型统计。用 problem_type 指定类型查看具体条目（配合 limit/offset 分页），problem_type 传 \"*\" 列出全部类型的具体条目。",
        }
        if scope_note:
            out["note"] = scope_note
        return out

    # 指定类型：过滤出问题里含该类型的条目（子串匹配，与统计口径对齐）
    if problem_type != "*":
        wanted = [t.strip() for t in problem_type.split(",") if t.strip()]
        problems = [p for p in problems if any(w in _split_problem_types(p.get("problem", "")) for w in wanted)]

    limit = args.get("limit", 10)
    offset = args.get("offset", 0)
    try:
        limit = max(1, min(int(limit), 20))
    except (TypeError, ValueError):
        limit = 10
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0
    matched = len(problems)
    page = problems[offset : offset + limit]
    # trans_by 与 read_transl_cache / search_transl_cache 同一套（见 _dominant_trans_by）：
    # 这批里出现最多的那个模型（多数派，通常就是翻译引擎翻的）逐条删掉、记在顶层一次，
    # 少数派（Agent 改过的、手工改的）逐条保留——列表里真正要看的是异常来源。
    dominant = _dominant_trans_by(page)
    for row in page:
        _strip_dominant_trans_by(row, dominant)
    # context=N：每条问题并上 N 句上文（默认；only_preceding=false 才前后都并，语义同
    # read_transl_cache 的 context）。上限比 read/search 的 20 收得更紧：这里一行就是
    # "问题行 + 上下文行"，一页最多 20 条问题，N=5 时返回体就已经不小了。
    raw_context = args.get("context", 0)
    try:
        context = max(0, min(int(raw_context), 5))
    except (TypeError, ValueError):
        raise AgentToolError(f"context 必须是 0-5 的整数（收到 {raw_context!r}）")
    result = {
        "total": total,
        "matched": matched,
        "problem_type": problem_type,
        "offset": offset,
        "returned": len(page),
        "has_more": offset + limit < matched,
        "problems": page,
    }
    if scope_note:
        result["note"] = scope_note
    if context > 0:
        only_preceding = _only_preceding_arg(args)
        result["context"] = context
        result["only_preceding"] = only_preceding
        if page:
            result["problems"] = _merge_problem_context(runner, page, context, only_preceding)
    if dominant:
        result["majority_trans_by"] = dominant
    return result
