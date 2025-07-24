import asyncio
import httpx
from opencc import OpenCC
from typing import Optional
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
from openai import RateLimitError, AsyncOpenAI
from openai._types import NOT_GIVEN
import random
import time


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

        # 流式输出模式（废弃）
        self.streamOutputMode = config.getKey("gpt.streamOutputMode", False)
        if config.getKey("workersPerProject") > 1:  # 多线程关闭流式输出
            self.streamOutputMode = False

        self.tokenProvider = token_pool

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
            client = AsyncOpenAI(
                api_key=token.token,
                base_url=token.domain,
                max_retries=0,
                http_client=httpx.AsyncClient(proxy=proxy_addr, trust_env=trust_env),
            )
            self.client_list.append((client, token))

        pass

    async def ask_chatbot(
        self,
        prompt="",
        system="",
        messages=[],
        temperature=0.6,
        frequency_penalty=NOT_GIVEN,
        top_p=NOT_GIVEN,
        stream=NOT_GIVEN,
        max_tokens=NOT_GIVEN,
        reasoning_effort=NOT_GIVEN,
        file_name="",
        base_try_count=0,
    ):
        api_try_count = base_try_count
        stream = stream if stream else self.stream
        client: AsyncOpenAI
        token: COpenAIToken
        client, token = random.choices(self.client_list, k=1)[0]
        if messages == []:
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ]

        if "qwen3" in token.model_name:
            messages[-1]["content"] = "/no_think" + messages[-1]["content"]
        if "gemini" in token.model_name:
            temperature = NOT_GIVEN

        while True:
            try:
                if self.tokenStrategy == "random":
                    if api_try_count % 2 == 0:
                        client, token = random.choices(self.client_list, k=1)[0]
                elif self.tokenStrategy == "fallback":
                    index = api_try_count % len(self.client_list)
                    client, token = self.client_list[index]
                else:
                    raise ValueError("tokenStrategy must be random or fallback")
                LOGGER.debug(f"Call {token.domain} withs token {token.maskToken()}")

                response = await client.chat.completions.create(
                    model=token.model_name,
                    messages=messages,
                    stream=stream,
                    temperature=temperature,
                    frequency_penalty=frequency_penalty,
                    max_tokens=max_tokens,
                    timeout=self.api_timeout,
                    top_p=top_p,
                    reasoning_effort=reasoning_effort,
                )
                result = ""
                lastline = ""
                if stream:
                    async for chunk in response:
                        if not chunk.choices:
                            continue
                        if hasattr(chunk.choices[0].delta,"reasoning_content"):
                            lastline = lastline + (chunk.choices[0].delta.reasoning_content or "")
                        if hasattr(chunk.choices[0].delta,"content"):
                            result = result+ (chunk.choices[0].delta.content or "")
                            lastline = lastline + (chunk.choices[0].delta.content or "")
                        if "\n" in lastline:
                            if self.pj_config.active_workers == 1:
                                lastline_sp=lastline.split("\n")
                                print("\n".join(lastline_sp[:-1]))
                                lastline = lastline_sp[-1]
                else:
                    try:
                        result = response.choices[0].message.content
                    except:
                        raise ValueError(
                            "response.choices[0].message.content is None, no_candidates"
                        )
                return result, token
            except Exception as e:
                api_try_count += 1
                # gemini no_candidates
                if "no_candidates" in str(e) and api_try_count > 1:
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

                if isinstance(e, RateLimitError):
                    self.pj_config.bar.text(
                        "-> 检测到频率限制(429 RateLimitError)，翻译仍在进行中但速度将受影响..."
                    )
                else:
                    if file_name != "" and file_name[:1] != "[":
                        file_name = f"[{file_name}]"
                    try:
                        LOGGER.error(
                            f"[API Error]{token_info}{file_name} {response.model_extra['error']} sleeping {sleep_time}s"
                        )
                    except:
                        LOGGER.error(
                            f"[API Error]{token_info}{file_name} {e} sleeping {sleep_time}s"
                        )

                await asyncio.sleep(sleep_time)

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

        if self.skipH:
            LOGGER.warning("skipH: 将跳过含有敏感词的句子")
            translist_unhit = [
                tran
                for tran in translist_unhit
                if not any(word in tran.post_jp for word in H_WORDS_LIST)
            ]

        if len(translist_unhit) == 0:
            return []
        # 新文件重置chatbot
        if self.last_file_name != filename:
            self.reset_conversation()
            self.last_file_name = filename
        i = 0

        if (
            self.eng_type != "unoffapi"
            and self.restore_context_mode
            and len(self.chatbot.conversation["default"]) == 1
        ):
            if not proofread:
                self.restore_context(translist_unhit, num_pre_request)

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
            LOGGER.info(result_output)
            trans_result_list += trans_result
            transl_step_count += 1
            if transl_step_count >= self.save_steps:
                await save_transCache_to_json(trans_list, cache_file_path)
                transl_step_count = 0
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
