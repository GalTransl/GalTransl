import os
import sys
from GalTransl import LOGGER
from GalTransl.CSentense import CSentense
from GalTransl.GTPlugin import GTextPlugin
import budoux

class LineBreakFix(GTextPlugin):
    def gtp_init(self, plugin_conf: dict, project_conf: dict):
        """
        初始化插件
        :param plugin_conf: 插件配置字典
        :param project_conf: 项目配置字典
        """
        # 从配置中获取插件名称，默认为"行内换行修复"
        self.pname = plugin_conf["Core"].get("Name", "行内换行修复")
        settings = plugin_conf["Settings"]
        LOGGER.info(f"[{self.pname}] 插件启动")

        # 从配置中读取设置
        self.linebreak = settings.get("换行符", "[r]")  # 换行符，默认为"[r]"
        self.mode = settings.get("换行模式", "保持位置")  # 换行模式，默认为"保持位置"
        self.auto_segment_threshold = settings.get("分段字数阈值", 21)  # 分段字数阈值，默认为"21"
        if self.mode == "固定字数" and self.auto_segment_threshold <= 0:
            LOGGER.error(f"[{self.pname}] 分段字数阈值必须为正整数")
            raise ValueError("分段字数阈值无效")
        LOGGER.info(f"[{self.pname}] 分段字数阈值: {self.auto_segment_threshold}")
        self.force_fix = settings.get("强制修复", False)  # 是否强制修复，默认为False
        self.tokenizer_module = settings.get("分词器", "budoux")  # 分词器，默认为"budoux"

        # 输出设置信息
        LOGGER.info(f"[{self.pname}] 换行符: {self.linebreak}")
        LOGGER.info(f"[{self.pname}] 换行模式: {self.mode}")
        LOGGER.info(f"[{self.pname}] 强制修复: {self.force_fix}")

        # 初始化分词器
        if self.tokenizer_module == "budoux":
            self.tokenizer = budoux.load_default_simplified_chinese_parser()
        elif self.tokenizer_module == "jieba":
            try:
                import jieba

                self.tokenizer = jieba
            except ImportError:
                LOGGER.warning("缺少依赖包jieba, 请更新依赖")
                raise ImportError("缺少依赖包jieba")
        elif self.tokenizer_module == "pkuseg":
            try:
                import pkuseg
                self.tokenizer = pkuseg.pkuseg()
            except ImportError:
                LOGGER.warning("缺少依赖包pkuseg, 请更新依赖")
                raise ImportError("缺少依赖包pkuseg")
        elif self.tokenizer_module == "hanlp":
            try:
                import hanlp
                self.tokenizer = hanlp.load(hanlp.pretrained.mtl.UD_ONTONOTES_TOK_POS_LEM_FEA_NER_SRL_DEP_SDP_CON_XLMR_BASE)
            except ImportError:
                LOGGER.warning("缺少依赖包hanlp, 请更新依赖")
                raise ImportError("缺少依赖包hanlp")
        else:
            LOGGER.error(f"[{self.pname}] 未知的分词器: {self.tokenizer_module}")
            raise ValueError("未知的分词器")
        
        self.fix_count = 0  # 修复次数
        
    def tokenize(self, text):
        """
        统一分词接口
        :param text: 要分词的文本
        :return: 分词后的词语列表
        """
        if self.tokenizer_module == "budoux":
            return self.tokenizer.parse(text)
        elif self.tokenizer_module == "jieba":
            return list(self.tokenizer.cut(text))
        elif self.tokenizer_module == "pkuseg":
            return self.tokenizer.cut(text)
        elif self.tokenizer_module == "hanlp":
            return self.tokenizer(text)["tok"]
        else:
            LOGGER.error(f"[{self.pname}] 未知的分词器: {self.tokenizer_module}")

    def after_dst_processed(self, tran: CSentense) -> CSentense:
        """
        在目标文本处理之后的操作，主要的换行符修复逻辑
        :param tran: CSentense对象
        :return: 处理后的CSentense对象
        """
        src_breaks = tran.pre_jp.count(self.linebreak)
        dst_breaks = tran.post_zh.count(self.linebreak)
        LOGGER.debug(f"[{self.pname}] 源文本换行符数量: {src_breaks}")
        LOGGER.debug(f"[{self.pname}] 目标文本换行符数量: {dst_breaks}")

        if src_breaks == dst_breaks and not self.force_fix:
            return tran

        LOGGER.info(f"[{self.pname}] {'强制修复' if self.force_fix else '发现源文本和目标文本的换行符数量不一致'}，正在进行换行符修复，模式: {self.mode}")
        self.fix_count += 1

        # 根据不同的换行模式调用相应的处理方法
        if self.mode == "平均":
            tran.post_zh = self.average_mode(tran.post_zh, src_breaks)
        elif self.mode == "切最长":
            tran.post_zh = self.intersperse_mode(tran.post_zh, src_breaks)
        elif self.mode == "保持位置":
            tran.post_zh = self.keep_position_mode(tran.post_zh, tran.pre_jp, src_breaks)
        elif self.mode == "前置":
            tran.post_zh = self.prepend_mode(tran.post_zh, src_breaks)
        elif self.mode == "后置":
            tran.post_zh = self.append_mode(tran.post_zh, src_breaks)
        elif self.mode == "固定字数":
            tran.post_zh = self.auto_segment_mode(tran.post_zh)
        else:
            LOGGER.warning(f"[{self.pname}] 未知的换行模式: {self.mode}")

        return tran

    def prepend_mode(self, text: str, target_breaks: int) -> str:
        """
        前置模式：将所有换行符放在文本的最前面
        :param text: 原文本
        :param target_breaks: 目标换行符数量
        :return: 处理后的文本
        """
        text_without_breaks = text.replace(self.linebreak, '')
        return self.linebreak * target_breaks + text_without_breaks

    def append_mode(self, text: str, target_breaks: int) -> str:
        """
        后置模式：将所有换行符放在文本的最后面
        :param text: 原文本
        :param target_breaks: 目标换行符数量
        :return: 处理后的文本
        """
        text_without_breaks = text.replace(self.linebreak, '')
        return text_without_breaks + self.linebreak * target_breaks

    def average_mode(self, text: str, target_breaks: int) -> str:
        """
        平均模式：忽略原有换行符，将文本等分，在等分点插入换行符
        :param text: 原文本
        :param target_breaks: 目标换行符数量
        :return: 处理后的文本
        """
        text_without_breaks = text.replace(self.linebreak, '')
        total_length = len(text_without_breaks)
        chars_per_slice = total_length // (target_breaks + 1)
        
        phrases = self.tokenize(text_without_breaks)
        result = []
        current_length = 0
        breaks_added = 0
        
        for phrase in phrases:
            result.append(phrase)
            current_length += len(phrase)
            if current_length >= chars_per_slice and breaks_added < target_breaks:
                result.append(self.linebreak)
                current_length = 0
                breaks_added += 1
        
        # 如果还没有添加足够的换行符，在最后添加
        while breaks_added < target_breaks:
            result.append(self.linebreak)
            breaks_added += 1
        
        return ''.join(result)

    def intersperse_mode(self, text: str, target_breaks: int) -> str:
        """
        切最长模式：保留原有换行符，反复找最长片段从中间切分，直到达到目标换行符数量
        :param text: 原文本
        :param target_breaks: 目标换行符数量
        :return: 处理后的文本
        """
        slices = text.split(self.linebreak)
        while len(slices) - 1 < target_breaks:
            longest_slice_index = max(range(len(slices)), key=lambda i: len(slices[i]))
            longest_slice = slices[longest_slice_index]
            phrases = self.tokenize(longest_slice)
            
            if len(phrases) < 2:
                # 如果无法再分割，就在最后添加空字符串
                slices.append('')
            else:
                mid = len(phrases) // 2
                left_part = ''.join(phrases[:mid])
                right_part = ''.join(phrases[mid:])
                slices[longest_slice_index:longest_slice_index+1] = [left_part, right_part]
        
        return self.linebreak.join(slices[:target_breaks+1])

    def keep_position_mode(self, text: str, src_text: str, target_breaks: int) -> str:
        """
        保持位置模式：根据原文的换行符相对位置，重新计算目标换行符的位置，保证相对位置不变
        :param text: 目标文本
        :param src_text: 源文本
        :param target_breaks: 目标换行符数量
        :return: 处理后的文本
        """
        src_length = len(src_text.replace(self.linebreak, ''))
        dst_length = len(text.replace(self.linebreak, ''))
        
        src_breaks = src_text.split(self.linebreak)
        break_positions = []
        current_length = 0
        
        LOGGER.debug(f"[{self.pname}] 源文本: {src_text}")
        LOGGER.debug(f"[{self.pname}] 目标文本: {text}")
        LOGGER.debug(f"[{self.pname}] 源文本分片: {src_breaks}")
        
        for i, slice in enumerate(src_breaks):
            if i < len(src_breaks) - 1:  # 不计算最后一个换行符后的位置
                current_length += len(slice)
                relative_position = current_length / src_length
                dst_position = int(relative_position * dst_length)
                break_positions.append(dst_position)
                LOGGER.debug(f"[{self.pname}] 计算换行位置: 当前长度 {current_length}, 相对位置 {relative_position:.2f}, 目标位置 {dst_position}")
        
        phrases = self.tokenize(text.replace(self.linebreak, ''))
        LOGGER.debug(f"[{self.pname}] 目标文本分词结果: {phrases}")
        LOGGER.debug(f"[{self.pname}] 计算出的换行符位置: {break_positions}")
        
        result = []
        current_length = 0
        break_index = 0
        
        for phrase in phrases:
            result.append(phrase)
            current_length += len(phrase)
            if break_index < len(break_positions) and current_length >= break_positions[break_index]:
                result.append(self.linebreak)
                break_index += 1
                LOGGER.debug(f"[{self.pname}] 插入换行符，当前长度: {current_length}")
        
        # 如果还没有添加足够的换行符，在最后添加
        while break_index < target_breaks:
            result.append(self.linebreak)
            break_index += 1
        
        final_result = ''.join(result)
        LOGGER.debug(f"[{self.pname}] 最终结果: {final_result}")
        return final_result

    def auto_segment_mode(self, text: str) -> str:
        """
        固定字数模式：当字符数超过阈值时，按分词结果插入换行符
        :param text: 原文本（可能含现有换行符）
        :return: 处理后的文本
        """
        # 移除所有现有换行符，获取纯文本
        clean_text = text.replace(self.linebreak, '')
        if len(clean_text) <= self.auto_segment_threshold:
            return text  # 无需分段
    
        # 分词并累计字符数
        phrases = self.tokenize(clean_text)
        result = []
        current_length = 0
    
        for phrase in phrases:
            result.append(phrase)
            current_length += len(phrase)
            # 超过阈值且不在最后一个词语时插入换行符
            if current_length >= self.auto_segment_threshold and len(result) < len(phrases):
                result.append(self.linebreak)
                current_length = 0  # 重置计数器
    
        # 合并结果并保留原换行符外的内容
        new_text = ''.join(result)
        # 移除末尾多余的换行符（如果原文本没有）
        if not text.endswith(self.linebreak) and new_text.endswith(self.linebreak):
            new_text = new_text[:-len(self.linebreak)]
        return new_text

    def gtp_final(self):
        """
        插件结束时的操作
        """
        LOGGER.info(f"[{self.pname}] 共修复{self.fix_count}处换行符")