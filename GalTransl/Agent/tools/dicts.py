"""字典类工具：列出、读取与保存字典文件（save_dict 带 category 时顺带新建）。"""

from __future__ import annotations

import urllib.parse
from typing import Any, TYPE_CHECKING

from GalTransl.Agent.models import AgentToolError
from GalTransl.Agent.tools.common import _diff_lines
from GalTransl.Dictionary import CNormalDic, split_dictionary_line

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
    query = args.get("query", "")
    if not isinstance(query, str):
        raise AgentToolError("query must be a string")
    offset = args.get("offset", 0)
    limit = args.get("limit", 100)
    if type(offset) is not int or offset < 0:
        raise AgentToolError("offset must be a non-negative integer")
    if type(limit) is not int or not 1 <= limit <= 500:
        raise AgentToolError("limit must be an integer between 1 and 500")
    pid = runner._project_id()
    cfg = urllib.parse.quote(runner.state.config_file_name)
    data = runner._http_get(f"/api/projects/{pid}/dictionary/project?config={cfg}")
    contents = data.get("dict_contents", {})
    if file_key not in contents:
        available = list(contents.keys())
        raise AgentToolError(f"file_key 不存在：{file_key}。可选：{available}")
    entry = contents[file_key]
    lines = entry.get("lines", [])
    needle = query.casefold()
    matches = [(number, line) for number, line in enumerate(lines, start=1) if needle in line.casefold()]
    page = matches[offset:offset + limit]
    has_more = offset + len(page) < len(matches)
    result = {
        "file_key": file_key,
        "lines": [line for _, line in page],
        "line_numbers": [number for number, _ in page],
        "count": len(lines),
        "matched": len(matches),
        "returned": len(page),
        "query": query,
        "offset": offset,
        "limit": limit,
        "has_more": has_more,
    }
    if has_more:
        result["next_offset"] = offset + len(page)
    return result


# 与 GalTransl/Dictionary.py 的解析口径一致：条件/情景字典的身份包含作用范围与查找词，
# 同一个查找词在不同条件、独白和对话中的规则不能互相覆盖。
_DICT_CONDITION_KEYS = frozenset(CNormalDic.conditionaDic_key)
_DICT_SITUATION_KEYS = frozenset(CNormalDic.situationsDic_key)
_DICT_KEY_ALIASES = {"pre_jp": "pre_src", "post_jp": "post_src", "pre_zh": "pre_dst", "post_zh": "post_dst"}


def _dict_line_key(line: str, *, lenient: bool = False, category: str = "") -> str:
    """取字典行的匹配键（patch/delete 判断同一词条用）。

    普通行取第一列（原文）；条件/情景行包含条件类型、条件和查找词；空行与注释行
    （// 或 \\ 开头）返回 ""，表示不参与按键匹配。

    lenient=True 时，只有一列的行也按第一列当 key——delete 允许只写 key 而不
    复制整行；patch 用严格模式，残缺行不参与按键匹配。"""
    if not line.strip() or line.lstrip().startswith(("//", "\\\\")):
        return ""
    gpt = category == "gpt" or (not category and "->" in line)
    sp = split_dictionary_line(line, gpt=gpt)
    if len(sp) < 2:
        return sp[0] if lenient else ""
    head = sp[0]
    if not gpt and head in _DICT_CONDITION_KEYS:
        return "\t".join([_DICT_KEY_ALIASES.get(head, head), *sp[1:3]]) if len(sp) >= (3 if lenient else 4) else ""
    if not gpt and head in _DICT_SITUATION_KEYS:
        return "\t".join(sp[:2]) if len(sp) >= (2 if lenient else 3) else ""
    return head


def _split_dict_incoming(content: str) -> list[str]:
    """待写入内容切行：统一换行符，丢掉空行，保留注释行。"""
    text = str(content or "").replace("\r\n", "\n").replace("\r", "\n")
    return [line for line in text.split("\n") if line.strip()]


def _merge_dict_lines(before_lines: list[str], incoming: list[str], action: str, *, category: str = "") -> tuple[list[str], dict[str, Any]]:
    """按 action 把 incoming 合并到 before_lines，返回 (新行列表, 附加统计)。

    - patch：按 key 更新已有行（译名与备注），新词条追加到末尾；同批重复 key 以最后一行为准。
    - delete：按 key 删除已有行；未匹配的 key 记入 not_found_keys。
    """
    if action == "overwrite":
        return list(incoming), {}

    if action == "delete":
        # incoming 是“要删除的词条”，允许只写 key（lenient）不复制整行
        wanted: list[str] = []
        for line in incoming:
            key = _dict_line_key(line, lenient=True, category=category)
            if key and key not in wanted:
                wanted.append(key)
        wanted_set = set(wanted)
        kept: list[str] = []
        deleted: list[str] = []
        for line in before_lines:
            key = _dict_line_key(line, category=category)
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
        key = _dict_line_key(line, category=category)
        if key:
            existing.setdefault(key, i)

    # patch：保留已有词条的位置，新词条按首次出现的顺序追加。
    new_lines = list(before_lines)
    appended: list[str] = []
    replaced: list[str] = []
    for line in incoming:
        key = _dict_line_key(line, category=category)
        if not key:  # 注释/无键行：原样追加
            new_lines.append(line)
            continue
        idx = existing.get(key)
        if idx is None:
            existing[key] = len(new_lines)
            new_lines.append(line)
            appended.append(key)
            continue
        new_lines[idx] = line
        if idx < len(before_lines) and key not in replaced:
            replaced.append(key)
    replaced = [key for key in replaced if new_lines[existing[key]] != before_lines[existing[key]]]
    return new_lines, {"appended_keys": appended, "replaced_keys": replaced}


def _dict_new_lines(before_lines: list[str], content: str, action: str, *, category: str = "") -> tuple[list[str], dict[str, Any]]:
    """按 action 算出写入后的整份字典行（**只算不写**），返回（新行, 附带的明细）。

    save_dict 与审批卡上的「将要变更」预览共用这一份合并规则：卡上给用户看的 diff 就是
    真执行会写下去的东西（patch 更新已有词条、追加新词条这些细节都一致）。
    """
    if action == "overwrite":
        return content.replace("\r\n", "\n").replace("\r", "\n").split("\n"), {}
    return _merge_dict_lines(before_lines, _split_dict_incoming(content), action, category=category)


def _dict_file_categories(data: dict[str, Any]) -> dict[str, str]:
    return {key: category for category in ("pre", "gpt", "post") for key in data.get(f"{category}_dict_files", [])}


# save_dict 的 category：文件还不存在时新建并登记到哪一类字典（原 create_dict_file 的职责）
DICT_CATEGORIES: tuple[str, ...] = ("pre", "gpt", "post")
_DICT_CATEGORY_LABELS = {"pre": "译前", "gpt": "GPT", "post": "译后"}
_DICT_PROJECT_MARKER = "(project_dir)"
# 能在新文件上执行的 action：delete 需要已有词条，对新文件没有意义
_DICT_CREATE_ACTIONS = ("overwrite", "patch")


def _dict_new_file_key(file_key: str) -> str:
    """新建时 file_key 可以只写文件名：补上项目字典的 (project_dir) 前缀。"""
    return file_key if file_key.startswith(_DICT_PROJECT_MARKER) else _DICT_PROJECT_MARKER + file_key


def _dict_save_plan(contents: dict[str, Any], args: dict[str, Any], categories: dict[str, str] | None = None) -> dict[str, Any]:
    """解析 save_dict 的入参并对照现有字典，定下写哪个文件、要不要先新建（只算不写）。

    save_dict 与审批卡预览共用：返回 {file_key, action, content, category, create, before_lines}。
    """
    file_key = str(args.get("file_key", "")).strip()
    if not file_key:
        raise AgentToolError("file_key is required")
    action = str(args.get("action", "") or "patch").strip().lower() or "patch"
    if action not in ("overwrite", "patch", "delete"):
        raise AgentToolError("action must be one of: overwrite, patch, delete（append / replace 已统一为 patch）")
    content = str(args.get("content", ""))
    if action == "delete" and not _split_dict_incoming(content):
        raise AgentToolError("delete 需要 content（要删除的词条，每行一个 key）")
    category = str(args.get("category", "") or "").strip().lower()
    if category and category not in DICT_CATEGORIES:
        raise AgentToolError("category must be one of: pre, gpt, post")

    create = False
    if file_key not in contents and category:
        file_key = _dict_new_file_key(file_key)
        create = file_key not in contents
    if file_key not in contents and not create:
        raise AgentToolError(
            f"字典文件不存在：{file_key}。已有：{list(contents)}。"
            "要新建就带上 category（pre=译前 / gpt=GPT / post=译后），file_key 写文件名（如 项目GPT字典2.txt）。"
        )
    if create and action not in _DICT_CREATE_ACTIONS:
        raise AgentToolError(f"{file_key} 还不存在，{action} 没有可改的词条：新建时用 overwrite 或 patch。")
    old = contents.get(file_key)
    before_lines = [str(x) for x in old.get("lines", [])] if isinstance(old, dict) else []
    return {
        "file_key": file_key,
        "action": action,
        "content": content,
        "category": category,
        "create": create,
        "before_lines": before_lines,
        "dictionary_type": (categories or {}).get(file_key, category if create else ""),
    }


def _tool_save_dict(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """写入项目字典。action 决定写入方式：

    - overwrite：显式指定才整文件覆盖；
    - patch（默认）：按每行的 key 更新已有词条（译名与备注），不存在则追加到末尾；
    - delete：按 key 删除词条（content 传要删的词条，可整行粘贴或只写 key）。

    带 category（pre/gpt/post）且文件还不存在时，先新建并登记到对应的字典清单，再写入。
    """
    pid = runner._project_id()
    # 先读旧内容（算行级 diff + 作为 patch 的基底），写完后随结果返回
    cfg = urllib.parse.quote(runner.state.config_file_name)
    data = runner._http_get(f"/api/projects/{pid}/dictionary/project?config={cfg}")
    plan = _dict_save_plan(data.get("dict_contents", {}), args, _dict_file_categories(data))
    file_key, action, before_lines = plan["file_key"], plan["action"], plan["before_lines"]

    created: dict[str, Any] = {}
    if plan["create"]:
        body = {
            "config_file_name": runner.state.config_file_name,
            "category": plan["category"],
            "filename": file_key[len(_DICT_PROJECT_MARKER):],
        }
        res = runner._http_post(f"/api/projects/{pid}/dictionary/project/create", body)
        file_key = str((res or {}).get("file_key") or file_key)
        created = {"created": True, "category": plan["category"]}

    new_lines, extra = _dict_new_lines(before_lines, plan["content"], action, category=plan["dictionary_type"])

    before_text = "\n".join(before_lines)
    new_text = "\n".join(new_lines)
    if new_text == before_text:
        note = (
            f"已新建空字典文件，并登记为{_DICT_CATEGORY_LABELS[plan['category']]}字典"
            if created
            else "内容没有变化，未写入"
        )
        return {"file_key": file_key, "action": action, **created, "note": note, **extra}

    body = {
        "config_file_name": runner.state.config_file_name,
        "file_key": file_key,
        "content": new_text,
    }
    runner._http_post(f"/api/projects/{pid}/dictionary/project/save", body)
    diff = _diff_lines(before_text, new_text)
    added = diff["added"]
    removed = diff["removed"]
    return {
        "file_key": file_key,
        "action": action,
        **created,
        "line_count_before": len(before_lines),
        "line_count_after": len(new_lines),
        "lines_added": added,
        "lines_removed": removed,
        "line_diff": diff,
        **extra,
    }
