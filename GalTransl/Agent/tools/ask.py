"""询问用户（ask_user）与压缩归档回查工具。"""

from __future__ import annotations

import os
from typing import Any, TYPE_CHECKING

from GalTransl.Agent.core import COMPACT_ARCHIVE_READ_CHARS, _log, _truncate_text
from GalTransl.Agent.models import AgentToolError
from GalTransl.Agent.permissions import AUTO_QUIET_MODE, _normalize_permission_mode
from GalTransl.Agent.tools.search import _apply_search_order, _search_order, _search_paging_args
from GalTransl.Search import select_search_hits

if TYPE_CHECKING:
    from GalTransl.Agent.runner import AgentRunner


# ---- 询问用户（ask_user）----
# 工具阻塞等回答、**不设超时**；用户跳过或回合被停止时，
# 该题以空答案返回（工具仍算成功，模型据此继续，而不是让整个回合报错）。
ASK_MAX_QUESTIONS = 4  # 一次最多问几题（界面一题一步，问太多就成了审讯）
ASK_MAX_OPTIONS = 6  # 每题最多几个候选项（用户永远还能自己填）
ASK_WAIT_TICK = 0.2  # 等待回答的轮询步长（秒）：只为尽快响应停止信号


def _normalize_ask_questions(args: dict[str, Any]) -> list[dict[str, Any]]:
    """校验并归一 ask_user 的问题：题干非空、选项去重后至少一个、题数与选项数有上限。"""
    raw = args.get("questions")
    if not isinstance(raw, list) or not raw:
        raise AgentToolError("questions 必须是非空数组")
    if len(raw) > ASK_MAX_QUESTIONS:
        raise AgentToolError(f"一次最多问 {ASK_MAX_QUESTIONS} 个问题（收到 {len(raw)} 个）")
    questions: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise AgentToolError("questions 里每一项都必须是一个对象")
        text = str(item.get("question") or "").strip()
        if not text:
            raise AgentToolError("每个问题都要有非空的 question")
        raw_options = item.get("options")
        if not isinstance(raw_options, list):
            raise AgentToolError(f"问题「{text}」缺少 options 数组")
        options: list[str] = []
        for option in raw_options:
            label = str(option or "").strip()
            if label and label not in options:
                options.append(label)
        if not options:
            raise AgentToolError(f"问题「{text}」至少要有一个非空选项")
        capped = options[:ASK_MAX_OPTIONS]
        recommended = str(item.get("recommended") or "").strip()
        if recommended and recommended not in capped:
            # 让它改而不是默默丢掉：零打断档位要靠这个值代答，写错就等于没推荐
            raise AgentToolError(
                f"问题「{text}」的 recommended（{recommended}）必须是 options 里的一项"
            )
        questions.append(
            {
                "question": text,
                "options": capped,
                "multiSelect": item.get("multiSelect") is True,
                "recommended": recommended,
            }
        )
    return questions


def _normalize_ask_answers(raw: Any, questions: list[dict[str, Any]]) -> list[list[str] | None]:
    """校验前端送回的答案：题数要对上、单选不许给多个值；空值 = 跳过（None）。"""
    if not isinstance(raw, list) or len(raw) != len(questions):
        raise AgentToolError(f"答案数量必须与问题数量一致（需要 {len(questions)} 个）")
    answers: list[list[str] | None] = []
    for idx, (item, question) in enumerate(zip(raw, questions), start=1):
        if item is None:
            answers.append(None)
            continue
        if not isinstance(item, list):
            raise AgentToolError(f"第 {idx} 题的答案必须是字符串数组或 null")
        values: list[str] = []
        for value in item:
            text = str(value or "").strip()
            if text and text not in values:
                values.append(text)
        if len(values) > 1 and not question.get("multiSelect"):
            raise AgentToolError(f"第 {idx} 题是单选，只能给一个答案")
        answers.append(values or None)
    return answers


def _format_ask_answers(questions: list[dict[str, Any]], answers: list[list[str] | None]) -> str:
    """给模型看的答案文本（固定口径）：`问题：答案1、答案2`，多题用 --- 分隔。"""
    lines = []
    for question, answer in zip(questions, answers):
        lines.append(f"{question['question']}：{'、'.join(answer) if answer else '（跳过）'}")
    return "\n---\n".join(lines)


def _tool_read_history_archive(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """读上下文压缩归档（会话目录里的 chunk 文件）。

    三种用法：不带参数列出归档；带 chunk 取全文；带 query 在全部归档里检索。
    归档只是"想不起来时回查"的兜底，所以单次回传有字符上限——超了就让模型
    改用关键词检索，而不是把整份归档塞回上下文（那就白压了）。
    """
    store = getattr(runner, "_store", None)
    if store is None:
        return {"archives": [], "note": "本会话没有落盘，压缩归档不可用"}
    chunks = store.list_chunks()
    if not chunks:
        return {"archives": [], "note": "本次会话还没有压缩归档（历史还没触发过上下文压缩）"}

    raw_chunk = str(args.get("chunk", "") or "").strip()
    query = str(args.get("query", "") or "").strip()
    limit = args.get("limit")
    if not isinstance(limit, int) or limit <= 0:
        limit = 30
    limit = min(limit, 200)

    if query:
        hits: list[dict[str, Any]] = []
        order = _search_order(args)
        _, offset = _search_paging_args(args)
        for item in chunks:
            text = store.read_chunk(item["name"]) or ""
            for lineno, line in enumerate(text.splitlines(), 1):
                if query.lower() in line.lower():
                    hits.append({
                        "chunk": item["name"],
                        "line": lineno,
                        "text": _truncate_text(line.strip(), 300),
                    })
        page = select_search_hits(hits, limit, offset, order)
        result = {
            "query": query, "hits": page, "total": len(hits), "offset": offset,
            "returned": len(page), "has_more": offset + len(page) < len(hits),
            "truncated": len(page) < len(hits),
        }
        _apply_search_order(result, order)
        return result

    if not raw_chunk:
        return {
            "archives": [
                {"name": item["name"], "topics": item.get("topics") or ""} for item in chunks
            ],
            "note": "用 chunk 参数读其中一份，或用 query 关键词检索细节",
        }

    names = [item["name"] for item in chunks]
    name = raw_chunk if raw_chunk in names else ""
    if not name and raw_chunk.isdigit():
        index = int(raw_chunk)
        if 1 <= index <= len(names):
            name = names[index - 1]
    if not name:
        raise AgentToolError(f"没有这个归档：{raw_chunk}（可用：{'、'.join(names)}）")
    text = store.read_chunk(name) or ""
    truncated = len(text) > COMPACT_ARCHIVE_READ_CHARS
    return {
        "chunk": name,
        "content": text[:COMPACT_ARCHIVE_READ_CHARS] if truncated else text,
        "truncated": truncated,
        "note": "内容过长已截断，可用 query 检索关键词" if truncated else "",
    }


def _auto_ask_answers(questions: list[dict[str, Any]]) -> list[list[str] | None]:
    """「全自动-零打断」的自动作答：每题取推荐项，没给推荐就退而取第一个选项。

    取第一个是刻意的兜底——模型列选项时通常把首选放最前面，而这一档的语义就是
    "别停下来问我"：卡在等人作答上，比偶尔选歪一次更糟。多选问题也只给这一项。
    """
    answers: list[list[str] | None] = []
    for question in questions:
        recommended = str(question.get("recommended") or "").strip()
        options = list(question.get("options") or [])
        pick = recommended if recommended in options else (options[0] if options else "")
        answers.append([pick] if pick else None)
    return answers


def _tool_ask_user(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """问用户：阻塞当前回合直到用户作答（或回合被停止，此时按跳过返回）。

    **全自动-零打断**档位不阻塞：直接按每题的 recommended 代答（没填推荐就取第一个
    选项），用户完全不会被打断。模型仍照常"问"，只是拿到的是系统代选的答案。
    """
    questions = _normalize_ask_questions(args)
    if _normalize_permission_mode(runner.state.permission_mode) == AUTO_QUIET_MODE:
        answers = _auto_ask_answers(questions)
        _log(f"  🤖 零打断档位：按推荐项代答 {len(questions)} 个问题，不等用户")
        return {
            "summary": _format_ask_answers(questions, answers)
            + "\n（当前是「全自动-零打断」档位：以上答案由系统按推荐项自动选择，用户未被打断）",
            "questions": [question["question"] for question in questions],
            "answers": answers,
            "auto_answered": True,
        }
    answers = runner.ask_user(os.urandom(8).hex(), runner._active_tool_call_id, questions)
    return {
        "summary": _format_ask_answers(questions, answers),
        "questions": [question["question"] for question in questions],
        "answers": answers,
    }
