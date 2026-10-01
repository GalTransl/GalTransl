"""问题白名单：按「缓存文件 + 条目 index」把指定缓存条目标记为"不检查问题"。

与 common.problemFilterKey（按问题文本子串过滤整类问题）不同，白名单是按位置精确豁免：
命中的条目等价于逐条勾选了 skip_check——不再检测、不再展示，也不计入问题统计。
存储为项目配置 common.problemWhiteList，元素形如：

    - "01.json:12"      # 01.json 的第 12 条
    - "01.json:12-15"   # 第 12~15 条（闭区间）

文件名按缓存快照名书写；写成 "01.json" 时同时覆盖翻译中的 "01.append.jsonl"。
"""

from __future__ import annotations

_APPEND_SUFFIX = ".append.jsonl"
# 单条区间上限：挡住手写的超大区间把内存撑爆（正常白名单不会有这么多条）
_MAX_RANGE_SPAN = 100000
_EMPTY_INDEXES: frozenset[int] = frozenset()


def normalize_problem_white_list(value) -> list[str]:
    """入参（字符串 / 字符串数组 / 配置里那串）→ 去空白、去重保序的原始条目列表。"""
    if isinstance(value, str):
        items = value.splitlines()
    elif isinstance(value, list):
        items = value
    else:
        return []
    return list(dict.fromkeys(s.strip() for s in items if isinstance(s, str) and s.strip()))


def canonical_cache_name(name) -> str:
    """缓存文件名归一：.append.jsonl 增量日志与对应的 .json 视为同一份文件。"""
    text = str(name or "")
    if text.endswith(_APPEND_SUFFIX):
        return text[: -len(_APPEND_SUFFIX)]
    return text


def parse_problem_white_list_entry(spec) -> tuple[str, frozenset[int]] | None:
    """解析单条白名单：返回（归一文件名, 命中的 index 集合）；不合法返回 None。

    合法形式："<文件>:<index>"，index 为单个（"12"）或闭区间（"12-15"）。
    """
    text = str(spec or "").strip()
    filename, sep, index_token = text.rpartition(":")
    filename = filename.strip()
    index_token = index_token.strip()
    if not sep or not filename or not index_token:
        return None
    if "-" in index_token:
        start_text, _, end_text = index_token.partition("-")
        try:
            start, end = int(start_text), int(end_text)
        except ValueError:
            return None
        if start > end:
            start, end = end, start
        if end - start > _MAX_RANGE_SPAN:
            return None
        return canonical_cache_name(filename), frozenset(range(start, end + 1))
    try:
        return canonical_cache_name(filename), frozenset({int(index_token)})
    except ValueError:
        return None


def build_problem_white_list_index(specs) -> dict[str, frozenset[int]]:
    """把白名单条目列表编译成 {归一文件名: index 集合}，逐条判断时 O(1) 查询。

    不合法的条目忽略（配置可能被手改）；同一文件的多个区间取并集。
    """
    merged: dict[str, set[int]] = {}
    for spec in normalize_problem_white_list(specs):
        parsed = parse_problem_white_list_entry(spec)
        if parsed is None:
            continue
        filename, indexes = parsed
        merged.setdefault(filename, set()).update(indexes)
    return {name: frozenset(values) for name, values in merged.items()}


def is_problem_whitelisted(index_map, filename, entry_index) -> bool:
    """(文件, index) 是否命中白名单。index_map 由 build_problem_white_list_index 生成。"""
    if not index_map:
        return False
    try:
        idx = int(entry_index)
    except (TypeError, ValueError):
        return False
    return idx in index_map.get(canonical_cache_name(filename), _EMPTY_INDEXES)
