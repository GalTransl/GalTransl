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
            # 可用性检测只关心"能否成功返回一个响应"，
            # 用极简 prompt + max_tokens=1 避免模型做无谓生成，大幅缩短检测耗时。
            create_kwargs = dict(
                model=token.model_name,
                messages=[{"role": "user", "content": "1+1="}],
                timeout=self.timeout,
                stream=token.stream,
                max_tokens=1,
            )
            try:
                response = client.chat.completions.create(**create_kwargs)
            except TypeError:
                # 少数兼容实现不接受 max_tokens 参数，回退一次
                create_kwargs.pop("max_tokens", None)
                response = client.chat.completions.create(**create_kwargs)
            if token.stream == False:
                if len(response.choices) > 0:
                    return True, token
                else:
                    return False, token
            else:
                for chunk in response:
                    if len(chunk.choices) > 0:
                        return True, token
                    else:
                        return False, token
                # 如果流响应为空，返回False
                return False, token
        except Exception as e:
            LOGGER.error(e)


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
        with terminal_progress(
            should_print_translation_logs(self.pj_config),
            total=len(self.tokens),
            title="Testing Key……",
        ) as bar:
            self.bar = bar
            index = 0
            for _, token in self.tokens:
                self._raise_if_stop_requested()
                index += 1
                LOGGER.info(
                    f"Testing key{index}---{token.maskToken()}---{token.model_name}"
                )
                tasks.append(asyncio.create_task(check_one_token(token)))
            result: list[tuple[bool, COpenAIToken]] = []
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
                        result.append(await done_task)
            except BaseException:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

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
    if "endpoints" in projectConfig.getBackendConfigSection(section_name):
        endpoints = projectConfig.getBackendConfigSection(section_name)["endpoints"]
    else:
        endpoints = [projectConfig.getBackendConfigSection(section_name)["endpoint"]]
    repeated = (workersPerProject + len(endpoints) - 1) // len(endpoints)
    for _ in range(repeated):
        for endpoint in endpoints:
            await sakura_endpoint_queue.put(endpoint)
    LOGGER.info(f"当前使用 {workersPerProject} 个Sakura worker引擎")
    return sakura_endpoint_queue
