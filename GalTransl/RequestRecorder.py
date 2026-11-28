"""录制模式：把翻译请求的完整细节按模板逐条追加到项目的 CSV 里。

一次请求的完整参数和原始响应由 BaseTranslate._ask_chatbot 在请求成功时放进 _LAST_EXCHANGE，
模板解析完这次响应后再调用 record_translation_request 落盘：解析结果（成功、部分解析、解析失败）
只有模板知道，「仅成功」模式要靠它过滤。没拿到响应的请求（接口报错）没有可记的内容，不写入。
"""

from __future__ import annotations

import csv
import json
import os
import re
import threading
from contextvars import ContextVar
from datetime import datetime
from typing import Any

from openai._types import NotGiven

from GalTransl import LOGGER

RECORD_MODES = ("off", "success", "all")
RECORD_DIR_NAME = "transl_record"
RECORD_COLUMNS = (
    "time", "template", "model", "status", "error", "file", "index_range", "sentence_count",
    "input", "gpt_dict", "context", "request", "response", "reasoning", "tool_calls",
)

# 每个批次在自己的 asyncio 任务里跑，ContextVar 让并发批次各自拿到自己最后一次请求。
_LAST_EXCHANGE: ContextVar[dict[str, Any] | None] = ContextVar("galtransl_last_exchange", default=None)
# 同一进程里多个任务/线程可能同时写同一个模板的文件，整行写入要串行
_WRITE_LOCK = threading.Lock()
_warned_paths: set[str] = set()


def record_mode(pj_config: Any) -> str:
    mode = getattr(pj_config, "request_record_mode", "off")
    return mode if mode in RECORD_MODES else "off"


def is_recording(pj_config: Any) -> bool:
    return record_mode(pj_config) != "off"


def clear_exchange() -> None:
    _LAST_EXCHANGE.set(None)


def capture_exchange(request_kwargs: dict[str, Any], content: Any, reasoning: str, tool_calls: list) -> None:
    # timeout 是客户端参数，不在请求体里；NOT_GIVEN 的参数 SDK 不会发出
    request = {
        key: value for key, value in request_kwargs.items()
        if key != "timeout" and not isinstance(value, NotGiven)
    }
    _LAST_EXCHANGE.set({
        "request": request,
        "response": content if isinstance(content, str) else "",
        "reasoning": reasoning or "",
        "tool_calls": tool_calls or [],
    })


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _template_file_name(template: str) -> str:
    return (re.sub(r"[^\w.\-]+", "_", template).strip("._") or "unknown") + ".csv"


def record_path(project_dir: str, template: str) -> str:
    return os.path.join(project_dir, RECORD_DIR_NAME, _template_file_name(template))


def record_translation_request(
    pj_config: Any,
    *,
    template: str,
    status: str,
    error: str,
    filename: str,
    index_range: str,
    sentence_count: int,
    input_text: str,
    gpt_dict: str,
    context: str,
) -> None:
    """把当前任务最后一次成功拿到响应的请求写成一行。

    status 是 success / partial / parse_error；「仅成功」模式只写 success。
    没开录制或没有可写的请求时什么都不做。录制只是旁路记录：写盘出错只警告一次，不能影响翻译本身。
    """
    mode = record_mode(pj_config)
    if mode == "off" or (mode == "success" and status != "success"):
        return
    exchange = _LAST_EXCHANGE.get()
    if not exchange:
        return
    # 同一次请求只写一次：外层兜底路径不会把已经记过的请求再记一遍
    _LAST_EXCHANGE.set(None)
    project_dir = getattr(pj_config, "runtime_project_dir", None) or pj_config.getProjectDir()
    path = record_path(project_dir, template)
    request = exchange["request"]
    row = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "template": template,
        "model": str(request.get("model", "")),
        "status": status,
        "error": error,
        "file": filename,
        "index_range": index_range,
        "sentence_count": sentence_count,
        "input": input_text,
        "gpt_dict": gpt_dict,
        "context": "" if context.strip() == "None" else context,
        "request": _json_text(request),
        "response": exchange["response"],
        "reasoning": exchange["reasoning"],
        "tool_calls": _json_text(exchange["tool_calls"]) if exchange["tool_calls"] else "",
    }
    try:
        with _WRITE_LOCK:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            new_file = not os.path.isfile(path) or os.path.getsize(path) == 0
            # utf-8-sig：Excel 直接打开不乱码；追加到非空文件时 Python 不会再写 BOM
            with open(path, "a", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=RECORD_COLUMNS)
                if new_file:
                    writer.writeheader()
                writer.writerow(row)
    except Exception as exc:
        if path not in _warned_paths:
            _warned_paths.add(path)
            LOGGER.warning(f"[录制模式] 写入 {path} 失败，本次任务之后的同类错误不再提示：{exc}")
