import os
import sys
import argparse
from InquirerPy import inquirer
from InquirerPy.base.control import Choice
# 基本配置，避免循环导入
from GalTransl import (
    CONFIG_FILENAME,
    PROGRAM_SPLASH,
    TRANSLATOR_SUPPORTED
)
from GalTransl.i18n import get_text,GT_LANG

class BulletMenu:
    """
    A CLI menu to select a choice from a list of choices using InquirerPy.
    """

    def __init__(self, prompt: str = None, choices: dict[str, str] = None):
        self.prompt = prompt
        self.choices = list(choices.keys())
        self.descriptions = list(choices.values())

    def run(self, default_choice: int = 0) -> str:
        """Start the menu and return the selected choice"""

        rsp = inquirer.select(
            message=self.prompt,
            choices=[
                Choice(
                    name=f"{choice:20}{self.descriptions[i]}",
                    value=choice
                ) for i, choice in enumerate(self.choices)
            ],
            instruction="↑↓ + Enter",
            default=self.choices[default_choice] if self.choices else None
        ).execute()

        return rsp

# Get input prompt based on language
def get_input_prompt():
    return get_text("input_project_path", GT_LANG)


def parse_arguments():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description='GalTransl Runner')
    parser.add_argument('project_path', nargs='?', default=None, help='Project directory or config file path.')
    parser.add_argument('translator', nargs='?', default=None, choices=list(TRANSLATOR_SUPPORTED.keys()), help='Translator template name.')
    args = parser.parse_args()
    return args.project_path, args.translator


class ProjectManager:
    def __init__(self):
        self.user_input = ""
        self.project_dir = ""
        self.config_file_name = CONFIG_FILENAME
        self.translator = ""

    def validate_project_path(self, user_input: str) -> tuple[str | None, str | None, str | None]:
        if not user_input or not isinstance(user_input, str):
            print(get_text("input_path_empty", GT_LANG))
            return None, None, None
        try:
            user_input_abs = os.path.abspath(user_input)
            if user_input_abs.endswith(".yaml"):
                config_file_name = os.path.basename(user_input_abs)
                project_dir = os.path.dirname(user_input_abs)
            else:
                config_file_name = CONFIG_FILENAME
                project_dir = user_input_abs

            if not os.path.exists(project_dir):
                print(get_text("project_folder_not_exist", GT_LANG, project_dir))
                return None, None, None

            config_path = os.path.join(project_dir, config_file_name)
            if not os.path.exists(config_path):
                print(get_text("config_file_not_exist", GT_LANG, config_path))
                return None, None, None

            if not os.path.isfile(config_path):
                print(get_text("config_file_not_valid", GT_LANG, config_path))
                return None, None, None

            # 返回原始输入路径（可能相对），验证后的绝对项目目录，以及配置文件名
            return user_input, project_dir, config_file_name
        except Exception as e:
            print(get_text("error_validating_path", GT_LANG, str(e)))
            return None, None, None

    def get_user_input(self):
        while True:
            current_project_display = get_text("continue_with_project", GT_LANG, self.project_name()) if self.project_dir else ""
            default_input = os.path.join(self.project_dir, self.config_file_name) if self.project_dir and self.config_file_name else ""
            
            input_prompt = get_text("input_project_path", GT_LANG).replace("[default]", current_project_display)
            
            # 使用原始 user_input 或验证后的路径作为默认值
            user_input_raw = input(input_prompt).strip() or self.user_input or default_input

            if not user_input_raw:
                continue
            
            user_input_cleaned = user_input_raw.strip("&").strip(" ").strip('"').strip("'")
            validated_input, project_dir, config_file_name = self.validate_project_path(user_input_cleaned)
            
            if project_dir:
                self.user_input = validated_input # 存储用户最初的有效输入或验证后的路径
                self.project_dir = project_dir
                self.config_file_name = config_file_name
                return
            else:
                # 如果验证失败，清除 project_dir 以便下次循环提示
                self.project_dir = ""
                self.config_file_name = CONFIG_FILENAME # 重置为默认
                self.user_input = "" # 清除无效输入

    def print_program_info(self):
        from GalTransl import GALTRANSL_VERSION, AUTHOR, CONTRIBUTORS
        print(PROGRAM_SPLASH)
        print(f"Ver: {GALTRANSL_VERSION}")
        print(f"Author: {AUTHOR}")
        print(f"Contributors: {CONTRIBUTORS}\n")

    def choose_translator(self):
        
        default_choice_index = -1 # 默认不选中
        translator_keys = list(TRANSLATOR_SUPPORTED.keys())
        if self.translator and self.translator in translator_keys:
            try:
                default_choice_index = translator_keys.index(self.translator)
            except ValueError:
                pass # translator 不在支持列表里，保持 -1
        
        os.system("")  # 解决cmd的ANSI转义bug
        translators_dic={x:y[GT_LANG] for x,y in TRANSLATOR_SUPPORTED.items()}
        
        # 如果 default_choice_index 是 -1 (无效或未设置), BulletMenu 会默认选第一个
        # 如果希望没有预设翻译器时不默认选，需要修改 BulletMenu 或在此处处理
        chosen_translator = BulletMenu(
            get_text("select_translator",GT_LANG, self.project_name()), translators_dic
        ).run(default_choice_index if default_choice_index != -1 else 0) # 传递索引，如果无效则从0开始
        
        self.translator = chosen_translator

    def project_name(self):
        return os.path.basename(self.project_dir) if self.project_dir else ""

    def create_shortcut_win(self) -> None:
        if "nt" not in os.name:  # not windows
            return
        try:
            from GalTransl import GALTRANSL_VERSION
            TEMPLATE = '@echo off\nchcp 65001\nset "CURRENT_PATH=%CD%"\ncd /d "{0}"\nset "GT_LANG={4}"\n{1} "{2}" {3}\npause\ncd /d "%CURRENT_PATH%"'
            
            if getattr(sys, "frozen", False):  # PyInstaller
                run_com = os.path.basename(sys.executable)
                program_dir = os.path.dirname(sys.executable)
            else:
                run_com = "python.exe \"" + os.path.abspath(__file__) + "\"" # 使用绝对路径
                program_dir = os.path.dirname(os.path.abspath(__file__))

            # 确保 project_dir 是绝对路径
            abs_project_dir = os.path.abspath(self.project_dir)
            # 快捷方式中的配置路径应相对于 CURRENT_PATH
            # config_file_name 已经是纯文件名了
            conf_path_in_shortcut = os.path.join(abs_project_dir, self.config_file_name)
            
            shortcut_filename = f"run_GTv{GALTRANSL_VERSION}_{self.config_file_name.replace('.yaml','')}_{self.translator}.bat"
            shortcut_path = os.path.join(abs_project_dir, shortcut_filename)

            # 格式化模板
            # {0}: program_dir (GalTransl 脚本或 exe 所在目录)
            # {1}: run_com (执行命令，python.exe 或 exe)
            # {2}: conf_path_in_shortcut (配置文件的绝对路径，给 run_com 使用)
            # {3}: self.translator (翻译器名称)
            # {4}: GT_LANG (语言设置)
            text = TEMPLATE.format(program_dir, run_com, conf_path_in_shortcut, self.translator, GT_LANG)
            
            if not os.path.exists(shortcut_path):
                with open(shortcut_path, "w", encoding="utf-8") as f:
                    f.write(text)
                print(f"Shortcut created: {shortcut_path}") # 提示用户创建成功
        except Exception as e:
            print(get_text("error_creating_shortcut", GT_LANG, str(e)))

    def start_worker(self,show_banner=False):
        from GalTransl.__main__ import worker
        # 执行核心翻译任务
        try:
            worker(
                self.project_dir,
                self.config_file_name,
                self.translator,
                show_banner=show_banner
            )
            print(f"\n{get_text('translation_completed', GT_LANG)}")
        except Exception as e:
            print(f"\nError during translation: {e}") # 添加错误处理

        # 重置状态以便下次循环或退出
        self.translator = "" # 清空翻译器选择

        try:
            # 等待用户按键继续或允许退出
            input("Press Enter to start a new task or Ctrl+C to exit...") 
            os.system("cls" if os.name == 'nt' else 'clear') # 清屏
        except KeyboardInterrupt:
            return

    def run(self):
        # 优先处理命令行参数
        initial_project_path, initial_translator = parse_arguments()

        if initial_project_path:
            validated_input, project_dir, config_file_name = self.validate_project_path(initial_project_path)
            if project_dir:
                self.user_input = validated_input
                self.project_dir = project_dir
                self.config_file_name = config_file_name
                if initial_translator:
                    self.translator = initial_translator
                    self.start_worker(show_banner=True)
            else:
                 # 命令行提供的路径无效，将在循环中请求用户输入
                 pass 

        while True:
            self.print_program_info()

            try:
                self.get_user_input()
            except KeyboardInterrupt:
                return
            
            # 如果没有指定翻译器，则让用户选择
            if not self.translator:
                try:
                    self.choose_translator()
                except KeyboardInterrupt:
                    return
            
            # 创建快捷方式（如果适用）
            if self.translator not in ["show-plugs", "dump-name"]:
                self.create_shortcut_win()
            # 执行核心任务
            self.start_worker()


if __name__ == "__main__":
    # 可以在这里处理全局异常或设置
    manager = ProjectManager()
    manager.run()
