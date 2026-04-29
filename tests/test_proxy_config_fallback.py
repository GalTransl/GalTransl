import unittest

from GalTransl.ConfigHelper import CProxyPool, has_usable_proxy_config


class _FakeConfig:
    def __init__(self, proxies):
        self._proxies = proxies

    def getProxyConfigSection(self):
        return self._proxies


class ProxyConfigFallbackTests(unittest.TestCase):
    def test_enabled_proxy_without_address_is_not_usable(self) -> None:
        proxy_cfg = {
            "enableProxy": True,
            "proxies": [{"address": "   "}],
        }
        self.assertFalse(has_usable_proxy_config(proxy_cfg))

    def test_proxy_pool_skips_empty_address_entries(self) -> None:
        config = _FakeConfig(
            [
                {"address": ""},
                {"address": "   "},
                {"address": "http://127.0.0.1:7890"},
            ]
        )
        pool = CProxyPool(config)
        self.assertEqual(len(pool.proxies), 1)
        self.assertEqual(pool.proxies[0][1].addr, "http://127.0.0.1:7890")


if __name__ == "__main__":
    unittest.main()
