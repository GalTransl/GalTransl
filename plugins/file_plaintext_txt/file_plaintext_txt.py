import json
import re
from GalTransl import LOGGER
from GalTransl.GTPlugin import GFilePlugin


class file_plugin(GFilePlugin):
    def gtp_init(self, plugin_conf: dict, project_conf: dict):
        """
        This method is called when the plugin is loaded.
        :param plugin_conf: The settings for the plugin.
        :param project_conf: The settings for the project.
        """
        settings = plugin_conf["Settings"]
        self.pname = plugin_conf["Core"].get("Name", "")
        self.保存双语txt文件 = settings.get("保存双语txt文件", False)
        self.上下双语1左右双语2 = settings.get("上下双语1左右双语2", 1)
        self.txt读取编码 = settings.get("txt读取编码", "utf-8")
        self.txt写入编码 = settings.get("txt写入编码", "utf-8")
        settings = plugin_conf["Settings"]
        LOGGER.debug(
            f"[{self.pname}] 当前配置：\n  txt读取编码:{self.txt读取编码}\n  txt写入编码:{self.txt写入编码}\n  保存双语txt文件:{self.保存双语txt文件}\n  上下双语1左右双语2:{self.上下双语1左右双语2}")


    def load_file(self, file_path: str) -> list:
        """
        This method is called to load a file.
        :param file_path: The path of the file to load.
        :return: A list of messages.
        """
        with open(file_path, "r", encoding=self.txt读取编码) as file:
            lines = file.readlines()

        result = [{"index": idx + 1, "message": line.strip(), "org_message": line.strip()}
                  for idx, line in enumerate(lines)]
        return result

    def save_file(self, file_path: str, transl_json: list):
        """
        This method is called to save a file.
        :param file_path: The path of the file to save.
        :param transl_json: A list of objects same as the return of load_file().
        :return: None.
        """
        result = ""
        for item in transl_json:
            if not self.保存双语txt文件:
                result += f"{item['message']}\n"
            else:
                if self.上下双语1左右双语2 == 2:
                    result += f"{item['message']} {item['org_message']}\n"
                elif self.上下双语1左右双语2 == 1:
                    result += f"{item['message']}\n{item['org_message']}\n"

        with open(file_path, "w", encoding=self.txt写入编码) as file:
            file.write(result.strip())

    def gtp_final(self):
        """
        This method is called after all translations are done.
        """
        pass
