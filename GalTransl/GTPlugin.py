from GalTransl import LOGGER
from GalTransl.yapsy.IPlugin import IPlugin
from GalTransl.CSentense import CSentense


class GTextPlugin(IPlugin):
    def gtp_init(self, plugin_conf: dict, project_conf: dict):
        """
        This method is called when the plugin is loaded.在插件加载时被调用。
        :param plugin_conf: The settings for the plugin.插件yaml中所有设置的dict。
        :param project_conf: The settings for the project.项目yaml中common下设置的dict。
        """
        pass

    def before_src_processed(self, tran: CSentense) -> CSentense:
        """
        This method is called before the source sentence is processed.
        在post_jp没有被去除对话框和字典替换之前的处理，如果这是第一个插件的话post_jp=原始日文。
        :param tran: The CSentense to be processed.
        :return: The modified CSentense."""
        return tran

    def after_src_processed(self, tran: CSentense) -> CSentense:
        """
        This method is called after the source sentence is processed.
        在post_jp已经被去除对话框和字典替换之后的处理。
        :param tran: The CSentense to be processed.
        :return: The modified CSentense.
        """
        return tran

    def before_dst_processed(self, tran: CSentense) -> CSentense:
        """
        This method is called before the destination sentence is processed.
        在post_zh没有被恢复对话框和字典替换之前的处理，如果这是第一个插件的话post_zh=原始译文。
        :param tran: The CSentense to be processed.
        :return: The modified CSentense.
        """
        return tran

    def after_dst_processed(self, tran: CSentense) -> CSentense:
        """
        This method is called after the destination sentence is processed.
        在post_zh已经被恢复对话框和字典替换之后的处理。
        :param tran: The CSentense to be processed.
        :return: The modified CSentense.
        """
        return tran

    def gtp_final(self):
        """
        This method is called after all translations are done.
        在所有文件翻译完成之后的动作，例如输出提示信息。
        """
        pass


class GProblemPlugin(IPlugin):
    """Translation checks run after destination text processing."""

    problem_types = ()

    def get_problem_types(self) -> list[dict]:
        """Declare check names and descriptions without requiring gtp_init."""
        types = []
        for item in self.problem_types:
            if isinstance(item, dict):
                types.append(dict(item))
            else:
                types.append({
                    "name": item if isinstance(item, str) else item.name,
                    "description": getattr(item, "description", ""),
                    "default_enabled": getattr(item, "default_enabled", False),
                })
        return types

    def gtp_init(self, plugin_conf: dict, project_conf: dict):
        pass

    def check(self, tran: CSentense, project_config, gpt_dict=None) -> list[str]:
        """Return problem messages without changing the sentence or its problem field."""
        raise NotImplementedError("This method must be implemented by the plugin.")

    def gtp_final(self):
        pass


class GFilePlugin(IPlugin):
    def gtp_init(self, plugin_conf: dict, project_conf: dict):
        """
        This method is called when the plugin is loaded.在插件加载时被调用。
        :param plugin_conf: The settings for the plugin.插件yaml中所有设置的dict。
        :param project_conf: The settings for the project.项目yaml中common下设置的dict。
        """
        pass

    def load_file(self, file_path: str) -> list:
        """
        This method is called to load a file.
        加载文件时被调用。
        :param file_path: The path of the file to load.加载文件路径。
        :return: A list of objects with message and name(optional).返回一个包含message和name(可空)的对象列表。
        """
        raise NotImplementedError("This method must be implemented by the plugin.")

    def reload_file(self, file_path: str) -> list:
        """重新提取原文；持有提取缓存的插件应覆盖此方法并刷新自己的缓存。"""
        return self.load_file(file_path)

    def save_file(self, file_path: str, transl_json: list):
        """
        This method is called to save a file.
        保存文件时被调用。
        :param file_path: The path of the file to save.保存文件路径
        :param transl_json: A list of objects same as the return of load_file().load_file提供的json在翻译message和name后的结果。
        :return: None.
        """
        raise NotImplementedError("This method must be implemented by the plugin.")

    def gtp_final(self):
        """
        This method is called after all translations are done.
        在所有文件翻译完成之后的动作，例如输出提示信息。
        """
        pass
