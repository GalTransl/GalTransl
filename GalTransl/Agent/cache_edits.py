"""Attribute HTTP cache edits without sharing mutable state between workers."""

from contextlib import contextmanager
from contextvars import ContextVar


_EDIT_SOURCE: ContextVar[dict] = ContextVar("cache_edit_source", default={})


@contextmanager
def cache_edit_source(metadata: dict):
    token = _EDIT_SOURCE.set(metadata)
    try:
        yield
    finally:
        _EDIT_SOURCE.reset(token)


def current_cache_edit_source(runner) -> dict:
    return {
        "session_id": runner.state.session_id,
        "turn_id": getattr(runner, "_cache_turn_id", ""),
        "actor_id": "main", "actor_label": "主 Agent",
        "tool_call_id": runner._active_tool_call_id,
        **_EDIT_SOURCE.get(),
    }
