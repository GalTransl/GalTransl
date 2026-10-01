"""调用本机 server.py HTTP API 的辅助函数。"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from typing import Any

from GalTransl.Agent.core import _log
from GalTransl.Agent.models import AgentToolError


def _normalize_endpoint(endpoint: str) -> str:
    """复用 COpenAI 的 endpoint 规范化逻辑：补 /v1、去掉 /chat/completions 尾巴。"""
    domain = endpoint.strip()
    if domain.endswith("/chat/completions"):
        domain = domain.replace("/chat/completions", "")
        base_path = ""
    else:
        base_path = "/v1" if not re.search(r"/v\d+", domain) else ""
    return domain.strip("/") + base_path


def _encode_project_id(project_dir: str) -> str:
    """与前端 encodeProjectDir 一致：UTF-8 base64url，去填充。"""
    from base64 import urlsafe_b64encode

    raw = project_dir.encode("utf-8")
    return urlsafe_b64encode(raw).decode("ascii").rstrip("=").replace("+", "-").replace("/", "_")


def _http_json(method: str, url: str, body: dict[str, Any] | None = None) -> Any:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    _log(f"  HTTP {method} {url}")
    http_started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
            ms = int((time.time() - http_started) * 1000)
            _log(f"  HTTP {method} {url} -> {resp.status} ({len(raw)} bytes, {ms}ms)")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        ms = int((time.time() - http_started) * 1000)
        try:
            err_body = json.loads(exc.read().decode("utf-8"))
            msg = err_body.get("error") or json.dumps(err_body, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            msg = f"HTTP {exc.code} {exc.reason}"
        _log(f"  HTTP {method} {url} -> {exc.code} ({ms}ms) 错误: {msg}")
        raise AgentToolError(msg) from exc
    except urllib.error.URLError as exc:
        ms = int((time.time() - http_started) * 1000)
        _log(f"  HTTP {method} {url} -> 连接失败 ({ms}ms): {exc}")
        raise AgentToolError(f"无法连接后端：{exc}") from exc
