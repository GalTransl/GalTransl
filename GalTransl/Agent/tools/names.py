"""人名表工具（含用 GPT 字典回填 name 字段）。"""

from __future__ import annotations

import urllib.parse
from typing import Any, TYPE_CHECKING

from GalTransl.Agent.core import DEFAULT_CONFIG_FILE, _log
from GalTransl.Agent.models import AgentToolError
from GalTransl.Agent.tools.common import _change
from GalTransl.Agent.tools.listing import _list_limit, _list_offset, _list_paging
from GalTransl.Agent.tools.project import _MISSING, _get_config_key
from GalTransl.Dictionary import split_dictionary_line

if TYPE_CHECKING:
    from GalTransl.Agent.runner import AgentRunner


# ---- GPT 字典用于 name 字段（dictionary.useGPTDictInName）----
# 翻译时（GalTransl/Name.py 的 load_name_table）会拿 GPT 字典里同名的词条去补 name 字段，
# 而且这个开关默认就是开的（见 DefaultProjectConfig）。也就是说人名表里"译名空着、字典里
# 有"的行其实早已生效——get_name_table 若不体现这一点，模型会去补一整批字典里早就有的名字。
# 下面这套就是按同一口径把字典里的译名补进返回值（前端人名页的 overlayGptDictOntoNames
# 是同一套规则的另一份实现：都只补空译名，不覆盖表里已有的）。


def _gpt_dict_line(raw: str) -> tuple[str, str]:
    """GPT 字典的一行 →（查找词, 替换词）；不是词条行则返回两个空串。

    共用 GalTransl/Dictionary.py 的列解析：跳过空行/注释行，4 个空格当 Tab，
    兼容 `src->dst #note` 写法，至少两列才算词条。**不做 strip**——字典的命中判定是全等
    比较（CGptDict.get_dst），把空白修掉会让"其实查不到"的行看起来像命中了。
    """
    if not raw or raw.startswith("\n"):
        return "", ""
    if raw.lstrip().startswith(("//", "\\\\")):  # 注释行
        return "", ""
    parts = split_dictionary_line(raw, gpt=True)
    if len(parts) < 2:
        return "", ""
    return parts[0], parts[1]


def _gpt_dict_name_map(runner: AgentRunner) -> dict[str, str]:
    """项目 GPT 字典 → {查找词: 替换词}（只读）。

    同一查找词出现多次时取**首次**出现的那条：CGptDict.get_dst 返回的就是第一个 search_word
    全等的词条（字典里"后写的覆盖先写的"在这里不成立）。
    """
    pid = runner._project_id()
    cfg = urllib.parse.quote(runner.state.config_file_name or DEFAULT_CONFIG_FILE)
    data = runner._http_get(f"/api/projects/{pid}/dictionary/project?config={cfg}")
    if not isinstance(data, dict):
        return {}
    contents = data.get("dict_contents", {})
    if not isinstance(contents, dict):
        return {}
    out: dict[str, str] = {}
    for key in data.get("gpt_dict_files", []) or []:
        entry = contents.get(str(key))
        if not isinstance(entry, dict):
            continue
        for raw in entry.get("lines", []) or []:
            src, dst = _gpt_dict_line(str(raw))
            if src and dst:
                out.setdefault(src, dst)
    return out


def _use_gpt_dict_in_name(runner: AgentRunner) -> bool:
    """配置里 dictionary.useGPTDictInName 是否开着。

    取值口径与 Name.py 一致（**缺键 = 没开**）：配置里没写就意味着翻译时不会拿字典补 name
    字段，这时在 get_name_table 里补上译名等于骗模型——它会以为这些名字已经有人管了。
    配置读不到也按"没开"处理：这只是附加信息，不值得为一个可选展示把工具弄失败。
    """
    try:
        pid = runner._project_id()
        cfg = urllib.parse.quote(runner.state.config_file_name or DEFAULT_CONFIG_FILE)
        data = runner._http_get(f"/api/projects/{pid}/config?config={cfg}")
    except Exception as exc:  # noqa: BLE001
        _log(f"  ⚠ 读项目配置失败，get_name_table 不补 GPT 字典译名：{exc}")
        return False
    config = data.get("config") if isinstance(data, dict) else None
    if not isinstance(config, dict):
        return False
    value = _get_config_key(config, "dictionary.useGPTDictInName")
    return value is not _MISSING and bool(value)


def _fill_names_from_gpt_dict(
    names: list[Any], gpt_map: dict[str, str]
) -> tuple[list[Any], list[str]]:
    """把 GPT 字典里的译名补到**译名为空**的人名行上（只算不写）。

    与人名翻译页的 overlayGptDictOntoNames 同一套规则：只补空的，不覆盖表里已有的译名——
    表里写下的译名是用户（或 Agent）的决定，字典只回答"这行其实已经有出处了"。
    返回（补好的人名行, 仍然没有译名的 src_name）。
    """
    out: list[Any] = []
    still_empty: list[str] = []
    for item in names:
        if not isinstance(item, dict):
            out.append(item)
            continue
        src = str(item.get("src_name") or item.get("name") or "").strip()
        dst = str(item.get("dst_name") or "")
        if dst.strip() or not src:
            out.append(item)
            continue
        mapped = gpt_map.get(src)
        if not mapped:
            out.append(item)
            still_empty.append(src)
            continue
        # 盖上出处：这行的译名不在表里，是字典在翻译时给 name 字段补的
        out.append({**item, "dst_name": mapped, "dst_name_source": "gpt_dict"})
    return out, still_empty


def _load_effective_name_table(runner: AgentRunner) -> Any:
    """读人名替换表；useGPTDictInName 开着时，顺带把 GPT 字典里已有的译名补进返回值。

    为什么要补：翻译时 name 字段会吃 GPT 字典里同名的词条，表里译名空着而字典里有人的行
    **其实已经生效**。不体现这一点，模型会去补一整批"字典里早就写过"的名字。补上之后
    still_empty 才是真正要它动手的那几个。

    补的是**空译名**的行（与前端人名页同一套规则）：表里已有的译名不动，字典只作补充说明；
    这些行在返回里带 dst_name_source="gpt_dict"——要固定住某行的译名得写进表（save_name_table），
    要改它则得改字典。

    **只读**：一个字节都不写回；配置/字典读不到就退回原样返回，绝不因此把工具弄失败。
    """
    pid = runner._project_id()
    data = runner._http_get(f"/api/projects/{pid}/name-table")
    names = data.get("names") if isinstance(data, dict) else None
    if not isinstance(names, list) or not names:
        return data
    if not _use_gpt_dict_in_name(runner):
        return data
    try:
        gpt_map = _gpt_dict_name_map(runner)
    except Exception as exc:  # noqa: BLE001 - 字典读不到就少补一块，表本身照常返回
        _log(f"  ⚠ 读 GPT 字典失败，get_name_table 返回原始人名表：{exc}")
        return data
    overlaid, still_empty = _fill_names_from_gpt_dict(names, gpt_map)
    return {
        **data,
        "names": overlaid,
        "use_gpt_dict_in_name": True,
        "still_empty": still_empty,
        "note": (
            "dictionary.useGPTDictInName 开着：names 里译名为空、而 GPT 字典收录了的行，"
            "已经按字典的译名补上（带 dst_name_source=gpt_dict，翻译时真的会生效）。"
            "**still_empty 才是表与字典都没有、需要你补的**；"
            "要把某个译名固定下来（不再依赖字典）用 save_name_table 写进表里。"
        ),
    }


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
    return {
        **data, "names": page, "total": len(names), "matched": len(matched), "returned": len(page),
        "query": query, "only_missing": only_missing, "limit": limit,
        "missing_total": sum(missing(row) for row in names),
        "still_empty": [row.get("src_name", "") for row in page if missing(row)],
        **_list_paging(len(matched), len(page), offset, "name"),
    }


def _name_table_entries(raw: Any) -> dict[str, str]:
    """人名表 entries → {src_name: dst_name}（按出现顺序）。

    接口两个方向都是这个结构（get_name_table 返回、save_name_table 入参）：
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

    save_name_table 与审批预览共用。**按 src_name 逐条比 dst_name**，而不是把整条 entry
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
    """按 mode 计算完整写入内容，供保存与审批预览共用。"""
    mode = args.get("mode", "patch")
    if mode not in ("overwrite", "patch"):
        raise AgentToolError("mode must be overwrite or patch")
    names = args.get("names", [])
    if not isinstance(names, list):
        raise AgentToolError("names must be an array")
    if mode == "overwrite":
        return mode, names

    merged = {
        str(item["src_name"]): dict(item)
        for item in old_raw
        if isinstance(item, dict) and item.get("src_name")
    }
    for item in names:
        if not isinstance(item, dict) or not isinstance(item.get("src_name"), str) or not item["src_name"].strip():
            raise AgentToolError("each patch entry must have a non-empty src_name")
        src = item["src_name"]
        entry = merged.setdefault(src, {"src_name": src, "dst_name": "", "count": 0})
        # 只合并显式传入的字段；空译名可用于清空，省略字段则保留旧值。
        for field in ("dst_name", "count"):
            if field in item:
                entry[field] = item[field]
    return mode, list(merged.values())


def _tool_save_name_table(runner: AgentRunner, args: dict[str, Any]) -> Any:
    pid = runner._project_id()
    # 用原始表合并，避免把 get_name_table 的 GPT 字典展示译名写进未修改的行。
    old = runner._http_get(f"/api/projects/{pid}/name-table")
    mode, names = _name_table_save_plan(old.get("names", []), args)
    result = runner._http_post(f"/api/projects/{pid}/name-table/save", {"names": names})
    added, removed, changes = _name_table_changes(old.get("names", []), names)
    return {
        **(result if isinstance(result, dict) else {}),
        "mode": mode,
        "names_added": added,
        "names_removed": removed,
        "changes": changes,
    }
