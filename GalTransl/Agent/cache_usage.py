"""Provider 实测提示缓存用量；与上下文估算锚点独立，不因压缩而清零。"""

from __future__ import annotations

from typing import Any

from GalTransl.TokenUsage import _field, _provider_cache_usage, _tokens  # 旧导入路径仍可用


def _restore_cache_usage(raw: Any) -> dict[str, Any]:
    """恢复有界累计统计，百分比重新推导，拒绝损坏的落盘值。"""
    if not isinstance(raw, dict):
        return {}
    count = _tokens(raw.get("request_count"))
    total = _tokens(raw.get("input_tokens"))
    cached = _tokens(raw.get("cached_tokens"))
    latest_total = _tokens(raw.get("latest_input_tokens"))
    latest_cached = _tokens(raw.get("latest_cached_tokens"))
    if (count is None or total is None or cached is None or cached > total
            or (count == 0) != (total == 0) or not latest_total):
        return {}
    if latest_cached is not None and latest_cached > latest_total:
        latest_cached = None
    return {
        "request_count": count, "input_tokens": total, "cached_tokens": cached,
        "hit_rate": cached / total if total else None,
        "latest_input_tokens": latest_total, "latest_cached_tokens": latest_cached,
        "latest_hit_rate": latest_cached / latest_total if latest_cached is not None else None,
    }


def _add_cache_usage(previous: dict[str, Any], usage: tuple[int, int | None]) -> dict[str, Any]:
    previous = _restore_cache_usage(previous)
    total, cached = usage
    known = cached is not None
    return _restore_cache_usage({
        "request_count": previous.get("request_count", 0) + int(known),
        "input_tokens": previous.get("input_tokens", 0) + (total if known else 0),
        "cached_tokens": previous.get("cached_tokens", 0) + (cached if known else 0),
        "latest_input_tokens": total, "latest_cached_tokens": cached,
    })
