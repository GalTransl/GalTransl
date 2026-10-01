"""会话状态与事件的数据结构。"""

from __future__ import annotations

import json
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from GalTransl.Agent.core import DEFAULT_CONTEXT_WINDOW, RUNTIME_EVENT_KEEP
from GalTransl.Agent.permissions import DEFAULT_PERMISSION_MODE


@dataclass(slots=True)
class AgentEvent:
    """单条 Agent 事件，会原样推给前端 SSE。"""

    type: str  # content | content_delta | content_end | reasoning_delta | reasoning_end | user_message | tool_call | tool_result | finish | error | stopped
    step: int
    data: dict[str, Any] = field(default_factory=dict)

    def to_sse(self) -> str:
        payload = {"type": self.type, "step": self.step, **self.data}
        return f"event: agent\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "step": self.step, **self.data}


@dataclass(slots=True)
class PendingMessage:
    """排队中的用户消息（模型还没看到）。

    带 id 是为了让界面上的队列面板能精确地"立即发送/编辑/删除"某一条——
    文本可能重复，不能靠内容定位。队列只在内存里：重启即清空。
    """

    id: str
    text: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "text": self.text}


@dataclass(slots=True)
class AgentState:
    status: str = "idle"  # idle | running | awaiting_input | stopped | failed
    first_prompt: str = ""  # 用户首条输入；仅用于消息初始化及会话信息。
    project_dir: str = ""
    config_file_name: str = ""
    backend_profile_data: dict[str, Any] = field(default_factory=dict)
    # 后端配置名（只存在前端 localStorage，故随 start/message 一起送过来）：
    # backend_profile_name = 本会话在用的那份（Agent 页选中的默认）；
    # translator_* = 翻译任务实际会用的那份（项目选择 → 否则全局默认）。
    # 只用于「了解项目」如实报出实际后端；不落盘，重启后由下一次 message 补上。
    backend_profile_name: str = ""
    translator_profile_name: str = ""
    translator_profile_data: dict[str, Any] = field(default_factory=dict)
    # 权限模式（同样只存在前端 localStorage，随 start/message 送过来）：见 PERMISSION_MODES。
    # 不落盘，重启后由下一次 start/message 补上；拿不到就是默认的「每次询问」。
    permission_mode: str = DEFAULT_PERMISSION_MODE
    # 本会话里用户点过「本会话允许」的工具名（按工具名放行，不跨会话、不落盘）
    permission_grants: set[str] = field(default_factory=set)
    started_at: float = 0.0
    finished_at: float = 0.0
    error: str = ""
    # 长期事件（user_message/tool_call/tool_result/finish/…）：进 deque（maxlen
    # 防泄漏），status 快照与 SSE 回放都从这里取，刷新/重启后不丢。
    events: deque[AgentEvent] = field(default_factory=lambda: deque(maxlen=RUNTIME_EVENT_KEEP))
    # 瞬态事件（content_delta/wait_tick）：量大且只对当前回合的实时流有意义。
    # 单走旁路队列，SSE drain 拉走即弃，不占长期 deque 的 maxlen 窗口——
    # 否则一次长流式就会把 user_message 挤出窗口，刷新后首条消息消失。
    transient_events: deque[AgentEvent] = field(default_factory=lambda: deque(maxlen=512))
    # 保护 events / transient_events / step：回合线程在 _emit 里写，HTTP 线程在
    # status / drain_events 里读。deque 边遍历边追加会抛 "mutated during iteration"。
    event_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    step: int = 0
    # 持久的多轮对话历史（OpenAI messages），跨回合保留，reset 才清空
    messages: list[dict[str, Any]] = field(default_factory=list)
    # 运行中收到的新消息先排队（界面上显示为 composer 上方的队列面板，不进聊天
    # 转录）。语义是"等本轮工作做完再发"：本轮收尾时由 _close_turn 写进历史并
    # 开新回合续跑；用户主动停止则留在队列里等用户决定（不代跑）。
    pending_messages: deque[PendingMessage] = field(default_factory=deque)
    pending_followup: bool = False
    # 「立即」发送的排队消息（用户要求打断当前回合、马上把这条发出去）。
    # 回合收尾时它被当成新回合的第一条消息（其余排队项继续等它们自己的时机）。
    immediate_message: str = ""
    # 本回合的收尾类型，SSE stream 据此判断是否还有后续（awaiting_input 不算终态）
    turn_end: str = ""
    # 会话身份：一个项目下可以有多个会话，互不干扰
    session_id: str = ""
    title: str = ""
    # 上次 LLM 响应的 prompt_tokens，作为上下文用量估算的锚点（0 表示未知）
    last_prompt_tokens: int = 0
    # 锚点对应的历史长度：锚点之后新增的消息要另外估算
    anchored_message_count: int = 0
    # 上下文窗口（token），来自后端配置 contextWindow；界面指示器的分母
    context_window: int = DEFAULT_CONTEXT_WINDOW
    # 从磁盘恢复的会话标记（本次进程内还没跑过回合）
    restored: bool = False
    # 已经发给过模型的 recent_errors 事件 id（get_runtime 的水位线）：同一个错误不该在
    # 每次查询里反复出现、逼模型重新判断"是不是新错误"。只记内存——错误事件本身也在
    # 后端内存里，进程重启后两边一起清空，不会出现"重启后又重复报旧错误"。
    seen_error_ids: set[str] = field(default_factory=set)


class AgentToolError(Exception):
    """工具执行失败。"""
