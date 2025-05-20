import json, time, asyncio, os, traceback, re
from opencc import OpenCC
from typing import Optional
from GalTransl.COpenAI import COpenAITokenPool
from GalTransl.ConfigHelper import CProxyPool
from GalTransl import LOGGER, LANG_SUPPORTED, TRANSLATOR_DEFAULT_ENGINE
from GalTransl.i18n import get_text, GT_LANG
from sys import exit, stdout
from GalTransl.ConfigHelper import (
    CProjectConfig,
)
from random import choice
from GalTransl.CSentense import CSentense, CTransList
from GalTransl.Cache import save_transCache_to_json
from GalTransl.Dictionary import CGptDict
from GalTransl.Utils import extract_code_blocks, fix_quotes2
from GalTransl.Backend.Prompts import (
    FORGAL_SYSTEM,
    FORGAL_TRANS_PROMPT_EN,
    H_WORDS_LIST,
)
from GalTransl.Backend.BaseTranslate import BaseTranslate


class ForGalTranslate(BaseTranslate):
    # init
    def __init__(
        self,
        config: CProjectConfig,
        eng_type: str,
        proxy_pool: Optional[CProxyPool],
        token_pool: COpenAITokenPool,
    ):
        super().__init__(config, eng_type, proxy_pool, token_pool)
        # enhance_jailbreak
        if val := config.getKey("gpt.enhance_jailbreak"):
            self.enhance_jailbreak = val
        else:
            self.enhance_jailbreak = False
        self.trans_prompt = FORGAL_TRANS_PROMPT_EN
        self.system_prompt = FORGAL_SYSTEM
        self.last_translation = ""
        self._set_temp_type("precise")
        self.init_chatbot(eng_type=eng_type, config=config)
        if "qwen3" in self.model_name.lower():
            self.system_prompt+="/no_think"

        pass

    async def translate(self, trans_list: CTransList, gptdict="", proofread=False):
        input_list = []
        for i, trans in enumerate(trans_list):
            speaker = trans.speaker if trans.speaker else "null"
            speaker = speaker.replace("\r\n", "").replace("\t", "").replace("\n", "")
            src_text = trans.post_jp
            src_text = src_text.replace("\r\n", "\\n").replace("\t", "\\t").replace("\n", "\\n")
            tmp_obj = f"{trans.index}\t{speaker}\t{src_text}"
            input_list.append(tmp_obj)
        input_src = "\n".join(input_list)

        prompt_req = self.trans_prompt
        prompt_req = prompt_req.replace("[Input]", input_src)
        prompt_req = prompt_req.replace("[Glossary]", gptdict)
        prompt_req = prompt_req.replace("[SourceLang]", self.source_lang)
        prompt_req = prompt_req.replace("[TargetLang]", self.target_lang)

        if self.enhance_jailbreak:
            assistant_prompt = "ID\tNAME\tDST"
        else:
            assistant_prompt = ""

        messages = []
        messages.append({"role": "system", "content": self.system_prompt})
        if self.last_translation:
            messages.append({"role": "user", "content": "(……上轮翻译请求……)"})
            messages.append({"role": "assistant", "content": self.last_translation})
        messages.append({"role": "user", "content": prompt_req})
        if self.enhance_jailbreak:
            messages.append({"role": "assistant", "content": assistant_prompt})

        while True:  # 一直循环，直到得到数据
            if self.pj_config.active_workers == 1:
                LOGGER.info(
                    f"->{'翻译输入' if not proofread else '校对输入'}：\n{gptdict}\n{input_src}\n"
                )
                LOGGER.info("->输出：")
            resp = ""
            resp = await self.ask_chatbot(
                model_name=self.model_name,
                messages=messages,
                temperature=self.temperature,
                frequency_penalty=self.frequency_penalty,
            )

            result_text = resp
            result_text = result_text.split("ID\tNAME\tDST")[-1].strip()

            i = -1
            result_trans_list = []
            result_lines = result_text.splitlines()
            error_flag = False
            error_message = ""
            for line in result_lines:
                if "```" in line:
                    continue
                if line.strip() == "":
                    continue
                if line.startswith("ID"):
                    continue

                line_sp = line.split("\t")
                if len(line_sp) != 3:
                    error_message = f"无法解析行：{line}"
                    error_flag = True
                    break

                i += 1
                # 本行输出不正常
                try:
                    line_id = int(line_sp[0])
                except:
                    error_message = f"第{line}句id无法解析"
                    error_flag = True
                    break
                if line_id != trans_list[i].index:
                    error_message = f"输出{line_id}句id未对应"
                    error_flag = True
                    break

                line_dst = line_sp[2]
                # 本行输出不应为空
                if trans_list[i].post_jp != "" and line_dst == "":
                    error_message = f"第{line_id}句空白"
                    error_flag = True
                    break

                if "Chinese" in self.target_lang:  # 统一简繁体
                    line_dst = self.opencc.convert(line_dst)

                if (
                    "”" not in trans_list[i].post_jp
                    and '"' not in trans_list[i].post_jp
                ):
                    line_dst = line_dst.replace('"', "")
                elif '"' not in trans_list[i].post_jp and '"' in line_dst:
                    line_dst = fix_quotes2(line_dst)
                elif '"' in trans_list[i].post_jp and "”" in line_dst:
                    line_dst = line_dst.replace("“", '"')
                    line_dst = line_dst.replace("”", '"')

                if not line_dst.startswith("「") and trans_list[i].post_jp.startswith(
                    "「"
                ):
                    line_dst = "「" + line_dst
                if not line_dst.endswith("」") and trans_list[i].post_jp.endswith("」"):
                    line_dst = line_dst + "」"

                if "\r\n" in trans_list[i].post_jp:
                    line_dst = line_dst.replace("\\n", "\r\n")
                if "\t" in trans_list[i].post_jp:
                    line_dst = line_dst.replace("\\t", "\t")
                if "\n" in trans_list[i].post_jp:
                    line_dst = line_dst.replace("\\n", "\n")
                if "……" in trans_list[i].post_jp and "..." in line_dst:
                    line_dst = line_dst.replace("......", "……")
                    line_dst = line_dst.replace("...", "……")

                trans_list[i].pre_zh = line_dst
                trans_list[i].post_zh = line_dst
                trans_list[i].trans_by = self.model_name
                result_trans_list.append(trans_list[i])
                if i >= len(trans_list) - 1:
                    break

            if error_flag:
                LOGGER.error(f"-> [解析错误]解析结果出错：{error_message}")
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
                # 重试中止
                if self.retry_count == 4:
                    self.reset_conversation()
                    LOGGER.error("-> [解析错误]解析反复出错，跳过本轮翻译")
                    i = 0 if i < 0 else i
                    while i < len(trans_list):
                        if not proofread:
                            trans_list[i].pre_zh = "Failed translation"
                            trans_list[i].post_zh = "Failed translation"
                            trans_list[i].problem = "Failed translation"
                            trans_list[i].trans_by = f"{self.model_name}(Failed)"
                        else:
                            trans_list[i].proofread_zh = trans_list[i].pre_zh
                            trans_list[i].post_zh = trans_list[i].pre_zh
                            trans_list[i].problem = "Failed translation"
                            trans_list[i].proofread_by = f"{self.model_name}(Failed)"
                        result_trans_list.append(trans_list[i])
                        i = i + 1
                    return i, result_trans_list
                continue

            # 翻译完成，收尾
            self._set_temp_type("precise")
            self.retry_count = 0
            self.last_translation = resp
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
        translist_hit: CTransList = [],
        translist_unhit: CTransList = [],
    ) -> CTransList:
        if len(translist_unhit) == 0:
            return []
        if self.skipH:
            translist_unhit = [
                tran
                for tran in translist_unhit
                if not any(word in tran.post_jp for word in H_WORDS_LIST)
            ]

        # 新文件重置chatbot
        if self.last_file_name != filename:
            self.reset_conversation()
            self.last_file_name = filename
            # LOGGER.info(f"-> 开始翻译文件：{filename}")
        i = 0

        if self.restore_context_mode and not proofread:
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

            dic_prompt = gpt_dic.gen_prompt(trans_list_split, "tsv") if gpt_dic else ""

            num, trans_result = await self.translate(
                trans_list_split, dic_prompt, proofread=proofread
            )

            if num > 0:
                i += num
            self.pj_config.bar(num)
            result_output = ""
            for trans in trans_result:
                result_output = result_output + repr(trans)
            LOGGER.info(result_output)
            trans_result_list += trans_result
            transl_step_count += 1
            if transl_step_count >= self.save_steps:
                save_transCache_to_json(trans_list, cache_file_path)
                transl_step_count = 0

            LOGGER.info(
                f"{filename}: {str(len(trans_result_list))}/{str(len_trans_list)}"
            )

        return trans_result_list

    def reset_conversation(self):
        self.last_translation = ""

    def _set_temp_type(self, style_name: str):
        if self._current_temp_type == style_name:
            return
        self._current_temp_type = style_name
        temperature = 0.8
        frequency_penalty = 0.5
        if style_name == "precise":
            temperature = 0.4
            frequency_penalty = 0.1
        elif style_name == "normal":
            pass
        self.temperature = temperature
        self.frequency_penalty = frequency_penalty

    def restore_context(self, translist_unhit: CTransList, num_pre_request: int):
        if translist_unhit[0].prev_tran == None:
            return
        tmp_context = []
        num_count = 0
        current_tran = translist_unhit[0].prev_tran
        while current_tran != None:
            if current_tran.pre_zh == "":
                current_tran = current_tran.prev_tran
                continue
            speaker = current_tran.speaker if current_tran.speaker else "null"
            tmp_obj = f"{current_tran.index}\t{speaker}\t{current_tran.pre_zh}"
            tmp_context.append(tmp_obj)
            num_count += 1
            if num_count >= num_pre_request:
                break
            current_tran = current_tran.prev_tran

        tmp_context.reverse()
        json_lines = "\n".join(tmp_obj)
        self.last_translation = "ID\tNAME\tDST\n" + json_lines
        LOGGER.info("-> 恢复了上下文")


if __name__ == "__main__":
    pass
