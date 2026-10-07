"""上下文管理：token 估算、压缩切点与归档、残缺历史修补、prompt cache。"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Sequence

from GalTransl.Agent.core import (
    ASCII_CHARS_PER_TOKEN,
    COMPACT_ARCHIVE_TOOL_RESULT_CHARS,
    COMPACT_KEEP_RECENT_MAX_TOKENS,
    COMPACT_KEEP_RECENT_MIN_TOKENS,
    COMPACT_KEEP_RECENT_RATIO,
    DEFAULT_CONTEXT_WINDOW,
    LLM_SILENCE_TIMEOUT,
    MESSAGE_OVERHEAD_TOKENS,
    MULTIBYTE_CHARS_PER_TOKEN,
    REASONING_FIELD_NAMES,
    _INTERNAL_MSG_PREFIX,
    _truncate_text,
)


def _parse_context_window(raw: Any) -> int:
    """解析后端配置里的 contextWindow。非法/缺失时用默认值。

    允许 "128000" / 128000 / "128k" 这几种写法。
    """
    if raw is None:
        return DEFAULT_CONTEXT_WINDOW
    try:
        if isinstance(raw, str):
            text = raw.strip().lower()
            if not text:
                return DEFAULT_CONTEXT_WINDOW
            if text.endswith("k"):
                value = int(float(text[:-1]) * 1000)
            else:
                value = int(float(text))
        else:
            value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CONTEXT_WINDOW
    # 明显不合理的值（负数、太小）当作没配
    if value < 1000:
        return DEFAULT_CONTEXT_WINDOW
    return value


def _llm_timeout() -> Any:
    """单次 LLM 请求的超时（连接/写入/池收紧，读用静默上限）。

    用 SDK 自己的 Timeout 类构造：openai>=2 起它不再是 `httpx.Timeout` 的别名
    （内部换成 httpx2），直接传 httpx.Timeout 虽然运行时能用、但跨版本不保证。
    老版本 SDK 没有这个类时退化成秒数（四档同值，仍有兜底作用）。
    """
    try:
        from openai import Timeout
    except ImportError:  # pragma: no cover - 老版本 SDK
        return LLM_SILENCE_TIMEOUT
    return Timeout(connect=10.0, read=LLM_SILENCE_TIMEOUT, write=30.0, pool=10.0)


def _profile_context_window(profile: dict[str, Any] | None) -> int:
    """从后端配置里取上下文窗口（与 AgentRunner._resolve_llm 同一口径）。

    窗口配在 OpenAI-Compatible.tokens[0].contextWindow，缺省/非法时用默认值。
    AgentRuntime.start 要在建会话时就知道窗口（写进状态供界面显示），所以抽出来
    共用，避免两处解析口径漂移。
    """
    openai_section = (profile or {}).get("OpenAI-Compatible") or {}
    if isinstance(openai_section, dict):
        tokens = openai_section.get("tokens") or []
        if isinstance(tokens, list) and tokens and isinstance(tokens[0], dict):
            return _parse_context_window(tokens[0].get("contextWindow"))
    return DEFAULT_CONTEXT_WINDOW


def _printable_ascii_chars(text: str) -> int:
    """可打印 ASCII（空格 ~ `~`）的字符数；换行、控制符、中日韩都不算。"""
    return sum(1 for ch in text if " " <= ch <= "~")


def _chars_to_tokens(ascii_chars: int, multibyte_chars: int) -> int:
    """按字符构成折算 token（系数见 ASCII_CHARS_PER_TOKEN / MULTIBYTE_CHARS_PER_TOKEN）。"""
    return int(
        ascii_chars / ASCII_CHARS_PER_TOKEN
        + multibyte_chars / MULTIBYTE_CHARS_PER_TOKEN
        + 0.999  # 向上取整
    )


def _estimate_text_tokens(text: str) -> int:
    """一段文本的 token 粗估。"""
    ascii_chars = _printable_ascii_chars(text)
    return _chars_to_tokens(ascii_chars, len(text) - ascii_chars)


def _message_text_parts(message: dict[str, Any]) -> list[str]:
    """一条消息里**真的会发给 provider** 的文本：正文 + 回传的思考 + tool_calls 的名字与参数。"""
    parts: list[str] = []
    content = message.get("content")
    if isinstance(content, str):
        parts.append(content)
    for name in REASONING_FIELD_NAMES:
        reasoning = message.get(name)
        if isinstance(reasoning, str):
            parts.append(reasoning)
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        if isinstance(function, dict):
            parts.append(str(function.get("name") or ""))
            parts.append(str(function.get("arguments") or ""))
    return parts


def _estimate_message_tokens(message: dict[str, Any]) -> int:
    """单条消息的 token 粗估：正文 + 回传的思考 + tool_calls 的参数 JSON。

    两件必须算进去的东西：

    - **思考**（reasoning_content / reasoning）：thinking 模式下它会跟着 assistant 消息一起
      回传给 provider（见 _messages_for_request 与 _reasoning_echo），是这份请求真实占用的
      一部分。漏掉它，纯本地估算就系统性偏低（实测一条会话 22 万字符的思考被当成免费），而
      estimated 走锚点法（provider 真实 prompt_tokens + 锚点后新增消息的本地估算）并不偏低
      ——两套口径对不上，压缩的"保留段"判定就被带歪。
    - **中文按多字节折算**（见 _chars_to_tokens）：一刀切按 /4 算，中文内容会再低两三倍，
      保留段预算就形同虚设——按这个偏小的单位量预算，实际留下来的历史远超预算。
    """
    ascii_chars = 0
    multibyte_chars = 0
    for part in _message_text_parts(message):
        part_ascii = _printable_ascii_chars(part)
        ascii_chars += part_ascii
        multibyte_chars += len(part) - part_ascii
    return _chars_to_tokens(ascii_chars, multibyte_chars) + MESSAGE_OVERHEAD_TOKENS


# 工具参数里"含密钥"的键：emit 成事件/写日志前换成占位。模型自己仍能传真实值
# （它需要真配置才能起任务），但界面上的工具卡片与会话文件不该出现明文 token。
_SECRET_TOOL_ARGS = ("backend_profile_data", "translator_profile_data", "gendic_profile_data", "subagent_profile_data")


def _sanitize_tool_args(args: dict[str, Any]) -> dict[str, Any]:
    """工具参数 → 可展示/落盘的副本（含密钥的字段换成占位）。"""
    if not any(name in args for name in _SECRET_TOOL_ARGS):
        return args
    return {
        **args,
        **{name: "<含 token 的后端配置，已省略>" for name in _SECRET_TOOL_ARGS if name in args},
    }


def _reasoning_echo(acc: dict[str, Any] | None) -> dict[str, str]:
    """这一轮的思考内容 → 要随 assistant 消息回传给 provider 的键值对。

    用流里出现过的原字段名（reasoning_content / reasoning）回传；provider 没给过思考
    就返回空 dict——不往消息里塞它不认识的字段。
    """
    if not acc:
        return {}
    text = "".join(acc.get("reasoning") or [])
    if not text:
        return {}
    return {str(acc.get("reasoning_field") or REASONING_FIELD_NAMES[0]): text}


def _assistant_parts(acc: dict[str, Any] | None) -> list[dict[str, Any]]:
    """把一条助手响应的累加器拍成有序 parts（助手消息的 content 模型）。

    段落顺序：思考 → 正文 → 工具调用。同类文本会归并成一段，不按「想/说」交替
    逐段切分——provider 交替推思考时碎片极多，逐段成卡片会把界面刷爆；界面本身
    也是按类归并渲染的（见 AgentPage 的 findAppendTarget），两边口径必须一致，
    否则「进行中的消息」与 delta 拼出来的卡片对不上，收尾时会对不上号。

    返回的是**可持久化的转录数据**：思考与正文只有进了这里，刷新/切会话后
    才重建得出来（delta 事件是瞬态的、不落盘也不进快照）。
    """
    if not acc:
        return []
    parts: list[dict[str, Any]] = []
    reasoning = "".join(acc.get("reasoning") or [])
    if reasoning:
        parts.append({"type": "reasoning", "text": reasoning})
    content = "".join(acc.get("content") or [])
    if content:
        parts.append({"type": "text", "text": content})
    for _, slot in sorted((acc.get("tools") or {}).items()):
        parts.append({
            "type": "tool_call",
            "id": slot.get("id", ""),
            "name": slot.get("name", ""),
            "arguments": "".join(slot.get("arguments_parts") or []),
        })
    return parts


_TOOLS_OVERHEAD_CACHE: dict[str, int] = {}


def _tools_overhead_tokens(tools: Any) -> int:
    """请求里 tools schema 的固定开销（token 粗估）。

    它不在 messages 里，却每次都随请求发出去——本地估算只算 messages 的话，界面上的"用量"
    和日志里的"压缩后大小"会比真实 prompt_tokens 小一截（实测差约 9k），看着就像"估算不准"。
    schema 是常量，按 JSON 串缓存，不必每回合重新序列化。
    """
    schema = json.dumps(tools, ensure_ascii=False)
    cached = _TOOLS_OVERHEAD_CACHE.get(schema)
    if cached is None:
        cached = _estimate_text_tokens(schema)
        _TOOLS_OVERHEAD_CACHE[schema] = cached
    return cached


def _estimate_usage_tokens(
    messages: list[dict[str, Any]], anchor: int = 0, anchored: int = 0, overhead: int = 0
) -> int:
    """估算一次请求占用的 token 数（供压缩判断与界面用量指示共用）。

    用法锚定法：有上一次响应的 prompt_tokens 作锚点时，只对锚点之后新增的消息按字符估算
    ——锚点里已经含了 system/tools 那部分静态开销，不能再加一遍；没有锚点就整体估算，
    这时要把 tools schema 的固定开销（overhead）算上，否则整体偏低约 9k。
    """
    if anchor > 0 and 0 <= anchored <= len(messages):
        return anchor + sum(_estimate_message_tokens(m) for m in messages[anchored:])
    return overhead + sum(_estimate_message_tokens(m) for m in messages)


def _is_tool_call_anchor(message: dict[str, Any]) -> bool:
    """该消息是否为 tool 响应（必须紧跟其 assistant.tool_calls）。"""
    return message.get("role") == "tool"


def _message_has_tool_calls(message: dict[str, Any]) -> bool:
    return bool(message.get("tool_calls"))


def _keep_recent_tokens(limit: int, ratio: float = COMPACT_KEEP_RECENT_RATIO) -> int:
    """保留段的 token 预算：触发线的一个零头，夹在上下限之间。

    对齐 PI-Desktop 的 keepRecentTokens（由 hardLimit 派生、约两成、夹在 8K~64K）：窗口配得
    很小（触发线不为正）时按下限走，至少留一点现场给模型。
    """
    if limit <= 0:
        return COMPACT_KEEP_RECENT_MIN_TOKENS
    return max(
        COMPACT_KEEP_RECENT_MIN_TOKENS,
        min(int(limit * ratio), COMPACT_KEEP_RECENT_MAX_TOKENS),
    )


def _find_compaction_cut(messages: list[dict[str, Any]], keep_recent_tokens: int) -> int:
    """找出压缩切点：从尾部往前凑够保留预算，返回前缀长度。

    保留段按 **token 预算**挑，不是"最近 N 条"：一条几万字符的工具结果就能吃掉整个预算，
    按条数挑会把它连同十几条无关消息一起扣在上下文里，压缩等于没压。最后一条消息永远保留
    ——否则没有可压的头部，压缩根本启动不了；单条就超预算的大结果会独占保留段，这是"不截断
    消息内容"的代价（要更狠就得截断它，PI-Desktop 走的是截断那条路）。

    切点必须落在"安全位置"，否则会切断 assistant.tool_calls 与后续 tool 响应
    的配对，导致请求非法。具体规则：
    - 切点前一条不能是带 tool_calls 的 assistant（否则其 tool 响应被留下）；
    - 切点本身不能是 tool 响应（否则它的 assistant 被裁掉）。
    找不到安全位置就往前退，退到 0 表示放弃本次压缩。
    """
    total = len(messages)
    if total <= 1:
        return 0
    budget = max(1, int(keep_recent_tokens))
    cut = total
    used = 0
    while cut > 0:
        cost = _estimate_message_tokens(messages[cut - 1])
        if cut < total and used + cost > budget:
            break  # 再往前就装不下了（最后一条不设限：总得留一条）
        used += cost
        cut -= 1
    # 我们要保留 [cut:]，所以被裁掉的是 [0:cut]
    while cut > 0:
        prev = messages[cut - 1] if cut - 1 >= 0 else None
        nxt = messages[cut]
        # 前缀最后一条是带 tool_calls 的 assistant -> 会拆散它和它的 tool 响应
        if prev is not None and _message_has_tool_calls(prev):
            cut -= 1
            continue
        # 后缀第一条是 tool 响应 -> 它的 assistant 被裁掉了
        if _is_tool_call_anchor(nxt):
            cut -= 1
            continue
        break
    # 至少要有内容被裁掉，且尾部保留完整
    if cut <= 0 or cut >= total:
        return 0
    # 头部只装得下 system（其余都进了保留段）：没有可摘的内容，别为它白跑一次摘要请求
    if not any(m.get("role") != "system" for m in messages[:cut]):
        return 0
    return cut


def _history_fingerprint(messages: list[dict[str, Any]]) -> str:
    """只保存历史的摘要，不把正文、工具参数或密钥复制进统计元信息。"""
    encoded = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _restore_usage_anchor(messages: list[dict[str, Any]], checkpoint: Any) -> tuple[int, int, str]:
    """统计只能用于原请求对应的消息前缀；压缩、截断或改写后自动作废。"""
    if not isinstance(checkpoint, dict):
        return 0, 0, ""
    tokens = checkpoint.get("prompt_tokens")
    count = checkpoint.get("message_count")
    if (type(tokens) is not int or tokens <= 0 or type(count) is not int
            or not 0 < count <= len(messages)):
        return 0, 0, ""
    if checkpoint.get("fingerprint") != _history_fingerprint(messages[:count]):
        return 0, 0, ""
    return tokens, count, str(checkpoint.get("model") or "")


def _restore_compacted_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """恢复会话时重建「压缩之后的历史」：system + 摘要 + 当时的现场 + 其后的消息。

    历史文件是**只追加**的：压缩不会把被裁掉的那些消息从文件里删掉（它们早就写在那儿了），
    所以 load() 拿回来的是压缩**前**的全量。不看摘要重建的话，重启后模型看到的是把摘要又
    摊开一遍的全量历史（实测 98 条 / 167k tokens，触发线才 94k）——压缩白做，而且一开回合
    立刻又压一次、归档越堆越多。

    摘要自己记着「当时保留了多少条真消息」（`_compact_kept`），据此从摘要往前找回那段现场；
    往前扫时跳过内部消息（上一轮的摘要不是消息）。老会话没有这条摘要记录，原样返回。
    """
    last = -1
    for index, message in enumerate(messages):
        if isinstance(message, dict) and message.get("_compact_summary"):
            last = index
    if last < 0:
        return list(messages)
    try:
        kept = max(0, int(messages[last].get("_compact_kept") or 0))
    except (TypeError, ValueError):
        kept = 0
    tail: list[dict[str, Any]] = []
    index = last - 1
    while index > 0 and len(tail) < kept:  # 不碰 messages[0]：那是 system
        message = messages[index]
        if not _is_internal_message(message):
            tail.append(message)
        index -= 1
    tail.reverse()
    system = messages[0] if messages and messages[0].get("role") == "system" else None
    return ([system] if system is not None else []) + [messages[last]] + tail + messages[last + 1 :]


def _serialize_for_summary(messages: list[dict[str, Any]]) -> str:
    """把一段消息序列化成供摘要模型阅读的文本（截断超长工具结果）。"""
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role") or "?"
        content = msg.get("content")
        text = content if isinstance(content, str) else ""
        calls = msg.get("tool_calls") or []
        if calls:
            names = []
            for tc in calls:
                fn = tc.get("function") or {} if isinstance(tc, dict) else {}
                names.append(str(fn.get("name") or "?"))
            text = (text + " " if text else "") + f"[调用工具: {', '.join(names)}]"
        if len(text) > 1500:
            text = text[:1500] + "…（已截断）"
        lines.append(f"{role}: {text}")
    return "\n".join(lines)


def _local_fallback_summary(messages: list[dict[str, Any]]) -> str:
    """摘要模型不可用时的兜底：只记录这段时间调用过哪些工具。"""
    tools: list[str] = []
    for msg in messages:
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {} if isinstance(tc, dict) else {}
            name = str(fn.get("name") or "")
            if name:
                tools.append(name)
    tool_text = "、".join(dict.fromkeys(tools)) if tools else "无"
    return (
        "## 目标\n（早前对话已压缩，详细目标见消息历史）\n\n"
        "## 已完成的工作\n"
        f"早前 {len(messages)} 条消息因上下文超限被压缩。期间调用过的工具：{tool_text}。\n\n"
        "## 关键决策\n无法从压缩中恢复，请根据当前项目状态判断。\n\n"
        "## 当前进度与项目状态\n请先查一次项目概览与运行状态重新确认当前进度。\n\n"
        "## 待办与注意事项\n"
        "如不确定之前的进展，先查一次项目状态再继续，避免重复已完成的操作。"
    )


def _is_internal_message(message: Any) -> bool:
    """是否带内部标记（`_` 开头的键）：压缩指令 / 摘要消息。

    这类消息只在内存里用（回滚、缓存断点跳过、是否已完成压缩的判断），
    发请求前必须剥掉标记而不是整条丢弃。
    """
    return isinstance(message, dict) and any(
        isinstance(key, str) and key.startswith(_INTERNAL_MSG_PREFIX) for key in message
    )


def _content_text(content: Any) -> str:
    """取消息正文文本（content 可能是字符串，也可能是多段 block 数组）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block["text"]
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        return "\n".join(parts)
    return "" if content is None else str(content)


def _render_archive_messages(messages: list[dict[str, Any]]) -> list[str]:
    """把一段消息渲染成归档正文（Markdown）。

    工具结果要截断：归档是给模型"想不起来时回查"用的，一条几万字符的缓存读取
    原样存进去，回查一次又把上下文撑爆，等于没压。
    """
    lines: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "?")
        if role == "user":
            lines += ["## 用户", "", _content_text(message.get("content")), ""]
        elif role == "assistant":
            lines += ["## 助手", ""]
            calls = message.get("tool_calls") or []
            if calls:
                names = []
                for tc in calls:
                    fn = tc.get("function") or {} if isinstance(tc, dict) else {}
                    names.append(str(fn.get("name") or "?"))
                lines += [f"_工具调用：{', '.join(names)}_", ""]
            text = _content_text(message.get("content"))
            if text:
                lines += [text, ""]
        else:
            name = str(message.get("name") or "tool")
            text = _truncate_text(_content_text(message.get("content")), COMPACT_ARCHIVE_TOOL_RESULT_CHARS)
            lines += [f"### 工具结果：{name}", "", "```", text, "```", ""]
    return lines


def _strip_internal_fields(message: dict[str, Any]) -> dict[str, Any]:
    """去掉 `_` 开头的内部键（压缩指令 / 摘要标记），发给 provider 前必须剥掉。

    没有内部键时原样返回，省一次整条消息的拷贝。
    """
    if not _is_internal_message(message):
        return message
    return {
        key: value
        for key, value in message.items()
        if not (isinstance(key, str) and key.startswith(_INTERNAL_MSG_PREFIX))
    }


# ---- 残缺历史（进程在工具执行中途被杀）----
# 界面与请求两侧同一句话：两边都给用户/模型一个能对上的说法
_DANGLING_TOOL_NOTE = "这一步没有执行完（应用中途退出，工具结果缺失）"


def _dangling_tool_calls(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """找出"assistant 调了工具、却没有对应 tool 响应"的调用（按出现顺序）。

    进程在工具执行中途退出便会留下这种残缺历史——最典型的就是**卡在 ask_user 或权限
    审批上时用户直接关了程序**：assistant.tool_calls 已经落盘，每个调用的 tool 消息还没写。
    """
    dangling: list[dict[str, Any]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            index += 1
            continue
        # 这一批 tool 响应一直到哪：中间不能夹别的角色
        cursor = index + 1
        answered: set[str] = set()
        while cursor < len(messages) and messages[cursor].get("role") == "tool":
            answered.add(str(messages[cursor].get("tool_call_id") or ""))
            cursor += 1
        for call in message.get("tool_calls") or []:
            call_id = str((call or {}).get("id") or "")
            if not call_id or call_id in answered:
                continue
            function = (call or {}).get("function") or {}
            dangling.append(
                {
                    "id": call_id,
                    "name": str(function.get("name") or ""),
                    "arguments": str(function.get("arguments") or ""),
                    # 占位结果该插在哪：这一批 tool 响应之后（保持 tool 消息连续）
                    "at": cursor,
                }
            )
        index = max(cursor, index + 1)
    return dangling


def _tool_placeholder_message(call_id: str) -> dict[str, Any]:
    """给残缺调用补的占位 tool 消息。"""
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps(
            {"status": "interrupted", "error": _DANGLING_TOOL_NOTE}, ensure_ascii=False
        ),
    }


def _messages_with_tool_placeholders(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """补上缺失的 tool 占位响应，返回一份**合法**的消息列表（不动原历史）。

    provider 要求带 tool_calls 的 assistant 后面必须紧跟每个调用的 tool 响应，缺一条整份
    请求就非法——"重启之后接着聊"的第一句会被 400 挡回来。补在**请求这一侧**、不写回历史：
    会话是追加式落盘的，往中间插一条会让内存顺序与磁盘顺序对不上（重启重放时又变回残缺）。
    """
    dangling = _dangling_tool_calls(messages)
    if not dangling:
        return messages
    merged = list(messages)
    # 从后往前插：前面的位置不会漂；同一个 assistant 的多个缺失调用插在同一位置，
    # 倒序插入后顺序与 tool_calls 一致
    for item in reversed(dangling):
        merged.insert(min(int(item["at"]), len(merged)), _tool_placeholder_message(str(item["id"])))
    return merged


def _dangling_ask_question_texts(messages: Sequence[dict[str, Any]]) -> list[str]:
    """从残缺历史里取回最近一次 ask_user 的题目文本（取不回来就返回空表）。"""
    for item in reversed(_dangling_tool_calls(messages)):
        if item["name"] != "ask_user":
            continue
        try:
            args = json.loads(item["arguments"] or "{}")
        except json.JSONDecodeError:
            return []
        raw = args.get("questions")
        if not isinstance(raw, list):
            return []
        return [
            str(q.get("question") or "").strip()
            for q in raw
            if isinstance(q, dict) and str(q.get("question") or "").strip()
        ]
    return []


def _answers_for_text(answers: Any) -> list[list[str] | None]:
    """HTTP 传来的答案（数组套数组，空/null = 跳过）归一成 _format_ask_answers 要的形状。"""
    if not isinstance(answers, list):
        return []
    picked: list[list[str] | None] = []
    for item in answers:
        if isinstance(item, list):
            values = [str(v).strip() for v in item if str(v).strip()]
            picked.append(values or None)
        else:
            picked.append(None)
    return picked


def _permission_decision_text(decision: str, name: str, reason: str) -> str:
    """审批卡的选择 → 一条用户消息（审批对应的回合已经没了时用）。"""
    target = f"「{name}」" if name else "上面那步操作"
    if decision == "deny":
        text = f"我不同意执行{target}，这一步别做了。"
        note = reason.strip()
        if note:
            text += f"原因：{note}"
        return text
    return f"我同意执行{target}，请继续。"


def _resume_context(**kwargs: Any) -> dict[str, Any]:
    """重启后"接着聊"要重放的前端上下文（token 不落盘，只能由请求带上）。

    字段与 /api/agent/message 同一套；任务后端的空对象表示清除专属默认，None 才是未提供。
    """
    return {
        key: value for key, value in kwargs.items()
        if value or (key.startswith(("gendic_profile_", "subagent_profile_")) and value is not None)
    }


def _with_cache_control(message: dict[str, Any]) -> dict[str, Any]:
    """给一条消息的正文末尾挂 cache_control（Anthropic 的断点语法）。

    content 是纯字符串时包成单块数组——这是走 OpenAI 兼容接口表达 Anthropic 断点
    的唯一方式。只有 tool_calls、没有正文的 assistant 消息，使用消息级断点。
    """
    content = message.get("content")
    if isinstance(content, str) and content:
        return {
            **message,
            "content": [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}],
        }
    if isinstance(content, list) and content:
        blocks = list(content)
        last = blocks[-1]
        if isinstance(last, dict):
            blocks[-1] = {**last, "cache_control": {"type": "ephemeral"}}
            return {**message, "content": blocks}
    if message.get("tool_calls"):
        return {**message, "cache_control": {"type": "ephemeral"}}
    return message


def _apply_prompt_cache(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """给尾部最近 2 条真实消息 + 工具表末尾打 cache_control 断点（滚动双 marker）。

    为什么是 2 个：本轮的第 2 个断点就是下一轮的"读"断点，历史单调增长也能命中；
    工具调用失败、回滚掉最后一条消息时，倒数第二个断点还在，单步回滚依然命中。
    只打 1 个的经典失败是——本轮标记最后一条，下一轮它变成倒数第二条、带 marker 的
    位置整体前移，服务端看到的前缀不同，整段 miss。
    """
    out = list(messages)
    marked = 0
    for index in range(len(out) - 1, -1, -1):
        if marked >= 2:
            break
        message = out[index]
        if _is_internal_message(message):
            continue  # 瞬时注入的消息下一轮不会以同样形式出现，标记它等于白写
        cached = _with_cache_control(message)
        if cached is message:
            continue  # 空消息没有可标记内容，不消耗断点名额
        out[index] = cached
        marked += 1
    cached_tools = list(tools)
    if cached_tools:
        cached_tools[-1] = {**cached_tools[-1], "cache_control": {"type": "ephemeral"}}
    return out, cached_tools


def _resolve_prompt_caching(raw: Any, model: str, base_url: str) -> bool:
    """是否注入 cache_control 断点。配置取值：auto（默认）/ on / off。

    Anthropic 系（含经网关转发的 Claude）需要显式断点；OpenAI 兼容的多数实现
    （DeepSeek 等）由服务端做自动前缀缓存，注入既无收益、还可能因陌生字段被拒。
    auto 因此只在 base_url / 模型名看得出是 Anthropic 系时才开。
    """
    value = str(raw or "").strip().lower()
    if value in ("on", "true", "1", "yes"):
        return True
    if value in ("off", "false", "0", "no"):
        return False
    lowered = f"{base_url or ''} {model or ''}".lower()
    return any(hint in lowered for hint in ("anthropic", "claude", "openrouter"))
