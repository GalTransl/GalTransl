"""翻译任务的句子范围：按文件解析顺序取从 0 开始的半开区间。"""

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class SentenceRange:
    start: int
    end: int

    def __str__(self) -> str:
        return f"{self.start}-{self.end}"


def parse_sentence_range(value: object, files: list[str], translator: str) -> SentenceRange | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\s*[0-9]+\s*-\s*[0-9]+\s*", value):
        raise ValueError("sentence_range 必须为从 0 开始的半开区间，如 0-150（前 150 句）")
    start, end = map(int, value.split("-"))
    if end <= start:
        raise ValueError("sentence_range 的结束位置必须大于开始位置")
    if not files:
        raise ValueError("sentence_range 必须同时指定非空 files/input_files，范围分别用于每个文件")
    if translator in ("GenDic", "dump-name", "rebuilda", "rebuildr"):
        raise ValueError("sentence_range 只用于模型翻译，不支持 GenDic/dump-name/rebuilda/rebuildr")
    return SentenceRange(start, end)
