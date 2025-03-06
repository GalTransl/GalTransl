from GalTransl import DEBUG_LEVEL, LOGGER
from GalTransl.CSentense import CSentense
from GalTransl.GTPlugin import GTextPlugin
from re import compile


BGI_AUTO_CORRECT_PATTERN = compile(r'^(・|…)+$')


def process_string(source: str, remove_incorrect: bool = True, auto_correct: bool = True):
    i = 0
    le = len(source)
    # [ruby_rt_start:ruby_rt_end] 即 <rxxxx>
    ruby_rt_start = None
    ruby_rt_end = None
    s = source
    pre_is_angle_bracket = False
    while i < le:
        # 解析<r 后的内容
        if ruby_rt_start is not None and ruby_rt_end is None:
            if s[i] == '>':
                ruby_rt_end = i + 1
            elif remove_incorrect and s[i] == '<':
                # 移除 <r
                s = s[:ruby_rt_start] + s[ruby_rt_start + 2:]
                ruby_rt_start = None
                ruby_rt_end = None
                i -= 2
                le -= 2
        if pre_is_angle_bracket:
            # <r
            if s[i] == 'r':
                pre_is_angle_bracket = False
                ruby_rt_start = i - 1
            elif s[i] == '/':
                if i + 2 < le:
                    # 结尾标签</r>
                    if s[i + 1] == 'r' and s[i + 2] == '>':
                        if ruby_rt_start is not None and ruby_rt_end is not None:
                            ruby = s[ruby_rt_end:i-1]
                            rt = s[ruby_rt_start+2:ruby_rt_end-1]
                            rt_len = len(rt)
                            if auto_correct and rt_len > 0:
                                ruby_len = len(ruby)
                                o = '・' * ruby_len
                                if rt != o and BGI_AUTO_CORRECT_PATTERN.match(rt):
                                    s = s[:ruby_rt_start+2] + o + '>' + s[ruby_rt_end:]
                                    offset = ruby_len - rt_len
                                    le += offset
                                    i += offset
                            if remove_incorrect and rt_len == 0:
                                # 移除例如 <r>文本</r> 中的 <r> </r>
                                s = s[:ruby_rt_start] + s[ruby_rt_end:i-1] + s[i+3:]
                                ruby_rt_start = None
                                ruby_rt_end = None
                                pre_is_angle_bracket = False
                                le -= 7
                                i -= 4
                                continue
                            ruby_rt_start = None
                            ruby_rt_end = None
                            i += 3
                        elif remove_incorrect:
                            # 移除 </r>
                            s = s[:i-1] + s[i+3:]
                            i -= 1
                            le -= 4
                        else:
                            ruby_rt_start = None
                            ruby_rt_end = None
                            i += 3
                        pre_is_angle_bracket = False
                        continue
                    elif remove_incorrect:
                        if s[i + 1] == 'r':
                            s = s[:i-1] + s[i+2:]
                            le -= 3
                            i -= 3
                        else:
                            s = s[:i-1] + s[i+1:]
                            le -= 2
                            i -= 2
                elif remove_incorrect:
                    if i + 1 < le:
                        if s[i + 1] == 'r':
                            le -= 1
                            s = s[:-1]
                    s = s[:i-1] + s[i+1:]
                    le -= 2
                    i -= 2
        if s[i] == '<':
            if remove_incorrect and pre_is_angle_bracket:
                # 移除多余的 <
                s = s[:i] + s[i+1:]
                le -= 1
                continue
            pre_is_angle_bracket = True
            i += 1
            continue
        pre_is_angle_bracket = False
        i += 1
    if remove_incorrect:
        if ruby_rt_start is not None:
            if ruby_rt_end is None:
                s = s[:ruby_rt_start] + s[ruby_rt_start+2:]
            else:
                s = s[:ruby_rt_start] + s[ruby_rt_end:]
        if s[-1] == '<':
            s = s[:-1]
    return s


class TextBgiFixruby(GTextPlugin):
    def gtp_init(self, plugin_conf: dict, project_conf: dict):
        """
        This method is called when the plugin is loaded.在插件加载时被调用。
        :param plugin_conf: The settings for the plugin.插件yaml中所有设置的dict。
        :param project_conf: The settings for the project.项目yaml中common下设置的dict。
        """
        # 打印提示的方法，打印时请带上模块名，以便区分日志。
        self.pname = plugin_conf["Core"].get("Name", "text_bgi_fixruby")
        settings = plugin_conf["Settings"]
        LOGGER.info(f"[{self.pname}] BGI 修复 ruby 标签扩展已启用")
        self.remove_incorrect = settings.get('remove_incorrect', True)
        if self.remove_incorrect:
            LOGGER.info(f"[{self.pname}] 已启用移除不正确的 ruby 标签")
        self.auto_correct = settings.get('auto_correct', True)
        if self.auto_correct:
            LOGGER.info(f"[{self.pname}] 已启用自动修正着重符号（・）数量")
        self.process_log_level = DEBUG_LEVEL[settings.get('process_log_level', 'debug')]
        self.count = 0

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
        if self.remove_incorrect is False and self.auto_correct is False:
            return tran
        t = process_string(tran.post_zh, self.remove_incorrect, self.auto_correct)
        if t != tran.post_zh:
            LOGGER.log(self.process_log_level, f"[{self.pname}][{tran.index}]{tran.post_zh} -> {t}")
            tran.post_zh = t
            self.count += 1
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
        LOGGER.info(f'[{self.pname}]修改了 {self.count} 条对话')
