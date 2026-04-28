from GalTransl.CSentense import CSentense
from GalTransl.GTPlugin import GTextPlugin


class text_common_normalfix(GTextPlugin):

    def before_src_processed(self, tran: CSentense) -> CSentense:
        while True:
            if tran.post_src.startswith("　"):
                tran.post_src = tran.post_src[1:]
                tran.left_symbol += "　"
            elif tran.post_src.endswith("　"):
                tran.post_src = tran.post_src[:-1]
                tran.right_symbol = "　" + tran.right_symbol
            elif tran.post_src.startswith(" "):
                tran.post_src = tran.post_src[1:]
                tran.left_symbol += " "
            elif tran.post_src.endswith(" "):
                tran.post_src = tran.post_src[:-1]
                tran.right_symbol = " " + tran.right_symbol
            elif tran.post_src.startswith("\\n"):
                tran.post_src = tran.post_src[2:]
                tran.left_symbol = tran.left_symbol + "\\n"
            elif tran.post_src.endswith("\\n"):
                tran.post_src = tran.post_src[:-2]
                tran.right_symbol = "\\n" + tran.right_symbol
            elif tran.post_src.startswith("\n"):
                tran.post_src = tran.post_src[1:]
                tran.left_symbol = tran.left_symbol + "\n"
            elif tran.post_src.endswith("\n"):
                tran.post_src = tran.post_src[:-1]
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
        if "\r\n" in tran.post_src:
            if "\r\n" not in tran.post_dst and "\n" in tran.post_dst:
                tran.post_dst = tran.post_dst.replace("\n", "\r\n")
            if tran.post_dst.startswith("\r\n") and not tran.post_src.startswith("\r\n"):
                tran.post_dst = tran.post_dst[2:]

        return tran

    def after_dst_processed(self, tran: CSentense) -> CSentense:
        lb = ""
        if "\r\n" in tran.post_src:
            lb = "\r\n"
        elif "\n" in tran.post_src:
            lb = "\n"
        elif "\\n" in tran.post_src:
            lb = "\\n"
        if lb == "":
            return tran
        while tran.post_dst.count(lb) > tran.pre_src.count(lb):
            tran.post_dst = tran.post_dst.replace(lb, "", 1)

        return tran

    def _remove_first_symbol(self, tran, line_break_symbol="\\n"):
        """译后用，移除第一个字符是逗号，句号，换行符的情况"""
        if tran.post_dst[:1] in ["，", "。"]:
            tran.post_dst = tran.post_dst[1:]
        if tran.post_dst[:2] in [line_break_symbol]:
            tran.post_dst = tran.post_dst[2:]
        return tran

    def _fix_last_symbol(self, tran):
        """
        针对一些最后一个符号丢失的问题进行补回
        """
        if not tran.post_src.endswith("\r\n") and tran.post_dst.endswith("\r\n"):
            tran.post_dst = tran.post_dst[:-2]
        if tran.post_src[-1:] == "♪" and tran.post_dst[-1:] != "♪":
            tran.post_dst += "♪"
        if tran.post_src[-2:] == "！？" and tran.post_dst[-1:] == "！":
            tran.post_dst = tran.post_dst + "？"
            
        if tran.post_src[-1:] != "、" and tran.post_dst[-1:] == "，":
            tran.post_dst = tran.post_dst[:-1]
        if tran.post_dst[-1:]=="。" and tran.post_src[-1:] not in ".。":
            tran.post_dst = tran.post_dst[:-1]
        return tran

    def _simple_fix_double_quotaion(self):
        """
        译后用，简单的记数法修复双引号左右不对称的问题，只适合句子里只有一对双引号的情况
        用在译后的字典替换后
        """
        if self.post_dst.count("”") == 2 and self.post_dst.count("“") == 0:
            self.post_dst = self.post_dst.replace("”", "“", 1)
        if self.post_dst.count("』") == 2 and self.post_dst.count("『") == 0:
            self.post_dst = self.post_dst.replace("』", "『", 1)
