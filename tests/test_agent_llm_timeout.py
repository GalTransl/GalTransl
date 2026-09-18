"""LLM 请求的「静默」上限：挂死的连接不能让"停止"失效好几分钟。

背景（真实日志）：22:50:03 发出第 2 轮请求 → 用户点停止 → 22:51:53 才按
"用户停止"收尾。原因是回合线程阻塞在 socket read 上，SDK 默认静默上限是
600s，期间主循环没有任何机会去看停止信号；期间用户发的「继续」只能排队，
看起来就像"停止信号没清、第二轮被上一轮的停止带走了"。

这里把静默上限收到 LLM_SILENCE_TIMEOUT（流式期间每个 chunk 都会重置 read
计时，正常生成不受影响），并锁住客户端配置。
"""

import unittest

from GalTransl.Agent.runtime import LLM_SILENCE_TIMEOUT, AgentRunner, AgentState, _llm_timeout

try:  # 开发解释器可能没装后端依赖，那就跳过（后端运行环境里必然有）
    import openai  # noqa: F401

    _HAS_OPENAI = True
except ImportError:  # pragma: no cover - 取决于解释器
    _HAS_OPENAI = False

PROFILE = {
    "OpenAI-Compatible": {
        "tokens": [{"modelName": "fake-model", "token": "fake-token", "endpoint": "https://example.com/v1"}],
    }
}


@unittest.skipUnless(_HAS_OPENAI, "需要 openai 包（后端运行环境）")
class LlmSilenceTimeoutTests(unittest.TestCase):
    def test_llm_timeout_bounds_silence(self):
        """超时按 SDK 自己的 Timeout 类构造，读用静默上限、其余收紧。"""
        timeout = _llm_timeout()
        self.assertEqual(timeout.read, LLM_SILENCE_TIMEOUT)
        self.assertLess(timeout.read, 600.0)  # 明显短于 SDK 默认
        self.assertEqual(timeout.connect, 10.0)
        self.assertEqual(timeout.write, 30.0)

    def test_client_gets_bounded_silence_timeout(self):
        state = AgentState()
        state.session_id = ""  # 不落盘
        state.backend_profile_data = PROFILE
        runner = AgentRunner(state)

        runner._resolve_llm()

        self.assertEqual(runner._openai_client.timeout.read, LLM_SILENCE_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
