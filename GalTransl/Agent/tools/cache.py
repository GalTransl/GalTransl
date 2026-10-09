"""翻译缓存类工具：read_transl_cache（列文件 / 读条目 / 搜索）、修改、删除缓存条目，以及读取输出文件。"""

from __future__ import annotations

import fnmatch
import os
import re
import threading
import urllib.parse
from typing import Any, TYPE_CHECKING

from GalTransl.Agent.models import AgentToolError
from GalTransl.Agent.tools.cache_fields import (
    CACHE_ENTRY_FIELDS,
    CACHE_ENTRY_FIELDS_DEFAULT,
    CACHE_ENTRY_FIELDS_IF_PRESENT,
    _PATCHABLE_FIELDS,
    _TRANSLATION_FIELDS,
    _cache_field_value,
    _patchable_fields_text,
)
from GalTransl.Agent.tools.common import (
    _change,
    _dominant_trans_by,
    _entry_index,
    _read_entries_page,
    _mark_context_row,
    _only_preceding_arg,
    _parse_index_spec,
    _strip_dominant_trans_by,
)
from GalTransl.Agent.tools.listing import (
    _grep_items,
    _list_grep,
    _list_limit,
    _list_notes,
    _list_order,
    _list_offset,
    _list_paging,
    _select_list_items,
)
from GalTransl.Agent.tools.project import _APPEND_CACHE_SUFFIX, _backend_summary
from GalTransl.Agent.tools.search import _apply_search_order, _search_order, _search_paging_args, _tool_search_transl_cache
from GalTransl.Search import select_search_hits

if TYPE_CHECKING:
    from GalTransl.Agent.runner import AgentRunner


def _tool_list_transl_cache(
    runner: AgentRunner, args: dict[str, Any], names: tuple[str, ...] | None = None
) -> Any:
    """read_transl_cache(action="list")：列出缓存文件（译文）。带每个文件的条目数。

    只列 .json 快照：翻译过程中并行写的增量日志不是可读的缓存，直接不出现在清单里——
    模型看不到它，也就不会去读它。

    清单支持 grep（文件名子串）、limit（默认 100）与 order（怎么挑这 100 个：均匀采样 /
    文件名顺序 / 随机采样 / 按大小从大到小 / 从小到大，见 LIST_ORDER_MODES）：缓存文件是按
    名字排的，成百上千个文件时"只看前 100 个"会把后半段整个藏起来（默认的 even 就是为这个）。

    names 非空时只列这些文件（子代理的锁定范围）：**先过滤再采样**，免得本属于它的文件被
    采样摇掉、看起来像不存在。
    """
    limit = _list_limit(args)
    grep = _list_grep(args)
    order = _list_order(args)
    offset = _list_offset(args)
    pid = runner._project_id()
    data = runner._http_get(f"/api/projects/{pid}/cache")
    files = []
    for f in data.get("files", []):
        name = str(f.get("name", ""))
        if not name or name.endswith(_APPEND_CACHE_SUFFIX):
            continue  # 增量日志不是可读的缓存：不进清单
        entry: dict[str, Any] = {"name": name, "size": f.get("size", 0)}
        # 后端 _list_dir_entries 只对 .json 统计 entry_count（按 list 长度，含 0）；
        # 没有该字段时干脆不返回 entries，而不是填 0——否则会被误读成「空缓存」。
        entry_count = f.get("entry_count")
        if isinstance(entry_count, int):
            entry["entries"] = entry_count
        files.append(entry)
    if names is not None:
        wanted = set(names)
        files = [f for f in files if f["name"] in wanted]

    matched = _grep_items(files, grep)
    shown = _select_list_items(matched, limit, order, offset)
    result: dict[str, Any] = {
        "cache_files": shown,
        "count": len(matched),
        "returned": len(shown),
        **_list_paging(len(matched), len(shown), offset, order),
        "sampled": len(shown) < len(matched),
    }
    notes = _list_notes(
        matched=len(matched), grep=grep, returned=len(shown), limit=limit, order=order, unit="缓存文件", offset=offset
    )
    if notes:
        result["note"] = "；".join(notes)
    return result


# 译文首尾成对出现的对话符号：翻译前由 CSentense.analyse_dialogue 摘掉、译后再由
# recover_dialogue_symbol 补回来（见 GalTransl/CSentense.py）。所以 post_dst_preview
# 与译文几乎总是差这么一对括号——那不算"译后处理改了内容"，别因此把它带出来。
_DIALOGUE_BRACKETS = "「『」』"


def _same_ignoring_dialogue_brackets(left: Any, right: Any) -> bool:
    """两段译文是否只差首尾的对话符号（与首尾空白）。"""
    def norm(text: Any) -> str:
        return str(text or "").strip().strip(_DIALOGUE_BRACKETS).strip()

    return norm(left) == norm(right)


def _normalize_cache_fields(args: dict[str, Any]) -> list[str] | None:
    """解析 fields：None = 用默认精简集；给了就校验去重（"*"/"all" 表示全字段）。"""
    raw = args.get("fields")
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        raise AgentToolError("fields 必须是非空数组（不传表示用默认精简字段）")
    wanted: list[str] = []
    for item in raw:
        name = str(item or "").strip()
        if not name:
            continue
        if name in ("*", "all"):
            return list(CACHE_ENTRY_FIELDS)
        if name not in CACHE_ENTRY_FIELDS:
            raise AgentToolError(
                f"fields 里有未知字段：{name}（可选：{'、'.join(CACHE_ENTRY_FIELDS)}）"
            )
        if name not in wanted:
            wanted.append(name)
    if not wanted:
        raise AgentToolError(f"fields 里没有有效字段（可选：{'、'.join(CACHE_ENTRY_FIELDS)}）")
    return wanted


def _project_cache_entries(
    entries: list[dict[str, Any]], fields: list[str] | None
) -> list[dict[str, Any]]:
    """按 fields 裁条目；fields=None 时用默认精简集并省略空值。

    index 永远带上（定位/后续 patch 都靠它），即使调用方没写。
    取值走 _cache_field_value：老缓存里是 pre_jp/zh 那套旧键名也能读到。
    """
    out: list[dict[str, Any]] = []
    for entry in entries:
        if fields is None:
            item: dict[str, Any] = {}
            for key in CACHE_ENTRY_FIELDS_DEFAULT:
                value = _cache_field_value(entry, key)
                if key != "index" and value in (None, "", 0):
                    continue
                item[key] = value
            # 译后处理真的改了内容才有意义（译后字典替换、引号矫正…）。不能只比字符串：
            # 译后处理会把首尾「」补回来，几乎所有对话条目的预览都会"不同"。跟最终译文
            # （有校对稿就是校对稿，与看译文时 proofread_dst ＞ pre_dst 的口径一致）比，
            # 只差对话符号就不占位置。
            post_dst = _cache_field_value(entry, "post_dst_preview")
            base_dst = _cache_field_value(entry, "proofread_dst") or _cache_field_value(entry, "pre_dst")
            if post_dst and not _same_ignoring_dialogue_brackets(post_dst, base_dst):
                item["post_dst_preview"] = post_dst
            for key in CACHE_ENTRY_FIELDS_IF_PRESENT:
                value = _cache_field_value(entry, key)
                if value:
                    item[key] = value
            out.append(item)
            continue
        keys = ["index", *[key for key in fields if key != "index"]]
        picked: dict[str, Any] = {}
        for key in keys:
            value = _cache_field_value(entry, key)
            if value is not None:
                picked[key] = value
        out.append(picked)
    return out


def _grep_field_text(entry: dict[str, Any], name: str) -> str:
    """取字段的可搜索文本（认老缓存的旧键名；name 这类列表拼成一段）。"""
    value = _cache_field_value(entry, name)
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value if v is not None)
    return str(value)


def _grep_field_present(entry: dict[str, Any], name: str) -> bool:
    """字段是否有内容：字符串非空白；列表/字典非空；数字等非 None 即算。"""
    value = _cache_field_value(entry, name)
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) > 0
    return True


def _grep_cache_entries(
    entries: list[dict[str, Any]], grep: Any, fields: list[str] | None
) -> list[dict[str, Any]]:
    """read_transl_cache 的 grep 过滤（只读）。

    - grep 是字符串：在 fields（不传则默认精简集）的字段内容里做大小写不敏感的子串搜索，
      命中任一字段的条目保留；
    - grep 是字符串数组：每个元素当字段名，只保留这些字段**都不为空**的条目
      （如 ["problem", "proofread_comment"] = 既有问题、又有校对批注的条目）。
    """
    if grep is None:
        return entries
    if isinstance(grep, str):
        needle = grep.strip()
        if not needle:
            return entries
        keys = list(fields) if fields is not None else list(CACHE_ENTRY_FIELDS_DEFAULT)
        folded = needle.casefold()
        return [e for e in entries if any(folded in _grep_field_text(e, k).casefold() for k in keys)]
    if isinstance(grep, list):
        names = [str(x).strip() for x in grep if str(x).strip()]
        if not names:
            return entries
        for name in names:
            if name not in CACHE_ENTRY_FIELDS:
                raise AgentToolError(
                    f"grep 数组里不是有效的字段名：{name}（可选：{'、'.join(CACHE_ENTRY_FIELDS)}）"
                )
        return [e for e in entries if all(_grep_field_present(e, name) for name in names)]
    raise AgentToolError("grep 必须是字符串（按内容搜索）或字符串数组（按字段非空过滤）")


def _cache_grep_note(grep: Any, total: int) -> str | None:
    """给渲染层的一句话说明；grep 没实际生效（空串/空数组）时返回 None。"""
    if isinstance(grep, str) and grep.strip():
        return f"grep「{grep.strip()}」（文件共 {total} 条）"
    if isinstance(grep, list):
        names = [str(x).strip() for x in grep if str(x).strip()]
        if names:
            return f"grep 非空：{'、'.join(names)}（文件共 {total} 条）"
    return None


def _read_transl_cache_entries(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """read_transl_cache(action="read")：读某个缓存文件的条目。"""
    filename = str(args.get("filename", "")).strip()
    if not filename:
        raise AgentToolError("action=read 需要 filename（缓存文件名，来自 action=list 的清单）")
    fields = _normalize_cache_fields(args)
    pid = runner._project_id()
    data = runner._http_get(f"/api/projects/{pid}/cache/{urllib.parse.quote(filename)}")
    raw_entries = [e for e in data.get("entries", []) if isinstance(e, dict)]
    total_entries = len(raw_entries)
    # grep 在原始条目上过滤（数组模式要判任意字段是否非空，投影之后就看不到没选中的列了），
    # 之后再按 fields 裁剪。
    grep_arg = args.get("grep")
    entries = _project_cache_entries(_grep_cache_entries(raw_entries, grep_arg, fields), fields)
    result_extra: dict[str, Any] = {}
    grep_note = _cache_grep_note(grep_arg, total_entries)
    if grep_note:
        result_extra["grep_note"] = grep_note
    # trans_by 与 search 同一套规则（见 _dominant_trans_by）：逐条只给少数派，多数派与空值
    # 删掉、改记在顶层 majority_trans_by。默认精简集里没有这列，只有 fields 点名要时才处理。
    if fields is not None and "trans_by" in fields:
        dominant = _dominant_trans_by(entries)
        for entry in entries:
            _strip_dominant_trans_by(entry, dominant)
        if dominant:
            result_extra["majority_trans_by"] = dominant
    result_extra["fields"] = list(fields) if fields is not None else list(CACHE_ENTRY_FIELDS_DEFAULT)
    if fields is None:
        result_extra["fields_note"] = (
            "默认精简字段：post_dst_preview 只在译后处理真的改了内容时返回（只差首尾「」这类"
            "对话符号不算），proofread_* / trans_by / 备注等空值已省略；要看其它字段传 fields。"
        )
    index_spec = str(args.get("index", "") or "").strip()
    # 不指定 index：按 order 从全部过滤结果中挑选，默认仍取前 30 条。
    if not index_spec:
        order = _search_order(args)
        limit, offset = _search_paging_args({"limit": 30, **args})
        picked = select_search_hits(entries, limit, offset, order)
        more = offset + len(picked) < len(entries)
        result = {
            "filename": filename, "count": len(entries), "offset": offset,
            "returned": len(picked), "has_more": more, "entries": picked, **result_extra,
        }
        if more and order in ("name", "reverse"):
            result["next_offset"] = offset + len(picked)
        _apply_search_order(result, order)
        return result

    raw_context = args.get("context", 0)
    try:
        context = max(0, min(int(raw_context), 20))
    except (TypeError, ValueError):
        raise AgentToolError(f"context 必须是 0-20 的整数（收到 {raw_context!r}）")
    only_preceding = _only_preceding_arg(args)
    return {
        "filename": filename, "count": len(entries), "context": context,
        **({"only_preceding": only_preceding} if context else {}), **result_extra,
        **_read_entries_page(entries, args, context=context, only_preceding=only_preceding),
    }


def _tool_read_output(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """读取最终输出文件（gt_output，交付物）。输出是缓存经 postDict 替换、
    控制符还原后的最终形态，和缓存可能不完全一致——验收交付物用它。"""
    filename = str(args.get("filename", "")).strip()
    if not filename:
        raise AgentToolError("filename is required")
    pid = runner._project_id()
    cfg = urllib.parse.quote(runner.state.config_file_name)
    try:
        data = runner._http_get(f"/api/projects/{pid}/output/{urllib.parse.quote(filename)}?config={cfg}")
    except AgentToolError as exc:
        # 文件不存在时附上输出目录清单，省一轮试错
        listing = runner._http_get(f"/api/projects/{pid}/files")
        available = [f.get("name") for f in listing.get("output_files", []) if f.get("name") and f.get("is_file", True)]
        if available:
            raise AgentToolError(f"{exc}. 可用的输出文件：{available}") from exc
        raise
    filename = str(data.get("filename") or filename)
    # 输出条目里 message 位就是最终译文；统一映射成 {index, name, message}
    entries = [
        {"index": e.get("index"), "name": e.get("name", ""), "message": e.get("pre_src", "")}
        for e in data.get("entries", [])
        if isinstance(e, dict)
    ]
    return {"filename": filename, "count": len(entries), **_read_entries_page(entries, args)}


def _agent_model_name(runner: AgentRunner) -> str:
    """本会话 Agent 后端的模型名：patch 改过的条目用它写 trans_by。

    优先取本回合实际在跑的模型名（_resolve_llm 解析出的 _model），拿不到时退回 state
    里那份后端配置（「了解项目」报的就是它）；都没有就返回空串——宁可不写标记，
    也不要瞎填一个模型名。
    """
    model = str(getattr(runner, "_model", "") or "").strip()
    if model:
        return model
    state = runner.state
    profile = getattr(state, "backend_profile_data", None) or {}
    name = getattr(state, "backend_profile_name", "") or ""
    return str(_backend_summary(profile, name).get("model") or "")


# read_transl_cache 一个工具管三件读缓存的事（原来是 list/read/search_transl_cache 三个工具）。
CACHE_READ_ACTIONS: tuple[str, ...] = ("list", "read", "search")


def _cache_read_action(args: dict[str, Any]) -> str:
    """read_transl_cache 的 action：显式给了就校验；没给按参数推断——
    有 query 是 search，有 filename 是 read，都没有就是 list（先看有哪些文件）。"""
    action = str(args.get("action", "") or "").strip().lower()
    if action:
        if action not in CACHE_READ_ACTIONS:
            raise AgentToolError(f"action 只能是 {' / '.join(CACHE_READ_ACTIONS)}，收到 {action!r}")
        return action
    if str(args.get("query", "") or "").strip():
        return "search"
    if str(args.get("filename", "") or "").strip():
        return "read"
    return "list"


def _tool_read_transl_cache(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """读缓存：list=列缓存文件，read=读某个文件的条目，search=在缓存里搜。

    结果带上 action，渲染器（_md_render_transl_cache）据此选表格样式。
    """
    action = _cache_read_action(args)
    if action == "list":
        result = _tool_list_transl_cache(runner, args)
    elif action == "search":
        if not str(args.get("query", "") or "").strip():
            raise AgentToolError("action=search 需要 query（要搜的关键词）")
        result = _tool_search_transl_cache(runner, args)
    else:
        result = _read_transl_cache_entries(runner, args)
    return {"action": action, **result} if isinstance(result, dict) else result


# ---- 换行归一化（patch_transl_cache 写回前）----
# 模型在 Markdown 表格里看到的换行是 <br>——这不是渲染器的发明：翻译管线送翻时，原文里的
# 换行（真 \r\n、真 \n、乃至字面两字符 \r\n/\n）本来就全部替换成 <br> 再发给模型
# （ForGalJsonTranslate.py:72-83），模型输出的 <br> 在落盘前又会被换回真换行
# （_normalize_parsed_translation_text）。所以模型写 <br> 是管线内的标准写法。
# 真正的缺口在 patch 的写路径：正常翻译有"模型输出 → 缓存"的归一化，patch 没有——
# 模型照着表格写 <br> 就原样进了缓存，译文里的真换行变成了字面 <br>。
# 下面把这条补上：写回前把 <br> / 真换行 / 字面 \n 全部统一成**该条目自己的换行形式**。

def _infer_linebreak_symbol(*texts: str) -> str:
    """从参考文本里推断"这条数据用的换行符"（优先级照抄翻译管线，ForGalJsonTranslate.py:72-79）：

    字面 \\r\\n > 真 \\r\\n > 字面 \\n > 真 \\n；都没有返回空串。给多条参考文本时取**第一个
    有换行的**——patch 归一化里先给字段现值（正在编辑的那份，风格以它为准）、再给 post_src。
    """
    for text in texts:
        if not text:
            continue
        if "\\r\\n" in text:
            return "\\r\\n"
        if "\r\n" in text:
            return "\r\n"
        if "\\n" in text:
            return "\\n"
        if "\n" in text:
            return "\n"
    return ""


def _normalize_linebreaks_like(value: str, *reference_texts: str) -> str:
    """把 value 里的各种换行形态统一成参考文本所用的那种。

    参考文本推断不出换行（整条没有换行）时**原样返回**——管线在 n_symbol 为空时同样不动
    模型输出的 <br>，保持同一口径。
    """
    if not value:
        return value
    symbol = _infer_linebreak_symbol(*reference_texts)
    if not symbol:
        return value
    # 先把所有形态折成真 \n：<br>（宽松大小写与自闭合）、真 \r\n / \r、
    # 字面两字符 \r\n / \n / \r（模型双重转义时会出现）
    v = re.sub(r"(?i)<br\s*/?>", "\n", value)
    v = v.replace("\r\n", "\n").replace("\r", "\n")
    v = v.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
    # 再展开成该条目的换行形式
    if symbol != "\n":
        v = v.replace("\n", symbol)
    return v


def _group_cache_patches_by_file(args: dict[str, Any]) -> list[tuple[str, list[Any]]]:
    """把这次调用的 patches 按缓存文件分组（**按文件首次出现的顺序**）。

    两种写法都认、也能混着写（见 patch_transl_cache 的 schema）：

    - 顶层 filename + patches（每条不带 file）：老写法，一次一个文件；
    - patches 里每条自带 file：跨文件一次提交——统一译名/术语这类活不必"一个文件一次调用"，
      也就不会出现"点了停止、11 个文件只落了 2 个、剩下 9 个还得自己记住改到哪"。

    顶层 filename 是"没写 file 的那些 patch"的默认文件。两边都没给就直接报错——与其猜一个
    文件改错地方，不如让模型补一个入参。
    """
    patches_raw = args.get("patches")
    if not isinstance(patches_raw, list) or not patches_raw:
        raise AgentToolError("patches must be a non-empty array")
    default = str(args.get("filename", "") or "").strip()
    groups: dict[str, list[Any]] = {}
    for position, patch in enumerate(patches_raw, start=1):
        name = default
        if isinstance(patch, dict):
            name = str(patch.get("file", "") or "").strip() or default
        if not name:
            raise AgentToolError(
                f"第 {position} 条 patch 没写 file，顶层也没给 filename："
                "只改一个文件就给顶层 filename，要一次改多个文件就每条 patch 都写 file。"
            )
        groups.setdefault(name, []).append(patch)
    return list(groups.items())


def _cache_replace_request(args: dict[str, Any], allowed: frozenset[str]) -> dict[str, Any] | None:
    action = args.get("action", "patch")
    if action not in ("patch", "replace"):
        raise AgentToolError("action must be patch or replace")
    if action == "patch":
        return None
    if "patches" in args:
        raise AgentToolError("action=replace cannot be combined with patches")
    query = args.get("query")
    replacement = args.get("replacement")
    if not isinstance(query, str) or not query:
        raise AgentToolError("query must be a non-empty string")
    if not isinstance(replacement, str):
        raise AgentToolError("replacement must be a string (empty string deletes matches)")
    filenames = args.get("files")
    if filenames is None:
        filenames = [args.get("filename")]
    elif args.get("filename"):
        raise AgentToolError("action=replace requires either files or filename, not both")
    if isinstance(filenames, str):
        filenames = [filenames]
    if not isinstance(filenames, list) or not filenames:
        raise AgentToolError("files must be a cache filename/glob or a non-empty array of them")
    if any(not isinstance(name, str) or not name.strip() for name in filenames):
        raise AgentToolError("each file must be a non-empty cache filename")
    fields = args.get("fields", ["pre_dst", "proofread_dst"])
    if not isinstance(fields, list) or not fields:
        raise AgentToolError("fields must be a non-empty array")
    if any(not isinstance(field, str) or field not in _TRANSLATION_FIELDS or field not in allowed for field in fields):
        raise AgentToolError("replace fields must be allowed translation fields: pre_dst / proofread_dst")
    return {
        "files": list(dict.fromkeys(name.strip() for name in filenames)),
        "fields": list(dict.fromkeys(fields)),
        "query": query,
        "replacement": replacement,
    }


def _resolve_cache_replace_files(runner: AgentRunner, selectors: list[str]) -> list[str]:
    """展开缓存文件选择器；预览与执行共用，重叠选择器只处理一次。"""
    if not any(any(char in name for char in "*?[") for name in selectors):
        return selectors
    pid = runner._project_id()
    listing = runner._http_get(f"/api/projects/{pid}/cache")
    available = sorted({
        item["name"] for item in listing.get("files", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
        and item.get("is_file", True) and item["name"].endswith(".json")
    })
    selected: dict[str, None] = {}
    for selector in selectors:
        # 字面文件名优先，兼容名字本身带方括号的缓存。
        if selector in available or not any(char in selector for char in "*?["):
            selected[selector] = None
            continue
        matches = [name for name in available if fnmatch.fnmatchcase(name, selector)]
        if not matches:
            raise AgentToolError(f"缓存文件通配符未匹配任何文件：{selector}")
        selected.update(dict.fromkeys(matches))
    return list(selected)


def _plan_cache_replacements(
    entries: list[Any], request: dict[str, Any], allowed: frozenset[str], *, clear_comment: bool = False,
) -> dict[str, Any]:
    patches = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        updates = {}
        for field in request["fields"]:
            value = _cache_field_value(entry, field)
            if isinstance(value, str) and request["query"] in value:
                after = value.replace(request["query"], request["replacement"])
                if after != value:
                    updates[field] = after
        if updates:
            patches.append({"index": entry.get("index"), **updates})
    # 文本替换保留命中范围之外的字符，包括已有换行和转义序列。
    return _plan_cache_patches(
        entries, patches, allowed, clear_comment=clear_comment, normalize_linebreaks=False,
    )


def _plan_cache_patches(
    entries: list[Any],
    patches_raw: list[Any],
    allowed: frozenset[str],
    *,
    clear_comment: bool = False,
    normalize_linebreaks: bool = True,
) -> dict[str, Any]:
    """把 patches 解析成「要改哪些条目的哪些字段」（**只读**，不动 entries）。

    patch_transl_cache 的落盘与审批卡上的「将要变更」预览共用这一份判断：卡上给用户看的
    before→after 就是真执行会写下去的东西，不会两边各算一遍再漂移。调用方拿到 plan 后
    自己逐条 entry.update(updates) 才算写。

    clear_comment=True 时，**点名的条目**（落进 plan 的那些）顺带把 proofread_comment 清空——按
    批注改完一批译文后，不必再在每条 patch 里各写一遍 `"proofread_comment": ""`（意见几十条时，
    重复的键名本身就要占不少输出）。两条边界：本来就空的批注不产生变更（免得刷出一堆 `"" → ""`
    的噪音行）；某条 patch 自己带了 proofread_comment（含空串）就以它为准，不被这个开关覆盖。

    返回：
    - by_index：index → 条目（handler 盖章 trans_by 时要用）
    - plan：[{entry, index, updates}]，按顺序应用
    - changes / skipped / not_found：与原来逐条累积出来的字段一致
    """
    by_index: dict[int, dict[str, Any]] = {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        idx = e.get("index")
        if idx is not None:
            try:
                by_index[int(idx)] = e
            except (TypeError, ValueError):
                continue

    plan: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    not_found: list[int] = []
    changes: list[dict[str, Any]] = []
    # 同一个 index 被两条 patch 命中时，第二条的 before 要看见第一条的结果（真执行是逐条
    # entry.update），所以在影子副本上模拟同样的链式修改，算出来的 diff 才一致。
    shadow: dict[int, dict[str, Any]] = {}
    for p in patches_raw:
        if not isinstance(p, dict):
            skipped.append({"index": None, "reason": "patch 不是对象"})
            continue
        idx = p.get("index")
        try:
            idx_i = int(idx)
        except (TypeError, ValueError):
            skipped.append({"index": idx, "reason": "index 不是整数"})
            continue
        entry = by_index.get(idx_i)
        if entry is None:
            not_found.append(idx_i)
            continue
        updates = {k: v for k, v in p.items() if k in allowed and v is not None}
        now = shadow.setdefault(idx_i, dict(entry))
        # 顶层 clear_comment：把点名条目的批注一并清掉。看的是 now（影子副本）而不是 entry——同一个
        # index 被前面那条 patch 写过批注时，要清的是"当前那份"，与真执行的链式修改一致。
        # 某条 patch 自己带了 proofread_comment（含空串）就不插手：显式写的以它为准。
        if (
            clear_comment
            and "proofread_comment" not in updates
            and "proofread_comment" in allowed
            and now.get("proofread_comment")
        ):
            updates["proofread_comment"] = ""
        if not updates:
            reason = f"无可更新字段（只允许 {_patchable_fields_text(allowed)}）"
            if clear_comment and "proofread_comment" in allowed:
                reason += "，这条本来也没有校对批注可清"
            skipped.append({"index": idx_i, "reason": reason})
            continue
        for f, v in list(updates.items()):
            if normalize_linebreaks and isinstance(v, str):
                # 换行归一化：字段现值（正在编辑的那份）的风格优先，其次该条 post_src 的风格。
                # 归一化后的值就是真执行会写下去的东西，变更卡的 before→after 也用它——所见即所得。
                v = _normalize_linebreaks_like(
                    v, str(now.get(f) or ""), str(entry.get("post_src") or "")
                )
                updates[f] = v
            if _cache_field_value(now, f) == v:
                updates.pop(f)
                continue
            changes.append(_change(f"#{idx_i}.{f}", _cache_field_value(now, f), v, "replace"))
            now[f] = v
        if not updates:
            continue
        plan.append({"entry": entry, "index": idx_i, "updates": updates})
    return {
        "by_index": by_index,
        "plan": plan,
        "changes": changes,
        "skipped": skipped,
        "not_found": not_found,
    }


# 缓存文件的读-改-写锁：/cache/save 是整文件覆盖，并行子代理切同一个文件的不同
# index 段同时 patch 时，不加锁后存的会把先存的改动整个盖掉。按 (项目, 文件) 分锁。
_CACHE_FILE_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_CACHE_FILE_LOCKS_GUARD = threading.Lock()


def _cache_file_lock(runner: AgentRunner, filename: str) -> threading.Lock:
    project_dir = str(getattr(runner.state, "project_dir", "") or "")
    key = (os.path.normcase(os.path.abspath(project_dir)) if project_dir else "", os.path.normcase(filename))
    with _CACHE_FILE_LOCKS_GUARD:
        lock = _CACHE_FILE_LOCKS.get(key)
        if lock is None:
            lock = _CACHE_FILE_LOCKS[key] = threading.Lock()
        return lock


def _patch_one_cache_file(
    runner: AgentRunner,
    pid: str,
    filename: str,
    patches: list[Any],
    allowed: frozenset[str],
    *,
    qualify: bool,
    clear_comment: bool = False,
    replace_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with _cache_file_lock(runner, filename):
        return _patch_one_cache_file_locked(
            runner, pid, filename, patches, allowed, qualify=qualify, clear_comment=clear_comment,
            replace_request=replace_request,
        )


def _patch_one_cache_file_locked(
    runner: AgentRunner,
    pid: str,
    filename: str,
    patches: list[Any],
    allowed: frozenset[str],
    *,
    qualify: bool,
    clear_comment: bool = False,
    replace_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """改一个缓存文件里的若干条目：读全量 → 计划 → 写全量 → 回「改了什么、还剩什么问题」。

    qualify=True 时给每条变更的 path 加「文件名#」前缀：一次调用跨了多个文件时，变更卡与
    结果里得看得出这一条改的是哪份文件（只改一个文件时省掉，保持原来的短路径）。
    clear_comment=True 时点名条目的 proofread_comment 一并清空（见 _plan_cache_patches）。
    一条都没落地时**不抛异常**，把原因放进 `error` 返回——跨文件批量时别的文件还要改
    （部分成功），整次算不算失败由 _tool_patch_transl_cache 汇总判定。
    """
    # 读现有条目，按 index 建索引，只为命中的条目应用补丁，再整体写回。
    # /cache/save 会整体覆盖文件并由后端重建 problem/post_dst_preview，
    # 所以这里必须读全量 -> 改 -> 写全量，而非只写补过的几条。
    data = runner._http_get(f"/api/projects/{pid}/cache/{urllib.parse.quote(filename)}")
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        raise AgentToolError(f"「{filename}」的 entries 非数组，无法 patch")

    planned = (
        _plan_cache_replacements(entries, replace_request, allowed, clear_comment=clear_comment)
        if replace_request is not None
        else _plan_cache_patches(entries, patches, allowed, clear_comment=clear_comment)
    )
    by_index = planned["by_index"]
    skipped = planned["skipped"]
    not_found = planned["not_found"]
    applied_indexes: list[int] = []
    retranslated_indexes: list[int] = []
    for item in planned["plan"]:
        item["entry"].update(item["updates"])
        applied_indexes.append(item["index"])
        if item["updates"].keys() & _TRANSLATION_FIELDS:
            retranslated_indexes.append(item["index"])
    changes: list[dict[str, Any]] = []
    for change in planned["changes"]:
        row = {**change, "file": filename}
        if qualify and isinstance(row.get("path"), str):
            # path 本来就以 `#` 开头（`#33.pre_dst`），前缀只补文件名
            row["path"] = f"{filename}{row['path']}"
        changes.append(row)

    result: dict[str, Any] = {
        "filename": filename,
        "updated": len(applied_indexes),
        "changes": changes,
    }
    if not applied_indexes and not skipped and not not_found:
        result["note"] = "内容没有变化，未保存文件"
    elif not applied_indexes:
        # 把跳过原因带上：否则模型只看到"没有条目被更新"，不知道是字段不许改还是 index 写错了
        # （窄字段白名单也通过这里返回拒绝原因）
        reasons = "；".join(str(s.get("reason") or "") for s in skipped if s.get("reason"))
        result["error"] = (
            f"没有条目被更新（skipped={len(skipped)}, not_found={len(not_found)}）"
            + (f"：{reasons}" if reasons else "")
        )
    if not_found:
        result["not_found"] = [{"file": filename, "index": idx} for idx in sorted(not_found)]
    if skipped:
        result["skipped"] = [{**s, "file": filename} for s in skipped]
    if not applied_indexes:
        return result  # 没有要写的东西：别白跑一次 /cache/save（它还会重建 problem）

    # 译文被改过的条目标上本会话的模型名（trans_by 不在 _PATCHABLE_FIELDS 里，模型指定不了）：
    # 用户与后续复核才分得清"这句是 Agent 手改的"还是"翻译引擎翻的"。
    # 只写校对意见（proofread_comment）的条目**不盖章**——译文一个字没动，盖了会把"谁翻的"弄错，
    # 也会让 trans_by 的少数派统计多出一堆假来源。
    agent_model = _agent_model_name(runner)
    if agent_model and retranslated_indexes:
        for idx_i in retranslated_indexes:
            by_index[idx_i]["trans_by"] = agent_model
        result["trans_by"] = agent_model

    save_body = {
        "filename": filename,
        "entries": entries,
        "config_file_name": runner.state.config_file_name,
    }
    # 注意：/cache/save 的应答带全文件 entries（重建 problem 后原样回传给
    # 桌面端用），绝不能整体透传给 LLM。这里只提取「被改条目重建后仍存在的问题」
    # 作为轻量校验信号——没引入新问题的条目不出现在 problems 里；改了什么由
    # changes 的 before→after 表达，不重复回传最终译文，不必再 read_transl_cache。
    save_result = runner._http_post(f"/api/projects/{pid}/cache/save", save_body)
    saved_entries = save_result.get("entries") if isinstance(save_result, dict) else None
    result["verification"] = (
        "checked" if isinstance(saved_entries, list) and save_result.get("verification", "checked") == "checked" else "unknown"
    )
    if isinstance(saved_entries, list):
        wanted = set(applied_indexes)
        problems: list[dict[str, Any]] = []
        for e in saved_entries:
            if not isinstance(e, dict):
                continue
            try:
                idx_i = int(e.get("index"))
            except (TypeError, ValueError):
                continue
            if idx_i not in wanted:
                continue
            problem = str(e.get("problem", ""))
            if not problem:
                continue
            problems.append({
                "file": filename,
                "index": idx_i,
                "problem": problem[:120] + ("…" if len(problem) > 120 else ""),
            })
        if problems:
            result["problems"] = problems
    return result


def _tool_patch_transl_cache(
    runner: AgentRunner, args: dict[str, Any], allowed_fields: frozenset[str] | None = None
) -> Any:
    """改缓存条目的字段（主 Agent 可改 pre_dst / proofread_dst / proofread_comment）。

    **一次调用可以跨多个缓存文件**：patches 里每条自带 file，或只改一个文件时用顶层 filename
    （见 _group_cache_patches_by_file）。落盘仍是"一个文件一次 /cache/save"（后端接口本来就按
    文件整体覆盖、重建 problem），但工具调用只有一次——统一译名这类活一次交完，中断时模型也
    不必自己记"改到哪个文件了"：结果按文件给出改了什么、哪条没落地。

    顶层 clear_comment=true 让**点名条目的校对批注一并清空**：按批注改完一批译文后，不必在每条
    patch 里各写一遍 `"proofread_comment": ""`（见 _plan_cache_patches）。它只对主 Agent 生效，
    见下面 allowed_fields 那段。

    allowed_fields 是内部调用可选的字段白名单。校对子代理走 ProofreadFixer，复用单文件
    patch 实现，并额外校验职责范围、读取快照、停止信号和修改记录。
    """
    allowed = allowed_fields if allowed_fields is not None else _PATCHABLE_FIELDS
    # 显式窄白名单调用不启用批量清批注，避免顺手删除未处理意见。
    clear_comment = bool(args.get("clear_comment")) and allowed_fields is None
    replace_request = _cache_replace_request(args, allowed)
    targets = (
        [(name, []) for name in _resolve_cache_replace_files(runner, replace_request["files"])]
        if replace_request is not None else _group_cache_patches_by_file(args)
    )
    pid = runner._project_id()
    qualify = len(targets) > 1  # 跨文件才给 path 加文件名前缀
    files: list[dict[str, Any]] = []
    for filename, patches in targets:
        try:
            files.append(
                _patch_one_cache_file(
                    runner,
                    pid,
                    filename,
                    patches,
                    allowed,
                    qualify=qualify,
                    clear_comment=clear_comment,
                    replace_request=replace_request,
                )
            )
        except AgentToolError as exc:
            # 单个文件没改成（文件名写错 / 接口报错）不该带走整批：一次改十几个文件时，
            # 其中一个出错不该让另外那些白做。记在这份文件上，下面汇总时统一交代。
            files.append({"filename": filename, "updated": 0, "changes": [], "error": str(exc)})

    updated = sum(int(item.get("updated") or 0) for item in files)
    if not updated and all(item.get("error") for item in files):
        detail = "；".join(
            f"{item['filename']}：{item.get('error') or '没有条目被更新'}" for item in files
        )
        raise AgentToolError(f"没有条目被更新（{detail}）")

    result: dict[str, Any] = {
        "updated": updated,
        # files：按文件分组的结果（Markdown 就是按它分节的）
        "files": files,
        # changes：跨文件汇总一份——前端变更卡认的是顶层 changes（见 extractChangeList），
        # 跨文件时 path 带「文件名#」前缀，一眼看得出改的是哪份。
        "changes": [change for item in files for change in item.get("changes") or []],
    }
    if replace_request is not None:
        result["action"] = "replace"
    trans_by = next((str(item["trans_by"]) for item in files if item.get("trans_by")), "")
    if trans_by:
        result["trans_by"] = trans_by
    return result


def _plan_cache_delete(
    entries: list[Any], wanted: set[int]
) -> tuple[list[Any], list[int], list[dict[str, Any]]]:
    """按 index 挑出要删的条目（**只读**）：返回（保留的条目、命中的 index、被删条目的预览）。

    delete_transl_cache 的落盘与审批卡上的「将要变更」预览共用这一份挑选：卡上列出的
    "会删掉哪几条、删的是什么"就是真执行会删的那些。
    """
    kept: list[Any] = []
    deleted_indexes: list[int] = []
    previews: list[dict[str, Any]] = []
    for e in entries:
        try:
            idx = int(e.get("index"))
        except (TypeError, ValueError):
            kept.append(e)
            continue
        if idx in wanted:
            deleted_indexes.append(idx)
            preview = str(e.get("pre_dst", "") or e.get("post_dst", "") or "")
            if len(preview) > 60:
                preview = preview[:57] + "…"
            previews.append({"index": idx, "text": preview})
        else:
            kept.append(e)
    return kept, deleted_indexes, previews


def _tool_delete_transl_cache(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """删除缓存：物理删除条目后，重启翻译时这些句子会 cache 未命中而重新翻译。

    两种粒度：
    - 指定 indexes：删除某个缓存文件里的部分条目（index 可用 read_transl_cache /
      list_problems 返回的 index，支持 "33-40,50-60" 区间写法）
    - 不指定 indexes：删除整个缓存文件（该文件全部句子重翻）
    删除不可撤销；rebuilda/rebuildr 依赖缓存存在，删除后不要跑重建。
    """
    filename = str(args.get("filename", "")).strip()
    if not filename:
        raise AgentToolError("filename is required")
    pid = runner._project_id()
    index_spec = str(args.get("indexes", "") or "").strip()

    # 整文件删除
    if not index_spec:
        if filename == "*" or filename == "all":
            # 全部缓存文件：列出后逐个删
            listing = runner._http_get(f"/api/projects/{pid}/cache")
            targets = [f["name"] for f in listing.get("files", []) if str(f.get("name", "")).endswith(".json")]
            if not targets:
                return {"deleted_files": [], "not_found_files": [], "note": "没有可删除的缓存文件"}
            res = runner._http_post(f"/api/projects/{pid}/cache/delete-file", {"filenames": targets})
            return {"deleted_files": res.get("deleted_files", []), "not_found_files": res.get("not_found_files", []), "note": "已删除全部缓存文件，重启翻译将全部重翻"}
        res = runner._http_post(f"/api/projects/{pid}/cache/delete-file", {"filenames": [filename]})
        return {"deleted_files": res.get("deleted_files", []), "not_found_files": res.get("not_found_files", [])}

    # 按 index 删除部分条目：读全量 -> 剔除命中 -> 写回（与 patch_transl_cache 同通道）
    wanted = _parse_index_spec(index_spec)
    if not wanted:
        raise AgentToolError(f"无法解析 indexes：{index_spec!r}（示例：33-40,50-60）")
    with _cache_file_lock(runner, filename):
        data = runner._http_get(f"/api/projects/{pid}/cache/{urllib.parse.quote(filename)}")
        entries = data.get("entries", [])
        if not isinstance(entries, list):
            raise AgentToolError("缓存文件 entries 非数组，无法删除")

        kept, deleted_indexes, deleted_previews = _plan_cache_delete(entries, wanted)

        if not deleted_indexes:
            raise AgentToolError(f"没有命中的条目（文件共 {len(entries)} 条，请求 index：{sorted(wanted)}）")

        save_body = {
            "filename": filename,
            "entries": kept,
            "config_file_name": runner.state.config_file_name,
        }
        runner._http_post(f"/api/projects/{pid}/cache/save", save_body)
    missing = sorted(i for i in wanted if i not in deleted_indexes)
    result: dict[str, Any] = {
        "filename": filename,
        "count_before": len(entries),
        "count_after": len(kept),
        "deleted_indexes": deleted_indexes,
        "deleted_preview": deleted_previews[:50],
        "note": "被删除的句子已不在缓存中，重启翻译（start_translation）时它们会重新翻译",
    }
    if missing:
        result["missing_indexes"] = missing
    return result
