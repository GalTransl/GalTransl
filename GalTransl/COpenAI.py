"""
CloseAI related classes
"""

import asyncio
from asyncio import gather
from alive_progress import alive_bar
from time import time
from GalTransl import LOGGER, TRANSLATOR_DEFAULT_ENGINE
from GalTransl.ConfigHelper import CProjectConfig, CProxy
from typing import Optional, Tuple
from random import choice
from asyncio import Queue
from openai import OpenAI
import re
import httpx


class COpenAIToken:
    """
    OpenAI 令牌
    """

    def __init__(self, token: str, domain: str,model_name:str, isAvailable: bool) -> None:
        self.token: str = token
        self.domain: str = domain
        self.model_name:str=model_name
        self.isAvailable: bool = isAvailable
        self.avg_latency:float=0
        self.req_count:int=0

    def maskToken(self) -> str:
        """
        返回脱敏后的 sk-*******-****
        """
        if len(self.token)>10:
            return self.token[:6] + "..." + self.token[-4:]
        else:
            return self.token



class COpenAITokenPool:
    """
    OpenAI 令牌池
    """

    def __init__(self, config: CProjectConfig, eng_type: str) -> None:

        token_list: list[dict] = []
        defaultEndpoint = "https://api.openai.com"
        section_name = "OpenAI-Compatible"
        self.tokens: list[tuple[bool, COpenAIToken]] = []
        self.force_eng_name = config.getBackendConfigSection(section_name).get(
            "rewriteModelName", ""
        )
        self.stream=config.getBackendConfigSection(section_name).get(
            "stream", False
        )
        self.timeout=config.getBackendConfigSection(section_name).get(
            "apiTimeout", 60
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
                    model_name=tokenEntry["modelName"]
                else:
                    model_name=self.force_eng_name

                domain = domain[:-1] if domain.endswith("/") else domain
                base_path="/v1" if not re.search(r"/v\d+", domain) else ""
                token_list.append(COpenAIToken(token, domain+base_path, model_name, True))
                pass

        for token in token_list:
            self.tokens.append((True, token))

    async def _isTokenAvailable(
        self, token: COpenAIToken, proxy: CProxy = None
    ) -> Tuple[bool, bool, bool, COpenAIToken]:


        try:
            st = time()
            
            client = OpenAI(
                api_key=token.token,
                base_url=token.domain,
                http_client=httpx.Client(proxy=proxy.addr if proxy else None)
            )
            response = client.chat.completions.create(
                model=token.model_name,
                messages=[{"role": "user", "content": "JUST echo OK"}],
                timeout=self.timeout,
                stream=self.stream,
            )
            if self.stream==False:
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
                    pass
        except Exception as e:
            LOGGER.error(e)

            LOGGER.debug(
                "we got exception in testing OpenAI token %s", token.maskToken()
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
        max_retries: int = 3,
    ) -> Tuple[bool, COpenAIToken]:
        for retry_count in range(max_retries):
            is_available, token = await self._isTokenAvailable(token, proxy)
            if is_available:
                self.bar()
                return is_available, token
            else:
                # wait for some time before retrying, you can add some delay here
                LOGGER.warning(f"可用性检查失败，正在重试 {retry_count + 1} 次...")
                await asyncio.sleep(1)

        # If all retries fail, return the result from the last attempt
        self.bar()
        return is_available, token

    async def checkTokenAvailablity(
        self, proxy: CProxy = None, eng_type: str = ""
    ) -> None:
        """
        检测令牌有效性
        """
        fs = []
        with alive_bar(total=len(self.tokens),title="Testing Key……") as bar:
            self.bar = bar
            index=0
            for _, token in self.tokens:
                index+=1
                LOGGER.info(f"Testing key{index}---{token.maskToken()}---{token.model_name}")
                fs.append(
                    self._check_token_availability_with_retry(
                        token, proxy if proxy else None
                    )
                )
            result: list[tuple[bool, COpenAIToken]] = await gather(*fs)

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
        for id, tokenPair in enumerate(self.tokens):
            if tokenPair[1] == token:
                self.tokens.pop(id)
            pass
        pass

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
