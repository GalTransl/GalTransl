"""输入文件类工具：列出与读取待翻译原文。"""

from __future__ import annotations

import urllib.parse
from typing import Any, TYPE_CHECKING

from GalTransl.Agent.models import AgentToolError
from GalTransl.Agent.tools.common import _read_entries_page
from GalTransl.Agent.tools.listing import (
    LIST_ITEMS_DEFAULT_LIMIT,
    LIST_ORDER_DEFAULT,
    _grep_items,
    _list_grep,
    _list_limit,
    _list_notes,
    _list_order,
    _list_offset,
    _list_paging,
    _select_list_items,
)

if TYPE_CHECKING:
    from GalTransl.Agent.runner import AgentRunner


def _list_input_payload(
    runner: AgentRunner,
    *,
    grep: str = "",
    limit: int = LIST_ITEMS_DEFAULT_LIMIT,
    order: str = LIST_ORDER_DEFAULT,
    offset: int = 0,
    names: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """列输入文件（grep / limit / order 都在这一层做），供工具与子代理的锁定包装共用。

    names 只给子代理的"文件锁定"用：**先按锁定名单过滤、再按 order 截取**，顺序不能反——
    反过来的话采样可能把属于它的文件摇掉，看起来就像"这些文件不在清单里"。
    """
    pid = runner._project_id()
    cfg = urllib.parse.quote(runner.state.config_file_name or "config.yaml")
    files = runner._http_get(f"/api/projects/{pid}/files?counts=1&config={cfg}")
    input_files: list[dict[str, Any]] = []
    for item in files.get("input_files", []):
        if not isinstance(item, dict) or not item.get("is_file", True):
            continue
        parsed = item.get("sentences")
        input_files.append({
            "name": str(item.get("name") or ""),
            "size": item.get("size", 0),
            # 解析失败给 null，不要编造成 0：0 会被读成"这个文件不用翻"
            "sentences": parsed if isinstance(parsed, int) else None,
        })

    matched = _grep_items(input_files, grep)
    if names is not None:
        locked = set(names)
        matched = [f for f in matched if f["name"] in locked]
    shown = _select_list_items(matched, limit, order, offset)
    notes = [
        "sentences 是输入文件解析出的条数，只用来估工作量：文本插件（如「跳过无日文句」）"
        "还没跑，真正要翻的句数通常比它少，所以按它估总时长会略偏大；null 表示解析失败。"
        "**它不是进度**——某文件缓存里有多少条与这个数无关，"
        "整体翻没翻完看 get_project_overview 的 files_translated/files_total。"
    ]
    notes.extend(
        _list_notes(
            matched=len(matched),
            grep=grep,
            returned=len(shown),
            limit=limit,
            order=order,
            unit="输入文件",
            offset=offset,
        )
    )
    return {
        "input_files": shown,
        "count": len(matched),
        "returned": len(shown),
        **_list_paging(len(matched), len(shown), offset, order),
        "sampled": len(shown) < len(matched),
        "sentences_total": sum(
            f["sentences"] for f in matched if isinstance(f["sentences"], int)
        ),
        "note": "；".join(notes),
    }


def _tool_list_input_files(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """列出待翻译文件（原文件，输入目录），带每个文件**解析出的条数**。

    口径一律是输入文件本身：`/files?counts=1` 让后端用文件插件解析原文数一遍。

    以前这里对"已有缓存的文件"改报**缓存条数**（理由是它与进度/ETA 同口径、更准），
    实际是个陷阱：缓存条数只说明缓存里存了多少条，一旦和别处的数字相等，读的人就会
    以为整个文件翻完了。句数是拿来看工作量的，不该兼职当进度——进度去看
    get_project_overview 的 files_translated / progress，那里才有真正翻到哪了。

    注意口径：这是文件插件解析出的**原始条目数**，文本插件（如「跳过无日文句」）
    还没跑，因此通常**大于**最终会送去翻译的句数（估工作量偏大是已知的取舍）。

    清单支持 grep（文件名子串）、limit（默认 100）与 order（怎么挑这 100 个：均匀采样 /
    文件名顺序 / 随机采样 / 按大小从大到小 / 从小到大，见 LIST_ORDER_MODES）。
    sentences_total 是**过滤后整份清单**的合计（含被截取省略的那些文件）：它是工作量估计，
    不能因为少显示了几行就变小。
    """
    return _list_input_payload(
        runner, grep=_list_grep(args), limit=_list_limit(args), order=_list_order(args), offset=_list_offset(args)
    )


def _tool_read_input_file(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """读取待翻译原文。filename 来自 list_input_files；index 统一为 1-based，支持区间
    （"1-100"、读取文件头几十句足够了解文风）。"""
    filename = str(args.get("filename", "")).strip()
    if not filename:
        raise AgentToolError("filename is required")
    pid = runner._project_id()
    cfg = urllib.parse.quote(runner.state.config_file_name)
    data = runner._http_get(f"/api/projects/{pid}/input/{urllib.parse.quote(filename)}?config={cfg}")
    entries = data.get("entries", [])
    return {"filename": filename, "count": len(entries), **_read_entries_page(entries, args)}
