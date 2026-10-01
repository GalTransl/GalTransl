"""常量、日志、异常与 LLM 请求的错误分类/重试退避。"""

from __future__ import annotations

from typing import Any


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
# 粗略字符 -> token 换算系数（没有 tokenizer 时的估算），分两档：
#   可打印 ASCII / 代码 ~ 4 字符一个 token；中日韩等多字节 ~ 1.5 字符一个 token
#   （一个汉字基本就是一个 token）。只按 /4 一刀切会系统性偏低两三倍——压缩的"保留段预算"
#   就是按这个偏小的单位量的，于是实际留下来的历史远超预算（"压完还是很大"）。
#   口径对齐 openclacky 的 estimate_content_tokens（ASCII/4 + 多字节/1.5）。
ASCII_CHARS_PER_TOKEN = 4
MULTIBYTE_CHARS_PER_TOKEN = 1.5
# 每条消息的固定开销（role、分隔符等）
MESSAGE_OVERHEAD_TOKENS = 4
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


def _truncate_text(text: str, limit: int, hint: str = "") -> str:
    """超长截断（给子代理的上下文用）：截断要明说，否则它会以为读全了。"""
    if len(text) <= limit:
        return text
    return text[:limit] + (hint or f"…（已截断，共 {len(text)} 字符）")
