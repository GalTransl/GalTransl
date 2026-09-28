from __future__ import annotations

import itertools
import json
import os
import re
import threading
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version
from yaml import safe_load

from GalTransl import CACHE_FOLDERNAME
from GalTransl.ProblemFilter import filter_problem_text, normalize_problem_filter_keys
from GalTransl.ProblemWhiteList import (
    build_problem_white_list_index,
    is_problem_whitelisted,
    normalize_problem_white_list,
)

def _utcnow_text() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _normalize_version_text(value: str) -> str:
    return value.strip().removeprefix("v").removeprefix("V")


def _has_newer_release(current_version: str, latest_version: str | None) -> bool:
    if not latest_version:
        return False

    current_text = _normalize_version_text(current_version)
    latest_text = _normalize_version_text(latest_version)
    try:
        return Version(latest_text) > Version(current_text)
    except InvalidVersion:
        return latest_text != current_text


def _normalize_project_dir(project_dir: str) -> str:
    return str(Path(project_dir).resolve())


class _ConcurrentLimitError(ValueError):
    """Raised when the global concurrent job limit has been reached."""
    pass


@dataclass(slots=True)
class RuntimeSentenceEvent:
    id: str
    ts: str
    filename: str
    index: int
    speaker: str | list[str] | None
    source_preview: str
    translation_preview: str
    trans_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RuntimeErrorEvent:
    id: str
    ts: str
    kind: str
    level: str
    message: str
    filename: str = ""
    index_range: str = ""
    retry_count: int | None = None
    model: str = ""
    sleep_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


RUNTIME_RECENT_EVENT_LIMIT = 80
RUNTIME_PER_FILE_SUCCESS_LIMIT = 100
# Upper bound on the flat list of success events returned per snapshot. Each
# translating file keeps its own 100-slot deque, but returning all of them every
# poll would quickly explode the HTTP payload. 500 is enough to satisfy the
# UI's 100-card render budget plus any per-file filter on a small number of
# concurrently active files.
RUNTIME_SNAPSHOT_SUCCESS_LIMIT = 500


@dataclass(slots=True)
class RuntimeState:
    project_dir: str
    workers_active: int = 0
    workers_configured: int = 0
    stage: str = ""
    current_file: str = ""
    updated_at: str = field(default_factory=_utcnow_text)
    file_totals: dict[str, int] = field(default_factory=dict)
    cache_file_display_map: dict[str, str] = field(default_factory=dict)
    # Per-file deque of recent success events. Each file keeps up to
    # RUNTIME_PER_FILE_SUCCESS_LIMIT events independently so that concurrent
    # translations of multiple files do not evict each other's cards.
    # Each deque stores newest-first (appendleft) for O(1) merging.
    recent_successes_by_file: dict[str, deque[RuntimeSentenceEvent]] = field(default_factory=dict)
    recent_errors: deque[RuntimeErrorEvent] = field(default_factory=lambda: deque(maxlen=RUNTIME_RECENT_EVENT_LIMIT))
    success_timestamps: deque[float] = field(default_factory=deque)
    # 引擎自报的速度（单位与它上报的进度计数一致，即「完成项/分」），None 表示没报。
    # 默认的实时速度是「最近一分钟的成功事件数」，对普通翻译来说一个成功事件正好是一句话，
    # 与进度计数（句）同口径，所以引擎不用报；GenDic 的进度单位是分片/批次，而成功事件是
    # 这一段抽出的一个个术语（一段几十个），两者不是一个单位，拿成功事件算 ETA 会小一个数量级，
    # 所以它自己按「完成项数/耗时」报上来（见 GenDic._progress_speed_lpm）。
    progress_speed_lpm: float | None = None
    # 此刻在跑的请求：{请求编号: _RequestActivity}。「文件进度」那一行据此点小灯
    # （请求中黄 / 思考中蓝 / 翻译中绿 / 重试中红，光晕的呼吸快慢跟着输出的字/秒走）。
    requests: dict[int, "_RequestActivity"] = field(default_factory=dict)


# 在出字的请求多久没有新输出就算停住了：灯从蓝/绿退回「请求中」，字/秒也归零。
# 流式里两个 chunk 之间正常隔不了这么久
REQUEST_STALL_SECONDS = 3.0
# 算「字/秒」的滑动窗口：与上面一样长，停住的请求窗口里正好没有样本，速度自然是 0
_REQUEST_RATE_WINDOW_SECONDS = 3.0
# 兜底：登记后这么久既没动静也没注销，就当它丢了。正常路径都会注销（ask_chatbot 的 finally），
# 这只防万一、别让一盏灯永远亮着；比任何接口超时都宽裕，不会误摘一个还在等响应的请求
_REQUEST_ABANDON_SECONDS = 30 * 60
# 同一个文件同时有几个请求（切块并发、GenDic 多线程）时那一行显示哪个阶段：有一个在出正文就是
# 「翻译中」，其次「思考中」「请求中」，全都在退避才是「重试中」
_REQUEST_PHASE_RANK = {"retrying": 0, "waiting": 1, "thinking": 2, "writing": 3}
# 请求编号全局递增：项目重置（新任务开始）后，上一轮还没收尾的请求报上来也对不上号，直接忽略
_REQUEST_IDS = itertools.count(1)


@dataclass(slots=True)
class _RequestActivity:
    """一次请求（某个文件的一批句子，含重试）从发出到返回的状态。

    phase：waiting 请求已发出、还没出字 / thinking 在吐思考内容 / writing 在出正文 /
    retrying 上一次失败了、正在退避等下一次。时间一律是 time.monotonic()。
    """

    filename: str
    phase: str
    updated_at: float
    phase_started_at: float
    last_output_at: float = 0.0
    samples: deque[tuple[float, int]] = field(default_factory=deque)

    def note(self, phase: str, chars: int, now: float) -> None:
        if phase != self.phase:
            # 换阶段就重新起窗：思考和出正文的速度不是一回事，别让思考的字数把绿灯一上来就撑得飞快
            self.phase = phase
            self.phase_started_at = now
            self.samples.clear()
        if chars > 0:
            self.samples.append((now, chars))
            self.last_output_at = now
        self.updated_at = now

    def shown_phase(self, now: float) -> str:
        """界面上显示的阶段：在出字的请求停住超过 REQUEST_STALL_SECONDS，就退回「请求中」。"""
        if self.phase in ("thinking", "writing") and now - self.last_output_at > REQUEST_STALL_SECONDS:
            return "waiting"
        return self.phase

    def rate_per_second(self, now: float) -> float:
        cutoff = now - _REQUEST_RATE_WINDOW_SECONDS
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()
        if not self.samples:
            return 0.0
        # 分母取「这一阶段开始到现在」与窗口长度里短的那个，且至少按 0.5s 算：
        # 刚开始出字时只有一两笔样本，别报出个天文数字
        span = max(min(now - self.phase_started_at, _REQUEST_RATE_WINDOW_SECONDS), 0.5)
        return sum(chars for _, chars in self.samples) / span


class RuntimeRegistry:
    def __init__(self) -> None:
        self._states: dict[str, RuntimeState] = {}
        # 请求编号 → 它登记在哪个项目的状态上：上报/注销只带编号，不必每次都去解析项目路径
        self._request_owners: dict[int, RuntimeState] = {}
        self._lock = threading.Lock()

    def ensure_project(self, project_dir: str) -> RuntimeState:
        normalized = _normalize_project_dir(project_dir)
        with self._lock:
            state = self._states.get(normalized)
            if state is None:
                state = RuntimeState(project_dir=project_dir)
                self._states[normalized] = state
            else:
                state.project_dir = project_dir
            state.updated_at = _utcnow_text()
            return state

    def reset_project(self, project_dir: str) -> None:
        with self._lock:
            normalized = _normalize_project_dir(project_dir)
            previous = self._states.get(normalized)
            if previous is not None:
                # 上一轮还没收尾的请求：之后再报上来/注销都对不上号，按忽略处理
                for request_id in previous.requests:
                    self._request_owners.pop(request_id, None)
            self._states[normalized] = RuntimeState(project_dir=project_dir)

    def update_status(
        self,
        project_dir: str,
        *,
        stage: str | None = None,
        current_file: str | None = None,
        workers_active: int | None = None,
        workers_configured: int | None = None,
        file_totals: dict[str, int] | None = None,
        cache_file_display_map: dict[str, str] | None = None,
        progress_speed_lpm: float | None = None,
    ) -> None:
        with self._lock:
            state = self._states.get(_normalize_project_dir(project_dir))
            if state is None:
                state = RuntimeState(project_dir=project_dir)
                self._states[_normalize_project_dir(project_dir)] = state
            if stage is not None:
                state.stage = stage
            if current_file is not None:
                state.current_file = current_file
            if workers_active is not None:
                state.workers_active = max(0, workers_active)
            if workers_configured is not None:
                state.workers_configured = max(0, workers_configured)
            if file_totals is not None:
                state.file_totals = dict(file_totals)
            if cache_file_display_map is not None:
                state.cache_file_display_map = dict(cache_file_display_map)
            if progress_speed_lpm is not None:
                state.progress_speed_lpm = max(0.0, float(progress_speed_lpm))
            state.updated_at = _utcnow_text()

    def begin_request(self, project_dir: str, *, filename: str) -> int | None:
        """登记「这个文件有一个请求开始跑了」（阶段是请求中），返回请求编号，交给 note_request / end_request。

        引擎报的是它自己的文件名（切块的带 `_<n>`、多级目录用 `-}` 拼），这里换成「文件进度」那一行的
        显示名；认不出是本次任务登记过的哪一行就返回 None、不登记——宁可没有灯，也别点到别的文件上。
        """
        now = time.monotonic()
        with self._lock:
            state = self._states.get(_normalize_project_dir(project_dir))
            if state is None:
                return None
            display = self._resolve_display_filename_locked(state, filename)
            if display not in state.file_totals:
                return None
            request_id = next(_REQUEST_IDS)
            state.requests[request_id] = _RequestActivity(
                filename=display, phase="waiting", updated_at=now, phase_started_at=now
            )
            self._request_owners[request_id] = state
            return request_id

    def note_request(self, request_id: int, *, phase: str, chars: int = 0) -> None:
        """这个请求换了阶段 / 又出了 chars 个字（phase 取 waiting / thinking / writing / retrying）。"""
        now = time.monotonic()
        with self._lock:
            state = self._request_owners.get(request_id)
            activity = state.requests.get(request_id) if state is not None else None
            if activity is not None:
                activity.note(phase, max(0, int(chars)), now)

    def end_request(self, request_id: int) -> None:
        """请求结束（成功、重试到上限、取消都算）：这一行不再因为它亮灯。"""
        with self._lock:
            state = self._request_owners.pop(request_id, None)
            if state is not None:
                state.requests.pop(request_id, None)

    def append_success(
        self,
        project_dir: str,
        *,
        filename: str,
        index: int,
        speaker: str | list[str] | None,
        source_preview: str,
        translation_preview: str,
        trans_by: str = "",
    ) -> None:
        now = datetime.utcnow().timestamp()
        with self._lock:
            state = self._states.get(_normalize_project_dir(project_dir))
            if state is None:
                state = RuntimeState(project_dir=project_dir)
                self._states[_normalize_project_dir(project_dir)] = state
            display_filename = self._resolve_display_filename_locked(state, filename)
            event = RuntimeSentenceEvent(
                id=f"{display_filename}:{index}:{int(now * 1000)}",
                ts=_utcnow_text(),
                filename=display_filename,
                index=index,
                speaker=speaker,
                source_preview=_trim_preview(source_preview),
                translation_preview=_trim_preview(translation_preview),
                trans_by=trans_by,
            )
            file_deque = state.recent_successes_by_file.get(display_filename)
            if file_deque is None:
                file_deque = deque(maxlen=RUNTIME_PER_FILE_SUCCESS_LIMIT)
                state.recent_successes_by_file[display_filename] = file_deque
            file_deque.appendleft(event)
            state.success_timestamps.append(now)
            self._trim_speed_window_locked(state, now)
            state.updated_at = event.ts

    def append_error(
        self,
        project_dir: str,
        *,
        kind: str,
        message: str,
        filename: str = "",
        index_range: str = "",
        retry_count: int | None = None,
        model: str = "",
        sleep_seconds: float | None = None,
        level: str = "error",
    ) -> None:
        with self._lock:
            state = self._states.get(_normalize_project_dir(project_dir))
            if state is None:
                state = RuntimeState(project_dir=project_dir)
                self._states[_normalize_project_dir(project_dir)] = state
            display_filename = self._resolve_display_filename_locked(state, filename)
            ts = _utcnow_text()
            state.recent_errors.appendleft(RuntimeErrorEvent(
                id=f"{kind}:{display_filename}:{int(datetime.utcnow().timestamp() * 1000)}",
                ts=ts,
                kind=kind,
                level=level,
                message=_trim_preview(message, 240),
                filename=display_filename,
                index_range=index_range,
                retry_count=retry_count,
                model=model,
                sleep_seconds=sleep_seconds,
            ))
            state.updated_at = ts

    @staticmethod
    def _resolve_display_filename_locked(state: RuntimeState, filename: str) -> str:
        normalized = str(filename or "").strip()
        if not normalized:
            return ""

        candidates: list[str] = []

        def add_candidate(value: str) -> None:
            candidate = str(value or "").strip()
            if candidate and candidate not in candidates:
                candidates.append(candidate)

        add_candidate(normalized)
        add_candidate(f"{normalized}.json")
        add_candidate(f"{normalized}{_CACHE_APPEND_SUFFIX}")

        split_match = re.match(r"^(.*)_\d+$", normalized)
        if split_match:
            split_base = split_match.group(1)
            add_candidate(split_base)
            add_candidate(f"{split_base}.json")
            add_candidate(f"{split_base}{_CACHE_APPEND_SUFFIX}")

        normalized_path = normalized.replace("-}", "/")
        add_candidate(normalized_path)
        add_candidate(f"{normalized_path}.json")
        add_candidate(f"{normalized_path}{_CACHE_APPEND_SUFFIX}")

        split_path_match = re.match(r"^(.*)_\d+$", normalized_path)
        if split_path_match:
            split_path_base = split_path_match.group(1)
            add_candidate(split_path_base)
            add_candidate(f"{split_path_base}.json")
            add_candidate(f"{split_path_base}{_CACHE_APPEND_SUFFIX}")

        for candidate in candidates:
            display = state.cache_file_display_map.get(candidate)
            if display:
                return display

        if normalized_path in state.file_totals:
            return normalized_path
        if split_path_match and split_path_match.group(1) in state.file_totals:
            return split_path_match.group(1)
        if normalized in state.file_totals:
            return normalized

        return normalized_path

    def get_runtime_snapshot(self, project_dir: str) -> dict[str, Any]:
        normalized = _normalize_project_dir(project_dir)
        with self._lock:
            state = self._states.get(normalized)
            if state is None:
                return {
                    "stage": "",
                    "current_file": "",
                    "workers_active": 0,
                    "workers_configured": 0,
                    "translation_speed_lpm": 0,
                    "file_totals": {},
                    "cache_file_display_map": {},
                    "activity": {},
                    "recent_errors": [],
                    "recent_successes": [],
                    "updated_at": _utcnow_text(),
                }
            now = datetime.utcnow().timestamp()
            self._trim_speed_window_locked(state, now)
            activity = self._activity_snapshot_locked(state, time.monotonic())
            speed = round((len(state.success_timestamps) / 60) * 60, 1) if state.success_timestamps else 0
            if state.progress_speed_lpm is not None:
                # 引擎自报的速度优先：它的单位与进度计数一致，前端的「实时速度/预计剩余」
                # 以及下面按它算的 eta_seconds 才跟 x/y 项的进度对得上
                speed = state.progress_speed_lpm
            # Flatten per-file success deques (each newest-first) and re-order
            # globally by timestamp desc so the snapshot list remains newest-first
            # for existing clients.
            merged_successes: list[RuntimeSentenceEvent] = []
            for file_deque in state.recent_successes_by_file.values():
                merged_successes.extend(file_deque)
            merged_successes.sort(key=lambda ev: ev.ts, reverse=True)
            if len(merged_successes) > RUNTIME_SNAPSHOT_SUCCESS_LIMIT:
                merged_successes = merged_successes[:RUNTIME_SNAPSHOT_SUCCESS_LIMIT]
            return {
                "stage": state.stage,
                "current_file": state.current_file,
                "workers_active": state.workers_active,
                "workers_configured": state.workers_configured,
                "translation_speed_lpm": speed,
                "file_totals": dict(state.file_totals),
                "cache_file_display_map": dict(state.cache_file_display_map),
                "activity": activity,
                "recent_errors": [event.to_dict() for event in state.recent_errors],
                "recent_successes": [event.to_dict() for event in merged_successes],
                "updated_at": state.updated_at,
            }

    @staticmethod
    def _trim_speed_window_locked(state: RuntimeState, now: float) -> None:
        while state.success_timestamps and now - state.success_timestamps[0] > 60:
            state.success_timestamps.popleft()

    def _activity_snapshot_locked(self, state: RuntimeState, now: float) -> dict[str, dict[str, Any]]:
        """{显示名: {phase, cps, requests}}，只给此刻有请求在跑的文件；顺手摘掉丢了的请求。

        同一个文件可能同时有几个请求（切块并发、GenDic 多线程）：阶段取最往前的那个
        （见 _REQUEST_PHASE_RANK），字/秒加总，requests 是请求数——分开报的话那一行会
        在几个请求的阶段之间来回跳。
        """
        snapshot: dict[str, dict[str, Any]] = {}
        for request_id, activity in list(state.requests.items()):
            if now - activity.updated_at > _REQUEST_ABANDON_SECONDS:
                del state.requests[request_id]
                self._request_owners.pop(request_id, None)
                continue
            phase = activity.shown_phase(now)
            entry = snapshot.get(activity.filename)
            if entry is None:
                entry = snapshot[activity.filename] = {"phase": phase, "cps": 0.0, "requests": 0}
            elif _REQUEST_PHASE_RANK[phase] > _REQUEST_PHASE_RANK[entry["phase"]]:
                entry["phase"] = phase
            entry["cps"] += activity.rate_per_second(now)
            entry["requests"] += 1
        for entry in snapshot.values():
            entry["cps"] = round(entry["cps"], 1)
        return snapshot


def _trim_preview(value: str, limit: int = 140) -> str:
    normalized = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)] + "…"


RUNTIME_REGISTRY = RuntimeRegistry()
_CACHE_APPEND_SUFFIX = ".append.jsonl"


@dataclass(slots=True)
class _CacheProgressFileStat:
    mtime_ns: int
    size: int
    translated_keys: frozenset[str]
    problem_keys: frozenset[str]
    failed_keys: frozenset[str]
    retran_terms_signature: tuple[str, ...] = field(default_factory=tuple)
    retran_hit_keys: dict[str, frozenset[str]] = field(default_factory=dict)
    problem_filter_signature: tuple[str, ...] = field(default_factory=tuple)
    problem_white_signature: tuple[str, ...] = field(default_factory=tuple)


@dataclass(slots=True)
class _RetranConfigStat:
    mtime_ns: int
    size: int
    retran_key: str | list[str]
    problem_filter_keys: list[str] = field(default_factory=list)
    problem_white_list: list[str] = field(default_factory=list)


def _normalize_retran_key(value: Any) -> str | list[str]:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return ""


def _normalize_retran_terms(value: str | list[str]) -> list[str]:
    if isinstance(value, str):
        normalized = value.strip()
        return [normalized] if normalized else []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            text = str(item or "").strip()
            if text:
                result.append(text)
        return result
    return []


def _check_retran_key(retran_key: str | list[str], target: Any) -> bool:
    text = str(target or "")
    if isinstance(retran_key, str):
        return bool(retran_key) and retran_key in text
    if isinstance(retran_key, list):
        return any(key in text for key in retran_key if key)
    return False


def _parse_runtime_job_started_at_ns(value: str) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        normalized = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        return int(dt.timestamp() * 1_000_000_000)
    except Exception:
        return None


class RuntimeProgressCache:
    def __init__(self) -> None:
        self._project_files: dict[str, dict[str, _CacheProgressFileStat]] = {}
        self._retran_config_cache: dict[str, _RetranConfigStat] = {}
        self._lock = threading.Lock()

    def reset_project(self, project_dir: str) -> None:
        normalized = _normalize_project_dir(project_dir)
        with self._lock:
            self._project_files.pop(normalized, None)

    def get_retran_key(self, project_dir: str, config_file_name: str = "config.yaml") -> str | list[str]:
        config_path = os.path.join(project_dir, config_file_name or "config.yaml")
        normalized_config = str(Path(config_path).resolve())

        try:
            stat = os.stat(config_path)
        except OSError:
            with self._lock:
                self._retran_config_cache.pop(normalized_config, None)
            return ""

        with self._lock:
            cached = self._retran_config_cache.get(normalized_config)
            if (
                cached is not None
                and cached.mtime_ns == int(stat.st_mtime_ns)
                and cached.size == int(stat.st_size)
            ):
                return cached.retran_key

        retran_key: str | list[str] = ""
        problem_filter_keys = []
        problem_white_list = []
        try:
            with open(config_path, "rb") as cfg_file:
                cfg = safe_load(cfg_file.read()) or {}
            common = cfg.get("common", {}) if isinstance(cfg, dict) else {}
            retran_key = _normalize_retran_key(common.get("retranslKey", ""))
            problem_filter_keys = normalize_problem_filter_keys(common.get("problemFilterKey", []))
            problem_white_list = normalize_problem_white_list(common.get("problemWhiteList", []))
        except Exception:
            retran_key = ""

        with self._lock:
            self._retran_config_cache[normalized_config] = _RetranConfigStat(
                mtime_ns=int(stat.st_mtime_ns),
                size=int(stat.st_size),
                retran_key=retran_key,
                problem_filter_keys=problem_filter_keys,
                problem_white_list=problem_white_list,
            )

        return retran_key

    def get_problem_filter_keys(self, project_dir: str, config_file_name: str = "config.yaml") -> list[str]:
        self.get_retran_key(project_dir, config_file_name)
        config_path = str(Path(project_dir, config_file_name or "config.yaml").resolve())
        with self._lock:
            cached = self._retran_config_cache.get(config_path)
            return list(cached.problem_filter_keys) if cached else []

    def get_problem_white_list(self, project_dir: str, config_file_name: str = "config.yaml") -> list[str]:
        self.get_retran_key(project_dir, config_file_name)
        config_path = str(Path(project_dir, config_file_name or "config.yaml").resolve())
        with self._lock:
            cached = self._retran_config_cache.get(config_path)
            return list(cached.problem_white_list) if cached else []

    def get_progress(
        self,
        project_dir: str,
        file_totals: dict[str, int],
        cache_file_display_map: dict[str, str],
        retran_key: str | list[str] = "",
        retran_terms: list[str] | None = None,
        current_job_started_at_ns: int | None = None,
        problem_filter_keys=None,
        problem_white_list=None,
    ) -> dict[str, Any]:
        normalized = _normalize_project_dir(project_dir)
        cache_dir = os.path.join(project_dir, CACHE_FOLDERNAME)
        retran_terms = retran_terms or []
        retran_terms_signature = tuple(retran_terms)
        problem_filter_keys = normalize_problem_filter_keys(problem_filter_keys)
        problem_filter_signature = tuple(problem_filter_keys)
        problem_white_list = normalize_problem_white_list(problem_white_list)
        problem_white_signature = tuple(problem_white_list)
        problem_white_index = build_problem_white_list_index(problem_white_list)

        with self._lock:
            project_stats = self._project_files.setdefault(normalized, {})
            seen_files: set[str] = set()

            if os.path.isdir(cache_dir):
                for entry in os.scandir(cache_dir):
                    if not entry.is_file():
                        continue
                    if not (
                        entry.name.endswith(".json")
                        or entry.name.endswith(_CACHE_APPEND_SUFFIX)
                    ):
                        continue

                    name = entry.name
                    seen_files.add(name)

                    try:
                        stat = entry.stat()
                    except OSError:
                        continue

                    cached = project_stats.get(name)
                    if (
                        cached is not None
                        and cached.mtime_ns == int(stat.st_mtime_ns)
                        and cached.size == int(stat.st_size)
                        and cached.retran_terms_signature == retran_terms_signature
                        and cached.problem_filter_signature == problem_filter_signature
                        and cached.problem_white_signature == problem_white_signature
                    ):
                        continue

                    translated_keys: set[str] = set()
                    problem_keys: set[str] = set()
                    failed_keys: set[str] = set()
                    retran_hit_keys: dict[str, set[str]] = {term: set() for term in retran_terms}

                    def _name_src(items: list[Any], idx: int) -> str:
                        if idx < 0 or idx >= len(items):
                            return ""
                        item = items[idx]
                        if not isinstance(item, dict):
                            return ""
                        name = str(item.get("name", "") or "")
                        pre_src = str(item.get("pre_src", item.get("pre_jp", "")) or "")
                        return f"{name}{pre_src}"

                    def _entry_signature(items: list[Any], idx: int) -> str:
                        line_now = _name_src(items, idx)
                        row = items[idx] if 0 <= idx < len(items) else {}
                        row_index = str(row.get("index", "")) if isinstance(row, dict) else ""
                        if not line_now:
                            if isinstance(row, dict):
                                row_src = str(
                                    row.get("pre_src", row.get("pre_jp", row.get("post_src", "")))
                                    or ""
                                )
                                row_name = str(row.get("name", "") or "")
                                return f"__row__:{idx}:{row_index}:{row_name}:{row_src}"
                            return f"__row__:{idx}"

                        line_prev = "None"
                        j = idx - 1
                        while j >= 0:
                            candidate = _name_src(items, j)
                            if candidate:
                                line_prev = candidate
                                break
                            j -= 1

                        line_next = "None"
                        j = idx + 1
                        while j < len(items):
                            candidate = _name_src(items, j)
                            if candidate:
                                line_next = candidate
                                break
                            j += 1

                        # 在 context key 前拼接 entry 的 index，使相同上下文三元组但位于
                        # 不同位置的条目（如重复短句）生成不同 key，避免 set 去重导致
                        # 进度少计。index 前缀同时保证 .json 与 .append.jsonl 对同一
                        # 位置条目仍能正确去重（二者 index 相同 → key 相同）。
                        context_key = f"{line_prev}{line_now}{line_next}"
                        if row_index:
                            return f"{row_index}:{context_key}"
                        return context_key

                    entries: list[Any] = []
                    try:
                        import orjson
                        with open(entry.path, "rb") as f:
                            raw = f.read()
                        if entry.name.endswith(_CACHE_APPEND_SUFFIX):
                            for line in raw.splitlines():
                                if not line:
                                    continue
                                try:
                                    row = orjson.loads(line)
                                except Exception:
                                    continue
                                if isinstance(row, dict):
                                    entries.append(row)
                        else:
                            loaded = orjson.loads(raw)
                            if isinstance(loaded, list):
                                entries = loaded
                    except Exception:
                        continue

                    for idx, item in enumerate(entries):
                        if not isinstance(item, dict):
                            continue

                        entry_key = str(item.get("__cache_key", "")).strip()
                        if entry_key:
                            # 同 _entry_signature：以 index 为前缀使不同位置的同文本条目
                            # 在 set 中各占一席，同时保持与 .json 快照 key 的一致性。
                            item_index = str(item.get("index", ""))
                            if item_index:
                                entry_key = f"{item_index}:{entry_key}"
                        else:
                            entry_key = _entry_signature(entries, idx)

                        is_translated = bool(item.get("pre_dst", "") or item.get("pre_zh", ""))
                        problem_text = filter_problem_text(item.get("problem", ""), problem_filter_keys)
                        # 白名单命中：等价于该条勾了 skip_check，问题整体不算
                        if is_problem_whitelisted(problem_white_index, entry.name, item.get("index", "")):
                            problem_text = ""
                        is_problem = bool(problem_text)
                        is_failed = (
                            "翻译失败" in problem_text
                            or "(Failed)" in str(item.get("pre_dst", "") or item.get("pre_zh", ""))
                            or "(翻译失败)" in str(item.get("pre_dst", "") or item.get("pre_zh", ""))
                        )

                        no_proofread = str(item.get("proofread_dst", "") or "") == ""

                        # retran_hit_keys 的统计不应受 retran_key 过滤影响：
                        # 无论当前是否处于重翻 job 中，命中重翻词条的缓存条目
                        # 都应被一致地计入 retransl_stats，避免新旧文件统计口径不一致
                        # 导致前端句数在翻译过程中逐渐增加。
                        if (
                            not entry.name.endswith(_CACHE_APPEND_SUFFIX)
                            and retran_hit_keys
                        ):
                            source_text = item.get("pre_src", item.get("pre_jp", ""))
                            for term in retran_terms:
                                if _check_retran_key(term, source_text) or _check_retran_key(term, problem_text):
                                    retran_hit_keys[term].add(entry_key)

                        should_apply_retransl_filter = not entry.name.endswith(_CACHE_APPEND_SUFFIX)
                        if (
                            should_apply_retransl_filter
                            and current_job_started_at_ns is not None
                            and int(stat.st_mtime_ns) >= int(current_job_started_at_ns)
                        ):
                            should_apply_retransl_filter = False
                        if (
                            is_translated
                            and should_apply_retransl_filter
                            and retran_key
                            and no_proofread
                            and (
                                _check_retran_key(retran_key, item.get("pre_src", item.get("pre_jp", "")))
                                or _check_retran_key(retran_key, problem_text)
                            )
                        ):
                            is_translated = False

                        if is_translated:
                            translated_keys.add(entry_key)
                        if is_problem:
                            problem_keys.add(entry_key)
                        if is_failed:
                            failed_keys.add(entry_key)

                    project_stats[name] = _CacheProgressFileStat(
                        mtime_ns=int(stat.st_mtime_ns),
                        size=int(stat.st_size),
                        translated_keys=frozenset(translated_keys),
                        problem_keys=frozenset(problem_keys),
                        failed_keys=frozenset(failed_keys),
                        retran_terms_signature=retran_terms_signature,
                        problem_filter_signature=problem_filter_signature,
                        problem_white_signature=problem_white_signature,
                        retran_hit_keys={
                            term: frozenset(hit_keys)
                            for term, hit_keys in retran_hit_keys.items()
                        },
                    )

            stale_files = [name for name in project_stats if name not in seen_files]
            for name in stale_files:
                project_stats.pop(name, None)

            file_progress_map: dict[str, dict[str, Any]] = {}
            retran_counts: dict[str, set[str]] = {term: set() for term in retran_terms}

            for name, stat in project_stats.items():
                canonical_name = (
                    name[: -len(_CACHE_APPEND_SUFFIX)]
                    if name.endswith(_CACHE_APPEND_SUFFIX)
                    else name
                )
                display_name = cache_file_display_map.get(canonical_name, canonical_name)
                if file_totals and display_name not in file_totals:
                    continue
                if display_name not in file_progress_map:
                    file_progress_map[display_name] = {
                        "filename": display_name,
                        "total": int(file_totals.get(display_name, 0)),
                        "translated": 0,
                        "problems": 0,
                        "failed": 0,
                        "_translated_keys": set(),
                        "_problem_keys": set(),
                        "_failed_keys": set(),
                    }
                file_progress_map[display_name]["_translated_keys"].update(stat.translated_keys)
                file_progress_map[display_name]["_problem_keys"].update(stat.problem_keys)
                file_progress_map[display_name]["_failed_keys"].update(stat.failed_keys)
                for term, hit_keys in stat.retran_hit_keys.items():
                    retran_counts.setdefault(term, set()).update(hit_keys)

            for display_name, total_count in file_totals.items():
                file_progress_map.setdefault(
                    display_name,
                    {
                        "filename": display_name,
                        "total": int(total_count),
                        "translated": 0,
                        "problems": 0,
                        "failed": 0,
                        "_translated_keys": set(),
                        "_problem_keys": set(),
                        "_failed_keys": set(),
                    },
                )

            for file_progress in file_progress_map.values():
                total_count = int(file_progress.get("total", 0))
                translated = len(file_progress["_translated_keys"])
                problems = len(file_progress["_problem_keys"])
                failed = len(file_progress["_failed_keys"])

                if total_count > 0:
                    translated = min(translated, total_count)
                    problems = min(problems, total_count)
                    failed = min(failed, total_count)

                file_progress["translated"] = translated
                file_progress["problems"] = problems
                file_progress["failed"] = failed
                file_progress.pop("_translated_keys", None)
                file_progress.pop("_problem_keys", None)
                file_progress.pop("_failed_keys", None)

            files = sorted(file_progress_map.values(), key=lambda item: item["filename"])
            return {
                "total": sum(int(item["total"]) for item in files),
                "translated": sum(int(item["translated"]) for item in files),
                "problems": sum(int(item["problems"]) for item in files),
                "failed": sum(int(item["failed"]) for item in files),
                "retransl_stats": [
                    {"key": term, "count": len(retran_counts.get(term, set()))}
                    for term in retran_terms
                ],
                "files": files,
            }


RUNTIME_PROGRESS_CACHE = RuntimeProgressCache()


def reset_runtime_project(project_dir: str) -> None:
    RUNTIME_REGISTRY.reset_project(project_dir)
    RUNTIME_PROGRESS_CACHE.reset_project(project_dir)


def update_runtime_status(
    project_dir: str,
    *,
    stage: str | None = None,
    current_file: str | None = None,
    workers_active: int | None = None,
    workers_configured: int | None = None,
    file_totals: dict[str, int] | None = None,
    cache_file_display_map: dict[str, str] | None = None,
    progress_speed_lpm: float | None = None,
) -> None:
    RUNTIME_REGISTRY.update_status(
        project_dir,
        stage=stage,
        current_file=current_file,
        workers_active=workers_active,
        workers_configured=workers_configured,
        file_totals=file_totals,
        cache_file_display_map=cache_file_display_map,
        progress_speed_lpm=progress_speed_lpm,
    )


def record_runtime_success(
    project_dir: str,
    *,
    filename: str,
    index: int,
    speaker: str | list[str] | None,
    source_preview: str,
    translation_preview: str,
    trans_by: str = "",
) -> None:
    RUNTIME_REGISTRY.append_success(
        project_dir,
        filename=filename,
        index=index,
        speaker=speaker,
        source_preview=source_preview,
        translation_preview=translation_preview,
        trans_by=trans_by,
    )


def record_runtime_error(
    project_dir: str,
    *,
    kind: str,
    message: str,
    filename: str = "",
    index_range: str = "",
    retry_count: int | None = None,
    model: str = "",
    sleep_seconds: float | None = None,
    level: str = "error",
) -> None:
    RUNTIME_REGISTRY.append_error(
        project_dir,
        kind=kind,
        message=message,
        filename=filename,
        index_range=index_range,
        retry_count=retry_count,
        model=model,
        sleep_seconds=sleep_seconds,
        level=level,
    )


def begin_runtime_request(project_dir: str, *, filename: str) -> int | None:
    """某个文件的一次请求开始了（ask_chatbot 进门）：返回请求编号，认不出这个文件时返回 None。

    工作台的「文件进度」用它给那一行点小灯，见 RuntimeRegistry.begin_request。
    """
    return RUNTIME_REGISTRY.begin_request(project_dir, filename=filename)


def note_runtime_request(request_id: int, *, phase: str, chars: int = 0) -> None:
    """请求换阶段 / 新出了 chars 个字：phase 取 waiting / thinking / writing / retrying。"""
    RUNTIME_REGISTRY.note_request(request_id, phase=phase, chars=chars)


def end_runtime_request(request_id: int) -> None:
    """请求结束（ask_chatbot 出门，不管成功失败）。"""
    RUNTIME_REGISTRY.end_request(request_id)


# ---------------------------------------------------------------------------
# Project path helpers - encode/decode directory paths for use in URLs
# ---------------------------------------------------------------------------

def encode_project_dir(project_dir: str) -> str:
    """Encode a filesystem path to a URL-safe token."""
    return urlsafe_b64encode(project_dir.encode("utf-8")).decode("ascii")


def decode_project_dir(token: str) -> str:
    """Decode a URL-safe token back to a filesystem path."""
    padding = 4 - len(token) % 4
    if padding != 4:
        token += "=" * padding
    return urlsafe_b64decode(token.encode("ascii")).decode("utf-8")


def _safe_project_dir(token: str) -> str:
    """Decode and validate a project directory token. Raises ValueError on failure."""
    try:
        project_dir = decode_project_dir(token)
    except Exception:
        raise ValueError("invalid project id")
    if not os.path.isdir(project_dir):
        raise ValueError(f"project directory does not exist: {project_dir}")
    return project_dir
