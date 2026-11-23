"""NO_PROXY 里带方括号的 IPv6（"[::1]"）不能让模型请求在发出前就失败。

回归背景：部分代理工具把 NO_PROXY 写成 "127.0.0.1,::1,[::1],localhost"。httpx 把
"[::1]" 拼成 "all://*[::1]" 再解析，构造任何客户端都抛 InvalidURL: Invalid port: ':1]'，
界面上表现为“检查模型可用性请求失败”，与 key 和接口地址无关。
"""
from __future__ import annotations

import httpx
import pytest

from GalTransl import _normalize_no_proxy_env


@pytest.fixture
def _clean_proxy_env(monkeypatch):
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_bracketed_ipv6_no_proxy_no_longer_breaks_httpx_clients(_clean_proxy_env):
    _clean_proxy_env.setenv("NO_PROXY", "127.0.0.1,::1,[::1],localhost")
    with pytest.raises(httpx.InvalidURL):
        httpx.Client()

    _normalize_no_proxy_env()

    httpx.Client().close()


def test_normalization_keeps_every_bypassed_host(_clean_proxy_env):
    _clean_proxy_env.setenv("NO_PROXY", "127.0.0.1, [::1] ,localhost,.example.com")

    _normalize_no_proxy_env()

    import os

    assert os.environ["NO_PROXY"] == "127.0.0.1,::1,localhost,.example.com"


def test_values_without_brackets_are_left_untouched(_clean_proxy_env):
    _clean_proxy_env.setenv("NO_PROXY", "127.0.0.1, localhost")

    _normalize_no_proxy_env()

    import os

    assert os.environ["NO_PROXY"] == "127.0.0.1, localhost"
