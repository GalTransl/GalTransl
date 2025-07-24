import os
import sys
import json
import re
from GalTransl import LOGGER
from GalTransl.GTPlugin import GFilePlugin


webvtt_path = os.path.abspath(os.path.dirname(__file__))
sys.path.append(webvtt_path)
import webvtt


class file_plugin(GFilePlugin):
    def gtp_init(self, plugin_conf: dict, project_conf: dict):
        """
        This method is called when the plugin is loaded.在插件加载时被调用。
        :param plugin_conf: The settings for the plugin.插件yaml中所有设置的dict。
        :param project_conf: The settings for the project.项目yaml中common下设置的dict。
        """
        settings = plugin_conf["Settings"]
        self.保存双语字幕 = settings.get("保存双语字幕", False)
        self.上下双语1左右双语2 = settings.get("上下双语1左右双语2", 2)
        self.srt_pattern = re.compile(
            r"(\d+)\n([\d:,]+ --> [\d:,]+)\n(.+?)(?=\n\d+|\Z)", re.DOTALL
        )
        self.lrc_pattern = re.compile(r"\[(\d+:\d+\.\d+)\](.*)", re.UNICODE)

    def load_file(self, file_path: str) -> list:
        """
        This method is called to load a file.
        加载文件时被调用。
        :param file_path: The path of the file to load.
        :return: A list of CSentense objects.
        """
        if not any(file_path.endswith(ext) for ext in [".srt", ".lrc", ".vtt"]):
            raise TypeError("File type not supported.")

        with open(file_path, "r", encoding="utf-8") as file:
            text = file.read()

        if file_path.endswith(".srt"):
            try:
                matches = self.srt_pattern.findall(text)
                result = [
                    {
                        "index": int(m[0]),
                        "timestamp": m[1],
                        "message": m[2].strip(),
                        "org_message": m[2].strip(),
                    }
                    for m in matches
                ]
            except Exception as e:
                raise e
        elif file_path.endswith(".lrc"):
            try:
                matches = self.lrc_pattern.findall(text)
                LOGGER.debug(f"matches: {matches}")
                result = [
                    {
                        "timestamp": m[0],
                        "message": m[1].strip(),
                        "org_message": m[1].strip(),
                    }
                    for m in matches
                ]
            except Exception as e:
                raise e
        elif file_path.endswith(".vtt"):
            vtt = webvtt.read(file_path)
            result = []
            for caption in vtt:
                result.append(
                    {
                        # "index": caption.index,
                        "timestamp": f"{caption.start} --> {caption.end}",
                        "message": caption.text,
                        "org_message": caption.text,
                    }
                )

        return result

    def save_file(self, file_path: str, transl_json: list):
        """
        This method is called to save a file.
        保存文件时被调用。
        :param file_path: The path of the file to save.保存文件路径
        :param transl_json: A list of objects same as the return of load_file().load_file提供的json在翻译message和name后的结果。
        :return: None.
        """

        result = ""
        if file_path.endswith(".srt"):
            for item in transl_json:
                if not self.保存双语字幕:
                    result += (
                        f"{item['index']}\n{item['timestamp']}\n{item['message']}\n\n"
                    )
                else:
                    if self.上下双语1左右双语2 == 2:
                        result += f"{item['index']}\n{item['timestamp']}\n{item['message']} {item['org_message']}\n\n"
                    elif self.上下双语1左右双语2 == 1:
                        result += f"{item['index']}\n{item['timestamp']}\n{item['message']}\n{item['org_message']}\n\n"
        elif file_path.endswith(".lrc"):
            for item in transl_json:
                if not item['message'] and not item['org_message']:
                    # 只有时间戳的行，只保存一次
                    result += f"[{item['timestamp']}]\n"
                elif not self.保存双语字幕:
                    result += f"[{item['timestamp']}]{item['message']}\n"
                elif self.保存双语字幕:
                    if self.上下双语1左右双语2 == 1:
                        result += f"[{item['timestamp']}]{item['message']}\n[{item['timestamp']}]{item['org_message']}\n"
                    elif self.上下双语1左右双语2 == 2:
                        result += f"[{item['timestamp']}]{item['message']} {item['org_message']}\n"
        elif file_path.endswith(".vtt"):
            vtt = webvtt.WebVTT()
            for item in transl_json:
                caption = webvtt.Caption(
                    item["timestamp"].split(" --> ")[0],
                    item["timestamp"].split(" --> ")[1],
                    item["message"],
                )
                if self.保存双语字幕:
                    if self.上下双语1左右双语2 == 2:
                        caption.text = f"{item['message']} {item['org_message']}"
                    elif self.上下双语1左右双语2 == 1:
                        caption.text = f"{item['message']}\n{item['org_message']}"
                vtt.captions.append(caption)
            vtt.save(file_path)

        if file_path.endswith(".srt") or file_path.endswith(".lrc"):
            with open(file_path, "w", encoding="utf-8") as file:
                file.write(result.strip())

    def gtp_final(self):
        """
        This method is called after all translations are done.
        在所有文件翻译完成之后的动作，例如输出提示信息。
        """
        pass
