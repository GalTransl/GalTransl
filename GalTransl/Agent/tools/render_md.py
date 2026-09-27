"""工具结果的 Markdown 表格渲染（给模型看的输出）。"""

from __future__ import annotations

from collections import Counter
from typing import Any

from GalTransl.Agent.core import _log
from GalTransl.Agent.tools.common import _context_phrase


# ---- Markdown 表格输出（当前启用）----
# 可表格化的部分（数据行）用 Markdown 表格：第一行列名、第二行分隔线、每行一条——模型
# 最熟的形状；计数 / 提示 / 警告这些杂项不用硬塞进表格，按文字写在表格前后。
# 单元格规则只有一条：竖线转义、换行折成 <br>（Markdown 表格单元格不能有真换行），
# None/缺失就是空单元格——不再需要 ION 那套 ~ 与位置对齐。
def _md_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    s = str(value)
    return (
        s.replace("|", "\\|")
        .replace("\r\n", "<br>")
        .replace("\n", "<br>")
        .replace("\r", "<br>")
    )


def _md_table(columns: list[str], rows: Any) -> str:
    """Markdown 表格；没有数据行就返回空串（调用方拼文档时自动略过）。"""
    if not isinstance(rows, list) or not rows:
        return ""
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join(["---"] * len(columns)) + " |",
    ]
    for row in rows:
        row = row if isinstance(row, dict) else {}
        lines.append("| " + " | ".join(_md_cell(row.get(col)) for col in columns) + " |")
    return "\n".join(lines)


def _md_doc(*parts: str) -> str:
    return "\n\n".join(part for part in parts if part)


def _md_render_list_transl_cache(result: dict[str, Any]) -> str:
    head_parts: list[str] = []
    if result.get("count") is not None:
        head_parts.append(f"共 {result['count']} 个缓存文件")
    if result.get("sampled"):
        head_parts.append(f"下面只列其中 {result.get('returned')} 个（均匀采样，不是前几名）")
    table = _md_table(["name", "size", "entries"], result.get("cache_files"))
    note = result.get("note")
    return _md_doc("，".join(head_parts), table, f"备注：{note}" if note else "")


def _md_render_list_input_files(result: dict[str, Any]) -> str:
    head_parts: list[str] = []
    if result.get("count") is not None:
        head_parts.append(f"共 {result['count']} 个输入文件")
    if result.get("sampled"):
        head_parts.append(f"下面只列其中 {result.get('returned')} 个（均匀采样，不是前几名）")
    if result.get("sentences_total") is not None:
        head_parts.append(f"句数合计 {result['sentences_total']}（含没列出来的文件）")
    table = _md_table(["name", "size", "sentences"], result.get("input_files"))
    note = result.get("note")
    return _md_doc("，".join(head_parts), table, f"备注：{note}" if note else "")


def _md_render_list_problems(result: dict[str, Any]) -> str:
    note = result.get("note")
    note_line = f"备注：{note}" if note else ""
    if result.get("mode") == "stats":
        head = f"共 {result.get('total')} 个问题，类型统计如下"
        table = _md_table(["type", "count"], result.get("types"))
        hint = result.get("hint")
        return _md_doc(head, table, f"提示：{hint}" if hint else "", note_line)

    head_parts: list[str] = []
    if result.get("problem_type") is not None:
        head_parts.append(f"问题类型：{result['problem_type']}")
    if result.get("matched") is not None:
        head_parts.append(f"命中 {result['matched']} 条")
    if result.get("has_more"):
        head_parts.append(f"本页显示 {result.get('returned')} 条、还有更多（用 offset 翻页）")
    else:
        head_parts.append(f"本页显示 {result.get('returned')} 条")
    if result.get("majority_trans_by"):
        head_parts.append(
            f"多数派模型 {result['majority_trans_by']}（表里已省略，只留少数派/改过的来源）"
        )
    context_phrase = _context_phrase(result, "每条问题")
    if context_phrase:
        head_parts.append(context_phrase)
    table = _md_table(
        ["filename", "index", "speaker", "post_src", "pre_dst", "problem", "trans_by"],
        result.get("problems"),
    )
    return _md_doc("；".join(head_parts), table, note_line)


def _md_render_read_input_file(result: dict[str, Any]) -> str:
    head_parts: list[str] = []
    if result.get("filename") is not None:
        head_parts.append(f"文件 {result['filename']}")
    if result.get("count") is not None:
        head_parts.append(f"共 {result['count']} 条，显示 {result.get('returned')} 条")
    missing = result.get("missing_indexes")
    missing_text = (
        "缺失 index：" + ",".join(str(i) for i in missing) if isinstance(missing, list) and missing else ""
    )
    table = _md_table(["index", "name", "pre_src"], result.get("entries"))
    return _md_doc("，".join(head_parts), missing_text, table)


def _md_render_read_transl_cache(result: dict[str, Any]) -> str:
    head_parts: list[str] = []
    if result.get("filename") is not None:
        head_parts.append(f"文件 {result['filename']}")
    if result.get("grep_note"):
        head_parts.append(str(result["grep_note"]))
    if result.get("count") is not None:
        head_parts.append(f"共 {result['count']} 条，显示 {result.get('returned')} 条")
    context_phrase = _context_phrase(result, "点名条目")
    if context_phrase:
        head_parts.append(context_phrase)
    if result.get("majority_trans_by"):
        head_parts.append(
            f"多数派模型 {result['majority_trans_by']}（表里已省略，只留少数派/改过的来源）"
        )
    fields_note = result.get("fields_note")
    missing = result.get("missing_indexes")
    missing_text = (
        "缺失 index：" + ",".join(str(i) for i in missing) if isinstance(missing, list) and missing else ""
    )
    fields = [str(f) for f in result.get("fields") or []]
    if not fields:
        entries = result.get("entries")
        if isinstance(entries, list) and entries and isinstance(entries[0], dict):
            fields = list(entries[0].keys())
    table = _md_table(fields, result.get("entries"))
    notes = [f"字段说明：{fields_note}"] if fields_note else []
    return _md_doc("，".join(head_parts), missing_text, *notes, table)


# 搜索类工具（/cache/search 与 /input/search）的返回结构是同一套：results + total，
# 带 context 时多 returned_hits / returned。表头那些文字（命中数、上下文、命中分布、多数派、
# 解析失败的文件）只有一处口径；行本身各出各的表——两侧的列不一样。
_SEARCH_CACHE_COLUMNS = ["filename", "index", "speaker", "post_src", "pre_dst", "problem", "trans_by"]
_SEARCH_INPUT_COLUMNS = ["filename", "index", "speaker", "src"]


def _md_search_head(result: dict[str, Any]) -> str:
    parts: list[str] = []
    total = result.get("total")
    if total is not None:
        parts.append(f"共 {total} 条命中")
    context_phrase = _context_phrase(result, "每条命中")
    if context_phrase:
        parts.append(context_phrase)
    # 分页口径与 list_problems 一致：returned 是本页命中数，returned_rows 才含前后文行
    shown = result.get("returned")
    if isinstance(shown, int):
        page = f"本页 {shown} 条命中"
        rows = result.get("returned_rows")
        if isinstance(rows, int):
            page += f"、含前后文共 {rows} 行"
        if result.get("offset"):
            page += f"（offset={result['offset']}）"
        if result.get("has_more"):
            page += "、还有更多（用 offset 翻页）"
        parts.append(page)
    matched_in = result.get("matched_in")
    if isinstance(matched_in, dict) and matched_in:
        parts.append("命中分布：" + "、".join(f"{key} {value}" for key, value in matched_in.items()))
    if result.get("majority_trans_by"):
        parts.append(f"多数派模型 {result['majority_trans_by']}（表里已省略，只留少数派/改过的来源）")
    failed = result.get("files_failed")
    if isinstance(failed, list) and failed:
        parts.append("这些输入文件解析失败、没参与搜索：" + "、".join(str(name) for name in failed))
    return "；".join(parts)


def _md_render_search(result: dict[str, Any], columns: list[str]) -> str | None:
    """搜索类工具共用的渲染：表头文字 + 命中行表格 + 备注。

    一行都没有（0 命中这类）时表头文字还在，不会渲染出一段空内容把结果弄丢。
    """
    note = result.get("note")
    return _md_doc(
        _md_search_head(result),
        _md_table(columns, result.get("results")),
        f"备注：{note}" if note else "",
    ) or None


def _md_render_search_transl_cache(result: dict[str, Any]) -> str | None:
    """在缓存里搜（原文/译文/问题）：命中行与 read_transl_cache 是同一批列。"""
    return _md_render_search(result, _SEARCH_CACHE_COLUMNS)


def _md_render_search_input(result: dict[str, Any]) -> str | None:
    """在待翻译原文里搜：原文侧只有正文与说话人，另加定位用的 filename / index。"""
    return _md_render_search(result, _SEARCH_INPUT_COLUMNS)


def _md_render_manage_problem_filter(result: dict[str, Any]) -> str | None:
    """list 的过滤清单渲染成表：每条过滤项当前挡住了多少条问题。

    add/remove 的结果是变更 diff（changes），没有可表格化的清单——返回 None 让它照旧走 JSON。
    """
    filters = result.get("filters")
    if not isinstance(filters, list) or not filters:
        return None
    parts = [
        f"共 {result.get('count')} 条过滤项",
        _md_table(["key", "problems"], filters),
    ]
    entries = result.get("problem_entries")
    visible = result.get("visible_entries")
    if isinstance(entries, int) and isinstance(visible, int):
        parts.append(
            f"当前共 {entries} 条问题，其中 {entries - visible} 条被过滤项挡住，"
            f"list_problems 可见 {visible} 条"
        )
    if any(isinstance(f, dict) and not f.get("problems") for f in filters):
        parts.append("problems 为 0 的过滤项当前一条也挡不到，可考虑 remove")
    return _md_doc(*parts)


def _md_render_run_subagents(result: dict[str, Any]) -> str:
    """子代理批次结果 → 一篇 Markdown：开头顶部统计，随后每个子代理一个小节。

    JSON 版很长（十几份报告 + 每份的批注清单），而批注清单就是「文件 × index」的规整数据，
    用表格最省、也最好认；报告本身是子代理写的 Markdown，原样贴。
    """
    tasks = [row for row in (result.get("tasks") or []) if isinstance(row, dict)]
    if not tasks:
        return ""
    status_labels = {"done": "完成", "failed": "失败", "stopped": "中止"}
    counts = Counter(str(row.get("status") or "") for row in tasks)
    status_text = "、".join(
        f"{status_labels.get(status, status or '未知')} {count}"
        for status, count in counts.items()
        if count
    )
    comments_total = sum(len(row.get("proofread_comment") or []) for row in tasks)
    head = f"共派出 {len(tasks)} 个子代理（{status_text}），合计 {comments_total} 条校对批注"
    skipped = result.get("skipped")
    if skipped:
        head += f"；另有 {skipped} 个任务因文件不够分被跳过"
    parts: list[str] = [head + "。"]
    note = result.get("note")
    if note:
        parts.append(str(note))
    for i, row in enumerate(tasks, start=1):
        label = str(row.get("label") or row.get("agent") or "子代理")
        name = str(row.get("file") or "、".join(row.get("files") or []) or "（不锁定文件）")
        parts.append(f"## {i}. {label} · {name}")
        meta = [f"状态 {status_labels.get(str(row.get('status') or ''), row.get('status'))}"]
        if row.get("indexes"):
            meta.append(f"区间 {row['indexes']}")
        if row.get("turns") is not None:
            meta.append(f"轮数 {row['turns']}")
        if row.get("tool_calls") is not None:
            meta.append(f"工具调用 {row['tool_calls']} 次")
        if row.get("duration_ms") is not None:
            meta.append(f"耗时 {int(row['duration_ms']) / 1000:.1f}s")
        if row.get("error"):
            meta.append(f"错误：{row['error']}")
        parts.append(" ｜ ".join(str(item) for item in meta))
        comments = row.get("proofread_comment")
        if isinstance(comments, list) and comments:
            table = _md_table(["file", "index"], comments)
            parts.append(
                f"写下的校对批注 {len(comments)} 条（全文在对应缓存的 proofread_comment 里）："
                f"\n\n{table}"
            )
        report = str(row.get("report") or "").strip()
        if report:
            parts.append(report)
    return _md_doc(*parts)


def _md_render_patch_transl_cache(result: dict[str, Any]) -> str:
    """改缓存的结果 → 一篇 Markdown：开头顶部一句总计，随后每个文件一个小节。

    按文件分节是因为一次调用可以跨多个文件（统一译名/术语这类活）：每节先列「改了什么」
    （before→after 表格），再列没落地的条目与"改完仍存在的问题"——剩下的问题当场可验，
    不必再 read_transl_cache 兜一圈。
    """
    files = [row for row in (result.get("files") or []) if isinstance(row, dict)]
    if not files:
        return ""
    total = sum(int(row.get("updated") or 0) for row in files)
    failed = [row for row in files if row.get("error")]
    head = f"共改动 {total} 条，涉及 {len(files)} 个缓存文件"
    if failed:
        head += f"；其中 {len(failed)} 个文件没有改动"
    parts: list[str] = [head + "。"]
    if result.get("trans_by"):
        parts.append(
            f"被改条目的 trans_by 已标成 {result['trans_by']}（与翻译引擎翻的区分开）。"
        )
    for index, row in enumerate(files, start=1):
        parts.append(f"## {index}. {row.get('filename')}（改 {int(row.get('updated') or 0)} 条）")
        if row.get("error"):
            parts.append(f"⚠ {row['error']}")
        block: list[str] = []
        table = _md_table(
            ["path", "before", "after"],
            [c for c in (row.get("changes") or []) if isinstance(c, dict)],
        )
        if table:
            block.append(table)
        not_found = [n for n in (row.get("not_found") or []) if isinstance(n, dict)]
        if not_found:
            block.append("没找到这些 index：" + "、".join(str(n.get("index")) for n in not_found))
        skipped = [s for s in (row.get("skipped") or []) if isinstance(s, dict)]
        if skipped:
            block.append(
                "跳过这些条目：\n"
                + "\n".join(f"- #{s.get('index')}：{s.get('reason')}" for s in skipped)
            )
        problems = [p for p in (row.get("problems") or []) if isinstance(p, dict)]
        if problems:
            block.append(
                f"改完仍存在的问题 {len(problems)} 条（没列出来的就是已经消掉了）：\n"
                + "\n".join(f"- #{p.get('index')}：{p.get('problem')}" for p in problems)
            )
        if block:
            parts.append("\n\n".join(block))
    return _md_doc(*parts)


def _md_render_transl_cache(result: dict[str, Any]) -> str | None:
    """read_transl_cache 的三种 action 各用各的表：看结果里的 action；没有（直接拿内部函数的
    结果来渲染）就按形状认——列文件有 cache_files，搜索有 results，其余是读条目。"""
    action = result.get("action")
    if action is None:
        action = "list" if "cache_files" in result else "search" if "results" in result else "read"
    if action == "list":
        return _md_render_list_transl_cache(result)
    if action == "search":
        return _md_render_search_transl_cache(result)
    return _md_render_read_transl_cache(result)


# 工具名 → 渲染器。渲染只对这里列出的工具生效，其余工具维持 JSON。
_MD_RENDERERS: dict[str, Any] = {
    "list_input_files": _md_render_list_input_files,
    "list_problems": _md_render_list_problems,
    "read_input_file": _md_render_read_input_file,
    "read_transl_cache": _md_render_transl_cache,
    "search_input": _md_render_search_input,
    "manage_problem_filter": _md_render_manage_problem_filter,
    "run_subagents": _md_render_run_subagents,
    "patch_transl_cache": _md_render_patch_transl_cache,
}


def _render_tool_result_table(name: str, result: Any) -> str | None:
    """把大清单工具的结果 dict 渲染成 Markdown（表格化数据 + 文字化杂项）。

    不在名单 / 不是 dict / 渲染失败时返回 None——调用方退回 JSON，绝不能因为格式丢数据。
    """
    render = _MD_RENDERERS.get(name)
    if render is None or not isinstance(result, dict):
        return None
    try:
        return render(result)
    except Exception as exc:  # noqa: BLE001
        _log(f"  ⚠ Markdown 渲染失败（{name}），退回 JSON：{exc}")
        return None
