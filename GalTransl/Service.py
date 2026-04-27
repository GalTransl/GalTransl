from __future__ import annotations

from asyncio import CancelledError, run
from dataclasses import asdict, dataclass, field
from datetime import datetime
import os
import traceback
from typing import Any

from GalTransl import LOGGER, DEBUG_LEVEL
from GalTransl.Cache import compact_cache_append_logs
from GalTransl.ConfigHelper import CProjectConfig
from GalTransl.Runner import run_galtransl
from GalTransl.i18n import get_text, GT_LANG
from GalTransl.AppSettings import load_app_settings


class JobCancelledError(Exception):
    pass


ERROR_LOG_MAX_BYTES = 1024 * 1024


def _utcnow_text() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _should_skip_error_log(ex: BaseException) -> bool:
    if isinstance(ex, (JobCancelledError, KeyboardInterrupt, CancelledError)):
        return True

    module_name = type(ex).__module__ or ""
    if module_name.startswith("openai"):
        return True
    if module_name.startswith("httpx"):
        return True
    if module_name.startswith("requests"):
        return True

    return False


def _resolve_error_log_path(project_dir: str) -> str | None:
    normalized_dir = str(project_dir or "").strip()
    if not normalized_dir:
        return None
    try:
        os.makedirs(normalized_dir, exist_ok=True)
    except OSError:
        return None
    return os.path.join(normalized_dir, "error.log")


def _append_error_log(spec: JobSpec, ex: BaseException, *, phase: str) -> None:
    if _should_skip_error_log(ex):
        return

    log_path = _resolve_error_log_path(spec.project_dir)
    if not log_path:
        return

    trace_text = traceback.format_exc().strip()
    if not trace_text or trace_text == "NoneType: None":
        trace_text = "".join(traceback.format_exception(type(ex), ex, ex.__traceback__)).strip()

    lines = [
        f"[{_utcnow_text()}] phase={phase}",
        f"job_id={spec.job_id or '-'}",
        f"project_dir={spec.project_dir}",
        f"translator={spec.translator}",
        f"config_file_name={spec.config_file_name}",
        f"exception_type={type(ex).__module__}.{type(ex).__name__}",
        f"exception_message={str(ex)}",
        "traceback:",
        trace_text,
        "",
    ]
    payload = "\n".join(lines).encode("utf-8", errors="replace")
    try:
        if os.path.isfile(log_path):
            current_size = os.path.getsize(log_path)
        else:
            current_size = 0

        if current_size + len(payload) <= ERROR_LOG_MAX_BYTES:
            with open(log_path, "ab") as f:
                f.write(payload)
            return

        existing = b""
        if current_size > 0:
            with open(log_path, "rb") as f:
                existing = f.read()

        combined = existing + payload
        trimmed = combined[-ERROR_LOG_MAX_BYTES:]
        trimmed = trimmed.decode("utf-8", errors="ignore").encode("utf-8")

        with open(log_path, "wb") as f:
            f.write(trimmed)
    except OSError:
        return


@dataclass(slots=True)
class JobSpec:
    project_dir: str
    translator: str
    config_file_name: str = "config.yaml"
    job_id: str = ""
    backend_profile: str = ""
    backend_profile_data: dict[str, Any] = field(default_factory=dict)
    prompt_template_overrides: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass(slots=True)
class JobState:
    job_id: str
    project_dir: str
    translator: str
    config_file_name: str
    status: str = "pending"
    success: bool = False
    error: str = ""
    created_at: str = field(default_factory=_utcnow_text)
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def create_job_state(spec: JobSpec) -> JobState:
    return JobState(
        job_id=spec.job_id,
        project_dir=spec.project_dir,
        translator=spec.translator,
        config_file_name=spec.config_file_name,
    )


async def run_job_async(
    spec: JobSpec,
    state: JobState | None = None,
    stop_event=None,
) -> JobState:
    from GalTransl.server import reset_runtime_project, update_runtime_status

    current_state = state or create_job_state(spec)
    cfg: CProjectConfig | None = None
    current_state.started_at = _utcnow_text()
    current_state.finished_at = ""
    current_state.status = "running"
    current_state.success = False
    current_state.error = ""
    reset_runtime_project(spec.project_dir)

    if not spec.project_dir or not isinstance(spec.project_dir, str):
        current_state.status = "failed"
        current_state.error = get_text("error_project_path_empty", GT_LANG)
        current_state.finished_at = _utcnow_text()
        LOGGER.error(current_state.error)
        return current_state
    if not spec.config_file_name or not isinstance(spec.config_file_name, str):
        current_state.status = "failed"
        current_state.error = get_text("error_config_file_empty", GT_LANG)
        current_state.finished_at = _utcnow_text()
        LOGGER.error(current_state.error)
        return current_state
    if not spec.translator or not isinstance(spec.translator, str):
        current_state.status = "failed"
        current_state.error = get_text("error_translator_empty", GT_LANG)
        current_state.finished_at = _utcnow_text()
        LOGGER.error(current_state.error)
        return current_state

    try:
        cfg = CProjectConfig(spec.project_dir, spec.config_file_name)
        cfg.non_interactive = True  # 前端启动，非交互模式
        cfg.runtime_project_dir = spec.project_dir
        app_settings = load_app_settings()
        cfg.print_translation_log_in_terminal = bool(app_settings.get("printTranslationLogInTerminal", True))
        LOGGER.setLevel(
            DEBUG_LEVEL[cfg.getCommonConfigSection().get("loggingLevel", "info")]
        )

        profile = spec.backend_profile_data if isinstance(spec.backend_profile_data, dict) else {}
        if not profile and spec.backend_profile:
            from GalTransl.server import _read_backend_profiles
            profiles_data = _read_backend_profiles()
            profiles = profiles_data.get("profiles", {})
            if spec.backend_profile in profiles:
                candidate = profiles[spec.backend_profile]
                if isinstance(candidate, dict):
                    profile = candidate
            else:
                LOGGER.warning("Backend profile not found: %s", spec.backend_profile)
        if profile:
            cfg.projectConfig["backendSpecific"] = profile
            if "proxy" in profile:
                cfg.projectConfig["proxy"] = profile["proxy"]
                cfg.keyValues["internals.enableProxy"] = profile["proxy"].get("enableProxy", False)
            LOGGER.info("Applied backend profile: %s", spec.backend_profile or "inline")

        # Apply prompt template overrides from job spec
        prompt_overrides = spec.prompt_template_overrides or {}
        template_override = prompt_overrides.get(spec.translator)
        if isinstance(template_override, dict):
            system_prompt_override = template_override.get("system_prompt")
            user_prompt_override = template_override.get("user_prompt")
            if isinstance(system_prompt_override, str):
                cfg.keyValues["internals.prompt_template.system_prompt_override"] = system_prompt_override
            if isinstance(user_prompt_override, str):
                cfg.keyValues["internals.prompt_template.user_prompt_override"] = user_prompt_override
            if isinstance(system_prompt_override, str) or isinstance(user_prompt_override, str):
                LOGGER.info("Applied prompt template override from job spec: %s", spec.translator)

    except Exception as ex:
        _append_error_log(spec, ex, phase="load_config")
        current_state.status = "failed"
        current_state.error = get_text("error_loading_config", GT_LANG, str(ex))
        current_state.finished_at = _utcnow_text()
        LOGGER.error(current_state.error)
        return current_state

    try:
        update_runtime_status(spec.project_dir, workers_active=0, workers_configured=int(cfg.getKey("workersPerProject") or 1))
        await run_galtransl(cfg, spec.translator, stop_event=stop_event)
        current_state.status = "completed"
        current_state.success = True
    except JobCancelledError:
        current_state.status = "cancelled"
        current_state.error = "用户请求停止翻译"
    except KeyboardInterrupt:
        current_state.status = "cancelled"
        current_state.error = get_text("goodbye", GT_LANG)
    except RuntimeError as ex:
        _append_error_log(spec, ex, phase="run_job")
        current_state.status = "failed"
        current_state.error = get_text("program_error", GT_LANG, ex)
        LOGGER.error(current_state.error)
    except BaseException as ex:
        _append_error_log(spec, ex, phase="run_job")
        current_state.status = "failed"
        current_state.error = get_text("error_unexpected", GT_LANG, str(ex))
        LOGGER.error(current_state.error, exc_info=True)
    finally:
        if current_state.status == "cancelled" and cfg is not None:
            try:
                compacted = await compact_cache_append_logs(cfg.getCachePath())
                if compacted > 0:
                    LOGGER.info(f"[cache]停止翻译后已合并 {compacted} 个增量缓存文件")
            except Exception as ex:
                LOGGER.warning(f"[cache]停止翻译后合并增量缓存失败：{str(ex)}")
        current_state.finished_at = _utcnow_text()
        update_runtime_status(spec.project_dir, workers_active=0)

    return current_state


def run_job(spec: JobSpec, state: JobState | None = None, stop_event=None) -> JobState:
    return run(run_job_async(spec, state, stop_event=stop_event))
