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
from typing import Any
from uuid import uuid4

# 程序根 = app_settings.json 所在目录，与 AppSettings.py 保持一致
_PROGRAM_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSIONS_ROOT = os.path.join(os.path.dirname(_PROGRAM_ROOT), "agent_sessions")

# 不落盘的事件类型：流式增量高频且能由最终消息重建
_SKIP_EVENT_TYPES = {"content_delta", "reasoning_delta", "wait_tick"}


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
        self._append({"t": "event", "at": time.time(), "event": event})

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
    """只读文件头几行取 meta，避免为列表动作解析整个大文件。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for _ in range(20):
                line = f.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("t") == "meta":
                    return {k: v for k, v in rec.items() if k not in ("t",)}
    except Exception:  # noqa: BLE001
        pass
    return {}


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
    """新建会话文件并写 meta，返回 session_id。"""
    session_id = new_session_id()
    store = SessionStore(project_dir, session_id)
    store.append_meta(
        project_dir=project_dir,
        title=title or session_id,
        created_at=time.time(),
    )
    return session_id


def delete_session(project_dir: str, session_id: str) -> None:
    SessionStore(project_dir, session_id).clear()


def session_exists(project_dir: str, session_id: str) -> bool:
    return os.path.isfile(SessionStore(project_dir, session_id).path)


def next_session_title(project_dir: str, base_name: str) -> str:
    """生成"项目名+序号"标题：扫已有标题里同前缀的数字，取 max+1。

    标题形如 "MyProject123"（项目名 + 递增序号，序号从 1 开始）。
    """
    prefix = base_name or "会话"
    max_seq = 0
    for item in list_sessions(project_dir):
        title = item.get("title") or ""
        if not title.startswith(prefix):
            continue
        suffix = title[len(prefix):]
        if suffix.isdigit():
            max_seq = max(max_seq, int(suffix))
    return f"{prefix}{max_seq + 1}"
