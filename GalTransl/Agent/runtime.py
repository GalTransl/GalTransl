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
# 单回合的 LLM 轮数上限（一轮 = 一次请求 + 它带回的那批工具调用）。
# 不设实际上限：主循环跑到"模型不再调工具"为止，收尾只由模型自身、工具批返回
# terminate、请求出错或用户点停止决定，正常任务不靠计数收尾。
# 这里仍留一个防呆值：万一模型陷入重复调用，不至于把回合线程挂到天荒地老。
# 正常长任务（几十上百轮：等待-查进度-修复的循环）根本碰不到它。
MAX_STEPS = 1000
RUNTIME_EVENT_KEEP = 500
# 单次 get_runtime 最多报几"类"新错误（同类会合并；发过的被水位线记住，不再重复报）
RUNTIME_ERRORS_PER_QUERY = 10
# 瞬态事件：只进当前回合的 SSE 流，不进内存 events deque（也不落盘）。
# 两类：一类是流式增量（一次请求几十条，进 deque 会把 user_message/tool_call
# 等长期事件挤出 maxlen 窗口，前端刷新后就丢内容）；一类是能从状态快照重建的
# 实时指标（context_usage），刷新后由 status() 重新给出即可，不必占转录。
_TRANSIENT_EVENT_TYPES = frozenset({
    "content_delta",
    "reasoning_delta",
    "wait_tick",
    "context_usage",
    "queue",
})

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

# 标准翻译流程（必须按此顺序推进）
1. **了解项目**：先调用 get_project_overview 看翻译进度与项目配置（不传 include，一次拿全）。注意进度里的 total/translated 是「句数」且只统计已生成缓存的文件，translated==total 不等于整个项目翻完，整体是否翻完看 files_translated/files_total。再确认返回的 backend（agent = 本会话在用的后端，translator = 翻译任务会用的后端，各含配置名/类型/模型名）、项目确有输入文件（输入文件清单用 list_input_files 查），然后继续。之后再看进度时只传 include=["progress"]（必要时加 "backend"）：配置与配置键说明基本不变，不必重复拉。
2. **字典准备（在启动翻译前必须完成）**：
   a. 调用 list_dict_files 查看项目已配置的译前/GPT/译后字典文件；
   b. 调用 read_dict 读取现有内容，判断人名、专有名词是否已收录；
   c. 若缺少人名表，调用 get_name_table；若返回为空，先调用 start_translation(translator="dump-name") 生成人名表（dump-name 是导出 name 字段的专用 translator），完成后再次 get_name_table 查看结果，再调用 save_name_table 写回（若需要修正译名）；
   d. 若 GPT 字典为空且项目较大，可调用 start_translation(translator="GenDic") 自动生成 GPT 字典，并在该任务 completed 后通过 list_dict_files/read_dict 确认生成结果。
3. **试译定稿（全量翻译前必做，除非项目已有大量缓存）**：
   a. 调用 read_guideline 读取项目当前使用的翻译规范（配置 common.gpt.translation_guideline），理解文风要求；
   b. 调用 list_input_files 拿到文件清单与每个文件的待翻译句数（sentences：已有缓存的是准确值，标注 input 的原文条数估计偏大），据此估整体工作量、挑 1-2 个有代表性的文件；再用 read_input_file 各读几十句（index 使用 1-based，区间如 "1-50"），掌握角色、语气、专有名词、场景类型；
   c. 基于原文补充 GPT 字典：把抽读中遇到的人名、专有名词、常见口语用 save_dict(action="append") 收录进项目 GPT 字典（只发新增行，不重发整份字典）；
   d. 调用 start_translation(translator="<主翻译引擎>", files=["<一个代表性文件>"]) 只翻译这一个文件作为试译；
   e. 试译完成后用 read_transl_cache 阅读试译文件的译文，对照翻译规范评估文风、译名、语气是否达标；
   f. 若不满意：继续完善字典（save_dict）；对全局性的文风问题，用 update_project_config 把 common.gpt.change_prompt 设为 "AdditionalPrompt" 并设置 common.gpt.prompt_content 写入额外的翻译要求（如「译名统一用XX」「口语化程度、敬称的处理方式」等），这些要求会追加到每次翻译请求的 Prompt 里；也可以用 update_project_config 切换 common.gpt.translation_guideline 换一份更合适的规范；
   g. 满意后，把试译结果告知用户并说明你的评估结论，然后用 ask_user 询问是否开始全量翻译（给出「开始全量」/「先再调一版规范」之类的候选选项），等用户回答后再进入下一步。
4. **启动翻译（全量）**：调用 start_translation(translator="<主翻译引擎>")（不传 files 即翻译全部）。主翻译引擎从项目配置或 overview 中确认，常用值：ForGal-json / ForGal-tsv / ForNovel / sakura-v1.0 / galtransl-v3。一次只启动一个，项目已有运行中任务时不要重复提交。
5. **跟进进度（wait 前后都要查状态）**：启动翻译后先调用 get_runtime 确认任务已在跑，再调用 wait 等待一段合理时间（翻译任务 wait minutes=1~3，短任务 wait seconds=30）。wait 结束后必须再调用 get_runtime 确认任务状态：completed 进入下一步；仍在 running 时看返回的 eta_seconds 估算剩余时间——eta 还很长（如 >10 分钟）就按其一半的时长继续 wait，快完了（如 <2 分钟）就 wait seconds=30 再查，不要连续空转轮询也不要一次等过头。等待期间界面会显示倒计时。（get_runtime 各字段与 recent_errors 的口径见该工具说明。）
6. **复核结果**：调用 list_problems（不带参数）先看类型统计，了解哪类问题最多；再传 problem_type（如 problem_type="残留日文"）+ limit/offset 分页查看该类型的具体条目。用 read_transl_cache 的 index 参数精确读取有问题的条目（如 list_problems 返回的 index，可直接 `index="33-40,50-60"` 一次取多条）浏览实际译文；判断语意是否连贯时传 context（如 context=3）把前后各几句一起带上。要查某个词/译名在全项目的所有出现处、判断译法是否统一（如「ドルード」该统一成哪个写法），用 search_transl_cache(query="ドルード", context=3) 一次看遍所有出现处及其上下文。它默认只返回必要字段（说话人/原文/译文/问题，空值与未变化的字段会省略），要看译后字典替换结果或校对稿再传 fields。需要看缓存文件全貌（文件、条数）时用 list_transl_cache；注意返回里标注 translating / .append.jsonl 后缀的文件正在翻译中，此时读到的是旧快照，等任务 completed 再操作。
7. **问题修复循环**：对能直接改译文的条目，用 patch_transl_cache 一次批量修改多条（传 patches 数组，每条给 index 和要改的字段，如 pre_dst/proofread_dst），适合修正残留日文、明显错译；对需要字典约束的系统性问题，先 save_dict 补字典，再 start_translation(translator="rebuilda") 用更新后的字典重建（rebuilda 会跳过翻译、用译前/译后字典刷写缓存+结果 json；不要用 rebuildr，它只刷结果 json 不更新缓存，list_problems 看不到变化）。patch_transl_cache 与 rebuilda 可配合使用：先 patch 掉个别硬错，再 rebuilda 统一刷一遍字典相关的问题。对译文质量差、patch 也救不回来的句子，可用 delete_transl_cache 按条目删除缓存（indexes 支持区间），再 start_translation 让这些句子重翻。重建/修改后再 list_problems 复核（同样先看统计、再按类型下钻），直到问题数量显著下降。对确认无需处理的系统性问题类型（如字典使用提示、纯语气词提示），可用 manage_problem_filter(action="add", keyword=["…"]) 加入问题过滤清单（keyword 可传数组一次加多个），让统计聚焦真问题；过滤后统计会明显下降，属于预期效果。
8. **完成**：收尾前先调用 get_project_overview 确认项目真的翻完——只有 files_translated == files_total 且没有 running 任务才算整体完成（total==translated 可能只代表已缓存的部分翻完，不要据此收尾）；若还有文件没翻，回到流程 4 继续 start_translation 翻剩余文件。问题数可控、整体完成后，用 read_output 抽查最终输出文件（交付物；输出与缓存不完全一致，译后字典替换只在输出生效），确认无误后用一段自然语言总结本次操作（做了什么、翻译进度、剩余问题建议），不要调用工具，直接输出总结即可结束。

# 约束
- 每一步只调用必要的工具；能在一次工具调用里拿到的信息不要拆成多次。重复查看同类信息时用工具的分段参数（如 get_project_overview 的 include）只取变化的部分，别把基本不变的配置/说明反复拉一遍。
- 不要在未准备字典的情况下直接启动主翻译。
- 不要连续重复调用同一个工具相同参数（避免死循环）；若上一步结果不理想，换策略或总结收尾。
- 工具返回的 error 要阅读并据此调整下一步，不要忽略。
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
        masked = (token[:4] + "…" + token[-4:]) if len(token) > 8 else "***"
        _log(f"LLM 配置: model={model} endpoint={base_url} token={masked} context_window={self._context_window}")
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

                # 历史过长先压缩，避免下一步请求撑爆上下文窗口
                self._maybe_compact()

                # 上下文用量（界面指示器）：压缩之后再报，界面上立即看到回落
                self._emit_context_usage()

                loop_step = turns + 1
                turns += 1
                _log(f"—— 第 {loop_step}/{MAX_STEPS} 轮：请求 LLM（流式）中…")
                req_started = time.time()
                try:
                    content, tool_calls, finish_reason = self._stream_llm_response()
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
                        self._emit("tool_result", {"id": call_id, "name": name, "ok": True, "result": result, "duration_ms": duration_ms})
                        content_str = json.dumps(result, ensure_ascii=False)
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
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": self._messages_for_request(),
            "tools": AGENT_TOOLS,
            "tool_choice": "auto",
            "stream": True,
        }
        if include_usage:
            kwargs["stream_options"] = {"include_usage": True}
        return self._openai_client.chat.completions.create(**kwargs)

    def _messages_for_request(self) -> list[dict[str, Any]]:
        """发请求前的消息列表：按需给 assistant 消息补齐 thinking 模式的思考字段。

        DeepSeek（及同类 thinking 模式）要求：只要请求带了 tools，历史里每条 assistant
        消息都必须把当初的 reasoning_content 回传——**即使该轮模型没有实际进行工具调用**，
        少一条就 400「The `reasoning_content` in the thinking mode must be passed back to
        the API」。本方法负责：这场会话是 thinking 会话时（流里见过该字段，或历史里已有），
        给缺的那些补空串；老会话（本修复之前存的 assistant 消息还没带这个字段）靠这一步
        救回来。不是 thinking 会话就原样返回，不给不认它的 provider 塞陌生字段。
        """
        field = self._reasoning_field
        if not field:
            for message in self.state.messages:
                if message.get("role") != "assistant":
                    continue
                field = next((name for name in REASONING_FIELD_NAMES if name in message), "")
                if field:
                    break
        if not field:
            return list(self.state.messages)
        messages: list[dict[str, Any]] = []
        for message in self.state.messages:
            if message.get("role") == "assistant" and field not in message:
                messages.append({**message, field: ""})
            else:
                messages.append(message)
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
            started = segment_started[kind]
            parts = content_parts if kind == "content" else reasoning_parts
            self._emit(f"{kind}_end", {
                "length": len("".join(parts)),
                "duration_ms": int((time.time() - (started if started is not None else stream_started)) * 1000),
            })
            segment_started[kind] = None

        def _flush_stream(kind: str, pending: list[str], total: int, force: bool = False) -> None:
            """节流冲刷增量：最多 25ms 一条，避免 step 计数被 delta 刷爆。"""
            if not pending:
                return
            now = time.monotonic()
            if force or now - throttle[kind] >= 0.025:
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
        "post_dst_preview 只在它与 pre_dst 不同（译后字典替换过）时才带上。"
    )
    lines.append(
        f"读缓存默认只回精简列（{' / '.join(CACHE_ENTRY_FIELDS_DEFAULT)}，以及有值的附加列），"
        "要看别的列传 fields（fields=[\"*\"] 全要）。改译文用 patch_transl_cache，只能改 "
        f"{_patchable_fields_text()}；"
        "problem 与 post_* 是后端算出来的派生字段，改不动——改完译文跑 rebuilda（或重翻）"
        "它们才会跟着更新。"
    )
    lines.append(
        "另外注意：缓存 ≠ 交付物。最终的 gt_output 文件是缓存经译后字典替换、控制符还原后的形态，"
        "验收交付物要用 read_output，不要拿缓存当输出。"
    )
    return "\n".join(lines)


def _build_system_prompt(state: "AgentState", summary: str | None = None) -> str:
    """构造 system prompt：基础约束 + 当前项目环境 +（可选）对话压缩摘要。

    始终作为会话顶部唯一一条 system 消息。环境信息（项目目录/配置文件/目标）
    集中注入到 system prompt，对应的 user 消息只放用户的原始输入，避免重复。
    长会话压缩后调用方把摘要传进来，摘要块拼到末尾，这样压缩后仍能保持
    「基础约束 + 环境信息 + 摘要」三段结构，环境上下文不丢。
    """
    goal = state.goal or "按标准流程完成本项目的翻译"
    # 缓存字段说明跟着 system prompt 走：压缩会话后 _build_system_prompt 会重建整条
    # system 消息，这段说明也就跟着保留下来（不会被摘要吃掉）。
    parts: list[str] = [AGENT_SYSTEM_PROMPT + AGENT_TURN_PROMPT, _cache_fields_section()]
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
            "name": "list_input_files",
            "description": "列出待翻译的输入文件（原文）与每个文件的待翻译句数，供估工作量与挑选代表性文件（不必再逐个 read_input_file 数句子）。sentences_source=cache 是已有缓存文件的准确句数（与进度/ETA 同口径），=input 是尚未翻译文件的原文条数估计（未过文本插件过滤，可能偏大）；句数只用于估工作量，不代表进度。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_input_file",
            "description": "读取待翻译原文内容（文件插件解析后的条目：说话人+原文）。index 统一从 1 开始；留空 index 返回前 30 条；指定 index 支持区间，如 \"1-100\"。试译前用它了解原文文风、角色、专有名词。",
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
            "name": "read_guideline",
            "description": "读取翻译规范文件（translation_guidelines 目录，决定文风与措辞）。不带参数列出可选文件名；传 name（如 \"日译中_增强v2.md\"）返回规范全文。试译定稿前必读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "可选。规范文件名，来自不带参数调用返回的列表。"},
                },
                "required": [],
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
                "用法：先 get_runtime 确认任务在跑 → wait → wait 结束后再 get_runtime 查状态（completed / 仍在跑看 eta_seconds 决定下一轮等多久）。"
                "注意：wait 结束只是计时到了，不代表后台任务完成，必须查任务状态确认。"
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
                },
                "required": ["updates"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_problems",
            "description": "查询自动检测到的翻译问题（残留日文、字典使用、过长等）。默认返回类型统计（各类型问题数）；传 problem_type 查看该类型的具体条目，支持分页。",
            "parameters": {
                "type": "object",
                "properties": {
                    "problem_type": {
                        "type": "string",
                        "description": "可选。要查看的问题类型（来自默认返回的统计列表，如 \"残留日文\"），支持逗号分隔多个；传 \"*\" 返回所有类型的具体条目。留空只返回类型统计。",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "可选。单次返回条目数，默认 50，最大 200。",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "可选。分页偏移，默认 0。配合 has_more 翻页。",
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
            "description": "管理问题过滤关键字（项目配置 common.problemFilterKey，与「缓存与问题」页同一套配置）。命中的问题项会被 list_problems 和进度统计过滤掉。适合在确认某类问题（如字典使用提示、纯语气词提示）不需要处理后，将其加入过滤清单让统计聚焦真问题；也可移除误过滤的关键字。keyword 可传字符串或数组，一次增删多个。",
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
                        "description": "add/remove 必填。要操作的关键字（如 \"使用了GPT词典\"、\"正文直出\"），精确匹配、区分大小写。可传单个字符串，也可传数组一次操作多个。",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_transl_cache",
            "description": "列出缓存文件（译文）与各 .json 文件的条目数（.append.jsonl 增量日志不统计条目数）。注意：后缀为 .append.jsonl 的文件表示对应文件正在翻译中，此时读取缓存读到的是旧快照，应等任务 completed 后再读取/修改。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_transl_cache",
            "description": "读取某个缓存文件的条目（译文）。filename 来自 list_transl_cache 的缓存文件列表。留空 index 返回前 30 条；指定 index 只返回指定的条目。修问题/润色判断语意连贯时传 context 让目标条目前后各多带几句上下文。默认只返回必要字段（index/说话人/原文/译文/问题，以及确实非空或与原文不同的附加字段），要看别的字段再传 fields。不要读取 .append.jsonl 增量文件（翻译中旧快照），读对应的 .json 文件。",
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
                        "description": "可选。上下文句数（0-20）：目标条目前后各多返回 N 句，前后文条目标注 in_context=true。如 index=\"205-206\" context=3 返回 202~209。修问题判断语意时建议 2-4。",
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "可选。每条要返回哪些字段：index、name（说话人）、pre_src（原句）、pre_dst（译文）、"
                            "post_src、post_dst_preview（译后字典替换后的预览）、proofread_dst、proofread_by、"
                            "trans_by、problem。"
                            "不传 = 默认精简集（index/name/post_src/pre_dst/problem；post_dst_preview 仅在与 pre_dst 不同时给，"
                            "空值省略；要看 pre_src/trans_by 等列得显式传 fields）；传 [\"pre_dst\",\"problem\"] 这类只要某几列。"
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
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_transl_cache",
            "description": "在缓存中搜索译文/原文/问题。query 为关键词，field 取 all/src/dst/problem。只看命中行往往不够判断（如查「ドルード」要决定译成「多鲁德」还是「杜罗德」），传 context=N 让每条命中再带上前后各 N 句（in_context=true 的是上下文，不是命中），用法同 read_transl_cache 的 context。传 filename 只搜某个缓存文件（来自 list_transl_cache），修单文件问题时用，如 search_transl_cache(query=\"アクメ\", field=\"src\", filename=\"sc_2_st01.txt.json\")。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "field": {"type": "string", "enum": ["all", "src", "dst", "problem"]},
                    "filename": {"type": "string", "description": "可选。只在这个缓存文件里搜（来自 list_transl_cache）。留空搜全项目。"},
                    "context": {
                        "type": "integer",
                        "description": "可选，0-20（默认 0）。每条命中再带上前后各 N 句上下文，用于判断译名/语气/语意连贯。带上下文时命中上限会收紧（如 context=3 → 最多 40 条命中），命中很多时可配合 filename 缩小范围。",
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
            "description": "批量修改某个缓存文件中若干条目的译文/校对等字段。只更新 patches 里指定的条目与字段，其它条目原样保留。返回 updated（改动条目数）、changes（逐字段 before→after 的变更）与 problems（被改条目重建后仍存在的问题，没有则不返回）；改了什么一目了然、有没有引入新问题当场可验，不必再 read_transl_cache。适合发现问题后改译文、再配合 rebuilda 重建的复核循环。",
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
                                "trans_by": {"type": "string", "description": "可选。标记译者，如 'manual' 或 'agent'"},
                            },
                            "required": ["index"],
                        },
                    },
                },
                "required": ["filename", "patches"],
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
                "2-6 个候选选项，用户还可以自己填；一次最多 4 题。用户跳过某题会以空答案返回"
                "（不算失败），你按自己的最佳判断继续即可。能从项目配置、字典或原文里判断出来的"
                "不要问——只有真的需要人来定夺时才用。"
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
    "common.problemFilterKey": "问题过滤关键字列表：命中的问题项在问题统计与 list_problems 中被过滤掉",
    "common.gpt.contextNum": "每次请求附带的前文句数；值越大上下文越强、成本越高（常用 8）[0-32]",
    "common.gpt.translation_guideline": "使用的翻译规范文件名（位于 translation_guidelines 文件夹），决定文风与措辞",
    "common.gpt.enhance_jailbreak": "是否启用「抗拒答」增强提示，降低模型拒答概率 [true/false]",
    "common.gpt.change_prompt": "Prompt 修改模式：no 不改；AdditionalPrompt 追加；OverwritePrompt 覆盖默认提示词",
    "common.gpt.prompt_content": "Prompt 自定义内容；仅在 change_prompt 为 AdditionalPrompt/OverwritePrompt 时生效",
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
    """「了解项目」返回的配置快照：剔除 backendSpecific。

    那一节是 API 令牌、端点等敏感信息（发给模型等于把密钥递出去），对"了解项目"
    也没有价值——实际生效的后端见返回里的 backend 字段。
    """
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if k != "backendSpecific"}


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


def _tool_list_input_files(runner: AgentRunner, _args: dict[str, Any]) -> Any:
    """列出待翻译文件（原文件，输入目录），带每个文件的待翻译句数。

    句数供估工作量、挑试译文件用，两个来源按可靠性优先：

    - 已经有缓存的文件 → **缓存条数**（准确，与进度/ETA 同一口径）；
    - 尚未翻译的文件 → 文件插件解析原文的条数（**未过文本插件过滤**，如「跳过无日文句」
      会丢掉一部分句子，所以通常偏大），标注 sentences_source=input。

    这样调用方不必再逐个 read_input_file 去数句子，也不会把未过滤的条数
    （偏大）误当成与进度同口径的总句数。
    """
    pid = runner._project_id()
    cfg = urllib.parse.quote(runner.state.config_file_name or "config.yaml")
    files = runner._http_get(f"/api/projects/{pid}/files?counts=1&config={cfg}")
    # 缓存文件的条数（/files 的 cache_files 带 entry_count）
    cache_counts = {
        str(item.get("name")): int(item.get("entry_count") or 0)
        for item in files.get("cache_files", [])
        if isinstance(item, dict) and item.get("name") and item.get("entry_count")
    }
    input_files: list[dict[str, Any]] = []
    exact_files = 0
    estimated_files = 0
    for item in files.get("input_files", []):
        if not isinstance(item, dict) or not item.get("is_file", True):
            continue
        name = str(item.get("name") or "")
        singles, chunk_re = _input_cache_matchers(name)
        cached_sentences = sum(
            count
            for cache_name, count in cache_counts.items()
            if cache_name in singles or chunk_re.match(cache_name)
        )
        parsed = item.get("sentences")
        sentences: int | None = None
        source = ""
        if cached_sentences > 0:
            sentences, source = cached_sentences, "cache"
            exact_files += 1
        elif isinstance(parsed, int):
            sentences, source = parsed, "input"
            estimated_files += 1
        input_files.append({
            "name": name,
            "size": item.get("size", 0),
            "sentences": sentences,
            "sentences_source": source,
        })

    note = (
        "sentences 是该文件的待翻译句数，用来估工作量（别拿它当进度）："
        "sentences_source=cache 表示该文件已有缓存、取缓存条数（准确，与进度/ETA 同口径）；"
        "=input 表示尚未翻译、按文件插件解析原文的条数估计"
        "（未过文本插件过滤，如「跳过无日文句」会少掉一部分，因此可能偏大）；"
        "null 表示解析失败。"
    )
    if input_files:
        note += f"本次 {exact_files} 个文件是准确值，{estimated_files} 个是估计值。"
    return {
        "input_files": input_files,
        "count": len(input_files),
        "sentences_total": sum(
            f["sentences"] for f in input_files if isinstance(f["sentences"], int)
        ),
        "note": note,
    }


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
    """读取翻译规范文件内容。不带参数列出可选的规范文件名。"""
    name = str(args.get("name", "") or "").strip()
    if not name:
        data = runner._http_get("/api/translation-guidelines")
        guidelines = data.get("guidelines", [])
        current = "（见 get_project_overview 配置 common.gpt.translation_guideline）"
        return {"guidelines": guidelines, "note": f"当前项目使用的规范：{current}。传 name 读取内容。"}
    return runner._http_get(f"/api/translation-guidelines/{urllib.parse.quote(name)}")


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

    if action == "overwrite":
        new_lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        extra: dict[str, Any] = {}
    else:
        new_lines, extra = _merge_dict_lines(before_lines, _split_dict_incoming(content), action)

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


def _tool_manage_problem_filter(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """增/删/查项目配置 common.problemFilterKey（问题过滤关键字）。

    与桌面端「缓存与问题」页同一套配置：命中的问题项会在 list_problems /
    进度统计里被过滤掉。add/remove 都是对关键字的精确匹配（区分大小写）。"""
    action = str(args.get("action", "")).strip()
    if action not in ("list", "add", "remove"):
        raise AgentToolError("action must be one of: list, add, remove")
    pid = runner._project_id()
    config_name = runner.state.config_file_name or DEFAULT_CONFIG_FILE

    def _normalize(raw: Any) -> list[str]:
        items = raw.split("\n") if isinstance(raw, str) else raw
        if not isinstance(items, list):
            return []
        return [k.strip() for k in items if isinstance(k, str) and k.strip()]

    def _load() -> tuple[dict[str, Any], list[str]]:
        data = runner._http_get(f"/api/projects/{pid}/config?config={urllib.parse.quote(config_name)}")
        config = data.get("config")
        if not isinstance(config, dict):
            raise AgentToolError("项目配置读取失败")
        common = config.get("common")
        if not isinstance(common, dict):
            common = {}
            config["common"] = common
        keys = _normalize(common.get("problemFilterKey", []))
        # 去重保序
        return config, list(dict.fromkeys(keys))

    if action == "list":
        _, keys = _load()
        return {"filter_keys": keys, "count": len(keys)}

    # keyword 支持单个字符串或字符串数组（一次增删多个）：去重保序、忽略空串
    raw_keyword = args.get("keyword")
    if isinstance(raw_keyword, str):
        keywords = [raw_keyword.strip()]
    elif isinstance(raw_keyword, list):
        keywords = [k.strip() for k in raw_keyword if isinstance(k, str)]
    else:
        keywords = []
    keywords = [k for k in dict.fromkeys(keywords) if k]
    if not keywords:
        raise AgentToolError("keyword is required for add/remove（字符串或字符串数组）")

    config, keys = _load()
    existing = set(keys)
    if action == "add":
        hit = [k for k in keywords if k not in existing]  # 实际新增
        miss = [k for k in keywords if k in existing]  # 本来就有
        changes = [_change("problemFilterKey", None, k, "add") for k in hit]
        hit_key, miss_key, miss_note = "added", "already_present", "已在列表中"
    else:  # remove
        hit = [k for k in keywords if k in existing]  # 实际移除
        miss = [k for k in keywords if k not in existing]  # 本来就没有
        changes = [_change("problemFilterKey", k, None, "remove") for k in hit]
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

    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    for item in updates:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", "")).strip()
        if not key:
            continue
        value = _parse_config_value(item.get("value"))
        before = _get_config_key(config, key)
        if _set_config_key(config, key, value):
            applied.append({"key": key, "value": value})
            kind = "add" if before is _MISSING else "replace"
            changes.append(_change(key, before if before is not _MISSING else None, value, kind))
        else:
            skipped.append({"key": key, "reason": "配置里不存在该键；只能修改已存在的键"})

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


def _tool_get_name_table(runner: AgentRunner, _args: dict[str, Any]) -> Any:
    pid = runner._project_id()
    return runner._http_get(f"/api/projects/{pid}/name-table")


def _tool_save_name_table(runner: AgentRunner, args: dict[str, Any]) -> Any:
    names = args.get("names", [])
    if not isinstance(names, list):
        raise AgentToolError("names must be an array")
    pid = runner._project_id()
    old = runner._http_get(f"/api/projects/{pid}/name-table")
    old_names = [n.get("name") if isinstance(n, dict) else n for n in old.get("names", [])]
    result = runner._http_post(f"/api/projects/{pid}/name-table/save", {"names": names})
    old_set = set(map(str, old_names))
    new_set = set(map(str, names))
    added = sorted(new_set - old_set)
    removed = sorted(old_set - new_set)
    changes: list[dict[str, Any]] = [
        *(_change("人名表", None, n, "add") for n in added),
        *(_change("人名表", n, None, "remove") for n in removed),
    ]
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
    # 事件带上本次工具调用 id：界面据此把倒计时挂到这一行（多次等待各挂各行）
    call_id = runner._active_tool_call_id
    _log(f"  ⏳ 开始等待 {total:g}s" + (f"（{reason}）" if reason else ""))
    runner._emit("wait_start", {"id": call_id, "seconds": round(total, 1), "total_ms": total_ms, "reason": reason})

    interrupted = False
    while True:
        if runner.stop_event.is_set():
            interrupted = True
            break
        elapsed = time.monotonic() - started
        if elapsed >= total:
            break
        remaining_ms = max(0, total_ms - int(elapsed * 1000))
        runner._emit("wait_tick", {"id": call_id, "remaining_ms": remaining_ms, "total_ms": total_ms})
        runner.stop_event.wait(WAIT_TICK)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    remaining_ms = 0 if interrupted else max(0, total_ms - elapsed_ms)
    runner._emit(
        "wait_end",
        {
            "id": call_id,
            "interrupted": interrupted,
            "elapsed_ms": elapsed_ms,
            "remaining_ms": remaining_ms,
            "total_ms": total_ms,
        },
    )
    if interrupted:
        _log(f"  ⏳ 等待被停止信号打断，已等 {elapsed_ms / 1000:.1f}s")
        return {"waited_seconds": round(elapsed_ms / 1000, 1), "wait_interrupted": True, "note": "等待被用户停止打断"}
    _log(f"  ⏳ 等待结束，共 {elapsed_ms / 1000:.1f}s")
    return {
        "waited_seconds": round(elapsed_ms / 1000, 1),
        "wait_completed": True,
        "note": "这只是计时结束，不代表后台任务完成。如果是翻译任务，请调用 get_runtime 确认任务状态后再决定下一步。",
    }


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


def _diff_lines(before_text: str, after_text: str, *, context: int = 0, max_lines: int = 200) -> list[dict[str, Any]]:
    """整文本替换时的逐行 diff（新增行/删除行），给前端渲染行级 diff。

    用最长公共行序列近似（对字典这种逐行 KV 文本足够准确）；超过 max_lines
    时截断并标记 truncated，避免整本小说级 diff 刷屏。"""
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


def _tool_list_problems(runner: AgentRunner, args: dict[str, Any]) -> Any:
    pid = runner._project_id()
    cfg = urllib.parse.quote(runner.state.config_file_name)
    data = runner._http_get(f"/api/projects/{pid}/problems?config={cfg}")
    problems = data.get("problems", [])

    # 不带 problem_type：先给类型统计（大项目问题上千条，全量列出没有意义），
    # Agent 据此决定看哪一类。
    problem_type = str(args.get("problem_type", "") or "").strip()
    if not problem_type:
        stats: dict[str, int] = {}
        for p in problems:
            for t in _split_problem_types(p.get("problem", "")):
                stats[t] = stats.get(t, 0) + 1
        ranked = sorted(stats.items(), key=lambda kv: -kv[1])
        return {
            "total": data.get("total", len(problems)),
            "mode": "stats",
            "types": [{"type": t, "count": c} for t, c in ranked],
            "hint": "默认只返回类型统计。用 problem_type 指定类型查看具体条目（配合 limit/offset 分页），problem_type 传 \"*\" 列出全部类型的具体条目。",
        }

    # 指定类型：过滤出问题里含该类型的条目（子串匹配，与统计口径对齐）
    if problem_type != "*":
        wanted = [t.strip() for t in problem_type.split(",") if t.strip()]
        problems = [p for p in problems if any(w in _split_problem_types(p.get("problem", "")) for w in wanted)]

    limit = args.get("limit", 50)
    offset = args.get("offset", 0)
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0
    matched = len(problems)
    return {
        "total": data.get("total", len(problems)),
        "matched": matched,
        "problem_type": problem_type,
        "offset": offset,
        "returned": len(problems[offset : offset + limit]),
        "has_more": offset + limit < matched,
        "problems": problems[offset : offset + limit],
    }


def _tool_list_transl_cache(runner: AgentRunner, _args: dict[str, Any]) -> Any:
    """列出缓存文件（译文）。带每个文件的条目数。

    .append.jsonl 后缀 = 增量缓存，说明该文件正在翻译中（快照+增量并行写）：
    此时缓存还没合并，read_transl_cache / patch_transl_cache / delete_transl_cache 读到的可能是
    旧快照，操作前先确认翻译任务已结束。"""
    pid = runner._project_id()
    data = runner._http_get(f"/api/projects/{pid}/cache")
    files = []
    translating = 0
    for f in data.get("files", []):
        name = str(f.get("name", ""))
        if not name:
            continue
        entry: dict[str, Any] = {"name": name, "size": f.get("size", 0)}
        # 后端 _list_dir_entries 只对 .json 统计 entry_count（按 list 长度，含 0）；
        # .append.jsonl 等没有该字段时干脆不返回 entries，而不是填 0——否则会被
        # 误读成「空缓存」。
        entry_count = f.get("entry_count")
        if isinstance(entry_count, int):
            entry["entries"] = entry_count
        if name.endswith(".append.jsonl"):
            entry["status"] = "translating"
            translating += 1
        files.append(entry)
    result: dict[str, Any] = {"cache_files": files, "count": len(files)}
    if translating:
        result["translating"] = translating
        result["note"] = f"有 {translating} 个 .append.jsonl 增量缓存文件，说明对应文件正在翻译中；此时读取缓存会读到旧快照，等任务 completed 后再操作。"
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
    "trans_by",
    "problem",
)
# 默认精简集：一条只回「谁说的 + 原文 + 译文 + 问题」。
# 原文只给一列——post_src（真正送去翻译的那版），不再同时带 pre_src：两列在多数条目上
# 只差对话符号/译前字典替换，一次读几十条就是双份原文，白占上下文（要看 pre_src 传 fields）。
# trans_by 同理默认不给（它是"哪个模型翻的"，读译文时基本用不上）。
CACHE_ENTRY_FIELDS_DEFAULT: tuple[str, ...] = ("index", "name", "post_src", "pre_dst", "problem")
# 每个字段的含义（拼进 system prompt，见 _cache_fields_section）。
# 命名来源见 GalTransl/CSentense.py：pre_src=前原、post_src=前润（送去翻译的原文）、
# pre_dst=后原（模型原始译文）、post_dst=后润（最终译文）。
CACHE_ENTRY_FIELD_DESCRIPTIONS: dict[str, str] = {
    "index": "条目序号（1 起、按文件顺序）；改译文/删条目/读上下文都用它定位",
    "name": "说话人；旁白为空",
    "pre_src": "原始原文（前原），管道最开始的句子；默认不返回（要看它传 fields）",
    "post_src": "真正送去翻译的原文（前润）：对话符号处理 + 译前字典替换之后的文本",
    "pre_dst": "模型返回的译文（后原），未经译后字典替换",
    "post_dst_preview": "最终译文的缓存快照（后润）：译后字典替换 + 对话符号恢复之后的形态",
    "proofread_dst": "校对/润色稿；有内容时它就是这条的最终译文（优先于 pre_dst）",
    "proofread_by": "校对者标记（校对失败的会带 Fail）；未校对为空",
    "trans_by": "译者标记：模型名或引擎名；被 Agent/manual 手改过的也会标在这里；默认不返回（要看它传 fields）",
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
}


def _cache_field_value(entry: dict[str, Any], name: str) -> Any:
    """按字段名取缓存条目的值，兼容旧缓存的旧键名（取不到返回 None）。"""
    if name in entry:
        return entry[name]
    old_key = _CACHE_ENTRY_OLD_KEYS.get(name)
    return entry.get(old_key) if old_key else None


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
            # 译后字典替换过才有意义：只在它与 pre_dst 不同时才值得占位置
            post_dst = _cache_field_value(entry, "post_dst_preview")
            if post_dst and post_dst != _cache_field_value(entry, "pre_dst"):
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


def _tool_read_transl_cache(runner: AgentRunner, args: dict[str, Any]) -> Any:
    filename = str(args.get("filename", "")).strip()
    if not filename:
        raise AgentToolError("filename is required")
    fields = _normalize_cache_fields(args)
    pid = runner._project_id()
    data = runner._http_get(f"/api/projects/{pid}/cache/{urllib.parse.quote(filename)}")
    raw_entries = [e for e in data.get("entries", []) if isinstance(e, dict)]
    entries = _project_cache_entries(raw_entries, fields)
    result_extra: dict[str, Any] = {}
    # 正在翻译中的增量缓存：读到的是旧快照，明确告诉模型而不是让它误判
    if filename.endswith(".append.jsonl"):
        result_extra["warning"] = "这是翻译中的增量缓存文件，读到的是旧快照；请等任务 completed 后用同名 .json 文件读取。"
    result_extra["fields"] = list(fields) if fields is not None else list(CACHE_ENTRY_FIELDS_DEFAULT)
    if fields is None:
        result_extra["fields_note"] = (
            "默认精简字段：post_dst_preview 只在它与 pre_dst 不同（存在译后字典替换）时返回，"
            "proofread_* / trans_by / 备注等空值已省略；要看其它字段传 fields。"
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

    # context=N：目标条目前后各多带 N 句（修问题/润色时需要前后文判断语意连贯）。
    # 按文件顺序连续取，扩展 index 标注 in_context=true，与目标条目区分。
    raw_context = args.get("context", 0)
    try:
        context = max(0, min(int(raw_context), 20))
    except (TypeError, ValueError):
        raise AgentToolError(f"context 必须是 0-20 的整数（收到 {raw_context!r}）")

    result: dict[str, Any] = {
        "filename": filename,
        "count": len(entries),
        "context": context,
        **result_extra,
    }

    if context > 0 and by_index:
        # 以命中 index 的闭包向外扩 N 句：例如 index="205-206", context=3
        # -> 返回 202~209。多个命中段各自扩展后合并。
        spans: list[tuple[int, int]] = []
        for i in sorted(wanted):
            if spans and i <= spans[-1][1] + 2 * context + 1:
                spans[-1] = (spans[-1][0], i)
            else:
                spans.append((i, i))
        wanted_ctx: set[int] = set(wanted)
        for a, b in spans:
            for j in range(max(0, a - context), b + context + 1):
                wanted_ctx.add(j)
        # 浅拷贝再标注 in_context（不污染共享条目）；目标条目 False，扩展
        # 出来的前后文 True，让模型聚焦目标条目
        picked_ctx: list[dict[str, Any]] = []
        for i in sorted(wanted_ctx):
            e = by_index.get(i)
            if e is None:
                continue
            copy = dict(e)
            copy["in_context"] = i not in wanted
            picked_ctx.append(copy)
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


def _tool_search_transl_cache(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """在缓存里搜译文/原文/问题；context=N 时每条命中再带上前后各 N 句。

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
    # 带上下文时收紧命中上限：命中 × (2N+1) 行一起返回，总量控制在 ~300 行内，
    # 否则"命中上百条 × 前后各几句"会直接把返回体撑爆。total 不受影响（仍报全部命中数）。
    max_hits = 100 if context == 0 else max(1, 300 // (2 * context + 1))
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
    if filename:
        body["filename"] = filename
    result = runner._http_post(f"/api/projects/{pid}/cache/search", body)
    notes: list[str] = []
    if isinstance(result, dict) and context:
        result["context"] = context  # 服务端已回；这里兜底，保证调用方一定看得到
        notes.append(
            f"已带上下文：每条命中前后各 {context} 句（in_context=true 的是上下文、不是命中）；"
            f"带上下文时命中上限收紧为 {max_hits} 条以免返回体过大，total 仍是全部命中数——"
            "命中很多时可用 filename 缩小范围或换更具体的关键词。"
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


# patch_transl_cache 允许更新的条目字段白名单（其余字段一律不动，避免误改 problem/preview 等派生字段）
_PATCHABLE_FIELDS = {
    "pre_dst",
    "proofread_dst",
    "trans_by",
}


def _patchable_fields_text() -> str:
    """可改字段的一行文本（按 CACHE_ENTRY_FIELDS 的顺序，输出稳定）。

    system prompt 的字段说明与 patch 工具的报错都用它，避免两处各写一份再漂移。
    """
    return " / ".join(name for name in CACHE_ENTRY_FIELDS if name in _PATCHABLE_FIELDS)


def _tool_patch_transl_cache(runner: AgentRunner, args: dict[str, Any]) -> Any:
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

    applied_indexes: list[int] = []
    skipped: list[dict[str, Any]] = []
    not_found: list[int] = []
    changes: list[dict[str, Any]] = []
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
            skipped.append({"index": idx_i, "reason": f"无可更新字段（只允许 {_patchable_fields_text()}）"})
            continue
        for f, v in updates.items():
            changes.append(_change(f"#{idx_i}.{f}", entry.get(f), v, "replace"))
        entry.update(updates)
        applied_indexes.append(idx_i)

    if not applied_indexes:
        raise AgentToolError(
            f"没有条目被更新（updated=0, skipped={len(skipped)}, not_found={len(not_found)}）"
        )

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

    kept: list[dict[str, Any]] = []
    deleted_indexes: list[int] = []
    deleted_previews: list[dict[str, Any]] = []
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
            deleted_previews.append({"index": idx, "text": preview})
        else:
            kept.append(e)

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
        questions.append(
            {
                "question": text,
                "options": options[:ASK_MAX_OPTIONS],
                "multiSelect": item.get("multiSelect") is True,
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


def _tool_ask_user(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """问用户：阻塞当前回合直到用户作答（或回合被停止，此时按跳过返回）。"""
    questions = _normalize_ask_questions(args)
    answers = runner.ask_user(os.urandom(8).hex(), runner._active_tool_call_id, questions)
    return {
        "summary": _format_ask_answers(questions, answers),
        "questions": [question["question"] for question in questions],
        "answers": answers,
    }


_TOOL_HANDLERS: dict[str, Callable[[AgentRunner, dict[str, Any]], Any]] = {
    "get_project_overview": _tool_get_project_overview,
    "update_project_config": _tool_update_project_config,
    "list_input_files": _tool_list_input_files,
    "read_input_file": _tool_read_input_file,
    "read_guideline": _tool_read_guideline,
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
    "list_transl_cache": _tool_list_transl_cache,
    "read_transl_cache": _tool_read_transl_cache,
    "read_output": _tool_read_output,
    "delete_transl_cache": _tool_delete_transl_cache,
    "search_transl_cache": _tool_search_transl_cache,
    "patch_transl_cache": _tool_patch_transl_cache,
    "ask_user": _tool_ask_user,
}


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
        translator_profile_name: str = "",
        translator_profile_data: dict[str, Any] | None = None,
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
            if translator_profile_name:
                state.translator_profile_name = translator_profile_name
            if translator_profile_data:
                state.translator_profile_data = translator_profile_data

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
