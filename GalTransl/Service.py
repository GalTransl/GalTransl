from __future__ import annotations

from asyncio import run
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from GalTransl import LOGGER, DEBUG_LEVEL
from GalTransl.Cache import compact_cache_append_logs
from GalTransl.ConfigHelper import CProjectConfig
from GalTransl.Runner import run_galtransl
from GalTransl.i18n import get_text, GT_LANG
from GalTransl.AppSettings import load_app_settings


class JobCancelledError(Exception):
    pass


def _utcnow_text() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


@dataclass(slots=True)
class JobSpec:
    project_dir: str
    translator: str
    config_file_name: str = "config.yaml"
    job_id: str = ""
    backend_profile: str = ""
    backend_profile_data: dict[str, Any] = field(default_factory=dict)


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

    except Exception as ex:
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
        current_state.status = "failed"
        current_state.error = get_text("program_error", GT_LANG, ex)
        LOGGER.error(current_state.error)
    except BaseException as ex:
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
