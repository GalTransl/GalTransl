import json, time, asyncio, os, traceback, re
from opencc import OpenCC
from typing import Optional, List, Set

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
from GalTransl.Utils import extract_code_blocks, fix_quotes
from GalTransl.Backend.Prompts import (
    FORGAL_JSON_SYSTEM_PROMPT,
    FORGAL_JSON_TRANS_PROMPT,
    H_WORDS_LIST,
)
from GalTransl.Backend.BaseTranslate import BaseTranslate
from openai._types import NOT_GIVEN


class ForGalJsonTranslate(BaseTranslate):
    _SIGCHARS = "abcdefghijklmnopqrstuvwxyz0123456789"

    def _encode_sig_jsonline(self, sig: str, obj: dict) -> str:
        return f"{sig}|" + json.dumps(obj, ensure_ascii=False)

    # init
    def __init__(
        self,
        config: CProjectConfig,
        eng_type: str,
        proxy_pool: Optional[CProxyPool],
        token_pool: COpenAITokenPool,
    ):
        super().__init__(config, eng_type, proxy_pool, token_pool)
        self.trans_prompt = FORGAL_JSON_TRANS_PROMPT
        self.system_prompt = FORGAL_JSON_SYSTEM_PROMPT
        self._apply_internal_prompt_template_overrides()
        # enhance_jailbreak
        if val := config.getKey("gpt.enhance_jailbreak"):
            self.enhance_jailbreak = val
        else:
            self.enhance_jailbreak = False

        self.last_translations = {}
        self.init_chatbot(eng_type=eng_type, config=config)
        self._set_temp_type("precise")

        pass

    async def translate(
        self, trans_list: CTransList, gptdict="", proofread=False, filename=""
    ):
        input_list = []
        sig_list = []
        tmp_enhance_jailbreak = False
        n_symbol = ""
        idx_tip = self._build_idx_tip(trans_list)

        for i, trans in enumerate(trans_list):
            speaker_name = trans.get_speaker_name()
            speaker = speaker_name if speaker_name else "null"
            speaker = speaker.replace("\r\n", "").replace("\t", "").replace("\n", "")
            src_text = trans.post_src

            if "\\r\\n" in src_text:
                n_symbol = "\\r\\n"
            elif "\r\n" in src_text:
                n_symbol = "\r\n"
            elif "\\n" in src_text:
                n_symbol = "\\n"
            elif "\n" in src_text:
                n_symbol = "\n"

            src_text = src_text.replace("\t", "[t]")
            if n_symbol:
                src_text = src_text.replace(n_symbol, "<br>")

            while True:
                sig = "".join(choice(self._SIGCHARS) for _ in range(3))
                if sig not in sig_list:
                    break
            sig_list.append(sig)

            if not proofread:
                tmp_obj = {
                    "id": trans.index,
                    "name": speaker,
                    "src": src_text,
                }
            else:
                tmp_obj = {
                    "id": trans.index,
                    "name": speaker,
                    "src": src_text,
                    "dst": (
                        trans.pre_dst if trans.proofread_zh == "" else trans.proofread_zh
                    ),
                }

            if tmp_obj["name"] == "null":
                del tmp_obj["name"]

            input_list.append(self._encode_sig_jsonline(sig, tmp_obj))
        input_src = "\n".join(input_list)

        self.restore_context(trans_list, self.contextNum, filename)

        prompt_template = self._build_prompt_request(input_src, gptdict)

        retry_count = 0
        emitted_success_indices = set()
        while True:  # 一直循环，直到得到数据
            self._check_stop_requested()
            if self.enhance_jailbreak or tmp_enhance_jailbreak:
                assistant_prompt = "```jsonline"
            else:
                assistant_prompt = ""

            messages = []
            messages.append({"role": "system", "content": self.system_prompt})
            prompt_req = self._apply_history_result(prompt_template, filename)
            messages.append({"role": "user", "content": prompt_req})
            if assistant_prompt:
                messages.append({"role": "assistant", "content": assistant_prompt})

            if self.pj_config.active_workers == 1:
                LOGGER.info(
                    f"->{'翻译输入' if not proofread else '校对输入'}：\n{gptdict}\n{input_src}\n"
                )
                LOGGER.info("->输出：")
            parsed_result_trans_list = []
            stream_parse_error_message = ""
            stream_cursor = {"i": -1, "success_count": 0, "started": False}

            def _parse_stream_lines(lines, is_final_chunk):
                nonlocal stream_parse_error_message, parsed_result_trans_list
                if stream_parse_error_message:
                    return False
                key_name = "dst" if not proofread else "newdst"
                for raw_line in lines:
                    line = raw_line.strip()
                    if not line:
                        continue
                    if line.startswith("```"):
                        continue
                    if not stream_cursor["started"]:
                        sig_start = re.search(r"\b[a-z0-9]{3}\|\{\"id\"", line)
                        if sig_start:
                            line = line[sig_start.start() :]
                            stream_cursor["started"] = True
                        else:
                            continue
                    line = fix_quotes(line)
                    parse_ok, parse_error = self._parse_jsonline_result_line(
                        line,
                        trans_list,
                        getattr(self, "_last_chatbot_model_name", ""),
                        n_symbol,
                        key_name,
                        stream_cursor,
                        parsed_result_trans_list,
                        filename=filename,
                        emit_runtime_success=(not proofread),
                        emitted_success_indices=emitted_success_indices,
                        sig_list=sig_list,
                    )
                    if not parse_ok:
                        stream_parse_error_message = parse_error
                        return False
                return True

            resp = None
            resp, token = await self.ask_chatbot(
                messages=messages,
                file_name=f"{filename}:{idx_tip}",
                base_try_count=retry_count,
                stream_line_callback=_parse_stream_lines,
            )

            result_text = resp or ""

            if "</think>" in result_text:
                result_text = result_text.split("</think>")[-1]
            if "```json" in result_text:
                lang_list, code_list = extract_code_blocks(result_text)
                if len(lang_list) > 0 and len(code_list) > 0:
                    result_text = code_list[0]
            sig_start = re.search(r"\b[a-z0-9]{3}\|\{\"id\"", result_text)
            if sig_start:
                result_text = result_text[sig_start.start() :]
            result_text = fix_quotes(result_text)

            i = -1
            success_count = 0
            result_trans_list = []
            result_lines = result_text.splitlines()
            error_flag = False
            error_message = ""
            key_name = "dst" if not proofread else "newdst"

            if result_text == "":
                error_message = "输出为空/被拦截"
                error_flag = True

            if getattr(self, "_last_chatbot_was_stream", False):
                if stream_parse_error_message:
                    error_message = stream_parse_error_message
                    error_flag = True
                result_trans_list = parsed_result_trans_list
                success_count = len(parsed_result_trans_list)
                i = stream_cursor["i"]
            else:
                for line in result_lines:
                    parse_ok, parse_error = self._parse_jsonline_result_line(
                        line,
                        trans_list,
                        getattr(token, "model_name", ""),
                        n_symbol,
                        key_name,
                        {"i": i, "success_count": success_count},
                        result_trans_list,
                        filename=filename,
                        emit_runtime_success=False,
                        emitted_success_indices=emitted_success_indices,
                        sig_list=sig_list,
                    )
                    if not parse_ok:
                        error_message = parse_error
                        error_flag = True
                        break
                    i += 1
                    success_count += 1
                    if i >= len(trans_list) - 1:
                        break

            if success_count > 0 and not stream_parse_error_message:
                error_flag = False  # 部分解析

            if error_flag:
                try:
                    from GalTransl.server import record_runtime_error
                    record_runtime_error(
                        getattr(self.pj_config, "runtime_project_dir", self.pj_config.getProjectDir()),
                        kind="parse",
                        message=error_message,
                        filename=filename,
                        index_range=str(idx_tip),
                        retry_count=retry_count + 1,
                        model=getattr(token, "model_name", ""),
                        level="warning",
                    )
                except Exception:
                    pass

                LOGGER.warning(
                    f"[解析错误][{filename}:{idx_tip}]解析结果出错：{error_message}"
                )
                retry_count += 1
                self._check_stop_requested()
                await asyncio.sleep(1)

                tmp_enhance_jailbreak = not tmp_enhance_jailbreak

                # 2次重试则对半拆
                if retry_count == 2 and len(trans_list) > 1 and self.smartRetry:
                    retry_count -= 1
                    LOGGER.warning(
                        f"[解析错误][{filename}:{idx_tip}]连续2次出错，尝试拆分重试"
                    )
                    return await self.translate(
                        trans_list[: max(len(trans_list) // 3,1)],
                        gptdict,
                        proofread=proofread,
                        filename=filename,
                    )
                # 单句重试仍错则重置会话
                if retry_count == 3 and self.smartRetry:
                    self.last_translations[filename] = ""
                    LOGGER.warning(
                        f"[解析错误][{filename}:{idx_tip}]连续3次出错，尝试清空上文"
                    )
                # 重试中止
                if retry_count >= 4:
                    self.last_translations[filename] = ""
                    LOGGER.error(
                        f"[解析错误][{filename}:{idx_tip}]解析反复出错，跳过本轮翻译"
                    )
                    i = self._append_parse_failure_fallback_results(
                        trans_list,
                        0 if i < 0 else i,
                        result_trans_list,
                        getattr(token, "model_name", ""),
                        proofread=proofread,
                        translate_failed_prefix="(Failed)",
                        translate_problem_message="翻译失败",
                        proofread_problem_message="翻译失败",
                        proofread_problem_append=True,
                    )
                    return i, result_trans_list
                continue
            elif error_flag == False and error_message:
                LOGGER.warning(
                    f"[{filename}:{idx_tip}]解析了{len(trans_list)}句中的{success_count}句，存在问题：{error_message}"
                )

            # 翻译完成，收尾
            break
        return success_count, result_trans_list

    def _parse_jsonline_result_line(
        self,
        line: str,
        trans_list: CTransList,
        model_name: str,
        n_symbol: str,
        key_name: str,
        cursor: dict,
        result_trans_list: list,
        filename: str = "",
        emit_runtime_success: bool = False,
        emitted_success_indices: Optional[Set[int]] = None,
        sig_list: Optional[List[str]] = None,
    ):
        if "|" not in line:
            return False, f"jsonline缺少sig前缀：{line}"
        line_sig, line = line.split("|", 1)
        try:
            line_json = json.loads(line)
        except Exception:
            return False, f"json无法解析行：{line}"

        cursor["i"] += 1
        i = cursor["i"]
        if (
            isinstance(line_json, dict) == False
            or "id" not in line_json
            or type(line_json["id"]) != int
            or i > len(trans_list) - 1
        ):
            return False, f"{line}句无法解析"

        line_id = line_json["id"]
        if sig_list is not None:
            if line_sig != sig_list[i]:
                return False, f"第{trans_list[i].index}句疑似串行：期望{sig_list[i]}，实际{line_sig}"
        if line_id != trans_list[i].index:
            return False, f"{line_id}句id未对应{trans_list[i].index}"

        if key_name not in line_json or type(line_json[key_name]) != str:
            return False, f"第{trans_list[i].index}句找不到{key_name}"

        line_dst = line_json[key_name]
        if trans_list[i].post_src != "" and line_dst == "":
            return False, f"第{trans_list[i].index}句空白"
        if "�" in line_dst:
            return False, f"第{trans_list[i].index}句包含乱码：{line_dst}"

        line_dst = self._normalize_parsed_translation_text(
            line_dst, trans_list[i], n_symbol
        )

        return self._append_parsed_translation_result(
            trans_list[i],
            line_dst,
            model_name,
            cursor,
            result_trans_list,
            filename=filename,
            emit_runtime_success=emit_runtime_success,
            emitted_success_indices=emitted_success_indices,
            result_index=i,
        )

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
        return await self._batch_translate_common(
            filename=filename,
            cache_file_path=cache_file_path,
            translist_unhit=translist_unhit,
            num_pre_request=num_pre_request,
            gpt_dic=gpt_dic,
            proofread=proofread,
            glossary_style="gpt",
            failed_markers=("(Failed)", "(翻译失败)"),
            h_words_list=H_WORDS_LIST,
            ensure_last_translations=True,
        )

    def reset_conversation(self, filename=""):
        self.last_translations[filename] = ""

    def _format_restore_context_line(self, current_tran: CSentense) -> str:
        speaker_name = current_tran.get_speaker_name()
        speaker = speaker_name if speaker_name else "null"
        tmp_obj = {
            "id": current_tran.index,
            "name": speaker,
            "dst": current_tran.pre_dst,
        }
        if speaker == "null":
            del tmp_obj["name"]
        return self._encode_sig_jsonline("old", tmp_obj)

    def _format_restore_context_payload(self, lines: List[str]) -> str:
        return "```jsonline\n" + "\n".join(lines) + "\n```"


if __name__ == "__main__":
    pass
