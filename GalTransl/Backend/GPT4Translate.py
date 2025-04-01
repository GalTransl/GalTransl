import json, time, asyncio, os, traceback, re
from sys import exit,stdout
from opencc import OpenCC
from typing import Optional
from GalTransl.COpenAI import COpenAITokenPool
from GalTransl.ConfigHelper import CProxyPool
from GalTransl import LOGGER, LANG_SUPPORTED
from sys import exit
from GalTransl.ConfigHelper import (
    CProjectConfig,
)
from tqdm.contrib.logging import logging_redirect_tqdm
from tqdm.asyncio import tqdm as atqdm
from random import choice
from GalTransl.CSentense import CSentense, CTransList
from GalTransl.Cache import get_transCache_from_json_new, save_transCache_to_json
from GalTransl.Dictionary import CGptDict
from GalTransl.Utils import extract_code_blocks, fix_quotes
from GalTransl.Backend.Prompts import (
    NAME_PROMPT4,
    NAME_PROMPT4_R1,
    GPT4Turbo_SYSTEM_PROMPT,
    GPT4Turbo_TRANS_PROMPT,
    GPT4Turbo_CONF_PROMPT,
    GPT4Turbo_PROOFREAD_PROMPT,
    GPT4_CONF_PROMPT,
    H_WORDS_LIST,
    DEEPSEEK_SYSTEM_PROMPT,
    DEEPSEEK_TRANS_PROMPT,
    DEEPSEEK_PROOFREAD_PROMPT,
)
from GalTransl.Backend.BaseTranslate import BaseTranslate


class CGPT4Translate(BaseTranslate):
    # init
    def __init__(
        self,
        config: CProjectConfig,
        eng_type: str,
        proxy_pool: Optional[CProxyPool],
        token_pool: COpenAITokenPool,
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
        self.eng_type = eng_type
        self.last_file_name = ""
        self.restore_context_mode = config.getKey("gpt.restoreContextMode")
        self.retry_count = 0
        # 保存间隔
        if val := config.getKey("save_steps"):
            self.save_steps = val
        else:
            self.save_steps = 1
        # 记录确信度
        if val := config.getKey("gpt.recordConfidence"):
            self.record_confidence = val
        else:
            self.record_confidence = False
        # 语言设置
        if val := config.getKey("language"):
            sp = val.split("2")
            self.source_lang = sp[0]
            self.target_lang = sp[1]
        elif val := config.getKey("sourceLanguage"):  # 兼容旧版本配置
            self.source_lang = val
            self.target_lang = config.getKey("targetLanguage")
        else:
            self.source_lang = "ja"
            self.target_lang = "zh-cn"
        if self.source_lang not in LANG_SUPPORTED.keys():
            raise ValueError("错误的源语言代码：" + self.source_lang)
        else:
            self.source_lang = LANG_SUPPORTED[self.source_lang]
        if self.target_lang not in LANG_SUPPORTED.keys():
            raise ValueError("错误的目标语言代码：" + self.target_lang)
        else:
            self.target_lang = LANG_SUPPORTED[self.target_lang]
        # 429等待时间
        if val := config.getKey("gpt.tooManyRequestsWaitTime"):
            self.wait_time = val
        else:
            self.wait_time = 60
        # 挥霍token模式
        if val := config.getKey("gpt.fullContextMode"):
            self.full_context_mode = val
        else:
            self.full_context_mode = False
        # 跳过重试
        if val := config.getKey("skipRetry"):
            self.skipRetry = val
        else:
            self.skipRetry = False
        # 跳过h
        if val := config.getKey("skipH"):
            self.skipH = val
        else:
            self.skipH = False
        # enhance_jailbreak
        if val := config.getKey("gpt.enhance_jailbreak"):
            self.enhance_jailbreak = val
        else:
            self.enhance_jailbreak = False
        # 流式输出模式
        if val := config.getKey("gpt.streamOutputMode"):
            self.streamOutputMode = val
        else:
            self.streamOutputMode = False
        if val := config.getKey("workersPerProject"):  # 多线程关闭流式输出
            if val > 1:
                self.streamOutputMode = False

        self.tokenProvider = token_pool
        if config.getKey("internals.enableProxy") == True:
            self.proxyProvider = proxy_pool
        else:
            self.proxyProvider = None

        self._current_temp_type = ""

        self.init_chatbot(eng_type=eng_type, config=config)  # 模型选择

        self._set_temp_type("precise")

        if self.target_lang == "Simplified_Chinese":
            self.opencc = OpenCC("t2s.json")
        elif self.target_lang == "Traditional_Chinese":
            self.opencc = OpenCC("s2tw.json")

        pass

    def init_chatbot(self, eng_type, config):
        eng_name = config.getBackendConfigSection("GPT4").get("rewriteModelName", "")

        from GalTransl.Backend.revChatGPT.V3 import Chatbot as ChatbotV3

        self.token = self.tokenProvider.getToken(False, True)
        eng_name = "gpt-4-1106-preview" if eng_name == "" else eng_name

        try:
            change_prompt = CProjectConfig.getProjectConfig(config)['common']['gpt.change_prompt']
            prompt_type = CProjectConfig.getProjectConfig(config)['common']['gpt.prompt_type']
            prompt_content = CProjectConfig.getProjectConfig(config)['common']['gpt.prompt_content']

            if change_prompt and prompt_type == "AdditionalPrompt":
                system_prompt = GPT4Turbo_SYSTEM_PROMPT
                trans_prompt = GPT4Turbo_TRANS_PROMPT + "\n# Additional Requirements: " + prompt_content
                proofread_prompt = GPT4Turbo_PROOFREAD_PROMPT
                # R1需要使用专用的prompt
                if eng_type == "r1":
                    system_prompt = DEEPSEEK_SYSTEM_PROMPT
                    trans_prompt = DEEPSEEK_TRANS_PROMPT + prompt_content
                    proofread_prompt = DEEPSEEK_PROOFREAD_PROMPT

            elif change_prompt and prompt_type == "OverwritePrompt":
                system_prompt = GPT4Turbo_SYSTEM_PROMPT
                trans_prompt = prompt_content
                proofread_prompt = GPT4Turbo_PROOFREAD_PROMPT
                if eng_type == "r1":
                    system_prompt = DEEPSEEK_SYSTEM_PROMPT
                    trans_prompt = prompt_content
                    proofread_prompt = DEEPSEEK_PROOFREAD_PROMPT

            else:
                system_prompt = GPT4Turbo_SYSTEM_PROMPT
                trans_prompt = GPT4Turbo_TRANS_PROMPT
                proofread_prompt = GPT4Turbo_PROOFREAD_PROMPT
                if eng_type == "r1":
                    system_prompt = DEEPSEEK_SYSTEM_PROMPT
                    trans_prompt = DEEPSEEK_TRANS_PROMPT
                    proofread_prompt = DEEPSEEK_PROOFREAD_PROMPT
        except KeyError:
            system_prompt = GPT4Turbo_SYSTEM_PROMPT
            trans_prompt = GPT4Turbo_TRANS_PROMPT
            proofread_prompt = GPT4Turbo_PROOFREAD_PROMPT
            if eng_type == "r1":
                system_prompt = DEEPSEEK_SYSTEM_PROMPT
                trans_prompt = DEEPSEEK_TRANS_PROMPT
                proofread_prompt = DEEPSEEK_PROOFREAD_PROMPT

        base_path = "/v1" if not re.search(r"/v\d+$", self.token.domain) else ""
        self.chatbot = ChatbotV3(
            api_key=self.token.token,
            temperature=0.4,
            frequency_penalty=0.2,
            system_prompt=system_prompt,
            engine=eng_name,
            api_address=f"{self.token.domain}{base_path}/chat/completions",
            timeout=30,
            response_format="json",
        )
        self.chatbot.trans_prompt = trans_prompt
        self.chatbot.proofread_prompt = proofread_prompt
        self.chatbot.update_proxy(
            self.proxyProvider.getProxy().addr if self.proxyProvider else None
        )

    async def translate(self, trans_list: CTransList, gptdict="", proofread=False):
        input_list = []
        for i, trans in enumerate(trans_list):
            if not proofread:
                tmp_obj = {
                    "id": trans.index,
                    "name": trans.speaker,
                    "src": trans.post_jp,
                }
                if trans.speaker == "":
                    del tmp_obj["name"]
                input_list.append(tmp_obj)
            else:
                tmp_obj = {
                    "id": trans.index,
                    "name": trans.speaker,
                    "src": trans.post_jp,
                    "dst": (
                        trans.pre_zh if trans.proofread_zh == "" else trans.proofread_zh
                    ),
                }
                if trans.speaker == "":
                    del tmp_obj["name"]

                input_list.append(tmp_obj)
        # dump as jsonline
        input_json = "\n".join(
            [json.dumps(obj, ensure_ascii=False) for obj in input_list]
        )

        prompt_req = (
            self.chatbot.trans_prompt
            if not proofread
            else self.chatbot.proofread_prompt
        )
        prompt_req = prompt_req.replace("[Input]", input_json)
        prompt_req = prompt_req.replace("[Glossary]", gptdict)
        prompt_req = prompt_req.replace("[SourceLang]", self.source_lang)
        prompt_req = prompt_req.replace("[TargetLang]", self.target_lang)
        if self.record_confidence:
            prompt_req = prompt_req.replace("[ConfRecord]", GPT4_CONF_PROMPT)
        else:
            prompt_req = prompt_req.replace("[ConfRecord]", "")
        if '"name"' in input_json:
            if self.eng_type == "r1":
                prompt_req = prompt_req.replace("[NamePrompt3]", NAME_PROMPT4_R1)
            else:
                prompt_req = prompt_req.replace("[NamePrompt3]", NAME_PROMPT4)
        else:
            prompt_req = prompt_req.replace("[NamePrompt3]", "")
        if self.enhance_jailbreak:
            assistant_prompt = "```jsonline"
        else:
            assistant_prompt = ""
        while True:  # 一直循环，直到得到数据
            try:
                # change token
                if self.eng_type != "unoffapi":
                    self.token = self.tokenProvider.getToken(False, True)
                    self.chatbot.set_api_key(self.token.token)
                    base_path = "/v1" if not re.search(r"/v\d+$", self.token.domain) else ""
                    self.chatbot.set_api_addr(
                        f"{self.token.domain}{base_path}/chat/completions"
                    )
                # LOGGER.info("->输入：\n" + prompt_req + "\n")
                with logging_redirect_tqdm(loggers=[LOGGER]):
                    LOGGER.info(
                        f"->{'翻译输入' if not proofread else '校对输入'}：{gptdict}\n{input_json}\n"
                    )
                if self.streamOutputMode:
                    LOGGER.info("->输出：")
                resp, data = "", ""
                if self.eng_type != "unoffapi":
                    if not self.full_context_mode:
                        self._del_previous_message()
                    async for data in self.chatbot.ask_stream_async(
                        prompt_req, assistant_prompt=assistant_prompt
                    ):
                        if self.streamOutputMode:
                            print(data, end="", flush=True)
                        resp += data
                elif self.eng_type == "unoffapi":
                    async for data in self.chatbot.ask_async(prompt_req):
                        if self.streamOutputMode:
                            print(data["message"][len(resp) :], end="", flush=True)
                        resp = data["message"]

                if not self.streamOutputMode:
                    LOGGER.info(f"->输出：\n{resp}")
                else:
                    print("")
            except asyncio.CancelledError:
                raise
            except RuntimeError:
                raise
            except Exception as ex:
                str_ex = str(ex).lower()
                LOGGER.error(f"-> {str_ex}")
                if "quota" in str_ex:
                    self.tokenProvider.reportTokenProblem(self.token)
                    LOGGER.error(f"-> [请求错误]余额不足： {self.token.maskToken()}")
                    self.token = self.tokenProvider.getToken(False, True)
                    self.chatbot.set_api_key(self.token.token)
                    self._del_last_answer()
                    LOGGER.warning(f"-> [请求错误]切换到token {self.token.maskToken()}")
                    continue
                elif "try again later" in str_ex or "too many requests" in str_ex:
                    LOGGER.warning(
                        f"-> [请求错误]请求受限，{self.wait_time}秒后继续尝试"
                    )
                    await asyncio.sleep(self.wait_time)
                    continue
                elif "try reload" in str_ex:
                    self.reset_conversation()
                    LOGGER.error("-> [请求错误]报错重置会话")
                    continue
                else:
                    self._del_last_answer()
                    LOGGER.info("-> [请求错误]报错:%s, 2秒后重试" % ex)
                    await asyncio.sleep(2)
                    continue

            result_text = resp
            if "```json" in result_text:
                lang_list, code_list = extract_code_blocks(result_text)
                if len(lang_list) > 0 and len(code_list) > 0:
                    result_text = code_list[0]
            if '{"id' in result_text:
                result_text = result_text[result_text.find('{"id') :]

            result_text = (
                result_text.replace(", doub:", ', "doub":')
                .replace(", conf:", ', "conf":')
                .replace(", unkn:", ', "unkn":')
            )
            result_text = fix_quotes(result_text)

            i = -1
            result_trans_list = []
            key_name = "dst" if not proofread else "newdst"
            error_flag = False
            error_message = ""
            for line in result_text.split("\n"):
                try:
                    line_json = json.loads(line)  # 尝试解析json
                    i += 1
                except:
                    if i == -1:
                        LOGGER.error("-> 非json：\n" + line + "\n")
                        error_flag = True
                        break
                    else:
                        continue

                # 本行输出不正常
                if (
                    isinstance(line_json, dict) == False
                    or "id" not in line_json
                    or type(line_json["id"]) != int
                    or i > len(trans_list) - 1
                ):
                    error_message = f"{line}句不无法解析"
                    error_flag = True
                    break
                line_id = line_json["id"]
                if line_id != trans_list[i].index:
                    error_message = f"输出{line_id}句id未对应"
                    error_flag = True
                    break
                if key_name not in line_json or type(line_json[key_name]) != str:
                    error_message = f"第{trans_list[i].index}句找不到{key_name}"
                    error_flag = True
                    break
                # 本行输出不应为空
                if trans_list[i].post_jp != "" and line_json[key_name] == "":
                    error_message = f"第{line_id}句空白"
                    error_flag = True
                    break
                if "/" in line_json[key_name]:
                    if (
                        "／" not in trans_list[i].post_jp
                        and "/" not in trans_list[i].post_jp
                    ):
                        error_message = (
                            f"第{line_id}句多余 / 符号：" + line_json[key_name]
                        )
                        error_flag = True
                        break
                if self.target_lang != "English":
                    if "can't fullfill" in line_json[key_name]:
                        error_message = f"GPT4拒绝了翻译"
                        error_flag = True
                        break

                if "Chinese" in self.target_lang:  # 统一简繁体
                    line_json[key_name] = self.opencc.convert(line_json[key_name])

                if not proofread:
                    trans_list[i].pre_zh = line_json[key_name]
                    trans_list[i].post_zh = line_json[key_name]
                    trans_list[i].trans_by = self.chatbot.engine
                    if "conf" in line_json:
                        trans_list[i].trans_conf = line_json["conf"]
                    if "doub" in line_json:
                        trans_list[i].doub_content = line_json["doub"]
                    if "unkn" in line_json:
                        trans_list[i].unknown_proper_noun = line_json["unkn"]
                    result_trans_list.append(trans_list[i])
                else:
                    trans_list[i].proofread_zh = line_json[key_name]
                    trans_list[i].proofread_by = self.chatbot.engine
                    trans_list[i].post_zh = line_json[key_name]
                    result_trans_list.append(trans_list[i])

            if error_flag:
                LOGGER.error(f"-> [解析错误]解析结果出错：{error_message}")
                if self.skipRetry:
                    self.reset_conversation()
                    LOGGER.warning("-> [解析错误]解析出错但跳过本轮翻译")
                    i = 0 if i < 0 else i
                    while i < len(trans_list):
                        if not proofread:
                            trans_list[i].pre_zh = "Failed translation"
                            trans_list[i].post_zh = "Failed translation"
                            trans_list[i].trans_by = f"{self.chatbot.engine}(Failed)"
                        else:
                            trans_list[i].proofread_zh = trans_list[i].pre_zh
                            trans_list[i].post_zh = trans_list[i].pre_zh
                            trans_list[i].proofread_by = (
                                f"{self.chatbot.engine}(Failed)"
                            )
                        result_trans_list.append(trans_list[i])
                        i = i + 1
                    return i, result_trans_list

                await asyncio.sleep(1)
                self._del_last_answer()
                self.retry_count += 1
                # 切换模式
                self._set_temp_type("normal")
                # 2次重试则对半拆
                if self.retry_count == 2 and len(trans_list) > 1:
                    self.retry_count -= 1
                    LOGGER.warning("-> 仍然出错，拆分重试")
                    return await self.translate(
                        trans_list[: len(trans_list) // 2], gptdict
                    )
                # 单句重试仍错则重置会话
                if self.retry_count == 3:
                    self.reset_conversation()
                    LOGGER.warning("-> 单句仍错，重置会话")
                # 单句5次重试则中止
                if self.retry_count == 5:
                    LOGGER.error(f"-> 单句反复出错，已中止。错误为：{error_message}")
                    raise RuntimeError(f"-> 单句反复出错，最后错误为：{error_message}")
                continue

            # 翻译完成，收尾
            self._set_temp_type("precise")
            self.retry_count = 0
            break
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
    ) -> CTransList:
        _, trans_list_unhit = get_transCache_from_json_new(
            trans_list,
            cache_file_path,
            retry_failed=retry_failed,
            proofread=proofread,
            retran_key=retran_key,
        )

        if self.skipH:
            LOGGER.warning("skipH: 将跳过含有敏感词的句子")
            trans_list_unhit = [
                tran
                for tran in trans_list_unhit
                if not any(word in tran.post_jp for word in H_WORDS_LIST)
            ]

        if len(trans_list_unhit) == 0:
            return []
        # 新文件重置chatbot
        if self.last_file_name != filename:
            self.reset_conversation()
            self.last_file_name = filename
            LOGGER.info(f"-> 开始翻译文件：{filename}")
        i = 0

        if (
            self.eng_type != "unoffapi"
            and self.restore_context_mode
            and len(self.chatbot.conversation["default"]) == 1
        ):
            if not proofread:
                self.restore_context(trans_list_unhit, num_pre_request)

        trans_result_list = []
        len_trans_list = len(trans_list_unhit)
        transl_step_count = 0
        progress_bar = atqdm(
            total=len_trans_list,
            desc=f"Translating {filename}",
            unit="line",
            dynamic_ncols=True,
            leave=False,
            file=stdout,
        )
        while i < len_trans_list:
            await asyncio.sleep(1)
            trans_list_split = (
                trans_list_unhit[i : i + num_pre_request]
                if (i + num_pre_request < len_trans_list)
                else trans_list_unhit[i:]
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
                save_transCache_to_json(trans_list, cache_file_path)
                transl_step_count = 0
            progress_bar.update(num)
            LOGGER.info(
                f"{filename}: {str(len(trans_result_list))}/{str(len_trans_list)}"
            )

        progress_bar.close()
        return trans_result_list

    def reset_conversation(self):
        if self.eng_type != "unoffapi":
            self.chatbot.reset()
        elif self.eng_type == "unoffapi":
            self.chatbot.reset_chat()

    def _del_previous_message(self) -> None:
        """删除历史消息，只保留最后一次的翻译结果，节约tokens"""
        last_assistant_message = None
        last_user_message = None
        for message in self.chatbot.conversation["default"]:
            if message["role"] == "assistant":
                last_assistant_message = message
        for message in self.chatbot.conversation["default"]:
            if message["role"] == "user":
                last_user_message = message
                last_user_message["content"] = "(History Translation Request)"
        system_message = self.chatbot.conversation["default"][0]
        self.chatbot.conversation["default"] = [system_message]
        if last_user_message:
            self.chatbot.conversation["default"].append(last_user_message)
        if last_assistant_message:
            self.chatbot.conversation["default"].append(last_assistant_message)

    def _del_last_answer(self):
        if self.eng_type != "unoffapi":
            # 删除上次输出
            if self.chatbot.conversation["default"][-1]["role"] == "assistant":
                self.chatbot.conversation["default"].pop()
            elif self.chatbot.conversation["default"][-1]["role"] is None:
                self.chatbot.conversation["default"].pop()
            # 删除上次输入
            if self.chatbot.conversation["default"][-1]["role"] == "user":
                self.chatbot.conversation["default"].pop()
        elif self.eng_type == "unoffapi":
            pass

    def _set_temp_type(self, style_name: str):
        if self.eng_type == "unoffapi":
            return
        if self._current_temp_type == style_name:
            return
        self._current_temp_type = style_name
        # normal default
        temperature, top_p = 1.0, 1.0
        frequency_penalty, presence_penalty = 0.3, 0.0
        if style_name == "precise":
            temperature, top_p = 0.5, 1.0
            frequency_penalty, presence_penalty = 0.3, 0.0
        elif style_name == "normal":
            pass
        if self.eng_type != "unoffapi":
            self.chatbot.temperature = temperature
            self.chatbot.top_p = top_p
            self.chatbot.frequency_penalty = frequency_penalty
            self.chatbot.presence_penalty = presence_penalty

    def restore_context(self, trans_list_unhit: CTransList, num_pre_request: int):
        if self.eng_type != "unoffapi":
            if trans_list_unhit[0].prev_tran == None:
                return
            tmp_context = []
            num_count = 0
            current_tran = trans_list_unhit[0].prev_tran
            while current_tran != None:
                if current_tran.pre_zh == "":
                    current_tran = current_tran.prev_tran
                    continue
                tmp_obj = {
                    "id": current_tran.index,
                    "name": current_tran._speaker,
                    "dst": current_tran.pre_zh,
                }
                if current_tran._speaker == "":
                    del tmp_obj["name"]
                tmp_context.append(tmp_obj)
                num_count += 1
                if num_count >= num_pre_request:
                    break
                current_tran = current_tran.prev_tran

            tmp_context.reverse()
            json_lines = "\n".join(
                [json.dumps(obj, ensure_ascii=False) for obj in tmp_context]
            )
            self.chatbot.conversation["default"].append(
                {"role": "user", "content": "(History Translation Request)"}
            )
            self.chatbot.conversation["default"].append(
                {
                    "role": "assistant",
                    "content": f"Transl: \n```jsonline\n{json_lines}\n```",
                },
            )
            LOGGER.info("-> 恢复了上下文")

        elif self.eng_type == "unoffapi":
            pass


if __name__ == "__main__":
    pass
