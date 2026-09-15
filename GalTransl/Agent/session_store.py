"""Agent 会话落盘（JSONL）。

一个会话一个文件，append-only 逐行写：

    <程序根>/agent_sessions/<base64url(project_dir)>/<session_id>.jsonl

每行一条 JSON 记录，`t` 字段区分类型：

- meta    —— 会话元信息（标题、配置文件、后端配置、目标、创建时间）
- message —— 一条 OpenAI 格式的对话消息（角色/内容/tool_calls）
- event   —— 一条 AgentEvent（前端转录用；content_delta 不落盘，高频且可重建）
- compact —— 上下文压缩发生点的标记

设计原则：落盘失败绝不能打断 Agent 主流程，所有 IO/解析异常都降级为日志警告。
逐行解析时跳过损坏行，这样进程被强杀写了一半也不会让整个会话不可读。
"""
from __future__ import annotations

import json
import os
import time
from base64 import urlsafe_b64encode
from collections import deque
from typing import Any
from uuid import uuid4

# 程序根 = app_settings.json 所在目录，与 AppSettings.py 保持一致
_PROGRAM_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSIONS_ROOT = os.path.join(os.path.dirname(_PROGRAM_ROOT), "agent_sessions")

# 不落盘的事件类型：流式增量高频且能由最终消息重建
_SKIP_EVENT_TYPES = {"content_delta", "reasoning_delta", "wait_tick"}

# 新建但还没发第一条消息时的占位标题；首条用户消息到达后会被其内容替换
DEFAULT_TITLE = "新会话"
# 标题最大字符数（超出截断并加省略号）
TITLE_MAX_CHARS = 30
# 转录回放时跳过的事件类型：瞬态数据（流式增量、等待倒计时、上下文用量指标），
# 它们不进 events deque 也不该出现在重建出来的转录里
_SKIP_TRANSCRIPT_TYPES = frozenset({"content_delta", "reasoning_delta", "wait_tick", "context_usage"})
# 一次最多回放多少条转录事件（超出只保留最早的 user_message + 最近这段）
TRANSCRIPT_MAX_EVENTS = 2000


def _compact_event_for_storage(event: dict[str, Any]) -> dict[str, Any]:
    """避免 tool_result 在 event + message 两条记录中重复保存大结果。

    tool message 是模型续聊所需的权威结果；event 只保留前端转录所需的
    调用标识、状态和耗时。读取时再从对应 tool message 补回 result。
    """
    if event.get("type") != "tool_result":
        return event
    # 成功结果通常是体积最大的字段，必须避免和 role=tool message 重复落盘。
    # error 一般很短，而且某些异常路径（例如响应被截断）没有对应的 tool
    # message；保留它可避免这类事件在重启后丢失诊断信息。
    return {key: value for key, value in event.items() if key != "result"}


def _restore_tool_results(
    events: list[dict[str, Any]], messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """从 tool message 恢复被压缩掉的 tool_result.result 字段。"""
    results: dict[str, tuple[Any, str | None]] = {}
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        call_id = str(message.get("tool_call_id") or "")
        if not call_id:
            continue
        raw = message.get("content")
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, json.JSONDecodeError):
            payload = raw
        if isinstance(payload, dict) and "error" in payload:
            results[call_id] = (None, str(payload.get("error") or ""))
        else:
            results[call_id] = (payload, None)

    restored: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "tool_result" or "result" in event or "error" in event:
            restored.append(event)
            continue
        call_id = str(event.get("id") or "")
        payload = results.get(call_id)
        if payload is None:
            restored.append(event)
            continue
        result, error = payload
        enriched = dict(event)
        if error is not None:
            enriched["error"] = error
            enriched["ok"] = False
        else:
            enriched["result"] = result
            enriched["ok"] = True
        restored.append(enriched)
    return restored


def _log(msg: str, *args: object) -> None:
    try:
        import time as _t

        print(f"[{_t.strftime('%H:%M:%S')}] [Agent/store] {msg}", *args, flush=True)
    except Exception:  # noqa: BLE001 - 日志不能影响主流程
        pass


def encode_project_dir(project_dir: str) -> str:
    """项目目录 -> 文件名安全 token（UTF-8 base64url 去填充）。"""
    raw = os.path.abspath(project_dir).encode("utf-8")
    return urlsafe_b64encode(raw).decode("ascii").rstrip("=").replace("+", "-").replace("/", "_")


def project_dir_for(project_dir: str) -> str:
    """该项目所有会话文件的存放目录。"""
    return os.path.join(SESSIONS_ROOT, encode_project_dir(project_dir))


def new_session_id() -> str:
    return uuid4().hex


class SessionStore:
    """单会话的 JSONL 读写器。构造时只算路径，不碰磁盘。"""

    def __init__(self, project_dir: str, session_id: str) -> None:
        self.project_dir = project_dir
        self.session_id = session_id
        self.dir = project_dir_for(project_dir)
        self.path = os.path.join(self.dir, f"{session_id}.jsonl")

    # ---- 写入 ----
    def _append(self, record: dict[str, Any]) -> None:
        try:
            os.makedirs(self.dir, exist_ok=True)
            line = json.dumps(record, ensure_ascii=False) + "\n"
            # 若上一次写被强杀在中途（缺末尾换行），先补一个换行把残行终结掉，
            # 否则本次记录会粘在残行后面，两条记录在 load() 时会作为一条损坏行
            # 一起被丢弃。用二进制读最后一个字节判断，避免误判文本模式下的 \r。
            prefix = ""
            if os.path.isfile(self.path) and os.path.getsize(self.path) > 0:
                with open(self.path, "rb") as f:
                    f.seek(-1, os.SEEK_END)
                    if f.read(1) != b"\n":
                        prefix = "\n"
            with open(self.path, "a", encoding="utf-8") as f:
                if prefix:
                    f.write(prefix)
                f.write(line)
        except Exception as exc:  # noqa: BLE001 - 落盘失败不影响主流程
            _log(f"写入失败 {self.path}: {exc}")

    def append_meta(self, **fields: Any) -> None:
        self._append({"t": "meta", "at": time.time(), **fields})

    def append_message(self, message: dict[str, Any]) -> None:
        self._append({"t": "message", "at": time.time(), "msg": message})

    def append_event(self, event: dict[str, Any]) -> None:
        if event.get("type") in _SKIP_EVENT_TYPES:
            return
        self._append({"t": "event", "at": time.time(), "event": _compact_event_for_storage(event)})

    def append_compact(self, *, removed: int, summary_chars: int, tokens_before: int) -> None:
        self._append({
            "t": "compact",
            "at": time.time(),
            "removed": removed,
            "summary_chars": summary_chars,
            "tokens_before": tokens_before,
        })

    # ---- 读取 ----
    def load(self) -> dict[str, Any]:
        """读回整个会话。文件不存在或全损坏时返回空结构。

        返回 {meta, messages, events, compactions}。损坏行跳过。
        """
        out: dict[str, Any] = {"meta": {}, "messages": [], "events": [], "compactions": []}
        if not os.path.isfile(self.path):
            return out
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # 写了一半的行，跳过
                    if not isinstance(rec, dict):
                        continue
                    kind = rec.get("t")
                    if kind == "meta":
                        # meta 记录是增量写入的（例如收尾只写 running=false），
                        # 不能用最新一条覆盖早先的 goal/title/config 等字段。
                        out["meta"].update({k: v for k, v in rec.items() if k not in ("t", "at")})
                    elif kind == "message":
                        msg = rec.get("msg")
                        if isinstance(msg, dict):
                            out["messages"].append(msg)
                    elif kind == "event":
                        ev = rec.get("event")
                        if isinstance(ev, dict):
                            out["events"].append(ev)
                    elif kind == "compact":
                        out["compactions"].append(rec)
        except Exception as exc:  # noqa: BLE001
            _log(f"读取失败 {self.path}: {exc}")
        out["events"] = _restore_tool_results(out["events"], out["messages"])
        return out

    def clear(self) -> None:
        """删除本会话文件（reset / delete 用）。"""
        try:
            if os.path.isfile(self.path):
                os.remove(self.path)
        except Exception as exc:  # noqa: BLE001
            _log(f"删除失败 {self.path}: {exc}")


# ---- 项目级操作 ----


def _read_meta(path: str) -> dict[str, Any]:
    """读会话的文件级 meta（标题/目标/配置/创建时间），后写的字段优先。

    meta 是增量追加的：新建只写占位标题，首条消息到达后才补写真正的标题，
    收尾再补 running=false。所以不能只认第一条 meta（否则列表里永远是新建
    时的占位标题），要像 load() 那样按顺序合并。为兼顾大会话，只解析形如
    meta 的行，message/event 大行直接跳过。
    """
    meta: dict[str, Any] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if '"meta"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("t") == "meta":
                    meta.update({k: v for k, v in rec.items() if k != "t"})
    except Exception:  # noqa: BLE001
        pass
    return meta


def list_sessions(project_dir: str) -> list[dict[str, Any]]:
    """列出项目的所有会话，最近更新的在前。"""
    directory = project_dir_for(project_dir)
    if not os.path.isdir(directory):
        return []
    items: list[dict[str, Any]] = []
    try:
        for name in os.listdir(directory):
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(directory, name)
            session_id = name[: -len(".jsonl")]
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = 0.0
            meta = _read_meta(path)
            items.append({
                "session_id": session_id,
                "title": str(meta.get("title") or session_id),
                "created_at": float(meta.get("created_at") or mtime or 0.0),
                "updated_at": float(mtime or 0.0),
            })
    except Exception as exc:  # noqa: BLE001
        _log(f"列会话失败 {directory}: {exc}")
        return []
    items.sort(key=lambda it: it["updated_at"], reverse=True)
    return items


def create_session(project_dir: str, title: str = "") -> str:
    """新建会话文件并写 meta，返回 session_id。

    title 为空时先用占位标题（DEFAULT_TITLE），真正的标题在用户发出第一条
    消息时由 title_from_message 生成并写回 meta。
    """
    session_id = new_session_id()
    store = SessionStore(project_dir, session_id)
    store.append_meta(
        project_dir=project_dir,
        title=title or DEFAULT_TITLE,
        created_at=time.time(),
    )
    return session_id


def delete_session(project_dir: str, session_id: str) -> None:
    SessionStore(project_dir, session_id).clear()


def session_exists(project_dir: str, session_id: str) -> bool:
    return os.path.isfile(SessionStore(project_dir, session_id).path)


def title_from_message(text: str, fallback: str = DEFAULT_TITLE) -> str:
    """用用户的第一条消息生成会话标题。

    换行与连续空白折叠成单个空格（标题必须是单行），超过 TITLE_MAX_CHARS
    截断并补省略号；消息为空时返回 fallback。
    """
    line = " ".join(str(text or "").split())
    if not line:
        return fallback
    if len(line) <= TITLE_MAX_CHARS:
        return line
    return line[:TITLE_MAX_CHARS] + "…"


def read_meta(project_dir: str, session_id: str) -> dict[str, Any]:
    """读会话 meta（标题/目标/配置文件/创建时间等）。"""
    return _read_meta(SessionStore(project_dir, session_id).path)


def session_title(project_dir: str, session_id: str, fallback: str = "") -> str:
    """读磁盘 meta 里的会话标题（内存中没有该会话状态时用）。"""
    meta = read_meta(project_dir, session_id)
    return str(meta.get("title") or fallback or session_id)


def read_transcript(project_dir: str, session_id: str, limit: int = TRANSCRIPT_MAX_EVENTS) -> list[dict[str, Any]]:
    """从会话日志回放转录事件（已提交的那部分），按发生顺序返回。

    与 status().events 的区别：那个是内存里 500 条的滑动窗口，长期会话里最早的
    记录会被挤掉；这里直接读会话 JSONL，转录不会因为窗口而缺头
    （转录从持久化日志派生，不从内存状态派生）。

    超过 limit 时保留**首条 user_message + 最近 limit 条**——首条用户消息是会话
    的身份锚点，丢了刷新后第一句话就没来源了。
    """
    path = SessionStore(project_dir, session_id).path
    if not os.path.isfile(path):
        return []
    tail: deque[dict[str, Any]] = deque(maxlen=max(1, limit))
    messages: list[dict[str, Any]] = []
    first_user: dict[str, Any] | None = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if '"event"' not in line and '"message"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("t") != "event":
                    if rec.get("t") == "message" and isinstance(rec.get("msg"), dict):
                        messages.append(rec["msg"])
                    continue
                ev = rec.get("event")
                if not isinstance(ev, dict) or ev.get("type") in _SKIP_TRANSCRIPT_TYPES:
                    continue
                if ev.get("type") == "user_message" and first_user is None:
                    first_user = ev
                tail.append(ev)
    except Exception as exc:  # noqa: BLE001
        _log(f"读取转录失败 {path}: {exc}")
        return []
    events = _restore_tool_results(list(tail), messages)
    if first_user is not None and (not events or events[0] is not first_user):
        events.insert(0, first_user)
    return events


def has_user_message(project_dir: str, session_id: str) -> bool:
    """会话是否已存过用户消息（据此判断本次 start 是不是首个回合）。

    逐行扫到第一条用户消息就返回，不为大会话解析整个文件。
    """
    path = SessionStore(project_dir, session_id).path
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if '"user"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict) or rec.get("t") != "message":
                    continue
                msg = rec.get("msg")
                if (
                    isinstance(msg, dict)
                    and msg.get("role") == "user"
                    and str(msg.get("content") or "").strip()
                ):
                    return True
    except Exception as exc:  # noqa: BLE001
        _log(f"检查用户消息失败 {path}: {exc}")
    return False
