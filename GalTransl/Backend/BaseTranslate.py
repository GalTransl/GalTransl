import asyncio
import httpx
from opencc import OpenCC
from typing import Optional
from collections import deque
from threading import Lock
from GalTransl.COpenAI import COpenAITokenPool, COpenAIToken
from GalTransl.ConfigHelper import CProxyPool
from GalTransl import LOGGER, LANG_SUPPORTED, TRANSLATOR_DEFAULT_ENGINE
from GalTransl.i18n import get_text, GT_LANG
from GalTransl.ConfigHelper import (
    CProjectConfig,
)
from GalTransl.CSentense import CSentense, CTransList
from GalTransl.Cache import save_transCache_to_json
from GalTransl.Dictionary import CGptDict
from GalTransl.Utils import load_guideline_file
from openai import RateLimitError, AsyncOpenAI
from openai import DefaultAioHttpClient
from openai._types import NOT_GIVEN
import random
import time
from GalTransl.TerminalOutput import should_print_translation_logs


_GLOBAL_RPM_LOCK = Lock()
_GLOBAL_NEXT_ALLOWED_TS = 0.0


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


class BaseTranslate:
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
        # 翻译规范
        if val := config.getKey("gpt.translation_guideline"):
            guideline_file = val
        else:
            guideline_file = "日译中_基础.md"
        self.pj_config.translation_guideline=load_guideline_file(guideline_file)
        
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

        if self.target_lang == "Simplified_Chinese":
            self.opencc = OpenCC("t2s.json")
        elif self.target_lang == "Traditional_Chinese":
            self.opencc = OpenCC("s2tw.json")

        pass

    def init_chatbot(self, eng_type, config: CProjectConfig):
        section_name = "OpenAI-Compatible"

        self.api_timeout = config.getBackendConfigSection(section_name).get(
            "apiTimeout", 60
        )
        self.apiErrorWait = config.getBackendConfigSection(section_name).get(
            "apiErrorWait", "auto"
        )
        self.tokenStrategy = config.getBackendConfigSection(section_name).get(
            "tokenStrategy", "random"
        )
        self.stream = config.getBackendConfigSection(section_name).get("stream", True)

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

        if self.apiErrorWait == "auto":
            self.apiErrorWait = -1

        if self.proxyProvider:
            proxy_addr = self.proxyProvider.getProxy().addr
        else:
            proxy_addr = None

        trust_env = False  # 不使用系统代理
        self.client_list = []
        for token in self.tokenProvider.get_available_token():
            # client = AsyncOpenAI(
            #     api_key=token.token,
            #     base_url=token.domain,
            #     max_retries=0,
            #     http_client=httpx.AsyncClient(proxy=proxy_addr, trust_env=trust_env),
            # )
            client = AsyncOpenAI(
                api_key=token.token,
                base_url=token.domain,
                max_retries=0,
                http_client=DefaultAioHttpClient(
                    #proxy=proxy_addr,
                    trust_env=trust_env,
                    limits=httpx.Limits(
                        max_keepalive_connections=None, max_connections=None
                    ),
                ),
            )
            self.client_list.append((client, token))

        pass

    @staticmethod
    def _is_stop_requested(pj_config) -> bool:
        stop_event = getattr(pj_config, "stop_event", None)
        return stop_event is not None and stop_event.is_set()

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
        messages=[],
        temperature=NOT_GIVEN,
        frequency_penalty=NOT_GIVEN,
        top_p=NOT_GIVEN,
        stream=NOT_GIVEN,
        max_tokens=NOT_GIVEN,
        reasoning_effort=NOT_GIVEN,
        file_name="",
        base_try_count=0,
    ):
        api_try_count = base_try_count
        client: AsyncOpenAI
        token: COpenAIToken
        client, token = random.choices(self.client_list, k=1)[0]
        if messages == []:
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
                LOGGER.debug(f"Call {token.domain} withs token {token.maskToken()}")

                await self._wait_for_global_rpm_slot()

                # Create the API call as a task so we can cancel it if
                # the user requests a stop while the request is in-flight.
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
                    )
                )

                # Poll stop_event while waiting for the API response.
                # This ensures that a stop request is detected within 0.5s
                # even when the LLM endpoint is slow or unresponsive.
                while not api_task.done():
                    if self._is_stop_requested(self.pj_config):
                        api_task.cancel()
                        from GalTransl.Service import JobCancelledError
                        raise JobCancelledError()
                    done, _ = await asyncio.wait({api_task}, timeout=0.5)
                    if done:
                        break

                response = api_task.result()
                result = ""
                lastline = ""
                if is_stream:
                    async for chunk in response:
                        # Check stop in the middle of streaming so we don't
                        # have to wait for the entire stream to finish.
                        if self._is_stop_requested(self.pj_config):
                            from GalTransl.Service import JobCancelledError
                            raise JobCancelledError()
                        if not chunk.choices:
                            continue
                        if hasattr(chunk.choices[0].delta, "reasoning_content"):
                            lastline = lastline + (
                                chunk.choices[0].delta.reasoning_content or ""
                            )
                        if hasattr(chunk.choices[0].delta, "content"):
                            result = result + (chunk.choices[0].delta.content or "")
                            lastline = lastline + (chunk.choices[0].delta.content or "")
                        if "\n" in lastline:
                            if should_print_translation_logs(self.pj_config) and self.pj_config.active_workers == 1:
                                lastline_sp = lastline.split("\n")
                                print("\n".join(lastline_sp[:-1]))
                                lastline = lastline_sp[-1]
                else:
                    try:
                        result = response.choices[0].message.content
                    except:
                        raise ValueError(
                            "response.choices[0].message.content is None, no_candidates"
                        )
                self._record_request_health(
                    time.monotonic() - request_started,
                    is_rate_limited=False,
                )
                return result, token
            except Exception as e:
                is_rate_limited = isinstance(e, RateLimitError)
                self._record_request_health(
                    time.monotonic() - request_started,
                    is_rate_limited=is_rate_limited,
                )

                from GalTransl.Service import JobCancelledError
                if isinstance(e, JobCancelledError):
                    raise

                api_try_count += 1
                # gemini no_candidates
                if "candidates" in str(e) and api_try_count > 1:
                    return "", token
                if self.apiErrorWait >= 0:
                    sleep_time = self.apiErrorWait + random.random()
                else:
                    # https://aws.amazon.com/cn/blogs/architecture/exponential-backoff-and-jitter/
                    sleep_time = 2 ** min(api_try_count, 6)
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
                            retry_count=api_try_count,
                            model=getattr(token, "model_name", ""),
                            sleep_seconds=float(sleep_time),
                            level="warning",
                        )
                    except Exception:
                        pass

                await self._interruptible_sleep(sleep_time)

    def clean_up(self):
        pass

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
                if not any(word in tran.post_jp for word in h_words_list)
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
                await save_transCache_to_json(trans_list, cache_file_path)
                transl_step_count = 0
            if should_print_translation_logs(self.pj_config):
                LOGGER.info(
                    f"{filename}: {str(len(trans_result_list))}/{str(len_trans_list)}"
                )

        return trans_result_list

    def _set_temp_type(self, style_name: str):
        if self._current_temp_type == style_name:
            return
        self._current_temp_type = style_name
        temperature = 0.6
        frequency_penalty = NOT_GIVEN
        self.temperature = temperature
        self.frequency_penalty = frequency_penalty
