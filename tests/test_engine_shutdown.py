"""流程收尾会统一对 gptapi 调 shutdown()：任何引擎都必须能被安全关掉。

重建引擎（rebuilda/rebuildr）整个覆写了 __init__（它不翻译、不持有模型客户端），于是基类
那套客户端初始化从没跑过，基类 shutdown 里的 _shutdown_done / _retired_clients 都不存在。
原来收尾就记一条 `'CRebuildTranslate' object has no attribute '_shutdown_done'` 的假警告——
用户看着像是"关闭客户端失败了"，其实什么都没坏。这里锁两件事：

1. 重建引擎的 shutdown 是空操作，重复调用也不出错（收尾不止一处会调）；
2. 基类 shutdown 不假定自己的 __init__ 跑过：没标记也能关、也只关一次客户端。
"""

import unittest

from GalTransl.Backend.BaseTranslate import BaseTranslate
from GalTransl.Backend.RebuildTranslate import CRebuildTranslate


class _FakeClient:
    """只记调用次数：验证 close 只发生一次（重复 shutdown 不能再关一遍）。"""

    def __init__(self):
        self.closed = 0

    async def close(self):
        self.closed += 1


class _InitSkippingEngine(BaseTranslate):
    """复刻重建引擎的写法：整个覆写 __init__，不跑基类的客户端初始化。"""

    def __init__(self):
        pass


class RebuildEngineShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_is_noop_and_repeatable(self):
        engine = CRebuildTranslate(None, "rebuilda")

        await engine.shutdown()
        await engine.shutdown()  # 收尾可能被多条路径各调一次

    async def test_pipeline_close_call_finds_a_working_shutdown(self):
        # 与 LLMTranslate 收尾同一套调用姿势：getattr 取到就 await，异常会被记成警告
        for eng_type in ("rebuilda", "rebuildr"):
            engine = CRebuildTranslate(None, eng_type)
            shutdown_callable = getattr(engine, "shutdown", None)
            self.assertTrue(callable(shutdown_callable), eng_type)
            await shutdown_callable()


class BaseShutdownRobustnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_without_base_init_does_not_raise(self):
        # 没跑过基类构造 → 没有 _shutdown_done / _retired_clients，也不该抛异常
        await _InitSkippingEngine().shutdown()

    async def test_shutdown_closes_clients_exactly_once(self):
        engine = _InitSkippingEngine()
        client = _FakeClient()
        engine.client_list = [(client, None)]

        await engine.shutdown()
        await engine.shutdown()

        self.assertEqual(client.closed, 1)


if __name__ == "__main__":
    unittest.main()
