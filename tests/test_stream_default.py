"""流式开关的默认值：配置里没写 stream 时要按「开启」处理。

工作台的「文件进度」小灯、实时速度、预计剩余都是从流式输出里攒出来的（见
BaseTranslate._report_stream_progress）。非流式请求一次性返回，这些实时信息就没有了，
界面只能显示按缓存算的静态进度。

默认值以前是 False，而真正生效的那份后端配置是前端送来的配置档（整段替换
backendSpecific，见 Service.py 的 run_job），它默认不写 stream——于是新建的配置全是
非流式，工作台永远停在「处理中」。
"""

import unittest
from types import SimpleNamespace

from GalTransl.COpenAI import COpenAITokenPool


def _pool(section: dict) -> COpenAITokenPool:
    """按后端的方式建令牌池：只读 backendSpecific 里的这一节，不发任何请求。"""
    config = SimpleNamespace(getBackendConfigSection=lambda _name: section)
    return COpenAITokenPool(config, "ForGal")


class StreamDefaultTests(unittest.TestCase):
    def test_missing_stream_key_defaults_to_on(self):
        pool = _pool(
            {"tokens": [{"token": "sk-real", "endpoint": "https://api.example.com"}]}
        )
        self.assertTrue(pool.stream)
        self.assertTrue(pool.tokens[0][1].stream)

    def test_section_stream_false_still_applies_to_tokens(self):
        pool = _pool(
            {
                "stream": False,
                "tokens": [{"token": "sk-real", "endpoint": "https://api.example.com"}],
            }
        )
        self.assertFalse(pool.tokens[0][1].stream)

    def test_token_level_setting_overrides_section(self):
        pool = _pool(
            {
                "stream": True,
                "tokens": [
                    {"token": "sk-a", "endpoint": "https://a.example.com", "stream": False},
                    {"token": "sk-b", "endpoint": "https://b.example.com"},
                ],
            }
        )
        self.assertFalse(pool.tokens[0][1].stream)
        self.assertTrue(pool.tokens[1][1].stream)

    def test_example_tokens_are_skipped(self):
        pool = _pool(
            {"tokens": [{"token": "sk-example-key1", "endpoint": "https://api.deepseek.com"}]}
        )
        self.assertEqual(pool.tokens, [])


if __name__ == "__main__":
    unittest.main()
