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

GALTRANSL_VERSION = "6.2.1"
AUTHOR = "xd2333"
CONTRIBUTORS = "ryank231231, PiDanShouRouZhouXD, Noriverwater, Isotr0py, adsf0427, pipixia244, gulaodeng, sakura-umi, lifegpc, natsumerinchan, szyzbg"

CONFIG_FILENAME = "config.yaml"
INPUT_FOLDERNAME = "gt_input"
OUTPUT_FOLDERNAME = "gt_output"
CACHE_FOLDERNAME = "transl_cache"
TRANSLATOR_SUPPORTED = {
    "ForGal": {
        "zh-cn": "（GPT4/Claude-3/Deepseek-V3）为翻译Gal重新定制的翻译模板，更快更省更好。默认deepseek-chat模型",
        "en": " （GPT4/Claude-3/Deepseek-V3）Customized template for Gal translation, faster and more efficient. Default model: deepseek-chat"
    },
    "gpt4": {
        "zh-cn": "（GPT4/Claude-3/Deepseek-V3）比较聪明的模型通用的翻译模板，默认gpt-4模型",
        "en": "(GPT4/Claude-3/Deepseek-V3) General translation template for smarter models. Default model: gpt-4"
    },
    "r1": {
        "zh-cn": "Deepseek-R1模型专用翻译模板，默认deepseek-reasoner模型",
        "en": "Specialized translation template for Deepseek-R1 model. Default model: deepseek-reasoner"
    },
    "sakura-v1.0": {
        "zh-cn": "（适用v1.0版sakura）为翻译轻小说/Gal开展大规模训练的本地模型，具有多个型号和大小",
        "en": "(For v1.0 prompt) Locally trained model for light novel/Gal translation, available in multiple sizes"
    },
    "galtransl-v3": {
        "zh-cn": "为翻译Gal基于Sakura进一步优化的本地模型",
        "en": "Further optimized local small model based on Sakura for Gal translation, can run on gaming GPUs with 6GB VRAM and MacBooks"
    },
    "GenDic": {
        "zh-cn": "自动化构建GPT字典，需要接大模型如Deepseek-V3",
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
    "ForGal": "deepseek-chat",
    "gpt4": "gpt-4",
    "r1": "deepseek-reasoner",
    "sakura-v1.0": "sakura-7b-qwen2.5-v1.0",
    "galtransl-v3": "Sakura-GalTransl-7B-v3",
    "GenDic": "deepseek-chat",
}
NEED_OpenAITokenPool=["gpt", "r1", "ForGal","GenDic"]
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

transl_counter = {"tran_count": 0, "error_count": 0}
