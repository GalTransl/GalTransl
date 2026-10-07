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
from GalTransl.Utils import extract_code_blocks
from GalTransl.Backend.Prompts import (
    FORGAL_MARKDOWN_FOLLOWUP_PROMPT,
    FORGAL_MARKDOWN_SYSTEM,
    FORGAL_MARKDOWN_TRANS_PROMPT_EN,
    H_WORDS_LIST,
)
from GalTransl.Backend.MultiTurnTranslate import MultiTurnTranslate
from openai._types import NOT_GIVEN


def newline_symbol(text):
    for symbol in ("\\r\\n", "\r\n", "\\n", "\n"):
        if symbol in text:
            return symbol
    return ""


def markdown_row(*cells):
    def escape(cell):
        cell = cell.replace("\r\n", "<br>").replace("\n", "<br>")
        return cell.replace("\\", "\\\\").replace("|", "\\|")
    return "| " + " | ".join(escape(cell) for cell in cells) + " |"


def parse_markdown_row(line):
    line = line.strip()
    if not line.startswith("|") or not line.endswith("|"):
        return None
    cells, cell = [], []
    body = line[1:-1]
    i = 0
    while i < len(body):
        char = body[i]
        if char == "\\" and i + 1 < len(body) and body[i + 1] in "\\|":
            i += 1
            cell.append(body[i])
        elif char == "|":
            cells.append("".join(cell).strip())
            cell = []
        else:
            cell.append(char)
        i += 1
    cells.append("".join(cell).strip())
    return cells


class ForGalMarkdownTranslate(MultiTurnTranslate):
    followup_prompt = FORGAL_MARKDOWN_FOLLOWUP_PROMPT

    # init
    def __init__(
        self,
        config: CProjectConfig,
        eng_type: str,
        proxy_pool: Optional[CProxyPool],
        token_pool: COpenAITokenPool,
    ):
        super().__init__(config, eng_type, proxy_pool, token_pool)
        self.trans_prompt = FORGAL_MARKDOWN_TRANS_PROMPT_EN
        self.system_prompt = FORGAL_MARKDOWN_SYSTEM
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
        tmp_enhance_jailbreak = False
        n_symbol = ""
        idx_tip = self._build_idx_tip(trans_list)

        for i, trans in enumerate(trans_list):
            speaker_name = trans.get_speaker_name()
            speaker = speaker_name if speaker_name else "null"
            speaker = speaker.replace("\r\n", "").replace("\t", "").replace("\n", "")

            src_text = trans.post_src
            n_symbol = newline_symbol(src_text)
            src_text = src_text.replace("\t", "[t]")
            if n_symbol:
                src_text = src_text.replace(n_symbol, "<br>")

            tmp_obj = markdown_row(str(trans.index), speaker, src_text)
            input_list.append(tmp_obj)

        input_src = "| ID | NAME | SRC |\n| --- | --- | --- |\n" + "\n".join(input_list)

        self.restore_context(trans_list, self.contextNum, filename)

        prompt_template = self._build_prompt_request(input_src, gptdict)

        retry_count = 0
        emitted_success_indices = set()
        while True:  # 一直循环，直到得到数据
            self._check_stop_requested()
            if self.enhance_jailbreak or tmp_enhance_jailbreak:
                assistant_prompt = ""
            else:
                assistant_prompt = ""

            messages, session, prompt_req = self._prepare_translation_messages(
                prompt_template, input_src, gptdict, trans_list, filename,
                proofread=proofread, assistant_prompt=assistant_prompt,
            )

            if self.pj_config.active_workers == 1:
                LOGGER.info(
                    f"->{'翻译输入' if not proofread else '校对输入'}：\n{gptdict}\n{input_src}\n"
                )
                LOGGER.info("->输出：")
            parsed_result_trans_list = []
            stream_parse_error_message = ""
            stream_cursor = {"i": -1, "success_count": 0}

            def _parse_stream_lines(lines, is_final_chunk):
                nonlocal stream_parse_error_message, parsed_result_trans_list
                if stream_parse_error_message:
                    return False
                for raw_line in lines:
                    line = raw_line.strip()
                    if not line or line.startswith("```"):
                        continue
                    parse_ok, parse_error = self._parse_markdown_result_line(
                        line,
                        trans_list,
                        self._get_chatbot_state()[1],
                        n_symbol,
                        stream_cursor,
                        parsed_result_trans_list,
                        filename=filename,
                        emit_runtime_success=(not proofread),
                        emitted_success_indices=emitted_success_indices,
                    )
                    if not parse_ok:
                        stream_parse_error_message = parse_error
                        return False
                return True
            resp = None
            self._clear_chatbot_state()
            reasoning = {}
            resp, token = await self._ask_translation_chatbot(
                session,
                reasoning_holder=reasoning,
                messages=messages,
                file_name=f"{filename}:{idx_tip}",
                base_try_count=retry_count,
                stream_line_callback=_parse_stream_lines,
                max_retry_count=self.max_api_retries,
                progress_file=filename,
            )

            result_text = resp or ""

            i = -1
            success_count = 0
            result_trans_list = []
            result_lines = result_text.splitlines()
            error_flag = False
            error_message = ""

            if result_text == "":
                error_message = "输出为空/被拦截"
                error_flag = True

            if self._get_chatbot_state()[0]:
                if stream_parse_error_message:
                    error_message = stream_parse_error_message
                    error_flag = True
                result_trans_list = parsed_result_trans_list
                success_count = len(parsed_result_trans_list)
                i = stream_cursor["i"]
            else:
                for line in result_lines:
                    line = line.strip()
                    if line.startswith("```"):
                        continue
                    if line.strip() == "":
                        continue

                    parse_ok, parse_error = self._parse_markdown_result_line(
                        line,
                        trans_list,
                        getattr(token, "model_name", ""),
                        n_symbol,
                        stream_cursor,
                        result_trans_list,
                        filename=filename,
                        emit_runtime_success=False,
                        emitted_success_indices=emitted_success_indices,
                    )
                    if not parse_ok:
                        error_message = parse_error
                        error_flag = True
                        break
                    i = stream_cursor["i"]
                    success_count = stream_cursor["success_count"]

            if error_message or stream_parse_error_message or success_count != len(trans_list):
                self._raise_parse_error_for_auto(error_message or stream_parse_error_message or f"译文缺句：{success_count}/{len(trans_list)}")

            if success_count > 0 and not stream_parse_error_message:
                error_flag = False  # 部分解析

            if not error_flag and success_count <= 0 and not result_trans_list:
                error_message = "未解析到有效句子"
                error_flag = True

            if error_flag:
                try:
                    from GalTransl.server import record_runtime_error
                    record_runtime_error(
                        getattr(self.pj_config, "runtime_project_dir", self.pj_config.getProjectDir()),
                        kind="parse",
                        message=error_message,
                        filename=filename,
                        index_range=idx_tip,
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
                await self._interruptible_sleep(1)

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
                    self.reset_conversation(filename)
                    LOGGER.warning(
                        f"[解析错误][{filename}:{idx_tip}]连续3次出错，尝试清空上文"
                    )
                # 重试中止
                if retry_count >= 4:
                    self.reset_conversation(filename)
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
            self._remember_translation_turn(
                session, prompt_req, resp or "", reasoning, trans_list,
                success_count, error_message, assistant_prompt,
            )
            break
        return i + 1, result_trans_list

    def _parse_markdown_result_line(
        self,
        line: str,
        trans_list: CTransList,
        model_name: str,
        n_symbol: str,
        cursor: dict,
        result_trans_list: list,
        filename: str = "",
        emit_runtime_success: bool = False,
        emitted_success_indices: Optional[Set[int]] = None,
    ):
        line_sp = parse_markdown_row(line)
        if line_sp is None or len(line_sp) != 3:
            return False, f"无法解析行：{line}"

        if not cursor.get("header"):
            if line_sp != ["ID", "NAME", "DST"]:
                return False, "缺少 Markdown 表头 ID | NAME | DST"
            cursor["header"] = True
            return True, ""
        if not cursor.get("separator"):
            if not all(re.fullmatch(r":?-{3,}:?", cell) for cell in line_sp):
                return False, "缺少 Markdown 表格分隔行"
            cursor["separator"] = True
            return True, ""

        cursor["i"] += 1
        i = cursor["i"]
        if i > len(trans_list) - 1:
            return False, f"无法解析行：{line}"

        line_id = line_sp[0]
        if str(trans_list[i].index) != line_id:
            return False, f"{line_id}句id未对应{trans_list[i].index}"

        line_dst = line_sp[2]
        if trans_list[i].post_src != "" and line_dst == "":
            return False, f"第{line_id}句空白"
        if "�" in line_dst:
            return False, f"第{line_id}句包含乱码：{line_dst}"

        line_dst = self._normalize_parsed_translation_text(
            line_dst, trans_list[i], newline_symbol(trans_list[i].post_src)
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
            glossary_style="tsv",
            failed_markers=("(Failed)",),
            h_words_list=H_WORDS_LIST,
            ensure_last_translations=True,
        )

    def _format_restore_context_line(self, current_tran: CSentense) -> str:
        speaker_name = current_tran.get_speaker_name()
        speaker = speaker_name if speaker_name else "null"
        return markdown_row(str(current_tran.index), speaker, current_tran.pre_dst)

    def _format_restore_context_payload(self, lines: List[str]) -> str:
        return "| ID | NAME | DST |\n| --- | --- | --- |\n" + "\n".join(lines)


if __name__ == "__main__":
    pass
