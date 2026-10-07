"""翻译任务类工具：启动/停止翻译、等待、运行时状态与错误汇总。"""

from __future__ import annotations

import time
from typing import Any, TYPE_CHECKING

from GalTransl import TRANSLATOR_SUPPORTED
from GalTransl.Agent.core import RUNTIME_ERRORS_PER_QUERY, _log
from GalTransl.Agent.models import AgentToolError
from GalTransl.Agent.tools.project import _backend_summary
from GalTransl.Agent.tools.plugin_settings import encoding_recovery

if TYPE_CHECKING:
    from GalTransl.Agent.models import AgentState
    from GalTransl.Agent.runner import AgentRunner


def _tool_start_translation(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """启动翻译任务。

    后端必须用**翻译任务会用的那份**（前端按「项目选择 → 否则全局『翻译器默认』」送来，
    见 state.translator_profile_data），不能用 Agent 自己那份——否则任务会拿着 Agent 的
    模型跑，可用性检测也跟着测错模型（用户就是这么发现的）。只有前端没送来时才回落到
    Agent 那份，并在返回里说明。
    """
    translator = str(args.get("translator", "")).strip()
    if not translator:
        raise AgentToolError("translator is required")
    # 模型/用户可能使用小写引擎名，任务接口要求注册表中的标准拼写。
    translator = next(
        (name for name in TRANSLATOR_SUPPORTED if name.casefold() == translator.casefold()),
        translator,
    )
    state = runner.state
    profile = state.translator_profile_data or state.backend_profile_data
    body = {
        "project_dir": state.project_dir,
        "config_file_name": state.config_file_name,
        "translator": translator,
        "backend_profile_data": profile,
    }
    files = args.get("files")
    if files is not None:
        if not isinstance(files, list) or not files:
            raise AgentToolError("files must be a non-empty array of filenames")
        body["input_files"] = [str(f).strip() for f in files if str(f).strip()]
        if not body["input_files"]:
            raise AgentToolError("files 里没有有效的文件名")
    result = runner._http_post("/api/jobs", body)
    used = _backend_summary(profile, state.translator_profile_name)
    out: dict[str, Any] = {
        "job_id": result.get("job_id"),
        "status": result.get("status"),
        "translator": translator,
        # 实际用哪份后端起任务（名字/类型/模型），出问题时一眼能对上
        "backend": used,
        **({"files": body["input_files"]} if files is not None else {}),
    }
    if not state.translator_profile_data:
        out["note"] = (
            "没拿到「翻译任务会用」的后端配置（前端没随消息送 translator_profile_data，"
            "通常是项目选择或全局「翻译器默认」那份），"
            f"本次用的是 Agent 自己的后端（{used['name'] or used['type'] or '未知'}）。"
        )
    return out


def _tool_stop_translation(runner: AgentRunner, _args: dict[str, Any]) -> Any:
    pid = runner._project_id()
    return runner._http_post(f"/api/projects/{pid}/stop", {})


WAIT_SECONDS_MAX = 1800  # 单次等待上限 30 分钟，避免 Agent 卡死在一次无限等待里
WAIT_TICK = 0.5  # 倒计时刷新步长（秒），兼顾界面流畅与轮询开销
# 带 job_id 时查任务状态的间隔（秒）：0.5s 那是给界面倒计时用的，查后端别这么勤
WAIT_JOB_POLL_SECONDS = 3.0
# 任务已经结束的状态（见 Service.JobState.status）：等到其中之一就不必再等了
WAIT_JOB_DONE_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _find_job(runner: AgentRunner, job_id: str) -> dict[str, Any] | None:
    """在任务列表里按 id 找一个任务：**列表里没有这个 id 返回 None**。

    用列表接口而不是 /api/jobs/{id}：后者对未知 id 直接 404（`_http_json` 会抛错），
    而"这个 id 不在列表里"对等待来说是个正常结局（id 写错/任务已被清掉），不该跟
    "查询失败"混在一起。列表本身拿不到（网络/格式不对）时抛错，由调用方当查询失败处理
    ——继续等，别把一次抖动当成"任务不见了"。
    """
    data = runner._http_get("/api/jobs")
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        raise AgentToolError("任务列表读取失败：/api/jobs 没有返回 jobs 数组")
    for job in jobs:
        if isinstance(job, dict) and str(job.get("job_id") or "") == job_id:
            return job
    return None


def _tool_wait(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """等待指定时长；给了 job_id 就"它先结束，或时长先到"，谁先到算谁。

    期间持续推 wait_tick 事件供界面显示倒计时。等待可被停止信号立即打断：
    先等满则 normal，被打断则 interrupted。无论哪种都以工具成功返回，
    把状态交给模型判断下一步，而不是抛错中断整个循环。
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
        raise AgentToolError(
            "未指定等待时长：请给出 seconds 或 minutes"
            "（带 job_id 时也要给——它是兜底：任务先结束就提前返回）"
        )
    total = min(total, WAIT_SECONDS_MAX)

    # 要盯的任务（可选）：给了它就不用非等满时长——任务先结束就立刻收尾
    job_id = str(args.get("job_id", "") or "").strip()

    reason = str(args.get("reason", "") or "").strip()
    total_ms = int(total * 1000)
    started = time.monotonic()
    # 事件带上本次工具调用 id：界面据此把倒计时挂到这一行（多次等待各挂各行）
    call_id = runner._active_tool_call_id
    _log(
        f"  ⏳ 开始等待 {total:g}s"
        + (f"（或任务 {job_id} 先结束）" if job_id else "")
        + (f"（{reason}）" if reason else "")
    )
    runner._emit("wait_start", {
        "id": call_id,
        "seconds": round(total, 1),
        "total_ms": total_ms,
        "reason": reason,
        **({"job_id": job_id} if job_id else {}),
    })

    interrupted = False
    job_status = ""
    job_success: bool | None = None
    job_error = ""
    job_found = True
    next_poll = 0.0  # 先立刻查一次，之后每 WAIT_JOB_POLL_SECONDS 一次
    while True:
        if runner.stop_event.is_set():
            interrupted = True
            break
        elapsed = time.monotonic() - started
        if job_id and elapsed >= next_poll:
            next_poll = elapsed + WAIT_JOB_POLL_SECONDS
            found: dict[str, Any] | None = None
            query_ok = True
            try:
                found = _find_job(runner, job_id)
            except Exception as exc:  # noqa: BLE001 - 一次查询失败当"还在跑"，时间到了照样收尾
                query_ok = False
                _log(f"  ⏳ 查询任务 {job_id} 状态失败（{exc}），继续等")
            if query_ok and found is None:
                job_found = False  # id 写错或任务已被清掉：再等下去没意义
                break
            if found is not None:
                job_status = str(found.get("status") or "")
                if job_status in WAIT_JOB_DONE_STATUSES:
                    job_success = bool(found.get("success"))
                    job_error = str(found.get("error") or "")
                    break
        if elapsed >= total:
            break
        remaining_ms = max(0, total_ms - int(elapsed * 1000))
        runner._emit("wait_tick", {"id": call_id, "remaining_ms": remaining_ms, "total_ms": total_ms})
        runner.stop_event.wait(WAIT_TICK)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    remaining_ms = 0 if interrupted else max(0, total_ms - elapsed_ms)
    end_data: dict[str, Any] = {
        "id": call_id,
        "interrupted": interrupted,
        "elapsed_ms": elapsed_ms,
        "remaining_ms": remaining_ms,
        "total_ms": total_ms,
    }
    if job_id:
        # 为什么结束的：done=任务先结束 / timeout=时长先到 / missing=任务不在列表里
        end_data["job_status"] = job_status
        end_data["job_end_reason"] = (
            "interrupted" if interrupted
            else "missing" if not job_found
            else "done" if job_status in WAIT_JOB_DONE_STATUSES
            else "timeout"
        )
    runner._emit("wait_end", end_data)

    waited = round(elapsed_ms / 1000, 1)
    if interrupted:
        _log(f"  ⏳ 等待被停止信号打断，已等 {elapsed_ms / 1000:.1f}s")
        return {"waited_seconds": waited, "wait_interrupted": True, "note": "等待被用户停止打断"}
    if job_id and not job_found:
        _log(f"  ⏳ 任务 {job_id} 不在任务列表里，提前结束等待")
        return {
            "waited_seconds": waited,
            "job_id": job_id,
            "job_found": False,
            "note": (
                f"任务列表里找不到 {job_id}：id 可能写错，或这个任务已经不在列表里。"
                "用 get_runtime 看当前项目的任务状态再决定下一步。"
            ),
        }
    if job_id and job_status in WAIT_JOB_DONE_STATUSES:
        _log(f"  ⏳ 任务 {job_id} 已结束（{job_status}），等待提前收尾，共 {elapsed_ms / 1000:.1f}s")
        out: dict[str, Any] = {
            "waited_seconds": waited,
            "job_id": job_id,
            "job_status": job_status,
            "job_success": job_success,
            "wait_completed": True,
            "job_finished": True,
            "note": f"任务已经结束（{job_status}）——比等待时长先到，不用再等了，按流程处理结果（查进度/问题清单）。",
        }
        if job_error:
            out["job_error"] = job_error
            recovery = encoding_recovery(job_error)
            if recovery:
                out["recovery"] = recovery
        return out
    _log(f"  ⏳ 等待结束，共 {elapsed_ms / 1000:.1f}s")
    if job_id:
        # 时长先到：这一刻模型要的就是进度与 eta_seconds（下一步一定是 get_runtime），
        # 顺手取一份快照带上，省它一个来回。
        snapshot = _runtime_snapshot(runner)
        out: dict[str, Any] = {
            "waited_seconds": waited,
            "job_id": job_id,
            "job_status": job_status or "running",
            "job_finished": False,
            "wait_completed": True,
        }
        if snapshot is not None:
            out["runtime"] = snapshot
            out["note"] = (
                f"等待时长到了，任务 {job_id} 还在跑（{job_status or 'running'}）："
                "下面附了当前运行时快照（等同 get_runtime，含 summary.eta_seconds），"
                "据此决定下一轮等多久——eta 还长就再 wait 一次同一个 job_id，快完了就把时长调短盯着。"
            )
        else:
            out["note"] = (
                f"等待时长到了，任务 {job_id} 还在跑（{job_status or 'running'}）："
                "调用 get_runtime 看进度与 eta_seconds 再决定下一轮等多久"
                "（也可以再 wait 一次同一个 job_id）。"
            )
        return out
    return {
        "waited_seconds": waited,
        "wait_completed": True,
        "note": "这只是计时结束，不代表后台任务完成。如果是翻译任务，请调用 get_runtime 确认任务状态后再决定下一步。",
    }


def _runtime_snapshot(runner: AgentRunner) -> dict[str, Any] | None:
    """顺手取一份运行时快照（等同模型再调一次 get_runtime），取不到就返回 None。

    放在 wait 的"时长先到、任务还在跑"分支里：那一刻模型正需要进度与 eta_seconds 来决定
    下一轮等多久，直接带上就不必再多跑一个来回。副作用（错误水位线 seen_error_ids）与
    模型自己调 get_runtime 一致，所以不会造成同一条报错被重复发。
    快照只是顺手带的，取不到（后端抖了/项目读不到）不该影响 wait 本身的结论。
    """
    try:
        return _tool_get_runtime(runner, {})
    except Exception as exc:  # noqa: BLE001
        _log(f"  ⚠ wait 结束时取运行时快照失败：{exc}")
        return None


def _error_key(err: dict[str, Any]) -> str:
    """错误的去重键：后端 id + 内容指纹。

    后端 id 只精确到毫秒（同毫秒内两条同类型错误会撞成同一个 id），所以再拼上内容；
    缺 id 时就只用内容。同一条事件（id 与内容都不变）在快照窗口里待多久都只对应一个
    键——这正是"同一条 warning 不再反复报"的依据。
    """
    return "|".join(
        str(err.get(field) or "")
        for field in ("id", "kind", "filename", "index_range", "message", "ts")
    )


def _error_group_key(err: dict[str, Any]) -> tuple[str, str, str]:
    """归并键：同一类原因的报错算一组。

    按 kind + level + message 归并——真实报错（如 kind=parse 的「未解析到有效句子」）
    会横跨很多文件、很多条目反复出现，逐条发只会刷屏。message 里若嵌了本条自己的
    文件名，换成 {file} 占位，免得同一个原因被文件名拆成十几组。
    """
    kind = str(err.get("kind") or "")
    message = str(err.get("message") or "")
    filename = str(err.get("filename") or "")
    if filename and filename in message:
        message = message.replace(filename, "{file}")
    return kind, str(err.get("level") or ""), message


def _group_text(kind: str, level: str, message: str, count: int, files: list[str]) -> str:
    """一行可读的摘要：「parse 警告 × 23 次：未解析到有效句子（涉及 12 个文件…）」。"""
    label = "警告" if level == "warning" else "错误"
    text = f"{kind or '未知'} {label} × {count} 次：{message or '(无描述)'}"
    if files:
        listed = "、".join(files[:3])
        more = f" 等 {len(files)} 个" if len(files) > 3 else ""
        text += f"（涉及文件：{listed}{more}）"
    return text


def _summarize_group(events: list[dict[str, Any]]) -> dict[str, Any]:
    """把同一类的一批报错压成一条：次数 / 涉及文件 / 时间范围 / 可读摘要。"""
    first = events[0]
    kind, level, message = _error_group_key(first)
    files: list[str] = []
    for err in events:
        name = str(err.get("filename") or "")
        if name and name not in files:
            files.append(name)
    timestamps = [str(err.get("ts") or "") for err in events if err.get("ts")]
    count = len(events)
    return {
        "kind": kind,
        "level": level,
        "message": message,
        "count": count,
        "files": files[:5],
        "files_total": len(files),
        "first_ts": min(timestamps) if timestamps else "",
        "last_ts": max(timestamps) if timestamps else "",
        "text": _group_text(kind, level, message, count, files),
    }


def _group_errors(
    events: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """同类归并并按"报得多、最近报过"排序，返回（摘要, 该组的事件）。"""
    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for err in events:
        buckets.setdefault(_error_group_key(err), []).append(err)
    grouped = [(_summarize_group(group), group) for group in buckets.values()]
    grouped.sort(key=lambda item: str(item[0]["last_ts"]), reverse=True)  # 先按最近出现
    grouped.sort(key=lambda item: item[0]["count"], reverse=True)  # 次数多的在前（稳定排序）
    return grouped


def _take_fresh_errors(
    state: AgentState, errors: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    """挑出"还没发给过模型"的报错、同类归并，返回（本次要发的组, 还没发的事件数）。

    水位线（state.seen_error_ids）只保留仍在快照里的键——滚出快照的错误不会再回来，
    不必长期记着。归并后单次最多发 RUNTIME_ERRORS_PER_QUERY **组**；没轮到的组**不标记
    为已发**，留到下次查询，既不会一次刷屏，也不会把错误吞掉。
    """
    keys = [_error_key(err) for err in errors]
    live = set(keys)
    seen = {key for key in state.seen_error_ids if key in live}
    fresh_events = [err for key, err in zip(keys, errors) if key not in seen]
    grouped = _group_errors(fresh_events)
    reported = grouped[:RUNTIME_ERRORS_PER_QUERY]
    for _group, group_events in reported:  # 只把真发出去的记为已发
        for err in group_events:
            seen.add(_error_key(err))
    state.seen_error_ids = seen
    pending = sum(len(group) for _, group in grouped[RUNTIME_ERRORS_PER_QUERY:])
    return [group for group, _ in reported], pending


def _tool_get_runtime(runner: AgentRunner, _args: dict[str, Any]) -> Any:
    """运行时状态：任务状态 / 阶段 / 本轮计数 / ETA / 新出现的错误。

    只返回数据本身——口径说明（summary 是本轮任务口径、recent_errors 是增量且已同类
    合并）都写在工具的 description 里：这个工具在等待循环里被反复调用，静态说明每次
    跟着返回只是白占上下文。

    summary 是**本轮任务自己的**计数（按任务计划统计，含正在翻译、缓存还没落盘的
    文件）；「了解项目」里的 progress 是**已落盘缓存**的口径，两者分母不同，同一次
    查询下数字本来就会差一截，不需要互相校对。

    recent_errors 只给"上次查询之后新出现的"（水位线记在 state 上），避免同一条
    parse warning 在连续几次查询里反复出现、逼模型重新判断。
    """
    pid = runner._project_id()
    data = runner._http_get(f"/api/projects/{pid}/runtime")
    job = data.get("job") or {}
    summary = data.get("summary") or {}
    raw_errors = data.get("recent_errors") or []
    fresh_errors, pending_errors = _take_fresh_errors(
        runner.state, [err for err in raw_errors if isinstance(err, dict)]
    )
    result: dict[str, Any] = {
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
        "recent_errors": fresh_errors,
    }
    if job.get("error"):
        result["job_error"] = str(job["error"])
        recovery = encoding_recovery(result["job_error"])
        if recovery:
            result["recovery"] = recovery
    if pending_errors:
        # 还有没发完的新错误：说明这次报错很密集，下次查询继续给
        result["recent_errors_pending"] = pending_errors
    return result
