from typing import List, Dict, Any, Optional, Union, Tuple
from dataclasses import dataclass
from collections import defaultdict
import json
from GalTransl import LOGGER
from GalTransl.Loader import load_transList


@dataclass
class SplitChunkMetadata:
    """
    用于存储分割后的文本块元数据的数据类。

    属性:
    start_index: 块在原始文本中的起始索引
    end_index: 块在原始文本中的结束索引
    chunk_non_cross_size: 不包括交叉部分的块大小
    chunk_size: 包括交叉部分的实际块大小
    cross_num: 交叉句子数量
    content: 块的实际内容
    """

    __file_finished_chunk = (
        {}
    )  # file_path: List[SplitChunkMetadata]，按文件存储已完成的块

    def __init__(
        self,
        chunk_index: int,
        start_index: int,
        end_index: int,
        chunk_non_cross_size: int,
        chunk_size: int,
        cross_num: int,
        json_list: List[Dict],
        file_path: str,
    ):
        self.chunk_index = chunk_index  # 块索引
        self.start_index = start_index  # 块起始索引
        self.end_index = end_index  # 块结束索引
        self.chunk_non_cross_size = chunk_non_cross_size  # 块非交叉大小
        self.chunk_size = chunk_size  # 块实际大小
        self.cross_num = cross_num  # 交叉句子数量
        self.json_list = json_list  # 块内容
        self.trans_list, _ = load_transList(json_list)  # 翻译列表
        self.file_path = file_path  # 文件路径
        self.total_chunks = 0  # 总块数
        if self.file_path not in SplitChunkMetadata.__file_finished_chunk:
            SplitChunkMetadata.__file_finished_chunk[self.file_path] = []

    def update_total_chunks(self, total_chunks: int):
        self.total_chunks = total_chunks

    def update_file_finished_chunk(self):
        SplitChunkMetadata.__file_finished_chunk[self.file_path].append(self)

    def is_file_finished(self):
        return (
            len(SplitChunkMetadata.__file_finished_chunk[self.file_path])
            == self.total_chunks
        )

    def get_file_finished_chunks(self):
        return SplitChunkMetadata.__file_finished_chunk[self.file_path]

    @staticmethod
    def clear_file_finished_chunk():
        SplitChunkMetadata.__file_finished_chunk = {}


class InputSplitter:
    """
    输入分割器的基类，定义了分割方法的接口。
    """

    @staticmethod
    def split(json_list: List[Dict], file_path: str = "") -> List[SplitChunkMetadata]:
        """
        分割输入内容的方法，由子类实现。

        参数:
        json_list: 要分割的内容，可以是字符串或列表
        cross_num: 交叉句子的数量
        total_chunks: 总块数
        返回:
        分割后的SplitChunkMetadata列表
        """
        pass


class DictionaryCountSplitter(InputSplitter):
    """
    基于字典计数的分割器，将输入内容按指定的字典数量进行分割。
    """

    def __init__(self, dict_count: int, cross_num: int = 0):
        """
        初始化分割器。

        参数:
        dict_count: 每个分割块中包含的字典数量
        """
        self.dict_count = dict_count
        self.cross_num = cross_num

    def split(
        self, json_list: List[Dict], file_path: str = ""
    ) -> List[SplitChunkMetadata]:
        """
        实现分割方法，将输入内容按字典数量分割。

        参数:
        content: 要分割的内容
        cross_num: 交叉句子的数量

        返回:
        分割后的SplitChunkMetadata列表
        """

        total_items = len(json_list)
        result = []

        for start in range(0, total_items, self.dict_count):
            end = min(start + self.dict_count, total_items)
            chunk_start = max(0, start - self.cross_num)
            chunk_end = min(total_items, end + self.cross_num)
            chunk = json_list[chunk_start:chunk_end]

            result.append(
                SplitChunkMetadata(
                    chunk_index=len(result),
                    start_index=start,
                    end_index=end,
                    chunk_non_cross_size=end - start,
                    chunk_size=len(chunk),
                    cross_num=self.cross_num,
                    json_list=chunk,
                    file_path=file_path,
                )
            )

        # 更新总块数
        for chunk in result:
            chunk.update_total_chunks(len(result))

        return result


class EqualPartsSplitter(InputSplitter):
    """
    将输入内容平均分割成指定数量的部分。
    """

    def __init__(self, parts: int, cross_num: int = 0):
        """
        初始化分割器。

        参数:
        parts: 要分割成的部分数量
        """
        self.parts = parts
        self.cross_num = cross_num

    def split(
        self, json_list: List[Dict], file_path: str = ""
    ) -> List[SplitChunkMetadata]:
        """
        实现分割方法，将输入内容平均分割。

        参数:
        content: 要分割的内容
        cross_num: 交叉句子的数量

        返回:
        分割后的SplitChunkMetadata列表
        """

        total_items = len(json_list)
        items_per_part = total_items // self.parts
        remainder = total_items % self.parts

        result = []
        start = 0
        for i in range(self.parts):
            end = start + items_per_part + (1 if i < remainder else 0)
            chunk_start = max(0, start - self.cross_num)
            chunk_end = min(total_items, end + self.cross_num)
            chunk = json_list[chunk_start:chunk_end]
            result.append(
                SplitChunkMetadata(
                    chunk_index=len(result),
                    start_index=start,
                    end_index=end,
                    chunk_non_cross_size=end - start,
                    chunk_size=len(chunk),
                    cross_num=self.cross_num,
                    json_list=chunk,
                    file_path=file_path,
                )
            )
            start = end

        # 更新总块数
        for chunk in result:
            chunk.update_total_chunks(len(result))

        return result


class OutputCombiner:
    """
    输出合并器的基类，定义了合并方法的接口。
    """

    @staticmethod
    def combine(
        results: List[Tuple[List, List, SplitChunkMetadata]]
    ) -> Tuple[List, List]:
        """
        合并输出结果的方法，由子类实现。

        参数:
        results: 包含翻译结果、JSON列表和元数据的元组列表

        返回:
        合并后的翻译列表和JSON列表的元组
        """
        pass


class DictionaryCombiner(OutputCombiner):
    """
    基于字典的输出合并器，用于合并分割后的翻译结果。
    """

    @staticmethod
    def combine(chunks: List[SplitChunkMetadata]) -> Tuple[List, List]:
        """
        实现合并方法，合并分割后的翻译结果。

        参数:
        results: 包含翻译结果、JSON列表和元数据的元组列表

        返回:
        合并后的翻译列表和JSON列表的元组
        """
        if len(chunks) == 1:
            # 如果只有一个结果，直接返回
            return chunks[0].trans_list, chunks[0].json_list

        all_trans_list = []
        all_json_list = []
        sorted_chunks = sorted(chunks, key=lambda x: x.start_index)

        for i, chunk in enumerate(sorted_chunks):
            trans_list = chunk.trans_list
            json_list = chunk.json_list
            if i == 0:
                # 对于第一个块，我们取全部内容
                all_trans_list.extend(trans_list[: chunk.chunk_non_cross_size])
                all_json_list.extend(json_list[: chunk.chunk_non_cross_size])
            else:
                # 对于后续块，我们只取非交叉部分
                start = chunk.cross_num
                end = start + chunk.chunk_non_cross_size
                all_trans_list.extend(trans_list[start:end])
                all_json_list.extend(json_list[start:end])

        return all_trans_list, all_json_list
