"""AgentRuntime：进程内的会话注册表（启动/消息/停止/恢复/事件读取）。"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Any

from GalTransl.Agent import session_store
from GalTransl.Agent.session_store import SessionStore
from GalTransl.Agent.context import (
    _answers_for_text,
    _dangling_ask_question_texts,
    _dangling_tool_calls,
    _estimate_usage_tokens,
    _permission_decision_text,
    _profile_context_window,
    _restore_compacted_history,
    _resume_context,
    _tools_overhead_tokens,
)
from GalTransl.Agent.core import (
    DEFAULT_BACKEND_HOST,
    DEFAULT_BACKEND_PORT,
    DEFAULT_CONFIG_FILE,
    DEFAULT_CONTEXT_WINDOW,
    RUNTIME_EVENT_KEEP,
    _log,
)
from GalTransl.Agent.models import AgentEvent, AgentState, PendingMessage
from GalTransl.Agent.permissions import _apply_permission_mode, _normalize_permission_mode
from GalTransl.Agent.runner import AgentRunner
from GalTransl.Agent.tool_schemas import AGENT_TOOLS
from GalTransl.Agent.tools.ask import _format_ask_answers


def _initial_session_title(project_dir: str, session_id: str, first_prompt: str, current: str) -> str:
    """首个回合的会话标题：取用户第一条消息；已有用户消息则保留 current。

    新建会话时用户还没输入，create_session 只能给占位标题（「新会话」），
    真正的标题在首条消息到达（start）时才定下来。续聊（会话里已有用户消息）
    不重算，避免把标题改成后续某条消息。
    """
    text = (first_prompt or "").strip()
    if not text or session_store.has_user_message(project_dir, session_id):
        return current
    return session_store.title_from_message(text)


class AgentRuntime:
    """全局 Agent 注册表：一个项目下可以有多个会话，互不干扰。

    数据按 (project_dir, session_id) 组织。同一会话同时只跑一个回合（运行中
    再发消息走插话排队），但不同会话可以各自独立运行。

    会话落盘在 JSONL（session_store），进程重启后 status/drain_events/message
    会按需从磁盘懒加载恢复，所以关掉应用再打开还能接着聊，不用清空重来。
    """

    def __init__(self, host: str = DEFAULT_BACKEND_HOST, port: int = DEFAULT_BACKEND_PORT) -> None:
        self.host = host
        self.port = port
        # project_dir -> session_id -> state / runner / stop_event
        self._states: dict[str, dict[str, AgentState]] = {}
        self._runners: dict[str, dict[str, AgentRunner]] = {}
        self._stop_events: dict[str, dict[str, threading.Event]] = {}
        self._lock = threading.RLock()  # 可重入锁：允许在持锁时调用本类其它方法
        # 排队消息的 id 序号（界面按 id 定位某一条做"立即/编辑/删除"）
        self._queue_seq = 0

    @staticmethod
    def _key(project_dir: str) -> str:
        return os.path.abspath(project_dir)

    # ---- 会话管理 ----

    def list_sessions(self, project_dir: str) -> list[dict[str, Any]]:
        """列出项目下的会话。内存里有状态的优先（可能还没落盘）。

        每项附一个 `status`（无状态时为空串），供侧边栏那个状态灯用：running 亮蓝灯、
        awaiting_input/stopped 亮绿灯、failed 亮橙灯。**只认内存**——落盘的 running
        标记在进程重启后是过期的（没有 runner 在跑了），拿它当"运行中"会让灯一直蓝着。
        """
        items = session_store.list_sessions(project_dir)
        with self._lock:
            states = self._states.get(self._key(project_dir), {})
            for item in items:
                state = states.get(str(item.get("session_id") or ""))
                item["status"] = state.status if state is not None else ""
        return items

    def create_session(self, project_dir: str, title: str = "") -> dict[str, Any]:
        """新建一个空会话（不启动回合）。

        标题缺省为占位「新会话」——此时用户还没输入，等首条消息发出时
        由 start 用这条消息的内容改写（见 _initial_session_title）。
        """
        resolved = title.strip() or session_store.DEFAULT_TITLE
        session_id = session_store.create_session(project_dir, resolved)
        _log(f"新建会话: project={project_dir} session={session_id} title={resolved}")
        store = SessionStore(project_dir, session_id)
        store.append_meta(title=resolved, project_dir=project_dir)
        return {
            "session_id": session_id,
            "title": resolved,
            "created_at": time.time(),
            "updated_at": time.time(),
        }

    def delete_session(self, project_dir: str, session_id: str) -> dict[str, Any]:
        """删除会话：先打断运行中的回合，再清内存与磁盘。"""
        key = self._key(project_dir)
        with self._lock:
            event = self._stop_events.get(key, {}).get(session_id)
            if event:
                event.set()
            runner = self._runners.get(key, {}).get(session_id)
            if runner is not None:
                runner.abort_in_flight()  # 在途请求要立刻断，否则线程还挂在 read 上
                runner.close_store()  # 回合线程收尾时的写入不能把会话文件又建回来
            self._states.get(key, {}).pop(session_id, None)
            self._runners.get(key, {}).pop(session_id, None)
            self._stop_events.get(key, {}).pop(session_id, None)
            session_store.delete_session(project_dir, session_id)
            _log(f"删除会话: project={key} session={session_id}")
        return {"status": "ok", "session_id": session_id}

    def rename_session(self, project_dir: str, session_id: str, title: str) -> dict[str, Any]:
        """改会话标题（当前前端未用，留给后续重命名 UI）。"""
        clean = (title or "").strip()
        if not clean:
            raise ValueError("title is required")
        key = self._key(project_dir)
        with self._lock:
            state = self._states.get(key, {}).get(session_id)
            if state is not None:
                state.title = clean
            store = SessionStore(project_dir, session_id)
            store.append_meta(title=clean, project_dir=project_dir)
        return {"status": "ok", "session_id": session_id, "title": clean}

    # ---- 排队消息（界面队列面板的操作）----
    def _new_pending(self, text: str) -> PendingMessage:
        """生成带唯一 id 的排队条目。id 只需进程内唯一（队列只在内存里）。"""
        self._queue_seq += 1
        return PendingMessage(id=f"q{self._queue_seq}", text=text)

    @staticmethod
    def _pop_queued(state: AgentState, item_id: str) -> PendingMessage | None:
        """按 id 从队列里摘出一条（不存在返回 None）。"""
        for i, item in enumerate(state.pending_messages):
            if item.id == item_id:
                del state.pending_messages[i]
                return item
        return None

    def _queue_ctx(
        self, project_dir: str, session_id: str | None
    ) -> tuple[str, str | None, AgentState | None, AgentRunner | None]:
        """队列操作共用的上下文（state/runner 可能为 None）。"""
        key = self._key(project_dir)
        sid = self._resolve_session_id(project_dir, session_id)
        state: AgentState | None = None
        runner: AgentRunner | None = None
        if sid:
            state = self._states.get(key, {}).get(sid)
            runner = self._runners.get(key, {}).get(sid)
        return key, sid, state, runner

    def queue_delete(self, project_dir: str, item_id: str, session_id: str | None = None) -> dict[str, Any]:
        """删掉一条排队消息（模型还没看到的那条）。"""
        with self._lock:
            _key, sid, state, runner = self._queue_ctx(project_dir, session_id)
            if state is not None and self._pop_queued(state, item_id) is not None:
                _log(f"删除排队消息: session={sid} id={item_id}")
                if runner is not None:
                    runner.emit_queue()
            return self.status(project_dir, sid)

    def queue_update(
        self, project_dir: str, item_id: str, text: str, session_id: str | None = None
    ) -> dict[str, Any]:
        """就地改一条排队消息的文本（位置不变，仍排在原来的次序上）。"""
        clean = (text or "").strip()
        if not clean:
            raise ValueError("排队消息不能为空")
        with self._lock:
            _key, sid, state, runner = self._queue_ctx(project_dir, session_id)
            if state is not None:
                for item in state.pending_messages:
                    if item.id == item_id:
                        item.text = clean
                        if runner is not None:
                            runner.emit_queue()
                        break
            return self.status(project_dir, sid)

    def queue_send(self, project_dir: str, item_id: str, session_id: str | None = None) -> dict[str, Any]:
        """「立即」：打断当前回合，把这条排队消息马上发出去。

        回合在跑 → 摘出队列存进 immediate_message 再 stop()（顺带打断在途请求），
        回合线程收尾时把它作为新回合的第一条消息落历史、发 user_message；
        其余排队项继续排队，等它们自己的时机（下次本轮收尾）。
        回合已结束 → 直接当普通消息开新回合。
        """
        with self._lock:
            _key, sid, state, runner = self._queue_ctx(project_dir, session_id)
            if state is None:
                return self.status(project_dir, sid)
            item = self._pop_queued(state, item_id)
            if item is None:
                return self.status(project_dir, sid)
            _log(f"立即发送排队消息: session={sid} id={item_id} msg={item.text[:60]}")
            if state.status == "running":
                state.immediate_message = item.text
                if runner is not None:
                    runner.emit_queue()
                self.stop(project_dir, sid)
            else:
                self.message(project_dir, item.text, sid)
            return self.status(project_dir, sid)

    def _resolve_session_id(self, project_dir: str, session_id: str | None) -> str | None:
        """缺省 session_id 时取最近活跃的会话（兼容旧前端只传项目）。"""
        if session_id:
            return session_id
        sessions = self.list_sessions(project_dir)
        if sessions:
            return sessions[0]["session_id"]
        with self._lock:
            states = self._states.get(self._key(project_dir)) or {}
            if states:
                return next(iter(states))
        return None

    def _restore(self, project_dir: str, session_id: str) -> AgentState | None:
        """从磁盘懒加载一个会话到内存（进程重启后恢复用）。"""
        store = SessionStore(project_dir, session_id)
        data = store.load()
        meta = data.get("meta") or {}
        messages = data.get("messages") or []
        events = data.get("events") or []
        if not messages and not events:
            return None
        # 文件里带的是压缩**前**的全量（只追加），按最后一次压缩的摘要重建出真正在用的那份
        history = _restore_compacted_history(messages)
        # 事件按 step 重建 deque（保留最近 RUNTIME_EVENT_KEEP 条）
        ev_deque: deque[AgentEvent] = deque(maxlen=RUNTIME_EVENT_KEEP)
        max_step = 0
        for raw in events:
            try:
                step = int(raw.get("step", 0))
                etype = str(raw.get("type", ""))
            except (TypeError, ValueError, AttributeError):
                continue
            data_fields = {k: v for k, v in raw.items() if k not in ("type", "step")}
            ev_deque.append(AgentEvent(type=etype, step=step, data=data_fields))
            max_step = max(max_step, step)

        # 首条用户输入同时存在于 meta.first_prompt / messages 和 user_message 事件中。
        # 旧版本、异常退出或事件窗口裁剪可能只留下前两者；恢复时补一条内存事件，
        # 否则模型回答能恢复，用户的第一条气泡却会消失。step 放在现有事件之前，
        # 不改变后续事件编号，也不写回磁盘，避免恢复过程重复追加记录。
        initial_text = session_store.first_prompt_from_meta(meta).strip()
        if not initial_text:
            for message in messages:
                if isinstance(message, dict) and message.get("role") == "user":
                    candidate = str(message.get("content") or "").strip()
                    if candidate:
                        initial_text = candidate
                        break
        has_initial_event = any(
            event.type == "user_message" and str(event.data.get("message") or "").strip() == initial_text
            for event in ev_deque
        )
        if initial_text and not has_initial_event:
            first_step = min((event.step for event in ev_deque), default=1)
            ev_deque.appendleft(AgentEvent(
                type="user_message",
                step=max(0, first_step - 1),
                data={"message": initial_text},
            ))

        # 上次是运行中 -> 进程重启把回合中断了，标记为 stopped
        was_running = bool(meta.get("running"))
        state = AgentState(
            status="stopped" if was_running else "awaiting_input",
            first_prompt=session_store.first_prompt_from_meta(meta) or initial_text,
            project_dir=project_dir,
            config_file_name=str(meta.get("config_file_name") or DEFAULT_CONFIG_FILE),
            backend_profile_data=meta.get("backend_profile_data") or {},
            started_at=float(meta.get("created_at") or 0.0),
            error="上次运行被应用重启中断" if was_running else "",
            messages=history,
            step=max_step,
            session_id=session_id,
            title=str(meta.get("title") or session_id),
            context_window=int(meta.get("context_window") or DEFAULT_CONTEXT_WINDOW),
            restored=True,
        )
        state.events = ev_deque
        if was_running:
            # 给中断的会话补一条可见提示，用户继续发消息即可接着干
            state.events.append(AgentEvent(
                type="stopped",
                step=max_step + 1,
                data={"reason": "上次运行被应用重启中断，发消息即可继续"},
            ))
            state.step = max_step + 1
        key = self._key(project_dir)
        with self._lock:
            self._states.setdefault(key, {})[session_id] = state
        compacted_note = "" if len(history) == len(messages) else f"（另有 {len(messages) - len(history)} 条已压缩）"
        _log(
            f"从磁盘恢复会话: project={key} session={session_id} "
            f"messages={len(history)}{compacted_note} events={len(ev_deque)}"
        )
        return state

    def _get_state(self, project_dir: str, session_id: str | None) -> AgentState | None:
        """取会话状态：内存优先，没有则尝试从磁盘恢复。"""
        sid = self._resolve_session_id(project_dir, session_id)
        if sid is None:
            return None
        key = self._key(project_dir)
        with self._lock:
            state = self._states.get(key, {}).get(sid)
        if state is not None:
            return state
        return self._restore(project_dir, sid)

    def start(
        self,
        project_dir: str,
        config_file_name: str,
        backend_profile_data: dict[str, Any],
        first_prompt: str = "",
        session_id: str | None = None,
        host: str | None = None,
        port: int | None = None,
        backend_profile_name: str = "",
        translator_profile_name: str = "",
        translator_profile_data: dict[str, Any] | None = None,
        permission_mode: str = "",
    ) -> dict[str, Any]:
        """启动一个回合。session_id 为空时新建会话；标题取用户第一条消息。"""
        key = self._key(project_dir)
        with self._lock:
            sid = self._resolve_session_id(project_dir, session_id) if session_id else None
            if sid is None:
                session = self.create_session(project_dir)
                sid = session["session_id"]
                title = session["title"]
            else:
                existing = self._states.get(key, {}).get(sid)
                if existing and existing.status == "running":
                    _log(f"启动被拒：该会话已有回合在运行 -> {key}/{sid}")
                    raise ValueError("该会话已有回合在运行")
                title = existing.title if existing else session_store.session_title(project_dir, sid)
            # 会话标题 = 用户第一条消息（新建的空会话此时才拿到）
            title = _initial_session_title(project_dir, sid, first_prompt, title)

            stop_event = threading.Event()
            state = AgentState(
                status="running",
                first_prompt=first_prompt,
                project_dir=project_dir,
                config_file_name=config_file_name or DEFAULT_CONFIG_FILE,
                backend_profile_data=backend_profile_data or {},
                backend_profile_name=backend_profile_name,
                translator_profile_name=translator_profile_name,
                translator_profile_data=translator_profile_data or {},
                # 权限模式只在前端 localStorage，随 start/message 送过来（拿不到就是默认档）
                permission_mode=_normalize_permission_mode(permission_mode),
                started_at=time.time(),
                session_id=sid,
                title=title,
                # 窗口在建会话时就定下来：界面指示器不用等第一轮请求
                context_window=_profile_context_window(backend_profile_data),
            )
            runner = AgentRunner(
                state, host=host if host is not None else self.host,
                port=port if port is not None else self.port, stop_event=stop_event, registry=self,
            )
            self._states.setdefault(key, {})[sid] = state
            self._runners.setdefault(key, {})[sid] = runner
            self._stop_events.setdefault(key, {})[sid] = stop_event
            # meta 落盘：记录会话身份与运行标记，重启后据此恢复。
            # backend_profile_data（含 token）不落盘，只存窗口大小这一个整数。
            if runner._store is not None:
                runner._store.append_meta(
                    project_dir=project_dir,
                    title=title,
                    first_prompt=first_prompt,
                    config_file_name=state.config_file_name,
                    context_window=state.context_window,
                    created_at=state.started_at,
                    running=True,
                )
            thread = threading.Thread(target=runner.run, name=f"agent-{os.path.basename(key)}", daemon=True)
            thread.start()
            _log(f"Agent 回合已启动: project={key} session={sid} config={config_file_name} first_prompt={first_prompt[:60]}")
            return self.status(project_dir, sid)

    def message(
        self,
        project_dir: str,
        message: str,
        session_id: str | None = None,
        backend_profile_name: str = "",
        backend_profile_data: dict[str, Any] | None = None,
        translator_profile_name: str = "",
        translator_profile_data: dict[str, Any] | None = None,
        permission_mode: str = "",
    ) -> dict[str, Any]:
        """向会话追加一条用户消息。

        - 会话不存在（内存与磁盘都没有）：报错（前端应先 start/create）。
        - 正在运行：进队列（界面显示在 composer 上方的队列面板里），**不发**
          user_message 事件；等本轮工作做完（模型不再调工具、给出最终回复）由
          收尾流程写进历史并开新回合。想提前发就用 queue_send（「立即」，会打断
          当前回合）。
        - 已结束（awaiting_input / stopped / failed 等旧状态，含重启恢复的）：
          追加历史并开新回合继续跑。
        """
        text = (message or "").strip()
        key = self._key(project_dir)
        with self._lock:
            sid = self._resolve_session_id(project_dir, session_id)
            state = self._states.get(key, {}).get(sid) if sid else None
            if state is None:
                state = self._restore(project_dir, sid) if sid else None
            if state is None or not state.messages:
                raise ValueError("该项目还没有 Agent 会话，请先发送第一条消息启动")
            # 会话存在说明 sid 必然有值（state 就是按 sid 取到的），这里显式收窄
            assert sid is not None
            # 后端上下文跟着最新一次的 start/message 走：用户可能中途改了默认配置
            # （名字只在前端 localStorage，后端只能这样拿到）。
            if backend_profile_name:
                state.backend_profile_name = backend_profile_name
            if backend_profile_data:
                state.backend_profile_data = backend_profile_data
            if translator_profile_name:
                state.translator_profile_name = translator_profile_name
            if translator_profile_data:
                state.translator_profile_data = translator_profile_data
            # 权限模式同理：前端改了选择就跟着走，下一次工具调用按新模式判；
            # 与选择器那条路径（set_permission_mode）共用一套规则，真换了档就清空放行记录
            if permission_mode:
                _apply_permission_mode(state, permission_mode)

            if state.status == "running":
                # 排队期间只算"待发"：不进历史、也不发 user_message 事件——界面上
                # 显示在 composer 上方的队列面板里（emit_queue）。真正被模型看到时
                # 才由消费点补发事件（run 的注入点 / _close_turn）。
                state.pending_messages.append(self._new_pending(text))
                runner = self._runners.get(key, {}).get(sid)
                if runner is not None:
                    runner.emit_queue()
                _log(f"Agent 运行中，消息已排队: session={sid} msg={text[:60]}")
                return self.status(project_dir, sid)

            # 回合已结束（可能是重启后恢复的）
            if state.pending_followup:
                state.pending_followup = False
            runner = self._runners.get(key, {}).get(sid)
            if runner is None:
                stop_event = threading.Event()
                runner = AgentRunner(state, host=self.host, port=self.port, stop_event=stop_event, registry=self)
                self._runners.setdefault(key, {})[sid] = runner
                self._stop_events.setdefault(key, {})[sid] = stop_event
            else:
                stop_event = threading.Event()
                runner.stop_event = stop_event
                self._stop_events.setdefault(key, {})[sid] = stop_event
            # 上一轮在工具执行中途退出（进程被杀 / 用户点了停止）会留下"没结果"的卡片：
            # 先收掉，而且**必须排在 user_message 前面**——反过来的话前端在转录里找不到
            # 原来的工具行，只能新建一段挂到新消息后面，看着像刚刚出错（同一处说明见
            # _close_dangling_before_message；那条路径在锁外，这里已经在锁里，直接叫 runner）。
            runner._close_dangling_tool_calls()
            runner._persist_message({"role": "user", "content": text})

            state.status = "running"
            state.error = ""
            state.finished_at = 0.0
            state.turn_end = ""
            runner._emit("user_message", {"message": text})
            thread = threading.Thread(target=runner.run, name=f"agent-{os.path.basename(key)}", daemon=True)
            thread.start()
            _log(f"Agent 继续回合: session={sid} msg={text[:60]}")
            return self.status(project_dir, sid)

    def answer_ask(
        self,
        project_dir: str,
        session_id: str | None,
        answers: Any,
        backend_profile_name: str = "",
        backend_profile_data: dict[str, Any] | None = None,
        translator_profile_name: str = "",
        translator_profile_data: dict[str, Any] | None = None,
        permission_mode: str = "",
    ) -> dict[str, Any]:
        """把用户对 ask_user 提问的回答送回去，唤醒正在等待的那个回合。

        校验（题数一致、单选不许给多个值）在回合线程里的 resolve_ask 做；这里只负责
        找到对应的 runner。

        **找不到在等的询问时不报错**：最常见的成因是"卡在提问上时用户关了程序再打开"
        ——后端那个回合已经没了（内存里的 runner 没有了），卡片却还在转录里（前端从落盘
        事件重建出来的），照直报错等于让用户白选一次。这时把这次选择当成一条**用户消息**
        接着聊（见 _answer_ask_as_message），行为与用户自己把选择打出来一样。

        重启后这条路上要**另起一个回合**，而 token 只在请求里（内存里的会话状态不落盘），
        所以前端上下文要随这次答复一起带上来（同 /api/agent/message）。
        """
        context = _resume_context(
            backend_profile_name=backend_profile_name,
            backend_profile_data=backend_profile_data,
            translator_profile_name=translator_profile_name,
            translator_profile_data=translator_profile_data,
            permission_mode=permission_mode,
        )
        key = self._key(project_dir)
        sid = self._resolve_session_id(project_dir, session_id)
        with self._lock:
            runner = self._runners.get(key, {}).get(sid) if sid else None
        if runner is not None:
            try:
                return runner.resolve_ask(answers)
            except ValueError:
                # 没有在等的询问（已作答 / 回合已结束 / 重启后重建的卡片）
                _log(f"❓ ask_user 没有在等的询问，改为按用户消息继续: session={sid}")
        else:
            _log(f"❓ ask_user 对应的回合不在内存里（多半是重启过），改为按用户消息继续: session={sid}")
        return self._answer_ask_as_message(project_dir, sid, answers, context)

    def _answer_ask_as_message(
        self, project_dir: str, sid: str | None, answers: Any, context: dict[str, Any]
    ) -> dict[str, Any]:
        """没有在等的 ask_user：把这次选择拼成一条用户消息，开新回合继续。

        题目从历史里那次残缺的 ask_user 调用上取回（取不到就用「第 N 题」占位），
        渲染口径与工具结果一致（见 _format_ask_answers）。
        """
        state = self._get_state(project_dir, sid) if sid else None
        if state is None:
            raise ValueError("该项目还没有 Agent 会话，请先发送第一条消息启动")
        self._close_dangling_before_message(project_dir, state)
        picked = _answers_for_text(answers)
        questions = [{"question": text} for text in _dangling_ask_question_texts(state.messages)] or [
            {"question": f"第 {index + 1} 题"} for index in range(len(picked))
        ]
        body = _format_ask_answers(questions, picked)
        status = self.message(project_dir, f"回答上面的提问：\n{body}", sid, **context)
        return {**status, "ok": True, "answers": answers, "resumed_as_message": True}

    def _close_dangling_before_message(self, project_dir: str, state: AgentState) -> None:
        """先把界面上那些"没结果"的工具卡片收掉，再发用户消息。

        顺序要紧：user_message 在转录里会另起一段（前端 closeActivity），反过来的话这条
        tool_result 就找不到原来的工具行、只能新建一段挂到最后面去了。
        """
        if not _dangling_tool_calls(state.messages):
            return
        self._runner_for_state(project_dir, state)._close_dangling_tool_calls()

    def _runner_for_state(self, project_dir: str, state: AgentState) -> AgentRunner:
        """取本会话的 runner（没有就建一个）。**不**动 stop_event——这里只是要发事件，
        真正的回合由随后的 message() 启动（它会换一个新的 stop_event）。"""
        key = self._key(project_dir)
        sid = state.session_id
        with self._lock:
            runner = self._runners.get(key, {}).get(sid)
            if runner is None:
                runner = AgentRunner(state, host=self.host, port=self.port, registry=self)
                self._runners.setdefault(key, {})[sid] = runner
        return runner

    def answer_permission(
        self,
        project_dir: str,
        session_id: str | None,
        decision: Any,
        reason: Any = "",
        backend_profile_name: str = "",
        backend_profile_data: dict[str, Any] | None = None,
        translator_profile_name: str = "",
        translator_profile_data: dict[str, Any] | None = None,
        permission_mode: str = "",
    ) -> dict[str, Any]:
        """把用户对权限审批卡的答复送回去，唤醒正在等待的那个回合。

        decision 只有 allow-once / allow-session / deny 三种；reason 是拒绝时可选的
        一句话（随工具结果给模型看）。校验在 resolve_permission 里做，这里只负责找到
        对应的 runner。

        与 answer_ask 同一套兜底：**没有在等的审批时不报错**，而是把这次决定当成一条
        用户消息接着聊（卡片对应的回合已经结束、或重启后只剩一张重建出来的卡片）。
        前端上下文同理要一起带上来——那条路要另起回合。
        """
        context = _resume_context(
            backend_profile_name=backend_profile_name,
            backend_profile_data=backend_profile_data,
            translator_profile_name=translator_profile_name,
            translator_profile_data=translator_profile_data,
            permission_mode=permission_mode,
        )
        key = self._key(project_dir)
        sid = self._resolve_session_id(project_dir, session_id)
        with self._lock:
            runner = self._runners.get(key, {}).get(sid) if sid else None
        if runner is not None:
            try:
                return runner.resolve_permission(decision, reason)
            except ValueError:
                _log(f"🔐 审批没有在等的请求，改为按用户消息继续: session={sid}")
        else:
            _log(f"🔐 审批对应的回合不在内存里（多半是重启过），改为按用户消息继续: session={sid}")
        return self._answer_permission_as_message(project_dir, sid, decision, reason, context)

    def _answer_permission_as_message(
        self,
        project_dir: str,
        sid: str | None,
        decision: Any,
        reason: Any,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """没有在等的审批：把这次决定拼成一条用户消息，开新回合继续。"""
        state = self._get_state(project_dir, sid) if sid else None
        if state is None:
            raise ValueError("该项目还没有 Agent 会话，请先发送第一条消息启动")
        self._close_dangling_before_message(project_dir, state)
        name = ""
        for item in reversed(_dangling_tool_calls(state.messages)):
            name = str(item["name"])
            break
        text = _permission_decision_text(str(decision or ""), name, str(reason or ""))
        status = self.message(project_dir, text, sid, **context)
        return {
            **status,
            "ok": True,
            "decision": str(decision or ""),
            "name": name,
            "reason": str(reason or ""),
            "resumed_as_message": True,
        }

    def set_permission_mode(
        self, project_dir: str, session_id: str | None, mode: Any
    ) -> dict[str, Any]:
        """随时改权限模式：回合跑着也能改，下一次工具调用就按新档判。

        与 answer_* 不同，这里不要求"有东西在等"——空闲会话也能改（前端改完本地也存了，
        下次 start/message 照样带上，两边一致）；只在会话根本不存在时报 ValueError。
        顺带处理掉"已经弹出来的那张卡"：新档本来就会放行它的话直接放行（见
        AgentRunner.apply_permission_mode），用户不必再点一次。

        换档会清空「本会话允许」的放行记录（_apply_permission_mode）。
        """
        key = self._key(project_dir)
        sid = self._resolve_session_id(project_dir, session_id)
        # 内存优先、没有则从磁盘恢复（刚打开的历史会话）：改档不值得逼用户先发一条消息。
        # 没有任何会话（连文件都没有）时 _get_state 给 None，按错误返回。
        state = self._get_state(project_dir, sid)
        if state is None:
            raise ValueError("该项目还没有 Agent 会话")
        with self._lock:
            runner = self._runners.get(key, {}).get(sid) if sid else None
        if runner is not None:
            runner.apply_permission_mode(mode)  # state 是同一份，顺带看有没有在等的卡
        else:
            _apply_permission_mode(state, mode)
        _log(f"权限模式改为 {state.permission_mode}（session={sid}）")
        return {"ok": True, "permission_mode": state.permission_mode, "session_id": sid or ""}

    def stop(self, project_dir: str, session_id: str | None = None) -> dict[str, Any]:
        """用户点停止：置位信号 + 打断在途请求，收尾仍由回合线程自己做。"""
        key = self._key(project_dir)
        with self._lock:
            sid = self._resolve_session_id(project_dir, session_id)
            event = self._stop_events.get(key, {}).get(sid) if sid else None
            if event:
                event.set()
            # 只置位事件不够：请求可能正阻塞在 socket read 上，主循环根本没机会看
            # 信号（这正是以前"点了停止要等 1-2 分钟才收尾"的原因）。主动打断在途请求，
            # 停止才能秒级生效。
            state = self._states.get(key, {}).get(sid) if sid else None
            runner = self._runners.get(key, {}).get(sid) if sid else None
            if runner is not None and (state is None or state.status == "running"):
                runner.abort_in_flight()
            # 不直接改状态：回合线程自己收尾落 stopped，并消费排队中的插话。
            # 期间 status 保持 running，此时到达的 message() 会走插话路径，
            # 最终由 followup 消费。
        return self.status(project_dir, sid)

    def reset(self, project_dir: str, session_id: str | None = None) -> dict[str, Any]:
        """清空该会话：停掉运行中的回合并丢弃全部历史（内存 + 磁盘）。"""
        key = self._key(project_dir)
        with self._lock:
            sid = self._resolve_session_id(project_dir, session_id)
            event = self._stop_events.get(key, {}).get(sid) if sid else None
            if event:
                event.set()
            if sid:
                runner = self._runners.get(key, {}).get(sid)
                if runner is not None:
                    runner.abort_in_flight()  # 同 stop()：在途请求要立刻断
                    runner.close_store()  # 同 delete_session：别让收尾写入复活会话文件
                self._states.get(key, {}).pop(sid, None)
                self._runners.get(key, {}).pop(sid, None)
                self._stop_events.get(key, {}).pop(sid, None)
                session_store.delete_session(project_dir, sid)
            _log(f"Agent 会话已重置: project={key} session={sid}")
        return {"status": "idle", "project_dir": project_dir, "session_id": sid or "", "step": 0, "events": []}

    def _begin_followup(self, project_dir: str, session_id: str) -> None:
        """回合收尾发现滞留插话时，由后台线程调用：开新回合消费。"""
        key = self._key(project_dir)
        with self._lock:
            state = self._states.get(key, {}).get(session_id)
            runner = self._runners.get(key, {}).get(session_id)
            if state is None or runner is None or not state.pending_followup:
                return
            if state.status == "running":
                return  # 已有新回合在跑（例如用户又手动发了消息）
            state.pending_followup = False
            state.status = "running"
            state.finished_at = 0.0
            state.turn_end = ""
            stop_event = threading.Event()
            runner.stop_event = stop_event
            self._stop_events.setdefault(key, {})[session_id] = stop_event
            thread = threading.Thread(target=runner.run, name=f"agent-{os.path.basename(key)}", daemon=True)
            thread.start()
            _log(f"Agent followup 回合已启动: project={key} session={session_id}")

    def status(self, project_dir: str, session_id: str | None = None) -> dict[str, Any]:
        sid = self._resolve_session_id(project_dir, session_id)
        if sid is None:
            return {"status": "idle", "project_dir": project_dir, "session_id": "", "events": [], "step": 0}
        state = self._get_state(project_dir, sid)
        if state is None:
            return {"status": "idle", "project_dir": project_dir, "session_id": sid, "events": [], "step": 0}
        with self._lock:
            runner = (self._runners.get(self._key(project_dir)) or {}).get(sid)
            return {
                "status": state.status,
                "project_dir": state.project_dir,
                "session_id": state.session_id,
                "title": state.title,
                "first_prompt": state.first_prompt,
                "step": state.step,
                "started_at": state.started_at,
                "finished_at": state.finished_at,
                "error": state.error,
                # 正在生成的助手消息（进行中）。与 events 里的已提交记录分离：
                # 它就是"进行中的助手消息"快照，刷新/切会话时据此把半条消息补上。
                "streaming": runner.live_streaming() if runner is not None else None,
                # 上下文用量：界面指示器的兜底来源（实时更新走 context_usage 事件）。
                # 打开页面/刷新时按当前历史现场估算，不依赖历史事件回放。
                # 口径必须和 _emit_context_usage 一致（都含 tools schema 那笔固定开销），
                # 否则刷新前后指示器会跳一下。
                "context": {
                    "used_tokens": _estimate_usage_tokens(
                        state.messages,
                        state.last_prompt_tokens,
                        state.anchored_message_count,
                        _tools_overhead_tokens(AGENT_TOOLS),
                    ),
                    "window_tokens": state.context_window,
                },
                # 排队中的消息：队列面板的数据源（实时变更走 queue 事件）
                "queued": [m.to_dict() for m in state.pending_messages],
                "events": self._snapshot_events(state),
            }

    @staticmethod
    def _snapshot_events(state: AgentState) -> list[dict[str, Any]]:
        with state.event_lock:
            return [e.to_dict() for e in state.events]

    def transcript(self, project_dir: str, session_id: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        """会话转录：已提交事件的完整回放（从会话日志读）。

        界面重建转录的权威来源。内存里的 events deque 只有 RUNTIME_EVENT_KEEP 条，
        长期会话会缺头；这里读日志，并把首条用户消息当锚点保住。
        """
        sid = self._resolve_session_id(project_dir, session_id)
        if sid is None:
            return []
        events = session_store.read_transcript(
            project_dir, sid, limit or session_store.TRANSCRIPT_MAX_EVENTS
        )
        # 旧版本/异常退出可能没落下首条 user_message 事件：用 meta.first_prompt 补一条，
        # 否则刷新后用户的第一句就没了（与 _restore 的补救口径一致）。
        if not any(ev.get("type") == "user_message" for ev in events):
            first_prompt = session_store.first_prompt_from_meta(session_store.read_meta(project_dir, sid)).strip()
            if first_prompt:
                events.insert(0, {"type": "user_message", "step": 0, "message": first_prompt})
        return events

    def drain_events(self, project_dir: str, after_step: int = 0, session_id: str | None = None) -> list[dict[str, Any]]:
        """取 after_step 之后的所有事件，供 SSE 增量推送。

        合并长期 deque 与瞬态旁路（content_delta/wait_tick），按 step 排序输出；
        瞬态事件被取走即从旁路清除（实时流专用，不参与回放）。"""
        sid = self._resolve_session_id(project_dir, session_id)
        if sid is None:
            return []
        state = self._get_state(project_dir, sid)
        if state is None:
            return []
        with self._lock, state.event_lock:
            out: list[dict[str, Any]] = []
            keep_transient: deque[AgentEvent] = deque(maxlen=512)
            while state.transient_events:
                ev = state.transient_events.popleft()
                if ev.step > after_step:
                    out.append(ev.to_dict())
                # <= after_step 的是上一条流已回放过的，直接丢弃
                elif state.status == "running":
                    keep_transient.append(ev)
            # 仍 running 时未被消费的瞬态事件放回（客户端断线重订的场景），
            # awaiting_input 等终态时旁路清空，避免跨回合残留
            if state.status == "running":
                keep_transient.extend(state.transient_events)
            state.transient_events.clear()
            state.transient_events.extend(keep_transient)
            out.extend(e.to_dict() for e in state.events if e.step > after_step)
            out.sort(key=lambda e: e.get("step", 0))
            return out
