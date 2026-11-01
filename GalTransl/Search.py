"""Shared ordering and sampling for bounded search results."""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Any, TypeVar

T = TypeVar("T")
SEARCH_ORDER_MODES = ("name", "reverse", "even", "random")
SEARCH_ORDER_LABELS = {"name": "文件/条目正序", "reverse": "文件/条目倒序", "even": "均匀采样", "random": "随机采样"}
CACHE_SEARCH_FIELDS = ("all", "src", "dst", "problem", "name", "pre_src", "post_src", "pre_dst", "proofread_dst")
CACHE_SEARCH_CONDITION_LIMIT = 20
CACHE_SEARCH_MAX_ROWS = 100
CACHE_SEARCH_MAX_CHARS = 10000


@dataclass(frozen=True)
class CacheSearchCondition:
    field: str
    query: str
    pattern: re.Pattern[str] | None = None
    match_case: bool = False

    def matches(self, text: str) -> bool:
        if self.pattern is not None:
            return bool(self.pattern.search(text))
        return self.query in text if self.match_case else self.query.lower() in text.lower()


def cache_search_texts(entry):
    """搜索与替换共用字段解释；精确字段仅兼容旧键，不回退到别的列。"""
    old_keys = {"pre_src": "pre_jp", "post_src": "post_jp", "pre_dst": "pre_zh", "proofread_dst": "proofread_zh"}
    exact = {key: str(entry.get(key, entry.get(old, "")) or "") for key, old in old_keys.items()}
    name = entry.get("name", "")
    return {
        **exact,
        "src": str(entry.get("post_src") or entry.get("post_jp") or entry.get("pre_src") or entry.get("pre_jp") or ""),
        "dst": str(entry.get("pre_dst") or entry.get("pre_zh") or entry.get("proofread_dst") or entry.get("proofread_zh") or ""),
        "problem": str(entry.get("problem") or ""),
        "name": "\n".join(str(part) for part in name) if isinstance(name, list) else str(name or ""),
    }


def cache_search_matches(texts, conditions):
    matched = []
    for condition in conditions:
        fields = ("src", "dst", "problem", "name") if condition.field == "all" else (condition.field,)
        hits = [field for field in fields if condition.matches(texts[field])]
        if not hits:
            return []
        matched.extend(hits)
    return list(dict.fromkeys(matched))


def parse_cache_search_conditions(payload: dict[str, Any]) -> list[CacheSearchCondition]:
    """各条件在同一条缓存上取交集；旧 query/field 作为额外的第一个条件。"""
    raw = payload.get("conditions")
    if raw is not None and (
        not isinstance(raw, list) or not 1 <= len(raw) <= CACHE_SEARCH_CONDITION_LIMIT
    ):
        raise ValueError(f"conditions must contain 1-{CACHE_SEARCH_CONDITION_LIMIT} search conditions")
    specs = list(raw or [])
    if str(payload.get("query", "")).strip():
        specs.insert(0, {"query": str(payload["query"]), "field": payload.get("field", "all"),
                         "options": payload.get("options", {})})
    conditions = []
    for spec in specs:
        if not isinstance(spec, dict):
            raise ValueError("each search condition must be an object")
        query = spec.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("each search condition requires a non-empty query")
        field = spec.get("field", "all")
        if field not in CACHE_SEARCH_FIELDS:
            raise ValueError(f"search field must be one of: {', '.join(CACHE_SEARCH_FIELDS)}")
        options = spec.get("options", {})
        if not isinstance(options, dict):
            raise ValueError("options must be an object")
        match_case = options.get("match_case", False)
        if not isinstance(match_case, bool):
            raise ValueError("match_case must be a boolean")
        use_regex = options.get("re", False)
        if not isinstance(use_regex, bool):
            raise ValueError("re must be a boolean")
        try:
            pattern = re.compile(
                query.strip() if use_regex else re.escape(query.strip()),
                0 if match_case else re.IGNORECASE,
            )
        except re.error as exc:
            raise ValueError(f"invalid regular expression: {exc}") from exc
        conditions.append(CacheSearchCondition(field, query.strip(), pattern, match_case))
    return conditions


def parse_search_order(args: dict[str, Any]) -> str:
    order = str(args.get("order", "name") or "name").strip().lower()
    if order not in SEARCH_ORDER_MODES:
        raise ValueError(f"order must be one of: {', '.join(SEARCH_ORDER_MODES)}")
    return order


def select_search_hits(hits: list[T], limit: int, offset: int, order: str) -> list[T]:
    """Sample the full remaining range before applying the result cap."""
    candidates = (list(reversed(hits)) if order == "reverse" else list(hits))[max(0, offset):]
    if limit <= 0:
        return []
    if order == "random":
        return random.sample(candidates, min(limit, len(candidates)))
    if limit >= len(candidates):
        return candidates
    if order == "even":
        if limit == 1:
            return candidates[:1]
        return [candidates[(i * (len(candidates) - 1)) // (limit - 1)] for i in range(limit)]
    return candidates[:limit]


def search_order_note(order: str) -> str:
    if order in ("even", "random"):
        return (
            f"已按{SEARCH_ORDER_LABELS[order]}从全部命中中挑选（offset 先跳过正序前 N 条）；"
            "上下文按文件内句子顺序展示。采样不能用 offset 保证连续无重复翻页；"
            "random 可再次调用换一批，要完整遍历请用 order=name/reverse 配合 offset。"
        )
    return ""
