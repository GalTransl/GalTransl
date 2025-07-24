from GalTransl import LOGGER
from GalTransl.CSentense import CSentense
from GalTransl.GTPlugin import GTextPlugin


class text_common_fullWidthFix(GTextPlugin):

    def gtp_init(self, plugin_conf: dict, project_conf: dict):
        self.pname = plugin_conf["Core"].get("Name", "")
        settings = plugin_conf["Settings"]
        self.是否替换标点 = settings.get("是否替换标点", False)
        self.替换时机 = settings.get("替换时机", "before_src_processed")
        self.反向替换 = settings.get("反向替换", False)
        self.自定义替换表 = settings.get("自定义替换表", {})

        LOGGER.info(f"[{self.pname}] 全角半角转换功能已启用。")
        LOGGER.info(f"[{self.pname}] 当前是否转换标点: {self.是否替换标点}")
        LOGGER.info(f"[{self.pname}] 当前替换时机: {self.替换时机}")
        LOGGER.info(f"[{self.pname}] 当前是否反向替换: {self.反向替换}")

        self.conversion_dict = self.create_conversion_dict()

    def create_conversion_dict(self):
        conversion_dict = {}
        # 数字和字母的转换
        conversion_dict.update({chr(0xFF10 + i): chr(0x30 + i) for i in range(10)})
        conversion_dict.update({chr(0xFF21 + i): chr(0x41 + i) for i in range(26)})
        conversion_dict.update({chr(0xFF41 + i): chr(0x61 + i) for i in range(26)})

        # 标点符号和常见符号的转换
        punctuations = {
            '。': '.', '，': ',', '、': '\\', '；': ';', '：': ':',
            '？': '?', '！': '!', '"': '"', '"': '"', ''': "'", ''': "'",
            '（': '(', '）': ')', '【': '[', '】': ']', '《': '<', '》': '>',
            '「': '[', '」': ']', '『': '{', '』': '}', '〔': '[', '〕': ']',
            '｛': '{', '｝': '}', '［': '[', '］': ']',
            '％': '%', '＋': '+', '－': '-', '＊': '*', '／': '/', '＝': '=',
            '＜': '<', '＞': '>', '＆': '&', '＄': '$', '＃': '#', '＠': '@',
            '＾': '^', '｜': '|', '～': '~', '｀': '`',
            '　': ' ',  # 全角空格到半角空格
            '…': '...', '—': '-', '－': '-', 'ー': '-',  # 各种横线
            '・': '·', '·': '·',  # 中点
            '′': "'", '″': '"',  # 角分符号
            '〜': '~', '～': '~',  # 波浪线
            '〇': '0',  # 汉字零
        }

        # 货币符号
        currency_symbols = {
            '￥': '¥', '￠': '¢', '￡': '£', '€': 'E',
        }

        if self.是否替换标点:
            conversion_dict.update(punctuations)
            conversion_dict.update(currency_symbols)

        # 添加自定义替换表
        conversion_dict.update(self.自定义替换表)

        # 如果是反向替换，交换键值
        if self.反向替换:
            conversion_dict = {v: k for k, v in conversion_dict.items()}

        return conversion_dict

    def convert_chars(self, s):
        return ''.join(self.conversion_dict.get(char, char) for char in s)

    def process_text(self, text: str) -> str:
        return self.convert_chars(text)

    def before_src_processed(self, tran: CSentense) -> CSentense:
        if self.替换时机 == "before_src_processed":
            tran.post_jp = self.process_text(tran.pre_jp)
        return tran

    def after_src_processed(self, tran: CSentense) -> CSentense:
        if self.替换时机 == "after_src_processed":
            tran.post_jp = self.process_text(tran.post_jp)
        return tran

    def before_dst_processed(self, tran: CSentense) -> CSentense:
        if self.替换时机 == "before_dst_processed":
            tran.post_zh = self.process_text(tran.pre_zh)
        return tran

    def after_dst_processed(self, tran: CSentense) -> CSentense:
        if self.替换时机 == "after_dst_processed":
            tran.post_zh = self.process_text(tran.post_zh)
        return tran

    def gtp_final(self):
        pass