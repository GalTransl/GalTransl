"""GalTransl Agent runtime.

一个 Agent 推理循环：读取选定的后端配置（OpenAI-Compatible）与项目，
用 OpenAI 官方 function-calling 接口驱动"先写字典后启动翻译"的标准流程。
工具通过本机 HTTP 调回现有 server.py 的 API，走和 UI 一样的代码路径。
不直接接触文件系统，所有写入经现有 API 的路径校验。

Agent 是一个持久的多轮会话：用户的第一条消息启动会话，之后 Agent 在后台
跑一个回合（可以调用任意多次工具直到自然收尾）；用户随时可以打断，或等
回合结束后继续发消息，Agent 在同一条对话历史上接着干。reset 才会清空。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import traceback
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from GalTransl.Agent import session_store
from GalTransl.Agent.session_store import SessionStore

DEFAULT_BACKEND_HOST = "127.0.0.1"
DEFAULT_BACKEND_PORT = 12333
DEFAULT_CONFIG_FILE = "config.yaml"
MAX_STEPS = 32
RUNTIME_EVENT_KEEP = 500

# ---- 上下文预算 ----
# 后端配置未指定 contextWindow 时的默认窗口（token）
DEFAULT_CONTEXT_WINDOW = 128_000
# 用量超过窗口的该比例就触发压缩
COMPACT_TRIGGER_RATIO = 0.80
# 压缩时保留最近 N 条消息原文不动
COMPACT_KEEP_RECENT_MSGS = 16
# 尾部预留：当前轮新增消息 + 模型输出
CONTEXT_RESERVE_TOKENS = 8_192
# 摘要生成的最大输出 token
SUMMARY_MAX_TOKENS = 2_048
# 粗略字符->token 换算系数（无 tokenizer 时的估算）
CHARS_PER_TOKEN = 4


def _log(msg: str, *args: object) -> None:
    """后端控制台调试日志。print + flush，确保即时可见。"""
    try:
        import time as _t

        ts = _t.strftime("%H:%M:%S")
        print(f"[{ts}] [Agent] {msg}", *args, flush=True)
    except Exception:  # noqa: BLE001 - 日志不能影响主流程
        pass


AGENT_SYSTEM_PROMPT = """你是 GalTransl 项目翻译助手 Agent。你接到一个 Galgame 翻译项目，需要自主驱动从准备字典到完成翻译再到质量复核的全流程，就像一个熟手用户在桌面端图形界面里操作一样。

# 你的身份
- 你只操作"当前选定的这一个项目"，不要假设有其他项目。
- 你通过调用工具完成所有操作，工具背后调用的是和图形界面完全相同的后端 API，你不会绕过校验。
- 你可以也应该在调用工具的同时用自然语言说明你的决策与思考（这一段会实时展示给用户）。

# 标准翻译流程（必须按此顺序推进）
1. **了解项目**：先调用 get_project_overview 看输入文件、缓存进度、配置。确认项目输入文件非空、配置里已设翻译引擎，再继续。
2. **字典准备（在启动翻译前必须完成）**：
   a. 调用 list_dict_files 查看项目已配置的译前/GPT/译后字典文件；
   b. 调用 read_dict 读取现有内容，判断人名、专有名词是否已收录；
   c. 若缺少人名表，调用 get_name_table；若返回为空，先调用 start_translation(translator="dump-name") 生成人名表（dump-name 是导出 name 字段的专用 translator），完成后再次 get_name_table 查看结果，再调用 save_name_table 写回（若需要修正译名）；
   d. 若 GPT 字典为空且项目较大，可调用 start_translation(translator="GenDic") 自动生成 GPT 字典，并在该任务 completed 后通过 list_dict_files/read_dict 确认生成结果。
3. **启动翻译**：字典就绪后，调用 start_translation(translator="<主翻译引擎>")。主翻译引擎从项目配置或 overview 中确认，常用值：ForGal-json / ForGal-tsv / ForNovel / sakura-v1.0 / galtransl-v3。一次只启动一个，项目已有运行中任务时不要重复提交。
4. **跟进进度**：调用 get_progress 或 get_runtime 轮询。翻译任务 completed 后再进入下一步；running 时用 wait 工具等待一段合理时间（如翻译任务就 wait minutes=1~3，短任务 wait seconds=30）后再查，不要连续空转轮询。等待期间界面会显示倒计时。
5. **复核结果**：调用 list_problems 查看自动检测到的翻译问题（残留日文、字典使用不当、过长等）。用 read_cache 的 index 参数精确读取有问题的条目（如 list_problems 返回的 index，可直接 `index="33-40,50-60"` 一次取多条）浏览实际译文。
6. **问题修复循环**：对能直接改译文的条目，用 patch_cache 一次批量修改多条（传 patches 数组，每条给 index 和要改的字段，如 pre_dst/proofread_dst），适合修正残留日文、明显错译；对需要字典约束的系统性问题，先 save_dict 补字典，再 start_translation(translator="rebuildr") 用更新后的字典重建结果（rebuildr 会跳过翻译、仅用译前/译后字典刷写结果 json）。patch_cache 与 rebuildr 可配合使用：先 patch 掉个别硬错，再 rebuildr 统一刷一遍字典相关的问题。重建/修改后再 list_problems 复核，直到问题数量显著下降。
7. **完成**：当翻译完成、问题数可控时，用一段自然语言总结本次操作（做了什么、翻译进度、剩余问题建议），不要调用工具，直接输出总结即可结束。

# 约束
- 每一步只调用必要的工具；能在一次工具调用里拿到的信息不要拆成多次。
- 不要在未准备字典的情况下直接启动主翻译。
- 不要连续重复调用同一个工具相同参数（避免死循环）；若上一步结果不理想，换策略或总结收尾。
- 工具返回的 error 要阅读并据此调整下一步，不要忽略。
- 你无法关闭程序、无法修改项目目录以外的文件、无法访问网络。只做翻译相关工作。
- 在启动翻译前，建议询问一下用户对你创建的字典是否满意，是否要开始翻译流程。
"""


# 系统提示词附加的多轮会话说明：Agent 可能被用户中途打断或在回合结束后
# 收到新指令，需要告诉它这是同一个会话里的交互，而不是全新任务。
AGENT_TURN_PROMPT = """
# 会话交互
- 这是一个多轮会话：用户可能中途打断你、也可能在你收尾后补充新指令。收到新消息时，接着当前的项目状态继续干，不要把已经完成的工作重来一遍。
- 用户打断（stopped）后你收到的新消息，先确认现场（比如 get_runtime / get_progress 看任务是否还在跑），再决定从哪里继续。
- 一次回复里把当前这轮指令做完：该调工具就调工具，做完用自然语言小结。除非用户另有要求，不要主动无限制地等待轮询。"""


# 压缩会话历史时用于生成摘要的提示词。摘要要保留"接着干下去"所需的硬信息，
# 而不是复述对话：文件路径、字典名、任务 id、问题条目 index 这些丢了就找不回来。
COMPACT_SUMMARY_PROMPT = """你在为一个 Galgame 翻译项目的 AI 助手压缩对话历史。下面是这个助手之前的工作记录，请把它压缩成一份摘要，供助手在后续对话中继续工作时参考。

要求：
1. 只输出摘要正文，不要任何前言、客套或"好的"之类的话。
2. 严格按下面的骨架输出 Markdown，每一节都要有内容（确实没有就写"无"）：
   ## 目标
   用户要完成的任务。
   ## 已完成的工作
   已经做完的关键操作（按时间顺序，简明）。
   ## 关键决策
   做过的重要选择及其原因（选了哪个翻译引擎、为什么改某个译名等）。
   ## 当前进度与项目状态
   项目现在处于什么状态：翻译是否在跑、进度如何、有哪些文件/字典已就绪。
   ## 待办与注意事项
   还没做的事、已知问题、下次继续时要注意的点。
3. 必须原样保留这些硬信息，不要概括掉：文件路径、字典文件名、翻译引擎名（如 ForGal-json / rebuildr）、任务 id、问题条目 index 或 index 区间、具体的译名修正。
4. 用中文写。简洁但不要丢信息。

<conversation>
{conversation}
</conversation>

请输出摘要："""

@dataclass(slots=True)
class AgentEvent:
    """单条 Agent 事件，会原样推给前端 SSE。"""

    type: str  # thought | thought_delta | thought_end | user_message | tool_call | tool_result | finish | error | stopped
    step: int
    data: dict[str, Any] = field(default_factory=dict)

    def to_sse(self) -> str:
        payload = {"type": self.type, "step": self.step, **self.data}
        return f"event: agent\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "step": self.step, **self.data}


@dataclass(slots=True)
class AgentState:
    status: str = "idle"  # idle | running | awaiting_input | stopped | failed
    goal: str = ""
    project_dir: str = ""
    config_file_name: str = ""
    backend_profile_data: dict[str, Any] = field(default_factory=dict)
    started_at: float = 0.0
    finished_at: float = 0.0
    error: str = ""
    events: deque[AgentEvent] = field(default_factory=lambda: deque(maxlen=RUNTIME_EVENT_KEEP))
    step: int = 0
    # 持久的多轮对话历史（OpenAI messages），跨回合保留，reset 才清空
    messages: list[dict[str, Any]] = field(default_factory=list)
    # 运行中收到的新消息先排队，Agent 在安全点（每次 LLM 调用前）取走；
    # 若滞留到回合收尾，pending_followup 置位由注册表开新回合消费
    pending_messages: deque[str] = field(default_factory=deque)
    pending_followup: bool = False
    # 本回合的收尾类型，SSE stream 据此判断是否还有后续（awaiting_input 不算终态）
    turn_end: str = ""
    # 会话身份：一个项目下可以有多个会话，互不干扰
    session_id: str = ""
    title: str = ""
    # 上次 LLM 响应的 prompt_tokens，作为上下文用量估算的锚点（0 表示未知）
    last_prompt_tokens: int = 0
    # 锚点对应的历史长度：锚点之后新增的消息要另外估算
    anchored_message_count: int = 0
    # 从磁盘恢复的会话标记（本次进程内还没跑过回合）
    restored: bool = False


class AgentToolError(Exception):
    """工具执行失败。"""


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
        self._openai_client: Any = None
        self._model: str = ""
        self._context_window = DEFAULT_CONTEXT_WINDOW
        self._compacted_this_turn = False
        # 会话落盘器：state 里没有 session_id（理论上不该发生）时退化为内存态
        self._store = SessionStore(state.project_dir, state.session_id) if state.session_id else None

    # ---- 事件 ----
    def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        self.state.step += 1
        event = AgentEvent(type=event_type, step=self.state.step, data=data)
        self.state.events.append(event)
        if self._store is not None:
            self._store.append_event(event.to_dict())

    def _persist_message(self, message: dict[str, Any]) -> None:
        """把一条消息追加进历史并落盘。所有 message append 都走这里。"""
        self.state.messages.append(message)
        if self._store is not None:
            self._store.append_message(message)

    # ---- OpenAI 客户端 ----
    def _resolve_llm(self) -> None:
        """从 backend_profile_data 解析出 OpenAI 客户端与模型名。"""
        _log("解析后端配置中…")
        profile = self.state.backend_profile_data or {}
        openai_section = profile.get("OpenAI-Compatible") or {}
        if not isinstance(openai_section, dict):
            raise RuntimeError("backend profile missing OpenAI-Compatible section")
        tokens = openai_section.get("tokens") or []
        if not isinstance(tokens, list) or not tokens:
            raise RuntimeError("backend profile OpenAI-Compatible.tokens is empty")
        first = tokens[0]
        if not isinstance(first, dict):
            raise RuntimeError("first token entry is not an object")
        token = str(first.get("token", "")).strip()
        endpoint = str(first.get("endpoint", "")).strip()
        model = str(first.get("modelName", "")).strip()
        if not token:
            raise RuntimeError("backend profile token is empty (请先在「翻译后端配置」页填写 token)")
        if not model:
            raise RuntimeError("backend profile modelName is empty (请先在「翻译后端配置」页填写 modelName)")
        # 上下文窗口：可选配置，缺省用默认值。用于压缩触发判断。
        self._context_window = _parse_context_window(first.get("contextWindow"))
        base_url = _normalize_endpoint(endpoint)
        masked = (token[:4] + "…" + token[-4:]) if len(token) > 8 else "***"
        _log(f"LLM 配置: model={model} endpoint={base_url} token={masked} context_window={self._context_window}")
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - 依赖缺失
            raise RuntimeError("openai 包未安装，Agent 无法运行") from exc
        self._openai_client = OpenAI(api_key=token, base_url=base_url)
        self._model = model
        _log("OpenAI 客户端就绪")

    # ---- 主循环 ----
    def run(self) -> None:
        """跑一个回合：从当前对话历史出发，直到模型不再调工具、用户停止或出错。

        对话历史由注册表维护（start 初始化首条、message 追加后续），
        run 只负责循环。回合结束后状态置为 awaiting_input，用户可继续
        发消息触发下一回合。
        """
        try:
            self._resolve_llm()
            if not self.state.messages:
                self._persist_message({"role": "system", "content": _build_system_prompt(self.state)})
                # user 消息只放用户的原始输入；项目目录/配置文件/目标这些环境
                # 上下文已经拼进上面的 system prompt，这里不再重复塞。
                first_user_text = self.state.goal or "按标准流程完成本项目的翻译。"
                self._persist_message({"role": "user", "content": first_user_text})
                # 首条用户消息也进事件流：SSE 全量回放（重开页面/状态对账）时
                # 气泡不丢。前端发送时已乐观显示，收到会按内容去重。
                self._emit("user_message", {"message": first_user_text})

            # 单回合步数上限：防止一轮内无限跑；超限不丢历史，用户可接着指挥
            for _ in range(MAX_STEPS):
                if self.stop_event.is_set():
                    self._end_turn("stopped", {"reason": "用户停止"})
                    return
                # 运行中用户插话：在这里注入，模型下一步就能看到
                injected = self._drain_pending_messages()
                if injected:
                    self._persist_message({"role": "user", "content": "\n".join(injected)})

                # 历史过长先压缩，避免下一步请求撑爆上下文窗口
                self._maybe_compact()

                loop_step = _ + 1
                _log(f"—— 第 {loop_step}/{MAX_STEPS} 轮：请求 LLM（流式）中…")
                req_started = time.time()
                content, tool_calls, finish_reason = self._stream_llm_response()
                streamed_content = bool(content)
                req_ms = int((time.time() - req_started) * 1000)
                _log(f"LLM 返回（耗时 {req_ms}ms）：content 长度={len(content)} tool_calls={len(tool_calls)} finish={finish_reason}")

                # 流结束立刻检查停止信号：流期间用户可能已点了停止
                if self.stop_event.is_set():
                    self._end_turn("stopped", {"reason": "用户停止"})
                    return

                # 截断保护：输出被 max_tokens 截断时，流式拼出来的工具参数可能是
                # "能解析但残缺"的半截 JSON，执行它会做出错误操作（pi 的做法）。
                # 这里直接丢弃本批工具调用，把失败写回历史让模型重试。
                if tool_calls and finish_reason == "length":
                    _log(f"  ⚠ 响应被截断（finish_reason=length），丢弃 {len(tool_calls)} 个工具调用")
                    self._persist_message({"role": "assistant", "content": content} if content else {"role": "assistant", "content": ""})
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
                # thought_delta 增量推送；仅当流期间没有发出过任何 delta 时才
                # 补发一条完整 thought（兜底非流式返回的 provider）。
                if content and not streamed_content:
                    preview = content if len(content) <= 120 else content[:117] + "…"
                    _log(f"  💭 思考: {preview}")
                    self._emit("thought", {"content": content})

                if not tool_calls:
                    # 收尾回复也要写进历史，下一轮对话才能看到 Agent 说过什么
                    self._persist_message({"role": "assistant", "content": content})
                    _log(f"无工具调用，回合完成，共 {self.state.step} 步")
                    self._end_turn("done", {"summary": content, "total_steps": self.state.step})
                    return

                # 把 assistant 这条消息原样追加（含 tool_calls），再逐个执行
                assistant_msg: dict[str, Any] = {"role": "assistant"}
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
                    后必须紧跟对应的 tool 消息，否则下一回合的请求不合法。"""
                    for tc2 in tool_calls:
                        if tc2["id"] not in responded:
                            self._persist_message({
                                "role": "tool",
                                "tool_call_id": tc2["id"],
                                "content": json.dumps({"error": "回合被用户停止", "status": "stopped"}, ensure_ascii=False),
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
                        self._persist_message({"role": "tool", "tool_call_id": call_id, "content": json.dumps({"error": err}, ensure_ascii=False)})
                        continue

                    args_preview = json.dumps(args, ensure_ascii=False)
                    if len(args_preview) > 160:
                        args_preview = args_preview[:157] + "…"
                    _log(f"  🔧 工具调用: {name}({args_preview})")
                    self._emit("tool_call", {"id": call_id, "name": name, "arguments": args})
                    started = time.time()
                    try:
                        result = self._dispatch_tool(name, args)
                        ok = True
                        duration_ms = int((time.time() - started) * 1000)
                        _log(f"  ✅ 工具结果: {name} 耗时 {duration_ms}ms")
                        self._emit("tool_result", {"id": call_id, "name": name, "ok": True, "result": result, "duration_ms": duration_ms})
                        content_str = json.dumps(result, ensure_ascii=False)
                    except AgentToolError as exc:
                        duration_ms = int((time.time() - started) * 1000)
                        _log(f"  ❌ 工具失败: {name} 耗时 {duration_ms}ms -> {exc}")
                        self._emit("tool_result", {"id": call_id, "name": name, "ok": False, "error": str(exc), "duration_ms": duration_ms})
                        content_str = json.dumps({"error": str(exc)}, ensure_ascii=False)
                    self._persist_message({"role": "tool", "tool_call_id": call_id, "content": content_str})

            # 超出单回合步数上限：收尾但保留历史，提示用户可以继续
            _log(f"超出最大步数 {MAX_STEPS}，回合收尾")
            self._persist_message({
                "role": "assistant",
                "content": f"本回合达到最大步数 {MAX_STEPS}，已暂停。等待用户下一步指示。",
            })
            self._end_turn(
                "done",
                {
                    "summary": f"本回合达到最大步数 {MAX_STEPS}，已暂停。你可以发消息让我继续。",
                    "total_steps": self.state.step,
                },
            )
        except Exception as exc:  # noqa: BLE001 - 顶层守护
            tb = traceback.format_exc()
            _log(f"❌ Agent 异常: {exc}\n{tb}")
            self._emit("error", {"message": str(exc), "traceback": tb})
            self.state.error = str(exc)
            self.state.status = "failed"
            self.state.turn_end = "failed"
        finally:
            self.state.finished_at = time.time()
            # 无论正常收尾还是异常退出，都要清掉落盘里的 running 标记
            if self._store is not None:
                self._store.append_meta(running=False)
            _log(f"Agent 回合结束，状态={self.state.status}，总步数={self.state.step}")
            if self.state.pending_followup:
                # 插话滞留到收尾（回合已停止消费），开新回合处理
                followup_runner = getattr(self, "_registry", None)
                if followup_runner is not None:
                    followup_runner._begin_followup(self.state.project_dir, self.state.session_id)

    def _end_turn(self, kind: str, data: dict[str, Any]) -> None:
        """收尾一个回合：发终态事件并落状态。awaiting_input 表示会话还活着。

        排队中的插话在收尾前冲进历史：回合线程已停止，不再有安全点消费它们，
        直接追加为 user 消息并通知调用方立即开新回合。
        """
        queued = self._drain_pending_messages()
        for msg in queued:
            self._persist_message({"role": "user", "content": msg})
        self.state.pending_followup = bool(queued)
        # 回合结束就把压缩标记清掉，下一回合重新评估上下文用量
        self._compacted_this_turn = False
        # 清掉落盘里的 running 标记：否则下次启动会误判"上次被中断"
        if self._store is not None:
            self._store.append_meta(running=False)
        if kind == "stopped":
            self._emit("stopped", data)
            self.state.status = "stopped"
        else:
            self._emit("finish", data)
            self.state.status = "awaiting_input"
        self.state.turn_end = kind

    def _drain_pending_messages(self) -> list[str]:
        """取走运行期间用户插话（无插话返回空列表）。"""
        if not self.state.pending_messages:
            return []
        msgs: list[str] = []
        while self.state.pending_messages:
            msgs.append(self.state.pending_messages.popleft())
        _log(f"  💬 注入用户插话 x{len(msgs)}")
        return msgs

    # ---- 流式 LLM 响应 ----
    def _stream_llm_response(self) -> tuple[str, list[dict[str, Any]], str]:
        """发起一次流式 chat.completions 请求，边收边推 thought_delta 事件。

        返回 (content, tool_calls, finish_reason)：
        - content：文本部分全文（流期间已通过 thought_delta 增量推送过）；
        - tool_calls：按 delta 顺序拼接好的调用列表，结构为
          [{id, name, arguments(str)}]；
        - finish_reason：stop / length / tool_calls 等，length 表示被截断。

        停止信号在流期间到达时立即弃流返回（上层会走 stopped 收尾），
        不再消费后续 chunk。
        """
        # include_usage 让 provider 在流末尾回一个 usage（部分兼容实现不认，
        # 抛错就退回到不带该参数重试一次）。usage 用于上下文用量锚点估算。
        try:
            stream = self._openai_client.chat.completions.create(
                model=self._model,
                messages=self.state.messages,
                tools=AGENT_TOOLS,
                tool_choice="auto",
                stream=True,
                stream_options={"include_usage": True},
            )
        except Exception:  # noqa: BLE001 - 兼容不支持 stream_options 的实现
            stream = self._openai_client.chat.completions.create(
                model=self._model,
                messages=self.state.messages,
                tools=AGENT_TOOLS,
                tool_choice="auto",
                stream=True,
            )

        content_parts: list[str] = []
        # index -> {id, name, arguments_parts}
        tool_calls_acc: dict[int, dict[str, Any]] = {}
        last_delta_emit = 0.0
        pending_delta = []  # 距上次 emit 攒下的文本（节流缓冲）
        finish_reason = ""

        for chunk in stream:
            if self.stop_event.is_set():
                _log("  流式响应被停止信号打断，弃流")
                break
            # usage 可能在 choices 为空的收尾 chunk 上单独到达
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                prompt_tokens = getattr(usage, "prompt_tokens", None)
                if isinstance(prompt_tokens, int) and prompt_tokens > 0:
                    self.state.last_prompt_tokens = prompt_tokens
                    self.state.anchored_message_count = len(self.state.messages)
            if not getattr(chunk, "choices", None):
                continue
            choice = chunk.choices[0]
            if getattr(choice, "finish_reason", None):
                finish_reason = str(choice.finish_reason)
            delta = choice.delta
            piece = getattr(delta, "content", None)
            if piece:
                content_parts.append(piece)
                pending_delta.append(piece)
                # 节流：文本增量最多 25ms 一条，避免 step 计数被 delta 刷爆
                now = time.monotonic()
                if now - last_delta_emit >= 0.025:
                    self._emit(
                        "thought_delta",
                        {"delta": "".join(pending_delta), "index": len(content_parts)},
                    )
                    pending_delta = []
                    last_delta_emit = now
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

        # 冲掉节流缓冲里剩下的文本
        if pending_delta:
            self._emit(
                "thought_delta",
                {"delta": "".join(pending_delta), "index": len(content_parts)},
            )
        # 告诉前端这一段文本流结束（前端据此撤掉打字机光标）
        if content_parts:
            self._emit("thought_end", {"length": len("".join(content_parts))})

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
    def _estimate_context_tokens(self) -> int:
        """估算当前历史占用的 token 数。

        抄 pi 的用法锚定法：有上一次响应的 prompt_tokens 作锚点时，只对锚点
        之后新增的消息按字符数估算；没有锚点就整体估算。不引入 tokenizer 依赖。
        """
        messages = self.state.messages
        anchor = self.state.last_prompt_tokens
        anchored = self.state.anchored_message_count
        if anchor > 0 and 0 <= anchored <= len(messages):
            tail = messages[anchored:]
            return anchor + sum(_estimate_message_tokens(m) for m in tail)
        return sum(_estimate_message_tokens(m) for m in messages)

    def _maybe_compact(self) -> None:
        """历史过长时压缩早期对话。每次回合最多压一次，失败降级为本地截断。

        触发线：估算用量 > 窗口的 COMPACT_TRIGGER_RATIO。
        """
        if self._compacted_this_turn:
            return
        window = self._context_window
        limit = int(window * COMPACT_TRIGGER_RATIO) - CONTEXT_RESERVE_TOKENS
        estimated = self._estimate_context_tokens()
        if estimated <= limit:
            return

        cut = _find_compaction_cut(self.state.messages, COMPACT_KEEP_RECENT_MSGS)
        if cut <= 0:
            _log(f"  ⚠ 上下文估算 {estimated} 超阈值 {limit}，但找不到安全切点，跳过压缩")
            return

        _log(f"  📦 上下文估算 {estimated} > {limit}，压缩前 {cut} 条消息")
        head = self.state.messages[:cut]
        tail = self.state.messages[cut:]

        summary = ""
        try:
            summary = self._summarize_messages(head)
        except Exception as exc:  # noqa: BLE001 - 摘要失败必须降级，不能卡死会话
            _log(f"  ⚠ 摘要生成失败，回退本地截断: {exc}")
        if not summary.strip():
            summary = _local_fallback_summary(head)

        self.state.messages = [
            {"role": "system", "content": _build_system_prompt(self.state, summary=summary)},
            {"role": "user", "content": "以上是之前对话的压缩摘要。请在此基础上继续，不要重复已完成的工作。"},
            *tail,
        ]
        # 压缩后旧的 usage 锚点失效，重置避免继续用错误的估算
        self.state.last_prompt_tokens = 0
        self.state.anchored_message_count = 0
        self._compacted_this_turn = True
        if self._store is not None:
            self._store.append_compact(removed=cut, summary_chars=len(summary), tokens_before=estimated)
        self._emit("compacted", {
            "removed": cut,
            "summary_chars": len(summary),
            "tokens_before": estimated,
        })
        _log(f"  📦 压缩完成：移除 {cut} 条，摘要 {len(summary)} 字符")

    def _summarize_messages(self, messages: list[dict[str, Any]]) -> str:
        """调 LLM 把一段历史压成结构化摘要（独立请求，非流式）。"""
        conversation = _serialize_for_summary(messages)
        prompt = COMPACT_SUMMARY_PROMPT.replace("{conversation}", conversation)
        resp = self._openai_client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=SUMMARY_MAX_TOKENS,
            stream=False,
        )
        if not getattr(resp, "choices", None):
            return ""
        content = getattr(resp.choices[0].message, "content", None)
        return str(content or "")

    # ---- 工具分发 ----
    def _dispatch_tool(self, name: str, args: dict[str, Any]) -> Any:
        handler = _TOOL_HANDLERS.get(name)
        if handler is None:
            raise AgentToolError(f"未知工具：{name}")
        return handler(self, args)

    # ---- 工具实现（调本机 HTTP） ----
    def _project_id(self) -> str:
        return _encode_project_id(self.state.project_dir)

    def _http_get(self, path: str) -> Any:
        url = f"{self.base_url}{path}"
        return _http_json("GET", url)

    def _http_post(self, path: str, body: dict[str, Any]) -> Any:
        url = f"{self.base_url}{path}"
        return _http_json("POST", url, body)


def _build_system_prompt(state: "AgentState", summary: str | None = None) -> str:
    """构造 system prompt：基础约束 + 当前项目环境 +（可选）对话压缩摘要。

    始终作为会话顶部唯一一条 system 消息。环境信息（项目目录/配置文件/目标）
    集中注入到 system prompt，对应的 user 消息只放用户的原始输入，避免重复。
    长会话压缩后调用方把摘要传进来，摘要块拼到末尾，这样压缩后仍能保持
    「基础约束 + 环境信息 + 摘要」三段结构，环境上下文不丢。
    """
    goal = state.goal or "按标准流程完成本项目的翻译"
    parts: list[str] = [AGENT_SYSTEM_PROMPT + AGENT_TURN_PROMPT]
    parts.append(
        "\n\n# 当前项目环境\n"
        f"- 项目目录：{state.project_dir}\n"
        f"- 配置文件：{state.config_file_name or DEFAULT_CONFIG_FILE}\n"
        f"- 本次目标：{goal}"
    )
    if summary and summary.strip():
        parts.append("\n\n# 会话摘要（早前对话已压缩）\n\n" + summary.strip())
    return "".join(parts)


def _parse_context_window(raw: Any) -> int:
    """解析后端配置里的 contextWindow。非法/缺失时用默认值。

    允许 "128000" / 128000 / "128k" 这几种写法。
    """
    if raw is None:
        return DEFAULT_CONTEXT_WINDOW
    try:
        if isinstance(raw, str):
            text = raw.strip().lower()
            if not text:
                return DEFAULT_CONTEXT_WINDOW
            if text.endswith("k"):
                value = int(float(text[:-1]) * 1000)
            else:
                value = int(float(text))
        else:
            value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CONTEXT_WINDOW
    # 明显不合理的值（负数、太小）当作没配
    if value < 1000:
        return DEFAULT_CONTEXT_WINDOW
    return value


def _estimate_message_tokens(message: dict[str, Any]) -> int:
    """单条消息的 token 粗估：正文 + tool_calls 的参数 JSON，按字符数/4。"""
    total_chars = 0
    content = message.get("content")
    if isinstance(content, str):
        total_chars += len(content)
    for tc in message.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        if isinstance(fn, dict):
            total_chars += len(str(fn.get("name") or ""))
            total_chars += len(str(fn.get("arguments") or ""))
    # 每条消息的固定开销（role/分隔符等）
    return total_chars // CHARS_PER_TOKEN + 4


def _is_tool_call_anchor(message: dict[str, Any]) -> bool:
    """该消息是否为 tool 响应（必须紧跟其 assistant.tool_calls）。"""
    return message.get("role") == "tool"


def _message_has_tool_calls(message: dict[str, Any]) -> bool:
    return bool(message.get("tool_calls"))


def _find_compaction_cut(messages: list[dict[str, Any]], keep_recent: int) -> int:
    """找出压缩切点：保留尾部 keep_recent 条，返回前缀长度。

    切点必须落在"安全位置"，否则会切断 assistant.tool_calls 与后续 tool 响应
    的配对，导致请求非法。具体规则：
    - 切点前一条不能是带 tool_calls 的 assistant（否则其 tool 响应被留下）；
    - 切点本身不能是 tool 响应（否则它的 assistant 被裁掉）。
    找不到安全位置就往前退，退到 0 表示放弃本次压缩。
    """
    total = len(messages)
    if total <= keep_recent + 1:
        return 0
    cut = total - keep_recent
    # 我们要保留 [cut:]，所以被裁掉的是 [0:cut]
    while cut > 0:
        prev = messages[cut - 1] if cut - 1 >= 0 else None
        nxt = messages[cut]
        # 前缀最后一条是带 tool_calls 的 assistant -> 会拆散它和它的 tool 响应
        if prev is not None and _message_has_tool_calls(prev):
            cut -= 1
            continue
        # 后缀第一条是 tool 响应 -> 它的 assistant 被裁掉了
        if _is_tool_call_anchor(nxt):
            cut -= 1
            continue
        break
    # 至少要有内容被裁掉，且尾部保留完整
    if cut <= 0:
        return 0
    return cut


def _serialize_for_summary(messages: list[dict[str, Any]]) -> str:
    """把一段消息序列化成供摘要模型阅读的文本（截断超长工具结果）。"""
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role") or "?"
        content = msg.get("content")
        text = content if isinstance(content, str) else ""
        calls = msg.get("tool_calls") or []
        if calls:
            names = []
            for tc in calls:
                fn = tc.get("function") or {} if isinstance(tc, dict) else {}
                names.append(str(fn.get("name") or "?"))
            text = (text + " " if text else "") + f"[调用工具: {', '.join(names)}]"
        if len(text) > 1500:
            text = text[:1500] + "…（已截断）"
        lines.append(f"{role}: {text}")
    return "\n".join(lines)


def _local_fallback_summary(messages: list[dict[str, Any]]) -> str:
    """摘要模型不可用时的兜底：只记录这段时间调用过哪些工具。"""
    tools: list[str] = []
    for msg in messages:
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {} if isinstance(tc, dict) else {}
            name = str(fn.get("name") or "")
            if name:
                tools.append(name)
    tool_text = "、".join(dict.fromkeys(tools)) if tools else "无"
    return (
        "## 目标\n（早前对话已压缩，详细目标见消息历史）\n\n"
        "## 已完成的工作\n"
        f"早前 {len(messages)} 条消息因上下文超限被压缩。期间调用过的工具：{tool_text}。\n\n"
        "## 关键决策\n无法从压缩中恢复，请根据当前项目状态判断。\n\n"
        "## 当前进度与项目状态\n请先查一次项目概览与运行状态重新确认当前进度。\n\n"
        "## 待办与注意事项\n"
        "如不确定之前的进展，先查一次项目状态再继续，避免重复已完成的操作。"
    )


def _normalize_endpoint(endpoint: str) -> str:
    """复用 COpenAI 的 endpoint 规范化逻辑：补 /v1、去掉 /chat/completions 尾巴。"""
    domain = endpoint.strip()
    if domain.endswith("/chat/completions"):
        domain = domain.replace("/chat/completions", "")
        base_path = ""
    else:
        base_path = "/v1" if not re.search(r"/v\d+", domain) else ""
    return domain.strip("/") + base_path


def _encode_project_id(project_dir: str) -> str:
    """与前端 encodeProjectDir 一致：UTF-8 base64url，去填充。"""
    from base64 import urlsafe_b64encode

    raw = project_dir.encode("utf-8")
    return urlsafe_b64encode(raw).decode("ascii").rstrip("=").replace("+", "-").replace("/", "_")


def _http_json(method: str, url: str, body: dict[str, Any] | None = None) -> Any:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    _log(f"  HTTP {method} {url}")
    http_started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
            ms = int((time.time() - http_started) * 1000)
            _log(f"  HTTP {method} {url} -> {resp.status} ({len(raw)} bytes, {ms}ms)")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        ms = int((time.time() - http_started) * 1000)
        try:
            err_body = json.loads(exc.read().decode("utf-8"))
            msg = err_body.get("error") or json.dumps(err_body, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            msg = f"HTTP {exc.code} {exc.reason}"
        _log(f"  HTTP {method} {url} -> {exc.code} ({ms}ms) 错误: {msg}")
        raise AgentToolError(msg) from exc
    except urllib.error.URLError as exc:
        ms = int((time.time() - http_started) * 1000)
        _log(f"  HTTP {method} {url} -> 连接失败 ({ms}ms): {exc}")
        raise AgentToolError(f"无法连接后端：{exc}") from exc


# ---- Agent 工具的 OpenAI function schema ----
AGENT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_project_overview",
            "description": "了解项目：列出输入/输出/缓存文件与当前翻译进度、项目配置。流程第一步，调用它确认项目可用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dict_files",
            "description": "列出项目配置的译前字典(preDict)、GPT字典(gpt.dict)、译后字典(postDict)文件及各文件内容。准备字典阶段使用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_dict",
            "description": "读取某个项目字典文件的完整内容（按 file_key，来自 list_dict_files 返回的 dict_contents 的 key）。",
            "parameters": {
                "type": "object",
                "properties": {"file_key": {"type": "string", "description": "字典文件 key，形如 (project_dir)项目GPT字典.txt"}},
                "required": ["file_key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_dict",
            "description": "写入/覆盖某个项目字典文件的内容。file_key 必须来自 list_dict_files；content 为 tab 分隔文本（格式：日文<Tab>中文[<Tab>解释]）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_key": {"type": "string"},
                    "content": {"type": "string", "description": "字典全文，覆盖写入"},
                },
                "required": ["file_key", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_dict_file",
            "description": "在项目里新建一个字典文件并登记到配置（pre/gpt/post 三类之一）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": ["pre", "gpt", "post"], "description": "pre=译前, gpt=GPT, post=译后"},
                    "filename": {"type": "string", "description": "字典文件名，如 项目GPT字典.txt"},
                },
                "required": ["category", "filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_name_table",
            "description": "读取 name替换表（人名表），返回 src_name/dst_name/count 列表。为空说明尚未生成。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_name_table",
            "description": "保存人名表（写入 name替换表.csv）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "names": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"src_name": {"type": "string"}, "dst_name": {"type": "string"}, "count": {"type": "integer"}},
                        },
                    }
                },
                "required": ["names"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_translation",
            "description": "提交一个翻译任务。translator 取值：ForGal-json/ForGal-tsv/ForNovel（主翻译）；GenDic（生成GPT字典）；dump-name（导出人名表）；rebuildr（用字典重建结果，跳过翻译）；rebuilda（用字典重建缓存+结果）。会复用当前选定的后端配置。",
            "parameters": {
                "type": "object",
                "properties": {"translator": {"type": "string"}},
                "required": ["translator"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_translation",
            "description": "停止当前项目正在运行的翻译任务。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait",
            "description": (
                "等待一段时间后继续。用于翻译/GenDic 等后台任务还在跑、需要隔一会儿再看进度的场景。"
                "等待期间界面会显示倒计时；若用户期间点了停止，会立即中断等待。"
                "单次最多等待 1800 秒（30 分钟）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "seconds": {
                        "type": "number",
                        "description": "等待的秒数。与 minutes 二选一；两个都传时以二者之和为准。",
                    },
                    "minutes": {
                        "type": "number",
                        "description": "等待的分钟数。适合等待较久的翻译任务。",
                    },
                    "reason": {
                        "type": "string",
                        "description": "可选。等待原因，会显示在界面上，如 '等待翻译任务完成'。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_progress",
            "description": "查询当前翻译进度（已翻译/总句数、问题数、失败数、各文件进度）。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_runtime",
            "description": "查询运行时状态：当前任务状态(running/completed/failed)、阶段、最近错误与成功、ETA。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_problems",
            "description": "列出自动检测到的翻译问题（残留日文、字典使用、过长等），用于复核与修复循环。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_cache",
            "description": "读取某个缓存文件的条目（译文）。filename 来自 get_project_overview 的缓存文件列表。留空 index 返回前 30 条；指定 index 只返回指定的条目。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                    "index": {
                        "type": "string",
                        "description": "可选。要读取的条目 index 列表，支持逗号和区间，如 \"33-40,50-60\"、\"5,9,12\"、\"100-105\"。留空返回前 30 条。",
                    },
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_cache",
            "description": "在缓存中搜索译文/原文/问题。query 为关键词，field 取 all/src/dst/problem。",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "field": {"type": "string", "enum": ["all", "src", "dst", "problem"]}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "patch_cache",
            "description": "批量修改某个缓存文件中若干条目的译文/校对等字段。一次可改多条，只更新 patches 里指定的条目与字段，其它条目原样保留。适合发现问题后改译文、再配合 rebuildr 重建的复核循环。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "缓存文件名，来自 get_project_overview 的缓存文件列表"},
                    "patches": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "index": {"type": "integer", "description": "要修改的条目 index"},
                                "pre_dst": {"type": "string", "description": "可选。新译文（机翻结果）"},
                                "proofread_dst": {"type": "string", "description": "可选。新校对译文（校对/润色结果，优先于 pre_dst）"},
                                "trans_by": {"type": "string", "description": "可选。标记译者，如 'manual' 或 'agent'"},
                                "trans_conf": {"type": "integer", "description": "可选。译文置信度 0-100"},
                                "doub_content": {"type": "string", "description": "可选。存疑内容备注"},
                                "unknown_proper_noun": {"type": "string", "description": "可选。未知专有名词备注"},
                            },
                            "required": ["index"],
                        },
                    },
                },
                "required": ["filename", "patches"],
            },
        },
    },
]


def _join_lines(lines: list[str]) -> str:
    return "\n".join(lines)


# ---- 工具实现 ----
def _tool_get_project_overview(runner: AgentRunner, _args: dict[str, Any]) -> Any:
    pid = runner._project_id()
    files = runner._http_get(f"/api/projects/{pid}/files")
    progress = runner._http_get(f"/api/projects/{pid}/progress")
    cfg = runner._http_get(f"/api/projects/{pid}/config?config={urllib.parse.quote(runner.state.config_file_name)}")
    return {
        "input_files": [f["name"] for f in files.get("input_files", [])],
        "output_files": [f["name"] for f in files.get("output_files", [])],
        "cache_files": [f["name"] for f in files.get("cache_files", [])],
        "progress": {
            "total": progress.get("total", 0),
            "translated": progress.get("translated", 0),
            "problems": progress.get("problems", 0),
            "failed": progress.get("failed", 0),
        },
        "config": cfg.get("config", {}),
    }


def _tool_list_dict_files(runner: AgentRunner, _args: dict[str, Any]) -> Any:
    pid = runner._project_id()
    cfg = urllib.parse.quote(runner.state.config_file_name)
    data = runner._http_get(f"/api/projects/{pid}/dictionary/project?config={cfg}")
    contents = data.get("dict_contents", {})
    summary = {key: val.get("count", 0) for key, val in contents.items()}
    return {
        "pre_dict_files": data.get("pre_dict_files", []),
        "gpt_dict_files": data.get("gpt_dict_files", []),
        "post_dict_files": data.get("post_dict_files", []),
        "line_counts": summary,
        "contents": {k: _join_lines(v.get("lines", [])) for k, v in contents.items()},
    }


def _tool_read_dict(runner: AgentRunner, args: dict[str, Any]) -> Any:
    file_key = str(args.get("file_key", "")).strip()
    if not file_key:
        raise AgentToolError("file_key is required")
    pid = runner._project_id()
    cfg = urllib.parse.quote(runner.state.config_file_name)
    data = runner._http_get(f"/api/projects/{pid}/dictionary/project?config={cfg}")
    contents = data.get("dict_contents", {})
    if file_key not in contents:
        available = list(contents.keys())
        raise AgentToolError(f"file_key 不存在：{file_key}。可选：{available}")
    entry = contents[file_key]
    return {"file_key": file_key, "lines": entry.get("lines", []), "count": entry.get("count", 0)}


def _tool_save_dict(runner: AgentRunner, args: dict[str, Any]) -> Any:
    file_key = str(args.get("file_key", "")).strip()
    content = str(args.get("content", ""))
    if not file_key:
        raise AgentToolError("file_key is required")
    pid = runner._project_id()
    body = {
        "config_file_name": runner.state.config_file_name,
        "file_key": file_key,
        "content": content,
    }
    return runner._http_post(f"/api/projects/{pid}/dictionary/project/save", body)


def _tool_create_dict_file(runner: AgentRunner, args: dict[str, Any]) -> Any:
    category = str(args.get("category", "")).strip()
    filename = str(args.get("filename", "")).strip()
    if category not in ("pre", "gpt", "post"):
        raise AgentToolError("category must be one of: pre, gpt, post")
    if not filename:
        raise AgentToolError("filename is required")
    pid = runner._project_id()
    body = {"config_file_name": runner.state.config_file_name, "category": category, "filename": filename}
    return runner._http_post(f"/api/projects/{pid}/dictionary/project/create", body)


def _tool_get_name_table(runner: AgentRunner, _args: dict[str, Any]) -> Any:
    pid = runner._project_id()
    return runner._http_get(f"/api/projects/{pid}/name-table")


def _tool_save_name_table(runner: AgentRunner, args: dict[str, Any]) -> Any:
    names = args.get("names", [])
    if not isinstance(names, list):
        raise AgentToolError("names must be an array")
    pid = runner._project_id()
    return runner._http_post(f"/api/projects/{pid}/name-table/save", {"names": names})


def _tool_start_translation(runner: AgentRunner, args: dict[str, Any]) -> Any:
    translator = str(args.get("translator", "")).strip()
    if not translator:
        raise AgentToolError("translator is required")
    body = {
        "project_dir": runner.state.project_dir,
        "config_file_name": runner.state.config_file_name,
        "translator": translator,
        "backend_profile_data": runner.state.backend_profile_data,
    }
    result = runner._http_post("/api/jobs", body)
    return {"job_id": result.get("job_id"), "status": result.get("status"), "translator": translator}


def _tool_stop_translation(runner: AgentRunner, _args: dict[str, Any]) -> Any:
    pid = runner._project_id()
    return runner._http_post(f"/api/projects/{pid}/stop", {})


WAIT_SECONDS_MAX = 1800  # 单次等待上限 30 分钟，避免 Agent 卡死在一次无限等待里
WAIT_TICK = 0.5  # 倒计时刷新步长（秒），兼顾界面流畅与轮询开销


def _tool_wait(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """等待指定时长。期间持续推 wait_tick 事件供界面显示倒计时。

    等待可被停止信号立即打断：先等满则 normal，被打断则 interrupted。无论哪种
    都以工具成功返回，把状态交给模型判断下一步，而不是抛错中断整个循环。
    """
    raw_seconds = args.get("seconds")
    raw_minutes = args.get("minutes")
    try:
        seconds = float(raw_seconds) if raw_seconds is not None else 0.0
        minutes = float(raw_minutes) if raw_minutes is not None else 0.0
    except (TypeError, ValueError):
        raise AgentToolError("seconds / minutes 必须是数字")
    if seconds < 0 or minutes < 0:
        raise AgentToolError("等待时长不能为负数")

    total = seconds + minutes * 60
    if total <= 0:
        raise AgentToolError("未指定等待时长：请给出 seconds 或 minutes")
    total = min(total, WAIT_SECONDS_MAX)

    reason = str(args.get("reason", "") or "").strip()
    total_ms = int(total * 1000)
    started = time.monotonic()
    _log(f"  ⏳ 开始等待 {total:g}s" + (f"（{reason}）" if reason else ""))
    runner._emit("wait_start", {"seconds": round(total, 1), "total_ms": total_ms, "reason": reason})

    interrupted = False
    while True:
        if runner.stop_event.is_set():
            interrupted = True
            break
        elapsed = time.monotonic() - started
        if elapsed >= total:
            break
        remaining_ms = max(0, total_ms - int(elapsed * 1000))
        runner._emit("wait_tick", {"remaining_ms": remaining_ms, "total_ms": total_ms})
        runner.stop_event.wait(WAIT_TICK)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    remaining_ms = 0 if interrupted else max(0, total_ms - elapsed_ms)
    runner._emit(
        "wait_end",
        {"interrupted": interrupted, "elapsed_ms": elapsed_ms, "remaining_ms": remaining_ms, "total_ms": total_ms},
    )
    if interrupted:
        _log(f"  ⏳ 等待被停止信号打断，已等 {elapsed_ms / 1000:.1f}s")
        return {"waited_seconds": round(elapsed_ms / 1000, 1), "status": "interrupted", "note": "等待被用户停止打断"}
    _log(f"  ⏳ 等待结束，共 {elapsed_ms / 1000:.1f}s")
    return {"waited_seconds": round(elapsed_ms / 1000, 1), "status": "completed"}


def _tool_get_progress(runner: AgentRunner, _args: dict[str, Any]) -> Any:
    pid = runner._project_id()
    return runner._http_get(f"/api/projects/{pid}/progress")


def _tool_get_runtime(runner: AgentRunner, _args: dict[str, Any]) -> Any:
    pid = runner._project_id()
    data = runner._http_get(f"/api/projects/{pid}/runtime")
    job = data.get("job") or {}
    summary = data.get("summary") or {}
    return {
        "job_status": job.get("status"),
        "job_translator": job.get("translator"),
        "stage": data.get("stage"),
        "current_file": data.get("current_file"),
        "summary": {
            "total": summary.get("total", 0),
            "translated": summary.get("translated", 0),
            "percent": summary.get("percent", 0),
            "problems": summary.get("problems", 0),
            "failed": summary.get("failed", 0),
            "eta_seconds": summary.get("eta_seconds"),
            "workers_active": summary.get("workers_active", 0),
        },
        "recent_errors": data.get("recent_errors", [])[:5],
    }


def _tool_list_problems(runner: AgentRunner, _args: dict[str, Any]) -> Any:
    pid = runner._project_id()
    cfg = urllib.parse.quote(runner.state.config_file_name)
    data = runner._http_get(f"/api/projects/{pid}/problems?config={cfg}")
    problems = data.get("problems", [])
    return {"total": data.get("total", len(problems)), "problems": problems[:50]}


def _tool_read_cache(runner: AgentRunner, args: dict[str, Any]) -> Any:
    filename = str(args.get("filename", "")).strip()
    if not filename:
        raise AgentToolError("filename is required")
    pid = runner._project_id()
    data = runner._http_get(f"/api/projects/{pid}/cache/{urllib.parse.quote(filename)}")
    entries = data.get("entries", [])
    index_spec = str(args.get("index", "") or "").strip()
    # 不指定 index：返回前 30 条，供 Agent 通览
    if not index_spec:
        return {"filename": filename, "count": len(entries), "returned": len(entries[:30]), "entries": entries[:30]}

    wanted = _parse_index_spec(index_spec)
    if not wanted:
        raise AgentToolError(f"无法解析 index 列表：{index_spec!r}（示例：33-40,50-60）")
    by_index = {int(e.get("index", -1)): e for e in entries if e.get("index") is not None}
    picked = [by_index[i] for i in sorted(wanted) if i in by_index]
    missing = sorted(i for i in wanted if i not in by_index)
    result: dict[str, Any] = {
        "filename": filename,
        "count": len(entries),
        "requested": sorted(wanted),
        "returned": len(picked),
        "entries": picked,
    }
    if missing:
        result["missing_indexes"] = missing
    return result


def _parse_index_spec(spec: str) -> set[int]:
    """解析 \"33-40,50-60\" / \"5,9,12\" / \"100-105\" 为 index 集合。"""
    result: set[int] = set()
    for part in spec.split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            bounds = token.split("-", 1)
            try:
                lo = int(bounds[0])
                hi = int(bounds[1])
            except ValueError:
                continue
            if lo > hi:
                lo, hi = hi, lo
            result.update(range(lo, hi + 1))
        else:
            try:
                result.add(int(token))
            except ValueError:
                continue
    return result


def _tool_search_cache(runner: AgentRunner, args: dict[str, Any]) -> Any:
    query = str(args.get("query", "")).strip()
    field = str(args.get("field", "all")).strip() or "all"
    if not query:
        raise AgentToolError("query is required")
    pid = runner._project_id()
    body = {
        "query": query,
        "field": field,
        "options": {"re": False},
        "max_results": 100,
        "config_file_name": runner.state.config_file_name,
    }
    return runner._http_post(f"/api/projects/{pid}/cache/search", body)


# patch_cache 允许更新的条目字段白名单（其余字段一律不动，避免误改 problem/preview 等派生字段）
_PATCHABLE_FIELDS = {
    "pre_dst",
    "proofread_dst",
    "trans_by",
    "trans_conf",
    "doub_content",
    "unknown_proper_noun",
}


def _tool_patch_cache(runner: AgentRunner, args: dict[str, Any]) -> Any:
    filename = str(args.get("filename", "")).strip()
    if not filename:
        raise AgentToolError("filename is required")
    patches_raw = args.get("patches")
    if not isinstance(patches_raw, list) or not patches_raw:
        raise AgentToolError("patches must be a non-empty array")
    pid = runner._project_id()

    # 读现有条目，按 index 建索引，只为命中的条目应用补丁，再整体写回。
    # /cache/save 会整体覆盖文件并由后端重建 problem/post_dst_preview，
    # 所以这里必须读全量 -> 改 -> 写全量，而非只写补过的几条。
    data = runner._http_get(f"/api/projects/{pid}/cache/{urllib.parse.quote(filename)}")
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        raise AgentToolError("缓存文件 entries 非数组，无法 patch")
    by_index: dict[int, dict[str, Any]] = {}
    for e in entries:
        idx = e.get("index")
        if idx is not None:
            try:
                by_index[int(idx)] = e
            except (TypeError, ValueError):
                continue

    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    not_found: list[int] = []
    changed_fields: list[str] = []
    for p in patches_raw:
        if not isinstance(p, dict):
            skipped.append({"index": None, "reason": "patch 不是对象"})
            continue
        idx = p.get("index")
        try:
            idx_i = int(idx)
        except (TypeError, ValueError):
            skipped.append({"index": idx, "reason": "index 不是整数"})
            continue
        entry = by_index.get(idx_i)
        if entry is None:
            not_found.append(idx_i)
            continue
        updates = {k: v for k, v in p.items() if k in _PATCHABLE_FIELDS and v is not None}
        if not updates:
            skipped.append({"index": idx_i, "reason": "无可更新字段（只允许 pre_dst/proofread_dst/trans_by/trans_conf/doub_content/unknown_proper_noun）"})
            continue
        entry.update(updates)
        applied.append({"index": idx_i, "fields": list(updates.keys())})
        for f in updates:
            if f not in changed_fields:
                changed_fields.append(f)

    if not applied:
        raise AgentToolError(
            f"没有条目被更新（applied=0, skipped={len(skipped)}, not_found={len(not_found)}）"
        )

    save_body = {
        "filename": filename,
        "entries": entries,
        "config_file_name": runner.state.config_file_name,
    }
    save_result = runner._http_post(f"/api/projects/{pid}/cache/save", save_body)
    return {
        "filename": filename,
        "applied": applied,
        "applied_count": len(applied),
        "not_found_indexes": not_found,
        "skipped": skipped,
        "changed_fields": changed_fields,
        "save": save_result,
    }


_TOOL_HANDLERS: dict[str, Callable[[AgentRunner, dict[str, Any]], Any]] = {
    "get_project_overview": _tool_get_project_overview,
    "list_dict_files": _tool_list_dict_files,
    "read_dict": _tool_read_dict,
    "save_dict": _tool_save_dict,
    "create_dict_file": _tool_create_dict_file,
    "get_name_table": _tool_get_name_table,
    "save_name_table": _tool_save_name_table,
    "start_translation": _tool_start_translation,
    "stop_translation": _tool_stop_translation,
    "wait": _tool_wait,
    "get_progress": _tool_get_progress,
    "get_runtime": _tool_get_runtime,
    "list_problems": _tool_list_problems,
    "read_cache": _tool_read_cache,
    "search_cache": _tool_search_cache,
    "patch_cache": _tool_patch_cache,
}


def _join_lines(lines: list[str]) -> str:
    return "\n".join(lines)


class AgentRuntime:
    """全局 Agent 注册表：一个项目下可以有多个会话，互不干扰。

    数据按 (project_dir, session_id) 组织。同一会话同时只跑一个回合（运行中
    再发消息走插话排队），但不同会话可以各自独立运行。

    会话落盘在 JSONL（session_store），进程重启后 status/drain_events/message
    会按需从磁盘懒加载恢复，所以关掉应用再打开还能接着聊，不用清空重来。
    """

    def __init__(self) -> None:
        # project_dir -> session_id -> state / runner / stop_event
        self._states: dict[str, dict[str, AgentState]] = {}
        self._runners: dict[str, dict[str, AgentRunner]] = {}
        self._stop_events: dict[str, dict[str, threading.Event]] = {}
        self._lock = threading.RLock()  # 可重入锁：允许在持锁时调用本类其它方法

    @staticmethod
    def _key(project_dir: str) -> str:
        return os.path.abspath(project_dir)

    # ---- 会话管理 ----

    def list_sessions(self, project_dir: str) -> list[dict[str, Any]]:
        """列出项目下的会话。内存里有状态的优先（可能还没落盘）。"""
        return session_store.list_sessions(project_dir)

    def create_session(self, project_dir: str, title: str = "") -> dict[str, Any]:
        """新建一个空会话（不启动回合），标题缺省用"项目名+序号"。"""
        base = os.path.basename(self._key(project_dir)) or "会话"
        resolved = title.strip() or session_store.next_session_title(project_dir, base)
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
        # 上次是运行中 -> 进程重启把回合中断了，标记为 stopped
        was_running = bool(meta.get("running"))
        state = AgentState(
            status="stopped" if was_running else "awaiting_input",
            goal=str(meta.get("goal") or ""),
            project_dir=project_dir,
            config_file_name=str(meta.get("config_file_name") or DEFAULT_CONFIG_FILE),
            backend_profile_data=meta.get("backend_profile_data") or {},
            started_at=float(meta.get("created_at") or 0.0),
            error="上次运行被应用重启中断" if was_running else "",
            messages=list(messages),
            step=max_step,
            session_id=session_id,
            title=str(meta.get("title") or session_id),
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
        _log(f"从磁盘恢复会话: project={key} session={session_id} messages={len(messages)} events={len(ev_deque)}")
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
        goal: str = "",
        session_id: str | None = None,
        host: str = DEFAULT_BACKEND_HOST,
        port: int = DEFAULT_BACKEND_PORT,
    ) -> dict[str, Any]:
        """启动一个回合。session_id 为空时新建会话（标题=项目名+序号）。"""
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
                title = existing.title if existing else sid

            stop_event = threading.Event()
            state = AgentState(
                status="running",
                goal=goal,
                project_dir=project_dir,
                config_file_name=config_file_name or DEFAULT_CONFIG_FILE,
                backend_profile_data=backend_profile_data or {},
                started_at=time.time(),
                session_id=sid,
                title=title,
            )
            runner = AgentRunner(state, host=host, port=port, stop_event=stop_event, registry=self)
            self._states.setdefault(key, {})[sid] = state
            self._runners.setdefault(key, {})[sid] = runner
            self._stop_events.setdefault(key, {})[sid] = stop_event
            # meta 落盘：记录会话身份与运行标记，重启后据此恢复
            if runner._store is not None:
                runner._store.append_meta(
                    project_dir=project_dir,
                    title=title,
                    goal=goal,
                    config_file_name=state.config_file_name,
                    created_at=state.started_at,
                    running=True,
                )
            thread = threading.Thread(target=runner.run, name=f"agent-{os.path.basename(key)}", daemon=True)
            thread.start()
            _log(f"Agent 回合已启动: project={key} session={sid} config={config_file_name} goal={goal[:60]}")
            return self.status(project_dir, sid)

    def message(self, project_dir: str, message: str, session_id: str | None = None) -> dict[str, Any]:
        """向会话追加一条用户消息。

        - 会话不存在（内存与磁盘都没有）：报错（前端应先 start/create）。
        - 正在运行：发 user_message 事件 + 插话排队，Agent 在下一个 LLM 调用前
          读到；若回合恰好正在收尾，插话转成 followup，自动开新回合消费。
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

            if state.status == "running":
                state.pending_messages.append(text)
                runner = self._runners.get(key, {}).get(sid)
                if runner is not None:
                    runner._emit("user_message", {"message": text})
                _log(f"Agent 运行中，消息已排队: session={sid} msg={text[:60]}")
                return self.status(project_dir, sid)

            # 回合已结束（可能是重启后恢复的）
            if state.pending_followup:
                state.pending_followup = False
            runner = self._runners.get(key, {}).get(sid)
            if runner is None:
                stop_event = threading.Event()
                runner = AgentRunner(state, stop_event=stop_event, registry=self)
                self._runners.setdefault(key, {})[sid] = runner
                self._stop_events.setdefault(key, {})[sid] = stop_event
            else:
                stop_event = threading.Event()
                runner.stop_event = stop_event
                self._stop_events.setdefault(key, {})[sid] = stop_event
            runner._persist_message({"role": "user", "content": text})

            state.status = "running"
            state.error = ""
            state.finished_at = 0.0
            state.turn_end = ""
            state.goal = state.goal or text  # 保留初始目标；为空时用首条后续消息补上
            runner._emit("user_message", {"message": text})
            thread = threading.Thread(target=runner.run, name=f"agent-{os.path.basename(key)}", daemon=True)
            thread.start()
            _log(f"Agent 继续回合: session={sid} msg={text[:60]}")
            return self.status(project_dir, sid)

    def stop(self, project_dir: str, session_id: str | None = None) -> dict[str, Any]:
        key = self._key(project_dir)
        with self._lock:
            sid = self._resolve_session_id(project_dir, session_id)
            event = self._stop_events.get(key, {}).get(sid) if sid else None
            if event:
                event.set()
            # 不直接改状态：回合线程（可能正阻塞在一次 LLM/工具调用里）稍后
            # 自己收尾落 stopped，并消费排队中的插话。期间 status 保持 running，
            # 此时到达的 message() 会走插话路径，最终由 followup 消费。
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
            return {
                "status": state.status,
                "project_dir": state.project_dir,
                "session_id": state.session_id,
                "title": state.title,
                "goal": state.goal,
                "step": state.step,
                "started_at": state.started_at,
                "finished_at": state.finished_at,
                "error": state.error,
                "events": [e.to_dict() for e in state.events],
            }

    def drain_events(self, project_dir: str, after_step: int = 0, session_id: str | None = None) -> list[dict[str, Any]]:
        """取 after_step 之后的所有事件，供 SSE 增量推送。"""
        sid = self._resolve_session_id(project_dir, session_id)
        if sid is None:
            return []
        state = self._get_state(project_dir, sid)
        if state is None:
            return []
        with self._lock:
            return [e.to_dict() for e in state.events if e.step > after_step]
