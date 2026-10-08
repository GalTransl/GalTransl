"""Shared ordering and sampling for bounded search results."""

from __future__ import annotations

import random
from typing import Any, TypeVar

T = TypeVar("T")
SEARCH_ORDER_MODES = ("name", "reverse", "even", "random")
SEARCH_ORDER_LABELS = {"name": "文件/条目正序", "reverse": "文件/条目倒序", "even": "均匀采样", "random": "随机采样"}


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
