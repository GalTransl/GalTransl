"""
工具函数
"""

import os
import codecs
from pathlib import Path
from typing import Tuple, List
from re import compile
import requests
import re
from GalTransl.RuntimePaths import get_active_translation_guidelines_dir

PATTERN_CODE_BLOCK = compile(r"```([\w]*)\n([\s\S]*?)\n```")
whitespace = " \t\n\r\v\f"
ascii_lowercase = "abcdefghijklmnopqrstuvwxyz"
ascii_uppercase = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
ascii_letters = ascii_lowercase + ascii_uppercase
digits = "0123456789"
hexdigits = digits + "abcdef" + "ABCDEF"
octdigits = "01234567"
punctuation = r"""!"#$%&'()*+,-./:;<=>?@[\]^_`{|}~"""
punctuation_zh = "。？！…（）；：《》「」『』【】"
printable = digits + ascii_letters + punctuation + whitespace

def _resolve_guideline_path(file_path: str) -> Path:
    candidate = Path(file_path).expanduser()
    if candidate.is_absolute():
        return candidate
    if candidate.parts and candidate.parts[0] == "translation_guidelines":
        candidate = Path(*candidate.parts[1:])
    return get_active_translation_guidelines_dir() / candidate


def load_guideline_file(file_path: str) -> str:
    resolved_path = _resolve_guideline_path(file_path)
    try:
        with open(resolved_path, "r", encoding="utf-8") as file:
            return file.read()
    except Exception as e:
        print(f"Error reading translation_guideline file {resolved_path}: {e}")
        raise e


def contains_katakana(text: str) -> bool:
    # 日文字符范围
    katakana_range = (0x30A0, 0x30FF)

    # 检查字符串中的每个字符
    for char in text:
        # 排除ー
        if char in ["ー", "・"]:
            continue
        # 获取字符的 Unicode 码点
        code_point = ord(char)
        # 检查字符是否在日文字符范围内
        if katakana_range[0] <= code_point <= katakana_range[1]:
            return True
    return False


def is_all_chinese(text: str) -> bool:
    """
    此函数接受一个字符串作为输入，检查其中是否 *全部* 都是中文字符 (汉字)。
    (使用循环检查每个字符)

    参数:
    - text: 要检查的字符串。

    返回值:
    - 如果字符串中的 *所有* 字符都是中文字符，则返回 True，否则返回 False。
      如果字符串为空，则返回 False。
    """
    if not text:
        return False

    # 定义中文字符的 Unicode 范围
    cjk_unified_range = (0x4E00, 0x9FFF)
    cjk_extension_a_range = (0x3400, 0x4DBF)
    cjk_compatibility_range = (0xF900, 0xFAFF)
    # 添加更多扩展区... (注意：大于 0xFFFF 的码点需要特殊处理或 Python 3.3+ 支持)
    # cjk_extension_b_range = (0x20000, 0x2A6DF)

    for char in text:
        code_point = ord(char)

        # 检查字符 *是否在* 任何一个定义的中文范围内
        is_chinese = (
            (cjk_unified_range[0] <= code_point <= cjk_unified_range[1])
            or (cjk_extension_a_range[0] <= code_point <= cjk_extension_a_range[1])
            or (cjk_compatibility_range[0] <= code_point <= cjk_compatibility_range[1])
            # Add checks for other ranges here if needed, e.g.:
            # or (cjk_extension_b_range[0] <= code_point <= cjk_extension_b_range[1])
        )

        # 如果当前字符 *不是* 中文字符，则整个字符串不满足条件，立即返回 False
        if not is_chinese:
            return False

    # 如果循环正常结束，说明所有字符都是中文字符
    return True

def extract_code_blocks(content: str) -> Tuple[List[str], List[str]]:
    # 匹配带语言标签的代码块
    matches_with_lang = PATTERN_CODE_BLOCK.findall(content)

    # 提取所有匹配到的带语言标签的代码块
    lang_list = []
    code_list = []
    for match in matches_with_lang:
        lang_list.append(match[0])
        code_list.append(match[1])

    return lang_list, code_list


def get_file_name(file_path: str) -> str:
    """
    获取文件名，不包含扩展名
    """
    base_name = os.path.basename(file_path)
    file_name, _ = os.path.splitext(base_name)
    return file_name


def get_file_list(directory: str):
    file_list = []
    for dirpath, dirnames, filenames in os.walk(directory):
        for file in filenames:
            file_list.append(os.path.join(dirpath, file))
    return file_list


def process_escape(text: str) -> str:
    return codecs.escape_decode(bytes(text, "utf-8"))[0].decode("utf-8")


pattern_fix_quotes = compile(r'"dst": *"(.+?)"}')


def fix_quotes(text):
    results = pattern_fix_quotes.findall(text)
    for match in results:
        new_match = match
        for i in range(match.count('"')):
            if i % 2 == 0:
                new_match = new_match.replace('"', "“", 1).replace(r"\“", "“", 1)
            else:
                new_match = new_match.replace('"', "”", 1).replace(r"\”", "”", 1)
        text = text.replace(match, new_match)
    return text


def fix_quotes2(text):
    if text.startswith('"') and text.endswith('"'):
        text = f"“{text[1:-1]}”"
    for i in range(text.count('"')):
        if i % 2 == 0:
            text = text.replace('"', "“", 1).replace(r"\“", "“", 1)
        else:
            text = text.replace('"', "”", 1).replace(r"\”", "”", 1)
    return text


def get_n_symbol(src_text: str):
    n_symbols = []
    if "\r\n" in src_text:
        n_symbols.append("\r\n")
    if "\n" in src_text and "\r\n" not in src_text:
        n_symbols.append("\n")
    if "\\r\\n" in src_text:
        n_symbols.append("\\r\\n")
    if "\\n" in src_text and "\\r\\n" not in src_text:
        n_symbols.append("\\n")

    return n_symbols


def check_for_tool_updates(new_version):
    try:
        release_api = "https://api.github.com/repos/xd2333/GalTransl/releases/latest"
        response = requests.get(release_api, timeout=5).json()
        latest_release = response["tag_name"]
        new_version.append(latest_release)
    except Exception:
        pass


def find_most_repeated_substring(text):
    max_count = 0
    max_substring = ""
    n = len(text)

    for i in range(n):
        for j in range(i + 1, n + 1):
            substring = text[i:j]
            count = 1
            start = j
            while (
                start + len(substring) <= n
                and text[start : start + len(substring)] == substring
            ):
                count += 1
                start += len(substring)

            if count > max_count or (
                count == max_count and len(substring) > len(max_substring)
            ):
                max_count = count
                max_substring = substring

    return max_substring, max_count


def decompress_file_lzma(input_filepath, output_filepath=None):
    """
    解压缩使用 LZMA 算法压缩的单个文件。

    Args:
        input_filepath (str): 要解压缩的输入文件路径 (通常以 '.xz' 结尾)。
        output_filepath (str, optional): 解压缩后的输出文件路径。
                                         如果为 None，则移除输入文件名中的 '.xz'。
    """
    import lzma

    if output_filepath is None:
        if input_filepath.endswith(".xz"):
            output_filepath = input_filepath[:-3]
        else:
            print(
                "错误: 输入文件名不以 '.xz' 结尾，无法自动确定输出文件名。请指定 output_filepath。"
            )
            return

    try:
        with lzma.open(input_filepath, "rb") as f_in, open(
            output_filepath, "wb"
        ) as f_out:
            while True:
                chunk = f_in.read(4096)
                if not chunk:
                    break
                f_out.write(chunk)
        # print(f"文件 '{input_filepath}' 已成功解压缩为 '{output_filepath}'")
    except FileNotFoundError:
        print(f"错误: 文件 '{input_filepath}' 未找到。")
    except Exception as e:
        print(f"解压缩文件时发生错误: {e}")


if __name__ == "__main__":
    pass
