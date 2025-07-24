import os
# Language support for GalTransl
# This file contains all the text strings used in the UI and backend

# Available languages
AVAILABLE_LANGUAGES = ["zh-cn", "en"]

# Default language
DEFAULT_LANGUAGE = "zh-cn"

GT_LANG=os.environ.get("GT_LANG", DEFAULT_LANGUAGE)

# UI text strings
UI_TEXT = {
    # Common
    "language_name": {
        "zh-cn": "简体中文",
        "en": "English"
    },
    
    # Input prompts
    "input_project_path": {
        "zh-cn": "请输入/拖入项目文件夹，或项目文件夹内的yaml配置文件[default]：",
        "en": "Please input/drag project folder, or yaml config file in project folder[default]: "
    },
    "input_path_empty": {
        "zh-cn": "输入路径不能为空且必须是字符串类型\n",
        "en": "Input path cannot be empty and must be a string\n"
    },
    "project_folder_not_exist": {
        "zh-cn": "项目文件夹 {0} 不存在，请检查后重新输入\n",
        "en": "Project folder {0} does not exist, please check and input again\n"
    },
    "config_file_not_exist": {
        "zh-cn": "配置文件 {0} 不存在，请检查后重新输入\n",
        "en": "Config file {0} does not exist, please check and input again\n"
    },
    "config_file_not_valid": {
        "zh-cn": "配置文件路径 {0} 不是一个有效的文件，请检查后重新输入\n",
        "en": "Config file path {0} is not a valid file, please check and input again\n"
    },
    "continue_with_project": {
        "zh-cn": "(留空继续『{0}』项目)",
        "en": "(leave empty to continue with \"{0}\" project)"
    },
    "select_translator": {
        "zh-cn": "请为『{0}』项目选择翻译模板：",
        "en": "Please select translation template for \"{0}\" project:"
    },
    
    # Error messages
    "error_validating_path": {
        "zh-cn": "验证项目路径时发生错误: {0}\n",
        "en": "Error validating project path: {0}\n"
    },
    "error_creating_shortcut": {
        "zh-cn": "创建快捷方式时发生错误: {0}\n",
        "en": "Error creating shortcut: {0}\n"
    },
    "error_project_path_empty": {
        "zh-cn": "项目路径不能为空且必须是字符串类型",
        "en": "Project path cannot be empty and must be a string"
    },
    "error_config_file_empty": {
        "zh-cn": "配置文件名不能为空且必须是字符串类型",
        "en": "Config file name cannot be empty and must be a string"
    },
    "error_translator_empty": {
        "zh-cn": "翻译器名称不能为空且必须是字符串类型",
        "en": "Translator name cannot be empty and must be a string"
    },
    "error_loading_config": {
        "zh-cn": "加载配置文件失败: {0}",
        "en": "Failed to load config file: {0}"
    },
    "error_creating_event_loop": {
        "zh-cn": "创建新的事件循环...",
        "en": "Creating new event loop..."
    },
    "error_unexpected": {
        "zh-cn": "程序遇到未预期的错误: {0}",
        "en": "Program encountered an unexpected error: {0}"
    },
    "error_closing_event_loop": {
        "zh-cn": "关闭事件循环时发生错误: {0}",
        "en": "Error closing event loop: {0}"
    },
    
    # Status messages
    "translation_completed": {
        "zh-cn": "任务完成，准备重新开始...",
        "en": "task completed, preparing to restart..."
    },
    "waiting_for_requests": {
        "zh-cn": "正在等待现有请求返回...",
        "en": "Waiting for existing requests to return..."
    },
    "goodbye": {
        "zh-cn": "Goodbye.",
        "en": "Goodbye."
    },
    "program_error": {
        "zh-cn": "程序遇到问题，即将退出（诊断信息：{0}）",
        "en": "Program encountered a problem and will exit (diagnostic info: {0})"
    },
    "invalid_source_language": {
        "zh-cn": "错误的源语言代码：{0}",
        "en": "Invalid source language code: {0}"
    },
    "invalid_target_language": {
        "zh-cn": "错误的目标语言代码：{0}",
        "en": "Invalid target language code: {0}"
    },
    "request_error_quota": {
        "zh-cn": "[请求错误]余额不足：{0}",
        "en": "[Request Error] Insufficient quota: {0}"
    },
    "request_error_switch_token": {
        "zh-cn": "[请求错误]切换到token {0}",
        "en": "[Request Error] Switching to token {0}"
    },
    "request_error_too_many": {
        "zh-cn": "[请求错误]请求受限，{0}秒后继续尝试",
        "en": "[Request Error] Request limited, trying again in {0} seconds"
    },
    "request_error_reset": {
        "zh-cn": "[请求错误]报错重置会话",
        "en": "[Request Error] Error resetting session"
    },
    "request_error_retry": {
        "zh-cn": "[请求错误]报错:{0}, 2秒后重试",
        "en": "[Request Error] Error: {0}, retrying in 2 seconds"
    },
    "parse_error": {
        "zh-cn": "[解析错误]解析结果出错：{0}",
        "en": "[Parse Error] Error parsing result: {0}"
    },
    "parse_error_skip": {
        "zh-cn": "[解析错误]解析出错但跳过本轮翻译",
        "en": "[Parse Error] Error parsing but skipping this translation round"
    },
    "repeated_error": {
        "zh-cn": "单句反复出错，已中止。错误为：{0}",
        "en": "Repeated errors on a single sentence, aborted. Error: {0}"
    },
    "translation_input": {
        "zh-cn": "->翻译输入：{0}\n{1}\n",
        "en": "->Translation input: {0}\n{1}\n"
    },
    "proofread_input": {
        "zh-cn": "->校对输入：{0}\n{1}\n",
        "en": "->Proofreading input: {0}\n{1}\n"
    },
    "output": {
        "zh-cn": "->输出：",
        "en": "->Output:"
    },
    "output_with_content": {
        "zh-cn": "->输出：\n{0}",
        "en": "->Output:\n{0}"
    },
    "non_json_output": {
        "zh-cn": "-> 非json：\n{0}\n",
        "en": "-> Non-JSON:\n{0}\n"
    },
    "cache_read_error": {
        "zh-cn": "读取缓存{0}时出现错误，请检查错误信息",
        "en": "Error reading cache {0}, please check error message"
    },
    "retry_failed": {
        "zh-cn": "重试失败的: {0}",
        "en": "Retrying failed: {0}"
    },
    "loading_plugin": {
        "zh-cn": "加载插件\"{0}\"...",
        "en": "Loading plugin \"{0}\"..."
    },
    "plugin_load_failed": {
        "zh-cn": "插件\"{0}\"加载失败: {1}",
        "en": "Plugin \"{0}\" loading failed: {1}"
    },
    "file_processing_error": {
        "zh-cn": "处理文件 {0} 时发生错误: {1}",
        "en": "Error processing file {0}: {1}"
    },
    "task_execution_failed": {
        "zh-cn": "任务执行失败: {0}",
        "en": "Task execution failed: {0}"
    },
    "plugin_execution_failed": {
        "zh-cn": "插件 {0} 执行失败: {1}",
        "en": "Plugin {0} execution failed: {1}"
    },
    "file_translation_completed": {
        "zh-cn": "文件 {0}{1} 翻译完成，用时 {2:.3f}s",
        "en": "File {0}{1} translation completed, time used {2:.3f}s"
    },
    "file_chunks_completed": {
        "zh-cn": "文件 {0} 的所有chunk都翻译完成",
        "en": "All chunks of file {0} have been translated"
    },
    "file_load_failed": {
        "zh-cn": "文件 {0} 无法加载",
        "en": "File {0} cannot be loaded"
    },
    "cache_incomplete": {
        "zh-cn": "{0} 缓存不完整，无法重构",
        "en": "{0} cache incomplete, cannot rebuild"
    }
}

# Function to get text in the current language
def get_text(key, lang=GT_LANG, *args):
    """
    Get text in the specified language
    
    Args:
        key: The key for the text
        lang: The language code (default: DEFAULT_LANGUAGE)
        *args: Arguments to format into the text
        
    Returns:
        The text in the specified language, or the key if not found
    """
    if lang not in AVAILABLE_LANGUAGES:
        lang = DEFAULT_LANGUAGE  # Default to default language if language not supported
    
    if key not in UI_TEXT:
        return key
    
    if lang not in UI_TEXT[key]:
        # Fallback to default language if the key exists but not in the requested language
        lang = DEFAULT_LANGUAGE
    
    text = UI_TEXT[key][lang]
    
    if args:
        try:
            return text.format(*args)
        except:
            return text
    
    return text