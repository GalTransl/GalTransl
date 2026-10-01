"""
分析问题
"""

import re

from GalTransl.CSentense import CTransList
from GalTransl.ConfigHelper import CProjectConfig, CProblemType
from GalTransl.Utils import (
    get_most_common_char,
    contains_japanese,
    contains_english,
    punctuation_zh,
    contains_korean,
    is_all_gbk,
    extract_control_substrings
)
from GalTransl.Dictionary import CGptDict

MONOLOGUE_MALE_HE_EXCLUDES = (
    "其他",
    "他们",
    "他人",
    "他乡",
    "他国",
    "他日",
    "他山",
)

def find_problems(
    trans_list: CTransList,
    projectConfig: CProjectConfig,
    gpt_dict: CGptDict = None,
) -> None:
    """
    此函数接受一个翻译列表，查找其中的问题并将其记录在每个翻译对象的 `problem` 属性中。

    参数:
    - trans_list: 翻译对象列表。
    - find_type: 要查找的问题类型列表。
    - arinashi_dict: 一个自定义字典，其中的键值对将会被用于查找问题。

    返回值:
    - 无返回值，但会修改每个翻译对象的 `problem` 属性。
    """
    arinashi_dict = projectConfig.getProblemAnalyzeArinashiDict()
    find_type = projectConfig.getProblemAnalyzeConfig("problemList")
    if not find_type:
        find_type = projectConfig.getProblemAnalyzeConfig("GPT35")  # 兼容旧版

    for tran in trans_list:
        if getattr(tran, "skip_check", False):
            tran.problem = ""
            continue

        pre_src = tran.pre_src
        post_src = tran.post_src
        pre_dst = tran.pre_dst
        post_dst = tran.post_dst
        if pre_dst == "":
            continue
        n_symbol = ""
        if "\\r\\n" in pre_src:
            n_symbol = "\\r\\n"
        elif "\r\n" in pre_src:
            n_symbol = "\r\n"
        elif "\\n" in pre_src:
            n_symbol = "\\n"
        elif "\n" in pre_src:
            n_symbol = "\n"
        if projectConfig.getlbSymbol() != "auto" and projectConfig.getlbSymbol() != "":
            n_symbol = projectConfig.getlbSymbol()

        problem_list = []
        if CProblemType.词频过高 in find_type:
            most_word, word_count = get_most_common_char(pre_dst)
            most_word_src, word_count_src = get_most_common_char(pre_src)
            if word_count > 20 and word_count > word_count_src * 2:
                problem_list.append(f"词频过高：'{most_word}'{str(word_count)}次")
        if CProblemType.标点错漏 in find_type:
            char_to_error = {
                ("（", ")"): "括号",
                "：": "冒号",
                "*": "*符号",
                "；": "；符号",
                "[": "[符号",
                "<": "<符号",
                ("『", "「", "“"): "引号",
            }

            for chars, error in char_to_error.items():
                if isinstance(chars, tuple):
                    if not any(char in pre_src for char in chars):
                        if any(char in post_dst for char in chars):
                            problem_list.append(f"本无{error}")
                    elif any(char in pre_src for char in chars):
                        if not any(char in post_dst for char in chars):
                            problem_list.append(f"本有{error}")
                else:
                    if chars not in pre_src:
                        if chars in post_dst:
                            problem_list.append(f"本无{error}")
                    elif chars in pre_src:
                        if chars not in post_dst:
                            problem_list.append(f"本有{error}")

            if contains_korean(pre_dst) and not contains_korean(pre_src):
                problem_list.append("本无韩文")
        if CProblemType.残留日文 in find_type:
            pre_dst_jp_chars = contains_japanese(pre_dst)
            post_dst_jp_chars = contains_japanese(post_dst)
            if pre_dst_jp_chars != "" and post_dst_jp_chars != "":
                problem_list.append(f"残留日文：{post_dst_jp_chars}")
        if CProblemType.丢失换行 in find_type and n_symbol != "":
            if pre_src.count(n_symbol) > post_dst.count(n_symbol):
                problem_list.append("丢失换行")
        if CProblemType.单句过长 in find_type and n_symbol != "":
            n_number = post_dst.count(n_symbol)
            # 去除换行符本身的字符长度，只计算纯文本
            clean_len = len(post_dst) - n_number * len(n_symbol)
            avg_sentence_length = clean_len / (n_number + 1)
            threshold = projectConfig.getAvgSentenceLengthThreshold()
            if avg_sentence_length > threshold:
                problem_list.append("单句过长")
        if CProblemType.多加换行 in find_type and n_symbol != "":
            if pre_src.count(n_symbol) < post_dst.count(n_symbol):
                problem_list.append("多加换行")
        if CProblemType.比日文长 in find_type or CProblemType.比日文长严格 in find_type:
            len_beta = 1.3
            min_diff=8
            if CProblemType.比日文长严格 in find_type:
                len_beta = 1.0
                min_diff=0
            if len(post_dst) > len(pre_src) * len_beta and len(post_dst) - len(pre_src) >= min_diff:
                problem_list.append(
                    f"比日文长：{round(len(post_dst)/max(len(pre_src),0.1),1)}倍({len(post_dst)-len(pre_src)}字符)"

                )
        if CProblemType.字典使用 in find_type:
            if val := gpt_dict.check_dic_use(pre_dst, tran):
                problem_list.append(val)
        if CProblemType.引入英文 in find_type:
            if not contains_english(post_src) and contains_english(pre_dst):
                eng_chars = contains_english(post_dst)
                if len(eng_chars)>4:
                    problem_list.append(f"引入英文：{eng_chars}")
        if CProblemType.语言不通 in find_type:
            if "zh" in projectConfig.target_lang:
                if not is_all_gbk(pre_dst):
                    non_gbk_whites=["♪","♥"]
                    non_gbk_chars = is_all_gbk(post_dst)
                    for non_gbk_white in non_gbk_whites:
                        non_gbk_chars = non_gbk_chars.replace(non_gbk_white,"")
                    if non_gbk_chars !="":
                        problem_list.append(f"语言不通-非GBK：{non_gbk_chars}")
        if CProblemType.缺控制符 in find_type:
            control_list_src = extract_control_substrings(pre_src)
            # 用「子串包含」而不是「token 精确相等」判断是否保留：extract_control_substrings
            # 是按 ASCII 连续段切词的，源文 `[石浦城跡/いしうらじょうあと]`（括号内是日文，
            # 非 ASCII）切出 ['[', '/', ']']，译文 `[石浦城迹/shipuchengji]`（括号内是罗马字，
            # `]` 又在允许字符集里）会把它们并成一个 token `/shipuchengji]`——精确比较就会
            # 误报「缺控制符：/ ]」。真正的控制符（如 `<color=red>`）丢没丢，子串包含同样判得出来。
            lost_list = [
                control_src for control_src in control_list_src
                if control_src not in pre_dst and control_src not in post_dst
            ]
            if lost_list:
                problem_list.append(f"缺控制符：{' '.join(lost_list)}")
        if CProblemType.独白男他 in find_type:
            if tran.speaker == "" and "他" in post_dst:
                if not any(exclude in post_dst for exclude in MONOLOGUE_MALE_HE_EXCLUDES):
                    problem_list.append("独白男他")

        if arinashi_dict != {}:
            for key, value in arinashi_dict.items():
                if key not in pre_src and value in post_dst:
                    problem_list.append(f"本无 {key} 译有 {value}")
                if key in pre_src and value not in post_dst:
                    problem_list.append(f"本有 {key} 译无 {value}")

        if "(Failed)" in post_dst:
            problem_list.append("翻译失败")

        if problem_list:
            # tran.problem 可能已经有内容：失败批次会先把「翻译失败」写进去（见 BaseTranslate
            # 的 _merge_problem_message）。以前的 `+=` 只在新串内部有分隔符、与已有内容之间没有，
            # 于是会黏成「翻译失败残留日文：…」。这里带 ", " 合并，并按问题项去重
            #（「翻译失败」两边都会加），且不改写已有文本。
            existing = {p.strip() for p in re.split(r",\s*", tran.problem) if p.strip()} if tran.problem else set()
            additions: list[str] = []
            for item in problem_list:
                item = item.strip()
                if item and item not in existing:
                    existing.add(item)
                    additions.append(item)
            if additions:
                extra = ", ".join(additions)
                tran.problem = f"{tran.problem}, {extra}" if tran.problem else extra
