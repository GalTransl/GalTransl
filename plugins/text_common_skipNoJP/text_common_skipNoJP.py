from GalTransl import LOGGER
from GalTransl.CSentense import CSentense
from GalTransl.GTPlugin import GTextPlugin
from GalTransl.Utils import contains_japanese


class skip_noJP(GTextPlugin):
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
        if not contains_japanese(tran.post_jp):
            if not contains_hankaku(tran.post_jp):
                # 像这样赋值pre_zh后，这句话就不会被翻译。
                tran.pre_zh = tran.post_jp
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


def contains_hankaku(text: str) -> bool:

    hankaku_range = (0x4E00, 0x9FFF)
    for char in text:
        code_point = ord(char)
        if hankaku_range[0] <= code_point <= hankaku_range[1]:
            return True
    return False
