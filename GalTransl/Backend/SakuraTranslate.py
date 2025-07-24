import sys, asyncio, traceback
from opencc import OpenCC
from typing import Optional
from random import choice
from GalTransl import LOGGER, LANG_SUPPORTED
from GalTransl.ConfigHelper import CProjectConfig, CProxyPool
from GalTransl.CSentense import CSentense, CTransList
from GalTransl.Cache import save_transCache_to_json
from GalTransl.Dictionary import CGptDict
from GalTransl.Utils import find_most_repeated_substring
from GalTransl.Backend.BaseTranslate import BaseTranslate
from GalTransl.COpenAI import COpenAIToken
from GalTransl.Backend.Prompts import (
    Sakura_TRANS_PROMPT,
    Sakura_SYSTEM_PROMPT,
    Sakura_TRANS_PROMPT010,
    Sakura_SYSTEM_PROMPT010,
    GalTransl_SYSTEM_PROMPT,
    GalTransl_TRANS_PROMPT,
    GalTransl_TRANS_PROMPT_V3,
)
from GalTransl import transl_counter


class CSakuraTranslate(BaseTranslate):
    # init
    def __init__(
        self,
        config: CProjectConfig,
        eng_type: str,
        endpoint: str,
        proxy_pool: Optional[CProxyPool],
    ):

        super().__init__(
            config=config,
            eng_type=eng_type,
            proxy_pool=proxy_pool,
        )
        # transl_dropout
        if val := config.getKey("gpt.transl_dropout"):
            self.transl_dropout = val
        else:
            self.transl_dropout = 0
        # token_limit
        if val := config.getKey("gpt.token_limit"):
            self.token_limit = val
            import tiktoken

            self.tokenizer = tiktoken.get_encoding("o200k_base")
        else:
            self.token_limit = 0
            self.tokenizer = None

        if self.target_lang == "Simplified_Chinese":
            self.opencc = OpenCC("t2s.json")
        elif self.target_lang == "Traditional_Chinese":
            self.opencc = OpenCC("s2tw.json")

        self.last_translations = {}
        self.endpoint = endpoint
        self.api_timeout = 30
        self.rateLimitWait = 1
        if eng_type == "sakura-v1.0":
            self.system_prompt = Sakura_SYSTEM_PROMPT010
            self.trans_prompt = Sakura_TRANS_PROMPT010
        if "galtransl" in eng_type:
            self.system_prompt = GalTransl_SYSTEM_PROMPT
            self.trans_prompt = GalTransl_TRANS_PROMPT_V3
        self.init_chatbot(eng_type=eng_type, config=config)  # 模型初始化
        self._set_temp_type("precise")

        pass

    def init_chatbot(self, eng_type, config: CProjectConfig):
        from openai import RateLimitError, AsyncOpenAI
        import httpx
        import re

        self.tokenStrategy =  "random"
        backendSpecific = config.projectConfig["backendSpecific"]
        section_name = "SakuraLLM" if "SakuraLLM" in backendSpecific else "Sakura"
        model_name = config.getBackendConfigSection(section_name).get(
            "rewriteModelName","sakura"
        )
        self.apiErrorWait = 0
        self.model_name = model_name if model_name else "sakura"

        endpoint = self.endpoint
        endpoint = endpoint[:-1] if endpoint.endswith("/") else endpoint
        base_path = "/v1" if not re.search(r"/v\d+$", endpoint) else ""
        self.stream = True
        if "sakura-share" in endpoint:
            self.stream = False

        if self.proxyProvider:
            self.proxy = self.proxyProvider.getProxy()
            client = httpx.AsyncClient(proxy=self.proxy.addr if self.proxy else None,trust_env=False)
        else:
            client = httpx.AsyncClient(trust_env=False)

        chatbot = AsyncOpenAI(
            api_key="sk-sakura",
            base_url=f"{endpoint}{base_path}",
            max_retries=0,
            http_client=client,
        )
        token=COpenAIToken("sk-sakura",f"{endpoint}{base_path}",model_name,True)
        self.client_list=[]
        self.client_list.append((chatbot,token))

    def clean_up(self):
        self.pj_config.endpointQueue.put_nowait(self.endpoint)

    async def translate(self, trans_list: CTransList, gptdict="",filename=""):
        input_list = []
        max_repeat = 0
        retry_count = 0
        line_lens = []
        start_idx=trans_list[0].index
        end_idx=trans_list[-1].index
        idx_tip=""
        if start_idx!=end_idx:
            idx_tip=f"{start_idx}~{end_idx}"
        for i, trans in enumerate(trans_list):
            tmp_text = trans.post_jp.replace("\r\n", "\\n").replace("\n", "\\n")
            speaker_name=trans.get_speaker_name()

            if speaker_name != "":
                tmp_text = f"{speaker_name}「{tmp_text}」"
            input_list.append(tmp_text)
            _, count = find_most_repeated_substring(tmp_text)
            max_repeat = max(max_repeat, count)
            line_lens.append(len(tmp_text))
        input_str = "\n".join(input_list).strip("\n")

        prompt_req = self.trans_prompt
        prompt_req = prompt_req.replace("[Input]", input_str)
        prompt_req = prompt_req.replace("[Glossary]", gptdict)

        last_translation=""
        if filename in self.last_translations:
            last_translation = self.last_translations[filename]

        if self.eng_type in ["galtransl-v3"]:  # v3不使用多轮对话做上下文
            history = ""
            if last_translation:
                history = f"历史翻译：{last_translation}\n"
            prompt_req = prompt_req.replace("[History]", history)

        messages = []
        messages.append({"role": "system", "content": self.system_prompt})
        if "sakura" in self.eng_type and last_translation:
            messages.append({"role": "user", "content": "(上轮翻译请求)"})
            messages.append({"role": "assistant", "content": last_translation})
        messages.append({"role": "user", "content": prompt_req})

        while True:  # 一直循环，直到得到数据
            if self.pj_config.active_workers == 1:
                print(f"-> 字典输入: \n{gptdict}")
                print(f"-> 翻译输入: \n{input_str}")
                print("-> 输出: ")

            resp = ""
            resp,token = await self.ask_chatbot(
                messages=messages,
                temperature=self.temperature,
                frequency_penalty=self.frequency_penalty,
                top_p=self.top_p,
                max_tokens=len(input_str) * 2,
                stream=self.stream,
            )

            result_list = resp.strip("\n").split("\n")

            i = -1
            result_trans_list = []
            error_flag = False
            error_message = ""

            if len(result_list) != len(trans_list):
                error_message = f"翻译结果行数 与 原文行数 不一致"
                error_flag = True

            for line in result_list:
                if error_flag:
                    break
                i += 1
                # 本行输出不应为空
                if trans_list[i].post_jp != "" and line == "":
                    error_message = f"第{i+1}句空白"
                    error_flag = True
                    break

                # 提取对话内容
                if trans_list[i].get_speaker_name() != "":
                    if "「" in line:
                        line = line[line.find("「") + 1 :]
                    if line.endswith("」"):
                        line = line[:-1]
                    if line.endswith("」。") or line.endswith("」."):
                        line = line[:-2]
                # 统一简繁体
                line = self.opencc.convert(line)
                # 还原换行
                if "\r\n" in trans_list[i].post_jp:
                    line = line.replace("\\n", "\r\n")
                elif "\n" in trans_list[i].post_jp:
                    line = line.replace("\\n", "\n")

                # fix trick
                if line.startswith("："):
                    line = line[1:]

                trans_list[i].pre_zh = line
                trans_list[i].post_zh = line
                trans_list[i].trans_by = self.eng_type
                result_trans_list.append(trans_list[i])

            if error_flag:
                transl_counter["error_count"] += 1
                LOGGER.debug(f"错误计数：{transl_counter['error_count']}")
                LOGGER.debug(f"翻译句数：{transl_counter['tran_count']}")

                if self.skipRetry:
                    self.reset_conversation()
                    LOGGER.warning(f"[{filename}:{idx_tip}]解析出错但跳过本轮翻译")
                    i = 0 if i < 0 else i
                    while i < len(trans_list):
                        trans_list[i].pre_zh = "Failed translation"
                        trans_list[i].post_zh = "Failed translation"
                        trans_list[i].trans_by = f"{self.eng_type}(Failed)"
                        result_trans_list.append(trans_list[i])
                        i = i + 1
                else:
                    LOGGER.error(f"[{filename}:{idx_tip}]错误的输出：{error_message}")

                    # 可拆分先对半拆
                    if len(trans_list) > 1:
                        LOGGER.warning(f"[{filename}:{idx_tip}]对半拆分重试")
                        half_len = len(trans_list) // 3
                        half_len = 1 if half_len < 1 else half_len
                        return await self.translate(trans_list[:half_len], gptdict,filename)

                    # 拆成单句后，才开始计算重试次数
                    retry_count += 1
                    # 5次重试则填充原文
                    if retry_count >= 5:
                        LOGGER.error(
                            f"[{filename}:{idx_tip}]单句循环重试{retry_count}次出错，填充原文"
                        )
                        i = 0 if i < 0 else i
                        while i < len(trans_list):
                            trans_list[i].pre_zh = trans_list[i].post_jp
                            trans_list[i].post_zh = trans_list[i].post_jp
                            trans_list[i].trans_by = f"{self.eng_type}(Failed)"
                            result_trans_list.append(trans_list[i])
                            i = i + 1
                        return i, result_trans_list
                    # 2次重试则重置会话
                    elif retry_count % 2 == 0:
                        self.last_translations[filename]=""
                        LOGGER.warning(
                            f"[{filename}:{idx_tip}]单句循环重试{retry_count}次出错，重置会话"
                        )
                        continue
                    continue
            else:
                retry_count = 0

            self._set_temp_type("precise")
            self.last_translations[filename]=resp
            return i + 1, result_trans_list

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
        translist_hit: CTransList = [],
        translist_unhit: CTransList = [],
    ) -> CTransList:

        if len(translist_unhit) == 0:
            return []

        if filename not in self.last_translations:
            self.last_translations[filename]=""

        i = 0

        self.restore_context(translist_unhit, num_pre_request, filename)

        trans_result_list = []
        len_trans_list = len(translist_unhit)
        transl_step_count = 0

        while i < len_trans_list:
            # await asyncio.sleep(1)

            trans_list_split = translist_unhit[i : i + num_pre_request]
            dic_prompt = (
                gpt_dic.gen_prompt(trans_list_split, type="sakura")
                if gpt_dic != None
                else ""
            )

            num, trans_result = await self.translate(trans_list_split, dic_prompt, filename)

            if self.transl_dropout > 0 and num == num_pre_request:
                if self.transl_dropout < num:
                    num -= self.transl_dropout
                    trans_result = trans_result[:num]

            i += num if num > 0 else 0
            transl_counter["tran_count"] += num
            self.pj_config.bar(num)
            transl_step_count += 1
            if transl_step_count >= self.save_steps:
                await save_transCache_to_json(trans_list, cache_file_path)
                transl_step_count = 0

            trans_result_list += trans_result

            LOGGER.info("".join([repr(tran) for tran in trans_result]))
            LOGGER.info(
                f"{filename}: {str(len(trans_result_list))}/{str(len_trans_list)}"
            )

        return trans_result_list

    def _set_temp_type(self, style_name: str):
        if self._current_temp_type == style_name:
            return
        self._current_temp_type = style_name

        if style_name == "precise":
            temperature, top_p = 0.1, 0.8
            frequency_penalty = 0.1
        elif style_name == "normal":
            temperature, top_p = 0.4, 0.95
            frequency_penalty = 0.3

        if "galtransl" in self.eng_type:
            if style_name == "precise":
                temperature, top_p = 0.3, 0.8
                frequency_penalty = 0.1
            elif style_name == "normal":
                temperature, top_p = 0.6, 0.95
                frequency_penalty = 0.5

        self.temperature = temperature
        self.frequency_penalty = frequency_penalty
        self.top_p = top_p

    def restore_context(self, translist_unhit: CTransList, num_pre_request: int,filename=""):
        if translist_unhit[0].prev_tran == None:
            return
        tmp_context = []
        num_count = 0
        current_tran = translist_unhit[0].prev_tran
        while current_tran != None:
            if current_tran.pre_zh == "":
                current_tran = current_tran.prev_tran
                continue
            speaker_name=current_tran.get_speaker_name()
            if speaker_name != "":
                tmp_text = f"{speaker_name}「{current_tran.pre_zh}」"
            else:
                tmp_text = f"{current_tran.pre_zh}"
            tmp_context.append(tmp_text)
            num_count += 1
            if num_count >= num_pre_request:
                break
            current_tran = current_tran.prev_tran

        tmp_context.reverse()
        json_lines = "\n".join(tmp_context)
        self.last_translations[filename] = json_lines
        if json_lines:
            LOGGER.info(f"{filename} 恢复了上下文")

    def check_degen_in_process(self, cn: str = ""):
        line_count = cn.count("\n") + 1
        if line_count < len(self.JP_LINE_LENS):  # 长度不超当前行直接放行
            if len(cn.split("\n")[-1]) < self.JP_LINE_LENS[line_count - 1]:
                return False
        else:  # 行数超过当前行，某行反复输出的情况
            repeated_str, repeated_count = find_most_repeated_substring(cn)
            if repeated_count > max(self.JP_REPETITION_THRESHOLD_ALL * 2, 12):
                return True

        # 行内反复输出的情况
        last_line = cn.split("\n")[-1]
        repeated_str, repeated_count = find_most_repeated_substring(last_line)
        if repeated_count > max(self.JP_REPETITION_THRESHOLD_LINE * 2, 12):
            return True

        return False


if __name__ == "__main__":
    pass
