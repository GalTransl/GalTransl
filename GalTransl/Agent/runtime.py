"""GalTransl Agent runtime.

一个 Agent 推理循环：读取选定的后端配置（OpenAI-Compatible）与项目，
用 OpenAI 官方 function-calling 接口驱动"先写字典后启动翻译"的标准流程。
工具通过本机 HTTP 调回现有 server.py 的 API，走和 UI 一样的代码路径。
不直接接触文件系统，所有写入经现有 API 的路径校验。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import traceback
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

DEFAULT_BACKEND_HOST = "127.0.0.1"
DEFAULT_BACKEND_PORT = 12333
DEFAULT_CONFIG_FILE = "config.yaml"
MAX_STEPS = 32
RUNTIME_EVENT_KEEP = 500


def _log(msg: str, *args: object) -> None:
    """后端控制台调试日志。print + flush，确保即时可见。"""
    try:
        import time as _t

        ts = _t.strftime("%H:%M:%S")
        print(f"[{ts}] [Agent] {msg}", *args, flush=True)
    except Exception:  # noqa: BLE001 - 日志不能影响主流程
        pass


AGENT_SYSTEM_PROMPT = """你是 GalTransl 项目翻译助手 Agent。你接到一个 Galgame 翻译项目，需要自主驱动从准备字典到完成翻译再到质量复核的全流程，就像一个熟手用户在桌面端图形界面里操作一样。

# 你的身份
- 你只操作"当前选定的这一个项目"，不要假设有其他项目。
- 你通过调用工具完成所有操作，工具背后调用的是和图形界面完全相同的后端 API，你不会绕过校验。
- 你可以也应该在调用工具的同时用自然语言说明你的决策与思考（这一段会实时展示给用户）。

# 标准翻译流程（必须按此顺序推进）
1. **了解项目**：先调用 get_project_overview 看输入文件、缓存进度、配置。确认项目输入文件非空、配置里已设翻译引擎，再继续。
2. **字典准备（在启动翻译前必须完成）**：
   a. 调用 list_dict_files 查看项目已配置的译前/GPT/译后字典文件；
   b. 调用 read_dict 读取现有内容，判断人名、专有名词是否已收录；
   c. 若缺少人名表，调用 get_name_table；若返回为空，先调用 start_translation(translator="dump-name") 生成人名表（dump-name 是导出 name 字段的专用 translator），完成后再次 get_name_table 查看结果，再调用 save_name_table 写回（若需要修正译名）；
   d. 若 GPT 字典为空且项目较大，可调用 start_translation(translator="GenDic") 自动生成 GPT 字典，并在该任务 completed 后通过 list_dict_files/read_dict 确认生成结果。
3. **启动翻译**：字典就绪后，调用 start_translation(translator="<主翻译引擎>")。主翻译引擎从项目配置或 overview 中确认，常用值：ForGal-json / ForGal-tsv / ForNovel / sakura-v1.0 / galtransl-v3。一次只启动一个，项目已有运行中任务时不要重复提交。
4. **跟进进度**：调用 get_progress 或 get_runtime 轮询。翻译任务 completed 后再进入下一步；running 时用 wait 工具等待一段合理时间（如翻译任务就 wait minutes=1~3，短任务 wait seconds=30）后再查，不要连续空转轮询。等待期间界面会显示倒计时。
5. **复核结果**：调用 list_problems 查看自动检测到的翻译问题（残留日文、字典使用不当、过长等）。用 read_cache 的 index 参数精确读取有问题的条目（如 list_problems 返回的 index，可直接 `index="33-40,50-60"` 一次取多条）浏览实际译文。
6. **问题修复循环**：对能直接改译文的条目，用 patch_cache 一次批量修改多条（传 patches 数组，每条给 index 和要改的字段，如 pre_dst/proofread_dst），适合修正残留日文、明显错译；对需要字典约束的系统性问题，先 save_dict 补字典，再 start_translation(translator="rebuildr") 用更新后的字典重建结果（rebuildr 会跳过翻译、仅用译前/译后字典刷写结果 json）。patch_cache 与 rebuildr 可配合使用：先 patch 掉个别硬错，再 rebuildr 统一刷一遍字典相关的问题。重建/修改后再 list_problems 复核，直到问题数量显著下降。
7. **完成**：当翻译完成、问题数可控时，用一段自然语言总结本次操作（做了什么、翻译进度、剩余问题建议），不要调用工具，直接输出总结即可结束。

# 约束
- 每一步只调用必要的工具；能在一次工具调用里拿到的信息不要拆成多次。
- 不要在未准备字典的情况下直接启动主翻译。
- 不要连续重复调用同一个工具相同参数（避免死循环）；若上一步结果不理想，换策略或总结收尾。
- 工具返回的 error 要阅读并据此调整下一步，不要忽略。
- 你无法关闭程序、无法修改项目目录以外的文件、无法访问网络。只做翻译相关工作。
"""


@dataclass(slots=True)
class AgentEvent:
    """单条 Agent 事件，会原样推给前端 SSE。"""

    type: str  # thought | tool_call | tool_result | finish | error | stopped
    step: int
    data: dict[str, Any] = field(default_factory=dict)

    def to_sse(self) -> str:
        payload = {"type": self.type, "step": self.step, **self.data}
        return f"event: agent\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "step": self.step, **self.data}


@dataclass(slots=True)
class AgentState:
    status: str = "idle"  # idle | running | done | stopped | failed
    goal: str = ""
    project_dir: str = ""
    config_file_name: str = ""
    backend_profile_data: dict[str, Any] = field(default_factory=dict)
    started_at: float = 0.0
    finished_at: float = 0.0
    error: str = ""
    events: deque[AgentEvent] = field(default_factory=lambda: deque(maxlen=RUNTIME_EVENT_KEEP))
    step: int = 0


class AgentToolError(Exception):
    """工具执行失败。"""


class AgentRunner:
    """单次 Agent 运行。在独立线程内执行 run()。"""

    def __init__(
        self,
        state: AgentState,
        host: str = DEFAULT_BACKEND_HOST,
        port: int = DEFAULT_BACKEND_PORT,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.state = state
        self.base_url = f"http://{host}:{port}"
        self.stop_event = stop_event or threading.Event()
        self._openai_client: Any = None
        self._model: str = ""

    # ---- 事件 ----
    def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        self.state.step += 1
        self.state.events.append(AgentEvent(type=event_type, step=self.state.step, data=data))

    # ---- OpenAI 客户端 ----
    def _resolve_llm(self) -> None:
        """从 backend_profile_data 解析出 OpenAI 客户端与模型名。"""
        _log("解析后端配置中…")
        profile = self.state.backend_profile_data or {}
        openai_section = profile.get("OpenAI-Compatible") or {}
        if not isinstance(openai_section, dict):
            raise RuntimeError("backend profile missing OpenAI-Compatible section")
        tokens = openai_section.get("tokens") or []
        if not isinstance(tokens, list) or not tokens:
            raise RuntimeError("backend profile OpenAI-Compatible.tokens is empty")
        first = tokens[0]
        if not isinstance(first, dict):
            raise RuntimeError("first token entry is not an object")
        token = str(first.get("token", "")).strip()
        endpoint = str(first.get("endpoint", "")).strip()
        model = str(first.get("modelName", "")).strip()
        if not token:
            raise RuntimeError("backend profile token is empty (请先在「翻译后端配置」页填写 token)")
        if not model:
            raise RuntimeError("backend profile modelName is empty (请先在「翻译后端配置」页填写 modelName)")
        base_url = _normalize_endpoint(endpoint)
        masked = (token[:4] + "…" + token[-4:]) if len(token) > 8 else "***"
        _log(f"LLM 配置: model={model} endpoint={base_url} token={masked}")
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - 依赖缺失
            raise RuntimeError("openai 包未安装，Agent 无法运行") from exc
        self._openai_client = OpenAI(api_key=token, base_url=base_url)
        self._model = model
        _log("OpenAI 客户端就绪")

    # ---- 主循环 ----
    def run(self) -> None:
        try:
            self._resolve_llm()
            messages = [
                {"role": "system", "content": AGENT_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"项目目录：{self.state.project_dir}\n"
                        f"配置文件：{self.state.config_file_name}\n"
                        f"目标：{self.state.goal or '按标准流程完成本项目的翻译'}\n\n"
                        "请开始。先了解项目，再准备字典，然后启动翻译。"
                    ),
                },
            ]
            for _ in range(MAX_STEPS):
                if self.stop_event.is_set():
                    self._emit("stopped", {"reason": "用户停止"})
                    _log("收到停止信号，退出循环")
                    self.state.status = "stopped"
                    return
                loop_step = _ + 1
                _log(f"—— 第 {loop_step}/{MAX_STEPS} 轮：请求 LLM 中…")
                req_started = time.time()
                resp = self._openai_client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    tools=AGENT_TOOLS,
                    tool_choice="auto",
                    stream=False,
                )
                req_ms = int((time.time() - req_started) * 1000)
                choice = resp.choices[0].message
                content = (getattr(choice, "content", None) or "").strip()
                tool_calls = getattr(choice, "tool_calls", None) or []
                _log(f"LLM 返回（耗时 {req_ms}ms）：content 长度={len(content)} tool_calls={len(tool_calls)}")

                # 思考/决策文本（即使同时有 tool_calls 也展示）
                if content:
                    preview = content if len(content) <= 120 else content[:117] + "…"
                    _log(f"  💭 思考: {preview}")
                    self._emit("thought", {"content": content})

                if not tool_calls:
                    _log(f"无工具调用，Agent 完成，共 {self.state.step} 步")
                    self._emit("finish", {"summary": content, "total_steps": self.state.step})
                    self.state.status = "done"
                    return

                # 把 assistant 这条消息原样追加（含 tool_calls），再逐个执行
                messages.append(_drop_none(choice.model_dump()))
                for tc in tool_calls:
                    if self.stop_event.is_set():
                        self._emit("stopped", {"reason": "用户停止"})
                        _log("工具执行前收到停止信号，退出")
                        self.state.status = "stopped"
                        return
                    call_id = tc.id
                    name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError as exc:
                        args = {}
                        _log(f"  🔧 工具调用: {name} (参数解析失败: {exc})")
                        self._emit("tool_call", {"id": call_id, "name": name, "arguments": tc.function.arguments})
                        err = f"参数 JSON 解析失败：{exc}"
                        self._emit("tool_result", {"id": call_id, "name": name, "ok": False, "error": err, "duration_ms": 0})
                        messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps({"error": err}, ensure_ascii=False)})
                        continue

                    args_preview = json.dumps(args, ensure_ascii=False)
                    if len(args_preview) > 160:
                        args_preview = args_preview[:157] + "…"
                    _log(f"  🔧 工具调用: {name}({args_preview})")
                    self._emit("tool_call", {"id": call_id, "name": name, "arguments": args})
                    started = time.time()
                    try:
                        result = self._dispatch_tool(name, args)
                        ok = True
                        duration_ms = int((time.time() - started) * 1000)
                        _log(f"  ✅ 工具结果: {name} 耗时 {duration_ms}ms")
                        self._emit("tool_result", {"id": call_id, "name": name, "ok": True, "result": result, "duration_ms": duration_ms})
                        content_str = json.dumps(result, ensure_ascii=False)
                    except AgentToolError as exc:
                        duration_ms = int((time.time() - started) * 1000)
                        _log(f"  ❌ 工具失败: {name} 耗时 {duration_ms}ms -> {exc}")
                        self._emit("tool_result", {"id": call_id, "name": name, "ok": False, "error": str(exc), "duration_ms": duration_ms})
                        content_str = json.dumps({"error": str(exc)}, ensure_ascii=False)
                    messages.append({"role": "tool", "tool_call_id": call_id, "content": content_str})

            # 超出步数上限
            _log(f"超出最大步数 {MAX_STEPS}，Agent 停止")
            self._emit("error", {"message": f"达到最大步数 {MAX_STEPS}，Agent 停止", "total_steps": self.state.step})
            self.state.status = "failed"
        except Exception as exc:  # noqa: BLE001 - 顶层守护
            tb = traceback.format_exc()
            _log(f"❌ Agent 异常: {exc}\n{tb}")
            self._emit("error", {"message": str(exc), "traceback": tb})
            self.state.error = str(exc)
            self.state.status = "failed"
        finally:
            self.state.finished_at = time.time()
            _log(f"Agent 结束，状态={self.state.status}，总步数={self.state.step}")

    # ---- 工具分发 ----
    def _dispatch_tool(self, name: str, args: dict[str, Any]) -> Any:
        handler = _TOOL_HANDLERS.get(name)
        if handler is None:
            raise AgentToolError(f"未知工具：{name}")
        return handler(self, args)

    # ---- 工具实现（调本机 HTTP） ----
    def _project_id(self) -> str:
        return _encode_project_id(self.state.project_dir)

    def _http_get(self, path: str) -> Any:
        url = f"{self.base_url}{path}"
        return _http_json("GET", url)

    def _http_post(self, path: str, body: dict[str, Any]) -> Any:
        url = f"{self.base_url}{path}"
        return _http_json("POST", url, body)


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


# ---- Agent 工具的 OpenAI function schema ----
AGENT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_project_overview",
            "description": "了解项目：列出输入/输出/缓存文件与当前翻译进度、项目配置。流程第一步，调用它确认项目可用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dict_files",
            "description": "列出项目配置的译前字典(preDict)、GPT字典(gpt.dict)、译后字典(postDict)文件及各文件内容。准备字典阶段使用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_dict",
            "description": "读取某个项目字典文件的完整内容（按 file_key，来自 list_dict_files 返回的 dict_contents 的 key）。",
            "parameters": {
                "type": "object",
                "properties": {"file_key": {"type": "string", "description": "字典文件 key，形如 (project_dir)项目GPT字典.txt"}},
                "required": ["file_key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_dict",
            "description": "写入/覆盖某个项目字典文件的内容。file_key 必须来自 list_dict_files；content 为 tab 分隔文本（格式：日文<Tab>中文[<Tab>解释]）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_key": {"type": "string"},
                    "content": {"type": "string", "description": "字典全文，覆盖写入"},
                },
                "required": ["file_key", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_dict_file",
            "description": "在项目里新建一个字典文件并登记到配置（pre/gpt/post 三类之一）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": ["pre", "gpt", "post"], "description": "pre=译前, gpt=GPT, post=译后"},
                    "filename": {"type": "string", "description": "字典文件名，如 项目GPT字典.txt"},
                },
                "required": ["category", "filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_name_table",
            "description": "读取 name替换表（人名表），返回 src_name/dst_name/count 列表。为空说明尚未生成。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_name_table",
            "description": "保存人名表（写入 name替换表.csv）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "names": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"src_name": {"type": "string"}, "dst_name": {"type": "string"}, "count": {"type": "integer"}},
                        },
                    }
                },
                "required": ["names"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_translation",
            "description": "提交一个翻译任务。translator 取值：ForGal-json/ForGal-tsv/ForNovel（主翻译）；GenDic（生成GPT字典）；dump-name（导出人名表）；rebuildr（用字典重建结果，跳过翻译）；rebuilda（用字典重建缓存+结果）。会复用当前选定的后端配置。",
            "parameters": {
                "type": "object",
                "properties": {"translator": {"type": "string"}},
                "required": ["translator"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_translation",
            "description": "停止当前项目正在运行的翻译任务。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait",
            "description": (
                "等待一段时间后继续。用于翻译/GenDic 等后台任务还在跑、需要隔一会儿再看进度的场景。"
                "等待期间界面会显示倒计时；若用户期间点了停止，会立即中断等待。"
                "单次最多等待 1800 秒（30 分钟）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "seconds": {
                        "type": "number",
                        "description": "等待的秒数。与 minutes 二选一；两个都传时以二者之和为准。",
                    },
                    "minutes": {
                        "type": "number",
                        "description": "等待的分钟数。适合等待较久的翻译任务。",
                    },
                    "reason": {
                        "type": "string",
                        "description": "可选。等待原因，会显示在界面上，如 '等待翻译任务完成'。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_progress",
            "description": "查询当前翻译进度（已翻译/总句数、问题数、失败数、各文件进度）。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_runtime",
            "description": "查询运行时状态：当前任务状态(running/completed/failed)、阶段、最近错误与成功、ETA。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_problems",
            "description": "列出自动检测到的翻译问题（残留日文、字典使用、过长等），用于复核与修复循环。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_cache",
            "description": "读取某个缓存文件的条目（译文）。filename 来自 get_project_overview 的缓存文件列表。留空 index 返回前 30 条；指定 index 只返回指定的条目。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                    "index": {
                        "type": "string",
                        "description": "可选。要读取的条目 index 列表，支持逗号和区间，如 \"33-40,50-60\"、\"5,9,12\"、\"100-105\"。留空返回前 30 条。",
                    },
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_cache",
            "description": "在缓存中搜索译文/原文/问题。query 为关键词，field 取 all/src/dst/problem。",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "field": {"type": "string", "enum": ["all", "src", "dst", "problem"]}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "patch_cache",
            "description": "批量修改某个缓存文件中若干条目的译文/校对等字段。一次可改多条，只更新 patches 里指定的条目与字段，其它条目原样保留。适合发现问题后改译文、再配合 rebuildr 重建的复核循环。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "缓存文件名，来自 get_project_overview 的缓存文件列表"},
                    "patches": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "index": {"type": "integer", "description": "要修改的条目 index"},
                                "pre_dst": {"type": "string", "description": "可选。新译文（机翻结果）"},
                                "proofread_dst": {"type": "string", "description": "可选。新校对译文（校对/润色结果，优先于 pre_dst）"},
                                "trans_by": {"type": "string", "description": "可选。标记译者，如 'manual' 或 'agent'"},
                                "trans_conf": {"type": "integer", "description": "可选。译文置信度 0-100"},
                                "doub_content": {"type": "string", "description": "可选。存疑内容备注"},
                                "unknown_proper_noun": {"type": "string", "description": "可选。未知专有名词备注"},
                            },
                            "required": ["index"],
                        },
                    },
                },
                "required": ["filename", "patches"],
            },
        },
    },
]


def _drop_none(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


# ---- 工具实现 ----
def _tool_get_project_overview(runner: AgentRunner, _args: dict[str, Any]) -> Any:
    pid = runner._project_id()
    files = runner._http_get(f"/api/projects/{pid}/files")
    progress = runner._http_get(f"/api/projects/{pid}/progress")
    cfg = runner._http_get(f"/api/projects/{pid}/config?config={urllib.parse.quote(runner.state.config_file_name)}")
    return {
        "input_files": [f["name"] for f in files.get("input_files", [])],
        "output_files": [f["name"] for f in files.get("output_files", [])],
        "cache_files": [f["name"] for f in files.get("cache_files", [])],
        "progress": {
            "total": progress.get("total", 0),
            "translated": progress.get("translated", 0),
            "problems": progress.get("problems", 0),
            "failed": progress.get("failed", 0),
        },
        "config": cfg.get("config", {}),
    }


def _tool_list_dict_files(runner: AgentRunner, _args: dict[str, Any]) -> Any:
    pid = runner._project_id()
    cfg = urllib.parse.quote(runner.state.config_file_name)
    data = runner._http_get(f"/api/projects/{pid}/dictionary/project?config={cfg}")
    contents = data.get("dict_contents", {})
    summary = {key: val.get("count", 0) for key, val in contents.items()}
    return {
        "pre_dict_files": data.get("pre_dict_files", []),
        "gpt_dict_files": data.get("gpt_dict_files", []),
        "post_dict_files": data.get("post_dict_files", []),
        "line_counts": summary,
        "contents": {k: _join_lines(v.get("lines", [])) for k, v in contents.items()},
    }


def _tool_read_dict(runner: AgentRunner, args: dict[str, Any]) -> Any:
    file_key = str(args.get("file_key", "")).strip()
    if not file_key:
        raise AgentToolError("file_key is required")
    pid = runner._project_id()
    cfg = urllib.parse.quote(runner.state.config_file_name)
    data = runner._http_get(f"/api/projects/{pid}/dictionary/project?config={cfg}")
    contents = data.get("dict_contents", {})
    if file_key not in contents:
        available = list(contents.keys())
        raise AgentToolError(f"file_key 不存在：{file_key}。可选：{available}")
    entry = contents[file_key]
    return {"file_key": file_key, "lines": entry.get("lines", []), "count": entry.get("count", 0)}


def _tool_save_dict(runner: AgentRunner, args: dict[str, Any]) -> Any:
    file_key = str(args.get("file_key", "")).strip()
    content = str(args.get("content", ""))
    if not file_key:
        raise AgentToolError("file_key is required")
    pid = runner._project_id()
    body = {
        "config_file_name": runner.state.config_file_name,
        "file_key": file_key,
        "content": content,
    }
    return runner._http_post(f"/api/projects/{pid}/dictionary/project/save", body)


def _tool_create_dict_file(runner: AgentRunner, args: dict[str, Any]) -> Any:
    category = str(args.get("category", "")).strip()
    filename = str(args.get("filename", "")).strip()
    if category not in ("pre", "gpt", "post"):
        raise AgentToolError("category must be one of: pre, gpt, post")
    if not filename:
        raise AgentToolError("filename is required")
    pid = runner._project_id()
    body = {"config_file_name": runner.state.config_file_name, "category": category, "filename": filename}
    return runner._http_post(f"/api/projects/{pid}/dictionary/project/create", body)


def _tool_get_name_table(runner: AgentRunner, _args: dict[str, Any]) -> Any:
    pid = runner._project_id()
    return runner._http_get(f"/api/projects/{pid}/name-table")


def _tool_save_name_table(runner: AgentRunner, args: dict[str, Any]) -> Any:
    names = args.get("names", [])
    if not isinstance(names, list):
        raise AgentToolError("names must be an array")
    pid = runner._project_id()
    return runner._http_post(f"/api/projects/{pid}/name-table/save", {"names": names})


def _tool_start_translation(runner: AgentRunner, args: dict[str, Any]) -> Any:
    translator = str(args.get("translator", "")).strip()
    if not translator:
        raise AgentToolError("translator is required")
    body = {
        "project_dir": runner.state.project_dir,
        "config_file_name": runner.state.config_file_name,
        "translator": translator,
        "backend_profile_data": runner.state.backend_profile_data,
    }
    result = runner._http_post("/api/jobs", body)
    return {"job_id": result.get("job_id"), "status": result.get("status"), "translator": translator}


def _tool_stop_translation(runner: AgentRunner, _args: dict[str, Any]) -> Any:
    pid = runner._project_id()
    return runner._http_post(f"/api/projects/{pid}/stop", {})


WAIT_SECONDS_MAX = 1800  # 单次等待上限 30 分钟，避免 Agent 卡死在一次无限等待里
WAIT_TICK = 0.5  # 倒计时刷新步长（秒），兼顾界面流畅与轮询开销


def _tool_wait(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """等待指定时长。期间持续推 wait_tick 事件供界面显示倒计时。

    等待可被停止信号立即打断：先等满则 normal，被打断则 interrupted。无论哪种
    都以工具成功返回，把状态交给模型判断下一步，而不是抛错中断整个循环。
    """
    raw_seconds = args.get("seconds")
    raw_minutes = args.get("minutes")
    try:
        seconds = float(raw_seconds) if raw_seconds is not None else 0.0
        minutes = float(raw_minutes) if raw_minutes is not None else 0.0
    except (TypeError, ValueError):
        raise AgentToolError("seconds / minutes 必须是数字")
    if seconds < 0 or minutes < 0:
        raise AgentToolError("等待时长不能为负数")

    total = seconds + minutes * 60
    if total <= 0:
        raise AgentToolError("未指定等待时长：请给出 seconds 或 minutes")
    total = min(total, WAIT_SECONDS_MAX)

    reason = str(args.get("reason", "") or "").strip()
    total_ms = int(total * 1000)
    started = time.monotonic()
    _log(f"  ⏳ 开始等待 {total:g}s" + (f"（{reason}）" if reason else ""))
    runner._emit("wait_start", {"seconds": round(total, 1), "total_ms": total_ms, "reason": reason})

    interrupted = False
    while True:
        if runner.stop_event.is_set():
            interrupted = True
            break
        elapsed = time.monotonic() - started
        if elapsed >= total:
            break
        remaining_ms = max(0, total_ms - int(elapsed * 1000))
        runner._emit("wait_tick", {"remaining_ms": remaining_ms, "total_ms": total_ms})
        runner.stop_event.wait(WAIT_TICK)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    remaining_ms = 0 if interrupted else max(0, total_ms - elapsed_ms)
    runner._emit(
        "wait_end",
        {"interrupted": interrupted, "elapsed_ms": elapsed_ms, "remaining_ms": remaining_ms, "total_ms": total_ms},
    )
    if interrupted:
        _log(f"  ⏳ 等待被停止信号打断，已等 {elapsed_ms / 1000:.1f}s")
        return {"waited_seconds": round(elapsed_ms / 1000, 1), "status": "interrupted", "note": "等待被用户停止打断"}
    _log(f"  ⏳ 等待结束，共 {elapsed_ms / 1000:.1f}s")
    return {"waited_seconds": round(elapsed_ms / 1000, 1), "status": "completed"}


def _tool_get_progress(runner: AgentRunner, _args: dict[str, Any]) -> Any:
    pid = runner._project_id()
    return runner._http_get(f"/api/projects/{pid}/progress")


def _tool_get_runtime(runner: AgentRunner, _args: dict[str, Any]) -> Any:
    pid = runner._project_id()
    data = runner._http_get(f"/api/projects/{pid}/runtime")
    job = data.get("job") or {}
    summary = data.get("summary") or {}
    return {
        "job_status": job.get("status"),
        "job_translator": job.get("translator"),
        "stage": data.get("stage"),
        "current_file": data.get("current_file"),
        "summary": {
            "total": summary.get("total", 0),
            "translated": summary.get("translated", 0),
            "percent": summary.get("percent", 0),
            "problems": summary.get("problems", 0),
            "failed": summary.get("failed", 0),
            "eta_seconds": summary.get("eta_seconds"),
            "workers_active": summary.get("workers_active", 0),
        },
        "recent_errors": data.get("recent_errors", [])[:5],
    }


def _tool_list_problems(runner: AgentRunner, _args: dict[str, Any]) -> Any:
    pid = runner._project_id()
    cfg = urllib.parse.quote(runner.state.config_file_name)
    data = runner._http_get(f"/api/projects/{pid}/problems?config={cfg}")
    problems = data.get("problems", [])
    return {"total": data.get("total", len(problems)), "problems": problems[:50]}


def _tool_read_cache(runner: AgentRunner, args: dict[str, Any]) -> Any:
    filename = str(args.get("filename", "")).strip()
    if not filename:
        raise AgentToolError("filename is required")
    pid = runner._project_id()
    data = runner._http_get(f"/api/projects/{pid}/cache/{urllib.parse.quote(filename)}")
    entries = data.get("entries", [])
    index_spec = str(args.get("index", "") or "").strip()
    # 不指定 index：返回前 30 条，供 Agent 通览
    if not index_spec:
        return {"filename": filename, "count": len(entries), "returned": len(entries[:30]), "entries": entries[:30]}

    wanted = _parse_index_spec(index_spec)
    if not wanted:
        raise AgentToolError(f"无法解析 index 列表：{index_spec!r}（示例：33-40,50-60）")
    by_index = {int(e.get("index", -1)): e for e in entries if e.get("index") is not None}
    picked = [by_index[i] for i in sorted(wanted) if i in by_index]
    missing = sorted(i for i in wanted if i not in by_index)
    result: dict[str, Any] = {
        "filename": filename,
        "count": len(entries),
        "requested": sorted(wanted),
        "returned": len(picked),
        "entries": picked,
    }
    if missing:
        result["missing_indexes"] = missing
    return result


def _parse_index_spec(spec: str) -> set[int]:
    """解析 \"33-40,50-60\" / \"5,9,12\" / \"100-105\" 为 index 集合。"""
    result: set[int] = set()
    for part in spec.split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            bounds = token.split("-", 1)
            try:
                lo = int(bounds[0])
                hi = int(bounds[1])
            except ValueError:
                continue
            if lo > hi:
                lo, hi = hi, lo
            result.update(range(lo, hi + 1))
        else:
            try:
                result.add(int(token))
            except ValueError:
                continue
    return result


def _tool_search_cache(runner: AgentRunner, args: dict[str, Any]) -> Any:
    query = str(args.get("query", "")).strip()
    field = str(args.get("field", "all")).strip() or "all"
    if not query:
        raise AgentToolError("query is required")
    pid = runner._project_id()
    body = {
        "query": query,
        "field": field,
        "options": {"re": False},
        "max_results": 100,
        "config_file_name": runner.state.config_file_name,
    }
    return runner._http_post(f"/api/projects/{pid}/cache/search", body)


# patch_cache 允许更新的条目字段白名单（其余字段一律不动，避免误改 problem/preview 等派生字段）
_PATCHABLE_FIELDS = {
    "pre_dst",
    "proofread_dst",
    "trans_by",
    "trans_conf",
    "doub_content",
    "unknown_proper_noun",
}


def _tool_patch_cache(runner: AgentRunner, args: dict[str, Any]) -> Any:
    filename = str(args.get("filename", "")).strip()
    if not filename:
        raise AgentToolError("filename is required")
    patches_raw = args.get("patches")
    if not isinstance(patches_raw, list) or not patches_raw:
        raise AgentToolError("patches must be a non-empty array")
    pid = runner._project_id()

    # 读现有条目，按 index 建索引，只为命中的条目应用补丁，再整体写回。
    # /cache/save 会整体覆盖文件并由后端重建 problem/post_dst_preview，
    # 所以这里必须读全量 -> 改 -> 写全量，而非只写补过的几条。
    data = runner._http_get(f"/api/projects/{pid}/cache/{urllib.parse.quote(filename)}")
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        raise AgentToolError("缓存文件 entries 非数组，无法 patch")
    by_index: dict[int, dict[str, Any]] = {}
    for e in entries:
        idx = e.get("index")
        if idx is not None:
            try:
                by_index[int(idx)] = e
            except (TypeError, ValueError):
                continue

    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    not_found: list[int] = []
    changed_fields: list[str] = []
    for p in patches_raw:
        if not isinstance(p, dict):
            skipped.append({"index": None, "reason": "patch 不是对象"})
            continue
        idx = p.get("index")
        try:
            idx_i = int(idx)
        except (TypeError, ValueError):
            skipped.append({"index": idx, "reason": "index 不是整数"})
            continue
        entry = by_index.get(idx_i)
        if entry is None:
            not_found.append(idx_i)
            continue
        updates = {k: v for k, v in p.items() if k in _PATCHABLE_FIELDS and v is not None}
        if not updates:
            skipped.append({"index": idx_i, "reason": "无可更新字段（只允许 pre_dst/proofread_dst/trans_by/trans_conf/doub_content/unknown_proper_noun）"})
            continue
        entry.update(updates)
        applied.append({"index": idx_i, "fields": list(updates.keys())})
        for f in updates:
            if f not in changed_fields:
                changed_fields.append(f)

    if not applied:
        raise AgentToolError(
            f"没有条目被更新（applied=0, skipped={len(skipped)}, not_found={len(not_found)}）"
        )

    save_body = {
        "filename": filename,
        "entries": entries,
        "config_file_name": runner.state.config_file_name,
    }
    save_result = runner._http_post(f"/api/projects/{pid}/cache/save", save_body)
    return {
        "filename": filename,
        "applied": applied,
        "applied_count": len(applied),
        "not_found_indexes": not_found,
        "skipped": skipped,
        "changed_fields": changed_fields,
        "save": save_result,
    }


_TOOL_HANDLERS: dict[str, Callable[[AgentRunner, dict[str, Any]], Any]] = {
    "get_project_overview": _tool_get_project_overview,
    "list_dict_files": _tool_list_dict_files,
    "read_dict": _tool_read_dict,
    "save_dict": _tool_save_dict,
    "create_dict_file": _tool_create_dict_file,
    "get_name_table": _tool_get_name_table,
    "save_name_table": _tool_save_name_table,
    "start_translation": _tool_start_translation,
    "stop_translation": _tool_stop_translation,
    "wait": _tool_wait,
    "get_progress": _tool_get_progress,
    "get_runtime": _tool_get_runtime,
    "list_problems": _tool_list_problems,
    "read_cache": _tool_read_cache,
    "search_cache": _tool_search_cache,
    "patch_cache": _tool_patch_cache,
}


def _join_lines(lines: list[str]) -> str:
    return "\n".join(lines)


class AgentRuntime:
    """全局 Agent 注册表：单项目单 agent，与翻译 job 类似的互斥。"""

    def __init__(self) -> None:
        self._states: dict[str, AgentState] = {}
        self._runners: dict[str, AgentRunner] = {}
        self._stop_events: dict[str, threading.Event] = {}
        self._lock = threading.RLock()  # 可重入锁：允许在持锁时调用本类其它方法（如 start() 内调用 status()）

    @staticmethod
    def _key(project_dir: str) -> str:
        return os.path.abspath(project_dir)

    def start(
        self,
        project_dir: str,
        config_file_name: str,
        backend_profile_data: dict[str, Any],
        goal: str = "",
        host: str = DEFAULT_BACKEND_HOST,
        port: int = DEFAULT_BACKEND_PORT,
    ) -> dict[str, Any]:
        key = self._key(project_dir)
        with self._lock:
            existing = self._states.get(key)
            if existing and existing.status == "running":
                _log(f"启动被拒：项目已有 Agent 在运行 -> {key}")
                raise ValueError("该项目已有 Agent 在运行")
            stop_event = threading.Event()
            state = AgentState(
                status="running",
                goal=goal,
                project_dir=project_dir,
                config_file_name=config_file_name or DEFAULT_CONFIG_FILE,
                backend_profile_data=backend_profile_data or {},
                started_at=time.time(),
            )
            runner = AgentRunner(state, host=host, port=port, stop_event=stop_event)
            self._states[key] = state
            self._runners[key] = runner
            self._stop_events[key] = stop_event
            thread = threading.Thread(target=runner.run, name=f"agent-{os.path.basename(key)}", daemon=True)
            thread.start()
            _log(f"Agent 线程已启动: project={key} config={config_file_name} goal={goal[:60]}")
            return self.status(project_dir)

    def stop(self, project_dir: str) -> dict[str, Any]:
        key = self._key(project_dir)
        with self._lock:
            event = self._stop_events.get(key)
            if event:
                event.set()
            state = self._states.get(key)
            if state and state.status == "running":
                state.status = "stopped"
        return self.status(project_dir)

    def status(self, project_dir: str) -> dict[str, Any]:
        key = self._key(project_dir)
        with self._lock:
            state = self._states.get(key)
            if state is None:
                return {"status": "idle", "project_dir": project_dir, "events": [], "step": 0}
            return {
                "status": state.status,
                "project_dir": state.project_dir,
                "goal": state.goal,
                "step": state.step,
                "started_at": state.started_at,
                "finished_at": state.finished_at,
                "error": state.error,
                "events": [e.to_dict() for e in state.events],
            }

    def drain_events(self, project_dir: str, after_step: int = 0) -> list[dict[str, Any]]:
        """取 after_step 之后的所有事件，供 SSE 增量推送。"""
        key = self._key(project_dir)
        with self._lock:
            state = self._states.get(key)
            if state is None:
                return []
            return [e.to_dict() for e in state.events if e.step > after_step]
