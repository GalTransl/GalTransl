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
from openai._types import NOT_GIVEN


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
        self.last_translations = {}
        self.init_chatbot(eng_type=eng_type, config=config)
        self._set_temp_type("precise")

        pass

    async def translate(
        self, trans_list: CTransList, gptdict="", proofread=False, filename=""
    ):
        input_list = []
        tmp_enhance_jailbreak = False
        n_symbol = ""
        start_idx = trans_list[0].index
        end_idx = trans_list[-1].index
        idx_tip = ""
        if start_idx != end_idx:
            idx_tip = f"{start_idx}~{end_idx}"
        else:
            idx_tip = start_idx

        for i, trans in enumerate(trans_list):
            speaker_name = trans.get_speaker_name()
            speaker = speaker_name if speaker_name else "null"
            speaker = speaker.replace("\r\n", "").replace("\t", "").replace("\n", "")

            src_text = trans.post_jp
            src_text = src_text.replace("\t", "[t]")

            tmp_obj = f"{speaker}\t{src_text}\t{trans.index}"
            input_list.append(tmp_obj)

        input_src = "\n".join(input_list)

        self.restore_context(trans_list, 8, filename)

        prompt_req = self.trans_prompt
        prompt_req = prompt_req.replace("[Input]", input_src)
        prompt_req = prompt_req.replace("[Glossary]", gptdict)
        prompt_req = prompt_req.replace("[SourceLang]", self.source_lang)
        prompt_req = prompt_req.replace("[TargetLang]", self.target_lang)

        retry_count = 0
        while True:  # 一直循环，直到得到数据
            if self.enhance_jailbreak or tmp_enhance_jailbreak:
                assistant_prompt = "```NAME\tDST\tID\n"
            else:
                assistant_prompt = ""

            messages = []
            messages.append({"role": "system", "content": self.system_prompt})
            if (
                filename in self.last_translations
                and self.last_translations[filename] != ""
            ):
                self.last_translations[filename] = self.last_translations[
                    filename
                ].replace("<br>", "")
                messages.append(
                    {"role": "user", "content": "<input>\n(...truncated history source texts...)\n</input>\n<output>\n"}
                )
                messages.append(
                    {"role": "assistant", "content": self.last_translations[filename]}
                )
            messages.append({"role": "user", "content": prompt_req})
            if assistant_prompt:
                messages.append({"role": "assistant", "content": assistant_prompt})

            if self.pj_config.active_workers == 1:
                LOGGER.info(
                    f"->{'翻译输入' if not proofread else '校对输入'}：\n{gptdict}\n{input_src}\n"
                )
                LOGGER.info("->输出：")
            resp = None
            resp, token = await self.ask_chatbot(
                messages=messages,
                temperature=self.temperature,
                file_name=f"{filename}:{idx_tip}",
                base_try_count=retry_count
            )

            result_text = resp or ""
            result_text = result_text.split("NAME\tDST\tID")[-1].strip()

            i = -1
            success_count = 0
            result_trans_list = []
            result_lines = result_text.splitlines()
            error_flag = False
            error_message = ""

            if result_text == "":
                error_message = "输出为空/被拦截"
                error_flag = True

            for line in result_lines:
                if "```" in line:
                    continue
                if line.strip() == "":
                    continue
                if line.startswith("NAME"):
                    continue

                line_sp = line.split("\t")
                if len(line_sp) != 3:
                    error_message = f"无法解析行：{line}"
                    error_flag = True
                    break

                i += 1
                # 本行输出不正常
                try:
                    line_id = line_sp[2]
                except:
                    error_message = f"第{line}句id无法解析"
                    error_flag = True
                    break
                if str(trans_list[i].index) not in line_id:
                    error_message = f"{line_id}句id未对应{trans_list[i].index}"
                    error_flag = True
                    break

                line_dst = line_sp[1]
                # 本行输出不应为空
                if trans_list[i].post_jp != "" and line_dst == "":
                    error_message = f"第{line_id}句空白"
                    error_flag = True
                    break
                if "�" in line_dst:
                    error_message = f"第{line_id}句包含乱码：" + line_dst
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

                if "「" not in line_dst and trans_list[i].post_jp.startswith("「"):
                    line_dst = "「" + line_dst
                if "」" not in line_dst and trans_list[i].post_jp.endswith("」"):
                    line_dst = line_dst + "」"

                line_dst = line_dst.replace("[t]", "\t")
                if n_symbol:
                    line_dst = line_dst.replace("<br>", n_symbol)
                    line_dst = line_dst.replace("<BR>", n_symbol)

                if "……" in trans_list[i].post_jp and "..." in line_dst:
                    line_dst = line_dst.replace("......", "……")
                    line_dst = line_dst.replace("...", "……")

                trans_list[i].pre_zh = line_dst
                trans_list[i].post_zh = line_dst
                trans_list[i].trans_by = token.model_name
                result_trans_list.append(trans_list[i])
                success_count += 1

                if i >= len(trans_list) - 1:
                    break

            if success_count > 0:
                error_flag = False  # 部分解析

            if error_flag:
                LOGGER.error(
                    f"[解析错误][{filename}:{idx_tip}]解析结果出错：{error_message}"
                )
                retry_count += 1
                await asyncio.sleep(1)

                tmp_enhance_jailbreak = not tmp_enhance_jailbreak

                # 2次重试则对半拆
                if retry_count == 2 and len(trans_list) > 1:
                    retry_count -= 1
                    LOGGER.warning(
                        f"[解析错误][{filename}:{idx_tip}]仍然出错，拆分重试"
                    )
                    return await self.translate(
                        trans_list[: max(len(trans_list) // 3,1)],
                        gptdict,
                        proofread=proofread,
                        filename=filename,
                    )
                # 单句重试仍错则重置会话
                if retry_count == 3:
                    self.last_translations[filename] = ""
                    LOGGER.warning(
                        f"[解析错误][{filename}:{idx_tip}]单句仍错，重置会话"
                    )
                # 重试中止
                if retry_count >= 4:
                    self.last_translations[filename] = ""
                    LOGGER.error(
                        f"[解析错误][{filename}:{idx_tip}]解析反复出错，跳过本轮翻译"
                    )
                    i = 0 if i < 0 else i
                    while i < len(trans_list):
                        if not proofread:
                            trans_list[i].pre_zh = "(Failed)"+trans_list[i].post_jp
                            trans_list[i].post_zh = "(Failed)"+trans_list[i].post_jp
                            trans_list[i].problem += "翻译失败"
                            trans_list[i].trans_by = f"{token.model_name}(Failed)"
                        else:
                            trans_list[i].proofread_zh = trans_list[i].pre_zh
                            trans_list[i].post_zh = trans_list[i].pre_zh
                            trans_list[i].problem += "翻译失败"
                            trans_list[i].proofread_by = f"{token.model_name}(Failed)"
                        result_trans_list.append(trans_list[i])
                        i = i + 1
                    return i, result_trans_list
                continue
            elif error_flag == False and error_message:
                LOGGER.warning(
                    f"[{filename}:{idx_tip}]解析了{len(trans_list)}句中的{success_count}句，存在问题：{error_message}"
                )

            # 翻译完成，收尾
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

        if filename not in self.last_translations:
            self.last_translations[filename] = ""

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

            dic_prompt = gpt_dic.gen_prompt(trans_list_split, "tsv") if gpt_dic else ""

            num, trans_result = await self.translate(
                trans_list_split, dic_prompt, proofread=proofread, filename=filename
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
                await save_transCache_to_json(trans_list, cache_file_path)
                transl_step_count = 0

            trans_by = trans_result[0].trans_by
            LOGGER.info(
                f"{filename}: {str(len(trans_result_list))}/{str(len_trans_list)} with {trans_by}"
            )

        return trans_result_list

    def reset_conversation(self, filename=""):
        self.last_translations[filename] = ""

    def restore_context(
        self, translist_unhit: CTransList, num_pre_request: int, filename=""
    ):
        if translist_unhit[0].prev_tran == None:
            return
        tmp_context = []
        num_count = 0
        current_tran = translist_unhit[0].prev_tran
        while current_tran != None:
            if current_tran.pre_zh == "":
                current_tran = current_tran.prev_tran
                continue
            speaker_name = current_tran.get_speaker_name()
            speaker = speaker_name if speaker_name else "null"
            tmp_obj = f"{speaker}\t{current_tran.pre_zh}\t{current_tran.index}"
            tmp_context.append(tmp_obj)
            num_count += 1
            if num_count >= num_pre_request:
                break
            current_tran = current_tran.prev_tran

        tmp_context.reverse()
        json_lines = "\n".join(tmp_context)
        self.last_translations[filename] = "NAME\tDST\tID\n" + json_lines
        # LOGGER.info("-> 恢复了上下文")


if __name__ == "__main__":
    pass
