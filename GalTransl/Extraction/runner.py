"""Extraction domain adapter for the translation AgentRunner.

Streaming, retry, history, cancellation, approvals and compaction all execute in
GalTransl.Agent. This module supplies only tools, a prompt and the UI event bridge.
"""
from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path

from GalTransl.Agent.cache_usage import _restore_cache_usage
from GalTransl.Agent.context import _restore_compacted_history, _restore_microcompacted_history, _restore_usage_anchor
from GalTransl.Agent.core import AgentStopRequested
from GalTransl.Agent.models import AgentState, AgentToolError
from GalTransl.Agent.permissions import AUTO_QUIET_MODE
from GalTransl.Agent.prompts import INITIAL_USER_MESSAGE_PREFIX
from GalTransl.Agent.runner import AgentRunner
from GalTransl.Agent.session_store import SessionStore
from GalTransl.Agent.tools.ask import _auto_ask_answers, _tool_ask_user, _tool_read_history_archive
from .tool_schemas import EXTRACTION_TOOLS, prepare_arguments, tool_prompt_section, validate_arguments
from .tools import ExtractionTools
from .skills import discover_skills, expand_skill_command, format_skills_for_prompt


def display_arguments(value, budget=24000):
    """Bound UI/approval previews; execution always receives the original arguments."""
    if isinstance(value, dict) and 'reason' in value:
        value = {'reason': value['reason'], **{key: item for key, item in value.items() if key != 'reason'}}
    remaining = budget
    def visit(item):
        nonlocal remaining
        if isinstance(item, str):
            data = item.encode('utf-8')
            size = min(len(data), max(0, remaining))
            remaining -= size
            return data[:size].decode('utf-8', errors='ignore') + ('… [preview truncated]' if size < len(data) else '')
        if isinstance(item, dict):
            result = {}
            for key, child in item.items():
                if remaining <= 0:
                    result['_preview'] = 'Additional fields omitted'
                    break
                remaining -= len(str(key).encode('utf-8')) + 8
                result[key] = visit(child)
            return result
        if isinstance(item, list):
            result = []
            for child in item:
                if remaining <= 0:
                    result.append('[Additional entries omitted]')
                    break
                remaining -= 8
                result.append(visit(child))
            return result
        remaining -= 32
        return item
    return visit(value)


def restore_state(session, directory, profile, prompt, mode):
    store = SessionStore(session["workspace"], session["id"], directory=str(directory / "execution"))
    saved = store.load()
    messages = _restore_compacted_history(_restore_microcompacted_history(
        saved["messages"], saved.get("microcompactions", [])))
    meta = saved["meta"]
    usage, count, model, overhead = _restore_usage_anchor(messages, meta.get("context_usage_anchor"))
    snapshot = meta.get("mnemon_snapshot")
    state = AgentState(project_dir=session["workspace"], session_id=session["id"], title=session["title"],
        status="running", first_prompt=prompt, backend_profile_data=profile, permission_mode=mode,
        messages=messages, last_prompt_tokens=usage, anchored_message_count=count, usage_model=model,
        usage_overhead_tokens=overhead,
        mnemon_snapshot=snapshot if isinstance(snapshot, str) else None,
        cache_usage=_restore_cache_usage(meta.get("cache_usage")),
        step=max((event.get("step", 0) for event in saved["events"]), default=0))
    return state, store


class ExtractionRunner(AgentRunner):
    _billion_context_setting = "extractionBillionContextEnabled"
    _mnemon_setting = "extractionMnemonEnabled"
    _mnemon_agent = "extraction"

    def _mnemon_project_dir(self):
        return self.session["game_dir"]

    def __init__(self, state, store, session, available, emit):
        super().__init__(state)
        self._store = store
        self.session = session
        self.available = available
        self.publish = emit
        self.message_id = ""
        self.turn_lock = threading.RLock()
        self.skills = available.get("skills")
        if self.skills is None:
            self.skills, _ = discover_skills([Path(available["settings"]["skill_dir"])])
        self.tools = ExtractionTools(session["workspace"], session["game_dir"], available["settings"]["skill_dir"],
                                     available, self.stop_event, emit, self._require_permission,
                                     session_dir=Path(store.dir).parent)

    def _system_prompt(self):
        sections = [
            "You are an expert extraction assistant operating inside GalTransl, a coding agent harness. "
            "You help users by reading files, executing commands, editing code, and writing new files.",
            tool_prompt_section(),
            f"# Project Context\n\nGame directory: {Path(self.session['game_dir']).as_posix()}\n"
            "Write outputs only in the current working directory; preserve original game files and installed skills.",
            format_skills_for_prompt(self.skills),
            f"Current working directory: {Path(self.session['workspace']).as_posix()}",
        ]
        return "\n\n".join(section for section in sections if section)

    def _request_system_prompt(self, saved_prompt):
        # Refresh generated capabilities while retaining the session's memory snapshot.
        return super()._request_system_prompt(self._system_prompt())

    def _tool_definitions(self):
        return EXTRACTION_TOOLS

    def _messages_for_request(self, *, strip_internal=True):
        messages = super()._messages_for_request(strip_internal=strip_internal)
        projected, attachments = [], []
        for message in messages:
            # Chat Completions tool messages accept text only. Keep all replies
            # in a tool-call batch together, then supply images as user content.
            # Projecting here also repairs saved sessions without rewriting them.
            if message.get('role') != 'tool' and attachments:
                projected.extend(attachments)
                attachments = []
            content = message.get('content')
            if message.get('role') == 'tool' and isinstance(content, list):
                images = [block for block in content if block.get('type') == 'image_url']
                if images:
                    attachment = {'role': 'user', 'content': [
                        {'type': 'text', 'text': f"Images returned by tool call {message['tool_call_id']}:"},
                        *images,
                    ]}
                    if not strip_internal:
                        attachment['_billion_context_synthetic'] = True
                    attachments.append(attachment)
                    message = {key: value for key, value in message.items() if key != '_images'}
                    message['content'] = '\n'.join(block['text'] for block in content if block.get('type') == 'text')
            projected.append(message)
        projected.extend(attachments)
        return projected

    def _serialize_tool_result(self, result):
        if isinstance(result, dict) and isinstance(result.get("content"), list):
            self._tool_images = [block["image"] for block in result["content"] if block.get("type") == "image" and "image" in block]
            return "\n".join(block["text"] for block in result["content"] if block.get("type") == "text")
        # The old sidecar returned free-text answers verbatim and objects as JSON.
        return result if isinstance(result, str) else super()._serialize_tool_result(result)

    def _persist_message(self, message):
        if message.get("role") == "user":
            def expand(text):
                prefix = INITIAL_USER_MESSAGE_PREFIX if text.startswith(INITIAL_USER_MESSAGE_PREFIX) else ""
                return prefix + expand_skill_command(text.removeprefix(prefix), self.skills)

            content = message.get("content")
            if isinstance(content, str):
                message = {**message, "content": expand(content)}
            if isinstance(content, list):
                message = {**message, "content": [
                    {**block, "text": expand(block["text"])} if block.get("type") == "text" else block
                    for block in content]}
        if message.get("role") == "tool" and getattr(self, "_tool_images", None):
            message = {**message, "_images": self._tool_images}
            self._tool_images = []
        return super()._persist_message(message)

    def _dispatch_tool(self, name, args):
        args = prepare_arguments(name, args)
        validate_arguments(name, args)
        if self.stop_event.is_set():
            raise AgentStopRequested()
        try:
            if name == "read_history_archive":
                return _tool_read_history_archive(self, args)
            if name == "ask_user":
                result = _tool_ask_user(self, args)
                if self.stop_event.is_set():
                    raise AgentStopRequested()
                return result
            result = getattr(self.tools, name)(args, self._active_tool_call_id)
            if name == "edit" and any(len(result['details'][key].encode('utf-8')) > 51200 for key in ('diff', 'patch')):
                # Preserve full evidence while keeping shared events and UI journals bounded.
                report = self.tools.write_path(f'reports/edit-{uuid.uuid4().hex}.json')
                self.tools.save(report, json.dumps(result['details'], ensure_ascii=False))
                details = {**result['details'], 'fullDiffPath': str(report)}
                for key in ('diff', 'patch'):
                    raw = details[key].encode('utf-8')
                    if len(raw) > 51200:
                        details[key] = raw[:51200].decode('utf-8', errors='ignore') + f'\n[Preview truncated. Full diff and patch: {report}]'
                result = {**result, 'details': details}
            if name == "read" and isinstance(result, dict):
                from GalTransl.Agent.images import save_tool_image
                blocks = []
                for block in result["content"]:
                    if block.get("type") == "image":
                        blocks.append({"type": "image", "image": save_tool_image(self._store, block, args["path"])})
                    else:
                        blocks.append(block)
                # structuredContent repeats base64 in native Pi; persist references instead.
                if any(block.get("type") == "image" for block in blocks):
                    result = {"content": blocks}
            return result
        except (OSError, UnicodeError, ValueError) as exc:
            raise AgentToolError(str(exc)) from exc

    def _on_ask_pending(self, holder):
        # A mode change can race with the question being registered. Check again
        # after registration, while publishing through the runtime's lock.
        self.publish({"type": "ask_request", "requestId": holder["request_id"],
                      "toolCallId": holder["tool_call_id"], "questions": holder["questions"],
                      "question": holder["questions"][0]["question"]})

    def apply_permission_mode(self, mode):
        super().apply_permission_mode(mode)
        if self.state.permission_mode == AUTO_QUIET_MODE:
            with self._ask_lock:
                holder = self._pending_ask
                if holder is not None and not holder['event'].is_set():
                    holder['answers'] = _auto_ask_answers(holder['questions'])
                    holder['event'].set()

    def abort_in_flight(self):
        super().abort_in_flight()
        self.tools.commands.stop()

    def _stream_llm_attempt(self):
        if not self._stream_quiet:
            self.message_id = uuid.uuid4().hex
            self.publish({"type": "assistant_start", "messageId": self.message_id})
        return super()._stream_llm_attempt()

    def _end_turn(self, kind, data):
        # Queue submission and turn completion share the runtime's lock.
        with self.turn_lock:
            self._close_turn(kind, data)

    def _emit(self, event_type, data):
        if event_type in {'tool_call', 'permission_request'} and data.get('name') in {'read', 'bash', 'edit', 'write', 'run_python'}:
            data = {**data, 'arguments': display_arguments(data.get('arguments', {}))}
        if event_type == 'tool_result' and isinstance(data.get('result'), dict) and 'content' in data['result']:
            data = {**data, 'result': {key: value for key, value in data['result'].items() if key != 'structuredContent'}}
        super()._emit(event_type, data)
        event = None
        if event_type == "user_message":
            event = {"type": "user_message", "text": data.get("message", ""),
                     **({'images': data['images']} if data.get('images') else {})}
        elif event_type in {"content_delta", "reasoning_delta"}:
            event = {"type": "assistant_delta" if event_type == "content_delta" else "thinking_delta",
                     "messageId": self.message_id, "text": data.get("delta", "")}
        elif event_type in {"content_end", "reasoning_end"}:
            event = {"type": event_type, "messageId": self.message_id, **data}
        elif event_type == "assistant_message":
            parts = data.get("parts", [])
            text = "".join(part.get("text", "") for part in parts if part["type"] == "text")
            thinking = "".join(part.get("text", "") for part in parts if part["type"] == "reasoning")
            event = {"type": "assistant_message", "messageId": self.message_id, "text": text[:64000],
                     "stopReason": "toolUse" if any(part["type"] == "tool_call" for part in parts) else "stop"}
            if thinking:
                self.publish({"type": "thinking_message", "messageId": self.message_id, "text": thinking[-64000:]})
        elif event_type == "tool_call":
            event = {"type": "tool_start", "toolCallId": data["id"], "toolName": data["name"], "arguments": data.get("arguments", {})}
        elif event_type == "tool_result":
            result = data.get("result", "") if data.get("ok") else data.get("error", "Tool failed")
            if "result" in data and isinstance(data["result"], dict) and "content" in data["result"]:
                result = data["result"]
            native = isinstance(result, dict) and isinstance(result.get("content"), list)
            if native:
                text = "\n".join(block["text"] for block in result["content"] if block.get("type") == "text")
                if result.get("details", {}).get("diff"):
                    text += "\n\n" + result["details"]["diff"]
            else:
                text = (result if isinstance(result, str) else json.dumps(result, ensure_ascii=False))[-24000:]
            event = {"type": "tool_end", "toolCallId": data["id"], "toolName": data["name"],
                     "text": text,
                     "isError": not data.get("ok"), "duration_ms": data.get("duration_ms")}
            if native:
                images = [block["image"] for block in result["content"] if block.get("type") == "image" and "image" in block]
                if images:
                    event["images"] = images
                if result.get("details"):
                    event["details"] = result["details"]
        elif event_type == "permission_request":
            event = {"type": "permission_request", "requestId": data["id"], "toolCallId": data["tool_call_id"],
                     "toolName": data["name"], "arguments": data["arguments"], "risk": data["risk"]}
        elif event_type == "context_usage":
            event = {"type": "context_usage", **data["context"]}
        elif event_type == "compacting":
            event = {"type": "compaction", "active": data.get("phase") == "start"}
        elif event_type in {"compacted", "queue"}:
            event = {"type": event_type, **data}
        elif event_type in {"llm_retry_start", "llm_retry_end"}:
            event = {"type": event_type, "messageId": self.message_id, **data}
        elif event_type == "error":
            event = {"type": "error", "message": str(data.get("message", "Model request failed"))[:2000]}
        if event:
            self.publish(event)
