"""字典类工具：列出、读取、保存与新建字典文件。"""

from __future__ import annotations

import urllib.parse
from typing import Any, TYPE_CHECKING

from GalTransl.Agent.models import AgentToolError
from GalTransl.Agent.tools.common import _diff_lines

if TYPE_CHECKING:
    from GalTransl.Agent.runner import AgentRunner


def _tool_list_dict_files(runner: AgentRunner, _args: dict[str, Any]) -> Any:
    """列出项目字典清单（文件 + 行数）。内容用 read_dict 单独读取，这里不回传全文。"""
    pid = runner._project_id()
    cfg = urllib.parse.quote(runner.state.config_file_name)
    data = runner._http_get(f"/api/projects/{pid}/dictionary/project?config={cfg}")
    contents = data.get("dict_contents", {})
    summary = {key: val.get("count", 0) for key, val in contents.items()}
    return {
        "pre_dict_files": data.get("pre_dict_files", []),
        "gpt_dict_files": data.get("gpt_dict_files", []),
        "post_dict_files": data.get("post_dict_files", []),
        "line_counts": summary,
    }


def _tool_read_dict(runner: AgentRunner, args: dict[str, Any]) -> Any:
    file_key = str(args.get("file_key", "")).strip()
    if not file_key:
        raise AgentToolError("file_key is required")
    pid = runner._project_id()
    cfg = urllib.parse.quote(runner.state.config_file_name)
    data = runner._http_get(f"/api/projects/{pid}/dictionary/project?config={cfg}")
    contents = data.get("dict_contents", {})
    if file_key not in contents:
        available = list(contents.keys())
        raise AgentToolError(f"file_key 不存在：{file_key}。可选：{available}")
    entry = contents[file_key]
    return {"file_key": file_key, "lines": entry.get("lines", []), "count": entry.get("count", 0)}


# 与 GalTransl/Dictionary.py 的解析口径一致：这两类别名的字典行，查找词不在第一列，
# 所以拼 key 时要按各自的位置取（条件字典: sp[2]；情景字典: sp[1]）。
_DICT_CONDITION_KEYS = frozenset({"pre_src", "post_src", "pre_dst", "post_dst", "pre_jp", "post_jp", "pre_zh", "post_zh"})
_DICT_SITUATION_KEYS = frozenset({"mono", "diag"})


def _dict_line_key(line: str, *, lenient: bool = False) -> str:
    """取字典行的匹配键（replace/append/delete 判断同一词条用）。

    普通行取第一列（原文）；条件字典/情景字典行取真正的查找词；空行与注释行
    （// 或 \\ 开头）返回 ""，表示不参与按键匹配。

    lenient=True 时，只有一列的行也按第一列当 key——delete 允许只写 key 而不
    复制整行；append/replace 用严格模式，避免把残缺行当成新词条写进字典。"""
    if not line.strip() or line.startswith("//") or line.startswith("\\\\"):
        return ""
    sp = line.replace("    ", "\t").split("\t")
    if len(sp) < 2:
        return sp[0].strip() if lenient else ""
    head = sp[0].strip()
    if head in _DICT_CONDITION_KEYS and len(sp) >= 4:
        return sp[2].strip()
    if head in _DICT_SITUATION_KEYS and len(sp) >= 3:
        return sp[1].strip()
    return head


def _split_dict_incoming(content: str) -> list[str]:
    """待写入内容切行：统一换行符，丢掉空行，保留注释行。"""
    text = str(content or "").replace("\r\n", "\n").replace("\r", "\n")
    return [line for line in text.split("\n") if line.strip()]


def _merge_dict_lines(before_lines: list[str], incoming: list[str], action: str) -> tuple[list[str], dict[str, Any]]:
    """按 action 把 incoming 合并到 before_lines，返回 (新行列表, 附加统计)。

    - append：新词条追加到末尾；key 已存在则跳过并记入 skipped_duplicate_keys。
    - replace：按 key 替换已有行；未匹配的 key 记入 not_found_keys，不新增（要新增用 append）。
    - delete：按 key 删除已有行；未匹配的 key 记入 not_found_keys。
    """
    if action == "overwrite":
        return list(incoming), {}

    if action == "delete":
        # incoming 是“要删除的词条”，允许只写 key（lenient）不复制整行
        wanted: list[str] = []
        for line in incoming:
            key = _dict_line_key(line, lenient=True)
            if key and key not in wanted:
                wanted.append(key)
        wanted_set = set(wanted)
        kept: list[str] = []
        deleted: list[str] = []
        for line in before_lines:
            key = _dict_line_key(line)
            if key and key in wanted_set:
                deleted.append(key)
                continue
            kept.append(line)
        extra: dict[str, Any] = {"deleted_keys": deleted}
        not_found = [k for k in wanted if k not in deleted]
        if not_found:
            extra["not_found_keys"] = not_found
        return kept, extra

    existing: dict[str, int] = {}
    for i, line in enumerate(before_lines):
        key = _dict_line_key(line)
        if key:
            existing.setdefault(key, i)

    if action == "append":
        new_lines = list(before_lines)
        appended: list[str] = []
        duplicates: list[str] = []
        for line in incoming:
            key = _dict_line_key(line)
            if not key:  # 注释/无键行：原样追加
                new_lines.append(line)
                continue
            if key in existing:
                if key not in duplicates:
                    duplicates.append(key)
                continue
            new_lines.append(line)
            existing[key] = len(new_lines) - 1
            appended.append(key)
        extra: dict[str, Any] = {"appended_keys": appended}
        if duplicates:
            extra["skipped_duplicate_keys"] = duplicates
        return new_lines, extra

    # replace
    new_lines = list(before_lines)
    replaced: list[str] = []
    not_found: list[str] = []
    for line in incoming:
        key = _dict_line_key(line)
        if not key:  # 注释/无键行在 replace 下忽略
            continue
        idx = existing.get(key)
        if idx is None:
            if key not in not_found:
                not_found.append(key)
            continue
        new_lines[idx] = line
        if key not in replaced:
            replaced.append(key)
    extra = {"replaced_keys": replaced}
    if not_found:
        extra["not_found_keys"] = not_found
    return new_lines, extra


def _dict_new_lines(before_lines: list[str], content: str, action: str) -> tuple[list[str], dict[str, Any]]:
    """按 action 算出写入后的整份字典行（**只算不写**），返回（新行, 附带的明细）。

    save_dict 与审批卡上的「将要变更」预览共用这一份合并规则：卡上给用户看的 diff 就是
    真执行会写下去的东西（append 跳过重复 key、replace 只改命中的 key 这些细节都一致）。
    """
    if action == "overwrite":
        return content.replace("\r\n", "\n").replace("\r", "\n").split("\n"), {}
    return _merge_dict_lines(before_lines, _split_dict_incoming(content), action)


def _tool_save_dict(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """写入项目字典。action 决定写入方式：

    - overwrite（默认）：整文件覆盖，等价于旧行为；
    - replace：按每行的 key 替换已有词条，未匹配的 key 不新增；
    - append：把行追加到末尾，key 已存在的行跳过；
    - delete：按 key 删除词条（content 传要删的词条，可整行粘贴或只写 key）。
    """
    file_key = str(args.get("file_key", "")).strip()
    if not file_key:
        raise AgentToolError("file_key is required")
    action = str(args.get("action", "") or "overwrite").strip().lower() or "overwrite"
    if action not in ("overwrite", "replace", "append", "delete"):
        raise AgentToolError("action must be one of: overwrite, replace, append, delete")
    content = str(args.get("content", ""))
    if action == "delete" and not _split_dict_incoming(content):
        raise AgentToolError("delete 需要 content（要删除的词条，每行一个 key）")
    pid = runner._project_id()

    # 先读旧内容（算行级 diff + 作为 append/replace 的基底），写完后随结果返回
    cfg = urllib.parse.quote(runner.state.config_file_name)
    before_lines: list[str] = []
    data = runner._http_get(f"/api/projects/{pid}/dictionary/project?config={cfg}")
    contents = data.get("dict_contents", {})
    old = contents.get(file_key)
    if isinstance(old, dict):
        before_lines = [str(x) for x in old.get("lines", [])]

    new_lines, extra = _dict_new_lines(before_lines, content, action)

    before_text = "\n".join(before_lines)
    new_text = "\n".join(new_lines)
    if new_text == before_text:
        return {"file_key": file_key, "action": action, "note": "内容没有变化，未写入", **extra}

    body = {
        "config_file_name": runner.state.config_file_name,
        "file_key": file_key,
        "content": new_text,
    }
    runner._http_post(f"/api/projects/{pid}/dictionary/project/save", body)
    diff = _diff_lines(before_text, new_text)
    added = sum(1 for r in diff["rows"] if r["op"] == "add")
    removed = sum(1 for r in diff["rows"] if r["op"] == "del")
    return {
        "file_key": file_key,
        "action": action,
        "line_count_before": len(before_lines),
        "line_count_after": len(new_lines),
        "lines_added": added,
        "lines_removed": removed,
        "line_diff": diff,
        **extra,
    }


def _tool_create_dict_file(runner: AgentRunner, args: dict[str, Any]) -> Any:
    category = str(args.get("category", "")).strip()
    filename = str(args.get("filename", "")).strip()
    if category not in ("pre", "gpt", "post"):
        raise AgentToolError("category must be one of: pre, gpt, post")
    if not filename:
        raise AgentToolError("filename is required")
    pid = runner._project_id()
    body = {"config_file_name": runner.state.config_file_name, "category": category, "filename": filename}
    return runner._http_post(f"/api/projects/{pid}/dictionary/project/create", body)
