"""收尾拆分（splitTailChunks）：队列取空后，把正在翻的分片还没发出去的后半段拆给空闲 worker。

一个分片只能由一个 worker 逐批串行翻（每批带上前几批的译文当上下文）。收尾时最后几个大分片
各占一个 worker，其余 worker 早早没活可干。开启后，空闲 worker 从「还没发出的句子最多」的
那段里拆走后一半，自己接着串行翻；拆出来的那段还能再被拆。代价：拆开处的第一批拿不到前文译文
当上下文。

后端的批次循环用 bind_tail_split() 认领当前这段的句柄：每批发出前 reserve()，循环终点读
end（被拆走后会变小）。没接入的后端不调用它，句柄始终没绑定，也就不会被拆。
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

_CURRENT_TAIL_SPLIT: ContextVar["TailSplitHandle | None"] = ContextVar("galtransl_tail_split", default=None)


class TailSplitHandle:
    """一段待翻句子的拆分句柄：[0, reserved) 已发出，[reserved, end) 还能拆走。"""

    def __init__(self, min_lines: int) -> None:
        # 每次拆分两边至少各留这么多句（取每批句数，拆得更碎只会多丢上下文）
        self.min_lines = max(1, int(min_lines))
        self.items: list | None = None
        self.end = 0
        self.reserved = 0
        # 从这段拆出去的部分，翻完时各自 set_result；本段收尾前要等它们
        self.children: list[asyncio.Future] = []

    def bind(self, items: list) -> "TailSplitHandle":
        self.items = items
        self.end = len(items)
        self.reserved = 0
        return self

    def reserve(self, start: int, count: int) -> list:
        """后端发出一批前调用：取 [start, start+count)，不越过 end，并记下已发出的位置。"""
        assert self.items is not None
        stop = min(start + max(1, int(count)), self.end)
        self.reserved = max(self.reserved, stop)
        return self.items[start:stop]

    @property
    def spare(self) -> int:
        """还没发出、能拆走的句数。"""
        return 0 if self.items is None else max(0, self.end - self.reserved)

    def can_split(self) -> bool:
        return self.spare >= 2 * self.min_lines

    def split(self) -> list:
        """拆走还没发出的后一半；不够拆返回 []。"""
        if not self.can_split():
            return []
        assert self.items is not None
        cut = self.reserved + (self.spare + 1) // 2
        stolen = self.items[cut:self.end]
        self.end = cut
        return stolen

    async def wait_children(self) -> None:
        if self.children:
            await asyncio.gather(*self.children)


def set_current_tail_split(handle: TailSplitHandle | None):
    """让当前任务里接下来的后端批次循环认领 handle；返回的 token 交给 reset_current_tail_split。"""
    return _CURRENT_TAIL_SPLIT.set(handle)


def reset_current_tail_split(token) -> None:
    _CURRENT_TAIL_SPLIT.reset(token)


def bind_tail_split(items: list) -> TailSplitHandle | None:
    """后端批次循环开始时调用：当前这段开了收尾拆分就绑定并返回句柄，否则返回 None。"""
    handle = _CURRENT_TAIL_SPLIT.get()
    if handle is None or handle.items is not None:
        return None
    return handle.bind(items)


@dataclass(eq=False)
class TailSplitTask:
    """正在翻的一段：整个分片，或从别的段拆出来的一段。"""

    split_chunk: Any
    cache_file_path: str
    # 分片自己的后端文件名（file_name 或 file_name_{分片号}）；拆出来的段在后面再加 _{段号}
    base_filename: str
    translation_list: list
    eta_key: str
    handle: TailSplitHandle


@dataclass
class TailSplitRegistry:
    """所有正在翻、可能被拆的段。只在翻译所在的事件循环里用。"""

    active: list[TailSplitTask] = field(default_factory=list)
    pieces: int = 0

    def add(self, task: TailSplitTask) -> None:
        self.active.append(task)

    def remove(self, task: TailSplitTask) -> None:
        if task in self.active:
            self.active.remove(task)

    def pick(self) -> TailSplitTask | None:
        """还没发出的句子最多、且够拆的那段。"""
        candidates = [task for task in self.active if task.handle.can_split()]
        return max(candidates, key=lambda task: task.handle.spare, default=None)
