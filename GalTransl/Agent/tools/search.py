"""搜索类工具：在翻译缓存、待翻译原文与最终输出里搜索（分页）。"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from GalTransl.Agent.models import AgentToolError
from GalTransl.Search import parse_search_order, search_order_note
from GalTransl.Agent.tools.common import (
    _dominant_trans_by,
    _mark_context_row,
    _only_preceding_arg,
    _strip_dominant_trans_by,
)

if TYPE_CHECKING:
    from GalTransl.Agent.runner import AgentRunner


# /cache/search 返回体里逐行的命中标记（服务端给界面用的：缓存页拿它画「原文/译文/问题」
# 小徽标）。逐行丢给模型纯属噪音——命中的位置从行内容（post_src / pre_dst / problem）直接
# 看得到。这里收成顶层一条汇总（见 _slim_search_results）。
_SEARCH_MATCH_KEYS: dict[str, str] = {
    "match_src": "src",
    "match_dst": "dst",
    "match_problem": "problem",
}

# /input/search 同样有逐行命中标记，只是原文侧可搜的只有两列：正文与说话人。
_INPUT_SEARCH_MATCH_KEYS: dict[str, str] = {
    "match_src": "src",
    "match_name": "name",
}
_OUTPUT_SEARCH_MATCH_KEYS: dict[str, str] = {
    "match_dst": "dst",
    "match_name": "name",
}


def _slim_search_results(
    result: dict[str, Any],
    field: str,
    context: int,
    match_keys: dict[str, str] | None = None,
) -> None:
    """就地精简搜索接口的返回（模型看到的那份）：/cache/search 与 /input/search 共用。

    - 逐行的命中标记（缓存是 match_src/match_dst/match_problem，原文是 match_src/match_name）
      去掉，改成顶层 matched_in 汇总一次（只在 field="all" 时给：指定 field 的搜索本来就只有
      那一侧会命中，汇总没有信息量）；
    - 逐行的 in_context（服务端标注的上下文行）删掉，改成把**上下文行的 index 标成 "12*"**
      ——"哪行是搭着给的上文"必须一眼看得出来（index 是跨工具对齐条目的把手），而一个
      布尔标注字段每行都要占一截；带上下文（context>0）时才有这回事；
    - trans_by（只有缓存条目有）逐条只给**少数派**（见 _dominant_trans_by）：逐条的多数派与
      空值都删掉，多数派放顶层 majority_trans_by 记一次——这批"大头是谁翻的、哪几条是别人改的"
      一目了然。
    """
    keys = match_keys if match_keys is not None else _SEARCH_MATCH_KEYS
    rows = result.get("results")
    if not isinstance(rows, list):
        return
    dominant = _dominant_trans_by(rows)
    counts: dict[str, int] = {}
    slimmed: list[Any] = []
    for row in rows:
        if not isinstance(row, dict):
            slimmed.append(row)
            continue
        matched = False
        for key, label in keys.items():
            if row.get(key):
                counts[label] = counts.get(label, 0) + 1
                matched = True
        clean = {k: v for k, v in row.items() if k not in keys}
        clean.pop("in_context", None)
        # 带上下文时：没有任何命中标记的行就是搭着给的上文（命中行一定有标记），标个 *
        if context > 0 and not matched:
            clean = _mark_context_row(clean)
        _strip_dominant_trans_by(clean, dominant)
        slimmed.append(clean)
    result["results"] = slimmed
    if field == "all" and counts:
        result["matched_in"] = counts
    if dominant:
        result["majority_trans_by"] = dominant


# 搜索类工具的分页口径（与 list_problems 同名同义）：limit 是本页最多几条**命中**，
# offset 是跳过前几条命中。默认 100 = 历史行为（"一次看遍某个词的所有出现处"是搜索的常用
# 姿势，不宜像 list_problems 那样默认 10）。
_SEARCH_LIMIT_DEFAULT = 100
_SEARCH_LIMIT_MAX = 200
# 带 context 时一行命中要搭上前后各 N 句，所以整页行数按它换算命中上限
# （命中数 ≈ 200 // (2N+1)，见 _tool_search_transl_cache）——**整个返回体最多 200 行**。
# 不带 context 时行数就是命中数，而 limit 上限本身就是 200，两边口径一致。
_SEARCH_ROW_BUDGET = 200


def _search_order(args: dict[str, Any]) -> str:
    try:
        return parse_search_order(args)
    except ValueError as exc:
        raise AgentToolError(str(exc)) from exc


def _apply_search_order(result: dict[str, Any], order: str) -> None:
    result["order"] = order
    note = search_order_note(order)
    if note:
        result["note"] = "；".join(part for part in (str(result.get("note") or ""), note) if part)


def _search_paging_args(args: dict[str, Any]) -> tuple[int, int]:
    """limit / offset：与本仓库其它清单工具同一套（非法值按默认处理，不报错）。"""
    raw_limit = args.get("limit", _SEARCH_LIMIT_DEFAULT)
    raw_offset = args.get("offset", 0)
    try:
        limit = max(1, min(int(raw_limit), _SEARCH_LIMIT_MAX))
    except (TypeError, ValueError):
        limit = _SEARCH_LIMIT_DEFAULT
    try:
        offset = max(0, int(raw_offset))
    except (TypeError, ValueError):
        offset = 0
    return limit, offset


def _apply_search_paging(result: dict[str, Any], offset: int) -> None:
    """给搜索结果补上分页口径（就地改），字段名与 list_problems 对齐。

    - returned：本页的**命中数**（服务端的 returned_hits；不带 context 时就是行数）；
    - returned_rows：本页总行数（带 context 时 = 命中行 + 搭着给的前后文行）；
    - has_more：offset 之后还有命中。total 始终是全部命中数，不受分页影响。
    """
    rows = result.get("results")
    hits = result.pop("returned_hits", None)
    row_count = result.pop("returned", None)
    if not isinstance(hits, int):
        hits = len(rows) if isinstance(rows, list) else 0
    result["offset"] = offset
    result["returned"] = hits
    if isinstance(row_count, int):
        result["returned_rows"] = row_count
    total = result.get("total")
    result["has_more"] = isinstance(total, int) and offset + hits < total


def _tool_search_transl_cache(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """在缓存里搜译文/原文/问题；context=N 时每条命中再带上 N 句上文（默认只给上文）。

    只给命中行常常不够判断——比如查「ドルード」，要决定该译成「多鲁德」还是「杜罗德」，
    得看这句前后的对话与说话人，和 read_transl_cache 的 context 是同一个用途。
    """
    query = str(args.get("query", "")).strip()
    field = str(args.get("field", "all")).strip() or "all"
    if not query:
        raise AgentToolError("query is required")
    filename = str(args.get("filename", "") or "").strip()
    raw_context = args.get("context", 0)
    try:
        context = max(0, min(int(raw_context or 0), 20))
    except (TypeError, ValueError):
        raise AgentToolError(f"context 必须是 0-20 的整数（收到 {raw_context!r}）")
    # 带上下文时收紧命中上限：命中 × 每条搭的行数一起返回，整页压在 _SEARCH_ROW_BUDGET 行内，
    # 否则"命中上百条 × 前后各几句"会直接把返回体撑爆。只给上文时每条只搭 N 行，同样的行数
    # 预算里能多给几条命中。total 不受影响（仍报全部命中数）。
    only_preceding = _only_preceding_arg(args)
    order = _search_order(args)
    limit, offset = _search_paging_args(args)
    rows_per_hit = (context + 1) if only_preceding else (2 * context + 1)
    max_hits = limit if context == 0 else min(limit, max(1, _SEARCH_ROW_BUDGET // rows_per_hit))
    pid = runner._project_id()
    body: dict[str, Any] = {
        "query": query,
        "field": field,
        "options": {"re": False},
        "max_results": max_hits,
        "order": order,
        "config_file_name": runner.state.config_file_name,
    }
    if context:
        body["context"] = context
        if only_preceding:
            body["preceding_only"] = True  # 服务端默认两边都给（界面在用），这边显式只要上文
    if offset:
        body["offset"] = offset
    if filename:
        body["filename"] = filename
    result = runner._http_post(f"/api/projects/{pid}/cache/search", body)
    if isinstance(result, dict):
        _slim_search_results(result, field, context)
        _apply_search_paging(result, offset)
        _apply_search_order(result, order)
    notes: list[str] = []
    if isinstance(result, dict) and context:
        result["context"] = context  # 服务端已回；这里兜底，保证调用方一定看得到
        result["only_preceding"] = only_preceding
        scope = (
            f"每条命中前面 {context} 句（默认只给上文，要上下都给传 only_preceding=false）"
            if only_preceding
            else f"每条命中前后各 {context} 句"
        )
        notes.append(
            f"已带上下文：{scope}（包含关键词的那行是命中，index 带 * 的是搭着给的上下文行）；"
            f"带上下文时命中上限收紧为 {max_hits} 条、整页最多 {_SEARCH_ROW_BUDGET} 行，"
            "total 仍是全部命中数——命中很多时用 offset 翻页，或配合 filename 缩小范围。"
        )
    # 指定了文件但 0 命中：确认一下该文件是否存在，避免模型误以为关键词不匹配
    if isinstance(result, dict) and not result.get("total") and filename:
        try:
            listing = runner._http_get(f"/api/projects/{pid}/cache")
            if not any(f.get("name") == filename for f in listing.get("files", [])):
                # 文件确实存在、只是还没翻译：缓存里当然搜不到，要搜它的原文得换 search_input
                if any(f.get("name") == filename for f in listing.get("uncached_files", [])):
                    notes.append(
                        f"{filename} 还没翻译（没有缓存文件），这里是缓存搜索的 0 命中；"
                        f"要搜它的原文用 search_input（filename={filename}）。"
                    )
                else:
                    notes.append(
                        f"缓存文件 {filename} 不存在（检查 read_transl_cache（action=list）清单里的文件名拼写）；这是全项目搜索的 0 命中。"
                    )
        except AgentToolError:
            pass
    if notes and isinstance(result, dict):
        existing = str(result.get("note") or "")
        result = {**result, "note": "；".join([part for part in [existing, *notes] if part])}
    return result


def _tool_search_input(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """在待翻译原文里搜关键词/说话人；context=N 时每条命中再带上 N 句上文（默认只给上文）。

    与 read_transl_cache(action="search") 是一套用法，区别只在搜的对象：那边搜**缓存**（原文 + 译文 + 问题，
    含已翻的部分），这边搜**输入文件**（还没翻译的原文全文）。用途也由此分工——
    - 定译法/收字典前，查某个称呼、口头禅、专有名词在全篇出现过多少次、都出现在什么上下文里
      （出现次数与说话人是"该不该收进字典、收哪个写法"的依据）；
    - 拿不准某句原文的语境时，比 read_input_file 逐段读更省 token；
    - 命中的 filename + index 可直接交给 read_input_file 精读。

    搜的是原文，所以**译文侧的问题（漏译/残留日文）不在这里**，那些用 read_transl_cache(action="search")。
    每次搜索都要把涉及的输入文件过一遍文件插件（比搜缓存慢），要缩小范围就传 filename。
    """
    return _tool_search_text_files(runner, args)


def _tool_search_output_files(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """搜索 gt_output 的实际正文与说话人，核查译后字典替换效果。"""
    return _tool_search_text_files(runner, args, output=True)


def _tool_search_text_files(runner: AgentRunner, args: dict[str, Any], *, output: bool = False) -> Any:
    source = "output" if output else "input"
    label = "输出" if output else "输入"
    read_tool = "read_output" if output else "read_input_file"
    text_field = "dst" if output else "src"
    query = str(args.get("query", "")).strip()
    if not query:
        raise AgentToolError("query is required")
    field = str(args.get("field", "all") or "all").strip() or "all"
    if field not in ("all", text_field, "name"):
        raise AgentToolError(f"field must be one of: all, {text_field}, name")
    filename = str(args.get("filename", "") or "").strip()
    raw_context = args.get("context", 0)
    try:
        context = max(0, min(int(raw_context or 0), 20))
    except (TypeError, ValueError):
        raise AgentToolError(f"context 必须是 0-20 的整数（收到 {raw_context!r}）")
    # 与缓存搜索（_tool_search_transl_cache）同一套收紧规则：命中 × 每条搭的行数一起返回，整页压在
    # _SEARCH_ROW_BUDGET 行内。total 不受影响（仍是全部命中数）。
    only_preceding = _only_preceding_arg(args)
    order = _search_order(args)
    limit, offset = _search_paging_args(args)
    rows_per_hit = (context + 1) if only_preceding else (2 * context + 1)
    max_hits = limit if context == 0 else min(limit, max(1, _SEARCH_ROW_BUDGET // rows_per_hit))
    pid = runner._project_id()
    body: dict[str, Any] = {
        "query": query,
        "field": field,
        "options": {"re": False},
        "max_results": max_hits,
        "order": order,
        "config_file_name": runner.state.config_file_name,
    }
    if context:
        body["context"] = context
        if only_preceding:
            body["preceding_only"] = True  # 服务端默认两边都给（界面在用），这边显式只要上文
    if offset:
        body["offset"] = offset
    if filename:
        body["filename"] = filename
    result = runner._http_post(f"/api/projects/{pid}/{source}/search", body)
    if isinstance(result, dict):
        match_keys = _OUTPUT_SEARCH_MATCH_KEYS if output else _INPUT_SEARCH_MATCH_KEYS
        _slim_search_results(result, field, context, match_keys)
        _apply_search_paging(result, offset)
        _apply_search_order(result, order)
        if output:
            result["source"] = "output"
    notes: list[str] = []
    # 解析不了的文件（插件/格式问题）被跳过了：明说，否则"这个文件里没有"和"这个文件没读"
    # 看起来一模一样。要诊断那个文件用 read_input_file。
    if isinstance(result, dict) and result.get("files_failed"):
        notes.append(
            f"这些{label}文件解析失败、没参与搜索：{'、'.join(str(n) for n in result['files_failed'])}"
            f"（文件插件/格式问题，用 {read_tool} 试读该文件可看到具体报错）；"
            "它们里面有没有命中是未知的。"
        )
    if isinstance(result, dict) and context:
        result["context"] = context  # 服务端已回；这里兜底，保证调用方一定看得到
        result["only_preceding"] = only_preceding
        scope = (
            f"每条命中前面 {context} 句（默认只给上文，要上下都给传 only_preceding=false）"
            if only_preceding
            else f"每条命中前后各 {context} 句"
        )
        notes.append(
            f"已带上下文：{scope}（包含关键词的那行是命中，index 带 * 的是搭着给的上下文行）；"
            f"带上下文时命中上限收紧为 {max_hits} 条、整页最多 {_SEARCH_ROW_BUDGET} 行，"
            "total 仍是全部命中数——命中很多时用 offset 翻页。"
        )
    # 指定了文件但 0 命中：确认一下该输入文件是否存在，避免模型误以为关键词不匹配
    if not output and isinstance(result, dict) and not result.get("total") and filename:
        try:
            listing = runner._http_get(f"/api/projects/{pid}/files")
            available = [
                str(f.get("name") or "")
                for f in listing.get("input_files", [])
                if isinstance(f, dict) and f.get("is_file", True)
            ]
            if filename not in available:
                notes.append(
                    f"输入文件 {filename} 不存在（检查 list_input_files 的文件名拼写）；"
                    "这是全项目搜索的 0 命中。"
                )
        except AgentToolError:
            pass
    if notes and isinstance(result, dict):
        existing = str(result.get("note") or "")
        result = {**result, "note": "；".join([part for part in [existing, *notes] if part])}
    return result
