"""
CloseAI related classes
"""

import asyncio
from time import time
from GalTransl import LOGGER, TRANSLATOR_DEFAULT_ENGINE
from GalTransl.ConfigHelper import CProjectConfig, CProxy, build_httpx_sync_proxy_kwargs
from typing import Optional, Tuple
from random import choice
from asyncio import Queue
from openai import OpenAI
import re
import httpx
from GalTransl.TerminalOutput import should_print_translation_logs, terminal_progress

# 可用性检测的输出上限：小到几乎不生成，但不能取 1——有的家对 max_tokens 有下限
# （「max_tokens must be greater than 2」），取 1 会把一个本来能用的后端判成不可用。
AVAILABILITY_CHECK_MAX_TOKENS = 16


def _rejects_max_tokens(exc: BaseException) -> bool:
    """provider 是否在抱怨 max_tokens 这个参数（下限/上限/不认识）。"""
    text = str(exc).lower()
    return "max_tokens" in text or "max tokens" in text


def normalize_sakura_endpoints(section: dict, fallback_endpoint: str = "") -> list[str]:
    raw_endpoints = section.get("endpoints", section.get("endpoint", []))
    if isinstance(raw_endpoints, str):
        raw_endpoints = [raw_endpoints]
    elif not isinstance(raw_endpoints, list):
        raw_endpoints = []

    endpoints = []
    for endpoint in raw_endpoints:
        if not isinstance(endpoint, str):
            continue
        endpoint = endpoint.strip()
        if endpoint and endpoint not in endpoints:
            endpoints.append(endpoint)

    fallback_endpoint = fallback_endpoint.strip() if isinstance(fallback_endpoint, str) else ""
    if not endpoints and fallback_endpoint:
        endpoints.append(fallback_endpoint)

    if not endpoints:
        endpoints.append("http://127.0.0.1:8501")

    return endpoints


class COpenAIToken:
    """
    OpenAI 令牌
    """

    def __init__(
        self,
        token: str,
        domain: str,
        model_name: str,
        stream: bool = True,
        isAvailable: bool = True,
    ) -> None:
        self.token: str = token
        self.domain: str = domain
        self.model_name: str = model_name
        self.stream: bool = stream
        self.isAvailable: bool = isAvailable
        self.avg_latency: float = 0
        self.req_count: int = 0

    def maskToken(self) -> str:
        """
        返回脱敏后的 sk-*******-****
        """
        if len(self.token) > 10:
            return self.token[:6] + "..." + self.token[-4:]
        else:
            return self.token


class COpenAITokenPool:
    """
    OpenAI 令牌池
    """

    def __init__(self, config: CProjectConfig, eng_type: str) -> None:

        token_list: list[COpenAIToken] = []
        self.pj_config = config
        defaultEndpoint = "https://api.openai.com"
        section_name = "OpenAI-Compatible"
        self.tokens: list[tuple[bool, COpenAIToken]] = []
        self.force_eng_name = config.getBackendConfigSection(section_name).get(
            "rewriteModelName", ""
        )
        self.stream = config.getBackendConfigSection(section_name).get("stream", False)
        self.timeout = config.getBackendConfigSection(section_name).get(
            "apiTimeout", 300
        )

        if all_tokens := config.getBackendConfigSection(section_name).get("tokens"):
            for tokenEntry in all_tokens:
                token = tokenEntry["token"]
                if "-example-" in token:
                    continue
                domain = (
                    tokenEntry["endpoint"]
                    if tokenEntry.get("endpoint")
                    else defaultEndpoint
                )
                if "modelName" in tokenEntry:
                    model_name = tokenEntry["modelName"]
                else:
                    model_name = self.force_eng_name

                if "stream" in tokenEntry:
                    is_stream = tokenEntry["stream"]
                else:
                    is_stream = self.stream

                if domain.endswith("/chat/completions"):
                    base_path=""
                    domain=domain.replace("/chat/completions", "")
                else:
                    base_path = "/v1" if not re.search(r"/v\d+", domain) else ""
                domain=domain.strip("/") + base_path
                token_list.append(
                    COpenAIToken(
                        token,
                        domain=domain,
                        model_name=model_name,
                        stream=is_stream,
                        isAvailable=True,
                    )
                )
                pass

        for token in token_list:
            self.tokens.append((True, token))

    def _raise_if_stop_requested(self) -> None:
        stop_event = getattr(self.pj_config, "stop_event", None)
        if stop_event is not None and stop_event.is_set():
            from GalTransl.Service import JobCancelledError

            raise JobCancelledError()

    def _record_runtime_error(
        self,
        *,
        kind: str,
        message: str,
        model: str = "",
        level: str = "warning",
    ) -> None:
        try:
            from GalTransl.server import record_runtime_error

            record_runtime_error(
                getattr(
                    self.pj_config,
                    "runtime_project_dir",
                    self.pj_config.getProjectDir(),
                ),
                kind=kind,
                message=message,
                model=model,
                level=level,
            )
        except Exception:
            return

    async def _interruptible_sleep(self, seconds: float) -> None:
        remaining = float(seconds)
        while remaining > 0:
            self._raise_if_stop_requested()
            step = min(remaining, 0.5)
            await asyncio.sleep(step)
            remaining -= step

    async def _isTokenAvailable(
        self, token: COpenAIToken, proxy: CProxy = None
    ) -> Tuple[bool, COpenAIToken]:
        return await asyncio.to_thread(self._isTokenAvailable_sync, token, proxy)

    def _isTokenAvailable_sync(
        self, token: COpenAIToken, proxy: CProxy = None
    ) -> Tuple[bool, COpenAIToken]:
        st = time()

        try:
            LOGGER.info(f"API URL: {token.domain}/chat/completions")
            proxy_kwargs = build_httpx_sync_proxy_kwargs(proxy.addr if proxy else None)
            client = OpenAI(
                api_key=token.token,
                base_url=token.domain,
                http_client=httpx.Client(**proxy_kwargs) if proxy_kwargs else None,
            )
            # 可用性检测只关心"能否成功返回一个响应"：给个很小的输出上限避免模型做无谓生成。
            # 但不能取 1——有的家对 max_tokens 有下限（「max_tokens must be greater than 2」），
            # 那样会把一个本来能用的后端整条判死；下面还有一道"摘掉该参数"的兜底。
            create_kwargs = dict(
                model=token.model_name,
                messages=[{"role": "user", "content": "1+1="}],
                timeout=self.timeout,
                stream=token.stream,
                max_tokens=AVAILABILITY_CHECK_MAX_TOKENS,
            )

            def _attempt(**kwargs) -> Tuple[bool, COpenAIToken]:
                """发一次请求并判断"有没有拿到响应"（流式看首个 chunk 有没有 choices）。"""
                response = client.chat.completions.create(**kwargs)
                if token.stream == False:
                    return len(response.choices) > 0, token
                for chunk in response:
                    return len(chunk.choices) > 0, token
                # 如果流响应为空，返回False
                return False, token

            try:
                return _attempt(**create_kwargs)
            except TypeError:
                # 少数兼容实现不接受 max_tokens 参数，回退一次
                create_kwargs.pop("max_tokens", None)
                return _attempt(**create_kwargs)
            except Exception as exc:
                # 有的家嫌 max_tokens 不合规（下限/上限）——检测本身不需要这个可选参数，
                # 摘掉再来一次，别让一个能用的后端因为一个参数被判不可用。
                if "max_tokens" not in create_kwargs or not _rejects_max_tokens(exc):
                    raise
                LOGGER.warning(
                    "可用性检测：provider 不接受 max_tokens=%s（%s），改为不带该参数重试",
                    create_kwargs["max_tokens"],
                    str(exc).strip()[:120],
                )
                create_kwargs.pop("max_tokens", None)
                return _attempt(**create_kwargs)
        except Exception as e:
            exception_text = str(e).strip()
            if exception_text:
                message = f"{type(e).__name__}: {exception_text}"
            else:
                message = type(e).__name__
            runtime_message = (
                f"检查模型可用性请求失败 [{token.maskToken()}]: {message}"
            )
            LOGGER.error(runtime_message)
            self._record_runtime_error(
                kind="api",
                message=runtime_message,
                model=getattr(token, "model_name", ""),
                level="warning",
            )


            LOGGER.debug(
                "we got exception in testing OpenAI token %s", token.maskToken(), exc_info=True
            )
            return False, token
        finally:
            et = time()
            LOGGER.debug("tested OpenAI token %s in %s", token.maskToken(), et - st)
            pass

    async def _check_token_availability_with_retry(
        self,
        token: COpenAIToken,
        proxy: CProxy = None,
        max_retries: int = 2,
    ) -> Tuple[bool, COpenAIToken]:
        is_available = False
        for retry_count in range(max_retries):
            self._raise_if_stop_requested()
            is_available, token = await self._isTokenAvailable(token, proxy)
            if is_available:
                self.bar()
                return is_available, token
            else:
                # wait for some time before retrying, you can add some delay here
                LOGGER.warning(f"可用性检查失败，正在重试 {retry_count + 1} 次...")
                await self._interruptible_sleep(0.3)

        # If all retries fail, return the result from the last attempt
        self.bar()
        return is_available, token

    async def checkTokenAvailablity(
        self, proxy: CProxy = None, eng_type: str = ""
    ) -> None:
        """
        检测令牌有效性
        """
        section_name = "OpenAI-Compatible"
        raw_concurrency = self.pj_config.getBackendConfigSection(section_name).get(
            "checkAvailableConcurrency", 4
        )
        try:
            check_concurrency = max(1, min(16, int(raw_concurrency)))
        except (TypeError, ValueError):
            check_concurrency = 4
        check_semaphore = asyncio.Semaphore(check_concurrency)

        async def check_one_token(token: COpenAIToken) -> Tuple[bool, COpenAIToken]:
            async with check_semaphore:
                self._raise_if_stop_requested()
                return await self._check_token_availability_with_retry(
                    token, proxy if proxy else None
                )

        tasks = []
        task_indices: dict[asyncio.Task, int] = {}
        with terminal_progress(
            should_print_translation_logs(self.pj_config),
            total=len(self.tokens),
            title="Testing Key……",
        ) as bar:
            self.bar = bar
            for token_index, (_, token) in enumerate(self.tokens):
                self._raise_if_stop_requested()
                LOGGER.info(
                    f"Testing key{token_index + 1}---{token.maskToken()}---{token.model_name}"
                )
                task = asyncio.create_task(check_one_token(token))
                tasks.append(task)
                task_indices[task] = token_index
            result_by_index: dict[int, tuple[bool, COpenAIToken]] = {}
            pending = set(tasks)
            try:
                while pending:
                    self._raise_if_stop_requested()
                    done, pending = await asyncio.wait(
                        pending,
                        timeout=0.5,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for done_task in done:
                        # Availability checks complete out of order, but the
                        # fallback policy must keep the configured order.
                        result_by_index[task_indices[done_task]] = await done_task
            except BaseException:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

        result = [result_by_index[index] for index in range(len(tasks))]

        # replace list with new one
        newList: list[tuple[bool, COpenAIToken]] = []
        for isAvailable, token in result:
            if isAvailable != True:
                LOGGER.warning(
                    "%s is not available for %s, will be removed",
                    token.maskToken(),
                    token.model_name,
                )
            else:
                newList.append((True, token))

        self.tokens = newList

    def reportTokenProblem(self, token: COpenAIToken) -> None:
        """
        报告令牌无效
        """
        # 用过滤替代迭代中 pop，避免并发修改列表
        self.tokens = [pair for pair in self.tokens if pair[1] != token]

    def getToken(self) -> COpenAIToken:
        """
        获取一个有效的 token
        """
        rounds: int = 0
        while True:
            if rounds > 20:
                raise RuntimeError("COpenAITokenPool::getToken: 可用的API key耗尽！")
            try:
                available, token = choice(self.tokens)
                if not available:
                    continue
                if token.isAvailable:
                    return token
                rounds += 1
            except IndexError:
                raise RuntimeError("没有可用的 API key！")

    def get_available_token(self) -> list[COpenAIToken]:
        """
        获取所有可用的token
        """
        return [token for available, token in self.tokens if available]


async def init_sakura_endpoint_queue(projectConfig: CProjectConfig) -> Optional[Queue]:
    """
    初始化端点队列，用于Sakura或GalTransl引擎。

    参数:
    projectConfig: 项目配置对象
    workersPerProject: 每个项目的工作线程数
    eng_type: 引擎类型

    返回:
    初始化的端点队列，如果不需要则返回None
    """

    workersPerProject = projectConfig.getKey("workersPerProject") or 1
    sakura_endpoint_queue = asyncio.Queue()
    section_name = "SakuraLLM"
    endpoints = normalize_sakura_endpoints(projectConfig.getBackendConfigSection(section_name))
    repeated = (workersPerProject + len(endpoints) - 1) // len(endpoints)
    for _ in range(repeated):
        for endpoint in endpoints:
            await sakura_endpoint_queue.put(endpoint)
    LOGGER.info(f"当前使用 {workersPerProject} 个Sakura worker引擎")
    return sakura_endpoint_queue
