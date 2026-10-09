"""工具结果的 Markdown 表格渲染（给模型看的输出）。"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from GalTransl.Search import SEARCH_ORDER_LABELS
from GalTransl.Agent.core import _log
from GalTransl.Agent.tools.common import _context_phrase


def _search_more_hint(result: dict[str, Any]) -> str:
    return {
        "random": "可再次随机采样",
        "even": "可换采样方式或缩小范围",
    }.get(result.get("order"), "用 offset 翻页")


def _tool_result_json(result: Any) -> str:
    """模型消息的 JSON fallback：只去掉结构空白，正文、字段和 Unicode 原样保留。"""
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


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


def _md_code_block(text: str) -> str:
    # 字典可以包含 Markdown 围栏；外层用更长的围栏，正文不转义、不改空白。
    fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", text)), default=0))
    return f"{fence}text\n{text}\n{fence}"


def _md_render_read_dict(result: dict[str, Any]) -> str | None:
    lines = result.get("lines")
    if not isinstance(lines, list):
        return None
    head = f"字典 {_md_cell(result.get('file_key'))}：共 {result.get('count', len(lines))} 行"
    return _md_doc(head, _md_code_block("\n".join(lines)) if lines else "字典为空。")


def _md_render_list_dict_files(result: dict[str, Any]) -> str | None:
    categories = (("pre", "pre_dict_files"), ("gpt", "gpt_dict_files"), ("post", "post_dict_files"))
    if not any(key in result for _, key in categories):
        return None
    counts = result.get("line_counts") or {}
    rows: list[dict[str, Any]] = []
    listed: set[str] = set()
    summary: list[str] = []
    for category, key in categories:
        files = result.get(key) or []
        summary.append(f"{category} {len(files)} 个")
        # 同类字典的加载顺序影响匹配，按原列表顺序展示。
        for file_key in files:
            rows.append({"category": category, "file_key": file_key, "lines": counts.get(file_key, "未知")})
            listed.add(file_key)
    for file_key, count in counts.items():
        if file_key not in listed:
            rows.append({"category": "未分类", "file_key": file_key, "lines": count})
    return _md_doc(
        "项目字典：" + "；".join(summary) + "（pre=译前，gpt=GPT，post=译后）",
        _md_table(["category", "file_key", "lines"], rows) if rows else "没有配置字典文件。",
    )


def _md_render_project_overview(result: dict[str, Any]) -> str | None:
    if not any(key in result for key in ("progress", "backend", "config", "config_field_descriptions")):
        return None
    parts: list[str] = []
    if "progress" in result:
        progress = result["progress"]
        parts.extend([
            "## progress · 翻译进度",
            _md_table(["field", "value"], [
                {"field": key, "value": _tool_result_json(value)}
                for key, value in progress.items() if key != "note"
            ]) if progress else "暂无进度数据。",
            str(progress.get("note") or ""),
        ])
    if "backend" in result:
        backend = result["backend"]
        parts.extend([
            "## backend · 实际生效的后端",
            _md_table(["role", "name", "type", "model"], [
                {"role": role, **backend[role]} for role in ("agent", "translator") if role in backend
            ]) if backend else "暂无后端信息。",
            str(backend.get("note") or ""),
        ])
    has_config = "config" in result
    has_descriptions = "config_field_descriptions" in result
    if has_config or has_descriptions:
        descriptions = result.get("config_field_descriptions") or {}
        rows: list[dict[str, Any]] = []
        described: set[str] = set()

        def append_row(key: str, value: str) -> None:
            rows.append({"key": key, "value": value, "description": descriptions.get(key, "")})
            described.add(key)

        def walk(config: dict[str, Any], prefix: str = "") -> None:
            for key, value in config.items():
                path = f"{prefix}.{key}" if prefix else key
                if isinstance(value, dict) and value:
                    if path in descriptions:
                        append_row(path, "（配置分组）")
                    walk(value, path)
                else:
                    # 字符串加引号，保留 null/false/0/空字符串的区别；列表整体保留次序。
                    append_row(path, _tool_result_json(value))

        if has_config:
            walk(result["config"])
        for key in descriptions:
            if key not in described:
                append_row(key, "")
        columns = ["key"] + (["value"] if has_config else []) + (["description"] if has_descriptions else [])
        title = "## config · 项目配置" if has_config else "## config_field_descriptions · 配置键说明"
        parts.extend([title, _md_table(columns, rows) if rows else "没有配置项。"])
    if result.get("note"):
        parts.append(f"备注：{result['note']}")
    return _md_doc(*parts)


# 每次工具调用共享的模型侧预览预算；原始结果仍供前端变更卡使用。
MODEL_DIFF_MAX_LINES = 10
MODEL_DIFF_CELL_CHARS = 500


def _diff_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        text = _tool_result_json(value)
    else:
        text = str(value) if value is not None else ""
    if len(text) > MODEL_DIFF_CELL_CHARS:
        text = text[:MODEL_DIFF_CELL_CHARS] + "…（内容已截断）"
    return text


def _diff_omission(shown: int, total: int, truncated: bool = False) -> str:
    if total <= shown and not truncated:
        return ""
    count = f"至少 {total}" if truncated else str(total)
    return f"变更预览共 {count} 行，本次展示 {shown} 行，其余已省略（每次调用最多 {MODEL_DIFF_MAX_LINES} 行，仅限制预览）。"


def _md_changes(changes: Any, limit: int = MODEL_DIFF_MAX_LINES) -> str:
    rows = [row for row in (changes or []) if isinstance(row, dict)]
    shown = rows[:limit]
    columns = ["path", "before", "after"]
    # 空译名也能新增/删除；两格都空时仍须说明操作类型。
    if any(row.get("kind") in ("add", "remove") for row in shown):
        columns.append("kind")
    table = _md_table(
        columns,
        [{key: _diff_value(row.get(key)) for key in columns} for row in shown],
    )
    return _md_doc(table, _diff_omission(len(shown), len(rows)))


def _md_line_diff(diff: dict[str, Any]) -> str:
    rows = [row for row in (diff.get("rows") or []) if isinstance(row, dict)]
    shown = rows[:MODEL_DIFF_MAX_LINES]
    lines = [
        ("+" if row.get("op") == "add" else "-")
        + _diff_value(row.get("line")).replace("\r", "\\r").replace("\n", "\\n")
        for row in shown
    ]
    total = diff.get("added", 0) + diff.get("removed", 0)
    known_total = isinstance(total, int) and total >= len(rows) and total > 0
    return _md_doc(
        "```diff\n" + "\n".join(lines) + "\n```" if lines else "没有行级变更。",
        _diff_omission(len(shown), total if known_total else len(rows), bool(diff.get("truncated")) and not known_total),
    )


_WRITE_FIELD_LABELS = {
    "success": "成功", "file_key": "字典", "filename": "文件", "source_file": "文件",
    "action": "操作", "mode": "模式", "created": "已新建", "category": "字典类别",
    "total": "总条数", "count": "当前条数", "updated": "已更新", "changed": "内容有变化",
    "line_count_before": "原行数", "line_count_after": "现行数",
    "lines_before": "原行数", "lines_after": "现行数", "lines_added": "新增行数", "lines_removed": "删除行数",
    "count_before": "原条数", "count_after": "现条数", "length": "字符数",
    "reason": "原因", "note": "备注", "error": "错误", "skipped": "跳过",
    "not_found_keys": "未找到的词条", "skipped_duplicate_keys": "跳过的重复词条",
    "already_present": "已存在", "not_found": "未找到", "missing_indexes": "未找到的 index",
    "not_found_files": "未找到的文件",
}
_WRITE_COUNT_LABELS = {
    "names_added": "新增人名", "names_removed": "删除人名", "appended_keys": "新增词条",
    "replaced_keys": "替换词条", "deleted_keys": "删除词条", "added": "新增", "removed": "删除",
    "deleted_indexes": "删除条目", "deleted_files": "删除文件",
}


def _md_render_write_result(result: dict[str, Any]) -> str:
    """写入结果只回统计、异常说明和有限 diff，避免重复回传整份文件/成功列表。"""
    parts = ["### 写入结果"]
    details: list[str] = []
    for key, value in result.items():
        if key in {"changes", "line_diff", "deleted_preview", "files", "content", "applied", "names", "filter_keys", "white_list"}:
            continue
        if key in _WRITE_COUNT_LABELS and isinstance(value, list):
            details.append(f"- {_WRITE_COUNT_LABELS[key]}：{len(value)}")
        elif isinstance(value, list):
            if value:
                # 未命中/跳过原因影响下一步操作，不能跟成功 diff 一起截掉。
                text = "、".join(_tool_result_json(item) if isinstance(item, dict) else str(item) for item in value)
                details.append(f"- {_WRITE_FIELD_LABELS.get(key, key)}：{_md_cell(text)}")
        elif value is not None:
            details.append(f"- {_WRITE_FIELD_LABELS.get(key, key)}：{_md_cell(value)}")
    parts.append("\n".join(details))
    if isinstance(result.get("line_diff"), dict):
        parts.append(_md_line_diff(result["line_diff"]))
    elif "changes" in result:
        changes = [row for row in (result.get("changes") or []) if isinstance(row, dict)]
        parts.append(f"共 {len(changes)} 项字段变更。")
        parts.append(_md_changes(changes))
    elif isinstance(result.get("deleted_preview"), list):
        rows = result["deleted_preview"]
        shown = rows[:MODEL_DIFF_MAX_LINES]
        parts.append(_md_table(["index", "text"], [
            {key: _diff_value(row.get(key)) for key in ("index", "text")} for row in shown
        ]))
        parts.append(_diff_omission(len(shown), len(result.get("deleted_indexes") or rows)))
    return _md_doc(*parts)


def _md_render_manage_problem_white_list(result: dict[str, Any]) -> str:
    if "white_list" not in result or any(key in result for key in ("changes", "added", "removed", "note")):
        return _md_render_write_result(result)
    entries = result.get("white_list") or []
    return _md_doc(f"共 {result.get('count', len(entries))} 条白名单", _md_table(["entry"], [{"entry": entry} for entry in entries]))


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
        head_parts.append(f"本页显示 {result.get('returned')} 条、还有更多（{_search_more_hint(result)}）")
    else:
        head_parts.append(f"本页显示 {result.get('returned')} 条")
    if result.get("order"):
        head_parts.append(f"排序：{SEARCH_ORDER_LABELS.get(result['order'], result['order'])}")
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


def _md_render_entries(result: dict[str, Any], columns: list[str]) -> str:
    head_parts: list[str] = []
    if result.get("filename") is not None:
        head_parts.append(f"文件 {result['filename']}")
    if result.get("count") is not None:
        head_parts.append(f"共 {result['count']} 条，显示 {result.get('returned')} 条")
    missing = result.get("missing_indexes")
    missing_text = (
        "缺失 index：" + ",".join(str(i) for i in missing) if isinstance(missing, list) and missing else ""
    )
    table = _md_table(columns, result.get("entries"))
    return _md_doc("，".join(head_parts), missing_text, table)


def _md_render_read_input_file(result: dict[str, Any]) -> str:
    return _md_render_entries(result, ["index", "name", "pre_src"])


def _md_render_read_output(result: dict[str, Any]) -> str | None:
    if not isinstance(result.get("entries"), list):
        return None
    return _md_render_entries(result, ["index", "name", "message"])


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
    if result.get("order"):
        head_parts.append(f"排序：{SEARCH_ORDER_LABELS.get(result['order'], result['order'])}")
    if result.get("note"):
        notes.append(f"备注：{result['note']}")
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
    if result.get("order"):
        parts.append(f"排序：{SEARCH_ORDER_LABELS.get(result['order'], result['order'])}")
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
            page += f"、还有更多（{_search_more_hint(result)}）"
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

    add/remove 的结果走写入摘要与有限 diff。
    """
    filters = result.get("filters")
    if not isinstance(filters, list):
        return _md_render_write_result(result)
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
    comments_total = sum(row.get("comment_count", len(row.get("proofread_comment") or [])) for row in tasks)
    head = f"共派出 {len(tasks)} 个子代理（{status_text}），合计 {comments_total} 条校对批注"
    if any("modified_count" in row for row in tasks):
        head += f"，直接修改 {sum(row.get('modified_count', 0) for row in tasks)} 条译文"
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
        if "modified_count" in row:
            meta.extend([
                f"读取 {row.get('read_count', 0)} 条（不等于已校对数）",
                f"修改 {row.get('modified_count', 0)} 条",
                f"需二次审查 {row.get('needs_review_count', 0)} 条",
                f"未验证 {row.get('unverified_count', 0)} 条",
                f"仍有问题 {row.get('remaining_problem_count', 0)} 条",
                f"提交失败 {row.get('failed_file_count', 0)} 个文件",
                f"修改记录 task_id={row.get('change_task_id', row.get('id', ''))}",
            ])
        parts.append(" ｜ ".join(str(item) for item in meta))
        review = row.get("needs_review")
        if review:
            parts.append("需二次审查的译文（包括已修改但仍待确认的条目）：\n\n" + _md_table(["file", "index", "reason"], review))
        if row.get("review_truncated"):
            parts.append('这里只展示前 20 条；用 read_proofread_changes(task_id="' + str(row.get("change_task_id", "")) + '", view="review") 分页查看完整清单。')
        if row.get("review_record_error"):
            parts.append("二次审查清单保存失败：" + str(row["review_record_error"]))
        comments = row.get("proofread_comment")
        if isinstance(comments, list) and comments:
            table = _md_table(["file", "index"], comments)
            parts.append(
                f"校对批注位置 {len(comments)} 条（全文在对应缓存的 proofread_comment 里）："
                f"\n\n{table}"
            )
        if row.get("comments_truncated"):
            parts.append(f"共有 {row.get('comment_count')} 条待裁决批注，只展示前 20 条；请按文件用 read_transl_cache(grep=['proofread_comment']) 分段查看。")
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
    remaining = MODEL_DIFF_MAX_LINES
    for index, row in enumerate(files, start=1):
        parts.append(f"## {index}. {row.get('filename')}（改 {int(row.get('updated') or 0)} 条）")
        if row.get("error"):
            parts.append(f"⚠ {row['error']}")
        if row.get("note"):
            parts.append(str(row["note"]))
        if row.get("verification") == "unknown":
            parts.append("检测状态：未验证，不能据此判断问题已消除。")
        if row.get("audit_error"):
            parts.append("修改已提交，但修改记录的完成状态写入失败；pending 记录保留修改前后内容。")
        block: list[str] = []
        changes = [c for c in (row.get("changes") or []) if isinstance(c, dict)]
        table = _md_changes(changes, remaining)
        remaining -= min(remaining, len(changes))
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


def _md_render_get_name_table(result: dict[str, Any]) -> str | None:
    """人名表 → 一篇 Markdown：表头写来源与条数，正文是 src/dst 表格，末尾点出仍缺译名的。

    dst_name_source 只在真有行带它时才加进表头（开着 useGPTDictInName 且确实补上过才有），
    否则平白多一列空单元格。表为空（还没生成）时不渲染，交给 JSON 原样返回——模型照旧
    能看出"表是空的、该去 dump-name"。
    """
    names = result.get("names")
    if not isinstance(names, list) or not names:
        return None
    head = f"共 {len(names)} 条人名"
    if result.get("source_file"):
        head = f"{result['source_file']}：{head}"
    columns = ["src_name", "dst_name", "count"]
    if any(isinstance(row, dict) and row.get("dst_name_source") for row in names):
        columns.append("dst_name_source")
    parts = [head, _md_table(columns, names)]
    still_empty = result.get("still_empty")
    if isinstance(still_empty, list) and still_empty:
        parts.append(
            f"表与字典都没有译名、需要补的有 {len(still_empty)} 个："
            + "、".join(str(src) for src in still_empty)
        )
    note = result.get("note")
    if note:
        parts.append(f"备注：{note}")
    return _md_doc(*parts)


# 工具名 → 渲染器。渲染只对这里列出的工具生效，其余工具维持 JSON。
_MD_RENDERERS: dict[str, Any] = {
    "get_project_overview": _md_render_project_overview,
    "list_dict_files": _md_render_list_dict_files,
    "read_dict": _md_render_read_dict,
    "read_output": _md_render_read_output,
    "list_input_files": _md_render_list_input_files,
    "list_problems": _md_render_list_problems,
    "read_input_file": _md_render_read_input_file,
    "read_transl_cache": _md_render_transl_cache,
    "search_input": _md_render_search_input,
    "manage_problem_filter": _md_render_manage_problem_filter,
    "run_subagents": _md_render_run_subagents,
    "patch_transl_cache": _md_render_patch_transl_cache,
    "get_name_table": _md_render_get_name_table,
    "save_dict": _md_render_write_result,
    "save_name_table": _md_render_write_result,
    "write_project_guideline": _md_render_write_result,
    "update_project_config": _md_render_write_result,
    "manage_problem_white_list": _md_render_manage_problem_white_list,
    "delete_transl_cache": _md_render_write_result,
}


def _render_tool_result_table(name: str, result: Any) -> str | None:
    """把清单与写入工具的结果渲染成 Markdown；写入 diff 使用模型侧预览上限。

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
