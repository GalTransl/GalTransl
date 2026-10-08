"""AgentRunner：一个会话的回合主循环（LLM 流式请求、工具调度、审批、压缩）。"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
import traceback
from typing import Any, TYPE_CHECKING

from GalTransl.Agent.llm_backend import SubagentBackendView, resolve_llm_backend, summarize_messages
from GalTransl.Agent.session_store import SessionStore
from GalTransl.Agent.backend_http import _encode_project_id, _http_json
from GalTransl.Agent.context import (
    _DANGLING_TOOL_NOTE,
    _apply_prompt_cache,
    _assistant_parts,
    _dangling_tool_calls,
    _estimate_message_tokens,
    _estimate_usage_tokens,
    _find_compaction_cut,
    _history_fingerprint,
    _is_internal_message,
    _keep_recent_tokens,
    _local_fallback_summary,
    _messages_with_tool_placeholders,
    _reasoning_echo,
    _render_archive_messages,
    _sanitize_tool_args,
    _strip_internal_fields,
    _tools_overhead_tokens,
)
from GalTransl.Agent.core import (
    AgentStopRequested,
    COMPACT_ARCHIVE_MAX_LISTED,
    COMPACT_ARCHIVE_NAME,
    COMPACT_TRIGGER_RATIO,
    CONTEXT_RESERVE_TOKENS,
    DEFAULT_BACKEND_HOST,
    DEFAULT_BACKEND_PORT,
    DEFAULT_CONTEXT_WINDOW,
    LLM_MAX_RETRIES,
    MAX_STEPS,
    REASONING_FIELD_NAMES,
    _ContextOverflow,
    _SUBAGENT_STEP_EVENTS,
    _TRANSIENT_EVENT_TYPES,
    _classify_llm_error,
    _is_unsupported_param_error,
    _llm_retry_delay_ms,
    _log,
    _requested_reasoning_field,
)
from GalTransl.Agent.handlers import _RETIRED_TOOLS, _TOOL_HANDLERS, _attach_reason
from GalTransl.Agent.models import AgentEvent, AgentToolError
from GalTransl.Agent.permissions import (
    PERMISSION_DECISIONS,
    PERMISSION_WAIT_TICK,
    _apply_permission_mode,
    _normalize_permission_mode,
    _normalize_permission_reason,
    _permission_denied_reason,
    _permission_needed,
    _permission_tool_label,
    _tool_risk,
)
from GalTransl.Agent.prompts import (
    COMPACT_INSTRUCTION_PROMPT,
    _build_system_prompt,
    _parse_compact_summary,
    _parse_compact_topics,
)
from GalTransl.Agent.tool_schemas import AGENT_TOOLS
from GalTransl.Agent.tools.ask import ASK_WAIT_TICK, _normalize_ask_answers
from GalTransl.Agent.tools.preview import _preview_tool_changes
from GalTransl.Agent.tools.render_md import _render_tool_result_table, _tool_result_json

if TYPE_CHECKING:
    from GalTransl.Agent.models import AgentState, PendingMessage
    from GalTransl.Agent.registry import AgentRuntime


class AgentRunner:
    """单次 Agent 运行。在独立线程内执行 run()。"""

    def __init__(
        self,
        state: AgentState,
        host: str = DEFAULT_BACKEND_HOST,
        port: int = DEFAULT_BACKEND_PORT,
        stop_event: threading.Event | None = None,
        registry: "AgentRuntime | None" = None,
    ) -> None:
        self.state = state
        self.base_url = f"http://{host}:{port}"
        self.stop_event = stop_event or threading.Event()
        self._registry = registry
        self._subagent_clients: list[Any] = []
        self._clients_lock = threading.Lock()
        self._openai_client: Any = None
        self._model: str = ""
        self._context_window = DEFAULT_CONTEXT_WINDOW
        self._compacted_this_turn = False
        # 进行中的压缩（Insert-then-Compress）：指令已挂在历史末尾、等这一轮请求
        # 回摘要。字段见 _begin_compaction，收尾后置空。
        self._pending_compaction: dict[str, Any] | None = None
        # 溢出恢复（400 context too long → 强制压缩 + 重试）每回合只做一次，
        # 压完还超限说明是别的问题（比如工具 schema 本身太长），别再空转。
        self._overflow_recovery_used = False
        # 本回合压缩失败过（两条路径都没成）就不再重试：否则主循环每轮都会
        # "注入指令 → 失败 → 回滚 → 再注入"，一路空转到 MAX_STEPS。
        self._compact_failed_this_turn = False
        # 是否给请求注入 cache_control 断点（见 _apply_prompt_cache）：
        # Anthropic 系需要显式断点，OpenAI 兼容的多数实现是服务端自动前缀缓存。
        self._prompt_caching = False
        # 压缩请求期间把流式增量静音：摘要内容不该以"助手正文"的形式刷到界面上。
        self._stream_quiet = False
        # thinking 模式（DeepSeek 等）下要回传给 provider 的思考字段名：流里见到过就记下来，
        # 之后每条 assistant 消息都带回去（带 tools 的请求不回传会 400）。空 = 还没见过，
        # 这时不往历史里塞这个字段，免得给不认它的 provider 添乱。
        self._reasoning_field: str = ""
        # 正在生成中的助手消息累加器（进行中消息快照）：引用流式期间
        # 那几份 list/dict，响应落定后由 _take_stream_acc 取走并置空。
        self._stream_acc: dict[str, Any] | None = None
        # 正在执行的工具调用 id（工具事件带上它，界面才能挂到对应行上）
        self._active_tool_call_id = ""
        # ask_user 的挂起询问：{request_id, tool_call_id, questions, answers, event}。
        # HTTP 线程（answer_ask）会写 answers 并 set(event)，回合线程在这里等。
        self._ask_lock = threading.Lock()
        self._pending_ask: dict[str, Any] | None = None
        # 权限审批的挂起请求：{request_id, tool_call_id, name, risk, arguments, decision, event}。
        # 与 ask_user 同一套「回合线程挂起、HTTP 线程唤醒」，都不设超时；唯一差别是回合被
        # 停止时这里按拒绝收尾（"停下"就是别做了），而 ask_user 按跳过返回。
        self._perm_lock = threading.Lock()
        self._pending_permission: dict[str, Any] | None = None
        # 会话落盘器：state 里没有 session_id（理论上不该发生）时退化为内存态
        self._store = SessionStore(state.project_dir, state.session_id) if state.session_id else None
        # 本进程里已经补过失败事件的"没结果的工具调用"（见 _close_dangling_tool_calls）：
        # 避免每个回合开跑都重复补一遍
        self._closed_tool_calls: set[str] = set()

    def _write_running_meta(self, running: bool) -> None:
        """在注册表锁内写 running 标记。

        清标记（False）时若状态已是 running，说明收尾之后用户马上发了消息、新回合
        已经开跑——旧线程这次迟到的写入会把新回合的标记盖掉，所以跳过。
        """
        if self._store is None:
            return
        registry = getattr(self, "_registry", None)
        with registry._lock if registry is not None else contextlib.nullcontext():
            if not running and self.state.status == "running":
                return
            self._store.append_meta(running=running)

    def close_store(self) -> None:
        """会话被删除/重置：本 runner 之后不再落盘。"""
        if self._store is not None:
            self._store.close()

    # ---- 事件 ----
    def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        with self.state.event_lock:
            self.state.step += 1
            event = AgentEvent(type=event_type, step=self.state.step, data=data)
            if event_type in _TRANSIENT_EVENT_TYPES:
                # 瞬态事件只给实时流（SSE drain 即取即弃），不进长期窗口
                self.state.transient_events.append(event)
                # 子代理的逐步活动是例外：不进内存窗口，但要落盘——切页/刷新后
                # 前端重建转录时要靠它还原子代理的动作（见 _SUBAGENT_STEP_EVENTS）。
                if event_type in _SUBAGENT_STEP_EVENTS and self._store is not None:
                    self._store.append_event(event.to_dict())
                return
            self.state.events.append(event)
            if self._store is not None:
                self._store.append_event(event.to_dict())

    def _persist_message(self, message: dict[str, Any]) -> None:
        """把一条消息追加进历史并落盘。所有 message append 都走这里。"""
        self.state.messages.append(message)
        if self._store is not None:
            self._store.append_message(message)

    def _close_dangling_tool_calls(self) -> None:
        """给"没等到结果"的工具调用补一条失败事件，把界面上那张卡片收掉。

        典型场景：卡在 ask_user / 权限审批上时用户关掉了程序。历史里那个 tool_calls 是残缺的
        （见 _dangling_tool_calls）——请求侧会给它补占位结果让 provider 收下，但**界面**上那张
        卡片还原样挂着，用户点提交只会被后端告知"没有在等的问题"。这里补一条 tool_result 事件，
        卡片就此收掉（事件会落盘，刷新/重开也不会再冒出来）。

        同一条调用只补一次。集合记在 runner 上，但重启后 runner 是新的、集合是空的——这时
        先去落盘事件里认一下"这条是不是上个进程已经收过了"（见 SessionStore.reported_tool_calls）：
        历史里它永远是残缺的（占位结果只补在请求侧、不写回历史），光看历史分不出"还没来得及收"
        和"已经收过、事件也落盘了"。不认的话重启后每开一个新回合都会再补一条——而这条会落在
        新用户消息**之后**，前端找不到原来的工具行，只能新建一段挂在最后，看着像刚刚出错
        （实际是几轮前的老账）。
        """
        dangling = _dangling_tool_calls(self.state.messages)
        if not dangling:
            return
        if not self._closed_tool_calls and self._store is not None:
            self._closed_tool_calls |= self._store.reported_tool_calls(
                {str(item["id"]) for item in dangling}
            )
        for item in dangling:
            call_id = str(item["id"])
            if call_id in self._closed_tool_calls:
                continue
            self._closed_tool_calls.add(call_id)
            self._emit(
                "tool_result",
                {
                    "id": call_id,
                    "name": str(item["name"]),
                    "ok": False,
                    "error": _DANGLING_TOOL_NOTE,
                    "duration_ms": 0,
                },
            )

    # ---- 排队消息（界面上的队列面板）----
    def emit_queue(self) -> None:
        """把当前队列整份推给界面。

        整份推送而不是增量：队列很小，整份最不容易出错，前端也不用做对账。
        走瞬态事件（刷新后由 status().queued 兜底）。
        """
        self._emit("queue", {"queued": [m.to_dict() for m in self.state.pending_messages]})

    # ---- OpenAI 客户端 ----
    def _resolve_llm(self) -> None:
        """每回合创建主代理客户端，被停止关闭后不复用旧连接。"""
        backend = resolve_llm_backend(self.state.backend_profile_data or {})
        # 模型名或别名变化时仍沿用最后的实测基线，直到新请求返回 usage。
        # 提前清空会让停止/失败且没有 usage 的回合永久退回全量字符估算。
        self._openai_client = backend.client
        self._model = backend.model
        self._context_window = backend.context_window
        self.state.context_window = backend.context_window
        if self._store is not None:
            self._store.append_meta(context_window=backend.context_window)
        self._prompt_caching = backend.prompt_caching
        _log(f"主 Agent 模型: {backend.model} context_window={backend.context_window} prompt_caching={backend.prompt_caching}")

    @contextlib.contextmanager
    def _subagent_backend(self, profile: dict[str, Any], stop_event: threading.Event):
        """子代理连接独立持有，停止时关闭，任务结束后释放。"""
        backend = resolve_llm_backend(profile)
        client = backend.client
        try:
            with self._clients_lock:
                if stop_event.is_set():
                    raise AgentStopRequested()
                self._subagent_clients.append(client)
            yield SubagentBackendView(self, backend, stop_event)
        finally:
            with self._clients_lock:
                self._subagent_clients = [item for item in self._subagent_clients if item is not client]
            with contextlib.suppress(Exception):
                client.close()

    def abort_in_flight(self) -> None:
        """打断在途的 LLM 请求（等价于给请求发一个 abort 信号）。

        支持取消的 HTTP 栈能把 abort 一路传进请求层，在那里真实中断在途连接。
        Python 的 OpenAI SDK 没有可传的 signal，等价手段是关掉这份客户端：httpx 会让
        阻塞在 socket read 上的请求立刻抛 APIConnectionError（本地用"黑洞"服务实测：
        close() 0ms 返回、在途读立即被打断）。主循环随即看到停止信号、按「用户停止」
        收尾，不必干等 read 超时（以前是 600s，现在 LLM_SILENCE_TIMEOUT 兜底）。

        关闭主代理和当前仍在运行的子代理连接，可在 HTTP 处理线程里同步调用。
        """
        with self._clients_lock:
            clients = [self._openai_client, *self._subagent_clients]
        for client in clients:
            if client is not None:
                try:
                    client.close()
                except Exception as exc:  # noqa: BLE001
                    _log(f"关闭 LLM 客户端失败（忽略，仍按停止收尾）: {exc}")

    # ---- 主循环 ----
    def run(self) -> None:
        """跑一个回合：从当前对话历史出发，直到模型不再调工具、用户停止或出错。

        对话历史由注册表维护（start 初始化首条、message 追加后续），
        run 只负责循环。回合结束后状态置为 awaiting_input，用户可继续
        发消息触发下一回合。
        """
        turns = 0  # 本回合真实 LLM 请求次数；state.step 是事件计数（含流式 delta），不代表轮数
        # 每个回合（不只是首回合）都记下 running：重启后据此判断"上次被中断"
        self._write_running_meta(True)
        try:
            # 上一轮在工具执行中途退出（进程被杀/崩溃）时，界面上还挂着"没结果"的卡片：
            # 先补一条失败事件把它收掉（请求侧的合法性见 _messages_with_tool_placeholders）
            self._close_dangling_tool_calls()
            if not self.state.messages and not self.state.first_prompt.strip():
                self._end_turn("done", {})
                return
            self._resolve_llm()
            if not self.state.messages:
                self._persist_message({"role": "system", "content": _build_system_prompt(self.state)})
                # 首条消息只使用用户的实际输入。
                first_user_text = self.state.first_prompt
                self._persist_message({"role": "user", "content": first_user_text})
                # 首条用户消息也进事件流：SSE 全量回放（重开页面/状态对账）时
                # 气泡不丢。前端发送时已乐观显示，收到会按内容去重。
                self._emit("user_message", {"message": first_user_text})

            # 循环到模型不再调工具为止（没有实际步数上限）；MAX_STEPS 只是
            # 防呆——真触发了也不丢历史，用户可以接着指挥继续。
            for _ in range(MAX_STEPS):
                if self.stop_event.is_set():
                    self._end_turn("stopped", {"reason": "用户停止"})
                    return
                # 排队消息**不在这里注入**：它们的语义是"等本轮工作做完再发"——
                # 模型给出最终回复、不再调工具（turn_end=done）之后才轮到它们。
                # 消费点只有两处：本轮收尾（_close_turn）与「立即」发送（queue_send）。
                # 以前在这里按"安全点"注入，结果模型刚跑完第一个工具调用就被插进
                # 一条新消息，把一轮任务劈成两半。

                # 历史过长先压缩（Insert-then-Compress）：挂上压缩指令，用一轮
                # "复用当前会话"的请求拿摘要，再继续正常干活。
                if self._begin_compaction():
                    self._run_compaction_request()
                    turns += 1
                    continue

                # 上下文用量（界面指示器）：压缩之后再报，界面上立即看到回落
                self._emit_context_usage()

                loop_step = turns + 1
                turns += 1
                _log(f"—— 第 {loop_step}/{MAX_STEPS} 轮：请求 LLM（流式）中…")
                req_started = time.time()
                try:
                    content, tool_calls, finish_reason = self._stream_llm_response()
                except _ContextOverflow:
                    # 400 上下文超限：强制压缩一次再重试。先弹掉尾部一条腾空间
                    # （历史可能已经大到连压缩指令都塞不进去），压完再接回去。
                    # 每回合只做一次，仍超限说明问题不在历史长度，交给外层报错。
                    _log("  ⚠ 请求超出上下文窗口，强制压缩后重试")
                    if not self._begin_compaction(force=True, pull_back=1):
                        raise
                    self._run_compaction_request()
                    turns += 1
                    continue
                finally:
                    # 响应已落定（或抛错）：不再对外暴露"进行中的消息"
                    acc = self._take_stream_acc()
                streamed_content = bool(content)
                req_ms = int((time.time() - req_started) * 1000)
                _log(f"LLM 返回（耗时 {req_ms}ms）：content 长度={len(content)} tool_calls={len(tool_calls)} finish={finish_reason}")

                # 流结束立刻检查停止信号：流期间用户可能已点了停止
                if self.stop_event.is_set():
                    self._end_turn("stopped", {"reason": "用户停止"})
                    return

                # 截断保护：输出被 max_tokens 截断时，流式拼出来的工具参数可能是
                # "能解析但残缺"的半截 JSON，执行它会做出错误操作。
                # 这里直接丢弃本批工具调用，把失败写回历史让模型重试。
                if tool_calls and finish_reason == "length":
                    _log(f"  ⚠ 响应被截断（finish_reason=length），丢弃 {len(tool_calls)} 个工具调用")
                    # 思考/正文照常进转录，被丢弃的那批工具调用不进（它们没执行）
                    self._emit_assistant_message(acc, drop_tools=True)
                    self._persist_message({"role": "assistant", "content": content, **_reasoning_echo(acc)})
                    truncated_msg = (
                        "上一次响应因达到输出长度上限被截断，其中的工具调用可能不完整，已全部丢弃、未执行。"
                        "请缩小单次操作范围（比如减少一次读取的条目数、拆分批量修改）后重试。"
                    )
                    self._persist_message({"role": "user", "content": truncated_msg})
                    self._emit("tool_result", {
                        "id": "truncated",
                        "name": "(已丢弃的截断工具调用)",
                        "ok": False,
                        "error": f"响应被截断，{len(tool_calls)} 个工具调用未执行",
                        "duration_ms": 0,
                    })
                    continue

                # 思考/决策文本（即使同时有 tool_calls 也展示）。流式期间已通过
                # content_delta 增量推送；仅当流期间没有发出过任何 delta 时才
                # 补发一条完整 content（兜底非流式返回的 provider）。
                if content and not streamed_content:
                    preview = content if len(content) <= 120 else content[:117] + "…"
                    _log(f"  💭 思考: {preview}")
                    self._emit("content", {"content": content})

                # 助手消息落定：思考/正文/工具调用作为有序 parts 提交（转录的文本
                # 来源）。放在 tool_call 事件之前，界面先按 parts 校正文本卡片，
                # 再由后面的 tool_call/tool_result 事件按 id 更新工具行。
                self._emit_assistant_message(acc)

                if not tool_calls:
                    # 收尾回复也要写进历史，下一轮对话才能看到 Agent 说过什么。
                    # 思考（thinking 模式）一并回传：带 tools 的请求里，未调工具的那条
                    # assistant 消息同样要带 reasoning_content，否则下一次请求 400。
                    self._persist_message({"role": "assistant", "content": content, **_reasoning_echo(acc)})
                    _log(f"无工具调用，回合完成，共 {turns} 轮")
                    self._end_turn("done", {"summary": content, "total_steps": turns})
                    return

                # 把 assistant 这条消息原样追加（含 tool_calls），再逐个执行
                assistant_msg: dict[str, Any] = {"role": "assistant", **_reasoning_echo(acc)}
                if content:
                    assistant_msg["content"] = content
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": tc["arguments"]},
                    }
                    for tc in tool_calls
                ]
                self._persist_message(assistant_msg)
                responded: set[str] = set()

                def _fill_tool_placeholders() -> None:
                    """为未执行的工具补占位结果：OpenAI 要求 assistant.tool_calls
                    后必须紧跟对应的 tool 消息，否则下一回合的请求不合法。

                    顺带补一条失败事件，把界面上那行"没结果"的卡片收掉——它不会再执行了，
                    留着只会让用户以为还能操作（点了会被后端告知"没有在等的问题"）。
                    """
                    for tc2 in tool_calls:
                        if tc2["id"] in responded:
                            continue
                        note = "回合被用户停止，这一步没有执行"
                        self._persist_message({
                            "role": "tool",
                            "tool_call_id": tc2["id"],
                            "content": _tool_result_json({"error": note, "status": "stopped"}),
                        })
                        self._emit("tool_result", {
                            "id": tc2["id"], "name": tc2["name"], "ok": False,
                            "error": note, "duration_ms": 0,
                        })

                for tc in tool_calls:
                    call_id = tc["id"]
                    name = tc["name"]
                    if self.stop_event.is_set():
                        _fill_tool_placeholders()
                        self._end_turn("stopped", {"reason": "用户停止"})
                        return
                    responded.add(call_id)
                    try:
                        args = json.loads(tc["arguments"] or "{}")
                    except json.JSONDecodeError as exc:
                        args = {}
                        _log(f"  🔧 工具调用: {name} (参数解析失败: {exc})")
                        self._emit("tool_call", {"id": call_id, "name": name, "arguments": tc["arguments"]})
                        err = f"参数 JSON 解析失败：{exc}"
                        self._emit("tool_result", {"id": call_id, "name": name, "ok": False, "error": err, "duration_ms": 0})
                        self._persist_message({"role": "tool", "tool_call_id": call_id, "content": _tool_result_json({"error": err})})
                        continue

                    safe_args = _sanitize_tool_args(args)
                    args_preview = json.dumps(safe_args, ensure_ascii=False)
                    if len(args_preview) > 160:
                        args_preview = args_preview[:157] + "…"
                    _log(f"  🔧 工具调用: {name}({args_preview})")
                    self._emit("tool_call", {"id": call_id, "name": name, "arguments": safe_args})
                    started = time.time()
                    # 让工具（wait 等）在事件里带上本次调用的 id：界面据此把倒计时挂到
                    # 对应的那一行上。不能靠"找第一个 wait 行"——同一个活动组里等过几次
                    # 就会挂到最早那行，后面的等待没有倒计时。
                    self._active_tool_call_id = call_id
                    try:
                        result = self._dispatch_tool(name, args)
                        ok = True
                        duration_ms = int((time.time() - started) * 1000)
                        _log(f"  ✅ 工具结果: {name} 耗时 {duration_ms}ms")
                        # 写入工具的模型消息只带有限 Markdown 预览，前端保留原始变更明细。
                        rendered = _render_tool_result_table(name, result)
                        has_changes = isinstance(result, dict) and any(
                            key in result for key in ("changes", "line_diff", "deleted_preview")
                        )
                        self._emit(
                            "tool_result",
                            {
                                "id": call_id,
                                "name": name,
                                "ok": True,
                                "result": result if has_changes or rendered is None else rendered,
                                "duration_ms": duration_ms,
                            },
                        )
                        content_str = rendered if rendered is not None else _tool_result_json(result)
                    except AgentToolError as exc:
                        duration_ms = int((time.time() - started) * 1000)
                        _log(f"  ❌ 工具失败: {name} 耗时 {duration_ms}ms -> {exc}")
                        self._emit("tool_result", {"id": call_id, "name": name, "ok": False, "error": str(exc), "duration_ms": duration_ms})
                        content_str = _tool_result_json({"error": str(exc)})
                    finally:
                        self._active_tool_call_id = ""
                    self._persist_message({"role": "tool", "tool_call_id": call_id, "content": content_str})

            # 触到防呆上限（正常任务不会到这里）：收尾但保留历史，提示用户可以继续
            _log(f"触及单回合防呆上限 {MAX_STEPS} 轮，回合收尾")
            self._persist_message({
                "role": "assistant",
                "content": f"本回合轮数达到防呆上限 {MAX_STEPS}，已暂停。等待用户下一步指示。",
            })
            self._end_turn(
                "done",
                {
                    "summary": f"本回合轮数达到防呆上限 {MAX_STEPS}，已暂停。你可以发消息让我继续。",
                    "total_steps": turns,
                },
            )
        except AgentStopRequested:
            # 用户点了停止（可能在流式中、退避等待中、工具边界或请求刚被打断）：
            # 一律按正常停止收尾，不报错。具体是哪一处先看到信号由上面各自的日志说明。
            _log("收到停止信号，按用户停止收尾")
            self._end_turn("stopped", {"reason": "用户停止"})
        except Exception as exc:  # noqa: BLE001 - 顶层守护
            tb = traceback.format_exc()
            _log(f"❌ Agent 异常: {exc}\n{tb}")
            self._emit("error", {"message": str(exc), "traceback": tb})
            self.state.error = str(exc)
            self.state.status = "failed"
            self.state.turn_end = "failed"
        finally:
            self.state.finished_at = time.time()
            # 异常退出不经过 _close_turn，这里兜底清掉落盘里的 running 标记
            self._write_running_meta(False)
            _log(f"Agent 回合结束，状态={self.state.status}，共 {turns} 轮（事件 {self.state.step} 个）")
            if self.state.pending_followup:
                # 插话滞留到收尾（回合已停止消费），开新回合处理
                followup_runner = getattr(self, "_registry", None)
                if followup_runner is not None:
                    followup_runner._begin_followup(self.state.project_dir, self.state.session_id)

    def _end_turn(self, kind: str, data: dict[str, Any]) -> None:
        """收尾一个回合：发终态事件并落状态。awaiting_input 表示会话还活着。

        排队中的消息由 _close_turn 分两种处置：用户主动停止→留在队列面板里等
        用户决定（不代跑）；其他收尾→写进历史并开新回合消费。
        """
        # 「取插话 → 置 pending_followup → 落终态」必须与 message() 的
        # 「看状态 → 入队」互斥（共用注册表锁），否则用户恰好在收尾这几毫秒里发的
        # 消息会被当成"运行中插话"排队（那时状态还是 running），却错过了下面的
        # drain——消息既没进历史、也没人开新回合，界面上只剩一个孤零零的气泡。
        registry = getattr(self, "_registry", None)
        if registry is not None:
            with registry._lock:
                self._close_turn(kind, data)
        else:
            self._close_turn(kind, data)

    def _close_turn(self, kind: str, data: dict[str, Any]) -> None:
        """在注册表锁内完成的原子收尾：取插话 + 发终态事件 + 落终态。

        事件与状态的先后沿用原顺序（先事件后状态）：SSE 那条循环是「先取事件、
        再看状态」，状态若先落终态，它可能在终态事件还没入队时就判定收流。
        """
        queued: list[PendingMessage] = []
        if kind == "stopped":
            # 用户主动停止时不代跑排队消息，它们留在队列面板里等用户决定
            # （立即发送 / 编辑 / 删除，或直接再发一条）。一个字都不进历史。
            # 例外是用户点了「立即」的那条：它已被摘出队列、暂存在 immediate_message，
            # 这里把它作为新回合的第一条消息发出去（见 queue_send）。
            immediate = self.state.immediate_message
            self.state.immediate_message = ""
            if immediate:
                self._persist_message({"role": "user", "content": immediate})
                self._emit("user_message", {"message": immediate})
                # 这次"停止"是「立即」触发的，不是用户按了停止：文案要说清，
                # 否则界面上看着像"用户把回合停了"
                data = {**data, "reason": "已按「立即」打断本轮，马上发送这条消息"}
            self.state.pending_followup = bool(immediate)
            self.emit_queue()
        else:
            # 普通收尾（跑完/出错）：本轮工作已结束，排队的消息现在写进历史并开新
            # 回合续跑——这正是"等本轮做完再发"的落点。事件先记着、稍后再发
            # （见函数末尾：要排在 finish 之后，界面上才读得顺）。
            queued = self._drain_pending_messages()
            for msg in queued:
                self._persist_message({"role": "user", "content": msg.text})
            self.state.pending_followup = bool(queued)
            # 兜底：drain 之后队列里又冒出消息（锁能挡住 message() 的正常路径，这里防呆）
            # ——留着让下一回合取走，绝不能把它丢在队列里没人管。
            if not self.state.pending_followup and self.state.pending_messages:
                self.state.pending_followup = True
        # 回合结束就把压缩标记清掉，下一回合重新评估上下文用量
        self._compacted_this_turn = False
        # 溢出恢复同理：新回合可以再用一次（同一回合内只恢复一次，避免空转）
        self._overflow_recovery_used = False
        # 压缩失败标记也只在回合内有效：下个回合重新给一次机会
        self._compact_failed_this_turn = False
        # 清掉落盘里的 running 标记：否则下次启动会误判"上次被中断"
        if self._store is not None:
            self._store.append_meta(running=False)
        # 紧接着就会开 followup 回合（「立即」发送 / 滞留插话）：告诉前端别把运行态
        # 打回停止——否则停止按钮消失、顶栏显示成"空闲"，而后端其实还在跑。
        if self.state.pending_followup:
            data = {**data, "followup": True}
        if kind == "stopped":
            self._emit("stopped", data)
            self.state.status = "stopped"
        else:
            self._emit("finish", data)
            self.state.status = "awaiting_input"
        self.state.turn_end = kind
        # 排队消息的 user_message 事件排在收尾事件之后：界面上才是
        # 「本轮最终回复 → 你发的新消息 → 下一轮」。反过来的话，本轮最终回复会
        # 渲染在你这条新消息的下面（其实是本轮先说的）。
        for msg in queued:
            self._emit("user_message", {"message": msg.text})

    def _drain_pending_messages(self) -> list[PendingMessage]:
        """取走队列里等待被模型看到的消息（无则返回空列表），并同步界面面板。"""
        if not self.state.pending_messages:
            return []
        msgs: list[PendingMessage] = []
        while self.state.pending_messages:
            msgs.append(self.state.pending_messages.popleft())
        _log(f"  💬 注入用户插话 x{len(msgs)}")
        self.emit_queue()  # 面板上这几条要撤掉（它们已进转录）
        return msgs

    # ---- 助手消息（转录里思考/正文的唯一来源）----

    def _take_stream_acc(self) -> dict[str, Any] | None:
        """取走并清空"进行中消息"的累加器（响应落定或失败时调用）。"""
        acc = self._stream_acc
        self._stream_acc = None
        return acc

    def live_streaming(self) -> dict[str, Any] | None:
        """当前正在生成的助手消息快照（进行中消息），没有则 None。

        status() 直接调它：界面刷新/切会话时能把"正在写的那半条消息"照原样画
        出来，不用等它收尾。step 是快照覆盖到的事件序号，客户端据此续订 SSE，
        避免把快照里已经包含的增量又补一遍。
        """
        acc = self._stream_acc
        if acc is None:
            return None
        parts = _assistant_parts(acc)
        if not parts:
            return None
        return {"step": self.state.step, "parts": parts}

    def _emit_assistant_message(self, acc: dict[str, Any] | None, *, drop_tools: bool = False) -> None:
        """把一条已落定的助手响应提交为持久化事件。

        content_delta / reasoning_delta 是瞬态的（不落盘、不进快照），思考与正文
        只有并入这里才成为转录的一部分——刷新/切会话/重连后重建得出来，靠的就是
        它。drop_tools 用于"响应被截断、工具调用已丢弃"的场景。
        """
        parts = _assistant_parts(acc)
        if drop_tools:
            parts = [p for p in parts if p["type"] != "tool_call"]
        if not parts:
            return
        self._emit("assistant_message", {"parts": parts})

    # ---- 流式 LLM 响应 ----
    def _stream_llm_response(self) -> tuple[str, list[dict[str, Any]], str]:
        """带自动重试的流式请求：失败时退避重试，并把过程实时推给界面。

        重试对用户可见（llm_retry_start / llm_retry_end 事件），中间失败不落错误
        提示；退避次数耗尽后才把失败交给上层报错。退避期间收到停止信号会立刻
        中断（抛 AgentStopRequested，由主循环按「用户停止」收尾）。
        """
        attempt = 0
        while True:
            try:
                return self._stream_llm_attempt()
            except AgentStopRequested:
                raise
            except Exception as exc:  # noqa: BLE001 - 按分类决定是否重试
                info = _classify_llm_error(exc)
                if self.stop_event.is_set():
                    # 请求失败的同时用户已请求停止：按停止收尾，不要当成错误弹给用户。
                    # 注意这条路径没有退避重试——停止优先于重试，日志也如实区分
                    # （以前复用"退避重试期间收到停止信号"的文案，容易让人以为没重试）。
                    _log("请求失败且已收到停止信号，按用户停止收尾（不重试）")
                    raise AgentStopRequested() from exc
                if not info["retriable"] or attempt >= LLM_MAX_RETRIES:
                    # 上下文超限：原地重试没有意义（历史长度没变），抛给主循环做一次
                    # 强制压缩后再重试原请求。压缩请求自己（quiet）不参与这次恢复，
                    # 否则会把唯一一次恢复机会消耗在压缩上。
                    if (
                        not self._stream_quiet
                        and info["code"] == "CONTEXT_TOO_LARGE"
                        and not self._overflow_recovery_used
                    ):
                        self._overflow_recovery_used = True
                        _log("  ⚠ 上下文超限：交给主循环强制压缩后重试")
                        raise _ContextOverflow() from exc
                    if attempt > 0:
                        _log(f"  ❌ 重试 {attempt} 次后仍失败：{info['code']} {info['message']}")
                        raise RuntimeError(
                            f"模型请求失败（已重试 {attempt} 次）：{info['message']}"
                        ) from exc
                    raise
                attempt += 1
                # 失败那次尝试的半截内容作废：界面已按 llm_retry_start 丢掉这些卡片，
                # 快照也别再挂着，免得刷新后又冒出来
                self._stream_acc = None
                delay_ms = _llm_retry_delay_ms(attempt, info)
                _log(
                    f"  ⚠ 请求失败（{info['code']}: {info['message']}），"
                    f"{delay_ms / 1000:g}s 后重试 {attempt}/{LLM_MAX_RETRIES}"
                )
                self._emit("llm_retry_start", {
                    "attempt": attempt,
                    "max_attempts": LLM_MAX_RETRIES,
                    "delay_ms": delay_ms,
                    "code": info["code"],
                    "reason": info["message"],
                    "status": info["status"],
                    "ts": time.time(),
                })
                aborted = self.stop_event.wait(delay_ms / 1000)
                self._emit("llm_retry_end", {"attempt": attempt, "aborted": aborted})
                if aborted:
                    _log("  ⏹ 退避等待期间收到停止信号，放弃重试")
                    raise AgentStopRequested() from exc

    def _create_stream(self, *, include_usage: bool) -> Any:
        """发一次流式请求（不做重试，重试由外层的 _stream_llm_attempt 与重试循环负责）。"""
        messages = self._messages_for_request(strip_internal=False)
        tools = AGENT_TOOLS
        if self._prompt_caching:
            # 只对认这套断点的后端注入（见 _resolve_prompt_caching）：Anthropic 系
            # 需要显式 cache_control，OpenAI 兼容的多数实现是服务端自动前缀缓存。
            messages, tools = _apply_prompt_cache(messages, tools)
        # 断点选择依赖内部标记；选择后再剥掉，不能提前丢失临时消息的身份。
        messages = [_strip_internal_fields(message) for message in messages]
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "stream": True,
        }
        if include_usage:
            kwargs["stream_options"] = {"include_usage": True}
        return self._openai_client.chat.completions.create(**kwargs)

    def _messages_for_request(self, *, strip_internal: bool = True) -> list[dict[str, Any]]:
        """发请求前的消息列表：剥掉内部标记，并按需补齐 thinking 模式的思考字段。

        内部标记（`_compact_instruction` / `_compact_summary` 这些 `_` 开头的键）只在
        内存里用，第三方 OpenAI 兼容端点收到陌生字段可能直接 400，必须剥掉。
        strip_internal=False 仅供缓存断点选择使用，_create_stream 会在发送前统一剥除。

        DeepSeek（及同类 thinking 模式）要求：只要请求带了 tools，历史里每条 assistant
        消息都必须把当初的 reasoning_content 回传——**即使该轮模型没有实际进行工具调用**，
        少一条就 400「The `reasoning_content` in the thinking mode must be passed back to
        the API」。本方法负责：这场会话是 thinking 会话时（流里见过该字段，或历史里已有），
        给缺的那些补空串；老会话（本修复之前存的 assistant 消息还没带这个字段）靠这一步
        救回来。不是 thinking 会话就不补，不给不认它的 provider 塞陌生字段。
        """
        field = self._reasoning_field
        if not field:
            for message in self.state.messages:
                if message.get("role") != "assistant":
                    continue
                field = next((name for name in REASONING_FIELD_NAMES if name in message), "")
                if field:
                    break
        messages: list[dict[str, Any]] = []
        # 中途退出的回合会留下"有 tool_calls、缺 tool 响应"的残缺历史：补占位结果后再发，
        # 否则整份请求不合法（provider 直接 400，见 _messages_with_tool_placeholders）
        for message in _messages_with_tool_placeholders(self.state.messages):
            clean = _strip_internal_fields(message) if strip_internal else message
            if field and message.get("role") == "assistant" and field not in message:
                clean = {**clean, field: ""}
            messages.append(clean)
        return messages

    def _open_stream(self, *, include_usage: bool) -> Any:
        """建流式请求，并对两类「provider 侧的要求」就地兜底重试一次。

        - 不认 stream_options → 摘掉它再试（usage 只是上下文用量的估算锚点，没有也能跑）；
        - thinking 模式要求回传思考字段 → 记下字段名，补齐历史（见 _messages_for_request）后重试。

        其它错误（网络/限流/5xx）一律抛给外层重试循环，否则会在这里再悄悄发一次请求——
        既让用户看不到重试，又把请求次数翻倍。
        """
        try:
            return self._create_stream(include_usage=include_usage)
        except Exception as exc:  # noqa: BLE001 - 只对下面这两类做兜底，其余原样抛
            if include_usage and _is_unsupported_param_error(exc):
                _log("  ⚠ provider 不支持 stream_options，退回不带 usage 的请求")
                return self._open_stream(include_usage=False)
            field = _requested_reasoning_field(exc)
            if field and field != self._reasoning_field:
                _log(f"  ⚠ API 要求回传 {field}（thinking 模式 + tools），补齐历史后重试")
                self._reasoning_field = field
                return self._open_stream(include_usage=include_usage)
            raise

    def _stream_llm_attempt(self) -> tuple[str, list[dict[str, Any]], str]:
        """发起一次流式 chat.completions 请求，边收边推 content_delta 事件。

        返回 (content, tool_calls, finish_reason)：
        - content：文本部分全文（流期间已通过 content_delta 增量推送过）；
        - tool_calls：按 delta 顺序拼接好的调用列表，结构为
          [{id, name, arguments(str)}]；
        - finish_reason：stop / length / tool_calls 等，length 表示被截断。

        推理模型（DeepSeek-R1/GLM 等）的思考内容在非标准字段
        reasoning_content / reasoning 里，位置因平台而异：有的在
        delta.reasoning_content 直接属性上，有的被 OpenAI SDK 收进
        delta.model_extra。这里统一提取：走独立的 reasoning_delta 事件流给前端渲染成
        可折叠的「思考中」卡片，同时记下字段名——thinking 模式下它还要跟着 assistant
        消息回传给 provider（带 tools 的请求不回传会 400，见 _messages_for_request）。

        停止信号在流期间到达时立即弃流返回（上层会走 stopped 收尾），
        不再消费后续 chunk。
        """
        # include_usage 让 provider 在流末尾回一个 usage（部分兼容实现不认，
        # 抛错就退回到不带该参数重试一次）。usage 用于上下文用量锚点估算。
        stream = self._open_stream(include_usage=True)

        content_parts: list[str] = []  # 「说」：模型回复正文
        reasoning_parts: list[str] = []  # 「想」：思考内容，只展示不进历史
        # index -> {id, name, arguments_parts}
        tool_calls_acc: dict[int, dict[str, Any]] = {}
        # 对外暴露"正在生成的助手消息"（status().streaming）：这里只挂引用，
        # 快照按需即时组装（见 live_streaming），不随每个 delta 重建。
        self._stream_acc = {
            "content": content_parts,
            "reasoning": reasoning_parts,
            "tools": tool_calls_acc,
        }
        pending_content: list[str] = []  # 距上次 emit 攒下的回复文本（节流缓冲）
        pending_reasoning: list[str] = []  # 距上次 emit 攒下的思考文本（节流缓冲）
        throttle: dict[str, float] = {"content": 0.0, "reasoning": 0.0}
        finish_reason = ""
        stream_started = time.time()  # 段起点缺失时的耗时兜底
        # 交替思考模型（GLM-4.6 等）在同一条流里 想/说 会来回切换，而
        # content_end / reasoning_end 是前端撤打字机光标的依据，必须跟着
        # 段走：切换时立即收掉上一段，流结束时收掉还开着的那段。
        open_kind: str | None = None  # 当前正在流的路：content / reasoning
        segment_started: dict[str, float | None] = {"content": None, "reasoning": None}

        def _end_stream_segment(kind: str) -> None:
            """收掉一段流：发 {kind}_end（前端据此撤光标、记耗时）。

            先冲掉该路节流缓冲里攒着的尾巴，保证 end 之前该段增量已全部
            送达——否则尾部增量会晚于 end 到达，在前端漏成孤立的残段卡片。
            """
            if kind == "content":
                _flush_stream("content", pending_content, len(content_parts), force=True)
            else:
                _flush_stream("reasoning", pending_reasoning, len(reasoning_parts), force=True)
            if not self._stream_quiet:
                started = segment_started[kind]
                parts = content_parts if kind == "content" else reasoning_parts
                self._emit(f"{kind}_end", {
                    "length": len("".join(parts)),
                    "duration_ms": int((time.time() - (started if started is not None else stream_started)) * 1000),
                })
            segment_started[kind] = None

        def _flush_stream(kind: str, pending: list[str], total: int, force: bool = False) -> None:
            """节流冲刷增量：最多 25ms 一条，避免 step 计数被 delta 刷爆。

            压缩请求（_stream_quiet）只静音 emit，缓冲照常清空——不清会越攒越大。
            """
            if not pending:
                return
            now = time.monotonic()
            if force or now - throttle[kind] >= 0.025:
                if not self._stream_quiet:
                    self._emit(f"{kind}_delta", {"delta": "".join(pending), "index": total})
                pending.clear()
                throttle[kind] = now

        for chunk in stream:
            if self.stop_event.is_set():
                _log("  流式响应被停止信号打断，弃流")
                break
            # usage 可能在 choices 为空的收尾 chunk 上单独到达
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                prompt_tokens = getattr(usage, "prompt_tokens", None)
                if isinstance(prompt_tokens, int) and prompt_tokens > 0 and self._pending_compaction is None:
                    self._record_context_usage(prompt_tokens)
            if not getattr(chunk, "choices", None):
                continue
            choice = chunk.choices[0]
            if getattr(choice, "finish_reason", None):
                finish_reason = str(choice.finish_reason)
            delta = choice.delta
            # 思考内容：直接属性 / model_extra 里的 reasoning_content 或
            # reasoning（OpenRouter 等平台用后者），逐个都试一遍。命中的字段名要记下来：
            # thinking 模式下同样的字段要跟着 assistant 消息回传（见 _messages_for_request）。
            extra = getattr(delta, "model_extra", None) or {}
            reasoning_piece = getattr(delta, "reasoning_content", None)
            reasoning_field = REASONING_FIELD_NAMES[0] if reasoning_piece else ""
            if not reasoning_piece and isinstance(extra, dict):
                for candidate in REASONING_FIELD_NAMES:
                    value = extra.get(candidate)
                    if isinstance(value, str) and value:
                        reasoning_piece, reasoning_field = value, candidate
                        break
            if isinstance(reasoning_piece, str) and reasoning_piece:
                if not reasoning_field:
                    reasoning_field = REASONING_FIELD_NAMES[0]
                if not self._reasoning_field:
                    self._reasoning_field = reasoning_field
                if self._stream_acc is not None:
                    self._stream_acc["reasoning_field"] = reasoning_field
                if open_kind != "reasoning":
                    if open_kind == "content":
                        _end_stream_segment("content")  # 说→想 切换：先收掉说的一段
                    open_kind = "reasoning"
                if segment_started["reasoning"] is None:
                    segment_started["reasoning"] = time.time()
                reasoning_parts.append(reasoning_piece)
                pending_reasoning.append(reasoning_piece)
                _flush_stream("reasoning", pending_reasoning, len(reasoning_parts))
            piece = getattr(delta, "content", None)
            if piece:
                if open_kind != "content":
                    if open_kind == "reasoning":
                        _end_stream_segment("reasoning")  # 想→说 切换：先收掉想的一段
                    open_kind = "content"
                if segment_started["content"] is None:
                    segment_started["content"] = time.time()
                content_parts.append(piece)
                pending_content.append(piece)
                _flush_stream("content", pending_content, len(content_parts))
            for tc in getattr(delta, "tool_calls", None) or []:
                idx = tc.index
                slot = tool_calls_acc.setdefault(idx, {"id": "", "name": "", "arguments_parts": []})
                if tc.id:
                    slot["id"] = tc.id
                fn = getattr(tc, "function", None)
                if fn is not None:
                    # name 与 id 一样可能分片到达（部分 provider 逐字符推）
                    if fn.name:
                        slot["name"] += fn.name
                    if fn.arguments:
                        slot["arguments_parts"].append(fn.arguments)

        # 冲掉两条节流缓冲里剩下的文本
        _flush_stream("content", pending_content, len(content_parts), force=True)
        _flush_stream("reasoning", pending_reasoning, len(reasoning_parts), force=True)
        if reasoning_parts:
            _log(f"  🧠 思考内容 {len(''.join(reasoning_parts))} 字（展示流 + thinking 模式下随 assistant 消息回传）")
        # 收掉还开着的最后一段（切换发生时上一段已当场收掉）。前端据此
        # 撤掉打字机光标、记下耗时；停止信号弃流时也要走到这里，否则光标残留。
        if open_kind is not None:
            _end_stream_segment(open_kind)

        tool_calls = [
            {
                "id": slot["id"],
                "name": slot["name"],
                "arguments": "".join(slot["arguments_parts"]),
            }
            for _, slot in sorted(tool_calls_acc.items())
        ]
        return "".join(content_parts), tool_calls, finish_reason

    # ---- 上下文用量估算与压缩 ----
    def _request_overhead_tokens(self) -> int:
        """这次请求里 messages 之外的固定开销：tools schema（见 _tools_overhead_tokens）。"""
        return _tools_overhead_tokens(AGENT_TOOLS)

    def _record_context_usage(self, prompt_tokens: int) -> None:
        """持久化 provider 实测用量，重启后继续只估算新增消息。"""
        messages = self.state.messages
        self.state.last_prompt_tokens = prompt_tokens
        self.state.anchored_message_count = len(messages)
        self.state.usage_model = self._model
        if self._store is not None:
            self._store.append_meta(context_usage_anchor={
                "prompt_tokens": prompt_tokens,
                "message_count": len(messages),
                "fingerprint": _history_fingerprint(messages),
                "model": self._model,
            }, context_window=self._context_window)

    def _estimate_context_tokens(self) -> int:
        """估算当前请求占用的 token 数（锚点法，见 _estimate_usage_tokens）。"""
        return _estimate_usage_tokens(
            self.state.messages,
            self.state.last_prompt_tokens,
            self.state.anchored_message_count,
            self._request_overhead_tokens(),
        )

    def _emit_context_usage(self) -> None:
        """把当前上下文用量推给界面（指示器）。

        走瞬态事件：刷新页面后由 status() 里的 context 快照重新给出，不必占
        对话转录（否则每次 LLM 请求都会在界面上多出一条无意义记录）。
        """
        self._emit("context_usage", {
            "context": {
                "used_tokens": self._estimate_context_tokens(),
                "window_tokens": self._context_window,
            },
        })

    def _maybe_compact(self) -> None:
        """同步压缩入口（降级路径）：直接另发一次摘要请求，压完重建。

        正式路径是 Insert-then-Compress（_begin_compaction + _run_compaction_request，
        复用当前会话前缀）；这里是它的兜底——压缩请求失败、模型不按指令走时可以调用。
        触发线：估算用量 > 窗口的 COMPACT_TRIGGER_RATIO。
        """
        if self._compacted_this_turn or self._pending_compaction is not None:
            return
        window = self._context_window
        limit = int(window * COMPACT_TRIGGER_RATIO) - CONTEXT_RESERVE_TOKENS
        if self._estimate_context_tokens() <= limit:
            return
        cut = _find_compaction_cut(self.state.messages, _keep_recent_tokens(limit))
        if cut <= 0:
            _log("  ⚠ 上下文超阈值但找不到安全切点，跳过压缩")
            return
        self._compact_via_separate_request(cut)

    def _begin_compaction(self, *, force: bool = False, pull_back: int = 0) -> bool:
        """判断要不要压缩；要就把压缩指令挂到当前历史末尾，返回 True。

        **Insert-then-Compress**（不另开摘要请求）：指令作为一条**不落盘**的瞬时
        消息拼在会话尾部，由下一轮正常请求带着它一起发出去——system prompt、tools、
        历史前缀全部复用，摘要调用本身也能命中提示缓存；压完只产生一次前缀失效。
        对照：另发独立摘要请求的共享前缀为 0，压完主会话还要冷 4~5 轮。

        pull_back > 0 用于溢出恢复：历史已经超过窗口、连指令都塞不进去时，先弹出
        尾部 K 条腾空间（由 _rebuild_after_compaction 接回重建后的尾部，不会丢）。
        """
        if self._pending_compaction is not None or self._compact_failed_this_turn:
            return False
        window = self._context_window
        limit = int(window * COMPACT_TRIGGER_RATIO) - CONTEXT_RESERVE_TOKENS
        estimated = self._estimate_context_tokens()
        if not force:
            if self._compacted_this_turn:
                return False
            if estimated <= limit:
                return False

        messages = self.state.messages
        pulled: list[dict[str, Any]] = []
        if pull_back > 0:
            count = min(pull_back, max(0, len(messages) - 1))  # 永不弹 system
            if count > 0:
                pulled = messages[-count:]
                del messages[-count:]

        cut = _find_compaction_cut(messages, _keep_recent_tokens(limit))
        if cut <= 0:
            messages.extend(pulled)  # 找不到安全切点：把弹出的放回去
            _log("  ⚠ 上下文超限但找不到安全切点，跳过本次压缩")
            return False
        # 摘掉头部后仍在触发线以上：问题出在**保留段自身**（典型是尾部压着一条超大工具
        # 结果），压缩救不回来——别白花一次摘要请求，更别把摘要本身再摘要一遍。
        # force（溢出恢复）不走这里：那时只有压缩这一条路。
        if not force and limit > 0:
            # estimated 走锚点法（provider 真实 prompt_tokens + 锚点后新增消息的本地估算，
            # 里面已经含 tools schema 那笔固定开销），下面这仓名是纯本地字符估算——两套单位
            # 不能直接相减：中文场景本地估算偏低好几倍，一相减"保留段"就被放大到远超实际，
            # 守卫每回合都误触发、压缩永远不跑（"爆上下文 157k/128k 却不压缩"的事故就是它）。
            # 正确做法：把 estimated 里 messages 那一部分换算成比例，只按它折算保留段，
            # 再把 tools 固定开销加回来一次——算出来的才是"压完那次请求"的真实大小。
            overhead = self._request_overhead_tokens()
            full = list(messages) + list(pulled)  # pull_back 摘下的尾部也计入总量
            total_local = sum(_estimate_message_tokens(m) for m in full)
            head_local = sum(_estimate_message_tokens(m) for m in messages[:cut])
            tail_local = total_local - head_local
            message_est = max(0, estimated - overhead)
            if total_local > 0 and message_est > 0:
                tail_est = int(tail_local * message_est / total_local) + overhead
            else:
                tail_est = tail_local + overhead
            if tail_est >= limit:
                messages.extend(pulled)
                _log("  ⚠ 保留段自身已到触发线（尾部有压不掉的大结果），压缩无益，跳过")
                return False

        instruction = {
            "role": "user",
            "content": COMPACT_INSTRUCTION_PROMPT,
            "_compact_instruction": True,
        }
        messages.append(instruction)  # 只进内存：失败回滚时才不会污染落盘历史
        self._pending_compaction = {
            "cut": cut,
            "pulled": pulled,
            "estimated": estimated,
            "instruction": instruction,
        }
        _log(f"  📦 准备压缩：将移除 {cut} 条（估算 {estimated} tokens，复用当前会话前缀）")
        return True

    def _abort_compaction(self) -> None:
        """放弃本次压缩：摘掉指令消息，把弹出的消息放回原位。"""
        ctx = self._pending_compaction
        self._pending_compaction = None
        if ctx is None:
            return
        messages = self.state.messages
        instruction = ctx.get("instruction")
        if instruction is not None and messages and messages[-1] is instruction:
            messages.pop()
        pulled = ctx.get("pulled") or []
        if pulled:
            messages.extend(pulled)

    def _run_compaction_request(self) -> None:
        """发出携带压缩指令的请求并收下摘要；失败回退独立摘要请求。

        这条请求走的是正常流式通道，但把增量静音（quiet）——摘要内容不该以
        「助手正文」的形式刷到界面上。模型不按指令走（返回工具调用）或响应无法
        解析时，一律回退到旧的独立摘要路径，保证"压不了"不会演变成"回合失败"。
        """
        self._emit("compacting", {"phase": "start", "tokens_before": int((self._pending_compaction or {}).get("estimated") or 0)})
        previous_quiet = self._stream_quiet
        self._stream_quiet = True  # 摘要内容不该以助手正文的形式上屏
        try:
            content, tool_calls, _finish = self._stream_llm_response()
        except AgentStopRequested:
            self._abort_compaction()
            raise
        except Exception as exc:  # noqa: BLE001 - 压缩失败不能拖垮整回合
            _log(f"  ⚠ 压缩请求失败（{exc}），回退独立摘要请求")
            self._abort_compaction()
            self._compact_via_separate_request()
            return
        finally:
            self._stream_quiet = previous_quiet
            # 压缩请求也走流式通道，会留下"进行中消息"快照；摘要内容不该以
            # 助手正文的形式挂到界面上，这里直接丢掉。
            self._stream_acc = None
        if tool_calls:
            _log("  ⚠ 压缩请求返回了工具调用（没按指令走），回退独立摘要请求")
            self._abort_compaction()
            self._compact_via_separate_request()
            return
        if not self._finish_compaction(content):
            _log("  ⚠ 压缩响应解析失败，回退独立摘要请求")
            self._compact_via_separate_request()

    def _finish_compaction(self, content: str) -> bool:
        """摘要到手 → 归档被裁历史 → 重建消息列表。"""
        if self._pending_compaction is None:
            return False
        summary = _parse_compact_summary(content)
        if not summary.strip():
            self._abort_compaction()
            return False
        topics = _parse_compact_topics(content)
        return self._rebuild_after_compaction(summary, topics)

    def _compact_via_separate_request(self, cut: int | None = None) -> None:
        """降级路径：另发一次非流式摘要请求（共享前缀为 0，但一定能拿到摘要）。"""
        messages = self.state.messages
        if cut is None:
            limit = int(self._context_window * COMPACT_TRIGGER_RATIO) - CONTEXT_RESERVE_TOKENS
            cut = _find_compaction_cut(messages, _keep_recent_tokens(limit))
        if cut <= 0:
            self._compact_failed_this_turn = True
            return
        head = messages[:cut]
        estimated = self._estimate_context_tokens()
        summary = ""
        try:
            summary = self._summarize_messages(head)
        except Exception as exc:  # noqa: BLE001
            _log(f"  ⚠ 独立摘要请求也失败（{exc}），改用本地兜底摘要")
        if not summary.strip():
            summary = _local_fallback_summary(head)
        self._pending_compaction = {
            "cut": cut,
            "pulled": [],
            "estimated": estimated,
            "instruction": None,
        }
        if not self._rebuild_after_compaction(summary, ""):
            self._abort_compaction()
            self._compact_failed_this_turn = True

    def _rebuild_after_compaction(self, summary: str, topics: str) -> bool:
        """用摘要替换被裁掉的那段历史：system 原样保留，摘要单独成条。

        刻意**不改 system prompt**：把摘要拼进 system 会让整个前缀（含 tools）
        从第一条起失效；摘要作为 system 之后的一条独立消息，system + tools 这段
        前缀还能继续命中缓存。

        摘要消息同时写进会话文件（见下面 append_message）：历史文件只追加，被裁掉的
        那些消息不会从文件里消失，恢复会话时只能靠这条摘要知道"压缩后的历史长什么样"
        （见 _restore_compacted_history）。
        """
        ctx = self._pending_compaction
        self._pending_compaction = None
        if ctx is None:
            return False
        messages = self.state.messages
        instruction = ctx.get("instruction")
        if instruction is not None and messages and messages[-1] is instruction:
            messages.pop()
        cut = int(ctx.get("cut") or 0)
        if cut <= 0 or cut > len(messages):
            pulled = list(ctx.get("pulled") or [])
            if pulled:
                messages.extend(pulled)
            return False
        system = messages[0] if messages and messages[0].get("role") == "system" else None
        head, tail = messages[:cut], messages[cut:]
        kept = [*tail, *list(ctx.get("pulled") or [])]
        archive_name = self._archive_compacted(head, topics)
        summary_msg = self._build_summary_message(summary, archive_name)
        # 保留了多少条**真消息**记在摘要上：恢复时靠它把这段现场一起接回来（上一轮的摘要
        # 不是消息，不计——它已经被这次的摘要概括掉了）。
        summary_msg["_compact_kept"] = sum(1 for m in kept if not _is_internal_message(m))
        # system 原样保留（前缀稳定的关键）；历史里没有 system 的异常情况才补一条，
        # 保证压缩后仍是「system 打头」的合法结构。
        if system is None:
            system = {"role": "system", "content": _build_system_prompt(self.state)}
        self.state.messages = [system, summary_msg, *kept]
        # 摘要落盘。不能走 _persist_message：它会把这条再往 state.messages 末尾塞一份，
        # 而这条在内存里的位置是 system 之后。
        if self._store is not None:
            self._store.append_message(summary_msg)
        # 压缩后旧的 usage 锚点失效，重置避免继续用错误的估算
        self.state.last_prompt_tokens = 0
        self.state.anchored_message_count = 0
        self._compacted_this_turn = True
        removed = len(head)
        tokens_before = int(ctx.get("estimated") or 0)
        # 压缩后的大小按**重建出来的真实消息列表**再估一次（锚点刚重置，这里是纯字符估算）：
        # 保留的尾部、尤其是里面体积很大的工具结果，必须一起算进去——只报摘要大小会让人
        # 以为"压完就剩这么点"，而实际下一轮请求可能还是贴着上限。
        tokens_after = self._estimate_context_tokens()
        if self._store is not None:
            self._store.append_compact(
                removed=removed,
                summary_chars=len(summary),
                tokens_before=tokens_before,
                tokens_after=tokens_after,
            )
        self._emit("compacted", {
            "removed": removed,
            "summary_chars": len(summary),
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
            "archive": archive_name or "",
        })
        self._emit("compacting", {"phase": "done"})
        tail_note = f"，归档 {archive_name}" if archive_name else ""
        _log(
            f"  📦 压缩完成：移除 {removed} 条，摘要 {len(summary)} 字符{tail_note}"
            f"（估算 {tokens_before} → {tokens_after} tokens）"
        )
        return True

    def _build_summary_message(self, summary: str, archive_name: str) -> dict[str, Any]:
        """摘要消息：模型认得的历史卡，外加归档索引（细节靠 read_history_archive 回查）。"""
        lines = ["[早前对话的压缩摘要 —— 原对话已归档]", "", summary.strip()]
        chunks: list[dict[str, Any]] = []
        if self._store is not None:
            chunks = self._store.list_chunks()
        if chunks:
            lines += ["", "---", "📁 已归档的早期对话（需要细节时用 read_history_archive 回查）："]
            for item in chunks[-COMPACT_ARCHIVE_MAX_LISTED:]:
                suffix = f" — {item['topics']}" if item.get("topics") else ""
                lines.append(f"- {item['name']}{suffix}")
            if len(chunks) > COMPACT_ARCHIVE_MAX_LISTED:
                lines.append(f"- ……另有 {len(chunks) - COMPACT_ARCHIVE_MAX_LISTED} 个更早的归档")
        return {"role": "user", "content": "\n".join(lines), "_compact_summary": True}

    def _archive_compacted(self, head: list[dict[str, Any]], topics: str) -> str:
        """把被裁掉的历史写成一份归档文件，返回文件名（失败不影响压缩本身）。"""
        if self._store is None:
            return ""
        body = [
            m for m in head
            if isinstance(m, dict) and m.get("role") != "system" and not _is_internal_message(m)
        ]
        if not body:
            return ""
        name = COMPACT_ARCHIVE_NAME.format(index=len(self._store.list_chunks()) + 1)
        lines = [
            "---",
            f"session_id: {self.state.session_id}",
            f"archived_at: {time.time():.0f}",
            f"message_count: {len(body)}",
        ]
        if topics:
            lines.append(f"topics: {topics}")
        lines += [
            "---",
            "",
            "# 会话归档",
            "",
            "> 这是上下文压缩时归档下来的原始对话。用 read_history_archive 读它。",
            "",
        ]
        lines.extend(_render_archive_messages(body))
        return self._store.write_chunk(name, "\n".join(lines)) or ""

    def _summarize_messages(self, messages: list[dict[str, Any]]) -> str:
        """调 LLM 把一段历史压成结构化摘要（独立请求，非流式）。"""
        return summarize_messages(self._openai_client, self._model, messages)

    # ---- 工具分发 ----
    def _dispatch_tool(self, name: str, args: dict[str, Any]) -> Any:
        handler = _TOOL_HANDLERS.get(name)
        if handler is None:
            raise AgentToolError(_RETIRED_TOOLS.get(name) or f"未知工具：{name}")
        # 权限门禁：按当前模式放行 / 先请用户批准（拒绝时抛 AgentToolError，由主循环
        # 转成工具错误给模型看——见 _require_permission）。
        self._require_permission(name, args)
        # 带 reason 的工具（写类 + 启动翻译）的可选 reason（模型说明"为什么"）集中在这里
        # 搬到结果上，而不是让各自的 handler 都拼一遍——它们的结果结构各不相同，漏一个
        # 就是"填了却看不见"。
        return _attach_reason(name, args, handler(self, args))

    # ---- 询问用户（ask_user）----
    def ask_user(
        self, request_id: str, tool_call_id: str, questions: list[dict[str, Any]]
    ) -> list[list[str] | None]:
        """发起一次询问并**阻塞**等用户作答，返回每题答案（跳过为 None）。

        **没有超时**，只有两种情况会收尾——用户点了停止/会话被删
        （stop_event 置位），或前端把答案送了进来（AgentRuntime.answer_ask）。
        被中断时每题按"跳过"返回空答案，工具本身仍算成功：模型据此继续，而不是整个
        回合报错。这样"我问了但没人理"不会把会话卡死成错误态。
        """
        event = threading.Event()
        holder: dict[str, Any] = {
            "request_id": request_id,
            "tool_call_id": tool_call_id,
            "questions": questions,
            "answers": None,
            "event": event,
        }
        with self._ask_lock:
            self._pending_ask = holder
        _log(f"  ❓ 等待用户回答 {len(questions)} 个问题（{request_id[:8]}）")
        try:
            while not event.wait(ASK_WAIT_TICK):
                if self.stop_event.is_set():
                    _log("  ❓ 回合被停止，未答的问题按跳过处理")
                    break
            answers = holder["answers"]
        finally:
            with self._ask_lock:
                if self._pending_ask is holder:
                    self._pending_ask = None
        if answers is None:
            return [None for _ in questions]
        return answers

    def resolve_ask(self, answers: Any) -> dict[str, Any]:
        """把用户在卡片上选好的答案送进来，唤醒挂起的 ask_user（HTTP 线程调用）。"""
        with self._ask_lock:
            holder = self._pending_ask
        if holder is None:
            raise ValueError("当前没有等待回答的问题（可能已作答、已跳过或回合已结束）")
        try:
            clean = _normalize_ask_answers(answers, holder["questions"])
        except AgentToolError as exc:
            # HTTP 层按 ValueError → 409 处理，把校验原因原样返给界面
            raise ValueError(str(exc)) from exc
        with self._ask_lock:
            if self._pending_ask is not holder:
                raise ValueError("这道题刚刚已经结束了")
            holder["answers"] = clean
        holder["event"].set()
        _log(f"  ❓ 收到用户回答（{holder['request_id'][:8]}）")
        return {"ok": True, "answers": clean}

    # ---- 权限审批（工具执行前的门禁）----
    def _require_permission(self, name: str, args: dict[str, Any]) -> None:
        """按权限模式决定这次调用放不放行；要审批就阻塞等用户点。

        三种放行：模式本来就允许（见 _permission_needed）、本会话已勾过"本会话允许"、
        用户这次点了允许。被拒绝时抛 AgentToolError——模型因此收到一条"用户拒绝权限"
        的工具错误，可以换策略；不抛错就会把"没执行"当成"执行成功"。
        """
        risk = _tool_risk(name)
        mode = _normalize_permission_mode(self.state.permission_mode)
        if not _permission_needed(risk, mode):
            return
        if name in self.state.permission_grants:
            _log(f"  🔐 权限：{name} 本会话已放行，直接执行")
            return
        decision, deny_reason = self.request_permission(
            os.urandom(8).hex(), self._active_tool_call_id, name, args, mode
        )
        if decision == "allow-once":
            return
        if decision == "allow-session":
            self.state.permission_grants.add(name)
            return
        _log(f"  🔐 权限被拒（{decision}）：{name}" + (f"（原因：{deny_reason}）" if deny_reason else ""))
        raise AgentToolError(_permission_denied_reason(name, decision, deny_reason))

    def request_permission(
        self, request_id: str, tool_call_id: str, name: str, args: dict[str, Any], mode: str
    ) -> tuple[str, str]:
        """发起一次审批并**阻塞**等用户点，返回（答复, 拒绝原因）。

        答复 ∈ allow-once / allow-session / deny / stopped。第二个值是用户在卡上填的拒绝
        原因，只有 deny 且填了才非空——它会被拼进给模型的那句工具结果
        （见 _permission_denied_reason），所以"用户说不要"和"用户说不要、因为 X"是两回事。

        与 ask_user 同一套「回合线程挂起、HTTP 线程唤醒（resolve_permission）」，都**不设
        超时**：没人答就一直挂着，卡片一直在转录里等着（刷新、切走再回来都还在）。唯一差别
        是回合被停止时这里按拒绝收尾（"停下"的意思就是别做了），而 ask_user 按跳过继续。

        挂起前会顺手把「这次会改成什么」算一遍塞进事件（preview，见 _preview_tool_changes）：
        写类工具的那份 before→after 就这么提前摆到卡上。
        """
        risk = _tool_risk(name)
        safe_args = _sanitize_tool_args(args)
        event = threading.Event()
        holder: dict[str, Any] = {
            "request_id": request_id,
            "tool_call_id": tool_call_id,
            "name": name,
            "risk": risk,
            "arguments": safe_args,
            "decision": "",
            "event": event,
        }
        with self._perm_lock:
            self._pending_permission = holder
        # 挂起之前先把「这次会改成什么」算出来（只读，算不出就当没有）：写类工具把
        # before→after 直接摆到卡上，用户不必先展开工具行的原始参数才敢点允许。
        # 用**原始 args** 而不是展示用的 safe_args——预览要的是真数据（见 _preview_tool_changes）。
        preview = _preview_tool_changes(self, name, args)
        payload: dict[str, Any] = {
            "id": request_id,
            "tool_call_id": tool_call_id,
            "name": name,
            "label": _permission_tool_label(name),
            "risk": risk,
            "mode": mode,
            "arguments": safe_args,
        }
        if preview:
            payload["preview"] = preview
        self._emit("permission_request", payload)
        _log(f"  🔐 等待用户批准 {name}（模式 {mode}，不设超时）")
        try:
            while not event.wait(PERMISSION_WAIT_TICK):
                if self.stop_event.is_set():
                    _log("  🔐 回合被停止，权限请求按拒绝处理")
                    break
            decision = str(holder.get("decision") or "")
        finally:
            with self._perm_lock:
                if self._pending_permission is holder:
                    self._pending_permission = None
        if decision:
            return decision, str(holder.get("reason") or "")
        # 走到这里只有一种可能：回合被停止（否则 event 被 set 时必然带上了 decision）
        return "stopped", ""

    def apply_permission_mode(self, mode: Any) -> None:
        """改档（HTTP 线程调用）：下一次工具调用按新档判。

        常见情形是"卡已经弹出来了，用户这时才想起该切全自动"：那张卡按新档本来就会放行，
        让它自己作废比逼用户再点一次合理——记成 allow-once（不写本会话放行），与用户点了
        「允许一次」完全等价。更严格的档位则不动那张卡（用户还得回答一次，语义也没错）。

        换档同时清空本会话放行记录（见 _apply_permission_mode）：档位是信任级别，
        旧档下点过的「本会话允许」不该跨档沿用。
        """
        _apply_permission_mode(self.state, mode)
        with self._perm_lock:
            holder = self._pending_permission
        if holder is None:
            return
        name = str(holder.get("name") or "")
        if _permission_needed(_tool_risk(name), self.state.permission_mode):
            return
        holder["decision"] = "allow-once"
        holder["event"].set()
        _log(f"  🔐 模式改为 {self.state.permission_mode}，在等的 {name} 直接放行")

    def resolve_permission(self, decision: Any, reason: Any = "") -> dict[str, Any]:
        """把用户在审批卡上的选择送进来，唤醒挂起的请求（HTTP 线程调用）。

        reason 是卡上那个输入框里的"拒绝原因"（可选）：只有拒绝用得上，别的答复一律忽略
        （批准时说原因没意义）。它会随那条工具结果回给模型，见 _permission_denied_reason。
        """
        clean = str(decision or "").strip()
        if clean not in PERMISSION_DECISIONS:
            raise ValueError(
                f"未知的权限答复：{decision!r}（可选：{'、'.join(PERMISSION_DECISIONS)}）"
            )
        note = _normalize_permission_reason(reason) if clean == "deny" else ""
        with self._perm_lock:
            holder = self._pending_permission
        if holder is None:
            raise ValueError("当前没有等待批准的权限请求（可能已批准、已拒绝或回合已结束）")
        with self._perm_lock:
            if self._pending_permission is not holder:
                raise ValueError("这个权限请求刚刚已经结束了")
            holder["decision"] = clean
            if note:
                holder["reason"] = note
        holder["event"].set()
        _log(f"  🔐 收到权限答复 {clean}（{holder['request_id'][:8]}）" + (f"，原因：{note}" if note else ""))
        return {"ok": True, "decision": clean, "name": holder["name"], "reason": note}

    # ---- 工具实现（调本机 HTTP） ----
    def _project_id(self) -> str:
        return _encode_project_id(self.state.project_dir)

    def _http_get(self, path: str) -> Any:
        url = f"{self.base_url}{path}"
        return _http_json("GET", url)

    def _http_post(self, path: str, body: dict[str, Any]) -> Any:
        url = f"{self.base_url}{path}"
        return _http_json("POST", url, body)

    def _http_put(self, path: str, body: dict[str, Any]) -> Any:
        url = f"{self.base_url}{path}"
        return _http_json("PUT", url, body)
