import orjson, re
from GalTransl import LOGGER
from GalTransl.GTPlugin import GFilePlugin


class file_plugin(GFilePlugin):
    def gtp_init(self, plugin_conf: dict, project_conf: dict):
        """
        This method is called when the plugin is loaded.在插件加载时被调用。
        :param plugin_conf: The settings for the plugin.插件yaml中所有设置的dict。
        :param project_conf: The settings for the project.项目yaml中common下设置的dict。
        """
        self.pname = plugin_conf["Core"].get("Name", "")
        settings = plugin_conf["Settings"]
        self.output_with_src = settings.get("output_with_src", False)
        pass

    def load_file(self, file_path: str) -> list:
        """
        This method is called to load a file.
        加载文件时被调用。
        :param file_path: The path of the file to load.文件路径。
        :return: A list of objects with message and name(optional).返回一个包含message和name(可空)的对象列表。
        """
        if not file_path.endswith(".json"):
            # 检查不支持的文件类型并抛出TypeError
            raise TypeError("请检查filePlugin的配置并选择合适的文件插件.")
        with open(file_path, "r", encoding="utf-8") as f:
            json_list = orjson.loads(f.read())
        if self.output_with_src:
            for i in json_list:
                i["src_msg"]=i["message"]
        return json_list

    def save_file(self, file_path: str, transl_json: list):
        """
        This method is called to save a file.
        保存文件时被调用。
        :param file_path: The path of the file to save.保存文件路径
        :param transl_json: A list of objects same as the return of load_file().load_file提供的json在翻译message和name后的结果。
        :return: None.
        """
        with open(file_path, "wb") as f:
            f.write(orjson.dumps(transl_json, option=orjson.OPT_INDENT_2))

    def gtp_final(self):
        """
        This method is called after all translations are done.
        在所有文件翻译完成之后的动作，例如输出提示信息。
        """
        pass
