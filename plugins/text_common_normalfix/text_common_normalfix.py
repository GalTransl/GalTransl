from GalTransl.CSentense import CSentense
from GalTransl.GTPlugin import GTextPlugin


class text_common_normalfix(GTextPlugin):

    def before_src_processed(self, tran: CSentense) -> CSentense:
        while True:
            if tran.post_jp.startswith("　"):
                tran.post_jp = tran.post_jp[1:]
                tran.left_symbol += "　"
            elif tran.post_jp.endswith("　"):
                tran.post_jp = tran.post_jp[:-1]
                tran.right_symbol = "　" + tran.right_symbol
            elif tran.post_jp.startswith(" "):
                tran.post_jp = tran.post_jp[1:]
                tran.left_symbol += " "
            elif tran.post_jp.endswith(" "):
                tran.post_jp = tran.post_jp[:-1]
                tran.right_symbol = " " + tran.right_symbol
            elif tran.post_jp.startswith("\\n"):
                tran.post_jp = tran.post_jp[2:]
                tran.left_symbol = tran.left_symbol + "\\n"
            elif tran.post_jp.endswith("\\n"):
                tran.post_jp = tran.post_jp[:-2]
                tran.right_symbol = "\\n" + tran.right_symbol
            elif tran.post_jp.startswith("\n"):
                tran.post_jp = tran.post_jp[1:]
                tran.left_symbol = tran.left_symbol + "\n"
            elif tran.post_jp.endswith("\n"):
                tran.post_jp = tran.post_jp[:-1]
                tran.right_symbol = "\n" + tran.right_symbol
            else:
                break

        return tran

    def after_src_processed(self, tran: CSentense) -> CSentense:
        return tran

    def before_dst_processed(self, tran: CSentense) -> CSentense:
        tran = self._remove_first_symbol(tran)
        tran = self._fix_last_symbol(tran)
        # 修复输出中的\r\n换行符
        if "\r\n" in tran.post_jp:
            if "\r\n" not in tran.post_zh and "\n" in tran.post_zh:
                tran.post_zh = tran.post_zh.replace("\n", "\r\n")
            if tran.post_zh.startswith("\r\n") and not tran.post_jp.startswith("\r\n"):
                tran.post_zh = tran.post_zh[2:]

        return tran

    def after_dst_processed(self, tran: CSentense) -> CSentense:
        lb = ""
        if "\r\n" in tran.post_jp:
            lb = "\r\n"
        elif "\n" in tran.post_jp:
            lb = "\n"
        elif "\\n" in tran.post_jp:
            lb = "\\n"
        if lb == "":
            return tran
        while tran.post_zh.count(lb) > tran.pre_jp.count(lb):
            tran.post_zh = tran.post_zh.replace(lb, "", 1)

        return tran

    def _remove_first_symbol(self, tran, line_break_symbol="\\n"):
        """译后用，移除第一个字符是逗号，句号，换行符的情况"""
        if tran.post_zh[:1] in ["，", "。"]:
            tran.post_zh = tran.post_zh[1:]
        if tran.post_zh[:2] in [line_break_symbol]:
            tran.post_zh = tran.post_zh[2:]
        return tran

    def _fix_last_symbol(self, tran):
        """
        针对一些最后一个符号丢失的问题进行补回
        """
        if not tran.post_jp.endswith("\r\n") and tran.post_zh.endswith("\r\n"):
            tran.post_zh = tran.post_zh[:-2]
        if tran.post_jp[-1:] == "♪" and tran.post_zh[-1:] != "♪":
            tran.post_zh += "♪"
        if tran.post_jp[-2:] == "！？" and tran.post_zh[-1:] == "！":
            tran.post_zh = tran.post_zh + "？"
            
        if tran.post_jp[-1:] != "、" and tran.post_zh[-1:] == "，":
            tran.post_zh = tran.post_zh[:-1]
        if tran.post_zh[-1:]=="。" and tran.post_jp[-1:] not in ".。":
            tran.post_zh = tran.post_zh[:-1]
        return tran

    def _simple_fix_double_quotaion(self):
        """
        译后用，简单的记数法修复双引号左右不对称的问题，只适合句子里只有一对双引号的情况
        用在译后的字典替换后
        """
        if self.post_zh.count("”") == 2 and self.post_zh.count("“") == 0:
            self.post_zh = self.post_zh.replace("”", "“", 1)
        if self.post_zh.count("』") == 2 and self.post_zh.count("『") == 0:
            self.post_zh = self.post_zh.replace("』", "『", 1)
