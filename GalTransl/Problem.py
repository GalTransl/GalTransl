"""
分析问题
"""

from GalTransl.CSentense import CTransList
from GalTransl.ConfigHelper import CProjectConfig, CProblemType
from GalTransl.Utils import (
    get_most_common_char,
    contains_japanese,
    contains_english,
    punctuation_zh,
    contains_korean,
)
from GalTransl.Dictionary import CGptDict


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
        pre_jp = tran.pre_jp
        post_jp = tran.post_jp
        pre_zh = tran.pre_zh
        post_zh = tran.post_zh
        if pre_zh == "":
            continue
        n_symbol = ""
        if "\\r\\n" in pre_jp:
            n_symbol = "\\r\\n"
        elif "\r\n" in pre_jp:
            n_symbol = "\r\n"
        elif "\\n" in pre_jp:
            n_symbol = "\\n"
        elif "\n" in pre_jp:
            n_symbol = "\n"
        if projectConfig.getlbSymbol() != "auto" and projectConfig.getlbSymbol() != "":
            n_symbol = projectConfig.getlbSymbol()

        problem_list = []
        if CProblemType.词频过高 in find_type:
            most_word, word_count = get_most_common_char(pre_zh)
            most_word_jp, word_count_jp = get_most_common_char(pre_jp)
            if word_count > 20 and word_count>word_count_jp*2:
                problem_list.append(f"词频过高-'{most_word}'{str(word_count)}次")
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
                    if not any(char in pre_jp for char in chars):
                        if any(char in post_zh for char in chars):
                            problem_list.append(f"本无{error}")
                    elif any(char in pre_jp for char in chars):
                        if not any(char in post_zh for char in chars):
                            problem_list.append(f"本有{error}")
                else:
                    if chars not in pre_jp:
                        if chars in post_zh:
                            problem_list.append(f"本无{error}")
                    elif chars in pre_jp:
                        if chars not in post_zh:
                            problem_list.append(f"本有{error}")

            if contains_korean(pre_zh) and not contains_korean(pre_jp):
                problem_list.append("本无韩文")
        if CProblemType.残留日文 in find_type:
            if contains_japanese(pre_zh) and contains_japanese(post_zh):
                problem_list.append("残留日文")
        if CProblemType.丢失换行 in find_type and n_symbol != "":
            if pre_jp.count(n_symbol) > post_zh.count(n_symbol):
                problem_list.append("丢失换行")
        if CProblemType.多加换行 in find_type and n_symbol != "":
            if pre_jp.count(n_symbol) < post_zh.count(n_symbol):
                problem_list.append("多加换行")
        if CProblemType.比日文长 in find_type or CProblemType.比日文长严格 in find_type:
            len_beta = 1.3
            if CProblemType.比日文长严格 in find_type:
                len_beta = 1.0
            if len(post_zh) > len(pre_jp) * len_beta:
                problem_list.append(
                    f"比日文长{round(len(post_zh)/max(len(pre_jp),0.1),1)}倍"
                )
        if CProblemType.字典使用 in find_type:
            if val := gpt_dict.check_dic_use(pre_zh, tran):
                problem_list.append(val)
        if CProblemType.引入英文 in find_type:
            if not contains_english(post_jp) and contains_english(pre_zh):
                if contains_english(post_zh):  # 修了的不显示
                    problem_list.append("引入英文")
        if CProblemType.语言不通 in find_type:
            tmp_text = pre_zh
            for chr in punctuation_zh:
                tmp_text = tmp_text.replace(chr, "")
            if len(tmp_text) > 3:
                import langid

                result = langid.classify(tmp_text)
                lang_id = result[0]
                if lang_id == "zh":
                    lang_id = "zh-cn"
                if lang_id != projectConfig.target_lang:
                    if lang_id != "ja":
                        problem_list.append(f"语言不通")

        if arinashi_dict != {}:
            for key, value in arinashi_dict.items():
                if key not in pre_jp and value in post_zh:
                    problem_list.append(f"本无 {key} 译有 {value}")
                if key in pre_jp and value not in post_zh:
                    problem_list.append(f"本有 {key} 译无 {value}")

        if "(Failed)" in post_zh:
            problem_list.append("翻译失败")

        if problem_list:
            tran.problem += ", ".join(problem_list)
