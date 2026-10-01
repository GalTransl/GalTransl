"""运行时速度：引擎自报的速度（与进度计数同单位）要盖住默认的「成功事件/分」。

默认速度是最近一分钟的成功事件数。普通翻译一个成功事件就是一句话，和进度（句）同口径；
GenDic 的进度是分片/批次（x/130 项），成功事件却是抽出来的一个个术语（一段几十个），
不覆盖的话工作台的「预计剩余」会把 130 项的活算成还剩两分钟。所以 GenDic 自己报速度
（见 GenDic._progress_speed_lpm），这里锁住注册表侧的优先关系与清零。
"""

import unittest

from GalTransl.server import RuntimeRegistry


class ProgressSpeedOverrideTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = RuntimeRegistry()
        self.project_dir = r"E:\tmp\gendic_project"

    def _record_successes(self, count: int) -> None:
        for index in range(count):
            self.registry.append_success(
                self.project_dir,
                filename="GenDic 术语提取",
                index=index,
                speaker=None,
                source_preview="src",
                translation_preview="dst",
                trans_by="model",
            )

    def _speed(self) -> float:
        return self.registry.get_runtime_snapshot(self.project_dir)["translation_speed_lpm"]

    def test_default_speed_is_successes_per_minute(self) -> None:
        self._record_successes(60)
        self.assertEqual(self._speed(), 60.0)

    def test_engine_reported_speed_wins(self) -> None:
        self._record_successes(60)  # 一分钟里 60 个术语
        self.registry.update_status(self.project_dir, progress_speed_lpm=5.6)
        self.assertEqual(self._speed(), 5.6)  # 但真正的进度速度是 5.6 项/分

    def test_zero_clears_the_estimate_instead_of_falling_back(self) -> None:
        self._record_successes(60)
        self.registry.update_status(self.project_dir, progress_speed_lpm=5.6)
        self.registry.update_status(self.project_dir, progress_speed_lpm=0)
        # 0 表示「不按引擎的速度算了」，不能再回落成术语/分
        self.assertEqual(self._speed(), 0)

    def test_reset_project_drops_the_override(self) -> None:
        self.registry.update_status(self.project_dir, progress_speed_lpm=5.6)
        self.registry.reset_project(self.project_dir)
        self.assertEqual(self._speed(), 0)


if __name__ == "__main__":
    unittest.main()
