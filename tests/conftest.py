"""测试全局设置。"""

import os

# 测试不访问外网，本地 HTTP 测试也不该走代理。开发机/沙箱里的代理变量会干扰测试：
# 例如 NO_PROXY 里带方括号的 "[::1]" 会让 httpx2 构造客户端时直接报 Invalid port。
for _name in list(os.environ):
    if _name.lower() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        del os.environ[_name]
os.environ["NO_PROXY"] = "*"
