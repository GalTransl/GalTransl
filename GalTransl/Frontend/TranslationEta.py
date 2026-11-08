"""翻译任务的剩余时间估算：按实际调度方式模拟剩余分片的完成时间。

为什么不用「剩余句数 ÷ 最近速度」：一个分片只能由一个 worker 逐批串行翻完（每批要带上前几批
的译文当上下文），队列取空后先做完的 worker 直接退出，收尾阶段只剩少数几个 worker 在跑。
满并发时测得的速度拿去除剩余句数，收尾一开始会严重偏短，之后并发越来越少、ETA 越跑越长。

这里改成：测「单个 worker 的速度」（句/秒），再把正在翻的分片剩余量和排队分片的待翻量
按真实调度（依次交给最早空闲的 worker）模拟一遍，最后一个 worker 完成的时刻就是 ETA。

开了收尾拆分（splitTailChunks）时，收尾阶段空闲 worker 会把别人剩下的活拆走，剩余工作量
近似均摊到所有 worker 上；只是拆不到比 min_split_lines 更碎。
"""

from __future__ import annotations

import heapq
import time
from dataclasses import dataclass
from typing import Callable, Iterable

# 单 worker 速度的指数平滑系数：每完成一批采样一次，并发高时样本多，取小一些让读数稳定
_RATE_ALPHA = 0.1


@dataclass
class _RunningChunk:
    pending: int
    done: int = 0
    last_progress_at: float = 0.0


class TranslationEtaEstimator:
    """记录分片的排队/开始/进度/完成，按调度模拟估算剩余秒数。

    分片用缓存文件路径作 key（每个分片一份缓存，天然唯一）。线程不安全：只在翻译所在的
    事件循环里调用。
    """

    def __init__(self, workers: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.workers = max(1, int(workers))
        self._clock = clock
        # dict 保持插入顺序 = 调度顺序
        self._queued: dict[str, int] = {}
        self._running: dict[str, _RunningChunk] = {}
        self._rate: float | None = None
        # 收尾拆分最小能拆出的句数；0 表示没开收尾拆分
        self.min_split_lines = 0

    @property
    def rate(self) -> float | None:
        """单个 worker 的平滑速度（句/秒）；还没有样本时为 None。"""
        return self._rate

    def queue(self, items: Iterable[tuple[str, int]]) -> None:
        """按调度顺序登记排队的分片及其待翻句数。"""
        for key, pending in items:
            self._queued[key] = max(0, int(pending))

    def start(self, key: str, pending: int) -> None:
        """分片开始翻译；pending 用 worker 这次缓存检查的实际未命中数。"""
        self._queued.pop(key, None)
        if pending > 0:
            self._running[key] = _RunningChunk(pending=int(pending), last_progress_at=self._clock())

    def record(self, key: str, lines: int) -> None:
        """分片完成一批（lines 句，含失败句）。用两批之间的间隔采样单 worker 速度。"""
        chunk = self._running.get(key)
        if chunk is None or lines <= 0:
            return
        now = self._clock()
        elapsed = now - chunk.last_progress_at
        chunk.last_progress_at = now
        chunk.done += lines
        if elapsed <= 0:
            return
        sample = lines / elapsed
        self._rate = sample if self._rate is None else self._rate + _RATE_ALPHA * (sample - self._rate)

    def split(self, key: str, new_key: str, lines: int) -> None:
        """从正在翻的 key 拆出 lines 句，作为新的一段 new_key 开始翻。"""
        chunk = self._running.get(key)
        if chunk is not None:
            chunk.pending = max(chunk.done, chunk.pending - int(lines))
        self.start(new_key, lines)

    def finish(self, key: str) -> None:
        self._running.pop(key, None)
        self._queued.pop(key, None)

    def estimate(self) -> float | None:
        """剩余秒数；还没测到速度时返回 None（界面显示「估算中」或退回旧算法）。"""
        rate = self._rate
        if not rate or rate <= 0:
            return None
        now = self._clock()
        # 每个 worker 什么时候空出来：正在翻的按剩余量算（扣掉当前这批已经等了的时间），其余现在就空
        free_at = [
            max(0.0, max(0, chunk.pending - chunk.done) / rate - (now - chunk.last_progress_at))
            for chunk in self._running.values()
        ]
        free_at.extend([0.0] * max(0, self.workers - len(free_at)))
        finish = max(free_at, default=0.0)
        heapq.heapify(free_at)
        # 排队的分片依次交给最早空闲的 worker（与 worker_loop 从队列取活的方式一致）
        for pending in self._queued.values():
            done_at = heapq.heappop(free_at) + pending / rate
            finish = max(finish, done_at)
            heapq.heappush(free_at, done_at)
        if self.min_split_lines > 0 and free_at:
            # 收尾拆分：剩余活均摊给所有 worker，但最后一段至少还要翻 min_split_lines 句
            balanced = sum(free_at) / len(free_at)
            finish = max(balanced, min(finish, self.min_split_lines / rate))
        return finish
