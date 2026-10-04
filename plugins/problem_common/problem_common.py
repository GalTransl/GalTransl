"""
分析问题
"""

from enum import Enum
from collections import Counter
from string import ascii_letters, digits, punctuation
from typing import List, Tuple
import re
import math

from GalTransl.CSentense import CSentense
from GalTransl.ConfigHelper import CProjectConfig
from GalTransl.GTPlugin import GProblemPlugin
from GalTransl.Dictionary import CGptDict


_DECIMAL_LITERAL_RE = re.compile(r"^[+-]?(\d+(\.\d+)?|\.\d+)([eE][+-]?\d+)?$")


def normalize_sentence_length_threshold(raw) -> int:
    if isinstance(raw, bool):
        return 17
    if isinstance(raw, str) and not _DECIMAL_LITERAL_RE.match(raw.strip()):
        return 17
    try:
        number = float(raw)
    except (TypeError, ValueError, OverflowError):
        return 17
    if not math.isfinite(number) or not number.is_integer() or number <= 0:
        return 17
    return int(number)


def extract_control_substrings(text: str) -> list[str]:
    """
    提取文本中所有以英文标点符号开头，且仅包含英文字母、数字和标点符号的子串。

    Args:
        text: 输入的文本字符串。

    Returns:
        一个包含所有匹配子串的列表。
    """
    # 定义允许的字符集：英文字母、数字和标点符号
    # string.punctuation 包含 !"#$%&'()*+,-./:;<=>?@[]^_`{|}~
    # string.ascii_letters 包含 a-z 和 A-Z
    # string.digits 包含 0-9
    allowed_chars = ascii_letters + digits + punctuation
    first_punctuation = r"""!#$%&()*+-./:;<=>?@[\]^_`{|}~"""
    # 构建正则表达式：
    # 1. [{re.escape(string.punctuation)}] - 匹配一个英文标点符号作为开头
    # 2. [{re.escape(allowed_chars)}]* - 匹配零个或多个由允许字符组成的后续部分
    # re.escape() 用于转义字符集中的特殊正则字符（如 `[` `]` `^` `-`）
    pattern = f"[{re.escape(first_punctuation)}][{re.escape(allowed_chars)}]*"
    
    # 使用 re.findall 查找所有匹配的子串
    return re.findall(pattern, text)


def get_most_common_char(input_text: str) -> Tuple[str, int]:
    """
    此函数接受一个字符串作为输入，并返回该字符串中最常见的字符及其出现次数。
    它会忽略黑名单中的字符，包括 "." 和 "，"。

    参数:
    - input_text: 一段文本字符串。

    返回值:
    - 包含最常见字符及其出现次数的元组。
    """
    black_list: List[str] = [".", "，"]
    counter: Counter = Counter(input_text)
    most_common = counter.most_common()
    most_char: str = ""
    most_char_count: int = 0
    for char in most_common:
        if char[0] not in black_list:
            most_char = char[0]
            most_char_count = char[1]
            break
    return most_char, most_char_count


def contains_japanese(text: str) -> str:
    """
    此函数接受一个字符串作为输入，检查其中是否包含日文字符。

    参数:
    - text: 要检查的字符串。

    返回值:
    - 如果字符串中包含日文字符，则返回 True，否则返回 False。
    """
    # 日文字符范围
    hiragana_range = (0x3040, 0x309F)
    katakana_range = (0x30A0, 0x30FF)
    katakana_range2 = (0xFF66, 0xFF9F)

    jp_chars = set()
    # 检查字符串中的每个字符
    for char in text:
        # 黑名单
        if char in ["ー", "・"]:
            continue
        # 获取字符的 Unicode 码点
        code_point = ord(char)
        # 检查字符是否在日文字符范围内
        if (
            hiragana_range[0] <= code_point <= hiragana_range[1]
            or katakana_range[0] <= code_point <= katakana_range[1]
            or katakana_range2[0] <= code_point <= katakana_range2[1]
        ):
            jp_chars.add(char)
    return "".join(jp_chars)


def contains_korean(text: str) -> bool:
    """
    此函数接受一个字符串作为输入，检查其中是否包含韩文字符。

    参数:
    - text: 要检查的字符串。

    返回值:
    - 如果字符串中包含韩文字符，则返回 True，否则返回 False。
    """
    # 韩文字符范围
    hangul_jamo_range = (0x1100, 0x11FF)  # 韩文声母和韵母
    hangul_compatibility_jamo_range = (0x3130, 0x318F)  # 韩文兼容声母和韵母
    hangul_syllables_range = (0xAC00, 0xD7AF)  # 韩文音节

    # 检查字符串中的每个字符
    for char in text:
        # 获取字符的 Unicode 码点
        code_point = ord(char)
        # 检查字符是否在韩文字符范围内
        if (
            hangul_jamo_range[0] <= code_point <= hangul_jamo_range[1]
            or hangul_compatibility_jamo_range[0]
            <= code_point
            <= hangul_compatibility_jamo_range[1]
            or hangul_syllables_range[0] <= code_point <= hangul_syllables_range[1]
        ):
            return True
    return False


def is_all_gbk(s):
    if s == "":
        return ""
    
    non_gbk_chars = set()
    for char in s:
        try:
            char.encode('gbk')
        except UnicodeEncodeError:
            non_gbk_chars.add(char)
    
    return str("".join(non_gbk_chars))


def contains_english(text: str) -> str:
    """
    此函数接受一个字符串作为输入，检查其中是否包含英文字符。

    参数:
    - text: 要检查的字符串。

    返回值:
    - 如果字符串中包含英文字符，则返回 True，否则返回 False。
    """
    # 英文字符范围
    english_range = (0x0041, 0x005A)
    english_range2 = (0x0061, 0x007A)
    english_range3 = (0xFF21, 0xFF3A)
    english_range4 = (0xFF41, 0xFF5A)

    eng_chars = ""
    # 检查字符串中的每个字符
    for char in text:
        # 获取字符的 Unicode 码点
        code_point = ord(char)
        # 检查字符是否在英文字符范围内
        if (
            english_range[0] <= code_point <= english_range[1]
            or english_range2[0] <= code_point <= english_range2[1]
            or english_range3[0] <= code_point <= english_range3[1]
            or english_range4[0] <= code_point <= english_range4[1]
        ):
            eng_chars += char
    return eng_chars


class CProblemType(Enum):
    """Common checks and their display metadata, owned by this plugin."""

    def __new__(cls, value, description, default_enabled):
        member = object.__new__(cls)
        member._value_ = value
        member.description = description
        member.default_enabled = default_enabled
        return member

    词频过高 = 1, "某字在译文中重复大于 20 次（且远多于原文）。", True
    标点错漏 = 2, "括号/引号/冒号等标点与原文不一致。", True
    本无括号 = 标点错漏
    本无引号 = 标点错漏
    残留日文 = 3, "译文中残留日文平假名或片假名。", True
    丢失换行 = 4, "译文缺少原文中的行内换行。", False
    多加换行 = 5, "译文换行符比原文多，可能导致溢出。", True
    比日文长 = 6, "译文长度超过原文 1.3 倍（常用，宽松阈值）。", True
    字典使用 = 7, "没有按 GPT 字典的要求翻译。", True
    引入英文 = 8, "原文无英文，但译文引入了英文单词。", False
    比日文长严格 = 9, "译文长度超过原文（零容忍，严格阈值）。", False
    语言不通 = 10, "译文包含大量非 GBK 字符（仅对中文目标语言生效）。", True
    缺控制符 = 11, "译文缺少原文中的控制符（如 \\n、变量标记等）。", True
    独白男他 = 12, "独白（无name）译文出现'他'。", True
    单句过长 = 13, "译文单句过长，平均分句长度超过阈值（avgSentenceLengthThreshold）。", False

MONOLOGUE_MALE_HE_EXCLUDES = (
    "其他",
    "他们",
    "他人",
    "他乡",
    "他国",
    "他日",
    "他山",
)

def _check_problems(
    tran: CSentense,
    projectConfig: CProjectConfig,
    gpt_dict: CGptDict = None,
    sentence_length_threshold: int = 17,
) -> list[str]:
    """Return the legacy check results for a sentence."""
    arinashi_dict = projectConfig.getProblemAnalyzeArinashiDict()
    find_type = projectConfig.getProblemAnalyzeConfig("problemList")
    # Other plugins may add names to the same project problem list.
    find_type = {
        CProblemType.__members__[name]
        for item in find_type
        if (name := getattr(item, "name", item)) in CProblemType.__members__
    }

    if getattr(tran, "skip_check", False):
        return []

    pre_src = tran.pre_src
    post_src = tran.post_src
    pre_dst = tran.pre_dst
    post_dst = tran.post_dst
    if pre_dst == "":
        return []
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
        if avg_sentence_length > sentence_length_threshold:
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
    if CProblemType.字典使用 in find_type and gpt_dict is not None:
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

    return problem_list


class CommonProblemPlugin(GProblemPlugin):
    problem_types = CProblemType
    sentence_length_threshold: int = 17

    def gtp_init(self, plugin_conf, project_conf):
        self.sentence_length_threshold = normalize_sentence_length_threshold(
            plugin_conf.get("Settings", {}).get("avgSentenceLengthThreshold", 17)
        )

    def check(self, tran, project_config, gpt_dict=None) -> list[str]:
        return _check_problems(tran, project_config, gpt_dict, self.sentence_length_threshold)
