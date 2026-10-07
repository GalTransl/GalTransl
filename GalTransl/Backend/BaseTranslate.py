import asyncio
import httpx
from datetime import timedelta
from opencc import OpenCC
from typing import Optional, List
from collections import deque
from contextvars import ContextVar
from threading import Lock
from GalTransl.COpenAI import COpenAITokenPool, COpenAIToken
from GalTransl.ConfigHelper import CProxyPool, build_httpx_proxy_kwargs
from GalTransl import LOGGER, LANG_SUPPORTED, TRANSLATOR_DEFAULT_ENGINE
from GalTransl import DEFAULT_GUIDELINE_NAME
from GalTransl.i18n import get_text, GT_LANG
from GalTransl.ConfigHelper import (
    CProjectConfig,
)
from GalTransl.CSentense import CSentense, CTransList
from GalTransl.Cache import save_transCache_to_json
from GalTransl.Dictionary import CGptDict
from GalTransl.Utils import load_guideline_file, fix_quotes2
from GalTransl.ProjectGuideline import combine_guidelines, read_project_guideline
from openai import RateLimitError, AsyncOpenAI, APIConnectionError, APITimeoutError
from openai import DefaultAioHttpClient
from openai._types import NOT_GIVEN
import random
import time
from contextlib import suppress
from GalTransl.TerminalOutput import should_print_translation_logs

try:
    from pyreqwest.compatibility.httpx import HttpxTransport
    from pyreqwest.client import ClientBuilder as PyreqwestClientBuilder
except Exception:
    HttpxTransport = None
    PyreqwestClientBuilder = None


_GLOBAL_RPM_LOCK = Lock()
_GLOBAL_NEXT_ALLOWED_TS = 0.0
_CHATBOT_STATE: ContextVar[tuple[bool, str] | None] = ContextVar(
    "galtransl_chatbot_state", default=None
)

# 思考内容的字段名：不同平台不一样（DeepSeek 用 reasoning_content，OpenRouter 等用 reasoning）。
# 与 GalTransl/Agent/core.py 的 REASONING_FIELD_NAMES 同一套约定：多轮对话里拿到思考内容后
# 必须按命中的原名跟着 assistant 消息带回去，thinking 模式下少一条下一次请求就会被判 400
# （「The `reasoning_content` in the thinking mode must be passed back to the API」）。
REASONING_FIELD_NAMES = ("reasoning_content", "reasoning")

# 流式输出期间给工作台报「思考中/翻译中 + 输出速度」的间隔（秒）：逐 chunk 报太密，
# 报太稀又看不出快慢，0.2s 大概是一次界面轮询的五分之一。换阶段不等这个间隔，立刻报
_STREAM_PROGRESS_REPORT_INTERVAL = 0.2


def _extract_reasoning(message_or_delta) -> tuple[str, str]:
    """从流式 delta / 非流式 message 上取思考内容，返回 (文本, 命中的字段名)。

    位置因平台而异：有的在直接属性（delta.reasoning_content）上，有的被 OpenAI SDK
    收进 model_extra（部分平台直接用 reasoning）。两个名字、两处都试一遍。
    """
    extra = getattr(message_or_delta, "model_extra", None) or {}
    for name in REASONING_FIELD_NAMES:
        piece = getattr(message_or_delta, name, None)
        if not isinstance(piece, str) or not piece:
            piece = extra.get(name) if isinstance(extra, dict) else None
        if isinstance(piece, str) and piece:
            return piece, name
    return "", ""


class _InlineThinkDetector:
    """判断流式正文此刻还在不在开头那段 <think>…</think> 里。

    有的接口（本地 llama.cpp、部分中转）不走 reasoning_content，而是把思考直接写进正文开头，
    引擎最后按 </think> 剥掉（见 ForGalJsonTranslate）。只盯累计的正文、不看单个 chunk：
    标签会被流式切成两半（`</thi` + `nk>`），逐 chunk 找必然漏。
    """

    _OPEN = "<think>"
    _CLOSE = "</think>"

    def __init__(self) -> None:
        self._state = "head"  # head 还没出现非空白 / think 在思考段里 / body 正文
        self._buffer = ""

    def feed(self, piece: str) -> Optional[str]:
        """喂一段正文，返回它算 thinking 还是 writing；只有空白、还判断不了时返回 None。"""
        if self._state == "body":
            return "writing" if piece.strip() else None
        self._buffer += piece
        if self._state == "head":
            head = self._buffer.lstrip()
            if not head:
                self._buffer = ""
                return None
            if head.startswith(self._OPEN):
                self._state = "think"
                self._buffer = head[len(self._OPEN):]
            elif self._OPEN.startswith(head):
                return "thinking"  # 「<thi」：多半是标签被切开了，先按思考算
            else:
                self._state = "body"
                self._buffer = ""
                return "writing"
        end = self._buffer.find(self._CLOSE)
        if end < 0:
            # 留一截尾巴，拼下一段时能认出被切开的 </think>
            self._buffer = self._buffer[-(len(self._CLOSE) - 1):]
            return "thinking"
        rest = self._buffer[end + len(self._CLOSE):]
        self._state = "body"
        self._buffer = ""
        return "writing" if rest.strip() else "thinking"


class _FileRequestProgress:
    """一次 ask_chatbot 报给工作台「文件进度」的状态：那一行的小灯就是它点的。

    进门登记（请求中），每次尝试发出前回到「请求中」，流式输出时按阶段报字数（思考中 / 翻译中），
    失败退避时报「重试中」，出门注销——注销在 ask_chatbot 的 finally 里，正常返回、重试到上限、
    取消都会走到，不会留下一盏一直亮着的灯。非流式请求从头到尾都是「请求中」。
    progress_file 留空就什么都不做。这只是界面上的一颗灯：出错一律吞掉，不能影响翻译本身。
    """

    def __init__(self, pj_config, progress_file: str) -> None:
        self._request_id = None
        self._phase = "waiting"
        self._pending_chars = 0
        self._last_report = 0.0
        if not progress_file:
            return
        try:
            from GalTransl.server import begin_runtime_request

            self._request_id = begin_runtime_request(
                getattr(pj_config, "runtime_project_dir", pj_config.getProjectDir()),
                filename=progress_file,
            )
        except Exception:
            self._request_id = None

    def attempt(self) -> None:
        """又要发一次请求了（首发或重试）：回到「请求中」，等第一个字。"""
        self._switch("waiting")

    def retrying(self) -> None:
        """这一次失败了，正在退避等下一次。"""
        self._switch("retrying")

    def output(self, phase: Optional[str], chars: int) -> None:
        """流式里新到一段输出。phase 取 thinking / writing；None 表示只是空白，不改阶段。"""
        if self._request_id is None:
            return
        if phase is not None and phase != self._phase:
            self._switch(phase, chars)
            return
        if self._phase not in ("thinking", "writing"):
            return
        self._pending_chars += chars
        now = time.monotonic()
        if now - self._last_report >= _STREAM_PROGRESS_REPORT_INTERVAL:
            self._report(now)

    def close(self) -> None:
        if self._request_id is None:
            return
        request_id, self._request_id = self._request_id, None
        try:
            from GalTransl.server import end_runtime_request

            end_runtime_request(request_id)
        except Exception:
            pass

    def _switch(self, phase: str, chars: int = 0) -> None:
        if self._request_id is None:
            return
        if phase == self._phase and not chars and not self._pending_chars:
            return
        now = time.monotonic()
        if self._pending_chars:
            # 上一阶段攒着的字先记到上一阶段名下，再换
            self._report(now)
        self._phase = phase
        self._pending_chars = chars
        self._report(now)

    def _report(self, now: float) -> None:
        chars, self._pending_chars = self._pending_chars, 0
        self._last_report = now
        try:
            from GalTransl.server import note_runtime_request

            note_runtime_request(self._request_id, phase=self._phase, chars=chars)
        except Exception:
            pass


class RequestHealthMetrics:
    def __init__(self) -> None:
        self._samples: deque[tuple[float, float, bool]] = deque()
        self._lock = Lock()

    def _trim_locked(self, now: float, window_seconds: float) -> None:
        cutoff = now - max(5.0, float(window_seconds))
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def record(self, latency_seconds: float, is_rate_limited: bool) -> None:
        now = time.monotonic()
        latency = max(0.0, float(latency_seconds))
        with self._lock:
            self._samples.append((now, latency, bool(is_rate_limited)))
            self._trim_locked(now, 120.0)

    def snapshot(self, window_seconds: float = 30.0) -> dict:
        now = time.monotonic()
        with self._lock:
            self._trim_locked(now, window_seconds)
            total = len(self._samples)
            if total == 0:
                return {
                    "total": 0,
                    "rate_limited": 0,
                    "rate_limited_ratio": 0.0,
                    "avg_latency": 0.0,
                }
            rate_limited = sum(1 for _, _, limited in self._samples if limited)
            avg_latency = sum(lat for _, lat, _ in self._samples) / total
            return {
                "total": total,
                "rate_limited": rate_limited,
                "rate_limited_ratio": rate_limited / total,
                "avg_latency": avg_latency,
            }


class TranslationRequestError(RuntimeError):
    """自动模板的一次 API 请求失败，包括流式响应读取失败。"""


class TranslationParseError(RuntimeError):
    """一次翻译响应未通过校验；自动模板据此立即切换模式。"""


class BaseTranslate:
    def _raise_parse_error_for_auto(self, message):
        if getattr(self, "fail_fast", False):
            raise TranslationParseError(message)

    def __init__(
        self,
        config: CProjectConfig,
        eng_type: str,
        proxy_pool: Optional[CProxyPool] = None,
        token_pool: COpenAITokenPool = None,
    ):
        """
        根据提供的类型、配置、API 密钥和代理设置初始化 Chatbot 对象。

        Args:
            config (dict, 可选): 使用 非官方API 时提供 的配置字典。默认为空字典。
            apikey (str, 可选): 使用 官方API 时的 API 密钥。默认为空字符串。
            proxy (str, 可选): 使用 官方API 时的代理 URL，非官方API的代理写在config里。默认为空字符串。

        Returns:
            None
        """
        self.pj_config = config
        self.eng_type = eng_type
        self.last_file_name = ""
        self.restore_context_mode = config.getKey("gpt.restoreContextMode", True)
        # 翻译规范：全局规范（translation_guidelines/ 里选的那份）+ 项目规范
        # （项目目录里的 translation_guideline.md，可能没有）。两份拼成一段塞进
        # prompt 的 <translation_guidelines> 段：项目规范在后、冲突时以它为准。
        # 这里只在翻译器初始化时读一次，所以改完规范要**下一次启动翻译**才生效。
        if val := config.getKey("gpt.translation_guideline"):
            guideline_file = val
        else:
            guideline_file = DEFAULT_GUIDELINE_NAME
        self.pj_config.translation_guideline = combine_guidelines(
            load_guideline_file(guideline_file),
            read_project_guideline(config.getProjectDir()),
        )
        
        # 保存间隔
        if val := config.getKey("save_steps"):
            self.save_steps = val
        else:
            self.save_steps = 1
        # 语言设置
        if val := config.getKey("language"):
            sp = val.split("2")
            self.source_lang = sp[0]
            self.target_lang = sp[-1]
        elif val := config.getKey("sourceLanguage"):  # 兼容旧版本配置
            self.source_lang = val
            self.target_lang = config.getKey("targetLanguage")
        else:
            self.source_lang = "ja"
            self.target_lang = "zh-cn"
        if self.source_lang not in LANG_SUPPORTED.keys():
            raise ValueError(
                get_text("invalid_source_language", self.target_lang, self.source_lang)
            )
        else:
            self.source_lang = LANG_SUPPORTED[self.source_lang]
        if self.target_lang not in LANG_SUPPORTED.keys():
            raise ValueError(
                get_text("invalid_target_language", self.target_lang, self.target_lang)
            )
        else:
            self.target_lang = LANG_SUPPORTED[self.target_lang]

        # 429等待时间（废弃）
        self.wait_time = config.getKey("gpt.tooManyRequestsWaitTime", 60)
        # 跳过重试
        self.skipRetry = config.getKey("skipRetry", False)
        # 跳过h
        self.skipH = config.getKey("skipH", False)

        self.tokenProvider = token_pool

        self.contextNum:int = config.getKey("gpt.contextNum", 8)

        self.smartRetry:bool=config.getKey("smartRetry", True)

        self.dynamic_num_per_request = self._coerce_bool(
            config.getKey("gpt.dynamicNumPerRequestTranslate", False)
        )
        self.dynamic_num_per_request_min = self._coerce_positive_int(
            config.getKey("gpt.dynamicNumPerRequestTranslate.min", 1), 1
        )
        self.dynamic_num_per_request_max = self._coerce_positive_int(
            config.getKey("gpt.dynamicNumPerRequestTranslate.max", 16), 16
        )
        if self.dynamic_num_per_request_min > self.dynamic_num_per_request_max:
            self.dynamic_num_per_request_min, self.dynamic_num_per_request_max = (
                self.dynamic_num_per_request_max,
                self.dynamic_num_per_request_min,
            )
        self._dynamic_num_per_request_current: Optional[int] = None
        self._dynamic_num_per_request_success_streak = 0

        metrics = getattr(config, "request_health_metrics", None)
        if metrics is None:
            metrics = RequestHealthMetrics()
            setattr(config, "request_health_metrics", metrics)
        self.request_health_metrics: RequestHealthMetrics = metrics

        backend_rpm = 0
        try:
            backend_rpm = int(
                config.getBackendConfigSection("OpenAI-Compatible").get(
                    "globalRequestRPM", 0
                )
                or 0
            )
        except Exception:
            backend_rpm = 0
        self.global_request_rpm = max(0, backend_rpm)

        if config.getKey("internals.enableProxy") == True:
            self.proxyProvider = proxy_pool
        else:
            self.proxyProvider = None

        self._current_temp_type = ""
        self._shutdown_done = False
        self._client_failure_counts: dict[int, int] = {}
        self._retired_clients: list[AsyncOpenAI] = []
        self._client_recycle_lock = asyncio.Lock()

        if self.target_lang == "Simplified_Chinese":
            self.opencc = OpenCC("t2s.json")
        elif self.target_lang == "Traditional_Chinese":
            self.opencc = OpenCC("s2tw.json")

        pass

    @staticmethod
    def _coerce_bool(value) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"true", "1", "yes", "on"}
        return bool(value)

    @staticmethod
    def _coerce_positive_int(value, default: int) -> int:
        try:
            result = int(value)
        except (TypeError, ValueError):
            result = default
        return max(1, result)

    def _get_effective_num_per_request(self, configured_value: int, proofread: bool = False) -> int:
        configured = self._coerce_positive_int(configured_value, 1)
        if proofread or not self.dynamic_num_per_request:
            return configured

        if self._dynamic_num_per_request_current is None:
            self._dynamic_num_per_request_current = min(
                self.dynamic_num_per_request_max,
                max(self.dynamic_num_per_request_min, configured),
            )
        return self._dynamic_num_per_request_current

    def _update_dynamic_num_per_request(
        self,
        requested_count: int,
        completed_count: int,
        trans_result: CTransList,
        filename: str,
        proofread: bool = False,
    ) -> None:
        if proofread or not self.dynamic_num_per_request:
            return

        current = self._get_effective_num_per_request(requested_count, proofread=False)
        failed_markers = ("(Failed)", "(翻译失败)")
        has_failed_result = any(
            any(marker in getattr(trans, "pre_dst", "") for marker in failed_markers)
            for trans in trans_result
        )
        has_parse_issue = completed_count < requested_count or has_failed_result

        if has_parse_issue:
            next_value = max(self.dynamic_num_per_request_min, max(1, current // 2))
            self._dynamic_num_per_request_success_streak = 0
            if next_value != current:
                LOGGER.warning(
                    f"[{filename}]动态句数调整：检测到解析异常，单次翻译句数 {current} -> {next_value}"
                )
                self._dynamic_num_per_request_current = next_value
            return

        if completed_count >= requested_count and requested_count == current:
            self._dynamic_num_per_request_success_streak += 1
            if (
                self._dynamic_num_per_request_success_streak >= 3
                and current < self.dynamic_num_per_request_max
            ):
                next_value = min(self.dynamic_num_per_request_max, current + 1)
                LOGGER.info(
                    f"[{filename}]动态句数调整：连续成功，单次翻译句数 {current} -> {next_value}"
                )
                self._dynamic_num_per_request_current = next_value
                self._dynamic_num_per_request_success_streak = 0

    def _apply_internal_prompt_template_overrides(self) -> None:
        """Apply runtime prompt-template overrides passed from backend service layer."""
        system_prompt_override = self.pj_config.getKey(
            "internals.prompt_template.system_prompt_override", None
        )
        user_prompt_override = self.pj_config.getKey(
            "internals.prompt_template.user_prompt_override", None
        )
        if isinstance(system_prompt_override, str):
            self.system_prompt = system_prompt_override
        if isinstance(user_prompt_override, str):
            self.trans_prompt = user_prompt_override

    def init_chatbot(self, eng_type, config: CProjectConfig):
        section_name = "OpenAI-Compatible"
        backend_config = config.getBackendConfigSection(section_name)

        self.api_timeout = backend_config.get("apiTimeout", 300)
        self.apiErrorWait = backend_config.get("apiErrorWait", "auto")
        self.tokenStrategy = backend_config.get("tokenStrategy", "random")
        self.stream = backend_config.get("stream", True)
        # Bound each logical API request so a dead endpoint cannot keep a
        # worker retrying forever. This is a total-call limit, including the
        # first call; callers may override it explicitly in ask_chatbot().
        self.max_api_retries = self._coerce_positive_int(
            backend_config.get("maxApiRetries", 6), 6
        )

        # 旧项目的 Prompt 覆盖：gpt.change_prompt / gpt.prompt_content 这对键已下线
        # （前端不再显示、Agent 读不到也改不了、新项目不再生成），这里只保留兼容：
        # 老工程配置文件里还留着的话照样生效。新的自定义入口是项目翻译规范
        # （见 ProjectGuideline / 「项目规范」页 / write_project_guideline 工具）。
        change_prompt = CProjectConfig.getProjectConfig(config)["common"].get(
            "gpt.change_prompt", "no"
        )
        prompt_content = CProjectConfig.getProjectConfig(config)["common"].get(
            "gpt.prompt_content", ""
        )
        if change_prompt == "AdditionalPrompt" and prompt_content != "":
            self.trans_prompt = (
                "# Additional Requirements: "
                + prompt_content
                + "\n"
                + self.trans_prompt
            )
        if change_prompt == "OverwritePrompt" and prompt_content != "":
            self.trans_prompt = prompt_content

        # 规范化 apiErrorWait：
        # - "auto" / 非法值 -> -1（走指数退避）
        # - 数字字符串如 "120" -> int(120)
        # 避免后续 `self.apiErrorWait >= 0` 出现 str 与 int 比较的 TypeError。
        if isinstance(self.apiErrorWait, bool):
            # bool 是 int 的子类，显式拒绝，避免 True/False 被当作 1/0
            self.apiErrorWait = -1
        elif isinstance(self.apiErrorWait, (int, float)):
            self.apiErrorWait = int(self.apiErrorWait)
        else:
            raw_wait = str(self.apiErrorWait).strip().lower()
            if raw_wait == "auto" or raw_wait == "":
                self.apiErrorWait = -1
            else:
                try:
                    self.apiErrorWait = int(float(raw_wait))
                except (TypeError, ValueError):
                    self.apiErrorWait = -1

        if self.proxyProvider:
            proxy_addr = self.proxyProvider.getProxy().addr
        else:
            proxy_addr = None

        self.client_list = []
        for token in self.tokenProvider.get_available_token():
            http_client = self._build_http_client(proxy_addr)

            client = AsyncOpenAI(
                api_key=token.token,
                base_url=token.domain,
                max_retries=0,
                http_client=http_client,
            )
            self.client_list.append((client, token))

        pass

    @staticmethod
    def _build_http_client(proxy_addr: Optional[str] = None):
        """Build a bounded HTTP client and expire idle connections regularly."""
        proxy_kwargs = build_httpx_proxy_kwargs(proxy_addr)
        if HttpxTransport is not None and not proxy_kwargs:
            try:
                pyreqwest_client = None
                if PyreqwestClientBuilder is not None:
                    pyreqwest_client = (
                        PyreqwestClientBuilder()
                        .pool_idle_timeout(timedelta(seconds=30))
                        .pool_max_idle_per_host(20)
                        .build()
                    )
                return httpx.AsyncClient(
                    trust_env=False,
                    limits=httpx.Limits(
                        max_keepalive_connections=20,
                        max_connections=100,
                        keepalive_expiry=30.0,
                    ),
                    transport=HttpxTransport(pyreqwest_client)
                    if pyreqwest_client is not None
                    else HttpxTransport(),
                )
            except Exception as exc:
                LOGGER.warning(
                    "初始化 pyreqwest HttpxTransport 失败，回退 DefaultAioHttpClient: %s",
                    exc,
                )
        elif HttpxTransport is not None and proxy_kwargs:
            LOGGER.warning(
                "检测到代理配置，当前回退到 DefaultAioHttpClient（pyreqwest transport 路径未启用代理注入）"
            )

        return DefaultAioHttpClient(
            trust_env=False,
            limits=httpx.Limits(
                max_keepalive_connections=20,
                max_connections=100,
                keepalive_expiry=30.0,
            ),
            **proxy_kwargs,
        )

    @staticmethod
    def _is_transport_error(error: BaseException) -> bool:
        """Return whether an error is likely caused by a broken connection.

        API status errors (bad request, auth, quota, etc.) must not cause us to
        churn clients. Connection and timeout errors are safe to recover from
        by replacing the underlying HTTP client.
        """
        return isinstance(
            error,
            (
                APIConnectionError,
                APITimeoutError,
                httpx.TransportError,
                TimeoutError,
                ConnectionError,
                OSError,
            ),
        )

    async def _recycle_failed_client(
        self, failed_client: AsyncOpenAI, token: COpenAIToken
    ) -> Optional[AsyncOpenAI]:
        """Replace one unhealthy client without interrupting other workers."""
        if self._shutdown_done:
            return None
        async with self._client_recycle_lock:
            current = next(
                (
                    pair
                    for pair in self.client_list
                    if pair[0] is failed_client and pair[1] is token
                ),
                None,
            )
            if current is None:
                return None

            proxy_addr = None
            if self.proxyProvider:
                try:
                    proxy_addr = self.proxyProvider.getProxy().addr
                except Exception:
                    proxy_addr = None
            http_client = self._build_http_client(proxy_addr)

            replacement = AsyncOpenAI(
                api_key=token.token,
                base_url=token.domain,
                max_retries=0,
                http_client=http_client,
            )
            self.client_list = [
                (replacement if client is failed_client else client, pair_token)
                for client, pair_token in self.client_list
            ]
            self._retired_clients.append(failed_client)
            LOGGER.warning("连续网络错误，已刷新 API 客户端 [%s]", token.maskToken())
            return replacement

    def _get_chatbot_state(self) -> tuple[bool, str]:
        """Get request-local metadata, with compatibility fallback for tests/plugins."""
        return _CHATBOT_STATE.get() or (
            getattr(self, "_last_chatbot_was_stream", False),
            getattr(self, "_last_chatbot_model_name", ""),
        )

    @staticmethod
    def _clear_chatbot_state() -> None:
        _CHATBOT_STATE.set(None)

    @staticmethod
    def _is_stop_requested(pj_config) -> bool:
        stop_event = getattr(pj_config, "stop_event", None)
        return stop_event is not None and stop_event.is_set()

    def _check_stop_requested(self) -> None:
        if self._is_stop_requested(self.pj_config):
            from GalTransl.Service import JobCancelledError

            raise JobCancelledError()

    @staticmethod
    def _build_idx_tip(trans_list: CTransList) -> str:
        start_idx = trans_list[0].index
        end_idx = trans_list[-1].index
        if start_idx != end_idx:
            return f"{start_idx}~{end_idx}"
        return str(start_idx)

    def _build_prompt_request(self, input_src: str, gptdict: str) -> str:
        prompt_req = self.trans_prompt
        prompt_req = prompt_req.replace(
            "[translation_guideline]", self.pj_config.translation_guideline
        )
        prompt_req = prompt_req.replace("[Input]", input_src)
        prompt_req = prompt_req.replace("[Glossary]", gptdict)
        prompt_req = prompt_req.replace("[SourceLang]", self.source_lang)
        prompt_req = prompt_req.replace("[TargetLang]", self.target_lang)
        return prompt_req

    def _apply_history_result(self, prompt_req: str, filename: str) -> str:
        if (
            hasattr(self, "last_translations")
            and filename in self.last_translations
            and self.last_translations[filename] != ""
        ):
            history_result = self.last_translations[filename].replace("<br>", "")
            return prompt_req.replace("[history_result]", history_result)
        return prompt_req.replace("[history_result]", "None")

    def _record_runtime_success(self, filename: str, trans: CSentense) -> None:
        try:
            from GalTransl.server import record_runtime_success

            record_runtime_success(
                getattr(
                    self.pj_config,
                    "runtime_project_dir",
                    self.pj_config.getProjectDir(),
                ),
                filename=filename,
                index=getattr(trans, "runtime_index", getattr(trans, "index", 0)),
                speaker=getattr(trans, "speaker", None),
                source_preview=getattr(trans, "post_src", ""),
                translation_preview=getattr(trans, "pre_dst", ""),
                trans_by=getattr(trans, "trans_by", ""),
            )
        except Exception:
            pass

    def _normalize_parsed_translation_text(
        self, line_dst: str, current_tran: CSentense, n_symbol: str
    ) -> str:
        if "Chinese" in self.target_lang:
            line_dst = self.opencc.convert(line_dst)

        if "”" not in current_tran.post_src and '"' not in current_tran.post_src:
            line_dst = line_dst.replace('"', "")
        elif '"' not in current_tran.post_src and '"' in line_dst:
            line_dst = fix_quotes2(line_dst)
        elif '"' in current_tran.post_src and "”" in line_dst:
            line_dst = line_dst.replace("“", '"')
            line_dst = line_dst.replace("”", '"')

        if not line_dst.startswith("「") and current_tran.post_src.startswith("「"):
            line_dst = "「" + line_dst
        if not line_dst.endswith("」") and current_tran.post_src.endswith("」"):
            line_dst = line_dst + "」"

        line_dst = line_dst.replace("[t]", "\t")
        if n_symbol:
            line_dst = line_dst.replace("<br>", n_symbol)
            line_dst = line_dst.replace("<BR>", n_symbol)

        if "……" in current_tran.post_src and "..." in line_dst:
            line_dst = line_dst.replace("......", "……")
            line_dst = line_dst.replace("...", "……")

        return line_dst

    def _append_parsed_translation_result(
        self,
        current_tran: CSentense,
        line_dst: str,
        model_name: str,
        cursor: dict,
        result_trans_list: list,
        filename: str = "",
        emit_runtime_success: bool = False,
        emitted_success_indices=None,
        result_index: Optional[int] = None,
    ) -> tuple[bool, str]:
        current_tran.pre_dst = line_dst
        current_tran.post_dst = line_dst
        current_tran.trans_by = model_name
        if emit_runtime_success:
            if emitted_success_indices is None:
                emitted_success_indices = set()
            if result_index is not None and result_index not in emitted_success_indices:
                self._record_runtime_success(filename, current_tran)
                emitted_success_indices.add(result_index)
            current_tran._runtime_success_recorded = True
        result_trans_list.append(current_tran)
        cursor["success_count"] = cursor.get("success_count", 0) + 1
        return True, ""

    @staticmethod
    def _merge_problem_message(
        current_problem: str, message: str, append: bool = True
    ) -> str:
        current_problem = current_problem or ""
        message = message or ""
        if not message:
            return current_problem
        if not append:
            return message
        if message in current_problem:
            return current_problem
        if not current_problem:
            return message
        return f"{current_problem}, {message}"

    def _append_parse_failure_fallback_results(
        self,
        trans_list: CTransList,
        start_index: int,
        result_trans_list: list,
        model_name: str,
        proofread: bool = False,
        translate_failed_prefix: str = "(Failed)",
        translate_problem_message: str = "翻译失败",
        proofread_problem_message: str = "翻译失败",
        proofread_problem_append: bool = True,
    ) -> int:
        i = max(0, start_index)
        failed_model_name = f"{model_name}(Failed)"
        while i < len(trans_list):
            current_tran = trans_list[i]
            if not proofread:
                failed_text = translate_failed_prefix + current_tran.post_src
                current_tran.pre_dst = failed_text
                current_tran.post_dst = failed_text
                current_tran.problem = self._merge_problem_message(
                    current_tran.problem, translate_problem_message, append=True
                )
                current_tran.trans_by = failed_model_name
            else:
                current_tran.proofread_zh = current_tran.pre_dst
                current_tran.post_dst = current_tran.pre_dst
                current_tran.problem = self._merge_problem_message(
                    current_tran.problem,
                    proofread_problem_message,
                    append=proofread_problem_append,
                )
                current_tran.proofread_by = failed_model_name
            result_trans_list.append(current_tran)
            i += 1
        return i

    async def _batch_translate_common(
        self,
        filename,
        cache_file_path,
        translist_unhit: CTransList,
        num_pre_request: int,
        gpt_dic: CGptDict = None,
        proofread: bool = False,
        glossary_style: str = "",
        failed_markers: tuple[str, ...] = ("(Failed)",),
        h_words_list: Optional[List[str]] = None,
        ensure_last_translations: bool = False,
    ) -> CTransList:
        if len(translist_unhit) == 0:
            return []

        if self.skipH and h_words_list:
            translist_unhit = [
                tran
                for tran in translist_unhit
                if not any(word in tran.post_src for word in h_words_list)
            ]

        if ensure_last_translations and hasattr(self, "last_translations"):
            if filename not in self.last_translations:
                self.last_translations[filename] = ""

        i = 0
        trans_result_list = []
        len_trans_list = len(translist_unhit)
        transl_step_count = 0

        while i < len_trans_list:
            self._check_stop_requested()
            effective_num_pre_request = self._get_effective_num_per_request(
                num_pre_request,
                proofread=proofread,
            )
            trans_list_split = (
                translist_unhit[i : i + effective_num_pre_request]
                if (i + effective_num_pre_request < len_trans_list)
                else translist_unhit[i:]
            )

            if gpt_dic:
                if glossary_style:
                    dic_prompt = gpt_dic.gen_prompt(trans_list_split, glossary_style)
                else:
                    dic_prompt = gpt_dic.gen_prompt(trans_list_split)
            else:
                dic_prompt = ""

            try:
                num, trans_result = await self.translate(
                    trans_list_split,
                    dic_prompt,
                    proofread=proofread,
                    filename=filename,
                )
            except Exception as exc:
                from GalTransl.Service import JobCancelledError

                if isinstance(exc, JobCancelledError):
                    raise
                LOGGER.error(
                    f"[{filename}:{self._build_idx_tip(trans_list_split)}]批次翻译失败，跳过该批次：{exc}"
                )
                try:
                    from GalTransl.server import record_runtime_error

                    record_runtime_error(
                        getattr(
                            self.pj_config,
                            "runtime_project_dir",
                            self.pj_config.getProjectDir(),
                        ),
                        kind="api",
                        message=str(exc),
                        filename=filename,
                        index_range=self._build_idx_tip(trans_list_split),
                        level="error",
                    )
                except Exception:
                    pass
                trans_result = []
                self._append_parse_failure_fallback_results(
                    trans_list_split,
                    0,
                    trans_result,
                    "",
                    proofread=proofread,
                    translate_failed_prefix="(Failed)",
                    translate_problem_message="翻译失败",
                )
                num = len(trans_result)

            if num <= 0 and not trans_result:
                LOGGER.warning(
                    f"[{filename}:{self._build_idx_tip(trans_list_split)}] translate returned no progress, retrying once"
                )
                self._check_stop_requested()
                await self._interruptible_sleep(1)
                # 重试一次
                try:
                    num, trans_result = await self.translate(
                        trans_list_split,
                        dic_prompt,
                        proofread=proofread,
                        filename=filename,
                    )
                except Exception as exc:
                    from GalTransl.Service import JobCancelledError

                    if isinstance(exc, JobCancelledError):
                        raise
                    LOGGER.error(
                        f"[{filename}:{self._build_idx_tip(trans_list_split)}]重试批次失败，跳过该批次：{exc}"
                    )
                    trans_result = []
                    self._append_parse_failure_fallback_results(
                        trans_list_split,
                        0,
                        trans_result,
                        "",
                        proofread=proofread,
                        translate_failed_prefix="(Failed)",
                        translate_problem_message="翻译失败",
                    )
                    num = len(trans_result)

            if num <= 0 and not trans_result:
                LOGGER.error(
                    f"[{filename}:{self._build_idx_tip(trans_list_split)}] translate returned no progress after retry, marking batch as failed"
                )
                fallback_list = []
                # 按翻译失败处理
                self._append_parse_failure_fallback_results(
                    trans_list_split,
                    0,
                    fallback_list,
                    "",
                    proofread=proofread,
                    translate_failed_prefix="(Failed)",
                    translate_problem_message="翻译失败",
                )
                trans_result = fallback_list
                num = len(fallback_list)

            if num > 0:
                i += num
            self.pj_config.bar(num)
            self._update_dynamic_num_per_request(
                requested_count=len(trans_list_split),
                completed_count=max(0, num),
                trans_result=trans_result,
                filename=filename,
                proofread=proofread,
            )

            result_output = ""
            for trans in trans_result:
                if (
                    not proofread
                    and trans.pre_dst
                    and not getattr(trans, "_runtime_success_recorded", False)
                    and not any(marker in trans.pre_dst for marker in failed_markers)
                ):
                    self._record_runtime_success(filename, trans)
                result_output += repr(trans)

            LOGGER.info(result_output)
            trans_result_list += trans_result
            transl_step_count += 1
            if transl_step_count >= self.save_steps:
                runtime_project_dir = getattr(
                    self.pj_config, "runtime_project_dir", None
                )
                project_dir = (
                    runtime_project_dir
                    if runtime_project_dir is not None
                    else self.pj_config.getProjectDir()
                )
                await save_transCache_to_json(
                    trans_result,
                    cache_file_path,
                    project_dir=project_dir,
                )
                transl_step_count = 0

            if trans_result:
                trans_by = trans_result[0].trans_by
                LOGGER.info(
                    f"{filename}: {str(len(trans_result_list))}/{str(len_trans_list)} with {trans_by}"
                )

        return trans_result_list

    async def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep that can be interrupted by stop_event.

        Instead of blocking for the full duration, we check every 0.5s
        so that a stop request is honoured promptly.
        """
        remaining = seconds
        while remaining > 0:
            if self._is_stop_requested(self.pj_config):
                from GalTransl.Service import JobCancelledError
                raise JobCancelledError()
            chunk = min(remaining, 0.5)
            await asyncio.sleep(chunk)
            remaining -= chunk

    async def _wait_for_global_rpm_slot(self) -> None:
        if self.global_request_rpm <= 0:
            return

        global _GLOBAL_NEXT_ALLOWED_TS
        interval = 60.0 / float(self.global_request_rpm)
        wait_seconds = 0.0

        with _GLOBAL_RPM_LOCK:
            now = time.monotonic()
            if now >= _GLOBAL_NEXT_ALLOWED_TS:
                _GLOBAL_NEXT_ALLOWED_TS = now + interval
                wait_seconds = 0.0
            else:
                wait_seconds = _GLOBAL_NEXT_ALLOWED_TS - now
                _GLOBAL_NEXT_ALLOWED_TS = _GLOBAL_NEXT_ALLOWED_TS + interval

        if wait_seconds > 0:
            await self._interruptible_sleep(wait_seconds)

    def _record_request_health(self, latency_seconds: float, is_rate_limited: bool) -> None:
        try:
            self.request_health_metrics.record(latency_seconds, is_rate_limited)
        except Exception:
            return

    async def ask_chatbot(
        self,
        prompt="",
        system="",
        messages=None,
        temperature=NOT_GIVEN,
        frequency_penalty=NOT_GIVEN,
        top_p=NOT_GIVEN,
        stream=NOT_GIVEN,
        max_tokens=NOT_GIVEN,
        reasoning_effort=NOT_GIVEN,
        file_name="",
        base_try_count=0,
        stream_line_callback=None,
        max_retry_count: Optional[int] = None,
        reasoning_holder: Optional[dict] = None,
        progress_file: str = "",
        tools=NOT_GIVEN,
        tool_response_holder: Optional[dict] = None,
        tool_choice=NOT_GIVEN,
    ):
        """发一次请求并返回 (正文, token)。

        传了 reasoning_holder 时，把这次响应的思考内容填进去：{"field": 字段名, "text": 思考全文}。
        多轮对话的调用方要拿它跟着 assistant 消息回传给 provider（见 REASONING_FIELD_NAMES）。

        progress_file 是这次请求属于哪个文件：工作台「文件进度」那一行的小灯靠它点（请求中黄、
        思考中蓝、翻译中绿、重试中红，见 _FileRequestProgress）。留空就不报。
        """
        progress = _FileRequestProgress(self.pj_config, progress_file)
        try:
            # 按类取而不是 self._ask_chatbot：测试里常拿一个只挂了几个属性的替身当 self 调
            return await BaseTranslate._ask_chatbot(
                self,
                prompt=prompt,
                system=system,
                messages=messages,
                temperature=temperature,
                frequency_penalty=frequency_penalty,
                top_p=top_p,
                stream=stream,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
                file_name=file_name,
                base_try_count=base_try_count,
                stream_line_callback=stream_line_callback,
                max_retry_count=max_retry_count,
                reasoning_holder=reasoning_holder,
                progress=progress,
                tools=tools,
                tool_response_holder=tool_response_holder,
                tool_choice=tool_choice,
            )
        finally:
            progress.close()

    async def _ask_chatbot(
        self,
        *,
        prompt,
        system,
        messages,
        temperature,
        frequency_penalty,
        top_p,
        stream,
        max_tokens,
        reasoning_effort,
        file_name,
        base_try_count,
        stream_line_callback,
        max_retry_count: Optional[int],
        reasoning_holder: Optional[dict],
        progress: _FileRequestProgress,
        tools=NOT_GIVEN,
        tool_response_holder: Optional[dict] = None,
        tool_choice=NOT_GIVEN,
    ):
        """ask_chatbot 的本体：带重试地发请求，边收边报 progress。"""
        if max_retry_count is None:
            max_retry_count = getattr(self, "max_api_retries", None)
        if max_retry_count is not None:
            max_retry_count = self._coerce_positive_int(max_retry_count, 6)

        # api_try_count controls token rotation and preserves the caller's
        # existing base offset. api_attempts is independent so parse retries
        # do not accidentally consume the API attempt budget.
        api_try_count = base_try_count
        api_attempts = 0
        client: AsyncOpenAI
        token: COpenAIToken
        client, token = random.choices(self.client_list, k=1)[0]
        if messages is None:
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ]

        if "gemini" in token.model_name:
            temperature = NOT_GIVEN

        while True:
            # Check stop_event before each API attempt so that cancelling
            # the job actually works even when we are stuck in an API-error
            # retry loop with long backoff sleeps.
            if self._is_stop_requested(self.pj_config):
                from GalTransl.Service import JobCancelledError
                raise JobCancelledError()

            progress.attempt()
            request_started = time.monotonic()
            try:
                if self.tokenStrategy == "random":
                    if api_try_count % 2 == 0:
                        client, token = random.choices(self.client_list, k=1)[0]
                elif self.tokenStrategy == "fallback":
                    index = api_try_count % len(self.client_list)
                    client, token = self.client_list[index]
                else:
                    raise ValueError("tokenStrategy must be random or fallback")
                is_stream=stream if stream != NOT_GIVEN else token.stream
                self._last_chatbot_was_stream = bool(is_stream)
                self._last_chatbot_model_name = getattr(token, "model_name", "")
                _CHATBOT_STATE.set(
                    (bool(is_stream), getattr(token, "model_name", ""))
                )
                LOGGER.debug(f"Call {token.domain} withs token {token.maskToken()}")

                await self._wait_for_global_rpm_slot()

                # Create the API call as a task so we can cancel it if
                # the user requests a stop while the request is in-flight.
                LOGGER.info(f"timeout: {self.api_timeout}")
                api_task = asyncio.ensure_future(
                    client.chat.completions.create(
                        model=token.model_name,
                        messages=messages,
                        stream=is_stream,
                        temperature=temperature,
                        frequency_penalty=frequency_penalty,
                        max_tokens=max_tokens,
                        timeout=self.api_timeout,
                        top_p=top_p,
                        reasoning_effort=reasoning_effort,
                        **({"tools": tools, "tool_choice": "auto" if tool_choice is NOT_GIVEN else tool_choice} if tools is not NOT_GIVEN else {}),
                    )
                )

                # Poll stop_event while waiting for the API response.
                # This ensures that a stop request is detected within 0.5s
                # even when the LLM endpoint is slow or unresponsive.
                while not api_task.done():
                    if self._is_stop_requested(self.pj_config):
                        api_task.cancel()
                        with suppress(BaseException):
                            try:
                                await asyncio.wait_for(
                                    asyncio.shield(api_task), timeout=2.0
                                )
                            except (asyncio.TimeoutError, asyncio.CancelledError):
                                pass
                        from GalTransl.Service import JobCancelledError
                        raise JobCancelledError()
                    done, _ = await asyncio.wait({api_task}, timeout=0.5)
                    if done:
                        break

                response = api_task.result()
                result = ""
                lastline = ""
                reasoning_parts: List[str] = []
                reasoning_field = ""
                tool_calls = {}
                finish_reason = None
                if is_stream:
                    stream_abort_requested = False
                    stream_line_buffer = ""
                    stream_completed = False
                    # 正文开头若是 <think>…</think>（思考写在正文里的接口），那一段也算「思考中」
                    think_detector = _InlineThinkDetector()
                    try:
                        async for chunk in response:
                            # Check stop in the middle of streaming so we don't
                            # have to wait for the entire stream to finish.
                            if self._is_stop_requested(self.pj_config):
                                stream_abort_requested = True
                                from GalTransl.Service import JobCancelledError
                                raise JobCancelledError()
                            if not chunk.choices:
                                continue
                            finish_reason = getattr(chunk.choices[0], "finish_reason", None) or finish_reason
                            if tool_response_holder is not None:
                                for call in getattr(chunk.choices[0].delta, "tool_calls", None) or []:
                                    slot = tool_calls.setdefault(call.index, {"id": ""})
                                    if call.id:
                                        slot["id"] = call.id
                                    custom = getattr(call, "custom", None)
                                    if custom:
                                        slot["type"] = "custom"
                                        target = slot.setdefault("custom", {"name": "", "input": ""})
                                        target["name"] += custom.get("name") or ""
                                        piece = custom.get("input") or ""
                                        target["input"] += piece
                                        progress.output("writing", len(piece))
                                    if call.function:
                                        slot["type"] = "function"
                                        slot.setdefault("function", {"name": "", "arguments": ""})
                                        slot["function"]["name"] += call.function.name or ""
                                        piece = call.function.arguments or ""
                                        slot["function"]["arguments"] += piece
                                        progress.output("writing", len(piece))
                            reasoning_piece, hit_field = _extract_reasoning(
                                chunk.choices[0].delta
                            )
                            if reasoning_piece:
                                # 思考内容照旧打进 lastline 供终端打印，同时攒起来交给
                                # 多轮对话的调用方回传（不回传下一次请求会被判 400）
                                if not reasoning_field:
                                    reasoning_field = hit_field
                                reasoning_parts.append(reasoning_piece)
                                lastline = lastline + reasoning_piece
                                progress.output("thinking", len(reasoning_piece))
                            if hasattr(chunk.choices[0].delta, "content"):
                                content_piece = chunk.choices[0].delta.content or ""
                                result = result + content_piece
                                lastline = lastline + content_piece
                                stream_line_buffer += content_piece
                                if content_piece:
                                    progress.output(
                                        think_detector.feed(content_piece), len(content_piece)
                                    )
                                if stream_line_callback and "\n" in stream_line_buffer:
                                    line_parts = stream_line_buffer.split("\n")
                                    finished_lines = line_parts[:-1]
                                    stream_line_buffer = line_parts[-1]
                                    try:
                                        callback_result = stream_line_callback(
                                            finished_lines, False
                                        )
                                        if callback_result is False:
                                            stream_abort_requested = True
                                            break
                                    except Exception:
                                        pass
                            if "\n" in lastline:
                                if should_print_translation_logs(self.pj_config) and self.pj_config.active_workers == 1:
                                    lastline_sp = lastline.split("\n")
                                    print("\n".join(lastline_sp[:-1]))
                                    lastline = lastline_sp[-1]
                        stream_completed = True
                        if stream_line_callback and stream_line_buffer:
                            try:
                                callback_result = stream_line_callback(
                                    [stream_line_buffer], True
                                )
                                if callback_result is False:
                                    stream_abort_requested = True
                            except Exception:
                                pass
                    finally:
                        if not stream_completed or stream_abort_requested:
                            close_stream = getattr(response, "aclose", None)
                            if callable(close_stream):
                                try:
                                    await asyncio.wait_for(close_stream(), timeout=3.0)
                                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                                    pass
                else:
                    if tool_response_holder is not None and response.choices:
                        finish_reason = response.choices[0].finish_reason
                        tool_calls = {
                            i: call.model_dump(exclude_none=True)
                            for i, call in enumerate(response.choices[0].message.tool_calls or [])
                        }
                    try:
                        result = response.choices[0].message.content
                    except:
                        raise ValueError(
                            "response.choices[0].message.content is None, no_candidates"
                        )
                    if (not isinstance(result, str) or result.strip() == "") and not tool_calls:
                        raise ValueError(
                            "response.choices[0].message.content is empty"
                        )
                    reasoning_piece, hit_field = _extract_reasoning(
                        response.choices[0].message
                    )
                    if reasoning_piece:
                        reasoning_field = reasoning_field or hit_field
                        reasoning_parts.append(reasoning_piece)
                if tool_response_holder is not None:
                    tool_response_holder.clear()
                    tool_response_holder.update(
                        tool_calls=[call for _, call in sorted(tool_calls.items())],
                        finish_reason=finish_reason,
                    )
                self._record_request_health(
                    time.monotonic() - request_started,
                    is_rate_limited=False,
                )
                getattr(self, "_client_failure_counts", {}).pop(id(client), None)
                if reasoning_holder is not None:
                    reasoning_holder["field"] = reasoning_field or REASONING_FIELD_NAMES[0]
                    reasoning_holder["text"] = "".join(reasoning_parts)
                return result, token
            except Exception as e:
                is_rate_limited = isinstance(e, RateLimitError)
                if BaseTranslate._is_transport_error(e):
                    failure_counts = getattr(self, "_client_failure_counts", None)
                    if failure_counts is not None:
                        client_key = id(client)
                        failure_counts[client_key] = failure_counts.get(client_key, 0) + 1
                        if failure_counts[client_key] >= 3:
                            try:
                                replacement = await self._recycle_failed_client(client, token)
                                if replacement is not None:
                                    client = replacement
                            except Exception:
                                LOGGER.debug("刷新失败的 API 客户端时出错", exc_info=True)
                            failure_counts.pop(client_key, None)
                self._record_request_health(
                    time.monotonic() - request_started,
                    is_rate_limited=is_rate_limited,
                )

                from GalTransl.Service import JobCancelledError
                if isinstance(e, JobCancelledError):
                    raise

                if getattr(self, "fail_fast", False):
                    raise TranslationRequestError(str(e)) from e

                api_try_count += 1
                api_attempts += 1
                if max_retry_count is not None and api_attempts >= max_retry_count:
                    # 把最后一次的真实错误带上：调用方（_batch_translate_common / 运行时错误
                    # 记录）只看得到这一句，不带上原因就还得回上游日志翻为什么
                    last_error = str(e).strip() or type(e).__name__
                    raise RuntimeError(
                        f"API请求达到重试上限 ({max_retry_count})，最后一次错误：{last_error}"
                    ) from e

                # gemini no_candidates
                if "candidates" in str(e) and api_try_count > 1:
                    return "", token
                if self.apiErrorWait >= 0:
                    sleep_time = self.apiErrorWait + random.random()
                else:
                    # https://aws.amazon.com/cn/blogs/architecture/exponential-backoff-and-jitter/
                    sleep_time = 2 ** min(api_attempts, 6)
                    sleep_time = random.randint(0, sleep_time)

                if len(self.client_list) > 1:
                    token_info = f"[{token.maskToken()}]"
                else:
                    token_info = ""

                if is_rate_limited:
                    self.pj_config.bar.text(
                        "-> 检测到频率限制(429 RateLimitError)，翻译仍在进行中但速度将受影响..."
                    )
                else:
                    if file_name != "" and file_name[:1] != "[":
                        file_name = f"[{file_name}]"
                    raw_file_name = file_name[1:-1] if file_name.startswith("[") and file_name.endswith("]") else file_name
                    error_parts = []
                    exception_type = type(e).__name__
                    exception_text = str(e).strip()
                    if exception_text:
                        error_parts.append(f"{exception_type}: {exception_text}")
                    else:
                        error_parts.append(exception_type)

                    api_error_text = ""
                    try:
                        raw_api_error = response.model_extra.get("error")
                        if isinstance(raw_api_error, dict):
                            api_error_text = str(
                                raw_api_error.get("message")
                                or raw_api_error.get("code")
                                or raw_api_error
                            ).strip()
                        elif raw_api_error is not None:
                            api_error_text = str(raw_api_error).strip()
                    except Exception:
                        pass

                    if api_error_text:
                        error_parts.append(f"API返回: {api_error_text}")

                    message_text = " | ".join(part for part in error_parts if part)
                    message_text = f"{message_text} | sleeping {sleep_time:.3f}s"
                    LOGGER.warning(
                        f"[API Error]{token_info}{file_name} {message_text}"
                    )

                    try:
                        from GalTransl.server import record_runtime_error
                        record_runtime_error(
                            getattr(self.pj_config, "runtime_project_dir", self.pj_config.getProjectDir()),
                            kind="api",
                            message=message_text,
                            filename=raw_file_name,
                            retry_count=api_attempts,
                            model=getattr(token, "model_name", ""),
                            sleep_seconds=float(sleep_time),
                            level="warning",
                        )
                    except Exception:
                        pass

                progress.retrying()
                await self._interruptible_sleep(sleep_time)

    def clean_up(self):
        pass

    async def shutdown(self):
        # 用 getattr 兜底：子类可以整个覆写 __init__（如 CRebuildTranslate 不持有模型客户端），
        # 那样基类这套标记就不存在——关闭是收尾动作，不该因为"没跑过基类构造"抛异常。
        if getattr(self, "_shutdown_done", False):
            return
        self._shutdown_done = True

        clients = [client for client, _ in getattr(self, "client_list", [])]
        retired_clients = getattr(self, "_retired_clients", [])
        clients.extend(retired_clients)
        seen_clients: set[int] = set()
        for client in clients:
            if id(client) in seen_clients:
                continue
            seen_clients.add(id(client))
            if client is None:
                continue

            close_callable = getattr(client, "close", None)
            if callable(close_callable):
                try:
                    maybe_coro = close_callable()
                    if asyncio.iscoroutine(maybe_coro):
                        try:
                            await asyncio.wait_for(maybe_coro, timeout=3.0)
                        except (asyncio.TimeoutError, asyncio.CancelledError):
                            pass
                    continue
                except Exception:
                    pass

            aclose_callable = getattr(client, "aclose", None)
            if callable(aclose_callable):
                try:
                    maybe_coro = aclose_callable()
                    if asyncio.iscoroutine(maybe_coro):
                        try:
                            await asyncio.wait_for(maybe_coro, timeout=3.0)
                        except (asyncio.TimeoutError, asyncio.CancelledError):
                            pass
                except Exception:
                    pass
        retired_clients.clear()

    def translate(self, trans_list: CTransList, gptdict=""):
        pass

    async def batch_translate(
        self,
        filename,
        cache_file_path,
        trans_list: CTransList,
        num_pre_request: int,
        retry_failed: bool = False,
        gpt_dic: CGptDict = None,
        proofread: bool = False,
        retran_key: str = "",
    ) -> CTransList:
        translist_unhit = list(trans_list)

        if self.skipH:
            LOGGER.warning("skipH: 将跳过含有敏感词的句子")
            h_words_list = globals().get("H_WORDS_LIST", [])
            translist_unhit = [
                tran
                for tran in translist_unhit
                if not any(word in tran.post_src for word in h_words_list)
            ]

        if len(translist_unhit) == 0:
            return []
        # 新文件重置chatbot
        if self.last_file_name != filename:
            self.reset_conversation()
            self.last_file_name = filename
        i = 0

        trans_result_list = []
        len_trans_list = len(translist_unhit)
        transl_step_count = 0
        while i < len_trans_list:
            # await asyncio.sleep(1)
            trans_list_split = (
                translist_unhit[i : i + num_pre_request]
                if (i + num_pre_request < len_trans_list)
                else translist_unhit[i:]
            )

            dic_prompt = gpt_dic.gen_prompt(trans_list_split) if gpt_dic else ""

            num, trans_result = await self.translate(
                trans_list_split, dic_prompt, proofread=proofread
            )

            if num > 0:
                i += num
            result_output = ""
            for trans in trans_result:
                result_output = result_output + repr(trans)
            if should_print_translation_logs(self.pj_config):
                LOGGER.info(result_output)
            trans_result_list += trans_result
            transl_step_count += 1
            if transl_step_count >= self.save_steps:
                runtime_project_dir = getattr(
                    self.pj_config, "runtime_project_dir", None
                )
                project_dir = (
                    runtime_project_dir
                    if runtime_project_dir is not None
                    else self.pj_config.getProjectDir()
                )
                await save_transCache_to_json(
                    trans_result,
                    cache_file_path,
                    project_dir=project_dir,
                )
                transl_step_count = 0
            if should_print_translation_logs(self.pj_config):
                LOGGER.info(
                    f"{filename}: {str(len(trans_result_list))}/{str(len_trans_list)}"
                )

        return trans_result_list

    def _get_restore_context_failed_markers(self) -> tuple[str, ...]:
        return ("(Failed)",)

    def _format_restore_context_line(self, current_tran: CSentense) -> str:
        raise NotImplementedError

    def _format_restore_context_payload(self, lines: List[str]) -> str:
        return "\n".join(lines)

    def _collect_restore_context_items(
        self, translist_unhit: CTransList, num_pre_request: int
    ) -> List[CSentense]:
        if translist_unhit[0].prev_tran == None:
            return []

        context_items: List[CSentense] = []
        num_count = 0
        current_tran = translist_unhit[0].prev_tran
        failed_markers = self._get_restore_context_failed_markers()

        while current_tran != None:
            if current_tran.pre_dst == "" or any(
                marker in current_tran.pre_dst for marker in failed_markers
            ):
                current_tran = current_tran.prev_tran
                continue

            context_items.append(current_tran)
            num_count += 1
            if num_count >= num_pre_request:
                break
            current_tran = current_tran.prev_tran

        context_items.reverse()
        return context_items

    def restore_context(
        self, translist_unhit: CTransList, num_pre_request: int, filename=""
    ):
        if not hasattr(self, "last_translations"):
            return

        context_items = self._collect_restore_context_items(
            translist_unhit, num_pre_request
        )
        if not context_items:
            self.last_translations[filename] = ""
            return

        context_lines = [
            self._format_restore_context_line(current_tran)
            for current_tran in context_items
        ]
        self.last_translations[filename] = self._format_restore_context_payload(
            context_lines
        )

    def _set_temp_type(self, style_name: str):
        if self._current_temp_type == style_name:
            return
        self._current_temp_type = style_name
        temperature = 0.6
        frequency_penalty = NOT_GIVEN
        self.temperature = temperature
        self.frequency_penalty = frequency_penalty
