"""Provider 返回的 usage 解析：翻译请求的用量计量与 Agent 的缓存统计共用同一套口径。

单独成模块而不是放在 GalTransl/Agent 里：翻译引擎每次请求都要用，不能为此把整个 Agent 包导入进来。
"""

from __future__ import annotations

from typing import Any


def _field(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else getattr(value, key, None)


def _tokens(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _provider_cache_usage(usage: Any) -> tuple[int, int | None] | None:
    """OpenAI/DeepSeek/兼容端点；缓存字段缺失不能当成零命中。

    OpenAI prompt_tokens 已含缓存。仅原生 Anthropic input_tokens 不含 read/write，
    此时才补上，避免对兼容端点的 prompt_tokens 重复加算。
    """
    cached = None
    for value in (
        _field(_field(usage, "prompt_tokens_details"), "cached_tokens"),
        _field(usage, "prompt_cache_hit_tokens"),
        _field(usage, "cache_read_input_tokens"),
    ):
        cached = _tokens(value)
        if cached is not None:
            break
    total = _tokens(_field(usage, "prompt_tokens"))
    if total is None:
        total = _tokens(_field(usage, "input_tokens"))
        if total is not None:
            total += (_tokens(_field(usage, "cache_read_input_tokens")) or 0)
            total += (_tokens(_field(usage, "cache_creation_input_tokens")) or 0)
        else:
            miss = _tokens(_field(usage, "prompt_cache_miss_tokens"))
            if cached is not None and miss is not None:
                total = cached + miss
    if total is None or total <= 0:
        return None
    return total, cached if cached is not None and cached <= total else None


def parse_request_usage(usage: Any) -> dict[str, int | None] | None:
    """一次请求的用量：{input_tokens, cached_tokens, output_tokens, reasoning_tokens}。

    没有可用的输入 token 数时返回 None（接口没报 usage）。cached_tokens / reasoning_tokens
    为 None 表示接口没报这一项，不能当成 0：否则命中率和思考占比会被没报的请求拉低。
    output_tokens 按 OpenAI 的口径包含思考 token。
    """
    reported = _provider_cache_usage(usage)
    if reported is None:
        return None
    input_tokens, cached_tokens = reported
    output_tokens = _tokens(_field(usage, "completion_tokens"))
    if output_tokens is None:
        output_tokens = _tokens(_field(usage, "output_tokens")) or 0
    reasoning_tokens = _tokens(
        _field(_field(usage, "completion_tokens_details"), "reasoning_tokens")
    )
    if reasoning_tokens is not None and reasoning_tokens > output_tokens:
        reasoning_tokens = None
    return {
        "input_tokens": input_tokens,
        "cached_tokens": cached_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
    }
