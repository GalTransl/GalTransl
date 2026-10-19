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


def _thinking_enabled(raw: Any) -> bool | None:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in ("true", "on", "1"):
            return True
        if text in ("false", "off", "0"):
            return False
    return None


def build_reasoning_kwargs(
    section: dict[str, Any], token_entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把用户明确填写的思考选项转换成请求参数。

    每个参数都独立生效，令牌设置覆盖全局设置；不根据模型名或服务地址猜测格式。
    ``enableThinking`` 对应 Qwen3、DashScope、SiliconFlow 常见的 ``enable_thinking``；
    ``thinkingType`` 对应 DeepSeek 等接口的 ``thinking.type``；``reasoningEffort``
    对应 OpenAI o 系列、GPT-5 及许多中转的 ``reasoning_effort``。
    """
    section = section if isinstance(section, dict) else {}
    entry = token_entry if isinstance(token_entry, dict) else {}
    kwargs: dict[str, Any] = {}
    extra_body: dict[str, Any] = {}

    enabled = _thinking_enabled(_configured_value(entry, section, "enableThinking"))
    if enabled is not None:
        extra_body["enable_thinking"] = enabled

    thinking_type = str(_configured_value(entry, section, "thinkingType") or "").strip().lower()
    if thinking_type in {"enabled", "disabled"}:
        extra_body["thinking"] = {"type": thinking_type}

    effort = str(_configured_value(entry, section, "reasoningEffort") or "").strip().lower()
    if effort in REASONING_EFFORTS:
        kwargs["reasoning_effort"] = effort

    if extra_body:
        kwargs["extra_body"] = extra_body
    return kwargs


def apply_reasoning_kwargs(
    request: dict[str, Any], reasoning: dict[str, Any],
) -> dict[str, Any]:
    """合并思考参数，兼容 OpenAI 思考模型不接受的采样参数与旧输出上限。"""
    kwargs = {**request, **reasoning}
    request_extra = request.get("extra_body")
    reasoning_extra = reasoning.get("extra_body")
    if isinstance(request_extra, dict) and isinstance(reasoning_extra, dict):
        # 保留调用方的其他供应商参数；明确配置的思考参数覆盖同名字段。
        kwargs["extra_body"] = {**request_extra, **reasoning_extra}
    effort = reasoning.get("reasoning_effort")
    if effort and effort != "none":
        for name in ("temperature", "top_p", "frequency_penalty"):
            kwargs.pop(name, None)
    # 明确选用标准等级参数时，输出上限使用包含思考 token 的标准字段。
    if effort and "max_tokens" in kwargs:
        kwargs.setdefault("max_completion_tokens", kwargs.pop("max_tokens"))
    return kwargs
