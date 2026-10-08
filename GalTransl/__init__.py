import os
import logging
from time import localtime
import threading
from GalTransl.Utils import check_for_tool_updates

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)

PROGRAM_SPLASH1 = r"""
   ____       _ _____                    _ 
  / ___| __ _| |_   _| __ __ _ _ __  ___| |
 | |  _ / _` | | | || '__/ _` | '_ \/ __| |
 | |_| | (_| | | | || | | (_| | | | \__ \ |
  \____|\__,_|_| |_||_|  \__,_|_| |_|___/_|                 

------Translate your favorite Galgame------
"""

PROGRAM_SPLASH2 = r"""
   ______      ________                      __
  / ____/___ _/ /_  __/________ _____  _____/ /
 / / __/ __ `/ / / / / ___/ __ `/ __ \/ ___/ / 
/ /_/ / /_/ / / / / / /  / /_/ / / / (__  ) /  
\____/\__,_/_/ /_/ /_/   \__,_/_/ /_/____/_/   
                                             
-------Translate your favorite Galgame-------
"""

PROGRAM_SPLASH3 = r'''

   ___              _     _____                                     _    
  / __|   __ _     | |   |_   _|    _ _   __ _    _ _      ___     | |   
 | (_ |  / _` |    | |     | |     | '_| / _` |  | ' \    (_-<     | |   
  \___|  \__,_|   _|_|_   _|_|_   _|_|_  \__,_|  |_||_|   /__/_   _|_|_  
_|"""""|_|"""""|_|"""""|_|"""""|_|"""""|_|"""""|_|"""""|_|"""""|_|"""""| 
"`-0-0-'"`-0-0-'"`-0-0-'"`-0-0-'"`-0-0-'"`-0-0-'"`-0-0-'"`-0-0-'"`-0-0-' 

--------------------Translate your favorite Galgame--------------------
'''

PROGRAM_SPLASH4 = r"""
     _____)           ______)                 
   /             /)  (, /                  /) 
  /   ___   _   //     /  __  _  __   _   //  
 /     / ) (_(_(/_  ) /  / (_(_(_/ (_/_)_(/_  
(____ /            (_/                        

-------Translate your favorite Galgame-------
"""
ALL_BANNERS = [PROGRAM_SPLASH1, PROGRAM_SPLASH2, PROGRAM_SPLASH3, PROGRAM_SPLASH4]
PROGRAM_SPLASH = ALL_BANNERS[localtime().tm_mday % 4]

GALTRANSL_VERSION = "8.3.0"
AUTHOR = "xd2333"
CONTRIBUTORS = "ryank231231, PiDanShouRouZhouXD, Noriverwater, Isotr0py, adsf0427, pipixia244, gulaodeng, sakura-umi, lifegpc, natsumerinchan, szyzbg"

CONFIG_FILENAME = "config.yaml"
INPUT_FOLDERNAME = "gt_input"
OUTPUT_FOLDERNAME = "gt_output"
CACHE_FOLDERNAME = "transl_cache"
# 全局翻译规范目录名（程序根目录下）：各项目的 common.gpt.translation_guideline 从这里选一份
GUIDELINES_FOLDERNAME = "translation_guidelines"
# 项目没配置全局规范时兜底读的那一份（见 Backend/BaseTranslate.py）：
# 界面上不允许删它，否则那些项目的翻译会直接报"读不到规范"。
DEFAULT_GUIDELINE_NAME = "Basic.md"
TRANSLATOR_SUPPORTED = {
    "auto-translate": {
        "zh-cn": "模型名含 sakura/galtransl 时固定专用模板、只拆分重试；否则按文件 name 字段选择 Gal/Novel；Gal 按 tool→markdown→json、Novel 按 tool→普通模式切换并拆分文本，共5次请求（重试4次）。",
        "en": "Models containing sakura/galtransl use a fixed specialized template with split-only retries; otherwise select Gal/Novel by the input name field; rotate tool/Markdown/JSON and split on errors, with up to four retries (five attempts total)."
    },
    "ForGal-json": {
        "zh-cn": "(openai接口)翻译Gal时使用，json格式输入，兼容性好。",
        "en": "Customized template for Gal translation, json input. "
    },
    "ForGal-tool": {
        "zh-cn": "(openai接口)通过译文补丁工具分组翻译Gal，需支持函数工具调用，批次间不保留多轮对话。",
        "en": "Gal translation in groups using a translation patch tool; requires function calling support. Each batch is independent."
    },
    "ForNovel-tool": {
        "zh-cn": "(openai接口)通过译文补丁工具翻译小说，输入不带name字段，批次间不保留多轮对话。",
        "en": "Novel translation using a patch tool, without name fields. Each batch is independent."
    },
    "ForNovel": {
        "zh-cn": "(openai接口)翻译轻小说等其他文本时使用，区别是输入不带name字段。",
        "en": " Customized template for Novel translation. "
    },
    "ForGal-markdown": {
        "zh-cn": "(openai接口)翻译Gal时使用，Markdown表格输入和输出，句内换行使用<br>。",
        "en": " Gal translation using Markdown tables for input and output, with <br> line breaks. "
    },
    "galtransl-v3": {
        "zh-cn": "(sakura接口)为翻译Gal基于Sakura进一步优化的本地模型",
        "en": "Further optimized local small model based on Sakura for Gal translation"
    },
    "sakura-v1.0": {
        "zh-cn": "(sakura接口)为翻译轻小说/Gal开展大规模训练的本地模型，具有多个型号和大小",
        "en": "(For v1.0 prompt) Locally trained model for light novel/Gal translation, available in multiple sizes"
    },
    "GenDic": {
        "zh-cn": "(openai接口)自动化构建GPT字典，需要接大模型如Deepseek-V3",
        "en": "Automatically build GPT dictionary, requires a large model, recommended GPT4/Claude-3/Deepseek-V3"
    },
    "rebuildr": {
        "zh-cn": "重建结果 用译前译后字典通过缓存刷写结果json -- 跳过翻译和写缓存",
        "en": "Rebuild results - Use pre/post translation dictionary to rewrite result json via cache - Skip translation and cache writing"
    },
    "rebuilda": {
        "zh-cn": "重建缓存和结果 用译前译后字典刷写缓存+结果json -- 跳过翻译",
        "en": "Rebuild cache and results - Use pre/post translation dictionary to rewrite cache+result json - Skip translation"
    },
    "dump-name": {
        "zh-cn": "导出name字段，生成name替换表，用于翻译name字段",
        "en": "Export name field to generate name replacement table for name field translation"
    },
    "show-plugs": {
        "zh-cn": "显示全部插件列表",
        "en": "Show all plugin list"
    },
}
TRANSLATOR_DEFAULT_ENGINE = {
    "auto-translate": "gpt-5",
    "ForGal-tool": "gpt-5",
    "ForGal-markdown": "deepseek-chat",
    "ForNovel-tool": "gpt-5",
    "ForNovel": "deepseek-chat",
    "ForGal-json": "gpt-4.1",
    "sakura-v1.0": "sakura-7b-qwen2.5-v1.0",
    "galtransl-v3": "Sakura-GalTransl-7B-v3",
    "GenDic": "deepseek-chat",
}
NEED_OpenAITokenPool=["auto-translate", "ForGal-json", "ForGal-tool", "ForGal-markdown", "ForNovel", "ForNovel-tool", "GenDic"]
LANG_SUPPORTED = {
    "zh-cn": "Simplified_Chinese",
    "zh-tw": "Traditional_Chinese",
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
    "ru": "Russian",
    "fr": "French",
}
LANG_SUPPORTED_W = {
    "zh-cn": "简体中文",
    "zh-tw": "繁體中文",
    "en": "English",
    "ja": "日本語",
    "ko": "한국어",
    "ru": "русский",
    "fr": "Français",
}
DEBUG_LEVEL = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

new_version = []
update_thread = threading.Thread(target=check_for_tool_updates, args=(new_version,))
update_thread.start()

