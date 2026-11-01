"""人名表工具（含用 GPT 字典回填 name 字段）。"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from GalTransl.Agent.name_table import (
    _fill_names_from_gpt_dict,
    _gpt_dict_line,
    _gpt_dict_name_map,
    _load_effective_name_table,
    _use_gpt_dict_in_name,
)
from GalTransl.Agent.models import AgentToolError
from GalTransl.Agent.tools.common import _change, _write_action
from GalTransl.Agent.tools.listing import _list_limit, _list_offset, _list_paging

if TYPE_CHECKING:
    from GalTransl.Agent.runner import AgentRunner


def _tool_get_name_table(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """先补上实际生效的 GPT 译名，再筛选和分页，避免把已覆盖人名当作缺漏。"""
    limit, offset = _list_limit(args), _list_offset(args)
    query = args.get("query", "")
    only_missing = args.get("only_missing", False)
    if not isinstance(query, str) or type(only_missing) is not bool:
        raise AgentToolError("query 必须是字符串，only_missing 必须是布尔值")
    data = _load_effective_name_table(runner)
    if not isinstance(data, dict):
        return data
    raw = data.get("names")
    names = [row for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []
    def missing(row: dict[str, Any]) -> bool:
        return not str(row.get("dst_name") or "").strip()

    needle = query.casefold()
    matched = [row for row in names if (not only_missing or missing(row)) and
               any(needle in str(row.get(key) or "").casefold() for key in ("src_name", "dst_name"))]
    page = matched[offset:offset + limit]
    guidance = (
        '**GPT 字典第一优先，不要优先手动编辑人名表**：应先调用 translation(action="start", translator="GenDic")（genDic）生成 GPT 字典，等待任务 completed，并结合「字典用在name字段(GPT)」（dictionary.useGPTDictInName）填充人名表的生效译名。已有字典译名需要修改时，优先用 dictionary(action="set") 修正 GPT 字典，无需把字典译名重复抄进表。GenDic 完成后再用 names(action="read", only_missing=true) 检查 still_empty；只有 GenDic 未覆盖到的 name 才应使用 names(action="set") 手动补漏。若该开关关闭或配置/字典读取失败，应先核对配置与字典覆盖情况，不要把字典已覆盖的 name 当作手动补漏项。'
    )
    return {
        **data, "names": page, "total": len(names), "matched": len(matched), "returned": len(page),
        "note": "\n\n".join(part for part in (data.get("note"), guidance) if part),
        "query": query, "only_missing": only_missing, "limit": limit,
        "missing_total": sum(missing(row) for row in names),
        "still_empty": [row.get("src_name", "") for row in page if missing(row)],
        **_list_paging(len(matched), len(page), offset, "name"),
    }


def _name_table_entries(raw: Any) -> dict[str, str]:
    """人名表 entries → {src_name: dst_name}（按出现顺序）。

    接口两个方向都是这个结构（names(action="read") 返回、names 入参）：
    [{src_name, dst_name, count}]，对应 CSV 的 SRC_Name / DST_Name / Count 三列。
    顺带认下老会话里可能还留着的 {"name": ...} 与裸字符串写法（当成只有 src）。
    """
    out: dict[str, str] = {}
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, dict):
            src = str(item.get("src_name") or item.get("name") or "").strip()
            dst = str(item.get("dst_name") or "")
        else:
            src, dst = str(item).strip(), ""
        if src:
            out[src] = dst
    return out


def _name_table_changes(
    old_raw: Any, new_raw: Any
) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    """人名表的增删改明细（**只算不写**）：返回（新增的 src_name, 移除的 src_name, changes）。

    names 与审批预览共用。**按 src_name 逐条比 dst_name**，而不是把整条 entry
    拿去比对：CSV 的键是 SRC_Name，值才是 DST_Name，count 只是出现次数统计——整条比会让
    "count 变了"也算成加一条删一条，卡上就会冒出「+ {'src_name': ...}」和「− None」这种
    噪音（字段名一换更是整表都成了新增）。count 不在比对范围内。
    """
    old_map = _name_table_entries(old_raw)
    new_map = _name_table_entries(new_raw)
    added = [src for src in new_map if src not in old_map]
    removed = [src for src in old_map if src not in new_map]
    changes: list[dict[str, Any]] = []
    changes.extend(_change(src, None, new_map[src] or None, "add") for src in added)
    changes.extend(_change(src, old_map[src] or None, None, "remove") for src in removed)
    # 译名改动（同一个 src_name、dst_name 变了）：这才是这个工具最常干的事
    changes.extend(
        _change(src, old_map[src], dst, "replace")
        for src, dst in new_map.items()
        if src in old_map and old_map[src] != dst
    )
    return added, removed, changes


def _name_table_save_plan(old_raw: Any, args: dict[str, Any]) -> tuple[str, list[Any]]:
    """按 action 计算完整写入内容，供保存与审批预览共用。"""
    action = _write_action(args, ("set", "overwrite"))
    names = args.get("names")
    if not isinstance(names, list):
        raise AgentToolError("names must be an array")
    existing = {
        str(item["src_name"]): dict(item)
        for item in old_raw
        if isinstance(item, dict) and item.get("src_name")
    }
    merged = {} if action == "overwrite" else {src: dict(item) for src, item in existing.items()}
    for item in names:
        if not isinstance(item, dict) or not isinstance(item.get("src_name"), str) or not item["src_name"].strip():
            raise AgentToolError("each name entry must have a non-empty src_name")
        if "count" in item:
            raise AgentToolError("count 是只读的出现次数统计；names 写入请只传 src_name / dst_name")
        if "dst_name" in item and not isinstance(item["dst_name"], str):
            raise AgentToolError("dst_name must be a string")
        src = item["src_name"]
        entry = merged.setdefault(src, {"src_name": src, "dst_name": "", "count": existing.get(src, {}).get("count", 0)})
        # 只合并显式传入的字段；空译名可用于清空，省略字段则保留旧值。
        if "dst_name" in item:
            entry["dst_name"] = item["dst_name"]
    return action, list(merged.values())


def _tool_save_name_table(runner: AgentRunner, args: dict[str, Any]) -> Any:
    pid = runner._project_id()
    # 用原始表合并，避免把 get_name_table 的 GPT 字典展示译名写进未修改的行。
    old = runner._http_get(f"/api/projects/{pid}/name-table")
    action, names = _name_table_save_plan(old.get("names", []), args)
    result = runner._http_post(f"/api/projects/{pid}/name-table/save", {"names": names})
    added, removed, changes = _name_table_changes(old.get("names", []), names)
    return {
        **(result if isinstance(result, dict) else {}),
        "action": action,
        "names_added": added,
        "names_removed": removed,
        "changes": changes,
    }
