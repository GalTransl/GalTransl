"""OpenAI 兼容接口的思考配置；所有模型调用共用同一套参数转换。"""

from __future__ import annotations

from typing import Any


REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def _configured_value(
    entry: dict[str, Any], section: dict[str, Any], key: str,
) -> Any:
    """读取令牌覆盖；空值表示未配置，回退到全局值。"""
    value = entry.get(key)
    if value is None:
        return section.get(key)
    if isinstance(value, str) and not value.strip():
        return section.get(key)
    return value


def build_reasoning_kwargs(
    section: dict[str, Any], token_entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把明确配置的 ``reasoningEffort`` 转换成请求参数 ``reasoning_effort``。

    令牌设置覆盖全局设置；不根据模型名或服务地址推断思考等级。
    """
    section = section if isinstance(section, dict) else {}
    entry = token_entry if isinstance(token_entry, dict) else {}
    kwargs: dict[str, Any] = {}
    effort = str(_configured_value(entry, section, "reasoningEffort") or "").strip().lower()
    if effort in REASONING_EFFORTS:
        kwargs["reasoning_effort"] = effort

    return kwargs


def apply_reasoning_kwargs(
    request: dict[str, Any], reasoning: dict[str, Any],
) -> dict[str, Any]:
    """合并思考参数，兼容 OpenAI 思考模型不接受的采样参数与旧输出上限。"""
    kwargs = {**request, **reasoning}
    effort = reasoning.get("reasoning_effort")
    if effort and effort != "none":
        for name in ("temperature", "top_p", "frequency_penalty"):
            kwargs.pop(name, None)
    # 明确选用标准等级参数时，输出上限使用包含思考 token 的标准字段。
    if effort and "max_tokens" in kwargs:
        kwargs.setdefault("max_completion_tokens", kwargs.pop("max_tokens"))
    return kwargs
