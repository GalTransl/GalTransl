"""Agent 的模型配置；主代理与子代理使用同一套解析规则。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from GalTransl.Agent.backend_http import _normalize_endpoint
from GalTransl.Agent.context import _llm_timeout, _profile_context_window, _resolve_prompt_caching, _serialize_for_summary
from GalTransl.Agent.core import SUMMARY_MAX_TOKENS
from GalTransl.Agent.prompts import COMPACT_SUMMARY_PROMPT


@dataclass
class LLMBackend:
    client: Any
    model: str
    context_window: int
    prompt_caching: bool


def resolve_llm_backend(profile: dict[str, Any]) -> LLMBackend:
    section = profile.get("OpenAI-Compatible") or {}
    if not isinstance(section, dict):
        raise RuntimeError("backend profile missing OpenAI-Compatible section")
    tokens = section.get("tokens") or []
    if not isinstance(tokens, list) or not tokens:
        raise RuntimeError("backend profile OpenAI-Compatible.tokens is empty")
    first = tokens[0]
    if not isinstance(first, dict):
        raise RuntimeError("first token entry is not an object")
    token = str(first.get("token", "")).strip()
    model = str(first.get("modelName", "")).strip()
    if not token or not model:
        raise RuntimeError("请先在「翻译后端配置」页填写 token 和 modelName")
    base_url = _normalize_endpoint(str(first.get("endpoint", "")).strip())
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("openai 包未安装，Agent 无法运行") from exc
    return LLMBackend(
        client=OpenAI(api_key=token, base_url=base_url, max_retries=0, timeout=_llm_timeout()),
        model=model,
        context_window=_profile_context_window(profile),
        prompt_caching=_resolve_prompt_caching(section.get("promptCaching"), model, base_url),
    )


def summarize_messages(client: Any, model: str, messages: list[dict[str, Any]]) -> str:
    prompt = COMPACT_SUMMARY_PROMPT.replace("{conversation}", _serialize_for_summary(messages))
    response = client.chat.completions.create(
        model=model, messages=[{"role": "user", "content": prompt}],
        temperature=0, max_tokens=SUMMARY_MAX_TOKENS, stream=False,
    )
    if not getattr(response, "choices", None):
        return ""
    return str(getattr(response.choices[0].message, "content", None) or "")


class SubagentBackendView:
    """工具仍访问同一会话，但模型、压缩、缓存和校对署名使用子代理后端。"""

    def __init__(self, parent: Any, backend: LLMBackend, stop_event: Any) -> None:
        self._parent = parent
        self._openai_client = backend.client
        self._model = backend.model
        self._context_window = backend.context_window
        self._prompt_caching = backend.prompt_caching
        self.stop_event = stop_event

    def __getattr__(self, name: str) -> Any:
        return getattr(self._parent, name)

    def _summarize_messages(self, messages: list[dict[str, Any]]) -> str:
        return summarize_messages(self._openai_client, self._model, messages)
