import asyncio
import httpx
from opencc import OpenCC
from typing import Optional, List
from collections import deque
from threading import Lock
from GalTransl.COpenAI import COpenAITokenPool, COpenAIToken
from GalTransl.ConfigHelper import CProxyPool, build_httpx_proxy_kwargs
from GalTransl import LOGGER, LANG_SUPPORTED, TRANSLATOR_DEFAULT_ENGINE
from GalTransl.i18n import get_text, GT_LANG
from GalTransl.ConfigHelper import (
    CProjectConfig,
)
from GalTransl.CSentense import CSentense, CTransList
from GalTransl.Cache import save_transCache_to_json
from GalTransl.Dictionary import CGptDict
from GalTransl.Utils import load_guideline_file, fix_quotes2
from openai import RateLimitError, AsyncOpenAI
from openai import DefaultAioHttpClient
from openai._types import NOT_GIVEN
import random
import time
from contextlib import suppress
from GalTransl.TerminalOutput import should_print_translation_logs

try:
    from pyreqwest.compatibility.httpx import HttpxTransport
except Exception:
    HttpxTransport = None


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
            guideline_file = "Basic.md"
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
        self._shutdown_done = False

        if self.target_lang == "Simplified_Chinese":
            self.opencc = OpenCC("t2s.json")
        elif self.target_lang == "Traditional_Chinese":
            self.opencc = OpenCC("s2tw.json")

        pass

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

        self.api_timeout = config.getBackendConfigSection(section_name).get(
            "apiTimeout", 300
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

        trust_env = False  # 不使用系统代理
        proxy_kwargs = build_httpx_proxy_kwargs(proxy_addr)
        self.client_list = []
        for token in self.tokenProvider.get_available_token():
            http_client = None

            use_pyreqwest_transport = HttpxTransport is not None and not proxy_kwargs
            if use_pyreqwest_transport:
                try:
                    http_client = httpx.AsyncClient(
                        trust_env=trust_env,
                        limits=httpx.Limits(
                            max_keepalive_connections=None, max_connections=None
                        ),
                        transport=HttpxTransport(),
                    )
                except Exception as e:
                    LOGGER.warning(
                        f"初始化 pyreqwest HttpxTransport 失败，回退 DefaultAioHttpClient: {e}"
                    )

            if http_client is None:
                if HttpxTransport is not None and proxy_kwargs:
                    LOGGER.warning(
                        "检测到代理配置，当前回退到 DefaultAioHttpClient（pyreqwest transport 路径未启用代理注入）"
                    )
                http_client = DefaultAioHttpClient(
                    trust_env=trust_env,
                    limits=httpx.Limits(
                        max_keepalive_connections=None, max_connections=None
                    ),
                    **proxy_kwargs,
                )

            client = AsyncOpenAI(
                api_key=token.token,
                base_url=token.domain,
                max_retries=0,
                http_client=http_client,
            )
            self.client_list.append((client, token))

        pass

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
            trans_list_split = (
                translist_unhit[i : i + num_pre_request]
                if (i + num_pre_request < len_trans_list)
                else translist_unhit[i:]
            )

            if gpt_dic:
                if glossary_style:
                    dic_prompt = gpt_dic.gen_prompt(trans_list_split, glossary_style)
                else:
                    dic_prompt = gpt_dic.gen_prompt(trans_list_split)
            else:
                dic_prompt = ""

            num, trans_result = await self.translate(
                trans_list_split,
                dic_prompt,
                proofread=proofread,
                filename=filename,
            )

            if num <= 0 and not trans_result:
                LOGGER.warning(
                    f"[{filename}:{self._build_idx_tip(trans_list_split)}] translate returned no progress, retrying once"
                )
                self._check_stop_requested()
                await asyncio.sleep(1)
                # 重试一次
                num, trans_result = await self.translate(
                    trans_list_split,
                    dic_prompt,
                    proofread=proofread,
                    filename=filename,
                )

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
                await save_transCache_to_json(trans_result, cache_file_path)
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
    ):
        api_try_count = base_try_count
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
                if is_stream:
                    stream_abort_requested = False
                    stream_line_buffer = ""
                    stream_completed = False
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
                            if hasattr(chunk.choices[0].delta, "reasoning_content"):
                                lastline = lastline + (
                                    chunk.choices[0].delta.reasoning_content or ""
                                )
                            if hasattr(chunk.choices[0].delta, "content"):
                                content_piece = chunk.choices[0].delta.content or ""
                                result = result + content_piece
                                lastline = lastline + content_piece
                                stream_line_buffer += content_piece
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
                    try:
                        result = response.choices[0].message.content
                    except:
                        raise ValueError(
                            "response.choices[0].message.content is None, no_candidates"
                        )
                    if not isinstance(result, str) or result.strip() == "":
                        raise ValueError(
                            "response.choices[0].message.content is empty"
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
                if max_retry_count is not None and api_try_count >= max_retry_count:
                    raise RuntimeError(
                        f"ask_chatbot reached retry limit ({max_retry_count})"
                    ) from e

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

    async def shutdown(self):
        if self._shutdown_done:
            return
        self._shutdown_done = True

        for client, _ in getattr(self, "client_list", []):
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
                await save_transCache_to_json(trans_result, cache_file_path)
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
