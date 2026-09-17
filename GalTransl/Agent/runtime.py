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

import copy
import json
import os
import random
import re
import threading
import time
import traceback
import urllib.parse
import urllib.request
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from GalTransl.Agent import session_store
from GalTransl.Agent.session_store import SessionStore
from GalTransl.ProblemWhiteList import parse_problem_white_list_entry

DEFAULT_BACKEND_HOST = "127.0.0.1"
DEFAULT_BACKEND_PORT = 12333
DEFAULT_CONFIG_FILE = "config.yaml"
# 单回合的 LLM 轮数上限（一轮 = 一次请求 + 它带回的那批工具调用）。
# 不设实际上限：主循环跑到"模型不再调工具"为止，收尾只由模型自身、工具批返回
# terminate、请求出错或用户点停止决定，正常任务不靠计数收尾。
# 这里仍留一个防呆值：万一模型陷入重复调用，不至于把回合线程挂到天荒地老。
# 正常长任务（几十上百轮：等待-查进度-修复的循环）根本碰不到它。
MAX_STEPS = 1000
RUNTIME_EVENT_KEEP = 500
# 单次 get_runtime 最多报几"类"新错误（同类会合并；发过的被水位线记住，不再重复报）
RUNTIME_ERRORS_PER_QUERY = 10
# 瞬态事件：只进当前回合的 SSE 流，不进内存 events deque。
# 两类：一类是流式增量（一次请求几十条，进 deque 会把 user_message/tool_call
# 等长期事件挤出 maxlen 窗口，前端刷新后就丢内容）；一类是能从状态快照重建的
# 实时指标（context_usage），刷新后由 status() 重新给出即可，不必占转录。
# 例外：子代理的逐步活动虽然也不进内存窗口，但要落盘 —— 见 _SUBAGENT_STEP_EVENTS。
_TRANSIENT_EVENT_TYPES = frozenset({
    "content_delta",
    "reasoning_delta",
    "wait_tick",
    "context_usage",
    "queue",
    # 子代理的逐步活动：一次派 16 个、每个十几轮，事件量能到上千条，进内存长期窗口会把
    # 主 Agent 的转录挤出去。所以只走实时流 + 落盘（见 _SUBAGENT_STEP_EVENTS）。
    "subagent_message",
    "subagent_tool_call",
    "subagent_tool_result",
    # 压缩的开始/结束只是过程指示：终态有 compacted（持久），刷新后由它重建即可。
    "compacting",
    # 子代理的退避重试只是过程指示：终态由 subagent_done 给出。
    "subagent_retry",
})

# 瞬态但**要落盘**的事件：子代理的逐步活动。它们不进内存 events 窗口（量太大，会把
# 主转录挤出去），但写进会话 JSONL——read_transcript 会一并回放，这样切页/刷新后
# 展开子代理还能看到它读了哪些文件、说了什么，而不是只剩 start/done 两条。
_SUBAGENT_STEP_EVENTS = frozenset({
    "subagent_message",
    "subagent_tool_call",
    "subagent_tool_result",
    "subagent_retry",
})

# ---- 子代理（subagent）的常量 ----
# 概念、实现与取舍见下面「子代理」那一节（在 _TOOL_HANDLERS 之前）。常量放在这里是因为
# AGENT_TOOLS 里的 run_subagents schema 要引用它们（MAX_TASKS 与角色清单要出现在工具描述里）。
SUBAGENT_AGENT_PROOFREAD = "proofread"
SUBAGENT_AGENT_EXPLORE = "explore"
SUBAGENT_AGENTS: tuple[str, ...] = (SUBAGENT_AGENT_PROOFREAD, SUBAGENT_AGENT_EXPLORE)
# 一次最多派几个（上限 16），同时也是并发上限
SUBAGENT_MAX_TASKS = 16
# 单个子代理的 LLM 轮数上限：防呆。正常校对十几轮足够，真跑满也算"干了活"，把已有报告交回去。
SUBAGENT_MAX_ROUNDS = 24
# 单个子代理回给主 Agent 的报告上限（字符）：一批 16 份报告不能把主 Agent 的上下文塞爆
SUBAGENT_REPORT_CHARS = 800
# 原文探索的报告放宽到 6000：它交回来的本来就是"字典候选 + 规范建议"的清单，800 字符装不下；
# 仍然要有上限——它是主 Agent 唯一会读到的产出，太长一样挤上下文。
SUBAGENT_EXPLORE_REPORT_CHARS = 6_000
# 子代理一次工具结果的回传上限（字符）：读缓存动辄几十条，超了截断并提示它分段读
SUBAGENT_TOOL_RESULT_CHARS = 24_000
# 子代理自己的历史压缩：保留段同样按 token 预算挑（见 _keep_recent_tokens）。子代理历史比
# 父会话短得多、任务也单一，预算取父会话（COMPACT_KEEP_RECENT_RATIO）的一半。
SUBAGENT_COMPACT_KEEP_RECENT_RATIO = 0.1
# 父回合等待子代理时的进度打印间隔（秒）
SUBAGENT_PROGRESS_TICK = 5.0

SUBAGENT_LABELS: dict[str, str] = {
    SUBAGENT_AGENT_PROOFREAD: "校对",
    SUBAGENT_AGENT_EXPLORE: "原文探索",
}

# ---- 权限（工具执行前的审批）----
# 四档模式，对应输入区那个选择器。审批在**后端**做：模型不知道当前是什么模式
# （也不会被告知），它只会在被拒绝时收到一条工具错误。
# - ask（每次询问，默认）：写操作一律先问；
# - accept-edits（允许编辑）：只自动放行"改译文数据"（缓存 / 字典 / 人名表），
#   改项目配置、改项目规范、启动翻译仍然要问；
# - auto（全自动）：全部放行；
# - auto-quiet（全自动-零打断）：放行规则与 auto 一模一样，差别只有一处——**ask_user
#   不再真的拦下来等人**：后端直接按模型给的「推荐选项」代答（见 _tool_ask_user），
#   用户完全不被打断。其余档位照旧：不把档位写进 system prompt，模型只会在被拒绝时
#   收到一条工具错误。
AUTO_QUIET_MODE = "auto-quiet"
PERMISSION_MODES: tuple[str, ...] = ("ask", "accept-edits", "auto", AUTO_QUIET_MODE)
DEFAULT_PERMISSION_MODE = "ask"
PERMISSION_MODE_LABELS: dict[str, str] = {
    "ask": "每次询问",
    "accept-edits": "允许编辑",
    "auto": "全自动",
    AUTO_QUIET_MODE: "全自动-零打断",
}
# 审批的三种答复（前端按钮）：只批这一次 / 本会话都批这个工具 / 拒绝
PERMISSION_DECISIONS: tuple[str, ...] = ("allow-once", "allow-session", "deny")
# **不设超时**：没人答就一直挂着（与 ask_user 一致），只有回合被停止才算拒绝。
# 以前是 120 秒到点自动拒绝（fail closed），但"离开一会儿回来发现已经被自动拒了、Agent
# 还顺着换了策略"比多等一会儿更糟——审批是该由人决定的事，不该由时钟决定。
# 等待答复的轮询步长（秒）：只为尽快响应停止信号
PERMISSION_WAIT_TICK = 0.2

# 风险分级（只分三档，够上面三种模式用）：
# - read：不改任何东西，任何模式都直接放行；
# - edit：改译文数据（缓存 / 字典 / 人名表）——"允许编辑"档自动放行的就是这些；
# - high：改项目设置 / 项目规范 / 启动任务——只有"全自动"放行。
PERMISSION_READ = "read"
PERMISSION_EDIT = "edit"
PERMISSION_HIGH = "high"

PERMISSION_TOOL_RISK: dict[str, str] = {
    "save_dict": PERMISSION_EDIT,
    "create_dict_file": PERMISSION_EDIT,
    "save_name_table": PERMISSION_EDIT,
    "patch_transl_cache": PERMISSION_EDIT,
    "delete_transl_cache": PERMISSION_EDIT,
    "update_project_config": PERMISSION_HIGH,
    "manage_problem_filter": PERMISSION_HIGH,
    "manage_problem_white_list": PERMISSION_HIGH,
    "write_project_guideline": PERMISSION_HIGH,
    "start_translation": PERMISSION_HIGH,
    # 派子代理：虽然它写的只是缓存里的"校对批注"（proofread_comment，改不了译文），但这是
    # 「要不要开始干这件事」——一次最多 16 个并行跑起来、每个都要调大模型、都会写缓存，
    # 让用户在派之前批一次（卡上能看到派给谁、看哪些文件）比事后发现跑歪了强。所以按
    # high 走：ask 与 accept-edits 都要问，只有两个全自动档直接放行。
    "run_subagents": PERMISSION_HIGH,
}
# 只读类工具：读 / 检索 / 等待 / 询问，外加"停止任务"——停止是安全方向的动作，
# 要停下来还得先点确认是最糟的设计，所以任何模式都直接放行。
PERMISSION_READ_TOOLS: frozenset[str] = frozenset({
    "get_project_overview",
    "list_input_files",
    "read_input_file",
    "search_input",
    "read_guideline",
    "list_dict_files",
    "read_dict",
    "get_name_table",
    "list_problems",
    "list_transl_cache",
    "read_transl_cache",
    "read_output",
    "search_transl_cache",
    "read_history_archive",
    "get_runtime",
    "wait",
    "ask_user",
    "stop_translation",
})
# 审批卡上显示的工具名（前端有自己的 TOOL_META，这里只需要一个可读的名字）
PERMISSION_TOOL_LABELS: dict[str, str] = {
    "save_dict": "保存字典",
    "create_dict_file": "新建字典",
    "save_name_table": "保存人名表",
    "patch_transl_cache": "修改译文",
    "delete_transl_cache": "删除缓存",
    "update_project_config": "修改项目配置",
    "manage_problem_filter": "管理问题过滤",
    "manage_problem_white_list": "管理问题白名单",
    "write_project_guideline": "修改项目规范",
    "start_translation": "启动翻译",
    "run_subagents": "派子代理",
}


def _tool_risk(name: str) -> str:
    """工具的风险档。没登记的工具按 high 处理（fail closed）：新增写类工具忘了
    登记时宁可多问一次，也不要默默改掉用户的项目。"""
    if name in PERMISSION_TOOL_RISK:
        return PERMISSION_TOOL_RISK[name]
    if name in PERMISSION_READ_TOOLS:
        return PERMISSION_READ
    return PERMISSION_HIGH


def _permission_tool_label(name: str) -> str:
    return PERMISSION_TOOL_LABELS.get(name, name)


def _normalize_permission_mode(value: Any) -> str:
    """前端送来的模式：不认识的（含空值）一律回落到默认的「每次询问」。"""
    mode = str(value or "").strip()
    return mode if mode in PERMISSION_MODES else DEFAULT_PERMISSION_MODE


def _apply_permission_mode(state: AgentState, mode: Any) -> bool:
    """换档：改掉档位并**清空本会话的放行记录**，返回是否真的换了。

    换档等于换一套信任级别，之前点过的「本会话允许」不再作数——从「每次询问」切到
    「允许编辑」再切回来时，旧放行还留着会让人以为档位没生效。反过来，**同档重复设置
    不算换档**（前端每回合都会带上当前档位），否则「本会话允许」活不过一个回合。
    """
    clean = _normalize_permission_mode(mode)
    if clean == state.permission_mode:
        return False
    state.permission_mode = clean
    if state.permission_grants:
        _log(f"换档为 {clean}，清空 {len(state.permission_grants)} 条本会话放行记录")
        state.permission_grants.clear()
    return True


def _permission_needed(risk: str, mode: str) -> bool:
    """这次调用要不要先请用户批准。读类永远不用；其余按模式矩阵判断。"""
    if risk == PERMISSION_READ:
        return False
    if mode in ("auto", AUTO_QUIET_MODE):
        return False
    if mode == "accept-edits":
        # 只自动放行"改译文数据"，配置/规范/启动任务仍要确认
        return risk != PERMISSION_EDIT
    return True


# 用户填的「拒绝原因」长度上限：一句话够模型换策略了，太长会把工具结果挤成一大段
PERMISSION_REASON_MAX = 500


def _normalize_permission_reason(value: Any) -> str:
    """用户填的拒绝原因：折行压成空格、去掉首尾空白、限长。没填（或不是字符串）给空串。"""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    if len(text) > PERMISSION_REASON_MAX:
        text = text[: PERMISSION_REASON_MAX - 1] + "…"
    return text


def _permission_denied_reason(name: str, decision: str, reason: str = "") -> str:
    """没批准时给模型看的那句话：说清"没执行"，并区分拒绝 / 回合被停。

    reason 是用户在拒绝时填的原因（可选，卡上那个输入框）：原样带上，模型据此换策略，
    不用去猜"用户为什么不要"。只有拒绝才有原因——"回合被停"时人根本没在答。
    """
    label = _permission_tool_label(name)
    if decision == "stopped":
        return f"回合被停止，本次「{label}」调用没有执行。"
    text = f"用户拒绝权限：本次「{label}」调用没有执行。"
    if reason:
        text += f"用户填写的拒绝原因：{reason}"
    return text

# ---- 上下文预算 ----
# 后端配置未指定 contextWindow 时的默认窗口（token）
DEFAULT_CONTEXT_WINDOW = 128_000
# 用量超过窗口的该比例就触发压缩
COMPACT_TRIGGER_RATIO = 0.80
# 压缩时保留的最近上下文按 **token 预算**挑（不是"最近 N 条"）：从尾部往前累加，装到装不下
# 为止。按条数挑的毛病是——最近 16 条里只要压着一条几万字符的工具结果，压缩就几乎降不下来，
# 界面上还会看着像"压完只剩摘要那么大"。对齐 PI-Desktop 的 keepRecentTokens：由触发线派生、
# 约两成、夹在 8K~64K 之间。
COMPACT_KEEP_RECENT_RATIO = 0.2
COMPACT_KEEP_RECENT_MIN_TOKENS = 8_000
COMPACT_KEEP_RECENT_MAX_TOKENS = 64_000
# 尾部预留：当前轮新增消息 + 模型输出
CONTEXT_RESERVE_TOKENS = 8_192
# 摘要生成的最大输出 token
SUMMARY_MAX_TOKENS = 2_048
# 粗略字符->token 换算系数（无 tokenizer 时的估算）
CHARS_PER_TOKEN = 4
# 压缩归档（chunk）里单条工具结果的上限（字符）：归档是给模型回查细节用的，
# 一条几万字符的缓存读取原样存进去只会让回查本身又撑爆上下文。
COMPACT_ARCHIVE_TOOL_RESULT_CHARS = 2_000
# 摘要消息里最多列几个历史归档（更早的只报数量）
COMPACT_ARCHIVE_MAX_LISTED = 10
# 压缩归档文件名模板
COMPACT_ARCHIVE_NAME = "chunk-{index:04d}.md"
# 一次 read_history_archive 最多回传多少字符（超长截断，提示改用关键词检索）
COMPACT_ARCHIVE_READ_CHARS = 8_000
# 消息上的内部标记：`_` 开头的键只在内存里用（压缩指令 / 摘要 / 归档标记），
# 发请求前一律剥掉——第三方 OpenAI 兼容端点收到陌生字段可能直接 400。
_INTERNAL_MSG_PREFIX = "_"

# ---- LLM 请求重试 ----
# 失败自动重试的上限（不含首次请求）与退避参数。重试由 runtime 自己掌控，
# 因此 SDK 内置的静默重试要关掉（见 _resolve_llm 的 max_retries=0）——否则
# 失败会被 SDK 在后台悄悄重发，用户界面上什么都看不到。
# 退避 1s/2s/4s/8s… 封顶 8s，10 次重试最长约 1 分钟。
LLM_MAX_RETRIES = 10
LLM_RETRY_INITIAL_DELAY_MS = 1_000
LLM_RETRY_MAX_DELAY_MS = 8_000
# 单次请求的「静默」上限（秒）。SDK 默认 600s：一条挂死的连接会让"停止"最多等
# 10 分钟才生效——回合线程阻塞在 read 上时，主循环没有任何机会去看停止信号。
# 收到 3 分钟：流式期间每个 chunk 都会重置 read 计时，正常生成不受影响；真挂死
# 了就尽快失败，上层随即看到停止信号按"用户停止"收尾（或按可重试错误退避重试）。
LLM_SILENCE_TIMEOUT = 180.0


def _log(msg: str, *args: object) -> None:
    """后端控制台调试日志。print + flush，确保即时可见。"""
    try:
        import time as _t

        ts = _t.strftime("%H:%M:%S")
        print(f"[{ts}] [Agent] {msg}", *args, flush=True)
    except Exception:  # noqa: BLE001 - 日志不能影响主流程
        pass


class AgentStopRequested(Exception):
    """重试退避等待期间收到停止信号：交给主循环按「用户停止」收尾。"""


class _ContextOverflow(Exception):
    """请求被 provider 判为超出上下文窗口（400 context too long）。

    不在原地重试——历史长度没变，重试多少次都一样。抛给主循环做一次强制压缩
    （含弹出尾部腾空间）后再重试原请求，见 run()。
    """


# 网络层关键词：SDK/网关把原因藏在文案里时的兜底识别
_NETWORK_ERROR_HINTS = (
    "connection",
    "connect",
    "network",
    "socket",
    "reset",
    "refused",
    "unreachable",
    "timed out",
    "timeout",
    "dns",
    "econn",
    "etimedout",
)


def _retry_after_ms_from_response(exc: BaseException) -> int | None:
    """从 429/5xx 响应的 Retry-After 头里取服务端建议的等待时长（毫秒）。"""
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    if headers is None:
        return None
    try:
        raw_ms = headers.get("retry-after-ms")
        if raw_ms is not None:
            return max(0, int(float(raw_ms)))
        raw_s = headers.get("retry-after")
        if raw_s is not None:
            return max(0, int(float(raw_s) * 1000))
    except (TypeError, ValueError):
        return None
    return None


def _classify_llm_error(exc: BaseException) -> dict[str, Any]:
    """把请求异常归一成 {code, message, retriable, status, retry_after_ms}。

    只重试「重试有意义」的瞬态错误（网络/超时/限流/5xx/流中断）；鉴权、参数、
    上下文超限这类重试多少次都一样的问题直接终态，避免浪费时间。判断优先看
    结构化字段（HTTP 状态码），再退回类名/文案关键词。
    """
    name = type(exc).__name__
    status = _status_code_of(exc)

    message = str(exc).strip() or name
    lowered = message.lower()
    # 是否来自 provider 传输层（openai / httpx）。自己代码里的 bug 不该重试。
    module = type(exc).__module__ or ""
    from_provider = module.startswith(("openai", "httpx", "httpcore"))

    def result(code: str, retriable: bool) -> dict[str, Any]:
        return {
            "code": code,
            "message": message[:600],
            "retriable": retriable,
            "status": status,
            "retry_after_ms": _retry_after_ms_from_response(exc),
        }

    if status is not None:
        if status in (401, 403):
            return result("PROVIDER_UNAUTHORIZED", False)
        if status == 404:
            return result("MODEL_NOT_CONFIGURED", False)
        if status == 413:
            return result("CONTEXT_TOO_LARGE", False)
        if status == 429:
            return result("RATE_LIMITED", True)
        if status in (408, 409) or status >= 500:
            return result("PROVIDER_ERROR", True)
        if status in (400, 422):
            if "context" in lowered or "token" in lowered:
                return result("CONTEXT_TOO_LARGE", False)
            return result("PROVIDER_BAD_REQUEST", False)
        return result("PROVIDER_ERROR", from_provider)

    if "rate" in lowered and "limit" in lowered:
        return result("RATE_LIMITED", True)
    if "timeout" in lowered or "timed out" in lowered:
        return result("TIMEOUT", True)
    if any(hint in lowered for hint in _NETWORK_ERROR_HINTS):
        return result("NETWORK_ERROR", True)
    if "stream" in lowered:
        return result("STREAM_FAILED", True)
    if "context" in lowered and ("length" in lowered or "window" in lowered):
        return result("CONTEXT_TOO_LARGE", False)
    # provider 抛出的未知错误按瞬态处理；自家代码的异常不重试
    return result("PROVIDER_ERROR", from_provider)


def _llm_retry_delay_ms(attempt: int, info: dict[str, Any]) -> int:
    """退避时长：优先后端给的 Retry-After，否则 1s/2s/4s… 倍增并封顶。"""
    server_delay = info.get("retry_after_ms")
    if isinstance(server_delay, int) and server_delay >= 0:
        return min(server_delay, LLM_RETRY_MAX_DELAY_MS)
    base = LLM_RETRY_INITIAL_DELAY_MS * (2 ** max(0, attempt - 1))
    return min(base, LLM_RETRY_MAX_DELAY_MS)


def _status_code_of(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    resp = getattr(exc, "response", None)
    candidate = getattr(resp, "status_code", None)
    return candidate if isinstance(candidate, int) else None


def _is_unsupported_param_error(exc: BaseException) -> bool:
    """判断异常是否属于「不认识 stream_options 这个可选参数」。

    限定在 400/404/422 这类参数错误里，且**必须点到该参数名**（写法不一：
    stream_options / stream options）。不再用 "invalid"/"unknown"/"unsupported" 这类
    泛关键词——各家的参数错误都长这样（DeepSeek 的 invalid_request_error 就是），
    误判会让同一次失败悄悄多发一次请求，还会遮住其它 400 的兜底分支。"""
    if _status_code_of(exc) not in (400, 404, 422):
        return False
    message = str(exc).lower()
    return "stream_options" in message or "stream options" in message


# 思考内容的字段名：不同平台不一样（DeepSeek 用 reasoning_content，OpenRouter 等用 reasoning）。
# 提取时按这个顺序试，回传时用命中的那个原名。
REASONING_FIELD_NAMES = ("reasoning_content", "reasoning")


def _requested_reasoning_field(exc: BaseException) -> str:
    """从 provider 的报错里认出它要的思考字段名（认不出返回空串）。

    DeepSeek 的原文是「The `reasoning_content` in the thinking mode must be passed back
    to the API」——把字段名抠出来，就能就地给老会话（历史里存的 assistant 消息还没带这个
    字段）补上再重试一次。
    """
    text = str(exc)
    lowered = text.lower()
    if "thinking mode" not in lowered and "reasoning_content" not in text:
        return ""
    for name in REASONING_FIELD_NAMES:
        if name in text:
            return name
    return REASONING_FIELD_NAMES[0] if "thinking mode" in lowered else ""


AGENT_SYSTEM_PROMPT = """你是 GalTransl 项目翻译助手 Agent。你接到一个 Galgame 翻译项目，需要自主驱动从准备字典到完成翻译再到质量复核的全流程，就像一个熟手用户在桌面端图形界面里操作一样。

# 你的身份
- 你只操作"当前选定的这一个项目"，不要假设有其他项目。
- 你通过调用工具完成所有操作，工具背后调用的是和图形界面完全相同的后端 API，你不会绕过校验。
- 你可以也应该在调用工具的同时用自然语言说明你的决策与思考（这一段会实时展示给用户）。

# 标准翻译流程
1. **了解项目**：先调用 get_project_overview 看翻译进度与项目配置（不传 include，一次拿全）。注意进度里的 total/translated 是「句数」且只统计已生成缓存的文件，translated==total 不等于整个项目翻完，整体是否翻完看 files_translated/files_total。再确认返回的 backend（agent = 本会话在用的后端，translator = 翻译任务会用的后端，各含配置名/类型/模型名）、项目确有输入文件（输入文件清单用 list_input_files 查），然后继续。之后再看进度时只传 include=["progress"]（必要时加 "backend"）：配置与配置键说明基本不变，不必重复拉。
2. **字典准备（在启动翻译前必须完成）**：
   a. 调用 list_dict_files 查看项目已配置的译前/GPT/译后字典文件；
   b. 调用 read_dict 读取现有内容，判断人名、专有名词是否已收录；
   c. **先把 GPT 字典补起来**：若 GPT 字典为空（或很薄）且项目较大，可调用 start_translation(translator="GenDic") 自动生成 GPT 字典，并在该任务 completed 后通过 list_dict_files/read_dict 确认生成结果；
   d. **再看人名表还缺什么**：调用 get_name_table 看现有的人名与译名——**它已经把这件事算给你了**：`dictionary.useGPTDictInName` 默认开着，**GPT 字典里已收录的名字/称呼在翻译时会自动用于 name 字段**，所以工具会把"译名为空、字典里有"的行按字典译名补上（带 `dst_name_source=gpt_dict`），**你真正要补的是返回里 `still_empty` 列出的那几个**（不必再往人名表里抄一遍字典里已经有了的），所以**先做完 c 走这一步能少补很多**——要补的通常只剩 GenDic 没抓到的（昵称、低频称呼、它认不出的写法）。若人名表本身还不存在（get_name_table 返回为空），先调用 start_translation(translator="dump-name") 把 name 字段导出来生成它（dump-name 是导出 name 字段的专用 translator），完成后再次 get_name_table 查看结果，再调用 save_name_table 写回（若需要修正译名）。
   **原文探索子代理（可选，explore）**：**很费 token，属于可选步骤**：派之前**必须用 ask_user 征得用户同意**（把"会读较多原文、比较费 token"说清楚），同意才派、不同意就不派；只读**原文**与 **GPT 字典**（不看译文、不写任何文件），干两件事——补齐 GenDic 覆盖不到的字典候选（昵称/爱称/绰号、地名组织道具、特殊称呼如お兄ちゃん、口癖，以及"同一个人被叫好几个名字"的判断），以及给出翻译规范建议（称谓与人称、文体语气、标点）。结论在它交回的报告里，由你汇总后落地：字典候选用 save_dict 进 GPT 字典，规范建议用 write_project_guideline 进项目规范。它要通读原文、通常 1-2 个。要 2 个就写**一条**任务：`{agent:"explore", file:"*", count:2}`——它会自动把原文均分成两份并行跑，brief 只写一遍（别把上千字的 brief 复制两条）。派之前先想清楚要它重点看什么，写进 brief 比它自己发挥准。
3. **试译定稿（全量翻译前必做，除非项目已有大量缓存）**：
   a. 调用 read_guideline 读取项目当前使用的翻译规范（配置 common.gpt.translation_guideline），理解文风要求；
   b. 调用 list_input_files 拿到文件清单与每个文件解析出的条数（sentences 是原文解析条数、文本插件还没过滤，估工作量偏大；它**不是进度**，别拿它判断文件翻没翻完），据此估整体工作量、挑 1-2 个有代表性的文件；再用 read_input_file 各读几十句（index 使用 1-based，区间如 "1-50"），掌握角色、语气、专有名词、场景类型；
   c. 基于原文补充 GPT 字典：把抽读中遇到的人名、专有名词、常见口语用 save_dict(action="append") 收录进项目 GPT 字典（只发新增行，不重发整份字典）——拿不准某个写法该不该收、该收哪个时，先用 search_input(query="…", context=2) 看它在全篇出现过几次、都在什么上下文（"译法统一"靠的正是这些出现处，别凭一次偶遇下结论）；要把这步做全（GenDic 漏掉的昵称、低频专有名词、特殊称呼，外加翻译规范建议），用流程 2 末尾那节「原文探索子代理」；
   d. 调用 start_translation(translator="<主翻译引擎>", files=["<一个代表性文件>"]) 只翻译这一个文件作为试译；
   e. 试译完成后用 read_transl_cache 阅读试译文件的译文，对照翻译规范评估文风、译名、语气是否达标；
   f. 若不满意：继续完善字典（save_dict）；对全局性的文风问题，用 write_project_guideline 把额外的翻译要求写进**项目规范**（如「译名统一用XX」「口语化程度、敬称的处理方式」等）——它会跟项目规范一起进每次翻译请求的 Prompt，下一次启动翻译就生效。写之前先 read_guideline(scope="project") 看已经写了什么：补充新要求用 append，改掉不合适的那条用 replace（旧那段原文要给全、确保唯一）；另外也可以用 update_project_config 切换 common.gpt.translation_guideline 换一份更合适的全局规范；
   g. 满意后，把试译结果告知用户并说明你的评估结论，然后用 ask_user 询问是否开始全量翻译（给出「开始全量」/「先再调一版规范」之类的候选选项），等用户回答后再进入下一步。
4. **启动翻译（全量）**：调用 start_translation(translator="<主翻译引擎>")（不传 files 即翻译全部）。主翻译引擎从项目配置或 overview 中确认，常用值：ForGal-json / ForGal-tsv / ForNovel / sakura-v1.0 / galtransl-v3。一次只启动一个，项目已有运行中任务时不要重复提交。
5. **跟进进度（wait 前后都要查状态）**：启动翻译后先调用 get_runtime 确认任务已在跑，再调用 wait 等待一段合理时间（翻译任务 wait minutes=1~3，短任务 wait seconds=30）。**优先把 start_translation 返回的 job_id 一起传进去**（如 wait(job_id="<id>", minutes=5)）：任务先跑完就立刻返回、不必等满时长（返回里 job_finished=true 说明是它先结束的）；时长先到而它还在跑，返回里会带上当前状态**外加一份运行时快照（等同 get_runtime，含 eta_seconds）**——有这份快照就直接用，不必再单独查一次。wait 结束后必须确认任务状态（快照已在返回里就不必重查）：completed 进入下一步；仍在 running 时看返回的 eta_seconds 估算剩余时间——eta 还很长（如 >10 分钟）就按其一半的时长继续 wait，快完了（如 <2 分钟）就 wait seconds=30 再查，不要连续空转轮询也不要一次等过头。等待期间界面会显示倒计时。（get_runtime 各字段与 recent_errors 的口径见该工具说明。）
6. **复核结果**：调用 list_problems（不带参数）先看类型统计，了解哪类问题最多；再传 problem_type（如 problem_type="残留日文"）+ limit/offset 分页查看该类型的具体条目。用 read_transl_cache 的 index 参数精确读取有问题的条目（如 list_problems 返回的 index，可直接 `index="33-40,50-60"` 一次取多条）浏览实际译文；判断语意是否连贯时传 context（如 context=3）把它上文的几句一起带上（带 context 的工具默认只给上文，要前后都给传 only_preceding=false；上下文行的 index 带 *，别拿它当本页要找的条目）。要查某个词/译名在全项目的所有出现处、判断译法是否统一（如「ドルード」该统一成哪个写法），用 search_transl_cache(query="ドルード", context=3) 一次看遍所有出现处及其上文。它默认只返回必要字段（说话人/原文/译文/问题，空值与未变化的字段会省略），要看译后字典替换结果或校对稿再传 fields。需要看缓存文件全貌（文件、条数）时用 list_transl_cache。
6.5 **派子代理（校对与润色，可选）**：**很费 token，属于可选步骤**：派之前**必须用 ask_user 征得用户同意**（把"会读较多原文、比较费 token"说清楚），同意才派、不同意就不派；用 run_subagents 一次派多个子代理并行干活，每个有自己的上下文与受限工具，跑完只交回一份报告（过程不进你的上下文）。这个阶段用的是**校对子代理（proofread）**：
   - **校对（proofread）**：每个负责一个（或一组）缓存文件，一次最多 16 个。file 填具体文件名就是点名；填 `"*"` 则**自动均分**——同批的 `"*"` 任务平分全部缓存文件（如派 16 个 `"*"`、256 个缓存文件 → 每个 16 个），要一次覆盖全部文件时用它，不用自己去数文件再逐个点名。大文件还能用 indexes 切区间。它们只能读 + 写缓存条目的 proofread_comment（校对批注：校对建议、润色建议都写这里），**改不了译文**：返回的 tasks[].doubts 带文件名与 index，报告是各自的总结（含"拿不准"的点）。拿到后按 7 的流程处理——读那些 index 的 proofread_comment，改完译文把该条的 proofread_comment 清空。**推荐在修复前跑一遍**。
   **派之前先用 ask_user 问清意见类型**：这一遍要它们写哪一类——「只写校对建议（错译/漏译/事实错误/不通这些硬伤）」「只写润色建议（没硬伤但中文能更好：翻译腔、口语不自然、用词单调、节奏拖沓）」「两者都要」——再把答案写进 brief（如 brief="本次只写润色建议，每条给具体改法；对话读起来要像人话"）。brief 里不写这句时它们默认只写校对建议；两类意见都写进 proofread_comment，同一条目只留一条，所以"两者都要"时要交代它们**硬伤优先**。
   派之前先想清楚要它们重点看什么，写进 brief 比它们自己发挥准。
7. **问题修复循环**：对能直接改译文的条目，用 patch_transl_cache 一次批量修改多条（传 patches 数组，每条给 index 和要改的字段，如 pre_dst/proofread_dst），适合修正残留日文、明显错译；对需要字典约束的系统性问题，先 save_dict 补字典，再 start_translation(translator="rebuilda") 用更新后的字典重建（rebuilda 会跳过翻译、用译前/译后字典刷写缓存+结果 json；不要用 rebuildr，它只刷结果 json 不更新缓存，list_problems 看不到变化）。patch_transl_cache 与 rebuilda 可配合使用：先 patch 掉个别硬错，再 rebuilda 统一刷一遍字典相关的问题。对译文质量差、patch 也救不回来的句子，可用 delete_transl_cache 按条目删除缓存（indexes 支持区间），再 start_translation 让这些句子重翻。重建/修改后再 list_problems 复核（同样先看统计、再按类型下钻），直到问题数量显著下降。问题过滤关键字是**正则**，但**原则上不要过滤大类、只过滤小类**：用 manage_problem_filter(action="add", keyword=["<正则>"]) 命中问题项即过滤——要写具体样式（如 `缺失.*标点`、`^残留日文：♪`），不要用 `残留日文`、`^残留日文：` 这类把整个大类藏起来的写法（大类里往往混着真问题，整类过滤等于放弃复核）；想按字面过滤某条，就把特殊字符转义。若某几条反复误报、不值得再改，用 manage_problem_white_list(action="add", entry=["<文件名>:<index>", …]) 按位置豁免（entry 支持 "01.json:12" 与 "01.json:12-15" 区间，可传数组），效果等同于给这几条勾上 skip_check：不再检测、不计入统计。
8. **完成**：收尾前先调用 get_project_overview 确认项目真的翻完——只有 files_translated == files_total 且没有 running 任务才算整体完成（total==translated 可能只代表已缓存的部分翻完，不要据此收尾）；若还有文件没翻，回到流程 4 继续 start_translation 翻剩余文件。若 list_problems 的统计里有**翻译失败**（失败的批次会把 problem 标成「翻译失败」、译文带 "(Failed)" 标记）：确认项目配置 `common.retranslKey` 里有没有「翻译失败」（get_project_overview 的 config 能看到，没有就 update_project_config 加上）：有的话**再启动一次 start_translation** 即可把这些句子重翻一遍。问题数可控、整体完成后，用 read_output 抽查最终输出文件（交付物；输出与缓存不完全一致，译后字典替换只在输出生效），确认无误后用一段自然语言总结本次操作（做了什么、翻译进度、剩余问题建议），不要调用工具，直接输出总结即可结束。

# 译前 / 译后字典（替换类字典）的用法
它们和 GPT 字典不是一回事：GPT 字典是随 Prompt 发给模型的"译法约束"（你最常维护的是这层），译前/译后字典是在文本**进出模型前后做机械替换**——译前字典把原文里的写法换掉再送给模型，译后字典把译文里的写法换回来。文件在 list_dict_files 的 pre_dict_files / post_dict_files 里（file_key 形如 `(project_dir)项目字典_译前.txt`），用 read_dict / save_dict 读写。每行是「查找词 + Tab + 替换词」（Tab 分隔，不是空格）；行首加 `^^` 表示只匹配句首、加 `1^` 表示只替换第一次出现，`//` 开头是注释，不加前缀就是全篇全量替换。

两个典型用法：

1. **人名/称呼在全篇是个变量或特殊写法**：例如男主在剧本里一律写作 `$name`
   - 译前字典加一行：`$name` → `张三`
   - 译后字典加一行：`张三` → `$name`
   - 效果：模型全程按"张三"翻译（称谓、语气、上下文都自然），而缓存与交付文件里仍然是脚本要的 `$name`，变量不会被翻坏或翻丢。
   - 注意别误伤：译后把中文名换回变量时，如果这个中文名在别处也会作为普通词出现，就不建议用这个词。
2. **全篇反复出现的长控制符**（例如每句都挂着同一大串 `<...>` 之类的标记）：
   - 译前把它换成一个**又短又独特**的占位符（如 `<C1>`，先确认原文里不会自然出现这种写法），译后再把占位符换回原来那串。
   - 好处：模型不必每次照抄那一长串东西，既省 token，也少一次抄错/漏字的机会；交付文件里仍是原始标记。
   - 占位符要"独特"：别用会被模型顺手翻译或改写的常见词、中文词；同一个占位符全篇固定对应同一段控制符，不要一号多用。

生效方式（改完得让翻译跑一遍才算数）：
- 译前字典改的是**原文**：受影响句子的 post_src 变了 → 缓存直接未命中、需要**重新翻译**，用 start_translation 跑主翻译引擎重翻这些句子即可（rebuilda 不翻译，碰到它们会报"缓存未命中"）。
- 译后字典只改**译文**：start_translation(translator="rebuilda") 就能重刷（跳过翻译、重跑替换，缓存与输出一起更新）。
- `name`（说话人）字段默认**不吃**译前/译后字典：要让人名在 name 字段里也跟着替换，用 update_project_config 打开 `dictionary.usePreDictInName` / `dictionary.usePostDictInName`（GPT 字典对 name 默认是开的，见 `useGPTDictInName`）。

# 约束
- list_transl_cache / list_input_files / list_problems / read_input_file / read_transl_cache 的返回是 **Markdown 表格 + 文字说明**：开头一段文字是计数与提示（共多少、是否采样、缺哪些 index 等），随后的表格第一行是列名、每行一条数据；单元格里的换行写作 `<br>`、竖线转义为 `\\|`，空单元格就是没值。单元格里的 `<br>` 就是换行——与翻译管线送翻时的写法一致；用 patch_transl_cache 写回时写 `<br>`、真换行或字面 \\n 都可以，落盘前会统一成该条目原有的换行形式。
- 每一步只调用必要的工具；能在一次工具调用里拿到的信息不要拆成多次。重复查看同类信息时用工具的分段参数（如 get_project_overview 的 include）只取变化的部分，别把基本不变的配置/说明反复拉一遍。
- 要把某条缓存（原文 + 译文，或几条）摆给用户看时，在回复里**单独一行**写 `$transl_cache("<缓存文件名>", <行号>)`：文件名来自 list_transl_cache，行号是缓存条目的 index，可写区间 `12-15` 或逗号列表 `12,20`。界面会把它渲染成那几行缓存的卡片，比自己把原文译文抄一遍清楚、也不会抄错。不要把它写进代码块，也不要加额外解释行。
- 翻译规范有两份：全局规范（translation_guidelines 目录里选的那份，通用规则）和**项目规范**（项目目录里的 `translation_guideline.md`，本项目专属，跟项目一起走）。翻译时两份拼在一起、项目规范在后，冲突以项目规范为准。读项目规范用 read_guideline(scope="project")；用户提出新的术语/称呼/语气要求时，先看项目规范里是否已经写过，再用 write_project_guideline 改：新增要求用 append，旧规则要改成新的用 replace（把旧那段原文给全，确保唯一），整套重写才用 overwrite。改完在**下一次启动翻译**时生效，正在跑的翻译不受影响；别在同一份规范里堆互相矛盾的规则。
- 写类工具（改配置 / 项目规范 / 字典 / 人名表 / 缓存、管问题过滤）和 start_translation 都带一个可选参数 `reason`：**尽量填**一句"为什么这么做"（依据或要解决的问题，如「第 33 句残留日文：按人名表统一为『多鲁德』」「试译已定稿，开始全量」）。界面会把它显示在那条改动的变更卡里给用户复核；启动翻译的还会出现在权限审批卡上——全量启动这类动作，用户在批之前要先看到理由。不用再重复改了哪些内容，changes / diff 已经列出了。
- 不要在未准备字典的情况下直接启动主翻译。
- 不要连续重复调用同一个工具相同参数（避免死循环）；若上一步结果不理想，换策略或总结收尾。
- 工具返回的 error 要阅读并据此调整下一步，不要忽略。其中「用户拒绝权限：…」不是故障，是用户的决定：不要反复重试同一个调用，换一个不需要动它的做法，或说明情况收尾。
- 不确定该不该做（要不要动这个文件、要不要重翻）、或不确定该怎么翻译（用词/称谓/语气取舍）时，用 ask_user 提问并等回答，别自己猜；能直接从项目配置、字典或原文里判断出来的不要问。
- 你无法关闭程序、无法修改项目目录以外的文件、无法访问网络。只做翻译相关工作。
- 在启动全量翻译前，必须先完成试译定稿（流程 3），并把试译评估结论告知用户、确认后再全量启动。
"""


# 系统提示词附加的多轮会话说明：Agent 可能被用户中途打断或在回合结束后
# 收到新指令，需要告诉它这是同一个会话里的交互，而不是全新任务。
AGENT_TURN_PROMPT = """
# 会话交互
- 这是一个多轮会话：用户可能中途打断你、也可能在你收尾后补充新指令。收到新消息时，接着当前的项目状态继续干，不要把已经完成的工作重来一遍。
- 用户打断（stopped）后你收到的新消息，先确认现场（ get_runtime 看任务是否还在跑），再决定从哪里继续。
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
3. 必须原样保留这些硬信息，不要概括掉：文件路径、字典文件名、翻译引擎名（如 ForGal-json / rebuilda）、任务 id、问题条目 index 或 index 区间、具体的译名修正。
4. 用中文写。简洁但不要丢信息。

<conversation>
{conversation}
</conversation>

请输出摘要："""

# Insert-then-Compress 用的指令（见 _begin_compaction）。
# 它不单独发一次「摘要请求」，而是作为一条**瞬时消息**拼在当前会话末尾，让下一轮
# 正常请求带着它一起发出去——system prompt / tools / 历史前缀全部复用，摘要调用
# 本身也能命中提示缓存。代价是必须把话说死：模型手上的上下文里全是"继续干活"的
# 暗示，稍微含糊一点它就会接着调工具，而不是老实压缩。
COMPACT_INSTRUCTION_PROMPT = """═══════════════════════════════════════════════
任务切换：记忆压缩模式（IMPORTANT）
═══════════════════════════════════════════════
上面的对话**已经结束**，你现在处于记忆压缩模式。严格执行：

1. 这不是继续对话；
2. **不要**执行上面提到的任何请求；
3. **不要**调用任何工具（tool_calls 必须为空）；
4. 你的回复必须是**纯文本**。

你唯一的任务：把上面的对话压缩成一份摘要。

输出格式（严格遵守）：
先输出一行 <topics>3-6 个关键主题短语，逗号分隔</topics>
再用 <summary></summary> 包住摘要正文。

摘要必须保留"接着干下去"所需的硬信息，按下面的骨架写：
## 目标
## 已完成的工作
## 关键决策
## 当前进度与项目状态
## 待办与注意事项

必须原样保留、不要概括掉：文件路径、字典文件名、翻译引擎名（如 ForGal-json）、任务 id、问题条目 index 或区间、具体的译名修正。
用中文写，简洁但不丢信息。现在开始，直接输出 <topics> 与 <summary>。"""


def _parse_compact_summary(content: str) -> str:
    """从压缩响应里取摘要正文。

    优先取 <summary>…</summary>；模型没按格式走时退而用整段文本（除了 <topics> 行），
    总比因为格式瑕疵白压一次强。
    """
    text = str(content or "").strip()
    if not text:
        return ""
    match = re.search(r"<summary>(.*?)</summary>", text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    text = re.sub(r"<topics>.*?</topics>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    text = re.sub(r"</?summary>", "", text, flags=re.IGNORECASE).strip()
    return text


def _parse_compact_topics(content: str) -> str:
    match = re.search(r"<topics>(.*?)</topics>", str(content or ""), re.DOTALL | re.IGNORECASE)
    return " ".join(match.group(1).split())[:200] if match else ""


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
    goal: str = ""
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
        self._emit_lock = threading.Lock()
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

    # ---- 事件 ----
    def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        with self._emit_lock:
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

    # ---- 排队消息（界面上的队列面板）----
    def emit_queue(self) -> None:
        """把当前队列整份推给界面。

        整份推送而不是增量：队列很小，整份最不容易出错，前端也不用做对账。
        走瞬态事件（刷新后由 status().queued 兜底）。
        """
        self._emit("queue", {"queued": [m.to_dict() for m in self.state.pending_messages]})

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
        # 上下文窗口：可选配置，缺省用默认值。用于压缩触发判断与界面用量指示。
        self._context_window = _profile_context_window(profile)
        self.state.context_window = self._context_window
        base_url = _normalize_endpoint(endpoint)
        # 提示缓存断点：后端配置里可选（auto/on/off），缺省 auto——只在 Anthropic 系
        # 的端点上注入 cache_control，其余（DeepSeek 等）靠服务端自动前缀缓存。
        self._prompt_caching = _resolve_prompt_caching(
            openai_section.get("promptCaching"), model, base_url
        )
        masked = (token[:4] + "…" + token[-4:]) if len(token) > 8 else "***"
        _log(
            f"LLM 配置: model={model} endpoint={base_url} token={masked} "
            f"context_window={self._context_window} prompt_caching={self._prompt_caching}"
        )
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - 依赖缺失
            raise RuntimeError("openai 包未安装，Agent 无法运行") from exc
        # max_retries=0：关掉 SDK 的静默重试，改由 _stream_llm_response 自己按
        # 退避重试，这样每一次重试都能作为事件推给界面（否则用户只看到长时间卡住）。
        # timeout：默认 600s 的静默上限太长，停止要等这么久才生效（见 LLM_SILENCE_TIMEOUT）。
        # 客户端是"每回合一份"：run() 第一步都会走到这里重建。上一回合若被停止打断，
        # 那份已被 abort_in_flight() 关掉，复用会直接抛 RuntimeError（见该方法注释）。
        self._openai_client = OpenAI(
            api_key=token, base_url=base_url, max_retries=0, timeout=_llm_timeout()
        )
        self._model = model
        _log("OpenAI 客户端就绪")

    def abort_in_flight(self) -> None:
        """打断在途的 LLM 请求（等价于给请求发一个 abort 信号）。

        支持取消的 HTTP 栈能把 abort 一路传进请求层，在那里真实中断在途连接。
        Python 的 OpenAI SDK 没有可传的 signal，等价手段是关掉这份客户端：httpx 会让
        阻塞在 socket read 上的请求立刻抛 APIConnectionError（本地用"黑洞"服务实测：
        close() 0ms 返回、在途读立即被打断）。主循环随即看到停止信号、按「用户停止」
        收尾，不必干等 read 超时（以前是 600s，现在 LLM_SILENCE_TIMEOUT 兜底）。

        只关当前这一份，且 close() 不阻塞，可以在 HTTP 处理线程里同步调用。
        """
        client = self._openai_client
        if client is None:
            return
        try:
            client.close()
        except Exception as exc:  # noqa: BLE001 - 关不掉不影响停止语义（仍有静默超时兜底）
            _log(f"关闭 LLM 客户端失败（忽略，仍按停止收尾）: {exc}")

    # ---- 主循环 ----
    def run(self) -> None:
        """跑一个回合：从当前对话历史出发，直到模型不再调工具、用户停止或出错。

        对话历史由注册表维护（start 初始化首条、message 追加后续），
        run 只负责循环。回合结束后状态置为 awaiting_input，用户可继续
        发消息触发下一回合。
        """
        turns = 0  # 本回合真实 LLM 请求次数；state.step 是事件计数（含流式 delta），不代表轮数
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
                        # 大清单工具：事件与模型消息给同一段 Markdown 文本（前端 formatPayload 对字符串原样显示）
                        rendered = _render_tool_result_table(name, result)
                        self._emit(
                            "tool_result",
                            {
                                "id": call_id,
                                "name": name,
                                "ok": True,
                                "result": rendered if rendered is not None else result,
                                "duration_ms": duration_ms,
                            },
                        )
                        content_str = rendered if rendered is not None else json.dumps(result, ensure_ascii=False)
                    except AgentToolError as exc:
                        duration_ms = int((time.time() - started) * 1000)
                        _log(f"  ❌ 工具失败: {name} 耗时 {duration_ms}ms -> {exc}")
                        self._emit("tool_result", {"id": call_id, "name": name, "ok": False, "error": str(exc), "duration_ms": duration_ms})
                        content_str = json.dumps({"error": str(exc)}, ensure_ascii=False)
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
            # 无论正常收尾还是异常退出，都要清掉落盘里的 running 标记
            if self._store is not None:
                self._store.append_meta(running=False)
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
        messages = self._messages_for_request()
        tools = AGENT_TOOLS
        if self._prompt_caching:
            # 只对认这套断点的后端注入（见 _resolve_prompt_caching）：Anthropic 系
            # 需要显式 cache_control，OpenAI 兼容的多数实现是服务端自动前缀缓存。
            messages, tools = _apply_prompt_cache(messages, tools)
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

    def _messages_for_request(self) -> list[dict[str, Any]]:
        """发请求前的消息列表：剥掉内部标记，并按需补齐 thinking 模式的思考字段。

        内部标记（`_compact_instruction` / `_compact_summary` 这些 `_` 开头的键）只在
        内存里用，第三方 OpenAI 兼容端点收到陌生字段可能直接 400，必须剥掉。

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
        for message in self.state.messages:
            clean = _strip_internal_fields(message)
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
                if isinstance(prompt_tokens, int) and prompt_tokens > 0:
                    self.state.last_prompt_tokens = prompt_tokens
                    self.state.anchored_message_count = len(self.state.messages)
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
    def _estimate_context_tokens(self) -> int:
        """估算当前历史占用的 token 数（锚点法，见 _estimate_usage_tokens）。"""
        return _estimate_usage_tokens(
            self.state.messages,
            self.state.last_prompt_tokens,
            self.state.anchored_message_count,
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
            head_tokens = sum(_estimate_message_tokens(m) for m in messages[:cut])
            if estimated - head_tokens >= limit:
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
        archive_name = self._archive_compacted(head, topics)
        summary_msg = self._build_summary_message(summary, archive_name)
        # system 原样保留（前缀稳定的关键）；历史里没有 system 的异常情况才补一条，
        # 保证压缩后仍是「system 打头」的合法结构。
        if system is None:
            system = {"role": "system", "content": _build_system_prompt(self.state)}
        self.state.messages = [system, summary_msg, *tail, *list(ctx.get("pulled") or [])]
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


def _cache_fields_section() -> str:
    """缓存字段说明块（拼进 system prompt）。

    字段清单与含义都从 CACHE_ENTRY_FIELDS / CACHE_ENTRY_FIELD_DESCRIPTIONS 生成，免得
    加了字段却没在提示里说明。只讲"看缓存时要懂什么"：每个字段是什么、哪个才是最终
    译文、哪些改得了——具体的读/改用法在各工具的 description 里。
    """
    lines = [
        "\n\n# 缓存（transl_cache）字段说明",
        "缓存文件 transl_cache/*.json 里每条就是「原文一句 → 译文一句」，字段含义：",
    ]
    for name in CACHE_ENTRY_FIELDS:
        description = CACHE_ENTRY_FIELD_DESCRIPTIONS.get(name)
        if not description:
            continue
        lines.append(f"- {name}：{description}")
    lines.append(
        "看译文时以 proofread_dst ＞ pre_dst 的顺序取（前者为空才用后者）；"
        "默认每条只回一列原文（post_src：真正送去翻译的那版）与一列译文（pre_dst），"
        "post_dst_preview 只在译后处理真的改了内容时才带上——只差补回来的首尾「」不算"
        "（那几乎是所有对话条目），要看它一律传 fields。"
    )
    lines.append(
        f"读缓存默认只回精简列（{' / '.join(CACHE_ENTRY_FIELDS_DEFAULT)}，以及有值的附加列），"
        "要看别的列传 fields（fields=[\"*\"] 全要）。只想看命中的条目就传 grep：字符串 = 在"
        "所选字段内容里搜文本（大小写不敏感）；数组 = 把这些元素当字段名、只留有内容的条目"
        "（如 grep=[\"problem\",\"proofread_comment\"] 取「有问题、且有校对批注」的条目）。"
        "改译文用 patch_transl_cache，只能改 "
        f"{_patchable_fields_text()}；"
        "problem 与 post_* 是后端算出来的派生字段，改不动——改完译文跑 rebuilda（或重翻）"
        "它们才会跟着更新。"
        "要在回复里把某条缓存展示给用户，单独一行写 $transl_cache(\"<缓存文件名>\", <行号>)"
        "（行号 = 条目 index，区间 12-15 / 列表 12,20 均可），界面会渲染成卡片。"
    )
    lines.append(
        "另外注意：缓存 ≠ 交付物。最终的 gt_output 文件是缓存经译后字典替换、控制符还原后的形态，"
        "验收交付物要用 read_output，不要拿缓存当输出。"
    )
    return "\n".join(lines)


def _build_system_prompt(state: "AgentState") -> str:
    """构造 system prompt：基础约束 + 当前项目环境。

    始终作为会话顶部唯一一条 system 消息。环境信息（项目目录/配置文件/目标）
    集中注入到 system prompt，对应的 user 消息只放用户的原始输入，避免重复。

    **压缩摘要不在这里**：它作为 system 之后的一条独立消息（见
    AgentRunner._build_summary_message）。摘要是会随压缩变化的内容，拼进 system
    会让整个前缀（含 tools）从第一条起失效；单独成条，system + tools 这段前缀
    在压缩后依然能命中提示缓存。
    """
    goal = state.goal or "按标准流程完成本项目的翻译"
    parts: list[str] = [AGENT_SYSTEM_PROMPT + AGENT_TURN_PROMPT, _cache_fields_section()]
    parts.append(
        "\n\n# 当前项目环境\n"
        f"- 项目目录：{state.project_dir}\n"
        f"- 配置文件：{state.config_file_name or DEFAULT_CONFIG_FILE}\n"
        f"- 本次目标：{goal}"
    )
    # 档位一律不写进 system prompt：permission_mode 是动态的，写进去会让前缀随档位切换
    # 失效，也不符合"system 建立后字节冻结"的约定。「全自动-零打断」的"不打断"由后端
    # 直接代答 ask_user 实现（见 _tool_ask_user），跟提示词无关。
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


def _llm_timeout() -> Any:
    """单次 LLM 请求的超时（连接/写入/池收紧，读用静默上限）。

    用 SDK 自己的 Timeout 类构造：openai>=2 起它不再是 `httpx.Timeout` 的别名
    （内部换成 httpx2），直接传 httpx.Timeout 虽然运行时能用、但跨版本不保证。
    老版本 SDK 没有这个类时退化成秒数（四档同值，仍有兜底作用）。
    """
    try:
        from openai import Timeout
    except ImportError:  # pragma: no cover - 老版本 SDK
        return LLM_SILENCE_TIMEOUT
    return Timeout(connect=10.0, read=LLM_SILENCE_TIMEOUT, write=30.0, pool=10.0)


def _profile_context_window(profile: dict[str, Any] | None) -> int:
    """从后端配置里取上下文窗口（与 AgentRunner._resolve_llm 同一口径）。

    窗口配在 OpenAI-Compatible.tokens[0].contextWindow，缺省/非法时用默认值。
    AgentRuntime.start 要在建会话时就知道窗口（写进状态供界面显示），所以抽出来
    共用，避免两处解析口径漂移。
    """
    openai_section = (profile or {}).get("OpenAI-Compatible") or {}
    if isinstance(openai_section, dict):
        tokens = openai_section.get("tokens") or []
        if isinstance(tokens, list) and tokens and isinstance(tokens[0], dict):
            return _parse_context_window(tokens[0].get("contextWindow"))
    return DEFAULT_CONTEXT_WINDOW


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


# 工具参数里"含密钥"的键：emit 成事件/写日志前换成占位。模型自己仍能传真实值
# （它需要真配置才能起任务），但界面上的工具卡片与会话文件不该出现明文 token。
_SECRET_TOOL_ARGS = ("backend_profile_data",)


def _sanitize_tool_args(args: dict[str, Any]) -> dict[str, Any]:
    """工具参数 → 可展示/落盘的副本（含密钥的字段换成占位）。"""
    if not any(name in args for name in _SECRET_TOOL_ARGS):
        return args
    return {
        **args,
        **{name: "<含 token 的后端配置，已省略>" for name in _SECRET_TOOL_ARGS if name in args},
    }


def _reasoning_echo(acc: dict[str, Any] | None) -> dict[str, str]:
    """这一轮的思考内容 → 要随 assistant 消息回传给 provider 的键值对。

    用流里出现过的原字段名（reasoning_content / reasoning）回传；provider 没给过思考
    就返回空 dict——不往消息里塞它不认识的字段。
    """
    if not acc:
        return {}
    text = "".join(acc.get("reasoning") or [])
    if not text:
        return {}
    return {str(acc.get("reasoning_field") or REASONING_FIELD_NAMES[0]): text}


def _assistant_parts(acc: dict[str, Any] | None) -> list[dict[str, Any]]:
    """把一条助手响应的累加器拍成有序 parts（助手消息的 content 模型）。

    段落顺序：思考 → 正文 → 工具调用。同类文本会归并成一段，不按「想/说」交替
    逐段切分——provider 交替推思考时碎片极多，逐段成卡片会把界面刷爆；界面本身
    也是按类归并渲染的（见 AgentPage 的 findAppendTarget），两边口径必须一致，
    否则「进行中的消息」与 delta 拼出来的卡片对不上，收尾时会对不上号。

    返回的是**可持久化的转录数据**：思考与正文只有进了这里，刷新/切会话后
    才重建得出来（delta 事件是瞬态的、不落盘也不进快照）。
    """
    if not acc:
        return []
    parts: list[dict[str, Any]] = []
    reasoning = "".join(acc.get("reasoning") or [])
    if reasoning:
        parts.append({"type": "reasoning", "text": reasoning})
    content = "".join(acc.get("content") or [])
    if content:
        parts.append({"type": "text", "text": content})
    for _, slot in sorted((acc.get("tools") or {}).items()):
        parts.append({
            "type": "tool_call",
            "id": slot.get("id", ""),
            "name": slot.get("name", ""),
            "arguments": "".join(slot.get("arguments_parts") or []),
        })
    return parts


def _estimate_usage_tokens(messages: list[dict[str, Any]], anchor: int = 0, anchored: int = 0) -> int:
    """估算一段历史占用的 token 数（供压缩判断与界面用量指示共用）。

    用法锚定法：有上一次响应的 prompt_tokens 作锚点时，只对锚点之后
    新增的消息按字符数估算；没有锚点就整体估算。不引入 tokenizer 依赖。
    """
    if anchor > 0 and 0 <= anchored <= len(messages):
        return anchor + sum(_estimate_message_tokens(m) for m in messages[anchored:])
    return sum(_estimate_message_tokens(m) for m in messages)


def _is_tool_call_anchor(message: dict[str, Any]) -> bool:
    """该消息是否为 tool 响应（必须紧跟其 assistant.tool_calls）。"""
    return message.get("role") == "tool"


def _message_has_tool_calls(message: dict[str, Any]) -> bool:
    return bool(message.get("tool_calls"))


def _keep_recent_tokens(limit: int, ratio: float = COMPACT_KEEP_RECENT_RATIO) -> int:
    """保留段的 token 预算：触发线的一个零头，夹在上下限之间。

    对齐 PI-Desktop 的 keepRecentTokens（由 hardLimit 派生、约两成、夹在 8K~64K）：窗口配得
    很小（触发线不为正）时按下限走，至少留一点现场给模型。
    """
    if limit <= 0:
        return COMPACT_KEEP_RECENT_MIN_TOKENS
    return max(
        COMPACT_KEEP_RECENT_MIN_TOKENS,
        min(int(limit * ratio), COMPACT_KEEP_RECENT_MAX_TOKENS),
    )


def _find_compaction_cut(messages: list[dict[str, Any]], keep_recent_tokens: int) -> int:
    """找出压缩切点：从尾部往前凑够保留预算，返回前缀长度。

    保留段按 **token 预算**挑，不是"最近 N 条"：一条几万字符的工具结果就能吃掉整个预算，
    按条数挑会把它连同十几条无关消息一起扣在上下文里，压缩等于没压。最后一条消息永远保留
    ——否则没有可压的头部，压缩根本启动不了；单条就超预算的大结果会独占保留段，这是"不截断
    消息内容"的代价（要更狠就得截断它，PI-Desktop 走的是截断那条路）。

    切点必须落在"安全位置"，否则会切断 assistant.tool_calls 与后续 tool 响应
    的配对，导致请求非法。具体规则：
    - 切点前一条不能是带 tool_calls 的 assistant（否则其 tool 响应被留下）；
    - 切点本身不能是 tool 响应（否则它的 assistant 被裁掉）。
    找不到安全位置就往前退，退到 0 表示放弃本次压缩。
    """
    total = len(messages)
    if total <= 1:
        return 0
    budget = max(1, int(keep_recent_tokens))
    cut = total
    used = 0
    while cut > 0:
        cost = _estimate_message_tokens(messages[cut - 1])
        if cut < total and used + cost > budget:
            break  # 再往前就装不下了（最后一条不设限：总得留一条）
        used += cost
        cut -= 1
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
    if cut <= 0 or cut >= total:
        return 0
    # 头部只装得下 system（其余都进了保留段）：没有可摘的内容，别为它白跑一次摘要请求
    if not any(m.get("role") != "system" for m in messages[:cut]):
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


def _is_internal_message(message: Any) -> bool:
    """是否带内部标记（`_` 开头的键）：压缩指令 / 摘要消息。

    这类消息只在内存里用（回滚、缓存断点跳过、是否已完成压缩的判断），
    发请求前必须剥掉标记而不是整条丢弃。
    """
    return isinstance(message, dict) and any(
        isinstance(key, str) and key.startswith(_INTERNAL_MSG_PREFIX) for key in message
    )


def _content_text(content: Any) -> str:
    """取消息正文文本（content 可能是字符串，也可能是多段 block 数组）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block["text"]
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        return "\n".join(parts)
    return "" if content is None else str(content)


def _render_archive_messages(messages: list[dict[str, Any]]) -> list[str]:
    """把一段消息渲染成归档正文（Markdown）。

    工具结果要截断：归档是给模型"想不起来时回查"用的，一条几万字符的缓存读取
    原样存进去，回查一次又把上下文撑爆，等于没压。
    """
    lines: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "?")
        if role == "user":
            lines += ["## 用户", "", _content_text(message.get("content")), ""]
        elif role == "assistant":
            lines += ["## 助手", ""]
            calls = message.get("tool_calls") or []
            if calls:
                names = []
                for tc in calls:
                    fn = tc.get("function") or {} if isinstance(tc, dict) else {}
                    names.append(str(fn.get("name") or "?"))
                lines += [f"_工具调用：{', '.join(names)}_", ""]
            text = _content_text(message.get("content"))
            if text:
                lines += [text, ""]
        else:
            name = str(message.get("name") or "tool")
            text = _truncate_text(_content_text(message.get("content")), COMPACT_ARCHIVE_TOOL_RESULT_CHARS)
            lines += [f"### 工具结果：{name}", "", "```", text, "```", ""]
    return lines


def _strip_internal_fields(message: dict[str, Any]) -> dict[str, Any]:
    """去掉 `_` 开头的内部键（压缩指令 / 摘要标记），发给 provider 前必须剥掉。

    没有内部键时原样返回，省一次整条消息的拷贝。
    """
    if not _is_internal_message(message):
        return message
    return {
        key: value
        for key, value in message.items()
        if not (isinstance(key, str) and key.startswith(_INTERNAL_MSG_PREFIX))
    }


def _with_cache_control(message: dict[str, Any]) -> dict[str, Any]:
    """给一条消息的正文末尾挂 cache_control（Anthropic 的断点语法）。

    content 是纯字符串时包成单块数组——这是走 OpenAI 兼容接口表达 Anthropic 断点
    的唯一方式。只有 tool_calls、没有正文的 assistant 消息，断点挂在 tool_calls 上。
    """
    content = message.get("content")
    if isinstance(content, str):
        if not content:
            return message
        return {
            **message,
            "content": [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}],
        }
    if isinstance(content, list) and content:
        blocks = list(content)
        last = blocks[-1]
        if isinstance(last, dict):
            blocks[-1] = {**last, "cache_control": {"type": "ephemeral"}}
            return {**message, "content": blocks}
    if message.get("tool_calls"):
        return {**message, "cache_control": {"type": "ephemeral"}}
    return message


def _apply_prompt_cache(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """给尾部最近 2 条真实消息 + 工具表末尾打 cache_control 断点（滚动双 marker）。

    为什么是 2 个：本轮的第 2 个断点就是下一轮的"读"断点，历史单调增长也能命中；
    工具调用失败、回滚掉最后一条消息时，倒数第二个断点还在，单步回滚依然命中。
    只打 1 个的经典失败是——本轮标记最后一条，下一轮它变成倒数第二条、带 marker 的
    位置整体前移，服务端看到的前缀不同，整段 miss。
    """
    out = list(messages)
    marked = 0
    for index in range(len(out) - 1, -1, -1):
        if marked >= 2:
            break
        message = out[index]
        if _is_internal_message(message):
            continue  # 瞬时注入的消息下一轮不会以同样形式出现，标记它等于白写
        out[index] = _with_cache_control(message)
        marked += 1
    cached_tools = list(tools)
    if cached_tools:
        cached_tools[-1] = {**cached_tools[-1], "cache_control": {"type": "ephemeral"}}
    return out, cached_tools


def _resolve_prompt_caching(raw: Any, model: str, base_url: str) -> bool:
    """是否注入 cache_control 断点。配置取值：auto（默认）/ on / off。

    Anthropic 系（含经网关转发的 Claude）需要显式断点；OpenAI 兼容的多数实现
    （DeepSeek 等）由服务端做自动前缀缓存，注入既无收益、还可能因陌生字段被拒。
    auto 因此只在 base_url / 模型名看得出是 Anthropic 系时才开。
    """
    value = str(raw or "").strip().lower()
    if value in ("on", "true", "1", "yes"):
        return True
    if value in ("off", "false", "0", "no"):
        return False
    lowered = f"{base_url or ''} {model or ''}".lower()
    return any(hint in lowered for hint in ("anthropic", "claude", "openrouter"))


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

# 带 reason 入参的工具（写类：改配置/规范/字典/缓存，加上启动翻译）共用的可选参数：
# 让模型自己交代"为什么这么做"。
# **怎么填、填了显示在哪里，只在 system prompt 的约束里写一份**（见 AGENT_SYSTEM_PROMPT），
# 这里只说明"这是什么"，这些工具引用同一个 dict，既不重复解释也不各写一遍。
# 哪些工具带这个参数见 _TOOLS_WITH_REASON（_attach_reason 按它把 reason 挂回结果）。
_REASON_PROPERTY: dict[str, Any] = {
    "type": "string",
    "description": "可选。这次操作的原因（怎么填、显示在哪见系统提示词里那条约束）。",
}

AGENT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_input_files",
            "description": "列出待翻译的输入文件（原文）与每个文件解析出的条数，供估工作量与挑选代表性文件（不必再逐个 read_input_file 数句子）。条数是原文解析出的条数（文本插件如「跳过无日文句」还没跑，可能偏大）；**只用于估工作量，不代表进度**（不管这个文件有没有缓存）——进度看 get_project_overview 的 files_translated/files_total。文件很多时默认只返回 100 个（order=even：**均匀采样**，含首尾、等距摊满整个清单，不是前 100 个；sentences_total 仍是整份清单的合计），要缩小范围用 grep（文件名子串），换挑选方式用 order。返回 Markdown 表格 + 文字说明（格式见系统提示）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "grep": {"type": "string", "description": "可选。按文件名过滤（子串、大小写不敏感），如 \"sc_2\"、\"pr00\"。"},
                    "limit": {"type": "integer", "description": "可选。最多返回多少个文件（默认 100，上限 500）；超出时按 order 挑选。"},
                    "order": {
                        "type": "string",
                        "enum": ["even", "name", "random", "size_desc", "size_asc"],
                        "description": "可选。清单的排列与采样方式（默认 even）：even=按文件名顺序均匀采样（含首尾、等距摊满整个清单）；name=按文件名顺序取前 limit 个；random=随机采样 limit 个（每次调用可能不同）；size_desc=按文件大小从大到小取前 limit 个；size_asc=按文件大小从小到大取前 limit 个。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_input_file",
            "description": "读取待翻译原文内容（文件插件解析后的条目：说话人+原文）。index 统一从 1 开始；留空 index 返回前 30 条；指定 index 支持区间，如 \"1-100\"。试译前用它了解原文文风、角色、专有名词。返回 Markdown 表格 + 文字说明（格式见系统提示）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "输入文件名，来自 list_input_files。"},
                    "index": {
                        "type": "string",
                    "description": "可选。要读取的条目 index（从 1 开始），支持逗号和区间，如 \"1-100\"。留空返回前 30 条。",
                    },
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_input",
            "description": "在**待翻译原文**里搜关键词或说话人（search_transl_cache 的原文侧对应工具：那边搜缓存=原文+译文+问题，这边只搜还没翻译的原文全文）。query 为关键词，field 取 all/src（原文正文）/name（说话人）；传 context=N 让每条命中再带上 N 句上文（默认只给上文，要前后都给传 only_preceding=false；上下文行的 index 带 *）。field=all 时顶层 matched_in 汇总命中在原文还是说话人。典型用途：定译法/收字典前先查某个称呼或专有名词在全篇出现过几次、都出现在哪些上下文（出现次数与说话人是「该不该收、收哪个写法」的依据），以及比 read_input_file 逐段读更省 token 地定位语境；命中的 filename+index 可直接交给 read_input_file 精读。传 filename 只搜某个输入文件（来自 list_input_files），留空搜全部输入文件——**每次搜索都要把涉及的输入文件过一遍文件插件（比搜缓存慢），要缩小范围就传 filename**。注意译文侧的问题（漏译/残留日文/译名是否统一）不在原文里，那些用 search_transl_cache。命中多时分页看：整页最多 200 行，limit 是本页最多几条命中（默认 100、最大 200；带 context 时命中上限按行数换算，如 context=3 → 最多 28 条），offset 是跳过前几条命中；结果里的 returned 是本页命中数、has_more 表示还有下一批，还有就把 offset 加上 returned 再查一次。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "field": {"type": "string", "enum": ["all", "src", "name"]},
                    "filename": {"type": "string", "description": "可选。只在这个输入文件里搜（来自 list_input_files）。留空搜全部输入文件。"},
                    "context": {
                        "type": "integer",
                        "description": "可选，0-20（默认 0）。每条命中再带上文（见 only_preceding），用于判断语意与称呼用法。带上下文时整页最多 200 行，命中上限按行数换算、会明显收紧（如 context=3 → 最多 28 条命中），命中很多时可配合 limit/offset 翻页或 filename 缩小范围。上下文行的 index 带 *（如 12*），那不是命中行。",
                    },
                    "only_preceding": {
                        "type": "boolean",
                        "description": "可选，默认 true：带 context 时只返回上文（判断这句为什么这么翻通常看上文就够，还省 token）；要前后两边都给传 false。",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "可选。本页最多返回几条命中，默认 100，最大 200（带 context 时还会按行数预算再收紧，整页最多 200 行）。total 始终是全部命中数。",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "可选。分页偏移：跳过前 N 条命中（默认 0，前后文行不算数）。配合 has_more 翻页。",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_guideline",
            "description": "读取翻译规范（决定文风与措辞）。scope=global（默认）读全局规范库：不带 name 列出可选文件名，传 name（如 \"日译中_增强v2.md\"）返回全文。scope=project 读**项目规范**——项目目录里的 translation_guideline.md，是这个项目专属的规则，翻译时拼在全局规范之后、冲突时以它为准。试译定稿前必读；要改文风/术语/称呼前，先看项目规范里已经写了什么。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "可选。scope=global 时的规范文件名，来自不带参数调用返回的列表。"},
                    "scope": {
                        "type": "string",
                        "enum": ["global", "project"],
                        "description": "可选。global（默认）读全局规范库；project 读本项目的项目规范。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_project_guideline",
            "description": "写**项目规范**（项目目录里的 translation_guideline.md）。三种模式：overwrite=整份覆写；append=末尾增写；replace=把 old_text 换成 new_text（old_text 要原样来自规范全文、且只出现一次，否则会报错让你带上更多前后文）。规范是写给翻译模型的，要具体可执行（术语对照、称呼、语气、标点习惯、禁忌），别写「要地道」这类空话。返回里带这一次改动的行级 diff（新增/删除的行、增删计数），不用再读一遍文件确认。",
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["overwrite", "append", "replace"],
                        "description": "写入方式：overwrite 覆写整份 / append 末尾增写 / replace 替换某段。",
                    },
                    "content": {"type": "string", "description": "mode=overwrite / append 时的规范文本（markdown）。"},
                    "old_text": {"type": "string", "description": "mode=replace 时要被替换的原文，连同前后文一起给，确保在规范里唯一。"},
                    "new_text": {"type": "string", "description": "mode=replace 时替换成的内容；传空串表示删掉这一段。"},
                    "reason": _REASON_PROPERTY,
                },
                "required": ["mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_project_overview",
            "description": "了解项目：查看翻译进度、实际生效的后端与项目配置。进度含句数 total/translated/problems/failed 和文件级 files_total/files_translated/files_untranslated；total/translated 只统计已生成缓存的文件，未翻译的文件不计入分母，translated==total 不代表整个项目翻完，整体进度看 files_translated/files_total。backend 里是两份实际生效的后端（各含 name 配置名 / type 后端类型 / model 模型名，不含地址与密钥）：agent 是本会话在用的，translator 是翻译任务会用的。流程第一步调用它确认项目可用；配置与配置键说明基本不变，之后再查进度只传 include=[\"progress\"] 即可，别重复拉。输入文件清单本身用 list_input_files / list_transl_cache 单独查询。",
            "parameters": {
                "type": "object",
                "properties": {
                    "include": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["progress", "backend", "config", "config_field_descriptions"],
                        },
                        "description": (
                            "可选。只返回这几部分（名字即返回体的键），用于避免重复拉取基本不变的内容："
                            "progress=进度；backend=实际生效的两份后端；config=项目配置；"
                            "config_field_descriptions=每个配置键的作用与取值说明（约 40 条，基本不变，"
                            "看过一次就不用再取）。留空返回全部。"
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dict_files",
            "description": "列出项目配置的译前字典(preDict)、GPT字典(gpt.dict)、译后字典(postDict)文件与各文件行数（不含内容，读内容用 read_dict）。准备字典阶段使用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_dict",
            "description": "读取某个项目字典文件的完整内容（按 file_key，来自 list_dict_files 返回的 pre_dict_files / gpt_dict_files / post_dict_files）。",
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
            "description": "写入/维护某个项目字典文件。file_key 必须来自 list_dict_files；content 为 tab 分隔文本（格式：日文<Tab>中文[<Tab>解释]）。action 决定操作：overwrite（默认，整文件覆盖）、replace（按 key 替换已有词条，未匹配的 key 不新增）、append（追加到末尾，重复 key 跳过）、delete（按 key 删除词条）。补充新词条优先用 append，避免重发整份字典；delete 的 content 可整行粘贴，也可只写 key。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_key": {"type": "string"},
                    "content": {"type": "string", "description": "要写入的字典内容（tab 分隔文本）；delete 时传要删除的词条（每行一个，可整行或只写 key）"},
                    "action": {
                        "type": "string",
                        "enum": ["overwrite", "replace", "append", "delete"],
                        "description": "overwrite=全量覆盖（默认）；replace=按 key 部分替换已有词条；append=追加到末尾（重复 key 跳过）；delete=按 key 删除词条。",
                    },
                    "reason": _REASON_PROPERTY,
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
                    "reason": _REASON_PROPERTY,
                },
                "required": ["category", "filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_name_table",
            "description": "读取 name替换表（人名表），返回 src_name/dst_name/count 列表。为空说明尚未生成。配置 dictionary.useGPTDictInName 开着时（默认开），译名为空而 GPT 字典已收录的行会按字典译名补上（带 dst_name_source=gpt_dict），并额外返回 filled_from_gpt_dict 与 still_empty 两份清单——**还缺哪些名字看 still_empty**。",
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
                    },
                    "reason": _REASON_PROPERTY,
                },
                "required": ["names"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_translation",
            "description": "提交一个翻译任务。translator 取值：ForGal-json/ForGal-tsv/ForNovel（主翻译）；GenDic（生成GPT字典）；dump-name（导出人名表）；rebuilda（用字典重建缓存+结果，跳过翻译，复核时用这个才能在 list_problems 看到变化）；rebuildr（只重建结果 json，不更新缓存，一般不用）。任务用的是「翻译任务会用」的那份后端（项目选择 → 否则全局「翻译器默认」），不是本 Agent 会话自己那份；返回里的 backend 会写明实际用的模型。传 files 只翻译指定的输入文件（试译时用：只翻一两个文件验证文风）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "translator": {"type": "string"},
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选。只翻译这些输入文件（文件名来自 list_input_files），如试译只翻第一个文件。留空翻译全部。",
                    },
                    "reason": _REASON_PROPERTY,
                },
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
                "两种用法：① 只给时长——纯等这么久；② 时长 + job_id——**盯着这个任务等：它先结束就立刻返回，"
                "时长先到就照常返回**（例：「等这个任务完成，或最多等 5 分钟再看看」= job_id 给任务 id、minutes=5）。"
                "用法：先 get_runtime 确认任务在跑 → wait → wait 结束后再 get_runtime 查状态（completed / 仍在跑看 eta_seconds 决定下一轮等多久）。"
                "注意：没给 job_id 时 wait 结束只代表计时到了，不代表后台任务完成，必须查任务状态确认。"
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
                    "job_id": {
                        "type": "string",
                        "description": "可选。要等哪个任务（start_translation 返回的 job_id）。给了它就盯着这个任务：它先跑完（completed / failed / cancelled）就立刻返回，不必等满时长；时长先到而它还在跑，则照常返回并带上它当前的状态 + 一份运行时快照（等同 get_runtime，含 summary.eta_seconds，不必再单独查一次）。仍然必须给一个时长（那是兜底上限）。",
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
            "name": "get_runtime",
            "description": (
                "查询运行时状态：当前任务状态(running/completed/failed)、阶段、本轮任务计数与 ETA、本轮新出现的错误。"
                "summary.total/percent 是**本轮任务**的口径（按任务计划统计，含正在翻译、缓存尚未落盘的文件）；"
                "已落盘缓存的口径与文件级完成度看 get_project_overview 的 progress——两个 total 分母不同，"
                "数字不一致是正常的，不要为了对齐它们多查一轮。"
                "recent_errors 是**上次查询之后新出现**的错误，同类（同 kind/同原因）已合并为一条："
                "count 是本次新增次数、text 是可直接读的一行、files 是涉及的缓存文件（最多列 5 个），"
                f"单次最多 {RUNTIME_ERRORS_PER_QUERY} 类；已发过的不再重复，另有 recent_errors_pending 表示还没发完的新错误数。"
                "列表为空只代表没有新错误，不代表之前的问题已消失——整体问题情况用 list_problems 查。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_project_config",
            "description": "修改项目配置（与桌面端「项目配置」页同一通道）。键名与 get_project_overview 返回的 config/config_field_descriptions 一致（如 \"common.gpt.contextNum\"、\"common.language\"、\"common.gpt.translation_guideline\"），只允许改已存在的键。适合调整翻译参数、切换翻译规范文件、启停问题检测项等；改完对新启动的翻译任务生效。",
            "parameters": {
                "type": "object",
                "properties": {
                    "updates": {
                        "type": "array",
                        "description": "要修改的键值对列表，一次可改多个。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "key": {"type": "string", "description": "配置键的点号路径，如 \"common.gpt.contextNum\"。说明见 get_project_overview 的 config_field_descriptions。"},
                                "value": {"description": "新值，类型跟随配置原值（数字/布尔/字符串/列表）。"},
                            },
                            "required": ["key", "value"],
                        },
                    },
                    "reason": _REASON_PROPERTY,
                },
                "required": ["updates"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_problems",
            "description": "查询自动检测到的翻译问题（残留日文、字典使用、过长等）。默认返回类型统计（各类型问题数）；传 problem_type 查看该类型的具体条目，支持分页。传 context=N 让每条问题在表里并上 N 句上文（默认只给上文，要前后都给传 only_preceding=false；上下文行的 index 带 *）——判断\"这句到底哪里有问题、该怎么改\"通常直接看这张表就够了，不必再逐条 read_transl_cache。trans_by 与 read_transl_cache 同一套：逐行只给少数派（本会话改过的、手工改的），多数派记在顶层 majority_trans_by。返回 Markdown 表格 + 文字说明（格式见系统提示）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "problem_type": {
                        "type": "string",
                        "description": "可选。要查看的问题类型（来自默认返回的统计列表，如 \"残留日文\"），支持逗号分隔多个；传 \"*\" 返回所有类型的具体条目。留空只返回类型统计。",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "可选。单次返回条目数，默认 10，最大 20。",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "可选。分页偏移，默认 0。配合 has_more 翻页。",
                    },
                    "context": {
                        "type": "integer",
                        "description": "可选，0-5（默认 0）。每条问题在表里并上 N 句上文（见 only_preceding）；相邻问题的窗口会合并、重复行只给一份。需要判断语意与改法时建议 2-3。上下文行的 index 带 *（如 12*），那不是本页的问题行。",
                    },
                    "only_preceding": {
                        "type": "boolean",
                        "description": "可选，默认 true：带 context 时只并上文（看这句为什么出问题通常看上文就够，还省 token）；要前后都并传 false。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_problem_filter",
            "description": "管理问题过滤关键字（项目配置 common.problemFilterKey，与「缓存与问题」页同一套配置）。**正则匹配**：keyword 是一条正则，按 re.search 命中问题项的那一项会被 list_problems 与进度统计过滤掉（如 `缺失.*标点` 按样式、`比日文长：1\\.5倍` 精确到某条；正则里的特殊字符要转义，写坏的正则会被拒）。**原则上只过滤小类，不要过滤大类**：像 `残留日文`、`^残留日文：` 这种把整个问题大类藏起来的写法不要用——大类里通常混着真问题，整类过滤等于不再复核；确实个别条目不用再处理时用 manage_problem_white_list 按条目豁免。list 会给出每条过滤项当前各挡住了多少条问题（problems 为 0 说明它已经一条也挡不到，可考虑 remove）。keyword 可传字符串或数组，一次增删多个。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "add", "remove"],
                        "description": "list 查看当前关键字；add 添加；remove 移除。",
                    },
                    "keyword": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                        "description": "add/remove 必填。要操作的过滤项（**正则**，如 \"缺失.*标点\"、\"^残留日文：♪\"）；命中问题项的任意位置即过滤，特殊字符需转义（\\. \\( \\[ \\*）。原则上只过滤小类：整类写法（如 \"残留日文\"）禁止使用。可传单个字符串，也可传数组一次操作多个。",
                    },
                    "reason": _REASON_PROPERTY,
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_problem_white_list",
            "description": "管理问题白名单（项目配置 common.problemWhiteList）。白名单是「缓存文件 + 条目 index」的名单，命中的条目等价于勾选了 skip_check：不再检测/展示问题，也不计入问题统计。适合确认某几条译文无需再处理时按位置精确豁免（如个别专有名词、语气词导致的反复误报）。entry 传 \"文件名:index\"（如 \"01.json:12\"，区间写 \"01.json:12-15\"），可传字符串或数组一次增删多个。与 manage_problem_filter 的区别：filter 按问题文本子串整类过滤，白名单按具体条目豁免。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "add", "remove"],
                        "description": "list 查看当前白名单；add 添加；remove 移除。",
                    },
                    "entry": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                        "description": "add/remove 必填。要操作的条目，格式 \"<缓存文件名>:<index>\"（如 \"01.json:12\"；闭区间写 \"01.json:12-15\"）。可传单个字符串，也可传数组一次操作多条。",
                    },
                    "reason": _REASON_PROPERTY,
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_transl_cache",
            "description": "列出缓存文件（译文）与各文件的条目数。文件很多时默认只返回 100 个（order=even：**均匀采样**，含首尾、等距摊满整个清单，不是前 100 个），要缩小范围用 grep（文件名子串，如 grep=\"sc_2\"），换挑选方式用 order（文件名顺序 / 随机采样 / 按大小从大到小或从小到大），要看更多把 limit 调大（上限 500）。返回 Markdown 表格 + 文字说明（格式见系统提示）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "grep": {"type": "string", "description": "可选。按文件名过滤（子串、大小写不敏感），如 \"sc_2\"、\"pr00\"。"},
                    "limit": {"type": "integer", "description": "可选。最多返回多少个文件（默认 100，上限 500）；超出时按 order 挑选。"},
                    "order": {
                        "type": "string",
                        "enum": ["even", "name", "random", "size_desc", "size_asc"],
                        "description": "可选。清单的排列与采样方式（默认 even）：even=按文件名顺序均匀采样（含首尾、等距摊满整个清单）；name=按文件名顺序取前 limit 个；random=随机采样 limit 个（每次调用可能不同）；size_desc=按文件大小从大到小取前 limit 个；size_asc=按文件大小从小到大取前 limit 个。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_transl_cache",
            "description": "读取某个缓存文件的条目（译文）。filename 来自 list_transl_cache 的缓存文件列表。留空 index 返回前 30 条；指定 index 只返回指定的条目。修问题/润色判断语意连贯时传 context 让目标条目带上几句上文（默认只给上文，要前后都给传 only_preceding=false；上下文行的 index 带 *）。默认只返回必要字段（index/说话人/原文/译文/问题，以及确实非空或与原文不同的附加字段），要看别的字段再传 fields。要只看命中的条目传 grep：字符串 = 在 fields 选中的字段内容里搜文本（大小写不敏感），数组 = 把这些元素当字段名、只留有内容的条目（如 [\"problem\",\"proofread_comment\"]）。返回 Markdown 表格 + 文字说明（格式见系统提示）。要把某条缓存展示给用户时，在回复里单独一行写 $transl_cache(\"<缓存文件名>\", <行号>)（行号 = 条目 index，区间 12-15 / 列表 12,20 均可），界面会把它渲染成那几行缓存的卡片。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                    "index": {
                        "type": "string",
                        "description": "可选。要读取的条目 index 列表，支持逗号和区间，如 \"33-40,50-60\"、\"5,9,12\"、\"100-105\"。留空返回前 30 条。",
                    },
                    "context": {
                        "type": "integer",
                        "description": "可选。上下文句数（0-20）：目标条目向上多返回 N 句（见 only_preceding），如 index=\"205-206\" context=3 返回 202~206（only_preceding=false 时 202~209）。修问题判断语意时建议 2-4。上下文行的 index 带 *（如 205*），那不是点名的条目。",
                    },
                    "only_preceding": {
                        "type": "boolean",
                        "description": "可选，默认 true：带 context 时只返回上文（修问题看上文通常就够，还省 token）；要前后都给传 false。",
                    },
                    "grep": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                        "description": (
                            "可选。只返回命中的条目。①字符串 = 在 fields 选中的字段（不传 fields 则默认精简集）"
                            "内容里做大小写不敏感的子串搜索，命中任一字段即保留，如 grep=\"残留日文\"；"
                            "②字符串数组 = 每个元素当字段名，只保留这些字段都不为空的条目，"
                            "如 grep=[\"problem\",\"proofread_comment\"] 取「有问题、且有校对批注」的条目。"
                            "与 index 同用时先按 grep 过滤，再按 index 取。"
                        ),
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "可选。每条要返回哪些字段：index、name（说话人）、pre_src（原句）、pre_dst（译文）、"
                            "post_src、post_dst_preview（译后字典替换后的预览）、proofread_dst、proofread_by、"
                            "trans_by、problem。"
                            "不传 = 默认精简集（index/name/post_src/pre_dst/problem；post_dst_preview 仅在译后处理真的改了内容时给，"
                            "空值省略；要看 pre_src/trans_by 等列得显式传 fields）；传 [\"pre_dst\",\"problem\"] 这类只要某几列。"
                            "trans_by 逐条只给少数派（多数派 = 这批里出现最多的那个模型，通常就是引擎翻的；它记在返回的 majority_trans_by 里）。"
                        ),
                    },
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_output",
            "description": "读取最终输出文件（gt_output，交付物）。输出是缓存经译后字典替换、控制符处理后的最终形态，与缓存可能不完全一致——验收交付物、确认 postDict 替换效果用这个，而不是 read_transl_cache。文件名通常与输入文件同名。留空 index 返回前 30 条。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "输出文件名，通常与输入文件同名（如 sc_0_pr00.txt.json）"},
                    "index": {
                        "type": "string",
                    "description": "可选。要读取的条目 index（从 1 开始），支持逗号和区间（如 \"1-100\"）。留空返回前 30 条。",
                    },
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_transl_cache",
            "description": "删除缓存（条目或整个文件）。物理删除后，重启翻译时被删除的句子会因缓存未命中而重新翻译——这是触发部分重翻的手段。注意：删除不可撤销；rebuilda/rebuildr 依赖缓存，删除后不要再跑重建。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "缓存文件名（来自 get_project_overview 的 cache_files）。传 \"*\" 删除全部缓存文件。",
                    },
                    "indexes": {
                        "type": "string",
                        "description": "可选。要删除的条目 index 列表，支持逗号和区间（如 \"33-40,50-60\"，index 来自 read_transl_cache/list_problems）。留空则删除整个文件。",
                    },
                    "reason": _REASON_PROPERTY,
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_transl_cache",
            "description": "在缓存中搜索译文/原文/问题。query 为关键词，field 取 all/src/dst/problem。只看命中行往往不够判断（如查「ドルード」要决定译成「多鲁德」还是「杜罗德」），传 context=N 让每条命中再带上 N 句上文，用法同 read_transl_cache 的 context（默认也只给上文，要前后都给传 only_preceding=false；上下文行的 index 带 *）。field=all 时顶层 matched_in 汇总命中在哪一侧（src/dst/problem），不再逐行重复标注；trans_by 逐行只给少数派——整批命中里出现最多的那个模型（多数派，通常就是翻译引擎翻的）过滤掉并记在顶层 majority_trans_by，其余少数派逐行保留。传 filename 只搜某个缓存文件（来自 list_transl_cache），修单文件问题时用，如 search_transl_cache(query=\"アクメ\", field=\"src\", filename=\"sc_2_st01.txt.json\")。命中多时分页看：整页最多 200 行，limit 是本页最多几条命中（默认 100、最大 200；带 context 时命中上限按行数换算，如 context=3 → 最多 28 条），offset 是跳过前几条命中；结果里的 returned 是本页命中数、has_more 表示还有下一批，还有就把 offset 加上 returned 再查一次。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "field": {"type": "string", "enum": ["all", "src", "dst", "problem"]},
                    "filename": {"type": "string", "description": "可选。只在这个缓存文件里搜（来自 list_transl_cache）。留空搜全项目。"},
                    "context": {
                        "type": "integer",
                        "description": "可选，0-20（默认 0）。每条命中再带上文（见 only_preceding），用于判断译名/语气/语意连贯。带上下文时整页最多 200 行，命中上限按行数换算、会明显收紧（如 context=3 → 最多 28 条命中），命中很多时可配合 limit/offset 翻页或 filename 缩小范围。上下文行的 index 带 *（如 12*），那不是命中行。",
                    },
                    "only_preceding": {
                        "type": "boolean",
                        "description": "可选，默认 true：带 context 时只返回上文（判断这处译名怎么定通常看上文就够，还省 token）；要前后两边都给传 false。",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "可选。本页最多返回几条命中，默认 100，最大 200（带 context 时还会按行数预算再收紧，整页最多 200 行）。total 始终是全部命中数。",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "可选。分页偏移：跳过前 N 条命中（默认 0，前后文行不算数）。配合 has_more 翻页。",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "patch_transl_cache",
            "description": "批量修改某个缓存文件中若干条目的译文（pre_dst / proofread_dst 两列）。只更新 patches 里指定的条目与字段，其它条目原样保留。返回 updated（改动条目数）、changes（逐字段 before→after 的变更）与 problems（被改条目重建后仍存在的问题，没有则不返回）；改了什么一目了然、有没有引入新问题当场可验，不必再 read_transl_cache。适合发现问题后改译文、再配合 rebuilda 重建的复核循环。trans_by 由工具自动标记，不用手动指定。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "缓存文件名，来自 list_transl_cache 的缓存文件列表"},
                    "patches": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "index": {"type": "integer", "description": "要修改的条目 index"},
                                "pre_dst": {"type": "string", "description": "可选。新译文（机翻结果）"},
                                "proofread_dst": {"type": "string", "description": "可选。新校对译文（校对/润色结果，优先于 pre_dst）"},
                                "proofread_comment": {"type": "string", "description": "可选。校对批注（校对子代理写下的意见：校对建议或润色建议，见 run_subagents）。按它改完译文后传空串清掉，表示这条已处理"},
                            },
                            "required": ["index"],
                        },
                    },
                    "reason": _REASON_PROPERTY,
                },
                "required": ["filename", "patches"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_subagents",
            "description": (
                "派一批子代理并行干活，等它们全部跑完，把每份报告收回来。子代理有自己的上下文与"
                "受限工具集，干活过程不进你的上下文，返回给你的只有每份报告。两种角色："
                "**proofread（校对）**——只能读缓存/人名表/规范/问题清单，加写缓存条目的「校对批注」"
                "（proofread_comment，校对建议与润色建议都写这里），改不了译文；适合翻译完成后逐文件校对，"
                "返回它写了哪些 index 的疑问，你用 read_transl_cache 读那些 proofread_comment、改完译文再清空它。"
                "**explore（原文探索）**——只读原文与 GPT 字典、不写任何文件；用来补 GenDic 覆盖不到的"
                "昵称/专有名词/称呼，以及给翻译规范提建议，结论在你的报告里由你汇总落地"
                "（save_dict / write_project_guideline）。explore 要通读原文、**很费 token**，"
                "属于可选项：派之前先用 ask_user 征得用户同意。"
                f"一次最多 {SUBAGENT_MAX_TASKS} 个，要它们重点看什么就写进 brief。"
                '要并行多个又不想写多条任务：一条任务写 file:"*" + count:N 就展开成 N 个'
                "（brief 只写一遍）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "description": f"要派的任务，1-{SUBAGENT_MAX_TASKS} 个（并行跑）。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "agent": {
                                    "type": "string",
                                    "enum": list(SUBAGENT_AGENTS),
                                    "description": "子代理角色：proofread（校对缓存译文）/ explore（通读原文，提字典候选与规范建议）",
                                },
                                "file": {
                                    "type": "string",
                                    "description": '要负责的文件。proofread 必填：缓存文件名（来自 list_transl_cache）；explore 可选：原文文件名，留空则由它自己按 list_input_files 挑。填 "*" 表示**自动均分**：本批里同角色的每个 "*" 任务平分该角色的全部文件（proofread=缓存文件，explore=原文文件）——例如派 16 个 "*" 任务、项目有 256 个缓存文件，就每个 16 个；除不尽时前面的多一个；本批里已具体点名的文件不会再分给 "*"。除单个文件名和 "*" 外不支持其他写法：要手动分组就一个任务写一个文件名。锁定是工具层强制的：子代理只能读写派给它的那些文件，范围外会被拒。**要并行 N 个不必写 N 条任务**：写一条 file:"*" + count:N 即可（brief 只写一遍）',
                                },
                                "count": {
                                    "type": "integer",
                                    "description": '可选。把这一条任务展开成 N 个子代理并行跑（默认 1，上限同批任务数）。只有 file 填 "*" 时可用——它们平分这批文件。要派 2 个 explore 通读原文，就写一条 {agent:"explore", file:"*", count:2}，别把长 brief 复制两遍',
                                },
                                "indexes": {
                                    "type": "string",
                                    "description": "可选。只处理这个区间（写法同 read_transl_cache 的 index，如 \"1-200\"）；留空=整个文件",
                                },
                                "brief": {
                                    "type": "string",
                                    "description": "可选。给这个子代理的额外要求：重点核对什么、注意哪些角色/术语；**校对子代理还要在这里写明这一遍写哪一类意见**（只写校对建议 / 只写润色建议 / 两者都要，先 ask_user 问用户，见流程 6.5），没写就默认只写校对建议。count > 1 时这一份 brief 由展开出来的每个子代理共用（不用重复写）",
                                },
                            },
                            "required": ["agent"],
                        },
                    },
                },
                "required": ["tasks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_history_archive",
            "description": (
                "读本次会话被上下文压缩归档下来的早期对话（压缩摘要消息里列出的 chunk 文件）。"
                "不带参数：列出所有归档及主题，供你判断该查哪一份。带 chunk：返回该归档全文"
                "（超长会截断）。带 query：在所有归档里检索关键词，返回命中行及上下文，用来"
                "找回具体细节（早前定下的术语、某个文件的处理结论等）。只在摘要信息不够时"
                "才查，不要为了「确认一下」逐个通读归档。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chunk": {
                        "type": "string",
                        "description": "归档文件名（如 chunk-0001.md）或序号（如 1）。留空 = 列出全部归档。",
                    },
                    "query": {
                        "type": "string",
                        "description": "可选。关键词，在各归档里检索并返回命中行及上下文。",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "可选。最多返回多少条命中/多少行，默认 30。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": (
                "当你不确定该不该做（要不要动这个文件、要不要重翻）、或不确定该怎么翻译"
                "（用词、称谓、语气、风格取舍）时，向用户提问并等待回答，不要自己猜。每题给出"
                "2-6 个候选选项，用户还可以自己填；一次最多 4 题。每题都要填 recommended——"
                "你推荐的那个选项：「全自动-零打断」档位下后端会直接采用它替你作答、不打扰用户，"
                "其余档位只把它标成卡片上的「推荐」。用户跳过某题会以空答案返回（不算失败），"
                "你按自己的最佳判断继续即可。能从项目配置、字典或原文里判断出来的不要问——"
                "只有真的需要人来定夺时才用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "questions": {
                        "type": "array",
                        "description": "要问的问题（1-4 个）",
                        "items": {
                            "type": "object",
                            "properties": {
                                "question": {
                                    "type": "string",
                                    "description": "问题本身。写清背景与各选项的差别，让用户不必再看别处就能决定。",
                                },
                                "options": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "候选答案（2-6 个）；用户在卡片里还可以自己填。",
                                },
                                "multiSelect": {
                                    "type": "boolean",
                                    "description": "可选。true 表示可以多选，默认单选。",
                                },
                                "recommended": {
                                    "type": "string",
                                    "description": (
                                        "你推荐的那个选项，必须与 options 里的某一项一字不差。"
                                        "建议每题都填：「全自动-零打断」档位下后端直接采用它代答，"
                                        "不填就退而取第一个选项。"
                                    ),
                                },
                            },
                            "required": ["question", "options"],
                        },
                    },
                },
                "required": ["questions"],
            },
        },
    },
]


# ---- 工具实现 ----
# 配置键说明表：get_project_overview 返回配置时附上，让 Agent 看懂每个键的
# 作用与取值。口径与 sampleProject/config.inc.yaml 的注释、桌面端「项目配置」
# 页各 section 的字段说明一致。键用点号路径，与 YAML 展平后一致。
CONFIG_FIELD_DESCRIPTIONS: dict[str, str] = {
    # ---- common ----
    "common.gpt.numPerRequestTranslate": "每次请求打包的句子数，建议不超过 16 [1-32]",
    "common.workersPerProject": "项目级并行文件数；单文件并行需配合 splitFile",
    "common.autoAdjustWorkers": "根据近期 429 比例和响应延迟自动降/升 worker 并发 [true/false]",
    "common.sortBy": "文件调度顺序：name 按文件名，size 优先大文件（并行时通常更快）",
    "common.language": "目标输出语言 [zh-cn/zh-tw/en/ja/ko/ru/fr]",
    "common.splitFile": "单文件分片模式：no 关闭；Num 每 n 句切一片；Equal 每文件均分 n 片。【重要】分割设置直接影响缓存读取命中，迁移旧项目必须保持一致",
    "common.splitFileNum": "分片参数：Num 模式表示每片句数；Equal 模式表示分片总数",
    "common.splitFileCrossNum": "分片重叠句数（上下文缓冲），可提升片段衔接质量 [常用 0 或 10]",
    "common.save_steps": "每处理 n 个批次保存一次缓存；值越大保存越少、速度可能更快",
    "common.start_time": "定时启动时间（24 小时制，如 00:30）；留空立即启动",
    "common.linebreakSymbol": "JSON 内换行符类型，供问题检测/自动修复使用，不改变翻译语义",
    "common.skipH": "是否跳过可能触发敏感词检测的句子 [true/false]",
    "common.smartRetry": "解析失败时自动缩小批次并重置上下文，减少无效重试 [true/false]",
    "common.retranslFail": "程序重启时是否自动重翻标记为 (Failed) 的句子 [true/false]",
    "common.retranslKey": "重翻关键字列表：启动时命中缓存 problem 或原文关键字的句子会被重翻（如「翻译失败」「残留日文」）",
    "common.problemFilterKey": "问题过滤关键字列表：**正则列表**，每项是一条正则，命中的问题项在问题统计与 list_problems 中被过滤掉。原则上只过滤小类（如 `缺失.*标点`、`^残留日文：♪`），不要用 `残留日文` 这类整类写法",
    "common.problemWhiteList": "问题白名单：按「缓存文件名:index」（如 a.json:12，区间写 a.json:12-15）豁免指定缓存条目的问题，等价于给该条勾选 skip_check",
    "common.gpt.contextNum": "每次请求附带的前文句数；值越大上下文越强、成本越高（常用 8）[0-32]",
    "common.gpt.translation_guideline": "使用的**全局**翻译规范文件名（位于 translation_guidelines 文件夹），决定文风与措辞；项目专属规范不是配置项，而是项目目录里的 translation_guideline.md（用 read_guideline/write_project_guideline 读改），翻译时拼在全局规范之后",
    "common.gpt.enhance_jailbreak": "是否启用「抗拒答」增强提示，降低模型拒答概率 [true/false]",
    "common.gpt.token_limit": "(Sakura/GalTransl) 单轮 token 上限；0 表示不限制，用于避免上下文溢出",
    "common.loggingLevel": "日志输出级别：debug 详细，info 常规，warning 仅警告 [debug/info/warning]",
    "common.saveLog": "是否将运行日志写入文件 [true/false]",
    "common.gpt.dynamicNumPerRequestTranslate": "动态句数调整：根据模型解析错误自动降/升单次翻译句数 [true/false]",
    # ---- problemAnalyze ----
    "problemAnalyze.problemList": "要启用的问题检测清单（词频过高/标点错漏/残留日文/丢失换行/多加换行/比日文长/比日文长严格/字典使用/引入英文/语言不通/缺控制符/独白男他/单句过长）",
    "problemAnalyze.avgSentenceLengthThreshold": "单句过长检测的平均分句长度阈值",
    "problemAnalyze.arinashiDict": "有無字典：检测多加/漏加字典符号（如【】）的词表",
    # ---- dictionary ----
    "dictionary.defaultDictFolder": "通用字典文件夹（相对程序目录，也可绝对路径）",
    "dictionary.usePreDictInName": "将译前字典用在 name 字段（人名替换）[true/false]",
    "dictionary.usePostDictInName": "将译后字典用在 name 字段 [true/false]",
    "dictionary.useGPTDictInName": "将 GPT 字典用在 name 字段 [true/false]",
    "dictionary.sortDict": "将所有字典按查找词长度重排序 [true/false]",
    "dictionary.preDict": "译前字典文件列表（每行一个；前缀 (project_dir) 代表在项目目录下）。译前字典在送入模型前直接替换原文",
    "dictionary.gpt.dict": "GPT 字典文件列表。随 Prompt 发给模型，约束人名/术语译法（Agent 应主要维护这层）",
    "dictionary.postDict": "译后字典文件列表。翻译完成后对译文做替换（符号矫正等）",
    # ---- plugin ----
    "plugin.filePlugin": "文件插件（决定输入/输出格式）：file_galtransl_json；字幕 file_subtitle_srt_lrc_vtt；小说 file_epub_epub / file_plaintext_txt；Mtool json 用 file_i18n_json",
    "plugin.textPlugins": "文本处理插件列表（按顺序执行）：如 text_common_normalfix 常规修复、text_common_skipNoJP 跳过无日文句",
    # ---- proxy ----
    "proxy.enableProxy": "是否启用代理 [true/false]，使用中转供应商时一般不用开",
}


def _annotate_config(config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """给配置附带键说明。只附配置里实际存在的键（含点号键的 section 形式，
    如 common 里直接写 gpt.numPerRequestTranslate 的展平路径）。"""
    section_hints = {
        "common": "通用程序设置",
        "problemAnalyze": "自动问题分析配置",
        "dictionary": "字典设置",
        "plugin": "文件/文本插件配置",
        "proxy": "代理设置",
    }
    descriptions: dict[str, str] = {}
    for section, note in section_hints.items():
        if section in config:
            descriptions[section] = note
    for dotted, desc in CONFIG_FIELD_DESCRIPTIONS.items():
        if "." not in dotted:
            continue
        section, _, key = dotted.partition(".")
        value = config.get(section)
        if isinstance(value, dict) and key in value:
            descriptions[dotted] = desc
    return config, descriptions


# 已下线的旧 Prompt 键（common 下的展平键名）：只有老工程配置文件里还留着，
# 翻译流程仍认它们（BaseTranslate 的兼容分支），但不再提供给前端与 Agent——
# get_project_overview 不返回、update_project_config 直接拒掉，模型就不会去碰。
# 现在的自定义入口是项目翻译规范（ProjectGuideline / write_project_guideline）。
LEGACY_PROMPT_KEYS = ("gpt.change_prompt", "gpt.prompt_content")
LEGACY_PROMPT_PATHS = frozenset(f"common.{key}" for key in LEGACY_PROMPT_KEYS)


# 翻译进行中会在 <缓存>.json 旁并行写 <缓存>.json.append.jsonl 增量日志（见 GalTransl/Cache.py）
_APPEND_CACHE_SUFFIX = ".append.jsonl"


def _input_cache_matchers(name: str) -> tuple[set[str], re.Pattern[str]]:
    """把输入文件名换算成它在缓存目录里可能出现的文件名，用于把缓存归属回输入文件。

    命名规则（见 Frontend/LLMTranslate._build_runtime_file_maps 与 doLLMTranslSingleChunk）：
    输入文件相对路径把分隔符替换成 "-}"，多分块再追加 "_<分块号>"，最后
    save_transCache_to_json 在结尾补一次 ".json"（已经以 ".json" 结尾则不再补）。例如：
      foo.json → foo.json（单块）/ foo.json_0.json（多块）
      foo.ks   → foo.ks.json（单块）/ foo.ks_0.json（多块）
    """
    base = name.replace("/", "-}").replace("\\", "-}")
    single = base if base.endswith(".json") else f"{base}.json"
    singles = {single, f"{single}{_APPEND_CACHE_SUFFIX}"}
    chunk_re = re.compile(rf"^{re.escape(base)}_\d+\.json(?:{re.escape(_APPEND_CACHE_SUFFIX)})?$")
    return singles, chunk_re


def _count_input_file_progress(input_files: list[str], progress_files: list[dict[str, Any]]) -> dict[str, int]:
    """按输入文件统计「已有译文的文件数 / 尚无译文的文件数」。

    progress_files 是 /progress 返回的 files（每个缓存文件的 filename 与 translated 句数）。
    只有累计译文句数 > 0 的文件才算「已翻译」，这样即使 rebuild 阶段生成了全空缓存，
    也不会把尚未翻译的文件误算成已翻译。
    """
    translated_by_cache = {
        str(item.get("filename", "")): int(item.get("translated", 0) or 0)
        for item in progress_files
        if isinstance(item, dict) and item.get("filename")
    }
    files_translated = 0
    for name in input_files:
        singles, chunk_re = _input_cache_matchers(name)
        translated = sum(
            count
            for cache_name, count in translated_by_cache.items()
            if cache_name in singles or chunk_re.match(cache_name)
        )
        if translated > 0:
            files_translated += 1
    total = len(input_files)
    return {
        "files_total": total,
        "files_translated": files_translated,
        "files_untranslated": max(total - files_translated, 0),
    }


def _config_for_overview(raw: Any) -> dict[str, Any]:
    """「了解项目」返回的配置快照：剔除 backendSpecific 与已下线的旧键。

    backendSpecific 那节是 API 令牌、端点等敏感信息（发给模型等于把密钥递出去），
    对"了解项目"也没有价值——实际生效的后端见返回里的 backend 字段。

    LEGACY_PROMPT_KEYS 同理不给模型看：它们只为老工程保留（翻译流程仍认），模型看不到
    就不会去改；新项目不会再生成这两个键。
    """
    if not isinstance(raw, dict):
        return {}
    out = {k: v for k, v in raw.items() if k != "backendSpecific"}
    common = out.get("common")
    if isinstance(common, dict):
        out["common"] = {k: v for k, v in common.items() if k not in LEGACY_PROMPT_KEYS}
    return out


def _backend_summary(profile: Any, name: str = "") -> dict[str, str]:
    """一份后端配置 → {name, type, model}：只给名字与模型名，不含地址与密钥。"""
    section = ""
    model = ""
    if isinstance(profile, dict):
        for key, conf in profile.items():
            if not isinstance(conf, dict):
                continue
            section = str(key)
            if key == "OpenAI-Compatible":
                tokens = conf.get("tokens")
                if isinstance(tokens, list) and tokens and isinstance(tokens[0], dict):
                    model = str(tokens[0].get("modelName") or "")
            break
    return {"name": name or section, "type": section, "model": model}


def _backend_overview(runner: AgentRunner) -> dict[str, Any]:
    """实际生效的两份后端：本会话（Agent）用的 + 翻译任务会用的。

    项目配置文件里的 backendSpecific 常是旧值（后端还会被全局后端配置覆盖），
    所以两份都以"真正会被使用"的配置为准：
    - agent：runner 手里那份（Agent 自己这一会话用的）；
    - translator：前端送来的项目选择（没有项目选择时就是全局"翻译器默认"）——
      启动翻译任务用的就是它（见 _tool_start_translation）。
    """
    state = runner.state
    return {
        "agent": _backend_summary(state.backend_profile_data, state.backend_profile_name),
        "translator": _backend_summary(
            state.translator_profile_data, state.translator_profile_name
        ),
    }


# 「了解项目」可分段返回。名字与返回体的键一一对应（include 里写什么，回来就是什么键），
# 顺序即返回顺序；不传 include 就是全部（保持老行为）。
OVERVIEW_SECTIONS: tuple[str, ...] = (
    "progress",
    "backend",
    "config",
    "config_field_descriptions",
)


def _normalize_overview_include(args: dict[str, Any]) -> list[str]:
    """校验 include：不传 = 全部；传了就去重并按标准顺序返回，未知名字直接报错。"""
    raw = args.get("include")
    if raw is None:
        return list(OVERVIEW_SECTIONS)
    if not isinstance(raw, list) or not raw:
        raise AgentToolError("include 必须是非空数组（不传表示返回全部）")
    wanted: set[str] = set()
    for item in raw:
        name = str(item or "").strip()
        if not name:
            continue
        if name not in OVERVIEW_SECTIONS:
            raise AgentToolError(
                f"include 里有未知的部分：{name}（可选：{'、'.join(OVERVIEW_SECTIONS)}）"
            )
        wanted.add(name)
    if not wanted:
        raise AgentToolError(f"include 里没有有效部分（可选：{'、'.join(OVERVIEW_SECTIONS)}）")
    return [section for section in OVERVIEW_SECTIONS if section in wanted]


def _tool_get_project_overview(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """了解项目。按 include 分块返回（不传 = 全部）。

    分块的意义：config 与 config_field_descriptions（约 40 个键的说明）基本是静态的，
    开局拿全看过一次之后，再查进度时没有理由原样重发一遍。只取需要的部分，没要配置
    就**连那条 HTTP 都不发**（省一次本机往返），返回体也不会被那份静态说明撑大。
    """
    include = _normalize_overview_include(args)
    pid = runner._project_id()
    config_name = runner.state.config_file_name or DEFAULT_CONFIG_FILE
    cfg_name = urllib.parse.quote(config_name)
    out: dict[str, Any] = {}

    if "progress" in include:
        progress = runner._http_get(f"/api/projects/{pid}/progress?config={cfg_name}")
        files = runner._http_get(f"/api/projects/{pid}/files")
        input_files = [
            str(entry.get("name", ""))
            for entry in files.get("input_files", [])
            if isinstance(entry, dict) and entry.get("is_file", True) and entry.get("name")
        ]
        file_counts = _count_input_file_progress(input_files, progress.get("files", []))
        out["progress"] = {
            "total": progress.get("total", 0),
            "translated": progress.get("translated", 0),
            "problems": progress.get("problems", 0),
            "failed": progress.get("failed", 0),
            **file_counts,
            "note": (
                "total/translated 是句数，且只统计已生成缓存的文件；未开始翻译的文件不计入分母，"
                "所以 translated==total 只说明「已有缓存的部分翻完了」，不代表整个项目翻完。"
                "整体进度请结合 files_translated/files_total 判断。"
                "这里是**已落盘缓存**的口径（扫缓存目录得到）；本轮任务自身的计数与 ETA 见 "
                "get_runtime 的 summary（按任务计划统计，含正在翻译、尚未落盘的文件，"
                "total 通常比这里大）——两个分母不同，别拿它们互相校对。"
            ),
        }

    if "backend" in include:
        out["backend"] = {
            **_backend_overview(runner),
            "note": (
                "实际生效的后端（各自含 name 配置名 / type 后端类型 / model 模型名）："
                "agent 是本 Agent 会话在用的；translator 是翻译任务会用的"
            ),
        }

    if "config" in include or "config_field_descriptions" in include:
        cfg = runner._http_get(f"/api/projects/{pid}/config?config={cfg_name}")
        config, descriptions = _annotate_config(_config_for_overview(cfg.get("config")))
        if "config" in include:
            out["config"] = config
        if "config_field_descriptions" in include:
            out["config_field_descriptions"] = descriptions

    if len(include) < len(OVERVIEW_SECTIONS):
        out["note"] = (
            f"本次只返回了{'、'.join(include)}；需要其它部分时再调用一次并带上对应的 include"
            f"（可选：{'、'.join(OVERVIEW_SECTIONS)}）。"
        )
    return out


# ---- 清单类工具的公共入参（grep + limit + order）----
# list_transl_cache / list_input_files 都是一行一个文件的清单：大项目动辄几百上千行，全量
# 倒给模型既费 token 也淹掉重点，只给前 N 行又会让它以为"项目就这些文件"——后面的文件它
# 根本不会去查。所以按 limit（默认 100）截取，**怎么截由 order 决定**（见下面的模式表）；
# 要精确定位某个文件用 grep（文件名子串，大小写不敏感）。
LIST_ITEMS_DEFAULT_LIMIT = 100
LIST_ITEMS_MAX_LIMIT = 500

# order：清单的排列与采样方式。默认 even（均匀采样）——它保证整个范围都有代表，是"先看看
# 项目里都有些什么"的默认姿势；其余几种各有明确用途，都是模型自己点名才会用：
# - even：按文件名排好后均匀采样（含首尾、等距取）；
# - name：按文件名顺序取前 limit 个（挨着看某一批，配合 grep 用）；
# - random：随机采样 limit 个（每次调用可能不同，避免永远只看同一段）；
# - size_desc / size_asc：按文件大小从大到小 / 从小到大取前 limit 个（找大文件优先处理，
#   或先扫小文件）。size 模式的"顺序"本身就是它要表达的东西，所以截断取的是最大/最小的那些。
LIST_ORDER_MODES: tuple[str, ...] = ("even", "name", "random", "size_desc", "size_asc")
LIST_ORDER_DEFAULT = "even"
LIST_ORDER_LABELS: dict[str, str] = {
    "even": "均匀采样",
    "name": "文件名顺序",
    "random": "随机采样",
    "size_desc": "按文件大小从大到小",
    "size_asc": "按文件大小从小到大",
}


def _list_limit(args: dict[str, Any]) -> int:
    """清单工具的 limit：默认 100，夹到 1-500。"""
    raw = args.get("limit", LIST_ITEMS_DEFAULT_LIMIT)
    if raw is None or raw == "":
        return LIST_ITEMS_DEFAULT_LIMIT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise AgentToolError(f"limit 必须是整数（收到 {raw!r}）")
    return max(1, min(value, LIST_ITEMS_MAX_LIMIT))


def _list_grep(args: dict[str, Any]) -> str:
    """清单工具的 grep：文件名过滤词（空串 = 不过滤）。"""
    return str(args.get("grep", "") or "").strip()


def _grep_items(items: list[Any], grep: str) -> list[Any]:
    """按 name 过滤清单（子串、大小写不敏感）。"""
    if not grep:
        return list(items)
    needle = grep.lower()
    return [item for item in items if needle in str(item.get("name", "")).lower()]


def _sample_evenly(items: list[Any], limit: int) -> list[Any]:
    """从长清单里均匀采样 limit 条（含首尾，等距取）。

    与"取前 limit 条"的区别就是这个函数存在的理由：清单按文件名排序，截断会把后半段
    整个藏起来（模型据此以为项目里没有那些文件），采样则每一段都留代表。
    """
    n = len(items)
    if n <= 0:
        return []
    if limit >= n:
        return list(items)
    if limit <= 1:
        return [items[0]]
    return [items[(i * (n - 1)) // (limit - 1)] for i in range(limit)]


def _list_order(args: dict[str, Any]) -> str:
    """清单工具的 order：排列 / 采样方式，默认 even（不当的值直接报错，别静默退回默认）。"""
    raw = str(args.get("order", "") or "").strip().lower()
    if not raw:
        return LIST_ORDER_DEFAULT
    if raw not in LIST_ORDER_MODES:
        raise AgentToolError(f"order 必须是 {'/'.join(LIST_ORDER_MODES)} 之一（收到 {raw!r}）")
    return raw


def _item_size(item: Any) -> tuple[int, str]:
    """大小排序键：（size, name）——size 拿不到当 0，第二关键字用文件名，同尺寸时输出稳定。"""
    size = item.get("size", 0) if isinstance(item, dict) else 0
    try:
        size_i = int(size)
    except (TypeError, ValueError):
        size_i = 0
    name = str(item.get("name", "")) if isinstance(item, dict) else ""
    return (size_i, name)


def _item_name(item: Any) -> str:
    return str(item.get("name", "")) if isinstance(item, dict) else ""


def _select_list_items(items: list[Any], limit: int, order: str) -> list[Any]:
    """按 order 从清单里挑出最多 limit 条（各模式唯一的实现，两个清单工具共用）。"""
    if order in ("size_desc", "size_asc"):
        desc = order == "size_desc"
        # 主键是大小；同尺寸一律按文件名升序（连第二关键字一起 reverse 会让输出不可预期）
        def _key(item: Any) -> tuple[int, str]:
            size, name = _item_size(item)
            return (-size, name) if desc else (size, name)

        return sorted(items, key=_key)[:limit]
    if len(items) <= limit:
        return list(items)  # 用不着截断：原顺序（按文件名）直接给
    if order == "name":
        return list(items[:limit])
    if order == "random":
        # 采样后按文件名排回去：随机的只是"挑中哪些"，清单读起来仍是有序的
        return sorted(random.sample(items, limit), key=_item_name)
    return _sample_evenly(items, limit)  # even


def _list_notes(
    *, matched: int, grep: str, returned: int, limit: int, order: str, unit: str
) -> list[str]:
    """过滤 / 截取的说明（各清单工具拼进返回体的 note）。"""
    notes: list[str] = []
    if grep:
        notes.append(f'已按 grep="{grep}" 过滤文件名：命中 {matched} 个{unit}。')
    if returned < matched:
        tail = f"要看更多把 limit 调大（当前 {limit}，上限 {LIST_ITEMS_MAX_LIMIT}）。"
        if order == "name":
            notes.append(
                f"{matched} 个{unit}超过上限，已按**文件名顺序**取前 {returned} 个（后面的没列）："
                "找具体文件用 grep 缩小范围，想看到整个范围就换 order=\"even\"（均匀采样）；" + tail
            )
        elif order == "random":
            notes.append(
                f"{matched} 个{unit}超过上限，已**随机采样** {returned} 个"
                "（每次调用挑中的可能不同，这是这个模式的本意）：要多看几批就再调一次，"
                "或用 grep / order=\"size_desc\" 缩小范围；" + tail
            )
        elif order in ("size_desc", "size_asc"):
            notes.append(
                f"{matched} 个{unit}超过上限，已按文件大小**{LIST_ORDER_LABELS[order]}**"
                f"取前 {returned} 个（只列了最大/最小的那批）：" + tail
            )
        else:
            notes.append(
                f"{matched} 个{unit}超过上限，已从整个清单里**均匀采样** {returned} 个"
                f"（含首尾、等距取，不是前 {returned} 个）："
                "要定位具体文件用 grep 缩小范围；" + tail
            )
    return notes


def _list_input_payload(
    runner: AgentRunner,
    *,
    grep: str = "",
    limit: int = LIST_ITEMS_DEFAULT_LIMIT,
    order: str = LIST_ORDER_DEFAULT,
    names: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """列输入文件（grep / limit / order 都在这一层做），供工具与子代理的锁定包装共用。

    names 只给子代理的"文件锁定"用：**先按锁定名单过滤、再按 order 截取**，顺序不能反——
    反过来的话采样可能把属于它的文件摇掉，看起来就像"这些文件不在清单里"。
    """
    pid = runner._project_id()
    cfg = urllib.parse.quote(runner.state.config_file_name or "config.yaml")
    files = runner._http_get(f"/api/projects/{pid}/files?counts=1&config={cfg}")
    input_files: list[dict[str, Any]] = []
    for item in files.get("input_files", []):
        if not isinstance(item, dict) or not item.get("is_file", True):
            continue
        parsed = item.get("sentences")
        input_files.append({
            "name": str(item.get("name") or ""),
            "size": item.get("size", 0),
            # 解析失败给 null，不要编造成 0：0 会被读成"这个文件不用翻"
            "sentences": parsed if isinstance(parsed, int) else None,
        })

    matched = _grep_items(input_files, grep)
    if names is not None:
        locked = set(names)
        matched = [f for f in matched if f["name"] in locked]
    shown = _select_list_items(matched, limit, order)
    notes = [
        "sentences 是输入文件解析出的条数，只用来估工作量：文本插件（如「跳过无日文句」）"
        "还没跑，真正要翻的句数通常比它少，所以按它估总时长会略偏大；null 表示解析失败。"
        "**它不是进度**——某文件缓存里有多少条与这个数无关，"
        "整体翻没翻完看 get_project_overview 的 files_translated/files_total。"
    ]
    notes.extend(
        _list_notes(
            matched=len(matched),
            grep=grep,
            returned=len(shown),
            limit=limit,
            order=order,
            unit="输入文件",
        )
    )
    return {
        "input_files": shown,
        "count": len(matched),
        "returned": len(shown),
        "sampled": len(shown) < len(matched),
        "sentences_total": sum(
            f["sentences"] for f in matched if isinstance(f["sentences"], int)
        ),
        "note": "；".join(notes),
    }


def _tool_list_input_files(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """列出待翻译文件（原文件，输入目录），带每个文件**解析出的条数**。

    口径一律是输入文件本身：`/files?counts=1` 让后端用文件插件解析原文数一遍。

    以前这里对"已有缓存的文件"改报**缓存条数**（理由是它与进度/ETA 同口径、更准），
    实际是个陷阱：缓存条数只说明缓存里存了多少条，一旦和别处的数字相等，读的人就会
    以为整个文件翻完了。句数是拿来看工作量的，不该兼职当进度——进度去看
    get_project_overview 的 files_translated / progress，那里才有真正翻到哪了。

    注意口径：这是文件插件解析出的**原始条目数**，文本插件（如「跳过无日文句」）
    还没跑，因此通常**大于**最终会送去翻译的句数（估工作量偏大是已知的取舍）。

    清单支持 grep（文件名子串）、limit（默认 100）与 order（怎么挑这 100 个：均匀采样 /
    文件名顺序 / 随机采样 / 按大小从大到小 / 从小到大，见 LIST_ORDER_MODES）。
    sentences_total 是**过滤后整份清单**的合计（含被截取省略的那些文件）：它是工作量估计，
    不能因为少显示了几行就变小。
    """
    return _list_input_payload(
        runner, grep=_list_grep(args), limit=_list_limit(args), order=_list_order(args)
    )


def _entry_index(entry: Any) -> int:
    """Return a comparable entry index, or -1 for malformed/missing values."""
    if not isinstance(entry, dict):
        return -1
    try:
        return int(entry.get("index", -1))
    except (TypeError, ValueError):
        return -1


def _tool_read_input_file(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """读取待翻译原文。filename 来自 list_input_files；index 统一为 1-based，支持区间
    （"1-100"、读取文件头几十句足够了解文风）。"""
    filename = str(args.get("filename", "")).strip()
    if not filename:
        raise AgentToolError("filename is required")
    pid = runner._project_id()
    cfg = urllib.parse.quote(runner.state.config_file_name)
    data = runner._http_get(f"/api/projects/{pid}/input/{urllib.parse.quote(filename)}?config={cfg}")
    entries = data.get("entries", [])
    index_spec = str(args.get("index", "") or "").strip()
    if not index_spec:
        return {"filename": filename, "count": len(entries), "returned": len(entries[:30]), "entries": entries[:30]}
    wanted = _parse_index_spec(index_spec)
    if not wanted:
        raise AgentToolError(f"无法解析 index 列表：{index_spec!r}（示例：1-100）")
    picked = [e for e in entries if _entry_index(e) in wanted]
    available = {_entry_index(e) for e in entries}
    missing = sorted(i for i in wanted if i not in available)
    result: dict[str, Any] = {
        "filename": filename,
        "count": len(entries),
        "returned": len(picked),
        "entries": picked,
    }
    if missing:
        result["missing_indexes"] = missing
    return result


def _tool_read_guideline(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """读取翻译规范：scope=global 读全局规范库（不带 name 列出文件名），scope=project 读项目规范。"""
    scope = str(args.get("scope", "") or "global").strip().lower()
    if scope == "project":
        return runner._http_get(f"/api/projects/{runner._project_id()}/guideline")
    name = str(args.get("name", "") or "").strip()
    if not name:
        data = runner._http_get("/api/translation-guidelines")
        guidelines = data.get("guidelines", [])
        current = "（见 get_project_overview 配置 common.gpt.translation_guideline）"
        return {
            "guidelines": guidelines,
            "note": (
                f"当前项目使用的全局规范：{current}。传 name 读取全文；"
                '本项目专属的项目规范用 scope="project" 读。'
            ),
        }
    return runner._http_get(f"/api/translation-guidelines/{urllib.parse.quote(name)}")


def _tool_write_project_guideline(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """写项目规范（overwrite 覆写 / append 增写 / replace 替换）。

    三种模式的实现都在后端（server → ProjectGuideline.apply_project_guideline_edit）：
    replace 没命中或命中多处会返回 400，由 _http_json 转成工具错误原样给模型看，
    这里不做二次加工，免得"到底改成了什么"有两套口径。

    行级 diff（前端渲染「变更」卡的依据）也按同一原则来：**写前、写后各读一次文件**，
    diff 的是真正落盘的内容，而不是在本地按三种模式重算一遍编辑结果——后者等于把
    "改成什么样"的逻辑实现第二遍，早晚跟后端那份对不上。

    审批卡上的提前预览同理，只是那次带 dry_run（见 _preview_guideline_write）：同一个入口，
    服务端算完就走。所以"批准前看到的 diff"和"批准后落盘的内容"来自同一段拼接逻辑。
    """
    endpoint = f"/api/projects/{runner._project_id()}/guideline"
    before = str((runner._http_get(endpoint) or {}).get("content") or "")
    body = {
        "mode": str(args.get("mode", "") or "").strip(),
        "content": str(args.get("content", "") or ""),
        "old_text": str(args.get("old_text", "") or ""),
        "new_text": str(args.get("new_text", "") or ""),
    }
    result = runner._http_put(endpoint, body)
    after = str((runner._http_get(endpoint) or {}).get("content") or "")

    diff = _diff_lines(before, after)
    added = sum(1 for r in diff["rows"] if r["op"] == "add")
    removed = sum(1 for r in diff["rows"] if r["op"] == "del")
    out: dict[str, Any] = dict(result) if isinstance(result, dict) else {}
    out.update(
        {
            "changed": before != after,
            "lines_before": len(before.splitlines()),
            "lines_after": len(after.splitlines()),
            "lines_added": added,
            "lines_removed": removed,
            "line_diff": diff,
        }
    )
    return out


def _tool_list_dict_files(runner: AgentRunner, _args: dict[str, Any]) -> Any:
    """列出项目字典清单（文件 + 行数）。内容用 read_dict 单独读取，这里不回传全文。"""
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


# 与 GalTransl/Dictionary.py 的解析口径一致：这两类别名的字典行，查找词不在第一列，
# 所以拼 key 时要按各自的位置取（条件字典: sp[2]；情景字典: sp[1]）。
_DICT_CONDITION_KEYS = frozenset({"pre_src", "post_src", "pre_dst", "post_dst", "pre_jp", "post_jp", "pre_zh", "post_zh"})
_DICT_SITUATION_KEYS = frozenset({"mono", "diag"})


def _dict_line_key(line: str, *, lenient: bool = False) -> str:
    """取字典行的匹配键（replace/append/delete 判断同一词条用）。

    普通行取第一列（原文）；条件字典/情景字典行取真正的查找词；空行与注释行
    （// 或 \\ 开头）返回 ""，表示不参与按键匹配。

    lenient=True 时，只有一列的行也按第一列当 key——delete 允许只写 key 而不
    复制整行；append/replace 用严格模式，避免把残缺行当成新词条写进字典。"""
    if not line.strip() or line.startswith("//") or line.startswith("\\\\"):
        return ""
    sp = line.replace("    ", "\t").split("\t")
    if len(sp) < 2:
        return sp[0].strip() if lenient else ""
    head = sp[0].strip()
    if head in _DICT_CONDITION_KEYS and len(sp) >= 4:
        return sp[2].strip()
    if head in _DICT_SITUATION_KEYS and len(sp) >= 3:
        return sp[1].strip()
    return head


def _split_dict_incoming(content: str) -> list[str]:
    """待写入内容切行：统一换行符，丢掉空行，保留注释行。"""
    text = str(content or "").replace("\r\n", "\n").replace("\r", "\n")
    return [line for line in text.split("\n") if line.strip()]


def _merge_dict_lines(before_lines: list[str], incoming: list[str], action: str) -> tuple[list[str], dict[str, Any]]:
    """按 action 把 incoming 合并到 before_lines，返回 (新行列表, 附加统计)。

    - append：新词条追加到末尾；key 已存在则跳过并记入 skipped_duplicate_keys。
    - replace：按 key 替换已有行；未匹配的 key 记入 not_found_keys，不新增（要新增用 append）。
    - delete：按 key 删除已有行；未匹配的 key 记入 not_found_keys。
    """
    if action == "overwrite":
        return list(incoming), {}

    if action == "delete":
        # incoming 是“要删除的词条”，允许只写 key（lenient）不复制整行
        wanted: list[str] = []
        for line in incoming:
            key = _dict_line_key(line, lenient=True)
            if key and key not in wanted:
                wanted.append(key)
        wanted_set = set(wanted)
        kept: list[str] = []
        deleted: list[str] = []
        for line in before_lines:
            key = _dict_line_key(line)
            if key and key in wanted_set:
                deleted.append(key)
                continue
            kept.append(line)
        extra: dict[str, Any] = {"deleted_keys": deleted}
        not_found = [k for k in wanted if k not in deleted]
        if not_found:
            extra["not_found_keys"] = not_found
        return kept, extra

    existing: dict[str, int] = {}
    for i, line in enumerate(before_lines):
        key = _dict_line_key(line)
        if key:
            existing.setdefault(key, i)

    if action == "append":
        new_lines = list(before_lines)
        appended: list[str] = []
        duplicates: list[str] = []
        for line in incoming:
            key = _dict_line_key(line)
            if not key:  # 注释/无键行：原样追加
                new_lines.append(line)
                continue
            if key in existing:
                if key not in duplicates:
                    duplicates.append(key)
                continue
            new_lines.append(line)
            existing[key] = len(new_lines) - 1
            appended.append(key)
        extra: dict[str, Any] = {"appended_keys": appended}
        if duplicates:
            extra["skipped_duplicate_keys"] = duplicates
        return new_lines, extra

    # replace
    new_lines = list(before_lines)
    replaced: list[str] = []
    not_found: list[str] = []
    for line in incoming:
        key = _dict_line_key(line)
        if not key:  # 注释/无键行在 replace 下忽略
            continue
        idx = existing.get(key)
        if idx is None:
            if key not in not_found:
                not_found.append(key)
            continue
        new_lines[idx] = line
        if key not in replaced:
            replaced.append(key)
    extra = {"replaced_keys": replaced}
    if not_found:
        extra["not_found_keys"] = not_found
    return new_lines, extra


def _dict_new_lines(before_lines: list[str], content: str, action: str) -> tuple[list[str], dict[str, Any]]:
    """按 action 算出写入后的整份字典行（**只算不写**），返回（新行, 附带的明细）。

    save_dict 与审批卡上的「将要变更」预览共用这一份合并规则：卡上给用户看的 diff 就是
    真执行会写下去的东西（append 跳过重复 key、replace 只改命中的 key 这些细节都一致）。
    """
    if action == "overwrite":
        return content.replace("\r\n", "\n").replace("\r", "\n").split("\n"), {}
    return _merge_dict_lines(before_lines, _split_dict_incoming(content), action)


def _tool_save_dict(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """写入项目字典。action 决定写入方式：

    - overwrite（默认）：整文件覆盖，等价于旧行为；
    - replace：按每行的 key 替换已有词条，未匹配的 key 不新增；
    - append：把行追加到末尾，key 已存在的行跳过；
    - delete：按 key 删除词条（content 传要删的词条，可整行粘贴或只写 key）。
    """
    file_key = str(args.get("file_key", "")).strip()
    if not file_key:
        raise AgentToolError("file_key is required")
    action = str(args.get("action", "") or "overwrite").strip().lower() or "overwrite"
    if action not in ("overwrite", "replace", "append", "delete"):
        raise AgentToolError("action must be one of: overwrite, replace, append, delete")
    content = str(args.get("content", ""))
    if action == "delete" and not _split_dict_incoming(content):
        raise AgentToolError("delete 需要 content（要删除的词条，每行一个 key）")
    pid = runner._project_id()

    # 先读旧内容（算行级 diff + 作为 append/replace 的基底），写完后随结果返回
    cfg = urllib.parse.quote(runner.state.config_file_name)
    before_lines: list[str] = []
    data = runner._http_get(f"/api/projects/{pid}/dictionary/project?config={cfg}")
    contents = data.get("dict_contents", {})
    old = contents.get(file_key)
    if isinstance(old, dict):
        before_lines = [str(x) for x in old.get("lines", [])]

    new_lines, extra = _dict_new_lines(before_lines, content, action)

    before_text = "\n".join(before_lines)
    new_text = "\n".join(new_lines)
    if new_text == before_text:
        return {"file_key": file_key, "action": action, "note": "内容没有变化，未写入", **extra}

    body = {
        "config_file_name": runner.state.config_file_name,
        "file_key": file_key,
        "content": new_text,
    }
    runner._http_post(f"/api/projects/{pid}/dictionary/project/save", body)
    diff = _diff_lines(before_text, new_text)
    added = sum(1 for r in diff["rows"] if r["op"] == "add")
    removed = sum(1 for r in diff["rows"] if r["op"] == "del")
    return {
        "file_key": file_key,
        "action": action,
        "line_count_before": len(before_lines),
        "line_count_after": len(new_lines),
        "lines_added": added,
        "lines_removed": removed,
        "line_diff": diff,
        **extra,
    }


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


def _parse_filter_keywords(raw: Any) -> list[str]:
    """字符串列表入参（单个字符串 / 字符串数组 / 配置里那串）→ 去空白、去重保序的列表。

    manage_problem_filter / manage_problem_white_list 与审批卡上的「将要变更」预览共用：
    字符串既可能是单个项，也可能是换行分隔的一串（模型两种都爱写）。
    """
    items = raw.split("\n") if isinstance(raw, str) else raw
    if not isinstance(items, list):
        return []
    cleaned = [k.strip() for k in items if isinstance(k, str) and k.strip()]
    return list(dict.fromkeys(cleaned))  # 去重保序


def _load_problem_filter_keys(
    runner: AgentRunner, pid: str, config_name: str
) -> tuple[dict[str, Any], list[str]]:
    """读配置里的 common.problemFilterKey：返回（整份 config, 去重保序的关键字清单）。

    manage_problem_filter 的 list/add/remove 三支与预览都走它——"现在的清单是什么"
    只能有一处口径。
    """
    data = runner._http_get(f"/api/projects/{pid}/config?config={urllib.parse.quote(config_name)}")
    config = data.get("config") if isinstance(data, dict) else None
    if not isinstance(config, dict):
        raise AgentToolError("项目配置读取失败")
    common = config.get("common")
    if not isinstance(common, dict):
        common = {}
        config["common"] = common
    return config, _parse_filter_keywords(common.get("problemFilterKey", []))


def _load_problem_filter_stats(
    runner: AgentRunner, pid: str, keys: list[str], config_name: str
) -> dict[str, Any]:
    """list 的结果：过滤清单 + 每条当前各挡住了多少条问题。

    条数由服务端扫缓存算（/problem_filter_stats），与 list_problems / 进度统计同一口径
    （白名单命中的条目不算）。数字是「现在」的快照：0 说明这条过滤项当前一条也挡不到，
    多半已经没用了。统计取不到（老服务端/接口异常）时退回只有清单的结果——list 是只读查询，
    不该因为统计挂掉。
    """
    result: dict[str, Any] = {"filter_keys": keys, "count": len(keys)}
    if not keys:
        return result
    try:
        data = runner._http_get(
            f"/api/projects/{pid}/problem_filter_stats?config={urllib.parse.quote(config_name)}"
        )
    except Exception:  # noqa: BLE001
        return result
    if not isinstance(data, dict):
        return result
    for key in ("filters", "problem_entries", "visible_entries"):
        if data.get(key) is not None:
            result[key] = data[key]
    return result


def _plan_problem_filter(
    keys: list[str], action: str, keywords: list[str], field: str = "problemFilterKey"
) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    """算出这次 add/remove 实际会动到哪些项（**只读**）：返回（命中, 未命中, changes）。

    与 _tool_manage_problem_filter / _tool_manage_problem_white_list 共用：卡上列出的
    增删就是真执行会写进去的那些；field 决定变更卡上显示的是哪个配置键。
    """
    existing = set(keys)
    if action == "add":
        hit = [k for k in keywords if k not in existing]  # 实际新增
        miss = [k for k in keywords if k in existing]  # 本来就有
        changes = [_change(field, None, k, "add") for k in hit]
    else:  # remove
        hit = [k for k in keywords if k in existing]  # 实际移除
        miss = [k for k in keywords if k not in existing]  # 本来就没有
        changes = [_change(field, k, None, "remove") for k in hit]
    return hit, miss, changes


def _is_valid_regex(pattern: str) -> bool:
    """过滤项是正则：写坏的模式在 add 时就拒掉（并提示转义），别留到过滤时才发现。"""
    try:
        re.compile(pattern)
    except re.error:
        return False
    return True


def _tool_manage_problem_filter(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """增/删/查项目配置 common.problemFilterKey（问题过滤关键字）。

    与桌面端「缓存与问题」页同一套配置。**正则匹配**：keyword 是一条正则，按 re.search
    命中问题项的那一项才会在 list_problems / 进度统计里被过滤掉（如 `缺失.*标点`）。
    **原则上只过滤小类、不过滤大类**（`残留日文`、`^残留日文：` 这类整类写法等于放弃复核，
    要在提示里挡住）。add/remove 是对清单里字符串的精确增删（区分大小写）；
    add 时校验正则可编译，写坏了直接报错并提示转义。
    """
    action = str(args.get("action", "")).strip()
    if action not in ("list", "add", "remove"):
        raise AgentToolError("action must be one of: list, add, remove")
    pid = runner._project_id()
    config_name = runner.state.config_file_name or DEFAULT_CONFIG_FILE

    if action == "list":
        _, keys = _load_problem_filter_keys(runner, pid, config_name)
        # 每条过滤项当前各挡住了多少条问题：判断哪条已经没用了（problems 为 0）
        return _load_problem_filter_stats(runner, pid, keys, config_name)

    # keyword 支持单个字符串或字符串数组（一次增删多个）：去重保序、忽略空串
    keywords = _parse_filter_keywords(args.get("keyword"))
    if not keywords:
        raise AgentToolError("keyword is required for add/remove（字符串或字符串数组）")
    if action == "add":
        invalid = [k for k in keywords if not _is_valid_regex(k)]
        if invalid:
            raise AgentToolError(
                "这些过滤项不是合法正则：" + "、".join(invalid)
                + "。过滤项按正则匹配；想按字面过滤请转义特殊字符（\\. \\( \\[ \\*）。"
            )

    config, keys = _load_problem_filter_keys(runner, pid, config_name)
    hit, miss, changes = _plan_problem_filter(keys, action, keywords)
    if action == "add":
        hit_key, miss_key, miss_note = "added", "already_present", "已在列表中"
    else:  # remove
        hit_key, miss_key, miss_note = "removed", "not_found", "不在列表中"

    if not hit:
        return {
            "filter_keys": keys,
            "count": len(keys),
            "note": f"这些关键字{miss_note}，过滤清单未变化",
        }

    if action == "add":
        keys.extend(hit)
    else:
        removing = set(hit)
        keys = [k for k in keys if k not in removing]

    config["common"]["problemFilterKey"] = keys
    runner._http_put(
        f"/api/projects/{pid}/config",
        {"config": config, "config_file_name": config_name},
    )
    # 配置已写回：进度缓存按 mtime 自动失效，后续 list_problems 立即用新过滤
    result: dict[str, Any] = {"filter_keys": keys, "count": len(keys), hit_key: hit, "changes": changes}
    if miss:
        result[miss_key] = miss
    return result


def _load_problem_white_list(
    runner: AgentRunner, pid: str, config_name: str
) -> tuple[dict[str, Any], list[str]]:
    """读配置里的 common.problemWhiteList：返回（整份 config, 去重保序的条目清单）。

    manage_problem_white_list 的 list/add/remove 三支与审批卡预览都走它——"现在的
    白名单是什么"只能有一处口径。
    """
    data = runner._http_get(f"/api/projects/{pid}/config?config={urllib.parse.quote(config_name)}")
    config = data.get("config") if isinstance(data, dict) else None
    if not isinstance(config, dict):
        raise AgentToolError("项目配置读取失败")
    common = config.get("common")
    if not isinstance(common, dict):
        common = {}
        config["common"] = common
    return config, _parse_filter_keywords(common.get("problemWhiteList", []))


_WHITE_LIST_ENTRY_HINT = '条目格式为 "<缓存文件名>:<index>"（如 "01.json:12"），区间写 "01.json:12-15"'


def _tool_manage_problem_white_list(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """增/删/查项目配置 common.problemWhiteList（问题白名单）。

    白名单是「缓存文件 + 条目 index」的名单，命中的条目等价于勾了 skip_check：
    list_problems / 进度统计不再显示它的问题，缓存重建时也不再检测。与
    manage_problem_filter（按问题文本子串整类过滤）互补：白名单按具体位置豁免。
    """
    action = str(args.get("action", "")).strip()
    if action not in ("list", "add", "remove"):
        raise AgentToolError("action must be one of: list, add, remove")
    pid = runner._project_id()
    config_name = runner.state.config_file_name or DEFAULT_CONFIG_FILE

    if action == "list":
        _, entries = _load_problem_white_list(runner, pid, config_name)
        return {"white_list": entries, "count": len(entries)}

    entries = _parse_filter_keywords(args.get("entry"))
    if not entries:
        raise AgentToolError(f"entry is required for add/remove（{_WHITE_LIST_ENTRY_HINT}）")
    if action == "add":
        invalid = [e for e in entries if parse_problem_white_list_entry(e) is None]
        if invalid:
            raise AgentToolError(f"这些条目格式不对：{'、'.join(invalid)}；{_WHITE_LIST_ENTRY_HINT}")

    config, current = _load_problem_white_list(runner, pid, config_name)
    hit, miss, changes = _plan_problem_filter(current, action, entries, field="problemWhiteList")
    if action == "add":
        hit_key, miss_key, miss_note = "added", "already_present", "已在白名单里"
    else:  # remove
        hit_key, miss_key, miss_note = "removed", "not_found", "不在白名单里"

    if not hit:
        return {
            "white_list": current,
            "count": len(current),
            "note": f"这些条目{miss_note}，白名单未变化",
        }

    if action == "add":
        current.extend(hit)
    else:
        removing = set(hit)
        current = [k for k in current if k not in removing]

    config["common"]["problemWhiteList"] = current
    runner._http_put(
        f"/api/projects/{pid}/config",
        {"config": config, "config_file_name": config_name},
    )
    result: dict[str, Any] = {
        "white_list": current, "count": len(current), hit_key: hit, "changes": changes
    }
    if miss:
        result[miss_key] = miss
    return result


def _parse_config_value(raw: Any) -> Any:
    """把工具参数里的标量值转成 YAML 配置里应有的类型。

    模型经 JSON 传参，bool/数字天然带类型；字符串保持字符串——
    YAML 里本来就大量存在如 "Num"/"size" 的字符串枚举，不做猜测。"""
    return raw


def _set_nested(config: dict[str, Any], dotted: str, value: Any) -> bool:
    """按点号路径写入配置（如 common.gpt.contextNum）。返回键是否原本存在。"""
    parts = dotted.split(".")
    node: Any = config
    for p in parts[:-1]:
        if not isinstance(node, dict) or p not in node:
            return False
        node = node[p]
    if not isinstance(node, dict) or parts[-1] not in node:
        return False
    node[parts[-1]] = value
    return True


# 点号键的特殊展平：YAML 里 common 下可以直接写 "gpt.numPerRequestTranslate"
# 这种带点的键（不嵌套），但也存在真正的嵌套（如 common.gpt 为一个 dict）。
# 匹配顺序：section 内字面点号键 → section 内短键 → 整体嵌套路径。
def _set_config_key(config: dict[str, Any], dotted: str, value: Any) -> bool:
    sections = ("common", "problemAnalyze", "dictionary", "plugin", "proxy", "backendSpecific")
    section, _, rest = dotted.partition(".")
    if section in sections and rest:
        node = config.get(section)
        if isinstance(node, dict):
            # 字面点号键（common 的展平写法）
            if dotted in node:
                node[dotted] = value
                return True
            # 短键（去掉 section 前缀后直接是键名）
            if rest in node:
                node[rest] = value
                return True
            # 真嵌套（section 下有同名子 dict），交给通用嵌套写入
            if isinstance(node.get(rest.split(".")[0]), dict):
                return _set_nested(config, dotted, value)
            return False
    return _set_nested(config, dotted, value)


# _get_config_key 的「键不存在」哨兵（None 和 False 都是合法配置值，不能用）
_MISSING = object()


def _get_config_key(config: dict[str, Any], dotted: str) -> Any:
    """按 _set_config_key 的同一套匹配顺序读配置值。键不存在返回 _MISSING。"""
    sections = ("common", "problemAnalyze", "dictionary", "plugin", "proxy", "backendSpecific")
    section, _, rest = dotted.partition(".")
    if section in sections and rest:
        node = config.get(section)
        if isinstance(node, dict):
            if dotted in node:
                return node[dotted]
            if rest in node:
                return node[rest]
            if isinstance(node.get(rest.split(".")[0]), dict):
                # 复用 _set_nested 的路径遍历
                parts = dotted.split(".")
                cur: Any = config
                for p in parts:
                    if not isinstance(cur, dict) or p not in cur:
                        return _MISSING
                    cur = cur[p]
                return cur
            return _MISSING
    parts = dotted.split(".")
    cur: Any = config
    for p in parts:
        if not isinstance(cur, dict) or p not in cur:
            return _MISSING
        cur = cur[p]
    return cur


def _plan_config_updates(
    config: dict[str, Any], updates: list[Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """把 updates 解析成「要改哪些键、改成什么」（**只读**：在副本上算，不动传入的 config）。

    update_project_config 的落盘与审批卡上的「将要变更」预览共用这一份判断：卡上显示的
    before→after 就是真执行会写下去的东西。在**副本**上跑一遍 _set_config_key，而不是另写
    一套"这个键存不存在"的判断——展开的点号键 / 短键 / 真嵌套（见 _set_config_key）那套
    匹配顺序只有一处实现，连"同一个键被改两次时第二条的 before"这种细节也一致。
    返回（applied, skipped, changes）。
    """
    probe = copy.deepcopy(config)
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    for item in updates:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", "")).strip()
        if not key:
            continue
        if key in LEGACY_PROMPT_PATHS:
            # 旧工程里这两个键还在（翻译流程仍认），但已不对外提供：直接拒掉，
            # 免得模型绕开 write_project_guideline 去改一份"看不见的"旧机制
            skipped.append({
                "key": key,
                "reason": "该键已下线（仅为旧工程兼容保留），请改用 write_project_guideline 写项目规范",
            })
            continue
        value = _parse_config_value(item.get("value"))
        before = _get_config_key(probe, key)
        if _set_config_key(probe, key, value):
            applied.append({"key": key, "value": value})
            kind = "add" if before is _MISSING else "replace"
            changes.append(_change(key, before if before is not _MISSING else None, value, kind))
        else:
            skipped.append({"key": key, "reason": "配置里不存在该键；只能修改已存在的键"})
    return applied, skipped, changes


def _tool_update_project_config(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """修改项目配置：读-改-写回（与桌面端「项目配置」页同一通道）。

    只允许改已存在的键，防止模型凭空捏造配置项；键名与
    get_project_overview 返回的 config/config_field_descriptions 一致。"""
    updates = args.get("updates")
    if not isinstance(updates, list) or not updates:
        raise AgentToolError("updates must be a non-empty array of {key, value}")
    pid = runner._project_id()
    config_name = runner.state.config_file_name or DEFAULT_CONFIG_FILE
    data = runner._http_get(f"/api/projects/{pid}/config?config={urllib.parse.quote(config_name)}")
    config = data.get("config")
    if not isinstance(config, dict):
        raise AgentToolError("项目配置读取失败")

    # 先在一份副本上算出「哪些键、改成什么」（与审批卡上的预览同一份判断），
    # 再把同一批改动打到真 config 上——同一个 _set_config_key、同样顺序。
    applied, skipped, changes = _plan_config_updates(config, updates)
    for item in applied:
        _set_config_key(config, item["key"], item["value"])

    if not applied:
        return {"updated": 0, "applied": [], "skipped": skipped or [{"key": "", "reason": "updates 为空"}]}

    runner._http_put(
        f"/api/projects/{pid}/config",
        {"config": config, "config_file_name": config_name},
    )
    result: dict[str, Any] = {"updated": len(applied), "applied": applied, "changes": changes}
    if skipped:
        result["skipped"] = skipped
    return result


# ---- GPT 字典用于 name 字段（dictionary.useGPTDictInName）----
# 翻译时（GalTransl/Name.py 的 load_name_table）会拿 GPT 字典里同名的词条去补 name 字段，
# 而且这个开关默认就是开的（见 DefaultProjectConfig）。也就是说人名表里"译名空着、字典里
# 有"的行其实早已生效——get_name_table 若不体现这一点，模型会去补一整批字典里早就有的名字。
# 下面这套就是按同一口径把字典里的译名补进返回值（前端人名页的 overlayGptDictOntoNames
# 是同一套规则的另一份实现：都只补空译名，不覆盖表里已有的）。


def _gpt_dict_line(raw: str) -> tuple[str, str]:
    """GPT 字典的一行 →（查找词, 替换词）；不是词条行则返回两个空串。

    照抄 GalTransl/Dictionary.py 的 CGptDict.load_dic：跳过空行/注释行，4 个空格当 Tab，
    兼容 `src->dst #note` 写法，至少两列才算词条。**不做 strip**——字典的命中判定是全等
    比较（CGptDict.get_dst），把空白修掉会让"其实查不到"的行看起来像命中了。
    """
    if not raw or raw.startswith("\n"):
        return "", ""
    if raw.lstrip().startswith(("//", "\\\\")):  # 注释行
        return "", ""
    line = raw.replace("    ", "\t")
    if "->" in line:
        line = line.replace("->", "\t").replace("#", "\t")
    parts = line.rstrip("\r\n").split("\t")
    if len(parts) < 2:
        return "", ""
    return parts[0], parts[1]


def _gpt_dict_name_map(runner: AgentRunner) -> dict[str, str]:
    """项目 GPT 字典 → {查找词: 替换词}（只读）。

    同一查找词出现多次时取**首次**出现的那条：CGptDict.get_dst 返回的就是第一个 search_word
    全等的词条（字典里"后写的覆盖先写的"在这里不成立）。
    """
    pid = runner._project_id()
    cfg = urllib.parse.quote(runner.state.config_file_name or DEFAULT_CONFIG_FILE)
    data = runner._http_get(f"/api/projects/{pid}/dictionary/project?config={cfg}")
    if not isinstance(data, dict):
        return {}
    contents = data.get("dict_contents", {})
    if not isinstance(contents, dict):
        return {}
    out: dict[str, str] = {}
    for key in data.get("gpt_dict_files", []) or []:
        entry = contents.get(str(key))
        if not isinstance(entry, dict):
            continue
        for raw in entry.get("lines", []) or []:
            src, dst = _gpt_dict_line(str(raw))
            if src and dst:
                out.setdefault(src, dst)
    return out


def _use_gpt_dict_in_name(runner: AgentRunner) -> bool:
    """配置里 dictionary.useGPTDictInName 是否开着。

    取值口径与 Name.py 一致（**缺键 = 没开**）：配置里没写就意味着翻译时不会拿字典补 name
    字段，这时在 get_name_table 里补上译名等于骗模型——它会以为这些名字已经有人管了。
    配置读不到也按"没开"处理：这只是附加信息，不值得为一个可选展示把工具弄失败。
    """
    try:
        pid = runner._project_id()
        cfg = urllib.parse.quote(runner.state.config_file_name or DEFAULT_CONFIG_FILE)
        data = runner._http_get(f"/api/projects/{pid}/config?config={cfg}")
    except Exception as exc:  # noqa: BLE001
        _log(f"  ⚠ 读项目配置失败，get_name_table 不补 GPT 字典译名：{exc}")
        return False
    config = data.get("config") if isinstance(data, dict) else None
    if not isinstance(config, dict):
        return False
    value = _get_config_key(config, "dictionary.useGPTDictInName")
    return value is not _MISSING and bool(value)


def _fill_names_from_gpt_dict(
    names: list[Any], gpt_map: dict[str, str]
) -> tuple[list[Any], list[str], list[str]]:
    """把 GPT 字典里的译名补到**译名为空**的人名行上（只算不写）。

    与人名翻译页的 overlayGptDictOntoNames 同一套规则：只补空的，不覆盖表里已有的译名——
    表里写下的译名是用户（或 Agent）的决定，字典只回答"这行其实已经有出处了"。
    返回（补好的人名行, 由字典补上的 src_name, 仍然没有译名的 src_name）。
    """
    out: list[Any] = []
    filled: list[str] = []
    still_empty: list[str] = []
    for item in names:
        if not isinstance(item, dict):
            out.append(item)
            continue
        src = str(item.get("src_name") or item.get("name") or "").strip()
        dst = str(item.get("dst_name") or "")
        if dst.strip() or not src:
            out.append(item)
            continue
        mapped = gpt_map.get(src)
        if not mapped:
            out.append(item)
            still_empty.append(src)
            continue
        filled.append(src)
        # 盖上出处：这行的译名不在表里，是字典在翻译时给 name 字段补的
        out.append({**item, "dst_name": mapped, "dst_name_source": "gpt_dict"})
    return out, filled, still_empty


def _tool_get_name_table(runner: AgentRunner, _args: dict[str, Any]) -> Any:
    """读人名替换表；useGPTDictInName 开着时，顺带把 GPT 字典里已有的译名补进返回值。

    为什么要补：翻译时 name 字段会吃 GPT 字典里同名的词条，表里译名空着而字典里有人的行
    **其实已经生效**。不体现这一点，模型会去补一整批"字典里早就写过"的名字。补上之后
    still_empty 才是真正要它动手的那几个。

    补的是**空译名**的行（与前端人名页同一套规则）：表里已有的译名不动，字典只作补充说明；
    这些行在返回里带 dst_name_source="gpt_dict"——要固定住某行的译名得写进表（save_name_table），
    要改它则得改字典。

    **只读**：一个字节都不写回；配置/字典读不到就退回原样返回，绝不因此把工具弄失败。
    """
    pid = runner._project_id()
    data = runner._http_get(f"/api/projects/{pid}/name-table")
    names = data.get("names") if isinstance(data, dict) else None
    if not isinstance(names, list) or not names:
        return data
    if not _use_gpt_dict_in_name(runner):
        return data
    try:
        gpt_map = _gpt_dict_name_map(runner)
    except Exception as exc:  # noqa: BLE001 - 字典读不到就少补一块，表本身照常返回
        _log(f"  ⚠ 读 GPT 字典失败，get_name_table 返回原始人名表：{exc}")
        return data
    overlaid, filled, still_empty = _fill_names_from_gpt_dict(names, gpt_map)
    return {
        **data,
        "names": overlaid,
        "use_gpt_dict_in_name": True,
        "filled_from_gpt_dict": filled,
        "still_empty": still_empty,
        "note": (
            "dictionary.useGPTDictInName 开着：names 里译名为空、而 GPT 字典收录了的行，"
            "已经按字典的译名补上（带 dst_name_source=gpt_dict，翻译时真的会生效），"
            "它们也列在 filled_from_gpt_dict 里。**still_empty 才是表与字典都没有、需要你补的**；"
            "要把某个译名固定下来（不再依赖字典）用 save_name_table 写进表里。"
        ),
    }


def _name_table_entries(raw: Any) -> dict[str, str]:
    """人名表 entries → {src_name: dst_name}（按出现顺序）。

    接口两个方向都是这个结构（get_name_table 返回、save_name_table 入参）：
    [{src_name, dst_name, count}]，对应 CSV 的 SRC_Name / DST_Name / Count 三列。
    顺带认下老会话里可能还留着的 {"name": ...} 与裸字符串写法（当成只有 src）。
    """
    out: dict[str, str] = {}
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, dict):
            src = str(item.get("src_name") or item.get("name") or "").strip()
            dst = str(item.get("dst_name") or "")
        else:
            src, dst = str(item).strip(), ""
        if src:
            out[src] = dst
    return out


def _name_table_changes(
    old_raw: Any, new_raw: Any
) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    """人名表的增删改明细（**只算不写**）：返回（新增的 src_name, 移除的 src_name, changes）。

    save_name_table 与审批预览共用。**按 src_name 逐条比 dst_name**，而不是把整条 entry
    拿去比对：CSV 的键是 SRC_Name，值才是 DST_Name，count 只是出现次数统计——整条比会让
    "count 变了"也算成加一条删一条，卡上就会冒出「+ {'src_name': ...}」和「− None」这种
    噪音（字段名一换更是整表都成了新增）。count 不在比对范围内。
    """
    old_map = _name_table_entries(old_raw)
    new_map = _name_table_entries(new_raw)
    added = [src for src in new_map if src not in old_map]
    removed = [src for src in old_map if src not in new_map]
    changes: list[dict[str, Any]] = []
    changes.extend(_change(src, None, new_map[src] or None, "add") for src in added)
    changes.extend(_change(src, old_map[src] or None, None, "remove") for src in removed)
    # 译名改动（同一个 src_name、dst_name 变了）：这才是这个工具最常干的事
    changes.extend(
        _change(src, old_map[src], dst, "replace")
        for src, dst in new_map.items()
        if src in old_map and old_map[src] != dst
    )
    return added, removed, changes


def _tool_save_name_table(runner: AgentRunner, args: dict[str, Any]) -> Any:
    names = args.get("names", [])
    if not isinstance(names, list):
        raise AgentToolError("names must be an array")
    pid = runner._project_id()
    # 先读旧表（算 changes 的基底），再整表覆写
    old = runner._http_get(f"/api/projects/{pid}/name-table")
    result = runner._http_post(f"/api/projects/{pid}/name-table/save", {"names": names})
    added, removed, changes = _name_table_changes(old.get("names", []), names)
    return {
        **(result if isinstance(result, dict) else {}),
        "names_added": added,
        "names_removed": removed,
        "changes": changes,
    }


def _tool_start_translation(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """启动翻译任务。

    后端必须用**翻译任务会用的那份**（前端按「项目选择 → 否则全局『翻译器默认』」送来，
    见 state.translator_profile_data），不能用 Agent 自己那份——否则任务会拿着 Agent 的
    模型跑，可用性检测也跟着测错模型（用户就是这么发现的）。只有前端没送来时才回落到
    Agent 那份，并在返回里说明。
    """
    translator = str(args.get("translator", "")).strip()
    if not translator:
        raise AgentToolError("translator is required")
    state = runner.state
    profile = state.translator_profile_data or state.backend_profile_data
    body = {
        "project_dir": state.project_dir,
        "config_file_name": state.config_file_name,
        "translator": translator,
        "backend_profile_data": profile,
    }
    files = args.get("files")
    if files is not None:
        if not isinstance(files, list) or not files:
            raise AgentToolError("files must be a non-empty array of filenames")
        body["input_files"] = [str(f).strip() for f in files if str(f).strip()]
        if not body["input_files"]:
            raise AgentToolError("files 里没有有效的文件名")
    result = runner._http_post("/api/jobs", body)
    used = _backend_summary(profile, state.translator_profile_name)
    out: dict[str, Any] = {
        "job_id": result.get("job_id"),
        "status": result.get("status"),
        "translator": translator,
        # 实际用哪份后端起任务（名字/类型/模型），出问题时一眼能对上
        "backend": used,
        **({"files": body["input_files"]} if files is not None else {}),
    }
    if not state.translator_profile_data:
        out["note"] = (
            "没拿到「翻译任务会用」的后端配置（前端没随消息送 translator_profile_data，"
            "通常是项目选择或全局「翻译器默认」那份），"
            f"本次用的是 Agent 自己的后端（{used['name'] or used['type'] or '未知'}）。"
        )
    return out


def _tool_stop_translation(runner: AgentRunner, _args: dict[str, Any]) -> Any:
    pid = runner._project_id()
    return runner._http_post(f"/api/projects/{pid}/stop", {})


WAIT_SECONDS_MAX = 1800  # 单次等待上限 30 分钟，避免 Agent 卡死在一次无限等待里
WAIT_TICK = 0.5  # 倒计时刷新步长（秒），兼顾界面流畅与轮询开销
# 带 job_id 时查任务状态的间隔（秒）：0.5s 那是给界面倒计时用的，查后端别这么勤
WAIT_JOB_POLL_SECONDS = 3.0
# 任务已经结束的状态（见 Service.JobState.status）：等到其中之一就不必再等了
WAIT_JOB_DONE_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _find_job(runner: AgentRunner, job_id: str) -> dict[str, Any] | None:
    """在任务列表里按 id 找一个任务：**列表里没有这个 id 返回 None**。

    用列表接口而不是 /api/jobs/{id}：后者对未知 id 直接 404（`_http_json` 会抛错），
    而"这个 id 不在列表里"对等待来说是个正常结局（id 写错/任务已被清掉），不该跟
    "查询失败"混在一起。列表本身拿不到（网络/格式不对）时抛错，由调用方当查询失败处理
    ——继续等，别把一次抖动当成"任务不见了"。
    """
    data = runner._http_get("/api/jobs")
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        raise AgentToolError("任务列表读取失败：/api/jobs 没有返回 jobs 数组")
    for job in jobs:
        if isinstance(job, dict) and str(job.get("job_id") or "") == job_id:
            return job
    return None


def _tool_wait(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """等待指定时长；给了 job_id 就"它先结束，或时长先到"，谁先到算谁。

    期间持续推 wait_tick 事件供界面显示倒计时。等待可被停止信号立即打断：
    先等满则 normal，被打断则 interrupted。无论哪种都以工具成功返回，
    把状态交给模型判断下一步，而不是抛错中断整个循环。
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
        raise AgentToolError(
            "未指定等待时长：请给出 seconds 或 minutes"
            "（带 job_id 时也要给——它是兜底：任务先结束就提前返回）"
        )
    total = min(total, WAIT_SECONDS_MAX)

    # 要盯的任务（可选）：给了它就不用非等满时长——任务先结束就立刻收尾
    job_id = str(args.get("job_id", "") or "").strip()

    reason = str(args.get("reason", "") or "").strip()
    total_ms = int(total * 1000)
    started = time.monotonic()
    # 事件带上本次工具调用 id：界面据此把倒计时挂到这一行（多次等待各挂各行）
    call_id = runner._active_tool_call_id
    _log(
        f"  ⏳ 开始等待 {total:g}s"
        + (f"（或任务 {job_id} 先结束）" if job_id else "")
        + (f"（{reason}）" if reason else "")
    )
    runner._emit("wait_start", {
        "id": call_id,
        "seconds": round(total, 1),
        "total_ms": total_ms,
        "reason": reason,
        **({"job_id": job_id} if job_id else {}),
    })

    interrupted = False
    job_status = ""
    job_success: bool | None = None
    job_error = ""
    job_found = True
    next_poll = 0.0  # 先立刻查一次，之后每 WAIT_JOB_POLL_SECONDS 一次
    while True:
        if runner.stop_event.is_set():
            interrupted = True
            break
        elapsed = time.monotonic() - started
        if job_id and elapsed >= next_poll:
            next_poll = elapsed + WAIT_JOB_POLL_SECONDS
            found: dict[str, Any] | None = None
            query_ok = True
            try:
                found = _find_job(runner, job_id)
            except Exception as exc:  # noqa: BLE001 - 一次查询失败当"还在跑"，时间到了照样收尾
                query_ok = False
                _log(f"  ⏳ 查询任务 {job_id} 状态失败（{exc}），继续等")
            if query_ok and found is None:
                job_found = False  # id 写错或任务已被清掉：再等下去没意义
                break
            if found is not None:
                job_status = str(found.get("status") or "")
                if job_status in WAIT_JOB_DONE_STATUSES:
                    job_success = bool(found.get("success"))
                    job_error = str(found.get("error") or "")
                    break
        if elapsed >= total:
            break
        remaining_ms = max(0, total_ms - int(elapsed * 1000))
        runner._emit("wait_tick", {"id": call_id, "remaining_ms": remaining_ms, "total_ms": total_ms})
        runner.stop_event.wait(WAIT_TICK)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    remaining_ms = 0 if interrupted else max(0, total_ms - elapsed_ms)
    end_data: dict[str, Any] = {
        "id": call_id,
        "interrupted": interrupted,
        "elapsed_ms": elapsed_ms,
        "remaining_ms": remaining_ms,
        "total_ms": total_ms,
    }
    if job_id:
        # 为什么结束的：done=任务先结束 / timeout=时长先到 / missing=任务不在列表里
        end_data["job_status"] = job_status
        end_data["job_end_reason"] = (
            "interrupted" if interrupted
            else "missing" if not job_found
            else "done" if job_status in WAIT_JOB_DONE_STATUSES
            else "timeout"
        )
    runner._emit("wait_end", end_data)

    waited = round(elapsed_ms / 1000, 1)
    if interrupted:
        _log(f"  ⏳ 等待被停止信号打断，已等 {elapsed_ms / 1000:.1f}s")
        return {"waited_seconds": waited, "wait_interrupted": True, "note": "等待被用户停止打断"}
    if job_id and not job_found:
        _log(f"  ⏳ 任务 {job_id} 不在任务列表里，提前结束等待")
        return {
            "waited_seconds": waited,
            "job_id": job_id,
            "job_found": False,
            "note": (
                f"任务列表里找不到 {job_id}：id 可能写错，或这个任务已经不在列表里。"
                "用 get_runtime 看当前项目的任务状态再决定下一步。"
            ),
        }
    if job_id and job_status in WAIT_JOB_DONE_STATUSES:
        _log(f"  ⏳ 任务 {job_id} 已结束（{job_status}），等待提前收尾，共 {elapsed_ms / 1000:.1f}s")
        out: dict[str, Any] = {
            "waited_seconds": waited,
            "job_id": job_id,
            "job_status": job_status,
            "job_success": job_success,
            "wait_completed": True,
            "job_finished": True,
            "note": f"任务已经结束（{job_status}）——比等待时长先到，不用再等了，按流程处理结果（查进度/问题清单）。",
        }
        if job_error:
            out["job_error"] = job_error
        return out
    _log(f"  ⏳ 等待结束，共 {elapsed_ms / 1000:.1f}s")
    if job_id:
        # 时长先到：这一刻模型要的就是进度与 eta_seconds（下一步一定是 get_runtime），
        # 顺手取一份快照带上，省它一个来回。
        snapshot = _runtime_snapshot(runner)
        out: dict[str, Any] = {
            "waited_seconds": waited,
            "job_id": job_id,
            "job_status": job_status or "running",
            "job_finished": False,
            "wait_completed": True,
        }
        if snapshot is not None:
            out["runtime"] = snapshot
            out["note"] = (
                f"等待时长到了，任务 {job_id} 还在跑（{job_status or 'running'}）："
                "下面附了当前运行时快照（等同 get_runtime，含 summary.eta_seconds），"
                "据此决定下一轮等多久——eta 还长就再 wait 一次同一个 job_id，快完了就把时长调短盯着。"
            )
        else:
            out["note"] = (
                f"等待时长到了，任务 {job_id} 还在跑（{job_status or 'running'}）："
                "调用 get_runtime 看进度与 eta_seconds 再决定下一轮等多久"
                "（也可以再 wait 一次同一个 job_id）。"
            )
        return out
    return {
        "waited_seconds": waited,
        "wait_completed": True,
        "note": "这只是计时结束，不代表后台任务完成。如果是翻译任务，请调用 get_runtime 确认任务状态后再决定下一步。",
    }


def _runtime_snapshot(runner: AgentRunner) -> dict[str, Any] | None:
    """顺手取一份运行时快照（等同模型再调一次 get_runtime），取不到就返回 None。

    放在 wait 的"时长先到、任务还在跑"分支里：那一刻模型正需要进度与 eta_seconds 来决定
    下一轮等多久，直接带上就不必再多跑一个来回。副作用（错误水位线 seen_error_ids）与
    模型自己调 get_runtime 一致，所以不会造成同一条报错被重复发。
    快照只是顺手带的，取不到（后端抖了/项目读不到）不该影响 wait 本身的结论。
    """
    try:
        return _tool_get_runtime(runner, {})
    except Exception as exc:  # noqa: BLE001
        _log(f"  ⚠ wait 结束时取运行时快照失败：{exc}")
        return None


def _error_key(err: dict[str, Any]) -> str:
    """错误的去重键：后端 id + 内容指纹。

    后端 id 只精确到毫秒（同毫秒内两条同类型错误会撞成同一个 id），所以再拼上内容；
    缺 id 时就只用内容。同一条事件（id 与内容都不变）在快照窗口里待多久都只对应一个
    键——这正是"同一条 warning 不再反复报"的依据。
    """
    return "|".join(
        str(err.get(field) or "")
        for field in ("id", "kind", "filename", "index_range", "message", "ts")
    )


def _error_group_key(err: dict[str, Any]) -> tuple[str, str, str]:
    """归并键：同一类原因的报错算一组。

    按 kind + level + message 归并——真实报错（如 kind=parse 的「未解析到有效句子」）
    会横跨很多文件、很多条目反复出现，逐条发只会刷屏。message 里若嵌了本条自己的
    文件名，换成 {file} 占位，免得同一个原因被文件名拆成十几组。
    """
    kind = str(err.get("kind") or "")
    message = str(err.get("message") or "")
    filename = str(err.get("filename") or "")
    if filename and filename in message:
        message = message.replace(filename, "{file}")
    return kind, str(err.get("level") or ""), message


def _group_text(kind: str, level: str, message: str, count: int, files: list[str]) -> str:
    """一行可读的摘要：「parse 警告 × 23 次：未解析到有效句子（涉及 12 个文件…）」。"""
    label = "警告" if level == "warning" else "错误"
    text = f"{kind or '未知'} {label} × {count} 次：{message or '(无描述)'}"
    if files:
        listed = "、".join(files[:3])
        more = f" 等 {len(files)} 个" if len(files) > 3 else ""
        text += f"（涉及文件：{listed}{more}）"
    return text


def _summarize_group(events: list[dict[str, Any]]) -> dict[str, Any]:
    """把同一类的一批报错压成一条：次数 / 涉及文件 / 时间范围 / 可读摘要。"""
    first = events[0]
    kind, level, message = _error_group_key(first)
    files: list[str] = []
    for err in events:
        name = str(err.get("filename") or "")
        if name and name not in files:
            files.append(name)
    timestamps = [str(err.get("ts") or "") for err in events if err.get("ts")]
    count = len(events)
    return {
        "kind": kind,
        "level": level,
        "message": message,
        "count": count,
        "files": files[:5],
        "files_total": len(files),
        "first_ts": min(timestamps) if timestamps else "",
        "last_ts": max(timestamps) if timestamps else "",
        "text": _group_text(kind, level, message, count, files),
    }


def _group_errors(
    events: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """同类归并并按"报得多、最近报过"排序，返回（摘要, 该组的事件）。"""
    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for err in events:
        buckets.setdefault(_error_group_key(err), []).append(err)
    grouped = [(_summarize_group(group), group) for group in buckets.values()]
    grouped.sort(key=lambda item: str(item[0]["last_ts"]), reverse=True)  # 先按最近出现
    grouped.sort(key=lambda item: item[0]["count"], reverse=True)  # 次数多的在前（稳定排序）
    return grouped


def _take_fresh_errors(
    state: AgentState, errors: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    """挑出"还没发给过模型"的报错、同类归并，返回（本次要发的组, 还没发的事件数）。

    水位线（state.seen_error_ids）只保留仍在快照里的键——滚出快照的错误不会再回来，
    不必长期记着。归并后单次最多发 RUNTIME_ERRORS_PER_QUERY **组**；没轮到的组**不标记
    为已发**，留到下次查询，既不会一次刷屏，也不会把错误吞掉。
    """
    keys = [_error_key(err) for err in errors]
    live = set(keys)
    seen = {key for key in state.seen_error_ids if key in live}
    fresh_events = [err for key, err in zip(keys, errors) if key not in seen]
    grouped = _group_errors(fresh_events)
    reported = grouped[:RUNTIME_ERRORS_PER_QUERY]
    for _group, group_events in reported:  # 只把真发出去的记为已发
        for err in group_events:
            seen.add(_error_key(err))
    state.seen_error_ids = seen
    pending = sum(len(group) for _, group in grouped[RUNTIME_ERRORS_PER_QUERY:])
    return [group for group, _ in reported], pending


def _tool_get_runtime(runner: AgentRunner, _args: dict[str, Any]) -> Any:
    """运行时状态：任务状态 / 阶段 / 本轮计数 / ETA / 新出现的错误。

    只返回数据本身——口径说明（summary 是本轮任务口径、recent_errors 是增量且已同类
    合并）都写在工具的 description 里：这个工具在等待循环里被反复调用，静态说明每次
    跟着返回只是白占上下文。

    summary 是**本轮任务自己的**计数（按任务计划统计，含正在翻译、缓存还没落盘的
    文件）；「了解项目」里的 progress 是**已落盘缓存**的口径，两者分母不同，同一次
    查询下数字本来就会差一截，不需要互相校对。

    recent_errors 只给"上次查询之后新出现的"（水位线记在 state 上），避免同一条
    parse warning 在连续几次查询里反复出现、逼模型重新判断。
    """
    pid = runner._project_id()
    data = runner._http_get(f"/api/projects/{pid}/runtime")
    job = data.get("job") or {}
    summary = data.get("summary") or {}
    raw_errors = data.get("recent_errors") or []
    fresh_errors, pending_errors = _take_fresh_errors(
        runner.state, [err for err in raw_errors if isinstance(err, dict)]
    )
    result: dict[str, Any] = {
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
        "recent_errors": fresh_errors,
    }
    if pending_errors:
        # 还有没发完的新错误：说明这次报错很密集，下次查询继续给
        result["recent_errors_pending"] = pending_errors
    return result


def _change(path: str, before: Any, after: Any, kind: str = "replace") -> dict[str, Any]:
    """写入类工具的变更记录：前端据此渲染 diff 风格卡片。

    kind: add（原不存在）/ remove（删后不存在）/ replace（改值）。
    before/after 用 JSON 序列化保持类型可读；超长的文本（如整本字典内容）
    不做 diff，改由工具自己返回统计（行数增删）而非全文。"""
    return {"path": path, "before": before, "after": after, "kind": kind}


def _diff_lines(before_text: str, after_text: str, *, context: int = 0, max_lines: int = 200) -> dict[str, Any]:
    """整文本替换时的逐行 diff（新增行/删除行），给前端渲染行级 diff。

    返回 {"rows": [{"op": "add"|"del", "line": str}], "truncated": bool}——与前端
    extractChangeList 认的 line_diff 结构一致。用最长公共行序列近似（对字典、规范这类
    逐行文本足够准确）；超过 max_lines 时截断并标记 truncated，避免整本小说级 diff 刷屏。"""
    import difflib

    before_lines = before_text.splitlines()
    after_lines = after_text.splitlines()
    sm = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    rows: list[dict[str, Any]] = []
    truncated = False
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if len(rows) >= max_lines:
            truncated = True
            break
        for line in before_lines[i1:i2]:
            if len(rows) >= max_lines:
                truncated = True
                break
            rows.append({"op": "del", "line": line})
        for line in after_lines[j1:j2]:
            if len(rows) >= max_lines:
                truncated = True
                break
            rows.append({"op": "add", "line": line})
    return {"rows": rows, "truncated": truncated}


# ---- 审批卡的「将要变更」预览 ----
# 写类工具真正执行前会挂起等用户批准。那张卡以前只有一句参数摘要，要看"它准备改成什么"
# 就得先展开工具行的原始 JSON；现在在挂起之前就把 before→after 算出来摆到卡上——用户点的，
# 就是自己看到的那份 diff。
#
# **只读**：只算不写，绝不落盘（写由获批后的 handler 做）。为此每个工具都走"先算出结果、
# 再决定写"的路子：缓存/字典/人名表在只读计划函数上算，配置在副本上算，项目规范让后端
# 带 dry_run 算（见 _preview_guideline_write）。也因此预览和真执行是两次独立取数：中间
# 用户自己改了文件、两条 patch 打同一个 index 这类情况都由真执行那份计划重新算，结果里的
# changes 才是最终事实。
#
# 覆盖范围＝所有"改完有 diff 可看"的写类工具（含 high 档的改配置 / 写规范 / 改问题过滤——
# 它们同样要用户点允许，同样该看到要改什么）。不在列的都是没有可比对 diff 的：整文件删缓存
# （delete_transl_cache 不传 indexes 时没有单个文件名可对）、启动翻译、派子代理。
PREVIEW_TOOLS: frozenset[str] = frozenset({
    "patch_transl_cache",
    "delete_transl_cache",
    "save_dict",
    "save_name_table",
    "update_project_config",
    "manage_problem_filter",
    "manage_problem_white_list",
    "write_project_guideline",
})


def _preview_tool_changes(runner: AgentRunner, name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """算「这次调用将要改成什么」（只读），给审批卡提前显示 diff。

    返回的结构与工具结果里那份一致（changes / line_diff / deleted_preview），前端用同一个
    ChangeListCard 渲染。没有可预览的改动（工具不在 PREVIEW_TOOLS、入参不全、index 没命中、
    内容其实没变）时返回 None——卡上就只剩摘要，别拿一个空 diff 骗用户。
    取数失败同样返回 None：预览只是锦上添花，绝不能因此挡住审批。
    """
    if name not in PREVIEW_TOOLS:
        return None
    try:
        if name == "patch_transl_cache":
            return _preview_cache_patch(runner, args)
        if name == "delete_transl_cache":
            return _preview_cache_delete(runner, args)
        if name == "save_dict":
            return _preview_dict_write(runner, args)
        if name == "update_project_config":
            return _preview_config_update(runner, args)
        if name == "manage_problem_filter":
            return _preview_problem_filter(runner, args)
        if name == "manage_problem_white_list":
            return _preview_problem_white_list(runner, args)
        if name == "write_project_guideline":
            return _preview_guideline_write(runner, args)
        return _preview_name_table(runner, args)
    except Exception as exc:  # noqa: BLE001 - 预览拿不到数据不影响审批流程
        _log(f"  ⚠ 变更预览失败（{name}）：{exc}")
        return None


def _preview_cache_patch(runner: AgentRunner, args: dict[str, Any]) -> dict[str, Any] | None:
    """patch_transl_cache 的预览：读条目（不改），按同一份判断算出 before→after。"""
    filename = str(args.get("filename", "")).strip()
    patches_raw = args.get("patches")
    if not filename or not isinstance(patches_raw, list) or not patches_raw:
        return None
    pid = runner._project_id()
    data = runner._http_get(f"/api/projects/{pid}/cache/{urllib.parse.quote(filename)}")
    entries = data.get("entries", []) if isinstance(data, dict) else []
    if not isinstance(entries, list):
        return None
    planned = _plan_cache_patches(entries, patches_raw, _PATCHABLE_FIELDS)
    changes = planned["changes"]
    if not changes:
        return None
    out: dict[str, Any] = {"filename": filename, "changes": changes}
    if planned["not_found"]:
        out["not_found_indexes"] = sorted(planned["not_found"])
    return out


def _preview_cache_delete(runner: AgentRunner, args: dict[str, Any]) -> dict[str, Any] | None:
    """delete_transl_cache 的预览（只覆盖"按 index 删条目"；整文件删除没有 diff 可看）。"""
    filename = str(args.get("filename", "")).strip()
    index_spec = str(args.get("indexes", "") or "").strip()
    if not filename or not index_spec:
        return None
    wanted = _parse_index_spec(index_spec)
    if not wanted:
        return None
    pid = runner._project_id()
    data = runner._http_get(f"/api/projects/{pid}/cache/{urllib.parse.quote(filename)}")
    entries = data.get("entries", []) if isinstance(data, dict) else []
    if not isinstance(entries, list):
        return None
    _, deleted_indexes, previews = _plan_cache_delete(entries, wanted)
    if not deleted_indexes:
        return None
    return {
        "filename": filename,
        "deleted_indexes": deleted_indexes,
        "deleted_preview": previews[:50],
    }


def _preview_dict_write(runner: AgentRunner, args: dict[str, Any]) -> dict[str, Any] | None:
    """save_dict 的预览：读旧内容 + 按同一份合并规则算新内容，做行级 diff。"""
    file_key = str(args.get("file_key", "")).strip()
    if not file_key:
        return None
    action = str(args.get("action", "") or "overwrite").strip().lower() or "overwrite"
    if action not in ("overwrite", "replace", "append", "delete"):
        return None
    content = str(args.get("content", ""))
    if action == "delete" and not _split_dict_incoming(content):
        return None
    pid = runner._project_id()
    cfg = urllib.parse.quote(runner.state.config_file_name)
    data = runner._http_get(f"/api/projects/{pid}/dictionary/project?config={cfg}")
    old = (data.get("dict_contents", {}) if isinstance(data, dict) else {}).get(file_key)
    before_lines = [str(x) for x in old.get("lines", [])] if isinstance(old, dict) else []
    new_lines, _ = _dict_new_lines(before_lines, content, action)
    before_text = "\n".join(before_lines)
    new_text = "\n".join(new_lines)
    if new_text == before_text:
        return None
    return {
        "file_key": file_key,
        "action": action,
        "line_diff": _diff_lines(before_text, new_text),
    }


def _preview_config_update(runner: AgentRunner, args: dict[str, Any]) -> dict[str, Any] | None:
    """update_project_config 的预览：读配置 + 在副本上算一遍改完的结果（一行都不写回）。"""
    updates = args.get("updates")
    if not isinstance(updates, list) or not updates:
        return None
    pid = runner._project_id()
    config_name = runner.state.config_file_name or DEFAULT_CONFIG_FILE
    data = runner._http_get(f"/api/projects/{pid}/config?config={urllib.parse.quote(config_name)}")
    config = data.get("config") if isinstance(data, dict) else None
    if not isinstance(config, dict):
        return None
    _, _, changes = _plan_config_updates(config, updates)
    if not changes:
        return None
    return {"changes": changes}


def _preview_problem_filter(runner: AgentRunner, args: dict[str, Any]) -> dict[str, Any] | None:
    """manage_problem_filter 的预览：读现在的清单，算出这次会加/删哪几个关键字。

    只覆盖 add / remove：list 不改任何东西，没有 diff 可看。
    """
    action = str(args.get("action", "")).strip()
    if action not in ("add", "remove"):
        return None
    keywords = _parse_filter_keywords(args.get("keyword"))
    if not keywords:
        return None
    if action == "add" and any(not _is_valid_regex(k) for k in keywords):
        return None  # 真执行会因正则不合法报错，卡上不必先画一份不会发生的变更
    pid = runner._project_id()
    config_name = runner.state.config_file_name or DEFAULT_CONFIG_FILE
    _, keys = _load_problem_filter_keys(runner, pid, config_name)
    _, _, changes = _plan_problem_filter(keys, action, keywords)
    if not changes:
        return None  # 全都在清单里（或本来就不在）：这次调用不会改变什么
    return {"changes": changes}


def _preview_problem_white_list(runner: AgentRunner, args: dict[str, Any]) -> dict[str, Any] | None:
    """manage_problem_white_list 的预览：读现在的白名单，算出这次会加/删哪几条。

    只覆盖 add / remove：list 不改任何东西，没有 diff 可看。
    """
    action = str(args.get("action", "")).strip()
    if action not in ("add", "remove"):
        return None
    entries = _parse_filter_keywords(args.get("entry"))
    if not entries:
        return None
    pid = runner._project_id()
    config_name = runner.state.config_file_name or DEFAULT_CONFIG_FILE
    _, current = _load_problem_white_list(runner, pid, config_name)
    _, _, changes = _plan_problem_filter(current, action, entries, field="problemWhiteList")
    if not changes:
        return None  # 全都在白名单里（或本来就不在）：这次调用不会改变什么
    return {"changes": changes}


def _preview_guideline_write(runner: AgentRunner, args: dict[str, Any]) -> dict[str, Any] | None:
    """write_project_guideline 的预览：让后端按 dry_run 算一遍"写完会是什么样"，再和现在比。

    三种 mode 的拼接规则（overwrite / append 的空行与结尾换行 / replace 的命中唯一性）只在
    ProjectGuideline.apply_project_guideline_edit 那一份实现里——在 Agent 侧重算一遍就是
    第二份，早晚对不上。所以预览也走那个入口，只是带 dry_run（服务端算完就走，不落盘）：
    连 replace 没命中、超长这类校验结果都跟真执行一致。
    """
    mode = str(args.get("mode", "") or "").strip()
    if mode not in ("overwrite", "append", "replace"):
        return None
    endpoint = f"/api/projects/{runner._project_id()}/guideline"
    before = str((runner._http_get(endpoint) or {}).get("content") or "")
    preview = runner._http_put(endpoint, {
        "mode": mode,
        "content": str(args.get("content", "") or ""),
        "old_text": str(args.get("old_text", "") or ""),
        "new_text": str(args.get("new_text", "") or ""),
        "dry_run": True,
    })
    after = str((preview or {}).get("content") or "") if isinstance(preview, dict) else ""
    if not after or after == before:
        return None  # 内容没变（如 append 一段已有的文字）：没有 diff 可显示
    return {"changed": True, "line_diff": _diff_lines(before, after)}


def _preview_name_table(runner: AgentRunner, args: dict[str, Any]) -> dict[str, Any] | None:
    """save_name_table 的预览：拿旧表按 src_name 比出新增/移除/改译名。

    整表覆写的写法下"改了哪几个名字"只能比对才看得出来（见 _name_table_changes）；
    原样回传同一张表时不产生任何 changes，卡上就不显示这块。
    """
    names = args.get("names", [])
    if not isinstance(names, list):
        return None
    pid = runner._project_id()
    old = runner._http_get(f"/api/projects/{pid}/name-table")
    _, _, changes = _name_table_changes(old.get("names", []), names)
    if not changes:
        return None
    return {"changes": changes}


def _split_problem_types(problem: str) -> list[str]:
    """问题文本按英文逗号拆项，取每项「类型：详情」的类型前缀去重。

    与桌面端「缓存与问题」统计 tab 的归类口径一致（problemFilter.ts）。"""
    types: list[str] = []
    for part in str(problem or "").split(","):
        token = part.strip()
        if not token:
            continue
        type_name = token.split("：", 1)[0].strip()
        if type_name and type_name not in types:
            types.append(type_name)
    return types


# ---- Markdown 表格输出（当前启用）----
# 可表格化的部分（数据行）用 Markdown 表格：第一行列名、第二行分隔线、每行一条——模型
# 最熟的形状；计数 / 提示 / 警告这些杂项不用硬塞进表格，按文字写在表格前后。
# 单元格规则只有一条：竖线转义、换行折成 <br>（Markdown 表格单元格不能有真换行），
# None/缺失就是空单元格——不再需要 ION 那套 ~ 与位置对齐。
def _md_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    s = str(value)
    return (
        s.replace("|", "\\|")
        .replace("\r\n", "<br>")
        .replace("\n", "<br>")
        .replace("\r", "<br>")
    )


def _md_table(columns: list[str], rows: Any) -> str:
    """Markdown 表格；没有数据行就返回空串（调用方拼文档时自动略过）。"""
    if not isinstance(rows, list) or not rows:
        return ""
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join(["---"] * len(columns)) + " |",
    ]
    for row in rows:
        row = row if isinstance(row, dict) else {}
        lines.append("| " + " | ".join(_md_cell(row.get(col)) for col in columns) + " |")
    return "\n".join(lines)


def _md_doc(*parts: str) -> str:
    return "\n\n".join(part for part in parts if part)


def _md_render_list_transl_cache(result: dict[str, Any]) -> str:
    head_parts: list[str] = []
    if result.get("count") is not None:
        head_parts.append(f"共 {result['count']} 个缓存文件")
    if result.get("sampled"):
        head_parts.append(f"下面只列其中 {result.get('returned')} 个（均匀采样，不是前几名）")
    table = _md_table(["name", "size", "entries"], result.get("cache_files"))
    note = result.get("note")
    return _md_doc("，".join(head_parts), table, f"备注：{note}" if note else "")


def _md_render_list_input_files(result: dict[str, Any]) -> str:
    head_parts: list[str] = []
    if result.get("count") is not None:
        head_parts.append(f"共 {result['count']} 个输入文件")
    if result.get("sampled"):
        head_parts.append(f"下面只列其中 {result.get('returned')} 个（均匀采样，不是前几名）")
    if result.get("sentences_total") is not None:
        head_parts.append(f"句数合计 {result['sentences_total']}（含没列出来的文件）")
    table = _md_table(["name", "size", "sentences"], result.get("input_files"))
    note = result.get("note")
    return _md_doc("，".join(head_parts), table, f"备注：{note}" if note else "")


def _md_render_list_problems(result: dict[str, Any]) -> str:
    note = result.get("note")
    note_line = f"备注：{note}" if note else ""
    if result.get("mode") == "stats":
        head = f"共 {result.get('total')} 个问题，类型统计如下"
        table = _md_table(["type", "count"], result.get("types"))
        hint = result.get("hint")
        return _md_doc(head, table, f"提示：{hint}" if hint else "", note_line)

    head_parts: list[str] = []
    if result.get("problem_type") is not None:
        head_parts.append(f"问题类型：{result['problem_type']}")
    if result.get("matched") is not None:
        head_parts.append(f"命中 {result['matched']} 条")
    if result.get("has_more"):
        head_parts.append(f"本页显示 {result.get('returned')} 条、还有更多（用 offset 翻页）")
    else:
        head_parts.append(f"本页显示 {result.get('returned')} 条")
    if result.get("majority_trans_by"):
        head_parts.append(
            f"多数派模型 {result['majority_trans_by']}（表里已省略，只留少数派/改过的来源）"
        )
    context_phrase = _context_phrase(result, "每条问题")
    if context_phrase:
        head_parts.append(context_phrase)
    table = _md_table(
        ["filename", "index", "speaker", "post_src", "pre_dst", "problem", "trans_by"],
        result.get("problems"),
    )
    return _md_doc("；".join(head_parts), table, note_line)


def _md_render_read_input_file(result: dict[str, Any]) -> str:
    head_parts: list[str] = []
    if result.get("filename") is not None:
        head_parts.append(f"文件 {result['filename']}")
    if result.get("count") is not None:
        head_parts.append(f"共 {result['count']} 条，显示 {result.get('returned')} 条")
    missing = result.get("missing_indexes")
    missing_text = (
        "缺失 index：" + ",".join(str(i) for i in missing) if isinstance(missing, list) and missing else ""
    )
    table = _md_table(["index", "name", "pre_src"], result.get("entries"))
    return _md_doc("，".join(head_parts), missing_text, table)


def _md_render_read_transl_cache(result: dict[str, Any]) -> str:
    head_parts: list[str] = []
    if result.get("filename") is not None:
        head_parts.append(f"文件 {result['filename']}")
    if result.get("grep_note"):
        head_parts.append(str(result["grep_note"]))
    if result.get("count") is not None:
        head_parts.append(f"共 {result['count']} 条，显示 {result.get('returned')} 条")
    context_phrase = _context_phrase(result, "点名条目")
    if context_phrase:
        head_parts.append(context_phrase)
    if result.get("majority_trans_by"):
        head_parts.append(
            f"多数派模型 {result['majority_trans_by']}（表里已省略，只留少数派/改过的来源）"
        )
    fields_note = result.get("fields_note")
    missing = result.get("missing_indexes")
    missing_text = (
        "缺失 index：" + ",".join(str(i) for i in missing) if isinstance(missing, list) and missing else ""
    )
    fields = [str(f) for f in result.get("fields") or []]
    if not fields:
        entries = result.get("entries")
        if isinstance(entries, list) and entries and isinstance(entries[0], dict):
            fields = list(entries[0].keys())
    table = _md_table(fields, result.get("entries"))
    notes = [f"字段说明：{fields_note}"] if fields_note else []
    return _md_doc("，".join(head_parts), missing_text, *notes, table)


# 搜索类工具（/cache/search 与 /input/search）的返回结构是同一套：results + total，
# 带 context 时多 returned_hits / returned。表头那些文字（命中数、上下文、命中分布、多数派、
# 解析失败的文件）只有一处口径；行本身各出各的表——两侧的列不一样。
_SEARCH_CACHE_COLUMNS = ["filename", "index", "speaker", "post_src", "pre_dst", "problem", "trans_by"]
_SEARCH_INPUT_COLUMNS = ["filename", "index", "speaker", "src"]


def _md_search_head(result: dict[str, Any]) -> str:
    parts: list[str] = []
    total = result.get("total")
    if total is not None:
        parts.append(f"共 {total} 条命中")
    context_phrase = _context_phrase(result, "每条命中")
    if context_phrase:
        parts.append(context_phrase)
    # 分页口径与 list_problems 一致：returned 是本页命中数，returned_rows 才含前后文行
    shown = result.get("returned")
    if isinstance(shown, int):
        page = f"本页 {shown} 条命中"
        rows = result.get("returned_rows")
        if isinstance(rows, int):
            page += f"、含前后文共 {rows} 行"
        if result.get("offset"):
            page += f"（offset={result['offset']}）"
        if result.get("has_more"):
            page += "、还有更多（用 offset 翻页）"
        parts.append(page)
    matched_in = result.get("matched_in")
    if isinstance(matched_in, dict) and matched_in:
        parts.append("命中分布：" + "、".join(f"{key} {value}" for key, value in matched_in.items()))
    if result.get("majority_trans_by"):
        parts.append(f"多数派模型 {result['majority_trans_by']}（表里已省略，只留少数派/改过的来源）")
    failed = result.get("files_failed")
    if isinstance(failed, list) and failed:
        parts.append("这些输入文件解析失败、没参与搜索：" + "、".join(str(name) for name in failed))
    return "；".join(parts)


def _md_render_search(result: dict[str, Any], columns: list[str]) -> str | None:
    """搜索类工具共用的渲染：表头文字 + 命中行表格 + 备注。

    一行都没有（0 命中这类）时表头文字还在，不会渲染出一段空内容把结果弄丢。
    """
    note = result.get("note")
    return _md_doc(
        _md_search_head(result),
        _md_table(columns, result.get("results")),
        f"备注：{note}" if note else "",
    ) or None


def _md_render_search_transl_cache(result: dict[str, Any]) -> str | None:
    """在缓存里搜（原文/译文/问题）：命中行与 read_transl_cache 是同一批列。"""
    return _md_render_search(result, _SEARCH_CACHE_COLUMNS)


def _md_render_search_input(result: dict[str, Any]) -> str | None:
    """在待翻译原文里搜：原文侧只有正文与说话人，另加定位用的 filename / index。"""
    return _md_render_search(result, _SEARCH_INPUT_COLUMNS)


def _md_render_manage_problem_filter(result: dict[str, Any]) -> str | None:
    """list 的过滤清单渲染成表：每条过滤项当前挡住了多少条问题。

    add/remove 的结果是变更 diff（changes），没有可表格化的清单——返回 None 让它照旧走 JSON。
    """
    filters = result.get("filters")
    if not isinstance(filters, list) or not filters:
        return None
    parts = [
        f"共 {result.get('count')} 条过滤项",
        _md_table(["key", "problems"], filters),
    ]
    entries = result.get("problem_entries")
    visible = result.get("visible_entries")
    if isinstance(entries, int) and isinstance(visible, int):
        parts.append(
            f"当前共 {entries} 条问题，其中 {entries - visible} 条被过滤项挡住，"
            f"list_problems 可见 {visible} 条"
        )
    if any(isinstance(f, dict) and not f.get("problems") for f in filters):
        parts.append("problems 为 0 的过滤项当前一条也挡不到，可考虑 remove")
    return _md_doc(*parts)


# 工具名 → 渲染器。渲染只对这里列出的工具生效，其余工具维持 JSON。
_MD_RENDERERS: dict[str, Any] = {
    "list_transl_cache": _md_render_list_transl_cache,
    "list_input_files": _md_render_list_input_files,
    "list_problems": _md_render_list_problems,
    "read_input_file": _md_render_read_input_file,
    "read_transl_cache": _md_render_read_transl_cache,
    "search_transl_cache": _md_render_search_transl_cache,
    "search_input": _md_render_search_input,
    "manage_problem_filter": _md_render_manage_problem_filter,
}


def _render_tool_result_table(name: str, result: Any) -> str | None:
    """把大清单工具的结果 dict 渲染成 Markdown（表格化数据 + 文字化杂项）。

    不在名单 / 不是 dict / 渲染失败时返回 None——调用方退回 JSON，绝不能因为格式丢数据。
    """
    render = _MD_RENDERERS.get(name)
    if render is None or not isinstance(result, dict):
        return None
    try:
        return render(result)
    except Exception as exc:  # noqa: BLE001
        _log(f"  ⚠ Markdown 渲染失败（{name}），退回 JSON：{exc}")
        return None


def _only_preceding_arg(args: dict[str, Any]) -> bool:
    """only_preceding：带上下文时是否只给上文（**默认 true**，省 token）。

    看「这句话为什么这么翻」通常只需要上文——后文对判断当前这句帮助有限，却要成倍占
    token。模型偶尔把布尔写成字符串（"false"），这里一并认；空串/缺失都按默认 true。
    """
    raw = args.get("only_preceding", True)
    if raw is None:
        return True
    if isinstance(raw, str):
        token = raw.strip().lower()
        if token == "":
            return True
        return token not in ("false", "0", "no", "off")
    return bool(raw)


def _mark_context_row(row: dict[str, Any]) -> dict[str, Any]:
    """把上下文行的 index 标成 "12*"（命中行/问题行保持原样）。

    index 是模型在各工具之间对齐条目的唯一把手，所以"这行是搭着给的上文"就标在它上面：
    带 * 的行不是本页要找的条目，别拿它去 patch、也别当命中数。
    """
    idx = row.get("index")
    if idx is None:
        return row
    return {**row, "index": f"{idx}*"}


def _context_phrase(result: dict[str, Any], subject: str = "条目") -> str:
    """带 context 的工具共用的表头一句话（只给上文 / 两边都给 + 星标的含义）。

    subject 是这个工具"要找的东西"怎么称呼（条目 / 问题 / 命中），三处口径保持一致。
    """
    n = result.get("context")
    if not n:
        return ""
    scope = f"前面 {n} 句" if result.get("only_preceding") else f"前后各 {n} 句"
    kind = "上文" if result.get("only_preceding") else "上下文"
    return f"含{kind}（{subject}{scope}；index 带 * 的是上下文行，不是要找的条目）"


def _merge_problem_context(
    runner: AgentRunner, page: list[dict[str, Any]], context: int, only_preceding: bool = True
) -> list[dict[str, Any]]:
    """给问题行并上文（list_problems 的 context，与 read_transl_cache 同一语义）。

    问题行自己往往看不出"为什么有问题"——修「残留日文」「译名不一致」要看着上文才敢动手，
    逐条 read_transl_cache 又太碎，所以把上文直接并进这张表。上下文行的 index 带 *（见
    _mark_context_row）：一眼分得开哪些是本页的问题行、哪些只是搭着给的上文。
    - 同一文件里相邻问题的窗口**合并**（重叠的上下文行只给一份）；
    - only_preceding（默认 true）只给上文；传 false 才前后各 N 句；
    - 上下文按每个文件取一次缓存，取不到（文件被删/翻写中）只少带一块：该文件的问题行照给，
      绝不因此把整次列表弄失败。
    """
    merged: list[dict[str, Any]] = []
    # 按文件分组（保持首次出现顺序）：跨文件的问题行不能混进同一个窗口
    order: list[str] = []
    by_file: dict[str, list[dict[str, Any]]] = {}
    for row in page:
        fname = str(row.get("filename") or "")
        if fname not in by_file:
            by_file[fname] = []
            order.append(fname)
        by_file[fname].append(row)
    pid = runner._project_id()
    for fname in order:
        rows = by_file[fname]
        try:
            data = runner._http_get(f"/api/projects/{pid}/cache/{urllib.parse.quote(fname)}")
            entries = [e for e in (data.get("entries") or []) if isinstance(e, dict)]
        except Exception:  # noqa: BLE001 - 上下文只是附加信息，取不到就只给问题行
            entries = []
        if not entries:
            merged.extend(rows)
            continue
        problem_by_index: dict[int, dict[str, Any]] = {}
        for r in rows:
            raw_idx = r.get("index")
            if raw_idx is None:
                continue
            try:
                problem_by_index[int(raw_idx)] = r
            except (TypeError, ValueError):
                continue
        spans: set[int] = set()
        after = 0 if only_preceding else context
        for idx in problem_by_index:
            spans.update(range(idx - context, idx + after + 1))
        file_rows: list[dict[str, Any]] = []
        emitted: set[int] = set()
        for e in entries:
            raw_idx = e.get("index")
            if raw_idx is None:
                continue
            try:
                idx = int(raw_idx)
            except (TypeError, ValueError):
                continue
            if idx in problem_by_index:
                file_rows.append(problem_by_index[idx])
                emitted.add(idx)
            elif idx in spans:
                # 上下文行只要「谁说的 + 原文 + 译文」，problem 列空着；index 带 * 标明它不是本页的
                # 问题行（问题行另有判据：它在 problem_by_index 里）
                file_rows.append(_mark_context_row({
                    "filename": fname,
                    "index": idx,
                    "speaker": str(_cache_field_value(e, "name") or ""),
                    "post_src": str(_cache_field_value(e, "post_src") or ""),
                    "pre_dst": str(_cache_field_value(e, "pre_dst") or ""),
                }))
        # 缓存里找不到的问题行（缓存与问题清单不同步）：原样保留，别把问题弄丢
        for idx, row in problem_by_index.items():
            if idx not in emitted:
                file_rows.append(row)

        def _row_sort_index(row: dict[str, Any]) -> int:
            idx = row.get("index")
            if isinstance(idx, int):
                return idx
            # 上下文行的 index 是 "12*"（见 _mark_context_row）：排序还按数字来
            try:
                return int(str(idx).rstrip("*"))
            except (TypeError, ValueError):
                return 0

        file_rows.sort(key=_row_sort_index)
        merged.extend(file_rows)
    return merged


def _tool_list_problems(
    runner: AgentRunner, args: dict[str, Any], allowed_files: Sequence[str] | None = None
) -> Any:
    """查问题清单。

    allowed_files（校对子代理按派活锁定）：只列这几份文件的问题——统计、命中数、分页与
    context 取的上文一并收窄，免得别人文件的问题混进来带偏"我这份还剩什么"。
    """
    pid = runner._project_id()
    cfg = urllib.parse.quote(runner.state.config_file_name)
    data = runner._http_get(f"/api/projects/{pid}/problems?config={cfg}")
    problems = data.get("problems", [])
    total = data.get("total", len(problems))
    scope_note = ""
    if allowed_files is not None:
        allowed = tuple(str(name).strip() for name in allowed_files if str(name).strip())
        allowed_set = set(allowed)
        problems = [p for p in problems if str(p.get("filename") or "") in allowed_set]
        total = len(problems)
        if len(allowed) == 1:
            scope_note = f"本次只派你看「{allowed[0]}」这一个文件，这里只列它的问题。"
        else:
            scope_note = f"本次只派你看这 {len(allowed)} 个文件，这里只列它们的问题。"

    # 不带 problem_type：先给类型统计（大项目问题上千条，全量列出没有意义），
    # Agent 据此决定看哪一类。
    problem_type = str(args.get("problem_type", "") or "").strip()
    if not problem_type:
        stats: dict[str, int] = {}
        for p in problems:
            for t in _split_problem_types(p.get("problem", "")):
                stats[t] = stats.get(t, 0) + 1
        ranked = sorted(stats.items(), key=lambda kv: -kv[1])
        out: dict[str, Any] = {
            "total": total,
            "mode": "stats",
            "types": [{"type": t, "count": c} for t, c in ranked],
            "hint": "默认只返回类型统计。用 problem_type 指定类型查看具体条目（配合 limit/offset 分页），problem_type 传 \"*\" 列出全部类型的具体条目。",
        }
        if scope_note:
            out["note"] = scope_note
        return out

    # 指定类型：过滤出问题里含该类型的条目（子串匹配，与统计口径对齐）
    if problem_type != "*":
        wanted = [t.strip() for t in problem_type.split(",") if t.strip()]
        problems = [p for p in problems if any(w in _split_problem_types(p.get("problem", "")) for w in wanted)]

    limit = args.get("limit", 10)
    offset = args.get("offset", 0)
    try:
        limit = max(1, min(int(limit), 20))
    except (TypeError, ValueError):
        limit = 10
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0
    matched = len(problems)
    page = problems[offset : offset + limit]
    # trans_by 与 read_transl_cache / search_transl_cache 同一套（见 _dominant_trans_by）：
    # 这批里出现最多的那个模型（多数派，通常就是翻译引擎翻的）逐条删掉、记在顶层一次，
    # 少数派（Agent 改过的、手工改的）逐条保留——列表里真正要看的是异常来源。
    dominant = _dominant_trans_by(page)
    for row in page:
        _strip_dominant_trans_by(row, dominant)
    # context=N：每条问题并上 N 句上文（默认；only_preceding=false 才前后都并，语义同
    # read_transl_cache 的 context）。上限比 read/search 的 20 收得更紧：这里一行就是
    # "问题行 + 上下文行"，一页最多 20 条问题，N=5 时返回体就已经不小了。
    raw_context = args.get("context", 0)
    try:
        context = max(0, min(int(raw_context), 5))
    except (TypeError, ValueError):
        raise AgentToolError(f"context 必须是 0-5 的整数（收到 {raw_context!r}）")
    result = {
        "total": total,
        "matched": matched,
        "problem_type": problem_type,
        "offset": offset,
        "returned": len(page),
        "has_more": offset + limit < matched,
        "problems": page,
    }
    if scope_note:
        result["note"] = scope_note
    if context > 0:
        only_preceding = _only_preceding_arg(args)
        result["context"] = context
        result["only_preceding"] = only_preceding
        if page:
            result["problems"] = _merge_problem_context(runner, page, context, only_preceding)
    if dominant:
        result["majority_trans_by"] = dominant
    return result


def _tool_list_transl_cache(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """列出缓存文件（译文）。带每个文件的条目数。

    只列 .json 快照：翻译过程中并行写的增量日志不是可读的缓存，直接不出现在清单里——
    模型看不到它，也就不会去读它。

    清单支持 grep（文件名子串）、limit（默认 100）与 order（怎么挑这 100 个：均匀采样 /
    文件名顺序 / 随机采样 / 按大小从大到小 / 从小到大，见 LIST_ORDER_MODES）：缓存文件是按
    名字排的，成百上千个文件时"只看前 100 个"会把后半段整个藏起来（默认的 even 就是为这个）。
    """
    limit = _list_limit(args)
    grep = _list_grep(args)
    order = _list_order(args)
    pid = runner._project_id()
    data = runner._http_get(f"/api/projects/{pid}/cache")
    files = []
    for f in data.get("files", []):
        name = str(f.get("name", ""))
        if not name or name.endswith(_APPEND_CACHE_SUFFIX):
            continue  # 增量日志不是可读的缓存：不进清单
        entry: dict[str, Any] = {"name": name, "size": f.get("size", 0)}
        # 后端 _list_dir_entries 只对 .json 统计 entry_count（按 list 长度，含 0）；
        # 没有该字段时干脆不返回 entries，而不是填 0——否则会被误读成「空缓存」。
        entry_count = f.get("entry_count")
        if isinstance(entry_count, int):
            entry["entries"] = entry_count
        files.append(entry)

    matched = _grep_items(files, grep)
    shown = _select_list_items(matched, limit, order)
    result: dict[str, Any] = {
        "cache_files": shown,
        "count": len(matched),
        "returned": len(shown),
        "sampled": len(shown) < len(matched),
    }
    notes = _list_notes(
        matched=len(matched), grep=grep, returned=len(shown), limit=limit, order=order, unit="缓存文件"
    )
    if notes:
        result["note"] = "；".join(notes)
    return result


# 缓存条目可选的字段（名字即返回体里的键），与缓存 JSON 的字段一致。
CACHE_ENTRY_FIELDS: tuple[str, ...] = (
    "index",
    "name",
    "pre_src",
    "post_src",
    "pre_dst",
    "post_dst_preview",
    "proofread_dst",
    "proofread_by",
    "proofread_comment",
    "trans_by",
    "problem",
)
# 默认精简集：一条只回「谁说的 + 原文 + 译文 + 问题」。
# 原文只给一列——post_src（真正送去翻译的那版），不再同时带 pre_src：两列在多数条目上
# 只差对话符号/译前字典替换，一次读几十条就是双份原文，白占上下文（要看 pre_src 传 fields）。
# trans_by 同理默认不给（它是"哪个模型翻的"，读译文时基本用不上）。
CACHE_ENTRY_FIELDS_DEFAULT: tuple[str, ...] = (
    "index",
    "name",
    "post_src",
    "pre_dst",
    "problem",
    # 校对子代理的产物（校对批注）：主 Agent 复核时要看它，默认就得带上；没写过则是空值，
    # 走默认精简集的"空值省略"，不会给普通条目添噪音
    "proofread_comment",
)
# 每个字段的含义（拼进 system prompt，见 _cache_fields_section）。
# 命名来源见 GalTransl/CSentense.py：pre_src=前原、post_src=前润（送去翻译的原文）、
# pre_dst=后原（模型原始译文）、post_dst=后润（最终译文）。
CACHE_ENTRY_FIELD_DESCRIPTIONS: dict[str, str] = {
    "index": "条目序号（1 起、按文件顺序）；改译文/删条目/读上下文都用它定位",
    "name": "说话人；旁白为空",
    "pre_src": "原始原文（前原），管道最开始的句子；默认不返回（要看它传 fields）",
    "post_src": "真正送去翻译的原文（前润）：对话符号处理 + 译前字典替换之后的文本",
    "pre_dst": "模型返回的译文（后原），未经译后字典替换",
    "post_dst_preview": "最终译文的缓存快照（后润）：译后字典替换 + 对话符号恢复之后的形态；默认只在它与译文实质不同（不只差首尾对话符号）时返回",
    "proofread_dst": "校对/润色稿；有内容时它就是这条的最终译文（优先于 pre_dst）",
    "proofread_by": "校对者标记（校对失败的会带 Fail）；未校对为空",
    "proofread_comment": "校对批注：校对子代理（run_subagents）看过后在条目上留下的批注——校对建议（错译/漏译/事实错误等）或润色建议（翻译腔、口语不自然等表达改进），一条一句；没写过的条目为空。要处理这条就按批注改 pre_dst，改完用 patch_transl_cache 把 proofread_comment 清空表示已处理",
    "trans_by": "译者标记：翻译引擎的模型名，或被别的来源改过时的那个名字（本会话 Agent 用 patch_transl_cache 改过的条目记的是 Agent 的模型名）；读缓存时逐条只报少数派——这批里出现最多的那个（多数派，通常就是引擎翻的）与空值都不逐条给，多数派记在顶层 majority_trans_by；默认不返回（要看它传 fields）",
    "problem": "自动问题分析写入的问题标签，可能多条（以「, 」分隔）；list_problems 的统计与下钻都基于它",
}
# 默认模式下"有内容才带上"的附加字段（空值一律省略）
CACHE_ENTRY_FIELDS_IF_PRESENT: tuple[str, ...] = (
    "proofread_dst",
    "proofread_by",
)


# 缓存 JSON 的旧键名（与 GalTransl/Cache.py 的 _CACHE_KEY_COMPAT 一致）：老项目里存的
# 是 pre_jp/post_jp/pre_zh 那一套，读取时按新名取不到，得回退到旧名。
_CACHE_ENTRY_OLD_KEYS: dict[str, str] = {
    "pre_src": "pre_jp",
    "post_src": "post_jp",
    "pre_dst": "pre_zh",
    "proofread_dst": "proofread_zh",
    "post_dst_preview": "post_zh_preview",
    # 校对批注的旧名（只读兼容旧缓存；写回一律用新名，见 GalTransl/Cache.py 的 _CACHE_KEY_COMPAT）
    "proofread_comment": "doub_content",
}


def _cache_field_value(entry: dict[str, Any], name: str) -> Any:
    """按字段名取缓存条目的值，兼容旧缓存的旧键名（取不到返回 None）。"""
    if name in entry:
        return entry[name]
    old_key = _CACHE_ENTRY_OLD_KEYS.get(name)
    return entry.get(old_key) if old_key else None


# 译文首尾成对出现的对话符号：翻译前由 CSentense.analyse_dialogue 摘掉、译后再由
# recover_dialogue_symbol 补回来（见 GalTransl/CSentense.py）。所以 post_dst_preview
# 与译文几乎总是差这么一对括号——那不算"译后处理改了内容"，别因此把它带出来。
_DIALOGUE_BRACKETS = "「『」』"


def _same_ignoring_dialogue_brackets(left: Any, right: Any) -> bool:
    """两段译文是否只差首尾的对话符号（与首尾空白）。"""
    def norm(text: Any) -> str:
        return str(text or "").strip().strip(_DIALOGUE_BRACKETS).strip()

    return norm(left) == norm(right)


def _normalize_cache_fields(args: dict[str, Any]) -> list[str] | None:
    """解析 fields：None = 用默认精简集；给了就校验去重（"*"/"all" 表示全字段）。"""
    raw = args.get("fields")
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        raise AgentToolError("fields 必须是非空数组（不传表示用默认精简字段）")
    wanted: list[str] = []
    for item in raw:
        name = str(item or "").strip()
        if not name:
            continue
        if name in ("*", "all"):
            return list(CACHE_ENTRY_FIELDS)
        if name not in CACHE_ENTRY_FIELDS:
            raise AgentToolError(
                f"fields 里有未知字段：{name}（可选：{'、'.join(CACHE_ENTRY_FIELDS)}）"
            )
        if name not in wanted:
            wanted.append(name)
    if not wanted:
        raise AgentToolError(f"fields 里没有有效字段（可选：{'、'.join(CACHE_ENTRY_FIELDS)}）")
    return wanted


def _project_cache_entries(
    entries: list[dict[str, Any]], fields: list[str] | None
) -> list[dict[str, Any]]:
    """按 fields 裁条目；fields=None 时用默认精简集并省略空值。

    index 永远带上（定位/后续 patch 都靠它），即使调用方没写。
    取值走 _cache_field_value：老缓存里是 pre_jp/zh 那套旧键名也能读到。
    """
    out: list[dict[str, Any]] = []
    for entry in entries:
        if fields is None:
            item: dict[str, Any] = {}
            for key in CACHE_ENTRY_FIELDS_DEFAULT:
                value = _cache_field_value(entry, key)
                if key != "index" and value in (None, "", 0):
                    continue
                item[key] = value
            # 译后处理真的改了内容才有意义（译后字典替换、引号矫正…）。不能只比字符串：
            # 译后处理会把首尾「」补回来，几乎所有对话条目的预览都会"不同"。跟最终译文
            # （有校对稿就是校对稿，与看译文时 proofread_dst ＞ pre_dst 的口径一致）比，
            # 只差对话符号就不占位置。
            post_dst = _cache_field_value(entry, "post_dst_preview")
            base_dst = _cache_field_value(entry, "proofread_dst") or _cache_field_value(entry, "pre_dst")
            if post_dst and not _same_ignoring_dialogue_brackets(post_dst, base_dst):
                item["post_dst_preview"] = post_dst
            for key in CACHE_ENTRY_FIELDS_IF_PRESENT:
                value = _cache_field_value(entry, key)
                if value:
                    item[key] = value
            out.append(item)
            continue
        keys = ["index", *[key for key in fields if key != "index"]]
        picked: dict[str, Any] = {}
        for key in keys:
            value = _cache_field_value(entry, key)
            if value is not None:
                picked[key] = value
        out.append(picked)
    return out


def _grep_field_text(entry: dict[str, Any], name: str) -> str:
    """取字段的可搜索文本（认老缓存的旧键名；name 这类列表拼成一段）。"""
    value = _cache_field_value(entry, name)
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value if v is not None)
    return str(value)


def _grep_field_present(entry: dict[str, Any], name: str) -> bool:
    """字段是否有内容：字符串非空白；列表/字典非空；数字等非 None 即算。"""
    value = _cache_field_value(entry, name)
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) > 0
    return True


def _grep_cache_entries(
    entries: list[dict[str, Any]], grep: Any, fields: list[str] | None
) -> list[dict[str, Any]]:
    """read_transl_cache 的 grep 过滤（只读）。

    - grep 是字符串：在 fields（不传则默认精简集）的字段内容里做大小写不敏感的子串搜索，
      命中任一字段的条目保留；
    - grep 是字符串数组：每个元素当字段名，只保留这些字段**都不为空**的条目
      （如 ["problem", "proofread_comment"] = 既有问题、又有校对批注的条目）。
    """
    if grep is None:
        return entries
    if isinstance(grep, str):
        needle = grep.strip()
        if not needle:
            return entries
        keys = list(fields) if fields is not None else list(CACHE_ENTRY_FIELDS_DEFAULT)
        folded = needle.casefold()
        return [e for e in entries if any(folded in _grep_field_text(e, k).casefold() for k in keys)]
    if isinstance(grep, list):
        names = [str(x).strip() for x in grep if str(x).strip()]
        if not names:
            return entries
        for name in names:
            if name not in CACHE_ENTRY_FIELDS:
                raise AgentToolError(
                    f"grep 数组里不是有效的字段名：{name}（可选：{'、'.join(CACHE_ENTRY_FIELDS)}）"
                )
        return [e for e in entries if all(_grep_field_present(e, name) for name in names)]
    raise AgentToolError("grep 必须是字符串（按内容搜索）或字符串数组（按字段非空过滤）")


def _cache_grep_note(grep: Any, total: int) -> str | None:
    """给渲染层的一句话说明；grep 没实际生效（空串/空数组）时返回 None。"""
    if isinstance(grep, str) and grep.strip():
        return f"grep「{grep.strip()}」（文件共 {total} 条）"
    if isinstance(grep, list):
        names = [str(x).strip() for x in grep if str(x).strip()]
        if names:
            return f"grep 非空：{'、'.join(names)}（文件共 {total} 条）"
    return None


def _tool_read_transl_cache(runner: AgentRunner, args: dict[str, Any]) -> Any:
    filename = str(args.get("filename", "")).strip()
    if not filename:
        raise AgentToolError("filename is required")
    fields = _normalize_cache_fields(args)
    pid = runner._project_id()
    data = runner._http_get(f"/api/projects/{pid}/cache/{urllib.parse.quote(filename)}")
    raw_entries = [e for e in data.get("entries", []) if isinstance(e, dict)]
    total_entries = len(raw_entries)
    # grep 在原始条目上过滤（数组模式要判任意字段是否非空，投影之后就看不到没选中的列了），
    # 之后再按 fields 裁剪。
    grep_arg = args.get("grep")
    entries = _project_cache_entries(_grep_cache_entries(raw_entries, grep_arg, fields), fields)
    result_extra: dict[str, Any] = {}
    grep_note = _cache_grep_note(grep_arg, total_entries)
    if grep_note:
        result_extra["grep_note"] = grep_note
    # trans_by 与 search 同一套规则（见 _dominant_trans_by）：逐条只给少数派，多数派与空值
    # 删掉、改记在顶层 majority_trans_by。默认精简集里没有这列，只有 fields 点名要时才处理。
    if fields is not None and "trans_by" in fields:
        dominant = _dominant_trans_by(entries)
        for entry in entries:
            _strip_dominant_trans_by(entry, dominant)
        if dominant:
            result_extra["majority_trans_by"] = dominant
    result_extra["fields"] = list(fields) if fields is not None else list(CACHE_ENTRY_FIELDS_DEFAULT)
    if fields is None:
        result_extra["fields_note"] = (
            "默认精简字段：post_dst_preview 只在译后处理真的改了内容时返回（只差首尾「」这类"
            "对话符号不算），proofread_* / trans_by / 备注等空值已省略；要看其它字段传 fields。"
        )
    index_spec = str(args.get("index", "") or "").strip()
    # 不指定 index：返回前 30 条，供 Agent 通览
    if not index_spec:
        picked = entries[:30]
        return {"filename": filename, "count": len(entries), "returned": len(picked), "entries": picked, **result_extra}

    wanted = _parse_index_spec(index_spec)
    if not wanted:
        raise AgentToolError(f"无法解析 index 列表：{index_spec!r}（示例：33-40,50-60）")
    by_index = {int(e.get("index", -1)): e for e in entries if e.get("index") is not None}

    # context=N：目标条目向上（默认）或上下各多带 N 句（修问题/润色要知道上文才敢动）。
    # 按文件顺序连续取，扩展出来的行 index 带 *（见 _mark_context_row）。
    raw_context = args.get("context", 0)
    try:
        context = max(0, min(int(raw_context), 20))
    except (TypeError, ValueError):
        raise AgentToolError(f"context 必须是 0-20 的整数（收到 {raw_context!r}）")
    only_preceding = _only_preceding_arg(args)

    result: dict[str, Any] = {
        "filename": filename,
        "count": len(entries),
        "context": context,
        **result_extra,
    }
    if context > 0:
        result["only_preceding"] = only_preceding

    if context > 0 and by_index:
        # 以命中 index 的闭包向外扩 N 句：例如 index="205-206", context=3
        # -> 只给上文返回 202~206，两边都给返回 202~209。多个命中段各自扩展后合并。
        after = 0 if only_preceding else context
        reach = after + context  # 两段窗口相接/重叠就并成一段（只给上文时窄一半）
        spans: list[tuple[int, int]] = []
        for i in sorted(wanted):
            if spans and i <= spans[-1][1] + reach + 1:
                spans[-1] = (spans[-1][0], i)
            else:
                spans.append((i, i))
        wanted_ctx: set[int] = set(wanted)
        for a, b in spans:
            for j in range(max(0, a - context), b + after + 1):
                wanted_ctx.add(j)
        picked_ctx = [
            by_index[i] if i in wanted else _mark_context_row(by_index[i])
            for i in sorted(wanted_ctx)
            if i in by_index
        ]
        result["returned"] = len(picked_ctx)
        result["entries"] = picked_ctx
        missing = sorted(i for i in wanted if i not in by_index)
    else:
        picked = [by_index[i] for i in sorted(wanted) if i in by_index]
        missing = sorted(i for i in wanted if i not in by_index)
        result["returned"] = len(picked)
        result["entries"] = picked

    if missing:
        result["missing_indexes"] = missing
    return result


def _tool_read_output(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """读取最终输出文件（gt_output，交付物）。输出是缓存经 postDict 替换、
    控制符还原后的最终形态，和缓存可能不完全一致——验收交付物用它。"""
    filename = str(args.get("filename", "")).strip()
    if not filename:
        raise AgentToolError("filename is required")
    pid = runner._project_id()
    cfg = urllib.parse.quote(runner.state.config_file_name)
    try:
        data = runner._http_get(f"/api/projects/{pid}/output/{urllib.parse.quote(filename)}?config={cfg}")
    except AgentToolError as exc:
        # 文件不存在时附上输出目录清单，省一轮试错
        listing = runner._http_get(f"/api/projects/{pid}/files")
        available = [f.get("name") for f in listing.get("output_files", []) if f.get("name")]
        if available:
            raise AgentToolError(f"{exc}. 可用的输出文件：{available}") from exc
        raise
    # 输出条目里 message 位就是最终译文；统一映射成 {index, name, message}
    entries = [
        {"index": e.get("index"), "name": e.get("name", ""), "message": e.get("pre_src", "")}
        for e in data.get("entries", [])
        if isinstance(e, dict)
    ]
    index_spec = str(args.get("index", "") or "").strip()
    if not index_spec:
        return {"filename": filename, "count": len(entries), "returned": len(entries[:30]), "entries": entries[:30]}
    wanted = _parse_index_spec(index_spec)
    if not wanted:
        raise AgentToolError(f"无法解析 index 列表：{index_spec!r}（示例：1-100）")
    picked = [e for e in entries if _entry_index(e) in wanted]
    available = {_entry_index(e) for e in entries}
    missing = sorted(i for i in wanted if i not in available)
    result: dict[str, Any] = {
        "filename": filename,
        "count": len(entries),
        "returned": len(picked),
        "entries": picked,
    }
    if missing:
        result["missing_indexes"] = missing
    return result


def _parse_index_spec(spec: str) -> set[int]:
    """解析 \"33-40,50-60\" / \"5,9,12\" / \"100-105\" 为 index 集合。

    带上下文的那几个工具会把上下文行的 index 写成 "12*"（见 _mark_context_row）：模型照抄
    过来时按 12 处理——号数是对的，没道理为这个多报一次错。
    """
    result: set[int] = set()
    for part in spec.split(","):
        token = part.strip().replace("*", "")  # 星标（上下文行标记）在 index 里没有别的含义
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


# /cache/search 返回体里逐行的命中标记（服务端给界面用的：缓存页拿它画「原文/译文/问题」
# 小徽标）。逐行丢给模型纯属噪音——命中的位置从行内容（post_src / pre_dst / problem）直接
# 看得到。这里收成顶层一条汇总（见 _slim_search_results）。
_SEARCH_MATCH_KEYS: dict[str, str] = {
    "match_src": "src",
    "match_dst": "dst",
    "match_problem": "problem",
}

# /input/search 同样有逐行命中标记，只是原文侧可搜的只有两列：正文与说话人。
_INPUT_SEARCH_MATCH_KEYS: dict[str, str] = {
    "match_src": "src",
    "match_name": "name",
}


def _slim_search_results(
    result: dict[str, Any],
    field: str,
    context: int,
    match_keys: dict[str, str] | None = None,
) -> None:
    """就地精简搜索接口的返回（模型看到的那份）：/cache/search 与 /input/search 共用。

    - 逐行的命中标记（缓存是 match_src/match_dst/match_problem，原文是 match_src/match_name）
      去掉，改成顶层 matched_in 汇总一次（只在 field="all" 时给：指定 field 的搜索本来就只有
      那一侧会命中，汇总没有信息量）；
    - 逐行的 in_context（服务端标注的上下文行）删掉，改成把**上下文行的 index 标成 "12*"**
      ——"哪行是搭着给的上文"必须一眼看得出来（index 是跨工具对齐条目的把手），而一个
      布尔标注字段每行都要占一截；带上下文（context>0）时才有这回事；
    - trans_by（只有缓存条目有）逐条只给**少数派**（见 _dominant_trans_by）：逐条的多数派与
      空值都删掉，多数派放顶层 majority_trans_by 记一次——这批"大头是谁翻的、哪几条是别人改的"
      一目了然。
    """
    keys = match_keys if match_keys is not None else _SEARCH_MATCH_KEYS
    rows = result.get("results")
    if not isinstance(rows, list):
        return
    dominant = _dominant_trans_by(rows)
    counts: dict[str, int] = {}
    slimmed: list[Any] = []
    for row in rows:
        if not isinstance(row, dict):
            slimmed.append(row)
            continue
        matched = False
        for key, label in keys.items():
            if row.get(key):
                counts[label] = counts.get(label, 0) + 1
                matched = True
        clean = {k: v for k, v in row.items() if k not in keys}
        clean.pop("in_context", None)
        # 带上下文时：没有任何命中标记的行就是搭着给的上文（命中行一定有标记），标个 *
        if context > 0 and not matched:
            clean = _mark_context_row(clean)
        _strip_dominant_trans_by(clean, dominant)
        slimmed.append(clean)
    result["results"] = slimmed
    if field == "all" and counts:
        result["matched_in"] = counts
    if dominant:
        result["majority_trans_by"] = dominant


# 搜索类工具的分页口径（与 list_problems 同名同义）：limit 是本页最多几条**命中**，
# offset 是跳过前几条命中。默认 100 = 历史行为（"一次看遍某个词的所有出现处"是搜索的常用
# 姿势，不宜像 list_problems 那样默认 10）。
_SEARCH_LIMIT_DEFAULT = 100
_SEARCH_LIMIT_MAX = 200
# 带 context 时一行命中要搭上前后各 N 句，所以整页行数按它换算命中上限
# （命中数 ≈ 200 // (2N+1)，见 _tool_search_transl_cache）——**整个返回体最多 200 行**。
# 不带 context 时行数就是命中数，而 limit 上限本身就是 200，两边口径一致。
_SEARCH_ROW_BUDGET = 200


def _search_paging_args(args: dict[str, Any]) -> tuple[int, int]:
    """limit / offset：与本仓库其它清单工具同一套（非法值按默认处理，不报错）。"""
    raw_limit = args.get("limit", _SEARCH_LIMIT_DEFAULT)
    raw_offset = args.get("offset", 0)
    try:
        limit = max(1, min(int(raw_limit), _SEARCH_LIMIT_MAX))
    except (TypeError, ValueError):
        limit = _SEARCH_LIMIT_DEFAULT
    try:
        offset = max(0, int(raw_offset))
    except (TypeError, ValueError):
        offset = 0
    return limit, offset


def _apply_search_paging(result: dict[str, Any], offset: int) -> None:
    """给搜索结果补上分页口径（就地改），字段名与 list_problems 对齐。

    - returned：本页的**命中数**（服务端的 returned_hits；不带 context 时就是行数）；
    - returned_rows：本页总行数（带 context 时 = 命中行 + 搭着给的前后文行）；
    - has_more：offset 之后还有命中。total 始终是全部命中数，不受分页影响。
    """
    rows = result.get("results")
    hits = result.pop("returned_hits", None)
    row_count = result.pop("returned", None)
    if not isinstance(hits, int):
        hits = len(rows) if isinstance(rows, list) else 0
    result["offset"] = offset
    result["returned"] = hits
    if isinstance(row_count, int):
        result["returned_rows"] = row_count
    total = result.get("total")
    result["has_more"] = isinstance(total, int) and offset + hits < total


def _tool_search_transl_cache(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """在缓存里搜译文/原文/问题；context=N 时每条命中再带上 N 句上文（默认只给上文）。

    只给命中行常常不够判断——比如查「ドルード」，要决定该译成「多鲁德」还是「杜罗德」，
    得看这句前后的对话与说话人，和 read_transl_cache 的 context 是同一个用途。
    """
    query = str(args.get("query", "")).strip()
    field = str(args.get("field", "all")).strip() or "all"
    if not query:
        raise AgentToolError("query is required")
    filename = str(args.get("filename", "") or "").strip()
    raw_context = args.get("context", 0)
    try:
        context = max(0, min(int(raw_context or 0), 20))
    except (TypeError, ValueError):
        raise AgentToolError(f"context 必须是 0-20 的整数（收到 {raw_context!r}）")
    # 带上下文时收紧命中上限：命中 × 每条搭的行数一起返回，整页压在 _SEARCH_ROW_BUDGET 行内，
    # 否则"命中上百条 × 前后各几句"会直接把返回体撑爆。只给上文时每条只搭 N 行，同样的行数
    # 预算里能多给几条命中。total 不受影响（仍报全部命中数）。
    only_preceding = _only_preceding_arg(args)
    limit, offset = _search_paging_args(args)
    rows_per_hit = (context + 1) if only_preceding else (2 * context + 1)
    max_hits = limit if context == 0 else min(limit, max(1, _SEARCH_ROW_BUDGET // rows_per_hit))
    pid = runner._project_id()
    body: dict[str, Any] = {
        "query": query,
        "field": field,
        "options": {"re": False},
        "max_results": max_hits,
        "config_file_name": runner.state.config_file_name,
    }
    if context:
        body["context"] = context
        if only_preceding:
            body["preceding_only"] = True  # 服务端默认两边都给（界面在用），这边显式只要上文
    if offset:
        body["offset"] = offset
    if filename:
        body["filename"] = filename
    result = runner._http_post(f"/api/projects/{pid}/cache/search", body)
    if isinstance(result, dict):
        _slim_search_results(result, field, context)
        _apply_search_paging(result, offset)
    notes: list[str] = []
    if isinstance(result, dict) and context:
        result["context"] = context  # 服务端已回；这里兜底，保证调用方一定看得到
        result["only_preceding"] = only_preceding
        scope = (
            f"每条命中前面 {context} 句（默认只给上文，要上下都给传 only_preceding=false）"
            if only_preceding
            else f"每条命中前后各 {context} 句"
        )
        notes.append(
            f"已带上下文：{scope}（包含关键词的那行是命中，index 带 * 的是搭着给的上下文行）；"
            f"带上下文时命中上限收紧为 {max_hits} 条、整页最多 {_SEARCH_ROW_BUDGET} 行，"
            "total 仍是全部命中数——命中很多时用 offset 翻页，或配合 filename 缩小范围。"
        )
    # 指定了文件但 0 命中：确认一下该文件是否存在，避免模型误以为关键词不匹配
    if isinstance(result, dict) and not result.get("total") and filename:
        try:
            listing = runner._http_get(f"/api/projects/{pid}/cache")
            if not any(f.get("name") == filename for f in listing.get("files", [])):
                notes.append(
                    f"缓存文件 {filename} 不存在（检查 list_transl_cache 的文件名拼写）；这是全项目搜索的 0 命中。"
                )
        except AgentToolError:
            pass
    if notes and isinstance(result, dict):
        existing = str(result.get("note") or "")
        result = {**result, "note": "；".join([part for part in [existing, *notes] if part])}
    return result


def _tool_search_input(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """在待翻译原文里搜关键词/说话人；context=N 时每条命中再带上 N 句上文（默认只给上文）。

    与 search_transl_cache 是一套用法，区别只在搜的对象：那边搜**缓存**（原文 + 译文 + 问题，
    含已翻的部分），这边搜**输入文件**（还没翻译的原文全文）。用途也由此分工——
    - 定译法/收字典前，查某个称呼、口头禅、专有名词在全篇出现过多少次、都出现在什么上下文里
      （出现次数与说话人是"该不该收进字典、收哪个写法"的依据）；
    - 拿不准某句原文的语境时，比 read_input_file 逐段读更省 token；
    - 命中的 filename + index 可直接交给 read_input_file 精读。

    搜的是原文，所以**译文侧的问题（漏译/残留日文）不在这里**，那些用 search_transl_cache。
    每次搜索都要把涉及的输入文件过一遍文件插件（比搜缓存慢），要缩小范围就传 filename。
    """
    query = str(args.get("query", "")).strip()
    if not query:
        raise AgentToolError("query is required")
    field = str(args.get("field", "all") or "all").strip() or "all"
    if field not in ("all", "src", "name"):
        raise AgentToolError("field must be one of: all, src, name")
    filename = str(args.get("filename", "") or "").strip()
    raw_context = args.get("context", 0)
    try:
        context = max(0, min(int(raw_context or 0), 20))
    except (TypeError, ValueError):
        raise AgentToolError(f"context 必须是 0-20 的整数（收到 {raw_context!r}）")
    # 与 search_transl_cache 同一套收紧规则：命中 × 每条搭的行数一起返回，整页压在
    # _SEARCH_ROW_BUDGET 行内。total 不受影响（仍是全部命中数）。
    only_preceding = _only_preceding_arg(args)
    limit, offset = _search_paging_args(args)
    rows_per_hit = (context + 1) if only_preceding else (2 * context + 1)
    max_hits = limit if context == 0 else min(limit, max(1, _SEARCH_ROW_BUDGET // rows_per_hit))
    pid = runner._project_id()
    body: dict[str, Any] = {
        "query": query,
        "field": field,
        "options": {"re": False},
        "max_results": max_hits,
        "config_file_name": runner.state.config_file_name,
    }
    if context:
        body["context"] = context
        if only_preceding:
            body["preceding_only"] = True  # 服务端默认两边都给（界面在用），这边显式只要上文
    if offset:
        body["offset"] = offset
    if filename:
        body["filename"] = filename
    result = runner._http_post(f"/api/projects/{pid}/input/search", body)
    if isinstance(result, dict):
        _slim_search_results(result, field, context, _INPUT_SEARCH_MATCH_KEYS)
        _apply_search_paging(result, offset)
    notes: list[str] = []
    # 解析不了的文件（插件/格式问题）被跳过了：明说，否则"这个文件里没有"和"这个文件没读"
    # 看起来一模一样。要诊断那个文件用 read_input_file。
    if isinstance(result, dict) and result.get("files_failed"):
        notes.append(
            f"这些输入文件解析失败、没参与搜索：{'、'.join(str(n) for n in result['files_failed'])}"
            "（文件插件/格式问题，用 read_input_file 试读该文件可看到具体报错）；"
            "它们里面有没有命中是未知的。"
        )
    if isinstance(result, dict) and context:
        result["context"] = context  # 服务端已回；这里兜底，保证调用方一定看得到
        result["only_preceding"] = only_preceding
        scope = (
            f"每条命中前面 {context} 句（默认只给上文，要上下都给传 only_preceding=false）"
            if only_preceding
            else f"每条命中前后各 {context} 句"
        )
        notes.append(
            f"已带上下文：{scope}（包含关键词的那行是命中，index 带 * 的是搭着给的上下文行）；"
            f"带上下文时命中上限收紧为 {max_hits} 条、整页最多 {_SEARCH_ROW_BUDGET} 行，"
            "total 仍是全部命中数——命中很多时用 offset 翻页。"
        )
    # 指定了文件但 0 命中：确认一下该输入文件是否存在，避免模型误以为关键词不匹配
    if isinstance(result, dict) and not result.get("total") and filename:
        try:
            listing = runner._http_get(f"/api/projects/{pid}/files")
            available = [
                str(f.get("name") or "")
                for f in listing.get("input_files", [])
                if isinstance(f, dict) and f.get("is_file", True)
            ]
            if filename not in available:
                notes.append(
                    f"输入文件 {filename} 不存在（检查 list_input_files 的文件名拼写）；"
                    "这是全项目搜索的 0 命中。"
                )
        except AgentToolError:
            pass
    if notes and isinstance(result, dict):
        existing = str(result.get("note") or "")
        result = {**result, "note": "；".join([part for part in [existing, *notes] if part])}
    return result


# patch_transl_cache 允许更新的条目字段白名单（其余字段一律不动，避免误改 problem/preview
# 等派生字段）。trans_by 不在这里：它是"谁改的"标记，由工具自动填成本会话的模型名，
# 不能让模型自己声明（写 "manual" 这种会把"翻译引擎翻的"和"Agent 改的"混起来）。
_PATCHABLE_FIELDS: frozenset[str] = frozenset({
    "pre_dst",
    "proofread_dst",
    # 校对批注也算可改：校对子代理写下意见、主 Agent 改完译文后要能把它清掉（或改写）
    "proofread_comment",
})

# "改了译文"的那两个字段：只有它们被改过才给条目盖 trans_by（谁改的）；只写校对意见不算
_TRANSLATION_FIELDS: frozenset[str] = frozenset({"pre_dst", "proofread_dst"})


def _patchable_fields_text(allowed: frozenset[str] | None = None) -> str:
    """可改字段的一行文本（按 CACHE_ENTRY_FIELDS 的顺序，输出稳定）。

    system prompt 的字段说明与 patch 工具的报错都用它，避免两处各写一份再漂移。
    allowed 给校对子代理那样的窄白名单用（见 SUBAGENT_PATCHABLE_FIELDS）。
    """
    fields = allowed if allowed is not None else _PATCHABLE_FIELDS
    return " / ".join(name for name in CACHE_ENTRY_FIELDS if name in fields)


def _dominant_trans_by(rows: list[Any]) -> str:
    """这批条目里出现最多的 trans_by 值（= 这批的"正常情况"，多半就是翻译引擎翻的）。

    逐条重复报它没有信息量，所以调用方把它删掉、改在顶层 majority_trans_by 记一次；少数派
    （Agent 用 patch_transl_cache 改过的、手工改的）才逐条留在 trans_by 上。空值不参与统计，
    一个值都没有时返回空串，调用方据此不做任何过滤。

    按**数据本身**判，而不是跟配置里「翻译任务会用」那份模型名比：项目当初是哪个模型翻的
    只有条目自己知道，用户换过「翻译器默认」之后按配置比会把老条目全当成异常来源显示。
    """
    counts: Counter[str] = Counter(
        str(row.get("trans_by") or "").strip() for row in rows if isinstance(row, dict)
    )
    counts.pop("", None)
    return counts.most_common(1)[0][0] if counts else ""


def _strip_dominant_trans_by(row: dict[str, Any], dominant: str) -> None:
    """就地删掉这一行的 trans_by：空值、或正好是多数派那个值。

    dominant 为空串（这批一个标记都没有）时只删空值——判不出"正常值"就别动，宁可多显示。
    """
    if str(row.get("trans_by") or "").strip() in ("", dominant):
        row.pop("trans_by", None)


def _agent_model_name(runner: AgentRunner) -> str:
    """本会话 Agent 后端的模型名：patch 改过的条目用它写 trans_by。

    优先取本回合实际在跑的模型名（_resolve_llm 解析出的 _model），拿不到时退回 state
    里那份后端配置（「了解项目」报的就是它）；都没有就返回空串——宁可不写标记，
    也不要瞎填一个模型名。
    """
    model = str(getattr(runner, "_model", "") or "").strip()
    if model:
        return model
    state = runner.state
    profile = getattr(state, "backend_profile_data", None) or {}
    name = getattr(state, "backend_profile_name", "") or ""
    return str(_backend_summary(profile, name).get("model") or "")


# ---- 换行归一化（patch_transl_cache 写回前）----
# 模型在 Markdown 表格里看到的换行是 <br>——这不是渲染器的发明：翻译管线送翻时，原文里的
# 换行（真 \r\n、真 \n、乃至字面两字符 \r\n/\n）本来就全部替换成 <br> 再发给模型
# （ForGalJsonTranslate.py:72-83），模型输出的 <br> 在落盘前又会被换回真换行
# （_normalize_parsed_translation_text）。所以模型写 <br> 是管线内的标准写法。
# 真正的缺口在 patch 的写路径：正常翻译有"模型输出 → 缓存"的归一化，patch 没有——
# 模型照着表格写 <br> 就原样进了缓存，译文里的真换行变成了字面 <br>。
# 下面把这条补上：写回前把 <br> / 真换行 / 字面 \n 全部统一成**该条目自己的换行形式**。

def _infer_linebreak_symbol(*texts: str) -> str:
    """从参考文本里推断"这条数据用的换行符"（优先级照抄翻译管线，ForGalJsonTranslate.py:72-79）：

    字面 \\r\\n > 真 \\r\\n > 字面 \\n > 真 \\n；都没有返回空串。给多条参考文本时取**第一个
    有换行的**——patch 归一化里先给字段现值（正在编辑的那份，风格以它为准）、再给 post_src。
    """
    for text in texts:
        if not text:
            continue
        if "\\r\\n" in text:
            return "\\r\\n"
        if "\r\n" in text:
            return "\r\n"
        if "\\n" in text:
            return "\\n"
        if "\n" in text:
            return "\n"
    return ""


def _normalize_linebreaks_like(value: str, *reference_texts: str) -> str:
    """把 value 里的各种换行形态统一成参考文本所用的那种。

    参考文本推断不出换行（整条没有换行）时**原样返回**——管线在 n_symbol 为空时同样不动
    模型输出的 <br>，保持同一口径。
    """
    if not value:
        return value
    symbol = _infer_linebreak_symbol(*reference_texts)
    if not symbol:
        return value
    # 先把所有形态折成真 \n：<br>（宽松大小写与自闭合）、真 \r\n / \r、
    # 字面两字符 \r\n / \n / \r（模型双重转义时会出现）
    v = re.sub(r"(?i)<br\s*/?>", "\n", value)
    v = v.replace("\r\n", "\n").replace("\r", "\n")
    v = v.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
    # 再展开成该条目的换行形式
    if symbol != "\n":
        v = v.replace("\n", symbol)
    return v


def _plan_cache_patches(
    entries: list[Any], patches_raw: list[Any], allowed: frozenset[str]
) -> dict[str, Any]:
    """把 patches 解析成「要改哪些条目的哪些字段」（**只读**，不动 entries）。

    patch_transl_cache 的落盘与审批卡上的「将要变更」预览共用这一份判断：卡上给用户看的
    before→after 就是真执行会写下去的东西，不会两边各算一遍再漂移。调用方拿到 plan 后
    自己逐条 entry.update(updates) 才算写。返回：
    - by_index：index → 条目（handler 盖章 trans_by 时要用）
    - plan：[{entry, index, updates}]，按顺序应用
    - changes / skipped / not_found：与原来逐条累积出来的字段一致
    """
    by_index: dict[int, dict[str, Any]] = {}
    for e in entries:
        idx = e.get("index")
        if idx is not None:
            try:
                by_index[int(idx)] = e
            except (TypeError, ValueError):
                continue

    plan: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    not_found: list[int] = []
    changes: list[dict[str, Any]] = []
    # 同一个 index 被两条 patch 命中时，第二条的 before 要看见第一条的结果（真执行是逐条
    # entry.update），所以在影子副本上模拟同样的链式修改，算出来的 diff 才一致。
    shadow: dict[int, dict[str, Any]] = {}
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
        updates = {k: v for k, v in p.items() if k in allowed and v is not None}
        if not updates:
            skipped.append(
                {"index": idx_i, "reason": f"无可更新字段（只允许 {_patchable_fields_text(allowed)}）"}
            )
            continue
        now = shadow.setdefault(idx_i, dict(entry))
        for f, v in list(updates.items()):
            if isinstance(v, str):
                # 换行归一化：字段现值（正在编辑的那份）的风格优先，其次该条 post_src 的风格。
                # 归一化后的值就是真执行会写下去的东西，变更卡的 before→after 也用它——所见即所得。
                v = _normalize_linebreaks_like(
                    v, str(now.get(f) or ""), str(entry.get("post_src") or "")
                )
                updates[f] = v
            changes.append(_change(f"#{idx_i}.{f}", now.get(f), v, "replace"))
            now[f] = v
        plan.append({"entry": entry, "index": idx_i, "updates": updates})
    return {
        "by_index": by_index,
        "plan": plan,
        "changes": changes,
        "skipped": skipped,
        "not_found": not_found,
    }


def _tool_patch_transl_cache(
    runner: AgentRunner, args: dict[str, Any], allowed_fields: frozenset[str] | None = None
) -> Any:
    """改缓存条目的字段（主 Agent 可改 pre_dst / proofread_dst / proofread_comment）。

    allowed_fields 是"这次调用最多能改哪些字段"的窄白名单，给校对子代理用：它拿同一个工具，
    但只放得住 proofread_comment——**改不了译文是靠这张白名单 + 子代理的入参 schema 双保险**，
    不是靠提示词自觉（见 _subagent_handlers / _subagent_patch_schema）。
    """
    allowed = allowed_fields if allowed_fields is not None else _PATCHABLE_FIELDS
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

    planned = _plan_cache_patches(entries, patches_raw, allowed)
    by_index = planned["by_index"]
    skipped = planned["skipped"]
    not_found = planned["not_found"]
    changes = planned["changes"]
    applied_indexes: list[int] = []
    retranslated_indexes: list[int] = []
    for item in planned["plan"]:
        item["entry"].update(item["updates"])
        applied_indexes.append(item["index"])
        if item["updates"].keys() & _TRANSLATION_FIELDS:
            retranslated_indexes.append(item["index"])

    if not applied_indexes:
        # 把跳过原因带上：否则模型只看到"没有条目被更新"，不知道是字段不许改还是 index 写错了
        # （校对子代理硬塞译文字段时也靠这条说清"只允许 proofread_comment"）
        reasons = "；".join(str(s.get("reason") or "") for s in skipped if s.get("reason"))
        raise AgentToolError(
            f"没有条目被更新（updated=0, skipped={len(skipped)}, not_found={len(not_found)}）"
            + (f"：{reasons}" if reasons else "")
        )

    # 译文被改过的条目标上本会话的模型名（trans_by 不在 _PATCHABLE_FIELDS 里，模型指定不了）：
    # 用户与后续复核才分得清"这句是 Agent 手改的"还是"翻译引擎翻的"。
    # 只写校对意见（proofread_comment）的条目**不盖章**——译文一个字没动，盖了会把"谁翻的"弄错，
    # 也会让 trans_by 的少数派统计多出一堆假来源。
    agent_model = _agent_model_name(runner)
    if agent_model:
        for idx_i in retranslated_indexes:
            by_index[idx_i]["trans_by"] = agent_model

    save_body = {
        "filename": filename,
        "entries": entries,
        "config_file_name": runner.state.config_file_name,
    }
    # 注意：/cache/save 的应答带全文件 entries（重建 problem 后原样回传给
    # 桌面端用），绝不能整体透传给 LLM。这里只提取「被改条目重建后仍存在的问题」
    # 作为轻量校验信号——没引入新问题的条目不出现在 problems 里；改了什么由
    # changes 的 before→after 表达，不重复回传最终译文，不必再 read_transl_cache。
    save_result = runner._http_post(f"/api/projects/{pid}/cache/save", save_body)
    result: dict[str, Any] = {
        "filename": filename,
        "updated": len(applied_indexes),
        "changes": changes,
    }
    if agent_model:
        result["trans_by"] = agent_model
    if not_found:
        result["not_found_indexes"] = sorted(not_found)
    if skipped:
        result["skipped"] = skipped
    saved_entries = save_result.get("entries") if isinstance(save_result, dict) else None
    if isinstance(saved_entries, list):
        wanted = set(applied_indexes)
        problems: list[dict[str, Any]] = []
        for e in saved_entries:
            if not isinstance(e, dict):
                continue
            try:
                idx_i = int(e.get("index"))
            except (TypeError, ValueError):
                continue
            if idx_i not in wanted:
                continue
            problem = str(e.get("problem", ""))
            if not problem:
                continue
            problems.append({
                "index": idx_i,
                "problem": problem[:120] + ("…" if len(problem) > 120 else ""),
            })
        if problems:
            result["problems"] = problems
    return result


def _plan_cache_delete(
    entries: list[Any], wanted: set[int]
) -> tuple[list[Any], list[int], list[dict[str, Any]]]:
    """按 index 挑出要删的条目（**只读**）：返回（保留的条目、命中的 index、被删条目的预览）。

    delete_transl_cache 的落盘与审批卡上的「将要变更」预览共用这一份挑选：卡上列出的
    "会删掉哪几条、删的是什么"就是真执行会删的那些。
    """
    kept: list[Any] = []
    deleted_indexes: list[int] = []
    previews: list[dict[str, Any]] = []
    for e in entries:
        try:
            idx = int(e.get("index"))
        except (TypeError, ValueError):
            kept.append(e)
            continue
        if idx in wanted:
            deleted_indexes.append(idx)
            preview = str(e.get("pre_dst", "") or e.get("post_dst", "") or "")
            if len(preview) > 60:
                preview = preview[:57] + "…"
            previews.append({"index": idx, "text": preview})
        else:
            kept.append(e)
    return kept, deleted_indexes, previews


def _tool_delete_transl_cache(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """删除缓存：物理删除条目后，重启翻译时这些句子会 cache 未命中而重新翻译。

    两种粒度：
    - 指定 indexes：删除某个缓存文件里的部分条目（index 可用 read_transl_cache /
      list_problems 返回的 index，支持 "33-40,50-60" 区间写法）
    - 不指定 indexes：删除整个缓存文件（该文件全部句子重翻）
    删除不可撤销；rebuilda/rebuildr 依赖缓存存在，删除后不要跑重建。
    """
    filename = str(args.get("filename", "")).strip()
    if not filename:
        raise AgentToolError("filename is required")
    pid = runner._project_id()
    index_spec = str(args.get("indexes", "") or "").strip()

    # 整文件删除
    if not index_spec:
        if filename == "*" or filename == "all":
            # 全部缓存文件：列出后逐个删
            listing = runner._http_get(f"/api/projects/{pid}/cache")
            targets = [f["name"] for f in listing.get("files", []) if str(f.get("name", "")).endswith(".json")]
            if not targets:
                return {"deleted_files": [], "not_found_files": [], "note": "没有可删除的缓存文件"}
            res = runner._http_post(f"/api/projects/{pid}/cache/delete-file", {"filenames": targets})
            return {"deleted_files": res.get("deleted_files", []), "not_found_files": res.get("not_found_files", []), "note": "已删除全部缓存文件，重启翻译将全部重翻"}
        res = runner._http_post(f"/api/projects/{pid}/cache/delete-file", {"filenames": [filename]})
        return {"deleted_files": res.get("deleted_files", []), "not_found_files": res.get("not_found_files", [])}

    # 按 index 删除部分条目：读全量 -> 剔除命中 -> 写回（与 patch_transl_cache 同通道）
    wanted = _parse_index_spec(index_spec)
    if not wanted:
        raise AgentToolError(f"无法解析 indexes：{index_spec!r}（示例：33-40,50-60）")
    data = runner._http_get(f"/api/projects/{pid}/cache/{urllib.parse.quote(filename)}")
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        raise AgentToolError("缓存文件 entries 非数组，无法删除")

    kept, deleted_indexes, deleted_previews = _plan_cache_delete(entries, wanted)

    if not deleted_indexes:
        raise AgentToolError(f"没有命中的条目（文件共 {len(entries)} 条，请求 index：{sorted(wanted)}）")

    save_body = {
        "filename": filename,
        "entries": kept,
        "config_file_name": runner.state.config_file_name,
    }
    runner._http_post(f"/api/projects/{pid}/cache/save", save_body)
    missing = sorted(i for i in wanted if i not in deleted_indexes)
    result: dict[str, Any] = {
        "filename": filename,
        "count_before": len(entries),
        "count_after": len(kept),
        "deleted_indexes": deleted_indexes,
        "deleted_preview": deleted_previews[:50],
        "note": "被删除的句子已不在缓存中，重启翻译（start_translation）时它们会重新翻译",
    }
    if missing:
        result["missing_indexes"] = missing
    return result


# ---- 询问用户（ask_user）----
# 工具阻塞等回答、**不设超时**；用户跳过或回合被停止时，
# 该题以空答案返回（工具仍算成功，模型据此继续，而不是让整个回合报错）。
ASK_MAX_QUESTIONS = 4  # 一次最多问几题（界面一题一步，问太多就成了审讯）
ASK_MAX_OPTIONS = 6  # 每题最多几个候选项（用户永远还能自己填）
ASK_WAIT_TICK = 0.2  # 等待回答的轮询步长（秒）：只为尽快响应停止信号


def _normalize_ask_questions(args: dict[str, Any]) -> list[dict[str, Any]]:
    """校验并归一 ask_user 的问题：题干非空、选项去重后至少一个、题数与选项数有上限。"""
    raw = args.get("questions")
    if not isinstance(raw, list) or not raw:
        raise AgentToolError("questions 必须是非空数组")
    if len(raw) > ASK_MAX_QUESTIONS:
        raise AgentToolError(f"一次最多问 {ASK_MAX_QUESTIONS} 个问题（收到 {len(raw)} 个）")
    questions: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise AgentToolError("questions 里每一项都必须是一个对象")
        text = str(item.get("question") or "").strip()
        if not text:
            raise AgentToolError("每个问题都要有非空的 question")
        raw_options = item.get("options")
        if not isinstance(raw_options, list):
            raise AgentToolError(f"问题「{text}」缺少 options 数组")
        options: list[str] = []
        for option in raw_options:
            label = str(option or "").strip()
            if label and label not in options:
                options.append(label)
        if not options:
            raise AgentToolError(f"问题「{text}」至少要有一个非空选项")
        capped = options[:ASK_MAX_OPTIONS]
        recommended = str(item.get("recommended") or "").strip()
        if recommended and recommended not in capped:
            # 让它改而不是默默丢掉：零打断档位要靠这个值代答，写错就等于没推荐
            raise AgentToolError(
                f"问题「{text}」的 recommended（{recommended}）必须是 options 里的一项"
            )
        questions.append(
            {
                "question": text,
                "options": capped,
                "multiSelect": item.get("multiSelect") is True,
                "recommended": recommended,
            }
        )
    return questions


def _normalize_ask_answers(raw: Any, questions: list[dict[str, Any]]) -> list[list[str] | None]:
    """校验前端送回的答案：题数要对上、单选不许给多个值；空值 = 跳过（None）。"""
    if not isinstance(raw, list) or len(raw) != len(questions):
        raise AgentToolError(f"答案数量必须与问题数量一致（需要 {len(questions)} 个）")
    answers: list[list[str] | None] = []
    for idx, (item, question) in enumerate(zip(raw, questions), start=1):
        if item is None:
            answers.append(None)
            continue
        if not isinstance(item, list):
            raise AgentToolError(f"第 {idx} 题的答案必须是字符串数组或 null")
        values: list[str] = []
        for value in item:
            text = str(value or "").strip()
            if text and text not in values:
                values.append(text)
        if len(values) > 1 and not question.get("multiSelect"):
            raise AgentToolError(f"第 {idx} 题是单选，只能给一个答案")
        answers.append(values or None)
    return answers


def _format_ask_answers(questions: list[dict[str, Any]], answers: list[list[str] | None]) -> str:
    """给模型看的答案文本（固定口径）：`问题：答案1、答案2`，多题用 --- 分隔。"""
    lines = []
    for question, answer in zip(questions, answers):
        lines.append(f"{question['question']}：{'、'.join(answer) if answer else '（跳过）'}")
    return "\n---\n".join(lines)


def _tool_read_history_archive(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """读上下文压缩归档（会话目录里的 chunk 文件）。

    三种用法：不带参数列出归档；带 chunk 取全文；带 query 在全部归档里检索。
    归档只是"想不起来时回查"的兜底，所以单次回传有字符上限——超了就让模型
    改用关键词检索，而不是把整份归档塞回上下文（那就白压了）。
    """
    store = getattr(runner, "_store", None)
    if store is None:
        return {"archives": [], "note": "本会话没有落盘，压缩归档不可用"}
    chunks = store.list_chunks()
    if not chunks:
        return {"archives": [], "note": "本次会话还没有压缩归档（历史还没触发过上下文压缩）"}

    raw_chunk = str(args.get("chunk", "") or "").strip()
    query = str(args.get("query", "") or "").strip()
    limit = args.get("limit")
    if not isinstance(limit, int) or limit <= 0:
        limit = 30
    limit = min(limit, 200)

    if query:
        hits: list[dict[str, Any]] = []
        truncated = False
        for item in chunks:
            text = store.read_chunk(item["name"]) or ""
            for lineno, line in enumerate(text.splitlines(), 1):
                if query.lower() in line.lower():
                    hits.append({
                        "chunk": item["name"],
                        "line": lineno,
                        "text": _truncate_text(line.strip(), 300),
                    })
                    if len(hits) >= limit:
                        truncated = True
                        break
            if truncated:
                break
        return {"query": query, "hits": hits, "truncated": truncated}

    if not raw_chunk:
        return {
            "archives": [
                {"name": item["name"], "topics": item.get("topics") or ""} for item in chunks
            ],
            "note": "用 chunk 参数读其中一份，或用 query 关键词检索细节",
        }

    names = [item["name"] for item in chunks]
    name = raw_chunk if raw_chunk in names else ""
    if not name and raw_chunk.isdigit():
        index = int(raw_chunk)
        if 1 <= index <= len(names):
            name = names[index - 1]
    if not name:
        raise AgentToolError(f"没有这个归档：{raw_chunk}（可用：{'、'.join(names)}）")
    text = store.read_chunk(name) or ""
    truncated = len(text) > COMPACT_ARCHIVE_READ_CHARS
    return {
        "chunk": name,
        "content": text[:COMPACT_ARCHIVE_READ_CHARS] if truncated else text,
        "truncated": truncated,
        "note": "内容过长已截断，可用 query 检索关键词" if truncated else "",
    }


def _auto_ask_answers(questions: list[dict[str, Any]]) -> list[list[str] | None]:
    """「全自动-零打断」的自动作答：每题取推荐项，没给推荐就退而取第一个选项。

    取第一个是刻意的兜底——模型列选项时通常把首选放最前面，而这一档的语义就是
    "别停下来问我"：卡在等人作答上，比偶尔选歪一次更糟。多选问题也只给这一项。
    """
    answers: list[list[str] | None] = []
    for question in questions:
        recommended = str(question.get("recommended") or "").strip()
        options = list(question.get("options") or [])
        pick = recommended if recommended in options else (options[0] if options else "")
        answers.append([pick] if pick else None)
    return answers


def _tool_ask_user(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """问用户：阻塞当前回合直到用户作答（或回合被停止，此时按跳过返回）。

    **全自动-零打断**档位不阻塞：直接按每题的 recommended 代答（没填推荐就取第一个
    选项），用户完全不会被打断。模型仍照常"问"，只是拿到的是系统代选的答案。
    """
    questions = _normalize_ask_questions(args)
    if _normalize_permission_mode(runner.state.permission_mode) == AUTO_QUIET_MODE:
        answers = _auto_ask_answers(questions)
        _log(f"  🤖 零打断档位：按推荐项代答 {len(questions)} 个问题，不等用户")
        return {
            "summary": _format_ask_answers(questions, answers)
            + "\n（当前是「全自动-零打断」档位：以上答案由系统按推荐项自动选择，用户未被打断）",
            "questions": [question["question"] for question in questions],
            "answers": answers,
            "auto_answered": True,
        }
    answers = runner.ask_user(os.urandom(8).hex(), runner._active_tool_call_id, questions)
    return {
        "summary": _format_ask_answers(questions, answers),
        "questions": [question["question"] for question in questions],
        "answers": answers,
    }


# ---- 子代理（subagent）----
#
# 概念与 PI-Desktop 的一致：主 Agent 把"一份可以独立完成的工作"交给子代理——子代理有自己的
# system prompt、自己的消息历史、**受限的工具集**（拿不到委派工具，所以不会递归），跑完只交回
# 一份报告。它中间的思考与工具调用不进主 Agent 的上下文（省 token），但会作为事件推给界面
# （见 subagent_* 事件），所以用户看得到它在干什么。
#
# 子代理也有自己的上下文预算：工具往返堆超窗口时按父 Agent 的同一套规则压缩——窗口取自父
# Agent 的 contextWindow、切点复用 _find_compaction_cut、摘要复用 _summarize_messages。
# 压缩发生在子代理自己的消息里，主 Agent 拿到的仍是那份最终报告。
#
# 与 PI-Desktop 的一处刻意差异：那边一个 Task 调用只起一个子代理、**立即返回** delegationId，
# 再由 TaskWait／自动 resume 收口；我们这里**一次调用带一批任务、阻塞到全部跑完**。原因是
# 我们的回合里工具是串行执行的（见 run() 的 for tc in tool_calls），没有 resume 那套机制，
# 阻塞式最省事也最不容易出错；并发一点没少——同一批里的子代理是真并行跑的。

# 校对子代理的 system prompt：**只能提意见**（写 proofread_comment），不能改译文。
# 写"校对建议"还是"润色建议"由主 Agent 问过用户后写在任务说明里（见 _SUBAGENT_BRIEF_TEMPLATE），
# 这里只把两类意见的定义、写法与优先级讲清楚；任务说明没提时默认只写校对建议。
SUBAGENT_PROOFREAD_PROMPT = """你是 GalTransl 的**校对子代理**，只干一件事：读完分配给你的缓存，把意见写进缓存条目的 proofread_comment。

# 权力边界（越界即失败）
- 你**只能读**（缓存、人名表、翻译规范、问题清单），以及用 patch_transl_cache **写 proofread_comment** 这一个字段；
- 你的 patch_transl_cache 里只有 index 与 proofread_comment 两个入参：**译文字段（pre_dst / proofread_dst）根本不存在**，也没有委派、启动任务、改配置的权力；
- **你只负责这一次派给你的那些文件**（可能是一个，也可能是自动均分出来的一组）：read_transl_cache / patch_transl_cache 的 filename 只接受它们，范围外会被直接拒掉；list_problems 也只会列这些文件的问题；要核对某个词在别处的译法，用 search_transl_cache（它是全项目范围）；
- 发现问题就写意见，改由主 Agent 做——不要试图绕路。

# 写哪一类意见：以任务说明为准
你的意见分两类，**这一遍写哪一类由任务说明（"主 Agent 的额外要求"）指定**——那里面已经写明用户要的是什么：
- 只要求「校对建议」→ 只挑硬伤，不要顺带报风格偏好；
- 只要求「润色建议」→ 只提表达上的改进，别去纠结对错（顺手看到明显硬伤也可以带上一条）；
- 两类都要求 → 都写，但**硬伤优先**：先保证错译漏译都被抓出来，再谈润色。
任务说明没提这件事时，默认**只写校对建议**。

**校对建议（硬伤，四类）**
1. **错译**：意思翻错、主客颠倒、否定/时态/数量弄反；
2. **漏译**：原文有的信息译文里没有（整句漏掉、半句被吞、人称/称谓被省掉）；
3. **事实错误**：人名/地名/专有名词/设定的译法与项目既有译法或原文设定冲突（先查人名表与其它出现处）；
4. **明显不通**：中文不成句、指代错乱、说话人张冠李戴（结合 speaker 判断）。

**润色建议（没硬伤，但中文能更好）**
- 读起来别扭、有翻译腔：词序拗口、修饰语堆叠、一连串"的"；
- 口语不自然：对话像书面语，语气与角色设定（傲娇 / 冷淡 / 大小姐 / 死党…）对不上；
- 用词单调或不准：同一段反复用同一个词，拟声词、感叹处理得生硬；
- 节奏问题：该断句的地方拖成一长串，或该一口气说完的被拆得很碎。
写润色建议必须**给出具体改法**（"建议改成……"）——只说"不够好""可以更自然"等于没写；也不要为了凑数硬提，一条条目最多一条润色意见，优先挑真正影响阅读的。

**两类共同的三条规矩**：一条条目只写一次（再写会覆盖）；没问题的条目不要写；每条都要写清"问题是什么 + 该怎么改"，必要时给出原文依据。宁可少报，也不要拿噪音把真正的硬伤淹掉。

# 怎么干
1. 先 read_transl_cache 读你负责的区间（默认列就够：原文 post_src、译文 pre_dst、机翻自查 problem，以及别人写过的 proofread_comment）；
2. 要判断译名一致性：search_transl_cache 搜同一个词的其它出现处、get_name_table 看人名表；判断取舍时 read_guideline 看项目规范（润色建议尤其要以项目规范为准，别跟规范里定下的文风打架）；
3. 有疑问就用 patch_transl_cache 写进去，一条一个问题、写清"问题是什么 + 该怎么改"。可以一次多条：
   patch_transl_cache(filename="<你的文件>", patches=[
     {"index": 33, "proofread_comment": "漏译：原文「おっぱい」在译文里没有对应词，建议补为「欧派」"},
     {"index": 41, "proofread_comment": "错译：原文是「否定」，译文翻成了肯定"},
     {"index": 58, "proofread_comment": "润色：直译得比较生硬，建议改成「我才不是特意为你做的呢！」"},
   ])
   没问题的条目不要写；同一个 index 只写一次（再写会覆盖）。
4. problem 里的机翻提示可以参考，但那是统计标签，**只写你核对过的**，不要照抄。
5. 读不完就分段读（index 支持区间），不要为了省事跳过没读的条目——你没读的部分等于没校对。

# 收尾
不再调用工具后，输出一份**简短**报告（这是主 Agent 唯一会看到的你的输出）：
- 负责的文件与区间、读了多少条；
- 写了几条意见、分别是哪一类（校对：错译/漏译/事实错误/不通；润色：表达/语气/用词/节奏）；
- 拿不准但值得人看一眼的点。
不要在报告里复述每条意见的全文（proofread_comment 里已经有了），也不要贴原文译文。"""

# 子代理的 user 消息（任务说明）。校对：文件是任务的天然边界——不同子代理写不同文件的
# proofread_comment，互不打架；同一个人也能只领一个区间（或自动均分出来的一组文件）。
_SUBAGENT_BRIEF_TEMPLATE = """# 你的任务

- 角色：{label}
- 负责的缓存文件：{file}
- 负责的区间：{indexes}
- 主 Agent 的额外要求（**里面会写明这一遍写哪一类意见：校对建议 / 润色建议 / 两者都要**）：{brief}

负责的文件可能不止一个（自动均分出来的），逐个文件处理，别漏；读完你负责的区间，把发现的
问题写进对应条目的 proofread_comment，然后交报告。上面没写意见类型时，默认只写校对建议。"""

# 原文探索的任务说明：它不写文件，交的只有报告。
_SUBAGENT_EXPLORE_BRIEF = """# 你的任务

- 角色：{label}
- 负责的原文：{file}
- 主 Agent 的额外要求：{brief}

按上面的流程探索，最后交报告（不写任何文件，不要改字典、也不要改规范）。"""


# 原文探索子代理的 system prompt：**它读的是原文，不是译文**——目标是"字典还缺什么"与
# "翻译规范该注意什么"，产出只有一份报告（落地由主 Agent 做）。定位是 GenDic 的补充：
# 自动生成会漏掉的昵称、低频专有名词、口头称呼，靠通读原文找。
SUBAGENT_EXPLORE_PROMPT = """你是 GalTransl 的**原文探索子代理**，只干两件事：读**原文**、对照 **GPT 字典**，找出「字典里还缺什么」与「翻译规范该注意什么」，把结论写进最后那份报告。

# 权力边界（越界即失败）
- 你**只能读**两样东西：输入目录里的原文（list_input_files / read_input_file / search_input）与项目 GPT 字典（list_dict_files / read_dict）；
- 你**不写任何文件**，也看不到译文：字典与项目规范由主 Agent 汇总后落地。你的价值在"读得广、找得准"，不在动手改；
- 任务里点名了原文文件的话，你就**只负责点名给你的那些**（可能不止一个，自动均分出来的，逐个处理别漏）：list_input_files 只会列出它们，读别的文件会被拒；没点名才由你自己挑。

# 找什么
1. **GPT 字典的缺口（主要目标）**，尤其是自动生成（GenDic）会漏的那类：
   - 人名、**昵称 / 爱称 / 绰号**，以及"同一个人被叫好几个名字"的情况（给出判断为同一人的依据）；
   - 专有名词：地名、组织、道具、招式、设定名词；
   - 特殊称呼与亲属称谓（お兄ちゃん、先輩、〜様 这类）；
   - 口癖、自造词、以及假名同音容易翻错的词。
2. **翻译规范建议（次要目标）**：称谓与人称取舍（敬称是否保留、さん / ちゃん 怎么落）、文体与语气（书面或口语、口癖要不要译出）、标点与符号（「」是否保留、省略号、感叹号）、以及"哪些词必须统一译法"。

# 怎么干
1. 先 list_input_files 看有哪些原文文件，挑代表性的读——**别试图读完整个项目**，预算用完就停，按文件顺序来；
2. 用 read_input_file 读原文（index 从 1 开始，支持区间如 "1-100"）；判断只能基于原文本身与字典，你没有译文可参考；
3. 用 **search_input** 核对"某个词/称呼全篇出现过多少次、都在什么上下文、是不是同一个角色在用"（context=2~3 一起看上文）——收不收进字典、收哪个写法，靠的是这些次数与场景，别凭一次偶遇下结论；
4. 用 list_dict_files / read_dict 看 GPT 字典里**已经有什么**：只报没有的或写得不好的，重复的建议是噪音；
5. 提到一个词时把信息给准：原文写法、出现处（文件 / index）、大约出现多少次（search_input 的 total 就是）、为什么该收进字典。

# 收尾
不再调用工具后，输出一份**紧凑的结构化报告**（这是主 Agent 唯一会看到的你的输出，有长度上限）：
## 字典候选
原文 → 建议译法 ｜ 出现处 ｜ 一句话依据（一行一条，按重要性排序：影响理解的、出现多的排前面）
## 规范建议
一句话一条，写清"什么情况 → 怎么处理"，不要写"注意语气"这类空话
## 拿不准的
需要人来定的取舍（列出选项与各自的代价）

不要复述原文成段内容，不要写剧情概述，不要提议与字典和规范无关的东西。"""


# 子代理唯一能写的字段：校对批注。它用的就是主 Agent 那个 patch_transl_cache，只是入参 schema
# 被摘得只剩 proofread_comment、handler 那头也只放得住这一个字段（双保险）。
SUBAGENT_PATCHABLE_FIELDS: frozenset[str] = frozenset({"proofread_comment"})


@dataclass(frozen=True)
class SubAgentRole:
    """一个子代理角色的规格：提示词、任务说明模板、工具白名单、要不要锁缓存文件、报告上限。

    加角色 = 在 SUBAGENT_ROLES 里加一条并写好 prompt。**工具白名单是"能不能干这件事"的唯一
    依据**（不是提示词），所以每个角色的 tools 都要显式列全；白名单里出现的名字必须都在
    _TOOL_HANDLERS 里（有测试盯着）。
    """

    prompt: str
    brief: str
    tools: tuple[str, ...]
    # 是否必须锁定一个缓存文件（校对要，靠文件边界避免两个子代理写同一条；原文探索不需要）
    needs_file: bool = False
    report_chars: int = SUBAGENT_REPORT_CHARS


SUBAGENT_ROLES: dict[str, SubAgentRole] = {
    SUBAGENT_AGENT_PROOFREAD: SubAgentRole(
        prompt=SUBAGENT_PROOFREAD_PROMPT,
        brief=_SUBAGENT_BRIEF_TEMPLATE,
        tools=(
            "read_transl_cache",
            "search_transl_cache",
            "list_problems",
            "get_name_table",
            "read_guideline",
            "patch_transl_cache",
        ),
        needs_file=True,
    ),
    SUBAGENT_AGENT_EXPLORE: SubAgentRole(
        prompt=SUBAGENT_EXPLORE_PROMPT,
        brief=_SUBAGENT_EXPLORE_BRIEF,
        # 只有原文与 GPT 字典：不碰缓存（它看的是原文）、不碰规范（建议由主 Agent 合并时取舍），
        # 更没有任何写工具。search_input 给它"某个称呼全篇出现过几次、都在什么上下文"这类
        # 判断用——它要的正是"读得广"。
        tools=("list_input_files", "read_input_file", "search_input", "list_dict_files", "read_dict"),
        report_chars=SUBAGENT_EXPLORE_REPORT_CHARS,
    ),
}


def _subagent_role(agent: str) -> SubAgentRole:
    """取角色的规格；不认识的角色直接报错（入口处也校验一次，这里是兜底）。"""
    role = SUBAGENT_ROLES.get(agent)
    if role is None:
        raise AgentToolError(
            f"不认识的子代理角色：{agent!r}（可用：{'、'.join(SUBAGENT_ROLES)}）"
        )
    return role


def _subagent_patch_schema() -> dict[str, Any]:
    """子代理版的 patch_transl_cache：**把 pre_dst / proofread_dst 两个入参摘掉**，只留 proofread_comment。

    与 handler 侧的窄白名单（SUBAGENT_PATCHABLE_FIELDS）一起构成"改不了译文"的双保险——
    这件事由代码保证，不靠提示词自觉。schema 从主 Agent 那份深拷贝再改，避免哪天主 Agent
    换了描述、子代理这份漂移。
    """
    for tool in AGENT_TOOLS:
        function = tool.get("function") or {}
        if function.get("name") != "patch_transl_cache":
            continue
        copy: dict[str, Any] = json.loads(json.dumps(tool, ensure_ascii=False))
        properties = copy["function"]["parameters"]["properties"]["patches"]["items"]["properties"]
        for field in ("pre_dst", "proofread_dst"):
            properties.pop(field, None)
        copy["function"]["description"] = (
            "把你的意见写进缓存条目的 proofread_comment（校对批注），一条一个具体问题，"
            "写清问题在哪、该怎么改。可以一次传多条 patches。"
            "**写校对建议还是润色建议以任务说明为准**（没说明就默认只写校对建议）。"
            "**你是校对子代理，只能写 proofread_comment**——译文字段不在你的入参里，改由主 Agent 做；"
            "同一个 index 写第二次会覆盖上一次。"
        )
        return copy
    raise RuntimeError("AGENT_TOOLS 里找不到 patch_transl_cache")


def _subagent_tools(agent: str) -> list[dict[str, Any]]:
    """某个角色的工具表：从主 Agent 的工具表里按它的白名单挑，patch_transl_cache 换成收窄版。"""
    role = _subagent_role(agent)
    picked: list[dict[str, Any]] = []
    for tool in AGENT_TOOLS:
        name = str((tool.get("function") or {}).get("name") or "")
        if name not in role.tools:
            continue
        picked.append(_subagent_patch_schema() if name == "patch_transl_cache" else tool)
    return picked


# 认"锁定文件"的工具：任务里给了 file 时，这些工具的 filename 入参被限制在派给它的那些文件里。
# 搜索类（search_transl_cache / search_input）与 get_name_table 本来就是跨文件/跨项目的，不在
# 其中——子代理要核对"这个词在别处怎么翻的/原文里怎么说"，正是它们的用途。list_problems 没有
# filename 入参，改由 _subagent_handlers 传 allowed_files 收窄（见 _tool_list_problems）。
_LOCKED_FILENAME_TOOLS: tuple[str, ...] = (
    "read_transl_cache",
    "patch_transl_cache",
    "read_input_file",
)

# 任务里 file 填这个值 = 自动均分：本批里**同一角色**的每个 "*" 任务平分该角色的全部文件
# （校对=缓存文件，原文探索=原文文件）。例如派 16 个 "*"、项目有 256 个缓存文件 → 每个 16 个。
SUBAGENT_FILE_ALL = "*"


def _subagent_candidate_files(runner: AgentRunner, agent: str) -> list[str]:
    """某个角色"可分派的文件"清单（按名字排序：顺序稳定，均分结果可复现）。

    - 校对：缓存目录里的缓存文件（只认 `.json`）——跳过条目数为 0 的（没什么可校对，
      派过去等于白烧一个 agent 的 token）；
    - 原文探索：输入目录里的原文文件。
    """
    pid = runner._project_id()
    if agent == SUBAGENT_AGENT_PROOFREAD:
        data = runner._http_get(f"/api/projects/{pid}/cache")
        names: list[str] = []
        for item in data.get("files", []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if not name or not name.endswith(".json"):
                continue
            count = item.get("entry_count")
            if isinstance(count, int) and count <= 0:
                continue
            names.append(name)
        return sorted(names)
    cfg = urllib.parse.quote(runner.state.config_file_name or "config.yaml")
    data = runner._http_get(f"/api/projects/{pid}/files?counts=1&config={cfg}")
    return sorted(
        str(item.get("name") or "")
        for item in data.get("input_files", [])
        if isinstance(item, dict) and item.get("is_file", True) and item.get("name")
    )


def _split_files_evenly(files: list[str], parts: int) -> list[list[str]]:
    """把 files 按顺序均分成 parts 份（256 个文件 16 份 → 每份 16 个）。

    除不尽时**前面几份多一个**（10 个文件 3 份 → 4/3/3）：谁也不比谁多挨近一倍。
    文件比份数少时，后面几份是空的——调用方据此把这些任务丢掉（空跑一轮照样烧 token）。
    """
    if parts <= 0:
        return []
    base, extra = divmod(len(files), parts)
    groups: list[list[str]] = []
    start = 0
    for i in range(parts):
        size = base + (1 if i < extra else 0)
        groups.append(files[start : start + size])
        start += size
    return groups


def _lock_to_filenames(
    fn: Callable[[AgentRunner, dict[str, Any]], Any], allowed: tuple[str, ...]
) -> Callable[[AgentRunner, dict[str, Any]], Any]:
    """把工具的 filename 入参限制在 allowed 里：范围外一律拒绝，并说清这是本次派活的锁定范围。"""

    def scope_text() -> str:
        if len(allowed) == 1:
            return f"你只负责「{allowed[0]}」这一个文件（本次派活的锁定范围）"
        shown = "、".join(f"「{name}」" for name in allowed[:6])
        more = f" 等 {len(allowed)} 个文件" if len(allowed) > 6 else ""
        return f"你只负责这 {len(allowed)} 个文件（本次派活的锁定范围）：{shown}{more}"

    def wrapped(runner: AgentRunner, args: dict[str, Any]) -> Any:
        asked = str(args.get("filename", "") or "").strip()
        if asked in allowed:
            return fn(runner, args)
        if not asked:
            raise AgentToolError(f"{scope_text()}：这次没给 filename。")
        raise AgentToolError(f"{scope_text()}：「{asked}」不在范围里。要看别的文件，让主 Agent 重新派任务。")

    return wrapped


def _lock_input_listing(
    allowed: tuple[str, ...],
) -> Callable[[AgentRunner, dict[str, Any]], Any]:
    """list_input_files 在锁定模式下只列自己负责的那几份原文；一个都对不上就照实说。"""

    def wrapped(runner: AgentRunner, args: dict[str, Any]) -> Any:
        # 过滤交给 _list_input_payload 的 names（**先过滤再采样**）：自己过滤采样后的结果，
        # 会把"本来属于它、但被采样摇掉"的文件误判成"不在原文清单里"。
        out = _list_input_payload(
            runner,
            grep=_list_grep(args),
            limit=_list_limit(args),
            order=_list_order(args),
            names=allowed,
        )
        if not out.get("input_files"):
            # 锁定的名字一个都不在原文清单里（多半是文件名写错了）：照旧全列，但把话说清楚，
            # 免得它对着空清单发懵
            full = _list_input_payload(
                runner, grep=_list_grep(args), limit=_list_limit(args), order=_list_order(args)
            )
            return {
                **full,
                "note": (
                    "注意：本次任务锁定的文件都不在原文清单里（检查一下文件名），"
                    "下面是全部原文文件。"
                ),
            }
        if len(allowed) == 1:
            note = (
                f"本次只派你看「{allowed[0]}」这一个文件，其余文件不在你的范围里"
                "（要处理别的文件，让主 Agent 重新派任务）。"
            )
        else:
            # 报的是**职责范围**（count）而不是这一屏显示了几行（returned）：采样只管显示
            note = (
                f"本次只派你看这 {out['count']} 个文件，其余文件不在你的范围里"
                "（要处理别的文件，让主 Agent 重新派任务）。"
            )
        return {**out, "note": "；".join([out["note"], note]) if out.get("note") else note}

    return wrapped


def _subagent_handlers(
    agent: str, locked_files: str | Sequence[str] = ()
) -> dict[str, Callable[[AgentRunner, dict[str, Any]], Any]]:
    """某个角色可用的 handler（按它的白名单从主 Agent 那张表里取）。

    - patch_transl_cache 包一层窄白名单：只放得住 proofread_comment（译文字段即使模型硬塞也会被
      当成"无可更新字段"跳过，并被回一条只允许 proofread_comment 的工具错误）；
    - **锁定文件**（locked_files 非空，来自任务里的 file；自动均分时是一组）：read_transl_cache /
      patch_transl_cache / read_input_file 的 filename 被限制在这一组里，list_input_files 也只列
      这些，list_problems 也只列这些文件的问题——"一份文件只归一个子代理"由工具层保证，模型串到
      范围外会被拒（省 token，也避免两个子代理写同一条）。要核对"这个词在别处怎么翻的"，仍走
      跨文件的 search_transl_cache / get_name_table；
    - 调用**不过权限门禁**：子代理的工具集本身就是白名单（只有读，加校对那一支写意见），
      一批 16 个逐条弹审批卡会把界面淹掉。改译文的权力仍然只在主 Agent 手上——那才是要审批的事。
    """
    role = _subagent_role(agent)
    handlers: dict[str, Callable[[AgentRunner, dict[str, Any]], Any]] = {
        name: _TOOL_HANDLERS[name] for name in role.tools if name in _TOOL_HANDLERS
    }
    if "patch_transl_cache" in role.tools:
        handlers["patch_transl_cache"] = lambda runner, args: _tool_patch_transl_cache(
            runner, args, SUBAGENT_PATCHABLE_FIELDS
        )
    # 裸字符串按"一个文件名"处理（Sequence[str] 会把字符串按字符拆开——那是陷阱不是功能）
    locked_names: Sequence[str] = (locked_files,) if isinstance(locked_files, str) else locked_files
    locked = tuple(str(name).strip() for name in locked_names if str(name).strip())
    if locked:
        for name in _LOCKED_FILENAME_TOOLS:
            if name in handlers:
                handlers[name] = _lock_to_filenames(handlers[name], locked)
        if "list_input_files" in handlers:
            handlers["list_input_files"] = _lock_input_listing(locked)
        if "list_problems" in handlers:
            handlers["list_problems"] = lambda runner, args: _tool_list_problems(runner, args, locked)
    return handlers


def _subagent_chat(
    client: Any, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
) -> tuple[str, list[Any], str, str]:
    """子代理的一次请求（**非流式**）：返回（正文, 工具调用, 思考字段名, 思考内容）。

    非流式是刻意的简化：子代理的中间输出不需要逐字上屏，一轮一次拿全更简单。代价是"停止"
    要等当前这次请求回来才生效（父回合的停止仍会立刻终止它后续的轮次）。
    思考字段的约定与主 Agent 一致（见 REASONING_FIELD_NAMES）：带 tools 的多轮对话里，
    DeepSeek 这类 provider 要求把上一轮的 reasoning 原样回传，不回就 400。

    tools=None 用于压缩那一轮：整个字段不发出（而不是发 null——有些兼容端点不认），
    模型因此没有"接着调工具"的选项（见 SubAgentRunner._begin_compaction）。
    """
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "timeout": _llm_timeout(),
    }
    if tools is not None:
        kwargs["tools"] = tools
    resp = client.chat.completions.create(**kwargs)
    choices = getattr(resp, "choices", None) or []
    message = getattr(choices[0], "message", None) if choices else None
    if message is None:
        return "", [], REASONING_FIELD_NAMES[0], ""
    content = str(getattr(message, "content", "") or "")
    tool_calls = list(getattr(message, "tool_calls", None) or [])
    field, reasoning = "", ""
    for name in REASONING_FIELD_NAMES:
        value = getattr(message, name, None)
        if value:
            field, reasoning = name, str(value)
            break
    return content, tool_calls, field, reasoning


def _truncate_text(text: str, limit: int, hint: str = "") -> str:
    """超长截断（给子代理的上下文用）：截断要明说，否则它会以为读全了。"""
    if len(text) <= limit:
        return text
    return text[:limit] + (hint or f"…（已截断，共 {len(text)} 字符）")


# 子代理的压缩指令（Insert-then-Compress 的那条瞬时消息，见 SubAgentRunner._begin_compaction）。
# 与主 Agent 那份（COMPACT_INSTRUCTION_PROMPT）的差别：子代理任务单一，而且压缩那一轮**不带
# tools**——它没有"接着调工具"的余地，所以不必像主 Agent 那样反复强调"不要执行上面的请求"。
# 输出同样用 <summary> 包住，与主 Agent 共用 _parse_compact_summary 解析。
SUBAGENT_COMPACT_INSTRUCTION_PROMPT = """[记忆压缩模式] 上面的工作已经告一段落。现在不要继续任务，把它压缩成一份摘要，供你在后续（换了一段上下文之后）接着做同一件事。严格执行：
1. 只输出摘要，不要调用工具、不要接着干活；
2. 正文用 <summary>…</summary> 包住，按下面的骨架写：
<summary>
## 任务与范围
（你负责的文件 / index 区间、目标）
## 已完成
（读过哪些区间、做了什么、写下了哪些意见或发现）
## 关键发现
（逐条列：文件名 / index / 原文写法 / 译名 / 结论——这些硬信息必须原样保留，不要概括掉）
## 待办
（还没读的区间、需要复查或拿不准的点）
</summary>
3. 用中文，简洁但不丢信息。"""


class SubAgentRunner:
    """一个子代理实例：自己的消息、自己的工具表，跑完交一份报告。

    一个实例只被一个线程跑（见 _tool_run_subagents 的线程池），所以内部不需要加锁。
    消息历史超窗口时与父 Agent 走同一套压缩（Insert-then-Compress：挂上压缩指令、用下一轮
    请求把摘要拿回来，见 _begin_compaction），避免 24 轮工具往返把上下文撑爆。
    """

    def __init__(
        self,
        parent: AgentRunner,
        *,
        agent: str,
        files: Sequence[str],
        indexes: str,
        brief: str,
        delegation_id: str,
    ) -> None:
        self.parent = parent
        self.agent = agent
        self.role = _subagent_role(agent)  # 提示词 / 工具白名单 / 报告上限都从它取
        # 派给它的文件（一个或一组；自动均分时是一组）。工具层按它限范围，见 _subagent_handlers
        self.files: tuple[str, ...] = tuple(str(name).strip() for name in files if str(name).strip())
        self.indexes = indexes
        self.brief = brief
        self.id = delegation_id
        self.messages: list[dict[str, Any]] = []
        self.turns = 0
        self.tool_calls = 0
        self.doubts: list[dict[str, Any]] = []
        self.started_at = time.time()
        # 正在进行的一次压缩：{cut, head_keep, estimated, limit}。挂上压缩指令后置上，
        # 收尾（_finish_compaction）或回滚（_abort_compaction）时清空。
        self._pending_compaction: dict[str, Any] | None = None
        # 压缩整条路子都失败过（插入式 + 独立请求都没压成）：不再重试，否则每轮都白跑一次
        self._compact_failed = False

    @property
    def file_label(self) -> str:
        """行上/结果里显示用的一行：单个就是文件名，一组是「首个 等 N 个文件」（完整清单在 files）。"""
        if not self.files:
            return ""
        if len(self.files) == 1:
            return self.files[0]
        return f"{self.files[0]} 等 {len(self.files)} 个文件"

    @property
    def file_list_text(self) -> str:
        """任务说明里给子代理看的**完整**清单——它得知道自己负责哪些（没有列表类工具可查）。"""
        return "、".join(self.files) if self.files else "全部（自己按清单挑）"

    def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        """子代理事件：一律带自己的 id，界面据此挂到发起它的那行下面。"""
        self.parent._emit(event_type, {"id": self.id, **data})

    def _finish(self, status: str, report: str, error: str = "") -> dict[str, Any]:
        text = report.strip()
        limit = self.role.report_chars
        if len(text) > limit:
            text = text[:limit] + "…（报告已截断）"
        result: dict[str, Any] = {
            "id": self.id,
            "agent": self.agent,
            "label": SUBAGENT_LABELS.get(self.agent, self.agent),
            # file 是给人看的一行（一组时是「首个 等 N 个」），files 是完整清单——
            # 主 Agent 后面要按文件去读/清 proofread_comment，必须有准名字
            "file": self.file_label,
            "files": list(self.files),
            "indexes": self.indexes,
            "status": status,
            "report": text,
            "turns": self.turns,
            "tool_calls": self.tool_calls,
            # 只回 文件 + index：意见全文在缓存里，主 Agent 需要细节就读那几条。
            # 带上 file 是因为一个子代理可能负责一组文件，只给 index 认不出是哪一份里的。
            "doubts": [{"file": d.get("file", ""), "index": d.get("index")} for d in self.doubts],
            "duration_ms": int((time.time() - self.started_at) * 1000),
        }
        if error:
            result["error"] = error
        self._emit(
            "subagent_done",
            {
                "status": status,
                "report": text,
                "turns": self.turns,
                "tool_calls": self.tool_calls,
                "doubts": len(self.doubts),
                "duration_ms": result["duration_ms"],
                # 结束时间戳（Unix 秒）：与 started_at 配对，界面重放后也能还原区间
                "finished_at": time.time(),
                "error": error,
            },
        )
        return result

    # ---- 上下文窗口复用与压缩 ----

    def _parent_context_window(self) -> int:
        """复用父 Agent 解析出的上下文窗口：同一个后端配置，子代理没理由再解析一遍。"""
        try:
            window = int(getattr(self.parent, "_context_window", 0) or 0)
        except (TypeError, ValueError):
            window = 0
        return window or DEFAULT_CONTEXT_WINDOW

    def _estimate_context_tokens(self) -> int:
        """估算当前历史占用的 token（子代理无 usage 锚点，纯字符估算；见 _estimate_usage_tokens）。"""
        return _estimate_usage_tokens(self.messages)

    def _begin_compaction(self) -> bool:
        """历史超窗口就挂上压缩指令，让**下一轮请求**顺带把摘要拿回来（Insert-then-Compress）。

        与父 Agent 同一机制、同一套阈值（父的窗口 + COMPACT_TRIGGER_RATIO + 尾部预留），
        差别只有三处：
        - 头部 2 条（system 提示词 + 任务说明）永远保留——子代理没有别的途径知道"我是谁、
          负责哪些文件"；
        - 保留段按 token 预算挑，预算取父会话的一半（SUBAGENT_COMPACT_KEEP_RECENT_RATIO）；
        - 压缩那一轮**不带 tools**，它没有"接着调工具"的余地，也就没有父 Agent 那条
          "模型回了工具调用就判失败"的分支。
        """
        if self._pending_compaction is not None or self._compact_failed:
            return False
        window = self._parent_context_window()
        limit = int(window * COMPACT_TRIGGER_RATIO) - CONTEXT_RESERVE_TOKENS
        if limit <= 0:
            return False
        estimated = self._estimate_context_tokens()
        if estimated <= limit:
            return False
        head_keep = 2
        keep_recent = _keep_recent_tokens(limit, SUBAGENT_COMPACT_KEEP_RECENT_RATIO)
        cut = _find_compaction_cut(self.messages[head_keep:], keep_recent)
        if cut <= 0:
            # 找不到安全切点：别每轮都重算一遍（父 Agent 的 _compact_failed_this_turn 同理）
            self._compact_failed = True
            return False
        cut += head_keep
        # 指令只进内存、不落盘；它也**不会**被写进摘要或报告（收尾时弹掉）
        self.messages.append({"role": "user", "content": SUBAGENT_COMPACT_INSTRUCTION_PROMPT})
        self._pending_compaction = {
            "cut": cut,
            "head_keep": head_keep,
            "estimated": estimated,
            "limit": limit,
        }
        return True

    def _abort_compaction(self) -> None:
        """压缩没成：把那条瞬时指令弹掉，历史回到原样。"""
        if self._pending_compaction is None:
            return
        self._pending_compaction = None
        if self.messages:
            self.messages.pop()

    def _run_compaction_request(self, client: Any, model: str) -> None:
        """发出带压缩指令的那轮请求、收下摘要；失败退回独立摘要请求。

        这轮**不带 tools**（见 _begin_compaction），拿到的正文按 <summary> 解析。整条路子
        （插入式 → 独立请求 → 本地兜底）都不会让子代理卡死。
        """
        pending = self._pending_compaction or {}
        cut = pending.get("cut")
        try:
            content, _, _, _ = self._chat_with_retry(client, model, None)
        except AgentStopRequested:
            self._abort_compaction()  # 父回合被停止：历史不必留着那条指令
            raise
        except Exception as exc:  # noqa: BLE001 - 压缩失败不能拖垮子代理
            _log(f"  ⚠ 子代理 {self.id} 压缩请求失败（{exc}），回退独立摘要请求")
            self._abort_compaction()
            self._compact_via_separate_request(cut)
            return
        if not self._finish_compaction(content):
            _log(f"  ⚠ 子代理 {self.id} 压缩响应里没有摘要，回退独立摘要请求")
            self._compact_via_separate_request(cut)

    def _finish_compaction(self, content: str) -> bool:
        """摘要到手 → 弹掉指令、按切点重建消息列表。"""
        pending = self._pending_compaction
        if pending is None:
            return False
        summary = _parse_compact_summary(content)
        if not summary.strip():
            self._abort_compaction()
            return False
        self._pending_compaction = None
        self.messages.pop()  # 那条压缩指令不是历史
        self._apply_summary(int(pending["head_keep"]), int(pending["cut"]), summary)
        return True

    def _compact_via_separate_request(self, cut: int | None = None) -> None:
        """降级路径：另发一次独立摘要请求（复用父 Agent 的 _summarize_messages，不带 tools）。

        插入式那轮请求失败、或模型没给出摘要时走它；摘要再失败还有 _local_fallback_summary
        收底。切点仍由 _find_compaction_cut 保证 tool_calls 与 tool 响应成对，不会切出非法请求。
        """
        head_keep = 2
        if cut is None:
            limit = int(self._parent_context_window() * COMPACT_TRIGGER_RATIO) - CONTEXT_RESERVE_TOKENS
            keep_recent = _keep_recent_tokens(limit, SUBAGENT_COMPACT_KEEP_RECENT_RATIO)
            cut = _find_compaction_cut(self.messages[head_keep:], keep_recent)
            if cut <= 0:
                self._compact_failed = True
                return
            cut += head_keep
        head = self.messages[head_keep:cut]
        summary = ""
        summarizer = getattr(self.parent, "_summarize_messages", None)
        if callable(summarizer):
            try:
                summary = str(summarizer(head) or "")
            except Exception as exc:  # noqa: BLE001 - 摘要失败必须降级，不能卡死子代理
                _log(f"  ⚠ 子代理 {self.id} 摘要失败，回退本地截断: {exc}")
        if not summary.strip():
            summary = _local_fallback_summary(head)
        self._apply_summary(head_keep, cut, summary)

    def _apply_summary(self, head_keep: int, cut: int, summary: str) -> None:
        """按切点重建消息列表：头部 head_keep 条 + 一条摘要 + 尾部原文。

        被裁掉的旧消息直接丢掉——子代理跑完只交一份报告，中间过程不需要召回（与主 Agent
        的 read_history_archive 不同，它没有"回头查旧账"的需求）。
        """
        dropped = cut - head_keep
        tail = self.messages[cut:]
        self.messages = [
            *self.messages[:head_keep],
            {
                "role": "user",
                "content": (
                    "# 早前工作的压缩摘要\n"
                    f"{summary}\n\n"
                    "以上是之前工作的压缩摘要，请在此基础上继续，不要重复已完成的工作。"
                ),
            },
            *tail,
        ]
        # 子代理的每一步都会推给界面（subagent_message 渲染成一步说明）：压缩这种状态变化
        # 也让它看得见，否则展开子代理会发现步数突然对不上
        self._emit("subagent_message", {
            "round": self.turns,
            "text": f"[上下文压缩] 早前的 {dropped} 条消息已压成摘要（{len(summary)} 字符），从摘要继续。",
        })
        _log(f"  📦 子代理 {self.id} 压缩 {dropped} 条为 {len(summary)} 字符摘要")

    def _chat_with_retry(
        self, client: Any, model: str, tools: list[dict[str, Any]] | None
    ) -> tuple[str, list[Any], str, str]:
        """一次请求 + 与主 Agent 同规则的重试（分类 / 退避 / 可被停止打断）。

        子代理的一次请求就是它整份报告的全部依赖，一次网络抖动就作废太亏，而且这里的
        用量比主请求小得多，重试代价低。规则与主 Agent 一致：只重试瞬态错误（超时 /
        限流 / 5xx / 流连接断了）；鉴权、参数、上下文超限重试多少次都一样，直接失败。
        退避期间父回合被停止就立刻收尾（抛 AgentStopRequested，由 run 转成 stopped）。
        tools=None 是压缩那一轮：不带工具，只求一段文字摘要（见 _begin_compaction）。
        """
        attempt = 0
        while True:
            try:
                return _subagent_chat(client, model, self.messages, tools)
            except Exception as exc:  # noqa: BLE001 - 按分类决定是否重试
                info = _classify_llm_error(exc)
                if self.parent.stop_event.is_set():
                    raise AgentStopRequested() from exc
                if not info["retriable"] or attempt >= LLM_MAX_RETRIES:
                    raise
                attempt += 1
                delay_ms = _llm_retry_delay_ms(attempt, info)
                _log(
                    f"  🧑‍🎓 子代理 {self.id} 请求失败（{info['code']}: {info['message']}），"
                    f"{delay_ms / 1000:g}s 后重试 {attempt}/{LLM_MAX_RETRIES}"
                )
                self._emit("subagent_retry", {
                    "attempt": attempt,
                    "max_attempts": LLM_MAX_RETRIES,
                    "delay_ms": delay_ms,
                    "code": info["code"],
                    "reason": info["message"],
                    "status": info["status"],
                })
                if self.parent.stop_event.wait(delay_ms / 1000):
                    raise AgentStopRequested() from exc

    def run(self) -> dict[str, Any]:
        """跑到自然收尾（不再调工具）、轮数上限、失败或被停止。"""
        client = getattr(self.parent, "_openai_client", None)
        model = str(getattr(self.parent, "_model", "") or "")
        label = SUBAGENT_LABELS.get(self.agent, self.agent)
        self._emit(
            "subagent_start",
            {
                "parent_id": self.parent._active_tool_call_id,
                "agent": self.agent,
                "label": label,
                "file": self.file_label,  # 一组时是「首个 等 N 个文件」，完整清单只在子代理的任务说明里
                "indexes": self.indexes,
                "brief": self.brief,
                "model": model,
                # 开始时间戳（Unix 秒）：subagent_start 是持久事件，刷新/切页后会重放，
                # 界面必须按它算"进行中耗时"，不能拿事件到达时间——否则每次重建都归零。
                "started_at": self.started_at,
            },
        )
        if client is None or not model:
            return self._finish("failed", "", error="主 Agent 的后端还没就绪，子代理起不来")
        self.messages = [
            {"role": "system", "content": self.role.prompt},
            {
                "role": "user",
                "content": self.role.brief.format(
                    label=label,
                    file=self.file_list_text,
                    indexes=self.indexes or "全部",
                    brief=self.brief or "（无）",
                ),
            },
        ]
        tools = _subagent_tools(self.agent)
        # 锁定文件：把它交给 handler，让"只能碰派给自己的那些"由工具层保证
        handlers = _subagent_handlers(self.agent, self.files)
        last_text = ""
        for round_i in range(1, SUBAGENT_MAX_ROUNDS + 1):
            self.turns = round_i
            if self.parent.stop_event.is_set():
                return self._finish("stopped", last_text, error="父回合被停止，子代理提前收尾")
            # 每轮请求前判一次：工具往返堆太多就先压缩——挂上压缩指令、这一轮专门拿摘要
            # （Insert-then-Compress，与父 Agent 同一机制），下一轮带着摘要继续干活。
            if self._begin_compaction():
                self._run_compaction_request(client, model)
                continue
            try:
                content, tool_calls, reasoning_field, reasoning = self._chat_with_retry(
                    client, model, tools
                )
            except AgentStopRequested:
                return self._finish("stopped", last_text, error="父回合被停止，子代理提前收尾")
            except Exception as exc:  # noqa: BLE001 - 子代理失败不该拖垮父回合
                _log(f"  🧑‍🎓 子代理 {self.id} 第 {round_i} 轮请求失败（已重试到上限）: {exc}")
                return self._finish(
                    "failed", last_text, error=f"请求失败（已重试 {LLM_MAX_RETRIES} 次）：{exc}"
                )
            if content.strip():
                last_text = content
                self._emit("subagent_message", {"round": round_i, "text": content[:2000]})
            if not tool_calls:
                return self._finish("done", content or last_text)
            assistant: dict[str, Any] = {"role": "assistant", "content": content or ""}
            if reasoning:
                assistant[reasoning_field] = reasoning
            assistant["tool_calls"] = tool_calls
            self.messages.append(assistant)
            for tool_call in tool_calls:
                self.messages.append(self._run_tool(tool_call, handlers))
        return self._finish(
            "max_rounds", last_text, error=f"轮数到上限（{SUBAGENT_MAX_ROUNDS}），按已有结果收尾"
        )

    def _run_tool(self, tool_call: Any, handlers: dict[str, Any]) -> dict[str, Any]:
        """执行一次工具调用（白名单之外一律拒绝），返回要塞回消息历史的那条 tool 消息。"""
        call_id = str(getattr(tool_call, "id", "") or "")
        fn = getattr(tool_call, "function", None)
        name = str(getattr(fn, "name", "") or "")
        raw = str(getattr(fn, "arguments", "") or "")
        self.tool_calls += 1
        try:
            parsed = json.loads(raw or "{}")
        except json.JSONDecodeError:
            parsed = {}
        args = parsed if isinstance(parsed, dict) else {}
        self._emit(
            "subagent_tool_call",
            {"tool_call_id": call_id, "name": name, "arguments": _sanitize_tool_args(args)},
        )
        handler = handlers.get(name)
        started = time.time()
        ok = True
        result: Any = None
        try:
            if handler is None:
                raise AgentToolError(
                    f"子代理没有这个工具：{name}（只能用 {'、'.join(self.role.tools)}）"
                )
            result = handler(self.parent, args)
            # 子代理读缓存是大头（校对一批 16 个、每个几十条）：同样走 Markdown 渲染
            rendered = _render_tool_result_table(name, result)
            payload = rendered if rendered is not None else json.dumps(result, ensure_ascii=False)
        except AgentToolError as exc:
            payload, ok = str(exc), False
        except Exception as exc:  # noqa: BLE001
            payload, ok = f"{type(exc).__name__}: {exc}", False
        duration_ms = int((time.time() - started) * 1000)
        if ok and name == "patch_transl_cache":
            self._remember_doubts(result, str(args.get("filename", "") or ""))
        # 事件里只给预览（读缓存动辄几万字符，界面用不上）；消息历史里给全文（有上限兜底）
        self._emit(
            "subagent_tool_result",
            {
                "tool_call_id": call_id,
                "name": name,
                "ok": ok,
                "result": payload[:400] if ok else None,
                "error": None if ok else payload[:400],
                "duration_ms": duration_ms,
            },
        )
        return {
            "role": "tool",
            "tool_call_id": call_id,
            "content": _truncate_text(
                payload,
                SUBAGENT_TOOL_RESULT_CHARS,
                "…（结果过长已截断：请用 index 区间分段读，别跳过没读的条目）",
            ),
        }

    def _remember_doubts(self, result: Any, filename: str) -> None:
        """从 patch_transl_cache 的变更里挑出 proofread_comment 那几条，记进报告用的小结。

        认的是返回的 changes（path 形如 `#33.proofread_comment`）而不是模型传的参数：它到底写了什么、
        写没写成功，以工具的返回为准。filename 一并记下：一个子代理可能负责一组文件，只留 index
        的话主 Agent 认不出这条意见在哪份文件里。
        """
        if not isinstance(result, dict):
            return
        for change in result.get("changes") or []:
            if not isinstance(change, dict):
                continue
            path = str(change.get("path") or "")
            if not path.endswith(".proofread_comment"):
                continue
            raw = path.split(".")[0].lstrip("#")
            try:
                index: Any = int(raw)
            except ValueError:
                index = raw
            self.doubts.append(
                {"file": filename, "index": index, "content": str(change.get("after") or "")}
            )


def _split_summary(agent: str, total: int, parts: int, sizes: list[int]) -> str:
    """自动均分后给主 Agent 的一句交代（分了多少、每个多少、有没有任务被跳过）。"""
    label = SUBAGENT_LABELS.get(agent, agent)
    non_empty = [size for size in sizes if size]
    span = str(non_empty[0]) if len(set(non_empty)) == 1 else f"{min(non_empty)}-{max(non_empty)}"
    text = f"已把 {total} 个文件自动均分给 {parts} 个「{label}」子代理（每个 {span} 个）"
    skipped = parts - len(non_empty)
    if skipped:
        text += f"；有 {skipped} 个任务没分到文件，已跳过（少派一个就少烧一份 token）"
    return text + "。"


def _tool_run_subagents(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """派一批子代理并行干活，等它们全部跑完，把每份报告收回来。

    文件怎么分：
    - 写具体文件名：这个任务就锁定这一份（或一组）。文件是任务的天然边界——不同子代理写不同
      文件的 proofread_comment 不会互相覆盖；同一个文件要拆就用 indexes 切区间，但两边别碰同一条。
    - 写 "*"（SUBAGENT_FILE_ALL）：**自动均分**——本批里同角色的每个 "*" 任务平分该角色的全部
      文件（校对=缓存文件，原文探索=原文文件）。派 16 个 "*"、项目 256 个缓存文件 → 每个 16 个；
      除不尽时前面的多一个。已经在别处点名过的文件不会再分给 "*"。
    - 原文探索的 file 可以留空（自己按 list_input_files 挑）：它只读、不写文件。
    """
    tasks_raw = args.get("tasks")
    if not isinstance(tasks_raw, list) or not tasks_raw:
        raise AgentToolError("tasks 必须是非空数组")
    if len(tasks_raw) > SUBAGENT_MAX_TASKS:
        raise AgentToolError(
            f"一次最多派 {SUBAGENT_MAX_TASKS} 个子代理（收到 {len(tasks_raw)} 个）：拆成两次调用。"
        )
    tasks: list[dict[str, Any]] = []
    for i, item in enumerate(tasks_raw, start=1):
        if not isinstance(item, dict):
            raise AgentToolError(f"第 {i} 个任务不是对象")
        agent = str(item.get("agent", "") or "").strip()
        if agent not in SUBAGENT_AGENTS:
            raise AgentToolError(
                f"第 {i} 个任务的 agent 不认识：{agent!r}（可用：{'、'.join(SUBAGENT_AGENTS)}）"
            )
        file_name = str(item.get("file", "") or "").strip()
        if SUBAGENT_ROLES[agent].needs_file and not file_name:
            raise AgentToolError(
                f"第 {i} 个任务缺 file：{SUBAGENT_LABELS.get(agent, agent)} 要锁定一个缓存文件"
                f'（也可以填 "{SUBAGENT_FILE_ALL}" 让它自动均分一批）'
            )
        is_auto = file_name == SUBAGENT_FILE_ALL
        # count：把这一条任务展开成几个子代理并行跑。brief 只写一遍——不让模型为了并行
        # 把上千字的 brief 复制 N 份，否则它宁可只派一个（真实踩过：要求派 2 个 explore，
        # 模型因为不想重复长 brief 只写了一条 task）。
        count_raw = item.get("count", 1)
        if isinstance(count_raw, bool) or not isinstance(count_raw, (int, str)):
            raise AgentToolError(f"第 {i} 个任务的 count 必须是整数")
        try:
            count = int(count_raw)
        except ValueError as exc:
            raise AgentToolError(f"第 {i} 个任务的 count 必须是整数（收到 {count_raw!r}）") from exc
        if count < 1:
            raise AgentToolError(f"第 {i} 个任务的 count 至少是 1")
        if count > SUBAGENT_MAX_TASKS:
            raise AgentToolError(
                f"第 {i} 个任务的 count 最多 {SUBAGENT_MAX_TASKS}（收到 {count}）"
            )
        if count > 1 and not is_auto:
            raise AgentToolError(
                f"第 {i} 个任务的 file 是具体文件名（{file_name}），没法平分给 {count} 个子代理："
                f'要并行就把 file 填 "{SUBAGENT_FILE_ALL}" 交给自动均分，或者拆成几条各写一个文件名'
            )
        for _ in range(count):
            tasks.append(
                {
                    "agent": agent,
                    "file": file_name,
                    # 解析后的实际范围：具体文件名 → 就它一个；"*" → 下面均分填进来
                    "files": [] if is_auto else ([file_name] if file_name else []),
                    "auto": is_auto,
                    "indexes": str(item.get("indexes", "") or "").strip(),
                    "brief": str(item.get("brief", "") or "").strip(),
                }
            )
    if len(tasks) > SUBAGENT_MAX_TASKS:
        raise AgentToolError(
            f"展开 count 后一次要派 {len(tasks)} 个子代理，超过上限 {SUBAGENT_MAX_TASKS}："
            "调小 count，或分两次调用。"
        )

    # 自动均分：候选 = 该角色全部文件 - 本批里已点名过的（点名优先，避免两个子代理抢同一份）
    auto_slots: dict[str, list[int]] = {}
    named: dict[str, set[str]] = {}
    for idx, task in enumerate(tasks):
        if task["auto"]:
            auto_slots.setdefault(task["agent"], []).append(idx)
        elif task["files"]:
            named.setdefault(task["agent"], set()).add(task["files"][0])
    split_notes: list[str] = []
    for agent, idxs in auto_slots.items():
        pool = [
            name
            for name in _subagent_candidate_files(runner, agent)
            if name not in named.get(agent, set())
        ]
        if not pool:
            hint = (
                "缓存里还没有可校对的文件（条目为空或还没跑翻译）：先用 list_transl_cache 看看，"
                "或先把具体文件名写出来。"
                if agent == SUBAGENT_AGENT_PROOFREAD
                else "输入目录里没有可分派的原文文件：先用 list_input_files 看看。"
            )
            raise AgentToolError(f"「{SUBAGENT_LABELS.get(agent, agent)}」自动均分拿不到文件：{hint}")
        groups = _split_files_evenly(pool, len(idxs))
        for idx, group in zip(idxs, groups):
            tasks[idx]["files"] = group
        split_notes.append(_split_summary(agent, len(pool), len(idxs), [len(g) for g in groups]))

    # 没分到文件的任务直接丢掉（空跑一轮照样烧 token），但"file 留空"的探索任务照旧派出
    scheduled = [task for task in tasks if task["files"] or not task["auto"]]
    skipped = len(tasks) - len(scheduled)
    tasks = scheduled

    if getattr(runner, "_openai_client", None) is None or not str(getattr(runner, "_model", "") or ""):
        raise AgentToolError("本回合的后端还没就绪，子代理跑不起来")

    slots: list[dict[str, Any] | None] = [None] * len(tasks)
    lock = threading.Lock()
    base = os.urandom(8).hex()

    def work(slot: int, task: dict[str, Any]) -> None:
        try:
            sub = SubAgentRunner(
                runner,
                agent=task["agent"],
                files=task["files"],
                indexes=task["indexes"],
                brief=task["brief"],
                delegation_id=f"{base}-{slot + 1:02d}",
            )
            out = sub.run()
        except Exception as exc:  # noqa: BLE001 - 子代理自己崩了只影响它这一格
            _log(f"  🧑‍🎓 子代理 {slot + 1} 起不来或崩了: {exc}")
            out = {
                "agent": task["agent"],
                "label": SUBAGENT_LABELS.get(task["agent"], task["agent"]),
                "file": "、".join(task["files"][:1]) + (
                    f" 等 {len(task['files'])} 个文件" if len(task["files"]) > 1 else ""
                ),
                "files": list(task["files"]),
                "indexes": task["indexes"],
                "status": "failed",
                "report": "",
                "turns": 0,
                "tool_calls": 0,
                "doubts": [],
                "duration_ms": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }
        with lock:
            slots[slot] = out

    threads = [
        threading.Thread(target=work, args=(i, task), name=f"subagent-{base}-{i + 1}", daemon=True)
        for i, task in enumerate(tasks)
    ]
    def _dispatch_label(task: dict[str, Any]) -> str:
        """日志里每个子代理的叫法：一组文件是「首个 等 N 个」，没分配文件的就是角色名。"""
        if len(task["files"]) > 1:
            return f"{task['files'][0]} 等 {len(task['files'])} 个文件"
        if task["files"]:
            return str(task["files"][0])
        return str(SUBAGENT_LABELS.get(task["agent"], task["agent"]))

    _log(
        f"  🧑‍🎓 派出 {len(threads)} 个子代理："
        + "、".join(_dispatch_label(task) for task in tasks)
    )
    for thread in threads:
        thread.start()
    started = time.time()
    while any(thread.is_alive() for thread in threads):
        if runner.stop_event.is_set():
            # 停止信号：各子代理在自己的轮次边界退出，这里不再死等
            _log("  🧑‍🎓 父回合被停止，等待子代理收尾")
            for thread in threads:
                thread.join(timeout=2.0)
            break
        # 以 0.1s 为步长轮询，而不是直接 sleep(5)：子代理一跑完就收尾，别让父回合
        # 白等一个 tick——只派一个、或最后一个刚跑完时，那 5 秒是纯浪费。
        # SUBAGENT_PROGRESS_TICK 只用来控制进度日志的节奏。
        deadline = time.time() + SUBAGENT_PROGRESS_TICK
        while time.time() < deadline and any(t.is_alive() for t in threads):
            if runner.stop_event.is_set():
                break
            time.sleep(0.1)
        if not any(thread.is_alive() for thread in threads):
            break
        alive = sum(1 for thread in threads if thread.is_alive())
        _log(f"  🧑‍🎓 子代理并行中：还剩 {alive}/{len(threads)} 个（已 {int(time.time() - started)}s）")

    results: list[dict[str, Any]] = []
    for task, out in zip(tasks, slots):
        if out is None:
            out = {
                "agent": task["agent"],
                "label": SUBAGENT_LABELS.get(task["agent"], task["agent"]),
                "file": "、".join(task["files"][:1]) + (
                    f" 等 {len(task['files'])} 个文件" if len(task["files"]) > 1 else ""
                ),
                "files": list(task["files"]),
                "indexes": task["indexes"],
                "status": "stopped",
                "report": "",
                "turns": 0,
                "tool_calls": 0,
                "doubts": [],
                "duration_ms": 0,
                "error": "父回合被停止，这个子代理没跑完",
            }
        results.append(out)
    total_doubts = sum(len(row.get("doubts") or []) for row in results)
    note = (
        "子代理的校对意见已写进各条缓存的 proofread_comment：按上面每项的 file 与 index 用 "
        "read_transl_cache 读那些条目，改完译文（pre_dst）后再用 patch_transl_cache 把该条的 "
        "proofread_comment 清空，表示已处理。"
    )
    if total_doubts == 0:
        note = "这批子代理没有提出任何疑问（没有条目被写入 proofread_comment）。"
    if skipped:
        note += f" 另有 {skipped} 个任务因文件不够分被跳过，实际派出 {len(results)} 个。"
    if split_notes:
        note = " ".join(split_notes) + " " + note
    return {
        "tasks": results,
        "total": len(results),
        "skipped": skipped,
        "total_doubts": total_doubts,
        "note": note,
    }


_TOOL_HANDLERS: dict[str, Callable[[AgentRunner, dict[str, Any]], Any]] = {
    "get_project_overview": _tool_get_project_overview,
    "update_project_config": _tool_update_project_config,
    "list_input_files": _tool_list_input_files,
    "read_input_file": _tool_read_input_file,
    "search_input": _tool_search_input,
    "read_guideline": _tool_read_guideline,
    "write_project_guideline": _tool_write_project_guideline,
    "list_dict_files": _tool_list_dict_files,
    "read_dict": _tool_read_dict,
    "save_dict": _tool_save_dict,
    "create_dict_file": _tool_create_dict_file,
    "get_name_table": _tool_get_name_table,
    "save_name_table": _tool_save_name_table,
    "start_translation": _tool_start_translation,
    "stop_translation": _tool_stop_translation,
    "wait": _tool_wait,
    "get_runtime": _tool_get_runtime,
    "list_problems": _tool_list_problems,
    "manage_problem_filter": _tool_manage_problem_filter,
    "manage_problem_white_list": _tool_manage_problem_white_list,
    "list_transl_cache": _tool_list_transl_cache,
    "read_transl_cache": _tool_read_transl_cache,
    "read_output": _tool_read_output,
    "delete_transl_cache": _tool_delete_transl_cache,
    "search_transl_cache": _tool_search_transl_cache,
    "patch_transl_cache": _tool_patch_transl_cache,
    "read_history_archive": _tool_read_history_archive,
    "ask_user": _tool_ask_user,
    "run_subagents": _tool_run_subagents,
}


# 带 reason 入参的工具：写类（改配置/规范/字典/缓存）+ start_translation。读类工具没有这个
# 参数、模型也不会传，这里再列一次是为了在分发时确认"这次调用确实能填原因"，而不是只靠
# schema 声明。start_translation 的 reason 还多一个用处：启动翻译是要审批的动作，理由会跟着
# arguments 一起进那张权限卡（见 request_permission），用户批之前先看到为什么。
_TOOLS_WITH_REASON: frozenset[str] = frozenset({
    "update_project_config",
    "write_project_guideline",
    "save_dict",
    "create_dict_file",
    "save_name_table",
    "manage_problem_filter",
    "manage_problem_white_list",
    "patch_transl_cache",
    "delete_transl_cache",
    "start_translation",
})


def _attach_reason(name: str, args: dict[str, Any], result: Any) -> Any:
    """把入参里的 reason 原样挂到工具结果上，供界面渲染（变更卡 / 启动翻译那一行）。

    模型不传（空串 / 只有空白）就什么都不加；结果不是 dict（读类工具可能返回列表）
    也不改形；结果里本来就有 reason 的以工具自己写的为准。
    """
    if name not in _TOOLS_WITH_REASON or not isinstance(result, dict):
        return result
    if "reason" in result:
        return result
    reason = str(args.get("reason") or "").strip()
    if not reason:
        return result
    return {**result, "reason": reason}


def _initial_session_title(project_dir: str, session_id: str, goal: str, current: str) -> str:
    """首个回合的会话标题：取用户第一条消息；已有用户消息则保留 current。

    新建会话时用户还没输入，create_session 只能给占位标题（「新会话」），
    真正的标题在首条消息到达（start）时才定下来。续聊（会话里已有用户消息）
    不重算，避免把标题改成后续某条消息。
    """
    text = (goal or "").strip()
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

    def __init__(self) -> None:
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

        # 首条用户输入同时存在于 meta.goal / messages 和 user_message 事件中。
        # 旧版本、异常退出或事件窗口裁剪可能只留下前两者；恢复时补一条内存事件，
        # 否则模型回答能恢复，用户的第一条气泡却会消失。step 放在现有事件之前，
        # 不改变后续事件编号，也不写回磁盘，避免恢复过程重复追加记录。
        initial_text = str(meta.get("goal") or "").strip()
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
            title = _initial_session_title(project_dir, sid, goal, title)

            stop_event = threading.Event()
            state = AgentState(
                status="running",
                goal=goal,
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
            runner = AgentRunner(state, host=host, port=port, stop_event=stop_event, registry=self)
            self._states.setdefault(key, {})[sid] = state
            self._runners.setdefault(key, {})[sid] = runner
            self._stop_events.setdefault(key, {})[sid] = stop_event
            # meta 落盘：记录会话身份与运行标记，重启后据此恢复。
            # backend_profile_data（含 token）不落盘，只存窗口大小这一个整数。
            if runner._store is not None:
                runner._store.append_meta(
                    project_dir=project_dir,
                    title=title,
                    goal=goal,
                    config_file_name=state.config_file_name,
                    context_window=state.context_window,
                    created_at=state.started_at,
                    running=True,
                )
            thread = threading.Thread(target=runner.run, name=f"agent-{os.path.basename(key)}", daemon=True)
            thread.start()
            _log(f"Agent 回合已启动: project={key} session={sid} config={config_file_name} goal={goal[:60]}")
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

    def answer_ask(self, project_dir: str, session_id: str | None, answers: Any) -> dict[str, Any]:
        """把用户对 ask_user 提问的回答送回去，唤醒正在等待的那个回合。

        校验（题数一致、单选不许给多个值）在回合线程里的 resolve_ask 做；这里只
        负责找到对应的 runner——没有在等待的询问时报 ValueError，界面据此提示。
        """
        key = self._key(project_dir)
        sid = self._resolve_session_id(project_dir, session_id)
        with self._lock:
            runner = self._runners.get(key, {}).get(sid) if sid else None
        if runner is None:
            raise ValueError("该会话没有正在等待回答的问题")
        return runner.resolve_ask(answers)

    def answer_permission(
        self, project_dir: str, session_id: str | None, decision: Any, reason: Any = ""
    ) -> dict[str, Any]:
        """把用户对权限审批卡的答复送回去，唤醒正在等待的那个回合。

        decision 只有 allow-once / allow-session / deny 三种；reason 是拒绝时可选的
        一句话（随工具结果给模型看）。校验在 resolve_permission 里做，这里只负责找到
        对应的 runner——没有在等待的审批时报 ValueError（卡片对应的回合已经结束、或这次
        审批已经答过了，界面据此提示）。
        """
        key = self._key(project_dir)
        sid = self._resolve_session_id(project_dir, session_id)
        with self._lock:
            runner = self._runners.get(key, {}).get(sid) if sid else None
        if runner is None:
            raise ValueError("该会话没有正在等待批准的权限请求")
        return runner.resolve_permission(decision, reason)

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
                "goal": state.goal,
                "step": state.step,
                "started_at": state.started_at,
                "finished_at": state.finished_at,
                "error": state.error,
                # 正在生成的助手消息（进行中）。与 events 里的已提交记录分离：
                # 它就是"进行中的助手消息"快照，刷新/切会话时据此把半条消息补上。
                "streaming": runner.live_streaming() if runner is not None else None,
                # 上下文用量：界面指示器的兜底来源（实时更新走 context_usage 事件）。
                # 打开页面/刷新时按当前历史现场估算，不依赖历史事件回放。
                "context": {
                    "used_tokens": _estimate_usage_tokens(
                        state.messages,
                        state.last_prompt_tokens,
                        state.anchored_message_count,
                    ),
                    "window_tokens": state.context_window,
                },
                # 排队中的消息：队列面板的数据源（实时变更走 queue 事件）
                "queued": [m.to_dict() for m in state.pending_messages],
                "events": [e.to_dict() for e in state.events],
            }

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
        # 旧版本/异常退出可能没落下首条 user_message 事件：用 meta.goal 补一条，
        # 否则刷新后用户的第一句就没了（与 _restore 的补救口径一致）。
        if not any(ev.get("type") == "user_message" for ev in events):
            goal = str((session_store.read_meta(project_dir, sid).get("goal") or "")).strip()
            if goal:
                events.insert(0, {"type": "user_message", "step": 0, "message": goal})
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
        with self._lock:
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
